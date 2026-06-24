# Implementasi Eksperimen: CPU Pinning x Crossover LP Solver di Kubernetes

Implementasi pendukung skripsi "Pengaruh CPU Pinning terhadap Performa Fase
Crossover pada LP Solver di Lingkungan Kubernetes". Lihat `SETUP_GUIDE.md`
untuk langkah eksekusi lengkap dari nol (gcloud → kubeadm → eksperimen).

## Struktur Proyek


```

crossover-experiment/
├── SETUP_GUIDE.md              # Panduan step-by-step gcloud + kubeadm (BACA INI DULU)
├── Dockerfile                  # Image solver berbasis gurobi/python
├── manifests/
│   ├── 00-namespace.yaml       # Namespace terisolasi untuk eksperimen
│   ├── 01-secret-template.yaml # Template struktur Secret
│   ├── 02-storage.yaml         # PV/PVC hostPath untuk instance & hasil
│   └── pod-template.yaml       # Template Pod, di-render scripts/run_experiment.sh
├── kubelet-configs/
│   ├── condition-A-none.yaml   # Config kubelet Kondisi A (baseline CFS)
│   └── condition-B-static.yaml # Config kubelet Kondisi B (CPU Manager static)
└── scripts/
├── run_solver.py                  # Dijalankan DI DALAM Pod: solve + catat timing
├── collect_system_metrics.py      # Dijalankan DI HOST: pantau context-switch & throttling
├── switch_cpu_manager_policy.sh   # Drain -> stop kubelet -> ganti config -> start -> uncordon
├── run_experiment.sh              # Orkestrasi N repetisi x M instance (Idempoten)
├── download_benchmarks.sh         # Unduh arsip Mittelmann valid (.bz2 ke .mps)
└── analyze_results.py             # Uji Mann-Whitney, Bonferroni, Spearman korelasi

```

## Keputusan Desain Kunci (dan Alasannya)

1. **Sequential, bukan paralel** — Lisensi Gurobi WLS akademik dibatasi 2 sesi konkuren. `run_experiment.sh` didesain untuk berjalan sekuensial. Skrip memiliki perlindungan *cooldown*: jika proses sebelumnya berjalan normal, jeda 10 detik diberikan. Namun jika Pod mengalami kegagalan (seperti `OOMKilled`), skrip memberikan jeda 300 detik (5 menit) untuk mematuhi durasi *token lifespan* WLS Gurobi guna menghindari *error* kelebihan sesi.

2. **Standardisasi Nama Pod (RFC 1123)** — Berbagai *instance* dari Mittlemann menggunakan huruf besar dan *underscore* (mis. `L1_sixm1000obs.mps`). Skrip orkestrasi memiliki *sanitizer* yang mengubah nama tersebut menjadi *lowercase* dan *hyphen* untuk injeksi ke dalam *manifest* Kubernetes agar pod tidak ditolak saat dijadwalkan, tanpa mengubah format file asli di penyimpanan.

3. **Pemisahan fase barrier/crossover via callback Gurobi** — `run_solver.py` memakai `GRB.Callback.RUNTIME` pada *callback* `BARRIER` dan `SIMPLEX` untuk presisi waktu tinggi. **`Method=2`** diset eksplisit agar Gurobi menggunakan *barrier* murni, sehingga transisi ke *simplex* (crossover) menjadi jelas.

4. **Kapasitas vs Alokasi Eksperimen (3 vCPU)** — Eksperimen dilakukan pada VM `c2-standard-8` dengan `--threads-per-core=1`, sehingga guest OS melihat **4 vCPU** (1 thread per physical core). Kubelet mereservasi **1 CPU** (`systemReserved=500m` + `kubeReserved=500m`), sehingga **3 vCPU** tersedia sebagai *Allocatable* untuk Pod solver. Skrip dieksekusi dengan parameter **3 vCPU** untuk Gurobi *solver*.

5. **Drain-stop-hapus state-ganti config-start-uncordon** — Bukan sekadar mengedit file lalu *restart*. Tanpa menghapus `/var/lib/kubelet/cpu_manager_state`, kubelet bisa mempertahankan alokasi core dari sebelumnya, atau justru merusak inisialisasi jika reservasi `systemReserved` berubah.

6. **Statistik mengikuti Metode Penelitian** — `analyze_results.py` mengaplikasikan koreksi Bonferroni untuk uji Mann-Whitney U, menghitung korelasi Spearman secara terpisah pada tiap Kondisi (menghindari efek *Simpson's Paradox*), dan memvalidasi independensi *noise* fase barrier.

## Keterbatasan yang Diwarisi dari Diskusi Metode

- vCPU Compute Engine = 1 hyperthread, bukan 1 physical core penuh.
- Meskipun node bersifat *single-tenant*, VM tetap berbagi *physical host* dengan VM penyewa GCP lain, sehingga mitigasi berupa pelaporan **median dan IQR** pada repetisi diterapkan.
- *Involuntary context switches* berperan sebagai proksi kuantitatif, bukan metrik absolut lokasi sirkuit *core*.

## Yang Perlu Anda Lakukan Manual

- [ ] Generate Academic WLS license di Gurobi User Portal.
- [ ] Install `bzip2` dan kompilasi `emps.c` (lihat SETUP_GUIDE.md).
- [ ] Ubah nama/konfirmasi *instance* LP pada `download_benchmarks.sh`.
- [ ] Pastikan Node berada di Kondisi A (`none`) sebelum menjalankan *baseline*.
