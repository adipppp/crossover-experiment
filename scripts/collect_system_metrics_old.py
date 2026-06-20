#!/usr/bin/env python3
"""
collect_system_metrics.py — Dijalankan di VM HOST (bukan di dalam Pod/container),
memonitor metrik sistem sebuah proses container Kubernetes selama solve berjalan:
  - involuntary context switches (dari /proc/<pid>/status)
  - CFS throttling statistics (dari cpu.stat cgroup v2 container)

Dipanggil oleh scripts/run_experiment.sh secara paralel (background) dengan
run_solver.py, lalu dihentikan begitu solver selesai.

Asumsi: cgroup v2 (default di Ubuntu 22.04+/kubeadm modern). Jika node memakai
cgroup v1, sesuaikan path di CGROUP_V1_FALLBACK di bawah.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def find_container_pid(pod_name: str, container_name: str, namespace: str) -> int:
    """Mendapatkan PID host dari proses utama container via crictl (lebih portable
    daripada docker inspect, karena bekerja untuk containerd maupun docker shim)."""
    try:
        container_id = subprocess.check_output(
            [
                "sudo", "crictl", "ps",
                "--name", container_name,
                "--pod", pod_name,
                "-o", "json",
            ],
            text=True,
        )
        data = json.loads(container_id)
        cid = data["containers"][0]["id"]
        inspect = subprocess.check_output(["sudo", "crictl", "inspect", cid], text=True)
        inspect_data = json.loads(inspect)
        pid = inspect_data["info"]["pid"]
        return int(pid)
    except (subprocess.CalledProcessError, KeyError, IndexError, json.JSONDecodeError) as e:
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


def main():
    parser = argparse.ArgumentParser(description="Monitor metrik sistem proses container selama solve.")
    parser.add_argument("--pod-name", required=True)
    parser.add_argument("--container-name", default="solver")
    parser.add_argument("--namespace", default="crossover-experiment")
    parser.add_argument("--poll-interval", type=float, default=0.2, help="Detik antar polling status proses")
    parser.add_argument("--output", required=True, help="Path file JSON output")
    args = parser.parse_args()

    pid = find_container_pid(args.pod_name, args.container_name, args.namespace)
    cgroup_path = find_cgroup_path(pid)
    print(f"[collect_system_metrics] Memantau PID host {pid}, cgroup {cgroup_path}")

    snapshot_before_ctxt = read_proc_status_ctxt_switches(pid)
    snapshot_before_cpu = read_cgroup_cpu_stat(cgroup_path)
    t_start = time.time()

    # Polling kontinu (bukan tunggu-lalu-baca-sekali): /proc/<pid>/status akan
    # HILANG begitu proses exit, jadi nilai delta context-switch HARUS diambil
    # dari snapshot TERAKHIR yang berhasil terbaca sebelum PID menghilang —
    # bukan dibaca sesudahnya (yang sudah pasti gagal).
    last_known_ctxt = snapshot_before_ctxt
    while True:
        proc_path = Path(f"/proc/{pid}")
        if not proc_path.exists():
            break
        try:
            last_known_ctxt = read_proc_status_ctxt_switches(pid)
        except FileNotFoundError:
            break  # proses exit tepat di antara check exists() dan read
        time.sleep(args.poll_interval)

    snapshot_after_ctxt = last_known_ctxt
    # cgroup biasanya masih tersedia sesaat setelah proses exit (sebelum container
    # dihapus oleh kubelet), tapi ini RACE CONDITION — baca secepat mungkin.
    snapshot_after_cpu = read_cgroup_cpu_stat(cgroup_path) if cgroup_path.exists() else {}

    t_end = time.time()

    result = {
        "pid_host": pid,
        "cgroup_path": str(cgroup_path),
        "monitoring_duration_seconds": round(t_end - t_start, 3),
        "ctxt_switches_before": snapshot_before_ctxt,
        "ctxt_switches_after_last_known": snapshot_after_ctxt,
        "involuntary_ctxt_switches_delta": (
            snapshot_after_ctxt["involuntary_ctxt_switches"] - snapshot_before_ctxt["involuntary_ctxt_switches"]
            if snapshot_after_ctxt.get("involuntary_ctxt_switches") is not None
            and snapshot_before_ctxt.get("involuntary_ctxt_switches") is not None
            else None
        ),
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
