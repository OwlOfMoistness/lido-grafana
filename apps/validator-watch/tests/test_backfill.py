"""Recovery must preserve evidence, attribution and notifications across failures."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_watch import FakeBeacon, KEY, root, response
from watch.chain import analyze, BeaconRequestError, Unknown
from watch.store import Store
from watch.alerts import Policy
from watch.main import backfill, capture_ownership, collect_one, queue_epochs, report_health, run

TIMING = (32,12,1000)
OWNERSHIP = {KEY:'Apple'}


def historical_beacon(missing_anchor_slots=()):
    b=FakeBeacon()
    original=b.get
    b.skip.update(missing_anchor_slots)
    def get(path,body=None):
        if '/headers/' in path:
            ident=path.rsplit('/',1)[1]
            slot=4096 if ident=='finalized' else int(ident)
            b.calls.append((path,body))
            if slot in missing_anchor_slots:
                raise BeaconRequestError('header not found',404)
            return response({'canonical':True,'root':root(slot),'header':{'message':{'slot':str(slot)}}})
        return original(path,body)
    b.get=get
    return b


class HistoricalEvidence(unittest.TestCase):
    def test_old_epoch_is_bounded_and_uses_recent_registry(self):
        b=historical_beacon()
        result=analyze(b,2,OWNERSHIP,TIMING)
        self.assertEqual(result['proposals'][0]['status'],'missed')
        self.assertTrue(result['attestations'][0]['head'])
        self.assertIn(('/eth/v1/beacon/states/4096/validators',{'ids':[KEY]}),b.calls)
        blocks=[p for p,_ in b.calls if '/blinded_blocks/' in p]
        self.assertEqual(len(blocks),64) # 64..128, with slot 70 skipped
        self.assertEqual(blocks[0],'/eth/v1/beacon/blinded_blocks/'+root(128))
        self.assertEqual(result['anchor'],root(128))

    def test_skipped_anchor_search_never_becomes_a_proposal_miss(self):
        b=historical_beacon({128,129})
        result=analyze(b,2,OWNERSHIP,TIMING)
        self.assertEqual(result['anchor'],root(130))
        self.assertEqual([p['slot'] for p in result['proposals']],[70])
        self.assertTrue(result['attestations'][0]['head'])
        b=historical_beacon(set(range(128,160)))
        with self.assertRaisesRegex(Unknown,'historical_anchor_unavailable'):
            analyze(b,2,OWNERSHIP,TIMING)
        self.assertFalse(any('/blinded_blocks/' in p for p,_ in b.calls))

    def test_unavailable_block_or_ambiguous_anchor_never_notifies(self):
        for problem in ('block404','timeout','canonical','optimistic','changed'):
            with self.subTest(problem=problem):
                b=historical_beacon();original=b.get;seen=0;outcomes=[]
                def get(path,body=None):
                    nonlocal seen
                    if problem=='block404' and '/blinded_blocks/' in path:
                        raise BeaconRequestError('unavailable block',404)
                    if path.endswith('/headers/128'):
                        seen+=1
                        if problem=='timeout':raise BeaconRequestError('timeout')
                    result=original(path,body)
                    if path.endswith('/headers/128'):
                        if problem=='canonical':result['data']['canonical']=False
                        if problem=='optimistic':result['execution_optimistic']=True
                        if problem=='changed' and seen==2:result['data']['root']=root(127)
                    return result
                b.get=get
                with self.assertRaises(Unknown):analyze(b,2,OWNERSHIP,TIMING,on_proposals=outcomes.append)
                self.assertEqual(outcomes,[])

    def test_registry_timeout_still_allows_independent_proposals(self):
        b=historical_beacon();original=b.get;outcomes=[]
        def get(path,body=None):
            if path.endswith('/validators'):raise Unknown('registry_timeout')
            return original(path,body)
        b.get=get
        with self.assertRaisesRegex(Unknown,'registry_timeout'):
            analyze(b,2,OWNERSHIP,TIMING,on_proposals=outcomes.append)
        self.assertEqual(len(outcomes),1)
        self.assertEqual(outcomes[0]['proposals'][0]['status'],'missed')


class RecoveryQueue(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name,'watch.sqlite')
        self.store=Store(self.path)
        Path(self.tmp.name,'Apple.csv').write_text(KEY+'\n')
        with self.store.db:self.store.set('chain_timing',json.dumps(TIMING))
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def queue(self,epoch=2,owner=OWNERSHIP):
        self.store.queue(epoch,1000+epoch*384,owner)
    def result(self):
        return analyze(FakeBeacon(),2,OWNERSHIP,TIMING)

    def test_partial_proposals_survive_commit_restart_and_full_retry_once(self):
        self.queue()
        b=FakeBeacon();original=b.get
        def get(path,body=None):
            if '/committees?' in path:raise Unknown('committee_timeout')
            return original(path,body)
        b.get=get
        with self.assertRaisesRegex(Unknown,'committee_timeout'):
            analyze(b,2,OWNERSHIP,TIMING,
                on_proposals=lambda r:self.store.save_proposals(r,True,True))
        self.assertEqual(self.store.db.execute('SELECT status FROM epochs').fetchone()[0],'unknown')
        self.assertEqual(self.store.db.execute('SELECT proposals_checked FROM backlog').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM summary').fetchone()[0],0)
        self.store.db.close();self.store=Store(self.path)
        self.assertTrue(self.store.save(self.result(),True,historical=True))
        self.assertFalse(self.store.save(self.result(),True,historical=True))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM backlog').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT expected FROM summary').fetchone()[0],1)
        messages=self.store.db.execute('SELECT content FROM outbox').fetchall()
        self.assertEqual(len(messages),1)
        self.assertIn('historical catch-up',messages[0][0])

    def test_repaired_gap_is_atomic_and_does_not_rewind_live_progress(self):
        self.store.gap(2,1768,'catchup_limit')
        self.queue()
        with self.store.db:
            self.store.set('next_epoch',100)
            self.store.set('last_checked',99999)
            self.store.set('alert:attestation:Apple','{"last_epoch":99,"bad":1}')
        with patch.object(self.store,'event',side_effect=RuntimeError('disk error')):
            with self.assertRaises(RuntimeError):self.store.save(self.result(),True,historical=True)
        self.assertEqual(self.store.db.execute('SELECT status FROM epochs').fetchone()[0],'unknown')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM summary').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM backlog').fetchone()[0],1)
        self.store.save(self.result(),True,historical=True)
        self.assertEqual(self.store.get('next_epoch'),'100')
        self.assertEqual(self.store.get('last_checked'),'99999')
        self.assertEqual(json.loads(self.store.get('alert:attestation:Apple')),{'last_epoch':99,'bad':1})

    def test_old_gaps_require_confirmation_and_existing_attribution_wins(self):
        self.store.gap(2,1768,'catchup_limit')
        with self.store.db:self.store.set('ownership:3',json.dumps({KEY:'Banana'}))
        self.store.gap(3,2152,'catchup_limit')
        self.store.db.close();self.store=Store(self.path)
        rows=self.store.db.execute('SELECT * FROM backlog ORDER BY epoch').fetchall()
        self.assertIsNone(rows[0]['ownership'])
        self.assertEqual(json.loads(rows[1]['ownership']),{KEY:'Banana'})
        with self.assertRaisesRegex(Unknown,'confirm_historical_inventory_required'):
            backfill(self.store,self.tmp.name,2,3,False)
        self.assertEqual(backfill(self.store,self.tmp.name,2,3,True),2)
        rows=self.store.db.execute('SELECT * FROM backlog ORDER BY epoch').fetchall()
        self.assertEqual(json.loads(rows[0]['ownership']),OWNERSHIP)
        self.assertEqual(json.loads(rows[1]['ownership']),{KEY:'Banana'})

    def test_recent_work_wins_then_backfill_is_throttled_across_restart(self):
        self.queue(2);self.queue(9);self.queue(10)
        calls=[]
        def analyze_job(beacon,epoch,ownership,timing,on_proposals=None):
            calls.append(epoch)
            r=self.result();r.update(epoch=epoch,time=1000+epoch*384,proposals=[])
            return r
        with patch('watch.main.analyze',side_effect=analyze_job),patch('watch.main.time.time',return_value=10000):
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),False,10000)
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),False,10000)
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),False,10000)
        self.assertEqual(calls,[10,9,2])
        self.queue(3)
        self.store.db.close();self.store=Store(self.path)
        with patch('watch.main.analyze',side_effect=analyze_job),patch('watch.main.time.time',return_value=10599):
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),False,10599)
        self.assertEqual(calls,[10,9,2])
        with patch('watch.main.analyze',side_effect=analyze_job),patch('watch.main.time.time',return_value=10600):
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),False,10600)
        self.assertEqual(calls,[10,9,2,3])

    def test_backoff_allows_other_history_and_does_not_poison_recent_health(self):
        self.queue(2);self.queue(3)
        with self.store.db:self.store.db.execute("INSERT INTO epochs(epoch,time,status) VALUES(10,4840,'checked')")
        with patch('watch.main.analyze',side_effect=Unknown('historical_timeout')) as scan,patch('watch.main.time.time',return_value=10000):
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),True,10000)
            self.assertEqual(scan.call_args.args[1],2)
        row=self.store.db.execute('SELECT * FROM backlog WHERE epoch=2').fetchone()
        self.assertEqual((row['attempts'],row['next_try']),(1,10600))
        with patch('watch.main.analyze',side_effect=Unknown('historical_timeout')) as scan,patch('watch.main.time.time',return_value=10600):
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),True,10600)
            self.assertEqual(scan.call_args.args[1],3)
            report_health(self.store,10,Policy(),True)
        self.assertEqual(self.store.get('collector_state'),'healthy')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM backlog').fetchone()[0],2)
        with patch('watch.main.analyze',side_effect=Unknown('historical_timeout')) as scan,patch('watch.main.time.time',return_value=11200):
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),True,11200)
            self.assertEqual(scan.call_args.args[1],2)
        row=self.store.db.execute('SELECT * FROM backlog WHERE epoch=2').fetchone()
        self.assertEqual((row['attempts'],row['next_try']),(2,12400))

    def test_no_retry_without_observed_ownership(self):
        self.queue(2,None)
        with patch('watch.main.analyze') as scan:
            collect_one(self.store,Mock(),10,TIMING,600,Policy(),False,10000)
        scan.assert_not_called()

    def test_long_backlog_materialization_does_not_lose_middle_epochs(self):
        with self.store.db:self.store.set('next_epoch',1)
        queue_epochs(self.store,2005,TIMING)
        self.assertEqual(self.store.get('queue_cursor'),'1001')
        # Saving latest advances the legacy result cursor, but not the queue cursor.
        r=self.result();r.update(epoch=2005,time=1000+2005*384,proposals=[])
        self.store.save(r)
        queue_epochs(self.store,2005,TIMING)
        queue_epochs(self.store,2005,TIMING)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM epochs').fetchone()[0],2005)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM backlog').fetchone()[0],2004)

    def test_pruning_never_drops_unresolved_jobs_or_their_proposals(self):
        self.queue();self.store.save_proposals(self.result(),True,True)
        self.store.defer(2,'timeout',600,now=1000)
        self.store.prune(1,now=9999999999)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM backlog').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM epochs').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM events').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_inventory_capture_is_immutable_and_continues_during_beacon_failure(self):
        now=1000+7*384
        capture_ownership(self.store,self.tmp.name,TIMING,now)
        Path(self.tmp.name,'Apple.csv').rename(Path(self.tmp.name,'Banana.csv'))
        capture_ownership(self.store,self.tmp.name,TIMING,now)
        self.assertEqual(json.loads(self.store.get('ownership:7')),OWNERSHIP)
        env={'BEACON_URL':'http://127.0.0.1:5552','PRIMARY_HEALTH_URL':'',
             'DATABASE_PATH':str(self.path),'INVENTORY_DIR':self.tmp.name,
             'DISCORD_WEBHOOK_URL':'','DISCORD_WEBHOOK_FILE':''}
        b=Mock();b.get.side_effect=Unknown('node_unavailable')
        with patch.dict(os.environ,env),patch('watch.main.Beacon',return_value=b),patch('watch.main.time.time',return_value=now+384):
            run(once=True)
        self.assertEqual(json.loads(self.store.get('ownership:8')),{KEY:'Banana'})


if __name__=='__main__':unittest.main()
