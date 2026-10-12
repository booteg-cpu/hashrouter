import asyncio
import unittest
from decimal import Decimal as D
from hash_router.models import RouterSnapshot, SourceSpec
from hash_router.multi import MemoryState
from hash_router.runtime import RouterRuntime
from hash_router.protocol import read_message, write_message
from tests_hash_router.helpers import FakePool, fake_server


class JobStabilityTests(unittest.IsolatedAsyncioTestCase):
    async def connected(self, endpoint):
        rt = RouterRuntime(MemoryState())
        source = SourceSpec('s', 'a', '1', 'r', D(97), 9999999999, 'input', 'password')
        await rt.apply(RouterSnapshot(1, 1, (source,), (), endpoint, 120, .02))
        await rt.start('127.0.0.1', 0)
        self.addAsyncCleanup(rt.stop)
        async with asyncio.timeout(3):
            while rt.outputs['remainder'].state != 'ready':
                await asyncio.sleep(.01)
        reader, writer = await asyncio.open_connection('127.0.0.1', rt.port)
        self.addCleanup(writer.close)
        await write_message(writer, {'id': 0, 'method': 'mining.subscribe', 'params': []})
        await read_message(reader)
        await write_message(writer, {'id': 1, 'method': 'mining.authorize', 'params': ['input', 'password']})
        self.assertTrue((await read_message(reader))['result'])
        self.assertEqual('mining.set_difficulty', (await read_message(reader))['method'])
        first = await read_message(reader)
        self.assertEqual('mining.notify', first['method'])
        return rt, reader, writer, first['params']

    async def assert_silent(self, reader):
        with self.assertRaises(TimeoutError):
            await read_message(reader, timeout=.12)

    async def test_unchanged_work_survives_quanta_and_accepts_delayed_share(self):
        pool = FakePool('01')
        async with fake_server(pool.handler) as endpoint:
            rt, reader, writer, job = await self.connected(endpoint)
            await self.assert_silent(reader)
            self.assertGreater(rt.scheduler.allocated_work['remainder'], 0)
            self.assertEqual(1, next(iter(rt.clients.values())).diagnostics['jobs_sent'])
            # Repeating the identical upstream notify must not reset the input either.
            upstream = next(iter(pool.writers))
            await write_message(upstream, {'id': None, 'method': 'mining.notify',
                'params': list(rt.outputs['remainder'].latest)})
            await self.assert_silent(reader)
            await write_message(writer, {'id': 2, 'method': 'mining.submit',
                'params': ['input', job[0], '0011', job[7], 'aabbccdd']})
            self.assertTrue((await read_message(reader))['result'])
            self.assertEqual(1, len(pool.shares))

    async def test_new_job_and_difficulty_are_delivered_in_order(self):
        pool = FakePool('01')
        async with fake_server(pool.handler) as endpoint:
            rt, reader, writer, first = await self.connected(endpoint)
            upstream = next(iter(pool.writers))
            job = list(rt.outputs['remainder'].latest)
            job[7] = '12345679'
            await write_message(upstream, {'id': None, 'method': 'mining.notify', 'params': job})
            changed = await read_message(reader)
            self.assertEqual('mining.notify', changed['method'])
            self.assertNotEqual(first[0], changed['params'][0])
            self.assertFalse(rt.jobs.jobs[first[0]].valid)
            await self.assert_silent(reader)
            await write_message(upstream, {'id': None, 'method': 'mining.set_difficulty', 'params': [2]})
            self.assertEqual([2.0], (await read_message(reader))['params'])
            self.assertEqual('mining.notify', (await read_message(reader))['method'])
            await self.assert_silent(reader)

    async def test_reconnect_reissues_work_even_with_identical_job_fields(self):
        pool = FakePool('01')
        async with fake_server(pool.handler) as endpoint:
            rt, reader, writer, first = await self.connected(endpoint)
            next(iter(pool.writers)).close()
            changed = await read_message(reader, timeout=3)
            self.assertEqual('mining.notify', changed['method'])
            self.assertNotEqual(first[0], changed['params'][0])
            self.assertFalse(rt.jobs.jobs[first[0]].valid)
            await self.assert_silent(reader)
