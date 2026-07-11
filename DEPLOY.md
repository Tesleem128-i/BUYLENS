# Deploying BuyLens to Render

## What changed in `app.py`
- `DATABASE_URL` now auto-converts `postgres://` → `postgresql://` (Render gives you the former, SQLAlchemy needs the latter).
- Added `pool_pre_ping` so the app reconnects cleanly if Render's Postgres drops an idle connection.
- The dev server now reads `PORT` from the environment and only runs with `debug=True` if `FLASK_DEBUG=true` is explicitly set (never turn that on in production — Flask's debugger allows remote code execution if `SECRET_KEY`/debug mode is exposed).

New files added: `requirements.txt`, `Procfile`, `render.yaml`, `.gitignore`, `.env.example`.

## 1. Rotate your database password first
You pasted your live Postgres connection string in this chat. Treat it as burned:
1. Render dashboard → your Postgres instance → **Info** tab → **Reset Password** (or recreate the database if you'd rather start clean).
2. Use the *new* connection string everywhere below, not the one from this conversation.

The same goes for the API keys in your uploaded `_env` file (Brevo, Gemini, Groq, Google Search, Pexels) — they were readable in that upload, so if this repo/chat is ever shared, rotate those too. It costs nothing and takes a minute per key.

## 2. Push to a Git repo
Don't commit `.env` or a real `pic/` folder of user uploads — both are already in `.gitignore`. Commit `.env.example` instead.

## 3. Create the Web Service on Render
**Option A — Blueprint (fastest):** commit `render.yaml`, then in Render click **New → Blueprint** and point it at your repo. It will create the web service and prompt you for the `sync: false` variables (DATABASE_URL, API keys).

**Option B — Manual:**
1. **New → Web Service**, connect your repo.
2. Runtime: Python 3.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --workers 2 --threads 4 --timeout 120`
5. Add environment variables (Settings → Environment):
   - `DATABASE_URL` = your **new** Postgres URL (use the "Internal Database URL" from Render's Postgres dashboard if the web service is in the same Render region — it's faster and doesn't leave Render's network)
   - `SECRET_KEY` = a long random string (Render can auto-generate this for you)
   - `FLASK_DEBUG` = `false`
   - `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`
   - `GEMINI_API_KEY`, `GROQ_API_KEY`
   - `GOOGLE_SEARCH_API_KEY`, `GOOGLE_SEARCH_CX`
   - `PEXELS_API_KEY`

## 4. Persistent disk for profile pictures
Render's filesystem is **ephemeral** — anything written to `pic/` (profile picture uploads) disappears on every deploy or restart. `render.yaml` already requests a 1GB persistent disk mounted at the upload folder. If you set the service up manually instead, add a Disk in the service's **Disks** tab with the mount path matching `app.config["UPLOAD_FOLDER"]` (currently `<project_root>/pic`). Skipping this means users' profile pictures vanish whenever you redeploy.

## 5. First deploy
`db.create_all()` runs automatically on app startup (see bottom of `app.py`), so your tables get created against the new Postgres database the first time it boots — no separate migration step needed for this schema. Watch the deploy logs for the "Add demo account if it doesn't exist" seed step to confirm it connected.

## 6. Sanity checklist after deploy
- Visit the Render URL → sign up / log in works (confirms DB connection + Brevo email).
- Try one AI feature (e.g. compatibility checker) → confirms `GROQ_API_KEY`.
- Upload a profile picture, redeploy, check it's still there → confirms the disk mount is correct.
