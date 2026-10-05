# GitHub Traffic Observer

Privacy-first archival and analysis for GitHub's aggregated repository Traffic metrics.

GitHub Traffic Observer keeps a durable history beyond GitHub's rolling window, records late corrections, follows repository renames by stable repository ID, tracks endpoint freshness, and emits simple explainable anomaly signals.

It **does not identify individual visitors**. GitHub's Traffic API exposes aggregate counters and top referrers/pages, not visitor identities, IP addresses, cookies, or user-agent fingerprints.

## Why this project exists

GitHub Traffic metrics are useful but short-lived and can be revised after the fact. This collector stores normalized history so you can answer questions such as:

- how daily clone/view counts changed over time;
- whether GitHub retroactively corrected a previous day;
- whether a repository rename fragmented historical reporting;
- which referrers or popular paths entered or left GitHub's top lists;
- whether an endpoint is fresh, stale, or temporarily unavailable.

Clones are **not people**, and a unique cloner is not a verified unique human. Treat the output as repository telemetry, not identity tracking.

## Recommended architecture

Keep the engine public and the real data private:

```text
PUBLIC
github-traffic-observer
  ├── scripts/
  ├── tests/
  ├── docs/
  └── action.yml
        │
        │ pinned version / commit
        ▼
PRIVATE
your-github-traffic-archive
  ├── traffic/*.csv
  ├── traffic/latest.json
  └── GitHub Actions artifacts (raw gzip snapshots)
```

The collector refuses, by default, to persist real traffic from GitHub Actions when the archive repository is public or its visibility cannot be verified.

## Features

- standard-library-only Python collector;
- stable `repository_id` continuity across renames;
- daily clones, views and GitHub-reported unique counters;
- explicit late-revision history;
- top referrer and popular-path changes;
- repository metadata snapshots;
- endpoint-level freshness/state;
- raw gzip snapshots outside Git;
- anomaly events for revisions, spikes, new referrers and clone concentration;
- offline regression tests;
- reusable composite GitHub Action.

## Quick start

Create a **private repository** for the collected data, add a secret named `TRAFFIC_GITHUB_TOKEN`, and use the workflow in `examples/private-archive-workflow.yml` as a starting point.

The caller should pin this project to a trusted tag or commit:

```yaml
- name: Collect GitHub Traffic
  uses: loic31000/github-traffic-observer@<trusted-tag-or-commit>
  env:
    TRAFFIC_OWNER: your-github-username
    TRAFFIC_GITHUB_TOKEN: ${{ secrets.TRAFFIC_GITHUB_TOKEN }}
```

For a fine-grained token, GitHub's Traffic endpoints require repository access compatible with the Traffic API. Grant only the permissions and repository scope you actually need.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `TRAFFIC_OWNER` | account whose owned repositories are discovered | required |
| `TRAFFIC_GITHUB_TOKEN` | token used for GitHub API requests | required |
| `TRAFFIC_DATA_DIR` | normalized archive directory | `traffic` |
| `TRAFFIC_ARCHIVE_REPOSITORY` | repository excluded from discovery; falls back to `GITHUB_REPOSITORY` | caller repository |
| `RAW_OUTPUT_DIR` | raw gzip output directory, ideally runner temp storage | `.traffic-raw` |
| `RAW_RETENTION_DAYS` | local raw cleanup window | `30` |
| `GITHUB_API_VERSION` | REST API version header | `2026-03-10` |
| `ALLOW_PUBLIC_TRAFFIC_ARCHIVE` | explicit override of the public-archive safety check | unset / false |

The public-archive override is intentionally opt-in. Publishing real traffic can expose private repository names, activity timing and referrer/path information.

## Data model

The normalized archive can contain:

- `daily.csv`
- `snapshots.csv`
- `revisions.csv`
- `repositories.csv`
- `repository-metadata.csv`
- `referrers.csv`
- `popular-paths.csv`
- `anomalies.csv`
- `errors.csv`
- `latest.json`
- optional `migration-conflicts.csv`

See [docs/architecture.md](docs/architecture.md) for the model and [docs/privacy.md](docs/privacy.md) for the privacy rules.

## Raw snapshots

Raw Traffic responses are written outside `TRAFFIC_DATA_DIR` to `RAW_OUTPUT_DIR/YYYY-MM-DD/HHMMSS.json.gz`.

The example private workflow uploads that directory as a GitHub Actions artifact with a bounded retention period. This avoids permanently growing Git history with compressed raw payloads.

## Validation

```bash
python -m py_compile scripts/archive_traffic.py
python -m unittest discover -s tests -v
```

The tests are offline and use temporary directories and mocked API responses.

## Privacy principles

This project is intended for aggregate repository observability.

It should not be extended to fingerprint, deanonymize or correlate an individual visitor across services. Do not add tracking pixels, cookies, IP collection, browser fingerprinting, or speculative identity attribution.

See [docs/privacy.md](docs/privacy.md).

## Security

Never commit GitHub tokens or real Traffic archives to this public repository. See [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
