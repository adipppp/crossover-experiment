# Pengaruh CPU Pinning terhadap Performa Fase Crossover pada LP Solver di Lingkungan Kubernetes

---

## Pendahuluan

### 1.1 Latar Belakang

Metode interior-point digunakan secara luas untuk menyelesaikan permasalahan optimasi linear programming (LP) berskala besar karena efisiensi komputasinya. Namun, solusi yang dihasilkan oleh metode ini umumnya berupa solusi interior atau nonbasic, sementara banyak aplikasi LP justru lebih mensyaratkan solusi basic [7]. Untuk menjembatani perbedaan ini, solver LP pada umumnya menerapkan fase crossover setelah fase barrier, yang berfungsi mengonversi solusi interior menjadi solusi basic yang valid melalui algoritma Simplex.

Permasalahannya, dibandingkan fase barrier, fase crossover secara proporsional jauh lebih rentan terhadap dampak cache locality dan migrasi thread [4][6][9] — bukan karena fase barrier sama sekali kebal dari kedua faktor tersebut, melainkan karena sifat sekuensial crossover membuatnya tidak memiliki paralelisme lain yang dapat "menutupi" cost setiap migrasi, sebagaimana dijelaskan lebih lanjut pada paragraf berikutnya. Akibatnya, jika tidak dikonfigurasi dengan baik, crossover berpotensi menjadi bottleneck yang justru lebih besar daripada fase barrier itu sendiri [7]. Hal ini terjadi karena crossover bersifat sangat sekuensial dan memiliki kemampuan terbatas untuk memanfaatkan paralelisme multi-core, berbeda dengan fase barrier yang mengandalkan faktorisasi Cholesky padat/jarang yang sangat paralel, maupun metode first-order seperti PDHG yang dapat dijalankan secara paralel di banyak thread atau bahkan GPU [13]. Kondisi ini makin terasa seiring meningkatnya tingkat paralelisme fase barrier, yang membuat kontribusi relatif waktu crossover terhadap total waktu eksekusi turut meningkat [13]. Pada beberapa instance benchmark, crossover bahkan tercatat memakan hingga 96% dari total waktu solver [11][12]. Crossover, dengan demikian, bukan lagi sekadar tahap pelengkap, melainkan dapat menjadi penentu utama performa solver LP secara keseluruhan.

Secara struktur data, operasi pivot pada crossover melibatkan akumulasi vektor jarang (*sparse vector accumulation*) yang memicu akses memori acak dan tidak kontigu — pola access pattern yang sangat berbeda dari operasi faktorisasi matriks pada fase barrier. Pola akses acak ini membuat keberhasilan menjaga data tetap berada pada cache level L3 (*cache preservation*) menjadi krusial bagi performa crossover; setiap *cache miss* yang terjadi akibat thread berpindah core (migrasi) akan memaksa data dipanggil ulang dari memori utama, menambah latensi yang signifikan pada workload yang sudah bersifat sekuensial ini.

Di sisi lain, perkembangan cloud-native infrastructure mendorong semakin banyak workload saintifik untuk dijalankan pada Kubernetes [14]. Untuk mengakomodasi workload seperti crossover yang sensitif terhadap locality, Kubernetes menyediakan konfigurasi CPU management policy. Secara default, kebijakan ini diatur ke `none`, yang berarti penjadwalan CPU sepenuhnya diserahkan kepada Completely Fair Scheduler (CFS) bawaan Linux. CFS menjadwalkan thread berdasarkan struktur red-black tree yang diurutkan menurut *virtual runtime* tiap thread, dan secara periodik melakukan *load balancing* lintas runqueue antar-core untuk meratakan beban — mekanisme inilah yang menjadi sumber migrasi thread dari satu core ke core lain antar time slice. Ketika kebijakan diubah menjadi `static`, container pada pod dengan kelas Guaranteed QoS dan permintaan CPU bertipe integer akan dialokasikan sejumlah core secara eksklusif melalui mekanisme cgroup `cpuset`, alih-alih hanya mendapat kuota waktu CPU dari CFS [4]. Controller `cpuset` ini bekerja dengan mengeluarkan core yang dialokasikan dari mekanisme load balancing CFS, sehingga thread yang berjalan di dalamnya tidak akan dipindah-pindah oleh scheduler kernel ke core lain selama masa hidup container tersebut [3][4]. Tersedianya mekanisme ini membuka peluang untuk mengurangi overhead migrasi thread yang menjadi sumber utama bottleneck pada fase crossover.

Selain berdampak pada locality, jitter penjadwalan yang ditimbulkan oleh migrasi thread juga berimplikasi pada non-determinisme numerik solver. Pada algoritma barrier multi-thread, variasi kecil pada urutan dan latensi penjadwalan thread dapat mengubah urutan operasi floating-point pada faktorisasi paralel, yang pada gilirannya dapat mengubah lintasan pencarian numerik solver — menghasilkan jumlah iterasi dan waktu konvergensi yang berbeda meski menyelesaikan instance yang identik. CPU pinning, dengan menstabilkan penjadwalan, berpotensi turut menstabilkan lintasan algoritmik ini, di luar efek murni pada cache locality.

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

### 1.4 Hipotesis Penelitian

Berdasarkan latar belakang di atas, penelitian ini mengajukan hipotesis sebagai berikut:

**H1:** Di bawah kebijakan CPU Manager `static`, jumlah *involuntary context switches* selama fase crossover akan menurun secara signifikan dibandingkan kebijakan `none`, yang berkorelasi dengan penurunan dan stabilisasi (pengurangan IQR) *crossover time* yang signifikan secara statistik.

**H0 (null):** Tidak terdapat perbedaan signifikan pada *involuntary context switches* maupun *crossover time* antara kebijakan `static` dan `none`.

Hipotesis ini diuji secara spesifik melalui prosedur pada Subbab "Analisis Data".

### 1.5 Manfaat Penelitian

**Manfaat Teoritis.** Penelitian ini memberikan kontribusi berupa pengukuran empiris yang memisahkan efek CPU pinning dari efek lain dalam Kubernetes (seperti throttling dan kebijakan resource lainnya), khususnya pada fase crossover solver LP — sebuah area yang belum banyak diteliti secara spesifik (lihat Subbab 1.1). Penelitian ini juga membedakan secara eksplisit antara dua sumber stabilitas yang berbeda: *algorithmic stability*, yang dicapai melalui penetapan parameter solver identik (`Method=2` dan `Crossover=4`) untuk mengeliminasi variasi jalur algoritmik solver; dan *systems stability*, yang dicapai melalui CPU pinning untuk mengeliminasi jitter penjadwalan dan migrasi thread pada level sistem operasi. Dengan memisahkan kedua sumber variasi ini, kontribusi ilmiahnya tidak hanya terletak pada evaluasi performa solver, tetapi juga pada pemahaman mekanisme sistem operasi dan orkestrasi container terhadap karakteristik workload optimisasi yang bersifat sekuensial.

**Manfaat Praktis.** Hasil penelitian ini diharapkan dapat menjadi dasar rekomendasi konfigurasi Kubernetes bagi praktisi yang menjalankan solver optimisasi di lingkungan container, khususnya dalam menentukan apakah CPU pinning benar-benar diperlukan untuk memperoleh performa yang stabil dan mendekati bare-metal pada workload mathematical optimization yang sensitif terhadap locality dan latency.

### 1.6 Batasan Masalah

Penelitian ini dibatasi pada lingkup berikut.

1. Perbandingan kebijakan CPU Manager hanya mencakup `none` (CFS default) dan `static`; konfigurasi lain seperti integrasi dengan Topology Manager atau NUMA awareness tidak termasuk dalam lingkup penelitian ini, namun topologi NUMA host akan diverifikasi dan didokumentasikan sebagai bagian dari karakterisasi lingkungan eksperimen (lihat Subbab "Karakterisasi Topologi Hardware").
2. Solver yang digunakan hanya Gurobi Optimizer dengan lisensi Academic WLS; perbandingan dengan solver open-source lain (mis. HiGHS, SoPlex) tidak dilakukan.
3. Eksperimen dilaksanakan pada satu klaster Kubernetes single-node di atas Virtual Machine Google Compute Engine (8 vCPU hardware, namun dikonfigurasi menjadi 4 vCPU pada guest OS dengan menonaktifkan SMT); skenario multi-node atau infrastruktur bare-metal dedicated tidak termasuk dalam lingkup penelitian ini. Langkah penonaktifan SMT ini dibahas sebagai mitigasi proaktif pada Subbab "Keterbatasan Metodologis".
4. Objek uji hanya berupa instance LP murni dari koleksi benchmark Mittelmann; instance MILP atau kelas masalah optimisasi lainnya tidak dicakup.
5. Migrasi thread diukur secara tidak langsung melalui involuntary context switches sebagai proksi, bukan melalui pengukuran perpindahan antar-core secara langsung (mis. tracing event migrasi kernel). Sebagai pelengkap, penelitian ini juga mengumpulkan metrik hardware performance counter (cache miss rate) melalui `perf stat` sebagai proksi kedua yang lebih dekat pada mekanisme fisik penyebab degradasi performa (lihat Subbab "Prosedur Pengukuran").
6. Cakupan penelitian dibatasi secara eksplisit pada perbandingan kebijakan `static` versus `none` pada lima instance LP terpilih dengan 15 repetisi (setara dengan rencana "Tahun Pertama" pada versi proposal sebelumnya). Perluasan ke arah multi-node, NUMA-aware scheduling, maupun concurrent crossover pada GPU diklasifikasikan sebagai arah penelitian jangka panjang dan tidak termasuk dalam lingkup skripsi ini (lihat Subbab "Arah Penelitian Selanjutnya").

---

## Metode Penelitian

### Desain Eksperimen

Penelitian ini menggunakan pendekatan eksperimen empiris terkontrol dengan desain *within-subject*, yaitu membandingkan dua kebijakan CPU Manager pada satu unit infrastruktur yang sama untuk mengeliminasi variabel perancu yang muncul apabila kedua kondisi diuji pada perangkat keras yang berbeda. Dua kondisi yang dibandingkan adalah:

1. **Kondisi A (baseline):** CPU Manager dengan kebijakan `none`, yaitu penjadwalan CPU diserahkan penuh kepada Completely Fair Scheduler (CFS) bawaan Linux.
2. **Kondisi B (perlakuan):** CPU Manager dengan kebijakan `static`, yaitu container pada pod dengan kelas Guaranteed QoS dialokasikan CPU secara eksklusif melalui mekanisme `cpuset`.

Kedua kondisi diuji secara bergantian pada node Kubernetes yang sama, dengan kebijakan CPU Manager diubah melalui modifikasi `kubelet-config.yaml` diikuti restart layanan kubelet di antara dua sesi pengujian.

### Infrastruktur Eksperimen

Eksperimen dilaksanakan pada satu Virtual Machine Google Compute Engine dengan spesifikasi 8 vCPU (tipe *compute-optimized*, mis. `c2-standard-8`), dibiayai melalui kredit *free trial* Google Cloud Platform. Pemilihan instance tunggal dengan kuota 8 vCPU ini sejalan dengan batas penggunaan *concurrent* Compute Engine pada akun *Free Trial* GCP. Untuk mengeliminasi interferensi akibat Hyper-Threading (SMT), VM dikonfigurasi menggunakan opsi `--threads-per-core=1` saat pembuatan, sehingga guest OS hanya mendeteksi 4 vCPU (masing-masing 1-to-1 dengan physical core).

Di atas VM tersebut dibangun klaster Kubernetes *single-node* menggunakan kubeadm (bukan layanan terkelola seperti GKE), dengan pertimbangan utama: (a) kebutuhan kontrol penuh atas konfigurasi kubelet untuk mengubah `cpuManagerPolicy` dan merestart layanan kapan pun diperlukan tanpa proses persetujuan administratif; dan (b) menghindari biaya tambahan di luar kuota *free trial* yang melekat pada layanan klaster terkelola. *Taint* bawaan pada node *control-plane* dihapus agar Pod beban kerja dapat dijadwalkan pada node tunggal tersebut.

Konfigurasi sumber daya pada kedua kondisi disusun sebagai berikut: 1 vCPU dicadangkan untuk proses sistem dan daemon Kubernetes melalui parameter `kubeReserved` (500m) dan `systemReserved` (500m), menyisakan 3 vCPU yang dapat dialokasikan ke Pod beban kerja. Pada Kondisi B, Pod solver didefinisikan dengan `resources.requests.cpu` sama dengan `resources.limits.cpu` bernilai 3 CPU, sebagai syarat agar Pod memenuhi kelas Guaranteed QoS dan berhak atas alokasi CPU eksklusif oleh kebijakan `static`.

### Karakterisasi Topologi Hardware

Sebelum eksperimen utama dilaksanakan, topologi hardware VM host dikarakterisasi terlebih dahulu menggunakan `lscpu` dan `numactl --hardware`, dengan dua tujuan:

1. **Verifikasi penonaktifan SMT/hyperthreading.** Pada platform cloud publik seperti Google Compute Engine, satu vCPU secara default merepresentasikan satu hardware thread (hyperthread), bukan satu physical core penuh. Melalui opsi pembuatan VM `--threads-per-core=1`, SMT dinonaktifkan secara paksa di level hypervisor. Hasil `lscpu` didokumentasikan untuk memastikan bahwa `Thread(s) per core` bernilai 1 dan jumlah `CPU(s)` yang terdeteksi oleh guest OS adalah 4, memastikan ke-3 vCPU yang dialokasikan ke Pod solver masing-masing berjalan pada physical core independen tanpa pembagian resource cache L1/L2 maupun execution pipeline.
2. **Verifikasi topologi NUMA.** Untuk workload yang memory-bound seperti crossover, latensi akses memori bergantung pada apakah memori yang diakses berada pada socket NUMA yang sama dengan core yang mengeksekusi thread. Hasil `numactl --hardware` didokumentasikan untuk memastikan apakah ke-4 vCPU yang terlihat oleh guest OS berada pada satu node NUMA tunggal.

### Perangkat Lunak dan Parameter Solver

Solver yang digunakan adalah **Gurobi Optimizer**, dengan lisensi *Academic Web License Service* (WLS). Karena lisensi akademik ini dibatasi maksimum dua sesi konkuren, seluruh pengujian dijalankan secara sekuensial — satu Pod solver pada satu waktu — sehingga tidak ada kontensi lisensi yang dapat mengacaukan pengukuran waktu.

Dua parameter solver ditetapkan secara eksplisit dan dijaga identik pada kedua kondisi, untuk memastikan perbedaan hasil semata-mata berasal dari kebijakan CPU Manager (*systems stability*), bukan dari perilaku algoritmik solver (*algorithmic stability*):

- **`Method = 2`**, memaksa solver menggunakan barrier murni (bukan *automatic* atau *concurrent*). Tanpa penetapan ini, Gurobi berpotensi memilih *concurrent optimizer* yang menjalankan simplex secara paralel dan terpisah dari barrier, sehingga titik transisi antara fase barrier dan fase crossover tidak dapat diidentifikasi secara andal.
- **`Crossover = 4`**, memaksa solver untuk mengeksekusi langkah *push* pada variabel primal dan dual secara eksplisit, mengesampingkan penentuan otomatis (`-1`) bawaan solver untuk memastikan fase crossover tereksekusi penuh.

Penetapan kedua parameter ini menjamin *algorithmic stability* — yaitu, lintasan algoritmik solver (jumlah iterasi, urutan operasi) yang seharusnya identik secara struktural pada kedua kondisi. Dengan demikian, setiap perbedaan waktu eksekusi maupun variabilitas (IQR) yang teramati antar-kondisi dapat diatribusikan pada *systems stability* yang dipengaruhi oleh kebijakan CPU Manager, bukan pada variasi jalur algoritmik akibat jitter penjadwalan yang memengaruhi urutan operasi floating-point pada faktorisasi paralel.

### Objek Uji

Objek uji diambil dari koleksi benchmark Mittelmann, karena koleksi ini merupakan standar yang lazim dipakai dalam evaluasi performa solver LP [5][8]. Dipilih lima instance LP murni (bukan MILP) dengan variasi ukuran dan struktur sparsity matriks, untuk mengamati apakah pengaruh CPU pinning terhadap fase crossover bervariasi sesuai karakteristik instance (menjawab Rumusan Masalah poin 4):

1. **neos3** — instance berukuran menengah yang umum dipakai sebagai pembanding performa solver LP.
2. **L1_sixm1000obs** — instance berskala besar dari keluarga problem fitting/aproksimasi L1.
3. **Linf_520c** — instance dari keluarga problem aproksimasi L∞, didokumentasikan memiliki fase crossover yang signifikan pada solver berbasis barrier.
4. **cont1** — instance dari keluarga problem kontrol kontinu (*continuous control problem*), dengan struktur matriks yang mengalami *fill-in* signifikan saat presolve/faktorisasi, menjadikannya kandidat yang relevan untuk mengamati sensitivitas terhadap cache locality.
5. **cont11** — varian dari cont1 dengan jumlah kolom (variabel) lebih banyak, menambah variasi bentuk matriks (jumlah baris jauh lebih banyak dari jumlah kolom).

Kelima instance ini mewakili dua keluarga problem yang berbeda strukturnya (fitting/aproksimasi dan kontrol kontinu), sehingga variasi ukuran dan sparsity yang diamati tidak terbatas pada satu jenis struktur masalah saja.

### Isolasi I/O Storage

Untuk mengeliminasi latensi I/O disk sebagai variabel perancu, seluruh file instance benchmark (`.mps`) dan direktori output hasil tidak dimuat dari `hostPath` yang menunjuk ke disk persisten, melainkan dari volume `emptyDir` bertipe `Memory` (tmpfs) yang dipasang pada Pod solver. Dengan mekanisme ini, baik pembacaan instance maupun penulisan hasil terjadi seluruhnya di RAM, sehingga thread solver tidak akan memasuki status *blocked* (*D state* pada Linux) akibat operasi I/O disk, yang jika tidak diisolasi dapat memicu *voluntary context switch* yang keliru diatribusikan sebagai efek penjadwalan CPU.

### Prosedur Pengukuran

Setiap instance benchmark dijalankan secara berulang sebanyak **15 kali** pada masing-masing kondisi (A dan B). Jumlah ini dipilih sebagai titik tengah antara kebutuhan daya statistik yang memadai untuk uji nonparametrik (Mann-Whitney U memerlukan minimal sekitar 8–10 sampel per grup agar valid) dan keterbatasan waktu eksekusi akibat sifat sekuensial pengujian (lihat Subbab "Perangkat Lunak dan Parameter Solver"). Pelaporan hasil menggunakan nilai median antar pengulangan, disertai ukuran variabilitas (*interquartile range*), bukan semata rata-rata, untuk mengantisipasi *outlier* akibat *noise* yang berpotensi timbul dari berbagi *physical host* dengan tenant lain di lingkungan cloud publik.

Pengukuran utama difokuskan pada *wall-clock crossover time*. Pemisahan waktu fase barrier dan fase crossover dilakukan melalui **instrumentasi callback Gurobi** (`GRB.Callback.RUNTIME` pada *callback* `BARRIER` dan `SIMPLEX`), bukan melalui pembacaan log teks solver — log Gurobi hanya mencatat *timestamp* dengan granularitas satu detik [1][2], yang terlalu kasar mengingat durasi fase crossover pada sejumlah kasus dapat berlangsung sub-detik. Parsing log teks tetap dijalankan sebagai pemeriksaan silang sekunder; apabila terdapat selisih signifikan antara kedua sumber pengukuran, *run* yang bersangkutan ditandai untuk pemeriksaan manual sebelum dimasukkan ke analisis.

Metrik pendukung yang dikumpulkan secara simultan dari level host (bukan dari dalam container, karena keterbatasan visibilitas Pod terhadap statistik cgroup node) meliputi:

- **Involuntary context switches**, diperoleh dari `/proc/[pid]/status`, sebagai *proksi* frekuensi gangguan penjadwalan terhadap thread solver pada rentang waktu fase crossover saja. Perlu ditegaskan bahwa metrik ini merupakan **proksi tidak langsung**: nilai `nonvoluntary_ctxt_switches` mengindikasikan bahwa CFS melakukan preemption terhadap suatu thread (baik karena time slice yang habis maupun karena task berprioritas lebih tinggi memasuki runqueue), namun tidak secara langsung membuktikan bahwa preemption tersebut diikuti oleh perpindahan thread ke core yang berbeda (migrasi). Untuk itu, metrik ini dilengkapi oleh metrik hardware performance counter di bawah sebagai proksi kedua yang lebih dekat pada mekanisme fisik penyebab degradasi performa (lihat Batasan Masalah poin 5).
- **Hardware performance counters**, diperoleh melalui `perf stat` yang dijalankan dari host terhadap PID proses solver di dalam container, mencakup minimal `cache-misses`, `cache-references`, `L1-dcache-load-misses`, `L1-dcache-loads`, `instructions`, dan `cycles`. Mengingat sifat crossover yang memory-bound dan bergantung pada akumulasi vektor jarang dengan pola akses non-kontigu, cache miss rate merupakan indikator yang lebih dekat secara fisik terhadap mekanisme degradasi performa dibandingkan involuntary context switches semata. Metrik ini digunakan untuk memperkuat (bukan menggantikan) interpretasi terhadap proksi context switch.
- **CFS throttling statistics** (`nr_throttled` dan `throttled_usec`), diperoleh dari `cpu.stat` pada cgroup container, dievaluasi untuk memverifikasi dan mengisolasi bahwa pelambatan yang terjadi bukan merupakan artefak dari pembatasan kuota CPU (CFS quota pauses), melainkan murni dari overhead migrasi thread.
- **Iteration count** fase barrier, untuk memverifikasi bahwa kedua kondisi mencapai titik awal crossover yang setara.

Seluruh metrik pendukung di atas dikumpulkan khusus pada rentang waktu fase crossover (ditentukan melalui instrumentasi callback yang sama), bukan pada keseluruhan durasi eksekusi solver, agar atribusi metrik terhadap fase crossover tetap presisi.

### Analisis Data

**Rumusan Masalah 1 (pengaruh CPU pinning terhadap waktu crossover).** Perbandingan *wall-clock crossover time* antara Kondisi A dan B per instance dianalisis secara deskriptif (median, IQR) dan diuji signifikansinya menggunakan uji nonparametrik Mann-Whitney U, mengingat ukuran sampel yang terbatas dan kemungkinan distribusi yang tidak normal akibat *noise* infrastruktur cloud. Karena pengujian dilakukan terpisah pada setiap instance (sehingga terdapat beberapa uji hipotesis sejenis), nilai-p dikoreksi menggunakan koreksi Bonferroni (α disesuaikan menjadi 0,05 dibagi jumlah instance) untuk mengontrol *family-wise error rate*; nilai-p tanpa koreksi tetap dilaporkan sebagai pembanding eksploratif. Pengujian ini secara langsung menjawab Hipotesis H1 pada Subbab 1.4.

**Rumusan Masalah 2 (korelasi context switches/migrasi thread dengan waktu crossover).** Korelasi Spearman antara *involuntary context switches* dan *crossover time* dihitung **secara terpisah di dalam masing-masing kondisi** (bukan digabung lintas Kondisi A dan B). Pemisahan ini perlu dilakukan karena penggabungan data lintas kondisi berisiko menghasilkan korelasi semu yang sebenarnya hanya mencerminkan perbedaan rata-rata antar kondisi (*confounding* akibat perbedaan tingkat keduanya, bukan hubungan sebab-akibat yang sesungguhnya di dalam satu kondisi). Sebagai pelengkap dan triangulasi, korelasi Spearman yang sama juga dihitung antara *cache miss rate* (dari `perf stat`) dengan *crossover time*, untuk menilai konsistensi arah hubungan antara kedua proksi yang berbeda level abstraksinya (level OS-scheduler vs. level hardware).

**Rumusan Masalah 3 (stabilitas fase barrier antar konfigurasi).** Selain perbandingan *iteration count* fase barrier, durasi fase barrier (hasil instrumentasi callback yang sama) juga diuji dengan Mann-Whitney U antar kondisi per instance. Hasil yang **tidak signifikan** pada uji ini mendukung asumsi bahwa fase barrier relatif stabil, sehingga perbedaan performa total yang teramati dapat diatribusikan pada fase crossover.

**Rumusan Masalah 4 (besar kontribusi kebijakan CPU Manager terhadap variasi performa antar instance).** Untuk setiap instance, dihitung *effect size* berupa korelasi *rank-biserial* (turunan langsung dari statistik U pada uji Mann-Whitney) serta persentase reduksi median *crossover time* dari Kondisi A ke Kondisi B. Kedua ukuran ini kemudian dibandingkan **antar instance** untuk mengamati apakah besar pengaruh CPU pinning berasosiasi secara sistematis dengan karakteristik instance (ukuran dan sparsity, lihat Subbab "Objek Uji") — bukan sekadar menyimpulkan signifikan/tidak signifikan, melainkan mengkuantifikasi seberapa besar kontribusinya pada masing-masing karakteristik instance.

### Keterbatasan Metodologis

Meskipun eksperimen dilaksanakan pada infrastruktur *virtual machine* di lingkungan *cloud* publik (bukan *bare-metal* dedicated), risiko *resource contention* akibat pembagian L1/L2 cache dan execution pipeline antara *sibling hyperthreads* telah dimitigasi secara proaktif dengan mengonfigurasi VM menggunakan opsi `--threads-per-core=1`. Konfigurasi ini menjamin bahwa setiap vCPU yang terlihat oleh guest OS berkorelasi 1-to-1 dengan physical core hardware. Hal ini mengeliminasi deviasi performa akibat persaingan thread solver di level core fisik yang sama, sehingga meningkatkan keterwakilan karakteristik *cache locality* hasil pinning agar lebih mendekati karakteristik *bare-metal*.

Namun, keterbatasan metodologis yang tetap ada adalah pembagian *physical host* hypervisor dengan *virtual machine* milik penyewa lain di sisi penyedia *cloud* (co-location noise). *Noise* residual ini tidak dapat dieliminasi secara penuh, namun diminimalkan pengaruhnya melalui pengulangan pengukuran (15 repetisi) dan pelaporan hasil menggunakan nilai median serta *interquartile range* (IQR).

---

## Arah Penelitian Selanjutnya

Skripsi ini secara eksplisit dibatasi pada pengukuran empiris pengaruh CPU pinning (CPU Manager `static`) terhadap waktu eksekusi fase crossover LP solver di Kubernetes, dibandingkan baseline CFS (`none`), pada lima instance Mittelmann terpilih dengan 15 repetisi per kondisi (lihat Batasan Masalah poin 6). Beberapa arah perluasan berikut diidentifikasi sebagai topik penelitian jangka panjang yang berada di luar cakupan skripsi ini, namun relevan untuk penelitian lanjutan (tesis, disertasi, atau hibah penelitian multi-tahun):

1. **Concurrent crossover pada GPU.** Mengevaluasi skema *concurrent crossover* berdampingan dengan metode first-order seperti PDHG yang dijalankan pada GPU [13], serta interaksinya dengan kebijakan CPU Manager di Kubernetes.
2. **Konfigurasi reservasi CPU lanjutan.** Menambah variabel konfigurasi seperti `reservedSystemCPUs` dan `strict-cpu-reservation` untuk mengevaluasi pengaruhnya terhadap isolasi resource pada workload crossover.
3. **Model rekomendasi konfigurasi otomatis.** Mengembangkan model *configuration advisor* yang memetakan karakteristik instance LP (ukuran, sparsity, struktur) terhadap kebijakan CPU Manager yang optimal.
4. **Topologi multi-node dan multi-socket.** Memperluas konteks eksperimen ke klaster multi-node/multi-socket, mengkaji interaksi CPU pinning dengan Topology Manager dan NUMA awareness secara langsung (bukan sekadar karakterisasi pasif sebagaimana pada skripsi ini).
5. **Best-practice guideline untuk praktisi.** Menyusun panduan praktik terbaik bagi komunitas praktisi serta menjajaki kolaborasi dengan vendor solver untuk integrasi *auto-tuning* konfigurasi Kubernetes.

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
