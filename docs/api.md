# mfatsftp API reference

Everything listed here is importable from the top-level `mfatsftp` package,
except the low-level codec in [`mfatsftp.protocol`](#mfatsftpprotocol).
Remote paths are `str` and always use `/`. Local paths can be `str` or
`os.PathLike`. File data is always `bytes`.

- [Connecting](#connecting): `connect`, `from_command`, `build_ssh_argv`
- [`SFTPClient`](#sftpclient)
  - [Lifecycle](#lifecycle)
  - [Paths and metadata](#paths-and-metadata)
  - [Directories](#directories)
  - [Removing and renaming](#removing-and-renaming)
  - [Opening files](#opening-files)
  - [Transfers](#transfers)
  - [Batch operations](#batch-operations)
  - [Server extensions](#server-extensions)
  - [Low-level handle I/O](#low-level-handle-io)
- [`SFTPFile`](#sftpfile)
- [`SFTPAttributes`](#sftpattributes), [`SFTPStatVFS`](#sftpstatvfs)
- [`AtomicUploadItem`](#atomicuploaditem), [`PipelinedWriter`](#pipelinedwriter)
- [Exceptions](#exceptions)
- [`mfatsftp.protocol`](#mfatsftpprotocol)

---

## Connecting

### `connect(host, *, port=None, identity=None, config=None, proxy_jump=None, known_hosts=None, ssh_options=None, batch_mode=False, ssh_path="ssh", extra_args=None, env=None, timeout=None) -> SFTPClient`

Starts `ssh … -s <host> sftp`, runs the SFTP handshake and returns a connected
client that owns the ssh process. Closing the client stops the process.

`host` is `"user@hostname"`, `"hostname"` or a `~/.ssh/config` alias. The
connection options are those of [`build_ssh_argv`](#build_ssh_argvhost-options---liststr).
`env` and `timeout` are those of [`from_command`](#from_commandargv--envnone-timeoutnone---sftpclient).

### `from_command(argv, *, env=None, timeout=None) -> SFTPClient`

Runs `argv`, which must speak the SFTP subsystem on its stdin/stdout, and
returns a connected client that owns the process. Use this for full control
of the command, such as an `sshpass` wrapper for password auth.

- `env`: extra environment variables, merged over `os.environ`.
- `timeout`: seconds allowed for login plus the SFTP handshake. If it takes
  longer, the process is stopped and `TimeoutError` is raised. `None` (the
  default) waits indefinitely, which suits interactive passphrase prompts.
  For network timeouts, prefer `ssh_options=["ConnectTimeout=10"]`.

Raises `PermissionError` when authentication fails, `TimeoutError`, or
`OSError` carrying ssh's error output for any other failure.

### `build_ssh_argv(host, **options) -> list[str]`

Returns the argv `connect` would run, without running it.

| Option | Effect |
|---|---|
| `port` | `-p PORT` |
| `identity` | `-i FILE` (`~` expanded) |
| `config` | `-F FILE` (`~` expanded) |
| `proxy_jump` | `-J HOSTS` |
| `known_hosts` | `-o UserKnownHostsFile=FILE` (`~` expanded) |
| `ssh_options` | each entry becomes `-o ENTRY` |
| `batch_mode` | `-o BatchMode=yes` (fail instead of prompting) |
| `ssh_path` | the executable (default `ssh`) |
| `extra_args` | inserted before `-s` |

---

## `SFTPClient`

A synchronous SFTP v3 client over a pair of byte streams. One background
thread matches replies to requests, so several requests can be in flight at
once and the client may be shared between threads.

### Lifecycle

| Member | Description |
|---|---|
| `SFTPClient(stdin, stdout, on_close=None)` | Wrap a stream pair: `stdin` is written to the server, `stdout` read from it. `on_close()` is called by `close()` to tear the transport down. Call `start()` before use. |
| `SFTPClient.from_pipes(stdin, stdout, on_close=None, *, start=True)` | Same, running the handshake by default. |
| `start()` | Run the INIT/VERSION handshake and adopt the server's limits. |
| `close()` | Close the session and transport. Idempotent. Pending requests fail with `SFTPConnectionError`. |
| `closed` | `True` after `close()` or once the connection is lost. |
| `with client:` | Closes on exit. |
| `version` | Protocol version the server speaks (3 for OpenSSH). |
| `extensions` | `dict[str, bytes]` of extensions the server advertised. |
| `max_read_length`, `max_write_length` | Transfer chunk sizes (from `limits@openssh.com`, else 32 KiB). |
| `max_open_handles` | Server's open-handle limit, 0 if unknown. |
| `max_concurrent_requests` | Optional cap on requests kept in flight by transfers and batch operations. `None` (default) means adaptive. Set it, e.g. to 64, for servers that stall with many outstanding requests. A server that sends back short reads can briefly see up to twice the cap. |
| `settimeout(seconds)`, `gettimeout()`, `timeout` | Wait at most this long for each reply. `None` (default) waits forever. A late reply raises `SFTPTimeoutError` (a `TimeoutError`) and is discarded when it arrives. The server may still have carried the request out, so closing the client is the safe way to recover. |
| `chdir(path=None)`, `getcwd()` | Emulated working directory, as in paramiko: relative paths passed to this client are joined onto it. `chdir(None)` clears it. A non-directory raises `NotADirectoryError`. |

### Paths and metadata

| Method | Description |
|---|---|
| `realpath(path) -> str` | Canonical absolute path. `"."` gives the login directory. |
| `normalize(path) -> str` | Alias for `realpath` (paramiko). |
| `stat(path) -> SFTPAttributes` | Attributes, following symlinks. |
| `lstat(path) -> SFTPAttributes` | Attributes of a symlink itself. |
| `setstat(path, attr)` | Set the fields present in `attr`: mode if non-zero, uid+gid as a pair, atime+mtime as a pair, size if non-zero. |
| `chmod(path, mode)` | Permission bits, e.g. `0o644`. |
| `chown(path, uid, gid)` | Numeric owner and group. |
| `utime(path, times=None)` | `(atime, mtime)` in epoch seconds, or `None` for now. Whole seconds. |
| `truncate(path, size)` | Cut or zero-extend to `size` bytes. |
| `symlink(target_path, link_path)` | Create `link_path` pointing at `target_path` (same argument order as `os.symlink`). |
| `readlink(path) -> str` | A symlink's target, unresolved. |

### Directories

| Method | Description |
|---|---|
| `listdir(path=".") -> list[str]` | Entry names (`.`/`..` omitted). |
| `listdir_attr(path=".") -> list[SFTPAttributes]` | Entries with `filename` set. Attributes describe the entry itself, so symlinks show as symlinks. |
| `listdir_iter(path=".", read_aheads=8)` | Generator yielding the same entries as they arrive, with `read_aheads` READDIRs in flight. The directory is opened on the first `next()`; stopping early closes it. |
| `walk(top)` | Generator of `(dirpath, dirnames, filenames)`, top-down, like `os.walk`. Symlinks to directories appear in `filenames` and are not followed. Removing items from `dirnames` prunes the walk. |
| `mkdir(path, mode=None)` | Create a directory (`mode` is subject to the server's umask). |
| `makedirs(path, mode=None)` | Create with missing parents, like `mkdir -p`. Existing directories are fine; a non-directory in the way raises `SFTPError`. |
| `rmdir(path)` | Remove an empty directory. |
| `rmtree(path)` | Recursive delete, like `rm -r`. Symlinks are removed, never followed. Works a level at a time with batched requests. A file or symlink `path` is just removed. On error, whatever was not yet deleted is left in place. |

### Removing and renaming

| Method | Description |
|---|---|
| `remove(path)` / `unlink(path)` | Remove a file or symlink. |
| `rename(old, new)` | Plain SFTP rename. Fails if `new` exists. |
| `posix_rename(old, new)` | Atomic rename over an existing `new` (needs `posix-rename@openssh.com`). |
| `atomic_rename(old, new)` | `posix_rename` when available. Otherwise (or if the server rejects it) removes `new` and renames. |
| `supports_posix_rename() -> bool` | Whether the server advertises `posix-rename@openssh.com`. |

### Opening files

#### `open(path, mode="r", bufsize=-1, *, create_mode=None) -> SFTPFile`

Also available as `file(...)`. Modes follow Python's `open()`:

| Mode | Read | Write | Creates | Truncates | Notes |
|---|---|---|---|---|---|
| `r` | ✓ | | | | |
| `r+` | ✓ | ✓ | | | |
| `w` | | ✓ | ✓ | ✓ | |
| `w+` | ✓ | ✓ | ✓ | ✓ | |
| `a` | | ✓ | ✓ | | starts at end of file; writes append |
| `a+` | ✓ | ✓ | ✓ | | as `a` |
| `x` | | ✓ | ✓ | | fails if the file exists |
| `x+` | ✓ | ✓ | ✓ | | as `x` |

`b` and `t` are accepted and ignored: data is always bytes. Any other mode
raises `ValueError`. `create_mode` sets a new file's permissions in the same
request (the server's umask still applies).

`bufsize` controls write buffering, as in paramiko:

| `bufsize` | Writes |
|---|---|
| `-1` (default), `0` | each `write()` is sent at once and confirmed before it returns |
| `1` | buffered until a newline is written |
| `> 1` | buffered up to that many bytes |

Buffered data goes out on `flush()`, on any read, seek, stat or metadata
call, and on `close()`. Reads are always buffered internally for
`readline`.

### Transfers

All transfers are pipelined. `callback(bytes_done, bytes_total)` is optional;
`total` is 0 when unknown. Each method returns the number of bytes
transferred.

| Method | Description |
|---|---|
| `get(remotepath, localpath, callback=None, prefetch=True, max_concurrent_prefetch_requests=None)` | Download to a local file (created or truncated). `max_concurrent_prefetch_requests` caps the reads in flight. `prefetch` is ignored, because reads are always pipelined; it is accepted for paramiko compatibility. |
| `getfo(remotepath, fl, callback=None, prefetch=True, max_concurrent_prefetch_requests=None)` | Download into a binary file object. |
| `put(localpath, remotepath, callback=None, confirm=False)` | Upload a local file (remote file created or truncated). |
| `putfo(fl, remotepath, file_size=0, callback=None, confirm=False)` | Upload from a binary file object. `file_size` is the callback's total (taken from `fl` if it is a real file). `confirm=True` checks the remote size afterwards and raises `OSError` on a mismatch. |
| `get_dir(remotepath, localpath, callback=None)` | Recursive download into `localpath` (created if needed). Symlinks to files are downloaded as files. Symlinks to directories and dangling symlinks are skipped. Names containing `/` (or `\` and `:` on Windows), or equal to `.`/`..`, raise `SFTPError`. |
| `put_dir(localpath, remotepath, callback=None)` | Recursive upload. Creates `remotepath` and missing subdirectories and replaces existing files. Keeps permission bits and mtimes. Small files are written to a hidden temporary name, then renamed into place. Symlinks to files are uploaded as files; symlinks to directories are not followed. |
| `copy(source, destination)` | Copy on the server: with `copy-data` no data crosses the network, otherwise it is read and written back. |

### Batch operations

These send many requests at once (in windows of up to 100), so N paths cost
about N/100 round trips instead of N. Results are in input order.

| Method | Description |
|---|---|
| `stat_many(paths, *, missing_on_error=False) -> list[SFTPAttributes \| None]` | `None` for missing paths. Other errors raise once in-flight replies have drained, unless `missing_on_error` is set (then any error except a lost connection gives `None`). |
| `listdir_many(paths) -> list[list[SFTPAttributes]]` | List many directories. The first error is raised after every handle is closed. |
| `mkdir_many(paths, mode=None) -> list[BaseException \| None]` | `None` per success, else the error. Paths in one call must not depend on each other. |
| `remove_many(paths, *, continue_on_error=False) -> list[(path, exc)]` | Delete files and symlinks. Missing paths are ignored. Raises the first error, or with `continue_on_error` returns all failures. |
| `rmdir_many(paths, *, continue_on_error=False)` | As `remove_many`, for empty directories. Remove children before parents across calls. |
| `read_small_files(items, *, check_cancel=None)` | `items` are `(path, expected_size)`. Yields `(start_index, [bytes, …])` per window. Files are read with one READ each when their size is as expected. Files that grew or shrank are still read completely. |
| `small_read_limit() -> int` | Largest size `read_small_files` reads in a single request. |
| `atomic_upload_many(items, *, on_file_bytes=None, check_cancel=None)` | Upload many small files ([`AtomicUploadItem`](#atomicuploaditem)), each through a temporary name and a rename. `on_file_bytes(item, n)` reports progress. Raise from `check_cancel()` to abort; temporary files are cleaned up. |

### Server extensions

| Method | Needs | Description |
|---|---|---|
| `supports_hardlink()`, `hardlink(old, new)` | `hardlink@openssh.com` | Create `new` as a hard link to `old`. |
| `supports_copy_data()`, `copy_data(src_handle, dst_handle)` | `copy-data` | Copy a whole open file into another on the server. |
| `supports_statvfs()`, `statvfs(path) -> SFTPStatVFS` | `statvfs@openssh.com` | Filesystem sizes, like `os.statvfs`. |
| `supports_users_groups_by_id()`, `users_groups_by_id(uids, gids) -> (users, groups)` | `users-groups-by-id@openssh.com` | Names in request order, `""` where the server has none. |

### Low-level handle I/O

For building your own operations. Handles are `bytes`.

| Method | Description |
|---|---|
| `open_handle(path, pflags, attr=None) -> bytes` | OPEN with `protocol.FXF_*` flags. |
| `close_handle(handle)` | CLOSE. Errors are logged, not raised. |
| `fstat(handle) -> SFTPAttributes` | Attributes of an open handle. |
| `fsetstat(handle, attr)` | As `setstat`, on a handle. |
| `read(handle, offset, length) -> bytes` | One READ. `b""` at EOF; may return fewer bytes than asked before EOF. |
| `iter_read(handle, offset=0, length=None, *, max_concurrent_requests=None)` | Generator of chunks from `offset` up to `length` or EOF, pipelined. |
| `readv(handle, chunks, *, max_concurrent_requests=None) -> list[bytes]` | Read many `(offset, length)` ranges at once, in order. A range past EOF comes back short. |
| `write(handle, offset, data)` | One WRITE; `data` must fit in `max_write_length`. |
| `pipelined_writer(handle, offset=0, *, on_ack=None) -> PipelinedWriter` | Streaming writer; see below. |

---

## `SFTPFile`

Returned by `SFTPClient.open`. A binary file object. Reads and writes larger
than one chunk are pipelined.

| Member | Description |
|---|---|
| `read(size=None) -> bytes` | Up to `size` bytes, or to EOF when `size` is `None` or negative. `b""` at EOF. |
| `readline(size=None) -> bytes` | One line including `b"\n"` (at most `size` bytes). `b""` at EOF. Data is fetched a server-sized chunk at a time, so reading line by line costs one round trip per chunk, not per line. |
| `readlines(sizehint=None)`, `for line in f` | All lines, or iterate over them. |
| `readinto(buffer) -> int` | Read into a writable buffer. This lets `io.TextIOWrapper(io.BufferedReader(f), encoding="utf-8")` wrap the file for decoded text. |
| `readv(chunks, max_concurrent_prefetch_requests=None) -> list[bytes]` | Read many `(offset, length)` ranges at once. The position is unchanged. |
| `write(data) -> int` | Write at the current position. Unless buffered or pipelined, returns once the server has confirmed it. |
| `writelines(lines)` | `write` each item. |
| `flush()` | Send buffered writes and wait for confirmation. Raises the first write error. |
| `set_pipelined(pipelined=True)` | Don't wait for each write to be confirmed. Errors then surface from the next read, seek, stat, `flush` or `close`. |
| `seek(offset, whence=os.SEEK_SET) -> int` | `SEEK_SET`, `SEEK_CUR` or `SEEK_END`. |
| `tell() -> int` | Current position. |
| `truncate(size=None) -> int` | Resize (default: to the current position). The position does not move. |
| `stat() -> SFTPAttributes` | Attributes of the open file. |
| `chmod(mode)`, `chown(uid, gid)`, `utime(times=None)` | As on the client, for this file. |
| `settimeout(t)`, `gettimeout()`, `setblocking(flag)` | Set the timeout of the whole client, as in paramiko. `setblocking(False)` is a timeout of 0. |
| `prefetch(file_size=None, max_concurrent_requests=None)` | No-op kept for paramiko compatibility; reads are always pipelined. |
| `readable()`, `writable()`, `seekable()` | From the mode. |
| `close()`, `closed` | Send buffered writes and close the handle (idempotent). A write error is raised after the handle is closed. Using a closed file raises `ValueError`. |
| `handle` | The raw handle, e.g. for `copy_data`. |
| `mode` | The mode string passed to `open`. |
| `with f:` | Closes on exit. |

A file that is garbage-collected without being closed sends its buffered
writes and closes its handle without waiting for replies, so errors at that
point go unnoticed. Close files explicitly.

---

## `SFTPAttributes`

A dataclass returned by `stat`, `lstat`, `fstat` and the listing methods.

| Field | Type | Notes |
|---|---|---|
| `st_size` | `int` | 0 if not sent |
| `st_mode` | `int` | type and permission bits; works with the `stat` module |
| `st_uid`, `st_gid` | `int \| None` | |
| `st_atime`, `st_mtime` | `int \| None` | epoch seconds |
| `filename` | `str \| None` | set for listing entries |
| `longname` | `str \| None` | the server's `ls -l` style line, for listing entries |
| `attr` | `dict[str, bytes]` | extended attributes the server sent (sent back by `setstat` when non-empty) |

Methods: `is_file()`, `is_dir()`, `is_symlink()`, and the class method
`SFTPAttributes.from_stat(os_stat_result, filename=None)`. `str(attr)` gives
an `ls -l`-like line.

To set attributes, build one with only the fields you want to change and pass
it to `setstat`/`fsetstat`.

## `SFTPStatVFS`

A named tuple shaped like `os.statvfs()`: `f_bsize`, `f_frsize`, `f_blocks`,
`f_bfree`, `f_bavail`, `f_files`, `f_ffree`, `f_favail`, `f_fsid`, `f_flag`,
`f_namemax`. Free space for normal users is `f_bavail * f_frsize`.

## `AtomicUploadItem`

A frozen dataclass describing one upload for `atomic_upload_many`:

| Field | Description |
|---|---|
| `local_path` | file to read |
| `remote_temp` | temporary remote name to write first (same directory as the destination) |
| `remote_dst` | final remote name; replaced if it exists |
| `create_mode` | permissions given when the temporary file is created (umask applies) |
| `existing_mode` | if not `None`, set explicitly afterwards (e.g. to keep a replaced file's mode) |
| `atime`, `mtime` | timestamps to set |

## `PipelinedWriter`

Streams data into an open handle with a window of writes in flight that adapts
to the round-trip time.

```python
with sftp.open("/tmp/out", "wb") as f:
    writer = sftp.pipelined_writer(f.handle, on_ack=lambda offset: print(offset))
    for chunk in produce_chunks():
        writer.write(chunk)
    writer.flush()          # required: waits for every write and raises any error
```

| Member | Description |
|---|---|
| `write(data)` | Queue `data` at `offset`, split into server-sized chunks. May raise an earlier write's error. |
| `flush()` | Wait until every write is confirmed. Call it before closing the handle. |
| `offset` | Where the next write lands. |
| `peak_pending` | Largest window reached (diagnostics). |

---

## Exceptions

All SFTP failures raise `SFTPError` (a subclass of `OSError`). It has
`.code` (an `FX_*` status from `mfatsftp.protocol`), `.errno` (the closest
POSIX errno) and `.message` (the server's text, possibly empty). Constructing
an `SFTPError` picks the subclass that matches the code:

| Class | Also a | For |
|---|---|---|
| `SFTPNoSuchFileError` | `FileNotFoundError` | `FX_NO_SUCH_FILE` |
| `SFTPPermissionError` | `PermissionError` | `FX_PERMISSION_DENIED` |
| `SFTPConnectionError` | `ConnectionError` | `FX_NO_CONNECTION`, `FX_CONNECTION_LOST`, and any request after the session closed |
| `SFTPTimeoutError` | `TimeoutError` | no reply within the client's `settimeout` (`.code` is `FX_FAILURE`, `.errno` is `ETIMEDOUT`) |
| `SFTPError` | `OSError` | everything else (`FX_FAILURE`, `FX_OP_UNSUPPORTED`, …) |

`connect`/`from_command` raise `PermissionError`, `TimeoutError` or `OSError`
(see above).

---

## `mfatsftp.protocol`

The pure SFTP v3 codec, with no I/O. It is useful for tests and custom
tooling.

- Constants: packet types `FXP_*`, status codes `FX_*`, open flags `FXF_*`,
  attribute flags `ATTR_*`, `PROTOCOL_VERSION`.
- Encoding: `pack_uint32`, `pack_uint64`, `pack_string`, `encode_attrs`,
  `build_packet`, `build_init`, `build_request`, `pack_id_list`.
- Decoding: `decode_attrs`, `parse_version`, `parse_status`, `parse_name`,
  `parse_handle`, `parse_data`, `parse_attrs`, `parse_statvfs`,
  `parse_id_names`, `response_request_id`.

Strings are encoded as UTF-8 and decoded with `surrogateescape`, so names that
aren't valid UTF-8 survive a round trip.

---

### Compatibility aliases

`mfatsftp.client.OpenSSHSFTPClient` and `mfatsftp.client.OpenSSHSFTPFile` are
aliases of `SFTPClient` and `SFTPFile`, for code written against sshPilot's
internal names.
