#!/usr/bin/env python3
"""
quick_check_timeseries.py — Cek cepat deret waktu crossover_seconds per
instance/kondisi/blok, TANPA menjalankan pipeline statistik penuh
(analyze_results.py). Dipakai untuk:

  1. Sanity check parsial saat baru sebagian blok tersedia (mis. intip Blok 1
     sebelum Blok 2 mulai).
  2. Cek manual-targeted satu kombinasi tertentu (mis. static/cont11/blk2 saja)
     untuk deteksi dini lompatan seperti temuan Subbab 4.3.5 pada run pertama,
     tanpa menunggu seluruh 300 run selesai.

Deteksi lompatan memakai metode yang sama dengan investigasi post-hoc run
pertama: bandingkan rerata paruh-pertama vs paruh-kedua tiap kombinasi
instance/kondisi/blok, tandai kalau selisihnya melebihi --jump-threshold
(default 3%, sesuai ambang yang dipakai pada temuan cont11 di draf pertama).
Ini BUKAN pengganti sensitivity_check_temporal_anomaly.py yang sesungguhnya —
tidak ada uji Mann-Whitney/rank-biserial di sini, murni deskriptif untuk
early-warning.

Penggunaan:
  # Lihat semua kombinasi
  python3 quick_check_timeseries.py --results-dir /path/ke/hasil

  # Fokus ke satu kombinasi (mis. mirip skenario cont11 run pertama)
  python3 quick_check_timeseries.py --results-dir /path/ke/hasil \\
      --instance cont11 --condition static --block 2

  # Ubah ambang deteksi lompatan
  python3 quick_check_timeseries.py --results-dir /path/ke/hasil --jump-threshold 0.05
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# Reuse load_results dari analyze_results.py supaya parsing run_id/block/
# status_code TIDAK menyimpang dari skrip analisis utama. Asumsi: file ini
# duduk di direktori scripts/ yang sama dengan analyze_results.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from analyze_results import load_results
except ImportError:
    print(
        "FATAL: tidak menemukan analyze_results.py di direktori yang sama.\n"
        "Taruh quick_check_timeseries.py bersebelahan dengan scripts/analyze_results.py.",
        file=sys.stderr,
    )
    sys.exit(1)


def extract_rep_index(run_id: str) -> int:
    """Ambil nomor repetisi dari run_id (…-run05 -> 5). Smoke test (…-run1
    tanpa leading zero, sudah di-skip oleh load_results) tidak akan sampai
    sini."""
    m = re.search(r"-run(\d+)$", run_id)
    return int(m.group(1)) if m else -1


def check_combo(df: pd.DataFrame, instance: str, condition: str, block: int,
                 jump_threshold: float) -> None:
    sub = df[
        (df["instance"] == instance)
        & (df["condition"] == condition)
        & (df["block"] == block)
    ].copy()

    label_cond = "A (none)" if condition == "none" else "B (static)"
    header = f"{instance} | Kondisi {label_cond} | Blok {block}"
    print("-" * 70)
    print(header)
    print("-" * 70)

    if sub.empty:
        print("  (belum ada data)")
        print()
        return

    sub["rep"] = sub["run_id"].apply(extract_rep_index)
    sub = sub.sort_values("rep")

    for _, row in sub.iterrows():
        print(f"  run{row['rep']:02d}  crossover={row['crossover_seconds']:.4f}s"
              f"  barrier={row['barrier_iteration_seconds']:.4f}s")

    n = len(sub)
    print(f"  n={n} (dari 15 direncanakan)")

    if n < 4:
        print("  (n terlalu kecil untuk cek paruh-pertama vs paruh-kedua)")
        print()
        return

    mid = n // 2
    first_half = sub["crossover_seconds"].iloc[:mid]
    second_half = sub["crossover_seconds"].iloc[mid:]
    fh_mean, sh_mean = first_half.mean(), second_half.mean()
    pct_change = (sh_mean - fh_mean) / fh_mean if fh_mean else float("nan")

    flag = abs(pct_change) > jump_threshold
    marker = "  ⚠ LOMPATAN TERDETEKSI" if flag else "  (tidak ada lompatan signifikan)"
    print(f"  paruh-1 mean={fh_mean:.4f}s | paruh-2 mean={sh_mean:.4f}s | "
          f"perubahan={pct_change:+.1%}{marker}")

    if flag:
        print("  -> Pertimbangkan investigasi lanjutan seperti Subbab 4.3.5 "
              "run pertama (bukan cuma tunggu blok berikutnya selesai).")
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--instance", default=None,
                         help="Filter ke satu instance saja (mis. cont11). "
                              "Kosongkan untuk semua instance.")
    parser.add_argument("--condition", default=None, choices=["none", "static"],
                         help="Filter ke satu kondisi saja. Kosongkan untuk keduanya.")
    parser.add_argument("--block", type=int, default=None, choices=[1, 2],
                         help="Filter ke satu blok saja. Kosongkan untuk keduanya.")
    parser.add_argument("--jump-threshold", type=float, default=0.03,
                         help="Ambang proporsi perubahan paruh-1 vs paruh-2 untuk "
                              "ditandai sebagai lompatan (default 0.03 = 3%%, "
                              "sesuai ambang deteksi cont11 di draf pertama).")
    args = parser.parse_args()

    df = load_results(Path(args.results_dir))
    if df.empty:
        print("Tidak ada hasil ditemukan di", args.results_dir)
        return

    # Hanya run yang solve-nya sukses (status_code == 2 / GRB.OPTIMAL) —
    # konsisten dengan filter di analyze_results.py.
    df = df[df["status_code"] == 2]

    instances = [args.instance] if args.instance else sorted(df["instance"].unique())
    conditions = [args.condition] if args.condition else ["none", "static"]
    blocks = [args.block] if args.block else [1, 2]

    print("=" * 70)
    print("CEK CEPAT DERET WAKTU crossover_seconds")
    print("(deskriptif saja — bukan pengganti uji statistik di analyze_results.py)")
    print("=" * 70)
    print()

    for instance in instances:
        for condition in conditions:
            for block in blocks:
                check_combo(df, instance, condition, block, args.jump_threshold)

    print("Selesai. Kombinasi dengan tanda ⚠ di atas layak dicek lebih lanjut")
    print("sebelum menunggu seluruh eksperimen selesai.")


if __name__ == "__main__":
    main()
