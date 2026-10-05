# Contributing

Thanks for considering a contribution.

## Scope

GitHub Traffic Observer is an aggregate repository observability tool. Contributions should preserve that scope.

Do not add features intended to:

- identify individual visitors;
- collect IP addresses, cookies or browser fingerprints;
- correlate named people across services;
- publish private repository traffic by default.

See [docs/privacy.md](docs/privacy.md) before proposing telemetry changes.

## Development

Use Python 3.13 and run:

```bash
python -m py_compile scripts/archive_traffic.py
python -m unittest discover -s tests -v
```

Keep tests offline and avoid requiring real credentials.

## Pull requests

Keep changes focused, explain behavior changes, and add or update tests when collector behavior changes.

The repository intentionally rejects tracked real traffic data and the em dash character.
