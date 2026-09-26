# Lido validator monitoring

## New hub/child installation

For the `.env`-selected hub/child stack, use the repository-root
[`compose.yml`](../compose.yml), [`.env.example`](../.env.example), and
[deployment guide](../deployment/README.md). The standalone/reuse files below
are earlier alternatives; do not start both deployments accidentally.

One Grafana dashboard for three Nimbus validator instances and their shared main
and fallback execution/consensus servers. Choose all instances or one instance
using the dashboard selectors. The main and fallback each have their own section.

**Already running Grafana and Prometheus? See [REUSE-EXISTING.md](REUSE-EXISTING.md).**
This guide describes a standalone installation and metric semantics. Choose one
installation approach to avoid duplicate monitoring services.

## Where to run it

The example uses `validator-1` as the monitoring hub. Check free RAM and disk
before deploying, then observe the monitoring overhead. Prometheus retains up to 30 days or a 10 GB storage retention limit,
whichever is reached first; WAL and other overhead need additional disk space.

A dedicated monitoring VM is also supported and keeps the dashboard available
if validator-1 goes down. In that case add SSH forwards for validator-1 and change
its two addresses in `targets.json`. Monitoring data is collected while your
laptop is disconnected. If the hub or its monitoring tunnels go down, collection
has gaps; this design has no remote buffering or redundant monitoring hub.

```text
validator-1 local metrics -------------------+
validator-2 metrics -- persistent SSH --------+
validator-3 metrics -- persistent SSH --------+--> Prometheus --> Grafana
main server metrics -- persistent SSH -------+       hub          hub
fallback metrics ----- persistent SSH -------+                     |
                                                         laptop SSH tunnel
```

This stack is for a **Linux hub**, using Docker host networking to reach
loopback-only SSH forwards. Grafana binds to `127.0.0.1:3200`, and Prometheus binds
to `127.0.0.1:9092`. The reuse guide illustrates an existing deployment on ports 3100/9091.
The stack has its own named volumes and does not migrate old Prometheus history.
Its first 24-hour view will only contain data collected since it started.

This directory does not change validator keys, signing, client images, or duty tunnels.
It does not require Rocket Pool, watchtower, Alertmanager, or `extra-scrape-jobs`.
No notifications are configured.

## Main/fallback behavior

Nimbus supports automatic fallback with beacon endpoints in priority order.
For example, when duty tunnels listen on the validator host loopback interface:

```yaml
- --beacon-node=http://127.0.0.1:5052
- --beacon-node=http://127.0.0.1:5552
- --beacon-node-mode=fallback
```

In this example, 5052 is the main beacon API and 5552 is the fallback beacon API.
Use the actual duty-tunnel addresses for each validator; do not change a working
validator configuration just to match this example.
These are duty API endpoints, not metrics endpoints. Monitoring uses separate
SSH forwards and never edits or restarts the existing duty tunnels.

Nimbus `validator_client_node_counts{status="good|viable|bad"}` reports how many
beacon connections are usable for all requests, some requests, or none. It does
**not** expose a per-endpoint active/fallback selector. The dashboard therefore
shows those counts, not a guessed "currently using main" indicator. Main/fallback
metrics scrape reachability is measured from the hub, not from each validator.
An exact per-request backend view would require additional client telemetry or
log processing.

## Port plan

All left-hand addresses are on the hub. Remote addresses are loopback on the
machine reached by SSH, not one of the validator-local 10.x duty tunnel IPs.
The ports below are a proposed allocation; check for existing listeners first.

| Machine | Component | Hub address | Remote metrics port |
| --- | --- | --- | --- |
| validator-1 (hub) | Nimbus validator | 127.0.0.1:8808 | local 8808 |
| validator-1 (hub) | node-exporter | 127.0.0.1:9103 | local 9103 |
| validator-2 | Nimbus validator | 127.0.0.1:28808 | 8808 |
| validator-2 | node-exporter | 127.0.0.1:29103 | 9103 |
| validator-3 | Nimbus validator | 127.0.0.1:38808 | 8808 |
| validator-3 | node-exporter | 127.0.0.1:39103 | 9103 |
| main | Nimbus beacon | 127.0.0.1:18008 | 8008 |
| main | Nethermind | 127.0.0.1:19105 | 9105 |
| main | node-exporter | 127.0.0.1:49103 | 9103 |
| fallback | Nimbus beacon | 127.0.0.1:28008 | 9100 |
| fallback | Geth | 127.0.0.1:29105 | 9105 |
| fallback | node-exporter | 127.0.0.1:59103 | 9103 |

The target examples use Nethermind for main and Geth for fallback execution.
Fallback execution sets `__metrics_path__` to `/debug/metrics/prometheus`; all
other targets use `/metrics`. Change or remove this override if using a different
execution client. Prometheus supports this per-target path override directly.
See [Geth metrics](https://geth.ethereum.org/docs/monitoring/metrics).

**Execution client support:** peer and head panels support Nethermind and Geth
(`p2p_peers` and `chain_head_block`), using one metric per scrape target and
hiding values when that target is down. Geth process RAM remains unavailable:
its supplied `system_memory_*` metrics are not verified equivalents of process
RSS/working set and are not substituted. An Up scrape alone does not establish
that all panels have data.

`targets.json` and `ssh/ssh_config.example` agree on this mapping. Keep the
`host`, `role`, and `component` labels unchanged unless also updating the dashboard
selectors. Every physical machine has a stable host label; `instance` retains the
actual scrape address. Collect each endpoint once, especially the shared backends.

## Complete the deployment details

Before starting, confirm:

1. The monitoring hub's SSH address; this example uses validator-1 as the hub.
2. SSH hostnames, login users, ports, key paths, and hub-to-server access for the
   other two validators and both backends. Edit the `REPLACE_WITH_...` values in
   a copy of `ssh/ssh_config.example`. All example SSH ports default to 22;
   replace the user/key placeholders and verify each server's actual SSH port.
3. Whether the fallback uses the same Nimbus/Nethermind metrics and ports as main.
4. Whether node-exporter is already available on each of the five machines.

No remote hosts are contacted by this repository. The supplied SSH configuration
is deliberately a template, not a working connection configuration.

## Host metrics

Reuse an existing node-exporter where it is already installed. Do not start a
second process on port 9103. On Linux hosts that need it, deploy just
`node-exporter.compose.yml` and run:

```sh
docker compose -f node-exporter.compose.yml up -d
```

This exporter binds to localhost and exposes host filesystems using a read-only
rootfs mount. The reuse overlay can add it to an existing exporter as well.
Validate filesystem labels before relying on its
disk panels. Cloud hosts may not expose CPU or disk temperature sensors; those
panels are intentionally absent.

## Persistent monitoring tunnels

On the hub, make an edited `ssh/ssh_config` from the example. Confirm the remote
hosts' SSH keys in the monitoring user's known_hosts through your normal SSH
login process. The service uses strict host-key checks and noninteractive login.
The local service user must own or be able to read its SSH key and known_hosts.

Copy the systemd template to an ignored local override and replace its
`REPLACE_WITH_LOCAL_USER` with the hub account that owns the SSH key. SSH `User`
is the remote login account and may differ. After configuring both templates:

```sh
cp ssh/lido-monitoring-tunnel@.service ssh/lido-monitoring-tunnel@.local.service
# Edit the local copy and replace REPLACE_WITH_LOCAL_USER before installing.
sudo install -d -m 755 /etc/lido-monitoring
sudo install -m 644 ssh/ssh_config /etc/lido-monitoring/ssh_config
sudo install -m 644 ssh/lido-monitoring-tunnel@.local.service /etc/systemd/system/lido-monitoring-tunnel@.service
sudo systemctl daemon-reload
sudo systemctl enable --now lido-monitoring-tunnel@lido-metrics-validator-2
sudo systemctl enable --now lido-monitoring-tunnel@lido-metrics-validator-3
sudo systemctl enable --now lido-monitoring-tunnel@lido-metrics-main
sudo systemctl enable --now lido-monitoring-tunnel@lido-metrics-fallback
```

Inspect failures with `journalctl -u lido-monitoring-tunnel@lido-metrics-main` (or
the other alias). A forwarded port listening only establishes the SSH listener;
use an actual `/metrics` request to check the remote service. Failed scrapes stay
visible as Unreachable instead of being silently removed from the dashboard.

## Start Grafana and Prometheus

From this `monitoring` directory on the Linux hub:

```sh
cp .env.example .env
# Edit .env and replace the example password before continuing.
chmod 600 .env
docker compose config --quiet
docker compose run --rm --no-deps --entrypoint /bin/promtool prometheus check config /etc/prometheus/prometheus.yml
docker compose up -d
```

The pinned versions are Grafana 13.2.2, Prometheus 3.14.0, and optional
node-exporter 1.12.1. These are a separate new installation, not an in-place
upgrade of your existing Grafana database. The Grafana datasource and dashboard
are provisioned from this directory. The admin password initializes an empty
Grafana volume; changing the environment later does not reset an existing password.

From your laptop, using the hub's actual SSH user, host and port:

```sh
ssh -N -L 127.0.0.1:3200:127.0.0.1:3200 YOUR_USER@YOUR_HUB
```

Then open http://localhost:3200, log in as `admin` with the configured password,
and select **Lido / Lido · Validator fleet**. Pick All to compare three validator
instances and both shared servers in the overview tables. Expand a machine’s
detail row below the activity charts when needed. Pick a single validator to
filter the view; the shared server selector remains independent. The default time
range is 24 hours and the accuracy window is 1 hour. Attestation charts match the
Rocket Pool rate calculation, scaled to submissions per 45 seconds. Proposal
charts show whole-number estimates over rolling five-minute windows.

## Metric meaning and remaining live checks

The overview starts with a horizontal strip of loaded validators, activity totals,
and metrics reporting. A shorter row of attestation, proposal, and accuracy charts
sits underneath, with extra width for accuracy. Below those charts, it presents all three validator instances
in one comparison table and main/fallback in a second table. It shows current metrics availability, loaded
keys, selected-period activity, process memory, host CPU/RAM, good beacon links,
EC/CL peers, and per-backend head accuracy. Main is listed before fallback.
Source/target/head accuracy histories and other detailed metrics remain in
expandable rows. Validator/server detail rows start expanded to avoid a
Grafana 9.5 scoping issue with initially collapsed repeated rows; they can be
collapsed after loading. Storage starts collapsed. Table joins preserve missing cells as —
rather than filling them with zero. Each target must have one stable host label.

The expanded detail layout pairs related values inside each card: CPU/RAM percentages,
execution/consensus process memory, execution/consensus peers, and attestation/
proposal totals. Main and fallback remain separate repeated sections. All metrics
from the earlier layout are retained; head slot/block numbers are shown in full.

### Activity interval in the supplied Rocket Pool dashboard

Panel 30 (`Validator Activity`) uses `rate(...[$__rate_interval]) * 45` for
both Nimbus submission counters. The factor 45 scales the per-second rate to
a per-45-second rate; it does not fix the averaging window at 45 seconds.
Grafana chooses `$__rate_interval` automatically. Attestations retain that formula. Proposal charts intentionally use
`round(sum(increase(beacon_blocks_sent_total[5m])))` instead, showing whole-number
estimates rather than fractional per-45-second rates. Overview aggregation is
by host. Expanded client details put attestations on the left axis and proposal
counts on the right, with both units labelled. The windows overlap; never sum
proposal chart points to calculate a total. Scrape-based counts are estimates.
Attestation axes start at zero with a soft maximum of 5, expanding for higher
values, to avoid exaggerating low-count fluctuations. Totals still cover the selected dashboard period. Missing
metrics stay missing rather than using RP’s final `or vector(0)` fallback.

### Accuracy source in the supplied Rocket Pool dashboard

Rocket Pool's Nimbus/Lodestar accuracy queries use `job="eth2"` and beacon-node
source, target, and head hit/miss counters. Accuracy therefore comes from the
beacon node, rather than the standalone Nimbus validator client. Rocket Pool's
multi-client panel also has `job="validator"` alternatives for Prysm. This
dashboard keeps the Nimbus beacon-node source and displays main/fallback
observations separately.

| Panel | Source / meaning |
| --- | --- |
| Active validators / Active balance | Optional CSV inventory collector: on-chain active count and actual consensus ETH balance, with freshness and completeness guards; see [inventory guide](../deployment/INVENTORY.md) |
| Loaded validators | Nimbus validator `validators`: attached keys, not active on-chain validators |
| Attestations/proposals | Validator `beacon_attestations_sent_total` / `beacon_blocks_sent_total`: successful submissions, not independently verified canonical inclusion |
| Selected-period totals | `increase(...[$__range])`, calculated separately per series before summing, so observed counter resets are handled |
| Activity | RP-compatible `sum(rate(counter[$__rate_interval])) * 45` (grouped by host in overview): average submissions per 45 seconds, not fixed buckets; Grafana adjusts the averaging window with resolution and scrape settings |
| Beacon connectivity | `validator_client_node_counts`, counts by good/viable/bad status |
| Attestation accuracy | Beacon `validator_monitor_prev_epoch_on_chain_{source,target,head}_attester_{hit,miss}_total`, aggregate `validator="total"` |
| Nimbus RAM | `process_resident_memory_bytes`, separately for each beacon and validator process |
| Nethermind RAM | `process_working_set_bytes`, from the supplied working dashboard |
| Peers | Nimbus `nbc_peers`; Nethermind `ethereum_peer_count`; Geth `p2p_peers`, verified from supplied samples |
| Head progression | Nimbus `beacon_head_slot`; Nethermind `nethermind_blockchain_height`; Geth `chain_head_block` |
| Hardware | Per-machine node-exporter metrics, with stable host labels |

**Accuracy coverage matters.** Each beacon node reports only its monitored
validator set. Automatic monitoring and fallback use can leave different sets on
main and fallback. The dashboard never sums the two servers' accuracy metrics
and does not attribute those aggregates to individual validator instances. If
`validator-monitor-details` is enabled instead of aggregate monitoring, the
`validator="total"` panels may have no data; inspect live output before adapting
them. A complete per-instance accuracy view needs a verified validator-to-instance
mapping and suitable per-validator observations or a separate on-chain collector.
Do not enable high-cardinality detailed monitoring merely to fill this panel.

Metrics endpoint reachability does not prove sync or readiness to sign. Head
progression, peers, successful duty activity, and Nimbus's connection status
should be considered together. This draft does not claim an authoritative
native per-instance on-chain active count or exact backend-selection indicator. The optional CSV collector supplies the active count and balance separately from Nimbus metrics.

The fleet loaded-key count is suppressed if a selected client or its count metric
is missing. Activity totals use available history, even when a client is now
offline; consult the scrape availability panel. That panel covers observed scrape
samples only, not missing history before startup or while Prometheus was stopped.
No missing activity metric is converted to a healthy-looking zero. The clients'
native process counters are not durable lifetime totals.

Nimbus metric definitions were checked against validator v26.7.0 and beacon
v26.2.0. Live metric names, labels, fallback client
versions, target reachability, and collector coverage still need verification.
After tunnels are configured, collect these on the hub (metrics only; no keys or
secrets are needed):

```sh
mkdir -p samples
curl -fsS --max-time 10 http://127.0.0.1:8808/metrics > samples/validator-1.txt
curl -fsS --max-time 10 http://127.0.0.1:28808/metrics > samples/validator-2.txt
curl -fsS --max-time 10 http://127.0.0.1:38808/metrics > samples/validator-3.txt
curl -fsS --max-time 10 http://127.0.0.1:18008/metrics > samples/main-beacon.txt
curl -fsS --max-time 10 http://127.0.0.1:19105/metrics > samples/main-execution.txt
curl -fsS --max-time 10 http://127.0.0.1:28008/metrics > samples/fallback-beacon.txt
curl -fsS --max-time 10 http://127.0.0.1:29105/debug/metrics/prometheus > samples/fallback-execution.txt
curl -fsS --max-time 10 http://127.0.0.1:9103/metrics > samples/validator-1-host.txt
```

For main/fallback accuracy, verify aggregate hit/miss counters exist and advance
over several epochs. Inspect all 12 fleet targets at Prometheus's `/targets` page
or `/api/v1/targets`; its UI can be reached through an additional temporary laptop
forward to hub port 9092. Routine dashboard use needs only the Grafana tunnel.

## Validation

Query behavior tests use synthetic series, including a validator counter reset,
an offline client with a stale count, a missing count metric, missing activity,
different accuracy observations on main and fallback, and zero attestation opportunities:

```sh
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD/tests:/tests:ro" prom/prometheus:v3.14.0 \
  test rules /tests/queries.test.yml
```

These validate query semantics, not your live servers. When changing a query,
update the corresponding test expression as well. Stop only this monitoring
stack with `docker compose down`; its named volumes are retained. Do not add `-v`
unless you intend to delete its monitoring history and Grafana database.

References: [Nimbus validator monitoring](https://nimbus.guide/validator-monitor.html),
[Prometheus increase](https://prometheus.io/docs/prometheus/latest/querying/functions/#increase),
[Nimbus v26.7.0 validator metrics](https://github.com/status-im/nimbus-eth2/blob/v26.7.0/beacon_chain/nimbus_validator_client.nim),
[Nimbus v26.7.0 validator count](https://github.com/status-im/nimbus-eth2/blob/v26.7.0/beacon_chain/validators/validator_pool.nim),
[Nimbus v26.2.0 accuracy metrics](https://github.com/status-im/nimbus-eth2/blob/v26.2.0/beacon_chain/validators/validator_monitor.nim).

## Latest presentation and storage changes

Attestation accuracy now also appears in the fleet overview, with independent
main/fallback source, target and head series. Solid lines identify main and
dashed lines fallback. Legends spell out Source, Target, and Head. This describes
the monitored validators, not network-wide
health. The backend accuracy chart is 5 of 24 grid columns (previously 12),
about 58% narrower. Activity totals and loaded counts use full numbers without
K/M abbreviations. Storage setup and live checks are in [STORAGE.md](STORAGE.md).

## Client names in the dashboard

VC 1, VC 2 and VC 3 identify the three Nimbus validator-client servers. The
selector and comparison table are labelled Validator clients. The underlying
Prometheus `host` labels remain `validator-1`, `validator-2` and `validator-3`;
only presentation changes, preserving existing queries and stored time series.
