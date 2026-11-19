"""wizard-youtube-music#15: a cloud Wizard with userspace Tailscale reaches the home relay through a tailnet proxy.

Real local sockets: a fake userspace-tailscaled proxy (SOCKS5 or HTTP CONNECT) that only knows the relay's
100.x address, and a fake authenticated home relay behind it. No Tailscale, home or YouTube traffic."""
import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest
import requests

RELAY_IP, RELAY_PORT = '100.90.80.70', 18765
PASSWORD = 'p' * 40


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('WIZARD_TAILNET_PROXY', raising=False)
    spec = importlib.util.spec_from_file_location('tailnet_server', Path(__file__).parents[1] / 'app/server.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.set_relay(mod.pairing_url(dict(schema='wizard-home-relay-pairing-v1', host=RELAY_IP, port=RELAY_PORT,
                                       scheme='socks5h', username='wizard', password=PASSWORD, provider='youtube')))
    return mod


def exact(conn, size):
    data = b''
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        assert chunk
        data += chunk
    return data


def quiet(handler, conn):
    try:
        handler(conn)
    except (AssertionError, OSError):
        pass  # a client hanging up early; each test checks what the fakes saw


def listen(handler):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(8)
    def loop():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=quiet, args=(handler, conn), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return sock, sock.getsockname()[1]


@pytest.fixture
def relay_box():
    """A home relay: SOCKS5 with required password, then answers one plain HTTP request."""
    seen = []
    def handle(conn):
        with conn:
            ver, n = exact(conn, 2)
            assert ver == 5 and 2 in exact(conn, n)
            conn.sendall(b'\x05\x02')
            exact(conn, 1)
            user = exact(conn, exact(conn, 1)[0])
            password = exact(conn, exact(conn, 1)[0])
            ok = user == b'wizard' and password == PASSWORD.encode()
            conn.sendall(b'\x01\x00' if ok else b'\x01\x01')
            if not ok:
                return
            ver, cmd, _, atyp = exact(conn, 4)
            assert atyp == 3  # remote DNS: the relay resolves the name
            host = exact(conn, exact(conn, 1)[0]).decode()
            port = int.from_bytes(exact(conn, 2), 'big')
            seen.append((host, port))
            conn.sendall(b'\x05\x00\x00\x01' + bytes(6))
            request = b''
            while b'\r\n\r\n' not in request:
                request += conn.recv(4096)
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: close\r\n\r\nvia-relay')
    sock, port = listen(handle)
    yield port, seen
    sock.close()


def bridge(conn, upstream):
    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(2)
                except OSError:
                    pass
    t = threading.Thread(target=pipe, args=(upstream, conn), daemon=True)
    t.start()
    pipe(conn, upstream)
    t.join()


@pytest.fixture
def socks_tailnet(relay_box):
    """Userspace tailscaled's --socks5-server: no auth, reaches the relay only by its 100.x address."""
    relay_port, _ = relay_box
    asked = []
    def handle(conn):
        with conn:
            ver, n = exact(conn, 2)
            assert ver == 5 and 0 in exact(conn, n)
            conn.sendall(b'\x05\x00')
            ver, cmd, _, atyp = exact(conn, 4)
            assert (ver, cmd, atyp) == (5, 1, 1)
            ip = socket.inet_ntoa(exact(conn, 4))
            port = int.from_bytes(exact(conn, 2), 'big')
            asked.append((ip, port))
            if (ip, port) != (RELAY_IP, RELAY_PORT):
                return conn.sendall(b'\x05\x04\x00\x01' + bytes(6))
            upstream = socket.create_connection(('127.0.0.1', relay_port))
            conn.sendall(b'\x05\x00\x00\x01' + bytes(6))
            bridge(conn, upstream)
    sock, port = listen(handle)
    yield port, asked
    sock.close()


@pytest.fixture
def http_tailnet(relay_box):
    """Userspace tailscaled's --outbound-http-proxy-listen: HTTP CONNECT to the 100.x address."""
    relay_port, _ = relay_box
    asked = []
    def handle(conn):
        with conn:
            head = b''
            while not head.endswith(b'\r\n\r\n'):
                head += exact(conn, 1)
            line = head.split(b'\r\n')[0].decode()
            asked.append((line, b'Proxy-Authorization: Basic dTpw' in head))
            if line != f'CONNECT {RELAY_IP}:{RELAY_PORT} HTTP/1.1':
                return conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
            upstream = socket.create_connection(('127.0.0.1', relay_port))
            conn.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            bridge(conn, upstream)
    sock, port = listen(handle)
    yield port, asked
    sock.close()


def fetch(server):
    s = server._session()
    try:
        return s.get('http://music.youtube.com/probe', timeout=10).text
    finally:
        s.close()


def test_without_setting_the_relay_is_used_directly(server):
    assert server.tailnet_proxy() is None
    assert server.relay_proxy() == server.relay()
    assert server.relay_status(server._load())['tailnet_proxy'] == 'off'


def test_socks5_tailnet_proxy_carries_catalog_and_downloader(server, monkeypatch, socks_tailnet, relay_box):
    port, asked = socks_tailnet
    monkeypatch.setenv('WIZARD_TAILNET_PROXY', f'socks5://127.0.0.1:{port}')
    assert fetch(server) == 'via-relay'
    assert asked == [(RELAY_IP, RELAY_PORT)]
    assert relay_box[1] == [('music.youtube.com', 80)]
    # yt-dlp gets the same loopback-only tunnel, relay password still end to end.
    cmd = server._live_cmd(server.CACHE / '.x', 'pVfFPuUYRXY')
    proxy = cmd[cmd.index('--proxy') + 1]
    assert proxy.startswith(f'socks5h://wizard:{PASSWORD}@127.0.0.1:') and RELAY_IP not in proxy
    status = server.status()
    assert status['relay_status']['tailnet_proxy'] == 'on'
    assert str(port) not in json.dumps(status) and PASSWORD not in json.dumps(status)


def test_http_tailnet_proxy_with_credentials(server, monkeypatch, http_tailnet):
    port, asked = http_tailnet
    monkeypatch.setenv('WIZARD_TAILNET_PROXY', f'http://u:p@127.0.0.1:{port}')
    assert fetch(server) == 'via-relay'
    assert asked == [(f'CONNECT {RELAY_IP}:{RELAY_PORT} HTTP/1.1', True)]


def test_relay_probe_opens_the_relay_through_the_tailnet_proxy(server, monkeypatch, socks_tailnet, relay_box):
    port, asked = socks_tailnet
    monkeypatch.setenv('WIZARD_TAILNET_PROXY', f'socks5h://127.0.0.1:{port}')
    sock = server.tailnet_connect(RELAY_IP, RELAY_PORT, 5)
    with sock:
        sock.sendall(b'\x05\x01\x02')
        assert exact(sock, 2) == b'\x05\x02'  # the relay itself answers, asking for its password
    assert asked == [(RELAY_IP, RELAY_PORT)]


def test_tailnet_proxy_refusal_fails_closed(server, monkeypatch, socks_tailnet):
    port, _ = socks_tailnet
    monkeypatch.setenv('WIZARD_TAILNET_PROXY', f'socks5://127.0.0.1:{port}')
    with pytest.raises(OSError):
        server.tailnet_connect('100.90.80.71', RELAY_PORT, 5)


@pytest.mark.parametrize('value', ['ftp://h:1', 'socks5://h', 'http://h:1/path', 'http://h:1?x=1', 'h:1080',
                                   'socks4://h:1', 'http://:1'])
def test_invalid_setting_is_reported_not_used(server, monkeypatch, value):
    monkeypatch.setenv('WIZARD_TAILNET_PROXY', value)
    assert server.tailnet_proxy() == 'invalid'
    assert server.relay_proxy() == server.relay()
    status = server.relay_status(server._load())
    assert status['tailnet_proxy'] == 'invalid' and 'WIZARD_TAILNET_PROXY' in status['message']
    assert value not in status['message']
