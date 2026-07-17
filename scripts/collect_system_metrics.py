#!/usr/bin/env python3
"""
collect_system_metrics.py — Dijalankan di VM HOST (bukan di dalam Pod/container),
memonitor metrik sistem sebuah proses container Kubernetes selama solve berjalan:
  - involuntary context switches (dari /proc/<pid>/task/<tid>/status), disampling KONTINU
    sepanjang siklus hidup proses, lalu dipotong agar sesuai rentang waktu fase
    crossover saja (lihat align_samples_to_crossover_phase), bukan whole-process.
  - CFS throttling statistics (dari cpu.stat cgroup v2 container) — tetap
    whole-process, karena tujuannya memverifikasi throttling kuota CPU secara
    umum, bukan spesifik per fase.
  - Hardware performance counters (cache misses, IPC, cycles, instructions) — diukur
    secara whole-process via `perf stat -p <pid>` karena keterbatasan teknis perf stat
    yang tidak mendukung pemotongan/pemberhentian per fase secara andal tanpa overhead.
    
    CATATAN METODOLOGI (Poin 4 Code Review):
    Terdapat kontradiksi internal di proposal Subbab "Prosedur Pengukuran" (HW counters
    diklaim whole-process karena limitasi perf stat, namun di kalimat akhir diklaim
    semua metrik pendukung hanya diukur pada fase crossover). Kode ini mengimplementasikan
    pendekatan whole-process yang lebih rasional secara teknis untuk PMU/HW counters,
    sementara menyelaraskan context switches ke fase crossover saja. Kontradiksi ini
    harus direkonsiliasi pada laporan akhir/skripsi tertulis.

Dipanggil oleh scripts/run_experiment.sh secara paralel (background) dengan
run_solver.py, lalu berhenti otomatis begitu proses container exit.

Asumsi: cgroup v2 (default di Ubuntu 22.04+/kubeadm modern). Jika node memakai
cgroup v1, sesuaikan find_cgroup_path() di bawah.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import signal
from pathlib import Path



def get_host_uptime() -> float | None:
    """Membaca status uptime host dari /proc/uptime."""
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.readline().split()[0])
    except Exception:
        return None


def find_pod_sandbox_id(pod_name: str) -> str:
    """
    Mendapatkan sandbox ID dari nama Pod Kubernetes via `crictl pods --name`.

    PENTING: `crictl ps --pod <X>` mengharapkan SANDBOX ID, BUKAN nama Pod
    Kubernetes — ini bug yang pernah ada di versi skrip sebelumnya (memberi
    nama Pod langsung ke `crictl ps --pod`, yang selalu menghasilkan kosong).
    Pencarian harus dua langkah: cari sandbox ID dulu via `crictl pods --name`,
    baru filter container dengan sandbox ID itu via `crictl ps --pod`.
    """
    try:
        output = subprocess.check_output(
            ["sudo", "crictl", "pods", "--name", pod_name, "-o", "json"],
            text=True,
        )
        data = json.loads(output)
        items = data.get("items", [])
        if not items:
            raise LookupError(f"Tidak ada pod sandbox dengan nama '{pod_name}' ditemukan via crictl pods.")
        
        # Filter sandbox yang berstatus READY terlebih dahulu untuk menghindari instans lama yang mati
        ready_items = [item for item in items if item.get("state") == "SANDBOX_READY"]
        if ready_items:
            return ready_items[0]["id"]
            
        # Fallback ke item pertama jika tidak ada yang READY (misal sudah keburu exit)
        return items[0]["id"]
    except (subprocess.CalledProcessError, KeyError, IndexError, json.JSONDecodeError, LookupError) as e:
        print(f"FATAL: gagal menemukan sandbox ID untuk pod '{pod_name}': {e}", file=sys.stderr)
        sys.exit(1)


def find_container_pid(pod_name: str, container_name: str) -> int:
    """Mendapatkan PID host dari proses utama container, via dua langkah crictl:
    1) crictl pods --name <pod_name>      -> sandbox ID
    2) crictl ps --pod <sandbox_id>       -> container ID
    3) crictl inspect <container_id>      -> PID host
    """
    sandbox_id = find_pod_sandbox_id(pod_name)

    try:
        output = subprocess.check_output(
            [
                "sudo", "crictl", "ps",
                "--name", container_name,
                "--pod", sandbox_id,
                "-o", "json",
            ],
            text=True,
        )
        data = json.loads(output)
        containers = data.get("containers", [])
        if not containers:
            raise LookupError(
                f"Tidak ada container '{container_name}' ditemukan di sandbox {sandbox_id} "
                f"(pod '{pod_name}')."
            )
        cid = containers[0]["id"]

        inspect = subprocess.check_output(["sudo", "crictl", "inspect", cid], text=True)
        inspect_data = json.loads(inspect)
        pid = inspect_data["info"]["pid"]
        return int(pid)
    except (subprocess.CalledProcessError, KeyError, IndexError, json.JSONDecodeError, LookupError) as e:
        print(f"FATAL: gagal menemukan PID container: {e}", file=sys.stderr)
        sys.exit(1)


def find_cgroup_path(pid: int) -> Path:
    """Menemukan path cgroup v2 dari PID, lewat /proc/<pid>/cgroup."""
    cgroup_file = Path(f"/proc/{pid}/cgroup")
    content = cgroup_file.read_text().strip()
    # Format cgroup v2 unified: "0::/kubepods.slice/.../cri-containerd-<id>.scope"
    # Issue 7: Robust search for 0:: prefix instead of assuming last line
    line = next((l for l in content.splitlines() if l.startswith("0::")), content.splitlines()[-1])
    rel_path = line.split(":")[-1]
    full_path = Path("/sys/fs/cgroup") / rel_path.lstrip("/")
    if not full_path.exists():
        raise FileNotFoundError(
            f"Path cgroup tidak ditemukan: {full_path}. "
            f"Kemungkinan node memakai cgroup v1 — perlu penyesuaian skrip."
        )
    return full_path


def read_proc_status_ctxt_switches(pid: int) -> dict:
    """
    Membaca context switches PER-TID (bukan cuma total) dari seluruh thread
    proses (task) di bawah /proc/<pid>/task/<tid>/status.

    Breakdown per-TID diperlukan karena Gurobi dapat membuat/menghancurkan
    thread secara dinamis antar fase (mis. barrier vs crossover memakai pola
    paralelisme berbeda). Menjumlahkan total across-TID pada dua titik waktu
    yang berbeda TIDAK VALID jika populasi TID di kedua titik itu berbeda —
    delta yang dihasilkan bisa negatif atau tidak bermakna secara matematis
    (lihat compute_identity_aware_delta). Fungsi ini mengembalikan breakdown
    agar identitas TID dapat dilacak antar sampling.
    """
    per_tid_involuntary = {}
    per_tid_voluntary = {}
    task_dir = Path(f"/proc/{pid}/task")
    if not task_dir.exists():
        raise FileNotFoundError(f"Direktori task untuk PID {pid} tidak ditemukan.")

    for tid_dir in task_dir.iterdir():
        status_path = tid_dir / "status"
        if status_path.exists():
            try:
                text = status_path.read_text()
                voluntary = re.search(r"voluntary_ctxt_switches:\s+(\d+)", text)
                nonvoluntary = re.search(r"nonvoluntary_ctxt_switches:\s+(\d+)", text)
                tid = tid_dir.name
                if voluntary:
                    per_tid_voluntary[tid] = int(voluntary.group(1))
                if nonvoluntary:
                    per_tid_involuntary[tid] = int(nonvoluntary.group(1))
            except (FileNotFoundError, ProcessLookupError):
                pass  # thread mungkin sudah exited di antara iterasi

    return {
        "voluntary_ctxt_switches": sum(per_tid_voluntary.values()),
        "involuntary_ctxt_switches": sum(per_tid_involuntary.values()),
        # Breakdown per-TID — dipakai oleh compute_identity_aware_delta untuk
        # menghitung delta yang identity-aware, bukan sekadar total-vs-total.
        "per_tid_involuntary": per_tid_involuntary,
    }


def compute_identity_aware_delta(sample_start: dict, sample_end: dict) -> dict:
    """
    Menghitung delta involuntary context switches antara dua snapshot per-TID,
    dengan penanganan eksplisit untuk TID yang muncul atau hilang di antara
    kedua titik waktu tersebut — BUKAN menjumlahkan total across-TID pada tiap
    titik lalu mengurangkannya (pendekatan lama yang salah jika populasi TID
    berbeda; lihat catatan pada read_proc_status_ctxt_switches).

    Aturan penanganan (skema kombinasi — lihat diskusi desain):
    - TID yang ADA di kedua titik: delta = count_end[tid] - count_start[tid].
      Jika hasilnya negatif (kernel counter tidak seharusnya turun untuk TID
      yang sama), diklem ke 0 dan dicatat sebagai anomali — kemungkinan besar
      TID di-reuse oleh kernel untuk thread yang berbeda di antara sampling.
    - TID yang HANYA ADA di titik akhir (thread baru, spawn di tengah window):
      seluruh count_end[tid] dihitung sebagai kontribusinya, karena kita tidak
      punya baseline sebelum thread ini muncul — ini adalah lower-bound yang
      valid untuk aktivitas thread tersebut sejak ia mulai teramati.
    - TID yang HANYA ADA di titik awal (thread mati sebelum window berakhir):
      TIDAK dihitung (kontribusi setelah titik awal tidak diketahui, dan static
      count_start[tid] itu sendiri sudah bagian dari delta sebelumnya, bukan
      delta window ini). Diikutkan di flag diagnostik agar terlihat di data,
      bukan didiamkan.

    Mengembalikan dict berisi delta serta metadata churn untuk pelaporan.
    """
    tids_start = set(sample_start.keys())
    tids_end = set(sample_end.keys())

    tids_common = tids_start & tids_end
    tids_appeared = tids_end - tids_start
    tids_disappeared = tids_start - tids_end

    total_delta = 0
    anomalous_negative_tids = []

    for tid in tids_common:
        d = sample_end[tid] - sample_start[tid]
        if d < 0:
            anomalous_negative_tids.append(tid)
            d = 0  # klem ke 0, kemungkinan TID di-reuse kernel untuk thread lain
        total_delta += d

    for tid in tids_appeared:
        total_delta += sample_end[tid]

    return {
        "delta": total_delta,
        "thread_churn_detected": bool(tids_appeared or tids_disappeared),
        "tids_appeared_mid_window": len(tids_appeared),
        "tids_disappeared_mid_window": len(tids_disappeared),
        "tids_anomalous_negative": len(anomalous_negative_tids),
    }


def read_cgroup_cpu_stat(cgroup_path: Path) -> dict:
    cpu_stat_path = cgroup_path / "cpu.stat"
    if not cpu_stat_path.exists():
        return {}
    text = cpu_stat_path.read_text()
    stats = {}
    for line in text.strip().splitlines():
        key, value = line.split()
        stats[key] = int(value)
    return stats


def align_samples_to_crossover_phase(samples, solver_result_path: Path):
    """
    Memotong time-series sample involuntary_ctxt_switches agar hanya mencakup
    rentang waktu fase crossover saja — BUKAN seluruh siklus hidup proses.

    `samples` berisi (timestamp, per_tid_dict) — bukan lagi (timestamp, total)
    — sehingga delta dihitung via compute_identity_aware_delta, yang menangani
    TID yang muncul/hilang di dalam window secara eksplisit alih-alih
    menjumlahkan total across-TID pada dua titik waktu berbeda (pendekatan
    lama, tidak valid jika populasi TID berubah — lihat catatan pada
    read_proc_status_ctxt_switches dan compute_identity_aware_delta).
    """
    if not solver_result_path.exists():
        print(
            f"PERINGATAN: file hasil solver {solver_result_path} tidak ditemukan — "
            f"tidak bisa menghitung delta context-switch khusus fase crossover.",
            file=sys.stderr,
        )
        return None

    try:
        solver_result = json.loads(solver_result_path.read_text())
    except json.JSONDecodeError:
        print(f"PERINGATAN: gagal parse {solver_result_path}.", file=sys.stderr)
        return None

    optimize_start_epoch = solver_result.get("optimize_start_epoch_unix")
    phase_timing = solver_result.get("phase_timing") or {}
    crossover_start_runtime = phase_timing.get("crossover_start_runtime")
    crossover_end_runtime = phase_timing.get("crossover_end_runtime")

    if optimize_start_epoch is None or crossover_start_runtime is None or crossover_end_runtime is None:
        print(
            "PERINGATAN: phase_timing tidak lengkap pada hasil solver (kemungkinan "
            "crossover tidak terjadi atau run gagal) — delta fase crossover = None.",
            file=sys.stderr,
        )
        return None

    crossover_start_epoch = optimize_start_epoch + crossover_start_runtime
    crossover_end_epoch = optimize_start_epoch + crossover_end_runtime

    if not samples:
        return None

    # Urutkan secara eksplisit agar aman dari segala bug log sampling
    samples.sort(key=lambda x: x[0])

    # Cari sample TERAKHIR dengan timestamp <= crossover_start_epoch
    idx_start = None
    for i, (ts, _) in enumerate(samples):
        if ts <= crossover_start_epoch:
            idx_start = i
        else:
            break

    # Cari sample TERAKHIR dengan timestamp <= crossover_end_epoch
    idx_end = None
    for i, (ts, _) in enumerate(samples):
        if ts <= crossover_end_epoch:
            idx_end = i

    if idx_start is None or idx_end is None:
        print(
            "WARNING: Gagal menemukan sampel metrics sebelum/sesudah crossover. Delta tidak presisi.",
            file=sys.stderr,
        )
        return None

    # Jika idx_start == idx_end, crossover fase berjalan sangat cepat (< interval polling)
    # sehingga snapshot_start dan snapshot_end diambil dari index sampel yang sama.
    crossover_too_short_warning = (idx_start == idx_end)

    snapshot_start = samples[idx_start][1]
    snapshot_end = samples[idx_end][1]

    delta_info = compute_identity_aware_delta(snapshot_start, snapshot_end)
    delta_info["crossover_too_short_warning"] = crossover_too_short_warning
    return delta_info


def parse_perf_stat(log_path: Path) -> dict:
    """
    Mem-parse hasil output dari file log mentah sudo perf stat.
    Format yang diharapkan adalah human-readable (tanpa flag -x,), berbeda
    dengan validate_pmu_fidelity.py yang menggunakan format CSV.
    """
    if not log_path.exists():
        return {}
    text = log_path.read_text()
    res = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.search(r"^([\d,.]+)\s+([a-zA-Z0-9_-]+(?::[a-zA-Z]+)?)", line)
        if match:
            val_str = match.group(1).replace(",", "")
            event = match.group(2)
            event = re.sub(r":[a-zA-Z]+$", "", event)
            try:
                if "." in val_str:
                    res[event] = float(val_str)
                else:
                    res[event] = int(val_str)
            except ValueError:
                pass
    return res


def main():
    parser = argparse.ArgumentParser(description="Monitor metrik sistem proses container selama solve.")
    parser.add_argument("--pod-name", required=True)
    parser.add_argument("--container-name", default="solver")
    parser.add_argument("--run-id", required=True, help="Run ID, untuk mencari file hasil JSON utama dari run_solver.py")
    parser.add_argument("--results-dir", required=True, help="Direktori hasil di HOST (sama persis dgn isi /app/results dari sudut pandang container, lewat hostPath)")
    parser.add_argument(
        "--poll-interval", type=float, default=0.05,
        help="Detik antar polling status proses (default 50ms — lebih halus dari versi sebelumnya "
             "agar resolusi penyelarasan ke fase crossover cukup, mengingat crossover bisa sub-detik)",
    )
    parser.add_argument("--output", required=True, help="Path file JSON output")
    args = parser.parse_args()

    pid = find_container_pid(args.pod_name, args.container_name)
    cgroup_path = find_cgroup_path(pid)
    print(f"[collect_system_metrics] Memantau PID host {pid}, cgroup {cgroup_path}")

    # Launch background perf stat targeting the host process
    perf_log = Path(args.output).with_suffix(".perf.txt")
    perf_cmd = [
        "sudo", "env", "LC_ALL=C", "perf", "stat",
        "-p", str(pid),
        "-e", "cache-misses,cache-references,L1-dcache-load-misses,L1-dcache-loads,instructions,cycles"
    ]
    perf_proc = None
    perf_log_file = None
    try:
        perf_log_file = open(perf_log, "w")
        perf_proc = subprocess.Popen(perf_cmd, stderr=perf_log_file, stdout=subprocess.DEVNULL)
    except Exception as e:
        print(f"WARNING: Gagal menjalankan perf stat: {e}", file=sys.stderr)
        if perf_log_file is not None:
            perf_log_file.close()
            perf_log_file = None

    snapshot_before_cpu = read_cgroup_cpu_stat(cgroup_path)

    # Sampling KONTINU (timestamp epoch, snapshot per-TID) sepanjang siklus
    # hidup proses — bukan cuma before/after — supaya bisa dipotong belakangan
    # agar pas dengan rentang fase crossover saja. Menyimpan breakdown per-TID
    # (bukan total across-TID) agar delta dapat dihitung secara identity-aware
    # via compute_identity_aware_delta, yang menangani thread yang spawn/exit
    # di tengah window secara eksplisit.
    samples = []
    try:
        snap = read_proc_status_ctxt_switches(pid)
        samples.append((time.time(), snap["per_tid_involuntary"]))
    except (FileNotFoundError, ProcessLookupError):
        pass

    # Snapshot cpu.stat "after" TIDAK dibaca pasca-exit (race condition — cgroup
    # kerap sudah dihapus containerd saat itu, restartPolicy: Never mempercepat
    # cleanup). Sebagai gantinya, snapshot terakhir yang berhasil dibaca SELAGI
    # proses masih hidup disimpan di sini pada tiap iterasi loop polling, lalu
    # dipakai sebagai pengganti "after" begitu loop berhenti. Trade-off: snapshot
    # bisa seusia maksimal satu poll_interval dari waktu exit sebenarnya —
    # ini disengaja dan lebih baik daripada gagal 100%.
    last_known_cpu_stat = read_cgroup_cpu_stat(cgroup_path)

    t_start = time.time()
    while True:
        proc_path = Path(f"/proc/{pid}")
        if not proc_path.exists():
            break
        try:
            snap = read_proc_status_ctxt_switches(pid)
            samples.append((time.time(), snap["per_tid_involuntary"]))
        except (FileNotFoundError, ProcessLookupError):
            break  # proses exit tepat di antara check exists() dan read

        # Perbarui snapshot cpu.stat SELAGI proses (dan cgroup-nya) dijamin
        # masih hidup. Dibungkus try/except terpisah dari sampling context
        # switch di atas: kegagalan baca cgroup di satu iterasi tidak boleh
        # menghentikan loop context-switch sampling, yang punya masalah
        # ketepatan waktu berbeda dan lebih kritis untuk RQ2.
        try:
            fresh_cpu_stat = read_cgroup_cpu_stat(cgroup_path)
            if fresh_cpu_stat:
                last_known_cpu_stat = fresh_cpu_stat
        except Exception:
            pass  # pertahankan last_known_cpu_stat sebelumnya, jangan overwrite dengan {}

        time.sleep(args.poll_interval)
    t_end = time.time()

    # Terminate perf process if it was started
    # Issue 8: Add fallback warning when SIGINT times out and perf stat is forcefully killed
    if perf_proc is not None:
        try:
            perf_proc.send_signal(signal.SIGINT)
            perf_proc.wait(timeout=10)
        except Exception as e:
            print(f"WARNING: perf stat did not exit cleanly (SIGINT timed out/failed): {e}. Force killing. PMU metrics may be missing.", file=sys.stderr)
            perf_proc.kill()
    if perf_log_file is not None:
        perf_log_file.close()

    # FIX race condition: TIDAK membaca cpu.stat pasca-exit (lihat catatan di
    # atas loop). snapshot_after_cpu memakai nilai terakhir yang berhasil
    # dibaca SELAGI proses masih hidup, dari dalam loop polling.
    snapshot_after_cpu = last_known_cpu_stat
    cgroup_exists_after = cgroup_path.exists()  # tetap dicatat sbg metadata info,
                                                 # TIDAK lagi menentukan validitas snapshot

    # Penyelarasan ke fase crossover: baca file hasil JSON utama dari run_solver.py
    # Karena sekarang hasil ditulis langsung ke hostPath (bukan kubectl cp),
    # file json sudah pasti ada sesaat setelah proses exit. Tunggu sebentar saja
    # jika file system butuh waktu sinkronisasi.
    solver_result_path = Path(args.results_dir) / f"{args.run_id}.json"
    
    deadline = time.time() + 5.0
    while not solver_result_path.exists() and time.time() < deadline:
        time.sleep(0.05)

    align_info = align_samples_to_crossover_phase(samples, solver_result_path)
    crossover_phase_delta = None
    crossover_too_short_warning = False
    crossover_thread_churn_detected = None
    crossover_tids_appeared_mid_window = None
    crossover_tids_disappeared_mid_window = None
    crossover_tids_anomalous_negative = None
    if align_info is not None:
        crossover_phase_delta = align_info["delta"]
        crossover_too_short_warning = align_info["crossover_too_short_warning"]
        crossover_thread_churn_detected = align_info["thread_churn_detected"]
        crossover_tids_appeared_mid_window = align_info["tids_appeared_mid_window"]
        crossover_tids_disappeared_mid_window = align_info["tids_disappeared_mid_window"]
        crossover_tids_anomalous_negative = align_info["tids_anomalous_negative"]

    # whole_process_delta juga dihitung identity-aware (konsisten dengan fix
    # pada crossover_phase_delta) — sebelumnya memakai total across-TID pada
    # dua titik waktu, yang tidak valid jika populasi TID berubah sepanjang
    # eksekusi (lihat catatan pada compute_identity_aware_delta).
    whole_process_info = (
        compute_identity_aware_delta(samples[0][1], samples[-1][1])
        if len(samples) >= 2 else None
    )
    whole_process_delta = whole_process_info["delta"] if whole_process_info else None

    # Parse raw perf stats
    perf_metrics = parse_perf_stat(perf_log)

    # Calculate Cache Miss Rate
    cache_misses = perf_metrics.get("cache-misses", 0)
    cache_references = perf_metrics.get("cache-references", 0)
    cache_miss_rate = (cache_misses / cache_references) if cache_references > 0 else None

    # Calculate IPC (Instructions Per Cycle) — proksi efisiensi eksekusi per siklus.
    # Digunakan bersama cache_miss_rate sebagai triangulasi mekanisme pada RQ2:
    # IPC relatif tetap → CPU mendapat alokasi waktu lebih konsisten (tanpa stall memori);
    # IPC meningkat pada static → efisiensi per siklus naik akibat berkurangnya cache miss.
    # (Subbab "Prosedur Pengukuran" dan "Analisis Data" RQ2 dalam proposal.)
    _instr  = perf_metrics.get("instructions")
    _cycles = perf_metrics.get("cycles")
    ipc = (_instr / _cycles) if (_instr and _cycles and _cycles > 0) else None

    result = {
        "pid_host": pid,
        "cgroup_path": str(cgroup_path),
        "monitoring_duration_seconds": round(t_end - t_start, 3),
        "ctxt_switch_samples_count": len(samples),
        "poll_interval_seconds": args.poll_interval,
        # Metrik UTAMA untuk RQ2: delta context-switch DI DALAM rentang waktu
        # fase crossover saja, diselaraskan via optimize_start_epoch_unix +
        # phase_timing dari run_solver.py. Gunakan field ini di analisis,
        # BUKAN involuntary_ctxt_switches_delta_whole_process.
        "involuntary_ctxt_switches_delta_crossover_phase_only": crossover_phase_delta,
        "crossover_too_short_warning": crossover_too_short_warning,
        # Field diagnostik BARU — mendeteksi thread churn (spawn/exit) selama
        # fase crossover, yang bisa memengaruhi keandalan delta di atas.
        # Diberi nilai per run agar analisis dapat menjalankan sensitivity
        # check (laporkan semua data + laporkan subset "bersih" tanpa churn
        # secara terpisah) — pola yang sama seperti throttling sensitivity
        # check untuk RQ1. Nilai None berarti alignment gagal total (mis.
        # phase_timing tidak lengkap), bukan berarti tidak ada churn.
        "crossover_thread_churn_detected": crossover_thread_churn_detected,
        "crossover_tids_appeared_mid_window": crossover_tids_appeared_mid_window,
        "crossover_tids_disappeared_mid_window": crossover_tids_disappeared_mid_window,
        "crossover_tids_anomalous_negative_count": crossover_tids_anomalous_negative,
        # Disimpan sebagai pembanding/konteks saja — TIDAK dipakai untuk RQ2
        # karena mencampur aktivitas presolve+barrier+crossover sekaligus.
        "involuntary_ctxt_switches_delta_whole_process": whole_process_delta,
        "cpu_stat_before": snapshot_before_cpu,
        "cpu_stat_after": snapshot_after_cpu,
        "throttled_periods_delta": (
            snapshot_after_cpu.get("nr_throttled", 0) - snapshot_before_cpu.get("nr_throttled", 0)
            if snapshot_after_cpu and snapshot_before_cpu else None
        ),
        "throttled_usec_delta": (
            snapshot_after_cpu.get("throttled_usec", 0) - snapshot_before_cpu.get("throttled_usec", 0)
            if snapshot_after_cpu and snapshot_before_cpu else None
        ),
        # Dengan fix race condition, snapshot_after_cpu diambil SELAGI proses
        # masih hidup (dari loop polling), bukan pasca-exit. Field ini sekarang
        # mencerminkan apakah snapshot valid berhasil didapat sama sekali
        # (mis. proses exit sebelum loop sempat iterasi & sebelum seed awal
        # berhasil membaca cgroup), bukan lagi soal cgroup exist pasca-exit.
        "missing_cgroup_snapshot": not bool(snapshot_after_cpu),
        "cgroup_existed_at_exit_check": cgroup_exists_after,
        # Hardware performance counters (triangulasi proksi RQ2)
        "perf_metrics": perf_metrics,
        "cache_miss_rate": cache_miss_rate,
        "ipc": ipc,
        # Metadata tambahan (sesuai proposal §II)
        "host_uptime_seconds": get_host_uptime(),
    }

    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
