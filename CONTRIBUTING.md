# Contributing

Thanks for helping improve mfatsftp. Bug reports, fixes and documentation
improvements are all welcome.

## Setting up

```sh
git clone https://github.com/mfat/mfatsftp
cd mfatsftp
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Most tests drive the real OpenSSH `sftp-server` binary over pipes. Install it
if it's missing:

- Debian/Ubuntu: `sudo apt install openssh-sftp-server`
- Fedora: `sudo dnf install openssh-server`
- macOS: already present at `/usr/libexec/sftp-server`

Tests that need it are skipped, not failed, when it isn't there.

## Checks

These are the checks CI runs. Please run them before opening a pull request:

```sh
pytest                                   # tests
ruff check src tests examples            # lint
mypy --strict src/mfatsftp               # types (the package ships py.typed)
sphinx-build -W -b html docs docs/_build/html   # docs (pip install -r docs/requirements.txt)
```

To test a real ssh login as well, point `MFATSFTP_TEST_HOST` at a host you
can reach without a password prompt (for example `localhost` with your key in
`~/.ssh/authorized_keys`):

```sh
MFATSFTP_TEST_HOST=localhost pytest tests/test_live_ssh.py
```

## Where things are

| Path | What |
|---|---|
| `src/mfatsftp/protocol.py` | SFTP v3 wire codec: pure functions, no I/O |
| `src/mfatsftp/client.py` | `SFTPClient`, `SFTPFile`, `PipelinedWriter`: requests, pipelining, transfers |
| `src/mfatsftp/transport.py` | `connect` / `from_command`: running `ssh -s host sftp` |
| `tests/real_server.py` | helpers for tests against `sftp-server`, including a simulated slow link |
| `tests/fake_server.py` | in-memory SFTP server for protocol-level tests |
| `docs/` | Sphinx site; `docs/api.md` is the hand-written reference |

## Guidelines

- **Tests:** add a test for every fix or feature. Prefer the real-server
  fixture (`client` in `tests/conftest.py`) over the fake server.
- **Speed:** if you change how requests are sent, check the round trips with
  the simulated slow link (`start_client(delay)`), as in
  `tests/test_large_files.py`.
- **Docs:** public methods need a docstring and an entry in `docs/api.md`.
  `tests/test_docs.py` fails otherwise. User-visible changes go under
  "Unreleased" in `CHANGELOG.md`.
- **Dependencies:** the package has no runtime dependencies beyond the
  standard library, and it should stay that way.
- **Compatibility:** Python 3.9+ on Linux and macOS.

## Releasing

1. Move the "Unreleased" changelog entries under a new `## [X.Y.Z] - date`
   heading and set the same version in `pyproject.toml` and
   `src/mfatsftp/__init__.py`.
2. Commit, push, and wait for CI to pass.
3. Tag and push: `git tag -a vX.Y.Z -m "mfatsftp X.Y.Z" && git push origin vX.Y.Z`.

The release workflow then builds the package, creates the GitHub release with
notes taken from the changelog, and publishes to PyPI (a separate job that runs
while the repository variable `PYPI_PUBLISH` is `true`). To rerun it for an
existing tag: `gh workflow run release.yml -f tag=vX.Y.Z`.

## Reporting bugs

Open an issue with your Python version, OS, `ssh -V` output, the server
software if known, and a minimal script that shows the problem. For security
problems, see [SECURITY.md](https://github.com/mfat/mfatsftp/blob/main/SECURITY.md) instead.

By contributing you agree that your contributions are licensed under the
project's license (MIT).
