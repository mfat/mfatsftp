"""Pure-Python SFTP v3 wire-protocol codec (no I/O, no third-party deps).

This implements just the SFTP protocol layer (draft-ietf-secsh-filexfer-02,
the version OpenSSH speaks as v3, plus the OpenSSH extensions described in
OpenSSH's ``PROTOCOL`` file). It is transport-agnostic: callers feed/drain
bytes (typically the stdin/stdout pipes of an ``ssh -s host sftp`` subprocess).

Everything here is pure and unit-testable: packet build/parse and attribute
encode/decode. The live client (threads, pipelining) lives in ``client.py``;
the subprocess transport in ``transport.py``.

Strings: SFTP v3 filenames are raw bytes. They are decoded as UTF-8 with
``surrogateescape``, so a name that is not valid UTF-8 survives a round trip
(the same convention as :func:`os.fsdecode` on POSIX).
"""

from __future__ import annotations

import dataclasses
import errno as _errno
import stat as _stat
import struct
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union, cast

PROTOCOL_VERSION = 3

# -- packet types -----------------------------------------------------------
FXP_INIT = 1
FXP_VERSION = 2
FXP_OPEN = 3
FXP_CLOSE = 4
FXP_READ = 5
FXP_WRITE = 6
FXP_LSTAT = 7
FXP_FSTAT = 8
FXP_SETSTAT = 9
FXP_FSETSTAT = 10
FXP_OPENDIR = 11
FXP_READDIR = 12
FXP_REMOVE = 13
FXP_MKDIR = 14
FXP_RMDIR = 15
FXP_REALPATH = 16
FXP_STAT = 17
FXP_RENAME = 18
FXP_READLINK = 19
FXP_SYMLINK = 20
FXP_STATUS = 101
FXP_HANDLE = 102
FXP_DATA = 103
FXP_NAME = 104
FXP_ATTRS = 105
FXP_EXTENDED = 200
FXP_EXTENDED_REPLY = 201

# -- status codes -----------------------------------------------------------
FX_OK = 0
FX_EOF = 1
FX_NO_SUCH_FILE = 2
FX_PERMISSION_DENIED = 3
FX_FAILURE = 4
FX_BAD_MESSAGE = 5
FX_NO_CONNECTION = 6
FX_CONNECTION_LOST = 7
FX_OP_UNSUPPORTED = 8

_FX_NAMES = {
    FX_OK: "OK",
    FX_EOF: "EOF",
    FX_NO_SUCH_FILE: "No such file",
    FX_PERMISSION_DENIED: "Permission denied",
    FX_FAILURE: "Failure",
    FX_BAD_MESSAGE: "Bad message",
    FX_NO_CONNECTION: "No connection",
    FX_CONNECTION_LOST: "Connection lost",
    FX_OP_UNSUPPORTED: "Operation unsupported",
}

# -- open flags (pflags) ----------------------------------------------------
FXF_READ = 0x01
FXF_WRITE = 0x02
FXF_APPEND = 0x04
FXF_CREAT = 0x08
FXF_TRUNC = 0x10
FXF_EXCL = 0x20

# -- attribute flags --------------------------------------------------------
ATTR_SIZE = 0x01
ATTR_UIDGID = 0x02
ATTR_PERMISSIONS = 0x04
ATTR_ACMODTIME = 0x08
ATTR_EXTENDED = 0x80000000

S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFLNK = 0o120000


_FX_ERRNO = {
    FX_NO_SUCH_FILE: _errno.ENOENT,
    FX_PERMISSION_DENIED: _errno.EACCES,
    FX_NO_CONNECTION: _errno.EPIPE,
    FX_CONNECTION_LOST: _errno.EPIPE,
    FX_OP_UNSUPPORTED: _errno.EOPNOTSUPP,
}


class SFTPError(IOError):
    """An SFTP STATUS response with a non-OK code (or a lost session).

    ``code`` is the SFTP status (``FX_*``) and ``errno`` the closest POSIX
    errno. Constructing one picks a subclass that also inherits the matching
    built-in exception, so ordinary Python handlers work::

        try:
            sftp.stat(path)
        except FileNotFoundError:   # FX_NO_SUCH_FILE
            ...
        except PermissionError:     # FX_PERMISSION_DENIED
            ...

    Every such error is still an ``SFTPError`` (and an ``OSError``).
    """

    code: int
    message: str

    def __new__(cls, code: int, message: str = "") -> "SFTPError":
        target = _ERROR_CLASSES.get(code, cls) if cls is SFTPError else cls
        return super().__new__(target, _status_text(code, message))

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(_status_text(code, message))
        self.code = code
        self.message = message
        self.errno = _FX_ERRNO.get(code, _errno.EIO)

    def __reduce__(self) -> Tuple[Any, ...]:
        return (SFTPError, (self.code, self.message))


def _status_text(code: int, message: str) -> str:
    return message or _FX_NAMES.get(code, f"SFTP error {code}")


class SFTPNoSuchFileError(SFTPError, FileNotFoundError):
    """``FX_NO_SUCH_FILE``: the path does not exist."""


class SFTPPermissionError(SFTPError, PermissionError):
    """``FX_PERMISSION_DENIED``: the server refused access."""


class SFTPConnectionError(SFTPError, ConnectionError):
    """``FX_NO_CONNECTION`` / ``FX_CONNECTION_LOST``: the session is gone."""


class SFTPTimeoutError(SFTPError, TimeoutError):
    """No reply arrived within the client's timeout (see
    ``SFTPClient.settimeout``). ``code`` is ``FX_FAILURE``."""

    def __init__(self, code: int = FX_FAILURE, message: str = "") -> None:
        super().__init__(code, message or "SFTP request timed out")
        self.errno = _errno.ETIMEDOUT

    def __new__(cls, code: int = FX_FAILURE, message: str = "") -> "SFTPTimeoutError":
        return cast(
            "SFTPTimeoutError", super().__new__(cls, code, message or "SFTP request timed out")
        )

    def __reduce__(self) -> Tuple[Any, ...]:
        return (SFTPTimeoutError, (self.code, self.message))


_ERROR_CLASSES = {
    FX_NO_SUCH_FILE: SFTPNoSuchFileError,
    FX_PERMISSION_DENIED: SFTPPermissionError,
    FX_NO_CONNECTION: SFTPConnectionError,
    FX_CONNECTION_LOST: SFTPConnectionError,
}


@dataclasses.dataclass
class SFTPAttributes:
    """Decoded file attributes, as returned by ``stat``/``listdir_attr``.

    Field names mirror :func:`os.stat_result` and paramiko's
    ``SFTPAttributes`` (``st_size``/``st_mode``/``st_mtime``/…), so the
    :mod:`stat` helpers work on ``st_mode`` directly. Fields the server did not
    send are ``None`` (``st_size``/``st_mode`` default to ``0``). Directory
    entries also carry ``filename`` and the server's ``ls -l`` style
    ``longname``. ``attr`` holds any extended attributes (name → raw value)
    the server sent; it is sent back by ``setstat`` when non-empty.
    """

    st_size: int = 0
    st_uid: Optional[int] = None
    st_gid: Optional[int] = None
    st_mode: int = 0
    st_atime: Optional[int] = None
    st_mtime: Optional[int] = None
    filename: Optional[str] = None  # set for FXP_NAME entries
    longname: Optional[str] = None
    attr: Dict[str, bytes] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_stat(cls, obj: Any, filename: Optional[str] = None) -> "SFTPAttributes":
        """Build attributes from an :func:`os.stat` result (paramiko-compatible)."""
        return cls(
            st_size=int(obj.st_size),
            st_uid=int(obj.st_uid),
            st_gid=int(obj.st_gid),
            st_mode=int(obj.st_mode),
            st_atime=int(obj.st_atime),
            st_mtime=int(obj.st_mtime),
            filename=filename,
        )

    def is_dir(self) -> bool:
        """True for a directory (a symlink from ``lstat`` is not one)."""
        return (self.st_mode & S_IFMT) == S_IFDIR

    def is_file(self) -> bool:
        """True for a regular file."""
        return _stat.S_ISREG(self.st_mode)

    def is_symlink(self) -> bool:
        """True for a symlink (only ``lstat`` and listings report these)."""
        return (self.st_mode & S_IFMT) == S_IFLNK

    def __str__(self) -> str:
        return self.longname or (
            f"{_stat.filemode(self.st_mode)} {self.st_size:>10} {self.filename or ''}"
        ).rstrip()


# -- low level field helpers ------------------------------------------------


def pack_uint32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def pack_uint64(value: int) -> bytes:
    return struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF)


def pack_string(value: Union[str, bytes]) -> bytes:
    if isinstance(value, str):
        # surrogateescape restores the raw bytes of a name that was not valid
        # UTF-8 when the server sent it (see ``_Reader.text``).
        value = value.encode("utf-8", "surrogateescape")
    return pack_uint32(len(value)) + value


class _Reader:
    """Cursor over a bytes payload."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def uint32(self) -> int:
        v: int = struct.unpack_from(">I", self._data, self._pos)[0]
        self._pos += 4
        return v

    def uint64(self) -> int:
        v: int = struct.unpack_from(">Q", self._data, self._pos)[0]
        self._pos += 8
        return v

    def string(self) -> bytes:
        length = self.uint32()
        v = self._data[self._pos : self._pos + length]
        self._pos += length
        return v

    def text(self) -> str:
        # surrogateescape, not replace: a filename's bytes need not be valid
        # UTF-8, and replacing them names a file that does not exist. The
        # lone surrogates round-trip through ``pack_string`` and
        # ``os.fsencode``.
        return self.string().decode("utf-8", "surrogateescape")

    def remaining(self) -> int:
        return len(self._data) - self._pos


def encode_attrs(attr: Optional[SFTPAttributes]) -> bytes:
    """Encode attributes for an outgoing request. ``None`` → empty (flags=0)."""

    if attr is None:
        return pack_uint32(0)
    flags = 0
    body = b""
    if attr.st_size is not None and attr.st_size:
        flags |= ATTR_SIZE
        body += pack_uint64(attr.st_size)
    if attr.st_uid is not None and attr.st_gid is not None:
        flags |= ATTR_UIDGID
        body += pack_uint32(attr.st_uid) + pack_uint32(attr.st_gid)
    if attr.st_mode:
        flags |= ATTR_PERMISSIONS
        body += pack_uint32(attr.st_mode)
    if attr.st_atime is not None and attr.st_mtime is not None:
        flags |= ATTR_ACMODTIME
        body += pack_uint32(int(attr.st_atime)) + pack_uint32(int(attr.st_mtime))
    if attr.attr:
        flags |= ATTR_EXTENDED
        body += pack_uint32(len(attr.attr))
        for name, value in attr.attr.items():
            body += pack_string(name) + pack_string(value)
    return pack_uint32(flags) + body


def decode_attrs(reader: _Reader) -> SFTPAttributes:
    """Decode an ATTRS structure from *reader* (positioned at the flags word)."""

    attr = SFTPAttributes()
    flags = reader.uint32()
    if flags & ATTR_SIZE:
        attr.st_size = reader.uint64()
    if flags & ATTR_UIDGID:
        attr.st_uid = reader.uint32()
        attr.st_gid = reader.uint32()
    if flags & ATTR_PERMISSIONS:
        attr.st_mode = reader.uint32()
    if flags & ATTR_ACMODTIME:
        attr.st_atime = reader.uint32()
        attr.st_mtime = reader.uint32()
    if flags & ATTR_EXTENDED:
        count = reader.uint32()
        for _ in range(count):
            name = reader.text()
            attr.attr[name] = reader.string()
    return attr


# -- packet framing ---------------------------------------------------------


def build_packet(ptype: int, payload: bytes) -> bytes:
    """Frame a packet: uint32 length (type+payload), byte type, payload."""

    return pack_uint32(len(payload) + 1) + bytes([ptype]) + payload


def build_init() -> bytes:
    return build_packet(FXP_INIT, pack_uint32(PROTOCOL_VERSION))


def build_request(ptype: int, request_id: int, payload: bytes) -> bytes:
    return build_packet(ptype, pack_uint32(request_id) + payload)


def parse_version(payload: bytes) -> Tuple[int, Dict[str, bytes]]:
    """Parse an FXP_VERSION payload → (version, extensions)."""

    reader = _Reader(payload)
    version = reader.uint32()
    extensions: Dict[str, bytes] = {}
    while reader.remaining() >= 4:
        name = reader.text()
        value = reader.string()
        extensions[name] = value
    return version, extensions


def parse_status(payload: bytes) -> Tuple[int, int, str]:
    """Parse a STATUS payload (after the type byte) → (request_id, code, msg)."""

    reader = _Reader(payload)
    request_id = reader.uint32()
    code = reader.uint32()
    message = ""
    if reader.remaining() >= 4:
        message = reader.text()
    return request_id, code, message


def parse_name(payload: bytes) -> Tuple[int, List[SFTPAttributes]]:
    """Parse a NAME payload → (request_id, [SFTPAttributes with filename])."""

    reader = _Reader(payload)
    request_id = reader.uint32()
    count = reader.uint32()
    entries: List[SFTPAttributes] = []
    for _ in range(count):
        filename = reader.text()
        longname = reader.text()
        attr = decode_attrs(reader)
        attr.filename = filename
        attr.longname = longname
        entries.append(attr)
    return request_id, entries


def parse_handle(payload: bytes) -> Tuple[int, bytes]:
    reader = _Reader(payload)
    request_id = reader.uint32()
    return request_id, reader.string()


def parse_data(payload: bytes) -> Tuple[int, bytes]:
    reader = _Reader(payload)
    request_id = reader.uint32()
    return request_id, reader.string()


class SFTPStatVFS(NamedTuple):
    """A ``statvfs@openssh.com`` reply, shaped like :func:`os.statvfs`.

    Block counts are in ``f_frsize`` units, so free space for unprivileged
    users is ``f_bavail * f_frsize`` bytes.
    """

    f_bsize: int
    f_frsize: int
    f_blocks: int
    f_bfree: int
    f_bavail: int
    f_files: int
    f_ffree: int
    f_favail: int
    f_fsid: int
    f_flag: int
    f_namemax: int


def parse_statvfs(payload: bytes) -> SFTPStatVFS:
    """Parse a ``statvfs@openssh.com`` EXTENDED_REPLY payload."""
    reader = _Reader(payload)
    reader.uint32()  # request id
    return SFTPStatVFS(*(reader.uint64() for _ in SFTPStatVFS._fields))


def pack_id_list(ids: List[int]) -> bytes:
    """A ``users-groups-by-id@openssh.com`` id list: uint32s inside one string."""
    return pack_string(b"".join(pack_uint32(i) for i in ids))


def _name_list(blob: bytes) -> List[str]:
    reader = _Reader(blob)
    names: List[str] = []
    while reader._pos < len(blob):
        names.append(reader.text())
    return names


def parse_id_names(payload: bytes) -> Tuple[List[str], List[str]]:
    """Parse a ``users-groups-by-id@openssh.com`` EXTENDED_REPLY payload.

    Names come back in request order; an id the server cannot name is ``""``.
    """
    reader = _Reader(payload)
    reader.uint32()  # request id
    users = _name_list(reader.string())
    groups = _name_list(reader.string())
    return users, groups


def parse_attrs(payload: bytes) -> Tuple[int, SFTPAttributes]:
    reader = _Reader(payload)
    request_id = reader.uint32()
    return request_id, decode_attrs(reader)


def response_request_id(ptype: int, payload: bytes) -> int:
    """Every post-INIT response begins with the request id."""

    request_id: int = struct.unpack_from(">I", payload, 0)[0]
    return request_id
