# Validation

## Public packaging and GHCR workflow — 2026-09-25

Public examples and documentation now use generic hub/child/main/fallback
labels and documentation-only addresses. Named deployment copies remain ignored.
Five mock screenshots were reviewed visually and checked for embedded metadata;
a storage preview with a deployment-specific mount label was moved out of the
public tree. Public source checks include staged content and an optional ignored
local identifier list. No private files are tracked and there is no prior commit
history in this checkout.

The GHCR workflow passed actionlint 1.7.12. The five publication-guard tests,
eight control-code tests, both Compose roles and Prometheus 3.14.0 query tests pass.
All referenced action commits were resolved from official repository tags. CI
also builds and exercises the image before publishing AMD64/ARM64 manifests.
Docker is unavailable locally, so those actual image builds and a first GHCR
publication remain pending the workflow run.

Fleet-size generation was checked for 1, 2, 3 and 5 clients, then reduced from
5 to 2 in the same output directory. Generated selectors, table/legend labels
and scrape targets contain exactly the configured clients; the Grafana client
row remains a variable-driven repeat template. This is configuration testing,
not a new browser-rendering check for each fleet size.

Custom-name generation was tested with Apple, Bananas and Oranges, including
storage legends that retain their mountpoint suffix. A display-only rename
leaves generated Prometheus targets unchanged. These generated display settings
have not yet received a new live browser-rendering check.

## New hub/child package — 2026-09-25

The root Compose deployment is documented in `deployment/README.md`. Local
checks passed for both HUB=false (exporter only) and HUB=true (configuration,
tunnels, exporter, Prometheus, Grafana), including environment interpolation
and build/bind paths. Six control-code tests cover target generation, invalid
configuration and port collisions, OpenSSH argument parsing, variable fleet
membership, SSH reconnect/shutdown and actual-scrape checker failure handling.
Prometheus 3.14.0 accepted the generated configuration and passed the dashboard
query suite. No image build or container startup was performed: the local Docker
daemon was unavailable. Nothing was deployed to the servers or published to GHCR.

Host-side backend checks are separate from verifying the new hub container
stack, hub-to-child SSH forwards, and backend filesystem accuracy.

## Earlier dashboard validation

Checks were performed locally on 2026-09-20 and 2026-09-21 with synthetic data.
No production validator server was changed.

## Configuration and queries

- YAML/JSON configurations parsed successfully.
- Standalone Compose and the reuse overlay merged successfully. The overlay was
  checked against a private reference base deployment; users must validate it
  against their own base Compose file.
- Prometheus 2.53.1 accepted the reuse configuration and twelve fleet targets;
  Prometheus 3.14.0 accepted the standalone configuration.
- All 60 dashboard query targets parsed in Prometheus 2.53.1.
- All 15 semantic tests passed, covering loaded-key completeness, offline and
  missing metrics, counter resets, separate main/fallback accuracy, zero
  opportunities, activity scaling, whole-number proposal estimates, and
  filesystem filtering.

Run the query tests as documented in [README.md](README.md#validation).

## Visual checks

The dashboard was provisioned in temporary Grafana 9.5.18 and 13.2.2 instances.
Repeated rows and single-instance filtering were exercised. Grafana 9.5.18 can
lose the first repeated row's variable scope when it starts collapsed, so
validator/backend rows start expanded. Users can collapse them after loading.
Main and fallback metrics were checked independently, including after reopening
rows. Storage is not repeated and starts collapsed.

The latest layout was checked in Grafana 9.5.18 at 1280×720: top summary strip,
three shorter graphs, full accuracy legends, comparison tables, VC detail,
main/fallback detail, and storage. The [current gallery](previews/50-validators/README.md)
uses 50 synthetic validators. Missing metrics displayed neutral No data, and
unreachable test endpoints displayed reporting failures.

The layout-only edits preserve all PromQL expressions. The screenshot preview's
denser synthetic activity and point density do not change production settings.

## Required live verification

Before relying on the dashboard, confirm SSH configuration, private forwarding
reachability, all metric endpoints and labels, node-exporter filesystem coverage,
beacon validator-monitor coverage, and hub memory/disk capacity. Mock screenshots
do not validate live storage mounts or validator performance. Aggregate beacon
metrics do not provide exact per-VC on-chain accuracy or an authoritative active
backend indicator.

## Repository cleanup

Original deployment inputs were moved byte-for-byte into the ignored `private/`
directory. Git ignore checks confirmed that original inputs, local environment
files, SSH configuration, and metric samples are excluded. Public Markdown links
and JSON files were checked after the move; local usernames, private endpoint
addresses, and machine-specific SSH settings were replaced with placeholders.
