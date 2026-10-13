import argparse
import json
import os
from pathlib import Path
import time
from .chain import Beacon, Unknown, analyze, inventory, number, readiness, require, root
from .store import Store
from .notify import drain, read_webhook
from .alerts import Policy


def capture_ownership(store, directory, timing, now):
    slots, seconds, genesis = timing
    current = int((now-genesis)//(slots*seconds))
    key = f'ownership:{current}'
    if store.get(key) is None:
        with store.db:
            store.set(key,json.dumps(inventory(directory)))
    return current


def queue_epochs(store, latest, timing):
    slots, seconds, genesis = timing
    cursor = int(store.get('queue_cursor',store.get('next_epoch')))
    # Bound local queue work after a long stop; prioritize the newest eligible
    # epoch even when materializing an unusually large backlog across cycles.
    with store.db:
        stop = min(latest+1,cursor+1000)
        for epoch in range(cursor,stop):
            store.queue(epoch,genesis+epoch*slots*seconds)
        if cursor <= latest:
            store.queue(latest,genesis+latest*slots*seconds)
            store.set('queue_cursor',stop)
            store.set('next_epoch',stop)


def collect_one(store, beacon, latest, timing, interval, policy, discord_enabled, now):
    # Recent finalized work always wins. No parallel beacon scans or unbounded
    # replay from the current head back through hours of blocks.
    job = store.db.execute("""SELECT * FROM backlog WHERE epoch BETWEEN ? AND ?
        AND ownership IS NOT NULL AND next_try<=? ORDER BY epoch DESC LIMIT 1""",
        (latest-1,latest,now)).fetchone()
    historical = False
    if job is None and now >= float(store.get('backfill_after','0')):
        job = store.db.execute("""SELECT * FROM backlog WHERE epoch<? AND ownership IS NOT NULL
            AND next_try<=? ORDER BY next_try,epoch LIMIT 1""",(latest-1,now)).fetchone()
        historical = job is not None
    if job is None:
        return
    epoch = job['epoch']
    # Persist a cooldown before starting so a crash/restart cannot create a hot
    # retry loop. Every individual attempt still has Beacon's request/time cap.
    with store.db:
        store.db.execute('UPDATE backlog SET next_try=? WHERE epoch=?',(now+300,epoch))
        if historical:store.set('backfill_after',now+interval)
    try:
        result = analyze(beacon,epoch,json.loads(job['ownership']),timing,
            on_proposals=lambda result:store.save_proposals(result,discord_enabled,historical))
        store.save(result,discord_enabled=discord_enabled,policy=policy,historical=historical)
        if historical:print(f'Backfill completed: epoch={epoch}',flush=True)
    except Exception as exc:
        code = str(exc) if isinstance(exc,Unknown) else type(exc).__name__
        base, cap = (interval,21600) if historical else (30,300)
        delay = min(cap,base*2**min(job['attempts'],10))
        store.defer(epoch,code,delay)
        label = 'Backfill deferred' if historical else 'Collection deferred'
        print(f'{label}: epoch={epoch} retry_in={delay}s reason={code}',flush=True)
    finally:
        if historical:
            with store.db:store.set('backfill_after',time.time()+interval)


def report_health(store, latest, policy, enabled):
    row = store.db.execute('SELECT status,reason FROM epochs WHERE epoch=?',(latest,)).fetchone()
    if row is None:
        store.health('waiting','Awaiting a fully finalized inclusion window',policy,enabled)
    elif row['status']=='checked':
        store.health('healthy',policy=policy,discord_enabled=enabled)
    elif row['reason']=='queued':
        store.health('waiting','Recent epoch queued for collection',policy,enabled)
    else:
        store.health('unknown',row['reason'],policy,enabled)


def run(once=False):
    directory=os.environ.get('INVENTORY_DIR','/inventory')
    store=Store(os.environ.get('DATABASE_PATH','/data/watch.sqlite'))
    beacon=Beacon(os.environ['BEACON_URL'],pause=1)
    webhook=read_webhook(os.environ.get('DISCORD_WEBHOOK_FILE',''), os.environ.get('DISCORD_WEBHOOK_URL',''))
    policy=Policy.from_env(os.environ)
    retention=int(os.environ.get('RETENTION_DAYS','30'))
    interval=int(os.environ.get('BACKFILL_INTERVAL_SECONDS','600'))
    require(1<=retention<=365,'invalid_retention')
    require(60<=interval<=86400,'invalid_backfill_interval')
    try:
        while True:
            try:
                beacon.reset()
                # Once chain timing is established, keep recording inventories
                # even when the beacon or its optional readiness guard is down.
                saved_timing=store.get('chain_timing')
                if saved_timing:
                    capture_ownership(store,directory,json.loads(saved_timing),time.time())
                guard_url=os.environ.get('PRIMARY_HEALTH_URL','')
                if guard_url:
                    readiness(Beacon(guard_url,role='primary_health'))
                genesis_data=beacon.get('/eth/v1/beacon/genesis')['data']
                chain_id=root(genesis_data['genesis_validators_root'])
                require(store.get('chain_id',chain_id)==chain_id,'chain_changed')
                spec=beacon.get('/eth/v1/config/spec')['data']
                slots=number(spec['SLOTS_PER_EPOCH'])
                seconds=(number(spec['SLOT_DURATION_MS'])/1000 if 'SLOT_DURATION_MS' in spec else number(spec['SECONDS_PER_SLOT']))
                genesis=number(genesis_data['genesis_time'])
                require(slots==32 and seconds==12,'unsupported_chain_timing')
                timing=(slots,int(seconds),genesis)
                with store.db:
                    store.set('chain_id',chain_id)
                    store.set('chain_timing',json.dumps(timing))
                current=capture_ownership(store,directory,timing,time.time())
                if store.get('next_epoch') is None:
                    with store.db:store.set('next_epoch',current)
                final_slot,_=readiness(beacon)
                require(0 <= time.time()-(genesis+final_slot*seconds) <= 8*slots*seconds,'stale_finality')
                latest=final_slot//slots-2
                queue_epochs(store,latest,timing)
                collect_one(store,beacon,latest,timing,interval,policy,webhook is not None,time.time())
                report_health(store,latest,policy,webhook is not None)
                with store.db:
                    store.set('last_request_count',beacon.calls)
                    store.set('last_response_bytes',beacon.bytes)
                store.prune(retention)
            except Exception as exc:
                # Do not leak node URLs, inventories or webhook tokens into logs.
                code=str(exc) if isinstance(exc,Unknown) else type(exc).__name__
                store.health('unknown',code,policy=policy,discord_enabled=webhook is not None)
                print('Collection paused: '+code,flush=True)
            drain(store,webhook)
            if once:return
            time.sleep(30)
    finally:
        store.db.close()


def backfill(store, directory, first, last, confirmed):
    """Operator explicitly attests that these CSVs describe the historical range."""
    require(confirmed,'confirm_historical_inventory_required')
    require(0<=first<=last and last-first<10000,'invalid_backfill_range')
    timing=store.get('chain_timing')
    require(timing is not None,'start_collector_first_to_record_chain_timing')
    slots,seconds,genesis=json.loads(timing)
    require(last < int((time.time()-genesis)//(slots*seconds))-2,'backfill_range_not_historical')
    ownership=inventory(directory)
    queued=0
    with store.db:
        for epoch in range(first,last+1):
            # Existing snapshots are authoritative and must never be overwritten.
            store.queue(epoch,genesis+epoch*slots*seconds,ownership)
            queued+=int(store.db.execute('SELECT 1 FROM backlog WHERE epoch=?',(epoch,)).fetchone() is not None)
    return queued


def main():
    parser=argparse.ArgumentParser(description='Validator Watch finalized duty collector')
    parser.add_argument('command',choices=['serve','once','probe','demo','configure','backfill'],nargs='?',default='serve')
    args,extra=parser.parse_known_args()
    if args.command=='probe':
        import probe
        return probe.main(extra)
    if args.command=='backfill':
        backfill_parser=argparse.ArgumentParser(description='Queue historical epochs using a confirmed inventory')
        backfill_parser.add_argument('--from-epoch',type=int,required=True)
        backfill_parser.add_argument('--to-epoch',type=int,required=True)
        backfill_parser.add_argument('--confirm-inventory',action='store_true',required=True)
        options=backfill_parser.parse_args(extra)
        store=Store(os.environ.get('DATABASE_PATH','/data/watch.sqlite'))
        try:
            count=backfill(store,os.environ.get('INVENTORY_DIR','/inventory'),options.from_epoch,options.to_epoch,options.confirm_inventory)
            print(f'Queued {count} incomplete epochs for slow recovery; existing ownership snapshots preserved.')
        finally:store.db.close()
        return 0
    if extra:parser.error('Unexpected arguments')
    if args.command=='configure':
        import shutil
        shutil.copytree('/app/grafana','/provisioned',dirs_exist_ok=True)
        return 0
    if args.command=='demo':
        from .demo import seed
        seed(Path(os.environ.get('DATABASE_PATH','/data/watch.sqlite')))
        return 0
    run(once=args.command=='once')
    return 0


if __name__=='__main__':raise SystemExit(main())
