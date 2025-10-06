
# PDF Object Hasher — Windows Packaging Guide (Updated)

This guide shows the **easiest ways** to ship a ready-to-use Windows build of the
**PDF Object Hasher** tool (the Python/PyInstaller version). It covers dependencies,
packaging approaches, download sources, and copy‑paste commands.

---

## What the app does (recap)

- Watches a `pdf\` folder for new PDFs (or runs a one-time catch-up).
- Explodes each PDF into objects (`mutool extract`) under `pdf-objects\<safe>\`.
- Hashes each object (SHA-256) and appends one row per object to **`objects.tsv`**
  using a **22‑column** schema (MCRO fields, hash, object info, `pdfsig` and `exiftool` fields).
- Copies each unique blob to **`hashed-objects\<sha256>.<ext>`** (deduped by content).
- Ensures **one‑time processing** via `processed.tsv` + per‑PDF stamp + in‑flight lock.
- Rebuilds **`hash-count.tsv`** from column 4 (SHA) after each processed PDF.

---

## Where to download the required tools

You have two ways to get the CLI tools the app uses at runtime:
- **Package manager**: `winget` (quick).  
- **Portable zips**: download prebuilt binaries and copy the EXEs (and any DLLs) next to your app.

> If you ship a **one‑folder** build, put these EXEs **in the same folder as** `pdf_object_hasher.exe`.
> If you ship a **single‑file** build, see the “Single‑File EXE (advanced)” section for bundling them.

### mutool (MuPDF)
- **winget (easy):**
  ```powershell
  winget install --id=ArtifexSoftware.mutool -e
  ```
- **Official site / releases:** https://mupdf.com/releases/
- **Docs (mutool is part of MuPDF tools):** https://mupdf.readthedocs.io/en/latest/mupdf-command-line.html

### pdfsig (Poppler)
- **winget (easy):**
  ```powershell
  winget install --id=oschwartz10612.Poppler -e
  ```
  *(Installs “Poppler for Windows” prebuilt binaries with `pdfsig.exe` + required DLLs.)*
- **Portable zip (prebuilt binaries):** https://github.com/oschwartz10612/poppler-windows/releases
- **Project page (source):** https://poppler.freedesktop.org/

### exiftool
- **Official download & install instructions (Windows executable zip):** https://exiftool.org/install.html  
  *(You’ll get `exiftool(-k).exe`; rename to `exiftool.exe` for CLI use.)*

### Optional font helpers (for the **Font Name** column)
For Windows, the most reliable way to get **`otfinfo.exe`** (LCDF Typetools) and **`fc-scan.exe`** (Fontconfig) is **MSYS2**:

1) Install MSYS2: https://www.msys2.org/docs/installer/  
2) Open the **MSYS2 UCRT64** shell and run:
   ```bash
   pacman -Syu
   pacman -S mingw-w64-ucrt-x86_64-fontconfig mingw-w64-ucrt-x86_64-texlive-bin
   ```
   - `fontconfig` provides `fc-scan.exe` under `C:\msys64\ucrt64\bin\`
   - `texlive-bin` provides `otfinfo.exe` (LCDF Typetools) under the same `bin\`

> After installing, copy `fc-scan.exe`, `otfinfo.exe` and any `.dll` files from `C:\msys64\ucrt64\bin\` into the same folder as your app exe (for a portable one‑folder distribution).

---

## Recommended packaging: **Portable One‑Folder** build (simplest)

**Why:** No code changes. Users unzip a single folder and double‑click the EXE. All
required tools (mutool, pdfsig, exiftool) sit next to the EXE so they’re discovered
without touching PATH.

### 1) Build the app

```powershell
# from the folder containing pdf_object_hasher.py
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py -m pip install pyinstaller

# one-folder build → dist\pdf_object_hasher\pdf_object_hasher.exe
py -m PyInstaller --onedir pdf_object_hasher.py
```

After this, you’ll have:
```
dist\
  pdf_object_hasher\
    pdf_object_hasher.exe
    (plus PyInstaller DLLs & support files)
```

### 2) Add Windows binaries **next to the EXE**

Place these files into **`dist\pdf_object_hasher\`** (same folder as `pdf_object_hasher.exe`):

- `mutool.exe`  (from MuPDF)
- `pdfsig.exe`  (from Poppler) **+ its DLLs**
- `exiftool.exe` (from ExifTool)

**Optional** (only for `Font Name` column):
- `otfinfo.exe` (LCDF Typetools) and/or `fc-scan.exe` (Fontconfig) **+ their DLLs**

### 3) First run (the app creates missing folders on first launch)

```powershell
cd dist\pdf_object_hasher

# one-time pass over anything already in .\pdf\
.\pdf_object_hasher.exe

# or continuous monitoring (drop-folder workflow)
.\pdf_object_hasher.exe --monitor
```

**Working layout** (auto-created if missing):
```
pdf_object_hasher\
  pdf\               # drop PDFs here
  pdf-objects\       # per-PDF extracted objects
  hashed-objects\    # unique blobs by SHA256 (dedup)
  objects.tsv        # main table (22 columns, tab-separated)
  hash-count.tsv     # <hash>\t<count>
  processed.tsv      # ledger of processed PDFs (by SHA256)
  .locks\inflight\   # runtime locks
  pdf_object_hasher.exe
  mutool.exe
  pdfsig.exe (+ DLLs)
  exiftool.exe
  (optional: otfinfo.exe / fc-scan.exe + DLLs)
```

### 4) (Optional) Double‑click launchers

Create two batch files in the same folder:

**Start (Monitor).bat**
```bat
@echo off
cd /d "%~dp0"
.\pdf_object_hasher.exe --monitor
```

**Start (Once).bat**
```bat
@echo off
cd /d "%~dp0"
.\pdf_object_hasher.exe
```

Zip the entire `pdf_object_hasher\` folder and share. Users just unzip and run.

---

## Alternative: **Single‑File EXE** that also bundles tools (advanced)

You can embed `mutool.exe`, `pdfsig.exe`, `exiftool.exe` and their DLLs **into** the
PyInstaller single‑file, then add a **runtime hook** that prepends PyInstaller’s
temp extraction dir to PATH so your script can call those tools by name.

### 1) Put third‑party EXEs/DLLs into a local `vendor\` folder

Example layout:
```
project\
  pdf_object_hasher.py
  vendor\
    mutool.exe
    pdfsig.exe
    exiftool.exe
    (DLLs required by pdfsig.exe / others)
    (optional font tools + DLLs)
```

### 2) Create a runtime hook to set PATH

Save as `hook_add_vendor_to_path.py`:
```python
# Ensures the PyInstaller MEIPASS (temp extract dir) is on PATH at runtime
import os, sys
if hasattr(sys, "_MEIPASS"):
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")
```

### 3) Build with `--onefile`, adding binaries and the hook

```powershell
py -m PyInstaller --onefile ^
  --add-binary "vendor\mutool.exe;." ^
  --add-binary "vendor\pdfsig.exe;." ^
  --add-binary "vendor\exiftool.exe;." ^
  --additional-hooks-dir . ^
  --runtime-hook hook_add_vendor_to_path.py ^
  pdf_object_hasher.py
```

> Add more `--add-binary` entries for any DLLs or optional tools (`otfinfo.exe`, `fc-scan.exe`).  
> Using `;.` puts them at the root of the extraction directory so they’re discoverable via PATH.

---

## Verifying the toolchain

From the app folder (or after one-file extraction at runtime), these should work:

```powershell
.\mutool.exe --version
.\pdfsig.exe -h
.\exiftool.exe -ver
.\otfinfo.exe -h      # optional
.\fc-scan.exe -h      # optional
```

If a command fails, you’re likely missing a DLL—copy the extra `.dll` files from the same source bundle into your app folder. Poppler in particular needs its `bin\` DLLs.

---

## Notes & Tips

- **DLL satisfaction (Poppler)**: `pdfsig.exe` often requires multiple DLLs. Copy the
  entire `bin\` from your Poppler bundle into the app folder to avoid missing DLL errors.
- **No PATH edits needed** in the one‑folder approach: Windows searches the current
  directory first when you launch the EXE **from that folder**.
- **Extraction directory**: the app extracts objects inside `pdf-objects\<safe>\` (Windows/Unix).
- **Reprocessing control**: to force reprocess, delete the PDF’s line in `processed.tsv`
  **and** remove `pdf-objects\<safe>\.processed.sha`.
- **Font names** are optional; without the font tools the column will be blank.
- **Monitoring performance**: The app prefers `watchdog` (file events). If not installed,
  it polls (default 5s). For most users, that’s fine on Windows.

---

## Quick “DRY RUN” checklist

1. `py -m PyInstaller --onedir pdf_object_hasher.py`
2. Copy `mutool.exe`, `pdfsig.exe` (+ DLLs), `exiftool.exe` next to `pdf_object_hasher.exe`.
3. Create `pdf\` folder and drop a test PDF.
4. Run `.\pdf_object_hasher.exe` → check `objects.tsv`, `hash-count.tsv`, `processed.tsv`.
5. Run `.\pdf_object_hasher.exe --monitor` → drop another PDF → watch it process live.

Done. Zip and ship the folder.

---

## Licensing

- **MuPDF/Mutool**, **Poppler/pdfsig**, **ExifTool**, and optional font tools have their
  own licenses. When redistributing binaries, include their license files as required
  by their respective licenses.
