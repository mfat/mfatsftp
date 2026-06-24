"""Run the in-memory fake SFTP server over stdin/stdout.

Used by the transport integration test as the child process that
``mfatsftp.from_command`` spawns in place of a real ``ssh -s host sftp``.
"""

import sys

from mfatsftp import protocol as proto
from tests.fake_server import _FakeSFTPServer


def main() -> None:
    server = _FakeSFTPServer(proto, sys.stdin.buffer, sys.stdout.buffer)
    server.run()  # synchronous: serve until the client closes the pipe


if __name__ == "__main__":
    main()
