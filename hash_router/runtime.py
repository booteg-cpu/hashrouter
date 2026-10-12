"""Live TCP routing with separate upstream ownership and bounded downstream work."""
import asyncio
import hmac
import time
import uuid
from dataclasses import dataclass,field,replace
from decimal import Decimal
from .models import Endpoint,RouterSnapshot,number
from .protocol import StratumSession,read_message,write_message,ProtocolError,RemoteError
from .jobs import JobBook,JobError,IncompatibleExtranonce
from .scheduler import Scheduler
from .metrics import Metrics
from .eventlog import EventLog


@dataclass
class OutputConnection:
    configured_endpoint: Endpoint
    endpoint: Endpoint
    task: object = None
    session: object = None
    latest: list | None = None
    difficulty: Decimal = Decimal(1)
    state: str = 'connecting'
    rotate: bool = False


@dataclass
class InputConnection:
    connection_id: str
    reader: object
    writer: object
    source: object = None
    subscribed: bool = False
    lock: object = field(default_factory=asyncio.Lock)
    wake: object = field(default_factory=asyncio.Event)
    sender: object = None
    submits: set = field(default_factory=set)


class RouterRuntime:
    def __init__(self,state,clock=time.monotonic,wall_clock=time.time,max_connections=128,metrics=None,event_log=None,algorithm=''):
        self.event_log=event_log or EventLog();self.algorithm=algorithm
        self.state=state;self.clock=clock;self.wall_clock=wall_clock;self.max_connections=max_connections
        raw=state.load();self.snapshot=RouterSnapshot.from_dict(raw) if raw else None
        self.scheduler=Scheduler(self.snapshot,clock,wall_clock) if self.snapshot else None
        self.jobs=JobBook(clock);self.metrics=metrics or Metrics(clock,wall_clock=wall_clock)
        self.outputs={};self.clients={};self.client_tasks=set()
        self.server=None;self.port=None;self.running=False;self.apply_lock=asyncio.Lock()
        self._summary_at=0;self._allocation_state={}
        if self.snapshot:self._protect(self.snapshot)

    def _protect(self,snapshot):
        endpoints=[snapshot.remainder]+[o.endpoint for o in snapshot.outputs if o.endpoint]
        self.event_log.protect(*(v for e in endpoints for v in (e.username,e.password)),
                               *(v for s in snapshot.sources for v in (s.username,s.password)))

    def log(self,event,level='INFO',**fields):
        self.event_log.emit(event,level,algorithm=self.snapshot.algorithm if self.snapshot else self.algorithm,**fields)

    @property
    def connection_count(self): return len(self.clients)

    async def apply(self,snapshot):
        self._protect(snapshot)
        async with self.apply_lock:
            if self.snapshot:
                if snapshot.algorithm!=self.snapshot.algorithm:raise ValueError('Cannot change routing algorithm')
                if snapshot.revision<self.snapshot.revision: raise ValueError('Stale configuration revision')
                if snapshot.revision==self.snapshot.revision:
                    if snapshot.to_dict(True)!=self.snapshot.to_dict(True): raise ValueError('Conflicting configuration revision')
                    return
                old={o.output_id:o for o in self.snapshot.outputs}
                for output in snapshot.outputs:
                    previous=old.get(output.output_id)
                    if previous and previous.rented_until and previous.rented_until>self.wall_clock():
                        migration = previous.rental_id is None and output.rental_id is not None
                        if (output.target_gh!=previous.target_gh or output.mrr_rig_id!=previous.mrr_rig_id
                                or (not migration and (output.rented_until!=previous.rented_until or output.rental_id!=previous.rental_id))):
                            raise ValueError('Active sale capacity and deadline cannot be changed')
                for previous in old.values():
                    if previous.rented_until and previous.rented_until>self.wall_clock() and previous.output_id not in {o.output_id for o in snapshot.outputs}:
                        raise ValueError('Active sold output cannot be removed')
            self.state.save(snapshot.to_dict(True))
            self.snapshot=snapshot
            if self.scheduler: self.scheduler.update(snapshot)
            else: self.scheduler=Scheduler(snapshot,self.clock,self.wall_clock)
            sources={s.source_id:s for s in snapshot.sources}
            for context in self.clients.values():
                if context.source:
                    updated=sources.get(context.source.source_id)
                    if not updated or (updated.username,updated.password)!=(context.source.username,context.source.password):
                        context.writer.close()
            if self.running: await self._reconcile()
            self._wake_clients()
            self.log('configuration_applied',revision=snapshot.revision,sources=len(snapshot.sources),outputs=len(snapshot.outputs))

    async def start(self,host,port):
        if self.running: raise RuntimeError('Runtime is already running')
        self.running=True
        try:
            self.server=await asyncio.start_server(self._handle_client,host,port,limit=65537)
            self.port=self.server.sockets[0].getsockname()[1]
            self.log('listener_started',host=host,port=self.port)
            if self.snapshot: await self._reconcile()
        except BaseException:
            await self.stop();raise

    async def _reconcile(self):
        desired={o.output_id:o.endpoint for o in self.snapshot.outputs if o.endpoint is not None}
        desired['remainder']=self.snapshot.remainder
        for key,slot in tuple(self.outputs.items()):
            if desired.get(key)!=slot.configured_endpoint:
                if slot.task: slot.task.cancel();await asyncio.gather(slot.task,return_exceptions=True)
                self.log('output_removed',output_id=key)
                self.outputs.pop(key);self.scheduler.set_ready(key,False)
        for key,endpoint in desired.items():
            if key not in self.outputs:
                slot=OutputConnection(endpoint,endpoint);self.outputs[key]=slot
                self.log('output_configured',output_id=key,host=endpoint.host,port=endpoint.port)
                slot.task=asyncio.create_task(self._output_loop(key,slot))

    def _wake_clients(self):
        for context in self.clients.values(): context.wake.set()

    def _reconnect_endpoint(self,slot,params):
        if not isinstance(params,list) or len(params)<2: raise ProtocolError('Invalid reconnect')
        host=params[0] or slot.endpoint.host
        allowed={s.configured_endpoint.host for s in self.outputs.values()}
        if not isinstance(host,str) or not (host in allowed or host.lower().endswith('.miningrigrentals.com')):
            raise ProtocolError('Unapproved reconnect host')
        port=int(params[1] or slot.endpoint.port)
        return Endpoint(host,port,slot.endpoint.username,slot.endpoint.password)

    async def _output_loop(self,key,slot):
        backoff=.25
        try:
            while self.running:
                session=StratumSession(slot.endpoint);slot.session=session;slot.state='connecting'
                started=self.clock()
                self.log('output_connecting','DEBUG',output_id=key,host=slot.endpoint.host,port=slot.endpoint.port)
                try:
                    await session.connect();slot.state='awaiting_job';slot.difficulty=Decimal(1);slot.rotate=False
                    self.log('output_authorized',output_id=key,duration_ms=round((self.clock()-started)*1000))
                    while self.running and not session.closed:
                        if slot.rotate: break
                        try: message=await asyncio.wait_for(session.notifications.get(),.1)
                        except TimeoutError: continue
                        method=message['method'];params=message.get('params',[])
                        if method=='mining.set_difficulty':
                            if len(params)!=1: raise ProtocolError('Invalid difficulty')
                            slot.difficulty=number(params[0],'difficulty',positive=True)
                            if slot.difficulty>Decimal('1e30'): raise ProtocolError('Unsupported difficulty')
                            self.log('difficulty_changed','DEBUG',output_id=key,difficulty=str(slot.difficulty))
                        elif method=='mining.notify':
                            if len(params)!=9: raise ProtocolError('Invalid mining job')
                            # Invalidate clean jobs even before a downstream selects this output.
                            if params[8] and slot.latest!=params:
                                self.jobs.invalidate(key,session.generation,clear_prefixes=False)
                            first=slot.state!='ready'
                            slot.latest=list(params);slot.state='ready';self.scheduler.set_ready(key,True)
                            self.log('output_ready' if first else 'job_received','INFO' if first else 'DEBUG',output_id=key)
                            backoff=.25;self._wake_clients()
                        elif method=='mining.set_extranonce':
                            if len(params)!=2 or not isinstance(params[0],str) or not isinstance(params[1],int):
                                raise ProtocolError('Invalid extranonce update')
                            bytes.fromhex(params[0])
                            if len(params[0])%2 or not 1<=params[1]<=32: raise ProtocolError('Invalid extranonce update')
                            self.jobs.invalidate(key,session.generation);session.generation=uuid.uuid4().hex
                            session.extranonce1=params[0];session.extranonce2_size=params[1]
                            slot.latest=None;self.scheduler.set_ready(key,False)
                        elif method=='client.reconnect':
                            slot.endpoint=self._reconnect_endpoint(slot,params);break
                        elif method=='client.get_version' and message.get('id') is not None:
                            async with session.write_lock:
                                await write_message(session.writer,{'id':message['id'],'result':'scrypt-hash-router/0.1','error':None})
                except (ConnectionError,OSError,TimeoutError,ValueError) as exc:
                    slot.state='disconnected'
                    self.log('output_disconnected','WARNING',output_id=key,error_type=type(exc).__name__,retry_seconds=backoff,
                             reason='upstream_rejected' if isinstance(exc,RemoteError) else 'connection_or_protocol_failure')
                finally:
                    self.scheduler.set_ready(key,False);slot.latest=None
                    if slot.state in ('ready','awaiting_job','connecting'): slot.state='reconnecting'
                    self.jobs.invalidate(key,session.generation)
                    await session.close();self._wake_clients()
                if self.running:
                    await asyncio.sleep(backoff);backoff=min(backoff*2,30)
        finally:
            if slot.session: await slot.session.close()
            slot.state='stopped';slot.latest=None
            if self.scheduler: self.scheduler.set_ready(key,False)

    async def _send(self,context,message):
        async with context.lock: await write_message(context.writer,message)

    async def _reply(self,context,request_id,result,error=None):
        await self._send(context,{'id':request_id,'result':result,'error':error})

    async def _handle_client(self,reader,writer):
        task=asyncio.current_task();self.client_tasks.add(task)
        context=InputConnection(uuid.uuid4().hex,reader,writer)
        if len(self.clients)>=self.max_connections:
            self.log('input_connection_limit','WARNING',connections=len(self.clients))
            writer.close();self.client_tasks.discard(task);return
        self.clients[context.connection_id]=context
        self.log('input_connected',connection=context.connection_id)
        try:
            while self.running:
                request=await read_message(reader,timeout=600 if context.source else 15)
                method=request.get('method');params=request.get('params');request_id=request.get('id')
                if not isinstance(params,list) or not isinstance(request_id,(int,str)) or isinstance(request_id,bool):
                    raise ProtocolError('Invalid Stratum request')
                if method=='mining.subscribe':
                    if context.subscribed: raise ProtocolError('Already subscribed')
                    context.subscribed=True
                    await self._reply(context,request_id,[[['mining.notify',context.connection_id]],'00',2])
                elif method=='mining.authorize':
                    source=None
                    if self.snapshot and context.subscribed and len(params)==2 and all(isinstance(p,str) for p in params):
                        source=next((s for s in self.snapshot.sources if s.username==params[0] and s.expires_at>self.wall_clock()
                                     and hmac.compare_digest(s.password.encode(),params[1].encode())),None)
                    if context.source is not None: source=None
                    await self._reply(context,request_id,source is not None)
                    self.log('input_authorized' if source else 'input_authorization_failed','INFO' if source else 'WARNING',
                             connection=context.connection_id,source_id=source.source_id if source else None)
                    if source is None: break
                    context.source=source;self.scheduler.set_connection_capacity(context.connection_id,source.source_id,Decimal(0))
                    context.sender=asyncio.create_task(self._sender_loop(context))
                elif method=='mining.submit' and context.source:
                    if len(context.submits)>=32:
                        await self._reply(context,request_id,False,[20,'Too many pending shares',None]);continue
                    submit=asyncio.create_task(self._submit(context,request_id,params))
                    context.submits.add(submit);submit.add_done_callback(context.submits.discard)
                elif method=='mining.extranonce.subscribe': await self._reply(context,request_id,True)
                elif method=='mining.configure':
                    extensions=params[0] if params and isinstance(params[0],list) else []
                    await self._reply(context,request_id,{extension:False for extension in extensions if isinstance(extension,str)})
                elif method=='client.get_version': await self._reply(context,request_id,'scrypt-hash-router/0.1')
                elif method=='mining.suggest_difficulty': await self._reply(context,request_id,False)
                else: await self._reply(context,request_id,False,[20,'Unsupported method',None])
        except (ConnectionError,OSError,TimeoutError,ValueError) as exc:
            self.log('input_error','WARNING',connection=context.connection_id,error_type=type(exc).__name__)
        finally:
            if context.sender: context.sender.cancel();await asyncio.gather(context.sender,return_exceptions=True)
            for submit in tuple(context.submits): submit.cancel()
            await asyncio.gather(*context.submits,return_exceptions=True)
            if self.scheduler: self.scheduler.remove_connection(context.connection_id)
            self.clients.pop(context.connection_id,None);self.client_tasks.discard(task)
            self._allocation_state.pop(context.connection_id,None)
            self.log('input_disconnected',connection=context.connection_id)
            writer.close()
            try: await writer.wait_closed()
            except (ConnectionError,OSError): pass

    async def _sender_loop(self,context):
        last=None;started=self.clock()
        try:
            while self.running:
                if last is not None:
                    elapsed=Decimal(str(max(0,self.clock()-started)))
                    self.scheduler.record_elapsed(context.connection_id,last,elapsed)
                    self.metrics.record_allocation(last,elapsed);last=None
                source=next((s for s in self.snapshot.sources if s.source_id==context.source.source_id),None)
                if source is None or source.expires_at<=self.wall_clock():
                    self.log('source_expired',connection=context.connection_id,source_id=context.source.source_id)
                    context.writer.close();return
                context.wake.clear()
                output=self.scheduler.choose(context.connection_id)
                if self._allocation_state.get(context.connection_id,'unset')!=output:
                    self._allocation_state[context.connection_id]=output
                    self.log('allocation_changed','DEBUG',connection=context.connection_id,output_id=output or 'none')
                if output is not None:
                    slot=self.outputs.get(output)
                    if slot and slot.session and slot.latest and not slot.session.closed:
                        try:
                            job=self.jobs.publish(context.connection_id,output,slot.session.generation,
                                slot.latest,slot.difficulty,slot.session.extranonce1,slot.session.extranonce2_size,'00',2)
                            await self._send(context,{'id':None,'method':'mining.set_difficulty','params':[float(slot.difficulty)]})
                            await self._send(context,{'id':None,'method':'mining.notify','params':job})
                            last=output;started=self.clock()
                        except IncompatibleExtranonce:
                            self.log('output_incompatible','WARNING',output_id=output,reason='extranonce')
                            slot.state='incompatible';slot.rotate=True;self.scheduler.set_ready(output,False)
                        except JobError:
                            self.log('invalid_job','WARNING',output_id=output)
                            slot.state='invalid_job';slot.latest=None;self.scheduler.set_ready(output,False)
                try: await asyncio.wait_for(context.wake.wait(),self.snapshot.quantum_seconds)
                except TimeoutError: pass
        except (ConnectionError,OSError,ValueError) as exc:
            self.log('sender_error','WARNING',connection=context.connection_id,error_type=type(exc).__name__)
            context.writer.close()
        finally:
            if last is not None:
                elapsed=Decimal(str(max(0,self.clock()-started)))
                self.scheduler.record_elapsed(context.connection_id,last,elapsed)
                self.metrics.record_allocation(last,elapsed)

    async def _submit(self,context,request_id,params):
        started=self.clock()
        try:
            route=self.jobs.route_submit(context.connection_id,params)
            slot=self.outputs.get(route.output_id)
            session=slot.session if slot else None
            if not session or session.closed or session.generation!=route.generation: raise JobError('Stale mining job')
            upstream=list(route.request_params);upstream[0]=session.endpoint.username
            try:
                accepted=await session.request('mining.submit',upstream)
            except RemoteError:
                accepted=False
            self.metrics.record_submit(route,accepted is True)
            self.log('share_accepted' if accepted is True else 'share_rejected','DEBUG',
                     connection=context.connection_id,output_id=route.output_id,duration_ms=round((self.clock()-started)*1000))
            if accepted is True:
                rates=self.metrics.connection_rates()
                for connection,rate in rates.items():
                    live=self.clients.get(connection)
                    if live and live.source:
                        self.scheduler.set_connection_capacity(connection,live.source.source_id,rate)
            await self._reply(context,request_id,accepted is True,None if accepted is True else [20,'Share rejected',None])
        except JobError:
            self.log('share_stale','DEBUG',connection=context.connection_id)
            await self._reply(context,request_id,False,[21,'Unknown or stale job',None])
        except (ConnectionError,OSError,TimeoutError) as exc:
            self.log('share_outcome_unknown','WARNING',connection=context.connection_id,error_type=type(exc).__name__)
            try: await self._reply(context,request_id,False,[20,'Upstream outcome unknown',None])
            except (ConnectionError,OSError,TimeoutError): pass

    def status(self):
        if not self.snapshot:
            from .metrics import StatusSnapshot
            return StatusSnapshot(0,time.time(),'required',[])
        result=self.metrics.status(self.snapshot)
        weights=self.scheduler.weights
        for row in result.outputs:
            slot=self.outputs.get(row['output_id'])
            row['state']=slot.state if slot else 'unprovisioned'
            row['allocated_target_gh']=format(weights.get(row['output_id'],Decimal(0)),'f')
        if self.clock()-self._summary_at>=30:
            self._summary_at=self.clock()
            for row in result.outputs:
                self.log('output_summary',output_id=row['output_id'],status=row['state'],
                         accepted=row.get('accepted',0),rejected=row.get('rejected',0),stale=row.get('stale',0),
                         target_gh=row['target_gh'],allocated_gh=row['allocated_target_gh'],
                         shortage_gh=str(max(Decimal(0),Decimal(row['target_gh'])-weights.get(row['output_id'],Decimal(0)))),
                         connections=len(self.clients))
        shortages={o.output_id:format(max(Decimal(0),o.target_gh-weights.get(o.output_id,Decimal(0))),'f') for o in self.snapshot.outputs}
        connections={key:{'gh':format(gh,'f'),'kind':'nominal' if self.scheduler.connections[key][1]==0 else 'measured'}
                     for key,gh in self.scheduler.connection_capacities.items()}
        return replace(result,connections=connections,shortages=shortages)

    async def stop(self):
        was_running=self.running
        self.running=False
        server=self.server
        if server: server.close()
        for context in self.clients.values(): context.writer.close()
        tasks=list(self.client_tasks)+[slot.task for slot in self.outputs.values() if slot.task]
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if server: await server.wait_closed()
        self.server=None
        if was_running:self.log('listener_stopped',port=self.port)
