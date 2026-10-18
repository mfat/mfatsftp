"""End-to-end through a real ``ssh`` login (opt-in).

Set ``MFATSFTP_TEST_HOST`` to a host you can reach non-interactively (key or
agent auth), e.g. ``MFATSFTP_TEST_HOST=localhost``. The test works in a fresh
temporary directory under the remote login directory and removes it.
"""

from __future__ import annotations

import os
import uuid

import pytest

import mfatsftp

HOST = os.environ.get("MFATSFTP_TEST_HOST")

pytestmark = pytest.mark.skipif(not HOST, reason="set MFATSFTP_TEST_HOST to run")


def test_connect_and_transfer(tmp_path):
    local = tmp_path / "tree"
    (local / "sub").mkdir(parents=True)
    (local / "small.txt").write_bytes(b"hello")
    (local / "sub" / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))

    with mfatsftp.connect(HOST, batch_mode=True, timeout=30) as sftp:
        home = sftp.realpath(".")
        remote = f"{home}/.mfatsftp-test-{uuid.uuid4().hex[:8]}"
        try:
            sftp.put_dir(local, remote)
            assert sorted(sftp.listdir(remote)) == ["small.txt", "sub"]
            back = tmp_path / "back"
            sftp.get_dir(remote, back)
            assert (back / "small.txt").read_bytes() == b"hello"
            assert (back / "sub" / "big.bin").read_bytes() == (local / "sub" / "big.bin").read_bytes()
        finally:
            sftp.rmtree(remote)
        with pytest.raises(FileNotFoundError):
            sftp.stat(remote)
    assert sftp.closed


def test_bad_host_fails_cleanly():
    with pytest.raises(OSError):
        mfatsftp.connect("mfatsftp-no-such-host.invalid", batch_mode=True, timeout=30)
