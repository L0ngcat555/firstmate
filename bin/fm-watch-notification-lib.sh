#!/usr/bin/env bash
# Watcher-owned reminder suppression, independent of agent/backend rendering.
# Only unchanged authenticated custom-check output uses this owner. Signal
# generations, merge outcomes, stale reminders, failures and wedge escalations
# retain their existing delivery paths. Full reasons stay in the durable wake
# queue; compact check batches only shorten the immediate prompt.
# FM_WATCH_REMINDER_SECS bounds unchanged reminders (default 900 seconds).
# Empty custom-check output resets its episode. Call record only AFTER durable
# append, so interruption never suppresses an event that was not queued.
# The singleton watcher owns these files; no other actor writes suppression state.
# Metrics: .watch-notification-metrics.tsv = epoch, queued|suppressed, kind,
# source fingerprint. Metrics are bounded to ~1 MiB. It records no check output
# or credentials.

fm_watch_notice_hash() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    return 1
  fi
}

fm_watch_notice_path() { # <kind> <source>
  local root="$STATE/.watch-notifications" key
  [ ! -L "$root" ] || return 1
  (umask 077; mkdir -p "$root") || return 1
  key=$(printf '%s\n%s' "$1" "$2" | fm_watch_notice_hash) || return 1
  printf '%s/%s\n' "$root" "$key"
}

fm_watch_notice_metric() { # <outcome> <kind> <source>
  local log="$STATE/.watch-notification-metrics.tsv" key size tmp
  [ ! -L "$log" ] || return 0
  key=$(printf '%s' "$3" | fm_watch_notice_hash) || return 0
  printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "$1" "$2" "$key" >> "$log" || return 0
  size=$(wc -c < "$log" | tr -d '[:space:]')
  if [ "$size" -gt 1048576 ]; then
    tmp=$(mktemp "$STATE/.watch-metrics.XXXXXX") || return 0
    tail -n 2000 "$log" > "$tmp" && mv -f "$tmp" "$log"
    rm -f "$tmp"
  fi
}

fm_watch_notice_due() { # <kind> <source> <content>
  local path fingerprint previous='' last=0 now interval=${FM_WATCH_REMINDER_SECS:-900}
  case "$interval" in ''|*[!0-9]*|0) interval=900 ;; esac
  path=$(fm_watch_notice_path "$1" "$2") || return 0
  fingerprint=$(printf '%s' "$3" | fm_watch_notice_hash) || return 0
  if [ -f "$path" ] && [ ! -L "$path" ]; then
    read -r last previous < "$path" || true
  fi
  case "$last" in ''|*[!0-9]*) return 0 ;; esac
  now=$(date +%s)
  if [ "$fingerprint" = "$previous" ] && [ "$now" -ge "$last" ] && [ "$((now - last))" -lt "$interval" ]; then
    fm_watch_notice_metric suppressed "$1" "$2"
    return 1
  fi
  return 0
}

# A CI-green hint is a durable event, not a periodic reminder. Changing the
# head/check fingerprint re-arms it. Record only after its wake was appended.
fm_watch_notice_changed() { # <kind> <source> <content>
  local path fingerprint previous='' last
  path=$(fm_watch_notice_path "$1" "$2") || return 0
  fingerprint=$(printf '%s' "$3" | fm_watch_notice_hash) || return 0
  if [ -f "$path" ] && [ ! -L "$path" ]; then
    read -r last previous < "$path" || true
  fi
  if [ "$fingerprint" = "$previous" ]; then
    fm_watch_notice_metric suppressed "$1" "$2"
    return 1
  fi
  return 0
}

fm_watch_notice_record() { # <kind> <source> <content>
  local path tmp fingerprint
  path=$(fm_watch_notice_path "$1" "$2") || return 0
  fingerprint=$(printf '%s' "$3" | fm_watch_notice_hash) || return 0
  tmp=$(mktemp "$STATE/.watch-notice.XXXXXX") || return 0
  printf '%s %s\n' "$(date +%s)" "$fingerprint" > "$tmp" && mv -f "$tmp" "$path"
  rm -f "$tmp"
  fm_watch_notice_metric queued "$1" "$2"
}

fm_watch_notice_reset() { # <kind> <source>
  local path
  path=$(fm_watch_notice_path "$1" "$2") || return 0
  rm -f "$path"
}

fm_watch_check_summary() { # <script> <full output>
  local short
  short=$(printf '%s' "$2" | tr '\t\r\n' '   ' | cut -c 1-240)
  printf 'check: %s: %s' "$1" "$short"
  [ "${#2}" -le 240 ] || printf ' [full output in wake queue]'
}
