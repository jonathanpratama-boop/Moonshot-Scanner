import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'candidate'))
import radar_ops as O
CFG=json.loads((ROOT/'fixtures/config.json').read_text())
CASES=json.loads((ROOT/'fixtures/cases.json').read_text())
T='2026-10-06T18:00:00Z'
T1='2026-10-06T19:00:00Z'
T2='2026-10-06T20:00:00Z'

def clock(t):
    return dict(value=t,verified=True,evidence='SYNTHETIC signed-event fixture',precision='second',semantics='exact_event',timezone='UTC',publisher='SYNTHETIC')

class RadarTests(unittest.TestCase):
    def setUp(self):
        self.r=O.Radar(CFG)
    def add(self,key='SYNTH',**fields):
        row=copy.deepcopy(CASES[key]);row.update(fields)
        self.r.ingest(row,T)
        return row['candidate_id']
    def read(self,run,state,**fields):
        n=int(run[1:])
        row=dict(source_id='primary',run_id=run,state=state,at=CFG['authorized_runs'][run],receipt='SYNTHETIC-READ-'+run,
                 messages=[],watermark_before='w0',watermark_after='w0',history_complete=True)
        row.update(fields)
        return row
    def test_01_hlb_late_claim_never_invents_time_or_cause(self):
        cid=self.add('HLB');c=self.r.candidate(cid)
        self.assertEqual(c['late_status'],'CAPTURED_LATE_CLAIM')
        self.assertIsNone(c['clocks']['published_at']['value'])
        self.assertIsNone(c['clocks']['radar_first_discovered_at']['value'])
        self.assertEqual(c['late_cause'],'UNKNOWN')
        self.assertIn('exact delay and cause remain unknown',self.r.summary(cid))
        self.assertEqual(self.r.measurements()['discovery_delays'],dict(seconds=[],sample_size=0,unknown_count=1))
    def test_01_attribution_requires_trace_not_speculation(self):
        cid=self.add('HLB')
        with self.assertRaises(O.Refused):self.r.attribute_delay(cid,'POLLING_LATENCY',{'explanation':'seems slow'})
        self.assertIsNone(self.r.measurements()['recall'])
    def test_02_near_rerun_and_state_transition_keep_dependency(self):
        cid=self.add('NEAR');before=self.r.candidate(cid)
        result=self.r.ingest(CASES['NEAR'],T)
        self.assertEqual(result['status'],'EXISTING_CANDIDATE')
        self.assertEqual(len(self.r.snapshot()['candidates']),1)
        self.r.revise(cid,{'state':'RESEARCHING'},T,'inspect detector claim',['proposal'])
        self.r.revise(cid,{'state':'WATCHING','detector_result':{'captured_label':'COMPRESSION','raw_output_unavailable':True}},T,'different described state',['proposal described transition; raw timing unavailable'])
        after=self.r.candidate(cid)
        self.assertEqual(before['dependency_group_id'],after['dependency_group_id'])
        self.assertEqual(before['original_discovered_at'],after['original_discovered_at'])
        self.assertIsNone(after['explicit_forecast']);self.assertFalse(after['forecast_admitted'])
        self.assertEqual(self.r.snapshot()['alert_previews'],[])
        self.assertEqual(self.r.snapshot()['events'][1]['body']['detector_result']['captured_label'],'RS_SHOCK')
    def test_03_apxc_venues_currency_and_unsupported_targets(self):
        cid=self.add('APXC');ca,us=CFG['universe']['APXC']
        with self.assertRaises(O.Refused):self.r.select_series(cid,ca)
        self.r.revise(cid,{'identity':ca},T,'synthetic resolution only',['fixture'])
        self.assertEqual(self.r.select_series(cid,ca)['status'],'IDENTITY_MATCH')
        with self.assertRaises(O.Refused):self.r.select_series(cid,us)
        self.assertEqual(self.r.candidate(cid)['unsupported_claims'],['+30%','+50%','+100%'])
        self.assertIsNone(self.r.candidate(cid)['explicit_forecast'])
    def test_04_techd_expiry_not_forecast_or_calendar(self):
        cid=self.add('TECHD');c=self.r.candidate(cid)
        self.assertEqual(c['expiry_at'],T2)
        self.assertIsNone(c['original_research_expiry']['value'])
        self.assertIsNone(c['explicit_forecast'])
        with self.assertRaises(O.Refused):self.r.session_endpoint(cid,{'verified':False,'close':T2},'close')
        self.assertEqual(c['fixture_provenance']['historical_cutoff'],None)
    def test_05_platform_time_cannot_supply_authorship(self):
        cid=self.add(platform_display_time=T)
        c=self.r.candidate(cid)
        self.assertIsNone(c['clocks']['alert_authored_at']['value'])
        self.assertIsNone(c['clocks']['published_at']['value'])
        with self.assertRaises(O.Refused):self.r.forecast(cid,dict(explicit=True,original_wording='fixture',event='price event',price_basis='fixture',author='fixture',source_evidence='fixture',deadline=T2,authored_at=O.unknown('platform time only')),T)
        self.assertFalse(c['forecast_admitted'])
    def test_05_receipts_append_and_cannot_be_edited(self):
        cid=self.add();orig=self.r.snapshot()['events'][1]
        h='a'*64
        self.r.add_receipt(cid,'receipt-1','collector_received_at',clock(T1),h)
        self.assertIsNone(self.r.candidate(cid)['clocks']['collector_received_at']['value'])
        self.assertFalse(self.r.add_receipt(cid,'receipt-2','collector_received_at',clock(T2),'b'*64)['authorship_established'])
        self.assertEqual(self.r.add_receipt(cid,'receipt-1','collector_received_at',clock(T1),h)['status'],'DUPLICATE_RECEIPT')
        with self.assertRaises(O.Refused):self.r.add_receipt(cid,'receipt-1','collector_received_at',clock(T2),h)
        self.assertEqual(orig,self.r.snapshot()['events'][1])
        with self.assertRaises(O.Refused):self.r.revise(cid,{'clocks':{}},T,'overwrite',['x'])
    def test_05_delays_need_exact_comparable_clocks(self):
        a,b=clock(T),clock(T1)
        self.assertEqual(O.delay(a,b)['seconds'],3600)
        a['semantics']='available_by';self.assertIsNone(O.delay(a,b)['seconds'])
        a['semantics']='exact_event';a['precision']='minute';self.assertIsNone(O.delay(a,b)['seconds'])
        self.assertIsNone(O.delay(b,clock(T))['seconds'])
    def test_05_explicit_forecast_remains_unadmitted(self):
        cid=self.add()
        f=dict(explicit=True,original_wording='SYNTHETIC statement',event='synthetic price above reference',price_basis='fixture',author='fixture',source_evidence='fixture',deadline=T2,authored_at=clock(T))
        result=self.r.forecast(cid,f,T)
        self.assertFalse(result['cohort_admitted']);self.assertFalse(self.r.candidate(cid)['forecast_admitted'])
        with self.assertRaises(O.Refused):self.r.forecast(cid,f,T)
    def test_05_event_lead_and_content_bound_delivery(self):
        cid=self.add()
        self.assertEqual(O.event_lead_time(clock(T2),clock(T1),'collector_received_at')['seconds'],3600)
        self.assertEqual(O.event_lead_time(clock(T),clock(T1),'radar_first_discovered_at')['seconds'],-3600)
        self.assertIsNone(O.event_lead_time(O.unknown('no exact event'),clock(T),'collector_received_at')['seconds'])
        self.r.add_receipt(cid,'c','collector_received_at',clock(T),'c'*64)
        self.r.add_receipt(cid,'d','delivered_at',clock(T1),'c'*64)
        self.r.add_receipt(cid,'edit','collector_received_at',clock(T1),'d'*64)
        m=self.r.measurements()['content_bound_receipt_delivery_delays']
        self.assertEqual(m,dict(seconds=[3600],sample_size=1,unknown_count=1))
    def test_06_source_failure_no_new_partial_recovery(self):
        failed=self.read('r1','FAILED',history_complete=False)
        self.r.source_read(failed)
        self.r.source_read(self.read('r2','SUCCESS_NO_NEW'))
        self.assertFalse(self.r.snapshot()['continuity_verified']['primary'])
        self.r.source_read(self.read('r3','PARTIAL',messages=['partial item'],watermark_after='w9',history_complete=False))
        self.assertEqual(self.r.snapshot()['watermarks']['primary'],'w0')
        self.r.source_read(self.read('r4','SUCCESS_NEW',messages=['delta'],watermark_after='w10',recovery={'from_watermark':'w0','gap_read_keys':['primary:r1','primary:r3'],'evidence':['SYNTHETIC complete replay covering gap']}))
        snap=self.r.snapshot()
        self.assertTrue(snap['continuity_verified']['primary']);self.assertEqual(snap['watermarks']['primary'],'w10')
        self.assertEqual(len(snap['gap_history']['primary']),2)
        self.assertTrue(all(x['resolved'] for x in snap['gap_history']['primary']))
        self.assertEqual(self.r.measurements()['explicit_scoped_no_signal_count'],0)
        self.assertEqual([r['state'] for r in snap['reads']],['FAILED','SUCCESS_NO_NEW','PARTIAL','SUCCESS_NEW'])
    def test_06_no_signal_and_unknown_denominator(self):
        cfg=copy.deepcopy(CFG);cfg['sources']['primary']['expected_runs']=None
        r=O.Radar(cfg)
        r.source_read(self.read('r1','SUCCESS_NO_NEW'))
        self.assertIsNone(r.measurements()['verified_expected_reads'])
        self.assertIsNone(r.measurements()['successful_read_share'])
        self.assertFalse(r.measurements()['full_coverage_established'])
        self.assertEqual(r.measurements()['explicit_scoped_no_signal_count'],0)
        with self.assertRaises(O.Refused):r.source_read(self.read('r2','FAILED',no_signal_conclusion=dict(explicit=True,scope=['SYNTH'],evidence='bad')))
        r.source_read(self.read('r2','SUCCESS_NO_NEW',no_signal_conclusion=dict(explicit=True,scope=['SYNTH'],evidence='SYNTHETIC explicit scoped radar conclusion')))
        self.assertEqual(r.measurements()['explicit_scoped_no_signal_count'],1)
        with self.assertRaises(O.Refused):r.source_read(self.read('r3','EXPECTED_NOT_OBSERVED'))
    def test_06_missing_expected_run_and_registry_order(self):
        cid=self.add()
        self.r.source_read(self.read('r1','EXPECTED_NOT_OBSERVED'))
        order=self.r.work_order('r2')
        self.assertEqual([r['kind'] for r in order],['disclosure','review','tape'])
        with self.assertRaises(O.Refused):self.r.work_order('invented-run')
        self.assertGreater(self.r.measurements()['expected_not_observed_count'],0)
    def test_06_bad_recovery_cannot_erase_gap(self):
        self.r.source_read(self.read('r1','FAILED'))
        before=self.r.snapshot()
        with self.assertRaises(O.Refused):self.r.source_read(self.read('r2','SUCCESS_NEW',messages=['x'],watermark_after='w2',recovery={'from_watermark':'wrong','gap_read_keys':['primary:r1'],'evidence':['x']}))
        self.assertEqual(before,self.r.snapshot())
        with self.assertRaises(O.Refused):self.r.source_read(self.read('r1','SUCCESS_NO_NEW'))
    def test_07_expiry_close_and_linked_reopening(self):
        cid=self.add();first=self.r.candidate(cid)
        self.assertEqual(len(self.r.due(T1)),1)
        self.r.settle_due(T2)
        self.assertEqual(self.r.candidate(cid)['state'],'CLOSED')
        with self.assertRaises(O.Refused):self.r.revise(cid,dict(state='RESEARCHING',next_review_at='2026-10-06T21:00:00Z',expiry_at='2026-10-06T22:00:00Z'),T2,'',[])
        self.r.revise(cid,dict(state='RESEARCHING',next_review_at='2026-10-06T21:00:00Z',expiry_at='2026-10-06T22:00:00Z'),T2,'new evidence justifies reopening',['SYNTHETIC new evidence'])
        c=self.r.candidate(cid)
        self.assertEqual(c['parent_revision_id'],1)
        self.assertEqual(c['original_discovered_at'],first['original_discovered_at'])
        self.assertEqual(c['dependency_group_id'],first['dependency_group_id'])
        self.assertIsNone(c['explicit_forecast']);self.assertEqual(self.r.snapshot()['alert_previews'],[])
    def test_07_bounded_review_renewal_and_metrics(self):
        cid=self.add()
        with self.assertRaises(O.Refused):self.r.review(cid,dict(next_review_at=None,expiry_at=None),T1,'unbounded',['x'])
        self.r.review(cid,dict(state='RESEARCHING',next_review_at=T2,expiry_at='2026-10-06T21:00:00Z'),T1,'named unresolved evidence',['SYNTHETIC updated evidence'])
        self.assertEqual(self.r.measurements()['reviews'],dict(count=1,on_time=1,on_time_share=1))
        self.assertEqual(self.r.due(T1),[])
        self.assertEqual(self.r.snapshot()['alert_previews'],[])
    def test_07_alert_gating_dedup_revisions_stay_silent(self):
        cid=self.add();q=dict(trigger='EVIDENCE_CHANGED',independently_qualified=True,evidence=['x'],event_id='unchanged-event')
        with self.assertRaises(O.Refused):self.r.preview_alert(cid,'qualified',q)
        with self.assertRaises(O.Refused):self.r.preview_alert(cid,'invented',q)
        q['trigger']='EXISTING_QUALIFIED_ALERT'
        self.assertEqual(self.r.preview_alert(cid,'qualified',q)['status'],'QUALIFIED_PREVIEW_ONLY')
        self.r.revise(cid,{'evidence':['new material evidence']},T,'material evidence',['proof'])
        self.assertEqual(self.r.preview_alert(cid,'qualified',q)['status'],'DUPLICATE_SUPPRESSED')
        self.assertEqual(len(self.r.snapshot()['alert_previews']),1)
        self.assertFalse(self.r.snapshot()['alert_previews'][0]['delivered'])
    def test_07_overdue_and_failure_notice_rule_required(self):
        cid=self.add();due=self.r.due(T1)[0]['due_at']
        q=dict(trigger='OVERDUE',at=T1,independently_qualified=True,event_id=due,evidence=['SYNTHETIC operational rule'])
        self.assertEqual(self.r.preview_alert(cid,'notice',q)['status'],'QUALIFIED_PREVIEW_ONLY')
        self.assertEqual(self.r.preview_alert(cid,'notice',q)['status'],'DUPLICATE_SUPPRESSED')
        self.r.source_read(self.read('r1','FAILED'))
        q=dict(trigger='SOURCE_FAILURE',independently_qualified=True,event_id='primary:r1',evidence=['read trace'])
        self.assertEqual(self.r.preview_alert(cid,'failure',q)['status'],'QUALIFIED_PREVIEW_ONLY')
        self.assertEqual(self.r.preview_alert(cid,'failure',q)['status'],'DUPLICATE_SUPPRESSED')
    def test_07_invalid_transitions_scope_and_unbounded_records(self):
        cid=self.add()
        with self.assertRaises(O.Refused):self.r.revise(cid,{'state':'WATCHING'},T,'skip',['x'])
        with self.assertRaises(O.Refused):self.add('HLB',universe_key='NEW_UNIVERSE')
        with self.assertRaises(O.Refused):self.add('HLB',next_review_at=None,expiry_at=None)
        with self.assertRaises(O.Refused):self.add('HLB',owner=None)
    def test_07_review_only_deadline_closes_without_renewal(self):
        cid=self.add(expiry_at=None)
        self.r.settle_due(T1)
        self.assertEqual(self.r.candidate(cid)['state'],'CLOSED')
        self.assertEqual(self.r.due(T2),[])
    def test_08_exact_stock_crypto_parity_missing_and_wrong_contract(self):
        for key in ('APXC','NEAR'):
            cid=self.add(key,identity=CFG['universe'][key][0])
            ident=copy.deepcopy(CFG['universe'][key][0]);self.r.select_series(cid,ident)
            field='venue' if key=='APXC' else 'contract'
            ident[field]='wrong'
            with self.assertRaises(O.Refused):self.r.select_series(cid,ident)
            ident.pop(field)
            with self.assertRaises(O.Refused):self.r.select_series(cid,ident)
    def test_08_injected_detector_bytes_unchanged_not_actual_astra(self):
        fx=json.loads((ROOT/'fixtures/injected_detector_outputs.json').read_text())
        d=O.FrozenFixtureDetector(CFG['detector_config_sha256'],fx)
        for key,row in fx.items():
            raw=d.evaluate(key,{'fixture':key},CFG['detector_config_sha256'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),row['output_sha256'])
            self.assertEqual(raw,row['output_utf8'].encode())
        with self.assertRaises(O.Refused):d.evaluate('stock',{'fixture':'stock'},'wrongconfig')
        with self.assertRaises(O.Refused):d.evaluate('stock',{'fixture':'other'},CFG['detector_config_sha256'])
    def test_08_snapshot_copy_chain_and_refusal_atomicity(self):
        cid=self.add();snap=self.r.snapshot();original=self.r.candidate(cid)
        snap['candidates'][cid]['owner']='tampered'
        self.assertEqual(self.r.candidate(cid),original)
        prev=None
        for event in self.r.snapshot()['events']:
            h=event.pop('sha256');self.assertEqual(O.digest(event),h)
            self.assertEqual(event['previous_sha256'],prev);prev=h
        before=self.r.snapshot()
        with self.assertRaises(O.Refused):self.r.revise(cid,{'expiry_at':T},T,'past',['x'])
        self.assertEqual(self.r.snapshot(),before)
    def test_08_offline_only_frozen_baseline_hashes(self):
        cfg=copy.deepcopy(CFG);cfg['mode']='LIVE'
        with self.assertRaises(O.Refused):O.Radar(cfg)
        lock=json.loads((ROOT/'BASELINE_LOCK.json').read_text())
        actual={p.relative_to(ROOT/'baseline').as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'baseline').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
        self.assertEqual(lock,actual)

class SyntheticEntryTests(unittest.TestCase):
    def test_08_radar_record_complete_separate_plan_buy_and_whole_exit(self):
        os.environ['CANDIDATE_ROOT']=str(ROOT/'baseline/candidate_tree/candidate')
        sys.path.insert(0,str(ROOT/'baseline/prior_tests'))
        import rv_helpers as H
        sys.path.insert(0,str(H.ROOT/'catalyst_paper_delivery'))
        import entry_bridge as B
        cfg=copy.deepcopy(CFG);cfg['authorized_runs']['r1']=H.B
        synthetic=copy.deepcopy(CASES['SYNTH'])
        synthetic['clocks']['radar_first_discovered_at']=clock(H.B)
        synthetic['next_review_at']=H.at(H.B,3600);synthetic['expiry_at']=H.at(H.B,7200)
        synthetic['fixture_provenance']['operational_fixture_time']=H.B
        radar=O.Radar(cfg);radar.ingest(synthetic,H.B)
        record=json.loads((ROOT/'baseline/fixtures/synthetic_candidate.json').read_text())
        record['research_record']=radar.export_research(record['candidate_id'])
        self.assertIsNone(record['research_record']['explicit_forecast'])
        self.assertIsNone(record['explicit_forecast'])
        # Exact synthetic research -> decision ordering; no historical reconstruction.
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'paper'
            src=H.ROOT/'catalyst_paper_v2/legacy/checkpoint.json'
            versions=json.dumps({'checkpoint':{'id':H.LIB,'version':12},'workbook':{'id':H.LIB,'version':11}}).encode()
            B.R.initialize(root,'genesis',src,H.L.sha(src.read_bytes()),'2026-10-05T19:30:00Z',versions,True,True)
            def head():return H.L.verify_chain_dir(root/'chain')[-1]
            got=B.decide(root,'decision',head()['checkpoint_sha256'],H.LIB,record)
            self.assertEqual(got['status'],'ENTRY_DECISION_COMMITTED');self.assertIsNone(got['decision'])
            p=record['manager_dossier']['plan'];now=record['decision_request']['now']
            def forward(op,method,kw,rc=None):return B.forward(root,op,head()['checkpoint_sha256'],H.LIB,json.dumps([dict(method=method,kwargs=kw)]).encode(),json.dumps(rc).encode() if rc else None,synthetic_fixture=True,method=method)
            rc=H.rcpt(head(),40)|{'verified_at_utc':H.at(now,40)}
            buy=forward('buy','execute',H.inputs(p,now,60),rc)
            self.assertEqual(buy['decision'],'ENTER PAPER');self.assertFalse(buy['operational_entry_enabled'])
            t=p['checks'][0]
            forward('observe','observe',dict(now=t,bid='120',mark_at=t,exit_fee=H.appl(p,'SELL',t)))
            self.assertEqual(H.L.verify(head()).pending['kind'],'EXIT')
            sold=forward('sell','execute',dict(q=H.quote(H.at(t,5),'120','120.1','SYNTHETIC-EXIT'),now=H.at(t,7),exit_fee=H.appl(p,'SELL',H.at(t,6))),H.rcpt(head(),0)|{'verified_at_utc':H.at(t,4)})
            self.assertEqual(sold['execution_action'],'EXIT PAPER')
            self.assertIsNone(H.L.verify(head()).position)
            self.assertFalse(radar.candidate(record['candidate_id'])['forecast_admitted'])
