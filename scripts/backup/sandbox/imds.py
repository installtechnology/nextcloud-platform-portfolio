"""Fictitious IMDSv2 protocol. No client, upstream, AWS SDK or import side effects."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import secrets
import ssl
import stat
import time
from threading import RLock

ROLE = 'synthetic-sandbox-role'


class Protocol:
    def __init__(self, clock=time.time, random=secrets.token_urlsafe):
        self.clock, self.random = clock, random
        self.tokens = {}
        self.credentials = None
        self.lock = RLock()

    def request(self, method, path, headers, body_length=0):
        with self.lock:
            return self._request(method, path, headers, body_length)

    def _request(self, method, path, headers, body_length=0):
        if type(body_length) is not int or body_length != 0 or len(headers) > 32:
            return 400, b''
        if any(not isinstance(v, str) or len(v) > 512 for v in headers.values()):
            return 400, b''
        headers = {k.lower(): v for k, v in headers.items()}
        now = self.clock()
        self.tokens = {k: v for k, v in self.tokens.items() if v > now}
        if method == 'PUT' and path == '/latest/api/token':
            ttl = headers.get('x-aws-ec2-metadata-token-ttl-seconds', '')
            if not ttl.isascii() or not ttl.isdecimal() or not 1 <= int(ttl) <= 21600:
                return 400, b''
            if len(self.tokens) >= 64:
                return 429, b''
            token = self.random(32)
            self.tokens[token] = now + int(ttl)
            return 200, token.encode('ascii')
        if method != 'GET':
            return 405, b''
        if headers.get('x-aws-ec2-metadata-token') not in self.tokens:
            return 401, b''
        if path == '/latest/meta-data/iam/security-credentials/':
            return 200, (ROLE + '\n').encode()
        if path != '/latest/meta-data/iam/security-credentials/' + ROLE:
            return 404, b''
        if self.credentials is None or self.credentials[0] < now + 60:
            expires = now + 3600
            utc = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')
            # Random synthetic values are not backed by any AWS identity.
            self.credentials = (expires, {'Code': 'Success', 'Type': 'AWS-HMAC',
                'AccessKeyId': 'SYNTHETIC' + self.random(12),
                'SecretAccessKey': self.random(32), 'Token': self.random(32),
                'LastUpdated': utc(now), 'Expiration': utc(expires)})
        return 200, json.dumps(self.credentials[1]).encode()


def server():
    # Only an explicit future container invocation opens a listener.
    if (os.name != 'posix' or os.geteuid() != 1000
            or os.environ.get('PLATFORM_SYNTHETIC_SANDBOX') != 's3-imds-v1'
            or not re.fullmatch(r'nc-s3-imds-test-[a-f0-9]{16}', os.environ.get('PLATFORM_SANDBOX_PROJECT', ''))
            or any(k.startswith(('AWS_', 'MOTO_')) or k.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
                   for k in os.environ)):
        raise RuntimeError('SANDBOX_SERVER_CONTEXT_REJECTED')
    for name in ('server.crt', 'server.key'):
        info = Path('/fixture', name).lstat()
        if not stat.S_ISREG(info.st_mode) or (name.endswith('key') and info.st_mode & 0o077):
            raise RuntimeError('SANDBOX_TLS_INPUT_REJECTED')
    protocol = Protocol()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log request headers/tokens/credential response bytes.

        def handle_request(self):
            try:
                names = [k.lower() for k in self.headers.keys()]
                if self.headers.get('Transfer-Encoding') or len(names) != len(set(names)):
                    status, body = 400, b''
                else:
                    status, body = protocol.request(self.command, self.path, dict(self.headers),
                                                    int(self.headers.get('Content-Length', '0')))
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.close_connection = True
        do_PUT = handle_request
        do_GET = handle_request
        do_POST = handle_request
        do_DELETE = handle_request
    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, *args):
            pass
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain('/fixture/server.crt', '/fixture/server.key')
    listener = QuietServer(('0.0.0.0', 1338), Handler)
    listener.socket = context.wrap_socket(listener.socket, server_side=True)
    listener.serve_forever()


if __name__ == '__main__':
    try:
        server()
    except Exception:
        print('{"code":"SANDBOX_IMDS_START_FAILED","backup_complete":false}')
        raise SystemExit(2)
