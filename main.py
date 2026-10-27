import base64
import hashlib
import hmac
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException as StarletteHTTPException

app = FastAPI()
load_dotenv()

API_KEY = os.getenv("API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    seq BIGSERIAL PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    slot_id TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_per_slot
    ON bookings (slot_id) WHERE status = 'confirmed';
CREATE INDEX IF NOT EXISTS bookings_email ON bookings (email, seq);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    body_hash TEXT NOT NULL,
    status INT NOT NULL,
    response JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    email TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS attempts_email_at ON attempts (email, at);
"""

_ready = False


def seed_demo_slots(conn):
    base = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = []
    for day in range(1, 3):
        for hour in (9, 10, 11, 14, 15):
            start = base + timedelta(days=day, hours=hour - 9)
            end = start + timedelta(minutes=30)
            provider = "Dr Mike" if hour in (10, 15) else "Dr Ola"
            slot_id = f"s_{start.strftime('%Y%m%d_%H%M')}"
            rows.append((slot_id, provider, start, end))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO slots (id, provider, start_at, end_at) VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            rows,
        )

def db():
    """Open a connection. Use as `with db() as conn:` (commits on exit)."""
    global _ready
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    if not _ready:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(1)")
            fresh = conn.execute("SELECT to_regclass('public.slots') IS NULL AS fresh").fetchone()["fresh"]
            conn.execute(SCHEMA)
            if fresh:
                seed_demo_slots(conn)
        _ready = True
    return conn


def err(status, message, headers=None):
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def slot_time(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def created_time(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def booking_out(row):
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": created_time(row["created_at"]),
    }


async def read_json(request):
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


#  Level 2: API key on every route except the web page 

@app.middleware("http")
async def check_key(request: Request, call_next):
    if request.url.path != "/":
        key = request.headers.get("x-api-key", "")
        if not API_KEY or not hmac.compare_digest(key.encode(), API_KEY.encode()):
            return err(401, "missing or wrong API key")
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return err(exc.status_code, str(exc.detail))


#  Web page 

@app.get("/", include_in_schema=False)
def home():
    with db() as conn:
        has_future_available = conn.execute("""
            SELECT 1 FROM slots s
            WHERE s.start_at > now()
            AND NOT EXISTS (
                SELECT 1 FROM bookings b WHERE b.slot_id = s.id AND b.status = 'confirmed'
            )
            LIMIT 1
        """).fetchone()
        if not has_future_available:
            seed_demo_slots(conn)
    html = (Path(__file__).parent / "templates" / "index.html").read_text()
    key = json.dumps(API_KEY)[1:-1].replace("<", "\\u003c")
    return HTMLResponse(html.replace("__API_KEY__", key))


#  Level 1: slots 
@app.post("/reset")
async def reset(request: Request):
    data = await read_json(request)
    slots = data.get("slots") if data else None
    if not isinstance(slots, list):
        return err(400, "slots must be a list")

    rows, seen = [], set()
    for s in slots:
        try:
            slot_id, provider = s["id"], s["provider"]
            if not (isinstance(slot_id, str) and slot_id and isinstance(provider, str) and provider):
                raise ValueError
            if slot_id in seen:
                raise ValueError
            rows.append((slot_id, provider, parse_time(s["start"]), parse_time(s["end"])))
            seen.add(slot_id)
        except (KeyError, TypeError, ValueError, AttributeError):
            return err(400, "each slot needs a unique id, a provider, a start and an end")

    with db() as conn:
        conn.execute("TRUNCATE bookings, slots, idempotency, attempts")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO slots (id, provider, start_at, end_at) VALUES (%s, %s, %s, %s)", rows
            )
    return {"status": "ok"}


@app.get("/slots")
def list_slots(provider: str | None = None):
    sql = """
        SELECT s.*, NOT EXISTS (
            SELECT 1 FROM bookings b WHERE b.slot_id = s.id AND b.status = 'confirmed'
        ) AS available
        FROM slots s
    """
    args = ()
    if provider is not None:
        sql += " WHERE s.provider = %s"
        args = (provider,)
    sql += " ORDER BY s.start_at, s.id"
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return {
        "slots": [
            {
                "id": r["id"],
                "provider": r["provider"],
                "start": slot_time(r["start_at"]),
                "end": slot_time(r["end_at"]),
                "available": r["available"],
            }
            for r in rows
        ]
    }


#  Levels 1 to 5: booking 
def over_rate_limit(conn, email):
    """Returns seconds to wait if the email is over 5 attempts a minute, else records the attempt."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("rate:" + email,))
    row = conn.execute(
        """SELECT count(*) AS n, min(at) AS oldest, clock_timestamp() AS now
           FROM attempts WHERE email = %s AND at > clock_timestamp() - interval '60 seconds'""",
        (email,),
    ).fetchone()
    if row["n"] >= 5:
        age = (row["now"] - row["oldest"]).total_seconds()
        return max(math.ceil(60 - age), 1)
    conn.execute("INSERT INTO attempts (email) VALUES (%s)", (email,))
    return 0


def place_booking(conn, data, name, email):
    """Levels 1 to 3. Returns (status_code, body)."""
    slot_id = data.get("slot_id")
    if not (isinstance(slot_id, str) and slot_id.strip()) or not name or not email:
        return 400, {"error": "slot_id, name and email are required"}
    if not EMAIL_RE.match(email):
        return 400, {"error": "email is not valid"}

    slot = conn.execute("SELECT start_at FROM slots WHERE id = %s", (slot_id,)).fetchone()
    if not slot:
        return 404, {"error": "slot not found"}
    if slot["start_at"] <= datetime.now(timezone.utc):
        return 400, {"error": "slot has already started"}

    # The partial unique index lets only one confirmed booking exist per slot,
    # so two simultaneous requests cannot both win.
    row = conn.execute(
        """INSERT INTO bookings (id, slot_id, name, email, status)
           VALUES (%s, %s, %s, %s, 'confirmed')
           ON CONFLICT (slot_id) WHERE status = 'confirmed' DO NOTHING
           RETURNING *""",
        (str(uuid.uuid4()), slot_id, name, email),
    ).fetchone()
    if not row:
        return 409, {"error": "slot already booked"}
    return 201, booking_out(row)


@app.post("/bookings")
async def create_booking(request: Request):
    data = await read_json(request)
    if data is None:
        return err(400, "body must be a JSON object")

    name = data.get("name")
    name = name.strip() if isinstance(name, str) else ""
    email = data.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""

    key = request.headers.get("idempotency-key")
    body_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    with db() as conn:
        if key:
            # Level 4: the lock makes two simultaneous requests with one key run one after the other.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("idem:" + key,))
            old = conn.execute("SELECT * FROM idempotency WHERE key = %s", (key,)).fetchone()
            if old:
                if old["body_hash"] != body_hash:
                    return err(422, "Idempotency-Key was already used with a different body")
                return JSONResponse(old["response"], status_code=old["status"])

        # Level 5: every new attempt counts, even ones that fail validation.
        # A replay of an earlier request (above) is not a new attempt.
        if email:
            wait = over_rate_limit(conn, email)
            if wait:
                return err(429, "too many booking attempts, try again later", {"Retry-After": str(wait)})

        status, body = place_booking(conn, data, name, email)

        if key:
            conn.execute(
                "INSERT INTO idempotency (key, body_hash, status, response) VALUES (%s, %s, %s, %s)",
                (key, body_hash, status, Jsonb(body)),
            )
    return JSONResponse(body, status_code=status)


@app.get("/bookings")
def list_bookings(email: str | None = None, limit: str = "10", cursor: str | None = None):
    if not email or not email.strip():
        return err(400, "email is required")
    try:
        limit_n = int(limit)
        if not 1 <= limit_n <= 50:
            raise ValueError
    except ValueError:
        return err(400, "limit must be a whole number from 1 to 50")

    after = 0
    if cursor:
        try:
            after = int(base64.urlsafe_b64decode(cursor.encode()).decode())
        except Exception:
            return err(400, "invalid cursor")

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE email = %s AND seq > %s ORDER BY seq LIMIT %s",
            (email.strip().lower(), after, limit_n + 1),
        ).fetchall()

    page = rows[:limit_n]
    next_cursor = None
    if len(rows) > limit_n:
        next_cursor = base64.urlsafe_b64encode(str(page[-1]["seq"]).encode()).decode()
    return {"bookings": [booking_out(r) for r in page], "next_cursor": next_cursor}


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id = %s", (booking_id,)).fetchone()
    if not row:
        return err(404, "booking not found")
    return booking_out(row)


#  Level 3: cancel 

@app.delete("/bookings/{booking_id}")
def cancel_booking(booking_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id = %s FOR UPDATE", (booking_id,)).fetchone()
        if not row:
            return err(404, "booking not found")
        if row["status"] == "cancelled":
            return err(409, "booking is already cancelled")
        row = conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = %s RETURNING *", (booking_id,)
        ).fetchone()
    return booking_out(row)