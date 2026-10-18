#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "${ROOT}/data/ACQUISITION_HALTED" && "${ALLOW_RUN:-}" != "1" ]]; then
  echo "取得は安全待機中です。再開するまで launchd は入れません（ALLOW_RUN=1 で上書き）。" >&2
  exit 0
fi
PYTHON="$(command -v python3)"
PLIST_ID="com.tamaya.slot-scraper"
DEST="${HOME}/Library/LaunchAgents/${PLIST_ID}.plist"
LOG_DIR="${ROOT}/data/logs"
mkdir -p "$LOG_DIR"

cat > "$DEST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_ID}</string>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>-m</string>
    <string>src.scheduler</string>
    <string>--loop</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>LANG</key>
    <string>ja_JP.UTF-8</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>SCHEDULE_START</key>
    <string>10:50</string>
    <key>SCHEDULE_END</key>
    <string>22:00</string>
    <key>SCHEDULE_INTERVAL_MIN</key>
    <string>30</string>
    <key>SCRAPER_MAX_WORKERS</key>
    <string>24</string>
    <key>SCRAPER_PAUSE_MIN_SEC</key>
    <string>2.0</string>
    <key>SCRAPER_PAUSE_MAX_SEC</key>
    <string>3.0</string>
    <key>PLAYWRIGHT_BROWSERS_PATH</key>
    <string>${HOME}/Library/Caches/ms-playwright</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${PLIST_ID}" >/dev/null 2>&1 || true
if ! launchctl bootstrap "gui/$(id -u)" "$DEST"; then
  launchctl load -w "$DEST"
fi
echo "installed: $DEST"
echo "source: config/halls.json の source（玉屋∞本店は teramoba）"
echo "window: 10:50–23:00 JST / resident loop every 30 min / 23:00以降は実行しない"
echo "unload: launchctl bootout gui/$(id -u)/${PLIST_ID}"
