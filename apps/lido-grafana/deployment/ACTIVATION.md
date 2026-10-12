# Check validator activation

Copy `check_activation.py` to a host with beacon REST access, or transfer the updated repository files. It
needs Python 3 and no third-party packages, root access, signing keys, or keymanager
credentials. It only reads public consensus data.

Use existing inventory CSVs, or a file containing one full `0x`-prefixed validator
public key per line (96 hex characters after `0x`, no header). Quoted single-column
CSVs and blank lines are accepted. Duplicate keys are rejected.

From the repository root on that host:

```sh
python3 deployment/check_activation.py \
  --beacon-url http://127.0.0.1:5052 \
  inventory/*.csv
```

Replace the example URL with the **beacon REST API reachable from that host**, such as
your existing `BACKEND_ADDRESS:MAIN_BEACON_API_PORT` tunnel. This is not a metrics,
validator keymanager, or execution RPC endpoint. No public port needs opening;
the example loopback port is not guaranteed to exist on your host.

Add `--queue` for pending-deposit positions:

```sh
python3 deployment/check_activation.py \
  --beacon-url http://127.0.0.1:5052 \
  --queue inventory/*.csv
```

For a standalone copy, use `python3 check_activation.py` and your public-key file.
Output is CSV; append `> activation.csv` to save it. Snapshot details and warnings
go to stderr. No real public-key inventories are included in the script.

## Read the results

- `pending_initialized`: registered, but activation eligibility is not yet assigned.
- `pending_queued`: eligible but not yet active. A finite `activation_epoch` and
  `activation_utc` show the scheduled activation time. `not_assigned` means the node
  has no scheduled epoch yet, so the script leaves the time blank.
- `active_ongoing`: active; the displayed activation time is historical.
- `pending_deposit`: found in the pending-deposit list but not yet in the validator
  registry. This is a script label, not a Beacon API validator status.
- `not_in_registry`: absent from this node's registry snapshot. This alone does not
  mean a deposit was never made or was lost.
- Other statuses, including exiting or withdrawn, are preserved from the API.

`deposit_positions` lists **1-based pending-deposit entry positions**, separated by
semicolons when one key has multiple deposits. Position 100 means 99 entries precede
that entry in this snapshot. It does not mean 99 validators must activate first:
the list also contains top-ups, amounts vary, and processing can defer entries.
An active validator can still have a queued top-up. No activation ETA is inferred
from queue position. The modern balance-based deposit queue must not be replaced
with a sorted list of `pending_queued` validators.

`deposit_queue` distinguishes `present`, `absent`, `not_checked`, and `unavailable`.
If the node lacks the pending-deposits endpoint, or the download exceeds the size
limit, activation status is still printed with `unavailable` for queue information.
An explorer acknowledging a deposit does not guarantee that it is already in this
node's pending-deposit list or validator registry.

## Load and consistency

The default command queries only your keys, in batches of 128. `--queue` downloads
the whole pending-deposit list once because the standard endpoint has no public-key
filter. Each response has a 16 MiB download cap; JSON parsing uses additional memory.
Start with a one-shot run on small VPS instances, rather than polling frequently.
A very large queue may be unavailable under this cap; the script does not fall back
to downloading a full beacon state or all validators.

The script checks node readiness, pins state requests to one head slot, rejects
optimistic data, and rechecks the canonical block root before printing results.
If a reorg occurs, rerun. This is a head snapshot, not a finalized guarantee. UTC
activation timestamps use the node's genesis and configured slot duration, assuming
that duration remains constant through the scheduled epoch. Node configuration and
the node's reported sync status are trusted.

References: [Beacon validator API](https://github.com/ethereum/beacon-APIs/blob/master/apis/beacon/states/validators.yaml),
[pending deposits API](https://github.com/ethereum/beacon-APIs/blob/master/apis/beacon/states/pending_deposits.yaml),
[Electra deposit processing](https://ethereum.github.io/consensus-specs/specs/electra/beacon-chain/#process_pending_deposits).
