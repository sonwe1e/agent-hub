"""Actual local proxy sockets, bypass rules, credentials and fail-closed behavior."""
import base64
from datetime import datetime, timedelta, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from proxy_fixture import ConnectFixture, SocksFixture
from upstream_transport import ProxyError, connection_for
import workbuddy


# On Windows environment keys are case-insensitive, so put uppercase last.
PROXY_ENV = {name: '' for name in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
                                  'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')}


class ProxyTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        owner = self
        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                owner.calls.append((self.path, dict(self.headers)))
                data = json.dumps({'code': 0, 'data': {'via': 'fixture'}}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
        self.origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        threading.Thread(target=self.origin.serve_forever, daemon=True).start()
        self.addCleanup(self.origin.server_close); self.addCleanup(self.origin.shutdown)

    def test_socks5h_auth_and_remote_dns_use_real_relay(self):
        proxy = SocksFixture(self.origin.server_address, 'fixture-user', 'fixture-pass')
        self.addCleanup(proxy.close)
        env = {**PROXY_ENV, 'HTTP_PROXY': f'socks5h://fixture-user:fixture-pass@127.0.0.1:{proxy.port}'}
        with patch.dict(os.environ, env):
            conn, target, headers = connection_for(f'http://unresolvable.invalid:{self.origin.server_port}/models?x=1')
            conn.request('GET', target, headers=headers)
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
        self.assertEqual(proxy.targets, [('unresolvable.invalid', self.origin.server_port)])
        self.assertEqual(proxy.authentications, 1)
        self.assertEqual(self.calls[0][0], '/models?x=1')
        self.assertNotIn('Proxy-Authorization', self.calls[0][1])

    def test_socks5_resolves_destination_locally(self):
        proxy = SocksFixture(self.origin.server_address)
        self.addCleanup(proxy.close)
        env = {**PROXY_ENV, 'HTTP_PROXY': f'socks5://127.0.0.1:{proxy.port}'}
        with patch.dict(os.environ, env):
            conn, target, headers = connection_for(f'http://localhost:{self.origin.server_port}/')
            conn.request('GET', target, headers=headers)
            self.assertEqual(conn.getresponse().status, 200); conn.close()
        self.assertIn(proxy.targets[0][0], ('127.0.0.1', '::1'))

    def test_http_proxy_is_used_by_workbuddy_api_and_proxy_auth_is_not_in_path(self):
        env = {**PROXY_ENV, 'HTTP_PROXY': f'http://fixture-user:fixture-pass@127.0.0.1:{self.origin.server_port}'}
        with patch.dict(os.environ, env), patch.object(workbuddy, 'ENDPOINT', 'http://vendor.invalid'):
            self.assertEqual(workbuddy.call('/v3/config'), {'via': 'fixture'})
        path, headers = self.calls[0]
        self.assertEqual(path, 'http://vendor.invalid/v3/config')
        self.assertEqual(headers['Proxy-Authorization'], 'Basic ' + base64.b64encode(b'fixture-user:fixture-pass').decode())
        self.assertNotIn('fixture-pass', path)

    def test_no_proxy_bypasses_invalid_proxy_and_honors_port(self):
        env = {**PROXY_ENV, 'HTTP_PROXY': 'unsupported://user:secret@invalid',
               'NO_PROXY': f'127.0.0.1:{self.origin.server_port}'}
        with patch.dict(os.environ, env):
            conn, target, headers = connection_for(f'http://127.0.0.1:{self.origin.server_port}/')
            conn.request('GET', target, headers=headers)
            self.assertEqual(conn.getresponse().status, 200); conn.close()
            with self.assertRaises(ProxyError) as error:
                connection_for(f'http://127.0.0.1:{self.origin.server_port + 1}/')
        self.assertNotIn('secret', str(error.exception))

    def test_no_proxy_matches_ipv6_network_and_wildcard_domain(self):
        from upstream_transport import proxy_for
        from urllib.parse import urlsplit
        env = {**PROXY_ENV, 'HTTP_PROXY': 'socks4://invalid',
               'NO_PROXY': '::1,127.0.0.0/8,*.example.invalid'}
        with patch.dict(os.environ, env):
            self.assertIsNone(proxy_for(urlsplit('http://[::1]:1234/')))
            self.assertIsNone(proxy_for(urlsplit('http://127.12.3.4/')))
            self.assertIsNone(proxy_for(urlsplit('http://a.example.invalid/')))
            with self.assertRaises(ProxyError):
                proxy_for(urlsplit('http://elsewhere.invalid/'))

    def test_socks_authentication_failure_does_not_reveal_credentials(self):
        proxy = SocksFixture(self.origin.server_address, 'actual-user', 'actual-password')
        self.addCleanup(proxy.close)
        env = {**PROXY_ENV, 'HTTP_PROXY': f'socks5h://wrong-user:secret-value@127.0.0.1:{proxy.port}'}
        with patch.dict(os.environ, env):
            conn, target, headers = connection_for('http://vendor.invalid/')
            with self.assertRaises(ProxyError) as error:
                conn.request('GET', target, headers=headers)
            conn.close()
        self.assertIn('认证失败', str(error.exception))
        self.assertNotIn('secret-value', str(error.exception))
        self.assertEqual(self.calls, [])

    def test_invalid_proxy_fails_without_direct_connection(self):
        env = {**PROXY_ENV, 'HTTP_PROXY': 'socks4://user:secret@invalid'}
        with patch.dict(os.environ, env), self.assertRaises(ProxyError) as error:
            connection_for(f'http://127.0.0.1:{self.origin.server_port}/')
        self.assertIn('未改为直连', str(error.exception))
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(self.calls, [])

    def test_https_http_proxy_credentials_are_only_sent_in_connect(self):
        env = {**PROXY_ENV, 'HTTPS_PROXY': 'http://name:password@127.0.0.1:3128'}
        with patch.dict(os.environ, env):
            conn, target, headers = connection_for('https://vendor.invalid/v1/models')
        self.assertIsInstance(conn, http.client.HTTPSConnection)
        self.assertEqual((conn._tunnel_host, conn._tunnel_port, target), ('vendor.invalid', 443, '/v1/models'))
        self.assertEqual(headers, {})
        self.assertIn('Proxy-Authorization', conn._tunnel_headers)

    def test_https_socks_and_http_connect_tls_to_the_origin(self):
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.x509.oid import NameOID
        except ImportError:
            self.skipTest('cryptography is needed to create a temporary test certificate')
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        cert_path, key_path = Path(temp.name)/'test-cert.pem', Path(temp.name)/'test-key.pem'
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        tls_server = ThreadingHTTPServer(('127.0.0.1', 0), self.origin.RequestHandlerClass)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert_path, key_path)
        tls_server.socket = context.wrap_socket(tls_server.socket, server_side=True)
        threading.Thread(target=tls_server.serve_forever, daemon=True).start()
        self.addCleanup(tls_server.server_close); self.addCleanup(tls_server.shutdown)
        socks = SocksFixture(tls_server.server_address); self.addCleanup(socks.close)
        connect = ConnectFixture(tls_server.server_address); self.addCleanup(connect.close)
        for scheme, proxy in [('socks5h', socks), ('http', connect)]:
            with self.subTest(proxy=scheme):
                auth = 'name:password@' if scheme == 'http' else ''
                env = {**PROXY_ENV, 'HTTPS_PROXY': f'{scheme}://{auth}127.0.0.1:{proxy.port}'}
                with patch.dict(os.environ, env):
                    conn, target, headers = connection_for(f'https://localhost:{tls_server.server_port}/secure')
                    conn._context = ssl.create_default_context(cafile=str(cert_path))
                    conn.request('GET', target, headers=headers)
                    response = conn.getresponse(); self.assertEqual(response.status, 200)
                    response.read(); conn.close()
        self.assertEqual(socks.targets, [('localhost', tls_server.server_port)])
        self.assertTrue(connect.requests[0].startswith(f'CONNECT localhost:{tls_server.server_port} '.encode()))
        self.assertIn(b'Proxy-Authorization: Basic ', connect.requests[0])
        self.assertTrue(all('Proxy-Authorization' not in headers for _, headers in self.calls))


if __name__ == '__main__':
    unittest.main()
