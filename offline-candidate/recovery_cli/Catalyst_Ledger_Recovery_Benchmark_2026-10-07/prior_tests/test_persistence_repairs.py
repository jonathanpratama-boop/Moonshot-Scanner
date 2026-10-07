"""Focused persistence repair regressions, offline and synthetic only.

These are independent tests of the supplied R3-A06 and R3-C08 reproductions,
not recovered original R3 coverage. The original r3_scenarios.py is missing.
The small rejected-quote fixture below follows the existing B-review fixture.
900/120 timing values in rv_helpers are synthetic inputs, not approved policy.

Set CANDIDATE_ROOT to the candidate tree. Runs directly with -I -B and -I -B -O.
All fixture actions are outside Python assert statements.
"""

import copy
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rv_helpers import B, L, ROOT, adv, decided, genesis, inputs, rcpt


def rejected_inputs(p):
    kw = inputs(p, B, 65)
    kw['q']['product'] = 'CFD'
    return kw


def rejected_successor(cp, p, receipt=None):
    return adv(cp, 'execute', rejected_inputs(p), receipt or rcpt(cp, 40))


def filenames(directory):
    return sorted(p.name for p in Path(directory).iterdir())


class PrelinkFault:
    """Inject a single pre-publication I/O fault, leaving real I/O otherwise."""

    def __init__(self, stage):
        self.stage = stage
        self.real_open = Path.open
        self.real_fsync = os.fsync
        self.real_link = os.link

    def open(self, path, mode='r', *args, **kwargs):
        if 'x' in mode and self.stage == 'tmp_create':
            raise OSError(errno.ENOSPC, 'synthetic temporary creation failure')
        stream = self.real_open(path, mode, *args, **kwargs)
        if 'x' in mode and self.stage == 'partial_write':
            real_write = stream.write

            def partial(data):
                real_write(data[:len(data) // 2])
                raise OSError(errno.ENOSPC, 'synthetic partial write failure')

            stream.write = partial
        return stream

    def fsync(self, fd):
        if self.stage == 'file_fsync' and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, 'synthetic file fsync failure')
        return self.real_fsync(fd)

    def link(self, source, target):
        if self.stage == 'link':
            raise OSError(errno.EPERM, 'synthetic unsupported hard link')
        return self.real_link(source, target)

    def __enter__(self):
        self.stack = ExitStack()
        self.stack.enter_context(mock.patch.object(
            Path, 'open', lambda path, mode='r', *a, **k: self.open(path, mode, *a, **k)))
        self.stack.enter_context(mock.patch.object(os, 'fsync', self.fsync))
        self.stack.enter_context(mock.patch.object(os, 'link', self.link))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


class PersistenceRepairs(unittest.TestCase):
    def tempdir(self):
        temporary = tempfile.TemporaryDirectory(prefix='persistence-repair-')
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def chain_start(self):
        directory = self.tempdir()
        g = genesis()
        cp, p = decided()
        L.save(g, directory)
        return directory, g, cp, p

    def assert_uncertain(self, exc, cp, directory):
        self.assertIsInstance(exc, OSError)
        self.assertEqual(type(exc).__name__, 'PublicationUncertain')
        self.assertEqual(exc.errno, errno.EIO)
        self.assertEqual(Path(exc.path), directory / ('generation-%06d.json' % cp['generation']))
        self.assertEqual(exc.checkpoint_sha256, cp['checkpoint_sha256'])
        self.assertIs(exc.published, True)
        self.assertIs(exc.durability_confirmed, False)
        self.assertIsInstance(exc.__cause__, OSError)
        self.assertEqual(exc.__cause__.errno, errno.EIO)

    def assert_chain(self, directory, cps):
        readback = L.verify_chain_dir(directory)
        self.assertEqual([cp['checkpoint_sha256'] for cp in readback],
                         [cp['checkpoint_sha256'] for cp in cps])
        self.assertEqual(filenames(directory),
                         ['generation-%06d.json' % cp['generation'] for cp in cps])
        for cp in readback:
            path = directory / ('generation-%06d.json' % cp['generation'])
            self.assertEqual(path.read_bytes(), L.canonical(cp) + b'\n')

    def test_A06_nonstring_receipt_keys_refused_before_publication(self):
        """Exact supplied A06 metadata: numeric key order changes on JSON readback."""
        directory, g, cp1, p = self.chain_start()
        L.save(cp1, directory)
        receipt = rcpt(cp1, 40)
        receipt['svc'] = {10: 'a', 9: 'b'}
        cp2 = rejected_successor(cp1, p, receipt)
        before = copy.deepcopy(cp2)
        L.verify(cp2)  # Establish the old in-memory verifier accepts this checkpoint.
        with self.assertRaises((ValueError, TypeError)):
            L.save(cp2, directory)
        self.assertEqual(cp2, before)
        self.assert_chain(directory, [g, cp1])

    def test_A06_preflight_refusal_does_not_create_output_directory(self):
        cp1, p = decided()
        receipt = rcpt(cp1, 40)
        receipt['svc'] = {10: 'a', 9: 'b'}
        cp2 = rejected_successor(cp1, p, receipt)
        directory = self.tempdir() / 'absent' / 'chain'
        real_mkdir = Path.mkdir
        with mock.patch.object(Path, 'mkdir', autospec=True, side_effect=real_mkdir) as mkdir:
            with self.assertRaises((ValueError, TypeError)):
                L.save(cp2, directory)
        mkdir.assert_not_called()
        self.assertFalse(directory.parent.exists())

    def test_json_safe_receipt_metadata_saves_and_verifies_exact_bytes(self):
        directory, g, cp1, p = self.chain_start()
        L.save(cp1, directory)
        receipt = rcpt(cp1, 40)
        receipt['svc'] = {'10': 'a', '9': 'b', 'unicode': 'Jakarta / é / 中文',
                          'nested': [True, None, {'source': 'synthetic'}]}
        cp2 = rejected_successor(cp1, p, receipt)
        before = copy.deepcopy(cp2)
        saved = L.save(cp2, directory)
        self.assertEqual(saved, directory / 'generation-000002.json')
        self.assertEqual(cp2, before)
        self.assert_chain(directory, [g, cp1, cp2])

    def test_C08_directory_fsync_failure_retains_generation_and_reports_uncertainty(self):
        directory, g, cp1, p = self.chain_start()
        real_fsync = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, 'synthetic directory fsync failure')
            return real_fsync(fd)

        with mock.patch.object(os, 'fsync', fail_directory):
            with self.assertRaises(OSError) as raised:
                L.save(cp1, directory)
        self.assert_chain(directory, [g, cp1])
        self.assert_uncertain(raised.exception, cp1, directory)
        with self.assertRaises(FileExistsError):
            L.save(cp1, directory)
        cp2 = rejected_successor(L.verify_chain_dir(directory)[-1], p)
        L.save(cp2, directory)
        self.assert_chain(directory, [g, cp1, cp2])

    def test_C08_directory_open_failure_retains_generation(self):
        directory, g, cp1, _ = self.chain_start()
        real_open = os.open

        def fail_directory(path, *args, **kwargs):
            if Path(path) == directory:
                raise OSError(errno.EIO, 'synthetic directory open failure')
            return real_open(path, *args, **kwargs)

        with mock.patch.object(os, 'open', fail_directory):
            with self.assertRaises(OSError) as raised:
                L.save(cp1, directory)
        self.assert_chain(directory, [g, cp1])
        self.assert_uncertain(raised.exception, cp1, directory)

    def test_C08_directory_close_failure_retains_generation(self):
        directory, g, cp1, _ = self.chain_start()
        real_close = os.close

        def fail_directory(fd):
            is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            real_close(fd)  # Do not leak test descriptors while simulating close failure.
            if is_directory:
                raise OSError(errno.EIO, 'synthetic directory close failure')

        with mock.patch.object(os, 'close', fail_directory):
            with self.assertRaises(OSError) as raised:
                L.save(cp1, directory)
        self.assert_chain(directory, [g, cp1])
        self.assert_uncertain(raised.exception, cp1, directory)

    def test_C08_interleaved_committed_successor_is_not_orphaned(self):
        """Supplied C08 interleave, with desired corrected chain invariant."""
        directory, g, cp1, p = self.chain_start()
        real_fsync = os.fsync
        successors = []

        def fail_after_successor(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                head = L.verify_chain_dir(directory)[-1]
                self.assertEqual(head['generation'], 1)
                cp2 = rejected_successor(head, p)
                with mock.patch.object(os, 'fsync', real_fsync):
                    L.save(cp2, directory)
                successors.append(cp2)
                raise OSError(errno.EIO, 'synthetic parent fsync failure after successor commit')
            return real_fsync(fd)

        with mock.patch.object(os, 'fsync', fail_after_successor):
            with self.assertRaises(OSError) as raised:
                L.save(cp1, directory)
        self.assertEqual(len(successors), 1)
        self.assert_chain(directory, [g, cp1, successors[0]])
        self.assert_uncertain(raised.exception, cp1, directory)

    def test_C08_separate_process_commits_successor_before_parent_fsync_fails(self):
        """A child really reads visible generation 1 and durably saves generation 2."""
        directory, g, cp1, _ = self.chain_start()
        real_fsync = os.fsync
        child_results = []

        def fail_after_child(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                command = [sys.executable, '-I', '-B']
                if sys.flags.optimize:
                    command.append('-O')
                command.extend([str(Path(__file__).resolve()), '--write-successor', str(directory)])
                completed = subprocess.run(command, capture_output=True, text=True,
                                           timeout=30, env=os.environ.copy())
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                child_results.append(json.loads(completed.stdout))
                raise OSError(errno.EIO, 'synthetic parent fsync failure after child commit')
            return real_fsync(fd)

        with mock.patch.object(os, 'fsync', fail_after_child):
            with self.assertRaises(OSError) as raised:
                L.save(cp1, directory)
        self.assertEqual(len(child_results), 1)
        self.assertEqual(child_results[0]['count'], 3)
        cps = L.verify_chain_dir(directory)
        self.assertEqual(cps[-1]['checkpoint_sha256'], child_results[0]['checkpoint_sha256'])
        self.assert_chain(directory, [g, cp1, cps[-1]])
        self.assert_uncertain(raised.exception, cp1, directory)

    def test_successful_looking_short_or_changed_write_refused_before_publication(self):
        """A stream can return without error while storing different bytes."""
        for fault in ('short_write', 'changed_bytes'):
            with self.subTest(fault=fault):
                directory, g, cp1, _ = self.chain_start()
                real_open = Path.open

                def faulty_open(path, mode='r', *args, **kwargs):
                    stream = real_open(path, mode, *args, **kwargs)
                    if 'x' in mode:
                        real_write = stream.write

                        def write(data):
                            if fault == 'short_write':
                                return real_write(data[:len(data) // 2])
                            return real_write(data[:-1] + b' ')

                        stream.write = write
                    return stream

                with mock.patch.object(Path, 'open', faulty_open):
                    with self.assertRaises(OSError) as raised:
                        L.save(cp1, directory)
                self.assertEqual(raised.exception.errno, errno.EIO)
                self.assertNotEqual(type(raised.exception).__name__, 'PublicationUncertain')
                self.assert_chain(directory, [g])
                L.save(cp1, directory)
                self.assert_chain(directory, [g, cp1])

    def test_prelink_io_failures_clean_up_and_allow_retry(self):
        for stage in ('tmp_create', 'partial_write', 'file_fsync', 'link'):
            with self.subTest(stage=stage):
                directory, g, cp1, _ = self.chain_start()
                with PrelinkFault(stage):
                    with self.assertRaises(OSError) as raised:
                        L.save(cp1, directory)
                self.assertNotEqual(type(raised.exception).__name__, 'PublicationUncertain')
                self.assert_chain(directory, [g])
                L.save(cp1, directory)
                self.assert_chain(directory, [g, cp1])

    def test_successful_same_content_retry_still_refuses_overwrite(self):
        directory, g, cp1, _ = self.chain_start()
        saved = L.save(cp1, directory)
        original_bytes = saved.read_bytes()
        with self.assertRaises(FileExistsError):
            L.save(cp1, directory)
        self.assertEqual(saved.read_bytes(), original_bytes)
        self.assert_chain(directory, [g, cp1])

    def test_temporary_readback_io_failure_refused_before_publication(self):
        directory, g, cp1, _ = self.chain_start()
        real_read_bytes = Path.read_bytes

        def fail_temporary_readback(path):
            if path.name.startswith('.ledger-') and path.name.endswith('.tmp'):
                raise OSError(errno.EIO, 'synthetic temporary-file readback failure')
            return real_read_bytes(path)

        with mock.patch.object(Path, 'read_bytes', fail_temporary_readback):
            with self.assertRaises(OSError) as raised:
                L.save(cp1, directory)
        self.assertEqual(raised.exception.errno, errno.EIO)
        self.assertNotEqual(type(raised.exception).__name__, 'PublicationUncertain')
        self.assert_chain(directory, [g])
        L.save(cp1, directory)
        self.assert_chain(directory, [g, cp1])


def write_child_successor(directory):
    head = L.verify_chain_dir(directory)[-1]
    if head['generation'] != 1:
        raise ValueError('child expected newly visible generation 1')
    plan = L.verify(head).pending['plan']
    cp2 = rejected_successor(head, plan)
    L.save(cp2, directory)
    cps = L.verify_chain_dir(directory)
    print(json.dumps({'count': len(cps), 'checkpoint_sha256': cps[-1]['checkpoint_sha256']}))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--write-successor':
        write_child_successor(Path(sys.argv[2]))
    else:
        unittest.main(verbosity=2)
