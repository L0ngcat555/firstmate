#!/usr/bin/env bash
# tests/fm-watch-inbox-surface.test.sh - a captain inbox note must ring the
# primary by itself.
#
# bin/fm-inbox.sh announces a note as ONE `check` wake row keyed `inbox:<id>`.
# Nothing used to surface that row: the watcher's only other queue reader filters
# to `procevent:*`, the arm owner parses the watcher's stdout, and the queue is
# consumed by bin/fm-wake-drain.sh, which only the model runs. A note therefore
# needed a turn it could not cause, and on an idle fleet the heartbeat backstop -
# the only remaining turn source - absorbed itself as benign because it never
# looks at state/inbox/*.note. On 2026-10-27 two captain notes waited 3 h 38 min
# and 2 h 59 min, and a fresh note sat unread until an operator typed into the
# pane.
#
# The contract these tests pin: a queued inbox row with a still-pending note wakes
# the watcher once and marks the note as surfaced; the marker keeps it quiet for
# FM_INBOX_RING_INTERVAL and lets it re-ring afterwards; a row whose note has been
# handled never wakes; and a non-inbox check row is never claimed by this path.
set -u

# shellcheck source=tests/wake-helpers.sh
. "$(dirname "${BASH_SOURCE[0]}")/wake-helpers.sh"

WATCH="$ROOT/bin/fm-watch.sh"
DRAIN="$ROOT/bin/fm-wake-drain.sh"

TMP_ROOT=$(fm_test_tmproot fm-watch-inbox-surface-tests)

# Same knobs as the other watcher suites: tight poll, no check or heartbeat
# cadence, so a case only exercises the path it targets.
# The production shape is a HANDLING SUCCESSOR: the watcher started to handle one
# wake and lives on through the poll loop, where the recovery path deliberately
# stays quiet (FM_WATCH_HANDLING_SUCCESSOR=1) so a lost handshake cannot become an
# unbounded re-announce loop. That is the process a captain note actually lands in,
# and the one that used to swallow it.
watch_bg() {  # <state> <fakebin> <out> [extra env assignments...]
  local state=$1 fakebin=$2 out=$3
  shift 3
  PATH="$fakebin:$PATH" FM_STATE_OVERRIDE="$state" FM_CREW_STATE_BIN="$fakebin/fm-crew-state.sh" \
    FM_POLL=1 FM_SIGNAL_GRACE=1 FM_CHECK_INTERVAL=999999 FM_HEARTBEAT=999999 \
    FM_SECONDMATE_LIVENESS_SECS=99999999 FM_WATCH_HANDLING_SUCCESSOR=1 \
    "$@" "$WATCH" > "$out" &
}

# The incident's marker shape: an episode already announced and acknowledged, so
# the successor owes no re-announcement of its own.
mark_settled() {  # <state>
  printf 'acked:handling:1.%s.fixture\n' "$(date +%s)" > "$1/.watcher-down"
}

reap() {
  local rc
  kill "$1" 2>/dev/null || true
  wait_for_exit "$1" 100
  rc=$?
  [ "$rc" -ne 124 ] || fail "watcher pid $1 did not exit within 10s of TERM"
}

# Portable mtime in epoch seconds (never the `stat -f || stat -c` fallback, which
# writes a partial filesystem dump on Linux; see fm-watch.sh).
file_mtime() {
  if [ "$(uname)" = Darwin ]; then stat -f %m "$1" 2>/dev/null; else stat -c %Y "$1" 2>/dev/null; fi
}

# Wait up to <limit> 0.1s ticks for one full poll cycle (the beacon advances
# twice), so "the watcher stayed quiet" is asserted after it actually looked, not
# merely after it started.
wait_poll_cycle() {  # <state> <pid> [limit-ticks]
  local state=$1 pid=$2 limit=${3:-300} beat first now i=0
  beat="$state/.last-watcher-beat"
  rm -f "$beat"
  first=""
  while [ "$i" -lt "$limit" ]; do
    kill -0 "$pid" 2>/dev/null || return 1
    first=$(file_mtime "$beat")
    [ -n "$first" ] && break
    sleep 0.1
    i=$((i + 1))
  done
  while [ "$i" -lt "$limit" ]; do
    kill -0 "$pid" 2>/dev/null || return 1
    now=$(file_mtime "$beat")
    if [ -n "$now" ] && [ "$now" != "$first" ]; then
      return 0
    fi
    sleep 0.1
    i=$((i + 1))
  done
  return 1
}

# A pending captain note written the way bin/fm-inbox.sh writes one, plus the
# check row its announcement appends.
write_note() {  # <state> <id> [mtime-epoch]
  local state=$1 id=$2 epoch=${3:-}
  mkdir -p "$state/inbox"
  {
    printf 'id=%s\n' "$id"
    printf 'at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'source=text\n'
    printf 'announce_marker=1\n'
    printf -- '--\n'
    printf 'test note %s\n' "$id"
  } > "$state/inbox/$id.note"
  append_wake "$state" check "inbox:$id" "check: captain inbox note $id - test note $id"
  [ -z "$epoch" ] || set_mtime "$epoch" "$state/inbox/$id.note"
}

set_mtime() {  # <epoch> <file>
  local epoch=$1 f=$2 stamp
  if stamp=$(date -r "$epoch" +%Y%m%d%H%M.%S 2>/dev/null); then
    touch -t "$stamp" "$f"
  else
    stamp=$(date -d "@$epoch" +%Y%m%d%H%M.%S)
    touch -t "$stamp" "$f"
  fi
}

# A queued captain note with no other event is the whole reproduction: the pane is
# idle, the watch queue holds nothing else, and before this surface existed the
# watcher polled forever in silence.
test_pending_note_rings_the_primary() {
  local dir state fakebin out pid id
  dir=$(make_case inbox-surface); state="$dir/state"; fakebin="$dir/fakebin"
  out="$dir/watch.out"; id="1793103361-sjvBbl"
  export FM_FAKE_CREW_STATE='state: unknown · source: none · idle worker'
  mark_settled "$state"
  write_note "$state" "$id"
  watch_bg "$state" "$fakebin" "$out"
  pid=$!
  wait_for_exit "$pid" 100 || fail "watcher did not ring for a pending captain note"
  grep -F "check: captain inbox note $id (unread " "$out" >/dev/null \
    || fail "watcher printed no inbox wake reason: $(cat "$out")"
  grep -F "check: captain inbox note $id (unread " "$out" | grep -q "s);$" \
    || fail "the wake reason did not name the note and its age: $(cat "$out")"
  [ -s "$state/.seen-inbox-$id" ] \
    || fail "the surfaced note was not marked, so it would re-ring every poll"
  # The note record is untouched: surfacing is not an acknowledgement.
  [ -f "$state/inbox/$id.note" ] || fail "surfacing removed the durable note record"
  pass "a queued captain note on an idle fleet wakes the primary and is marked surfaced"
}

test_surfaced_note_stays_quiet_until_the_interval() {
  local dir state fakebin out pid id now
  dir=$(make_case inbox-quiet); state="$dir/state"; fakebin="$dir/fakebin"
  out="$dir/watch.out"; id="1793103362-quiet1"
  export FM_FAKE_CREW_STATE='state: unknown · source: none · idle worker'
  mark_settled "$state"
  write_note "$state" "$id"
  now=$(date +%s)
  printf '%s\n' "$now" > "$state/.seen-inbox-$id"
  watch_bg "$state" "$fakebin" "$out"
  pid=$!
  if ! wait_poll_cycle "$state" "$pid"; then
    reap "$pid"; fail "watcher exited for an already-surfaced note inside its ring interval: $(cat "$out")"
  fi
  [ ! -s "$out" ] || fail "an already-surfaced note printed another wake: $(cat "$out")"
  reap "$pid"
  pass "a note surfaced inside FM_INBOX_RING_INTERVAL does not ring again"
}

test_note_rerings_after_the_interval() {
  local dir state fakebin out pid id
  dir=$(make_case inbox-rering); state="$dir/state"; fakebin="$dir/fakebin"
  out="$dir/watch.out"; id="1793103363-again1"
  export FM_FAKE_CREW_STATE='state: unknown · source: none · idle worker'
  mark_settled "$state"
  write_note "$state" "$id"
  # Marked long ago: the note is still undrained, so the ring is owed again.
  printf '%s\n' "$(( $(date +%s) - 3600 ))" > "$state/.seen-inbox-$id"
  FM_INBOX_RING_INTERVAL=60 watch_bg "$state" "$fakebin" "$out"
  pid=$!
  wait_for_exit "$pid" 100 || fail "an undrained note stopped re-ringing after its interval"
  grep -F "check: captain inbox note $id (unread " "$out" >/dev/null \
    || fail "re-ring printed no inbox wake reason: $(cat "$out")"
  pass "an undrained note re-rings once its FM_INBOX_RING_INTERVAL has passed"
}

test_handled_note_never_rings() {
  local dir state fakebin out pid id
  dir=$(make_case inbox-handled); state="$dir/state"; fakebin="$dir/fakebin"
  out="$dir/watch.out"; id="1793103364-gone1"
  export FM_FAKE_CREW_STATE='state: unknown · source: none · idle worker'
  mark_settled "$state"
  # The row outlives the acknowledgement: the note file is the truth.
  append_wake "$state" check "inbox:$id" "check: captain inbox note $id - test note $id"
  watch_bg "$state" "$fakebin" "$out"
  pid=$!
  if ! wait_poll_cycle "$state" "$pid"; then
    reap "$pid"; fail "watcher exited for a row whose note was already handled: $(cat "$out")"
  fi
  [ ! -s "$out" ] || fail "a handled note still rang: $(cat "$out")"
  reap "$pid"
  pass "a queued row whose note is already handled never wakes the primary"
}

test_other_check_rows_are_not_claimed() {
  local dir state fakebin out pid
  dir=$(make_case inbox-other-keys); state="$dir/state"; fakebin="$dir/fakebin"
  out="$dir/watch.out"
  export FM_FAKE_CREW_STATE='state: unknown · source: none · idle worker'
  mark_settled "$state"
  append_wake "$state" check "mail:digest" "check: mail digest ready"
  watch_bg "$state" "$fakebin" "$out"
  pid=$!
  if ! wait_poll_cycle "$state" "$pid"; then
    reap "$pid"; fail "watcher exited for a non-inbox check row: $(cat "$out")"
  fi
  [ ! -s "$out" ] || fail "a non-inbox check row was surfaced by the inbox path: $(cat "$out")"
  reap "$pid"
  pass "the inbox surface claims inbox: rows only"
}

test_two_pending_notes_surface_together() {
  local dir state fakebin out pid
  dir=$(make_case inbox-two); state="$dir/state"; fakebin="$dir/fakebin"
  out="$dir/watch.out"
  export FM_FAKE_CREW_STATE='state: unknown · source: none · idle worker'
  mark_settled "$state"
  write_note "$state" "1793103365-two0001" "$(( $(date +%s) - 600 ))"
  write_note "$state" "1793103366-two0002" "$(( $(date +%s) - 60 ))"
  watch_bg "$state" "$fakebin" "$out"
  pid=$!
  wait_for_exit "$pid" 100 || fail "watcher did not ring for two pending notes"
  grep -F "check: captain inbox notes unread:" "$out" >/dev/null \
    || fail "the plural wake reason was not used for two due notes: $(cat "$out")"
  grep -F "1793103365-two0001 (unread " "$out" >/dev/null \
    || fail "the older note was not named: $(cat "$out")"
  grep -F "1793103366-two0002 (unread " "$out" >/dev/null \
    || fail "the newer note was not named: $(cat "$out")"
  [ -s "$state/.seen-inbox-1793103365-two0001" ] && [ -s "$state/.seen-inbox-1793103366-two0002" ] \
    || fail "not every surfaced note was marked"
  pass "two pending notes surface in one wake that names both"
}

test_pending_note_rings_the_primary
test_surfaced_note_stays_quiet_until_the_interval
test_note_rerings_after_the_interval
test_handled_note_never_rings
test_other_check_rows_are_not_claimed
test_two_pending_notes_surface_together
