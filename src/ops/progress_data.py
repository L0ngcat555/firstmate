"""Progress snapshot adapter.

Reads the factory's authoritative artifacts (pilot state, spans, merges,
completions, firstmate status files, PR metadata, staging health) and emits
the frozen snapshot contract consumed by ``ops.progress``.

Everything except :func:`snapshot`, the ledger append and the CLI is pure and
takes its inputs explicitly.  The factory is read-only here: the only writes
are the append-only ledger and the gh response cache under ``cache_dir``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_TTL = 300
MERGE_CLOSE_GRACE = 3600
REVIEW_PHASES = ("review", "code_review", "browser_review")
WORK_PHASES = ("setup", "implementation")
FAILED_OUTCOMES = ("returned", "failed", "rejected")
STAGES = ("claimed", "dispatched", "receipted", "reviewing", "reviewed",
          "published", "merged", "delivered", "incomplete", "rework")
TERMINAL_STAGES = ("delivered", "incomplete")

DEFAULT_CONFIG = {
    "repos": ["L0ngcat555/ombra"],
    "pilot_home": "/home/mw/ombra-pilot",
    "state_dir": "/home/mw/firstmate/state",
    "staging_url": "https://ombra-web-dev.ombra.workers.dev/api/health",
    "pane_label": "ombra · progress",
    "workspace_label": "ombra agents",
    "cache_dir": "~/.cache/fm-progress",
    "window_hours": 12,
    "merge_close_grace_seconds": MERGE_CLOSE_GRACE,
}

_TASK_ISSUE = re.compile(r"^ombra-gh-(\d+)(?:[-_.]|$)")
# A status line starts with an event name and carries one or more bracket
# groups; ``at=<epoch>`` may appear anywhere among them (real factory files
# have both ``[key=…] [at=…]`` and ``[at=…] [key=…]`` orders).
_STATUS_LINE = re.compile(r"^(\w[\w-]*)((?:\s+\[[^\]]*\])+)\s*:\s?(.*)$")
_AT_GROUP = re.compile(r"^at=(\d+)$")


# --------------------------------------------------------------------------
# small tolerant helpers


def utc_iso(epoch):
    """Epoch seconds -> ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _read_text(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _write_json(path, payload):
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        pass


def _at_key(value):
    """Sort key for ISO stamps; unparseable values sort last but stay stable."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)


def _stage_index(stage):
    try:
        return STAGES.index(stage)
    except ValueError:
        return len(STAGES)


def _earliest(values):
    values = [value for value in values if value]
    return min(values, key=_at_key) if values else None


def _same_issue(left, issue):
    try:
        return int(left) == int(issue)
    except (TypeError, ValueError):
        return False


def _sorted_history(stages):
    entries = [{"stage": stage, "at": at} for stage, at in stages.items() if at]
    entries.sort(key=lambda item: (_at_key(item["at"]),
                                  _stage_index(item["stage"])))
    return entries


# --------------------------------------------------------------------------
# pure readers (paths injected)


def task_issue(task):
    """``ombra-gh-42`` / ``ombra-gh-42-review-code`` -> 42."""
    match = _TASK_ISSUE.match(str(task or ""))
    return int(match.group(1)) if match else None


def parse_status_line(line):
    """Parse one firstmate status line, or return None when unrecognised."""
    match = _STATUS_LINE.match((line or "").strip())
    if not match:
        return None
    epoch = None
    for group in re.findall(r"\[([^\]]*)\]", match.group(2)):
        found = _AT_GROUP.match(group.strip())
        if found:
            epoch = found.group(1)
            break
    if epoch is None:
        return None
    return {
        "event": match.group(1),
        "at": utc_iso(int(epoch)),
        "note": match.group(3).strip(),
    }


def read_state(pilot_home):
    state = _read_json(Path(pilot_home) / "state.json")
    return state if isinstance(state, dict) else {}


def _claims_from(state):
    claims = state.get("claims") if isinstance(state, dict) else None
    if not isinstance(claims, dict):
        return {}
    return {str(key): value for key, value in claims.items()
            if isinstance(value, dict)}


def read_claims(pilot_home):
    return _claims_from(read_state(pilot_home))


def read_merges(pilot_home):
    return _read_json_dir(Path(pilot_home) / "merges")


def read_completed(pilot_home):
    return _read_json_dir(Path(pilot_home) / "completed")


def _read_json_dir(directory):
    records = {}
    try:
        paths = sorted(Path(directory).glob("*.json"))
    except OSError:
        return records
    for path in paths:
        payload = _read_json(path)
        if isinstance(payload, dict):
            records[path.stem] = payload
    return records


def read_spans(pilot_home):
    spans = []
    for line in _read_text(Path(pilot_home) / "spans.jsonl").splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("at"):
            spans.append(payload)
    return spans


def read_statuses(state_dir):
    """Map every ``<task>.status`` file to its parsed events (may be empty)."""
    statuses = {}
    try:
        paths = sorted(Path(state_dir).glob("*.status"))
    except OSError:
        return statuses
    for path in paths:
        events = []
        for line in _read_text(path).splitlines():
            parsed = parse_status_line(line)
            if parsed:
                events.append(parsed)
        statuses[path.name[: -len(".status")]] = events
    return statuses


def read_meta(path):
    meta = {}
    for line in _read_text(path).splitlines():
        key, sep, value = line.partition("=")
        if sep:
            meta[key.strip()] = value.strip()
    return meta


def find_review_pane(state_dir, issue):
    """Pane id from the newest ``<task>-review-*.meta`` for this issue."""
    try:
        paths = sorted(Path(state_dir).glob("*.meta"))
    except OSError:
        return None
    candidates = []
    for path in paths:
        task = path.name[: -len(".meta")]
        if task_issue(task) != issue or "review" not in task:
            continue
        meta = read_meta(path)
        pane = meta.get("review_pane") or meta.get("herdr_pane_id")
        if not pane:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, path.name, pane))
    return max(candidates)[2] if candidates else None


# --------------------------------------------------------------------------
# pure derivation


def _events_for(statuses, issue):
    events = []
    for task, task_events in (statuses or {}).items():
        if task_issue(task) != issue:
            continue
        for event in task_events or []:
            if isinstance(event, dict) and event.get("at"):
                events.append((task, event))
    return events


def _near_merge(at, merged_at, grace):
    """True when ``at`` is the merge itself, not a separate lifecycle event.

    GitHub stamps the issue and the PR independently, so a merge can be a few
    seconds before or after a related action/closure.
    """
    if not at or not merged_at:
        return False
    try:
        delta = abs((_at_key(at) - _at_key(merged_at)).total_seconds())
    except (AttributeError, TypeError, ValueError):
        return False
    return delta <= grace


def _delivery_pending(merged_at, now, grace):
    """True while a merged record can still be on its way to delivery.

    The grace is measured from ``merged_at`` to the observation time, not to
    the closure: GitHub auto-closes the issue at merge, so closure proximity
    says nothing about whether a completion receipt can still arrive.
    """
    if not merged_at:
        return False
    try:
        elapsed = (_at_key(utc_iso(now)) - _at_key(merged_at)).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return False
    return elapsed <= grace


def derive_history(issue, claim=None, statuses=None, spans=None, pr=None,
                   merge=None, completed=None, actions=None,
                   merge_grace=MERGE_CLOSE_GRACE, now=None):
    """Canonical, sorted, first-occurrence stage history for one issue.

    A stage whose source carries no timestamp is omitted, so the record is
    counted but never timed downstream.
    """
    try:
        issue = int(issue)
    except (TypeError, ValueError):
        return []
    if now is None:
        now = time.time()
    claim = claim if isinstance(claim, dict) else {}
    pr = pr if isinstance(pr, dict) else {}
    merge = merge if isinstance(merge, dict) else {}
    completed = completed if isinstance(completed, dict) else {}
    # The completion receipt carries the claim stamp for delivered records,
    # whose transient pilot claim is long gone.
    receipt_claim = completed.get("claim")
    if (not claim.get("claimed_at") and isinstance(receipt_claim, dict)
            and receipt_claim.get("claimed_at")):
        claim = receipt_claim

    spans = [item for item in (spans or [])
             if isinstance(item, dict) and _same_issue(item.get("issue"), issue)]
    span_starts = [item for item in spans
                   if item.get("event") in (None, "start") and item.get("at")]
    review_spans = [item for item in span_starts
                    if item.get("phase") in REVIEW_PHASES]
    work_spans = [item for item in span_starts
                  if item.get("phase") in WORK_PHASES]

    task_events = _events_for(statuses, issue)
    working = [event["at"] for _task, event in task_events
               if event.get("event") == "working"]
    review_working = [event["at"] for task, event in task_events
                      if event.get("event") == "working"
                      and "review" in str(task)]

    review_start = _earliest([item["at"] for item in review_spans])
    dispatched = _earliest(working + [item["at"] for item in work_spans])
    reviewing = review_start or _earliest(review_working)

    reviews = pr.get("reviews") if isinstance(pr.get("reviews"), list) else []
    approval = _earliest([
        review.get("submittedAt") for review in reviews
        if isinstance(review, dict)
        and str(review.get("state") or "").upper() == "APPROVED"
    ])

    merged_at = merge.get("merged_at") or pr.get("mergedAt")
    review_ends = [item["at"] for item in spans
                   if item.get("event") == "end" and item.get("at")
                   and item.get("phase") in REVIEW_PHASES
                   and str(item.get("outcome") or "").lower()
                   not in FAILED_OUTCOMES]
    if merged_at:
        review_ends = [at for at in review_ends
                       if _at_key(at) <= _at_key(merged_at)]
    # Agent review leaves no GitHub approval, so the completed review span is
    # the authoritative stamp; an approval (when present) wins as the more
    # precise receipt and keeps the legacy mapping.
    reviewed = approval or (max(review_ends, key=_at_key)
                            if review_ends else None)

    action_times = [
        action.get("at") for action in (actions or [])
        if isinstance(action, dict)
        and _same_issue(action.get("issue"), issue)
        and str(action.get("action") or "") in ("release", "requeue")
    ]
    # A release/requeue frees the slot: the issue is waiting for a slot
    # (rework, non-terminal), not closed without delivery.  An action stamped
    # at the merge itself is the merge, not a slot wait.
    live_actions = [at for at in action_times
                    if not _near_merge(at, merged_at, merge_grace)]
    rework = _earliest(live_actions)

    delivered = completed.get("completed_at")
    incomplete = None
    if not delivered:
        closed = pr.get("closedAt")
        if closed and not _delivery_pending(merged_at, now, merge_grace):
            incomplete = closed

    stages = {}
    for stage, at in (
        ("claimed", claim.get("claimed_at")),
        ("dispatched", dispatched),
        ("receipted", review_start or pr.get("createdAt")),
        ("reviewing", reviewing),
        ("reviewed", reviewed),
        ("published", reviewed),
        ("merged", merged_at),
        ("delivered", delivered),
        ("incomplete", incomplete),
        ("rework", rework),
    ):
        if at and stage not in stages:
            stages[stage] = at
    return _sorted_history(stages)


def rework_count(issue, statuses=None, spans=None):
    """Failed review rounds observed in spans, else in review-task status."""
    try:
        issue = int(issue)
    except (TypeError, ValueError):
        return 0
    failures = 0
    for item in spans or []:
        if not isinstance(item, dict):
            continue
        if not _same_issue(item.get("issue"), issue):
            continue
        if (item.get("phase") in REVIEW_PHASES
                and item.get("event") == "end"
                and str(item.get("outcome") or "").lower() in FAILED_OUTCOMES):
            failures += 1
    if failures:
        return failures
    count = 0
    for task, task_events in (statuses or {}).items():
        if task_issue(task) != issue or "review" not in str(task):
            continue
        for event in task_events or []:
            if (isinstance(event, dict) and event.get("event") == "done"
                    and "pass" not in str(event.get("note") or "").lower()):
                count += 1
    return count


def staging_sha(body):
    """Deployed SHA from a health body: ``dev+229b7447cf7b`` -> ``229b7447``."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    build = payload.get("build")
    if not isinstance(build, str) or "+" not in build:
        return None
    suffix = build.split("+", 1)[1]
    return suffix[:8] or None


# --------------------------------------------------------------------------
# cache / ledger


def _cache_dir(config):
    configured = (config or {}).get("cache_dir") or DEFAULT_CONFIG["cache_dir"]
    return Path(os.path.expanduser(str(configured)))


def backlog_snapshot(config, clock=time.time, runner=None):
    """Open-issue count with a 300 s cache; degrades to cache/stale/unavailable."""
    config = config or {}
    now = clock()
    cache_path = _cache_dir(config) / "open-issues.json"
    cached = _read_json(cache_path)
    cached_open = cached.get("open") if isinstance(cached, dict) else None
    cached_at = cached.get("at") if isinstance(cached, dict) else None
    if (cached_open is not None and cached_at is not None
            and now - cached_at < CACHE_TTL):
        return {"open": cached_open, "source": "cache"}
    try:
        output = (runner or _gh)([
            "issue", "list", "--repo", _repo(config), "--state", "open",
            "--limit", "200", "--json", "number",
        ])
        payload = json.loads(output)
        if not isinstance(payload, list):
            raise ValueError("unexpected gh issue list payload")
        open_count = len(payload)
        _write_json(cache_path, {"at": now, "open": open_count})
        return {"open": open_count, "source": "live"}
    except Exception:
        if cached_open is not None:
            return {"open": cached_open, "source": "stale"}
        return {"open": None, "source": "unavailable"}


def read_history_ledger(cache_dir):
    entries = []
    for line in _read_text(Path(cache_dir) / "history.jsonl").splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if (isinstance(payload, dict) and payload.get("issue")
                and payload.get("stage") and payload.get("at")):
            entries.append(payload)
    return entries


def append_history(cache_dir, records):
    """Append unseen ``(issue, stage, at)`` tuples under an exclusive lock."""
    try:
        path = Path(cache_dir) / "history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                seen = set()
                for line in handle:
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(payload, dict) and payload.get("issue")
                            and payload.get("stage") and payload.get("at")):
                        seen.add((str(payload["issue"]), payload["stage"],
                                  payload["at"]))
                for record in records:
                    key = (str(record.get("issue")), record.get("stage"),
                           record.get("at"))
                    if not all(key) or key in seen:
                        continue
                    handle.write(json.dumps(
                        {"issue": key[0], "stage": key[1], "at": key[2]},
                        sort_keys=True) + "\n")
                    seen.add(key)
                handle.flush()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return


# --------------------------------------------------------------------------
# source health (empty is not the same as unreadable)


def _dir_health(path):
    try:
        path = Path(path)
        if not path.exists():
            return "missing"
        if not path.is_dir():
            return "unreadable"
        for _child in path.iterdir():
            break
        return "ok"
    except OSError:
        return "unreadable"


def _attempts_health(pilot_home, state_dir):
    health = [_dir_health(pilot_home), _dir_health(state_dir)]
    state_path = Path(pilot_home) / "state.json"
    try:
        if state_path.exists() and not state_path.is_dir():
            json.loads(state_path.read_text())
    except (OSError, ValueError):
        health.append("unreadable")
    if "unreadable" in health:
        return "unreadable"
    if "missing" in health:
        return "missing"
    return "ok"


def _backlog_health(backlog):
    return ("ok" if (backlog or {}).get("source") in
            ("live", "cache", "stale") else "unreadable")


def _staging_health(url, status):
    if not url:
        return "missing"
    return "ok" if str(status).startswith(("up", "DOWN")) else "unreadable"


# --------------------------------------------------------------------------
# config and impure defaults


class ConfigError(Exception):
    """A config file that was named or exists could not be used.

    ``label`` lets the view render it as ``progress: config: <reason>``
    instead of leaking the exception class name.
    """

    label = "config"


def _config_source(path):
    """(path, explicit) for the config: env wins, then the argument."""
    explicit = os.environ.get("FM_PROGRESS_CONFIG") or path
    if explicit:
        return Path(str(explicit)), True
    return ROOT / "config" / "progress.json", False


def load_config(path=None):
    """Load config (env ``FM_PROGRESS_CONFIG`` wins) over frozen defaults.

    An explicitly named config must be usable: missing, unreadable or
    malformed raises :class:`ConfigError` instead of silently falling back
    to defaults.  The default path is optional (a first run has no file) but,
    once it exists, it must parse too.
    """
    source, explicit = _config_source(path)
    try:
        text = source.read_text()
    except OSError as error:
        if explicit or source.exists():
            raise ConfigError("%s: %s" % (
                source, error.strerror or type(error).__name__))
        data = None
    else:
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ConfigError("%s: malformed JSON: %s" % (source, error))
        if not isinstance(data, dict):
            raise ConfigError("%s: not a JSON object" % source)
    config = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        config.update(data)
    for key in ("pilot_home", "state_dir", "cache_dir"):
        if config.get(key):
            config[key] = os.path.expanduser(str(config[key]))
    override = os.environ.get("FM_PROGRESS_CACHE")
    if override:
        config["cache_dir"] = os.path.expanduser(override)
    return config


def _repo(config):
    repos = (config or {}).get("repos")
    if isinstance(repos, str):
        repos = [repos]
    if isinstance(repos, list):
        for repo in repos:
            if repo:
                return str(repo)
    return DEFAULT_CONFIG["repos"][0]


def _gh(args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                          timeout=30, check=True)
    return proc.stdout


def _http(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "fm-progress/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, ""


def _pilot_config(config):
    payload = _read_json(Path((config or {}).get("pilot_home")
                              or DEFAULT_CONFIG["pilot_home"]) / "config.json")
    return payload if isinstance(payload, dict) else {}


def _cached_pr(pr_number, cache_dir):
    """Persisted PR metadata, at any age; no fetch (resolved records only)."""
    cached = _read_json(Path(cache_dir) / ("pr-%s.json" % pr_number))
    data = cached.get("data") if isinstance(cached, dict) else None
    return data if isinstance(data, dict) else None


def _fetch_pr(pr_number, config, clock, runner, cache_dir):
    cache_path = Path(cache_dir) / ("pr-%s.json" % pr_number)
    cached = _read_json(cache_path)
    now = clock()
    if (isinstance(cached, dict) and cached.get("at") is not None
            and now - cached["at"] < CACHE_TTL):
        data = cached.get("data")
        return data if isinstance(data, dict) else None
    try:
        output = (runner or _gh)([
            "pr", "view", str(pr_number), "--repo", _repo(config),
            "--json", "createdAt,mergedAt,reviews,closedAt",
        ])
        payload = json.loads(output)
        if not isinstance(payload, dict):
            raise ValueError("unexpected gh pr view payload")
        _write_json(cache_path, {"at": now, "data": payload})
        return payload
    except Exception:
        data = cached.get("data") if isinstance(cached, dict) else None
        return data if isinstance(data, dict) else None


def _fetch_issue_closure(issue, config, clock, runner, cache_dir):
    """``state``/``closedAt`` for one issue, cached 300 s; None on failure."""
    cache_path = Path(cache_dir) / ("issue-%s.json" % issue)
    cached = _read_json(cache_path)
    now = clock()
    if (isinstance(cached, dict) and cached.get("at") is not None
            and now - cached["at"] < CACHE_TTL):
        data = cached.get("data")
        return data if isinstance(data, dict) else None
    try:
        output = (runner or _gh)([
            "issue", "view", str(issue), "--repo", _repo(config),
            "--json", "state,closedAt",
        ])
        payload = json.loads(output)
        if not isinstance(payload, dict):
            raise ValueError("unexpected gh issue view payload")
        _write_json(cache_path, {"at": now, "data": payload})
        return payload
    except Exception:
        return None


# --------------------------------------------------------------------------
# snapshot


def _issue_numbers(claims, statuses, merges, completed):
    issues = set()
    for key in claims:
        try:
            issues.add(int(key))
        except (TypeError, ValueError):
            continue
    for task in statuses:
        issue = task_issue(task)
        if issue is not None:
            issues.add(issue)
    for records in (merges, completed):
        for key in records:
            try:
                issues.add(int(key))
            except (TypeError, ValueError):
                continue
    return sorted(issues)


def _evidence_stage(claim, merge, completed):
    """Stage a record can support without any timestamp; never invented."""
    if isinstance(completed, dict) and completed:
        return "delivered"
    if isinstance(merge, dict) and merge:
        return "merged"
    if isinstance(claim, dict):
        return "claimed"
    return None


def _attempt(issue, config, state_dir, state_json, statuses, spans, pr,
             merge, completed, actions, merge_grace, now):
    repo = _repo(config)
    events = _events_for(statuses, issue)
    claim = (state_json.get("claims") or {}).get(str(issue))
    history = derive_history(
        issue, claim=claim,
        statuses=statuses, spans=spans, pr=pr, merge=merge,
        completed=completed, actions=actions, merge_grace=merge_grace,
        now=now)

    waiting = None
    blocked = [event for _task, event in events
               if event.get("event") in ("blocked", "paused")]
    if blocked:
        waiting = max(blocked, key=lambda item: _at_key(item.get("at"))
                      ).get("note") or None
    if not waiting:
        phase = (state_json.get("phases") or {}).get(str(issue))
        if (isinstance(phase, dict) and phase.get("reason_code")
                and (phase.get("phase") == "waiting"
                     or phase.get("state") == "waiting")):
            waiting = phase.get("reason_code")

    merged_sha = (completed.get("merged_sha") or merge.get("merged_sha") or "")
    deployed_sha = merged_sha[:8] if completed and merged_sha else None

    # Poll time is never an event time: history-less records report no
    # activity stamp and the view orders them last.
    last_active = history[-1]["at"] if history else None

    return {
        "issue": "%s#%s" % (repo, issue),
        "stage": (history[-1]["stage"] if history
                  else _evidence_stage(claim, merge, completed)),
        "pr": merge.get("pr") or completed.get("pr"),
        "history": history,
        "deployed_sha": deployed_sha,
        "waiting": waiting,
        "review_pane": find_review_pane(state_dir, issue),
        "rework": rework_count(issue, statuses=statuses, spans=spans),
        "last_active": last_active,
    }


def _finish_attempt(attempt):
    if not attempt["history"]:
        return
    attempt["history"].sort(key=lambda item: (
        _at_key(item["at"]), _stage_index(item["stage"])))
    attempt["stage"] = attempt["history"][-1]["stage"]
    attempt["last_active"] = attempt["history"][-1]["at"]


def _merge_ledger(attempts, ledger, merges=None, completed=None,
                  actions=None, merge_grace=MERGE_CLOSE_GRACE):
    """Replay persisted tuples, dropping ones current evidence contradicts.

    Older ledger rows may carry an ``incomplete`` emitted at PR ``closedAt``
    seconds before the merge; the ledger is append-only, so filter at read.
    """
    by_issue = {attempt["issue"]: attempt for attempt in attempts}
    released = {
        (str(action.get("issue")), action.get("at"))
        for action in (actions or [])
        if isinstance(action, dict)
        and str(action.get("action") or "") in ("release", "requeue")
    }
    for entry in ledger:
        attempt = by_issue.get(str(entry.get("issue")))
        if attempt is None:
            continue
        if any(item["stage"] == entry["stage"] and item["at"] == entry["at"]
               for item in attempt["history"]):
            continue
        if entry.get("stage") == "incomplete":
            number = str(entry.get("issue")).rsplit("#", 1)[-1]
            if (number, entry.get("at")) in released:
                continue
            merged_at = ((merges or {}).get(number) or {}).get("merged_at")
            if (completed or {}).get(number):
                continue
            # A merge cannot be preceded by incompleteness; a closure at
            # merge time is legitimate once the delivery grace has elapsed.
            if merged_at and _at_key(entry.get("at")) < _at_key(merged_at):
                continue
        attempt["history"].append({"stage": entry["stage"],
                                   "at": entry["at"]})
    for attempt in attempts:
        _finish_attempt(attempt)


def snapshot(config=None, clock=time.time, runner=None, http=None):
    """Build the frozen snapshot contract; never raises on missing sources.

    A :class:`ConfigError` from :func:`load_config` propagates: a broken
    config is a caller error, not degraded factory data.
    """
    config = config or load_config()
    cache_dir = _cache_dir(config)

    try:
        now = clock()
    except Exception:
        now = time.time()
    generated = utc_iso(now)

    pilot_home = config.get("pilot_home") or DEFAULT_CONFIG["pilot_home"]
    state_dir = config.get("state_dir") or DEFAULT_CONFIG["state_dir"]

    pilot_state = _read_json(Path(pilot_home) / "state.json")
    state_json = pilot_state if isinstance(pilot_state, dict) else {}
    claims = _claims_from(state_json)
    actions = state_json.get("actions")
    actions = actions if isinstance(actions, list) else []
    merges = read_merges(pilot_home)
    completed = read_completed(pilot_home)
    spans = read_spans(pilot_home)
    statuses = read_statuses(state_dir)

    issues = _issue_numbers(claims, statuses, merges, completed)

    try:
        merge_grace = float(config.get("merge_close_grace_seconds",
                                       MERGE_CLOSE_GRACE))
    except (TypeError, ValueError):
        merge_grace = float(MERGE_CLOSE_GRACE)

    pr_cache = {}
    attempts = []
    for issue in issues:
        merge = merges.get(str(issue)) or {}
        completion = completed.get(str(issue)) or {}
        pr_number = merge.get("pr") or completion.get("pr")
        pr = None
        if pr_number:
            # A merged or delivered record is already resolved: its PR
            # metadata is final and the local artifacts carry every stage, so
            # a cold refresh never spends a gh call on it.  Persisted metadata
            # is reused at any age.
            resolved = bool(completion) or bool(merge.get("merged_at"))
            if resolved:
                pr = _cached_pr(pr_number, cache_dir)
            elif pr_number not in pr_cache:
                try:
                    pr_cache[pr_number] = _fetch_pr(
                        pr_number, config, clock, runner, cache_dir)
                except Exception:
                    pr_cache[pr_number] = None
            if pr is None and not resolved:
                pr = pr_cache.get(pr_number)
        try:
            attempt = _attempt(
                issue, config, state_dir, state_json, statuses, spans, pr,
                merge, completion, actions, merge_grace, now)
        except Exception:
            attempt = {
                "issue": "%s#%s" % (_repo(config), issue),
                "stage": _evidence_stage(None, merge, completion),
                "pr": pr_number, "history": [],
                "deployed_sha": None, "waiting": None, "review_pane": None,
                "rework": 0, "last_active": None,
            }
        attempts.append(attempt)

    ledger = read_history_ledger(cache_dir)
    _merge_ledger(attempts, ledger, merges=merges, completed=completed,
                  actions=actions, merge_grace=merge_grace)

    # Frozen rule: a closed issue with no delivery receipt is incomplete.
    # Bounded to non-terminal, receipt-less candidates.
    for issue, attempt in zip(issues, attempts):
        if attempt["stage"] in TERMINAL_STAGES:
            continue
        if completed.get(str(issue)):
            continue
        closure = _fetch_issue_closure(
            issue, config, clock, runner, cache_dir)
        if not isinstance(closure, dict):
            continue
        if str(closure.get("state") or "").upper() != "CLOSED":
            continue
        closed_at = closure.get("closedAt")
        if not closed_at:
            continue
        # A release stamped after the closure is not a live slot wait.
        pruned = [item for item in attempt["history"]
                  if not (item["stage"] == "rework"
                          and _at_key(item["at"]) > _at_key(closed_at))]
        if len(pruned) != len(attempt["history"]):
            attempt["history"] = pruned
            _finish_attempt(attempt)
        merged_at = (merges.get(str(issue)) or {}).get("merged_at")
        if _delivery_pending(merged_at, now, merge_grace):
            continue
        if not any(item["stage"] == "incomplete" and item["at"] == closed_at
                   for item in attempt["history"]):
            attempt["history"].append({"stage": "incomplete",
                                       "at": closed_at})
            _finish_attempt(attempt)

    append_history(cache_dir, [
        {"issue": attempt["issue"], "stage": entry["stage"],
         "at": entry["at"]}
        for attempt in attempts for entry in attempt["history"]
    ])

    try:
        pilot_cfg = _pilot_config(config)
    except Exception:
        pilot_cfg = {}
    paused = not bool(pilot_cfg.get("enabled", True))
    limit = pilot_cfg.get("max_active") or config.get("issue_limit") or 4
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 4
    active = sum(1 for attempt in attempts
                 if attempt["stage"] not in TERMINAL_STAGES)
    # Occupied worker slots are pilot claims, which differ from the
    # non-terminal attempt count (review/waiting lanes have no claim slot).
    slots_used = (max(0, len(claims)) if isinstance(pilot_state, dict)
                  else active)

    try:
        backlog = backlog_snapshot(config, clock=clock, runner=runner)
    except Exception:
        backlog = {"open": None, "source": "unavailable"}

    fallback_sha = None
    delivered = [attempt for attempt in attempts
                 if attempt["stage"] == "delivered"
                 and attempt.get("deployed_sha")]
    if delivered:
        fallback_sha = max(
            delivered, key=lambda item: _at_key(item.get("last_active"))
        )["deployed_sha"]
    staging_url = config.get("staging_url")
    staged_sha = None
    staged_status = "ERROR"
    if staging_url:
        try:
            status, body = (http or _http)(staging_url)
            try:
                code = int(status)
            except (TypeError, ValueError):
                code = None
            if code == 200:
                staged_status = "up (HTTP 200)"
            elif code is None:
                staged_status = "ERROR"
            else:
                staged_status = "DOWN (HTTP %d)" % code
            staged_sha = staging_sha(body)
        except Exception:
            staged_status = "ERROR"

    return {
        "generated": generated,
        "sources": {
            "attempts": _attempts_health(pilot_home, state_dir),
            "backlog": _backlog_health(backlog),
            "staging": _staging_health(staging_url, staged_status),
        },
        "attempts": attempts,
        "capacity": {
            "active_issues": active,
            "slots_used": slots_used,
            "ceiling": {"issue_limit": limit},
        },
        "paused": {"paused": paused},
        "backlog": backlog,
        "staging": {
            "url": staging_url,
            "deployed_sha": staged_sha or fallback_sha,
            "status": staged_status,
        },
    }


def main(argv=None):
    del argv
    try:
        payload = snapshot()
    except ConfigError as error:
        sys.stderr.write("progress: config: %s\n" % error)
        return 2
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
