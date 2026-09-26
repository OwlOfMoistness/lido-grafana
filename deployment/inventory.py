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


def serve(config):
    clients = [config['own_id']] + [c['id'] for c in config['children']]
    enabled = config['inventory_enabled']
    directory = Path(os.environ.get('INVENTORY_DIRECTORY', '/inventory'))
    payload = metrics(clients, enabled=enabled)

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

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/metrics':
                self.send_error(404)
                return
            body = payload  # Atomic immutable snapshot; no partially updated fleet.
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
    server.serve_forever()
