# Vera bot — magicpin AI Challenge submission

## What this is
`bot.py` is a working server with all 5 required endpoints:
`POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `GET /v1/healthz`, `GET /v1/metadata`.

The message engine (`compose()`) is **deterministic** — no LLM call, no network
dependency, same input always gives the same output. It builds each message from a
small library of template functions, one per `trigger.kind` (research_digest,
recall_due, perf_dip, perf_spike, renewal_due, festival_upcoming, etc.), each of
which pulls real numbers/names/dates out of the category, merchant, trigger, and
optional customer context it was given — never invents a fact.

## Run it locally first
```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```
Then in another terminal:
```bash
curl http://localhost:8080/v1/healthz
```
You should see `{"status":"ok", ...}`.

## Get a public URL (easiest path — Render, free tier)
1. Go to https://render.com and sign up (free).
2. Click **New +** → **Web Service**.
3. Connect a GitHub repo containing `bot.py` and `requirements.txt`
   (push this folder to a new GitHub repo first — Render deploys from GitHub).
4. Set:
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
5. Click **Create Web Service**. Render gives you a URL like
   `https://your-bot-name.onrender.com` — that is your **Submission URL**.
6. Test it: `curl https://your-bot-name.onrender.com/v1/healthz`

Note: Render's free tier sleeps after ~15 min idle and takes ~30-60s to wake on
the next request — that first wake-up could eat into the judge's 30s timeout
budget. If you can, use a paid/always-on tier, or Railway/Fly.io instead, for
the actual evaluation window.

## What to put in the submission form
- **Submission URL**: the base URL only (e.g. `https://your-bot-name.onrender.com`),
  not a specific endpoint — the judge appends `/v1/...` itself.
- Keep the service running (don't stop/redeploy) until you've received your score,
  since all context is stored in memory and is lost on restart.

## Design notes (for the README the challenge asks for)
- **Deterministic-first**: every fact in the message comes straight from the
  context dicts; nothing is generated freeform, so identical inputs always
  produce identical outputs (a hard requirement in the brief).
- **Suppression**: `/v1/tick` skips any trigger whose `suppression_key` has
  already fired, so the same nudge for the same reason isn't repeated.
- **Category voice**: salutation and code-mix (Hindi-English) are derived from
  `merchant.identity.languages` and `category.slug` (e.g. dentists get "Dr. X").
- **Reply handling**: `/v1/reply` does lightweight intent detection — repeated
  verbatim messages or known canned phrases → `auto_reply` (bot exits
  gracefully); accept/decline keywords; a question mark → `question`; anything
  else → `unclear`, with a 3-strikes graceful exit.
- **What would help most**: real embeddings/retrieval over `digest` items for
  categories with many entries, and a proper conversation-state store (Redis)
  instead of in-memory dict, for a longer-running production version.
