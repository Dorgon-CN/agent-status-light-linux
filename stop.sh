#!/bin/bash
# Stop the status light started by start.sh.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
pid=$(cat data/status-light.pid 2>/dev/null)
# Check the command line so a reused pid never kills an unrelated process.
if [ -z "$pid" ] || ! tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q status_light.py; then
  echo "没有在运行"
  exit 0
fi
kill "$pid"
for _ in $(seq 10); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "已停止"
    exit 0
  fi
  sleep 0.3
done
echo "3 秒内未能停止（pid $pid）" >&2
exit 1
