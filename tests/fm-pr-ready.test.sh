#!/usr/bin/env bash
# CI hints never grant merge authority; test forge transitions and durable dedup.
set -eu
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/bin" "$TMP/state"
export STATE="$TMP/state"
# shellcheck source=bin/fm-watch-notification-lib.sh
. "$ROOT/bin/fm-watch-notification-lib.sh"
cat > "$TMP/bin/gh" <<'GH'
#!/usr/bin/env python3
import json, os, sys
a=sys.argv[1:]; mode=os.environ.get('CASE','green'); head='a'*40
if os.environ.get('GH_CALL_LOG'):
    with open(os.environ['GH_CALL_LOG'],'a') as log: log.write(json.dumps(a)+'\n')
if '--required' in a:
    if mode == 'required-pending': print('quality\tpending\t0\turl'); sys.exit(8)
    if mode == 'required-error': print('HTTP 403'); sys.exit(1)
    if mode == 'no-required': print("no required checks reported on the 'fixture' branch"); sys.exit(1)
    print('quality\tpass\t1m\turl'); sys.exit(0)
if '-q' in a:
    if a[-1] == '.state': print('MERGED' if mode == 'merged' else 'OPEN')
    else: print('OPEN '+('b'*40 if mode == 'head-race' else head))
    sys.exit(0)
check={'__typename':'CheckRun','name':'quality','status':'COMPLETED','conclusion':'SUCCESS'}
if mode == 'pending': check['status']='IN_PROGRESS'
if mode == 'failed': check['conclusion']='FAILURE'
if mode == 'unknown': check['__typename']='Other'
if mode == 'skipped': check['conclusion']='SKIPPED'
print(json.dumps({'state':'OPEN','headRefOid':head,'statusCheckRollup':[] if mode=='empty' else [check]}))
GH
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"
poll() { CASE=$1 "$ROOT/bin/fm-pr-poll.sh" --validated github https://github.com/acme/repo/pull/1 github.com acme/repo 1; }
for mode in pending failed unknown empty skipped required-pending required-error head-race; do
  [ -z "$(poll "$mode")" ] || { echo "unexpected ready: $mode" >&2; exit 1; }
done
[ "$(poll merged)" = merged ]
green=$(poll green)
[[ "$green" == ci-green:* ]]
[ "$(poll no-required)" = "$green" ]
fm_watch_notice_changed ci-green fixture "$green"
# The caller appends durably before recording; pending polls have no hint.
fm_watch_notice_record ci-green fixture "$green"
if fm_watch_notice_changed ci-green fixture "$green"; then exit 1; fi
fm_watch_notice_changed ci-green fixture "${green/a/b}"
fm_watch_notice_changed ci-green fixture "$green changed-check"
# A repository listed for local verification never polls hosted checks and
# emits only merged; an unlisted one keeps the hosted hint.
mkdir -p "$TMP/home/config"
printf '{"acme/repo":"/configured/verifier"}\n' > "$TMP/home/config/github-local-verification.json"
export GH_CALL_LOG="$TMP/gh-calls"
[ -z "$(FM_HOME="$TMP/home" poll green)" ] || { echo 'locally verified PR emitted a hosted CI hint' >&2; exit 1; }
[ "$(wc -l < "$GH_CALL_LOG")" -eq 1 ] || { echo 'locally verified PR polled hosted checks' >&2; exit 1; }
[ "$(FM_HOME="$TMP/home" poll merged)" = merged ] || { echo 'locally verified PR lost its merge notification' >&2; exit 1; }
[ -z "$(FM_HOME=/nonexistent FM_CONFIG_OVERRIDE="$TMP/home/config" poll green)" ] \
  || { echo 'FM_CONFIG_OVERRIDE did not select the local-verification list' >&2; exit 1; }
printf '{"acme/other":"/configured/verifier"}\n' > "$TMP/home/config/github-local-verification.json"
[ "$(FM_HOME="$TMP/home" poll green)" = "$green" ] || { echo 'an unlisted repository lost its hosted CI hint' >&2; exit 1; }
echo 'PASS: pending/failure/unknown/race stay silent; success, absent protection, merge, once-per-candidate wake, and local-verification silence'
