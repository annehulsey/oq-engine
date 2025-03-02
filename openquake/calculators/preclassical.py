# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2014-2022 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

import os
import logging
import operator
import numpy
from openquake.baselib import general, parallel, hdf5
from openquake.hazardlib import pmf, geo
from openquake.baselib.general import AccumDict, groupby
from openquake.hazardlib.contexts import read_cmakers, get_maxsize
from openquake.hazardlib.source.point import grid_point_sources, msr_name
from openquake.hazardlib.source.base import get_code2cls
from openquake.hazardlib.sourceconverter import SourceGroup
from openquake.hazardlib.calc.filters import split_source, SourceFilter
from openquake.hazardlib.scalerel.point import PointMSR
from openquake.calculators import base

U16 = numpy.uint16
U32 = numpy.uint32
F32 = numpy.float32
F64 = numpy.float64
TWO32 = 2 ** 32


def source_data(sources):
    data = AccumDict(accum=[])
    for src in sources:
        data['src_id'].append(src.source_id)
        data['nsites'].append(src.nsites)
        data['nrupts'].append(src.num_ruptures)
        data['weight'].append(src.weight)
        data['ctimes'].append(0)
    return data


def collapse_nphc(src):
    """
    Collapse the nodal_plane_distribution and hypocenter_distribution.
    """
    if (hasattr(src, 'nodal_plane_distribution') and
            hasattr(src, 'hypocenter_distribution')):
        if len(src.nodal_plane_distribution.data) > 1:
            ws, nps = zip(*src.nodal_plane_distribution.data)
            strike = numpy.average([np.strike for np in nps], weights=ws)
            dip = numpy.average([np.dip for np in nps], weights=ws)
            rake = numpy.average([np.rake for np in nps], weights=ws)
            val = geo.NodalPlane(strike, dip, rake)
            src.nodal_plane_distribution = pmf.PMF([(1., val)])
        if len(src.hypocenter_distribution.data) > 1:
            ws, vals = zip(*src.hypocenter_distribution.data)
            val = numpy.average(vals, weights=ws)
            src.hypocenter_distribution = pmf.PMF([(1., val)])
        src.magnitude_scaling_relationship = PointMSR()


# group together consistent point sources (same group, same msr)
def same_key(src):
    return (src.grp_id, msr_name(src))


def preclassical(srcs, sites, cmaker, monitor):
    """
    Weight the sources. Also split them if split_sources is true. If
    ps_grid_spacing is set, grid the point sources before weighting them.
    """
    split_sources = []
    spacing = cmaker.ps_grid_spacing
    grp_id = srcs[0].grp_id
    if sites is None:
        # in csm2rup just split the sources and count the ruptures
        for src in srcs:
            ss = split_source(src)
            if len(ss) > 1:
                for ss_ in ss:
                    ss_.nsites = 1
            split_sources.extend(ss)
            src.num_ruptures = src.count_ruptures()
        dic = {grp_id: split_sources}
        dic['before'] = len(srcs)
        dic['after'] = len(dic[grp_id])
        return dic

    sf = SourceFilter(sites, cmaker.maximum_distance)
    multiplier = 1 + len(sites) // 10_000
    sf = sf.reduce(multiplier)
    with monitor('filtering/splitting'):
        for src in srcs:
            # NB: this is approximate, since the sites are sampled
            src.nsites = len(sf.close_sids(src))  # can be 0
            # NB: it is crucial to split only the close sources, for
            # performance reasons (think of Ecuador in SAM)
            splits = split_source(src) if (
                cmaker.split_sources and src.nsites) else [src]
            split_sources.extend(splits)
    dic = grid_point_sources(split_sources, spacing, monitor)
    # this is also prefiltering the split sources
    mon = monitor('weighting sources', measuremem=False)
    cmaker.set_weight(dic[grp_id], sf, multiplier, mon)
    # print(mon.duration, [s.source_id for s in dic[grp_id]])
    dic['before'] = len(split_sources)
    dic['after'] = len(dic[grp_id])
    return dic


@base.calculators.add('preclassical')
class PreClassicalCalculator(base.HazardCalculator):
    """
    PreClassical PSHA calculator
    """
    core_task = preclassical
    accept_precalc = []

    def init(self):
        super().init()
        if self.oqparam.hazard_calculation_id:
            full_lt = self.datastore.parent['full_lt']
            trt_smrs = self.datastore.parent['trt_smrs'][:]
        else:
            full_lt = self.csm.full_lt
            trt_smrs = self.csm.get_trt_smrs()
        self.grp_ids = numpy.arange(len(trt_smrs))
        rlzs_by_gsim_list = full_lt.get_rlzs_by_gsim_list(trt_smrs)
        rlzs_by_g = []
        for rlzs_by_gsim in rlzs_by_gsim_list:
            for rlzs in rlzs_by_gsim.values():
                rlzs_by_g.append(rlzs)
        self.datastore.hdf5.save_vlen(
            'rlzs_by_g', [U32(rlzs) for rlzs in rlzs_by_g])

    def populate_csm(self):
        # and store full_lt and source_info
        csm = self.csm
        self.datastore['trt_smrs'] = csm.get_trt_smrs()
        self.datastore['toms'] = numpy.array(
            [sg.get_tom_toml(self.oqparam.investigation_time)
             for sg in csm.src_groups], hdf5.vstr)
        cmakers = read_cmakers(self.datastore, csm.full_lt)
        M = len(self.oqparam.imtls)
        G = max(len(cm.gsims) for cm in cmakers)
        N = get_maxsize(M, G)
        logging.info('NMG = ({:_d}, {:_d}, {:_d}) = {:.1f} MB'.format(
            N, M, G, N*M*G*8 / 1024**2))
        self.sitecol = sites = csm.sitecol if csm.sitecol else None
        # do nothing for atomic sources except counting the ruptures
        atomic_sources = []
        normal_sources = []
        reqv = 'reqv' in self.oqparam.inputs
        if reqv:
            logging.warning(
                'Using equivalent distance approximation and '
                'collapsing hypocenters and nodal planes')
        for sg in csm.src_groups:
            if reqv:
                for src in sg:
                    collapse_nphc(src)
            grp_id = sg.sources[0].grp_id
            if sg.atomic:
                cmakers[grp_id].set_weight(sg, sites)
                atomic_sources.extend(sg)
            else:
                normal_sources.extend(sg)

        # run preclassical for non-atomic sources
        sources_by_key = groupby(normal_sources, same_key)
        self.datastore.hdf5['full_lt'] = csm.full_lt
        logging.info('Starting preclassical')
        self.datastore.swmr_on()
        smap = parallel.Starmap(preclassical, h5=self.datastore.hdf5)
        for (grp_id, msr), srcs in sources_by_key.items():
            pointsources, pointlike, others = [], [], []
            for src in srcs:
                if hasattr(src, 'location'):
                    pointsources.append(src)
                elif hasattr(src, 'nodal_plane_distribution'):
                    pointlike.append(src)
                elif src.code in b'FN':  # multifault, nonparametric
                    others.extend(split_source(src)
                                  if self.oqparam.split_sources else [src])
                else:
                    others.append(src)
            if self.oqparam.ps_grid_spacing:
                if pointsources or pointlike:
                    smap.submit(
                        (pointsources + pointlike, sites, cmakers[grp_id]))
            else:
                if pointsources:
                    smap.submit_split(
                        (pointsources, sites, cmakers[grp_id]), 10, 160)
                for src in pointlike:  # area, multipoint
                    smap.submit(([src], sites, cmakers[grp_id]))
            if others:
                smap.submit_split((others, sites, cmakers[grp_id]), 10, 160)
        normal = smap.reduce()
        if atomic_sources:  # case_35
            n = len(atomic_sources)
            atomic = AccumDict({'before': n, 'after': n})
            for grp_id, srcs in groupby(
                    atomic_sources, lambda src: src.grp_id).items():
                atomic[grp_id] = srcs
        else:
            atomic = AccumDict()
        res = normal + atomic
        if ('before' in res and 'after' in res and
                res['before'] != res['after']):
            logging.info(
                'Reduced the number of point sources from {:_d} -> {:_d}'.
                format(res['before'], res['after']))
        acc = AccumDict(accum=0)
        code2cls = get_code2cls()
        for grp_id, srcs in res.items():
            # NB: grp_id can be the string "before" or "after"
            if not isinstance(grp_id, str):
                srcs.sort(key=operator.attrgetter('source_id'))
            # srcs can be empty if the minimum_magnitude filter is on
            if srcs and not isinstance(grp_id, str) and grp_id not in atomic:
                # check if OQ_SAMPLE_SOURCES is set
                ss = os.environ.get('OQ_SAMPLE_SOURCES')
                if ss:
                    logging.info('Sampled sources for group #%d', grp_id)
                    srcs = general.random_filter(srcs, float(ss)) or [srcs[0]]
                newsg = SourceGroup(srcs[0].tectonic_region_type)
                newsg.sources = srcs
                csm.src_groups[grp_id] = newsg
                for src in srcs:
                    assert src.weight
                    assert src.num_ruptures
                    acc[src.code] += int(src.num_ruptures)
        csm.fix_src_offset()
        for val, key in sorted((val, key) for key, val in acc.items()):
            cls = code2cls[key].__name__
            logging.info('{} ruptures: {:_d}'.format(cls, val))
        self.store_source_info(source_data(csm.get_sources()))
        return res

    def execute(self):
        """
        Run `preclassical(srcs, srcfilter, params, monitor)` by
        parallelizing on the sources according to their weight and
        tectonic region type.
        """
        self.populate_csm()
        self.max_weight = self.csm.get_max_weight(self.oqparam)
        return self.csm

    def post_execute(self, csm):
        """
        Store the CompositeSourceModel in binary format
        """
        self.datastore['_csm'] = csm
