"""SQLite summaries, exception history and a durable notification outbox."""
import json
import sqlite3
import time
from pathlib import Path
from .alerts import Policy, attestation_alerts, health_alerts

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epochs(epoch INTEGER PRIMARY KEY,time INTEGER NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',anchor TEXT NOT NULL DEFAULT '',unknown_keys INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS summary(bucket INTEGER,client TEXT,pubkey TEXT,expected INTEGER DEFAULT 0,affected INTEGER DEFAULT 0,source_miss INTEGER DEFAULT 0,target_miss INTEGER DEFAULT 0,head_miss INTEGER DEFAULT 0,sync_expected INTEGER DEFAULT 0,sync_missed INTEGER DEFAULT 0,sync_unknown INTEGER DEFAULT 0,PRIMARY KEY(bucket,client,pubkey));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,time INTEGER,epoch INTEGER,slot INTEGER,client TEXT,pubkey TEXT,kind TEXT,status TEXT,source INTEGER,target INTEGER,head INTEGER,score INTEGER,delay INTEGER,block_root TEXT);
CREATE INDEX IF NOT EXISTS events_time ON events(time);
CREATE INDEX IF NOT EXISTS summary_client ON summary(client,bucket);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,content TEXT,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0,last_status TEXT NOT NULL DEFAULT '',created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS backlog(epoch INTEGER PRIMARY KEY,ownership TEXT,attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0,proposals_checked INTEGER NOT NULL DEFAULT 0);
'''


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript(SCHEMA)
        # Upgrade old databases without discarding existing coverage gaps. Old
        # releases may have deleted ownership; those rows require confirmation.
        self.db.execute("""INSERT OR IGNORE INTO backlog(epoch,ownership)
            SELECT epoch,(SELECT value FROM meta WHERE key='ownership:' || epoch)
            FROM epochs WHERE status='unknown'""")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

    def health(self, state, detail='', policy=None, discord_enabled=False):
        with self.db:
            self.set('collector_state',state); self.set('collector_detail',detail)
            self.set('heartbeat',time.time())
            if policy is not None:
                health_alerts(self, state in ('healthy','waiting'), detail, policy, discord_enabled)

    def gap(self, epoch, timestamp, reason):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO epochs(epoch,time,status,reason) VALUES(?,?,'unknown',?)",(epoch,timestamp,reason))
            self.set('next_epoch',max(epoch+1,int(self.get('next_epoch','0'))))

    def queue(self, epoch, timestamp, ownership=None):
        """Queue incomplete evidence, never replace a captured ownership list."""
        with self.db:
            row = self.db.execute('SELECT status FROM epochs WHERE epoch=?',(epoch,)).fetchone()
            if row and row['status']=='checked':
                return
            saved = self.get(f'ownership:{epoch}')
            saved = saved if saved is not None else (json.dumps(ownership) if ownership is not None else None)
            self.db.execute("INSERT OR IGNORE INTO epochs(epoch,time,status,reason) VALUES(?,?,'unknown',?)",
                            (epoch,timestamp,'queued' if saved else 'ownership_not_observed'))
            self.db.execute("""INSERT OR IGNORE INTO backlog(epoch,ownership)
                SELECT ?,? WHERE EXISTS(SELECT 1 FROM epochs WHERE epoch=? AND status='unknown')""",
                (epoch,saved,epoch))
            if saved:
                self.db.execute('UPDATE backlog SET ownership=? WHERE epoch=? AND ownership IS NULL',(saved,epoch))
                self.db.execute("UPDATE epochs SET reason='queued' WHERE epoch=? AND reason IN ('catchup_limit','ownership_not_observed')",(epoch,))

    def defer(self, epoch, reason, delay, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('UPDATE backlog SET attempts=attempts+1,next_try=? WHERE epoch=?',(now+delay,epoch))
            self.db.execute("UPDATE epochs SET reason=? WHERE epoch=? AND status='unknown'",(reason,epoch))

    def save_proposals(self, result, discord_enabled=False, historical=False):
        """Commit independently of committee/attestation endpoint availability."""
        with self.db:
            self._proposals(result,discord_enabled,historical)
            self.db.execute('UPDATE backlog SET proposals_checked=1 WHERE epoch=?',(result['epoch'],))

    def _proposals(self, result, discord_enabled, historical):
        e = result['epoch']
        for p in result['proposals']:
            ident = f"proposal:{p['slot']}:{p['pubkey']}"
            when = result['genesis']+p['slot']*result['seconds']
            existing = self.db.execute('SELECT * FROM events WHERE id=?',(ident,)).fetchone()
            if existing:
                if (existing['epoch'],existing['status'],existing['block_root'],existing['client']) != (e,p['status'],p['block_root'],p['client']):
                    raise ValueError('Conflicting proposal evidence')
                continue
            self.event(ident,when,e,p['slot'],p['client'],p['pubkey'],'proposal',p['status'],None,None,None,None,None,p['block_root'])
            if p['status']=='missed' and discord_enabled:
                title = 'Confirmed missed proposal (historical catch-up)' if historical else 'Confirmed missed proposal'
                content = (f"{title}\nClient: {p['client']}\nValidator: {p['pubkey']}\n"
                           f"Slot: {p['slot']} · scheduled <t:{when}:F>\n"
                           f"Finalized canonical evidence; cause unknown.\nDetected <t:{int(time.time())}:F>\nIncident: {ident}")
                self.db.execute('INSERT OR IGNORE INTO outbox(id,content,created) VALUES(?,?,?)',(ident,content,time.time()))

    def save(self, result, discord_enabled=False, policy=None, historical=False):
        e, timestamp = result['epoch'], result['time']
        # Epoch insertion, counter deltas, exceptions, cursor and alert are one transaction.
        with self.db:
            if self.db.execute("SELECT 1 FROM epochs WHERE epoch=? AND status='checked'",(e,)).fetchone():
                self.db.execute('DELETE FROM backlog WHERE epoch=?',(e,))
                return False
            self.db.execute("""INSERT INTO epochs VALUES(?,?,'checked','',?,?)
                ON CONFLICT(epoch) DO UPDATE SET status='checked',reason='',anchor=excluded.anchor,unknown_keys=excluded.unknown_keys""",
                (e,timestamp,result['anchor'],result['unknown_keys']))
            bucket = timestamp//3600*3600
            by_key = {a['pubkey']:a for a in result['attestations']}
            for key, client in result['ownership'].items():
                a = by_key.get(key)
                missing = [int(not a[c]) for c in ('source','target','head')] if a else [0,0,0]
                sync = result['sync'].get(key,[0,0,0])
                values=(bucket,client,key,int(a is not None),int(any(missing)),*missing,*sync)
                self.db.execute('''INSERT INTO summary VALUES(?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(bucket,client,pubkey) DO UPDATE SET expected=expected+excluded.expected,
                  affected=affected+excluded.affected,source_miss=source_miss+excluded.source_miss,
                  target_miss=target_miss+excluded.target_miss,head_miss=head_miss+excluded.head_miss,
                  sync_expected=sync_expected+excluded.sync_expected,sync_missed=sync_missed+excluded.sync_missed,
                  sync_unknown=sync_unknown+excluded.sync_unknown''', values)
                if a and any(missing):
                    self.event(f'attestation:{e}:{key}', result['genesis']+a['slot']*result['seconds'],e,a['slot'],client,key,
                               'attestation','issue',*(int(a[c]) for c in ('source','target','head')),sum(missing),a['delay'],'')
            self._proposals(result,discord_enabled,historical)
            if not historical:
                attestation_alerts(self, result, policy or Policy(), discord_enabled)
            self.set('next_epoch',max(e+1,int(self.get('next_epoch','0'))))
            self.set('last_checked',max(timestamp,int(self.get('last_checked','0'))))
            self.db.execute('DELETE FROM backlog WHERE epoch=?',(e,))
            self.db.execute('DELETE FROM meta WHERE key=?',(f'ownership:{e}',))
        return True

    def event(self, *args):
        self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',args)

    def prune(self, days=30, now=None):
        cutoff = int((time.time() if now is None else now)-days*86400)
        with self.db:
            # Pending jobs/evidence are never aged out before recovery. Completed
            # history uses the normal retention; notification dedup remains durable.
            self.db.execute('DELETE FROM events WHERE time<? AND epoch NOT IN (SELECT epoch FROM backlog)',(cutoff,))
            self.db.execute("DELETE FROM epochs WHERE time<? AND status='checked'",(cutoff,))
            # Whole hourly buckets: at most one additional partial hour is retained.
            self.db.execute('DELETE FROM summary WHERE bucket<?',(cutoff//3600*3600,))
            self.db.execute("DELETE FROM outbox WHERE created<? AND state!='pending' AND id NOT LIKE 'proposal:%'",(cutoff,))
        self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')
