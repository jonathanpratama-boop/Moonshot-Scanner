"""Test-only process fault injection; never imported by the production caller."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch

module_path, fault, root_arg, sequence_arg = sys.argv[1:5]
root = Path(root_arg)
sequence = int(sequence_arg)
spec = importlib.util.spec_from_file_location('recovery', module_path)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
sys.stderr.write('TEST_PROCESS=' + json.dumps({'optimize': sys.flags.optimize, 'pid': os.getpid(), 'fault': fault}) + '\n')
intent_path = root / 'journal' / f'{sequence:06d}.intent.json'
complete_path = root / 'journal' / f'{sequence:06d}.complete.json'
generation = root / 'chain' / f'generation-{sequence:06d}.json'
write = R._write_record
sync = R._sync
save = R.L.save
ack = R._ack
fsync = os.fsync


def failure():
    raise OSError(errno.EIO, 'injected ' + fault)


def same_fd(fd, path):
    try:
        a, b = os.fstat(fd), path.stat()
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except FileNotFoundError:
        return False


def fsync_fault(fd):
    if fault == 'publication_dir_sync' and generation.exists() and same_fd(fd, root/'chain'):
        failure()
    if fault == 'intent_file_sync' and same_fd(fd, intent_path):
        failure()
    if fault == 'completion_file_sync' and same_fd(fd, complete_path):
        failure()
    return fsync(fd)


def write_fault(path, data):
    path = Path(path)
    if path == intent_path:
        if fault == 'intent_before':
            failure()
        if fault in ('intent_partial', 'crash_intent_partial'):
            with path.open('xb') as f:
                f.write(b'{"part')
                f.flush()
                fsync(f.fileno())
            if fault.startswith('crash'):
                os._exit(71)
            failure()
        write(path, data)
        if fault == 'after_intent':
            failure()
        if fault == 'crash_after_intent':
            os._exit(72)
        return
    if path == complete_path:
        if fault == 'completion_before':
            failure()
        if fault == 'completion_partial':
            with path.open('xb') as f:
                f.write(b'{"part')
                f.flush()
                fsync(f.fileno())
            failure()
        write(path, data)
        if fault == 'crash_after_completion':
            os._exit(74)
        return
    return write(path, data)


def sync_fault(path):
    path = Path(path)
    if fault == 'intent_dir_sync' and path == root/'journal' and intent_path.exists():
        failure()
    if fault == 'result_resync' and path == generation:
        failure()
    if fault == 'completion_dir_sync' and path == root/'journal' and complete_path.exists():
        failure()
    return sync(path)


def save_fault(cp, directory):
    if fault == 'save_before':
        failure()
    result = save(cp, directory)
    if fault == 'crash_after_publication':
        os._exit(73)
    return result


def ack_fault(*args, **kwargs):
    if fault == 'ack':
        failure()
    return ack(*args, **kwargs)


if fault == 'hold_lock':
    with R._locked(root):
        print('LOCK_HELD', flush=True)
        signal.pause()
else:
    with patch.object(R, '_write_record', write_fault), patch.object(R, '_sync', sync_fault), \
         patch.object(R.L, 'save', save_fault), patch.object(R, '_ack', ack_fault), patch.object(os, 'fsync', fsync_fault):
        raise SystemExit(R.main(sys.argv[5:]))
