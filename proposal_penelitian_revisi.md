# Proposal Penelitian

## Pengaruh CPU Pinning terhadap Performa Fase Crossover pada LP Solver di Lingkungan Kubernetes

---

## Pendahuluan

### 1.1 Latar Belakang

Metode interior-point digunakan secara luas untuk optimasi linear programming (LP) karena efisiensi dan skalabilitasnya. Akan tetapi, solusi yang dihasilkan oleh metode ini seringkali berupa solusi interior atau nonbasic. Padahal, solusi basic cenderung lebih diinginkan untuk berbagai aplikasi, seperti reoptimasi dan mixed-integer programming. Untuk mengonversi solusi nonbasic menjadi solusi basic yang dibutuhkan, fase crossover dapat diterapkan setelah fase barrier [7].

Fase crossover cenderung lebih sulit diparalelkan dibandingkan fase barrier. Operasi pivoting pada fase crossover membuat crossover sulit untuk diparalelkan, bahkan pada implementasi modern sekalipun. Walaupun penelitian terbaru dari Rothberg mengusulkan strategi concurrent crossover [13], pendekatan tersebut memanfaatkan core yang idle untuk menjalankan beberapa instance crossover secara paralel, bukan memparalelkan pivoting itu sendiri. Ketika thread yang menjalankan fase crossover tertunda karena migrasi, fase crossover akan langsung terhenti karena tidak ada thread lain yang dapat mengambil alih workload tersebut [4][6][9]. Akibatnya, waktu eksekusi relatif crossover terhadap total waktu solving meningkat seiring bertambahnya jumlah core [13]. Pada beberapa instance benchmark, crossover bahkan tercatat memakan hingga 97% dari total waktu solver [11][12]. Jika tidak dikonfigurasi dengan baik, crossover berpotensi menjadi bottleneck, bahkan melebihi fase barrier itu sendiri [7].

Di sisi lain, perkembangan infrastruktur cloud-native mendorong semakin banyak workload saintifik dijalankan pada Kubernetes [10][14]. Untuk mengakomodasi workload yang sensitif terhadap cache locality, Kubernetes menyediakan konfigurasi CPU management policy. Secara default, sebuah pod menggunakan CPU yang digunakan bersama proses lain, mengikuti pada scheduling bawaan Linux. Hal ini memungkinkan thread dapat berpindah core akibat mekanisme load balancing scheduler. Migrasi thread ini dapat menambah overhead berupa cache miss, scheduling latency, dan context switching [6][15]. Ketika kebijakan diubah menjadi `static`, pod dengan Guaranteed QoS dan CPU request integer dapat memperoleh core eksklusif [4], sehingga migrasi thread dapat dikurangi dan utilisasi cache dapat meningkat.

Penelitian sebelumnya menunjukkan bahwa CPU pinning dan thread affinity dapat meningkatkan performa solver secara signifikan. Pada konteks containerized HPC, affinity policy menghasilkan kinerja yang lebih baik dibandingkan baseline tanpa affinity [10]. Temuan ini menguatkan asumsi bahwa CPU pinning dapat menjadi solusi yang relevan untuk masalah migrasi thread pada workload yang sensitif terhadap locality.

Akan tetapi, manfaat CPU pinning tersebut belum tentu berlaku secara langsung pada konteks crossover di Kubernetes. Studi-studi mengenai CPU pinning selama ini lebih banyak mengevaluasi performa pada platform virtualisasi atau workload HPC secara umum [6][10], bukan pada karakteristik spesifik fase crossover. Sebaliknya, penelitian mengenai crossover sendiri sebagian besar berfokus pada pengembangan algoritma [7][13], bukan pada infrastruktur container atau mekanisme CPU scheduling di Kubernetes. Dengan demikian, belum ada kajian empiris yang mengukur overhead migrasi thread pada fase crossover LP solver di Kubernetes secara terpisah dari efek throttling atau policy resource lain.

Gap ini penting untuk diisi karena hasilnya berdampak langsung pada keputusan konfigurasi di lingkungan produksi. Bagi praktisi yang menjalankan solver optimasi di dalam container, kejelasan mengenai pengaruh CPU pinning terhadap fase crossover menjadi dasar penting dalam menentukan strategi deployment. Tanpa bukti empiris yang spesifik, praktisi tidak memiliki dasar yang cukup untuk menilai apakah CPU pinning benar-benar diperlukan untuk memperoleh performa solver yang stabil dan mendekati bare-metal, atau apakah upaya konfigurasi tersebut justru tidak sepadan dengan overhead operasionalnya [3][6][10].

### 1.2 Rumusan Masalah

Rumusan masalah dalam penelitian ini adalah sebagai berikut.

1. Bagaimana pengaruh CPU pinning terhadap waktu eksekusi fase crossover pada LP solver di Kubernetes?
2. Apakah penurunan involuntary context switches pada kebijakan `static` konsisten berkorelasi dengan penurunan waktu crossover, sebagai indikasi bahwa berkurangnya migrasi thread merupakan mekanisme di balik perbaikan performa tersebut?
3. Apakah fase barrier relatif stabil antar konfigurasi, sehingga perbedaan performa terutama berasal dari fase crossover?
4. Apakah besar pengaruh CPU pinning terhadap waktu crossover bervariasi secara sistematis sesuai karakteristik instance (ukuran dan struktur sparsity)?

### 1.3 Tujuan Penelitian

Berdasarkan rumusan masalah di atas, tujuan penelitian ini adalah sebagai berikut.

1. Mengukur pengaruh CPU pinning terhadap waktu eksekusi fase crossover pada LP solver di Kubernetes.
2. Menganalisis konsistensi korelasi antara penurunan involuntary context switches dengan penurunan waktu crossover pada kebijakan `static`, sebagai indikasi mekanisme berkurangnya migrasi thread.
3. Mengevaluasi stabilitas fase barrier antar konfigurasi CPU Manager, untuk memastikan bahwa perbedaan performa yang teramati benar-benar berasal dari fase crossover.
4. Mengevaluasi apakah besar pengaruh CPU pinning terhadap waktu crossover bervariasi secara sistematis sesuai karakteristik instance LP benchmark.

### 1.4 Hipotesis Penelitian

Berdasarkan latar belakang di atas, penelitian ini mengajukan hipotesis sebagai berikut:

**H1:** Di bawah kebijakan CPU Manager `static`, jumlah *involuntary context switches* selama fase crossover akan menurun secara signifikan dibandingkan kebijakan `none`, yang berkorelasi dengan penurunan dan stabilisasi (pengurangan IQR) *crossover time* yang signifikan secara statistik.

**H0 (null):** Tidak terdapat perbedaan signifikan pada *involuntary context switches* maupun *crossover time* antara kebijakan `static` dan `none`.

Hipotesis ini diuji secara spesifik melalui prosedur pada Subbab "Analisis Data".

### 1.5 Manfaat Penelitian

**Manfaat Teoritis.** Penelitian ini memberikan kontribusi berupa pengukuran empiris yang memisahkan efek CPU pinning dari efek lain dalam Kubernetes (seperti throttling dan kebijakan resource lainnya), khususnya pada fase crossover solver LP — sebuah area yang belum banyak diteliti secara spesifik (lihat Subbab 1.1). Dengan menjaga parameter solver tetap identik antar kondisi (lihat Subbab "Perangkat Lunak dan Parameter Solver"), penelitian ini juga memastikan bahwa setiap perbedaan performa yang teramati murni berasal dari kebijakan CPU Manager, bukan dari variasi perilaku algoritmik solver itu sendiri. Dengan demikian, kontribusi ilmiahnya tidak hanya terletak pada evaluasi performa solver, tetapi juga pada pemahaman mekanisme sistem operasi dan orkestrasi container terhadap karakteristik workload optimisasi yang bersifat sekuensial.

**Manfaat Praktis.** Hasil penelitian ini diharapkan dapat menjadi dasar rekomendasi konfigurasi Kubernetes bagi praktisi yang menjalankan solver optimisasi di lingkungan container, khususnya dalam menentukan apakah CPU pinning benar-benar diperlukan untuk memperoleh performa yang stabil dan mendekati bare-metal pada workload mathematical optimization yang sensitif terhadap locality dan latency.

### 1.6 Batasan Masalah

Penelitian ini dibatasi pada lingkup berikut.

1. Perbandingan kebijakan CPU Manager hanya mencakup `none` (CFS default) dan `static`; konfigurasi lain seperti integrasi dengan Topology Manager atau NUMA awareness tidak termasuk dalam lingkup penelitian ini, namun topologi NUMA host akan diverifikasi dan didokumentasikan sebagai bagian dari karakterisasi lingkungan eksperimen (lihat Subbab "Karakterisasi Topologi Hardware").
2. Solver yang digunakan hanya Gurobi Optimizer dengan lisensi Academic WLS; perbandingan dengan solver open-source lain (mis. HiGHS, SoPlex) tidak dilakukan.
3. Eksperimen dilaksanakan pada satu klaster Kubernetes single-node di atas Virtual Machine Google Compute Engine (8 vCPU); skenario multi-node atau infrastruktur bare-metal dedicated tidak termasuk dalam lingkup penelitian ini. Implikasi dari pilihan ini — termasuk kemungkinan dua vCPU merupakan sibling hyperthread pada satu physical core — dibahas pada Subbab "Keterbatasan Metodologis".
4. Objek uji hanya berupa instance LP murni dari koleksi benchmark Mittelmann; instance MILP atau kelas masalah optimisasi lainnya tidak dicakup.
5. Migrasi thread diukur secara tidak langsung melalui involuntary context switches sebagai proksi, bukan melalui pengukuran perpindahan antar-core secara langsung (mis. tracing event migrasi kernel). Sebagai pelengkap, penelitian ini juga mengumpulkan metrik hardware performance counter (cache miss rate) melalui `perf stat` sebagai proksi kedua yang lebih dekat pada mekanisme fisik penyebab degradasi performa (lihat Subbab "Prosedur Pengukuran").
6. Cakupan penelitian dibatasi secara eksplisit pada perbandingan kebijakan `static` versus `none` pada lima instance LP terpilih dengan total 30 repetisi per kondisi (15 repetisi per blok, dengan dua blok counterbalanced A→B dan B→A; lihat Subbab "Prosedur Eksperimen") — setara dengan rencana "Tahun Pertama" pada versi proposal sebelumnya. Perluasan ke arah multi-node, NUMA-aware scheduling, maupun concurrent crossover pada GPU diklasifikasikan sebagai arah penelitian jangka panjang dan tidak termasuk dalam lingkup skripsi ini (lihat Subbab "Arah Penelitian Selanjutnya").

---

## Metode Penelitian

### Desain Eksperimen

Penelitian ini menggunakan pendekatan eksperimen empiris terkontrol dengan desain *within-subject*, yaitu membandingkan dua kebijakan CPU Manager pada satu unit infrastruktur yang sama untuk mengeliminasi variabel perancu yang muncul apabila kedua kondisi diuji pada perangkat keras yang berbeda. Dua kondisi yang dibandingkan adalah:

1. **Kondisi A (baseline):** CPU Manager dengan kebijakan `none`, yaitu penjadwalan CPU diserahkan penuh kepada Completely Fair Scheduler (CFS) bawaan Linux.
2. **Kondisi B (perlakuan):** CPU Manager dengan kebijakan `static`, yaitu container pada pod dengan kelas Guaranteed QoS dialokasikan CPU secara eksklusif melalui mekanisme `cpuset`.

Kedua kondisi diuji secara bergantian pada node Kubernetes yang sama, dengan kebijakan CPU Manager diubah melalui modifikasi `kubelet-config.yaml` diikuti restart layanan kubelet di antara dua sesi pengujian.

### Infrastruktur Eksperimen

Eksperimen dilaksanakan pada satu Virtual Machine Google Compute Engine dengan spesifikasi 8 vCPU (tipe *compute-optimized*, mis. `c2-standard-8`), dibiayai melalui kredit *free trial* Google Cloud Platform. Pemilihan instance tunggal dengan kuota 8 vCPU ini sejalan dengan batas penggunaan *concurrent* Compute Engine pada akun *Free Trial* GCP. Perlu dicatat di awal bahwa angka "8 vCPU" ini merujuk pada hardware thread (hyperthread) sebagaimana didefinisikan oleh penyedia cloud, bukan physical core — rincian topologi SMT dan NUMA yang sesungguhnya (termasuk berapa physical core yang mendasari ke-8 vCPU tersebut) baru diverifikasi pada Subbab "Karakterisasi Topologi Hardware", bukan diasumsikan di sini.

Di atas VM tersebut dibangun klaster Kubernetes *single-node* menggunakan kubeadm (bukan layanan terkelola seperti GKE), dengan pertimbangan utama: (a) kebutuhan kontrol penuh atas konfigurasi kubelet untuk mengubah `cpuManagerPolicy` dan merestart layanan kapan pun diperlukan tanpa proses persetujuan administratif; dan (b) menghindari biaya tambahan di luar kuota *free trial* yang melekat pada layanan klaster terkelola. *Taint* bawaan pada node *control-plane* dihapus agar Pod beban kerja dapat dijadwalkan pada node tunggal tersebut.

Konfigurasi sumber daya pada kedua kondisi disusun sebagai berikut: satu vCPU dicadangkan untuk proses sistem dan daemon Kubernetes melalui parameter `kubeReserved`, menyisakan tujuh vCPU yang dapat dialokasikan ke Pod beban kerja. Pod solver didefinisikan dengan `resources.requests.cpu = 4` dan `resources.limits.cpu = 4` (nilai integer), dengan dua pertimbangan:

1. **Kepatuhan terhadap Guaranteed QoS.** Kesamaan nilai requests dan limits dalam integer merupakan syarat agar Pod memenuhi kelas Guaranteed QoS dan berhak atas alokasi CPU eksklusif oleh kebijakan `static`.
2. **Kesetaraan paralelisme.** Angka 4 ini sekaligus menjadi nilai parameter `Threads` pada solver (lihat Subbab "Perangkat Lunak dan Parameter Solver"), memastikan tingkat paralelisme identik di kedua kondisi.

Tiga core sisanya (dari total tujuh yang tersedia) tidak digunakan oleh Pod solver. Perlu ditegaskan bahwa dalam eksperimen ini, satu-satunya Pod beban kerja yang dijadwalkan pada klaster adalah Pod solver itu sendiri — tidak ada Pod beban kerja lain yang dijalankan secara bersamaan, sejalan dengan prosedur pengujian sekuensial pada Subbab "Perangkat Lunak dan Parameter Solver". Dengan demikian, pada Kondisi B, tiga core yang tidak dialokasikan ke Pod solver tetap berada di shared pool tanpa terpakai oleh Pod lain, dan hanya tersedia bagi proses sistem non-Kubernetes pada node (jika ada) di luar cakupan `kubeReserved`. Pemilihan *core spesifik mana* dari tujuh yang tersedia yang akan dialokasikan ke Pod solver pada Kondisi B baru ditentukan **setelah** karakterisasi topologi hardware (lihat Subbab "Karakterisasi Topologi Hardware") selesai dijalankan — bukan diasumsikan terlebih dahulu — sehingga keempat core yang dipilih dapat diverifikasi benar-benar berasal dari physical core yang berbeda (tidak membentuk pasangan sibling hyperthread), berdasarkan data topologi aktual, bukan sekadar asumsi bahwa angka genap otomatis menghindari sibling pair.

### Karakterisasi Topologi Hardware

Sebelum eksperimen utama dilaksanakan, topologi hardware VM host dikarakterisasi terlebih dahulu menggunakan `lscpu` dan `numactl --hardware`, dengan dua tujuan:

1. **Verifikasi konfigurasi SMT/hyperthreading.** Pada platform cloud publik seperti Google Compute Engine, satu vCPU merepresentasikan satu hardware thread (hyperthread), bukan satu physical core penuh. Apabila dua vCPU yang dialokasikan ke Pod solver merupakan sibling thread pada physical core yang sama, kedua thread tersebut akan berbagi L1/L2 cache dan execution pipeline, sehingga menimbulkan resource contention yang tidak terkontrol meskipun kebijakan `static` telah diterapkan. Hasil `lscpu` (khususnya pemetaan `CPU(s)` terhadap `Core(s) per socket` dan `Thread(s) per core`) didokumentasikan untuk memastikan apakah kondisi ini terjadi pada VM yang digunakan, dan jika ya, core mana yang merupakan pasangan sibling.
2. **Verifikasi topologi NUMA.** Untuk workload yang memory-bound seperti crossover, latensi akses memori bergantung pada apakah memori yang diakses berada pada socket NUMA yang sama dengan core yang mengeksekusi thread. Hasil `numactl --hardware` didokumentasikan untuk memastikan apakah ke-8 vCPU berada pada satu node NUMA tunggal (kondisi yang umum terjadi pada instance 8 vCPU, namun perlu diverifikasi, bukan diasumsikan).

Berdasarkan hasil karakterisasi ini, empat core yang akan dialokasikan secara eksklusif ke Pod solver pada Kondisi B dipilih secara spesifik agar tidak membentuk pasangan sibling hyperthread satu sama lain, sejauh topologi VM memungkinkan. Apabila ternyata jumlah physical core berbeda yang tersedia di antara tujuh vCPU yang dicadangkan tidak cukup untuk menghindari sibling pair sepenuhnya, hal ini dicatat sebagai bagian dari karakterisasi lingkungan dan didiskusikan sebagai faktor yang berpotensi melemahkan (bukan meniadakan) efek isolasi dari kebijakan `static`.

Sebagai alternatif terhadap pemilihan core manual, penelitian ini juga mempertimbangkan opsi konfigurasi `--cpu-manager-policy-options=full-pcpus-only=true` pada kebijakan `static`. Opsi ini memaksa CPU Manager untuk mengalokasikan CPU dalam kelipatan physical core utuh (kedua sibling hyperthread-nya) saat Pod meminta CPU dalam jumlah integer [4]. Opsi ini secara teknis tersedia pada versi Kubernetes yang digunakan dalam eksperimen ini (1.27, dengan fitur `CPUManagerPolicyOptions` diaktifkan) [3], namun sengaja tidak diaktifkan, karena pemilihan core manual berdasarkan hasil `lscpu` (sebagaimana dijelaskan di atas) sudah mencukupi untuk mencapai tujuan yang sama — yaitu menghindari sibling pair — sekaligus menjaga kompatibilitas dengan skenario produksi umum di mana opsi `full-pcpus-only` jarang diaktifkan secara default.

### Perangkat Lunak dan Parameter Solver

Solver yang digunakan adalah **Gurobi Optimizer**, dengan lisensi *Academic Web License Service* (WLS). Karena lisensi akademik ini dibatasi maksimum dua sesi konkuren, seluruh pengujian dijalankan secara sekuensial — satu Pod solver pada satu waktu — sehingga tidak ada kontensi lisensi yang dapat mengacaukan pengukuran waktu.

Tiga parameter solver ditetapkan secara eksplisit dan dijaga identik pada kedua kondisi, untuk memastikan perbedaan hasil semata-mata berasal dari kebijakan CPU Manager, bukan dari perilaku algoritmik solver:

- **`Method = 2`**, memaksa solver menggunakan barrier murni (bukan *automatic* atau *concurrent*). Tanpa penetapan ini, Gurobi berpotensi memilih *concurrent optimizer* yang menjalankan simplex secara paralel dan terpisah dari barrier, sehingga titik transisi antara fase barrier dan fase crossover tidak dapat diidentifikasi secara andal.
- **`Crossover = 4`**, memaksa solver untuk mengeksekusi langkah *push* pada variabel primal dan dual secara eksplisit, mengesampingkan penentuan otomatis (`-1`) bawaan solver untuk memastikan fase crossover tereksekusi penuh.
- **`Threads = 4`**, jumlah thread yang digunakan solver ditetapkan secara eksplisit. Penetapan ini krusial karena perilaku default Gurobi (`Threads=0`) mendeteksi jumlah core dari affinity mask yang terlihat oleh proses, bukan dari kuota CFS [16]. Pada Kondisi A (`none`), cpuset container mewarisi seluruh core node (7 core), sehingga Gurobi akan mendeteksi 7 core dan menjalankan 7 thread. Pada Kondisi B (`static`), cpuset dibatasi ke core yang dialokasikan (4 core), sehingga Gurobi akan mendeteksi 4 core dan menjalankan 4 thread. Perbedaan jumlah thread ini merupakan confounder yang jauh lebih besar daripada efek migrasi thread yang hendak diukur. Dengan menetapkan `Threads = 4` secara eksplisit di kedua kondisi, paralelisme solver dipaksa identik, sehingga perbedaan performa yang teramati benar-benar berasal dari kebijakan CPU Manager, bukan dari tingkat paralelisme yang berbeda.

Penetapan ketiga parameter ini menjaga jumlah iterasi, urutan operasi, dan tingkat paralelisme solver tetap identik secara struktural pada kedua kondisi. Dengan demikian, setiap perbedaan waktu eksekusi maupun variabilitas (IQR) yang teramati antar-kondisi dapat diatribusikan pada kebijakan CPU Manager, bukan pada variasi perilaku algoritmik solver itu sendiri.

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

### Prosedur Eksperimen

Untuk mengontrol variabel perancu temporal (perubahan beban *noisy neighbor* di *physical host* sepanjang hari, *thermal drift* VM, dan variasi laten lainnya), urutan pengujian antar kondisi dirancang dengan strategi *block counterbalancing* sebagai berikut:

1. Seluruh 15 repetisi untuk Kondisi A dijalankan dalam satu blok, kemudian seluruh 15 repetisi untuk Kondisi B dijalankan dalam blok berikutnya (urutan A→B).
2. Prosedur yang sama diulang dengan urutan dibalik (B→A) pada sesi eksperimen terpisah di hari yang berbeda, menghasilkan total 30 repetisi per kondisi per instance (15 dari blok A→B dan 15 dari blok B→A).

Strategi ini dipilih karena *interleaving* A-B-A-B per repetisi tidak praktis secara operasional: setiap pergantian kebijakan CPU Manager memerlukan modifikasi `kubelet-config.yaml` dan restart kubelet, yang berdampak pada seluruh Pod di node (termasuk Pod sistem) dan memakan waktu beberapa menit per siklus. Dengan *block counterbalancing*, efek urutan dapat diestimasi dan dipisahkan dari efek perlakuan melalui analisis varians dua-faktor (urutan × kebijakan) pada data agregat.

Pelaporan hasil utama menggunakan median dari seluruh 30 repetisi per kondisi (gabungan kedua blok), dengan IQR sebagai ukuran variabilitas. Apabila ditemukan perbedaan sistematis antara blok A→B dan B→A yang tidak dapat dijelaskan oleh variasi acak, hal ini akan didiskusikan secara eksplisit sebagai bukti adanya efek temporal yang tidak terkontrol.

### Prosedur Pengukuran

Setiap instance benchmark dijalankan secara berulang sebanyak **15 kali per blok** (sehingga 30 kali per kondisi secara keseluruhan, lihat Subbab "Prosedur Eksperimen") di dalam satu Pod solver yang sama per blok (bukan Pod baru per repetisi). Keputusan ini diambil untuk menghindari *cold start overhead* dari inisialisasi lisensi Gurobi (koneksi ke WLS) dan alokasi memori awal yang dapat menambah variasi antar repetisi. Namun, konsekuensi dari pendekatan ini adalah potensi *warming effect* pada cache OS (page cache) dan cache CPU antar repetisi dalam satu blok. Untuk memitigasi hal ini:

1. Seluruh file instance (`.mps`) telah dimuat ke tmpfs (`emptyDir` bertipe Memory), sehingga *page cache* yang memanas adalah cache RAM-to-RAM, bukan disk-to-RAM — efeknya jauh lebih kecil dibandingkan jika menggunakan disk persisten.
2. Antara setiap repetisi, dilakukan *flush* (`drop_caches`) pada level host setelah eksekusi selesai, untuk mengembalikan status cache ke kondisi yang sedekat mungkin dengan keadaan awal [17].
3. Sebagai pemeriksaan, *trend analysis* dilakukan pada data deret waktu repetisi ke-1 sampai ke-15 dalam tiap blok; apabila terdeteksi tren monoton (misal: waktu terus menurun seiring repetisi), hal ini akan dilaporkan sebagai bukti *warming effect* residual yang tidak sepenuhnya tereliminasi.

Jumlah 15 repetisi per blok dipilih sebagai titik tengah antara kebutuhan daya statistik yang memadai untuk uji nonparametrik (Mann-Whitney U memerlukan minimal sekitar 8–10 sampel per grup agar valid) dan keterbatasan waktu eksekusi akibat sifat sekuensial pengujian (lihat Subbab "Perangkat Lunak dan Parameter Solver").

Pengukuran utama difokuskan pada *wall-clock crossover time*. Pemisahan waktu fase barrier dan fase crossover dilakukan melalui **instrumentasi callback Gurobi** (`GRB.Callback.RUNTIME` pada *callback* `BARRIER` dan `SIMPLEX`), bukan melalui pembacaan log teks solver — log Gurobi hanya mencatat *timestamp* dengan granularitas satu detik [1][2], yang terlalu kasar mengingat durasi fase crossover pada sejumlah kasus dapat berlangsung sub-detik. Parsing log teks tetap dijalankan sebagai pemeriksaan silang sekunder; apabila terdapat selisih signifikan antara kedua sumber pengukuran, *run* yang bersangkutan ditandai untuk pemeriksaan manual sebelum dimasukkan ke analisis.

Metrik pendukung yang dikumpulkan secara simultan dari level host (bukan dari dalam container, karena keterbatasan visibilitas Pod terhadap statistik cgroup node) meliputi:

- **Involuntary context switches**, diperoleh dari `/proc/[pid]/status`, sebagai *proksi* frekuensi gangguan penjadwalan terhadap thread solver pada rentang waktu fase crossover saja. Perlu ditegaskan bahwa metrik ini merupakan **proksi tidak langsung**: nilai `nonvoluntary_ctxt_switches` mengindikasikan bahwa CFS melakukan preemption terhadap suatu thread (baik karena time slice yang habis maupun karena task berprioritas lebih tinggi memasuki runqueue), namun tidak secara langsung membuktikan bahwa preemption tersebut diikuti oleh perpindahan thread ke core yang berbeda (migrasi). Untuk itu, metrik ini dilengkapi oleh metrik hardware performance counter di bawah sebagai proksi kedua yang lebih dekat pada mekanisme fisik penyebab degradasi performa (lihat Batasan Masalah poin 5).
- **Hardware performance counters**, diperoleh melalui `perf stat` yang dijalankan dari host terhadap PID proses solver di dalam container, mencakup minimal `cache-misses`, `cache-references`, `L1-dcache-load-misses`, `L1-dcache-loads`, `instructions`, dan `cycles`. Mengingat sifat crossover yang memory-bound, cache miss rate merupakan indikator yang lebih dekat secara fisik terhadap mekanisme degradasi performa dibandingkan involuntary context switches semata. Metrik ini digunakan untuk memperkuat (bukan menggantikan) interpretasi terhadap proksi context switch.
- **CFS throttling statistics** (`nr_throttled` dan `throttled_usec`), diperoleh dari `cpu.stat` pada cgroup container, dievaluasi untuk memverifikasi dan mengisolasi bahwa pelambatan yang terjadi bukan merupakan artefak dari pembatasan kuota CPU (CFS quota pauses), melainkan murni dari overhead migrasi thread.
- **Iteration count** fase barrier, untuk memverifikasi bahwa kedua kondisi mencapai titik awal crossover yang setara.

Seluruh metrik pendukung di atas dikumpulkan khusus pada rentang waktu fase crossover (ditentukan melalui instrumentasi callback yang sama), bukan pada keseluruhan durasi eksekusi solver, agar atribusi metrik terhadap fase crossover tetap presisi.

### Analisis Data

**Rumusan Masalah 1 (pengaruh CPU pinning terhadap waktu crossover).** Perbandingan *wall-clock crossover time* antara Kondisi A dan B per instance dianalisis secara deskriptif (median, IQR) dan diuji signifikansinya menggunakan uji nonparametrik Mann-Whitney U, mengingat ukuran sampel yang terbatas dan kemungkinan distribusi yang tidak normal akibat *noise* infrastruktur cloud. Karena pengujian dilakukan terpisah pada setiap instance (sehingga terdapat beberapa uji hipotesis sejenis), nilai-p dikoreksi menggunakan koreksi Bonferroni (α disesuaikan menjadi 0,05 dibagi jumlah instance) untuk mengontrol *family-wise error rate*; nilai-p tanpa koreksi tetap dilaporkan sebagai pembanding eksploratif. Pengujian ini secara langsung menjawab Hipotesis H1 pada Subbab 1.4.

**Rumusan Masalah 2 (korelasi context switches/migrasi thread dengan waktu crossover).** Korelasi Spearman antara *involuntary context switches* dan *crossover time* dihitung **secara terpisah di dalam masing-masing kondisi** (bukan digabung lintas Kondisi A dan B). Pemisahan ini perlu dilakukan karena penggabungan data lintas kondisi berisiko menghasilkan korelasi semu yang sebenarnya hanya mencerminkan perbedaan rata-rata antar kondisi (*confounding* akibat perbedaan tingkat keduanya, bukan hubungan sebab-akibat yang sesungguhnya di dalam satu kondisi). Sebagai pelengkap dan triangulasi, korelasi Spearman yang sama juga dihitung antara *cache miss rate* (dari `perf stat`) dengan *crossover time*, untuk menilai konsistensi arah hubungan antara kedua proksi yang berbeda level abstraksinya (level OS-scheduler vs. level hardware).

**Rumusan Masalah 3 (stabilitas fase barrier antar konfigurasi).** Selain perbandingan *iteration count* fase barrier, durasi fase barrier (hasil instrumentasi callback yang sama) juga diuji dengan Mann-Whitney U antar kondisi per instance. Hasil yang **tidak signifikan** pada uji ini mendukung asumsi bahwa fase barrier relatif stabil, sehingga perbedaan performa total yang teramati dapat diatribusikan pada fase crossover. Karena uji ini bersifat *sanity check* (bukan pengujian hipotesis utama), koreksi Bonferroni tidak diterapkan pada RM3; nilai-p dilaporkan mentah dan diinterpretasikan secara deskriptif, dengan ambang signifikansi nominal α=0,05 sebagai panduan, bukan sebagai keputusan biner.

**Rumusan Masalah 4 (besar kontribusi kebijakan CPU Manager terhadap variasi performa antar instance).** Untuk setiap instance, dihitung *effect size* berupa korelasi *rank-biserial* (turunan langsung dari statistik U pada uji Mann-Whitney) serta persentase reduksi median *crossover time* dari Kondisi A ke Kondisi B. Kedua ukuran ini kemudian dibandingkan **antar instance** untuk mengamati apakah besar pengaruh CPU pinning berasosiasi secara sistematis dengan karakteristik instance (ukuran dan sparsity, lihat Subbab "Objek Uji") — bukan sekadar menyimpulkan signifikan/tidak signifikan, melainkan mengkuantifikasi seberapa besar kontribusinya pada masing-masing karakteristik instance.

### Keterbatasan Metodologis

Karena eksperimen dilaksanakan pada infrastruktur *virtual machine* di lingkungan *cloud* publik (bukan *bare-metal* dedicated), satu vCPU pada Google Compute Engine merepresentasikan satu *hyperthread*, bukan satu *physical core* penuh. Sebagaimana dijelaskan pada Subbab "Karakterisasi Topologi Hardware", apabila dua atau lebih vCPU yang dialokasikan secara eksklusif ke Pod solver oleh kebijakan `static` merupakan sibling thread pada physical core yang sama, kedua thread tersebut akan tetap berbagi L1/L2 cache dan execution pipeline meskipun keduanya berada di luar shared pool — sebuah bentuk resource contention yang tidak dapat dieliminasi oleh mekanisme `cpuset` semata. Karakteristik *cache locality* hasil pinning pada konteks ini berpotensi berbeda dari pinning pada *physical core* di lingkungan *bare-metal*, dan kondisi sibling-pairing pada VM yang digunakan akan didokumentasikan secara eksplisit sebagai bagian dari pelaporan hasil agar pembaca dapat menilai sejauh mana temuan dapat digeneralisasi ke lingkungan bare-metal.

Selain itu, meskipun klaster Kubernetes yang dibangun bersifat *single-tenant* pada level Pod, VM tetap berbagi *physical host* dengan *virtual machine* milik penyewa lain di sisi penyedia *cloud*, sehingga *noise* residual akibat *co-location* tidak dapat dieliminasi secara penuh — hanya diminimalkan melalui pengulangan pengukuran dan pelaporan median.

---

## Arah Penelitian Selanjutnya

Skripsi ini secara eksplisit dibatasi pada pengukuran empiris pengaruh CPU pinning (CPU Manager `static`) terhadap waktu eksekusi fase crossover LP solver di Kubernetes, dibandingkan baseline CFS (`none`), pada lima instance Mittelmann terpilih dengan total 30 repetisi per kondisi (lihat Batasan Masalah poin 6). Beberapa arah perluasan berikut diidentifikasi sebagai topik penelitian jangka panjang yang berada di luar cakupan skripsi ini, namun relevan untuk penelitian lanjutan (tesis, disertasi, atau hibah penelitian multi-tahun):

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

[14] S. Deng et al., "Cloud-Native Computing: A Survey from the Perspective of Services," *arXiv preprint arXiv:2306.14402*, 2023.

[15] U. Revilla-Duarte, M. A. Ramírez-Salinas, L. A. Villa-Vargas, and A. Tchernykh, "Proactive load balancing to reduce unnecessary Thread Migrations on Chip Multi-processor (CMP) systems," *Computación y Sistemas*, vol. 28, no. 2, Jun. 2024. doi: 10.13053/cys-28-2-4403

[16] Gurobi Optimization, LLC, "Specifying threads to be utilized on a remote machine," *Gurobi Help Center*, Mar. 5, 2024. [Online]. Available: https://support.gurobi.com/hc/en-us/community/posts/23017466807697-Specifying-threads-to-be-utilized-on-a-remote-machine

[17] The Linux Kernel documentation, "Documentation for /proc/sys/vm/ — drop_caches." [Online]. Available: https://www.kernel.org/doc/html/latest/admin-guide/sysctl/vm.html#drop-caches
