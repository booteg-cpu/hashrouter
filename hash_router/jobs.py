"""Extranonce translation preserves the exact upstream coinbase bytes."""
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
import json
import time
import uuid
from .models import number


class JobError(ValueError):
    def __init__(self, message, reason='job_error'):
        super().__init__(message)
        self.reason=reason
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
    version: str
    invalidation_reason: str = "none"


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
            raise IncompatibleExtranonce('Invalid extranonce size','extranonce_size_type')
        unique_size=upstream_extranonce2_size-downstream_extranonce2_size-len(downstream_nonce)//2
        if not 0<=unique_size<=30 or not 1<=downstream_extranonce2_size<=32:
            raise IncompatibleExtranonce('Extranonce space cannot preserve this connection','extranonce_space_insufficient')
        # The suffix of the upstream prefix is the downstream extranonce1.
        # Original: cb1 + upstream1 + unique + downstream1 + downstream2 + cb2.
        # Rewritten: (cb1 + upstream1 + unique) + downstream1 + downstream2 + cb2.
        work_fingerprint=json.dumps(notify[1:8],separators=(',',':'))
        namespace=(output_id,generation,upstream_nonce,upstream_extranonce2_size,
                   downstream_extranonce2_size,downstream_nonce,work_fingerprint)
        index=self.prefix_counts.get(namespace,0)
        if index>=256**unique_size: raise IncompatibleExtranonce('Extranonce assignment capacity exhausted','extranonce_capacity_exhausted')
        unique=index.to_bytes(unique_size,'big').hex()
        self.prefix_counts[namespace]=index+1
        while len(self.prefix_counts)>self.max_jobs:
            self.prefix_counts.pop(next(iter(self.prefix_counts)))
        fingerprint=json.dumps(notify,separators=(',',':'))
        current_key=(output_id,generation)
        if notify[8] and self.latest.get(current_key)!=fingerprint:
            self.invalidate(output_id,generation,clear_prefixes=False,reason='clean_job')
        self.latest[current_key]=fingerprint
        local_id=uuid.uuid4().hex
        layout=ExtranonceLayout(unique+downstream_nonce,downstream_nonce,downstream_extranonce2_size,upstream_extranonce2_size)
        record=JobRecord(local_id,source_connection_id,output_id,generation,notify[0],
                         number(difficulty,'difficulty',positive=True),layout,True,self.clock(),notify[7],notify[5])
        self.jobs[local_id]=record;self._collect()
        result=list(notify);result[0]=local_id;result[2]=notify[2]+upstream_nonce+unique
        result[4]=list(notify[4]);result[8]=True
        return result

    def route_submit(self,source_connection_id,params,version_mask=None):
        self._collect()
        if not isinstance(params,list):
            raise JobError('Invalid mining.submit', 'submit_not_list')
        if len(params)!=(6 if version_mask is not None else 5):
            raise JobError('Invalid mining.submit', 'submit_parameter_count')
        if not all(isinstance(p,str) for p in params):
            raise JobError('Invalid mining.submit', 'submit_parameter_type')
        job=self.jobs.get(params[1])
        if job is None: raise JobError('Unknown or stale mining job', 'job_unknown')
        if job.source_connection_id!=source_connection_id:
            raise JobError('Unknown or stale mining job', 'job_wrong_connection')
        if not job.valid: raise JobError('Unknown or stale mining job', 'job_invalidated')
        def field(value,size,name):
            try: return hex_value(value,size)
            except JobError: raise JobError('Invalid hexadecimal field', name+'_format') from None
        extra=field(params[2],job.extranonce_layout.downstream_size,'extranonce2')
        ntime=field(params[3],4,'ntime');nonce=field(params[4],4,'nonce')
        if int(ntime,16)<int(job.ntime,16): raise JobError('Invalid ntime','ntime_before_job')
        upstream=[params[0],job.upstream_job_id,job.extranonce_layout.prefix+extra,ntime,nonce]
        if version_mask is not None:
            bits=field(params[5],4,"version_bits")
            if int(bits,16)&~version_mask: raise JobError('Version bits outside negotiated mask','version_bits_outside_mask')
            upstream.append(bits)
        return ShareRoute(job.output_id,job.generation,upstream,job)

    def submit_diagnostic(self,connection,params,version_mask=None):
        values=params if isinstance(params,list) else []
        data={'parameter_count':len(values) if isinstance(params,list) else -1,
              'expected_parameter_count':6 if version_mask is not None else 5,
              'mask':format(version_mask or 0,'08x')}
        for index,name in ((2,'extranonce2_chars'),(3,'ntime_chars'),(4,'nonce_chars'),(5,'version_chars')):
            data[name]=len(values[index]) if len(values)>index and isinstance(values[index],str) else -1
        job=self.jobs.get(values[1]) if len(values)>1 and isinstance(values[1],str) else None
        data['job_known']=job is not None
        if job is not None:
            data.update(job_valid=job.valid,job_owned=job.source_connection_id==connection,
                        job_age_ms=round(max(0,self.clock()-job.created_at)*1000),
                        expected_extranonce2_chars=job.extranonce_layout.downstream_size*2,
                        upstream_extranonce2_bytes=job.extranonce_layout.upstream_size,
                        invalidation_reason=job.invalidation_reason)
        return data

    def invalidate(self,output_id,generation,clear_prefixes=True,reason="upstream_changed"):
        for job in self.jobs.values():
            if job.output_id==output_id and job.generation==generation:
                job.valid=False;job.invalidation_reason=reason
        if clear_prefixes:
            self.latest.pop((output_id,generation),None)
            for collection in (self.prefix_counts,):
                for key in list(collection):
                    if key[:2]==(output_id,generation): del collection[key]
