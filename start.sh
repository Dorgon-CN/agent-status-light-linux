#!/bin/bash
# Start the status light detached from the terminal; output goes to logs/status-light.log.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
  echo "没有图形会话（DISPLAY 未设置）" >&2
  exit 1
fi
pid=$(cat data/status-light.pid 2>/dev/null)
if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q status_light.py; then
  echo "已在运行（pid $pid）"
  exit 0
fi
setsid -f /usr/bin/python3 status_light.py >/dev/null 2>&1 </dev/null
echo "已启动，日志：$PWD/logs/status-light.log"
