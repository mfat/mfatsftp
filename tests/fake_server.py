"""An in-memory SFTP v3 server for testing the client without a network.

``connect_fake()`` wires a :class:`mfatsftp.SFTPClient` to a ``_FakeSFTPServer``
over a socketpair, so the full request/response path (framing, ids, the reader
thread) is exercised with no ssh subprocess.
"""

import socket
import threading

from mfatsftp import SFTPClient
from mfatsftp import protocol as proto


class _FakeSFTPServer(threading.Thread):
    def __init__(self, p, stream_r, stream_w):
        super().__init__(daemon=True)
        self.p = p
        self._r = stream_r
        self._w = stream_w
        # In-memory FS: path -> ('dir', None) or ('file', bytearray)
        self.fs = {
            "/": ("dir", None),
            "/home": ("dir", None),
            "/home/alice": ("dir", None),
            "/home/alice/notes.txt": ("file", bytearray(b"hello world")),
            "/home/alice/sub": ("dir", None),
            "/home/alice/sub/inner.txt": ("file", bytearray(b"inner")),
        }
        self._handles = {}
        self._hseq = 0

    def _read_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self._r.read(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _read_packet(self):
        length = int.from_bytes(self._read_exact(4), "big")
        body = self._read_exact(length)
        return body[0], body[1:]

    def _send(self, ptype, payload):
        self._w.write(self.p.build_packet(ptype, payload))
        self._w.flush()

    def _status(self, rid, code):
        self._send(
            self.p.FXP_STATUS,
            self.p.pack_uint32(rid) + self.p.pack_uint32(code)
            + self.p.pack_string("") + self.p.pack_string(""),
        )

    def _attrs_for(self, path):
        kind, data = self.fs[path]
        a = self.p.SFTPAttributes()
        if kind == "dir":
            a.st_mode = 0o040755
            a.st_size = 0
        else:
            a.st_mode = 0o100644
            a.st_size = len(data)
        a.st_mtime = 1000
        return a

    def run(self):
        p = self.p
        try:
            # Handshake.
            ptype, _ = self._read_packet()
            assert ptype == p.FXP_INIT
            self._send(p.FXP_VERSION, p.pack_uint32(p.PROTOCOL_VERSION))
            while True:
                ptype, payload = self._read_packet()
                r = p._Reader(payload)
                rid = r.uint32()
                if ptype == p.FXP_REALPATH:
                    path = r.text() or "."
                    real = "/home/alice" if path == "." else path
                    self._send(
                        p.FXP_NAME,
                        p.pack_uint32(rid) + p.pack_uint32(1)
                        + p.pack_string(real) + p.pack_string(real)
                        + p.encode_attrs(self._attrs_for(real) if real in self.fs else p.SFTPAttributes()),
                    )
                elif ptype in (p.FXP_STAT, p.FXP_LSTAT):
                    path = r.text()
                    if path in self.fs:
                        self._send(p.FXP_ATTRS, p.pack_uint32(rid) + p.encode_attrs(self._attrs_for(path)))
                    else:
                        self._status(rid, p.FX_NO_SUCH_FILE)
                elif ptype == p.FXP_OPENDIR:
                    path = r.text()
                    if self.fs.get(path, (None,))[0] != "dir":
                        self._status(rid, p.FX_NO_SUCH_FILE)
                        continue
                    self._hseq += 1
                    handle = f"d{self._hseq}".encode()
                    children = sorted(
                        name[len(path):].lstrip("/")
                        for name in self.fs
                        if name != path
                        and name.startswith(path.rstrip("/") + "/")
                        and "/" not in name[len(path.rstrip("/")) + 1:]
                    )
                    self._handles[handle] = {"kind": "dir", "children": children, "done": False}
                    self._send(p.FXP_HANDLE, p.pack_uint32(rid) + p.pack_string(handle))
                elif ptype == p.FXP_READDIR:
                    handle = r.string()
                    state = self._handles.get(handle)
                    if not state or state["done"]:
                        self._status(rid, p.FX_EOF)
                        continue
                    state["done"] = True
                    names = state["children"]
                    body = p.pack_uint32(rid) + p.pack_uint32(len(names))
                    for name in names:
                        child_path = None
                        # reconstruct child path from any matching fs entry
                        for cand in self.fs:
                            if cand.endswith("/" + name) or cand == name:
                                child_path = cand
                        attr = self._attrs_for(child_path) if child_path else p.SFTPAttributes()
                        body += p.pack_string(name) + p.pack_string(name) + p.encode_attrs(attr)
                    self._send(p.FXP_NAME, body)
                elif ptype == p.FXP_OPEN:
                    path = r.text()
                    pflags = r.uint32()
                    self._hseq += 1
                    handle = f"f{self._hseq}".encode()
                    if pflags & p.FXF_CREAT:
                        if (pflags & p.FXF_EXCL) and path in self.fs:
                            self._status(rid, p.FX_FAILURE)
                            continue
                        self.fs[path] = ("file", bytearray())
                    elif path not in self.fs:
                        self._status(rid, p.FX_NO_SUCH_FILE)
                        continue
                    self._handles[handle] = {"kind": "file", "path": path}
                    self._send(p.FXP_HANDLE, p.pack_uint32(rid) + p.pack_string(handle))
                elif ptype == p.FXP_READ:
                    handle = r.string()
                    offset = r.uint64()
                    length = r.uint32()
                    path = self._handles[handle]["path"]
                    data = self.fs[path][1]
                    chunk = bytes(data[offset:offset + length])
                    if not chunk:
                        self._status(rid, p.FX_EOF)
                    else:
                        self._send(p.FXP_DATA, p.pack_uint32(rid) + p.pack_string(chunk))
                elif ptype == p.FXP_WRITE:
                    handle = r.string()
                    offset = r.uint64()
                    data = r.string()
                    path = self._handles[handle]["path"]
                    buf = self.fs[path][1]
                    if len(buf) < offset + len(data):
                        buf.extend(b"\x00" * (offset + len(data) - len(buf)))
                    buf[offset:offset + len(data)] = data
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_CLOSE:
                    handle = r.string()
                    self._handles.pop(handle, None)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_MKDIR:
                    path = r.text()
                    self.fs[path] = ("dir", None)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_REMOVE:
                    path = r.text()
                    self.fs.pop(path, None)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_RMDIR:
                    path = r.text()
                    self.fs.pop(path, None)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_RENAME:
                    old = r.text()
                    new = r.text()
                    self.fs[new] = self.fs.pop(old)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_SETSTAT:
                    r.text()  # path (mode ignored by this fake)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_EXTENDED:
                    name = r.text()
                    if name == "posix-rename@openssh.com":
                        old = r.text()
                        new = r.text()
                        self.fs[new] = self.fs.pop(old)  # overwrite allowed
                        self._status(rid, p.FX_OK)
                    else:
                        self._status(rid, p.FX_OP_UNSUPPORTED)
                else:
                    self._status(rid, p.FX_OP_UNSUPPORTED)
        except (EOFError, OSError, ValueError):
            return


def connect_fake():
    """Return (client, server): a started SFTPClient wired to a fake server."""
    csock, ssock = socket.socketpair()

    def _teardown():
        # EOF both ends so the client reader and the server thread both unblock.
        for s in (csock, ssock):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    client = SFTPClient(csock.makefile("wb"), csock.makefile("rb"), on_close=_teardown)
    server = _FakeSFTPServer(proto, ssock.makefile("rb"), ssock.makefile("wb"))
    server.start()
    client.start()
    return client, server
