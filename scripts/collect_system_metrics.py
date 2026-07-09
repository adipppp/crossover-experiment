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
    Membaca dan menjumlahkan context switches dari seluruh thread proses (task)
    di bawah /proc/<pid>/task/<tid>/status untuk mendukung workload multi-threaded.
    """
    total_voluntary = 0
    total_involuntary = 0
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
                if voluntary:
                    total_voluntary += int(voluntary.group(1))
                if nonvoluntary:
                    total_involuntary += int(nonvoluntary.group(1))
            except (FileNotFoundError, ProcessLookupError):
                pass  # thread mungkin sudah exited di antara iterasi
                
    return {
        "voluntary_ctxt_switches": total_voluntary,
        "involuntary_ctxt_switches": total_involuntary,
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
    count_at_start = None
    for ts, count in samples:
        if ts <= crossover_start_epoch:
            count_at_start = count
        else:
            break

    # Cari sample TERAKHIR dengan timestamp <= crossover_end_epoch
    count_at_end = None
    for ts, count in samples:
        if ts <= crossover_end_epoch:
            count_at_end = count

    if count_at_start is None or count_at_end is None:
        print(
            "WARNING: Gagal menemukan sampel metrics sebelum/sesudah crossover. Delta tidak presisi.",
            file=sys.stderr,
        )
        return None

    return count_at_end - count_at_start


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

    # Sampling KONTINU (timestamp epoch, involuntary_ctxt_switches) sepanjang
    # siklus hidup proses — bukan cuma before/after — supaya bisa dipotong
    # belakangan agar pas dengan rentang fase crossover saja.
    samples = []
    try:
        snap = read_proc_status_ctxt_switches(pid)
        samples.append((time.time(), snap["involuntary_ctxt_switches"]))
    except (FileNotFoundError, ProcessLookupError):
        pass

    t_start = time.time()
    while True:
        proc_path = Path(f"/proc/{pid}")
        if not proc_path.exists():
            break
        try:
            snap = read_proc_status_ctxt_switches(pid)
            samples.append((time.time(), snap["involuntary_ctxt_switches"]))
        except (FileNotFoundError, ProcessLookupError):
            break  # proses exit tepat di antara check exists() dan read
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

    # cgroup biasanya masih tersedia sesaat setelah proses exit (sebelum container
    # dihapus oleh kubelet), tapi ini RACE CONDITION — baca secepat mungkin.
    cgroup_exists_after = cgroup_path.exists()
    snapshot_after_cpu = read_cgroup_cpu_stat(cgroup_path) if cgroup_exists_after else {}

    # Penyelarasan ke fase crossover: baca file hasil JSON utama dari run_solver.py
    # Karena sekarang hasil ditulis langsung ke hostPath (bukan kubectl cp),
    # file json sudah pasti ada sesaat setelah proses exit. Tunggu sebentar saja
    # jika file system butuh waktu sinkronisasi.
    solver_result_path = Path(args.results_dir) / f"{args.run_id}.json"
    
    deadline = time.time() + 5.0
    while not solver_result_path.exists() and time.time() < deadline:
        time.sleep(0.05)

    crossover_phase_delta = align_samples_to_crossover_phase(samples, solver_result_path)

    whole_process_delta = (samples[-1][1] - samples[0][1]) if len(samples) >= 2 else None

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
        "missing_cgroup_snapshot": not cgroup_exists_after,
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
