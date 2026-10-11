"""Extranonce translation preserves the exact upstream coinbase bytes."""
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
import json
import time
import uuid
from .models import number


class JobError(ValueError): pass
class IncompatibleExtranonce(JobError): pass


def hex_value(value, size=None):
    if not isinstance(value,str) or len(value)%2 or (size is not None and len(value)!=size*2):
        raise JobError('Invalid hexadecimal field')
    try: bytes.fromhex(value)
    except ValueError: raise JobError('Invalid hexadecimal field') from None
    return value.lower()


@dataclass(frozen=True,slots=True)
class ExtranonceLayout:
    prefix: str
    downstream_extranonce1: str
    downstream_size: int
    upstream_size: int


@dataclass(slots=True)
class JobRecord:
    local_id: str
    source_connection_id: str
    output_id: str
    generation: str
    upstream_job_id: str
    difficulty: Decimal
    extranonce_layout: ExtranonceLayout
    valid: bool
    created_at: float
    ntime: str


@dataclass(frozen=True,slots=True)
class ShareRoute:
    output_id: str
    generation: str
    request_params: list
    job: JobRecord


class JobBook:
    def __init__(self,clock=time.monotonic,max_jobs=4096,ttl=600):
        self.clock=clock;self.max_jobs=max_jobs;self.ttl=ttl
        self.jobs=OrderedDict();self.prefix_counts={};self.latest={}

    def _collect(self):
        now=self.clock()
        while self.jobs:
            record=next(iter(self.jobs.values()))
            if len(self.jobs)<=self.max_jobs and now-record.created_at<=self.ttl: break
            self.jobs.popitem(last=False)

    def publish(self,source_connection_id,output_id,generation,notify,difficulty,
                upstream_extranonce1,upstream_extranonce2_size,downstream_extranonce1,downstream_extranonce2_size):
        if not isinstance(notify,list) or len(notify)!=9 or not isinstance(notify[0],str):
            raise JobError('Invalid mining.notify')
        hex_value(notify[1],32);hex_value(notify[2]);hex_value(notify[3])
        if not isinstance(notify[4],list) or len(notify[4])>64: raise JobError('Invalid merkle branches')
        for branch in notify[4]: hex_value(branch,32)
        for index in (5,6,7): hex_value(notify[index],4)
        if not isinstance(notify[8],bool): raise JobError('Invalid clean_jobs')
        upstream_nonce=hex_value(upstream_extranonce1)
        downstream_nonce=hex_value(downstream_extranonce1)
        if not isinstance(upstream_extranonce2_size,int) or not isinstance(downstream_extranonce2_size,int):
            raise IncompatibleExtranonce('Invalid extranonce size')
        unique_size=upstream_extranonce2_size-downstream_extranonce2_size-len(downstream_nonce)//2
        if not 0<=unique_size<=30 or not 1<=downstream_extranonce2_size<=32:
            raise IncompatibleExtranonce('Extranonce space cannot preserve this connection')
        # The suffix of the upstream prefix is the downstream extranonce1.
        # Original: cb1 + upstream1 + unique + downstream1 + downstream2 + cb2.
        # Rewritten: (cb1 + upstream1 + unique) + downstream1 + downstream2 + cb2.
        work_fingerprint=json.dumps(notify[1:8],separators=(',',':'))
        namespace=(output_id,generation,upstream_nonce,upstream_extranonce2_size,
                   downstream_extranonce2_size,downstream_nonce,work_fingerprint)
        index=self.prefix_counts.get(namespace,0)
        if index>=256**unique_size: raise IncompatibleExtranonce('Extranonce assignment capacity exhausted')
        unique=index.to_bytes(unique_size,'big').hex()
        self.prefix_counts[namespace]=index+1
        while len(self.prefix_counts)>self.max_jobs:
            self.prefix_counts.pop(next(iter(self.prefix_counts)))
        fingerprint=json.dumps(notify,separators=(',',':'))
        current_key=(output_id,generation)
        if notify[8] and self.latest.get(current_key)!=fingerprint:
            self.invalidate(output_id,generation,clear_prefixes=False)
        self.latest[current_key]=fingerprint
        local_id=uuid.uuid4().hex
        layout=ExtranonceLayout(unique+downstream_nonce,downstream_nonce,downstream_extranonce2_size,upstream_extranonce2_size)
        record=JobRecord(local_id,source_connection_id,output_id,generation,notify[0],
                         number(difficulty,'difficulty',positive=True),layout,True,self.clock(),notify[7])
        self.jobs[local_id]=record;self._collect()
        result=list(notify);result[0]=local_id;result[2]=notify[2]+upstream_nonce+unique
        result[4]=list(notify[4]);result[8]=True
        return result

    def route_submit(self,source_connection_id,params):
        self._collect()
        if not isinstance(params,list) or len(params)!=5 or not all(isinstance(p,str) for p in params):
            raise JobError('Invalid mining.submit')
        job=self.jobs.get(params[1])
        if job is None or not job.valid or job.source_connection_id!=source_connection_id:
            raise JobError('Unknown or stale mining job')
        extra=hex_value(params[2],job.extranonce_layout.downstream_size)
        ntime=hex_value(params[3],4);nonce=hex_value(params[4],4)
        # Allow legitimate ntime rolling, but never an ntime older than the issued job.
        if int(ntime,16)<int(job.ntime,16): raise JobError('Invalid ntime')
        upstream=[params[0],job.upstream_job_id,job.extranonce_layout.prefix+extra,ntime,nonce]
        return ShareRoute(job.output_id,job.generation,upstream,job)

    def invalidate(self,output_id,generation,clear_prefixes=True):
        for job in self.jobs.values():
            if job.output_id==output_id and job.generation==generation: job.valid=False
        if clear_prefixes:
            self.latest.pop((output_id,generation),None)
            for collection in (self.prefix_counts,):
                for key in list(collection):
                    if key[:2]==(output_id,generation): del collection[key]
