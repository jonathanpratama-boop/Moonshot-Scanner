"""Focused tests: raw ledger.py init/advance refuse by default; recovery.py stays the mutation route.
Synthetic fresh roots only. Child processes inherit -O explicitly. No fixture action sits inside assert.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

WORK = Path(__file__).resolve().parent.parent
os.environ.setdefault('CANDIDATE_ROOT', str(WORK/'candidate_tree/candidate'))
sys.path.insert(0, str(WORK/'prior_tests'))
import rv_helpers as H
sys.path.insert(0, str(H.ROOT/'catalyst_paper_delivery'))
import recovery as R

L = H.L
FLAGS = ['-O'] if sys.flags.optimize else []
DRIVER = WORK/'recovery_tests'/'fault_driver.py'
CHILDREN = []


def run(script, args, *, fault=None, root=None, sequence=None, env=None):
    command = [sys.executable, '-I', '-B', *FLAGS]
    if fault:
        command += [str(DRIVER), str(script), fault, str(root), str(sequence)]
    else:
        command += [str(script)]
    command += [str(x) for x in args]
    p = subprocess.run(command, text=True, capture_output=True, timeout=60,
                       env=dict(os.environ, **(env or {})))
    CHILDREN.append({'argv': command, 'exit': p.returncode})
    return p


def snapshot(base):
    """Every directory and every file's exact bytes under base."""
    state = {}
    for dirpath, dirnames, filenames in os.walk(base):
        rel = Path(dirpath).relative_to(base).as_posix()
        state['dir:' + rel] = sorted(dirnames)
        for name in filenames:
            p = Path(dirpath)/name
            state['file:' + p.relative_to(base).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return state


class RawCliRefusalTests(unittest.TestCase):
    def setUp(self):
        (WORK/'tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK/'tmp', prefix='cli_refusal_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'caller'
        self.source = H.ROOT/'catalyst_paper_v2/legacy/checkpoint.json'
        self.expected = L.sha(self.source.read_bytes())
        self.versions = json.dumps({'checkpoint': {'id': H.LIB, 'version': 12},
                                    'workbook': {'id': H.LIB, 'version': 11}}).encode()
        self.vfile = self.base/'versions.json'
        self.vfile.write_bytes(self.versions)

    # ---- helpers on fresh synthetic recovery roots
    def recovery_init(self):
        result = R.initialize(self.root, 'genesis', self.source, self.expected, '2026-10-05T19:30:00Z',
                              self.versions, True, True)
        self.assertFalse(result.get('mutation_blocked'), result)

    def head(self):
        return L.verify_chain_dir(self.root/'chain')[-1]

    def opfile(self, name, method, kw):
        f = self.base/(name + '.ops.json')
        f.write_bytes(json.dumps([{'method': method, 'kwargs': kw}]).encode())
        return f

    def recovery_args(self, op_id, method, kw, receipt=None):
        args = ['advance', '--root', self.root, '--operation-id', op_id,
                '--expected-current-sha256', self.head()['checkpoint_sha256'],
                '--checkpoint-library-id', H.LIB, '--operations', self.opfile(op_id, method, kw)]
        if receipt is not None:
            rf = self.base/(op_id + '.receipt.json')
            rf.write_text(json.dumps(receipt))
            args += ['--publication-receipt', rf]
        return args

    def raw_advance_args(self, chain, method, kw, receipt=None):
        args = ['advance', '--chain', chain, '--operations', self.opfile('raw', method, kw),
                '--expected-current-sha256', L.verify_chain_dir(chain)[-1]['checkpoint_sha256'],
                '--checkpoint-library-id', H.LIB]
        if receipt is not None:
            rf = self.base/'raw.receipt.json'
            rf.write_text(json.dumps(receipt))
            args += ['--publication-receipt', rf]
        return args

    def assert_refused(self, p, command):
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(p.stdout, '')
        self.assertIn(f'ledger.py {command} is refused', p.stderr)
        root = 'FRESH_ROOT' if command == 'init' else 'ROOT'
        self.assertIn(f"python3 -I -B {shlex.quote(str(Path(R.__file__).resolve()))} {command} --root {root} "
                      '--operation-id OPERATION_ID', p.stderr)
        self.assertIn('status --root ROOT', p.stderr)
        self.assertIn('Nothing was read or written', p.stderr)
        self.assertNotIn('Traceback', p.stderr)

    # ---- refusal and no writes
    def test_raw_init_refused_without_creating_output_or_touching_inputs(self):
        out = self.base/'new_chain'
        full = ['init', '--source', self.source, '--expected-source-sha256', self.expected,
                '--activation', '2026-10-05T19:30:00Z', '--source-versions', self.vfile,
                '--out', out, '--approved', '--reviewed']
        source_before = self.source.read_bytes()
        for args in (full, ['init'], ['init', '--out', out, '--unknown-flag', 'x']):
            with self.subTest(args=[str(a) for a in args][:3]):
                before = snapshot(self.base)
                self.assert_refused(run(L.__file__, args), 'init')
                self.assertFalse(out.exists())
                self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.source.read_bytes(), source_before)
        existing = self.base/'existing_empty_dir'
        existing.mkdir()
        # Same valid argv with --out an existing empty dir: the unpatched CLI writes generation-000000 here.
        self.assert_refused(run(L.__file__, full[:-4] + ['--out', existing, '--approved', '--reviewed']), 'init')
        self.assertEqual(list(existing.iterdir()), [])

    def test_raw_advance_refused_on_recovery_root_and_plain_chain(self):
        self.recovery_init()
        p = H.xplan(H.B, contract=False)
        decided = self.recovery_args('decision', 'decide_entry', {'p': p, 'now': H.B})
        self.assertEqual(run(R.__file__, decided).returncode, 0)
        raw = self.raw_advance_args(self.root/'chain', 'execute', H.inputs(p, H.B, 60), H.rcpt(self.head(), 40))
        before, before_status = snapshot(self.base), R.status(self.root)
        self.assert_refused(run(L.__file__, raw), 'advance')
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(R.status(self.root), before_status)
        self.assertEqual(self.head()['generation'], 1)                    # genesis 0, decision 1: no new generation
        # A plain raw chain built with the imported API is refused the same way.
        chain = self.base/'plain_chain'
        g = H.genesis(); cp = H.adv(g, 'decide_entry', {'p': p, 'now': H.B})
        L.save(g, chain); L.save(cp, chain)
        raw = self.raw_advance_args(chain, 'execute', H.inputs(p, H.B, 60), H.rcpt(cp, 40))
        before = snapshot(self.base)
        self.assert_refused(run(L.__file__, raw), 'advance')
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(len(L.verify_chain_dir(chain)), 2)

    def test_raw_advance_refused_while_recovery_operation_is_uncertain(self):
        """The bypass scenario: an uncertain BUY must not be followed by a raw transition."""
        self.recovery_init()
        p = H.xplan(H.B, contract=False)
        self.assertEqual(run(R.__file__, self.recovery_args('decision', 'decide_entry', {'p': p, 'now': H.B})).returncode, 0)
        buy = self.recovery_args('buy', 'execute', H.inputs(p, H.B, 60), H.rcpt(self.head(), 40))
        self.assertEqual(run(R.__file__, buy, fault='publication_dir_sync', root=self.root,
                             sequence=self.head()['generation'] + 1).returncode, 2)
        status = R.status(self.root)
        self.assertTrue(status['mutation_blocked'], status)
        now = H.at(H.B, 61)
        followup = {'now': now, 'bid': '100', 'mark_at': now, 'exit_fee': H.appl(p, 'SELL', now)}
        raw = self.raw_advance_args(self.root/'chain', 'observe', followup)
        before = snapshot(self.base)
        self.assert_refused(run(L.__file__, raw), 'advance')
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(R.status(self.root), status)
        # The protected route still refuses the next transition until explicit reconciliation.
        nxt = self.recovery_args('next-observation', 'observe', followup)
        before = snapshot(self.base)
        self.assertEqual(run(R.__file__, nxt).returncode, 2)
        self.assertEqual(snapshot(self.base), before)

    # ---- recovery continuity
    def test_recovery_cli_init_and_advance_continue_to_work(self):
        init = ['init', '--root', self.root, '--operation-id', 'genesis', '--source', self.source,
                '--expected-source-sha256', self.expected, '--activation', '2026-10-05T19:30:00Z',
                '--source-versions', self.vfile, '--approved', '--reviewed']
        self.assertEqual(run(R.__file__, init).returncode, 0)
        self.assertEqual(run(R.__file__, init).returncode, 0)            # idempotent duplicate acknowledgement
        p = H.xplan(H.B, contract=False)
        self.assertEqual(run(R.__file__, self.recovery_args('decision', 'decide_entry', {'p': p, 'now': H.B})).returncode, 0)
        buy = self.recovery_args('buy', 'execute', H.inputs(p, H.B, 60), H.rcpt(self.head(), 40))
        first = run(R.__file__, buy)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = L.verify(self.head())
        self.assertEqual(state.events[-1]['type'], 'BUY')
        self.assertEqual(state.cash + state.position['basis'], state.baseline)
        generations = self.head()['generation']
        dup = run(R.__file__, buy)                                          # duplicate does not replay
        self.assertEqual(dup.returncode, 0, dup.stdout + dup.stderr)
        self.assertEqual(self.head()['generation'], generations)
        status = run(R.__file__, ['status', '--root', self.root])
        self.assertEqual(status.returncode, 0)
        self.assertFalse(json.loads(status.stdout)['mutation_blocked'])

    # ---- verify / export preserved
    def test_raw_verify_and_export_preserved_and_do_not_clear_pending(self):
        self.recovery_init()
        p = H.xplan(H.B, contract=False)
        self.assertEqual(run(R.__file__, self.recovery_args('decision', 'decide_entry', {'p': p, 'now': H.B})).returncode, 0)
        buy = self.recovery_args('buy', 'execute', H.inputs(p, H.B, 60), H.rcpt(self.head(), 40))
        run(R.__file__, buy, fault='publication_dir_sync', root=self.root, sequence=self.head()['generation'] + 1)
        status = R.status(self.root)
        self.assertTrue(status['mutation_blocked'])
        chain = self.root/'chain'
        genesis_sha = L.verify_chain_dir(chain)[0]['checkpoint_sha256']
        before = snapshot(self.root)
        v = run(L.__file__, ['verify', '--chain', chain])
        self.assertEqual(v.returncode, 0, v.stderr)
        out = json.loads(v.stdout)
        self.assertTrue(out['valid'])
        self.assertEqual(out['latest'], L.verify_chain_dir(chain)[-1]['checkpoint_sha256'])
        self.assertEqual(run(L.__file__, ['verify', '--chain', chain, '--anchor-sha256', genesis_sha]).returncode, 0)
        self.assertNotEqual(run(L.__file__, ['verify', '--chain', chain, '--anchor-sha256', 'f' * 64]).returncode, 0)
        export = self.base/'export.json'
        e = run(L.__file__, ['export', '--chain', chain, '--out', export])
        self.assertEqual((e.returncode, e.stdout.strip()), (0, str(export)), e.stderr)
        exported = json.loads(export.read_bytes())
        self.assertEqual(exported['current']['checkpoint_sha256'], out['latest'])
        written = export.read_bytes()
        self.assertNotEqual(run(L.__file__, ['export', '--chain', chain, '--out', export]).returncode, 0)  # write-once
        self.assertEqual(export.read_bytes(), written)
        self.assertEqual(run(L.__file__, ['export', '--chain', chain, '--anchor-sha256', genesis_sha,
                                          '--out', self.base/'export2.json']).returncode, 0)
        self.assertEqual(snapshot(self.root), before)                       # chain/journal untouched
        self.assertEqual(R.status(self.root), status)                       # pending state not cleared
        bad = run(L.__file__, ['verify', '--chain', chain, '--bogus'])
        self.assertEqual(bad.returncode, 2)
        self.assertIn('unrecognized arguments: --bogus', bad.stderr)

    # ---- no bypass, imported API unchanged
    def test_no_environment_bypass_and_only_documented_subcommands(self):
        env = {'LEDGER_ALLOW_RAW': '1', 'LEDGER_UNSAFE': '1', 'CATALYST_ALLOW_RAW_MUTATION': 'true',
               'ALLOW_UNSAFE': '1', 'PYTHONOPTIMIZE': '2'}
        for command in ('init', 'advance'):
            with self.subTest(command=command):
                self.assert_refused(run(L.__file__, [command], env=env), command)
        top = run(L.__file__, ['--help'])
        self.assertEqual(top.returncode, 0)
        self.assertIn('{init,advance,verify,export}', top.stdout)
        sub_help = run(L.__file__, ['advance', '--help'])
        self.assertEqual(sub_help.returncode, 0)
        self.assertIn('recovery.py advance --root ROOT', sub_help.stdout)

    def test_imported_api_and_in_process_main_semantics(self):
        chain = self.base/'api_chain'
        g = H.genesis(); p = H.xplan(H.B, contract=False)
        cp = H.adv(g, 'decide_entry', {'p': p, 'now': H.B})
        cp2 = H.adv(cp, 'execute', H.inputs(p, H.B, 60), H.rcpt(cp, 40))
        for c in (g, cp, cp2):
            L.save(c, chain)
        self.assertEqual(L.verify(L.verify_chain_dir(chain)[-1]).events[-1]['type'], 'BUY')
        self.assertTrue(callable(L.initialize) and callable(L.advance) and callable(L.save))
        # In-process main() reading sys.argv, with a valid BUY that the unpatched CLI would save as generation 2.
        decided = self.base/'api_decided_chain'
        L.save(g, decided); L.save(cp, decided)
        raw = [str(x) for x in self.raw_advance_args(decided, 'execute', H.inputs(p, H.B, 60), H.rcpt(cp, 40))]
        before = snapshot(self.base)
        with mock.patch.object(sys, 'argv', [L.__file__, *raw]), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(L.main(), 2)
        self.assertIn('ledger.py advance is refused', err.getvalue())
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(len(L.verify_chain_dir(decided)), 2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(L.main(raw), 2)
            self.assertEqual(L.main(['init']), 2)
        self.assertEqual(snapshot(self.base), before)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(L.main(['verify', '--chain', str(chain)]))
        self.assertEqual(json.loads(out.getvalue())['generations'], 3)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    print('CHILD_PROCESSES=' + json.dumps({'optimize': sys.flags.optimize, 'children': CHILDREN}))
    raise SystemExit(not result.wasSuccessful())
