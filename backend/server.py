#!/usr/bin/env python3
"""Yoga-Code backend. Python standard library only: no pip install needed.

Run:  python server.py   ->  open http://localhost:8000
Stores everything in backend/data/ (SQLite database + uploaded images).
"""
import hashlib, hmac, json, mimetypes, os, re, secrets, shutil, sqlite3, sys
import threading, time, traceback
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
FRONTEND = (BASE.parent / "frontend").resolve()
MEDIA = (BASE / "media").resolve()
DATA = Path(os.environ.get("YOGA_DATA", BASE / "data")).resolve()
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "yoga.db"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SECURE_COOKIE = os.environ.get("YOGA_SECURE", "0") == "1"  # set to 1 behind HTTPS

MAX_UPLOAD = 5 * 1024 * 1024
MAX_JSON = 64 * 1024
MAX_PHOTOS_PER_USER = 60
SESSION_DAYS = 30
COOKIE = "yoga_session"
LEVELS = {"beginner", "intermediate", "advanced"}
POSES = json.loads((BASE / "poses.json").read_text(encoding="utf-8"))
POSE_KEYS = {p["key"] for p in POSES}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,30}$")
GENDERS = {"female", "male", "non-binary", "prefer not to say"}
CONDITIONS = {"diabetes", "high blood pressure", "heart condition", "asthma", "back pain",
              "knee or joint problems", "arthritis", "thyroid", "pregnancy", "recent surgery",
              "injury", "none"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  username TEXT,
  name TEXT NOT NULL,
  pw_salt BLOB NOT NULL,
  pw_hash BLOB NOT NULL,
  goal TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL DEFAULT 'beginner',
  avatar TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  age INTEGER NOT NULL,
  gender TEXT NOT NULL,
  height_cm REAL NOT NULL,
  weight_kg REAL NOT NULL,
  has_history INTEGER NOT NULL,
  conditions TEXT NOT NULL DEFAULT '[]',
  details TEXT NOT NULL DEFAULT '',
  medications TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auth_tokens(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  pose TEXT NOT NULL,
  duration_sec INTEGER NOT NULL,
  best_hold_sec INTEGER NOT NULL,
  holds_completed INTEGER NOT NULL,
  day TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, created_at);
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  pose TEXT NOT NULL DEFAULT '',
  caption TEXT NOT NULL DEFAULT '',
  filename TEXT NOT NULL,
  created_at TEXT NOT NULL);
"""


# ---------------------------------------------------------------- helpers
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


_DUMMY_SALT = secrets.token_bytes(16)
_DUMMY_HASH = hash_password("dummy-password", _DUMMY_SALT)  # equalises login timing

_attempts, _lock = {}, threading.Lock()


def throttle(key, limit, window):
    """Raise 429 if `key` was hit `limit` times within `window` seconds."""
    t = time.time()
    with _lock:
        hits = [x for x in _attempts.get(key, []) if t - x < window]
        if len(hits) >= limit:
            raise ApiError(429, "Too many attempts. Please wait a few minutes and try again.")
        hits.append(t)
        _attempts[key] = hits


def clear_throttle(key):
    with _lock:
        _attempts.pop(key, None)


def sniff_image(b):
    if b[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ApiError(415, "Please upload a JPG, PNG or WebP image.")


def clean_text(v, field, maxlen, required=False):
    v = (v or "").strip() if isinstance(v, str) else ""
    if required and not v:
        raise ApiError(400, f"{field} is required.")
    if len(v) > maxlen:
        raise ApiError(400, f"{field} must be at most {maxlen} characters.")
    return v


def int_in(v, field, lo, hi):
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ApiError(400, f"{field} must be a whole number between {lo} and {hi}.")
    return v


def has_health(uid):
    with conn() as c:
        return c.execute("SELECT 1 FROM health WHERE user_id=?", (uid,)).fetchone() is not None


def health_dict(h):
    return {"age": h["age"], "gender": h["gender"], "height_cm": h["height_cm"], "weight_kg": h["weight_kg"],
            "has_history": bool(h["has_history"]), "conditions": json.loads(h["conditions"]),
            "details": h["details"], "medications": h["medications"], "updated_at": h["updated_at"]}


def user_dict(u):
    return {"id": u["id"], "email": u["email"], "username": u["username"] or u["name"],
            "onboarded": has_health(u["id"]), "name": u["name"], "goal": u["goal"],
            "level": u["level"], "has_avatar": bool(u["avatar"]), "avatar_v": u["avatar"] or "",
            "created_at": u["created_at"]}


def init_storage():
    DATA.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
        if "username" not in cols:  # upgrade a database from the previous version
            c.execute("ALTER TABLE users ADD COLUMN username TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(lower(username))")
        c.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (now(),))


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "YogaCode/1.0"

    # ----- low-level responses
    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")

    def send_json(self, data, status=200, extra_headers=()):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type=None, cache="no-cache"):
        try:
            body = Path(path).read_bytes()
        except OSError:
            raise ApiError(404, "Not found.")
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, limit):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise ApiError(411, "Content-Length is required.")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Bad Content-Length.")
        if n <= 0:
            raise ApiError(400, "Request body is empty.")
        if n > limit:
            left = n if n <= 4 * MAX_UPLOAD else 0  # drain modest bodies so the browser sees our reply
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            raise ApiError(413, "File too large (max 5 MB)." if limit == MAX_UPLOAD else "Request too large.")
        return self.rfile.read(n)

    def read_json(self):
        try:
            data = json.loads(self.read_body(MAX_JSON) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "JSON object expected.")
        return data

    # ----- auth
    def token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = cookies.SimpleCookie(raw)
        except cookies.CookieError:
            return None
        return jar[COOKIE].value if COOKIE in jar else None

    def current_user(self):
        tok = self.token()
        if not tok:
            return None
        th = hashlib.sha256(tok.encode()).hexdigest()
        with conn() as c:
            return c.execute(
                "SELECT u.* FROM auth_tokens t JOIN users u ON u.id=t.user_id "
                "WHERE t.token_hash=? AND t.expires_at>?", (th, now())).fetchone()

    def require_user(self):
        u = self.current_user()
        if not u:
            raise ApiError(401, "Please log in.")
        return u

    def cookie_header(self, value, max_age):
        parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if SECURE_COOKIE:
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def start_session(self, user_id):
        tok = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        with conn() as c:
            c.execute("INSERT INTO auth_tokens(token_hash,user_id,expires_at) VALUES(?,?,?)",
                      (hashlib.sha256(tok.encode()).hexdigest(), user_id, exp))
        return self.cookie_header(tok, SESSION_DAYS * 86400)

    # ----- dispatch
    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")
    def do_PATCH(self): self.dispatch("PATCH")
    def do_PUT(self): self.dispatch("PUT")
    def do_DELETE(self): self.dispatch("DELETE")

    def dispatch(self, method):
        try:
            url = urlparse(self.path)
            path = unquote(url.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if path.startswith("/api/"):
                if method != "GET":  # CSRF defence: custom header + same-origin check
                    if self.headers.get("X-Requested-With") != "yoga":
                        raise ApiError(403, "Missing X-Requested-With header.")
                    origin = self.headers.get("Origin")
                    if origin and urlparse(origin).netloc != self.headers.get("Host"):
                        raise ApiError(403, "Cross-origin request blocked.")
                self.api(method, path[5:].strip("/").split("/"), query)
            elif method == "GET":
                self.static(path)
            else:
                raise ApiError(405, "Method not allowed.")
        except ApiError as e:
            self.send_json({"error": e.message}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "Server error."}, 500)
            except Exception:
                pass

    # ----- static files (frontend + pose images)
    def static(self, path):
        if path.startswith("/media/"):
            root, rel = MEDIA, path[len("/media/"):]
        else:
            root, rel = FRONTEND, (path.lstrip("/") or "login.html")
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ApiError(404, "Not found.")
        if not target.is_file():
            raise ApiError(404, "Not found.")
        self.send_file(target)

    # ----- API routes
    def api(self, method, p, q):
        r = (method, p[0], len(p))
        if r == ("GET", "health", 1):
            return self.send_json({"ok": True})
        if r == ("GET", "poses", 1):
            return self.send_json({"poses": POSES})
        if r == ("POST", "auth", 2):
            return {"register": self.register, "login": self.login, "logout": self.logout}.get(
                p[1], self.not_found)()
        if p[0] == "me":
            return self.me_routes(method, p)
        if p[0] == "sessions":
            return self.session_routes(method, p, q)
        if r == ("GET", "stats", 1):
            return self.stats(q)
        if p[0] == "photos":
            return self.photo_routes(method, p, q)
        self.not_found()

    def not_found(self):
        raise ApiError(404, "Not found.")

    # ----- auth routes
    def register(self):
        ip = self.client_address[0]
        throttle(f"reg|{ip}", 10, 3600)
        d = self.read_json()
        email = clean_text(d.get("email"), "Email", 254, True).lower()
        username = clean_text(d.get("username"), "Username", 30, True)
        name = username
        pw = d.get("password")
        if not USERNAME_RE.match(username):
            raise ApiError(400, "Username must be 3 to 30 letters, numbers, dots or underscores.")
        if not EMAIL_RE.match(email):
            raise ApiError(400, "Please enter a valid email address.")
        if not isinstance(pw, str) or not 8 <= len(pw) <= 128:
            raise ApiError(400, "Password must be 8 to 128 characters.")
        salt = secrets.token_bytes(16)
        try:
            with conn() as c:
                if c.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (username,)).fetchone():
                    raise ApiError(409, "That username is taken. Please choose another.")
                cur = c.execute(
                    "INSERT INTO users(email,username,name,pw_salt,pw_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (email, username, name, salt, hash_password(pw, salt), now()))
                uid = cur.lastrowid
        except sqlite3.IntegrityError:
            raise ApiError(409, "An account with that email already exists.")
        cookie = self.start_session(uid)
        with conn() as c:
            u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        self.send_json({"user": user_dict(u)}, 201, [cookie])

    def login(self):
        d = self.read_json()
        email = clean_text(d.get("email"), "Email or username", 254).lower()
        pw = d.get("password") if isinstance(d.get("password"), str) else ""
        key = f"login|{self.client_address[0]}|{email}"
        throttle(key, 8, 600)
        with conn() as c:
            u = c.execute("SELECT * FROM users WHERE email=? OR lower(username)=?", (email, email)).fetchone()
        salt, good = (u["pw_salt"], u["pw_hash"]) if u else (_DUMMY_SALT, _DUMMY_HASH)
        ok = hmac.compare_digest(hash_password(pw[:128], salt), good) and u is not None
        if not ok:
            raise ApiError(401, "Incorrect email or password.")
        clear_throttle(key)
        self.send_json({"user": user_dict(u)}, 200, [self.start_session(u["id"])])

    def logout(self):
        tok = self.token()
        if tok:
            with conn() as c:
                c.execute("DELETE FROM auth_tokens WHERE token_hash=?",
                          (hashlib.sha256(tok.encode()).hexdigest(),))
        self.send_json({"ok": True}, 200, [self.cookie_header("", 0)])

    # ----- profile routes
    def me_routes(self, method, p):
        if method == "GET" and p == ["me"]:
            u = self.current_user()
            return self.send_json({"user": user_dict(u) if u else None})
        u = self.require_user()
        if p == ["me", "health"]:
            return self.health_routes(method, u)
        if method == "PATCH" and p == ["me"]:
            d = self.read_json()
            name = clean_text(d.get("name"), "Name", 60, True)
            goal = clean_text(d.get("goal"), "Goal", 200)
            level = d.get("level")
            if level not in LEVELS:
                raise ApiError(400, "Level must be beginner, intermediate or advanced.")
            with conn() as c:
                c.execute("UPDATE users SET name=?, goal=?, level=? WHERE id=?", (name, goal, level, u["id"]))
                u = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
            return self.send_json({"user": user_dict(u)})
        if p == ["me", "avatar"]:
            if method == "GET":
                if not u["avatar"]:
                    raise ApiError(404, "No avatar.")
                return self.send_stored(u["id"], u["avatar"])
            if method == "POST":
                data = self.read_body(MAX_UPLOAD)
                ext, _ = sniff_image(data)
                name = f"avatar-{secrets.token_hex(8)}.{ext}"
                self.store_file(u["id"], name, data)
                if u["avatar"]:
                    self.remove_file(u["id"], u["avatar"])
                with conn() as c:
                    c.execute("UPDATE users SET avatar=? WHERE id=?", (name, u["id"]))
                    u = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
                return self.send_json({"user": user_dict(u)})
        if method == "GET" and p == ["me", "export"]:
            with conn() as c:
                sess = [dict(r) for r in c.execute("SELECT * FROM sessions WHERE user_id=? ORDER BY id", (u["id"],))]
                phs = [dict(r) for r in c.execute(
                    "SELECT id,pose,caption,created_at FROM photos WHERE user_id=? ORDER BY id", (u["id"],))]
                h = c.execute("SELECT * FROM health WHERE user_id=?", (u["id"],)).fetchone()
            body = json.dumps({"profile": user_dict(u), "health": health_dict(h) if h else None,
                               "sessions": sess, "photos": phs}, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="yoga-code-data.json"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._common_headers()
            self.end_headers()
            return self.wfile.write(body)
        if method == "POST" and p == ["me", "delete"]:
            d = self.read_json()
            pw = d.get("password") if isinstance(d.get("password"), str) else ""
            throttle(f"del|{u['id']}", 5, 600)
            if not hmac.compare_digest(hash_password(pw[:128], u["pw_salt"]), u["pw_hash"]):
                raise ApiError(401, "Incorrect password.")
            with conn() as c:
                c.execute("DELETE FROM users WHERE id=?", (u["id"],))  # cascades to all rows
            shutil.rmtree(UPLOADS / str(u["id"]), ignore_errors=True)
            return self.send_json({"ok": True}, 200, [self.cookie_header("", 0)])
        self.not_found()

    # ----- health profile (asked once after first login; editable later)
    def health_routes(self, method, u):
        if method == "GET":
            with conn() as c:
                h = c.execute("SELECT * FROM health WHERE user_id=?", (u["id"],)).fetchone()
            return self.send_json({"health": health_dict(h) if h else None})
        if method != "PUT":
            self.not_found()
        d = self.read_json()
        age = int_in(d.get("age"), "Age", 5, 120)
        gender = d.get("gender")
        if gender not in GENDERS:
            raise ApiError(400, "Please choose a gender option.")
        try:
            height = round(float(d.get("height_cm")), 1)
            weight = round(float(d.get("weight_kg")), 1)
        except (TypeError, ValueError):
            raise ApiError(400, "Height and weight must be numbers.")
        if not 50 <= height <= 260:
            raise ApiError(400, "Height must be between 50 and 260 cm.")
        if not 15 <= weight <= 400:
            raise ApiError(400, "Weight must be between 15 and 400 kg.")
        has_history = d.get("has_history")
        if not isinstance(has_history, bool):
            raise ApiError(400, "Please answer whether you have any past medical history.")
        conds = d.get("conditions") if has_history else []
        if not isinstance(conds, list) or any(c not in CONDITIONS for c in conds):
            raise ApiError(400, "Unknown condition selected.")
        conds = [c for c in dict.fromkeys(conds) if c != "none"]
        details = clean_text(d.get("details"), "Details", 1000) if has_history else ""
        meds = clean_text(d.get("medications"), "Medications", 300) if has_history else ""
        with conn() as c:
            c.execute("INSERT INTO health(user_id,age,gender,height_cm,weight_kg,has_history,conditions,details,medications,updated_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET age=excluded.age,"
                      " gender=excluded.gender, height_cm=excluded.height_cm, weight_kg=excluded.weight_kg,"
                      " has_history=excluded.has_history, conditions=excluded.conditions, details=excluded.details,"
                      " medications=excluded.medications, updated_at=excluded.updated_at",
                      (u["id"], age, gender, height, weight, int(has_history), json.dumps(conds), details, meds, now()))
            h = c.execute("SELECT * FROM health WHERE user_id=?", (u["id"],)).fetchone()
        return self.send_json({"health": health_dict(h), "user": user_dict(u)})

    # ----- file storage (per-user folders, random names, owner-only access)
    def store_file(self, uid, name, data):
        folder = UPLOADS / str(uid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)

    def remove_file(self, uid, name):
        try:
            (UPLOADS / str(uid) / name).unlink()
        except OSError:
            pass

    def send_stored(self, uid, name):
        path = (UPLOADS / str(uid) / name).resolve()
        try:
            path.relative_to((UPLOADS / str(uid)).resolve())
        except ValueError:
            raise ApiError(404, "Not found.")
        self.send_file(path, sniff_image(path.read_bytes()[:16])[1] if path.is_file() else None,
                       cache="private, max-age=3600")

    # ----- practice sessions
    def session_routes(self, method, p, q):
        u = self.require_user()
        if method == "GET" and len(p) == 1:
            try:
                limit = max(1, min(int(q.get("limit", 50)), 200))
            except ValueError:
                limit = 50
            with conn() as c:
                rows = c.execute("SELECT * FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                 (u["id"], limit)).fetchall()
            return self.send_json({"sessions": [dict(r) for r in rows]})
        if method == "POST" and len(p) == 1:
            d = self.read_json()
            pose = d.get("pose")
            if pose not in POSE_KEYS:
                raise ApiError(400, "Unknown pose.")
            dur = int_in(d.get("duration_sec"), "duration_sec", 1, 43200)
            best = int_in(d.get("best_hold_sec"), "best_hold_sec", 0, dur)
            holds = int_in(d.get("holds_completed"), "holds_completed", 0, 1000)
            try:
                day = date.fromisoformat(str(d.get("day")))
            except ValueError:
                raise ApiError(400, "day must be YYYY-MM-DD.")
            if abs((day - datetime.now(timezone.utc).date()).days) > 2:
                raise ApiError(400, "day is out of range.")
            note = clean_text(d.get("note"), "Note", 300)
            with conn() as c:
                cur = c.execute(
                    "INSERT INTO sessions(user_id,pose,duration_sec,best_hold_sec,holds_completed,day,note,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)", (u["id"], pose, dur, best, holds, day.isoformat(), note, now()))
                row = c.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone()
            return self.send_json({"session": dict(row)}, 201)
        if method == "DELETE" and len(p) == 2 and p[1].isdigit():
            with conn() as c:
                n = c.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (int(p[1]), u["id"])).rowcount
            if not n:
                raise ApiError(404, "Session not found.")
            return self.send_json({"ok": True})
        self.not_found()

    def stats(self, q):
        u = self.require_user()
        try:
            today = date.fromisoformat(q.get("today", ""))
        except ValueError:
            today = datetime.now(timezone.utc).date()
        with conn() as c:
            rows = c.execute("SELECT pose,duration_sec,best_hold_sec,holds_completed,day FROM sessions "
                             "WHERE user_id=?", (u["id"],)).fetchall()
        days, per_pose, by_day = set(), {}, {}
        for r in rows:
            days.add(r["day"])
            by_day[r["day"]] = by_day.get(r["day"], 0) + r["duration_sec"]
            s = per_pose.setdefault(r["pose"], {"pose": r["pose"], "sessions": 0, "total_sec": 0, "best_hold_sec": 0})
            s["sessions"] += 1
            s["total_sec"] += r["duration_sec"]
            s["best_hold_sec"] = max(s["best_hold_sec"], r["best_hold_sec"])
        cursor = today if today.isoformat() in days else today - timedelta(days=1)
        streak = 0
        while cursor.isoformat() in days:
            streak += 1
            cursor -= timedelta(days=1)
        week = [{"day": (today - timedelta(days=i)).isoformat(),
                 "minutes": round(by_day.get((today - timedelta(days=i)).isoformat(), 0) / 60, 1)}
                for i in range(6, -1, -1)]
        self.send_json({
            "total_sessions": len(rows),
            "total_minutes": round(sum(r["duration_sec"] for r in rows) / 60, 1),
            "best_hold_sec": max((r["best_hold_sec"] for r in rows), default=0),
            "streak_days": streak, "week": week, "per_pose": sorted(per_pose.values(), key=lambda s: -s["sessions"])})

    # ----- progress photos
    def photo_routes(self, method, p, q):
        u = self.require_user()
        if method == "GET" and len(p) == 1:
            with conn() as c:
                rows = c.execute("SELECT id,pose,caption,created_at FROM photos WHERE user_id=? ORDER BY id DESC",
                                 (u["id"],)).fetchall()
            return self.send_json({"photos": [dict(r, url=f"/api/photos/{r['id']}/file") for r in rows]})
        if method == "POST" and len(p) == 1:
            pose = q.get("pose", "")
            if pose and pose not in POSE_KEYS:
                raise ApiError(400, "Unknown pose.")
            caption = clean_text(q.get("caption", ""), "Caption", 200)
            with conn() as c:
                count = c.execute("SELECT COUNT(*) FROM photos WHERE user_id=?", (u["id"],)).fetchone()[0]
            if count >= MAX_PHOTOS_PER_USER:
                raise ApiError(409, f"Photo limit reached ({MAX_PHOTOS_PER_USER}). Delete one to add another.")
            data = self.read_body(MAX_UPLOAD)
            ext, _ = sniff_image(data)
            name = f"{secrets.token_hex(12)}.{ext}"
            self.store_file(u["id"], name, data)
            with conn() as c:
                cur = c.execute("INSERT INTO photos(user_id,pose,caption,filename,created_at) VALUES(?,?,?,?,?)",
                                (u["id"], pose, caption, name, now()))
            return self.send_json({"photo": {"id": cur.lastrowid, "pose": pose, "caption": caption,
                                             "url": f"/api/photos/{cur.lastrowid}/file"}}, 201)
        if len(p) >= 2 and p[1].isdigit():
            with conn() as c:
                row = c.execute("SELECT * FROM photos WHERE id=? AND user_id=?", (int(p[1]), u["id"])).fetchone()
            if not row:
                raise ApiError(404, "Photo not found.")
            if method == "GET" and p[2:] == ["file"]:
                return self.send_stored(u["id"], row["filename"])
            if method == "DELETE" and len(p) == 2:
                with conn() as c:
                    c.execute("DELETE FROM photos WHERE id=?", (row["id"],))
                self.remove_file(u["id"], row["filename"])
                return self.send_json({"ok": True})
        self.not_found()


def main():
    init_storage()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Yoga-Code running at http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}  (Ctrl+C to stop)")
    print(f"Data folder: {DATA}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
