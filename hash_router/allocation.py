"""Exact capacity and price calculations; no network operations."""
from decimal import Decimal, InvalidOperation, localcontext
from dataclasses import replace
import time
from .models import number, integer


def hash_to_gh(value, unit):
    powers = {'hash': -9, 'h': -9, 'kh': -6, 'mh': -3, 'gh': 0, 'th': 3, 'ph': 6, 'eh': 9}
    if str(unit).lower() not in powers: raise ValueError('Unsupported hash unit')
    return number(value, 'hashrate') * Decimal(10) ** powers[str(unit).lower()]


def split_capacity(total_gh, chunk_gh, max_outputs):
    total = number(total_gh, 'total_gh')
    chunk = number(chunk_gh, 'chunk_gh', positive=True)
    maximum = integer(max_outputs, 'max_outputs', 0, 1000)
    with localcontext() as ctx:
        ctx.prec = max(50, len(total.as_tuple().digits) + len(chunk.as_tuple().digits) + 20)
        count = min(int(total / chunk), maximum)
        return count, total - chunk * count


def price_payload(policy):
    if policy.mode not in ('manual', 'recommended'): raise ValueError('Invalid price mode')
    try: modifier = Decimal(str(policy.modifier_pct))
    except (InvalidOperation, ValueError): raise ValueError('Invalid price modifier') from None
    if not modifier.is_finite() or modifier <= -100: raise ValueError('Invalid price modifier')
    btc = {'enabled': True, 'autoprice': policy.mode == 'recommended'}
    if policy.mode == 'manual':
        btc.update(price=format(number(policy.manual_btc_per_gh_day, 'manual price', positive=True), 'f'), modifier='0')
    else:
        btc['modifier'] = format(modifier, 'f')
    return {'type': 'gh', 'btc': btc, 'ltc': {'enabled': False},
            'eth': {'enabled': False}, 'doge': {'enabled': False}}


def available_sale_hours(sources, required_gh, now, reserve_seconds):
    required=number(required_gh,'required_gh',positive=True)
    reserve=number(reserve_seconds,'reserve_seconds')
    active=[s for s in sources if s.expires_at>now]
    if sum((s.advertised_gh for s in active),Decimal(0))<required: return Decimal(0)
    for deadline in sorted({s.expires_at for s in active}):
        after=sum((s.advertised_gh for s in active if s.expires_at>deadline),Decimal(0))
        if after<required:
            seconds=Decimal(str(deadline))-Decimal(str(now))-reserve
            return max(Decimal(0),seconds/Decimal(3600))
    return Decimal(0)


def allocate_shortage(capacity_gh, outputs, now=None):
    now=time.time() if now is None else now
    remaining=number(capacity_gh,'capacity_gh')
    result={o.output_id:Decimal(0) for o in outputs}
    sold=[o for o in outputs if o.rented_until is not None and o.rented_until>now]
    free=[o for o in outputs if o not in sold]
    for group in (sold,free):
        need=sum((o.target_gh for o in group),Decimal(0))
        if not need: continue
        supply=min(remaining,need)
        balance=supply
        for i,o in enumerate(group):
            # Assign rounding residue to last output so total never exceeds supply.
            share=balance if i==len(group)-1 else min(balance,supply*o.target_gh/need)
            result[o.output_id]=share
            balance-=share
        remaining-=supply
    return result


def resize_free_outputs(outputs,target_gh,now):
    target=number(target_gh,'target_gh',positive=True)
    return tuple(o if o.rented_until is not None and o.rented_until>now
                 else replace(o,target_gh=target,idle_hash_gh=min(o.idle_hash_gh,target)) for o in outputs)
