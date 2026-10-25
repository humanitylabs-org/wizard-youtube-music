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
RELAY_RE = re.compile(r"^socks5h://[A-Za-z0-9._~-]{1,64}:[A-Za-z0-9._~-]{8,200}@[A-Za-z0-9.-]{1,253}:[0-9]{2,5}$")
BLOCK_MARKERS = ("confirm you\u2019re not a bot", "confirm you're not a bot", "sign in to confirm", "http error 429", "http error 403")
STATIC = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".png": "image/png", ".svg": "image/svg+xml"}

DATA.mkdir(parents=True, exist_ok=True)
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
    tmp = SETTINGS.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(d, out)
    tmp.replace(SETTINGS)


def relay() -> str:
    r = _load().get("relay") or ""
    return r if isinstance(r, str) and RELAY_RE.match(r) else ""


def set_relay(value: str) -> dict:
    if not isinstance(value, str) or (value.strip() and not RELAY_RE.match(value.strip())):
        raise ValueError("A home relay address looks like socks5h://wizard:\u2026@100.x.x.x:18765")
    with _lock:
        d = _load()
        if value.strip():
            d["relay"] = value.strip()
        else:
            d.pop("relay", None)
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
            "relay": bool(relay()), "account": account_status()["state"] == "connected",
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

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
        if self.cross_site_write():
            return self.send(403, {"error": "Cross-origin writes refused."})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                return self.send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            if path == "/mcp":
                out = rpc(body if isinstance(body, dict) else {})
                return self.send(202 if out is None else 200, out)
            if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
                return self.send(415, {"error": "Use application/json."})
            if path == "/api/relay":
                return self.send(200, set_relay(str(body.get("relay", ""))))
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
