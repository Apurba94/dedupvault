import filecmp
import json
import os
import random
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from dedupvault import Vault, VaultError, chunks


def blob(n, seed=0):
    return random.Random(seed).randbytes(n)


class TestChunks(unittest.TestCase):
    def test_pieces_rebuild_the_data(self):
        for n in (0, 1, 1000, 500000):
            d = blob(n)
            self.assertEqual(b"".join(chunks(d)), d)

    def test_sizes_stay_within_limits(self):
        parts = list(chunks(blob(1500000)))
        self.assertTrue(all(len(p) <= 256 * 1024 for p in parts))
        self.assertTrue(all(len(p) >= 16 * 1024 for p in parts[:-1]))
        self.assertGreater(len(parts), 5)

    def test_same_data_same_cuts(self):
        d = blob(300000)
        self.assertEqual(list(chunks(d)), list(chunks(d)))

    def test_an_insertion_changes_only_nearby_chunks(self):
        d = blob(1500000)
        edited = d[:700000] + b"NEW BYTES" + d[700000:]
        a, b = set(chunks(d)), list(chunks(edited))
        new = [c for c in b if c not in a]
        self.assertLessEqual(len(new), 3)
        self.assertGreater(len(b), 8)

    def test_fixed_size_chunking_would_have_lost_everything(self):
        d = blob(800000)
        e = b"x" + d
        size = 64 * 1024
        a = {d[i:i + size] for i in range(0, len(d), size)}
        b = [e[i:i + size] for i in range(0, len(e), size)]
        self.assertEqual(sum(c in a for c in b), 0)

    def test_compressible_and_repeating_data(self):
        d = b"\0" * 1000000
        self.assertEqual(b"".join(chunks(d)), d)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, 12).timestamp()

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "src")
        os.makedirs(os.path.join(self.src, "sub"))
        self.write("a.bin", blob(300000, 1))
        self.write("sub/b.txt", b"hello world\n" * 1000)
        self.write("sub/empty", b"")
        self.write("বাংলা.txt", "পানি".encode())
        self.clock = Clock()
        self.v = Vault.init(os.path.join(self.dir, "vault"), clock=self.clock)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, data):
        with open(os.path.join(self.src, rel), "wb") as f:
            f.write(data)

    def same(self, a, b):
        c = filecmp.dircmp(a, b)
        self.assertEqual((c.left_only, c.right_only, c.diff_files), ([], [], []))
        for s in c.common_dirs:
            self.same(os.path.join(a, s), os.path.join(b, s))


class TestBackup(Base):
    def test_restore_is_identical(self):
        s = self.v.backup(self.src)
        out = os.path.join(self.dir, "out")
        self.assertEqual(self.v.restore(s["name"], out), 4)
        self.same(self.src, out)

    def test_second_backup_adds_nothing(self):
        self.v.backup(self.src)
        self.assertEqual(self.v.backup(self.src)["added"], 0)

    def test_small_edit_adds_little(self):
        first = self.v.backup(self.src)
        d = blob(300000, 1)
        self.write("a.bin", d[:100000] + b"edit" + d[100000:])
        self.assertLess(self.v.backup(self.src)["added"], first["added"] * 0.5)

    def test_identical_files_are_stored_once(self):
        first = self.v.backup(self.src)["added"]
        self.write("copy.bin", blob(300000, 1))
        self.assertLess(self.v.backup(self.src)["added"], first * 0.02)

    def test_old_versions_stay_available(self):
        s = self.v.backup(self.src)
        self.write("sub/b.txt", b"changed")
        self.v.backup(self.src)
        out = os.path.join(self.dir, "out")
        self.v.restore(s["name"], out)
        with open(os.path.join(out, "sub", "b.txt"), "rb") as f:
            self.assertEqual(f.read(), b"hello world\n" * 1000)

    def test_restore_one_file(self):
        s = self.v.backup(self.src)
        out = os.path.join(self.dir, "out")
        self.assertEqual(self.v.restore(s["name"], out, "sub/b.txt"), 1)
        self.assertFalse(os.path.exists(os.path.join(out, "a.bin")))

    def test_times_are_kept(self):
        os.utime(os.path.join(self.src, "a.bin"), (1000000, 1000000))
        s = self.v.backup(self.src)
        out = os.path.join(self.dir, "out")
        self.v.restore(s["name"], out)
        self.assertEqual(os.path.getmtime(os.path.join(out, "a.bin")), 1000000)

    def test_vault_inside_the_source_is_not_backed_up(self):
        v2 = Vault.init(os.path.join(self.src, "vault"))
        s = v2.backup(self.src)
        self.assertFalse(any(f["path"].startswith("vault/") for f in s["files"]))

    def test_bad_source(self):
        with self.assertRaises(VaultError):
            self.v.backup(os.path.join(self.dir, "nope"))

    def test_init_twice(self):
        with self.assertRaises(VaultError):
            Vault.init(self.v.path)

    def test_diff(self):
        a = self.v.backup(self.src)["name"]
        self.write("sub/b.txt", b"changed")
        self.write("new.txt", b"n")
        os.remove(os.path.join(self.src, "sub", "empty"))
        b = self.v.backup(self.src)["name"]
        self.assertEqual(self.v.diff(a, b), {"added": ["new.txt"], "removed": ["sub/empty"], "changed": ["sub/b.txt"]})

    def test_savings_are_reported(self):
        for _ in range(5):
            self.v.backup(self.src)
        self.assertGreater(self.v.stats()["ratio"], 4)


class TestSafety(Base):
    def chunk_files(self):
        return [os.path.join(b, f) for b, _, fs in os.walk(self.v.objects) for f in fs]

    def test_clean_vault_verifies(self):
        self.v.backup(self.src)
        self.assertEqual(self.v.verify(), [])

    def test_flipped_bit_is_caught(self):
        s = self.v.backup(self.src)
        p = sorted(self.chunk_files(), key=os.path.getsize)[-1]
        with open(p, "r+b") as f:
            f.seek(50)
            b = f.read(1)
            f.seek(50)
            f.write(bytes([b[0] ^ 1]))
        self.assertTrue(self.v.verify())
        with self.assertRaises(VaultError):
            self.v.restore(s["name"], os.path.join(self.dir, "out"))

    def test_missing_chunk_is_caught(self):
        self.v.backup(self.src)
        os.remove(self.chunk_files()[0])
        self.assertIn("missing", self.v.verify()[0])

    def test_interrupted_restore_leaves_no_half_file(self):
        s = self.v.backup(self.src)
        for p in self.chunk_files():
            os.remove(p)
        out = os.path.join(self.dir, "out")
        with self.assertRaises(VaultError):
            self.v.restore(s["name"], out)
        self.assertEqual([f for _, _, fs in os.walk(out) for f in fs], [])

    def test_a_hostile_path_cannot_escape_the_target(self):
        s = self.v.backup(self.src)
        p = os.path.join(self.v.snaps, s["name"] + ".json")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        data["files"][0]["path"] = "../../evil.txt"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with self.assertRaises(VaultError):
            self.v.restore(s["name"], os.path.join(self.dir, "a", "b"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evil.txt")))


class TestRetention(Base):
    def run_days(self, n):
        for _ in range(n):
            self.v.backup(self.src)
            self.clock.t += 86400

    def test_old_snapshots_thin_out(self):
        self.run_days(120)
        gone = self.v.forget(keep_last=3, keep_daily=7, keep_weekly=4, keep_monthly=3)
        self.assertGreater(len(gone), 90)
        left = self.v.snapshots()
        self.assertEqual(left[-1]["time"], max(s["time"] for s in left))
        self.assertLess(len(left), 20)

    def test_latest_is_always_kept(self):
        self.run_days(5)
        last = self.v.snapshots()[-1]["name"]
        self.v.forget(keep_last=1, keep_daily=0, keep_weekly=0, keep_monthly=0)
        self.assertEqual([s["name"] for s in self.v.snapshots()], [last])

    def test_prune_keeps_what_is_needed(self):
        first = self.v.backup(self.src)
        self.write("a.bin", blob(300000, 2))
        keep = self.v.backup(self.src)
        os.remove(os.path.join(self.v.snaps, first["name"] + ".json"))
        count, freed = self.v.prune()
        self.assertGreater(count, 0)
        self.assertGreater(freed, 0)
        self.assertEqual(self.v.verify(), [])
        out = os.path.join(self.dir, "out")
        self.v.restore(keep["name"], out)
        self.same(self.src, out)


if __name__ == "__main__":
    unittest.main()
