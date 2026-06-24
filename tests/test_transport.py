"""Tests for the self-contained ssh argv builder."""

import os

from mfatsftp import build_ssh_argv


def test_minimal_argv_requests_sftp_subsystem():
    argv = build_ssh_argv("user@host")
    assert argv == ["ssh", "-s", "user@host", "sftp"]


def test_argv_includes_options_in_order():
    argv = build_ssh_argv(
        "myalias",
        port=2222,
        identity="~/.ssh/id_ed25519",
        config="~/.ssh/config",
        proxy_jump="bastion",
        known_hosts="~/.ssh/known_hosts",
        ssh_options=["StrictHostKeyChecking=accept-new", "ServerAliveInterval=15"],
        batch_mode=True,
        ssh_path="/usr/bin/ssh",
    )
    assert argv[0] == "/usr/bin/ssh"
    # config first, then port/identity/proxy
    assert "-F" in argv and argv[argv.index("-F") + 1] == os.path.expanduser("~/.ssh/config")
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"
    assert "-i" in argv and argv[argv.index("-i") + 1] == os.path.expanduser("~/.ssh/id_ed25519")
    assert "-J" in argv and argv[argv.index("-J") + 1] == "bastion"
    assert "UserKnownHostsFile=" + os.path.expanduser("~/.ssh/known_hosts") in argv
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "ServerAliveInterval=15" in argv
    # subsystem request is always last: -s <host> sftp
    assert argv[-3:] == ["-s", "myalias", "sftp"]


def test_extra_args_passed_through_before_subsystem():
    argv = build_ssh_argv("host", extra_args=["-v"])
    assert "-v" in argv
    assert argv.index("-v") < argv.index("-s")
