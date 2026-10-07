"""Append-only paper checkpoint adapter. No network, broker or scheduler access."""
import argparse, base64, hashlib, json, sys, os, uuid, errno
from copy import deepcopy
from pathlib import Path
from datetime import timedelta
CORE = Path(__file__).resolve().parent.parent / 'catalyst_paper_v2'
sys.path.insert(0, str(CORE))
from catalyst import Simulator, cutover_check, export_snapshot, restore_snapshot, ts, iso, D, charges, wait_decision, entry_timing, log_details
from accounting import append_event, verify_chain

def ensure(ok, message):
    if not ok:
        raise ValueError(message)

class PublicationUncertain(OSError):
    """A complete generation is visible, but its directory sync was not confirmed.

    The path must stay in the chain: another writer may already depend on it.
    Readback can reconcile visible state; it does not prove power-loss durability.
    """
    def __init__(self, path, checkpoint_sha256, cause):
        self.path = Path(path)
        self.checkpoint_sha256 = checkpoint_sha256
        self.published = True
        self.durability_confirmed = False
        super().__init__(cause.errno or errno.EIO,
                         'generation published; durability unconfirmed; retain and reconcile before retrying',
                         str(self.path))

def bounded_copy(x, depth=0, memo=None, active=None):
    # Untrusted operation input: subtrees beyond the engine's audit depth become the sanitizer marker.
    if depth > 64:
        return {'unloggable_input': 'nesting depth exceeded'}
    if memo is None:
        memo = {}
    if active is None:
        active = set()
    identity = id(x)
    if identity in active:
        return {'unloggable_input': 'cyclic input'}
    container = isinstance(x, (dict, list, tuple))
    # A copy truncated on a deep path must not replace a later shallow alias.
    # At most 65 depth contexts per identity can be visited.
    key = (identity, depth) if container else identity
    if key in memo:
        return memo[key]
    if container:
        active.add(identity)
        try:
            if isinstance(x, dict):
                copied = {k: bounded_copy(v, depth + 1, memo, active) for k, v in x.items()}
            else:
                copied = [bounded_copy(v, depth + 1, memo, active) for v in x]
                if isinstance(x, tuple):
                    copied = tuple(copied)
            memo[key] = copied
            return copied
        finally:
            active.remove(identity)
    try:
        return deepcopy(x, memo)
    except RecursionError:
        return {'unloggable_input': 'nesting depth exceeded'}

def strict_json(raw):
    # UTF-8 only (C-08); duplicate keys are malformed evidence (F9.v1, C-14, B-03).
    def pairs(items):
        ensure(len({k for k, _ in items}) == len(items), 'duplicate JSON key')
        return dict(items)
    return json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw, object_pairs_hook=pairs)

def operation_json(raw):
    # Preserve protective processing of malformed quote/status evidence, without
    # silently choosing one of two decisive values. Plans and operation envelopes
    # remain strict; receipts, versions and generations still use strict_json.
    class ObjectPairs(list):
        pass
    raw = raw.encode('utf-8') if isinstance(raw, str) else raw
    parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=ObjectPairs)
    raw_hash = sha(raw)
    def convert(node, path=()):
        # The operation itself starts at path depth one, matching bounded_copy's
        # depth zero. Do not traverse metadata that bounded_copy would discard.
        if len(path) > 65:
            return {'unloggable_input': 'nesting depth exceeded'}
        if isinstance(node, ObjectPairs):
            counts = {}
            for key, _ in node:
                counts[key] = counts.get(key, 0) + 1
            duplicates = {key for key, count in counts.items() if count > 1}
            evidence = len(path) >= 3 and path[1] == 'kwargs' and path[2] in ('q', 'checkpoint_status')
            ensure(not duplicates or evidence, 'duplicate JSON key')
            return {key: ({'malformed_json_evidence': {'reason': 'duplicate JSON key', 'key': key,
                                                     'raw_sha256': raw_hash}}
                          if key in duplicates else convert(value, path + (key,)))
                    for key, value in node}
        if isinstance(node, list):
            return [convert(value, path + (index,)) for index, value in enumerate(node)]
        return node
    return convert(parsed)

def decode_generation(raw):
    # Exact-byte readback: the stored bytes must be the canonical checkpoint (B-03).
    cp = strict_json(raw)
    ensure(raw == canonical(cp) + b'\n', 'generation bytes are not the exact canonical checkpoint')
    return cp

def read_generation(path):
    return decode_generation(Path(path).read_bytes())

def sha(b):
    return hashlib.sha256(b).hexdigest()

def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()

def source_manifest(core=CORE):
    files = {}
    for name in ['catalyst_paper_v2/catalyst.py', 'catalyst_paper_v2/accounting.py', 'catalyst_paper_delivery/ledger.py', 'catalyst_paper_delivery/build_workbook.mjs']:
        b = (CORE.parent / name).read_bytes()
        files[name] = {'sha256': sha(b), 'bytes': len(b), 'utf8': b.decode()}
    return {'format': 'EXACT_SOURCE_UTF8_V1', 'files': files, 'manifest_sha256': sha(canonical(files))}

def verify_sources(m):
    ensure(set(m['files'])=={'catalyst_paper_v2/catalyst.py','catalyst_paper_v2/accounting.py','catalyst_paper_delivery/ledger.py','catalyst_paper_delivery/build_workbook.mjs'},'incomplete source manifest')
    ensure(sha(canonical(m['files'])) == m['manifest_sha256'], 'manifest hash')
    for name, x in m['files'].items():
        ensure(name in ['catalyst_paper_v2/catalyst.py', 'catalyst_paper_v2/accounting.py', 'catalyst_paper_delivery/ledger.py', 'catalyst_paper_delivery/build_workbook.mjs'] and sha(x['utf8'].encode()) == x['sha256'], 'embedded source hash')
        ensure(sha((CORE.parent / name).read_bytes()) == x['sha256'], 'running engine differs from frozen checkpoint')

def seal(cp):
    return sha(canonical({k: v for k, v in cp.items() if k != 'checkpoint_sha256'}))

def verify(cp):
    ensure(cp.get('schema') == 'CATALYST_PAPER_CHECKPOINT_V1', 'unsupported checkpoint schema')
    ensure(seal(cp) == cp['checkpoint_sha256'], 'checkpoint seal')
    verify_sources(cp['implementation'])
    ensure(sha(cp['protocol_document']['utf8'].encode())==cp['protocol_document']['sha256'],'protocol text hash')
    raw = base64.b64decode(cp['snapshot_base64'])
    ensure(sha(raw) == cp['snapshot_sha256'], 'invariant failed')
    sim = restore_snapshot(raw, cp['snapshot_sha256'])
    old = base64.b64decode(cp['previous_cohort']['checkpoint_base64'])
    ensure(sha(old) == cp['previous_cohort']['checkpoint_sha256'], 'previous cohort changed')
    ensure(sim.activation == cp['activation_at_utc'] and str(sim.baseline) == cp['opening_cash_usd'], 'invariant failed')
    ensure(sim.intake_end == cp['intake_ends_at_utc'] and cp['max_position_days'] == 30, 'intake/horizon metadata')
    ensure(ts(cp['as_of_utc'])>=sim.clock,'checkpoint clock')
    hist=cp.get('equity_history',[]);ensure(len(hist)==cp['generation'],'generation history count')
    for i,row in enumerate(hist):
        ensure(row['generation']==i and row['previous_generation_sha256']==(hist[i-1]['checkpoint_sha256'] if i else None),'history chain')
    ensure(cp['previous_generation_sha256']==(hist[-1]['checkpoint_sha256'] if hist else None),'history head')
    verify_chain(cp.get('research_reviews', []))
    ensure(cp['metrics'] == metrics(sim, cp['as_of_utc'], cp.get('research_reviews', [])), 'derived metrics mismatch')
    return sim

def metrics(sim, as_of=None, reviews=None):
    clock=ts(as_of) if as_of else sim.clock
    pos = sim.position
    val = None if pos else D(0)
    if pos and pos['last_value'] is not None and (timedelta(0) <= clock - ts(pos['last_value_at']) <= timedelta(seconds=120)) and (not pos.get('corporate_block')):
        val = pos['last_value']
    eq = None if val is None else sim.cash + val
    fees = D(0)
    slip = D(0)
    units = D(0)
    fills = []
    decisions = [{'id':'R'+e['id'],'at':e['at'],'type':e['type'],'details':json.dumps(e['details'],sort_keys=True)} for e in (reviews or [])]
    plans = []
    coverage = []
    trades = []
    active_plan = None
    active_entry = None
    active_basis = None
    for e in sim.events:
        d = e['details']
        kind = e['type']
        if kind == 'ENTRY_DECISION':
            p = d['plan']
            active_plan = p
            plans.append({'decision_id': e['id'], 'at': e['at'], 'instrument': p['instrument'], 'asset_class': p['asset_class'], 'deadline': p['deadline'], 'signal_expiry': p['signal_expiry'], 'target': p['target'], 'stop': p['stop'], 'target_method': p['dossier']['target_derivation']['method'], 'catalyst_source': p['dossier']['primary_source'], 'horizon_reason': p['dossier']['horizon_reason'], 'plan_hash': d['plan_hash'], 'checks_required': len(p['checks'])})
        if 'DECISION' in kind or kind in ['WAIT', 'PASS', 'TIGHTEN', 'EXPIRED_UNFILLED', 'FILL_GATE_FAILED']:
            decisions.append({'id': e['id'], 'at': e['at'], 'type': kind, 'details': json.dumps(d, sort_keys=True)})
        if kind == 'BUY':
            ec = d['economics']
            active_entry = e['at']
            active_basis = D(ec['debit'])
            units = D(ec['units'])
            notional = units * D(ec['price'])
            fee = D(ec['debit']) - notional
            slippage = units * D(d['quote']['ask']) * D('.001')
            fees += fee
            slip += slippage
            fills.append({'id': e['id'], 'at': e['at'], 'side': 'BUY', 'instrument': d['quote']['instrument'], 'units': str(units), 'quote': d['quote']['ask'], 'modeled_price': ec['price'], 'fees': str(fee), 'slippage': str(slippage), 'cash_change': str(-D(ec['debit'])), 'realized_pnl': None, 'quote_id': d['quote']['id']})
        if kind == 'SELL':
            price = D(d['quote']['bid']) * D('.999')
            gross = units * price
            fee = gross - D(d['net'])
            slippage = units * D(d['quote']['bid']) * D('.001')
            fees += fee
            slip += slippage
            fills.append({'id': e['id'], 'at': e['at'], 'side': 'SELL', 'instrument': d['quote']['instrument'], 'units': str(units), 'quote': d['quote']['bid'], 'modeled_price': str(price), 'fees': str(fee), 'slippage': str(slippage), 'cash_change': d['net'], 'realized_pnl': d['pnl'], 'quote_id': d['quote']['id']})
            trades.append({'instrument': d['quote']['instrument'], 'entry': active_entry, 'exit': e['at'], 'net_pnl': d['pnl'], 'entry_basis': str(active_basis), 'net_return': str(D(d['pnl'])/active_basis), 'asset_class': active_plan['asset_class'], 'target_method': active_plan['dossier']['target_derivation']['method'], 'catalyst_source': active_plan['dossier']['primary_source'], 'planned_horizon_days': (ts(active_plan['deadline'])-ts(active_entry)).total_seconds()/86400, 'exit_reason': json.dumps(d['reason'])})
            units = D(0)
        if kind == 'OBSERVATION':
            coverage.append({'id': e['id'], 'at': e['at'], 'missed': len(d['missed']), 'late_current_check': d['late_current_check'], 'mark_at': d['mark_at'], 'mark_error': d['mark_error'], 'reasons': json.dumps(d['reasons'])})
    return dict(cash=str(sim.cash), baseline=str(sim.baseline), realized=str(sim.realized), unrealized=None if val is None else str(val - pos['basis'] if pos else 0), net_equity=None if eq is None else str(eq), fees=str(fees), slippage=str(slip), position_state=pos['state'] if pos else 'CASH_ONLY', pending=sim.pending['kind'] if sim.pending else None, suspended=sim.suspended, mark_at=pos['last_value_at'] if pos else None, trades=trades, fills=fills, decisions=decisions, plans=plans, coverage=coverage, events=sim.events)

def equity_record(cp):
    return {'generation':cp['generation'],'checkpoint_sha256':cp['checkpoint_sha256'],'previous_generation_sha256':cp['previous_generation_sha256'],'as_of_utc':cp['as_of_utc'],**{k:v for k,v in cp['metrics'].items() if k not in ['fills','decisions','plans','coverage','events','trades']}}

def pack(sim, meta, previous=None, as_of=None):
    cp = deepcopy(meta)
    raw = export_snapshot(sim)
    cp.update(schema='CATALYST_PAPER_CHECKPOINT_V1', generation=0 if previous is None else previous['generation'] + 1, previous_generation_sha256=None if previous is None else previous['checkpoint_sha256'], snapshot_base64=base64.b64encode(raw).decode(), snapshot_sha256=sha(raw), as_of_utc=as_of or iso(sim.clock), metrics=metrics(sim,as_of,cp.get('research_reviews', [])))
    cp['equity_history'] = deepcopy(previous.get('equity_history',[])) + [equity_record(previous)] if previous else []
    cp['checkpoint_sha256'] = seal(cp)
    verify(cp)
    return cp

def save(cp, out):
    verify(cp)
    # Apply the same representation rules as readback before touching the output.
    # Python-only metadata (e.g. numeric dict keys) can change on a JSON round trip.
    data = canonical(cp) + b'\n'
    verify(decode_generation(data))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"generation-{cp['generation']:06d}.json"
    return atomic_write_once(data, path, checkpoint_sha256=cp['checkpoint_sha256'])

def atomic_write_once(data, path, *, checkpoint_sha256=None):
    path = Path(path)
    tmp = path.parent / f".ledger-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    created = False
    linked = False
    try:
        with tmp.open('xb') as f:
            created = True
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if checkpoint_sha256 is not None and tmp.read_bytes() != data:
            raise OSError(errno.EIO, 'checkpoint temporary-file readback mismatch', str(tmp))
        os.link(tmp, path)  # write-once: refuses every existing path, including symlinks
        linked = True
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as ex:
        if linked:
            if checkpoint_sha256 is not None:
                # Publication is already visible. Retraction can orphan a successor
                # that committed while this writer was synchronizing the directory.
                raise PublicationUncertain(path, checkpoint_sha256, ex) from ex
            path.unlink()  # standalone exports retain their pre-existing rollback contract
        raise
    finally:
        if created:
            try:
                tmp.unlink()
            except OSError:
                # An owned leftover cannot mask the original error or turn an
                # already synced publication into an ambiguous failed commit.
                pass
    return path

def initialize(source, expected, activation, versions, approved, reviewed):
    raw = Path(source).read_bytes()
    cash = cutover_check(raw, expected, approved, reviewed)
    old = json.loads(raw)
    ensure(ts(activation) >= ts(old['state']['last_decision_at_utc']), 'cutover precedes source state')
    ensure(versions['checkpoint']['version'] > 0 and versions['workbook']['version'] > 0, 'invariant failed')
    ensure(versions['checkpoint']['id'] and versions['workbook']['id'] and (type(versions['checkpoint']['version']) is int) and (type(versions['workbook']['version']) is int), 'invariant failed')
    sim = Simulator(cash, activation)
    meta = {'cohort_id': 'PAPER-CATALYST-' + activation.replace('-', '').replace(':', '').replace('.', '') + '-v2', 'mode': 'PAPER_ONLY', 'checkpoint_library_id': None, 'research_reviews': [], 'activation_at_utc': activation, 'intake_ends_at_utc': sim.intake_end, 'max_position_days': 30, 'opening_cash_usd': str(cash), 'cash_benchmark': 'FLAT_ZERO_INTEREST', 'implementation': source_manifest(), 'protocol_document': {'utf8': (CORE/'README.md').read_text(encoding='utf-8'), 'sha256': sha((CORE/'README.md').read_bytes())}, 'previous_cohort': {'cohort_id': old['cohort_id'], 'excluded_from_new_performance': True, 'checkpoint_sha256': sha(raw), 'checkpoint_base64': base64.b64encode(raw).decode(), 'library_versions': versions, 'closing_cash_transferred_once': str(cash)}, 'status': 'PAPER_COHORT_INITIALIZED', 'notes': ['Prior cohorts remain immutable and excluded from this performance series.', 'Actual positions remain pending or censored until supported reconciliation; no fabricated liquidation.', 'Observed sampled drawdown is not continuous-market drawdown.']}
    return pack(sim, meta)

def advance(cp, operations, expected_current_sha256, checkpoint_library_id, receipt=None):
    ensure(cp['checkpoint_sha256'] == expected_current_sha256, 'stale writer')
    ensure(len(operations) == 1, 'one operation per committed generation required')
    ensure(checkpoint_library_id and (cp['checkpoint_library_id'] is None or checkpoint_library_id == cp['checkpoint_library_id']), 'checkpoint Library identity changed')
    sim = verify(cp)
    op=bounded_copy(operations[0])
    ensure(receipt is None or op['method']=='execute','receipt only allowed for execution')
    anchor=None
    rejection=None
    timing_failure=False
    if op['method'] in ('execute','expire_entry'):
        ensure(sim.pending is not None,'execute requires prior committed decision')
        now=op['kwargs']['now']
        ensure(ts(now)>=ts(cp['as_of_utc']),'regressing operation checkpoint clock')
        expired=sim.pending['kind']=='ENTRY' and ts(now)>=min(ts(sim.pending['plan']['signal_expiry']),ts(sim.intake_end))
        if op['method']=='expire_entry' or expired:
            ensure(expired,'expiry requires expired pending ENTRY')
            ensure(receipt is None,'expiry-only cancellation does not accept a publication receipt')
            ensure(not any(op['kwargs'].get(k) is not None for k in ('preview','exit_fee')),'expiry-only cancellation does not accept fill evidence')
            op={'method':'expire_entry','kwargs':{'now':now}}
        else:
            ensure(receipt and receipt['checkpoint_sha256']==cp['checkpoint_sha256'] and receipt['readback_verified'] is True,'published decision readback receipt required')
            ensure(receipt['checkpoint_library_id']==checkpoint_library_id and type(receipt['checkpoint_library_version']) is int and receipt['checkpoint_library_version']>0,'receipt Library identity/version')
            ensure(receipt['checkpoint_library_version']>cp.get('last_verified_library_version',0),'nonincreasing publication version')
            verified=ts(receipt['verified_at_utc'])
            ensure(ts(cp['as_of_utc'])<=verified<=ts(now),'receipt predates checkpoint or is future')
            q=op['kwargs'].get('q')
            try:
                ensure(set(op['kwargs'])<={'q','now','preview','exit_fee'},'unsupported execute keyword')
                ensure(isinstance(q,dict) and 'retrieved' in q,'execution quote retrieval evidence required')
                ensure(verified<ts(q['retrieved'])<=ts(now),'quote must be retrieved after decision publication readback')
            except (ValueError,KeyError,TypeError) as ex:
                # The receipt is valid even when quote proof is not. Commit it and
                # this rejection, so another receipt cannot erase the first clock.
                rejection=str(ex)
            if sim.pending['kind']=='ENTRY':
                decision=sim.pending['at'];plan_hash=sim.pending['plan_hash']
                stored=cp.get('entry_publication_anchor')
                if stored is not None:
                    ensure(stored['decision_at']==decision and stored['plan_hash']==plan_hash,'entry publication anchor identity')
                    anchor=deepcopy(stored)
                else:
                    anchor={'decision_at':decision,'plan_hash':plan_hash,'first_receipt':deepcopy(receipt)}
                first=ts(anchor['first_receipt']['verified_at_utc'])
                ensure(ts(decision)<=first<=verified,'entry first readback clock')
                timing=entry_timing(sim.pending['plan'],decision)
                if timing is not None:
                    if verified-ts(decision)>timedelta(seconds=timing['max_decision_to_readback_seconds']):
                        rejection='original decision publication bound exceeded';timing_failure=True
                    elif ts(now)-first>timedelta(seconds=timing['max_first_readback_to_quote_seconds']) and rejection is not None:
                        rejection='original first-readback quote bound exceeded without usable quote';timing_failure=True
                    elif rejection is None and ts(q['retrieved'])-first>timedelta(seconds=timing['max_first_readback_to_quote_seconds']):
                        rejection='original first-readback quote bound exceeded';timing_failure=True
    meta = deepcopy({k: v for k, v in cp.items() if k not in ['checkpoint_sha256', 'generation', 'previous_generation_sha256', 'snapshot_base64', 'snapshot_sha256', 'metrics', 'as_of_utc']})
    if op['method']=='record_review':
        ensure(sim.position is None and sim.pending is None,'research review requires flat and no pending action')
        kw=deepcopy(op['kwargs']);at=kw['at'];ensure(ts(at)>=ts(cp['as_of_utc']),'regressing review clock')
        ensure(kw['action'] in ['WAIT','PASS','NO_CANDIDATE'] and kw['evidence'] and kw['reason'],'research evidence required')
        if kw['action']=='WAIT':
            ensure(sim.position is None and sim.pending is None and ts(at)<ts(sim.intake_end),'WAIT intake/exposure')
            result=wait_decision(cash=sim.cash,now=at,intake_end=sim.intake_end,**kw['wait_inputs'])
            ensure(result['action']=='WAIT','WAIT feasibility failed')
            kw['validated_wait']=result
        reviews=meta.setdefault('research_reviews',[])
        append_event(reviews,{'id':str(len(reviews)+1),'type':kw['action'],'at':at,'details':log_details(kw)})
    else:
        ensure(op['method'] in ['decide_entry', 'execute', 'expire_entry', 'observe', 'tighten'], 'unsupported operation')
        ensure(ts(op['kwargs']['now'])>=ts(cp['as_of_utc']),'regressing operation checkpoint clock')
        if rejection is not None:
            sim.reject_execution(op['kwargs'].get('q'),op['kwargs']['now'],rejection,timing_failure)
        else:
            getattr(sim, op['method'])(**op['kwargs'])
        at=iso(sim.clock)
    meta['checkpoint_library_id'] = checkpoint_library_id
    # An expiry or nonexecution transition cannot erase/replace accepted receipt provenance.
    if receipt is not None:
        meta['parent_publication_receipt']=deepcopy(receipt)
        meta['last_verified_library_version']=receipt['checkpoint_library_version']
        meta.setdefault('publication_receipts',[]).append(deepcopy(receipt))
    if op['method']=='decide_entry':
        meta.pop('entry_publication_anchor',None)
    if anchor is not None:
        meta['entry_publication_anchor']=anchor
    return pack(sim, meta, cp, at)

def verify_chain_dir(directory, anchor_sha256=None):
    paths = sorted(Path(directory).glob('generation-*.json'))
    ensure(paths, 'empty chain')
    prev = None
    cps = []
    first=read_generation(paths[0])
    ensure(first['generation']==0 or anchor_sha256==first['checkpoint_sha256'],'non-genesis restore requires trusted anchor SHA')
    ensure(anchor_sha256 is None or anchor_sha256==first['checkpoint_sha256'],'trusted anchor does not match the first generation')
    start=first['generation']
    for n, p in enumerate(paths):
        cp = read_generation(p)
        verify(cp)
        ensure(cp['generation'] == n+start and (prev is None or cp['previous_generation_sha256'] == prev['checkpoint_sha256']), 'generation chain')
        if prev:
            ensure(cp['previous_cohort'] == prev['previous_cohort'] and cp['implementation'] == prev['implementation'] and cp['protocol_document']==prev['protocol_document'], 'immutable provenance')
            ensure(cp['activation_at_utc'] == prev['activation_at_utc'] and cp['opening_cash_usd'] == prev['opening_cash_usd'], 'cohort start changed')
            ensure(prev['checkpoint_library_id'] is None or cp['checkpoint_library_id'] == prev['checkpoint_library_id'], 'Library identity changed')
            ensure(cp['equity_history']==prev['equity_history']+[equity_record(prev)],'equity history changed')
            ensure(cp.get('research_reviews',[])[:len(prev.get('research_reviews',[]))]==prev.get('research_reviews',[]),'research prefix changed')
            ensure(cp.get('publication_receipts',[])[:len(prev.get('publication_receipts',[]))]==prev.get('publication_receipts',[]),'publication receipt prefix changed')
            ensure(cp.get('last_verified_library_version',0)>=prev.get('last_verified_library_version',0),'publication version regressed')
            pending=verify(prev).pending
            if pending is not None and pending['kind']=='ENTRY' and prev.get('entry_publication_anchor') is not None:
                ensure(cp.get('entry_publication_anchor')==prev['entry_publication_anchor'],'original entry publication anchor changed')
            pr,cr=prev.get('publication_receipts',[]),cp.get('publication_receipts',[])
            ensure(len(cr) in (len(pr),len(pr)+1),'publication receipt journal grew by more than one')
            if len(cr)==len(pr)+1:
                r=cr[-1]
                ensure(isinstance(r,dict) and r.get('checkpoint_sha256')==prev['checkpoint_sha256'] and r.get('checkpoint_library_id')==cp['checkpoint_library_id'] and r.get('readback_verified') is True and type(r.get('checkpoint_library_version')) is int and r['checkpoint_library_version']>prev.get('last_verified_library_version',0),'appended receipt not bound to its parent generation')
                ensure(ts(prev['as_of_utc'])<=ts(r['verified_at_utc'])<=ts(cp['as_of_utc']),'appended receipt clock outside its generation')
                ensure(cp.get('parent_publication_receipt')==r and cp.get('last_verified_library_version')==r['checkpoint_library_version'],'receipt fields disagree with the receipt journal')
            else:
                ensure(cp.get('parent_publication_receipt')==prev.get('parent_publication_receipt') and cp.get('last_verified_library_version',0)==prev.get('last_verified_library_version',0),'receipt fields changed without a new receipt')
            anchor=cp.get('entry_publication_anchor')
            if anchor is not None and anchor!=prev.get('entry_publication_anchor'):
                ensure(pending is not None and pending['kind']=='ENTRY' and len(cr)==len(pr)+1 and anchor=={'decision_at':pending['at'],'plan_hash':pending['plan_hash'],'first_receipt':cr[-1]},'new entry anchor not bound to the receipt accepted in its generation')
            ensure(ts(cp['as_of_utc'])>=ts(prev['as_of_utc']),'generation clock')
            oldevents = verify(prev).events
            ensure(verify(cp).events[:len(oldevents)] == oldevents, 'event prefix changed')
        cps.append(cp)
        prev = cp
    return cps

import shlex
RECOVERY_CLI = 'python3 -I -B ' + shlex.quote(str(Path(__file__).resolve().with_name('recovery.py')))
RECOVERY_USAGE = {
    'init': '--root FRESH_ROOT --operation-id OPERATION_ID --source SOURCE_JSON --expected-source-sha256 SOURCE_SHA256 '
            '--activation UTC_TIMESTAMP --source-versions VERSIONS_JSON [--approved] [--reviewed]',
    'advance': '--root ROOT --operation-id OPERATION_ID --expected-current-sha256 CURRENT_SHA256 '
               '--checkpoint-library-id LIBRARY_ID --operations OPERATIONS_JSON [--publication-receipt RECEIPT_JSON]',
}

def refusal(command):
    return (f'ledger.py {command} is refused: direct chain mutation bypasses the recovery journal, operation IDs, '
            'stale-parent checks and pending/uncertain-state guards. Nothing was read or written.\n'
            f'Use the recovery caller instead:\n  {RECOVERY_CLI} {command} {RECOVERY_USAGE[command]}\n'
            f'Check state first with: {RECOVERY_CLI} status --root ROOT  (and verify --root ROOT). '
            'Resolve a pending operation only with recovery.py reconcile and its exact operation ID and expected '
            'result SHA-256. A raw --chain or --out directory is not a recovery root and is never adopted.\n'
            'FRESH_ROOT must not exist yet. Still available: ledger.py verify (read-only) and ledger.py export '
            '(writes only a new --out file; never point it inside a recovery root).')

def main(argv=None):
    a = argparse.ArgumentParser(description='Raw checkpoint chain tools. init and advance are refused; '
                                'mutate only through recovery.py.')
    sub = a.add_subparsers(dest='cmd', required=True)
    for name in RECOVERY_USAGE:
        # Refused before any argument is interpreted or any file is touched.
        sub.add_parser(name, help=f'refused: use recovery.py {name}', description=refusal(name),
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    v = sub.add_parser('verify')
    v.add_argument('--chain', required=True);v.add_argument('--anchor-sha256')
    e = sub.add_parser('export')
    e.add_argument('--chain', required=True);e.add_argument('--anchor-sha256')
    e.add_argument('--out', required=True)
    args, extra = a.parse_known_args(argv)
    if args.cmd in RECOVERY_USAGE:
        print(refusal(args.cmd), file=sys.stderr)
        return 2
    if extra:
        a.error('unrecognized arguments: ' + ' '.join(extra))
    else:
        cps = verify_chain_dir(args.chain,args.anchor_sha256)
        if args.cmd == 'export':
            atomic_write_once(canonical({'current': cps[-1], 'series': cps[-1]['equity_history']+[equity_record(cps[-1])]}), args.out)
            print(args.out)
        else:
            print(json.dumps({'valid': True, 'generations': len(cps), 'latest': cps[-1]['checkpoint_sha256']}))
if __name__ == '__main__':
    raise SystemExit(main())
