# Cutplanner — Streamlit + Supabase + browser report archive

**Run locally**

```bash
pip install -r requirements.txt
streamlit run app.py
```

Set `[auth]` secrets for the shared login or the corresponding `CUTPLANNER_USERNAME`, `CUTPLANNER_PASSWORD`, `CUTPLANNER_COOKIE_SECRET` environment variables.

See **SETUP_SUPABASE.md** for cloud deployment and limitations. The existing Python cutting optimizer is preserved. Cloud PostgreSQL stores patterns and complete exported reports; IndexedDB stores completed PDF/Excel reports in the current browser.
