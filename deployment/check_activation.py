#!/usr/bin/env python3
"""One-shot, read-only validator activation report. Python 3 standard library only."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
from http.client import HTTPException
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

FAR_FUTURE = 2**64 - 1
PUBKEY = re.compile(r"0x[0-9a-fA-F]{96}")
ROOT = re.compile(r"0x[0-9a-fA-F]{64}")
STATUSES = {
    'pending_initialized', 'pending_queued', 'active_ongoing', 'active_exiting',
    'active_slashed', 'exited_unslashed', 'exited_slashed', 'withdrawal_possible',
    'withdrawal_done',
}
FIELDS = ['pubkey', 'status', 'validator_index', 'balance_eth',
          'activation_eligibility_epoch', 'activation_epoch', 'activation_utc',
          'deposit_queue', 'deposit_positions']


def read_keys(paths):
    keys, seen = [], set()
    for path in paths:
        if path.stat().st_size > 10_000_000:
            raise ValueError('Each public-key file must be smaller than 10 MB')
        with path.open(encoding='utf-8-sig', newline='') as source:
            for line, row in enumerate(csv.reader(source, strict=True), 1):
                if not row or all(not item.strip() for item in row):
                    continue
                if len(row) != 1 or not PUBKEY.fullmatch(row[0].strip()):
                    raise ValueError(f'{path.name}:{line}: expected one 0x-prefixed public key, no header')
                key = row[0].strip().lower()
                if key in seen:
                    raise ValueError(f'{path.name}:{line}: duplicate public key')
                seen.add(key)
                keys.append(key)
    if not keys:
        raise ValueError('No public keys supplied')
    return keys


def request_json(url, body=None):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    # Contact only the requested beacon endpoint, without ambient proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode(),
        headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
    with opener.open(request, timeout=30) as response:
        limit = 16 * 1024 * 1024
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError('Response exceeds the 16 MiB download limit')
    return json.loads(payload)


def uint(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ValueError('Invalid beacon integer')
    number = int(value)
    if number > FAR_FUTURE:
        raise ValueError('Beacon integer exceeds uint64')
    return number


def verified_header(response):
    data = response['data']
    if response.get('execution_optimistic') is not False or data.get('canonical') is not True:
        raise ValueError('Beacon head is optimistic or noncanonical')
    slot = uint(data['header']['message']['slot'])
    root = data['root']
    if not ROOT.fullmatch(root):
        raise ValueError('Invalid beacon block root')
    return slot, root


def verified_records(response):
    if response.get('execution_optimistic') is not False or not isinstance(response.get('data'), list):
        raise ValueError('Invalid or optimistic beacon response')
    return response['data']


def report(endpoint, keys, include_queue=False, fetch=request_json):
    sync = fetch(endpoint + '/eth/v1/node/syncing')['data']
    if any(sync.get(field) is not False for field in ('is_syncing', 'is_optimistic', 'el_offline')):
        raise ValueError('Beacon node is syncing, optimistic, offline, or missing readiness fields')
    spec = fetch(endpoint + '/eth/v1/config/spec')['data']
    slots_per_epoch = uint(spec['SLOTS_PER_EPOCH'])
    # Support clients exposing either the old seconds or newer milliseconds constant.
    slot_ms = uint(spec['SLOT_DURATION_MS']) if 'SLOT_DURATION_MS' in spec else uint(spec['SECONDS_PER_SLOT']) * 1000
    if not slots_per_epoch or not slot_ms:
        raise ValueError('Invalid slot timing')
    genesis = uint(fetch(endpoint + '/eth/v1/beacon/genesis')['data']['genesis_time'])
    slot, root = verified_header(fetch(endpoint + '/eth/v1/beacon/headers/head'))
    state_url = endpoint + f'/eth/v1/beacon/states/{slot}'
    records = {}
    for start in range(0, len(keys), 128):
        batch = keys[start:start + 128]
        for record in verified_records(fetch(state_url + '/validators', {'ids': batch})):
            key = record['validator']['pubkey'].lower()
            if key not in batch or key in records or record['status'] not in STATUSES:
                raise ValueError('Unexpected validator record')
            records[key] = record

    positions = {key: [] for key in keys}
    queue_state, queue_total, warning = 'not_checked', None, None
    if include_queue:
        try:
            deposits = verified_records(fetch(state_url + '/pending_deposits'))
            queue_total = len(deposits)
            for position, deposit in enumerate(deposits, 1):
                key = deposit['pubkey'].lower()
                if not PUBKEY.fullmatch(key):
                    raise ValueError('Invalid pending-deposit public key')
                if key in positions:
                    positions[key].append(position)
            queue_state = 'checked'
        except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException) as exc:
            # Queue support is optional. Never equate failure with an empty queue.
            queue_state, queue_total = 'unavailable', None
            positions = {key: [] for key in keys}
            reason = f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            warning = f'Pending-deposit lookup unavailable ({reason}); no queue positions inferred.'

    # Pin reads to a slot for Nimbus compatibility, and reject a reorg across requests.
    if verified_header(fetch(endpoint + f'/eth/v1/beacon/headers/{slot}')) != (slot, root):
        raise ValueError('Beacon reorganized during lookup; rerun the command')
    rows = []
    for key in keys:
        row = dict.fromkeys(FIELDS, '')
        row.update(pubkey=key, status='not_in_registry', deposit_queue=queue_state)
        if queue_state == 'checked':
            row['deposit_queue'] = 'present' if positions[key] else 'absent'
            row['deposit_positions'] = ';'.join(map(str, positions[key]))
            if key not in records and positions[key]:
                row['status'] = 'pending_deposit'
        if key in records:
            record = records[key]
            validator = record['validator']
            row.update(status=record['status'], validator_index=uint(record['index']),
                       balance_eth=str(Decimal(uint(record['balance'])) / Decimal(10**9)))
            for field in ('activation_eligibility_epoch', 'activation_epoch'):
                epoch = uint(validator[field])
                row[field] = 'not_assigned' if epoch == FAR_FUTURE else epoch
            if row['activation_epoch'] != 'not_assigned':
                timestamp = genesis + row['activation_epoch'] * slots_per_epoch * slot_ms / 1000
                row['activation_utc'] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        rows.append(row)
    return rows, {'slot': slot, 'epoch': slot // slots_per_epoch,
                  'queue_entries': queue_total, 'warning': warning}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--beacon-url', required=True, help='Beacon REST base URL reachable from this host')
    parser.add_argument('--queue', action='store_true', help='Also download pending deposits once (16 MiB maximum)')
    parser.add_argument('files', nargs='+', type=Path, help='One public key per line, optional CSV quotes, no header')
    args = parser.parse_args(argv)
    endpoint = args.beacon_url.rstrip('/')
    parsed = urllib.parse.urlsplit(endpoint)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        parser.error('--beacon-url must be an HTTP(S) base URL without credentials, query, or fragment')
    try:
        rows, meta = report(endpoint, read_keys(args.files), args.queue)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, HTTPException, csv.Error) as exc:
        reason = f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else str(exc)
        print(f'Activation lookup failed: {reason}', file=sys.stderr)
        return 1
    print(f"Snapshot slot {meta['slot']}, epoch {meta['epoch']}; {len(rows)} public keys.", file=sys.stderr)
    if meta['queue_entries'] is not None:
        print(f"Pending deposit entries: {meta['queue_entries']}. Positions are 1-based, not activation ETAs.", file=sys.stderr)
    if meta['warning']:
        print(meta['warning'], file=sys.stderr)
    writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return 0


if __name__ == '__main__':
    sys.exit(main())
