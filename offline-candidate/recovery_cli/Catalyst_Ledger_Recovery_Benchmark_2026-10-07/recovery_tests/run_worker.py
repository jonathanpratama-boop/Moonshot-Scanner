import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_recovery as T

result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(T))
report = {'python': sys.version, 'optimize': sys.flags.optimize, 'tests_run': result.testsRun,
          'failures': [(t.id(), detail) for t, detail in result.failures],
          'errors': [(t.id(), detail) for t, detail in result.errors],
          'skips': [(t.id(), reason) for t, reason in result.skipped],
          'expected_failures': [t.id() for t, _ in result.expectedFailures],
          'successful': result.wasSuccessful(), 'child_processes': T.TRACE}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2)+'\n')
raise SystemExit(not result.wasSuccessful())
