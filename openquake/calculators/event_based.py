# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2015-2022 GEM Foundation
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

import time
import os.path
import logging
import operator
import numpy
import pandas

from openquake.baselib import hdf5, parallel
from openquake.baselib.general import AccumDict, copyobj, humansize
from openquake.hazardlib.probability_map import ProbabilityMap
from openquake.hazardlib.stats import geom_avg_std, compute_pmap_stats
from openquake.hazardlib.calc.stochastic import sample_ruptures
from openquake.hazardlib.gsim.base import ContextMaker, FarAwayRupture
from openquake.hazardlib.calc.filters import nofilter, getdefault, SourceFilter
from openquake.hazardlib.calc.gmf import GmfComputer
from openquake.hazardlib import InvalidFile
from openquake.hazardlib.calc.stochastic import get_rup_array, rupture_dt
from openquake.hazardlib.source.rupture import (
    RuptureProxy, EBRupture, get_ruptures)
from openquake.commonlib import (
    calc, util, logs, readinput, logictree, datastore)
from openquake.risklib.riskinput import str2rsi, rsi2str
from openquake.calculators import base, views
from openquake.calculators.getters import (
    get_rupture_getters, sig_eps_dt, time_dt)
from openquake.calculators.classical import ClassicalCalculator
from openquake.engine import engine

U8 = numpy.uint8
U16 = numpy.uint16
U32 = numpy.uint32
F32 = numpy.float32
F64 = numpy.float64
TWO32 = numpy.float64(2 ** 32)


# ######################## GMF calculator ############################ #

def get_mean_curves(dstore, imt):
    """
    Extract the mean hazard curves from the datastore, as an array of shape
    (N, L1)
    """
    if 'hcurves-stats' in dstore:  # shape (N, S, M, L1)
        arr = dstore.sel('hcurves-stats', stat='mean', imt=imt)
    else:  # there is only 1 realization
        arr = dstore.sel('hcurves-rlzs', rlz_id=0, imt=imt)
    return arr[:, 0, 0, :]

# ########################################################################## #


def count_ruptures(src):
    """
    Count the number of ruptures on a heavy source
    """
    return {src.source_id: src.count_ruptures()}


def strip_zeros(gmf_df):
    # remove the rows with all zero values
    df = gmf_df[gmf_df.columns[3:]]  # strip eid, sid, rlz
    ok = df.to_numpy().sum(axis=1) > 0
    return gmf_df[ok]


def event_based(proxies, full_lt, oqparam, dstore, monitor):
    """
    Compute GMFs and optionally hazard curves
    """
    alldata = AccumDict(accum=[])
    sig_eps = []
    times = []  # rup_id, nsites, dt
    hcurves = {}  # key -> poes
    trt_smr = proxies[0]['trt_smr']
    fmon = monitor('filtering ruptures', measuremem=False)
    cmon = monitor('computing gmfs', measuremem=False)
    with dstore:
        trt = full_lt.trts[trt_smr // len(full_lt.sm_rlzs)]
        sitecol = dstore['sitecol']
        extra = sitecol.array.dtype.names
        srcfilter = SourceFilter(
            sitecol, oqparam.maximum_distance(trt))
        rupgeoms = dstore['rupgeoms']
        rlzs_by_gsim = full_lt._rlzs_by_gsim(trt_smr)
        cmaker = ContextMaker(trt, rlzs_by_gsim, oqparam, extraparams=extra)
        cmaker.min_mag = getdefault(oqparam.minimum_magnitude, trt)
        for proxy in proxies:
            t0 = time.time()
            with fmon:
                if proxy['mag'] < cmaker.min_mag:
                    continue
                sids = srcfilter.close_sids(proxy, trt)
                if len(sids) == 0:  # filtered away
                    continue
                proxy.geom = rupgeoms[proxy['geom_id']]
                ebr = proxy.to_ebr(cmaker.trt)  # after the geometry is set
                try:
                    computer = GmfComputer(
                        ebr, srcfilter.sitecol.filtered(sids), cmaker,
                        oqparam.correl_model, oqparam.cross_correl,
                        oqparam._amplifier, oqparam._sec_perils)
                except FarAwayRupture:
                    continue
            with cmon:
                data = computer.compute_all(sig_eps)
            dt = time.time() - t0
            times.append(
                (computer.ebrupture.id, len(computer.ctx.sids), dt))
            for key in data:
                alldata[key].extend(data[key])
    for key, val in sorted(alldata.items()):
        if key in 'eid sid rlz':
            alldata[key] = U32(alldata[key])
        else:
            alldata[key] = F32(alldata[key])
    gmfdata = strip_zeros(pandas.DataFrame(alldata))
    if len(gmfdata) and oqparam.hazard_curves_from_gmfs:
        hc_mon = monitor('building hazard curves', measuremem=False)
        for (sid, rlz), df in gmfdata.groupby(['sid', 'rlz']):
            with hc_mon:
                poes = calc.gmvs_to_poes(
                    df, oqparam.imtls, oqparam.ses_per_logic_tree_path)
                for m, imt in enumerate(oqparam.imtls):
                    hcurves[rsi2str(rlz, sid, imt)] = poes[m]
    times = numpy.array([tup + (monitor.task_no,) for tup in times], time_dt)
    times.sort(order='rup_id')
    if not oqparam.ground_motion_fields:
        gmfdata = ()
    return dict(gmfdata=gmfdata, hcurves=hcurves, times=times,
                sig_eps=numpy.array(sig_eps, sig_eps_dt(oqparam.imtls)))


def compute_avg_gmf(gmf_df, weights, min_iml):
    """
    :param gmf_df: a DataFrame with colums eid, sid, rlz, gmv...
    :param weights: E weights associated to the realizations
    :param min_iml: array of M minimum intensities
    :returns: a dictionary site_id -> array of shape (2, M)
    """
    dic = {}
    E = len(weights)
    M = len(min_iml)
    for sid, df in gmf_df.groupby(gmf_df.index):
        eid = df.pop('eid')
        if len(df) < E:
            gmvs = numpy.ones((E, M), F32) * min_iml
            gmvs[eid.to_numpy()] = df.to_numpy()
        else:
            gmvs = df.to_numpy()
        dic[sid] = geom_avg_std(gmvs, weights)
    return dic


@base.calculators.add('event_based', 'scenario', 'ucerf_hazard')
class EventBasedCalculator(base.HazardCalculator):
    """
    Event based PSHA calculator generating the ground motion fields and
    the hazard curves from the ruptures, depending on the configuration
    parameters.
    """
    core_task = event_based
    is_stochastic = True
    accept_precalc = ['event_based', 'ebrisk', 'event_based_risk']

    def init(self):
        if self.oqparam.cross_correl.__class__.__name__ == 'GodaAtkinson2009':
            logging.warning(
                'The truncation_level param is ignored with GodaAtkinson2009')
        if hasattr(self, 'csm'):
            self.check_floating_spinning()
        if hasattr(self.oqparam, 'maximum_distance'):
            self.srcfilter = self.src_filter()
        else:
            self.srcfilter = nofilter
        if not self.datastore.parent:
            self.datastore.create_dset('ruptures', rupture_dt)
            self.datastore.create_dset('rupgeoms', hdf5.vfloat32)

    def acc0(self):
        """
        Initial accumulator, a dictionary rlz -> ProbabilityMap
        """
        self.L = self.oqparam.imtls.size
        return {r: ProbabilityMap(self.L) for r in range(self.R)}

    def build_events_from_sources(self):
        """
        Prefilter the composite source model and store the source_info
        """
        oq = self.oqparam
        gsims_by_trt = self.csm.full_lt.get_gsims_by_trt()
        sources = self.csm.get_sources()
        # weighting the heavy sources
        nrups = parallel.Starmap(
            count_ruptures, [(src,) for src in sources if src.code in b'AMC'],
            progress=logging.debug
        ).reduce()
        for src in sources:
            try:
                src.num_ruptures = nrups[src.source_id]
            except KeyError:
                src.num_ruptures = src.count_ruptures()
            src.weight = src.num_ruptures
        maxweight = sum(sg.weight for sg in self.csm.src_groups) / (
            self.oqparam.concurrent_tasks or 1)
        eff_ruptures = AccumDict(accum=0)  # grp_id => potential ruptures
        source_data = AccumDict(accum=[])
        allargs = []
        srcfilter = self.srcfilter
        logging.info('Building ruptures')
        for sg in self.csm.src_groups:
            if not sg.sources:
                continue
            logging.info('Sending %s', sg)
            cmaker = ContextMaker(sg.trt, gsims_by_trt[sg.trt], oq)
            for src_group in sg.split(maxweight):
                allargs.append((src_group, cmaker, srcfilter.sitecol))
        smap = parallel.Starmap(
            sample_ruptures, allargs, h5=self.datastore.hdf5)
        mon = self.monitor('saving ruptures')
        self.nruptures = 0  # estimated classical ruptures within maxdist
        for dic in smap:
            # NB: dic should be a dictionary, but when the calculation dies
            # for an OOM it can become None, thus giving a very confusing error
            if dic is None:
                raise MemoryError('You ran out of memory!')
            rup_array = dic['rup_array']
            if len(rup_array) == 0:
                continue
            if dic['source_data']:
                source_data += dic['source_data']
            if dic['eff_ruptures']:
                eff_ruptures += dic['eff_ruptures']
            with mon:
                n = len(rup_array)
                rup_array['id'] = numpy.arange(
                    self.nruptures, self.nruptures + n)
                self.nruptures += n
                hdf5.extend(self.datastore['ruptures'], rup_array)
                hdf5.extend(self.datastore['rupgeoms'], rup_array.geom)
        if len(self.datastore['ruptures']) == 0:
            raise RuntimeError('No ruptures were generated, perhaps the '
                               'investigation time is too short')

        # don't change the order of the 3 things below!
        self.store_source_info(source_data)
        self.store_rlz_info(eff_ruptures)
        imp = calc.RuptureImporter(self.datastore)
        with self.monitor('saving ruptures and events'):
            imp.import_rups_events(
                self.datastore.getitem('ruptures')[()], get_rupture_getters)

    def agg_dicts(self, acc, result):
        """
        :param acc: accumulator dictionary
        :param result: an AccumDict with events, ruptures, gmfs and hcurves
        """
        sav_mon = self.monitor('saving gmfs')
        agg_mon = self.monitor('aggregating hcurves')
        primary = self.oqparam.get_primary_imtls()
        sec_imts = self.oqparam.get_sec_imts()
        with sav_mon:
            df = result.pop('gmfdata')
            if len(df):
                dset = self.datastore['gmf_data/sid']
                times = result.pop('times')
                [task_no] = numpy.unique(times['task_no'])
                rupids = list(times['rup_id'])
                self.datastore['gmf_data/time_by_rup'][rupids] = times
                hdf5.extend(dset, df.sid.to_numpy())
                hdf5.extend(self.datastore['gmf_data/eid'], df.eid.to_numpy())
                for m in range(len(primary)):
                    hdf5.extend(self.datastore[f'gmf_data/gmv_{m}'],
                                df[f'gmv_{m}'])
                for sec_imt in sec_imts:
                    hdf5.extend(self.datastore[f'gmf_data/{sec_imt}'],
                                df[sec_imt])
                sig_eps = result.pop('sig_eps')
                hdf5.extend(self.datastore['gmf_data/sigma_epsilon'], sig_eps)
                self.offset += len(df)
        if self.offset >= TWO32:
            raise RuntimeError(
                'The gmf_data table has more than %d rows' % TWO32)
        imtls = self.oqparam.imtls
        with agg_mon:
            for key, poes in result.get('hcurves', {}).items():
                r, sid, imt = str2rsi(key)
                array = acc[r].setdefault(sid, 0).array[imtls(imt), 0]
                array[:] = 1. - (1. - array) * (1. - poes)
        self.datastore.flush()
        return acc

    def _read_scenario_ruptures(self):
        oq = self.oqparam
        gsim_lt = readinput.get_gsim_lt(self.oqparam)
        G = gsim_lt.get_num_paths()
        if oq.calculation_mode.startswith('scenario'):
            ngmfs = oq.number_of_ground_motion_fields
        if oq.inputs['rupture_model'].endswith('.xml'):
            # check the number of branchsets
            bsets = len(gsim_lt._ltnode)
            if bsets > 1:
                raise InvalidFile(
                    '%s for a scenario calculation must contain a single '
                    'branchset, found %d!' % (oq.inputs['job_ini'], bsets))
            [(trt, rlzs_by_gsim)] = gsim_lt.get_rlzs_by_gsim_trt().items()
            rup = readinput.get_rupture(oq)
            oq.mags_by_trt = {trt: ['%.2f' % rup.mag]}
            self.cmaker = ContextMaker(trt, rlzs_by_gsim, oq)
            if self.N > oq.max_sites_disagg:  # many sites, split rupture
                ebrs = [EBRupture(copyobj(rup, rup_id=rup.rup_id + i),
                                  'NA', 0, G, e0=i * G, scenario=True)
                        for i in range(ngmfs)]
            else:  # keep a single rupture with a big occupation number
                ebrs = [EBRupture(rup, 'NA', 0, G * ngmfs, rup.rup_id,
                                  scenario=True)]
            srcfilter = SourceFilter(self.sitecol, oq.maximum_distance(trt))
            aw = get_rup_array(ebrs, srcfilter)
            if len(aw) == 0:
                raise RuntimeError(
                    'The rupture is too far from the sites! Please check the '
                    'maximum_distance and the position of the rupture')
        elif oq.inputs['rupture_model'].endswith('.csv'):
            aw = get_ruptures(oq.inputs['rupture_model'])
            if len(gsim_lt.values) == 1:  # fix for scenario_damage/case_12
                aw['trt_smr'] = 0  # a single TRT
            if oq.calculation_mode.startswith('scenario'):
                # rescale n_occ by ngmfs and nrlzs
                aw['n_occ'] *= ngmfs * gsim_lt.get_num_paths()
        else:
            raise InvalidFile("Something wrong in %s" % oq.inputs['job_ini'])
        rup_array = aw.array
        hdf5.extend(self.datastore['rupgeoms'], aw.geom)

        if len(rup_array) == 0:
            raise RuntimeError(
                'There are no sites within the maximum_distance'
                ' of %s km from the rupture' % oq.maximum_distance(
                    rup.tectonic_region_type)(rup.mag))

        fake = logictree.FullLogicTree.fake(gsim_lt)
        self.realizations = fake.get_realizations()
        self.datastore['full_lt'] = fake
        self.store_rlz_info({})  # store weights
        self.save_params()
        imp = calc.RuptureImporter(self.datastore)
        imp.import_rups_events(rup_array, get_rupture_getters)

    def execute(self):
        oq = self.oqparam
        dstore = self.datastore
        if oq.ground_motion_fields and oq.min_iml.sum() == 0:
            logging.warning('The GMFs are not filtered: '
                            'you may want to set a minimum_intensity')
        elif oq.minimum_intensity:
            logging.info('minimum_intensity=%s', oq.minimum_intensity)
        else:
            logging.info('min_iml=%s', oq.min_iml)
        self.offset = 0
        if oq.hazard_calculation_id:  # from ruptures
            dstore.parent = datastore.read(oq.hazard_calculation_id)
        elif hasattr(self, 'csm'):  # from sources
            self.build_events_from_sources()
            if (oq.ground_motion_fields is False and
                    oq.hazard_curves_from_gmfs is False):
                return {}
        elif 'rupture_model' not in oq.inputs:
            logging.warning(
                'There is no rupture_model, the calculator will just '
                'import data without performing any calculation')
            fake = logictree.FullLogicTree.fake()
            dstore['full_lt'] = fake  # needed to expose the outputs
            dstore['weights'] = [1.]
            return {}
        else:  # scenario
            self._read_scenario_ruptures()
            if (oq.ground_motion_fields is False and
                    oq.hazard_curves_from_gmfs is False):
                return {}

        if oq.ground_motion_fields:
            imts = oq.get_primary_imtls()
            nrups = len(dstore['ruptures'])
            base.create_gmf_data(dstore, imts, oq.get_sec_imts())
            dstore.create_dset('gmf_data/sigma_epsilon', sig_eps_dt(oq.imtls))
            dstore.create_dset('gmf_data/time_by_rup',
                               time_dt, (nrups,), fillvalue=None)

        # event_based in parallel
        nr = len(dstore['ruptures'])
        logging.info('Reading {:_d} ruptures'.format(nr))
        scenario = 'scenario' in oq.calculation_mode
        proxies = [RuptureProxy(rec, scenario)
                   for rec in dstore['ruptures'][:]]
        full_lt = self.datastore['full_lt']
        dstore.swmr_on()  # must come before the Starmap
        smap = parallel.Starmap.apply_split(
            self.core_task.__func__, (proxies, full_lt, oq, self.datastore),
            key=operator.itemgetter('trt_smr'),
            weight=operator.itemgetter('n_occ'),
            h5=dstore.hdf5,
            concurrent_tasks=oq.concurrent_tasks or 1,
            duration=oq.time_per_task,
            outs_per_task=oq.outs_per_task)
        acc = smap.reduce(self.agg_dicts, self.acc0())
        if 'gmf_data' not in dstore:
            return acc
        if oq.ground_motion_fields:
            with self.monitor('saving avg_gmf', measuremem=True):
                self.save_avg_gmf()
        return acc

    def save_avg_gmf(self):
        """
        Compute and save avg_gmf, unless there are too many GMFs
        """
        size = self.datastore.getsize('gmf_data')
        maxsize = self.oqparam.gmf_max_gb * 1024 ** 3
        logging.info(f'Stored {humansize(size)} of GMFs')
        if size > maxsize:
            logging.warning(
                f'There are more than {humansize(maxsize)} of GMFs,'
                ' not computing avg_gmf')
            return numpy.unique(self.datastore['gmf_data/eid'][:])

        rlzs = self.datastore['events']['rlz_id']
        self.weights = self.datastore['weights'][:][rlzs]
        gmf_df = self.datastore.read_df('gmf_data', 'sid')
        for sec_imt in self.oqparam.get_sec_imts():  # ignore secondary perils
            del gmf_df[sec_imt]
        rel_events = gmf_df.eid.unique()
        e = len(rel_events)
        if e == 0:
            raise RuntimeError(
                'No GMFs were generated, perhaps they were '
                'all below the minimum_intensity threshold')
        elif e < len(self.datastore['events']):
            self.datastore['relevant_events'] = rel_events
            logging.info('Stored {:_d} relevant event IDs'.format(e))

        # really compute and store the avg_gmf
        M = len(self.oqparam.min_iml)
        avg_gmf = numpy.zeros((2, self.N, M), F32)
        for sid, avgstd in compute_avg_gmf(
                gmf_df, self.weights, self.oqparam.min_iml).items():
            avg_gmf[:, sid] = avgstd
        self.datastore['avg_gmf'] = avg_gmf
        return rel_events

    def post_execute(self, result):
        oq = self.oqparam
        if (not result or not oq.ground_motion_fields and not
                oq.hazard_curves_from_gmfs):
            return
        N = len(self.sitecol.complete)
        M = len(oq.imtls)  # 0 in scenario
        L = oq.imtls.size
        L1 = L // (M or 1)
        # check seed dependency unless the number of GMFs is huge
        if 'gmf_data' in self.datastore and self.datastore.getsize(
                'gmf_data/gmv_0') < 4E9:
            logging.info('Checking stored GMFs')
            msg = views.view('extreme_gmvs', self.datastore)
            logging.warning(msg)
        if oq.hazard_curves_from_gmfs:
            rlzs = self.datastore['full_lt'].get_realizations()
            # compute and save statistics; this is done in process and can
            # be very slow if there are thousands of realizations
            weights = [rlz.weight for rlz in rlzs]
            # NB: in the future we may want to save to individual hazard
            # curves if oq.individual_rlzs is set; for the moment we
            # save the statistical curves only
            hstats = oq.hazard_stats()
            S = len(hstats)
            pmaps = list(result.values())
            R = len(weights)
            if len(pmaps) != R:
                # this should never happen, unless I break the
                # logic tree reduction mechanism during refactoring
                raise AssertionError('Expected %d pmaps, got %d' %
                                     (len(weights), len(pmaps)))
            if oq.individual_rlzs:
                logging.info('Saving individual hazard curves')
                self.datastore.create_dset('hcurves-rlzs', F32, (N, R, M, L1))
                self.datastore.set_shape_descr(
                    'hcurves-rlzs', site_id=N, rlz_id=R,
                    imt=list(oq.imtls), lvl=numpy.arange(L1))
                if oq.poes:
                    P = len(oq.poes)
                    M = len(oq.imtls)
                    ds = self.datastore.create_dset(
                        'hmaps-rlzs', F32, (N, R, M, P))
                    self.datastore.set_shape_descr(
                        'hmaps-rlzs', site_id=N, rlz_id=R,
                        imt=list(oq.imtls), poe=oq.poes)
                for r, pmap in enumerate(pmaps):
                    arr = numpy.zeros((N, M, L1), F32)
                    for sid in pmap:
                        arr[sid] = pmap[sid].array.reshape(M, L1)
                    self.datastore['hcurves-rlzs'][:, r] = arr
                    if oq.poes:
                        hmap = calc.make_hmap(pmap, oq.imtls, oq.poes)
                        for sid in hmap:
                            ds[sid, r] = hmap[sid].array

            if S:
                logging.info('Computing statistical hazard curves')
                self.datastore.create_dset('hcurves-stats', F32, (N, S, M, L1))
                self.datastore.set_shape_descr(
                    'hcurves-stats', site_id=N, stat=list(hstats),
                    imt=list(oq.imtls), lvl=numpy.arange(L1))
                if oq.poes:
                    P = len(oq.poes)
                    M = len(oq.imtls)
                    ds = self.datastore.create_dset(
                        'hmaps-stats', F32, (N, S, M, P))
                    self.datastore.set_shape_descr(
                        'hmaps-stats', site_id=N, stat=list(hstats),
                        imt=list(oq.imtls), poes=oq.poes)
                for s, stat in enumerate(hstats):
                    pmap = compute_pmap_stats(
                        pmaps, [hstats[stat]], weights, oq.imtls)
                    arr = numpy.zeros((N, M, L1), F32)
                    for sid in pmap:
                        arr[sid] = pmap[sid].array.reshape(M, L1)
                    self.datastore['hcurves-stats'][:, s] = arr
                    if oq.poes:
                        hmap = calc.make_hmap(pmap, oq.imtls, oq.poes)
                        for sid in hmap:
                            ds[sid, s] = hmap[sid].array
        if self.datastore.parent:
            self.datastore.parent.open('r')
        if oq.compare_with_classical:  # compute classical curves
            export_dir = os.path.join(oq.export_dir, 'cl')
            if not os.path.exists(export_dir):
                os.makedirs(export_dir)
            oq.export_dir = export_dir
            oq.calculation_mode = 'classical'
            with logs.init('job', vars(oq)) as log:
                self.cl = ClassicalCalculator(oq, log.calc_id)
                # TODO: perhaps it is possible to avoid reprocessing the source
                # model, however usually this is quite fast and do not dominate
                # the computation
                self.cl.run()
                engine.expose_outputs(self.cl.datastore)
                for imt in oq.imtls:
                    cl_mean_curves = get_mean_curves(self.datastore, imt)
                    eb_mean_curves = get_mean_curves(self.datastore, imt)
                    self.rdiff, index = util.max_rel_diff_index(
                        cl_mean_curves, eb_mean_curves)
                    logging.warning(
                        'Relative difference with the classical '
                        'mean curves: %d%% at site index %d, imt=%s',
                        self.rdiff * 100, index, imt)
