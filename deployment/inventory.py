"""Read public-key inventories and export aggregate beacon-state metrics only."""
import csv
from decimal import Decimal
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.request

PUBKEY = re.compile(r"0x[0-9a-fA-F]{96}")
ROOT = re.compile(r"0x[0-9a-fA-F]{64}")
ACTIVE = {'active_ongoing', 'active_exiting', 'active_slashed'}
STATUSES = ACTIVE | {'pending_initialized', 'pending_queued', 'exited_unslashed',
                     'exited_slashed', 'withdrawal_possible', 'withdrawal_done'}


def read_inventory(directory, clients):
    """An empty file is an explicit empty inventory; a missing file is an error.

    Duplicate ownership is rejected across the entire fleet to avoid double counting.
    Public keys never appear in exported labels or error messages.
    """
    result, seen = {}, set()
    for ident in clients:
        path = directory / f'{ident}.csv'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 10_000_000:
            raise ValueError('Inventory must be a regular CSV smaller than 10 MB')
        keys = []
        with path.open(newline='', encoding='utf-8-sig') as source:
            for row in csv.reader(source, strict=True):
                if not row or all(not cell.strip() for cell in row):
                    continue
                if len(row) != 1 or not PUBKEY.fullmatch(row[0].strip()):
                    raise ValueError('Expected one 0x-prefixed validator public key per line, without a header')
                key = row[0].strip().lower()
                if key in seen:
                    raise ValueError('Duplicate public key in inventory')
                seen.add(key)
                keys.append(key)
        result[ident] = keys
    return result


def request_json(url, body=None):
    # Do not send private backend requests through ambient HTTP proxies or redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    with opener.open(request, timeout=10) as response:
        data = response.read(4_000_001)
    if len(data) > 4_000_000:
        raise ValueError('Beacon response exceeded size limit')
    return json.loads(data)


def snapshot(endpoint, inventories, fetch=request_json):
    sync = fetch(endpoint + '/eth/v1/node/syncing')['data']
    if any(sync.get(key) is not False for key in ('is_syncing', 'is_optimistic', 'el_offline')):
        raise ValueError('Beacon node is not ready')
    header = fetch(endpoint + '/eth/v1/beacon/headers/head')
    if header.get('execution_optimistic') is not False or header['data'].get('canonical') is not True:
        raise ValueError('Beacon head is not verified')
    # Nimbus cannot resolve state-root IDs in all versions. Pin to a slot and
    # recheck its canonical block root after the batches to detect a reorg.
    state = header['data']['header']['message']['slot']
    block_root = header['data']['root']
    if not isinstance(state, str) or not state.isascii() or not state.isdigit() or not ROOT.fullmatch(block_root):
        raise ValueError('Invalid beacon header')
    keys = [key for values in inventories.values() for key in values]
    records = {}
    for start in range(0, len(keys), 128):
        batch = keys[start:start + 128]
        response = fetch(endpoint + f'/eth/v1/beacon/states/{state}/validators', {'ids': batch})
        if response.get('execution_optimistic') is not False or not isinstance(response.get('data'), list):
            raise ValueError('Invalid or optimistic validator response')
        for record in response['data']:
            key = record['validator']['pubkey'].lower()
            balance = record['balance']
            status = record['status']
            if key not in batch or key in records or status not in STATUSES:
                raise ValueError('Unexpected validator record')
            if not isinstance(balance, str) or not balance.isascii() or not balance.isdigit() or int(balance) > 2**64-1:
                raise ValueError('Invalid validator balance')
            records[key] = (status, int(balance))
    confirmed = fetch(endpoint + f'/eth/v1/beacon/headers/{state}')
    if (confirmed.get('execution_optimistic') is not False
            or confirmed['data'].get('canonical') is not True
            or confirmed['data']['root'] != block_root):
        raise ValueError('Beacon reorganized during lookup')
    result = {}
    for ident, owned in inventories.items():
        active = [records[k][1] for k in owned if k in records and records[k][0] in ACTIVE]
        result[ident] = {'keys': len(owned), 'unknown': sum(k not in records for k in owned),
                         'active': len(active), 'balance': Decimal(sum(active)) / Decimal(10**9)}
    return result


def collect(directory, clients, endpoints, fetch=request_json):
    inventories = read_inventory(directory, clients)
    for index, endpoint in enumerate(endpoints):
        try:
            return snapshot(endpoint, inventories, fetch), index
        except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException, csv.Error):
            continue
    raise ValueError('No healthy beacon API returned a complete snapshot')


def metrics(clients, result=None, timestamp=0, backend=0, enabled=True):
    descriptions = {
        'enabled': 'Whether CSV inventory collection is enabled.',
        'fallback': 'Whether the most recent successful snapshot used the fallback beacon.',
        'success': 'Whether the latest inventory lookup succeeded.',
        'timestamp_seconds': 'Unix start time of the successful inventory snapshot.',
        'keys': 'Public keys supplied for this validator client.',
        'unknown_keys': 'Supplied keys not present in beacon state.',
        'active_validators': 'On-chain active validators in the supplied inventory.',
        'active_balance_eth': 'Actual consensus ETH balance of active inventory validators.',
    }
    lines = []
    for name, description in descriptions.items():
        lines += [f'# HELP lido_inventory_{name} {description}', f'# TYPE lido_inventory_{name} gauge']
    lines += [f'lido_inventory_enabled {int(enabled)}', f'lido_inventory_fallback {backend}']
    for ident in clients:
        label = '{host="' + ident + '"}'
        ok = result is not None and ident in result
        lines.append(f'lido_inventory_success{label} {int(ok)}')
        if ok:
            row = result[ident]
            for name, value in [('timestamp_seconds', timestamp), ('keys', row['keys']),
                                ('unknown_keys', row['unknown']), ('active_validators', row['active']),
                                ('active_balance_eth', row['balance'])]:
                lines.append(f'lido_inventory_{name}{label} {value}')
    return ('\n'.join(lines) + '\n').encode()



# A separate, lightweight duty poll never blocks balance collection or contacts VCs.
PROPOSAL_POLL_SECONDS = 30
PROPOSAL_MAX_AGE = 90


def uint(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or int(value) >= 2**64:
        raise ValueError('Invalid unsigned integer in beacon response')
    return int(value)


def proposal_snapshot(endpoint, inventories, fetch=request_json, now=time.time, parameters=None):
    parameters = parameters if parameters is not None else {}
    if endpoint not in parameters:
        genesis = uint(fetch(endpoint + '/eth/v1/beacon/genesis')['data']['genesis_time'])
        spec = fetch(endpoint + '/eth/v1/config/spec')['data']
        seconds = (uint(spec['SLOT_DURATION_MS']) / 1000 if 'SLOT_DURATION_MS' in spec
                   else uint(spec['SECONDS_PER_SLOT']))
        slots = uint(spec['SLOTS_PER_EPOCH'])
        if not genesis or not 1 <= seconds <= 60 or not 1 <= slots <= 1024:
            raise ValueError('Unsupported chain timing')
        parameters[endpoint] = (genesis, seconds, slots)
    genesis, seconds, slots = parameters[endpoint]
    started = now()
    current_slot = int((started - genesis) // seconds)
    if current_slot < 0:
        raise ValueError('Chain has not started')
    epoch = current_slot // slots
    sync = fetch(endpoint + '/eth/v1/node/syncing')['data']
    if any(sync.get(k) is not False for k in ('is_syncing', 'is_optimistic', 'el_offline')):
        raise ValueError('Beacon node is not ready')
    head = fetch(endpoint + '/eth/v1/beacon/headers/head')
    root = head['data']['root']
    head_slot = uint(head['data']['header']['message']['slot'])
    if (head.get('execution_optimistic') is not False or head['data'].get('canonical') is not True
            or not ROOT.fullmatch(root) or not 0 <= current_slot - head_slot <= 2):
        raise ValueError('Beacon head is stale or unverified')
    owners = {key: ident for ident, keys in inventories.items() for key in keys}
    duties = {ident: [] for ident in inventories}
    for requested in (epoch, epoch + 1):
        response = fetch(endpoint + f'/eth/v1/validator/duties/proposer/{requested}')
        if (response.get('execution_optimistic') is not False
                or not ROOT.fullmatch(response['dependent_root']) or not isinstance(response['data'], list)):
            raise ValueError('Unverified proposer schedule')
        seen = set()
        for duty in response['data']:
            slot = uint(duty['slot'])
            uint(duty['validator_index'])
            key = duty['pubkey'].lower()
            if not PUBKEY.fullmatch(key) or slot // slots != requested or slot in seen:
                raise ValueError('Invalid proposer duty')
            seen.add(slot)
            if key in owners:
                duties[owners[key]].append((slot, genesis + slot * seconds))
        # Never turn an empty/partial API response into a reassuring zero.
        expected = set(range(requested * slots, (requested + 1) * slots)) - {0}
        if seen != expected:
            raise ValueError('Incomplete proposer schedule')
    confirmed = fetch(endpoint + f'/eth/v1/beacon/headers/{head_slot}')
    if (confirmed.get('execution_optimistic') is not False
            or confirmed['data'].get('canonical') is not True or confirmed['data']['root'] != root):
        raise ValueError('Beacon reorganized during duty lookup')
    boundary = genesis + (epoch + 1) * slots * seconds
    if not started <= now() < min(boundary, started + PROPOSAL_MAX_AGE):
        raise ValueError('Schedule expired during lookup')
    return dict(duties=duties, timestamp=started, epoch=epoch, seconds=seconds,
                boundary=boundary, coverage_end=boundary + slots * seconds)


def collect_proposals(directory, clients, endpoints, fetch=request_json, now=time.time, parameters=None):
    inventories = read_inventory(directory, clients)
    for backend, endpoint in enumerate(endpoints):
        try:
            result = proposal_snapshot(endpoint, inventories, fetch, now, parameters)
            result['fallback'] = backend
            return result
        except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException):
            continue
    raise ValueError('No healthy beacon returned both epochs of proposer duties')


def proposal_metrics(clients, result=None, now=None):
    now = time.time() if now is None else now
    descriptions = {
        'success': 'Complete fresh current and next epoch proposer schedule available.',
        'timestamp_seconds': 'Start time of the duty lookup.',
        'valid_until_seconds': 'Schedule expires at this epoch boundary or freshness deadline.',
        'coverage_end_seconds': 'End of the next epoch covered by this snapshot.',
        'upcoming': 'Remaining proposals in the current and next epochs, including the current slot.',
    }
    lines = []
    for name, description in descriptions.items():
        lines += [f'# HELP lido_proposals_{name} {description}', f'# TYPE lido_proposals_{name} gauge']
    valid_until = min(result['boundary'], result['timestamp'] + PROPOSAL_MAX_AGE) if result else 0
    ok = result is not None and result['timestamp'] <= now < valid_until
    for ident in clients:
        label = '{host="' + ident + '"}'
        lines.append(f'lido_proposals_success{label} {int(ok)}')
        if ok:
            # Keep a proposal counted until its entire slot has passed.
            count = sum(start + result['seconds'] > now for _, start in result['duties'][ident])
            for name, value in [('timestamp_seconds', result['timestamp']), ('valid_until_seconds', valid_until),
                                ('coverage_end_seconds', result['coverage_end']), ('upcoming', count)]:
                lines.append(f'lido_proposals_{name}{label} {value}')
    return ('\n'.join(lines) + '\n').encode()

def serve(config):
    clients = [ident for ident, _name in config['clients']]
    enabled = config['inventory_enabled']
    directory = Path(os.environ.get('INVENTORY_DIRECTORY', '/inventory'))
    payload = metrics(clients, enabled=enabled)
    proposal_result = None

    def poll():
        nonlocal payload
        while True:
            started = time.time()
            try:
                result, backend = collect(directory, clients, config['beacon_apis'])
                # Timestamp the start, not the end, so slow batches cannot mask stale data.
                payload = metrics(clients, result, started, backend)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException, csv.Error):
                payload = metrics(clients)
                print('Inventory lookup failed: check CSV files and beacon REST access; balances withheld.', flush=True)
            time.sleep(60)

    def poll_proposals():
        nonlocal proposal_result
        parameters = {}
        while True:
            try:
                proposal_result = collect_proposals(directory, clients, config['beacon_apis'], parameters=parameters)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException, csv.Error):
                proposal_result = None
                print('Proposal schedule unavailable; upcoming proposals withheld.', flush=True)
            # Wake at epoch boundaries too: an old window cannot stand in for a new one.
            delay = PROPOSAL_POLL_SECONDS
            if proposal_result is not None:
                delay = max(1, min(delay, proposal_result['boundary'] - time.time()))
            time.sleep(delay)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/metrics':
                self.send_error(404)
                return
            body = payload + proposal_metrics(clients, proposal_result)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', config['inventory_port']), Handler)
    if enabled:
        threading.Thread(target=poll, daemon=True).start()
        threading.Thread(target=poll_proposals, daemon=True).start()
    server.serve_forever()
