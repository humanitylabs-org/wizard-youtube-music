#!/usr/bin/env python3
"""YouTube Music connector for Wizard (contract wizard.music-source/1).

Music (the player) asks this app for search results, radio, related artists and audio.
Everything YouTube lives here: the catalog client, the downloader, an optional owner
account, and an optional home relay for Wizards on data-center servers.

YouTube may block data-center addresses. A home connection or Wizard Home Relay can
help but does not guarantee playback, account safety or any account entitlements.
The setup screen says so and proves playback with a real test download.
"""
from __future__ import annotations

import json
import ipaddress
import secrets
import tempfile
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_ID = "wizard-youtube-music"
VERSION = "0.1.0"
CONTRACT = "wizard.music-source/1"
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DATA_DIR", "/data"))
CACHE = DATA / "audio"
SETTINGS = DATA / "settings.json"
AUTH = DATA / "ytmusic-browser.json"  # optional owner account (ytmusicapi browser auth), mode 0600
YTDLP = os.environ.get("YTDLP", "yt-dlp")
CACHE_MAX_BYTES = int(os.environ.get("CACHE_MAX_BYTES", str(3 * 1024**3)))
RADIO_LOCATION = os.environ.get("RADIO_LOCATION", "US")
PROBE_VIDEO = os.environ.get("PROBE_VIDEO", "sWcLccMuCA8")  # a short, widely available track
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

BLOCK_MARKERS = ("confirm you\u2019re not a bot", "confirm you're not a bot", "sign in to confirm", "http error 429", "http error 403")
STATIC = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".png": "image/png", ".svg": "image/svg+xml"}

DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
CACHE.mkdir(parents=True, exist_ok=True)
_lock = threading.RLock()
_dl_locks: dict[str, threading.Lock] = {}
_clients: dict[str, object] = {}

# ---------------------------------------------------------------- settings


def _load() -> dict:
    try:
        d = json.loads(SETTINGS.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    # Unique O_EXCL temporary file: no predictable-file/symlink truncation.
    fd, name = tempfile.mkstemp(prefix=".settings-", dir=DATA)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(d, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, SETTINGS)
    finally:
        Path(name).unlink(missing_ok=True)


def pairing_url(obj):
    """Validate before building a URL. Export domain hints never grant access."""
    error = "Invalid private pairing file. Export a new file from Wizard Home Relay."
    if not isinstance(obj, dict):
        raise ValueError(error)
    if any(obj.get(k) != v for k, v in {
        "schema": "wizard-home-relay-pairing-v1", "scheme": "socks5h",
        "username": "wizard", "provider": "youtube"}.items()):
        raise ValueError(error)
    host, port, password = obj.get("host"), obj.get("port"), obj.get("password")
    try:
        addr = ipaddress.IPv4Address(host) if isinstance(host, str) else None
    except ipaddress.AddressValueError:
        addr = None
    if (addr is None or addr not in ipaddress.IPv4Network("100.64.0.0/10")
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(password, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password)):
        raise ValueError(error)
    return f"socks5h://wizard:{urllib.parse.quote(password, safe='')}@{addr}:{port}"


def valid_relay(value):
    try:
        u = urllib.parse.urlsplit(value)
        return value == pairing_url({"schema": "wizard-home-relay-pairing-v1", "provider": "youtube",
            "scheme": u.scheme, "host": u.hostname, "port": u.port, "username": u.username, "password": u.password})
    except (ValueError, TypeError, AttributeError):
        return False


def import_pairing(body):
    if body.get("confirm_home_device") is not True:
        raise ValueError("Confirm this file belongs to your home Tailscale device first.")
    value = pairing_url(body.get("pairing"))
    return set_relay(value)


def test_relay():
    # Serialize probe/replacement so an obsolete result cannot verify a new import.
    with _lock:
        value = relay()
        if not value:
            raise ValueError("Import your home relay file first.")
        state = "failed"
        try:
            code = _relay_probe(value)
            if code == 200:
                state = "verified"
            elif code in (403, 429):
                state = "provider_blocked"
        except Exception:
            pass  # diagnostics can contain credentials
        d = _load()
        d["relay_check"] = {"state": state, "at": time.time()}
        _save(d)
        return status()


def _relay_probe(value):
    """Required SOCKS auth, remote DNS, verified TLS, total deadline and byte caps."""
    import socket
    import ssl
    u = urllib.parse.urlsplit(value)
    deadline = time.monotonic() + 15
    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError()
        return seconds
    def read(sock, size):
        data = b""
        while len(data) < size:
            sock.settimeout(remaining())
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise OSError("relay closed")
            data += chunk
        return data
    def write(sock, data):
        sock.settimeout(remaining())
        sock.sendall(data)
    with socket.create_connection((u.hostname, u.port), timeout=remaining()) as sock:
        # Offer ONLY password auth; a no-auth service cannot pass pairing.
        write(sock, b"\x05\x01\x02")
        if read(sock, 2) != b"\x05\x02":
            raise OSError("relay authentication required")
        password = u.password.encode("ascii")
        write(sock, b"\x01\x06wizard" + bytes([len(password)]) + password)
        if read(sock, 2) != b"\x01\x00":
            raise OSError("relay authentication refused")
        domain = b"music.youtube.com"
        write(sock, b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + b"\x01\xbb")
        header = read(sock, 4)
        if header[:3] != b"\x05\x00\x00" or header[3] not in (1, 3, 4):
            raise OSError("relay connection refused")
        size = {1: 4, 4: 16}.get(header[3])
        if size is None:
            size = read(sock, 1)[0]
        read(sock, size + 2)
        sock.settimeout(remaining())
        with ssl.create_default_context().wrap_socket(sock, server_hostname="music.youtube.com") as tls:
            write(tls, b"HEAD / HTTP/1.1\r\nHost: music.youtube.com\r\nConnection: close\r\n\r\n")
            line = b""
            while len(line) < 256 and not line.endswith(b"\r\n"):
                line += read(tls, 1)
            match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3}) [^\r\n]*\r\n", line)
            if not match:
                raise OSError("invalid provider response")
            return int(match[1])


def relay() -> str:
    r = _load().get("relay") or ""
    return r if isinstance(r, str) and valid_relay(r) else ""


def set_relay(value: str) -> dict:
    if not isinstance(value, str) or (value and not valid_relay(value)):
        raise ValueError("Invalid home relay. Use a private pairing file with a Tailscale IPv4 address.")
    with _lock:
        d = _load()
        if value.strip():
            d["relay"] = value.strip()
        else:
            d.pop("relay", None)
        d["relay_check"] = {"state": "imported_unverified" if value else "not_configured", "at": 0}
        d["playback"] = {"state": "untested", "at": 0}
        _save(d)
        _clients.clear()
    return status()


def _record(state: str, detail: str = "") -> None:
    with _lock:
        d = _load()
        d["playback"] = {"state": state, "at": time.time(), "detail": detail[-200:]}
        _save(d)


def status() -> dict:
    d = _load()
    stored = d.get("playback") or {"state": "untested", "at": 0}
    # Older versions stored downloader stderr, which may contain relay credentials.
    p = {"state": stored.get("state", "untested"), "at": stored.get("at", 0)}
    via = "home relay" if relay() else "this Wizard's own internet"
    message = {
        "ok": f"Working. Songs play through {via}.",
        "untested": "Not tested yet. Tap Test playback.",
        "blocked": ("YouTube is blocking this Wizard's internet. That happens on cloud servers (VPS) in data centers. "
                    "Run Wizard at home, or add a home relay below."),
        "failed": "The last test download failed. Try again, or check the home relay.",
    }.get(p.get("state"), "")
    return {"app": APP_ID, "version": VERSION, "contract": CONTRACT, "provider": "youtube-music",
            "name": "YouTube Music", "ready": p.get("state") == "ok", "playback": {**p, "via": via, "message": message},
            "relay": bool(relay()), "relay_status": relay_status(d), "account": account_status()["state"] == "connected",
            "account_status": account_status(),
            "capabilities": {"library": ["playlists", "likes", "playlist"], "library_path": "/api/music/library/"},
            "requirement": "Use home internet directly, or an always-on home relay for a cloud Wizard. Neither guarantees playback."}

# ---------------------------------------------------------------- owner account (documented ytmusicapi device OAuth)

OAUTH = DATA / "ytmusic-oauth.json"
OAUTH_CLIENT = DATA / "oauth-client.json"
_pending = None  # device code stays in memory only; restarting cancels setup
_account = {"state": "signed_out", "checked_at": 0}


def _private_json(path, value):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out)
    tmp.replace(path)


def _oauth_credentials():
    from ytmusicapi import OAuthCredentials
    try:
        c = json.loads(OAUTH_CLIENT.read_text())
        return OAuthCredentials(client_id=c["client_id"], client_secret=c["client_secret"], session=_session())
    except Exception:
        raise ValueError("Owner setup needed: provision oauth-client.json privately; see README.") from None


def account_status():
    state = dict(_account)
    if not (OAUTH.exists() or AUTH.exists()):
        state = {"state": "signed_out", "checked_at": 0}
    if state["state"] == "signed_out" and (OAUTH.exists() or AUTH.exists()):
        state["state"] = "unverified"
    return {**state, "configured": OAUTH_CLIENT.exists(), "method": "device_oauth",
            "legacy_browser_credentials": AUTH.exists()}


def account_start():
    global _pending
    with _lock:
        if OAUTH.exists() or AUTH.exists():
            raise ValueError("Disconnect the existing account before connecting another.")
        try:
            code = _oauth_credentials().get_code()
            # Never accept a provider-supplied arbitrary URL in the owner UI.
            url = code.get("verification_url") or code.get("verification_uri")
            if url not in ("https://www.google.com/device", "https://google.com/device", "https://www.youtube.com/activate"):
                raise ValueError("unexpected verification URL")
            _pending = {"device_code": code["device_code"], "expires": time.time() + min(int(code["expires_in"]), 1800),
                        "interval": max(5, int(code.get("interval", 5))), "next": 0}
            return {"verification_url": url, "user_code": code["user_code"], "expires_in": min(int(code["expires_in"]), 1800)}
        except Exception:
            _pending = None
            raise ValueError("Could not start Google authorization. Check owner OAuth client configuration.") from None


def account_check():
    global _account
    with _lock:
        if not (OAUTH.exists() or AUTH.exists()):
            _account = {"state": "signed_out", "checked_at": 0}
        else:
            try:
                # A file alone is NOT evidence of an authenticated account.
                ytm().get_library_playlists(limit=1)
                _account = {"state": "connected", "checked_at": time.time()}
            except Exception:
                _clients.clear()
                _account = {"state": "needs_attention", "checked_at": time.time()}
        return status()


def account_poll():
    global _pending
    with _lock:
        if not _pending or time.time() >= _pending["expires"]:
            _pending = None
            return {"state": "expired"}
        if time.time() < _pending["next"]:
            return {"state": "pending", "retry_after": _pending["interval"]}
        _pending["next"] = time.time() + _pending["interval"]
        try:
            token = _oauth_credentials().token_from_code(_pending["device_code"])
        except Exception:
            raise ValueError("Google authorization unavailable. Try again later.") from None
        error = token.get("error")
        if error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                _pending["interval"] += 5
                _pending["next"] = time.time() + _pending["interval"]
            return {"state": "pending", "retry_after": _pending["interval"]}
        if error or not all(token.get(k) for k in ("access_token", "refresh_token", "token_type", "scope", "expires_in")):
            _pending = None
            return {"state": "denied"}
        token = {k: token[k] for k in ("access_token", "refresh_token", "token_type", "scope", "expires_in")}
        token["expires_at"] = int(time.time()) + int(token["expires_in"])
        _private_json(OAUTH, token)
        _pending = None
        _clients.clear()
        return {"state": "complete", "status": account_check()}


def account_disconnect():
    global _pending, _account
    with _lock:
        _pending = None
        OAUTH.unlink(missing_ok=True)
        AUTH.unlink(missing_ok=True)
        _clients.clear()
        _account = {"state": "signed_out", "checked_at": 0}
        return status()


def library(kind, playlist="", limit=40):
    if account_status()["state"] != "connected":
        account_check()
    if account_status()["state"] != "connected":
        raise ValueError("Connect and verify your YouTube account first.")
    limit = max(1, min(limit, 100))
    if kind not in ("playlists", "likes", "playlist"):
        raise ValueError("Unknown library kind")
    if kind == "playlist" and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", playlist):
        raise ValueError("Invalid playlist id")
    try:
        client = ytm()
        if kind == "playlists":
            return {"playlists": [{k: p[k] for k in ("playlistId", "title", "thumbnails", "count") if k in p}
                                  for p in client.get_library_playlists(limit=limit)[:limit]]}
        if kind == "likes":
            raw = client.get_liked_songs(limit=limit)
        else:
            raw = client.get_playlist(playlist, limit=limit)
        return {"tracks": [i for i in (_item(r) for r in raw.get("tracks", [])[:limit]) if i]}
    except Exception:
        _account.update(state="needs_attention", checked_at=time.time())
        raise RuntimeError("Account library unavailable; verify your connection.") from None

# ---------------------------------------------------------------- YouTube catalog


def _session():
    import requests
    class BoundedSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", 20)
            return super().request(*args, **kwargs)
    s = BoundedSession()
    if relay():
        s.trust_env = False
        s.proxies.update({"http": relay(), "https": relay()})
    return s


def ytm(radio: bool = False):
    key = "radio" if radio else "main"
    with _lock:
        if key not in _clients:
            from ytmusicapi import YTMusic
            kw = {"requests_session": _session()}
            if radio and RADIO_LOCATION:
                kw["location"] = RADIO_LOCATION
            if OAUTH.exists():
                kw["oauth_credentials"] = _oauth_credentials()
                _clients[key] = YTMusic(str(OAUTH), **kw)
            else:
                _clients[key] = YTMusic(str(AUTH), **kw) if AUTH.exists() else YTMusic(**kw)
        return _clients[key]


KEEP = ("videoId", "title", "artists", "album", "duration", "duration_seconds", "length", "thumbnails", "thumbnail", "author")


def _item(raw: dict) -> dict | None:
    if not isinstance(raw, dict) or not YT_ID.match(str(raw.get("videoId") or "")):
        return None
    out = {k: raw[k] for k in KEEP if k in raw}
    if isinstance(out.get("artists"), list):
        out["artists"] = [{"name": a.get("name", "")} for a in out["artists"] if isinstance(a, dict)]
    if isinstance(out.get("album"), dict):
        out["album"] = {"name": out["album"].get("name", "")}
    return out


def search(q: str, kind: str = "songs", limit: int = 20) -> list[dict]:
    return [i for i in (_item(r) for r in ytm().search(q, filter=kind, limit=limit)[:limit]) if i]


def radio(seed: str, limit: int = 60) -> list[dict]:
    if not YT_ID.match(seed):
        raise ValueError("seed must be a video id")
    watch = ytm(radio=True).get_watch_playlist(seed, radio=True, limit=limit)
    return [i for i in (_item(r) for r in watch.get("tracks", [])) if i]


def related_artists(name: str) -> list[str]:
    want = re.sub(r"\W+", "", name.lower())
    hits = ytm().search(name, filter="artists", limit=5)
    hit = next((h for h in hits if h.get("browseId") and re.sub(r"\W+", "", (h.get("artist") or h.get("title") or "").lower()) == want), None)
    if not hit:
        return []
    info = ytm().get_artist(hit["browseId"])
    return [i.get("title") for i in ((info.get("related") or {}).get("results") or [])[:15] if i.get("title")]


def genre_tracks(genre: str) -> list[dict]:
    lists = ytm().search(genre, filter="featured_playlists", limit=3)
    pid = next((p["browseId"] for p in lists if p.get("browseId")), "")
    if not pid:
        return []
    items = ytm().get_playlist(pid[2:] if pid.startswith("VL") else pid, limit=25).get("tracks") or []
    return [i for i in (_item(r) for r in items) if i]

# ---------------------------------------------------------------- audio


def _cached(vid: str) -> Path | None:
    for ext in ("m4a", "webm", "opus", "mp3"):
        p = CACHE / f"{vid}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _evict() -> None:
    files = sorted((p for p in CACHE.iterdir() if p.is_file() and not p.name.startswith(".")), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= CACHE_MAX_BYTES:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)


def audio(vid: str) -> Path:
    if not YT_ID.match(vid):
        raise LookupError("bad id")
    hit = _cached(vid)
    if hit:
        os.utime(hit)
        return hit
    with _lock:
        lock = _dl_locks.setdefault(vid, threading.Lock())
    with lock:
        hit = _cached(vid)
        if hit:
            return hit
        tmp = CACHE / f".{vid}.{os.getpid()}.{threading.get_ident()}"
        cmd = [YTDLP, "-q", "--no-warnings", "--no-cache-dir", "--no-playlist", "--js-runtimes", "node",
               "-f", "bestaudio[ext=m4a]/bestaudio", "-o", f"{tmp}.%(ext)s", f"https://music.youtube.com/watch?v={vid}"]
        if relay():
            cmd[1:1] = ["--proxy", relay()]
        if AUTH.with_name("cookies.txt").exists():
            cmd[1:1] = ["--cookies", str(AUTH.with_name("cookies.txt"))]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
        except subprocess.TimeoutExpired:
            _record("failed", "timed out")
            raise RuntimeError("download timed out") from None
        made = sorted(CACHE.glob(f"{tmp.name}.*"))
        if proc.returncode != 0 or not made:
            for m in made:
                m.unlink(missing_ok=True)
            low = (proc.stderr or "").lower()
            _record("blocked" if any(m in low for m in BLOCK_MARKERS) else "failed", "YouTube refused the download" if any(m in low for m in BLOCK_MARKERS) else "Download failed")
            raise RuntimeError("blocked" if any(m in low for m in BLOCK_MARKERS) else "download failed")
        final = CACHE / f"{vid}{made[0].suffix}"
        made[0].replace(final)
        _record("ok")
        _evict()
        return final


def test_playback() -> dict:
    for p in CACHE.glob(f"{PROBE_VIDEO}.*"):
        p.unlink(missing_ok=True)
    try:
        audio(PROBE_VIDEO)
    except Exception:  # noqa: BLE001 - state recorded by audio()
        pass
    return status()

# ---------------------------------------------------------------- MCP

TOOLS = [
    {"name": "youtube_music_status", "description": "Whether YouTube Music playback works on this Wizard, and why not if it doesn't.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "youtube_music_search", "description": "Search YouTube Music songs (catalog only; play them in Music).",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "youtube_music_test", "description": "Run a real test download and report whether playback works.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def rpc(msg: dict):
    if "id" not in msg:
        return None
    i, m, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if m == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": APP_ID, "version": VERSION}}
    elif m == "tools/list":
        r = {"tools": TOOLS}
    elif m == "ping":
        r = {}
    elif m == "tools/call":
        n, a = p.get("name"), p.get("arguments") or {}
        try:
            v = status() if n == "youtube_music_status" else test_playback() if n == "youtube_music_test" else \
                {"tracks": search(str(a.get("query", ""))[:200])} if n == "youtube_music_search" else None
            if v is None:
                return {"jsonrpc": "2.0", "id": i, "error": {"code": -32602, "message": "Unknown tool."}}
            r = {"content": [{"type": "text", "text": json.dumps(v)}], "structuredContent": v, "isError": False}
        except Exception as e:  # noqa: BLE001
            r = {"content": [{"type": "text", "text": "YouTube Music request failed; check connection status."}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "Unknown method."}}
    return {"jsonrpc": "2.0", "id": i, "result": r}

# ---------------------------------------------------------------- HTTP


def _csv(name: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()}


PORT = int(os.environ.get("PORT", "9206"))
TRUSTED_ORIGINS = _csv("WIZARD_TRUSTED_ORIGINS")


RELAY_ORIGIN = os.environ.get("WIZARD_RELAY_IMPORT_ORIGIN", "")
RELAY_KEY = DATA / "relay-import-key"


def relay_import_key():
    try:
        info = RELAY_KEY.lstat()
        import stat
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return ""
        if info.st_size > 256:
            return ""
        value = RELAY_KEY.read_text().strip()
        return value if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value) else ""
    except (OSError, UnicodeError):
        return ""


def relay_status(d):
    check = d.get("relay_check") or {}
    state = check.get("state", "imported_unverified") if relay() else "not_configured"
    messages = {
        "not_configured": "No valid home relay configured. Import your private pairing file.",
        "imported_unverified": "Imported privately, not paired yet. Test relay connectivity next.",
        "verified": "Paired: authenticated relay reached YouTube. Test playback separately.",
        "provider_blocked": "Relay probe reached a provider refusal (403/429). Not paired; check the home connection.",
        "failed": "Relay connectivity failed. Check Tailscale access, the home service and its token, then retry.",
    }
    if state not in messages:
        state = "imported_unverified" if relay() else "not_configured"
    try:
        origin = urllib.parse.urlsplit(RELAY_ORIGIN)
        enabled = (origin.scheme == "https" and bool(origin.hostname) and not origin.username
                   and not origin.password and origin.port != 0
                   and RELAY_ORIGIN == f"https://{origin.netloc}" and bool(relay_import_key()))
    except ValueError:
        enabled = False
    return {"state": state, "at": check.get("at", 0), "message": messages[state], "import_enabled": enabled}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Never log arbitrary request targets (credentials can be misplaced in URLs).
        sys.stderr.write("%s request\n" % self.command)

    def send(self, code: int, body=None, ctype: str = "application/json", extra: dict | None = None):
        b = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; frame-ancestors 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def cross_site_write(self) -> bool:
        origin = (self.headers.get("Origin") or "").lower()
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").lower()
        if origin and origin not in TRUSTED_ORIGINS and urllib.parse.urlsplit(origin).netloc != host:
            return True
        return (self.headers.get("Sec-Fetch-Site") or "") == "cross-site"

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        q = lambda k, d="": (qs.get(k) or [d])[0][:200]  # noqa: E731
        try:
            if path == "/healthz":
                return self.send(200, {"ok": True, "app": APP_ID, "version": VERSION})
            if path == "/api/status":
                return self.send(200, status())
            if path in ("/api/music/library/playlists", "/api/music/library/likes", "/api/music/library/playlist"):
                return self.send(200, library(path.rsplit("/", 1)[1], q("id"), int(q("limit", "40"))))
            if path == "/api/music/search":
                return self.send(200, {"tracks": search(q("q"), q("kind", "songs"), min(int(q("limit", "20")), 40))})
            if path == "/api/music/radio":
                return self.send(200, {"tracks": radio(q("seed"), min(int(q("limit", "60")), 100))})
            if path == "/api/music/related-artists":
                return self.send(200, {"artists": related_artists(q("name"))})
            if path == "/api/music/genre":
                return self.send(200, {"tracks": genre_tracks(q("genre"))})
            m = re.match(r"^/api/music/audio/([A-Za-z0-9_-]{11})$", path)
            if m:
                return self.stream(audio(m.group(1)))
            name = "index.html" if path == "/" else path[1:]
            kind = STATIC.get(Path(name).suffix)
            f = ROOT / "web" / name
            if kind and "/" not in name and not name.startswith(".") and f.is_file():
                return self.send(200, f.read_bytes(), kind)
            if path == "/icon.png":
                return self.send(200, (ROOT / "icon.png").read_bytes(), "image/png")
            return self.send(404, {"error": "not_found"})
        except LookupError as e:
            return self.send(404, {"error": str(e)})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            print("GET failed", path, type(e).__name__, file=sys.stderr)
            return self.send(502, {"error": "blocked" if "blocked" in str(e) else "source_unavailable", "status": status()["playback"]})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/relay"):
            # A separate owner-provisioned credential authenticates these secret writes;
            # upstream shell login alone is not inferred from spoofable identity headers.
            self.close_connection = True
            import_key = relay_import_key()
            if not relay_status(_load())["import_enabled"]:
                return self.send(503, {"error": "Private import is disabled. Owner must configure HTTPS origin and private import key; see README."})
            if (self.headers.get("Origin") != RELAY_ORIGIN
                    or self.headers.get("Host") != urllib.parse.urlsplit(RELAY_ORIGIN).netloc
                    or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
                    or urllib.parse.urlsplit(self.path).query):
                return self.send(403, {"error": "Exact same-origin HTTPS relay operation required."})
            auth = self.headers.get("Authorization", "")
            if not import_key or not secrets.compare_digest(auth.encode(), ("Bearer " + import_key).encode()):
                return self.send(401, {"error": "Private relay import authorization required."})
        if self.cross_site_write():
            return self.send(403, {"error": "Cross-origin writes refused."})
        try:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("Invalid request length.") from None
            if n < 0 or n > 65536 or self.headers.get("Transfer-Encoding"):
                return self.send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeError):
                raise ValueError("Invalid JSON request.") from None
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            if path == "/mcp":
                out = rpc(body if isinstance(body, dict) else {})
                return self.send(202 if out is None else 200, out)
            if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
                return self.send(415, {"error": "Use application/json."})
            if path == "/api/relay":
                if body != {"relay": ""}:
                    raise ValueError("Raw relay addresses are not accepted. Use private file import.")
                return self.send(200, set_relay(""))
            if path == "/api/relay/import":
                return self.send(200, import_pairing(body))
            if path == "/api/relay/test":
                return self.send(200, test_relay())
            if path == "/api/test":
                return self.send(200, test_playback())
            if path == "/api/account/start":
                return self.send(200, account_start())
            if path == "/api/account/poll":
                return self.send(200, account_poll())
            if path == "/api/account/check":
                return self.send(200, account_check())
            if path == "/api/account/disconnect":
                return self.send(200, account_disconnect())
            return self.send(404, {"error": "not_found"})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception:
            return self.send(502, {"error": "Request unavailable. Check connection status."})

    def stream(self, p: Path):
        size = p.stat().st_size
        ctype = {".m4a": "audio/mp4", ".webm": "audio/webm", ".opus": "audio/ogg", ".mp3": "audio/mpeg"}.get(p.suffix, "application/octet-stream")
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        code = 200
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with p.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0 and (chunk := fh.read(min(262144, left))):
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


if __name__ == "__main__":
    if "relay" in sys.argv[1:2]:  # `python server.py relay` runs Wizard Home Relay on a home computer
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        sys.path.insert(0, str(Path(__file__).parent))
        import asyncio, home_relay  # noqa: E401
        home_relay.address() if sys.argv[1:] == ["address"] else asyncio.run(home_relay.main())
    else:
        srv = ThreadingHTTPServer((os.environ.get("BIND", "0.0.0.0"), PORT), Handler)
        srv.daemon_threads = True
        print(f"{APP_ID} {VERSION} on :{PORT}", flush=True)
        srv.serve_forever()
