# cutplanner — automatic guillotine cutting layouts

## Quick start (Windows)
1. Install Python 3.10+ from python.org (tick "Add Python to PATH").
2. In the program folder: `pip install -r requirements.txt`
3. Double-click `start_web.bat` (or run `python -m streamlit run app.py`). The page opens at http://localhost:8501.

macOS / Linux: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && python -m streamlit run app.py`

Everything runs locally and for free.

## Web interface
Upload an Excel/CSV order file, edit the order table, press "Calculate layout", then review the patterns with drawings and
cutting order, and download the PDF/Excel files. Parameters are in the left panel. The "Size finder" tab suggests a size
close to the desired one with less waste; the "Pattern library" tab lists saved patterns.

Choose the unit of album sizes (mm, cm or inches) above the order table. The unit applies to the Width and Length columns,
both in the table and in an uploaded file. Changing the unit does not convert the numbers, it changes how they are read.

Order file format (Excel .xlsx or CSV): three columns — **Width, Length, Quantity (albums)**. The header row is optional;
without it the first three columns are used in this order. With a header, the columns are found by name (Width / Length / Quantity)
and extra columns are ignored. In CSV, use `;` as the separator if you write decimal commas (12,5).

## Command line
    python cutplanner.py plan order.xlsx --unit cm     # unit of the sizes in the file: mm, cm or in (default in)
    python cutplanner.py plan order.xlsx --library-only
    python cutplanner.py library
    python cutplanner.py suggest --sheet 61x86 --size 16x16 --unit cm --tol 10

The result is a folder `<file>_plan` with `layout.pdf` (pattern drawings and cutting order) and `layout.xlsx` (summary and cuts).

## Pattern library
Every pattern found is saved in `patterns.db` (SQLite). Later calculations take patterns from the library first and create
new ones only if that is clearly better (penalty `--new-penalty`, default 0.5 packs per new pattern).
The library is shared by the web interface and the command line.

## Parameters
| parameter | default | meaning |
|---|---|---|
| `--sheet 780x1090` | 780x1090 | sheet, mm (width x length) |
| `--knife 780` | 780 | max cut length, mm |
| `--trim 0` | 0 | edge trim on each side, mm |
| `--pack 15` | 15 | sheets per pack (multiple) |
| `--max-patterns 8` | 8 | max number of different patterns per order |
| `--max-waste 10` | 10 | waste threshold per pattern, %; patterns above it are avoided if possible, otherwise flagged ⚠ |
| `--new-penalty 0.5` | 0.5 | higher = more willing to use library patterns |

Sizes are rounded up to whole mm. Patterns are guillotine patterns: the first cut goes across the sheet width (≤ knife),
then strips → columns → pieces. The geometry of every pattern is verified.

## Flexible size finder
    python cutplanner.py suggest --sheet 61x86 --size 16x16 --unit cm --tol 10
Searches sizes near the desired one (0.1 cm step) for minimal waste. `--free-aspect` changes width and height independently,
`--weight` sets how important closeness to the desired size is. The same is available on the "Size finder" tab.

## Filling leftovers
If free space remains on a sheet after the main pieces, it can be filled with other formats: `--fillers A3,A4,A5,A6`
or custom ones: `--fillers "A5,mine=100x150"` (mm). The program finds all free rectangles separated by through-cuts and
fills them as densely as possible. The result is by-product output (quantities are in the totals); it is drawn hatched on the pattern.
In the web interface, use the field in the left panel.

## Free hosting
See the notes in the chat: the app is a Streamlit app, so it can be hosted on Streamlit Community Cloud (connect a GitHub
repository, main file `app.py`). Note that `patterns.db` is stored on the server's disk, which is not permanent on free hosts.


## Shared password protection (Streamlit)
The web app displays a login screen before any planner controls, calculations or database reads.
There is one shared username and password, not individual user accounts.

**Local development:** Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`,
set a strong username/password, and then start Streamlit. The real `secrets.toml` is gitignored.
Alternatively set environment variables `CUTPLANNER_USERNAME` and `CUTPLANNER_PASSWORD`.

**Streamlit Community Cloud:** Open the deployed app's **Settings → Secrets** and enter:

```toml
[auth]
username = "your-team-login"
password = "your-unique-strong-password"
cookie_secret = "another-long-random-secret-for-cookie-signing"
```

Never upload real credentials to GitHub or share them in chat. Without credentials the
app refuses access. Users can log out with the sidebar **Log out** button.

**Limitations:** This is a shared password gate, not per-user Supabase Auth; it has no
per-user audit trail, roles, or account recovery. Use a long random password and share
it only with trusted staff. Network-level brute-force protections and more advanced
access controls are not included. A signed browser cookie keeps the shared login active for up to 12 hours (including browser refresh and 30+ minutes idle). The cookie does not contain credentials. Cookies are created through a community Streamlit component and are not HttpOnly; for stronger security use provider-based authentication later. Logout clears the browser cookie. Closing/reopening the browser before expiry usually preserves access. Changing the shared password or cookie_secret invalidates previous cookies.

**Database note:** This release adds authentication ONLY. It continues to store the
pattern library in the local SQLite `patterns.db`. For persistent cloud storage, migrate
that library to Supabase PostgreSQL before relying on it for production data.
