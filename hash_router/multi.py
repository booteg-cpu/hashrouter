"""Independent routing groups; one persistent Stratum port per algorithm."""
import asyncio
import copy
import re
import time
from dataclasses import dataclass
from aiohttp import web
from .models import RouterSnapshot, integer
from .runtime import RouterRuntime
from .metrics import Metrics, DifficultyConvention
from .control import create_control_app
from .eventlog import EventLog

KNOWN_PROTOCOLS = {'scrypt':'bitcoin-stratum-v1','sha256ab':'bitcoin-stratum-v1',
                   'sha256':'bitcoin-stratum-v1','sha256d':'bitcoin-stratum-v1'}
HASH_UNITS = {'hash','h','kh','mh','gh','th','ph','eh'}
UNITS = HASH_UNITS | {'sol','ksol','msol','gsol'}

def algorithm_code(code):
    if not isinstance(code,str) or not re.fullmatch(r'[a-z0-9_-]{1,64}',code):
        raise ValueError('Invalid algorithm code')
    return code

def parse_catalog(data):
    if not isinstance(data,dict) or not isinstance(data.get('algorithms'),dict):
        raise ValueError('Algorithm catalog required')
    if len(data['algorithms'])>100:raise ValueError('Too many algorithms')
    result={}
    for code,meta in data['algorithms'].items():
        algorithm_code(code)
        if not isinstance(meta,dict) or meta.get('unit') not in UNITS:raise ValueError('Unsupported hashrate unit')
        title=meta.get('title',code)
        if not isinstance(title,str) or not title or len(title)>100 or any(ord(c)<32 for c in title):
            raise ValueError('Invalid algorithm title')
        protocol=meta.get('protocol',KNOWN_PROTOCOLS.get(code,'unsupported'))
        if protocol not in ('bitcoin-stratum-v1','unsupported'):raise ValueError('Unsupported protocol family')
        if meta['unit'] not in HASH_UNITS:protocol='unsupported'
        result[code]={'title':title,'unit':meta['unit'],'protocol':protocol,'active':True}
    return result

class MemoryState:
    def __init__(self,data=None):self.data=data
    def load(self):return copy.deepcopy(self.data)
    def save(self,data):self.data=copy.deepcopy(data)

class GroupState:
    def __init__(self,owner,code):self.owner=owner;self.code=code
    def load(self):return copy.deepcopy(self.owner.saved.get(self.code))
    def save(self,data):
        saved=dict(self.owner.saved);saved[self.code]=data
        self.owner.persist(saved=saved)
        self.owner.saved=saved

@dataclass
class MultiStatus:
    data:dict
    def to_dict(self):return self.data

class MultiRouter:
    def __init__(self,state,host,base_port,control_port,max_connections=128,conventions=None,event_log=None):
        self.event_log=event_log or EventLog()
        self.state=state;self.host=host;self.base_port=base_port;self.control_port=control_port
        self.max_connections=max_connections;self.conventions=conventions or {}
        self.running=False;self.lock=asyncio.Lock();self.groups={}
        raw=state.load() or {}
        if raw and raw.get('schema_version')==1:
            raw={'schema_version':2,'catalog':{'scrypt':{'title':'Scrypt','unit':'gh',
                 'protocol':'bitcoin-stratum-v1','active':True}},'ports':{'scrypt':base_port},'groups':{'scrypt':raw}}
        if raw and raw.get('schema_version')!=2:raise ValueError('Unsupported multi-router state')
        self.catalog=raw.get('catalog',{});self.ports=raw.get('ports',{});self.saved=raw.get('groups',{})
        parse_catalog({'algorithms':self.catalog})
        for code,port in self.ports.items():
            algorithm_code(code);integer(port,'stratum port',0 if base_port==0 else 1,65535)
            if port==control_port:raise ValueError('Stratum port conflicts with HTTPS')
        nonzero=[p for p in self.ports.values() if p]
        if len(nonzero)!=len(set(nonzero)):raise ValueError('Duplicate Stratum ports')
        for code,raw_group in self.saved.items():
            snapshot=RouterSnapshot.from_dict(raw_group)
            if code not in self.catalog or code not in self.ports or snapshot.algorithm!=code:
                raise ValueError('Invalid saved algorithm group')
            self.groups[code]=self.make_group(code,GroupState(self,code))

    @property
    def snapshot(self):
        group=self.groups.get('scrypt');return group.snapshot if group else None

    def make_group(self,code,state):
        # A verified Scrypt scale is never inherited by another algorithm.
        convention=DifficultyConvention(**self.conventions.get(code,{'name':code+'-unverified'}))
        return RouterRuntime(state,max_connections=self.max_connections,metrics=Metrics(convention=convention),event_log=self.event_log,algorithm=code)

    def persist(self,*,catalog=None,ports=None,saved=None):
        self.state.save({'schema_version':2,'catalog':self.catalog if catalog is None else catalog,
            'ports':self.ports if ports is None else ports,'groups':self.saved if saved is None else saved})

    async def sync_catalog(self,data):
        desired=parse_catalog(data)
        async with self.lock:
            catalog={code:dict(meta,active=False) for code,meta in self.catalog.items()}
            ports=dict(self.ports)
            for code,meta in desired.items():
                old=catalog.get(code)
                if code in self.groups and old and old['protocol']!=meta['protocol']:
                    raise ValueError('Cannot change protocol of a configured group')
                catalog[code]=meta
                if code not in ports and meta['protocol']=='bitcoin-stratum-v1':
                    if self.base_port==0:ports[code]=0
                    else:
                        port=self.base_port if code=='scrypt' else self.base_port+1
                        while port in ports.values() or port==self.control_port:port+=1
                        ports[code]=integer(port,'stratum port',1,65535)
            if len(catalog)>100:raise ValueError('Too many retained algorithms')
            self.persist(catalog=catalog,ports=ports)
            self.catalog=catalog;self.ports=ports
            self.event_log.emit('catalog_applied',count=len(desired))

    async def apply_group(self,code,snapshot):
        algorithm_code(code)
        async with self.lock:
            if code!=snapshot.algorithm:raise ValueError('Algorithm/group mismatch')
            meta=self.catalog.get(code)
            if not meta or meta['protocol']!='bitcoin-stratum-v1':raise ValueError('Unsupported algorithm protocol')
            # Retired groups retain paid obligations, including a just-confirmed input; no new outputs.
            if not meta['active']:
                previous=self.saved.get(code,{})
                if not {o.output_id for o in snapshot.outputs}<={o['output_id'] for o in previous.get('outputs',[])}:
                    raise ValueError('Algorithm removed from SBP')
            group=self.groups.get(code)
            if group:
                await group.apply(snapshot);return
            group=self.make_group(code,MemoryState())
            await group.apply(snapshot)
            try:
                if self.running:await group.start(self.host,self.ports[code])
                saved=dict(self.saved);saved[code]=snapshot.to_dict(True)
                self.persist(saved=saved)
            except BaseException:
                await group.stop();raise
            self.saved=saved;group.state=GroupState(self,code);self.groups[code]=group

    async def apply(self,snapshot):
        if snapshot.algorithm!='scrypt':raise ValueError('Use algorithm-specific endpoint')
        if 'scrypt' not in self.catalog:
            await self.sync_catalog({'algorithms':dict(self.catalog,scrypt={'title':'Scrypt','unit':'gh'})})
        await self.apply_group('scrypt',snapshot)

    async def start(self,*unused):
        if self.running:raise RuntimeError('Already running')
        self.running=True
        try:
            for code,group in self.groups.items():await group.start(self.host,self.ports[code])
        except BaseException:
            await self.stop();raise

    async def stop(self):
        self.running=False
        await asyncio.gather(*(g.stop() for g in self.groups.values()))

    def status(self):
        legacy=self.groups['scrypt'].status().to_dict() if 'scrypt' in self.groups else {
            'revision':0,'outputs':[],'connections':{},'shortages':{},'calibration':'required'}
        algorithms={}
        for code,meta in self.catalog.items():
            group=self.groups.get(code)
            algorithms[code]=dict(meta,port=(group.port if group and group.running else self.ports.get(code)),
                listening=bool(group and group.running and group.server),
                routing='unsupported' if meta['protocol']=='unsupported' else ('configured' if group else 'unconfigured'))
        return MultiStatus(dict(legacy,measured_at=time.time(),schema_version=2,algorithms=algorithms,
            groups={code:dict(g.status().to_dict(),algorithm=code,port=g.port if g.running else self.ports[code])
                    for code,g in self.groups.items()}))

def create_multi_app(runtime,token):
    app=create_control_app(runtime,token)
    async def catalog(request):
        try:await runtime.sync_catalog(await request.json())
        except (ValueError,TypeError,KeyError):raise web.HTTPBadRequest(text='Invalid algorithm catalog') from None
        except OSError:raise web.HTTPServiceUnavailable(text='Cannot persist catalog') from None
        return web.json_response(runtime.status().to_dict())
    async def config(request):
        try:snapshot=RouterSnapshot.from_dict(await request.json())
        except (ValueError,TypeError):raise web.HTTPBadRequest(text='Invalid routing configuration') from None
        try:await runtime.apply_group(request.match_info['algorithm'],snapshot)
        except ValueError:raise web.HTTPConflict(text='Algorithm, revision or obligation conflict') from None
        except OSError:raise web.HTTPServiceUnavailable(text='Cannot persist or bind algorithm group') from None
        return web.json_response({'revision':snapshot.revision,'applied':True})
    async def status(request):
        code=request.match_info['algorithm'];result=runtime.status().to_dict()
        if code not in result['algorithms']:raise web.HTTPNotFound()
        return web.json_response(dict(result['groups'].get(code,{'revision':0,'outputs':[]}),
                                      algorithm=code,**{'listener':result['algorithms'][code]}))
    app.router.add_put('/v2/algorithms',catalog)
    app.router.add_put('/v2/algorithms/{algorithm}/config',config)
    app.router.add_get('/v2/algorithms/{algorithm}/status',status)
    return app
