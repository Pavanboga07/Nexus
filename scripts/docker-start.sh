#!/bin/bash
# Container entrypoint: backend API (:8001) + frontend (:3001).
# Required env (fail loud, never defaults for secrets):
#   NEXUS_IDENTITY_KEY - generated per machine, see README
#   NEXUS_LLM_API_KEY  - owner's own model key
# Optional: NEXUS_LLM_BASE_URL, NEXUS_LLM_MODEL, NEXUS_RELAY_URL, PORT_API, PORT_UI
set -e
: "${NEXUS_IDENTITY_KEY:?Set NEXUS_IDENTITY_KEY (generate one per machine, never share it)}"
: "${NEXUS_LLM_API_KEY:?Set NEXUS_LLM_API_KEY (the owner model key)}"
API_PORT="${PORT_API:-8001}"
UI_PORT="${PORT_UI:-3001}"
mkdir -p "$(dirname "${NEXUS_DB_PATH:-/data/nexus.db}")"
python -m uvicorn app.main:app --host 0.0.0.0 --port "$API_PORT" &
BACK_PID=$!
cd frontend
NEXT_PUBLIC_NEXUS_API="${NEXT_PUBLIC_NEXUS_API:-http://127.0.0.1:8001}" \
PORT="$UI_PORT" npm run start &
UI_PID=$!
wait -n $BACK_PID $UI_PID
