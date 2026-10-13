"""Persistent alert transitions; callers commit state and the outbox together."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import time


@dataclass(frozen=True)
class Policy:
    per_thousand: Decimal = Decimal('10')
    epochs: int = 2
    health_seconds: int = 300

    @classmethod
    def from_env(cls, env):
        try:
            per_thousand = Decimal(env.get('ATTESTATION_ISSUE_PER_THOUSAND', '10'))
            epochs = int(env.get('ATTESTATION_ISSUE_EPOCHS', '2'))
            health = int(env.get('COLLECTOR_ALERT_AFTER_SECONDS', '300'))
            if not per_thousand.is_finite() or not 0 < per_thousand <= 1000 or not 1 <= epochs <= 32 or not 30 <= health <= 86400:
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ValueError('Invalid alert thresholds; check the documented ranges') from None
        return cls(per_thousand, epochs, health)


def read_state(store, key):
    return json.loads(store.get('alert:' + key, '{}'))


def write_state(store, key, state):
    store.set('alert:' + key, json.dumps(state))


def enqueue(store, ident, content, now):
    store.db.execute('INSERT OR IGNORE INTO outbox(id,content,created) VALUES(?,?,?)',
                     (ident, content + f'\nDetected <t:{int(now)}:F>\nIncident: {ident}', now))


def attestation_alerts(store, result, policy, enabled, now=None):
    now = time.time() if now is None else now
    epoch = result['epoch']
    counts = {client: [0, 0] for client in result['ownership'].values()}
    for row in result['attestations']:
        values = counts[row['client']]
        values[0] += 1
        values[1] += int(not all(row[k] for k in ('source', 'target', 'head')))
    opened, recovered = [], []
    for client, (expected, affected) in sorted(counts.items()):
        state = read_state(store, 'attestation:' + client)
        if state.get('last_epoch',-1) >= epoch:
            continue
        # A gap, zero-duty epoch or policy change cannot extend a streak or
        # establish recovery. Keep existing incidents open until checked again.
        fingerprint = f'per_thousand:{policy.per_thousand}:{policy.epochs}'
        if state.get('last_epoch') != epoch - 1 or state.get('policy') != fingerprint:
            state['bad'] = state['good'] = 0
        state.update(last_epoch=epoch, policy=fingerprint)
        if not expected:
            state['bad'] = state['good'] = 0
        else:
            bad = Decimal(affected * 1000) >= policy.per_thousand * expected
            state['bad'] = state.get('bad', 0) + 1 if bad else 0
            state['good'] = state.get('good', 0) + 1 if not bad else 0
            line = f'{client}: {affected}/{expected} affected ({Decimal(affected * 1000)/expected:.2f} per 1,000) in epoch {epoch}'
            if bad and state['bad'] >= policy.epochs and not state.get('active'):
                state.update(active=True, opened=epoch, notified=False)
            # Enabling Discord during an ongoing incident can announce it once.
            if bad and state['bad'] >= policy.epochs and state.get('active') and not state.get('notified') and enabled:
                opened.append(line)
                state['notified'] = True
            if not bad and state['good'] >= policy.epochs and state.get('active'):
                if state.get('notified') and enabled:
                    recovered.append(line)
                state.update(active=False, notified=False)
        write_state(store, 'attestation:' + client, state)
    for kind, lines in [('attestation', opened), ('attestation-recovery', recovered)]:
        if not lines:
            continue
        if kind == 'attestation':
            title = 'Repeated attestation issues' if len(lines) == 1 else 'Fleet alert: repeated attestation issues'
            explanation = f'At least {policy.per_thousand} per 1,000 affected in each of {policy.epochs} consecutive checked epochs.\n'
            explanation += 'Affected = missing any source/target/head credit; each duty counted once. Cause unknown.'
        else:
            title = 'Attestation issue rate recovered'
            explanation = f'Below {policy.per_thousand} per 1,000 for {policy.epochs} consecutive checked epochs.'
        # Keep a fleet burst inside Discord's message limit without cutting off
        # the incident identifier. Full client history remains in Grafana.
        visible = lines[:10]
        if len(lines) > 10:
            visible.append(f'And {len(lines)-10} more clients; see Grafana.')
        enqueue(store, f'{kind}:{epoch}', title+'\n'+'\n'.join(visible)+'\n'+explanation, now)


def health_alerts(store, healthy, detail, policy, enabled, now=None):
    now = time.time() if now is None else now
    state = read_state(store, 'collector')
    if healthy:
        if state.get('notified') and enabled:
            enqueue(store, f"collector-recovery:{state['since']}", 'Monitoring recovered\nBeacon collection is responding again; historical gaps remain visible.', now)
        state = {}
    else:
        if 'since' not in state:
            state['since'] = int(now)
        if now - state['since'] >= policy.health_seconds and not state.get('notified') and enabled:
            enqueue(store, f"collector:{state['since']}",
                    f'Monitoring unavailable\nCollection has been paused for at least {policy.health_seconds} seconds.\n'
                    f'Reason: {detail}\nValidator outcomes during the gap are unknown.', now)
            state['notified'] = True
    write_state(store, 'collector', state)
