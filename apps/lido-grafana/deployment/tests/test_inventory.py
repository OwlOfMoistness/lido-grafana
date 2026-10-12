"""Inventory accounting, API failure handling and publication boundaries."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('inventory', ROOT / 'deployment/inventory.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def key(n):
    return '0x' + f'{n:096x}'


def header(root='a'):
    return {'execution_optimistic': False, 'data': {'canonical': True, 'root': '0x'+root*64,
            'header': {'message': {'slot': '100'}}}}


class Beacon:
    def __init__(self, records=None):
        self.records = records or {}
        self.calls = []

    def __call__(self, url, body=None):
        self.calls.append((url, body))
        if url.endswith('/syncing'):
            return {'data': {'is_syncing': False, 'is_optimistic': False, 'el_offline': False}}
        if '/headers/' in url:
            return header()
        return {'execution_optimistic': False, 'data': [
            {'validator': {'pubkey': k}, 'status': self.records[k][0], 'balance': str(self.records[k][1])}
            for k in body['ids'] if k in self.records]}


class Inventory(unittest.TestCase):
    def test_csv_empty_pending_and_duplicate_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            (folder/'validator-1.csv').write_text('\ufeff'+key(1)+'\n\n"'+key(2)+'"\n')
            (folder/'validator-2.csv').write_text('')
            self.assertEqual(inventory.read_inventory(folder,['validator-1','validator-2']),
                             {'validator-1':[key(1),key(2)],'validator-2':[]})
            for content in [key(1), 'pubkey\n'+key(3), key(3)+',extra', '0x1234', key(3)+'\n'+key(3)]:
                (folder/'validator-2.csv').write_text(content)
                with self.subTest(content=content[:12]), self.assertRaises(ValueError):
                    inventory.read_inventory(folder,['validator-1','validator-2'])
            with self.assertRaises(ValueError):
                inventory.read_inventory(folder,['validator-3'])

    def test_balance_uses_actual_gwei_and_all_active_statuses(self):
        fake=Beacon({key(1):('active_ongoing',32_123456789), key(2):('active_exiting',33_000000000),
                     key(3):('active_slashed',31_000000000),key(4):('pending_queued',32_000000000),
                     key(5):('exited_unslashed',34_000000000)})
        result=inventory.snapshot('http://beacon',{'validator-1':[key(i) for i in range(1,7)],'validator-2':[]},fake)
        self.assertEqual(result['validator-1'],dict(keys=6,unknown=1,active=3,balance=Decimal('96.123456789')))
        self.assertEqual(result['validator-2'],dict(keys=0,unknown=0,active=0,balance=Decimal(0)))
        text=inventory.metrics(['validator-1'],result,100).decode()
        self.assertIn('96.123456789',text)
        self.assertNotIn(key(1),text)
        self.assertNotIn('lido_inventory_active_balance_eth{',inventory.metrics(['validator-1']).decode())

    def test_batches_pin_slot_and_recheck_block(self):
        fake=Beacon()
        result=inventory.snapshot('http://beacon',{'validator-1':[key(i) for i in range(300)]},fake)
        batches=[(url,body) for url,body in fake.calls if body is not None]
        self.assertEqual([len(body['ids']) for _,body in batches],[128,128,44])
        self.assertTrue(all('/states/100/validators' in url for url,_ in batches))
        self.assertTrue(fake.calls[-1][0].endswith('/headers/100'))
        self.assertEqual(result['validator-1']['unknown'],300)

    def test_reject_reorg_optimistic_syncing_and_malformed_records(self):
        for failure in ['reorg','optimistic','syncing','offline','missing_sync_field','unexpected','duplicate','negative','bad_status']:
            fake=Beacon({key(1):('active_ongoing',32_000000000)})
            def fetch(url,body=None):
                data=fake(url,body)
                if url.endswith('/syncing'):
                    if failure=='syncing': data['data']['is_syncing']=True
                    if failure=='offline': data['data']['el_offline']=True
                    if failure=='missing_sync_field': del data['data']['is_optimistic']
                if failure=='reorg' and url.endswith('/headers/100'): return header('b')
                if body is not None:
                    if failure=='optimistic': data['execution_optimistic']=True
                    if failure=='unexpected': data['data'][0]['validator']['pubkey']=key(2)
                    if failure=='duplicate': data['data']*=2
                    if failure=='negative': data['data'][0]['balance']='-1'
                    if failure=='bad_status': data['data'][0]['status']='active'
                return data
            with self.subTest(failure=failure),self.assertRaises(ValueError):
                inventory.snapshot('http://beacon',{'validator-1':[key(1)]},fetch)

    def test_fallback_restarts_whole_snapshot_and_all_failures_withhold_values(self):
        fake=Beacon({key(1):('active_ongoing',32_000000000)})
        def fetch(url,body=None):
            if url.startswith('http://main') and body is not None: raise OSError('unavailable')
            return fake(url,body)
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);(folder/'validator-1.csv').write_text(key(1))
            result,source=inventory.collect(folder,['validator-1'],['http://main','http://fallback'],fetch)
            self.assertEqual(source,1)
            self.assertEqual(result['validator-1']['active'],1)
            with self.assertRaises(ValueError):
                inventory.collect(folder,['validator-1'],['http://main'],fetch)

    def test_exporter_clears_previous_values_after_failure(self):
        # Run two polling iterations with deterministic thread/server stand-ins.
        config={'clients':[('validator-1','VC 1')],'inventory_enabled':True,
                'inventory_port':19200,'beacon_apis':['http://beacon']}
        captures=[]
        class Stop(Exception): pass
        class Response:
            path='/metrics'
            def send_response(self,*args): pass
            def send_header(self,*args): pass
            def end_headers(self): pass
            class Writer:
                def write(self,body): captures.append(body.decode())
            wfile=Writer()
        handler=[]
        def server(address, cls): handler.append(cls);return None
        def sleep(_):
            handler[0].do_GET(Response())
            if len(captures)==2: raise Stop()
        class Thread:
            def __init__(self,target,**kwargs): self.target=target
            def start(self): self.target()
        row={'validator-1':dict(keys=1,unknown=0,active=1,balance=Decimal(32))}
        with patch.object(inventory,'ThreadingHTTPServer',side_effect=server),patch.object(inventory.threading,'Thread',Thread),patch.object(inventory,'collect',side_effect=[(row,0),ValueError('failed')]),patch.object(inventory.time,'sleep',side_effect=sleep),self.assertRaises(Stop):
            inventory.serve(config)
        self.assertIn('active_balance_eth',captures[0])
        self.assertNotIn('lido_inventory_active_balance_eth{',captures[1])
        self.assertIn('lido_inventory_success{host="validator-1"} 0',captures[1])


if __name__=='__main__': unittest.main()
