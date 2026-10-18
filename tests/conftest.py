import pytest

from tests.real_server import SFTP_SERVER, start_client, stop_client


@pytest.fixture
def client():
    """A client connected to a local OpenSSH ``sftp-server`` (skips if absent)."""
    if SFTP_SERVER is None:
        pytest.skip("OpenSSH sftp-server not installed")
    sftp, process, stdout = start_client()
    try:
        yield sftp
    finally:
        stop_client(sftp, process, stdout)
