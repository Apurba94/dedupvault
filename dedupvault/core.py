"""Deduplicating backups. Files are cut into chunks by their content, and each chunk is stored once.

Cutting at fixed sizes would make one inserted byte change every later chunk. With a rolling hash,
cut points follow the content, so only the chunks around an edit change, and a backup of a nearly
unchanged folder stores almost nothing new. Every chunk is verified by its SHA-256 on restore.
"""
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import zlib
from datetime import datetime, timedelta

MIN_CHUNK, AVG_BITS, MAX_CHUNK = 16 * 1024, 16, 256 * 1024
GEAR = [int.from_bytes(hashlib.sha256(bytes([i])).digest()[:8], "big") for i in range(256)]
MASK64 = (1 << 64) - 1


class VaultError(Exception):
    pass


def chunks(data, min_size=MIN_CHUNK, bits=AVG_BITS, max_size=MAX_CHUNK):
    """Yield byte strings cut at content-defined points, by a gear hash as in FastCDC."""
    mask = ((1 << bits) - 1) << (64 - bits)
    n = len(data)
    start = 0
    while start < n:
        end = min(n, start + max_size)
        i = min(n, start + min_size)
        h = 0
        cut = end
        while i < end:
            h = ((h << 1) + GEAR[data[i]]) & MASK64
            if not h & mask:
                cut = i + 1
                break
            i += 1
        yield data[start:cut]
        start = cut


class Vault:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.objects = os.path.join(path, "objects")
        self.snaps = os.path.join(path, "snapshots")
        self.clock = clock

    @classmethod
    def init(cls, path, **kw):
        if os.path.exists(os.path.join(path, "objects")):
            raise VaultError("a vault already exists here")
        os.makedirs(os.path.join(path, "objects"))
        os.makedirs(os.path.join(path, "snapshots"))
        return cls(path, **kw)

    def _chunk_path(self, h):
        return os.path.join(self.objects, h[:2], h)

    def _put(self, data):
        h = hashlib.sha256(data).hexdigest()
        path = self._chunk_path(h)
        if os.path.exists(path):
            return h, 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = zlib.compress(data, 6)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return h, len(body)

    def _get(self, h):
        try:
            with open(self._chunk_path(h), "rb") as f:
                data = zlib.decompress(f.read())
        except (OSError, zlib.error) as exc:
            raise VaultError(f"chunk {h[:12]} is missing or damaged: {exc}") from None
        if hashlib.sha256(data).hexdigest() != h:
            raise VaultError(f"chunk {h[:12]} is damaged")
        return data

    def backup(self, source, label=""):
        """Back up a folder. Unchanged data adds nothing. The snapshot is written last, so an
        interrupted backup leaves the earlier ones untouched."""
        source = os.path.abspath(source)
        if not os.path.isdir(source):
            raise VaultError(f"{source} is not a folder")
        files, added, total = [], 0, 0
        skipped = []
        vault = os.path.abspath(self.path)
        for base, dirs, names in os.walk(source):
            dirs[:] = sorted(d for d in dirs if os.path.abspath(os.path.join(base, d)) != vault)
            for name in sorted(names):
                full = os.path.join(base, name)
                rel = os.path.relpath(full, source).replace("\\", "/")
                try:
                    st = os.stat(full)
                    with open(full, "rb") as f:
                        data = f.read()
                except OSError as exc:
                    skipped.append(f"{rel}: {exc}")
                    continue
                ids = []
                for c in chunks(data):
                    h, n = self._put(c)
                    ids.append(h)
                    added += n
                total += len(data)
                files.append({"path": rel, "size": len(data), "mtime": st.st_mtime, "mode": stat.S_IMODE(st.st_mode), "chunks": ids})
        now = self.clock()
        name = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S") + f"-{os.urandom(2).hex()}"
        snap = {"name": name, "time": now, "source": source, "label": label, "files": files, "size": total, "added": added, "skipped": skipped}
        tmp = os.path.join(self.snaps, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(tmp, os.path.join(self.snaps, name + ".json"))
        return snap

    def snapshots(self):
        out = []
        for n in sorted(os.listdir(self.snaps)):
            if n.endswith(".json"):
                with open(os.path.join(self.snaps, n), encoding="utf-8") as f:
                    out.append(json.load(f))
        return sorted(out, key=lambda s: s["time"])

    def snapshot(self, name):
        hits = [s for s in self.snapshots() if s["name"].startswith(name)]
        if name == "latest" and self.snapshots():
            return self.snapshots()[-1]
        if len(hits) != 1:
            raise VaultError(f"no single snapshot matches {name}")
        return hits[0]

    def restore(self, name, target, only=None):
        """Restore into a folder. Paths are checked, so a damaged snapshot cannot write outside it."""
        snap = self.snapshot(name)
        target = os.path.abspath(target)
        done = 0
        for f in snap["files"]:
            if only and not (f["path"] == only or f["path"].startswith(only.rstrip("/") + "/")):
                continue
            full = os.path.abspath(os.path.join(target, f["path"]))
            if not full.startswith(target + os.sep):
                raise VaultError(f"unsafe path in snapshot: {f['path']}")
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".restore-tmp"
            try:
                with open(tmp, "wb") as out:
                    for h in f["chunks"]:
                        out.write(self._get(h))
            except VaultError:
                os.remove(tmp)
                raise
            os.replace(tmp, full)
            os.utime(full, (f["mtime"], f["mtime"]))
            done += 1
        return done

    def verify(self):
        """Re-read every chunk that any snapshot needs. Returns a list of problems."""
        problems = []
        needed = set()
        for s in self.snapshots():
            for f in s["files"]:
                needed.update(f["chunks"])
        for h in sorted(needed):
            try:
                self._get(h)
            except VaultError as exc:
                problems.append(str(exc))
        return problems

    def diff(self, a, b):
        """What changed between two snapshots."""
        fa = {f["path"]: f["chunks"] for f in self.snapshot(a)["files"]}
        fb = {f["path"]: f["chunks"] for f in self.snapshot(b)["files"]}
        return {"added": sorted(set(fb) - set(fa)), "removed": sorted(set(fa) - set(fb)), "changed": sorted(p for p in fa.keys() & fb.keys() if fa[p] != fb[p])}

    def forget(self, keep_last=7, keep_daily=7, keep_weekly=4, keep_monthly=12):
        """Thin out old snapshots: keep the latest few, then one for each recent day, week and month."""
        snaps = self.snapshots()
        keep = {s["name"] for s in snaps[-keep_last:]} if keep_last else set()
        rules = (("%Y-%m-%d", keep_daily), ("%G-%V", keep_weekly), ("%Y-%m", keep_monthly))
        for fmt, count in rules:
            seen = set()
            for s in reversed(snaps):
                k = datetime.fromtimestamp(s["time"]).strftime(fmt)
                if k not in seen and len(seen) < count:
                    seen.add(k)
                    keep.add(s["name"])
        gone = [s["name"] for s in snaps if s["name"] not in keep]
        for n in gone:
            os.remove(os.path.join(self.snaps, n + ".json"))
        return gone

    def prune(self):
        """Delete chunks that no snapshot uses. Returns (chunks removed, bytes freed)."""
        needed = set()
        for s in self.snapshots():
            for f in s["files"]:
                needed.update(f["chunks"])
        count = freed = 0
        for d in os.listdir(self.objects):
            folder = os.path.join(self.objects, d)
            for h in os.listdir(folder):
                if h not in needed:
                    p = os.path.join(folder, h)
                    freed += os.path.getsize(p)
                    os.remove(p)
                    count += 1
        return count, freed

    def stats(self):
        stored = sum(os.path.getsize(os.path.join(b, f)) for b, _, fs in os.walk(self.objects) for f in fs)
        snaps = self.snapshots()
        logical = sum(s["size"] for s in snaps)
        return {"snapshots": len(snaps), "logical": logical, "stored": stored, "ratio": logical / stored if stored else 0.0}
