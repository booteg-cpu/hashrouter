import asyncio
import tempfile
import unittest
from contextlib import AsyncExitStack
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from hash_router.models import Endpoint,OutputSpec,SourceSpec,RouterSnapshot
from hash_router.state import AtomicState
from hash_router.runtime import RouterRuntime
from hash_router.metrics import Metrics,DifficultyConvention
from hash_router.protocol import write_message
from tests_hash_router.helpers import FakePool,fake_server,mining_client


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_17gh_seven_outputs_restart_and_customer_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with AsyncExitStack() as stack:
                pools=[FakePool(f'{i+1:02x}') for i in range(9)]
                endpoints=[await stack.enter_async_context(fake_server(p.handler)) for p in pools]
                source=SourceSpec('s','a','99','r',D(17),9999999999,'input','password')
                outputs=tuple(OutputSpec(f'slot-{i+1}',str(i+1),D('2.2'),endpoints[i]) for i in range(7))
                snapshot=RouterSnapshot(1,1,(source,),outputs,endpoints[7],120,.01)
                state=AtomicState(Path(tmp)/'router.json')
                rt=RouterRuntime(state);await rt.apply(snapshot);await rt.start('127.0.0.1',0)
                try:
                    _,coinbases=await mining_client(Endpoint('127.0.0.1',rt.port,'input','password'),jobs=34)
                    self.assertTrue(all(p.shares for p in pools[:8]))
                    self.assertCountEqual(coinbases,[c for p in pools[:8] for _,c in p.shares])
                    for writer in tuple(pools[0].writers):
                        await write_message(writer,{'id':None,'method':'client.reconnect','params':[endpoints[8].host,endpoints[8].port,0]})
                    for _ in range(200):
                        if rt.outputs['slot-1'].session and rt.outputs['slot-1'].endpoint==endpoints[8] and rt.outputs['slot-1'].latest: break
                        await asyncio.sleep(.01)
                    await mining_client(Endpoint('127.0.0.1',rt.port,'input','password'),jobs=17)
                    self.assertTrue(pools[8].shares)
                    failed=replace(outputs[1],endpoint=Endpoint('127.0.0.1',1,'worker','password'))
                    changed=replace(snapshot,revision=2,outputs=(outputs[0],failed,*outputs[2:]))
                    await rt.apply(changed);await rt.apply(changed)
                    await mining_client(Endpoint('127.0.0.1',rt.port,'input','password'),jobs=17)
                finally: await rt.stop()
                rt=RouterRuntime(state);await rt.start('127.0.0.1',0)
                try:
                    self.assertEqual(rt.snapshot.revision,2)
                    await mining_client(Endpoint('127.0.0.1',rt.port,'input','password'),jobs=17)
                finally: await rt.stop()
    async def test_bot_vps_disconnect_ends_on_known_deadline(self):
        wall=[100.0]
        pool=FakePool('01')
        with tempfile.TemporaryDirectory() as tmp:
            async with fake_server(pool.handler) as endpoint:
                source=SourceSpec('s','a','99','r',D(17),101,'input','password')
                snapshot=RouterSnapshot(1,1,(source,),(),endpoint,120,.01)
                state=AtomicState(Path(tmp)/'router.json');rt=RouterRuntime(state,wall_clock=lambda:wall[0])
                await rt.apply(snapshot);await rt.start('127.0.0.1',0)
                try:
                    client=Endpoint('127.0.0.1',rt.port,'input','password')
                    await mining_client(client,jobs=2)
                    wall[0]=102
                    auth,_=await mining_client(client,jobs=0)
                    self.assertFalse(auth['result'])
                    self.assertEqual({},rt.scheduler.connection_capacities)
                    restored=RouterRuntime(state,wall_clock=lambda:wall[0])
                    self.assertEqual(restored.snapshot.sources[0].expires_at,101)
                finally:await rt.stop()
    async def test_verified_share_rates_feed_connection_scheduler(self):
        pool=FakePool('01')
        with tempfile.TemporaryDirectory() as tmp:
            async with fake_server(pool.handler) as endpoint:
                source=SourceSpec('s','a','99','r',D(17),9999999999,'input','password')
                snapshot=RouterSnapshot(1,1,(source,),(),endpoint,120,.01)
                metrics=Metrics(convention=DifficultyConvention(D(65536),True,'tested'))
                rt=RouterRuntime(AtomicState(Path(tmp)/'router.json'),metrics=metrics)
                await rt.apply(snapshot);await rt.start('127.0.0.1',0)
                try:
                    # Hold a client while its shares arrive so scheduler feedback is observable.
                    task=asyncio.create_task(mining_client(Endpoint('127.0.0.1',rt.port,'input','password'),jobs=40))
                    try:
                        for _ in range(200):
                            if metrics.counts['remainder']['accepted']>=2: break
                            await asyncio.sleep(.002)
                        self.assertTrue(any(capacity>0 for _,capacity in rt.scheduler.connections.values()))
                    finally: await task
                finally: await rt.stop()


