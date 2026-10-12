import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('probe', Path(__file__).resolve().parents[1] / 'probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class Fake:
    def __init__(self, bad=None):
        self.rows, self.calls, self.bad = [], [], bad

    def get(self, name, path, body=None):
        self.calls.append((name, path, body))
        if name == 'version': return {'data': {'version': 'Nimbus/test'}}
        if name == 'readiness':
            return {'data': dict(is_syncing=self.bad == 'syncing', is_optimistic=False, el_offline=False)}
        if name == 'chain_timing': return {'data': {'SLOTS_PER_EPOCH': '32'}}
        if name in ('canonical_head','finalized_head','head_recheck'):
            slot = 300 if name != 'finalized_head' else 256
            if self.bad == 'finality' and name == 'finalized_head': slot = 32
            return {'execution_optimistic': self.bad == 'optimistic', 'data': {
                'canonical': True, 'root': '0x' + ('b' if self.bad == 'reorg' and name == 'head_recheck' else 'a') * 64,
                'header': {'message': {'slot': str(slot)}}}}
        return None  # Missing optional APIs must not be labeled supported.


class ProbeTests(unittest.TestCase):
    def test_basic_probe_is_bounded_and_has_no_rewards(self):
        c = Fake(); r = p.probe(c)
        self.assertTrue(r['ready'])
        self.assertEqual(len(c.calls), 9)
        self.assertNotIn('attestation_rewards', [x[0] for x in c.calls])
        self.assertNotIn('attester_duties', [x[0] for x in c.calls])
        self.assertTrue(all(body is None for _, _, body in c.calls))

    def test_unready_and_reorg_never_ready(self):
        for bad in ('syncing', 'optimistic', 'finality', 'reorg'):
            with self.subTest(bad=bad):
                c=Fake(bad);self.assertFalse(p.probe(c)['ready'])
                if bad == 'syncing': self.assertEqual(len(c.calls), 2)

    def test_rewards_filter_one_index_and_no_identity_in_report(self):
        c=Fake();r=p.probe(c, 12345, True)
        rewards=[v for v in c.calls if v[0]=='attestation_rewards']
        self.assertEqual(rewards, [('attestation_rewards','/eth/v1/beacon/rewards/attestations/6',['12345'])])
        self.assertNotIn('12345',json.dumps(r))
        with self.assertRaises(ValueError):p.probe(Fake(),None,True)

    def test_reject_urls_with_secrets_or_non_http_schemes(self):
        for url in ('file:///etc/passwd','http://u:p@localhost','http://localhost/?token=x','http://localhost/#x'):
            with self.subTest(url=url),self.assertRaises(Exception):p.endpoint(url)
        self.assertEqual(p.endpoint('http://127.0.0.1:5552/'),'http://127.0.0.1:5552')

    def test_size_limit_and_invalid_json_do_not_escape_in_report(self):
        for body in (b'x'*(p.LIMIT+1),b'private backend error',b'[]'):
            c=p.Client('http://127.0.0.1:5552',pause=0)
            class Response(io.BytesIO): status=200
            with patch.object(c.opener,'open',return_value=Response(body)):
                self.assertIsNone(c.get('example','/example'))
            self.assertEqual(c.rows[0]['status'],'unavailable')
            self.assertNotIn('private backend error',json.dumps(c.rows))
            self.assertNotIn('127.0.0.1',json.dumps(c.rows))

    def test_http_error_not_recorded_as_absent_duty(self):
        c=p.Client('http://127.0.0.1:5552',pause=0)
        error=urllib.error.HTTPError('http://private',404,'private error',{},None)
        with patch.object(c.opener,'open',side_effect=error): self.assertIsNone(c.get('example','/example'))
        self.assertEqual(c.rows[0]['http_status'],404)
        self.assertEqual(c.rows[0]['status'],'unavailable')
        self.assertNotIn('private',json.dumps(c.rows))

    def test_redirects_are_not_followed(self):
        self.assertIsNone(p.NoRedirect().redirect_request(None,None,302,'',{},'http://other-node'))


if __name__ == '__main__': unittest.main()
