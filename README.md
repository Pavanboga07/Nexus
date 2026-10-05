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

## Agent OS (Phase 5)

Bounded autonomy: persistent schedules (cron/one-shot) and durable
event triggers fire orchestrated tasks with agent autonomy levels,
budgets, and depth caps; tasks recover after restarts
(reconcile + workflow resume, no duplicate execution); memories have
TTL; chat threads are per-agent; remote cancel/status travel as
signed control actions; same task/policy/delegation gates apply to
every autonomous run. UI: autonomy levels on Agents, lineage +
workflows + schedules on Tasks. Details and honest limits in
[ARCHITECTURE.md](./ARCHITECTURE.md).

## Nexus network (Phase 4)

Independent Nexus installations discover (verified gateway directory,
never trusted) and work together through paired + TRUSTED peers:
trust lifecycle (suspend/resume/revoke), signed cross-Nexus
delegation, remote task dispatch over the existing A2A transport with
origin/remote correlation, remote failure mapping, offline
buffering with deadline bounds. UI: peer trust controls + directory
lookup on People. Details and honest limits in
[ARCHITECTURE.md](./ARCHITECTURE.md).

## Orchestration (Phase 3)

Durable tasks with an explicit state machine (`POST /tasks`,
cancel/retry/advance), an orchestrator that routes by explicit target
or capability (never guessing, never default-fallback), delegation +
policy + approval enforcement before execution, local inline run or
signed A2A dispatch, parent/child trees, idempotency keys, timeout
sweeps, and minimal workflows (sequential + parallel levels,
dependency blocking). Chat accepts `?agent=` and records per-turn
tasks. UI: Tasks page, Agents page, chat agent picker. Details and
honest limits in [ARCHITECTURE.md](./ARCHITECTURE.md).

## Architecture (Phase 2: agent-centric)

One owner operates many agents, each with its own Ed25519 identity,
memory partition, policy scope, and capabilities; agents delegate via
signed grants and talk over the existing signed A2A envelopes. See
[ARCHITECTURE.md](./ARCHITECTURE.md) for the model, lifecycle,
delegation flow, and honest limitations. Key endpoints: `/agents`,
`/agents/{ref}/card`, `/agents/discover`, `/delegations`,
`/agents/{ref}/execute`, `/agents` UI page.
