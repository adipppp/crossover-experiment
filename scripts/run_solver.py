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
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import gurobipy as gp
from gurobipy import GRB


def get_host_uptime() -> Optional[float]:
    """Membaca status uptime host dari /proc/uptime."""
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.readline().split()[0])
    except Exception:
        return None


def parse_args():
    parser = argparse.ArgumentParser(description="Jalankan satu sesi solve Gurobi dan catat metrik crossover.")
    parser.add_argument("--instance", required=True, help="Path file model LP (.mps/.mps.gz) di dalam container")
    parser.add_argument("--condition", choices=["none", "static"], help="Label kondisi CPU Manager")
    parser.add_argument("--run-id", help="Identifier unik untuk run ini, mis. none-run03")
    parser.add_argument("--output-dir", default="/app/results", help="Direktori output JSON hasil")
    parser.add_argument("--threads", type=int, help="Param Threads Gurobi (wajib bilangan bulat positif)")
    parser.add_argument("--check", action="store_true", help="Validasi lisensi dan instans mps tanpa menjalankan optimize")
    
    args = parser.parse_args()
    
    if not args.check:
        missing = []
        if not args.condition: missing.append("--condition")
        if not args.run_id: missing.append("--run-id")
        if args.threads is None: missing.append("--threads")
        if missing:
            parser.error(f"Argumen berikut wajib diberikan kecuali jika --check digunakan: {', '.join(missing)}")
            
    return args


def build_env(max_retries: int = 3, backoff_seconds: float = 5.0):
    """
    Membangun GRBEnv dari kredensial WLS di environment variable.

    Checkout lisensi WLS dicoba ulang (retry) dengan backoff sederhana, karena
    eksperimen menjalankan puluhan-ratusan run SEKUENSIAL yang masing-masing
    butuh koneksi ke server lisensi Gurobi — kegagalan transient (mis. blip
    jaringan sesaat) seharusnya tidak menggagalkan seluruh run jika dicoba lagi.
    """
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

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            env = gp.Env(params=params)
            return env
        except gp.GurobiError as e:
            last_error = e
            print(
                f"PERINGATAN: gagal checkout lisensi WLS (percobaan {attempt}/{max_retries}): {e}",
                file=sys.stderr,
            )
            if attempt < max_retries:
                time.sleep(backoff_seconds * attempt)  # backoff linear: 5s, 10s, ...

    print(f"FATAL: checkout lisensi WLS gagal setelah {max_retries} percobaan: {last_error}", file=sys.stderr)
    sys.exit(1)


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

    def __call__(self, model, where):
        if where == GRB.Callback.BARRIER:
            try:
                t = model.cbGet(GRB.Callback.RUNTIME)
                if self.barrier_first_seen_runtime is None:
                    self.barrier_first_seen_runtime = t
                self.barrier_last_runtime = t
            except gp.GurobiError as e:
                print(f"PERINGATAN: Gagal memanggil cbGet di BARRIER callback: {e}", file=sys.stderr)
        elif where == GRB.Callback.SIMPLEX:
            if self.crossover_first_seen_runtime is None:
                # Hanya catat waktu di iterasi PERTAMA untuk menghindari overhead Python GIL
                try:
                    self.crossover_first_seen_runtime = model.cbGet(GRB.Callback.RUNTIME)
                except gp.GurobiError as e:
                    print(f"PERINGATAN: Gagal memanggil cbGet di SIMPLEX callback: {e}", file=sys.stderr)

    def summary(self, total_runtime):
        # total_runtime didapat dari model.Runtime setelah optimize() selesai
        crossover_seconds = None
        if self.crossover_first_seen_runtime is not None:
            crossover_seconds = round(total_runtime - self.crossover_first_seen_runtime, 6)
            if crossover_seconds < 0:
                print("PERINGATAN: crossover_seconds bernilai negatif. Diset ke None.", file=sys.stderr)
                crossover_seconds = None

        
        barrier_seconds = None
        if self.barrier_first_seen_runtime is not None and self.barrier_last_runtime is not None:
            # CATATAN: barrier_seconds hanya menghitung durasi iterasi barrier
            # (dari callback BARRIER pertama ke terakhir), bukan seluruh fase barrier
            # karena mengecualikan waktu presolve/startup Gurobi.
            barrier_seconds = round(self.barrier_last_runtime - self.barrier_first_seen_runtime, 6)
            
        return {
            "barrier_start_runtime": self.barrier_first_seen_runtime,
            "barrier_end_runtime": self.barrier_last_runtime,
            "barrier_duration_seconds_callback": barrier_seconds,
            "crossover_start_runtime": self.crossover_first_seen_runtime,
            "crossover_end_runtime": total_runtime,
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
        return {
            "barrier_end_time_log": None,
            "total_time_log": None,
            "barrier_iterations": None,
            "concurrent_crossover_detected": False,
        }

    text = Path(log_path).read_text(errors="ignore")

    # Tangkap baris-baris iterasi barrier secara lebih longgar:
    # iteration number (optional *), values, and ends with Xs
    barrier_iter_pattern = re.compile(r"^\s*(\d+)\*?\s+.*\s+(\d+(?:\.\d+)?)s\s*$", re.MULTILINE)
    matches = barrier_iter_pattern.findall(text)
    barrier_end_time_log = float(matches[-1][1]) if matches else None
    barrier_iterations = int(matches[-1][0]) if matches else None

    # Tangkap baris akhir: "Solved in 1868 iterations and 1.05 seconds"
    summary_pattern = re.search(r"Solved in (\d+) iterations and ([\d.]+) seconds", text)
    total_time_log = float(summary_pattern.group(2)) if summary_pattern else None

    # Validasi crossover tunggal: periksa apakah ada indikator crossover paralel/concurrent
    text_lower = text.lower()
    concurrent_crossover_detected = (
        "concurrent crossover" in text_lower or
        "parallel crossover" in text_lower or
        "concurrent barrier/crossover" in text_lower
    )

    return {
        "barrier_end_time_log": barrier_end_time_log,
        "total_time_log": total_time_log,
        "barrier_iterations": barrier_iterations,
        "concurrent_crossover_detected": concurrent_crossover_detected,
    }


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    out_path = None
    if args.check:
        log_path = ""
    else:
        if "/" in args.run_id or "\\" in args.run_id or ".." in args.run_id:
            raise ValueError("FATAL: run-id contains invalid path traversal characters.")
        log_path = str(output_dir / f"{args.run_id}.log")
        out_path = output_dir / f"{args.run_id}.json"

    env = build_env()
    model = None

    try:
        if not args.check:
            # Hapus file log/json lama secara defensif untuk mencegah appending/polusi
            Path(log_path).unlink(missing_ok=True)
            if out_path:
                out_path.unlink(missing_ok=True)

        env.setParam("LogFile", log_path)
        
        # Atur parameter solver yang wajib (juga divalidasi pada --check)
        env.setParam("Method", 2)       # WAJIB: paksa barrier murni
        env.setParam("Crossover", 4)    # Explicit: paksa push step primal+dual
        
        if not args.check:
            env.setParam("TimeLimit", 1700.0) # Batas waktu pengerjaan solver
            if args.threads <= 0:
                raise ValueError(f"--threads must be a positive integer (got {args.threads}). "
                                 "Gurobi thread auto-detection is a proposal confounder.")
            env.setParam("Threads", args.threads)
        else:
            # Di check mode, jika threads diberikan, validasikan nilainya
            if args.threads is not None and args.threads <= 0:
                raise ValueError(f"--threads must be a positive integer (got {args.threads}).")
            if args.threads is not None:
                env.setParam("Threads", args.threads)

        # Validasi pre-flight terhadap tipe parameter dan batasan nilainya
        try:
            crossover_info = env.getParamInfo("Crossover")
            min_val, max_val = crossover_info[3], crossover_info[4]
            if not (min_val <= 4 <= max_val):
                raise ValueError(f"Nilai 4 tidak didukung oleh parameter Crossover (range: [{min_val}, {max_val}])")
        except gp.GurobiError as e:
            raise ValueError(f"Gagal memvalidasi parameter Crossover: {e}")

        model = gp.read(args.instance, env=env)

        if args.check:
            print(">>> PRE-FLIGHT CHECK BERHASIL: lisensi valid dan model mps berhasil dimuat.")
            return

        pid = os.getpid()
        phase_cb = PhaseTimingCallback()

        # optimize_start_epoch_unix (wall-clock, BUKAN perf_counter) dicatat supaya
        # collect_system_metrics.py — yang berjalan di HOST dan men-timestamp sample
        # context-switch dengan time.time() — bisa mengonversi crossover_start_runtime
        # dan crossover_end_runtime (relatif terhadap awal optimize()) menjadi waktu
        # epoch absolut, lalu memotong sample context-switch persis pada rentang fase
        # crossover saja (bukan seluruh siklus hidup proses). Ini valid karena container
        # berbagi clock kernel yang sama dengan host (runc tanpa virtualisasi clock
        # terpisah seperti gVisor/Kata) — TIDAK ada skew jam antara container dan host.
        optimize_start_epoch_unix = time.time()

        t_start = time.perf_counter()
        model.optimize(phase_cb)
        t_end = time.perf_counter()

        wall_clock_total = t_end - t_start
        gurobi_runtime_attr = model.Runtime  # atribut resmi Gurobi, dalam detik

        callback_summary = phase_cb.summary(model.Runtime)
        log_derived = parse_log_for_phase_split(log_path)  # cross-check sekunder, granularitas 1 detik

        if log_derived.get("concurrent_crossover_detected"):
            print("WARNING: Terdeteksi crossover concurrent/parallel aktif di log Gurobi!", file=sys.stderr)

        result = {
            "run_id": args.run_id,
            "condition": args.condition,
            "instance": args.instance,
            "pid_in_container": pid,
            "status_code": model.Status,
            # wall_clock_total_seconds_DO_NOT_USE_FOR_PHASE_ANALYSIS contains Python process-level wall time.
            # Use callback-derived crossover_seconds / barrier_seconds for actual phase analysis.
            "wall_clock_total_seconds_DO_NOT_USE_FOR_PHASE_ANALYSIS": wall_clock_total,
            "gurobi_runtime_attribute_seconds": gurobi_runtime_attr,
            "barrier_iter_count": getattr(model, "BarIterCount", None),
            "simplex_iter_count": getattr(model, "IterCount", None),
            # Epoch absolut awal optimize() — dipakai collect_system_metrics.py
            # untuk memotong sample context-switch persis pada rentang fase crossover.
            "optimize_start_epoch_unix": optimize_start_epoch_unix,
            # Sumber UTAMA pemisahan fase: callback RUNTIME presisi tinggi.
            "phase_timing": callback_summary,
            # Cross-check sekunder dari parsing teks log (granularitas 1 detik saja,
            # dipakai untuk sanity-check, bukan metrik utama dalam analisis).
            "log_derived_crosscheck": log_derived,
            "timestamp_unix": time.time(),
            "concurrent_crossover_detected": log_derived.get("concurrent_crossover_detected", False),
            # Metadata tambahan (sesuai proposal §II)
            "host_uptime_seconds": get_host_uptime(),
        }

        # Metrik utama crossover time yang dipakai dalam analisis (lihat Metode):
        # diambil dari callback_summary, BUKAN dari log_derived.
        result["crossover_seconds"] = callback_summary["crossover_duration_seconds_callback"]
        result["barrier_iteration_seconds"] = callback_summary["barrier_duration_seconds_callback"]

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

        out_path.write_text(json.dumps(result, indent=2))
        print(f"[run_solver] Selesai. Hasil: {out_path}")
        print(json.dumps(result, indent=2))

    except Exception as e:
        # Tulis JSON kegagalan MINIMAL supaya run ini tetap tercatat (bukan cuma
        # menghilang dari hasil), lalu lempar ulang exception supaya proses exit
        # dengan kode non-zero — Pod akan berstatus Failed (bukan diam-diam
        # Succeeded), sehingga run_experiment.sh dapat mendeteksinya dengan benar.
        if not args.check and out_path is not None:
            failure_result = {
                "run_id": args.run_id,
                "condition": args.condition,
                "instance": args.instance,
                "status_code": None,
                "error": f"{type(e).__name__}: {e}",
                "timestamp_unix": time.time(),
            }
            out_path.write_text(json.dumps(failure_result, indent=2))
        print(f"FATAL: run {args.run_id if not args.check else 'check'} gagal: {e}", file=sys.stderr)
        raise

    finally:
        # WAJIB dijalankan baik solve berhasil, gagal, maupun exception lain —
        # supaya sesi lisensi WLS (maksimum 2 konkuren) tidak pernah menggantung
        # dan menghalangi run berikutnya dalam antrian sekuensial.
        if model is not None:
            model.dispose()
        env.dispose()

        # Synchronize RAM results to host disk to comply with Memory-only I/O constraint
        host_out = Path("/app/host_results")
        if host_out.exists():
            for f in output_dir.glob(f"{args.run_id}.*"):
                shutil.copy2(f, host_out)


if __name__ == "__main__":
    main()
