"""wizard-music#46: ?live=1 sends a song while yt-dlp is still writing it, then caches it as usual."""
import importlib.util
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

FAKE = r'''#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
out = args[args.index("-o") + 1].replace("%(ext)s", "m4a")
mode = os.environ["FAKE_MODE"]
body = bytes(range(256)) * 4096  # 1 MiB
if mode == "blocked":
    sys.stderr.write("ERROR: Sign in to confirm you're not a bot\n"); sys.exit(1)
size = len(body) + (7 if mode == "short" else 0)
print(f"{'NA' if mode == 'na' else size} m4a", flush=True)
assert "--no-part" in args and "--fixup" in args
with open(out, "wb") as fh:
    for i in range(0, len(body), 65536):
        fh.write(body[i:i + 65536]); fh.flush()
        if i == 0:
            open(os.environ["FAKE_STARTED"], "w").close()
            while not os.path.exists(os.environ["FAKE_GO"]):
                time.sleep(0.02)
        if mode == "die" and i > 300000:
            sys.exit(2)
'''
BODY = bytes(range(256)) * 4096


@pytest.fixture
def ytm(monkeypatch, tmp_path):
    fake = tmp_path / 'yt-dlp'
    fake.write_text(FAKE)
    fake.chmod(0o755)
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('YTDLP', str(fake))
    monkeypatch.setenv('FAKE_STARTED', str(tmp_path / 'started'))
    monkeypatch.setenv('FAKE_GO', str(tmp_path / 'go'))
    spec = importlib.util.spec_from_file_location('ytm_live', Path(__file__).parents[1] / 'app' / 'server.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mod.base = f'http://127.0.0.1:{srv.server_port}'
    mod.tmp = tmp_path
    yield mod
    srv.shutdown()


def test_first_bytes_arrive_before_the_download_ends_and_the_song_is_cached(ytm, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'ok')
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?live=1', timeout=10) as r:
        assert r.headers['Content-Length'] == str(len(BODY)) and r.headers['Content-Type'] == 'audio/mp4'
        first = r.read1(65536)
        assert first == BODY[:len(first)] and first
        assert not (ytm.tmp / 'go').exists()  # yt-dlp is still waiting: these bytes came mid-download
        (ytm.tmp / 'go').touch()
        rest = r.read()
    assert first + rest == BODY
    assert (ytm.CACHE / 'sWcLccMuCA8.m4a').read_bytes() == BODY
    assert not [p for p in ytm.CACHE.iterdir() if p.name.startswith('.')]
    assert ytm.status()['playback']['state'] == 'ok'
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?live=1', timeout=10) as r:
        assert r.read() == BODY  # cached now: served straight from the cache


def test_without_live_the_whole_download_comes_first_as_before(ytm, monkeypatch):
    seen = {}

    def run(cmd, **k):
        seen['cmd'] = cmd
        Path([c for c in cmd if c.endswith('.%(ext)s')][0].replace('%(ext)s', 'm4a')).write_bytes(b'audio')
        return type('P', (), {'returncode': 0, 'stderr': ''})()
    monkeypatch.setattr(ytm.subprocess, 'run', run)
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8', timeout=10) as r:
        assert r.read() == b'audio'
    assert '--no-part' not in seen['cmd']


def test_a_blocked_start_answers_502_so_the_hub_retries(ytm, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'blocked')
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?live=1', timeout=10)
    assert exc.value.code == 502
    assert ytm.status()['playback']['state'] == 'blocked'


@pytest.mark.parametrize('mode', ['die', 'short'])
def test_a_broken_download_never_looks_complete(ytm, monkeypatch, mode):
    monkeypatch.setenv('FAKE_MODE', mode)
    (ytm.tmp / 'go').touch()
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?live=1', timeout=10) as r:
        with pytest.raises(Exception):
            got = r.read()
            assert len(got) == int(r.headers['Content-Length'])
    if mode == 'die':
        assert not ytm._cached('sWcLccMuCA8')


def test_an_unknown_size_streams_until_the_connection_closes(ytm, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'na')
    (ytm.tmp / 'go').touch()
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?live=1', timeout=10) as r:
        assert r.headers['Content-Length'] is None
        assert r.read() == BODY
    assert (ytm.CACHE / 'sWcLccMuCA8.m4a').read_bytes() == BODY
