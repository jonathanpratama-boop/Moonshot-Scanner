"""Before-case harness: unmodified raw CLI with failed directory fsync."""
import os
import runpy
import stat
import sys

original = os.fsync
def injected(fd):
    if stat.S_ISDIR(os.fstat(fd).st_mode):
        raise OSError(5, 'synthetic directory sync failure')
    return original(fd)
os.fsync = injected
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
