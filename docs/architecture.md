# Architecture

GitHub Traffic Observer separates **collector code** from **collected data**.

## Components

1. **Collector** — `scripts/archive_traffic.py`, public and reusable.
2. **Caller workflow** — runs from a private archive repository.
3. **Normalized archive** — CSV/JSON files stored in the private repository.
4. **Raw snapshots** — gzip payloads stored outside Git and optionally uploaded as short-retention Actions artifacts.

## Identity

Repositories are tracked primarily by GitHub's stable numeric `repository_id`. Names are retained as observations so a rename does not split the historical series.

## Endpoint isolation

Clones, views, referrers, popular paths and metadata are fetched independently. A failed endpoint does not become a false zero. The last successful value remains in state, while the current snapshot records the endpoint as unavailable/stale.

## Late revisions

GitHub may revise an earlier day. The collector re-reads the returned daily window, updates the normalized value and writes an explicit revision event for completed days.

## Ranked data

Referrers and popular paths are top-list snapshots, not visit logs. Changes are recorded as `new`, `existing` or `dropped_from_top10`.

## Anomalies

Signals are deliberately simple and explainable:

- `late_revision`
- `repository_renamed`
- `new_referrer`
- `clone_concentration`
- `traffic_spike`

They are telemetry hints, not conclusions about a human visitor or malicious actor.

## Storage boundary

The public repository must contain only code, documentation, tests and synthetic examples. Real data belongs in a private archive repository.
