import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from hash_router.jobs import JobBook, JobError, IncompatibleExtranonce
from hash_router.runtime import RouterRuntime, InputConnection
from hash_router.multi import MemoryState


class SubmitDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.book=JobBook(clock=lambda:10)
        self.notify=['private-job','00'*32,'0102','0304',[], '00000001','1d00ffff','12345678',False]
        self.job=self.book.publish('c','out','g',self.notify,Decimal(1),'bb',4,'00',2)
        self.params=['private-worker',self.job[0],'1122','12345678','12345678']

    def assert_reason(self,reason,params,connection='c',mask=None):
        with self.assertRaises(JobError) as caught:
            self.book.route_submit(connection,params,mask)
        self.assertEqual(reason,caught.exception.reason)

    def test_distinct_structure_job_and_field_errors(self):
        self.assert_reason('submit_not_list',{})
        self.assert_reason('submit_parameter_count',self.params[:4])
        self.assert_reason('submit_parameter_type',[0]+self.params[1:])
        self.assert_reason('job_unknown',[self.params[0],'missing']+self.params[2:])
        self.assert_reason('job_wrong_connection',self.params,connection='other')
        for index,name in ((2,'extranonce2'),(3,'ntime'),(4,'nonce')):
            values=list(self.params);values[index]='zz'
            self.assert_reason(name+'_format',values)
        values=list(self.params);values[3]='00000001'
        self.assert_reason('ntime_before_job',values)
        self.assert_reason('version_bits_format',self.params+['bad'],mask=0x2000)
        self.assert_reason('version_bits_outside_mask',self.params+['00004000'],mask=0x2000)
        self.book.invalidate('out','g',reason='output_disconnected')
        self.assert_reason('job_invalidated',self.params)
        diagnostic=self.book.submit_diagnostic('c',self.params)
        self.assertEqual('output_disconnected',diagnostic['invalidation_reason'])
        self.assertNotIn('private',json.dumps(diagnostic))
        self.assertEqual(4,diagnostic['expected_extranonce2_chars'])

    def test_capacity_error_is_distinct_from_size(self):
        for size,reason in ((2,'extranonce_space_insufficient'),(3,'extranonce_capacity_exhausted')):
            book=JobBook()
            if size==3: book.publish('c','out','g',self.notify,Decimal(1),'bb',size,'00',2)
            with self.assertRaises(IncompatibleExtranonce) as caught:
                book.publish('c','out','g',self.notify,Decimal(1),'bb',size,'00',2)
            self.assertEqual(reason,caught.exception.reason)


class RuntimeSubmitDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasons_are_visible_bounded_and_payload_hidden(self):
        rt=RouterRuntime(MemoryState(),algorithm='sha256ab');rt._reply=AsyncMock()
        context=InputConnection('c',None,None)
        for _ in range(10):
            await rt._submit(context,1,['secret-worker','secret-job'])
            await rt._submit(context,2,['secret-worker','secret-job','1122','12345678','12345678'])
        rows=[r for r in rt.event_log.read()['entries'] if r['event']=='input_submit_rejected']
        self.assertEqual(6,len(rows))
        self.assertEqual({'submit_parameter_count','job_unknown'},{r['reason'] for r in rows})
        self.assertTrue(all('parameter_count' in r for r in rows))
        self.assertNotIn('secret-',json.dumps(rows))
        self.assertEqual(20,context.diagnostics['local_rejected'])
        self.assertEqual(0,context.diagnostics['forward_attempts'])

    async def test_valid_submit_still_reaches_output(self):
        rt=RouterRuntime(MemoryState(),algorithm='sha256ab');rt._reply=AsyncMock()
        context=InputConnection('c',None,None)
        notify=['job','00'*32,'0102','0304',[],'00000001','1d00ffff','12345678',False]
        job=rt.jobs.publish('c','out','g',notify,Decimal(1),'bb',4,'00',2)
        session=SimpleNamespace(closed=False,generation='g',version_mask=None,
                                endpoint=SimpleNamespace(username='upstream'),request=AsyncMock(return_value=True))
        rt.outputs['out']=SimpleNamespace(session=session)
        await rt._submit(context,1,['worker',job[0],'1122',job[7],'12345678'])
        session.request.assert_awaited_once()
        self.assertEqual(1,context.diagnostics['accepted'])
        self.assertEqual(1,context.diagnostics['forward_attempts'])
        self.assertEqual(0,context.diagnostics['local_rejected'])
