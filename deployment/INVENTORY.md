# Active validator inventory

The optional hub collector connects public-key ownership to beacon state. It
requires no signing keys, Keymanager token, execution RPC or new child tunnels.
It exposes only aggregate metrics on `127.0.0.1:19200`; public keys never become
Prometheus labels. The image contains no operator inventory files.

## Setup and v0.3.0 upgrade

1. Download the v0.5.0 `compose.yml` and update `CONTROL_IMAGE` to
   `ghcr.io/owlofmoistness/lido-grafana:0.5.0` in the existing hub `.env`.
   Preserve the existing project name, data volumes, password and server settings.
2. Create one CSV for every configured validator ID. With local monitoring
   enabled, this includes the hub validator (normally `inventory/validator-1.csv`)
   and every child. With `LOCAL_VALIDATOR_ENABLED=false`, only child CSVs are
   required; there is no inventory entry for the hub itself.
   Each nonblank line contains one 98-character public key (`0x` plus 96 hex
   characters), without a header. Single-column quoted CSV is accepted. Empty
   files mean zero assigned keys; missing files are errors. Duplicates within a
   file or across clients reject the whole collection to prevent double counting.
3. Add `INVENTORY_ENABLED=true` and `INVENTORY_DIR=./inventory`. Set
   `MAIN_BEACON_API_PORT=5052` and `FALLBACK_BEACON_API_PORT=5552` to your existing
   beacon REST forwards on `BACKEND_ADDRESS`. These defaults are independent of
   metrics ports. `INVENTORY_PORT=19200` must be unused on the hub.
4. Make the directory and files readable inside the root, capability-dropped
   collector. Restrictive files owned by another UID may need ownership adjusted;
   do not loosen permissions on validator secrets. CSVs contain public keys only.
5. On the hub, run:

```bash
docker compose pull
docker compose up -d --force-recreate
```

Do not remove volumes. No changes to the validator processes or existing backend
SSH services are needed. Children do not run the collector and need no CSVs or
new ports. Existing operators can leave `INVENTORY_ENABLED=false` to retain
monitoring without the new values. The hub inventory service then stays idle.

## Accounting and consistency

Every 60 seconds, the collector rereads the files, checks beacon sync readiness,
gets a canonical non-optimistic head slot, and looks up the keys in batches of
128 using `POST /eth/v1/beacon/states/{slot}/validators`. All clients use the same
slot. A final header check rejects a block-root change during collection. Slot
lookup supports Nimbus versions that cannot resolve state-root IDs.

If the main node fails, the entire snapshot is retried using the fallback; results
from different nodes or slots are never mixed. Successful snapshots replace all
client metrics together. Failed reads clear the previous counts and balances.
Grafana also checks scrape health and suppresses snapshots older than 180 seconds,
including when a collection is taking too long.

Active means `active_ongoing`, `active_exiting` or `active_slashed`. It does not mean
the client is online or successfully attesting. Pending, exited and withdrawn
validators are excluded. Actual balances are summed in integer Gwei before ETH
conversion, including consensus rewards. Effective balance, execution rewards and
withdrawn ETH are not included. This is a current snapshot; the activity totals
continue to use the dashboard's selected time period.

Membership comes from the supplied inventory, not the loaded-key gauge. Files
for IDs absent from the current configuration are ignored. There is
no automatic test that your CSV matches the keys loaded by the client. Update the
files when keys move; use atomic file replacement when editing. Unknown keys
(valid public-key format but absent from beacon state) are reported separately
and excluded from active counts, so an inventory of pending deposits can show
zero active validators without being a collection failure.

## Check collection

After about a minute, on the hub:

```bash
docker compose exec tunnels python /app/control.py check
docker compose logs --tail=30 inventory
curl --noproxy '*' -fsS http://127.0.0.1:19200/metrics
```

Important per-client metrics:

| Metric | Meaning |
| --- | --- |
| `lido_inventory_success` | The latest collection succeeded (1) or failed (0) |
| `lido_inventory_timestamp_seconds` | Start time of the successful snapshot |
| `lido_inventory_keys` | Public keys supplied for this client |
| `lido_inventory_unknown_keys` | Supplied keys absent from the queried beacon state |
| `lido_inventory_active_validators` | Active validators in that inventory |
| `lido_inventory_active_balance_eth` | Actual consensus balance of those active validators |

`lido_inventory_fallback` is 1 when the fallback supplied the snapshot and 0
when the main node did. On failure, success is 0 and count/balance/time series
are omitted. The `check` command checks lookup freshness as well as scraping;
review unknown-key counts and compare inventory membership with your clients.

Keep inventories out of source control; the default `/inventory/` directory is
ignored. Custom directories should also be excluded. The beacon API contract is
specified in the [Ethereum Beacon APIs](https://ethereum.github.io/beacon-APIs/).
