import argparse
import json
import os
from pathlib import Path
import time
from .chain import Beacon, Unknown, analyze, inventory, number, readiness, require, root
from .store import Store
from .notify import drain, read_webhook
from .alerts import Policy


def run(once=False):
    directory=os.environ.get('INVENTORY_DIR','/inventory')
    store=Store(os.environ.get('DATABASE_PATH','/data/watch.sqlite'))
    beacon=Beacon(os.environ['BEACON_URL'],pause=1)
    webhook=read_webhook(os.environ.get('DISCORD_WEBHOOK_FILE',''), os.environ.get('DISCORD_WEBHOOK_URL',''))
    policy=Policy.from_env(os.environ)
    retention=int(os.environ.get('RETENTION_DAYS','30'))
    require(1<=retention<=365,'invalid_retention')
    while True:
        try:
            beacon.reset()
            guard_url=os.environ.get('PRIMARY_HEALTH_URL','')
            if guard_url:
                guard=Beacon(guard_url)
                readiness(guard)
            genesis_data=beacon.get('/eth/v1/beacon/genesis')['data']
            chain_id=root(genesis_data['genesis_validators_root'])
            require(store.get('chain_id',chain_id)==chain_id,'chain_changed')
            spec=beacon.get('/eth/v1/config/spec')['data']
            slots=number(spec['SLOTS_PER_EPOCH'])
            seconds=(number(spec['SLOT_DURATION_MS'])/1000 if 'SLOT_DURATION_MS' in spec else number(spec['SECONDS_PER_SLOT']))
            genesis=number(genesis_data['genesis_time'])
            require(slots==32 and seconds==12,'unsupported_chain_timing')
            seconds=int(seconds)
            final_slot,_=readiness(beacon)
            require(0 <= time.time()-(genesis+final_slot*seconds) <= 8*slots*seconds,'stale_finality')
            latest=final_slot//slots-2
            current=int((time.time()-genesis)//(slots*seconds))
            # Capture attribution while the epoch is live, not minutes later when
            # finalized. The first sampled inventory for an epoch is immutable.
            current_key=f'ownership:{current}'
            if store.get(current_key) is None:
                with store.db:store.set(current_key,json.dumps(inventory(directory)))
            next_epoch=store.get('next_epoch')
            if next_epoch is None:
                # Start forward from the wall-clock epoch. No month-long backfill.
                next_epoch=current
                with store.db:store.set('next_epoch',next_epoch);store.set('chain_id',chain_id)
            next_epoch=int(next_epoch)
            # Long outages become explicit gaps. Only two recent epochs may catch up.
            # A very old database only needs gaps within its retained history.
            next_epoch=max(next_epoch,current-retention*86400//(slots*seconds))
            while next_epoch<latest-1:
                store.gap(next_epoch,genesis+next_epoch*slots*seconds,'catchup_limit')
                with store.db:store.db.execute('DELETE FROM meta WHERE key=?',(f'ownership:{next_epoch}',))
                next_epoch+=1
            if next_epoch<=latest:
                ownership_key=f'ownership:{next_epoch}'
                saved=store.get(ownership_key)
                if saved:
                    result=analyze(beacon,next_epoch,json.loads(saved),(slots,seconds,genesis))
                    store.save(result,discord_enabled=webhook is not None,policy=policy)
                else:
                    # No historical ownership observation: don't assign duties to
                    # today's client or send an alert against the wrong client.
                    store.gap(next_epoch,genesis+next_epoch*slots*seconds,'ownership_not_observed')
                with store.db:store.db.execute('DELETE FROM meta WHERE key=?',(ownership_key,))
                store.health('healthy',policy=policy,discord_enabled=webhook is not None)
            else:store.health('waiting','Awaiting a fully finalized inclusion window',policy=policy,discord_enabled=webhook is not None)
            with store.db:store.set('last_request_count',beacon.calls);store.set('last_response_bytes',beacon.bytes)
            store.prune(retention)
            with store.db:
                store.db.execute("DELETE FROM meta WHERE key LIKE 'ownership:%' AND CAST(substr(key,11) AS INTEGER) < ?",(latest-1,))
        except Exception as exc:
            # Do not leak node URLs, inventories or webhook tokens into logs.
            code=str(exc) if isinstance(exc,Unknown) else type(exc).__name__
            store.health('unknown',code,policy=policy,discord_enabled=webhook is not None)
            print('Collection paused: '+code,flush=True)
        drain(store,webhook)
        if once:return
        time.sleep(30)


def main():
    parser=argparse.ArgumentParser(description='Validator Watch finalized duty collector')
    parser.add_argument('command',choices=['serve','once','probe','demo','configure'],nargs='?',default='serve')
    args,extra=parser.parse_known_args()
    if args.command=='probe':
        import probe
        return probe.main(extra)
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
