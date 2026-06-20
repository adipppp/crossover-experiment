#!/usr/bin/env python3
"""
collect_system_metrics.py — Dijalankan di VM HOST (bukan di dalam Pod/container),
memonitor metrik sistem sebuah proses container Kubernetes selama solve berjalan:
  - involuntary context switches (dari /proc/<pid>/status), disampling KONTINU
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
from pathlib import Path


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
    line = content.splitlines()[-1]
    rel_path = line.split(":")[-1]
    full_path = Path("/sys/fs/cgroup") / rel_path.lstrip("/")
    if not full_path.exists():
        raise FileNotFoundError(
            f"Path cgroup tidak ditemukan: {full_path}. "
            f"Kemungkinan node memakai cgroup v1 — perlu penyesuaian skrip."
        )
    return full_path


def read_proc_status_ctxt_switches(pid: int) -> dict:
    status_path = Path(f"/proc/{pid}/status")
    text = status_path.read_text()
    voluntary = re.search(r"voluntary_ctxt_switches:\s+(\d+)", text)
    nonvoluntary = re.search(r"nonvoluntary_ctxt_switches:\s+(\d+)", text)
    return {
        "voluntary_ctxt_switches": int(voluntary.group(1)) if voluntary else None,
        "involuntary_ctxt_switches": int(nonvoluntary.group(1)) if nonvoluntary else None,
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
    rentang waktu fase crossover saja — BUKAN seluruh siklus hidup proses
    (startup, presolve, barrier, dst).

    Cara kerja: run_solver.py mencatat `optimize_start_epoch_unix` (wall-clock
    epoch tepat sebelum model.optimize() dipanggil) beserta `phase_timing`
    (crossover_start_runtime, crossover_end_runtime — relatif terhadap awal
    optimize(), dari callback Gurobi RUNTIME). Karena container dan host
    berbagi clock kernel yang sama (runc, tanpa virtualisasi clock terpisah),
    crossover_start_epoch = optimize_start_epoch_unix + crossover_start_runtime
    dapat dipakai langsung untuk memotong sample yang ditimestamp time.time()
    di skrip ini.

    Mengembalikan delta involuntary_ctxt_switches HANYA dalam rentang fase
    crossover, atau None jika data pendukung tidak tersedia.
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

    # Cari sample TERAKHIR dengan timestamp <= crossover_start_epoch (nilai
    # counter pada/sesaat sebelum crossover dimulai).
    count_at_start = None
    for ts, count in samples:
        if ts <= crossover_start_epoch:
            count_at_start = count
        else:
            break

    # Cari sample TERAKHIR dengan timestamp <= crossover_end_epoch (nilai
    # counter pada/sesaat sebelum crossover berakhir).
    count_at_end = None
    for ts, count in samples:
        if ts <= crossover_end_epoch:
            count_at_end = count

    if count_at_start is None or count_at_end is None:
        return None

    return count_at_end - count_at_start


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

    snapshot_before_cpu = read_cgroup_cpu_stat(cgroup_path)

    # Sampling KONTINU (timestamp epoch, involuntary_ctxt_switches) sepanjang
    # siklus hidup proses — bukan cuma before/after — supaya bisa dipotong
    # belakangan agar pas dengan rentang fase crossover saja.
    samples = []
    try:
        snap = read_proc_status_ctxt_switches(pid)
        samples.append((time.time(), snap["involuntary_ctxt_switches"]))
    except FileNotFoundError:
        pass

    t_start = time.time()
    while True:
        proc_path = Path(f"/proc/{pid}")
        if not proc_path.exists():
            break
        try:
            snap = read_proc_status_ctxt_switches(pid)
            samples.append((time.time(), snap["involuntary_ctxt_switches"]))
        except FileNotFoundError:
            break  # proses exit tepat di antara check exists() dan read
        time.sleep(args.poll_interval)
    t_end = time.time()

    # cgroup biasanya masih tersedia sesaat setelah proses exit (sebelum container
    # dihapus oleh kubelet), tapi ini RACE CONDITION — baca secepat mungkin.
    snapshot_after_cpu = read_cgroup_cpu_stat(cgroup_path) if cgroup_path.exists() else {}

    # Penyelarasan ke fase crossover: baca file hasil JSON utama dari run_solver.py
    # (sudah pasti tertulis di titik ini, karena ditulis SEBELUM proses exit).
    solver_result_path = Path(args.results_dir) / f"{args.run_id}.json"
    crossover_phase_delta = align_samples_to_crossover_phase(samples, solver_result_path)

    whole_process_delta = (samples[-1][1] - samples[0][1]) if len(samples) >= 2 else None

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
    }

    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
