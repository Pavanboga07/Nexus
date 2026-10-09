# Two-Laptop Gate — Runbook (V8)

The only gate that matters for v1. Two machines, two owners, one completed
delegated task with approvals on both ends. Everything else in this repo
exists to make this script pass.

## Prerequisites (operator)

- [ ] Relay deployed (see below), URL noted: `______________________`
- [ ] Laptop A: repo checkout, Python 3.12, Node 20, model key ready
- [ ] Laptop B: same, DIFFERENT model key (or same provider, different key),
      DIFFERENT database (each laptop uses its own local SQLite — nothing shared)
- [ ] Both laptops: online, clocks correct (TLS + token expiry depend on it)

## 0. Deploy the relay (operator, once)

1. Create a hosted Postgres (Neon free tier): note `RELAY_DATABASE_URL`: `______`
2. Deploy `relay/` to Render (free tier OK for 2 users): set `RELAY_DATABASE_URL`,
   note public URL: `______`
3. `GET <url>/readyz` → 200. `GET <url>/metrics` → counters present.
4. Record both URLs above. Old relay (if any) stays untouched as fallback.

## 1. First boot (each laptop, ~5 min)

1. `pip install -r requirements.txt`, `npm --prefix frontend install`
2. Start backend (port 8001), start frontend dev (port 3001). No `.env` needed
   yet — app runs unconfigured. (Ports default to 8001/3001 via
   `scripts/docker-start.sh`; `PORT_API`/`PORT_UI` override them.)
   Pre-flight: `curl localhost:8001/health` → `{"status":"ok"}`.
3. Open `http://localhost:3001/chat`. Send any message → expect the
   missing-key error (proves the path works before keys exist).
4. Set model key (see README), display name. Confirm fingerprint on the
   Agent page. RECORD both fingerprints:
   - A: `______________________`
   - B: `______________________` (MUST differ — identical means shared state, STOP)

## 2. Pairing (~3 min)

1. A: People → Pair → show code. B: enter code within 15 min.
2. B sees A's name + fingerprint → compare against A's recorded value above.
   Mismatch → STOP (possible relay tampering), record and abort.
3. B approves → A sees incoming request with B's fingerprint → compare → approve.
4. Both sides list each other as contacts. RECORD time: `______`

## 3. The ask (~5 min)

1. A opens chat, picks B as peer, asks a question needing a web answer
   (e.g. "latest stable Python release?"), sends.
2. EXPECT: A's request arrives at B as an approval card showing who/what/why.
3. B approves → B's agent answers (may search) → answer returns to A cited.
4. EXPECT: A sees the cited answer; both audit trails show the exchange.
5. RECORD: approvals seen both ends (yes/no), citations present (yes/no),
   wall time ask→answer: `______`

## 4. Adversarial checks (~5 min, optional but recommended)

- B goes offline (quit app) → A asks → message queued → B restarts →
  delivery arrives (check relay `/metrics` queue_depth returns to 0).
- A revokes B (People → unpair) → B's next ask fails closed with a clear error.
- Wrong code 5× → cooldown; expired code → regenerate path.

## 5. Sign-off

- [ ] All EXPECT lines above hold. Transcript (times + fingerprints) pasted below.
- [ ] Any failure → file as follow-up task with logs, do NOT proceed to packaging.

## Transcript

```
(paste here)
```

## Known live-watch items (from reviews — verify, do not fix mid-run)

- `tool_choice:"auto"` always sent with schemas — some gateways reject
  explicit auto. If a provider 400s on it, record the exact error.
- Multi-blob `extra_content` merge is shallow — single-blob path is safe.
- V2 socket-test flake under combined runs (timing, pre-existing).
