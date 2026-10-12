# Validator Watch previews

Actual Grafana 13.2.2 screenshots using the SQLite datasource and deterministic
synthetic data: 50 fictional validators across Apple, Banana and Orange.
No real beacon endpoint or Discord webhook is used.

- [Fleet overview](01-fleet-overview.jpg): aggregate counters, source/target/head
  issues, proposal outcomes and client rankings.
- [Exceptions and alerts](02-exceptions-and-alerts.jpg): individual attestation
  exceptions, collection coverage and mock Discord delivery history.
- [Client overview](03-client-overview.jpg): Apple selected; per-validator rankings.
- [Single validator](04-validator-detail.jpg): one validator's history.

`Affected` counts attestations with at least one missing flag; `Issue points`
counts missing flags (up to three per attestation). `Checked` is the number of
attestation duties evaluated. Coverage is checked versus attempted epochs for
the fleet; it does not change when filtering a client. Mock `sent` delivery rows
are example data and do not represent messages actually sent to Discord.

Regenerate mock history using `python3 -m watch.main demo` with `DATABASE_PATH`
pointing to a new disposable database. The screenshots are captured from the
provisioned dashboards; they are not image-generated designs.
