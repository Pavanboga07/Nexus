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
- **Your own identity secret** — you will generate this below. It is the
  master secret of YOUR agent. Never send it to anyone, including me.

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

## 3. Create YOUR identity secret (yours alone — generate, never copy mine)

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the output. If you don't have Python, use any password generator
with 40+ random characters — but Python one-liner above is easiest
(Windows ships it? if not, install Python 3.12 from python.org first).

## 4. Start it (replace the two placeholders, keep the quotes)

```powershell
docker run -d --name nexus -p 8001:8001 -p 3001:3001 -v nexus-data:/data `
  -e NEXUS_IDENTITY_KEY="<paste YOUR generated secret>" `
  -e NEXUS_LLM_API_KEY="<paste YOUR Gemini key>" `
  -e NEXUS_LLM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/" `
  -e NEXUS_LLM_MODEL="gemini-3.6-flash" `
  -e NEXUS_RELAY_URL="wss://nexus-gateway-mv63.onrender.com" `
  pavanboga07/nexus-v1:latest
```

(Backticks mean "continues on next line" — or paste it all as one line
without backticks.)

Check it's up:

```powershell
docker logs nexus
```

You should see `Uvicorn running` and no Traceback. Then open
**http://localhost:3001/chat** — say hi to your agent.

## 5. Pair with me

1. I create an invite and READ YOU the code over a call (never chat/email).
2. People page → enter it within 15 minutes.
3. Compare the fingerprint on your screen with mine, character by character.
4. Both approve → we're connected.

## 6. Daily use

- Open http://localhost:3001/chat. Start Docker first if it was closed.
- Your data lives in the `nexus-data` volume — `docker rm nexus` never
  deletes it. To erase everything (new identity!): `docker volume rm nexus-data`.
- To update to a newer version I send: rebuild, stop old, start new with
  the SAME `-v nexus-data:/data` and SAME secrets — identity and memory survive.
- Logs when something breaks: `docker logs nexus` — send me the last 20 lines.

## Rules (short version)

1. Your `NEXUS_IDENTITY_KEY` is yours alone. Never share it.
2. Your model key is yours alone. Get your own free one.
3. You need nothing else: no database, no accounts, no code tools.
