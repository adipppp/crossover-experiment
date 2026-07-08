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
import math
import re
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

        # Validasi struktur data hasil solver untuk menghindari file JSON lain di direktori yang sama
        if not isinstance(data, dict) or "run_id" not in data or "condition" not in data or "instance" not in data:
            continue

        sysmetrics_path = results_dir / f"{data['run_id']}.sysmetrics.json"
        ctxt_delta_crossover_only = None
        ctxt_delta_whole_process = None
        throttled_usec_delta = None
        cache_miss_rate = None
        ipc = None
        missing_cgroup_snapshot = None
        if sysmetrics_path.exists():
            try:
                sysmetrics = json.loads(sysmetrics_path.read_text())
                ctxt_delta_crossover_only = sysmetrics.get("involuntary_ctxt_switches_delta_crossover_phase_only")
                ctxt_delta_whole_process = sysmetrics.get("involuntary_ctxt_switches_delta_whole_process")
                throttled_usec_delta = sysmetrics.get("throttled_usec_delta")
                cache_miss_rate = sysmetrics.get("cache_miss_rate")
                ipc = sysmetrics.get("ipc")
                missing_cgroup_snapshot = sysmetrics.get("missing_cgroup_snapshot")
            except json.JSONDecodeError:
                print(f"PERINGATAN: gagal parse {sysmetrics_path}, metrik sistem run ini diabaikan.")

        wall_clock = data.get("wall_clock_total_seconds_DO_NOT_USE_FOR_PHASE_ANALYSIS") or data.get("wall_clock_total_seconds")

        # Parse nomor blok dari run_id (format: condition-instance-blkN-runXX).
        # Run lama (sebelum Phase 4) tidak mengandung 'blk' — default ke blok 1.
        run_id_str = data.get("run_id", "")
        blk_match = re.search(r"-blk(\d+)-run", run_id_str)
        block = int(blk_match.group(1)) if blk_match else 1

        rows.append({
            "run_id": run_id_str,
            "block": block,
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
            "ipc": ipc,
            "discrepancy_warning": data.get("phase_timing_discrepancy_warning"),
            "missing_cgroup_snapshot": missing_cgroup_snapshot,
            "error": data.get("error"),
        })

    return pd.DataFrame(rows)


def separate_flagged_runs(df: pd.DataFrame):
    """
    Pisahkan run Kondisi A yang mengalami CFS throttling (throttled_usec_delta > 0).
    Run ini dikeluarkan dari analisis SENSITIVITY saja (lihat
    run_throttling_sensitivity_check di bawah), BUKAN dari analisis utama RM1.
    Analisis utama tetap memakai seluruh data.

    Sesuai proposal Subbab "Analisis Data" RM1:
    "Apabila kedua hasil konsisten, throttling dapat disingkirkan sebagai
    confounding factor utama bagi RM1."
    """
    # Run yang di-flag: Kondisi A dengan throttled_usec_delta > 0
    throttled_mask = (
        (df["condition"] == "none") &
        (df["throttled_usec_delta"].notna()) &
        (df["throttled_usec_delta"] > 0)
    )
    flagged = df[throttled_mask].copy()
    clean   = df.copy()  # analisis utama tetap pakai semua data
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


def compute_mde_rank_biserial(n1: int, n2: int, alpha: float,
                              power: float = 0.80) -> float:
    """
    Estimasi minimum detectable effect size (rank-biserial r) untuk uji
    Mann-Whitney U dua sisi menggunakan aproksimasi normal.

    Rumus: r_MDE = (z_{alpha/2} + z_{power}) * SE(r)
    SE(r) = sqrt((n1+n2+1) / (3*n1*n2))

    Sesuai proposal Subbab "Prosedur Pengukuran":
    "Estimasi minimum detectable effect akan dilaporkan secara post hoc
    bersamaan dengan hasil pengujian RM1."
    Fungsi ini dipanggil per instance dengan n=N aktual setelah eksklusi
    run gagal, bukan dengan n=30 yang diasumsikan sebelum data terkumpul.
    """
    from scipy.stats import norm
    se_r   = math.sqrt((n1 + n2 + 1) / (3 * n1 * n2))
    z_crit = norm.ppf(1 - alpha / 2)
    z_pow  = norm.ppf(power)
    return (z_crit + z_pow) * se_r


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

        mde_r = compute_mde_rank_biserial(
            len(group_none), len(group_static), alpha_corrected
        )
        rank_biserial = (2 * u_stat) / (len(group_none) * len(group_static)) - 1
        mde_label = (
            f"terdeteksi (|r|={abs(rank_biserial):.3f} >= MDE={mde_r:.3f})"
            if abs(rank_biserial) >= mde_r else
            f"di bawah MDE (|r|={abs(rank_biserial):.3f} < MDE={mde_r:.3f})"
        )
        print(
            f"      MDE @ 80% power, alpha={alpha_corrected:.5f}: r_min={mde_r:.3f} | "
            f"efek aktual {mde_label}"
        )

        mw_results[instance] = {
            "u_stat": u_stat,
            "p_raw": p_value,
            "n_none": len(group_none),
            "n_static": len(group_static),
            "pct_reduction": pct_reduction,
            "rank_biserial": rank_biserial,
            "mde_rank_biserial": mde_r,
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
    print("CATATAN: Involuntary context switches di-scope KHUSUS pada fase crossover.")
    print("         Metrik PMU (cache miss rate, IPC) adalah WHOLE-PROCESS karena")
    print("         keterbatasan teknis perf stat dalam mem-scope eksekusi internal.")
    print("=" * 70)

    for condition in sorted(df["condition"].unique()):
        print(f"\n--- Kondisi: {condition} ---")
        cond_df = df[df["condition"] == condition]
        
        # 1. Involuntary Context Switches Correlation
        clean_ctxt = cond_df.dropna(subset=["involuntary_ctxt_switches_delta_crossover_only", "crossover_seconds"])
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
        clean_cache = cond_df.dropna(subset=["cache_miss_rate", "crossover_seconds"])
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

        # 3. IPC Correlation (proksi efisiensi eksekusi per siklus — sesuai proposal RQ2)
        # IPC yang lebih rendah pada kondisi none mengindikasikan lebih banyak stall
        # memori akibat cache miss; IPC yang lebih tinggi pada static mengindikasikan
        # perbaikan efisiensi eksekusi (bukan hanya konsistensi alokasi waktu CPU).
        clean_ipc = cond_df.dropna(subset=["ipc", "crossover_seconds"])
        if len(clean_ipc) < 4:
            print(f"  [IPC]            Data tidak cukup untuk korelasi (n={len(clean_ipc)}).")
        elif "ipc" not in cond_df.columns or clean_ipc["ipc"].nunique() <= 1:
            print(f"  [IPC]            Varians nol atau kolom tidak tersedia (PMU mungkin NO-GO).")
        else:
            rho, p_value = stats.spearmanr(
                clean_ipc["ipc"], clean_ipc["crossover_seconds"]
            )
            # Ekspektasi: rho NEGATIF (IPC tinggi -> crossover cepat)
            direction = "negatif (sesuai ekspektasi)" if rho < 0 else "positif (periksa!)"
            print(f"  [IPC]            Spearman's rho = {rho:.4f}, p = {p_value:.4f}, n = {len(clean_ipc)} [{direction}]")

    print(
        "\n  Interpretasi: rho positif & signifikan untuk context-switch/cache-miss"
        " DI DALAM kondisi yang sama -> gangguan penjadwalan atau miss cache lebih"
        " tinggi berasosiasi dengan crossover time lebih lambat.\n"
        "  rho negatif untuk IPC -> IPC lebih tinggi berasosiasi dengan crossover"
        " time lebih cepat.\n"
        "  Ketiga proksi yang konsisten arahnya memperkuat plausibilitas mekanisme"
        " migrasi thread (bukan membuktikan kausalitas — lihat Keterbatasan Metodologis)."
    )
    print()


def check_warming_trend(df: pd.DataFrame):
    print("=" * 70)
    print("TREND ANALYSIS UNTUK WARMING EFFECT")
    print("(Pemeriksaan monotonisitas waktu crossover dalam satu blok)")
    print("=" * 70)
    
    trend_found = False
    for instance in df["instance"].unique():
        for condition in ["none", "static"]:
            for block in df["block"].unique():
                subset = df[
                    (df["instance"] == instance) &
                    (df["condition"] == condition) &
                    (df["block"] == block)
                ].sort_values("run_id")
                if len(subset) < 5:
                    continue
                rho, p = stats.spearmanr(
                    range(len(subset)),
                    subset["crossover_seconds"].values
                )
                if p < 0.05:
                    print(f"  ⚠ Tren terdeteksi: {instance} / Kondisi {condition} / Blok {block} | "
                          f"rho={rho:.3f} p={p:.3f}")
                    trend_found = True
                    
    if not trend_found:
        print("  ✓ KEPUTUSAN: Tidak ada tren monoton (warming effect) yang signifikan.")
    else:
        print("  CATATAN: Ada bukti residual warming effect. Laporkan di Keterbatasan.")
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


def check_order_effect(df: pd.DataFrame) -> bool:
    """
    Periksa apakah urutan blok (A→B vs B→A) berdampak signifikan pada
    crossover time, untuk memvalidasi asumsi block counterbalancing.

    Sesuai proposal Subbab "Prosedur Eksperimen":
    Dua uji Mann-Whitney U terpisah pada data AGREGAT lintas instance:
      Uji 1: Kondisi A Blok 1 vs Kondisi A Blok 2
      Uji 2: Kondisi B Blok 1 vs Kondisi B Blok 2
    alpha = 0.05 (tidak dikoreksi Bonferroni — ini sanity check, bukan uji utama).

    Kembalikan True jika kedua uji tidak signifikan (data bisa digabung).
    """
    print("=" * 70)
    print("PEMERIKSAAN EFEK URUTAN (ORDER-EFFECT CHECK)")
    print("(Validasi block counterbalancing — Subbab 'Prosedur Eksperimen')")
    print("=" * 70)

    # Periksa apakah kedua blok tersedia
    blocks_available = df["block"].unique().tolist()
    if len(blocks_available) < 2 or not (1 in blocks_available and 2 in blocks_available):
        print(f"  Blok tersedia: {blocks_available}")
        print("  Blok 1 dan 2 belum keduanya tersedia — skip order-effect check.")
        print("  (Jalankan run_full_experiment.sh block2 terlebih dahulu.)")
        print()
        return True  # Asumsikan tidak ada masalah; analisis tetap lanjut

    both_non_sig = True
    for condition in ["none", "static"]:
        grp1 = df[(df["condition"] == condition) & (df["block"] == 1)]["crossover_seconds"].dropna()
        grp2 = df[(df["condition"] == condition) & (df["block"] == 2)]["crossover_seconds"].dropna()

        label_cond = "A (none)" if condition == "none" else "B (static)"
        if len(grp1) < 3 or len(grp2) < 3:
            print(f"  Kondisi {label_cond}: data tidak cukup (n_blk1={len(grp1)}, n_blk2={len(grp2)})")
            continue

        u_stat, p_val = stats.mannwhitneyu(grp1, grp2, alternative="two-sided")
        sig = p_val < ALPHA
        verdict = "SIGNIFIKAN ⚠" if sig else "tidak signifikan ✓"
        print(
            f"  Kondisi {label_cond}: Blok1 median={grp1.median():.4f}s, "
            f"Blok2 median={grp2.median():.4f}s | "
            f"U={u_stat:.1f}, p={p_val:.4f} [{verdict}]"
        )
        if sig:
            both_non_sig = False

    print()
    if both_non_sig:
        print("  ✓ KEPUTUSAN: Tidak ada efek urutan signifikan.")
        print("    Median dari 30 repetisi gabungan (Blok 1 + Blok 2) digunakan")
        print("    sebagai hasil utama pada RQ1–RQ4.")
    else:
        print("  ⚠  KEPUTUSAN: Efek urutan SIGNIFIKAN terdeteksi pada minimal satu kondisi.")
        print("    Hasil per blok dilaporkan TERPISAH selain gabungan.")
        print("    Pertimbangkan model mixed-effects (blok sebagai random effect)")
        print("    untuk estimasi efek kondisi yang tidak bias oleh urutan.")
        print("    (Analisis mixed-effects tidak diimplementasikan di skrip ini —")
        print("     diklasifikasikan sebagai penelitian lanjutan per proposal.)")
    print()
    return both_non_sig


def run_throttling_sensitivity_check(df_all: pd.DataFrame, mw_results_all: dict,
                                     alpha_corrected: float):
    """
    Uji sensitivitas RM1: jalankan ulang Mann-Whitney U dengan mengekslusi
    run Kondisi A yang mengalami CFS throttling (throttled_usec_delta > 0).

    Sesuai proposal Subbab "Analisis Data" RM1:
    "Apabila kedua hasil konsisten, throttling dapat disingkirkan sebagai
    confounding factor utama bagi RM1. Apabila tidak konsisten, perbedaan
    ini dilaporkan secara eksplisit."
    """
    print("=" * 70)
    print("UJI SENSITIVITAS THROTTLING (RQ1)")
    print("(Memverifikasi throttling bukan confounding factor dominan)")
    print("=" * 70)

    # Identifikasi run ter-throttle
    throttled_mask = (
        (df_all["condition"] == "none") &
        (df_all["throttled_usec_delta"].notna()) &
        (df_all["throttled_usec_delta"] > 0)
    )
    n_throttled = throttled_mask.sum()
    n_total_none = (df_all["condition"] == "none").sum()
    n_missing = (
        (df_all["condition"] == "none") &
        (df_all.get("missing_cgroup_snapshot", False) == True)
    ).sum()

    print(f"  Run Kondisi A dengan throttled_usec_delta > 0: {n_throttled} / {n_total_none}")
    if n_missing > 0:
        print(f"  Run Kondisi A dengan missing_cgroup_snapshot: {n_missing} (tidak diketahui status throttling-nya)")

    if n_throttled == 0:
        print("  Tidak ada run yang ter-throttle — sensitivity check tidak berlaku.")
        print("  Throttling bukan faktor sama sekali pada dataset ini.")
        print()
        return

    df_excl = df_all[~throttled_mask].copy()

    # Re-run Mann-Whitney U pada data ter-filter (print singkat)
    instances = df_all["instance"].unique()
    consistent = True
    print()
    print(f"  {'Instance':<30} {'All (p_raw)':>14} {'Excl throttled (p_raw)':>24} {'Konsisten?':>12}")
    print("  " + "─" * 82)
    for instance in instances:
        if instance not in mw_results_all:
            continue
        sub = df_excl[df_excl["instance"] == instance]
        g_none   = sub[sub["condition"] == "none"]["crossover_seconds"].dropna()
        g_static = sub[sub["condition"] == "static"]["crossover_seconds"].dropna()
        if len(g_none) < 3 or len(g_static) < 3:
            print(f"  {instance:<30} {'(data terlalu sedikit setelah eksklusi)':>52}")
            continue

        _, p_excl = stats.mannwhitneyu(g_none, g_static, alternative="two-sided")
        p_all = mw_results_all[instance]["p_raw"]
        sig_all  = p_all  < alpha_corrected
        sig_excl = p_excl < alpha_corrected
        ok = sig_all == sig_excl
        if not ok:
            consistent = False
        print(
            f"  {instance:<30} {p_all:>14.4f} {p_excl:>24.4f} "
            f"{'✓' if ok else '⚠  BEDA':>12}"
        )

    print()
    if consistent:
        print("  ✓ KONSISTEN: Verdict signifikansi tidak berubah setelah eksklusi.")
        print("    Throttling bukan confounding factor dominan untuk RQ1.")
    else:
        print("  ⚠  TIDAK KONSISTEN: Verdict berubah setelah eksklusi run ter-throttle.")
        print("    Laporkan kedua hasil (semua data / setelah eksklusi) secara terpisah.")
        print("    Periksa run ter-throttle secara manual sebelum interpretasi akhir.")
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

    # Pisahkan run Kondisi A yang mengalami CFS throttling (tidak dikeluarkan dari utama)
    df, flagged_df = separate_flagged_runs(df)
    if not flagged_df.empty:
        print(f"INFO: {len(flagged_df)} run Kondisi A mengalami CFS throttling "
              f"(throttled_usec_delta > 0) — TETAP masuk analisis utama, "
              f"namun diuji ulang secara terpisah di throttling sensitivity check.")
        print(flagged_df[["run_id", "throttled_usec_delta"]].to_string(index=False))
        print()

    n_missing_cgroup = df["missing_cgroup_snapshot"].sum() if "missing_cgroup_snapshot" in df.columns else 0
    if n_missing_cgroup > 0:
        print(f"INFO: {int(n_missing_cgroup)} run kehilangan snapshot cgroup (terkena race condition). "
              f"Throttled runs mungkin undercounted.")
        print()

    n_instances = df["instance"].nunique()
    alpha_corrected = ALPHA / n_instances if n_instances > 0 else ALPHA

    # Pemeriksaan efek urutan (Phase 4 — block counterbalancing)
    # Harus dijalankan SEBELUM analisis utama untuk menentukan apakah
    # hasil 30 rep digabung atau dilaporkan per blok.
    both_blocks_ok = check_order_effect(df)
    if not both_blocks_ok:
        print("  CATATAN: Analisis RQ1-RQ4 di bawah tetap menggunakan data GABUNGAN")
        print("  (30 rep). Hasil per blok tersedia di combined_results.csv kolom 'block'.")
        print()

    check_warming_trend(df)

    summarize(df)
    mw_results = run_mann_whitney_with_bonferroni(df)

    # Uji sensitivitas throttling (Phase 5 — sesuai proposal RQ1)
    run_throttling_sensitivity_check(df, mw_results, alpha_corrected)

    check_barrier_stability(df)
    run_correlation_analysis_per_condition(df)
    run_effect_size_analysis(mw_results, alpha_corrected)

    csv_out = results_dir / "combined_results.csv"
    df.to_csv(csv_out, index=False)
    print(f"Data gabungan (run bersih, sudah lolos filter) disimpan ke: {csv_out}")

    if not flagged_df.empty:
        flagged_csv_out = results_dir / "throttled_runs_for_sensitivity.csv"
        flagged_df.to_csv(flagged_csv_out, index=False)
        print(f"Run yang ditandai mengalami throttling disimpan ke: {flagged_csv_out}")


if __name__ == "__main__":
    main()
