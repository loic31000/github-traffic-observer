# Changelog

All notable changes to GitHub Traffic Observer are documented here.

## v1.0.0 - 2026-10-05

First stable public release.

### Added

- reusable composite GitHub Action;
- standard-library-only Python collector;
- stable repository identity tracking with numeric repository IDs;
- clone, view, referrer, popular-path and repository metadata collection;
- endpoint freshness and stale-state tracking;
- late revision history;
- repository rename continuity;
- traffic spike, new referrer and clone concentration anomaly events;
- raw gzip snapshots outside Git with bounded artifact retention;
- public-archive safety guard;
- offline regression test suite;
- privacy, security and architecture documentation;
- synthetic examples for a private archive setup;
- GitHub Marketplace branding metadata.

### Privacy

The project works with aggregated GitHub Traffic data. It does not identify individual visitors and is not intended for fingerprinting or deanonymization.
