# Upcoming block proposals

Implemented in source; not included in the published v0.5.0 image. A new hub image
and bundled dashboard are required. Child deployments do not change.

With `INVENTORY_ENABLED=true`, the existing inventory service also reads proposer
duties. It uses the same headerless public-key CSVs and main/fallback beacon REST
endpoints. No new keys, environment variables, ports, child requests or containers
are needed. CSV membership must match the keys assigned to each validator client.

The fleet client table adds **Upcoming · now + next** after historical Proposals.
The per-client overview adds **Upcoming** between Activity and VC RAM.
The count covers remaining slots in the current epoch, including the slot already
in progress, and the next epoch. Completed proposal totals retain the dashboard's
selected history period. Upcoming values use instant queries at the dashboard's
end time: use a range ending **now**, refresh, and wait for the refresh to complete
before consulting this indicator for maintenance. A historical time range shows
a historical schedule, not a current reboot decision.

- **0**: a complete, fresh schedule contains no remaining assigned proposals in
  the covered window for that client's supplied keys.
- **1 or more** (orange): assigned proposals remain in the covered window.
- **Unknown**: disabled/missing inventory, collection/scrape failure, stale data,
  an unverified or incomplete response, or a window that has expired.

This is a scheduling aid, not a reboot safety guarantee. Attestation and sync
committee duties still apply, and reorganizations can change proposer assignments.
A browser that stops refreshing can keep showing its previous rendered value.

## Collection and overhead

A separate thread checks the shared beacon about every 30 seconds and at the next
epoch boundary. After caching genesis and chain timing, a successful poll uses
five small GET requests: syncing, head header, current-epoch duties, next-epoch
duties, and a final canonical-header check. Requests are shared across the whole
fleet. Balances keep their independent 60-second collection loop.

The collector rejects syncing/optimistic/offline nodes, heads more than two slots
behind the wall clock, malformed or duplicate slots, missing assignments, a
changed canonical head root, and lookups spanning an epoch boundary. Both epochs
must succeed on one backend; a failure retries the complete window on the fallback.
If neither works, all upcoming counts are withheld. Failure of the schedule poll
does not remove otherwise valid balance metrics.

Schedules expire after 90 seconds or at the next epoch boundary, whichever comes
first. Prometheus queries check that deadline and collector scrape health. A
proposal stays counted until its full slot has passed; normal scrape/dashboard
refresh delays may keep a completed proposal visible a little longer.

Nimbus v26.2.0's v1 proposer-duty endpoint supports requests through the next
epoch. If another client/version cannot return a complete next epoch, the status
is Unknown; it never silently reduces the coverage to the current epoch.

## Metrics

All metrics use only a `host` label, never a public key, index or slot label:

| Metric | Meaning |
| --- | --- |
| `lido_proposals_success` | Complete fresh schedule available (1), otherwise 0 |
| `lido_proposals_upcoming` | Remaining assigned proposals in the two-epoch window |
| `lido_proposals_timestamp_seconds` | Start time of the successful lookup |
| `lido_proposals_valid_until_seconds` | Snapshot freshness/epoch expiry deadline |
| `lido_proposals_coverage_end_seconds` | End of the next epoch covered by the snapshot |

The existing `control.py check` command checks scrape health and balance snapshot
freshness. Inspect these proposal metrics separately if the new panels say Unknown.

References: [Beacon proposer duties](https://ethereum.github.io/beacon-APIs/#/Validator/getProposerDuties),
[Nimbus v26.2.0 implementation](https://github.com/status-im/nimbus-eth2/blob/v26.2.0/beacon_chain/rpc/rest_validator_api.nim).
