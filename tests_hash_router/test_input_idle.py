import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from hash_router.runtime import RouterRuntime, InputConnection
from hash_router.multi import MemoryState
from hash_router.protocol import read_message, ProtocolError


class InputIdleTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self,authorized=True):
        rt=RouterRuntime(MemoryState(),wall_clock=lambda:100)
        rt.running=True
        context=InputConnection('c',asyncio.StreamReader(),None)
        if authorized: context.source=SimpleNamespace(expires_at=200)
        return rt,context

    async def test_authenticated_idle_keeps_partial_message_and_receives_later(self):
        rt,context=self.runtime()
        context.reader.feed_data(b'{"id":1,')
        async def accelerated(reader,timeout):
            self.assertEqual(600,timeout)
            return await read_message(reader,timeout=.01)
        with patch('hash_router.runtime.read_message',accelerated):
            task=asyncio.create_task(rt._read_input(context))
            try:
                async with asyncio.timeout(2):
                    while not rt.event_log.read()['entries']: await asyncio.sleep(.001)
                self.assertFalse(task.done())
                context.reader.feed_data(b'"method":"mining.submit","params":[]}\n')
                message=await asyncio.wait_for(task,1)
                self.assertEqual('mining.submit',message['method'])
                self.assertEqual(0,context.idle_waits)
                self.assertEqual('input_idle',rt.event_log.read()['entries'][0]['event'])
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_unauthorized_and_expired_sources_still_timeout(self):
        for authorized in (False,True):
            rt,context=self.runtime(authorized)
            if authorized: context.source.expires_at=100
            mocked=AsyncMock(side_effect=TimeoutError)
            with patch('hash_router.runtime.read_message',mocked):
                with self.assertRaises(TimeoutError): await rt._read_input(context)
            self.assertEqual(600 if authorized else 15,mocked.call_args.kwargs['timeout'])
            self.assertEqual([],rt.event_log.read()['entries'])

    async def test_eof_invalid_frame_and_cancellation_are_not_swallowed(self):
        for data,error in ((None,ConnectionError),(b'bad-json\n',ProtocolError)):
            rt,context=self.runtime()
            if data: context.reader.feed_data(data)
            else: context.reader.feed_eof()
            with self.assertRaises(error): await rt._read_input(context)
        rt,context=self.runtime()
        task=asyncio.create_task(rt._read_input(context))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
