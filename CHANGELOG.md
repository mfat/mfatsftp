# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] — initial extraction
### Added
- Pure-Python SFTP v3 wire-protocol codec (`mfatsftp.protocol`).
- `SFTPClient` / `SFTPFile` over a stream pair, with a paramiko-compatible
  surface (`open`/`file`, `stat`/`lstat`, `listdir`/`listdir_attr`,
  `mkdir`/`makedirs`/`rmdir`, `remove`/`unlink`, `rename`/`posix_rename`,
  `chmod`, `realpath`/`normalize`, `get`/`put`/`get_dir`/`put_dir`).
- `connect()` / `from_command()` / `build_ssh_argv()` transport over the system
  OpenSSH (`ssh -s <host> sftp`), with clean subprocess teardown.
- In-memory SFTP server test fixture; protocol, client and transport test suites.

[Unreleased]: https://github.com/mfat/mfatsftp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mfat/mfatsftp/releases/tag/v0.1.0
