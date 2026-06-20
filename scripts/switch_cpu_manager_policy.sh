#!/usr/bin/env bash
# switch_cpu_manager_policy.sh — Mengganti kebijakan CPU Manager kubelet dengan
# AMAN, mengikuti prosedur resmi: drain node -> stop kubelet -> hapus state
# file lama -> ganti config -> start kubelet -> uncordon.
#
# PERINGATAN: skrip ini akan mengevict SEMENTARA semua Pod di node (karena
# cluster ini single-node, berarti seluruh workload eksperimen Anda berhenti
# sesaat). Pada cluster single-node tanpa node lain, drain hanya akan berhasil
# jika tidak ada DaemonSet/Pod tanpa toleransi eviction yang menghalangi;
# gunakan --ignore-daemonsets sesuai contoh di bawah.
#
# Penggunaan:
#   ./switch_cpu_manager_policy.sh none      # pindah ke Kondisi A
#   ./switch_cpu_manager_policy.sh static    # pindah ke Kondisi B

set -euo pipefail

POLICY="${1:-}"
if [[ "$POLICY" != "none" && "$POLICY" != "static" ]]; then
  echo "Penggunaan: $0 <none|static>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_SRC="$SCRIPT_DIR/../kubelet-configs/condition-$([ "$POLICY" == "none" ] && echo "A-none" || echo "B-static").yaml"
KUBELET_CONFIG_DEST="/var/lib/kubelet/config.yaml"
STATE_FILE="/var/lib/kubelet/cpu_manager_state"
NODE_NAME="$(hostname)"

echo ">>> Berpindah ke kebijakan CPU Manager: $POLICY"
echo ">>> Sumber config: $CONFIG_SRC"

if [[ ! -f "$CONFIG_SRC" ]]; then
  echo "FATAL: file config sumber tidak ditemukan: $CONFIG_SRC" >&2
  exit 1
fi

echo ">>> [1/6] Draining node $NODE_NAME (mengevict Pod aktif sementara)..."
kubectl drain "$NODE_NAME" --ignore-daemonsets --delete-emptydir-data --force --timeout=120s || {
  echo "PERINGATAN: drain gagal/timeout. Periksa Pod yang masih tersisa dengan:" >&2
  echo "  kubectl get pods --all-namespaces -o wide --field-selector spec.nodeName=$NODE_NAME" >&2
  exit 1
}

echo ">>> [2/6] Menghentikan kubelet..."
sudo systemctl stop kubelet

echo ">>> [3/6] Menghapus state file CPU Manager lama (wajib saat ganti policy)..."
if [[ -f "$STATE_FILE" ]]; then
  sudo cp "$STATE_FILE" "${STATE_FILE}.bak.$(date +%s)"  # backup untuk audit, bukan untuk dipulihkan
  sudo rm -f "$STATE_FILE"
else
  echo "    (tidak ada state file sebelumnya, lanjut)"
fi

echo ">>> [4/6] Mengganti $KUBELET_CONFIG_DEST..."
sudo cp "$KUBELET_CONFIG_DEST" "${KUBELET_CONFIG_DEST}.bak.$(date +%s)"
sudo cp "$CONFIG_SRC" "$KUBELET_CONFIG_DEST"

echo ">>> [5/6] Menyalakan kembali kubelet..."
sudo systemctl start kubelet

echo ">>> Menunggu kubelet siap (10s)..."
sleep 10
sudo systemctl is-active --quiet kubelet || {
  echo "FATAL: kubelet gagal start. Cek: sudo journalctl -u kubelet -n 100 --no-pager" >&2
  exit 1
}

echo ">>> [6/6] Uncordon node..."
kubectl uncordon "$NODE_NAME"

echo ">>> Verifikasi kebijakan aktif:"
if [[ -f "$STATE_FILE" ]]; then
  cat "$STATE_FILE"
else
  echo "    (state file belum dibuat — normal jika belum ada Pod Guaranteed yang dijadwalkan)"
fi

echo ">>> Selesai. Kebijakan CPU Manager sekarang: $POLICY"
