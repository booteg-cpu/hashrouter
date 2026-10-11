import asyncio
import unittest
from hash_router.protocol import read_message,write_message,StratumSession,ProtocolError
from tests_hash_router.helpers import fake_server

class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_and_batched_json(self):
        reader=asyncio.StreamReader(limit=65537)
        reader.feed_data(b'{"id":0,'); task=asyncio.create_task(read_message(reader))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        reader.feed_data(b'"result":true}\n{"method":"mining.notify","params":[]}\n')
        self.assertEqual({'id':0,'result':True},await task)
        self.assertEqual('mining.notify',(await read_message(reader))['method'])
        for payload in (b'\xff\n',b'not-json\n',b'[]\n',b'x'*65537+b'\n'):
            bad=asyncio.StreamReader(limit=65537);bad.feed_data(payload)
            with self.assertRaises(ProtocolError): await read_message(bad)

    async def test_handshake_and_ids(self):
        seen=[]
        async def handler(reader,writer):
            while True:
                request=await read_message(reader);seen.append(request['method'])
                result=[[['mining.notify','sid']],'abcd',4] if request['method']=='mining.subscribe' else True
                if request['method']=='echo':
                    await write_message(writer,{'id':None,'method':'mining.set_difficulty','params':[2]})
                    result=request['params']
                await write_message(writer,{'id':request['id'],'result':result,'error':None})
        async with fake_server(handler) as endpoint:
            session=StratumSession(endpoint)
            await session.connect()
            self.assertEqual(['mining.subscribe','mining.authorize'],seen)
            self.assertEqual(['hello'],await session.request('echo',['hello']))
            self.assertEqual('mining.set_difficulty',(await session.notifications.get())['method'])
            self.assertEqual('abcd',session.extranonce1)
            old=session.generation;await session.close()
            await session.connect();self.assertNotEqual(old,session.generation)
            await session.close()

    async def test_timeout_and_close(self):
        async def handler(reader,writer):
            while True:
                req=await read_message(reader)
                if req['method']=='mining.subscribe': result=[[],'aa',4]
                elif req['method']=='mining.authorize': result=True
                else: continue
                await write_message(writer,{'id':req['id'],'result':result,'error':None})
        async with fake_server(handler) as endpoint:
            session=StratumSession(endpoint);await session.connect()
            with self.assertRaises(TimeoutError): await session.request('silent',[],timeout=.01)
            pending=asyncio.create_task(session.request('silent',[]));await asyncio.sleep(.01)
            await session.close()
            with self.assertRaises(ConnectionError): await pending
            self.assertEqual({},session.pending)

if __name__=='__main__': unittest.main()
