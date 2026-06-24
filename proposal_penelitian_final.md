# Proposal Penelitian

## Pengaruh CPU Pinning terhadap Performa Fase Crossover pada LP Solver di Lingkungan Kubernetes

---

## Pendahuluan

### 1.1 Latar Belakang

Metode interior-point digunakan secara luas untuk menyelesaikan permasalahan optimasi linear programming (LP) berskala besar karena efisiensi komputasinya. Namun, solusi yang dihasilkan oleh metode ini umumnya berupa solusi interior atau nonbasic, sementara banyak aplikasi LP justru lebih mensyaratkan solusi basic [7]. Untuk menjembatani perbedaan ini, solver LP pada umumnya menerapkan fase crossover setelah fase barrier, yang berfungsi mengonversi solusi interior menjadi solusi basic yang valid.

Permasalahannya, fase crossover dikenal sensitif terhadap cache locality dan migrasi thread [4][6][9], sehingga jika tidak dikonfigurasi dengan baik, crossover berpotensi menjadi bottleneck yang justru lebih besar daripada fase barrier itu sendiri [7]. Hal ini terjadi karena crossover bersifat sangat sekuensial dan memiliki kemampuan terbatas untuk memanfaatkan paralelisme multi-core, berbeda dengan fase barrier maupun metode first-order seperti PDHG yang dapat dijalankan secara paralel [13]. Kondisi ini makin terasa seiring meningkatnya tingkat paralelisme fase barrier, yang membuat kontribusi relatif waktu crossover terhadap total waktu eksekusi turut meningkat [13]. Pada beberapa instance benchmark, crossover bahkan tercatat memakan hingga 96% dari total waktu solver [11][12]. Crossover, dengan demikian, bukan lagi sekadar tahap pelengkap, melainkan dapat menjadi penentu utama performa solver LP secara keseluruhan.

Di sisi lain, perkembangan cloud-native infrastructure mendorong semakin banyak workload saintifik untuk dijalankan pada Kubernetes [14]. Untuk mengakomodasi workload seperti crossover yang sensitif terhadap locality, Kubernetes menyediakan konfigurasi CPU management policy. Secara default, kebijakan ini diatur ke `none`, yang berarti penjadwalan CPU sepenuhnya diserahkan kepada Completely Fair Scheduler (CFS) bawaan Linux, sehingga thread suatu container dapat berpindah dari satu core ke core lain dari satu time slice ke time slice berikutnya. Ketika kebijakan diubah menjadi `static`, container pada pod dengan kelas Guaranteed QoS dan permintaan CPU bertipe integer akan dialokasikan sejumlah core secara eksklusif melalui mekanisme cgroup `cpuset`, alih-alih hanya mendapat kuota waktu CPU dari CFS [4]. Core yang telah dialokasikan secara eksklusif tersebut dikeluarkan dari shared pool dan tidak dapat dipakai oleh container lain, sehingga thread yang berjalan di dalamnya tidak akan dipindah-pindah (migrasi) oleh scheduler kernel ke core lain selama masa hidup container tersebut [3][4]. Tersedianya mekanisme ini membuka peluang untuk mengurangi overhead migrasi thread yang menjadi sumber utama bottleneck pada fase crossover.

Efektivitas CPU pinning dan thread affinity dalam meningkatkan performa solver telah didukung oleh sejumlah penelitian terdahulu secara umum. Studi mengenai CPU pinning melaporkan bahwa migrasi proses dapat menimbulkan overhead melalui cache miss, akses memori berulang, interrupt reestablishment, dan context switching [6]. Pada konteks containerized HPC, studi lain juga menunjukkan bahwa penerapan affinity policy menghasilkan kinerja yang lebih baik dibandingkan baseline tanpa affinity [10]. Temuan-temuan ini menguatkan asumsi bahwa CPU pinning dapat menjadi solusi yang relevan untuk masalah migrasi thread pada workload yang sensitif terhadap locality.

Akan tetapi, manfaat CPU pinning tersebut belum tentu dapat divalidasi secara langsung pada konteks crossover LP solver di Kubernetes. Studi-studi mengenai CPU pinning yang ada selama ini lebih banyak mengevaluasi performa pada platform virtualisasi atau workload HPC secara umum [6][10], bukan pada karakteristik spesifik fase crossover. Sebaliknya, penelitian mengenai crossover sendiri sebagian besar berfokus pada pengembangan algoritma [7][13], tanpa menyentuh aspek interaksinya dengan infrastruktur container dan mekanisme penjadwalan CPU di Kubernetes. Akibatnya, terdapat gap penelitian yang jelas: belum ada kajian empiris yang secara spesifik mengukur overhead migrasi thread pada fase crossover LP solver di lingkungan Kubernetes, sekaligus membedakannya dari efek throttling atau *policy* resource lain.

Gap ini penting untuk diisi karena bagi praktisi yang menjalankan solver optimisasi di dalam container, kejelasan mengenai pengaruh CPU pinning terhadap fase crossover sangat menentukan keputusan konfigurasi di lingkungan produksi. Tanpa bukti empiris yang spesifik, praktisi tidak memiliki dasar yang cukup untuk menentukan apakah CPU pinning benar-benar diperlukan demi memperoleh performa solver yang stabil dan mendekati bare-metal, atau apakah upaya konfigurasi tersebut justru tidak sepadan dengan overhead operasionalnya [3][6][10].

### 1.2 Rumusan Masalah

Rumusan masalah dalam penelitian ini adalah sebagai berikut.

1. Bagaimana pengaruh CPU pinning terhadap waktu eksekusi fase crossover pada LP solver di Kubernetes?
2. Apakah penurunan waktu crossover berkorelasi dengan berkurangnya involuntary context switches dan migrasi thread?
3. Apakah fase barrier relatif stabil antar konfigurasi, sehingga perbedaan performa terutama berasal dari fase crossover?
4. Seberapa besar kontribusi kebijakan CPU Manager terhadap variasi performa solver pada instance LP benchmark?

### 1.3 Tujuan Penelitian

Berdasarkan rumusan masalah di atas, tujuan penelitian ini adalah sebagai berikut.

1. Mengukur pengaruh CPU pinning terhadap waktu eksekusi fase crossover pada LP solver di Kubernetes.
2. Menganalisis korelasi antara penurunan waktu crossover dengan berkurangnya involuntary context switches dan migrasi thread.
3. Mengevaluasi stabilitas fase barrier antar konfigurasi CPU Manager, untuk memastikan bahwa perbedaan performa yang teramati benar-benar berasal dari fase crossover.
4. Mengukur besar kontribusi kebijakan CPU Manager terhadap variasi performa solver pada berbagai instance LP benchmark.

### 1.4 Manfaat Penelitian

**Manfaat Teoritis.** Penelitian ini memberikan kontribusi berupa pengukuran empiris yang memisahkan efek CPU pinning dari efek lain dalam Kubernetes (seperti throttling dan kebijakan resource lainnya), khususnya pada fase crossover solver LP — sebuah area yang belum banyak diteliti secara spesifik (lihat Subbab 1.1). Dengan demikian, kontribusi ilmiahnya tidak hanya terletak pada evaluasi performa solver, tetapi juga pada pemahaman mekanisme sistem operasi dan orkestrasi container terhadap karakteristik workload optimisasi yang bersifat sekuensial.

**Manfaat Praktis.** Hasil penelitian ini diharapkan dapat menjadi dasar rekomendasi konfigurasi Kubernetes bagi praktisi yang menjalankan solver optimisasi di lingkungan container, khususnya dalam menentukan apakah CPU pinning benar-benar diperlukan untuk memperoleh performa yang stabil dan mendekati bare-metal pada workload mathematical optimization yang sensitif terhadap locality dan latency.

### 1.5 Batasan Masalah

Penelitian ini dibatasi pada lingkup berikut.

1. Perbandingan kebijakan CPU Manager hanya mencakup `none` (CFS default) dan `static`; konfigurasi lain seperti integrasi dengan Topology Manager atau NUMA awareness tidak termasuk dalam lingkup penelitian ini.
2. Solver yang digunakan hanya Gurobi Optimizer dengan lisensi Academic WLS; perbandingan dengan solver open-source lain (mis. HiGHS, SoPlex) tidak dilakukan.
3. Eksperimen dilaksanakan pada satu klaster Kubernetes single-node di atas Virtual Machine Google Compute Engine (8 vCPU); skenario multi-node atau infrastruktur bare-metal dedicated tidak termasuk dalam lingkup penelitian ini.
4. Objek uji hanya berupa instance LP murni dari koleksi benchmark Mittelmann; instance MILP atau kelas masalah optimisasi lainnya tidak dicakup.
5. Migrasi thread diukur secara tidak langsung melalui involuntary context switches sebagai proksi, bukan melalui pengukuran perpindahan antar-core secara langsung (mis. tracing event migrasi kernel).

---

## Metode Penelitian

### Desain Eksperimen

Penelitian ini menggunakan pendekatan eksperimen empiris terkontrol dengan desain *within-subject*, yaitu membandingkan dua kebijakan CPU Manager pada satu unit infrastruktur yang sama untuk mengeliminasi variabel perancu yang muncul apabila kedua kondisi diuji pada perangkat keras yang berbeda. Dua kondisi yang dibandingkan adalah:

1. **Kondisi A (baseline):** CPU Manager dengan kebijakan `none`, yaitu penjadwalan CPU diserahkan penuh kepada Completely Fair Scheduler (CFS) bawaan Linux.
2. **Kondisi B (perlakuan):** CPU Manager dengan kebijakan `static`, yaitu container pada pod dengan kelas Guaranteed QoS dialokasikan CPU secara eksklusif melalui mekanisme `cpuset`.

Kedua kondisi diuji secara bergantian pada node Kubernetes yang sama, dengan kebijakan CPU Manager diubah melalui modifikasi `kubelet-config.yaml` diikuti restart layanan kubelet di antara dua sesi pengujian.

### Infrastruktur Eksperimen

Eksperimen dilaksanakan pada satu Virtual Machine Google Compute Engine dengan spesifikasi 8 vCPU (tipe *compute-optimized*, mis. `c2-standard-8`), dibiayai melalui kredit *free trial* Google Cloud Platform. Pemilihan instance tunggal dengan kuota 8 vCPU ini sejalan dengan batas penggunaan *concurrent* Compute Engine pada akun *Free Trial* GCP.

Di atas VM tersebut dibangun klaster Kubernetes *single-node* menggunakan kubeadm (bukan layanan terkelola seperti GKE), dengan pertimbangan utama: (a) kebutuhan kontrol penuh atas konfigurasi kubelet untuk mengubah `cpuManagerPolicy` dan merestart layanan kapan pun diperlukan tanpa proses persetujuan administratif; dan (b) menghindari biaya tambahan di luar kuota *free trial* yang melekat pada layanan klaster terkelola. *Taint* bawaan pada node *control-plane* dihapus agar Pod beban kerja dapat dijadwalkan pada node tunggal tersebut.

Konfigurasi sumber daya pada kedua kondisi disusun sebagai berikut: satu vCPU dicadangkan untuk proses sistem dan daemon Kubernetes melalui parameter `kubeReserved`, menyisakan tujuh vCPU yang dapat dialokasikan ke Pod beban kerja. Pada Kondisi B, Pod solver didefinisikan dengan `resources.requests.cpu` sama dengan `resources.limits.cpu` dalam nilai integer, sebagai syarat agar Pod memenuhi kelas Guaranteed QoS dan berhak atas alokasi CPU eksklusif oleh kebijakan `static`.

### Perangkat Lunak dan Parameter Solver

Solver yang digunakan adalah **Gurobi Optimizer**, dengan lisensi *Academic Web License Service* (WLS). Karena lisensi akademik ini dibatasi maksimum dua sesi konkuren, seluruh pengujian dijalankan secara sekuensial — satu Pod solver pada satu waktu — sehingga tidak ada kontensi lisensi yang dapat mengacaukan pengukuran waktu.

Dua parameter solver ditetapkan secara eksplisit dan dijaga identik pada kedua kondisi, untuk memastikan perbedaan hasil semata-mata berasal dari kebijakan CPU Manager, bukan dari perilaku algoritmik solver:

- **`Method = 2`**, memaksa solver menggunakan barrier murni (bukan *automatic* atau *concurrent*). Tanpa penetapan ini, Gurobi berpotensi memilih *concurrent optimizer* yang menjalankan simplex secara paralel dan terpisah dari barrier, sehingga titik transisi antara fase barrier dan fase crossover tidak dapat diidentifikasi secara andal.
- **`Crossover = 4`**, memaksa solver untuk mengeksekusi langkah *push* pada variabel primal dan dual secara eksplisit, mengesampingkan penentuan otomatis (`-1`) bawaan solver untuk memastikan fase crossover tereksekusi penuh.

### Objek Uji

Objek uji diambil dari koleksi benchmark Mittelmann, karena koleksi ini merupakan standar yang lazim dipakai dalam evaluasi performa solver LP [5][8]. Dipilih lima instance LP murni (bukan MILP) dengan variasi ukuran dan struktur sparsity matriks, untuk mengamati apakah pengaruh CPU pinning terhadap fase crossover bervariasi sesuai karakteristik instance (menjawab Rumusan Masalah poin 4):

1. **neos3** — instance berukuran menengah yang umum dipakai sebagai pembanding performa solver LP.
2. **L1_sixm1000obs** — instance berskala besar dari keluarga problem fitting/aproksimasi L1.
3. **Linf_520c** — instance dari keluarga problem aproksimasi L∞, didokumentasikan memiliki fase crossover yang signifikan pada solver berbasis barrier.
4. **cont1** — instance dari keluarga problem kontrol kontinu (*continuous control problem*), dengan struktur matriks yang mengalami *fill-in* signifikan saat presolve/faktorisasi, menjadikannya kandidat yang relevan untuk mengamati sensitivitas terhadap cache locality.
5. **cont11** — varian dari cont1 dengan jumlah kolom (variabel) lebih banyak, menambah variasi bentuk matriks (jumlah baris jauh lebih banyak dari jumlah kolom).

Kelima instance ini mewakili dua keluarga problem yang berbeda strukturnya (fitting/aproksimasi dan kontrol kontinu), sehingga variasi ukuran dan sparsity yang diamati tidak terbatas pada satu jenis struktur masalah saja.

### Prosedur Pengukuran

Setiap instance benchmark dijalankan secara berulang sebanyak **15 kali** pada masing-masing kondisi (A dan B). Jumlah ini dipilih sebagai titik tengah antara kebutuhan daya statistik yang memadai untuk uji nonparametrik (Mann-Whitney U memerlukan minimal sekitar 8–10 sampel per grup agar valid) dan keterbatasan waktu eksekusi akibat sifat sekuensial pengujian (lihat Subbab "Perangkat Lunak dan Parameter Solver"). Pelaporan hasil menggunakan nilai median antar pengulangan, disertai ukuran variabilitas (*interquartile range*), bukan semata rata-rata, untuk mengantisipasi *outlier* akibat *noise* yang berpotensi timbul dari berbagi *physical host* dengan tenant lain di lingkungan cloud publik.

Pengukuran utama difokuskan pada *wall-clock crossover time*. Pemisahan waktu fase barrier dan fase crossover dilakukan melalui **instrumentasi callback Gurobi** (`GRB.Callback.RUNTIME` pada *callback* `BARRIER` dan `SIMPLEX`), bukan melalui pembacaan log teks solver — log Gurobi hanya mencatat *timestamp* dengan granularitas satu detik [1][2], yang terlalu kasar mengingat durasi fase crossover pada sejumlah kasus dapat berlangsung sub-detik. Parsing log teks tetap dijalankan sebagai pemeriksaan silang sekunder; apabila terdapat selisih signifikan antara kedua sumber pengukuran, *run* yang bersangkutan ditandai untuk pemeriksaan manual sebelum dimasukkan ke analisis.

Metrik pendukung yang dikumpulkan secara simultan dari level host (bukan dari dalam container, karena keterbatasan visibilitas Pod terhadap statistik cgroup node) meliputi:

- **Involuntary context switches**, diperoleh dari `/proc/[pid]/status`, sebagai *proksi* frekuensi gangguan penjadwalan terhadap thread solver pada rentang waktu fase crossover saja (lihat Batasan Masalah poin 5 — metrik ini bukan pengukuran langsung migrasi antar-core).
- **CFS throttling statistics**, diperoleh dari `cpu.stat` pada cgroup container, dievaluasi untuk memverifikasi dan mengisolasi bahwa pelambatan yang terjadi bukan merupakan artefak dari pembatasan kuota CPU (CFS quota pauses), melainkan murni dari overhead migrasi thread.
- **Iteration count** fase barrier, untuk memverifikasi bahwa kedua kondisi mencapai titik awal crossover yang setara.

### Analisis Data

**Rumusan Masalah 1 (pengaruh CPU pinning terhadap waktu crossover).** Perbandingan *wall-clock crossover time* antara Kondisi A dan B per instance dianalisis secara deskriptif (median, IQR) dan diuji signifikansinya menggunakan uji nonparametrik Mann-Whitney U, mengingat ukuran sampel yang terbatas dan kemungkinan distribusi yang tidak normal akibat *noise* infrastruktur cloud. Karena pengujian dilakukan terpisah pada setiap instance (sehingga terdapat beberapa uji hipotesis sejenis), nilai-p dikoreksi menggunakan koreksi Bonferroni (α disesuaikan menjadi 0,05 dibagi jumlah instance) untuk mengontrol *family-wise error rate*; nilai-p tanpa koreksi tetap dilaporkan sebagai pembanding eksploratif.

**Rumusan Masalah 2 (korelasi context switches/migrasi thread dengan waktu crossover).** Korelasi Spearman antara *involuntary context switches* dan *crossover time* dihitung **secara terpisah di dalam masing-masing kondisi** (bukan digabung lintas Kondisi A dan B). Pemisahan ini perlu dilakukan karena penggabungan data lintas kondisi berisiko menghasilkan korelasi semu yang sebenarnya hanya mencerminkan perbedaan rata-rata antar kondisi (*confounding* akibat perbedaan tingkat keduanya, bukan hubungan sebab-akibat yang sesungguhnya di dalam satu kondisi).

**Rumusan Masalah 3 (stabilitas fase barrier antar konfigurasi).** Selain perbandingan *iteration count* fase barrier, durasi fase barrier (hasil instrumentasi callback yang sama) juga diuji dengan Mann-Whitney U antar kondisi per instance. Hasil yang **tidak signifikan** pada uji ini mendukung asumsi bahwa fase barrier relatif stabil, sehingga perbedaan performa total yang teramati dapat diatribusikan pada fase crossover.

**Rumusan Masalah 4 (besar kontribusi kebijakan CPU Manager terhadap variasi performa antar instance).** Untuk setiap instance, dihitung *effect size* berupa korelasi *rank-biserial* (turunan langsung dari statistik U pada uji Mann-Whitney) serta persentase reduksi median *crossover time* dari Kondisi A ke Kondisi B. Kedua ukuran ini kemudian dibandingkan **antar instance** untuk mengamati apakah besar pengaruh CPU pinning berasosiasi secara sistematis dengan karakteristik instance (ukuran dan sparsity, lihat Subbab "Objek Uji") — bukan sekadar menyimpulkan signifikan/tidak signifikan, melainkan mengkuantifikasi seberapa besar kontribusinya pada masing-masing karakteristik instance.

### Keterbatasan Metodologis

Karena eksperimen dilaksanakan pada infrastruktur *virtual machine* di lingkungan *cloud* publik (bukan *bare-metal* dedicated), satu vCPU pada Google Compute Engine merepresentasikan satu *hyperthread*, bukan satu *physical core* penuh. Karakteristik *cache locality* hasil pinning pada konteks ini berpotensi berbeda dari pinning pada *physical core* di lingkungan *bare-metal*. Selain itu, meskipun klaster Kubernetes yang dibangun bersifat *single-tenant* pada level Pod, VM tetap berbagi *physical host* dengan *virtual machine* milik penyewa lain di sisi penyedia *cloud*, sehingga *noise* residual akibat *co-location* tidak dapat dieliminasi secara penuh — hanya diminimalkan melalui pengulangan pengukuran dan pelaporan median.

---

## Roadmap Penelitian

**Tahun Pertama:** Pengukuran empiris pengaruh CPU pinning (CPU Manager `static`) terhadap waktu eksekusi fase crossover LP solver di Kubernetes, dibandingkan baseline CFS (`none`), menggunakan benchmark Mittelmann.

**Tahun Kedua:** Memperluas pengukuran ke skema *concurrent crossover* berdampingan dengan PDHG pada GPU [13], serta menambah variabel konfigurasi `reservedSystemCPUs` dan `strict-cpu-reservation`.

**Tahun Ketiga:** Mengembangkan model rekomendasi konfigurasi (*configuration advisor*) yang memetakan karakteristik instance LP terhadap kebijakan CPU Manager optimal.

**Tahun Keempat:** Memperluas konteks ke topologi multi-node/multi-socket, mengkaji interaksi CPU pinning dengan Topology Manager dan NUMA awareness.

**Tahun Kelima:** Menyusun *best-practice guideline* untuk komunitas praktisi dan menjajaki kolaborasi dengan vendor solver untuk integrasi *auto-tuning* konfigurasi.

---

## Daftar Pustaka

*Sitasi disusun dan ditulis berdasarkan sistem nomor sesuai dengan urutan pengutipan. Hanya pustaka yang disitasi pada usulan penelitian yang dicantumkan dalam Daftar Pustaka.*

[1] Gurobi Optimization, LLC, *Parameter Reference – Gurobi Optimizer Reference Manual*. [Online]. Available: https://docs.gurobi.com/projects/optimizer/en/current/reference/parameters.html

[2] Gurobi Optimization, LLC, *Barrier Logging – Gurobi Optimizer Reference Manual*. [Online]. Available: https://docs.gurobi.com/projects/optimizer/en/current/concepts/logging/barrier.html

[3] Kubernetes, "Kubernetes v1.26: CPUManager goes GA." [Online]. Available: https://kubernetes.io/blog/2022/12/27/cpumanager-ga/

[4] Kubernetes, "Control CPU Management Policies on the Node." [Online]. Available: https://kubernetes.io/docs/tasks/administer-cluster/cpu-management-policies/

[5] H. Mittelmann, "Benchmarks for Optimization Software." [Online]. Available: https://plato.asu.edu/bench.html

[6] Ghatrehsamani et al., "The Art of CPU-Pinning: Evaluating and Improving the Performance of Virtualization and Containerization Platforms," *arXiv:2006.02055*, 2020.

[7] E. Rothberg, "From an Interior Point to a Corner Point: Smart Crossover," *INFORMS Journal on Computing*, 2021.

[8] Texas A&M University, "Mittelmann – SuiteSparse Matrix Collection." [Online]. Available: https://sparse.tamu.edu/Mittelmann

[9] S. Damani, P. Barua, and V. Sarkar, "Memory Access Scheduling to Reduce Thread Migrations," in *Proc. ACM*, 2022.

[10] A. Abu-Lebdeh et al., "Fine-Grained Scheduling for Containerized HPC Workloads in Kubernetes Clusters," *arXiv:2211.11487*, 2022.

[11] Gurobi Optimization, LLC, "Crossover LP symmetric solution," *Gurobi Help Center*, Jul. 31, 2023. [Online]. Available: https://support.gurobi.com/hc/en-us/community/posts/17441473898641-Crossover-LP-symmetric-solution

[12] Gurobi Optimization, LLC, "Disable Crossover for MILP that is reduced to LP in Presolve," *Gurobi Help Center*, Jun. 8, 2022. [Online]. Available: https://support.gurobi.com/hc/en-us/community/posts/6704391605265-Disable-Crossover-for-MILP-that-is-reduced-to-LP-in-Presolve

[13] E. Rothberg, "Concurrent Crossover for PDHG," *arXiv preprint arXiv:2510.24429*, Oct. 2025.

[14] S. Deng, H. Zhao, B. Huang, C. Zhang, F. Chen, Y. Deng, J. Yin, S. Dustdar, and A. Y. Zomaya, "Cloud-Native Computing: A Survey from the Perspective of Services," *arXiv preprint arXiv:2306.14402*, 2023.
