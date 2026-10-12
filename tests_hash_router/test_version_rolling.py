import asyncio
import unittest
from decimal import Decimal as D
from hash_router.protocol import StratumSession,read_message,write_message,version_mask,ProtocolError
from hash_router.runtime import RouterRuntime
from hash_router.multi import MemoryState
from hash_router.models import RouterSnapshot,SourceSpec
from hash_router.jobs import JobBook,JobError
from tests_hash_router.helpers import fake_server
from tests_hash_router.test_jobs import notify


class VersionRollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_preserves_version_and_legacy_submits(self):
        shares=[];methods=[];writers=[]
        async def pool(reader,writer):
            writers.append(writer)
            while True:
                req=await read_message(reader);method=req['method'];methods.append(method)
                if method=='mining.configure':
                    result={'version-rolling':True,'version-rolling.mask':'1fffe000'}
                elif method=='mining.subscribe': result=[[],'aa',4]
                elif method=='mining.authorize': result=True
                elif method=='mining.submit':
                    shares.append(req['params']);result=len(req['params'])==6
                else: result=False
                await write_message(writer,{'id':req['id'],'result':result,'error':None})
                if method=='mining.authorize':
                    job=notify(clean=True);job[5]='20006000'
                    await write_message(writer,{'id':None,'method':'mining.notify','params':job})
        async with fake_server(pool) as endpoint:
            source=SourceSpec('s','a','1','r',D(97),9999999999,'input','pass')
            rt=RouterRuntime(MemoryState())
            await rt.apply(RouterSnapshot(1,1,(source,),(),endpoint,120,.05,'sha256ab'))
            await rt.start('127.0.0.1',0)
            try:
                async with asyncio.timeout(3):
                    while rt.outputs['remainder'].state!='ready': await asyncio.sleep(.01)
                for rolling in (True,False):
                    reader,writer=await asyncio.open_connection('127.0.0.1',rt.port)
                    try:
                        if rolling:
                            await write_message(writer,{'id':0,'method':'mining.configure','params':[
                                ['version-rolling'],{'version-rolling.mask':'00002000','version-rolling.min-bit-count':1}]})
                            config=await read_message(reader)
                            self.assertEqual({'version-rolling':True,'version-rolling.mask':'00002000'},config['result'])
                        await write_message(writer,{'id':1,'method':'mining.subscribe','params':[]})
                        self.assertIsInstance((await read_message(reader))['result'],list)
                        await write_message(writer,{'id':2,'method':'mining.authorize','params':['input','pass']})
                        self.assertTrue((await read_message(reader))['result'])
                        while True:
                            msg=await read_message(reader,timeout=3)
                            if msg.get('method')=='mining.notify': break
                        job=msg['params']
                        rt._summary_at=rt.clock()-31;rt.status()
                        flow=[r for r in rt.event_log.read()['entries'] if r['event']=='input_flow_summary'][-1]
                        self.assertGreater(flow['jobs_sent'],0)
                        self.assertEqual(0,flow['submits_received'])
                        self.assertNotIn('last_submit_age_ms',flow)
                        self.assertGreaterEqual(flow['last_job_age_ms'],0)
                        params=['input',job[0],'0011',job[7],'aabbccdd']+(['00000000'] if rolling else [])
                        await write_message(writer,{'id':3,'method':'mining.submit','params':params})
                        while True:
                            msg=await read_message(reader,timeout=3)
                            if msg.get('id')==3: break
                        self.assertTrue(msg['result'])
                        self.assertEqual('00004000' if rolling else '00006000',shares[-1][5])
                        self.assertEqual('worker',shares[-1][0])
                        if rolling:
                            await write_message(writers[0],{'id':None,'method':'mining.set_version_mask','params':['00004000']})
                            async with asyncio.timeout(3):
                                while True:
                                    changed=await read_message(reader)
                                    if changed.get('method')=='mining.set_version_mask': break
                            self.assertEqual(['00000000'],changed['params'])
                            while True:
                                changed=await read_message(reader)
                                if changed.get('method')=='mining.notify': break
                            changed_job=changed['params']
                            await write_message(writer,{'id':4,'method':'mining.submit','params':[
                                'input',changed_job[0],'0022',changed_job[7],'aabbccdd','00000000']})
                            while True:
                                changed=await read_message(reader)
                                if changed.get('id')==4: break
                            self.assertTrue(changed['result'])
                            self.assertEqual('00004000',shares[-1][5])
                            # Restore the wider upstream mask for the legacy client below.
                            await write_message(writers[0],{'id':None,'method':'mining.set_version_mask','params':['1fffe000']})
                            async with asyncio.timeout(3):
                                while rt.outputs['remainder'].session.version_mask!=0x1fffe000: await asyncio.sleep(.01)
                    finally:
                        writer.close();await writer.wait_closed()
            finally: await rt.stop()
        self.assertEqual(['mining.configure','mining.subscribe','mining.authorize'],methods[:3])

    async def test_legacy_pool_closes_configure_reconnects_plain(self):
        methods=[]
        async def legacy(reader,writer):
            while True:
                req=await read_message(reader);methods.append(req['method'])
                if req['method']=='mining.configure': return
                result=[[],'aa',4] if req['method']=='mining.subscribe' else True
                await write_message(writer,{'id':req['id'],'result':result,'error':None})
        async with fake_server(legacy) as endpoint:
            session=StratumSession(endpoint,version_rolling=True)
            try:
                await session.connect()
                self.assertIsNone(session.version_mask)
                self.assertEqual(['mining.configure','mining.subscribe','mining.authorize'],methods)
            finally: await session.close()

    async def test_masks_and_submit_validation(self):
        for bad in ('fffffffff',' ff00000','gg000000',True,123):
            with self.assertRaises(ProtocolError): version_mask(bad)
        book=JobBook();job=book.publish('c','out','g',notify(),D(1),'aa',4,'00',2)
        base=['u',job[0],'0011',job[7],'aabbccdd']
        self.assertEqual('00002000',book.route_submit('c',base+['00002000'],0x2000).request_params[5])
        for params,mask in ((base,0x2000),(base+['00004000'],0x2000),(base+['00002000'],None)):
            with self.assertRaises(JobError): book.route_submit('c',params,mask)

    async def test_invalid_upstream_mask_never_authorizes(self):
        methods=[]
        async def invalid(reader,writer):
            req=await read_message(reader);methods.append(req['method'])
            await write_message(writer,{'id':req['id'],'result':{
                'version-rolling':True,'version-rolling.mask':'not-a-mask'},'error':None})
            await reader.read()
        async with fake_server(invalid) as endpoint:
            session=StratumSession(endpoint,version_rolling=True)
            with self.assertRaises(ProtocolError): await session.connect()
            self.assertTrue(session.closed)
            self.assertEqual(['mining.configure'],methods)


if __name__=='__main__': unittest.main()
