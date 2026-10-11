import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from aiohttp.test_utils import TestClient, TestServer
from hash_router.state import AtomicState
from hash_router.models import RouterSnapshot
from hash_router.multi import MultiRouter, create_multi_app
from tests_hash_router.test_control import snapshot
from tests_hash_router.helpers import FakePool, fake_server, mining_client
from hash_router.models import Endpoint


class MultiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = AtomicState(Path(self.tmp.name)/'state.json')
        self.router = MultiRouter(self.state, '127.0.0.1', 0, 8443)
        self.client = TestClient(TestServer(create_multi_app(self.router, 'x'*32)))
        await self.client.start_server(); self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.router.stop)
        self.headers = {'Authorization':'Bearer '+'x'*32}

    async def catalog(self):
        return await self.client.put('/v2/algorithms', json={'algorithms':{
            'scrypt':{'title':'Scrypt','unit':'gh'},
            'sha256ab':{'title':'SHA256','unit':'th'},
            'equihash':{'title':'Equihash','unit':'kh'}}}, headers=self.headers)

    async def put_group(self, code, raw):
        return await self.client.put('/v2/algorithms/'+code+'/config', json=raw, headers=self.headers)

    async def test_catalog_auth_persistence_and_capability(self):
        self.assertEqual(401,(await self.client.put('/v2/algorithms',json={})).status)
        self.assertEqual(200,(await self.catalog()).status)
        restored=MultiRouter(self.state,'127.0.0.1',0,8443)
        self.assertEqual('th',restored.catalog['sha256ab']['unit'])
        status=self.router.status().to_dict()
        self.assertEqual('unsupported',status['algorithms']['equihash']['routing'])
        self.assertEqual('unconfigured',status['algorithms']['sha256ab']['routing'])

    async def test_two_groups_isolated_ports_capacity_and_restart(self):
        await self.catalog()
        for code, power in [('scrypt','17'),('sha256ab','200000')]:
            raw=snapshot().to_dict(True); raw['algorithm']=code
            raw['sources'][0]['advertised_gh']=power
            raw['sources'][0]['expires_at']=9999999999
            self.assertEqual(200,(await self.put_group(code,raw)).status)
        await self.router.start()
        left,right=self.router.groups['scrypt'],self.router.groups['sha256ab']
        self.assertNotEqual(left.port,right.port)
        self.assertIsNot(left.scheduler,right.scheduler)
        left.scheduler.set_connection_capacity('a','s',0)
        right.scheduler.set_connection_capacity('b','s',0)
        self.assertEqual('17',str(left.scheduler.connection_capacities['a']))
        self.assertEqual('200000',str(right.scheduler.connection_capacities['b']))
        self.assertFalse(right.metrics.convention.verified)
        restored=MultiRouter(self.state,'127.0.0.1',0,8443)
        self.assertEqual(2,len(restored.groups))
        self.assertEqual('sha256ab',restored.groups['sha256ab'].snapshot.algorithm)

    async def test_wrong_group_and_unsupported_protocol_blocked(self):
        await self.catalog()
        raw=snapshot().to_dict(True)
        self.assertEqual(409,(await self.put_group('sha256ab',raw)).status)
        raw['algorithm']='equihash'
        self.assertEqual(409,(await self.put_group('equihash',raw)).status)
        self.assertEqual({},self.router.groups)

    async def test_removed_algorithm_preserves_paid_group(self):
        await self.catalog()
        raw=snapshot().to_dict(True);raw['outputs'][0]['rented_until']=9999999999
        self.assertEqual(200,(await self.put_group('scrypt',raw)).status)
        response=await self.client.put('/v2/algorithms',json={'algorithms':{}},headers=self.headers)
        self.assertEqual(200,response.status)
        self.assertFalse(self.router.catalog['scrypt']['active'])
        self.assertIn('scrypt',self.router.groups)
        raw['revision']=2;raw['outputs']=[]
        self.assertEqual(409,(await self.put_group('scrypt',raw)).status)

    async def test_save_failure_keeps_catalog(self):
        original=self.state.save;self.state.save=Mock(side_effect=OSError())
        self.assertEqual(503,(await self.catalog()).status)
        self.assertEqual({},self.router.catalog)
        self.state.save=original

    async def test_legacy_state_and_endpoint(self):
        self.state.save(snapshot().to_dict(True))
        legacy=MultiRouter(self.state,'127.0.0.1',0,8443)
        self.assertEqual(1,legacy.snapshot.revision)
        self.assertIn('scrypt',legacy.groups)
        self.assertEqual('scrypt',RouterSnapshot.from_dict(snapshot().to_dict(True)).algorithm)

    async def test_simultaneous_tcp_groups_do_not_cross_jobs_or_credentials(self):
        await self.catalog()
        pools=[FakePool('01'),FakePool('02')]
        async with fake_server(pools[0].handler) as a,fake_server(pools[1].handler) as b:
            for code,endpoint,username in [('scrypt',a,'scrypt-input'),('sha256ab',b,'sha-input')]:
                raw=snapshot().to_dict(True);raw.update(algorithm=code,quantum_seconds=.01)
                raw['sources'][0].update(username=username,password='password',expires_at=9999999999)
                raw['outputs']=raw['outputs'][:1];raw['outputs'][0]['endpoint']=endpoint.__dict__ if hasattr(endpoint,'__dict__') else {
                    'host':endpoint.host,'port':endpoint.port,'username':endpoint.username,'password':endpoint.password}
                raw['remainder']=raw['outputs'][0]['endpoint']
                self.assertEqual(200,(await self.put_group(code,raw)).status)
            await self.router.start()
            endpoints=[Endpoint('127.0.0.1',self.router.groups[code].port,user,'password')
                       for code,user in [('scrypt','scrypt-input'),('sha256ab','sha-input')]]
            failed,_=await mining_client(endpoints[1],username='scrypt-input')
            self.assertFalse(failed['result'])
            results=await asyncio.gather(*(mining_client(e,username=e.username,jobs=4) for e in endpoints))
            for index,(auth,coinbases) in enumerate(results):
                self.assertTrue(auth['result'])
                self.assertEqual(4,len(coinbases))
                self.assertTrue(all(c.startswith(pools[index].label) for c in coinbases))
                self.assertEqual(4,len(pools[index].shares))
            await self.router.stop()

    async def test_port_conflict_does_not_persist_new_group(self):
        await self.catalog();await self.router.start()
        server=await asyncio.start_server(lambda r,w:w.close(),'127.0.0.1',0)
        self.router.ports['scrypt']=server.sockets[0].getsockname()[1]
        try:
            self.assertEqual(503,(await self.put_group('scrypt',snapshot().to_dict(True))).status)
            self.assertNotIn('scrypt',self.router.groups)
            self.assertNotIn('scrypt',self.router.saved)
        finally:server.close();await server.wait_closed()

    async def test_explicit_protocol_can_enable_new_compatible_algorithm(self):
        response=await self.client.put('/v2/algorithms',json={'algorithms':{
            'x11':{'title':'X11','unit':'gh','protocol':'bitcoin-stratum-v1'}}},headers=self.headers)
        self.assertEqual(200,response.status)
        raw=snapshot().to_dict(True);raw['algorithm']='x11'
        self.assertEqual(200,(await self.put_group('x11',raw)).status)


if __name__=='__main__':unittest.main()
