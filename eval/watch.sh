#!/usr/bin/env bash
# Watch a long tool run: sample the fuzzer process's memory (and crAPI's
# containers) on an interval, and shout if memory crosses a threshold or the
# process disappears. Meant to run alongside a multi-hour campaign.
#
#   bash eval/watch.sh                 # defaults: match eval.campaign, 2000 MB, 15s
#   bash eval/watch.sh eval.campaign 4000 30
#   PATTERN=track_a bash eval/watch.sh
#
# Emits one line per sample (timestamp, RSS, crAPI mem) and a line starting with
# ALERT on a threshold breach or when no matching process is found -- so it works
# well piped into anything that reacts to "ALERT".
set -u

PATTERN="${1:-${PATTERN:-eval.campaign}}"
THRESHOLD_MB="${2:-${THRESHOLD_MB:-2000}}"
INTERVAL="${3:-${INTERVAL:-15}}"

echo "watch: pattern='$PATTERN' threshold=${THRESHOLD_MB}MB interval=${INTERVAL}s"
seen=0
while true; do
  ts=$(date +%H:%M:%S)
  # total RSS (KB) of all processes whose command matches the pattern
  rss_kb=$(ps -eo rss,args | grep -F "$PATTERN" | grep -v grep | grep -v watch.sh \
           | awk '{s+=$1} END{print s+0}')
  rss_mb=$(( rss_kb / 1024 ))

  if [ "$rss_kb" -eq 0 ]; then
    if [ "$seen" -eq 1 ]; then
      echo "ALERT $ts process matching '$PATTERN' is gone (run ended or crashed)"
      break
    fi
    echo "$ts waiting for a process matching '$PATTERN'..."
  else
    seen=1
    # crAPI container memory, if the stack is up (best-effort, non-fatal)
    crapi=$(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null \
            | grep -iE 'crapi|mongo|postgres|mailhog' | awk '{printf "%s=%s ", $1,$2}')
    line="$ts fuzzer_rss=${rss_mb}MB ${crapi}"
    echo "$line"
    if [ "$rss_mb" -ge "$THRESHOLD_MB" ]; then
      echo "ALERT $ts fuzzer RSS ${rss_mb}MB >= ${THRESHOLD_MB}MB threshold"
    fi
  fi
  sleep "$INTERVAL"
done
