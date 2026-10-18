"""Helpers for tests that run against the real OpenSSH ``sftp-server`` binary.

The client talks to ``sftp-server`` directly over its stdin/stdout — the same
bytes it would see through ``ssh -s host sftp``, minus the network. Tests that
use these helpers are skipped when the binary is not installed.

``_DelayLine`` delays the server's replies to simulate a slow link, so tests
can count round trips.
"""

from __future__ import annotations

import heapq
import os
import shutil
import subprocess
import threading
import time

import pytest

from mfatsftp import SFTPClient

SFTP_SERVER = next(
    (
        path
        for path in (
            "/usr/lib/openssh/sftp-server",
            "/usr/libexec/openssh/sftp-server",
            "/usr/lib/ssh/sftp-server",
            "/usr/libexec/sftp-server",
        )
        if os.access(path, os.X_OK)
    ),
    shutil.which("sftp-server"),
)

requires_sftp_server = pytest.mark.skipif(
    SFTP_SERVER is None, reason="OpenSSH sftp-server not installed"
)


class _DelayLine:
    """Delivers the server's replies ``delay`` seconds late, in order, so a
    request/reply costs one simulated round trip while pipelined requests
    still overlap."""

    def __init__(self, source, delay: float) -> None:
        self._source = source
        self._delay = delay
        read_fd, write_fd = os.pipe()
        self.reader = os.fdopen(read_fd, "rb")
        self._writer = os.fdopen(write_fd, "wb", buffering=0)
        self._queue: list = []
        self._seq = 0
        self._cond = threading.Condition()
        self._eof = False
        threading.Thread(target=self._pump_in, daemon=True).start()
        threading.Thread(target=self._pump_out, daemon=True).start()

    def _pump_in(self) -> None:
        while True:
            data = self._source.read1(65536)
            with self._cond:
                if not data:
                    self._eof = True
                else:
                    self._seq += 1
                    heapq.heappush(self._queue, (time.monotonic() + self._delay, self._seq, data))
                self._cond.notify()
            if not data:
                return

    def _pump_out(self) -> None:
        try:
            while True:
                with self._cond:
                    while not self._queue and not self._eof:
                        self._cond.wait()
                    if not self._queue:
                        break
                    due, _, data = self._queue[0]
                    wait = due - time.monotonic()
                    if wait > 0:
                        self._cond.wait(wait)
                        continue
                    heapq.heappop(self._queue)
                self._writer.write(data)
        except OSError:
            pass
        finally:
            self._writer.close()


def start_client(delay: float = 0.0, *, argv=None):
    """Start ``sftp-server`` and a client on it → ``(client, process, stdout)``."""
    process = subprocess.Popen(
        argv or [SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    stdout = process.stdout if not delay else _DelayLine(process.stdout, delay).reader
    sftp = SFTPClient(process.stdin, stdout, on_close=process.terminate)
    sftp.start()
    return sftp, process, stdout


def stop_client(sftp, process, stdout) -> None:
    sftp.close()
    process.wait(timeout=5)
    process.stdout.close()
    if stdout is not process.stdout:
        stdout.close()


def limited_handles_argv(limit: int = 256):
    """An argv running ``sftp-server`` with at most *limit* open files."""
    return ["/bin/sh", "-c", f'ulimit -n {limit} && exec "{SFTP_SERVER}"']
