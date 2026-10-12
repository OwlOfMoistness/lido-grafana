"""Deterministic mock data, isolated from collectors and notification delivery."""
import random
import time
from .store import Store


def seed(path):
    if path.exists():raise ValueError('Demo requires a new database path')
    rng=random.Random(42);store=Store(path)
    now=int(time.time())//3600*3600
    names=['Apple','Banana','Orange']
    for hour in range(24):
        when=now-(23-hour)*3600
        for i in range(50):
            client=names[min(i//17,2)];key='0x'+format(i+1,'096x')
            misses = (rng.randrange(1,4) if i in (2,7,19,36) and hour in (7,8,16,20) else 0)
            source=int(misses>=3);target=int(misses>=2);head=int(misses>=1)
            with store.db:
                store.db.execute('INSERT INTO summary VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (when,client,key,8 if hour==12 else 9, int(misses>0),source,target,head,32 if i==7 else 0,2 if i==7 and hour==8 else 0,0))
                if misses:
                    store.event(f'demo-a-{hour}-{i}',when+600,100000+hour,3200000+hour*32+i,
                        client,key,'attestation','issue',1-source,1-target,1-head,misses,2 if misses==1 else 1 if misses==2 else None,'')
        with store.db:
            store.db.execute('INSERT INTO epochs VALUES(?,?,?,?,?,?)',
                (100000+hour*9,when,'unknown' if hour==12 else 'checked','demo connection gap' if hour==12 else '', '0x'+'a'*64,0))
            for sub in range(1,9):
                store.db.execute('INSERT INTO epochs VALUES(?,?,?,?,?,?)',(100000+hour*9+sub,when+sub*384,'checked','','0x'+'a'*64,0))
    with store.db:
        for n,ago,client,status in [(1,2,'Apple','included'),(2,5,'Banana','missed'),(3,10,'Orange','included')]:
            when=now-ago*3600+1800
            store.event(f'demo-p-{n}',when,100100+n,3203200+n,client,'0x'+format({'Apple':3,'Banana':20,'Orange':37}[client],'096x'),
                'proposal',status,None,None,None,None,None,'' if status=='missed' else '0x'+'b'*64)
        store.db.execute("INSERT INTO outbox VALUES('demo-only','Mock confirmed missed proposal','sent',1,0,'200',?)",(now-5*3600,))
        store.set('collector_state','MOCK DATA');store.set('heartbeat',time.time())
        store.set('last_checked',now);store.set('last_request_count',103);store.set('last_response_bytes',2600000)
    store.db.close()
