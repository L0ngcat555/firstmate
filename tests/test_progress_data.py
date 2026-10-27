"""Behavioural tests for the progress snapshot adapter (roadmap T2).

Synthetic fixtures under ``tests/fixtures/progress`` stand in for the pilot
home and the firstmate state directory.  Every impure edge (clock, gh runner,
HTTP fetch, cache directory) is injected; no test touches the network, gh,
herdr, or the live factory state.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ops import progress_data  # noqa: E402  (this import is the intended red state)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "progress"
PILOT = FIXTURES / "pilot"
STATE = FIXTURES / "state"

T = {
    "09:00": "2026-10-04T09:00:00Z",
    "09:05": "2026-10-04T09:05:00Z",
    "09:10": "2026-10-04T09:10:00Z",
    "09:20": "2026-10-04T09:20:00Z",
    "09:25": "2026-10-04T09:25:00Z",
    "09:30": "2026-10-04T09:30:00Z",
    "09:35": "2026-10-04T09:35:00Z",
    "09:40": "2026-10-04T09:40:00Z",
    "09:50": "2026-10-04T09:50:00Z",
    "10:00": "2026-10-04T10:00:00Z",
    "10:20": "2026-10-04T10:20:00Z",
    "08:00": "2026-10-04T08:00:00Z",
    "11:00": "2026-10-04T11:00:00Z",
    "11:30": "2026-10-04T11:30:00Z",
    "12:00": "2026-10-04T12:00:00Z",
}

PR201 = {
    "createdAt": T["09:25"],
    "mergedAt": T["10:00"],
    "reviews": [{"state": "APPROVED", "submittedAt": T["09:50"]}],
}


def span(issue, phase, at, event="start", **extra):
    payload = {"issue": issue, "phase": phase, "event": event, "at": at}
    payload.update(extra)
    return payload


def event(name, at, note=""):
    return {"event": name, "at": at, "note": note}


def pairs(history):
    return [(entry["stage"], entry["at"]) for entry in history]


def make_config(cache_dir):
    return {
        "repos": ["acme/widgets"],
        "pilot_home": str(PILOT),
        "state_dir": str(STATE),
        "staging_url": "https://example.test/api/health",
        "cache_dir": str(cache_dir),
        "window_hours": 12,
    }


class StageMappingTest(unittest.TestCase):
    def test_full_chain_maps_every_stage_from_its_source(self):
        history = progress_data.derive_history(
            42,
            claim={"claimed_at": T["09:00"]},
            statuses={"ombra-gh-42": [event("working", T["09:10"])]},
            spans=[
                span(42, "implementation", T["09:10"]),
                span(42, "review", T["09:30"]),
                span(42, "review", T["09:40"]),
            ],
            pr={"createdAt": T["09:35"], "mergedAt": T["10:00"],
                "reviews": [{"state": "APPROVED",
                             "submittedAt": T["09:50"]}]},
            merge={"merged_at": T["10:00"]},
            completed={"completed_at": T["10:20"]},
        )
        self.assertEqual(pairs(history), [
            ("claimed", T["09:00"]),
            ("dispatched", T["09:10"]),
            ("receipted", T["09:30"]),
            ("reviewing", T["09:30"]),
            ("reviewed", T["09:50"]),
            ("published", T["09:50"]),
            ("merged", T["10:00"]),
            ("delivered", T["10:20"]),
        ])

    def test_fallbacks_use_spans_pr_and_review_status(self):
        history = progress_data.derive_history(
            43,
            spans=[span(43, "setup", T["09:05"])],
            statuses={"ombra-gh-43-review-code": [event("working",
                                                       T["09:40"])]},
            pr={"createdAt": T["09:25"], "mergedAt": T["10:00"],
                "reviews": []},
        )
        self.assertEqual(pairs(history), [
            ("dispatched", T["09:05"]),
            ("receipted", T["09:25"]),
            ("reviewing", T["09:40"]),
            ("merged", T["10:00"]),
        ])

    def test_first_occurrence_wins_and_history_is_sorted(self):
        history = progress_data.derive_history(
            44,
            claim={"claimed_at": T["09:00"]},
            statuses={"ombra-gh-44": [
                event("working", T["09:10"]),
                event("working", T["09:05"]),
            ]},
            spans=[
                span(44, "code_review", T["11:00"]),
                span(44, "review", T["09:40"]),
                span(44, "review", T["09:30"]),
            ],
        )
        self.assertEqual(pairs(history), [
            ("claimed", T["09:00"]),
            ("dispatched", T["09:05"]),
            ("receipted", T["09:30"]),
            ("reviewing", T["09:30"]),
        ])
        stages = [entry["stage"] for entry in history]
        self.assertEqual(len(stages), len(set(stages)))

    def test_incomplete_from_release_action_and_closed_issue(self):
        """Mission: stages include "rework"; only "delivered, incomplete" are
        terminal, and rework renders as "waiting for slot".  A release or
        requeue on an *open* issue is therefore waiting for a slot, not a
        terminal incomplete; incomplete is reserved for closed-without-delivery.
        """
        released = progress_data.derive_history(
            45,
            claim={"claimed_at": T["09:00"]},
            actions=[
                {"action": "release", "issue": 45, "at": T["10:20"]},
                {"action": "release", "issue": 99, "at": T["10:20"]},
            ],
        )
        self.assertEqual(pairs(released), [
            ("claimed", T["09:00"]),
            ("rework", T["10:20"]),
        ])

        closed = progress_data.derive_history(
            46,
            pr={"createdAt": T["09:00"], "closedAt": T["10:20"]},
        )
        self.assertEqual(pairs(closed), [
            ("receipted", T["09:00"]),
            ("incomplete", T["10:20"]),
        ])

        delivered = progress_data.derive_history(
            46,
            pr={"createdAt": T["09:00"], "closedAt": T["10:20"]},
            completed={"completed_at": T["10:20"]},
        )
        self.assertNotIn("incomplete",
                         [entry["stage"] for entry in delivered])

    def test_record_without_any_timestamp_has_empty_history(self):
        self.assertEqual(progress_data.derive_history(47), [])


class ReaderTest(unittest.TestCase):
    def test_task_to_issue(self):
        self.assertEqual(progress_data.task_issue("ombra-gh-42"), 42)
        self.assertEqual(
            progress_data.task_issue("ombra-gh-42-review-code"), 42)
        self.assertIsNone(progress_data.task_issue("firstmate-tool-updates"))
        self.assertIsNone(progress_data.task_issue("ombra-gh-"))

    def test_status_line_parser_ignores_unknown_lines(self):
        parsed = progress_data.parse_status_line(
            "blocked [key=103-slot] [at=1791108300]: waiting for review slot")
        self.assertEqual(parsed["event"], "blocked")
        self.assertEqual(parsed["at"], "2026-10-04T10:05:00Z")
        self.assertEqual(parsed["note"], "waiting for review slot")

        working = progress_data.parse_status_line(
            "working [at=1791108120]: worktree ready")
        self.assertEqual(working["event"], "working")
        self.assertEqual(working["at"], "2026-10-04T10:02:00Z")
        self.assertEqual(working["note"], "worktree ready")

        self.assertIsNone(
            progress_data.parse_status_line("this line is malformed"))
        self.assertIsNone(progress_data.parse_status_line(""))
        self.assertIsNone(
            progress_data.parse_status_line("working without a timestamp"))

    def test_status_line_parser_accepts_at_before_key(self):
        # Real factory shape (state/ombra-gh-619.status): the epoch bracket is
        # not last, so a parser anchored on ``[at=N]:`` drops the line and
        # ``waiting`` falls back to a stale earlier note.
        parsed = progress_data.parse_status_line(
            "working [at=1791104218] [key=619-historical-evidence]: "
            "worktree ready")
        self.assertEqual(parsed["event"], "working")
        self.assertEqual(parsed["at"], progress_data.utc_iso(1791104218))
        self.assertEqual(parsed["note"], "worktree ready")

        events = progress_data.read_statuses(STATE)["ombra-gh-103"]
        self.assertIn("retrying after review",
                      [item.get("note") for item in events])

    def test_claims_merges_completed_and_spans_readers(self):
        claims = progress_data.read_claims(PILOT)
        self.assertEqual(set(claims), {"101", "102"})
        self.assertEqual(claims["101"]["claimed_at"], T["09:00"])

        merges = progress_data.read_merges(PILOT)
        self.assertIn("201", merges)
        self.assertEqual(merges["201"]["merged_at"], T["10:00"])

        completed = progress_data.read_completed(PILOT)
        self.assertIn("201", completed)
        self.assertEqual(completed["201"]["completed_at"], T["10:20"])

        spans = progress_data.read_spans(PILOT)
        self.assertTrue(spans)
        self.assertEqual(spans[0]["issue"], 201)
        self.assertTrue(all("at" in item for item in spans))

    def test_status_reader_keeps_malformed_files_as_history_less_tasks(self):
        statuses = progress_data.read_statuses(STATE)
        self.assertIn("ombra-gh-103", statuses)
        events = statuses["ombra-gh-103"]
        self.assertEqual(events[0]["event"], "working")
        self.assertEqual(events[0]["at"], "2026-10-04T10:02:00Z")
        self.assertIn("waiting for review slot",
                      [item.get("note") for item in events])
        self.assertIn("ombra-gh-104", statuses)
        self.assertEqual(statuses["ombra-gh-104"], [])


class BacklogCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)
        self.now = 1000.0
        self.calls = []

    def runner(self, args):
        self.calls.append(list(args))
        return json.dumps([11, 12, 13])

    def clock(self):
        return self.now

    def test_live_then_cached_for_three_hundred_seconds(self):
        first = progress_data.backlog_snapshot(
            self.config, clock=self.clock, runner=self.runner)
        self.assertEqual(first, {"open": 3, "source": "live"})
        self.assertEqual(len(self.calls), 1)

        self.now += 299
        cached = progress_data.backlog_snapshot(
            self.config, clock=self.clock, runner=self.runner)
        self.assertEqual(cached, {"open": 3, "source": "cache"})
        self.assertEqual(len(self.calls), 1)

        self.now += 2
        refreshed = progress_data.backlog_snapshot(
            self.config, clock=self.clock, runner=self.runner)
        self.assertEqual(refreshed, {"open": 3, "source": "live"})
        self.assertEqual(len(self.calls), 2)

    def test_expired_cache_is_stale_fallback_on_failure(self):
        progress_data.backlog_snapshot(
            self.config, clock=self.clock, runner=self.runner)

        def failing(args):
            raise RuntimeError("gh failed")

        self.now += 301
        stale = progress_data.backlog_snapshot(
            self.config, clock=self.clock, runner=failing)
        self.assertEqual(stale, {"open": 3, "source": "stale"})

    def test_failure_without_cache_is_unavailable(self):
        def failing(args):
            raise RuntimeError("gh failed")

        result = progress_data.backlog_snapshot(
            self.config, clock=self.clock, runner=failing)
        self.assertEqual(result, {"open": None, "source": "unavailable"})


class StagingShatest(unittest.TestCase):
    def test_build_suffix_becomes_eight_char_sha(self):
        self.assertEqual(
            progress_data.staging_sha('{"build": "dev+229b7447cf7b"}'),
            "229b7447",
        )
        self.assertIsNone(progress_data.staging_sha('{"build": "dev"}'))
        self.assertIsNone(progress_data.staging_sha("{}"))


class SnapshotTest(unittest.TestCase):
    """Fixture #102 is an open release action.

    Mission: rework is "waiting for slot" and only "delivered, incomplete" are
    terminal stages, so an open release is non-terminal rework, not incomplete.
    """
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)
        # Resolved records never fetch; their persisted PR metadata (approved
        # reviews included) is read from the cache directory.
        Path(self.tmp.name).mkdir(exist_ok=True)
        (Path(self.tmp.name) / "pr-9201.json").write_text(json.dumps({
            "at": 1791112000.0, "data": PR201}))
        self.gh_calls = []

    def runner(self, args):
        args = list(args)
        self.gh_calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps([1, 2, 3, 4, 5])
        if args[:2] == ["pr", "view"]:
            self.assertEqual(args[2], "9201")
            return json.dumps(PR201)
        raise AssertionError("unexpected gh invocation: %r" % (args,))

    def http(self, url):
        self.assertEqual(url, "https://example.test/api/health")
        return 200, '{"build": "dev+229b7447cf7b"}'

    def snapshot(self):
        return progress_data.snapshot(
            self.config,
            clock=lambda: 1791112000.0,
            runner=self.runner,
            http=self.http,
        )

    def test_contract_mapping_from_synthetic_fixtures(self):
        snap = self.snapshot()
        self.assertEqual(snap["generated"], "2026-10-04T11:06:40Z")
        by_issue = {item["issue"]: item for item in snap["attempts"]}
        self.assertEqual(set(by_issue), {
            "acme/widgets#101",
            "acme/widgets#102",
            "acme/widgets#103",
            "acme/widgets#104",
            "acme/widgets#201",
        })

        self.assertEqual(by_issue["acme/widgets#101"]["stage"], "claimed")
        self.assertEqual(pairs(by_issue["acme/widgets#101"]["history"]), [
            ("claimed", T["09:00"]),
        ])

        released = by_issue["acme/widgets#102"]
        self.assertEqual(released["stage"], "rework")
        self.assertEqual(pairs(released["history"]), [
            ("claimed", T["09:00"]),
            ("rework", T["10:20"]),
        ])

        active = by_issue["acme/widgets#103"]
        self.assertEqual(active["stage"], "reviewing")
        self.assertEqual(active["review_pane"], "w4:p11")
        self.assertEqual(active["waiting"], "waiting for review slot")
        self.assertEqual(pairs(active["history"]), [
            ("dispatched", "2026-10-04T10:02:00Z"),
            ("receipted", "2026-10-04T10:45:00Z"),
            ("reviewing", "2026-10-04T10:45:00Z"),
        ])

        self.assertEqual(by_issue["acme/widgets#104"]["history"], [])

        delivered = by_issue["acme/widgets#201"]
        self.assertEqual(delivered["stage"], "delivered")
        self.assertEqual(delivered["pr"], 9201)
        self.assertEqual(delivered["deployed_sha"], "abcdef01")
        self.assertEqual(pairs(delivered["history"]), [
            ("dispatched", T["09:10"]),
            ("receipted", T["09:20"]),
            ("reviewing", T["09:20"]),
            ("reviewed", T["09:50"]),
            ("published", T["09:50"]),
            ("merged", T["10:00"]),
            ("delivered", T["10:20"]),
        ])

    def test_capacity_paused_backlog_and_staging(self):
        snap = self.snapshot()
        self.assertEqual(snap["capacity"]["active_issues"], 4)
        self.assertEqual(snap["capacity"]["ceiling"]["issue_limit"], 4)
        self.assertEqual(snap["paused"], {"paused": False})
        self.assertEqual(snap["backlog"], {"open": 5, "source": "live"})
        self.assertEqual(snap["staging"]["deployed_sha"], "229b7447")
        self.assertEqual(snap["staging"]["url"],
                         "https://example.test/api/health")

    def test_gh_failure_degrades_to_typed_fields(self):
        def failing(args):
            raise RuntimeError("gh is unavailable")

        snap = progress_data.snapshot(
            self.config,
            clock=lambda: 1791112000.0,
            runner=failing,
            http=self.http,
        )
        self.assertEqual(snap["backlog"]["open"], None)
        self.assertEqual(snap["backlog"]["source"], "unavailable")
        self.assertIn("attempts", snap)
        self.assertEqual(snap["capacity"]["active_issues"], 4)

    def test_slots_used_counts_claims_not_active_attempts(self):
        snap = self.snapshot()
        self.assertEqual(snap["capacity"]["slots_used"], 2)
        self.assertEqual(snap["capacity"]["active_issues"], 4)

    def test_slots_used_falls_back_to_active_without_pilot_state(self):
        config = dict(self.config)
        config["pilot_home"] = str(FIXTURES / "missing-pilot")
        snap = progress_data.snapshot(
            config,
            clock=lambda: 1791112000.0,
            runner=self.runner,
            http=self.http,
        )
        self.assertEqual(snap["capacity"]["active_issues"], 3)
        self.assertEqual(snap["capacity"]["slots_used"], 3)


class ClosureRuleTest(unittest.TestCase):
    """A closed issue without a delivery receipt ends incomplete."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pilot = base / "pilot"
        self.state = base / "state"
        self.cache = base / "cache"
        (self.pilot / "merges").mkdir(parents=True)
        (self.pilot / "completed").mkdir()
        self.state.mkdir()
        (self.pilot / "state.json").write_text("{}")
        (self.pilot / "config.json").write_text('{"enabled": true}')
        (self.pilot / "merges" / "301.json").write_text(json.dumps({
            "issue": 301, "task": "ombra-gh-301", "pr": 9301,
            "merged_sha": "abcdef0123456789",
            "merged_at": T["10:00"],
        }))
        self.config = make_config(self.cache)
        self.config["pilot_home"] = str(self.pilot)
        self.config["state_dir"] = str(self.state)
        self.now = 1000.0
        self.issue_state = "OPEN"
        self.issue_fail = False
        self.issue_calls = []
        self.closed_at = T["10:20"]
        self.now = 1791112000.0  # 2026-10-04T11:06:40Z

    def runner(self, args):
        args = list(args)
        if args[:2] == ["issue", "list"]:
            return "[]"
        if args[:2] == ["issue", "view"]:
            self.issue_calls.append(args[2])
            if self.issue_fail:
                raise RuntimeError("gh is unavailable")
            return json.dumps({"state": self.issue_state,
                               "closedAt": self.closed_at})
        if args[:2] == ["pr", "view"]:
            return json.dumps({"createdAt": T["09:25"],
                               "mergedAt": T["10:00"], "reviews": []})
        raise AssertionError("unexpected gh invocation: %r" % (args,))

    def http(self, url):
        return 200, '{"build": "dev+229b7447cf7b"}'

    def snapshot(self):
        return progress_data.snapshot(
            self.config, clock=lambda: self.now,
            runner=self.runner, http=self.http)

    def attempt(self, snap):
        return snap["attempts"][0]

    def test_closed_merged_issue_without_receipt_becomes_incomplete(self):
        # Closure beyond the merge grace is not explained by the merge.
        self.issue_state = "CLOSED"
        self.closed_at = T["12:00"]
        snap = self.snapshot()
        record = self.attempt(snap)
        self.assertEqual(record["stage"], "incomplete")
        self.assertEqual(pairs(record["history"])[-1],
                         ("incomplete", T["12:00"]))
        self.assertEqual(record["last_active"], T["12:00"])
        self.assertEqual(snap["capacity"]["active_issues"], 0)

    def test_closure_explained_by_merge_within_grace_stays_merged(self):
        # A merge means the record is on the delivery path: while a completion
        # receipt can still arrive, closure seconds after merged_at must not
        # add a terminal incomplete.
        self.issue_state = "CLOSED"
        self.closed_at = "2026-10-04T10:00:02Z"
        self.now = 1791108600.0  # 2026-10-04T10:10:00Z, inside the grace
        snap = self.snapshot()
        record = self.attempt(snap)
        self.assertEqual(record["stage"], "merged")
        self.assertNotIn("incomplete",
                         [item["stage"] for item in record["history"]])
        self.assertEqual(snap["capacity"]["active_issues"], 1)

    def test_merge_grace_is_configurable(self):
        self.issue_state = "CLOSED"
        self.closed_at = T["10:20"]
        self.config["merge_close_grace_seconds"] = 60
        snap = self.snapshot()
        self.assertEqual(self.attempt(snap)["stage"], "incomplete")

    def test_merged_two_hours_ago_without_receipt_is_incomplete(self):
        self.issue_state = "CLOSED"
        self.closed_at = "2026-10-04T10:00:02Z"
        self.now = 1791115200.0  # 2026-10-04T12:00:00Z, past the grace
        snap = self.snapshot()
        record = self.attempt(snap)
        self.assertEqual(record["stage"], "incomplete")
        self.assertEqual([item for item in record["history"]
                          if item["stage"] == "incomplete"],
                         [{"stage": "incomplete", "at": self.closed_at}])
        self.assertEqual(snap["capacity"]["active_issues"], 0)

    def test_merged_two_hours_ago_with_receipt_stays_delivered(self):
        self.issue_state = "CLOSED"
        self.closed_at = "2026-10-04T10:00:02Z"
        self.now = 1791115200.0
        (self.pilot / "completed" / "301.json").write_text(json.dumps({
            "issue": 301, "pr": 9301, "merged_sha": "abcdef0123456789",
            "completed_at": T["10:20"],
        }))
        snap = self.snapshot()
        self.assertEqual(self.attempt(snap)["stage"], "delivered")
        self.assertNotIn("incomplete", [
            item["stage"] for item in self.attempt(snap)["history"]])

    def test_release_after_closure_does_not_keep_the_record_active(self):
        (self.pilot / "merges" / "301.json").unlink()
        (self.state / "ombra-gh-301.status").write_text(
            "working [at=1791105600]: dispatched\n")
        (self.pilot / "state.json").write_text(json.dumps({"actions": [
            {"action": "release", "issue": 301,
             "at": "2026-10-04T10:21:00Z"}]}))
        # A stale ledger row classified the release itself as incomplete.
        self.cache.mkdir(parents=True)
        (self.cache / "history.jsonl").write_text(json.dumps({
            "issue": "acme/widgets#301", "stage": "incomplete",
            "at": "2026-10-04T10:21:00Z"}) + "\n")
        self.issue_state = "CLOSED"
        snap = self.snapshot()
        record = self.attempt(snap)
        self.assertEqual(record["stage"], "incomplete")
        self.assertNotIn("rework",
                         [item["stage"] for item in record["history"]])
        self.assertEqual([item for item in record["history"]
                          if item["stage"] == "incomplete"],
                         [{"stage": "incomplete", "at": T["10:20"]}])

    def test_ledger_cannot_resurrect_a_pre_merge_incomplete(self):
        # The append-only ledger recorded the old closedAt->incomplete; it is
        # not replayed once the merge explains that closure.
        self.cache.mkdir(parents=True)
        (self.cache / "history.jsonl").write_text(json.dumps({
            "issue": "acme/widgets#301", "stage": "incomplete",
            "at": "2026-10-04T09:59:58Z"}) + "\n")
        snap = self.snapshot()
        record = self.attempt(snap)
        self.assertEqual(record["stage"], "merged")
        self.assertNotIn("incomplete",
                         [item["stage"] for item in record["history"]])

    def test_closed_issue_with_completion_receipt_stays_delivered(self):
        self.issue_state = "CLOSED"
        (self.pilot / "completed" / "301.json").write_text(json.dumps({
            "issue": 301, "pr": 9301, "merged_sha": "abcdef0123456789",
            "completed_at": T["10:20"],
        }))
        snap = self.snapshot()
        self.assertEqual(self.attempt(snap)["stage"], "delivered")
        self.assertEqual(self.issue_calls, [])
        self.assertEqual(snap["capacity"]["active_issues"], 0)

    def test_open_issue_stage_is_unchanged(self):
        snap = self.snapshot()
        record = self.attempt(snap)
        self.assertEqual(record["stage"], "merged")
        self.assertNotIn("incomplete",
                         [item["stage"] for item in record["history"]])
        self.assertEqual(self.issue_calls, ["301"])
        self.assertEqual(snap["capacity"]["active_issues"], 1)

    def test_gh_failure_leaves_stage_unchanged(self):
        self.issue_fail = True
        snap = self.snapshot()
        self.assertEqual(self.attempt(snap)["stage"], "merged")
        self.assertEqual(snap["capacity"]["active_issues"], 1)

    def test_closure_lookup_is_cached_for_three_hundred_seconds(self):
        self.now = 1000.0
        self.snapshot()
        self.now = 1299.0
        self.snapshot()
        self.assertEqual(self.issue_calls, ["301"])
        self.now = 1301.0
        self.snapshot()
        self.assertEqual(self.issue_calls, ["301", "301"])


class ReviewSpanTest(unittest.TestCase):
    """Agent review ends are the review/merge boundary when no approval."""

    def review_end(self, issue, phase, at):
        return {"issue": issue, "phase": phase, "event": "end", "at": at,
                "outcome": "passed"}

    def test_last_review_end_before_merge_stamps_reviewed_and_published(self):
        history = progress_data.derive_history(
            50,
            spans=[
                span(50, "review", T["09:30"]),
                self.review_end(50, "review", T["09:40"]),
                self.review_end(50, "code_review", T["09:50"]),
                self.review_end(50, "review", T["11:00"]),
            ],
            merge={"merged_at": T["10:00"]},
        )
        self.assertEqual(pairs(history), [
            ("receipted", T["09:30"]),
            ("reviewing", T["09:30"]),
            ("reviewed", T["09:50"]),
            ("published", T["09:50"]),
            ("merged", T["10:00"]),
        ])

    def test_gh_approval_still_wins_when_present(self):
        history = progress_data.derive_history(
            51,
            spans=[self.review_end(51, "review", T["09:40"])],
            pr={"reviews": [{"state": "APPROVED",
                             "submittedAt": T["09:50"]}],
                "mergedAt": T["10:00"]},
        )
        self.assertEqual(pairs(history), [
            ("reviewed", T["09:50"]),
            ("published", T["09:50"]),
            ("merged", T["10:00"]),
        ])

    def test_unmerged_record_uses_last_review_end_overall(self):
        history = progress_data.derive_history(
            52,
            spans=[
                span(52, "browser_review", T["09:30"]),
                self.review_end(52, "review", T["09:40"]),
                self.review_end(52, "browser_review", T["09:50"]),
            ],
        )
        self.assertEqual(pairs(history), [
            ("receipted", T["09:30"]),
            ("reviewing", T["09:30"]),
            ("reviewed", T["09:50"]),
            ("published", T["09:50"]),
        ])

    def test_no_span_end_and_no_approval_omits_both_stages(self):
        history = progress_data.derive_history(
            53, pr={"createdAt": T["09:25"]})
        self.assertEqual(pairs(history), [("receipted", T["09:25"])])


class StagingStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for name in ("pilot", "state", "cache"):
            (base / name).mkdir()
        self.config = {
            "repos": ["acme/widgets"],
            "pilot_home": str(base / "pilot"),
            "state_dir": str(base / "state"),
            "staging_url": "https://example.test/api/health",
            "cache_dir": str(base / "cache"),
        }

    def runner(self, args):
        return "[]"

    def snapshot(self, http):
        return progress_data.snapshot(
            self.config, clock=lambda: 1000.0,
            runner=self.runner, http=http)

    def test_http_200_reports_up_and_derives_sha(self):
        snap = self.snapshot(
            lambda url: (200, '{"build": "dev+229b7447cf7b"}'))
        self.assertEqual(snap["staging"]["status"], "up (HTTP 200)")
        self.assertEqual(snap["staging"]["deployed_sha"], "229b7447")

    def test_non_200_reports_down(self):
        snap = self.snapshot(lambda url: (404, "not found"))
        self.assertEqual(snap["staging"]["status"], "DOWN (HTTP 404)")
        self.assertIsNone(snap["staging"]["deployed_sha"])

    def test_transport_failure_reports_error(self):
        def failing(url):
            raise OSError("connection refused")

        snap = self.snapshot(failing)
        self.assertEqual(snap["staging"]["status"], "ERROR")
        self.assertIsNone(snap["staging"]["deployed_sha"])



class IncompleteClassificationTest(unittest.TestCase):
    """Mission stages: rework is "waiting for slot"; incomplete is terminal.

    A merge puts the record on the delivery path, so a closure stamped at
    merge time (either side of ``merged_at`` by a few seconds) must not add a
    pre-merge incomplete; closure only means incomplete beyond the grace.
    """

    def test_closed_release_action_is_incomplete(self):
        history = progress_data.derive_history(
            60,
            claim={"claimed_at": T["09:00"]},
            actions=[{"action": "requeue", "issue": 60, "at": T["09:30"]}],
            pr={"createdAt": T["09:05"], "closedAt": T["10:20"]},
        )
        stages = [entry["stage"] for entry in history]
        self.assertIn("rework", stages)
        self.assertEqual(pairs(history)[-1], ("incomplete", T["10:20"]))

    def test_merge_grace_absorbs_a_closure_stamped_at_merge_time(self):
        history = progress_data.derive_history(
            61,
            pr={"closedAt": "2026-10-04T09:59:58Z", "mergedAt": T["10:00"]},
            merge={"merged_at": T["10:00"]},
            now=1791108600.0,  # 10 minutes after the merge
        )
        self.assertEqual(pairs(history), [("merged", T["10:00"])])

    def test_closure_beyond_the_grace_is_incomplete(self):
        history = progress_data.derive_history(
            62,
            pr={"closedAt": T["11:30"], "mergedAt": T["10:00"]},
            merge={"merged_at": T["10:00"]},
            now=1791115200.0,  # 12:00, two hours past the merge
        )
        self.assertEqual(pairs(history), [
            ("merged", T["10:00"]),
            ("incomplete", T["11:30"]),
        ])

    def test_release_far_from_the_merge_is_still_a_slot_wait(self):
        history = progress_data.derive_history(
            65,
            actions=[{"action": "release", "issue": 65, "at": T["08:00"]}],
            merge={"merged_at": T["10:00"]},
            now=1791115200.0,
        )
        self.assertIn(("rework", T["08:00"]), pairs(history))

    def test_release_after_the_merge_is_not_a_slot_wait(self):
        history = progress_data.derive_history(
            64,
            actions=[{"action": "release", "issue": 64, "at": T["10:20"]}],
            merge={"merged_at": T["10:00"]},
        )
        self.assertEqual(pairs(history), [("merged", T["10:00"])])

    def test_merge_grace_is_a_parameter(self):
        history = progress_data.derive_history(
            63,
            pr={"closedAt": T["10:20"], "mergedAt": T["10:00"]},
            merge={"merged_at": T["10:00"]},
            merge_grace=60,
            now=1791108600.0,
        )
        self.assertIn("incomplete",
                      [entry["stage"] for entry in history])


class LedgerDedupeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.records = [
            {"issue": "acme/widgets#1", "stage": "claimed", "at": T["09:00"]},
            {"issue": "acme/widgets#1", "stage": "delivered",
             "at": T["10:20"]},
        ]

    def lines(self):
        return (self.cache / "history.jsonl").read_text().splitlines()

    def test_two_runs_over_the_same_inputs_append_nothing_twice(self):
        progress_data.append_history(self.cache, self.records)
        progress_data.append_history(self.cache, self.records)
        self.assertEqual(len(self.lines()), 2)

    def test_duplicates_inside_one_batch_are_appended_once(self):
        progress_data.append_history(
            self.cache, self.records + list(self.records))
        self.assertEqual(len(self.lines()), 2)

    def test_malformed_ledger_lines_are_tolerated(self):
        (self.cache / "history.jsonl").write_text("not json\n")
        progress_data.append_history(self.cache, self.records)
        self.assertEqual(len(self.lines()), 3)


class RefreshCostTest(unittest.TestCase):
    """Resolved records come from local artifacts; their PR metadata is final.

    A cold cache must therefore spend no ``gh pr view`` calls on them, and a
    second refresh inside the cache TTL spends no gh calls at all.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pilot = base / "pilot"
        self.state = base / "state"
        self.cache = base / "cache"
        (self.pilot / "merges").mkdir(parents=True)
        (self.pilot / "completed").mkdir()
        self.state.mkdir()
        (self.pilot / "state.json").write_text("{}")
        (self.pilot / "config.json").write_text('{"enabled": true}')
        (self.pilot / "merges" / "91.json").write_text(json.dumps({
            "issue": 91, "pr": 9091, "merged_sha": "a" * 40,
            "merged_at": T["10:00"]}))
        (self.pilot / "completed" / "91.json").write_text(json.dumps({
            "issue": 91, "pr": 9091, "merged_sha": "a" * 40,
            "completed_at": T["10:20"]}))
        (self.pilot / "merges" / "92.json").write_text(json.dumps({
            "issue": 92, "pr": 9092, "merged_sha": "b" * 40,
            "merged_at": T["10:00"]}))
        self.config = make_config(self.cache)
        self.config["pilot_home"] = str(self.pilot)
        self.config["state_dir"] = str(self.state)
        self.calls = []

    def runner(self, args):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return "[]"
        if args[:2] == ["issue", "view"]:
            return json.dumps({"state": "OPEN", "closedAt": None})
        if args[:2] == ["pr", "view"]:
            return json.dumps(PR201)
        raise AssertionError("unexpected gh invocation: %r" % (args,))

    def snapshot(self):
        return progress_data.snapshot(
            self.config, clock=lambda: 1000.0,
            runner=self.runner, http=lambda url: (200, "{}"))

    def pr_views(self):
        return [call for call in self.calls if call[:2] == ["pr", "view"]]

    def test_cold_refresh_does_not_fetch_resolved_pr_metadata(self):
        self.snapshot()
        self.assertEqual(self.pr_views(), [])
        # Issue list plus the open #92 closure probe; no PR metadata at all.
        self.assertEqual(len(self.calls), 2)

    def test_second_refresh_within_ttl_makes_no_gh_calls(self):
        self.snapshot()
        before = len(self.calls)
        self.snapshot()
        self.assertEqual(len(self.calls), before)

    def test_persisted_pr_metadata_is_reused_without_a_fetch(self):
        (self.cache).mkdir(parents=True)
        (self.cache / "pr-9091.json").write_text(json.dumps({
            "at": 0, "data": PR201}))
        snap = self.snapshot()
        record = {item["issue"]: item for item in snap["attempts"]}[
            "acme/widgets#91"]
        self.assertIn("reviewed",
                      [entry["stage"] for entry in record["history"]])
        self.assertEqual(self.pr_views(), [])


class HistorylessRecordTest(unittest.TestCase):
    """A record with no timestamp is counted, never timed, and gets no
    invented stage or poll-time activity stamp."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pilot = base / "pilot"
        self.state = base / "state"
        self.cache = base / "cache"
        (self.pilot / "merges").mkdir(parents=True)
        (self.pilot / "completed").mkdir()
        self.state.mkdir()
        (self.pilot / "state.json").write_text(
            json.dumps({"claims": {"82": {}}}))
        (self.pilot / "config.json").write_text('{"enabled": true}')
        (self.pilot / "completed" / "80.json").write_text(
            json.dumps({"issue": 80}))
        (self.pilot / "merges" / "81.json").write_text(
            json.dumps({"issue": 81}))
        (self.state / "ombra-gh-83.status").write_text("no timestamp here\n")
        self.config = make_config(self.cache)
        self.config["pilot_home"] = str(self.pilot)
        self.config["state_dir"] = str(self.state)

    def runner(self, args):
        if list(args)[:2] == ["issue", "view"]:
            return json.dumps({"state": "OPEN", "closedAt": None})
        return "[]"

    def snapshot(self):
        return progress_data.snapshot(
            self.config, clock=lambda: 1000.0,
            runner=self.runner, http=lambda url: (200, "{}"))

    def test_stage_comes_from_evidence_and_last_active_is_null(self):
        snap = self.snapshot()
        by_issue = {item["issue"]: item for item in snap["attempts"]}
        self.assertEqual(by_issue["acme/widgets#80"]["stage"], "delivered")
        self.assertEqual(by_issue["acme/widgets#81"]["stage"], "merged")
        self.assertEqual(by_issue["acme/widgets#82"]["stage"], "claimed")
        self.assertIsNone(by_issue["acme/widgets#83"]["stage"])
        for number in (80, 81, 82, 83):
            record = by_issue["acme/widgets#%d" % number]
            self.assertEqual(record["history"], [])
            self.assertIsNone(record["last_active"])

    def test_find_review_pane_returns_the_newest_meta(self):
        older = self.state / "ombra-gh-90-review-code.meta"
        newer = self.state / "ombra-gh-90-review-browser.meta"
        older.write_text("herdr_pane_id=w1:p1\n")
        newer.write_text("herdr_pane_id=w1:p2\n")
        os.utime(older, (100, 100))
        os.utime(newer, (200, 200))
        self.assertEqual(
            progress_data.find_review_pane(self.state, 90), "w1:p2")
        os.utime(newer, (50, 50))  # newest-by-mtime, not by file name
        self.assertEqual(
            progress_data.find_review_pane(self.state, 90), "w1:p1")


class SourcesHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cache = self.base / "cache"

    def config(self, pilot, state, staging="https://example.test/api/health"):
        config = make_config(self.cache)
        config["pilot_home"] = str(pilot)
        config["state_dir"] = str(state)
        if staging is None:
            config.pop("staging_url", None)
        else:
            config["staging_url"] = staging
        return config

    def snapshot(self, config, runner=None, http=None):
        return progress_data.snapshot(
            config, clock=lambda: 1000.0,
            runner=runner or (lambda args: "[]"),
            http=http or (lambda url: (200, '{"build": "dev+229b7447cf7b"}')))

    def test_missing_sources_are_not_reported_as_ok(self):
        snap = self.snapshot(self.config(
            self.base / "no-pilot", self.base / "no-state"))
        self.assertEqual(snap["attempts"], [])
        self.assertEqual(snap["sources"]["attempts"], "missing")

    def test_present_but_empty_sources_are_ok(self):
        pilot = self.base / "pilot"
        state = self.base / "state"
        (pilot / "merges").mkdir(parents=True)
        (pilot / "completed").mkdir()
        (pilot / "state.json").write_text("{}")
        state.mkdir()
        snap = self.snapshot(self.config(pilot, state))
        self.assertEqual(snap["attempts"], [])
        self.assertEqual(snap["sources"]["attempts"], "ok")

    def test_unreadable_source_is_reported(self):
        pilot = self.base / "pilot"
        pilot.mkdir()
        (pilot / "state.json").write_text("{}")
        state = self.base / "state-is-a-file"
        state.write_text("not a directory")
        snap = self.snapshot(self.config(pilot, state))
        self.assertEqual(snap["sources"]["attempts"], "unreadable")

    def test_backlog_and_staging_health(self):
        pilot = self.base / "pilot"
        state = self.base / "state"
        pilot.mkdir()
        (pilot / "state.json").write_text("{}")
        state.mkdir()

        def failing(args):
            raise RuntimeError("gh down")

        snap = self.snapshot(self.config(pilot, state), runner=failing)
        self.assertEqual(snap["sources"]["backlog"], "unreadable")
        flat = self.snapshot(
            self.config(pilot, state),
            http=lambda url: (_ for _ in ()).throw(OSError("down")))
        self.assertEqual(flat["sources"]["staging"], "unreadable")
        absent = self.snapshot(self.config(pilot, state, staging=None))
        self.assertEqual(absent["sources"]["staging"], "missing")


class ClaimBackfillTest(unittest.TestCase):
    """completed/<n>.json claim.claimed_at backfills the claimed stage so the
    work phase (claimed -> receipted) is timeable for delivered records."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pilot = base / "pilot"
        self.state = base / "state"
        self.cache = base / "cache"
        (self.pilot / "merges").mkdir(parents=True)
        (self.pilot / "completed").mkdir()
        self.state.mkdir()
        (self.pilot / "state.json").write_text("{}")
        (self.pilot / "config.json").write_text('{"enabled": true}')
        (self.pilot / "merges" / "70.json").write_text(json.dumps({
            "issue": 70, "pr": 9070, "merged_sha": "c" * 40,
            "merged_at": T["10:00"]}))
        (self.pilot / "completed" / "70.json").write_text(json.dumps({
            "issue": 70, "pr": 9070, "merged_sha": "c" * 40,
            "completed_at": T["10:20"],
            "claim": {"claimed_at": T["09:00"]}}))
        (self.pilot / "spans.jsonl").write_text(json.dumps(
            span(70, "review", T["09:30"])) + "\n")
        self.config = make_config(self.cache)
        self.config["pilot_home"] = str(self.pilot)
        self.config["state_dir"] = str(self.state)

    def test_delivered_record_gets_claimed_from_the_completion_receipt(self):
        snap = progress_data.snapshot(
            self.config, clock=lambda: 1000.0,
            runner=lambda args: "[]", http=lambda url: (200, "{}"))
        record = snap["attempts"][0]
        self.assertEqual(pairs(record["history"])[:2], [
            ("claimed", T["09:00"]),
            ("receipted", T["09:30"]),
        ])
        from ops import progress as view
        self.assertEqual(view.phases(record, view.parse_iso(T["11:00"]))[
            "work"], 1800.0)


class ConfigFaultTest(unittest.TestCase):
    """F1: a named or existing config that cannot be used is never ignored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FM_PROGRESS_CONFIG", None)
        os.environ.pop("FM_PROGRESS_CACHE", None)

    def write(self, name, text):
        path = self.base / name
        path.write_text(text)
        return path

    def test_missing_explicit_env_config_raises_and_cli_exits_two(self):
        os.environ["FM_PROGRESS_CONFIG"] = str(self.base / "absent.json")
        with self.assertRaises(progress_data.ConfigError) as raised:
            progress_data.load_config()
        self.assertIn("No such file", str(raised.exception))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = progress_data.main([])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        lines = [line for line in err.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^progress: config: .*No such file")

    def test_malformed_explicit_argument_raises(self):
        path = self.write("broken.json", "{ this is not json\n")
        with self.assertRaises(progress_data.ConfigError) as raised:
            progress_data.load_config(str(path))
        self.assertIn("malformed JSON", str(raised.exception))

    def test_malformed_default_path_config_is_an_error_too(self):
        (self.base / "config").mkdir()
        self.write("config/progress.json", "{ nope\n")
        with mock.patch.object(progress_data, "ROOT", self.base):
            with self.assertRaises(progress_data.ConfigError) as raised:
                progress_data.load_config()
        self.assertIn("malformed JSON", str(raised.exception))

    def test_absent_default_config_keeps_the_frozen_defaults(self):
        with mock.patch.object(progress_data, "ROOT", self.base):
            config = progress_data.load_config()
        self.assertEqual(config["repos"], progress_data.DEFAULT_CONFIG["repos"])
        self.assertEqual(config["window_hours"],
                         progress_data.DEFAULT_CONFIG["window_hours"])

    def test_valid_explicit_config_overrides_and_expands_paths(self):
        path = self.write("config.json", json.dumps({
            "repos": ["acme/widgets"], "window_hours": 3,
            "cache_dir": "~/elsewhere"}))
        os.environ["FM_PROGRESS_CACHE"] = str(self.base / "cache")
        config = progress_data.load_config(str(path))
        self.assertEqual(config["repos"], ["acme/widgets"])
        self.assertEqual(config["window_hours"], 3)
        self.assertEqual(config["cache_dir"], str(self.base / "cache"))

    def test_non_object_config_is_reported_by_the_cli(self):
        path = self.write("list.json", "[]\n")
        os.environ["FM_PROGRESS_CONFIG"] = str(path)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = progress_data.main([])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertRegex(err.getvalue(),
                         r"^progress: config: .*not a JSON object\n$")
        self.assertEqual(progress_data.ConfigError.label, "config")


class PureToleranceTest(unittest.TestCase):
    """Additive: defensive branches of the pure helpers stay observable."""

    def test_at_key_sorts_unparseable_values_last(self):
        broken = progress_data._at_key(None)
        self.assertEqual(progress_data._at_key("bad"), broken)
        self.assertGreater(broken, progress_data._at_key(T["10:00"]))

    def test_stage_index_unknown_stage_sorts_after_known_ones(self):
        self.assertEqual(progress_data._stage_index("unknown"),
                         len(progress_data.STAGES))
        self.assertEqual(progress_data._stage_index("merged"),
                         progress_data.STAGES.index("merged"))

    def test_same_issue_compares_numerically_and_rejects_garbage(self):
        self.assertTrue(progress_data._same_issue("42", 42))
        self.assertFalse(progress_data._same_issue("widgets", 42))
        self.assertFalse(progress_data._same_issue(None, None))

    def test_parse_status_line_rejects_lines_without_an_at_group(self):
        self.assertIsNone(progress_data.parse_status_line(""))
        self.assertIsNone(progress_data.parse_status_line("hello"))
        self.assertIsNone(progress_data.parse_status_line(
            "working [note=no-stamp] : waiting"))

    def test_near_merge_and_delivery_pending_reject_bad_stamps(self):
        naive = "2026-10-04T10:00:00"
        self.assertFalse(progress_data._near_merge(None, T["10:00"], 60))
        self.assertFalse(progress_data._near_merge(T["10:00"], naive, 60))
        self.assertTrue(progress_data._near_merge(T["10:00"], T["10:00"], 60))
        self.assertFalse(progress_data._delivery_pending(None, 1000.0, 60))
        self.assertFalse(progress_data._delivery_pending(naive, 1000.0, 60))

    def test_derive_history_rejects_a_non_numeric_issue(self):
        self.assertEqual(progress_data.derive_history("widgets"), [])

    def test_rework_count_rejects_garbage_and_reads_both_sources(self):
        self.assertEqual(progress_data.rework_count("widgets"), 0)
        self.assertEqual(progress_data.rework_count(
            42, spans=[None, span(42, "review", T["09:40"], event="end",
                                  outcome="returned")]), 1)
        self.assertEqual(progress_data.rework_count(
            42, spans=[],
            statuses={"ombra-gh-42-review-code": [
                event("done", T["09:50"], "checks failed")]}), 1)

    def test_staging_sha_rejects_non_object_payloads(self):
        self.assertIsNone(progress_data.staging_sha("[]"))
        self.assertIsNone(progress_data.staging_sha('{"build": "dev"}'))

    def test_backlog_snapshot_rejects_a_non_list_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"cache_dir": tmp, "repos": ["acme/widgets"]}
            result = progress_data.backlog_snapshot(
                config, clock=lambda: 1000.0,
                runner=lambda args: '{"not": "a list"}')
        self.assertEqual(result, {"open": None, "source": "unavailable"})

    def test_read_history_ledger_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "history.jsonl").write_text(
                "not json\n" + json.dumps(
                    {"issue": "acme/widgets#1", "stage": "claimed",
                     "at": T["09:00"]}) + "\n")
            entries = progress_data.read_history_ledger(tmp)
        self.assertEqual(len(entries), 1)

    def test_repo_accepts_a_string_and_falls_back(self):
        self.assertEqual(progress_data._repo({"repos": "acme/widgets"}),
                         "acme/widgets")
        self.assertEqual(progress_data._repo({"repos": []}),
                         progress_data.DEFAULT_CONFIG["repos"][0])
        self.assertEqual(progress_data._repo({}),
                         progress_data.DEFAULT_CONFIG["repos"][0])

    def test_issue_numbers_skips_unparseable_keys(self):
        issues = progress_data._issue_numbers(
            {"bad": {}}, {}, {"oops": {}}, {"7": {}})
        self.assertEqual(issues, [7])

    def test_merge_ledger_skips_unknown_released_and_contradicted_rows(self):
        attempts = [{"issue": "acme/widgets#1", "history": [],
                     "stage": None, "last_active": None}]
        ledger = [
            {"issue": "acme/widgets#9", "stage": "delivered",
             "at": T["09:00"]},
            {"issue": "acme/widgets#1", "stage": "incomplete",
             "at": T["09:10"]},
            {"issue": "acme/widgets#1", "stage": "incomplete",
             "at": T["09:20"]},
            {"issue": "acme/widgets#1", "stage": "incomplete",
             "at": T["09:25"]},
            {"issue": "acme/widgets#1", "stage": "delivered",
             "at": T["09:30"]},
        ]
        progress_data._merge_ledger(
            attempts, ledger,
            merges={"1": {"merged_at": T["10:00"]}},
            completed={"1": {"completed_at": T["09:40"]}},
            actions=[{"issue": "1", "at": T["09:10"],
                      "action": "release"}])
        self.assertEqual(pairs(attempts[0]["history"]),
                         [("delivered", T["09:30"])])
        self.assertEqual(attempts[0]["stage"], "delivered")


class ReaderFaultTest(unittest.TestCase):
    """Additive: unreadable inputs degrade instead of raising."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_write_json_swallows_oserror(self):
        blocker = self.base / "blocker"
        blocker.write_text("x")
        progress_data._write_json(blocker / "nested" / "cache.json", {"a": 1})
        self.assertFalse((blocker / "nested").exists())

    def test_read_json_dir_swallows_glob_oserror(self):
        with mock.patch.object(Path, "glob", side_effect=OSError("denied")):
            self.assertEqual(progress_data._read_json_dir(self.base), {})

    def test_append_history_swallows_oserror(self):
        blocker = self.base / "blocker"
        blocker.write_text("x")
        progress_data.append_history(
            blocker, [{"issue": "acme/widgets#1", "stage": "claimed",
                       "at": T["09:00"]}])
        self.assertTrue(blocker.is_file())

    def test_read_spans_skips_malformed_lines(self):
        pilot = self.base / "pilot"
        pilot.mkdir()
        (pilot / "spans.jsonl").write_text(
            "{bad}\n" + json.dumps(span(1, "review", T["09:00"])) + "\n")
        spans = progress_data.read_spans(pilot)
        self.assertEqual(len(spans), 1)

    def test_read_statuses_swallows_glob_oserror(self):
        with mock.patch.object(Path, "glob", side_effect=OSError("denied")):
            self.assertEqual(progress_data.read_statuses(self.base), {})

    def test_find_review_pane_handles_glob_and_stat_failures(self):
        with mock.patch.object(Path, "glob", side_effect=OSError("denied")):
            self.assertIsNone(progress_data.find_review_pane(self.base, 7))

        state = self.base / "state"
        state.mkdir()
        (state / "ombra-gh-7.meta").write_text("review_pane=ignored\n")
        (state / "ombra-gh-7-review-empty.meta").write_text("stage=x\n")
        (state / "ombra-gh-7-review-code.meta").write_text(
            "review_pane=pane-9\n")
        real_stat = Path.stat

        def flaky_stat(path, *args, **kwargs):
            if path.name == "ombra-gh-7-review-code.meta":
                raise OSError("gone")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", flaky_stat):
            self.assertEqual(
                progress_data.find_review_pane(state, 7), "pane-9")

    def test_dir_health_reports_unreadable_on_iterdir_failure(self):
        with mock.patch.object(Path, "iterdir",
                               side_effect=OSError("denied")):
            self.assertEqual(progress_data._dir_health(self.base),
                             "unreadable")

    def test_attempts_health_reports_a_malformed_state_json(self):
        pilot = self.base / "pilot"
        state = self.base / "state"
        pilot.mkdir()
        state.mkdir()
        (pilot / "state.json").write_text("{ nope")
        self.assertEqual(
            progress_data._attempts_health(pilot, state), "unreadable")


class GhHttpSeamTest(unittest.TestCase):
    """Additive: the real gh/http seams are exercised with injected fakes."""

    def test_gh_returns_stdout(self):
        completed = mock.Mock(stdout="[]\n")
        with mock.patch.object(progress_data.subprocess, "run",
                               return_value=completed) as run:
            self.assertEqual(progress_data._gh(["issue", "list"]), "[]\n")
        self.assertEqual(run.call_args.kwargs["check"], True)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_http_returns_status_and_body(self):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"build": "dev+229b7447cf7b"}'
        response.__enter__.return_value = response
        with mock.patch.object(progress_data.urllib.request, "urlopen",
                               return_value=response):
            status, body = progress_data._http("http://example.test")
        self.assertEqual(status, 200)
        self.assertIn("dev+229b7447cf7b", body)

    def test_http_returns_the_error_code_on_http_error(self):
        error = progress_data.urllib.error.HTTPError(
            "http://example.test", 503, "down", {}, None)
        with mock.patch.object(progress_data.urllib.request, "urlopen",
                               side_effect=error):
            self.assertEqual(progress_data._http("http://example.test"),
                             (503, ""))


class PrFetchTest(unittest.TestCase):
    """Additive: cache reuse and failure fallbacks of the PR metadata fetch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.config = {"repos": ["acme/widgets"], "cache_dir": str(self.cache)}
        self.payload = {"createdAt": T["09:00"], "mergedAt": None}

    def failing(self, args):
        raise RuntimeError("gh down")

    def test_cold_fetch_writes_the_cache_and_warm_fetch_reuses_it(self):
        def runner(args):
            self.assertEqual(args[:2], ["pr", "view"])
            return json.dumps(self.payload)

        self.assertEqual(
            progress_data._fetch_pr(5, self.config, lambda: 1000.0, runner,
                                    str(self.cache)),
            self.payload)
        cached = json.loads((self.cache / "pr-5.json").read_text())
        self.assertEqual(cached, {"at": 1000.0, "data": self.payload})
        self.assertEqual(
            progress_data._fetch_pr(5, self.config, lambda: 1200.0,
                                    self.failing, str(self.cache)),
            self.payload)

    def test_stale_cache_is_returned_when_the_fetch_fails(self):
        progress_data._write_json(self.cache / "pr-5.json",
                                  {"at": 1000.0, "data": self.payload})
        self.assertEqual(
            progress_data._fetch_pr(5, self.config, lambda: 99999.0,
                                    self.failing, str(self.cache)),
            self.payload)

    def test_non_object_payload_is_treated_as_a_failed_fetch(self):
        self.assertIsNone(
            progress_data._fetch_pr(5, self.config, lambda: 1000.0,
                                    lambda args: "[]", str(self.cache)))

    def test_issue_closure_rejects_a_non_object_payload(self):
        self.assertIsNone(
            progress_data._fetch_issue_closure(
                5, self.config, lambda: 1000.0, lambda args: "[]",
                str(self.cache)))


class SnapshotFaultTest(unittest.TestCase):
    """Additive: degraded clocks, configs and fetches never crash a snapshot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pilot = self.base / "pilot"
        self.state = self.base / "state"
        self.cache = self.base / "cache"
        for path in (self.pilot / "merges", self.pilot / "completed",
                     self.state):
            path.mkdir(parents=True)
        (self.pilot / "state.json").write_text("{}")
        self.config = make_config(self.cache)
        self.config["pilot_home"] = str(self.pilot)
        self.config["state_dir"] = str(self.state)
        self.config.pop("staging_url", None)

    def test_a_broken_clock_falls_back_to_wall_time(self):
        def broken_clock():
            raise RuntimeError("no clock")

        snap = progress_data.snapshot(
            self.config, clock=broken_clock, runner=lambda args: "[]")
        self.assertTrue(snap["generated"])

    def test_a_bad_merge_grace_falls_back_to_the_frozen_grace(self):
        self.config["merge_close_grace_seconds"] = "soon"
        snap = progress_data.snapshot(
            self.config, clock=lambda: 1000.0, runner=lambda args: "[]")
        self.assertIn("attempts", snap)

    def test_unresolved_prs_use_the_fetch_and_survive_failures(self):
        (self.pilot / "merges" / "5.json").write_text(json.dumps(
            {"issue": 5, "pr": 505}))

        def runner(args):
            if args[:2] == ["pr", "view"]:
                return json.dumps({"createdAt": T["09:00"]})
            return "[]"

        snap = progress_data.snapshot(
            self.config, clock=lambda: 1000.0, runner=runner)
        self.assertEqual(snap["attempts"][0]["pr"], 505)

        def failing(args):
            if args[:2] == ["pr", "view"]:
                raise RuntimeError("gh down")
            return "[]"

        snap = progress_data.snapshot(
            self.config, clock=lambda: 1000.0, runner=failing)
        self.assertEqual(snap["attempts"][0]["pr"], 505)

    def test_an_attempt_derivation_failure_keeps_an_evidence_record(self):
        (self.pilot / "merges" / "6.json").write_text(json.dumps(
            {"issue": 6, "merged_at": T["09:00"], "merged_sha": "b" * 40}))
        with mock.patch.object(progress_data, "_attempt",
                               side_effect=RuntimeError("boom")):
            snap = progress_data.snapshot(
                self.config, clock=lambda: 1000.0, runner=lambda args: "[]")
        self.assertEqual(snap["attempts"][0]["stage"], "merged")
        self.assertEqual(snap["attempts"][0]["history"], [])

    def test_a_raising_pr_fetch_is_contained_per_issue(self):
        (self.pilot / "merges" / "8.json").write_text(json.dumps(
            {"issue": 8, "pr": 808}))
        with mock.patch.object(progress_data, "_fetch_pr",
                               side_effect=RuntimeError("boom")):
            snap = progress_data.snapshot(
                self.config, clock=lambda: 1000.0, runner=lambda args: "[]")
        self.assertEqual(snap["attempts"][0]["pr"], 808)

    def test_a_completed_record_with_a_non_terminal_attempt_is_not_closed(self):
        (self.pilot / "completed" / "9.json").write_text(json.dumps(
            {"issue": 9, "completed_at": T["09:00"]}))
        with mock.patch.object(progress_data, "_attempt",
                               return_value={
                                   "issue": "acme/widgets#9",
                                   "stage": "claimed", "pr": None,
                                   "history": [], "deployed_sha": None,
                                   "waiting": None, "review_pane": None,
                                   "rework": 0, "last_active": None}):
            snap = progress_data.snapshot(
                self.config, clock=lambda: 1000.0, runner=lambda args: "[]")
        self.assertEqual(snap["attempts"][0]["stage"], "claimed")

    def test_closure_without_a_timestamp_and_bad_pilot_config(self):
        (self.pilot / "merges" / "7.json").write_text(json.dumps(
            {"issue": 7}))

        def runner(args):
            if args[:2] == ["issue", "view"]:
                return json.dumps({"state": "CLOSED"})
            return "[]"

        with mock.patch.object(progress_data, "_pilot_config",
                               side_effect=RuntimeError("boom")):
            snap = progress_data.snapshot(
                self.config, clock=lambda: 1000.0, runner=runner)
        self.assertEqual(snap["capacity"]["ceiling"]["issue_limit"], 4)

    def test_invalid_pilot_limit_and_backlog_failure_degrade(self):
        (self.pilot / "config.json").write_text(
            json.dumps({"max_active": "lots"}))
        with mock.patch.object(progress_data, "backlog_snapshot",
                               side_effect=RuntimeError("boom")):
            snap = progress_data.snapshot(
                self.config, clock=lambda: 1000.0, runner=lambda args: "[]")
        self.assertEqual(snap["backlog"], {"open": None,
                                           "source": "unavailable"})
        self.assertEqual(snap["capacity"]["ceiling"]["issue_limit"], 4)

    def test_non_numeric_and_failing_http_status_render_as_error(self):
        self.config["staging_url"] = "http://example.test/health"
        snap = progress_data.snapshot(
            self.config, clock=lambda: 1000.0, runner=lambda args: "[]",
            http=lambda url: ("weird", "{}"))
        self.assertEqual(snap["staging"]["status"], "ERROR")
        snap = progress_data.snapshot(
            self.config, clock=lambda: 1000.0, runner=lambda args: "[]",
            http=lambda url: (_ for _ in ()).throw(OSError("down")))
        self.assertEqual(snap["staging"]["status"], "ERROR")

    def test_waiting_reason_comes_from_the_pilot_phase(self):
        (self.pilot / "merges" / "42.json").write_text(json.dumps(
            {"issue": 42}))
        (self.pilot / "state.json").write_text(json.dumps({
            "phases": {"42": {"phase": "waiting",
                              "reason_code": "no-slot"}}}))
        snap = progress_data.snapshot(
            self.config, clock=lambda: 1000.0, runner=lambda args: "[]")
        self.assertEqual(snap["attempts"][0]["waiting"], "no-slot")

        (self.pilot / "state.json").write_text(json.dumps({
            "phases": {"42": {"state": "waiting",
                              "reason_code": "review-busy"}}}))
        snap = progress_data.snapshot(
            self.config, clock=lambda: 1000.0, runner=lambda args: "[]")
        self.assertEqual(snap["attempts"][0]["waiting"], "review-busy")


class CliTest(unittest.TestCase):
    """Additive: the adapter CLI prints the snapshot and exits 0."""

    def test_main_emits_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"pilot_home": tmp, "state_dir": tmp,
                      "cache_dir": tmp}
            with mock.patch.object(progress_data, "load_config",
                                   return_value=config), \
                    mock.patch.object(progress_data, "_gh",
                                      side_effect=RuntimeError("no gh")), \
                    mock.patch("sys.stdout", io.StringIO()) as out:
                code = progress_data.main([])
        self.assertEqual(code, 0)
        self.assertIn('"attempts"', out.getvalue())

    def test_module_entry_reports_the_config_fault(self):
        import runpy
        import warnings
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "absent.json")
            err = io.StringIO()
            with mock.patch.dict(os.environ,
                                 {"FM_PROGRESS_CONFIG": missing}), \
                    mock.patch("sys.stderr", err), \
                    warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_module("ops.progress_data",
                                     run_name="__main__")
        self.assertEqual(raised.exception.code, 2)
        self.assertRegex(err.getvalue(), r"^progress: config: ")


if __name__ == "__main__":
    unittest.main()
