#!/usr/bin/env bash
# Keeps localtunnel on a FIXED subdomain in front of the sandbox gateway (:8781), so the Uber dashboard URL never
# changes. Restarts on exit or after 3 failed public health checks; alerts if the subdomain is not granted.
SB="$(cd "$(dirname "$0")" && pwd)"; SUB=nextgen-uber-sbx-q7k3; WANT="https://$SUB.loca.lt"
while true; do
  log="$SB/lt_current.log"; : > "$log"
  lt --port 8781 --local-host 127.0.0.1 --subdomain "$SUB" > "$log" 2>&1 &
  pid=$!; got=""
  for i in $(seq 1 30); do got=$(grep -o "https://[a-z0-9-]*\.loca\.lt" "$log" | head -1); [ -n "$got" ] && break; sleep 1; done
  if [ "$got" = "$WANT" ]; then echo "$(date -u -Is) START pid=$pid url=$got" >> "$SB/lt_watchdog.log"
  else echo "$(date -u -Is) ALERT subdomain not granted (got='${got:-none}'); retrying in 30s" >> "$SB/lt_watchdog.log"; kill $pid 2>/dev/null; sleep 30; continue; fi
  fails=0
  while kill -0 $pid 2>/dev/null; do
    sleep 20
    code=$(curl -s -m 15 -o /dev/null -w "%{http_code}" -H "User-Agent: nextgen-watchdog" "$WANT/health")
    if [ "$code" = 200 ]; then fails=0; else fails=$((fails+1)); echo "$(date -u -Is) health=$code fails=$fails" >> "$SB/lt_watchdog.log"; fi
    [ $fails -ge 3 ] && { echo "$(date -u -Is) RESTART (3 failed checks)" >> "$SB/lt_watchdog.log"; kill $pid; break; }
  done
  echo "$(date -u -Is) EXIT pid=$pid; restarting in 3s" >> "$SB/lt_watchdog.log"; sleep 3
done
