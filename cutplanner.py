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
# Planner geometry uses 0.1 mm units internally. This avoids losing fractional
# millimetres (for example, 6 in = 152.4 mm) while keeping the DP arrays integer-based.
GEOMETRY_SCALE = 10
UNITS = {'mm': 1.0, 'cm': 10.0, 'in': INCH}
def to_mm(v, unit='in'):
    """Convert an input dimension to integer tenths of a millimetre."""
    return int(math.ceil(v * UNITS[unit] * GEOMETRY_SCALE - 1e-9))
def fnum(v): return ('%g' % v)
def cm(v): return '%.1f' % (v / (10 * GEOMETRY_SCALE))

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
