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
    └── analyze_results.py             # Mann-Whitney+Bonferroni, korelasi per-kondisi, effect size RQ4
```

## Keputusan Desain Kunci (dan Alasannya)

1. **Sequential, bukan paralel** — lisensi Gurobi WLS akademik dibatasi 2 sesi
   konkuren. `run_experiment.sh` menjalankan satu Pod solver pada satu waktu,
   menunggu selesai sepenuhnya (`Succeeded` + log + metrik diambil) sebelum
   memulai run berikutnya. Checkout lisensi WLS juga di-retry otomatis
   (backoff linear, 3 percobaan) di `run_solver.py` untuk menahan kegagalan
   transient jaringan ke server lisensi pada ratusan run sekuensial.

2. **Pemisahan fase barrier/crossover via callback Gurobi, bukan parsing log
   teks** — log Gurobi membulatkan timestamp ke detik (`Xs`), terlalu kasar
   untuk crossover yang bisa berlangsung sub-detik. `run_solver.py` memakai
   `GRB.Callback.RUNTIME` di callback `BARRIER` dan `SIMPLEX` untuk presisi
   tinggi. **`Method=2` diset eksplisit** agar barrier dipakai murni (bukan
   concurrent), supaya asumsi "callback SIMPLEX pertama = awal crossover"
   tetap valid. Parsing log teks tetap dijalankan sebagai cross-check sekunder
   saja — run dengan selisih besar antar kedua sumber ditandai
   `phase_timing_discrepancy_warning` dan **dikeluarkan otomatis** dari
   statistik utama oleh `analyze_results.py` sampai diperiksa manual.

3. **Context-switch diukur khusus pada rentang fase crossover, bukan seluruh
   siklus hidup proses** — `run_solver.py` mencatat `optimize_start_epoch_unix`
   (wall-clock) plus batas waktu fase crossover dari callback Gurobi.
   `collect_system_metrics.py` melakukan sampling kontinu (default tiap 50ms)
   terhadap `involuntary_ctxt_switches`, lalu memotong delta-nya tepat pada
   rentang fase crossover saja (`involuntary_ctxt_switches_delta_crossover_only`).
   Tanpa ini, delta context-switch akan didominasi aktivitas fase barrier
   (jauh lebih banyak iterasi), mengaburkan korelasi yang ingin diuji pada
   Rumusan Masalah poin 2. Field versi whole-process tetap disimpan untuk
   transparansi/audit, tapi tidak dipakai pada uji statistik.

4. **Resource Pod identik di kedua kondisi** — `pod-template.yaml` sama
   persis untuk Kondisi A dan B; satu-satunya pembeda adalah `cpuManagerPolicy`
   di level kubelet node. Ini krusial agar perbandingan valid (lihat
   `run_experiment.sh`, ada validasi otomatis yang akan GAGAL jika Anda lupa
   `switch_cpu_manager_policy.sh` sebelum menjalankan kondisi yang salah).

5. **Drain-stop-hapus state-ganti config-start-uncordon saat berganti
   kebijakan** — bukan sekadar edit file lalu restart. Tanpa menghapus
   `/var/lib/kubelet/cpu_manager_state`, kubelet bisa mempertahankan state
   alokasi CPU dari kebijakan sebelumnya dan menyebabkan hasil tidak valid.

6. **Metrik sistem dikumpulkan dari HOST, bukan dari dalam container** —
   `collect_system_metrics.py` butuh akses ke `/proc/<pid>/status` milik
   proses container dan ke path cgroup v2 node, yang TIDAK terlihat dari
   dalam Pod tanpa privileged access. Dijalankan sebagai proses terpisah di
   VM, dikoordinasikan oleh `run_experiment.sh`. Pencarian PID container
   dilakukan dua langkah via `crictl` (`crictl pods --name` untuk sandbox ID,
   baru `crictl ps --pod <sandbox_id>` untuk container) — `crictl ps --pod`
   mengharapkan sandbox ID, BUKAN nama Pod Kubernetes secara langsung.

7. **Statistik mengikuti persis Subbab "Analisis Data" di Metode
   Penelitian** — `analyze_results.py` menerapkan koreksi Bonferroni pada uji
   Mann-Whitney U (RQ1, karena diuji terpisah per instance), menghitung
   korelasi Spearman **terpisah per kondisi** (RQ2, menghindari confound
   Simpson's paradox dari penggabungan lintas kondisi), menguji stabilitas
   fase barrier (RQ3), dan menghitung effect size rank-biserial antar
   instance (RQ4) — bukan hanya melaporkan signifikan/tidak signifikan.

8. **Robustness sesi Gurobi** — `model.optimize()` di `run_solver.py`
   dibungkus `try/except/finally`: kegagalan di tengah solve tetap menulis
   JSON kegagalan minimal (supaya run tercatat, bukan menghilang), Pod tetap
   exit dengan kode non-zero (supaya `kubectl wait` mendeteksinya sebagai
   `Failed`, bukan diam-diam `Succeeded`), dan `dispose()` SELALU terpanggil
   supaya sesi lisensi WLS tidak menggantung dan menghalangi run berikutnya.

## Keterbatasan yang Diwarisi dari Diskusi Metode

- vCPU Compute Engine = 1 hyperthread, bukan 1 physical core penuh (lihat
  bagian "Keterbatasan Metodologis" di proposal).
- VM tetap berbagi physical host dengan tenant GCP lain di luar kendali
  eksperimen — mitigasi: repetisi (N=15) + pelaporan median/IQR (sudah
  diimplementasi di `analyze_results.py`), bukan rata-rata tunggal.
- Resolusi penyelarasan context-switch ke fase crossover dibatasi oleh
  interval polling `collect_system_metrics.py` (default 50ms) — untuk
  instance dengan crossover yang jauh lebih singkat dari itu, delta yang
  terhitung tetap merupakan APROKSIMASI, bukan nilai eksak.
- Involuntary context switches tetap PROKSI untuk migrasi thread, bukan
  pengukuran langsung perpindahan antar-core (lihat Batasan Masalah poin 5
  di proposal) — context switch bisa terjadi tanpa migrasi (dilanjutkan di
  core yang sama).
- `download_benchmarks.sh` SENGAJA tidak diisi URL instance asli — Anda harus
  memverifikasi & memilih instance dari https://plato.asu.edu/bench.html
  secara manual, karena URL individual berubah tergantung suite benchmark
  aktif dan saya tidak ingin menebak/mengarang URL yang mungkin sudah usang.

## Yang Masih Perlu Anda Lakukan Manual

- [ ] Generate Academic WLS license di Gurobi User Portal
- [ ] Pilih & verifikasi **minimal 5 instance** benchmark dari plato.asu.edu
      (variasi ukuran & struktur sparsity, lihat Subbab "Objek Uji" di
      proposal), isi ke `scripts/download_benchmarks.sh` DAN samakan nama
      filenya di array `INSTANCES` pada `scripts/run_experiment.sh`
- [ ] Request kenaikan kuota CPU GCP jika region pilihan Anda quotanya < 8
- [ ] Pastikan `crictl` terinstal & terkonfigurasi (`SETUP_GUIDE.md` 2.6)
      sebelum menjalankan `run_experiment.sh` — tanpa ini,
      `collect_system_metrics.py` akan gagal
- [ ] Jalankan smoke test (1 repetisi) sebelum batch penuh N=15 — lihat
      `SETUP_GUIDE.md` Bagian 5
