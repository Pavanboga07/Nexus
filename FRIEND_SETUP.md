# Friend Setup — run Nexus on your laptop (Windows)

Time needed: ~15 minutes (mostly downloads). You need NO coding tools —
only Docker Desktop. Everything of yours stays on your machine except
signed messages relayed through our shared relay.

## 0. What you need first

- **Docker Desktop for Windows** — download from docker.com, install with
  defaults, start it, wait until it says running.
- **A free Gemini API key** (yours alone, never shared):
  1. Go to https://aistudio.google.com/apikey
  2. Sign in with your Google account → **Create API key** → copy it.
  3. Keep it handy — you paste it inside the app in step 3, not in
     the terminal.

Your agent's identity secret is created automatically on first start and
stays in your private data volume. There is nothing to generate, copy,
or paste for it.

## 1. Get the app (no source code, no builds)

You need **Docker Desktop for Windows** only (docker.com → download,
install with defaults, start it, wait until it says running).

Pull the ready-made image (about 2 GB, one time):

```powershell
docker pull pavanboga07/nexus-v1:latest
```

(Fallback if pull fails: unzip the `nexus-v1-friend.zip` I sent separately,
then `docker build -f Dockerfile.friend -t nexus-v1-friend .` — same result,
~5 minutes.)

## 2. Start it (no secrets, no setup)

```powershell
docker run -d --name nexus -p 8001:8001 -p 3001:3001 -v nexus-data:/data `
  pavanboga07/nexus-v1:latest
```

That is the whole command — no `-e` flags. Your identity is created
inside `nexus-data` on first start.

Check it's up:

```powershell
docker logs nexus
```

You should see `Uvicorn running` and no Traceback.

## 3. Unlock the app with your operator token

1. Open **http://localhost:3001/chat**. A yellow banner asks for an
   operator token (the app's API refuses to work without it).
2. Get the token (created on first start):
   ```powershell
   docker exec nexus python -c "from app.machine_config import get_or_create_operator_token; print(get_or_create_operator_token()[0])"
   ```
3. Paste it into the banner → **Unlock**. It stays in your browser.
   If you ever rotate it, repeat this step with the new value.

## 4. Paste your model key in the app

1. At the top, in **Model key**, pick **Google Gemini**, paste your
   Gemini key → **Save key**.
3. The key is checked live before it is saved: a bad key is rejected
   with the provider's message and never stored. When it says
   **Configured**, say hi to your agent.
4. To change it later, paste a new key in the same box (**Rotate key**) —
   same check, same box.

Manual live check (proves the saved key works end to end): after
**Configured** appears, send a chat message and confirm you get an
answer with sources. If chat says the key is missing instead, re-paste
it and try again.

## 5. Pair with me

1. I create an invite and READ YOU the code over a call (never chat/email).
2. People page → enter it within 15 minutes.
3. Compare the fingerprint on your screen with mine, character by character.
4. Both approve → we're connected.

## 6. Daily use

- Open http://localhost:3001/chat. Start Docker first if it was closed.
- Your data (identity + memories + model key) lives in the `nexus-data`
  volume — `docker rm nexus` never deletes it. To erase everything (new
  identity!): `docker volume rm nexus-data`.
- To update to a newer version I send: rebuild, stop old, start new with
  the SAME `-v nexus-data:/data` — identity, memory, and key survive.
  Nothing to re-enter.
- Logs when something breaks: `docker logs nexus` — send me the last 20 lines.

## Optional overrides (advanced — skip unless I ask)

The image already points at the shared relay and the Gemini-compatible
provider. If I ever ask you to aim elsewhere, add `-e` flags to the
`docker run` command above, e.g. `-e NEXUS_LLM_MODEL="other-model"`.
Explicit `-e` values always win over the built-ins (and over the key
saved in the app, for the model key). You never need these for normal use.

## Rules (short version)

1. Your identity secret is yours alone. It lives inside your `nexus-data`
   volume — never copy it out, never share it, not even with me.
2. Your model key is yours alone. Get your own free one, and paste it
   only into YOUR app's Model key box.
3. You need nothing else: no database, no accounts, no code tools.
