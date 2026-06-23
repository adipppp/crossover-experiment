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
# Ganti REGION sesuai pilihan, mis. us-central1
REGION="us-central1"

gcloud compute regions describe "$REGION" \
  --format="table(quotas.filter(metric='CPUS'))"

```

Jika quota CPUS di region itu kurang dari **8**, ajukan kenaikan kuota lewat
Console: **IAM & Admin > Quotas**, filter `CPUs`, region sesuai pilihan,
request naik ke minimal 8.

> Catatan: meskipun `--threads-per-core=1` membuat VM hanya melihat 4 vCPU
> dari dalam guest OS, GCP selalu menghitung **kuota berdasarkan machine type
> (8 vCPU untuk c2-standard-8)** — bukan jumlah thread yang terlihat guest.

Proses approval biasanya cepat (menit-jam) untuk kenaikan kecil pada akun trial.

### 1.3 Buat VM

```bash
ZONE="us-central1-a"   # sesuaikan dengan region yang quota-nya cukup

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

> **`--threads-per-core=1`** menonaktifkan SMT (Hyper-Threading) di level
> hypervisor GCP — cara yang benar dan didukung resmi. Hasilnya: VM hanya
> memiliki **4 vCPU** (1 thread per physical core) yang terlihat oleh guest OS.
> Anda tetap ditagih untuk 8 vCPU penuh (`c2-standard-8`), tapi tiap vCPU
> kini 1-to-1 dengan physical core — tidak ada SMT sharing antar-container.
> Jangan gunakan `echo off > /sys/devices/system/cpu/smt/control` di dalam VM;
> pada KVM GCP, interface sysfs tersebut read-only atau tidak fungsional.

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

### 2.2.1 Verifikasi SMT sudah dinonaktifkan oleh GCP

SMT sudah dinonaktifkan saat pembuatan VM via `--threads-per-core=1` (§1.3).
Langkah ini hanya memverifikasi hasilnya — tidak ada perintah tambahan yang dibutuhkan.

```bash
# Harus menunjukkan 4 (bukan 8) — karena SMT off di level hypervisor
nproc

# Verifikasi topologi: setiap 'Core(s) per socket' harus 1 thread
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
# Salin konfigurasi kubelet baseline (Condition A - none) ke folder kubelet:
sudo cp ~/crossover-experiment/kubelet-configs/condition-A-none.yaml \
  /var/lib/kubelet/config.yaml

# Nyalakan ulang kubelet agar perubahan terbaca:
sudo systemctl restart kubelet

# Verifikasi kubelet aktif dan setting terbaca:
sudo systemctl is-active kubelet
grep -E 'cpuManagerPolicy|systemReserved|kubeReserved|cpuManagerReconcilePeriod' \
  /var/lib/kubelet/config.yaml

```

> **Catatan urutan:** Lakukan ini SEBELUM §2.8 (install Flannel) agar node
> sudah dalam konfigurasi eksperimen yang benar sejak awal.

### 2.8 Install CNI plugin (Flannel — sederhana, cukup untuk single-node)

```bash
# Pin ke versi spesifik — JANGAN pakai /releases/latest karena mutable
# Cek versi stable terbaru di: https://github.com/flannel-io/flannel/releases
FLANNEL_VERSION="v0.28.0"
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

### 2.12 Izinkan `perf stat` di host (wajib untuk `collect_system_metrics.py`)

`collect_system_metrics.py` menjalankan `sudo perf stat -p <pid>` dari host
untuk mengukur cache misses container solver. Default Ubuntu 22.04 memblokir ini.

```bash
# Aktifkan akses perf (permanen, bertahan setelah reboot):
echo 'kernel.perf_event_paranoid = -1' | sudo tee /etc/sysctl.d/99-perf.conf
sudo sysctl -p /etc/sysctl.d/99-perf.conf

# Verifikasi (harus return -1):
sysctl kernel.perf_event_paranoid

# Tes perf bisa berjalan (harus tidak error):
sudo perf stat -e cache-misses sleep 0.1 2>&1 | grep -E 'cache-misses|not supported'

```

> ⚠️ Jika `perf stat` mengembalikan `<not supported>` (bukan nilai angka),
> artinya hardware PMU tidak di-expose oleh hypervisor GCP untuk VM ini.
> Dalam kasus itu, `collect_system_metrics.py` akan tetap berjalan tapi
> field `perf_metrics` di output JSON akan kosong — metrik utama
> (context switches) tetap terkumpul via `/proc/<pid>/status`.

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
docker build -t crossover-experiment/gurobi-solver:latest .

```

### 3.4 Import image ke containerd

```bash
docker save crossover-experiment/gurobi-solver:latest -o /tmp/solver-image.tar
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

```bash
bash scripts/download_benchmarks.sh

```

---

## Bagian 5 — Verifikasi & Pemulihan

### 5.1 Pastikan kebijakan default node adalah `none`

```bash
grep cpuManagerPolicy /var/lib/kubelet/config.yaml || echo "belum diset eksplisit, default = none"

```

### 5.2 Verifikasi alokasi CPU dan jalankan smoke test

Dengan `--threads-per-core=1`, VM melihat **4 vCPU**. Kubelet mereservasi
`systemReserved=500m + kubeReserved=500m = 1 CPU`, sehingga:
`Allocatable = 4 - 1 = 3 CPU` untuk Pod solver.

```bash
# Verifikasi allocatable CPU node (harus 3)
kubectl get node -o jsonpath='{.items[0].status.allocatable.cpu}'

chmod +x scripts/*.sh
bash scripts/run_experiment.sh none 1 3

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

```bash
# Kondisi A (baseline) — N=15, 3 vCPU
bash scripts/run_experiment.sh none 15 3

# Berpindah ke Kondisi B (perlakuan)
bash scripts/switch_cpu_manager_policy.sh static

# Kondisi B (perlakuan) — N=15, 3 vCPU
bash scripts/run_experiment.sh static 15 3

```

## Bagian 7 — Analisis

Untuk menjalankan skrip analisis, Anda memerlukan pengelola paket `pip` dan pustaka data sains Python. 

```bash
# Install pip untuk Python 3
sudo apt-get update
sudo apt-get install -y python3-pip

# Install pustaka analisis (mengabaikan error/warning system-wide package di Ubuntu 22.04)
pip3 install --break-system-packages pandas scipy

# Jalankan skrip analisis
python3 scripts/analyze_results.py --results-dir /mnt/experiment-data/results

```
