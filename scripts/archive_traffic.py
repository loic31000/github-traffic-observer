#!/usr/bin/env python3
import csv
import gzip
import json
import os
import shutil
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.github.com"
API_VERSION = os.environ.get("GITHUB_API_VERSION", "2026-03-10")
OWNER = os.environ.get("TRAFFIC_OWNER", "")
TOKEN = os.environ.get("TRAFFIC_GITHUB_TOKEN")
ARCHIVE_REPOSITORY = os.environ.get("TRAFFIC_ARCHIVE_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY", "")
ALLOW_PUBLIC_TRAFFIC_ARCHIVE = os.environ.get("ALLOW_PUBLIC_TRAFFIC_ARCHIVE", "").lower() == "true"
ARCHIVE_PRIVATE_HINT = os.environ.get("TRAFFIC_ARCHIVE_PRIVATE", "").lower()
RAW_RETENTION_DAYS = max(1, int(os.environ.get("RAW_RETENTION_DAYS", "30")))

ROOT = Path(os.environ.get("TRAFFIC_DATA_DIR", "traffic"))
DAILY_FILE = ROOT / "daily.csv"
SNAPSHOTS_FILE = ROOT / "snapshots.csv"
REFERRERS_FILE = ROOT / "referrers.csv"
PATHS_FILE = ROOT / "popular-paths.csv"
ERRORS_FILE = ROOT / "errors.csv"
LATEST_FILE = ROOT / "latest.json"
REVISIONS_FILE = ROOT / "revisions.csv"
REPOSITORIES_FILE = ROOT / "repositories.csv"
ANOMALIES_FILE = ROOT / "anomalies.csv"
METADATA_FILE = ROOT / "repository-metadata.csv"
RAW_ROOT = Path(os.environ.get("RAW_OUTPUT_DIR", ".traffic-raw"))

now = datetime.now(timezone.utc).replace(microsecond=0)
SNAPSHOT_UTC = now.isoformat().replace("+00:00", "Z")
TODAY = now.date().isoformat()

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
    "X-GitHub-Api-Version": API_VERSION,
    "User-Agent": "github-traffic-observer",
}

errors = []


def add_error(repository, endpoint, message):
    errors.append({
        "snapshot_utc": SNAPSHOT_UTC,
        "repository": repository,
        "endpoint": endpoint,
        "repository_id": "",
        "error": str(message).replace(TOKEN, "[REDACTED]")[:1000].replace("\n", " ") if TOKEN else str(message)[:1000].replace("\n", " "),
    })


def api_get(url, repository="", endpoint="", record_error=True):
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if record_error:
            add_error(repository, endpoint, f"HTTP {exc.code}: {body[:700]}")
    except urllib.error.URLError as exc:
        if record_error:
            add_error(repository, endpoint, f"Network error: {exc}")
    except Exception as exc:
        if record_error:
            add_error(repository, endpoint, f"Unexpected error: {exc}")
    return None


def ensure_archive_privacy():
    """Refuse to persist real traffic in a public GitHub Actions repository by default."""
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return
    if not ARCHIVE_REPOSITORY or ALLOW_PUBLIC_TRAFFIC_ARCHIVE:
        return
    if ARCHIVE_PRIVATE_HINT == "true":
        return
    if ARCHIVE_PRIVATE_HINT == "false":
        raise RuntimeError(
            "Refusing to persist GitHub Traffic data in a public archive repository."
        )

    metadata = api_get(
        f"{API}/repos/{ARCHIVE_REPOSITORY}",
        ARCHIVE_REPOSITORY,
        "archive-privacy",
    )
    if not isinstance(metadata, dict) or metadata.get("private") is not True:
        raise RuntimeError(
            "Refusing to persist GitHub Traffic data in a public or unverifiable "
            "archive repository. Use a private data repository, or set "
            "ALLOW_PUBLIC_TRAFFIC_ARCHIVE=true only if you intentionally accept "
            "publishing the collected traffic data."
        )


def load_rows(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_rows(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def append_rows(path, fields, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        with path.open(encoding="utf-8", newline="") as fh:
            previous_fields = next(csv.reader(fh))
        if previous_fields != fields:
            old_rows = load_rows(path)
            write_rows(path, fields, [dict((field, row.get(field, "")) for field in fields) for row in old_rows])
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def load_latest():
    if not LATEST_FILE.exists():
        return {"snapshot_utc": None, "repositories": {}}
    try:
        return json.loads(LATEST_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        add_error(
            ARCHIVE_REPOSITORY,
            "latest.json",
            f"Could not read previous state: {exc}",
        )
        raise RuntimeError("Cannot read latest.json; refusing to discard previous state") from exc


def discover_repositories():
    repos = []
    page = 1
    while True:
        query = urllib.parse.urlencode({
            "affiliation": "owner",
            "visibility": "all",
            "sort": "full_name",
            "direction": "asc",
            "per_page": 100,
            "page": page,
        })
        data = api_get(
            f"{API}/user/repos?{query}",
            OWNER,
            "repository-discovery",
        )
        if data is None:
            raise RuntimeError("Repository discovery incomplete; refusing partial discovery")
        for repo in data:
            full_name = repo.get("full_name", "")
            owner_login = repo.get("owner", {}).get("login", "")
            if owner_login.lower() != OWNER.lower():
                continue
            if full_name.lower() == ARCHIVE_REPOSITORY.lower():
                continue
            if repo.get("id"):
                repos.append(repo)
        if len(data) < 100:
            break
        page += 1
    by_id = {int(repo["id"]): repo for repo in repos}
    return sorted(
        by_id.values(),
        key=lambda repo: repo["full_name"].lower(),
    )


def day_map(payload, key):
    result = {}
    if payload is None:
        return result
    for item in payload.get(key, []):
        timestamp = item.get("timestamp", "")
        if timestamp:
            result[timestamp[:10]] = {
                "count": int(item.get("count", 0)),
                "uniques": int(item.get("uniques", 0)),
            }
    return result


def today_values(payload, key):
    values = day_map(payload, key).get(
        TODAY,
        {"count": 0, "uniques": 0},
    )
    return values["count"], values["uniques"]


def delta(current, previous):
    if current is None or previous is None or previous == "":
        return ""
    return int(current) - int(previous)


def daily_delta(current, previous, previous_date):
    if current is None or previous is None or previous == "":
        return ""
    if previous_date != TODAY:
        return int(current)
    return int(current) - int(previous)


def load_daily():
    return {
        (row["date"], repository_key(row)): row
        for row in load_rows(DAILY_FILE)
    }


def save_daily(rows):
    fields = [
        "date",
        "repository_id",
        "repository",
        "clones",
        "unique_cloners",
        "views",
        "unique_visitors",
    ]
    ordered = sorted(
        rows.values(),
        key=lambda row: (row["date"], row["repository"].lower()),
    )
    write_rows(DAILY_FILE, fields, ordered)


REVISION_FIELDS = [
    "detected_at",
    "repository_id",
    "repository",
    "date",
    "metric",
    "old_value",
    "new_value",
    "delta",
]


def update_daily(
    daily_rows,
    repository_id,
    repository,
    clones,
    views,
    revisions,
):
    dates = [
        (now.date() - timedelta(days=offset)).isoformat()
        for offset in range(13, -1, -1)
    ]
    clone_days = (
        day_map(clones, "clones")
        if clones is not None
        else None
    )
    view_days = (
        day_map(views, "views")
        if views is not None
        else None
    )
    sources = []
    if clone_days is not None:
        sources.extend([
            ("clones", clone_days, "count"),
            ("unique_cloners", clone_days, "uniques"),
        ])
    if view_days is not None:
        sources.extend([
            ("views", view_days, "count"),
            ("unique_visitors", view_days, "uniques"),
        ])

    dates = sorted(set(dates) | set(clone_days or {}) | set(view_days or {}))
    for date in dates:
        key = (date, str(repository_id))
        row = daily_rows.get(
            key,
            {
                "date": date,
                "repository_id": repository_id,
                "repository": repository,
                "clones": "",
                "unique_cloners": "",
                "views": "",
                "unique_visitors": "",
            },
        )
        row["repository_id"] = repository_id
        row["repository"] = repository
        for field, source, value_key in sources:
            new_value = int(
                source.get(
                    date,
                    {"count": 0, "uniques": 0},
                )[value_key]
            )
            old_value = row.get(field, "")
            if (
                date < TODAY
                and old_value not in ("", None)
                and int(old_value) != new_value
            ):
                revisions.append({
                    "detected_at": SNAPSHOT_UTC,
                    "repository_id": repository_id,
                    "repository": repository,
                    "date": date,
                    "metric": field,
                    "old_value": old_value,
                    "new_value": new_value,
                    "delta": new_value - int(old_value),
                })
            row[field] = new_value
        daily_rows[key] = row


def canonical_referrers(items):
    return [
        {
            "referrer": item.get("referrer", ""),
            "count": int(item.get("count", 0)),
            "uniques": int(item.get("uniques", 0)),
        }
        for item in (items or [])
    ]


def canonical_paths(items):
    return [
        {
            "path": item.get("path", ""),
            "title": item.get("title", ""),
            "count": int(item.get("count", 0)),
            "uniques": int(item.get("uniques", 0)),
        }
        for item in (items or [])
    ]


def ranked_history(
    repository,
    current,
    previous,
    key_name,
    title_name=None,
    repository_id="",
):
    if current == previous:
        return []

    previous_by_key = {
        item[key_name]: (rank, item)
        for rank, item in enumerate(previous, 1)
    }
    current_keys = set()
    rows = []

    for rank, item in enumerate(current, 1):
        key = item[key_name]
        current_keys.add(key)
        old = previous_by_key.get(key)

        row = {
            "snapshot_utc": SNAPSHOT_UTC,
            "repository_id": repository_id,
            "repository": repository,
            "status": "existing" if old else "new",
            "rank": rank,
            "previous_rank": old[0] if old else "",
            "rank_delta": rank - old[0] if old else "",
            key_name: key,
            "count": item["count"],
            "previous_count": old[1]["count"] if old else "",
            "delta_count": (
                item["count"] - old[1]["count"]
                if old
                else ""
            ),
            "uniques": item["uniques"],
            "previous_uniques": (
                old[1]["uniques"]
                if old
                else ""
            ),
            "delta_uniques": (
                item["uniques"] - old[1]["uniques"]
                if old
                else ""
            ),
        }
        if title_name:
            row[title_name] = item.get(title_name, "")
        rows.append(row)

    for old_rank, item in enumerate(previous, 1):
        if item[key_name] in current_keys:
            continue

        row = {
            "snapshot_utc": SNAPSHOT_UTC,
            "repository_id": repository_id,
            "repository": repository,
            "status": "dropped_from_top10",
            "rank": "",
            "previous_rank": old_rank,
            "rank_delta": "",
            key_name: item[key_name],
            "count": "",
            "previous_count": item["count"],
            "delta_count": "",
            "uniques": "",
            "previous_uniques": item["uniques"],
            "delta_uniques": "",
        }
        if title_name:
            row[title_name] = item.get(title_name, "")
        rows.append(row)

    return rows


REPOSITORY_FIELDS = [
    "repository_id",
    "name",
    "first_seen_utc",
    "last_seen_utc",
    "is_current",
]


def load_repository_history():
    history = {}
    for row in load_rows(REPOSITORIES_FILE):
        try:
            key = (
                int(row["repository_id"]),
                row["name"],
            )
        except (KeyError, TypeError, ValueError):
            continue
        history[key] = row
    return history


def update_repository_history(history, repos, daily_rows):
    current_ids = {
        int(repo["id"])
        for repo in repos
    }
    current_names = {
        repo["full_name"]
        for repo in repos
    }

    for row in history.values():
        row["is_current"] = "0"

    for repo in repos:
        repository_id = int(repo["id"])
        name = repo["full_name"]
        key = (repository_id, name)
        row = history.get(key)

        if row is None:
            history[key] = {
                "repository_id": repository_id,
                "name": name,
                "first_seen_utc": SNAPSHOT_UTC,
                "last_seen_utc": SNAPSHOT_UTC,
                "is_current": "1",
            }
        else:
            row["last_seen_utc"] = SNAPSHOT_UTC
            row["is_current"] = "1"

    known_names = {
        row["name"]
        for row in history.values()
    }
    historic_names = sorted({
        row["repository"]
        for row in daily_rows.values()
    })

    for historic_name in historic_names:
        if (
            historic_name in current_names
            or historic_name in known_names
        ):
            continue
        if not historic_name.lower().startswith(
            OWNER.lower() + "/"
        ):
            continue

        resolved = api_get(
            f"{API}/repos/{historic_name}",
            historic_name,
            "repository-identity-migration",
            record_error=False,
        )
        if not resolved or not resolved.get("id"):
            continue

        repository_id = int(resolved["id"])
        if repository_id not in current_ids:
            continue

        dates = sorted(
            row["date"]
            for row in daily_rows.values()
            if row["repository"] == historic_name
        )
        history[(repository_id, historic_name)] = {
            "repository_id": repository_id,
            "name": historic_name,
            "first_seen_utc": (
                f"{dates[0]}T00:00:00Z"
                if dates
                else SNAPSHOT_UTC
            ),
            "last_seen_utc": (
                f"{dates[-1]}T23:59:59Z"
                if dates
                else SNAPSHOT_UTC
            ),
            "is_current": "0",
        }


def save_repository_history(history):
    ordered = sorted(
        history.values(),
        key=lambda row: (
            int(row["repository_id"]),
            row["first_seen_utc"],
            row["name"].lower(),
        ),
    )
    write_rows(
        REPOSITORIES_FILE,
        REPOSITORY_FIELDS,
        ordered,
    )


METADATA_FIELDS = [
    "date",
    "snapshot_utc",
    "repository_id",
    "repository",
    "visibility",
    "archived",
    "default_branch",
    "stars",
    "forks",
    "subscribers",
    "watchers_count_legacy",
    "open_issues",
    "size_kb",
    "created_at",
    "updated_at",
    "pushed_at",
]


def load_metadata():
    return {
        (row["date"], row["repository_id"]): row
        for row in load_rows(METADATA_FILE)
    }


def update_metadata(rows, repo):
    repository_id = str(repo["id"])
    rows[(TODAY, repository_id)] = {
        "date": TODAY,
        "snapshot_utc": SNAPSHOT_UTC,
        "repository_id": repository_id,
        "repository": repo.get("full_name", ""),
        "visibility": repo.get("visibility", ""),
        "archived": int(
            bool(repo.get("archived", False))
        ),
        "default_branch": repo.get(
            "default_branch",
            "",
        ),
        "stars": int(
            repo.get("stargazers_count", 0)
        ),
        "forks": int(
            repo.get("forks_count", 0)
        ),
        "subscribers": repo.get("subscribers_count", ""),
        "watchers_count_legacy": "",
        "open_issues": int(
            repo.get("open_issues_count", 0)
        ),
        "size_kb": int(
            repo.get("size", 0)
        ),
        "created_at": repo.get("created_at", ""),
        "updated_at": repo.get("updated_at", ""),
        "pushed_at": repo.get("pushed_at", ""),
    }


def save_metadata(rows):
    ordered = sorted(
        rows.values(),
        key=lambda row: (
            row["date"],
            row["repository"].lower(),
        ),
    )
    write_rows(
        METADATA_FILE,
        METADATA_FIELDS,
        ordered,
    )


ANOMALY_FIELDS = [
    "detected_at",
    "repository_id",
    "repository",
    "type",
    "severity",
    "date_utc",
    "metric",
    "value",
    "baseline",
    "details",
    "signature",
]


def anomaly_signatures():
    return {
        row.get("signature", "")
        for row in load_rows(ANOMALIES_FILE)
        if row.get("signature")
    }


def add_anomaly(
    rows,
    signatures,
    repository_id,
    repository,
    kind,
    severity="info",
    date_utc="",
    metric="",
    value="",
    baseline="",
    details="",
    signature="",
):
    if not signature:
        signature = "|".join(map(
            str,
            [
                repository_id,
                repository,
                kind,
                date_utc,
                metric,
                value,
                baseline,
            ],
        ))

    if signature in signatures:
        return

    signatures.add(signature)
    rows.append({
        "detected_at": SNAPSHOT_UTC,
        "repository_id": repository_id,
        "repository": repository,
        "type": kind,
        "severity": severity,
        "date_utc": date_utc,
        "metric": metric,
        "value": value,
        "baseline": baseline,
        "details": details,
        "signature": signature,
    })


def detect_spike(
    daily_rows,
    repository_id,
    repository,
    date_utc,
    field,
    anomaly_rows,
    signatures,
):
    current = daily_rows.get(
        (date_utc, str(repository_id))
    )
    if (
        not current
        or current.get(field, "") in ("", None)
    ):
        return

    value = int(current[field])
    day = datetime.fromisoformat(date_utc).date()
    previous = []

    for offset in range(1, 8):
        row = daily_rows.get((
            (
                day - timedelta(days=offset)
            ).isoformat(),
            str(repository_id),
        ))
        if (
            row
            and row.get(field, "") not in ("", None)
        ):
            previous.append(int(row[field]))

    if len(previous) < 3:
        return

    baseline = statistics.median(previous)
    minimum = 10 if field == "clones" else 20
    threshold = max(minimum, baseline * 3)
    minimum_gap = (
        5
        if field == "clones"
        else 10
    )

    if (
        value >= threshold
        and value - baseline >= minimum_gap
    ):
        add_anomaly(
            anomaly_rows,
            signatures,
            repository_id,
            repository,
            "traffic_spike",
            "medium",
            date_utc,
            field,
            value,
            baseline,
            (
                f"{field} above 3x the median of "
                "the previous 7 available days"
            ),
            (
                f"{repository_id}|traffic_spike|"
                f"{date_utc}|{field}|{value}"
            ),
        )


def write_raw_snapshot(payload):
    day_dir = RAW_ROOT / TODAY
    day_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    path = (
        day_dir
        / f"{now.strftime('%H%M%S')}.json.gz"
    )
    with gzip.open(
        path,
        "wt",
        encoding="utf-8",
    ) as fh:
        json.dump(
            payload,
            fh,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return path


def prune_raw_snapshots():
    if not RAW_ROOT.exists():
        return

    cutoff = (
        now.date()
        - timedelta(days=RAW_RETENTION_DAYS)
    )

    for child in RAW_ROOT.iterdir():
        if not child.is_dir():
            continue
        try:
            day = datetime.strptime(
                child.name,
                "%Y-%m-%d",
            ).date()
        except ValueError:
            continue

        if day < cutoff:
            shutil.rmtree(child)


def repository_key(row):
    return str(row.get("repository_id") or row["repository"])


def identity_for_name(name, history):
    identities = {str(row["repository_id"]) for row in history.values() if row["name"] == name}
    # Reused names cannot be resolved safely without temporal evidence.
    return next(iter(identities)) if len(identities) == 1 else ""


def migrate_archive(history):
    """Upgrade legacy tables without inventing IDs or subscriber counts."""
    current_names = {str(row["repository_id"]): row["name"] for row in history.values() if row["is_current"] == "1"}
    for path in [DAILY_FILE, SNAPSHOTS_FILE, REFERRERS_FILE, PATHS_FILE, ERRORS_FILE]:
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            fields = next(csv.reader(fh))
        if "repository_id" not in fields:
            fields.insert(fields.index("repository"), "repository_id")
        original = load_rows(path)
        rows = deepcopy(original)
        for row in rows:
            if not row.get("repository_id"):
                row["repository_id"] = identity_for_name(row["repository"], history)
        if path == DAILY_FILE:
            grouped = {}
            conflicts = []
            for row in rows:
                key = (row["date"], repository_key(row))
                old = grouped.get(key)
                if old is None:
                    grouped[key] = row
                    continue
                prefer_new = row["repository"] == current_names.get(str(row["repository_id"]))
                kept, other = (row, old) if prefer_new else (old, row)
                metrics = ["clones", "unique_cloners", "views", "unique_visitors"]
                if any(kept.get(field, "") != other.get(field, "") for field in metrics):
                    conflicts.append({"date": row["date"], "repository_id": row["repository_id"], "kept_row": json.dumps(kept, sort_keys=True), "other_row": json.dumps(other, sort_keys=True)})
                for field in metrics:
                    if kept.get(field, "") == "":
                        kept[field] = other.get(field, "")
                grouped[key] = kept
            rows = sorted(grouped.values(), key=lambda row: (row["date"], row["repository"].lower()))
            append_rows(ROOT / "migration-conflicts.csv", ["date", "repository_id", "kept_row", "other_row"], conflicts)
        if rows != original or not original:
            write_rows(path, fields, rows)

    if METADATA_FILE.exists():
        rows = load_rows(METADATA_FILE)
        changed = False
        for row in rows:
            if "watchers" in row:
                row["watchers_count_legacy"] = row.pop("watchers")
                row["subscribers"] = ""
                changed = True
        if changed:
            write_rows(METADATA_FILE, METADATA_FIELDS, rows)

    if LATEST_FILE.exists():
        latest = load_latest()
        original = deepcopy(latest)
        latest["schema_version"] = 2
        for state in latest.get("repositories", {}).values():
            if "endpoints" in state:
                continue
            last_run = state.get("last_run_utc") or latest.get("snapshot_utc")
            state["endpoints"] = {}
            for endpoint in ["clones", "views", "referrers", "popular_paths", "metadata"]:
                success = state.get(f"{endpoint}_snapshot_utc")
                state["endpoints"][endpoint] = {
                    "last_attempt_utc": last_run,
                    "last_success_utc": success,
                    "stale": not success or success != last_run,
                    "status": "ok" if success and success == last_run else "unknown",
                }
        if latest != original:
            write_latest(latest)


def write_latest(state):
    ROOT.mkdir(parents=True, exist_ok=True)
    temporary = LATEST_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(LATEST_FILE)


def traffic_get(url, repository, endpoint):
    payload = api_get(url, repository, endpoint)
    if payload is None:
        return None
    try:
        if endpoint in ("clones", "views"):
            if not isinstance(payload, dict) or not isinstance(payload[endpoint], list):
                raise ValueError("Expected traffic series")
            values = [payload, *payload[endpoint]]
            for item in payload[endpoint]:
                datetime.strptime(item["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
        else:
            if not isinstance(payload, list):
                raise ValueError("Expected ranked list")
            values = payload
            key = "referrer" if endpoint == "referrers" else "path"
            if any(not isinstance(item.get(key), str) for item in values):
                raise ValueError("Missing ranked key")
        if any(type(item.get(key)) is not int or item[key] < 0 for item in values for key in ("count", "uniques")):
            raise ValueError("Expected non-negative integer counts")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        add_error(repository, endpoint, f"Invalid payload: {exc}")
        return None
    return payload


def endpoint_status(previous, payloads, attempted=True):
    result = {}
    for endpoint, payload in payloads.items():
        old = previous.get("endpoints", {}).get(endpoint, {})
        last_success = old.get("last_success_utc") or previous.get(f"{endpoint}_snapshot_utc")
        result[endpoint] = {
            "last_attempt_utc": SNAPSHOT_UTC if attempted else old.get("last_attempt_utc"),
            "last_success_utc": SNAPSHOT_UTC if payload is not None else last_success,
            "stale": payload is None,
            "status": ("ok" if payload is not None else "unavailable") if attempted else "not_discovered",
        }
    return result


def update_concentration(state, previous, clones, repository_id, repository, rows, signatures):
    if clones is None:
        return  # An API failure neither starts nor ends an episode.
    count, uniques = int(clones["count"]), int(clones["uniques"])
    active = count >= 20 and uniques > 0 and count / uniques >= 5
    previous_active = previous.get("clone_concentration_active")
    if previous_active is None:
        old_count, old_uniques = previous.get("clones_14d", 0), previous.get("unique_cloners_14d", 0)
        previous_active = bool(old_uniques and old_count >= 20 and old_count / old_uniques >= 5)
    state["clone_concentration_active"] = active
    if active and not previous_active:
        add_anomaly(rows, signatures, repository_id, repository, "clone_concentration", "medium", TODAY,
                    "clones_14d_per_unique", round(count / uniques, 2), 5,
                    f"{count} clones for {uniques} unique cloners over 14 days",
                    f"{repository_id}|clone_concentration|episode|{SNAPSHOT_UTC}")


def main():
    global errors
    errors = []
    if "--migrate-only" in sys.argv:
        migrate_archive(load_repository_history())
        return
    if not OWNER or not TOKEN:
        raise SystemExit("ERROR: TRAFFIC_OWNER and TRAFFIC_GITHUB_TOKEN are required.")
    ensure_archive_privacy()
    ROOT.mkdir(parents=True, exist_ok=True)
    previous_state = load_latest()
    previous_repositories = previous_state.get(
        "repositories",
        {},
    )
    previous_by_id = {}

    for previous_name, state in previous_repositories.items():
        repository_id = state.get("repository_id")
        if repository_id is not None:
            previous_by_id[str(repository_id)] = (
                previous_name,
                state,
            )

    daily_rows = load_daily()
    repositories = discover_repositories()

    if not repositories:
        append_rows(
            ERRORS_FILE,
            [
                "snapshot_utc",
                "repository_id",
                "repository",
                "endpoint",
                "error",
            ],
            errors,
        )
        print(
            "ERROR: no repositories could be "
            "discovered with this token."
        )
        sys.exit(1)

    print(
        f"Monitoring {len(repositories)} repositories."
    )

    repository_history = load_repository_history()
    update_repository_history(
        repository_history,
        repositories,
        daily_rows,
    )
    migrate_archive(repository_history)
    daily_rows = load_daily()
    metadata_rows = load_metadata()
    revision_rows = []
    snapshot_rows = []
    referrer_rows = []
    path_rows = []
    anomaly_rows = []
    signatures = anomaly_signatures()

    raw_payload = {
        "snapshot_utc": SNAPSHOT_UTC,
        "api_version": API_VERSION,
        "owner": OWNER,
        "repositories": {},
    }

    next_state = {
        "snapshot_utc": SNAPSHOT_UTC,
        "api_version": API_VERSION,
        "owner": OWNER,
        "archive_repository": ARCHIVE_REPOSITORY,
        "raw_retention_days": RAW_RETENTION_DAYS,
        "schema_version": 2,
        "repositories": {},
    }

    successful_metric_snapshots = 0

    for repo_data in repositories:
        repository = repo_data["full_name"]
        repository_id = int(repo_data["id"])
        owner, repo = repository.split("/", 1)
        base = (
            f"{API}/repos/{owner}/{repo}/traffic"
        )

        print(f"\n[{repository}]")

        clones = traffic_get(
            f"{base}/clones?per=day",
            repository,
            "clones",
        )
        views = traffic_get(
            f"{base}/views?per=day",
            repository,
            "views",
        )
        referrers = traffic_get(
            f"{base}/popular/referrers",
            repository,
            "referrers",
        )
        paths = traffic_get(
            f"{base}/popular/paths",
            repository,
            "popular-paths",
        )

        raw_payload["repositories"][repository] = {
            "repository_id": repository_id,
            "clones": clones,
            "views": views,
            "referrers": referrers,
            "popular_paths": paths,
        }

        previous_name = repository
        found = previous_by_id.get(str(repository_id))
        if found:
            previous_name, previous = found
        else:
            candidate = previous_repositories.get(repository, {})
            previous = candidate if candidate.get("repository_id") in (None, repository_id) else {}

        if previous is None:
            previous = {}

        if previous_name != repository:
            add_anomaly(
                anomaly_rows,
                signatures,
                repository_id,
                repository,
                "repository_renamed",
                "info",
                details=(
                    f"{previous_name} -> {repository}"
                ),
                signature=(
                    f"{repository_id}|repository_renamed|"
                    f"{previous_name}|{repository}"
                ),
            )

        state = deepcopy(previous)
        state["repository_id"] = repository_id
        state["repository_name"] = repository
        state.pop("discovery_missing", None)

        update_daily(
            daily_rows,
            repository_id,
            repository,
            clones,
            views,
            revision_rows,
        )
        details = api_get(f"{API}/repos/{repository}", repository, "metadata")
        metadata_data = dict(repo_data)
        if isinstance(details, dict) and "subscribers_count" in details:
            metadata_data["subscribers_count"] = details["subscribers_count"]
        update_metadata(metadata_rows, metadata_data)

        clones_today = unique_cloners_today = None
        views_today = unique_visitors_today = None

        clone_prev_snapshot = previous.get(
            "clones_snapshot_utc",
            "",
        )
        clone_prev_date = previous.get(
            "clones_date_utc",
            "",
        )
        view_prev_snapshot = previous.get(
            "views_snapshot_utc",
            "",
        )
        view_prev_date = previous.get(
            "views_date_utc",
            "",
        )

        if clones is not None:
            (
                clones_today,
                unique_cloners_today,
            ) = today_values(
                clones,
                "clones",
            )

            state.update({
                "clones_snapshot_utc": SNAPSHOT_UTC,
                "clones_date_utc": TODAY,
                "clones_today": clones_today,
                "unique_cloners_today": (
                    unique_cloners_today
                ),
                "clones_14d": int(
                    clones.get("count", 0)
                ),
                "unique_cloners_14d": int(
                    clones.get("uniques", 0)
                ),
            })

        if views is not None:
            (
                views_today,
                unique_visitors_today,
            ) = today_values(
                views,
                "views",
            )

            state.update({
                "views_snapshot_utc": SNAPSHOT_UTC,
                "views_date_utc": TODAY,
                "views_today": views_today,
                "unique_visitors_today": (
                    unique_visitors_today
                ),
                "views_14d": int(
                    views.get("count", 0)
                ),
                "unique_visitors_14d": int(
                    views.get("uniques", 0)
                ),
            })

        if (
            clones is not None
            or views is not None
        ):
            successful_metric_snapshots += 1

            snapshot_rows.append({
                "snapshot_utc": SNAPSHOT_UTC,
                "repository_id": repository_id,
                "repository": repository,
                "date_utc": TODAY,
                "clones_today": (
                    clones_today
                    if clones_today is not None
                    else ""
                ),
                "unique_cloners_today": (
                    unique_cloners_today
                    if unique_cloners_today is not None
                    else ""
                ),
                "delta_clones_today": (
                    daily_delta(
                        clones_today,
                        previous.get("clones_today"),
                        clone_prev_date,
                    )
                    if clones is not None
                    else ""
                ),
                "delta_unique_cloners_today": (
                    daily_delta(
                        unique_cloners_today,
                        previous.get(
                            "unique_cloners_today"
                        ),
                        clone_prev_date,
                    )
                    if clones is not None
                    else ""
                ),
                "clones_14d": (
                    int(clones.get("count", 0))
                    if clones is not None
                    else ""
                ),
                "unique_cloners_14d": (
                    int(clones.get("uniques", 0))
                    if clones is not None
                    else ""
                ),
                "delta_clones_14d": (
                    delta(
                        int(
                            clones.get("count", 0)
                        ),
                        previous.get("clones_14d"),
                    )
                    if clones is not None
                    else ""
                ),
                "delta_unique_cloners_14d": (
                    delta(
                        int(
                            clones.get(
                                "uniques",
                                0,
                            )
                        ),
                        previous.get(
                            "unique_cloners_14d"
                        ),
                    )
                    if clones is not None
                    else ""
                ),
                "clones_previous_snapshot_utc": (
                    clone_prev_snapshot
                ),
                "views_today": (
                    views_today
                    if views_today is not None
                    else ""
                ),
                "unique_visitors_today": (
                    unique_visitors_today
                    if unique_visitors_today is not None
                    else ""
                ),
                "delta_views_today": (
                    daily_delta(
                        views_today,
                        previous.get("views_today"),
                        view_prev_date,
                    )
                    if views is not None
                    else ""
                ),
                "delta_unique_visitors_today": (
                    daily_delta(
                        unique_visitors_today,
                        previous.get(
                            "unique_visitors_today"
                        ),
                        view_prev_date,
                    )
                    if views is not None
                    else ""
                ),
                "views_14d": (
                    int(views.get("count", 0))
                    if views is not None
                    else ""
                ),
                "unique_visitors_14d": (
                    int(views.get("uniques", 0))
                    if views is not None
                    else ""
                ),
                "delta_views_14d": (
                    delta(
                        int(
                            views.get("count", 0)
                        ),
                        previous.get("views_14d"),
                    )
                    if views is not None
                    else ""
                ),
                "delta_unique_visitors_14d": (
                    delta(
                        int(
                            views.get(
                                "uniques",
                                0,
                            )
                        ),
                        previous.get(
                            "unique_visitors_14d"
                        ),
                    )
                    if views is not None
                    else ""
                ),
                "views_previous_snapshot_utc": (
                    view_prev_snapshot
                ),
                "referrers_in_top10": (
                    len(referrers)
                    if isinstance(referrers, list)
                    else ""
                ),
                "popular_paths_in_top10": (
                    len(paths)
                    if isinstance(paths, list)
                    else ""
                ),
            })

        if isinstance(referrers, list):
            current_referrers = (
                canonical_referrers(referrers)
            )
            previous_referrers = (
                canonical_referrers(
                    previous.get("referrers", [])
                )
            )

            referrer_rows.extend(
                ranked_history(
                    repository,
                    current_referrers,
                    previous_referrers,
                    "referrer",
                    repository_id=repository_id,
                )
            )

            previous_keys = {
                item["referrer"]
                for item in previous_referrers
            }

            for item in current_referrers:
                if (
                    item["referrer"]
                    and item["referrer"]
                    not in previous_keys
                ):
                    add_anomaly(
                        anomaly_rows,
                        signatures,
                        repository_id,
                        repository,
                        "new_referrer",
                        "info",
                        metric="referrer",
                        value=item["referrer"],
                        details=(
                            "New top referrer: "
                            f"{item['referrer']}"
                        ),
                        signature=(
                            f"{repository_id}|"
                            "new_referrer|"
                            f"{item['referrer']}"
                        ),
                    )

            state["referrers"] = current_referrers
            state["referrers_snapshot_utc"] = (
                SNAPSHOT_UTC
            )

        if isinstance(paths, list):
            current_paths = canonical_paths(paths)
            previous_paths = canonical_paths(
                previous.get(
                    "popular_paths",
                    [],
                )
            )

            path_rows.extend(
                ranked_history(
                    repository,
                    current_paths,
                    previous_paths,
                    "path",
                    "title",
                    repository_id=repository_id,
                )
            )

            state["popular_paths"] = current_paths
            state["popular_paths_snapshot_utc"] = (
                SNAPSHOT_UTC
            )

        update_concentration(state, previous, clones, repository_id, repository, anomaly_rows, signatures)
        state["endpoints"] = endpoint_status(previous, {
            "clones": clones, "views": views, "referrers": referrers,
            "popular_paths": paths, "metadata": details,
        })

        state["last_run_utc"] = SNAPSHOT_UTC
        next_state["repositories"][
            repository
        ] = state

        if clones is not None:
            print(
                "  clones: "
                f"{state.get('clones_14d', 0)} / "
                f"{state.get('unique_cloners_14d', 0)} "
                "uniques (14d)"
            )

        if views is not None:
            print(
                "  views : "
                f"{state.get('views_14d', 0)} / "
                f"{state.get('unique_visitors_14d', 0)} "
                "uniques (14d)"
            )

    for revision in revision_rows:
        add_anomaly(
            anomaly_rows,
            signatures,
            revision["repository_id"],
            revision["repository"],
            "late_revision",
            "info",
            revision["date"],
            revision["metric"],
            revision["new_value"],
            revision["old_value"],
            (
                "GitHub revised "
                f"{revision['metric']} "
                f"from {revision['old_value']} "
                f"to {revision['new_value']}"
            ),
            (
                f"{revision['repository_id']}|"
                "late_revision|"
                f"{revision['date']}|"
                f"{revision['metric']}|"
                f"{revision['old_value']}|"
                f"{revision['new_value']}"
            ),
        )

    for repo_data in repositories:
        repository = repo_data["full_name"]
        repository_id = int(repo_data["id"])

        # Recompute the full fetched window; delayed revisions also affect later baselines.
        for candidate_date in [(now.date() - timedelta(days=offset)).isoformat() for offset in range(14, -1, -1)]:
            detect_spike(
                daily_rows,
                repository_id,
                repository,
                candidate_date,
                "clones",
                anomaly_rows,
                signatures,
            )
            detect_spike(
                daily_rows,
                repository_id,
                repository,
                candidate_date,
                "views",
                anomaly_rows,
                signatures,
            )

    for previous_name, previous in previous_repositories.items():
        if str(previous.get("repository_id")) not in {str(repo["id"]) for repo in repositories}:
            retained = deepcopy(previous)
            retained["endpoints"] = endpoint_status(previous, dict.fromkeys(["clones", "views", "referrers", "popular_paths", "metadata"]), attempted=False)
            retained["discovery_missing"] = True
            retained_key = previous_name
            if retained_key in next_state["repositories"]:
                retained_key = f"{previous_name}#repository-id-{previous.get('repository_id')}"
            next_state["repositories"][retained_key] = retained
    for error in errors:
        error["repository_id"] = identity_for_name(error["repository"], repository_history)

    save_daily(daily_rows)
    save_repository_history(repository_history)
    save_metadata(metadata_rows)

    snapshot_fields = [
        "snapshot_utc",
        "repository_id",
        "repository",
        "date_utc",
        "clones_today",
        "unique_cloners_today",
        "delta_clones_today",
        "delta_unique_cloners_today",
        "clones_14d",
        "unique_cloners_14d",
        "delta_clones_14d",
        "delta_unique_cloners_14d",
        "clones_previous_snapshot_utc",
        "views_today",
        "unique_visitors_today",
        "delta_views_today",
        "delta_unique_visitors_today",
        "views_14d",
        "unique_visitors_14d",
        "delta_views_14d",
        "delta_unique_visitors_14d",
        "views_previous_snapshot_utc",
        "referrers_in_top10",
        "popular_paths_in_top10",
    ]

    append_rows(
        SNAPSHOTS_FILE,
        snapshot_fields,
        snapshot_rows,
    )

    append_rows(
        REFERRERS_FILE,
        [
            "snapshot_utc",
            "repository_id",
            "repository",
            "status",
            "rank",
            "previous_rank",
            "rank_delta",
            "referrer",
            "count",
            "previous_count",
            "delta_count",
            "uniques",
            "previous_uniques",
            "delta_uniques",
        ],
        referrer_rows,
    )

    append_rows(
        PATHS_FILE,
        [
            "snapshot_utc",
            "repository_id",
            "repository",
            "status",
            "rank",
            "previous_rank",
            "rank_delta",
            "path",
            "title",
            "count",
            "previous_count",
            "delta_count",
            "uniques",
            "previous_uniques",
            "delta_uniques",
        ],
        path_rows,
    )

    append_rows(
        REVISIONS_FILE,
        REVISION_FIELDS,
        revision_rows,
    )

    append_rows(
        ANOMALIES_FILE,
        ANOMALY_FIELDS,
        anomaly_rows,
    )

    append_rows(
        ERRORS_FILE,
        [
            "snapshot_utc",
            "repository_id",
            "repository",
            "endpoint",
            "error",
        ],
        errors,
    )

    write_latest(next_state)

    raw_payload["errors"] = errors
    raw_path = write_raw_snapshot(raw_payload)
    prune_raw_snapshots()

    summary_path = os.environ.get(
        "GITHUB_STEP_SUMMARY"
    )

    if summary_path:
        def cell(value):
            return (
                "-"
                if value == ""
                or value is None
                else str(value)
            )

        with open(
            summary_path,
            "a",
            encoding="utf-8",
        ) as summary:
            summary.write(
                "# GitHub Traffic Snapshot\n\n"
            )
            summary.write(
                f"Snapshot UTC: {SNAPSHOT_UTC}\n\n"
            )
            summary.write(
                "| Repository | Clones today | Δ clones | "
                "Unique cloners | Δ unique | Views today | "
                "Δ views | Unique visitors | "
                "Δ unique visitors |\n"
            )
            summary.write(
                "|---|---:|---:|---:|---:|---:|---:|"
                "---:|---:|\n"
            )

            for row in snapshot_rows:
                summary.write(
                    f"| {row['repository']} "
                    f"| {cell(row['clones_today'])} "
                    f"| {cell(row['delta_clones_today'])} "
                    f"| {cell(row['unique_cloners_today'])} "
                    f"| {cell(row['delta_unique_cloners_today'])} "
                    f"| {cell(row['views_today'])} "
                    f"| {cell(row['delta_views_today'])} "
                    f"| {cell(row['unique_visitors_today'])} "
                    f"| {cell(row['delta_unique_visitors_today'])} |\n"
                )

            summary.write(
                "\nLate revisions: "
                f"**{len(revision_rows)}** · "
                "New anomaly events: "
                f"**{len(anomaly_rows)}**\n"
            )

            if errors:
                summary.write(
                    "\n⚠️ API errors recorded: "
                    f"**{len(errors)}** "
                    "(see traffic/errors.csv).\n"
                )

    print()
    print(f"Snapshot UTC: {SNAPSHOT_UTC}")
    print(
        "Metric snapshots written: "
        f"{len(snapshot_rows)}"
    )
    print(
        "Referrer changes written: "
        f"{len(referrer_rows)}"
    )
    print(
        "Popular-path changes written: "
        f"{len(path_rows)}"
    )
    print(
        "Late revisions written: "
        f"{len(revision_rows)}"
    )
    print(
        "Anomaly events written: "
        f"{len(anomaly_rows)}"
    )
    print(f"API errors: {len(errors)}")

    if raw_path:
        print(f"Raw snapshot: {raw_path}")

    if successful_metric_snapshots == 0:
        print(
            "ERROR: no clone/view metrics could "
            "be collected."
        )
        sys.exit(1)



if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        append_rows(ERRORS_FILE, ["snapshot_utc", "repository_id", "repository", "endpoint", "error"], errors)
        raise SystemExit(str(exc))
