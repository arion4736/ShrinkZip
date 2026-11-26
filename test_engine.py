import hashlib, os, shutil, signal, subprocess, sys, time, zipfile
import pytest
from conftest import app, run_engine

HAVE_7Z = app.get_7z_tool_path() is not None


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def make_zip(path, files, method=zipfile.ZIP_STORED):
    """files: {name: bytes}. Returns {name: sha256}."""
    with zipfile.ZipFile(path, "w", method) as z:
        for n, b in files.items():
            z.writestr(n, b)
    return {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}


def check_output(out, manifest):
    for n, h in manifest.items():
        assert sha(out / n) == h, n


def random_files(count, size):
    return {f"d{i % 4}/f{i}.bin": os.urandom(size) for i in range(count)}


def test_inplace_roundtrip_consumes_archive(tmp_path):
    z = tmp_path / "a.zip"
    man = make_zip(z, {**random_files(40, 2_000_000), "empty/": b""})
    (final, _), _ = run_engine(z, tmp_path / "out")
    assert final == "done"
    check_output(tmp_path / "out", {k: v for k, v in man.items() if not k.endswith("/")})
    assert (tmp_path / "out" / "empty").is_dir()
    assert not z.exists() and not (tmp_path / "a.zip.shrinkzip").exists()


def test_keep_mode_never_touches_archive(tmp_path):
    z = tmp_path / "a.zip"
    man = make_zip(z, random_files(10, 100_000))
    before = sha(z)
    (final, _), _ = run_engine(z, tmp_path / "out", shrink=False)
    assert final == "done" and sha(z) == before
    check_output(tmp_path / "out", man)


def test_keep_mode_deletes_only_when_asked_and_successful(tmp_path):
    z = tmp_path / "a.zip"
    make_zip(z, random_files(3, 1000))
    run_engine(z, tmp_path / "out", shrink=False, delete=True)
    assert not z.exists()


def test_stop_leaves_valid_zip_then_resume_completes(tmp_path):
    z = tmp_path / "a.zip"
    man = make_zip(z, random_files(60, 2_000_000))
    (final, _), _ = run_engine(z, tmp_path / "out", stop_after_shrinks=2)
    assert final == "stopped"
    with zipfile.ZipFile(z) as zf:                      # a normal ZIP again
        assert zf.testzip() is None and 0 < len(zf.namelist()) < 60
    assert not (tmp_path / "a.zip.shrinkzip").exists()
    (final, _), _ = run_engine(z, tmp_path / "out")
    assert final == "done" and not z.exists()
    check_output(tmp_path / "out", man)


def test_estimated_peak_matches_reality_for_single_big_file(tmp_path):
    z = tmp_path / "a.zip"
    make_zip(z, {"big.pak": os.urandom(30_000_000)})
    info = app.analyze_archive(str(z))
    est = app.estimate_inplace_peak(info["units"], info["archive_size"])
    assert est >= 30_000_000                            # one file must fit completely


def test_estimated_peak_is_small_for_many_small_files(tmp_path):
    z = tmp_path / "a.zip"
    make_zip(z, random_files(200, 1_000_000))
    info = app.analyze_archive(str(z))
    est = app.estimate_inplace_peak(info["units"], info["archive_size"])
    assert est < 200_000_000 * 0.4                      # far below the 200 MB total


def test_unsafe_paths_are_refused_before_anything_changes(tmp_path):
    z = tmp_path / "a.zip"
    make_zip(z, {"ok.txt": b"x", "../evil.txt": b"y"})
    before = sha(z)
    (final, data), _ = run_engine(z, tmp_path / "out")
    assert final == "error" and "unsafe" in data.lower()
    assert sha(z) == before and not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize("name", ["../x", "/abs", "C:/x", "a/../../b"])
def test_safe_member_path_rejects(name):
    assert app.safe_member_path(name) is None


def test_safe_member_path_accepts_normal():
    assert app.safe_member_path("a/b\\c.txt") == os.path.join("a", "b", "c.txt")


def test_zip64_index_rebuild_over_65535_entries(tmp_path):
    """recover_archive must emit a ZIP64 end record when there are >65535 entries."""
    z = tmp_path / "many.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for i in range(70_000):
            zf.writestr(f"f{i:06d}", b"x")
    vs = app.VolumeSet([str(z)], writable=True)
    with zipfile.ZipFile(vs) as zf:
        app.write_resume_sidecar(str(z), vs, zf)
        cut = sorted(i.header_offset for i in zf.infolist())[66_000]
    vs.truncate(cut)                                    # simulate a crash after cutting
    vs.close()
    assert app.recover_archive(str(z)) == 66_000
    with zipfile.ZipFile(z) as zf:
        assert len(zf.namelist()) == 66_000 and zf.testzip() is None


def test_stale_sidecar_is_ignored_when_archive_untouched(tmp_path):
    z = tmp_path / "a.zip"
    make_zip(z, random_files(3, 1000))
    vs = app.VolumeSet([str(z)], writable=True)
    with zipfile.ZipFile(vs) as zf:
        app.write_resume_sidecar(str(z), vs, zf)
    vs.close()
    assert app.recover_archive(str(z)) is None
    assert not (tmp_path / "a.zip.shrinkzip").exists()
    with zipfile.ZipFile(z) as zf:
        assert len(zf.namelist()) == 3


def test_sidecar_of_a_different_archive_is_refused(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(a, random_files(40, 100_000))
    vs = app.VolumeSet([str(a)], writable=True)
    with zipfile.ZipFile(vs) as zf:
        app.write_resume_sidecar(str(a), vs, zf)
        cut = sorted(i.header_offset for i in zf.infolist())[20]
    vs.close()
    shutil.copy(a, b)
    os.replace(str(a) + ".shrinkzip", str(b) + ".shrinkzip")
    # b is a different (shorter) file now: same name check passes only if names match
    with open(b, "r+b") as fp:
        fp.truncate(cut)
        fp.seek(0); fp.write(b"\x00" * 64)              # corrupt the identifying prefix
    with pytest.raises(app.ArchiveError):
        app.recover_archive(str(b))


@pytest.mark.skipif(not HAVE_7Z, reason="7z not installed")
def test_failed_7z_extraction_never_deletes_the_archive(tmp_path):
    src = tmp_path / "src" / "sub"
    src.mkdir(parents=True)
    for i in range(4):
        (src / f"f{i}.bin").write_bytes(os.urandom(100_000))
    a = tmp_path / "a.7z"
    subprocess.run([app.get_7z_tool_path(), "a", "-ms=off", str(a), str(src)], capture_output=True)
    out = tmp_path / "out"
    out.mkdir()
    (out / "sub").write_text("blocker")                 # a FILE where a folder is needed
    before = sha(a)
    (final, _), _ = run_engine(a, out, delete=True)
    assert final == "error" and a.exists() and sha(a) == before


@pytest.mark.skipif(not HAVE_7Z, reason="7z not installed")
def test_deflate64_zip_falls_back_to_7zip(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello world\n" * 50_000)
    z = tmp_path / "a.zip"
    subprocess.run([app.get_7z_tool_path(), "a", "-tzip", "-mm=Deflate64", str(z), str(src / "a.txt")], capture_output=True)
    (final, _), _ = run_engine(z, tmp_path / "out")
    assert final == "done" and z.exists()               # archive kept: no in-place for this method
    assert (tmp_path / "out" / "a.txt").read_text().startswith("hello world")


@pytest.mark.slow
def test_kill_9_at_random_moments_then_resume(tmp_path):
    """The crash-safety claim: SIGKILL anywhere, run again, get every file back."""
    import random
    child = tmp_path / "child.py"
    child.write_text(
        "import sys, queue\n"
        f"sys.path.insert(0, {os.path.dirname(__file__)!r})\n"
        "from conftest import app\n"
        "w = app.ShrinkZipEngineWorker(sys.argv[1], sys.argv[2], True, False, 'overwrite', queue.Queue())\n"
        "w._extract_zip(None)\n")
    for trial in range(8):
        z, out = tmp_path / f"t{trial}.zip", tmp_path / f"out{trial}"
        files = {f"d{i % 30}/f{i}.bin": os.urandom(random.choice([300, 20_000, 600_000])) for i in range(900)}
        man = make_zip(z, files)
        p = subprocess.Popen([sys.executable, str(child), str(z), str(out)])
        time.sleep(random.uniform(0.15, 2.0))
        p.send_signal(signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
        p.wait()
        if z.exists():
            (final, _), _ = run_engine(z, out)
            assert final == "done"
        check_output(out, man)
        assert not z.exists()
