"""
============================================================
  ELECTROFIX HUB — app.py
  Python 3.13 Flask Backend
  Nairobi's Electronics & Electrical Expert
============================================================
  Features:
    • Serves the full website (HTML/CSS/JS)
    • REST API for contact/booking form
    • SQLite database for all inquiries & bookings
    • Admin dashboard (password-protected)
    • Email notifications via SMTP
    • WhatsApp link generator
    • Service quote calculator
    • Logging & error handling
============================================================
"""

from __future__ import annotations

import os
import re
import sqlite3
import smtplib
import logging
import hashlib
import secrets
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask, request, jsonify, render_template_string,
    redirect, url_for, session, send_from_directory, abort
)

# ── Python 3.13: use tomllib (stdlib) for config ──────────────────────────────
try:
    import tomllib                         # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib               # pip install tomli  (fallback)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent
DB_PATH    = BASE_DIR / "electrofix.db"
STATIC_DIR = BASE_DIR / "static"
LOG_FILE   = BASE_DIR / "electrofix.log"

# Read optional config.toml, fall back to env vars / defaults
_CFG: dict[str, Any] = {}
_CFG_FILE = BASE_DIR / "config.toml"
if _CFG_FILE.exists():
    with open(_CFG_FILE, "rb") as _f:
        _CFG = tomllib.load(_f)

def _cfg(key: str, default: Any = None) -> Any:
    """Look up a config key (config.toml → env var → default)."""
    val = _CFG.get(key) or os.environ.get(key.upper())
    return val if val is not None else default

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s [%(levelname)s] %(message)s",
    handlers = [
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("electrofix")

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC_DIR))
app.secret_key = _cfg("secret_key", secrets.token_hex(32))
app.config["JSON_SORT_KEYS"] = False

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS inquiries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    phone        TEXT    NOT NULL,
    email        TEXT,
    service      TEXT    NOT NULL,
    location     TEXT,
    message      TEXT,
    status       TEXT    DEFAULT 'new',
    ip_address   TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id   INTEGER REFERENCES inquiries(id),
    service      TEXT    NOT NULL,
    description  TEXT,
    amount_ksh   REAL    NOT NULL,
    valid_days   INTEGER DEFAULT 7,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token        TEXT    PRIMARY KEY,
    created_at   TEXT    NOT NULL
);
"""

def get_db() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with row_factory."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db() -> None:
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript(SCHEMA)
    log.info("Database initialised at %s", DB_PATH)

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
PHONE_RE = re.compile(r"^[\+\d\s\-\(\)]{7,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def validate_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone.strip()))

def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip())) if email else True   # optional

def sanitise(text: str | None, maxlen: int = 500) -> str:
    if not text:
        return ""
    return str(text).strip()[:maxlen]

def wa_link(phone: str, message: str = "") -> str:
    """Build a wa.me link for the given phone number."""
    clean = re.sub(r"[^\d+]", "", phone)
    if not clean.startswith("+"):
        clean = "+254" + clean.lstrip("0")
    import urllib.parse
    return f"https://wa.me/{clean.lstrip('+')}?text={urllib.parse.quote(message)}"

# ─────────────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION
# ─────────────────────────────────────────────────────────────────────────────
def send_email_notification(inquiry: dict[str, Any]) -> bool:
    """
    Send a notification email to the shop owner.
    Configure SMTP settings in config.toml or env vars:
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, NOTIFY_EMAIL
    """
    smtp_host  = _cfg("smtp_host",   "smtp.gmail.com")
    smtp_port  = int(_cfg("smtp_port", 587))
    smtp_user  = _cfg("smtp_user",   "")
    smtp_pass  = _cfg("smtp_pass",   "")
    notify_to  = _cfg("notify_email", smtp_user)

    if not smtp_user or not smtp_pass:
        log.warning("Email not configured — skipping notification.")
        return False

    subject = f"[Electrofix Hub] New Booking: {inquiry['service']}"
    body = (
        f"New inquiry received!\n\n"
        f"Name    : {inquiry['name']}\n"
        f"Phone   : {inquiry['phone']}\n"
        f"Email   : {inquiry.get('email') or 'N/A'}\n"
        f"Service : {inquiry['service']}\n"
        f"Location: {inquiry.get('location') or 'N/A'}\n"
        f"Message : {inquiry.get('message') or 'N/A'}\n\n"
        f"Time    : {inquiry['created_at']}\n"
        f"Admin   : http://localhost:5000/admin\n"
    )

    msg = MIMEMultipart()
    msg["From"]    = smtp_user
    msg["To"]      = notify_to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, notify_to, msg.as_string())
        log.info("Notification email sent to %s", notify_to)
        return True
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
        return False

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN AUTH
# ─────────────────────────────────────────────────────────────────────────────
ADMIN_PASSWORD_HASH = hashlib.sha256(
    _cfg("admin_password", "electrofix2024").encode()
).hexdigest()

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────────────────────────────────────
# SERVICE PRICE GUIDE  (KSh)
# ─────────────────────────────────────────────────────────────────────────────
SERVICE_PRICES: dict[str, dict] = {
    "phone_screen":          {"label": "Phone Screen Replacement",    "min": 1500,  "max": 8000},
    "phone_battery":         {"label": "Phone Battery Replacement",   "min": 500,   "max": 2500},
    "phone_charging_port":   {"label": "Charging Port Repair",        "min": 800,   "max": 2000},
    "frp_bypass":            {"label": "FRP / Google Account Bypass", "min": 500,   "max": 1500},
    "imei_unlock":           {"label": "IMEI / Network Unlock",       "min": 1000,  "max": 3000},
    "laptop_screen":         {"label": "Laptop Screen Replacement",   "min": 5000,  "max": 20000},
    "laptop_keyboard":       {"label": "Laptop Keyboard",             "min": 2000,  "max": 8000},
    "solar_1kw":             {"label": "Solar System 1kW",            "min": 45000, "max": 75000},
    "solar_3kw":             {"label": "Solar System 3kW",            "min": 110000,"max": 160000},
    "solar_maintenance":     {"label": "Solar Maintenance Visit",     "min": 2000,  "max": 5000},
    "wiring_1phase":         {"label": "1-Phase House Wiring",        "min": 15000, "max": 60000},
    "wiring_3phase":         {"label": "3-Phase Industrial Wiring",   "min": 40000, "max": 200000},
    "motor_contactor":       {"label": "Motor Contactor Panel",       "min": 20000, "max": 80000},
    "autocad_drawing":       {"label": "AutoCAD Drawing",             "min": 3000,  "max": 15000},
    "website_basic":         {"label": "Basic Business Website",      "min": 8000,  "max": 20000},
    "website_ecommerce":     {"label": "E-Commerce Website",          "min": 25000, "max": 80000},
    "mobile_app":            {"label": "Mobile App Development",      "min": 30000, "max": 120000},
    "building_fitting":      {"label": "Building Electrical Fitting", "min": 10000, "max": 80000},
    "cctv_install":          {"label": "CCTV Installation (4 cams)",  "min": 15000, "max": 35000},
    "piping":                {"label": "Piping / Conduit Works",      "min": 5000,  "max": 30000},
}

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — PUBLIC
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the main website HTML."""
    html_path = BASE_DIR / "electrofix_hub.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>Electrofix Hub</h1><p>Place electrofix_hub.html in the same folder.</p>", 200


@app.route("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(STATIC_DIR, filename)


# ─────────────────────────────────────────────────────────────────────────────
# API — CONTACT / BOOKING FORM
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/contact", methods=["POST"])
def api_contact():
    """
    Accepts JSON or form-data booking submissions.
    Required fields : name, phone, service
    Optional fields : email, location, message
    """
    data = request.get_json(silent=True) or request.form.to_dict()

    # ── Validation ──────────────────────────────────────────────────────────
    errors: list[str] = []

    name    = sanitise(data.get("name"),    100)
    phone   = sanitise(data.get("phone"),    30)
    email   = sanitise(data.get("email"),   150)
    service = sanitise(data.get("service"), 120)
    loc     = sanitise(data.get("location"), 150)
    msg     = sanitise(data.get("message"), 1000)

    if not name:
        errors.append("Name is required.")
    if not phone:
        errors.append("Phone number is required.")
    elif not validate_phone(phone):
        errors.append("Phone number looks invalid.")
    if email and not validate_email(email):
        errors.append("Email address looks invalid.")
    if not service:
        errors.append("Please select a service.")

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    # ── Save to DB ───────────────────────────────────────────────────────────
    ts = now_iso()
    inquiry = {
        "name":       name,
        "phone":      phone,
        "email":      email,
        "service":    service,
        "location":   loc,
        "message":    msg,
        "ip_address": request.remote_addr,
        "created_at": ts,
        "updated_at": ts,
    }

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO inquiries
               (name, phone, email, service, location, message,
                ip_address, created_at, updated_at)
               VALUES (:name,:phone,:email,:service,:location,:message,
                       :ip_address,:created_at,:updated_at)""",
            inquiry,
        )
        inquiry["id"] = cur.lastrowid

    log.info("New inquiry #%s — %s — %s", inquiry["id"], name, service)

    # ── Email notification (non-blocking) ────────────────────────────────────
    send_email_notification(inquiry)

    # ── WhatsApp deep-link for owner ─────────────────────────────────────────
    owner_phone = _cfg("owner_phone", "254700000000")
    wa_msg = (
        f"New booking from {name} ({phone})\n"
        f"Service: {service}\n"
        f"Location: {loc or 'N/A'}\n"
        f"Message: {msg or 'N/A'}"
    )
    wa = wa_link(owner_phone, wa_msg)

    return jsonify({
        "ok":         True,
        "message":    "Thank you! We will contact you shortly.",
        "inquiry_id": inquiry["id"],
        "whatsapp":   wa,
    }), 201


# ─────────────────────────────────────────────────────────────────────────────
# API — QUOTE CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/services", methods=["GET"])
def api_services():
    """Return full service catalogue with price ranges."""
    return jsonify({"ok": True, "services": SERVICE_PRICES})


@app.route("/api/quote", methods=["POST"])
def api_quote():
    """
    Estimate a service quote.
    Body: { "service_key": "solar_1kw", "notes": "optional" }
    """
    data        = request.get_json(silent=True) or {}
    service_key = sanitise(data.get("service_key"), 60)

    if service_key not in SERVICE_PRICES:
        return jsonify({
            "ok":      False,
            "error":   "Unknown service key.",
            "choices": list(SERVICE_PRICES.keys()),
        }), 400

    svc = SERVICE_PRICES[service_key]
    return jsonify({
        "ok":          True,
        "service":     svc["label"],
        "min_ksh":     svc["min"],
        "max_ksh":     svc["max"],
        "note":        "Final price depends on scope. Request a free site survey.",
        "whatsapp":    wa_link(
            _cfg("owner_phone", "254700000000"),
            f"Hi Electrofix Hub! I need a quote for: {svc['label']}"
        ),
    })


# ─────────────────────────────────────────────────────────────────────────────
# API — WHATSAPP LINK GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/whatsapp", methods=["GET"])
def api_whatsapp():
    """
    Generate a wa.me link.
    Query params: service (optional), message (optional)
    """
    service = request.args.get("service", "")
    custom  = request.args.get("message", "")
    phone   = _cfg("owner_phone", "254700000000")

    default_msg = (
        f"Hi Electrofix Hub! I need help with: {service}"
        if service else
        "Hi Electrofix Hub! I'd like to book a service."
    )
    link = wa_link(phone, custom or default_msg)
    return jsonify({"ok": True, "link": link})


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — LOGIN
# ─────────────────────────────────────────────────────────────────────────────

ADMIN_LOGIN_HTML = """
<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Electrofix Hub Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#06090f;color:#ddeeff;font-family:'Segoe UI',sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#0f1928;border:1px solid #1a2d45;border-radius:12px;padding:2.5rem 2rem;width:360px}
h2{font-size:1.3rem;color:#00d4ff;margin-bottom:1.5rem;text-align:center}
label{display:block;font-size:.78rem;color:#4d7a99;margin-bottom:.4rem;letter-spacing:.08em}
input{width:100%;background:#0c1220;border:1px solid #1a2d45;color:#ddeeff;
      padding:.75rem 1rem;border-radius:6px;outline:none;font-size:.95rem;margin-bottom:1.2rem}
input:focus{border-color:#00d4ff}
button{width:100%;background:#00d4ff;color:#06090f;font-weight:700;font-size:.9rem;
       letter-spacing:.08em;padding:.9rem;border:none;border-radius:6px;cursor:pointer}
button:hover{opacity:.88}
.err{color:#ff3b5c;font-size:.83rem;margin-bottom:1rem;text-align:center}
</style></head><body>
<div class="box">
  <h2>⚡ ELECTROFIX HUB ADMIN</h2>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="post">
    <label>PASSWORD</label>
    <input type="password" name="password" placeholder="Enter admin password" autofocus/>
    <button type="submit">LOGIN →</button>
  </form>
</div></body></html>
"""

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw   = request.form.get("password", "")
        hashed = hashlib.sha256(pw.encode()).hexdigest()
        if hashed == ADMIN_PASSWORD_HASH:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Incorrect password."
    return render_template_string(ADMIN_LOGIN_HTML, error=error)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

ADMIN_HTML = """
<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin — Electrofix Hub</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#06090f;--panel:#0c1220;--card:#0f1928;--border:#1a2d45;
      --cyan:#00d4ff;--orange:#ff7b00;--green:#00ff88;--red:#ff3b5c;
      --text:#ddeeff;--muted:#4d7a99}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;font-size:14px}
nav{background:var(--panel);border-bottom:1px solid var(--border);
    padding:.9rem 2rem;display:flex;justify-content:space-between;align-items:center}
nav h1{color:var(--cyan);font-size:1rem;letter-spacing:.08em}
nav a{color:var(--muted);text-decoration:none;font-size:.82rem}
nav a:hover{color:var(--text)}
.wrap{padding:2rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin-bottom:2rem}
.stat{background:var(--card);border:1px solid var(--border);border-radius:8px;
      padding:1.2rem;text-align:center}
.stat .num{font-size:2rem;font-weight:900;color:var(--cyan)}
.stat .lbl{color:var(--muted);font-size:.78rem;margin-top:.2rem;letter-spacing:.06em}
.filters{display:flex;gap:.7rem;margin-bottom:1.2rem;flex-wrap:wrap}
.filters select,.filters input{background:var(--card);border:1px solid var(--border);
  color:var(--text);padding:.5rem .8rem;border-radius:5px;outline:none;font-size:.82rem}
.filters select:focus,.filters input:focus{border-color:var(--cyan)}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden}
thead{background:var(--panel)}
th{text-align:left;padding:.85rem 1rem;font-size:.72rem;color:var(--muted);
   letter-spacing:.1em;border-bottom:1px solid var(--border)}
td{padding:.8rem 1rem;border-bottom:1px solid var(--border);font-size:.83rem;
   max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
tr:last-child td{border:none}
tr:hover td{background:rgba(0,212,255,.03)}
.badge{display:inline-block;padding:.18rem .55rem;border-radius:3px;
       font-size:.65rem;font-weight:700;letter-spacing:.08em}
.badge.new    {background:rgba(0,212,255,.12);color:var(--cyan)}
.badge.done   {background:rgba(0,255,136,.12);color:var(--green)}
.badge.pending{background:rgba(255,123,0,.12);color:var(--orange)}
.btn-sm{padding:.3rem .75rem;border-radius:4px;font-size:.72rem;
        font-weight:700;cursor:pointer;border:none;letter-spacing:.06em}
.btn-sm.green{background:var(--green);color:#000}
.btn-sm.red  {background:var(--red);color:#fff}
.btn-sm.wa   {background:#25d366;color:#fff;text-decoration:none;display:inline-block}
.empty{text-align:center;padding:3rem;color:var(--muted)}
</style></head>
<body>
<nav>
  <h1>⚡ ELECTROFIX HUB — ADMIN PANEL</h1>
  <div style="display:flex;gap:1.5rem;align-items:center">
    <span style="color:var(--muted)">{{ total }} total inquiries</span>
    <a href="{{ url_for('admin_logout') }}">Logout →</a>
  </div>
</nav>
<div class="wrap">

  <!-- Stats -->
  <div class="stats">
    <div class="stat"><div class="num">{{ total }}</div><div class="lbl">TOTAL INQUIRIES</div></div>
    <div class="stat"><div class="num" style="color:var(--cyan)">{{ new_count }}</div><div class="lbl">NEW</div></div>
    <div class="stat"><div class="num" style="color:var(--orange)">{{ pending_count }}</div><div class="lbl">PENDING</div></div>
    <div class="stat"><div class="num" style="color:var(--green)">{{ done_count }}</div><div class="lbl">COMPLETED</div></div>
  </div>

  <!-- Filters -->
  <form method="get" style="display:contents">
  <div class="filters">
    <select name="status" onchange="this.form.submit()">
      <option value="">All Statuses</option>
      <option value="new"     {% if status_f=='new'     %}selected{% endif %}>New</option>
      <option value="pending" {% if status_f=='pending' %}selected{% endif %}>Pending</option>
      <option value="done"    {% if status_f=='done'    %}selected{% endif %}>Done</option>
    </select>
    <input type="text" name="q" value="{{ q }}" placeholder="Search name / service / phone…"/>
    <button class="btn-sm green" type="submit">Search</button>
    <a href="{{ url_for('admin_dashboard') }}" class="btn-sm" style="background:var(--border);color:var(--text)">Clear</a>
  </div>
  </form>

  <!-- Table -->
  {% if rows %}
  <table>
    <thead>
      <tr>
        <th>#</th><th>NAME</th><th>PHONE</th><th>SERVICE</th>
        <th>LOCATION</th><th>MESSAGE</th><th>STATUS</th><th>DATE</th><th>ACTIONS</th>
      </tr>
    </thead>
    <tbody>
    {% for r in rows %}
    <tr>
      <td>{{ r['id'] }}</td>
      <td>{{ r['name'] }}</td>
      <td>{{ r['phone'] }}</td>
      <td title="{{ r['service'] }}">{{ r['service'][:35] }}{% if r['service']|length > 35 %}…{% endif %}</td>
      <td>{{ r['location'] or '—' }}</td>
      <td title="{{ r['message'] }}">{{ (r['message'] or '—')[:40] }}{% if r['message'] and r['message']|length > 40 %}…{% endif %}</td>
      <td><span class="badge {{ r['status'] }}">{{ r['status'].upper() }}</span></td>
      <td>{{ r['created_at'][:10] }}</td>
      <td style="white-space:nowrap;display:flex;gap:.4rem;align-items:center">
        <a class="btn-sm wa"
           href="https://wa.me/{{ r['phone']|replace('+','') }}?text=Hi+{{ r['name']|urlencode }}%2C+this+is+Electrofix+Hub!"
           target="_blank">WA</a>
        <form method="post" action="{{ url_for('admin_update_status', inquiry_id=r['id']) }}" style="display:contents">
          <select name="status" onchange="this.form.submit()"
            style="background:var(--panel);border:1px solid var(--border);
                   color:var(--text);padding:.25rem .4rem;border-radius:4px;font-size:.72rem">
            <option value="new"     {% if r['status']=='new'     %}selected{% endif %}>New</option>
            <option value="pending" {% if r['status']=='pending' %}selected{% endif %}>Pending</option>
            <option value="done"    {% if r['status']=='done'    %}selected{% endif %}>Done</option>
          </select>
        </form>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No inquiries found.</div>
  {% endif %}

</div>
</body></html>
"""

@app.route("/admin")
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    status_f = request.args.get("status", "")
    q        = request.args.get("q", "")

    with get_db() as conn:
        # Counts
        def count(status=None):
            if status:
                return conn.execute(
                    "SELECT COUNT(*) FROM inquiries WHERE status=?", (status,)
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]

        total         = count()
        new_count     = count("new")
        pending_count = count("pending")
        done_count    = count("done")

        # Query
        sql    = "SELECT * FROM inquiries WHERE 1=1"
        params: list[Any] = []
        if status_f:
            sql    += " AND status=?"
            params.append(status_f)
        if q:
            sql    += " AND (name LIKE ? OR phone LIKE ? OR service LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like, like])
        sql += " ORDER BY created_at DESC LIMIT 200"

        rows = conn.execute(sql, params).fetchall()

    return render_template_string(
        ADMIN_HTML,
        rows=rows, total=total,
        new_count=new_count, pending_count=pending_count, done_count=done_count,
        status_f=status_f, q=q,
    )


@app.route("/admin/inquiry/<int:inquiry_id>/status", methods=["POST"])
@admin_required
def admin_update_status(inquiry_id: int):
    new_status = sanitise(request.form.get("status", "new"), 20)
    if new_status not in ("new", "pending", "done"):
        abort(400)
    with get_db() as conn:
        conn.execute(
            "UPDATE inquiries SET status=?, updated_at=? WHERE id=?",
            (new_status, now_iso(), inquiry_id),
        )
    log.info("Inquiry #%s status → %s", inquiry_id, new_status)
    return redirect(url_for("admin_dashboard"))


# ─────────────────────────────────────────────────────────────────────────────
# API — INQUIRY LIST (for programmatic use)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/inquiries", methods=["GET"])
@admin_required
def api_inquiries():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM inquiries ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return jsonify({"ok": True, "inquiries": [dict(r) for r in rows]})


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":  "ok",
        "service": "Electrofix Hub API",
        "python":  "3.13",
        "time":    now_iso(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# ERROR HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(_e):
    return jsonify({"ok": False, "error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    log.error("500 error: %s", e)
    return jsonify({"ok": False, "error": "Internal server error"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    STATIC_DIR.mkdir(exist_ok=True)
    init_db()
    log.info("Starting Electrofix Hub server…")
    log.info("Admin panel → http://127.0.0.1:5000/admin  (default password: electrofix2024)")
    app.run(
        host  = _cfg("host", "0.0.0.0"),
        port  = int(_cfg("port", 5002)),
        debug = bool(_cfg("debug", True)),
    )