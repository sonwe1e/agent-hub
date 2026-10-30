"""Small local SOCKS5 relay for transport integration tests."""
import select
import socket
import socketserver
import threading


def read_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        data = sock.recv(count - len(result))
        if not data:
            raise EOFError('proxy client closed')
        result.extend(data)
    return bytes(result)


class SocksFixture:
    def __init__(self, destination, username=None, password=None):
        owner = self
        self.destination = destination
        self.username, self.password = username, password
        self.targets = []
        self.authentications = 0

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                client = self.request
                version, count = read_exact(client, 2)
                methods = read_exact(client, count)
                wanted = 2 if owner.username is not None else 0
                if version != 5 or wanted not in methods:
                    client.sendall(b'\x05\xff'); return
                client.sendall(bytes([5, wanted]))
                if wanted == 2:
                    version, nuser = read_exact(client, 2)
                    user = read_exact(client, nuser).decode()
                    password = read_exact(client, read_exact(client, 1)[0]).decode()
                    if version != 1 or (user, password) != (owner.username, owner.password):
                        client.sendall(b'\x01\x01'); return
                    owner.authentications += 1
                    client.sendall(b'\x01\x00')
                version, cmd, _, kind = read_exact(client, 4)
                if kind == 1:
                    host = socket.inet_ntop(socket.AF_INET, read_exact(client, 4))
                elif kind == 4:
                    host = socket.inet_ntop(socket.AF_INET6, read_exact(client, 16))
                elif kind == 3:
                    host = read_exact(client, read_exact(client, 1)[0]).decode('idna')
                else:
                    return
                port = int.from_bytes(read_exact(client, 2), 'big')
                owner.targets.append((host, port))
                if version != 5 or cmd != 1:
                    return
                with socket.create_connection(owner.destination, timeout=3) as upstream:
                    client.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
                    while True:
                        ready, _, _ = select.select([client, upstream], [], [], 3)
                        if not ready:
                            return
                        for src in ready:
                            try:
                                data = src.recv(65536)
                            except ConnectionError:
                                return
                            if not data:
                                return
                            try:
                                (upstream if src is client else client).sendall(data)
                            except ConnectionError:
                                return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ConnectFixture:
    def __init__(self, destination):
        owner = self
        self.destination = destination
        self.requests = []
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                client = self.request
                raw = bytearray()
                while not raw.endswith(b'\r\n\r\n') and len(raw) < 8192:
                    raw.extend(read_exact(client, 1))
                owner.requests.append(bytes(raw))
                if not raw.startswith(b'CONNECT '):
                    return
                with socket.create_connection(owner.destination, timeout=3) as upstream:
                    client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    while True:
                        ready, _, _ = select.select([client, upstream], [], [], 3)
                        if not ready:
                            return
                        for src in ready:
                            try:
                                data = src.recv(65536)
                                if not data:
                                    return
                                (upstream if src is client else client).sendall(data)
                            except ConnectionError:
                                return
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
