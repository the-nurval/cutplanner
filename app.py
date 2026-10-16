# -*- coding: utf-8 -*-
"""Web interface for cutplanner. Run:  python -m streamlit run app.py"""
import re, tempfile, hmac, os, uuid, base64, datetime
from auth_session import COOKIE_NAME, SESSION_HOURS, issue_token, valid_token
from argparse import Namespace
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

import cutplanner as cp
from cloud_results import save_result, recent_results, get_result
from storage import cloud_enabled
import streamlit.components.v1 as components

DB = str(Path(__file__).with_name('patterns.db'))
COL_W, COL_L, COL_QTY = 'Width', 'Length', 'Albums'
UNIT_OPTS = {'mm': 'mm', 'cm': 'cm', 'inches': 'in'}

st.set_page_config(page_title='Cutting layout', page_icon='✂️', layout='wide')

# Shared login: credentials are supplied via Streamlit secrets or local environment.
# Fail closed if credentials are missing. Never put passwords in the repository.
def _credential(name):
    value = os.environ.get(f'CUTPLANNER_{name.upper()}')
    if value is not None:
        return str(value)
    try:
        return str(st.secrets['auth'][name])
    except (FileNotFoundError, KeyError, AttributeError):
        return None


def require_login():
    username = _credential('username')
    password = _credential('password')
    cookie_secret = _credential('cookie_secret')
    if not username or not password or not cookie_secret:
        st.error('Login configuration missing: set [auth] username, password and cookie_secret in Streamlit Secrets.')
        st.stop()

    # Signed bearer token in the URL survives full browser reloads without
    # relying on JS cookie components or Streamlit WebSocket cookie forwarding.
    # IMPORTANT: users must not share a logged-in URL.
    token = st.query_params.get('session', '')
    authenticated = bool(token and valid_token(token, username, password, cookie_secret))
    if not authenticated:
        if token:
            # Expired/invalid token: prevent confusing repeated failures.
            del st.query_params['session']
        st.markdown("""<style>
          [data-testid="stSidebar"] {display:none;}
          .block-container {padding-top: clamp(3rem, 12vh, 7rem);}
        </style>""", unsafe_allow_html=True)
        left, center, right = st.columns([1, 1.05, 1], gap='large')
        with center:
            st.markdown('### ✂️ Cutplanner')
            st.caption('Sign in to access the cutting planner')
            with st.form('login_form'):
                input_user = st.text_input('Username', autocomplete='username')
                input_password = st.text_input('Password', type='password', autocomplete='current-password')
                submitted = st.form_submit_button('Sign in', type='primary', use_container_width=True)
            if submitted:
                user_ok = hmac.compare_digest(input_user.encode(), username.encode())
                pass_ok = hmac.compare_digest(input_password.encode(), password.encode())
                if user_ok and pass_ok:
                    st.query_params['session'] = issue_token(username, password, cookie_secret)
                    st.rerun()
                else:
                    st.error('Invalid username or password.')
        st.stop()

    with st.sidebar:
        if st.button('Log out', key='logout_button'):
            del st.query_params['session']
            st.session_state.clear()
            st.rerun()


require_login()
st.title('✂️ Paper cutting layout for the guillotine')
if not cloud_enabled():
    st.info('Cloud database not configured yet. Local SQLite is active; on Streamlit Cloud this data may be lost after restart. Set [database] url in Secrets for Supabase.')

ss = st.session_state
ss.setdefault('df', pd.DataFrame({COL_W: pd.Series(dtype=float), COL_L: pd.Series(dtype=float), COL_QTY: pd.Series(dtype=float)}))
ss.setdefault('ver', 0); ss.setdefault('last_file', None); ss.setdefault('R', None); ss.setdefault('snapshot', None); ss.setdefault('sync_error', None)

def set_df(rows):
    ss.df = pd.DataFrame({COL_W: [r[0] for r in rows], COL_L: [r[1] for r in rows], COL_QTY: [r[2] for r in rows]})
    ss.ver += 1; ss.R = None

def clean_orders(df, unit):
    rows = []
    for n, (_, row) in enumerate(df.iterrows(), 1):
        vals = [row.get(COL_W), row.get(COL_L), row.get(COL_QTY)]
        empty = [v is None or pd.isna(v) for v in vals]
        if all(empty): continue
        if any(empty): raise cp.PlanError(f'Row {n}: fill in width, length and albums.')
        w, l, q = (float(v) for v in vals)
        if q <= 0: continue
        if w <= 0 or l <= 0: raise cp.PlanError(f'Row {n}: width and length must be above 0.')
        rows.append((w, l, int(q)))
    return cp.orders_from_rows(rows, unit)

# ------------------------------------------------------------ parameters
with st.sidebar:
    st.header('Parameters')
    c1, c2 = st.columns(2)
    sheet_w = c1.number_input('Sheet: width, mm', 100, 3000, 780, 10)
    sheet_l = c2.number_input('Sheet: length, mm', 100, 3000, 1090, 10)
    knife = st.number_input('Max cut length (knife), mm', 100, 3000, 780, 10)
    trim = st.number_input('Edge trim on each side, mm', 0, 50, 0, 1)
    pack = st.number_input('Sheets per pack (multiple)', 1, 100, 15, 1)
    max_patterns = st.slider('Max number of different patterns', 1, 15, 8)
    new_penalty = st.slider('Prefer patterns from the library', 0.0, 3.0, 0.5, 0.1,
                            help='The higher the value, the more willingly the program uses saved patterns instead of new ones.')
    max_waste = st.slider('Waste threshold per pattern, %', 5, 100, 10, 1,
                          help='Patterns above the threshold are not used if they can be avoided; otherwise they are flagged ⚠. 100 = no limit.')
    fill_sel = st.multiselect('Fill sheet leftovers with formats', ['A3', 'A4', 'A5', 'A6'], default=[],
                              help='Leftovers are not filled by default. The selected formats will occupy free spots on the sheet (by-products).')
    fill_extra = st.text_input('Custom leftover formats, mm', placeholder='name=100x150, name2=90x130')
    lib_only = st.checkbox('Use only patterns from the library')
    time_limit = st.number_input('Solver time limit, sec', 10, 600, 60, 10)

tab_calc, tab_size, tab_lib = st.tabs(['Calculation', 'Size finder', 'Pattern library'])

# ------------------------------------------------------------ calculation
with tab_calc:
    st.subheader('1. Order')
    unit_label = st.radio('Unit of album sizes', list(UNIT_OPTS), index=2, horizontal=True, key='unit',
                          help='Applies to the Width and Length columns, both in the table below and in an uploaded file.')
    unit = UNIT_OPTS[unit_label]
    if ss.get('unit_prev') != unit_label:
        ss.unit_prev = unit_label; ss.R = None
    up = st.file_uploader('Upload an Excel (.xlsx) or CSV file with three columns: Width, Length, Quantity (albums). '
                          f'The header row is optional. Sizes are read in the selected unit ({unit_label}).',
                          type=['xlsx', 'xlsm', 'csv'])
    if up is not None and ss.last_file != (up.name, up.size):
        ss.last_file = (up.name, up.size)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / up.name; p.write_bytes(up.getvalue())
            try: set_df(cp.read_order_rows(p))
            except cp.PlanError as e: st.error(str(e))
    st.caption('You can edit the table and add rows. Changing the unit does not convert the numbers, it only changes how they are read.')
    edited = st.data_editor(ss.df, num_rows='dynamic', width='stretch', key=f'ed{ss.ver}',
                            column_config={COL_W: st.column_config.NumberColumn(f'Width, {unit_label}', min_value=0.0),
                                           COL_L: st.column_config.NumberColumn(f'Length, {unit_label}', min_value=0.0),
                                           COL_QTY: st.column_config.NumberColumn('Albums', min_value=0, step=1)})

    if st.button('Calculate layout', type='primary'):
        try:
            orders = clean_orders(edited, unit)
            a = Namespace(sheet=f'{sheet_w}x{sheet_l}', knife=knife, trim=trim, pack=pack, max_patterns=max_patterns,
                          new_penalty=new_penalty, pattern_penalty=0.15, library_only=lib_only, time_limit=time_limit, db=DB, max_waste=max_waste, unit=unit,
                          fillers=','.join(fill_sel + ([fill_extra] if fill_extra.strip() else [])))
            with st.spinner('Calculating layout...'):
                R = cp.compute(orders, a, lambda *_: None, 'web')
                with tempfile.TemporaryDirectory() as td:
                    cp.write_pdf(Path(td) / 'r.pdf', R['pl'], R['result'], orders, pack, R['W'] * R['Lg'], R['byproducts'])
                    cp.write_xlsx(Path(td) / 'r.xlsx', R['pl'], R['result'], orders, pack, R['byproducts'])
                    R['pdf'] = (Path(td) / 'r.pdf').read_bytes(); R['xlsx'] = (Path(td) / 'r.xlsx').read_bytes()
            R['pack'] = pack; R['unit_label'] = unit_label; ss.R = R
            # Unique identifier makes cloud writes safe to retry without duplicates.
            created = datetime.datetime.now(datetime.timezone.utc).isoformat()
            snapshot = dict(id=str(uuid.uuid4()), name='Cutting layout ' + created[:16].replace('T', ' '),
                            created=created, sheets=R['total_sheets'], patterns=len(R['result']),
                            waste=round(R['waste'], 2),
                            pdf=base64.b64encode(R['pdf']).decode('ascii'),
                            xlsx=base64.b64encode(R['xlsx']).decode('ascii'))
            ss.snapshot = snapshot
            try:
                save_result(DB, snapshot)
                ss.sync_error = None
            except Exception as exc:
                ss.sync_error = str(exc)

        except cp.PlanError as e:
            ss.R = None; st.error(str(e))

    if ss.get('sync_error'):
        st.warning('Result generated, but cloud persistence failed. A browser-local copy is available below when browser storage is enabled. Database error: ' + ss.sync_error)
        if st.button('Retry cloud save') and ss.get('snapshot'):
            try:
                save_result(DB, ss.snapshot)
                ss.sync_error = None
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    # A separate browser component stores finished reports in IndexedDB.
    # Unlike Streamlit session_state, these are retained across page refreshes.
    vault = components.declare_component('cutplanner_browser_vault', path=str(Path(__file__).with_name('browser_vault')))
    vault_event = vault(result=ss.snapshot, synced=not bool(ss.sync_error),
                        ack=ss.get('sync_ack'), key='cutplanner_browser_archive', default=None)
    if isinstance(vault_event, dict) and vault_event.get('pending'):
        candidate = vault_event['pending']
        if isinstance(candidate, dict) and candidate.get('id') != ss.get('sync_ack'):
            try:
                save_result(DB, candidate)
                ss.sync_ack = candidate['id']
                ss.sync_error = None
                st.rerun()
            except Exception as exc:
                ss.sync_error = str(exc)


    R = ss.R
    if R:
        pl, res, names = R['pl'], R['result'], R['names']
        st.subheader('2. Result')
        st.caption(f"Album sizes are in {R['unit_label']}; sizes are shown as shorter side × longer side.")
        m = st.columns(4)
        m[0].metric('Sheets of paper', R['total_sheets']); m[1].metric('Packs', R['total_packs'])
        m[2].metric('Patterns', len(res)); m[3].metric('Average waste', f"{R['waste']:.1f}%")
        rows = [{'Pattern': r['label'], 'Packs': r['packs'], 'Sheets': r['sheets'],
                 **{n: int(c) if c else None for n, c in zip(names, r['counts'])},
                 'Waste %': round(r['waste'], 1), 'Waste with leftovers %': round(r['waste_filled'], 1) if r['fill'] else None,
                 'Warning': '⚠ waste above threshold' if r['over'] else '', 'Source': 'new' if r['new'] else 'library'} for r in res]
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)
        st.caption('The numbers per size show how many pieces one sheet yields. A pack of %d sheets gives %d pieces of each size in the pattern = finished albums.' % (R['pack'], R['pack']))
        made = {n: sum(r['packs'] * r['counts'][i] for r in res) for i, n in enumerate(names)}
        st.dataframe(pd.DataFrame({'Size': names, 'Albums needed': [R['orders'][n] for n in names],
                                   'Will produce': [int(made[n]) for n in names],
                                   'Surplus': [int(made[n] - R['orders'][n]) for n in names]}), width='stretch', hide_index=True)
        if any(r['over'] for r in res):
            st.warning(f"Some patterns have waste above {R['max_waste']:g}%. Use them only if the price covers all the waste. "
                       'Try changing the size on the "Size finder" tab.')
        if R['byproducts']:
            st.info('By-products from leftovers: ' + ', '.join(f'{n} — {k} pcs' for n, k in R['byproducts'].items()))
        d1, d2 = st.columns(2)
        d1.download_button('⬇ PDF with patterns', R['pdf'], 'layout.pdf', 'application/pdf')
        d2.download_button('⬇ Excel summary', R['xlsx'], 'layout.xlsx',
                           'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        st.subheader('3. Cutting patterns')
        for r in res:
            with st.expander(f"Pattern {r['label']} — {r['packs']} packs ({r['sheets']} sheets), waste {r['waste']:.1f}%", expanded=False):
                cA, cB = st.columns([1, 1])
                fig = cp.draw_pattern(pl, r)
                cA.pyplot(fig, width='stretch'); plt.close(fig)
                cB.markdown('**Cutting order**'); cB.code('\n'.join(cp.instructions(pl, r['strips'])), language=None)

# ------------------------------------------------------------ size finder
with tab_size:
    st.subheader('Find a size close to the desired one')
    st.caption('The program looks for a size close to the one you need that wastes less of the sheet. Sizes can have decimals.')
    c = st.columns(4)
    sw_cm = c[0].number_input('Sheet: width, cm', 5.0, 300.0, 61.0, 0.5)
    sl_cm = c[1].number_input('Sheet: length, cm', 5.0, 300.0, 86.0, 0.5)
    pw_cm = c[2].number_input('Desired size: width, cm', 0.5, 200.0, 16.0, 0.1)
    ph_cm = c[3].number_input('Desired size: height, cm', 0.5, 200.0, 16.0, 0.1)
    c = st.columns(3)
    tol = c[0].slider('Allowed deviation from the size, %', 1, 30, 10)
    free = c[1].checkbox('Change width and height independently', value=False)
    wgt = c[2].slider('Importance of staying close to the desired size', 0.0, 3.0, 1.0, 0.1,
                      help='Higher — the program stays closer to the given size.')
    if st.button('Find size'):
        front, rec = cp.suggest_sizes(round(sw_cm * 10), round(sl_cm * 10), round(pw_cm * 10), round(ph_cm * 10),
                                      tol, not free, trim, knife, wgt)
        if not front: st.error('This size does not fit on the sheet.')
        else:
            f = lambda v: f'{v / 10:.1f}'
            if rec:
                st.success(f"Recommended: {f(rec['w'])}×{f(rec['h'])} cm — {rec['n']} pcs per sheet, waste {rec['waste']:.1f}% "
                           f"(deviation {rec['dev']:.1f}%).")
            st.dataframe(pd.DataFrame([{'Size, cm': f"{f(r['w'])} × {f(r['h'])}", 'Pieces per sheet': r['n'],
                                        'Waste, %': round(r['waste'], 1), 'Deviation, %': round(r['dev'], 1),
                                        '': 'recommended' if r is rec else ('desired' if r['dev'] == 0 else '')} for r in front]),
                         width='stretch', hide_index=True)
            st.caption('Only options with lower waste than any size closer to the desired one are listed.')

# ------------------------------------------------------------ library
with tab_lib:
    st.subheader('Saved patterns')
    lib = cp.Library(DB)
    data = []
    for k, sw, sl, t, cr in lib.db.execute('SELECT key,sw,sl,times_used,created FROM patterns ORDER BY times_used DESC'):
        strips = cp.from_json(k); cnt = {}
        for _, cols in strips:
            for n, w, ph, q in cols: cnt[n] = cnt.get(n, 0) + q
        data.append({'Sheet (mm)': f'{sw}×{sl}', 'Per sheet': ', '.join(f'{n} ×{q}' for n, q in sorted(cnt.items())),
                     'Times used': t, 'Created': cr})
    if data: st.dataframe(pd.DataFrame(data), width='stretch', hide_index=True)
    else: st.info('The library is empty — patterns will appear after the first calculation.')
    try:
        saved = recent_results(DB)
        if saved:
            st.subheader('Saved result archive (central database)')
            selected = st.selectbox('Open a stored result', [''] + [x[0] for x in saved],
                                    format_func=lambda rid: next((f'{x[1]} — {x[2]}' for x in saved if x[0] == rid), 'Select a result'))
            if selected:
                remote = get_result(DB, selected)
                if remote:
                    c_pdf,c_xlsx=st.columns(2)
                    c_pdf.download_button('Download stored PDF', base64.b64decode(remote['pdf']),
                                          'layout.pdf','application/pdf',key='remote_pdf')
                    c_xlsx.download_button('Download stored Excel',base64.b64decode(remote['xlsx']),
                                           'layout.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',key='remote_xlsx')
    except Exception as exc:
        st.warning('Could not load saved result archive: ' + str(exc))
    jobs = lib.db.execute('SELECT created,name FROM jobs ORDER BY id DESC LIMIT 20').fetchall()
    if jobs:
        st.subheader('Calculation history'); st.dataframe(pd.DataFrame(jobs, columns=['Date', 'File']), hide_index=True)
