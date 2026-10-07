import json,sys,unittest,os
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import test_radar
suite=unittest.defaultTestLoader.loadTestsFromModule(test_radar)
class Result(unittest.TextTestResult):
    def __init__(self,*a,**kw):super().__init__(*a,**kw);self.passed=[]
    def addSuccess(self,test):super().addSuccess(test);self.passed.append(test.id())
r=unittest.TextTestRunner(verbosity=2,resultclass=Result).run(suite)
data={'successful':r.wasSuccessful(),'tests_run':r.testsRun,'passed':r.passed,'failures':[(str(t),v) for t,v in r.failures],'errors':[(str(t),v) for t,v in r.errors],'skips':[(str(t),v) for t,v in r.skipped],'optimize':sys.flags.optimize,'pid':os.getpid(),'python':sys.version,'actual_frozen_astra_replay':'BLOCKED_MISSING_ARTIFACTS'}
Path(sys.argv[1]).write_text(json.dumps(data,indent=2)+'\n')
raise SystemExit(0 if r.wasSuccessful() else 1)
