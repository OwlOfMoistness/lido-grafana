"""Proposal windows must never turn failed or incomplete schedules into zero."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('proposal_inventory', ROOT / 'deployment/inventory.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def key(n):
    return '0x' + f'{n:096x}'


class Beacon:
    def __init__(self):
        self.calls = []
        self.head_slot = 100

    def __call__(self, url, body=None):
        self.calls.append(url)
        if url.endswith('/genesis'):
            return {'data': {'genesis_time': '1000'}}
        if url.endswith('/spec'):
            return {'data': {'SECONDS_PER_SLOT': '12', 'SLOTS_PER_EPOCH': '32'}}
        if url.endswith('/syncing'):
            return {'data': dict(is_syncing=False, is_optimistic=False, el_offline=False)}
        if '/headers/' in url:
            return {'execution_optimistic': False, 'data': {'canonical': True, 'root': '0x'+'a'*64,
                    'header': {'message': {'slot': str(self.head_slot)}}}}
        epoch = int(url.rsplit('/', 1)[-1])
        return {'execution_optimistic': False, 'dependent_root': '0x'+'b'*64,
                'data': [{'slot': str(slot), 'validator_index': str(slot),
                          'pubkey': key(1 if slot in (99,100,105,130) else 2)}
                         for slot in range(epoch*32, (epoch+1)*32)]}


class Proposals(unittest.TestCase):
    def snapshot(self, fetch=None, now=lambda: 2205):
        return inventory.proposal_snapshot('http://main', {'validator-1':[key(1)], 'validator-2':[]},
                                           fetch or Beacon(), now)

    def test_complete_window_includes_current_slot_and_expires_at_boundary(self):
        result = self.snapshot()
        text = inventory.proposal_metrics(['validator-1','validator-2'],result,2205).decode()
        self.assertIn('lido_proposals_upcoming{host="validator-1"} 3',text)
        self.assertIn('lido_proposals_upcoming{host="validator-2"} 0',text)
        self.assertNotIn(key(1),text)
        self.assertIn('lido_proposals_coverage_end_seconds{host="validator-1"} 2920',text)
        self.assertIn('lido_proposals_upcoming{host="validator-1"} 2',inventory.proposal_metrics(['validator-1'],result,2212).decode())
        # A refresh just before the epoch changes must expire at that boundary.
        result['timestamp']=2530
        result['boundary']=2536
        self.assertIn('lido_proposals_success{host="validator-1"} 0',inventory.proposal_metrics(['validator-1'],result,2536).decode())

    def test_millisecond_timing_constant(self):
        beacon=Beacon()
        def fetch(url,body=None):
            response=beacon(url,body)
            if url.endswith('/spec'):
                response['data'].pop('SECONDS_PER_SLOT')
                response['data']['SLOT_DURATION_MS']='12000'
            return response
        self.assertEqual(self.snapshot(fetch)['seconds'],12)

    def test_stale_failed_and_clock_reversal_are_unknown(self):
        for now in (2204,2295):
            text=inventory.proposal_metrics(['validator-1'],self.snapshot(),now).decode()
            self.assertNotIn('lido_proposals_upcoming{',text)
            self.assertIn('lido_proposals_success{host="validator-1"} 0',text)
        self.assertNotIn('lido_proposals_upcoming{',inventory.proposal_metrics(['validator-1']).decode())

    def test_reject_partial_duplicate_wrong_epoch_optimistic_bad_key_and_reorg(self):
        for failure in ('partial','empty_next','duplicate','wrong_epoch','optimistic','bad_key','bad_root','reorg','offline','stale_head'):
            beacon=Beacon()
            def fetch(url,body=None):
                result=beacon(url,body)
                if '/duties/' in url:
                    if failure=='partial': result['data'].pop()
                    if failure=='empty_next' and url.endswith('/4'): result['data']=[]
                    if failure=='duplicate': result['data'][-1]=copy.deepcopy(result['data'][0])
                    if failure=='wrong_epoch': result['data'][0]['slot']='1'
                    if failure=='optimistic': result['execution_optimistic']=True
                    if failure=='bad_key': result['data'][0]['pubkey']='0x123'
                    if failure=='bad_root': result['dependent_root']='0x123'
                if failure=='reorg' and url.endswith('/headers/100'): result['data']['root']='0x'+'c'*64
                if failure=='offline' and url.endswith('/syncing'): result['data']['el_offline']=True
                if failure=='stale_head' and url.endswith('/headers/head'): result['data']['header']['message']['slot']='90'
                return result
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                self.snapshot(fetch)

    def test_epoch_rollover_during_lookup_rejected(self):
        times=iter((2205,2536))
        with self.assertRaises(ValueError): self.snapshot(now=lambda:next(times))

    def test_fallback_restarts_complete_window_and_parameters_are_cached(self):
        beacon=Beacon();cache={}
        def fetch(url,body=None):
            if url.startswith('http://main') and '/duties/proposer/4' in url: raise OSError('next epoch unavailable')
            return beacon(url,body)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'validator-1.csv').write_text(key(1))
            for _ in range(2):
                result=inventory.collect_proposals(path,['validator-1'],['http://main','http://fallback'],fetch,lambda:2205,cache)
                self.assertEqual(result['fallback'],1)
            self.assertEqual(sum(url.endswith('/genesis') for url in beacon.calls),2)
            with self.assertRaises(ValueError):
                inventory.collect_proposals(path,['validator-1'],['http://main'],fetch,lambda:2205)

    def test_failed_poll_removes_previous_proposal_counts(self):
        config={'clients':[('validator-1','VC 1')], 'inventory_enabled':True,
                'inventory_port':19200,'beacon_apis':['http://beacon']}
        captures=[]; polls=[]; handlers=[]
        class Stop(Exception): pass
        class Response:
            path='/metrics'
            def send_response(self,*args): pass
            def send_header(self,*args): pass
            def end_headers(self): pass
            class Writer:
                def write(self,body): captures.append(body.decode())
            wfile=Writer()
        class Thread:
            def __init__(self,target,**kwargs): polls.append(target)
            def start(self): pass
        class Server:
            def __init__(self,address,handler): handlers.append(handler)
            def serve_forever(self): polls[1]()
        def sleep(_):
            handlers[0].do_GET(Response())
            if len(captures)==2: raise Stop()
        result=self.snapshot()
        with patch.object(inventory,'ThreadingHTTPServer',Server), patch.object(inventory.threading,'Thread',Thread), patch.object(inventory,'collect_proposals',side_effect=[result,ValueError('failed')]), patch.object(inventory.time,'time',return_value=2205), patch.object(inventory.time,'sleep',side_effect=sleep), self.assertRaises(Stop):
            inventory.serve(config)
        self.assertIn('lido_proposals_upcoming{host="validator-1"} 3',captures[0])
        self.assertNotIn('lido_proposals_upcoming{',captures[1])
        self.assertIn('lido_proposals_success{host="validator-1"} 0',captures[1])

    def test_dashboard_uses_instant_unknown_fallback_and_places_card_after_activity(self):
        board=json.loads((ROOT/'monitoring/dashboards/lido-validator-fleet.json').read_text())
        panels={p['id']:p for p in board['panels']}
        card=panels[45];table=panels[38]
        self.assertTrue(card['targets'][0]['instant'])
        self.assertIn('valid_until_seconds',card['targets'][0]['expr'])
        self.assertIn(' - 1)',card['targets'][0]['expr'])
        self.assertEqual(card['targets'][0]['expr'],table['targets'][-1]['expr'])
        self.assertEqual(panels[12]['gridPos']['x']+panels[12]['gridPos']['w'],card['gridPos']['x'])
        self.assertEqual(card['gridPos']['x']+card['gridPos']['w'],panels[14]['gridPos']['x'])


if __name__ == '__main__': unittest.main()
