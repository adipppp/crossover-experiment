#!/usr/bin/env bash
# download_benchmarks.sh — Mengunduh subset instance dari koleksi benchmark
# Mittelmann untuk LP. Jalankan di VM, di luar Pod.
#
# Pipeline dekompresi:
#   .bz2 (bzip2) → bzip2 -dc → netlib compressed MPS → emps → plain .mps
#
# Pastikan emps sudah dikompilasi sebelum menjalankan skrip ini:
#   gcc -O2 -m64 -o /path/ke/emps /path/to/emps.c
#
# Path emps bisa di mana saja. Override default dengan env var:
#   EMPS_BIN=/path/ke/emps bash scripts/download_benchmarks.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="/mnt/experiment-data/instances"
EMPS_BIN="${EMPS_BIN:-$SCRIPT_DIR/../emps}"

# Validasi emps tersedia
if [[ ! -x "$EMPS_BIN" ]]; then
  echo "ERROR: emps binary tidak ditemukan atau tidak executable di '$EMPS_BIN'." >&2
  echo "       Kompilasi: gcc -O2 -m64 -o /path/ke/emps /path/to/emps.c" >&2
  echo "       Lalu jalankan: EMPS_BIN=/path/ke/emps bash $0" >&2
  exit 1
fi

# Validasi bzip2 tersedia
if ! command -v bzip2 &>/dev/null; then
  echo "ERROR: bzip2 tidak ditemukan." >&2
  echo "       Install terlebih dahulu, misalnya: sudo apt install bzip2" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
cd "$DEST_DIR"

echo ">>> Menggunakan emps binary: $EMPS_BIN"
echo ">>> Mengunduh instance Mittelmann LP benchmark..."

# URL aktual dari plato.asu.edu (Hans Mittelmann LP test set)
# File yang diunduh adalah netlib compressed MPS yang dibungkus bzip2.
declare -A INSTANCE_URLS=(
  ["neos3.bz2"]="https://plato.asu.edu/ftp/lptestset/misc/neos3.bz2"
  ["L1_sixm1000obs.bz2"]="https://plato.asu.edu/ftp/lptestset/L1_sixm1000obs.bz2"
  ["Linf_520c.bz2"]="https://plato.asu.edu/ftp/lptestset/Linf_520c.bz2"
  ["cont1.bz2"]="https://plato.asu.edu/ftp/lptestset/misc/cont1.bz2"
  ["cont11.bz2"]="https://plato.asu.edu/ftp/lptestset/misc/cont11.bz2"
)

for archive in "${!INSTANCE_URLS[@]}"; do
  mps_file="${archive%.bz2}.mps"   # e.g. neos3.mps
  url="${INSTANCE_URLS[$archive]}"

  if [[ -f "$mps_file" ]]; then
    echo ">>> $mps_file sudah ada, skip."
    continue
  fi

  echo ">>> Mengunduh $archive dari $url ..."
  curl -fSL -o "$archive" "$url" || {
    echo "GAGAL mengunduh $archive" >&2
    continue
  }

  # Tahap 1: bzip2 -dc  → decompress bzip2, hasilkan netlib compressed MPS ke stdout
  # Tahap 2: emps -      → baca dari stdin, ekspansi ke plain MPS, tulis ke stdout
  # Hasilnya disimpan langsung ke file .mps final.
  echo ">>> Mengekspansi $archive → $mps_file (bzip2 | emps)..."
  if bzip2 -dc "$archive" | "$EMPS_BIN" - > "$mps_file"; then
    echo ">>> OK: $mps_file berhasil dibuat."
    rm -f "$archive"   # Hapus arsip .bz2 setelah berhasil
  else
    echo "GAGAL mengekspansi $archive ke $mps_file" >&2
    rm -f "$mps_file"  # Hapus output parsial jika gagal
  fi
done

echo ">>> Verifikasi integritas file (.mps):"
for archive in "${!INSTANCE_URLS[@]}"; do
  mps_file="${archive%.bz2}.mps"
  if [[ -f "$mps_file" ]]; then
    size=$(stat -c%s "$mps_file")
    # Cek minimal: file harus diawali dengan "NAME" (header MPS standar)
    first_word=$(head -c 4 "$mps_file" 2>/dev/null || echo "")
    if [[ "$first_word" == "NAME" ]]; then
      echo "  $mps_file: ${size} bytes, status: OK (header MPS valid)"
    else
      echo "  $mps_file: ${size} bytes, status: PERINGATAN — header tidak dimulai dengan NAME"
    fi
  else
    echo "  $mps_file: GAGAL DIEKSTRAK ATAU TIDAK DITEMUKAN"
  fi
done

echo ">>> Selesai. Pastikan semua file berstatus OK sebelum menjalankan eksperimen."
