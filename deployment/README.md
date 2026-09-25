# One hub, multiple validator-client servers

This is the primary deployment for a new installation. Run the root
`compose.yml` on each Linux server; `.env` selects the role. Requires Docker
Engine and Docker Compose 2.20+ (Compose `include` support).

| Server | HUB | VC_ID | Starts |
| --- | --- | --- | --- |
| the hub | true | validator-1 | Grafana, Prometheus, SSH forwards, host exporter |
| child 2 | false | validator-2 | Host exporter |
| child 3 | false | validator-3 | Host exporter |

Nimbus validators continue running in their existing stacks. Main/fallback
systemd tunnels continue running unchanged. The hub scrapes its own validator
and exporter locally, the two children through container-managed SSH forwards,
and the shared backends through its existing host-level tunnel listeners.
Main and fallback are collected once, not once through each VC server.

The custom **control image** packages the dashboard, provisioning generator,
SSH client/supervisor and post-start checker. Grafana, Prometheus and node-exporter
use their upstream images as separate containers. The GitHub workflow publishes
the control image to GHCR on version-tag pushes; `.env.example` selects `:1.0.0`.
It is available only after pushing `v1.0.0` and its workflow succeeding. For local builds,
set `CONTROL_IMAGE=lido-fleet-control:local` and `CONTROL_PULL_POLICY=build`.

## Populate the environment files first

Copy the same repository to each machine. In the repository root:

```sh
cp .env.example .env
chmod 600 .env
```

Edit `.env` on each machine. Use `HUB=true` only on the hub and `HUB=false`
on the two children. Use lowercase literal values. The children only need
`HUB` and `NODE_EXPORTER_PORT` for startup; VC_ID/VC_NAME document their
identities, which must match the hub's CHILDREN list.

On the hub, fill in:

- `VC_ID`, `VC_NAME` and `VC_METRICS_ADDRESS` (its existing validator metrics).
- `BACKEND_ADDRESS`: the private address to which the hub's existing main and
  fallback SSH forwards bind. Use the address from its working service files.
- `CHILDREN`: real SSH DNS/IP, SSH port and user for child 2 and child 3.
  Their existing validator metrics are normally `127.0.0.1:8808` on each child;
  confirm that endpoint on each child. `node_port` defaults to the new `19103`
  exporter, not the existing `9103` exporter.
- `SSH_KEY_FILE` and `SSH_KNOWN_HOSTS_FILE`: absolute paths to existing files
  on the hub. Ensure key permissions allow unattended OpenSSH use. Host keys
  must be verified for the exact host names and nondefault ports used in CHILDREN.
  The container does not automatically trust new keys, run a remote shell, or
  forward the SSH agent. It uses one shared identity for the child connections.
- A strong `GRAFANA_ADMIN_PASSWORD`. In `.env`, single-quote passwords containing
  `$` or `#` to preserve them literally. This initializes new Grafana storage;
  later password changes are made through Grafana.

CHILDREN is a single-quoted JSON array. Add another entry to monitor more VCs;
the generated dashboard selector, chart/table labels and repeated client overview
sections include them automatically. IDs must be unique
`validator-N` values. Give each entry a distinct `local_port`; the next port is
also reserved for its exporter. Keep these assignments stable to preserve
time-series identity. Addresses support IPv4 and DNS names. Arbitrary SSH config
aliases and passphrase/agent-based keys are not supported by this initial version.

### Custom client names

Set the hub's `VC_NAME` to a display name such as `Apple`. In the hub's CHILDREN
list, set each child's `name`, for example `Bananas` or `Oranges`. These names
appear in the client selector, repeated section headings, comparison tables,
activity legends and host-storage legends. Names support letters, numbers,
spaces, dots, underscores and hyphens, up to 80 characters, beginning with a
letter or number.

The hub is the source of display names: a child's own `.env` does not advertise
its name to the hub. Edit that child's `name` in the hub's list instead. Keep its
`id` and endpoint assignments unchanged; changing only the name does not change
Prometheus series identity or discard history. Recreate the monitoring stack
and refresh Grafana after changing names, just as for membership changes below.

### Fleet size follows the configured clients

There is no separate client-count setting: **total clients = one hub + the number
of entries in CHILDREN**. One entry means two client sections; four entries mean
five sections. An empty list means only the hub. No unused client placeholders
are generated. With “All” selected in Grafana, it repeats the overview for every
configured client; selecting a subset shows only those clients.

To add a client, start its child deployment and add its SSH/metrics entry to the
hub's CHILDREN list, using two unused local forwarding ports. To remove one,
remove its entry. Recreate the hub's monitoring stack with
`docker compose up -d --force-recreate`, then reload Grafana and select “All”.
No dashboard editing or new image build is needed just to change fleet size.
An offline client that remains configured still appears as unavailable; it is
not silently removed. This is configured membership, not automatic discovery.

The bare dashboard JSON contains three example clients for manual imports. The
hub image replaces those examples using its configuration during provisioning.
Larger fleets still need sufficient hub resources; the layout has no three-client
limit. Shared main/fallback sections remain independent of the VC count.

No SSH private key is placed in `.env` or baked into an image. The root
`.dockerignore` allowlists only the control code and dashboard for image builds.
Do not copy `private/` to a public repository or registry.

## Start the children, then the hub

On the children first, and then the hub:

```sh
docker compose config --quiet
docker compose up -d
docker compose ps -a
```

No profile flag is needed. A successful `configure` container exits with code 0;
that is expected. The hub's long-running containers remain running. Invalid
configuration stops dependent services from starting.

New loopback-only listeners are `19103` (exporter on every server), `9092`
(Prometheus on hub), `3200` (Grafana on hub), and `20000`–`20003` (child forwards
on hub with the example configuration). Check these are free before starting.
The new stack uses separate named volumes and does not reuse old Grafana data.
It may coexist temporarily with the older stack; assess RAM/disk before keeping
both. A 30s scrape interval and 7d/2GB retention are starting values, not a RAM cap
or a guarantee of fitting a 1GB server. Monitor memory and swapping during rollout.

No new public metrics ports are required: SSH reaches child loopback listeners.
The existing SSH ports must allow connections from the hub. Existing backend
forwards use their existing private bind address. The stack uses Linux host
networking to reach them directly; it adds no Docker-published public ports.

## Verify after startup

The previous tunnel script established host access to the shared backends.
Wait at least two scrape intervals after starting the hub, then **on the hub**:

```sh
docker compose exec tunnels python /app/control.py check
```

This reads the running Prometheus instance's target status and verifies the
expected host/component/job labels, addresses and paths. For three VCs there
should be **12 fleet scrape targets**: three validators, three VC exporters,
and six shared-backend endpoints. It also checks Grafana's database health.
Prometheus scrapes itself separately. A failing SSH child cannot silently
remove its targets; it remains visible as a down scrape and SSH retries.

If a check fails:

```sh
docker compose logs --tail=100 configure tunnels prometheus grafana
```

Open Grafana via your laptop's usual SSH tunnel to the hub, forwarding laptop
port 3200 to the hub `127.0.0.1:3200`. Log in as `admin` with the configured
password. Verify the fleet count and individual server panels using real data.
All-up scraping does not prove attestation accuracy coverage, correct filesystem
measurements, or successful validator duties. The existing fallback
exporter still needs the storage verification in `monitoring/STORAGE.md`;
this stack only supplies new exporters on the VC machines where it is started.

For later configuration/image updates, force recreation so Prometheus and
Grafana reload the generated provisioning as well as the tunnel settings:

```sh
docker compose up -d --force-recreate
```

Do not switch an already-running hub to child mode without removing its old
hub containers (`docker compose down` while the old role is still selected).
Ordinary `up` leaves orphan services behind. Named volumes persist unless
explicitly removed. This project does not manage or stop the validator stack.

## Images from GitHub Container Registry

The workflow in `.github/workflows/container.yml` validates the public files,
unit tests, both Compose roles, a real image build, generated configuration and
dashboard queries before publication. It publishes `linux/amd64` and
`linux/arm64` images using the repository's automatic `GITHUB_TOKEN`. You do not
need to give Actions any validator credentials, private keys or deployment `.env`.

| Git event | Image tags |
| --- | --- |
| Push to main, or any manual run | Validation/build only; no publication |
| Push a release tag such as v1.0.0 | `1.0.0`, `1.0`, `sha-<full commit>`, `latest` |
| Pull request | Validation/build only; no publication |

The image name is derived from the repository, lowercased. For this repository:

```dotenv
CONTROL_IMAGE=ghcr.io/owlofmoistness/lido-grafana:1.0.0
CONTROL_PULL_POLICY=always
```

The default `always` policy fetches the selected tag on `docker compose up -d`.
Release tags must use `vMAJOR.MINOR.PATCH`, such as `v1.0.0` or `v1.0.1`.
Use the exact version or digest for controlled upgrades rather than `latest`.
No `main` image tag is published. No repository secrets need to be created:
GitHub supplies `GITHUB_TOKEN`, and the publishing job requests `packages: write`.
Changes to generated configuration still require the force-recreate command
above when updating an already running deployment.

GHCR packages initially default to private, even when the source repository is
public. After the first successful publication, open the package settings and
choose Public if anonymous pulls are desired. Otherwise, authenticate on the
hub with a token that has read access to the package. Do not add that token to
the repository or image. See [GitHub's registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

Distribute the root Compose file, the two role files and `.env.example` together;
the dashboard/config code comes from the image. Children use only the upstream
exporter image. To publish a release after the initial commit is on main:

```sh
git tag v1.0.0
git push origin v1.0.0
```

## Public/private boundary

Only generic examples belong in Git. Store deployment `.env` files, server
inventories, original service files, backups, keys and real metric captures in
ignored locations. The entire `private/` directory is ignored, and the Docker
build context allowlists only three source files. The five public screenshots
use mock data and generic identifiers; review any replacement images before
adding them.

Before committing, run `python3 scripts/check-public.py`. It checks public
working files and their staged versions for private paths, common credentials,
personal home directories, non-example IPs, Ethereum addresses and image
metadata. An optional `private/publication-denylist.txt` adds local server names
and domains to the check without publishing the list. CI runs the generic
checks again before image publication. These checks are guardrails: they do not
replace review of new screenshots or arbitrary prose, and CI cannot undo a
secret already pushed to a public repository.

## Local validation and limits

```sh
python3 -m unittest discover -s deployment/tests -v
```

Tests cover endpoint mapping, invalid/duplicate configuration, port collisions,
SSH forwarding/host-key options, reconnect/shutdown, generated provisioning and
dashboard membership, and rejection of missing/down/misconfigured scrape targets.
Compose configuration is validated separately for both roles without a daemon.
Image build/start and actual SSH connectivity require a running Docker Engine;
they must pass before this deployment is considered live-verified.

References: [Compose include](https://docs.docker.com/reference/compose-file/include/),
[environment interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).
