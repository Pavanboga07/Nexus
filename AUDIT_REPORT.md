# Audit Report: nexus-v1

## 1. Summary
- **What it is:** A local-first, two-laptop AI assistant network. Each laptop runs a modular monolith (FastAPI + SQLite backend, Next.js frontend) with streaming LLM chat, vector/FTS memory, and signed peer-to-peer ask/approve/answer messaging relayed through a shared hosted relay (`wss://nexus-gateway-mv63.onrender.com`) with decentralized trust (Ed25519 identities, fingerprint comparison). Assumed goal (from `README.md:1`): the "locked v1 demo" — two laptops pair via invite codes and complete one ask → approve → cited answer loop. Audience: the developer plus one non-technical friend running the Docker image (`FRIEND_SETUP.md`).
- **Audit mode: DEMO.** Judged on whether the main flows work convincingly. Production concerns (auth, scale) are noted but weighted lightly, except where they threaten the demo or user data.
- **Assumption:** single mutually-trusting user per laptop; unauthenticated localhost API is intended, not an oversight. All "missing auth" notes follow from this.
- **Verdict:** The app is **salvageable and mostly working, not sound as-is**. I verified the full friend-chat loop live (sign → queue → relay transport → live bridge → approval → answer → receipt) and the AI chat stream against the real provider. But the demo stands on three fragile legs: a free-tier relay that sleeps (I hit `RELAY_UNREACHABLE` twice during this audit), a live `machine.json` holding a real API key + identity secret that is one careless `git add -A` from being committed, and an entire milestone of work (LangGraph, threads, provider config, UI remodel) sitting uncommitted and CI-unseen. Fix the secret hygiene + relay flakiness first; the rest is real, tested functionality with ordinary tech-debt edges.
- **What I could not verify:** rendered visuals/responsive layout (no browser tooling — HTML/CSS delivery confirmed via curl only); a real two-human pairing (simulated with two local instances instead); behavior under load; OpenAI-provider path (only the configured provider path was exercised live); fresh install from scratch (dependencies were preinstalled).

## 2. Scorecard

| Area | Score (1-10) | Confidence | One-line justification |
|---|---|---|---|
| Functionality (claimed vs real) | 7 | HIGH | Core flows verified live; deductions for relay-sleep flakiness and minor validation cosmetics. |
| Usability | 6 | MEDIUM | Clean ChatGPT-like UI with starters/skeletons/busy states; no onboarding, raw provider errors, contrast wart. |
| Architecture | 6 | HIGH | Coherent monolith+relay split, real layering; 1600-line page, per-request graph compile, dual DB helpers. |
| Implementation quality | 6 | HIGH | Parameterized SQL, typed errors, SSRF guards; offset by broad-except culture and zero logging. |
| Build-vs-reuse decisions | 7 | HIGH | Frameworks fit; hand-rolled provider/extractor justified; LangGraph thin but genuine. |
| Security | 5 | HIGH | No secrets in git, sealed identity, no XSS sinks; machine.json near-miss + vuln deps drag it down. |
| Reliability & performance | 6 | MEDIUM | Timeouts, retries, background delivery all present; relay sleep + per-stream saver setup are weak spots. |
| Tests, docs, DX | 5 | HIGH | 210 substantive tests green + CI; 16-line README, zero frontend tests, zero lint. |
| **Overall (weighted for the audit mode)** | **6** | — | Demo-weighted: Func .30, Usab .20, Arch .15, Impl .10, Sec .10, Rel .05, Tests .05, Reuse .05. Acceptable with notable gaps. |

## 3. Feature ledger

| Feature | Claimed status | Actual | Evidence |
|---|---|---|---|
| Streaming AI chat (SSE token/tool/done) | Working | WORKING | Live: `GET /chat/stream` → token + done events, real provider text; `app/api/routes/chat.py:191`; tests `test_stream.py` green |
| Chat history / threads | Working | WORKING | Live: stream persisted thread, `GET /chat/threads/{id}` returned both turns; `test_chat_threads.py` (3 tests) green |
| Memory search/list/forget | Working | WORKING | Live CRUD round-trip (import→list→update→delete) all 200; `app/api/routes/memory.py:48,59,73` |
| Memory export/import/edit/bulk-delete/audit | Working | WORKING | `PUT /{id}`, `POST /bulk-delete`, `GET /audit` exercised live 200; `test_memory_extra.py` green |
| Pairing invite/claim/approve/unpair | Working | WORKING | Live two-instance pairing both directions, fingerprints matched; `test_pairing.py` green |
| Ask → approve → answer + retry | Working | WORKING | Live A→B→A loop: `relayed`, approval parked, answer received; `test_a2a_loop.py` green |
| Live relay bridge (`/ask/live`) | Working | WORKING | Live: `ready` + `delivery` frames received over test sockets; `app/api/routes/ask.py:472+` |
| Approval/invite expiry enforcement | Working | WORKING | Code `app/a2a/service.py:497-519` (410s), `app/pairing.py:299-312`; `test_approval_expiry.py` green |
| Model key verify-before-store + provider/model config + model list | Working | WORKING | Live: status shows `base_url`/`model`; `GET /settings/llm-models` returned real IDs; `test_llm_provider_config.py` (8 tests) green |
| Full backup export | Working | WORKING | Live: `/backup` returned identity/memories/peers/policy/audit, no private material; `test_backup_and_me.py` green |
| Own identity endpoint | Working | PARTIAL | `GET /pairing/me` returns card when secret matches; correctly reports 503 `NO_IDENTITY` on mismatch — but see §5G |
| Friend Docker image | Working | WORKING | Built, smoke-tested (health/UI/threads/Gemini defaults all 200), pushed; verified no secrets baked in |
| Schedules | Not claimed | MISSING | No route, table, or UI (`grep schedul app/` empty). Not in README/demo scope — correctly absent, not a gap |
| Auth on local API | Not claimed | MISSING (by design) | No auth middleware anywhere; acceptable iff localhost-only assumption holds — it does (`uvicorn 127.0.0.1:8001`) |
| Rate limiting | Not claimed | MISSING | `grep slowapi|throttle` empty; localhost-only makes this Low severity |
| Frontend tests / lint / formatting | Implied by CI | MISSING | No test files under `frontend/`; no eslint/prettier/ruff configs anywhere; CI runs only `tsc` + `build` for frontend |

## 4. Build vs. reuse map

| Capability | Approach | Verdict | Instead |
|---|---|---|---|
| Web API | FRAMEWORK (FastAPI) | good | — |
| UI | FRAMEWORK (Next.js 14 + Tailwind) | good | — |
| LLM calls | CUSTOM-BUILT (`OpenAICompatibleProvider`, httpx) | good | LangChain would add a dependency tree for a surface this small; the hand-rolled loop is hardened (shape contracts, quarantine, typed errors) and fully tested |
| Agent orchestration | LIBRARY (LangGraph 1.2) | acceptable | Genuine use (state, edges, SQLite checkpointer) but thin: 4 linear nodes, graph recompiled per request (`chat.py:64-69`), saver opened per stream. Keep, hoist compile |
| Memory vectors | LIBRARY (sqlite-vec) + CUSTOM rank blend | good | Documented blend rationale in `memory/store.py:14-25` |
| Fact extraction | CUSTOM heuristic (regex) | acceptable | Honestly documented as heuristic (`extract.py:3-8`); an LLM extractor is the obvious upgrade, not a framework |
| Migrations (app) | CUSTOM in-code runner | acceptable | Documented choice (`store.py:3-8`); alembic retained for relay only — coherent but confusing side-by-side |
| Migrations (relay) | FRAMEWORK (alembic, targets `relay.models`) | good | — |
| Identity/crypto | LIBRARY (cryptography) + thin service | good | — |
| WS client | CUSTOM (`relay_client.py`) | acceptable | Small, tested surface; no need for a heavier client |
| State (frontend) | ESTABLISHED PATTERN (useState/useCallback) | good | No store library needed at this size |
| API client (frontend) | RAW (per-page `api()` copies in chat/people/memory/threads-nav) | reinvented wheel | One shared `app/components/api.ts`; 4 copies already drift (422-list handling differs) |
| Validation errors to UI | RAW | risky | 422 list-details reach `ErrorState` unformatted (cosmetic, no crash — React renders string arrays) |

## 5. Findings by area

### A. Functionality
- **Relay sleep breaks the demo path (High, HIGH).** Observed twice: `POST /pairing/invites` → 502 `RELAY_UNREACHABLE: relay is down: The read operation timed out`, and a bare `wss://…/ws` connect failure. Cause: Render free tier sleeps after ~15 min idle; `CONNECT_TIMEOUT = 3.0s` (`relay_client.py`) cannot survive a ~60s cold start. Wake via `/health` then retry works, but a first-time friend demo fails on first click.
- **Stale model IDs 404 opaquely (Medium, HIGH).** Retired Gemini model (`gemini-2.5-flash`) → provider-verbatim `404 … chat/completions` surfaced raw in chat. No model allow-list check at save; the UI "Load models" picker mitigates but the failure message never suggests it.
- **Self-pairing is possible and confusing (Medium, HIGH).** This laptop's only peer shared its own agent ID (pre-existing state). Loopback messages can never deliver (correct replay rejection, `service.py` UNIQUE constraint) yet the UI lets you send them; they sit `delivery_failed` with no explanation.
- **CORS test is stale (Low, HIGH).** `tests/test_cors.py:1` docstring says frontend is `:3001`; `app/main.py:17-24` now allows `:3000` too. Test still passes but no longer covers the actual matrix.

### B. Usability
- **No first-run path (High, MEDIUM).** `README.md` is 16 lines, provider-only. No setup, run, pair, or troubleshoot instructions. `FRIEND_SETUP.md` covers the Docker friend well; the developer path (clone → pip/npm → env → run ports) exists nowhere. A new user cannot start without guidance.
- **Raw provider errors reach users (Medium, HIGH).** 401/404 bodies are forwarded verbatim into chat (`provider.py` error taxonomy is good engineering, but the UI shows `provider rejected the key (HTTP 401): {…platform.openai.com…}` with no next step).
- **Small accent text likely fails contrast (Medium, MEDIUM).** Links/citations use `text-accent` (`#10a37f` on white ≈ 3.0:1, below WCAG AA 4.5:1 for small text). Status text correctly uses darker emerald-700. Needs measured confirm (no browser tooling available).
- **Keyboard/a11y basics present but thin (Low, HIGH).** Native buttons, `htmlFor` labels, focus rings, `aria-live`/`role=alert` throughout (verified by grep + HTML). Gaps: no skip link, no Escape handling (only Enter-to-send, `page.tsx:1561`), sidebar not collapsible on desktop, mobile rendering UNVERIFIED.
- **Multi-instance needs tribal knowledge (Low, HIGH).** Second copy needs its own DB/secret/ports/API URL plus CORS-compatible port; nothing documents this (I verified it works, but only by reading code).

### C. Architecture
- **Giant files (Medium, HIGH).** `frontend/app/chat/page.tsx` 1627 lines, `app/llm/provider.py` 643, `app/a2a/service.py` 651, `app/api/routes/ask.py` 561, `app/pairing.py` 458. The chat page mixes orchestration, 10+ state slices, and all JSX; change risk concentrates here.
- **Two SQLite open-patterns in one module (Low, HIGH).** `ask.py:72` `get_conn()` (generator) vs `:81` `_request_conn()` (direct) — both legitimately used (sync vs async routes) but the split is subtle and uncommented at use sites.
- **LangGraph adds ceremony per request (Low, HIGH).** `build_graph()` compiles and `AsyncSqliteSaver.from_conn_string()` opens on every stream (`chat.py:64-69`). Durability value is marginal (turns already persist in the app DB; a crashed stream does not resume mid-turn). Works, but the framework earns its keep only if branching/memory nodes arrive.
- **Migrations live in two systems (Low, HIGH).** In-code `MIGRATIONS` for the app DB (`store.py`), alembic for the relay (`alembic/env.py` → `relay.models`). Each is defensible; together they confuse (a reader expects alembic to own the app schema).

### D. Implementation quality
- **Zero application logging (Medium, HIGH).** `grep getLogger|print( app/` empty. Uvicorn access logs exist, but extraction failures, pump outcomes, and relay faults are invisible (several comments admit "logged nowhere"). Debugging is printf-by-test only.
- **Broad-except culture, mostly annotated (Medium, HIGH).** 23 `except Exception` sites; most carry honest `# noqa: BLE001` reasons (SSE well-formedness, best-effort teardown). Unannotated/interesting: `extract.py:104,116` (background extraction silently dies), `crypto.py:101` (non-signature crypto faults masked as "invalid signature" — Low).
- **Exact-string API contracts in tests (Low, HIGH).** `assert resp.json() == {"ok": True}` (`test_machine_config.py:136`) forbids ever extending that response; the models list had to become a separate endpoint partly for this reason.
- **Consistency is otherwise good (note).** Naming, typed `ApiError`/`A2AError`/`PairingError` shapes, and error-code conventions are uniform — no sign of disconnected-session drift. Git history shows small focused commits by one author.

### E. Build vs. reuse
- Covered in §4. Dependency audit: `requirements.txt` is **almost entirely unpinned** (fastapi, uvicorn, pydantic, httpx, pytest, sqlalchemy, asyncpg, alembic, langgraph, aiosqlite; only `cryptography==46.0.1` + `sqlite-vec==0.1.6` pinned). A fresh `pip install` today pulled langgraph 1.2.12 successfully (verified), but CI can break on any upstream major.
- `package-lock.json` exists (deterministic frontend installs). No duplicate HTTP/date libs. `langsmith` arrived transitively with langgraph (no-op without API key — verified absent from code usage).

### F. AI-generated code smells
- **Mild.** No mock-theater (tests drive real ASGI/HTTP/SQLite), no `any`/`ts-ignore`, no commented-out blocks, no TODOs (grep clean). Two genuine smells: (1) four copies of the frontend `api()` helper (copy-paste drift across sessions); (2) docstring/comment claims outliving code — `test_cors.py:1` (`:3001` only), `Dockerfile.friend` briefly pinned a stale model (fixed this session, now uncommitted). Missing "boring glue" is the real pattern: no `.env.example`, no seed data, no logging, no rate limits.

### G. Security
- **machine.json one careless commit from leaking live secrets (High, HIGH).** `data/machine.json` holds a working LLM key + identity secret. It was never committed (verified: `git log --all -- data/machine.json` empty) — but `.gitignore` has no `machine.json`/`data/` rule (`git check-ignore` silent for it; `git status` shows `?? data/`). `*.db*` covers the databases, NOT the JSON. Any `git add -A` publishes both secrets.
- **No auth on the API (Medium, HIGH — by design).** Anyone on the machine (or LAN, if bound outward) can read memories, approve requests, unpair peers, rotate keys. Acceptable under the stated localhost-only assumption (`uvicorn 127.0.0.1:8001` — verified listening address); becomes Critical the moment the API binds `0.0.0.0` (the Docker image does exactly that — mitigated only by Docker's port mapping).
- **Vulnerable dependencies (Medium, HIGH).** `cryptography==46.0.1` pinned to a version with multiple PYSEC advisories (fix ≥46.0.5, per `pip-audit` run this session); transitive `starlette 0.38.6` flagged (fix 0.40.0); Next.js 14.2.35 carries critical DoS/RSC CVEs (fix = breaking major bump, per `npm audit`). Localhost binding keeps exploitability low — this is hygiene debt, not an open door.
- **Positives (verified):** SSRF guard with DNS-pin + private/loopback blocks (`search/fetch.py:60-96`, `follow_redirects=False:149`); prompt-injection quarantine (`wrap_retrieved`, `provider.py:138-140`); no secrets in tracked files (`git grep` for key patterns clean); no XSS sinks (`dangerouslySetInnerHTML`/eval absent); invite/approval expiry enforced server-side; Ed25519 sealed identity with self-verification.

### H. Reliability and performance
- **Relay is the single point of demo failure (High, HIGH).** Sleep + 3s connect/5s ack timeouts + no wake/retry UX (see §5A). Background delivery + Retry buttons are the right mitigations and both work.
- **Unbounded reads are small-scale-safe (Low, HIGH).** `list_all()` has no cap but lists are capped at call sites (`/memory` limit ≤50, threads 50/200, audit 500). Fine for a laptop; note before any shared use.
- **Extraction runs on daemon threads (Low, HIGH).** `extract.py:66-78` — a restart mid-extraction silently drops facts. Acceptable; unobservable by design (see logging).
- **`/health` is shallow (Low, HIGH).** Returns `{"status":"ok"}` without checking DB/relay. Fine for Docker, useless for diagnosis.

### I. Tests, docs, DX
- **Tests: strong (verified).** `210 passed` in 100s (my run, this session). Tests hit real ASGI/HTTP/SQLite with scripted provider chunks — e.g. `test_stream.py` rendezvous-proves first-token streaming; `test_a2a_loop.py` runs mutual pairing end-to-end. Relay tests need Postgres `:5433` and skip-or-pass gracefully (observed green in 1.44s–28s).
- **Docs: weak.** README 16 lines, no setup/run/troubleshoot; backend dev loop (ports, env vars, two-instance recipe) undocumented; FRIEND_SETUP.md is the one good doc.
- **DX gaps:** no lint/format configs, no `.env.example`, no frontend tests, backend run command lives only in `scripts/docker-start.sh`, uncommitted working tree means CI has never validated the current code.

## 6. Biggest mistakes

1. **#1 — Demo depends on a narcoleptic relay with no wake-up plan (DECISION mistake).** Evidence: two `RELAY_UNREACHABLE` failures observed in one session; `CONNECT_TIMEOUT = 3.0` vs ~60s Render cold start; invite creation itself requires the relay, so even *pairing* fails on first try. It matters because the README's "locked v1 demo" fails convincingly on a cold start — the exact moment credibility counts. Cost: failed demos, support load ("is it broken?"), Retry-button fatigue. Fix with wake-on-startup ping, longer first-try timeouts, or a relay with no sleep.
2. **#2 — A full milestone sits uncommitted and CI-blind (EXECUTION mistake).** Evidence: `git status` shows 17 modified + 9 untracked paths (LangGraph core, threads, provider config, UI remodel, 4 test files) with last commit Oct 2. Cost: loss risk, zero CI signal on the current code, reviewers auditing stale trees.
3. **#3 — Live secrets one `git add -A` from publication (EXECUTION mistake).** Evidence: `data/machine.json` contains working `llm_key` + `identity_secret`; `.gitignore` lacks any rule covering it; `git status` shows `?? data/`. Cost: one habitual command leaks an API key (billable) and the identity root (trust-destroying). Fix is one line.

## 7. What is genuinely good
- **Security primitives are real, not theater:** DNS-pinned SSRF guard (`fetch.py:60-96`), replay protection via UNIQUE message_id (verified blocking a real loopback), expiry enforced with 410s, quarantine delimiters on untrusted web content, sealed self-verifying identity.
- **Failure semantics are designed, not accidental:** every send is `queued → relayed/delivery_failed/local-only` with observable status and retry; background delivery never blocks requests; recall/extraction provably cannot break a turn (1s bound + fire-and-forget, both tested).
- **Tests assert behavior, not coverage:** rendezvous streaming proof, expiry tests, mutual-pairing loop, HTTP-contract tests with scripted providers. 210 green is earned.
- **Zero-secret shipping works:** `.dockerignore` excludes `data/`, `.env`, `*.db*` (verified by inspecting the built image — no secrets inside), identity self-generates, key pastes via verified UI flow.

## 8. Prioritized fix plan

- **P0: Fix now**
  1. Add `machine.json` (and `data/`) to `.gitignore`; rotate the exposed LLM key. Why: live secret one command from public. Effort S, impact High. Accept: `git status --porcelain` shows no secret-bearing paths; `git check-ignore data/machine.json` hits a rule.
  2. Commit the working tree (or back it up off-tree). Why: a milestone with zero VCS/CI coverage. Effort S, impact High. Accept: `git status` clean on intended paths; CI green on the push.
  3. Bump `cryptography` to ≥46.0.5 and pin remaining backend deps (or add `pip-compile`). Why: pinned-vulnerable crypto + unpinned everything. Effort S, impact Med. Accept: `pip-audit` clean for project deps; fresh-venv `pip install -r requirements.txt && pytest -q` green.
  4. Relay wake/retry: ping `/health` at backend startup + raise first-try timeouts + surface "relay asleep, retrying" in UI. Why: cold-start demo failure observed twice. Effort M, impact High. Accept: cold relay → invite succeeds within ~90s without manual health-pinging.

- **P1: Fix soon**
  5. Write the missing README: setup, run (ports/env), two-instance recipe, troubleshoot (relay sleep, NO_IDENTITY, MISSING_KEY). Effort M, impact High. Accept: a new dev reaches paired chat following only the README.
  6. Add application logging (stdlib `logging`, one module-level logger per package). Effort M, impact Med. Accept: a failed delivery/extraction leaves a trace with message_id + code.
  7. Decide auth/threat model explicitly (localhost-only documented + bind check at startup). Effort S, impact Med. Accept: startup refuses or warns on non-loopback bind without an auth flag.
  8. Fix `text-accent` small-text contrast (darken or restrict to large/bold). Effort S, impact Med. Accept: measured ≥4.5:1 on body/link text.
  9. Update stale `test_cors.py` matrix to `:3000`/`:3001`. Effort S, impact Low. Accept: covers current `allow_origins`.

- **P2: Improve**
  10. Split `chat/page.tsx` (orchestrator + components) and extract one shared frontend `api()` client. Effort L, impact Med. Accept: no duplicated fetch helpers; page file <400 lines.
  11. Hoist LangGraph compile + reuse checkpoint saver across streams. Effort S, impact Low. Accept: one compile per process; same test suite green.
  12. Add ruff + eslint/prettier configs and run in CI. Effort S, impact Low. Accept: `pre-commit`/CI gate green.
  13. First frontend tests (history sidebar + key form with mocked fetch). Effort M, impact Med. Accept: 5+ component tests green in CI.
  14. Evaluate Next.js 15/16 upgrade path for the critical CVEs (localhost-bound → schedule, don't rush). Effort L, impact Low. Accept: `npm audit` criticals cleared or risk-accepted in writing.

- **P3: Nice to have**
  15. LLM-based memory extraction behind the heuristic (interface already supports it). 16. Deeper `/health` (DB + relay reachability). 17. `.env.example`.

## 9. Recommended next 3 actions
1. Add the gitignore rule and rotate the LLM key — five minutes, removes the only secret-exposure risk found.
2. Commit everything and watch CI — turns today's verified-good tree into protected, reproducible state.
3. Make cold-start relay failure impossible-or-invisible (startup wake ping + friendlier retry) — protects the demo that justifies the project.
