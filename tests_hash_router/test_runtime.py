import asyncio
import unittest
import tempfile
from pathlib import Path
from decimal import Decimal as D
from dataclasses import replace
from hash_router.runtime import RouterRuntime
from hash_router.state import AtomicState
from hash_router.models import SourceSpec,OutputSpec,RouterSnapshot,Endpoint
from tests_hash_router.helpers import FakePool,fake_server,mining_client

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def run_setup(self,first,second,remainder):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        source=SourceSpec('s','a','1','r',D('17'),9999999999,'input','password')
        snap=RouterSnapshot(1,1,(source,),(
            OutputSpec('one','1',D('6'),first),OutputSpec('two','2',D('6'),second)),remainder,120,.01)
        rt=RouterRuntime(AtomicState(Path(self.tmp.name)/'state.json'))
        await rt.apply(snap);await rt.start('127.0.0.1',0)
        self.addAsyncCleanup(rt.stop)
        return rt,Endpoint('127.0.0.1',rt.port,'input','password')
    async def test_two_buyers_and_remainder_end_to_end(self):
        pools=[FakePool('01'),FakePool('02'),FakePool('03')]
        async with fake_server(pools[0].handler) as a,fake_server(pools[1].handler) as b,fake_server(pools[2].handler) as c:
            rt,endpoint=await self.run_setup(a,b,c)
            _,coinbases=await mining_client(endpoint,jobs=12)
            received=[coinbase for pool in pools for _,coinbase in pool.shares]
            self.assertCountEqual(coinbases,received)
            self.assertTrue(all(pool.shares for pool in pools))
            self.assertEqual(12,sum(row['accepted'] for row in rt.status().to_dict()['outputs']))
            await rt.stop()
    async def test_client_reconnect_changes_output_session(self):
        pool=FakePool('01');new=FakePool('02');remainder=FakePool('03')
        async with fake_server(pool.handler) as a,fake_server(new.handler) as b,fake_server(remainder.handler) as c:
            rt,endpoint=await self.run_setup(a,a,c)
            await mining_client(endpoint,jobs=3)
            for writer in tuple(pool.writers):
                await __import__('hash_router.protocol',fromlist=['write_message']).write_message(writer,
                    {'id':None,'method':'client.reconnect','params':[b.host,b.port,0]})
            for _ in range(200):
                if any(slot.endpoint.port==b.port and slot.latest and not slot.session.closed for slot in rt.outputs.values()): break
                await asyncio.sleep(.01)
            await mining_client(endpoint,jobs=6)
            self.assertTrue(new.shares)
            await rt.stop()
    async def test_output_failure_uses_remainder(self):
        pool=FakePool('01');remainder=FakePool('03')
        async with fake_server(pool.handler) as a,fake_server(remainder.handler) as c:
            unavailable=Endpoint('127.0.0.1',1,'worker','password')
            rt,endpoint=await self.run_setup(a,unavailable,c)
            await mining_client(endpoint,jobs=6)
            self.assertTrue(pool.shares and remainder.shares)
            await rt.stop()
    async def test_auth_limits_and_shutdown(self):
        pool=FakePool('01')
        async with fake_server(pool.handler) as a:
            rt,endpoint=await self.run_setup(a,a,a)
            auth,_=await mining_client(endpoint,password='wrong')
            self.assertFalse(auth['result'])
            await rt.stop()
            self.assertEqual(0,rt.connection_count)
            self.assertTrue(all(slot.session is None or slot.session.closed for slot in rt.outputs.values()))
    async def test_shutdown_with_authenticated_client_still_open(self):
        pool=FakePool('01')
        async with fake_server(pool.handler) as a:
            rt,endpoint=await self.run_setup(a,a,a)
            reader,writer=await asyncio.open_connection(endpoint.host,endpoint.port)
            from hash_router.protocol import read_message,write_message
            await write_message(writer,{'id':0,'method':'mining.subscribe','params':[]});await read_message(reader)
            await write_message(writer,{'id':1,'method':'mining.authorize','params':['input','password']});await read_message(reader)
            try: await asyncio.wait_for(rt.stop(),.5)
            finally: writer.close();await writer.wait_closed()

if __name__=='__main__': unittest.main()
