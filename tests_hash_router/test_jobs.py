import unittest
from decimal import Decimal as D
from hash_router.jobs import JobBook,JobError,IncompatibleExtranonce

def notify(job='1',clean=False):
    return [job,'00'*32,'0102','0304',[],'00000001','1d00ffff','12345678',clean]

class JobTests(unittest.TestCase):
    def publish(self,book,output='a',generation='g',job='1',clean=False,diff=D('2')):
        return book.publish('conn',output,generation,notify(job,clean),diff,'bb',4,'aa',2)
    def test_coinbase_preserved_across_extranonce_translation(self):
        book=JobBook();n=self.publish(book)
        route=book.route_submit('conn',['input',n[0],'1122',n[7],'aabbccdd'])
        original='0102'+'bb'+route.request_params[2]+'0304'
        downstream=n[2]+'aa'+'1122'+n[3]
        self.assertEqual(bytes.fromhex(original),bytes.fromhex(downstream))
        self.assertEqual('1',route.request_params[1])
        self.assertEqual(8,len(route.request_params[2]))
    def test_job_collision_and_late_submit(self):
        book=JobBook();a=self.publish(book,'a');b=self.publish(book,'b')
        self.assertNotEqual(a[0],b[0])
        route=book.route_submit('conn',['input',a[0],'1122',a[7],'aabbccdd'])
        self.assertEqual(('a','g'),(route.output_id,route.generation))
        book.invalidate('a','g');self.publish(book,'a','newg')
        with self.assertRaises(JobError): book.route_submit('conn',['input',a[0],'1122',a[7],'aabbccdd'])
        with self.assertRaises(JobError): book.route_submit('other',['input',b[0],'1122',b[7],'aabbccdd'])
    def test_difficulty_snapshot(self):
        book=JobBook();a=self.publish(book,diff=D('2'));self.publish(book,diff=D('4'))
        self.assertEqual(D('2'),book.route_submit('conn',['u',a[0],'1122',a[7],'aabbccdd']).job.difficulty)
    def test_clean_jobs_scope(self):
        book=JobBook();a=self.publish(book,'a');b=self.publish(book,'b')
        self.publish(book,'a',job='2',clean=True)
        with self.assertRaises(JobError): book.route_submit('conn',['u',a[0],'1122',a[7],'aabbccdd'])
        self.assertEqual('b',book.route_submit('conn',['u',b[0],'1122',b[7],'aabbccdd']).output_id)
    def test_incompatible_extranonce(self):
        book=JobBook()
        with self.assertRaises(IncompatibleExtranonce):
            book.publish('c','a','g',notify(),D('1'),'bb',2,'aa',2)
    def test_connection_prefixes_and_nonce_validation(self):
        book=JobBook();a=self.publish(book)
        b=book.publish('second','a','g',notify(),D('1'),'bb',4,'aa',2)
        self.assertNotEqual(a[2],b[2])
        for extra,time,nonce in [('11',a[7],'aabbccdd'),('1122','invalid','aabbccdd'),('1122',a[7],'ff')]:
            with self.assertRaises(JobError): book.route_submit('conn',['u',a[0],extra,time,nonce])
    def test_reissued_upstream_job_gets_fresh_coinbase(self):
        book=JobBook();a=self.publish(book);b=self.publish(book)
        self.assertNotEqual(a[2],b[2])
        ra=book.route_submit('conn',['u',a[0],'1122',a[7],'aabbccdd'])
        rb=book.route_submit('conn',['u',b[0],'1122',b[7],'aabbccdd'])
        self.assertNotEqual(ra.request_params[2],rb.request_params[2])

if __name__=='__main__': unittest.main()
