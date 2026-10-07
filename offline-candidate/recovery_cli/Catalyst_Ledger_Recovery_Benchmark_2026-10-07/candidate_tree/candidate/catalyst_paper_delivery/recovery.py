"""Isolated local paper-ledger caller. No external receipt or execution authority.

The existing ledger API/CLI can bypass this caller. Deployment/wiring is separate.
Journal records are immutable; a failed/incomplete record is never deleted here.
Requires a local filesystem supporting flock, exclusive creation and fsync.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger as L

FORMAT = 'LOCAL_PAPER_RECOVERY_V1'
HEX = re.compile(r'[0-9a-f]{64}\Z')
OP_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
RECORD = re.compile(r'([0-9]{6,})\.(intent|complete)\.json\Z')
MAX_INPUT = 32 * 1024 * 1024


class Blocked(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Blocked(message)


def _regular(path):
    info = Path(path).lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            'control/input must be a regular file with one link: ' + str(path))
    return info


def _directory(path):
    require(stat.S_ISDIR(Path(path).lstat().st_mode), 'directory missing or symlinked: ' + str(path))


def _read(path, limit=MAX_INPUT):
    path = Path(path)
    info = _regular(path)
    require(info.st_size <= limit, 'file exceeds caller input bound')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        opened = os.fstat(f.fileno())
        require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino), 'file changed while opening')
        raw = f.read(limit + 1)
    require(len(raw) <= limit, 'file exceeds caller input bound')
    return raw


def _sync(path):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), 'cannot sync nonregular/symlink path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino), 'sync path changed')
        os.fsync(fd)
    finally:
        os.close(fd)


def _sealed(data):
    return dict(data, record_sha256=L.sha(L.canonical(data)))


def _read_record(path):
    raw = _read(path, 16384)
    data = L.strict_json(raw)
    require(type(data) is dict and raw == L.canonical(data) + b'\n', 'noncanonical control record')
    checksum = data.get('record_sha256')
    body = {k: v for k, v in data.items() if k != 'record_sha256'}
    require(checksum == L.sha(L.canonical(body)), 'control checksum mismatch')
    return data


def _write_record(path, data):
    """Leave even partial records in place: interrupted control state fails closed."""
    raw = L.canonical(data) + b'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        require(f.write(raw) == len(raw), 'short control write')
        f.flush()
        os.fsync(f.fileno())
    require(_read(path, 16384) == raw, 'control readback mismatch')
    _sync(Path(path).parent)


def _identity():
    return {'format': FORMAT, 'caller_sha256': L.sha(Path(__file__).read_bytes()),
            'engine_sha256': L.source_manifest()['manifest_sha256']}


def _bootstrap(root):
    root.mkdir(mode=0o700)  # exclusive; never adopt an existing unjournaled chain
    fd = os.open(root / 'lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    (root / 'chain').mkdir(mode=0o700)
    (root / 'journal').mkdir(mode=0o700)
    identity = _sealed(dict(_identity(), root_id=uuid.uuid4().hex))
    _write_record(root / 'identity.json', identity)
    _sync(root / 'chain')
    _sync(root / 'journal')
    _sync(root)
    _sync(root.parent)


@contextmanager
def _locked(root):
    _directory(root)
    lock = root / 'lock'
    info = _regular(lock)
    require(info.st_size == 0, 'malformed permanent lock file')
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        require((info.st_dev, info.st_ino) == (opened.st_dev, opened.st_ino), 'lock file changed')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as ex:
            raise Blocked('another caller holds the lock') from ex
        yield
    finally:
        os.close(fd)  # also releases flock after exceptions; process death does too


def _load(root, *, fresh=False):
    require({p.name for p in root.iterdir()} == {'lock', 'identity.json', 'chain', 'journal'},
            'missing or unexpected caller controls')
    for name in ('chain', 'journal'):
        _directory(root / name)
    identity = _read_record(root / 'identity.json')
    require(set(identity) == {'format', 'caller_sha256', 'engine_sha256', 'root_id', 'record_sha256'},
            'identity fields')
    require(all(identity.get(k) == v for k, v in _identity().items()), 'caller/engine identity changed')
    require(type(identity['root_id']) is str and re.fullmatch('[0-9a-f]{32}', identity['root_id']), 'root identity')
    chain_paths = []
    for path in sorted((root / 'chain').iterdir()):
        # The ledger's published generation temporarily has a second hard link.
        require(stat.S_ISREG(path.lstat().st_mode), 'nonregular chain member')
        if re.fullmatch(r'generation-[0-9]{6,}\.json', path.name):
            chain_paths.append(path)
        else:
            require(re.fullmatch(r'\.ledger-[0-9]+-[0-9a-f]{32}\.tmp', path.name), 'unexpected chain member')
    cps = L.verify_chain_dir(root / 'chain') if chain_paths else []
    require(all(p.name == f'generation-{n:06d}.json' for n, p in enumerate(chain_paths)), 'chain filenames/gaps')
    slots = {}
    for path in sorted((root / 'journal').iterdir()):
        match = RECORD.fullmatch(path.name)
        require(match is not None, 'unknown journal member')
        n, kind = int(match[1]), match[2]
        require(path.name == f'{n:06d}.{kind}.json', 'noncanonical journal filename')
        slots.setdefault(n, {})[kind] = _read_record(path)
    require(sorted(slots) == list(range(len(slots))), 'journal sequence gap')
    intents, completions, seen = [], [], set()
    parent = None
    for n, slot in sorted(slots.items()):
        intent = slot.get('intent')
        require(intent is not None, 'completion without intent')
        require(set(intent) == {'format', 'caller_sha256', 'engine_sha256', 'root_id', 'sequence',
                               'operation_id', 'request_sha256', 'parent_sha256', 'result_sha256',
                               'result_bytes_sha256', 'kind', 'record_sha256'}, 'intent fields')
        require(all(intent.get(k) == identity[k] for k in ('format', 'caller_sha256', 'engine_sha256', 'root_id')),
                'intent identity')
        require(intent['kind'] == 'intent' and type(intent['sequence']) is int and intent['sequence'] == n,
                'intent sequence')
        require(type(intent['operation_id']) is str and OP_ID.fullmatch(intent['operation_id']), 'operation identity')
        require(intent['operation_id'] not in seen, 'duplicate operation identity')
        seen.add(intent['operation_id'])
        require(intent['parent_sha256'] == parent, 'intent parent mismatch')
        for key in ('request_sha256', 'result_sha256', 'result_bytes_sha256'):
            require(type(intent[key]) is str and HEX.fullmatch(intent[key]), 'intent digest')
        if n < len(cps):
            cp = cps[n]
            require(cp['checkpoint_sha256'] == intent['result_sha256'] and
                    L.sha(chain_paths[n].read_bytes()) == intent['result_bytes_sha256'], 'visible result differs from intent')
        complete = slot.get('complete')
        if complete is None:
            require(n == len(slots) - 1, 'operation after pending intent')
        else:
            require(set(complete) == {'kind', 'intent_sha256', 'result_sha256', 'basis', 'record_sha256'}, 'completion fields')
            require(complete['kind'] == 'complete' and complete['intent_sha256'] == intent['record_sha256'] and
                    complete['result_sha256'] == intent['result_sha256'] and
                    complete['basis'] in ('save_readback_resync', 'explicit_local_resync'), 'completion binding')
            require(n < len(cps), 'completed result missing')
        intents.append(intent)
        completions.append(complete)
        parent = intent['result_sha256']
    pending = bool(intents and completions[-1] is None)
    require(len(cps) <= len(intents), 'unexplained successor: raw caller bypass or missing journal')
    require(len(cps) >= len(intents) - int(pending), 'missing recorded generation')
    require(fresh or bool(intents), 'incomplete initialization; no durable intent')
    return {'identity': identity, 'cps': cps, 'intents': intents, 'completions': completions, 'pending': pending}


def _resync(root, state):
    # Ordering matters: generation durability precedes every completion record.
    for n in range(len(state['cps'])):
        _sync(root / 'chain' / f'generation-{n:06d}.json')
    _sync(root / 'chain')
    for path in sorted((root / 'journal').iterdir()):
        _sync(path)
    _sync(root / 'journal')
    _sync(root / 'identity.json')
    _sync(root / 'lock')
    _sync(root)
    _sync(root.parent)


def _complete(root, intent, basis):
    complete = _sealed({'kind': 'complete', 'intent_sha256': intent['record_sha256'],
                        'result_sha256': intent['result_sha256'], 'basis': basis})
    _write_record(root / 'journal' / f"{intent['sequence']:06d}.complete.json", complete)
    _sync(root)


def _ack(intent, head, *, duplicate=False):
    return {'status': 'LOCALLY_ACCEPTED', 'operation_id': intent['operation_id'],
            'result_sha256': intent['result_sha256'], 'current_head_sha256': head['checkpoint_sha256'],
            'duplicate': duplicate, 'executed_again': False, 'external_execution_approved': False}


def _submit(root, operation_id, request, prepare, *, initialize=False):
    root = Path(root).absolute()
    require(type(operation_id) is str and OP_ID.fullmatch(operation_id), 'invalid operation ID')
    request_hash = L.sha(L.canonical(request))
    fresh = initialize and not os.path.lexists(root)
    if fresh:
        _bootstrap(root)
    with _locked(root):
        state = _load(root, fresh=fresh)
        require(not state['pending'], 'pending operation: explicit reconciliation required; do not retry')
        for intent in state['intents']:
            if intent['operation_id'] == operation_id:
                require(intent['request_sha256'] == request_hash, 'operation ID reused with different request')
                _resync(root, state)
                return _ack(intent, state['cps'][-1], duplicate=True)
        require(not initialize or fresh, 'caller root already initialized')
        _resync(root, state)
        cp = prepare(state['cps'][-1] if state['cps'] else None)
        data = L.canonical(cp) + b'\n'
        L.verify(L.decode_generation(data))
        sequence = len(state['intents'])
        parent = state['cps'][-1]['checkpoint_sha256'] if state['cps'] else None
        require(cp['generation'] == sequence and cp['previous_generation_sha256'] == parent, 'prepared result parent')
        identity = {k: state['identity'][k] for k in ('format', 'caller_sha256', 'engine_sha256', 'root_id')}
        intent = _sealed(dict(identity, sequence=sequence, kind='intent', operation_id=operation_id,
                              request_sha256=request_hash, parent_sha256=parent,
                              result_sha256=cp['checkpoint_sha256'], result_bytes_sha256=L.sha(data)))
        _write_record(root / 'journal' / f'{sequence:06d}.intent.json', intent)
        # No publication before the preceding control write and directory sync succeed.
        L.save(cp, root / 'chain')
        state = _load(root)
        require(len(state['cps']) == sequence + 1, 'intended result is not visible')
        _resync(root, state)
        _complete(root, intent, 'save_readback_resync')
        return _ack(intent, cp)


def initialize(root, operation_id, source, expected, activation, versions_raw, approved, reviewed):
    raw = _read(source)
    require(L.sha(raw) == expected, 'source digest mismatch')
    versions = L.strict_json(versions_raw)
    request = {'command': 'init', 'source_sha256': expected, 'activation': activation,
               'versions_sha256': L.sha(versions_raw), 'approved': approved, 'reviewed': reviewed}
    def prepare(_):
        cp = L.initialize(source, expected, activation, versions, approved, reviewed)
        require(_read(source) == raw, 'source changed during initialization')
        return cp
    return _submit(root, operation_id, request, prepare, initialize=True)


def advance(root, operation_id, expected, library_id, operations_raw, receipt_raw=None):
    operations = L.operation_json(operations_raw)
    require(type(operations) is list and len(operations) == 1, 'one operation required')
    for op in operations:
        require(type(op) is dict and type(op.get('kwargs')) is dict, 'operation envelope')
        if op.get('method') == 'decide_entry':
            p = op['kwargs'].get('p')
            require(type(p) is dict and 'entry_timing' not in p, 'extended-entry timing disabled in recovery caller')
    receipt = L.strict_json(receipt_raw) if receipt_raw is not None else None
    request = {'command': 'advance', 'expected_parent': expected, 'library_id': library_id,
               'operations_sha256': L.sha(operations_raw),
               'receipt_sha256': L.sha(receipt_raw) if receipt_raw is not None else None}
    def prepare(cp):
        require(cp is not None, 'missing caller genesis')
        return L.advance(cp, operations, expected, library_id, receipt)
    return _submit(root, operation_id, request, prepare)


def reconcile(root, operation_id, expected_result):
    root = Path(root).absolute()
    with _locked(root):
        state = _load(root)
        require(state['intents'], 'no intent')
        intent = state['intents'][-1]
        require(intent['operation_id'] == operation_id and intent['result_sha256'] == expected_result,
                'reconciliation must name exact latest intent and result')
        require(len(state['cps']) == len(state['intents']), 'result absent: cannot clear or replay pending intent')
        _resync(root, state)
        if state['pending']:
            _complete(root, intent, 'explicit_local_resync')
        return _ack(intent, state['cps'][-1], duplicate=not state['pending'])


def _view(cp):
    if cp is None:
        return None
    return {'generation': cp['generation'], 'checkpoint_sha256': cp['checkpoint_sha256'],
            'cash': cp['metrics']['cash'], 'position_state': cp['metrics']['position_state'],
            'pending': cp['metrics']['pending']}


def status(root):
    root = Path(root).absolute()
    try:
        with _locked(root):
            state = _load(root)
            accepted = sum(c is not None for c in state['completions'])
            visible_pending = state['pending'] and len(state['cps']) == len(state['intents'])
            return {'status': ('RESULT_VISIBLE_PENDING' if visible_pending else 'PENDING') if state['pending'] else 'COMPLETION_RECORDED',
                    'chain_valid': bool(state['cps']), 'mutation_blocked': state['pending'],
                    'pending_operation_id': state['intents'][-1]['operation_id'] if state['pending'] else None,
                    'pending_result_sha256': state['intents'][-1]['result_sha256'] if state['pending'] else None,
                    'recorded_simulated_state': _view(state['cps'][-1] if state['cps'] else None),
                    'last_completed_local_state': _view(state['cps'][accepted - 1] if accepted else None),
                    'current_call_confirmed_durability': False,
                    'resync_required_before_mutation': True, 'external_execution_approved': False}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as ex:
        return {'status': 'BLOCKED_CONTROL', 'mutation_blocked': True,
                'chain_valid': None, 'reason': str(ex), 'external_execution_approved': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'advance', 'reconcile', 'status', 'verify'):
        p = sub.add_parser(name)
        p.add_argument('--root', required=True)
        if name in ('init', 'advance', 'reconcile'):
            p.add_argument('--operation-id', required=True)
        if name == 'init':
            p.add_argument('--source', required=True)
            p.add_argument('--expected-source-sha256', required=True)
            p.add_argument('--activation', required=True)
            p.add_argument('--source-versions', required=True)
            p.add_argument('--approved', action='store_true')
            p.add_argument('--reviewed', action='store_true')
        elif name == 'advance':
            p.add_argument('--expected-current-sha256', required=True)
            p.add_argument('--checkpoint-library-id', required=True)
            p.add_argument('--operations', required=True)
            p.add_argument('--publication-receipt')
        elif name == 'reconcile':
            p.add_argument('--expected-result-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            result = initialize(args.root, args.operation_id, args.source, args.expected_source_sha256,
                                args.activation, _read(args.source_versions), args.approved, args.reviewed)
        elif args.command == 'advance':
            result = advance(args.root, args.operation_id, args.expected_current_sha256,
                             args.checkpoint_library_id, _read(args.operations),
                             _read(args.publication_receipt) if args.publication_receipt else None)
        elif args.command == 'reconcile':
            result = reconcile(args.root, args.operation_id, args.expected_result_sha256)
        else:
            result = status(args.root)
        print(json.dumps(result, sort_keys=True))
        return 2 if result.get('mutation_blocked') else 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as ex:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(ex),
                          'mutation_blocked': True, 'external_execution_approved': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
