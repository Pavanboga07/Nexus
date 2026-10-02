#!/bin/bash
# Container entrypoint: backend API (:8001) + frontend (:3001).
# Zero secrets required: the identity secret self-generates into the
# data dir on first need, and the model key is pasted in the UI (Chat
# settings, verified live before it is stored).
# Optional env overrides (all unset by default):
#   NEXUS_IDENTITY_KEY, NEXUS_LLM_API_KEY, NEXUS_LLM_BASE_URL,
#   NEXUS_LLM_MODEL, NEXUS_RELAY_URL, NEXUS_DB_PATH, PORT_API, PORT_UI
set -e
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
