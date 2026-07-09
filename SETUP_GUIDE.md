# Panduan Setup Infrastruktur — Eksperimen CPU Pinning x Crossover LP

Ikuti langkah ini SECARA URUT. Bagian yang butuh login interaktif (gcloud auth,
Gurobi WLS) tidak bisa diotomasi penuh — wajar untuk dijalankan manual.

---

## Bagian 1 — Setup VM Google Cloud (dijalankan di laptop/komputer Anda)

### 1.1 Install & autentikasi gcloud CLI (kalau belum)
```bash
# Cek apakah sudah terinstal
gcloud version || echo "Belum terinstal, lihat https://cloud.google.com/sdk/docs/install"

gcloud auth login
gcloud config set project <PROJECT_ID_ANDA>

```

### 1.2 Cek dan minta kuota CPU di region pilihan (PENTING — sering jadi penghambat)

```bash
# Ganti REGION dan PROJECT_ID sesuai konfigurasi Anda.
# Skrip memeriksa kuota untuk c2-standard-8 (8 vCPU, Option 1/2) DAN
# c2-standard-16 (16 vCPU, Option 3) sekaligus, sehingga eskalasi nanti
# tidak menunggu approval kedua di tengah eksperimen.
REGION="us-central1" PROJECT_ID="<PROJECT_ID_ANDA>" \
  bash scripts/check_quota.sh

```

Skrip menampilkan ringkasan ketersediaan kuota dan — jika diperlukan —
menawarkan untuk mengajukan `gcloud beta quotas preferences create` secara
langsung (ketik `y` dan masukkan email). Approval biasanya memakan waktu
menit hingga jam untuk kenaikan kecil pada akun trial.

> **Catatan kuota Option 3:** GCP menghitung kuota berdasarkan jumlah vCPU
> nominal machine type, bukan jumlah thread yang terlihat guest setelah SMT
> dinonaktifkan. `c2-standard-8` selalu dihitung 8 vCPU; `c2-standard-16`
> selalu dihitung 16 vCPU.

Tunggu konfirmasi approval dari `gcloud beta quotas preferences describe
<preference-id>` sebelum melanjutkan ke §​1.3.

### 1.3 Buat VM (Default — Option 1, SMT Aktif)

Ini adalah jalur default. SMT dibiarkan aktif sehingga VM memiliki **8 vCPU**
(2 hardware thread per physical core). Karakterisasi topologi di §​1.7 akan
menentukan pasangan sibling mana yang harus dihindari saat memilih core untuk
solver, dan keputusan akhir selalu dikonfirmasi manual sebelum setup Kubernetes.

```bash
ZONE="us-central1-a"   # sesuaikan dengan region yang quota-nya cukup

gcloud compute instances create crossover-experiment-vm \
  --zone="$ZONE" \
  --machine-type="c2-standard-8" \
  --min-cpu-platform="Intel Cascade Lake" \
  --maintenance-policy=TERMINATE \
  --no-restart-on-failure \
  --image-family="ubuntu-2204-lts" \
  --image-project="ubuntu-os-cloud" \
  --boot-disk-size="50GB" \
  --boot-disk-type="pd-ssd" \
  --tags="crossover-experiment"

```

> **`--maintenance-policy=TERMINATE`** mencegah live migration host yang
> tidak terdeteksi selama sesi pengujian, karena live migration dapat mengubah
> pemetaan vCPU ke physical core dan mereset status cache/TLB secara diam-diam.
> Jangan menambahkan `--threads-per-core=1` di sini kecuali hasil karakterisasi
> topologi (§​1.7) menunjukkan Option 1 tidak layak dan Anda memilih Option 2.
> `--threads-per-core` bersifat creation-time-only — tidak bisa diubah in-place;
> jika diperlukan, VM harus dibuat ulang (lihat §​1.3.1 dan §​1.3.2 di bawah).

### 1.3.1 Kontingensi Option 2 — SMT Dinonaktifkan (c2-standard-8)

**Jalankan ini HANYA jika** karakterisasi topologi di §​1.7 menghasilkan
`infra/topology-decision.txt` berisi `option2`. Hapus VM yang ada, lalu buat
ulang dengan `--threads-per-core=1`:

```bash
ZONE="us-central1-a"

# Hapus VM yang ada (pastikan tidak ada data eksperimen yang belum disalin)
gcloud compute instances delete crossover-experiment-vm --zone="$ZONE" --quiet

# Buat ulang dengan SMT dinonaktifkan
gcloud compute instances create crossover-experiment-vm \
  --zone="$ZONE" \
  --machine-type="c2-standard-8" \
  --min-cpu-platform="Intel Cascade Lake" \
  --threads-per-core=1 \
  --maintenance-policy=TERMINATE \
  --no-restart-on-failure \
  --image-family="ubuntu-2204-lts" \
  --image-project="ubuntu-os-cloud" \
  --boot-disk-size="50GB" \
  --boot-disk-type="pd-ssd" \
  --tags="crossover-experiment"

```

> Dengan `--threads-per-core=1`, VM hanya melihat **4 vCPU** (1 per physical
> core). Jalankan ulang `characterize_topology.py` setelah VM baru aktif untuk
> mengonfirmasi topologi, lalu lanjutkan setup dari §​1.7. GCP tetap menghitung
> kuota sebagai 8 vCPU (berdasarkan machine type nominal, bukan thread guest).

### 1.3.2 Kontingensi Option 3 — Eskalasi ke c2-standard-16

**Jalankan ini HANYA jika** karakterisasi topologi di §​1.7 menghasilkan
`infra/topology-decision.txt` berisi `option3`. Membutuhkan kuota 16 vCPU
(pastikan `check_quota.sh` sudah melaporkan ketersediaan kuota ini, atau
ajukan terlebih dahulu):

```bash
ZONE="us-central1-a"

# Hapus VM yang ada
gcloud compute instances delete crossover-experiment-vm --zone="$ZONE" --quiet

# Buat ulang sebagai c2-standard-16
gcloud compute instances create crossover-experiment-vm \
  --zone="$ZONE" \
  --machine-type="c2-standard-16" \
  --min-cpu-platform="Intel Cascade Lake" \
  --maintenance-policy=TERMINATE \
  --no-restart-on-failure \
  --image-family="ubuntu-2204-lts" \
  --image-project="ubuntu-os-cloud" \
  --boot-disk-size="50GB" \
  --boot-disk-type="pd-ssd" \
  --threads-per-core=1 \
  --tags="crossover-experiment"

```

> `c2-standard-16` dengan `--threads-per-core=1` mematikan SMT sehingga VM
> hanya melihat 8 vCPU murni (1 thread per physical core). Ini adalah
> SATU-SATUNYA cara memaksa Kubelet CPU Manager (yang punya tabiat selalu
> mengutamakan "full physical cores") untuk memberikan 4 core fisik yang
> benar-benar terisolasi kepada Gurobi tanpa ada SMT packing.

### 1.4 Buka akses SSH (firewall) jika belum ada rule default

```bash
# Dapatkan IP publik Anda (misal dari curl -s ifconfig.me)
MY_IP="<IP_PUBLIK_ANDA>"

gcloud compute firewall-rules create allow-ssh-crossover-experiment \
  --network=default \
  --allow=tcp:22 \
  --source-ranges="${MY_IP}/32" \
  --target-tags=crossover-experiment

```

> Catatan keamanan: Pada perintah di atas, ganti `<IP_PUBLIK_ANDA>` dengan alamat IP publik koneksi internet Anda. Ini membatasi akses SSH hanya dari IP Anda, jauh lebih aman dari `0.0.0.0/0`.

### 1.5 Transfer file proyek ke VM

Jalankan perintah ini di **laptop Anda** (bukan di dalam VM) untuk mengirim seluruh file proyek crossover-experiment ke home directory VM:

```bash
ZONE="us-central1-a"   # sesuaikan dengan zone VM Anda
gcloud compute scp --recurse ./crossover-experiment crossover-experiment-vm:~/ --zone="$ZONE"

```

### 1.6 SSH ke VM

Jalankan perintah ini di **laptop Anda** untuk masuk ke dalam shell VM:

```bash
ZONE="us-central1-a"   # sesuaikan dengan zone VM Anda
gcloud compute ssh crossover-experiment-vm --zone="$ZONE"

```

**Semua langkah selanjutnya (Bagian 2 dst.) dijalankan DI DALAM VM ini.**

### 1.7 Karakterisasi topologi dan validasi PMU (Phase 1 — DI DALAM VM)

Langkah ini WAJIB dijalankan sebelum setup Kubernetes. Hasilnya menentukan
konfigurasi kubelet yang akan dirender di akhir bagian ini (§​1.7.3).

#### 1.7.1 Install prasyarat minimal

```bash
sudo apt-get update -q
sudo apt-get install -y gcc numactl linux-tools-generic linux-tools-$(uname -r)

```

#### 1.7.2 Izinkan perf mengakses PMU

Diperlukan agar `validate_pmu_fidelity.py` dan nantinya `collect_system_metrics.py`
dapat membaca hardware performance counter:

```bash
echo 'kernel.perf_event_paranoid = -1' | sudo tee /etc/sysctl.d/99-perf.conf
sudo sysctl -p /etc/sysctl.d/99-perf.conf

# Verifikasi (harus return -1):
sysctl kernel.perf_event_paranoid

```

#### 1.7.3 Jalankan karakterisasi topologi

```bash
cd ~/crossover-experiment
python3 scripts/characterize_topology.py

```

Skrip mencetak ringkasan topologi, daftar sibling pair, analisis kelayakan
ketiga opsi, dan rekomendasi. **Baca seluruh output.**

⚠️ **HENTI MANUAL:** Setelah membaca output (dan berdiskusi dengan pembimbing
jika perlu), tetapkan keputusan dengan menulis **salah satu** dari:

```bash
# Pilih SALAH SATU sesuai rekomendasi/keputusan Anda:
echo "option1" > infra/topology-decision.txt   # SMT aktif, pilih core manual
echo "option2" > infra/topology-decision.txt   # SMT off (butuh recreate VM — lihat §​1.3.1)
echo "option3" > infra/topology-decision.txt   # c2-standard-16 (butuh recreate VM — lihat §​1.3.2)

```

Jika memilih Option 2 atau Option 3, hentikan proses ini, buat ulang VM sesuai
§​1.3.1 atau §​1.3.2, SSH kembali, dan mulai lagi dari §​1.7.1.

#### 1.7.4 Validasi fidelitas hardware performance counter (PMU)

```bash
python3 scripts/validate_pmu_fidelity.py

```

Skrip mengompilasi micro-benchmark, menjalankan dua pola akses memori yang
kontras di bawah `perf stat`, dan mengevaluasi go/no-go. Hasilnya tersimpan
di `infra/pmu-validation-report.json`.

- **Verdict GO / DEGRADED:** lanjutkan ke §​1.7.5. Metrik PMU valid.
- **Verdict NO-GO:** lanjutkan ke §​1.7.5. Hanya `involuntary context switches`
  yang digunakan sebagai proksi dalam analisis utama; keterbatasan ini akan
  didokumentasikan secara eksplisit di Subbab 'Keterbatasan Metodologis'.

#### 1.7.5 Render konfigurasi kubelet

Berdasarkan `infra/topology-decision.txt` yang sudah ditetapkan, render
konfigurasi kubelet final (menambahkan `reservedSystemCPUs` dan menghapus
`full-pcpus-only` yang tidak sesuai proposal):

```bash
python3 scripts/render_kubelet_configs.py

```

Hasil render tersimpan di `kubelet-configs/rendered/`. Konfigurasi ini yang
akan disalin ke `/var/lib/kubelet/config.yaml` pada §​2.7.

---

## Bagian 2 — Setup Kubernetes single-node (kubeadm) — DI DALAM VM

### 2.1 Update sistem dan install dependensi dasar

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg \
  software-properties-common linux-tools-generic linux-tools-$(uname -r)

```

> `linux-tools-*` diperlukan untuk `perf stat` yang dijalankan oleh
> `collect_system_metrics.py` di host untuk mengukur cache misses dan hardware
> performance counters.

### 2.2 Disable swap (syarat wajib kubelet)

```bash
sudo swapoff -a
sudo sed -i '/ swap / s/^/#/' /etc/fstab

```

### 2.2.1 Verifikasi topologi CPU (ringkas)

Karakterisasi topologi lengkap sudah dilakukan di §1.7.3 (`characterize_topology.py`)
dan hasilnya tersimpan di `infra/topology-report.json`. Langkah ini hanya
memverifikasi secara cepat bahwa VM yang aktif konsisten dengan laporan tersebut:

```bash
# Jumlah vCPU yang terlihat harus sesuai opsi yang dipilih:
#   Option 1 (c2-standard-8, SMT aktif)   → 8 vCPU
#   Option 2 (c2-standard-8, SMT nonaktif) → 4 vCPU
#   Option 3 (c2-standard-16, SMT aktif)   → 16 vCPU
nproc

# Konfirmasi threads-per-core sesuai opsi:
lscpu | grep -E 'Thread|Core|Socket|CPU\(s\)'

```

> **Catatan tentang CPU Governor:** Pada VM GCP berbasis KVM, `cpupower frequency-set`
> **tidak berpengaruh** karena frekuensi CPU dikelola oleh hypervisor, bukan guest OS.
> Variabel ini tidak bisa dikontrol dari dalam VM — itulah sebabnya c2-standard-8
> dipilih (Compute Optimized, frekuensi turbo konsisten per GCP SLA).

### 2.3 Install containerd

```bash
sudo apt-get install -y containerd
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml
# WAJIB: aktifkan SystemdCgroup agar konsisten dengan cgroup driver kubelet
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd
sudo systemctl enable containerd

```

### 2.4 Setup kernel modules & sysctl untuk networking Kubernetes

```bash
cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
sudo modprobe overlay
sudo modprobe br_netfilter

cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system

```

### 2.5 Install kubeadm, kubelet, kubectl

```bash
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key | \
  sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /' | \
  sudo tee /etc/apt/sources.list.d/kubernetes.list

sudo apt-get update
sudo apt-get install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl

```

> Catatan: ganti `v1.36` di atas dengan versi minor stabil terbaru jika
> sudah ada rilis lebih baru saat Anda membaca ini — cek di
> https://kubernetes.io/releases/

### 2.6 Install & konfigurasi crictl

`scripts/collect_system_metrics.py` BERGANTUNG pada `crictl` untuk menemukan
PID host dari container solver.

```bash
CRICTL_VERSION="v1.36.0"   # samakan major.minor dengan versi Kubernetes di 2.5
curl -fsSL "https://github.com/kubernetes-sigs/cri-tools/releases/download/${CRICTL_VERSION}/crictl-${CRICTL_VERSION}-linux-amd64.tar.gz" \
  -o /tmp/crictl.tar.gz
sudo tar zxvf /tmp/crictl.tar.gz -C /usr/local/bin
rm /tmp/crictl.tar.gz

cat <<EOF | sudo tee /etc/crictl.yaml
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 10
debug: false
EOF

# Verifikasi crictl bisa bicara dengan containerd:
sudo crictl version
sudo crictl pods

```

### 2.7 Inisialisasi cluster

```bash
sudo kubeadm init --pod-network-cidr=10.244.0.0/16

# Setelah selesai, set kubeconfig untuk user biasa (BUKAN root):
mkdir -p "$HOME/.kube"
sudo cp -i /etc/kubernetes/admin.conf "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"

```

> ⚠️ **PENTING — Konfigurasi Kubelet Eksperimen:**
> Untuk single-node eksperimen ini, kita menyalin file konfigurasi Kubelet secara langsung ke `/var/lib/kubelet/config.yaml`. Karena tidak ada rencana melakukan upgrade cluster (`kubeadm upgrade`), cara langsung ini aman dan praktis.

```bash
# Gunakan konfigurasi yang sudah di-RENDER oleh render_kubelet_configs.py (§1.7.5),
# bukan file mentah di kubelet-configs/. File rendered sudah berisi
# reservedSystemCPUs yang tepat berdasarkan topologi aktual VM.
sudo cp ~/crossover-experiment/kubelet-configs/rendered/condition-A-none.yaml \
  /var/lib/kubelet/config.yaml

# Nyalakan ulang kubelet agar perubahan terbaca:
sudo systemctl restart kubelet

# Verifikasi kubelet aktif dan setting terbaca:
sudo systemctl is-active kubelet
grep -E 'cpuManagerPolicy|systemReserved|kubeReserved|reservedSystemCPUs|cpuManagerReconcilePeriod' \
  /var/lib/kubelet/config.yaml

```

> **Catatan urutan:** Lakukan ini SEBELUM §2.8 (install Flannel) agar node
> sudah dalam konfigurasi eksperimen yang benar sejak awal.
>
> **Mengapa `rendered/`?** File di `kubelet-configs/` adalah template — belum
> berisi nilai `reservedSystemCPUs` yang spesifik terhadap topologi VM ini.
> `render_kubelet_configs.py` (§1.7.5) mengisi nilai tersebut berdasarkan
> `infra/topology-report.json` dan menulis hasilnya ke `kubelet-configs/rendered/`.

### 2.8 Install CNI plugin (Flannel — sederhana, cukup untuk single-node)

```bash
# Pin ke versi spesifik — JANGAN pakai /releases/latest karena mutable
# Cek versi stable terbaru di: https://github.com/flannel-io/flannel/releases
FLANNEL_VERSION="v0.28.5"
kubectl apply -f "https://github.com/flannel-io/flannel/releases/download/${FLANNEL_VERSION}/kube-flannel.yml"

```

### 2.9 Hapus taint control-plane (supaya Pod bisa dijadwalkan di node ini)

```bash
kubectl taint nodes --all node-role.kubernetes.io/control-plane-

```

### 2.10 Verifikasi cluster siap

```bash
kubectl get nodes -o wide
# Tunggu sampai STATUS = Ready (bisa 1-2 menit setelah Flannel terpasang)
kubectl get pods -A

```

### 2.11 Berikan akses sudo tanpa password untuk crictl

`run_experiment.sh` memanggil `collect_system_metrics.py` di background sambil
menunggu Pod selesai — proses ini perlu menjalankan `sudo crictl` tanpa
diinterupsi prompt password.

```bash
echo "$USER ALL=(ALL) NOPASSWD: /usr/local/bin/crictl" | sudo tee /etc/sudoers.d/crictl-nopasswd
sudo chmod 440 /etc/sudoers.d/crictl-nopasswd

```

### 2.12 Verifikasi akses `perf stat` di host

`kernel.perf_event_paranoid = -1` sudah disetel di §1.7.2 dan persistensi
lintas reboot sudah dikonfigurasi via `/etc/sysctl.d/99-perf.conf`. Langkah
ini hanya memverifikasi nilai masih aktif setelah setup Kubernetes:

```bash
# Harus mengembalikan -1:
sysctl kernel.perf_event_paranoid

# Tes perf bisa mengukur PID arbitrer dari host (cara yang sama dengan
# collect_system_metrics.py — berbeda dari validate_pmu_fidelity.py yang
# mengukur proses milik sendiri):
sudo perf stat -e cache-misses,instructions,cycles -p $$ sleep 0.1 2>&1 | \
  grep -E 'cache-misses|instructions|cycles|not supported'

```

> ⚠️ Fidelitas PMU sudah divalidasi secara menyeluruh di §1.7.4
> (`validate_pmu_fidelity.py`). Hasilnya tersimpan di
> `infra/pmu-validation-report.json`. Jika verdict adalah NO-GO, field
> `perf_metrics` di output `collect_system_metrics.py` akan dikosongkan
> pada fase analisis — hanya `involuntary context switches` yang digunakan.

---

## Bagian 3 — Build & load image solver — DI DALAM VM

### 3.1 Install Docker (untuk build image; containerd dipakai runtime Kubernetes)

```bash
sudo apt-get update
sudo apt-get install -y docker.io
sudo usermod -aG docker "$USER"
newgrp docker   # refresh group membership

```

> ⚠️ **Troubleshooting: dpkg error setelah install Docker**
> Jika muncul `E: Sub-process /usr/bin/dpkg returned an error code (1)`, jalankan:
> `sudo DEBIAN_FRONTEND=noninteractive dpkg --configure --force-confdef --force-confold -a`

### 3.2 Sinkronisasi/Transfer ulang file proyek (Opsional)

File proyek sudah ditransfer pada langkah 1.5. Namun jika Anda melakukan perubahan file di laptop Anda dan ingin menyinkronkannya kembali ke VM, jalankan perintah ini di **laptop Anda**:

```bash
ZONE="us-central1-a"   # sesuaikan dengan zone VM Anda
gcloud compute scp --recurse ./crossover-experiment crossover-experiment-vm:~/ --zone="$ZONE"

```

### 3.3 Build image Docker — DI DALAM VM

```bash
cd ~/crossover-experiment
docker build -t crossover-experiment/gurobi-solver:v1.0.0 .

```

### 3.4 Import image ke containerd

```bash
docker save crossover-experiment/gurobi-solver:v1.0.0 -o /tmp/solver-image.tar
sudo ctr -n k8s.io images import /tmp/solver-image.tar
rm /tmp/solver-image.tar

```

---

## Bagian 4 — Setup Kubernetes resources & kredensial — DI DALAM VM

### 4.1 Buat namespace dan storage

```bash
cd ~/crossover-experiment
kubectl apply -f manifests/00-namespace.yaml
sudo mkdir -p /mnt/experiment-data/{instances,results}
sudo chmod -R 777 /mnt/experiment-data
kubectl apply -f manifests/02-storage.yaml

```

### 4.2 Buat Secret kredensial Gurobi WLS

```bash
kubectl create secret generic gurobi-wls-credentials \
  --namespace=crossover-experiment \
  --from-literal=GRB_WLSACCESSID='<isi_dari_portal_gurobi>' \
  --from-literal=GRB_WLSSECRET='<isi_dari_portal_gurobi>' \
  --from-literal=GRB_LICENSEID='<isi_dari_portal_gurobi>'

```

### 4.3 Unduh instance benchmark Mittelmann

Pastikan Anda memiliki `bzip2` dan telah mengkompilasi *utility* `emps` yang digunakan untuk mengekstrak format kompresi netlib ke format MPS biasa:

```bash
# Install bzip2 (jika belum ada)
sudo apt update && sudo apt install -y bzip2

# Kompilasi emps dari source
sudo gcc -O2 -m64 -o /usr/local/bin/emps emps.c

# Jika tidak punya akses root, Anda bisa mengkompilasi di direktori lokal
# dan menjalankan skrip dengan variabel environment EMPS_BIN:
# gcc -O2 -m64 -o emps emps.c
# EMPS_BIN=./emps bash scripts/download_benchmarks.sh

# Eksekusi skrip unduh (menggunakan emps di /usr/local/bin)
bash scripts/download_benchmarks.sh
```

---

## Bagian 5 — Verifikasi & Pemulihan

### 5.1 Pastikan kebijakan default node adalah `none`

```bash
grep cpuManagerPolicy /var/lib/kubelet/config.yaml || echo "belum diset eksplisit, default = none"

```

### 5.2 Verifikasi alokasi CPU dan jalankan smoke test

Dengan c2-standard-8 dan SMT aktif (Option 1), VM melihat **8 vCPU**.
Kubelet mereservasi 1 core via `reservedSystemCPUs` (dikonfigurasikan oleh
`render_kubelet_configs.py` berdasarkan topologi aktual), sehingga:

```
Total vCPU        : 8
reservedSystemCPUs: 1  (core tunggal untuk daemon sistem & Kubernetes)
systemReserved    : 500m CPU (cgroup weight, bukan core eksklusif)
kubeReserved      : 500m CPU (cgroup weight, bukan core eksklusif)
Allocatable CPU   : 8 - 1 = 7 CPU
```

Pod solver meminta **4 CPU** (`resources.requests.cpu = limits.cpu = 4`,
Guaranteed QoS), menyisakan 3 CPU di shared pool untuk daemon sistem.

> Jika Option 2 (SMT off, 4 vCPU) yang dipilih, Allocatable = 4 - 1 = 3 CPU,
> dan Pod solver meminta 2 CPU (perlu revisi rencana alokasi resource).
> Jika Option 3 (c2-standard-16 dengan SMT off, 8 vCPU), Allocatable = 8 - 1 = 7 CPU,
> dan Pod solver tetap meminta 4 CPU (sisa 3 core fisik di shared pool).

```bash
# Verifikasi allocatable CPU node
# Option 1: harus 7 | Option 2: harus 3 | Option 3: harus 7
kubectl get node -o jsonpath='{.items[0].status.allocatable.cpu}'

# Verifikasi headroom dan reserved CPU
kubectl describe node | grep -A8 "Allocated resources"

chmod +x scripts/*.sh
# Smoke test: 1 repetisi, instance pertama, 4 vCPU (sesuai proposal)
bash scripts/run_experiment.sh none 1 4

```

Periksa hasilnya di `/mnt/experiment-data/results/`.

### 5.3 Troubleshooting: Kubelet Crash / Node Unschedulable

Jika eksekusi gagal di tengah pergantian policy dan node terjebak di status `SchedulingDisabled`, jalankan urutan ini untuk pemulihan:

```bash
# 1. Timpa konfigurasi yang rusak dengan backup yang benar
sudo cp ~/crossover-experiment/kubelet-configs/condition-A-none.yaml /var/lib/kubelet/config.yaml
# 2. Hapus file state lama
sudo rm -f /var/lib/kubelet/cpu_manager_state
# 3. Nyalakan ulang kubelet
sudo systemctl restart kubelet
# 4. Buka kunci penjadwalan node di API Server
kubectl uncordon crossover-experiment-vm

```

---

## Bagian 6 — Eksperimen penuh

Ini adalah Blok 1 (urutan A→B). Blok 2 (urutan B→A, hari terpisah) akan
diimplementasikan sebagai bagian dari Phase 4 (block counterbalancing).
Sementara itu, jalankan Blok 1 terlebih dahulu:

```bash
# Blok 1 — Urutan A→B

# Kondisi A (baseline) — N=15, 4 vCPU sesuai proposal (requests=limits=4, Guaranteed QoS)
bash scripts/run_experiment.sh none 15 4

# Berpindah ke Kondisi B (perlakuan)
bash scripts/switch_cpu_manager_policy.sh static

# Kondisi B (perlakuan) — N=15, 4 vCPU
bash scripts/run_experiment.sh static 15 4

```

> **Catatan block counterbalancing:** Blok 2 (B→A) dijalankan di hari yang
> berbeda untuk mengontrol variabel perancu temporal. Orkestrasi dua-blok
> penuh — termasuk uji efek urutan Mann-Whitney U antar blok — akan tersedia
> setelah Phase 4 (run_full_experiment.sh) diimplementasikan.

## Bagian 7 — Analisis

Untuk menjalankan skrip analisis, Anda memerlukan pengelola paket `pip` dan pustaka data sains Python. 

```bash
# Install pip untuk Python 3
sudo apt-get update
sudo apt-get install -y python3-pip

# Install pustaka analisis
pip3 install --no-warn-script-location pandas scipy

# Jalankan skrip analisis
python3 scripts/analyze_results.py --results-dir /mnt/experiment-data/results

```
