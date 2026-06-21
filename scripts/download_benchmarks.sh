#!/usr/bin/env bash
# download_benchmarks.sh — Mengunduh subset instance dari koleksi benchmark
# Mittelmann untuk LP. Jalankan di VM, di luar Pod.

set -euo pipefail

DEST_DIR="/mnt/experiment-data/instances"
mkdir -p "$DEST_DIR"
cd "$DEST_DIR"

echo ">>> Mengunduh instance Mittelmann LP benchmark..."

# URL aktual dari plato.asu.edu (Hans Mittelmann LP test set)
declare -A INSTANCE_URLS=(
  ["neos3.mps.bz2"]="https://plato.asu.edu/ftp/lptestset/misc/neos3.bz2"
  ["L1_sixm1000obs.mps.bz2"]="https://plato.asu.edu/ftp/lptestset/L1_sixm1000obs.bz2"
  ["Linf_520c.mps.bz2"]="https://plato.asu.edu/ftp/lptestset/Linf_520c.bz2"
  ["cont1.mps.bz2"]="https://plato.asu.edu/ftp/lptestset/misc/cont1.bz2"
  ["cont11.mps.bz2"]="https://plato.asu.edu/ftp/lptestset/misc/cont11.bz2"
)

for archive in "${!INSTANCE_URLS[@]}"; do
  filename="${archive%.bz2}" # Ekstrak nama file .mps
  url="${INSTANCE_URLS[$archive]}"
  
  if [[ -f "$filename" ]]; then
    echo ">>> $filename sudah ada, skip."
    continue
  fi
  
  echo ">>> Mengunduh $archive dari $url ..."
  curl -fSL -o "$archive" "$url" || {
    echo "GAGAL mengunduh $archive" >&2
    continue
  }

  echo ">>> Mengekstrak $archive..."
  bzip2 -d "$archive"
done

echo ">>> Verifikasi integritas file (.mps):"
for archive in "${!INSTANCE_URLS[@]}"; do
  filename="${archive%.bz2}"
  if [[ -f "$filename" ]]; then
    size=$(stat -c%s "$filename")
    echo "  $filename: ${size} bytes, status: OK"
  else
    echo "  $filename: GAGAL DIEKSTRAK ATAU TIDAK DITEMUKAN"
  fi
done

echo ">>> Selesai. Pastikan semua file berstatus OK sebelum menjalankan eksperimen."
