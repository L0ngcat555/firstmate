#!/usr/bin/env bash
# fm-lane-reconcile.sh - reconcile parked or finished lanes against their
# recorded issue and pull request, and release the stale ones through the
# guarded teardown.
#
# Usage:
#   fm-lane-reconcile.sh scan [--apply]
#   fm-lane-reconcile.sh check <task-id> [--apply]
#   fm-lane-reconcile.sh bind-issue <task-id> <issue-url>
#
# A lane's declared wait or finished outcome says nothing about the work item it
# serves: an operator can close the issue, or merge or close the pull request,
# while the lane sits parked holding its endpoint and worktree, and the merge
# poll (bin/fm-pr-check.sh) is only one optional source of that news. This
# command reads the forge directly, so no reconciliation depends on that poll.
#
# Records. A lane is a direct ship or scout task with state/<id>.meta; a
# secondmate record is not a lane and is skipped. Its pull request is the meta's
# pr= (bin/fm-pr-check.sh writes it). Its issue is the meta's issue=, written
# only by `bind-issue`, which accepts a GitHub issue URL
# (https://github.com/<owner>/<repo>/issues/<n>), replaces any earlier binding,
# and places the line ahead of the pr= block that bin/fm-pr-lib.sh reads as the
# record's tail. A relaunch preserves both. The meta must be one regular,
# single-link file whose every line is key=value with at most one kind=, issue=,
# and pr=, each well formed; kind must be ship or scout. A status file that
# exists must be a regular, readable, non-symlink file. Anything else is
# reported `invalid` with the reason and nothing is read further, guessed, or
# changed.
#
# Lane state comes from bin/fm-crew-state.sh, the sole current-state source.
# Only `done` (finished), `paused` (a declared wait), and `parked` (a pipeline
# gate) are candidates. When that read answers `unknown` - typically because the
# lane's endpoint or worktree is already gone, which is exactly how an
# abandoned lane looks - the lane's recorded declaration decides instead: a
# status log whose current line (status_current_line) is `done` or `paused`
# makes it a candidate reported as `done (recorded)` or `paused (recorded)`.
# Any other state is `untouched` and the forge is never read for it. For a
# candidate the bound issue and pull request are read with a bounded forge
# call each (FM_LANE_RECONCILE_FORGE_SECS, default 10).
#
# Verdict per lane, one line each (`scan` omits untouched and current lanes, so
# every line it prints is a lane that was cleaned or needs attention):
#   <id>: <verdict> · lane: <state> · issue: <s> · pr: <s>[ · wait: <until>] · <detail>
# where <s> is open, closed, merged, unbound, or unreadable.
#   untouched     the lane is not parked or finished.
#   current       candidate; nothing it serves has closed and its declared wait,
#                 if timed, has not expired.
#   stale         the issue closed or the pull request merged or closed. With
#                 --apply the lane is released through bin/fm-teardown.sh, whose
#                 landed-work and completion gates stay authoritative and are
#                 never forced. A teardown refusal is reported and also
#                 recorded as an open `blocked [key=lane-reconcile]` decision
#                 on the lane's status log, so it surfaces as an actionable
#                 event until firstmate resolves that key.
#   held          stale, but the lane has an open decision or sits at a
#                 pipeline gate; reported, never cleaned, so cleanup cannot
#                 answer an open question.
#   wait-expired  a paused lane whose `until` time has passed while nothing it
#                 serves has closed; reported for a recheck, never cleaned,
#                 because an open work item is not finished work.
#   unreadable    the lane state or a bound forge record could not be read.
#   invalid       the records above are missing or corrupt.
#
# Every verdict other than untouched and current is appended to
# data/<id>/lane-reconcile.log (which teardown leaves in place) unless it
# repeats that log's last entry, so the divergence and the action taken stay on
# record after the lane is gone.
# Exit: 0 when nothing needs attention, 3 when any lane was reported stale
# without --apply, held, wait-expired, unreadable, invalid, or refused cleanup
# (exit 4 from teardown, a lost pool slot, is also attention), 2 on usage.
set -u
export LC_ALL=C

SCRIPT_DIR="$(d=${BASH_SOURCE[0]%/*}; [ "$d" != "${BASH_SOURCE[0]}" ] || d=.; cd "${d:-/}" && pwd)"
FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}}"
STATE="${FM_STATE_OVERRIDE:-$FM_HOME/state}"
DATA="${FM_DATA_OVERRIDE:-$FM_HOME/data}"
CREW_STATE_BIN="${FM_LANE_RECONCILE_CREW_STATE_BIN:-$SCRIPT_DIR/fm-crew-state.sh}"
TEARDOWN_BIN="$SCRIPT_DIR/fm-teardown.sh"
FORGE_SECS=${FM_LANE_RECONCILE_FORGE_SECS:-10}
CREW_STATE_SECS=60
DECISION_KEY=lane-reconcile
SEP=' · '

# shellcheck source=bin/fm-classify-lib.sh
. "$SCRIPT_DIR/fm-classify-lib.sh"
# shellcheck source=bin/fm-wake-lib.sh
. "$SCRIPT_DIR/fm-wake-lib.sh"
# shellcheck source=bin/fm-pr-lib.sh
. "$SCRIPT_DIR/fm-pr-lib.sh"
# shellcheck source=bin/fm-timeout-lib.sh
. "$SCRIPT_DIR/fm-timeout-lib.sh"

usage() {
  sed -n '2,/^set -u$/s/^# \{0,1\}//p' "$0" | sed '$d'
}

die_usage() {
  printf 'fm-lane-reconcile: %s\n' "$*" >&2
  exit 2
}

case "$FORGE_SECS" in
  ''|*[!0-9]*|0) die_usage "FM_LANE_RECONCILE_FORGE_SECS must be a positive whole number" ;;
esac

# Parses a GitHub issue URL into ISSUE_OWNER, ISSUE_REPO, ISSUE_NUMBER.
issue_url_parse() {  # <url>
  local pattern='^https://github\.com/([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]{0,37}[A-Za-z0-9])/([A-Za-z0-9._-]{1,100})/issues/([1-9][0-9]*)$'
  ISSUE_OWNER=
  ISSUE_REPO=
  ISSUE_NUMBER=
  [[ "${1-}" =~ $pattern ]] || return 1
  [[ "${BASH_REMATCH[1]}" != *--* ]] || return 1
  [ "${BASH_REMATCH[2]}" != . ] && [ "${BASH_REMATCH[2]}" != .. ] || return 1
  ISSUE_OWNER=${BASH_REMATCH[1]}
  ISSUE_REPO=${BASH_REMATCH[2]}
  ISSUE_NUMBER=${BASH_REMATCH[3]}
}

# Validates one lane's records. Sets LANE_KIND, LANE_ISSUE, LANE_PR, LANE_STATUS.
# Returns 0 for a valid lane, 1 with LANE_INVALID set, 2 for a non-lane record.
lane_records() {  # <id>
  local id=$1 meta="$STATE/$1.meta" line key value kinds=0 issues=0 prs=0
  LANE_KIND=
  LANE_ISSUE=
  LANE_PR=
  LANE_STATUS="$STATE/$id.status"
  LANE_INVALID=
  if [ ! -e "$meta" ] && [ ! -L "$meta" ]; then
    LANE_INVALID="task record state/$id.meta is missing"
    return 1
  fi
  if [ -L "$meta" ] || [ ! -f "$meta" ] || [ "$(fm_pr_file_link_count "$meta")" != 1 ]; then
    LANE_INVALID="task record is not one regular file"
    return 1
  fi
  if [ ! -r "$meta" ]; then
    LANE_INVALID="task record is unreadable"
    return 1
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    if ! [[ "$line" =~ ^[a-z_][a-z0-9_]*= ]]; then
      LANE_INVALID="task record has a malformed line"
      return 1
    fi
    key=${line%%=*}
    value=${line#*=}
    case "$key" in
      kind) kinds=$((kinds + 1)); LANE_KIND=$value ;;
      issue) issues=$((issues + 1)); LANE_ISSUE=$value ;;
      pr) prs=$((prs + 1)); LANE_PR=$value ;;
    esac
  done < "$meta"
  if [ "$kinds" -ne 1 ]; then
    LANE_INVALID="task record names $kinds kind= lines"
    return 1
  fi
  case "$LANE_KIND" in
    ship|scout) ;;
    secondmate) return 2 ;;
    *) LANE_INVALID="task record kind '$LANE_KIND' is not ship or scout"; return 1 ;;
  esac
  if [ "$issues" -gt 1 ] || { [ "$issues" -eq 1 ] && ! issue_url_parse "$LANE_ISSUE"; }; then
    LANE_INVALID="task record issue= binding is malformed"
    return 1
  fi
  if [ "$prs" -gt 1 ] || { [ "$prs" -eq 1 ] && ! fm_pr_url_parse "$LANE_PR"; }; then
    LANE_INVALID="task record pr= binding is malformed"
    return 1
  fi
  if [ -e "$LANE_STATUS" ] || [ -L "$LANE_STATUS" ]; then
    if [ -L "$LANE_STATUS" ] || [ ! -f "$LANE_STATUS" ] || [ ! -r "$LANE_STATUS" ]; then
      LANE_INVALID="status log is not a readable regular file"
      return 1
    fi
  fi
  return 0
}

# Prints the lane's current state word from bin/fm-crew-state.sh.
lane_state() {  # <id>
  local out word
  out=$(fm_run_timed "$CREW_STATE_SECS" "$CREW_STATE_BIN" "$1" 2>/dev/null) || return 1
  word=$(printf '%s\n' "$out" | sed -n '1s/^state: \([a-z-]*\)\( .*\)\{0,1\}$/\1/p')
  case "$word" in
    working|parked|done|blocked|paused|failed|unknown) printf '%s' "$word" ;;
    *) return 1 ;;
  esac
}

# Prints open or closed for the bound GitHub issue.
issue_state() {  # <url>
  local out state
  issue_url_parse "$1" || return 1
  if command -v gh >/dev/null 2>&1 \
    && out=$(fm_run_timed "$FORGE_SECS" gh api "repos/$ISSUE_OWNER/$ISSUE_REPO/issues/$ISSUE_NUMBER" --jq .state 2>/dev/null); then
    state=$out
  elif command -v gh-axi >/dev/null 2>&1 \
    && out=$(fm_run_timed "$FORGE_SECS" gh-axi issue view "$ISSUE_NUMBER" --repo "$ISSUE_OWNER/$ISSUE_REPO" 2>/dev/null); then
    state=$(printf '%s\n' "$out" | awk '$1 == "state:" { n++; v = $2 } END { if (n == 1) print v }')
  else
    return 1
  fi
  state=$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')
  case "$state" in
    open|closed) printf '%s' "$state" ;;
    *) return 1 ;;
  esac
}

# Prints open, merged, or closed for the bound pull request, through the same
# per-forge record readers bin/fm-crew-state.sh uses.
pr_state() {  # <url>
  local record state merged
  fm_pr_url_parse "$1" || return 1
  # shellcheck disable=SC2016  # The inner script expands after bash -c receives positional args.
  record=$(fm_run_timed "$FORGE_SECS" bash -c '
    . "$1"
    case "$2" in
      github) fm_pr_github_read_record "${4%%/*}" "${4#*/}" "$5" ;;
      gitlab) fm_pr_gitlab_read_record "$3" "$4" "$5" ;;
      forgejo) fm_pr_forgejo_read_record "$3" "$4" "$5" ;;
      gerrit) fm_pr_gerrit_read_record "$3" "$5" ;;
      *) false ;;
    esac || exit 1
    printf "%s %s\n" "$FM_PR_RECORD_STATE" "$FM_PR_RECORD_MERGED"
  ' _ "$SCRIPT_DIR/fm-pr-lib.sh" "$FM_PR_PROVIDER" "$FM_PR_HOST" "$FM_PR_PATH" "$FM_PR_NUMBER" 2>/dev/null) || return 1
  state=$(printf '%s' "${record%% *}" | tr '[:upper:]' '[:lower:]')
  merged=${record##* }
  if [ "$merged" = true ]; then
    printf merged
    return 0
  fi
  [ "$merged" = false ] || return 1
  case "$state" in
    open|opened|new) printf open ;;
    closed|abandoned) printf closed ;;
    *) return 1 ;;
  esac
}

record_log() {  # <id> <entry>
  local dir="$DATA/$1" log last
  log="$dir/lane-reconcile.log"
  mkdir -p "$dir" 2>/dev/null || return 0
  if [ -f "$log" ]; then
    last=$(tail -n 1 "$log" 2>/dev/null)
    [ "${last#* }" != "$2" ] || return 0
  fi
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" >> "$log" 2>/dev/null || true
}

# Records a cleanup refusal as an open keyed decision on the lane's status log.
record_refusal() {  # <id> <note>
  local line="blocked [key=$DECISION_KEY]: lane reconcile: $2"
  ! status_event_recorded "$LANE_STATUS" "$line" || return 0
  fm_wake_status_append_self_announced "$STATE" "$LANE_STATUS" "$line" >/dev/null 2>&1
  [ "$?" -ne 2 ] || printf 'fm-lane-reconcile: could not record the refusal on %s\n' "$LANE_STATUS" >&2
}

# Reconciles one lane. Prints its verdict line; returns 0 when nothing needs
# attention and 3 when it does.
reconcile_lane() {  # <id> <apply>
  local id=$1 apply=$2 rc lstate istate=unbound pstate=unbound wait_line until_epoch wait='' now
  local verdict detail divergence out first recorded=''
  lane_records "$id"
  rc=$?
  if [ "$rc" -eq 2 ]; then
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    printf '%s: invalid%s%s\n' "$id" "$SEP" "$LANE_INVALID"
    record_log "$id" "invalid $LANE_INVALID"
    return 3
  fi
  if ! lstate=$(lane_state "$id"); then
    printf '%s: unreadable%slane: unknown%scurrent state could not be read\n' "$id" "$SEP" "$SEP"
    record_log "$id" "unreadable lane state"
    return 3
  fi
  if [ "$lstate" = unknown ] && [ -f "$LANE_STATUS" ]; then
    status_line_verb "$(status_current_line "$LANE_STATUS" "$LANE_KIND")" recorded
    case "$recorded" in
      done|paused) lstate="$recorded (recorded)" ;;
    esac
  fi
  case "$lstate" in
    done*|paused*|parked) ;;
    *)
      [ "$QUIET" = 1 ] || printf '%s: untouched%slane: %s\n' "$id" "$SEP" "$lstate"
      return 0
      ;;
  esac
  if [ -n "$LANE_ISSUE" ]; then istate=$(issue_state "$LANE_ISSUE") || istate=unreadable; fi
  if [ -n "$LANE_PR" ]; then pstate=$(pr_state "$LANE_PR") || pstate=unreadable; fi
  divergence=
  [ "$istate" != closed ] || divergence="issue $LANE_ISSUE closed"
  case "$pstate" in
    merged|closed) divergence="${divergence:+$divergence, }pull request $LANE_PR $pstate" ;;
  esac
  if [ "${lstate%% *}" = paused ] && [ -f "$LANE_STATUS" ]; then
    wait_line=$(status_declared_wait_line "$LANE_STATUS")
    if until_epoch=$(status_paused_until "$wait_line"); then
      now=$(date +%s)
      wait="until $(date -u -d "@$until_epoch" +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -r "$until_epoch" +%Y-%m-%dT%H:%MZ)"
      [ "$until_epoch" -gt "$now" ] || wait="expired $wait"
    fi
  fi
  if [ -n "$divergence" ]; then
    if [ "$lstate" = parked ] || [ -n "$(status_own_open_decisions "$LANE_STATUS")" ]; then
      verdict=held
      detail="$divergence while the lane holds an open decision; not cleaned"
    elif [ "$apply" != 1 ]; then
      verdict=stale
      detail="$divergence while the lane is $lstate; cleanup needs --apply"
    else
      verdict=stale
      out=$("$TEARDOWN_BIN" "$id" 2>&1)
      rc=$?
      case "$rc" in
        0) detail="$divergence while the lane is $lstate; cleaned up" ;;
        4) detail="$divergence while the lane is $lstate; cleaned up, but its pool slot was lost" ;;
        *)
          first=$(printf '%s\n' "$out" | grep -iE -m 1 'refus|error' || true)
          [ -n "$first" ] || first=$(printf '%s\n' "$out" | awk 'NF { l = $0 } END { print l }')
          first=$(printf '%s' "$first" | tr -s '[:space:]' ' ' | cut -c1-300)
          detail="$divergence while the lane is $lstate; cleanup refused (exit $rc): ${first:-no output}"
          record_refusal "$id" "$detail"
          ;;
      esac
    fi
  elif [ "$istate" = unreadable ] || [ "$pstate" = unreadable ]; then
    verdict=unreadable
    detail="a bound forge record could not be read"
  else
    case "$wait" in
      expired*)
        verdict='wait-expired'
        detail="declared wait has expired while nothing it serves has closed; recheck the wait"
        ;;
      *)
        verdict=current
        detail="nothing it serves has closed"
        ;;
    esac
  fi
  if [ "$verdict" != current ] || [ "$QUIET" != 1 ]; then
    printf '%s: %s%slane: %s%sissue: %s%spr: %s%s%s%s\n' "$id" "$verdict" "$SEP" "$lstate" "$SEP" "$istate" "$SEP" "$pstate" \
      "${wait:+$SEP}" "${wait:+wait: $wait}" "$SEP$detail"
  fi
  [ "$verdict" != current ] || return 0
  record_log "$id" "$verdict lane=${lstate// /-} issue=${LANE_ISSUE:-none}:$istate pr=${LANE_PR:-none}:$pstate${wait:+ wait=${wait// /-}} $detail"
  [ "$verdict" = stale ] && [ "$apply" = 1 ] && [ "${rc:-1}" -eq 0 ] && return 0
  return 3
}

bind_issue() {  # <id> <url>
  local id=$1 url=$2 meta lock tmp mode rc
  fm_pr_task_id_valid "$id" || die_usage "invalid task id"
  issue_url_parse "$url" || die_usage "expected a GitHub issue URL https://github.com/<owner>/<repo>/issues/<n>"
  meta="$STATE/$id.meta"
  lock=$(fm_meta_lock_path "$meta") || die_usage "invalid task id"
  fm_lock_acquire_wait "$lock"
  # shellcheck disable=SC2064  # The lock path is fixed now.
  trap "fm_lock_release '$lock' >/dev/null 2>&1 || true" EXIT
  lane_records "$id"
  rc=$?
  if [ "$rc" -eq 2 ]; then
    printf 'fm-lane-reconcile: %s is a secondmate, not a lane\n' "$id" >&2
    exit 1
  elif [ "$rc" -ne 0 ] && [ "$LANE_INVALID" != "task record issue= binding is malformed" ]; then
    printf 'fm-lane-reconcile: %s: %s\n' "$id" "$LANE_INVALID" >&2
    exit 1
  fi
  mode=$(fm_pr_file_mode "$meta") || mode=600
  tmp=$(mktemp "$STATE/.fm-lane-meta.XXXXXX") || exit 1
  if ! awk -v binding="issue=$url" '
    /^issue=/ { next }
    !placed && /^pr(_head)?=/ { print binding; placed = 1 }
    { print }
    END { if (!placed) print binding }
  ' "$meta" > "$tmp" || ! chmod "$mode" "$tmp" || ! mv -f -- "$tmp" "$meta"; then
    rm -f -- "$tmp"
    printf 'fm-lane-reconcile: could not write %s\n' "$meta" >&2
    exit 1
  fi
  printf '%s: issue %s bound\n' "$id" "$url"
}

cmd=${1:-}
QUIET=0
case "$cmd" in
  -h|--help) usage; exit 0 ;;
  scan)
    shift
    apply=0
    case "${1:-}" in
      --apply) apply=1; shift ;;
    esac
    [ "$#" -eq 0 ] || die_usage "usage: fm-lane-reconcile.sh scan [--apply]"
    QUIET=1
    status=0
    for meta in "$STATE"/*.meta; do
      [ -e "$meta" ] || [ -L "$meta" ] || continue
      id=${meta##*/}
      id=${id%.meta}
      fm_pr_task_id_valid "$id" || continue
      reconcile_lane "$id" "$apply" || status=3
    done
    exit "$status"
    ;;
  check)
    shift
    [ "$#" -ge 1 ] || die_usage "usage: fm-lane-reconcile.sh check <task-id> [--apply]"
    id=$1
    shift
    apply=0
    case "${1:-}" in
      --apply) apply=1; shift ;;
    esac
    [ "$#" -eq 0 ] || die_usage "usage: fm-lane-reconcile.sh check <task-id> [--apply]"
    fm_pr_task_id_valid "$id" || die_usage "invalid task id"
    reconcile_lane "$id" "$apply"
    exit $?
    ;;
  bind-issue)
    shift
    [ "$#" -eq 2 ] || die_usage "usage: fm-lane-reconcile.sh bind-issue <task-id> <issue-url>"
    bind_issue "$1" "$2"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
