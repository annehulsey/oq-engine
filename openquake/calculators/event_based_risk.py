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

import os.path
import logging
import operator
import itertools
from functools import partial
import numpy
import pandas
from scipy import sparse

from openquake.baselib import hdf5, parallel, general
from openquake.hazardlib import stats, InvalidFile
from openquake.hazardlib.source.rupture import RuptureProxy
from openquake.risklib.scientific import (
    total_losses, insurance_losses, MultiEventRNG, LTI)
from openquake.calculators import base, event_based
from openquake.calculators.post_risk import (
    PostRiskCalculator, post_aggregate, fix_dtypes)

U8 = numpy.uint8
U16 = numpy.uint16
U32 = numpy.uint32
U64 = numpy.uint64
F32 = numpy.float32
F64 = numpy.float64
TWO16 = 2 ** 16
TWO32 = U64(2 ** 32)
get_n_occ = operator.itemgetter(1)


def fast_agg(keys, values, correl, li, acc):
    """
    :param keys: an array of N uint64 numbers encoding (event_id, agg_id)
    :param values: an array of (N, D) floats
    :param correl: True if there is asset correlation
    :param li: loss type index
    :param acc: dictionary unique key -> array(L, D)
    """
    ukeys, avalues = general.fast_agg2(keys, values)
    if correl:  # restore the variances
        avalues[:, 0] = avalues[:, 0] ** 2
    for ukey, avalue in zip(ukeys, avalues):
        acc[ukey][li] += avalue


def average_losses(ln, alt, rlz_id, AR, collect_rlzs):
    """
    :returns: a sparse coo matrix with the losses per asset and realization
    """
    if collect_rlzs or len(numpy.unique(rlz_id)) == 1:
        ldf = pandas.DataFrame(
            dict(aid=alt.aid.to_numpy(), loss=alt.loss.to_numpy()))
        tot = ldf.groupby('aid').loss.sum()
        aids = tot.index.to_numpy()
        rlzs = numpy.zeros_like(tot)
        return sparse.coo_matrix((tot.to_numpy(), (aids, rlzs)), AR)
    else:
        ldf = pandas.DataFrame(
            dict(aid=alt.aid.to_numpy(), loss=alt.loss.to_numpy(),
                 rlz=rlz_id[U32(alt.eid)]))  # NB: without the U32 here
        # the SURA calculation would fail with alt.eid being F64 (?)
        tot = ldf.groupby(['aid', 'rlz']).loss.sum()
        aids, rlzs = zip(*tot.index)
        return sparse.coo_matrix((tot.to_numpy(), (aids, rlzs)), AR)


def aggreg(outputs, crmodel, ARK, aggids, rlz_id, monitor):
    """
    :returns: (avg_losses, agg_loss_table)
    """
    mon_agg = monitor('aggregating losses', measuremem=False)
    mon_avg = monitor('averaging losses', measuremem=False)
    mon_df = monitor('building dataframe', measuremem=True)
    oq = crmodel.oqparam
    xtypes = oq.ext_loss_types
    loss_by_AR = {ln: [] for ln in xtypes}
    correl = int(oq.asset_correlation)
    (A, R, K), L = ARK, len(xtypes)
    acc = general.AccumDict(accum=numpy.zeros((L, 2)))  # u8idx->array
    value_cols = ['variance', 'loss']
    for out in outputs:
        for li, ln in enumerate(oq.ext_loss_types):
            if ln not in out or len(out[ln]) == 0:
                continue
            alt = out[ln]
            if oq.avg_losses:
                with mon_avg:
                    coo = average_losses(
                        ln, alt, rlz_id, (A, R), oq.collect_rlzs)
                    loss_by_AR[ln].append(coo)
            with mon_agg:
                if correl:  # use sigma^2 = (sum sigma_i)^2
                    alt['variance'] = numpy.sqrt(alt.variance)
                eids = alt.eid.to_numpy() * TWO32  # U64
                values = numpy.array([alt[col] for col in value_cols]).T
                # aggregate all assets
                fast_agg(eids + U64(K), values, correl, li, acc)
                if len(aggids):
                    # aggregate assets for each tag combination
                    aids = alt.aid.to_numpy()
                    for kids in aggids[:, aids]:
                        fast_agg(eids + U64(kids), values, correl, li, acc)
    with mon_df:
        dic = general.AccumDict(accum=[])
        for ukey, arr in acc.items():
            eid, kid = divmod(ukey, TWO32)
            for li in range(L):
                if arr[li].any():
                    dic['event_id'].append(eid)
                    dic['agg_id'].append(kid)
                    dic['loss_id'].append(LTI[xtypes[li]])
                    for c, col in enumerate(value_cols):
                        dic[col].append(arr[li, c])
        fix_dtypes(dic)
        df = pandas.DataFrame(dic)
    return dict(avg=loss_by_AR, alt=df)


def event_based_risk(df, oqparam, dstore, monitor):
    """
    :param df: a DataFrame of GMFs with fields sid, eid, gmv_X, ...
    :param oqparam: parameters coming from the job.ini
    :param dstore: a DataStore instance
    :param monitor: a Monitor instance
    :returns: a dictionary of arrays
    """
    if dstore.parent:
        dstore.parent.open('r')
    with dstore, monitor('reading data'):
        if hasattr(df, 'start'):  # it is actually a slice
            df = dstore.read_df('gmf_data', slc=df)
        assetcol = dstore['assetcol']
        if oqparam.K:
            aggids, _ = assetcol.build_aggids(oqparam.aggregate_by)
        else:
            aggids = ()
        crmodel = monitor.read('crmodel')
        rlz_id = monitor.read('rlz_id')
        weights = [1] if oqparam.collect_rlzs else dstore['weights'][()]
    if dstore.parent:
        dstore.parent.close()  # essential on Windows with h5py>=3.6
    ARK = len(assetcol), len(weights), oqparam.K
    if oqparam.ignore_master_seed or oqparam.ignore_covs:
        rng = None
    else:
        rng = MultiEventRNG(oqparam.master_seed, df.eid.unique(),
                            int(oqparam.asset_correlation))

    def outputs():
        mon_risk = monitor('computing risk', measuremem=False)
        for taxo, adf in assetcol.to_dframe().groupby('taxonomy'):
            gmf_df = df[numpy.isin(df.sid.to_numpy(), adf.site_id.to_numpy())]
            if len(gmf_df) == 0:
                continue
            with mon_risk:
                adf = adf.set_index('ordinal')
                out = crmodel.get_output(
                    taxo, adf, gmf_df, oqparam._sec_losses, rng)
            yield out

    return aggreg(outputs(), crmodel, ARK, aggids, rlz_id, monitor)


def ebrisk(proxies, full_lt, oqparam, dstore, monitor):
    """
    :param proxies: list of RuptureProxies with the same trt_smr
    :param full_lt: a FullLogicTree instance
    :param oqparam: input parameters
    :param monitor: a Monitor instance
    :returns: a dictionary of arrays
    """
    oqparam.ground_motion_fields = True
    dic = event_based.event_based(proxies, full_lt, oqparam, dstore, monitor)
    if len(dic['gmfdata']) == 0:  # no GMFs
        return {}
    return event_based_risk(dic['gmfdata'], oqparam, dstore, monitor)


@base.calculators.add('ebrisk', 'scenario_risk', 'event_based_risk')
class EventBasedRiskCalculator(event_based.EventBasedCalculator):
    """
    Event based risk calculator generating event loss tables
    """
    core_task = ebrisk
    is_stochastic = True
    precalc = 'event_based'
    accept_precalc = ['scenario', 'event_based', 'event_based_risk', 'ebrisk']

    def pre_execute(self):
        oq = self.oqparam
        if oq.calculation_mode == 'ebrisk':
            oq.ground_motion_fields = False
            logging.warning('You should be using the event_based_risk '
                            'calculator, not ebrisk!')
        parent = self.datastore.parent
        if parent:
            self.datastore['full_lt'] = parent['full_lt']
            self.parent_events = ne = len(parent['events'])
            logging.info('There are %d ruptures and %d events',
                         len(parent['ruptures']), ne)
        else:
            self.parent_events = None

        if oq.investigation_time and oq.return_periods != [0]:
            # setting return_periods = 0 disable loss curves
            eff_time = oq.investigation_time * oq.ses_per_logic_tree_path
            if eff_time < 2:
                logging.warning(
                    'eff_time=%s is too small to compute loss curves',
                    eff_time)
        super().pre_execute()
        parentdir = (os.path.dirname(self.datastore.ppath)
                     if self.datastore.ppath else None)
        oq.hdf5path = self.datastore.filename
        oq.parentdir = parentdir
        logging.info(
            'There are {:_d} ruptures'.format(len(self.datastore['ruptures'])))
        self.events_per_sid = numpy.zeros(self.N, U32)
        try:
            K = len(self.datastore['agg_keys'])
        except KeyError:
            K = 0
        self.datastore.swmr_on()
        sec_losses = []  # one insured loss for each loss type with a policy
        if hasattr(self, 'policy_df'):
            sec_losses.append(
                partial(insurance_losses, policy_df=self.policy_df))
        if oq.total_losses:
            sec_losses.append(partial(total_losses, kind=oq.total_losses))
        oq._sec_losses = sec_losses
        oq.M = len(oq.all_imts())
        oq.N = self.N
        oq.K = K
        ct = oq.concurrent_tasks or 1
        oq.maxweight = int(oq.ebrisk_maxsize / ct)
        self.A = A = len(self.assetcol)
        self.L = L = len(oq.loss_types)
        if (oq.aggregate_by and self.E * A > oq.max_potential_gmfs and
                all(val == 0 for val in oq.minimum_asset_loss.values())):
            logging.warning('The calculation is really big; consider setting '
                            'minimum_asset_loss')
        base.create_risk_by_event(self)
        self.rlzs = self.datastore['events']['rlz_id']
        self.num_events = numpy.bincount(self.rlzs, minlength=self.R)
        if oq.avg_losses:
            self.create_avg_losses()
        alt_nbytes = 4 * self.E * L
        if alt_nbytes / (oq.concurrent_tasks or 1) > TWO32:
            raise RuntimeError('The risk_by_event is too big to be transfer'
                               'ed with %d tasks' % oq.concurrent_tasks)

    def create_avg_losses(self):
        oq = self.oqparam
        ws = self.datastore['weights']
        R = 1 if oq.collect_rlzs else len(ws)
        if oq.collect_rlzs:
            if oq.investigation_time:  # event_based
                self.avg_ratio = numpy.array([oq.time_ratio / len(ws)])
            else:  # scenario
                self.avg_ratio = numpy.array([1. / self.num_events.sum()])
        else:
            if oq.investigation_time:  # event_based
                self.avg_ratio = numpy.array([oq.time_ratio] * len(ws))
            else:  # scenario
                self.avg_ratio = 1. / self.num_events
        self.avg_losses = {}
        for lt in oq.ext_loss_types:
            self.avg_losses[lt] = numpy.zeros((self.A, R), F32)
            self.datastore.create_dset(
                'avg_losses-rlzs/' + lt, F32, (self.A, R))
            self.datastore.set_shape_descr(
                'avg_losses-rlzs/' + lt, asset_id=self.assetcol['id'], rlz=R)

    def execute(self):
        """
        Compute risk from GMFs or ruptures depending on what is stored
        """
        oq = self.oqparam
        self.gmf_bytes = 0
        if 'gmf_data' not in self.datastore:  # start from ruptures
            if (oq.ground_motion_fields and
                    'gsim_logic_tree' not in oq.inputs and
                    oq.gsim == '[FromFile]'):
                raise InvalidFile('Missing gsim or gsim_logic_tree_file in %s'
                                  % oq.inputs['job_ini'])
            elif not hasattr(oq, 'maximum_distance'):
                raise InvalidFile('Missing maximum_distance in %s'
                                  % oq.inputs['job_ini'])
            srcfilter = self.src_filter()
            scenario = 'scenario' in oq.calculation_mode
            proxies = [RuptureProxy(rec, scenario)
                       for rec in self.datastore['ruptures'][:]]
            full_lt = self.datastore['full_lt']
            self.datastore.swmr_on()  # must come before the Starmap
            smap = parallel.Starmap.apply_split(
                ebrisk, (proxies, full_lt, oq, self.datastore),
                key=operator.itemgetter('trt_smr'),
                weight=operator.itemgetter('n_occ'),
                h5=self.datastore.hdf5,
                duration=oq.time_per_task,
                outs_per_task=5)
            smap.monitor.save('srcfilter', srcfilter)
            smap.monitor.save('crmodel', self.crmodel)
            smap.monitor.save('rlz_id', self.rlzs)
            smap.reduce(self.agg_dicts)
            if self.gmf_bytes == 0:
                raise RuntimeError(
                    'No GMFs were generated, perhaps they were '
                    'all below the minimum_intensity threshold')
            logging.info(
                'Produced %s of GMFs', general.humansize(self.gmf_bytes))
        else:  # start from GMFs
            eids = self.datastore['gmf_data/eid'][:]
            self.log_info(eids)
            self.datastore.swmr_on()  # crucial!
            smap = parallel.Starmap(
                event_based_risk, self.gen_args(eids), h5=self.datastore.hdf5)
            smap.monitor.save('assets', self.assetcol.to_dframe('id'))
            smap.monitor.save('crmodel', self.crmodel)
            smap.monitor.save('rlz_id', self.rlzs)
            smap.reduce(self.agg_dicts)
        if self.parent_events:
            assert self.parent_events == len(self.datastore['events'])
        return 1

    def log_info(self, eids):
        """
        Printing some information about the risk calculation
        """
        logging.info('Processing {:_d} rows of gmf_data'.format(len(eids)))
        E = len(numpy.unique(eids))
        K = self.oqparam.K
        logging.info('Risk parameters (rel_E={:_d}, K={:_d}, L={})'.
                     format(E, K, self.L))

    def agg_dicts(self, dummy, dic):
        """
        :param dummy: unused parameter
        :param dic: dictionary with keys "avg", "alt"
        """
        if not dic:
            return
        self.gmf_bytes += dic['alt'].memory_usage().sum()
        self.oqparam.ground_motion_fields = False  # hack
        with self.monitor('saving risk_by_event'):
            alt = dic['alt']
            if alt is not None:
                for name in alt.columns:
                    dset = self.datastore['risk_by_event/' + name]
                    hdf5.extend(dset, alt[name].to_numpy())
            for ln, ls in dic['avg'].items():
                for coo in ls:
                    self.avg_losses[ln][coo.row, coo.col] += coo.data

    def post_execute(self, dummy):
        """
        Compute and store average losses from the risk_by_event dataset,
        and then loss curves and maps.
        """
        oq = self.oqparam

        # sanity check on the risk_by_event
        alt = self.datastore.read_df('risk_by_event')
        K = self.datastore['risk_by_event'].attrs.get('K', 0)
        upper_limit = self.E * (K + 1) * len(oq.ext_loss_types)
        size = len(alt)
        assert size <= upper_limit, (size, upper_limit)
        # sanity check on uniqueness by (agg_id, loss_id, event_id)
        arr = alt[['agg_id', 'loss_id', 'event_id']].to_numpy()
        uni = numpy.unique(arr, axis=0)
        if len(uni) < len(arr):
            raise RuntimeError('risk_by_event contains %d duplicates!' %
                               (len(arr) - len(uni)))
        if oq.avg_losses:
            for lt in oq.ext_loss_types:
                al = self.avg_losses[lt]
                for r in range(self.R):
                    al[:, r] *= self.avg_ratio[r]
                name = 'avg_losses-rlzs/' + lt
                self.datastore[name][:] = al
                stats.set_rlzs_stats(self.datastore, name,
                                     asset_id=self.assetcol['id'])

        self.build_aggcurves()
        if oq.reaggregate_by:
            post_aggregate(self.datastore.calc_id,
                           ','.join(oq.reaggregate_by))

    def build_aggcurves(self):
        prc = PostRiskCalculator(self.oqparam, self.datastore.calc_id)
        prc.assetcol = self.assetcol
        if hasattr(self, 'exported'):
            prc.exported = self.exported
        with prc.datastore:
            prc.run(exports='')

    def gen_args(self, eids):
        """
        :yields: pairs (gmf_slice, param)
        """
        ct = self.oqparam.concurrent_tasks or 1
        maxweight = len(eids) / ct
        start = stop = weight = 0
        # IMPORTANT!! we rely on the fact that the hazard part
        # of the calculation stores the GMFs in chunks of constant eid
        for eid, group in itertools.groupby(eids):
            nsites = sum(1 for _ in group)
            stop += nsites
            weight += nsites
            if weight > maxweight:
                yield slice(start, stop), self.oqparam, self.datastore
                weight = 0
                start = stop
        if weight:
            yield slice(start, stop), self.oqparam, self.datastore
