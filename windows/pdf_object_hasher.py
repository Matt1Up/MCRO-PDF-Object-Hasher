
#!/usr/bin/env python3
"""
pdf_object_hasher.py
Cross-platform Python version suitable for PyInstaller (--onefile).

Features:
- Watches ./pdf/ for new PDFs (watchdog) or runs one-shot "catch-up".
- Explodes PDFs with mutool extract into ./pdf-objects/<safe>/
- Hashes every extracted object; appends one row per object to objects.tsv (22 columns)
- Dedup-copies unique objects to ./hashed-objects/<sha256>.<ext> (first extension wins)
- Uses processed.tsv + per-PDF .processed.sha + in-flight lock file to prevent reprocessing
- Parses MCRO_* filename into Case Number / Filing Type / Filing Date (first 3 tokens after MCRO_)
- Extracts Author/Creator via exiftool; signatures (CN/time/ranges) via pdfsig (up to 4 blocks)
- Rebuilds hash-count.tsv (from column #4 = SHA256) after each PDF
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Optional: watchdog for live monitoring
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except Exception:
    HAS_WATCHDOG = False

ROOT = Path.cwd()
PDF_DIR = ROOT / "pdf"
OBJ_DIR = ROOT / "pdf-objects"
HASHED_DIR = ROOT / "hashed-objects"

OBJECTS_TSV = ROOT / "objects.tsv"
HASH_COUNT_TSV = ROOT / "hash-count.tsv"
PROCESSED_TSV = ROOT / "processed.tsv"

LOCK_DIR = ROOT / ".locks"
INFLIGHT_DIR = LOCK_DIR / "inflight"

# Schema: 22 columns
OBJECTS_HEADER = (
    "Case Number\tFiling Type\tFiling Date\t"
    "SHA256 Hash Value\tPdf File Name\tPdf Internal Object Path\tObject Type\tFont Name\t"
    "Sig #1 Common Name\tSig #2 Common Name\tAuthor\tCreator\tSig #3 Common Name\tSig #4 Common Name\t"
    "Sig #1 Signing Time\tSig #2 Signing Time\tSig #3 Signing Time\tSig #4 Signing Time\t"
    "Sig #1 Byte Ranges\tSig #2 Byte Ranges\tSig #3 Byte Ranges\tSig #4 Byte Ranges"
)

OLD_HEADER_5 = "SHA256 Hash Value\tPdf File Name\tPdf Internal Object Path\tObject Type\tFont Name"

PDF_EXT_RE = re.compile(r"\.pdf$", re.IGNORECASE)

def run_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    """Run a command and return (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"Command not found: {cmd[0]}"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"

def need_tool(name: str, check_args: Optional[List[str]] = None) -> None:
    """Verify a tool exists. Warn if missing, exit if core tool (mutool/sha256)."""
    if check_args is None:
        check_args = ["--version"]
    rc, out, err = run_cmd([name] + check_args)
    if rc == 127:
        if name in ("mutool",):
            sys.exit(f"Missing required external tool: {name}")
        # non-fatal for optional tools; we just warn
        print(f"NOTE: Optional tool not found: {name}", file=sys.stderr)

def ensure_layout() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    OBJ_DIR.mkdir(parents=True, exist_ok=True)
    HASHED_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    INFLIGHT_DIR.mkdir(parents=True, exist_ok=True)

    if not OBJECTS_TSV.exists() or OBJECTS_TSV.stat().st_size == 0:
        OBJECTS_TSV.write_text(OBJECTS_HEADER + "\n", encoding="utf-8")
    else:
        first = ""
        with OBJECTS_TSV.open("r", encoding="utf-8", errors="ignore") as f:
            first = f.readline().rstrip("\n")
        if first == OLD_HEADER_5:
            # migrate: prepend 3 blanks and append 14 blanks
            tmp = OBJECTS_TSV.with_suffix(".tsv.tmp")
            with tmp.open("w", encoding="utf-8", newline="") as out:
                out.write(OBJECTS_HEADER + "\n")
                with OBJECTS_TSV.open("r", encoding="utf-8", errors="ignore") as inp:
                    next(inp)  # skip old header
                    for line in inp:
                        line = line.rstrip("\n")
                        # 3 blanks + old row + 14 blanks
                        out.write("\t\t\t" + line + ("\t" * 14) + "\n")
            tmp.replace(OBJECTS_TSV)
        # else: leave custom headers alone; we will still append new-schema rows

    if not HASH_COUNT_TSV.exists():
        HASH_COUNT_TSV.write_text("", encoding="utf-8")
    if not PROCESSED_TSV.exists():
        PROCESSED_TSV.write_text("", encoding="utf-8")

def safe_name(pdf_name: str) -> str:
    base = re.sub(r"\.pdf$", "", pdf_name, flags=re.IGNORECASE)
    base = os.path.basename(base)
    return base.replace(" ", "_").replace("/", "_")

def file_sha256(path: Path, bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def wait_for_quiet_file(path: Path, tries: int = 10, delay: float = 0.3) -> None:
    last = -1
    for _ in range(tries):
        if not path.exists():
            time.sleep(delay)
            continue
        size = path.stat().st_size
        if size == last:
            return
        last = size
        time.sleep(delay)

def load_processed_shas() -> set:
    shas = set()
    if PROCESSED_TSV.exists():
        with PROCESSED_TSV.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split("\t")
                if parts and parts[0]:
                    shas.add(parts[0])
    return shas

def has_inflight(sha: str) -> bool:
    return (INFLIGHT_DIR / f"{sha}.lock").exists()

def mark_inflight(sha: str) -> None:
    (INFLIGHT_DIR / f"{sha}.lock").write_text(datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8")

def clear_inflight(sha: str) -> None:
    try:
        (INFLIGHT_DIR / f"{sha}.lock").unlink(missing_ok=True)
    except Exception:
        pass

def record_processed(sha: str, pdf_name: str, bytes_size: int, mtime_epoch: int) -> None:
    iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with PROCESSED_TSV.open("a", encoding="utf-8") as f:
        f.write(f"{sha}\t{pdf_name}\t{bytes_size}\t{mtime_epoch}\t{iso}\n")

def parse_mcro_fields(pdf_name: str) -> Tuple[str, str, str]:
    """If name starts with MCRO_, return (case, filing_type, filing_date); else blanks."""
    if not pdf_name.startswith("MCRO_"):
        return "", "", ""
    noext = PDF_EXT_RE.sub("", pdf_name)
    parts = noext.split("_")
    if len(parts) >= 4:
        # parts[0] = 'MCRO'
        return parts[1], parts[2], parts[3]
    return "", "", ""

SIG_BLOCK_RE = re.compile(r"^Signature\s#([1-4]):")
CN_RE = re.compile(r"Signer\sCertificate\sCommon\sName:\s(.*)$")
TIME_RE = re.compile(r"Signing\sTime:\s(.*)$")
RANGE_RE = re.compile(r"Signed\sRanges:\s(.*)$")

def normalize_sig_time(raw: str) -> str:
    """
    Try to convert 'Apr 11 2024 08:35:56' to '2024-04-11 08:35:56'.
    Fall back to raw if parsing fails.
    """
    for fmt in ("%b %d %Y %H:%M:%S", "%b %d %Y %H:%M", "%c"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    # some locales may include timezone or other text; attempt a mild cleanup
    try:
        tokens = raw.strip().split()
        # keep first 5 tokens: Mon DD YYYY HH:MM:SS
        if len(tokens) >= 5:
            s = " ".join(tokens[:5])
            dt = datetime.strptime(s, "%b %d %Y %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return raw.strip()

def parse_pdfsig(pdf_path: Path) -> Dict[str, str]:
    """Run pdfsig and parse up to 4 signature blocks; returns dict of SIG* keys."""
    rc, out, err = run_cmd(["pdfsig", str(pdf_path)])
    sigs = {
        "SIG1_CN": "", "SIG2_CN": "", "SIG3_CN": "", "SIG4_CN": "",
        "SIG1_TIME": "", "SIG2_TIME": "", "SIG3_TIME": "", "SIG4_TIME": "",
        "SIG1_RANGE": "", "SIG2_RANGE": "", "SIG3_RANGE": "", "SIG4_RANGE": "",
    }
    if rc != 0 or not out:
        return sigs

    cur = 0
    for line in out.splitlines():
        m = SIG_BLOCK_RE.match(line.strip())
        if m:
            cur = int(m.group(1))
            continue
        if not (1 <= cur <= 4):
            continue
        m = CN_RE.search(line)
        if m:
            sigs[f"SIG{cur}_CN"] = m.group(1).strip()
            continue
        m = TIME_RE.search(line)
        if m:
            sigs[f"SIG{cur}_TIME"] = normalize_sig_time(m.group(1))
            continue
        m = RANGE_RE.search(line)
        if m:
            sigs[f"SIG{cur}_RANGE"] = m.group(1).strip()
            continue
    return sigs

def get_author_creator(pdf_path: Path) -> Tuple[str, str]:
    """Use exiftool to grab Author and Creator; blanks if not present or tool missing."""
    rc, out, err = run_cmd(["exiftool", "-s", "-s", "-s", "-Author", "-Creator", str(pdf_path)])
    author, creator = "", ""
    if rc != 0:
        return author, creator
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # exiftool -s -s -s returns bare values in order of tags provided
        if author == "":
            author = line
        elif creator == "":
            creator = line
    return author, creator

FONT_EXTS = {".ttf", ".otf", ".ttc", ".woff", ".woff2", ".pfb", ".pfa"}

def get_font_name(obj_path: Path) -> str:
    """Best-effort font name via otfinfo or fc-scan; blank if unavailable."""
    ext = obj_path.suffix.lower()
    if ext not in FONT_EXTS:
        return ""
    rc, out, err = run_cmd(["otfinfo", "-i", str(obj_path)])
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("Full name:"):
                return line.split(":", 1)[1].strip()
    rc, out, err = run_cmd(["fc-scan", "--format", "%{family}\n", str(obj_path)])
    if rc == 0 and out.strip():
        return out.splitlines()[0].strip()
    return ""

def copy_object_if_new_by_hash(src: Path, sha: str, dest_dir: Path) -> None:
    """Copy obj to hashed-objects/<sha>.<ext> if not already present under any extension."""
    # Check <sha> and <sha>.*
    base = dest_dir / sha
    if base.exists():
        return
    for cand in dest_dir.glob(f"{sha}.*"):
        if cand.exists():
            return
    ext = src.suffix.lower()
    if len(ext) > 11:
        ext = ""  # ignore insane ext length
    dest = dest_dir / f"{sha}{ext}"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, tmp)
    tmp.replace(dest)

def update_hash_counts() -> None:
    """Rebuild hash-count.tsv from objects.tsv column #4 (skip header)."""
    if not OBJECTS_TSV.exists():
        HASH_COUNT_TSV.write_text("", encoding="utf-8")
        return
    counts = Counter()
    with OBJECTS_TSV.open("r", encoding="utf-8", errors="ignore") as f:
        first = True
        for line in f:
            if first:
                first = False
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 4 and parts[3]:
                counts[parts[3]] += 1
    # Sort by count desc, then hash
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    with HASH_COUNT_TSV.open("w", encoding="utf-8", newline="") as out:
        for h, c in rows:
            out.write(f"{h}\t{c}\n")

def extract_with_mutool(pdf_path: Path, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    rc, out, err = run_cmd(["mutool", "extract", str(pdf_path)])
    # mutool extracts into cwd; run in outdir
    if rc != 0:
        # retry by forcing cwd to outdir (Windows often needs cwd control)
        try:
            proc = subprocess.run(["mutool", "extract", str(pdf_path)], cwd=str(outdir), capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr or "mutool extract failed")
        except Exception as e:
            raise RuntimeError(f"mutool extract failed: {e}")
    else:
        # if happened in current dir, move items into outdir (defensive)
        # (Most builds require cwd control; above branch covers typical case)
        pass

def per_pdf_sig_and_meta(pdf_path: Path) -> Dict[str, str]:
    sig = parse_pdfsig(pdf_path)
    author, creator = get_author_creator(pdf_path)
    sig["AUTHOR"] = author
    sig["CREATOR"] = creator
    return sig

def process_pdf(pdf_path: Path) -> None:
    """Process one PDF end-to-end (idempotent)."""
    wait_for_quiet_file(pdf_path)
    if not pdf_path.exists():
        return

    pdf_name = pdf_path.name
    print(f"→  Examining: {pdf_name}")
    sha = file_sha256(pdf_path)
    size = pdf_path.stat().st_size
    mtime_epoch = int(pdf_path.stat().st_mtime)

    if has_inflight(sha):
        print(f"↷  Skipping (in-flight): {pdf_name}")
        return

    processed_shas = load_processed_shas()
    if sha in processed_shas:
        print(f"↷  Skipping (already processed): {pdf_name}")
        return

    safe = safe_name(pdf_name)
    outdir = OBJ_DIR / safe
    stamp = outdir / ".processed.sha"
    if stamp.exists() and stamp.read_text(encoding="utf-8", errors="ignore").strip() == sha:
        if sha not in processed_shas:
            record_processed(sha, pdf_name, size, mtime_epoch)
        print(f"↷  Skipping (stamp says processed): {pdf_name}")
        return

    # mark inflight
    mark_inflight(sha)
    try:
        print(f"→  Extracting objects from: {pdf_name}")
        extract_with_mutool(pdf_path, outdir)

        # Per-PDF fields
        case_num, filing_type, filing_date = parse_mcro_fields(pdf_name)
        sigmeta = per_pdf_sig_and_meta(pdf_path)

        # Iterate extracted files
        rows_added = 0
        for root, _, files in os.walk(outdir):
            for fn in files:
                if fn == ".processed.sha":
                    continue
                obj_path = Path(root) / fn
                rel = str(obj_path.relative_to(OBJ_DIR))
                h = file_sha256(obj_path)
                ext = obj_path.suffix.lower() if obj_path.suffix else ""
                if len(ext) > 11:
                    ext = ""
                font_name = get_font_name(obj_path) if ext in FONT_EXTS else ""

                # Write row
                row = [
                    case_num, filing_type, filing_date,
                    h, pdf_name, rel, ext, font_name,
                    sigmeta.get("SIG1_CN", ""), sigmeta.get("SIG2_CN", ""), sigmeta.get("AUTHOR", ""), sigmeta.get("CREATOR", ""),
                    sigmeta.get("SIG3_CN", ""), sigmeta.get("SIG4_CN", ""),
                    sigmeta.get("SIG1_TIME", ""), sigmeta.get("SIG2_TIME", ""), sigmeta.get("SIG3_TIME", ""), sigmeta.get("SIG4_TIME", ""),
                    sigmeta.get("SIG1_RANGE", ""), sigmeta.get("SIG2_RANGE", ""), sigmeta.get("SIG3_RANGE", ""), sigmeta.get("SIG4_RANGE", ""),
                ]
                with OBJECTS_TSV.open("a", encoding="utf-8", newline="") as f:
                    f.write("\t".join(row) + "\n")

                # Dedup store by hash
                copy_object_if_new_by_hash(obj_path, h, HASHED_DIR)
                rows_added += 1

        # stamp + ledger + counts
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(sha + "\n", encoding="utf-8")
        record_processed(sha, pdf_name, size, mtime_epoch)
        update_hash_counts()

        # Per-PDF row count (column #5 is pdf name)
        per_pdf = 0
        with OBJECTS_TSV.open("r", encoding="utf-8", errors="ignore") as f:
            next(f)  # header
            for line in f:
                if not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 5 and parts[4] == pdf_name:
                    per_pdf += 1
        print(f"✔  Processed {pdf_name} → {per_pdf} object-rows now recorded for this PDF")

    finally:
        clear_inflight(sha)

class PdfWatchHandler(FileSystemEventHandler):
    def __init__(self, poll_quiet_sec: float = 0.5):
        super().__init__()
        self.poll_quiet_sec = poll_quiet_sec

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if PDF_EXT_RE.search(p.name):
            time.sleep(self.poll_quiet_sec)
            process_pdf(p)

    def on_moved(self, event):
        if event.is_directory:
            return
        p = Path(event.dest_path)
        if PDF_EXT_RE.search(p.name):
            time.sleep(self.poll_quiet_sec)
            process_pdf(p)

def scan_once() -> None:
    print(f"Scanning for unprocessed PDFs in: {PDF_DIR}")
    if not PDF_DIR.exists():
        print("… no PDFs found.")
        return
    matched = False
    for p in sorted(PDF_DIR.iterdir()):
        if p.is_file() and PDF_EXT_RE.search(p.name):
            matched = True
            process_pdf(p)
    if not matched:
        print("… no PDFs found.")
    print("↺  hash-count.tsv updated.")

def monitor_loop(poll_interval: float = 5.0) -> None:
    print(f"🔎 Monitoring '{PDF_DIR}' for new PDFs… (Ctrl+C to stop)")
    if HAS_WATCHDOG:
        observer = Observer()
        handler = PdfWatchHandler()
        observer.schedule(handler, str(PDF_DIR), recursive=False)
        observer.start()
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
    else:
        print(f"NOTE: watchdog not installed. Polling every {poll_interval:.0f}s.")
        try:
            while True:
                scan_once()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            pass

def main():
    parser = argparse.ArgumentParser(description="PDF Object Hasher (PyInstaller-ready)")
    parser.add_argument("-m", "--monitor", action="store_true", help="Catch-up, then watch ./pdf/ for new PDFs")
    parser.add_argument("--poll", type=float, default=5.0, help="Polling seconds when watchdog is unavailable (default 5)")
    args = parser.parse_args()

    # Core external tools
    need_tool("mutool", ["--version"])
    # Optional tools for enriched metadata
    need_tool("pdfsig", ["-h"])
    need_tool("exiftool", ["-ver"])
    need_tool("otfinfo", ["-h"])
    need_tool("fc-scan", ["-h"])

    ensure_layout()
    scan_once()
    if args.monitor:
        monitor_loop(args.poll)

if __name__ == "__main__":
    main()
