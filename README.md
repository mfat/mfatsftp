# mfatsftp

**Pure-Python SFTP that speaks through your system OpenSSH.**

`mfatsftp` implements the SFTP v3 application protocol and uses the local `ssh`
binary as its transport (`ssh -s <host> sftp`). It does **not** reimplement SSH:
every cipher, key exchange (including post-quantum), `~/.ssh/config` setting,
`ProxyJump`, certificate, agent, FIDO/PIV key and askpass prompt that your
OpenSSH supports works automatically — and there is no crypto for this library
to maintain.

```python
import mfatsftp

with mfatsftp.connect("user@host") as sftp:
    for attr in sftp.listdir_attr("."):
        print(attr.filename, attr.st_size, attr.is_dir())
    sftp.get("/etc/hostname", "hostname.txt")
    sftp.put("local.txt", "/tmp/remote.txt", callback=lambda done, total: None)
    sftp.posix_rename("/tmp/remote.txt", "/tmp/final.txt")
```

## Requirements & scope (read this first)

- **Requires the `ssh` binary at runtime** (OpenSSH). This is the trade-off that
  buys you all of OpenSSH's transport/config for free; `mfatsftp` is not
  self-contained the way a pure-protocol stack (e.g. paramiko) is.
- **SFTP client only.** No remote `exec`, no port forwarding, no shell channels,
  no server side. If you use paramiko *purely* for SFTP, this is a drop-in-ish
  replacement; if you use the rest of paramiko, it is not.
- Primary target is **Linux/macOS**. Windows OpenSSH may work but is not yet a
  supported/tested configuration.

## Why

- **Config fidelity for free** — it runs `ssh -F <config> <host>`, so per-host
  `IdentityFile`, `Port`, `ProxyJump`, certificates, etc. resolve exactly as they
  do on your command line.
- **No crypto to maintain** — new ciphers/KEX algorithms and crypto CVEs are
  OpenSSH's problem, not this library's.
- **Original code, freely licensed.** The wire codec was written from the RFC
  (no paramiko code), so the project carries no inherited license constraints.
  It is released under the GPLv3 (see below).

## Authentication

Auth is delegated to `ssh`: the agent, `~/.ssh/config`, and the system askpass
(GUI passphrase prompt when there's no TTY) all just work. For **password auth**,
wrap your own command (e.g. with `sshpass`) and hand it to `from_command`:

```python
sftp = mfatsftp.from_command(
    ["sshpass", "-f", "/path/to/fifo", "ssh", "-F", "~/.ssh/config", "-s", "host", "sftp"]
)
```

## API

- `connect(host, *, port, identity, config, proxy_jump, known_hosts, ssh_options, batch_mode, ssh_path, extra_args, env, timeout) -> SFTPClient`
- `from_command(argv, *, env, timeout) -> SFTPClient` — full control of the ssh command
- `build_ssh_argv(host, **opts) -> list[str]` — compose the argv without spawning
- `SFTPClient` — `open`/`file`, `stat`/`lstat`, `listdir`/`listdir_attr`,
  `mkdir`/`makedirs`/`rmdir`, `remove`/`unlink`, `rename`/`posix_rename`, `chmod`,
  `realpath`/`normalize`, `get`/`put`/`get_dir`/`put_dir`, `close` (context manager)
- `SFTPFile`, `SFTPAttributes` (`st_*` fields), `SFTPError` (an `IOError` with `errno`)
- `SFTPClient.from_pipes(stdin, stdout, on_close=...)` — drive a custom transport / tests

## Status

Alpha (`0.1.x`). The protocol codec and client are extracted from a working
GTK file manager and exercised against an in-memory SFTP server in CI. Roadmap:
pipelined/windowed transfers for throughput, Windows support, an optional async
layer, and a documented paramiko-compatibility shim.

## License

GNU General Public License v3.0 or later (GPLv3+) — see [LICENSE](LICENSE).

> Note: GPLv3 is a strong copyleft license. Software that distributes or links
> against `mfatsftp` must itself be GPL-compatible — a stronger requirement than
> paramiko's LGPL. Keep this in mind when positioning it as a paramiko
> alternative for downstream projects.
