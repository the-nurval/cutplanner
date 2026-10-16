# Cutplanner: Supabase connection

## 1. Create a Supabase project

Use the Supabase Dashboard, create a free project in a region near users.
In **Connect** select **Session pooler** (PostgreSQL) connection string (use the URI format). Ensure password and hostname exactly match your project. Depending on the provider's panel, **Transaction pooler** can also work, but Session pooler is simpler for this connection pattern.

## 2. Streamlit Community Cloud → app → Settings → Secrets

Keep your existing login details and add the database section:

```toml
[auth]
username = "cutplanner"
password = "YOUR_EXISTING_SHARED_PASSWORD"
cookie_secret = "YOUR_EXISTING_COOKIE_SECRET"

[database]
url = "postgresql://postgres.PROJECT_REFERENCE:YOUR_DB_PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require"
```

The URL above is **illustrative**, not a working address. Copy your project's exact connection string from **Connect**. Passwords containing special characters must be URL encoded. Never commit secrets to GitHub. Avoid sending any connection passwords through chat.

The app automatically creates three tables in the `public` schema on its first connection:
- `patterns`: reusable optimized patterns
- `jobs`: legacy calculation history
- `saved_results`: complete PDF/Excel snapshot per run (base64 JSON)

Backend PostgreSQL credentials remain on the **Python server**. Do **not** enable public RLS policies for these tables or expose the database password, `service_role` key, or a direct REST endpoint to browser JavaScript.

**Security caveat:** The existing URL bearer-token login is a temporary arrangement, not suitable for confidential production data. Authenticated URLs must not be shared. Before broader rollout, replace with a server-managed, HttpOnly session or proper identity provider. Also introduce app-side rate limiting on the shared password login.

## 3. Deploy

Upload this package's files into your existing GitHub repo, including `storage.py`, `cloud_results.py` and the `browser_vault/` folder. Deploy using `app.py`. Python requirements now include `psycopg[binary]`.

## 4. Offline/browser behavior

- After a completed **online** calculation, the PDF and Excel are stored in the current browser's IndexedDB; downloads work without requesting a fresh report from Python while the component remains loaded.
- A **failed cloud write** leaves a pending local record; when the component next communicates with Streamlit, it attempts to send that record back for retry. The server persists it to Supabase; repeated saves use the same UUID and cannot create duplicates.
- Supabase stores full report snapshots; reports can be downloaded from the `Pattern library` tab on another logged-in device.
- The existing Python optimizer remains entirely server-side. No new computations offline.

## NOT YET IMPLEMENTED (do not assume these work)

1. **True offline page reload/PWA:** the main Streamlit page is not service-worker cached. F5 while offline cannot load the app, even though IndexedDB has the results. Browser data survives and will reappear on reconnect on the same site/browser profile.
2. **Realtime/WebSocket cross-user updates:** remote archive is read when the page reruns; there is no live Postgres Changes subscription or sync conflict UI yet.
3. **Fully nonblocking cloud writes:** writes initiated by the server still run in Streamlit execution. Browser-queued retry mitigates outages but is not a separate always-on background worker.
4. **Pattern cache in browser:** the browser archive stores ready PDF/Excel files; the optimizer currently reads pattern library from Supabase over the network.
5. Browser storage is device and browser-profile specific and may be cleared by the user/browser. Use the cloud copy as the long-term source of truth.

### Next architectural work

Build a separate, tiny browser PWA/offline archive with a service worker, secured API sync and an authenticated Supabase Realtime feed. That is necessary to meet all the requested offline-first guarantees; a Streamlit-only build cannot provide them just by connecting IndexedDB. Versioned change envelopes, durable sync checkpoints, error backoff, and explicit conflict resolution must precede allowing multiple devices to edit the same saved report.

## Local development

With no `[database]` URL, the app uses SQLite (`patterns.db`) as before. To test PostgreSQL locally set `CUTPLANNER_DATABASE_URL` in your local environment. Don't check this secret into GitHub.
