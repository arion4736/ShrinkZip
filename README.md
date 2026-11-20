# ⚡ ShrinkZIP

**Extract big archives when you don't have room for both the archive and its contents.**

ShrinkZIP reads an archive from its tail inward and **cuts the archive file down as each part is safely extracted**, so the space it frees is reused by the extracted files. You need room for the extracted data, not for the archive *plus* the extracted data.

Inspired by [HatchZip](https://github.com/xevrion/hatchzip) (Rust, MIT OR Apache-2.0). ShrinkZIP is an **independent Python implementation of the same public design**: no HatchZip code is copied. HatchZip is the more complete tool (see [Compared with HatchZip](#compared-with-hatchzip)).

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

**Not verified yet:** Windows (the CI workflow runs the tests there; check it before trusting Windows behaviour), macOS, RAR 4 creation (the installed `rar` cannot make RAR 4 files, so RAR 4 is simply refused in place), and the built `.exe` itself.

## Compared with HatchZip

HatchZip additionally has: parallel decoding, **freeing space inside one huge file** (hole punching), in-place `.tar.gz`, RAR 4 / volume sets / encrypted archives, password support, a restore that rebuilds a valid 7z, macOS + Linux builds, a CLI, signed releases and a much larger test and fuzz suite. If you need any of that, use HatchZip. ShrinkZIP is a small, readable Python GUI built on the same idea.

## Install and run

**Portable exe:** download `ShrinkUnzip.exe` from the [Releases](../../releases) page (Windows 10/11).

**From source:**

```bash
git clone https://github.com/your-username/ShrinkZIP.git
cd ShrinkZIP
pip install -r requirements.txt
python shrink_unzip_gui.py
```

RAR needs a `7z` binary (7-Zip) on the machine or bundled in the exe.

## Build the exe

**Easiest: GitHub Actions.** Push the repo, open **Actions → build-exe → Run workflow**, then download `ShrinkUnzip-windows` from the run's *Artifacts*. Push a tag (`git tag v2.2 && git push --tags`) to also publish it as a Release. The workflow runs the tests first and bundles 7-Zip.

**On your PC:** install Python 3.10+, double-click `build.bat`. Output: `dist\ShrinkUnzip.exe`.

Optional files next to the spec are bundled when present: `app_icon.ico`, `7z.exe`, `7z.dll`. If you bundle 7-Zip, ship its license (LGPL, with the unRAR restriction) with your release.

## Project layout

* `shrinkzip_engine.py`: the engine (no GUI code)
* `shrink_unzip_gui.py`: the customtkinter GUI
* `tests/`: pytest suite

## Credits

* **Created & designed by:** Arman Ahmed
* **Polished & assisted by:** Claude
* Design ideas (end-first extraction, saved tail/index, fsync-before-cut, replay planner, split volumes as one logical file): [HatchZip](https://github.com/xevrion/hatchzip)

## License

MIT, see [LICENSE](LICENSE).
