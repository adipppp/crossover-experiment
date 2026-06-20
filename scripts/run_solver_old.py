#!/usr/bin/env python3
"""
run_solver.py — Menjalankan satu sesi Gurobi pada satu instance LP benchmark,
mencatat wall-clock time fase barrier dan crossover secara terpisah,
serta menyimpan hasilnya sebagai JSON.

Kredensial WLS WAJIB disuplai lewat environment variable
(GRB_WLSACCESSID, GRB_WLSSECRET, GRB_LICENSEID), TIDAK pernah ditulis
ke file ini atau ke image Docker.

Penting: karena lisensi WLS akademik dibatasi maksimum 2 sesi konkuren,
skrip ini didesain untuk dijalankan satu Pod pada satu waktu (sequential),
sesuai desain eksperimen (lihat scripts/run_experiment.sh).
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import gurobipy as gp
from gurobipy import GRB


def parse_args():
    parser = argparse.ArgumentParser(description="Jalankan satu sesi solve Gurobi dan catat metrik crossover.")
    parser.add_argument("--instance", required=True, help="Path file model LP (.mps/.mps.gz) di dalam container")
    parser.add_argument("--condition", required=True, choices=["none", "static"], help="Label kondisi CPU Manager")
    parser.add_argument("--run-id", required=True, help="Identifier unik untuk run ini, mis. none-run03")
    parser.add_argument("--output-dir", default="/app/results", help="Direktori output JSON hasil")
    parser.add_argument("--threads", type=int, default=0, help="Param Threads Gurobi; 0 = otomatis (pakai semua core yang terlihat container)")
    return parser.parse_args()


def build_env():
    """Membangun GRBEnv dari kredensial WLS di environment variable."""
    required = ["GRB_WLSACCESSID", "GRB_WLSSECRET", "GRB_LICENSEID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"FATAL: environment variable lisensi tidak ditemukan: {missing}", file=sys.stderr)
        sys.exit(1)

    params = {
        "WLSACCESSID": os.environ["GRB_WLSACCESSID"],
        "WLSSECRET": os.environ["GRB_WLSSECRET"],
        "LICENSEID": int(os.environ["GRB_LICENSEID"]),
    }
    env = gp.Env(params=params)
    return env


class PhaseTimingCallback:
    """
    Callback Gurobi untuk menangkap waktu transisi antar fase dengan presisi
    penuh (atribut RUNTIME Gurobi, bukan timestamp log bergranularitas detik).

    Urutan fase pada barrier+crossover: PRESOLVE -> BARRIER (iterasi interior
    point) -> SIMPLEX (ini adalah push phase + cleanup crossover) -> selesai.
    Transisi BARRIER -> SIMPLEX pertama kali = awal crossover.
    """

    def __init__(self):
        self.barrier_first_seen_runtime = None
        self.barrier_last_runtime = None
        self.crossover_first_seen_runtime = None
        self.simplex_last_runtime = None

    def __call__(self, model, where):
        if where == GRB.Callback.BARRIER:
            t = model.cbGet(GRB.Callback.RUNTIME)
            if self.barrier_first_seen_runtime is None:
                self.barrier_first_seen_runtime = t
            self.barrier_last_runtime = t
        elif where == GRB.Callback.SIMPLEX:
            t = model.cbGet(GRB.Callback.RUNTIME)
            if self.crossover_first_seen_runtime is None:
                # Callback SIMPLEX pertama setelah BARRIER pernah terlihat
                # == awal crossover (push phase dimulai di sini).
                self.crossover_first_seen_runtime = t
            self.simplex_last_runtime = t

    def summary(self):
        crossover_seconds = None
        if self.crossover_first_seen_runtime is not None and self.simplex_last_runtime is not None:
            crossover_seconds = round(self.simplex_last_runtime - self.crossover_first_seen_runtime, 6)
        barrier_seconds = None
        if self.barrier_first_seen_runtime is not None and self.barrier_last_runtime is not None:
            barrier_seconds = round(self.barrier_last_runtime - self.barrier_first_seen_runtime, 6)
        return {
            "barrier_start_runtime": self.barrier_first_seen_runtime,
            "barrier_end_runtime": self.barrier_last_runtime,
            "barrier_duration_seconds_callback": barrier_seconds,
            "crossover_start_runtime": self.crossover_first_seen_runtime,
            "crossover_end_runtime": self.simplex_last_runtime,
            "crossover_duration_seconds_callback": crossover_seconds,
        }


def parse_log_for_phase_split(log_path: str):
    """
    Mem-parsing log Gurobi untuk memisahkan waktu fase barrier vs crossover,
    berdasarkan struktur log resmi:
      - Baris terakhir di 'Progress Section' (baris iterasi barrier, format:
        '  NN  <primal> <dual> ... Xs') -> tanda akhir barrier.
      - Baris 'Solved in N iterations and X seconds' -> waktu total solve.
    Crossover time (kasar, granularitas detik) = total_time - barrier_end_time.
    Nilai ini HANYA untuk validasi/cross-check terhadap pengukuran perf_counter
    yang lebih presisi di sekitar model.optimize().
    """
    if not Path(log_path).exists():
        return {"barrier_end_time_log": None, "total_time_log": None, "barrier_iterations": None}

    text = Path(log_path).read_text(errors="ignore")

    # Tangkap baris-baris iterasi barrier: "   12   1.234e+05  ...    2s"
    barrier_iter_pattern = re.compile(r"^\s*(\d+)\*?\s+[-\d.e+]+\s+[-\d.e+]+\s+[-\d.e+]+\s+[-\d.e+]+\s+[-\d.e+]+\s+(\d+(?:\.\d+)?)s\s*$", re.MULTILINE)
    matches = barrier_iter_pattern.findall(text)
    barrier_end_time_log = float(matches[-1][1]) if matches else None
    barrier_iterations = int(matches[-1][0]) if matches else None

    # Tangkap baris akhir: "Solved in 1868 iterations and 1.05 seconds"
    summary_pattern = re.search(r"Solved in (\d+) iterations and ([\d.]+) seconds", text)
    total_time_log = float(summary_pattern.group(2)) if summary_pattern else None

    return {
        "barrier_end_time_log": barrier_end_time_log,
        "total_time_log": total_time_log,
        "barrier_iterations": barrier_iterations,
    }


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log_path = str(output_dir / f"{args.run_id}.log")

    env = build_env()
    env.setParam("LogFile", log_path)
    env.setParam("Method", 2)      # WAJIB: paksa barrier murni (bukan automatic/concurrent).
                                    # Tanpa ini, Gurobi bisa memilih concurrent optimizer yang
                                    # menjalankan simplex paralel terpisah dari barrier, sehingga
                                    # asumsi "callback SIMPLEX pertama = awal crossover" tidak valid.
    env.setParam("Crossover", 4)   # default; aktifkan crossover (bukan 0/disabled)
    if args.threads > 0:
        env.setParam("Threads", args.threads)

    model = gp.read(args.instance, env=env)

    # Catat waktu wall-clock presisi tinggi sebagai sumber utama (bukan log Gurobi
    # yang granularitasnya hanya 1 detik). os.times() / perf_counter dipakai
    # bersamaan supaya bisa dikorelasikan dengan metrik context-switch eksternal
    # yang dikumpulkan oleh collect_system_metrics.py pada PID proses ini.
    pid = os.getpid()
    phase_cb = PhaseTimingCallback()

    t_start = time.perf_counter()
    model.optimize(phase_cb)
    t_end = time.perf_counter()

    wall_clock_total = t_end - t_start
    gurobi_runtime_attr = model.Runtime  # atribut resmi Gurobi, dalam detik

    callback_summary = phase_cb.summary()
    log_derived = parse_log_for_phase_split(log_path)  # cross-check sekunder, granularitas 1 detik

    result = {
        "run_id": args.run_id,
        "condition": args.condition,
        "instance": args.instance,
        "pid_in_container": pid,
        "status_code": model.Status,
        "wall_clock_total_seconds": wall_clock_total,
        "gurobi_runtime_attribute_seconds": gurobi_runtime_attr,
        "barrier_iter_count": getattr(model, "BarIterCount", None),
        "simplex_iter_count": getattr(model, "IterCount", None),
        # Sumber UTAMA pemisahan fase: callback RUNTIME presisi tinggi.
        "phase_timing": callback_summary,
        # Cross-check sekunder dari parsing teks log (granularitas 1 detik saja,
        # dipakai untuk sanity-check, bukan metrik utama dalam analisis).
        "log_derived_crosscheck": log_derived,
        "timestamp_unix": time.time(),
    }

    # Metrik utama crossover time yang dipakai dalam analisis (lihat Metode):
    # diambil dari callback_summary, BUKAN dari log_derived.
    result["crossover_seconds"] = callback_summary["crossover_duration_seconds_callback"]
    result["barrier_seconds"] = callback_summary["barrier_duration_seconds_callback"]

    # Peringatan jika kedua sumber pengukuran berbeda signifikan (>0.5s),
    # supaya anomali terlihat saat inspeksi hasil, bukan terkubur.
    if (
        result["crossover_seconds"] is not None
        and log_derived["total_time_log"] is not None
        and log_derived["barrier_end_time_log"] is not None
    ):
        log_crossover_estimate = log_derived["total_time_log"] - log_derived["barrier_end_time_log"]
        if abs(log_crossover_estimate - result["crossover_seconds"]) > 0.5:
            result["phase_timing_discrepancy_warning"] = (
                f"Selisih besar antara estimasi log ({log_crossover_estimate:.3f}s) "
                f"dan callback ({result['crossover_seconds']:.3f}s) — periksa manual."
            )

    out_path = output_dir / f"{args.run_id}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[run_solver] Selesai. Hasil: {out_path}")
    print(json.dumps(result, indent=2))

    model.dispose()
    env.dispose()


if __name__ == "__main__":
    main()
