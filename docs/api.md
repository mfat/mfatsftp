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
raises `ValueError`. `bufsize` is ignored (kept for paramiko compatibility).
`create_mode` sets a new file's permissions in the same request (the server's
umask still applies).

### Transfers

All transfers are pipelined. `callback(bytes_done, bytes_total)` is optional;
`total` is 0 when unknown. Each method returns the number of bytes
transferred.

| Method | Description |
|---|---|
| `get(remotepath, localpath, callback=None)` | Download to a local file (created or truncated). |
| `getfo(remotepath, fl, callback=None)` | Download into a binary file object. |
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
| `iter_read(handle, offset=0, length=None)` | Generator of chunks from `offset` up to `length` or EOF, pipelined. |
| `write(handle, offset, data)` | One WRITE; `data` must fit in `max_write_length`. |
| `pipelined_writer(handle, offset=0, *, on_ack=None) -> PipelinedWriter` | Streaming writer; see below. |

---

## `SFTPFile`

Returned by `SFTPClient.open`. A binary, unbuffered file object. Reads and
writes larger than one chunk are pipelined.

| Member | Description |
|---|---|
| `read(size=None) -> bytes` | Up to `size` bytes, or to EOF when `size` is `None` or negative. `b""` at EOF. |
| `readinto(buffer) -> int` | Read into a writable buffer. Lets `io.BufferedReader` wrap the file: `io.TextIOWrapper(io.BufferedReader(f), encoding="utf-8")`. |
| `write(data) -> int` | Write at the current position. Returns once the server has confirmed it. |
| `seek(offset, whence=os.SEEK_SET) -> int` | `SEEK_SET`, `SEEK_CUR` or `SEEK_END`. |
| `tell() -> int` | Current position. |
| `truncate(size=None) -> int` | Resize (default: to the current position). The position does not move. |
| `stat() -> SFTPAttributes` | Attributes of the open file. |
| `chmod(mode)`, `utime(times=None)` | As on the client, for this file. |
| `flush()` | No-op (writes are already confirmed). |
| `readable()`, `writable()`, `seekable()` | From the mode. |
| `close()`, `closed` | Close the handle (idempotent). Using a closed file raises `ValueError`. |
| `handle` | The raw handle, e.g. for `copy_data`. |
| `mode` | The mode string passed to `open`. |
| `with f:` | Closes on exit. |

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

Methods: `is_file()`, `is_dir()`, `is_symlink()`. `str(attr)` gives an
`ls -l`-like line.

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
