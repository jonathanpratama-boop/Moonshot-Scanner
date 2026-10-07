"""Offline integration fixture adapter; no market retrieval or operational enablement.

Admission arithmetic and policy belong exclusively to the pinned engine. A research
alert or forecast is never promoted into a manager dossier by this adapter.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recovery as R
import ledger as L
import catalyst as C

CONTRACT_PATH = Path(__file__).with_name('entry_contract.json')
CONTRACT = L.strict_json(CONTRACT_PATH.read_bytes())


def reason(code, field=None, detail=None):
    return {'code': code, **({'field': field} if field else {}), **({'detail': detail} if detail else {})}


def _missing(value, pointer, prefix=''):
    parts = pointer.strip('/').split('/')
    def walk(node, remaining, path):
        if not remaining:
            return [path] if node is None else []
        key, tail = remaining[0], remaining[1:]
        if key == '*':
            if not isinstance(node, list):
                return [path]
            return [p for n, item in enumerate(node) for p in walk(item, tail, path+'/'+str(n))]
        next_path = path+'/'+key
        if not isinstance(node, dict) or key not in node or node[key] is None:
            return [next_path + (('/'+'/'.join(tail)) if tail else '')]
        return walk(node[key], tail, next_path)
    return walk(value, parts, prefix)


def _base(record=None):
    synthetic = isinstance(record, dict) and record.get('synthetic_fixture') is True
    return {'schema': CONTRACT['schema'], 'environment': 'OFFLINE_SYNTHETIC' if synthetic else 'READ_ONLY_RESEARCH',
            'candidate_id': record.get('candidate_id') if isinstance(record, dict) else None,
            'decision': None, 'execution_action': None, 'paper_fill_accepted': False,
            'operational_entry_enabled': False, 'external_execution_approved': False,
            'forecast_required_for_entry': False, 'forecast_admission_performed': False,
            'source_truth_verified_by_software': False, 'reasons': []}


def assess(record):
    """Presence/provenance diagnostics only; never calculate or invent eligibility."""
    out = _base(record)
    if not isinstance(record, dict):
        return out | {'status': 'BLOCKED_INPUT', 'decision': 'PASS',
                      'reasons': [reason('INPUT_SHAPE', '/', 'candidate bundle must be an object')]}
    reasons = []
    for key in ('candidate_id', 'research_record', 'decision_request'):
        if record.get(key) is None:
            reasons.append(reason('INPUT_MISSING', '/'+key))
    if record.get('candidate_id') is not None and (not isinstance(record['candidate_id'], str) or not record['candidate_id'].strip()):
        reasons.append(reason('INPUT_SHAPE', '/candidate_id', 'nonempty string required'))
    request = record.get('decision_request')
    if request is not None:
        if not isinstance(request, dict):
            reasons.append(reason('INPUT_SHAPE', '/decision_request', 'object required'))
        else:
            for key in ('action', 'now'):
                if request.get(key) is None:
                    reasons.append(reason('INPUT_MISSING', '/decision_request/'+key))
    research = record.get('research_record')
    if research is not None and (not isinstance(research, dict) or research.get('record_type') != 'research_candidate'):
        reasons.append(reason('INPUT_SHAPE', '/research_record/record_type', 'research_candidate required'))
    manager = record.get('manager_dossier')
    p = manager.get('plan') if isinstance(manager, dict) else None
    if not isinstance(manager, dict) or manager.get('record_type') != 'manager_dossier':
        reasons.append(reason('MANAGER_DOSSIER_REQUIRED', '/manager_dossier',
                              'Research and optional forecast records are not manager-authored plans.'))
    else:
        for key in CONTRACT['manager_required']:
            if manager.get(key) is None or manager.get(key) == '':
                reasons.append(reason('INPUT_MISSING', '/manager_dossier/'+key))
        for key in ('author_id', 'authored_at', 'authorship_evidence'):
            if manager.get(key) is not None and (not isinstance(manager[key], str) or not manager[key].strip()):
                reasons.append(reason('INPUT_SHAPE', '/manager_dossier/'+key, 'nonempty declared provenance string required'))
        if manager.get('candidate_id') != record.get('candidate_id'):
            reasons.append(reason('PLAN_BINDING_MISMATCH', '/manager_dossier/candidate_id'))
    missing = sorted({path for pointer in CONTRACT['required_plan_paths']
                      for path in _missing(p, pointer, '/manager_dossier/plan')})
    if isinstance(p, dict):
        if p.get('asset_class') != 'crypto':
            missing += [path for pointer in CONTRACT['conditional_plan_paths'][0]['paths']
                        for path in _missing(p, pointer, '/manager_dossier/plan')]
        request = record.get('decision_request') if isinstance(record.get('decision_request'), dict) else {}
        try:
            if C.ts(p['deadline']).date() > C.ts(request['now']).date() and p.get('overnight_approved') is None:
                missing.append('/manager_dossier/plan/overnight_approved')
            for n, event in enumerate(p.get('events', [])):
                if C.ts(event['at']) <= C.ts(p['deadline']):
                    for key in ('failure_first_exit_bid', 'first_exit_source', 'first_exit_at'):
                        if event.get(key) is None:
                            missing.append(f'/manager_dossier/plan/events/{n}/{key}')
        except (KeyError, TypeError, ValueError):
            pass  # malformed values are rejected by the exact frozen validator later
        if isinstance(manager, dict) and not missing:
            if manager.get('plan_sha256') != C.digest(p):
                reasons.append(reason('PLAN_BINDING_MISMATCH', '/manager_dossier/plan_sha256'))
    reasons += [reason('INPUT_MISSING', path) for path in sorted(set(missing))]
    out.update(status='BLOCKED_INPUT' if reasons else 'DOSSIER_PRESENT_PENDING_FROZEN_VALIDATION',
               decision='PASS' if reasons else None, reasons=reasons,
               economic_eligibility='not_evaluated', execution_quote_requested=False,
               execution_preview_requested=False, provider_access_failure_inferred=False)
    out['plain_english'] = ('PASS: the record does not supply a complete manager-authored paper plan. '
                            'No target, stop, forecast or execution quote has been invented.' if reasons else
                            'The manager dossier is present. Frozen validation and a later execution quote are still required; no paper entry exists yet.')
    return out


def _fixture(record):
    R.require(isinstance(record, dict) and record.get('synthetic_fixture') is True,
              'SYNTHETIC_FIXTURE_ONLY: this adapter is not enabled for operational records')


def _provenance(record):
    return {'candidate_id': record['candidate_id'], 'candidate_record_sha256': L.sha(L.canonical(record)),
            'bridge_sha256': L.sha(Path(__file__).read_bytes()),
            'contract_sha256': L.sha(CONTRACT_PATH.read_bytes()),
            'manager_dossier_sha256': L.sha(L.canonical(record.get('manager_dossier')))}


def _result_checkpoint(root, expected):
    matches = [cp for cp in L.verify_chain_dir(Path(root)/'chain') if cp['checkpoint_sha256'] == expected]
    R.require(len(matches) == 1, 'acknowledged result not found exactly once')
    return matches[0]


def _call(root, operation_id, expected, library_id, raw, receipt=None, base=None):
    out = dict(base or _base({'synthetic_fixture': True}))
    try:
        acknowledgement = R.advance(root, operation_id, expected, library_id, raw, receipt)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as ex:
        state = R.status(root)
        pending = state['status'] in ('PENDING', 'RESULT_VISIBLE_PENDING')
        visible = state.get('recorded_simulated_state')
        changed = visible is not None and visible['checkpoint_sha256'] != expected
        uncertain = state['mutation_blocked'] or changed
        out.update(status='RECOVERY_BLOCKED' if uncertain else 'VALIDATOR_REFUSED',
                   decision=None if uncertain else 'PASS', recovery=state,
                   reasons=[reason('CALLER_PENDING' if pending else 'CALLER_BLOCKED' if state['mutation_blocked']
                                   or changed else 'FROZEN_VALIDATOR_REJECTED', detail=str(ex))],
                   plain_english='Paper action is not acknowledged. Preserve the recorded state and resolve the stated block; do not retry a trade blindly.')
        return out
    out['acknowledgement'] = acknowledgement
    if acknowledgement['duplicate']:
        return out | {'status': 'DUPLICATE_SUPPRESSED', 'decision': None, 'execution_action': None,
                      'paper_fill_accepted': False, 'reasons': [reason('DUPLICATE_OPERATION_SUPPRESSED')],
                      'plain_english': 'This operation was already recorded. No new paper trade or alert is produced.'}
    cp = _result_checkpoint(root, acknowledgement['result_sha256'])
    sim = L.verify(cp)
    event = sim.events[-1] if sim.events else None
    out['checkpoint_sha256'] = cp['checkpoint_sha256']
    out['generation'] = cp['generation']
    out['event'] = event
    method = L.operation_json(raw)[0]['method']
    if method == 'record_review':
        review = cp['research_reviews'][-1]
        action = review['type']
        out.update(status='REVIEW_RECORDED', decision=action, review=review,
                   plain_english=f"{action}: {review['details']['reason']}")
    elif event and event['type'] == 'ENTRY_DECISION':
        out.update(status='ENTRY_DECISION_COMMITTED', decision=None,
                   plan_sha256=event['details']['plan_hash'],
                   plain_english='A paper entry decision is recorded. No fill is accepted yet; a new postdecision quote, required publication receipt and final-size preview must pass the frozen gates.')
    elif event and event['type'] in ('BUY', 'SELL'):
        fill = cp['metrics']['fills'][-1]
        buy = event['type'] == 'BUY'
        out.update(status='PAPER_FILL_ACCEPTED', decision='ENTER PAPER' if buy else None,
                   execution_action='BUY PAPER' if buy else 'EXIT PAPER', paper_fill_accepted=True,
                   fill=fill, plain_english=(f"ENTER PAPER: synthetic purchase of {fill['units']} units of {fill['instrument']} recorded."
                                            if buy else f"EXIT PAPER: the entire synthetic position in {fill['instrument']} was sold."))
    elif event and event['type'] in ('FILL_GATE_FAILED', 'EXPIRED_UNFILLED'):
        out.update(status='ENTRY_CANCELLED', decision='PASS',
                   reasons=[reason('FROZEN_FILL_GATE_FAILED', detail=event['details'].get('reason', event['type']))],
                   plain_english='PASS: the paper entry decision is permanently cancelled. A different quote cannot fill this decision.')
    elif event and event['type'] == 'REJECTED_QUOTE':
        out.update(status='ENTRY_PENDING_DATA' if sim.pending and sim.pending['kind'] == 'ENTRY' else 'EXIT_PENDING_DATA',
                   reasons=[reason('FROZEN_QUOTE_REJECTED', detail=event['details']['reason'])],
                   plain_english='The quote was rejected by the frozen validator. No paper fill occurred; this is not a validated WAIT decision.')
    elif event and event['type'] == 'EXIT_PENDING_DATA':
        out.update(status='EXIT_PENDING_DATA', reasons=[reason('FROZEN_EXIT_PENDING', detail=event['details']['reason'])],
                   plain_english='The whole paper position remains open pending usable exit evidence. Cash has not been released.')
    else:
        out.update(status='OBSERVATION_RECORDED', pending=sim.pending['kind'] if sim.pending else None,
                   plain_english='The paper observation is recorded. Any required exit remains pending until an accepted whole-position sale.')
    return out


def decide(root, operation_id, expected, library_id, record):
    _fixture(record)
    out = _base(record)
    request = record.get('decision_request')
    R.require(isinstance(request, dict) and request.get('action') in ('ENTER PAPER', 'WAIT', 'PASS'), 'decision action required')
    action, now = request['action'], request.get('now')
    provenance = _provenance(record)
    out['bindings'] = provenance
    if action == 'PASS':
        R.require(isinstance(request.get('reason'), str) and request['reason'].strip(), 'PASS reason required')
        kwargs = {'at': now, 'action': 'PASS', 'evidence': provenance, 'reason': request['reason']}
        operation = {'method': 'record_review', 'kwargs': kwargs, 'bridge_provenance': provenance}
    else:
        diagnostic = assess(record)
        if diagnostic['reasons']:
            return diagnostic | {'bindings': provenance}
        manager = record['manager_dossier']
        try:
            R.require(C.ts(manager['authored_at']) <= C.ts(now), 'manager authored time is after decision')
        except (ValueError, TypeError) as ex:
            return out | {'status': 'BLOCKED_INPUT', 'decision': 'PASS',
                          'reasons': [reason('MANAGER_PROVENANCE_INVALID', '/manager_dossier/authored_at', str(ex))]}
        p = manager['plan']
        if action == 'ENTER PAPER':
            operation = {'method': 'decide_entry', 'kwargs': {'p': p, 'now': now}, 'bridge_provenance': provenance}
        else:
            cp = _result_checkpoint(root, expected)
            sim = L.verify(cp)
            wait = {'p': p, **{k: request.get(k) for k in CONTRACT['wait_inputs']}}
            evaluated = C.wait_decision(cash=sim.cash, now=now, intake_end=sim.intake_end, **wait)
            kwargs = {'at': now, 'action': evaluated['action'], 'evidence': provenance,
                      'reason': request.get('condition') if evaluated['action'] == 'WAIT' else evaluated['reason']}
            if evaluated['action'] == 'WAIT':
                kwargs['wait_inputs'] = wait
            else:
                out['reasons'] = [reason('FROZEN_WAIT_REJECTED', detail=evaluated['reason'])]
            operation = {'method': 'record_review', 'kwargs': kwargs, 'bridge_provenance': provenance}
    return _call(root, operation_id, expected, library_id, L.canonical([operation]), base=out)


def forward(root, operation_id, expected, library_id, operations_raw, receipt_raw=None, *, synthetic_fixture=False, method='execute'):
    _fixture({'synthetic_fixture': synthetic_fixture})
    operations = L.operation_json(operations_raw)
    R.require(method in ('execute', 'observe') and isinstance(operations, list) and len(operations) == 1
              and operations[0].get('method') == method, 'single exact execution/observation operation required')
    # Preserve original bytes and the existing quote-metadata parser; never prevalidate
    # a preview and thereby evade first-valid-quote cancellation.
    return _call(root, operation_id, expected, library_id, operations_raw, receipt_raw)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('assess');a.add_argument('--record', required=True)
    for name in ('decide', 'execute', 'observe'):
        a = sub.add_parser(name)
        a.add_argument('--root', required=True);a.add_argument('--operation-id', required=True)
        a.add_argument('--expected-current-sha256', required=True);a.add_argument('--checkpoint-library-id', required=True)
        if name == 'decide':
            a.add_argument('--record', required=True)
        else:
            a.add_argument('--operations', required=True);a.add_argument('--synthetic-fixture', action='store_true')
            if name == 'execute':a.add_argument('--publication-receipt')
    args = p.parse_args(argv)
    try:
        if args.command == 'assess':
            result = assess(L.strict_json(R._read(args.record)))
        elif args.command == 'decide':
            result = decide(args.root, args.operation_id, args.expected_current_sha256,
                            args.checkpoint_library_id, L.strict_json(R._read(args.record)))
        else:
            receipt = R._read(args.publication_receipt) if getattr(args, 'publication_receipt', None) else None
            result = forward(args.root, args.operation_id, args.expected_current_sha256, args.checkpoint_library_id,
                             R._read(args.operations), receipt, synthetic_fixture=args.synthetic_fixture, method=args.command)
        print(json.dumps(result, sort_keys=True))
        return 2 if result['status'] in ('BLOCKED_INPUT', 'RECOVERY_BLOCKED', 'VALIDATOR_REFUSED') else 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as ex:
        print(json.dumps(_base() | {'status': 'BLOCKED_INPUT', 'reasons': [reason('INPUT_SHAPE', detail=str(ex))]}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
