nexus-v1 is a fresh rebuild of the Nexus two-laptop assistant network as a modular monolith per laptop (FastAPI + Next.js + SQLite), talking through one hardened relay with decentralized trust. The locked v1 demo definition: two laptops install from source checkouts, pair via invite codes, and complete one ask → approve-on-both-ends → cited answer loop over the network.

## Model provider (V5)

Streaming chat (`GET /chat/stream`) uses a generic OpenAI-compatible
model read from machine-local config — no settings UI in v1 (it arrives
with packaging):

- `NEXUS_LLM_API_KEY` (required)
- `NEXUS_LLM_BASE_URL` (default `https://api.openai.com/v1`)
- `NEXUS_LLM_MODEL` (default `gpt-4o-mini`)

Without a key the endpoint returns 503 `MISSING_KEY` naming the variable.
