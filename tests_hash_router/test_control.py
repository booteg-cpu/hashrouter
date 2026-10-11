import unittest,tempfile,io
from pathlib import Path
from unittest.mock import patch
from aiohttp.test_utils import TestClient,TestServer
from hash_router.control import create_control_app,validate_service_config
from hash_router.runtime import RouterRuntime
from hash_router.state import AtomicState
from tests_hash_router.test_scheduler import snapshot

class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=AtomicState(Path(self.tmp.name)/'state.json');self.rt=RouterRuntime(self.state)
        self.token='a'*40
        self.client=TestClient(TestServer(create_control_app(self.rt,self.token)))
        await self.client.start_server();self.addAsyncCleanup(self.client.close)
        self.headers={'Authorization':'Bearer '+self.token}
    async def test_revision_replay_and_conflict(self):
        raw=snapshot().to_dict(True)
        response=await self.client.put('/v1/config',json=raw,headers=self.headers);self.assertEqual(200,response.status)
        response=await self.client.put('/v1/config',json=raw,headers=self.headers);self.assertEqual(200,response.status)
        raw['quantum_seconds']=2
        response=await self.client.put('/v1/config',json=raw,headers=self.headers);self.assertEqual(409,response.status)
        raw['revision']=0
        response=await self.client.put('/v1/config',json=raw,headers=self.headers);self.assertEqual(409,response.status)
    async def test_restart_and_expired_source(self):
        raw=snapshot().to_dict(True);raw['sources'][0]['expires_at']=1
        await self.client.put('/v1/config',json=raw,headers=self.headers)
        restored=RouterRuntime(self.state)
        self.assertEqual(1,restored.snapshot.sources[0].expires_at)
        restored.scheduler.set_connection_capacity('c','s',0)
        self.assertEqual({},restored.scheduler.connection_capacities)
    async def test_api_auth_tls_and_limits(self):
        self.assertEqual(401,(await self.client.get('/v1/status')).status)
        self.assertEqual(413,(await self.client.put('/v1/config',data=io.BytesIO(b'x'*(1024*1024+1)),headers=self.headers)).status)
        with self.assertRaises(ValueError): validate_service_config({'api_token':self.token})
    async def test_save_failure_does_not_apply(self):
        with patch.object(self.state,'save',side_effect=OSError('disk failed')):
            response=await self.client.put('/v1/config',json=snapshot().to_dict(True),headers=self.headers)
        self.assertEqual(503,response.status)
        self.assertIsNone(self.rt.snapshot)
        self.assertEqual({},self.state.load())
    async def test_calibration_requires_explicit_boolean(self):
        config={'api_token':self.token,'tls_certificate':'cert.pem','tls_key':'key.pem','state_path':'state.json',
                'stratum_host':'0.0.0.0','stratum_port':3333,'control_host':'0.0.0.0','control_port':8443}
        config['difficulty_convention']={'verified':'yes','expected_hashes_per_difficulty':'65536','name':'test'}
        with self.assertRaises(ValueError):validate_service_config(config)
        config['difficulty_convention']['verified']=True
        self.assertTrue(validate_service_config(config)['difficulty_convention']['verified'])
        config['difficulty_convention']['expected_hashes_per_difficulty']='0'
        with self.assertRaises(ValueError):validate_service_config(config)
    async def test_legacy_sale_backfill_then_same_lease_is_immutable(self):
        self.rt.wall_clock=lambda:100
        raw=snapshot().to_dict(True)
        raw['outputs'][0]['rented_until']=7300
        from hash_router.models import RouterSnapshot
        await self.rt.apply(RouterSnapshot.from_dict(raw))
        raw['revision']=2;raw['outputs'][0].update(rental_id='buyer-1',rented_until=5000)
        await self.rt.apply(RouterSnapshot.from_dict(raw))
        restored=RouterRuntime(self.state,wall_clock=lambda:100)
        self.assertEqual(restored.snapshot.outputs[0].rental_id,'buyer-1')
        raw['revision']=3;raw['outputs'][0]['rented_until']=5100
        with self.assertRaises(ValueError):await self.rt.apply(RouterSnapshot.from_dict(raw))
        raw['outputs'][0].update(rented_until=5000,target_gh='2.3')
        with self.assertRaises(ValueError):await self.rt.apply(RouterSnapshot.from_dict(raw))
        raw['outputs'][0].update(target_gh='2.2',rental_id='buyer-2',rented_until=9000)
        with self.assertRaises(ValueError):await self.rt.apply(RouterSnapshot.from_dict(raw))
        self.rt.wall_clock=lambda:6000
        await self.rt.apply(RouterSnapshot.from_dict(raw))

if __name__=='__main__': unittest.main()
