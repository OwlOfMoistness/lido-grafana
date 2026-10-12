"""Activation timing, queue semantics, and consistent beacon snapshots."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

SPEC = importlib.util.spec_from_file_location('activation', Path(__file__).resolve().parents[1] / 'check_activation.py')
activation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activation)


def key(number):
    return '0x' + f'{number:096x}'


def validator(number, status='pending_queued', epoch=activation.FAR_FUTURE):
    return {'index': str(number), 'balance': '32000000001', 'status': status,
            'validator': {'pubkey': key(number), 'activation_epoch': str(epoch),
                          'activation_eligibility_epoch': '1'}}


class Beacon:
    def __init__(self, records=(), deposits=()):
        self.records = {r['validator']['pubkey']: r for r in records}
        self.deposits = [{'pubkey': key(n)} for n in deposits]
        self.calls = []

    def __call__(self, url, body=None):
        self.calls.append((url, body))
        if url.endswith('/syncing'):
            return {'data': dict(is_syncing=False, is_optimistic=False, el_offline=False)}
        if url.endswith('/spec'):
            return {'data': {'SLOTS_PER_EPOCH': '32', 'SECONDS_PER_SLOT': '12'}}
        if url.endswith('/genesis'):
            return {'data': {'genesis_time': '1600000000'}}
        if '/headers/' in url:
            return {'execution_optimistic': False, 'data': {'canonical': True, 'root': '0x' + 'a'*64,
                    'header': {'message': {'slot': '64'}}}}
        if url.endswith('/pending_deposits'):
            return {'execution_optimistic': False, 'data': self.deposits}
        if url.endswith('/validators'):
            return {'execution_optimistic': False,
                    'data': [self.records[k] for k in body['ids'] if k in self.records]}
        raise AssertionError(url)


class Activation(unittest.TestCase):
    def test_finite_epoch_and_far_future_sentinel(self):
        fake = Beacon([validator(1, epoch=3), validator(2), validator(3, 'active_ongoing', 1)])
        rows, meta = activation.report('http://beacon', [key(n) for n in (1, 2, 3, 4)], fetch=fake)
        self.assertEqual(rows[0]['activation_utc'], '2020-09-13T12:45:52+00:00')
        self.assertEqual(rows[0]['balance_eth'], '32.000000001')
        self.assertEqual(rows[1]['activation_epoch'], 'not_assigned')
        self.assertEqual(rows[1]['activation_utc'], '')
        self.assertEqual(rows[2]['status'], 'active_ongoing')
        self.assertEqual(rows[3]['status'], 'not_in_registry')
        self.assertEqual(rows[3]['deposit_queue'], 'not_checked')
        self.assertEqual(meta['epoch'], 2)
        self.assertFalse(any(url.endswith('/pending_deposits') for url, _ in fake.calls))

    def test_queue_counts_all_entries_and_preserves_multiple_deposits(self):
        fake = Beacon([validator(2, 'active_ongoing', 1)], [99, 1, 2, 1])
        rows, meta = activation.report('http://beacon', [key(1), key(2), key(3)], True, fake)
        self.assertEqual(rows[0]['status'], 'pending_deposit')
        self.assertEqual(rows[0]['deposit_positions'], '2;4')
        self.assertEqual(rows[1]['status'], 'active_ongoing')
        self.assertEqual(rows[1]['deposit_positions'], '3')
        self.assertEqual(rows[2]['deposit_queue'], 'absent')
        self.assertEqual(rows[2]['status'], 'not_in_registry')
        self.assertEqual(meta['queue_entries'], 4)

    def test_failed_queue_is_unavailable_not_absent(self):
        for mode in ('unsupported', 'oversize', 'optimistic'):
            fake = Beacon([validator(1)])
            def fetch(url, body=None):
                if url.endswith('/pending_deposits'):
                    if mode == 'unsupported':
                        raise HTTPError(url, 404, 'Missing', {}, None)
                    if mode == 'oversize':
                        raise ValueError('Response exceeds limit')
                    return {'execution_optimistic': True, 'data': []}
                return fake(url, body)
            with self.subTest(mode=mode):
                rows, meta = activation.report('http://beacon', [key(1)], True, fetch)
                self.assertEqual(rows[0]['deposit_queue'], 'unavailable')
                self.assertEqual(rows[0]['status'], 'pending_queued')
                self.assertIsNotNone(meta['warning'])

    def test_reorg_unready_and_unexpected_records_are_rejected(self):
        for mode in ('reorg', 'syncing', 'optimistic', 'unexpected', 'duplicate'):
            fake = Beacon([validator(1)])
            def fetch(url, body=None):
                response = fake(url, body)
                if mode == 'reorg' and url.endswith('/headers/64'):
                    response['data']['root'] = '0x' + 'b'*64
                if mode == 'syncing' and url.endswith('/syncing'):
                    response['data']['is_syncing'] = True
                if url.endswith('/validators'):
                    if mode == 'optimistic': response['execution_optimistic'] = True
                    if mode == 'unexpected': response['data'] = [validator(9)]
                    if mode == 'duplicate': response['data'] *= 2
                return response
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                activation.report('http://beacon', [key(1)], fetch=fetch)

    def test_batches_and_queue_use_same_slot(self):
        fake = Beacon()
        activation.report('http://beacon', [key(n) for n in range(300)], True, fake)
        batches = [(url, body) for url, body in fake.calls if body is not None]
        self.assertEqual([len(body['ids']) for _, body in batches], [128, 128, 44])
        self.assertTrue(all('/states/64/validators' in url for url, _ in batches))
        self.assertIn(('http://beacon/eth/v1/beacon/states/64/pending_deposits', None), fake.calls)
        self.assertTrue(fake.calls[-1][0].endswith('/headers/64'))

    def test_millisecond_slot_constant(self):
        fake = Beacon([validator(1, epoch=3)])
        def fetch(url, body=None):
            if url.endswith('/spec'):
                return {'data': {'SLOTS_PER_EPOCH': '32', 'SLOT_DURATION_MS': '12000'}}
            return fake(url, body)
        rows, _ = activation.report('http://beacon', [key(1)], fetch=fetch)
        self.assertEqual(rows[0]['activation_utc'], '2020-09-13T12:45:52+00:00')

    def test_key_input_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'keys.csv'
            path.write_text('\ufeff"' + key(1) + '"\n\n' + key(2) + '\n')
            self.assertEqual(activation.read_keys([path]), [key(1), key(2)])
            for content in ('', 'pubkey\n' + key(1), 'not-a-public-key', key(1) + '\n' + key(1)):
                path.write_text(content)
                with self.subTest(content=content[:12]), self.assertRaises(ValueError):
                    activation.read_keys([path])


if __name__ == '__main__':
    unittest.main()
