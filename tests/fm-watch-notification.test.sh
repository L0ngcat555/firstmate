#!/usr/bin/env bash
# Notification policy uses durable source state, not vendor rendering.
set -eu
# shellcheck source=tests/wake-helpers.sh
. "$(dirname "${BASH_SOURCE[0]}")/wake-helpers.sh"
# shellcheck source=/dev/null
. "$ROOT/bin/fm-watch-notification-lib.sh"
TMP_ROOT=$(fm_test_tmproot fm-watch-notification)
STATE="$TMP_ROOT/policy"
mkdir -p "$STATE"

fm_watch_notice_due check inbox event-a || fail 'first observation suppressed'
fm_watch_notice_due check inbox event-a || fail 'unrecorded observation suppressed'
fm_watch_notice_record check inbox event-a
if fm_watch_notice_due check inbox event-a; then fail 'unchanged recorded check repeated'; fi
fm_watch_notice_due check inbox event-b || fail 'changed content suppressed'
fm_watch_notice_due check another event-a || fail 'different source suppressed'
fm_watch_notice_reset check inbox
fm_watch_notice_due check inbox event-a || fail 'reappearing episode suppressed'
fm_watch_notice_record check inbox event-a
marker=$(fm_watch_notice_path check inbox)
old=$(($(date +%s) - 901))
fingerprint=$(printf event-a | fm_watch_notice_hash)
printf '%s %s\n' "$old" "$fingerprint" > "$marker"
fm_watch_notice_due check inbox event-a || fail 'bounded reminder suppressed forever'
printf 'corrupt\n' > "$marker"
fm_watch_notice_due check inbox event-a || fail 'corrupt record suppressed event'
pass 'check changes, episode reset, corruption and bounded reminders preserve delivery'

long=$(printf '%0600d' 0)
summary=$(fm_watch_check_summary /private/state/check.sh "$long")
[ "${#summary}" -lt 320 ] || fail 'compact payload is unbounded'
printf '%s' "$summary" | grep -q 'full output in wake queue' || fail 'truncated output lacks retrieval pointer'
grep -q $'\tsuppressed\tcheck\t' "$STATE/.watch-notification-metrics.tsv" || fail 'suppression metric missing'
grep -q $'\tqueued\tcheck\t' "$STATE/.watch-notification-metrics.tsv" || fail 'queue publication metric missing'
pass 'bounded summaries retain full-record pointer and both metrics'

# Run the actual watcher with two authenticated checks. It must queue both
# original outputs and return one compact batch, with no real agent/process control.
dir=$(make_case checks)
STATE="$dir/state"
for id in alpha beta; do
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "ready-%s"\n' "$id" > "$STATE/$id.check.sh"
  chmod 700 "$STATE/$id.check.sh"
  FM_STATE_OVERRIDE="$STATE" bash "$ROOT/bin/fm-check-register.sh" "$id" >/dev/null
done
PATH="$dir/fakebin:$PATH" FM_STATE_OVERRIDE="$STATE" FM_HOME="$dir" \
  FM_POLL=0.1 FM_CHECK_INTERVAL=0 FM_HEARTBEAT=999999 \
  FM_SECONDMATE_LIVENESS_SECS=99999999 \
  timeout 30 bash "$ROOT/bin/fm-watch.sh" > "$dir/out"
grep -q 'check: batch (2)' "$dir/out" || fail "checks did not batch: $(cat "$dir/out")"
grep -q 'ready-alpha' "$STATE/.wake-queue" || fail 'first full event absent'
grep -q 'ready-beta' "$STATE/.wake-queue" || fail 'second full event absent'
[ "$(wc -l < "$STATE/.wake-queue")" -eq 2 ] || fail 'batch lost/duplicated events'
pass 'real watcher queues two checks durably and returns one notification'

FM_HOME="$dir" FM_STATE_OVERRIDE="$STATE" bash "$ROOT/bin/fm-wake-drain.sh" > "$dir/drain" 2> "$dir/drain.err"
FM_HOME="$dir" ack_drain_err "$STATE" "$dir/drain.err" >/dev/null 2>&1 || fail 'fixture acknowledgment failed'
printf 'window=test:fm-task\nkind=ship\nharness=omp\nspawn_gen=first\n' > "$STATE/task.meta"
printf 'done [at=%s]: completed issue work\nneeds-decision [at=%s]: user decision required\n' "$(date +%s)" "$(date +%s)" > "$STATE/task.status"
PATH="$dir/fakebin:$PATH" FM_STATE_OVERRIDE="$STATE" FM_HOME="$dir" \
  FM_POLL=0.1 FM_SIGNAL_GRACE=1 FM_CHECK_INTERVAL=0 FM_HEARTBEAT=999999 \
  FM_SECONDMATE_LIVENESS_SECS=99999999 \
  timeout 30 bash "$ROOT/bin/fm-watch.sh" > "$dir/signal.out"
grep -q '^signal:' "$dir/signal.out" || fail "unchanged checks starved actionable signal: $(cat "$dir/signal.out")"
[ "$(awk -F '\t' '$3=="check" {n++} END {print n+0}' "$STATE/.wake-queue")" -eq 0 ] || fail 'unchanged checks queued again after acknowledgment'
FM_HOME="$dir" FM_STATE_OVERRIDE="$STATE" bash "$ROOT/bin/fm-wake-drain.sh" > "$dir/decision" 2> "$dir/decision.err"
grep -q 'user decision required' "$dir/decision" || fail 'user decision lost from drain'
grep -q 'completed issue work' "$dir/decision" || fail 'completion lost from drain'
pass 'unchanged checks stay silent across watcher restart while completion and decision events survive'

# A full batch leaves checks due. On the next arm an unchanged first check
# cannot monopolize the slot and prevent a later source from being delivered.
dir=$(make_case bounded)
STATE="$dir/state"
for id in alpha beta; do
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "ready-%s"\n' "$id" > "$STATE/$id.check.sh"
  chmod 700 "$STATE/$id.check.sh"
  FM_STATE_OVERRIDE="$STATE" bash "$ROOT/bin/fm-check-register.sh" "$id" >/dev/null
done
for id in alpha beta; do
  PATH="$dir/fakebin:$PATH" FM_STATE_OVERRIDE="$STATE" FM_HOME="$dir" \
    FM_POLL=0.1 FM_CHECK_INTERVAL=0 FM_HEARTBEAT=999999 \
    FM_WATCH_CHECK_BATCH_MAX=1 FM_SECONDMATE_LIVENESS_SECS=99999999 \
    timeout 30 bash "$ROOT/bin/fm-watch.sh" > "$dir/$id.out"
  grep -Fq "check: $STATE/$id.check.sh: ready-$id" "$dir/$id.out" || fail "bounded batch starved $id"
  FM_HOME="$dir" FM_STATE_OVERRIDE="$STATE" bash "$ROOT/bin/fm-wake-drain.sh" > "$dir/drain" 2> "$dir/drain.err"
  FM_HOME="$dir" ack_drain_err "$STATE" "$dir/drain.err" >/dev/null 2>&1 || fail 'bounded fixture acknowledgment failed'
done
pass 'bounded batches preserve singleton format and advance past unchanged earlier checks'
