"""Deterministic OFFLINE radar replay. No sources, detectors, clocks or alerts are fetched.

The caller supplies a frozen fixture configuration and traceable records. This layer
validates consistency, not source truth or live authorization. Its append-only output
is review evidence, not a durable production journal or an execution authority.
"""
import argparse
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path

READ_STATES = ('SUCCESS_NEW', 'SUCCESS_NO_NEW', 'FAILED', 'PARTIAL', 'EXPECTED_NOT_OBSERVED')
CLOCKS = ('published_at', 'first_verified_available_at', 'radar_first_discovered_at',
          'alert_authored_at', 'collector_received_at', 'delivered_at', 'source_data_as_of', 'retrieved_at')
CAUSES = ('PUBLICATION_NOT_YET_AVAILABLE', 'SOURCE_ACCESS_FAILURE', 'POLLING_LATENCY',
          'SEARCH_MISS', 'QUEUE_FOLLOWUP_MISS', 'COLLECTOR_DELAY', 'UNKNOWN')
IDENTITY = {'stock': ('ticker', 'venue', 'currency', 'security_id'),
            'crypto': ('symbol', 'chain', 'contract', 'product', 'venue', 'currency')}
TRANSITIONS = {'NEW': {'RESEARCHING', 'CLOSED'}, 'RESEARCHING': {'WATCHING', 'CLOSED'},
               'WATCHING': {'RESEARCHING', 'CLOSED'}, 'CLOSED': {'RESEARCHING'}}


class Refused(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def clone(value):
    return json.loads(canonical(value))


def timestamp(value):
    require(isinstance(value, str), 'timestamp must be an explicit string')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise Refused('invalid timestamp') from exc
    require(dt.tzinfo is not None and 'T' in value, 'timezone and time required')
    return dt


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def unknown(reason):
    return {'value': None, 'verified': False, 'null_reason': reason, 'evidence': None,
            'precision': None, 'semantics': None, 'timezone': None, 'publisher': None}


def validate_clock(value, name):
    require(isinstance(value, dict), name + ': clock object required')
    if value.get('value') is None:
        require(value.get('verified') is False and nonempty(value.get('null_reason')), name + ': unknown reason required')
    else:
        timestamp(value['value'])
        require(value.get('verified') is True and all(nonempty(value.get(k)) for k in
                ('evidence', 'precision', 'semantics', 'timezone', 'publisher')), name + ': verified provenance required')
        require(value['semantics'] in ('exact_event', 'available_by'), name + ': unsupported semantics')
        require(value['precision'] in ('second', 'minute', 'day'), name + ': invalid precision')
    return clone(value)


def delay(start, end):
    if not all(c.get('verified') is True and c.get('value') and c.get('semantics') == 'exact_event'
               and c.get('precision') == 'second' for c in (start, end)):
        return {'seconds': None, 'reason': 'missing, bounded or insufficiently precise clock'}
    seconds = (timestamp(end['value']) - timestamp(start['value'])).total_seconds()
    return {'seconds': seconds if seconds >= 0 else None,
            'reason': None if seconds >= 0 else 'chronology conflict'}


def event_lead_time(event_clock, reference_clock, reference_name):
    require(reference_name in ('radar_first_discovered_at', 'collector_received_at'), 'lead-time clock must be named')
    if not all(c.get('verified') is True and c.get('value') and c.get('precision') == 'second'
               and c.get('semantics') == 'exact_event' for c in (event_clock, reference_clock)):
        return {'seconds': None, 'reference_clock': reference_name, 'reason': 'event/reference time unverified or inexact'}
    return {'seconds': (timestamp(event_clock['value'])-timestamp(reference_clock['value'])).total_seconds(),
            'reference_clock': reference_name, 'reason': None}


def identity_key(value):
    if not isinstance(value, dict) or value.get('asset_class') not in IDENTITY:
        return None
    fields = IDENTITY[value['asset_class']]
    if not all(nonempty(value.get(k)) for k in fields):
        return None
    if value['asset_class'] == 'crypto' and value['product'] not in ('spot', 'perpetual'):
        return None
    return (value['asset_class'], *(value[k] for k in fields))


class FrozenFixtureDetector:
    """Byte-preserving injection boundary; does not implement or certify ASTRA."""
    def __init__(self, config_hash, fixtures):
        self.config_hash = config_hash
        self.fixtures = copy.deepcopy(fixtures)

    def evaluate(self, key, input_record, expected_config_hash):
        require(expected_config_hash == self.config_hash, 'detector configuration mismatch')
        row = self.fixtures[key]
        require(row['input_sha256'] == digest(input_record), 'detector input differs from fixture')
        raw = row['output_utf8'].encode()
        require(hashlib.sha256(raw).hexdigest() == row['output_sha256'], 'detector output pin mismatch')
        return bytes(raw)


class Radar:
    def __init__(self, config):
        self._config = clone(config)
        require(config.get('mode') == 'OFFLINE_SYNTHETIC_REPLAY', 'live operation unsupported')
        require(nonempty(config.get('authorization_evidence')), 'scope evidence required')
        require(bool(config.get('universe')) and bool(config.get('sources')), 'frozen scope required')
        for items in config['universe'].values():
            require(bool(items) and all(identity_key(x) for x in items), 'exact allowed identities required')
        for src in config['sources'].values():
            require(src.get('kind') in ('disclosure', 'tape'), 'unknown source category')
            require(set(src['universe']).issubset(config['universe']), 'source widens universe')
            require('expected_runs' in src and 'watermark' in src and 'connector_limits' in src
                    and 'calendar' in src and 'coverage_start' in src, 'source registry incomplete')
            if src['expected_runs'] is not None:
                require(nonempty(src.get('schedule_evidence')), 'schedule provenance required')
                require(set(src['expected_runs']).issubset(config['authorized_runs']), 'source widens cadence')
        for when in config['authorized_runs'].values():
            timestamp(when)
        self._candidates = {}
        self._dependencies = {}
        self._events = []
        self._reads = {}
        self._watermarks = {k: v['watermark'] for k, v in config['sources'].items()}
        self._gaps = {k: [] for k in config['sources']}
        self._continuity = {k: v.get('continuity_verified') is True for k, v in config['sources'].items()}
        self._alerts = {}
        self._append('CONFIG', self._config)

    def _append(self, kind, body):
        previous = self._events[-1]['sha256'] if self._events else None
        event = {'seq': len(self._events), 'previous_sha256': previous, 'kind': kind, 'body': clone(body)}
        event['sha256'] = digest(event)
        self._events.append(event)
        return clone(event)

    def candidate(self, candidate_id):
        return clone(self._candidates[candidate_id])

    def snapshot(self):
        return clone({'schema': 'RADAR_REPLAY_V1', 'operational_enabled': False, 'study_state': 'PAUSED',
                      'config_sha256': digest(self._config), 'candidates': self._candidates,
                      'events': self._events, 'reads': list(self._reads.values()),
                      'watermarks': self._watermarks, 'continuity_verified': self._continuity,
                      'gap_history': self._gaps, 'alert_previews': list(self._alerts.values())})

    def _clockset(self, values):
        require(set(values) == set(CLOCKS), 'all clock fields and null reasons required')
        return {name: validate_clock(values[name], name) for name in CLOCKS}

    def _bounded(self, record, now):
        require(nonempty(record.get('owner')) and bool(record.get('evidence_to_clear')), 'owner and next evidence required')
        bounds = [record.get('next_review_at'), record.get('expiry_at')]
        trigger = record.get('event_trigger')
        if trigger:
            require(trigger.get('verified') is True and nonempty(trigger.get('evidence'))
                    and nonempty(trigger.get('event')), 'verified event trigger required')
            bounds.append(trigger.get('review_deadline'))
        require(any(bounds), 'bounded next review required')
        for bound in filter(None, bounds):
            require(timestamp(bound) > timestamp(now), 'review/expiry must remain prospective')

    def _validate_identity(self, identity, universe_key):
        if identity is None:
            return
        require(identity_key(identity) in [identity_key(x) for x in self._config['universe'][universe_key]], 'identity outside frozen universe')
        require(identity.get('verified') is True and nonempty(identity.get('evidence')), 'identity evidence required')

    def ingest(self, record, now):
        record = clone(record)
        timestamp(now)
        require(record.get('fixture_provenance', {}).get('kind') in ('synthetic', 'proposal_described_claim'), 'fixture provenance required')
        require(record.get('universe_key') in self._config['universe'], 'candidate widens universe')
        for key in ('candidate_id', 'dependency_group_id', 'mechanism', 'invalidation', 'source_id', 'run_id', 'message_id', 'configuration_label', 'configuration_hash'):
            require(nonempty(record.get(key)), key + ' required')
        require(record['source_id'] in self._config['sources'], 'unknown source')
        require(record['universe_key'] in self._config['sources'][record['source_id']]['universe'], 'candidate outside source scope')
        require(record['run_id'] in self._config['authorized_runs'], 'unauthorized run')
        require(timestamp(now) >= timestamp(self._config['authorized_runs'][record['run_id']]), 'candidate precedes its run')
        require(record.get('state') == 'NEW' and record.get('explicit_forecast') is None, 'new record must be research only')
        require(bool(record.get('evidence')) and isinstance(record.get('missing_facts'), list), 'evidence/missing facts required')
        self._clockset(record['clocks'])
        require('identity' in record, 'identity value or explicit null required')
        require(all(c.get('value') is None or timestamp(c['value']) <= timestamp(now) for c in record['clocks'].values()), 'record clock lies after ingestion')
        self._validate_identity(record.get('identity'), record['universe_key'])
        self._bounded(record, now)
        require(record.get('late_cause', 'UNKNOWN') == 'UNKNOWN', 'late cause needs separate trace')
        require(record['configuration_hash'] == self._config['detector_config_sha256'], 'wrong detector config')
        dep = record['dependency_group_id']
        if dep in self._dependencies:
            existing = self._candidates[self._dependencies[dep]]
            require(existing['universe_key'] == record['universe_key'], 'dependency crosses instruments')
            self._append('REPEATED_EVIDENCE', {'candidate_id': existing['candidate_id'], 'record': record, 'at': now})
            return {'status': 'EXISTING_CANDIDATE', 'candidate_id': existing['candidate_id'], 'alert': None}
        require(record['candidate_id'] not in self._candidates, 'candidate ID already used')
        record.update(revision_id=0, parent_revision_id=None, original_discovered_at=record['clocks']['radar_first_discovered_at'],
                      explicit_forecast=None, forecast_admitted=False, last_revised_at=now)
        self._candidates[record['candidate_id']] = record
        self._dependencies[dep] = record['candidate_id']
        self._append('CANDIDATE', record)
        return {'status': 'NEW', 'candidate_id': record['candidate_id'], 'alert': None}

    def revise(self, candidate_id, changes, now, reason, evidence):
        old = self._candidates[candidate_id]
        require(timestamp(now) >= timestamp(old['last_revised_at']), 'revision time reverses')
        require(nonempty(reason) and bool(evidence), 'revision reason and evidence required')
        allowed = {'state', 'identity', 'next_review_at', 'expiry_at', 'event_trigger', 'evidence_to_clear',
                   'missing_facts', 'evidence', 'owner', 'closure_reason', 'detector_result', 'unsupported_claims'}
        require(bool(changes) and set(changes).issubset(allowed), 'immutable or unknown candidate field')
        new = clone(old)
        new.update(clone(changes))
        if new['state'] != old['state']:
            require(new['state'] in TRANSITIONS[old['state']], 'invalid state transition')
        self._validate_identity(new['identity'], new['universe_key'])
        if new['state'] == 'CLOSED':
            require(nonempty(new.get('closure_reason')), 'closure reason required')
        else:
            self._bounded(new, now)
            new.pop('closure_reason', None)
        new.update(revision_id=old['revision_id']+1, parent_revision_id=old['revision_id'], last_revised_at=now)
        self._candidates[candidate_id] = new
        self._append('REVISION', {'before_sha256': digest(old), 'candidate': new, 'reason': reason, 'evidence': evidence, 'at': now})
        return {'status': new['state'], 'revision_id': new['revision_id'], 'alert': None}

    def add_receipt(self, candidate_id, receipt_id, kind, clock, content_sha256):
        require(kind in ('collector_received_at', 'delivered_at'), 'receipt kind invalid')
        clock = validate_clock(clock, kind)
        require(clock['value'] and clock['semantics'] == 'exact_event', 'receipt must prove its event')
        require(nonempty(receipt_id) and len(content_sha256) == 64, 'receipt identity/hash required')
        self.candidate(candidate_id)
        body = dict(candidate_id=candidate_id, receipt_id=receipt_id, kind=kind, clock=clock, content_sha256=content_sha256)
        previous = [e['body'] for e in self._events if e['kind'] == 'RECEIPT' and e['body']['receipt_id'] == receipt_id]
        if previous:
            require(previous[0] == body, 'receipt is immutable')
            return {'status': 'DUPLICATE_RECEIPT'}
        self._append('RECEIPT', body)
        return {'status': 'RECORDED_OBSERVATION_ONLY', 'authorship_established': False}

    def review(self, candidate_id, changes, now, reason, evidence):
        c = self._candidates[candidate_id]
        require(c['state'] != 'CLOSED', 'closed record needs explicit linked reopening')
        bounds = [c.get('next_review_at'), c.get('expiry_at'), (c.get('event_trigger') or {}).get('review_deadline')]
        deadline = min(timestamp(x) for x in bounds if x)
        result = self.revise(candidate_id, changes, now, reason, evidence)
        self._append('REVIEWED', {'candidate_id': candidate_id, 'at': now, 'due_at': deadline.isoformat(),
                                  'on_time': timestamp(now) <= deadline, 'evidence': evidence})
        return result

    def attribute_delay(self, candidate_id, cause, trace):
        require(cause in CAUSES and cause != 'UNKNOWN', 'specific supported cause required')
        require(trace.get('supports_cause') == cause and trace.get('verified') is True
                and nonempty(trace.get('source')) and bool(trace.get('run_or_message_ids'))
                and nonempty(trace.get('explanation')), 'trace-supported attribution required')
        self.candidate(candidate_id)
        self._append('DELAY_ATTRIBUTION', {'candidate_id': candidate_id, 'cause': cause, 'trace': trace})
        return {'cause': cause, 'source_truth_verified_by_software': False}

    def forecast(self, candidate_id, statement, now):
        c = self._candidates[candidate_id]
        require(c['explicit_forecast'] is None, 'forecast original cannot be overwritten')
        require(c['identity'] is not None, 'exact forecast identity required')
        require(statement.get('explicit') is True and all(nonempty(statement.get(k)) for k in
                ('original_wording', 'event', 'price_basis', 'author', 'source_evidence')), 'explicit original forecast required')
        authored = validate_clock(statement['authored_at'], 'forecast authorship')
        require(authored['value'] is not None and authored['semantics'] == 'exact_event', 'verified authorship required')
        require(timestamp(authored['value']) <= timestamp(now) < timestamp(statement['deadline']), 'forecast must be prospective')
        require(timestamp(now) >= timestamp(c['last_revised_at']), 'forecast time reverses')
        # Recording a statement is never admission to a frozen or future cohort.
        new = clone(c)
        new['explicit_forecast'] = clone(statement)
        new['revision_id'] += 1
        new['parent_revision_id'] = c['revision_id']
        new['last_revised_at'] = now
        self._candidates[candidate_id] = new
        self._append('EXPLICIT_FORECAST_STATEMENT', {'candidate_id': candidate_id, 'statement': statement, 'at': now, 'cohort_admitted': False})
        return {'status': 'STATEMENT_RECORDED', 'cohort_admitted': False}

    def select_series(self, candidate_id, series_identity):
        candidate = self._candidates[candidate_id]
        require(candidate.get('identity') is not None, 'identity unresolved')
        require(identity_key(series_identity) is not None and identity_key(series_identity) == identity_key(candidate['identity']), 'wrong or incomplete price-series identity')
        return {'status': 'IDENTITY_MATCH', 'source_truth_verified': False}

    def session_endpoint(self, candidate_id, calendar, name):
        c = self._candidates[candidate_id]
        require(calendar.get('verified') is True and nonempty(calendar.get('evidence')), 'verified calendar required')
        self.select_series(candidate_id, calendar['identity'])
        timestamp(calendar[name])
        return calendar[name]

    def due(self, now):
        when = timestamp(now)
        rows = []
        for c in self._candidates.values():
            if c['state'] == 'CLOSED':
                continue
            bounds = [c.get('next_review_at'), c.get('expiry_at'), (c.get('event_trigger') or {}).get('review_deadline')]
            earliest = min(timestamp(x) for x in bounds if x)
            if earliest <= when:
                rows.append({'candidate_id': c['candidate_id'], 'due_at': earliest.isoformat(), 'expired': bool(c.get('expiry_at') and timestamp(c['expiry_at']) <= when)})
        return sorted(rows, key=lambda r: (r['due_at'], r['candidate_id']))

    def settle_due(self, now):
        # Caller can submit an evidence-backed review/renewal first; elapsed bounds close.
        output = []
        for row in self.due(now):
            output.append(self.revise(row['candidate_id'], {'state': 'CLOSED', 'closure_reason': 'research bound reached; no justified renewal'},
                                      now, 'scheduled research bound', ['recorded review/expiry clock']))
        return output

    def source_read(self, read):
        read = clone(read)
        source = read['source_id']
        require(source in self._config['sources'] and read['run_id'] in self._config['authorized_runs'], 'outside authorized source/run')
        require(read['state'] in READ_STATES and nonempty(read.get('receipt')), 'read state/receipt required')
        require(timestamp(read['at']) >= timestamp(self._config['authorized_runs'][read['run_id']]), 'read precedes run')
        key = source + ':' + read['run_id']
        if key in self._reads:
            require(self._reads[key] == read, 'read receipt immutable; recovery needs new run')
            return {'status': 'DUPLICATE_READ'}
        src = self._config['sources'][source]
        earlier = [r for r in self._reads.values() if r['source_id'] == source]
        require(not earlier or timestamp(read['at']) >= max(timestamp(r['at']) for r in earlier), 'source read time reverses')
        if read['state'] == 'EXPECTED_NOT_OBSERVED':
            require(src['expected_runs'] is not None and read['run_id'] in src['expected_runs'], 'missing read requires verified expectation')
        messages = read.get('messages', [])
        require(isinstance(messages, list), 'messages must be a list')
        if read['state'] == 'SUCCESS_NO_NEW':
            require(not messages and read.get('watermark_after') == self._watermarks[source], 'no-new must preserve watermark')
        if read['state'] == 'SUCCESS_NEW':
            require(bool(messages) and nonempty(read.get('watermark_after')), 'new read requires delta and watermark')
        conclusion = read.get('no_signal_conclusion')
        if conclusion:
            require(read['state'] in ('SUCCESS_NEW', 'SUCCESS_NO_NEW') and conclusion.get('explicit') is True
                    and nonempty(conclusion.get('evidence')) and bool(conclusion.get('scope'))
                    and set(conclusion['scope']).issubset(src['universe']), 'no-signal needs successful scoped explicit conclusion')
        current = self._watermarks[source]
        continuity = read.get('watermark_before') == current and read.get('history_complete') is True
        success = read['state'] in ('SUCCESS_NEW', 'SUCCESS_NO_NEW')
        recovery = read.get('recovery')
        if recovery:
            require(success and continuity and recovery.get('from_watermark') == current
                    and bool(recovery.get('evidence')) and set(recovery.get('gap_read_keys', [])) ==
                    {r['read_key'] for r in self._gaps[source] if not r['resolved']}, 'recovery must cover every open gap')
        self._reads[key] = read
        if not success or not continuity:
            self._gaps[source].append({'read_key': key, 'resolved': False, 'state': read['state']})
            self._continuity[source] = False
        elif recovery:
            for gap in self._gaps[source]:
                if not gap['resolved']:
                    gap.update(resolved=True, recovery_read_key=key)
            self._continuity[source] = True
        if success and continuity and self._continuity[source]:
            self._watermarks[source] = read['watermark_after']
        self._append('SOURCE_READ', read)
        return {'status': read['state'], 'continuity_verified': self._continuity[source], 'confirmed_watermark': self._watermarks[source]}

    def work_order(self, run_id):
        require(run_id in self._config['authorized_runs'], 'cadence expansion refused')
        sources = self._config['sources']
        due_sources = [k for k, v in sources.items() if v['expected_runs'] is not None and run_id in v['expected_runs']]
        return ([{'kind': 'disclosure', 'source_id': k} for k in sorted(due_sources) if sources[k]['kind'] == 'disclosure'] +
                [{'kind': 'review', **x} for x in self.due(self._config['authorized_runs'][run_id])] +
                [{'kind': 'tape', 'source_id': k} for k in sorted(due_sources) if sources[k]['kind'] == 'tape'])

    def preview_alert(self, candidate_id, rule_id, qualification):
        c = self._candidates[candidate_id]
        rules = self._config.get('notification_rules', {})
        require(rule_id in rules, 'no existing authorized notification rule')
        rule = rules[rule_id]
        require(rule.get('authorized') is True and nonempty(rule.get('authorization_evidence')), 'rule authorization absent')
        require(qualification.get('trigger') == rule['trigger'] and qualification.get('trigger') != 'EVIDENCE_CHANGED', 'evidence revision alone cannot qualify')
        require(qualification.get('independently_qualified') is True and bool(qualification.get('evidence'))
                and nonempty(qualification.get('event_id')), 'independent qualification evidence required')
        require(c['universe_key'] in rule['universe'] and set(rule['universe']).issubset(self._config['universe']), 'notification scope mismatch')
        if rule['trigger'] == 'OVERDUE':
            require(any(r['candidate_id'] == candidate_id and r['due_at'] == qualification['event_id'] for r in self.due(qualification['at'])), 'overdue event does not match due record')
        require(rule['trigger'] in ('OVERDUE', 'SOURCE_FAILURE', 'EXISTING_QUALIFIED_ALERT'), 'unknown trigger')
        if rule['trigger'] == 'SOURCE_FAILURE':
            read = self._reads.get(qualification['event_id'])
            require(read and read['source_id'] == c['source_id'] and read['state'] in ('FAILED', 'PARTIAL', 'EXPECTED_NOT_OBSERVED'), 'source-failure evidence mismatch')
        key = digest([c['dependency_group_id'], rule_id, qualification['event_id']])
        if key in self._alerts:
            return {'status': 'DUPLICATE_SUPPRESSED', 'alert': None}
        alert = {'dedup_key': key, 'candidate_id': candidate_id, 'rule_id': rule_id, 'qualification': clone(qualification),
                 'text': self.summary(candidate_id), 'delivered': False, 'fixture_only': True}
        self._alerts[key] = alert
        self._append('ALERT_PREVIEW', alert)
        return {'status': 'QUALIFIED_PREVIEW_ONLY', 'alert': clone(alert)}

    def summary(self, candidate_id):
        c = self._candidates[candidate_id]
        ident = c['identity']
        name = 'unresolved listing' if ident is None else (' / '.join(identity_key(ident)[1:]))
        late = ' Late discovery is a captured claim; exact delay and cause remain unknown.' if c.get('late_status') == 'CAPTURED_LATE_CLAIM' else ''
        return (f"Research candidate {c['candidate_id']}: {name}. State: {c['state']}. "
                f"Owner: {c['owner']}. Next review: {c.get('next_review_at') or (c.get('event_trigger') or {}).get('review_deadline') or 'expiry only'}. "
                f"Research expiry: {c.get('expiry_at') or 'unset'}. "
                + ('No explicit forecast is recorded.' if c['explicit_forecast'] is None else 'An explicit statement is recorded; no cohort admission.')
                + ' No paper entry is authorized by this record.' + late)

    def export_research(self, candidate_id):
        c = self.candidate(candidate_id)
        c['record_type'] = 'research_candidate'
        c['radar_record_sha256'] = digest(self._candidates[candidate_id])
        return c

    def measurements(self):
        cs = list(self._candidates.values())
        delays = [delay(c['clocks']['first_verified_available_at'], c['clocks']['radar_first_discovered_at']) for c in cs]
        expected = None if any(s['expected_runs'] is None for s in self._config['sources'].values()) else sum(len(s['expected_runs']) for s in self._config['sources'].values())
        expected_keys = {source+':'+run for source, s in self._config['sources'].items() for run in (s['expected_runs'] or [])}
        successful = sum(k in expected_keys and r['state'] in ('SUCCESS_NEW', 'SUCCESS_NO_NEW') for k, r in self._reads.items())
        states = {s: sum(r['state'] == s for r in self._reads.values()) for s in READ_STATES}
        reviews = [e['body'] for e in self._events if e['kind'] == 'REVIEWED']
        capture = [delay(c['clocks']['alert_authored_at'], c['clocks']['collector_received_at']) for c in cs]
        delivery = [delay(c['clocks']['collector_received_at'], c['clocks']['delivered_at']) for c in cs]
        receipt_groups = {}
        for e in self._events:
            if e['kind'] == 'RECEIPT':
                b = e['body']
                group = receipt_groups.setdefault((b['candidate_id'], b['content_sha256']), {})
                group.setdefault(b['kind'], b['clock'])
        receipt_delivery = [delay(g.get('collector_received_at', unknown('no collector receipt')),
                                  g.get('delivered_at', unknown('no delivery receipt'))) for g in receipt_groups.values()]
        def distribution(values):
            return {'seconds': [v['seconds'] for v in values if v['seconds'] is not None],
                    'sample_size': sum(v['seconds'] is not None for v in values), 'unknown_count': sum(v['seconds'] is None for v in values)}
        return {'verified_expected_reads': expected, 'verified_expected_subset': len(expected_keys), 'reads_by_state': states,
                'expected_not_observed_count': len(expected_keys-set(self._reads)) + states['EXPECTED_NOT_OBSERVED'],
                'successful_expected_reads': successful, 'successful_read_share': successful/expected if expected else None,
                'full_coverage_established': False, 'source_continuity': clone(self._continuity),
                'source_availability': {s: {'successful_reads': sum(r['source_id'] == s and r['state'].startswith('SUCCESS') for r in self._reads.values()),
                                            'unknown_without_verified_schedule': v['expected_runs'] is None} for s, v in self._config['sources'].items()},
                'explicit_scoped_no_signal_count': sum(bool(r.get('no_signal_conclusion')) for r in self._reads.values()),
                'unique_candidates': len(cs), 'identity_resolved': sum(c['identity'] is not None for c in cs),
                'identity_resolved_share': sum(c['identity'] is not None for c in cs)/len(cs) if cs else None,
                'closures': sum(c['state'] == 'CLOSED' for c in cs), 'unresolved_blockers': {c['candidate_id']: c['missing_facts'] for c in cs if c['missing_facts']},
                'reviews': {'count': len(reviews), 'on_time': sum(r['on_time'] for r in reviews), 'on_time_share': sum(r['on_time'] for r in reviews)/len(reviews) if reviews else None},
                'capture_delays': distribution(capture), 'delivery_delays': distribution(delivery),
                'content_bound_receipt_delivery_delays': distribution(receipt_delivery),
                'discovery_delays': {'seconds': [x['seconds'] for x in delays if x['seconds'] is not None],
                                     'sample_size': sum(x['seconds'] is not None for x in delays), 'unknown_count': sum(x['seconds'] is None for x in delays)},
                'recall': None, 'recall_reason': 'independent point-in-time event denominator unavailable', 'forecast_accuracy': None}


def replay(document):
    radar = Radar(document['config'])
    results = []
    allowed = {'ingest', 'revise', 'review', 'add_receipt', 'source_read', 'settle_due', 'forecast', 'preview_alert', 'select_series', 'attribute_delay'}
    for op in document['operations']:
        require(op['method'] in allowed, 'unsupported replay operation')
        results.append(getattr(radar, op['method'])(**op['args']))
    return {'results': results, 'snapshot': radar.snapshot(), 'measurements': radar.measurements(),
            'summaries': [radar.summary(k) for k in radar._candidates]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixture', type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(json.loads(args.fixture.read_text())), indent=2, ensure_ascii=True, allow_nan=False))


if __name__ == '__main__':
    main()
