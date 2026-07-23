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

        # Filter out contaminated smoke test files (e.g., -run1 suffix without leading zero)
        _run_id = data.get("run_id", "")
        if re.search(r"-run[0-9]$", _run_id):
            print(f"Skipping contaminated smoke test: {json_path.name} (run_id={_run_id})")
            continue

        sysmetrics_path = results_dir / f"{_run_id}.sysmetrics.json"
        ctxt_delta_crossover_only = None
        ctxt_delta_whole_process = None
        throttled_usec_delta = None
        cache_miss_rate = None
        ipc = None
        missing_cgroup_snapshot = None
        # TAMBAHAN pasca-audit: steal time (lihat collect_system_metrics.py) --
        # dipakai sbg kovariat/pemeriksa tambahan untuk mendeteksi kontensi
        # level-hypervisor (noisy-neighbor), termasuk untuk investigasi anomali
        # temporal semacam Subbab 4.3.5. None untuk data lama sebelum fix ini.
        steal_seconds_crossover = None
        steal_seconds_whole_process = None
        if sysmetrics_path.exists():
            try:
                sysmetrics = json.loads(sysmetrics_path.read_text())
                ctxt_delta_crossover_only = sysmetrics.get("involuntary_ctxt_switches_delta_crossover_phase_only")
                ctxt_delta_whole_process = sysmetrics.get("involuntary_ctxt_switches_delta_whole_process")
                throttled_usec_delta = sysmetrics.get("throttled_usec_delta")
                cache_miss_rate = sysmetrics.get("cache_miss_rate")
                ipc = sysmetrics.get("ipc")
                missing_cgroup_snapshot = sysmetrics.get("missing_cgroup_snapshot")
                steal_seconds_crossover = sysmetrics.get("steal_seconds_delta_crossover_phase")
                steal_seconds_whole_process = sysmetrics.get("steal_seconds_delta_whole_process")
            except json.JSONDecodeError:
                print(f"PERINGATAN: gagal parse {sysmetrics_path}, metrik sistem run ini diabaikan.")

        wall_clock = data.get("wall_clock_total_seconds_DO_NOT_USE_FOR_PHASE_ANALYSIS") or data.get("wall_clock_total_seconds")

        # Parse nomor blok dari run_id (format: condition-instance-blkN-runXX).
        # Run lama (sebelum Phase 4) tidak mengandung 'blk' — default ke blok 1.
        run_id_str = data.get("run_id", "")
        blk_match = re.search(r"-blk(\d+)-run", run_id_str)
        block = int(blk_match.group(1)) if blk_match else 1

        # Parse nomor run untuk pengurutan numerik temporal yang presisi
        rep_match = re.search(r"-run(\d+)", run_id_str)
        rep_index = int(rep_match.group(1)) if rep_match else 0

        rows.append({
            "run_id": run_id_str,
            "block": block,
            "rep_index": rep_index,
            "condition": data.get("condition"),
            "instance": Path(data.get("instance", "")).stem if data.get("instance") else None,
            "status_code": data.get("status_code"),
            "crossover_seconds": data.get("crossover_seconds"),
            "barrier_iteration_seconds": data.get("barrier_iteration_seconds"),
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
            # TAMBAHAN pasca-audit (lihat CHANGELOG.md):
            "steal_seconds_delta_crossover_phase": steal_seconds_crossover,
            "steal_seconds_delta_whole_process": steal_seconds_whole_process,
            "barrier_to_crossover_transition_gap_seconds": data.get("barrier_to_crossover_transition_gap_seconds"),
        })

    return pd.DataFrame(rows)


def identify_throttled_runs(df: pd.DataFrame, proportional_threshold: float = 0.05):
    """
    Identifikasi run Kondisi A yang mengalami CFS throttling SECARA BERMAKNA
    (bukan sekadar throttled_usec_delta > 0). Run ini di-flag untuk analisis
    SENSITIVITY saja (lihat run_throttling_sensitivity_check di bawah), BUKAN
    untuk dikeluarkan dari analisis utama RM1. Analisis utama tetap memakai
    seluruh data.

    PENTING — ambang PROPORSIONAL, bukan biner:
    Versi sebelumnya mengeklusi run berdasarkan throttled_usec_delta > 0 saja
    (biner: ada/tidak ada throttling sama sekali). Ini bermasalah untuk
    instance dengan crossover time panjang (mis. L1_sixm1000obs, ~2.5s):
    throttling ratusan ribu mikrodetik terlihat besar dalam angka absolut,
    tapi proporsinya terhadap durasi crossover itulah yang relevan terhadap
    ambang 5% yang ditetapkan Rev3 — bukan sekadar non-zero. Ambang biner
    membuang HAMPIR SEMUA run pada instance dengan throttling tersebar
    (kebanyakan run sedikit throttle, jarang yang benar-benar bersih),
    menyisakan sampel yang terlalu kecil untuk diuji ("data terlalu sedikit
    setelah eksklusi") — bukan karena datanya memang tidak cukup, tapi karena
    ambangnya terlalu ketat untuk pertanyaan yang sebenarnya ingin dijawab:
    "apakah throttling YANG BERMAKNA memengaruhi kesimpulan RQ1?"

    proportional_threshold: proporsi throttled_usec_delta terhadap
    crossover_seconds (dikonversi ke skala yang sama, detik) di ATAS mana
    sebuah run dianggap "throttled secara bermakna". Default 0.05 (5%),
    sesuai ambang yang ditetapkan proposal Rev3 untuk indikator throttling
    overhead. Run dengan crossover_seconds tidak tersedia (None/NaN) tidak
    bisa dihitung proporsinya — diperlakukan sebagai throttled (konservatif)
    agar tidak keliru dimasukkan ke sampel "bersih" tanpa dasar.
    """
    is_none = df["condition"] == "none"
    has_throttle_data = df["throttled_usec_delta"].notna()
    has_crossover_time = df["crossover_seconds"].notna() & (df["crossover_seconds"] > 0)

    # Proporsi throttling terhadap durasi crossover (throttled_usec_delta
    # dalam mikrodetik -> detik, dibagi crossover_seconds dalam detik).
    throttle_proportion = pd.Series(index=df.index, dtype=float)
    valid_for_ratio = has_throttle_data & has_crossover_time
    throttle_proportion[valid_for_ratio] = (
        (df.loc[valid_for_ratio, "throttled_usec_delta"] / 1_000_000)
        / df.loc[valid_for_ratio, "crossover_seconds"]
    )

    # Run dianggap "throttled secara bermakna" jika:
    # (a) proporsinya di atas ambang, ATAU
    # (b) datanya tidak cukup untuk dihitung (konservatif — tidak dimasukkan
    #     ke sampel bersih tanpa bukti bahwa throttling-nya memang rendah)
    meaningfully_throttled = is_none & (
        (valid_for_ratio & (throttle_proportion > proportional_threshold)) |
        (~valid_for_ratio & has_throttle_data)  # ada data tapi crossover_seconds hilang
    )

    flagged = df[meaningfully_throttled].copy()
    flagged["throttle_proportion_of_crossover"] = throttle_proportion[meaningfully_throttled]
    return flagged


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

        if len(group_none) < 8 or len(group_static) < 8:
            print(f"  [PERINGATAN] {instance}: jumlah sampel di bawah batas validitas metodologi (n_none={len(group_none)}, n_static={len(group_static)} < 8-10)")

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
        # Issue 5: Print number of valid context switch samples (non-None)
        clean_ctxt = cond_df.dropna(subset=["involuntary_ctxt_switches_delta_crossover_only", "crossover_seconds"])
        print(f"  [Context Switches] Valid samples (non-None): {len(clean_ctxt)}")
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
            # Issue 2: Inline caveat warning that cache_miss_rate is a whole-process metric
            print(f"  [Cache Miss Rate] Spearman's rho = {rho:.4f}, p = {p_value:.4f}, n = {len(clean_cache)} (WHOLE-PROCESS metric — see §VIII caveat)")

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
             # Ekspektasi: rho NEGATIF (IPC tinggi -> crossover time lebih rendah)
            direction = "negatif (sesuai ekspektasi)" if rho < 0 else "positif (periksa!)"
            # Issue 2: Inline caveat warning that IPC is a whole-process metric
            print(f"  [IPC]            Spearman's rho = {rho:.4f}, p = {p_value:.4f}, n = {len(clean_ipc)} [{direction}] (WHOLE-PROCESS metric — see §VIII caveat)")

    print(
        "\n  Interpretasi: rho positif & signifikan untuk context-switch/cache-miss"
        " DI DALAM kondisi yang sama -> gangguan penjadwalan atau miss cache lebih"
        " tinggi berasosiasi dengan crossover time lebih lambat.\n"
        "  rho negatif untuk IPC -> IPC lebih tinggi berasosiasi dengan crossover"
        " time lebih singkat (cepat).\n"
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
                ].sort_values("rep_index")
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
    print("STABILITAS FASE BARRIER ANTAR KONDISI (RQ3)")
    print("(Menjawab Rumusan Masalah poin 3 — apakah perbedaan performa benar")
    print(" berasal dari crossover, bukan dari barrier yang juga berubah,")
    print(" serta konsistensi jumlah iterasi barrier antar konfigurasi)")
    print("=" * 70)

    for instance in df["instance"].unique():
        subset = df[df["instance"] == instance]
        group_none = subset[subset["condition"] == "none"]["barrier_iteration_seconds"].dropna()
        group_static = subset[subset["condition"] == "static"]["barrier_iteration_seconds"].dropna()

        group_none_iters = subset[subset["condition"] == "none"]["barrier_iter_count"].dropna()
        group_static_iters = subset[subset["condition"] == "static"]["barrier_iter_count"].dropna()

        if len(group_none) < 3 or len(group_static) < 3:
            print(f"  {instance}: data tidak cukup untuk stabilitas barrier (n_none={len(group_none)}, n_static={len(group_static)}) — dilewati.")
            continue

        if len(group_none) < 8 or len(group_static) < 8:
            print(f"  [PERINGATAN] {instance}: jumlah sampel di bawah batas validitas metodologi (n_none={len(group_none)}, n_static={len(group_static)} < 8-10) untuk stabilitas barrier")

        u_stat, p_value = stats.mannwhitneyu(group_none, group_static, alternative="two-sided")
        
        iter_none_med = group_none_iters.median() if not group_none_iters.empty else 0
        iter_static_med = group_static_iters.median() if not group_static_iters.empty else 0
        if iter_none_med > 0:
            iter_diff_pct = (abs(iter_none_med - iter_static_med) / iter_none_med) * 100
        else:
            iter_diff_pct = 0.0

        print(
            f"  {instance}:\n"
            f"    Waktu Barrier:   median_none={group_none.median():.4f}s, median_static={group_static.median():.4f}s, p={p_value:.4f}\n"
            f"                    ({'TIDAK signifikan -> barrier stabil, baik' if p_value > ALPHA else 'SIGNIFIKAN -> periksa confounding!'})\n"
            f"                    (NOTE: barrier_iteration_seconds mengecualikan waktu presolve/startup Gurobi)\n"
            f"    Iterasi Barrier: median_none={iter_none_med:.0f}, median_static={iter_static_med:.0f}, beda={iter_diff_pct:.2f}%\n"
            f"                    ({'✓ KONSISTEN (beda < 5%)' if iter_diff_pct < 5.0 else '⚠ BEDA (beda >= 5%)'})\n"
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
        rank_biserial = r["rank_biserial"]
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
        print("  ⛔ PERINGATAN KRITIS: Efek urutan SIGNIFIKAN terdeteksi pada minimal satu kondisi.")
        print()
        print("  ══════════════════════════════════════════════════════════════════")
        print("  SESUAI PROPOSAL (Rev3): Mixed-effects model tidak diterapkan karena jumlah blok")
        print("  (N=2) tidak memadai untuk menghasilkan estimasi varians antar-level yang andal.")
        print("  Sebagai gantinya, analisis dilanjutkan dengan melaporkan hasil secara")
        print("  deskriptif per blok sebagai tambahan informasi.")
        print()
        print("  FALLBACK DARURAT: Analisis utama berikut DILANJUTKAN dengan DATA GABUNGAN")
        print("  (semua blok sekaligus), NAMUN hasil ini HARUS ditandai BIAS POTENSIAL")
        print("  di laporan akhir. Laporkan temuan ini secara eksplisit di Subbab")
        print("  'Keterbatasan Metodologis'.")
        print("  ══════════════════════════════════════════════════════════════════")
        print()
        print("  ANALISIS PER BLOK TERPISAH (informatif, sesuai Subbab 'Prosedur Eksperimen'):")
        for blok_num in [1, 2]:
            blok_label = "A→B (Blok 1)" if blok_num == 1 else "B→A (Blok 2)"
            print(f"\n  --- Blok {blok_num} ({blok_label}) ---")
            for condition in ["none", "static"]:
                grp = df[(df["condition"] == condition) & (df["block"] == blok_num)]["crossover_seconds"].dropna()
                label_cond = "A (none)" if condition == "none" else "B (static)"
                if len(grp) >= 1:
                    print(
                        f"    Kondisi {label_cond}: median={grp.median():.4f}s, "
                        f"IQR=[{grp.quantile(0.25):.4f}, {grp.quantile(0.75):.4f}], n={len(grp)}"
                    )
                else:
                    print(f"    Kondisi {label_cond}: tidak ada data (n=0)")
        print()

    print()
    return both_non_sig


def run_throttling_sensitivity_check(df_all: pd.DataFrame, mw_results_all: dict,
                                     alpha_corrected: float, proportional_threshold: float = 0.05):
    """
    Uji sensitivitas RM1: jalankan ulang Mann-Whitney U dengan mengekslusi
    run Kondisi A yang mengalami CFS throttling SECARA BERMAKNA — proporsi
    throttled_usec_delta terhadap crossover_seconds di atas
    proportional_threshold (default 5%, ambang Rev3), BUKAN throttled_usec_delta
    > 0 semata. Lihat catatan lengkap di identify_throttled_runs mengapa ambang
    proporsional dipakai alih-alih biner.

    Sesuai proposal Subbab "Analisis Data" RM1:
    "Apabila kedua hasil konsisten, throttling dapat disingkirkan sebagai
    confounding factor utama bagi RM1. Apabila tidak konsisten, perbedaan
    ini dilaporkan secara eksplisit."
    """
    print("=" * 70)
    print("UJI SENSITIVITAS THROTTLING (RQ1)")
    print(f"(Ambang eksklusi: throttling > {proportional_threshold:.0%} dari crossover_seconds per run)")
    print("=" * 70)

    is_none = df_all["condition"] == "none"
    has_throttle_data = df_all["throttled_usec_delta"].notna()
    has_crossover_time = df_all["crossover_seconds"].notna() & (df_all["crossover_seconds"] > 0)
    valid_for_ratio = has_throttle_data & has_crossover_time

    throttle_proportion = pd.Series(index=df_all.index, dtype=float)
    throttle_proportion[valid_for_ratio] = (
        (df_all.loc[valid_for_ratio, "throttled_usec_delta"] / 1_000_000)
        / df_all.loc[valid_for_ratio, "crossover_seconds"]
    )

    throttled_mask = is_none & (
        (valid_for_ratio & (throttle_proportion > proportional_threshold)) |
        (~valid_for_ratio & has_throttle_data)
    )
    n_throttled = throttled_mask.sum()
    n_total_none = is_none.sum()
    if "missing_cgroup_snapshot" in df_all.columns:
        missing_mask = is_none & (df_all["missing_cgroup_snapshot"] == True)
    else:
        missing_mask = pd.Series(False, index=df_all.index)
    n_missing = missing_mask.sum()

    print(f"  Run Kondisi A dengan throttling > {proportional_threshold:.0%} dari crossover_seconds: {n_throttled} / {n_total_none}")
    # Info tambahan: berapa run yang punya throttling non-zero TAPI di bawah
    # ambang (dulu dieksklusi oleh ambang biner, sekarang tetap masuk sampel
    # "bersih" karena proporsinya kecil) — membantu menjelaskan mengapa n
    # berbeda dari versi sebelumnya jika dibandingkan.
    n_nonzero_but_below_threshold = (
        is_none & valid_for_ratio & (throttle_proportion > 0) & (throttle_proportion <= proportional_threshold)
    ).sum()
    if n_nonzero_but_below_threshold > 0:
        print(f"  (Tambahan: {n_nonzero_but_below_threshold} run none punya throttling non-zero namun di bawah ambang — tetap masuk sampel 'bersih'.)")
    if n_missing > 0:
        print(f"  Run Kondisi A dengan missing_cgroup_snapshot: {n_missing} (tidak diketahui status throttling-nya)")

    if n_throttled == 0:
        if n_missing == n_total_none and n_total_none > 0:
            # Seluruh run Kondisi A gagal mendapat snapshot cpu.stat sama sekali —
            # kemungkinan besar akibat race condition cgroup (lihat catatan di
            # collect_system_metrics.py). n_throttled=0 di sini TIDAK berarti
            # throttling=0; artinya statusnya sama sekali tidak diketahui.
            print("  PERINGATAN: Seluruh run Kondisi A memiliki missing_cgroup_snapshot=True (100% gagal baca cpu.stat).")
            print("  Throttled_usec_delta tidak dapat dihitung sama sekali — snapshot cpu.stat tidak tersedia")
            print("  untuk run-run ini (mis. proses exit sebelum snapshot awal sempat dibaca).")
            print("  Uji sensitivitas throttling TIDAK DAPAT dijalankan. Ini BUKAN bukti bahwa throttling = 0;")
            print("  ini adalah keterbatasan pengukuran yang harus dicatat di bab Hasil/Keterbatasan.")
        else:
            # Snapshot cpu.stat berhasil didapat (n_missing < n_total_none), dan
            # tidak ada satu pun run yang throttling-nya MELEBIHI ambang
            # proporsional. Dua sub-kasus dibedakan secara eksplisit karena
            # bermakna berbeda: (a) throttled_usec_delta memang 0 di semua run
            # (throttling genuinely tidak terjadi), vs (b) ada throttling
            # non-zero di banyak run tapi proporsinya selalu di bawah ambang
            # (throttling terjadi tapi tidak bermakna secara praktis). Kasus
            # (b) TIDAK bisa disebut "tidak ada throttled_usec_delta > 0" —
            # itu keliru dan pernah jadi pesan versi sebelumnya.
            n_all_zero = (
                is_none & valid_for_ratio & (throttle_proportion == 0)
            ).sum()
            if n_all_zero == n_total_none:
                print("  INFO: throttled_usec_delta = 0 di SELURUH run Kondisi A — throttling genuinely")
                print("  tidak terjadi pada data ini, bukan sekadar di bawah ambang. Konsisten dengan")
                print("  ekspektasi proposal Rev3 (limits.cpu=4 pada Kondisi A membuat throttling MUNGKIN")
                print("  terjadi, bukan PASTI terjadi).")
            else:
                print(f"  INFO: Sejumlah run Kondisi A mengalami throttling non-zero (lihat baris 'Tambahan' di atas),")
                print(f"  namun SELURUHNYA berada di bawah ambang {proportional_threshold:.0%} dari crossover_seconds")
                print("  masing-masing run — sehingga tidak ada run yang dieksklusi dari sampel 'bersih'.")
            print("  Uji sensitivitas dilewati karena tidak ada run untuk dieksklusi (himpunan 'excl")
            print("  throttled' identik dengan seluruh data). Ini adalah hasil yang MENDUKUNG bahwa")
            print("  throttling bukan confounding factor bermakna untuk RQ1 pada instance ini.")
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


def run_mannwhitney_context_switches(df: pd.DataFrame):
    """
    Uji konfirmatori RM2 — Mann-Whitney U langsung untuk involuntary context
    switches antara Kondisi none vs static.
    Ini adalah uji yang dijanjikan Rev3 Subbab 'Analisis Data' RM2 sebagai
    'uji konfirmatori utama bagi RM2' terhadap H1a, namun tidak diimplementasikan
    di script asli.
    """
    print("=" * 70)
    print("UJI MANN-WHITNEY U: INVOLUNTARY CONTEXT SWITCHES")
    print("(Uji konfirmatori RM2 — H1a: perbandingan langsung none vs static)")
    print("=" * 70)

    col = "involuntary_ctxt_switches_delta_crossover_only"
    if col not in df.columns:
        print("  Kolom not found — skip.")
        print()
        return

    # Per-instance
    for instance in sorted(df["instance"].unique()):
        sub = df[df["instance"] == instance]
        g_none = sub[sub["condition"] == "none"][col].dropna()
        g_static = sub[sub["condition"] == "static"][col].dropna()
        if len(g_none) < 3 or len(g_static) < 3:
            print(f"  {instance}: data tidak cukup (none_n={len(g_none)}, static_n={len(g_static)})")
            continue
        u_stat, p = stats.mannwhitneyu(g_none, g_static, alternative="two-sided")
        med_none = g_none.median()
        med_static = g_static.median()
        ratio = med_static / med_none if med_none > 0 else float('inf')
        print(f"  {instance:<20} none_med={med_none:>10.1f}  static_med={med_static:>10.1f}  "
              f"ratio={ratio:>6.1f}x  U={u_stat:.0f}  p={p:.8f}")

    # Gabungan semua instance
    g_none_all = df[df["condition"] == "none"][col].dropna()
    g_static_all = df[df["condition"] == "static"][col].dropna()
    if len(g_none_all) >= 3 and len(g_static_all) >= 3:
        u_stat_all, p_all = stats.mannwhitneyu(g_none_all, g_static_all, alternative="two-sided")
        med_none_all = g_none_all.median()
        med_static_all = g_static_all.median()
        ratio_all = med_static_all / med_none_all if med_none_all > 0 else float('inf')
        print(f"  {'Gabungan (n=' + str(len(g_none_all)) + ' vs ' + str(len(g_static_all)) + ')':<20} "
              f"none_med={med_none_all:>10.1f}  static_med={med_static_all:>10.1f}  "
              f"ratio={ratio_all:>6.1f}x  U={u_stat_all:.0f}  p={p_all:.8f}")

    print()
    print("  Interpretasi: Jika p < 0.05, H1a ditolak — ada perbedaan signifikan antara")
    print("  none dan static. Arah rasio menunjukkan kondisi mana yang lebih tinggi.")
    print()


def check_completeness(df: pd.DataFrame, expected_n: int = 30,
                        expected_blocks: tuple = (1, 2),
                        # PENTING: casing di sini HARUS cocok dengan nilai yang
                        # dihasilkan load_results() -- yaitu Path(data["instance"]).stem
                        # dari field "instance" pada JSON (path .mps ASLI, mis.
                        # "/app/instances/L1_sixm1000obs.mps"), BUKAN dari run_id
                        # yang huruf kecil/hyphen (mis. "l1-sixm1000obs" pada nama
                        # file hasil/Pod -- konvensi berbeda karena nama resource
                        # K8s harus lowercase DNS-compatible sedangkan file .mps asli
                        # dari koleksi Mittelmann memakai mixed-case). Diverifikasi
                        # langsung dari data: unique() instance sesungguhnya adalah
                        # ['L1_sixm1000obs', 'Linf_520c', 'cont1', 'cont11', 'neos3'].
                        expected_instances: tuple = (
                            "cont1", "cont11", "L1_sixm1000obs", "Linf_520c", "neos3",
                        )) -> bool:
    """
    Periksa apakah setiap kombinasi instance x kondisi sudah mencapai n
    yang direncanakan (default 30 = 15 rep x 2 blok). Kalau BELUM, cetak
    banner peringatan yang jelas agar output ini tidak keliru terbaca
    sebagai hasil final saat baru sebagian blok yang selesai (mis. dicek
    di sela-sela jeda antar blok).

    FIX: versi sebelumnya memakai `df.groupby(["instance","condition"]).size()`
    untuk mencari kombinasi dengan n < expected_n. Bug-nya: groupby HANYA
    mendaftar kombinasi yang BENAR-BENAR MUNCUL di df -- instance yang HILANG
    TOTAL (0 baris, mis. baru 2 dari 5 instance selesai dikumpulkan) tidak
    pernah tampil sebagai baris groupby sama sekali, sehingga tidak pernah
    tertangkap sebagai "n < expected_n" dan lolos sebagai "lengkap". Diverifikasi
    langsung: subset berisi HANYA cont11+neos3 (keduanya n=30 penuh) dilaporkan
    "lengkap" oleh versi lama walau 3 dari 5 instance sama sekali tidak ada.
    Fix: bangun cartesian product instance x kondisi yang DIHARAPKAN secara
    eksplisit, lalu reindex count aktual terhadapnya (isi 0 untuk yang tidak
    ada), baru cek < expected_n -- sehingga kombinasi dengan 0 baris ikut tertangkap.

    Mengembalikan True kalau data lengkap (n=expected_n di semua sel yang
    DIHARAPKAN, termasuk yang belum py=0, DAN kedua blok yang diharapkan hadir),
    False kalau parsial.
    """
    expected_conditions = ("none", "static")
    expected_index = pd.MultiIndex.from_product(
        [expected_instances, expected_conditions], names=["instance", "condition"]
    )
    counts = df.groupby(["instance", "condition"]).size().reindex(expected_index, fill_value=0)
    incomplete = counts[counts < expected_n]

    # Instance yang muncul di data tapi TIDAK ada di expected_instances (mis.
    # typo penamaan, atau instance baru yang belum didaftarkan di sini) --
    # ditandai terpisah supaya tidak diam-diam terlewat dari cek manapun.
    unexpected_instances = sorted(set(df["instance"].unique()) - set(expected_instances))

    blocks_present = sorted(df["block"].unique().tolist())
    missing_blocks = [b for b in expected_blocks if b not in blocks_present]

    is_complete = incomplete.empty and not missing_blocks and not unexpected_instances

    if is_complete:
        return True

    print("#" * 70)
    print("# ⚠️  PERINGATAN: DATA BELUM LENGKAP — HASIL DI BAWAH INI INTERIM")
    print("#" * 70)
    if missing_blocks:
        print(f"#  Blok belum tersedia: {missing_blocks} (tersedia: {blocks_present})")
    if unexpected_instances:
        print(f"#  Instance TAK DIKENALI (di luar {list(expected_instances)}): "
              f"{unexpected_instances} -- cek kemungkinan typo run_id.")
    if not incomplete.empty:
        print("#  Kombinasi instance x kondisi dengan n < "
              f"{expected_n} (target penuh, termasuk yang n=0 / belum mulai sama sekali):")
        for (instance, condition), n in incomplete.items():
            label_cond = "A (none)" if condition == "none" else "B (static)"
            print(f"#    {instance:<20} Kondisi {label_cond:<10} n={n} (dari {expected_n})")
    print("#")
    print("#  Seluruh p-value, MDE, effect size, dan tabel di bawah ini DIHITUNG")
    print("#  DARI DATA YANG TERSEDIA SAAT INI SAJA. Jangan jadikan hasil final")
    print("#  sampai seluruh blok/repetisi yang direncanakan selesai dan skrip")
    print("#  ini dijalankan ulang.")
    print("#" * 70)
    print()
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    parser.add_argument(
        "--throttling-proportional-threshold", type=float, default=0.05,
        help="Ambang proporsi throttled_usec_delta/1e6/crossover_seconds untuk "
             "eksklusi run pada throttling sensitivity check RQ1 (default 0.05 = 5%%, sesuai Rev3)."
    )
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
        # Issue 3: Explicitly report status_code=9 (TIME_LIMIT) runs
        timeout_runs = failed[failed["status_code"] == 9]
        if not timeout_runs.empty:
            print(f"  (Catatan: {len(timeout_runs)} diantaranya adalah run TIME_LIMIT (timeout) — data fase parsial diabaikan)")
        print()
    df = df[df["status_code"] == 2]

    # Banner data-parsial: HARUS di sini, sebelum analisis apa pun, supaya
    # jadi hal pertama yang terlihat kalau skrip ini dijalankan di sela-sela
    # jeda antar blok (mis. sanity check Blok 1 sebelum Blok 2 selesai).
    check_completeness(df)

    # Identifikasi run Kondisi A yang mengalami CFS throttling secara bermakna
    # (proporsional terhadap crossover_seconds — lihat identify_throttled_runs).
    # Tidak dikeluarkan dari analisis utama, hanya untuk sensitivity check.
    flagged_df = identify_throttled_runs(df, proportional_threshold=args.throttling_proportional_threshold)
    if not flagged_df.empty:
        print(f"INFO: {len(flagged_df)} run Kondisi A mengalami CFS throttling bermakna "
              f"(> {args.throttling_proportional_threshold:.0%} dari crossover_seconds) — TETAP masuk "
              f"analisis utama, namun diuji ulang secara terpisah di throttling sensitivity check.")
        print(flagged_df[["run_id", "throttled_usec_delta", "throttle_proportion_of_crossover"]].to_string(index=False))
        print()

    n_missing_cgroup = df["missing_cgroup_snapshot"].sum() if "missing_cgroup_snapshot" in df.columns else 0
    if n_missing_cgroup > 0:
        print(f"INFO: {int(n_missing_cgroup)} run kehilangan snapshot cgroup (cpu.stat tidak terbaca "
              f"sama sekali selama proses berjalan — periksa apakah ini kasus terisolasi atau sistemik). "
              f"Throttled runs pada run tersebut mungkin undercounted.")
        print()

    n_instances = df["instance"].nunique()
    alpha_corrected = ALPHA / n_instances if n_instances > 0 else ALPHA

    # Pemeriksaan efek urutan (Phase 4 — block counterbalancing)
    # Harus dijalankan SEBELUM analisis utama untuk menentukan apakah
    # hasil 30 rep digabung atau dilaporkan per blok.
    check_order_effect(df)

    check_warming_trend(df)

    summarize(df)
    mw_results = run_mann_whitney_with_bonferroni(df)

    # Uji sensitivitas throttling (Phase 5 — sesuai proposal RQ1)
    run_throttling_sensitivity_check(df, mw_results, alpha_corrected,
                                      proportional_threshold=args.throttling_proportional_threshold)

    check_barrier_stability(df)
    run_mannwhitney_context_switches(df)
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
