"""Read-only terminal progress view for the issue -> merge -> deploy factory.

Pure functions take ``now``; the watch loop takes its fetch/sleep/out/err
seams so tests never touch the network, subprocesses or a real terminal.
"""

import argparse
import re
import shutil
import statistics
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone

TERMINAL = {"delivered", "incomplete"}

PHASES = (("work", "claimed", "receipted"),
          ("review", "receipted", "published"),
          ("merge", "published", "merged"),
          ("deploy", "merged", "delivered"))

STAGE_WORDS = {
    "claimed": "claimed",
    "dispatched": "worker coding",
    "receipted": "checks/review queue",
    "reviewing": "in review",
    "reviewed": "approved, merging",
    "published": "approved, merging",
    "merged": "deploying",
    "delivered": "delivered",
    "incomplete": "incomplete",
    "rework": "waiting for slot",
}

_COLOURS = {"red": 31, "green": 32, "yellow": 33, "magenta": 35,
            "cyan": 36, "dim": 2}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BAR_WIDTH = 28
# Columns a timeline row needs besides its bar: "  #NNNN " + " ▶  " + the
# widest "MM-DD HH:MM→MM-DD HH:MM" range.
_TIMELINE_FIXED = 35
_PHASE_COLOURS = {"work": "yellow", "review": "cyan",
                  "merge": "magenta", "deploy": "green"}
_PHASE_GLYPHS = {"work": "▒", "review": "▓", "merge": "█", "deploy": "█"}


def parse_iso(value):
    """Parse an ISO-8601 stamp into an aware UTC datetime (or None)."""
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def duration(seconds):
    """'42m' below 90 minutes, otherwise hours with one decimal ('1.5h')."""
    if seconds is None:
        return "n/a"
    seconds = max(0, float(seconds))
    if seconds < 90 * 60:
        return "%dm" % int(round(seconds / 60))
    return "%.1fh" % (seconds / 3600.0)


def _stamps(record):
    """Sorted (datetime, stage) pairs from a record's history."""
    pairs = []
    for entry in record.get("history") or []:
        when = parse_iso(entry.get("at"))
        if when is not None:
            pairs.append((when, entry.get("stage")))
    pairs.sort(key=lambda pair: pair[0])
    return pairs


def _first(pairs):
    firsts = {}
    for when, stage in pairs:
        firsts.setdefault(stage, when)
    return firsts


def _last_active(record):
    when = parse_iso(record.get("last_active"))
    if when is not None:
        return when
    pairs = _stamps(record)
    return pairs[-1][0] if pairs else None


def phases(record, now):
    """Seconds per phase using each stage's first occurrence.

    A phase with a start but no end runs to ``now`` for active records and
    is omitted for terminal ones.  Missing starts are never timed.
    """
    pairs = _stamps(record)
    firsts = _first(pairs)
    active = record.get("stage") not in TERMINAL
    values = {}
    for name, start_stage, end_stage in PHASES:
        start = firsts.get(start_stage)
        if start is None:
            values[name] = None
            continue
        end = firsts.get(end_stage)
        if end is None:
            end = now if active else None
        values[name] = None if end is None else max(0, (end - start).total_seconds())
    return values


def phase_medians(records, now):
    """Median seconds per phase across completed and running phases."""
    medians = {name: None for name, _, _ in PHASES}
    for name, _, _ in PHASES:
        observed = [value for value in
                    (phases(record, now).get(name) for record in records)
                    if value is not None]
        if observed:
            medians[name] = statistics.median(observed)
    return medians


def _delivered_at(record):
    pairs = _stamps(record)
    for when, stage in reversed(pairs):
        if stage == "delivered":
            return when
    return pairs[-1][0] if pairs else _last_active(record)


def _cycle(record):
    pairs = _stamps(record)
    if len(pairs) < 2:
        return None
    return (pairs[-1][0] - pairs[0][0]).total_seconds()


def _attempts_source(snapshot):
    """'ok', 'missing' or 'unreadable' for the attempts source.

    A foreign snapshot without the additive ``sources`` map is assumed ok.
    """
    sources = snapshot.get("sources")
    if not isinstance(sources, dict):
        return "ok"
    value = sources.get("attempts", "ok")
    return value if isinstance(value, str) else "ok"


def _capacity(snapshot):
    capacity = snapshot.get("capacity") or {}
    ceiling = capacity.get("ceiling") or {}
    return capacity, ceiling


def totals(snapshot, now, window_hours=12):
    """Headline counts: deliveries, window throughput, cycle, backlog ETA."""
    attempts = snapshot.get("attempts") or []
    delivered = [record for record in attempts
                 if record.get("stage") == "delivered"]
    cutoff = now.timestamp() - float(window_hours) * 3600
    in_window = 0
    for record in delivered:
        when = _delivered_at(record)
        if when is not None and when.timestamp() >= cutoff:
            in_window += 1

    cycles = [cycle for cycle in (_cycle(record) for record in delivered)
              if cycle is not None]
    median_cycle = statistics.median(cycles) if cycles else None

    capacity, ceiling = _capacity(snapshot)
    counted_active = sum(1 for record in attempts
                         if record.get("stage") not in TERMINAL)
    capacity_active = capacity.get("active_issues")
    try:
        capacity_active = int(capacity_active)
    except (TypeError, ValueError):
        capacity_active = counted_active
    slots_used = capacity.get("slots_used")
    if slots_used is None:
        slots_used = capacity_active
    try:
        slots_used = int(slots_used)
    except (TypeError, ValueError):
        slots_used = capacity_active
    try:
        slots_limit = int(ceiling.get("issue_limit"))
    except (TypeError, ValueError):
        slots_limit = None

    backlog = (snapshot.get("backlog") or {}).get("open")
    if backlog is not None:
        backlog = max(0, int(backlog) - capacity_active)
    throughput = in_window / float(window_hours) if window_hours else 0.0
    eta = None
    if backlog and throughput > 0:
        eta = backlog / throughput

    values = {
        "delivered_all": len(delivered),
        "delivered_window": in_window,
        "active": counted_active,
        "backlog": backlog,
        "slots_used": slots_used,
        "slots_limit": slots_limit,
        "throughput_hour": throughput,
        "median_cycle": median_cycle,
        "eta_hours": eta,
    }
    if _attempts_source(snapshot) != "ok":
        # An unreadable attempts source means "unknown", never zero.
        for key in ("delivered_all", "delivered_window", "active", "backlog",
                    "slots_used", "throughput_hour", "median_cycle",
                    "eta_hours"):
            values[key] = None
    return values


def stage_view(record):
    """Plain words for the current stage, plus ``rework N`` when present."""
    stage = record.get("stage")
    words = STAGE_WORDS.get(stage, str(stage or "unknown"))
    rework = record.get("rework") or 0
    try:
        rework = int(rework)
    except (TypeError, ValueError):
        rework = 0
    if rework > 0:
        words = "%s · rework %d" % (words, rework)
    return words


def _number(issue):
    return str(issue or "").rsplit("#", 1)[-1]


def _spans(record, now):
    """(phase, start, end) segments for the timeline bar."""
    firsts = _first(_stamps(record))
    active = record.get("stage") not in TERMINAL
    spans = []
    for name, start_stage, end_stage in PHASES:
        start = firsts.get(start_stage)
        if start is None:
            continue
        end = firsts.get(end_stage)
        if end is None:
            end = now if active else None
        if end is None or end < start:
            continue
        spans.append((name, start, end))
    return spans


def timeline_rows(attempts, now, window_hours=None):
    """The 20 most recently active records inside the selected window.

    ``window_hours`` bounds every bar: records last active before the window
    are excluded and spans are clipped to it, so labels stay inside it.  A
    record without history is never timed -- it gets ``timed=False`` and no
    start/end, never the snapshot's generated time.
    """
    cutoff = None
    if window_hours:
        cutoff = now - timedelta(hours=float(window_hours))

    timed_candidates = []
    untimed = []
    for record in attempts:
        pairs = _stamps(record)
        if not pairs:
            if record.get("stage") not in TERMINAL:
                untimed.append(record)
            continue
        when = _last_active(record) or pairs[-1][0]
        if cutoff is not None and when < cutoff:
            continue
        timed_candidates.append((when, record))
    timed_candidates.sort(key=lambda pair: pair[0], reverse=True)

    older = max(0, len(timed_candidates) + len(untimed) - 20)
    rows = []
    for when, record in timed_candidates[:20]:
        pairs = _stamps(record)
        start = pairs[0][0]
        end = max(when, pairs[-1][0])
        spans = []
        for name, span_start, span_end in _spans(record, now):
            begin = max(span_start, cutoff) if cutoff is not None else span_start
            stop = min(span_end, now)
            if stop > begin:
                spans.append((name, begin, stop))
        rows.append({
            "issue": record.get("issue"),
            "number": _number(record.get("issue")),
            "glyph": _glyph(record.get("stage")),
            "timed": True,
            "start": max(start, cutoff) if cutoff is not None else start,
            "end": min(end, now),
            "spans": spans,
            "stage": record.get("stage"),
        })
    for record in untimed:
        if len(rows) >= 20:
            break
        rows.append({
            "issue": record.get("issue"),
            "number": _number(record.get("issue")),
            "glyph": _glyph(record.get("stage")),
            "timed": False,
            "start": None,
            "end": None,
            "spans": [],
            "stage": record.get("stage"),
        })
    return {"rows": rows, "older": older}


def _glyph(stage):
    if stage == "delivered":
        return "✓"
    if stage == "incomplete":
        return "✗"
    return "▶"


def recent_delivered(attempts, now, limit=6):
    """Newest delivered records with PR, cycle, finish and deployed SHA."""
    rows = []
    for record in attempts:
        if record.get("stage") != "delivered":
            continue
        finish = _delivered_at(record)
        if finish is None:
            continue
        rows.append({
            "issue": record.get("issue"),
            "number": _number(record.get("issue")),
            "pr": record.get("pr"),
            "cycle": _cycle(record),
            "finish": finish,
            "deployed_sha": record.get("deployed_sha"),
        })
    rows.sort(key=lambda row: row["finish"], reverse=True)
    return rows[:limit]


class _Paint:
    def __init__(self, enabled):
        self.enabled = bool(enabled)

    def __call__(self, text, colour):
        if not self.enabled:
            return text
        return "\x1b[%dm%s\x1b[0m" % (_COLOURS[colour], text)


def _truncate(line, width):
    """Cut to ``width`` visible characters, keeping ANSI sequences intact."""
    if width is None or width <= 0:
        return line
    visible = 0
    index = 0
    while index < len(line):
        match = _ANSI.match(line, index)
        if match:
            index = match.end()
            continue
        if visible >= width:
            break
        visible += 1
        index += 1
    cut = line[:index]
    if index < len(line) and "\x1b[" in line:
        cut += "\x1b[0m"
    return cut


def _fit(line, width):
    """Wrap a plain line onto indented continuation rows; cut a coloured one."""
    if width is None or width <= 0 or len(_ANSI.sub("", line)) <= width:
        return line
    if "\x1b[" in line:
        return _truncate(line, width)
    indent = len(line) - len(line.lstrip(" ")) + 4
    return "\n".join(textwrap.wrap(line, width=width, subsequent_indent=" " * indent,
                                   break_long_words=True, break_on_hyphens=False))


def _bar_width(width):
    if width is None or width <= 0:
        return _BAR_WIDTH
    return max(8, min(_BAR_WIDTH, width - _TIMELINE_FIXED))


def legend(paint):
    """What the timeline glyphs and colours mean (merge and deploy differ by colour)."""
    bars = "  ".join(paint("%s %s" % (_PHASE_GLYPHS[name], name), _PHASE_COLOURS[name])
                     for name, _, _ in PHASES)
    return ["  bars   %s  %s" % (bars, paint("· no phase", "dim")),
            "  marks  ▶ in progress  ✓ delivered  ✗ incomplete" +
            ("" if paint.enabled else "  (merge and deploy differ only by colour)")]


def _bar(row, lo, hi, width=_BAR_WIDTH):
    span = max(1.0, (hi - lo).total_seconds())
    cells = [None] * width
    for name, start, end in row["spans"]:
        begin = int((start - lo).total_seconds() / span * width)
        stop = int((end - lo).total_seconds() / span * width)
        begin = max(0, min(width - 1, begin))
        stop = max(begin + 1, min(width, stop))
        for cell in range(begin, stop):
            cells[cell] = name
    return "".join(_PHASE_GLYPHS.get(cell, "·") for cell in cells), cells


def _paint_bar(row, lo, hi, paint, width=_BAR_WIDTH):
    raw, cells = _bar(row, lo, hi, width)
    colours = _PHASE_COLOURS
    if not paint.enabled:
        return raw
    out = []
    run, run_colour = [], None
    for char, cell in zip(raw, cells):
        colour = colours.get(cell)
        if colour != run_colour:
            if run:
                out.append(paint("".join(run), run_colour or "dim"))
            run, run_colour = [char], colour
        else:
            run.append(char)
    if run:
        out.append(paint("".join(run), run_colour or "dim"))
    return "".join(out)


def _clock(value, now=None):
    """HH:MM locally, prefixed with the date when it is not today."""
    local = value.astimezone()
    if now is not None and local.date() != now.astimezone().date():
        return local.strftime("%m-%d %H:%M")
    return local.strftime("%H:%M")


def render(snapshot, now, color=False, width=100):
    """Render all seven sections; ANSI only when ``color`` is true."""
    paint = _Paint(color)
    attempts = snapshot.get("attempts") or []
    window_hours = float(snapshot.get("window_hours") or 12)
    source = _attempts_source(snapshot)
    known = source == "ok"
    values = totals(snapshot, now, window_hours=window_hours)
    lines = []

    header = "factory progress  %s" % now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    if (snapshot.get("paused") or {}).get("paused"):
        header += "  " + paint("PAUSED", "red")
    lines.append(header)
    if not known:
        lines.append("note  attempts source %s — counts unknown" % source)

    limit = values["slots_limit"]
    used = values["slots_used"]
    slot_text = "n/a" if used is None else "%s/%s" % (
        used, "?" if limit is None else limit)
    if values["delivered_all"] is None:
        lines.append("delivered  n/a all · n/a/%gh" % window_hours)
    else:
        lines.append("delivered  %d all · %d/%gh" % (
            values["delivered_all"], values["delivered_window"],
            window_hours))
    active_text = ("unknown" if values["active"] is None
                   else "%d issues" % values["active"])
    lines.append("active  %s · slots %s · backlog %s" % (
        active_text, slot_text, "n/a" if values["backlog"] is None
        else values["backlog"]))
    if values["throughput_hour"] is None:
        lines.append("throughput n/a · median cycle n/a · ETA n/a")
    else:
        eta = "n/a" if values["eta_hours"] is None else duration(
            values["eta_hours"] * 3600)
        lines.append("throughput %.1f/h · median cycle %s · ETA %s" % (
            values["throughput_hour"],
            duration(values["median_cycle"]), eta))

    medians = phase_medians(attempts, now) if known else {}
    lines.append("median phase  " + " · ".join(
        "%s %s" % (name, duration(medians.get(name)))
        for name, _, _ in PHASES))

    staging = snapshot.get("staging") or {}
    sha = staging.get("deployed_sha") or "n/a"
    status = staging.get("status") or staging.get("state")
    lines.append("staging  %s" % ("%s · %s" % (status, sha) if status else sha))

    lines.append("active issues")
    if not known:
        lines.append("  (unknown)")
    else:
        active = [record for record in attempts
                  if record.get("stage") not in TERMINAL]
        active.sort(key=lambda record: _last_active(record)
                    or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for record in active:
            pairs = _stamps(record)
            in_stage = (now - pairs[-1][0]).total_seconds() if pairs else None
            total = (now - pairs[0][0]).total_seconds() if pairs else None
            bits = ["#%s" % _number(record.get("issue")),
                    stage_view(record),
                    duration(in_stage),
                    "total %s" % duration(total)]
            if record.get("pr"):
                bits.append("PR %s" % record["pr"])
            if record.get("review_pane"):
                bits.append(str(record["review_pane"]))
            if record.get("waiting"):
                bits.append("waiting: %s" % record["waiting"])
            lines.append("  " + " · ".join(bits))
        if not active:
            lines.append("  (none)")

    lines.append("timeline (last 20 · %gh window)" % window_hours)
    if not known:
        lines.append("  (unknown)")
    else:
        lines.extend(legend(paint))
        timeline = timeline_rows(attempts, now, window_hours=window_hours)
        rows = timeline["rows"]
        lo = now - timedelta(hours=window_hours)
        for row in rows:
            bar = _paint_bar(row, lo, now, paint, _bar_width(width))
            if row["timed"]:
                lines.append("  %-5s %s %s  %s→%s" % (
                    "#" + row["number"], bar, row["glyph"],
                    _clock(row["start"], now), _clock(row["end"], now)))
            else:
                lines.append("  %-5s %s %s  untimed" % (
                    "#" + row["number"], bar, row["glyph"]))
        if timeline["older"]:
            lines.append("  (%d older not shown)" % timeline["older"])

    lines.append("recently delivered")
    if not known:
        lines.append("  (unknown)")
    else:
        recent = recent_delivered(attempts, now)
        for row in recent:
            lines.append("  #%-5s PR %-5s cycle %-5s %s  %s" % (
                row["number"],
                row["pr"] if row["pr"] is not None else "—",
                duration(row["cycle"]), _clock(row["finish"], now),
                row["deployed_sha"] or "—"))
        if not recent:
            lines.append("  (none)")

    return "\n".join(_fit(line, width) for line in lines) + "\n"


def _default_fetch():
    from ops import progress_data
    return progress_data.snapshot()


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="progress")
    parser.add_argument("--watch", type=float, default=15.0,
                        help="refresh interval in seconds (0 = once)")
    parser.add_argument("--window", type=float, default=12.0,
                        help="delivery window in hours (default 12)")
    parser.add_argument("--color", choices=("auto", "always", "never"),
                        default="auto")
    return parser.parse_args(argv)


def main(argv=None, fetch=None, sleep=time.sleep, out=None, err=None,
         clock=None):
    """Watch loop; the failures are contained to one line per refresh."""
    args = _parse_args(sys.argv[1:] if argv is None else list(argv))
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    fetch = fetch or _default_fetch
    if args.color == "always":
        color = True
    elif args.color == "never":
        color = False
    else:
        color = bool(getattr(out, "isatty", lambda: False)())

    while True:
        try:
            snapshot = fetch()
            snapshot = dict(snapshot)
            snapshot.setdefault("window_hours", args.window)
            now = clock() if clock else datetime.now(timezone.utc)
            width = shutil.get_terminal_size((100, 40)).columns - 1
            text = render(snapshot, now, color=color, width=width)
            if color and args.watch > 0:
                out.write("\x1b[H\x1b[2J")
            out.write(text)
            out.flush()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # one line, keep the pane alive
            label = getattr(exc, "label", type(exc).__name__)
            err.write("progress: %s: %s\n" % (label, exc))
            err.flush()
        if args.watch <= 0:
            return 0
        try:
            sleep(args.watch)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
