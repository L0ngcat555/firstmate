#!/usr/bin/env bash
# Behavioral coverage for bin/fm-lane-reconcile.sh: a parked or finished lane is
# reconciled against its bound issue and pull request, and a stale one is
# released only through the real guarded bin/fm-teardown.sh.
#
# Matrix:
#   (a) paused lane, bound issue closed, work pushed      -> stale, cleaned, recorded
#   (b) done lane, PR merged, report-only then --apply    -> stale reported (3), then cleaned
#   (c) working lane, open issue                          -> untouched, no forge read
#   (d) done lane, open issue and open PR                 -> current, untouched
#   (e) done lane, PR closed, unpushed work               -> refused, retained, keyed blocker once
#   (f) paused lane, wait not expired / expired           -> current / wait-expired, never cleaned
#   (g) missing, duplicate-pr, malformed, symlinked meta  -> invalid, nothing read or changed
#   (h) closed issue but an open decision                 -> held, not cleaned
#   (i) unreadable forge record                           -> unreadable, not cleaned
#   (j) bind-issue placement, rebinding, and refusal
#   (k) unknown current state, recorded done vs working  -> stale and cleaned / untouched
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
fm_git_identity fmtest fmtest@example.invalid

RECON="$ROOT/bin/fm-lane-reconcile.sh"
TMP_ROOT=$(fm_test_tmproot fm-lane-reconcile)

# One isolated home per case: state/data/config, a bare origin, a project
# clone, a task worktree on its own branch, and hermetic fakes for every
# external tool teardown or the reconcile reads.
make_case() {  # <name> -> echoes the case dir
  local case_dir="$TMP_ROOT/$1" fake
  fake="$case_dir/fakebin"
  mkdir -p "$case_dir"/{state,data,config,forge,crew} "$fake"
  touch "$case_dir/state/.last-watcher-beat"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$fake/treehouse"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$fake/tmux"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$fake/no-mistakes"
  cat > "$fake/fm-crew-state.sh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "${FM_FAKE_DIR:?}/crew.log"
cat "$FM_FAKE_DIR/crew/$1" 2>/dev/null || printf 'state: unknown · source: none\n'
SH
  # gh answers only the issue and pull-request state reads the reconcile makes;
  # every other call fails, so teardown's own PR lookups find nothing.
  cat > "$fake/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FM_FAKE_DIR:?}/forge.log"
case "$1 ${2:-}" in
  "api repos/"*/issues/*)
    f="$FM_FAKE_DIR/forge/issue-${2##*/}"
    [ -f "$f" ] || exit 1
    cat "$f"
    ;;
  "api graphql")
    for a in "$@"; do case "$a" in number=*) n=${a#number=} ;; esac; done
    f="$FM_FAKE_DIR/forge/pr-$n"
    [ -f "$f" ] || exit 1
    cat "$f"
    ;;
  *) exit 1 ;;
esac
SH
  cat > "$fake/gh-axi" <<'SH'
#!/usr/bin/env bash
case "${1:-} ${2:-}" in
  "pr list") printf '%s\n' "count: 0 (showing first 0)" "pull_requests[]: []" ; exit 0 ;;
esac
exit 1
SH
  chmod +x "$fake"/*
  git init -q --bare "$case_dir/origin.git"
  git -C "$case_dir/origin.git" symbolic-ref HEAD refs/heads/main
  git clone -q "$case_dir/origin.git" "$case_dir/seed" 2>/dev/null
  git -C "$case_dir/seed" commit -q --allow-empty -m baseline
  git -C "$case_dir/seed" push -q origin main
  rm -rf "$case_dir/seed"
  git clone -q "$case_dir/origin.git" "$case_dir/project"
  git -C "$case_dir/project" remote set-head origin main 2>/dev/null || true
  git -C "$case_dir/project" worktree add -q -b fm/lane-x1 "$case_dir/wt" main
  printf '%s\n' "$case_dir"
}

write_lane() {  # <case> <kind> [extra key=value...]
  local case_dir=$1 kind=$2
  shift 2
  fm_write_meta "$case_dir/state/lane-x1.meta" \
    "window=firstmate:fm-lane-x1" \
    "endpoint_task_id=lane-x1" \
    "worktree=$case_dir/wt" \
    "project=$case_dir/project" \
    "kind=$kind" \
    "mode=no-mistakes" \
    "spawn_gen=lane-reconcile-test" \
    "$@"
}

crew() {  # <case> <state>
  printf 'state: %s · source: fake\n' "$2" > "$1/crew/lane-x1"
}

status_line() {  # <case> <line>
  printf '%s\n' "$2" >> "$1/state/lane-x1.status"
}

commit_work() {  # <case> [push]
  printf 'work\n' > "$1/wt/work.txt"
  git -C "$1/wt" add work.txt
  git -C "$1/wt" commit -q -m work
  [ "${2:-}" != push ] || { git -C "$1/wt" push -q origin fm/lane-x1 2>/dev/null; git -C "$1/project" fetch -q origin; }
}

run_recon() {  # <case> <args...>; sets OUT and RC
  local case_dir=$1
  shift
  OUT=$(FM_HOME="$case_dir" FM_ROOT_OVERRIDE="$ROOT" \
    FM_STATE_OVERRIDE="$case_dir/state" FM_DATA_OVERRIDE="$case_dir/data" \
    FM_CONFIG_OVERRIDE="$case_dir/config" FM_FAKE_DIR="$case_dir" \
    FM_LANE_RECONCILE_CREW_STATE_BIN="$case_dir/fakebin/fm-crew-state.sh" \
    PATH="$case_dir/fakebin:$PATH" "$RECON" "$@" 2>&1)
  RC=$?
}

ISSUE=https://github.com/acme/widgets/issues/623
PR=https://github.com/acme/widgets/pull/624

# (a) A lane parked on a declared wait whose issue the operator closed.
C=$(make_case a)
write_lane "$C" ship "issue=$ISSUE"
crew "$C" paused
status_line "$C" "paused [at=1790000000]: waiting on operator review"
commit_work "$C" push
printf 'closed\n' > "$C/forge/issue-623"
run_recon "$C" scan --apply
expect_code 0 "$RC" "(a) cleaned stale lane exits 0"
assert_contains "$OUT" "lane-x1: stale · lane: paused · issue: closed · pr: unbound" "(a) verdict reads the closed issue back"
assert_contains "$OUT" "cleaned up" "(a) stale lane is cleaned"
assert_absent "$C/state/lane-x1.meta" "(a) guarded teardown retired the task record"
assert_grep "stale lane=paused issue=$ISSUE:closed" "$C/data/lane-x1/lane-reconcile.log" "(a) divergence is recorded where teardown leaves it"
pass "(a) closed issue -> stale, reconciled, cleaned through teardown"

# (b) A finished lane whose PR merged: report-only first, then --apply.
C=$(make_case b)
write_lane "$C" ship "pr=$PR"
crew "$C" "done"
commit_work "$C" push
printf 'state=MERGED\nmerged=true\n' > "$C/forge/pr-624"
run_recon "$C" check lane-x1
expect_code 3 "$RC" "(b) report-only stale lane needs attention"
assert_contains "$OUT" "lane-x1: stale · lane: done · issue: unbound · pr: merged" "(b) merged PR makes the lane stale"
assert_contains "$OUT" "cleanup needs --apply" "(b) report-only names the missing --apply"
assert_present "$C/state/lane-x1.meta" "(b) report-only cleans nothing"
run_recon "$C" check lane-x1 --apply
expect_code 0 "$RC" "(b) --apply cleans the merged lane"
assert_absent "$C/state/lane-x1.meta" "(b) task record retired after merge"
pass "(b) merged PR -> stale; cleanup only under --apply"

# (c) A live lane with an open issue is never read from the forge.
C=$(make_case c)
write_lane "$C" ship "issue=$ISSUE"
crew "$C" working
printf 'closed\n' > "$C/forge/issue-623"
run_recon "$C" scan --apply
expect_code 0 "$RC" "(c) live lane needs nothing"
assert_equals "" "$OUT" "(c) scan prints nothing for a live lane"
run_recon "$C" check lane-x1 --apply
expect_code 0 "$RC" "(c) check of a live lane needs nothing"
assert_contains "$OUT" "lane-x1: untouched · lane: working" "(c) live lane untouched"
assert_present "$C/state/lane-x1.meta" "(c) live lane kept"
assert_absent "$C/forge.log" "(c) no forge read for a live lane"
pass "(c) live lane -> untouched without a forge read"

# (d) A finished lane whose issue and PR are still open is current.
C=$(make_case d)
write_lane "$C" ship "issue=$ISSUE" "pr=$PR"
crew "$C" "done"
printf 'open\n' > "$C/forge/issue-623"
printf 'state=OPEN\nmerged=false\n' > "$C/forge/pr-624"
run_recon "$C" scan --apply
expect_code 0 "$RC" "(d) current lane needs nothing"
assert_equals "" "$OUT" "(d) scan prints nothing for a current lane"
run_recon "$C" check lane-x1 --apply
assert_contains "$OUT" "lane-x1: current · lane: done · issue: open · pr: open" "(d) open work keeps the lane current"
assert_present "$C/state/lane-x1.meta" "(d) current lane kept"
assert_absent "$C/data/lane-x1/lane-reconcile.log" "(d) current lane records nothing"
pass "(d) open issue and PR -> current"

# (e) Unlanded work behind a closed PR: teardown refuses and the refusal surfaces.
C=$(make_case e)
write_lane "$C" ship "pr=$PR"
crew "$C" "done"
status_line "$C" "done [at=1790000000]: PR $PR checks green"
commit_work "$C"
printf 'state=CLOSED\nmerged=false\n' > "$C/forge/pr-624"
run_recon "$C" scan --apply
expect_code 3 "$RC" "(e) refused cleanup needs attention"
assert_contains "$OUT" "lane-x1: stale · lane: done · issue: unbound · pr: closed" "(e) closed PR makes the lane stale"
assert_contains "$OUT" "cleanup refused" "(e) refusal reported"
assert_present "$C/state/lane-x1.meta" "(e) task record kept"
assert_present "$C/wt/work.txt" "(e) unlanded work kept"
assert_equals "$(git -C "$C/wt" log -1 --format=%s)" work "(e) unlanded commit kept"
assert_grep "blocked [key=lane-reconcile]" "$C/state/lane-x1.status" "(e) refusal recorded as an open keyed decision"
run_recon "$C" scan --apply
expect_code 3 "$RC" "(e) repeat still needs attention"
assert_equals 1 "$(grep -c 'key=lane-reconcile' "$C/state/lane-x1.status")" "(e) a repeat run does not duplicate the blocker"
pass "(e) unlanded work -> reported, not cleaned"

# (f) A declared wait: not expired stays current, expired is reported, never cleaned.
C=$(make_case f)
write_lane "$C" ship "issue=$ISSUE"
crew "$C" paused
printf 'open\n' > "$C/forge/issue-623"
status_line "$C" "paused [at=1790000000]: waiting on staging until 2999-01-01T00:00Z"
run_recon "$C" check lane-x1 --apply
expect_code 0 "$RC" "(f) unexpired wait needs nothing"
assert_contains "$OUT" "lane-x1: current · lane: paused · issue: open · pr: unbound · wait: until 2999-01-01T00:00Z" "(f) unexpired wait current"
status_line "$C" "paused [at=1790000001]: waiting on staging until 2001-01-01T00:00Z"
run_recon "$C" scan --apply
expect_code 3 "$RC" "(f) expired wait needs attention"
assert_contains "$OUT" "lane-x1: wait-expired · lane: paused · issue: open · pr: unbound · wait: expired until 2001-01-01T00:00Z" "(f) expired wait reported"
assert_present "$C/state/lane-x1.meta" "(f) expired wait over open work is never cleaned"
pass "(f) unexpired wait untouched; expired wait reported only"

# (g) Missing or corrupt records are reported, never guessed or changed.
C=$(make_case g)
run_recon "$C" check lane-x1 --apply
expect_code 3 "$RC" "(g) missing record needs attention"
assert_contains "$OUT" "lane-x1: invalid · task record state/lane-x1.meta is missing" "(g) missing record reported"
write_lane "$C" ship "pr=$PR" "pr=$PR"
crew "$C" "done"
cp "$C/state/lane-x1.meta" "$C/before.meta"
run_recon "$C" check lane-x1 --apply
expect_code 3 "$RC" "(g) duplicate pr= needs attention"
assert_contains "$OUT" "lane-x1: invalid · task record pr= binding is malformed" "(g) duplicate pr= reported"
cmp -s "$C/before.meta" "$C/state/lane-x1.meta" || fail "(g) corrupt record left unchanged"
write_lane "$C" ship
printf 'garbage without a key\n' >> "$C/state/lane-x1.meta"
run_recon "$C" check lane-x1 --apply
assert_contains "$OUT" "lane-x1: invalid · task record has a malformed line" "(g) malformed line reported"
write_lane "$C" ship "issue=https://github.com/acme/widgets/pull/1"
run_recon "$C" check lane-x1 --apply
assert_contains "$OUT" "lane-x1: invalid · task record issue= binding is malformed" "(g) non-issue URL reported"
mv "$C/state/lane-x1.meta" "$C/real.meta"
ln -s "$C/real.meta" "$C/state/lane-x1.meta"
run_recon "$C" check lane-x1 --apply
assert_contains "$OUT" "lane-x1: invalid · task record is not one regular file" "(g) symlinked record reported"
assert_absent "$C/crew.log" "(g) no state read for an invalid record"
assert_absent "$C/forge.log" "(g) no forge read for an invalid record"
pass "(g) missing/corrupt records -> reported, not guessed"

# (h) A closed issue under an open decision is held, never cleaned.
C=$(make_case h)
write_lane "$C" ship "issue=$ISSUE"
crew "$C" paused
commit_work "$C" push
status_line "$C" "needs-decision [key=scope] [at=1790000000]: widen the fix?"
status_line "$C" "paused [at=1790000001]: waiting on the scope answer"
printf 'closed\n' > "$C/forge/issue-623"
run_recon "$C" scan --apply
expect_code 3 "$RC" "(h) held lane needs attention"
assert_contains "$OUT" "lane-x1: held · lane: paused · issue: closed" "(h) open decision holds the lane"
assert_present "$C/state/lane-x1.meta" "(h) held lane kept"
pass "(h) open decision -> held, not cleaned"

# (i) An unreadable forge record is reported, never read as closed.
C=$(make_case i)
write_lane "$C" scout "issue=$ISSUE"
crew "$C" "done"
run_recon "$C" scan --apply
expect_code 3 "$RC" "(i) unreadable forge needs attention"
assert_contains "$OUT" "lane-x1: unreadable · lane: done · issue: unreadable" "(i) unreadable issue reported"
assert_present "$C/state/lane-x1.meta" "(i) unreadable lane kept"
pass "(i) unreadable forge -> reported, not cleaned"

# (j) bind-issue keeps the pr= block last, replaces an earlier binding, and refuses bad input.
C=$(make_case j)
write_lane "$C" ship "pr=$PR" "pr_head=0123456789abcdef0123456789abcdef01234567"
run_recon "$C" bind-issue lane-x1 https://github.com/acme/widgets/issues/1
expect_code 0 "$RC" "(j) bind succeeds"
run_recon "$C" bind-issue lane-x1 "$ISSUE"
expect_code 0 "$RC" "(j) rebind succeeds"
assert_equals 1 "$(grep -c '^issue=' "$C/state/lane-x1.meta")" "(j) rebinding replaces the earlier issue"
assert_grep "issue=$ISSUE" "$C/state/lane-x1.meta" "(j) latest issue bound"
FM_PR_PARSE=$(bash -c '. "$1/bin/fm-pr-lib.sh"; fm_pr_metadata_identity_parse "$2" && printf %s "$FM_PR_META_URL"' _ "$ROOT" "$C/state/lane-x1.meta")
assert_equals "$PR" "$FM_PR_PARSE" "(j) the PR record still parses after binding"
run_recon "$C" bind-issue lane-x1 "$PR"
expect_code 2 "$RC" "(j) a PR URL is not an issue"
crew "$C" "done"
printf 'closed\n' > "$C/forge/issue-623"
printf 'state=OPEN\nmerged=false\n' > "$C/forge/pr-624"
run_recon "$C" check lane-x1
assert_contains "$OUT" "issue: closed · pr: open" "(j) the bound issue is what reconcile reads"
pass "(j) bind-issue binding"

# (k) An abandoned lane whose endpoint is gone reads unknown; its recorded
# declaration decides: recorded done with a merged PR is stale and cleaned,
# recorded working stays untouched.
C=$(make_case k)
write_lane "$C" ship "pr=$PR"
crew "$C" unknown
commit_work "$C" push
status_line "$C" "done [at=1790000000]: PR $PR checks green"
printf 'state=MERGED\nmerged=true\n' > "$C/forge/pr-624"
run_recon "$C" scan --apply
expect_code 0 "$RC" "(k) recorded-done stale lane is cleaned"
assert_contains "$OUT" "lane-x1: stale · lane: done (recorded) · issue: unbound · pr: merged" "(k) recorded declaration makes the lane a candidate"
assert_absent "$C/state/lane-x1.meta" "(k) abandoned lane retired through teardown"
C=$(make_case k2)
write_lane "$C" ship "pr=$PR"
crew "$C" unknown
status_line "$C" "working [at=1790000000]: implementing"
printf 'state=MERGED\nmerged=true\n' > "$C/forge/pr-624"
run_recon "$C" check lane-x1 --apply
expect_code 0 "$RC" "(k) unknown working lane needs nothing"
assert_contains "$OUT" "lane-x1: untouched · lane: unknown" "(k) recorded working stays untouched"
assert_absent "$C/forge.log" "(k) no forge read for an untouched lane"
pass "(k) unknown current state -> recorded declaration decides"
