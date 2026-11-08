#!/usr/bin/env bash
# Keeps a Cloudflare quick tunnel (http2, IPv4) in front of the sandbox gateway on :8781.
# Restarts it when the public /health check fails 3 times in a row; writes the current URL to current_tunnel_url.txt.
SB="$(cd "$(dirname "$0")" && pwd)"; CF="/c/Program Files (x86)/cloudflared/cloudflared"
while true; do
  ts=$(date -u +%Y%m%dT%H%M%SZ); log="$SB/tunnel_$ts.log"
  "$CF" tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 --url http://127.0.0.1:8781 > "$log" 2>&1 &
  pid=$!; url=""
  for i in $(seq 1 60); do url=$(grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$log" | head -1); [ -n "$url" ] && grep -q "Registered tunnel" "$log" && break; sleep 2; done
  echo "$url" > "$SB/current_tunnel_url.txt"; echo "$(date -u -Is) START pid=$pid url=$url log=$(basename "$log")" >> "$SB/watchdog.log"
  fails=0
  while kill -0 $pid 2>/dev/null; do
    sleep 30
    code=$(curl -s -m 15 -o /dev/null -w "%{http_code}" "$url/health")
    if [ "$code" = 200 ]; then fails=0; else fails=$((fails+1)); echo "$(date -u -Is) health=$code fails=$fails" >> "$SB/watchdog.log"; fi
    [ $fails -ge 3 ] && { echo "$(date -u -Is) RESTART (3 failed checks)" >> "$SB/watchdog.log"; kill $pid; break; }
  done
  sleep 3
done
