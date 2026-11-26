# ==============================================================================
# ShrinkZIP engine - in-place, low-disk archive extraction (no GUI code here)
#
# Created & Designed by: Arman Ahmed
# Polished & assisted by: Claude
#
#  Format        In place?   Unit that is freed at once
#  ZIP / ZIP64   yes         one entry
#  tar           yes         one entry (incl. long-name / pax records)
#  7z            yes         one solid block (a single giant block saves nothing)
#  RAR 5         yes*        one file     (*non-solid, single volume; RAR 4 not in place)
#  split .001..  yes         any of the above, volumes deleted as they empty
#  tar.gz etc.   no          compressed streams cannot be cut from the end
# ==============================================================================

import io
import os
import re
import sys
import json
import stat
import time
import shutil
import struct
import hashlib
import tarfile
import zipfile
import zlib
import threading
import subprocess
import warnings
warnings.filterwarnings("ignore")

try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False

try:
    import rarfile
    HAS_RARFILE = True
except ImportError:
    HAS_RARFILE = False


# ------------------------------------------------------------------------------
# Tunables
# ------------------------------------------------------------------------------
TRUNCATE_STEP = 32 * 1024 * 1024       # cut the archive once this much can be reclaimed
SUPPORTED_ZIP_METHODS = {0, 8, 12, 14}  # stored, deflate, bzip2, lzma
ZIP_METHOD_NAMES = {0: "Stored", 8: "Deflate", 9: "Deflate64", 12: "BZip2", 14: "LZMA",
                    93: "Zstandard", 95: "XZ", 98: "PPMd", 99: "AES-encrypted"}
SIDECAR_SUFFIX = ".shrinkzip"
SIDECAR_MAGIC = b"SHRINKZIP-RESUME-2\n"
RAR5_SIG = b"Rar!\x1a\x07\x01\x00"
RAR4_SIG = b"Rar!\x1a\x07\x00"
SEVENZ_SIG = b"7z\xbc\xaf\x27\x1c"
BCJ2_ID = b"\x03\x03\x01\x1b"


class ArchiveError(Exception):
    """A problem with the archive that must stop the run before data is lost."""


# ------------------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------------------
def get_resource_path(relative_path: str) -> str:
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def get_7z_tool_path():
    """Find the native 7-Zip binary (bundled, next to the script, standard dirs, PATH)."""
    candidates = [
        get_resource_path("7z.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "7z.exe"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return shutil.which("7z") or shutil.which("7za")


def format_bytes(num_bytes):
    if num_bytes is None:
        return "--"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def format_time(seconds):
    if seconds <= 0 or seconds > 359999:
        return "--:--"
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hrs > 0:
        return f"{hrs:02d}h {mins:02d}m {secs:02d}s"
    return f"{mins:02d}m {secs:02d}s"


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_member_path(name):
    """Safe relative path for an archive member, or None if unsafe (never 'repaired')."""
    if not name or "\x00" in name:
        return None
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/"):
        return None
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if not parts:
        return ""
    if len(parts[0]) == 2 and parts[0][1] == ":":
        return None
    for p in parts:
        if p == "..":
            return None
        if os.name == "nt" and (":" in p or p.rstrip(" .") != p or p.split(".")[0].upper() in _WIN_RESERVED):
            return None
    return os.path.join(*parts)


def _fsync_parent_dir(path):
    if os.name == "nt":
        return
    try:
        fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_parent_dir(path)


class _Meter:
    def __init__(self, total_bytes):
        self.total = total_bytes
        self.t0 = time.time()
        self.last_t = self.t0
        self.last_b = 0
        self.speed_str = "-- MB/s"
        self.eta_str = "CALCULATING..."

    def update(self, done_bytes):
        now = time.time()
        dt = now - self.last_t
        if dt >= 0.5:
            cur = (done_bytes - self.last_b) / dt
            avg = done_bytes / (now - self.t0) if now > self.t0 else 0
            speed = cur * 0.7 + avg * 0.3 if cur > 0 else avg
            if speed > 0:
                self.speed_str = f"{format_bytes(int(speed))}/s"
                self.eta_str = format_time(max(0, self.total - done_bytes) / speed)
            self.last_t, self.last_b = now, done_bytes
        return self.speed_str, self.eta_str, format_time(now - self.t0)


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


# ------------------------------------------------------------------------------
# Storage: one logical file made of one or more volumes
# ------------------------------------------------------------------------------
_VOL_RE = re.compile(r"^(.*)\.(\d{3,})$")


def first_volume(path):
    """'x.7z.003' -> 'x.7z.001'; anything else is returned unchanged."""
    m = _VOL_RE.match(path)
    if not m:
        return path
    cand = f"{m.group(1)}.{1:0{len(m.group(2))}d}"
    return cand if os.path.exists(cand) else path


class VolumeSet(io.RawIOBase):
    """Reads (and, if writable, truncates) a split archive as ONE logical file.

    truncate(n) deletes every volume that lies wholly at or past n and shortens
    the one n falls in, so freed volumes give their space back immediately.
    """
    def __init__(self, paths, writable=False):
        super().__init__()
        self.paths = list(paths)
        self.rw = writable
        self.lens = [os.path.getsize(p) for p in self.paths]
        self._fh = {}
        self._pos = 0

    @classmethod
    def discover(cls, path, writable=False):
        m = _VOL_RE.match(path)
        if not m:
            return cls([path], writable)
        base, width = m.group(1), len(m.group(2))
        parts, i = [], 1
        while os.path.exists(f"{base}.{i:0{width}d}"):
            parts.append(f"{base}.{i:0{width}d}")
            i += 1
        return cls(parts or [path], writable)

    def _starts(self):
        out, t = [], 0
        for n in self.lens:
            out.append(t)
            t += n
        return out

    def size(self):
        return sum(self.lens)

    def readable(self):
        return True

    def seekable(self):
        return True

    def writable(self):
        return self.rw

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        p = offset if whence == 0 else (self._pos + offset if whence == 1 else self.size() + offset)
        if p < 0:
            raise ValueError("negative seek position")
        self._pos = p
        return p

    def _handle(self, i):
        fh = self._fh.get(i)
        if fh is None or fh.closed:
            fh = open(self.paths[i], "r+b" if self.rw else "rb")
            self._fh[i] = fh
        return fh

    def readinto(self, b):
        mv = memoryview(b)
        n, total = 0, self.size()
        starts = self._starts()
        while n < len(mv) and self._pos < total:
            i = max(k for k, s in enumerate(starts) if s <= self._pos)
            local = self._pos - starts[i]
            fh = self._handle(i)
            fh.seek(local)
            data = fh.read(min(len(mv) - n, self.lens[i] - local))
            if not data:
                break
            mv[n:n + len(data)] = data
            n += len(data)
            self._pos += len(data)
        return n

    def write(self, b):
        """Append/overwrite inside the LAST volume only (used to rebuild an index)."""
        i = len(self.paths) - 1
        start = self._starts()[i]
        if self._pos < start:
            raise ValueError("can only write into the last volume")
        fh = self._handle(i)
        fh.seek(self._pos - start)
        fh.write(b)
        self._pos += len(b)
        self.lens[i] = max(self.lens[i], self._pos - start)
        return len(b)

    def truncate(self, size=None):
        n = self._pos if size is None else size
        starts = self._starts()
        removed = False
        while len(self.paths) > 1 and starts[-1] >= n:
            i = len(self.paths) - 1
            fh = self._fh.pop(i, None)
            if fh:
                fh.close()
            os.remove(self.paths[i])
            self.paths.pop(); self.lens.pop(); starts.pop()
            removed = True
        i = len(self.paths) - 1
        local = max(0, n - starts[i])
        if local < self.lens[i]:
            fh = self._handle(i)
            fh.flush()
            fh.truncate(local)
            fh.flush()
            self.lens[i] = local
        if removed:
            _fsync_parent_dir(self.paths[0])
        return n

    def flush(self):
        for fh in list(self._fh.values()):
            if not fh.closed:
                fh.flush()

    def fsync(self):
        for fh in list(self._fh.values()):
            if not fh.closed:
                fh.flush()
                os.fsync(fh.fileno())

    def close(self):
        for fh in list(self._fh.values()):
            try:
                fh.close()
            except Exception:
                pass
        self._fh.clear()
        super().close()

    def delete_all(self):
        self.close()
        for p in self.paths:
            try:
                os.remove(p)
            except OSError:
                pass


class TailView(io.RawIOBase):
    """The archive as it was originally: live prefix + zeros (already cut) + saved tail."""
    def __init__(self, base, tail_start, tail):
        super().__init__()
        self.base, self.tail_start, self.tail = base, tail_start, tail
        self._pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        total = self.tail_start + len(self.tail)
        p = offset if whence == 0 else (self._pos + offset if whence == 1 else total + offset)
        if p < 0:
            raise ValueError("negative seek position")
        self._pos = p
        return p

    def readinto(self, b):
        mv = memoryview(b)
        n, total = 0, self.tail_start + len(self.tail)
        while n < len(mv) and self._pos < total:
            want = len(mv) - n
            if self._pos < self.tail_start:
                avail = self.base.size()
                span = min(want, self.tail_start - self._pos)
                if self._pos < avail:
                    self.base.seek(self._pos)
                    chunk = self.base.read(min(span, avail - self._pos))
                else:
                    chunk = bytes(span)
            else:
                o = self._pos - self.tail_start
                chunk = self.tail[o:o + want]
            if not chunk:
                break
            mv[n:n + len(chunk)] = chunk
            n += len(chunk)
            self._pos += len(chunk)
        return n


# ------------------------------------------------------------------------------
# Journal (side file with the archive index / tail) - ZIP and 7z
# ------------------------------------------------------------------------------
def sidecar_path(archive_path):
    return first_volume(archive_path) + SIDECAR_SUFFIX


def _fingerprints(vs, size):
    res = {}
    for n in (30, 512, 4096):
        n = min(n, size)
        vs.seek(0)
        res[str(n)] = hashlib.sha256(vs.read(n)).hexdigest()
    return res


def _fingerprints_ok(vs, meta):
    size = vs.size()
    usable = [int(k) for k in meta["fingerprints"] if 0 < int(k) <= size]
    if not usable:
        return True
    n = max(usable)
    vs.seek(0)
    return hashlib.sha256(vs.read(n)).hexdigest() == meta["fingerprints"][str(n)]


def save_journal(archive, meta, blob):
    head = json.dumps(meta).encode("utf-8")
    _atomic_write(sidecar_path(archive), SIDECAR_MAGIC + struct.pack("<Q", len(head)) + head + blob)


def load_journal(archive):
    with open(sidecar_path(archive), "rb") as f:
        data = f.read()
    if not data.startswith(SIDECAR_MAGIC):
        raise ArchiveError("Resume file is not a ShrinkZIP resume file.")
    pos = len(SIDECAR_MAGIC)
    (jl,) = struct.unpack_from("<Q", data, pos)
    pos += 8
    return json.loads(data[pos:pos + jl].decode("utf-8")), data[pos + jl:]


def _journal_parts(archive, meta):
    """Volumes of the journaled archive that still exist (a prefix of the original set)."""
    d = os.path.dirname(os.path.abspath(archive))
    if meta["parts"][0][0] != os.path.basename(first_volume(archive)):
        raise ArchiveError("The resume file next to this archive does not belong to it. "
                           f"If you replaced the archive, delete '{os.path.basename(sidecar_path(archive))}'.")
    paths = []
    for name, _ln in meta["parts"]:
        p = os.path.join(d, name)
        if not os.path.exists(p):
            break
        paths.append(p)
    if not paths:
        raise ArchiveError("The archive file(s) named in the resume file are missing.")
    return paths


def _journal_untouched(vs, meta):
    return vs.size() == sum(ln for _n, ln in meta["parts"])


def _journal_matches(archive, vs, meta):
    names = [n for n, _l in meta["parts"]]
    have = [os.path.basename(p) for p in vs.paths]
    if have != names[:len(have)] or vs.size() > sum(l for _n, l in meta["parts"]) \
            or not _fingerprints_ok(vs, meta):
        raise ArchiveError("The resume file next to this archive does not belong to it. "
                           f"If you replaced the archive, delete '{os.path.basename(sidecar_path(archive))}'.")


def _clear_journal(archive):
    for p in (sidecar_path(archive), sidecar_path(archive) + ".restore", sidecar_path(archive) + ".tmp"):
        if os.path.exists(p):
            os.remove(p)


# ------------------------------------------------------------------------------
# ZIP: analysis, index save, index rebuild
# ------------------------------------------------------------------------------
def analyze_zip(zf, archive_size):
    infos = zf.infolist()
    reasons, bad_paths = [], []
    methods = {i.compress_type for i in infos}
    encrypted = sum(1 for i in infos if i.flag_bits & 0x1)
    unsupported = sorted(m for m in methods if m not in SUPPORTED_ZIP_METHODS)
    if unsupported:
        names = ", ".join(ZIP_METHOD_NAMES.get(m, f"method {m}") for m in unsupported)
        reasons.append(f"uses compression this engine cannot read in place ({names})")
    if encrypted:
        reasons.append(f"{encrypted} password-protected file(s)")
    for i in infos:
        if safe_member_path(i.filename) is None:
            bad_paths.append(i.filename)
    if bad_paths:
        reasons.append(f"{len(bad_paths)} member(s) with unsafe paths (e.g. {bad_paths[0]!r})")
    asc = sorted(infos, key=lambda i: i.header_offset)
    start_dir = getattr(zf, "start_dir", None)
    if start_dir is None:
        reasons.append("cannot locate the central directory")
    elif asc:
        if asc[0].header_offset != 0:
            reasons.append("data before the first entry (self-extracting stub?)")
        for n, e in enumerate(asc):
            end = asc[n + 1].header_offset if n + 1 < len(asc) else start_dir
            if e.header_offset + 30 + len(e.orig_filename) + e.compress_size > end:
                reasons.append(f"entries overlap or are inconsistent near {e.filename!r}")
                break
    layout = [(e.header_offset, e.file_size) for e in asc]
    return {"layout": layout, "inplace_ok": not reasons, "reasons": reasons,
            "methods": methods, "encrypted": encrypted, "bad_paths": bad_paths}


def _split_central_directory(tail, count):
    lens, pos = [], 0
    for _ in range(count):
        if tail[pos:pos + 4] != b"PK\x01\x02":
            raise ArchiveError("Unexpected central directory layout.")
        n, m, k = struct.unpack_from("<HHH", tail, pos + 28)
        ln = 46 + n + m + k
        lens.append(ln)
        pos += ln
    return lens


def write_resume_sidecar(archive, vs, zf):
    infos = zf.infolist()
    size = vs.size()
    vs.seek(zf.start_dir)
    tail = vs.read(size - zf.start_dir)
    lens = _split_central_directory(tail, len(infos))
    meta = {"version": 2, "format": "zip",
            "parts": [[os.path.basename(p), l] for p, l in zip(vs.paths, vs.lens)],
            "data_end": zf.start_dir, "offsets": [i.header_offset for i in infos],
            "lens": lens, "fingerprints": _fingerprints(vs, size)}
    save_journal(archive, meta, tail[:sum(lens)])


def _build_eocd(count, cd_size, cd_offset):
    out = b""
    if count > 0xFFFF or cd_size > 0xFFFFFFFF or cd_offset > 0xFFFFFFFF:
        out += struct.pack("<IQHHIIQQQQ", 0x06064b50, 44, 45, 45, 0, 0, count, count, cd_size, cd_offset)
        out += struct.pack("<IIQI", 0x07064b50, 0, cd_offset + cd_size, 1)
    out += struct.pack("<IHHHHIIH", 0x06054b50, 0, 0, min(count, 0xFFFF), min(count, 0xFFFF),
                       min(cd_size, 0xFFFFFFFF), min(cd_offset, 0xFFFFFFFF), 0)
    return out


def recover_archive(archive_path, log=lambda s: None):
    """Make a paused / failed / crashed in-place ZIP a normal, valid ZIP again.

    Returns None if there was nothing to recover, else the number of entries still
    inside (0 = everything was extracted). Idempotent: interrupt it and run it again.
    """
    archive_path = first_volume(archive_path)
    if not os.path.exists(sidecar_path(archive_path)):
        return None
    meta, blob = load_journal(archive_path)
    if meta.get("format") != "zip":
        return None
    marker = sidecar_path(archive_path) + ".restore"
    vs = VolumeSet(_journal_parts(archive_path, meta), writable=True)
    try:
        if os.path.exists(marker):
            with open(marker) as f:
                data_end = int(f.read().strip())
        else:
            if _journal_untouched(vs, meta):
                _clear_journal(archive_path)
                return None
            _journal_matches(archive_path, vs, meta)
            data_end = vs.size()
            _atomic_write(marker, str(data_end).encode())
        offsets, lens = meta["offsets"], meta["lens"]
        starts, pos = [], 0
        for ln in lens:
            starts.append(pos)
            pos += ln
        keep = [i for i, off in enumerate(offsets) if off < data_end]
        vs.truncate(data_end)
        if keep:
            cd = b"".join(blob[starts[i]:starts[i] + lens[i]] for i in keep)
            vs.seek(data_end)
            vs.write(cd + _build_eocd(len(keep), len(cd), data_end))
        else:
            vs.truncate(0)
        vs.fsync()
    finally:
        vs.close()
    _clear_journal(archive_path)
    log(f"[RESTORE] Archive index rebuilt: {len(keep):,} entries remain inside the archive.")
    return len(keep)


# ------------------------------------------------------------------------------
# Planner (pure function): replay a tail-first extraction, return the peak EXTRA bytes
# ------------------------------------------------------------------------------
def make_batches(units_desc, batch_bytes):
    out, cur, acc = [], [], 0
    for u in units_desc:
        cur.append(u)
        acc += u["out"]
        if batch_bytes == 0 or acc >= batch_bytes:
            out.append(cur)
            cur, acc = [], 0
    if cur:
        out.append(cur)
    return out


def estimate_inplace_peak(units_asc, archive_size, step=TRUNCATE_STEP, batch_bytes=0):
    """units_asc: [(offset, output_size)] ascending. Uses the engine's exact cutting rule."""
    if not units_asc:
        return 0
    desc = [{"off": o, "out": s} for o, s in reversed(units_asc)]
    lowest = units_asc[0][0]
    out, cur, peak = 0, archive_size, 0
    for batch in make_batches(desc, batch_bytes):
        bout = sum(u["out"] for u in batch)
        peak = max(peak, out + bout - (archive_size - cur))
        out += bout
        off = batch[-1]["off"]
        if cur - off >= step or off == lowest:
            cur = off
    return peak


# ------------------------------------------------------------------------------
# Scanners: one per format. Each returns a dict:
#   units   - ascending [{'off','out','files'...}]  (the pieces freed from the end)
#   entries - rows for the GUI tree
#   reasons - why in-place is NOT possible (empty = fine)
# ------------------------------------------------------------------------------
def _mtime_str(t):
    try:
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(t))
    except Exception:
        return "--"


def scan_tar(vs):
    reasons, units, entries, seen = [], [], [], set()
    vs.seek(0)
    try:
        tf = tarfile.open(fileobj=vs, mode="r:")
    except tarfile.TarError as e:
        raise ArchiveError(f"Not a readable tar archive: {e}")
    try:
        for ti in tf:
            name = ti.name.lstrip("/")
            rel = safe_member_path(name)
            if rel is None:
                reasons.append(f"unsafe path {ti.name!r}")
            low = (rel or "").lower()
            if low in seen and rel:
                reasons.append(f"duplicate path {ti.name!r}")
            seen.add(low)
            kind = "file"
            if ti.islnk():
                reasons.append(f"hard link {ti.name!r}"); kind = "hardlink"
            elif ti.issparse():
                reasons.append(f"sparse file {ti.name!r}"); kind = "sparse"
            elif ti.isdir():
                kind = "dir"
            elif ti.issym():
                kind = "symlink"
            elif not ti.isreg():
                reasons.append(f"special file {ti.name!r}"); kind = "special"
            out = ti.size if kind == "file" else 0
            units.append({"off": ti.offset, "out": out, "name": name, "rel": rel, "kind": kind, "ti": ti})
            entries.append((name + ("/" if kind == "dir" and not name.endswith("/") else ""), out, out,
                            _mtime_str(ti.mtime), kind == "dir", "--", "tar"))
        end = tf.offset
    finally:
        pass
    if not units:
        reasons.append("archive has no entries")
    units.sort(key=lambda u: u["off"])
    return {"format": "tar", "units": units, "entries": entries, "reasons": reasons,
            "tail_start": end, "tf": tf, "batch_bytes": 0}


def _vint(buf, pos):
    r = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        r |= (b & 0x7F) << shift
        if not b & 0x80:
            return r, pos
        shift += 7


def _scan_rar5(vs):
    total = vs.size()
    pos, units, entries, reasons = 8, [], [], []
    while pos + 7 <= total:
        vs.seek(pos)
        head = vs.read(min(16, total - pos))
        try:
            hsize, p = _vint(head, 4)
        except IndexError:
            reasons.append("damaged header"); break
        vs.seek(pos + p)
        body = vs.read(hsize)
        if len(body) < hsize:
            reasons.append("archive is truncated mid-header"); break
        btype, q = _vint(body, 0)
        flags, q = _vint(body, q)
        extra = data = 0
        if flags & 1:
            extra, q = _vint(body, q)
        if flags & 2:
            data, q = _vint(body, q)
        block_end = pos + p + hsize + data
        if btype == 4:
            reasons.append("encrypted headers"); break
        if btype == 1:
            af, _ = _vint(body, q)
            if af & 1:
                reasons.append("multi-volume RAR set")
            if af & 4:
                reasons.append("solid archive")
        elif btype == 2:
            ff, q = _vint(body, q)
            usize, q = _vint(body, q)
            _attr, q = _vint(body, q)
            if ff & 2:
                q += 4
            crc = None
            if ff & 4:
                crc = struct.unpack_from("<I", body, q)[0]
                q += 4
            ci, q = _vint(body, q)
            _host, q = _vint(body, q)
            nlen, q = _vint(body, q)
            name = body[q:q + nlen].decode("utf-8", "replace")
            is_dir = bool(ff & 1)
            if ci & 0x40:
                reasons.append("solid archive")
            if ff & 8:
                reasons.append("file of unknown size")
            if extra:                                   # scan the extra area for encryption / links
                ex = body[len(body) - extra:]
                ep = 0
                while ep < len(ex):
                    try:
                        rs, ep2 = _vint(ex, ep)
                        rt, _ = _vint(ex, ep2)
                    except IndexError:
                        break
                    if rt == 1:
                        reasons.append(f"encrypted file {name!r}")
                    elif rt == 5:
                        reasons.append(f"link {name!r}")
                    ep = ep2 + rs
            rel = safe_member_path(name)
            if rel is None:
                reasons.append(f"unsafe path {name!r}")
            units.append({"off": pos, "out": 0 if is_dir else usize, "name": name, "rel": rel,
                          "kind": "dir" if is_dir else "file", "crc": crc, "size": usize})
            entries.append((name + ("/" if is_dir else ""), usize, data, "--", is_dir,
                            f"{crc:08X}" if crc is not None else "--", "RAR5"))
        elif btype == 5:
            break
        pos = block_end
    return units, entries, reasons


def scan_rar(vs):
    vs.seek(0)
    sig = vs.read(8)
    if sig == RAR5_SIG:
        units, entries, reasons = _scan_rar5(vs)
    elif sig[:7] == RAR4_SIG:
        return {"format": "rar", "units": [], "entries": None, "batch_bytes": TRUNCATE_STEP,
                "reasons": ["RAR 4 archives are not supported in place (RAR 5 is). Use Keep Archive mode."]}
    else:
        raise ArchiveError("Not a RAR archive.")
    if not units:
        reasons.append("archive has no entries")
    if not get_7z_tool_path():
        reasons.append("the 7-Zip engine (7z.exe) is needed to decode RAR")
    return {"format": "rar", "units": units, "entries": entries,
            "reasons": sorted(set(reasons)), "batch_bytes": TRUNCATE_STEP}


def scan_7z(vs):
    if not HAS_PY7ZR:
        raise ArchiveError("py7zr is not installed")
    vs.seek(0)
    z = py7zr.SevenZipFile(vs, mode="r")
    reasons = []
    if z.needs_password():
        raise ArchiveError("password-protected 7z")
    ms = z.header.main_streams
    folders = ms.unpackinfo.folders
    packpos, packsizes = ms.packinfo.packpositions, ms.packinfo.packsizes
    if len(packsizes) != len(folders):
        reasons.append("a block uses several packed streams (BCJ2?)")
    for f in folders:
        if any(c.get("method") == BCJ2_ID for c in f.coders):
            reasons.append("BCJ2 filter (not supported in place)")
            break
    counts = (ms.substreamsinfo.num_unpackstreams_folders if ms.substreamsinfo else [1] * len(folders))
    files = list(z.files)
    per_folder = [[] for _ in folders]
    empties, entries = [], []
    fi = 0
    for f in files:
        name = f.filename
        rel = safe_member_path(name)
        if rel is None:
            reasons.append(f"unsafe path {name!r}")
        if f.is_directory:
            entries.append((name + "/", 0, 0, "--", True, "--", "7z"))
            empties.append((name, rel, "dir"))
        elif f.emptystream:
            entries.append((name, 0, 0, "--", False, "--", "7z"))
            empties.append((name, rel, "file"))
        else:
            while fi < len(counts) and len(per_folder[fi]) >= counts[fi]:
                fi += 1
            if fi >= len(folders):
                reasons.append("inconsistent block table")
                break
            per_folder[fi].append((name, rel, f.uncompressed))
            entries.append((name, f.uncompressed, 0, "--", False,
                            f"{f.crc32:08X}" if getattr(f, "crc32", None) else "--", "7z"))
    units = []
    if len(packsizes) == len(folders):
        for k, fl in enumerate(per_folder):
            units.append({"off": 32 + packpos[k], "out": sum(x[2] for x in fl), "files": fl,
                          "span": packsizes[k]})
    tail_start = 32 + max((packpos[k] + packsizes[k] for k in range(len(packsizes))), default=0)
    if not units:
        reasons.append("archive has no data blocks")
    return {"format": "7z", "units": units, "entries": entries, "reasons": sorted(set(reasons)),
            "tail_start": tail_start, "empties": empties, "z": z, "batch_bytes": 0}


def sniff_format(vs, path):
    vs.seek(0)
    head = vs.read(8)
    if head.startswith(b"PK"):
        return "zip"
    if head.startswith(SEVENZ_SIG):
        return "7z"
    if head.startswith(b"Rar!"):
        return "rar"
    vs.seek(257)
    if vs.read(5) == b"ustar":
        return "tar"
    base = path
    m = _VOL_RE.match(path)
    if m:
        base = m.group(1)
    low = base.lower()
    if low.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar.zst")):
        return "tar.compressed"
    ext = os.path.splitext(low)[1]
    return {".zip": "zip", ".7z": "7z", ".rar": "rar", ".tar": "tar"}.get(ext, "unknown")


def analyze_archive(path):
    """Inspect an archive (read-only). Returns a dict the GUI/engine can plan from.

    keys: format, entries, units (asc [(offset, out)]), inplace_ok, reasons,
          archive_size, volumes, batch_bytes, largest, total_out, methods
    """
    first = first_volume(path)
    vs = VolumeSet.discover(first)
    try:
        fmt = sniff_format(vs, first)
        info = {"format": fmt, "entries": None, "units": [], "inplace_ok": False, "reasons": [],
                "archive_size": vs.size(), "volumes": len(vs.paths), "batch_bytes": 0,
                "largest": ("", 0), "total_out": 0, "methods": set()}
        if fmt == "zip":
            try:
                zf = zipfile.ZipFile(vs, "r")
            except zipfile.BadZipFile:
                raise ArchiveError("Not a valid ZIP file.")
            rep = analyze_zip(zf, vs.size())
            ents = []
            for f in zf.infolist():
                ents.append((f.filename, f.file_size, f.compress_size, _mtime_str(time.mktime(f.date_time + (0, 0, -1))),
                             f.is_dir(), f"{f.CRC:08X}" if f.CRC else "--",
                             ZIP_METHOD_NAMES.get(f.compress_type, f"Type {f.compress_type}")))
            info.update(entries=ents, units=rep["layout"], inplace_ok=rep["inplace_ok"],
                        reasons=rep["reasons"], methods=rep["methods"])
        elif fmt == "tar":
            sc = scan_tar(vs)
            info.update(entries=sc["entries"], units=[(u["off"], u["out"]) for u in sc["units"]],
                        reasons=sc["reasons"], inplace_ok=not sc["reasons"])
        elif fmt == "rar":
            sc = scan_rar(vs)
            info.update(entries=sc["entries"], units=[(u["off"], u["out"]) for u in sc["units"]],
                        reasons=sc["reasons"], inplace_ok=not sc["reasons"], batch_bytes=sc["batch_bytes"])
        elif fmt == "7z":
            try:
                sc = scan_7z(vs)
                info.update(entries=sc["entries"], units=[(u["off"], u["out"]) for u in sc["units"]],
                            reasons=sc["reasons"], inplace_ok=not sc["reasons"])
            except ArchiveError as e:
                info["reasons"] = [str(e)]
            except Exception as e:
                info["reasons"] = [f"cannot read 7z structure ({type(e).__name__})"]
        elif fmt == "tar.compressed":
            info["reasons"] = ["compressed tar streams cannot be cut from the end"]
        else:
            info["reasons"] = ["unrecognised archive format"]
        big = ("", 0)
        if info["entries"]:
            for e in info["entries"]:
                if not e[4] and e[1] > big[1]:
                    big = (e[0], e[1])
            info["total_out"] = sum(e[1] for e in info["entries"])
        info["largest"] = big
        return info
    finally:
        vs.close()


# ------------------------------------------------------------------------------
# Core Engine Worker
# ------------------------------------------------------------------------------
class ShrinkZipEngineWorker(threading.Thread):
    def __init__(self, archive_path, extract_dir, mode_shrink, delete_on_finish, conflict_mode, msg_queue):
        super().__init__(daemon=True)
        self.archive_path = first_volume(os.path.abspath(archive_path))
        self.extract_dir = os.path.abspath(extract_dir)
        self.mode_shrink = mode_shrink
        self.delete_on_finish = delete_on_finish
        self.conflict_mode = conflict_mode
        self.msg_queue = msg_queue
        self._stop_requested = threading.Event()
        self._active_subprocess = None
        self._partial = None

    def request_stop(self):
        self._stop_requested.set()
        if self._active_subprocess and self._active_subprocess.poll() is None:
            try:
                self._active_subprocess.terminate()
            except Exception:
                pass

    def _log(self, text):
        self.msg_queue.put(('log', text))

    # ---------------------------------------------------------------- dispatch
    def run(self):
        try:
            tool_7z = get_7z_tool_path()
            if os.path.exists(sidecar_path(self.archive_path)):
                fmt = load_journal(self.archive_path)[0].get("format")
            else:
                vs = VolumeSet.discover(self.archive_path)
                try:
                    fmt = sniff_format(vs, self.archive_path)
                finally:
                    vs.close()
            if self.mode_shrink:
                self._log("[INIT] Engine engaged. Mode: In-Place Extraction (Low Disk Space)")
            else:
                self._log("[INIT] Engine engaged. Mode: Keep Archive Extraction (Standard)")
            self._log(f"[ARCHIVE] {os.path.basename(self.archive_path)}  [{fmt}]")
            self._log(f"[TARGET] {self.extract_dir}")

            if fmt == "zip":
                self._extract_zip(tool_7z)
            elif fmt in ("tar", "7z", "rar") and self.mode_shrink:
                self._run_inplace_units(fmt, tool_7z)
            elif fmt in ("tar", "tar.compressed"):
                self._extract_tar_standard()
            elif fmt in ("7z", "rar"):
                if tool_7z:
                    self._extract_with_native_7z(tool_7z)
                elif fmt == "7z":
                    self._extract_7z_fallback()
                else:
                    self._extract_rar_fallback()
            else:
                self._extract_zip(tool_7z)
        except Exception as e:
            self.msg_queue.put(('error', f"{type(e).__name__}: {e}"))

    # ---------------------------------------------------------------- helpers
    def _open_target(self, rel, directory=False):
        """Absolute target path inside the destination, refusing anything that escapes it
        (including through symlinks that earlier entries created)."""
        base = os.path.realpath(self.extract_dir)
        target = os.path.abspath(os.path.join(self.extract_dir, rel)) if rel else self.extract_dir
        if os.path.commonpath([os.path.abspath(self.extract_dir), target]) != os.path.abspath(self.extract_dir):
            raise ArchiveError(f"path escapes the destination: {rel!r}")
        parent = target if directory else os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        if os.path.commonpath([base, os.path.realpath(parent)]) != base:
            raise ArchiveError(f"path goes through a link out of the destination: {rel!r}")
        return target

    @staticmethod
    def _truncate(vs, new_size):
        for attempt in range(5):
            try:
                vs.truncate(new_size)
                vs.fsync()
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)

    @staticmethod
    def _fsync_path(path):
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _progress(self, last_ui, meter, extracted_bytes, total_bytes, idx, total_units, name,
                  extracted_count, total_files, cur_size, initial_size, force=False):
        now = time.time()
        if not force and now - last_ui < 0.1:
            return last_ui
        speed, eta, elapsed = meter.update(extracted_bytes)
        pct = (extracted_bytes / total_bytes) if total_bytes > 0 else ((idx + 1) / max(1, total_units))
        self.msg_queue.put(('progress', {
            'progress_pct': min(1.0, pct), 'current_file': name,
            'extracted_count': extracted_count, 'total_files': total_files,
            'extracted_bytes': extracted_bytes, 'total_bytes': total_bytes,
            'current_archive_size': cur_size, 'initial_size': initial_size,
            'speed': speed, 'eta': eta, 'elapsed': elapsed}))
        return now

    # ------------------------------------------------------------------- ZIP
    def _extract_zip(self, tool_7z):
        vs = zf = None
        extracted_count = extracted_bytes = 0
        start_time = time.time()
        outcome = None
        self._partial = None
        try:
            try:
                mode = os.stat(self.archive_path).st_mode
                if self.mode_shrink and not (mode & stat.S_IWRITE):
                    os.chmod(self.archive_path, mode | stat.S_IWRITE)
            except Exception:
                pass

            if os.path.exists(sidecar_path(self.archive_path)):
                self._log("[RESUME] Interrupted in-place extraction found. Restoring the archive index...")
                remaining = recover_archive(self.archive_path, self._log)
                if remaining == 0:
                    VolumeSet.discover(self.archive_path).delete_all()
                    self.msg_queue.put(('done', "Everything had already been extracted. Archive removed."))
                    return

            vs = VolumeSet.discover(self.archive_path, writable=self.mode_shrink)
            initial_size = vs.size()
            self._log(f"[ARCHIVE] {format_bytes(initial_size)} in {len(vs.paths)} volume(s)")
            try:
                zf = zipfile.ZipFile(vs, "r")
            except zipfile.BadZipFile:
                raise ArchiveError("Not a valid ZIP file (or an unsupported format).")
            infos = zf.infolist()
            if not infos:
                self.msg_queue.put(('done', "Archive is empty."))
                return

            report = analyze_zip(zf, initial_size)
            if report["encrypted"]:
                self.msg_queue.put(('error', f"Archive contains {report['encrypted']} password-protected file(s). "
                                             "Password extraction is not supported. Nothing was changed."))
                return
            unsupported = [m for m in report["methods"] if m not in SUPPORTED_ZIP_METHODS]
            if unsupported:
                names = ", ".join(ZIP_METHOD_NAMES.get(m, f"method {m}") for m in sorted(unsupported))
                if tool_7z:
                    self._log(f"[INFO] This ZIP uses {names}, which cannot be read in place. "
                              "Using the native 7-Zip engine (standard extraction, archive kept).")
                    zf.close(); vs.close(); zf = vs = None
                    self._extract_with_native_7z(tool_7z)
                else:
                    self.msg_queue.put(('error', f"This ZIP uses {names}, which this build cannot extract. "
                                                 "Nothing was changed."))
                return
            if self.mode_shrink and not report["inplace_ok"]:
                self.msg_queue.put(('error', "In-place extraction refused: " + "; ".join(report["reasons"]) +
                                             ". Nothing was changed. Use Keep Archive mode instead."))
                return

            os.makedirs(self.extract_dir, exist_ok=True)
            total_files = len(infos)
            total_bytes = sum(i.file_size for i in infos)
            meter = _Meter(total_bytes)

            if self.mode_shrink:
                write_resume_sidecar(self.archive_path, vs, zf)
                self._log(f"[SAFE] Archive index saved to {os.path.basename(sidecar_path(self.archive_path))} "
                          "(keep this file until extraction finishes).")
                order = sorted(infos, key=lambda i: i.header_offset, reverse=True)
            else:
                order = sorted(infos, key=lambda i: i.header_offset)

            cur_size, last_ui, last_idx = initial_size, 0.0, len(order) - 1
            for idx, entry in enumerate(order):
                if self._stop_requested.is_set():
                    outcome = ('stopped', None)
                    break
                rel = safe_member_path(entry.filename)
                if rel is None:
                    self._log(f"[SEC] Skipped unsafe path: {entry.filename}")
                    continue
                if entry.is_dir() or entry.filename.endswith(("/", "\\")):
                    self._open_target(rel, directory=True)
                else:
                    target = self._open_target(rel)
                    if os.path.exists(target):
                        try:
                            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                        except Exception:
                            pass
                    self._partial = target
                    with zf.open(entry) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                        dst.flush()
                        os.fsync(dst.fileno())
                    self._partial = None
                    try:
                        dt = time.mktime(entry.date_time + (0, 0, -1))
                        os.utime(target, (dt, dt))
                    except Exception:
                        pass
                    extracted_count += 1
                    extracted_bytes += entry.file_size
                if self.mode_shrink:
                    cut = entry.header_offset
                    if cur_size - cut >= TRUNCATE_STEP or idx == last_idx:
                        freed = cur_size - cut
                        self._truncate(vs, cut)
                        cur_size = cut
                        self._log(f"[SHRINK] Archive cut at offset 0x{cut:X}: now {format_bytes(cur_size)} "
                                  f"(freed {format_bytes(freed)})")
                last_ui = self._progress(last_ui, meter, extracted_bytes, total_bytes, idx, len(order),
                                         rel or entry.filename, extracted_count, total_files, cur_size,
                                         initial_size, force=(idx == last_idx))
            else:
                outcome = ('done', None)
        except Exception as e:
            outcome = ('error', f"{type(e).__name__}: {e}")
            if self._partial:
                try:
                    os.remove(self._partial)
                except OSError:
                    pass
        finally:
            for h in (zf, vs):
                try:
                    if h:
                        h.close()
                except Exception:
                    pass
        if outcome is None:
            return
        self._finish_zip(outcome, extracted_bytes, extracted_count, start_time)

    def _finish_zip(self, outcome, extracted_bytes, extracted_count, start_time):
        kind, text = outcome
        resumable = os.path.exists(sidecar_path(self.archive_path))
        if kind == 'done':
            if resumable:
                VolumeSet.discover(self.archive_path).delete_all()
                _clear_journal(self.archive_path)
                self._log("[PURGE] Archive fully consumed and removed. Space reclaimed.")
            elif self.delete_on_finish:
                VolumeSet.discover(self.archive_path).delete_all()
                self._log("[PURGE] Original archive deleted (extraction fully succeeded).")
            self.msg_queue.put(('done', f"Extraction finished in {format_time(time.time() - start_time)}!\n"
                                        f"Total processed: {format_bytes(extracted_bytes)} across {extracted_count:,} files."))
            return
        note = ""
        if resumable:
            try:
                left = recover_archive(self.archive_path, self._log)
                if left:
                    note = (f"\n\nThe archive is a valid ZIP again with {left:,} entries left. "
                            "Press Start to continue where it stopped.")
            except Exception as err:
                note = ("\n\nThe archive index is saved in "
                        f"'{os.path.basename(sidecar_path(self.archive_path))}'. Free some disk space and press Start "
                        f"to resume. (Restore error: {err})")
        if kind == 'stopped':
            self._log("[ABORT] Process stopped by user.")
            self.msg_queue.put(('stopped', "Process halted safely. No data was lost." + note))
        else:
            self.msg_queue.put(('error', text + note))

    # ------------------------------------------------- tar / 7z / RAR in place
    def _run_inplace_units(self, fmt, tool_7z):
        start_time = time.time()
        vs = None
        ad = None
        outcome = None
        extracted_count = extracted_bytes = 0
        cut_happened = False
        self._partial = None
        try:
            if os.path.exists(sidecar_path(self.archive_path)) and fmt == "7z":
                self._log("[RESUME] Interrupted in-place extraction found. Resuming from the saved header...")
            vs = VolumeSet.discover(self.archive_path, writable=True)
            initial_size = vs.size()
            ad = {"tar": _TarAdapter, "7z": _SevenZAdapter, "rar": _RarAdapter}[fmt](self, vs, tool_7z)
            ad.prepare()
            os.makedirs(self.extract_dir, exist_ok=True)
            units = ad.units
            total_files = sum(1 for u in units if u["kind"] == "file") if fmt != "7z" else sum(len(u["files"]) for u in units)
            total_bytes = sum(u["out"] for u in units)
            meter = _Meter(total_bytes)
            desc = list(reversed(units))
            batches = make_batches(desc, ad.batch_bytes)
            cur_size, last_ui = vs.size(), 0.0
            self._log(f"[PLAN] {len(units):,} unit(s) in {len(batches):,} step(s); freed from the end of the archive.")
            for bi, batch in enumerate(batches):
                if self._stop_requested.is_set():
                    outcome = ('stopped', None)
                    break
                n_files, n_bytes = ad.extract(batch)
                extracted_count += n_files
                extracted_bytes += n_bytes
                cut = batch[-1]["off"]
                last = bi == len(batches) - 1
                if cur_size - cut >= TRUNCATE_STEP or last:
                    freed = cur_size - cut
                    self._truncate(vs, cut)
                    cur_size = cut
                    cut_happened = True
                    self._log(f"[SHRINK] Archive cut at offset 0x{cut:X}: now {format_bytes(cur_size)} "
                              f"(freed {format_bytes(freed)})")
                last_ui = self._progress(last_ui, meter, extracted_bytes, total_bytes, bi, len(batches),
                                         batch[-1].get("name") or ad.label(batch), extracted_count, total_files,
                                         cur_size, initial_size, force=last)
            else:
                outcome = ('done', None)
        except Exception as e:
            outcome = ('error', f"{type(e).__name__}: {e}")
            if self._partial:
                try:
                    os.remove(self._partial)
                except OSError:
                    pass
        finally:
            if ad:
                ad.close()
        kind, text = outcome
        if kind == 'done':
            if vs:
                vs.delete_all()
            _clear_journal(self.archive_path)
            self._log("[PURGE] Archive fully consumed and removed. Space reclaimed.")
            self.msg_queue.put(('done', f"Extraction finished in {format_time(time.time() - start_time)}!\n"
                                        f"Total processed: {format_bytes(extracted_bytes)} across {extracted_count:,} files."))
            return
        if vs:
            vs.close()
        note = ad.interrupted_note() if (ad and cut_happened) else ""
        if kind == 'stopped':
            self._log("[ABORT] Process stopped by user.")
            self.msg_queue.put(('stopped', "Process halted safely. No data was lost." + note))
        else:
            self.msg_queue.put(('error', text + note))

    # ------------------------------------------------------- standard: tar
    def _extract_tar_standard(self):
        start_time = time.time()
        fh = vs = None
        try:
            archive_size = os.path.getsize(self.archive_path)
            if _VOL_RE.match(self.archive_path):
                vs = VolumeSet.discover(self.archive_path)
                src, archive_size = vs, vs.size()
            else:
                fh = open(self.archive_path, "rb")
                src = fh
            os.makedirs(self.extract_dir, exist_ok=True)
            done_files = 0
            last_ui = 0.0
            with tarfile.open(fileobj=src, mode="r:*") as tf:
                for ti in tf:
                    if self._stop_requested.is_set():
                        self.msg_queue.put(('stopped', "Process halted by user. The archive was not modified."))
                        return
                    rel = safe_member_path(ti.name.lstrip("/"))
                    if rel is None or ti.islnk() or ti.isdev() or ti.isfifo() or ti.issparse():
                        self._log(f"[SKIP] Not extracted (unsafe or unsupported entry): {ti.name}")
                        continue
                    if ti.isdir():
                        self._open_target(rel, directory=True)
                    elif ti.issym():
                        target = self._open_target(rel)
                        try:
                            if os.path.lexists(target):
                                os.remove(target)
                            os.symlink(ti.linkname, target)
                        except (OSError, NotImplementedError):
                            self._log(f"[SKIP] Could not create symlink: {ti.name}")
                    elif ti.isreg():
                        target = self._open_target(rel)
                        with tf.extractfile(ti) as s, open(target, "wb") as d:
                            shutil.copyfileobj(s, d, 1024 * 1024)
                        try:
                            os.utime(target, (ti.mtime, ti.mtime))
                        except Exception:
                            pass
                        done_files += 1
                    now = time.time()
                    if now - last_ui >= 0.1:
                        last_ui = now
                        pos = fh.tell() if fh else src.tell()
                        self.msg_queue.put(('progress', {
                            'progress_pct': min(0.99, pos / max(1, archive_size)), 'current_file': rel,
                            'extracted_count': done_files, 'total_files': 0, 'extracted_bytes': 0, 'total_bytes': 0,
                            'current_archive_size': archive_size, 'initial_size': archive_size,
                            'speed': "-- MB/s", 'eta': "--:--", 'elapsed': format_time(now - start_time)}))
            if self.delete_on_finish:
                if fh:
                    fh.close(); fh = None
                (vs or VolumeSet.discover(self.archive_path)).delete_all()
                self._log("[PURGE] Original archive deleted (extraction fully succeeded).")
            self.msg_queue.put(('done', f"Extraction finished in {format_time(time.time() - start_time)}!\n"
                                        f"{done_files:,} files extracted."))
        except Exception as e:
            self.msg_queue.put(('error', f"tar extraction failed: {e}. The archive was NOT modified or deleted."))
        finally:
            for h in (fh, vs):
                try:
                    if h:
                        h.close()
                except Exception:
                    pass

    # --------------------------------------------------- standard: native 7-Zip
    def _extract_with_native_7z(self, tool_path):
        try:
            start_time = time.time()
            self._log(f"[7-ZIP ENGINE] Extracting: {os.path.basename(self.archive_path)}")
            os.makedirs(self.extract_dir, exist_ok=True)
            archive_size = os.path.getsize(self.archive_path)
            cmd = [tool_path, "x", "-aoa" if self.conflict_mode == "overwrite" else "-aos",
                   "-bsp1", "-y", f"-o{self.extract_dir}", self.archive_path]
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    startupinfo=_hidden_startupinfo())
            self._active_subprocess = proc
            tail = []
            for line in iter(proc.stdout.readline, ''):
                s = line.replace('\b', '').strip()
                if not s:
                    continue
                tail.append(s)
                del tail[:-8]
                if "%" in s:
                    try:
                        pct = float([p for p in s.split() if p.endswith("%")][0].rstrip("%")) / 100.0
                        elapsed = time.time() - start_time
                        eta = format_time(elapsed / pct - elapsed) if pct > 0.05 else "--:--"
                        self.msg_queue.put(('progress', {
                            'progress_pct': pct, 'current_file': s, 'extracted_count': int(pct * 100),
                            'total_files': 100, 'extracted_bytes': 0, 'total_bytes': 0,
                            'current_archive_size': archive_size, 'initial_size': archive_size,
                            'speed': "-- MB/s", 'eta': eta, 'elapsed': format_time(elapsed)}))
                    except Exception:
                        pass
                elif any(k in s.lower() for k in ("extracting", "everything is ok", "error", "warning")):
                    self._log(f"[7Z] {s}")
            proc.stdout.close()
            code = proc.wait()
            if self._stop_requested.is_set():
                self.msg_queue.put(('stopped', "Process halted by user. The archive was not modified."))
                return
            if code >= 2:
                self.msg_queue.put(('error', f"7-Zip failed (exit code {code}). The archive was NOT modified or deleted.\n"
                                             + "\n".join(tail[-4:])))
                return
            if code == 1:
                self.msg_queue.put(('done', "Finished with 7-Zip warnings (some files could not be processed). "
                                            "The archive was kept. Check the log."))
                return
            if self.delete_on_finish:
                VolumeSet.discover(self.archive_path).delete_all()
                self._log("[PURGE] Original archive deleted (extraction fully succeeded).")
            self.msg_queue.put(('done', f"Extraction successfully completed in {format_time(time.time() - start_time)}!"))
        except Exception as e:
            self.msg_queue.put(('error', f"Native extraction error: {e}"))

    def _extract_7z_fallback(self):
        if not HAS_PY7ZR:
            self.msg_queue.put(('error', "7-Zip binary or py7zr library required to extract .7z files."))
            return
        try:
            start_time = time.time()
            with py7zr.SevenZipFile(self.archive_path, mode='r') as archive:
                archive.extractall(path=self.extract_dir)
            if self.delete_on_finish and os.path.exists(self.archive_path):
                os.remove(self.archive_path)
            self.msg_queue.put(('done', f"7Z Extraction complete in {format_time(time.time() - start_time)}!"))
        except Exception as e:
            self.msg_queue.put(('error', f"7Z Extraction failed: {e}"))

    def _extract_rar_fallback(self):
        if not HAS_RARFILE:
            self.msg_queue.put(('error', "7-Zip binary or rarfile library required to extract .rar files."))
            return
        try:
            start_time = time.time()
            with rarfile.RarFile(self.archive_path) as rf:
                rf.extractall(path=self.extract_dir)
            if self.delete_on_finish and os.path.exists(self.archive_path):
                os.remove(self.archive_path)
            self.msg_queue.put(('done', f"RAR Extraction complete in {format_time(time.time() - start_time)}!"))
        except Exception as e:
            self.msg_queue.put(('error', f"RAR Extraction failed: {e}"))


# ------------------------------------------------------------------------------
# Format adapters for the unit-based in-place loop
# ------------------------------------------------------------------------------
class _Adapter:
    batch_bytes = 0

    def __init__(self, worker, vs, tool_7z):
        self.w, self.vs, self.tool = worker, vs, tool_7z
        self.units = []

    def label(self, batch):
        return f"{len(batch)} item(s)"

    def close(self):
        pass

    def interrupted_note(self):
        return ""

    def _refuse(self, reasons):
        raise ArchiveError("In-place extraction refused: " + "; ".join(reasons) +
                           ". Nothing was changed. Use Keep Archive mode instead.")


class _TarAdapter(_Adapter):
    def prepare(self):
        sc = scan_tar(self.vs)
        self.tf = sc["tf"]
        if sc["reasons"]:
            self._refuse(sc["reasons"])
        self.units = sc["units"]

    def extract(self, batch):
        files = nbytes = 0
        for u in batch:
            ti, rel = u["ti"], u["rel"]
            if u["kind"] == "dir":
                self.w._open_target(rel, directory=True)
                continue
            target = self.w._open_target(rel)
            if u["kind"] == "symlink":
                try:
                    if os.path.lexists(target):
                        os.remove(target)
                    os.symlink(ti.linkname, target)
                except (OSError, NotImplementedError):
                    self.w._log(f"[SKIP] Could not create symlink (needs privileges on Windows): {u['name']}")
                continue
            self.w._partial = target
            with self.tf.extractfile(ti) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            self.w._partial = None
            try:
                os.utime(target, (ti.mtime, ti.mtime))
                if os.name != "nt":
                    os.chmod(target, ti.mode & 0o777)
            except Exception:
                pass
            files += 1
            nbytes += u["out"]
        return files, nbytes

    def label(self, batch):
        return batch[-1]["name"]

    def interrupted_note(self):
        try:                                   # close the (cut) tar properly so tools see a clean archive
            vs = VolumeSet.discover(self.w.archive_path, writable=True)
            if vs.size() > 0:
                vs.seek(vs.size())
                vs.write(b"\0" * 1024)
                vs.fsync()
            vs.close()
        except Exception:
            pass
        return "\n\nThe archive is still a valid tar holding only the entries not yet extracted. Press Start to continue."


class _RarAdapter(_Adapter):
    batch_bytes = TRUNCATE_STEP

    def prepare(self):
        sc = scan_rar(self.vs)
        if sc["reasons"]:
            self._refuse(sc["reasons"])
        self.units = sc["units"]

    def extract(self, batch):
        names, expect = [], []
        for u in batch:
            if u["kind"] == "dir":
                self.w._open_target(u["rel"], directory=True)
            else:
                self.w._open_target(u["rel"])           # creates + validates the parent folder
                names.append(u["name"])
                expect.append(u)
        if not names:
            return 0, 0
        listing = self.w.archive_path + ".shrinkzip-list.tmp"
        with open(listing, "w", encoding="utf-8") as f:
            f.write("\n".join(names) + "\n")
        try:
            cmd = [self.tool, "x", "-aoa", "-y", "-spd", "-scsUTF-8", f"-o{self.w.extract_dir}",
                   self.w.archive_path, f"@{listing}"]
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  startupinfo=_hidden_startupinfo())
        finally:
            try:
                os.remove(listing)
            except OSError:
                pass
        # A cut RAR has no end-of-archive block, so 7-Zip exits non-zero even on success.
        # Trust nothing but the files: size and CRC-32 must match the archive's own header.
        nbytes = 0
        for u in expect:
            target = os.path.join(self.w.extract_dir, u["rel"])
            if not os.path.isfile(target) or os.path.getsize(target) != u["size"]:
                raise ArchiveError(f"7-Zip did not extract {u['name']!r} completely "
                                   f"(exit code {proc.returncode}). Nothing was cut from the archive.")
            if u.get("crc") is not None:
                crc = 0
                with open(target, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        crc = zlib.crc32(chunk, crc)
                if crc & 0xFFFFFFFF != u["crc"]:
                    raise ArchiveError(f"Checksum mismatch for {u['name']!r}. Nothing was cut from the archive.")
            self.w._fsync_path(target)
            nbytes += u["out"]
        return len(expect), nbytes

    def label(self, batch):
        return batch[-1]["name"]

    def interrupted_note(self):
        return "\n\nWhat is left of the archive is a valid RAR prefix (it only lacks its end marker). Press Start to continue."


class _SevenZAdapter(_Adapter):
    def prepare(self):
        w, vs = self.w, self.vs
        archive = w.archive_path
        resume = os.path.exists(sidecar_path(archive))
        if resume:
            meta, tail = load_journal(archive)
            if _journal_untouched(vs, meta):
                _clear_journal(archive)
                resume = False
            else:
                _journal_matches(archive, vs, meta)
        if resume:
            view = TailView(vs, meta["tail_start"], tail)
            sc = scan_7z(view)
        else:
            sc = scan_7z(vs)
        self.z, self.sc = sc["z"], sc
        if sc["reasons"]:
            self._refuse(sc["reasons"])
        if not resume:
            size = vs.size()
            vs.seek(sc["tail_start"])
            tail = vs.read(max(0, size - sc["tail_start"]))
            self._verify_saved_header(vs, sc["tail_start"], tail)
            meta = {"version": 2, "format": "7z", "tail_start": sc["tail_start"],
                    "parts": [[os.path.basename(p), l] for p, l in zip(vs.paths, vs.lens)],
                    "fingerprints": _fingerprints(vs, size)}
            save_journal(archive, meta, tail)
            w._log(f"[SAFE] Archive header saved to {os.path.basename(sidecar_path(archive))} "
                   "(keep this file until extraction finishes).")
        os.makedirs(w.extract_dir, exist_ok=True)
        for name, rel, kind in sc["empties"]:            # folders and empty files carry no data
            if kind == "dir":
                w._open_target(rel, directory=True)
            else:
                open(w._open_target(rel), "wb").close()
        remaining = vs.size()
        self.units = [dict(u, kind="file", name=None) for u in sc["units"] if u["off"] < remaining]
        for u in self.units:
            u["name"] = u["files"][0][0] if u["files"] else ""

    @staticmethod
    def _verify_saved_header(vs, tail_start, tail):
        """Prove the header we are about to save is complete BEFORE any byte is cut:
        its CRC must match the 7z start header, and py7zr must be able to open the
        archive through the exact virtual view a resume would use."""
        vs.seek(0)
        sh = vs.read(32)
        nho, nhs, nhc = struct.unpack_from("<QQI", sh, 12)
        hdr_start = 32 + nho
        if tail_start > hdr_start or hdr_start + nhs > tail_start + len(tail):
            raise ArchiveError("Cannot save the archive header safely (layout not understood). Nothing was changed.")
        piece = tail[hdr_start - tail_start: hdr_start - tail_start + nhs]
        if zlib.crc32(piece) & 0xFFFFFFFF != nhc:
            raise ArchiveError("Saved archive header failed its checksum. Nothing was changed.")
        try:
            probe = py7zr.SevenZipFile(TailView(vs, tail_start, tail), mode="r")
            probe.close()
        except Exception as e:
            raise ArchiveError(f"Cannot re-open the archive from its saved header ({e}). Nothing was changed.")

    def extract(self, batch):
        names, expect = [], []
        for u in batch:
            for name, rel, size in u["files"]:
                self.w._open_target(rel)
                names.append(name)
                expect.append((rel, size))
        if not names:
            return 0, 0
        self.z.extract(path=self.w.extract_dir, targets=names)    # py7zr verifies every CRC-32
        self.z.reset()
        nbytes = 0
        for rel, size in expect:
            target = os.path.join(self.w.extract_dir, rel)
            if not os.path.isfile(target) or os.path.getsize(target) != size:
                raise ArchiveError(f"{rel!r} was not extracted completely. Nothing was cut from the archive.")
            self.w._fsync_path(target)
            nbytes += size
        return len(expect), nbytes

    def label(self, batch):
        return batch[-1]["name"]

    def close(self):
        try:
            self.z.close()
        except Exception:
            pass

    def interrupted_note(self):
        return ("\n\nThe archive is paused in a resumable state: keep the archive AND its "
                f"'{os.path.basename(sidecar_path(self.w.archive_path))}' file, and press Start to continue. "
                "(Other programs cannot open a half-extracted 7z.)")
