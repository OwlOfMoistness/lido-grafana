# Lido Grafana

Monitor a fleet of Nimbus validator clients from one Grafana dashboard: validator
activity, proposals, attestation accuracy, peers, CPU, memory, and storage.
Shared Nimbus beacon nodes and Nethermind/Geth execution clients have their own
sections alongside the validator-client overviews.

![Fleet overview with synthetic data](monitoring/previews/50-validators/01-fleet-overview.jpg)

## How it works

Choose one validator server as the **hub**. It runs Grafana, Prometheus, a host
exporter, and SSH tunnels to the other servers—the **children**. Each child runs
an exporter alongside its existing validator client. This stack monitors your
clients; it does not install or manage the validators themselves.

The hub's client list controls the dashboard automatically:

| Configuration | Client overviews |
| --- | ---: |
| Hub + one child | 2 |
| Hub + two children | 3 |
| Hub + four children | 5 |

Add or remove entries in `CHILDREN` to change the fleet size. No manual dashboard
duplication is needed. Configured clients that go offline remain visible as
unavailable.

The current deployment supports one local validator client on the hub and
additional clients over SSH, with shared main/fallback backends. Multiple client
processes on one machine and a backend without a fallback are not yet supported
as dedicated configuration modes.

## Getting started

You need Linux servers with Docker Engine and Docker Compose 2.20+, enabled
client metrics, and SSH access from the hub to its children. The shared backend
metrics must already be reachable from the hub; see the
[deployment guide](deployment/README.md) for endpoint and tunnel configuration.

### 1. Get the files on each server

```bash
git clone https://github.com/OwlOfMoistness/lido-grafana.git
cd lido-grafana
cp .env.example .env
chmod 600 .env
```

### 2. Choose each server's role

On a **child**, the essential settings are:

```dotenv
HUB=false
NODE_EXPORTER_PORT=19103
```

On the **hub**, a minimal example for one child looks like this:

```dotenv
HUB=true
VC_ID=validator-1
VC_NAME='VC 1 - Hub'
VC_METRICS_ADDRESS=127.0.0.1:8808
CHILDREN='[{"id":"validator-2","name":"VC 2","host":"child-2.example.org","user":"monitor","port":22,"local_port":20000}]'
```

These are excerpts: also fill in the hub's backend address, SSH key and verified
known-hosts file paths, and Grafana password in [`.env.example`](.env.example).
Replace the example SSH host/user with your own. Each child entry reserves two
local forwarding ports; the next child could use `local_port=20002`.

**Choose your own names:** set `VC_NAME=Apple` for the hub and use `"name":"Bananas"`
or `"name":"Oranges"` in its child entries. Grafana uses these names in selectors,
client headings, tables, and graph legends. Names are configured on the hub;
keep IDs such as `validator-2` stable when renaming a client.

The example defaults to the GHCR image
`ghcr.io/owlofmoistness/lido-grafana:1.0.0`. It becomes available after pushing
the `v1.0.0` Git tag and a successful publishing workflow. Branch pushes run
validation only; version-tag pushes publish images. No manually created GitHub
secrets are required.
[Local builds and registry details](deployment/README.md#images-from-github-container-registry)
are covered in the deployment guide.

### 3. Start and check

Start the children first, then the hub. On each server:

```bash
docker compose up -d
```

After about a minute, check the running monitoring stack on the hub:

```bash
docker compose exec tunnels python /app/control.py check
```

From your laptop, forward Grafana's port over SSH:

```bash
ssh -N -L 3200:127.0.0.1:3200 monitor@hub.example.org
```

Open [localhost:3200](http://localhost:3200) and sign in as `admin` with your
configured password. Replace the example SSH login above with your hub's login.

When adding or removing children later, update the hub's `.env`, run
`docker compose up -d --force-recreate`, then reload Grafana and select **All**.

## More documentation

- [Deployment guide](deployment/README.md): configuration, SSH, image releases, and troubleshooting.
- [Metric guide](monitoring/README.md): what the dashboard measures and its limitations.
- [Reuse existing Grafana/Prometheus](monitoring/REUSE-EXISTING.md).
- [Host storage setup](monitoring/STORAGE.md).
- [Screenshot gallery](monitoring/previews/50-validators/README.md): 50 synthetic validators.

## Repository layout

- `compose.yml` and `.env.example`: deployment entry point and configuration template.
- `deployment/`: hub/child services, image build, configuration generator, and tests.
- `monitoring/dashboards/`: Grafana dashboard template.
- `monitoring/`: metric documentation, provisioning, and alternative deployment files.
- `monitoring/previews/`: mock-data screenshots.
- `.github/workflows/`: validation and GHCR image publishing.
