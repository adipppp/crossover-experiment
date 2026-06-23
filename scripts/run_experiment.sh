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
# Contoh (N=15 sesuai Metode Penelitian — lihat justifikasi jumlah repetisi di sana):
#   ./run_experiment.sh none 15 3
#   ./run_experiment.sh static 15 3

set -euo pipefail

CONDITION="${1:-}"
N_REPS="${2:-15}"
CPU_COUNT="${3:-3}"

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
# Minimal LIMA instance dengan variasi ukuran & struktur sparsity, sesuai
# Subbab "Objek Uji" di Metode Penelitian — daftar di bawah CONTOH STRUKTUR,
# isi nama file sesuai instance yang benar-benar Anda pilih dan unduh.
INSTANCES=(
  "neos3.mps"
  "L1_sixm1000obs.mps"
  "Linf_520c.mps"
  "cont1.mps"
  "cont11.mps"
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

# Verifikasi dukungan perf stat untuk hardware counters (PMU Virtualization)
echo ">>> Memverifikasi dukungan hardware performance counters di host..."
if command -v perf >/dev/null 2>&1 || which perf >/dev/null 2>&1; then
  if sudo perf stat -e cache-misses sleep 0.1 2>&1 | grep -q "<not supported>"; then
    echo "⚠️ PERINGATAN: Hardware performance counters tidak didukung/di-expose oleh hypervisor VM ini."
    echo "   Pengumpulan data cache-misses/L1-dcache di sysmetrics akan kosong."
    echo "   (Metrik utama context switch tetap dapat berjalan via procfs)."
  else
    echo ">>> [OK] Hardware performance counters didukung oleh host."
  fi
else
  echo "⚠️ PERINGATAN: 'perf' tidak terinstall di host. Pemasangan 'linux-tools-common' diperlukan untuk metrik hardware."
fi
echo ""

CONSECUTIVE_FAILURES=0

for instance in "${INSTANCES[@]}"; do
  instance_basename="${instance%%.*}"
  
  # --- TAMBAHAN ---
  # Sanitize basename untuk nama K8s: ubah huruf ke lowercase dan ganti '_' menjadi '-'
  sanitized_basename=$(echo "$instance_basename" | tr '[:upper:]' '[:lower:]' | tr '_' '-')

  for rep in $(seq -w 1 "$N_REPS"); do
    # Gunakan sanitized_basename di run_id agar Pod memenuhi standar RFC 1123
    run_id="${CONDITION}-${sanitized_basename}-run${rep}"
    pod_name="solver-${run_id}"

    echo ""
    echo "---------------------------------------------------------------"
    echo ">>> RUN: $run_id"
    echo "---------------------------------------------------------------"

    if [[ -s "$RESULTS_DIR/${run_id}.sysmetrics.json" && -s "$RESULTS_DIR/${run_id}.podlog.txt" && -s "$RESULTS_DIR/${run_id}.json" ]]; then
      echo ">>> RUN $run_id sudah memiliki hasil utuh. Melewati (skip)..."
      continue
    fi

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

    # Tunggu sampai container benar-benar mulai (bukan hanya scheduled), DENGAN
    # TIMEOUT — sebelumnya loop ini tidak punya batas waktu, sehingga Pod yang
    # gagal start (mis. ImagePullBackOff) membuat skrip hang selamanya.
    POD_START_TIMEOUT_SECONDS=120
    POLL_TICK_SECONDS=0.5
    MAX_TICKS=$(( POD_START_TIMEOUT_SECONDS * 2 ))  # karena tiap tick = 0.5s
    ticks=0
    until kubectl get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].state.running}' 2>/dev/null | grep -q "startedAt"; do
      sleep "$POLL_TICK_SECONDS"
      ticks=$((ticks + 1))
      if (( ticks >= MAX_TICKS )); then
        echo "FATAL: Pod $pod_name tidak Running dalam ${POD_START_TIMEOUT_SECONDS}s. Cek manual:" >&2
        echo "  kubectl describe pod $pod_name -n $NAMESPACE" >&2
        kubectl delete pod "$pod_name" -n "$NAMESPACE" --wait=false --ignore-not-found=true
        rm -f "$rendered_manifest"
        exit 1
      fi
    done

    metrics_output="$RESULTS_DIR/${run_id}.sysmetrics.json"
    python3 "$SCRIPT_DIR/collect_system_metrics.py" \
      --pod-name "$pod_name" \
      --container-name solver \
      --run-id "$run_id" \
      --results-dir "$RESULTS_DIR" \
      --poll-interval 0.05 \
      --output "$metrics_output" &
    METRICS_PID=$!
    
    # Pin metrics collector to core 0 (system reserved) to prevent interference with solver
    taskset -cp 0 $METRICS_PID >/dev/null 2>&1 || true

    echo ">>> Menunggu Pod selesai (solve berjalan)..."
    TIMEOUT=1800
    ELAPSED=0
    while true; do
        PHASE=$(kubectl get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")

        if [[ "$PHASE" == "Succeeded" ]]; then
            break
    	elif [[ "$PHASE" == "Failed" ]]; then
            echo "FATAL: Pod $pod_name berstatus Failed (kemungkinan OOMKilled)." >&2
            echo ">>> Menunggu 300 detik agar server Gurobi WLS menghapus token (default 5 menit)..." >&2
            sleep 300
            break
        fi

        if (( ELAPSED >= TIMEOUT )); then
            echo "PERINGATAN: Timeout 1800s tercapai untuk $pod_name." >&2
            break
        fi
        sleep 5
        ELAPSED=$((ELAPSED + 5))
    done

    # Ambil hasil JSON dari tmpfs Pod menggunakan kubectl cp sebelum Pod dihapus
    echo ">>> Mengambil file hasil JSON dari tmpfs Pod..."
    RUN_SUCCESS=false
    if kubectl cp "$NAMESPACE/$pod_name:/app/results/${run_id}.json" "$RESULTS_DIR/${run_id}.json" 2> "$RESULTS_DIR/${run_id}.cp_err.log"; then
      touch "$RESULTS_DIR/${run_id}.cp_done"
      RUN_SUCCESS=true
    else
      echo "PERINGATAN: Gagal menyalin file hasil JSON menggunakan kubectl cp. Mencoba metode fallback (kubectl exec -- cat)..." >&2
      if kubectl exec "$pod_name" -n "$NAMESPACE" -- cat "/app/results/${run_id}.json" > "$RESULTS_DIR/${run_id}.json" 2> "$RESULTS_DIR/${run_id}.cat_err.log"; then
        echo ">>> [OK] Berhasil menyalin file hasil JSON menggunakan metode fallback."
        touch "$RESULTS_DIR/${run_id}.cp_done"
        rm -f "$RESULTS_DIR/${run_id}.cp_err.log"
        rm -f "$RESULTS_DIR/${run_id}.cat_err.log"
        RUN_SUCCESS=true
      else
        echo "PERINGATAN: Metode fallback juga gagal. File hasil JSON tidak ditemukan atau rusak." >&2
      fi
    fi

    # Tunggu proses monitoring background ikut selesai (PID sudah exit di dalamnya).
    wait "$METRICS_PID" || echo "PERINGATAN: collect_system_metrics.py keluar dengan error untuk $run_id"

    # Bersihkan file sentinel
    rm -f "$RESULTS_DIR/${run_id}.cp_done"

    echo ">>> Mengambil log dan hasil JSON dari Pod (sebelum dihapus)..."
    kubectl logs "$pod_name" -n "$NAMESPACE" > "$RESULTS_DIR/${run_id}.podlog.txt" 2>&1 || true

    echo ">>> Menghapus Pod $pod_name (membersihkan sebelum run berikutnya)..."
    kubectl delete pod "$pod_name" -n "$NAMESPACE" --wait=true --timeout=60s

    rm -f "$rendered_manifest"

    # Bersihkan L3 Cache dan Page Cache di Host di antara run pengujian
    echo ">>> Mengosongkan host page cache & syncing..."
    sync
    echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null 2>&1 || true
    sleep 2

    # Hentikan jika kegagalan beruntun terjadi secara sistematis
    if [[ "$RUN_SUCCESS" == "true" ]]; then
      CONSECUTIVE_FAILURES=0
    else
      CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
      echo "⚠️ Terdeteksi kegagalan run. Jumlah kegagalan beruntun: ${CONSECUTIVE_FAILURES}/3"
      if (( CONSECUTIVE_FAILURES >= 3 )); then
        echo "FATAL: Terjadi kegagalan eksperimen secara berturut-turut sebanyak 3 kali." >&2
        echo "       Menghentikan seluruh orkestrasi untuk menghemat lisensi dan waktu." >&2
        exit 1
      fi
    fi

    # --- TAMBAHAN FIX LISENSI ---
    echo ">>> Menunggu 10 detik agar server Gurobi WLS merilis token lisensi..."
    sleep 10
    # ----------------------------

    echo ">>> RUN $run_id selesai."
  done
done

echo ""
echo "================================================================"
echo " SELESAI: semua repetisi untuk kondisi=$CONDITION sudah dijalankan."
echo " Hasil tersimpan di: $RESULTS_DIR"
echo "================================================================"
