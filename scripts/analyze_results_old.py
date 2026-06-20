#!/usr/bin/env python3
"""
analyze_results.py — Mengumpulkan seluruh file hasil JSON dari kedua kondisi,
menghitung statistik deskriptif (median, IQR), menjalankan uji Mann-Whitney U,
dan menghitung korelasi antara involuntary context switches dengan crossover time
(menjawab Rumusan Masalah poin 1 dan 2).

Penggunaan:
  python3 analyze_results.py --results-dir /mnt/experiment-data/results
"""

import argparse
import json
from pathlib import Path

import pandas as pd
from scipy import stats


def load_results(results_dir: Path) -> pd.DataFrame:
    rows = []
    for json_path in sorted(results_dir.glob("*.json")):
        if json_path.name.endswith(".sysmetrics.json"):
            continue  # ditangani terpisah, lihat load_sysmetrics

        try:
            data = json.loads(json_path.read_text())
        except json.JSONDecodeError:
            print(f"PERINGATAN: gagal parse {json_path}, dilewati.")
            continue

        sysmetrics_path = results_dir / f"{data['run_id']}.sysmetrics.json"
        involuntary_delta = None
        throttled_usec_delta = None
        if sysmetrics_path.exists():
            sysmetrics = json.loads(sysmetrics_path.read_text())
            involuntary_delta = sysmetrics.get("involuntary_ctxt_switches_delta")
            throttled_usec_delta = sysmetrics.get("throttled_usec_delta")

        rows.append({
            "run_id": data.get("run_id"),
            "condition": data.get("condition"),
            "instance": data.get("instance"),
            "status_code": data.get("status_code"),
            "crossover_seconds": data.get("crossover_seconds"),
            "barrier_seconds": data.get("barrier_seconds"),
            "wall_clock_total_seconds": data.get("wall_clock_total_seconds"),
            "barrier_iter_count": data.get("barrier_iter_count"),
            "involuntary_ctxt_switches_delta": involuntary_delta,
            "throttled_usec_delta": throttled_usec_delta,
            "discrepancy_warning": data.get("phase_timing_discrepancy_warning"),
        })

    df = pd.DataFrame(rows)
    return df


def summarize(df: pd.DataFrame):
    print("=" * 70)
    print("RINGKASAN DESKRIPTIF PER KONDISI PER INSTANCE")
    print("=" * 70)

    summary = df.groupby(["instance", "condition"])["crossover_seconds"].agg(
        median="median",
        q1=lambda x: x.quantile(0.25),
        q3=lambda x: x.quantile(0.75),
        n="count",
    )
    summary["iqr"] = summary["q3"] - summary["q1"]
    print(summary.to_string())
    print()

    # Peringatan jika ada discrepancy yang belum diperiksa.
    flagged = df[df["discrepancy_warning"].notna()]
    if not flagged.empty:
        print(f"PERHATIAN: {len(flagged)} run memiliki discrepancy_warning, periksa manual:")
        print(flagged[["run_id", "discrepancy_warning"]].to_string(index=False))
        print()

    return summary


def run_mann_whitney(df: pd.DataFrame):
    print("=" * 70)
    print("UJI MANN-WHITNEY U: crossover_seconds, none vs static (per instance)")
    print("=" * 70)

    for instance in df["instance"].unique():
        subset = df[df["instance"] == instance]
        group_none = subset[subset["condition"] == "none"]["crossover_seconds"].dropna()
        group_static = subset[subset["condition"] == "static"]["crossover_seconds"].dropna()

        if len(group_none) < 3 or len(group_static) < 3:
            print(f"  {instance}: data tidak cukup untuk uji (n_none={len(group_none)}, n_static={len(group_static)})")
            continue

        u_stat, p_value = stats.mannwhitneyu(group_none, group_static, alternative="two-sided")
        median_diff = group_none.median() - group_static.median()
        pct_reduction = (median_diff / group_none.median() * 100) if group_none.median() else float("nan")

        print(
            f"  {instance}: U={u_stat:.1f}, p={p_value:.4f}, "
            f"median_none={group_none.median():.4f}s, median_static={group_static.median():.4f}s, "
            f"reduksi={pct_reduction:.1f}%"
        )
    print()


def run_correlation_analysis(df: pd.DataFrame):
    print("=" * 70)
    print("KORELASI: involuntary context switches vs crossover time")
    print("(Menjawab Rumusan Masalah poin 2)")
    print("=" * 70)

    clean = df.dropna(subset=["involuntary_ctxt_switches_delta", "crossover_seconds"])
    if len(clean) < 4:
        print("  Data tidak cukup untuk analisis korelasi (butuh minimal beberapa titik valid).")
        return

    rho, p_value = stats.spearmanr(clean["involuntary_ctxt_switches_delta"], clean["crossover_seconds"])
    print(f"  Spearman's rho = {rho:.4f}, p = {p_value:.4f}, n = {len(clean)}")
    print(
        "  Interpretasi: rho positif & signifikan -> lebih banyak migrasi thread "
        "berasosiasi dengan crossover time lebih lama, mendukung hipotesis penelitian."
    )
    print()


def check_barrier_stability(df: pd.DataFrame):
    print("=" * 70)
    print("STABILITAS FASE BARRIER ANTAR KONDISI")
    print("(Menjawab Rumusan Masalah poin 3 — apakah perbedaan performa benar")
    print(" berasal dari crossover, bukan dari barrier yang juga berubah)")
    print("=" * 70)

    for instance in df["instance"].unique():
        subset = df[df["instance"] == instance]
        group_none = subset[subset["condition"] == "none"]["barrier_seconds"].dropna()
        group_static = subset[subset["condition"] == "static"]["barrier_seconds"].dropna()

        if len(group_none) < 3 or len(group_static) < 3:
            continue

        u_stat, p_value = stats.mannwhitneyu(group_none, group_static, alternative="two-sided")
        print(
            f"  {instance}: median_barrier_none={group_none.median():.4f}s, "
            f"median_barrier_static={group_static.median():.4f}s, p={p_value:.4f} "
            f"({'TIDAK signifikan -> barrier stabil, baik' if p_value > 0.05 else 'SIGNIFIKAN -> periksa confounding!'})"
        )
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    df = load_results(results_dir)

    if df.empty:
        print("Tidak ada hasil ditemukan di", results_dir)
        return

    print(f"Total run termuat: {len(df)} (kondisi: {df['condition'].unique().tolist()})\n")

    # Saring run yang gagal solve (status_code != 2 == GRB.OPTIMAL) supaya
    # tidak mencemari statistik dengan run yang infeasible/timeout.
    failed = df[df["status_code"] != 2]
    if not failed.empty:
        print(f"PERINGATAN: {len(failed)} run TIDAK optimal (status_code != 2), dikeluarkan dari analisis:")
        print(failed[["run_id", "status_code"]].to_string(index=False))
        print()
    df = df[df["status_code"] == 2]

    summarize(df)
    run_mann_whitney(df)
    check_barrier_stability(df)
    run_correlation_analysis(df)

    csv_out = results_dir / "combined_results.csv"
    df.to_csv(csv_out, index=False)
    print(f"Data gabungan disimpan ke: {csv_out}")


if __name__ == "__main__":
    main()
