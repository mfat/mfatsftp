"""Upload or download a file or directory over SFTP, with a progress readout.

Usage:
    python examples/transfer.py put user@host LOCAL REMOTE
    python examples/transfer.py get user@host REMOTE LOCAL

Directories are copied recursively. Uses your ssh config, agent and keys.
"""

import os
import sys
import time

import mfatsftp


def _progress():
    started = time.monotonic()

    def report(done: int, total: int) -> None:
        elapsed = max(time.monotonic() - started, 1e-6)
        percent = f"{done * 100 // total:3d}%" if total else "    "
        rate = done / elapsed / (1024 * 1024)
        print(f"\r{percent} {done:>14,} bytes  {rate:7.1f} MiB/s", end="", flush=True)

    return report


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] not in ("put", "get"):
        print(__doc__)
        return 2
    action, host, source, destination = sys.argv[1:]

    try:
        with mfatsftp.connect(host) as sftp:
            if action == "put":
                if os.path.isdir(source):
                    sftp.put_dir(source, destination, callback=_progress())
                else:
                    sftp.put(source, destination, callback=_progress())
            elif sftp.stat(source).is_dir():
                sftp.get_dir(source, destination, callback=_progress())
            else:
                sftp.get(source, destination, callback=_progress())
    except OSError as exc:
        print(f"\nfailed: {exc}", file=sys.stderr)
        return 1
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
