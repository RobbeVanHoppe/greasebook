#!/usr/bin/env python3
"""
GreaseBook web -- garage maintenance manager as a Flask app.

Same data model and logic as the desktop version; the database folder is
fully compatible (point GREASEBOOK_DATA at an existing ~/.greasebook).

Run for development:   python3 app.py
Run for real:          waitress-serve --host=0.0.0.0 --port=8000 app:app

Env:
  GREASEBOOK_DATA      data folder (default ~/.greasebook)
  GREASEBOOK_PASSWORD  if set, the app requires this password to log in
"""

import csv
import gzip
import io
import json
import logging
import re
import os
import secrets
import textwrap
from datetime import date, datetime
from html import escape

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename
import sqlite3

APP_NAME = "GreaseBook"
DATA_DIR = os.environ.get("GREASEBOOK_DATA") or os.path.join(
    os.path.expanduser("~"), ".greasebook")
DB_PATH = os.path.join(DATA_DIR, "garage.db")
ATTACH_DIR = os.path.join(DATA_DIR, "attachments")
DATE_FMT = "%Y-%m-%d"
PASSWORD = os.environ.get("GREASEBOOK_PASSWORD")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    vtype TEXT NOT NULL DEFAULT 'Car',
    make TEXT, model TEXT, year INTEGER,
    vin TEXT, plate TEXT,
    odometer_km REAL NOT NULL DEFAULT 0,
    odo_updated TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS suppliers(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT, email TEXT, phone TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS parts(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    part_number TEXT,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    unit_price REAL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS maint_items(
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    interval_km REAL,
    interval_months REAL,
    last_km REAL,
    last_date TEXT,
    part_id INTEGER REFERENCES parts(id) ON DELETE SET NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    odometer_km REAL,
    title TEXT,
    notes TEXT,
    labor_cost REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_parts(
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    qty REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_items(
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES maint_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS attachments(
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fuel_log(
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    odometer_km REAL NOT NULL,
    liters REAL NOT NULL,
    total_cost REAL,
    full_tank INTEGER NOT NULL DEFAULT 1,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS fuel_resets(
    vehicle_id INTEGER PRIMARY KEY REFERENCES vehicles(id) ON DELETE CASCADE,
    odometer_km REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_guides(
    id INTEGER PRIMARY KEY,
    to_number TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    change INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL,
    imported TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs(
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    guide_id INTEGER NOT NULL REFERENCES job_guides(id) ON DELETE CASCADE,
    guide_change INTEGER NOT NULL DEFAULT 0,
    started TEXT NOT NULL,
    finished TEXT,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    records TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS wiring_sets(
    id INTEGER PRIMARY KEY,
    set_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    change INTEGER NOT NULL DEFAULT 0,
    date TEXT,
    data TEXT NOT NULL,
    imported TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wiring_boards(
    set_id INTEGER NOT NULL REFERENCES wiring_sets(id) ON DELETE CASCADE,
    unit TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY(set_id, unit)
);
"""

# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(ATTACH_DIR, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.executescript(SCHEMA)
    return g.db


def q(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql, params=()):
    con = get_db()
    cur = con.execute(sql, params)
    con.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- helpers

def today_str():
    return date.today().strftime(DATE_FMT)


def parse_date(s):
    try:
        return datetime.strptime(str(s).strip(), DATE_FMT).date()
    except (ValueError, TypeError):
        return None


def months_between(d1, d2):
    return max(0.0, (d2 - d1).days / 30.4375)


def to_float(s):
    """'' -> None, valid number -> float, junk -> ValueError."""
    s = str(s or "").strip().replace(",", ".").replace(" ", "")
    if not s:
        return None
    return float(s)


def fmt_km(v):
    if v is None:
        return "\u2014"
    return f"{v:,.0f}".replace(",", "\u2009") + " km"


def fmt_money(v):
    if v is None:
        return "\u2014"
    return f"\u20ac\u2009{v:,.2f}".replace(",", "\u2009")


def effective_baseline(item):
    """The 'last done' baseline for a schedule item.

    Sessions are the source of truth: the newest session that covers this
    item wins; the manually stored last_km / last_date is the fallback.
    Editing / unchecking / deleting sessions recalculates automatically.
    """
    s = q1("SELECT s.odometer_km km, s.date d FROM session_items si "
           "JOIN sessions s ON s.id = si.session_id WHERE si.item_id=? "
           "ORDER BY (s.odometer_km IS NULL), s.odometer_km DESC, s.date DESC "
           "LIMIT 1", (item["id"],))
    mk, md = item["last_km"], item["last_date"]
    if not s:
        return mk, md
    sk, sd = s["km"], s["d"]
    if mk is not None and sk is not None:
        return (sk, sd) if sk >= mk else (mk, md)
    mdd, sdd = parse_date(md), parse_date(sd)
    if mdd and sdd:
        return (sk, sd) if sdd >= mdd else (mk, md)
    return sk, sd


def item_status(item, current_km):
    """Life-used status. pct is the worst of km-based and time-based usage."""
    lk, ld = effective_baseline(item)
    pcts = []
    due_km = None
    due_date = None
    if item["interval_km"] and lk is not None:
        used = max(0.0, (current_km or 0) - lk)
        pcts.append(used / item["interval_km"])
        due_km = lk + item["interval_km"]
    if item["interval_months"]:
        last_d = parse_date(ld)
        if last_d:
            pcts.append(months_between(last_d, date.today())
                        / item["interval_months"])
            due_date = date.fromordinal(
                last_d.toordinal() + int(item["interval_months"] * 30.4375))
    st = {"last_km": lk, "last_date": ld, "due_km": due_km,
          "due_date": due_date}
    if not pcts:
        st.update(pct=None, tag="na")
        return st
    pct = max(pcts)
    st.update(pct=pct, width=min(pct, 1.0) * 100,
              tag="due" if pct >= 1.0 else ("warn" if pct >= 0.75 else "ok"))
    return st


def fuel_segments(vehicle_id):
    """{fill-up id: (km, liters, start odometer)} for each full fill closing a
    full-to-full segment. Liters include partial fills inside the segment."""
    rows = q("SELECT * FROM fuel_log WHERE vehicle_id=? "
             "ORDER BY odometer_km, date", (vehicle_id,))
    segs = {}
    acc = 0.0
    prev_full = None
    for r in rows:
        if prev_full is not None:
            acc += r["liters"]
        if r["full_tank"]:
            if prev_full is not None and r["odometer_km"] > prev_full["odometer_km"]:
                segs[r["id"]] = (r["odometer_km"] - prev_full["odometer_km"], acc,
                                 prev_full["odometer_km"])
            prev_full, acc = r, 0.0
    return segs


def fuel_reset_km(vehicle_id):
    r = q1("SELECT odometer_km FROM fuel_resets WHERE vehicle_id=?", (vehicle_id,))
    return r["odometer_km"] if r else None


def fuel_stats(vehicle_id):
    """(km driven, liters burned) between full-tank fills since the last reset."""
    reset = fuel_reset_km(vehicle_id)
    km = lit = 0.0
    for d, l, start in fuel_segments(vehicle_id).values():
        if reset is None or start >= reset:
            km += d
            lit += l
    return km, lit


def cost_summary(vehicle_id):
    """Everything you have poured into this vehicle. The money pit, quantified."""
    parts = q1("SELECT COALESCE(SUM(sp.qty * sp.unit_price), 0) c "
               "FROM session_parts sp JOIN sessions s ON s.id = sp.session_id "
               "WHERE s.vehicle_id=?", (vehicle_id,))["c"]
    labor = q1("SELECT COALESCE(SUM(labor_cost), 0) c FROM sessions "
               "WHERE vehicle_id=?", (vehicle_id,))["c"]
    fuel = q1("SELECT COALESCE(SUM(total_cost), 0) c FROM fuel_log "
              "WHERE vehicle_id=?", (vehicle_id,))["c"]
    n_sess = q1("SELECT COUNT(*) c FROM sessions WHERE vehicle_id=?",
                (vehicle_id,))["c"]
    maint = parts + labor
    grand = maint + fuel

    # span of the records: odometer covered and calendar time
    span = q1("SELECT MIN(o) lo, MAX(o) hi, MIN(d) first, MAX(d) last FROM ("
              "SELECT odometer_km o, date d FROM sessions WHERE vehicle_id=? "
              "UNION ALL "
              "SELECT odometer_km o, date d FROM fuel_log WHERE vehicle_id=?)",
              (vehicle_id, vehicle_id))
    v = q1("SELECT odometer_km FROM vehicles WHERE id=?", (vehicle_id,))
    km_span = None
    if span and span["lo"] is not None:
        km_span = max(0.0, (v["odometer_km"] or 0) - span["lo"])
    per_km = (grand / km_span) if km_span else None

    years = None
    if span and span["first"]:
        d1, d2 = parse_date(span["first"]), date.today()
        if d1:
            years = (d2 - d1).days / 365.25
    # Only extrapolate to a yearly figure once there is enough history to make
    # it mean anything. Six months of records annualised is a fantasy number.
    per_year = (grand / years) if years and years >= 0.5 else None

    worst = q1("SELECT s.date, s.title, s.labor_cost + COALESCE("
               "(SELECT SUM(qty * unit_price) FROM session_parts "
               "WHERE session_id = s.id), 0) t FROM sessions s "
               "WHERE s.vehicle_id=? ORDER BY t DESC LIMIT 1", (vehicle_id,))
    top_parts = q("SELECT p.name, SUM(sp.qty) qty, SUM(sp.qty * sp.unit_price) t "
                  "FROM session_parts sp JOIN parts p ON p.id = sp.part_id "
                  "JOIN sessions s ON s.id = sp.session_id WHERE s.vehicle_id=? "
                  "GROUP BY p.id ORDER BY t DESC LIMIT 5", (vehicle_id,))
    return {"parts": parts, "labor": labor, "maint": maint, "fuel": fuel,
            "grand": grand, "sessions": n_sess, "km_span": km_span,
            "per_km": per_km, "years": years, "per_year": per_year,
            "worst": worst, "top_parts": top_parts,
            "first": span["first"] if span else None}


COMMON_ITEMS = {
    "Car": [
        ("Engine oil + filter", 10000, 12), ("Air filter", 30000, 36),
        ("Spark plugs", 40000, 48), ("Coolant", 40000, 48),
        ("Brake fluid", None, 24), ("Timing belt", 90000, 60),
        ("Gearbox oil", 60000, None), ("Differential oil", 60000, None),
        ("Brake pads (front)", 40000, None), ("Brake pads (rear)", 50000, None),
        ("Tires", 40000, 72),
    ],
    "Motorcycle": [
        ("Engine oil + filter", 6000, 12), ("Air filter", 12000, 24),
        ("Spark plugs", 12000, 24), ("Brake fluid", None, 24),
        ("Primary / transmission oil", 8000, 12),
        ("Drive belt / chain service", 8000, 12), ("Fork oil", 20000, 24),
        ("Tires", 15000, 60), ("Battery", None, 48),
    ],
}

# ---------------------------------------------------------------- tiny PDF

class SimplePDF:
    """Minimal dependency-free PDF writer: Helvetica text lines on A4."""

    PAGE_W, PAGE_H = 595, 842
    MARGIN = 50

    def __init__(self):
        self.items = []

    def add(self, text="", size=10, bold=False, wrap=100):
        for chunk in (textwrap.wrap(self._clean(str(text)), wrap) or [""]):
            self.items.append((chunk, size, bold))

    # Characters PDF base-14 Helvetica cannot render, mapped to ones it can.
    _SUBS = {
        "\u2009": " ",     # thin space (our thousands separator)
        "\u202f": " ",     # narrow no-break space
        "\u00a0": " ",     # no-break space
        "\u2011": "-",     # non-breaking hyphen
        "\u2212": "-",     # minus sign
    }

    @classmethod
    def _clean(cls, s):
        for a, b in cls._SUBS.items():
            s = s.replace(a, b)
        return s

    @classmethod
    def _esc(cls, s):
        s = cls._clean(s)
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    def render(self):
        pages, cur = [], []
        y = self.PAGE_H - self.MARGIN
        for text, size, bold in self.items:
            lh = size * 1.45
            if y - lh < self.MARGIN:
                pages.append(cur)
                cur, y = [], self.PAGE_H - self.MARGIN
            y -= lh
            cur.append((text, size, bold, y))
        pages.append(cur)

        objs = []
        page_ids = [5 + i * 2 for i in range(len(pages))]
        objs.append("<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
        objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                    "/Encoding /WinAnsiEncoding >>")
        objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                    "/Encoding /WinAnsiEncoding >>")
        for i, page in enumerate(pages):
            pid = page_ids[i]
            objs.append(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.PAGE_W} "
                f"{self.PAGE_H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> "
                f">> /Contents {pid + 1} 0 R >>")
            ops = ["BT"]
            for text, size, bold, ly in page:
                font = "/F2" if bold else "/F1"
                ops.append(f"{font} {size} Tf 1 0 0 1 {self.MARGIN} {ly:.1f} Tm "
                           f"({self._esc(text)}) Tj")
            ops.append("ET")
            stream = "\n".join(ops).encode("cp1252", "replace")
            objs.append(b"<< /Length " + str(len(stream)).encode()
                        + b" >>\nstream\n" + stream + b"\nendstream")

        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for i, obj in enumerate(objs, start=1):
            offsets.append(len(out))
            body = obj if isinstance(obj, bytes) else obj.encode("cp1252",
                                                                "replace")
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref}\n%%EOF").encode()
        return bytes(out)


def build_vehicle_report(v):
    pdf = SimplePDF()
    pdf.add(f"{APP_NAME} service history \u2014 {v['name']}", 16, True)
    pdf.add(f"Generated {today_str()}", 9)
    pdf.add()
    desc = " ".join(str(s) for s in (v["make"], v["model"], v["year"]) if s)
    pdf.add(f"{v['vtype']}: {desc or '-'}", 11, True)
    pdf.add(f"VIN: {v['vin'] or '-'}    Plate: {v['plate'] or '-'}", 10)
    pdf.add(f"Odometer: {fmt_km(v['odometer_km'])} "
            f"(updated {v['odo_updated'] or '-'})", 10)
    pdf.add()

    pdf.add("Maintenance schedule status", 12, True)
    items = q("SELECT * FROM maint_items WHERE vehicle_id=? "
              "ORDER BY name COLLATE NOCASE", (v["id"],))
    if not items:
        pdf.add("(no schedule items)", 9)
    for it in items:
        st = item_status(it, v["odometer_km"])
        due = []
        if st["due_km"] is not None:
            due.append(f"due {fmt_km(st['due_km'])}")
        if st["due_date"]:
            due.append(f"due {st['due_date']}")
        pct = "-" if st["pct"] is None else f"{st['pct'] * 100:.0f}% used"
        flag = "  ** OVERDUE **" if st["pct"] and st["pct"] >= 1 else ""
        pdf.add(f"{it['name']}: {pct}  ({', '.join(due) or 'no baseline'}){flag}",
                10)
    pdf.add()

    pdf.add("Maintenance sessions", 12, True)
    sessions = q("SELECT * FROM sessions WHERE vehicle_id=? "
                 "ORDER BY date DESC, id DESC", (v["id"],))
    if not sessions:
        pdf.add("(no sessions yet)", 9)
    for s in sessions:
        parts = q("SELECT sp.*, p.name, p.part_number FROM session_parts sp "
                  "JOIN parts p ON p.id = sp.part_id WHERE sp.session_id=?",
                  (s["id"],))
        pcost = sum(r["qty"] * r["unit_price"] for r in parts)
        total = pcost + (s["labor_cost"] or 0)
        pdf.add(f"{s['date']}  {s['title'] or 'Maintenance'}  @ "
                f"{fmt_km(s['odometer_km'])}   total {fmt_money(total)}",
                10, True)
        for r in parts:
            pn = f" [{r['part_number']}]" if r["part_number"] else ""
            pdf.add(f"    {r['qty']:g} x {r['name']}{pn}  @ "
                    f"{fmt_money(r['unit_price'])} = "
                    f"{fmt_money(r['qty'] * r['unit_price'])}", 9)
        if s["labor_cost"]:
            pdf.add(f"    Labor: {fmt_money(s['labor_cost'])}", 9)
        if s["notes"]:
            pdf.add(f"    Notes: {s['notes']}", 9)
        pdf.add()

    # ---- the money pit
    cs = cost_summary(v["id"])
    pdf.add()
    pdf.add("Cost summary", 12, True)
    pdf.add("-" * 76, 9)
    pdf.add(f"Parts:                    {fmt_money(cs['parts'])}", 10)
    pdf.add(f"Labor:                    {fmt_money(cs['labor'])}", 10)
    pdf.add(f"Maintenance total:        {fmt_money(cs['maint'])}"
            f"   ({cs['sessions']} session"
            f"{'s' if cs['sessions'] != 1 else ''})", 10)
    pdf.add(f"Fuel:                     {fmt_money(cs['fuel'])}", 10)
    pdf.add("-" * 76, 9)
    pdf.add(f"TOTAL SPENT ON THIS VEHICLE:   {fmt_money(cs['grand'])}", 13, True)
    pdf.add("-" * 76, 9)
    pdf.add()

    if cs["per_km"]:
        pdf.add(f"Cost per km:              {fmt_money(cs['per_km'])} / km"
                f"   (over {cs['km_span']:.0f} km of logged history)", 10)
    if cs["per_year"]:
        pdf.add(f"Cost per year:            {fmt_money(cs['per_year'])} / year"
                f"   (records start {cs['first']})", 10)
    km, lit = fuel_stats(v["id"])
    if km > 0:
        pdf.add(f"Fuel consumption:         {lit / km * 100:.2f} l/100km"
                f"   ({lit:.1f} l over {km:.0f} km between full tanks)", 10)
    if cs["worst"] and cs["worst"]["t"]:
        w = cs["worst"]
        pdf.add(f"Most expensive session:   {fmt_money(w['t'])}"
                f"   {w['title'] or 'Maintenance'} ({w['date']})", 10)
    if cs["top_parts"]:
        pdf.add()
        pdf.add("Biggest spend by part", 11, True)
        for r in cs["top_parts"]:
            pdf.add(f"    {fmt_money(r['t']):>12}   {r['qty']:g} x {r['name']}", 10)
    return pdf.render()


# ---------------------------------------------------------------- flask app

def _secret_key():
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, "secret_key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
    with open(p) as f:
        return f.read().strip()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)
app.secret_key = _secret_key()
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB uploads

app.jinja_env.filters["km"] = fmt_km
app.jinja_env.filters["money"] = fmt_money
app.jinja_env.globals["mazda"] = lambda c: mazda_colour(c)


@app.errorhandler(Exception)
def handle_any_error(e):
    """Never hand an HTML error page to a JSON fetch, and never show the
    user a blank 500. Log the traceback (journalctl -u greasebook) and
    surface a readable message."""
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(error=f"{e.code} {e.name}"), e.code
        return e
    app.logger.exception("Unhandled error: %s %s", request.method, request.path)
    if isinstance(e, sqlite3.IntegrityError):
        msg = ("This form refers to a part or supplier that no longer exists "
               "in the database. Reload the page (Ctrl-Shift-R) and try again.")
    else:
        msg = f"{type(e).__name__}: {e}"
    if request.path.startswith("/api/"):
        return jsonify(error=msg), 500
    flash(msg, "error")
    return redirect(request.referrer or url_for("index"))


@app.after_request
def no_stale_html(resp):
    """Stop browsers caching pages whose <select> options embed database IDs;
    a stale page is exactly how you end up posting a part id that is gone."""
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_globals():
    return {"APP_NAME": APP_NAME, "today": today_str(),
            "all_vehicles": q("SELECT * FROM vehicles "
                              "ORDER BY name COLLATE NOCASE"),
            "auth_enabled": bool(PASSWORD)}


@app.before_request
def require_login():
    if not PASSWORD:
        return
    if request.endpoint in ("login", "static") or session.get("auth"):
        return
    return redirect(url_for("login", next=request.path))


def form(name, default=""):
    return (request.form.get(name) or default).strip()


def num(name):
    return to_float(request.form.get(name))


# ---------------------------------------------------------------- auth

@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return redirect(url_for("index"))
    if request.method == "POST":
        if form("password") == PASSWORD:
            session["auth"] = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("Wrong password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- vehicles

@app.route("/")
def index():
    v = q1("SELECT id FROM vehicles ORDER BY name COLLATE NOCASE LIMIT 1")
    if v:
        return redirect(url_for("dashboard", vid=v["id"]))
    return render_template("welcome.html")


def get_vehicle(vid):
    v = q1("SELECT * FROM vehicles WHERE id=?", (vid,))
    if not v:
        abort(404)
    return v


@app.route("/vehicle/save", methods=["POST"])
def vehicle_save():
    vid = request.form.get("id") or None
    name = form("name")
    if not name:
        flash("Vehicle name is required.", "error")
        return redirect(request.referrer or url_for("index"))
    try:
        odo = num("odometer_km")
        year = num("year")
    except ValueError:
        flash("Odometer and year must be numbers.", "error")
        return redirect(request.referrer or url_for("index"))
    vals = (name, form("vtype") or "Car", form("make"), form("model"),
            int(year) if year else None, form("vin"), form("plate"),
            odo or 0, today_str(), form("notes"))
    if vid:
        x("UPDATE vehicles SET name=?, vtype=?, make=?, model=?, year=?, "
          "vin=?, plate=?, odometer_km=?, odo_updated=?, notes=? WHERE id=?",
          vals + (vid,))
        flash("Vehicle updated.")
    else:
        vid = x("INSERT INTO vehicles(name,vtype,make,model,year,vin,plate,"
                "odometer_km,odo_updated,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                vals)
        if form("add_templates"):
            for n, ikm, imo in COMMON_ITEMS.get(form("vtype") or "Car", []):
                x("INSERT INTO maint_items(vehicle_id,name,interval_km,"
                  "interval_months,last_km,last_date) VALUES(?,?,?,?,?,?)",
                  (vid, n, ikm, imo, odo or 0, today_str()))
        flash("Vehicle added.")
    return redirect(url_for("dashboard", vid=vid))


@app.route("/vehicle/<int:vid>/delete", methods=["POST"])
def vehicle_delete(vid):
    v = get_vehicle(vid)
    for r in q("SELECT a.path FROM attachments a JOIN sessions s "
               "ON s.id = a.session_id WHERE s.vehicle_id=?", (vid,)):
        try:
            os.remove(r["path"])
        except OSError:
            pass
    x("DELETE FROM vehicles WHERE id=?", (vid,))
    flash(f"Deleted {v['name']} and its history.")
    return redirect(url_for("index"))


@app.route("/vehicle/<int:vid>/odometer", methods=["POST"])
def odometer(vid):
    v = get_vehicle(vid)
    try:
        odo = num("odometer_km")
    except ValueError:
        odo = None
    if odo is None:
        flash("Enter a valid km reading.", "error")
    else:
        x("UPDATE vehicles SET odometer_km=?, odo_updated=? WHERE id=?",
          (odo, today_str(), vid))
        flash(f"Odometer set to {fmt_km(odo)}.")
    return redirect(url_for("dashboard", vid=vid))


# ---------------------------------------------------------------- dashboard

@app.route("/vehicle/<int:vid>")
def dashboard(vid):
    v = get_vehicle(vid)
    items = q("SELECT * FROM maint_items WHERE vehicle_id=?", (vid,))
    rows = []
    for it in items:
        st = item_status(it, v["odometer_km"])
        rows.append((st["pct"] if st["pct"] is not None else -1, it, st))
    rows.sort(key=lambda r: r[0], reverse=True)
    overdue = sum(1 for p, _, _ in rows if p >= 1)
    return render_template("dashboard.html", v=v, rows=rows, overdue=overdue)


@app.route("/vehicle/<int:vid>/items/save", methods=["POST"])
def item_save(vid):
    get_vehicle(vid)
    iid = request.form.get("id") or None
    name = form("name")
    if not name:
        flash("Item name is required.", "error")
        return redirect(url_for("dashboard", vid=vid))
    try:
        ikm, imo = num("interval_km"), num("interval_months")
        lkm = num("last_km")
    except ValueError:
        flash("Intervals and km must be numbers.", "error")
        return redirect(url_for("dashboard", vid=vid))
    if not ikm and not imo:
        flash("Set at least one interval (km or months).", "error")
        return redirect(url_for("dashboard", vid=vid))
    ldt = form("last_date")
    if ldt and not parse_date(ldt):
        flash("Date must be YYYY-MM-DD.", "error")
        return redirect(url_for("dashboard", vid=vid))
    vals = (name, ikm, imo, lkm, ldt or None, form("notes"))
    if iid:
        x("UPDATE maint_items SET name=?, interval_km=?, interval_months=?, "
          "last_km=?, last_date=?, notes=? WHERE id=?", vals + (iid,))
        flash("Schedule item updated.")
    else:
        x("INSERT INTO maint_items(name,interval_km,interval_months,last_km,"
          "last_date,notes,vehicle_id) VALUES(?,?,?,?,?,?,?)", vals + (vid,))
        flash("Schedule item added.")
    return redirect(url_for("dashboard", vid=vid))


@app.route("/items/<int:iid>/delete", methods=["POST"])
def item_delete(iid):
    it = q1("SELECT vehicle_id FROM maint_items WHERE id=?", (iid,)) or abort(404)
    x("DELETE FROM maint_items WHERE id=?", (iid,))
    flash("Schedule item deleted.")
    return redirect(url_for("dashboard", vid=it["vehicle_id"]))


@app.route("/items/<int:iid>/done", methods=["POST"])
def item_done(iid):
    it = q1("SELECT * FROM maint_items WHERE id=?", (iid,)) or abort(404)
    v = get_vehicle(it["vehicle_id"])
    x("UPDATE maint_items SET last_km=?, last_date=? WHERE id=?",
      (v["odometer_km"], today_str(), iid))
    flash(f"'{it['name']}' marked done at {fmt_km(v['odometer_km'])}. "
          "Tip: log a session instead to keep parts and cost history.")
    return redirect(url_for("dashboard", vid=v["id"]))


@app.route("/vehicle/<int:vid>/templates", methods=["POST"])
def add_templates(vid):
    v = get_vehicle(vid)
    tpl = COMMON_ITEMS.get(v["vtype"], [])
    for n, ikm, imo in tpl:
        x("INSERT INTO maint_items(vehicle_id,name,interval_km,"
          "interval_months,last_km,last_date) VALUES(?,?,?,?,?,?)",
          (vid, n, ikm, imo, v["odometer_km"], today_str()))
    flash(f"Added {len(tpl)} template items (baseline: today at current "
          "odometer -- edit each to match reality).")
    return redirect(url_for("dashboard", vid=vid))


# ---------------------------------------------------------------- sessions

def session_query(vid):
    sql = "SELECT * FROM sessions WHERE vehicle_id=?"
    params = [vid]
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term}%"
        sql += (" AND (title LIKE ? OR notes LIKE ? OR id IN ("
                "SELECT sp.session_id FROM session_parts sp "
                "JOIN parts p ON p.id = sp.part_id "
                "LEFT JOIN suppliers su ON su.id = p.supplier_id "
                "WHERE p.name LIKE ? OR p.part_number LIKE ? "
                "OR IFNULL(su.name,'') LIKE ?))")
        params += [like] * 5
    d1 = (request.args.get("from") or "").strip()
    d2 = (request.args.get("to") or "").strip()
    if parse_date(d1):
        sql += " AND date >= ?"
        params.append(d1)
    if parse_date(d2):
        sql += " AND date <= ?"
        params.append(d2)
    sql += " ORDER BY date DESC, id DESC"
    return q(sql, params)


@app.route("/vehicle/<int:vid>/sessions")
def sessions_list(vid):
    v = get_vehicle(vid)
    rows = []
    for s in session_query(vid):
        pc = q1("SELECT COALESCE(SUM(qty*unit_price),0) c FROM session_parts "
                "WHERE session_id=?", (s["id"],))["c"]
        na = q1("SELECT COUNT(*) c FROM attachments WHERE session_id=?",
                (s["id"],))["c"]
        rows.append((s, pc, pc + (s["labor_cost"] or 0), na))
    return render_template("sessions.html", v=v, rows=rows)


def session_form_ctx(v, s=None):
    parts = q("SELECT p.*, su.name sup FROM parts p LEFT JOIN suppliers su "
              "ON su.id = p.supplier_id ORDER BY p.name COLLATE NOCASE")
    suppliers = q("SELECT * FROM suppliers ORDER BY name COLLATE NOCASE")
    items = q("SELECT * FROM maint_items WHERE vehicle_id=? "
              "ORDER BY name COLLATE NOCASE", (v["id"],))
    lines, done, attach = [], set(), []
    if s:
        lines = q("SELECT sp.* FROM session_parts sp WHERE sp.session_id=?",
                  (s["id"],))
        done = {r["item_id"] for r in q(
            "SELECT item_id FROM session_items WHERE session_id=?", (s["id"],))}
        attach = q("SELECT * FROM attachments WHERE session_id=?", (s["id"],))
    return dict(v=v, s=s, parts=parts, suppliers=suppliers, items=items,
                lines=lines, done=done, attach=attach)


@app.route("/vehicle/<int:vid>/sessions/new")
def session_new(vid):
    return render_template("session_form.html",
                           **session_form_ctx(get_vehicle(vid)))


@app.route("/sessions/<int:sid>/edit")
def session_edit(sid):
    s = q1("SELECT * FROM sessions WHERE id=?", (sid,)) or abort(404)
    return render_template("session_form.html",
                           **session_form_ctx(get_vehicle(s["vehicle_id"]), s))


@app.route("/vehicle/<int:vid>/sessions/save", methods=["POST"])
def session_save(vid):
    v = get_vehicle(vid)
    sid = request.form.get("id") or None
    d = form("date")
    if not parse_date(d):
        flash("Date must be YYYY-MM-DD.", "error")
        return redirect(request.referrer or url_for("sessions_list", vid=vid))
    try:
        odo = num("odometer_km")
        labor = num("labor_cost") or 0.0
    except ValueError:
        flash("Odometer and labor cost must be numbers.", "error")
        return redirect(request.referrer or url_for("sessions_list", vid=vid))
    if odo is None:
        flash("Odometer is required.", "error")
        return redirect(request.referrer or url_for("sessions_list", vid=vid))
    vals = (d, odo, form("title"), form("notes"), labor)
    if sid:
        x("UPDATE sessions SET date=?, odometer_km=?, title=?, notes=?, "
          "labor_cost=? WHERE id=?", vals + (sid,))
        x("DELETE FROM session_parts WHERE session_id=?", (sid,))
        x("DELETE FROM session_items WHERE session_id=?", (sid,))
        flash("Session updated.")
    else:
        sid = x("INSERT INTO sessions(date,odometer_km,title,notes,labor_cost,"
                "vehicle_id) VALUES(?,?,?,?,?,?)", vals + (vid,))
        flash("Session logged.")

    # parts lines
    pids = request.form.getlist("line_part")
    qtys = request.form.getlist("line_qty")
    prices = request.form.getlist("line_price")
    skipped = []
    for pid, qty, price in zip(pids, qtys, prices):
        if not pid:
            continue
        if not q1("SELECT id FROM parts WHERE id=?", (pid,)):
            skipped.append(f"part #{pid}")      # stale page / deleted part
            continue
        try:
            qv = to_float(qty) or 1.0
            pv = to_float(price) or 0.0
        except ValueError:
            qv, pv = 1.0, 0.0
        x("INSERT INTO session_parts(session_id,part_id,qty,unit_price) "
          "VALUES(?,?,?,?)", (sid, pid, qv, pv))

    # covered schedule items (records; life bars derive from these)
    for iid in request.form.getlist("items"):
        if not q1("SELECT id FROM maint_items WHERE id=? AND vehicle_id=?",
                  (iid, vid)):
            skipped.append(f"schedule item #{iid}")
            continue
        x("INSERT INTO session_items(session_id,item_id) VALUES(?,?)",
          (sid, iid))
    if skipped:
        flash("Saved, but skipped " + ", ".join(skipped) +
              " (no longer in the database -- reload the page).", "error")

    # attachments: delete flagged, save uploads
    for aid in request.form.getlist("delete_att"):
        r = q1("SELECT * FROM attachments WHERE id=? AND session_id=?",
               (aid, sid))
        if r:
            x("DELETE FROM attachments WHERE id=?", (aid,))
            try:
                os.remove(r["path"])
            except OSError:
                pass
    dest_dir = os.path.join(ATTACH_DIR, f"session_{sid}")
    for f in request.files.getlist("attachments"):
        if not f or not f.filename:
            continue
        fname = secure_filename(f.filename) or "file"
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, fname)
        base, ext = os.path.splitext(fname)
        n = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{base}_{n}{ext}")
            n += 1
        f.save(dest)
        x("INSERT INTO attachments(session_id,filename,path) VALUES(?,?,?)",
          (sid, os.path.basename(dest), dest))

    if odo > (v["odometer_km"] or 0):
        x("UPDATE vehicles SET odometer_km=?, odo_updated=? WHERE id=?",
          (odo, today_str(), vid))
    return redirect(url_for("sessions_list", vid=vid))


@app.route("/sessions/<int:sid>/delete", methods=["POST"])
def session_delete(sid):
    s = q1("SELECT * FROM sessions WHERE id=?", (sid,)) or abort(404)
    for r in q("SELECT path FROM attachments WHERE session_id=?", (sid,)):
        try:
            os.remove(r["path"])
        except OSError:
            pass
    x("DELETE FROM sessions WHERE id=?", (sid,))
    flash("Session deleted; life bars recalculated.")
    return redirect(url_for("sessions_list", vid=s["vehicle_id"]))


@app.route("/attachments/<int:aid>")
def attachment(aid):
    r = q1("SELECT * FROM attachments WHERE id=?", (aid,)) or abort(404)
    d, fname = os.path.split(r["path"])
    return send_from_directory(d, fname)


# ---------------------------------------------------------------- fuel

@app.route("/vehicle/<int:vid>/fuel")
def fuel_list(vid):
    v = get_vehicle(vid)
    sql = "SELECT * FROM fuel_log WHERE vehicle_id=?"
    params = [vid]
    d1 = (request.args.get("from") or "").strip()
    d2 = (request.args.get("to") or "").strip()
    if parse_date(d1):
        sql += " AND date >= ?"
        params.append(d1)
    if parse_date(d2):
        sql += " AND date <= ?"
        params.append(d2)
    sql += " ORDER BY odometer_km DESC, date DESC"
    rows = q(sql, params)
    segs = fuel_segments(vid)
    per = {fid: l / d * 100 for fid, (d, l, _) in segs.items()}
    km, lit = fuel_stats(vid)
    avg = (lit / km * 100) if km > 0 else None
    return render_template("fuel.html", v=v, rows=rows, km=km, lit=lit, avg=avg,
                           per=per, segs=segs, reset_km=fuel_reset_km(vid))


@app.route("/vehicle/<int:vid>/fuel/reset", methods=["POST"])
def fuel_reset(vid):
    get_vehicle(vid)
    if request.form.get("undo"):
        x("DELETE FROM fuel_resets WHERE vehicle_id=?", (vid,))
        flash("Average reset undone; all fill-ups count again.")
        return redirect(url_for("fuel_list", vid=vid))
    last = q1("SELECT MAX(odometer_km) o FROM fuel_log WHERE vehicle_id=? AND full_tank=1",
              (vid,))["o"]
    if last is None:
        flash("Log a full-tank fill-up first; the average restarts from there.", "error")
        return redirect(url_for("fuel_list", vid=vid))
    x("INSERT INTO fuel_resets(vehicle_id, odometer_km) VALUES(?,?) "
      "ON CONFLICT(vehicle_id) DO UPDATE SET odometer_km=excluded.odometer_km",
      (vid, last))
    flash(f"Average reset. It restarts from the full tank at {last:.0f} km.")
    return redirect(url_for("fuel_list", vid=vid))


@app.route("/vehicle/<int:vid>/fuel/save", methods=["POST"])
def fuel_save(vid):
    v = get_vehicle(vid)
    fid = request.form.get("id") or None
    d = form("date")
    if not parse_date(d):
        flash("Date must be YYYY-MM-DD.", "error")
        return redirect(url_for("fuel_list", vid=vid))
    try:
        odo, lit, cost = num("odometer_km"), num("liters"), num("total_cost")
    except ValueError:
        flash("Odometer, liters and cost must be numbers.", "error")
        return redirect(url_for("fuel_list", vid=vid))
    if odo is None or lit is None:
        flash("Odometer and liters are required.", "error")
        return redirect(url_for("fuel_list", vid=vid))
    full = 1 if form("full_tank") else 0
    vals = (d, odo, lit, cost, full, form("notes"))
    if fid:
        x("UPDATE fuel_log SET date=?, odometer_km=?, liters=?, total_cost=?, "
          "full_tank=?, notes=? WHERE id=?", vals + (fid,))
        flash("Fill-up updated.")
    else:
        x("INSERT INTO fuel_log(date,odometer_km,liters,total_cost,full_tank,"
          "notes,vehicle_id) VALUES(?,?,?,?,?,?,?)", vals + (vid,))
        flash("Fill-up logged.")
    if odo > (v["odometer_km"] or 0):
        x("UPDATE vehicles SET odometer_km=?, odo_updated=? WHERE id=?",
          (odo, today_str(), vid))
    return redirect(url_for("fuel_list", vid=vid))


@app.route("/fuel/<int:fid>/delete", methods=["POST"])
def fuel_delete(fid):
    r = q1("SELECT vehicle_id FROM fuel_log WHERE id=?", (fid,)) or abort(404)
    x("DELETE FROM fuel_log WHERE id=?", (fid,))
    flash("Fill-up deleted.")
    return redirect(url_for("fuel_list", vid=r["vehicle_id"]))


# ---------------------------------------------------------------- catalog

@app.route("/catalog")
def catalog():
    pterm = (request.args.get("pq") or "").strip()
    psql = ("SELECT p.*, s.name sup FROM parts p LEFT JOIN suppliers s "
            "ON s.id = p.supplier_id")
    pparams = []
    if pterm:
        like = f"%{pterm}%"
        psql += (" WHERE p.name LIKE ? OR IFNULL(p.part_number,'') LIKE ? "
                 "OR IFNULL(s.name,'') LIKE ?")
        pparams = [like, like, like]
    psql += " ORDER BY p.name COLLATE NOCASE"
    sterm = (request.args.get("sq") or "").strip()
    ssql = "SELECT * FROM suppliers"
    sparams = []
    if sterm:
        like = f"%{sterm}%"
        ssql += (" WHERE name LIKE ? OR IFNULL(email,'') LIKE ? "
                 "OR IFNULL(phone,'') LIKE ?")
        sparams = [like, like, like]
    ssql += " ORDER BY name COLLATE NOCASE"
    return render_template("catalog.html", parts=q(psql, pparams),
                           suppliers=q(ssql, sparams), pq=pterm, sq=sterm,
                           all_suppliers=q("SELECT id, name FROM suppliers "
                                           "ORDER BY name COLLATE NOCASE"))


def save_part_from_form():
    pid = request.form.get("id") or None
    name = form("name")
    if not name:
        return None, "Part name is required."
    try:
        price = num("unit_price")
    except ValueError:
        return None, "Unit price must be a number."
    sup = request.form.get("supplier_id") or None
    if sup and not q1("SELECT id FROM suppliers WHERE id=?", (sup,)):
        return None, ("That supplier no longer exists. Reload the page and "
                      "pick or create the supplier again.")
    vals = (name, form("part_number"), sup, price, form("notes"))
    if pid:
        x("UPDATE parts SET name=?, part_number=?, supplier_id=?, "
          "unit_price=?, notes=? WHERE id=?", vals + (pid,))
    else:
        pid = x("INSERT INTO parts(name,part_number,supplier_id,unit_price,"
                "notes) VALUES(?,?,?,?,?)", vals)
    return int(pid), None


def save_supplier_from_form():
    sid = request.form.get("id") or None
    name = form("name")
    if not name:
        return None, "Supplier name is required."
    vals = (name, form("website"), form("email"), form("phone"), form("notes"))
    if sid:
        x("UPDATE suppliers SET name=?, website=?, email=?, phone=?, notes=? "
          "WHERE id=?", vals + (sid,))
    else:
        sid = x("INSERT INTO suppliers(name,website,email,phone,notes) "
                "VALUES(?,?,?,?,?)", vals)
    return int(sid), None


@app.route("/parts/save", methods=["POST"])
def part_save():
    _, err = save_part_from_form()
    flash(err or "Part saved.", "error" if err else "message")
    return redirect(request.referrer or url_for("catalog"))


@app.route("/parts/<int:pid>/delete", methods=["POST"])
def part_delete(pid):
    x("DELETE FROM parts WHERE id=?", (pid,))
    flash("Part deleted (any session lines using it were removed).")
    return redirect(url_for("catalog"))


@app.route("/suppliers/save", methods=["POST"])
def supplier_save():
    _, err = save_supplier_from_form()
    flash(err or "Supplier saved.", "error" if err else "message")
    return redirect(request.referrer or url_for("catalog"))


@app.route("/suppliers/<int:sid>/delete", methods=["POST"])
def supplier_delete(sid):
    x("DELETE FROM suppliers WHERE id=?", (sid,))
    flash("Supplier deleted; parts keep existing but lose the link.")
    return redirect(url_for("catalog"))


# JSON endpoints for inline creation inside the session form
@app.route("/api/parts", methods=["POST"])
def api_parts():
    pid, err = save_part_from_form()
    if err:
        return jsonify(error=err), 400
    p = q1("SELECT p.*, s.name sup FROM parts p LEFT JOIN suppliers s "
           "ON s.id = p.supplier_id WHERE p.id=?", (pid,))
    label = p["name"] + (f" [{p['part_number']}]" if p["part_number"] else "")
    return jsonify(id=pid, label=label, price=p["unit_price"])


@app.route("/api/suppliers", methods=["POST"])
def api_suppliers():
    sid, err = save_supplier_from_form()
    if err:
        return jsonify(error=err), 400
    s = q1("SELECT * FROM suppliers WHERE id=?", (sid,))
    return jsonify(id=sid, name=s["name"])


# ---------------------------------------------------------------- job guides
#
# A job guide (JG) is a step-by-step procedure imported as a "jg-bundle/1"
# JSON file: input conditions, numbered steps with WARNING/CAUTION/NOTE,
# one figure per paragraph (SVG inlined, or a 3D view onto a jg-model/1
# model rendered by static/jg3d.js). A run is one time you do the job
# on a vehicle; ticks and record fields live on the run, and finishing it
# logs a maintenance session.

JG_REQUIRED = ("to", "title", "input", "figures", "paras")
_SVG_BAD = [
    (re.compile(r"<script\b.*?</script\s*>", re.I | re.S), ""),
    (re.compile(r"<foreignObject\b.*?</foreignObject\s*>", re.I | re.S), ""),
    (re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I), ""),
    (re.compile(r"(href\s*=\s*[\"']?)\s*javascript:", re.I), r"\1#"),
]


def clean_svg(svg):
    """Imported figures are shown inline; strip anything executable."""
    for rx, rep in _SVG_BAD:
        svg = rx.sub(rep, svg)
    return svg


def parse_bundle(raw):
    try:
        jg = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, "That file is not valid JSON."
    if not isinstance(jg, dict) or jg.get("format") != "jg-bundle/1":
        return None, ("Not a job guide bundle. Build it with "
                      "to/jg-web/tools/bundle_jg.py (format jg-bundle/1).")
    missing = [k for k in JG_REQUIRED if k not in jg]
    if missing:
        return None, "Bundle is missing: " + ", ".join(missing)
    models = jg.setdefault("models", {})
    if not isinstance(models, dict) or any(
            not isinstance(m, dict) or m.get("format") != "jg-model/1"
            or not isinstance(m.get("parts"), list) for m in models.values()):
        return None, "Bundle models must be jg-model/1 objects with a parts list."
    for fid, f in jg["figures"].items():
        view = f.get("view")
        if view is not None:
            # 3D figure: a view (plain data) onto one of the bundle's models
            if not isinstance(view, dict) or view.get("model") not in models:
                return None, f"Figure {fid}: its 3D view names no model in the bundle."
        else:
            f["svg"] = clean_svg(f.get("svg", ""))
    return jg, None


def jg_steps(jg):
    return [s["n"] for p in jg["paras"] for s in p["steps"]]


def load_run(rid):
    r = q1("SELECT r.*, g.data, g.to_number, g.change cur_change FROM job_runs r "
           "JOIN job_guides g ON g.id = r.guide_id WHERE r.id=?", (rid,))
    if not r:
        abort(404)
    return r, json.loads(r["data"]), json.loads(r["steps"]), json.loads(r["records"])


@app.route("/jobs")
def jobs_library():
    guides = q("SELECT g.*, (SELECT COUNT(*) FROM job_runs r WHERE r.guide_id=g.id) "
               "runs FROM job_guides g ORDER BY to_number")
    return render_template("job_library.html", guides=guides)


@app.route("/jobs/import", methods=["POST"])
def jobs_import():
    f = request.files.get("bundle")
    if not f or not f.filename:
        flash("Choose a .jgb.json bundle to import.", "error")
        return redirect(url_for("jobs_library"))
    jg, err = parse_bundle(f.read())
    if err:
        flash(err, "error")
        return redirect(url_for("jobs_library"))
    old = q1("SELECT * FROM job_guides WHERE to_number=?", (jg["to"],))
    vals = (jg["title"], int(jg.get("change") or 0), json.dumps(jg), today_str())
    if old:
        x("UPDATE job_guides SET title=?, change=?, data=?, imported=? WHERE id=?",
          vals + (old["id"],))
        flash(f"{jg['to']} updated to change {vals[1]}. Runs in progress keep "
              "their ticks; re-check them against the new text.")
    else:
        x("INSERT INTO job_guides(title,change,data,imported,to_number) "
          "VALUES(?,?,?,?,?)", vals + (jg["to"],))
        flash(f"{jg['to']} imported.")
    return redirect(url_for("jobs_library"))


@app.route("/jobs/<int:gid>/delete", methods=["POST"])
def job_guide_delete(gid):
    g_ = q1("SELECT * FROM job_guides WHERE id=?", (gid,)) or abort(404)
    x("DELETE FROM job_guides WHERE id=?", (gid,))
    flash(f"Deleted {g_['to_number']} and its runs (logged sessions are kept).")
    return redirect(url_for("jobs_library"))


@app.route("/vehicle/<int:vid>/jobs")
def jobs_list(vid):
    v = get_vehicle(vid)
    runs = q("SELECT r.*, g.to_number, g.title, g.data FROM job_runs r "
             "JOIN job_guides g ON g.id = r.guide_id WHERE r.vehicle_id=? "
             "ORDER BY r.finished IS NOT NULL, r.started DESC, r.id DESC", (vid,))
    rows = [(r, len(json.loads(r["steps"])), len(jg_steps(json.loads(r["data"]))))
            for r in runs]
    guides = q("SELECT id, to_number, title FROM job_guides ORDER BY to_number")
    return render_template("jobs.html", v=v, rows=rows, guides=guides)


@app.route("/vehicle/<int:vid>/jobs/start", methods=["POST"])
def job_start(vid):
    get_vehicle(vid)
    g_ = q1("SELECT * FROM job_guides WHERE id=?",
            (request.form.get("guide_id"),))
    if not g_:
        flash("Pick a job guide to start (import one under Job guides first).",
              "error")
        return redirect(url_for("jobs_list", vid=vid))
    rid = x("INSERT INTO job_runs(vehicle_id,guide_id,guide_change,started) "
            "VALUES(?,?,?,?)", (vid, g_["id"], g_["change"], today_str()))
    return redirect(url_for("job_run", rid=rid))


@app.route("/runs/<int:rid>")
def job_run(rid):
    r, jg, done, recs = load_run(rid)
    v = get_vehicle(r["vehicle_id"])
    figno = {fid: i + 1 for i, fid in enumerate(jg["figures"])}
    return render_template("job_run.html", v=v, r=r, jg=jg, done=set(done),
                           recs=recs, figno=figno, total=len(jg_steps(jg)),
                           recnames=dict(jg.get("records", [])))


@app.route("/api/runs/<int:rid>/step", methods=["POST"])
def api_run_step(rid):
    r, jg, done, _ = load_run(rid)
    step = str((request.get_json(silent=True) or {}).get("step", ""))
    if step not in jg_steps(jg):
        return jsonify(error=f"No step {step!r} in {r['to_number']}."), 400
    want = bool((request.get_json(silent=True) or {}).get("done"))
    done = [s for s in done if s != step] + ([step] if want else [])
    x("UPDATE job_runs SET steps=? WHERE id=?", (json.dumps(done), rid))
    return jsonify(done=len(done))


@app.route("/api/runs/<int:rid>/record", methods=["POST"])
def api_run_record(rid):
    r, jg, _, recs = load_run(rid)
    body = request.get_json(silent=True) or {}
    field = str(body.get("field", ""))
    if field not in dict(jg.get("records", [])):
        return jsonify(error=f"No record field {field!r} in {r['to_number']}."), 400
    recs[field] = str(body.get("value", ""))[:4000]
    x("UPDATE job_runs SET records=? WHERE id=?", (json.dumps(recs), rid))
    return jsonify(ok=True)


@app.route("/runs/<int:rid>/finish", methods=["POST"])
def job_finish(rid):
    r, jg, done, recs = load_run(rid)
    v = get_vehicle(r["vehicle_id"])
    if r["session_id"]:
        return redirect(url_for("session_edit", sid=r["session_id"]))
    try:
        odo = num("odometer_km")
    except ValueError:
        odo = None
    if odo is None:
        flash("Enter the odometer reading to log the session.", "error")
        return redirect(url_for("job_run", rid=rid))
    total = len(jg_steps(jg))
    lines = [f"Job guide {jg['to']} change {r['guide_change']}: "
             f"{len(done)} of {total} steps signed off."]
    for fid, label in jg.get("records", []):
        if recs.get(fid):
            lines.append(f"{fid} {label}: {recs[fid]}")
    sid = x("INSERT INTO sessions(date,odometer_km,title,notes,labor_cost,"
            "vehicle_id) VALUES(?,?,?,?,?,?)",
            (today_str(), odo, jg.get("short") or jg["title"].title(),
             "\n".join(lines), 0, v["id"]))
    x("UPDATE job_runs SET finished=?, session_id=? WHERE id=?",
      (today_str(), sid, rid))
    if odo > (v["odometer_km"] or 0):
        x("UPDATE vehicles SET odometer_km=?, odo_updated=? WHERE id=?",
          (odo, today_str(), v["id"]))
    flash("Job finished and logged as a session. Add the parts, cost and "
          "schedule items it covered.")
    return redirect(url_for("session_edit", sid=sid))


@app.route("/runs/<int:rid>/delete", methods=["POST"])
def job_run_delete(rid):
    r = q1("SELECT vehicle_id FROM job_runs WHERE id=?", (rid,)) or abort(404)
    x("DELETE FROM job_runs WHERE id=?", (rid,))
    flash("Job run deleted (a session it logged is kept).")
    return redirect(url_for("jobs_list", vid=r["vehicle_id"]))


# ---------------------------------------------------------------- wiring diagrams
#
# A wiring set is imported as a "wd-bundle/1" file (gzip or plain JSON, built by
# to/jg-web/tools/bundle_wd.py): the wd-model/1 master (units, connectors with
# pins and nets, harnesses with wires, systems = one WD each), the pin-to-pin
# diagram SVG of every WD, and per-unit board detail (KiCad schematic sheets,
# perfboard layout, PCB, BOM). Units without a board are modules; their page is
# a pinout card drawn from the model.

WD_MODEL_KEYS = ("units", "connectors", "harnesses", "systems")


def parse_wd_bundle(raw):
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            return None, "That file is a damaged gzip archive."
    try:
        b = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, "That file is not valid JSON."
    if not isinstance(b, dict) or b.get("format") != "wd-bundle/1":
        return None, ("Not a wiring bundle. Build it with "
                      "to/jg-web/tools/bundle_wd.py (format wd-bundle/1).")
    m = b.get("model")
    if not isinstance(m, dict) or any(not isinstance(m.get(k), list) for k in WD_MODEL_KEYS):
        return None, "Bundle model must have units, connectors, harnesses and systems."
    if not isinstance(b.get("figs"), dict) or not b.get("set"):
        return None, "Bundle is missing its diagrams or its set name."
    b["figs"] = {k: clean_svg(v) for k, v in b["figs"].items() if isinstance(v, str)}
    boards = b.get("boards") or {}
    for bd in boards.values():
        for key in ("sheets", "layout", "pcb"):
            items = []
            for it in bd.get(key) or []:
                if it.get("svg"):
                    it["svg"] = clean_svg(it["svg"])
                elif not str(it.get("img", "")).startswith("data:image/png;base64,"):
                    continue
                items.append(it)
            bd[key] = items
    b["boards"] = boards
    oem = b.get("oem")
    if oem is not None:
        if not isinstance(oem, dict) or (oem.get("model") or {}).get("format") != "oem-model/1":
            return None, "Bundle factory wiring must be an oem-model/1 model."
        oem["figs"] = {k: clean_svg(v) for k, v in (oem.get("figs") or {}).items() if isinstance(v, str)}
    return b, None


class Wiring:
    """Index over one imported wd-model/1 master."""

    def __init__(self, row):
        self.row = row
        d = json.loads(row["data"])
        self.M, self.figs = d["model"], d["figs"]
        self.oem = OEM(d["oem"]) if d.get("oem") else None
        self.unit = {u["id"]: u for u in self.M["units"]}
        self.conn = {c["id"]: c for c in self.M["connectors"]}
        self.sys = {s["id"]: s for s in self.M["systems"]}
        self.pin_wires, self.wire_har = {}, {}
        for h in self.M["harnesses"]:
            for w in h["wires"]:
                self.wire_har[w["id"]] = h
                for e in (w["a"], w["b"]):
                    self.pin_wires.setdefault(e, []).append(w)

    def c_of(self, key):
        return self.conn.get(key.split(".", 1)[0])

    def label(self, key):
        c = self.c_of(key)
        return f'{c["unit"]} {c["ref"]}.{key.split(".", 1)[1]}' if c else key

    def pins(self, c):
        """Rows of (pin, far ends [(wire, harness, far key)]) for a connector."""
        out = []
        for p in c["pins"]:
            k = f'{c["id"]}.{p["pin"]}'
            far = [(w, self.wire_har[w["id"]], w["b"] if w["a"] == k else w["a"])
                   for w in self.pin_wires.get(k, [])]
            out.append((p, k, far))
        return out

    def unit_systems(self, uid):
        """WDs that draw this unit in full."""
        return [s for s in self.M["systems"] if uid in (s.get("dia") or s["units"])]


def module_card(W, uid):
    """Pinout card for an off-the-shelf module: body with its pin headers, our net on
    each pin, and the wire and far end leaving each pin."""
    conns = [c for c in W.M["connectors"] if c["unit"] == uid]
    rows = []
    for c in conns:
        rows.append(("head", c))
        rows += [("pin", r) for r in W.pins(c)]
    pitch, x0, bw = 26, 30, 230
    h = 90 + pitch * len(rows)
    farw = max([len(f"{w['id']} {w['colour']} → {W.label(k)}") for _, (_, _, fr) in
                [r for r in rows if r[0] == "pin"] for (w, _, k) in fr] + [20]) * 7.2
    width = int(x0 + bw + 60 + farw + 20)
    u = W.unit[uid]
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {h}" width="{width}" height="{h}" '
         f'font-family="Arial, Helvetica, sans-serif" class="wdcard">',
         f'<rect width="{width}" height="{h}" fill="#ffffff"/>',
         f'<rect x="{x0}" y="20" width="{bw}" height="{h - 40}" rx="8" fill="#2a5d3a" stroke="#111417" stroke-width="2"/>',
         f'<text x="{x0 + 12}" y="42" font-size="14" font-weight="bold" fill="#ffffff">{escape(u.get("short") or u["name"])}</text>']
    y = 60
    for kind, r in rows:
        if kind == "head":
            o.append(f'<text x="{x0 + 12}" y="{y + 17}" font-size="11" fill="#d7e8da">{escape(r["ref"])} · {escape(r["part"][:34])}</text>')
        else:
            p, k, far = r
            cy = y + pitch / 2
            o.append(f'<rect x="{x0 + bw - 22}" y="{cy - 7}" width="14" height="14" fill="#d4af37" stroke="#7a6214"/>'
                     f'<text x="{x0 + 12}" y="{cy + 4}" font-size="12" font-family="Consolas, monospace" font-weight="bold" fill="#ffffff">{escape(p["pin"])}</text>'
                     f'<text x="{x0 + 70}" y="{cy + 4}" font-size="11" font-family="Consolas, monospace" fill="#ffffff">{escape(p["net"])}</text>')
            for i, (w, hh, fk) in enumerate(far[:1]):
                o.append(f'<line x1="{x0 + bw - 8}" y1="{cy}" x2="{x0 + bw + 50}" y2="{cy}" stroke="#111417" stroke-width="3"/>'
                         f'<text x="{x0 + bw + 58}" y="{cy + 4}" font-size="11" font-family="Consolas, monospace" fill="#111417">'
                         f'{escape(w["id"])} {escape(w["colour"])} → {escape(W.label(fk))}{" (+%d)" % (len(far) - 1) if len(far) > 1 else ""}</text>')
        y += pitch
    o.append("</svg>")
    return "\n".join(o)


def get_wiring(sid):
    row = q1("SELECT * FROM wiring_sets WHERE id=?", (sid,)) or abort(404)
    return Wiring(row)


@app.route("/wiring")
def wiring_library():
    sets = q("SELECT id, set_key, title, change, date, imported, data FROM wiring_sets ORDER BY set_key")
    rows = [(s, json.loads(s["data"])["model"]["systems"]) for s in sets]
    return render_template("wiring_library.html", rows=rows)


@app.route("/wiring/import", methods=["POST"])
def wiring_import():
    f = request.files.get("bundle")
    if not f or not f.filename:
        flash("Choose a .wdb.json.gz bundle to import.", "error")
        return redirect(url_for("wiring_library"))
    b, err = parse_wd_bundle(f.read())
    if err:
        flash(err, "error")
        return redirect(url_for("wiring_library"))
    data = json.dumps({"model": b["model"], "figs": b["figs"], "oem": b.get("oem")})
    vals = (b.get("title") or b["set"], int(b.get("change") or 0), b.get("date", ""), data, today_str())
    old = q1("SELECT id FROM wiring_sets WHERE set_key=?", (b["set"],))
    if old:
        x("UPDATE wiring_sets SET title=?, change=?, date=?, data=?, imported=? WHERE id=?", vals + (old["id"],))
        sid = old["id"]
        x("DELETE FROM wiring_boards WHERE set_id=?", (sid,))
    else:
        sid = x("INSERT INTO wiring_sets(title,change,date,data,imported,set_key) VALUES(?,?,?,?,?,?)",
                vals + (b["set"],))
    for uid, bd in b["boards"].items():
        x("INSERT INTO wiring_boards(set_id,unit,data) VALUES(?,?,?)", (sid, uid, json.dumps(bd)))
    flash(f"Wiring {b['set']} change {vals[1]} imported: {len(b['figs'])} diagrams, "
          f"{sum(1 for v in b['boards'].values() if v.get('status') == 'ok')} boards with detail.")
    return redirect(url_for("wiring_library"))


# ---- factory (OEM) wiring: transcribed from the workshop manual, section Z

MAZDA_COLOURS = {"B": "#1b1b1b", "W": "#ffffff", "R": "#d0261d", "G": "#2e9a4a", "L": "#2f63d6", "Y": "#f2c400", "BR": "#7a4a22",
                 "O": "#f08a24", "P": "#ef7fb8", "V": "#8a3fc4", "PU": "#8a3fc4", "GY": "#8c8c8c", "LG": "#8fd18b",
                 "LB": "#8ecae6", "DG": "#1e5f2e", "DL": "#1d2f80", "T": "#c9a26b", "N": "#e8dcc0"}
OEM_REF = re.compile(r"^([A-Z](?:-\d[a-z]?)?)([①-⑳㉑-㉟㊱-㊿])")


def mazda_colour(code):
    """(base, stripe) hex colours for a Mazda wire code like 'W/R'; None when unknown."""
    parts = str(code).strip("()[]<>{}").split("/")
    base = MAZDA_COLOURS.get(parts[0])
    return base, (MAZDA_COLOURS.get(parts[1]) if base and len(parts) > 1 else None)


class OEM:
    """Index over the factory wiring model (oem-model/1)."""

    def __init__(self, d):
        self.M, self.figs = d["model"], d.get("figs") or {}
        self.conn = {c["id"]: c for c in self.M["connectors"]}
        self.sys = {s["id"]: s for s in self.M["systems"]}
        self.ends = {}                # "cid.pin" -> [(system, net, other end)]
        for S in self.M["systems"]:
            local = {self.conn[c]["code"] if c in self.conn else c: c for c in S.get("conns", [])}
            for net in S.get("nets", []):
                for seg in net["segs"]:
                    a, b = (self.resolve(local, e) for e in seg[:2])
                    col = seg[2] if len(seg) > 2 and seg[2] else net.get("colour", "")
                    har = seg[3] if len(seg) > 3 else ""
                    for x, y in ((a, b), (b, a)):
                        self.ends.setdefault(x, []).append({"sys": S["id"], "net": net["name"], "to": y,
                                                            "colour": col, "harness": har, "note": net.get("note", "")})

    @staticmethod
    def resolve(local, e):
        if "." in e and not OEM_REF.match(e) and not e.startswith(("GND", "BAT")):
            code, pin = e.split(".", 1)
            return f"{local.get(code, code)}.{pin}"
        return e

    def label(self, e):
        m = OEM_REF.match(e)
        if m:
            return f"system {m.group(1)} {m.group(2)}"
        if e.startswith("GND"):
            return f"ground {e[3:]}"
        if e.startswith("BAT."):
            return "battery +"
        cid, pin = e.split(".", 1)
        return f"{self.conn[cid]['code'] if cid in self.conn else cid} · {pin}"

    def target(self, e):
        """(kind, id) link target for an end: ('c', connector id) or ('s', system id)."""
        m = OEM_REF.match(e)
        if m:
            sid = m.group(1)
            sid = sid if sid in self.sys else next((s for s in self.sys if s.startswith(sid)), None)
            return ("s", sid) if sid else (None, None)
        if e.startswith(("GND", "BAT")):
            return ("s", "Y") if e.startswith("GND") else (None, None)
        cid = e.split(".", 1)[0]
        return ("c", cid) if cid in self.conn else (None, None)

    def pins(self, cid):
        c = self.conn[cid]
        return [(p, self.ends.get(f"{cid}.{p['pin']}", [])) for p in c.get("pins", [])]


def oem_face_svg(c):
    """Face view(s) of a factory connector as printed in the manual (harness side), cells coloured by wire."""
    faces = c.get("faces") or {}
    pins = c.get("pins") if isinstance(c.get("pins"), dict) else {}
    if not faces:
        return ""
    cw, ch, gap, out, x0 = 64, 40, 34, [], 10
    blocks = []
    for side, rows in faces.items():
        w = max(len(r) for r in rows) * cw
        blocks.append((side, rows, w))
    W = sum(b[2] for b in blocks) + gap * (len(blocks) - 1) + 20
    H = max(len(b[1]) for b in blocks) * ch + 48
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" class="oemface" '
               f'font-family="Arial, Helvetica, sans-serif">')
    out.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    x = x0
    for side, rows, w in blocks:
        out.append(f'<text x="{x}" y="16" font-size="12" font-weight="bold" fill="#111417">{escape(side)}</text>')
        for r, row in enumerate(rows):
            for k, t in enumerate(row):
                cx, cy = x + k * cw, 26 + r * ch
                if t == "":
                    continue
                if t == "X":
                    out.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" fill="#f1f3f3" stroke="#6b7477"/>'
                               f'<path d="M{cx},{cy} L{cx + cw},{cy + ch} M{cx + cw},{cy} L{cx},{cy + ch}" stroke="#6b7477"/>')
                    continue
                if t == "#":
                    out.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" fill="#c4cacb" stroke="#6b7477"/>')
                    continue
                label = t
                col = pins.get(t, t) if pins else t
                first = next((p for p in str(col).split("|") if p != "*"), "*")
                base, stripe = mazda_colour(first)
                fill = base or "#f7f7f5"
                ink = "#ffffff" if base in ("#1b1b1b", "#2f63d6", "#7a4a22", "#8a3fc4", "#1d2f80", "#1e5f2e", "#d0261d", "#2e9a4a") else "#111417"
                out.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" fill="{fill}" stroke="#111417"/>')
                if stripe:
                    out.append(f'<rect x="{cx + cw - 12}" y="{cy}" width="10" height="{ch}" fill="{stripe}" stroke="#111417" stroke-width="0.5"/>')
                lines = [label] + ([str(col).replace("|", " ")] if pins else [])
                for i, ln in enumerate(lines):
                    out.append(f'<text x="{cx + (cw - 12) / 2 + 1}" y="{cy + 17 + i * 14}" text-anchor="middle" font-size="{12 if i == 0 else 10}" '
                               f'font-weight="{"bold" if i == 0 else "normal"}" fill="{ink if not pins or i else ink}" '
                               f'font-family="Consolas, Menlo, monospace">{escape(ln.replace("|", " "))}</text>')
        x += w + gap
    out.append("</svg>")
    return "\n".join(out)


def oem_markers(O, focus=None):
    """3D markers for every placed connector (estimated anchors)."""
    by_anchor = {}
    for c in O.M["connectors"]:
        a = (c.get("loc") or {}).get("anchor")
        if a in O.M["anchors"]:
            by_anchor.setdefault(a, []).append(c)
    out = []
    for a, cs in by_anchor.items():
        for i, c in enumerate(cs):
            out.append({"id": c["id"], "code": c["code"], "name": c.get("name", ""), "anchor": a, "i": i, "n": len(cs),
                        "harness": c.get("harness", ""), "focus": c["id"] == focus})
    return out


@app.route("/wiring/<int:sid>/oem")
def wiring_oem(sid):
    W = get_wiring(sid)
    O = W.oem or abort(404)
    conns = sorted(O.M["connectors"], key=lambda c: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", c["code"])])
    return render_template("wiring_oem.html", W=W, O=O, sid=sid, conns=conns, markers=oem_markers(O))


@app.route("/wiring/<int:sid>/oem/sys/<sys_id>")
def wiring_oem_sys(sid, sys_id):
    W = get_wiring(sid)
    O = W.oem or abort(404)
    S = O.sys.get(sys_id) or abort(404)
    return render_template("wiring_oem_sys.html", W=W, O=O, S=S, sid=sid, svg=O.figs.get(sys_id, ""),
                           conns=[O.conn[c] for c in S.get("conns", []) if c in O.conn])


@app.route("/wiring/<int:sid>/oem/c/<path:cid>")
def wiring_oem_conn(sid, cid):
    W = get_wiring(sid)
    O = W.oem or abort(404)
    c = O.conn.get(cid) or abort(404)
    taps = [(x, u) for x in W.M["connectors"] for u in [x.get("oem")] if u and u["ref"].split(".", 1)[0] == c["code"]]
    return render_template("wiring_oem_conn.html", W=W, O=O, c=c, sid=sid, face=oem_face_svg(c), pins=O.pins(cid),
                           markers=oem_markers(O, cid), taps=taps)


@app.route("/wiring/<int:sid>/delete", methods=["POST"])
def wiring_delete(sid):
    s = q1("SELECT set_key FROM wiring_sets WHERE id=?", (sid,)) or abort(404)
    x("DELETE FROM wiring_sets WHERE id=?", (sid,))
    flash(f"Deleted wiring set {s['set_key']}.")
    return redirect(url_for("wiring_library"))


@app.route("/wiring/<int:sid>/wd/<sys_id>")
def wiring_system(sid, sys_id):
    W = get_wiring(sid)
    S = W.sys.get(sys_id) or abort(404)
    conns = [c for c in W.M["connectors"] if c["unit"] in S["units"]]
    hars = [h for h in W.M["harnesses"] if h["system"] == S["id"]]
    units = [W.unit[u] for u in (S.get("dia") or S["units"]) if u in W.unit]
    return render_template("wiring_system.html", W=W, S=S, sid=sid, conns=conns, hars=hars,
                           units=units, svg=W.figs.get(S["to"], ""))


@app.route("/wiring/<int:sid>/unit/<uid>")
def wiring_unit(sid, uid):
    W = get_wiring(sid)
    u = W.unit.get(uid) or abort(404)
    brow = q1("SELECT data FROM wiring_boards WHERE set_id=? AND unit=?", (sid, uid))
    bd = json.loads(brow["data"]) if brow else None
    tabs = []
    if bd and bd.get("status") == "ok":
        tabs += [("sch", "Schematic", bd["sheets"]), ("perf", "Perfboard", bd["layout"]),
                 ("pcb", "PCB", bd["pcb"])]
        tabs = [t for t in tabs if t[2]]
        if bd.get("bom"):
            tabs.append(("bom", "BOM", None))
    tabs.append(("pins", "Connectors", None))
    tab = request.args.get("tab") or tabs[0][0]
    items = next((t[2] for t in tabs if t[0] == tab), None)
    try:
        n = min(max(int(request.args.get("n", 0)), 0), len(items) - 1) if items else 0
    except ValueError:
        n = 0
    conns = [c for c in W.M["connectors"] if c["unit"] == uid]
    card = module_card(W, uid) if not bd else ""
    return render_template("wiring_unit.html", W=W, u=u, sid=sid, bd=bd, tabs=tabs, tab=tab,
                           items=items, n=n, conns=conns, card=card, wds=W.unit_systems(uid),
                           home=W.sys.get(u["system"]))


# ---------------------------------------------------------------- export

@app.route("/vehicle/<int:vid>/export")
def export_page(vid):
    v = get_vehicle(vid)
    km, lit = fuel_stats(vid)
    return render_template("export.html", v=v, cs=cost_summary(vid),
                           lp100=(lit / km * 100) if km > 0 else None)


def _csv_response(rows, header, fname):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    w.writerows(rows)
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    return send_file(data, as_attachment=True, download_name=fname,
                     mimetype="text/csv")


@app.route("/vehicle/<int:vid>/export/sessions.csv")
def exp_sessions(vid):
    v = get_vehicle(vid)
    out = []
    for s in q("SELECT * FROM sessions WHERE vehicle_id=? ORDER BY date, id",
               (vid,)):
        parts = q("SELECT sp.*, p.name, p.part_number, su.name sup FROM "
                  "session_parts sp JOIN parts p ON p.id = sp.part_id "
                  "LEFT JOIN suppliers su ON su.id = p.supplier_id "
                  "WHERE sp.session_id=?", (s["id"],))
        base = [s["date"], s["odometer_km"], s["title"] or ""]
        tail = [s["labor_cost"] or 0, s["notes"] or ""]
        if not parts:
            out.append(base + [""] * 6 + tail)
        for r in parts:
            out.append(base + [r["name"], r["part_number"] or "",
                               r["sup"] or "", r["qty"], r["unit_price"],
                               r["qty"] * r["unit_price"]] + tail)
    return _csv_response(out, ["date", "odometer_km", "title", "part",
                               "part_number", "supplier", "qty", "unit_price",
                               "line_total", "labor_cost", "notes"],
                         f"{v['name']}_sessions.csv")


@app.route("/vehicle/<int:vid>/export/fuel.csv")
def exp_fuel(vid):
    v = get_vehicle(vid)
    out = [[r["date"], r["odometer_km"], r["liters"], r["total_cost"] or "",
            r["full_tank"], r["notes"] or ""]
           for r in q("SELECT * FROM fuel_log WHERE vehicle_id=? "
                      "ORDER BY odometer_km", (vid,))]
    return _csv_response(out, ["date", "odometer_km", "liters", "total_cost",
                               "full_tank", "notes"], f"{v['name']}_fuel.csv")


@app.route("/export/parts.csv")
def exp_parts():
    out = [[r["name"], r["part_number"] or "", r["unit_price"] or "",
            r["sup"] or "", r["website"] or "", r["email"] or "",
            r["phone"] or "", r["notes"] or ""]
           for r in q("SELECT p.*, s.name sup, s.website, s.email, s.phone "
                      "FROM parts p LEFT JOIN suppliers s "
                      "ON s.id = p.supplier_id ORDER BY p.name COLLATE NOCASE")]
    return _csv_response(out, ["part", "part_number", "unit_price", "supplier",
                               "website", "email", "phone", "notes"],
                         "parts_catalog.csv")


@app.route("/vehicle/<int:vid>/export/report.pdf")
def exp_pdf(vid):
    v = get_vehicle(vid)
    return send_file(io.BytesIO(build_vehicle_report(v)), as_attachment=True,
                     download_name=f"{v['name']}_history.pdf",
                     mimetype="application/pdf")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
