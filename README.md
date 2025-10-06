# MCRO-PDF-Object-Hasher
Drop pdf files from the "MCRO Evidentiary Dataset" into the folder and all internal objects are extracted, hashed, and inserted into a csv table along with digital signature data (pdfsig) and metadata author and creator (exiftool)


The "MCRO Evidentiary Dataset" consists of 3,601 total PDF files which are purported to be authentic Minnesota Judicial Records - BUT THEY ARE NOT. Machine generated, mass forgery spanning 163 supposed cases in just this set. Among the fraud is:

**40 hash matched USPS "Returned Mail" filings:**
    https://MnCourtFraud.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.59.0.pdf
    https://storage.courtlistener.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.59.0.pdf

**1,165 cloned judicial signatures (hash matched):**
    https://MnCourtFraud.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.57.0.pdf
    https://storage.courtlistener.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.57.0.pdf

**371 cloned, judicial timestamps (hahs matched:**
    https://MnCourtFraud.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.58.0.pdf
    https://storage.courtlistener.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.58.0.pdf

**27 Cloned "Correspondence for Judicial Approval" filings:**
    https://MnCourtFraud.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.19.0.pdf
    https://storage.courtlistener.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.19.0.pdf

**Machine generated batches of fraudulent "Finding of Incompetency and Order" filings with clone X.509 signatures
  Same signing time, and same byte ranges, with 12 clones in a single group spanning different case numbers:**
    https://MnCourtFraud.com/recap/gov.uscourts.mnd.226147/gov.uscourts.mnd.226147.15.2.pdf
    
**Visit the Courtlistener Docket for Guertin v. Walz, et al. 25-cv-2670-PAM-DLM, D. Minn 2025 for more information:**
    https://www.MnCourtFraud.com/docket/70633540/guertin-v-walz/
    https://www.courtlistener.com/docket/70633540/guertin-v-walz/

**The "MCRO Evidentiary Dataset" of 3,601 AI gneerated Minnesota court records (all with valid, full doc, X.509 court sigs) can be
  downloaded at:** 
    https://MnCourtFraud.com/File/2017.zip - /2023.zip

**Backup mirrors available as file embedded, digitally signed PDF wrappers:**
    https://Matt1Up.Substack.com/p/evidence
    https://MnCourtFraud.substack.com/p/mcro-files



This repository/script watches a `pdf/` folder for new PDF files and, for each PDF:

1. **Explodes** it into per-object files (images, fonts, etc.) using `mutool extract`.
2. **Hashes** every extracted object (SHA‑256).
3. **Appends** a row per object to `objects.tsv` with rich metadata (see schema).
4. **Copies** each unique object (by hash) into `hashed-objects/` named `<sha256>.<ext>` (deduplicated by content).
5. **Maintains** a `processed.tsv` ledger so each PDF is processed **once**.
6. **Maintains** a `hash-count.tsv` summary of unique object hashes and counts.
7. **Parses** MCRO-style filenames to extract `Case Number`, `Filing Type`, `Filing Date`.
8. **Extracts** PDF metadata (Author/Creator via `exiftool`) and **digital-signature** fields (via `pdfsig`) — signed ranges, signer CNs, and signing times (normalized).

The script is **safe with spaces** in filenames, uses **locks** to avoid races, and supports both **one-shot** catch‑up and **monitor** mode.

---

## Quick Start

```bash
# 1) Place the script in a working directory and make it executable
chmod +x pdf_object_hasher.sh

# 2) Ensure directory layout exists (the script will also create them if missing)
mkdir -p pdf pdf-objects hashed-objects

# 3) Run once (catch-up any new PDFs in ./pdf/)
./pdf_object_hasher.sh

# 4) Or run continuously (watch for new PDFs)
./pdf_object_hasher.sh --monitor
```

> When running in monitor mode, if `inotifywait` is not installed, the script will **poll every 5 seconds**.

---

## Directory Layout

```
<working-dir>/
├─ pdf/                # Drop PDFs here (script reads from this folder)
├─ pdf-objects/        # Per-PDF extracted objects (1 subfolder per PDF; safe name)
│  └─ <safe>/
│     ├─ image-0001.png
│     ├─ font-0003.ttf
│     └─ .processed.sha     # stamp equal to the PDF’s SHA256 (used to prevent reprocessing)
├─ hashed-objects/     # Unique blobs copied by content hash → <sha256>.<ext>
├─ objects.tsv         # Main table (one row per extracted object, schema below)
├─ hash-count.tsv      # <sha256>\t<count> summary (rebuilt after each PDF)
├─ processed.tsv       # <sha256(pdf)>\t<filename>\t<bytes>\t<mtime_epoch>\t<processed_utc_iso>
└─ .locks/
   └─ inflight/        # in-flight guards: one file per PDF SHA during processing
```

---

## Outputs & Schemas

### `objects.tsv` (Tab-separated; **22 columns**)

Header (in order):

1. **Case Number** — from `MCRO_` filename (blank if not an MCRO file)  
2. **Filing Type** — from `MCRO_` filename (blank if not an MCRO file)  
3. **Filing Date** — from `MCRO_` filename (blank if not an MCRO file)  
4. **SHA256 Hash Value** — object content hash (used for deduplication)  
5. **Pdf File Name** — original PDF filename  
6. **Pdf Internal Object Path** — path relative to `pdf-objects/`  
7. **Object Type** — lowercase file extension (with leading dot), if present (e.g., `.png`, `.ttf`)  
8. **Font Name** — best‑effort (from `otfinfo` or `fc-scan`), blank if unknown  
9. **Sig #1 Common Name** — signer CN from `pdfsig` (signature block #1)  
10. **Sig #2 Common Name** — signer CN from `pdfsig` (signature block #2)  
11. **Author** — from `exiftool -Author`  
12. **Creator** — from `exiftool -Creator`  
13. **Sig #3 Common Name** — signer CN from `pdfsig` (signature block #3)  
14. **Sig #4 Common Name** — signer CN from `pdfsig` (signature block #4)  
15. **Sig #1 Signing Time** — normalized `YYYY-MM-DD HH:MM:SS`  
16. **Sig #2 Signing Time** — normalized `YYYY-MM-DD HH:MM:SS`  
17. **Sig #3 Signing Time** — normalized `YYYY-MM-DD HH:MM:SS`  
18. **Sig #4 Signing Time** — normalized `YYYY-MM-DD HH:MM:SS`  
19. **Sig #1 Byte Ranges** — e.g., `[0 - 157337], [169389 - 200740]`  
20. **Sig #2 Byte Ranges** — as above  
21. **Sig #3 Byte Ranges** — as above  
22. **Sig #4 Byte Ranges** — as above

> For non‑MCRO filenames, columns 1–3 remain blank.  
> If a PDF lacks signatures or metadata, the relevant columns are left blank.  
> A PDF’s signature/author/creator values repeat *per-object-row* (expected).

Example header row:
```
Case Number	Filing Type	Filing Date	SHA256 Hash Value	Pdf File Name	Pdf Internal Object Path	Object Type	Font Name	Sig #1 Common Name	Sig #2 Common Name	Author	Creator	Sig #3 Common Name	Sig #4 Common Name	Sig #1 Signing Time	Sig #2 Signing Time	Sig #3 Signing Time	Sig #4 Signing Time	Sig #1 Byte Ranges	Sig #2 Byte Ranges	Sig #3 Byte Ranges	Sig #4 Byte Ranges
```

### `hash-count.tsv`

- Built from **column 4** (SHA256) of `objects.tsv`.
- Format: `<sha256>\t<count>` sorted by descending `count`.

### `processed.tsv`

```
<sha256(pdf)>\t<pdf_filename>\t<bytes>\t<mtime_epoch>\t<processed_utc_iso>
```

> The script also writes a stamp file `pdf-objects/<safe>/.processed.sha` containing the same SHA to reinforce one‑time processing.

---

## MCRO Filename Parsing

If a file **starts with** `MCRO_`, the script splits the filename by underscores:

```
MCRO_<Case Number>_<Filing Type>_<Filing Date>_...
```

- `Case Number` = string after the first underscore and before the second.
- `Filing Type`  = string after the second underscore and before the third.
- `Filing Date`  = string after the third underscore and before the fourth.

If the filename does **not** start with `MCRO_`, these three columns are left blank.

---

## Digital Signatures & PDF Metadata

- **`pdfsig`** (Poppler) is used to collect up to **four** signature blocks:
  - Signer **Common Name** (CN) → `Sig #X Common Name`
  - **Signing Time** → normalized to `YYYY-MM-DD HH:MM:SS`
  - **Signed Ranges** → captured verbatim per block
- **`exiftool`** provides `Author` and `Creator`.

If a given field isn’t present in the PDF, the corresponding column stays blank.

---

## Behavior & Guarantees

- **One-time processing** — A PDF is considered processed if its SHA256 exists in `processed.tsv`, **or** the per‑PDF stamp file matches.  
- **In-flight lock** — While a PDF is being processed, an inflight lock prevents re‑entry.  
- **Space-safe** — Uses null‑delimited `find` and robust quoting.  
- **Quiet-file wait** — Small delay to ensure a PDF is fully written before processing.  
- **Dedup store** — Any object (by hash) is copied once into `hashed-objects/`. If the same hash reappears with a different extension, the first-seen copy is kept.

---

## Dependencies

Required:
- `mutool` (from `mupdf-tools`) — object extraction
- `sha256sum` (GNU coreutils) — hashing
- `bash`, `awk`, `sed`, `find`, `stat`, `date` — standard UNIX tools

Metadata / signatures:
- `exiftool` — Author/Creator
- `pdfsig` (from `poppler-utils`) — signature details

Optional (for font names in `objects.tsv`):
- `otfinfo` (from `lcdf-typetools`) **or** `fc-scan` (from `fontconfig`)

Optional (live watch, otherwise polling is used):
- `inotifywait` (from `inotify-tools`)

### Install on Debian/Ubuntu

```bash
sudo apt-get update
sudo apt-get install -y mupdf-tools poppler-utils exiftool inotify-tools fontconfig lcdf-typetools coreutils
```

### Install on macOS (Homebrew)

```bash
brew install mupdf-tools poppler exiftool coreutils fontconfig lcdf-typetools
# Optional: fswatch for watching if you prefer; the script will poll if inotifywait is absent
```

> macOS note: `date -d` is GNU `date`. If needed, install GNU coreutils and ensure `gdate` is available; the script uses `date -d`, so consider aliasing `date=gdate` or adjusting your PATH.

### Install on Arch

```bash
sudo pacman -S --needed mupdf poppler exiftool inotify-tools fontconfig lcdf-typetools coreutils
```

---

## Usage

```bash
# Help
./pdf_object_hasher.sh --help

# One-time processing of anything new in ./pdf/
./pdf_object_hasher.sh

# Continuous watch (reacts to each new PDF)
./pdf_object_hasher.sh --monitor
```

---

## Forcing Reprocessing (Advanced)

If you intentionally need to reprocess a PDF (e.g., after edits):

1. Remove its line from `processed.tsv` (match by SHA in column 1).
2. Remove the stamp: `rm pdf-objects/<safe>/.processed.sha`
3. (Optional) Remove its `pdf-objects/<safe>/` folder if you want a clean re‑extract.
4. (Optional) Leave `hashed-objects/` as-is to preserve dedupe, or remove specific hashes if appropriate.

> After changes, re-run the script (catch-up or monitor mode).

---

## Rebuilding `hash-count.tsv`

The script automatically rebuilds counts after each PDF, but you can regenerate manually:

```bash
# Column 4 is the SHA256 in the new schema
tail -n +2 objects.tsv | awk -F'\t' 'NF>=4{print $4}' | sort | uniq -c | sort -nr \
| awk '{print $2 "\t" $1}' > hash-count.tsv
```

---

## Troubleshooting

- **“Polling every 5s”**: Install `inotifywait` if you want immediate reactions.
- **No signature columns populated**: The PDF has no (or fewer) digital signatures, or `pdfsig` isn’t installed.
- **Author/Creator blank**: The PDF lacks those metadata fields, or `exiftool` isn’t installed.
- **Time parsing quirks**: On non-GNU `date`, times may not normalize; install GNU `date` (coreutils) and ensure it’s used.
- **Spaces/weird paths**: Fully supported. If you see issues, confirm you are running **Bash**.

---

## Design Notes

- **Idempotency**: `processed.tsv` + per-PDF stamp file + inflight lock ensure one-time processing per unique PDF content.
- **Atomic-ish writes**: temp files are moved into place to reduce risk of partial writes.
- **Extensible**: You can add more columns/sources; new columns will simply append to `objects.tsv` rows.

---

## License

Use freely for forensic analysis and research. No warranty.
