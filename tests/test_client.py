"""Client tests: drive SFTPClient against the in-memory fake server."""

import errno

import pytest

from mfatsftp import protocol as proto
from tests.fake_server import connect_fake


def test_realpath_and_listdir():
    client, server = connect_fake()
    assert client.realpath(".") == "/home/alice"
    names = sorted(a.filename for a in client.listdir_attr("/home/alice"))
    assert names == ["notes.txt", "sub"]
    assert sorted(client.listdir("/home/alice")) == ["notes.txt", "sub"]
    attrs = {a.filename: a for a in client.listdir_attr("/home/alice")}
    assert attrs["sub"].is_dir() is True
    assert attrs["notes.txt"].is_dir() is False
    client.close()


def test_read_write_roundtrip():
    client, server = connect_fake()
    handle = client.open_handle("/home/alice/notes.txt", proto.FXF_READ)
    assert client.read(handle, 0, 100) == b"hello world"
    assert client.read(handle, 11, 100) == b""  # EOF
    client.close_handle(handle)
    wh = client.open_handle(
        "/home/alice/new.txt", proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC
    )
    client.write(wh, 0, b"abc")
    client.close_handle(wh)
    assert bytes(server.fs["/home/alice/new.txt"][1]) == b"abc"
    client.close()


def test_open_file_shim_copy():
    client, server = connect_fake()
    with client.open("/home/alice/notes.txt", "rb") as src, client.open(
        "/home/alice/copy.txt", "wb"
    ) as dst:
        while True:
            chunk = src.read(32768)
            if not chunk:
                break
            dst.write(chunk)
    assert bytes(server.fs["/home/alice/copy.txt"][1]) == b"hello world"
    with client.open("/home/alice/notes.txt", "rb") as f:
        assert f.read() == b"hello world"
    client.close()


def test_mkdir_remove_rename():
    client, server = connect_fake()
    client.mkdir("/home/alice/d")
    assert server.fs["/home/alice/d"][0] == "dir"
    client.rename("/home/alice/notes.txt", "/home/alice/renamed.txt")
    assert "/home/alice/renamed.txt" in server.fs
    client.remove("/home/alice/renamed.txt")
    assert "/home/alice/renamed.txt" not in server.fs
    client.close()


def test_paramiko_surface_parity():
    client, server = connect_fake()
    assert client.normalize(".") == "/home/alice"
    client.mkdir("/home/alice/.ssh", 0o700)
    assert server.fs["/home/alice/.ssh"][0] == "dir"
    client.chmod("/home/alice/.ssh", 0o700)
    with client.file("/home/alice/.ssh/authorized_keys.tmp", "w") as fh:
        fh.write(b"ssh-ed25519 AAAA user@host\n")
    server.fs["/home/alice/.ssh/authorized_keys"] = ("file", bytearray(b"old"))
    client.posix_rename(
        "/home/alice/.ssh/authorized_keys.tmp",
        "/home/alice/.ssh/authorized_keys",
    )
    assert bytes(server.fs["/home/alice/.ssh/authorized_keys"][1]) == b"ssh-ed25519 AAAA user@host\n"
    assert "/home/alice/.ssh/authorized_keys.tmp" not in server.fs
    client.close()


def test_stat_missing_raises_enoent():
    client, server = connect_fake()
    with pytest.raises(proto.SFTPError) as exc:
        client.stat("/home/alice/missing")
    assert exc.value.errno == errno.ENOENT
    client.close()


def test_get_put_roundtrip(tmp_path):
    client, server = connect_fake()

    # put: local -> remote (spans multiple 32 KiB chunks)
    local = tmp_path / "up.bin"
    local.write_bytes(b"x" * 70000)
    seen = []
    moved = client.put(local, "/home/alice/up.bin", callback=lambda d, t: seen.append((d, t)))
    assert moved == 70000
    assert bytes(server.fs["/home/alice/up.bin"][1]) == b"x" * 70000
    assert seen[-1] == (70000, 70000)  # final progress reports completion

    # get: remote -> local
    dest = tmp_path / "down.bin"
    got = client.get("/home/alice/up.bin", dest)
    assert got == 70000
    assert dest.read_bytes() == b"x" * 70000
    client.close()


def test_context_manager_closes():
    client, server = connect_fake()
    with client as c:
        assert c.realpath(".") == "/home/alice"
    assert client._closed is True
