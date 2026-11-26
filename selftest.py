"""Headless self-test that runs INSIDE a frozen build (and also works unfrozen).

CI builds this script with the same spec as ShrinkZip.exe, so it proves that the
bundle really contains Tk, customtkinter, py7zr, the shrinkzip_engine module and the
bundled 7-Zip. Exit code 0 = every check passed.
"""
import hashlib
import os
import queue
import subprocess
import sys
import tarfile
import tempfile
import traceback
import zipfile

if not getattr(sys, "frozen", False):                      # allow running from source
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import shrinkzip_engine as eng

results = []


def case(name):
    def deco(fn):
        try:
            detail = fn() or ""
            results.append(True)
            print(f"[ OK ] {name} {detail}", flush=True)
        except Exception as exc:                            # noqa: BLE001
            results.append(False)
            print(f"[FAIL] {name}: {exc!r}", flush=True)
            traceback.print_exc()
        return fn
    return deco


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def make_files(root, count=8, size=300_000):
    man = {}
    for i in range(count):
        rel = f"d{i % 2}/file_{i}.bin"
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(os.urandom(size))
        man[rel] = sha(p)
    return man


def run_engine(archive, dest, shrink=True):
    q = queue.Queue()
    w = eng.ShrinkZipEngineWorker(archive, dest, shrink, False, "overwrite", q)
    w.start()
    while True:
        kind, data = q.get(timeout=120)
        if kind in ("done", "error", "stopped"):
            w.join(10)
            return kind, data


def verify(out, man):
    for rel, h in man.items():
        assert sha(os.path.join(out, rel)) == h, f"content mismatch: {rel}"


TMP = tempfile.mkdtemp(prefix="shrinkzip-selftest-")


@case("environment")
def _():
    return f"(frozen={getattr(sys, 'frozen', False)}, python={sys.version.split()[0]}, os={os.name})"


@case("tkinter window opens")
def _():
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        return "(skipped: no display)"
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    root.update()
    root.destroy()


@case("customtkinter assets + window")
def _():
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        return "(skipped: no display)"
    import customtkinter as ctk
    assets = os.path.join(os.path.dirname(ctk.__file__), "assets")
    assert os.path.isdir(assets), f"customtkinter assets missing: {assets}"
    root = ctk.CTk()
    root.update()
    root.destroy()


@case("py7zr importable")
def _():
    import py7zr
    return f"(py7zr {py7zr.__version__})"


TOOL = eng.get_7z_tool_path()


@case("native 7-Zip found and runs")
def _():
    assert TOOL, "7z binary not found"
    if getattr(sys, "frozen", False) and os.name == "nt":
        assert os.path.abspath(TOOL).startswith(os.path.abspath(sys._MEIPASS)), \
            f"7z.exe is not the bundled copy: {TOOL}"
    out = subprocess.run([TOOL, "i"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode == 0 and "Formats" in out.stdout, out.stdout[:200]
    return f"({TOOL})"


@case("ZIP in-place extraction")
def _():
    d = os.path.join(TMP, "zip"); os.makedirs(d)
    man = make_files(os.path.join(d, "src"))
    a = os.path.join(d, "a.zip")
    with zipfile.ZipFile(a, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in man:
            z.write(os.path.join(d, "src", rel), rel)
    kind, data = run_engine(a, os.path.join(d, "out"))
    assert kind == "done", data
    assert not os.path.exists(a), "archive should have been consumed"
    verify(os.path.join(d, "out"), man)


@case("tar in-place extraction")
def _():
    d = os.path.join(TMP, "tar"); os.makedirs(d)
    man = make_files(os.path.join(d, "src"))
    a = os.path.join(d, "a.tar")
    with tarfile.open(a, "w") as tf:
        tf.add(os.path.join(d, "src"), arcname=".")
    kind, data = run_engine(a, os.path.join(d, "out"))
    assert kind == "done", data
    assert not os.path.exists(a)
    verify(os.path.join(d, "out"), man)


@case("7z in-place extraction (bundled 7-Zip + py7zr)")
def _():
    d = os.path.join(TMP, "sevenz"); os.makedirs(d)
    man = make_files(os.path.join(d, "src"))
    a = os.path.join(d, "a.7z")
    subprocess.run([TOOL, "a", "-mx=1", "-ms=off", a, "*"],
                   cwd=os.path.join(d, "src"), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    kind, data = run_engine(a, os.path.join(d, "out"))
    assert kind == "done", data
    assert not os.path.exists(a)
    verify(os.path.join(d, "out"), man)


@case("7z standard extraction keeps the archive")
def _():
    d = os.path.join(TMP, "sevenz2"); os.makedirs(d)
    man = make_files(os.path.join(d, "src"))
    a = os.path.join(d, "a.7z")
    subprocess.run([TOOL, "a", "-mx=1", a, "*"],
                   cwd=os.path.join(d, "src"), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    kind, data = run_engine(a, os.path.join(d, "out"), shrink=False)
    assert kind == "done", data
    assert os.path.exists(a), "Keep Archive mode must not delete the archive"
    verify(os.path.join(d, "out"), man)


@case("split ZIP in-place extraction")
def _():
    d = os.path.join(TMP, "split"); os.makedirs(d)
    man = make_files(os.path.join(d, "src"), count=10, size=400_000)
    z = os.path.join(d, "a.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for rel in man:
            zf.write(os.path.join(d, "src", rel), rel)
    data = open(z, "rb").read()
    os.remove(z)
    parts = []
    for n, i in enumerate(range(0, len(data), 1_000_000)):
        p = f"{z}.{n + 1:03d}"
        open(p, "wb").write(data[i:i + 1_000_000])
        parts.append(p)
    kind, info = run_engine(parts[0], os.path.join(d, "out"))
    assert kind == "done", info
    assert not any(os.path.exists(p) for p in parts), "volumes should have been deleted"
    verify(os.path.join(d, "out"), man)


print(f"\n{sum(results)}/{len(results)} checks passed", flush=True)
sys.exit(0 if all(results) else 1)
