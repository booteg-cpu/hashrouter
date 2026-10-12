import json
import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import Mock
from aiohttp.test_utils import TestClient,TestServer
from hash_router.eventlog import EventLog
from hash_router.multi import MultiRouter,MemoryState,create_multi_app
from tests_hash_router.test_control import snapshot


class EventLogTests(unittest.TestCase):
    def test_bounded_paging_filters_gaps_and_reset(self):
        log=EventLog(capacity=4,level='DEBUG')
        for i in range(8):log.emit('test',algorithm='scrypt' if i%2 else 'sha256ab')
        page=log.read(after=1,limit=2,algorithm='scrypt')
        self.assertTrue(page['gap']);self.assertEqual([6,8],[r['seq'] for r in page['entries']])
        self.assertEqual(8,page['next']);self.assertEqual([],log.read(after=8)['entries'])
        self.assertTrue(log.read(after=99,stream='old')['reset'])
        for kwargs in ({'after':-1},{'limit':501},{'level':'bad'}):
            with self.assertRaises(ValueError):log.read(**kwargs)

    def test_strict_fields_secrets_and_no_control_characters(self):
        log=EventLog();secret='secret-password-123';log.protect(secret,'x')
        log.emit('output_example',host='host-'+secret+'\n\x1b',password=secret,token=secret,
                 params={'password':secret},reason='x')
        result=json.dumps(log.read())
        self.assertNotIn(secret,result);self.assertNotIn('password',result);self.assertNotIn('params',result)
        self.assertEqual('output_example',log.rows[0]['event'])
        self.assertEqual('[redacted]',log.rows[0]['reason'])
        self.assertNotIn('\x1b',log.rows[0]['host'])

    def test_rotation_recovery_and_disk_failure_keeps_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.jsonl';log=EventLog(path,max_bytes=500,backups=2)
            for i in range(20):log.emit('rotation',count=i)
            stream=log.stream;seq=log.sequence;log.close()
            self.assertTrue(path.with_name('events.jsonl.1').exists())
            self.assertLessEqual(len(list(Path(tmp).glob('events*'))),3)
            with path.open('a') as out:out.write('{incomplete')
            recovered=EventLog(path)
            self.assertEqual(stream,recovered.stream);self.assertEqual(seq,recovered.sequence)
            recovered.handler.stream.close();recovered.handler.stream=Mock()
            recovered.handler.shouldRollover=Mock(return_value=False)
            recovered.handler.stream.write.side_effect=OSError('disk full')
            recovered.emit('still_alive')
            self.assertTrue(recovered.write_failed)
            self.assertEqual('still_alive',recovered.read()['entries'][-1]['event'])
            recovered.close()

    def test_debug_is_optional_and_level_change_is_explicit(self):
        log=EventLog();log.emit('share_accepted','DEBUG')
        self.assertEqual([],log.read()['entries'])
        log.set_level('DEBUG');log.emit('share_accepted','DEBUG')
        self.assertEqual('share_accepted',log.read()['entries'][-1]['event'])
        with self.assertRaises(ValueError):log.set_level('SECRET')

    def test_slow_disk_cannot_block_mining_and_overflow_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=EventLog(Path(tmp)/'events.jsonl',async_write=True)
            entered=threading.Event();release=threading.Event();original=log._persist
            def slow(row):
                entered.set();release.wait(2);original(row)
            log._persist=slow
            log.emit('start');self.assertTrue(entered.wait(1))
            try:
                for i in range(5000):log.emit('bounded',count=i)
                self.assertEqual(2000,len(log.rows));self.assertGreater(log.read()['persist_dropped'],0)
            finally:release.set();log.close()


class LogApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.router=MultiRouter(MemoryState(),'127.0.0.1',0,8443)
        self.token='private-access-token-'+('z'*40)
        self.client=TestClient(TestServer(create_multi_app(self.router,self.token)))
        await self.client.start_server();self.addAsyncCleanup(self.client.close)
        self.headers={'Authorization':'Bearer '+self.token}

    async def test_auth_query_validation_and_level(self):
        self.assertEqual(401,(await self.client.get('/v2/logs')).status)
        self.assertEqual(401,(await self.client.put('/v2/logging',json={'level':'DEBUG'})).status)
        for query in ('limit=501','after=-1','level=BAD','after=text'):
            self.assertEqual(400,(await self.client.get('/v2/logs?'+query,headers=self.headers)).status)
        response=await self.client.put('/v2/logging',json={'level':'DEBUG'},headers=self.headers)
        self.assertEqual(200,response.status);self.assertTrue((await response.json())['temporary'])
        self.router.event_log.emit('test',algorithm='scrypt')
        response=await self.client.get('/v2/logs?algorithm=scrypt',headers=self.headers)
        data=await response.json();self.assertEqual(['test'],[row['event'] for row in data['entries']])
        self.assertNotIn(self.token,json.dumps(data))

    async def test_configuration_events_hide_all_credentials(self):
        raw=snapshot().to_dict(True)
        raw['sources'][0]['password']='source-secret-123'
        await self.router.sync_catalog({'algorithms':{'scrypt':{'title':'Scrypt','unit':'gh'}}})
        response=await self.client.put('/v2/algorithms/scrypt/config',json=raw,headers=self.headers)
        self.assertEqual(200,response.status)
        group=self.router.groups['scrypt']
        group.log('test_error','WARNING',reason='source-secret-123',password='source-secret-123')
        data=await (await self.client.get('/v2/logs',headers=self.headers)).json()
        self.assertIn('configuration_applied',[r['event'] for r in data['entries']])
        self.assertNotIn('source-secret-123',json.dumps(data))


if __name__=='__main__':unittest.main()
