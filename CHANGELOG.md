# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.2] - 2026-10-19

Licensing only; the library code is unchanged from 0.3.1.

### Changed
- License changed from GPL-3.0-or-later back to MIT, so mfatsftp can be
  used in any project, including closed-source ones.

## [0.3.1] - 2026-10-18

Documentation and packaging only; the library code is unchanged from 0.3.0.

### Fixed
- The README's link to the API reference was relative, so it led nowhere on
  the PyPI project page. All README links are now absolute or in-page, and a
  test enforces that.

### Changed
- README: install from PyPI (`pip install mfatsftp`), with PyPI, Python
  version, license and CI badges.
- Release workflow: a version tag always creates the GitHub release, with
  notes from the changelog. Publishing to PyPI is a separate job that only
  runs when the repository variable `PYPI_PUBLISH` is `true`, so it can no
  longer block the GitHub release. It can also be run by hand for an existing
  tag.

## [0.3.0] - 2026-10-18

paramiko parity for the SFTP client, a documentation site, strict typing, and
a security fix.

### Added
- Reply timeouts: `SFTPClient.settimeout()`/`gettimeout()` (also on
  `SFTPFile`, with `setblocking`), raising the new `SFTPTimeoutError`
  (a `TimeoutError`).
- Line I/O on `SFTPFile`: `readline`, `readlines`, iteration, `writelines`.
- Write buffering via `open(..., bufsize=N)` (`1` = line-buffered) and
  `SFTPFile.set_pipelined()`; `flush()` now sends and confirms buffered data.
- `chdir`/`getcwd` (an emulated working directory, as in paramiko).
- `listdir_iter` (streaming directory listing).
- `readv` on `SFTPClient` and `SFTPFile` (many ranges, one pipelined batch).
- `SFTPClient.max_concurrent_requests` and per-call
  `max_concurrent_prefetch_requests` on `get`/`getfo` to cap requests in flight.
- paramiko compatibility: `prefetch=` on `get`/`getfo`, `SFTPFile.prefetch()`
  (a no-op), `SFTPFile.chown()`, `SFTPAttributes.attr` (extended attributes,
  previously discarded) and `SFTPAttributes.from_stat()`.
- A garbage-collected `SFTPFile` sends its buffered writes and closes its
  handle without waiting for replies.
- Documentation site (Sphinx, with a reference generated from the
  docstrings), ready for Read the Docs; `CONTRIBUTING.md` and `SECURITY.md`.
- Tests that fail when a public member lacks a docstring or an entry in
  `docs/api.md`, or when a code sample in the docs does not compile.
- A release workflow that builds a `v*` tag, creates the GitHub release and
  publishes to PyPI.

### Changed
- Type hints pass `mypy --strict`; CI checks them, along with the docs build.
  Stream arguments are typed `IO[bytes]` and `env` as `Dict[str, str]`.

### Fixed
- `put_dir` now gives small files exactly their local permission bits. The
  server's umask could strip bits from them before, for example group write.
- The docs gave OpenSSH 9.6 as the first version with
  `users-groups-by-id@openssh.com`; it is 9.1.

### Security
- `connect()` / `build_ssh_argv()` refuse a host that is empty or starts with
  `-`. Before, a host such as `-oProxyCommand=…` from untrusted input was read
  by ssh as an option and could run a local command.

## [0.2.0] - 2026-10-18

Brings the client up to date with the SFTP code in sshPilot, adds the
convenience API a standalone library needs, and documents all of it.

### Added
- Pipelined transfers: reads and writes keep a window of requests in flight
  that grows from the measured round-trip time. Chunk sizes come from the
  server's `limits@openssh.com`. A server that returns less than it
  advertised gets the missing pieces fetched together.
- Batch operations that cost about one round trip per 100 paths:
  `stat_many`, `listdir_many`, `mkdir_many`, `remove_many`, `rmdir_many`,
  `read_small_files`, `atomic_upload_many` (with `AtomicUploadItem`).
  Directory listings keep several READDIRs in flight.
- `rmtree` (like `rm -r`, never follows symlinks) and `walk` (like `os.walk`).
- `getfo` / `putfo`, a `confirm` option on `put`/`putfo`, and `copy`
  (server-side with `copy-data`, read + write otherwise).
- Metadata: `fstat`, `setstat`, `fsetstat`, `chown`, `utime`, `truncate`,
  `symlink`, `readlink`.
- OpenSSH extensions: `atomic_rename` (falls back when `posix-rename` is
  missing or rejected), `hardlink`, `copy_data`, `statvfs` (`SFTPStatVFS`),
  `users_groups_by_id`, and `supports_*()` checks for each.
- `SFTPFile`: `seek`, `tell`, `truncate`, `stat`, `chmod`, `utime`,
  `readinto` (so `io.BufferedReader`/`io.TextIOWrapper` can wrap it),
  `readable`/`writable`/`seekable`, `closed`, `handle`.
- `open(..., create_mode=0o600)` sets a new file's permissions in the open
  request.
- `SFTPClient.closed`, `SFTPClient.extensions`, `max_read_length`,
  `max_write_length`, `max_open_handles`.
- `SFTPAttributes.is_file()` and an `ls -l`-style `str()`.
- Exception subclasses that match Python's built-ins: `SFTPNoSuchFileError`
  (`FileNotFoundError`), `SFTPPermissionError` (`PermissionError`),
  `SFTPConnectionError` (`ConnectionError`).
- `PipelinedWriter` (via `pipelined_writer`) with an `on_ack` progress hook.
- Full API reference (`docs/api.md`), an expanded README, and
  `examples/transfer.py`.
- Tests against the real OpenSSH `sftp-server` binary (including simulated
  high-latency links), and an opt-in end-to-end test through `ssh`
  (`MFATSFTP_TEST_HOST`).

### Changed
- `get`/`put`/`get_dir`/`put_dir` use the pipelined and batched paths. A
  tree of many small files now costs a few round trips per directory level
  instead of several per file.
- `put_dir` keeps local permission bits and modification times, and writes
  small files through a temporary name and a rename.
- `makedirs` now raises if something other than a directory is in the way,
  instead of silently ignoring every error.
- `open()` modes now follow Python: `r+` no longer creates a missing file,
  `w+` truncates, `x` fails if the file exists, `a` starts at the end, and an
  unknown mode raises `ValueError`.
- `SFTPFile.write` returns the number of bytes written.
- `from_command`/`connect`: `timeout` now limits login plus handshake and
  raises `TimeoutError`. The default is `None` (no limit). Before, the
  handshake could hang indefinitely whatever the value.
- Packaging: SPDX license expression (`GPL-3.0-or-later`) per PEP 639,
  Python 3.14 classifier, ruff configuration. CI lints, builds and checks
  the package, and runs the real-server tests.
- License changed from MIT to GPL-3.0-or-later.

### Fixed
- Reads no longer stop short on large files, and writes larger than one
  packet no longer drop the session. OpenSSH caps a READ at about 255 KiB and
  closes the connection on a message over 256 KiB.
- Filenames that are not valid UTF-8 are kept intact (`surrogateescape`)
  instead of being replaced, so such files can be opened, renamed and
  deleted.
- `get_dir` refuses server-listed names containing a path separator or equal
  to `.`/`..`, so a hostile server cannot make it write outside the target
  directory.
- A failed connection attempt now closes the ssh process's pipes.

## [0.1.0] - initial extraction
### Added
- Pure-Python SFTP v3 wire-protocol codec (`mfatsftp.protocol`).
- `SFTPClient` / `SFTPFile` over a stream pair, with a paramiko-compatible
  surface (`open`/`file`, `stat`/`lstat`, `listdir`/`listdir_attr`,
  `mkdir`/`makedirs`/`rmdir`, `remove`/`unlink`, `rename`/`posix_rename`,
  `chmod`, `realpath`/`normalize`, `get`/`put`/`get_dir`/`put_dir`).
- `connect()` / `from_command()` / `build_ssh_argv()` transport over the system
  OpenSSH (`ssh -s <host> sftp`), with clean subprocess teardown.
- In-memory SFTP server test fixture; protocol, client and transport test suites.

[Unreleased]: https://github.com/mfat/mfatsftp/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/mfat/mfatsftp/releases/tag/v0.3.2
[0.3.1]: https://github.com/mfat/mfatsftp/releases/tag/v0.3.1
[0.3.0]: https://github.com/mfat/mfatsftp/releases/tag/v0.3.0
[0.2.0]: https://github.com/mfat/mfatsftp/commit/09978e8
[0.1.0]: https://github.com/mfat/mfatsftp/commit/aa26356
