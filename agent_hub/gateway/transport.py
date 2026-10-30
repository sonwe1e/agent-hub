"""Shared outbound HTTP, HTTPS and SOCKS5 transport for the Hub gateway.

Explicit proxy settings are never silently ignored. No proxy credentials are
included in errors or forwarded to the origin server.
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import os
import socket
import urllib.parse
import urllib.request


class ProxyError(ValueError):
    """A proxy setting or handshake that cannot safely be used."""


def _bypass(url):
    host = (url.hostname or '').lower().rstrip('.')
    port = url.port or (443 if url.scheme == 'https' else 80)
    raw = os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or ''
    if not raw and 'NO_PROXY' not in os.environ and 'no_proxy' not in os.environ:
        # Preserve platform proxy bypass rules when no environment override exists.
        if urllib.request.proxy_bypass(url.netloc):
            return True
    for item in raw.split(','):
        rule = item.strip().lower()
        if not rule:
            continue
        if rule == '*':
            return True
        try:
            # Support the common unbracketed IPv6 and CIDR spellings too.
            network = ipaddress.ip_network(rule.strip('[]'), strict=False)
            if ipaddress.ip_address(host) in network:
                return True
            continue
        except ValueError:
            pass
        # urlsplit handles bracketed IPv6 and an optional port without guessing.
        try:
            parsed = urllib.parse.urlsplit('//' + rule)
            domain, wanted_port = (parsed.hostname or '').removeprefix('*.').lstrip('.').rstrip('.'), parsed.port
        except ValueError:
            continue
        if wanted_port is not None and wanted_port != port:
            continue
        if domain and (host == domain or host.endswith('.' + domain)):
            return True
    return False


def proxy_for(url):
    """Return parsed explicit proxy or None; never log its credential-bearing URL."""
    if _bypass(url):
        return None
    if url.scheme == 'https':
        raw = (os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
               or os.environ.get('ALL_PROXY') or os.environ.get('all_proxy'))
    else:
        raw = (os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
               or os.environ.get('ALL_PROXY') or os.environ.get('all_proxy'))
    if not raw:
        # urllib also reads the native Windows/macOS proxy settings. Both
        # WorkBuddy authentication and model traffic now honor the same source.
        system = urllib.request.getproxies()
        raw = system.get(url.scheme) or system.get('all')
    if not raw:
        return None
    try:
        proxy = urllib.parse.urlsplit(raw if '://' in raw else 'http://' + raw)
        host, port = proxy.hostname, proxy.port
        if (proxy.scheme.lower() not in ('http', 'socks5', 'socks5h') or not host
                or proxy.path not in ('', '/') or proxy.query or proxy.fragment):
            raise ValueError()
        if port is None:
            port = 1080 if proxy.scheme.lower().startswith('socks') else 80
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise ProxyError('代理配置无效或协议不支持；请使用 HTTP、SOCKS5 或 SOCKS5H，未改为直连') from None
    username = urllib.parse.unquote(proxy.username or '')
    password = urllib.parse.unquote(proxy.password or '')
    return {'scheme': proxy.scheme.lower(), 'host': host, 'port': port,
            'username': username, 'password': password,
            'credentials': proxy.username is not None}


def _recv(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ProxyError('SOCKS5 代理连接提前关闭')
        data.extend(chunk)
    return bytes(data)


def _socks_connect(proxy, host, port, timeout, source_address=None):
    sock = socket.create_connection((proxy['host'], proxy['port']), timeout, source_address)
    try:
        methods = b'\x00\x02' if proxy['credentials'] else b'\x00'
        sock.sendall(b'\x05' + bytes([len(methods)]) + methods)
        version, method = _recv(sock, 2)
        if version != 5 or method not in methods:
            raise ProxyError('SOCKS5 代理拒绝认证方式')
        if method == 2:
            username = proxy['username'].encode('utf-8')
            password = proxy['password'].encode('utf-8')
            if not username or len(username) > 255 or len(password) > 255:
                raise ProxyError('SOCKS5 代理账号格式无效')
            sock.sendall(b'\x01' + bytes([len(username)]) + username + bytes([len(password)]) + password)
            if _recv(sock, 2) != b'\x01\x00':
                raise ProxyError('SOCKS5 代理认证失败')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is None and proxy['scheme'] == 'socks5':
            # socks5 resolves locally; socks5h delegates hostname lookup to the proxy.
            address = ipaddress.ip_address(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0])
        if address is not None:
            dest = (b'\x01' if address.version == 4 else b'\x04') + address.packed
        else:
            hostname = host.encode('idna')
            if not hostname or len(hostname) > 255:
                raise ProxyError('SOCKS5 目标域名格式无效')
            dest = b'\x03' + bytes([len(hostname)]) + hostname
        sock.sendall(b'\x05\x01\x00' + dest + int(port).to_bytes(2, 'big'))
        version, status, reserved, kind = _recv(sock, 4)
        if version != 5 or reserved != 0 or status != 0:
            raise ProxyError('SOCKS5 代理无法连接上游')
        if kind == 1:
            _recv(sock, 4)
        elif kind == 4:
            _recv(sock, 16)
        elif kind == 3:
            _recv(sock, _recv(sock, 1)[0])
        else:
            raise ProxyError('SOCKS5 代理返回无效地址')
        _recv(sock, 2)
        return sock
    except BaseException:
        sock.close()
        raise


class _SocksHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, *, proxy, timeout):
        self.proxy = proxy
        super().__init__(host, port, timeout=timeout)

    def connect(self):
        self.sock = _socks_connect(self.proxy, self.host, self.port, self.timeout, self.source_address)


class _SocksHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, *, proxy, timeout):
        self.proxy = proxy
        super().__init__(host, port, timeout=timeout)

    def connect(self):
        raw = _socks_connect(self.proxy, self.host, self.port, self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def connection_for(url, *, timeout=180):
    """Return (connection, request target, proxy-only headers) for an upstream URL."""
    if isinstance(url, str):
        url = urllib.parse.urlsplit(url)
    try:
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            raise ValueError()
        port = url.port or (443 if url.scheme == 'https' else 80)
    except ValueError:
        raise ValueError('上游地址无效') from None
    target = urllib.parse.urlunsplit(('', '', url.path or '/', url.query, ''))
    proxy = proxy_for(url)
    if not proxy:
        cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        return cls(url.hostname, port, timeout=timeout), target, {}
    if proxy['scheme'].startswith('socks5'):
        cls = _SocksHTTPS if url.scheme == 'https' else _SocksHTTP
        return cls(url.hostname, port, proxy=proxy, timeout=timeout), target, {}
    headers = {}
    if proxy['credentials']:
        token = base64.b64encode((proxy['username'] + ':' + proxy['password']).encode('utf-8')).decode('ascii')
        headers['Proxy-Authorization'] = 'Basic ' + token
    if url.scheme == 'https':
        conn = http.client.HTTPSConnection(proxy['host'], proxy['port'], timeout=timeout)
        conn.set_tunnel(url.hostname, port, headers=headers)
        return conn, target, {}
    conn = http.client.HTTPConnection(proxy['host'], proxy['port'], timeout=timeout)
    absolute = urllib.parse.urlunsplit((url.scheme, url.netloc, url.path or '/', url.query, ''))
    return conn, absolute, headers
