"""Behavioural tests for the progress view pure functions (roadmap T1).

These tests are written before ``src/ops/progress.py`` exists.  They import
only the public names frozen by the roadmap and assert observable behaviour;
nothing here knows about the implementation's private structure.
"""

import io
import os
import re
import sys
import tempfile
import unittest
import warnings
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ops import progress  # noqa: E402  (this import is the intended red state)

UTC = timezone.utc
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def attempt(issue="acme/widgets#1", stage="claimed", history=None, **extra):
    record = {
        "issue": issue,
        "stage": stage,
        "pr": None,
        "history": list(history or []),
        "deployed_sha": None,
        "waiting": None,
        "review_pane": None,
        "rework": 0,
        "last_active": None,
    }
    record.update(extra)
    return record


def history_from(start, pairs):
    return [
        {"stage": stage, "at": iso(start + timedelta(seconds=offset))}
        for stage, offset in pairs
    ]


def snapshot(attempts=None, paused=False, open_issues=5, staging_sha="229b7447"):
    return {
        "generated": iso(NOW),
        "attempts": list(attempts or []),
        "capacity": {"active_issues": 1, "ceiling": {"issue_limit": 4}},
        "paused": {"paused": paused},
        "backlog": {"open": open_issues, "source": "live"},
        "staging": {
            "url": "https://example.test/api/health",
            "deployed_sha": staging_sha,
        },
    }


class ParseIsoTest(unittest.TestCase):
    def test_parses_utc_stamp_with_and_without_fraction(self):
        self.assertEqual(
            progress.parse_iso("2026-10-04T10:00:00Z"),
            datetime(2026, 10, 4, 10, 0, 0, tzinfo=UTC),
        )
        fractional = progress.parse_iso("2026-10-04T10:00:00.500000Z")
        self.assertEqual(fractional.replace(microsecond=0),
                         datetime(2026, 10, 4, 10, 0, 0, tzinfo=UTC))
        self.assertEqual(fractional.microsecond, 500000)
        self.assertEqual(
            progress.parse_iso("2026-10-04T12:00:00+02:00"),
            datetime(2026, 10, 4, 10, 0, 0, tzinfo=UTC),
        )


class DurationTest(unittest.TestCase):
    def test_minutes_below_ninety(self):
        self.assertEqual(progress.duration(42 * 60), "42m")
        self.assertEqual(progress.duration(89 * 60), "89m")

    def test_hours_at_ninety_minutes_and_above(self):
        self.assertEqual(progress.duration(90 * 60), "1.5h")
        self.assertEqual(progress.duration(102 * 60), "1.7h")


class PhasesTest(unittest.TestCase):
    def test_phases_use_first_occurrence_of_each_stage(self):
        start = NOW - timedelta(hours=2)
        record = attempt(history=history_from(start, [
            ("claimed", 0),
            ("receipted", 600),
            ("published", 900),
            # A later repeat of an earlier stage must not move the boundary.
            ("receipted", 1500),
            ("merged", 1800),
            ("delivered", 2100),
        ]))
        self.assertEqual(
            progress.phases(record, NOW),
            {"work": 600, "review": 300, "merge": 900, "deploy": 300},
        )

    def test_open_phase_of_active_record_runs_to_now(self):
        start = NOW - timedelta(minutes=20)
        record = attempt(stage="claimed", history=history_from(start, [
            ("claimed", 0),
        ]))
        values = progress.phases(record, NOW)
        self.assertEqual(values["work"], 20 * 60)
        self.assertIsNone(values["review"])
        self.assertIsNone(values["merge"])
        self.assertIsNone(values["deploy"])

    def test_open_phase_of_terminal_record_is_not_timed(self):
        start = NOW - timedelta(hours=2)
        record = attempt(stage="incomplete", history=history_from(start, [
            ("claimed", 0),
            ("incomplete", 600),
        ]))
        values = progress.phases(record, NOW)
        self.assertIsNone(values["work"])
        self.assertIsNone(values["review"])
        self.assertIsNone(values["merge"])
        self.assertIsNone(values["deploy"])

    def test_missing_end_never_reaches_now(self):
        start = NOW - timedelta(hours=1)
        record = attempt(stage="reviewing", history=history_from(start, [
            ("claimed", 0),
            ("receipted", 300),
        ]))
        values = progress.phases(record, NOW)
        self.assertEqual(values["work"], 300)
        self.assertEqual(values["review"], 3600 - 300)


class PhaseMediansTest(unittest.TestCase):
    def test_medians_over_completed_and_running_phases(self):
        start = NOW - timedelta(hours=2)
        quick = attempt(history=history_from(start, [
            ("claimed", 0), ("receipted", 600),
            ("published", 900),
            ("merged", 1500),
            ("delivered", 1800),
        ]))
        slow = attempt(history=history_from(start, [
            ("claimed", 0), ("receipted", 1200),
            ("published", 1500),
            ("merged", 2700),
            ("delivered", 3000),
        ]))
        running = attempt(stage="reviewing", history=history_from(start, [
            ("claimed", 0), ("receipted", 300),
        ]))
        medians = progress.phase_medians([quick, slow, running], NOW)
        self.assertEqual(medians["work"], 600)
        self.assertEqual(medians["review"], 300)
        self.assertEqual(medians["merge"], 900)
        self.assertEqual(medians["deploy"], 300)


class TotalsTest(unittest.TestCase):
    def test_window_limits_delivered_count_and_throughput(self):
        recent = attempt(
            issue="acme/widgets#1",
            stage="delivered",
            history=history_from(NOW - timedelta(hours=3), [
                ("claimed", 0), ("delivered", 3600),
            ]),
        )
        older = attempt(
            issue="acme/widgets#2",
            stage="delivered",
            history=history_from(NOW - timedelta(hours=22), [
                ("claimed", 0), ("delivered", 7200),
            ]),
        )
        snap = snapshot(attempts=[recent, older], open_issues=0)
        twelve = progress.totals(snap, NOW, window_hours=12)
        twenty_four = progress.totals(snap, NOW, window_hours=24)
        self.assertEqual(twelve["delivered_all"], 2)
        self.assertEqual(twelve["delivered_window"], 1)
        self.assertEqual(twenty_four["delivered_window"], 2)
        self.assertAlmostEqual(twelve["throughput_hour"], 1 / 12)

    def test_legacy_records_are_counted_but_never_timed(self):
        timed = attempt(
            issue="acme/widgets#1",
            stage="delivered",
            history=history_from(NOW - timedelta(hours=2), [
                ("claimed", 0), ("delivered", 3600),
            ]),
        )
        legacy = attempt(
            issue="acme/widgets#2",
            stage="delivered",
            history=[],
        )
        active = attempt(issue="acme/widgets#3", stage="claimed",
                         history=history_from(NOW - timedelta(minutes=10),
                                              [("claimed", 0)]))
        active_legacy = attempt(issue="acme/widgets#4", stage="claimed",
                                history=[])
        snap = snapshot(attempts=[timed, legacy, active, active_legacy],
                        open_issues=3)
        snap["capacity"] = {"active_issues": 2,
                            "ceiling": {"issue_limit": 4}}
        values = progress.totals(snap, NOW, window_hours=12)
        self.assertEqual(values["delivered_all"], 2)
        self.assertEqual(values["delivered_window"], 1)
        self.assertEqual(values["active"], 2)
        self.assertEqual(values["backlog"], 1)
        self.assertEqual(values["slots_used"], 2)
        self.assertEqual(values["slots_limit"], 4)
        self.assertEqual(values["median_cycle"], 3600)
        self.assertAlmostEqual(values["throughput_hour"], 1 / 12)
        self.assertAlmostEqual(values["eta_hours"], 12)

    def test_backlog_never_negative_and_missing_backlog_has_no_eta(self):
        snap = snapshot(attempts=[], open_issues=0)
        snap["capacity"] = {"active_issues": 3,
                            "ceiling": {"issue_limit": 4}}
        values = progress.totals(snap, NOW, window_hours=12)
        self.assertEqual(values["backlog"], 0)
        self.assertIsNone(values["eta_hours"])

        snap["backlog"] = {"open": None, "source": "unavailable"}
        values = progress.totals(snap, NOW, window_hours=12)
        self.assertIsNone(values["backlog"])
        self.assertIsNone(values["eta_hours"])


class StageViewTest(unittest.TestCase):
    def test_plain_words_for_each_stage(self):
        """Mission, LAYOUT 5: 'current stage in plain words (dispatched =
        "worker coding", or "rework N" after N review rounds; receipted =
        "checks/review queue"; reviewing = "in review"; reviewed/published =
        "approved, merging"; merged = "deploying"; rework = "waiting for
        slot")'.

        Earlier revisions of this test pinned "working"/"in review" for
        dispatched/receipted; the mission words are authoritative and this
        test now holds them exactly.
        """
        expectations = {
            "claimed": "claimed",
            "dispatched": "worker coding",
            "receipted": "checks/review queue",
            "reviewing": "in review",
            "reviewed": "approved, merging",
            "published": "approved, merging",
            "merged": "deploying",
            "rework": "waiting for slot",
            "delivered": "delivered",
            "incomplete": "incomplete",
        }
        for stage, words in expectations.items():
            with self.subTest(stage=stage):
                self.assertEqual(progress.stage_view(attempt(stage=stage)),
                                 words)

    def test_every_stage_word_is_exact(self):
        """No stage may fall back to a bare or invented word (mission 5)."""
        words = {
            stage: progress.stage_view(attempt(stage=stage))
            for stage in ("claimed", "dispatched", "receipted", "reviewing",
                          "reviewed", "published", "merged", "rework",
                          "delivered", "incomplete")
        }
        self.assertEqual(words["dispatched"], "worker coding")
        self.assertEqual(words["receipted"], "checks/review queue")
        self.assertEqual(words["rework"], "waiting for slot")
        self.assertEqual(len(set(words.values())), 9)

    def test_rework_count_is_spelled_out(self):
        rendered = progress.stage_view(
            attempt(stage="dispatched", rework=2)).lower()
        self.assertIn("worker coding", rendered)
        self.assertIn("rework 2", rendered)

    def test_zero_rework_is_not_shown(self):
        rendered = progress.stage_view(
            attempt(stage="dispatched", rework=0)).lower()
        self.assertNotIn("rework", rendered)


class TimelineRowsTest(unittest.TestCase):
    def _attempts(self, count):
        records = []
        for index in range(count):
            last = NOW - timedelta(minutes=index)
            records.append(attempt(
                issue="acme/widgets#%d" % (100 + index),
                stage="dispatched",
                history=[
                    {"stage": "claimed",
                     "at": iso(last - timedelta(minutes=5))},
                    {"stage": "dispatched", "at": iso(last)},
                ],
                last_active=iso(last),
            ))
        return records

    def test_twenty_most_recent_rows_and_older_count(self):
        result = progress.timeline_rows(self._attempts(25), NOW)
        rows = result["rows"]
        self.assertEqual(len(rows), 20)
        self.assertEqual(result["older"], 5)
        self.assertEqual(rows[0]["issue"], "acme/widgets#100")
        shown = {row["issue"] for row in rows}
        self.assertIn("acme/widgets#119", shown)
        self.assertNotIn("acme/widgets#120", shown)

    def test_short_timeline_has_no_older_count(self):
        result = progress.timeline_rows(self._attempts(3), NOW)
        self.assertEqual(len(result["rows"]), 3)
        self.assertEqual(result["older"], 0)


class RecentDeliveredTest(unittest.TestCase):
    def test_six_newest_deliveries(self):
        records = []
        for index in range(7):
            done = NOW - timedelta(minutes=index * 10)
            records.append(attempt(
                issue="acme/widgets#%d" % (200 + index),
                stage="delivered",
                history=[
                    {"stage": "claimed",
                     "at": iso(done - timedelta(hours=1))},
                    {"stage": "delivered", "at": iso(done)},
                ],
                pr=9000 + index,
            ))
        rows = progress.recent_delivered(records, NOW)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["issue"], "acme/widgets#200")
        self.assertNotIn("acme/widgets#206",
                         {row["issue"] for row in rows})


class RenderTest(unittest.TestCase):
    def _active(self):
        return attempt(
            issue="acme/widgets#42",
            stage="reviewing",
            history=history_from(NOW - timedelta(hours=1), [
                ("claimed", 0), ("receipted", 1200),
            ]),
            pr=629,
            review_pane="w4:p11",
            rework=1,
            last_active=iso(NOW - timedelta(minutes=30)),
        )

    def test_paused_only_when_factory_is_paused(self):
        text = progress.render(snapshot(attempts=[self._active()],
                                        paused=False), NOW, color=False)
        self.assertNotIn("PAUSED", text)
        paused = progress.render(snapshot(attempts=[self._active()],
                                          paused=True), NOW, color=False)
        self.assertIn("PAUSED", paused)

    def test_colour_is_gated(self):
        plain = progress.render(snapshot(attempts=[self._active()]), NOW,
                                color=False)
        coloured = progress.render(snapshot(attempts=[self._active()]), NOW,
                                   color=True)
        self.assertNotIn("\x1b[", plain)
        self.assertIn("\x1b[", coloured)

    def test_header_staging_and_active_row_are_present(self):
        text = progress.render(snapshot(attempts=[self._active()]), NOW,
                               color=False)
        self.assertRegex(text, r"\b\d{2}:\d{2}:\d{2}\b")
        self.assertIn("229b7447", text)
        self.assertIn("42", text)
        self.assertIn("rework 1", text.lower())

    def test_timeline_cap_renders_older_note(self):
        records = []
        for index in range(21):
            last = NOW - timedelta(minutes=index)
            records.append(attempt(
                issue="acme/widgets#%d" % (300 + index),
                stage="dispatched",
                history=[
                    {"stage": "claimed",
                     "at": iso(last - timedelta(minutes=5))},
                    {"stage": "dispatched", "at": iso(last)},
                ],
                last_active=iso(last),
            ))
        text = progress.render(snapshot(attempts=records), NOW, color=False)
        self.assertIn("(1 older not shown)", text)


class TolerantInputsTest(unittest.TestCase):
    """Additive: malformed stamps and counters degrade, never crash."""

    def test_parse_iso_returns_none_for_unparseable_and_assumes_utc(self):
        self.assertIsNone(progress.parse_iso("not-a-date"))
        self.assertIsNone(progress.parse_iso(12345))
        self.assertEqual(
            progress.parse_iso("2026-10-04T10:00:00"),
            datetime(2026, 10, 4, 10, 0, 0, tzinfo=UTC),
        )

    def test_totals_fall_back_on_non_numeric_capacity_values(self):
        snap = snapshot()
        snap["capacity"] = {"active_issues": "many", "slots_used": "few",
                            "ceiling": {"issue_limit": "n/a"}}
        values = progress.totals(snap, NOW, window_hours=12)
        self.assertEqual(values["active"], 0)
        self.assertEqual(values["slots_used"], 0)
        self.assertIsNone(values["slots_limit"])

    def test_stage_view_ignores_non_numeric_rework(self):
        rendered = progress.stage_view({"stage": "dispatched",
                                        "rework": "twice"})
        self.assertEqual(rendered, progress.STAGE_WORDS["dispatched"])

    def test_twenty_timed_rows_stop_before_untimed_overflow(self):
        timed = [attempt(
            issue="acme/widgets#%d" % (400 + index), stage="dispatched",
            history=history_from(NOW - timedelta(minutes=index + 1), [
                ("claimed", 0), ("dispatched", 30)]),
            last_active=iso(NOW - timedelta(minutes=index)))
            for index in range(20)]
        untimed = [attempt(issue="acme/widgets#%d" % (500 + index),
                           stage="dispatched", history=[])
                   for index in range(21)]
        result = progress.timeline_rows(timed + untimed, NOW,
                                        window_hours=12)
        self.assertEqual(len(result["rows"]), 20)
        self.assertEqual(result["older"], 21)
        self.assertTrue(all(row["timed"] for row in result["rows"]))

    def test_incomplete_stage_gets_the_cross_glyph(self):
        self.assertEqual(progress._glyph("incomplete"), "✗")
        record = attempt(
            issue="acme/widgets#903", stage="incomplete",
            history=history_from(NOW - timedelta(hours=1), [
                ("claimed", 0), ("incomplete", 600)]))
        text = progress.render(snapshot(attempts=[record]), NOW,
                               color=False)
        line = next(line for line in text.splitlines() if "#903" in line)
        self.assertIn("✗", line)

    def test_recent_delivered_skips_records_without_a_finish_stamp(self):
        record = attempt(issue="acme/widgets#904", stage="delivered",
                         history=[], last_active=None)
        self.assertEqual(progress.recent_delivered([record], NOW), [])

    def test_truncate_keeps_ansi_sequences_and_adds_a_reset(self):
        self.assertEqual(progress._truncate("abcdef", None), "abcdef")
        self.assertEqual(progress._truncate("abcdef", 0), "abcdef")
        self.assertEqual(progress._truncate("abcdef", 3), "abc")
        painted = "\x1b[31mabcdef\x1b[0m"
        cut = progress._truncate(painted, 3)
        self.assertTrue(cut.startswith("\x1b[31mabc"))
        self.assertTrue(cut.endswith("\x1b[0m"))


class LoopContainmentTest(unittest.TestCase):
    """Containment seam for the watch loop (B4 owns the implementation)."""

    def test_single_refresh_failure_is_one_line_and_exits_zero(self):
        out, err = io.StringIO(), io.StringIO()

        def broken():
            raise RuntimeError("source unavailable")

        code = progress.main(["--watch", "0"], fetch=broken,
                             sleep=lambda seconds: None, out=out, err=err)
        self.assertEqual(code, 0)
        lines = [line for line in err.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0],
                         r"^progress: \w+: source unavailable$")

    def test_broken_config_is_one_line_per_tick_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "progress.json"
            broken.write_text("{ this is not json\n")
            with mock.patch.dict(os.environ,
                                 {"FM_PROGRESS_CONFIG": str(broken)}):
                out, err = io.StringIO(), io.StringIO()
                code = progress.main(["--watch", "0"], out=out, err=err)
                self.assertEqual(code, 0)
                lines = [line for line in err.getvalue().splitlines()
                         if line.strip()]
                self.assertEqual(len(lines), 1)
                self.assertRegex(lines[0],
                                 r"^progress: config: .*malformed JSON: ")
                self.assertEqual(out.getvalue(), "")

                calls = {"sleep": 0}

                def interrupt_on_second_sleep(seconds):
                    calls["sleep"] += 1
                    if calls["sleep"] >= 2:
                        raise KeyboardInterrupt

                out, err = io.StringIO(), io.StringIO()
                code = progress.main(["--watch", "1"],
                                     sleep=interrupt_on_second_sleep,
                                     out=out, err=err)
        self.assertEqual(code, 0)
        lines = [line for line in err.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(line, r"^progress: config: ")
        self.assertNotIn("Traceback", err.getvalue())

    def test_coloured_refresh_clears_the_screen_and_stops_cleanly(self):
        out, err = io.StringIO(), io.StringIO()

        def fetch():
            return snapshot()

        def interrupt_in_sleep(seconds):
            raise KeyboardInterrupt

        code = progress.main(["--watch", "1", "--color", "always"],
                             fetch=fetch, sleep=interrupt_in_sleep,
                             out=out, err=err)
        self.assertEqual(code, 0)
        self.assertIn("\x1b[H\x1b[2J", out.getvalue())
        self.assertNotIn("\x1b[31m", err.getvalue())

    def test_keyboard_interrupt_during_fetch_exits_zero(self):
        def interrupt():
            raise KeyboardInterrupt

        code = progress.main(["--watch", "0"], fetch=interrupt,
                             out=io.StringIO(), err=io.StringIO())
        self.assertEqual(code, 0)

    def test_loop_keeps_ticking_after_failure(self):
        out, err = io.StringIO(), io.StringIO()
        calls = {"fetch": 0, "sleep": 0}

        def broken():
            calls["fetch"] += 1
            raise RuntimeError("still unavailable")

        def interrupt_on_second_sleep(seconds):
            calls["sleep"] += 1
            if calls["sleep"] >= 2:
                raise KeyboardInterrupt

        code = progress.main(["--watch", "1"], fetch=broken,
                             sleep=interrupt_on_second_sleep,
                             out=out, err=err)
        self.assertEqual(code, 0)
        self.assertEqual(calls["fetch"], 2)
        lines = [line for line in err.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.startswith("progress: "), line)

    def test_single_refresh_success_renders_once(self):
        out, err = io.StringIO(), io.StringIO()
        record = attempt(issue="acme/widgets#42", stage="claimed",
                         history=history_from(NOW - timedelta(minutes=5),
                                              [("claimed", 0)]))
        calls = []

        def fetch():
            calls.append(1)
            return snapshot(attempts=[record])

        code = progress.main(["--watch", "0", "--color", "never"],
                             fetch=fetch, sleep=lambda seconds: None,
                             out=out, err=err)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(out.getvalue().strip())
        self.assertEqual(err.getvalue(), "")


class StagingLineTest(unittest.TestCase):
    """Additive: staging.status renders before the deployed SHA."""

    def _staging_line(self, snap):
        text = progress.render(snap, NOW, color=False)
        return next(line for line in text.splitlines()
                    if line.startswith("staging"))

    def test_status_and_sha(self):
        snap = snapshot()
        snap["staging"]["status"] = "up (HTTP 200)"
        self.assertEqual(self._staging_line(snap),
                         "staging  up (HTTP 200) · 229b7447")

    def test_status_without_sha(self):
        snap = snapshot(staging_sha=None)
        snap["staging"]["status"] = "DOWN (HTTP 404)"
        self.assertEqual(self._staging_line(snap),
                         "staging  DOWN (HTTP 404) · n/a")

    def test_status_absent_keeps_plain_sha(self):
        snap = snapshot()
        snap["staging"].pop("status", None)
        self.assertEqual(self._staging_line(snap), "staging  229b7447")

    def test_error_status_renders(self):
        snap = snapshot()
        snap["staging"]["status"] = "ERROR"
        self.assertEqual(self._staging_line(snap), "staging  ERROR · 229b7447")


class ActiveTotalsRenderTest(unittest.TestCase):
    def test_active_issues_and_slots_are_separate_figures(self):
        """Mission 2: 'active; ... slots used/limit' are distinct figures."""
        first = attempt(issue="acme/widgets#1", stage="dispatched",
                        history=history_from(NOW - timedelta(hours=1),
                                             [("claimed", 0),
                                              ("dispatched", 60)]))
        second = attempt(issue="acme/widgets#2", stage="reviewing",
                         history=history_from(NOW - timedelta(hours=2),
                                              [("claimed", 0),
                                               ("receipted", 60)]))
        snap = snapshot(attempts=[first, second], open_issues=17)
        snap["capacity"] = {"active_issues": 2, "slots_used": 1,
                            "ceiling": {"issue_limit": 4}}
        text = progress.render(snap, NOW, color=False)
        line = next(line for line in text.splitlines()
                    if line.startswith("active  "))
        self.assertIn("2 issues", line)
        self.assertIn("slots 1/4", line)
        self.assertIn("backlog 15", line)


class UnknownSourceTest(unittest.TestCase):
    """Mission: the pane must never present an unknown as a zero."""

    def _snap(self, sources):
        delivered = attempt(
            issue="acme/widgets#1", stage="delivered",
            history=history_from(NOW - timedelta(hours=2), [
                ("claimed", 0), ("receipted", 600), ("published", 900),
                ("merged", 1500), ("delivered", 1800),
            ]),
            deployed_sha="c5e08414", pr=629)
        snap = snapshot(attempts=[delivered], open_issues=15)
        if sources is not None:
            snap["sources"] = sources
        return snap

    def _zero_lines(self, text):
        return [line for line in text.splitlines()
                if re.search(r"(?<![\d.])\b0(?![\d.])", line)]

    def test_missing_attempts_source_renders_unknown_with_one_note(self):
        text = progress.render(self._snap({"attempts": "missing"}),
                               NOW, color=False)
        self.assertEqual(self._zero_lines(text), [])
        self.assertEqual(text.count("attempts source"), 1)
        self.assertIn("n/a", text)
        self.assertIn("unknown", text)

    def test_unreadable_attempts_source_renders_unknown(self):
        snap = self._snap({"attempts": "unreadable"})
        values = progress.totals(snap, NOW, window_hours=12)
        for key in ("delivered_all", "delivered_window", "active",
                    "slots_used", "throughput_hour", "median_cycle",
                    "eta_hours", "backlog"):
            with self.subTest(key=key):
                self.assertIsNone(values[key])
        text = progress.render(snap, NOW, color=False)
        self.assertEqual(self._zero_lines(text), [])
        self.assertIn("attempts source unreadable", text)

    def test_absent_sources_is_treated_as_ok(self):
        text = progress.render(self._snap(None), NOW, color=False)
        self.assertIn("delivered  1 all", text)
        self.assertIn("active  0 issues · slots 1/4", text)
        self.assertNotIn("attempts source", text)


class TimelineWindowTest(unittest.TestCase):
    """Mission 6: 'one bar per issue across the window'."""

    def test_record_older_than_window_is_excluded(self):
        old = attempt(
            issue="acme/widgets#1", stage="delivered",
            history=history_from(NOW - timedelta(hours=30), [
                ("claimed", 0), ("delivered", 3600),
            ]),
            last_active=iso(NOW - timedelta(hours=29)))
        recent = attempt(
            issue="acme/widgets#2", stage="dispatched",
            history=history_from(NOW - timedelta(hours=1), [
                ("claimed", 0), ("dispatched", 60),
            ]),
            last_active=iso(NOW - timedelta(minutes=30)))
        result = progress.timeline_rows([old, recent], NOW, window_hours=12)
        self.assertEqual([row["issue"] for row in result["rows"]],
                         ["acme/widgets#2"])

    def test_spans_and_labels_are_clipped_to_the_window(self):
        record = attempt(
            issue="acme/widgets#3", stage="reviewing",
            history=history_from(NOW - timedelta(hours=20), [
                ("claimed", 0), ("receipted", 60),
            ]),
            last_active=iso(NOW - timedelta(minutes=5)))
        lo = NOW - timedelta(hours=6)
        result = progress.timeline_rows([record], NOW, window_hours=6)
        row = result["rows"][0]
        self.assertGreaterEqual(row["start"], lo)
        self.assertLessEqual(row["end"], NOW)
        for _, start, end in row["spans"]:
            self.assertGreaterEqual(start, lo)
            self.assertLessEqual(end, NOW)

        text = progress.render(
            snapshot(attempts=[record]), NOW, color=False)
        line = next(line for line in text.splitlines()
                    if "#3" in line and "→" in line)
        start_label = (NOW - timedelta(hours=12)).astimezone()
        self.assertIn(start_label.strftime("%H:%M") + "→", line)

    def test_historyless_record_has_no_timestamps(self):
        record = attempt(issue="acme/widgets#4", stage="dispatched",
                         history=[], last_active=None)
        result = progress.timeline_rows([record], NOW, window_hours=12)
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertFalse(row["timed"])
        self.assertIsNone(row["start"])
        self.assertIsNone(row["end"])
        snap = snapshot(attempts=[record])
        snap["generated"] = iso(NOW)
        text = progress.render(snap, NOW, color=False)
        section = text.split("timeline (last 20", 1)[1]
        timeline_line = next(line for line in section.splitlines()
                             if "#4" in line)
        self.assertNotRegex(timeline_line, r"\d{2}:\d{2}")
        self.assertIn("untimed", timeline_line)

    def test_mixed_null_and_timed_records_order_without_crashing(self):
        timed = attempt(
            issue="acme/widgets#5", stage="dispatched",
            history=history_from(NOW - timedelta(minutes=30), [
                ("claimed", 0), ("dispatched", 60),
            ]),
            last_active=iso(NOW - timedelta(minutes=10)))
        historyless = attempt(issue="acme/widgets#6", stage="dispatched",
                              history=[], last_active=None)
        text = progress.render(snapshot(attempts=[historyless, timed]),
                               NOW, color=False)
        self.assertIn("#5", text)
        self.assertIn("#6", text)


class TimelineBarContentTest(unittest.TestCase):
    """Mission 6: bars are coloured/glyphed by phase, so they cannot be flat."""

    def test_timeline_bar_shows_phase_glyphs(self):
        record = attempt(
            issue="acme/widgets#901", stage="delivered",
            history=history_from(NOW - timedelta(hours=8), [
                ("claimed", 0), ("receipted", 3600),
                ("published", 7200), ("merged", 10800),
                ("delivered", 14400),
            ]),
            deployed_sha="c5e08414", pr=629)
        text = progress.render(snapshot(attempts=[record]), NOW,
                               color=False)
        bar_line = next(line for line in text.splitlines()
                        if "#901" in line)
        self.assertIn("▒", bar_line)
        self.assertIn("▓", bar_line)
        self.assertIn("█", bar_line)


class RenderDetailsTest(unittest.TestCase):
    """Mission 5/7: the waiting reason and the deployed commit are shown."""

    def test_active_row_shows_waiting_reason(self):
        record = attempt(issue="acme/widgets#42", stage="dispatched",
                         history=history_from(NOW - timedelta(hours=1), [
                             ("claimed", 0), ("dispatched", 60),
                         ]),
                         waiting="blocked on CI")
        text = progress.render(snapshot(attempts=[record]), NOW,
                               color=False)
        line = next(line for line in text.splitlines() if "#42" in line)
        self.assertIn("waiting: blocked on CI", line)

    def test_recently_delivered_row_shows_deployed_sha(self):
        record = attempt(
            issue="acme/widgets#902", stage="delivered",
            history=history_from(NOW - timedelta(hours=2), [
                ("claimed", 0), ("delivered", 1800),
            ]),
            deployed_sha="c5e08414", pr=629)
        text = progress.render(snapshot(attempts=[record]), NOW,
                               color=False)
        section = text.split("recently delivered", 1)[1]
        line = next(line for line in section.splitlines() if "#902" in line)
        self.assertIn("c5e08414", line)



class ModuleEntryPointTest(unittest.TestCase):
    """Additive: ``python -m ops.progress`` surfaces a config fault and exits 0."""

    def test_module_entry_reports_the_config_fault(self):
        import runpy
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "absent.json")
            err = io.StringIO()
            argv = sys.argv
            sys.argv = ["progress", "--watch", "0"]
            try:
                with mock.patch.dict(os.environ,
                                     {"FM_PROGRESS_CONFIG": missing}), \
                        mock.patch("sys.stderr", err), \
                        warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    with self.assertRaises(SystemExit) as raised:
                        runpy.run_module("ops.progress", run_name="__main__")
            finally:
                sys.argv = argv
        self.assertEqual(raised.exception.code, 0)
        self.assertRegex(err.getvalue(), r"^progress: config: ")



class NarrowPaneTest(unittest.TestCase):
    """The view shares a herdr tab with the metrics dashboard (~73 columns)."""

    def test_fit_wraps_plain_lines_with_an_indent_and_cuts_coloured_ones(self):
        line = "  #351 · worker coding · " + "waiting: " + "word " * 30
        wrapped = progress._fit(line, 40).split("\n")
        self.assertGreater(len(wrapped), 1)
        self.assertTrue(all(len(row) <= 40 for row in wrapped))
        self.assertTrue(wrapped[1].startswith("      "))
        self.assertIn("word", wrapped[-1])
        painted = "\x1b[33m" + "x" * 50 + "\x1b[0m"
        self.assertEqual(progress._fit(painted, 10).count("\n"), 0)

    def test_bar_width_shrinks_to_fit_and_keeps_a_floor(self):
        self.assertEqual(progress._bar_width(100), progress._BAR_WIDTH)
        self.assertEqual(progress._bar_width(72), progress._BAR_WIDTH)
        self.assertEqual(progress._bar_width(50), 50 - progress._TIMELINE_FIXED)
        self.assertEqual(progress._bar_width(20), 8)

    def test_legend_names_every_phase_glyph_and_mark(self):
        plain = "\n".join(progress.legend(progress._Paint(False)))
        for word in ("▒ work", "▓ review", "█ merge", "█ deploy", "· no phase", "▶ in progress", "✓ delivered", "✗ incomplete"):
            self.assertIn(word, plain)
        self.assertIn("differ only by colour", plain)
        coloured = "\n".join(progress.legend(progress._Paint(True)))
        self.assertIn("\x1b[35m█ merge", coloured)
        self.assertIn("\x1b[32m█ deploy", coloured)
        self.assertNotIn("differ only by colour", coloured)

    def test_render_at_pane_width_never_exceeds_it(self):
        record = {"issue": "L0ngcat555/ombra#351", "stage": "dispatched",
                  "waiting": "a long explanation " * 8,
                  "history": [{"at": "2026-10-18T10:00:00Z", "stage": "claimed"},
                              {"at": "2026-10-18T10:05:00Z", "stage": "dispatched"}]}
        text = progress.render(snapshot(attempts=[record]), NOW, color=False, width=72)
        self.assertTrue(all(len(row) <= 72 for row in text.splitlines()), text)
        self.assertIn("bars ", text)


if __name__ == "__main__":
    unittest.main()
