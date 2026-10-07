"""Finite connection contracts only; no persisted engine or external access."""
import base64
import copy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
CANDIDATE = ROOT / 'candidate/Moonshot_Radar_Operational_Candidate_2026-10-06'
sys.path.insert(0, str(CANDIDATE / 'candidate'))
import collector_handoff as H
TRACE = []


def fixture(name):
    return json.loads((ROOT / 'fixtures' / (name + '.json')).read_bytes())


def synthetic(doc=None):
    return H.SyntheticEnvelope(H.canonical(doc or fixture('synthetic_envelope')),
                               (ROOT / 'fixtures/synthetic_config.json').read_bytes())


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.historical = H.from_archive(ROOT / 'recovered_packet')
        self.manager = fixture('synthetic_manager_dossier')
        self.request = fixture('synthetic_assessment_request')

    def complete(self, envelope=None, **kwargs):
        return H.assess_handoff(envelope or synthetic(), manager_dossier=self.manager,
                                decision_request=self.request, **kwargs)

    def test_historical_bytes_clocks_and_unknowns_round_trip(self):
        result = H.assess_handoff(self.historical)
        research = result['research_record']
        for entry in research['evidence']:
            self.assertEqual(base64.b64decode(entry['raw_base64']), self.historical.files()[entry['path']])
        clocks = research['clocks']
        self.assertEqual(clocks['platform_rendered_at'], '2026-10-05T07:56:25.357Z')
        self.assertEqual(clocks['collector_first_observed_at'], '2026-10-05T08:08:22Z')
        self.assertIsNone(clocks['alert_authored_at'])
        self.assertIsNone(clocks['source_published_at'])
        self.assertIsNone(clocks['radar_first_discovered_at'])
        self.assertEqual(result['classification'], 'HISTORICAL')
        self.assertEqual(research['state'], 'HISTORICAL')
        self.assertFalse(research['synthetic_fixture'])
        self.assertIn('UNREDACTED_RAW_NOT_RETAINED', [x['code'] for x in result['provenance']['issues']])

    def test_historical_never_enters_radar_or_execution(self):
        with patch.object(H.O, 'Radar', side_effect=AssertionError('Historical Radar forbidden')), \
             patch.object(H.B, 'decide', side_effect=AssertionError('decide forbidden')), \
             patch.object(H.B, 'forward', side_effect=AssertionError('forward forbidden')), \
             patch.object(H.B.R, 'advance', side_effect=AssertionError('persistence forbidden')):
            result = H.assess_handoff(self.historical)
        self.assertFalse(result['operational_queue_mutated'])
        self.assertFalse(result['study_admission_performed'])
        self.assertFalse(result['execution_performed'])
        self.assertIsNone(result['decision'])
        with self.assertRaisesRegex(ValueError, 'SYNTHETIC_FIXTURE_ONLY'):
            H.B._fixture(result['research_record'])
        with self.assertRaisesRegex(H.O.Refused, 'fixture provenance required'):
            H.O.Radar(fixture('synthetic_config')).ingest(result['research_record'], self.request['now'])

    def test_historical_promotion_explicitly_refused(self):
        for state in ['NEW', 'PROSPECTIVE', 'ADMITTED']:
            with self.subTest(state=state), self.assertRaisesRegex(H.HandoffRefused, 'HISTORICAL_PROMOTION_PROHIBITED'):
                H.assess_handoff(self.historical, requested_state=state)
        with self.assertRaises(FrozenInstanceError):
            self.historical.classification = 'SYNTHETIC_PROSPECTIVE'
        with self.assertRaisesRegex(H.HandoffRefused, 'SYNTHETIC_ENVELOPE_REQUIRED'):
            H.SyntheticEnvelope(self.historical.record_raw, synthetic().config_raw)

    def test_historical_pin_tamper_and_missing_raw_refused(self):
        with self.assertRaisesRegex(H.HandoffRefused, 'RAW_EVIDENCE_MISSING'):
            replace(self.historical, capture_raw=b'')
        with self.assertRaisesRegex(H.HandoffRefused, 'ARCHIVE_RAW_PIN_MISMATCH'):
            replace(self.historical, capture_raw=self.historical.capture_raw + b' ')
        record = H.parse(self.historical.record_raw)
        record['authored_at_utc'] = record['source_displayed_datetime_utc']
        with self.assertRaisesRegex(H.HandoffRefused, 'ARCHIVE_RAW_PIN_MISMATCH'):
            replace(self.historical, record_raw=H.canonical(record))

    def test_missing_dossier_exact_frozen_reasons_no_forecast_gate(self):
        result = H.assess_handoff(self.historical)
        expected = H.B.assess({'candidate_id': self.historical.identity(),
                              'research_record': result['research_record'], 'synthetic_fixture': False,
                              'explicit_forecast': None, 'manager_dossier': None, 'decision_request': None})
        self.assertEqual(result['eligibility']['input_reasons'], expected['reasons'])
        self.assertEqual(len(expected['reasons']), 109)
        self.assertIn({'code': 'MANAGER_DOSSIER_REQUIRED', 'field': '/manager_dossier',
                       'detail': 'Research and optional forecast records are not manager-authored plans.'}, expected['reasons'])
        self.assertFalse(result['eligibility']['forecast_required'])
        self.assertFalse(any('probability' in x.get('field', '') or 'explicit_forecast' in x.get('field', '') for x in expected['reasons']))

    def test_identity_mismatch_preserves_original(self):
        identity = H.parse(self.historical.record_raw)['identity']
        wrong = dict(identity, venue='WRONG')
        result = H.assess_handoff(self.historical, expected_identity=wrong)
        self.assertEqual(result['research_record']['identity'], identity)
        self.assertIn('IDENTITY_MISMATCH', [x['code'] for x in result['eligibility']['input_reasons']])

    def test_synthetic_complete_assessment_without_execution(self):
        before = H.canonical(self.manager)
        with patch.object(H.B, 'decide', side_effect=AssertionError('decide forbidden')), \
             patch.object(H.B, 'forward', side_effect=AssertionError('forward forbidden')), \
             patch.object(H.B.R, 'advance', side_effect=AssertionError('persistence forbidden')):
            result = self.complete()
        self.assertEqual(H.canonical(self.manager), before)
        self.assertEqual(result['eligibility']['manager_input_status'], 'DOSSIER_PRESENT_PENDING_FROZEN_VALIDATION')
        self.assertEqual(result['eligibility']['input_reasons'], [])
        self.assertEqual(result['eligibility']['status'], 'NOT_EVALUATED')
        self.assertEqual(result['research_record']['record_type'], 'research_candidate')
        self.assertIn('radar_record_sha256', result['research_record'])
        self.assertEqual(len(result['synthetic_radar_snapshot']['candidates']), 1)
        self.assertEqual(result['synthetic_radar_snapshot']['alert_previews'], [])
        for key in ['entry_eligible', 'prospective_admitted', 'forecast_required']:
            self.assertFalse(result['eligibility'][key])
        for key in ['execution_performed', 'publication_performed', 'quote_retrieval_performed', 'study_admission_performed']:
            self.assertFalse(result[key])
        self.assertIsNone(result['decision'])

    def test_synthetic_missing_raw_and_hash_mismatch(self):
        doc = fixture('synthetic_envelope'); doc.pop('raw_text')
        with self.assertRaisesRegex(H.HandoffRefused, 'RAW_EVIDENCE_MISSING'):
            synthetic(doc)
        doc = fixture('synthetic_envelope'); doc['raw_text'] += ' changed'
        with self.assertRaisesRegex(H.HandoffRefused, 'RAW_BODY_HASH_MISMATCH'):
            synthetic(doc)

    def test_synthetic_missing_authored_and_collector_clocks_stay_unknown(self):
        for outer, inner, code in [('authored_at', 'alert_authored_at', 'AUTHORED_TIME_UNKNOWN'),
                                   ('collector_received_at', 'collector_received_at', 'COLLECTOR_TIME_UNKNOWN')]:
            doc = fixture('synthetic_envelope'); doc[outer] = None
            doc['radar_record']['clocks'][inner] = H.O.unknown('SYNTHETIC missing clock test')
            result = self.complete(synthetic(doc))
            self.assertIn(code, [x['code'] for x in result['provenance']['issues']])
            self.assertIsNone(result['research_record']['clocks'][inner]['value'])
            # Authorship diagnostics do not invent a forecast or economic gate.
            self.assertEqual(result['eligibility']['input_reasons'], [])
            self.assertEqual(result['eligibility']['status'], 'NOT_EVALUATED')

    def test_conflicting_clock_binding_refused(self):
        for field, code in [('authored_at', 'AUTHORED_CLOCK_BINDING_MISMATCH'),
                            ('collector_received_at', 'COLLECTOR_CLOCK_BINDING_MISMATCH')]:
            doc = fixture('synthetic_envelope'); doc[field] = None
            with self.assertRaisesRegex(H.HandoffRefused, code):
                self.complete(synthetic(doc))

    def test_future_read_and_contradictory_publication_clock_refused(self):
        doc = fixture('synthetic_envelope'); doc['source_read']['at'] = '2026-10-05T20:00:00Z'
        with self.assertRaisesRegex(H.HandoffRefused, 'FUTURE_COLLECTOR_READ'):
            self.complete(synthetic(doc))
        doc = fixture('synthetic_envelope'); doc['source_published_at'] = None
        with self.assertRaisesRegex(H.HandoffRefused, 'SOURCE_CLOCK_BINDING_MISMATCH'):
            self.complete(synthetic(doc))
        doc = fixture('synthetic_envelope'); doc['platform_rendered_at'] = '2026-10-05T20:00:00Z'
        with self.assertRaisesRegex(H.HandoffRefused, 'FUTURE_ENVELOPE_CLOCK'):
            self.complete(synthetic(doc))

    def test_continuity_gap_remains_separate_from_dossier(self):
        doc = fixture('synthetic_envelope'); doc['source_read']['history_complete'] = False
        result = self.complete(synthetic(doc))
        self.assertEqual(result['provenance']['continuity_status'], 'GAP_OR_UNKNOWN')
        self.assertIn('CONTINUITY_GAP', [x['code'] for x in result['provenance']['issues']])
        self.assertEqual(result['eligibility']['manager_input_status'], 'DOSSIER_PRESENT_PENDING_FROZEN_VALIDATION')
        self.assertFalse(result['eligibility']['entry_eligible'])
        self.assertFalse(result['synthetic_radar_snapshot']['gap_history']['primary'][0]['resolved'])

    def test_duplicate_does_not_create_new_or_promote(self):
        index = H.ReviewIndex()
        for envelope in [self.historical, synthetic()]:
            self.assertEqual(index.add(envelope)['status'], 'REVIEW_RETAINED')
            repeat = index.add(envelope)
            self.assertEqual(repeat['status'], 'REPEAT_REVIEW')
            self.assertFalse(repeat['operational_queue_mutated'])

    def test_conflicting_revision_is_not_silently_overwritten(self):
        index = H.ReviewIndex(); envelope = synthetic(); before = index.add(envelope)
        doc = fixture('synthetic_envelope'); doc['raw_text'] += ' revision'
        doc['raw_text_sha256'] = H.digest(doc['raw_text'].encode())
        with self.assertRaisesRegex(H.HandoffRefused, 'REVISION_REQUIRES_SEPARATE_REVIEW'):
            index.add(synthetic(doc))
        self.assertEqual(index.add(envelope)['envelope_sha256'], before['envelope_sha256'])
        doc['revision_of'] = envelope.fingerprint()
        with self.assertRaisesRegex(H.HandoffRefused, 'REVISION_REQUIRES_SEPARATE_REVIEW'):
            synthetic(doc)

    def test_separate_dossier_candidate_binding_and_plan_hash(self):
        self.manager['candidate_id'] = 'OTHER'
        result = self.complete()
        self.assertIn({'code': 'PLAN_BINDING_MISMATCH', 'field': '/manager_dossier/candidate_id'},
                      result['eligibility']['input_reasons'])
        self.manager = fixture('synthetic_manager_dossier'); self.manager['plan_sha256'] = '0' * 64
        result = self.complete()
        self.assertIn({'code': 'PLAN_BINDING_MISMATCH', 'field': '/manager_dossier/plan_sha256'},
                      result['eligibility']['input_reasons'])

    def test_plan_identity_mismatch_not_overwritten(self):
        self.manager['plan']['instrument'] = 'OTHER'
        self.manager['plan_sha256'] = H.B.C.digest(self.manager['plan'])
        result = self.complete()
        self.assertIn('PLAN_IDENTITY_MISMATCH', [x['code'] for x in result['eligibility']['input_reasons']])
        self.assertEqual(result['research_record']['identity']['symbol'], 'SYNTH')

    def test_unresolved_identity_reports_missing_input(self):
        doc = fixture('synthetic_envelope'); doc['radar_record']['identity'] = None
        result = self.complete(synthetic(doc))
        self.assertIsNone(result['research_record']['identity'])
        self.assertIn('IDENTITY_UNRESOLVED', [x['code'] for x in result['eligibility']['input_reasons']])
        self.assertEqual(result['eligibility']['manager_input_status'], 'BLOCKED_INPUT')

    def test_collector_record_binding_mismatch(self):
        doc = fixture('synthetic_envelope'); doc['source_read']['messages'] = ['other']
        with self.assertRaisesRegex(H.HandoffRefused, 'COLLECTOR_RECORD_BINDING_MISMATCH'):
            self.complete(synthetic(doc))

    def test_cli_child_actual_optimization_and_no_execution(self):
        command = [sys.executable, '-I', '-S', '-B'] + (['-O'] if sys.flags.optimize else []) + [str(Path(H.__file__)),
                   '--synthetic-envelope', str(ROOT/'fixtures/synthetic_envelope.json'),
                   '--synthetic-config', str(ROOT/'fixtures/synthetic_config.json'),
                   '--manager-dossier', str(ROOT/'fixtures/synthetic_manager_dossier.json'),
                   '--decision-request', str(ROOT/'fixtures/synthetic_assessment_request.json')]
        child = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        output = json.loads(child.stdout)
        self.assertEqual(output['assessment_runtime']['optimize'], sys.flags.optimize)
        self.assertEqual(output['assessment_runtime']['isolated'], 1)
        self.assertFalse(output['execution_performed'])
        TRACE.append({'argv': command, 'returncode': child.returncode,
                      'actual_child_runtime': output['assessment_runtime'], 'stderr': child.stderr})
