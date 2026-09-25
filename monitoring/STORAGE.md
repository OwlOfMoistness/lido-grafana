# Host storage: fix and live verification

A containerized node-exporter that mounts only `/proc` and `/sys`, without a
host-root mount and `--path.rootfs`, can inspect container paths instead of host
mounts. The old Rocket Pool panel also has a second-disk selector `device=""`,
which does not identify an ordinary disk.

## Included fix

The recommended `compose.existing.yml` overlay now adds `pid: host`, a read-only
`/:/host:ro,rslave` bind mount and `--path.rootfs=/host` to the existing exporter.
It preserves the exporter image, listener and textfile/proc/sys settings. The
optional `node-exporter.compose.yml` for other Linux machines already has the
host-root mount and path options. Reuse existing exporters; do not run two on the
same port. These changes are local deployment files, not applied to any server.

The storage query uses actual `host` and `mountpoint` labels, including `/` and
any separate data filesystem. It excludes temporary/container filesystems,
zero-size filesystems and offline exporters. It does not require a hard-coded
block-device name. A directory such as `/data` appears separately only if it
is a distinct filesystem mount; otherwise its usage is part of `/`.

## Verify on each Linux server after applying its exporter configuration

Check the real mount layout and available bytes:

```sh
findmnt -o TARGET,SOURCE,FSTYPE,SIZE,AVAIL
df -B1 --output=source,target,size,avail
curl -fsS http://127.0.0.1:9103/metrics > /tmp/lido-node-metrics.txt
```

In that saved metrics output inspect `node_filesystem_size_bytes`,
`node_filesystem_avail_bytes`, `node_filesystem_device_error`, and
`node_scrape_collector_success{collector="filesystem"}`. The collector should
succeed; device errors should be zero, and real root/data mounts should have
positive sizes. If there are errors, inspect exporter logs for mount-path or
permission errors. The mounted path must be traversable by the exporter's user;
do not resolve this by enabling a privileged container.

The graph uses `1 - available_bytes / size_bytes`, so reserved filesystem space
is counted as unavailable to ordinary users. Compare those same two byte fields
rather than assuming `df`'s rounded Use% has exactly the same denominator.

In the central Prometheus expression browser check:

```promql
up{job="node"}
node_scrape_collector_success{job="node",collector="filesystem"}
node_filesystem_device_error{job="node"}
node_filesystem_size_bytes{job="node",fstype!~"tmpfs|devtmpfs|overlay|squashfs|nsfs|tracefs"} > 0
```

Expect an exporter for each of the five machines, with the correct `host` label.
If local `/metrics` is correct but the central series is missing, inspect the
Prometheus Targets page, SSH forward and Docker bridge reachability. The hub's
existing Prometheus uses `host.docker.internal`; the remote forward ports are in
`targets-existing.json`. An Up scrape alone does not prove filesystem collection
succeeded. Grafana No data must never be interpreted as zero disk usage.

## What has been checked locally

The merged Compose configuration retains the existing exporter image and adds
the correct read-only rootfs mount, propagation and path flags. Prometheus query
tests cover root plus data mounts, temporary filesystems, zero capacity and an
offline exporter. Grafana renders the mock filesystems successfully. Actual mount
paths, permissions, exporter availability and tunnel endpoints still require
verification on the servers; mock screenshots cannot establish those facts.

Reference: [official node-exporter container documentation](https://github.com/prometheus/node_exporter#docker).
