"""Transport: launch the system OpenSSH as an SFTP subsystem and wrap the pipes.

``connect()`` builds an ``ssh … -s <host> sftp`` command, spawns it, runs the
SFTP handshake, and returns a connected :class:`~mfatsftp.client.SFTPClient`
that owns the subprocess (closing the client stops it).

**This library requires the ``ssh`` binary at runtime.** All transport, crypto,
host-key checking, ``~/.ssh/config`` resolution, ProxyJump, agent, askpass and
auth are handled by OpenSSH — mfatsftp only speaks the SFTP application layer.
Authentication is therefore ssh's job (agent / askpass / config); for a password
you must supply your own wrapped command via :func:`from_command`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import List, Optional, Sequence

from .client import SFTPClient

logger = logging.getLogger(__name__)


def build_ssh_argv(
    host: str,
    *,
    port: Optional[int] = None,
    identity: Optional[str] = None,
    config: Optional[str] = None,
    proxy_jump: Optional[str] = None,
    known_hosts: Optional[str] = None,
    ssh_options: Optional[Sequence[str]] = None,
    batch_mode: bool = False,
    ssh_path: str = "ssh",
    extra_args: Optional[Sequence[str]] = None,
) -> List[str]:
    """Compose a minimal ``ssh`` argv that requests the ``sftp`` subsystem.

    Per-host details you don't pass here are resolved by ssh from
    ``~/.ssh/config`` — that file is the source of truth, exactly as on the
    command line.

    :param host: ``"user@hostname"``, ``"hostname"`` or a ``~/.ssh/config``
        alias.
    :param port: ``-p``.
    :param identity: private key file, ``-i`` (``~`` is expanded).
    :param config: ssh config file, ``-F`` (``~`` is expanded).
    :param proxy_jump: jump host(s), ``-J``.
    :param known_hosts: ``-o UserKnownHostsFile=…`` (``~`` is expanded).
    :param ssh_options: passed verbatim as ``-o`` entries, e.g.
        ``["StrictHostKeyChecking=accept-new", "ConnectTimeout=10"]``.
    :param batch_mode: ``-o BatchMode=yes``: never prompt, fail instead
        (recommended for unattended scripts).
    :param ssh_path: the ssh executable.
    :param extra_args: extra arguments inserted before ``-s`` (e.g. ``["-v"]``).
    """
    argv: List[str] = [ssh_path]
    if config:
        argv += ["-F", os.path.expanduser(config)]
    if port:
        argv += ["-p", str(int(port))]
    if identity:
        argv += ["-i", os.path.expanduser(identity)]
    if proxy_jump:
        argv += ["-J", proxy_jump]
    if known_hosts:
        argv += ["-o", f"UserKnownHostsFile={os.path.expanduser(known_hosts)}"]
    if batch_mode:
        argv += ["-o", "BatchMode=yes"]
    for opt in ssh_options or ():
        argv += ["-o", opt]
    for arg in extra_args or ():
        argv.append(arg)
    # ``-s <host> sftp`` requests the "sftp" subsystem after the host.
    argv += ["-s", host, "sftp"]
    return argv


def _terminate_proc(proc: subprocess.Popen) -> None:
    """Stop the ssh subprocess (EOFs its pipes so the reader/stderr threads
    unblock). Idempotent and quiet."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:  # pragma: no cover - best effort
        pass


class _ProcessSFTPClient(SFTPClient):
    """An :class:`SFTPClient` that owns the ssh subprocess it talks to.

    Overrides ``close()`` to tear the subprocess down cleanly: terminate it so
    the reader/stderr threads EOF, join the reader, then reap and close the
    process's own pipe objects (so their finalizers can't double-close fds).
    """

    def __init__(self, proc: subprocess.Popen, stderr_lines: List[str]) -> None:
        super().__init__(proc.stdin, proc.stdout, on_close=lambda: _terminate_proc(proc))
        self._proc = proc
        self._stderr_lines = stderr_lines

    def close(self) -> None:
        _terminate_proc(self._proc)
        super().close()
        try:
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
            except Exception:  # pragma: no cover - best effort
                pass
        for attr in ("stdout", "stderr", "stdin"):
            stream = getattr(self._proc, attr, None)
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # pragma: no cover - best effort
                pass


def _classify_handshake_failure(text: str, exc: Exception) -> Exception:
    text = (text or "").strip()
    lowered = text.lower()
    if "permission denied" in lowered or "password" in lowered or "publickey" in lowered:
        return PermissionError(text or "Authentication failed")
    if text:
        return IOError(text)
    return exc


def from_command(
    argv: Sequence[str],
    *,
    env: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> SFTPClient:
    """Spawn *argv* (which must speak the SFTP subsystem on stdin/stdout) and
    return a connected client. Use this when you need full control of the ssh
    command (custom flags, an ``sshpass`` wrapper for password auth, etc.).

    :param env: extra environment variables, merged over ``os.environ``.
    :param timeout: seconds allowed for login plus the SFTP handshake; the
        process is stopped and :class:`TimeoutError` raised if it takes longer.
        ``None`` (the default) waits indefinitely, which suits interactive
        passphrase prompts. For network timeouts prefer ssh's own
        ``ConnectTimeout`` option.
    :raises PermissionError: authentication failed.
    :raises TimeoutError: *timeout* expired.
    :raises OSError: ssh failed for another reason (the message carries
        ssh's error output), or *argv* could not be started.
    """
    argv = list(argv)
    logger.debug("mfatsftp launching: %s", " ".join(argv))
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})},
        bufsize=0,
    )

    # Drain stderr continuously so verbose ssh output can't fill the pipe buffer
    # and block ssh mid-session; keep the lines for failure classification.
    stderr_lines: List[str] = []

    def _drain() -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    stderr_lines.append(line)
                    logger.debug("ssh stderr: %s", line)
        except Exception:  # pragma: no cover - best effort
            pass

    stderr_thread = threading.Thread(target=_drain, name="sftp-stderr", daemon=True)
    stderr_thread.start()

    client = _ProcessSFTPClient(proc, stderr_lines)
    timed_out = threading.Event()
    timer = None
    if timeout is not None:

        def _expire() -> None:
            timed_out.set()
            _terminate_proc(proc)

        timer = threading.Timer(timeout, _expire)
        timer.daemon = True
        timer.start()
    try:
        client.start()
    except Exception as exc:
        # Handshake never completed — auth failed, no sftp subsystem on the
        # host, or the timeout stopped it.
        try:
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - defensive
            proc.kill()
        stderr_thread.join(timeout=0.5)
        client.close()
        if timed_out.is_set():
            raise TimeoutError(
                f"SFTP connection not established within {timeout} seconds"
            ) from exc
        error = _classify_handshake_failure("\n".join(stderr_lines), exc)
        if error is exc:
            raise
        raise error from exc
    finally:
        if timer is not None:
            timer.cancel()
    return client


def connect(
    host: str,
    *,
    port: Optional[int] = None,
    identity: Optional[str] = None,
    config: Optional[str] = None,
    proxy_jump: Optional[str] = None,
    known_hosts: Optional[str] = None,
    ssh_options: Optional[Sequence[str]] = None,
    batch_mode: bool = False,
    ssh_path: str = "ssh",
    extra_args: Optional[Sequence[str]] = None,
    env: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> SFTPClient:
    """Connect to *host* over SFTP via the system OpenSSH.

    *host* may be ``"user@hostname"`` or a ``~/.ssh/config`` alias. See
    :func:`build_ssh_argv` for the connection options and :func:`from_command`
    for *env*, *timeout* and the exceptions raised. Auth is delegated to ssh
    (agent / askpass / config); for password auth, wrap your own command and
    use :func:`from_command`.

    Use the returned client as a context manager so the ssh process is
    always stopped::

        with mfatsftp.connect("backup@nas", batch_mode=True) as sftp:
            sftp.put_dir("site/", "/var/www/site")
    """
    argv = build_ssh_argv(
        host,
        port=port,
        identity=identity,
        config=config,
        proxy_jump=proxy_jump,
        known_hosts=known_hosts,
        ssh_options=ssh_options,
        batch_mode=batch_mode,
        ssh_path=ssh_path,
        extra_args=extra_args,
    )
    return from_command(argv, env=env, timeout=timeout)
