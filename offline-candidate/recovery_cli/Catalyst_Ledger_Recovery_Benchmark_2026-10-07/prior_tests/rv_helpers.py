"""Reviewer-owned synthetic helpers. CANDIDATE_ROOT env selects the tree under test."""
import os,sys,copy,json
from pathlib import Path
from datetime import timedelta
ROOT=Path(os.environ['CANDIDATE_ROOT']).resolve()
sys.path[:0]=[str(ROOT/'catalyst_paper_v2'),str(ROOT/'catalyst_paper_delivery')]
import catalyst as C
import ledger as L
from test_catalyst import plan as base_plan, quote, preview as base_preview, T
B='2026-10-05T19:31:00Z'; LIB='REVIEWER_SYNTHETIC_ONLY'
def at(base,s=0): return C.iso(C.ts(base)+timedelta(seconds=s))
def shift(x,d):
    if isinstance(x,dict): return {k:shift(v,d) for k,v in x.items()}
    if isinstance(x,list): return [shift(v,d) for v in x]
    if isinstance(x,str) and len(x)==20 and x[10]=='T' and x.endswith('Z'): return C.iso(C.ts(x)+d)
    return x
def xplan(base=T,dr=900,rq=120,contract=True):
    p=shift(base_plan(),C.ts(base)-C.ts(T))
    if contract: p['entry_timing']={'max_decision_to_readback_seconds':dr,'max_first_readback_to_quote_seconds':rq,'policy_reference':'REVIEWER SYNTHETIC ONLY - NOT APPROVED'}
    return p
def appl(p,side,checked,retrieved=None):
    fz=p['open_fee' if side=='BUY' else 'close_fee']
    return dict(copy.deepcopy(fz),checked_at=checked,retrieved_at=retrieved or checked,applicability_source='REVIEWER synthetic recheck '+side,terms_hash=C.digest(C.fee_terms(fz)) if hasattr(C,'fee_terms') else 'n/a')
def inputs(p,base,sec,qid=None):
    q=quote(at(base,sec-5),qid=qid or 'Q'+str(sec)); pr=base_preview(p,q)
    pr.update(at=at(base,sec-3),retrieved=at(base,sec-2),evidence_checked_at=at(base,sec-3))
    pr['fee_applicability']=appl(p,'BUY',at(base,sec-3),at(base,sec-2))
    return dict(q=q,now=at(base,sec),preview=pr,exit_fee=appl(p,'SELL',at(base,sec-2),at(base,sec-1)))
def genesis():
    src=ROOT/'catalyst_paper_v2/legacy/checkpoint.json'
    return L.initialize(src,L.sha(src.read_bytes()),'2026-10-05T19:30:00Z',{'checkpoint':{'id':LIB,'version':12},'workbook':{'id':LIB,'version':11}},True,True)
def adv(cp,method,kw,r=None): return L.advance(cp,[{'method':method,'kwargs':kw}],cp['checkpoint_sha256'],LIB,r)
def decided(**k):
    p=xplan(B,**k); return adv(genesis(),'decide_entry',{'p':p,'now':B}),p
def rcpt(cp,sec,ver=None):
    return {'checkpoint_sha256':cp['checkpoint_sha256'],'checkpoint_library_id':LIB,'checkpoint_library_version':ver or cp.get('last_verified_library_version',0)+1,'readback_verified':True,'verified_at_utc':at(B,sec)}
def jsonish(text): return json.loads(text)   # emulate CLI json.loads of operation files
def entered_adapter(**k):
    cp,p=decided(**k); cp=adv(cp,'execute',inputs(p,B,60),rcpt(cp,40))
    assert L.verify(cp).position is not None
    return cp,p
