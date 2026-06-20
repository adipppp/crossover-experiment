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
│   ├── 01-secret-template.yaml # Template struktur Secret (JANGAN diisi & apply langsung)
│   ├── 02-storage.yaml         # PV/PVC hostPath untuk instance & hasil
│   └── pod-template.yaml       # Template Pod, di-render scripts/run_experiment.sh
├── kubelet-configs/
│   ├── condition-A-none.yaml   # Config kubelet Kondisi A (baseline CFS)
│   └── condition-B-static.yaml # Config kubelet Kondisi B (CPU Manager static)
└── scripts/
    ├── run_solver.py                  # Dijalankan DI DALAM Pod: solve + catat timing
    ├── collect_system_metrics.py      # Dijalankan DI HOST: pantau context-switch & throttling
    ├── switch_cpu_manager_policy.sh   # Drain -> stop kubelet -> ganti config -> start -> uncordon
    ├── run_experiment.sh              # Orkestrasi N repetisi x M instance, SEQUENTIAL
    ├── download_benchmarks.sh         # Unduh instance Mittelmann (URL perlu diisi manual)
    └── analyze_results.py             # Statistik deskriptif + Mann-Whitney U + korelasi
```

## Keputusan Desain Kunci (dan Alasannya)

1. **Sequential, bukan paralel** — lisensi Gurobi WLS akademik dibatasi 2 sesi
   konkuren. `run_experiment.sh` menjalankan satu Pod solver pada satu waktu,
   menunggu selesai sepenuhnya (`Succeeded` + log + metrik diambil) sebelum
   memulai run berikutnya.

2. **Pemisahan fase barrier/crossover via callback Gurobi, bukan parsing log
   teks** — log Gurobi membulatkan timestamp ke detik (`Xs`), terlalu kasar
   untuk crossover yang bisa berlangsung sub-detik. `run_solver.py` memakai
   `GRB.Callback.RUNTIME` di callback `BARRIER` dan `SIMPLEX`untuk presisi
   tinggi. **`Method=2` diset eksplisit** agar barrier dipakai murni (bukan
   concurrent), supaya asumsi "callback SIMPLEX pertama = awal crossover"
   tetap valid. Parsing log teks tetap dijalankan sebagai cross-check sekunder
   saja (lihat `phase_timing_discrepancy_warning` di output JSON).

3. **Resource Pod identik di kedua kondisi** — `pod-template.yaml` sama
   persis untuk Kondisi A dan B; satu-satunya pembeda adalah `cpuManagerPolicy`
   di level kubelet node. Ini krusial agar perbandingan valid (lihat
   `run_experiment.sh`, ada validasi otomatis yang akan GAGAL jika Anda lupa
   `switch_cpu_manager_policy.sh` sebelum menjalankan kondisi yang salah).

4. **Drain-stop-hapus state-ganti config-start-uncordon saat berganti
   kebijakan** — bukan sekadar edit file lalu restart. Tanpa menghapus
   `/var/lib/kubelet/cpu_manager_state`, kubelet bisa mempertahankan state
   alokasi CPU dari kebijakan sebelumnya dan menyebabkan hasil tidak valid.

5. **Metrik sistem dikumpulkan dari HOST, bukan dari dalam container** —
   `collect_system_metrics.py` butuh akses ke `/proc/<pid>/status` milik
   proses container dan ke path cgroup v2 node, yang TIDAK terlihat dari
   dalam Pod tanpa privileged access. Dijalankan sebagai proses terpisah di
   VM, dikoordinasikan oleh `run_experiment.sh`.

## Keterbatasan yang Diwarisi dari Diskusi Metode

- vCPU Compute Engine = 1 hyperthread, bukan 1 physical core penuh (lihat
  bagian "Keterbatasan Metodologis" di proposal).
- VM tetap berbagi physical host dengan tenant GCP lain di luar kendali
  eksperimen — mitigasi: repetisi + pelaporan median/IQR (sudah diimplementasi
  di `analyze_results.py`), bukan rata-rata tunggal.
- `download_benchmarks.sh` SENGAJA tidak diisi URL instance asli — Anda harus
  memverifikasi & memilih instance dari https://plato.asu.edu/bench.html
  secara manual, karena URL individual berubah tergantung suite benchmark
  aktif dan saya tidak ingin menebak/mengarang URL yang mungkin sudah usang.

## Yang Masih Perlu Anda Lakukan Manual

- [ ] Generate Academic WLS license di Gurobi User Portal
- [ ] Pilih & verifikasi instance benchmark dari plato.asu.edu, isi ke
      `scripts/download_benchmarks.sh`
- [ ] Request kenaikan kuota CPU GCP jika region pilihan Anda quotanya < 8
- [ ] Jalankan smoke test (1 repetisi) sebelum batch penuh — lihat
      `SETUP_GUIDE.md` Bagian 5
