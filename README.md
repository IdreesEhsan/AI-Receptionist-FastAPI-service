# AI Receptionist — FastAPI Webhook

Deploys straight to Render or Railway. No local server needed — verify everything
against the live URL after each deploy using the curl commands below.

## Files
- `main.py` — the three Custom Function endpoints Retell calls, plus a `/` health check
- `db.py` — Supabase client
- `requirements.txt` — dependencies (minimum versions, not exact pins — verify against
  current PyPI if a build fails)
- `render.yaml` — Render infra-as-code config
- `Procfile` — for Railway (or any platform that reads Procfiles)
- `.env.example` — copy to `.env` locally if you ever do test locally; never commit `.env`

## Deploy to Render

1. Push this `webhook/` folder to a GitHub repo.
2. In the Render dashboard: **New > Blueprint**, connect the repo. Render will read
   `render.yaml` automatically and set up the service.
   - Alternative: **New > Web Service**, connect the repo, set:
     - Build command: `pip install -r requirements.txt`
     - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. In the service's **Environment** tab, add the three real secrets:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY` (the service_role key, not the anon key)
   - `RETELL_API_KEY`
4. Deploy. Watch the build logs — first deploy takes a few minutes.
5. Note your public URL, e.g. `https://ai-receptionist-webhook.onrender.com`.

## Deploy to Railway (alternative)

1. Push this folder to GitHub.
2. Railway dashboard: **New Project > Deploy from GitHub repo**.
3. Railway auto-detects Python via `Procfile`. If it doesn't, manually set the start
   command to `uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. Add the same three environment variables under the service's **Variables** tab.
5. Deploy and note the generated public URL (Railway calls this a "domain" — you may
   need to generate one under **Settings > Networking** if it's not auto-created).

## Verify the deploy — do this every time, right after deploying

Since there's no local testing step, this is your only feedback loop. Run these in
order and don't move on until each one passes.

### 1. Health check
```bash
curl https://YOUR-DEPLOYED-URL/
```
Expect: `{"status":"ok"}`. If this fails, the deploy itself is broken — check the
platform's logs before touching anything else.

### 2. Check availability (expect a 401 — this confirms signature verification is working)
```bash
curl -X POST https://YOUR-DEPLOYED-URL/check-availability \
  -H "Content-Type: application/json" \
  -d '{"name":"check_availability","args":{"date":"2026-09-10"},"call":{"call_id":"test123"}}'
```
Expect: `{"message":"Unauthorized"}` with a 401 status. This is correct — the request
has no valid `X-Retell-Signature` header, since it didn't come from Retell. A 401 here
means your signature check is working, not that something is broken. You can only get
a real signed request from Retell itself (or the SDK's `verify` function with your
actual key), so full end-to-end testing of these endpoints happens once the Retell
agent is wired up in Phase 4 — that's expected, not a gap in your testing.

### 3. Check the platform's live logs while making that curl request
Render: **Logs** tab on the service. Railway: **Deployments > View Logs**. You should
see the request hit `/check-availability` and the 401 get returned. This confirms
requests are actually reaching your code — if you see nothing in the logs at all, the
URL or routing is wrong, not the auth logic.

### 4. Once the Retell Custom Functions are configured (Phase 4)
Trigger a real test call and watch the same logs. Retell's dashboard also shows you the
exact request/response for each Custom Function call per call — cross-check that
against your Render/Railway logs if anything looks off.

## Common failure modes when deploying blind (no local test)

- **Wrong start command** — if you see the build succeed but the service immediately
  crashes/restarts, check that the start command matches exactly:
  `uvicorn main:app --host 0.0.0.0 --port $PORT`. Forgetting `--host 0.0.0.0` is a
  classic mistake — the app binds to `127.0.0.1` and the platform's health check can
  never reach it, even though it "works."
- **Missing env vars** — `KeyError: 'SUPABASE_URL'` in the logs means an environment
  variable wasn't set in the platform dashboard. `.env` files are NOT read in
  production; you must set these in Render/Railway's dashboard directly.
- **Service_role key vs anon key mixed up** — using the anon key will cause Supabase
  writes (bookings, contacts) to silently fail or get blocked by row-level security.
  Always use the service_role key for this backend.
- **Retell can't reach the endpoint** — Retell blocks localhost/private IPs and needs
  a real public HTTPS URL. Render/Railway URLs satisfy this automatically, so this
  should only bite you if you paste the wrong URL into the Custom Function config.
