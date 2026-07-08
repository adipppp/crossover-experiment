# Implementasi Eksperimen: CPU Pinning x Crossover LP Solver di Kubernetes

Implementasi pendukung skripsi "Pengaruh CPU Pinning terhadap Performa Fase
Crossover pada LP Solver di Lingkungan Kubernetes". Lihat `SETUP_GUIDE.md`
untuk langkah eksekusi lengkap dari nol (gcloud → kubeadm → eksperimen).

## Struktur Proyek


```

crossover-experiment/
├── SETUP_GUIDE.md                    # Panduan step-by-step gcloud → kubeadm → eksperimen (BACA INI DULU)
├── Dockerfile                        # Image solver berbasis gurobi/python
├── infra/                            # Output Phase 1 — dibuat oleh skrip, bukan diedit manual
│   ├── quota-check-report.json       # Hasil check_quota.sh
│   ├── topology-report.json          # Hasil characterize_topology.py
│   ├── topology-decision.txt         # Keputusan manual: option1 / option2 / option3
│   ├── pmu-validation-report.json    # Hasil validate_pmu_fidelity.py
│   ├── experiment-state.json         # State blok (run_full_experiment.sh)
│   └── pmu_validation/
│       └── pmu_bench.c               # Micro-benchmark untuk validasi PMU
├── manifests/
│   ├── 00-namespace.yaml             # Namespace terisolasi untuk eksperimen
│   ├── 01-secret-template.yaml       # Template struktur Secret
│   ├── 02-storage.yaml               # PV/PVC hostPath untuk instance & hasil
│   └── pod-template.yaml             # Template Pod, di-render run_experiment.sh
├── kubelet-configs/
│   ├── condition-A-none.yaml         # TEMPLATE Kondisi A (belum berisi reservedSystemCPUs)
│   ├── condition-B-static.yaml       # TEMPLATE Kondisi B (belum berisi reservedSystemCPUs)
│   └── rendered/                     # Output render_kubelet_configs.py — GUNAKAN INI
│       ├── condition-A-none.yaml     # Config final Kondisi A (sudah berisi reservedSystemCPUs)
│       ├── condition-B-static.yaml   # Config final Kondisi B (sudah berisi reservedSystemCPUs)
│       └── render-manifest.json      # Audit trail render
└── scripts/
    ├── check_quota.sh                # [LAPTOP] Cek kuota GCP dan ajukan kenaikan
    ├── characterize_topology.py      # [VM] Analisis sibling pair, pilih core, rekomendasikan opsi
    ├── validate_pmu_fidelity.py      # [VM] Uji fidelitas hardware performance counter (go/no-go)
    ├── render_kubelet_configs.py     # [VM] Render template kubelet → kubelet-configs/rendered/
    ├── run_solver.py                 # [CONTAINER] Solve + catat timing fase barrier/crossover
    ├── collect_system_metrics.py     # [HOST] Pantau context-switch, PMU, throttling per fase
    ├── switch_cpu_manager_policy.sh  # Drain → stop → hapus state → tukar config → start → uncordon
    ├── run_experiment.sh             # Orkestrasi N rep × M instance untuk SATU kondisi (idempoten)
    ├── run_full_experiment.sh        # Orkestrasi DUA BLOK lengkap (block counterbalancing)
    ├── download_benchmarks.sh        # Unduh arsip Mittelmann (.bz2 → .mps)
    └── analyze_results.py            # Mann-Whitney+Bonferroni, order-effect, throttling sensitivity,
                                      # MDE, Spearman per kondisi, IPC, rank-biserial effect size

```

## Keputusan Desain Kunci (dan Alasannya)

1. **Sequential, bukan paralel** — Lisensi Gurobi WLS akademik dibatasi 2 sesi konkuren. `run_experiment.sh` didesain untuk berjalan sekuensial dengan cooldown 30 detik antar run (300 detik jika Pod gagal/OOMKilled, untuk mematuhi token lifespan WLS).

2. **Standardisasi Nama Pod (RFC 1123)** — Instance Mittelmann menggunakan huruf besar dan underscore (mis. `L1_sixm1000obs.mps`). Skrip orkestrasi men-sanitize ke lowercase + hyphen untuk nama Pod, tanpa mengubah nama file asli.

3. **Pemisahan fase barrier/crossover via callback Gurobi** — `run_solver.py` memakai `GRB.Callback.RUNTIME` pada callback `BARRIER` dan `SIMPLEX`. `Method=2` memaksa barrier murni sehingga transisi ke simplex (= awal crossover) jelas dan terukur secara presisi.

4. **Karakterisasi topologi dulu, baru konfigurasi** — Tidak ada `--threads-per-core=1` yang dibuat-buat di command VM secara default. `characterize_topology.py` membaca `lscpu -p` untuk memetakan sibling pair aktual, memilih 4 solver CPU + 1 reserved CPU secara otomatis dengan kontaminasi sibling minimal, lalu mencetak rekomendasi terstruktur. Keputusan akhir (Option 1/2/3) selalu dikonfirmasi manual sebelum `render_kubelet_configs.py` menulis `reservedSystemCPUs` ke config kubelet. VM default adalah `c2-standard-8` dengan SMT aktif (8 vCPU); recreate dengan `--threads-per-core=1` atau eskalasi ke `c2-standard-16` hanya jika topologi mengharuskan.

5. **Alokasi CPU: 4 solver + 1 reserved dari 8 vCPU** — `resources.requests.cpu = resources.limits.cpu = 4` (Guaranteed QoS) dan `Threads=4` pada Gurobi — identik di kedua kondisi untuk menghilangkan tingkat paralelisme sebagai confounder. `reservedSystemCPUs` meng-pin 1 core untuk daemon sistem, memisahkannya dari pool eksklusif solver.

6. **Drain-stop-hapus state-ganti config-start-uncordon** — Tanpa menghapus `/var/lib/kubelet/cpu_manager_state`, kubelet dapat mempertahankan alokasi core lama atau gagal inisialisasi. `switch_cpu_manager_policy.sh` melakukan seluruh siklus ini secara otomatis dan membaca dari `kubelet-configs/rendered/` (bukan template mentah).

7. **Block counterbalancing dua sesi** — `run_full_experiment.sh` mengimplementasikan dua blok: Blok 1 (A→B, hari ke-1) dan Blok 2 (B→A, hari ke-2). Efek urutan diuji via Mann-Whitney U agregat (A-blk1 vs A-blk2, B-blk1 vs B-blk2) sebelum analisis utama. State blok disimpan di `infra/experiment-state.json`; skrip menolak Blok 2 jika dijalankan hari yang sama dengan Blok 1.

8. **Analisis statistik sesuai proposal** — `analyze_results.py` menerapkan: Bonferroni-corrected Mann-Whitney U per instance (RQ1); post-hoc MDE rank-biserial; throttling sensitivity re-run (RQ1 dengan/tanpa run ter-throttle); korelasi Spearman per kondisi untuk context-switch, cache-miss rate, dan IPC (RQ2, menghindari Simpson's Paradox); barrier stability check (RQ3); rank-biserial effect size per instance (RQ4).

## Keterbatasan yang Diwarisi dari Diskusi Metode

- **vCPU = hyperthread, bukan physical core penuh.** Pada `c2-standard-8` dengan SMT aktif (Option 1), 8 vCPU memetakan ke 4 physical core (2 thread per core). Dengan kebutuhan 4 solver CPU + 1 reserved, kontaminasi sibling antara reserved CPU dan salah satu solver CPU tidak terhindarkan secara matematis. `characterize_topology.py` mendokumentasikan pasangan sibling mana yang terdampak; kondisi ini dilaporkan eksplisit di bagian Keterbatasan Metodologis laporan akhir.
- **Noise residual co-location cloud.** Node bersifat single-tenant di level Pod, namun VM berbagi physical host dengan penyewa GCP lain. Mitigasi: pelaporan **median + IQR** dari 30 repetisi, `--maintenance-policy=TERMINATE` untuk mencegah live migration tak terdeteksi.
- **Isolasi core Kubernetes ≠ isolasi kernel penuh.** `reservedSystemCPUs` membatasi scheduler decisions, bukan timer interrupt atau softirq yang tetap bisa membebani core "eksklusif". Ini merepresentasikan skenario cloud-native nyata di mana peneliti tidak memiliki akses ke `isolcpus`/`nohz_full`.
- **Proksi, bukan pengukuran langsung migrasi.** Involuntary context switches mengindikasikan preemption CFS, bukan perpindahan core secara spesifik. Cache-miss rate dan IPC dari `perf stat` digunakan sebagai triangulasi hardware-level. Validitas PMU dikonfirmasi oleh `validate_pmu_fidelity.py` sebelum eksperimen; jika NO-GO, hanya context switches yang dilaporkan.
- **Hubungan asosiatif, bukan kausal.** Analisis mediasi formal (bootstrapped indirect effect) tidak dilakukan — diklasifikasikan sebagai penelitian lanjutan.

## Yang Perlu Anda Lakukan Manual

**Sebelum VM dibuat (di laptop):**
- [ ] Generate Academic WLS license di Gurobi User Portal.
- [ ] Jalankan `bash scripts/check_quota.sh` — cek kuota CPU dan ajukan kenaikan jika perlu.

**Setelah VM dibuat, sebelum setup Kubernetes:**
- [ ] Jalankan `python3 scripts/characterize_topology.py` — baca output dan rekomendasi.
- [ ] Tetapkan keputusan topologi: `echo "option1" > infra/topology-decision.txt`
  (atau `option2`/`option3` jika topologi mengharuskan — lihat §1.3.1/§1.3.2 SETUP_GUIDE.md).
- [ ] Jalankan `python3 scripts/validate_pmu_fidelity.py` — catat verdict (GO/DEGRADED/NO-GO).
- [ ] Jalankan `python3 scripts/render_kubelet_configs.py` — hasilkan `kubelet-configs/rendered/`.

**Setup Kubernetes:**
- [ ] Gunakan `kubelet-configs/rendered/condition-A-none.yaml` (bukan template mentah) pada §2.7.
- [ ] Install `bzip2` dan kompilasi `emps.c` (lihat SETUP_GUIDE.md §2.1).
- [ ] Ubah nama/konfirmasi instance LP pada `download_benchmarks.sh`.

**Eksperimen:**
- [ ] Blok 1: `bash scripts/run_full_experiment.sh block1`
- [ ] Blok 2 (hari berbeda): `bash scripts/run_full_experiment.sh block2`
- [ ] Analisis final: `python3 scripts/analyze_results.py --results-dir /mnt/experiment-data/results`
