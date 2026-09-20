#!/usr/bin/env python3
"""Eddy — local chat backend.

Serves eddy.html and forwards chat requests to a Llama model running in
Ollama on this same machine. Everything is bound to 127.0.0.1, so nothing
leaves the PC.

Run with:
    python server.py
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

HOST = "127.0.0.1"
PORT = int(os.environ.get("EDDY_PORT", "8000"))
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("EDDY_MODEL", "llama3.2:3b")

SYSTEM_PROMPT = (
    "You are Eddy, a helpful local AI assistant.\n\n"
    "Give clear, useful and accurate answers.\n"
    "Do not pretend to have access to information you do not have.\n"
    "When writing code, use code blocks.\n"
    "Keep responses reasonably concise unless the user asks for detail."
)

# Modes are generation limits only; the model itself never changes.
MODE_OPTIONS = {
    "quick":   {"num_predict": 200, "num_ctx": 4096},
    "default": {"num_predict": 1024, "num_ctx": 4096},
    "complex": {"num_predict": 4096, "num_ctx": 8192},
}

# http.server protocol: "HTTP/1.0" closes the connection at the end of the
# response, which is how streaming JSON lines are delimited for the browser.
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# Conversation logging. Set EDDY_LOG=0 to turn the file off.
LOG_ENABLED = os.environ.get("EDDY_LOG", "1").strip().lower() not in ("0", "false", "no")
LOG_FILE = os.environ.get("EDDY_LOG_FILE") or os.path.join(BASE_DIR, "chat_log.json")
_log_lock = threading.Lock()
_print_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Local account store (prototype). Accounts and sessions live in plain JSON
# files under data/; passwords are PBKDF2-hashed, never stored in plaintext.
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
PROFILE_DIR = os.path.join(DATA_DIR, "profile-pictures")
MAX_PROFILE_PIC = 2 * 1000 * 1000
_auth_lock = threading.RLock()
_PBKDF2_ROUNDS = 180_000

_EMAIL_RE = re.compile(r"^[^@\s]{1,254}@[^@\s]{1,254}\.[^@\s]+$")

AVATAR_COLORS = [
    "#E5484D", "#F76B15", "#FFC53D", "#46A758", "#12A594",
    "#0091FF", "#3E63DD", "#5E4BC9", "#8E4EC6", "#E93D82", "#AD7F58",
]


def _ensure_data_dir():
    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    _ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_users():
    data = _read_json(USERS_FILE, {})
    users = data.get("users", []) if isinstance(data, dict) else []
    return users if isinstance(users, list) else []


def save_users(users):
    _write_json(USERS_FILE, {"version": 1, "users": users})


def load_sessions():
    data = _read_json(SESSIONS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_sessions(sessions):
    _write_json(SESSIONS_FILE, sessions)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return salt.hex(), digest.hex()


def verify_password(password, salt_hex, hash_hex):
    try:
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(hash_hex)
    except (TypeError, ValueError):
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return hmac.compare_digest(check, digest)


def avatar_color_for(uid):
    digest = hashlib.sha256((uid or "").encode("utf-8")).digest()
    return AVATAR_COLORS[digest[0] % len(AVATAR_COLORS)]


def public_user(user):
    color = user.get("avatarColor") or avatar_color_for(user.get("uid", ""))
    return {
        "uid": user["uid"],
        "name": user["name"],
        "ident": user["ident"],
        "avatarColor": color,
        "profilePicture": user.get("profilePicture") or "",
        "profilePictureVer": int(user.get("pictureVer") or 0),
    }


def sniff_image(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def profile_file(ref):
    if not ref or not ref.startswith("profile-pictures/"):
        return None
    name = ref[len("profile-pictures/"):]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    return os.path.join(PROFILE_DIR, name)


def _echo(text):
    """Write to the console without letting threads interleave mid-line."""
    with _print_lock:
        sys.stdout.write(text)
        sys.stdout.flush()


def print_prompt(entry):
    lines = [
        "",
        "-" * 68,
        f"{entry['time']}  model={entry['model']}  mode={entry['mode']}  "
        f"{'stream' if entry['stream'] else 'full'}",
    ]
    for m in entry["messages"]:
        lines.append(f"{m['role']:>9}: {m['content']}")
    _echo("\n".join(lines) + "\nassistant: ")


def write_log(entry):
    """Append one exchange to the JSON log file (a plain JSON array)."""
    if not LOG_ENABLED:
        return
    try:
        with _log_lock:
            entries = []
            if os.path.isfile(LOG_FILE):
                try:
                    with open(LOG_FILE, "r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, list):
                        entries = loaded
                except (OSError, ValueError):
                    entries = []
            entries.append(entry)
            tmp = LOG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, LOG_FILE)
    except OSError as exc:
        print(f"[eddy] Could not write {LOG_FILE}: {exc}")


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def ollama_tags():
    """Return the set of installed model names (empty if Ollama is down)."""
    try:
        with urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {m.get("name", "") for m in data.get("models", [])}
    except OSError:
        return set()


def ollama_reachable():
    try:
        with urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=3):
            return True
    except OSError:
        return False


def model_installed():
    tags = ollama_tags()
    return MODEL in tags or MODEL + ":latest" in tags


def ollama_stream(path, payload, timeout=180):
    """Yield parsed JSON objects from an Ollama API POST (stream mode)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_HOST + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw.decode("utf-8"))
            except ValueError:
                continue


def find_ollama():
    """Path to the ollama command, or None if it is not installed."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidate = os.path.join(local, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def pull_model():
    print(f"Pulling model '{MODEL}' (first run only). This can take a few minutes...")
    last = None
    try:
        for obj in ollama_stream("/api/pull", {"name": MODEL, "stream": True}, timeout=60):
            if "error" in obj:
                print(f"Pull failed: {obj['error']}")
                return False
            status = obj.get("status", "")
            if status == "success":
                print("  Model ready.")
                return True
            if status != last:
                last = status
                if obj.get("total"):
                    pct = int(100 * (obj.get("completed", 0) / obj["total"]))
                    print(f"  {pct:3d}%  {obj['status']}")
                else:
                    print(f"  {obj['status']}")
    except OSError as exc:
        print(f"  Could not reach Ollama to pull the model: {exc}")
    return False


def prepare_model():
    """Check Ollama is present and running, and that the model is pulled."""
    if not find_ollama() and not ollama_reachable():
        print("Ollama was not found.\n")
        print("Install Ollama and restart Eddy.")
        return False
    if not ollama_reachable():
        print("Ollama is installed but not running.\n")
        print("Start Ollama and restart Eddy.")
        return False
    if not model_installed():
        if not pull_model():
            print("Could not download the model. Is Ollama running and online?")
            return False
    return True


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

class EddyRequestHandler(BaseHTTPRequestHandler):
    server_version = "Eddy/1.0"

    # -- response helpers --------------------------------------------------

    def send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_ndjson(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return None
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError):
            return None

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/eddy.html"):
            self.serve_ui()
        elif path in ("/icon.svg", "/favicon.svg", "/favicon.ico"):
            self.serve_icon()
        elif path == "/health":
            self.send_json(200, {"ok": True, "model": MODEL, "ready": ollama_reachable()})
        elif path == "/api/auth/me":
            self.handle_me()
        elif path == "/api/auth/profile-picture":
            self.handle_get_profile_picture()
        else:
            self.send_json(404, {"error": {"code": "not_found", "message": "Not found."}})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/chat":
            self.handle_chat()
            return
        routes = {
            "/api/auth/register": self.handle_register,
            "/api/auth/login": self.handle_login,
            "/api/auth/logout": self.handle_logout,
            "/api/auth/logout_all": self.handle_logout_all,
            "/api/auth/me": self.handle_update_me,
            "/api/auth/profile-picture": self.handle_upload_profile_picture,
        }
        handler = routes.get(path)
        if handler is None:
            self.send_json(404, {"error": {"code": "not_found", "message": "Not found."}})
            return
        handler()

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/account":
            self.handle_delete_account()
            return
        if path == "/api/auth/profile-picture":
            self.handle_delete_profile_picture()
            return
        self.send_json(404, {"error": {"code": "not_found", "message": "Not found."}})

    def serve_icon(self):
        icon = os.path.join(BASE_DIR, "icon.svg")
        try:
            with open(icon, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_json(404, {"error": {"code": "not_found",
                                           "message": "icon.svg not found next to server.py."}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def serve_ui(self):
        ui = os.path.join(BASE_DIR, "eddy.html")
        try:
            with open(ui, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_json(500, {"error": {"code": "missing_ui",
                                           "message": "eddy.html not found next to server.py."}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- auth --------------------------------------------------------------

    def _auth_error(self, code, message, status=400):
        self.send_json(status, {"error": {"code": code, "message": message}})

    def _bearer_token(self):
        auth = self.headers.get("Authorization", "") or ""
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        body = self.read_json_body()
        token = body.get("token") if isinstance(body, dict) else None
        return token or None

    def _session_user(self, token):
        if not token:
            return None
        with _auth_lock:
            sessions = load_sessions()
            rec = sessions.get(token)
            if not rec:
                return None
            uid = rec.get("uid")
            for user in load_users():
                if user.get("uid") == uid:
                    return user
        return None

    @staticmethod
    def _issue_session(uid):
        token = secrets.token_urlsafe(32)
        with _auth_lock:
            sessions = load_sessions()
            sessions[token] = {"uid": uid, "createdAt": int(time.time())}
            save_sessions(sessions)
        return token

    def _drop_session(self, token):
        with _auth_lock:
            sessions = load_sessions()
            if token in sessions:
                del sessions[token]
                save_sessions(sessions)

    def handle_register(self):
        body = self.read_json_body() or {}
        ident = str(body.get("ident", "")).strip()
        password = str(body.get("password", "") or "")
        name = str(body.get("name", "")).strip()

        if not _EMAIL_RE.match(ident):
            return self._auth_error(
                "invalid_email",
                "Enter a valid email address.",
            )
        if len(password) < 6:
            return self._auth_error(
                "weak_password", "The password must be at least 6 characters long.")
        if not name:
            name = ident
        if len(name) > 60:
            return self._auth_error(
                "invalid_name", "The name must be 1-60 characters.")

        with _auth_lock:
            users = load_users()
            if any(u.get("ident", "").lower() == ident.lower() for u in users):
                return self._auth_error(
                    "identifier_taken", "An account with that email already exists.",
                    status=409)
            salt, digest = hash_password(password)
            user = {
                "uid": secrets.token_hex(8),
                "name": name,
                "ident": ident,
                "salt": salt,
                "hash": digest,
                "avatarColor": secrets.choice(AVATAR_COLORS),
                "createdAt": int(time.time()),
            }
            users.append(user)
            save_users(users)
            token = self._issue_session(user["uid"])
        return self.send_json(200, {"token": token, "user": public_user(user)})

    def handle_login(self):
        body = self.read_json_body() or {}
        ident = str(body.get("ident", "")).strip()
        password = str(body.get("password", "") or "")
        with _auth_lock:
            user = next(
                (u for u in load_users() if u.get("ident", "").lower() == ident.lower()),
                None,
            )
            if user is None or not verify_password(password, user.get("salt", ""), user.get("hash", "")):
                return self._auth_error(
                    "invalid_credentials", "The login identifier or password is wrong.")
            token = self._issue_session(user["uid"])
        return self.send_json(200, {"token": token, "user": public_user(user)})

    def handle_me(self):
        user = self._session_user(self._bearer_token())
        if user is None:
            return self._auth_error("unauthorized", "Not signed in.", status=401)
        return self.send_json(200, {"user": public_user(user)})

    def handle_update_me(self):
        user = self._session_user(self._bearer_token())
        if user is None:
            return self._auth_error("unauthorized", "Not signed in.", status=401)
        body = self.read_json_body() or {}
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 60:
            return self._auth_error("invalid_name", "The name must be 1-60 characters.")
        with _auth_lock:
            users = load_users()
            for u in users:
                if u["uid"] == user["uid"]:
                    u["name"] = name[:60]
                    save_users(users)
                    return self.send_json(200, {"user": public_user(u)})
        return self._auth_error("unauthorized", "Not signed in.", status=401)

    def handle_logout(self):
        token = self._bearer_token()
        if token:
            self._drop_session(token)
        return self.send_json(200, {"ok": True})

    def handle_logout_all(self):
        user = self._session_user(self._bearer_token())
        if user is None:
            return self._auth_error("unauthorized", "Not signed in.", status=401)
        with _auth_lock:
            sessions = load_sessions()
            kept = {k: v for k, v in sessions.items() if v.get("uid") != user["uid"]}
            save_sessions(kept)
        return self.send_json(200, {"ok": True})

    def handle_delete_account(self):
        user = self._session_user(self._bearer_token())
        if user is None:
            return self._auth_error("unauthorized", "Not signed in.", status=401)
        body = self.read_json_body() or {}
        password = str(body.get("password", "") or "")
        if not verify_password(password, user.get("salt", ""), user.get("hash", "")):
            return self._auth_error("invalid_credentials", "The password is wrong.")
        self._remove_profile_picture_file(user["uid"])
        with _auth_lock:
            users = load_users()
            users = [u for u in users if u["uid"] != user["uid"]]
            save_users(users)
            sessions = load_sessions()
            kept = {k: v for k, v in sessions.items() if v.get("uid") != user["uid"]}
            save_sessions(kept)
        return self.send_json(200, {"ok": True})

    def _query_param(self, name):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        for pair in qs.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k == name:
                    return v
        return None

    def _remove_profile_picture_file(self, uid):
        try:
            for f in os.listdir(PROFILE_DIR):
                if f.startswith(uid + "."):
                    os.remove(os.path.join(PROFILE_DIR, f))
        except OSError:
            pass

    def handle_upload_profile_picture(self):
        user = self._session_user(self._bearer_token())
        if user is None:
            return self._auth_error("unauthorized", "Not signed in.", status=401)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._auth_error("bad_request", "Empty request body.")
        toss = min(length, MAX_PROFILE_PIC * 4)
        data = b""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            if len(data) < toss:
                data += chunk if len(data) + len(chunk) <= toss else chunk[:toss - len(data)]
            remaining -= len(chunk)
        if length > MAX_PROFILE_PIC:
            return self._auth_error(
                "payload_too_large", "The image is too large.", status=413)
        ext = sniff_image(data)
        if ext is None:
            return self._auth_error(
                "unsupported_media", "Only PNG, JPEG and WebP images are allowed.",
                status=415)
        os.makedirs(PROFILE_DIR, exist_ok=True)
        uid = user["uid"]
        self._remove_profile_picture_file(uid)
        with open(os.path.join(PROFILE_DIR, uid + ext), "wb") as fh:
            fh.write(data)
        ref = "profile-pictures/" + uid + ext
        with _auth_lock:
            users = load_users()
            updated = None
            for u in users:
                if u["uid"] == uid:
                    u["profilePicture"] = ref
                    u["pictureVer"] = int(u.get("pictureVer") or 0) + 1
                    save_users(users)
                    updated = public_user(u)
                    break
        return self.send_json(200, {"profilePicture": ref, "user": updated})

    def handle_get_profile_picture(self):
        token = self._query_param("token") or self._bearer_token()
        user = self._session_user(token)
        if user is None:
            return self._auth_error("unauthorized", "Not signed in.", status=401)
        ref = user.get("profilePicture") or ""
        path = profile_file(ref)
        if not path or not os.path.isfile(path):
            return self._auth_error("not_found", "No profile picture.", status=404)
        with open(path, "rb") as fh:
            body = fh.read()
        ext = os.path.splitext(path)[1].lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
                    ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_delete_profile_picture(self):
        user = self._session_user(self._bearer_token())
        if user is None:
            return self._auth_error("unauthorized", "Not signed in.", status=401)
        self._remove_profile_picture_file(user["uid"])
        with _auth_lock:
            users = load_users()
            updated = None
            for u in users:
                if u["uid"] == user["uid"]:
                    u.pop("profilePicture", None)
                    u["pictureVer"] = int(u.get("pictureVer") or 0) + 1
                    save_users(users)
                    updated = public_user(u)
                    break
        return self.send_json(200, {"ok": True, "user": updated})

    # -- chat --------------------------------------------------------------

    def handle_chat(self):
        data = self.read_json_body()
        if data is None:
            return self.send_json(400, {"error": {"code": "bad_request",
                                                  "message": "Body must be valid JSON."}})

        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return self.send_json(400, {"error": {"code": "bad_request",
                                                  "message": "'messages' must be a non-empty list."}})

        cleaned = []
        for m in messages:
            if not isinstance(m, dict):
                return self.send_json(400, {"error": {"code": "bad_request",
                                                      "message": "Messages must be objects with role and content."}})
            role = m.get("role")
            content = m.get("content")
            if role not in ("system", "user", "assistant") or not isinstance(content, str):
                return self.send_json(400, {"error": {"code": "bad_request",
                                                      "message": "Role must be system/user/assistant and content must be a string."}})
            cleaned.append({"role": role, "content": content})

        mode = data.get("mode", "default")
        options = MODE_OPTIONS.get(mode, MODE_OPTIONS["default"])

        system = SYSTEM_PROMPT
        conversation = []
        for m in cleaned:
            if m["role"] == "system":
                system += "\n\n" + m["content"]
            else:
                conversation.append(m)

        payload = {
            "model": MODEL,
            "messages": [{"role": "system", "content": system}] + conversation,
            "stream": True,
            "options": options,
        }

        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": MODEL,
            "mode": mode,
            "stream": bool(data.get("stream", True)),
            "system": system,
            "messages": conversation,
            "reply": "",
            "error": None,
        }
        print_prompt(entry)

        # Fetch the first token before replying so connection failures surface
        # as a clean error response instead of a broken stream.
        try:
            gen = self._chat(payload)
            first = next(gen)
        except StopIteration:
            return self.finish_error(entry, "Ollama returned no data.", 502,
                                     {"code": "offline", "message": "Ollama returned no data."})
        except OSError as exc:
            message = self._upstream_message(exc)
            return self.finish_error(entry, message, 502, {"code": "offline", "message": message})

        if not entry["stream"]:
            text, err = self._collect(gen)
            entry["reply"] = text
            _echo(text + "\n")
            if err:
                entry["error"] = err["error"]["message"]
                write_log(entry)
                return self.send_json(502, err)
            write_log(entry)
            return self.send_json(200, {"message": text})

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        parts = []
        try:
            status = self._relay(first, parts)
            while status is None:
                status = self._relay(next(gen), parts)
            if status != "done":
                entry["error"] = status
        except StopIteration:
            pass
        except _CLIENT_GONE:
            entry["error"] = "stopped by the client"
            _echo("\n")
        except OSError as exc:
            message = self._upstream_message(exc)
            entry["error"] = message
            self._try_ndjson_error(exc)
            _echo("\n")
        finally:
            gen.close()
            entry["reply"] = "".join(parts)
            write_log(entry)

    def finish_error(self, entry, message, status, payload):
        entry["error"] = message
        _echo(f"[error] {message}\n")
        write_log(entry)
        return self.send_json(status, payload)

    def _collect(self, gen):
        parts = []
        err = None
        try:
            for obj in gen:
                if "error" in obj:
                    if self._repeat_abort(str(obj["error"])):
                        break
                    err = {"error": {"code": "model_unavailable",
                                     "message": str(obj["error"])}}
                    break
                if obj.get("done"):
                    break
                parts.append((obj.get("message") or {}).get("content") or "")
        finally:
            gen.close()
        return "".join(parts), err

    @staticmethod
    def _repeat_abort(message):
        return ("token repeat limit" in message
                or "repeat_last_n" in message
                or "repeat last n" in message)

    def _relay(self, obj, parts):
        """Send one Ollama chunk to the browser. Returns None to continue,
        "done" when finished, or an error message string."""
        if "error" in obj:
            message = str(obj["error"])
            if self._repeat_abort(message):
                # The model hit Ollama's repeat detector at the end of a long
                # reply. Treat it as a graceful finish, not a failure.
                self.send_ndjson({"done": True})
                _echo("\n")
                return "done"
            self.send_ndjson({"error": {"code": "model_unavailable", "message": message}})
            _echo("\n")
            return message
        if obj.get("done"):
            self.send_ndjson({"done": True})
            _echo("\n")
            return "done"
        content = (obj.get("message") or {}).get("content") or ""
        if content:
            parts.append(content)
            _echo(content)
            self.send_ndjson({"content": content})
        return None

    def _try_ndjson_error(self, exc):
        try:
            self.send_ndjson({"error": {"code": "offline",
                                        "message": self._upstream_message(exc)}})
        except _CLIENT_GONE:
            pass

    @staticmethod
    def _chat(payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_HOST + "/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue

    @staticmethod
    def _upstream_message(exc):
        if isinstance(exc, urllib.error.URLError):
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ConnectionRefusedError):
                return "Ollama is not running. Start Ollama and try again."
            return f"Cannot reach Ollama: {reason}"
        if isinstance(exc, socket.timeout):
            return "Ollama took too long to respond."
        return f"Cannot reach Ollama: {exc}"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def run():
    # Keep banners/pull progress visible even when stdout is redirected.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if not prepare_model():
        sys.exit(1)

    try:
        httpd = ThreadingHTTPServer((HOST, PORT), EddyRequestHandler)
    except OSError as exc:
        print(f"Could not start the server on http://{HOST}:{PORT} ({exc}).")
        print("Another instance of Eddy may already be running.")
        sys.exit(1)
    httpd.daemon_threads = True

    print()
    print("Eddy is running at:")
    print(f"http://localhost:{PORT}")
    print()
    print(f"Chat log: {LOG_FILE}" if LOG_ENABLED else "Chat log: disabled (EDDY_LOG=0)")
    print("Press Ctrl+C to stop.")
    print()

    def open_browser():
        time.sleep(0.6)
        webbrowser.open(f"http://localhost:{PORT}/")

    if not os.environ.get("EDDY_NO_BROWSER"):
        threading.Thread(target=open_browser, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping Eddy.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()