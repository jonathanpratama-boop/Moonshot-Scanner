"""Packaging utility: record actual subprocess argv without altering tests."""
import json
import os
from pathlib import Path
import runpy
import sys

trace = []
def audit(event, args):
    if event == 'subprocess.Popen':
        executable, argv, cwd, _env = args
        trace.append({'executable': str(executable), 'argv': [str(x) for x in argv],
                      'cwd': str(cwd) if cwd else None,
                      'explicit_optimized_flag': any(x in ('-O', '-OO') for x in argv)})
sys.addaudithook(audit)
script = Path(sys.argv[1]).resolve()
sys.argv = [str(script), *sys.argv[2:]]
sys.path.insert(0, str(script.parent))
try:
    runpy.run_path(str(script), run_name='__main__')
finally:
    Path(os.environ['BENCHMARK_TRACE']).write_text(json.dumps({
        'worker_optimize': sys.flags.optimize, 'worker_argv': sys.orig_argv,
        'child_processes': trace}, indent=2) + '\n')
