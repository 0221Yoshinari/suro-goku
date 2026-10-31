#!/usr/bin/env bash
# スロ極のX投稿を、日本時間の 11:50 / 14:00 / 17:00 / 19:00 / 21:00 に本番実行する。
# 朝 10:00 のエリア宣言は行わない（auto_poster も morning をスキップする）。
# 各5分前に、Discordへ投稿予告と本文プレビューを送る。Xへは出さない。
# 本番は --dry-run も --discord も付けない。日付はスクリプト側で日本時間の当日になる。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
LOG_DIR="${ROOT}/data/logs"
mkdir -p "$LOG_DIR"

install_one() {
  local slot="$1"
  local hour="$2"
  local minute="$3"
  local plist_id="com.tamaya.suro-poster.${slot}"
  local dest="${HOME}/Library/LaunchAgents/${plist_id}.plist"
  cat > "$dest" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${plist_id}</string>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${ROOT}/scripts/auto_poster.py</string>
    <string>--slot</string>
    <string>${slot}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/poster-${slot}.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/poster-${slot}.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>LANG</key>
    <string>ja_JP.UTF-8</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>PLAYWRIGHT_BROWSERS_PATH</key>
    <string>${HOME}/Library/Caches/ms-playwright</string>
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/${plist_id}" >/dev/null 2>&1 || true
  if ! launchctl bootstrap "gui/$(id -u)" "$dest"; then
    launchctl load -w "$dest"
  fi
  echo "installed: $dest (${hour}:$(printf '%02d' "$minute") JST --slot ${slot})"
}

install_preview() {
  local slot="$1"
  local hour="$2"
  local minute="$3"
  local plist_id="com.tamaya.suro-poster.preview-${slot}"
  local dest="${HOME}/Library/LaunchAgents/${plist_id}.plist"
  cat > "$dest" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${plist_id}</string>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${ROOT}/scripts/auto_poster.py</string>
    <string>--slot</string>
    <string>${slot}</string>
    <string>--discord</string>
    <string>--allow-off-hours</string>
    <string>--heads-up</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/poster-preview-${slot}.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/poster-preview-${slot}.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>LANG</key>
    <string>ja_JP.UTF-8</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>PLAYWRIGHT_BROWSERS_PATH</key>
    <string>${HOME}/Library/Caches/ms-playwright</string>
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/${plist_id}" >/dev/null 2>&1 || true
  if ! launchctl bootstrap "gui/$(id -u)" "$dest"; then
    launchctl load -w "$dest"
  fi
  echo "installed: $dest (${hour}:$(printf '%02d' "$minute") JST preview ${slot})"
}

launchctl bootout "gui/$(id -u)/com.tamaya.suro-poster.morning" >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)/com.tamaya.suro-poster.preview-morning" >/dev/null 2>&1 || true
rm -f "${HOME}/Library/LaunchAgents/com.tamaya.suro-poster.morning.plist"
rm -f "${HOME}/Library/LaunchAgents/com.tamaya.suro-poster.preview-morning.plist"

install_one pickup 11 50
install_one midday 14 0
install_one afternoon 17 0
install_one evening 19 0
install_one night 21 0
install_preview pickup 11 45
install_preview midday 13 55
install_preview afternoon 16 55
install_preview evening 18 55
install_preview night 20 55
echo "unload: launchctl bootout gui/$(id -u)/com.tamaya.suro-poster.midday"
echo "         launchctl bootout gui/$(id -u)/com.tamaya.suro-poster.evening"
