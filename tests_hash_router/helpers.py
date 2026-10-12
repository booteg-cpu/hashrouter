import asyncio
from contextlib import asynccontextmanager
from hash_router.models import Endpoint
from hash_router.protocol import read_message,write_message

@asynccontextmanager
async def fake_server(handler):
    tasks=set()
    async def accepted(reader,writer):
        task=asyncio.current_task();tasks.add(task)
        try: await handler(reader,writer)
        except ConnectionError: pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except ConnectionError: pass
            tasks.discard(task)
    server=await asyncio.start_server(accepted,'127.0.0.1',0)
    endpoint=Endpoint('127.0.0.1',server.sockets[0].getsockname()[1],'worker','password')
    try: yield endpoint
    finally:
        server.close()
        for task in tuple(tasks): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await server.wait_closed()

class FakePool:
    def __init__(self,label):
        self.label=label;self.shares=[];self.writers=set();self.reconnect=None
    async def handler(self,reader,writer):
        self.writers.add(writer)
        try:
            while True:
                req=await read_message(reader)
                method=req['method'];params=req['params']
                if method=='mining.subscribe': result=[[],'bb',4]
                elif method=='mining.authorize': result=True
                elif method=='mining.submit':
                    result=params[1]==self.label and len(params[2])==8
                    if result: self.shares.append((params,self.label+'bb'+params[2]+'0304'))
                else: result=True
                await write_message(writer,{'id':req['id'],'result':result,'error':None})
                if method=='mining.authorize':
                    await write_message(writer,{'id':None,'method':'mining.set_difficulty','params':[1]})
                    await write_message(writer,{'id':None,'method':'mining.notify',
                        'params':[self.label,'00'*32,self.label,'0304',[],'00000001','1d00ffff','12345678',True]})
        finally: self.writers.discard(writer)

async def mining_client(endpoint,username='input',password='password',jobs=3):
    reader,writer=await asyncio.open_connection(endpoint.host,endpoint.port)
    coinbases=[];notifications=[]
    try:
        await write_message(writer,{'id':0,'method':'mining.subscribe','params':[]})
        subscribe=await read_message(reader);extra1,size=subscribe['result'][1:3]
        await write_message(writer,{'id':1,'method':'mining.authorize','params':[username,password]})
        auth=await read_message(reader)
        if not auth.get('result'): return auth,coinbases
        job=None
        for i in range(jobs):
            # A miner can submit multiple shares for one unchanged job.
            while True:
                try: message=await read_message(reader,timeout=3 if job is None else .03)
                except TimeoutError: break
                if message.get('method')=='mining.notify':
                    job=message['params'];break
            if job is None: raise AssertionError('No initial mining job')
            extra2=f'{i:0{size*2}x}'
            coinbases.append(job[2]+extra1+extra2+job[3])
            await write_message(writer,{'id':i+2,'method':'mining.submit',
                'params':[username,job[0],extra2,job[7],'aabbccdd']})
            while True:
                reply=await read_message(reader,timeout=3)
                if reply.get('method')=='mining.notify': job=reply['params']
                if reply.get('id')==i+2:
                    if not reply.get('result'): raise AssertionError(reply)
                    break
        return auth,coinbases
    finally:
        writer.close();await writer.wait_closed()
