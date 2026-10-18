# Security policy

## Supported versions

Security fixes go into the latest release. mfatsftp is pre-1.0, so please
upgrade to the newest version before reporting.

## Reporting a vulnerability

Please **don't** open a public issue. Report it privately through GitHub's
[private vulnerability reporting](https://github.com/mfat/mfatsftp/security/advisories/new).
Include what an attacker can do, how to reproduce it, and the affected
versions. You should get a reply within a week. A fix and an advisory follow
once the problem is confirmed.

## Scope

mfatsftp hands the SSH connection itself to the system's OpenSSH. Encryption,
host-key verification, authentication and `~/.ssh/config` handling are
OpenSSH's, so report vulnerabilities in those to the
[OpenSSH project](https://www.openssh.com/security.html).

In scope for mfatsftp:

- **Building the ssh command.** `connect()` refuses host names starting with
  `-`, which ssh would otherwise read as options.
- **Handling what the server sends.** `get_dir` refuses listed names that
  would write outside the destination. `rmtree`, `walk` and the tree
  transfers never follow symlinks into directories. Malformed packets must
  not crash the client in ways that harm the caller.
- **Local file handling** in `get`, `get_dir` and `put_dir`.

## Guidance for applications

- Use `batch_mode=True` for unattended use, so ssh fails instead of
  prompting.
- Keep OpenSSH's host-key checking on. `StrictHostKeyChecking=accept-new` is
  reasonable for first contact; `StrictHostKeyChecking=no` is not.
- Treat remote file names as untrusted input in your own code too.
