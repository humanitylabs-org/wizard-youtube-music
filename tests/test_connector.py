import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def ytm(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    spec = importlib.util.spec_from_file_location('ytm_server', Path(__file__).parents[1] / 'app' / 'server.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Proc:
    def __init__(self, code, err=''):
        self.returncode, self.stderr = code, err


def test_untested_by_default_and_states_requirement(ytm):
    s = ytm.status()
    assert s['ready'] is False and s['playback']['state'] == 'untested'
    assert 'home' in s['requirement'].lower()
    assert s['contract'] == 'wizard.music-source/1'


def test_blocked_is_reported_not_ok(ytm, monkeypatch):
    monkeypatch.setattr(ytm.subprocess, 'run', lambda *a, **k: Proc(1, "ERROR: Sign in to confirm you're not a bot"))
    s = ytm.test_playback()
    assert s['ready'] is False and s['playback']['state'] == 'blocked'
    assert 'cloud' in s['playback']['message']


def test_relay_is_private_routes_downloads_and_resets_test(ytm, monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        ytm.set_relay('http://evil:80')
    addr = 'socks5h://wizard:abcdefgh12345678@100.64.1.2:18765'
    s = ytm.set_relay(addr)
    assert s['relay'] is True and s['playback']['state'] == 'untested'
    assert 'abcdefgh' not in json.dumps(s)
    assert (tmp_path / 'settings.json').stat().st_mode & 0o777 == 0o600
    seen = {}

    def run(cmd, **k):
        seen['cmd'] = cmd
        out = [c for c in cmd if c.endswith('.%(ext)s')][0].replace('%(ext)s', 'm4a')
        Path(out).write_bytes(b'audio')
        return Proc(0)
    monkeypatch.setattr(ytm.subprocess, 'run', run)
    s = ytm.test_playback()
    assert seen['cmd'][1:3] == ['--proxy', addr]
    assert s['ready'] is True and s['playback']['via'] == 'home relay'
    assert ytm.set_relay('')['relay'] is False


def test_bad_video_id_refused(ytm):
    with pytest.raises(LookupError):
        ytm.audio('../../etc/passwd')
    with pytest.raises(ValueError):
        ytm.radio('nope')


def test_item_keeps_only_catalog_fields(ytm):
    raw = {'videoId': 'sWcLccMuCA8', 'title': 'Xtal', 'artists': [{'name': 'Aphex Twin', 'id': 'x'}], 'secret': 'x',
           'album': {'name': 'SAW', 'id': 'y'}}
    item = ytm._item(raw)
    assert 'secret' not in item and item['artists'] == [{'name': 'Aphex Twin'}] and item['album'] == {'name': 'SAW'}
    assert ytm._item({'videoId': 'bad'}) is None


def test_mcp_lists_tools(ytm):
    out = ytm.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert {t['name'] for t in out['result']['tools']} == {'youtube_music_status', 'youtube_music_search', 'youtube_music_test'}
