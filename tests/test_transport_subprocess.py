"""Integration test for the real subprocess transport.

Spawns a child process (the fake server over stdio) through
``mfatsftp.from_command`` to exercise the actual ``Popen`` path: pipe framing,
the stderr drain, the handshake, and the clean teardown in ``close()``.
"""

import os
import sys
import time

import pytest

from mfatsftp import from_command

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child_env():
    return {"PYTHONPATH": os.path.join(_ROOT, "src") + os.pathsep + _ROOT}


def test_from_command_subprocess_roundtrip(tmp_path):
    argv = [sys.executable, os.path.join(_ROOT, "tests", "sftp_stdio_server.py")]
    client = from_command(argv, env=_child_env())
    try:
        assert client.realpath(".") == "/home/alice"
        assert sorted(client.listdir("/home/alice")) == ["notes.txt", "sub"]
        dest = tmp_path / "notes.txt"
        assert client.get("/home/alice/notes.txt", dest) == len(b"hello world")
        assert dest.read_bytes() == b"hello world"
    finally:
        client.close()  # terminate + reap + close pipes, no hang/EBADF

    # The owned subprocess is reaped by close().
    assert client._proc.poll() is not None


def test_handshake_timeout_stops_a_silent_command():
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        from_command(argv, timeout=0.5)
    assert time.monotonic() - started < 10


def test_auth_failure_becomes_permission_error():
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('user@host: Permission denied (publickey).\\n')",
    ]
    with pytest.raises(PermissionError, match="Permission denied"):
        from_command(argv)


def test_other_ssh_failure_carries_its_message():
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('ssh: Could not resolve hostname nope\\n'); sys.exit(255)",
    ]
    with pytest.raises(OSError, match="Could not resolve hostname"):
        from_command(argv)
