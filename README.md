# DedupVault

Back up a folder as often as you like, for almost no extra space. Each backup looks like a full copy, but identical data is stored only once. It uses only the Python standard library.

```
python -m dedupvault init E:/backups
python -m dedupvault backup E:/backups "D:/Thesis" "before submission"
python -m dedupvault list E:/backups
python -m dedupvault restore E:/backups latest "D:/Restored"
python -m dedupvault verify E:/backups
```

## How it works

**Content-defined chunks.** Files are cut where a rolling hash of the content says so, not at fixed sizes. Insert one byte at the start of a file, and fixed-size chunks would all change, while here only the chunks near the edit do. The method is a gear hash, as in FastCDC.

**Each chunk is stored once,** compressed, under its SHA-256 hash, so copies of a file, or small edits to a large file, add almost nothing.

**Snapshots are plain JSON lists** of files and their chunks, and a snapshot is written only after its data is safe, so an interrupted backup leaves earlier ones intact.

## Keeping backups safe

- Every chunk is re-checked against its hash on restore, and `verify` checks the whole vault
- Restores write to a temporary name first, so a failure leaves no half-written file
- Restore paths are checked, so a damaged snapshot cannot write outside the target folder
- Files that could not be read are listed in the backup report, not skipped without notice

## Keeping old backups in check

`forget` keeps the latest few, then one for each recent day, week and month. `prune` then frees chunks that nothing uses.

## How it is tested

Tests check that a restore is byte-identical, that a second backup adds nothing, and that an edit in the middle of a 1.5 MB file changes at most 3 chunks. A flipped bit, a missing chunk and a hostile path in a snapshot must all be caught. Retention is run over 120 simulated days.

## Limits

**A backup on the same disk is not a backup,** so use another drive, and test a restore now and then. There is no encryption, so protect the vault if the data is private. Files are read fully into memory, so very large files need plenty of RAM. Symbolic links are followed rather than stored. It is written in Python, so it is slower than restic or Borg, which are the better choice for large or critical data.

## Tests

```
python -m unittest discover -s tests
```

MIT licence.
