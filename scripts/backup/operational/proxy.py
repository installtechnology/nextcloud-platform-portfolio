"""Fixed local UNIX socket -> SSH stdio proxy. Never knows AWS credentials."""
import os
import socket
import sys
import threading
from pathlib import Path


def relay(path, incoming, outgoing):
    if os.name != 'posix' or not Path(path).is_absolute():
        raise RuntimeError('UNIX_PROXY_REQUIRED')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(20)
        connection.connect(path)
        connection.settimeout(None)
        def send():
            try:
                # BufferedReader.read(size) can wait for a full 64 KiB and
                # deadlock the small SSH handshake. read1 returns available bytes.
                read = getattr(incoming, 'read1', incoming.read)
                while chunk := read(65536): connection.sendall(chunk)
                connection.shutdown(socket.SHUT_WR)
            except (OSError, ValueError):
                connection.close()
        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        try:
            while chunk := connection.recv(65536):
                outgoing.write(chunk); outgoing.flush()
        finally:
            connection.close()


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2: raise RuntimeError('PROXY_ARGUMENTS_REJECTED')
        relay(sys.argv[1], sys.stdin.buffer, sys.stdout.buffer)
    except BaseException:
        raise SystemExit(2) from None
