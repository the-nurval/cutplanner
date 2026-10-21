#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cutplanner - guillotine cutting layouts for paper sheets, with a pattern library.

  python cutplanner.py plan orders.xlsx            # compute a layout
  python cutplanner.py library                     # show saved patterns

Idea: 1 pack = 15 sheets of paper. One cut through a pack gives 15 pieces of each size
in the pattern = finished albums. So the number of sheets per pattern is a multiple of 15.
All patterns are guillotine patterns: the 1st cut goes across the sheet width (cut length <= knife),
then strips -> columns -> pieces. Patterns that are found are saved in SQLite and
reused in later calculations.
"""
import argparse, csv, json, math, re, sqlite3, sys, time
from storage import connect as connect_database
from pathlib import Path
import numpy as np
from scipy.optimize import linprog, milp, LinearConstraint, Bounds

INCH = 25.4
class PlanError(Exception): pass
def fail(msg): raise PlanError(msg)
UNITS = {'mm': 1.0, 'cm': 10.0, 'in': INCH}
def to_mm(v, unit='in'): return int(math.ceil(v * UNITS[unit] - 1e-9))
def fnum(v): return ('%g' % v)
def cm(v): return '%.1f' % (v / 10)

# ---------------------------------------------------------------- order
def order_name(w, l, unit='in'):
    """Size name used as a key: smaller side first; inches have no suffix (compatible with older libraries)."""
    a, b = sorted((w, l))
    return f'{fnum(a)}x{fnum(b)}' + ('' if unit == 'in' else unit)

def dims_of(name):
    m = re.match(r'(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)', name)
    return float(m.group(1)), float(m.group(2))

def _num(c):
    if isinstance(c, bool): return None
    if isinstance(c, (int, float)): return float(c)
    if isinstance(c, str):
        try: return float(c.strip().replace(',', '.'))
        except ValueError: return None
    return None

_HDR = {'w': ('width', 'ширин', 'laius'), 'l': ('length', 'height', 'длин', 'высот', 'pikkus'),
        'q': ('quantity', 'qty', 'album', 'количеств', 'kogus')}

def read_order_rows(path):
    """Reads an order file with three columns: width, length, quantity (albums).
    A header row is optional; without it the first three columns are used in that order.
    Returns [(width, length, quantity), ...] as plain numbers (the unit is chosen separately)."""
    p = Path(path)
    if p.suffix.lower() in ('.xlsx', '.xlsm'):
        import openpyxl
        ws = openpyxl.load_workbook(p, data_only=True).active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        text = p.read_text(encoding='utf-8-sig')
        head = next((l for l in text.splitlines() if l.strip()), '')
        delim = ';' if ';' in head else ('\t' if '\t' in head else ',')       # ';' allows decimal commas
        rows = list(csv.reader(text.splitlines(), delimiter=delim))
    blank = lambda c: c is None or (isinstance(c, str) and not c.strip())
    numbered = [(i, r) for i, r in enumerate(rows, 1) if not all(blank(c) for c in r)]
    if not numbered: fail('The file is empty.')
    cols, first = (0, 1, 2), numbered[0][1]
    if not any(_num(c) is not None for c in first):                    # first row is a header
        found = {}
        for j, c in enumerate(first):
            t = str(c).strip().lower() if c is not None else ''
            for key, words in _HDR.items():
                if key not in found and t.startswith(words): found[key] = j
        if len(found) < 3:
            fail('The header row must contain the columns Width, Length and Quantity '
                 '(or remove the header and put these three columns first, in this order).')
        cols = (found['w'], found['l'], found['q']); numbered = numbered[1:]
    out = []
    for i, r in numbered:
        if len(r) <= max(cols): fail(f'Row {i}: expected three columns (width, length, quantity).')
        w, l, q = (_num(r[j]) for j in cols)
        if w is None or l is None or q is None or w <= 0 or l <= 0 or q < 0 or q != int(q):
            fail(f'Row {i}: width, length and quantity must be numbers (width and length above 0, quantity a whole number).')
        if q > 0: out.append((w, l, int(q)))
    if not out: fail('No order rows found in the file (width, length, quantity).')
    return out

def orders_from_rows(rows, unit='in'):
    """[(width, length, quantity), ...] -> {size name: albums}; sizes are in `unit` (mm, cm or in)."""
    orders = {}
    for w, l, q in rows:
        name = order_name(w, l, unit); orders[name] = orders.get(name, 0) + int(q)
    if not orders: fail('Nothing to calculate: add sizes and album quantities.')
    return orders

def parse_orders(path, unit='in'): return orders_from_rows(read_order_rows(path), unit)

# ---------------------------------------------------------------- pattern library
class Library:
    def __init__(self, path):
        self.db = connect_database(path)
        self.db.execute('''CREATE TABLE IF NOT EXISTS patterns(
            key TEXT PRIMARY KEY, sw INT, sl INT, data TEXT, area_used REAL,
            times_used INT DEFAULT 0, created TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY, name TEXT, created TEXT, result TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS solutions(
            key TEXT PRIMARY KEY, result TEXT, created TEXT)''')
        self.db.commit()
    def get_solution(self, key):
        rows = list(self.db.execute('SELECT result FROM solutions WHERE key=?', (key,)))
        return json.loads(rows[0][0]) if rows else None
    def put_solution(self, key, sol):
        self.db.execute('DELETE FROM solutions WHERE key=?', (key,))
        self.db.execute('INSERT INTO solutions(key,result,created) VALUES(?,?,?)',
                        (key, json.dumps(sol), time.strftime('%Y-%m-%d %H:%M')))
    def load(self, sw, sl, pieces):
        out = {}
        for key, data in self.db.execute('SELECT key,data FROM patterns WHERE sw=? AND sl=?', (sw, sl)):
            strips = from_json(data)
            ok = all(n in pieces and {w, ph} == set(pieces[n]) for _, cols in strips for n, w, ph, _n in cols)
            if ok: out[key] = strips
        return out
    def save(self, key, strips, sw, sl, area, used):
        self.db.execute('INSERT OR IGNORE INTO patterns(key,sw,sl,data,area_used,created) VALUES(?,?,?,?,?,?)',
                        (key, sw, sl, to_json(strips), area, time.strftime('%Y-%m-%d %H:%M')))
        if used: self.db.execute('UPDATE patterns SET times_used=times_used+1 WHERE key=?', (key,))
    def commit(self): self.db.commit()

def to_json(strips): return json.dumps([{'h': h, 'cols': [list(c) for c in cols]} for h, cols in strips])
def from_json(s): return [(d['h'], [tuple(c) for c in d['cols']]) for d in json.loads(s)]
def canon(strips):
    return sorted(((h, sorted(cols)) for h, cols in strips), key=lambda t: (-t[0], t[1]))
def pkey(strips): return to_json(canon(strips))

# ---------------------------------------------------------------- pattern generation
class Planner:
    def __init__(self, sw, sl, pieces):
        self.sw, self.sl, self.pieces = sw, sl, pieces
        self.names = list(pieces); self.idx = {n: i for i, n in enumerate(self.names)}
        self.cols = []
        for n, (a, b) in pieces.items():
            if min(a, b) > sw or max(a, b) > sl and min(a, b) > sl:
                fail(f'Size {n} does not fit on the sheet')
            self.cols.append((n, a, b))
            if a != b: self.cols.append((n, b, a))          # (name, column width, piece height)
        H = set()
        for a, b in pieces.values():
            for d in (a, b):
                for m in range(1, 7):
                    if d * m <= sl: H.add(d * m)
        self.Hs = sorted(H)
    def counts(self, strips):
        v = np.zeros(len(self.names))
        for _, cols in strips:
            for n, w, ph, k in cols: v[self.idx[n]] += k
        return v
    def _strip(self, h, dual):
        ct = [(n, w, ph, h // ph) for n, w, ph in self.cols if ph <= h and w <= self.sw]
        best = [0.0] * (self.sw + 1); ch = [None] * (self.sw + 1)
        for x in range(1, self.sw + 1):
            best[x] = best[x - 1]
            for n, w, ph, k in ct:
                if w <= x:
                    v = best[x - w] + k * dual[self.idx[n]]
                    if v > best[x] + 1e-12: best[x] = v; ch[x] = (n, w, ph, k)
        comp, x = [], self.sw
        while x > 0:
            if ch[x] is None: x -= 1
            else: comp.append(ch[x]); x -= ch[x][1]
        return best[self.sw], comp
    def best_pattern(self, dual):
        sv = {h: self._strip(h, dual) for h in self.Hs}
        B = [0.0] * (self.sl + 1); C = [None] * (self.sl + 1)
        for y in range(1, self.sl + 1):
            B[y] = B[y - 1]
            for h in self.Hs:
                if h <= y and B[y - h] + sv[h][0] > B[y] + 1e-12:
                    B[y] = B[y - h] + sv[h][0]; C[y] = h
        strips, y = [], self.sl
        while y > 0:
            if C[y] is None: y -= 1
            else: strips.append((C[y], sv[C[y]][1])); y -= C[y]
        return B[self.sl], canon(strips)
    def validate(self, strips):
        assert sum(h for h, _ in strips) <= self.sl, 'pattern height exceeds the sheet'
        for h, cols in strips:
            assert sum(w for _, w, _, _ in cols) <= self.sw, 'strip width exceeds the sheet'
            for n, w, ph, k in cols:
                assert k * ph <= h and {w, ph} == set(self.pieces[n]), 'column error'
    def area(self, strips):
        return sum(self.counts(strips)[i] * a * b for i, (a, b) in enumerate(self.pieces.values()))

def _column_generation(pl, D, pool, iters=300):
    """Gilmore-Gomory LP for demand D; new patterns are added to the pool. Returns (A, LP result)."""
    for it in range(iters):
        keys = list(pool); A = np.array([pl.counts(pool[k]) for k in keys]).T
        r = linprog(np.ones(len(keys)), A_ub=-A, b_ub=-D, bounds=(0, None), method='highs')
        v, s = pl.best_pattern(-r.ineqlin.marginals)
        if v <= 1 + 1e-7 or pkey(s) in pool: break
        pool[pkey(s)] = s
    return A, r

def _packs_milp(A, D, maxp, tl, lp=None, cost=None, max_total=None, extra=()):
    """Whole packs: fewest packs (or `cost`) with at most `maxp` patterns.
    lp = theoretical minimum: the solver stops as soon as it reaches ceil(lp) instead of
    spending the whole time limit proving that nothing better exists."""
    m, n = A.shape; ub = int(math.ceil(D.max())) + 1
    cons = [LinearConstraint(np.hstack([A, np.zeros((m, n))]), D, np.inf),
            LinearConstraint(np.hstack([np.eye(n), -ub * np.eye(n)]), -np.inf, 0),
            LinearConstraint(np.r_[np.zeros(n), np.ones(n)][None, :], 0, maxp)]
    if max_total is not None:
        cons.append(LinearConstraint(np.r_[np.ones(n), np.zeros(n)][None, :], 0, max_total))
    for row, lo in extra:                            # extra: row·x >= lo (only on pack variables)
        cons.append(LinearConstraint(np.r_[row, np.zeros(n)][None, :], lo, np.inf))
    c = cost if cost is not None else np.r_[np.ones(n), np.zeros(n)]
    opts = {'time_limit': max(tl, 0.5)}
    if lp is not None and cost is None:
        t = math.ceil(lp - 1e-6)
        if t > 0: opts['mip_rel_gap'] = max((t - lp) / t, 0) + 1e-6
    r = milp(c, constraints=cons, integrality=np.ones(2 * n),
             bounds=Bounds(0, np.r_[np.full(n, ub), np.ones(n)]), options=opts)
    return r, (int(round(r.x[:n].sum())) if r.x is not None else None)

def _usable(pl, pool, keep):
    """Patterns allowed by the waste threshold (all patterns if the threshold leaves some size uncovered)."""
    ok = [k for k in pool if keep(pool[k])]
    if ok and all(any(pl.counts(pool[k])[i] > 0 for k in ok) for i in range(len(pl.names))): return ok
    return list(pool)

def _dive_once(pl, D, pool, rng, d):
    """Residual rounding ("diving"): fix whole packs of one LP pattern, subtract what it produces,
    re-run column generation for the remaining demand, repeat. Finds patterns that a good
    WHOLE-pack solution needs but the plain LP never produces. Returns the total packs of the dive."""
    resid = D.copy(); total = 0
    while resid.max() > 0:
        A, r = _column_generation(pl, np.clip(resid, 0, None), pool); x = r.x
        if d == 0: i = int(np.argmax(x))
        else:
            w = np.maximum(x, 0) ** 2 + 1e-9; i = int(rng.choice(len(x), p=w / w.sum()))
        f = x[i]
        n = int(math.ceil(f)) if d % 2 and f - math.floor(f) >= 0.5 else int(math.floor(f + 1e-6))
        n = max(1, n)
        resid = resid - n * A[:, i]; total += n
    return total

def _from_bigger(pl, D, pool, delta):
    """Plans the same order with every size `delta` mm bigger, then puts the real (smaller) pieces
    into those patterns. A pattern for bigger pieces always fits the real ones, and the different
    sizes lead the search to different combinations."""
    try: big = Planner(pl.sw, pl.sl, {n: (a + delta, b + delta) for n, (a, b) in pl.pieces.items()})
    except PlanError: return 0
    bpool = {}
    for n in big.names:
        d = np.zeros(len(big.names)); d[big.idx[n]] = 1
        _, s = big.best_pattern(d); bpool.setdefault(pkey(s), s)
    _column_generation(big, D, bpool, iters=40)
    added = 0
    for s in bpool.values():
        out = []
        for h, cols in s:
            nc = []
            for n, w, ph, k in cols:
                a, b = pl.pieces[n]
                nc.append((n, a, b, k) if (w, ph) == (a + delta, b + delta) else (n, b, a, k))
            out.append((max(k * ph for _, _, ph, k in nc), nc))
        try: pl.validate(out)
        except AssertionError: continue
        k = pkey(out)
        if k not in pool: pool[k] = canon(out); added += 1
    return added

def generate(pl, D, pool, log, maxp=8, keep=lambda s: True, budget=15):
    """Pattern search in stages; stops as soon as a whole-pack solution reaches the theoretical minimum.
    Returns (minimum possible packs, best packs found)."""
    t0 = time.time()
    for n in pl.names:
        d = np.zeros(len(pl.names)); d[pl.idx[n]] = 1
        _, s = pl.best_pattern(d); pool.setdefault(pkey(s), s)
    _, r = _column_generation(pl, D, pool); lp = r.fun; target = math.ceil(lp - 1e-6)
    log(f'  theoretical minimum: {lp:.1f} packs, so no fewer than {target}; patterns in pool: {len(pool)}')
    def check(stage):
        keys = _usable(pl, pool, keep); A = np.array([pl.counts(pool[k]) for k in keys]).T
        _, packs = _packs_milp(A, D, maxp, 3, lp)
        log(f'  {stage}: {packs} packs ({time.time() - t0:.0f} s)'); return packs
    best = check('library + basic search')
    if best == target: return target, best
    rng = np.random.default_rng(0); d = 0; deltas = [3, 6]
    while time.time() - t0 < budget:
        if deltas and d >= 2 * (3 - len(deltas)):     # alternate: 2 dives, +3 mm, 2 dives, +6 mm, more dives
            delta = deltas.pop(0); _from_bigger(pl, D, pool, delta); stage = f'patterns from sizes +{delta} mm'
        else:
            tot = _dive_once(pl, D, pool, rng, d); d += 1; stage = f'extra search, round {d}'
            if tot != target and d % 2: continue
        p = check(stage)
        if p is not None: best = min(best, p) if best else p
        if best == target: break
    if best != target:
        log(f'  best found: {best} packs; {target} might be possible but was not found within {budget} s')
    return target, best

def cut_count(strips):
    """Knife cuts for one pass through a pack: strip cuts + column cuts + piece cuts (simplicity of cutting)."""
    return sum(1 + sum(1 + max(k - 1, 0) for *_, k in cols) for _, cols in strips)

def solve(pl, D, pool, new_keys, maxp, pen, newpen, tl, log=print, lp=None, quick=None):
    """Priorities: 1) the order is complete; 2) fewest packs; 3) with those packs - least waste
    (extra pieces of order sizes count as useful, so a needed pack is filled with them);
    4) simplest cutting - fewer patterns, then fewer cuts, then library patterns."""
    keys = list(pool); A = np.array([pl.counts(pool[k]) for k in keys]).T
    n = A.shape[1]
    if lp is None:                                   # minimum for these patterns
        lp = linprog(np.ones(n), A_ub=-A, b_ub=-D, bounds=(0, None), method='highs').fun
    # Step 1: fewest packs; stops as soon as the minimum is reached.
    r1, best = _packs_milp(A, D, maxp, tl if quick is None else min(tl, quick), lp)
    if r1.x is None: fail('No solution found (increase --max-patterns or remove --library-only).')
    if r1.status == 1 and best > math.ceil(lp - 1e-6):
        log('  warning: solver time limit reached, the result may not be the best one (increase the time limit)')
    # Step 2: same packs, least waste = most useful area (in sheets).
    area = np.array([pl.area(pool[k]) for k in keys]) / (pl.sw * pl.sl)
    r2, p2 = _packs_milp(A, D, maxp, min(4, tl), cost=np.r_[-area, np.zeros(n)], max_total=best,
                         extra=[(np.ones(n), best)])
    if r2.x is None: r2 = r1
    used = float(area @ np.round(r2.x[:n]))
    # Step 3: same packs, same waste (within 0.05 sheet) - simplest cutting.
    cuts = np.array([cut_count(pool[k]) for k in keys], float)
    c = np.r_[0.001 * cuts, 1 + 0.1 * newpen * np.array([k in new_keys for k in keys], float)]
    r3, p3 = _packs_milp(A, D, maxp, min(4, tl), cost=c, max_total=best,
                         extra=[(np.ones(n), best), (area, used - 0.05)])
    r = r3 if r3.x is not None else r2
    x = np.round(r.x[:n]).astype(int)
    return [(keys[i], int(x[i])) for i in range(n) if x[i] > 0]

# ---------------------------------------------------------------- leftover filling
FILLER_PRESETS = {'A3': (297, 420), 'A4': (210, 297), 'A5': (148, 210), 'A6': (105, 148)}

def parse_fillers(spec):
    """'A3,A4,A5,mine=100x150' -> {'A3': (297, 420), ..., 'mine': (100, 150)} (mm)."""
    out = {}
    for tok in [t.strip() for t in (spec or '').split(',') if t.strip()]:
        if '=' in tok:
            name, dim = tok.split('=', 1)
            try: w, h = (int(round(float(v))) for v in re.split(r'[xXхХ×]', dim.strip()))
            except ValueError: fail(f'Could not parse leftover size "{tok}". Use: name=100x150 (mm)')
            out[name.strip()] = (w, h)
        elif tok.upper() in FILLER_PRESETS: out[tok.upper()] = FILLER_PRESETS[tok.upper()]
        else: fail(f'Unknown leftover format: {tok}')
    return out

def free_rects(strips, sw, sl):
    """Free rectangles of a pattern; each is separated by through-cuts, so they can be cut further on a guillotine."""
    rects, y = [], 0
    for i, (h, cols) in enumerate(strips, 1):
        x = 0
        for n, w, ph, k in cols:
            if h - k * ph > 0: rects.append((x, y + k * ph, w, h - k * ph, f'strip {i}, below column {n.replace("x", "×")}'))
            x += w
        if sw - x > 0: rects.append((x, y, sw - x, h, f'strip {i}, right side'))
        y += h
    if sl - y > 0: rects.append((0, y, sw, sl - y, 'bottom remainder of the sheet'))
    return rects

def fill_rect(w, h, fillers):
    fit = {n: d for n, d in fillers.items() if (d[0] <= w and d[1] <= h) or (d[1] <= w and d[0] <= h)}
    if not fit: return None
    pl = Planner(w, h, fit)
    v, s = pl.best_pattern(np.array([d[0] * d[1] for d in fit.values()], float))
    if v <= 0: return None
    pl.validate(s)
    return s, v

def fill_leftovers(pl, strips, fillers):
    out = []
    for x, y, w, h, where in free_rects(strips, pl.sw, pl.sl):
        f = fill_rect(w, h, fillers)
        if f: out.append(dict(x=x, y=y, w=w, h=h, where=where, strips=f[0], area=f[1]))
    return out

def strip_counts(strips):
    c = {}
    for _, cols in strips:
        for n, w, ph, k in cols: c[n] = c.get(n, 0) + k
    return c

def fill_instructions(r):
    if not r.get('fill'): return []
    L = ['+) Leftovers (extra cutting, by-products):']
    for f in r['fill']:
        L.append(f"   {f['where']} ({cm(f['w'])}×{cm(f['h'])} cm): " + ', '.join(f'{n} ×{k}' for n, k in strip_counts(f['strips']).items()))
    return L

# ---------------------------------------------------------------- output
def instructions(pl, strips):
    L = []; cum = 0; ys = []
    for h, _ in strips: cum += h; ys.append(cm(cum))
    L.append(f'1) Cuts across the full width ({cm(pl.sw)} cm), from the top edge: ' + ' → '.join(ys) + ' cm')
    if pl.sl - cum > 0: L.append(f'   (the last cut trims off {cm(pl.sl - cum)} cm of waste)')
    L.append('2) Cut each strip into columns:')
    for i, (h, cols) in enumerate(strips, 1):
        used = sum(w for _, w, _, _ in cols)
        parts = ' | '.join(f'{n.replace("x","×")}: {cm(w)}' + (f' (×{k} along the height)' if k > 1 else '') for n, w, ph, k in cols)
        L.append(f'   strip {i} (height {cm(h)}): {parts}  [remainder {cm(pl.sw - used)}]')
    seen = []
    for _, cols in strips:
        for n, w, ph, k in cols:
            if k > 1 and (n, ph, k) not in seen: seen.append((n, ph, k))
    if seen:
        L.append('3) Columns with several pieces along the height are cut into pieces:')
        for n, ph, k in seen: L.append(f'   {n.replace("x","×")}: {k} pcs of {cm(ph)} cm')
    return L

def draw_pattern(pl, r):
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    pal = plt.get_cmap('tab20'); colr = {n: pal(i * 2 % 20 + 1) for i, n in enumerate(pl.names)}
    strips = r['strips']; fig = plt.figure(figsize=(11.69, 8.27))
    fig.suptitle(f"Pattern {r['label']}: {r['packs']} packs = {r['sheets']} sheets", fontsize=15, fontweight='bold', x=0.04, ha='left')
    ax = fig.add_axes([0.04, 0.08, 0.42, 0.80]); y = 0
    for h, cols in strips:
        x = 0
        for n, w, ph, k in cols:
            for j in range(k):
                ax.add_patch(Rectangle((x, y + j * ph), w, ph, fc=colr[n], ec='k', lw=0.8))
                ax.text(x + w / 2, y + j * ph + ph / 2, n.replace('x', '×'), ha='center', va='center', fontsize=7, rotation=90 if w < 0.5 * ph else 0)
            x += w
        y += h
    for f in r.get('fill', []):
        yy = f['y']
        for h, cols in f['strips']:
            xx = f['x']
            for n, w, ph, k in cols:
                for j in range(k):
                    ax.add_patch(Rectangle((xx, yy + j * ph), w, ph, fc='#e0e0e0', ec='k', lw=0.6, hatch='///'))
                    ax.text(xx + w / 2, yy + j * ph + ph / 2, n, ha='center', va='center', fontsize=6, rotation=90 if w < 0.5 * ph else 0)
                xx += w
            yy += h
    ax.add_patch(Rectangle((0, 0), pl.sw, pl.sl, fc='none', ec='r', lw=1.5))
    ax.set_xlim(-20, pl.sw + 20); ax.set_ylim(pl.sl + 20, -20); ax.set_aspect('equal'); ax.axis('off')
    ax.set_title(f'Sheet {cm(pl.sw)}×{cm(pl.sl)} cm (side {cm(pl.sw)} = cut length)', fontsize=9)
    c = r['counts']
    t = ['Per sheet: ' + ', '.join(f'{n.replace("x","×")} — {int(c[i])} pcs' for i, n in enumerate(pl.names) if c[i]), '',
         'CUTTING ORDER:'] + instructions(pl, strips) + fill_instructions(r) + ['', f"Waste: {r['waste']:.1f}%" + (f" → {r['waste_filled']:.1f}% after filling leftovers" if r.get('fill') else '')]
    fig.text(0.49, 0.86, '\n'.join(t), fontsize=8.2, va='top', linespacing=1.5)
    if r.get('over'):
        fig.text(0.49, 0.10, '⚠ WASTE ABOVE THRESHOLD: use only if the price covers all the waste.\nTry changing the size (\"Size finder\" tab).', fontsize=9, color='red', va='top')
    return fig

def write_pdf(path, pl, result, orders, pack, fullarea, byprod=None):
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.patches import Rectangle
    pal = plt.get_cmap('tab20'); colr = {n: pal(i * 2 % 20 + 1) for i, n in enumerate(pl.names)}
    with PdfPages(path) as pdf:
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.suptitle('Cutting layout', fontsize=15, fontweight='bold', x=0.05, ha='left')
        hdr = ['Pattern', 'Packs', 'Sheets'] + [n.replace('x', '×') for n in pl.names] + ['Waste %']
        rows = []; tot = np.zeros(len(pl.names))
        for r in result:
            rows.append([r['label'] + (' ⚠' if r.get('over') else ''), r['packs'], r['sheets']] + [int(v) if v else '–' for v in r['counts']] + [f"{r['waste']:.1f}"])
            tot += r['packs'] * np.array(r['counts'])
        need = [orders[n] for n in pl.names]
        rows.append(['Total', sum(r['packs'] for r in result), sum(r['sheets'] for r in result)] + [int(v) for v in tot] + [''])
        rows.append(['Needed', '', ''] + need + [''])
        rows.append(['Surplus', '', ''] + [int(tot[i] - need[i]) for i in range(len(need))] + [''])
        ax = fig.add_axes([0.03, 0.3, 0.94, 0.55]); ax.axis('off')
        ax.text(0, 1.02, f'Pieces per sheet for each pattern; 1 pack = {pack} sheets = {pack} pieces of each size = albums', fontsize=10, transform=ax.transAxes)
        tb = ax.table(cellText=rows, colLabels=hdr, loc='upper center', cellLoc='center')
        tb.auto_set_font_size(False); tb.set_fontsize(10); tb.scale(1, 1.7)
        if byprod:
            fig.text(0.05, 0.22, 'By-products from leftovers (pcs): ' + ', '.join(f'{n} — {k}' for n, k in byprod.items()), fontsize=10)
        pdf.savefig(fig); plt.close(fig)
        for r in result:
            fig = draw_pattern(pl, r); pdf.savefig(fig); plt.close(fig)

def write_xlsx(path, pl, result, orders, pack, byprod=None):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = 'Summary'
    ws.append(['Pattern', f'Packs ({pack} sheets each)', 'Sheets'] + pl.names + ['Waste %'])
    tot = np.zeros(len(pl.names))
    for r in result:
        ws.append([r['label'], r['packs'], r['sheets']] + [int(v) for v in r['counts']] + [round(r['waste'], 1)])
        tot += r['packs'] * np.array(r['counts'])
    ws.append(['Total albums', sum(r['packs'] for r in result), sum(r['sheets'] for r in result)] + [int(v) for v in tot])
    ws.append(['Needed', '', ''] + [orders[n] for n in pl.names])
    ws.append(['Surplus', '', ''] + [int(tot[i] - orders[n]) for i, n in enumerate(pl.names)])
    if byprod:
        ws.append([]); ws.append(['By-products from leftovers (pcs)'] + [f'{n}: {k}' for n, k in byprod.items()])
    for c in ws[1]: c.font = Font(bold=True)
    w2 = wb.create_sheet('Cutting order')
    for r in result:
        w2.append([f"Pattern {r['label']} — {r['packs']} packs ({r['sheets']} sheets)"]); w2.cell(w2.max_row, 1).font = Font(bold=True)
        for line in instructions(pl, r['strips']) + fill_instructions(r): w2.append([line])
        w2.append([])
    w2.column_dimensions['A'].width = 140
    wb.save(path)

# ---------------------------------------------------------------- flexible size suggestion
def pieces_per_sheet(sw, sl, w, h, knife):
    """Maximum number of w×h pieces per sw×sl sheet (same three-stage guillotine patterns as the main calculation)."""
    best = 0
    for a, b in ((sw, sl), (sl, sw)):
        if a > knife or (min(w, h) > min(a, b)) or (max(w, h) > max(a, b)): continue
        try: pl = Planner(a, b, {'p': (w, h)})
        except PlanError: continue
        v, _ = pl.best_pattern(np.array([1.0]))
        best = max(best, int(round(v)))
    return best

def suggest_sizes(W, Lg, w, h, tol=10.0, keep_aspect=True, trim=0, knife=780, weight=1.0):
    """Looks for sizes near the desired one (w×h, mm) with minimal waste.
    tol is the tolerance, % of the size. Returns (list of Pareto-optimal options, recommendation).
    An option qualifies if its waste is lower than that of any size closer to the desired one."""
    sw, sl = W - 2 * trim, Lg - 2 * trim
    cands = set()
    lo_w, hi_w = int(round(w * (1 - tol / 100))), int(round(w * (1 + tol / 100)))
    lo_h, hi_h = int(round(h * (1 - tol / 100))), int(round(h * (1 + tol / 100)))
    if keep_aspect:
        for wi in range(lo_w, hi_w + 1): cands.add((wi, max(1, int(round(h * wi / w)))))
    else:
        for wi in range(lo_w, hi_w + 1):
            for hi in range(lo_h, hi_h + 1): cands.add((wi, hi))
    cands.add((w, h)); rows = []
    for wi, hi in cands:
        n = pieces_per_sheet(sw, sl, wi, hi, knife)
        if n < 1: continue
        dev = 100 * max(abs(wi - w) / w, abs(hi - h) / h)
        waste = 100 - 100 * n * wi * hi / (W * Lg)
        rows.append(dict(w=wi, h=hi, n=n, waste=waste, dev=dev, score=waste + weight * dev))
    rows.sort(key=lambda r: (r['dev'], r['waste']))
    front, best = [], 1e9
    for r in rows:
        if r['waste'] < best - 1e-9: front.append(r); best = r['waste']
    return front, (min(front, key=lambda r: r['score']) if front else None)

def cmd_suggest(a):
    k = {'mm': 1.0, 'cm': 10.0, 'in': 25.4}[a.unit]
    W, Lg = (round(float(v) * k) for v in a.sheet.lower().split('x'))
    w, h = (round(float(v) * k) for v in a.size.lower().split('x'))
    front, rec = suggest_sizes(W, Lg, w, h, a.tol, not a.free_aspect, a.trim, a.knife, a.weight)
    f = lambda v: '%.1f' % (v / 10)
    print(f'Sheet {f(W)}×{f(Lg)} cm, desired size {f(w)}×{f(h)} cm, tolerance ±{a.tol:g}%')
    print('size, cm          pcs/sheet      waste   deviation')
    for r in front:
        mark = '  <- recommended' if r is rec else ('  (desired)' if r['dev'] == 0 else '')
        print(f"{f(r['w']):>6} × {f(r['h']):<6}   {r['n']:>8}      {r['waste']:5.1f}%   {r['dev']:5.1f}%{mark}")

# ---------------------------------------------------------------- commands
def compute(orders, a, log=print, jobname=''):
    """Calculation core. orders: {'10x12': albums}. a: object with parameters (see main)."""
    unit = getattr(a, 'unit', 'in')
    names = sorted(orders, key=lambda s: (dims_of(s)[1], dims_of(s)[0]), reverse=True)
    W, Lg = (int(v) for v in a.sheet.lower().split('x'))
    if W > a.knife and Lg <= a.knife: W, Lg = Lg, W
    if W > a.knife: fail(f'Neither side of the sheet fits within the knife length of {a.knife} mm')
    sw, sl = W - 2 * a.trim, Lg - 2 * a.trim
    pieces = {n: tuple(to_mm(v, unit) for v in dims_of(n)) for n in names}
    pl = Planner(sw, sl, pieces)
    D = np.array([orders[n] for n in names], float)
    log(f'Sheet {W}×{Lg} mm, trim {a.trim} mm, pack {a.pack} sheets')
    lib = Library(a.db); pool = dict(lib.load(sw, sl, pieces)); libkeys = set(pool)
    log(f'Library patterns matching the order: {len(pool)}')
    mw = getattr(a, 'max_waste', 100)
    keep = lambda v: 100 - 100 * pl.area(v) / (W * Lg) <= mw
    # The same order (sheet, sizes, quantities, settings) was solved before: reuse it, no new search.
    skey = json.dumps([sw, sl, sorted(pieces.items()), [orders[n] for n in names], a.max_patterns, mw, a.library_only])
    cached = lib.get_solution(skey)
    if cached and all(k in pool for k, _ in cached):
        log('Same order was calculated before: solution taken from the library')
        sol = [(k, int(p)) for k, p in cached]
    else:
        if not a.library_only:
            log('Searching for new patterns...')
            budget = getattr(a, 'search_budget', 15)
            generate(pl, D, pool, log, a.max_patterns, keep, budget)
        elif not pool: fail('The library has no patterns for this sheet / these sizes.')
        if mw < 100:
            ok = {k: v for k, v in pool.items() if keep(v)}
            cover = [any(pl.counts(v)[i] > 0 for v in ok.values()) for i in range(len(names))]
            if all(cover): pool = ok; log(f'  patterns kept with waste ≤ {mw}%: {len(pool)}')
            else: log(f'  warning: no patterns with waste ≤ {mw}% exist for some sizes; limit not applied')
        sol = solve(pl, D, pool, set(pool) - libkeys, a.max_patterns, a.pattern_penalty, a.new_penalty, a.time_limit, log, quick=None if a.library_only else 5)
        lib.put_solution(skey, sol)
    sol.sort(key=lambda t: -t[1]); result = []
    for i, (k, packs) in enumerate(sol):
        s = pool[k]; pl.validate(s)
        result.append(dict(label='ABCDEFGHIJKLMNOPQRSTUVWXYZ'[i], key=k, strips=s, packs=packs, sheets=packs * a.pack,
                           counts=list(pl.counts(s)), waste=100 - 100 * pl.area(s) / (W * Lg), new=k not in libkeys))
    fillers = parse_fillers(getattr(a, 'fillers', ''))
    for r in result:
        r['fill'] = fill_leftovers(pl, r['strips'], fillers) if fillers else []
        r['fill_counts'] = {}
        for f in r['fill']:
            for n, k in strip_counts(f['strips']).items(): r['fill_counts'][n] = r['fill_counts'].get(n, 0) + k
        r['waste_filled'] = r['waste'] - 100 * sum(f['area'] for f in r['fill']) / (W * Lg)
    for r in result:
        r['over'] = mw < 100 and (r['waste_filled'] if r['fill'] else r['waste']) > mw
    byprod = {}
    for r in result:
        for n, k in r['fill_counts'].items(): byprod[n] = byprod.get(n, 0) + k * r['sheets']
    for r in result: lib.save(r['key'], r['strips'], sw, sl, pl.area(r['strips']), True)
    lib.db.execute('INSERT INTO jobs(name,created,result) VALUES(?,?,?)', (jobname, time.strftime('%Y-%m-%d %H:%M'),
                   json.dumps([[r['label'], r['packs']] for r in result]))); lib.commit()
    tot = sum(r['sheets'] for r in result)
    used = sum(r['sheets'] * pl.area(r['strips']) for r in result)
    return dict(pl=pl, result=result, names=names, orders=orders, unit=unit, W=W, Lg=Lg, total_sheets=tot, max_waste=mw, byproducts=byprod, fillers=fillers,
                total_packs=tot // a.pack, waste=100 - 100 * used / (tot * W * Lg))

def summary_lines(R):
    L = [f"TOTAL: {R['total_sheets']} sheets ({R['total_packs']} packs), patterns: {len(R['result'])}, average waste {R['waste']:.1f}%"]
    for r in R['result']:
        L.append(f"  {r['label']}: {r['packs']:>3} packs = {r['sheets']:>4} sheets | " + ', '.join(f'{n} ×{int(c)}' for n, c in zip(R['names'], r['counts']) if c)
                 + f" | waste {r['waste']:.1f}%" + (f" → {r['waste_filled']:.1f}% with leftovers" if r.get('fill') else '') + (' ⚠ WASTE ABOVE THRESHOLD' if r.get('over') else '') + (' [NEW]' if r['new'] else ' [from library]'))
    if R.get('byproducts'): L.append('By-products from leftovers: ' + ', '.join(f'{n} — {k} pcs' for n, k in R['byproducts'].items()))
    return L

def cmd_plan(a):
    orders = parse_orders(a.orders, a.unit)
    for n in sorted(orders): print(f'  {n}: {orders[n]} albums')
    R = compute(orders, a, print, Path(a.orders).name)
    out = Path(a.out) if a.out else Path(a.orders).with_suffix('').parent / (Path(a.orders).stem + '_plan')
    out.mkdir(parents=True, exist_ok=True)
    write_pdf(out / 'layout.pdf', R['pl'], R['result'], orders, a.pack, R['W'] * R['Lg'], R['byproducts'])
    write_xlsx(out / 'layout.xlsx', R['pl'], R['result'], orders, a.pack, R['byproducts'])
    print('\n' + '\n'.join(summary_lines(R))); print(f'Files: {out}')

def cmd_library(a):
    lib = Library(a.db)
    for k, sw, sl, ar, t, cr in lib.db.execute('SELECT key,sw,sl,area_used,times_used,created FROM patterns ORDER BY times_used DESC'):
        s = from_json(k); names = sorted({n for _, cols in s for n, *_ in cols})
        print(f'{sw}×{sl} | used {t} times | created {cr} | sizes: {", ".join(names)}')

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('plan'); p.add_argument('orders')
    p.add_argument('--db', default='patterns.db'); p.add_argument('--out')
    p.add_argument('--unit', choices=['mm', 'cm', 'in'], default='in', help='unit of album sizes in the order file (default: in)')
    p.add_argument('--sheet', default='780x1090', help='sheet size, mm (width x length)')
    p.add_argument('--knife', type=int, default=780, help='max cut length, mm')
    p.add_argument('--trim', type=int, default=0, help='edge trim on each side, mm')
    p.add_argument('--pack', type=int, default=15); p.add_argument('--max-patterns', type=int, default=8)
    p.add_argument('--new-penalty', type=float, default=0.5, help='penalty for a new pattern (in packs): higher = prefers library patterns')
    p.add_argument('--pattern-penalty', type=float, default=0.15)
    p.add_argument('--max-waste', type=float, default=10, help='waste threshold per pattern, %% (default 10; 100 = no limit). Patterns above the threshold are not used if they can be avoided; otherwise they are flagged ⚠')
    p.add_argument('--fillers', default='', help='what to fill sheet leftovers with, e.g. A3,A4,A5 or name=100x150 (mm)')
    p.add_argument('--library-only', action='store_true'); p.add_argument('--time-limit', type=int, default=60)
    p.set_defaults(f=cmd_plan)
    g = sub.add_parser('suggest', help='find a size close to the desired one (minimum waste)')
    g.add_argument('--sheet', required=True); g.add_argument('--size', required=True)
    g.add_argument('--unit', choices=['mm', 'cm', 'in'], default='cm')
    g.add_argument('--tol', type=float, default=10, help='tolerance, %% of the size')
    g.add_argument('--free-aspect', action='store_true', help='change width and height independently')
    g.add_argument('--trim', type=float, default=0); g.add_argument('--knife', type=int, default=780)
    g.add_argument('--weight', type=float, default=1.0, help='weight of closeness to the desired size (higher = closer)')
    g.set_defaults(f=cmd_suggest)
    l = sub.add_parser('library'); l.add_argument('--db', default='patterns.db'); l.set_defaults(f=cmd_library)
    a = ap.parse_args()
    try: a.f(a)
    except PlanError as e: sys.exit(str(e))

if __name__ == '__main__': main()
