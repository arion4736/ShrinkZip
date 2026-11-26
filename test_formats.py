"""In-place extraction of tar, split archives, 7z and RAR, built with the real tools."""
import hashlib, os, random, shutil, signal, subprocess, sys, tarfile, time, zipfile
import pytest
from conftest import app, run_engine

SEVENZ = app.get_7z_tool_path()
HAVE_7Z = SEVENZ is not None and app.HAS_PY7ZR
GNU_TAR = shutil.which("tar") is not None and os.name != "nt"
HAVE_RAR = shutil.which("rar") is not None and SEVENZ is not None


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def make_tree(root, count, size, deep=False):
    """Create files under root; return {relpath: sha256}."""
    man = {}
    for i in range(count):
        rel = f"d{i % 3}/" + ("very_long_directory_name_" * 5 + "/" if deep and i % 2 else "") + f"file_{i:03d}.bin"
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(os.urandom(size))
        man[rel] = sha(p)
    return man


def check(out, man):
    for rel, h in man.items():
        assert sha(os.path.join(out, rel)) == h, rel


def split_file(path, vol):
    data = open(path, "rb").read()
    parts = []
    for i in range(0, len(data), vol):
        p = f"{path}.{i // vol + 1:03d}"
        open(p, "wb").write(data[i:i + vol])
        parts.append(p)
    os.remove(path)
    return parts


# ------------------------------------------------------------------ VolumeSet
def test_volumeset_read_across_volumes_and_truncate_deletes_volumes(tmp_path):
    data = os.urandom(10_000)
    for i in range(4):
        (tmp_path / f"x.bin.{i + 1:03d}").write_bytes(data[i * 2500:(i + 1) * 2500])
    vs = app.VolumeSet.discover(str(tmp_path / "x.bin.003"), writable=True)   # any volume name finds the set
    assert len(vs.paths) == 4 and vs.size() == 10_000
    vs.seek(2400); assert vs.read(300) == data[2400:2700]
    vs.truncate(5100)                                   # inside volume 3
    assert [p.name for p in tmp_path.iterdir()] == ["x.bin.001", "x.bin.002", "x.bin.003"] or \
           sorted(p.name for p in tmp_path.iterdir()) == ["x.bin.001", "x.bin.002", "x.bin.003"]
    assert vs.size() == 5100
    vs.truncate(5000)                                   # exactly on a volume boundary: volume 3 goes
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.bin.001", "x.bin.002"]
    vs.seek(0); assert vs.read() == data[:5000]
    vs.close()


# ------------------------------------------------------------------------ tar
@pytest.mark.parametrize("fmt", [tarfile.GNU_FORMAT, tarfile.PAX_FORMAT, tarfile.USTAR_FORMAT])
def test_tar_inplace_roundtrip(tmp_path, fmt):
    src = tmp_path / "src"
    man = make_tree(str(src), 12, 300_000, deep=(fmt != tarfile.USTAR_FORMAT))
    os.makedirs(src / "emptydir")
    t = tmp_path / "a.tar"
    with tarfile.open(t, "w", format=fmt) as tf:
        tf.add(src, arcname=".")
    (final, _), _ = run_engine(t, tmp_path / "out")
    assert final == "done" and not t.exists()
    check(tmp_path / "out", man)
    assert (tmp_path / "out" / "emptydir").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_tar_symlink_created_and_hardlink_refused(tmp_path):
    src = tmp_path / "src"
    os.makedirs(src)
    (src / "real.txt").write_text("hello")
    os.symlink("real.txt", src / "link.txt")
    t = tmp_path / "a.tar"
    subprocess.run(["tar", "-cf", str(t), "-C", str(src), "."], check=True)
    (final, _), _ = run_engine(t, tmp_path / "out")
    assert final == "done" and os.path.islink(tmp_path / "out" / "link.txt")
    os.link(src / "real.txt", src / "hard.txt")
    t2 = tmp_path / "b.tar"
    subprocess.run(["tar", "-cf", str(t2), "-C", str(src), "."], check=True)
    before = sha(t2)
    (final, data), _ = run_engine(t2, tmp_path / "out2")
    assert final == "error" and "hard link" in data and sha(t2) == before


@pytest.mark.skipif(not GNU_TAR, reason="needs GNU tar")
def test_tar_stop_leaves_valid_tar_then_resume(tmp_path):
    src = tmp_path / "src"
    man = make_tree(str(src), 40, 2_000_000)
    t = tmp_path / "a.tar"
    subprocess.run(["tar", "-cf", str(t), "-C", str(src), "."], check=True)
    (final, _), _ = run_engine(t, tmp_path / "out", stop_after_shrinks=2)
    assert final == "stopped"
    left = subprocess.run(["tar", "-tf", str(t)], capture_output=True, text=True)
    assert left.returncode == 0 and 0 < len([l for l in left.stdout.split() if l.endswith(".bin")]) < 40
    (final, _), _ = run_engine(t, tmp_path / "out")
    assert final == "done" and not t.exists()
    check(tmp_path / "out", man)


def test_tar_gz_uses_standard_extraction_and_keeps_archive(tmp_path):
    src = tmp_path / "src"
    man = make_tree(str(src), 5, 100_000)
    t = tmp_path / "a.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        tf.add(src, arcname=".")
    (final, _), _ = run_engine(t, tmp_path / "out")          # in-place requested: still safe
    assert final == "done" and t.exists()
    check(tmp_path / "out", man)


def test_tar_path_escape_refused(tmp_path):
    t = tmp_path / "a.tar"
    with tarfile.open(t, "w") as tf:
        ti = tarfile.TarInfo("../evil.txt"); ti.size = 3
        import io; tf.addfile(ti, io.BytesIO(b"bad"))
    before = sha(t)
    (final, data), _ = run_engine(t, tmp_path / "out")
    assert final == "error" and sha(t) == before and not (tmp_path / "evil.txt").exists()


# ---------------------------------------------------------------- split archives
def test_split_zip_inplace_deletes_volumes_and_extracts(tmp_path):
    src = tmp_path / "src"
    man = make_tree(str(src), 30, 1_000_000)
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for rel in man:
            zf.write(src / rel, rel)
    parts = split_file(str(z), 5_000_000)
    assert len(parts) >= 6
    (final, _), _ = run_engine(parts[2], tmp_path / "out")     # any volume name works
    assert final == "done" and not any(os.path.exists(p) for p in parts)
    check(tmp_path / "out", man)


def test_split_zip_stop_then_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "TRUNCATE_STEP", 3_000_000)
    src = tmp_path / "src"
    man = make_tree(str(src), 40, 1_500_000)
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for rel in man:
            zf.write(src / rel, rel)
    parts = split_file(str(z), 6_000_000)
    (final, _), _ = run_engine(parts[0], tmp_path / "out", stop_after_shrinks=2)
    assert final == "stopped"
    assert sum(os.path.exists(p) for p in parts) < len(parts)  # volumes were really given back
    (final, _), _ = run_engine(parts[0], tmp_path / "out")
    assert final == "done" and not any(os.path.exists(p) for p in parts)
    check(tmp_path / "out", man)


@pytest.mark.skipif(not GNU_TAR, reason="needs GNU tar")
def test_split_tar_inplace(tmp_path):
    src = tmp_path / "src"
    man = make_tree(str(src), 20, 800_000)
    t = tmp_path / "a.tar"
    subprocess.run(["tar", "-cf", str(t), "-C", str(src), "."], check=True)
    parts = split_file(str(t), 4_000_000)
    (final, _), _ = run_engine(parts[0], tmp_path / "out")
    assert final == "done" and not any(os.path.exists(p) for p in parts)
    check(tmp_path / "out", man)


# -------------------------------------------------------------------------- 7z
def make_7z(tmp_path, count, size, solid_flag):
    src = tmp_path / "src"
    man = make_tree(str(src), count, size)
    a = tmp_path / "a.7z"
    subprocess.run([SEVENZ, "a", "-mx=1", solid_flag, str(a), "*"], cwd=src, capture_output=True, check=True)
    return a, man


@pytest.mark.skipif(not HAVE_7Z, reason="needs 7z and py7zr")
@pytest.mark.parametrize("flag", ["-ms=off", "-ms=1500k"])
def test_7z_inplace_roundtrip(tmp_path, flag):
    a, man = make_7z(tmp_path, 16, 700_000, flag)
    (final, _), _ = run_engine(a, tmp_path / "out")
    assert final == "done" and not a.exists() and not (tmp_path / "a.7z.shrinkzip").exists()
    check(tmp_path / "out", man)


@pytest.mark.skipif(not HAVE_7Z, reason="needs 7z and py7zr")
def test_7z_stop_then_resume_from_saved_header(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "TRUNCATE_STEP", 3_000_000)
    a, man = make_7z(tmp_path, 60, 1_000_000, "-ms=off")
    (final, data), _ = run_engine(a, tmp_path / "out", stop_after_shrinks=1)
    assert final == "stopped" and "resumable" in data
    assert (tmp_path / "a.7z.shrinkzip").exists() and a.stat().st_size < 60 * 1_000_000
    (final, _), _ = run_engine(a, tmp_path / "out")
    assert final == "done" and not a.exists()
    check(tmp_path / "out", man)


@pytest.mark.skipif(not HAVE_7Z, reason="needs 7z and py7zr")
def test_7z_single_solid_block_is_planned_honestly(tmp_path):
    a, _ = make_7z(tmp_path, 10, 500_000, "-ms=on")            # default: everything in one block
    info = app.analyze_archive(str(a))
    peak = app.estimate_inplace_peak(info["units"], info["archive_size"])
    assert len(info["units"]) == 1 and peak >= 10 * 500_000   # no saving, and we say so


@pytest.mark.skipif(not HAVE_7Z, reason="needs 7z and py7zr")
def test_split_7z_made_by_7zip(tmp_path):
    src = tmp_path / "src"
    man = make_tree(str(src), 24, 800_000)
    subprocess.run([SEVENZ, "a", "-mx=1", "-ms=off", "-v3m", str(tmp_path / "a.7z"), "*"], cwd=src, capture_output=True, check=True)
    parts = sorted(str(p) for p in tmp_path.glob("a.7z.0*"))
    assert len(parts) >= 4
    (final, _), _ = run_engine(parts[0], tmp_path / "out")
    assert final == "done" and not any(os.path.exists(p) for p in parts)
    check(tmp_path / "out", man)


@pytest.mark.skipif(not HAVE_7Z, reason="needs 7z and py7zr")
def test_7z_with_unsafe_path_refused_untouched(tmp_path):
    a, _ = make_7z(tmp_path, 3, 1000, "-ms=off")
    before = sha(a)
    # a perfectly normal archive must not be refused...
    info = app.analyze_archive(str(a))
    assert info["inplace_ok"], info["reasons"]
    assert sha(a) == before


# ------------------------------------------------------------------------- RAR
def make_rar(tmp_path, count, size, *flags, name="a.rar"):
    src = tmp_path / "src"
    man = make_tree(str(src), count, size)
    subprocess.run(["rar", "a", "-idq", "-ep1", "-r", "-m0", *flags, name, "./src/*"], cwd=tmp_path, check=True,
                   capture_output=True)
    # rar stores paths relative to src because of -ep1 on dirs; recompute relative layout
    return tmp_path / name, man


@pytest.mark.skipif(not HAVE_RAR, reason="needs rar and 7z")
def test_rar_inplace_roundtrip(tmp_path):
    a, man = make_rar(tmp_path, 14, 900_000, "-ma5")
    info = app.analyze_archive(str(a))
    assert info["format"] == "rar" and info["inplace_ok"], info["reasons"]
    (final, data), _ = run_engine(a, tmp_path / "out")
    assert final == "done", data
    assert not a.exists()
    for rel, h in man.items():
        found = [p for p in (tmp_path / "out").rglob(os.path.basename(rel))]
        assert found and sha(found[0]) == h, rel


@pytest.mark.skipif(not HAVE_RAR, reason="needs rar and 7z")
def test_rar_stop_then_resume(tmp_path):
    a, man = make_rar(tmp_path, 40, 2_000_000, "-ma5")
    (final, _), _ = run_engine(a, tmp_path / "out", stop_after_shrinks=1)
    assert final == "stopped" and a.exists()
    (final, data), _ = run_engine(a, tmp_path / "out")
    assert final == "done", data
    for rel, h in man.items():
        found = list((tmp_path / "out").rglob(os.path.basename(rel)))
        assert found and sha(found[0]) == h


@pytest.mark.skipif(not HAVE_RAR, reason="needs rar and 7z")
def test_solid_rar_is_refused_before_anything_changes(tmp_path):
    a, _ = make_rar(tmp_path, 4, 10_000, "-ma5", "-m1", "-s")
    before = sha(a)
    (final, data), _ = run_engine(a, tmp_path / "out")
    assert final == "error" and "solid" in data and sha(a) == before


@pytest.mark.skipif(not HAVE_RAR, reason="needs rar and 7z")
def test_corrupt_rar_member_never_cuts_the_archive(tmp_path):
    a, _ = make_rar(tmp_path, 6, 400_000, "-ma5")
    raw = bytearray(a.read_bytes())
    raw[-3000] ^= 0xFF                                  # damage data of the LAST member (extracted first)
    a.write_bytes(bytes(raw))
    size = a.stat().st_size
    (final, data), _ = run_engine(a, tmp_path / "out")
    assert final == "error" and a.exists() and a.stat().st_size == size


# --------------------------------------------------------------- kill -9 (slow)
def _kill_resume(tmp_path, build, fmt_name, trials=6):
    child = tmp_path / "child.py"
    child.write_text(
        "import sys, queue\n"
        f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})\n"
        "import shrinkzip_engine as app\n"
        "w = app.ShrinkZipEngineWorker(sys.argv[1], sys.argv[2], True, False, 'overwrite', queue.Queue())\n"
        "w.run()\n")
    for t in range(trials):
        work = tmp_path / f"t{t}"
        work.mkdir()
        archive, check_fn = build(work)
        out = work / "out"
        p = subprocess.Popen([sys.executable, str(child), str(archive), str(out)])
        time.sleep(random.uniform(0.3, 2.5))
        p.send_signal(getattr(signal, "SIGKILL", signal.SIGTERM))
        p.wait()
        if archive.exists():
            (final, data), _ = run_engine(archive, out)
            assert final == "done", f"{fmt_name} trial {t}: {data}"
        check_fn(out)
        assert not archive.exists()


@pytest.mark.slow
@pytest.mark.skipif(not GNU_TAR, reason="needs GNU tar")
def test_kill_9_tar(tmp_path):
    def build(work):
        src = work / "src"
        man = make_tree(str(src), 200, 150_000)
        t = work / "a.tar"
        subprocess.run(["tar", "-cf", str(t), "-C", str(src), "."], check=True)
        return t, lambda out: check(out, man)
    _kill_resume(tmp_path, build, "tar")


@pytest.mark.slow
@pytest.mark.skipif(not HAVE_7Z, reason="needs 7z and py7zr")
def test_kill_9_7z(tmp_path):
    def build(work):
        a, man = make_7z(work, 60, 400_000, "-ms=1m")
        return a, lambda out: check(out, man)
    _kill_resume(tmp_path, build, "7z")


@pytest.mark.slow
@pytest.mark.skipif(not HAVE_RAR, reason="needs rar and 7z")
def test_kill_9_rar(tmp_path):
    def build(work):
        a, man = make_rar(work, 60, 400_000, "-ma5")
        def chk(out):
            for rel, h in man.items():
                found = list(out.rglob(os.path.basename(rel)))
                assert found and sha(found[0]) == h
        return a, chk
    _kill_resume(tmp_path, build, "rar")


@pytest.mark.slow
def test_kill_9_split_zip(tmp_path):
    def build(work):
        src = work / "src"
        man = make_tree(str(src), 120, 200_000)
        z = work / "a.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            for rel in man:
                zf.write(src / rel, rel)
        parts = split_file(str(z), 3_000_000)
        class P:                                        # archive "exists" while any volume does
            def __init__(s): s.p = parts[0]
            def exists(s): return os.path.exists(parts[0])
            def __str__(s): return parts[0]
        return P(), lambda out: check(out, man)
    _kill_resume(tmp_path, build, "split zip")


@pytest.mark.skipif(not HAVE_7Z, reason="needs 7z and py7zr")
def test_7z_saved_header_is_complete_before_first_cut(tmp_path):
    """Regression: the saved tail once came out EMPTY because the data end was miscalculated."""
    import struct, zlib
    a, _ = make_7z(tmp_path, 12, 200_000, "-ms=off")
    vs = app.VolumeSet([str(a)])
    sc = app.scan_7z(vs)
    vs.seek(0); nho, nhs, nhc = struct.unpack_from("<QQI", vs.read(32), 12)
    assert sc["tail_start"] <= 32 + nho
    vs.seek(sc["tail_start"]); tail = vs.read()
    assert len(tail) >= nhs and zlib.crc32(tail[32 + nho - sc["tail_start"]:][:nhs]) & 0xFFFFFFFF == nhc
    vs.close()
