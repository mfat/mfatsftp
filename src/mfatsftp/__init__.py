"""mfatsftp — pure-Python SFTP that speaks through your system OpenSSH.

mfatsftp implements the SFTP v3 application protocol and uses the local ``ssh``
binary as its transport (``ssh -s <host> sftp``). It therefore inherits every
cipher, key exchange, ``~/.ssh/config`` setting, ProxyJump, certificate, agent
and askpass behaviour that your OpenSSH supports — with no crypto to maintain.

Requires the ``ssh`` binary at runtime. It is an SFTP client only (no exec, no
port forwarding, no server side).

Quickstart::

    import mfatsftp

    with mfatsftp.connect("user@host") as sftp:
        for attr in sftp.listdir_attr("."):
            print(attr.filename, attr.st_size)
        sftp.get("/etc/hostname", "hostname.txt")

See the README for the full guide and API reference.
"""

from .client import AtomicUploadItem, PipelinedWriter, ProgressCallback, SFTPClient, SFTPFile
from .protocol import (
    SFTPAttributes,
    SFTPConnectionError,
    SFTPError,
    SFTPNoSuchFileError,
    SFTPPermissionError,
    SFTPStatVFS,
)
from .transport import build_ssh_argv, connect, from_command

__all__ = [
    "connect",
    "from_command",
    "build_ssh_argv",
    "SFTPClient",
    "SFTPFile",
    "SFTPAttributes",
    "SFTPStatVFS",
    "SFTPError",
    "SFTPNoSuchFileError",
    "SFTPPermissionError",
    "SFTPConnectionError",
    "AtomicUploadItem",
    "PipelinedWriter",
    "ProgressCallback",
    "__version__",
]

__version__ = "0.2.0"
