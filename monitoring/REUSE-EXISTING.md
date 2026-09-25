# Reuse an existing monitoring stack

Use this overlay when the chosen hub already runs compatible Grafana,
Prometheus, and node-exporter services. Keep one monitoring stack on the hub;
do not also start the standalone `compose.yml` stack.

The base Compose file is user-provided and is not distributed here. The overlay
assumes services named `grafana`, `prometheus`, and `node-exporter`, the mount
paths shown in the overlay, Prometheus on port 9091, and node-exporter on 9103.
Review the merged Compose configuration against your deployment before applying.

`compose.existing.yml` extends the existing `grafana-docker-compose.yml`. It keeps
the current image versions, bind-mounted data, networking, old dashboard
provisioning, and Grafana port 3100. It adds a new dashboard/datasource and replaces
the scrape configuration with twelve labelled targets. The scrape interval is
30 seconds to keep collection overhead modest. The overlay also fixes host
filesystem visibility in the existing node-exporter (read-only rootfs mount,
rootfs path and host PID namespace). No validator container is changed. See
[STORAGE.md](STORAGE.md) for the cause and live verification steps.

## Docker and SSH addressing

Prometheus in a Docker bridge network cannot reach SSH forwards bound to host
loopback.
The base Compose file must map `host.docker.internal` to `host-gateway`
(using `extra_hosts: ["host.docker.internal:host-gateway"]` on Prometheus).
Use that mapping for the hub's local services and the new monitoring forwards.

1. On the hub, inspect `docker exec YOUR_PROMETHEUS_CONTAINER cat /etc/hosts` and find the IP
   mapped to `host.docker.internal`. Confirm it is a private Docker bridge address
   assigned to the hub (`ip addr`). Do not substitute a public interface address.
2. Copy `ssh/ssh_config.example` to `ssh/ssh_config`. Replace the SSH hostnames,
   users, key paths and ports for the remote machines.
3. On each **LocalForward** line, change only the first `127.0.0.1` (the local
   listener address) to that hub bridge IP. Keep the second `127.0.0.1` (the remote
   destination) unchanged. For example, using a documentation-only example IP (replace it with your actual bridge IP):

   ```text
   LocalForward 192.0.2.1:28808 127.0.0.1:8808
   ```

   This is a private hub interface, not a public listener. It is also not either
   validator instance's 10.x beacon API address. If host firewall rules filter
   Docker-to-host traffic, allow only the monitoring Docker network to these
   listeners. Actual `/metrics` requests from the Prometheus container must pass
   before relying on the dashboard.
4. Install the edited SSH configuration and four services as described in
   [README.md](README.md#persistent-monitoring-tunnels). The port allocation is
   unchanged. Use `targets-existing.json`, whose addresses are
   `host.docker.internal:<port>`, for this deployment.

Confirm that local validator metrics on port 8808 and node-exporter on port 9103
are reachable from the private Docker bridge. If either listener is bound only to
host loopback, configure private bridge reachability before deployment. For remote hosts, a loopback-only exporter
works because SSH connects to it on that remote host.

## Deploy the overlay

Copy this `monitoring` directory next to the **actual existing**
`grafana-docker-compose.yml` on the hub. Keep its other files and directories,
including Grafana's original provisioning files, dashboards, and data directories.
Back up the current scrape config and export the current dashboard first.

Run from that existing deployment directory. Determine the current Compose
project name instead of creating a second project:

```sh
MONITORING_PROJECT=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' YOUR_PROMETHEUS_CONTAINER)
```

Confirm that value is the expected nonempty project name, then:

```sh
docker compose -p "$MONITORING_PROJECT" -f grafana-docker-compose.yml -f monitoring/compose.existing.yml config --quiet
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD/monitoring/prometheus-existing.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$PWD/monitoring/targets-existing.json:/etc/prometheus/targets.json:ro" \
  prom/prometheus:v2.53.1 check config /etc/prometheus/prometheus.yml
docker compose -p "$MONITORING_PROJECT" -f grafana-docker-compose.yml -f monitoring/compose.existing.yml up -d --no-deps node-exporter prometheus grafana
```

The final command briefly recreates monitoring services, not the validator. Keep
using your existing laptop SSH tunnel and Grafana URL on port 3100. The new
dashboard is **Lido / Lido · Validator fleet**. Clear All and select one instance
for an individual view, or select All for the combined view.

Existing Prometheus history remains on disk. New labels create new time series,
so the new dashboard does not automatically attribute the old unlabelled history
to these hosts. There is no data backfill. The overlay adds a datasource without
changing your existing default datasource. It preserves old dashboards, although
old hardware queries that assumed one host should not be used for fleet totals.

Monitor `docker stats`, `free -h`, and `vmstat 1` after adding targets. In vmstat,
watch sustained swap-in/swap-out activity (`si`/`so`), rather than swap occupancy
alone. Growing memory pressure is a reason to increase RAM or move monitoring;
low CPU utilization does not establish RAM headroom. The new dashboard does not
enable per-validator detailed beacon metrics, which could add many series.

For rollback, use the same project and original Compose file without the overlay
to recreate only `node-exporter prometheus grafana`. Original configurations and data were not
overwritten. The dashboard/source metadata may remain in Grafana, but no extra
monitoring service or validator change needs undoing.

The same metric limitations and live checks documented in [README.md](README.md)
apply. Run sample requests to `host.docker.internal:<mapped-port>` from within the
Prometheus container, or to the verified bridge IP from the hub. The loopback
sample commands in the standalone guide are for that standalone topology only.
