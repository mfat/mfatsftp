# mfatsftp

[![PyPI](https://img.shields.io/pypi/v/mfatsftp)](https://pypi.org/project/mfatsftp/)
[![Python versions](https://img.shields.io/pypi/pyversions/mfatsftp)](https://pypi.org/project/mfatsftp/)
[![License](https://img.shields.io/pypi/l/mfatsftp)](https://github.com/mfat/mfatsftp/blob/main/LICENSE)
[![CI](https://github.com/mfat/mfatsftp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mfat/mfatsftp/actions/workflows/ci.yml)

**A pure-Python SFTP client that works through your system's OpenSSH.**

`mfatsftp` implements the SFTP application protocol (v3, the version OpenSSH
speaks) and uses the local `ssh` binary as its transport (`ssh -s <host> sftp`).
It does **not** reimplement SSH. Every cipher and key exchange your OpenSSH
supports (including post-quantum ones) works automatically, as do your
`~/.ssh/config` settings, `ProxyJump`, certificates, the agent, FIDO/PIV keys
and askpass prompts. The library has no crypto code to maintain and no
dependencies beyond the standard library.

```python
import mfatsftp

with mfatsftp.connect("user@host") as sftp:
    for attr in sftp.listdir_attr("."):
        print(attr.filename, attr.st_size, attr.is_dir())
    sftp.get("/etc/hostname", "hostname.txt")
    sftp.put("local.txt", "/tmp/remote.txt")
    sftp.put_dir("site/", "/var/www/site")
```

- [Requirements & scope](#requirements--scope)
- [Installation](#installation)
- [Connecting](#connecting)
- [Usage guide](#usage-guide)
- [Errors](#errors)
- [Performance](#performance)
- [Coming from paramiko](#coming-from-paramiko)
- [API reference](https://github.com/mfat/mfatsftp/blob/main/docs/api.md) (full reference generated from the docstrings: build `docs/` with Sphinx)
- [Development](#development)
- [License](#license)

## Requirements & scope

- **Python 3.9+**, no third-party dependencies.
- **The `ssh` binary (OpenSSH) at runtime.** That is the trade-off: you get
  all of OpenSSH's transport and configuration for free, but mfatsftp is not
  self-contained the way a full SSH stack (e.g. paramiko) is.
- **SFTP client only.** No remote command execution, no port forwarding, no
  shell channels, no server side. If you use paramiko *only* for SFTP, this is
  close to a drop-in replacement. If you use the rest of paramiko, it isn't.
- **Linux and macOS** are the supported platforms. Windows OpenSSH may work
  but is not tested yet.
- **Servers:** written for any SFTP v3 server and tested against OpenSSH's
  `sftp-server`. It copes with servers that lack OpenSSH's extensions
  (renames fall back, chunks drop to 32 KiB) or return short reads.
  OpenSSH servers also get the extras listed
  [below](#server-extensions).

## Installation

From [PyPI](https://pypi.org/project/mfatsftp/):

```sh
pip install mfatsftp
```

The latest development version: `pip install git+https://github.com/mfat/mfatsftp`
(or `pip install .` from a checkout).

## Connecting

```python
sftp = mfatsftp.connect("myalias")             # a ~/.ssh/config Host alias
sftp = mfatsftp.connect("deploy@203.0.113.7", port=2222, identity="~/.ssh/deploy_ed25519")
sftp = mfatsftp.connect("db1", proxy_jump="bastion", batch_mode=True)
```

`connect()` runs `ssh … -s <host> sftp`. Settings you don't pass are resolved
by ssh from `~/.ssh/config`, exactly as on the command line. That file stays
the source of truth.

| Option | ssh equivalent |
|---|---|
| `port` | `-p` |
| `identity` | `-i` |
| `config` | `-F` |
| `proxy_jump` | `-J` |
| `known_hosts` | `-o UserKnownHostsFile=` |
| `ssh_options=["Key=value", …]` | `-o Key=value` … |
| `batch_mode=True` | `-o BatchMode=yes` (never prompt; use it for scripts and services) |
| `ssh_path` | which `ssh` to run |
| `extra_args` | anything else, e.g. `["-v"]` |
| `env` | extra environment variables for ssh |
| `timeout` | seconds allowed for login and handshake (default: no limit) |

Always close the client, ideally with a `with` block. Closing stops the ssh
process. To see the exact command without running it, use
`mfatsftp.build_ssh_argv(host, **options)`. For full control (wrapper
programs, unusual flags) pass your own command to
`mfatsftp.from_command(argv)`.

### Authentication

Authentication is ssh's job: the agent, keys from `~/.ssh/config`, and the
system askpass (a GUI passphrase prompt when there is no terminal) all just
work. A connection that fails to authenticate raises `PermissionError`, with
ssh's message.

For **password auth**, wrap your own command (e.g. with `sshpass`) and pass it
to `from_command`:

```python
sftp = mfatsftp.from_command(
    ["sshpass", "-f", "/path/to/passfile", "ssh", "-s", "host", "sftp"]
)
```

### Host keys

Host-key checking is OpenSSH's, with your `known_hosts`. For first contact in
automation, use `ssh_options=["StrictHostKeyChecking=accept-new"]`.

## Usage guide

### Files

```python
sftp.put("report.pdf", "/srv/reports/report.pdf")   # upload
sftp.get("/var/log/syslog", "syslog.txt")           # download

# Progress: callback(bytes_done, bytes_total)
sftp.put("big.iso", "/tmp/big.iso", callback=lambda done, total: print(f"{done}/{total}"))

# File objects instead of paths
import io
sftp.putfo(io.BytesIO(b"hello\n"), "/tmp/hello.txt")
buf = io.BytesIO()
sftp.getfo("/tmp/hello.txt", buf)
```

`open()` returns a binary file object with `read`, `readline`, line iteration,
`write`, `writelines`, `seek`, `tell`, `truncate`, `stat` and `chmod`. Modes
follow Python's `open()` (`r`, `r+`, `w`, `w+`, `a`, `a+`, `x`, `x+`):

```python
with sftp.open("/etc/os-release") as f:
    text = f.read().decode()

with sftp.open("/srv/app/.env", "w", create_mode=0o600) as f:   # private from the start
    f.write(b"SECRET=1\n")

with sftp.open("/var/log/app.log", "a") as f:
    f.write(b"another line\n")

with sftp.open("/var/log/syslog") as f:
    for line in f:                 # one round trip per ~255 KiB, not per line
        ...

with sftp.open("/tmp/export.csv", "w", bufsize=65536) as f:   # buffer small writes
    for row in rows:
        f.write(row)
```

By default each `write()` waits for the server to confirm it, which costs a
round trip per call. When you write many small pieces, open with a `bufsize`
or call `f.set_pipelined()`. A write error then surfaces at the next
`flush()`, read, seek or `close()` instead.

### Safe replacement

Plain SFTP `rename` fails if the destination exists. To replace a file
atomically, write a temporary file and use `atomic_rename`. It uses OpenSSH's
`posix-rename` when the server has it. Otherwise it falls back to remove +
rename, which is not atomic: the destination is briefly missing.

```python
with sftp.open("/srv/app/config.tmp", "w") as f:
    f.write(new_config)
sftp.atomic_rename("/srv/app/config.tmp", "/srv/app/config")
```

### Directories

```python
sftp.listdir("/srv")                    # names
sftp.listdir_attr("/srv")               # SFTPAttributes, with .filename
for attr in sftp.listdir_iter("/huge"): # streams entries as they arrive
    ...
for top, dirs, files in sftp.walk("/srv"):   # like os.walk
    ...
sftp.mkdir("/srv/new", 0o755)
sftp.makedirs("/srv/a/b/c")             # like mkdir -p
sftp.rmdir("/srv/new")                  # must be empty
sftp.rmtree("/srv/a")                   # like rm -r; never follows symlinks
```

Whole trees, with progress over the entire transfer:

```python
sftp.put_dir("build/", "/var/www/site")     # keeps permission bits and mtimes
sftp.get_dir("/var/www/site", "backup/")
```

On symlinks: `put_dir` and `get_dir` copy what a symlink to a file points to,
as a regular file. They skip symlinks to directories, so a loop can't send
them round in circles. `get_dir` refuses any name the server lists that
contains a path separator, or is `.`/`..`, so a hostile server can't make it
write outside the destination you chose.

### Metadata

```python
attr = sftp.stat("/etc/passwd")      # follows symlinks; lstat() does not
attr.st_size, attr.st_mode, attr.st_mtime, attr.st_uid, attr.st_gid
attr.is_file(), attr.is_dir(), attr.is_symlink()

sftp.chmod(path, 0o644)
sftp.chown(path, uid, gid)
sftp.utime(path, (atime, mtime))     # or utime(path) for "now"
sftp.truncate(path, 0)
sftp.symlink("target", "/path/to/link")   # same argument order as os.symlink
sftp.readlink("/path/to/link")
sftp.realpath(".")                   # the login directory
```

`st_mode` works with the standard `stat` module, e.g.
`stat.filemode(attr.st_mode)`.

### Server extensions

OpenSSH servers advertise extras that mfatsftp uses when they are present.
Check with the matching `supports_*()` method:

| Method | Needs | What it does |
|---|---|---|
| `posix_rename` / `atomic_rename` | `posix-rename@openssh.com` | rename over an existing file atomically (`atomic_rename` falls back without it) |
| `copy(src, dst)` | `copy-data` (OpenSSH 9.0+) | copy on the server, with no data crossing the network (falls back to read + write) |
| `hardlink(old, new)` | `hardlink@openssh.com` | create a hard link |
| `statvfs(path)` | `statvfs@openssh.com` | free and total space, like `os.statvfs` |
| `users_groups_by_id(uids, gids)` | `users-groups-by-id@openssh.com` (OpenSSH 9.1+) | user and group names on the server |
| (automatic) | `limits@openssh.com` | larger read/write chunks |

### Filenames that aren't UTF-8

SFTP filenames are raw bytes. mfatsftp decodes them as UTF-8 with
`surrogateescape`, the same convention Python uses for local filenames. A name
that isn't valid UTF-8 therefore still works when passed back to `stat`,
`open`, `remove`, and so on, and `os.fsencode(name)` gives its original bytes.

### Timeouts

By default a request waits as long as it takes; OpenSSH's own
`ServerAliveInterval` option detects a dead connection. To bound each reply:

```python
sftp.settimeout(30)        # raises mfatsftp.SFTPTimeoutError (a TimeoutError)
```

After a timeout, the request may still have happened on the server, so the
safe way to recover is to close the client and reconnect.

### Threads

A client may be shared between threads, and concurrent calls are pipelined
over the one connection. Don't use a single `SFTPFile` from two threads at
once.

## Errors

Failures raise `mfatsftp.SFTPError`, an `OSError` that carries the SFTP status
(`.code`, one of the `FX_*` constants in `mfatsftp.protocol`) and the closest
POSIX `.errno`. Common cases are also instances of the matching built-in
exception, so ordinary Python handling works:

| Server says | Raised as |
|---|---|
| no such file | `SFTPNoSuchFileError`, also a `FileNotFoundError` |
| permission denied | `SFTPPermissionError`, also a `PermissionError` |
| connection lost / closed | `SFTPConnectionError`, also a `ConnectionError` |
| no reply within `settimeout()` | `SFTPTimeoutError`, also a `TimeoutError` |
| anything else | `SFTPError` |

```python
try:
    sftp.remove("/tmp/maybe")
except FileNotFoundError:
    pass
```

Connection setup raises `PermissionError` (authentication failed, as
recognised from ssh's message), `TimeoutError` (`timeout` expired), or
`OSError` carrying ssh's error message (unknown host, refused connection, no
SFTP subsystem, …). A host that is empty or starts with `-` raises
`ValueError` before anything runs: ssh would read it as an option.

## Performance

SFTP is a request/reply protocol, so on a high-latency link the time it takes
is mostly round trips. mfatsftp hides as many of them as it can:

- **Pipelined transfers.** Reads and writes keep many requests in flight. The
  window starts at 16 and grows from the measured round-trip time toward
  about half a second of data in flight (the same approach FileZilla uses).
- **Server-sized chunks.** Chunk sizes come from `limits@openssh.com` (about
  255 KiB on OpenSSH, instead of a fixed 32 KiB). When a server sends back
  less than it advertised, the rest is fetched in one batch.
- **Configurable.** Set `sftp.max_concurrent_requests` (or pass
  `max_concurrent_prefetch_requests=` to `get`) to cap the requests in
  flight for a server that misbehaves with many.
- **Batched metadata.** `stat_many`, `listdir_many`, `mkdir_many`,
  `remove_many`, `rmdir_many`, `read_small_files` and `atomic_upload_many`
  send up to 100 requests at a time. `get_dir`, `put_dir` and `rmtree` are
  built on them, so a tree of many small files costs a few round trips per
  directory level instead of several per file.

For example, against OpenSSH's `sftp-server`, reading and then rewriting a
1 MiB file takes about 7 round trips instead of more than 64 one request at
a time. Deleting a tree of 81 directories takes about 14 instead of about 500.
The test suite checks numbers like these.

## Coming from paramiko

All of paramiko's file-operation methods exist here with the same names and
argument order: `open`/`file`
(including `bufsize`), `stat`/`lstat`, `listdir`/`listdir_attr`/
`listdir_iter`, `mkdir`/`rmdir`, `remove`/`unlink`, `rename`/`posix_rename`,
`chmod`/`chown`/`utime`/`truncate`, `symlink`/`readlink`, `normalize`,
`chdir`/`getcwd`, and `get`/`put`/`getfo`/`putfo` (including `prefetch` and
`max_concurrent_prefetch_requests`). `SFTPFile` matches too: line reading and
iteration, `readv`, `set_pipelined`, `prefetch`, `settimeout`, `chmod`/
`chown`/`utime`/`truncate`/`stat`. `SFTPAttributes` has the same `st_*`
fields, `attr` and `from_stat`.

Differences:

- You connect with `mfatsftp.connect(host)` instead of building a `Transport`,
  and there's no `from_transport`/`get_channel`. Each client runs its own
  `ssh`; OpenSSH's `ControlMaster`/`ControlPersist` options let several share
  one login.
- A few keyword names differ: `open(path, …)` (paramiko: `filename`),
  `rename`/`posix_rename(old, new)` (`oldpath`, `newpath`) and
  `symlink(target_path, link_path)` (`source`, `dest`). Positional calls work
  unchanged.
- `put`/`putfo` default to `confirm=False`; pass `confirm=True` to check the
  remote size afterwards. `get`/`put`/`getfo`/`putfo` return the number of
  bytes transferred.
- Unset attribute fields: paramiko uses `None` for `st_size`/`st_mode`,
  mfatsftp uses `0`.
- `SFTPFile.check()` (the `check-file` hash extension, which OpenSSH
  doesn't support) and the legacy `xreadlines()` aren't implemented.
- At EOF, the low-level `SFTPClient.read(handle, …)` returns `b""` rather
  than raising `EOFError` (`SFTPFile` behaves the same in both).
- Errors are `SFTPError` subclasses that are also `FileNotFoundError`,
  `PermissionError`, `ConnectionError` or `TimeoutError`, so existing
  `except IOError` / `except OSError` handlers still catch them.
- No SFTP server implementation.

## Development

See [CONTRIBUTING.md](https://github.com/mfat/mfatsftp/blob/main/CONTRIBUTING.md) for the full guide. In short:

```sh
pip install -e .[dev]
pytest           # unit tests + tests against the local OpenSSH sftp-server
ruff check src tests examples
mypy             # strict type check
pip install -r docs/requirements.txt && sphinx-build -b html docs docs/_build/html
```

Most tests run the client against the real OpenSSH `sftp-server` binary over
pipes. They are skipped if it's not installed (Debian/Ubuntu package:
`openssh-sftp-server`). To also test a real ssh login, point
`MFATSFTP_TEST_HOST` at a host you can reach without a password prompt:

```sh
MFATSFTP_TEST_HOST=localhost pytest tests/test_live_ssh.py
```

To report a security problem, see
[SECURITY.md](https://github.com/mfat/mfatsftp/blob/main/SECURITY.md).

The SFTP code is shared with [sshPilot](https://github.com/mfat/sshpilot)'s
file manager, where it was first developed.

## License

GNU General Public License v3.0 or later (GPL-3.0-or-later). See
[LICENSE](https://github.com/mfat/mfatsftp/blob/main/LICENSE).

> GPLv3 is a strong copyleft license. Software that distributes or links
> against `mfatsftp` must itself be GPL-compatible, which is a stronger
> requirement than paramiko's LGPL.
