import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal as D
from hash_router.state import AtomicState
from hash_router.models import SourceSpec, OutputSpec
from hash_router.allocation import available_sale_hours, allocate_shortage, resize_free_outputs

class StateTests(unittest.TestCase):
    def test_atomic_write_failure_keeps_previous_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json'; state=AtomicState(path)
            state.save({'revision':1})
            with patch('hash_router.state.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError): state.save({'revision':2})
            self.assertEqual({'revision':1}, state.load())
            path.write_text('corrupt',encoding='utf8')
            with self.assertRaises(ValueError): state.load()
            self.assertEqual('corrupt',path.read_text())
    def test_sale_horizon(self):
        sources=[SourceSpec('a','a','1','r1',D('5'),7300,'u1','p'),
                 SourceSpec('b','a','2','r2',D('12'),10900,'u2','p')]
        self.assertEqual(D('1.9'),available_sale_hours(sources,D('15.4'),100,360))
        self.assertEqual(D('2.9'),available_sale_hours(sources,D('10'),100,360))
        self.assertEqual(D('0'),available_sale_hours(sources,D('18'),100,360))
        self.assertEqual(D('0'),available_sale_hours(sources,D('17'),8000,360))
    def test_sold_output_template_change(self):
        sold=OutputSpec('sold','1',D('2.2'),None,5000)
        free=OutputSpec('free','2',D('2.2'),None)
        result=resize_free_outputs([sold,free],D('3'),100)
        self.assertEqual(D('2.2'),result[0].target_gh)
        self.assertEqual(5000,result[0].rented_until)
        self.assertEqual(D('3'),result[1].target_gh)
    def test_shortage_priority(self):
        outputs=[OutputSpec('s1','1',D('2.2'),None,5000),
                 OutputSpec('f1','2',D('2.2'),None),OutputSpec('f2','3',D('2.2'),None)]
        self.assertEqual({'s1':D('2.2'),'f1':D('1.4'),'f2':D('1.4')},allocate_shortage(D('5'),outputs,now=100))
        outputs[1]=OutputSpec('s2','2',D('2.2'),None,5000)
        self.assertEqual({'s1':D('1.5'),'s2':D('1.5'),'f2':D('0')},allocate_shortage(D('3'),outputs,now=100))

if __name__=='__main__': unittest.main()
