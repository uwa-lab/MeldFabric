from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn


@pytest.fixture
def serve_admin():
    servers = []

    def start(kit):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(kit.create_app(), log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        servers.append((server, thread, sock))
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return origin

    yield start
    for server, thread, sock in servers:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
