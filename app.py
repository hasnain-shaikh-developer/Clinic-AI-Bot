"""
╔══════════════════════════════════════════════════════════════════╗
║         CLINIC APPOINTMENT CHATBOT  —  Flask Backend            ║
║         Reusable · Multi-client · International-ready           ║
╠══════════════════════════════════════════════════════════════════╣
║  HOW TO CUSTOMISE FOR A NEW CLIENT:                             ║
║    1.  Edit CLINIC_CONFIG below — nothing else needs changing.  ║
║    2.  Add clinic to CLINICS dict with its own timings/tz.      ║
║    3.  To reset appointment data: delete appointments.db        ║
║        — it is recreated automatically on next startup.         ║
║                                                                 ║
║  MULTI-CLIENT SAFETY:                                           ║
║    Keep each client in a separate project folder.               ║
║    Never share the same appointments.db across clients.         ║
╚══════════════════════════════════════════════════════════════════╝
"""

from flask import Flask, request, jsonify, render_template, redirect, url_for
import json, os, sqlite3
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo          # Python 3.9+ stdlib — no extra install
import requests
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════
#  CLINIC CONFIGURATION  ←  Shared UI defaults (colours, fees, etc.)
#  Per-clinic identity (name, timings, timezone) lives in CLINICS.
# ══════════════════════════════════════════════════════════════════
CLINIC_CONFIG = {
    "clinic_tagline":   "Trusted care, every visit",
    "clinic_location":  "London, UK",
    "clinic_fees":      "$20",
    "primary_color":    "#2563eb",
    "site_url":         "http://localhost:5000",
}

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

def get_base_url():
    return BASE_URL or CLINIC_CONFIG.get("site_url", "http://localhost:5000").rstrip("/")

# ══════════════════════════════════════════════════════════════════
#  MULTI-CLINIC REGISTRY  ←  UPDATED
#
#  Each clinic entry:
#    name                 — display name
#    timings              — "H:MM AM - H:MM PM"
#    slot_limit           — max appointments per day
#    timezone             — IANA tz string (used for "now" comparisons)
#    telegram_bot_token   — from @BotFather
#    telegram_chat_id     — doctor's chat/group id
#
#  clinic_id keys use random-looking strings (not "clinic1").
#  To add a clinic, copy an entry and change the key + values.
# ══════════════════════════════════════════════════════════════════
CLINICS = {
    "x7k2m9": {
        "name":               "CarePlus Clinic",
        "clinic_name":        "CarePlus Clinic",   # alias used in templates
        "clinic_days":        "Monday to Saturday",
        "clinic_timings":     "9:00 AM - 6:00 PM",
        "clinic_location":    "London, UK",
        "clinic_fees":        "$20",
        "daily_slot_limit":   10,
        "timezone":           "Europe/London",
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "PASTE_BOT_TOKEN_HERE"),
        "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID",   "PASTE_CHAT_ID_HERE"),
    },
    "p4q8w1": {
        "name":               "MedPoint Clinic",
        "clinic_name":        "MedPoint Clinic",
        "clinic_days":        "Monday to Friday",
        "clinic_timings":     "8:00 AM - 5:00 PM",
        "clinic_location":    "Karachi, Pakistan",
        "clinic_fees":        "PKR 800",
        "daily_slot_limit":   8,
        "timezone":           "Asia/Karachi",
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN_2", "PASTE_BOT_TOKEN_2_HERE"),
        "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID_2",   "PASTE_CHAT_ID_2_HERE"),
    },
    # ── Add more clinics here ─────────────────────────────────────
    # "r3n5t6": {
    #     "name":               "HealthFirst",
    #     "clinic_name":        "HealthFirst",
    #     "clinic_days":        "Monday to Saturday",
    #     "clinic_timings":     "10:00 AM - 7:00 PM",
    #     "clinic_location":    "Dubai, UAE",
    #     "clinic_fees":        "AED 150",
    #     "daily_slot_limit":   12,
    #     "timezone":           "Asia/Dubai",
    #     "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN_3"),
    #     "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID_3"),
    # },
}

# Default clinic_id used as fallback when none is provided
DEFAULT_CLINIC_ID = "x7k2m9"

# ══════════════════════════════════════════════════════════════════
app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "appointments.db")


def get_clinic(clinic_id):
    """Return clinic dict or the default clinic if id not found."""
    return CLINICS.get(clinic_id, CLINICS[DEFAULT_CLINIC_ID])


def cfg(key, clinic_id=None):
    """
    Read a config value.  Prefers CLINICS[clinic_id] for per-clinic
    keys; falls back to CLINIC_CONFIG for shared UI values.
    """
    if clinic_id:
        clinic = CLINICS.get(clinic_id)
        if clinic and key in clinic:
            return clinic[key]
    return CLINIC_CONFIG.get(key, "")


def build_clinic_ctx(clinic_id):
    """
    Build the template context dict for a given clinic.
    Merges CLINIC_CONFIG defaults with CLINICS[clinic_id] overrides.
    Also injects admin_path so filter pills work correctly.
    """
    clinic = get_clinic(clinic_id)
    ctx = {**CLINIC_CONFIG, **clinic}
    ctx["admin_path"] = f"admin/{clinic_id}"
    ctx["clinic_id"]  = clinic_id
    return ctx


# ── SQLite ────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id        TEXT PRIMARY KEY,
                clinic_id TEXT NOT NULL DEFAULT '',
                name      TEXT NOT NULL,
                phone     TEXT NOT NULL,
                date      TEXT NOT NULL,
                time      TEXT NOT NULL,
                problem   TEXT NOT NULL,
                booked_at TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


init_db()


# ══════════════════════════════════════════════════════════════════
#  DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════

def load_appointments(clinic_id=None):
    """
    Always fresh from SQLite.  Filters by clinic_id when provided.
    Never raises — returns [] on any error.
    """
    try:
        conn = get_db()
        try:
            if clinic_id:
                rows = conn.execute(
                    "SELECT * FROM appointments WHERE clinic_id = ? ORDER BY booked_at DESC",
                    (clinic_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM appointments ORDER BY booked_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as exc:
        print(f"[DB] load_appointments error: {exc}")
        return []


def save_appointment(appt):
    """Insert one appointment.  Returns True on success, False on error."""
    try:
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO appointments
                   (id, clinic_id, name, phone, date, time, problem, booked_at)
                   VALUES (:id, :clinic_id, :name, :phone, :date, :time, :problem, :booked_at)""",
                {
                    "id":        appt["id"],
                    "clinic_id": appt.get("clinic_id", DEFAULT_CLINIC_ID),
                    "name":      appt["name"],
                    "phone":     appt["phone"],
                    "date":      appt["date"],
                    "time":      appt["time"],
                    "problem":   appt["problem"],
                    "booked_at": appt["booked_at"],
                }
            )
            conn.commit()
            print(f"[DB] Saved: {appt['name']} on {appt['date']} at {appt['time']}")
            return True
        finally:
            conn.close()
    except sqlite3.IntegrityError:
        print(f"[DB] Duplicate id {appt.get('id')} — skipped.")
        return True
    except Exception as exc:
        print(f"[DB] save_appointment error: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════
#  TIME SLOT ENGINE  — UPDATED: per-clinic timings + timezone
# ══════════════════════════════════════════════════════════════════

def _parse_clinic_time(raw):
    """Parse "9:00 AM", "9 AM", "18:00" → datetime.time."""
    raw = raw.strip()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%H"):
        try:
            return datetime.strptime(raw.upper(), fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse time: {raw!r}")


def generate_time_slots(clinic_id):
    """
    UPDATED: Generate slots from CLINICS[clinic_id]['clinic_timings']
    and CLINICS[clinic_id]['daily_slot_limit'].
    Different clinics produce different slot schedules.
    """
    clinic    = get_clinic(clinic_id)
    timings   = clinic.get("clinic_timings", "9:00 AM - 6:00 PM")
    limit     = clinic.get("daily_slot_limit", 10)

    for sep in [" – ", " - ", "–", "—", "-"]:
        if sep in timings:
            raw_start, raw_end = timings.split(sep, 1)
            break
    else:
        raise ValueError(f"Cannot parse clinic_timings: {timings!r}")

    start = _parse_clinic_time(raw_start)
    end   = _parse_clinic_time(raw_end)

    base       = datetime(2000, 1, 1)
    dt_start   = base.replace(hour=start.hour, minute=start.minute)
    dt_end     = base.replace(hour=end.hour,   minute=end.minute)
    total_mins = int((dt_end - dt_start).total_seconds() / 60)
    interval   = total_mins // limit

    slots = []
    for i in range(limit):
        slot_dt = dt_start + timedelta(minutes=i * interval)
        slots.append(slot_dt.strftime("%I:%M %p").lstrip("0"))
    return slots


# Pre-generate slots for every clinic at startup
ALL_SLOTS = {cid: generate_time_slots(cid) for cid in CLINICS}


def clinic_now(clinic_id):
    """
    UPDATED: Return datetime.now() in the clinic's configured timezone.
    Uses zoneinfo (Python 3.9+ stdlib).
    """
    tz_name = get_clinic(clinic_id).get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).replace(tzinfo=None)   # naive local time for slot comparisons


def get_booked_times(date_str, records):
    """Return set of times already booked for date_str."""
    return {
        r.get("time", "")
        for r in records
        if date_str.lower() in r.get("date", "").lower()
    }


def count_booked(date_str, records):
    """Count appointments for date_str."""
    return sum(1 for r in records if date_str.lower() in r.get("date", "").lower())


def next_free_slot(date_str, records, clinic_id=DEFAULT_CLINIC_ID):
    """
    UPDATED: Return next unbooked slot that is in the future.

    - Uses clinic timezone for "now" comparison.
    - For TODAY: skips all slots whose combined datetime <= now.
    - For FUTURE dates: all unbooked slots are valid.
    - Returns None when no slots remain (caller handles next-day shift).
    """
    slots  = ALL_SLOTS.get(clinic_id, ALL_SLOTS[DEFAULT_CLINIC_ID])
    booked = get_booked_times(date_str, records)
    now    = clinic_now(clinic_id)

    parsed_date = parse_user_date(date_str)
    if not parsed_date:
        return None

    is_today = (parsed_date == now.date())

    for slot in slots:
        if slot in booked:
            continue
        if is_today:
            try:
                slot_time = datetime.strptime(slot, "%I:%M %p").time()
                slot_dt   = datetime.combine(parsed_date, slot_time)
                if slot_dt <= now:
                    continue   # past slot — skip
            except ValueError:
                pass
        return slot   # first valid future slot

    return None   # all gone


def find_next_open_date(from_date, records, clinic_id=DEFAULT_CLINIC_ID):
    """Walk forward from from_date until a date with free slots is found."""
    limit = get_clinic(clinic_id).get("daily_slot_limit", 10)
    for delta in range(60):
        candidate = from_date + timedelta(days=delta)
        if count_booked(format_date(candidate), records) < limit:
            return candidate
    return from_date + timedelta(days=60)


# ══════════════════════════════════════════════════════════════════
#  DATE HELPERS
# ══════════════════════════════════════════════════════════════════

_MONTHS = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
    "january":1,"february":2,"march":3,"april":4,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}


def parse_user_date(text):
    """
    Flexible date parser. Returns a date object or None.
    Compares using date objects — no string comparison.
    """
    if not text:
        return None
    text  = text.strip()
    today = date.today()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    tokens = text.replace(",", " ").split()
    day = month = year = None
    for tok in tokens:
        if tok.isdigit():
            val = int(tok)
            if val > 31:
                year = val
            elif day is None:
                day = val
            else:
                year = val
        else:
            m = _MONTHS.get(tok.lower())
            if m:
                month = m

    if day and month:
        if year is None:
            year = today.year
            try:
                if date(year, month, day) < today:
                    year = today.year + 1
            except ValueError:
                pass
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def format_date(d):
    return d.strftime("%d %B %Y")


def today_str():
    return format_date(date.today())


def example_future_date():
    return format_date(date.today() + timedelta(days=3))


def clinic_end_time(clinic_id=DEFAULT_CLINIC_ID):
    """Parse closing time from clinic config."""
    timings = get_clinic(clinic_id).get("clinic_timings", "")
    for sep in [" – ", " - ", "–", "—", "-"]:
        if sep in timings:
            _, raw_end = timings.split(sep, 1)
            try:
                return _parse_clinic_time(raw_end.strip())
            except ValueError:
                return None
    return None


def earliest_bookable_date(clinic_id=DEFAULT_CLINIC_ID):
    """
    Return the earliest date a patient may book.
    If clinic is already closed for today (per clinic timezone), return tomorrow.
    """
    today  = date.today()
    end    = clinic_end_time(clinic_id)
    now_tz = clinic_now(clinic_id)

    if end is not None and now_tz.time() >= end:
        return today + timedelta(days=1)
    return today


def validate_date(text, clinic_id=DEFAULT_CLINIC_ID):
    """
    Parse and validate a user-supplied date string.
    Returns (date_obj, None) or (None, error_message).
    Uses date objects for comparison — no string comparison.
    """
    parsed = parse_user_date(text)
    if parsed is None:
        return None, (
            f"I could not understand that date. "
            f"Please enter a date like: {example_future_date()}"
        )

    earliest = earliest_bookable_date(clinic_id)

    if parsed < earliest:
        if earliest == date.today():
            return None, (
                f"Please select a valid future date. Today is {today_str()}."
            )
        else:
            return None, (
                f"Our clinic is closed for today. "
                f"The earliest available date is {format_date(earliest)}.\n"
                f"Please enter {format_date(earliest)} or a later date."
            )

    return parsed, None


# ══════════════════════════════════════════════════════════════════
#  ADMIN DATE FILTERING  — UPDATED: added "tomorrow" filter
# ══════════════════════════════════════════════════════════════════

def filter_appointments(records, period):
    """
    Filter appointments by time period.

    Supported periods:
      all       → all records
      today     → appointment date == today
      tomorrow  → appointment date == tomorrow    ← NEW
      yesterday → appointment date == yesterday
      last7     → last 7 days (today inclusive)
      lastmonth → previous calendar month
      lastyear  → previous calendar year
    """
    if period == "all" or not period:
        return records

    today     = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow  = today + timedelta(days=1)        # NEW
    filtered  = []

    for appt in records:
        appt_date = parse_user_date(appt.get("date", ""))
        if appt_date is None:
            continue

        if period == "today":
            if appt_date == today:
                filtered.append(appt)

        elif period == "tomorrow":                # NEW — tomorrow filter
            if appt_date == tomorrow:
                filtered.append(appt)

        elif period == "yesterday":
            if appt_date == yesterday:
                filtered.append(appt)

        elif period == "last7":
            if today - timedelta(days=6) <= appt_date <= today:
                filtered.append(appt)

        elif period == "lastmonth":
            first_this = today.replace(day=1)
            last_prev  = first_this - timedelta(days=1)
            first_prev = last_prev.replace(day=1)
            if first_prev <= appt_date <= last_prev:
                filtered.append(appt)

        elif period == "lastyear":
            if appt_date.year == today.year - 1:
                filtered.append(appt)

    return filtered


# ══════════════════════════════════════════════════════════════════
#  TELEGRAM NOTIFICATION
# ══════════════════════════════════════════════════════════════════

def send_telegram_notification(bot_token, chat_id, message):
    """Send a Telegram message. Never raises."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        print(f"[Telegram] Status: {resp.status_code}")
    except Exception as e:
        print("Telegram Error:", e)


# ══════════════════════════════════════════════════════════════════
#  CHAT STATE MACHINE
# ══════════════════════════════════════════════════════════════════

STEPS = ["name", "phone", "date", "problem"]


def build_prompts():
    return {
        "name":    "Please enter your full name.",
        "phone":   "What is your contact phone number?",
        "date":    f"What date would you prefer?\n(e.g. {example_future_date()})",
        "problem": "Please briefly describe your symptoms or reason for the visit.",
    }


FAQ_KEYWORDS = {
    ("timing", "hours", "open", "schedule", "when"):    "timing",
    ("location", "address", "where", "directions"):     "location",
    ("fee", "fees", "cost", "charges", "price"):        "fees",
    ("contact", "reach", "call", "whatsapp", "phone"):  "contact",
}

SYMPTOM_KEYWORDS = (
    "cough", "flu", "fever", "cold", "headache", "pain", "nausea",
    "vomit", "diarrhea", "diarrhoea", "stomach", "throat", "runny",
    "sneeze", "chills", "fatigue", "tired", "sick", "ache", "aches",
)


def faq_answer(topic, clinic_id=DEFAULT_CLINIC_ID):
    c = get_clinic(clinic_id)
    answers = {
        "timing":   (f"{c.get('clinic_name','Clinic')} is open "
                     f"{c.get('clinic_days','')}, {c.get('clinic_timings','')}."),
        "location": f"We are located at {c.get('clinic_location','')}.",
        "fees":     (f"The consultation fee is {c.get('clinic_fees','')} per visit. "
                     f"We accept cash and all major cards."),
        "contact":  f"Please visit our clinic at {c.get('clinic_location','')} during opening hours.",
    }
    return answers.get(topic, "")


def detect_symptom(text):
    return any(kw in text.lower() for kw in SYMPTOM_KEYWORDS)


def match_faq(text, clinic_id=DEFAULT_CLINIC_ID):
    lower = text.lower()
    for keywords, topic in FAQ_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return faq_answer(topic, clinic_id)
    return None


def is_book_intent(text):
    return any(k in text.lower() for k in ("book", "appointment", "schedule", "reserve"))


def welcome_text(clinic_id=DEFAULT_CLINIC_ID):
    c = get_clinic(clinic_id)
    return (
        f"Welcome to {c.get('clinic_name','our clinic')}.\n"
        f"{CLINIC_CONFIG.get('clinic_tagline','')}\n\n"
        "I am your appointment assistant. I can help you:\n\n"
        "Book an appointment with our doctor\n"
        f"Check clinic hours ({c.get('clinic_days','')}, {c.get('clinic_timings','')})\n"
        f"Consultation fee: {c.get('clinic_fees','')}\n\n"
        "Use the quick buttons below or type your question."
    )


def idle_help_text(clinic_id=DEFAULT_CLINIC_ID):
    c = get_clinic(clinic_id)
    return (
        "Here is how I can help:\n\n"
        "Book Appointment — click the button or type 'book'\n"
        f"Hours — {c.get('clinic_days','')}, {c.get('clinic_timings','')}\n"
        f"Location — {c.get('clinic_location','')}\n"
        f"Fees — {c.get('clinic_fees','')} per visit"
    )


# ══════════════════════════════════════════════════════════════════
#  MAIN CHAT ENDPOINT
# ══════════════════════════════════════════════════════════════════

@app.route("/chat", methods=["POST"])
def chat():
    try:
        body      = request.get_json(silent=True) or {}
        message   = (body.get("message") or "").strip()
        session   = body.get("session") or {}

        step      = session.get("step", "idle")
        data      = session.get("data") or {}
        clinic_id = session.get("clinic_id") or body.get("clinic_id") or request.args.get("clinic") or DEFAULT_CLINIC_ID

        PROMPTS   = build_prompts()

        # ── Welcome ───────────────────────────────────────────────
        if step == "idle" and message == "":
            session.update({"step": "idle", "data": {}, "clinic_id": clinic_id})
            return jsonify(reply=welcome_text(clinic_id), session=session)

        msg_lower = message.lower().strip()

        # ── FAQ buttons ───────────────────────────────────────────
        if msg_lower in ("hours", "timing", "clinic hours"):
            return jsonify(reply=faq_answer("timing", clinic_id), session=session)
        if msg_lower in ("fees", "fee", "consultation fees"):
            return jsonify(reply=faq_answer("fees", clinic_id), session=session)
        if msg_lower in ("location", "address"):
            return jsonify(reply=faq_answer("location", clinic_id), session=session)

        # ── Symptom detection (idle only) ─────────────────────────
        if step == "idle" and detect_symptom(message):
            return jsonify(
                reply=(
                    "It seems like a common issue. "
                    "I recommend consulting the doctor. "
                    "Would you like to book an appointment?"
                ),
                session=session
            )

        # ── Booking flow ──────────────────────────────────────────
        if step in STEPS:
            try:
                if step == "phone" and not any(c.isdigit() for c in message):
                    return jsonify(
                        reply="Please enter a valid phone number (must contain digits).",
                        session=session
                    )

                if step == "date":
                    pending = session.get("pending_date")
                    if msg_lower in ("yes", "ok", "sure", "confirm", "okay") and pending:
                        message = pending
                    else:
                        parsed_date, date_error = validate_date(message, clinic_id)
                        if date_error:
                            return jsonify(reply=date_error, session=session)

                        records   = load_appointments(clinic_id=clinic_id)
                        formatted = format_date(parsed_date)
                        slot_lim  = get_clinic(clinic_id).get("daily_slot_limit", 10)

                        if count_booked(formatted, records) >= slot_lim:
                            search_from = max(
                                parsed_date + timedelta(days=1),
                                earliest_bookable_date(clinic_id)
                            )
                            next_d     = find_next_open_date(search_from, records, clinic_id)
                            next_d_str = format_date(next_d)
                            return jsonify(
                                reply=(
                                    f"The selected date ({formatted}) is fully booked.\n"
                                    f"The next available date is *{next_d_str}*.\n\n"
                                    f"Type 'yes' to confirm {next_d_str}, or enter a different date."
                                ),
                                session={**session, "pending_date": next_d_str}
                            )

                        message = formatted

                data[step]              = message
                session["data"]         = data
                session["clinic_id"]    = clinic_id
                session["pending_date"] = None

                idx = STEPS.index(step)
                if idx + 1 < len(STEPS):
                    next_step = STEPS[idx + 1]
                    session["step"] = next_step

                    if step == "date":
                        records   = load_appointments(clinic_id=clinic_id)
                        slot_lim  = get_clinic(clinic_id).get("daily_slot_limit", 10)
                        remaining = slot_lim - count_booked(message, records)
                        note = f"{remaining} slot{'s' if remaining != 1 else ''} available on this day.\n\n"
                        return jsonify(reply=note + PROMPTS[next_step], session=session)

                    return jsonify(reply=PROMPTS[next_step], session=session)

                # ── All steps done — assign slot, save, notify ────
                records       = load_appointments(clinic_id=clinic_id)
                assigned_time = next_free_slot(data["date"], records, clinic_id)

                if assigned_time is None:
                    parsed_appt_date = parse_user_date(data["date"]) or date.today()
                    next_d           = find_next_open_date(parsed_appt_date + timedelta(days=1), records, clinic_id)
                    next_d_str       = format_date(next_d)
                    assigned_time    = next_free_slot(next_d_str, records, clinic_id)

                    if assigned_time is None:
                        session.update({"step": "idle", "data": {}})
                        return jsonify(
                            reply="No slots found in the next 60 days. Please contact the clinic directly.",
                            session=session
                        )
                    data["date"] = next_d_str

                appt = {
                    **data,
                    "time":      assigned_time,
                    "clinic_id": clinic_id,
                    "id":        datetime.now().strftime("%Y%m%d%H%M%S"),
                    "booked_at": datetime.now().isoformat(),
                }

                saved = save_appointment(appt)

                if not saved:
                    session.update({"step": "idle", "data": {}})
                    return jsonify(
                        reply="Sorry, your appointment could not be saved. Please try again.",
                        session=session
                    )

                # Telegram notification
                try:
                    clinic_row = CLINICS.get(clinic_id)
                    if clinic_row:
                        dashboard_url = f"{get_base_url()}/admin/{clinic_id}"
                        send_telegram_notification(
                            clinic_row["telegram_bot_token"],
                            clinic_row["telegram_chat_id"],
                            f"New Appointment: {appt['name']}\nCheck Dashboard:\n{dashboard_url}"
                        )
                except Exception as tg_exc:
                    print("[Telegram] Error:", tg_exc)

                clinic_fees = get_clinic(clinic_id).get("clinic_fees", "")
                confirmation = (
                    "Appointment Received\n\n"
                    f"Name    : {appt['name']}\n"
                    f"Phone   : {appt['phone']}\n"
                    f"Date    : {appt['date']}\n"
                    f"Time    : {appt['time']}\n"
                    f"Problem : {appt['problem']}\n\n"
                    f"Your appointment has been confirmed for *{appt['date']}* at *{appt['time']}*.\n"
                    "Our team will contact you shortly.\n\n"
                    f"Consultation fee: {clinic_fees}\n"
                    "Please arrive 10 minutes before your appointment."
                )

                session.update({"step": "idle", "data": {}})
                return jsonify(
                    reply    = confirmation,
                    redirect = f"/receipt?name={appt['name']}&phone={appt['phone']}&date={appt['date']}&time={appt['time']}&problem={appt['problem']}&id={appt['id']}",
                    session  = session
                )

            except Exception as exc:
                print(f"[chat/booking-flow] {exc}")
                session.update({"step": "idle", "data": {}})
                return jsonify(
                    reply="Something went wrong. Please try again or type 'book' to start over.",
                    session=session
                )

        # ── Idle fallback ─────────────────────────────────────────
        try:
            faq = match_faq(message, clinic_id)
            if faq:
                return jsonify(reply=faq + "\n\nType 'book' to schedule an appointment.", session=session)

            if is_book_intent(message):
                session.update({"step": "name", "data": {}, "clinic_id": clinic_id})
                return jsonify(reply=f"Let us get your appointment scheduled.\n\n{PROMPTS['name']}", session=session)

            return jsonify(
                reply="How can I help you? You can book an appointment or ask about our services.\n\n" + idle_help_text(clinic_id),
                session=session
            )
        except Exception as exc:
            print(f"[chat/idle] {exc}")
            return jsonify(reply="How can I help you? You can book an appointment or ask about services.", session=session)

    except Exception as critical_exc:
        print(f"[chat] CRITICAL: {critical_exc}")
        return jsonify(reply="How can I help you? You can book an appointment or ask about services.", session={})


# ══════════════════════════════════════════════════════════════════
#  PAGE ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    # Pass the first clinic's config as default for the chatbot UI
    ctx = build_clinic_ctx(DEFAULT_CLINIC_ID)
    return render_template("index.html", clinic=ctx)


# ── Admin dashboard — UPDATED route: /admin/<clinic_id> ──────────
@app.route("/admin/<clinic_id>")
def admin(clinic_id):
    if clinic_id not in CLINICS:
        return f"Clinic '{clinic_id}' not found.", 404
    try:
        period      = request.args.get("filter", "all")
        all_records = load_appointments(clinic_id=clinic_id)
        filtered    = filter_appointments(all_records, period)

        slot_summary = {}
        for appt in all_records:
            d = appt.get("date", "Unknown")
            slot_summary[d] = slot_summary.get(d, 0) + 1

        today_count = len(filter_appointments(all_records, "today"))
        ctx         = build_clinic_ctx(clinic_id)

        return render_template(
            "admin.html",
            appointments  = filtered,
            all_count     = len(all_records),
            today_count   = today_count,
            clinic        = ctx,
            clinic_id     = clinic_id,
            slot_summary  = slot_summary,
            active_filter = period,
        )
    except Exception as exc:
        print(f"[admin/{clinic_id}] Error: {exc}")
        ctx = build_clinic_ctx(clinic_id)
        return render_template(
            "admin.html",
            appointments=[], all_count=0, today_count=0,
            clinic=ctx, clinic_id=clinic_id, slot_summary={}, active_filter="all",
        )


# ── Delete single appointment — UPDATED route ─────────────────────
@app.route("/admin/<clinic_id>/delete/<appt_id>", methods=["POST"])
def delete_appointment(clinic_id, appt_id):
    """Delete one appointment. Enforces clinic_id to prevent cross-clinic deletion."""
    if clinic_id not in CLINICS:
        return f"Clinic not found.", 404
    try:
        conn = get_db()
        conn.execute(
            "DELETE FROM appointments WHERE id = ? AND clinic_id = ?",
            (appt_id, clinic_id)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[delete] Error: {exc}")
    return redirect(f"/admin/{clinic_id}")


# ── Delete all appointments for a clinic ─────────────────────────
@app.route("/admin/<clinic_id>/delete_all", methods=["POST"])
def delete_all_appointments(clinic_id):
    """Delete all appointments for one clinic only."""
    if clinic_id not in CLINICS:
        return f"Clinic not found.", 404
    try:
        conn = get_db()
        conn.execute("DELETE FROM appointments WHERE clinic_id = ?", (clinic_id,))
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[delete_all] Error: {exc}")
    return redirect(f"/admin/{clinic_id}")


# ── Utility routes ────────────────────────────────────────────────
@app.route("/api/appointments")
def api_appointments():
    try:
        clinic_id = request.args.get("clinic", None)
        return jsonify(load_appointments(clinic_id=clinic_id))
    except Exception as exc:
        print(f"[api] {exc}")
        return jsonify([])


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/ping")
def ping():
    return "ok", 200


@app.route("/receipt")
def receipt():
    try:
        clinic_id = request.args.get("clinic", DEFAULT_CLINIC_ID)
        appt_data = {
            "name":    request.args.get("name",    ""),
            "phone":   request.args.get("phone",   ""),
            "date":    request.args.get("date",    ""),
            "time":    request.args.get("time",    ""),
            "problem": request.args.get("problem", ""),
            "id":      request.args.get("id",      ""),
        }
        if not any(appt_data.values()):
            return render_template("receipt.html", appointment=None,
                                   clinic=build_clinic_ctx(clinic_id),
                                   error="No appointment data found.")
        return render_template("receipt.html", appointment=appt_data,
                               clinic=build_clinic_ctx(clinic_id))
    except Exception as exc:
        print(f"[receipt] {exc}")
        return render_template("receipt.html", appointment=None,
                               clinic=build_clinic_ctx(DEFAULT_CLINIC_ID),
                               error="Something went wrong loading the receipt.")


@app.route("/success/<appt_id>")
def success(appt_id):
    try:
        conn = get_db()
        row  = conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        conn.close()
        if not row:
            return redirect("/")
        appt      = dict(row)
        clinic_id = appt.get("clinic_id", DEFAULT_CLINIC_ID)
        return render_template("receipt.html", appointment=appt,
                               clinic=build_clinic_ctx(clinic_id))
    except Exception as exc:
        print(f"[success] {exc}")
        return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)