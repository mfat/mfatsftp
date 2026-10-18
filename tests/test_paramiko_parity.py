"""paramiko-parity features against the real OpenSSH sftp-server: timeouts,
line I/O and write buffering, chdir, listdir_iter, readv, concurrency caps and
compatibility shims."""

from __future__ import annotations

import gc
import io
import os
import time

import pytest

import mfatsftp
from mfatsftp import protocol as proto
from tests.real_server import requires_sftp_server, start_client, stop_client

pytestmark = requires_sftp_server


def _count_writes(client, monkeypatch):
    sent = []
    original = client._send_write

    def counting(handle, offset, data):
        sent.append(len(data))
        return original(handle, offset, data)

    monkeypatch.setattr(client, "_send_write", counting)
    return sent


# -- timeouts ------------------------------------------------------------------


def test_timeout_raises_and_the_session_survives(tmp_path):
    sftp, process, stdout = start_client(delay=0.5)
    try:
        assert sftp.gettimeout() is None
        sftp.settimeout(0.1)
        started = time.monotonic()
        with pytest.raises(TimeoutError) as exc:
            sftp.stat(str(tmp_path))
        assert time.monotonic() - started < 0.45
        assert isinstance(exc.value, mfatsftp.SFTPTimeoutError)
        assert isinstance(exc.value, mfatsftp.SFTPError)
        assert not sftp._pending  # the timed-out request was forgotten
        # The late reply is discarded; later requests still match up.
        sftp.settimeout(None)
        time.sleep(0.6)
        assert sftp.stat(str(tmp_path)).is_dir()
        assert sftp.listdir(str(tmp_path)) == []
    finally:
        stop_client(sftp, process, stdout)


def test_file_timeout_methods_act_on_the_client(client, tmp_path):
    (tmp_path / "f").write_bytes(b"x")
    with client.open(str(tmp_path / "f")) as fh:
        fh.settimeout(5)
        assert client.gettimeout() == 5.0 and fh.gettimeout() == 5.0
        fh.setblocking(True)
        assert client.gettimeout() is None
        assert fh.read() == b"x"


# -- line I/O and buffering ----------------------------------------------------


def test_line_iteration_readline_readlines(client, tmp_path):
    lines = [f"line {i}\n".encode() for i in range(50_000)]
    (tmp_path / "big.txt").write_bytes(b"".join(lines) + b"no newline at end")
    with client.open(str(tmp_path / "big.txt")) as fh:
        got = list(fh)
    assert got == lines + [b"no newline at end"]

    with client.open(str(tmp_path / "big.txt")) as fh:
        assert fh.readline() == b"line 0\n"
        assert fh.readline(3) == b"lin"
        assert fh.readline() == b"e 1\n"
        assert fh.tell() == len(b"line 0\nline 1\n")
        assert fh.read(7) == b"line 2\n"  # read() continues from the buffer
        fh.seek(0)
        assert fh.readlines(sizehint=10) == [b"line 0\n", b"line 1\n"]
        fh.seek(-17, os.SEEK_END)
        assert fh.readline() == b"no newline at end"
        assert fh.readline() == b""


def test_iteration_costs_a_request_per_chunk_not_per_line(client, tmp_path, monkeypatch):
    (tmp_path / "f").write_bytes(b"x\n" * 200_000)
    reads = []
    original = client.read

    def counting(handle, offset, length):
        reads.append(length)
        return original(handle, offset, length)

    monkeypatch.setattr(client, "read", counting)
    with client.open(str(tmp_path / "f")) as fh:
        assert sum(1 for _ in fh) == 200_000
    assert len(reads) <= 400_000 // client.max_read_length + 2


def test_buffered_writes_send_few_requests(client, tmp_path, monkeypatch):
    sent = _count_writes(client, monkeypatch)
    path = tmp_path / "out.txt"
    with client.open(str(path), "w", bufsize=65536) as fh:
        fh.writelines(f"row {i}\n".encode() for i in range(10_000))
        before_flush = len(sent)
        fh.flush()
    assert path.read_bytes() == b"".join(f"row {i}\n".encode() for i in range(10_000))
    assert before_flush < 10
    assert len(sent) < 10


def test_unbuffered_writes_send_each_write(client, tmp_path, monkeypatch):
    sent = _count_writes(client, monkeypatch)
    with client.open(str(tmp_path / "f"), "w") as fh:
        for _ in range(5):
            fh.write(b"ab")
    assert sent == [2] * 5


def test_line_buffered_writes(client, tmp_path, monkeypatch):
    sent = _count_writes(client, monkeypatch)
    with client.open(str(tmp_path / "f"), "w", bufsize=1) as fh:
        fh.write(b"part ")
        fh.write(b"one\n")
        fh.write(b"tail")
        assert sent == [len(b"part one\n")]
    assert sent == [9, 4]
    assert (tmp_path / "f").read_bytes() == b"part one\ntail"


def test_buffered_data_is_visible_to_reads_and_seeks(client, tmp_path):
    with client.open(str(tmp_path / "f"), "w+", bufsize=4096) as fh:
        fh.write(b"hello world")
        fh.seek(0)
        assert fh.read() == b"hello world"
        fh.seek(6)
        fh.write(b"WORLD")
        assert fh.stat().st_size == 11
        fh.seek(0)
        assert fh.readline() == b"hello WORLD"


def test_pipelined_write_errors_surface_at_close(client, tmp_path):
    (tmp_path / "ro").write_bytes(b"")
    fh = client.open(str(tmp_path / "ro"), "r")
    fh.set_pipelined()
    fh.write(b"x" * 100_000)  # returns without waiting for the (failing) reply
    with pytest.raises(mfatsftp.SFTPError):
        fh.close()
    assert fh.closed
    assert client.stat(str(tmp_path / "ro")).st_size == 0


def test_pipelined_writes_land(client, tmp_path):
    content = os.urandom(1_000_000)
    with client.open(str(tmp_path / "f"), "w") as fh:
        fh.set_pipelined(True)
        for start in range(0, len(content), 4096):
            fh.write(content[start : start + 4096])
    assert (tmp_path / "f").read_bytes() == content


def test_garbage_collected_file_is_flushed_and_closed(client, tmp_path):
    fh = client.open(str(tmp_path / "f"), "w", bufsize=4096)
    fh.write(b"unflushed")
    del fh
    gc.collect()
    # The CLOSE is sent without waiting; any later request runs after it.
    client.stat(str(tmp_path))
    assert (tmp_path / "f").read_bytes() == b"unflushed"


def test_text_wrapper_still_works(client, tmp_path):
    (tmp_path / "t").write_text("ä\nb\n", encoding="utf-8")
    with client.open(str(tmp_path / "t")) as fh:
        assert list(io.TextIOWrapper(io.BufferedReader(fh), encoding="utf-8")) == ["ä\n", "b\n"]


# -- chdir / getcwd --------------------------------------------------------------


def test_chdir_resolves_relative_paths(client, tmp_path):
    assert client.getcwd() is None
    client.chdir(str(tmp_path))
    assert client.getcwd() == client.realpath(str(tmp_path))
    with client.open("a.txt", "w") as fh:
        fh.write(b"A")
    client.mkdir("sub")
    client.chdir("sub")
    assert client.getcwd().endswith("/sub")
    client.put(tmp_path / "a.txt", "copy.txt")
    assert (tmp_path / "sub" / "copy.txt").read_bytes() == b"A"
    assert client.listdir() == ["copy.txt"]
    assert client.realpath(".") == client.getcwd()
    client.rename("copy.txt", "../moved.txt")
    assert client.stat("../moved.txt").st_size == 1
    client.symlink("../a.txt", "link")
    assert client.readlink("link") == "../a.txt"
    assert (tmp_path / "sub" / "link").read_bytes() == b"A"
    client.chdir("..")
    assert sorted(client.listdir()) == ["a.txt", "moved.txt", "sub"]
    client.rmtree("sub")
    assert not (tmp_path / "sub").exists()
    with pytest.raises(NotADirectoryError):
        client.chdir("a.txt")
    client.chdir(None)
    assert client.getcwd() is None
    with pytest.raises(FileNotFoundError):
        client.stat("moved.txt")  # relative to the login directory again


def test_chdir_applies_to_batch_and_tree_operations(client, tmp_path):
    client.chdir(str(tmp_path))
    local = tmp_path.parent / (tmp_path.name + "-local")
    (local / "d").mkdir(parents=True)
    (local / "d" / "f").write_bytes(b"data")
    client.put_dir(local, "up")
    assert (tmp_path / "up" / "d" / "f").read_bytes() == b"data"
    assert [a.st_size for a in client.stat_many(["up/d/f"])] == [4]
    assert [[a.filename for a in entries] for entries in client.listdir_many(["up/d"])] == [["f"]]
    client.get_dir("up", tmp_path / "back")
    assert (tmp_path / "back" / "d" / "f").read_bytes() == b"data"
    assert client.mkdir_many(["m1", "m2"]) == [None, None]
    assert client.rmdir_many(["m1", "m2"]) == []
    assert not (tmp_path / "m1").exists()


# -- listdir_iter ------------------------------------------------------------------


def test_listdir_iter_streams_and_can_stop_early(client, tmp_path):
    for index in range(1000):
        (tmp_path / f"f{index:04d}").touch()
    it = client.listdir_iter(str(tmp_path), read_aheads=4)
    first = [next(it) for _ in range(5)]
    assert all(a.filename.startswith("f") for a in first)
    it.close()  # closes the remote directory handle
    names = sorted(a.filename for a in client.listdir_iter(str(tmp_path)))
    assert names == [f"f{index:04d}" for index in range(1000)]
    with pytest.raises(FileNotFoundError):
        list(client.listdir_iter(str(tmp_path / "missing")))


# -- readv ---------------------------------------------------------------------------


def test_readv_returns_ranges_in_order(client, tmp_path):
    content = os.urandom(3 * client.max_read_length + 123)
    (tmp_path / "f").write_bytes(content)
    size = len(content)
    chunks = [(size - 10, 50), (0, 5), (100, client.max_read_length * 2), (size + 5, 10), (7, 0)]
    with client.open(str(tmp_path / "f")) as fh:
        fh.read(3)
        blocks = fh.readv(chunks)
        assert fh.tell() == 3
    assert blocks == [content[o : o + n] for o, n in chunks]


# -- concurrency caps --------------------------------------------------------------


def test_max_concurrent_requests_caps_transfers_and_batches(client, tmp_path):
    content = os.urandom(2 * 1024 * 1024)
    (tmp_path / "f").write_bytes(content)
    client.max_concurrent_requests = 3
    client.max_read_length = client.max_write_length = 32768

    # Requests the server has not answered yet are exactly those still in
    # ``_pending`` (reads sent past EOF are answered but never waited for).
    peak = 0
    send = client._send

    def counting_send(*args):
        nonlocal peak
        slot = send(*args)
        peak = max(peak, len(client._pending))
        return slot

    client._send = counting_send
    try:
        assert client.get(str(tmp_path / "f"), tmp_path / "down") == len(content)
        client.put(tmp_path / "down", str(tmp_path / "up"))
        paths = []
        for index in range(20):
            (tmp_path / f"r{index}").touch()
            paths.append(str(tmp_path / f"r{index}"))
        client.stat_many(paths)
        client.remove_many(paths)
    finally:
        client._send = send
    assert (tmp_path / "up").read_bytes() == content
    assert peak <= 3, f"{peak} requests in flight"
    assert client.last_transfer_peak_pending <= 3


def test_get_accepts_paramiko_prefetch_arguments(client, tmp_path):
    (tmp_path / "f").write_bytes(b"abc" * 100_000)
    assert client.get(str(tmp_path / "f"), tmp_path / "a", prefetch=False) == 300_000
    out = io.BytesIO()
    client.getfo(str(tmp_path / "f"), out, max_concurrent_prefetch_requests=2)
    assert out.getvalue() == b"abc" * 100_000
    with client.open(str(tmp_path / "f")) as fh:
        fh.prefetch()
        fh.prefetch(300_000, 4)
        assert fh.read(3) == b"abc"


# -- attributes ----------------------------------------------------------------------


def test_file_chown_and_attributes_from_stat(client, tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"12345")
    info = path.stat()
    with client.open(str(path), "r+") as fh:
        fh.chown(info.st_uid, info.st_gid)
    attr = mfatsftp.SFTPAttributes.from_stat(info, filename="f")
    assert (attr.st_size, attr.st_mode, attr.filename) == (5, info.st_mode, "f")
    assert attr.attr == {}
    remote = client.stat(str(path))
    assert (remote.st_size, remote.st_mode, remote.st_mtime) == (
        attr.st_size,
        attr.st_mode,
        attr.st_mtime,
    )
    assert proto.SFTPAttributes(attr={"x@example.com": b"1"}).attr == {"x@example.com": b"1"}
