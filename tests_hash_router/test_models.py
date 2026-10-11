import unittest
from decimal import Decimal as D
from hash_router.models import PricePolicy, RouterSnapshot
from hash_router.allocation import split_capacity, price_payload, hash_to_gh

class ModelTests(unittest.TestCase):
    def test_split_exact(self):
        self.assertEqual((7, D('1.6')), split_capacity(D('17'), D('2.2'), 100))
        self.assertEqual((1, D('0')), split_capacity(D('2.2'), D('2.2'), 100))
        self.assertEqual((0, D('2.19')), split_capacity(D('2.19'), D('2.2'), 100))
        self.assertEqual((2, D('12.6')), split_capacity(D('17'), D('2.2'), 2))
    def test_units_and_validation(self):
        self.assertEqual(D('17'), hash_to_gh('17000', 'mh'))
        for value in ('NaN', 'Infinity', '-1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                split_capacity(D(value), D('2.2'), 100)
        with self.assertRaises(ValueError):
            split_capacity(D('17'), D('0'), 100)
    def test_price_modes(self):
        manual = price_payload(PricePolicy('manual', D('0.0001'), D('10')))
        self.assertFalse(manual['btc']['autoprice'])
        self.assertEqual('0.0001', manual['btc']['price'])
        for pct in ('10', '-5'):
            p = price_payload(PricePolicy('recommended', None, D(pct)))
            self.assertTrue(p['btc']['autoprice'])
            self.assertEqual(D(pct), D(p['btc']['modifier']))
        for policy in (PricePolicy('manual', D('0'), D('0')), PricePolicy('recommended', None, D('-100'))):
            with self.assertRaises(ValueError): price_payload(policy)
    def test_secrets_and_snapshot_validation(self):
        raw = {'schema_version': 1, 'revision': 1, 'sources': [
            {'source_id': 'a:rent', 'account_id': 'a', 'rig_id': '1', 'rental_id': '2',
             'advertised_gh': '17', 'expires_at': 5000, 'username': 'input', 'password': 'SECRET'}],
             'outputs': [], 'remainder': {'host': 'pool.test', 'port': 3333, 'username': 'wallet', 'password': 'POOL_SECRET'},
             'reserve_seconds': 120, 'quantum_seconds': 1}
        snap = RouterSnapshot.from_dict(raw)
        self.assertNotIn('SECRET', repr(snap))
        self.assertNotIn('SECRET', str(snap.to_dict()))
        self.assertEqual('SECRET', snap.to_dict(include_secrets=True)['sources'][0]['password'])
        raw['sources'].append(dict(raw['sources'][0]))
        with self.assertRaises(ValueError): RouterSnapshot.from_dict(raw)

if __name__ == '__main__': unittest.main()
