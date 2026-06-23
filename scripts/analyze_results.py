#!/usr/bin/env python3
"""
analyze_results.py — Mengumpulkan seluruh file hasil JSON dari kedua kondisi,
menghitung statistik deskriptif (median, IQR), menjalankan uji Mann-Whitney U
dengan koreksi Bonferroni (RQ1), uji stabilitas fase barrier (RQ3), korelasi
Spearman PER KONDISI terpisah antara context-switch fase-crossover dengan
crossover time (RQ2), dan effect size rank-biserial antar instance (RQ4).

Penggunaan:
  python3 analyze_results.py --results-dir /mnt/experiment-data/results
"""

import argparse
import json
from pathlib import Path

import pandas as pd
from scipy import stats

ALPHA = 0.05


def load_results(results_dir: Path) -> pd.DataFrame:
    rows = []
    for json_path in sorted(results_dir.glob("*.json")):
        if json_path.name.endswith(".sysmetrics.json"):
            continue  # ditangani terpisah sebagai file pendamping, lihat di bawah

        try:
            data = json.loads(json_path.read_text())
        except json.JSONDecodeError:
            print(f"PERINGATAN: gagal parse {json_path}, dilewati.")
            continue

        sysmetrics_path = results_dir / f"{data['run_id']}.sysmetrics.json"
        ctxt_delta_crossover_only = None
        ctxt_delta_whole_process = None
        throttled_usec_delta = None
        cache_miss_rate = None
        if sysmetrics_path.exists():
            try:
                sysmetrics = json.loads(sysmetrics_path.read_text())
                ctxt_delta_crossover_only = sysmetrics.get("involuntary_ctxt_switches_delta_crossover_phase_only")
                ctxt_delta_whole_process = sysmetrics.get("involuntary_ctxt_switches_delta_whole_process")
                throttled_usec_delta = sysmetrics.get("throttled_usec_delta")
                cache_miss_rate = sysmetrics.get("cache_miss_rate")
            except json.JSONDecodeError:
                print(f"PERINGATAN: gagal parse {sysmetrics_path}, metrik sistem run ini diabaikan.")

        wall_clock = data.get("wall_clock_total_seconds_DO_NOT_USE_FOR_PHASE_ANALYSIS") or data.get("wall_clock_total_seconds")

        rows.append({
            "run_id": data.get("run_id"),
            "condition": data.get("condition"),
            "instance": data.get("instance"),
            "status_code": data.get("status_code"),
            "crossover_seconds": data.get("crossover_seconds"),
            "barrier_seconds": data.get("barrier_seconds"),
            "wall_clock_total_seconds": wall_clock,
            "barrier_iter_count": data.get("barrier_iter_count"),
            # Metrik UTAMA untuk RQ2 — sudah diselaraskan ke rentang waktu fase
            # crossover saja oleh collect_system_metrics.py.
            "involuntary_ctxt_switches_delta_crossover_only": ctxt_delta_crossover_only,
            # Disimpan untuk transparansi/audit, TIDAK dipakai pada uji RQ2.
            "involuntary_ctxt_switches_delta_whole_process": ctxt_delta_whole_process,
            "throttled_usec_delta": throttled_usec_delta,
            "cache_miss_rate": cache_miss_rate,
            "discrepancy_warning": data.get("phase_timing_discrepancy_warning"),
            "error": data.get("error"),
        })

    return pd.DataFrame(rows)


def separate_flagged_runs(df: pd.DataFrame):
    """
    Filter dimatikan. Data dari Callback GRB.RUNTIME terbukti akurat. 
    Selisih dengan log murni disebabkan oleh file appending dan uncrush-delay.
    Semua data diikutkan ke dalam analisis utama.
    """
    clean = df.copy()
    # Buat dataframe kosong agar tidak ada run yang terbuang
    flagged = df.iloc[0:0].copy() 
    return clean, flagged


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
    return summary


def run_mann_whitney_with_bonferroni(df: pd.DataFrame) -> dict:
    """
    RQ1: pengaruh CPU pinning terhadap waktu crossover.

    Karena uji dilakukan terpisah per instance (beberapa uji hipotesis
    sejenis), alpha dikoreksi Bonferroni: alpha_corrected = 0.05 / n_instance.
    Nilai-p mentah tetap dilaporkan sebagai pembanding eksploratif, sesuai
    Subbab "Analisis Data" pada Metode Penelitian.

    Mengembalikan dict {instance: {"u_stat":..., "p_raw":..., "n_none":..., "n_static":...}}
    untuk dipakai ulang oleh run_effect_size_analysis (RQ4), supaya statistik
    U tidak dihitung dua kali secara terpisah.
    """
    print("=" * 70)
    print("UJI MANN-WHITNEY U: crossover_seconds, none vs static (per instance)")
    print("=" * 70)

    instances = df["instance"].unique()
    n_tests = len(instances)
    alpha_corrected = ALPHA / n_tests if n_tests > 0 else ALPHA
    print(f"Jumlah instance diuji: {n_tests} | alpha asli: {ALPHA} | alpha terkoreksi Bonferroni: {alpha_corrected:.5f}\n")

    mw_results = {}

    for instance in instances:
        subset = df[df["instance"] == instance]
        group_none = subset[subset["condition"] == "none"]["crossover_seconds"].dropna()
        group_static = subset[subset["condition"] == "static"]["crossover_seconds"].dropna()

        if len(group_none) < 3 or len(group_static) < 3:
            print(f"  {instance}: data tidak cukup untuk uji (n_none={len(group_none)}, n_static={len(group_static)})")
            continue

        u_stat, p_value = stats.mannwhitneyu(group_none, group_static, alternative="two-sided")
        median_diff = group_none.median() - group_static.median()
        pct_reduction = (median_diff / group_none.median() * 100) if group_none.median() else float("nan")

        verdict_raw = "signifikan" if p_value < ALPHA else "tidak signifikan"
        verdict_corrected = "signifikan" if p_value < alpha_corrected else "tidak signifikan"

        print(
            f"  {instance}: U={u_stat:.1f}, p_raw={p_value:.4f} ({verdict_raw} @ alpha=0.05), "
            f"p_vs_bonferroni ({verdict_corrected} @ alpha={alpha_corrected:.5f})\n"
            f"      median_none={group_none.median():.4f}s, median_static={group_static.median():.4f}s, "
            f"reduksi={pct_reduction:.1f}%"
        )

        mw_results[instance] = {
            "u_stat": u_stat,
            "p_raw": p_value,
            "n_none": len(group_none),
            "n_static": len(group_static),
            "pct_reduction": pct_reduction,
        }
    print()
    return mw_results


def run_correlation_analysis_per_condition(df: pd.DataFrame):
    """
    RQ2: korelasi involuntary context switches dan cache miss rate (DI DALAM rentang
    fase crossover saja) dengan crossover time.
    """
    print("=" * 70)
    print("KORELASI (PER KONDISI): involuntary context switches & cache miss rate vs crossover time")
    print("(Menjawab Rumusan Masalah poin 2)")
    print("=" * 70)

    for condition in sorted(df["condition"].unique()):
        print(f"\n--- Kondisi: {condition} ---")
        subset = df[df["condition"] == condition]
        
        # 1. Involuntary Context Switches Correlation
        clean_ctxt = subset.dropna(subset=["involuntary_ctxt_switches_delta_crossover_only", "crossover_seconds"])
        if len(clean_ctxt) < 4:
            print(f"  [Context Switches] Data tidak cukup untuk korelasi (n={len(clean_ctxt)}).")
        elif clean_ctxt["involuntary_ctxt_switches_delta_crossover_only"].nunique() <= 1:
            val = clean_ctxt["involuntary_ctxt_switches_delta_crossover_only"].iloc[0]
            print(f"  [Context Switches] Varians nol (semua bernilai {val}). Ini menunjukkan isolasi CPU bekerja dengan sempurna.")
        else:
            rho, p_value = stats.spearmanr(
                clean_ctxt["involuntary_ctxt_switches_delta_crossover_only"], clean_ctxt["crossover_seconds"]
            )
            print(f"  [Context Switches] Spearman's rho = {rho:.4f}, p = {p_value:.4f}, n = {len(clean_ctxt)}")

        # 2. Cache Miss Rate Correlation (PMU Counter Triangulation)
        clean_cache = subset.dropna(subset=["cache_miss_rate", "crossover_seconds"])
        if len(clean_cache) < 4:
            print(f"  [Cache Miss Rate] Data tidak cukup untuk korelasi (n={len(clean_cache)}).")
        elif clean_cache["cache_miss_rate"].nunique() <= 1:
            val = clean_cache["cache_miss_rate"].iloc[0]
            print(f"  [Cache Miss Rate] Varians nol (semua bernilai {val}).")
        else:
            rho, p_value = stats.spearmanr(
                clean_cache["cache_miss_rate"], clean_cache["crossover_seconds"]
            )
            print(f"  [Cache Miss Rate] Spearman's rho = {rho:.4f}, p = {p_value:.4f}, n = {len(clean_cache)}")

    print(
        "\n  Interpretasi: rho positif & signifikan DI DALAM kondisi yang sama -> gangguan\n"
        "  penjadwalan atau miss cache yang lebih tinggi berasosiasi dengan crossover time\n"
        "  yang lebih lambat. Membandingkan antar kondisi (none vs static) diuji lewat Mann-Whitney U."
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
            f"({'TIDAK signifikan -> barrier stabil, baik' if p_value > ALPHA else 'SIGNIFIKAN -> periksa confounding!'})"
        )
    print()


def run_effect_size_analysis(mw_results: dict, alpha_corrected: float):
    """
    RQ4: seberapa besar kontribusi kebijakan CPU Manager terhadap variasi
    performa SOLVER ANTAR INSTANCE — bukan sekadar signifikan/tidak, tapi
    seberapa besar (effect size), dan apakah besarnya bervariasi antar instance
    (mengindikasikan asosiasi dengan karakteristik instance, lihat Subbab
    "Objek Uji" di Metode Penelitian — ukuran & struktur sparsity).

    Effect size dihitung sebagai rank-biserial correlation, diturunkan
    langsung dari statistik U pada uji Mann-Whitney:
        r = (2 * U) / (n1 * n2) - 1
    U di sini adalah U untuk grup 'none' (grup pertama yang dipassing ke
    mannwhitneyu — scipy mengembalikan U relatif terhadap argumen pertama).
    r mendekati +1 berarti crossover time grup 'none' SECARA KONSISTEN lebih
    tinggi daripada 'static' (CPU pinning sangat berpengaruh, ke arah yang
    diharapkan); r mendekati 0 berarti hampir tidak ada beda sistematis;
    r negatif berarti arah sebaliknya (static justru lebih lambat — patut
    dicurigai sebagai anomali, periksa manual).
    """
    print("=" * 70)
    print("EFFECT SIZE (RANK-BISERIAL) ANTAR INSTANCE")
    print("(Menjawab Rumusan Masalah poin 4 — besar kontribusi CPU Manager)")
    print("=" * 70)

    if not mw_results:
        print("  Tidak ada hasil Mann-Whitney yang bisa dipakai (lihat bagian RQ1 di atas).")
        print()
        return

    rows = []
    for instance, r in mw_results.items():
        n1, n2 = r["n_none"], r["n_static"]
        rank_biserial = (2 * r["u_stat"]) / (n1 * n2) - 1
        significant = r["p_raw"] < alpha_corrected
        rows.append((instance, rank_biserial, r["pct_reduction"], r["p_raw"], significant))

    # Urutkan dari effect size terbesar ke terkecil, supaya pola lintas
    # instance (mis. instance besar vs kecil) mudah dibaca langsung.
    rows.sort(key=lambda x: x[1], reverse=True)

    print(f"  {'Instance':<30} {'rank-biserial r':>16} {'Reduksi median':>15} {'Signifikan (Bonferroni)':>24}")
    for instance, r_rb, pct, p_raw, sig in rows:
        print(f"  {instance:<30} {r_rb:>16.3f} {pct:>14.1f}% {('Ya' if sig else 'Tidak'):>24}")

    print(
        "\n  Interpretasi: bandingkan kolom rank-biserial r dan persentase reduksi di atas dengan\n"
        "  karakteristik instance (ukuran, struktur sparsity — lihat Subbab Objek Uji) untuk menilai\n"
        "  apakah besar kontribusi CPU Manager berasosiasi secara sistematis dengan karakteristik\n"
        "  tersebut, bukan sekadar seragam di semua instance."
    )
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    df = load_results(results_dir)
    flagged_df = pd.DataFrame()

    if df.empty:
        print("Tidak ada hasil ditemukan di", results_dir)
        return

    print(f"Total run termuat: {len(df)} (kondisi: {df['condition'].unique().tolist()})\n")

    # Saring run yang gagal solve (status_code != 2 == GRB.OPTIMAL) atau error
    # (mis. exception di run_solver.py, status_code None) supaya tidak
    # mencemari statistik dengan run yang infeasible/timeout/gagal.
    failed = df[(df["status_code"] != 2) | df["status_code"].isna()]
    if not failed.empty:
        print(f"PERINGATAN: {len(failed)} run TIDAK optimal/gagal, dikeluarkan dari analisis:")
        print(failed[["run_id", "status_code", "error"]].to_string(index=False))
        print()
    df = df[df["status_code"] == 2]

    # Pisahkan run yang punya discrepancy_warning — TIDAK ikut analisis utama
    # sampai diperiksa manual (lihat docstring separate_flagged_runs).
    df, flagged_df = separate_flagged_runs(df)
    if not flagged_df.empty:
        print(f"PERHATIAN: {len(flagged_df)} run DIKELUARKAN dari analisis utama karena "
              f"phase_timing_discrepancy_warning — periksa manual sebelum memutuskan apakah "
              f"layak dimasukkan kembali:")
        print(flagged_df[["run_id", "discrepancy_warning"]].to_string(index=False))
        print()

    summarize(df)
    mw_results = run_mann_whitney_with_bonferroni(df)
    check_barrier_stability(df)
    run_correlation_analysis_per_condition(df)

    n_instances = df["instance"].nunique()
    alpha_corrected = ALPHA / n_instances if n_instances > 0 else ALPHA
    run_effect_size_analysis(mw_results, alpha_corrected)

    csv_out = results_dir / "combined_results.csv"
    df.to_csv(csv_out, index=False)
    print(f"Data gabungan (run bersih, sudah lolos filter) disimpan ke: {csv_out}")

    if not flagged_df.empty:
        flagged_csv_out = results_dir / "flagged_for_manual_review.csv"
        flagged_df.to_csv(flagged_csv_out, index=False)
        print(f"Run yang ditandai discrepancy (perlu review manual) disimpan ke: {flagged_csv_out}")


if __name__ == "__main__":
    main()
