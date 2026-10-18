"""Round-trip tests for the pure SFTP v3 codec (no I/O)."""

from mfatsftp import protocol as p


def test_string_and_int_packing_roundtrip():
    assert p.pack_uint32(0x01020304) == b"\x01\x02\x03\x04"
    assert p.pack_uint64(1) == b"\x00\x00\x00\x00\x00\x00\x00\x01"
    assert p.pack_string("hi") == b"\x00\x00\x00\x02hi"
    assert p.pack_string(b"\x00\xff") == b"\x00\x00\x00\x02\x00\xff"


def test_attrs_roundtrip_full():
    attr = p.SFTPAttributes(
        st_size=123456789012,
        st_uid=1000,
        st_gid=1000,
        st_mode=0o040755,
        st_atime=111,
        st_mtime=222,
    )
    encoded = p.encode_attrs(attr)
    reader = p._Reader(encoded)
    decoded = p.decode_attrs(reader)
    assert decoded.st_size == attr.st_size
    assert decoded.st_uid == 1000 and decoded.st_gid == 1000
    assert decoded.st_mode == 0o040755
    assert decoded.st_mtime == 222
    assert decoded.is_dir() is True
    assert reader.remaining() == 0


def test_attrs_empty():
    encoded = p.encode_attrs(None)
    assert encoded == p.pack_uint32(0)
    decoded = p.decode_attrs(p._Reader(encoded))
    assert decoded.st_size == 0
    assert decoded.is_dir() is False


def test_build_and_parse_init_version():
    init = p.build_init()
    # length prefix (uint32) + type byte + version uint32
    assert init[4] == p.FXP_INIT
    version, ext = p.parse_version(init[5:])
    assert version == p.PROTOCOL_VERSION
    assert ext == {}


def test_build_request_frames_request_id():
    pkt = p.build_request(p.FXP_STAT, 7, p.pack_string("/etc"))
    length = int.from_bytes(pkt[:4], "big")
    assert length == len(pkt) - 4
    assert pkt[4] == p.FXP_STAT
    assert p.response_request_id(pkt[4], pkt[5:]) == 7


def test_parse_status():
    payload = p.pack_uint32(9) + p.pack_uint32(p.FX_NO_SUCH_FILE) + p.pack_string("nope")
    rid, code, msg = p.parse_status(payload)
    assert rid == 9 and code == p.FX_NO_SUCH_FILE and msg == "nope"


def test_parse_name_entries():
    a1 = p.SFTPAttributes(st_size=10, st_mode=0o100644)
    a2 = p.SFTPAttributes(st_size=0, st_mode=0o040755)
    payload = (
        p.pack_uint32(5)  # request id
        + p.pack_uint32(2)  # count
        + p.pack_string("file") + p.pack_string("-rw-r--r-- file") + p.encode_attrs(a1)
        + p.pack_string("dir") + p.pack_string("drwxr-xr-x dir") + p.encode_attrs(a2)
    )
    rid, entries = p.parse_name(payload)
    assert rid == 5
    assert [e.filename for e in entries] == ["file", "dir"]
    assert entries[0].is_dir() is False
    assert entries[1].is_dir() is True


def test_parse_handle_and_data():
    rid, handle = p.parse_handle(p.pack_uint32(3) + p.pack_string(b"H"))
    assert rid == 3 and handle == b"H"
    rid, data = p.parse_data(p.pack_uint32(4) + p.pack_string(b"payload"))
    assert rid == 4 and data == b"payload"


def test_sftp_error_sets_errno():
    import errno

    err = p.SFTPError(p.FX_NO_SUCH_FILE)
    assert err.errno == errno.ENOENT
    assert p.SFTPError(p.FX_PERMISSION_DENIED).errno == errno.EACCES


def test_sftp_error_subclasses_match_builtin_exceptions():
    import errno
    import pickle

    missing = p.SFTPError(p.FX_NO_SUCH_FILE, "gone")
    assert isinstance(missing, FileNotFoundError)
    assert isinstance(missing, p.SFTPNoSuchFileError)
    assert str(missing) == "gone"
    assert isinstance(p.SFTPError(p.FX_PERMISSION_DENIED), PermissionError)
    lost = p.SFTPError(p.FX_CONNECTION_LOST)
    assert isinstance(lost, ConnectionError) and lost.errno == errno.EPIPE
    generic = p.SFTPError(p.FX_FAILURE)
    assert type(generic) is p.SFTPError
    assert str(generic) == "Failure" and generic.errno == errno.EIO
    assert p.SFTPError(p.FX_OP_UNSUPPORTED).errno == errno.EOPNOTSUPP
    copy = pickle.loads(pickle.dumps(missing))
    assert type(copy) is p.SFTPNoSuchFileError
    assert (copy.code, str(copy)) == (p.FX_NO_SUCH_FILE, "gone")


def test_non_utf8_names_round_trip():
    raw = b"caf\xe9"
    name = p._Reader(p.pack_string(raw)).text()
    assert name == "caf\udce9"
    assert p.pack_string(name) == p.pack_string(raw)


def test_parse_statvfs():
    values = list(range(1, 12))
    payload = p.pack_uint32(1) + b"".join(p.pack_uint64(v) for v in values)
    vfs = p.parse_statvfs(payload)
    assert vfs.f_bsize == 1 and vfs.f_namemax == 11
    assert list(vfs) == values


def test_id_names_round_trip():
    names = p.pack_string(p.pack_string("root") + p.pack_string(""))
    groups = p.pack_string(p.pack_string("wheel") + p.pack_string("staff"))
    users, grps = p.parse_id_names(p.pack_uint32(1) + names + groups)
    assert users == ["root", ""]
    assert grps == ["wheel", "staff"]
    assert p.pack_id_list([0, 1]) == p.pack_string(p.pack_uint32(0) + p.pack_uint32(1))


def test_attributes_helpers_and_str():
    f = p.SFTPAttributes(st_mode=0o100644, st_size=12, filename="a.txt")
    assert f.is_file() and not f.is_dir() and not f.is_symlink()
    assert str(f) == "-rw-r--r--         12 a.txt"
    link = p.SFTPAttributes(st_mode=0o120777, longname="lrwxrwxrwx 1 u g 4 x -> y")
    assert link.is_symlink() and not link.is_file()
    assert str(link) == link.longname
