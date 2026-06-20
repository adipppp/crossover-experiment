#!/usr/bin/env bash
# download_benchmarks.sh — Mengunduh subset instance dari koleksi benchmark
# Mittelmann untuk LP. Jalankan di VM, di luar Pod.
#
# CATATAN: URL instance individual di plato.asu.edu berubah dari waktu ke
# waktu tergantung benchmark suite aktif (mis. "Barrier LP", "Crossover" suite).
# VERIFIKASI MANUAL daftar dan URL terbaru di https://plato.asu.edu/bench.html
# sebelum menjalankan skrip ini — daftar di bawah adalah CONTOH STRUKTUR,
# bukan URL final yang sudah divalidasi.

set -euo pipefail

DEST_DIR="/mnt/experiment-data/instances"
mkdir -p "$DEST_DIR"
cd "$DEST_DIR"

echo ">>> PERINGATAN: skrip ini berisi URL CONTOH yang HARUS diverifikasi"
echo ">>> manual terhadap https://plato.asu.edu/bench.html sebelum dipakai."
echo ">>> Lanjutkan? (Ctrl+C untuk batal, Enter untuk lanjut)"
read -r

# Contoh struktur unduhan — GANTI dengan URL instance aktual yang Anda
# pilih dari halaman benchmark Mittelmann (mis. set LP benchmark / Crossover).
# Contoh struktur unduhan — GANTI dengan URL instance aktual yang Anda
# pilih dari halaman benchmark Mittelmann (mis. set LP benchmark / Crossover).
# Minimal LIMA instance sesuai Subbab "Objek Uji" di Metode Penelitian —
# nama file di sini HARUS sama persis dengan array INSTANCES di run_experiment.sh.
declare -A INSTANCE_URLS=(
  ["neos3.mps.gz"]="https://example-placeholder.invalid/neos3.mps.gz"
  ["L1_sixm1000obs.mps.gz"]="https://example-placeholder.invalid/L1_sixm1000obs.mps.gz"
  ["Linf_520c.mps.gz"]="https://example-placeholder.invalid/Linf_520c.mps.gz"
  ["GANTI_instance_keempat.mps.gz"]="https://example-placeholder.invalid/GANTI_instance_keempat.mps.gz"
  ["GANTI_instance_kelima.mps.gz"]="https://example-placeholder.invalid/GANTI_instance_kelima.mps.gz"
)

for filename in "${!INSTANCE_URLS[@]}"; do
  url="${INSTANCE_URLS[$filename]}"
  if [[ -f "$filename" ]]; then
    echo ">>> $filename sudah ada, skip."
    continue
  fi
  echo ">>> Mengunduh $filename dari $url ..."
  curl -fSL -o "$filename" "$url" || {
    echo "GAGAL mengunduh $filename — periksa URL manual di plato.asu.edu" >&2
  }
done

echo ">>> Verifikasi integritas file (ukuran > 0, bisa dibuka gzip):"
for filename in "${!INSTANCE_URLS[@]}"; do
  if [[ -f "$filename" ]]; then
    size=$(stat -c%s "$filename")
    gzip -t "$filename" 2>/dev/null && status="OK" || status="CORRUPT/BUKAN_GZIP"
    echo "  $filename: ${size} bytes, status: $status"
  fi
done

echo ">>> Selesai. Pastikan setiap file berstatus OK sebelum menjalankan eksperimen."
