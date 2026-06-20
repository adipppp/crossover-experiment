#!/usr/bin/env bash
# run_experiment.sh — Orkestrasi utama: menjalankan seluruh repetisi eksperimen
# untuk SATU kondisi (none ATAU static) secara sequential.
#
# Sequential WAJIB (bukan pilihan desain semata) karena lisensi Gurobi WLS
# akademik dibatasi maksimum 2 sesi konkuren — menjalankan banyak Pod solver
# paralel berisiko gagal checkout lisensi di tengah eksperimen.
#
# Penggunaan:
#   ./run_experiment.sh <none|static> <jumlah_repetisi> <cpu_count>
# Contoh:
#   ./run_experiment.sh none 10 7
#   ./run_experiment.sh static 10 7

set -euo pipefail

CONDITION="${1:-}"
N_REPS="${2:-10}"
CPU_COUNT="${3:-7}"

if [[ "$CONDITION" != "none" && "$CONDITION" != "static" ]]; then
  echo "Penggunaan: $0 <none|static> <jumlah_repetisi> <cpu_count>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_TEMPLATE="$SCRIPT_DIR/../manifests/pod-template.yaml"
INSTANCES_DIR="/mnt/experiment-data/instances"
RESULTS_DIR="/mnt/experiment-data/results"
NAMESPACE="crossover-experiment"

mkdir -p "$RESULTS_DIR"

# Daftar instance benchmark Mittelmann yang diuji. Sesuaikan dengan instance
# yang sudah Anda unduh ke $INSTANCES_DIR (lihat scripts/download_benchmarks.sh).
INSTANCES=(
  "neos3.mps.gz"
  "L1_sixm1000obs.mps.gz"
  "Linf_520c.mps.gz"
)

echo "================================================================"
echo " EKSPERIMEN: kondisi=$CONDITION | repetisi=$N_REPS | cpu=$CPU_COUNT"
echo " Instance: ${INSTANCES[*]}"
echo "================================================================"

# Validasi pra-syarat: pastikan kebijakan CPU Manager node SESUAI dengan
# kondisi yang diminta, supaya tidak salah jalankan (mis. lupa switch policy).
ACTUAL_POLICY=$(cat /var/lib/kubelet/config.yaml | grep -A0 "cpuManagerPolicy:" | awk '{print $2}' || echo "unknown")
if [[ "$ACTUAL_POLICY" != "$CONDITION" ]]; then
  echo "FATAL: kebijakan CPU Manager node saat ini adalah '$ACTUAL_POLICY', bukan '$CONDITION'." >&2
  echo "       Jalankan dulu: ./switch_cpu_manager_policy.sh $CONDITION" >&2
  exit 1
fi

for instance in "${INSTANCES[@]}"; do
  instance_basename="${instance%%.*}"

  for rep in $(seq -w 1 "$N_REPS"); do
    run_id="${CONDITION}-${instance_basename}-run${rep}"
    pod_name="solver-${run_id}"

    echo ""
    echo "---------------------------------------------------------------"
    echo ">>> RUN: $run_id"
    echo "---------------------------------------------------------------"

    # Render manifest Pod dari template dengan placeholder substitution.
    rendered_manifest="/tmp/${pod_name}.yaml"
    sed \
      -e "s/__RUN_ID__/${run_id}/g" \
      -e "s/__CONDITION__/${CONDITION}/g" \
      -e "s/__INSTANCE_FILE__/${instance}/g" \
      -e "s/__CPU_COUNT__/${CPU_COUNT}/g" \
      "$MANIFEST_TEMPLATE" > "$rendered_manifest"

    echo ">>> Menjalankan Pod $pod_name..."
    kubectl apply -f "$rendered_manifest"

    # Mulai monitoring metrik sistem di BACKGROUND, baru setelah Pod terlihat Running.
    echo ">>> Menunggu Pod Running..."
    kubectl wait --for=condition=PodScheduled "pod/$pod_name" -n "$NAMESPACE" --timeout=60s
    # Tunggu sampai container benar-benar mulai (bukan hanya scheduled),
    # supaya PID container sudah ada saat collect_system_metrics.py mencarinya.
    until kubectl get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].state.running}' 2>/dev/null | grep -q "startedAt"; do
      sleep 0.5
    done

    metrics_output="$RESULTS_DIR/${run_id}.sysmetrics.json"
    python3 "$SCRIPT_DIR/collect_system_metrics.py" \
      --pod-name "$pod_name" \
      --container-name solver \
      --namespace "$NAMESPACE" \
      --output "$metrics_output" &
    METRICS_PID=$!

    echo ">>> Menunggu Pod selesai (solve berjalan)..."
    kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$pod_name" -n "$NAMESPACE" --timeout=1800s || {
      echo "PERINGATAN: Pod $pod_name tidak Succeeded dalam batas waktu. Cek manual:" >&2
      echo "  kubectl describe pod $pod_name -n $NAMESPACE" >&2
      echo "  kubectl logs $pod_name -n $NAMESPACE" >&2
    }

    # Tunggu proses monitoring background ikut selesai (PID sudah exit di dalamnya).
    wait "$METRICS_PID" || echo "PERINGATAN: collect_system_metrics.py keluar dengan error untuk $run_id"

    echo ">>> Mengambil log dan hasil JSON dari Pod (sebelum dihapus)..."
    kubectl logs "$pod_name" -n "$NAMESPACE" > "$RESULTS_DIR/${run_id}.podlog.txt" 2>&1 || true

    echo ">>> Menghapus Pod $pod_name (membersihkan sebelum run berikutnya)..."
    kubectl delete pod "$pod_name" -n "$NAMESPACE" --wait=true --timeout=60s

    rm -f "$rendered_manifest"

    echo ">>> RUN $run_id selesai."
  done
done

echo ""
echo "================================================================"
echo " SELESAI: semua repetisi untuk kondisi=$CONDITION sudah dijalankan."
echo " Hasil tersimpan di: $RESULTS_DIR"
echo "================================================================"
