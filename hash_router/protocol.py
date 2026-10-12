"""Bounded JSON-lines transport and authenticated upstream Stratum sessions."""
import asyncio
import json
import uuid
import re


def version_mask(value):
    if not isinstance(value,str) or re.fullmatch(r'[0-9a-fA-F]{8}',value) is None:
        raise ProtocolError('Invalid version mask')
    return int(value,16)


class ProtocolError(ValueError): pass
class RemoteError(ConnectionError): pass


async def read_message(reader, max_bytes=65536, timeout=30):
    try:
        data=await asyncio.wait_for(reader.readuntil(b'\n'),timeout)
    except (asyncio.LimitOverrunError,ValueError):
        raise ProtocolError('Stratum message exceeds buffer limit') from None
    except asyncio.IncompleteReadError:
        raise ConnectionError('Stratum connection closed') from None
    if len(data)>max_bytes: raise ProtocolError('Stratum message exceeds size limit')
    try:
        value=json.loads(data.decode('utf-8'),parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError,RecursionError):
        raise ProtocolError('Invalid Stratum JSON') from None
    if not isinstance(value,dict): raise ProtocolError('Stratum message must be an object')
    return value


async def write_message(writer,message):
    data=json.dumps(message,separators=(',',':'),allow_nan=False).encode('utf-8')+b'\n'
    if len(data)>65536: raise ProtocolError('Stratum message exceeds size limit')
    writer.write(data)
    await asyncio.wait_for(writer.drain(),30)


class StratumSession:
    def __init__(self,endpoint,version_rolling=False):
        self.endpoint=endpoint
        self.generation=uuid.uuid4().hex
        self.pending={}
        self.notifications=asyncio.Queue(maxsize=256)
        self.writer=None;self.reader=None;self.reader_task=None
        self.counter=0;self.write_lock=asyncio.Lock()
        self.extranonce1=None;self.extranonce2_size=None
        self.closed=True
        self.version_rolling=version_rolling;self.version_mask=None

    async def connect(self,negotiate=True):
        await self.close()
        self.generation=uuid.uuid4().hex
        self.notifications=asyncio.Queue(maxsize=256)
        self.counter=0
        self.version_mask=None
        self.reader,self.writer=await asyncio.wait_for(
            asyncio.open_connection(self.endpoint.host,self.endpoint.port,limit=65537),30)
        self.closed=False
        self.reader_task=asyncio.create_task(self._read_loop())
        try:
            if self.version_rolling and negotiate:
                try:
                    configured=await self.request('mining.configure',[['version-rolling'],
                        {'version-rolling.mask':'ffffffff','version-rolling.min-bit-count':0}],timeout=5)
                except RemoteError:
                    configured=None
                except (ConnectionError,OSError,TimeoutError):
                    # Legacy pools may close on an unknown first method.
                    await self.close()
                    return await self.connect(negotiate=False)
                if isinstance(configured,dict) and configured.get('version-rolling') is True:
                    self.version_mask=version_mask(configured.get('version-rolling.mask'))
            subscription=await self.request('mining.subscribe',['scrypt-hash-router/0.1'])
            if not isinstance(subscription,list) or len(subscription)<3:
                raise ProtocolError('Invalid subscribe response')
            nonce,size=subscription[1:3]
            if not isinstance(nonce,str) or len(nonce)%2 or len(nonce)>64:
                raise ProtocolError('Invalid extranonce')
            try: bytes.fromhex(nonce)
            except ValueError: raise ProtocolError('Invalid extranonce') from None
            if isinstance(size,bool) or not isinstance(size,int) or not 1<=size<=32:
                raise ProtocolError('Unsupported extranonce size')
            self.extranonce1=nonce;self.extranonce2_size=size
            authorized=await self.request('mining.authorize',[self.endpoint.username,self.endpoint.password])
            if authorized is not True: raise RemoteError('Upstream authorization rejected')
        except BaseException:
            await self.close();raise

    async def request(self,method,params,timeout=30):
        if self.closed: raise ConnectionError('Stratum connection is closed')
        request_id=self.counter;self.counter+=1
        future=asyncio.get_running_loop().create_future()
        self.pending[(int,request_id)]=future
        try:
            async with self.write_lock:
                await write_message(self.writer,{'id':request_id,'method':method,'params':params})
            return await asyncio.wait_for(future,timeout)
        finally:
            self.pending.pop((int,request_id),None)

    async def _read_loop(self):
        try:
            while True:
                message=await read_message(self.reader,timeout=600)
                if 'method' in message:
                    if not isinstance(message.get('method'),str) or not isinstance(message.get('params',[]),list):
                        raise ProtocolError('Invalid Stratum notification')
                    self.notifications.put_nowait(message)
                    continue
                request_id=message.get('id')
                if isinstance(request_id,(str,int)) and not isinstance(request_id,bool):
                    future=self.pending.get((type(request_id),request_id))
                    if future is not None and not future.done():
                        if message.get('error') is not None:
                            future.set_exception(RemoteError('Upstream request rejected'))
                        else: future.set_result(message.get('result'))
        except asyncio.CancelledError:
            pass
        except (ConnectionError,ValueError,TimeoutError,asyncio.QueueFull,OSError):
            pass
        finally:
            self.closed=True
            for future in tuple(self.pending.values()):
                if not future.done(): future.set_exception(ConnectionError('Stratum connection closed'))
            if self.writer is not None: self.writer.close()

    async def close(self):
        self.closed=True
        for future in tuple(self.pending.values()):
            if not future.done(): future.set_exception(ConnectionError('Stratum connection closed'))
        task=self.reader_task;self.reader_task=None
        if task is not None and task is not asyncio.current_task():
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        if self.writer is not None:
            self.writer.close()
            try: await self.writer.wait_closed()
            except (ConnectionError,OSError): pass
        self.writer=None;self.reader=None
