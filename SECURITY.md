# Security Policy

## Reporting a vulnerability

Use a private GitHub security-reporting channel when available. Do not post active credentials, private repository names, private Traffic archives or sensitive exploit details in a public issue.

## Secrets

Never commit API credentials, personal access credentials, private archive CSV/JSON files, or raw Traffic payloads from real repositories.

Use GitHub Actions Secrets or another secret manager.

## Token scope

Grant only the repositories and permissions needed to read GitHub Traffic. Rotate a credential if it is ever exposed.

## Data repository

The recommended deployment uses a separate **private** repository for normalized history and raw artifacts. The public collector repository should contain synthetic examples only.
