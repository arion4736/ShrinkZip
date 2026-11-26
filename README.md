# ⚡ ShrinkZIP

**Extract big archives when you don't have room for both the archive and its contents.**

ShrinkZIP reads an archive from its tail inward and **cuts the archive file down as each part is safely extracted**, so the space it frees is reused by the extracted files. You need room for the extracted data, not for the archive *plus* the extracted data.

[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](LICENSE)

## Supported formats

| Format | In place? | Space is freed in steps of | Notes |
|---|---|---|---|
| **ZIP / ZIP64** (stored, deflate, bzip2, lzma) | ✅ | one entry | Other methods (Deflate64, PPMd, ...) use 7-Zip, archive kept |
| **tar** (uncompressed) | ✅ | one entry | Hard links, sparse files, devices refused (use Keep Archive) |
| **7z** | ✅ | one **solid block** | Default 7-Zip settings can put everything in one block, which saves nothing (the app tells you before starting). BCJ2 and encrypted archives are refused |
| **RAR 5** (non-solid, single volume) | ✅ | one file | Decoded by 7-Zip (`7z.exe` needed) |
| **Split archives** (`.001`, `.002`, ...) of ZIP, tar or 7z | ✅ | as the format above | Each volume is deleted as soon as it is empty. Any volume's name works |
| RAR 4, solid RAR, RAR volume sets (`.part1.rar`) | ❌ standard only | | Extracted normally, archive kept |
| `.tar.gz`, `.tar.bz2`, `.tar.xz` | ❌ standard only | | Compressed streams cannot be cut from the end |
| Password-protected archives | ❌ | | Refused before anything is touched |

"Standard only" means a normal extraction: the archive is kept (or deleted only after a fully successful run, if you ask for that) and you need room for all extracted files.

## How it works

1. The format is split into **units** that can be decoded on their own: a ZIP/tar entry, a 7z solid block, a RAR file.
2. For formats with an index at the end (ZIP, 7z) that index is **saved to `<archive>.shrinkzip` before the first byte is cut**.
3. Units are extracted from the **highest offset down**. Each extracted file is **fsync'd to disk**; RAR files are also re-checked against the CRC-32 stored in the archive.
4. Only then is the archive **truncated** to the start of the extracted units (once at least 32 MiB can be reclaimed). For split archives, wholly emptied volumes are deleted.
5. At the end the leftover stub and the side file are removed.

**Stop, error, disk full or crash:** the archive is always either complete or a valid prefix of it, and extracted files are durable before their bytes are cut.

| Format | After *Stop* / error | After a hard crash (power loss, `kill -9`) |
|---|---|---|
| ZIP | Rebuilt into a normal, valid ZIP with the entries left | Run ShrinkZIP again: it rebuilds the index from the side file and continues |
| tar | Still a valid tar with the entries left | Run again |
| RAR | A valid RAR prefix (only the end marker is missing) | Run again |
| 7z | **Resumable by ShrinkZIP only**: keep the archive *and* its `.shrinkzip` file | Run again |

Never delete a `.shrinkzip` file while an extraction is unfinished.

### How much space do you need?

An archive unit is only freed **after it is completely extracted**, so:

> **peak extra space ≈ the largest unit (biggest file, or biggest solid block) + ~32 MiB**, plus the 1 GB safety margin the app adds.

A ZIP holding one 40 GB file, or a 7z with one giant solid block, gets **no saving at all**. The preflight planner replays the exact extraction order and cutting rule, so its number is the real peak.

## Safety rules

* Archives with unsafe paths (`..`, absolute paths, drive letters, Windows device names), duplicate paths, overlapping entries, or unsupported entry types are **refused before anything changes**.
* Files are never written through a symlink that points out of the destination.
* A corrupt member stops the run before its bytes are cut. A failed 7-Zip run never modifies or deletes the archive.
* **Delete archive** is **off by default** and only applies to Keep Archive mode.
* The 7z header is verified (checksum + a dry-run re-open) **before** the first cut.

## Measured results

Linux, Python 3.12, local SSD, 60 files × 5 MB of incompressible data (300 MB archive). "Predicted" is the planner's answer, "measured" is the highest extra disk use sampled every 5 ms during the real run.

| Archive | Predicted peak | Measured peak | Result |
|---|---|---|---|
| ZIP (stored) | 35 MB | 35 MB | ✅ files byte-identical |
| tar | 35 MB | 35 MB | ✅ |
| 7z, 15 MB solid blocks | 45 MB | 45 MB | ✅ |
| **7z, one solid block** | **300 MB** | **300 MB** | ✅ works, but **no saving** |
| RAR 5 | 35 MB | 35 MB | ✅ |
| ZIP split in 20 MB volumes | 35 MB | 35 MB | ✅ |

Test suite: 44 tests (`python -m pytest`), including `kill -9` at random moments followed by a resume and a byte-for-byte comparison, for **ZIP, tar, 7z, RAR and split ZIP**. The GUI was smoke-tested under a virtual display for every format.

**Not verified yet:** Windows (the CI workflow runs the tests there; check it before trusting Windows behaviour), macOS, RAR 4 creation (the installed `rar` cannot make RAR 4 files, so RAR 4 is simply refused in place), and the first run of the Windows build workflow.

## Known limitations

* No freeing of space *inside* a single huge file: the biggest file (or 7z solid block) must fit completely before its archive bytes are released.
* No in-place `.tar.gz` / `.tar.bz2` / `.tar.xz`, RAR 4, solid RAR, RAR volume sets or password-protected archives (these use normal extraction or are refused).
* A paused 7z can only be continued by ShrinkZIP itself.
* Windows, macOS and the built `.exe` are not yet verified (see above).

## Install and run

**Portable exe:** download `ShrinkZip.exe` from the [Releases](../../releases) page (Windows 10/11).

**From source:**

```bash
git clone https://github.com/your-username/ShrinkZIP.git
cd ShrinkZIP
pip install -r requirements.txt
python shrink_unzip_gui.py
```

RAR needs a `7z` binary (7-Zip) on the machine or bundled in the exe.

## Download (for users)

Open the **[Releases](../../releases)** page and download **`ShrinkZip.exe`**, then double-click it. No installation is needed (Windows 10/11, 64-bit). Optionally compare the file with `ShrinkZip.exe.sha256` from the same release.

Windows may show a "Windows protected your PC" (SmartScreen) warning because the exe is not code-signed. Click **More info → Run anyway**.

## Automated builds (for you)

The workflow `.github/workflows/build-windows-exe.yml` runs on a Windows runner and:

1. detects the project (Python + customtkinter, dependencies from `requirements.txt`),
2. runs the unit tests,
3. bundles 7-Zip (`7z.exe`, `7z.dll`) and builds **`dist/ShrinkZip.exe`** with PyInstaller (`shrink_unzip_gui.spec`),
4. builds a second, console **self-test** executable from the same spec and runs it: it extracts real ZIP, tar, 7z and split archives from inside the frozen build,
5. launches the real `ShrinkZip.exe` and fails if it crashes at startup,
6. uploads `ShrinkZip.exe` (+ SHA-256 and the 7-Zip license) as a **workflow artifact** on every run, and
7. **publishes a GitHub Release** when you push a version tag.

```bash
git tag v1.0.0
git push origin v1.0.0
```

Normal pushes and pull requests only produce the artifact (Actions → the run → *Artifacts*). Tags must look like `v1.0.0`; unit tests block the release on tags. You can also start a build by hand from **Actions → Build Windows EXE → Run workflow**.

**Locally:** install Python 3.10+ and double-click `build.bat` (output `dist\ShrinkZip.exe`). An optional installer script is in `installer/ShrinkZIP.iss` (compile it with [Inno Setup](https://jrsoftware.org/isinfo.php); it is not part of the automated build).

Optional files next to the spec are bundled when present: `app_icon.ico`, `7z.exe`, `7z.dll`. If you bundle 7-Zip, ship its license (LGPL, with the unRAR restriction); the workflow attaches it to every release.

## Project layout

* `shrinkzip_engine.py`: the engine (no GUI code)
* `shrink_unzip_gui.py`: the customtkinter GUI
* `installer/`: optional Inno Setup script
* `.github/workflows/`: Windows build + release, and cross-platform tests
* `.github/ci/selftest.py`: self-test that runs inside the frozen build
* `tests/`: pytest suite

## Credits

* **Created & designed by:** Arman Ahmed
* **Polished & assisted by:** Claude

## License

MIT, see [LICENSE](LICENSE).
