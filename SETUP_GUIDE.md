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
# Ganti REGION sesuai pilihan, mis. asia-southeast1 (Singapura, dekat Indonesia)
REGION="asia-southeast1"

gcloud compute regions describe "$REGION" \
  --format="table(quotas.filter(metric='CPUS'))"

```

Jika quota CPUS di region itu kurang dari 8, ajukan kenaikan kuota lewat
Console: **IAM & Admin > Quotas**, filter `CPUs`, region sesuai pilihan,
request naik ke minimal 8. Proses approval biasanya cepat (menit-jam) untuk
kenaikan kecil pada akun trial, tapi alokasikan waktu tunggu di rencana Anda.

### 1.3 Buat VM

```bash
ZONE="asia-southeast1-a"   # sesuaikan dengan region yang quota-nya cukup

gcloud compute instances create crossover-experiment-vm \
  --zone="$ZONE" \
  --machine-type="c2-standard-8" \
  --image-family="ubuntu-2204-lts" \
  --image-project="ubuntu-os-cloud" \
  --boot-disk-size="50GB" \
  --boot-disk-type="pd-ssd" \
  --tags="crossover-experiment"

```

### 1.4 Buka akses SSH (firewall) jika belum ada rule default

```bash
gcloud compute firewall-rules create allow-ssh-crossover-experiment \
  --network=default \
  --allow=tcp:22 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=crossover-experiment

```

> Catatan keamanan: `source-ranges=0.0.0.0/0` membuka SSH dari semua IP. Untuk
> keamanan lebih baik, ganti dengan IP publik Anda saja (cek di whatismyip.com),
> mis. `--source-ranges=36.xxx.xxx.xxx/32`.

### 1.5 SSH ke VM

```bash
gcloud compute ssh crossover-experiment-vm --zone="$ZONE"

```

**Semua langkah selanjutnya (Bagian 2 dst.) dijalankan DI DALAM VM ini.**

---

## Bagian 2 — Setup Kubernetes single-node (kubeadm) — DI DALAM VM

### 2.1 Update sistem dan install dependensi dasar

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg software-properties-common

```

### 2.2 Disable swap (syarat wajib kubelet)

```bash
sudo swapoff -a
sudo sed -i '/ swap / s/^/#/' /etc/fstab

```

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
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.31/deb/Release.key | \
  sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.31/deb/ /' | \
  sudo tee /etc/apt/sources.list.d/kubernetes.list

sudo apt-get update
sudo apt-get install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl

```

> Catatan: ganti `v1.31` di atas dengan versi minor stabil terbaru jika
> sudah ada rilis lebih baru saat Anda membaca ini — cek di
> https://kubernetes.io/releases/

### 2.6 Install & konfigurasi crictl

`scripts/collect_system_metrics.py` BERGANTUNG pada `crictl` untuk menemukan
PID host dari container solver.

```bash
CRICTL_VERSION="v1.31.1"   # samakan major.minor dengan versi Kubernetes di 2.5
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

### 2.8 Install CNI plugin (Flannel — sederhana, cukup untuk single-node)

```bash
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml

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

---

## Bagian 3 — Build & load image solver — DI DALAM VM

### 3.1 Install Docker (untuk build image; containerd dipakai runtime Kubernetes)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker   # refresh group membership

```

> ⚠️ **Troubleshooting: dpkg error setelah install Docker**
> Jika muncul `E: Sub-process /usr/bin/dpkg returned an error code (1)`, jalankan:
> `DEBIAN_FRONTEND=noninteractive dpkg --configure --force-confdef --force-confold -a`

### 3.2 Transfer file proyek dari laptop Anda ke VM

Di **laptop** (bukan di VM), jalankan:

```bash
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

### 5.2 Jalankan SATU run percobaan (smoke test) dengan alokasi 6 CPU

```bash
chmod +x scripts/*.sh
bash scripts/run_experiment.sh none 1 6

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
# Kondisi A (baseline) — N=15, 6 vCPU
bash scripts/run_experiment.sh none 15 6

# Berpindah ke Kondisi B (perlakuan)
bash scripts/switch_cpu_manager_policy.sh static

# Kondisi B (perlakuan) — N=15, 6 vCPU
bash scripts/run_experiment.sh static 15 6

```

## Bagian 7 — Analisis

Untuk menjalankan skrip analisis, Anda memerlukan pengelola paket `pip` dan pustaka data sains Python. 

```bash
# Install pip untuk Python 3
sudo apt-get update
sudo apt-get install -y python3-pip

# Install pustaka analisis (mengabaikan warning system-wide package di Ubuntu 22.04)
pip3 install pandas scipy

# Jalankan skrip analisis
python3 scripts/analyze_results.py --results-dir /mnt/experiment-data/results

```
