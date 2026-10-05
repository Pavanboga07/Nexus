# AI Nexus Architecture (Phase 2: agent-centric)

## Model

```text
Owner ("local", single operator per laptop)
 ├── Agent (id handle, crypto agent_id, display_name, status)
 │    ├── Identity (versioned Ed25519 keys: active → rotated → revoked)
 │    ├── Capabilities (`handle.name@vN`: schemas + optional tool binding)
 │    ├── Memory partition (memories.agent_id)
 │    ├── Policy rules (rules with agent_id '*' or named)
 │    └── Delegations issued / received
 │
 ├── Paired peers (remote agents: pinned card + key, trust at pairing)
 └── Approvals (per-correlation owner decisions, task-bound)
```

No `owners` table exists by design: one laptop, one operator. The
operator token (`NEXUS_OPERATOR_TOKEN` / `data/operator_token`) is the
human trust domain; Ed25519 is the agent trust domain. Never mixed.

## Agent lifecycle

`active` → signs, resolves in discovery, executes. `disabled` →
identity/history kept, no new work (signing, cards, discovery,
execution, and grant issuance all fail closed). `revoked` → distrusted
for new operations; keys retained for audit, never deleted. Rotation
mints v+1 and retires the old key to `rotated` (still verifies
in-flight material); revoking all keys strands signing until rotation
recovers it (tested path).

## Identity

Ed25519, self-certifying `nexus:ed25519:<pubkey-hash>` IDs, AES-GCM
sealed private keys, startup self-verification. The legacy `identity`
singleton backfills the `default` agent (same keypair, no fork).
Keys are per-agent and versioned; `resolve_signing_key` refuses
non-active agents and keyless agents.

## Capabilities & discovery

Capabilities are structured records, advertised on freshly signed
agent cards (`GET /agents/{ref}/card`: who, what with schemas, where,
how-to-verify). Discovery resolves exact agent ID → exact capability
→ exact handle → exact name, returning candidates on ambiguity and
never silently picking; substring search is opt-in (`fuzzy=true`).
Only active agents resolve. Unsigned/incomplete metadata is never
reported as verified.

## Delegation & execution

Signed grants (issuer, recipient, capability, purpose, task, ttl,
constraints, signature over a fixed field set). Verification needs
only the issuer's public key (local registry or pinned peer card).
Execution (`execute_capability`) enforces, in order: acting agent
active → capability active and owned → grant required unless
owner-self → grant valid (expiry, revocation, recipient, capability,
purpose, task, use budget, both agents usable) → local policy with
issuer as peer (DENY/ASK fail closed) → owner approval when policy
ASKs or the grant demands it (bound to the task's decided-approve
row) → bound tool runs → use audited. Delegation never bypasses
policy and is never permanent trust.

## A2A protocol (v0.3, unchanged version)

Signed envelopes, 5-minute clock-skew bound, expiry enforced (410s),
recipient must be the identity or a local agent, sender must be a
paired peer or local agent with signature verifying against the known
key, replay rejected by message-ID uniqueness, responses and
approve/reject decisions must correlate to a request we sent to that
peer (uncorrelated messages fail closed). Optional signed payload
fields: `capability_id`, `delegation_id`, `target_agent`, `reply_to`.

## Memory & policy scoping

Memories partition by `agent_id` (legacy rows → `default`); every
read path scopes by agent, sessions narrow further. Policy rules carry
`agent_id` (`'*'` = global); evaluation with an acting agent considers
global + that agent's rules, agent-less evaluation (identity-level A2A
receive) sees globals only. DENY (sensitive) > ASK (default) > ALLOW
holds in all scopes.

## Delivery lifecycle

`stored → queued → relayed | delivery_failed | local-only`, with
background retry and per-message retry. Relay outage surfaces as row
state, never a hung request.

## Database (new in Phase 2)

App migrations (append-only, `app/store.py` — entries are positional,
so new ones are ALWAYS appended last or already-migrated databases
silently skip them): v1 identity, v2 invites/peers, v3 a2a+policy, v4
`chat_threads` / `chat_turns`, v5 `agents` + `agent_identities`, v6
`policy_rules.agent_id`, v7 `capabilities`, v8 `delegations` +
`delegation_events`. Memories carry `agent_id` via `MemoryStore`
schema + ALTER backfill (legacy rows → `default`). Approval and
message rows carry no new columns: `capability_id` / `delegation_id` /
`target_agent` travel inside signed payloads and are surfaced from
there.

## What is NOT multi-agent yet

Remote-issuer revocation propagation, gateway directory client,
metrics/tracing. See the validation report for the honest list.

---

# Phase 3: Orchestration (tasks, routing, workflows)

## Orchestrator

`app/orchestration.py` coordinates without reimplementing rules:
`submit` = create → resolve → authorize → dispatch. Resolution is
explicit target (local handle/id/crypto, else paired peer) or
capability lookup (exactly one explicit match or AMBIGUOUS with
candidates — never a silent pick, never a default-agent fallback).
Authorization enforces lifecycle, delegation (agent requesters),
policy (DENY fails, ASK parks an approval card), and grant-demanded
approval. Dispatch runs local targets inline through the execution
gate or signs an A2A envelope for paired peers (caller backgrounds
delivery via `app/dispatch.py`). `advance_task` resumes
WAITING_APPROVAL after owner approval.

## Tasks

Durable rows (`tasks` + `task_events`, migration v9): owner,
requesting/target agents, capability, purpose, input/output/error,
correlation, parent/retry lineage, delegation + approval binding,
idempotency key (partial unique index), timeouts/deadlines.
Machine: PENDING → RESOLVING → AUTHORIZED → [WAITING_APPROVAL →]
DISPATCHED → RUNNING → COMPLETED; FAILED from anywhere non-terminal;
CANCELLED (RUNNING records CANCEL_REQUESTED — no false preemption).
Terminal states absorb; retry mints new tasks (max 3, transient-only).
Timeout sweep runs opportunistically on task reads. Late A2A answers
for terminal tasks are ignored, never resurrecting them. Owner sees
all; agent actors may touch only tasks they requested or target
(`require_task_actor`, enforced at service level).

## Approval as a task state

ASK policy (or grant-demanded approval) parks a normal approval card
bound by shared correlation_id + recorded approval_id; the owner
decides through the existing approve API, then `advance` resumes.
Incoming approve/reject must match an outbound request to that peer
(closes the correlation-guessing hole). API: `POST /tasks`,
`GET /tasks`, `GET /tasks/{id}` (children + events), cancel / retry /
advance.

## Workflows

Named DAGs of steps (`workflows` + `workflow_steps`), each step a real
task. Levels run concurrently (separate connections); failed
dependencies BLOCK without running; any non-completed required step
fails the flow; cancel propagates. Remote steps stay DISPATCHED and
converge through the normal A2A completion bridge
(`on_task_settled`). API: `POST /workflows/run`, list/get/cancel.

## Chat uses the orchestrator's resolution

`GET /chat/stream?agent=` resolves the acting agent (400 with
candidates when unknown), partitions recall AND extraction by that
agent, and records a durable per-turn task. Threads stay session
keyed. UI: agent picker in chat header, Tasks page (create/list/
detail/cancel/retry/advance), Agents page (existing).

## Remaining limitations (Phase 3)

No scheduler daemon (sweeps are opportunistic); no remote cancel
propagation beyond local marking; workflow steps run sync-in-request
(slow tools hold the HTTP call); `target_agent` in A2A payloads is
carried, not auto-routed on receipt; metrics/tracing still absent.

---

# Phase 4: Nexus network (peers, remote discovery, cross-Nexus tasks)

## Gateway audit (verified, not rewritten)

The relay is transport + buffering + verified directory, not
authority: Ed25519 challenge auth (single-use PG challenges),
routing, offline queue (PG claim/ack/DLQ/expiry/eviction), signed
cards verified on directory write with 409 handle conflicts, invite
TTLs, DB presence, metrics. ACK means transport delivery, never
application completion. `live_sockets`/`pending_acks` are
process-local (single-replica boundary, documented). The app never
trusted the gateway for authorization — and still doesn't.

## Peer trust (local model)

`paired_peers` carries `trust_state` (TRUSTED default for legacy rows
via migration v10) and `last_seen_at` (bumped on authenticated
ingest). Lifecycle: pairing ceremony → TRUSTED; SUSPENDED parks all
new sends/receives (history kept); REVOKED additionally fails
delegation verification; unpair deletes. Send, receive, and grant
evaluation all gate on TRUSTED; unknown revocation state never counts
as valid. Endpoints: `POST /pairing/peers/{id}/trust`. Peers list
derives `online` (seen < 15 min, informational only).

## Remote discovery

`app/remote_directory.py` reads the gateway directory and verifies
EVERY card through the single canonical path
(`relay.directory.verify_card`); failures are typed
(NOT_FOUND/GATEWAY_UNREACHABLE/INVALID_CARD), queries use encoded
params, agent IDs are pattern-checked before interpolation. Verified
cards are DISCOVERY metadata only: dispatch still requires a paired
+ TRUSTED peer. Search returns `{entries, rejected}`.

## Cross-Nexus tasks

Same task machine (no second state machine): remote dispatch signs
with the requesting agent's own key over sender-aware delivery,
payload carries task/capability/delegation/target (+reply_to parent).
Responses complete origin tasks with `remote_task_id` recorded;
remote `error` envelopes fail tasks as `REMOTE_<code>` only when the
sender matches the task target (strangers can't fail your tasks).
Timeout sweep, retry lineage, idempotency, and parent closing work
unchanged across the boundary. Offline peers buffer at the gateway
(existing queue); deadlines bound the wait; cancellation marks
locally and late answers are ignored, never resurrected.

## Revocation propagation

Immediate locally (grant status + trust checks on every use);
remotely via trust state (suspend/unpair the peer and their grants
fail here) bounded by grant expiry. Short TTLs recommended for
cross-Nexus grants. Unknown state is conservative: reject.

## Delivery semantics (exact)

`queued` = accepted locally. `sent` = stored, delivery attempted.
`relayed` = TRANSPORT acked by the gateway. `delivery_failed` /
`local-only` = terminal locally. `COMPLETED` = application answer
correlated — never from socket writes. `REMOTE_*` = the far side
reported failure, origin preserved.

## Remaining limitations (Phase 4)

No remote status protocol (intermediate states unwired — see Phase
5); no remote cancel message (see Phase 5 control actions); no
handle expiry on gateway; directory enumeration open (gateway-side);
multi-replica gateway needs shared presence/acks (PG-ready, not
done); per-agent chat sessions (see Phase 5).

---

# Phase 5: Agent OS (autonomy, durability, recovery)

## Autonomy model

Autonomy is a permission, never an assumption. Agents carry a level
(`disabled` / `approval_required` / `limited` / `enabled`, default
`limited`). Owner-initiated work (`user`, `chat` origins) always
passes; scheduled/triggered work passes through the level:
`disabled` refuses, `approval_required` parks owner consent first,
`limited`/`enabled` take the normal path. Every autonomous run
records origin, depth, root task, and trigger for audit.

## Durable tasks & recovery

Task states add `APPROVAL_EXPIRED` (bound card lapsed — terminal, no
silent late execution). Startup runs `reconcile_tasks` (deadlines →
TIMEOUT, stale RUNNING → WORKER_GONE since execution lived
in-process) plus `recover_workflows` (completed steps sync, live
steps requeue, flows return to PENDING for explicit resume — nothing
auto-executes on boot). A background scheduler loop (env-gated)
fires due schedules and drains events. Idempotency keys
(`sched:{id}:{fire}`, `trig:{trigger}:{event}`) make restarts
replay-safe: replays return existing work, never duplicates.

## Scheduler & triggers

Schedules (cron with a minimal no-dependency parser, or one-shot)
persist fire times; catch-up fires once, never storms. Events are
durable rows; triggers match type + filter subset with depth caps
(MAX_TRIGGER_DEPTH) and per-event idempotency, so cascades terminate
instead of looping. Scheduled/triggered runs go through the full
orchestrator (resolve → delegation → policy → approval → execute).

## Remote tasks & cancellation

Remote control travels as signed `request` envelopes with
`action: task_cancel | task_status` (no protocol break — the deployed
gateway validates only v0.3 shapes). Cancel requires the sender to be
a party of the referenced live task; terminal tasks ignore it;
RUNNING records CANCEL_REQUESTED instead of lying. Status updates
append `TASK_REMOTE_*` timeline events without moving task state —
completion still arrives only via correlated responses. Unknown
senders and unknown correlations are ignored, always acked at
transport.

## Memory & sessions

Memories carry TTL (`expires_at`): recall and listing exclude lapsed
rows, `forget_expired()` sweeps, export/import preserve the field,
audit keeps the forget record. Chat threads are agent-owned
(`chat_threads.agent_id`): history, listing, and recall scope per
agent; cross-agent reads 403 (UI passes the selected agent; legacy
calls stay open). Extraction records into the acting agent's
partition, so recall/extraction can no longer disagree.

## Workflows

Pause/resume (levels check between runs), crash recovery (completed
steps sync from task outputs, live steps requeue, flows to PENDING),
concurrent levels with CAS-claimed steps (double runners can't
double-execute), deferred-not-blocked semantics for not-yet-ready
dependencies, and the completion bridge (terminal tasks sync their
steps, including remote convergence).

## Observability

Request IDs (existing) now join correlation/task/parent/trigger/root
lineage on every task row; task detail exposes children + full event
timeline including remote status. Structured logs on delivery,
ingest, extraction, scheduler ticks.

## Remaining limitations (Phase 5)

No scheduler daemon outside the server process (ticks ride lifespan);
intermediate remote states are advisory events, not states; no remote
cancel message type (action-based, documented); multi-replica
gateway/scheduler coordination not done; metrics/tracing still
absent; UI verified in bundles, not a live browser.
