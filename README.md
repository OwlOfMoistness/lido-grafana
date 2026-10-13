# Validator monitoring applications

Two independent applications in one repository:

| Application | Purpose | Status |
| --- | --- | --- |
| [Lido Grafana](apps/lido-grafana/README.md) | Fleet metrics, client and node health, activity and upcoming proposals | Existing application |
| [Validator Watch](apps/validator-watch/README.md) | On-chain duty outcomes, incident history and notifications | Initial implementation; live validation pending |

## Lido Grafana

Follow the [installation guide](apps/lido-grafana/README.md#getting-started).
The standalone deployment files now live in `apps/lido-grafana/`: download
`compose.yml` and `.env.example` from that directory. Existing server deployments
keep their current file locations; the container name remains
`ghcr.io/owlofmoistness/lido-grafana`.

From a source checkout, run Grafana build and deployment commands inside
`apps/lido-grafana/`. The GitHub workflow uses that directory as its build context.
Version tags (`vMAJOR.MINOR.PATCH`) publish both images after both applications
pass validation. For example, pushing `v0.6.0` would publish:

- `ghcr.io/owlofmoistness/lido-grafana:0.6.0`
- `ghcr.io/owlofmoistness/validator-watch:0.6.0`

To release only Validator Watch, use `validator-watch/vMAJOR.MINOR.PATCH`.
For example, `validator-watch/v0.7.0` publishes `validator-watch:0.7.0` without
publishing a new Lido Grafana image.

Both images support AMD64 and ARM64 and also receive minor-version, `latest`,
and commit-SHA tags. Ordinary branch pushes and manual workflow runs validate
only. Publishing uses the workflow's `GITHUB_TOKEN`; no additional registry
secret is needed. The two publication jobs run independently, so a registry or
build failure can leave one published while the other needs retrying.

Publishing an image does not deploy either app or change running servers.

## Validator Watch

A separate collector, SQLite history and Grafana dashboard. It records attestation
exceptions, proposal outcomes and sync summaries, with optional Discord webhook
alerts for confirmed missed proposals, repeated attestation issues and collection
failures. Its standalone Compose deployment is configured through its own `.env`.
Public-key CSV files attribute outcomes to
clients; no signing keys or extra validator-client service are required.

Follow its [installation guide](apps/validator-watch/README.md#getting-started) and
review the [mock dashboard previews](apps/validator-watch/previews/README.md).
Automated tests use simulated chain data; live compatibility and node overhead
still need validation on the deployed beacon version.
