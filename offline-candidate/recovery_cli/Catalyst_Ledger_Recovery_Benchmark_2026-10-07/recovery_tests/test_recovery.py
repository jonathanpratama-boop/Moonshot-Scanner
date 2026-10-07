"""Synthetic recovery invariants. Standard library; actual CLI faults/restarts."""
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

WORK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORK/'prior_tests'))
import rv_helpers as H
sys.path.insert(0, str(H.ROOT/'catalyst_paper_delivery'))
import recovery as R

TRACE = []
FLAGS = ['-O'] if sys.flags.optimize else []
DRIVER = Path(__file__).with_name('fault_driver.py')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK/'tmp', prefix='recovery_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'caller'
        self.source = H.ROOT/'catalyst_paper_v2/legacy/checkpoint.json'
        self.expected = H.L.sha(self.source.read_bytes())
        self.versions = json.dumps({'checkpoint': {'id': H.LIB, 'version': 12},
                                   'workbook': {'id': H.LIB, 'version': 11}}).encode()
        self.vfile = self.base/'versions.json'
        self.vfile.write_bytes(self.versions)
        self.init_args = ['init', '--root', str(self.root), '--operation-id', 'genesis',
                          '--source', str(self.source), '--expected-source-sha256', self.expected,
                          '--activation', '2026-10-05T19:30:00Z', '--source-versions', str(self.vfile),
                          '--approved', '--reviewed']
        self.initialize()

    def initialize(self):
        return R.initialize(self.root, 'genesis', self.source, self.expected, '2026-10-05T19:30:00Z',
                            self.versions, True, True)

    def head(self):
        return H.L.verify_chain_dir(self.root/'chain')[-1]

    def tree(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file()}

    def operation(self, method, kw):
        return json.dumps([{'method': method, 'kwargs': kw}]).encode()

    def step(self, op_id, method, kw, receipt=None):
        return R.advance(self.root, op_id, self.head()['checkpoint_sha256'], H.LIB,
                         self.operation(method, kw), json.dumps(receipt).encode() if receipt else None)

    def decided(self):
        p = H.xplan(H.B, contract=False)
        self.step('decision', 'decide_entry', {'p': p, 'now': H.B})
        return p

    def entered(self):
        p = self.decided()
        self.step('buy', 'execute', H.inputs(p, H.B, 60), H.rcpt(self.head(), 40))
        self.assertIsNotNone(H.L.verify(self.head()).position)
        return p

    def cli(self, args, fault=None, sequence=None):
        if sequence is None:
            sequence = self.head()['generation'] + 1
        command = [sys.executable, '-I', '-B', *FLAGS]
        if fault:
            command += [str(DRIVER), R.__file__, fault, str(self.root), str(sequence)]
        else:
            command += [R.__file__]
        command += list(map(str, args))
        p = subprocess.run(command, text=True, capture_output=True, timeout=20)
        TRACE.append({'test': self.id(), 'argv': command, 'exit': p.returncode,
                      'stdout': p.stdout, 'stderr': p.stderr})
        if fault:
            lines = [x for x in p.stderr.splitlines() if x.startswith('TEST_PROCESS=')]
            self.assertTrue(lines)
            self.assertEqual(json.loads(lines[0].split('=', 1)[1])['optimize'], sys.flags.optimize)
        return p

    def args(self, op_id, method, kw, receipt=None):
        opfile = self.base/(op_id+'.ops.json')
        opfile.write_bytes(self.operation(method, kw))
        args = ['advance', '--root', str(self.root), '--operation-id', op_id,
                '--expected-current-sha256', self.head()['checkpoint_sha256'],
                '--checkpoint-library-id', H.LIB, '--operations', str(opfile)]
        if receipt is not None:
            rf = self.base/(op_id+'.receipt.json')
            rf.write_text(json.dumps(receipt))
            args += ['--publication-receipt', str(rf)]
        return args

    def buy_args(self):
        p = self.decided()
        return self.args('buy', 'execute', H.inputs(p, H.B, 60), H.rcpt(self.head(), 40))

    def test_normal_roundtrip_and_duplicate_does_not_replay(self):
        args = self.buy_args()
        self.assertEqual(self.cli(args).returncode, 0)
        before = self.tree()
        duplicate = self.cli(args)
        self.assertEqual(duplicate.returncode, 0)
        self.assertTrue(json.loads(duplicate.stdout)['duplicate'])
        self.assertEqual(before, self.tree())
        self.assertEqual([e['type'] for e in H.L.verify(self.head()).events].count('BUY'), 1)

    def test_duplicate_init_is_idempotent(self):
        before = self.tree()
        self.assertTrue(self.initialize()['duplicate'])
        self.assertEqual(self.cli(self.init_args).returncode, 0)
        self.assertEqual(self.tree(), before)

    def test_changed_request_under_completed_id_is_refused(self):
        p = self.decided()
        before = self.tree()
        with self.assertRaisesRegex(R.Blocked, 'different request'):
            self.step('decision', 'observe', {'now': H.B, 'bid': '100'})
        self.assertEqual(before, self.tree())

    def test_explicit_extended_timing_is_refused_without_intent(self):
        before = self.tree()
        with self.assertRaisesRegex(R.Blocked, 'extended-entry'):
            self.step('decision', 'decide_entry', {'p': H.xplan(H.B), 'now': H.B})
        self.assertEqual(before, self.tree())

    def test_stale_parent_is_refused_before_intent(self):
        before = self.tree()
        with self.assertRaisesRegex(ValueError, 'stale writer'):
            R.advance(self.root, 'stale', '0'*64, H.LIB,
                      self.operation('decide_entry', {'p': H.xplan(H.B, contract=False), 'now': H.B}))
        self.assertEqual(before, self.tree())

    def test_no_receipt_cannot_buy_and_no_new_intent(self):
        p = self.decided()
        before = self.tree()
        with self.assertRaisesRegex(ValueError, 'receipt'):
            self.step('buy', 'execute', H.inputs(p, H.B, 60))
        self.assertEqual(before, self.tree())

    def test_intent_create_failure_publishes_nothing_and_safe_retry(self):
        args = self.buy_args()
        before = self.tree()
        failed = self.cli(args, 'intent_before')
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(before, self.tree())
        self.assertEqual(self.cli(args).returncode, 0)

    def test_intent_sync_failures_publish_nothing_and_stay_pending(self):
        for fault in ('intent_file_sync', 'intent_dir_sync', 'after_intent', 'save_before'):
            with self.subTest(fault=fault):
                # Separate root for each terminal pending case.
                oldroot = self.root
                self.root = self.base/fault
                self.initialize()
                args = self.buy_args()
                before = self.head()['checkpoint_sha256']
                self.assertEqual(self.cli(args, fault).returncode, 2)
                state = R.status(self.root)
                self.assertEqual(state['status'], 'PENDING')
                self.assertEqual(self.head()['checkpoint_sha256'], before)
                frozen = self.tree()
                self.assertEqual(self.cli(args).returncode, 2)
                self.assertEqual(self.cli(['verify', '--root', self.root]).returncode, 2)
                with self.assertRaisesRegex(R.Blocked, 'result absent'):
                    R.reconcile(self.root, 'buy', state['pending_result_sha256'])
                self.assertEqual(frozen, self.tree())
                self.root = oldroot

    def test_partial_pending_record_blocks_without_publication(self):
        args = self.buy_args()
        before = self.head()['checkpoint_sha256']
        self.assertEqual(self.cli(args, 'intent_partial').returncode, 2)
        self.assertEqual(self.head()['checkpoint_sha256'], before)
        self.assertEqual(R.status(self.root)['status'], 'BLOCKED_CONTROL')
        frozen = self.tree()
        self.assertEqual(self.cli(args).returncode, 2)
        self.assertEqual(frozen, self.tree())

    def test_publication_uncertainty_status_is_read_only_and_reconcile_no_replay(self):
        args = self.buy_args()
        parent = self.head()
        self.assertEqual(self.cli(args, 'publication_dir_sync').returncode, 2)
        cp = self.head()
        self.assertIsNotNone(H.L.verify(cp).position)
        before = self.tree()
        for command in ('status', 'verify'):
            result = self.cli([command, '--root', self.root])
            self.assertEqual(result.returncode, 2)
            state = json.loads(result.stdout)
            self.assertTrue(state['chain_valid'])
            self.assertTrue(state['mutation_blocked'])
            self.assertFalse(state['external_execution_approved'])
            self.assertEqual(state['recorded_simulated_state']['checkpoint_sha256'], cp['checkpoint_sha256'])
            self.assertEqual(state['last_completed_local_state']['checkpoint_sha256'], parent['checkpoint_sha256'])
        self.assertEqual(before, self.tree())
        self.assertEqual(self.cli(args).returncode, 2)
        result = self.cli(['reconcile', '--root', self.root, '--operation-id', 'buy',
                           '--expected-result-sha256', cp['checkpoint_sha256']])
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['external_execution_approved'])
        self.assertEqual(cp, self.head())
        self.assertEqual(self.cli(args).returncode, 0)
        self.assertEqual(cp, self.head())

    def test_uncertain_sell_keeps_recorded_cash_separate_from_accepted_cash(self):
        p = self.entered()
        now = p['checks'][0]
        self.step('stop', 'observe', {'now': now, 'bid': '97', 'mark_at': now, 'exit_fee': H.appl(p, 'SELL', now)})
        parent = self.head()
        t = H.at(now, 5)
        kw = {'q': H.quote(t, '97', '97.1', 'EXIT-Q'), 'now': H.at(t, 2), 'exit_fee': H.appl(p, 'SELL', H.at(t, 1))}
        receipt = H.rcpt(parent, 0) | {'verified_at_utc': H.at(t, -1)}
        args = self.args('sell', 'execute', kw, receipt)
        self.assertEqual(self.cli(args, 'publication_dir_sync').returncode, 2)
        cp = self.head()
        self.assertIsNone(H.L.verify(cp).position)
        state = R.status(self.root)
        self.assertTrue(state['mutation_blocked'])
        self.assertEqual(state['last_completed_local_state']['cash'], parent['metrics']['cash'])
        self.assertEqual(state['recorded_simulated_state']['cash'], cp['metrics']['cash'])
        self.assertNotEqual(parent['metrics']['cash'], cp['metrics']['cash'])
        self.assertEqual(self.cli(args).returncode, 2)
        R.reconcile(self.root, 'sell', cp['checkpoint_sha256'])
        self.assertEqual(self.cli(args).returncode, 0)
        self.assertEqual(cp, self.head())
        self.assertEqual([e['type'] for e in H.L.verify(cp).events].count('SELL'), 1)

    def test_post_publication_resync_and_completion_failures(self):
        for fault in ('result_resync', 'completion_before'):
            with self.subTest(fault=fault):
                old = self.root
                self.root = self.base/fault
                self.initialize()
                args = self.buy_args()
                self.assertEqual(self.cli(args, fault).returncode, 2)
                cp = self.head()
                self.assertEqual(R.status(self.root)['status'], 'RESULT_VISIBLE_PENDING')
                R.reconcile(self.root, 'buy', cp['checkpoint_sha256'])
                self.assertEqual(self.cli(args).returncode, 0)
                self.assertEqual(cp, self.head())
                self.root = old

    def test_completion_uncertainty_and_lost_ack_never_replay(self):
        for fault in ('completion_file_sync', 'completion_dir_sync', 'ack'):
            with self.subTest(fault=fault):
                old = self.root
                self.root = self.base/fault
                self.initialize()
                args = self.buy_args()
                self.assertEqual(self.cli(args, fault).returncode, 2)
                cp = self.head()
                self.assertEqual(R.status(self.root)['status'], 'COMPLETION_RECORDED')
                retry = self.cli(args)
                self.assertEqual(retry.returncode, 0)
                self.assertTrue(json.loads(retry.stdout)['duplicate'])
                self.assertEqual(cp, self.head())
                self.root = old

    def test_partial_completion_fails_closed_and_preserves_result(self):
        args = self.buy_args()
        self.assertEqual(self.cli(args, 'completion_partial').returncode, 2)
        cp = self.head()
        self.assertEqual(R.status(self.root)['status'], 'BLOCKED_CONTROL')
        self.assertEqual(self.cli(args).returncode, 2)
        with self.assertRaises(ValueError):
            R.reconcile(self.root, 'buy', cp['checkpoint_sha256'])
        self.assertEqual(cp, self.head())

    def test_real_process_crashes_at_intent_publication_and_completion(self):
        for fault, code in (('crash_intent_partial', 71), ('crash_after_intent', 72),
                            ('crash_after_publication', 73), ('crash_after_completion', 74)):
            with self.subTest(fault=fault):
                old = self.root
                self.root = self.base/fault
                self.initialize()
                args = self.buy_args()
                self.assertEqual(self.cli(args, fault).returncode, code)
                state = R.status(self.root)
                if fault == 'crash_after_completion':
                    self.assertEqual(self.cli(args).returncode, 0)
                    self.assertEqual(self.head()['generation'], 2)
                else:
                    self.assertTrue(state['mutation_blocked'])
                    self.assertEqual(self.cli(args).returncode, 2)
                if fault == 'crash_after_publication':
                    cp = self.head()
                    R.reconcile(self.root, 'buy', cp['checkpoint_sha256'])
                    self.assertEqual(cp, self.head())
                self.root = old

    def test_failed_reconciliation_stays_pending(self):
        args = self.buy_args()
        self.cli(args, 'publication_dir_sync')
        cp = self.head()
        before = self.tree()
        with patch.object(R, '_sync', side_effect=OSError('resync failed')):
            with self.assertRaises(OSError):
                R.reconcile(self.root, 'buy', cp['checkpoint_sha256'])
        self.assertEqual(before, self.tree())
        self.assertTrue(R.status(self.root)['mutation_blocked'])
        for op_id, expected in (('other', cp['checkpoint_sha256']), ('buy', '0'*64)):
            with self.assertRaisesRegex(R.Blocked, 'exact latest'):
                R.reconcile(self.root, op_id, expected)
        self.assertEqual(before, self.tree())

    def test_reconciliation_record_failure_preserves_pending(self):
        args = self.buy_args()
        self.cli(args, 'publication_dir_sync')
        cp = self.head()
        with patch.object(R, '_write_record', side_effect=OSError('receipt create failed')):
            with self.assertRaises(OSError):
                R.reconcile(self.root, 'buy', cp['checkpoint_sha256'])
        self.assertTrue(R.status(self.root)['mutation_blocked'])
        self.assertEqual(cp, self.head())

    def test_missing_malformed_and_symlink_controls_fail_closed(self):
        for name in ('identity.json', 'lock', 'journal/000000.intent.json', 'journal/000000.complete.json'):
            with self.subTest(name=name):
                path = self.root/name
                saved = path.read_bytes()
                path.unlink()
                self.assertTrue(R.status(self.root)['mutation_blocked'])
                with self.assertRaises((OSError, ValueError)):
                    self.initialize()
                path.write_bytes(saved)
                path.write_bytes(b'bad')
                self.assertTrue(R.status(self.root)['mutation_blocked'])
                path.unlink()
                target = self.base/'target'
                target.write_bytes(saved)
                path.symlink_to(target)
                self.assertTrue(R.status(self.root)['mutation_blocked'])
                path.unlink()
                path.write_bytes(saved)

    def test_missing_journal_after_publication_cannot_adopt_chain(self):
        args = self.buy_args()
        self.cli(args, 'publication_dir_sync')
        (self.root/'journal/000002.intent.json').unlink()  # simulate loss/corruption, not a recovery action
        cp = self.head()
        self.assertTrue(R.status(self.root)['mutation_blocked'])
        self.assertEqual(self.cli(args).returncode, 2)
        self.assertEqual(cp, self.head())

    def test_unexplained_successor_preserved_and_blocks_reconciliation(self):
        args = self.buy_args()
        self.cli(args, 'publication_dir_sync')
        parent = self.head()
        p = H.L.verify(parent).position['plan']
        now = p['checks'][0]
        successor = H.adv(parent, 'observe', {'now': now, 'bid': '97', 'mark_at': now, 'exit_fee': H.appl(p, 'SELL', now)})
        H.L.save(successor, self.root/'chain')  # explicit demonstration of raw-API bypass
        before = self.tree()
        with self.assertRaisesRegex(R.Blocked, 'unexplained successor'):
            R.reconcile(self.root, 'buy', parent['checkpoint_sha256'])
        self.assertTrue(R.status(self.root)['mutation_blocked'])
        self.assertEqual(before, self.tree())
        self.assertEqual(len(H.L.verify_chain_dir(self.root/'chain')), 4)

    def test_completed_evidence_requires_resync_before_duplicate_or_new_mutation(self):
        before = self.tree()
        with patch.object(R, '_sync', side_effect=OSError('sync unavailable')):
            with self.assertRaises(OSError):
                self.initialize()
            with self.assertRaises(OSError):
                self.decided()
        self.assertEqual(before, self.tree())

    def test_concurrent_caller_lock_and_stale_lock_restart(self):
        command = [sys.executable, '-I', '-B', *FLAGS, str(DRIVER), R.__file__, 'hold_lock', str(self.root), '1']
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([child.stdout], [], [], 10)
            self.assertTrue(ready, 'lock child did not become ready')
            self.assertEqual(child.stdout.readline().strip(), 'LOCK_HELD')
            before = self.tree()
            self.assertEqual(self.cli(self.init_args).returncode, 2)
            self.assertEqual(before, self.tree())
            lock_inode = (self.root/'lock').stat().st_ino
        finally:
            child.kill()
            out, err = child.communicate(timeout=10)
            TRACE.append({'test': self.id(), 'argv': command, 'exit': child.returncode, 'stdout': out, 'stderr': err})
        self.assertEqual((self.root/'lock').stat().st_ino, lock_inode)
        self.assertEqual(self.cli(self.init_args).returncode, 0)
        self.assertFalse(R.status(self.root)['mutation_blocked'])

    def test_bootstrap_failure_cannot_publish_or_silently_resume(self):
        self.root = self.base/'failed_bootstrap'
        original = R._write_record
        def fail(path, data):
            if Path(path).name == 'identity.json':
                raise OSError('identity persistence failed')
            return original(path, data)
        with patch.object(R, '_write_record', fail):
            with self.assertRaises(OSError):
                self.initialize()
        self.assertEqual(list((self.root/'chain').iterdir()), [])
        with self.assertRaises((OSError, ValueError)):
            self.initialize()
        self.assertTrue(R.status(self.root)['mutation_blocked'])

    def test_raw_verify_valid_does_not_clear_pending(self):
        args = self.buy_args()
        self.cli(args, 'publication_dir_sync')
        before = self.tree()
        command = [sys.executable, '-I', '-B', *FLAGS, H.L.__file__, 'verify', '--chain', str(self.root/'chain')]
        p = subprocess.run(command, text=True, capture_output=True, timeout=20)
        TRACE.append({'test': self.id(), 'argv': command, 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
        self.assertEqual(p.returncode, 0)
        self.assertTrue(json.loads(p.stdout)['valid'])
        self.assertTrue(R.status(self.root)['mutation_blocked'])
        self.assertEqual(before, self.tree())

    def test_before_after_uncertain_buy_blocks_next_transition_only_through_caller(self):
        plan = H.xplan(H.B, contract=False)
        rawchain = self.base/'raw_chain'
        g = H.genesis()
        cp = H.adv(g, 'decide_entry', {'p': plan, 'now': H.B})
        H.L.save(g, rawchain)
        H.L.save(cp, rawchain)
        ops = self.base/'raw_buy.json'
        ops.write_bytes(self.operation('execute', H.inputs(plan, H.B, 60)))
        receipt = self.base/'raw_receipt.json'
        receipt.write_text(json.dumps(H.rcpt(cp, 40)))
        def raw_cli(args, fault=False):
            command = [sys.executable, '-I', '-B', *FLAGS]
            if fault:
                command += [str(Path(__file__).with_name('raw_fault_driver.py'))]
            command += [H.L.__file__, *map(str, args)]
            result = subprocess.run(command, text=True, capture_output=True, timeout=20)
            TRACE.append({'test': self.id(), 'role': 'before_raw_cli', 'argv': command,
                          'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
            return result
        failed = raw_cli(['advance', '--chain', rawchain, '--operations', ops,
                          '--expected-current-sha256', cp['checkpoint_sha256'], '--checkpoint-library-id', H.LIB,
                          '--publication-receipt', receipt], True)
        self.assertEqual(failed.returncode, 1)
        visible = H.L.verify_chain_dir(rawchain)[-1]
        self.assertTrue(json.loads(raw_cli(['verify', '--chain', rawchain]).stdout)['valid'])
        now = H.at(H.B, 61)
        followup = {'now': now, 'bid': '100', 'mark_at': now, 'exit_fee': H.appl(plan, 'SELL', now)}
        ops.write_bytes(self.operation('observe', followup))
        self.assertEqual(raw_cli(['advance', '--chain', rawchain, '--operations', ops,
                                 '--expected-current-sha256', visible['checkpoint_sha256'],
                                 '--checkpoint-library-id', H.LIB]).returncode, 0)
        self.assertEqual(H.L.verify_chain_dir(rawchain)[-1]['generation'], 3)
        args = self.buy_args()
        self.assertEqual(self.cli(args, 'publication_dir_sync').returncode, 2)
        before = self.tree()
        followup_args = self.args('next-observation', 'observe', followup)
        self.assertEqual(self.cli(followup_args).returncode, 2)
        self.assertEqual(self.head()['generation'], 2)
        self.assertEqual(before, self.tree())

    def test_two_process_buy_requests_commit_at_most_once(self):
        args = self.buy_args()
        command = [sys.executable, '-I', '-B', *FLAGS, R.__file__, *args]
        children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(2)]
        results = []
        try:
            for child in children:
                out, err = child.communicate(timeout=20)
                results.append(child.returncode)
                TRACE.append({'test': self.id(), 'argv': command, 'exit': child.returncode,
                              'stdout': out, 'stderr': err})
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)
        self.assertIn(0, results)
        self.assertTrue(all(code in (0, 2) for code in results))
        self.assertEqual(self.head()['generation'], 2)
        self.assertEqual([e['type'] for e in H.L.verify(self.head()).events].count('BUY'), 1)

    def test_changed_code_identity_or_visible_result_cannot_reconcile(self):
        identity = R._identity()
        with patch.object(R, '_identity', return_value=identity | {'caller_sha256': '0'*64}):
            self.assertTrue(R.status(self.root)['mutation_blocked'])
            with self.assertRaisesRegex(R.Blocked, 'identity changed'):
                self.initialize()
        args = self.buy_args()
        self.cli(args, 'publication_dir_sync')
        cp = self.head()
        expected = cp['checkpoint_sha256']
        cp['status'] = 'SYNTHETIC_DIFFERENT_VISIBLE_RESULT'
        cp['checkpoint_sha256'] = H.L.seal(cp)
        H.L.verify(cp)
        (self.root/'chain/generation-000002.json').write_bytes(H.L.canonical(cp)+b'\n')
        before = self.tree()
        with self.assertRaisesRegex(R.Blocked, 'differs from intent'):
            R.reconcile(self.root, 'buy', expected)
        self.assertTrue(R.status(self.root)['mutation_blocked'])
        self.assertEqual(before, self.tree())


if __name__ == '__main__':
    unittest.main(verbosity=2)
