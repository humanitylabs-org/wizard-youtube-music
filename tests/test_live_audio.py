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
ext = "webm" if args[args.index("-f") + 1].startswith("bestaudio[acodec=opus]") else "m4a"
out = args[args.index("-o") + 1].replace("%(ext)s", ext)
mode = os.environ["FAKE_MODE"]
body = bytes(range(256)) * 4096  # 1 MiB
if mode == "blocked":
    sys.stderr.write("ERROR: Sign in to confirm you're not a bot\n"); sys.exit(1)
size = len(body) + (7 if mode == "short" else 0)
print(f"{'NA' if mode == 'na' else size} {ext}", flush=True)
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


def test_codec_opus_asks_for_opus_and_caches_beside_the_aac_copy(ytm, monkeypatch):
    """wizard-os#820: ?codec=opus takes itag 251 (Opus webm); without it the format string is the same as before."""
    seen = []

    def run(cmd, **k):
        seen.append(cmd)
        fmt = cmd[cmd.index('-f') + 1]
        ext = 'webm' if fmt.startswith('bestaudio[acodec=opus]') else 'm4a'
        Path([c for c in cmd if c.endswith('.%(ext)s')][0].replace('%(ext)s', ext)).write_bytes(ext.encode() * 10)
        return type('P', (), {'returncode': 0, 'stderr': ''})()

    monkeypatch.setattr(ytm.subprocess, 'run', run)
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8', timeout=10) as r:
        assert r.headers['Content-Type'] == 'audio/mp4' and r.read() == b'm4a' * 10
    assert seen[-1][seen[-1].index('-f') + 1] == 'bestaudio[ext=m4a]/bestaudio'
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?codec=opus', timeout=10) as r:
        assert r.headers['Content-Type'] == 'audio/webm' and r.read() == b'webm' * 10
    assert seen[-1][seen[-1].index('-f') + 1] == 'bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio'
    assert sorted(p.name for p in ytm.CACHE.iterdir()) == ['sWcLccMuCA8.m4a', 'sWcLccMuCA8.opus.webm']
    # both cached now: each variant comes from its own file, the AAC answer never turns into webm
    n = len(seen)
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8', timeout=10) as r:
        assert r.headers['Content-Type'] == 'audio/mp4'
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?codec=opus', timeout=10) as r:
        assert r.headers['Content-Type'] == 'audio/webm'
    assert len(seen) == n


def test_live_codec_opus_uses_the_opus_format(ytm, monkeypatch):
    assert ytm._live_cmd(Path('/x/t'), 'sWcLccMuCA8')[ytm._live_cmd(Path('/x/t'), 'sWcLccMuCA8').index('-f') + 1] == 'bestaudio[ext=m4a]/bestaudio'
    cmd = ytm._live_cmd(Path('/x/t'), 'sWcLccMuCA8', 'opus')
    assert cmd[cmd.index('-f') + 1] == 'bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio'
    assert ytm._codec('flac') == 'aac' and ytm._codec('') == 'aac'


def test_live_opus_streams_webm_and_caches_it_as_the_opus_variant(ytm, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'ok')
    (ytm.tmp / 'go').touch()
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?live=1&codec=opus', timeout=10) as r:
        assert r.headers['Content-Type'] == 'audio/webm' and r.read() == BODY
    assert (ytm.CACHE / 'sWcLccMuCA8.opus.webm').read_bytes() == BODY
    assert not (ytm.CACHE / 'sWcLccMuCA8.m4a').exists()
    with urllib.request.urlopen(ytm.base + '/api/music/audio/sWcLccMuCA8?live=1', timeout=10) as r:
        assert r.headers['Content-Type'] == 'audio/mp4' and r.read() == BODY  # AAC is its own download
    assert (ytm.CACHE / 'sWcLccMuCA8.m4a').exists()
