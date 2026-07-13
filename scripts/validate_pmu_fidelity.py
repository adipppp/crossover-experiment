#!/usr/bin/env python3
"""
validate_pmu_fidelity.py — Validasi fidelitas hardware performance counter (PMU)
pada VM cloud sebelum eksperimen utama dijalankan.

Mengompilasi micro-benchmark (infra/pmu_validation/pmu_bench.c), menjalankan
perf stat pada dua pola akses memori yang kontras, lalu mengevaluasi apakah
nilai PMU yang dilaporkan mencerminkan perbedaan hardware yang sesungguhnya.

KRITERIA GO/NO-GO (sesuai Subbab 'Validasi Fidelitas PMU' dalam proposal):
  GO  : cache-miss rate mode "high" > MIN_RATIO_FACTOR × cache-miss rate mode "low"
        DAN tidak ada counter yang bernilai 0 atau <not supported>.
  NO-GO : salah satu atau kedua kondisi di atas tidak terpenuhi.

Dalam skenario NO-GO, seluruh metrik hardware performance counter dianggap
tidak valid; hanya involuntary context switches yang digunakan sebagai proksi
(lihat Subbab 'Keterbatasan Metodologis').

Penggunaan:
    python3 scripts/validate_pmu_fidelity.py [--repo-dir /path/ke/repo]
    python3 scripts/validate_pmu_fidelity.py --dry-run   # parse ulang tanpa re-run

Output:
    infra/pmu-validation-report.json
    Kode exit: 0 = GO, 1 = NO-GO, 2 = error infrastruktur
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Konstanta
# ─────────────────────────────────────────────────────────────────────────────

# Perf events yang diukur (konsisten dengan 'Prosedur Pengukuran' di proposal)
PERF_EVENTS = [
    "cache-misses",
    "cache-references",
    "L1-dcache-load-misses",
    "L1-dcache-loads",
    "instructions",
    "cycles",
]

# Faktor minimum yang diharapkan antara high-miss rate dan low-miss rate.
# high_miss_rate > MIN_RATIO_FACTOR × low_miss_rate → counter dianggap valid.
# Nilai 5× dipilih konservatif: cache miss rate 64 MB random-access seharusnya
# >90%, sedangkan 16 KB sequential seharusnya <5% → rasio aktual >> 18×.
MIN_RATIO_FACTOR = 5.0

# Batas bawah nilai absolut untuk mencegah false-positive pada miss rate
# yang keduanya sangat kecil (mis. keduanya ~0 jika counter tidak tersedia).
# Threshold 30% (bukan 90%) untuk toleransi variabilitas VM cloud dan
# kemungkinan sebagian akses menghit LLC. Asalkan high > low secara signifikan
# (rasio ≥ 5×), counter dianggap valid.
MIN_HIGH_MISS_RATE = 0.30  # high-miss mode harus menghasilkan miss rate ≥ 30%
MAX_LOW_MISS_RATE  = 0.20  # low-miss mode harus menghasilkan miss rate ≤ 20%

# Jumlah run per mode (dirata-rata untuk mengurangi noise satu run)
N_RUNS = 3

# ─────────────────────────────────────────────────────────────────────────────
# Prerequisit
# ─────────────────────────────────────────────────────────────────────────────

def check_prerequisites():
    """Pastikan gcc dan perf tersedia. Exit 2 jika tidak."""
    errors = []
    for tool in ["gcc", "perf"]:
        if not shutil.which(tool):
            errors.append(tool)
    if errors:
        print(f"ERROR: Tool tidak ditemukan: {', '.join(errors)}", file=sys.stderr)
        if "gcc" in errors:
            print("       Install: sudo apt install gcc", file=sys.stderr)
        if "perf" in errors:
            print("       Install: sudo apt install linux-tools-generic linux-tools-$(uname -r)",
                  file=sys.stderr)
        sys.exit(2)

    # Periksa apakah perf dapat mengakses PMU (bukan hanya tersedia)
    # Gunakan sudo agar konsisten dengan run_perf_stat() — tanpa sudo,
    # mungkin mendapatkan "Permission denied" dari kernel.perf_event_paranoid
    # meskipun PMU sebenarnya bisa diakses dengan elevated privilege.
    r = subprocess.run(
        ["sudo", "perf", "stat", "-e", "cache-misses", "sleep", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if r.returncode != 0 and "<not supported>" not in r.stderr and "Permission" in r.stderr:
        print("ERROR: perf tidak dapat mengakses PMU — mungkin perlu:", file=sys.stderr)
        print("       sudo sysctl kernel.perf_event_paranoid=1", file=sys.stderr)
        print("       Atau jalankan skrip ini dengan sudo.", file=sys.stderr)
        sys.exit(2)


# ─────────────────────────────────────────────────────────────────────────────
# Kompilasi
# ─────────────────────────────────────────────────────────────────────────────

def compile_benchmark(src_path: Path, bin_path: Path):
    """Kompilasi pmu_bench.c → pmu_bench. Sama dengan flag gcc emps.c di repo."""
    print(f">>> Mengompilasi {src_path.name} ...")
    r = subprocess.run(
        ["gcc", "-O2", "-m64", "-o", str(bin_path), str(src_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if r.returncode != 0:
        print(f"ERROR: Kompilasi gagal:\n{r.stderr}", file=sys.stderr)
        sys.exit(2)
    print(f"    → {bin_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Menjalankan perf stat
# ─────────────────────────────────────────────────────────────────────────────

def run_perf_stat(bin_path: Path, mode: str) -> dict:
    """
    Jalankan perf stat pada satu mode ("high" atau "low").
    Kembalikan dict {event_name: value_float | None}.
    None berarti counter tidak tersedia (<not supported>).

    CATATAN: perf stat dijalankan dengan sudo agar level privilege konsisten
    dengan collect_system_metrics.py (yang menggunakan sudo perf stat -p <pid>).
    Ini penting karena kernel.perf_event_paranoid pada VM cloud sering memerlukan
    elevated privilege — tanpa sudo, verdict GO/NO-GO bisa menjadi false NO-GO
    semata karena kurang privilege, padahal PMU sebenarnya bisa diakses dengan benar.
    Pastikan skrip ini dijalankan dari akun yang memiliki sudo tanpa password untuk perf,
    atau jalankan dengan: sudo python3 scripts/validate_pmu_fidelity.py
    """
    cmd = (
        ["sudo", "env", "LC_ALL=C",
         "perf", "stat",
         "-x,",                           # CSV output, delimiter ","
         "-e", ",".join(PERF_EVENTS),
         str(bin_path), mode]
    )
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    r = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )
    # perf stat menulis output ke stderr; stdout berisi output benchmark (kosong)
    return parse_perf_csv(r.stderr)


def parse_perf_csv(perf_stderr: str) -> dict:
    """
    Parse output 'perf stat -x,' dari stderr.

    Format kolom (kernel ≥ 3.14):
        value , unit , event , run_time_ns , pct_time_running , ...
    Nilai <not supported> ditandai sebagai None.
    """
    results = {}
    for line in perf_stderr.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue

        raw_value = parts[0].strip()
        event     = parts[2].strip()

        # Normalkan nama event (hapus ':u', ':k', modifier lain)
        event_clean = re.sub(r":[a-zA-Z]+$", "", event)

        if "<not supported>" in raw_value or "<not counted>" in raw_value:
            results[event_clean] = None
        else:
            try:
                results[event_clean] = float(raw_value)
            except ValueError:
                results[event_clean] = None

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Rata-rata N run
# ─────────────────────────────────────────────────────────────────────────────

def run_n_times(bin_path: Path, mode: str, n: int, label: str) -> dict:
    """
    Jalankan perf stat n kali untuk satu mode. Kembalikan rata-rata per event.
    None jika salah satu run mengembalikan None untuk event tersebut.
    """
    print(f"    Mode '{mode}' ({n} run) ...", end="", flush=True)
    all_runs = []
    for i in range(n):
        result = run_perf_stat(bin_path, mode)
        all_runs.append(result)
        print(".", end="", flush=True)
    print(f" selesai")

    # Kumpulkan semua event yang pernah muncul
    all_events = set()
    for r in all_runs:
        all_events.update(r.keys())

    averages = {}
    for event in all_events:
        values = [r.get(event) for r in all_runs]
        if any(v is None for v in values):
            averages[event] = None   # tidak tersedia di setidaknya satu run
        else:
            averages[event] = sum(values) / len(values)

    return averages


# ─────────────────────────────────────────────────────────────────────────────
# Evaluasi go/no-go
# ─────────────────────────────────────────────────────────────────────────────

def compute_miss_rate(stats: dict, miss_key: str, ref_key: str) -> float | None:
    """Hitung cache-miss rate = miss / references. None jika data tidak tersedia."""
    miss = stats.get(miss_key)
    ref  = stats.get(ref_key)
    if miss is None or ref is None or ref == 0:
        return None
    return miss / ref


def evaluate(high_stats: dict, low_stats: dict) -> tuple[str, list[str], list[str], float | None, float | None, float | None]:
    """
    Evaluasi go/no-go berdasarkan kriteria proposal.

    Returns:
        verdict : "GO" | "NO-GO" | "DEGRADED"
        failures : daftar kriteria yang gagal
        warnings : daftar kondisi mencurigakan (bukan langsung NO-GO)
        high_rate : cache-miss rate mode "high"
        low_rate : cache-miss rate mode "low"
        ratio : rasio high/low
    """
    failures = []
    warnings = []

    # ── 1. Periksa counter yang tidak tersedia ────────────────────────────────
    unsupported_high = [e for e, v in high_stats.items() if v is None]
    unsupported_low  = [e for e, v in low_stats.items()  if v is None]
    unsupported = list(set(unsupported_high + unsupported_low))
    if unsupported:
        failures.append(
            f"Counter tidak tersedia (<not supported>) pada setidaknya satu mode: "
            f"{unsupported}. Hypervisor kemungkinan memvirtualisasikan atau memblokir PMU."
        )

    # ── 2. Periksa counter bernilai nol ──────────────────────────────────────
    zero_events = []
    for key in ["cache-misses", "cache-references", "instructions", "cycles"]:
        if high_stats.get(key) == 0.0 or low_stats.get(key) == 0.0:
            zero_events.append(key)
    if zero_events:
        failures.append(
            f"Counter bernilai 0 pada satu atau kedua mode: {zero_events}. "
            "Ini mengindikasikan PMU tidak berjalan atau tidak diekspos oleh hypervisor."
        )

    # ── 3. Hitung miss rate dan rasio ─────────────────────────────────────────
    high_rate = compute_miss_rate(high_stats, "cache-misses", "cache-references")
    low_rate  = compute_miss_rate(low_stats,  "cache-misses", "cache-references")

    if high_rate is None or low_rate is None:
        failures.append(
            "Tidak dapat menghitung cache-miss rate: data cache-misses atau "
            "cache-references tidak tersedia."
        )
        ratio = None
    else:
        ratio = high_rate / low_rate if low_rate > 0 else None

        if high_rate < MIN_HIGH_MISS_RATE:
            failures.append(
                f"cache-miss rate mode 'high' ({high_rate:.1%}) lebih rendah dari "
                f"ambang minimum ({MIN_HIGH_MISS_RATE:.0%}). Ekspektasi >90% untuk "
                f"akses acak ke 64 MB array. Counter kemungkinan tidak mencerminkan "
                f"hardware yang sesungguhnya."
            )

        if low_rate > MAX_LOW_MISS_RATE:
            warnings.append(
                f"cache-miss rate mode 'low' ({low_rate:.1%}) lebih tinggi dari "
                f"ekspektasi ({MAX_LOW_MISS_RATE:.0%}). Mungkin L1/LLC lebih kecil "
                f"dari asumsi, atau noise VM tinggi. Periksa lscpu --caches."
            )

        if ratio is not None and ratio < MIN_RATIO_FACTOR:
            failures.append(
                f"Rasio miss rate high/low ({ratio:.1f}×) di bawah ambang minimum "
                f"({MIN_RATIO_FACTOR:.0f}×). Counter tidak membedakan dua pola akses "
                f"yang kontras — kemungkinan besar tidak valid."
            )

    # ── 4. Periksa IPC masuk akal ─────────────────────────────────────────────
    for mode_label, stats in [("high", high_stats), ("low", low_stats)]:
        instr  = stats.get("instructions")
        cycles = stats.get("cycles")
        if instr and cycles and cycles > 0:
            ipc = instr / cycles
            if ipc > 8.0 or ipc < 0.05:
                warnings.append(
                    f"IPC mode '{mode_label}' ({ipc:.2f}) di luar rentang masuk akal "
                    f"(0.05–8.0). Mungkin ada masalah dengan pembacaan PMU."
                )

    # ── Verdict ───────────────────────────────────────────────────────────────
    if failures:
        verdict = "NO-GO"
    elif warnings:
        verdict = "DEGRADED"   # valid tapi ada peringatan — dokumentasikan
    else:
        verdict = "GO"

    return verdict, failures, warnings, high_rate, low_rate, ratio


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Validasi fidelitas PMU (hardware performance counter) pada VM."
    )
    parser.add_argument(
        "--repo-dir",
        default=str(Path(__file__).resolve().parent.parent),
        help="Path ke root direktori repo (default: parent dari scripts/)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse ulang laporan yang ada tanpa menjalankan benchmark kembali."
    )
    args = parser.parse_args()

    repo_dir      = Path(args.repo_dir)
    infra_dir     = repo_dir / "infra"
    pmu_dir       = infra_dir / "pmu_validation"
    src_path      = pmu_dir  / "pmu_bench.c"
    bin_path      = pmu_dir  / "pmu_bench"
    report_path   = infra_dir / "pmu-validation-report.json"
    infra_dir.mkdir(exist_ok=True)

    print("=" * 62)
    print("  VALIDASI FIDELITAS HARDWARE PERFORMANCE COUNTER")
    print("  Phase 1 — validate_pmu_fidelity.py")
    print("=" * 62)
    print()

    # ── Dry-run: parse laporan yang ada ──────────────────────────────────────
    if args.dry_run:
        if not report_path.exists():
            print(f"ERROR: Laporan tidak ditemukan: {report_path}", file=sys.stderr)
            sys.exit(2)
        with open(report_path) as f:
            report = json.load(f)
        verdict = report.get("verdict", "UNKNOWN")
        print(f">>> Laporan yang ada: {report_path}")
        print(f">>> Verdict sebelumnya: {verdict}")
        sys.exit(0 if verdict == "GO" else 1)

    # ── Prerequisit ──────────────────────────────────────────────────────────
    check_prerequisites()

    if not src_path.exists():
        print(f"ERROR: Source micro-benchmark tidak ditemukan: {src_path}", file=sys.stderr)
        print(f"       Pastikan 'infra/pmu_validation/pmu_bench.c' ada di repo.",
              file=sys.stderr)
        sys.exit(2)

    # ── Kompilasi ─────────────────────────────────────────────────────────────
    compile_benchmark(src_path, bin_path)
    print()

    # ── Jalankan benchmark ────────────────────────────────────────────────────
    print(f">>> Menjalankan perf stat ({N_RUNS} run per mode) ...")
    high_stats = run_n_times(bin_path, "high", N_RUNS, "high-miss")
    low_stats  = run_n_times(bin_path, "low",  N_RUNS, "low-miss")
    print()

    # ── Evaluasi ──────────────────────────────────────────────────────────────
    verdict, failures, warnings, high_rate, low_rate, ratio = evaluate(
        high_stats, low_stats
    )

    # ── Tampilkan ringkasan ───────────────────────────────────────────────────
    print("─" * 62)
    print("  HASIL PENGUKURAN")
    print("─" * 62)
    print()
    print("  Rata-rata dari", N_RUNS, "run per mode:")
    print()
    header = f"  {'Event':<28} {'HIGH (acak 64MB)':>20} {'LOW (seq 16KB)':>20}"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for event in PERF_EVENTS:
        h = high_stats.get(event)
        l = low_stats.get(event)
        h_str = f"{h:,.0f}" if h is not None else "<not supported>"
        l_str = f"{l:,.0f}" if l is not None else "<not supported>"
        print(f"  {event:<28} {h_str:>20} {l_str:>20}")
    print()

    if high_rate is not None:
        print(f"  Cache-miss rate (high) : {high_rate:.2%}   (ekspektasi: >90% pada bare-metal, threshold kelayakan: >=30% pada VM cloud)")
    if low_rate is not None:
        print(f"  Cache-miss rate (low)  : {low_rate:.2%}   (ekspektasi: <5% pada bare-metal, threshold kelayakan: <=20% pada VM cloud)")
    if ratio is not None:
        print(f"  Rasio high/low         : {ratio:.1f}×       (minimum: {MIN_RATIO_FACTOR:.0f}×)")

    print()
    print("─" * 62)
    verdict_label = {"GO": "✓ GO", "DEGRADED": "~ DEGRADED (GO dengan peringatan)", "NO-GO": "✗ NO-GO"}
    print(f"  VERDICT: {verdict_label.get(verdict, verdict)}")
    print("─" * 62)

    if failures:
        print()
        print("  KEGAGALAN KRITERIA:")
        for f in failures:
            print(f"    ✗ {f}")

    if warnings:
        print()
        print("  PERINGATAN:")
        for w in warnings:
            print(f"    ! {w}")

    print()

    if verdict == "NO-GO":
        print("  IMPLIKASI:")
        print("  Metrik hardware performance counter (cache-miss rate, IPC) TIDAK")
        print("  digunakan dalam analisis utama eksperimen ini.")
        print("  Hanya involuntary context switches yang dilaporkan sebagai proksi")
        print("  migrasi thread, dengan keterbatasan ini didiskusikan secara eksplisit")
        print("  di Subbab 'Keterbatasan Metodologis' (lihat proposal).")
        print()
        print("  Pertimbangkan menjalankan ulang dengan:")
        print("    sudo sysctl kernel.perf_event_paranoid=-1")
        print("    python3 scripts/validate_pmu_fidelity.py")
        print()
    elif verdict == "DEGRADED":
        print("  IMPLIKASI:")
        print("  Counter valid — lanjutkan ke Phase 2. Peringatan di atas didokumentasikan")
        print("  di laporan dan dicatat pada Subbab 'Keterbatasan Metodologis'.")
        print()

    # ── Tulis laporan JSON ────────────────────────────────────────────────────
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict":      verdict,
        "criteria": {
            "min_ratio_factor":    MIN_RATIO_FACTOR,
            "min_high_miss_rate":  MIN_HIGH_MISS_RATE,
            "max_low_miss_rate":   MAX_LOW_MISS_RATE,
            "n_runs_per_mode":     N_RUNS,
        },
        "measurements": {
            "high_mode_avg": high_stats,
            "low_mode_avg":  low_stats,
        },
        "derived": {
            "cache_miss_rate_high": high_rate,
            "cache_miss_rate_low":  low_rate,
            "ratio_high_over_low":  ratio,
        },
        "failures": failures,
        "warnings": warnings,
        "recommendation": (
            "Lanjutkan ke Phase 2. Metrik PMU valid untuk analisis utama."
            if verdict in ("GO", "DEGRADED") else
            "Jangan gunakan metrik PMU dalam analisis utama. "
            "Hanya laporkan involuntary context switches sebagai proksi migrasi thread."
        ),
    }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f">>> Laporan disimpan: {report_path}")
    print()

    # ── STOP jika NO-GO ───────────────────────────────────────────────────────
    if verdict == "NO-GO":
        print("=" * 62)
        print("  !! HENTI — PMU TIDAK VALID !!")
        print("=" * 62)
        print()
        print("  Coba langkah di atas, lalu jalankan ulang.")
        print("  Jika tetap NO-GO, lanjutkan tanpa metrik PMU:")
        print("    Tetapkan keputusan ini secara eksplisit di laporan:")
        print(f"    sudo python3 -c \"")
        print(f"      import json; r=json.load(open('{report_path}'));")
        print(f"      r['pmu_in_analysis']=False;")
        print(f"      json.dump(r, open('{report_path}','w'), indent=2)\"")
        print()
        sys.exit(1)

    print(">>> PMU valid. Lanjutkan ke Phase 2 (SETUP_GUIDE.md §1.3).")
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
