#!/usr/bin/env bash
# pdf_object_hasher.sh — explode PDFs, hash objects, index results, dedupe by hash
# EXTENSIONS ONLY: add Case/Filing/Date (MCRO_), exiftool Author/Creator, and pdfsig fields.
set -Eeuo pipefail

# ---------- Config ----------
ROOT_DIR="$(pwd)"
PDF_DIR="${ROOT_DIR}/pdf"
OBJ_DIR="${ROOT_DIR}/pdf-objects"
HASHED_DIR="${ROOT_DIR}/hashed-objects"

OBJECTS_TSV="${ROOT_DIR}/objects.tsv"
HASH_COUNT_TSV="${ROOT_DIR}/hash-count.tsv"
PROCESSED_TSV="${ROOT_DIR}/processed.tsv"

LOCK_DIR="${ROOT_DIR}/.locks"
INFLIGHT_DIR="${LOCK_DIR}/inflight"
mkdir -p "$LOCK_DIR" "$INFLIGHT_DIR"

OBJECTS_LOCK="${LOCK_DIR}/objects.lock"
COUNTS_LOCK="${LOCK_DIR}/counts.lock"
PROCESSED_LOCK="${LOCK_DIR}/processed.lock"

POLL_SECONDS=5

# New unified header (3 new leading cols + original 5 + 14 appended cols)
OBJECTS_HEADER=$'Case Number\tFiling Type\tFiling Date\tSHA256 Hash Value\tPdf File Name\tPdf Internal Object Path\tObject Type\tFont Name\tSig #1 Common Name\tSig #2 Common Name\tAuthor\tCreator\tSig #3 Common Name\tSig #4 Common Name\tSig #1 Signing Time\tSig #2 Signing Time\tSig #3 Signing Time\tSig #4 Signing Time\tSig #1 Byte Ranges\tSig #2 Byte Ranges\tSig #3 Byte Ranges\tSig #4 Byte Ranges'

# For migrating from the previous 5-col objects.tsv
OLD_HEADER_5=$'SHA256 Hash Value\tPdf File Name\tPdf Internal Object Path\tObject Type\tFont Name'

# ---------- Helpers (UNCHANGED BEHAVIOR) ----------
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 1; }; }

ensure_layout() {
  mkdir -p "$PDF_DIR" "$OBJ_DIR" "$HASHED_DIR"
  [[ -f "$OBJECTS_TSV" ]]   || : > "$OBJECTS_TSV"
  [[ -f "$HASH_COUNT_TSV" ]]|| : > "$HASH_COUNT_TSV"
  [[ -f "$PROCESSED_TSV" ]] || : > "$PROCESSED_TSV"

  # Header handling / migration
  if [[ ! -s "$OBJECTS_TSV" ]]; then
    printf '%s\n' "$OBJECTS_HEADER" > "$OBJECTS_TSV"
  else
    local first; first="$(head -n1 "$OBJECTS_TSV" || true)"
    if [[ "$first" == "$OLD_HEADER_5" ]]; then
      # Migrate old 5-col file to new header: prepend 3 blanks and append 14 blanks to every data row.
      local tmp="${OBJECTS_TSV}.tmp.$$"
      {
        printf '%s\n' "$OBJECTS_HEADER"
        awk -F'\t' '
          NR==1 { next }
          {
            # Prepend three blanks (Case/Filing/Date), then original row,
            # then append 14 blanks (sig + meta columns)
            p = "\t\t\t" $0
            for (i=1;i<=14;i++) p = p "\t"
            print p
          }
        ' "$OBJECTS_TSV"
      } > "$tmp"
      mv -f -- "$tmp" "$OBJECTS_TSV"
    elif [[ "$first" != "$OBJECTS_HEADER" ]]; then
      # If it’s some other custom header, leave as-is (we’ll still write with the new schema going forward).
      :
    fi
  fi
}

abspath() ( cd "$(dirname "$1")" >/dev/null 2>&1 && printf '%s/%s' "$(pwd -P)" "$(basename "$1")" )
safe_name() { local b="$1"; b="${b%.pdf}"; printf '%s' "$(basename "$b" | tr ' /' '__')"; }
pdf_sha256() { sha256sum -- "$1" | awk '{print $1}'; }

has_sha_processed() {
  local sha="$1"
  awk -F'\t' -v S="$sha" '($1==S){found=1; exit} END{exit(!found)}' "$PROCESSED_TSV" 2>/dev/null
}

mark_inflight() { printf '%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > "${INFLIGHT_DIR}/$1.lock"; }
clear_inflight(){ rm -f -- "${INFLIGHT_DIR}/$1.lock" 2>/dev/null || true; }

record_processed() {
  local sha="$1" pdf="$2" bytes="$3" mtime="$4"
  local iso; iso="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  { flock -x 9
    printf '%s\t%s\t%s\t%s\t%s\n' "$sha" "$pdf" "$bytes" "$mtime" "$iso" >> "$PROCESSED_TSV"
  } 9>"$PROCESSED_LOCK"
}

update_hash_counts() {
  # SHA moved to column 4
  { flock -x 9
    if [[ $(wc -l < "$OBJECTS_TSV") -gt 1 ]]; then
      tail -n +2 "$OBJECTS_TSV" \
      | awk -F'\t' 'NF>=4{print $4}' \
      | sort | uniq -c | sort -nr \
      | awk '{print $2 "\t" $1}' > "${HASH_COUNT_TSV}.tmp"
      mv -f -- "${HASH_COUNT_TSV}.tmp" "$HASH_COUNT_TSV"
    else
      : > "$HASH_COUNT_TSV"
    fi
  } 9>"$COUNTS_LOCK"
}

copy_object_if_new_by_hash() {
  local src="$1" sha="$2"
  local base ext=""
  base="$(basename "$src")"
  if [[ "$base" == *.* ]]; then ext=".${base##*.}"; [[ ${#ext} -le 11 ]] || ext=""; fi
  shopt -s nullglob
  local found=( "${HASHED_DIR}/${sha}" "${HASHED_DIR}/${sha}."* ); shopt -u nullglob
  for cand in "${found[@]}"; do [[ -e "$cand" ]] && return 0; done
  local dest="${HASHED_DIR}/${sha}${ext,,}"
  cp -f -- "$src" "${dest}.tmp"; mv -f -- "${dest}.tmp" "$dest"
}

font_name_from_file() {
  local f="$1"
  if command -v otfinfo >/dev/null 2>&1; then
    otfinfo -i "$f" 2>/dev/null | sed -n 's/^Full name:[[:space:]]*//p' | head -n1 && return 0
  fi
  if command -v fc-scan >/dev/null 2>&1; then
    fc-scan --format '%{family}\n' "$f" 2>/dev/null | head -n1 && return 0
  fi
  printf ''
}

wait_for_quiet_file() {
  local f="$1" last=-1 cur=0
  for _ in {1..10}; do
    cur=$(stat -c%s -- "$f" 2>/dev/null || echo -1)
    [[ "$cur" -ge 0 ]] || { sleep 0.3; continue; }
    [[ "$cur" -eq "$last" ]] && return 0
    last="$cur"; sleep 0.3
  done
  return 0
}

already_processed_by_stamp() {
  local safe="$1" sha="$2" stamp="${OBJ_DIR}/${safe}/.processed.sha"
  [[ -f "$stamp" ]] && read -r s < "$stamp" && [[ "$s" == "$sha" ]]
}

write_stamp() {
  local safe="$1" sha="$2" stamp="${OBJ_DIR}/${safe}/.processed.sha"
  printf '%s\n' "$sha" > "$stamp"
}

# -------- NEW: parse MCRO_* filename into Case/Filing/Date --------
parse_mcro_fields() {
  local pdf_base="$1"
  CASE_NUM=""; FILING_TYPE=""; FILING_DATE=""
  if [[ "$pdf_base" == MCRO_* ]]; then
    local rest="${pdf_base#MCRO_}"
    # split on underscores
    IFS='_' read -r CASE_NUM FILING_TYPE FILING_DATE _ <<<"$rest"
    # strip trailing .pdf if it landed in a field we keep (normally not needed for these three)
    CASE_NUM="${CASE_NUM%.pdf}"
    FILING_TYPE="${FILING_TYPE%.pdf}"
    FILING_DATE="${FILING_DATE%.pdf}"
  fi
}

# -------- NEW: pdfsig (CN / Signing Time / Byte Ranges) --------
# Populates SIG{1..4}_CN, SIG{1..4}_TIME, SIG{1..4}_RANGE
parse_pdfsig() {
  local pdf_abs="$1"
  # clear outputs
  SIG1_CN=""; SIG2_CN=""; SIG3_CN=""; SIG4_CN=""
  SIG1_TIME=""; SIG2_TIME=""; SIG3_TIME=""; SIG4_TIME=""
  SIG1_RANGE=""; SIG2_RANGE=""; SIG3_RANGE=""; SIG4_RANGE=""

  command -v pdfsig >/dev/null 2>&1 || return 0

  local cur=0 line var raw norm
  while IFS= read -r line; do
    # e.g., "Signature #1:" -> capture 1..4
    if [[ $line =~ ^Signature[[:space:]]#([1-4]): ]]; then
      cur="${BASH_REMATCH[1]}"
      continue
    fi

    # Only parse fields while inside a signature block
    if (( cur >= 1 && cur <= 4 )); then
      if [[ $line =~ Signer[[:space:]]Certificate[[:space:]]Common[[:space:]]Name:[[:space:]](.*)$ ]]; then
        var="SIG${cur}_CN"
        printf -v "$var" '%s' "${BASH_REMATCH[1]}"
      elif [[ $line =~ Signing[[:space:]]Time:[[:space:]](.*)$ ]]; then
        raw="${BASH_REMATCH[1]}"
        norm="$(date -d "$raw" +'%F %T' 2>/dev/null || true)"
        var="SIG${cur}_TIME"
        printf -v "$var" '%s' "${norm:-$raw}"
      elif [[ $line =~ Signed[[:space:]]Ranges:[[:space:]](.*)$ ]]; then
        var="SIG${cur}_RANGE"
        printf -v "$var" '%s' "${BASH_REMATCH[1]}"
      fi
    fi
  done < <(pdfsig "$pdf_abs" 2>/dev/null || true)
}


# -------- NEW: exiftool (Author / Creator) --------
get_pdf_author_creator() {
  local pdf_abs="$1"
  AUTHOR=""; CREATOR=""
  if command -v exiftool >/dev/null 2>&1; then
    AUTHOR="$(exiftool -s -s -s -Author  "$pdf_abs" 2>/dev/null | head -n1 || true)"
    CREATOR="$(exiftool -s -s -s -Creator "$pdf_abs" 2>/dev/null | head -n1 || true)"
  fi
}

explode_and_hash_pdf() {
  local pdf_path="$1"
  local pdf_abs pdf_base sha bytes mtime
  pdf_abs="$(abspath "$pdf_path")"
  pdf_base="$(basename "$pdf_path")"

  wait_for_quiet_file "$pdf_abs"
  sha="$(pdf_sha256 "$pdf_abs")"
  bytes="$(stat -c%s -- "$pdf_abs")"
  mtime="$(stat -c %Y -- "$pdf_abs")"

  [[ -f "${INFLIGHT_DIR}/${sha}.lock" ]] && { echo "↷  Skipping (in-flight): $pdf_base"; return 0; }
  if has_sha_processed "$sha"; then
    echo "↷  Skipping (already processed): $pdf_base"
    return 0
  fi

  local safe outdir
  safe="$(safe_name "$pdf_base")"
  outdir="${OBJ_DIR}/${safe}"
  if already_processed_by_stamp "$safe" "$sha"; then
    if ! has_sha_processed "$sha"; then record_processed "$sha" "$pdf_base" "$bytes" "$mtime"; fi
    echo "↷  Skipping (stamp says processed): $pdf_base"
    return 0
  fi

  mark_inflight "$sha"

  mkdir -p -- "$outdir"
  echo "→  Extracting objects from: $pdf_base"
  ( cd "$outdir" && mutool extract -- "$pdf_abs" 2>/dev/null )

  # -------- NEW per-PDF metadata (once) --------
  parse_mcro_fields "$pdf_base"
  parse_pdfsig "$pdf_abs"
  get_pdf_author_creator "$pdf_abs"

  # Hash extracted files; append rows (22 columns)
  local added=0
  while IFS= read -r -d '' obj; do
    local h rel ext lname=""
    h="$(sha256sum -- "$obj" | awk '{print $1}')"
    rel="${obj#${OBJ_DIR}/}"
    if [[ "$obj" == *.* ]]; then ext=".${obj##*.}"; ext="${ext,,}"; else ext=""; fi
    case "$ext" in
      .ttf|.otf|.ttc|.woff|.woff2|.pfb|.pfa) lname="$(font_name_from_file "$obj")" ;;
      *) lname="" ;;
    esac

    # Row:
    # [Case Number] [Filing Type] [Filing Date]
    # [SHA256] [Pdf File Name] [Pdf Internal Object Path] [Object Type] [Font Name]
    # [Sig1 CN] [Sig2 CN] [Author] [Creator] [Sig3 CN] [Sig4 CN]
    # [Sig1 Time] [Sig2 Time] [Sig3 Time] [Sig4 Time]
    # [Sig1 Ranges] [Sig2 Ranges] [Sig3 Ranges] [Sig4 Ranges]
    { flock -x 9
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "${CASE_NUM}" "${FILING_TYPE}" "${FILING_DATE}" \
        "$h" "$pdf_base" "$rel" "$ext" "$lname" \
        "${SIG1_CN}" "${SIG2_CN}" "${AUTHOR}" "${CREATOR}" "${SIG3_CN}" "${SIG4_CN}" \
        "${SIG1_TIME}" "${SIG2_TIME}" "${SIG3_TIME}" "${SIG4_TIME}" \
        "${SIG1_RANGE}" "${SIG2_RANGE}" "${SIG3_RANGE}" "${SIG4_RANGE}" \
        >> "$OBJECTS_TSV"
    } 9>"$OBJECTS_LOCK"

    copy_object_if_new_by_hash "$obj" "$h"
    added=$((added+1))
  done < <(find "$outdir" -type f -print0)

  write_stamp "$safe" "$sha"
  record_processed "$sha" "$pdf_base" "$bytes" "$mtime"
  clear_inflight "$sha"
  update_hash_counts

  # Per-PDF count now reads column 5 (PDF name)
  local per_pdf
  per_pdf="$(tail -n +2 "$OBJECTS_TSV" | awk -F'\t' -v P="$pdf_base" '($5==P){c++} END{print (c?c:0)}')"
  echo "✔  Processed $pdf_base → $per_pdf object-rows now recorded for this PDF"
}

scan_once() {
  echo "Scanning for unprocessed PDFs in: $PDF_DIR"
  shopt -s nullglob
  local pdfs=( "$PDF_DIR"/*.pdf "$PDF_DIR"/*.PDF )
  shopt -u nullglob
  ((${#pdfs[@]})) || { echo "… no PDFs found."; return 0; }
  for pdf in "${pdfs[@]}"; do explode_and_hash_pdf "$pdf"; done
  echo "↺  hash-count.tsv updated."
}

monitor_loop() {
  echo "🔎 Monitoring '$PDF_DIR' for new PDFs… (Ctrl+C to stop)"
  if command -v inotifywait >/dev/null 2>&1; then
    while true; do
      inotifywait -e close_write -e moved_to -e create --format '%f' "$PDF_DIR" \
      | while IFS= read -r f; do
          [[ "$f" =~ \.pdf$|\.PDF$ ]] || continue
          explode_and_hash_pdf "${PDF_DIR}/${f}"
        done
    done
  else
    echo "NOTE: 'inotifywait' not found. Polling every ${POLL_SECONDS}s."
    while true; do
      scan_once
      sleep "$POLL_SECONDS"
    done
  fi
}

usage() {
  cat <<EOF
Usage:
  $0                 # one-time catch-up
  $0 -m|--monitor    # catch-up, then watch ./pdf/ for new PDFs

New columns:
  [Case Number] [Filing Type] [Filing Date] … then the original 5 … then:
  [Sig #1 Common Name] [Sig #2 Common Name] [Author] [Creator] [Sig #3 Common Name] [Sig #4 Common Name]
  [Sig #1 Signing Time] [Sig #2 Signing Time] [Sig #3 Signing Time] [Sig #4 Signing Time]
  [Sig #1 Byte Ranges]  [Sig #2 Byte Ranges]  [Sig #3 Byte Ranges]  [Sig #4 Byte Ranges]

Dependencies:
  mutool (mupdf-tools), sha256sum
  exiftool (Author/Creator)       → sudo apt-get install exiftool
  pdfsig (from poppler-utils)     → sudo apt-get install poppler-utils
  Optional for Font Name: lcdf-typetools (otfinfo) or fontconfig (fc-scan)
EOF
}

main() {
  [[ "${1:-}" =~ ^(-h|--help)$ ]] && { usage; exit 0; }
  need mutool; need sha256sum
  ensure_layout
  scan_once
  case "${1:-}" in
    -m|--monitor) monitor_loop ;;
    "") ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"

