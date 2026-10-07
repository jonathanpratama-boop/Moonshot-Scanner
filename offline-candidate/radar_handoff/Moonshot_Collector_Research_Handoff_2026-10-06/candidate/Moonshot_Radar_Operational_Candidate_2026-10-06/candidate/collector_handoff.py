"""Bounded offline collector -> research -> assessment; no operational entrypoint.

Historical evidence is byte-pinned and never enters Radar's NEW queue. Synthetic
prospective evidence exercises a fresh, disposable Radar and the same read-only
entry_bridge.assess contract. No decide, forward, quote, publish or ledger call.
Pins establish retained-byte identity, not source truth or external authority.
"""
import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'candidate'))
sys.path.insert(0, str(ROOT / 'baseline/candidate_tree/candidate/catalyst_paper_delivery'))
import archive_adapter as A
import radar_ops as O
import entry_bridge as B


class HandoffRefused(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise HandoffRefused(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse(raw):
    require(isinstance(raw, bytes) and bool(raw), 'RAW_EVIDENCE_MISSING')
    def pairs(items):
        require(len({k for k, _ in items}) == len(items), 'DUPLICATE_JSON_KEY')
        return dict(items)
    def constant(_):
        raise HandoffRefused('NONFINITE_JSON')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HandoffRefused('INVALID_UTF8_JSON') from exc


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def detached(value):
    return parse(canonical(value))


def reason(code, field, detail):
    return {'code': code, 'field': field, 'detail': detail}


def evidence(path, raw, kind):
    return {'path': path, 'sha256': digest(raw), 'bytes': len(raw),
            'evidence_class': kind, 'raw_base64': base64.b64encode(raw).decode('ascii')}


@dataclass(frozen=True)
class HistoricalEnvelope:
    case: str
    manifest_raw: bytes
    record_raw: bytes
    capture_raw: bytes
    clock_index_raw: bytes

    @property
    def classification(self):
        return 'HISTORICAL'

    def __post_init__(self):
        require(self.case in A.CASES, 'ARCHIVE_CASE_NOT_PINNED')
        manifest = parse(self.manifest_raw)
        require(digest(self.manifest_raw) == A.MANIFEST_SHA256, 'ARCHIVE_MANIFEST_PIN_MISMATCH')
        pins = {row['path']: row for row in manifest['files']}
        for path, raw in self.files().items():
            parse(raw)
            require(digest(raw) == pins[path]['sha256'] and len(raw) == pins[path]['bytes'],
                    'ARCHIVE_RAW_PIN_MISMATCH:' + path)

    def files(self):
        return {'records/' + self.case + '.json': self.record_raw,
                'captures/' + self.case + '.json': self.capture_raw,
                'captures/case_clock_and_provenance_index.json': self.clock_index_raw}

    def identity(self):
        return parse(self.record_raw)['record_id']

    def fingerprint(self):
        return digest(canonical({'classification': self.classification, 'case': self.case,
                                 'files': {k: digest(v) for k, v in self.files().items()}}))


def from_archive(packet_path, case='techd'):
    packet = A.load_packet(packet_path)
    require(case in A.CASES, 'ARCHIVE_CASE_NOT_PINNED')
    return HistoricalEnvelope(case, packet.file('MANIFEST.json').raw,
                              packet.file('records/' + case + '.json').raw,
                              packet.file('captures/' + case + '.json').raw,
                              packet.file('captures/case_clock_and_provenance_index.json').raw)


@dataclass(frozen=True)
class SyntheticEnvelope:
    raw: bytes
    config_raw: bytes

    @property
    def classification(self):
        return 'SYNTHETIC_PROSPECTIVE'

    def __post_init__(self):
        row, config = parse(self.raw), parse(self.config_raw)
        require(isinstance(row, dict) and row.get('schema') == 'moonshot.synthetic.collector.v1'
                and row.get('classification') == self.classification
                and row.get('synthetic_fixture') is True, 'SYNTHETIC_ENVELOPE_REQUIRED')
        record = row.get('radar_record', {})
        require(record.get('fixture_provenance', {}).get('kind') == 'synthetic'
                and str(record.get('candidate_id', '')).startswith('SYNTHETIC-')
                and row.get('collector_id') == 'SYNTHETIC-COLLECTOR', 'SYNTHETIC_PROVENANCE_REQUIRED')
        require(record.get('state') == 'NEW' and record.get('explicit_forecast') is None,
                'SYNTHETIC_NEW_RESEARCH_ONLY')
        require(config.get('mode') == 'OFFLINE_SYNTHETIC_REPLAY', 'OFFLINE_SYNTHETIC_CONFIG_REQUIRED')
        require(isinstance(row.get('raw_text'), str) and bool(row['raw_text']), 'RAW_EVIDENCE_MISSING')
        require(digest(row['raw_text'].encode()) == row.get('raw_text_sha256'), 'RAW_BODY_HASH_MISMATCH')
        require(row.get('revision_of') is None, 'REVISION_REQUIRES_SEPARATE_REVIEW')

    def identity(self):
        return parse(self.raw)['radar_record']['candidate_id']

    def fingerprint(self):
        return digest(canonical({'classification': self.classification, 'raw': digest(self.raw),
                                 'config': digest(self.config_raw)}))


class ReviewIndex:
    """Deduplicate this process's review receipts; no queue, journal or revision writes."""
    def __init__(self):
        self._seen = {}

    def add(self, envelope):
        require(type(envelope) in (HistoricalEnvelope, SyntheticEnvelope), 'TYPED_ENVELOPE_REQUIRED')
        key = (envelope.classification, envelope.identity())
        old = self._seen.get(key)
        current = envelope.fingerprint()
        require(old is None or old == current, 'REVISION_REQUIRES_SEPARATE_REVIEW')
        self._seen[key] = current
        return {'status': 'REPEAT_REVIEW' if old else 'REVIEW_RETAINED',
                'classification': envelope.classification, 'envelope_sha256': current,
                'operational_queue_mutated': False}


def _historical(envelope):
    record, capture = parse(envelope.record_raw), parse(envelope.capture_raw)
    index = next(row for row in parse(envelope.clock_index_raw) if row['case'] == envelope.case)
    metadata = index['source_metadata']
    clocks = {'source_published_at': None, 'source_data_as_of': record['workbook_values'].get('Data as-of UTC'),
              'radar_first_discovered_at': None, 'alert_authored_at': capture['authored_at_utc'],
              'platform_rendered_at': capture['source_datetime_utc'],
              'platform_rendered_text': capture.get('rendered_time_text'),
              'collector_first_observed_at': index['collector_first_observed_at_utc'],
              'collector_read_started_at': metadata.get('read_started_clock_utc'),
              'collector_normalization_completed_at': metadata.get('normalization_completed_receipt_utc'),
              'collector_replay_cutoff': index['collector_replay_cutoff_utc']}
    issues = [reason('AUTHORED_TIME_UNKNOWN', '/clocks/alert_authored_at',
                     'Rendered time and collector receipt do not establish authorship; kept null.'),
              reason('SOURCE_PUBLICATION_TIME_UNKNOWN', '/clocks/source_published_at',
                     'No exact source publication clock is established by this retained envelope.'),
              reason('RADAR_FIRST_DISCOVERY_UNKNOWN', '/clocks/radar_first_discovered_at',
                     'Collector first observation is not first radar discovery.'),
              reason('UNREDACTED_RAW_NOT_RETAINED', '/evidence/unredacted_body',
                     'Exact redacted capture is preserved. The original body hash is a recorded claim only.')]
    continuity = ('MATCHED_RETAINED_MARKER' if metadata.get('previous_marker_found') is True
                  and metadata.get('previous_marker_body_hash_match') is True else 'GAP_OR_UNKNOWN')
    if continuity != 'MATCHED_RETAINED_MARKER':
        issues.append(reason('CONTINUITY_GAP', '/source_metadata', 'Retained marker continuity is not established.'))
    research = {'record_type': 'research_candidate', 'candidate_id': record['record_id'],
                'classification': 'HISTORICAL', 'state': 'HISTORICAL', 'synthetic_fixture': False,
                'identity': record['identity'], 'dependency_group_id': record['dependency_group_id'],
                'source_signal_id': record['source_signal_id'], 'clocks': clocks,
                'clock_provenance': index, 'archive_record': record,
                'explicit_forecast': None, 'forecast_admitted': False,
                'evidence': [evidence(path, raw, 'EXACT_RETAINED_REDACTED_ARCHIVE')
                             for path, raw in envelope.files().items()]}
    return research, issues, continuity, None


def _synthetic(envelope):
    doc, config = parse(envelope.raw), parse(envelope.config_raw)
    record = doc['radar_record']
    read = doc.get('source_read')
    require(isinstance(read, dict), 'COLLECTOR_READ_REQUIRED')
    require(read.get('source_id') == record['source_id'] and read.get('run_id') == record['run_id']
            and record['message_id'] in read.get('messages', []), 'COLLECTOR_RECORD_BINDING_MISMATCH')
    require(doc.get('collector_received_at') == record['clocks']['collector_received_at']['value'],
            'COLLECTOR_CLOCK_BINDING_MISMATCH')
    require(doc.get('authored_at') == record['clocks']['alert_authored_at']['value'],
            'AUTHORED_CLOCK_BINDING_MISMATCH')
    require(doc.get('source_published_at') == record['clocks']['published_at']['value'],
            'SOURCE_CLOCK_BINDING_MISMATCH')
    assessment_at = O.timestamp(doc['assessment_at'])
    require(O.timestamp(read['at']) <= assessment_at, 'FUTURE_COLLECTOR_READ')
    for key in ('source_published_at', 'platform_rendered_at', 'collector_received_at', 'authored_at'):
        if doc.get(key) is not None:
            require(O.timestamp(doc[key]) <= assessment_at, 'FUTURE_ENVELOPE_CLOCK:' + key)
    issues = []
    for key in ('collector_received_at', 'authored_at'):
        if doc.get(key) is None:
            issues.append(reason('COLLECTOR_TIME_UNKNOWN' if key == 'collector_received_at' else 'AUTHORED_TIME_UNKNOWN',
                                 '/' + key, 'Absent clock remains null; another clock is not substituted.'))
    radar = O.Radar(config)  # private scratch instance, never supplied by a live caller
    radar.source_read(read)
    radar.ingest(record, doc['assessment_at'])
    research = radar.export_research(record['candidate_id'])
    snapshot = radar.snapshot()
    source = record['source_id']
    continuity = 'MATCHED_SYNTHETIC_MARKER' if snapshot['continuity_verified'][source] else 'GAP_OR_UNKNOWN'
    if continuity == 'GAP_OR_UNKNOWN':
        issues.append(reason('CONTINUITY_GAP', '/source_read', 'The retained source read has unresolved continuity.'))
    research.update(classification=envelope.classification, synthetic_fixture=True,
                    collector_clocks={key: doc.get(key) for key in
                                      ('source_published_at', 'platform_rendered_at', 'collector_received_at', 'authored_at')},
                    collector_raw_evidence=evidence('synthetic_collector_envelope.json', envelope.raw, 'SYNTHETIC_TEST_ONLY'))
    return research, issues, continuity, snapshot


def assess_handoff(envelope, *, manager_dossier=None, decision_request=None, expected_identity=None,
                   requested_state=None):
    require(type(envelope) in (HistoricalEnvelope, SyntheticEnvelope), 'TYPED_ENVELOPE_REQUIRED')
    historical = type(envelope) is HistoricalEnvelope
    if requested_state is not None:
        require(requested_state == ('HISTORICAL' if historical else 'NEW'),
                'HISTORICAL_PROMOTION_PROHIBITED' if historical else 'STATE_CHANGE_OUT_OF_SCOPE')
    research, provenance, continuity, radar_snapshot = _historical(envelope) if historical else _synthetic(envelope)
    bundle = {'candidate_id': envelope.identity(), 'research_record': research,
              'synthetic_fixture': not historical, 'explicit_forecast': None,
              'manager_dossier': detached(manager_dossier), 'decision_request': detached(decision_request)}
    bridge = B.assess(bundle)  # Presence and hash-binding diagnostics only.
    input_reasons = detached(bridge['reasons'])
    if expected_identity is not None and expected_identity != research['identity']:
        input_reasons.append(reason('IDENTITY_MISMATCH', '/research_record/identity',
                                    'Expected exact identity differs from the retained evidence; no relabeling applied.'))
    if not isinstance(research['identity'], dict):
        input_reasons.append(reason('IDENTITY_UNRESOLVED', '/research_record/identity',
                                    'Exact identity remains unknown; no identity inferred from the plan.'))
    manager = bundle['manager_dossier']
    if isinstance(manager, dict) and isinstance(manager.get('plan'), dict) and isinstance(research['identity'], dict):
        identity, plan = research['identity'], manager['plan']
        instrument = identity.get('ticker') or identity.get('symbol')
        currency = identity.get('quote_currency') or identity.get('currency')
        if plan.get('instrument') != instrument or plan.get('currency') != currency:
            input_reasons.append(reason('PLAN_IDENTITY_MISMATCH', '/manager_dossier/plan',
                                        'Plan instrument/currency does not match the retained research identity.'))
    eligibility = {'status': 'NOT_EVALUATED', 'economic_eligibility': 'not_evaluated',
                   'manager_input_status': bridge['status'], 'input_reasons': input_reasons,
                   'scope_reasons': ['HISTORICAL_REPLAY_ONLY'] if historical else ['SYNTHETIC_ASSESSMENT_ONLY'],
                   'entry_eligible': False, 'prospective_admitted': False, 'forecast_required': False}
    if input_reasons:
        eligibility['manager_input_status'] = 'BLOCKED_INPUT'
    return {'schema': 'moonshot.collector.research.assessment.v1', 'classification': envelope.classification,
            'envelope_sha256': envelope.fingerprint(), 'research_record': research,
            'provenance': {'status': 'RETAINED_WITH_KNOWN_LIMITS' if historical else 'SYNTHETIC_DECLARATIONS_ONLY',
                           'issues': provenance, 'continuity_status': continuity,
                           'source_truth_verified': False, 'original_detector_equivalence': 'UNVERIFIED'},
            'eligibility': eligibility, 'decision': None, 'execution_action': None,
            'operational_queue_mutated': False, 'study_admission_performed': False,
            'manager_dossier_synthesized': False, 'quote_retrieval_performed': False,
            'publication_performed': False, 'execution_performed': False,
            'radar_path': 'BYPASSED_HISTORICAL_QUEUE' if historical else 'DISPOSABLE_SYNTHETIC_RADAR',
            'synthetic_radar_snapshot': radar_snapshot,
            'assessment_runtime': {'optimize': sys.flags.optimize, 'isolated': sys.flags.isolated}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--historical-packet', type=Path)
    group.add_argument('--synthetic-envelope', type=Path)
    parser.add_argument('--case', default='techd', choices=A.CASES)
    parser.add_argument('--synthetic-config', type=Path)
    parser.add_argument('--manager-dossier', type=Path)
    parser.add_argument('--decision-request', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.historical_packet:
            require(args.synthetic_config is None, 'HISTORICAL_SYNTHETIC_CONFIG_FORBIDDEN')
            envelope = from_archive(args.historical_packet, args.case)
        else:
            require(args.synthetic_config is not None, 'SYNTHETIC_CONFIG_REQUIRED')
            envelope = SyntheticEnvelope(args.synthetic_envelope.read_bytes(), args.synthetic_config.read_bytes())
        manager = parse(args.manager_dossier.read_bytes()) if args.manager_dossier else None
        request = parse(args.decision_request.read_bytes()) if args.decision_request else None
        result = assess_handoff(envelope, manager_dossier=manager, decision_request=request)
    except (HandoffRefused, A.ArchiveRefused, O.Refused) as exc:
        print(json.dumps({'status': 'REFUSED', 'reason': str(exc), 'decision': None, 'execution_performed': False}))
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
