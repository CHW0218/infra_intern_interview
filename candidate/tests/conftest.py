import os
import socket
import subprocess
import sys
import time
import pytest

MOCK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mock_servers"))
_VENV_PY = os.path.join(MOCK_DIR, ".venv", "bin", "python")
PY = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable


def _port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _http_ready(url):
    import httpx
    try:
        httpx.get(url, timeout=1.0)
        return True
    except Exception:
        return False


def _ensure(script, port, ready):
    """Start one mock server if its port isn't already serving. Return proc or None."""
    if _port_open("localhost", port):
        return None
    proc = subprocess.Popen([PY, script], cwd=MOCK_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if ready():
            return proc
        time.sleep(0.5)
    proc.terminate()
    pytest.skip(f"{script} did not start")


@pytest.fixture(scope="session")
def lambda_server():
    proc = _ensure("lambda_server.py", 8002, lambda: _http_ready("http://localhost:8002/docs"))
    yield
    if proc:
        proc.terminate()


@pytest.fixture(scope="session")
def crusoe_server():
    proc = _ensure("crusoe_server.py", 8001, lambda: _http_ready("http://localhost:8001/docs"))
    yield
    if proc:
        proc.terminate()


@pytest.fixture(scope="session")
def nebius_server():
    proc = _ensure("nebius_server.py", 50051, lambda: _port_open("localhost", 50051))
    time.sleep(1.0)
    yield
    if proc:
        proc.terminate()


@pytest.fixture(scope="session")
def all_servers(lambda_server, crusoe_server, nebius_server):
    yield
