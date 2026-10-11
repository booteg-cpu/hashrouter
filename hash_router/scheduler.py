"""Weighted fair scheduling measured in estimated GH × seconds."""
from collections import defaultdict
from decimal import Decimal
import time
from .allocation import allocate_shortage
from .models import number


class Scheduler:
    def __init__(self,snapshot,clock=time.monotonic,wall_clock=time.time):
        self.snapshot=snapshot;self.clock=clock;self.wall_clock=wall_clock
        self.connections={};self.ready=set();self.allocated_work=defaultdict(Decimal)
        self.pending={}
        self.effective_weights={}

    def set_connection_capacity(self,connection_id,source_id,gh):
        if source_id not in {s.source_id for s in self.snapshot.sources}: raise ValueError('Unknown source')
        self.connections[connection_id]=(source_id,number(gh,'connection capacity'))

    def remove_connection(self,connection_id):
        previous=self.pending.pop(connection_id,None)
        if previous: self.allocated_work[previous[0]]-=previous[1]
        self.connections.pop(connection_id,None)

    @property
    def connection_capacities(self):
        result={};now=self.wall_clock()
        for source in self.snapshot.sources:
            if source.expires_at<=now: continue
            connections={key:gh for key,(sid,gh) in self.connections.items() if sid==source.source_id}
            known={key:gh for key,gh in connections.items() if gh>0}
            unknown=[key for key,gh in connections.items() if not gh]
            total=sum(known.values(),Decimal(0))
            scale=min(Decimal(1),source.advertised_gh/total) if total else Decimal(1)
            remaining=source.advertised_gh-min(total,source.advertised_gh)
            result.update({key:gh*scale for key,gh in known.items()})
            if unknown: result.update({key:remaining/len(unknown) for key in unknown})
        return result

    @property
    def weights(self):
        capacity=sum(self.connection_capacities.values(),Decimal(0))
        allocations=allocate_shortage(capacity,self.snapshot.outputs,now=self.wall_clock())
        weights={key:gh for key,gh in allocations.items() if key in self.ready and gh>0}
        remainder=max(Decimal(0),capacity-sum(weights.values(),Decimal(0)))
        if 'remainder' in self.ready and remainder>0: weights['remainder']=remainder
        return weights

    def set_ready(self,output_id,ready):
        if ready: self.ready.add(output_id)
        else: self.ready.discard(output_id)

    def choose(self,connection_id):
        capacity=self.connection_capacities.get(connection_id,Decimal(0))
        weights=self.weights
        self._rebase(weights)
        if capacity<=0 or not weights: return None
        previous=self.pending.pop(connection_id,None)
        if previous:
            # An assignment not actually issued must not consume another quantum.
            self.allocated_work[previous[0]]-=previous[1]
        output=min(weights,key=lambda key:self.allocated_work[key]/weights[key])
        reservation=capacity*Decimal(str(self.snapshot.quantum_seconds))
        self.allocated_work[output]+=reservation
        self.pending[connection_id]=(output,reservation,capacity)
        return output

    def _rebase(self,weights):
        if weights==self.effective_weights:return
        reserved=defaultdict(Decimal)
        for output,work,_ in self.pending.values():reserved[output]+=work
        completed={key:self.allocated_work[key]-reserved[key] for key in self.effective_weights}
        virtual={key:completed[key]/weight for key,weight in self.effective_weights.items() if weight>0}
        baseline=min(virtual.values(),default=Decimal(0))
        # Preserve bounded relative lead, not the lifetime work divided by new weights.
        rebased=defaultdict(Decimal)
        for key,weight in weights.items():
            rebased[key]=max(Decimal(0),virtual.get(key,baseline)-baseline)*weight
        for key,work in reserved.items():rebased[key]+=work
        self.allocated_work=rebased;self.effective_weights=dict(weights)

    def record_elapsed(self,connection_id,output_id,seconds):
        elapsed=number(seconds,'elapsed')
        previous=self.pending.pop(connection_id,None)
        capacity=self.connection_capacities.get(connection_id,Decimal(0))
        if previous:
            self.allocated_work[previous[0]]-=previous[1]
            capacity=previous[2]
        self.allocated_work[output_id]+=capacity*elapsed

    def update(self,snapshot):
        old={o.output_id:o.target_gh for o in self.snapshot.outputs}
        new={o.output_id:o.target_gh for o in snapshot.outputs}
        self.snapshot=snapshot
        source_ids={s.source_id for s in snapshot.sources}
        for key,(sid,_) in list(self.connections.items()):
            if sid not in source_ids: self.remove_connection(key)
