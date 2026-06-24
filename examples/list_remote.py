"""List a remote directory over SFTP using the system OpenSSH.

Usage:
    python examples/list_remote.py user@host [remote_path]
"""

import sys

import mfatsftp


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    host = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "."

    with mfatsftp.connect(host) as sftp:
        target = sftp.realpath(path)
        print(f"{target}:")
        for attr in sorted(sftp.listdir_attr(target), key=lambda a: a.filename or ""):
            kind = "d" if attr.is_dir() else "-"
            print(f"  {kind} {attr.st_size:>12}  {attr.filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
