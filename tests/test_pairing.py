"""Private-file contract, real local HTTP guards and SOCKS wire tests; no home/Google traffic."""
import importlib.util
import json
from pathlib import Path
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest
import requests


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    spec = importlib.util.spec_from_file_location('pairing_server', Path(__file__).parents[1] / 'app/server.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pairing(**changes):
    return dict(schema='wizard-home-relay-pairing-v1', host='100.90.80.70', port=18765,
                scheme='socks5h', username='wizard', password='offline_fixture_' * 3,
                provider='youtube', allowed_domains=['evil.example'], **{}) | changes


@pytest.mark.parametrize('field,value', [
    ('schema', 'unknown'), ('scheme', 'http'), ('scheme', 'socks5'), ('username', 'admin'),
    ('provider', 'other'), ('port', True), ('port', '18765'), ('port', 0), ('port', 65536),
    ('host', '127.0.0.1'), ('host', '192.168.1.1'), ('host', '8.8.8.8'), ('host', '100.63.255.255'),
    ('host', '100.128.0.0'), ('host', '::1'), ('host', '::ffff:100.90.80.70'), ('host', 'home.ts.net'),
    ('host', 'user@100.90.80.70'), ('host', '100.90.80.70/path'), ('host', '*'), ('host', 123),
    ('host', '100.090.080.070'), ('password', 'too-short'), ('password', 'x' * 129),
    ('password', 'x' * 32 + '\n'), ('password', 'x' * 32 + '@')])
def test_validation_never_echoes_or_writes(server, field, value):
    with pytest.raises(ValueError) as error:
        server.import_pairing({'pairing': pairing(**{field: value}), 'confirm_home_device': True})
    assert str(value) not in str(error.value) or value in ('http', 'other')
    assert not server.SETTINGS.exists()


def test_confirmation_persistence_readback_and_domain_hints(server):
    with pytest.raises(ValueError):
        server.import_pairing({'pairing': pairing()})
    result = server.import_pairing({'pairing': pairing(), 'confirm_home_device': True})
    assert result['relay_status']['state'] == 'imported_unverified'
    assert result['ready'] is False and result['account'] is False
    assert pairing()['password'] not in json.dumps(result)
    assert '100.90.80.70' not in json.dumps(result)
    assert server.SETTINGS.stat().st_mode & 0o777 == 0o600
    saved = json.loads(server.SETTINGS.read_text())
    assert saved['relay'] == server.pairing_url(pairing())
    assert 'allowed_domains' not in saved
    server._clients.clear()
    assert server.relay() == saved['relay']
    assert server.status()['relay_status']['state'] == 'imported_unverified'
    assert server.set_relay('')['relay_status']['state'] == 'not_configured'
    assert 'relay' not in json.loads(server.SETTINGS.read_text())


@pytest.mark.parametrize('http_code,state', [(200, 'verified'), (302, 'failed'), (403, 'provider_blocked'), (429, 'provider_blocked'), (500, 'failed')])
def test_fixed_bounded_probe_independent_of_playback(server, monkeypatch, http_code, state):
    server.import_pairing({'pairing': pairing(), 'confirm_home_device': True})
    def probe(value):
        assert value == server.pairing_url(pairing())
        return http_code
    monkeypatch.setattr(server, '_relay_probe', probe)
    result = server.test_relay()
    assert result['relay_status']['state'] == state
    assert result['playback']['state'] == 'untested' and not result['account']
    assert server.status()['relay_status']['state'] == state
    server.import_pairing({'pairing': pairing(), 'confirm_home_device': True})
    assert server.status()['relay_status']['state'] == 'imported_unverified'


def test_probe_exception_is_redacted(server, monkeypatch):
    server.set_relay(server.pairing_url(pairing()))
    def fail(*args, **kwargs): raise RuntimeError(pairing()['password'])
    monkeypatch.setattr(server, '_relay_probe', fail)
    result = server.test_relay()
    assert result['relay_status']['state'] == 'failed'
    assert pairing()['password'] not in json.dumps(result)
    assert 'detail' not in server._load()['relay_check']


def configure(server):
    server.RELAY_ORIGIN = 'https://wizard.example'
    server.RELAY_KEY.write_text('offline_authorization_' * 3)
    server.RELAY_KEY.chmod(0o600)
    return {'Host': 'wizard.example', 'Origin': server.RELAY_ORIGIN,
            'Authorization': 'Bearer ' + server.RELAY_KEY.read_text(), 'Sec-Fetch-Site': 'same-origin'}


def test_real_http_auth_origin_limits_no_echo_and_readback(server, capsys, monkeypatch):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    payload = {'pairing': pairing(), 'confirm_home_device': True}
    try:
        assert requests.post(base + '/api/relay/import', json=payload).status_code == 503
        headers = configure(server)
        for changes, expected in [({'Authorization': ''}, 401), ({'Authorization': 'Bearer wrong'}, 401),
                                  ({'Origin': ''}, 403), ({'Origin': 'https://evil.example'}, 403),
                                  ({'Host': 'evil.example'}, 403), ({'Sec-Fetch-Site': 'same-site'}, 403)]:
            response = requests.post(base + '/api/relay/import', headers=headers | changes, json=payload)
            assert response.status_code == expected
            assert not server.SETTINGS.exists()
        assert requests.post(base + '/api/relay/import?password=sentinel', headers=headers, json=payload).status_code == 403
        assert requests.post(base + '/api/relay/import', headers=headers, json=[]).status_code == 400
        assert requests.post(base + '/api/relay/import', headers=headers | {'Content-Type': 'application/json'}, data='{"password":"private-malformed",').status_code == 400
        assert requests.post(base + '/api/relay/import', headers=headers, json={'large': 'x' * 65536}).status_code == 413
        assert requests.post(base + '/api/relay/import', headers=headers, data='{}').status_code == 415
        response = requests.post(base + '/api/relay/import', headers=headers, json=payload)
        assert response.status_code == 200
        assert response.json()['relay_status']['state'] == 'imported_unverified'
        assert requests.get(base + '/api/status').json()['relay'] is True
        original = server.SETTINGS.read_bytes()
        assert requests.post(base + '/api/relay/import', headers=headers,
                             json={'pairing': pairing(host='evil.example'), 'confirm_home_device': True}).status_code == 400
        assert server.SETTINGS.read_bytes() == original
        monkeypatch.setattr(server, '_relay_probe', lambda value: 200)
        assert requests.post(base + '/api/relay/test', headers=headers, json={}).json()['relay_status']['state'] == 'verified'
        checked = requests.get(base + '/api/status').json()
        assert checked['relay_status']['state'] == 'verified' and checked['ready'] is False
        assert requests.post(base + '/api/relay', headers=headers, json={'relay': server.relay()}).status_code == 400
        response = requests.post(base + '/api/relay', headers=headers, json={'relay': ''})
        assert response.status_code == 200
        assert requests.get(base + '/api/status').json()['relay'] is False
        captured = capsys.readouterr()
        for secret in (pairing()['password'], server.RELAY_KEY.read_text(), 'sentinel', 'private-malformed'):
            assert secret not in captured.out + captured.err + response.text
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join()


def test_import_key_permissions_and_symlink_fail_closed(server):
    configure(server)
    assert server.status()['relay_status']['import_enabled']
    server.RELAY_KEY.chmod(0o644)
    assert not server.status()['relay_status']['import_enabled']
    server.RELAY_KEY.rename(server.RELAY_KEY.with_suffix('.backup'))
    server.RELAY_KEY.symlink_to(server.RELAY_KEY.with_suffix('.backup'))
    assert not server.status()['relay_status']['import_enabled']


@pytest.mark.parametrize('origin', ['http://wizard.example', 'https://wizard.example/',
                                  'https://wizard.example/path', 'https://user@wizard.example',
                                  'https://[invalid', 'https://wizard.example:invalid'])
def test_bad_import_origin_is_disabled(server, origin):
    configure(server)
    server.RELAY_ORIGIN = origin
    assert not server.status()['relay_status']['import_enabled']


def test_real_socks_transport_authentication_and_remote_dns(server, monkeypatch):
    """Run the real probe against a local, non-forwarding fixture service."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(1); listener.settimeout(5)
    port = listener.getsockname()[1]
    seen = {}
    errors = []
    def exact(conn, size):
        data = b''
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk: raise RuntimeError('unexpected EOF')
            data += chunk
        return data
    def fixture():
        try:
            with listener.accept()[0] as conn:
                conn.settimeout(5)
                version, count = exact(conn, 2)
                assert version == 5 and 2 in exact(conn, count)
                conn.sendall(b'\x05\x02')
                version, size = exact(conn, 2)
                assert version == 1
                seen['username'] = exact(conn, size).decode()
                seen['password'] = exact(conn, exact(conn, 1)[0]).decode()
                conn.sendall(b'\x01\x00')
                assert exact(conn, 4) == b'\x05\x01\x00\x03'
                seen['domain'] = exact(conn, exact(conn, 1)[0]).decode()
                seen['port'] = int.from_bytes(exact(conn, 2), 'big')
                # Refuse CONNECT; no internet traffic or fake successful TLS.
                conn.sendall(b'\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00')
        except Exception as exc: errors.append(exc)
        finally: listener.close()
    worker = threading.Thread(target=fixture, daemon=True); worker.start()
    monkeypatch.setattr(server, 'relay', lambda: f'socks5h://wizard:{pairing()["password"]}@127.0.0.1:{port}')
    result = server.test_relay()
    worker.join(6)
    assert not worker.is_alive() and not errors
    assert seen == {'username': 'wizard', 'password': pairing()['password'], 'domain': 'music.youtube.com', 'port': 443}
    assert result['relay_status']['state'] == 'failed'


@pytest.mark.parametrize('reply,expected', [(b'HTTP/1.1 200 OK\r\n', 200), (b'HTTP/1.1 429 Too Many Requests\r\n', 429), (b'x' * 300, None)])
def test_probe_protocol_tls_fixed_endpoint_and_byte_cap(server, monkeypatch, reply, expected):
    import ssl
    class Socket:
        def __init__(self, data): self.data, self.sent, self.timeouts = data, [], []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def recv(self, size):
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk
        def sendall(self, data): self.sent.append(data)
        def settimeout(self, seconds):
            assert 0 < seconds <= 15
            self.timeouts.append(seconds)
    raw = Socket(b'\x05\x02\x01\x00\x05\x00\x00\x01' + b'\x00' * 6)
    tls = Socket(reply)
    def connect(address, timeout):
        assert address == ('100.90.80.70', 18765) and 0 < timeout <= 15
        return raw
    class Context:
        def wrap_socket(self, sock, server_hostname):
            assert sock is raw and server_hostname == 'music.youtube.com'
            return tls
    monkeypatch.setattr(socket, 'create_connection', connect)
    monkeypatch.setattr(ssl, 'create_default_context', lambda: Context())
    if expected is None:
        with pytest.raises(OSError): server._relay_probe(server.pairing_url(pairing()))
        assert len(tls.data) == 44  # stopped after 256 bytes, never fetched a body
    else:
        assert server._relay_probe(server.pairing_url(pairing())) == expected
    assert raw.sent[0] == b'\x05\x01\x02'
    assert raw.sent[2] == b'\x05\x01\x00\x03\x11music.youtube.com\x01\xbb'
    assert tls.sent == [b'HEAD / HTTP/1.1\r\nHost: music.youtube.com\r\nConnection: close\r\n\r\n']


def test_probe_deadline_and_no_auth_downgrade(server, monkeypatch):
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def sendall(self, data): pass
        def recv(self, size): return b'\x05\x00'  # no-auth is never accepted
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: Socket())
    with pytest.raises(OSError, match='authentication required'):
        server._relay_probe(server.pairing_url(pairing()))
    times = iter([0, 16])
    monkeypatch.setattr(server.time, 'monotonic', lambda: next(times))
    with pytest.raises(TimeoutError):
        server._relay_probe(server.pairing_url(pairing()))
