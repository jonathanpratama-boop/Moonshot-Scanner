"""Pure offline paper accounting. No networking, broker orders, or account access."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, getcontext, InvalidOperation
from datetime import datetime
from datetime import timedelta
import hashlib, json, copy
getcontext().prec=28
D=lambda x: Decimal(str(x))
CENT=D('0.01')
def cash_round(x): return D(x).quantize(CENT,rounding=ROUND_HALF_UP)
def time(s): return datetime.fromisoformat(s.replace('Z','+00:00'))
def liquidation(lots,bid,rate=None,slippage='0.001',extras='0'):
    if any(x is None for x in [lots,bid,rate,slippage,extras]): return None
    if not all(D(x).is_finite() for x in [bid,rate,slippage,extras]+list(lots)): raise ValueError('nonfinite valuation input')
    if D(bid)<=0 or D(rate)<0 or D(extras)<0 or not D(0)<=D(slippage)<D(1) or any(D(q)<0 for q in lots): raise ValueError('invalid valuation input')
    amounts=[D(q)*D(bid)*(1-D(slippage)) for q in lots]
    commission=sum((cash_round(x*D(rate)) for x in amounts),D(0))
    gross=sum((D(q)*D(bid) for q in lots),D(0))
    consideration=sum(amounts,D(0))
    return dict(gross=gross,slippage=gross-consideration,commission=commission,extras=D(extras),net=consideration-commission-D(extras))
def quote_valid(decision,quote,retrieved):
    if not all(k in quote and quote[k] is not None for k in ['bid','ask','as_of_utc']): return False
    if not all(D(quote[k]).is_finite() for k in ['bid','ask']): return False
    if D(quote['bid'])<=0 or D(quote['ask'])<D(quote['bid']): return False
    age=(time(retrieved)-time(quote['as_of_utc'])).total_seconds()
    return time(quote['as_of_utc'])>time(decision) and 0<=age<=120
def entry_gate(action,status,quote_time,entry_expiry,intake_end,has_cash,costs_known,price_risk,cost_risk,execution_time=None,net_reward_risk=None):
    if any(x is None for x in [quote_time,entry_expiry,intake_end,price_risk,cost_risk,execution_time,net_reward_risk]): return False
    return (action=='ENTRY' and status=='PAPER_ENTRY_ELIGIBLE' and has_cash is True and costs_known is True
            and time(quote_time)<=time(execution_time)<min(time(entry_expiry),time(intake_end))
            and D(0)<D(price_risk)<=D('.05') and D(0)<D(cost_risk)<=D('.05') and D(net_reward_risk)>=D('1.5'))
def buy_size(cash,ask,rate=None,slippage='0.001',precision=6,extras='0'):
    if rate is None: raise ValueError('verified fee rate required')
    cash,ask,rate,slippage,extras=map(D,[cash,ask,rate,slippage,extras])
    if not all(x.is_finite() for x in [cash,ask,rate,slippage,extras]): raise ValueError('nonfinite sizing input')
    if ask<=0 or rate<0 or extras<0 or cash<extras or not D(0)<=slippage<D(1) or not isinstance(precision,int) or not 0<=precision<=12: raise ValueError('invalid sizing input')
    step=D(1).scaleb(-precision);price=ask*(1+slippage)
    units=((cash-extras)/(price*(1+rate))).quantize(step,rounding=ROUND_FLOOR)
    if units<0: raise ValueError('insufficient cash')
    while True:
        consideration=units*price;fee=cash_round(consideration*rate);debit=consideration+fee+extras
        if debit<=cash: break
        units-=((debit-cash)/price/step).to_integral_value(rounding=ROUND_FLOOR)*step+step
    return dict(units=units,price=price,consideration=consideration,commission=fee,debit=debit,residual=cash-debit)
def event_hash(event):
    return hashlib.sha256(json.dumps({k:v for k,v in event.items() if k!='record_hash'},sort_keys=True,separators=(',',':')).encode()).hexdigest()
def verify_chain(events):
    previous=None;ids=set()
    for event in events:
        if event.get('id') in ids or event.get('previous_record_hash')!=previous or event.get('record_hash')!=event_hash(event): raise ValueError('event chain tampered')
        ids.add(event['id']);previous=event['record_hash']
    return True
def append_event(events,payload):
    verify_chain(events)
    if any(x['id']==payload['id'] for x in events): raise ValueError('duplicate event id')
    event=copy.deepcopy(payload);event['previous_record_hash']=events[-1]['record_hash'] if events else None
    event['record_hash']=event_hash(event)
    events.append(event);return copy.deepcopy(event)
def observed_drawdown(values):
    peak=None;worst=D(0)
    for v in values:
        if v is None: continue
        v=D(v);peak=max(peak,v) if peak is not None else v
        if peak>0: worst=max(worst,1-v/peak)
    return worst
def validate_checkpoint(cp):
    verify_chain(cp['events'])
    for key,event_type in [('decisions','DECISION'),('observations','OBSERVATION'),('fills','FILL')]:
        canonical=[e['details'] for e in cp['events'] if e['type']==event_type]
        if canonical!=cp[key]: raise ValueError('derived history differs from canonical events: '+key)
    return True

def cash_or_position_equity(cash,lots,bid=None,rate=None,slippage="0.001",extras="0"):
    cash=D(cash)
    if not cash.is_finite() or cash<0: raise ValueError("invalid cash")
    if not lots or sum(D(q) for q in lots)==0: return cash
    held=liquidation(lots,bid,rate,slippage,extras)
    return None if held is None else cash+held["net"]
def planned_exit_check(entry_time,session_exit_trigger,authorized_checks):
    entry=time(entry_time);limit=min(entry+timedelta(hours=3),time(session_exit_trigger))
    feasible=[time(x) for x in authorized_checks if entry<time(x)<=limit]
    return max(feasible).isoformat().replace('+00:00','Z') if feasible else None
def entry_economics(cash,ask,bid,entry_min_ask,entry_max_ask,stop_bid,target_bid,open_rate,close_rate,precision=6,entry_extras='0',exit_extras='0'):
    vals=[cash,ask,bid,entry_min_ask,entry_max_ask,stop_bid,target_bid,open_rate,close_rate,entry_extras,exit_extras]
    if any(v is None for v in vals): return {'eligible':False,'reason':'MISSING_INPUT'}
    if not all(D(v).is_finite() for v in vals): return {'eligible':False,'reason':'NONFINITE_INPUT'}
    if not (D(0)<D(entry_min_ask)<=D(ask)<=D(entry_max_ask) and D(0)<D(stop_bid)<D(bid)<=D(ask)): return {'eligible':False,'reason':'ENTRY_BOUNDS_OR_STOP'}
    size=buy_size(cash,ask,open_rate,precision=precision,extras=entry_extras)
    if size['units']<=0 or D(target_bid)<=size['price']: return {'eligible':False,'reason':'SIZE_OR_TARGET'}
    stop=liquidation([size['units']],stop_bid,close_rate,extras=exit_extras)
    target=liquidation([size['units']],target_bid,close_rate,extras=exit_extras)
    price_risk=(size['price']-D(stop_bid))/size['price'];loss=size['debit']-stop['net'];reward=target['net']-size['debit']
    risk=loss/size['debit'];rr=reward/loss if loss>0 else D(0)
    passed=D(0)<price_risk<=D('.05') and D(0)<risk<=D('.05') and rr>=D('1.5')
    return {'eligible':passed,'reason':'PASS' if passed else 'RISK_REWARD_GATE','size':size,'price_risk':price_risk,'cost_inclusive_risk':risk,'net_reward_risk':rr,'ordinary_loss':loss,'net_target_reward':reward}
def exit_profile_valid(record,profiles):
    if record.get('cost_basis')!='FROZEN_EXIT_PROFILE' or record.get('verification_kind')!='FROZEN_PROFILE_APPLICABILITY_CHECK': return False
    matches=[p for p in profiles if p.get('id')==record.get('fee_profile_id')]
    if len(matches)!=1: return False
    p=matches[0]
    if p.get('status')!='VERIFIED' or p.get('side')!='SELL' or p.get('unresolved_material_cost') is not False: return False
    if p.get('currency')!='USD' or p.get('product')!='real': return False
    if any(p.get(k) is None or p.get(k)!=record.get(k) for k in ['instrument_id','product','currency']): return False
    try:
        when=time(record.get('filled_at_utc') or record.get('observed_at_utc'));verified=time(p['profile_verified_at']);checked=time(record['applicability_checked_at_utc'])
        if not verified<=checked<=when: return False
        if p.get('invalidated_at_utc') and time(p['invalidated_at_utc'])<=when: return False
        for k in ['commission_rate','fixed_fee','taxes','non_price_markup']:
            if record.get(k) is None or p.get(k) is None: return False
            a,b=D(record[k]),D(p[k])
            if not a.is_finite() or not b.is_finite() or a<0 or a!=b: return False
    except (ValueError,TypeError,KeyError,AttributeError,InvalidOperation): return False
    return True
