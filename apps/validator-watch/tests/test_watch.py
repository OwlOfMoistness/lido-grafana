import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from watch.chain import Unknown, analyze, bitlist, bitvector, flags, inventory
from watch.store import Store
from watch.notify import drain, read_webhook, deliver
from watch.demo import seed

KEY = '0x' + '1' * 96
OTHER = '0x' + '2' * 96

def root(slot): return '0x' + f'{slot:064x}'
def response(data): return {'execution_optimistic': False, 'data': data}
def header(): return response({'canonical': True, 'root': root(128), 'header': {'message': {'slot': '128'}}})

class FakeBeacon:
    def __init__(self):
        self.calls = []
        self.att = {'aggregation_bits': '0x06', 'committee_bits': '0x0300000000000000',
            'data': {'slot':'66', 'index':'0', 'beacon_block_root':root(66), 'target':{'epoch':'2','root':root(64)}}}
        self.inclusion = 67
        self.extra = []
        self.skip = {70}
        self.duties = [{'validator_index':'1','slot':'66','pubkey':KEY,'committee_index':'1',
                       'validator_committee_index':'0','committee_length':'1','committees_at_slot':'2'}]
        self.fail = None
    def get(self, path, body=None):
        self.calls.append((path,body))
        if self.fail and self.fail in path: raise OSError('Unavailable')
        if path == '/eth/v1/node/syncing':
            return {'data':dict(is_syncing=False,is_optimistic=False,el_offline=False)}
        if '/headers/' in path: return header()
        if '/blinded_blocks/' in path:
            slot = int(path.rsplit('/',1)[1],16)
            previous = slot-1
            while previous in self.skip: previous-=1
            data = {'slot':str(slot),'parent_root':root(previous),'proposer_index':'1' if slot==70 else '999',
                'body': {'attestations':([copy.deepcopy(self.att)] if slot==self.inclusion else []) +
                         [copy.deepcopy(att) for inc,att in self.extra if inc==slot],
                         'sync_aggregate':{'sync_committee_bits':'0x'+'ff'*64}}}
            return dict(response({'message':data}), version='fulu')
        if path.endswith('/validators'):
            return response([{'index':'1','validator':{'pubkey':KEY,'activation_epoch':'0','exit_epoch':str(2**64-1)}}])
        if '/duties/attester/' in path: return response(copy.deepcopy(self.duties))
        if '/committees?' in path:
            return response([{'slot':'66','index':'0','validators':['999']},{'slot':'66','index':'1','validators':['1']}])
        if '/duties/proposer/' in path:
            return response([{'slot':str(s),'validator_index':'1' if s==70 else '999',
                              'pubkey':KEY if s==70 else OTHER} for s in range(64,96)])
        if path.endswith('/sync_committees'):
            return response({'validators':['1','1']+['999']*510})
        raise AssertionError(path)

class Outcomes(unittest.TestCase):
    def result(self, beacon=None): return analyze(beacon or FakeBeacon(),2,{KEY:'Apple'},(32,12,1000))
    def test_ssz_and_timing(self):
        self.assertEqual(bitlist('0x06'),(2,2))
        self.assertEqual(bitlist('0x01'),(0,0))
        with self.assertRaises(Unknown): bitlist('0x00')
        with self.assertRaises(Unknown): bitvector('0xff',7)
        self.assertEqual(flags(1,True,True),(True,True,True))
        self.assertEqual(flags(2,True,True),(True,True,False))
        self.assertEqual(flags(6,True,True),(False,True,False))
        self.assertEqual(flags(1,False,True),(True,False,False))
    def test_electra_offsets_and_proven_skip(self):
        r=self.result()
        self.assertEqual([r['attestations'][0][c] for c in ('source','target','head')],[True]*3)
        self.assertEqual(r['proposals'][0]['status'],'missed')
        self.assertEqual(r['sync'][KEY],[64,0,2]) # duplicate positions; absent block is unknown
    def test_present_proposal(self):
        b=FakeBeacon();b.skip=set()
        self.assertEqual(self.result(b)['proposals'][0]['status'],'included')
    def test_delay_and_union(self):
        b=FakeBeacon();b.inclusion=72
        r=self.result(b)['attestations'][0]
        self.assertEqual((r['source'],r['target'],r['head']),(False,True,False))
        b.extra=[(67,copy.deepcopy(b.att))]
        r=self.result(b)['attestations'][0]
        self.assertTrue(r['head']);self.assertEqual(r['delay'],1)
    def test_wrong_head_and_no_participation(self):
        b=FakeBeacon();b.att['data']['beacon_block_root']=root(65)
        self.assertFalse(self.result(b)['attestations'][0]['head'])
        b.att['aggregation_bits']='0x05' # only OTHER committee position signed
        r=self.result(b)['attestations'][0]
        self.assertFalse(any(r[c] for c in ('source','target','head')))
    def test_partial_evidence_never_returns_missed(self):
        b=FakeBeacon();b.fail='/blinded_blocks/'
        with self.assertRaises(OSError): self.result(b)
        b=FakeBeacon();b.duties=[]
        with self.assertRaises(Unknown): self.result(b)
        b=FakeBeacon();b.att['aggregation_bits']='0x02'
        with self.assertRaises(Unknown): self.result(b)
    def test_deneb_and_unsupported_fork(self):
        b=FakeBeacon();original=b.get
        b.att['aggregation_bits']='0x03';b.att['data']['index']='1'
        def deneb(path,body=None):
            r=original(path,body)
            if 'version' in r:r['version']='deneb'
            return r
        b.get=deneb
        self.assertTrue(self.result(b)['attestations'][0]['head'])
        def future(path,body=None):
            r=original(path,body)
            if 'version' in r:r['version']='future'
            return r
        b.get=future
        with self.assertRaises(Unknown):self.result(b)
    def test_changed_anchor_and_optimistic_fail_closed(self):
        b=FakeBeacon();original=b.get
        def changed(path,body=None):
            r=original(path,body)
            if path.endswith('/headers/128'):r['data']['root']=root(129)
            return r
        b.get=changed
        with self.assertRaises(Unknown):self.result(b)
        def optimistic(path,body=None):
            r=original(path,body)
            if 'version' in r:r['execution_optimistic']=True
            return r
        b.get=optimistic
        with self.assertRaises(Unknown):self.result(b)
    def test_inventory_unique_and_names(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'Apple.csv').write_text(KEY+'\n')
            self.assertEqual(inventory(d),{KEY:'Apple'})
            Path(d,'Banana.csv').write_text(KEY+'\n')
            with self.assertRaises(Unknown): inventory(d)

class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name,'watch.sqlite');self.s=Store(self.path)
        self.r=analyze(FakeBeacon(),2,{KEY:'Apple'},(32,12,1000))
    def tearDown(self): self.s.db.close();self.tmp.cleanup()
    def test_good_duties_only_summary_and_missed_outbox(self):
        self.assertTrue(self.s.save(self.r,True));self.assertFalse(self.s.save(self.r,True))
        self.assertEqual(self.s.db.execute('SELECT expected FROM summary').fetchone()[0],1)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM events WHERE kind='attestation'").fetchone()[0],0)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        self.s.db.close();self.s=Store(self.path)
        self.assertEqual(self.s.get('next_epoch'),'3')
    def test_exception_points_and_atomic_rollback(self):
        self.r['attestations'][0].update(source=False,target=False,head=False)
        self.s.save(self.r)
        self.assertEqual(self.s.db.execute("SELECT score FROM events WHERE kind='attestation'").fetchone()[0],3)
        self.assertEqual(self.s.db.execute('SELECT affected FROM summary').fetchone()[0],1)
        r=copy.deepcopy(self.r);r['epoch']=3
        with self.assertRaises(Exception): self.s.save(r) # duplicate proposal fails whole epoch
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM epochs').fetchone()[0],1)
        self.assertEqual(self.s.db.execute('SELECT expected FROM summary').fetchone()[0],1)
    def test_gaps_no_alerts_retention(self):
        self.s.gap(1,1000,'unavailable');self.s.save(self.r)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        self.s.prune(30,now=40*86400)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM events').fetchone()[0],0)
    def test_notifications_retry_restart_and_disable(self):
        self.s.save(self.r,True)
        sent=[]
        def send(url,body):sent.append(body);return 429,90
        drain(self.s,None,send=send,now=1);self.assertFalse(sent)
        drain(self.s,'fake',send=send,now=1)
        row=self.s.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual((row['state'],row['attempts'],row['next_try']),('pending',1,91))
        drain(self.s,'fake',send=send,now=90);self.assertEqual(len(sent),1)
        drain(self.s,'fake',send=lambda *_:(200,0),now=92)
        self.assertEqual(self.s.db.execute('SELECT state FROM outbox').fetchone()[0],'sent')
        drain(self.s,'fake',send=send,now=1000);self.assertEqual(len(sent),1)
    def test_webhook_validation_and_no_mentions(self):
        p=Path(self.tmp.name,'secret');p.write_text('http://example.org/secret')
        with self.assertRaises(ValueError):read_webhook(p)
        class Reply:
            status=204
            def __enter__(self): return self
            def __exit__(self,*args): pass
        class Opener:
            def open(inner,request,**kw):
                data=json.loads(request.data)
                self.assertEqual(data['allowed_mentions'],{'parse':[]})
                return Reply()
        with patch('watch.notify.urllib.request.build_opener',return_value=Opener()):
            self.assertEqual(deliver('https://discord.com/api/webhooks/1/fake','@everyone'),(204,0))

class Service(unittest.TestCase):
    def test_once_commits_and_startup_does_not_backfill(self):
        from watch.main import run
        with tempfile.TemporaryDirectory() as d:
            Path(d,'Apple.csv').write_text(KEY+'\n')
            path=Path(d,'watch.sqlite')
            b=FakeBeacon();base=b.get
            b.reset=lambda:None;b.calls=0;b.bytes=0
            def get(path,body=None):
                if path.endswith('/genesis'):return {'data':{'genesis_time':'1000','genesis_validators_root':root(999)}}
                if path.endswith('/spec'):return {'data':{'SLOTS_PER_EPOCH':'32','SECONDS_PER_SLOT':'12'}}
                # FakeBeacon normally tracks a list; service uses the real numeric counter.
                b.calls=[]
                result=base(path,body)
                b.calls=1
                return result
            b.get=get
            env={'BEACON_URL':'http://127.0.0.1:5552','PRIMARY_HEALTH_URL':'','INVENTORY_DIR':d,
                 'DATABASE_PATH':str(path),'RETENTION_DAYS':'30','DISCORD_WEBHOOK_FILE':'','DISCORD_WEBHOOK_URL':''}
            with patch.dict(os.environ,env),patch('watch.main.Beacon',return_value=b),patch('watch.main.time.time',return_value=1000+6*384):
                run(once=True)
                st=Store(path)
                self.assertEqual(st.get('next_epoch'),'6')
                self.assertEqual(st.db.execute('SELECT count(*) FROM epochs').fetchone()[0],0)
                self.assertEqual(json.loads(st.get('ownership:6')),{KEY:'Apple'})
                with st.db:
                    st.set('next_epoch',2);st.set('ownership:2',json.dumps({KEY:'Apple'}))
                st.db.close()
                run(once=True)
                st=Store(path)
                self.assertEqual(st.get('next_epoch'),'3')
                self.assertEqual(st.db.execute('SELECT count(*) FROM epochs').fetchone()[0],1)
                self.assertEqual(st.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
                st.db.close()


class Dashboard(unittest.TestCase):
    def test_every_query_executes_against_mock_database(self):
        spec=importlib.util.spec_from_file_location('dash',APP/'grafana/build_dashboards.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d,'demo.sqlite');seed(path);s=Store(path)
            for detail in (False,True):
                dashboard=module.build(detail)
                for p in dashboard['panels']:
                    sql=p['targets'][0]['rawQueryText'].replace('$__from','0').replace('$__to','9999999999999')
                    sql=sql.replace('${client:sqlstring}',"'Apple','Banana','Orange'")
                    sql=sql.replace('${validator:sqlstring}',"'0x"+format(3,'096x')+"'")
                    self.assertIsNotNone(s.db.execute(sql).fetchall())
            s.db.close()

if __name__=='__main__':unittest.main()
