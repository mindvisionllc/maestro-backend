#!/usr/bin/env bash
# setup_local_secrets.sh — one-time interactive setup for the Twilio Verify
# credentials that could not be recovered from any running process during the
# 2026-09-14 restoration (see docs/PLMKR_RESTORE_REPORT.md).
#
# What this does:
#   - Prompts once for TWILIO_AUTH_TOKEN and TWILIO_VERIFY_SERVICE_SID.
#   - Validates each against the exact format main.py requires before writing
#     anything (main.py:13993-13995).
#   - Writes them into maestro-backend/.env (permission 600, git-ignored —
#     already covered by .gitignore) — the ONLY place backend secrets should
#     live now. Never touches ~/.bashrc, shell history, or any log.
#   - Input is read with `read -rs` (silent — not echoed to the terminal) and
#     is never printed, logged, or passed as a command-line argument (which
#     would leak into `ps`/shell history). Nothing here writes to Git.
#
# Safe to re-run: it replaces the placeholder/previous lines in .env in place.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE=".env"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Run this from a checkout of maestro-backend." >&2
  exit 1
fi

TOKEN_RE='^[0-9a-f]{32}$'
VERIFY_RE='^VA[0-9a-fA-F]{32}$'

read_secret() {
  local prompt="$1" varname="$2" pattern="$3"
  local value=""
  while true; do
    read -rs -p "$prompt: " value
    echo >&2
    if [[ "$value" =~ $pattern ]] || [[ "${value,,}" =~ $pattern ]]; then
      break
    fi
    echo "  Invalid format for $varname — try again (or Ctrl+C to abort)." >&2
  done
  printf -v "$varname" '%s' "$value"
}

echo "PLMKR local secret setup — values are never echoed or logged." >&2
echo "(Get these from console.twilio.com if you don't have them handy.)" >&2
echo >&2

read_secret "TWILIO_AUTH_TOKEN (32 lowercase hex chars)" AUTH_TOKEN "$TOKEN_RE"
read_secret "TWILIO_VERIFY_SERVICE_SID (VA + 32 hex chars)" VERIFY_SID "$VERIFY_RE"

# Replace-in-place: drop any existing line for these keys (set or commented),
# then append the real values. Done via a temp file + atomic rename so a
# crash mid-write can't corrupt .env.
tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
grep -vE '^#?\s*(TWILIO_AUTH_TOKEN|TWILIO_VERIFY_SERVICE_SID)=' "$ENV_FILE" > "$tmp" || true
{
  cat "$tmp"
  echo "TWILIO_AUTH_TOKEN=${AUTH_TOKEN}"
  echo "TWILIO_VERIFY_SERVICE_SID=${VERIFY_SID}"
} > "${tmp}.final"
mv "${tmp}.final" "$ENV_FILE"
rm -f "$tmp"
chmod 600 "$ENV_FILE"

unset AUTH_TOKEN VERIFY_SID value

echo >&2
echo "Written to $ENV_FILE (permission 600, git-ignored). Restart the backend to pick them up:" >&2
echo "  pkill -f 'uvicorn main:app'; cd $(pwd) && uvicorn main:app --host 0.0.0.0 --port 8000" >&2
