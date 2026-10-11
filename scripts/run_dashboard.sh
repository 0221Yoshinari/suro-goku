#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${DASHBOARD_PORT:-8000}"
export DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
export DASHBOARD_PORT="$PORT"
export PYTHONUNBUFFERED=1

PHONE_IP=""
for iface in en0 en1; do
  ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
  if [[ -n "${ip}" ]]; then
    PHONE_IP="$ip"
    break
  fi
done

BOLD="$(tput bold 2>/dev/null || printf '\033[1m')"
GOLD="$(printf '\033[1;93m')"
RESET="$(tput sgr0 2>/dev/null || printf '\033[0m')"
BAR="████████████████████████████████████████████████████████████████████"

echo
echo "${GOLD}${BOLD}${BAR}${RESET}"
echo "${GOLD}${BOLD}  スロプロ ／ スマホ実機で LP を開く（同じWi-Fi）${RESET}"
echo "${GOLD}${BOLD}${BAR}${RESET}"
echo
if [[ -n "${PHONE_IP}" ]]; then
  echo "${GOLD}${BOLD}  スマホ閲覧用URL${RESET}"
  echo
  echo "${GOLD}${BOLD}      http://${PHONE_IP}:${PORT}/lp${RESET}"
  echo
else
  echo "${BOLD}  IPを自動取得できませんでした。次を実行してください:${RESET}"
  echo "      ipconfig getifaddr en0"
  echo "      → http://[そのIP]:${PORT}/lp"
  echo
fi
echo "${BOLD}  PC用: http://127.0.0.1:${PORT}/${RESET}"
echo "  Wi-Fi外（4G/5G）: 起動後に HTTPS URL を発行し、Discord へ [ダッシュボードを開く] リンクを送ります"
echo "  外部公開を止めるとき: TUNNEL_ENABLED=0"
echo
echo "${GOLD}${BOLD}${BAR}${RESET}"
echo

exec python3 -m src.dashboard
