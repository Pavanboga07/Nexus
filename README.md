nexus-v1 is a fresh rebuild of the Nexus two-laptop assistant network as a modular monolith per laptop (FastAPI + Next.js + SQLite), talking through one hardened relay with decentralized trust. The locked v1 demo definition: two laptops install from source checkouts, pair via invite codes, and complete one ask → approve-on-both-ends → cited answer loop over the network.

## Model provider (V5)

Streaming chat (`GET /chat/stream`) uses a generic OpenAI-compatible
model read from machine-local config:

- model key: paste it in the UI (Chat → Model key, verified live before
  it is stored) or set `NEXUS_LLM_API_KEY` — the stored key wins, and a
  newly saved key applies without a restart
- `NEXUS_LLM_BASE_URL` (default `https://api.openai.com/v1`)
- `NEXUS_LLM_MODEL` (default `gpt-4o-mini`)

Without a key the endpoint returns 503 `MISSING_KEY` naming the settings
box. `GET /settings/llm-status` reports whether a key is configured
(never the value); `POST /settings/llm-key` verifies then stores it.
