#!/usr/bin/env python3
"""
characterize_topology.py — Karakterisasi topologi hardware VM untuk eksperimen
CPU pinning x Crossover LP.

Jalankan di dalam VM SETELAH VM berhasil dibuat dan SEBELUM setup Kubernetes.
Skrip ini membaca lscpu dan numactl, menganalisis sibling pairs, mengevaluasi
kelayakan Option 1/2/3, lalu menulis laporan ke infra/topology-report.json.

PENTING: Skrip ini TIDAK mengambil keputusan otomatis. Setelah membaca output,
tetapkan keputusan Anda secara manual:
    echo "option1" > infra/topology-decision.txt

lalu lanjutkan ke Phase 2/3 (render kubelet configs).

Penggunaan:
    python3 scripts/characterize_topology.py [--repo-dir /path/ke/repo]
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Konstanta desain eksperimen (sesuai Subbab "Infrastruktur Eksperimen")
# ─────────────────────────────────────────────────────────────────────────────

SOLVER_THREADS = 4  # resources.requests.cpu = resources.limits.cpu = 4 (Guaranteed QoS)
RESERVED_CPUS  = 1  # reservedSystemCPUs: satu core untuk daemon Kubernetes dan sistem
CPUS_NEEDED    = SOLVER_THREADS + RESERVED_CPUS  # = 5

# ─────────────────────────────────────────────────────────────────────────────
# Utilitas
# ─────────────────────────────────────────────────────────────────────────────

def run(cmd, check=True):
    """Jalankan perintah shell; kembalikan stdout. Exit jika gagal."""
    try:
        result = subprocess.run(
            cmd, shell=True, check=check,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Perintah gagal: {cmd}", file=sys.stderr)
        print(f"       stderr: {e.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def check_prerequisites():
    """Pastikan lscpu dan numactl tersedia."""
    missing = []
    for tool in ["lscpu", "numactl"]:
        r = subprocess.run(f"command -v {tool}", shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            missing.append(tool)
    if missing:
        print(f"ERROR: Tool tidak ditemukan: {', '.join(missing)}", file=sys.stderr)
        print("       Install: sudo apt install util-linux numactl", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing lscpu
# ─────────────────────────────────────────────────────────────────────────────

def parse_lscpu_extended():
    """
    Parse 'lscpu -p=CPU,CORE,SOCKET,NODE' → list of dicts {cpu, core, socket, node}.
    Setiap baris merepresentasikan satu vCPU dengan lokasinya dalam topologi fisik.
    """
    raw = run("lscpu -p=CPU,CORE,SOCKET,NODE")
    entries = []
    for line in raw.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            entries.append({
                "cpu":    int(parts[0]),
                "core":   int(parts[1]),
                "socket": int(parts[2]),
                "node":   int(parts[3]),
            })
        except ValueError:
            continue
    if not entries:
        print("ERROR: Tidak bisa mem-parse output 'lscpu -p=CPU,CORE,SOCKET,NODE'",
              file=sys.stderr)
        sys.exit(1)
    return entries


def parse_lscpu_summary():
    """Parse 'lscpu' (tanpa flag) → dict key:value."""
    raw = run("lscpu")
    info = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            info[key.strip()] = val.strip()
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Analisis topologi
# ─────────────────────────────────────────────────────────────────────────────

def build_sibling_map(entries):
    """
    Bangun peta (socket, core) → [cpu_ids].
    Dua vCPU adalah 'sibling' jika berbagi physical core yang sama (socket+core identik).
    """
    groups = {}
    for e in entries:
        key = (e["socket"], e["core"])
        groups.setdefault(key, []).append(e["cpu"])
    for key in groups:
        groups[key].sort()
    return groups


def select_solver_and_reserved(sibling_map):
    """
    Pilih SOLVER_THREADS vCPU untuk solver dan RESERVED_CPUS untuk sistem.

    Strategi untuk meminimalkan kontaminasi sibling:
      - Solver CPUs: satu vCPU dari setiap physical core (tidak ada sibling
        di antara sesama solver CPU).
      - Reserved CPU: dari physical core yang belum dipakai solver, jika ada;
        jika tidak, pilih sibling solver CPU yang paling sedikit digunakan.

    Returns:
        solver_cpus       : list[int] — vCPU IDs untuk solver
        reserved_cpu      : int       — vCPU ID untuk reserved
        contamination     : list[dict] — pasangan sibling yang tidak terhindarkan
        selection_error   : str|None  — pesan error jika seleksi tidak mungkin
    """
    # Urutkan physical cores berdasarkan CPU terkecil di dalamnya
    cores = sorted(sibling_map.values(), key=lambda g: g[0])
    n_physical_cores = len(cores)

    # Pilih satu vCPU (yang pertama/terkecil) dari setiap core untuk solver
    solver_cpus = [core[0] for core in cores[:SOLVER_THREADS]]

    if len(solver_cpus) < SOLVER_THREADS:
        return None, None, [], (
            f"Hanya {n_physical_cores} physical core — tidak cukup untuk "
            f"{SOLVER_THREADS} solver CPUs tanpa sibling satu sama lain."
        )

    solver_set = set(solver_cpus)
    all_vcpus  = {cpu for core in cores for cpu in core}
    remaining  = sorted(all_vcpus - solver_set)

    # Cari reserved CPU dari physical core yang TIDAK dipakai solver (isolasi bersih)
    clean_reserved_candidates = [
        cpu for core in cores
        if not any(c in solver_set for c in core)
        for cpu in core
    ]

    contamination = []

    if clean_reserved_candidates:
        reserved_cpu = min(clean_reserved_candidates)
        # Tidak ada kontaminasi
    elif remaining:
        # Semua physical core sudah dipakai solver (khas pada c2-standard-8 dengan
        # 4 physical core dan 4 solver threads) — reserved CPU pasti sibling satu solver CPU.
        reserved_cpu = remaining[0]
        for core in cores:
            if reserved_cpu in core:
                sibling_solvers = [c for c in core if c in solver_set]
                if sibling_solvers:
                    contamination.append({
                        "reserved_cpu":      reserved_cpu,
                        "sibling_solver_cpu": sibling_solvers[0],
                        "physical_core_group": core,
                        "description": (
                            f"CPU {reserved_cpu} (reserved) adalah sibling dari "
                            f"CPU {sibling_solvers[0]} (solver) pada physical core {core} "
                            f"— berbagi cache L1/L2 dan execution pipeline."
                        ),
                    })
                break
    else:
        return solver_cpus, None, [], "Tidak ada vCPU tersisa untuk reserved CPU."

    return solver_cpus, reserved_cpu, contamination, None


def analyze_options(total_vcpus, threads_per_core, n_physical_cores,
                    solver_cpus, reserved_cpu, contamination, selection_error):
    """
    Evaluasi kelayakan Option 1, 2, 3 berdasarkan topologi aktual VM.

    Option 1 : SMT aktif, pilih core secara manual (tidak perlu recreate VM)
    Option 2 : Matikan SMT via --threads-per-core=1 (butuh recreate VM)
    Option 3 : Eskalasi ke c2-standard-16 (butuh recreate VM + kuota 16 vCPU)
    """
    options = {}
    smt_active = threads_per_core >= 2

    # ── Option 1 ─────────────────────────────────────────────────────────────
    if not smt_active:
        options["option1"] = {
            "viable": False,
            "rationale": (
                "SMT tidak aktif pada VM ini (threads_per_core=1). "
                "Option 1 hanya relevan ketika SMT aktif."
            ),
            "vm_recreation_required": False,
        }
    elif selection_error:
        options["option1"] = {
            "viable": False,
            "rationale": f"Seleksi core gagal: {selection_error}",
            "vm_recreation_required": False,
        }
    else:
        contam_desc = (
            "Tidak ada kontaminasi sibling di antara solver CPUs maupun antara "
            "solver CPU dan reserved CPU." if not contamination else
            " ".join(c["description"] for c in contamination)
        )
        options["option1"] = {
            "viable": True,
            "rationale": (
                f"SMT aktif ({threads_per_core} thread/physical core). "
                f"{n_physical_cores} physical core tersedia. "
                f"4 solver CPUs dipilih satu per physical core — tidak ada sibling "
                f"di antara sesama solver CPU. "
                + contam_desc
            ),
            "solver_cpus":            solver_cpus,
            "reserved_cpu":           reserved_cpu,
            "contamination":          contamination,
            "contamination_avoidable": len(contamination) == 0,
            "vm_recreation_required": False,
        }

    # ── Option 2 ─────────────────────────────────────────────────────────────
    # Dengan --threads-per-core=1: hanya physical_cores vCPU yang terlihat guest.
    vcpus_smt_off = n_physical_cores
    option2_viable = vcpus_smt_off >= CPUS_NEEDED
    options["option2"] = {
        "viable": option2_viable,
        "rationale": (
            f"Menonaktifkan SMT (--threads-per-core=1) menyisakan {vcpus_smt_off} vCPU "
            f"(1 per physical core). Dibutuhkan {CPUS_NEEDED} vCPU "
            f"({RESERVED_CPUS} reserved + {SOLVER_THREADS} solver). "
            + (
                f"{vcpus_smt_off} >= {CPUS_NEEDED} — FEASIBLE dengan isolasi cache lebih bersih "
                f"(tidak ada sibling pair sama sekali)."
                if option2_viable else
                f"Hanya {vcpus_smt_off} vCPU tersedia setelah SMT dimatikan, "
                f"kurang dari {CPUS_NEEDED} yang dibutuhkan. TIDAK FEASIBLE "
                f"tanpa mengurangi jumlah solver thread (bertentangan dengan desain proposal: "
                f"Threads=4 dan Guaranteed QoS dengan requests.cpu=4)."
            )
        ),
        "vcpus_after_smt_off":      vcpus_smt_off,
        "vm_recreation_required":   True,
        "gcloud_flag_to_add":       "--threads-per-core=1",
        "note": (
            "GCP menghitung kuota berdasarkan machine type nominal (c2-standard-8 = 8 vCPU), "
            "bukan jumlah thread yang terlihat guest setelah SMT dimatikan."
        ),
    }

    # ── Option 3 ─────────────────────────────────────────────────────────────
    # c2-standard-16: 8 vCPU, 8 physical core (SMT dinonaktifkan)
    # 4 solver + 1 reserved → 4 core fisik eksklusif tanpa SMT packing dari Kubernetes
    options["option3"] = {
        "viable": "requires_vm_recreation",
        "rationale": (
            "c2-standard-16 dengan --threads-per-core=1 mematikan SMT sehingga VM "
            "hanya melihat 8 vCPU murni. Ini adalah satu-satunya cara memaksa Kubelet "
            "memberikan 4 core fisik terisolasi tanpa SMT packing. Membutuhkan kuota "
            "16 vCPU GCP. VM harus dibuat ulang."
        ),
        "target_machine_type":    "c2-standard-16",
        "expected_vcpus":         8,
        "expected_physical_cores": 8,
        "vm_recreation_required": True,
        "gcloud_machine_type_flag": "--machine-type=c2-standard-16 --threads-per-core=1",
        "quota_required_vcpus":   16,
    }

    # ── Rekomendasi ───────────────────────────────────────────────────────────
    if options["option1"]["viable"] is True:
        recommendation = "option1"
        rec_rationale  = (
            "Option 1 layak digunakan tanpa recreate VM. "
            + (
                "Tidak ada kontaminasi sibling — isolasi bersih."
                if not contamination else
                f"Kontaminasi satu sibling pair (reserved CPU {reserved_cpu} berbagi physical core "
                f"dengan solver CPU {contamination[0]['sibling_solver_cpu']}) tidak terhindarkan "
                f"pada {n_physical_cores} physical core dengan alokasi {SOLVER_THREADS} solver thread, "
                f"namun dapat didokumentasikan secara eksplisit di Subbab 'Keterbatasan Metodologis' "
                f"sesuai proposal. "
            )
            + (
                "Option 2 tidak feasible (SMT off menyisakan terlalu sedikit vCPU). "
                if not options["option2"]["viable"] else
                "Option 2 juga feasible jika isolasi lebih bersih diinginkan. "
            )
            + "Option 3 tersedia jika kontaminasi dinilai tidak dapat diterima."
        )
    elif options["option2"]["viable"] is True:
        recommendation = "option2"
        rec_rationale  = (
            "Option 1 tidak feasible; Option 2 feasible (SMT off, isolasi bersih). "
            "Membutuhkan recreate VM dengan --threads-per-core=1."
        )
    else:
        recommendation = "option3"
        rec_rationale  = (
            "Option 1 dan Option 2 tidak feasible pada topologi ini. "
            "Diperlukan eskalasi ke c2-standard-16 (Option 3)."
        )

    return options, recommendation, rec_rationale


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Karakterisasi topologi hardware VM untuk eksperimen CPU pinning."
    )
    parser.add_argument(
        "--repo-dir",
        default=str(Path(__file__).resolve().parent.parent),
        help="Path ke root direktori repo (default: parent dari scripts/)"
    )
    args = parser.parse_args()

    repo_dir      = Path(args.repo_dir)
    infra_dir     = repo_dir / "infra"
    report_path   = infra_dir / "topology-report.json"
    decision_path = infra_dir / "topology-decision.txt"
    infra_dir.mkdir(exist_ok=True)

    print("=" * 62)
    print("  KARAKTERISASI TOPOLOGI HARDWARE")
    print("  Phase 1 — characterize_topology.py")
    print("=" * 62)
    print()

    check_prerequisites()

    # ── Kumpulkan data sistem ─────────────────────────────────────────────────

    print(">>> Menjalankan lscpu ...")
    lscpu_summary_raw  = run("lscpu")
    lscpu_extended_raw = run("lscpu -p=CPU,CORE,SOCKET,NODE")

    print(">>> Menjalankan numactl --hardware ...")
    numactl_raw = run("numactl --hardware", check=False)
    if not numactl_raw:
        numactl_raw = "(numactl tidak menghasilkan output — NUMA mungkin tidak tersedia)"

    # ── Parse ─────────────────────────────────────────────────────────────────

    lscpu_info   = parse_lscpu_summary()
    entries      = parse_lscpu_extended()
    sibling_map  = build_sibling_map(entries)
    sibling_pairs_list = sorted([sorted(g) for g in sibling_map.values()])

    total_vcpus      = len(entries)
    threads_per_core = int(lscpu_info.get("Thread(s) per core", "1"))
    cores_per_socket = int(lscpu_info.get("Core(s) per socket", str(total_vcpus)))
    sockets          = int(lscpu_info.get("Socket(s)", "1"))
    n_physical_cores = len(sibling_map)
    model_name       = lscpu_info.get("Model name", "unknown")

    try:
        numa_nodes = int(lscpu_info.get("NUMA node(s)", "1"))
    except ValueError:
        numa_nodes = 1

    if numa_nodes > 1:
        print(f"\n  ⚠  PERINGATAN: VM memiliki {numa_nodes} NUMA node.")
        print("      Proposal mengasumsikan single-node NUMA untuk menghilangkan NUMA latency")
        print("      sebagai confounder. Pertimbangkan numactl --cpunodebind=0 atau dokumentasikan")
        print("      di Subbab Keterbatasan Metodologis.\n")

    vcpu_to_physical_core = {
        str(e["cpu"]): {"core": e["core"], "socket": e["socket"], "node": e["node"]}
        for e in entries
    }

    # ── Seleksi core ─────────────────────────────────────────────────────────

    solver_cpus, reserved_cpu, contamination, selection_error = \
        select_solver_and_reserved(sibling_map)

    # ── Analisis opsi ─────────────────────────────────────────────────────────

    options, recommendation, rec_rationale = analyze_options(
        total_vcpus, threads_per_core, n_physical_cores,
        solver_cpus, reserved_cpu, contamination, selection_error
    )

    # ── Tulis laporan JSON ────────────────────────────────────────────────────

    report = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "machine": {
            "model_name":      model_name,
            "total_vcpus":     total_vcpus,
            "threads_per_core": threads_per_core,
            "cores_per_socket": cores_per_socket,
            "sockets":          sockets,
            "physical_cores":   n_physical_cores,
            "numa_nodes":       numa_nodes,
        },
        "sibling_pairs":         sibling_pairs_list,
        "vcpu_topology":         vcpu_to_physical_core,
        "experiment_requirements": {
            "solver_threads":  SOLVER_THREADS,
            "reserved_cpus":   RESERVED_CPUS,
            "total_needed":    CPUS_NEEDED,
        },
        "core_selection": {
            "solver_cpus":     solver_cpus,
            "reserved_cpu":    reserved_cpu,
            "contamination":   contamination,
            "selection_error": selection_error,
        },
        "options":                 options,
        "recommendation":          recommendation,
        "recommendation_rationale": rec_rationale,
        # Diisi manual oleh peneliti setelah diskusi dengan pembimbing:
        "confirmed_decision":      None,
        "raw_output": {
            "lscpu":         lscpu_summary_raw,
            "lscpu_p":       lscpu_extended_raw,
            "numactl":       numactl_raw,
        },
    }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    # ── Tampilkan ringkasan ───────────────────────────────────────────────────

    print()
    print("─" * 62)
    print("  RINGKASAN TOPOLOGI")
    print("─" * 62)
    print(f"  Model         : {model_name}")
    print(f"  Total vCPU    : {total_vcpus}")
    print(f"  Thread/core   : {threads_per_core}  ({'SMT aktif' if threads_per_core >= 2 else 'SMT tidak aktif'})")
    print(f"  Physical core : {n_physical_cores}")
    print(f"  Socket        : {sockets}")
    print(f"  NUMA node(s)  : {numa_nodes}")
    print()
    print("  Sibling pairs (vCPU berbagi physical core):")
    for pair in sibling_pairs_list:
        print(f"    {pair}")
    print()

    print("─" * 62)
    print("  ANALISIS OPSI")
    print("─" * 62)

    labels = {True: "LAYAK", False: "TIDAK LAYAK", "requires_vm_recreation": "LAYAK (butuh VM baru)"}

    for opt_key in ("option1", "option2", "option3"):
        opt    = options[opt_key]
        viable = opt.get("viable")
        label  = labels.get(str(viable) if not isinstance(viable, bool) else viable, str(viable))
        print(f"\n  [{opt_key.upper()}] {label}")
        print(f"  {opt.get('rationale', '')}")
        if opt_key == "option1" and opt.get("viable") is True:
            print(f"  Solver CPUs  : {opt.get('solver_cpus')}")
            print(f"  Reserved CPU : {opt.get('reserved_cpu')}")
            for c in opt.get("contamination", []):
                print(f"  ⚠  {c['description']}")
            if not opt.get("contamination"):
                print("  ✓  Tidak ada kontaminasi sibling")

    print()
    print("─" * 62)
    print(f"  REKOMENDASI: {recommendation.upper()}")
    print("─" * 62)
    print(f"  {rec_rationale}")
    print()
    print(f">>> Laporan lengkap: {report_path}")
    print()

    # ── STOP — konfirmasi manual ──────────────────────────────────────────────

    print("=" * 62)
    print("  !! HENTI — KONFIRMASI MANUAL DIPERLUKAN !!")
    print("=" * 62)
    print()
    print("  Baca laporan di atas dan diskusikan dengan pembimbing jika perlu.")
    print()
    if decision_path.exists():
        current = decision_path.read_text().strip()
        print(f"  File keputusan sudah ada: '{current}'")
        print("  Timpa jika keputusan berubah:")
    else:
        print("  Tetapkan keputusan dengan menulis salah satu baris berikut:")
    print()
    print(f"    echo \"option1\" > {decision_path}")
    print(f"    echo \"option2\" > {decision_path}   # recreate VM + --threads-per-core=1")
    print(f"    echo \"option3\" > {decision_path}   # recreate VM sebagai c2-standard-16")
    print()
    print("  Setelah menetapkan keputusan, lanjutkan ke Phase 3:")
    print("    python3 scripts/render_kubelet_configs.py")
    print()
    print("  JANGAN lanjutkan setup Kubernetes (§2.3 dst.) sebelum keputusan ditetapkan.")
    print()


if __name__ == "__main__":
    main()
