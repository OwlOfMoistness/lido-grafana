"""Bounded finalized-chain analysis. Supports Deneb, Electra and Fulu only."""
import csv
import json
import math
import re
import time
import urllib.request
from pathlib import Path

KEY = re.compile(r'0x[0-9a-fA-F]{96}')
ROOT = re.compile(r'0x[0-9a-fA-F]{64}')
ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
FORKS = {'deneb', 'electra', 'fulu'}


class Unknown(Exception):
    """Unavailable or unsupported evidence is never a validator failure."""


def require(condition, reason='incomplete_evidence'):
    if not condition:
        raise Unknown(reason)


def number(v):
    require(isinstance(v, str) and v.isascii() and v.isdigit() and len(v) <= 20)
    n = int(v)
    require(n < 2**64)
    return n


def root(v):
    require(isinstance(v, str) and ROOT.fullmatch(v))
    return v.lower()


def inventory(directory):
    ownership = {}
    files = sorted(Path(directory).glob('*.csv'))
    require(bool(files), 'inventory_missing')
    for file in files:
        require(ID.fullmatch(file.stem) and not file.is_symlink() and file.stat().st_size <= 1_000_000, 'invalid_inventory')
        with file.open(encoding='utf-8-sig', newline='') as source:
            for row in csv.reader(source):
                if not row or all(not s.strip() for s in row):
                    continue
                require(len(row) == 1 and KEY.fullmatch(row[0].strip()), 'invalid_inventory')
                key = row[0].strip().lower()
                require(key not in ownership, 'duplicate_inventory_key')
                ownership[key] = file.stem
    require(0 < len(ownership) <= 2048, 'inventory_size')
    return ownership


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Beacon:
    def __init__(self, url, pause=1, timeout=10):
        from urllib.parse import urlsplit
        p = urlsplit(url)
        require(p.scheme in ('http', 'https') and p.hostname and not p.username and not p.password
                and not p.query and not p.fragment, 'invalid_beacon_url')
        self.url, self.pause, self.timeout = url.rstrip('/'), pause, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.reset()

    def reset(self):
        self.started, self.last, self.calls, self.bytes = time.monotonic(), 0, 0, 0

    def get(self, path, body=None):
        require(self.calls < 180 and time.monotonic() - self.started < 300, 'request_budget')
        time.sleep(max(0, self.pause - (time.monotonic() - self.last)))
        self.calls += 1
        req = urllib.request.Request(self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
            self.bytes += len(payload)
            require(len(payload) <= 4 * 1024 * 1024, 'response_limit')
            data = json.loads(payload)
            require(isinstance(data, dict) and 'data' in data)
            return data
        finally:
            self.last = time.monotonic()


def verified(response):
    require(response.get('execution_optimistic') is False, 'optimistic_or_unverified')
    return response['data']


def header(response):
    data = verified(response)
    require(data.get('canonical') is True)
    return number(data['header']['message']['slot']), root(data['root'])


def readiness(beacon):
    data = beacon.get('/eth/v1/node/syncing')['data']
    require(all(data.get(k) is False for k in ('is_syncing', 'is_optimistic', 'el_offline')), 'node_unready')
    return header(beacon.get('/eth/v1/beacon/headers/finalized'))


def bitlist(value):
    require(isinstance(value, str) and value.startswith('0x') and len(value) <= 65540)
    raw = bytes.fromhex(value[2:])
    require(raw and raw[-1] != 0)
    n = int.from_bytes(raw, 'little')
    size = n.bit_length() - 1  # SSZ terminator is not a participant.
    return n ^ (1 << size), size


def bitvector(value, size):
    require(isinstance(value, str) and value.startswith('0x'))
    raw = bytes.fromhex(value[2:])
    require(len(raw) == (size + 7) // 8)
    n = int.from_bytes(raw, 'little')
    require(n < 1 << size)
    return n


def flags(delay, target_matches, head_matches, slots=32):
    # Canonical, consensus-valid inclusion implies matching source. These are
    # participation flags, not reward amounts or evidence of network submission.
    require(delay >= 1)
    return (delay <= math.isqrt(slots), bool(target_matches), bool(target_matches and head_matches and delay == 1))


def analyze(beacon, epoch, ownership, timing):
    """Read a complete canonical window; return nothing on partial/ambiguous evidence."""
    slots, seconds, genesis = timing
    start, end = epoch * slots, (epoch + 2) * slots
    final_slot, final_root = readiness(beacon)
    require(final_slot >= end and final_slot - end <= 4 * slots, 'outside_recent_finality_window')
    # Walk actual parent roots, proving skipped slots instead of treating 404 as a miss.
    blocks = {}
    r, previous = final_root, final_slot + 1
    while True:
        response = beacon.get('/eth/v1/beacon/blinded_blocks/' + r)
        require(response.get('version', '').lower() in FORKS, 'unsupported_fork')
        message = verified(response)['message']
        slot = number(message['slot'])
        require(slot < previous and len(blocks) < 7 * slots, 'broken_parent_chain')
        previous = slot
        # Discard execution payload headers, signatures and other irrelevant fields.
        body = message['body']
        blocks[slot] = {'root': r, 'proposer': number(message['proposer_index']),
            'attestations': body['attestations'], 'sync': body['sync_aggregate']['sync_committee_bits'],
            'fork': response['version'].lower()}
        if slot <= start:
            break
        r = root(message['parent_root'])
    def at(slot):
        available = [s for s in blocks if s <= slot]
        require(bool(available), 'missing_root_history')
        return blocks[max(available)]['root']

    # Historical state is pinned to the checked epoch; no whole registry downloads.
    records = {}
    keys = list(ownership)
    for offset in range(0, len(keys), 128):
        batch = keys[offset:offset + 128]
        response = beacon.get(f'/eth/v1/beacon/states/{start}/validators', {'ids': batch})
        for row in verified(response):
            key = row['validator']['pubkey'].lower()
            require(key in batch and key not in records)
            records[key] = row
    active = {}
    for key, row in records.items():
        validator = row['validator']
        if number(validator['activation_epoch']) <= epoch < number(validator['exit_epoch']):
            idx = number(row['index'])
            require(idx not in active)
            active[idx] = key
    # Missing registry keys are displayed as inventory coverage, not invented duties.
    duties = {}
    indices = list(active)
    for offset in range(0, len(indices), 128):
        data = verified(beacon.get(f'/eth/v1/validator/duties/attester/{epoch}',
                                   [str(i) for i in indices[offset:offset + 128]]))
        for row in data:
            idx, slot = number(row['validator_index']), number(row['slot'])
            require(idx in active and idx not in duties and start <= slot < start + slots
                    and row['pubkey'].lower() == active[idx])
            duties[idx] = row
    require(set(duties) == set(active), 'incomplete_attester_duties')
    # Keep just lengths + owned positions. Electra combines multiple committees;
    # offsets depend on every selected committee, not just our validators.
    committees = {}
    for slot in sorted({number(d['slot']) for d in duties.values()}):
        rows = verified(beacon.get(f'/eth/v1/beacon/states/{start}/committees?epoch={epoch}&slot={slot}'))
        lengths, positions = {}, {}
        for row in rows:
            ci = number(row['index'])
            require(number(row['slot']) == slot and ci not in lengths)
            vals = [number(i) for i in row['validators']]
            require(len(vals) == len(set(vals)))
            lengths[ci] = len(vals)
            positions[ci] = [(i, pos) for pos, i in enumerate(vals) if i in active]
        expected = {number(d['committees_at_slot']) for d in duties.values() if number(d['slot']) == slot}
        require(len(expected) == 1 and set(lengths) == set(range(expected.pop())))
        for idx, duty in duties.items():
            if number(duty['slot']) == slot:
                ci = number(duty['committee_index']); pos = number(duty['validator_committee_index'])
                require(lengths[ci] == number(duty['committee_length']) and (idx, pos) in positions[ci])
        committees[slot] = lengths, positions
    earned, delays = {idx: [False]*3 for idx in active}, {}
    for inclusion_slot, block in sorted(blocks.items()):
        if not start < inclusion_slot < end:
            continue
        for att in block['attestations']:
            data = att['data']; slot = number(data['slot'])
            if slot not in committees or number(data['target']['epoch']) != epoch:
                continue
            lengths, positions = committees[slot]
            if block['fork'] in ('electra', 'fulu'):
                bits = bitvector(att['committee_bits'], 64)
                selected = [i for i in range(64) if bits & (1 << i)]
                require(number(data['index']) == 0)
            else:
                selected = [number(data['index'])]
            require(selected and all(i in lengths for i in selected))
            bits, size = bitlist(att['aggregation_bits'])
            require(size == sum(lengths[i] for i in selected), 'invalid_aggregation_length')
            credit = flags(inclusion_slot-slot, root(data['target']['root']) == at(start),
                           root(data['beacon_block_root']) == at(slot), slots)
            offset = 0
            for ci in selected:
                for idx, pos in positions[ci]:
                    if bits & (1 << (offset + pos)):
                        earned[idx] = [old or new for old, new in zip(earned[idx], credit)]
                        delays[idx] = min(delays.get(idx, 2**64), inclusion_slot-slot)
                offset += lengths[ci]
    rows = verified(beacon.get(f'/eth/v1/validator/duties/proposer/{epoch}'))
    proposers = {}
    for row in rows:
        slot = number(row['slot']); idx = number(row['validator_index'])
        require(start <= slot < start+slots and slot not in proposers)
        proposers[slot] = (idx, row['pubkey'].lower())
    require(set(proposers) == set(range(start, start+slots)), 'incomplete_proposer_duties')
    proposals = []
    for slot, (idx, key) in proposers.items():
        if key in ownership:
            require(key in records and number(records[key]['index']) == idx, 'proposer_registry_mismatch')
            if slot in blocks:
                require(blocks[slot]['proposer'] == idx, 'proposer_mismatch')
            proposals.append({'slot': slot, 'pubkey': key, 'client': ownership[key],
                'status': 'included' if slot in blocks else 'missed',
                'block_root': blocks[slot]['root'] if slot in blocks else ''})
    # Sync bits in block S describe committee signatures for S-1, using the
    # committee at S. Missing inclusion blocks are unknown, not blamed on a VC.
    sync = {key: [0, 0, 0] for key in ownership}  # expected / missed / unknown positions
    committee_rows = verified(beacon.get(f'/eth/v1/beacon/states/{start}/sync_committees'))
    members = [number(i) for i in committee_rows['validators']]
    require(len(members) == 512, 'unsupported_sync_committee_size')
    owned_indices = {number(row['index']): key for key, row in records.items()}
    for slot in range(start, start+slots):
        bits = bitvector(blocks[slot]['sync'], 512) if slot in blocks else None
        for pos, idx in enumerate(members):
            if idx in owned_indices:
                values = sync[owned_indices[idx]]; values[0] += 1
                if bits is None: values[2] += 1
                elif not bits & (1 << pos): values[1] += 1
    require(header(beacon.get(f'/eth/v1/beacon/headers/{final_slot}')) == (final_slot, final_root), 'anchor_changed')
    attestations = [{'pubkey': key, 'client': ownership[key], 'slot': number(duties[idx]['slot']),
                    'source': earned[idx][0], 'target': earned[idx][1], 'head': earned[idx][2],
                    'delay': delays.get(idx)} for idx, key in active.items()]
    return {'epoch': epoch, 'time': genesis+start*seconds, 'anchor': final_root,
            'attestations': attestations, 'proposals': proposals, 'sync': sync,
            'ownership': ownership, 'unknown_keys': len(keys)-len(records),
            'slots': slots, 'seconds': seconds, 'genesis': genesis}
