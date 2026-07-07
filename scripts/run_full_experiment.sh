#!/usr/bin/env bash
# run_full_experiment.sh — Orkestrasi LENGKAP dua blok (block counterbalancing).
#
# Proposal (Subbab "Prosedur Eksperimen") menetapkan:
#   Blok 1 (hari ke-1, urutan A→B): 15 rep Kondisi A, lalu 15 rep Kondisi B.
#   Blok 2 (hari ke-2, urutan B→A): 15 rep Kondisi B, lalu 15 rep Kondisi A.
# Total: 30 rep per kondisi per instance.
#
# Setiap perpindahan kondisi memerlukan:
#   switch_cpu_manager_policy.sh (drain → stop kubelet → hapus cpu_manager_state
#   → tukar config → start kubelet → uncordon) — memakan beberapa menit.
#
# Skrip ini TIDAK otomatis menjalankan kedua blok berturut-turut dalam satu
# sesi. Blok 2 membutuhkan konfirmasi eksplisit bahwa hari kalender berbeda
# dari Blok 1, kecuali --force-same-day diberikan (untuk debugging saja).
#
# Penggunaan:
#   bash scripts/run_full_experiment.sh block1              # jalankan Blok 1
#   bash scripts/run_full_experiment.sh block2              # jalankan Blok 2
#   bash scripts/run_full_experiment.sh block2 --force-same-day  # (debug saja)
#   bash scripts/run_full_experiment.sh status              # lihat status blok
#
# State tersimpan di: infra/experiment-state.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_FILE="$REPO_DIR/infra/experiment-state.json"
N_REPS=15
CPU_COUNT=4

BLOCK_CMD="${1:-status}"
FORCE_SAME_DAY=false
for arg in "${@:2}"; do
  [[ "$arg" == "--force-same-day" ]] && FORCE_SAME_DAY=true
done

# ─────────────────────────────────────────────────────────────────────────────
# Utilitas state
# ─────────────────────────────────────────────────────────────────────────────

init_state() {
  mkdir -p "$REPO_DIR/infra"
  if [[ ! -f "$STATE_FILE" ]]; then
    python3 - <<'PYEOF' "$STATE_FILE"
import json, sys
state = {
    "block1": {"status": "pending", "started_at": None, "completed_at": None,
               "order": "A_then_B"},
    "block2": {"status": "pending", "started_at": None, "completed_at": None,
               "order": "B_then_A"},
    "order_effect_check_done": False,
}
with open(sys.argv[1], "w") as f:
    json.dump(state, f, indent=2)
PYEOF
    echo ">>> State baru dibuat: $STATE_FILE"
  fi
}

get_state_field() {
  # Argumen: blok (block1|block2), field (status|started_at|completed_at)
  python3 - "$STATE_FILE" "$1" "$2" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f: state = json.load(f)
print(state[sys.argv[2]].get(sys.argv[3]) or "")
PYEOF
}

set_state_field() {
  # Argumen: blok, field, nilai
  python3 - "$STATE_FILE" "$1" "$2" "$3" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f: state = json.load(f)
state[sys.argv[2]][sys.argv[3]] = sys.argv[4]
with open(sys.argv[1], "w") as f: json.dump(state, f, indent=2)
PYEOF
}

print_status() {
  echo "========================================================"
  echo "  STATUS EKSPERIMEN"
  echo "========================================================"
  python3 - "$STATE_FILE" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f: state = json.load(f)
for blok in ("block1", "block2"):
    b = state[blok]
    order_str = b['order'].replace('_then_', ' -> ')
    print(f"  {blok.upper()} ({order_str})")
    print(f"    Status    : {b['status']}")
    print(f"    Dimulai   : {b['started_at'] or '-'}")
    print(f"    Selesai   : {b['completed_at'] or '-'}")
    print()
print(f"  Order-effect check selesai: {state['order_effect_check_done']}")
PYEOF
  echo "========================================================"
}

# ─────────────────────────────────────────────────────────────────────────────
# Validasi prasyarat
# ─────────────────────────────────────────────────────────────────────────────

check_rendered_configs() {
  local a="$REPO_DIR/kubelet-configs/rendered/condition-A-none.yaml"
  local b="$REPO_DIR/kubelet-configs/rendered/condition-B-static.yaml"
  if [[ ! -f "$a" || ! -f "$b" ]]; then
    echo "FATAL: Konfigurasi kubelet belum di-render." >&2
    echo "       Jalankan: python3 scripts/render_kubelet_configs.py" >&2
    exit 1
  fi
}

check_pmu_verdict() {
  local report="$REPO_DIR/infra/pmu-validation-report.json"
  if [[ ! -f "$report" ]]; then
    echo "PERINGATAN: infra/pmu-validation-report.json tidak ditemukan." >&2
    echo "            Jalankan validate_pmu_fidelity.py sebelum eksperimen." >&2
    return
  fi
  local verdict
  verdict=$(python3 -c "import json; print(json.load(open('$report'))['verdict'])" 2>/dev/null || echo "UNKNOWN")
  if [[ "$verdict" == "NO-GO" ]]; then
    echo ""
    echo "  ⚠  PMU verdict: NO-GO. Metrik PMU tidak valid pada VM ini."
    echo "     Hanya involuntary context switches yang digunakan dalam analisis."
    echo "     (lihat infra/pmu-validation-report.json)"
    echo ""
  else
    echo "  ✓ PMU verdict: $verdict"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Satu "sisi" blok: jalankan N_REPS untuk satu kondisi
# ─────────────────────────────────────────────────────────────────────────────

run_condition() {
  local condition="$1"
  local block_num="$2"

  echo ""
  echo "────────────────────────────────────────────────────────"
  echo "  Kondisi: $condition | Blok: $block_num"
  echo "────────────────────────────────────────────────────────"

  # Pastikan CPU Manager policy sesuai kondisi
  local actual_policy
  actual_policy=$(sudo grep "cpuManagerPolicy:" /var/lib/kubelet/config.yaml 2>/dev/null | awk '{print $2}' || echo "none")
  actual_policy="${actual_policy:-none}"

  if [[ "$actual_policy" != "$condition" ]]; then
    echo ">>> Kondisi aktif ($actual_policy) != $condition — menjalankan switch..."
    bash "$SCRIPT_DIR/switch_cpu_manager_policy.sh" "$condition"
  else
    echo ">>> CPU Manager sudah '$condition' — tidak perlu switch."
  fi

  bash "$SCRIPT_DIR/run_experiment.sh" "$condition" "$N_REPS" "$CPU_COUNT" "$block_num"
}

# ─────────────────────────────────────────────────────────────────────────────
# Pengecekan beda hari untuk Blok 2
# ─────────────────────────────────────────────────────────────────────────────

check_different_day() {
  local blk1_completed
  blk1_completed=$(get_state_field "block1" "completed_at")
  if [[ -z "$blk1_completed" ]]; then
    return  # Blok 1 belum pernah selesai — tidak bisa memeriksa hari
  fi

  local day_blk1 day_today
  day_blk1=$(python3 -c "from datetime import datetime; print(datetime.fromisoformat('$blk1_completed').strftime('%Y-%m-%d'))" 2>/dev/null || echo "")
  day_today=$(date -u +"%Y-%m-%d")

  if [[ "$day_blk1" == "$day_today" ]]; then
    echo ""
    echo "  ⚠  PERINGATAN: Blok 1 selesai pada $day_blk1 — HARI INI."
    echo "     Proposal menetapkan Blok 2 dijalankan di HARI BERBEDA untuk"
    echo "     mengontrol variabel perancu temporal (noisy neighbor, thermal drift)."
    echo ""
    if [[ "$FORCE_SAME_DAY" == "true" ]]; then
      echo "     --force-same-day aktif. Melanjutkan (gunakan untuk debugging saja)."
    else
      echo "     Jalankan ulang BESOK, atau gunakan --force-same-day jika yakin."
      exit 1
    fi
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# BLOK 1: urutan A → B
# ─────────────────────────────────────────────────────────────────────────────

run_block1() {
  local status
  status=$(get_state_field "block1" "status")

  if [[ "$status" == "completed" ]]; then
    echo ">>> Blok 1 sudah selesai. Lewati."
    echo "    Untuk menjalankan ulang, hapus state: rm $STATE_FILE"
    return
  fi

  echo "========================================================"
  echo "  BLOK 1: Kondisi A → B (${N_REPS} rep masing-masing)"
  echo "========================================================"

  check_rendered_configs
  check_pmu_verdict

  local ts_start
  ts_start=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  set_state_field "block1" "status"     "running"
  set_state_field "block1" "started_at" "$ts_start"

  # A→B
  run_condition "none"   1
  run_condition "static" 1

  local ts_end
  ts_end=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  set_state_field "block1" "status"       "completed"
  set_state_field "block1" "completed_at" "$ts_end"

  echo ""
  echo "========================================================"
  echo "  BLOK 1 SELESAI pada $ts_end"
  echo "  Jalankan analisis sementara (opsional, n=15 per kondisi):"
  echo "    python3 scripts/analyze_results.py --results-dir /mnt/experiment-data/results"
  echo ""
  echo "  Lanjutkan dengan Blok 2 BESOK:"
  echo "    bash scripts/run_full_experiment.sh block2"
  echo "========================================================"
}

# ─────────────────────────────────────────────────────────────────────────────
# BLOK 2: urutan B → A (hari berbeda)
# ─────────────────────────────────────────────────────────────────────────────

run_block2() {
  local blk1_status blk2_status
  blk1_status=$(get_state_field "block1" "status")
  blk2_status=$(get_state_field "block2" "status")

  if [[ "$blk1_status" != "completed" ]]; then
    echo "FATAL: Blok 1 belum selesai (status: $blk1_status)." >&2
    echo "       Jalankan dulu: bash scripts/run_full_experiment.sh block1" >&2
    exit 1
  fi

  if [[ "$blk2_status" == "completed" ]]; then
    echo ">>> Blok 2 sudah selesai. Lewati."
    echo "    Jalankan analisis final:"
    echo "      python3 scripts/analyze_results.py --results-dir /mnt/experiment-data/results"
    return
  fi

  check_different_day

  echo "========================================================"
  echo "  BLOK 2: Kondisi B → A (${N_REPS} rep masing-masing)"
  echo "  (urutan dibalik dari Blok 1 untuk counterbalancing)"
  echo "========================================================"

  check_rendered_configs
  check_pmu_verdict

  local ts_start
  ts_start=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  set_state_field "block2" "status"     "running"
  set_state_field "block2" "started_at" "$ts_start"

  # B→A (dibalik dari Blok 1)
  run_condition "static" 2
  run_condition "none"   2

  local ts_end
  ts_end=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  set_state_field "block2" "status"       "completed"
  set_state_field "block2" "completed_at" "$ts_end"

  echo ""
  echo "========================================================"
  echo "  BLOK 2 SELESAI pada $ts_end"
  echo "  Total: 30 rep per kondisi per instance siap dianalisis."
  echo ""
  echo "  Jalankan analisis final (termasuk uji efek urutan):"
  echo "    python3 scripts/analyze_results.py \\"
  echo "      --results-dir /mnt/experiment-data/results"
  echo "========================================================"
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

init_state

case "$BLOCK_CMD" in
  block1)
    run_block1
    ;;
  block2)
    run_block2
    ;;
  status)
    print_status
    ;;
  *)
    echo "Penggunaan: $0 <block1|block2|status> [--force-same-day]" >&2
    exit 1
    ;;
esac
