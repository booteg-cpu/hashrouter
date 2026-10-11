import unittest
from decimal import Decimal as D
from hash_router.metrics import Metrics,DifficultyConvention
from hash_router.jobs import JobBook
from tests_hash_router.test_jobs import notify
from tests_hash_router.test_scheduler import snapshot

class MetricsTests(unittest.TestCase):
    def route(self,nonce='aabbccdd',difficulty=D('2')):
        book=JobBook();job=book.publish('c','o0','g',notify(),difficulty,'bb',4,'aa',2)
        return book.route_submit('c',['input',job[0],'1122',job[7],nonce])
    def test_weighted_accepted_hashrate(self):
        clock=[0];metrics=Metrics(lambda:clock[0],DifficultyConvention(D('1000000000'),True,'test-only'))
        metrics.record_submit(self.route(),True)
        metrics.record_submit(self.route('aabbccde',D('50')),False)
        clock[0]=10
        row=metrics.status(snapshot()).to_dict()['outputs'][0]
        self.assertEqual('0.2',row['accepted_gh']['5m'])
        self.assertEqual((1,1),(row['accepted'],row['rejected']))
    def test_unverified_convention_not_presented_as_real_hashrate(self):
        metrics=Metrics(lambda:10,DifficultyConvention(D('65536'),False,'unverified'))
        metrics.record_submit(self.route(),True)
        status=metrics.status(snapshot()).to_dict()
        self.assertIsNone(status['outputs'][0]['accepted_gh']['5m'])
        self.assertEqual('required',status['calibration'])
    def test_windows_and_reordered_responses(self):
        clock=[0];metrics=Metrics(lambda:clock[0],DifficultyConvention(D('1000000000'),True,'test-only'))
        route=self.route();metrics.record_submit(route,True);metrics.record_submit(route,True)
        clock[0]=400
        row=metrics.status(snapshot()).to_dict()['outputs'][0]
        self.assertEqual(1,row['accepted'])
        self.assertEqual('0',row['accepted_gh']['5m'])
        self.assertEqual('0.005',row['accepted_gh']['15m'])
    def test_secret_redaction(self):
        raw=snapshot().to_dict(True);raw['sources'][0]['password']='SECRET';raw['remainder']['password']='SECRET'
        from hash_router.models import RouterSnapshot
        data=Metrics().status(RouterSnapshot.from_dict(raw)).to_dict()
        self.assertNotIn('SECRET',str(data))
        self.assertTrue(data['experimental'])

if __name__=='__main__': unittest.main()
