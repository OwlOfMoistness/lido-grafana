#!/usr/bin/env python3
"""One-shot Beacon API compatibility probe, not a duty-failure detector."""
import argparse
from http.client import HTTPException
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = re.compile(r'0x[0-9a-fA-F]{64}')
LIMIT = 4 * 1024 * 1024


def endpoint(value):
    p = urllib.parse.urlsplit(value)
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username is not None
            or p.password is not None or p.query or p.fragment):
        raise argparse.ArgumentTypeError('Use an HTTP(S) beacon URL without credentials, query or fragment')
    return value.rstrip('/')


def uint(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or int(value) >= 2**64:
        raise ValueError('Invalid integer')
    return int(value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base, timeout=10, pause=1):
        self.base, self.timeout, self.pause = endpoint(base), timeout, pause
        self.rows, self.last = [], None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, name, path, body=None):
        # Serial requests, no automatic retry, no proxy or redirect to another node.
        if self.last is not None:
            time.sleep(max(0, self.pause - (time.monotonic() - self.last)))
        start = time.monotonic()
        row = {'check': name, 'status': 'unavailable'}
        self.rows.append(row)
        try:
            request = urllib.request.Request(self.base + path,
                data=None if body is None else json.dumps(body).encode(),
                headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
            with self.opener.open(request, timeout=self.timeout) as response:
                row['http_status'] = response.status
                payload = response.read(LIMIT + 1)
            row['bytes'] = len(payload)
            if len(payload) > LIMIT:
                row['reason'] = 'response_size_limit'
                return None
            data = json.loads(payload)
            if not isinstance(data, dict) or 'data' not in data:
                raise ValueError('Invalid envelope')
            row['status'] = 'responded'
            return data
        except urllib.error.HTTPError as exc:
            row.update(http_status=exc.code, reason='http_error')
        except (OSError, ValueError, UnicodeError, HTTPException):
            row['reason'] = 'network_or_invalid_json'
        finally:
            row['elapsed_ms'] = round((time.monotonic() - start) * 1000, 1)
            self.last = time.monotonic()
        return None


def header(value):
    if value is None or value.get('execution_optimistic') is not False:
        raise ValueError('Unverified head')
    data = value['data']
    if data.get('canonical') is not True or not ROOT.fullmatch(data['root']):
        raise ValueError('Noncanonical head')
    return uint(data['header']['message']['slot']), data['root']


def probe(client, validator_index=None, check_rewards=False):
    if check_rewards and validator_index is None:
        raise ValueError('Reward probing requires one validator index')
    result = {'kind': 'compatibility_probe', 'ready': False, 'checks': client.rows,
              'note': 'Endpoint responses do not prove complete duty coverage or acceptable node load.'}
    version = client.get('version', '/eth/v1/node/version')
    if version and isinstance(version.get('data'), dict):
        result['version'] = str(version['data'].get('version', 'unknown'))[:160]
    sync = client.get('readiness', '/eth/v1/node/syncing')
    if not sync or not isinstance(sync.get('data'), dict) or any(
            sync['data'].get(k) is not False for k in ('is_syncing', 'is_optimistic', 'el_offline')):
        result['stopped'] = 'Beacon is unready or readiness data is unavailable'
        return result
    spec = client.get('chain_timing', '/eth/v1/config/spec')
    head = client.get('canonical_head', '/eth/v1/beacon/headers/head')
    final = client.get('finalized_head', '/eth/v1/beacon/headers/finalized')
    try:
        slots = uint(spec['data']['SLOTS_PER_EPOCH'])
        if not 1 <= slots <= 1024:
            raise ValueError('Unsupported timing')
        head_slot, head_root = header(head)
        final_slot, _ = header(final)
        if not 0 <= head_slot - final_slot <= 4 * slots:
            raise ValueError('Finality stalled')
    except (ValueError, KeyError, TypeError, AttributeError):
        result['stopped'] = 'Unverified chain timing, canonical headers or recent finality'
        return result
    epoch = head_slot // slots
    result.update(head_slot=head_slot, finalized_slot=final_slot)
    # These are bounded recent shared reads, not a history scan or full state download.
    client.get('proposer_duties', f'/eth/v1/validator/duties/proposer/{epoch}')
    client.get('block_attestations', f'/eth/v2/beacon/blocks/{head_root}/attestations')
    client.get('sync_committee', f'/eth/v1/beacon/states/{head_slot}/sync_committees')
    if validator_index is not None:
        client.get('attester_duties', f'/eth/v1/validator/duties/attester/{epoch}', [str(validator_index)])
    if check_rewards:
        if validator_index is None:
            raise ValueError('Reward probing requires one validator index; empty lists can request all validators')
        if final_slot // slots < 2:
            result['rewards_skipped'] = 'Not enough finalized history'
        else:
            # Explicit opt-in: reward calculation can reconstruct state on the beacon node.
            client.get('attestation_rewards', f'/eth/v1/beacon/rewards/attestations/{final_slot // slots - 2}', [str(validator_index)])
    try:
        if header(client.get('head_recheck', f'/eth/v1/beacon/headers/{head_slot}')) != (head_slot, head_root):
            raise ValueError('Head changed')
        result['ready'] = True
    except (ValueError, KeyError, TypeError, AttributeError):
        result['stopped'] = 'Head could not be reconfirmed; repeat later'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--beacon-url', required=True, type=endpoint)
    parser.add_argument('--validator-index', type=lambda v: uint(v), default=None)
    parser.add_argument('--check-rewards', action='store_true', help='Opt in to one potentially expensive attestation reward request')
    args = parser.parse_args(argv)
    if args.check_rewards and args.validator_index is None:
        parser.error('--check-rewards requires --validator-index')
    result = probe(Client(args.beacon_url), args.validator_index, args.check_rewards)
    print(json.dumps(result, indent=2))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
