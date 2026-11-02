"""Back up folders.

python -m dedupvault init VAULT
python -m dedupvault backup VAULT FOLDER [label]
python -m dedupvault list VAULT
python -m dedupvault restore VAULT SNAPSHOT TARGET [path inside]
python -m dedupvault verify VAULT
python -m dedupvault diff VAULT A B
python -m dedupvault prune VAULT
"""
import sys
from datetime import datetime

from . import Vault, VaultError


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, path = argv[1], argv[2]
    try:
        if cmd == "init":
            Vault.init(path)
            print("vault created")
            return 0
        v = Vault(path)
        if cmd == "backup":
            s = v.backup(argv[3], argv[4] if len(argv) > 4 else "")
            print(f"{s['name']}: {len(s['files'])} files, {s['size'] / 1e6:.1f} MB, {s['added'] / 1e6:.2f} MB new")
            for x in s["skipped"]:
                print("  could not read:", x)
        elif cmd == "list":
            for s in v.snapshots():
                print(f"{s['name']}  {datetime.fromtimestamp(s['time']):%Y-%m-%d %H:%M}  {len(s['files'])} files  {s['size'] / 1e6:.1f} MB  {s['label']}")
            st = v.stats()
            print(f"{st['stored'] / 1e6:.1f} MB stored for {st['logical'] / 1e6:.1f} MB of backups, ratio {st['ratio']:.1f}")
        elif cmd == "restore":
            print(v.restore(argv[3], argv[4], argv[5] if len(argv) > 5 else None), "files restored")
        elif cmd == "verify":
            problems = v.verify()
            print("\n".join(problems) or "all chunks are intact")
            return 1 if problems else 0
        elif cmd == "diff":
            print(v.diff(argv[3], argv[4]))
        elif cmd == "prune":
            print("removed %d chunks, freed %d bytes" % v.prune())
        else:
            print(__doc__)
            return 2
    except (VaultError, OSError, IndexError) as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
