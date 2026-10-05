import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import archive_traffic as a


def metrics(kind, date="2026-10-02", count=50, uniques=2):
    return {"count": count, "uniques": uniques, kind: [
        {"timestamp": date + "T00:00:00Z", "count": count, "uniques": uniques}
    ]}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "traffic"
        self.root.mkdir()
        clock = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
        overrides = {"ROOT": self.root, "RAW_ROOT": Path(self.temp.name) / "raw",
                     "now": clock, "TODAY": "2026-10-05", "SNAPSHOT_UTC": "2026-10-05T09:00:00Z",
                     "OWNER": "owner", "TOKEN": "test-secret", "ARCHIVE_REPOSITORY": "owner/archive", "ALLOW_PUBLIC_TRAFFIC_ARCHIVE": False, "ARCHIVE_PRIVATE_HINT": "", "errors": []}
        for name in ["DAILY_FILE", "SNAPSHOTS_FILE", "REFERRERS_FILE", "PATHS_FILE", "ERRORS_FILE", "LATEST_FILE",
                     "REVISIONS_FILE", "REPOSITORIES_FILE", "ANOMALIES_FILE", "METADATA_FILE"]:
            overrides[name] = self.root / getattr(a, name).name
        patcher = patch.multiple(a, **overrides)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        args = patch.object(sys, "argv", ["archive_traffic.py"])
        args.start()
        self.addCleanup(args.stop)

    def history(self):
        return {(1, name): {"repository_id": "1", "name": name,
                "first_seen_utc": "2026-09-01T00:00:00Z", "last_seen_utc": "2026-10-05T00:00:00Z",
                "is_current": str(int(name == "owner/new"))} for name in ["owner/old", "owner/new"]}

    def test_public_actions_archive_is_refused_by_default(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), \
             patch.object(a, "ARCHIVE_PRIVATE_HINT", ""), \
             patch.object(a, "api_get", return_value={"private": False}):
            with self.assertRaises(RuntimeError):
                a.ensure_archive_privacy()

        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), \
             patch.object(a, "ARCHIVE_PRIVATE_HINT", ""), \
             patch.object(a, "api_get", return_value={"private": True}):
            a.ensure_archive_privacy()

        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), \
             patch.object(a, "ALLOW_PUBLIC_TRAFFIC_ARCHIVE", True), \
             patch.object(a, "api_get") as mocked:
            a.ensure_archive_privacy()
            mocked.assert_not_called()

        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), \
             patch.object(a, "ARCHIVE_PRIVATE_HINT", "true"), \
             patch.object(a, "api_get") as mocked:
            a.ensure_archive_privacy()
            mocked.assert_not_called()

        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), \
             patch.object(a, "ARCHIVE_PRIVATE_HINT", "false"):
            with self.assertRaises(RuntimeError):
                a.ensure_archive_privacy()

    def test_import_needs_no_token_and_writes_nothing(self):
        source = str(Path(a.__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONPATH=source)
        workspace = Path(self.temp.name) / "import-only"
        workspace.mkdir()
        result = subprocess.run([sys.executable, "-c", "import scripts.archive_traffic"], cwd=workspace,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((workspace / "traffic").exists())

    def test_late_revision_and_failed_endpoint_preserves_value(self):
        rows = {("2026-10-02", "1"): {"date": "2026-10-02", "repository_id": "1", "repository": "owner/old",
                "clones": 0, "unique_cloners": 0, "views": 9, "unique_visitors": 2}}
        revisions = []
        a.update_daily(rows, 1, "owner/new", metrics("clones"), None, revisions)
        self.assertEqual(rows[("2026-10-02", "1")]["clones"], 50)
        self.assertEqual(rows[("2026-10-02", "1")]["views"], 9)
        self.assertEqual(rows[("2026-10-02", "1")]["repository"], "owner/new")
        self.assertEqual({r["metric"] for r in revisions}, {"clones", "unique_cloners"})
        again = []
        a.update_daily(rows, 1, "owner/new", metrics("clones"), None, again)
        self.assertEqual(again, [])

    def test_payload_day_at_window_boundary_is_preserved(self):
        rows = {}
        a.update_daily(rows, 1, "owner/new", metrics("clones", "2026-09-21"), None, [])
        self.assertEqual(rows[("2026-09-21", "1")]["clones"], 50)

    def test_unknown_endpoint_is_blank_and_empty_success_is_zero(self):
        rows = {}
        a.update_daily(rows, 1, "owner/new", {"count": 0, "uniques": 0, "clones": []}, None, [])
        self.assertEqual(rows[(a.TODAY, "1")]["clones"], 0)
        self.assertEqual(rows[(a.TODAY, "1")]["views"], "")

    def test_negative_revision_and_day_rollover(self):
        self.assertEqual(a.delta(2, 5), -3)
        self.assertEqual(a.daily_delta(2, 20, "2026-10-04"), 2)
        self.assertEqual(a.daily_delta(2, 5, a.TODAY), -3)
        self.assertEqual(a.delta(2, None), "")

    def test_rank_drop_reentry_and_id(self):
        old = [{"referrer": "linkedin.com", "count": 3, "uniques": 1}]
        rows = a.ranked_history("owner/new", [], old, "referrer", repository_id=1)
        self.assertEqual(rows[0]["status"], "dropped_from_top10")
        self.assertEqual(rows[0]["count"], "")
        self.assertEqual(rows[0]["repository_id"], 1)
        self.assertEqual(a.ranked_history("owner/new", old, [], "referrer")[0]["status"], "new")
        self.assertEqual(a.ranked_history("owner/new", old, old, "referrer"), [])

    def test_rank_change_and_title_change(self):
        old = [{"path": "/a", "title": "old", "count": 3, "uniques": 1},
               {"path": "/b", "title": "b", "count": 2, "uniques": 1}]
        new = [old[1], dict(old[0], title="new", count=4)]
        rows = a.ranked_history("owner/new", new, old, "path", "title", 1)
        self.assertEqual(rows[0]["rank_delta"], -1)
        self.assertEqual(rows[1]["delta_count"], 1)
        self.assertEqual(rows[1]["title"], "new")

    def test_migration_ids_merge_unknown_names_and_idempotence(self):
        fields = ["date", "repository", "clones", "unique_cloners", "views", "unique_visitors"]
        base = dict(date="2026-10-02", clones="0", unique_cloners="0", views="", unique_visitors="")
        a.write_rows(a.DAILY_FILE, fields, [dict(base, repository="owner/old", views="5"),
                     dict(base, repository="owner/new", clones="50"), dict(base, repository="owner/unknown")])
        a.migrate_archive(self.history())
        rows = a.load_rows(a.DAILY_FILE)
        known = next(row for row in rows if row["repository_id"] == "1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(known["clones"], "50")
        self.assertEqual(known["views"], "5")
        self.assertEqual(next(row for row in rows if row["repository"] == "owner/unknown")["repository_id"], "")
        before = {f.name: f.read_bytes() for f in self.root.iterdir()}
        a.migrate_archive(self.history())
        self.assertEqual(before, {f.name: f.read_bytes() for f in self.root.iterdir()})
        self.assertEqual(len(a.load_rows(self.root / "migration-conflicts.csv")), 1)

    def test_reused_name_is_not_guessed(self):
        history = self.history()
        history[(2, "owner/old")] = dict(history[(1, "owner/old")], repository_id="2")
        self.assertEqual(a.identity_for_name("owner/old", history), "")

    def test_legacy_watchers_are_not_subscribers(self):
        row = dict.fromkeys(a.METADATA_FIELDS, "")
        row.pop("subscribers")
        row.pop("watchers_count_legacy")
        row["watchers"] = "7"
        a.write_rows(a.METADATA_FILE, list(row), [row])
        a.migrate_archive({})
        result = a.load_rows(a.METADATA_FILE)[0]
        self.assertEqual(result["watchers_count_legacy"], "7")
        self.assertEqual(result["subscribers"], "")
        rows = {}
        a.update_metadata(rows, {"id": 1, "watchers_count": 50, "subscribers_count": 3})
        self.assertEqual(rows[(a.TODAY, "1")]["subscribers"], 3)
        a.update_metadata(rows, {"id": 1, "watchers_count": 50})
        self.assertEqual(rows[(a.TODAY, "1")]["subscribers"], "")

    def test_spike_uses_id_across_rename(self):
        rows = {}
        for offset in range(4):
            date = (a.now.date() - timedelta(days=offset)).isoformat()
            rows[(date, "1")] = {"clones": 50 if offset == 0 else 2, "repository": "owner/old"}
        events, signatures = [], set()
        a.detect_spike(rows, 1, "owner/new", a.TODAY, "clones", events, signatures)
        a.detect_spike(rows, 1, "owner/new", a.TODAY, "clones", events, signatures)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["baseline"], 2)

    def test_spike_requires_enough_observations(self):
        rows = {(a.TODAY, "1"): {"clones": 100}}
        events = []
        a.detect_spike(rows, 1, "owner/new", a.TODAY, "clones", events, set())
        self.assertEqual(events, [])

    def test_endpoint_failure_and_recovery(self):
        previous = {"views_snapshot_utc": "2026-10-04T02:00:00Z"}
        failed = a.endpoint_status(previous, {"views": None})
        self.assertTrue(failed["views"]["stale"])
        self.assertEqual(failed["views"]["last_success_utc"], previous["views_snapshot_utc"])
        recovered = a.endpoint_status({"endpoints": failed}, {"views": {}})
        self.assertFalse(recovered["views"]["stale"])
        self.assertEqual(recovered["views"]["last_success_utc"], a.SNAPSHOT_UTC)

    def test_concentration_episodes_failure_and_reset(self):
        state, rows, signatures = {}, [], set()
        a.update_concentration(state, {}, metrics("clones"), 1, "owner/new", rows, signatures)
        self.assertEqual(len(rows), 1)
        previous = deepcopy(state)
        a.update_concentration(state, previous, metrics("clones", count=70), 1, "owner/new", rows, signatures)
        a.update_concentration(state, state, None, 1, "owner/new", rows, signatures)
        self.assertEqual(len(rows), 1)
        a.update_concentration(state, state, metrics("clones", count=2), 1, "owner/new", rows, signatures)
        with patch.object(a, "SNAPSHOT_UTC", "2026-10-05T10:00:00Z"):
            a.update_concentration(state, state, metrics("clones"), 1, "owner/new", rows, signatures)
        self.assertEqual(len(rows), 2)

    def test_partial_discovery_fails_instead_of_marking_repos_missing(self):
        with patch.object(a, "api_get", side_effect=[[{"id": i} for i in range(100)], None]):
            with self.assertRaises(RuntimeError):
                a.discover_repositories()

    def test_invalid_payload_is_an_endpoint_failure(self):
        for payload in [{}, {"count": -1, "uniques": 0, "clones": []}, metrics("clones", "bad-date")]:
            with patch.object(a, "api_get", return_value=payload):
                self.assertIsNone(a.traffic_get("url", "owner/new", "clones"))
        self.assertEqual(len(a.errors), 3)

    def test_secret_is_redacted_from_errors(self):
        a.add_error("owner/new", "views", "response contains test-secret")
        self.assertNotIn("test-secret", a.errors[0]["error"])

    def test_corrupt_latest_fails_closed(self):
        a.LATEST_FILE.write_text("{bad")
        with self.assertRaises(RuntimeError):
            a.load_latest()

    def test_raw_is_outside_traffic_and_pruning_is_local(self):
        old = a.RAW_ROOT / "2026-08-01"
        old.mkdir(parents=True)
        file = a.write_raw_snapshot({"test": True})
        self.assertTrue(file.exists())
        self.assertFalse((a.ROOT / "raw").exists())
        a.prune_raw_snapshots()
        self.assertFalse(old.exists())

    def run_collection(self, views=None, clones=None):
        repo = {"id": 1, "full_name": "owner/new", "owner": {"login": "owner"}, "stargazers_count": 7}
        def fake_api(url, repository="", endpoint="", record_error=True):
            if endpoint == "repository-discovery":
                return [repo]
            if endpoint == "metadata":
                return dict(repo, subscribers_count=3)
            if endpoint == "clones":
                return clones
            if endpoint == "views":
                if views is None:
                    a.add_error(repository, endpoint, "HTTP 403")
                return views
            return []
        with patch.object(a, "api_get", side_effect=fake_api), contextlib.redirect_stdout(io.StringIO()):
            a.main()

    def test_full_collection_partial_failure_rename_and_old_spike(self):
        a.save_repository_history(self.history())
        a.LATEST_FILE.write_text(json.dumps({"repositories": {"owner/old": {
            "repository_id": 1, "views_14d": 9, "views_snapshot_utc": "2026-10-04T00:00:00Z"}}}))
        daily = {}
        for offset in range(3, 8):
            date = (a.now.date() - timedelta(days=offset)).isoformat()
            daily[(date, "1")] = {"date": date, "repository_id": 1, "repository": "owner/old",
                                 "clones": 0, "unique_cloners": 0, "views": 9, "unique_visitors": 2}
        a.save_daily(daily)
        self.run_collection(clones=metrics("clones"))
        state = json.loads(a.LATEST_FILE.read_text())["repositories"]["owner/new"]
        self.assertEqual(state["views_14d"], 9)
        self.assertTrue(state["endpoints"]["views"]["stale"])
        self.assertEqual(state["endpoints"]["views"]["last_success_utc"], "2026-10-04T00:00:00Z")
        snapshot = a.load_rows(a.SNAPSHOTS_FILE)[0]
        self.assertEqual(snapshot["repository_id"], "1")
        self.assertEqual(snapshot["views_14d"], "")
        events = a.load_rows(a.ANOMALIES_FILE)
        self.assertTrue(any(row["type"] == "traffic_spike" and row["date_utc"] == "2026-10-02" for row in events))
        self.assertTrue(any(row["type"] == "repository_renamed" for row in events))
        self.assertEqual(a.load_rows(a.METADATA_FILE)[0]["subscribers"], "3")
        self.assertEqual(a.load_rows(a.ERRORS_FILE)[0]["repository_id"], "1")

    def test_total_metrics_failure_still_saves_error_state_and_raw(self):
        with self.assertRaises(SystemExit):
            self.run_collection()
        state = json.loads(a.LATEST_FILE.read_text())["repositories"]["owner/new"]
        self.assertTrue(state["endpoints"]["clones"]["stale"])
        self.assertTrue(list(a.RAW_ROOT.rglob("*.gz")))
        self.assertTrue(a.load_rows(a.ERRORS_FILE))

    def test_missing_repository_state_is_kept_stale(self):
        a.LATEST_FILE.write_text(json.dumps({"repositories": {"owner/gone": {"repository_id": 2, "views_14d": 5}}}))
        self.run_collection(clones=metrics("clones"))
        state = json.loads(a.LATEST_FILE.read_text())["repositories"]["owner/gone"]
        self.assertTrue(state["discovery_missing"])
        self.assertEqual(state["views_14d"], 5)
        self.assertTrue(state["endpoints"]["views"]["stale"])

    def test_recreated_name_does_not_reuse_or_overwrite_old_identity(self):
        a.LATEST_FILE.write_text(json.dumps({"repositories": {"owner/new": {
            "repository_id": 2, "views_14d": 500}}}))
        self.run_collection(clones=metrics("clones"))
        states = json.loads(a.LATEST_FILE.read_text())["repositories"]
        self.assertEqual(states["owner/new"]["repository_id"], 1)
        self.assertNotIn("views_14d", states["owner/new"])
        retained = next(state for state in states.values() if state["repository_id"] == 2)
        self.assertEqual(retained["views_14d"], 500)
        self.assertTrue(retained["discovery_missing"])

    def test_latest_migration_keeps_original_collection_timestamp(self):
        a.LATEST_FILE.write_text(json.dumps({"snapshot_utc": "2026-10-04T01:00:00Z", "repositories": {
            "owner/new": {"repository_id": 1, "views_snapshot_utc": "2026-10-04T01:00:00Z"}}}))
        a.migrate_archive({})
        result = json.loads(a.LATEST_FILE.read_text())
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["snapshot_utc"], "2026-10-04T01:00:00Z")
        endpoints = result["repositories"]["owner/new"]["endpoints"]
        self.assertFalse(endpoints["views"]["stale"])
        self.assertEqual(endpoints["clones"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
