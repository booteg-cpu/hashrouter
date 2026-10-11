"""Accepted work estimates are withheld until the endpoint convention is verified."""
from collections import deque,defaultdict
from dataclasses import dataclass,field,asdict
from decimal import Decimal
import time
from .models import number


@dataclass(frozen=True,slots=True)
class DifficultyConvention:
    expected_hashes_per_difficulty: Decimal = Decimal('65536')
    verified: bool = False
    name: str = 'scrypt-unverified'

    def __post_init__(self):
        object.__setattr__(self,'expected_hashes_per_difficulty',number(self.expected_hashes_per_difficulty,'difficulty scale',positive=True))


@dataclass(frozen=True,slots=True)
class StatusSnapshot:
    revision: int
    measured_at: float
    calibration: str
    outputs: list
    experimental: bool = True
    connections: dict = field(default_factory=dict)
    shortages: dict = field(default_factory=dict)

    def to_dict(self): return asdict(self)


class Metrics:
    def __init__(self,clock=time.monotonic,convention=None,max_events=100000,wall_clock=time.time):
        self.clock=clock;self.convention=convention or DifficultyConvention()
        self.wall_clock=wall_clock
        self.started=clock();self.events=deque();self.seen={};self.max_events=max_events
        self.counts=defaultdict(lambda:{'accepted':0,'rejected':0,'stale':0,'last_accepted_at':None})
        self.allocations=defaultdict(Decimal);self.last_truncated=None

    def record_submit(self,route,accepted,stale=False):
        now=self.clock()
        key=(route.output_id,route.generation,*route.request_params[1:])
        self._prune(now)
        if key in self.seen: return
        self.seen[key]=now
        hashes=route.job.difficulty*self.convention.expected_hashes_per_difficulty if accepted else Decimal(0)
        self.events.append((now,route.output_id,route.job.source_connection_id,hashes,key))
        row=self.counts[route.output_id]
        row['accepted' if accepted else 'rejected']+=1
        if accepted: row['last_accepted_at']=self.wall_clock()
        if stale and not accepted: row['stale']+=1
        while len(self.events)>self.max_events:
            old=self.events.popleft();self.seen.pop(old[4],None);self.last_truncated=now

    def record_allocation(self,output_id,seconds):
        self.allocations[output_id]+=number(seconds,'allocated seconds')

    def _prune(self,now):
        while self.events and now-self.events[0][0]>1800:
            old=self.events.popleft();self.seen.pop(old[4],None)

    def _rate(self,output_id,seconds,now):
        if not self.convention.verified or (self.last_truncated is not None and now-self.last_truncated<seconds): return None
        duration=min(Decimal(str(seconds)),max(Decimal(0),Decimal(str(now-self.started))))
        if duration<=0: return None
        hashes=sum((event[3] for event in self.events if event[1]==output_id and now-event[0]<seconds),Decimal(0))
        return format(hashes/duration/Decimal(10**9),'f')

    def connection_rates(self,seconds=300):
        now=self.clock();self._prune(now)
        if not self.convention.verified or (self.last_truncated is not None and now-self.last_truncated<seconds): return {}
        duration=min(Decimal(str(seconds)),max(Decimal(0),Decimal(str(now-self.started))))
        if duration<=0: return {}
        hashes=defaultdict(Decimal)
        for when,_,connection,work,_ in self.events:
            if now-when<seconds: hashes[connection]+=work
        return {connection:work/duration/Decimal(10**9) for connection,work in hashes.items()}

    def status(self,snapshot):
        now=self.clock();self._prune(now)
        total=sum((source.advertised_gh for source in snapshot.sources if source.expires_at>self.wall_clock()),Decimal(0))
        targets=[(output.output_id,output.target_gh) for output in snapshot.outputs]
        targets.append(('remainder',max(Decimal(0),total-sum((o.target_gh for o in snapshot.outputs),Decimal(0)))))
        rows=[]
        for key,target in targets:
            rows.append({'output_id':key,'target_gh':format(target,'f'),**self.counts[key],
                         'allocated_seconds':format(self.allocations[key],'f'),
                         'accepted_gh':{label:self._rate(key,seconds,now) for label,seconds in [('5m',300),('15m',900),('30m',1800)]}})
        return StatusSnapshot(snapshot.revision,self.wall_clock(),'verified' if self.convention.verified else 'required',rows)
