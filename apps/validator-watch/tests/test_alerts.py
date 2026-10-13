from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from watch.alerts import Policy, attestation_alerts, health_alerts
from watch.notify import read_webhook
from watch.store import Store


def sample(epoch, affected=2, expected=200, clients=('Apple',)):
    ownership, attestations = {}, []
    for j, client in enumerate(clients):
        for i in range(expected):
            key = '0x' + f'{j*1000+i+1:096x}'
            ownership[key] = client
            attestations.append(dict(pubkey=key,client=client,slot=epoch*32,
                                     source=i>=affected,target=i>=affected,head=i>=affected,delay=None))
    if expected == 0:
        ownership['0x'+'1'*96] = clients[0]
    return dict(epoch=epoch,time=epoch*384,genesis=0,seconds=12,slots=32,anchor='0x'+'1'*64,
                ownership=ownership,attestations=attestations,proposals=[],sync={},unknown_keys=0)


class Alerts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name,'watch.sqlite')
        self.store=Store(self.path)
        self.policy=Policy()
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def collect(self, epoch, affected=2, expected=200, clients=('Apple',), enabled=True):
        self.store.save(sample(epoch,affected,expected,clients),enabled,self.policy)
    def messages(self):
        return [row['content'] for row in self.store.db.execute('SELECT * FROM outbox ORDER BY created,id')]
    def test_exact_ten_per_thousand_counts_duties_once_and_requires_two_epochs(self):
        self.collect(1);self.assertEqual(self.messages(),[])
        self.collect(2)
        self.assertEqual(len(self.messages()),1)
        self.assertIn('2/200 affected (10.00 per 1,000)',self.messages()[0])
        # Each of the two bad duties misses all three flags, still only 10 per 1,000.
        self.assertNotIn('30.00 per 1,000',self.messages()[0])
        self.collect(3);self.collect(3)
        self.assertEqual(len(self.messages()),1)
    def test_below_threshold_recovery_and_restart_persistence(self):
        self.collect(1);self.collect(2)
        self.store.db.close();self.store=Store(self.path)
        self.collect(3);self.assertEqual(len(self.messages()),1)
        self.collect(4,1);self.assertEqual(len(self.messages()),1)
        self.collect(5,1);self.assertEqual(len(self.messages()),2)
        self.assertIn('recovered',self.messages()[1])
        self.collect(6);self.collect(7);self.assertEqual(len(self.messages()),3)
    def test_gaps_and_zero_duties_never_establish_recovery_or_repeat(self):
        self.collect(1);self.store.gap(2,768,'unavailable');self.collect(3)
        self.assertEqual(self.messages(),[])
        self.collect(4);self.assertEqual(len(self.messages()),1)
        self.collect(5,0,0);self.collect(6,0)
        self.assertEqual(len(self.messages()),1)
        self.collect(7,0);self.assertEqual(len(self.messages()),2)
    def test_no_false_alert_below_ten_per_thousand_and_small_fleets(self):
        self.collect(1,1);self.collect(2,1)
        self.assertEqual(self.messages(),[])
        self.collect(3,1,50);self.collect(4,1,50)
        self.assertIn('1/50 affected (20.00 per 1,000)',self.messages()[0])
    def test_fractional_per_thousand_threshold(self):
        self.policy=Policy.from_env({'ATTESTATION_ISSUE_PER_THOUSAND':'0.5'})
        self.collect(1,1,2001);self.collect(2,1,2001)
        self.assertEqual(self.messages(),[])
        self.collect(3,1,2000);self.collect(4,1,2000)
        self.assertIn('1/2000 affected (0.50 per 1,000)',self.messages()[0])
        self.assertIn('At least 0.5 per 1,000',self.messages()[0])
    def test_lower_threshold_and_policy_change_reset_streak(self):
        self.collect(1,2,200)
        self.policy=Policy.from_env({'ATTESTATION_ISSUE_PER_THOUSAND':'5'})
        self.collect(2,1,200)
        self.assertEqual(self.messages(),[])
        self.collect(3,1,200)
        self.assertIn('1/200 affected (5.00 per 1,000)',self.messages()[0])
        self.collect(4,0,200);self.collect(5,0,200)
        self.assertIn('Below 5 per 1,000',self.messages()[1])
    def test_fleet_grouping(self):
        self.collect(1,clients=('Apple','Banana'))
        self.collect(2,clients=('Apple','Banana'))
        self.assertEqual(len(self.messages()),1)
        self.assertIn('Fleet alert',self.messages()[0])
        self.assertIn('Apple:',self.messages()[0]);self.assertIn('Banana:',self.messages()[0])
    def test_disable_then_enable_ongoing_incident_once(self):
        self.collect(1,enabled=False);self.collect(2,enabled=False)
        self.assertEqual(self.messages(),[])
        self.collect(3);self.collect(4)
        self.assertEqual(len(self.messages()),1)
    def test_health_delay_no_spam_and_recovery(self):
        with self.store.db:
            health_alerts(self.store,False,'node_unready',self.policy,True,now=1000)
            health_alerts(self.store,False,'node_unready',self.policy,True,now=1299)
        self.assertEqual(self.messages(),[])
        with self.store.db:
            health_alerts(self.store,False,'node_unready',self.policy,True,now=1300)
            health_alerts(self.store,False,'node_unready',self.policy,True,now=1400)
        self.assertEqual(len(self.messages()),1)
        with self.store.db:
            health_alerts(self.store,True,'',self.policy,True,now=1500)
            health_alerts(self.store,True,'',self.policy,True,now=1600)
        self.assertEqual(len(self.messages()),2)
        self.assertIn('recovered',self.messages()[1])
    def test_config_validation_and_webhook_sources(self):
        self.assertEqual(Policy.from_env({}).per_thousand,Decimal('10'))
        self.assertEqual(Policy.from_env({'ATTESTATION_ISSUE_PER_THOUSAND':'1000'}).per_thousand,Decimal('1000'))
        p=Policy.from_env({'ATTESTATION_ISSUE_PER_THOUSAND':'0.5','ATTESTATION_ISSUE_EPOCHS':'3'})
        self.assertEqual((p.per_thousand,p.epochs),(Decimal('0.5'),3))
        for value in ('0','-1','1001','NaN','Infinity','bad'):
            with self.assertRaises(ValueError):Policy.from_env({'ATTESTATION_ISSUE_PER_THOUSAND':value})
        for value in ('0','33','1.5'):
            with self.assertRaises(ValueError):Policy.from_env({'ATTESTATION_ISSUE_EPOCHS':value})
        url='https://discord.com/api/webhooks/123/fake-test-token'
        self.assertEqual(read_webhook(url=url),url)
        self.assertIsNone(read_webhook())
        with self.assertRaises(ValueError):read_webhook('file',url)
        with self.assertRaises(ValueError):read_webhook(url='http://example.org/webhook')
    def test_rollback_leaves_no_outbox_or_state(self):
        self.collect(1)
        with self.assertRaises(RuntimeError):
            with self.store.db:
                attestation_alerts(self.store,sample(2),self.policy,True)
                raise RuntimeError('Interrupted')
        self.assertEqual(self.messages(),[])
        self.collect(2);self.assertEqual(len(self.messages()),1)

    def test_out_of_order_recent_epoch_cannot_rewind_alert_state(self):
        self.collect(3);self.collect(4)
        state=self.store.get('alert:attestation:Apple')
        self.collect(2,0)
        self.assertEqual(self.store.get('alert:attestation:Apple'),state)
        self.assertEqual(len(self.messages()),1)
        self.collect(5,0);self.collect(6,0)
        self.assertEqual(len(self.messages()),2)

if __name__=='__main__':unittest.main()
