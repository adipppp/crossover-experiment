#!/usr/bin/env bash
# run_experiment.sh — Orkestrasi utama: menjalankan seluruh repetisi eksperimen
# untuk SATU kondisi (none ATAU static) secara sequential.
#
# Sequential WAJIB (bukan pilihan desain semata) karena lisensi Gurobi WLS
# akademik dibatasi maksimum 2 sesi konkuren — menjalankan banyak Pod solver
# paralel berisiko gagal checkout lisensi di tengah eksperimen.
#
# Penggunaan:
#   ./run_experiment.sh <none|static> <jumlah_repetisi> <cpu_count> [blok]
# Contoh (N=15, Blok 1 — lihat justifikasi jumlah repetisi di Metode Penelitian):
#   ./run_experiment.sh none 15 4 1
#   ./run_experiment.sh static 15 4 1
# Contoh (N=15, Blok 2 — hari berbeda, urutan terbalik via run_full_experiment.sh):
#   ./run_experiment.sh static 15 4 2
#   ./run_experiment.sh none 15 4 2
#
# cpu_count=4 sesuai proposal: resources.requests.cpu = resources.limits.cpu = 4
# (Guaranteed QoS) dan Threads=4 pada solver Gurobi.
# blok default=1. Dipropagasi ke run_id (misal: none-neos3-blk1-run01) agar
# Blok 2 tidak menimpa hasil Blok 1 via mekanisme skip-if-exists.

set -euo pipefail

CONDITION="${1:-}"
N_REPS="${2:-15}"
CPU_COUNT="${3:-4}"
BLOCK="${4:-1}"

if [[ "$CONDITION" != "none" && "$CONDITION" != "static" ]]; then
  echo "Penggunaan: $0 <none|static> <jumlah_repetisi> <cpu_count> [blok]" >&2
  exit 1
fi

if ! [[ "$BLOCK" =~ ^[0-9]+$ ]] || (( BLOCK < 1 )); then
  echo "FATAL: blok harus bilangan bulat >= 1 (dapat: $BLOCK)" >&2
  exit 1
fi

# ====================================================================
# [WRAPPER] SELF-RE-EXECUTING AUTO-RESUME
# ====================================================================
if [[ "${_AUTO_RESUME:-0}" != "1" ]]; then
    export _AUTO_RESUME=1
    MAX_RETRIES=5
    ATTEMPT=1
    
    if ! command -v jq &> /dev/null; then
        echo "FATAL: 'jq' wajib di-install." >&2
        exit 1
    fi
    
    trap 'echo ">>> Interupsi (Ctrl+C). Membersihkan zombie..."; pkill -f "collect_system_metrics.py" || true; kubectl delete pods -l app=crossover-solver -n crossover-experiment --force --grace-period=0 2>/dev/null || true; exit 130' SIGINT SIGTERM

    while (( ATTEMPT <= MAX_RETRIES )); do
        kubectl delete pods -l app=crossover-solver -n crossover-experiment --force --grace-period=0 2>/dev/null || true
        pkill -f "collect_system_metrics.py" || true

        echo ">>> [AUTO-RESUME] Memulai eksekusi (Attempt $ATTEMPT/$MAX_RETRIES)..."
        set +e
        bash "$0" "$@"
        EXIT_CODE=$?
        set -e

        if [[ $EXIT_CODE -eq 0 ]]; then
            echo ">>> [AUTO-RESUME] Seluruh eksekusi sukses 100%!"
            exit 0
        else
            if (( ATTEMPT == MAX_RETRIES )); then
                echo "FATAL: Batas percobaan ($MAX_RETRIES x) tercapai. Eksperimen dihentikan."
                exit 1
            fi
            echo ">>> [AUTO-RESUME] Ditemukan kegagalan (Exit: $EXIT_CODE). Menunggu 30s sebelum retry..."
            # FIX pasca-audit: catat setiap re-eksekusi skrip penuh ke jejak persisten.
            # jq dijamin tersedia di titik ini -- skrip sudah hard-fail di awal
            # (blok _AUTO_RESUME di atas) kalau jq tidak terinstal.
            RESULTS_DIR_FOR_LOG="/mnt/experiment-data/results"
            mkdir -p "$RESULTS_DIR_FOR_LOG" 2>/dev/null || true
            printf '%s\n' "$(jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg cond "${1:-}" --arg blok "${4:-1}" --argjson attempt "$ATTEMPT" --argjson exit_code "$EXIT_CODE" '{timestamp_utc:$ts, condition:$cond, block:$blok, attempt_that_failed:$attempt, exit_code:$exit_code, reason:"whole_script_auto_resume"}')" >> "$RESULTS_DIR_FOR_LOG/retry_log.jsonl" 2>/dev/null || true
            sleep 30
            ATTEMPT=$((ATTEMPT + 1))
        fi
    done
    exit 1
fi

TOTAL_FAILURES=0

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

# Verifikasi ketersediaan semua file instance
for instance in "${INSTANCES[@]}"; do
  if [[ ! -f "$INSTANCES_DIR/$instance" ]]; then
    echo "FATAL: File instance benchmark '$INSTANCES_DIR/$instance' tidak ditemukan." >&2
    exit 1
  fi
done

echo "================================================================"
echo " EKSPERIMEN: kondisi=$CONDITION | blok=$BLOCK | repetisi=$N_REPS | cpu=$CPU_COUNT"
echo " Instance: ${INSTANCES[*]}"
echo "================================================================"

# Validasi pra-syarat: pastikan kebijakan CPU Manager node SESUAI dengan
# kondisi yang diminta, supaya tidak salah jalankan (mis. lupa switch policy).
# Catatan: jika cpuManagerPolicy TIDAK ditulis di config.yaml (kubeadm tidak
# selalu menulisnya), kebijakan efektif adalah "none" (default Kubernetes).
ACTUAL_POLICY=$(sudo grep "cpuManagerPolicy:" /var/lib/kubelet/config.yaml 2>/dev/null | awk '{print $2}')
ACTUAL_POLICY="${ACTUAL_POLICY:-none}"   # default Kubernetes jika field tidak hadir
if [[ "$ACTUAL_POLICY" != "$CONDITION" ]]; then
  echo "FATAL: kebijakan CPU Manager node saat ini adalah '$ACTUAL_POLICY', bukan '$CONDITION'." >&2
  echo "       Jalankan dulu: bash scripts/switch_cpu_manager_policy.sh $CONDITION" >&2
  exit 1
fi

# Secondary check: verify kubelet is live and the node condition is Ready
NODE_NAME=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -n "$NODE_NAME" ]]; then
  if ! kubectl wait --for=condition=Ready "node/$NODE_NAME" --timeout=60s >/dev/null 2>&1; then
    echo "FATAL: Node tidak Ready — kubelet mungkin gagal start setelah policy switch." >&2
    exit 1
  fi
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
    run_id="${CONDITION}-${sanitized_basename}-blk${BLOCK}-run${rep}"
    pod_name="solver-${run_id}"

    echo ""
    echo "---------------------------------------------------------------"
    echo ">>> RUN: $run_id"
    echo "---------------------------------------------------------------"

    if [[ -s "$RESULTS_DIR/${run_id}.sysmetrics.json" && -s "$RESULTS_DIR/${run_id}.podlog.txt" && -s "$RESULTS_DIR/${run_id}.json" ]]; then
      if jq -e . "$RESULTS_DIR/${run_id}.json" >/dev/null 2>&1 && jq -e . "$RESULTS_DIR/${run_id}.sysmetrics.json" >/dev/null 2>&1; then
        echo ">>> RUN $run_id sudah memiliki hasil utuh dan valid. Melewati (skip)..."
        continue
      else
        echo "PERINGATAN: Artefak corrupt untuk $run_id. Menghapus untuk retry..."
        # FIX pasca-audit: catat retry ke jejak persisten SEBELUM menghapus,
        # supaya jumlah retry per run_id dapat direkonstruksi dari hasil akhir
        # (sebelumnya hilang total begitu artefak dihapus -- lihat keterbatasan
        # yang didokumentasikan di Bab Metode Subbab 3.5).
        printf '%s\n' "$(jq -nc --arg run_id "$run_id" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg reason "corrupt_artifact_on_skip_check" --arg cond "$CONDITION" --arg blok "$BLOCK" '{run_id:$run_id, timestamp_utc:$ts, reason:$reason, condition:$cond, block:$blok}')" >> "$RESULTS_DIR/retry_log.jsonl"
        rm -f "$RESULTS_DIR/${run_id}".*
      fi
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
    START_TIME=$SECONDS
    # CATATAN: containerStatuses hanya mencakup main container (bukan initContainers).
    # Selama stage-instance (initContainer) berjalan, jsonpath ini mengembalikan
    # string kosong -> loop terus menunggu. Loop baru keluar saat solver container
    # benar-benar Running -- perilaku yang kita inginkan.
    until kubectl get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].state.running}' 2>/dev/null | grep -q "startedAt"; do
      sleep "$POLL_TICK_SECONDS"
      ELAPSED=$(( SECONDS - START_TIME ))
      if (( ELAPSED >= POD_START_TIMEOUT_SECONDS )); then
        echo "FATAL: Pod $pod_name tidak Running dalam ${POD_START_TIMEOUT_SECONDS}s. Cek manual:" >&2
        echo "  kubectl describe pod $pod_name -n $NAMESPACE" >&2
        kubectl delete pod "$pod_name" -n "$NAMESPACE" --wait=false --ignore-not-found=true
        rm -f "$rendered_manifest"
        exit 1
      fi
    done

    # Pin metrics collector to reserved core to prevent interference with solver.
    #
    # FIX KRITIS (audit pasca-eksperimen): versi sebelumnya diam-diam fallback ke
    # CPU 0 jika topology-report.json tidak terbaca. Untuk topologi final (8
    # physical core, SMT off), reserved_cpu adalah core DI LUAR core_selection.solver_cpus
    # (lihat characterize_topology.py), sedangkan CPU 0 adalah salah satu dari 4
    # core yang justru dialokasikan EKSKLUSIF ke solver pada Kondisi B. Silent
    # fallback ke CPU 0 berarti metrics collector bisa taskset ke core solver itu
    # sendiri -- karena isolasi cpuset K8s CPU Manager `static` HANYA mencegah Pod
    # lain masuk, BUKAN proses host biasa di luar kubepods.slice seperti skrip ini.
    # Kalau ini terjadi, setiap siklus polling (tiap poll-interval) memaksa
    # preemption thread solver di core itu -- involuntary context switch BUATAN,
    # bukan efek CPU pinning yang sesungguhnya diukur (persis metrik utama RQ2).
    #
    # Karena itu, sekarang HARD-FAIL kalau reserved_cpu tidak terbaca, alih-alih
    # melanjutkan dengan asumsi yang berisiko salah secara diam-diam.
    RESERVED_CPU=""
    TOPOLOGY_REPORT="$SCRIPT_DIR/../infra/topology-report.json"
    SOLVER_CPUS_JSON=""
    if [ -f "$TOPOLOGY_REPORT" ]; then
      RESERVED_CPU=$(python3 -c "import json; print(json.load(open('$TOPOLOGY_REPORT'))['core_selection']['reserved_cpu'])" 2>/dev/null)
      SOLVER_CPUS_JSON=$(python3 -c "import json; print(json.dumps(json.load(open('$TOPOLOGY_REPORT'))['core_selection']['solver_cpus']))" 2>/dev/null)
    fi
    if [ -z "$RESERVED_CPU" ]; then
      echo "FATAL: Gagal membaca reserved_cpu dari $TOPOLOGY_REPORT." >&2
      echo "       TIDAK melanjutkan dengan fallback CPU 0 -- itu berisiko sama dengan" >&2
      echo "       core yang dialokasikan eksklusif ke solver pada Kondisi B, yang akan" >&2
      echo "       mencemari pengukuran involuntary context switches (RQ2) secara diam-diam." >&2
      echo "       Jalankan scripts/characterize_topology.py terlebih dahulu untuk" >&2
      echo "       menghasilkan $TOPOLOGY_REPORT, lalu ulangi." >&2
      # Pod sudah terlanjur dijalankan (kubectl apply di atas) sebelum titik ini;
      # bersihkan sebelum keluar supaya tidak menyisakan Pod menganggur.
      kubectl delete pod "$pod_name" -n "$NAMESPACE" --wait=false --ignore-not-found=true
      rm -f "$rendered_manifest"
      exit 1
    fi
    # Sanity check tambahan: reserved_cpu tidak boleh tumpang tindih dengan
    # solver_cpus yang SESUNGGUHNYA (dibaca langsung dari topology-report.json,
    # BUKAN dari CPU_COUNT -- CPU_COUNT adalah argumen jumlah core solver yang
    # diminta di kubelet/manifest, bukan daftar CPU ID solver_cpus itu sendiri,
    # sehingga membandingkan reserved_cpu terhadap rentang 0..CPU_COUNT-1 bisa
    # keliru kalau skema penomoran core_selection tidak dimulai dari 0 secara
    # berurutan seperti pada topologi saat ini).
    if [ -n "$SOLVER_CPUS_JSON" ]; then
      OVERLAP=$(python3 -c "import json,sys; print('1' if $RESERVED_CPU in json.loads('$SOLVER_CPUS_JSON') else '0')" 2>/dev/null)
      if [ "$OVERLAP" = "1" ]; then
        echo "FATAL: reserved_cpu ($RESERVED_CPU) tumpang tindih dengan solver_cpus ($SOLVER_CPUS_JSON)." >&2
        echo "       Periksa kembali $TOPOLOGY_REPORT dan render_kubelet_configs.py." >&2
        kubectl delete pod "$pod_name" -n "$NAMESPACE" --wait=false --ignore-not-found=true
        rm -f "$rendered_manifest"
        exit 1
      fi
    else
      echo "PERINGATAN: solver_cpus tidak terbaca dari $TOPOLOGY_REPORT -- sanity-check overlap dilewati (reserved_cpu=$RESERVED_CPU tetap dipakai)." >&2
    fi

    metrics_output="$RESULTS_DIR/${run_id}.sysmetrics.json"
    taskset -c "$RESERVED_CPU" python3 "$SCRIPT_DIR/collect_system_metrics.py" \
      --pod-name "$pod_name" \
      --container-name solver \
      --run-id "$run_id" \
      --results-dir "$RESULTS_DIR" \
      --poll-interval 0.05 \
      --output "$metrics_output" &
    METRICS_PID=$!

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

    # Tunggu proses monitoring background ikut selesai (PID sudah exit di dalamnya).
    wait "$METRICS_PID" || echo "PERINGATAN: collect_system_metrics.py keluar dengan error untuk $run_id"

    echo ">>> Mengambil log dan hasil JSON dari Pod (sebelum dihapus)..."
    kubectl logs "$pod_name" -n "$NAMESPACE" > "$RESULTS_DIR/${run_id}.podlog.txt" 2>&1 || true

    echo ">>> Menghapus Pod $pod_name (membersihkan sebelum run berikutnya)..."
    if ! kubectl delete pod "$pod_name" -n "$NAMESPACE" --wait=true --timeout=60s 2>/dev/null; then
       echo ">>> Memaksa penghapusan Pod yang stuck..."
       kubectl delete pod "$pod_name" -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true
    fi

    rm -f "$rendered_manifest"

    # Bersihkan L3 Cache dan Page Cache di Host di antara run pengujian
    echo ">>> Mengosongkan host page cache & syncing..."
    sync
    echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null 2>&1 || true
    sleep 2

    RUN_SUCCESS=false
    if [[ -s "$RESULTS_DIR/${run_id}.json" && -s "$RESULTS_DIR/${run_id}.sysmetrics.json" && -s "$RESULTS_DIR/${run_id}.podlog.txt" ]]; then
      if jq -e . "$RESULTS_DIR/${run_id}.json" >/dev/null 2>&1 && jq -e . "$RESULTS_DIR/${run_id}.sysmetrics.json" >/dev/null 2>&1; then
        echo ">>> [OK] Seluruh artefak utuh dan tervalidasi."
        RUN_SUCCESS=true
      fi
    fi

    # Hentikan jika kegagalan beruntun terjadi secara sistematis
    if [[ "$RUN_SUCCESS" == "true" ]]; then
      CONSECUTIVE_FAILURES=0
    else
      TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
      CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
      echo "PERINGATAN: Artefak tidak lengkap/korup untuk $run_id. Menghapus jejak untuk retry..."
      # FIX pasca-audit: sama seperti skip-check di atas, catat dulu sebelum hapus.
      printf '%s\n' "$(jq -nc --arg run_id "$run_id" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg reason "incomplete_after_execution" --arg cond "$CONDITION" --arg blok "$BLOCK" --argjson consecutive "$CONSECUTIVE_FAILURES" '{run_id:$run_id, timestamp_utc:$ts, reason:$reason, condition:$cond, block:$blok, consecutive_failures:$consecutive}')" >> "$RESULTS_DIR/retry_log.jsonl"
      rm -f "$RESULTS_DIR/${run_id}".*
      echo "⚠️ Terdeteksi kegagalan run. Jumlah kegagalan beruntun: ${CONSECUTIVE_FAILURES}/3"
      if (( CONSECUTIVE_FAILURES >= 3 )); then
        echo "FATAL: Terjadi kegagalan eksperimen secara berturut-turut sebanyak 3 kali." >&2
        echo "       Menghentikan seluruh orkestrasi untuk menghemat lisensi dan waktu." >&2
        exit 1
      fi
    fi

    # --- TAMBAHAN FIX LISENSI ---
    echo ">>> Menunggu 30 detik agar server Gurobi WLS merilis token lisensi..."
    sleep 30
    # ----------------------------

    echo ">>> RUN $run_id selesai."
  done
done

echo ""
echo "================================================================"
echo " SELESAI: semua repetisi untuk kondisi=$CONDITION sudah dijalankan."
echo " Hasil tersimpan di: $RESULTS_DIR"
echo "================================================================"

if (( TOTAL_FAILURES > 0 )); then
  echo ">>> Terdapat $TOTAL_FAILURES kegagalan pada sesi ini. Meminta retry dari Parent..."
  exit 2
fi
exit 0
