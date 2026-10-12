import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from hash_router.runtime import RouterRuntime
from hash_router.multi import MemoryState


class HandshakeLogTests(unittest.IsolatedAsyncioTestCase):
    async def run_client(self, requests=(), raw=None, failure=None):
        rt=RouterRuntime(MemoryState(),algorithm='sha256ab');rt.running=True
        reader=asyncio.StreamReader()
        reader.feed_data(raw if raw is not None else b''.join(
            json.dumps(r).encode()+b'\n' for r in requests))
        reader.feed_eof()
        writer=Mock();writer.drain=AsyncMock();writer.wait_closed=AsyncMock()
        if failure:
            with patch('hash_router.runtime.read_message',AsyncMock(side_effect=failure)):
                await rt._handle_client(reader,writer)
        else:await rt._handle_client(reader,writer)
        return rt.event_log.read()['entries'], writer

    async def test_subscribe_configure_then_peer_closes(self):
        rows,writer=await self.run_client([
            {'id':1,'method':'mining.configure','params':[['version-rolling'],{}]},
            {'id':2,'method':'mining.subscribe','params':['private-firmware']}])
        self.assertEqual(['mining.configure','mining.subscribe'],[
            r['method'] for r in rows if r['event']=='input_handshake_request'])
        self.assertEqual(2,sum(r['event']=='input_handshake_reply' for r in rows))
        last=rows[-1]
        self.assertEqual(('connection_closed','await_request','mining.subscribe'),
                         (last['reason'],last['phase'],last['method']))
        self.assertTrue(last['subscribed']);self.assertFalse(last['authorized'])
        self.assertNotIn('private-firmware',json.dumps(rows))
        self.assertEqual(2,writer.write.call_count)

    async def test_close_before_first_message(self):
        rows,_=await self.run_client()
        self.assertEqual('none',rows[-1]['method'])
        self.assertFalse(rows[-1]['subscribed'])

    async def test_denial_hides_credentials(self):
        rows,_=await self.run_client([
            {'id':1,'method':'mining.subscribe','params':[]},
            {'id':2,'method':'mining.authorize','params':['secret-user','secret-password']}])
        self.assertEqual('authorization_denied',rows[-1]['reason'])
        self.assertIn('denied',[r.get('status') for r in rows])
        self.assertNotIn('secret-',json.dumps(rows))

    async def test_protocol_error_timeout_and_transport_are_distinct(self):
        rows,_=await self.run_client(raw=b'not-json\n')
        self.assertEqual('protocol_error',rows[-1]['reason'])
        for exc,reason in ((TimeoutError('secret'), 'timeout'),(OSError('secret'),'transport_error')):
            rows,_=await self.run_client(failure=exc)
            self.assertEqual(reason,rows[-1]['reason']);self.assertNotIn('secret',json.dumps(rows))

    async def test_unknown_method_is_not_logged_verbatim_and_volume_is_bounded(self):
        rows,_=await self.run_client([{'id':i,'method':'secret-password','params':[]} for i in range(80)])
        self.assertEqual(32,sum(r['event']=='input_handshake_request' for r in rows))
        self.assertNotIn('secret-password',json.dumps(rows))

if __name__=='__main__':unittest.main()
