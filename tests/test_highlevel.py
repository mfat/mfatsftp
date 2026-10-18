"""The convenience API (transfers, trees, file objects, errors) against the real
OpenSSH sftp-server."""

from __future__ import annotations

import io
import os
import stat
import time

import pytest

import mfatsftp
from mfatsftp import protocol as proto
from tests.real_server import (
    measures_round_trips,
    requires_sftp_server,
    start_client,
    stop_client,
)

pytestmark = requires_sftp_server


def _make_tree(root, *, large_size):
    (root / "a" / "b").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "top.txt").write_bytes(b"top")
    (root / "a" / "mid.bin").write_bytes(os.urandom(5000))
    (root / "a" / "b" / "large.bin").write_bytes(os.urandom(large_size))
    (root / "a" / "b" / "zero").write_bytes(b"")
    os.chmod(root / "top.txt", 0o640)
    os.utime(root / "top.txt", (1_000_000, 1_500_000))


def _snapshot(root):
    result = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        result[rel] = "dir"
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                result[os.path.join(rel, name)] = fh.read()
    return result


# -- single-file transfers ---------------------------------------------------


def test_put_get_roundtrip_reports_progress(client, tmp_path):
    content = os.urandom(3 * 1024 * 1024 + 17)
    local = tmp_path / "up.bin"
    local.write_bytes(content)
    remote = str(tmp_path / "remote.bin")
    seen = []

    assert client.put(local, remote, callback=lambda d, t: seen.append((d, t))) == len(content)
    assert (tmp_path / "remote.bin").read_bytes() == content
    assert seen[-1] == (len(content), len(content))
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)

    seen.clear()
    dest = tmp_path / "down.bin"
    assert client.get(remote, dest, callback=lambda d, t: seen.append((d, t))) == len(content)
    assert dest.read_bytes() == content
    assert seen[-1] == (len(content), len(content))


def test_putfo_getfo_with_file_objects(client, tmp_path):
    remote = str(tmp_path / "obj.bin")
    content = os.urandom(700_000)
    assert client.putfo(io.BytesIO(content), remote, confirm=True) == len(content)
    out = io.BytesIO()
    assert client.getfo(remote, out) == len(content)
    assert out.getvalue() == content


def test_get_missing_file_raises_file_not_found(client, tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        client.get(str(tmp_path / "missing"), tmp_path / "x")
    assert isinstance(exc.value, mfatsftp.SFTPError)
    assert exc.value.code == proto.FX_NO_SUCH_FILE


def test_copy_with_and_without_copy_data(client, tmp_path):
    content = os.urandom(600_000)
    (tmp_path / "src").write_bytes(content)
    client.copy(str(tmp_path / "src"), str(tmp_path / "dst1"))
    assert (tmp_path / "dst1").read_bytes() == content
    client.extensions.pop("copy-data", None)
    client.copy(str(tmp_path / "src"), str(tmp_path / "dst2"))
    assert (tmp_path / "dst2").read_bytes() == content


# -- directory trees ---------------------------------------------------------


def test_put_dir_then_get_dir_roundtrip(client, tmp_path):
    local = tmp_path / "local"
    _make_tree(local, large_size=client.max_write_length * 3 + 5)
    (local / "link-to-top").symlink_to(local / "top.txt")
    (local / "link-to-dir").symlink_to(local / "a", target_is_directory=True)
    remote = tmp_path / "remote" / "nested"
    seen = []

    total = client.put_dir(local, str(remote), callback=lambda d, t: seen.append((d, t)))

    # A symlinked directory is not followed (and _snapshot skips it too); a
    # symlinked file is uploaded as a regular file.
    expected = _snapshot(local)
    assert not (remote / "link-to-dir").exists()
    uploaded = _snapshot(remote)
    assert uploaded == expected
    assert not (remote / "link-to-top").is_symlink()
    assert seen[-1] == (total, total)
    assert stat.S_IMODE((remote / "top.txt").stat().st_mode) == 0o640
    assert int((remote / "top.txt").stat().st_mtime) == 1_500_000
    # No temporaries left behind.
    assert not [n for n in os.listdir(remote) if n.endswith(".part")]

    back = tmp_path / "back"
    seen.clear()
    got = client.get_dir(str(remote), back, callback=lambda d, t: seen.append((d, t)))
    assert got == total
    assert _snapshot(back) == uploaded
    assert seen[-1] == (got, got)


def test_put_dir_replaces_existing_files(client, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    (local / "f").write_bytes(b"new")
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "f").write_bytes(b"old content")
    client.put_dir(local, str(remote))
    assert (remote / "f").read_bytes() == b"new"


def test_put_dir_refuses_a_file_where_a_directory_goes(client, tmp_path):
    local = tmp_path / "local"
    (local / "sub").mkdir(parents=True)
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "sub").write_bytes(b"in the way")
    with pytest.raises(mfatsftp.SFTPError):
        client.put_dir(local, str(remote))


def test_get_dir_skips_directory_and_dangling_symlinks(client, tmp_path):
    remote = tmp_path / "remote"
    (remote / "d").mkdir(parents=True)
    (remote / "d" / "f").write_bytes(b"x")
    (remote / "dir-link").symlink_to(remote / "d", target_is_directory=True)
    (remote / "dangling").symlink_to(remote / "nowhere")
    (remote / "file-link").symlink_to(remote / "d" / "f")
    local = tmp_path / "local"
    client.get_dir(str(remote), local)
    assert sorted(os.listdir(local)) == ["d", "file-link"]
    assert (local / "file-link").read_bytes() == b"x"


def test_get_dir_refuses_unsafe_names(client, tmp_path, monkeypatch):
    evil = proto.SFTPAttributes(st_mode=0o100644, st_size=1, filename="../escape")
    monkeypatch.setattr(client, "listdir_many", lambda paths: [[evil] for _ in paths])
    with pytest.raises(mfatsftp.SFTPError, match="unsafe"):
        client.get_dir(str(tmp_path), tmp_path / "local")
    assert not (tmp_path / "escape").exists()


def test_rmtree_removes_everything_but_does_not_follow_symlinks(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    tree = tmp_path / "tree"
    _make_tree(tree, large_size=10)
    (tree / "a" / "link").symlink_to(outside, target_is_directory=True)
    for index in range(150):  # more than one pipeline window of files
        (tree / "empty" / f"f{index}").write_bytes(b"")

    client.rmtree(str(tree))

    assert not tree.exists()
    assert (outside / "keep").read_bytes() == b"keep"


def test_rmtree_on_a_file_or_symlink(client, tmp_path):
    (tmp_path / "f").write_bytes(b"x")
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    client.rmtree(str(tmp_path / "f"))
    client.rmtree(str(tmp_path / "link"))
    assert sorted(os.listdir(tmp_path)) == []
    with pytest.raises(FileNotFoundError):
        client.rmtree(str(tmp_path / "missing"))


@measures_round_trips
def test_rmtree_over_a_slow_link_costs_round_trips_per_level(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(40):
        child = root / f"d{index}"
        child.mkdir()
        (child / "f").write_bytes(b"x")
        (child / "sub").mkdir()
    delay = 0.05
    sftp, process, stdout = start_client(delay)
    try:
        started = time.monotonic()
        sftp.rmtree(str(root))
        round_trips = (time.monotonic() - started) / delay
    finally:
        stop_client(sftp, process, stdout)
    assert not root.exists()
    # 81 directories walked one at a time cost ~6 round trips each.
    assert round_trips < 40, f"rmtree took {round_trips:.1f} round trips"


def test_walk_matches_os_walk(client, tmp_path):
    _make_tree(tmp_path, large_size=10)
    (tmp_path / "dir-link").symlink_to(tmp_path / "a", target_is_directory=True)

    def _norm(walk):
        return sorted((top, sorted(dirs), sorted(files)) for top, dirs, files in walk)

    expected = []
    for top, dirs, files in os.walk(tmp_path):
        # os.walk lists a directory symlink among dirnames; ours among files.
        links = [d for d in dirs if os.path.islink(os.path.join(top, d))]
        expected.append(
            (top, [d for d in dirs if d not in links], files + links)
        )
    assert _norm(client.walk(str(tmp_path))) == _norm(expected)


def test_makedirs(client, tmp_path):
    target = tmp_path / "x" / "y" / "z"
    client.makedirs(str(target))
    assert target.is_dir()
    client.makedirs(str(target))  # existing is fine
    client.makedirs(str(target) + "/")  # trailing slash too
    (tmp_path / "file").write_bytes(b"")
    with pytest.raises(mfatsftp.SFTPError):
        client.makedirs(str(tmp_path / "file" / "sub"))


# -- file objects ------------------------------------------------------------


def test_open_modes_follow_python_semantics(client, tmp_path):
    path = tmp_path / "f"
    with pytest.raises(FileNotFoundError):
        client.open(str(path), "r+")
    with client.open(str(path), "w") as fh:
        assert fh.write(b"hello") == 5
        assert fh.writable() and not fh.readable()
    with pytest.raises(mfatsftp.SFTPError):
        client.open(str(path), "x")
    with client.open(str(path), "a") as fh:
        assert fh.tell() == 5
        fh.write(b" world")
    assert path.read_bytes() == b"hello world"
    with client.open(str(path), "r+b") as fh:
        fh.seek(6)
        fh.write(b"WORLD")
        fh.seek(0)
        assert fh.read() == b"hello WORLD"
    with client.open(str(path), "w+") as fh:
        assert fh.read() == b""  # truncated
    with pytest.raises(ValueError):
        client.open(str(path), "rw")


def test_file_seek_tell_truncate_stat(client, tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"0123456789")
    with client.open(str(path), "r+") as fh:
        assert fh.seek(-3, os.SEEK_END) == 7
        assert fh.read() == b"789"
        assert fh.seek(-4, os.SEEK_CUR) == 6
        assert fh.read(2) == b"67"
        assert fh.read(-1) == b"89"
        assert fh.truncate(4) == 4
        assert fh.stat().st_size == 4
        fh.chmod(0o600)
    assert path.read_bytes() == b"0123"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert fh.closed
    with pytest.raises(ValueError):
        fh.read()


def test_file_wraps_in_standard_text_io(client, tmp_path):
    (tmp_path / "t.txt").write_text("one\ntwo\nthree\n")
    with client.open(str(tmp_path / "t.txt")) as fh:
        lines = list(io.TextIOWrapper(io.BufferedReader(fh), encoding="utf-8"))
    assert lines == ["one\n", "two\n", "three\n"]


# -- metadata ----------------------------------------------------------------


def test_chmod_chown_utime_truncate(client, tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"abcdef")
    client.chmod(str(path), 0o604)
    assert stat.S_IMODE(path.stat().st_mode) == 0o604
    info = path.stat()
    client.chown(str(path), info.st_uid, info.st_gid)  # no-op but accepted
    client.utime(str(path), (100, 200))
    assert int(path.stat().st_mtime) == 200
    before = time.time()
    client.utime(str(path))
    assert path.stat().st_mtime >= int(before) - 1
    client.truncate(str(path), 2)
    assert path.read_bytes() == b"ab"
    client.truncate(str(path), 0)
    assert path.read_bytes() == b""
    attrs = client.stat(str(path))
    assert attrs.is_file() and not attrs.is_dir() and not attrs.is_symlink()


def test_statvfs(client, tmp_path):
    assert client.supports_statvfs()
    vfs = client.statvfs(str(tmp_path))
    expected = os.statvfs(tmp_path)
    assert vfs.f_frsize == expected.f_frsize
    assert vfs.f_blocks == expected.f_blocks


def test_users_groups_by_id(client):
    if not client.supports_users_groups_by_id():
        pytest.skip("sftp-server predates users-groups-by-id@openssh.com")
    import grp
    import pwd

    uid, gid = os.getuid(), os.getgid()
    users, groups = client.users_groups_by_id([uid], [gid])
    assert users == [pwd.getpwuid(uid).pw_name]
    assert groups == [grp.getgrgid(gid).gr_name]


def test_symlink_and_readlink_on_openssh(client, tmp_path):
    (tmp_path / "target").write_bytes(b"t")
    client.symlink(str(tmp_path / "target"), str(tmp_path / "link"))
    assert os.readlink(tmp_path / "link") == str(tmp_path / "target")
    assert client.readlink(str(tmp_path / "link")) == str(tmp_path / "target")
    assert client.lstat(str(tmp_path / "link")).is_symlink()
    assert client.stat(str(tmp_path / "link")).is_file()


# -- names and errors --------------------------------------------------------


def test_names_that_are_not_utf8_round_trip(client, tmp_path):
    raw = b"caf\xe9.txt"  # Latin-1, not valid UTF-8
    try:
        with open(os.path.join(os.fsencode(tmp_path), raw), "wb") as fh:
            fh.write(b"data")
    except OSError as exc:  # macOS (APFS) only accepts UTF-8 names
        pytest.skip(f"local filesystem rejects non-UTF-8 names: {exc}")
    (name,) = client.listdir(str(tmp_path))
    assert os.fsencode(name) == raw
    remote = str(tmp_path) + "/" + name
    assert client.stat(remote).st_size == 4
    assert client.open(remote).read() == b"data"
    client.rename(remote, remote + ".bak")
    client.remove(remote + ".bak")
    assert os.listdir(tmp_path) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_permission_denied_is_a_permission_error(client, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        with pytest.raises(PermissionError) as exc:
            client.listdir(str(locked))
        assert exc.value.code == proto.FX_PERMISSION_DENIED
    finally:
        os.chmod(locked, 0o700)


def test_requests_after_close_raise_connection_error(tmp_path):
    sftp, process, stdout = start_client()
    stop_client(sftp, process, stdout)
    assert sftp.closed
    with pytest.raises(ConnectionError) as exc:
        sftp.stat(str(tmp_path))
    assert isinstance(exc.value, mfatsftp.SFTPConnectionError)
