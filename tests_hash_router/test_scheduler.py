import unittest
from collections import Counter
from decimal import Decimal as D
from dataclasses import replace
from hash_router.models import Endpoint,SourceSpec,OutputSpec,RouterSnapshot
from hash_router.scheduler import Scheduler

def snapshot(count=7):
    endpoint=Endpoint('pool.test',3333,'u','p')
    source=SourceSpec('s','a','1','r',D('17'),5000,'u','p')
    outputs=tuple(OutputSpec(f'o{i}',str(i),D('2.2'),endpoint) for i in range(count))
    return RouterSnapshot(1,1,(source,),outputs,endpoint)

class SchedulerTests(unittest.TestCase):
    def ready(self,count=7):
        sched=Scheduler(snapshot(count),clock=lambda:100,wall_clock=lambda:100)
        sched.set_connection_capacity('c','s',D('17'))
        for output in sched.snapshot.outputs: sched.set_ready(output.output_id,True)
        sched.set_ready('remainder',True)
        return sched
    def test_17_split_170_quanta(self):
        sched=self.ready();counts=Counter()
        for _ in range(170):
            out=sched.choose('c');counts[out]+=1;sched.record_elapsed('c',out,D('1'))
        self.assertEqual({**{f'o{i}':22 for i in range(7)},'remainder':16},dict(counts))
    def test_jitter_accounts_actual_elapsed(self):
        sched=self.ready();out=sched.choose('c');sched.record_elapsed('c',out,D('3'))
        self.assertEqual(D('51'),sum(sched.allocated_work.values()))
    def test_multiple_unequal_connections(self):
        sched=self.ready();sched.set_connection_capacity('other','s',D('17'))
        self.assertEqual(D('17'),sum(sched.connection_capacities.values()))
        sched.set_connection_capacity('c','s',D('0'));sched.set_connection_capacity('other','s',D('0'))
        self.assertEqual({'c':D('8.5'),'other':D('8.5')},sched.connection_capacities)
        sched.set_connection_capacity('c','s',D('5'))
        self.assertEqual({'c':D('5'),'other':D('12')},sched.connection_capacities)
    def test_mrr_idle_full_and_nicehash_rejected(self):
        sched=self.ready()
        self.assertEqual(D('2.2'),sched.weights['o0'])
        raw=sched.snapshot.to_dict(True);raw['sources'][0]['source_mode']='nicehash'
        with self.assertRaises(ValueError): RouterSnapshot.from_dict(raw)
    def test_expiry_and_disconnected_destination(self):
        sched=self.ready();sched.set_ready('o0',False)
        self.assertEqual(D('3.8'),sched.weights['remainder'])
        self.assertNotIn('o0',sched.weights)
        sched.wall_clock=lambda:6000
        self.assertIsNone(sched.choose('c'))
        self.assertEqual({},sched.connection_capacities)
    def test_capacity_change_rebases_history_and_pending_reservations(self):
        sched=self.ready()
        for _ in range(17000):
            out=sched.choose('c');sched.record_elapsed('c',out,D(1))
        # Keep a reservation outstanding across the effective-weight change.
        before=sched.choose('c')
        sched.set_connection_capacity('c','s',D(16))
        sched.record_elapsed('c',before,D(1))
        counts=Counter()
        for _ in range(1600):
            out=sched.choose('c');counts[out]+=1;sched.record_elapsed('c',out,D(1))
        self.assertGreaterEqual(counts['remainder'],55)
        self.assertLessEqual(counts['remainder'],65)
        sched.set_ready('o0',False)
        counts=Counter()
        for _ in range(1600):
            out=sched.choose('c');counts[out]+=1;sched.record_elapsed('c',out,D(1))
        self.assertGreaterEqual(counts['remainder'],270)

if __name__=='__main__': unittest.main()
