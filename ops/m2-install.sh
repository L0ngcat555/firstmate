#!/usr/bin/env bash
# m2-install.sh — idempotent ensure for the FirstMate progress pane.
#
# No pane manager runs on this host, so this script *is* the ensure path: it
# creates the labelled pane on first run and restarts the view in place when it
# finds an idle shell. It never focuses, resizes, closes, or kills any pane or
# tab, and it never touches a pane that is not its own (exact title match only).
#
# Writes: config/progress.json (only when absent), the cache dir, the pane.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
HERDR=${HERDR_BIN:-herdr}
CONFIG=${FM_PROGRESS_CONFIG:-$ROOT/config/progress.json}
DEFAULT_PANE_LABEL='ombra · progress'
DEFAULT_WORKSPACE_LABEL='ombra agents'
DEFAULT_CACHE_DIR='~/.cache/fm-progress'
VIEW="$ROOT/bin/progress"
WATCH_SECONDS=15

die() { local code=$1; shift; printf 'm2-install: %s\n' "$*" >&2; exit "$code"; }

# --- config: create from documented defaults only when absent ---------------
if [ ! -e "$CONFIG" ]; then
  mkdir -p -- "$(dirname -- "$CONFIG")"
  python3 - "$CONFIG" "$HOME/ombra-pilot" "$ROOT/state" <<'PY' || die 2 "cannot create $CONFIG"
import json, sys
path, pilot_home, state_dir = sys.argv[1:4]
cfg = {
    "repos": ["L0ngcat555/ombra"],
    "pilot_home": pilot_home,
    "state_dir": state_dir,
    "staging_url": "https://ombra-web-dev.ombra.workers.dev/api/health",
    "pane_label": "ombra · progress",
    "workspace_label": "ombra agents",
    "cache_dir": "~/.cache/fm-progress",
    "window_hours": 12,
}
try:
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
except FileExistsError:
    pass
PY
fi

cfg=$(python3 - "$CONFIG" "$DEFAULT_PANE_LABEL" "$DEFAULT_WORKSPACE_LABEL" "$DEFAULT_CACHE_DIR" <<'PY'
import json, sys
path, *defaults = sys.argv[1:5]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception as exc:  # malformed config must not be guessed around
    sys.exit("config unreadable: %s: %s" % (path, exc))
if not isinstance(data, dict):
    sys.exit("config unreadable: %s: not a JSON object" % path)
for key, default in zip(("pane_label", "workspace_label", "cache_dir"), defaults):
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        value = default
    print(value.replace("\t", " ").replace("\n", " "))
PY
) || die 2 "cannot read $CONFIG"
mapfile -t cfg_lines <<<"$cfg"
PANE_LABEL=${cfg_lines[0]}
WORKSPACE_LABEL=${cfg_lines[1]}
CACHE_DIR=${cfg_lines[2]}

case "$CACHE_DIR" in
  "~/"*) CACHE_DIR="$HOME/${CACHE_DIR#\~/}" ;;
  "~")   CACHE_DIR="$HOME" ;;
esac
mkdir -p -- "$CACHE_DIR"

command -v "$HERDR" >/dev/null 2>&1 || die 127 "herdr not found (HERDR_BIN=$HERDR)"
[ -x "$VIEW" ] || die 2 "progress launcher missing or not executable: $VIEW"

# Serialize concurrent first runs so two of them cannot each create a pane.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$CACHE_DIR/.m2-install.lock"
  flock 9
fi

# --- discovery --------------------------------------------------------------
# Prints "" (absent), "<pane_id>", or "DUPLICATE<TAB>id id" (never a guess).
find_pane() {
  "$HERDR" pane list | python3 -c '
import json, sys
label = sys.argv[1]
payload = json.load(sys.stdin)
panes = payload.get("result", {}).get("panes") or []
hits = []
for pane in panes:
    titles = [pane.get(k) for k in ("terminal_title", "terminal_title_stripped", "label", "title")]
    if label in titles:
        hits.append(pane.get("pane_id") or "?")
if len(hits) > 1:
    print("DUPLICATE\t" + " ".join(hits))
elif hits:
    print(hits[0])
' "$PANE_LABEL"
}

# Prints "shell<TAB>detail", "progress<TAB>detail" or "other<TAB>detail".
pane_state() {
  "$HERDR" pane process-info --pane "$1" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
procs = (payload.get("result", {}).get("process_info", {}) or {}).get("foreground_processes") or []
shells = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh"}
cmd = " | ".join(
    p.get("cmdline") or " ".join(p.get("argv") or []) for p in procs
).strip()
if any(
    "ops.progress" in (p.get("cmdline") or "")
    or any("ops.progress" in a or a.endswith("/bin/progress") for a in (p.get("argv") or []))
    for p in procs
):
    print("progress\t" + cmd)
elif procs and all((p.get("name") or "") in shells for p in procs):
    print("shell\t" + cmd)
else:
    print("other\t" + (cmd or "(no foreground process)"))
'
}

resolve_workspace() {
  local ws
  ws=$("$HERDR" workspace list | python3 -c '
import json, sys
label = sys.argv[1]
payload = json.load(sys.stdin)
spaces = payload.get("result", {}).get("workspaces") or []
hits = [w.get("workspace_id") for w in spaces if w.get("label") == label]
hits = [h for h in hits if h]
if len(hits) > 1:
    print("DUPLICATE\t" + " ".join(hits))
elif hits:
    print(hits[0])
' "$WORKSPACE_LABEL")
  case "$ws" in
    DUPLICATE*)
      die 2 "workspace label '$WORKSPACE_LABEL' matches several workspaces: ${ws#DUPLICATE$'\t'}" ;;
    "")
      ws=$("$HERDR" pane current | python3 -c '
import json, sys
pane = json.load(sys.stdin).get("result", {}).get("pane") or {}
print(pane.get("workspace_id") or "")
')
      [ -n "$ws" ] || die 1 "cannot resolve a workspace (no '$WORKSPACE_LABEL', no current pane)"
      printf 'm2-install: workspace label not found; using current pane workspace %s\n' "$ws" >&2
      ;;
  esac
  printf '%s\n' "$ws"
}

wait_for_shell() {
  local pane=$1 attempt state
  for attempt in $(seq 1 50); do
    state=$(pane_state "$pane" 2>/dev/null) || state=""
    case "$state" in
      shell*|progress*) return 0 ;;
    esac
    sleep 0.2
  done
  return 1
}

start_view() {
  local pane=$1 label_esc bin_q
  label_esc=${PANE_LABEL//\\/\\\\}
  label_esc=${label_esc//\"/\\\"}
  bin_q=$(printf '%q' "$VIEW")
  # Sets the terminal title, then execs the view (ESC 0x1b, BEL 0x07).
  "$HERDR" pane run "$pane" 'printf "\033]2;'"$label_esc"'\007"; exec '"$bin_q"' --watch '"$WATCH_SECONDS" \
    || die 1 "herdr pane run failed for $pane"
}

# --- ensure -----------------------------------------------------------------
row=$(find_pane) || die 1 "herdr pane list failed"
case "$row" in
  DUPLICATE*)
    die 2 "pane label '$PANE_LABEL' matches several panes: ${row#DUPLICATE$'\t'}" ;;
  "")
    ws=$(resolve_workspace)
    created=$("$HERDR" tab create --workspace "$ws" --cwd "$ROOT" \
                --label "$PANE_LABEL" --no-focus) \
      || die 1 "herdr tab create failed in workspace $ws"
    pane=$(printf '%s' "$created" | python3 -c '
import json, sys
result = json.load(sys.stdin).get("result") or {}
pane = result.get("root_pane") or {}
print(pane.get("pane_id") or "")
') || die 1 "cannot parse herdr tab create response"
    [ -n "$pane" ] || die 1 "herdr tab create returned no root_pane.pane_id"
    "$HERDR" pane rename "$pane" "$PANE_LABEL" >/dev/null \
      || die 1 "herdr pane rename failed for $pane"
    wait_for_shell "$pane" \
      || die 4 "pane $pane created but no shell became ready; re-run this installer"
    start_view "$pane"
    printf 'pane: %s\n' "$pane"
    printf 'status: created\n'
    exit 0
    ;;
  *)
    pane=$row
    state=$(pane_state "$pane") || die 1 "herdr pane process-info failed for $pane"
    kind=${state%%$'\t'*}
    detail=${state#*$'\t'}
    case "$kind" in
      progress)
        printf 'pane: %s\n' "$pane"
        printf 'status: running\n'
        printf 'progress view already running; nothing to do\n'
        exit 0
        ;;
      shell)
        start_view "$pane"
        printf 'pane: %s\n' "$pane"
        printf 'status: restarted\n'
        exit 0
        ;;
      *)
        die 3 "pane $pane ('$PANE_LABEL') is running something else: $detail"
        ;;
    esac
    ;;
esac
