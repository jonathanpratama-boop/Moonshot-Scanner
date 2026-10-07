"""Offline-only catalyst paper state machine. No network, broker, scheduler or persistence.
Evidence assertions are inputs requiring external verification, not automated fact checks.
"""
from copy import deepcopy
from functools import wraps
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, InvalidOperation
import hashlib, json, math
from accounting import D, cash_round, append_event, verify_chain, validate_checkpoint

VERSION = 'PAPER-CATALYST-v2-RC1'
class Blocked(ValueError): pass

def require(ok, reason):
    if not ok: raise Blocked(reason)
def ts(s):
    require(isinstance(s,str),'timestamp must be a UTC string')
    t=datetime.fromisoformat(s.replace('Z','+00:00'))
    require(t.tzinfo is not None and t.utcoffset()==timedelta(0),'UTC timestamp required')
    return t
def iso(t): return t.isoformat().replace('+00:00','Z')
def encode(x):
    if isinstance(x,Decimal): return {'__decimal__':str(x)}
    if isinstance(x,datetime): return {'__datetime__':iso(x)}
    if isinstance(x,set): return {'__set__':sorted(x)}
    if isinstance(x,dict): return {k:encode(v) for k,v in x.items()}
    if isinstance(x,list): return [encode(v) for v in x]
    return x
def decode(x):
    if isinstance(x,dict):
        if set(x)=={'__decimal__'}: return number(x['__decimal__'])
        if set(x)=={'__datetime__'}: return ts(x['__datetime__'])
        if set(x)=={'__set__'}: return set(x['__set__'])
        return {k:decode(v) for k,v in x.items()}
    if isinstance(x,list): return [decode(v) for v in x]
    return x
def digest(x): return hashlib.sha256(json.dumps(encode(x),sort_keys=True,separators=(',',':')).encode()).hexdigest()
def number(x):
    try: v=D(x)
    except (InvalidOperation,ValueError,TypeError): raise Blocked('invalid decimal')
    require(v.is_finite(),'nonfinite number'); return v

RESERVED_TAGS=({'__decimal__'},{'__datetime__'},{'__set__'})
# Sanitizer outputs. Literal inputs of the same shape are escaped too, so distinct inputs never alias.
MARKER_SHAPES=RESERVED_TAGS+({'unencodable_text'},{'unloggable_input'},{'reserved_tag_object'},{'nonfinite_number'})
KEY_PREFIXES=('unencodable_key:','literal_key:','nonstr_key:')
def escaped_text(s):
    # Double backslashes first so backslashreplace output stays injective.
    return s.replace('\\','\\\\').encode('utf-8','backslashreplace').decode('utf-8')
def loggable(x,depth=0):
    """Round-trip-safe event payload. Untrusted input text/shape must never block a commit:
    lone surrogates cannot be UTF-8 sealed, reserved tag shapes would be decoded on restore and
    non-finite numbers are not strict JSON. Ordinary JSON values are returned unchanged."""
    if depth>64: return {'unloggable_input':'nesting depth exceeded'}
    if isinstance(x,str):
        try: x.encode('utf-8'); return x
        except UnicodeEncodeError: return {'unencodable_text':escaped_text(x)}
    if isinstance(x,float) and not math.isfinite(x): return {'nonfinite_number':repr(x)}
    if isinstance(x,list): return [loggable(v,depth+1) for v in x]
    if isinstance(x,dict):
        out={}
        for k,v in x.items():
            if isinstance(k,str): key=k
            else:
                try: key='nonstr_key:'+repr(k)
                except Exception:
                    # Oversized ints and hostile __repr__ methods must not erase a commit.
                    # The existing reserved prefix also escapes literal look-alike keys.
                    key='nonstr_key:unloggable_key:'+type(k).__name__
            try:
                key.encode('utf-8')
                if isinstance(k,str) and key.startswith(KEY_PREFIXES): key='literal_key:'+key
            except UnicodeEncodeError: key='unencodable_key:'+escaped_text(key)
            base,index=key,2
            while key in out:
                key=base+':duplicate:'+str(index);index+=1
            out[key]=loggable(v,depth+1)
        return {'reserved_tag_object':{'escaped'+k:v for k,v in out.items()}} if set(out) in MARKER_SHAPES else out
    return x
def jsonable(x,depth=0,seen=frozenset()):
    """Non-raising JSON-native copy for inputs json.dumps rejects (cycles, non-string keys,
    oversized integers, deep nesting). Keeps every representable field, e.g. a quote's bid/ask."""
    if depth>64: return {'unloggable_input':'nesting depth exceeded'}
    if x is None or isinstance(x,(bool,str,float)): return x
    if isinstance(x,int):
        try: str(x); return x
        except ValueError: return {'unloggable_input':'integer of %d bits'%x.bit_length()}
    if isinstance(x,(dict,list,tuple)):
        if id(x) in seen: return {'unloggable_input':'cycle'}
        seen=seen|{id(x)}
        if isinstance(x,dict): return {k:jsonable(v,depth+1,seen) for k,v in x.items()}
        return [jsonable(v,depth+1,seen) for v in x]
    try: return str(x)
    except Exception: return {'unloggable_input':type(x).__name__}
def log_details(details):
    # Same values as json.loads(json.dumps(details,default=str)) for ordinary input, but never raises
    # and never merges distinct keys (e.g. 1 and '1').
    return loggable(jsonable(details))

PLAN_MAX_DEPTH=32
def plain_plan(p):
    """A frozen plan enters sealed state verbatim. Admit only bounded, exactly restorable JSON
    whose logged form is identical, so every later operation and restore sees the same plan."""
    stack=[(p,0)]
    while stack:
        v,d=stack.pop()
        require(d<=PLAN_MAX_DEPTH,'plan nesting too deep')
        if isinstance(v,dict):
            require(all(isinstance(k,str) for k in v),'plan keys must be strings')
            stack.extend((w,d+1) for w in v.values())
        elif isinstance(v,list): stack.extend((w,d+1) for w in v)
        elif isinstance(v,float): require(math.isfinite(v),'plan numbers must be finite')
        else: require(v is None or isinstance(v,(bool,int,str)),'plan values must be JSON')
    require(isinstance(p,dict) and loggable(p)==p,'plan contains reserved, unencodable or escaped content')

def charges(notional, profile):
    require(profile['verified'] is True and profile['unresolved'] is False,'unverified cost')
    vals=[number(profile[k]) for k in ('rate','fixed','tax','markup')]
    require(all(v>=0 for v in vals),'negative cost')
    return sum([cash_round(notional*vals[0])]+[cash_round(v) for v in vals[1:]],D(0))
def risk_gate(price_risk,cost_risk,rr):
    pr,cr,r=map(number,[price_risk,cost_risk,rr])
    return 0<pr<=D('.05') and 0<cr<=D('.05') and r>=D('1.5')

def economics(cash, ask, bid, p, slip='0.001'):
    cash,ask,bid,s=map(number,[cash,ask,bid,slip]); stop=number(p['stop']);target=number(p['target'])
    require(0<s<1 and 0<bid<=ask and 0<stop<bid and number(p['min_ask'])<=ask<=number(p['max_ask']),'price bounds')
    prec=p['precision'];require(type(prec) is int and 0<=prec<=12,'precision')
    price=ask*(1+s); step=D(1).scaleb(-prec); op=p['open_fee'];cl=p['close_fee']
    fixed=charges(D(0),op);rate=number(op['rate'])
    require(cash>fixed,'cash')
    q=((cash-fixed)/(price*(1+rate))).quantize(step,rounding=ROUND_FLOOR)
    while q>0 and q*price+charges(q*price,op)>cash:
        deficit=q*price+charges(q*price,op)-cash
        q-=max(D(1),(deficit/(price*step)).to_integral_value(rounding=ROUND_CEILING))*step
    require(q>0,'zero size'); debit=q*price+charges(q*price,op)
    require(number(p['min_units'])<=q<=number(p['max_units']) and number(p['min_notional'])<=q*price<=number(p['max_notional']),'instrument size')
    stopnet=q*stop*(1-s)-charges(q*stop*(1-s),cl)
    targetnet=q*target*(1-s)-charges(q*target*(1-s),cl)
    loss=debit-stopnet;reward=targetnet-debit
    pr=(price-stop)/price; cr=loss/debit;rr=reward/loss if loss>0 else D(0)
    return dict(units=q,debit=debit,residual=cash-debit,price=price,loss=loss,reward=reward,price_risk=pr,cost_risk=cr,rr=rr,eligible=(target>price and risk_gate(pr,cr,rr)))

def not_invalidated(p, now, reason):
    v=p.get('invalidated_at')
    require(v is None or (isinstance(v,str) and ts(now)<ts(v)),reason)

def profile(p, instrument, side, now):
    require(p['instrument']==instrument and p['currency']=='USD' and p['product']=='real' and p['side']==side,'fee binding')
    require(p['id'] and p['source'] and ts(p['verified_at'])<=ts(p['checked_at'])<=ts(now),'fee applicability clock')
    require(ts(now)-ts(p['checked_at'])<=timedelta(seconds=120),'stale applicability check')
    not_invalidated(p,now,'invalidated fee')
    require(p['holding_cost_verified_zero'] is True,'nonzero/unknown holding costs unsupported')
    charges(D(0),p)

# No new timing policy is selected by this repair. Delayed-entry support requires
# a prospectively frozen explicit contract; unchanged plans retain their old gates.
FEE_TERMS = ('id','instrument','side','product','currency','rate','fixed','tax','markup','holding_cost_verified_zero')
FEE_RECORD_FIELDS = set(FEE_TERMS) | {'verified','unresolved','source','verified_at','checked_at','invalidated_at',
                                    'applicability_source','retrieved_at','terms_hash'}

def fee_terms(p):
    require(isinstance(p,dict) and set(p)<=FEE_RECORD_FIELDS,'unsupported fee fields')
    return {k:p[k] for k in FEE_TERMS}

def unchanged_fee(current, frozen, instrument, side, now):
    profile(current,instrument,side,now)
    require(fee_terms(current)==fee_terms(frozen),'fee terms changed; reconciliation required')
    not_invalidated(frozen,now,'invalidated frozen fee')
    return dict(current)

def fresh_applicability(current, frozen, instrument, side, now, quote_at):
    """A real applicability check is distinct from retrieving an old fee document.
    Caller supplies and attests the source facts; the engine never invents clocks.
    """
    evidence=unchanged_fee(current,frozen,instrument,side,now)
    require(isinstance(evidence.get('applicability_source'),str) and bool(evidence['applicability_source'].strip()),'fee applicability source required')
    require(evidence.get('terms_hash')==digest(fee_terms(frozen)),'fee terms evidence binding')
    checked,retrieved=ts(evidence['checked_at']),ts(evidence['retrieved_at'])
    require(ts(quote_at)<=checked<=retrieved<=ts(now),'fee recheck/retrieval clock')
    return evidence

def entry_timing(p, decision):
    """None means compatibility gates, not an implicit 15-minute authorization.
    Bounds are explicit prospective inputs requiring approval outside this library.
    """
    contract=p.get('entry_timing')
    if contract is None:
        return None
    fields={'max_decision_to_readback_seconds','max_first_readback_to_quote_seconds','policy_reference'}
    require(isinstance(contract,dict) and set(contract)==fields,'entry timing contract fields')
    for k in fields-{'policy_reference'}:
        require(type(contract[k]) is int and contract[k]>0,'entry timing bounds must be positive integer seconds')
        require(contract[k]<=(ts(p['signal_expiry'])-ts(decision)).total_seconds(),'entry timing exceeds frozen signal lifetime')
    require(isinstance(contract['policy_reference'],str) and bool(contract['policy_reference'].strip()),'entry timing policy reference required')
    return contract

def in_session(p, now, entry=False):
    t=ts(now)
    if p['asset_class']=='crypto': return not entry or t.hour<20
    for sess in p['sessions']:
        require(sess['source'] and sess['verified'] is True,'unverified session')
        start,end=ts(sess['open']),ts(sess['close'])
        if start<=t<end: return not entry or t<end-timedelta(hours=3)
    return False

def validate_plan(p, now, intake_end):
    """Frozen before admission arithmetic; exact dates, numeric derivation, evidence IDs."""
    plain_plan(p)
    require(p['paper_only'] is True and p['currency']=='USD' and p['product']=='real' and p['leverage']==1,'scope')
    require(p['asset_class'] in ('stock','crypto','etf') and p['instrument'] and p['openable'] is True,'instrument')
    require(p['identity_source'] and p['precision_source'] and p['structure_source'] and p['bid_mapping_source'],'missing instrument/structure evidence')
    require(p['corporate_actions_clear_through_deadline'] is True and p['corporate_action_source'],'corporate actions blocked')
    require(p['supply_basis_verified'] is True and p['liquidity_usable'] is True,'supply/liquidity')
    d=p['dossier']
    for k in ('primary_source','mechanism','priced_in','disconfirming','sensitivity','horizon_reason','resistance_path','ordinary_variability'):
        require(bool(d[k]),'missing dossier '+k)
    require(ts(d['published_at'])<=ts(d['first_observed_at'])<=ts(now),'catalyst clocks')
    require(d['status'] in ('confirmed','conditional') and d['timing']=='dated','catalyst unresolved')
    require(d['valuation_support'] in ('direct','comparable'),'speculative valuation')
    target=d['target_derivation']
    require(target['method'] in ('equity_bridge','token_value','etf_nav','normalized_comparable'),'target method')
    require(target['source'] and target['assumptions'] and target['frozen_at'] and ts(target['frozen_at'])<=ts(now),'target not frozen')
    numerator=number(target['holder_value_low']);supply=number(target['diluted_units'])
    require(numerator>0 and supply>0 and numerator/supply==number(p['target']),'target derivation mismatch')
    expected={'stock':('equity_bridge','normalized_comparable'),'crypto':('token_value',),'etf':('etf_nav',)}
    require(target['method'] in expected[p['asset_class']],'target asset mismatch')
    for k in ('failure','partial','delivery'):
        sc=d['scenarios'][k]
        require(sc['source'] and sc['assumptions'] and number(sc['low'])>=0 and number(sc['high'])>=number(sc['low']),'scenario')
        require(ts(now)<ts(sc['by'])<=ts(p['deadline']),'scenario timing')
    require(number(d['scenarios']['delivery']['low'])==number(p['target']),'target not delivery lower bound')
    require(ts(now)<ts(p['signal_expiry'])<=ts(intake_end),'signal expiry')
    require(ts(now)<ts(p['deadline'])<=ts(now)+timedelta(days=30),'horizon cap')
    checks=[ts(x) for x in p['checks']]
    require(checks==sorted(set(checks)) and checks and checks[0]>ts(now) and checks[-1]==ts(p['deadline']),'checks/deadline')
    require(all((b-a)<=timedelta(hours=1) for a,b in zip([ts(now)]+checks,checks)),'hourly coverage missing')
    require(p['next_review']==p['checks'][0] and in_session(p,p['deadline']),'deadline not feasible')
    require(p['coverage_source'] and p['gap_stress'] and p['closed_session_plan'] and p['minute_monitoring_required'] is False,'coverage')
    overnight=ts(p['deadline']).date()>ts(now).date()
    require(not overnight or p['overnight_approved'] is True,'overnight not approved')
    require(p['checkpoints'],'no catalyst checkpoints')
    for c in p['checkpoints']:
        require(ts(now)<ts(c['at'])<=ts(p['deadline']) and c['at'] in p['checks'] and c['on_missing']=='EXIT','checkpoint')
    for ev in p['events']:
        require(ev['source'] and ts(now)<ts(ev['at']),'event evidence')
        if ts(ev['at'])<=ts(p['deadline']):
            require(ev['failure_first_exit_bid'] is not None,'uncharacterized discontinuity')
            require(ev['first_exit_source'] and ev['first_exit_at'] in p['checks'] and ts(ev['at'])<=ts(ev['first_exit_at']) and in_session(p,ev['first_exit_at']),'event first feasible observation')
            feasible=[x for x in p['checks'] if ts(x)>=ts(ev['at']) and in_session(p,x)]
            require(feasible and ev['first_exit_at']==feasible[0],'event exit not first feasible check')
    profile(p['open_fee'],p['instrument'],'BUY',now);profile(p['close_fee'],p['instrument'],'SELL',now)
    fee_terms(p['open_fee']);fee_terms(p['close_fee']);entry_timing(p,now)
    return True

def event_gate(p,e):
    for ev in p['events']:
        if ts(ev['at'])<=ts(p['deadline']):
            b=number(ev['failure_first_exit_bid']);require(b>0,'event price')
            proceeds=e['units']*b*D('.999')-charges(e['units']*b*D('.999'),p['close_fee'])
            require((e['debit']-proceeds)/e['debit']<=D('.05'),'event gap exceeds 5%')

def wait_decision(p, cash, now, intake_end, condition, evidence_due, witness_ask, witness_bid):
    """WAIT requires a concrete qualifying witness, not an unproved optimizer claim."""
    try:
        validate_plan(p,now,intake_end)
        require(condition and ts(now)<ts(evidence_due)<ts(p['signal_expiry']),'infeasible evidence timing')
        e=economics(cash,witness_ask,witness_bid,p);require(e['eligible'],'no feasible witness');event_gate(p,e)
        return {'action':'WAIT','condition':condition,'expiry':p['signal_expiry'],'witness_ask':str(witness_ask),'witness_bid':str(witness_bid),'plan_hash':digest(p)}
    except (Blocked,KeyError,ValueError,TypeError,ArithmeticError) as ex:
        return {'action':'PASS','reason':str(ex),'unsupported_fields':'not established'}

def cutover_check(source_bytes, expected_checkpoint_hash, approved, reviewed):
    require(isinstance(source_bytes,bytes),'source bytes required')
    require(hashlib.sha256(source_bytes).hexdigest()==expected_checkpoint_hash,'stale/tampered source checkpoint')
    old=json.loads(source_bytes);validate_checkpoint(old)
    s=old['state'];require(D(s['position_units'])==0 and D(s['position_paper_basis'])==0 and s['pending_action'] is None,'cutover requires flat reconciled source')
    require(s['position_state']=='CASH_ONLY' and not s.get('policy_breach'),'unresolved source state')
    require(approved is True and reviewed is True,'cutover not approved/reviewed')
    cash=number(s['cash']);require(cash>=0,'cash');return cash

def guarded(fn):
    @wraps(fn)
    def run(self,*args,**kwargs):
        current=digest({k:v for k,v in self.__dict__.items() if k!='_seal'})
        require(current==self._seal,'state integrity changed outside ledger')
        before=deepcopy(self.__dict__)
        try:
            result=fn(self,*args,**kwargs)
            verify_chain(self.events)
            self._seal=digest({k:v for k,v in self.__dict__.items() if k!='_seal'})
            return result
        except Exception:
            self.__dict__.clear();self.__dict__.update(before);raise
    return run

class Simulator:
    """Always a dry run. Each state/event stays in memory; no deployment API exists."""
    def __init__(self,cash,activation):
        self.cash=number(cash);require(self.cash>0,'cash');self.baseline=self.cash
        self.activation=activation;self.intake_end=iso(ts(activation)+timedelta(days=7))
        self.events=[];self.position=None;self.pending=None;self.used_quotes=set();self.last_quote_time=None
        self.realized=D(0);self.suspended=False;self.status='SANDBOX_ONLY';self.clock=ts(activation)
        self._seal=digest(self.__dict__)
    def log(self,kind,now,details):
        require(ts(now)>=self.clock,'regressing operation clock');self.clock=ts(now)
        append_event(self.events,{'id':str(len(self.events)+1),'type':kind,'at':now,'details':log_details(details)})
    @guarded
    def decide_entry(self,p,now):
        require(self.position is None and self.pending is None and not self.suspended,'exposure/pending/breach')
        require(ts(self.activation)<=ts(now)<ts(self.intake_end),'intake closed')
        validate_plan(p,now,self.intake_end);require(in_session(p,now,True),'entry session')
        self.log('ENTRY_DECISION',now,{'plan':p,'plan_hash':digest(p)})
        self.pending={'kind':'ENTRY','at':now,'plan':deepcopy(p),'plan_hash':digest(p)}
    def valid_quote(self,q,now,p):
        require(isinstance(q['id'],str),'quote id must be a string')
        require(q['id'] not in self.used_quotes,'reused quote')
        require(q['instrument']==p['instrument'] and q['currency']=='USD' and q['product']=='real','quote binding')
        market,retrieved,fill=ts(q['at']),ts(q['retrieved']),ts(now)
        require(ts(self.pending['at'])<market<=retrieved<=fill and fill-market<=timedelta(seconds=120) and fill-retrieved<=timedelta(seconds=120),'quote clock')
        require(self.last_quote_time is None or market>self.last_quote_time,'regressing quote')
        require(0<number(q['bid'])<=number(q['ask']),'quote prices')
        require(in_session(p,q['at']) and in_session(p,now),'quote/fill outside regular session')
    @guarded
    def reject_execution(self,q,now,reason,cancel_entry=False):
        """Adapter-only rejection after a verified receipt; preserve its audit trail.
        No quote is priced and no fee assumption, units, or cash is changed.
        """
        require(self.pending is not None,'no decision')
        require(not cancel_entry or self.pending['kind']=='ENTRY','cannot cancel an EXIT')
        self.log('FILL_GATE_FAILED' if cancel_entry else 'REJECTED_QUOTE',now,{'quote':q,'reason':reason,'adapter_rejection':True})
        if cancel_entry:self.pending=None
        return 'CANCELLED' if cancel_entry else 'PENDING_DATA'
    @guarded
    def expire_entry(self,now):
        require(self.pending is not None and self.pending['kind']=='ENTRY','expiry requires pending ENTRY')
        p=self.pending['plan'];expiry=min(ts(p['signal_expiry']),ts(self.intake_end))
        require(ts(now)>=expiry,'entry not expired')
        self.log('EXPIRED_UNFILLED',now,{'decision_at':self.pending['at'],'plan_hash':self.pending['plan_hash'],'expiry':iso(expiry)})
        self.pending=None
        return 'CANCELLED'
    @guarded
    def execute(self,q,now,preview=None,exit_fee=None):
        require(self.pending is not None,'no decision')
        p=self.pending['plan']
        if self.pending['kind']=='ENTRY' and ts(now)>=min(ts(p['signal_expiry']),ts(self.intake_end)):
            return self.expire_entry(now)
        try: self.valid_quote(q,now,p)
        except (Blocked,KeyError,ValueError,TypeError,ArithmeticError) as ex:
            self.log('REJECTED_QUOTE',now,{'quote':q,'reason':str(ex)});return 'PENDING_DATA'
        self.used_quotes.add(q['id']);self.last_quote_time=ts(q['at'])
        if self.pending['kind']=='ENTRY':
            try:
                require(digest(p)==self.pending['plan_hash'],'mutated frozen plan')
                validate_plan(p,self.pending['at'],self.intake_end)
                require(in_session(p,now,True) and ts(now)<ts(p['next_review']),'session/review passed')
                require(all(ts(now)<ts(ev['at']) for ev in p['events']),'event passed since decision')
                e=economics(self.cash,q['ask'],q['bid'],p);require(e['eligible'],'risk/reward');event_gate(p,e)
                require(preview is not None,'missing final-size preview')
                require(preview['evidence_unchanged'] is True and preview['dossier_hash']==digest(p['dossier']) and ts(q['at'])<=ts(preview['evidence_checked_at'])<=ts(now),'fresh dossier applicability required')
                require(preview['instrument']==p['instrument'] and preview['product']=='real' and preview['currency']=='USD' and preview['side']=='BUY','preview binding')
                require(number(preview['units'])==e['units'] and preview['fee_id']==p['open_fee']['id'],'preview final size/profile')
                pa,pr=ts(preview['at']),ts(preview['retrieved'])
                require(ts(q['at'])<=pa<=pr<=ts(now) and ts(now)-pa<=timedelta(seconds=120) and ts(now)-pr<=timedelta(seconds=120),'preview clock')
                timing=entry_timing(p,self.pending['at'])
                if timing is not None:
                    # Core-only synthetic use has no publication service. The adapter
                    # additionally enforces the immutable first actual readback clock.
                    require(ts(q['retrieved'])-ts(self.pending['at'])<=timedelta(seconds=timing['max_decision_to_readback_seconds']+timing['max_first_readback_to_quote_seconds']),'entry decision authority expired')
                    opening=fresh_applicability(preview.get('fee_applicability'),p['open_fee'],p['instrument'],'BUY',now,q['at'])
                    closing=fresh_applicability(exit_fee,p['close_fee'],p['instrument'],'SELL',now,q['at'])
                else:
                    # Compatibility mode does not silently enlarge the frozen entry lifetime.
                    profile(p['open_fee'],p['instrument'],'BUY',now);profile(p['close_fee'],p['instrument'],'SELL',now)
                    opening=deepcopy(p['open_fee']) if 'fee_applicability' not in preview else fresh_applicability(preview['fee_applicability'],p['open_fee'],p['instrument'],'BUY',now,q['at'])
                    closing=deepcopy(p['close_fee']) if exit_fee is None else unchanged_fee(exit_fee,p['close_fee'],p['instrument'],'SELL',now)
                fill_fees={'BUY':opening,'SELL':closing,'mode':'EXPLICIT_FRESH' if timing is not None else 'COMPATIBILITY'}
                require(number(preview['total_fee'])==charges(e['units']*e['price'],p['open_fee']),'preview fee mismatch')
            except Exception as ex:
                # First valid quote: any admission failure, however malformed the evidence, consumes the decision.
                self.log('FILL_GATE_FAILED',now,{'reason':str(ex),'quote':q,'preview':preview,'exit_fee':exit_fee});self.pending=None;return 'CANCELLED'
            self.cash=e['residual'];self.position={'plan':deepcopy(p),'units':e['units'],'basis':e['debit'],'entry':now,'last_value':None,'last_value_at':None,'checks_done':[],'checkpoint_results':{},'state':'OPEN','original_target':p['target']}
            self.log('BUY',now,{'quote':q,'preview':preview,'fee_applicability':fill_fees,'economics':e});self.pending=None;return 'FILLED'
        try:
            require(not self.position.get('corporate_block'),'corporate action requires external reconciliation; no modeled sale')
            require(in_session(p,now),'closed session')
            unchanged_fee(exit_fee,p['close_fee'],p['instrument'],'SELL',now)
            pos=self.position;gross=pos['units']*number(q['bid'])*D('.999');net=gross-charges(gross,exit_fee)
            require(net>=0,'negative proceeds unsupported')
        except Exception as ex:
            # Whole position and pending EXIT are retained; record the attempt and its decisive evidence.
            self.log('EXIT_PENDING_DATA',now,{'reason':str(ex),'quote':q,'exit_fee':exit_fee});return 'PENDING_DATA'
        pnl=net-pos['basis'];self.cash+=net;self.realized+=pnl
        self.log('SELL',now,{'quote':q,'exit_fee':exit_fee,'net':net,'pnl':pnl,'reason':self.pending['reason']})
        self.position=None;self.pending=None
        require(abs(self.cash-self.baseline-self.realized)<D('1e-20'),'reconciliation')
        return 'FILLED'
    @guarded
    def observe(self,now,bid=None,mark_at=None,checkpoint_status=None,thesis_invalid=False,corporate_action=False,exit_fee=None):
        require(self.position is not None,'flat');pos=self.position;p=pos['plan'];t=ts(now)
        due=[x for x in p['checks'] if ts(x)<=t and x not in pos['checks_done']]
        missed=due[:-1] if due else []
        late_current=due[-1] if due and ts(due[-1])<t else None
        if missed: self.suspended=True
        pos['checks_done'].extend(due)
        reasons=[]
        if corporate_action:
            self.suspended=True;pos['corporate_block']=True;pos['state']='CORPORATE_ACTION_RECONCILIATION';reasons.append('UNSUPPORTED_CORPORATE_ACTION')
        if thesis_invalid: reasons.append('THESIS_INVALID')
        if t>=ts(p['deadline']): reasons.append('DEADLINE')
        if missed: reasons.append('MISSED_REQUIRED_CHECK')
        status=checkpoint_status if isinstance(checkpoint_status,dict) else {}
        for c in p['checkpoints']:
            if ts(c['at'])<=t:
                if c['at'] not in pos['checkpoint_results']:
                    v=status.get(c['at'],'MISSING')
                    # Untrusted status enters sealed state: malformed values count as not MET.
                    pos['checkpoint_results'][c['at']]=v if isinstance(v,str) else 'MALFORMED'
                if pos['checkpoint_results'][c['at']]!='MET': reasons.append('CATALYST_CHECKPOINT_MISSING')
        mark_error=None
        if bid is not None:
            b=None
            try:
                require(not pos.get('corporate_block'),'corporate action mark blocked')
                require(mark_at is not None and in_session(p,mark_at) and in_session(p,now),'mark outside permitted session')
                require(mark_at is not None and timedelta(0)<=t-ts(mark_at)<=timedelta(seconds=120),'mark stale')
                require(ts(mark_at)>=ts(pos['entry']),'mark predates position entry')
                floor=pos.get('last_mark_at') or pos.get('last_value_at')
                require(floor is None or ts(mark_at)>=ts(floor),'mark older than an accepted mark')
                b=number(bid);require(b>0,'mark bid')
            except Exception as ex:
                # An unusable price withholds only a price trigger, never other exit reasons.
                b=None;mark_error=str(ex)
            if b is not None:
                pos['last_mark_at']=mark_at
                if b<=number(p['stop']): reasons.append('STOP')
                if b>=number(p['target']): reasons.append('TARGET')
                try:
                    unchanged_fee(exit_fee,p['close_fee'],p['instrument'],'SELL',now)
                    val=pos['units']*b*D('.999')-charges(pos['units']*b*D('.999'),p['close_fee'])
                    pos['last_value']=val;pos['last_value_at']=mark_at
                except Exception as ex:
                    # Any cost failure withholds valuation only; observed price intent is already latched.
                    mark_error=str(ex)
        self.log('OBSERVATION',now,{'reasons':reasons,'missed':missed,'late_current_check':late_current,'bid':bid,'mark_at':mark_at,'mark_error':mark_error,'exit_fee':exit_fee})
        if reasons and self.pending is None:
            self.log('EXIT_DECISION',now,{'reasons':reasons});self.pending={'kind':'EXIT','at':now,'plan':deepcopy(p),'reason':reasons};pos['state']='EXIT_PENDING_DATA'
        if t>=ts(p['deadline'])+timedelta(hours=24): pos['state']='CENSORED_OPEN';self.suspended=True
        return pos['state']
    @guarded
    def tighten(self,new_stop,now):
        require(self.position is not None and self.pending is None,'cannot tighten')
        p=self.position['plan'];require(number(p['stop'])<=number(new_stop)<number(p['target']),'stop cannot loosen')
        self.log('TIGHTEN',now,{'old':p['stop'],'new':str(new_stop)});p['stop']=str(new_stop)
    def extend_deadline(self,*args): raise Blocked('deadline extension forbidden; exit then new prospective thesis')


def export_snapshot(simulator):
    require(digest({k:v for k,v in simulator.__dict__.items() if k!='_seal'})==simulator._seal,'state integrity')
    verify_chain(simulator.events)
    return json.dumps({'protocol':VERSION,'mode':'OFFLINE_PAPER_SIMULATION','engine':encode(simulator.__dict__)},sort_keys=True,separators=(',',':')).encode()

def restore_snapshot(source_bytes,expected_sha256):
    require(hashlib.sha256(source_bytes).hexdigest()==expected_sha256,'snapshot digest mismatch')
    data=json.loads(source_bytes);require(data['protocol']==VERSION and data['mode']=='OFFLINE_PAPER_SIMULATION','snapshot version/mode')
    state=decode(data['engine']);verify_chain(state['events'])
    require(digest({k:v for k,v in state.items() if k!='_seal'})==state['_seal'],'state seal mismatch')
    sim=object.__new__(Simulator);sim.__dict__.update(state);return sim
