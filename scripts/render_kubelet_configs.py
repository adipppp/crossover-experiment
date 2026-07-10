#!/usr/bin/env python3
"""
render_kubelet_configs.py — Mengisi placeholder __RESERVED_SYSTEM_CPUS__ pada
template kubelet-configs/ berdasarkan topology-report.json dan
topology-decision.txt, lalu menulis hasil ke kubelet-configs/rendered/.

Jalankan ini SETELAH characterize_topology.py (§1.7.3) dan SETELAH menetapkan
keputusan di infra/topology-decision.txt (§1.7.3 HENTI MANUAL).

Hasil:
  kubelet-configs/rendered/condition-A-none.yaml    ← siap disalin ke kubelet
  kubelet-configs/rendered/condition-B-static.yaml  ← siap disalin ke kubelet
  kubelet-configs/rendered/render-manifest.json     ← audit trail render ini

Penggunaan:
    python3 scripts/render_kubelet_configs.py [--repo-dir /path/ke/repo]
    python3 scripts/render_kubelet_configs.py --dry-run   # hanya tampilkan, jangan tulis
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Konstanta desain eksperimen
# ─────────────────────────────────────────────────────────────────────────────

PLACEHOLDER        = "__RESERVED_SYSTEM_CPUS__"
VALID_DECISIONS    = ("option1", "option2", "option3")

# Ekspektasi topologi per opsi — untuk cross-validasi laporan vs keputusan
OPTION_VCPU_EXPECT = {
    "option1": {"threads_per_core": 2,  "total_vcpus": 8,  "machine_hint": "c2-standard-8, SMT aktif"},
    "option2": {"threads_per_core": 1,  "total_vcpus": 4,  "machine_hint": "c2-standard-8, SMT nonaktif"},
    "option3": {"threads_per_core": 1,  "total_vcpus": 8, "machine_hint": "c2-standard-16, SMT nonaktif"},
}

TEMPLATE_FILES = {
    "none":   "condition-A-none.yaml",
    "static": "condition-B-static.yaml",
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_decision(decision_path: Path) -> str:
    if not decision_path.exists():
        print(
            f"ERROR: infra/topology-decision.txt tidak ditemukan.\n"
            f"       Jalankan characterize_topology.py terlebih dahulu (§1.7.3),\n"
            f"       lalu tetapkan keputusan:\n"
            f"         echo \"option1\" > {decision_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    decision = decision_path.read_text().strip().lower()
    if decision not in VALID_DECISIONS:
        print(
            f"ERROR: Nilai tidak valid di topology-decision.txt: '{decision}'\n"
            f"       Harus salah satu dari: {VALID_DECISIONS}",
            file=sys.stderr,
        )
        sys.exit(1)
    return decision


def load_topology_report(report_path: Path) -> dict:
    if not report_path.exists():
        print(
            f"ERROR: infra/topology-report.json tidak ditemukan.\n"
            f"       Jalankan characterize_topology.py terlebih dahulu (§1.7.3).",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(report_path) as f:
        report = json.load(f)

    # Validasi field yang diperlukan
    required_paths = [
        ("core_selection", "reserved_cpu"),
        ("core_selection", "solver_cpus"),
        ("machine", "total_vcpus"),
        ("machine", "threads_per_core"),
        ("machine", "physical_cores"),
    ]
    for keys in required_paths:
        node = report
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                print(
                    f"ERROR: Field '{'.'.join(keys)}' tidak ditemukan di topology-report.json.\n"
                    f"       Jalankan ulang characterize_topology.py untuk memperbarui laporan.",
                    file=sys.stderr,
                )
                sys.exit(1)
            node = node[key]

    if report["core_selection"]["reserved_cpu"] is None:
        print(
            "ERROR: core_selection.reserved_cpu bernilai null di topology-report.json.\n"
            "       Topologi mungkin tidak valid. Jalankan ulang characterize_topology.py.",
            file=sys.stderr,
        )
        sys.exit(1)

    return report


def cross_validate(decision: str, report: dict) -> list[str]:
    """
    Bandingkan ekspektasi topologi untuk keputusan yang dipilih dengan
    topologi aktual yang dilaporkan. Kembalikan daftar peringatan (bukan error)
    agar peneliti dapat menilai sendiri.
    """
    warnings = []
    expect   = OPTION_VCPU_EXPECT[decision]
    actual_threads = report["machine"]["threads_per_core"]
    actual_vcpus   = report["machine"]["total_vcpus"]

    if actual_threads != expect["threads_per_core"]:
        warnings.append(
            f"threads_per_core aktual ({actual_threads}) tidak sesuai ekspektasi "
            f"{decision} ({expect['threads_per_core']}). "
            f"Pastikan VM sudah dibuat ulang sesuai §1.3.1/§1.3.2 jika memilih "
            f"option2 atau option3, lalu jalankan ulang characterize_topology.py."
        )

    if actual_vcpus != expect["total_vcpus"]:
        warnings.append(
            f"total_vcpus aktual ({actual_vcpus}) tidak sesuai ekspektasi "
            f"{decision} ({expect['total_vcpus']} — {expect['machine_hint']}). "
            f"Pastikan topology-report.json diperbarui SETELAH VM yang benar aktif."
        )

    return warnings


def render_template(template_path: Path, reserved_cpu_id: int, expect_placeholder: bool = True) -> str:
    """Baca template, ganti placeholder jika diharapkan, kembalikan string hasil render."""
    if not template_path.exists():
        print(f"ERROR: Template tidak ditemukan: {template_path}", file=sys.stderr)
        sys.exit(1)

    content = template_path.read_text(encoding="utf-8")

    if not expect_placeholder:
        if PLACEHOLDER in content:
            print(
                f"ERROR: Placeholder '{PLACEHOLDER}' ditemukan di {template_path.name} "
                f"meskipun tidak diharapkan untuk policy 'none'.",
                file=sys.stderr,
            )
            sys.exit(1)
        return content

    if PLACEHOLDER not in content:
        print(
            f"ERROR: Placeholder '{PLACEHOLDER}' tidak ditemukan di {template_path.name}.\n"
            f"       Pastikan file template di kubelet-configs/ belum dimodifikasi manual.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Replace all occurrences: the YAML value line uses "quotes" around the
    # placeholder, but the comment block references it without quotes. A single
    # str.replace on the bare placeholder handles both safely.
    rendered = content.replace(PLACEHOLDER, str(reserved_cpu_id))

    # Verifikasi placeholder tidak tersisa
    if PLACEHOLDER in rendered:
        print(
            f"ERROR: Masih ada placeholder '{PLACEHOLDER}' setelah substitusi di "
            f"{template_path.name}. File mungkin corrupt.",
            file=sys.stderr,
        )
        sys.exit(1)

    return rendered


def add_render_header(rendered: str, decision: str, reserved_cpu: int,
                      solver_cpus: list, generated_at: str) -> str:
    """
    Tambahkan header komentar pada hasil render agar file rendered/
    self-documenting — jelas kapan di-render, dari keputusan apa, dan
    dengan CPU mana sebagai reserved.
    """
    header = (
        f"# RENDERED oleh render_kubelet_configs.py pada {generated_at}\n"
        f"# Keputusan topologi : {decision}\n"
        f"# reservedSystemCPUs : {reserved_cpu}\n"
        f"# Solver CPUs (info) : {solver_cpus}\n"
        f"# File ini BUKAN template — jangan edit placeholder di sini.\n"
        f"# Edit template di kubelet-configs/ lalu render ulang.\n"
        f"#\n"
    )
    # Sisipkan setelah baris komentar template yang ada (baris yang diawali '#')
    lines = rendered.splitlines(keepends=True)
    insert_pos = 0
    for i, line in enumerate(lines):
        if not line.startswith("#"):
            insert_pos = i
            break
    lines.insert(insert_pos, header)
    return "".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Render kubelet config templates dengan reservedSystemCPUs aktual."
    )
    parser.add_argument(
        "--repo-dir",
        default=str(Path(__file__).resolve().parent.parent),
        help="Path ke root direktori repo (default: parent dari scripts/)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Tampilkan hasil render ke stdout tanpa menulis file."
    )
    args = parser.parse_args()

    repo_dir       = Path(args.repo_dir)
    infra_dir      = repo_dir / "infra"
    configs_dir    = repo_dir / "kubelet-configs"
    rendered_dir   = configs_dir / "rendered"
    decision_path  = infra_dir  / "topology-decision.txt"
    report_path    = infra_dir  / "topology-report.json"
    manifest_path  = rendered_dir / "render-manifest.json"

    print("=" * 62)
    print("  RENDER KUBELET CONFIGS")
    print("  Phase 3 — render_kubelet_configs.py")
    print("=" * 62)
    print()

    # ── Baca input ────────────────────────────────────────────────────────────
    decision = load_decision(decision_path)
    report   = load_topology_report(report_path)

    reserved_cpu = int(report["core_selection"]["reserved_cpu"])
    solver_cpus  = report["core_selection"]["solver_cpus"]
    contamination = report["core_selection"].get("contamination", [])
    machine      = report["machine"]
    generated_at = datetime.now(timezone.utc).isoformat()

    # ── Cross-validasi topologi vs keputusan ─────────────────────────────────
    cv_warnings = cross_validate(decision, report)

    # ── Tampilkan ringkasan input ─────────────────────────────────────────────
    print("─" * 62)
    print("  INPUT")
    print("─" * 62)
    print(f"  Keputusan topologi  : {decision}")
    print(f"  Model mesin         : {machine.get('model_name', 'unknown')}")
    print(f"  Total vCPU          : {machine['total_vcpus']}")
    print(f"  Threads per core    : {machine['threads_per_core']}")
    print(f"  Physical cores      : {machine['physical_cores']}")
    print(f"  reservedSystemCPUs  : {reserved_cpu}  (akan diisi ke placeholder)")
    print(f"  Solver CPUs (info)  : {solver_cpus}")
    print()

    if cv_warnings:
        print("  ⚠  PERINGATAN CROSS-VALIDASI:")
        for w in cv_warnings:
            print(f"     {w}")
        print()
        if not args.dry_run:
            print("  Lanjutkan render meskipun ada peringatan di atas? [y/N]")
            ans = input().strip().lower()
            if ans != "y":
                print("  Dibatalkan. Periksa peringatan di atas sebelum melanjutkan.")
                sys.exit(1)
            print()

    if contamination:
        print("  ⚠  KONTAMINASI SIBLING (didokumentasikan dari topology-report.json):")
        for c in contamination:
            print(f"     {c.get('description', str(c))}")
        print()
        print("  Ini diharapkan pada c2-standard-8 dengan 4 physical core dan")
        print("  alokasi 4 solver thread + 1 reserved CPU. Kondisi ini harus")
        print("  didiskusikan secara eksplisit di Subbab 'Keterbatasan Metodologis'.")
        print()

    # ── Render template ───────────────────────────────────────────────────────
    print("─" * 62)
    print("  RENDER")
    print("─" * 62)
    print()

    rendered_outputs = {}
    for policy, filename in TEMPLATE_FILES.items():
        template_path = configs_dir / filename
        print(f"  [{filename}]")
        expect_placeholder = (policy != "none")
        rendered = render_template(template_path, reserved_cpu, expect_placeholder=expect_placeholder)
        rendered = add_render_header(rendered, decision, reserved_cpu,
                                     solver_cpus, generated_at)
        rendered_outputs[filename] = rendered
        if expect_placeholder:
            print(f"    reservedSystemCPUs : {reserved_cpu}  ✓")
        else:
            print(f"    reservedSystemCPUs : tidak ada (sesuai proposal)  ✓")
        print(f"    cpuManagerPolicy   : {'none' if policy == 'none' else 'static'}  ✓")
        print(f"    full-pcpus-only    : tidak ada (sesuai proposal)  ✓")
        print()

    # ── Tulis atau tampilkan ──────────────────────────────────────────────────
    if args.dry_run:
        print("=" * 62)
        print("  DRY RUN — output ke stdout, tidak menulis file")
        print("=" * 62)
        for filename, rendered in rendered_outputs.items():
            print(f"\n{'─'*62}")
            print(f"  {filename}")
            print(f"{'─'*62}")
            print(rendered)
        return

    rendered_dir.mkdir(exist_ok=True)

    for filename, rendered in rendered_outputs.items():
        out_path = rendered_dir / filename
        out_path.write_text(rendered, encoding="utf-8")
        print(f"  Ditulis : {out_path}")

    # ── Tulis render manifest ─────────────────────────────────────────────────
    manifest = {
        "rendered_at":      generated_at,
        "decision":         decision,
        "topology_report":  str(report_path),
        "reserved_cpu":     reserved_cpu,
        "solver_cpus":      solver_cpus,
        "contamination":    contamination,
        "cross_validation_warnings": cv_warnings,
        "machine":          machine,
        "files_written": [
            str(rendered_dir / fn) for fn in TEMPLATE_FILES.values()
        ],
        "placeholder_used": PLACEHOLDER,
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"  Manifest : {manifest_path}")

    # ── Ringkasan dan instruksi selanjutnya ───────────────────────────────────
    print()
    print("=" * 62)
    print("  SELESAI")
    print("=" * 62)
    print()
    print("  File rendered siap digunakan. Salin ke kubelet saat §2.7:")
    print()
    print(f"    sudo cp {rendered_dir}/condition-A-none.yaml \\")
    print(f"      /var/lib/kubelet/config.yaml")
    print()
    print("  switch_cpu_manager_policy.sh sudah dikonfigurasi untuk membaca")
    print("  dari kubelet-configs/rendered/ secara otomatis.")
    print()
    if contamination:
        print("  ⚠  Ingat: kontaminasi sibling di atas harus didokumentasikan")
        print("     di bagian 'Keterbatasan Metodologis' laporan akhir.")
        print()


if __name__ == "__main__":
    main()
