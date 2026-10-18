"""Synchronous SFTP v3 client over a pair of byte streams.

:class:`SFTPClient` speaks the SFTP protocol (see :mod:`mfatsftp.protocol`) over
any readable/writable byte-stream pair — typically the stdin/stdout pipes of an
``ssh -s host sftp`` subprocess created by :mod:`mfatsftp.transport`. It is
transport-agnostic: hand it pipes (or a socketpair, for tests) and it works.

The everyday surface mirrors paramiko's ``SFTPClient`` (``open``/``file``,
``stat``/``lstat``, ``listdir``/``listdir_attr``, ``mkdir``, ``rmdir``,
``remove``/``unlink``, ``rename``/``posix_rename``, ``chmod``/``chown``/
``utime``/``truncate``, ``symlink``/``readlink``, ``realpath``/``normalize``,
``get``/``put``/``getfo``/``putfo``) so paramiko-oriented code can switch with
minimal changes.

Performance: one background thread matches replies to requests by id, so many
requests can be in flight at once. Reads and writes keep a window of requests
outstanding that grows from the measured round-trip time, chunk sizes come from
the server's ``limits@openssh.com``, and the ``*_many`` methods batch metadata
operations so that work on many paths costs a few round trips instead of one
per path.

Thread safety: a client may be shared between threads; each call is
independent. A single :class:`SFTPFile` must not be used from two threads at
once.
"""

from __future__ import annotations

import logging
import os
import stat as _stat
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import (
    IO,
    Callable,
    Deque,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

from . import protocol as proto

logger = logging.getLogger(__name__)

PathLike = Union[str, "os.PathLike[str]"]
#: ``callback(bytes_done, bytes_total)``; *total* is 0 when unknown.
ProgressCallback = Callable[[int, int], None]

_CHUNK = 32768  # 32 KiB — within the SFTP max packet for reads/writes.
# Upper bound on a read/write chunk, whatever limits@openssh.com advertises.
_MAX_CHUNK = 4 * 1024 * 1024
# A short READ at least this long is taken as the server's real cap (one
# shorter is more likely the end of the file).
_MIN_SHORT_READ_SPLIT = _CHUNK
# Starting READ/WRITE window. FileZilla uses the same initial depth, then grows
# it from measured RTT so a high-latency link still keeps the pipe full.
_INITIAL_PIPELINE_DEPTH = 16
# Target outstanding data as a multiple of one RTT (FileZilla's 500 ms aim).
_TARGET_WINDOW_MS = 500
# Hard ceiling on request count (FileZilla uses 16384); we also bound by bytes.
_MAX_PENDING_REQUESTS = 16 * 1024
# Cap bytes in flight so adaptive growth cannot pin tens of MiB of buffers.
_MAX_BYTES_IN_FLIGHT = 32 * 1024 * 1024
# Mass ``FXP_REMOVE`` window. FileZilla uses 100 for the same reason: deletes
# are tiny requests, so a deeper window hides RTT better than the transfer
# depth without saturating the channel the way large READ/WRITE payloads would.
_REMOVE_PIPELINE_DEPTH = 100
# Same depth for mass STAT / atomic small-file upload control-plane ops: each
# request is tiny, so overlapping ~100 of them collapses N×RTT to ~1 RTT per
# phase for a recursive directory of small files.
_META_PIPELINE_DEPTH = 100
# READDIRs a directory listing keeps in flight.
_READDIR_AHEAD = 8


@dataclass(frozen=True)
class AtomicUploadItem:
    """One temp→rename upload for :meth:`SFTPClient.atomic_upload_many`.

    The content of ``local_path`` is written to ``remote_temp`` and then
    renamed onto ``remote_dst``, so readers never see a half-written file.
    ``create_mode`` is applied on OPEN (the server's umask still applies).
    ``existing_mode``, when not ``None``, is set explicitly afterwards (use it
    to keep the permissions of a file being replaced). ``atime``/``mtime`` are
    POSIX timestamps set on the uploaded file.
    """

    local_path: str
    remote_temp: str
    remote_dst: str
    create_mode: int
    existing_mode: Optional[int]
    atime: int
    mtime: int


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[Tuple[int, bytes]] = None


class _AdaptivePipeline:
    """FileZilla-style outstanding-request window grown from measured RTT.

    When the window is full, a probe marks the end of the in-flight range. Once
    replies catch up to that marker, the elapsed time estimates one RTT and the
    window grows or shrinks toward ``_TARGET_WINDOW_MS`` of outstanding work.
    """

    __slots__ = (
        "max_pending",
        "peak_pending",
        "_chunk_size",
        "_hard_cap",
        "_probe_started",
        "_probe_marker",
    )

    def __init__(self, chunk_size: int) -> None:
        self._chunk_size = max(1, int(chunk_size))
        by_bytes = max(
            _INITIAL_PIPELINE_DEPTH,
            _MAX_BYTES_IN_FLIGHT // self._chunk_size,
        )
        self._hard_cap = min(_MAX_PENDING_REQUESTS, by_bytes)
        self.max_pending = _INITIAL_PIPELINE_DEPTH
        self.peak_pending = _INITIAL_PIPELINE_DEPTH
        self._probe_started: Optional[float] = None
        self._probe_marker: Optional[int] = None

    def note_full(self, marker: int) -> None:
        """Start an RTT probe once the window is saturated."""
        if self._probe_started is not None:
            return
        self._probe_started = time.monotonic()
        self._probe_marker = marker

    def on_catch_up(self, marker: int) -> None:
        """Adjust depth when replies reach the probe marker."""
        if self._probe_started is None or marker != self._probe_marker:
            return
        elapsed_ms = max(1, int((time.monotonic() - self._probe_started) * 1000))
        self._probe_started = None
        self._probe_marker = None
        ideal = self.max_pending * _TARGET_WINDOW_MS // elapsed_ms
        if ideal > self.max_pending:
            divisor = 2 if ideal > self.max_pending * 2 else 8
            self.max_pending += max(1, self.max_pending // divisor)
            if self.max_pending > self._hard_cap:
                self.max_pending = self._hard_cap
        elif ideal < self.max_pending:
            divisor = 2 if ideal * 2 < self.max_pending else 8
            self.max_pending -= max(1, self.max_pending // divisor)
            if self.max_pending < _INITIAL_PIPELINE_DEPTH:
                self.max_pending = _INITIAL_PIPELINE_DEPTH
        if self.max_pending > self.peak_pending:
            self.peak_pending = self.max_pending


class SFTPClient:
    """Synchronous SFTP v3 client over a pair of byte streams.

    Usually obtained from :func:`mfatsftp.connect` or
    :func:`mfatsftp.from_command`, which own the ssh subprocess. To drive a
    custom transport, use :meth:`from_pipes`.

    *stdin* is the writable stream to the server and *stdout* the readable
    stream from it (named from the subprocess's point of view). *on_close*, if
    given, is called by :meth:`close` to tear the transport down so the read
    side reaches EOF.

    Use it as a context manager to close it reliably::

        with mfatsftp.connect("host") as sftp:
            sftp.put("report.pdf", "/srv/reports/report.pdf")

    Attributes available after the handshake:

    ``version``
        The protocol version the server speaks (3 for OpenSSH).
    ``extensions``
        Extension name → data advertised by the server, e.g.
        ``"posix-rename@openssh.com"``. Use the ``supports_*`` methods rather
        than testing names directly.
    ``max_read_length`` / ``max_write_length``
        Largest READ/WRITE payload used for transfers (from
        ``limits@openssh.com``, else 32 KiB).
    ``max_open_handles``
        The server's open-handle limit, or 0 if unknown.
    """

    def __init__(self, stdin, stdout, on_close: Optional[Callable[[], None]] = None) -> None:
        self._stdin = stdin
        self._stdout = stdout
        # Optional transport teardown that makes the read side EOF so the reader
        # thread unblocks (e.g. terminate the ssh subprocess, or close the test
        # socketpair). Set by the owner of the transport.
        self._on_close = on_close
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, _Pending] = {}
        self._reader: Optional[threading.Thread] = None
        self._closed = False
        self.version: Optional[int] = None
        self.extensions: Dict[str, bytes] = {}
        # Largest READ/WRITE payload; raised from limits@openssh.com in start().
        self.max_read_length = _CHUNK
        self.max_write_length = _CHUNK
        # Server's open-handle limit from limits@openssh.com (0 = unknown).
        self.max_open_handles = 0
        # Peak outstanding depth from the most recent transfer pipeline (tests /
        # diagnostics). Updated by ``iter_read`` and ``PipelinedWriter``.
        self.last_transfer_peak_pending = _INITIAL_PIPELINE_DEPTH

    @classmethod
    def from_pipes(
        cls,
        stdin,
        stdout,
        on_close: Optional[Callable[[], None]] = None,
        *,
        start: bool = True,
    ) -> "SFTPClient":
        """Build a client over an existing stream pair and (by default) run the
        INIT/VERSION handshake. Use this to drive a custom transport."""
        client = cls(stdin, stdout, on_close=on_close)
        if start:
            client.start()
        return client

    @property
    def closed(self) -> bool:
        """True once the client was closed or the session was lost."""
        return self._closed

    # -- framing ----------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            buf = self._stdout.read(remaining)
            if not buf:
                raise EOFError("SFTP stream closed")
            chunks.append(buf)
            remaining -= len(buf)
        return b"".join(chunks)

    def _read_packet(self) -> Tuple[int, bytes]:
        length = int.from_bytes(self._read_exact(4), "big")
        if length == 0:
            raise EOFError("SFTP zero-length packet")
        body = self._read_exact(length)
        return body[0], body[1:]

    def _write_packet(self, data: bytes) -> None:
        with self._write_lock:
            self._stdin.write(data)
            self._stdin.flush()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """Perform the INIT/VERSION handshake, then start the reader thread."""
        self._write_packet(proto.build_init())
        ptype, payload = self._read_packet()
        if ptype != proto.FXP_VERSION:
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected SFTP VERSION")
        self.version, self.extensions = proto.parse_version(payload)
        self._reader = threading.Thread(
            target=self._reader_loop, name="sftp-reader", daemon=True
        )
        self._reader.start()
        self._load_limits()

    def _load_limits(self) -> None:
        """Adopt the server's READ/WRITE size limits (OpenSSH extension).

        OpenSSH advertises ~255 KiB, so a 1 MiB file moves in 4-5 chunks
        instead of 32. Servers without the extension keep the 32 KiB default.
        """
        if "limits@openssh.com" not in self.extensions:
            return
        try:
            ptype, payload = self._request(
                proto.FXP_EXTENDED, proto.pack_string("limits@openssh.com")
            )
            if ptype != proto.FXP_EXTENDED_REPLY:
                return
            reader = proto._Reader(payload)
            reader.uint32()  # request id
            max_packet = reader.uint64()
            max_read = reader.uint64()
            max_write = reader.uint64()
            try:
                max_handles = reader.uint64()
            except Exception:  # older/partial reply: no handle limit
                max_handles = 0
        except Exception as exc:
            logger.debug("SFTP limits request failed: %s", exc)
            return
        # 0 means "no limit"; a WRITE also carries its handle and header, so
        # leave room for them inside the packet limit.
        if max_packet:
            max_write = min(max_write or max_packet, max_packet - 1024)
        if max_read > 0:
            self.max_read_length = min(max_read, _MAX_CHUNK)
        if max_write > 0:
            self.max_write_length = min(max_write, _MAX_CHUNK)
        self.max_open_handles = max_handles

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                ptype, payload = self._read_packet()
                rid = proto.response_request_id(ptype, payload)
                slot = self._pending.pop(rid, None)
                if slot is not None:
                    slot.response = (ptype, payload)
                    slot.event.set()
        except Exception:  # EOF or stream error — fail everything pending.
            pass
        finally:
            self._closed = True
            for slot in list(self._pending.values()):
                slot.event.set()
            self._pending.clear()

    def close(self) -> None:
        """Close the session and its transport. Safe to call more than once;
        requests still waiting for a reply fail with
        :class:`~mfatsftp.SFTPConnectionError`."""
        self._closed = True
        # Tear down the transport first: this EOFs our read side so the reader
        # thread returns from its blocked readinto() instead of us yanking the
        # fd out from under the file objects (which caused EBADF on finalize).
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:  # pragma: no cover - best effort
                pass
        # Close the write side (also signals EOF to the peer).
        try:
            self._stdin.close()
        except Exception:  # pragma: no cover - best effort
            pass
        # Wake any in-flight requests so callers don't hang on a reply that will
        # never come.
        for slot in list(self._pending.values()):
            slot.event.set()
        self._pending.clear()
        # The reader (a daemon) exits once the read side EOFs. We do NOT close
        # ``self._stdout`` here — if the reader were still mid-read,
        # BufferedReader.close() would deadlock on the buffer lock; its owner
        # closes it after the reader has stopped.
        if self._reader is not None:
            self._reader.join(timeout=1.0)

    def __enter__(self) -> "SFTPClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- request/response -------------------------------------------------
    def _request(self, ptype: int, payload: bytes) -> Tuple[int, bytes]:
        return self._wait(self._send(ptype, payload))

    def _send(self, ptype: int, payload: bytes) -> _Pending:
        """Send a request without waiting; pass the slot to ``_wait``."""
        if self._closed:
            raise proto.SFTPError(proto.FX_CONNECTION_LOST, "SFTP session closed")
        with self._id_lock:
            self._next_id = (self._next_id + 1) & 0xFFFFFFFF
            rid = self._next_id
            slot = _Pending()
            self._pending[rid] = slot
        # The reader marks the session closed before it wakes the pending
        # slots, so a slot registered after that sweep is woken here.
        if self._closed:
            slot.event.set()
        self._write_packet(proto.build_request(ptype, rid, payload))
        return slot

    @staticmethod
    def _wait(slot: _Pending) -> Tuple[int, bytes]:
        slot.event.wait()
        if slot.response is None:
            raise proto.SFTPError(proto.FX_CONNECTION_LOST, "SFTP session lost")
        return slot.response

    @staticmethod
    def _expect_ok(resp: Tuple[int, bytes]) -> None:
        ptype, payload = resp
        if ptype != proto.FXP_STATUS:
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected STATUS")
        _, code, message = proto.parse_status(payload)
        if code != proto.FX_OK:
            raise proto.SFTPError(code, message)

    # -- high level operations -------------------------------------------
    def realpath(self, path: str) -> str:
        """The server's canonical absolute form of *path* (``"."`` gives the
        login directory). OpenSSH resolves symlinks and ``..``."""
        resp = self._request(proto.FXP_REALPATH, proto.pack_string(path))
        ptype, payload = resp
        if ptype == proto.FXP_NAME:
            _, entries = proto.parse_name(payload)
            if entries:
                return entries[0].filename or path
        self._expect_ok(resp)  # raises if STATUS error
        return path

    def stat(self, path: str) -> proto.SFTPAttributes:
        """Attributes of *path*, following symlinks."""
        return self._attrs(self._request(proto.FXP_STAT, proto.pack_string(path)))

    def lstat(self, path: str) -> proto.SFTPAttributes:
        """Attributes of *path* itself; a symlink is not followed."""
        return self._attrs(self._request(proto.FXP_LSTAT, proto.pack_string(path)))

    def fstat(self, handle: bytes) -> proto.SFTPAttributes:
        """Attributes of an open handle (see :attr:`SFTPFile.handle`)."""
        return self._attrs(self._request(proto.FXP_FSTAT, proto.pack_string(handle)))

    def stat_many(
        self, paths: List[str], *, missing_on_error: bool = False
    ) -> List[Optional[proto.SFTPAttributes]]:
        """STAT many paths at once; one entry per path, in order.

        Requests are sent in pipelined windows, so 100 paths cost about one
        round trip. Missing paths become ``None`` (same as a conflict probe that treats
        ``FX_NO_SUCH_FILE`` as absent). Other STATUS errors raise after the
        in-flight window is drained, unless ``missing_on_error`` is set: then
        any error except a lost connection also reads as ``None`` (for servers
        that answer a missing path with ``FX_FAILURE`` or a permission error).
        """
        if not paths:
            return []
        results: List[Optional[proto.SFTPAttributes]] = [None] * len(paths)
        inflight: Deque[Tuple[int, _Pending]] = deque()
        fatal: Optional[BaseException] = None

        def _drain_one() -> None:
            nonlocal fatal
            index, slot = inflight.popleft()
            try:
                results[index] = self._attrs(self._wait(slot))
            except proto.SFTPError as exc:
                if exc.code == proto.FX_NO_SUCH_FILE or (
                    missing_on_error and exc.code != proto.FX_CONNECTION_LOST
                ):
                    results[index] = None
                    return
                if fatal is None:
                    fatal = exc
            except Exception as exc:
                if missing_on_error:
                    results[index] = None
                    return
                if fatal is None:
                    fatal = exc

        for index, path in enumerate(paths):
            if fatal is not None:
                break
            inflight.append((index, self._send(proto.FXP_STAT, proto.pack_string(path))))
            if len(inflight) >= _META_PIPELINE_DEPTH:
                _drain_one()
        while inflight:
            _drain_one()
        if fatal is not None:
            raise fatal
        return results

    @staticmethod
    def _attrs(resp: Tuple[int, bytes]) -> proto.SFTPAttributes:
        ptype, payload = resp
        if ptype == proto.FXP_ATTRS:
            _, attr = proto.parse_attrs(payload)
            return attr
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(payload)
            raise proto.SFTPError(code, message)
        raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected ATTRS")

    def listdir_attr(self, path: str = ".") -> List[proto.SFTPAttributes]:
        """Attributes of every entry in directory *path* (``.``/``..`` are
        left out), each with ``filename`` set. Order is the server's.

        Entry attributes describe the entry itself: a symlink is reported as
        a symlink, as with :meth:`lstat`.

        Several READDIRs are kept in flight.

        Each READDIR returns the next batch (OpenSSH: ~100 names) or EOF, and
        a server handles requests on one handle in order, so a large directory
        costs a round trip per ``_READDIR_AHEAD`` batches instead of per batch.
        """
        resp = self._request(proto.FXP_OPENDIR, proto.pack_string(path))
        handle = self._handle(resp)
        entries: List[proto.SFTPAttributes] = []
        request = proto.pack_string(handle)
        inflight: Deque[_Pending] = deque()
        try:
            while True:
                while len(inflight) < _READDIR_AHEAD:
                    inflight.append(self._send(proto.FXP_READDIR, request))
                ptype, payload = self._wait(inflight.popleft())
                if ptype == proto.FXP_NAME:
                    _, names = proto.parse_name(payload)
                    for attr in names:
                        if attr.filename in (".", ".."):
                            continue
                        entries.append(attr)
                elif ptype == proto.FXP_STATUS:
                    _, code, message = proto.parse_status(payload)
                    if code == proto.FX_EOF:
                        break
                    raise proto.SFTPError(code, message)
                else:
                    raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected NAME")
            # The read-aheads past EOF answer EOF; settle them before CLOSE.
            while inflight:
                self._wait(inflight.popleft())
        finally:
            self.close_handle(handle)
        return entries

    def listdir(self, path: str = ".") -> List[str]:
        """Names of the entries in directory *path* (paramiko-compatible)."""
        return [a.filename for a in self.listdir_attr(path) if a.filename]

    def walk(
        self, top: str
    ) -> Iterator[Tuple[str, List[str], List[str]]]:
        """Walk a remote tree top-down, like :func:`os.walk`.

        Yields ``(dirpath, dirnames, filenames)``. Symlinks to directories
        are listed in *filenames* and not followed. As with :func:`os.walk`,
        removing names from *dirnames* in place prunes the walk.
        """
        dirnames: List[str] = []
        filenames: List[str] = []
        for attr in self.listdir_attr(top):
            name = _safe_name(attr.filename)
            if attr.is_dir() and not attr.is_symlink():
                dirnames.append(name)
            else:
                filenames.append(name)
        yield top, dirnames, filenames
        for name in dirnames:
            yield from self.walk(_join_remote(top, name))

    @staticmethod
    def _handle(resp: Tuple[int, bytes]) -> bytes:
        ptype, payload = resp
        if ptype == proto.FXP_HANDLE:
            _, handle = proto.parse_handle(payload)
            return handle
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(payload)
            raise proto.SFTPError(code, message)
        raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected HANDLE")

    def mkdir(self, path: str, mode: Optional[int] = None) -> None:
        """Create directory *path*. *mode* (e.g. ``0o755``) is subject to the
        server's umask; without it the server's default applies."""
        attr = None
        if mode is not None:
            attr = proto.SFTPAttributes(st_mode=int(mode) & 0o7777)
        self._expect_ok(
            self._request(proto.FXP_MKDIR, proto.pack_string(path) + proto.encode_attrs(attr))
        )

    def makedirs(self, path: str, mode: Optional[int] = None) -> None:
        """Create *path* and any missing parents, like ``mkdir -p``.

        Existing directories are fine; an existing non-directory in the way
        raises :class:`~mfatsftp.SFTPError` (as does any other failure).
        """
        try:
            if self.stat(path).is_dir():
                return
        except FileNotFoundError:
            pass
        parts = [part for part in path.split("/") if part]
        current = "/" if path.startswith("/") else ""
        for part in parts:
            current = _join_remote(current, part) if current else part
            try:
                self.mkdir(current, mode)
            except proto.SFTPError as exc:
                if exc.code == proto.FX_CONNECTION_LOST:
                    raise
                # Exists already (or is not ours to create): fine only if it
                # is a directory we can see.
                try:
                    attr = self.stat(current)
                except proto.SFTPError:
                    raise exc from None
                if not attr.is_dir():
                    raise proto.SFTPError(
                        proto.FX_FAILURE, f"Not a directory: {current}"
                    ) from None

    def rmdir(self, path: str) -> None:
        """Remove the empty directory *path*."""
        self._expect_ok(self._request(proto.FXP_RMDIR, proto.pack_string(path)))

    def remove(self, path: str) -> None:
        """Remove the file or symlink *path* (not a directory)."""
        self._expect_ok(self._request(proto.FXP_REMOVE, proto.pack_string(path)))

    def rmtree(self, path: str) -> None:
        """Delete *path* and everything below it, like ``rm -r``.

        Symlinks are removed as links and never followed, so the delete
        cannot escape the tree. The tree is handled a depth level at a time —
        each level is listed and its files removed in pipelined batches — so
        a wide tree costs a few round trips per level, not per directory.
        Directories are removed deepest level first once their files are
        gone. On failure, the error is raised and whatever was not yet
        removed stays in place.

        A *path* that is a file or symlink is simply removed.
        """
        attr = self.lstat(path)
        if not attr.is_dir() or attr.is_symlink():
            self.remove(path)
            return
        levels: List[List[str]] = [[path]]
        level = [path]
        while level:
            files: List[str] = []
            subdirs: List[str] = []
            for directory, entries in zip(level, self.listdir_many(level)):
                for entry in entries:
                    child = _join_remote(directory, _safe_name(entry.filename))
                    if entry.is_dir() and not entry.is_symlink():
                        subdirs.append(child)
                    else:
                        files.append(child)
            self.remove_many(files)
            if subdirs:
                levels.append(subdirs)
            level = subdirs
        for directories in reversed(levels):
            self.rmdir_many(directories)

    def remove_many(
        self, paths: List[str], *, continue_on_error: bool = False
    ) -> List[Tuple[str, BaseException]]:
        """Delete many files/symlinks at once with pipelined requests.

        Up to ``_REMOVE_PIPELINE_DEPTH`` removes stay in flight so a mass delete
        costs about one round trip per window instead of one per path. Missing
        paths are ignored (same idempotent policy as a recursive tree delete).

        When ``continue_on_error`` is false (default), the first hard STATUS
        error is raised after draining the current in-flight window. When true,
        every path is attempted and ``(path, exception)`` failures are returned
        instead of raising. Directories must be removed with ``rmdir`` /
        recursive walk — ``FXP_REMOVE`` on a directory fails.
        """
        return self._path_status_many(proto.FXP_REMOVE, paths, continue_on_error)

    def rmdir_many(
        self, paths: List[str], *, continue_on_error: bool = False
    ) -> List[Tuple[str, BaseException]]:
        """Remove many empty directories at once with pipelined requests.

        Same window, missing-path and error policy as :meth:`remove_many`. The
        caller removes children before parents across calls; directories in
        one call must not contain each other.
        """
        return self._path_status_many(proto.FXP_RMDIR, paths, continue_on_error)

    def _path_status_many(
        self, ptype: int, paths: List[str], continue_on_error: bool
    ) -> List[Tuple[str, BaseException]]:
        if not paths:
            return []
        inflight: Deque[Tuple[str, _Pending]] = deque()
        failures: List[Tuple[str, BaseException]] = []
        fatal: Optional[BaseException] = None

        def _drain_one() -> None:
            nonlocal fatal
            path, slot = inflight.popleft()
            try:
                self._expect_ok(self._wait(slot))
            except proto.SFTPError as exc:
                if exc.code == proto.FX_NO_SUCH_FILE:
                    return
                failures.append((path, exc))
                if not continue_on_error and fatal is None:
                    fatal = exc
            except Exception as exc:
                failures.append((path, exc))
                if not continue_on_error and fatal is None:
                    fatal = exc

        for path in paths:
            if fatal is not None:
                break
            inflight.append((path, self._send(ptype, proto.pack_string(path))))
            if len(inflight) >= _REMOVE_PIPELINE_DEPTH:
                _drain_one()
        while inflight:
            _drain_one()
        if fatal is not None:
            raise fatal
        return failures

    unlink = remove  # paramiko alias

    def rename(self, old: str, new: str) -> None:
        """Rename *old* to *new*. Plain SFTP rename fails if *new* exists;
        use :meth:`posix_rename` or :meth:`atomic_rename` to replace it."""
        self._expect_ok(
            self._request(
                proto.FXP_RENAME, proto.pack_string(old) + proto.pack_string(new)
            )
        )

    def supports_posix_rename(self) -> bool:
        """True if the server advertises ``posix-rename@openssh.com``."""
        return "posix-rename@openssh.com" in self.extensions

    def posix_rename(self, old: str, new: str) -> None:
        """Atomic rename that overwrites the target (OpenSSH extension).

        Regular SFTP RENAME fails if the destination exists; ``posix-rename``
        replaces it atomically, which is what installing a file safely (e.g.
        ``authorized_keys``) needs. Fails on servers without the extension;
        :meth:`atomic_rename` falls back automatically.
        """
        payload = (
            proto.pack_string("posix-rename@openssh.com")
            + proto.pack_string(old)
            + proto.pack_string(new)
        )
        self._expect_ok(self._request(proto.FXP_EXTENDED, payload))

    def atomic_rename(self, old: str, new: str) -> None:
        """Rename ``old`` onto ``new``, replacing ``new`` if it already exists.

        Prefers OpenSSH ``posix-rename`` when the server advertises it. Servers
        that reject the extension (ProFTPD mod_sftp, AWS Transfer Family,
        Dropbear, many appliances) get remove-destination then standard
        ``FXP_RENAME`` instead of a hard failure.
        """
        if self.supports_posix_rename():
            try:
                self.posix_rename(old, new)
                return
            except proto.SFTPError as exc:
                if exc.code == proto.FX_CONNECTION_LOST:
                    raise
                logger.debug(
                    "posix-rename failed (%s); falling back to remove+rename",
                    exc,
                )
        try:
            self.remove(new)
        except (FileNotFoundError, proto.SFTPError):
            pass
        self.rename(old, new)

    def atomic_upload_many(
        self,
        items: List[AtomicUploadItem],
        *,
        on_file_bytes: Optional[Callable[[AtomicUploadItem, int], None]] = None,
        check_cancel: Optional[Callable[[], None]] = None,
    ) -> None:
        """Upload many small files at once with pipelined requests.

        Each item is written to ``remote_temp`` then atomically renamed onto
        ``remote_dst`` (same policy as a single-file transfer). Items run in
        windows of at most :meth:`_meta_window` files; within a window every
        phase (OPEN, WRITE, FSETSTAT, CLOSE, rename) is sent at once, so a
        directory of tiny files costs about one RTT per phase per window
        instead of six RTTs per file, while the server never holds more than
        one window of open handles.

        Callers should pass files that fit in one WRITE chunk (see
        ``max_write_length``); a file that grew since it was measured is still
        written in ``max_write_length`` pieces.

        *on_file_bytes(item, nbytes)* is called as each item's data is
        acknowledged. *check_cancel()* is called between phases; raise from
        it to abort. On failure temps are removed and the error is raised;
        items in earlier windows stay uploaded.
        """
        window = self._meta_window()
        for start in range(0, len(items), window):
            if check_cancel is not None:
                check_cancel()
            self._atomic_upload_window(
                items[start : start + window], on_file_bytes, check_cancel
            )

    def _meta_window(self) -> int:
        # Concurrent transfers share this session, so take only a slice of
        # the server's handle limit when it advertises one.
        if self.max_open_handles:
            return max(1, min(_META_PIPELINE_DEPTH, self.max_open_handles // 4))
        return _META_PIPELINE_DEPTH

    def _send_all(self, requests: List[Tuple[int, bytes]]) -> List[object]:
        """Send *requests* together and wait for every reply.

        Returns one entry per request: the response tuple, or the exception
        raised while sending or waiting. Every sent request is drained, so no
        reply (e.g. a successful OPEN's handle) is ever abandoned.
        """
        slots: List[object] = []
        for ptype, payload in requests:
            try:
                slots.append(self._send(ptype, payload))
            except BaseException as exc:  # connection lost mid-window
                slots.append(exc)
        results: List[object] = []
        for slot in slots:
            if isinstance(slot, BaseException):
                results.append(slot)
                continue
            try:
                results.append(self._wait(slot))
            except BaseException as exc:
                results.append(exc)
        return results

    def _status_results(self, requests: List[Tuple[int, bytes]]) -> List[Optional[BaseException]]:
        """Like :meth:`_send_all` for STATUS replies: ``None`` means OK."""
        errors: List[Optional[BaseException]] = []
        for result in self._send_all(requests):
            if isinstance(result, BaseException):
                errors.append(result)
                continue
            try:
                self._expect_ok(result)
            except BaseException as exc:
                errors.append(exc)
            else:
                errors.append(None)
        return errors

    def _atomic_upload_window(
        self,
        items: List[AtomicUploadItem],
        on_file_bytes: Optional[Callable[[AtomicUploadItem, int], None]],
        check_cancel: Optional[Callable[[], None]],
    ) -> None:
        opened: List[Tuple[AtomicUploadItem, bytes]] = []
        # Temps that exist on the server and are not yet renamed into place.
        pending_temps: List[str] = []
        # Temps whose destination was already removed: the temp is now the
        # only copy, so it must survive a failure.
        keep_temps: List[str] = []

        def _cancel() -> None:
            if check_cancel is not None:
                check_cancel()

        def _first_error(errors: List[Optional[BaseException]]) -> None:
            for exc in errors:
                if exc is not None:
                    raise exc

        try:
            # Phase 1: OPEN temps ------------------------------------------------
            _cancel()
            open_results = self._send_all(
                [
                    (
                        proto.FXP_OPEN,
                        proto.pack_string(item.remote_temp)
                        + proto.pack_uint32(
                            proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC
                        )
                        + proto.encode_attrs(
                            proto.SFTPAttributes(st_mode=int(item.create_mode) & 0o7777)
                        ),
                    )
                    for item in items
                ]
            )
            open_error: Optional[BaseException] = None
            for item, result in zip(items, open_results):
                if isinstance(result, BaseException):
                    open_error = open_error or result
                    continue
                try:
                    handle = self._handle(result)
                except BaseException as exc:
                    open_error = open_error or exc
                    continue
                opened.append((item, handle))
                pending_temps.append(item.remote_temp)
            if open_error is not None:
                raise open_error

            # Phase 2: WRITE payloads --------------------------------------------
            _cancel()
            writes: List[Tuple[int, bytes]] = []
            written: List[Tuple[AtomicUploadItem, int]] = []
            step = max(1, int(self.max_write_length))
            for item, handle in opened:
                offset = 0
                with open(item.local_path, "rb") as source:
                    while True:
                        data = source.read(step)
                        if not data:
                            break
                        writes.append(
                            (
                                proto.FXP_WRITE,
                                proto.pack_string(handle)
                                + proto.pack_uint64(offset)
                                + proto.pack_string(data),
                            )
                        )
                        written.append((item, len(data)))
                        offset += len(data)
                if offset == 0 and on_file_bytes is not None:
                    on_file_bytes(item, 0)
            write_errors = self._status_results(writes)
            for (item, nbytes), exc in zip(written, write_errors):
                if exc is None and on_file_bytes is not None:
                    on_file_bytes(item, nbytes)
            _first_error(write_errors)

            # Phase 3: FSETSTAT (mode + mtime), best effort ----------------------
            _cancel()
            meta_errors = self._status_results(
                [
                    (
                        proto.FXP_FSETSTAT,
                        proto.pack_string(handle)
                        + proto.encode_attrs(
                            proto.SFTPAttributes(
                                st_mode=item.existing_mode,
                                st_atime=item.atime,
                                st_mtime=item.mtime,
                            )
                        ),
                    )
                    for item, handle in opened
                ]
            )
            for exc in meta_errors:
                if exc is None:
                    continue
                if not isinstance(exc, proto.SFTPError) or exc.code == proto.FX_CONNECTION_LOST:
                    raise exc
                logger.debug("Could not set uploaded file attributes: %s", exc)

            # Phase 4: CLOSE -----------------------------------------------------
            close_errors = self._status_results(
                [(proto.FXP_CLOSE, proto.pack_string(handle)) for _item, handle in opened]
            )
            opened.clear()
            _first_error(close_errors)

            # Phase 5: rename onto the destination ------------------------------
            _cancel()
            if self.supports_posix_rename():
                rename_errors = self._status_results(
                    [
                        (
                            proto.FXP_EXTENDED,
                            proto.pack_string("posix-rename@openssh.com")
                            + proto.pack_string(item.remote_temp)
                            + proto.pack_string(item.remote_dst),
                        )
                        for item in items
                    ]
                )
            else:
                # Plain RENAME succeeds for new destinations without touching
                # anything; only existing ones need the remove+rename fallback.
                rename_errors = self._status_results(
                    [
                        (
                            proto.FXP_RENAME,
                            proto.pack_string(item.remote_temp)
                            + proto.pack_string(item.remote_dst),
                        )
                        for item in items
                    ]
                )
            retry: List[AtomicUploadItem] = []
            for item, exc in zip(items, rename_errors):
                if exc is None:
                    pending_temps.remove(item.remote_temp)
                elif isinstance(exc, proto.SFTPError) and exc.code != proto.FX_CONNECTION_LOST:
                    retry.append(item)
                else:
                    raise exc
            # Fallback one file at a time so at most one destination is ever
            # missing while its replacement is still a temp.
            for item in retry:
                logger.debug(
                    "rename of %s failed; falling back to remove+rename", item.remote_temp
                )
                try:
                    self.remove(item.remote_dst)
                    removed = True
                except (FileNotFoundError, proto.SFTPError) as exc:
                    if getattr(exc, "code", None) == proto.FX_CONNECTION_LOST:
                        raise
                    removed = False
                if removed:
                    keep_temps.append(item.remote_temp)
                self.rename(item.remote_temp, item.remote_dst)
                pending_temps.remove(item.remote_temp)
                if removed:
                    keep_temps.remove(item.remote_temp)
        except BaseException:
            if opened:
                try:
                    self._status_results(
                        [(proto.FXP_CLOSE, proto.pack_string(h)) for _i, h in opened]
                    )
                except BaseException:
                    pass
            for path in keep_temps:
                logger.warning(
                    "Upload interrupted after its destination was removed; "
                    "keeping the new content at %s",
                    path,
                )
            doomed = [path for path in pending_temps if path not in keep_temps]
            if doomed:
                try:
                    self.remove_many(doomed, continue_on_error=True)
                except BaseException:
                    pass
            raise

    def _open_all(
        self, requests: List[Tuple[int, bytes]]
    ) -> Tuple[List[Optional[bytes]], Optional[BaseException]]:
        """Send OPEN/OPENDIR *requests* together; return each handle (``None``
        where it failed) and the first error."""
        handles: List[Optional[bytes]] = []
        error: Optional[BaseException] = None
        for result in self._send_all(requests):
            try:
                if isinstance(result, BaseException):
                    raise result
                handles.append(self._handle(result))
            except BaseException as exc:
                handles.append(None)
                error = error or exc
        return handles, error

    def _close_all(self, handles: List[Optional[bytes]]) -> None:
        """Best-effort pipelined CLOSE (like :meth:`close_handle`)."""
        requests = [
            (proto.FXP_CLOSE, proto.pack_string(handle))
            for handle in handles
            if handle is not None
        ]
        try:
            errors = self._status_results(requests)
        except BaseException as exc:  # pragma: no cover - best effort
            errors = [exc]
        for exc in errors:
            if exc is not None:
                logger.debug("SFTP close handle failed: %s", exc)

    def mkdir_many(
        self, paths: List[str], mode: Optional[int] = None
    ) -> List[Optional[BaseException]]:
        """Create many directories at once with pipelined requests.

        Returns one entry per path: ``None`` on success, else the error. The
        caller orders parents before children across calls; paths within one
        call must not depend on each other.
        """
        attr = None
        if mode is not None:
            attr = proto.SFTPAttributes(st_mode=int(mode) & 0o7777)
        errors: List[Optional[BaseException]] = []
        for start in range(0, len(paths), _META_PIPELINE_DEPTH):
            errors.extend(
                self._status_results(
                    [
                        (proto.FXP_MKDIR, proto.pack_string(path) + proto.encode_attrs(attr))
                        for path in paths[start : start + _META_PIPELINE_DEPTH]
                    ]
                )
            )
        return errors

    def listdir_many(self, paths: List[str]) -> List[List[proto.SFTPAttributes]]:
        """List many directories at once (entries as :meth:`listdir_attr`),
        one list per path, in order.

        Directories are opened a window at a time and read in rounds, each
        round sending READDIRs for every unfinished directory together, so a
        level of small directories costs a few round trips in total rather
        than a few per directory. The first error is raised after every reply
        is drained and every handle closed.
        """
        entries: List[List[proto.SFTPAttributes]] = []
        window = self._meta_window()
        for start in range(0, len(paths), window):
            entries.extend(self._listdir_window(paths[start : start + window]))
        return entries

    def _listdir_window(self, paths: List[str]) -> List[List[proto.SFTPAttributes]]:
        handles, error = self._open_all(
            [(proto.FXP_OPENDIR, proto.pack_string(path)) for path in paths]
        )
        entries: List[List[proto.SFTPAttributes]] = [[] for _ in paths]
        try:
            if error is not None:
                raise error
            active = list(range(len(paths)))
            # Two READDIRs finish a typical small directory (names, then EOF)
            # in one round trip; directories still going get the full ahead.
            ahead = 2
            while active:
                sent: List[Tuple[int, object]] = []
                for index in active:
                    request = proto.pack_string(handles[index])
                    for _ in range(ahead):
                        try:
                            sent.append((index, self._send(proto.FXP_READDIR, request)))
                        except BaseException as exc:
                            sent.append((index, exc))
                done = set()
                for index, slot in sent:
                    try:
                        if isinstance(slot, BaseException):
                            raise slot
                        # Always wait, even past EOF: replies on one handle
                        # arrive in order and must settle before CLOSE.
                        ptype, payload = self._wait(slot)
                        if index in done:
                            continue
                        if ptype == proto.FXP_NAME:
                            _, names = proto.parse_name(payload)
                            entries[index].extend(
                                attr for attr in names if attr.filename not in (".", "..")
                            )
                        elif ptype == proto.FXP_STATUS:
                            _, code, message = proto.parse_status(payload)
                            if code != proto.FX_EOF:
                                raise proto.SFTPError(code, message)
                            done.add(index)
                        else:
                            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected NAME")
                    except BaseException as exc:
                        error = error or exc
                        done.add(index)
                if error is not None:
                    raise error
                active = [index for index in active if index not in done]
                ahead = _READDIR_AHEAD
        finally:
            self._close_all(handles)
        return entries

    def small_read_limit(self) -> int:
        """Largest file size :meth:`read_small_files` reads in one READ."""
        return max(0, int(self.max_read_length) - 1)

    def read_small_files(
        self,
        items: List[Tuple[str, int]],
        *,
        check_cancel: Optional[Callable[[], None]] = None,
    ) -> Iterator[Tuple[int, List[bytes]]]:
        """Read many small files with pipelined OPEN / READ / CLOSE.

        *items* are ``(path, expected_size)``. Files are handled a window at a
        time; each window yields ``(start_index, contents)`` once all of its
        handles are closed, so no handle outlives a yield. Each file is read
        with one READ of ``expected_size + 1`` bytes: getting exactly the
        expected size back proves EOF without another round trip, and a file
        that grew or came back short is finished with :meth:`iter_read`.
        """
        window = self._meta_window()
        for start in range(0, len(items), window):
            if check_cancel is not None:
                check_cancel()
            yield start, self._read_window(items[start : start + window], check_cancel)

    def _read_window(
        self,
        items: List[Tuple[str, int]],
        check_cancel: Optional[Callable[[], None]],
    ) -> List[bytes]:
        handles, error = self._open_all(
            [
                (
                    proto.FXP_OPEN,
                    proto.pack_string(path)
                    + proto.pack_uint32(proto.FXF_READ)
                    + proto.encode_attrs(None),
                )
                for path, _size in items
            ]
        )
        contents: List[bytes] = []
        try:
            if error is not None:
                raise error
            if check_cancel is not None:
                check_cancel()
            step = max(1, int(self.max_read_length))
            lengths = [min(max(0, int(size)) + 1, step) for _path, size in items]
            replies = self._send_all(
                [
                    (
                        proto.FXP_READ,
                        proto.pack_string(handle)
                        + proto.pack_uint64(0)
                        + proto.pack_uint32(length),
                    )
                    for handle, length in zip(handles, lengths)
                ]
            )
            for reply in replies:
                try:
                    if isinstance(reply, BaseException):
                        raise reply
                    contents.append(self._read_data(reply))
                except BaseException as exc:
                    error = error or exc
                    contents.append(b"")
            if error is not None:
                raise error
            for index, ((_path, size), length) in enumerate(zip(items, lengths)):
                data = contents[index]
                if len(data) == size and length > size:
                    continue  # asked for one byte more than expected: at EOF
                # Grew, shrank, or a short reply: read the rest to real EOF.
                rest = b"".join(self.iter_read(handles[index], offset=len(data)))
                contents[index] = data + rest
        finally:
            self._close_all(handles)
        return contents

    def supports_hardlink(self) -> bool:
        """True if the server advertises ``hardlink@openssh.com``."""
        return "hardlink@openssh.com" in self.extensions

    def hardlink(self, old: str, new: str) -> None:
        """Create ``new`` as a hard link to ``old`` (OpenSSH extension).

        One round trip, and the link keeps the old inode's content even after
        ``old`` is later replaced by a rename.
        """
        payload = (
            proto.pack_string("hardlink@openssh.com")
            + proto.pack_string(old)
            + proto.pack_string(new)
        )
        self._expect_ok(self._request(proto.FXP_EXTENDED, payload))

    def supports_copy_data(self) -> bool:
        """True if the server advertises ``copy-data`` (OpenSSH 9.0+)."""
        return "copy-data" in self.extensions

    def copy_data(self, source_handle: bytes, destination_handle: bytes) -> None:
        """Copy a whole open file into another on the server (``copy-data``).

        One round trip, and no file data crosses the connection. Handles come
        from :meth:`open` (:attr:`SFTPFile.handle`) or :meth:`open_handle`.
        See :meth:`copy` for the path-based form.
        """
        payload = (
            proto.pack_string("copy-data")
            + proto.pack_string(source_handle)
            + proto.pack_uint64(0)  # read offset
            + proto.pack_uint64(0)  # length: 0 copies to EOF
            + proto.pack_string(destination_handle)
            + proto.pack_uint64(0)  # write offset
        )
        self._expect_ok(self._request(proto.FXP_EXTENDED, payload))

    def copy(self, source: str, destination: str) -> None:
        """Copy file *source* to *destination* on the server.

        Uses ``copy-data`` when the server has it, so no data crosses the
        connection; otherwise the data is read and written back through this
        client. *destination* is created or truncated.
        """
        with self.open(source, "rb") as src, self.open(destination, "wb") as dst:
            if self.supports_copy_data():
                self.copy_data(src.handle, dst.handle)
                return
            writer = self.pipelined_writer(dst.handle)
            for chunk in self.iter_read(src.handle):
                writer.write(chunk)
            writer.flush()

    def supports_statvfs(self) -> bool:
        """True if the server advertises ``statvfs@openssh.com``."""
        return "statvfs@openssh.com" in self.extensions

    def statvfs(self, path: str) -> proto.SFTPStatVFS:
        """Filesystem sizes for *path* (OpenSSH extension), like
        :func:`os.statvfs`."""
        payload = proto.pack_string("statvfs@openssh.com") + proto.pack_string(path)
        ptype, body = self._request(proto.FXP_EXTENDED, payload)
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(body)
            raise proto.SFTPError(code, message)
        if ptype != proto.FXP_EXTENDED_REPLY:
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected EXTENDED_REPLY")
        return proto.parse_statvfs(body)

    def supports_users_groups_by_id(self) -> bool:
        """True if the server advertises ``users-groups-by-id@openssh.com``
        (OpenSSH 9.6+)."""
        return "users-groups-by-id@openssh.com" in self.extensions

    def users_groups_by_id(
        self, uids: List[int], gids: List[int]
    ) -> Tuple[List[str], List[str]]:
        """Server-side user and group names for ids (OpenSSH extension).

        Returns the names in request order, ``""`` where the server has none.
        """
        payload = (
            proto.pack_string("users-groups-by-id@openssh.com")
            + proto.pack_id_list(uids)
            + proto.pack_id_list(gids)
        )
        ptype, body = self._request(proto.FXP_EXTENDED, payload)
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(body)
            raise proto.SFTPError(code, message)
        if ptype != proto.FXP_EXTENDED_REPLY:
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected EXTENDED_REPLY")
        users, groups = proto.parse_id_names(body)
        if len(users) != len(uids) or len(groups) != len(gids):
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "id name count mismatch")
        return users, groups

    def setstat(self, path: str, attr: proto.SFTPAttributes) -> None:
        """Set the attributes given in *attr* on *path*.

        Only the fields that are set are sent: ``st_mode`` when non-zero,
        ``st_uid``+``st_gid`` and ``st_atime``+``st_mtime`` as pairs, and
        ``st_size`` when non-zero (use :meth:`truncate` for size 0).
        """
        self._expect_ok(
            self._request(proto.FXP_SETSTAT, proto.pack_string(path) + proto.encode_attrs(attr))
        )

    def fsetstat(self, handle: bytes, attr: proto.SFTPAttributes) -> None:
        """Set attributes (mode, times, …) on an open handle; see
        :meth:`setstat`."""
        self._expect_ok(
            self._request(proto.FXP_FSETSTAT, proto.pack_string(handle) + proto.encode_attrs(attr))
        )

    def chmod(self, path: str, mode: int) -> None:
        """Change the permission bits of *path* (e.g. ``0o644``)."""
        self.setstat(path, proto.SFTPAttributes(st_mode=int(mode) & 0o7777))

    def chown(self, path: str, uid: int, gid: int) -> None:
        """Change the numeric owner and group of *path*."""
        self.setstat(path, proto.SFTPAttributes(st_uid=int(uid), st_gid=int(gid)))

    def utime(self, path: str, times: Optional[Tuple[float, float]] = None) -> None:
        """Set *path*'s access and modification times.

        *times* is ``(atime, mtime)`` in seconds since the epoch, or ``None``
        for now, as with :func:`os.utime`. SFTP v3 stores whole seconds.
        """
        if times is None:
            now = time.time()
            times = (now, now)
        atime, mtime = times
        self.setstat(path, proto.SFTPAttributes(st_atime=int(atime), st_mtime=int(mtime)))

    def truncate(self, path: str, size: int) -> None:
        """Set *path*'s size to *size* bytes, cutting or zero-extending it."""
        self._expect_ok(
            self._request(proto.FXP_SETSTAT, proto.pack_string(path) + _size_attrs(size))
        )

    def symlink(self, target_path: str, link_path: str) -> None:
        """Create ``link_path`` as a symlink pointing at ``target_path``
        (same argument order as :func:`os.symlink`).

        The SFTP v3 draft specifies ``SSH_FXP_SYMLINK`` as ``linkpath`` then
        ``targetpath``, but OpenSSH's sftp-server famously implemented the
        arguments swapped and kept that behaviour for compatibility (see
        OpenSSH's ``PROTOCOL`` §4.1). Since this client only ever talks to an
        OpenSSH subsystem, send ``targetpath`` first to match the server.
        """
        self._expect_ok(
            self._request(
                proto.FXP_SYMLINK,
                proto.pack_string(target_path) + proto.pack_string(link_path),
            )
        )

    def readlink(self, path: str) -> str:
        """The target of symlink *path*, unresolved."""
        resp = self._request(proto.FXP_READLINK, proto.pack_string(path))
        ptype, payload = resp
        if ptype == proto.FXP_NAME:
            _, entries = proto.parse_name(payload)
            if entries:
                return entries[0].filename or ""
            raise proto.SFTPError(proto.FX_BAD_MESSAGE, "empty READLINK reply")
        self._expect_ok(resp)  # raises if STATUS error
        raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected NAME")

    def normalize(self, path: str) -> str:
        """paramiko alias for :meth:`realpath`."""
        return self.realpath(path)

    def open_handle(
        self, path: str, pflags: int, attr: Optional[proto.SFTPAttributes] = None
    ) -> bytes:
        """Low-level OPEN with ``FXF_*`` *pflags*; returns the raw handle.

        Close it with :meth:`close_handle`. Prefer :meth:`open`.
        """
        payload = proto.pack_string(path) + proto.pack_uint32(pflags) + proto.encode_attrs(attr)
        return self._handle(self._request(proto.FXP_OPEN, payload))

    def open(
        self,
        path: str,
        mode: str = "r",
        bufsize: int = -1,
        *,
        create_mode: Optional[int] = None,
    ) -> "SFTPFile":
        """Open remote *path* and return an :class:`SFTPFile`.

        *mode* follows :func:`open`: ``r``, ``r+``, ``w``, ``w+``, ``a``,
        ``a+``, ``x``, ``x+``. Data is always bytes; a ``b`` (or ``t``) in
        *mode* is accepted and ignored, as in paramiko. *bufsize* is accepted
        for paramiko compatibility and ignored (I/O is unbuffered and
        pipelined).

        ``create_mode`` sets the permissions a newly created file gets in the
        same OPEN request (the server still applies its umask), saving the
        separate SETSTAT round trip.
        """
        normalized = "".join(sorted(mode.replace("b", "").replace("t", "")))
        try:
            pflags = _OPEN_FLAGS[normalized]
        except KeyError:
            raise ValueError(f"invalid mode: {mode!r}") from None
        attr = None
        if create_mode is not None:
            attr = proto.SFTPAttributes(st_mode=int(create_mode) & 0o7777)
        handle = self.open_handle(path, pflags, attr)
        sftp_file = SFTPFile(self, handle, mode)
        if pflags & proto.FXF_APPEND:
            try:
                sftp_file.seek(0, os.SEEK_END)
            except BaseException:
                sftp_file.close()
                raise
        return sftp_file

    # paramiko's SFTPClient exposes both ``open`` and ``file`` (an alias).
    def file(
        self,
        path: str,
        mode: str = "r",
        bufsize: int = -1,
        *,
        create_mode: Optional[int] = None,
    ) -> "SFTPFile":
        """Alias for :meth:`open` (paramiko-compatible)."""
        return self.open(path, mode, bufsize, create_mode=create_mode)

    def read(self, handle: bytes, offset: int, length: int) -> bytes:
        """Low-level single READ: up to *length* bytes at *offset*; ``b""``
        at EOF. The server may return fewer bytes than asked even before
        EOF; :meth:`iter_read` handles that."""
        return self._read_data(self._wait(self._send_read(handle, offset, length)))

    def _send_read(self, handle: bytes, offset: int, length: int) -> _Pending:
        payload = proto.pack_string(handle) + proto.pack_uint64(offset) + proto.pack_uint32(length)
        return self._send(proto.FXP_READ, payload)

    @staticmethod
    def _read_data(resp: Tuple[int, bytes]) -> bytes:
        """DATA reply → bytes; EOF → ``b""``."""
        ptype, body = resp
        if ptype == proto.FXP_DATA:
            _, data = proto.parse_data(body)
            return data
        if ptype == proto.FXP_STATUS:
            _, code, message = proto.parse_status(body)
            if code == proto.FX_EOF:
                return b""
            raise proto.SFTPError(code, message)
        raise proto.SFTPError(proto.FX_BAD_MESSAGE, "expected DATA")

    def write(self, handle: bytes, offset: int, data: bytes) -> None:
        """Low-level single WRITE of *data* at *offset*. *data* must fit in
        ``max_write_length``; use :meth:`pipelined_writer` for more."""
        self._expect_ok(self._wait(self._send_write(handle, offset, data)))

    def _send_write(self, handle: bytes, offset: int, data: bytes) -> _Pending:
        payload = proto.pack_string(handle) + proto.pack_uint64(offset) + proto.pack_string(data)
        return self._send(proto.FXP_WRITE, payload)

    def iter_read(
        self, handle: bytes, offset: int = 0, length: Optional[int] = None
    ) -> Iterator[bytes]:
        """Yield a file's bytes in order from ``offset``, up to ``length`` or EOF.

        Outstanding READs start at ``_INITIAL_PIPELINE_DEPTH`` and grow from
        measured RTT (FileZilla-style) so a high-latency link still fills the
        pipe. A server may return fewer bytes than asked (OpenSSH's sftp-server
        caps a READ at ~255 KiB), so a short reply is followed up for the rest
        of its chunk; only an empty reply means EOF. The first short reply also
        resizes the chunks to its length, in flight and to come.
        """
        step = self.max_read_length
        end = None if length is None else offset + length
        pipeline = _AdaptivePipeline(step)
        inflight: Deque[Tuple[int, int, _Pending]] = deque()
        next_offset = position = offset
        split = False
        try:
            while True:
                while len(inflight) < pipeline.max_pending and (
                    end is None or next_offset < end
                ):
                    size = step if end is None else min(step, end - next_offset)
                    inflight.append(
                        (next_offset, size, self._send_read(handle, next_offset, size))
                    )
                    next_offset += size
                    if len(inflight) >= pipeline.max_pending:
                        pipeline.note_full(next_offset)
                if not inflight:
                    return
                chunk_offset, size, slot = inflight.popleft()
                # A re-split chunk (below) may still be answered in full.
                data = self._read_data(self._wait(slot))[:size]
                chunk_end = chunk_offset + size
                if not split and _MIN_SHORT_READ_SPLIT <= len(data) < size:
                    # The server returns less than its limits advertise
                    # (Win32-OpenSSH caps a READ at 100 KiB), so every chunk
                    # in flight comes back short too. Ask for all their
                    # missing pieces now instead of one round trip per chunk.
                    split = True
                    step = len(data)
                    chunk_end = chunk_offset + step
                    inflight = self._split_reads(
                        handle, [(chunk_end, size - step, None), *inflight], step
                    )
                while data:
                    yield data
                    position += len(data)
                    if position >= chunk_end:
                        break
                    data = self.read(handle, position, chunk_end - position)
                if position < chunk_end:
                    # EOF inside this chunk; replies still in flight are past it.
                    return
                pipeline.on_catch_up(position)
        finally:
            self.last_transfer_peak_pending = pipeline.peak_pending

    def _split_reads(
        self,
        handle: bytes,
        reads: List[Tuple[int, int, Optional[_Pending]]],
        step: int,
    ) -> Deque[Tuple[int, int, _Pending]]:
        """Cut each ``(offset, size, slot)`` READ into ``step``-sized pieces.

        A read already sent keeps its slot for its first piece (its reply is
        trimmed to that); the rest, and a read with no slot, are sent now.
        """
        pieces: Deque[Tuple[int, int, _Pending]] = deque()
        for read_offset, size, slot in reads:
            read_end = read_offset + size
            if slot is not None:
                pieces.append((read_offset, min(size, step), slot))
                read_offset += step
            for piece in range(read_offset, read_end, step):
                piece_size = min(step, read_end - piece)
                pieces.append((piece, piece_size, self._send_read(handle, piece, piece_size)))
        return pieces

    def pipelined_writer(
        self,
        handle: bytes,
        offset: int = 0,
        *,
        on_ack: Optional[Callable[[int], None]] = None,
    ) -> "PipelinedWriter":
        """A :class:`PipelinedWriter` for *handle* starting at *offset*.
        *on_ack(offset)* is called as writes up to *offset* are confirmed."""
        return PipelinedWriter(self, handle, offset, on_ack=on_ack)

    def close_handle(self, handle: bytes) -> None:
        """Close a raw handle. Best effort: errors are logged, not raised."""
        try:
            self._expect_ok(self._request(proto.FXP_CLOSE, proto.pack_string(handle)))
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("SFTP close handle failed: %s", exc)

    # -- file transfer (paramiko-style get/put) --------------------------
    def getfo(
        self,
        remotepath: str,
        fl: IO[bytes],
        callback: Optional[ProgressCallback] = None,
    ) -> int:
        """Download *remotepath* into the open binary file object *fl*.

        Reads are pipelined. *callback*, if given, is called as
        ``callback(bytes_done, total)`` as data arrives (*total* is 0 if the
        remote size is unknown). Returns the number of bytes written.
        """
        handle = self.open_handle(remotepath, proto.FXF_READ)
        done = 0
        try:
            total = 0
            if callback is not None:
                try:
                    total = int(self.fstat(handle).st_size or 0)
                except proto.SFTPError:
                    pass
            for chunk in self.iter_read(handle):
                fl.write(chunk)
                done += len(chunk)
                if callback is not None:
                    callback(done, total)
        finally:
            self.close_handle(handle)
        return done

    def get(
        self,
        remotepath: str,
        localpath: PathLike,
        callback: Optional[ProgressCallback] = None,
    ) -> int:
        """Download *remotepath* to the local file *localpath*.

        The local file is created or truncated. See :meth:`getfo` for
        *callback*. Returns the number of bytes transferred.
        """
        with open(localpath, "wb") as fh:
            return self.getfo(remotepath, fh, callback)

    def putfo(
        self,
        fl: IO[bytes],
        remotepath: str,
        file_size: int = 0,
        callback: Optional[ProgressCallback] = None,
        confirm: bool = False,
    ) -> int:
        """Upload the open binary file object *fl* to *remotepath*.

        The remote file is created or truncated; writes are pipelined.
        *file_size* is the total passed to *callback* (taken from *fl* when it
        is a real file and *file_size* is 0). *callback* is called as
        ``callback(bytes_done, total)`` as the server confirms data. With
        *confirm*, the remote size is checked afterwards and a mismatch
        raises :class:`OSError`. Returns the number of bytes written.
        """
        total = int(file_size or 0)
        if not total and callback is not None:
            try:
                total = os.fstat(fl.fileno()).st_size
            except (AttributeError, OSError, ValueError):
                total = 0
        on_ack = None
        if callback is not None:
            def on_ack(offset: int) -> None:
                callback(offset, total)
        handle = self.open_handle(
            remotepath, proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC
        )
        try:
            writer = self.pipelined_writer(handle, on_ack=on_ack)
            step = max(1, int(self.max_write_length))
            while True:
                chunk = fl.read(step)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
        finally:
            self.close_handle(handle)
        if confirm:
            size = self.stat(remotepath).st_size
            if size != writer.offset:
                raise OSError(
                    f"size mismatch in put: {size} != {writer.offset} ({remotepath})"
                )
        return writer.offset

    def put(
        self,
        localpath: PathLike,
        remotepath: str,
        callback: Optional[ProgressCallback] = None,
        confirm: bool = False,
    ) -> int:
        """Upload the local file *localpath* to *remotepath*.

        See :meth:`putfo` for *callback* and *confirm*. Returns the number of
        bytes transferred.
        """
        with open(localpath, "rb") as fh:
            return self.putfo(
                fh, remotepath, os.fstat(fh.fileno()).st_size, callback, confirm
            )

    def get_dir(
        self,
        remotepath: str,
        localpath: PathLike,
        callback: Optional[ProgressCallback] = None,
    ) -> int:
        """Recursively download the remote directory *remotepath* into
        *localpath* (created if needed); existing local files are replaced.

        Directories are listed a level at a time and small files fetched in
        pipelined batches, so a tree of many small files costs a few round
        trips per level instead of several per file. Symlinks to files are
        downloaded as regular files; symlinks to directories and dangling
        symlinks are skipped (never followed, so loops are impossible).

        Every listed name is checked before it touches the local disk: a
        name containing a path separator, or ``.``/``..``, raises
        :class:`~mfatsftp.SFTPError` instead of writing outside *localpath*.

        *callback(bytes_done, total)* reports progress over the whole tree.
        Returns the number of bytes transferred.
        """
        local_root = os.fspath(localpath)
        os.makedirs(local_root, exist_ok=True)
        files: List[Tuple[str, str, int]] = []
        links: List[Tuple[str, str]] = []
        level: List[Tuple[str, str]] = [(remotepath, local_root)]
        while level:
            next_level: List[Tuple[str, str]] = []
            listings = self.listdir_many([remote for remote, _local in level])
            for (remote_dir, local_dir), entries in zip(level, listings):
                for entry in entries:
                    name = _safe_name(entry.filename)
                    remote = _join_remote(remote_dir, name)
                    local = os.path.join(local_dir, name)
                    if entry.is_symlink():
                        links.append((remote, local))
                    elif entry.is_dir():
                        os.makedirs(local, exist_ok=True)
                        next_level.append((remote, local))
                    else:
                        files.append((remote, local, int(entry.st_size or 0)))
            level = next_level
        if links:
            targets = self.stat_many([remote for remote, _local in links], missing_on_error=True)
            for (remote, local), attr in zip(links, targets):
                if attr is None or attr.is_dir():
                    logger.debug("get_dir: skipping symlink %s", remote)
                    continue
                files.append((remote, local, int(attr.st_size or 0)))

        total = sum(size for _remote, _local, size in files)
        done = 0
        limit = self.small_read_limit()
        small = [item for item in files if item[2] <= limit]
        large = [item for item in files if item[2] > limit]
        for start, contents in self.read_small_files(
            [(remote, size) for remote, _local, size in small]
        ):
            for offset, data in enumerate(contents):
                with open(small[start + offset][1], "wb") as fh:
                    fh.write(data)
                done += len(data)
                if callback is not None:
                    callback(done, total)
        for remote, local, _size in large:
            base = done
            done += self.get(
                remote,
                local,
                callback=(lambda d, _t, base=base: callback(base + d, total))
                if callback is not None
                else None,
            )
        return done

    def put_dir(
        self,
        localpath: PathLike,
        remotepath: str,
        callback: Optional[ProgressCallback] = None,
    ) -> int:
        """Recursively upload the local directory *localpath* to *remotepath*.

        *remotepath* and missing subdirectories are created; existing remote
        files are replaced. Each file keeps its local permission bits and
        modification time. Small files are uploaded in pipelined batches,
        each written to a temporary name and renamed into place (see
        :meth:`atomic_upload_many`); larger files are streamed with
        :meth:`put`. Symlinks to files are uploaded as regular files;
        symlinks to directories are not followed.

        *callback(bytes_done, total)* reports progress over the whole tree.
        Returns the number of bytes transferred.
        """
        local_root = os.fspath(localpath)
        if not os.path.isdir(local_root):
            raise NotADirectoryError(local_root)
        levels: Dict[int, List[str]] = {}
        files: List[Tuple[str, str, os.stat_result]] = []
        for root, _dirnames, names in os.walk(local_root):
            rel = os.path.relpath(root, local_root)
            if rel == ".":
                remote_dir = remotepath
            else:
                remote_dir = _join_remote(remotepath, *rel.split(os.sep))
                levels.setdefault(rel.count(os.sep), []).append(remote_dir)
            for name in sorted(names):
                local = os.path.join(root, name)
                try:
                    info = os.stat(local)
                except OSError:  # dangling symlink
                    logger.debug("put_dir: skipping unreadable %s", local)
                    continue
                if _stat.S_ISREG(info.st_mode):
                    files.append((local, _join_remote(remote_dir, name), info))

        self.makedirs(remotepath)
        for depth in sorted(levels):
            level = levels[depth]
            missing: List[str] = []
            for remote_dir, attr in zip(level, self.stat_many(level)):
                if attr is None:
                    missing.append(remote_dir)
                elif not attr.is_dir():
                    raise proto.SFTPError(proto.FX_FAILURE, f"Not a directory: {remote_dir}")
            for exc in self.mkdir_many(missing):
                if exc is not None:
                    raise exc

        total = sum(info.st_size for _local, _remote, info in files)
        done = 0
        limit = max(1, int(self.max_write_length))
        small = [item for item in files if item[2].st_size <= limit]
        large = [item for item in files if item[2].st_size > limit]
        if small:
            items = [
                AtomicUploadItem(
                    local_path=local,
                    remote_temp=_temp_name(remote),
                    remote_dst=remote,
                    create_mode=info.st_mode & 0o7777,
                    existing_mode=None,
                    atime=int(info.st_atime),
                    mtime=int(info.st_mtime),
                )
                for local, remote, info in small
            ]

            def _on_bytes(_item: AtomicUploadItem, nbytes: int) -> None:
                nonlocal done
                done += nbytes
                if callback is not None:
                    callback(done, total)

            self.atomic_upload_many(items, on_file_bytes=_on_bytes)
        for local, remote, info in large:
            base = done
            done += self.put(
                local,
                remote,
                callback=(lambda d, _t, base=base: callback(base + d, total))
                if callback is not None
                else None,
            )
            self.setstat(
                remote,
                proto.SFTPAttributes(
                    st_mode=info.st_mode & 0o7777,
                    st_atime=int(info.st_atime),
                    st_mtime=int(info.st_mtime),
                ),
            )
        return done


class PipelinedWriter:
    """Writes to an open handle with an RTT-adaptive WRITE window in flight.

    Data is split into chunks the server accepts: OpenSSH's sftp-server drops
    the session on a message over 256 KiB. A failed WRITE raises from a later
    ``write()`` or from ``flush()``, which must be called before the handle is
    closed. Outstanding depth starts at ``_INITIAL_PIPELINE_DEPTH`` and grows
    toward ~500 ms of in-flight requests (same algorithm as downloads).

    ``offset`` is where the next ``write()`` lands; *on_ack(offset)* is called
    as writes up to *offset* are confirmed by the server.
    """

    def __init__(
        self,
        client: "SFTPClient",
        handle: bytes,
        offset: int = 0,
        *,
        on_ack: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._client = client
        self._handle = handle
        self.offset = offset
        self._inflight: Deque[Tuple[int, _Pending]] = deque()
        self._pipeline = _AdaptivePipeline(client.max_write_length)
        self._acked_through = offset
        self._on_ack = on_ack

    @property
    def peak_pending(self) -> int:
        return self._pipeline.peak_pending

    def write(self, data: bytes) -> None:
        client = self._client
        pipeline = self._pipeline
        step = client.max_write_length
        view = memoryview(data)
        for start in range(0, len(view), step):
            chunk = bytes(view[start:start + step])
            end_offset = self.offset + len(chunk)
            self._inflight.append(
                (end_offset, client._send_write(self._handle, self.offset, chunk))
            )
            self.offset = end_offset
            if len(self._inflight) >= pipeline.max_pending:
                pipeline.note_full(end_offset)
                # ``while``: after the window shrinks, drain down to it rather
                # than holding the old depth one-in, one-out.
                while len(self._inflight) >= pipeline.max_pending:
                    self._drain_one()

    def flush(self) -> None:
        """Wait until every WRITE is acknowledged."""
        try:
            while self._inflight:
                self._drain_one()
        finally:
            self._client.last_transfer_peak_pending = self._pipeline.peak_pending

    def _drain_one(self) -> None:
        end_offset, slot = self._inflight.popleft()
        self._client._expect_ok(self._client._wait(slot))
        self._acked_through = end_offset
        self._pipeline.on_catch_up(end_offset)
        if self._on_ack is not None:
            self._on_ack(end_offset)


class SFTPFile:
    """A remote file opened with :meth:`SFTPClient.open`.

    Behaves like a binary, unbuffered Python file: it tracks its own position
    for ``read()``/``write()``, supports ``seek()``/``tell()``, and closes on
    leaving a ``with`` block. Reads and writes larger than one chunk are
    pipelined, so ``f.read()`` of a large file is fast.
    """

    def __init__(self, client: "SFTPClient", handle: bytes, mode: str = "r") -> None:
        self._client = client
        self._handle = handle
        self._offset = 0
        self._closed = False
        self.mode = mode

    @property
    def handle(self) -> bytes:
        """The raw SFTP handle (for :meth:`SFTPClient.copy_data` etc.)."""
        return self._handle

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("I/O operation on closed file")

    def read(self, size: Optional[int] = None) -> bytes:
        """Read up to *size* bytes, or to EOF when *size* is ``None`` or
        negative. Returns ``b""`` at EOF."""
        self._check_open()
        if size is not None and size < 0:
            size = None
        chunks = []
        for chunk in self._client.iter_read(self._handle, self._offset, size):
            chunks.append(chunk)
            self._offset += len(chunk)
        return b"".join(chunks)

    def readinto(self, buffer) -> int:
        """Read into a writable buffer; returns the number of bytes read.

        Lets :class:`io.BufferedReader` wrap the file, e.g. for text lines:
        ``io.TextIOWrapper(io.BufferedReader(f), encoding="utf-8")``.
        """
        view = memoryview(buffer).cast("B")
        data = self.read(len(view))
        view[: len(data)] = data
        return len(data)

    def write(self, data: bytes) -> int:
        """Write *data* at the current position; returns ``len(data)``.
        Returns once the server has confirmed every chunk."""
        self._check_open()
        writer = self._client.pipelined_writer(self._handle, self._offset)
        writer.write(data)
        writer.flush()
        self._offset = writer.offset
        return len(data)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Move the position, like :meth:`io.IOBase.seek`; returns it."""
        self._check_open()
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._offset + offset
        elif whence == os.SEEK_END:
            position = int(self.stat().st_size or 0) + offset
        else:
            raise ValueError(f"invalid whence: {whence!r}")
        if position < 0:
            raise ValueError("negative seek position")
        self._offset = position
        return position

    def tell(self) -> int:
        """The current position."""
        return self._offset

    def stat(self) -> proto.SFTPAttributes:
        """Attributes of the open file."""
        return self._client.fstat(self._handle)

    def chmod(self, mode: int) -> None:
        """Change the open file's permission bits."""
        self._client.fsetstat(self._handle, proto.SFTPAttributes(st_mode=int(mode) & 0o7777))

    def utime(self, times: Optional[Tuple[float, float]] = None) -> None:
        """Set access and modification times; see :meth:`SFTPClient.utime`."""
        if times is None:
            now = time.time()
            times = (now, now)
        atime, mtime = times
        self._client.fsetstat(
            self._handle, proto.SFTPAttributes(st_atime=int(atime), st_mtime=int(mtime))
        )

    def truncate(self, size: Optional[int] = None) -> int:
        """Resize the file to *size* bytes (default: the current position).
        The position is unchanged. Returns the new size."""
        self._check_open()
        if size is None:
            size = self._offset
        self._client._expect_ok(
            self._client._request(
                proto.FXP_FSETSTAT, proto.pack_string(self._handle) + _size_attrs(size)
            )
        )
        return size

    def flush(self) -> None:
        """No-op: writes are confirmed before ``write()`` returns."""

    def readable(self) -> bool:
        return "r" in self.mode or "+" in self.mode

    def writable(self) -> bool:
        return any(flag in self.mode for flag in "wax+")

    def seekable(self) -> bool:
        return True

    def close(self) -> None:
        """Close the remote handle. Safe to call more than once."""
        if not self._closed:
            self._closed = True
            self._client.close_handle(self._handle)

    def __enter__(self) -> "SFTPFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ``open()`` modes (letters sorted, ``b``/``t`` removed) → OPEN pflags.
_OPEN_FLAGS = {
    "r": proto.FXF_READ,
    "+r": proto.FXF_READ | proto.FXF_WRITE,
    "w": proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC,
    "+w": proto.FXF_READ | proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC,
    "a": proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_APPEND,
    "+a": proto.FXF_READ | proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_APPEND,
    "x": proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_EXCL,
    "+x": proto.FXF_READ | proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_EXCL,
}

# Names a listing may not contain: they would escape the directory they are
# joined onto (on Windows a backslash separates too and ``:`` names a drive/stream).
_UNSAFE_NAME_CHARS = "/\x00\\:" if os.name == "nt" else "/\x00"


def _safe_name(name: Optional[str]) -> str:
    """Return a listed entry's name, refusing anything but one component."""
    if not name or name in (".", "..") or any(c in name for c in _UNSAFE_NAME_CHARS):
        raise proto.SFTPError(
            proto.FX_BAD_MESSAGE, f"server listed an unsafe file name: {name!r}"
        )
    return name


def _join_remote(base: str, *parts: str) -> str:
    """Join remote path components with ``/`` (remote paths are POSIX)."""
    result = base
    for part in parts:
        part = part.strip("/")
        if not part:
            continue
        if not result:
            result = part
        elif result.endswith("/"):
            result += part
        else:
            result += "/" + part
    return result


def _temp_name(remote: str) -> str:
    """A hidden sibling of *remote* to upload into before renaming."""
    head, _, name = remote.rpartition("/")
    temp = f".{name}.{uuid.uuid4().hex[:8]}.part"
    return f"{head}/{temp}" if head or remote.startswith("/") else temp


def _size_attrs(size: int) -> bytes:
    """ATTRS carrying just a size (``encode_attrs`` cannot express size 0)."""
    return proto.pack_uint32(proto.ATTR_SIZE) + proto.pack_uint64(int(size))


# Backwards-compatible aliases for the names used inside sshPilot.
OpenSSHSFTPClient = SFTPClient
OpenSSHSFTPFile = SFTPFile
