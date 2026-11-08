#!/bin/bash
# Add "Agent 状态灯" to the application grid for the current user.
# Remove it again by deleting the printed .desktop file.
APP_DIR="$(dirname "$(readlink -f "$0")")"
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DEST="$DEST_DIR/agent-status-light.desktop"

# Exec values are double-quoted, so escape the characters the desktop entry spec reserves there.
quote_exec() {
  local value="$1"
  value="${value//\\/\\\\\\\\}"
  value="${value//\"/\\\\\"}"
  value="${value//\`/\\\\\`}"
  value="${value//\$/\\\\\$}"
  printf '"%s"' "$value"
}

mkdir -p "$DEST_DIR" || exit 1
cat > "$DEST" <<EOF || exit 1
[Desktop Entry]
Type=Application
Name=Agent Status Light
Name[zh_CN]=Agent 状态灯
Comment=Traffic light for Paseo agents
Comment[zh_CN]=用红绿灯显示 Paseo 里各 agent 的状态
Exec=/usr/bin/python3 $(quote_exec "$APP_DIR/status_light.py")
Icon=$APP_DIR/icon.png
Terminal=false
Categories=Utility;
Keywords=paseo;agent;status;light;红绿灯;状态灯;
StartupNotify=false
StartupWMClass=agent-status-light
Actions=stop;

[Desktop Action stop]
Name=Stop
Name[zh_CN]=停止状态灯
Exec=$(quote_exec "$APP_DIR/stop.sh")
EOF
echo "已添加到「全部应用」：$DEST"
