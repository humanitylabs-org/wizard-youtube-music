"""Offline owner-account tests. No real accounts, secrets, consent or downloads."""
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest
import requests
from ytmusicapi import OAuthCredentials


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    spec = importlib.util.spec_from_file_location('account_test_server', Path(__file__).parents[1] / 'app/server.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Transport:
    """Exercise real OAuthCredentials serialization/parsing without Google traffic."""
    def __init__(self):
        self.calls = []
        self.responses = []

    def post(self, url, data, **kwargs):
        self.calls.append((url, dict(data)))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(self.responses.pop(0)).encode()
        return response


@pytest.fixture
def flow(server, monkeypatch):
    transport = Transport()
    client = OAuthCredentials('fixture-client', 'fixture-secret', session=transport)
    monkeypatch.setattr(server, '_oauth_credentials', lambda: client)
    transport.responses.append({'device_code': 'private-device', 'user_code': 'OWNER-CODE',
                                'verification_url': 'https://www.google.com/device', 'expires_in': 900, 'interval': 5})
    return transport


class Library:
    def get_library_playlists(self, limit):
        return [{'playlistId': 'PL_fixture', 'title': 'My list', 'secret': 'not-for-client'}]

    def get_liked_songs(self, limit):
        return {'tracks': [{'videoId': 'sWcLccMuCA8', 'title': 'Liked', 'secret': 'not-for-client'}, {'videoId': 'invalid'}]}

    def get_playlist(self, pid, limit):
        assert pid == 'PL_fixture'
        return self.get_liked_songs(limit)


def token():
    return {'access_token': 'private-access', 'refresh_token': 'private-refresh', 'scope': 'https://www.googleapis.com/auth/youtube',
            'token_type': 'Bearer', 'expires_in': 3600}


def test_missing_owner_client_is_not_signin(server):
    assert server.status()['account_status']['state'] == 'signed_out'
    with pytest.raises(ValueError, match='configuration'):
        server.account_start()
    assert not server.OAUTH.exists()


def test_device_flow_real_library_transport_and_private_storage(server, flow, monkeypatch):
    relay = 'socks5h://wizard:fixturepassword@100.64.1.2:18765'
    server.set_relay(relay)
    start = server.account_start()
    assert start['user_code'] == 'OWNER-CODE' and 'device_code' not in start
    assert flow.calls[0][1]['client_id'] == 'fixture-client'
    assert flow.calls[0][1]['scope'] == 'https://www.googleapis.com/auth/youtube'
    flow.responses.append({'error': 'authorization_pending'})
    assert server.account_poll()['state'] == 'pending'
    count = len(flow.calls)
    assert server.account_poll()['state'] == 'pending'
    assert len(flow.calls) == count  # enforced polling interval
    server._pending['next'] = 0
    flow.responses.append(token())
    monkeypatch.setattr(server, 'ytm', lambda **kwargs: Library())
    result = server.account_poll()
    assert result['state'] == 'complete' and result['status']['account'] is True
    assert server.OAUTH.stat().st_mode & 0o777 == 0o600
    assert json.loads(server.OAUTH.read_text())['refresh_token'] == 'private-refresh'
    public = json.dumps(server.status())
    assert all(s not in public for s in ('private-access', 'private-refresh', 'private-device', 'fixturepassword', 'fixture-secret'))
    assert server.relay() == relay
    assert 'secret' not in json.dumps(server.library('playlists'))
    assert len(server.library('likes')['tracks']) == 1
    assert server.library('playlist', 'PL_fixture')['tracks'][0]['title'] == 'Liked'
    with pytest.raises(ValueError):
        server.library('playlist', '../../bad')
    assert server.account_disconnect()['account_status']['state'] == 'signed_out'
    assert not server.OAUTH.exists() and server._pending is None
    assert server.relay() == relay
    with pytest.raises(ValueError, match='Connect and verify'):
        server.library('likes')


def test_file_is_unverified_and_failure_is_not_connected(server, monkeypatch):
    server._private_json(server.OAUTH, token())
    assert server.status()['account'] is False
    assert server.status()['account_status']['state'] == 'unverified'
    def fail(**kw):
        raise RuntimeError('private-access provider diagnostic')
    monkeypatch.setattr(server, 'ytm', fail)
    result = server.account_check()
    assert result['account'] is False and result['account_status']['state'] == 'needs_attention'
    assert 'private-access' not in json.dumps(result)


@pytest.mark.parametrize('outcome', ['access_denied', 'expired_token', 'invalid_client'])
def test_refused_does_not_save_tokens(server, flow, outcome):
    server.account_start()
    flow.responses.append({'error': outcome, 'error_description': 'sensitive diagnostic'})
    assert server.account_poll() == {'state': 'denied'}
    assert server._pending is None and not server.OAUTH.exists()


def test_expiry_slowdown_cancel_and_url_validation(server, flow):
    server.account_start()
    flow.responses.append({'error': 'slow_down'})
    assert server.account_poll()['retry_after'] == 10
    server._pending['expires'] = 0
    assert server.account_poll()['state'] == 'expired'
    flow.responses.append({'device_code': 'd', 'user_code': 'c', 'verification_url': 'https://evil.example/', 'expires_in': 300})
    with pytest.raises(ValueError):
        server.account_start()
    assert server._pending is None
    server.AUTH.write_text('{}')
    assert server.status()['account_status']['state'] == 'unverified'
    server.account_disconnect()
    assert not server.AUTH.exists()


def test_provider_failure_library_marks_attention(server, monkeypatch):
    server._private_json(server.OAUTH, token())
    monkeypatch.setattr(server, 'ytm', lambda **kwargs: Library())
    server.account_check()
    def fail(limit):
        raise RuntimeError('private-access')
    client = Library()
    client.get_liked_songs = fail
    monkeypatch.setattr(server, 'ytm', lambda **kwargs: client)
    with pytest.raises(RuntimeError, match='Account library unavailable'):
        server.library('likes')
    assert server.account_status()['state'] == 'needs_attention'


def test_http_account_routes_origin_guard_and_library(server, flow, monkeypatch):
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(server, 'ytm', lambda **kwargs: Library())
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = 'http://127.0.0.1:' + str(httpd.server_port)
    try:
        assert requests.post(base + '/api/account/start', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert not flow.calls
        assert requests.post(base + '/api/account/start', json=[]).status_code == 400
        assert requests.post(base + '/api/account/start', json={}).status_code == 200
        flow.responses.append(token())
        r = requests.post(base + '/api/account/poll', json={})
        assert r.json()['status']['account'] is True
        assert requests.get(base + '/api/music/library/playlists').json()['playlists'][0]['playlistId'] == 'PL_fixture'
        assert requests.get(base + '/api/music/library/likes').json()['tracks'][0]['title'] == 'Liked'
        assert requests.get(base + '/api/music/library/playlist?id=PL_fixture').status_code == 200
        assert requests.post(base + '/api/account/check', json={}).json()['account'] is True
        assert requests.post(base + '/api/account/disconnect', json={}).json()['account'] is False
        assert requests.get(base + '/api/music/library/likes').status_code == 400
        assert requests.get(base + '/api/status').json()['account_status']['state'] == 'signed_out'
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()


def test_real_ytmusic_client_refresh_and_restart_readback(server, monkeypatch):
    from ytmusicapi.auth.oauth import OAuthCredentials
    class CatalogTransport(requests.Session):
        def __init__(self):
            super().__init__()
            self.seen = []
        def post(self, url, data=None, **kwargs):
            self.seen.append((url, data, kwargs))
            response = requests.Response()
            response.status_code = 200
            if data and data.get('grant_type') == 'refresh_token':
                payload = {'access_token': 'refreshed-fixture', 'expires_in': 3600, 'token_type': 'Bearer'}
            else:
                assert 'browse' in url
                assert kwargs['headers']['authorization'] == 'Bearer refreshed-fixture'
                payload = {'contents': {'singleColumnBrowseResultsRenderer': {'tabs': []}}}
            response._content = json.dumps(payload).encode()
            return response
    transport = CatalogTransport()
    monkeypatch.setattr(server, '_session', lambda: transport)
    server._private_json(server.OAUTH_CLIENT, {'client_id': 'fixture-client', 'client_secret': 'fixture-secret'})
    expired = token()
    expired.update(expires_in=0, expires_at=1)
    server._private_json(server.OAUTH, expired)
    assert server.account_check()['account'] is True  # actual YTMusic parser + OAuth refresh
    saved = json.loads(server.OAUTH.read_text())
    assert saved['access_token'] == 'refreshed-fixture'
    assert server.OAUTH.stat().st_mode & 0o777 == 0o600
    server._clients.clear()
    server._account = {'state': 'signed_out', 'checked_at': 0}  # process restart state
    assert server.status()['account_status']['state'] == 'unverified'
    assert server.account_check()['account_status']['state'] == 'connected'
    assert len(transport.seen) == 3  # one refresh, two actual library requests


def test_download_diagnostic_never_stores_relay_secret(server, monkeypatch):
    class Proc:
        returncode = 1
        stderr = 'socks5h://wizard:fixturepassword@100.64.1.2:18765 Sign in to confirm you are not a bot'
    monkeypatch.setattr(server.subprocess, 'run', lambda *a, **kw: Proc())
    assert 'fixturepassword' not in json.dumps(server.test_playback())
    assert 'fixturepassword' not in server.SETTINGS.read_text()
    server._save({'playback': {'state': 'failed', 'at': 1, 'detail': 'legacy-private-password'}})
    assert 'legacy-private-password' not in json.dumps(server.status())
