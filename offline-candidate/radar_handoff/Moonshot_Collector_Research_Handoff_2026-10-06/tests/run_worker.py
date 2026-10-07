import json
import os
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parent.parent
CANDIDATE = ROOT/'candidate/Moonshot_Radar_Operational_Candidate_2026-10-06'
sys.path[:0] = [str(ROOT/'tests'), str(CANDIDATE/'candidate'), str(CANDIDATE/'tests')]
import test_handoff as T
import test_recovered_contracts as A
import test_radar as R
suite = unittest.defaultTestLoader.loadTestsFromTestCase(T.HandoffTests)
# Finite relevant regression selection; none calls execution or persistence.
suite.addTest(A.RecoveredContractTests('test_capture_clocks_and_claims_remain_separate'))
suite.addTest(A.RecoveredContractTests('test_no_forecast_gate_added_to_paper_dossier'))
suite.addTest(R.RadarTests('test_08_exact_stock_crypto_parity_missing_and_wrong_contract'))
suite.addTest(R.RadarTests('test_08_offline_only_frozen_baseline_hashes'))
class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.passed = []
    def addSuccess(self, test):
        super().addSuccess(test); self.passed.append(test.id())
result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
report = {'successful': result.wasSuccessful(), 'tests_run': result.testsRun, 'passed': result.passed,
          'failures': [(str(t), trace) for t, trace in result.failures],
          'errors': [(str(t), trace) for t, trace in result.errors], 'skipped': result.skipped,
          'optimize': sys.flags.optimize, 'pid': os.getpid(), 'python': sys.version,
          'subprocesses': T.TRACE, 'execution_tests_run': False}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2)+'\n')
raise SystemExit(0 if result.wasSuccessful() else 1)
