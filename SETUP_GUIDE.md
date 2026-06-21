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
PID host dari container solver (lewat `crictl pods` lalu `crictl ps`/`crictl
inspect`) — tanpa langkah ini, skrip tersebut akan gagal dengan
"command not found".
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

### 2.11 Berikan akses sudo tanpa password untuk crictl (dibutuhkan saat eksperimen berjalan)
`run_experiment.sh` memanggil `collect_system_metrics.py` di background sambil
menunggu Pod selesai — proses ini perlu menjalankan `sudo crictl` tanpa
diinterupsi prompt password di tengah eksperimen otomatis.
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
newgrp docker   # refresh group membership tanpa perlu re-login
```

> ⚠️ **Troubleshooting: dpkg error setelah install Docker**
>
> Jika muncul `E: Sub-process /usr/bin/dpkg returned an error code (1)` saat
> menjalankan baris pertama, kemungkinan besar **config file conflict** antara
> `containerd` (Ubuntu repo, Section 2.3) dan `containerd.io` (Docker repo).
> Config `/etc/containerd/config.toml` yang sudah Anda edit di Section 2.3
> memicu prompt dpkg yang gagal karena non-interactive.
>
> **Fix:**
> ```bash
> DEBIAN_FRONTEND=noninteractive dpkg --configure --force-confdef --force-confold -a
> ```
> Ini memaksa dpkg mempertahankan config Anda dan menyelesaikan semua
> package pending. Setelah itu, verifikasi:
> ```bash
> docker run hello-world
> ```

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

### 3.4 Import image ke containerd (supaya terlihat oleh Kubernetes)
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
sudo chmod -R 777 /mnt/experiment-data   # longgar untuk simplicity single-user VM
kubectl apply -f manifests/02-storage.yaml
```

### 4.2 Buat Secret kredensial Gurobi WLS — JANGAN lewat file YAML
Dapatkan `WLSACCESSID`, `WLSSECRET`, `LICENSEID` dari Gurobi User Portal
(generate Academic WLS client license Anda), lalu:
```bash
kubectl create secret generic gurobi-wls-credentials \
  --namespace=crossover-experiment \
  --from-literal=GRB_WLSACCESSID='<isi_dari_portal_gurobi>' \
  --from-literal=GRB_WLSSECRET='<isi_dari_portal_gurobi>' \
  --from-literal=GRB_LICENSEID='<isi_dari_portal_gurobi>'
```
> Jangan jalankan perintah ini dengan `history` shell tersimpan ke file publik.
> Pertimbangkan `set +o history` sebelum baris ini dan `set -o history` setelah,
> supaya kredensial tidak tertinggal di `~/.bash_history`.

### 4.3 Unduh instance benchmark Mittelmann
```bash
# WAJIB edit dulu scripts/download_benchmarks.sh — isi URL instance ASLI
# dari https://plato.asu.edu/bench.html (URL contoh di skrip BUKAN URL final).
nano scripts/download_benchmarks.sh
bash scripts/download_benchmarks.sh
```

---

## Bagian 5 — Verifikasi awal sebelum eksperimen penuh

### 5.1 Pastikan kebijakan default node adalah `none`
```bash
grep cpuManagerPolicy /var/lib/kubelet/config.yaml || echo "belum diset eksplisit, default = none"
```

### 5.2 Jalankan SATU run percobaan (smoke test) sebelum batch penuh
```bash
chmod +x scripts/*.sh
bash scripts/run_experiment.sh none 1 7
```
Periksa hasilnya di `/mnt/experiment-data/results/` — pastikan file `.json`
muncul, `status_code` bernilai `2` (GRB.OPTIMAL), dan `crossover_seconds`
terisi angka wajar (bukan `null`).

---

## Bagian 6 — Eksperimen penuh

```bash
# Kondisi A (baseline) — N=15 sesuai Metode Penelitian
bash scripts/run_experiment.sh none 15 7

# Berpindah ke Kondisi B (perlakuan) — ini akan men-drain node sementara
bash scripts/switch_cpu_manager_policy.sh static

# Kondisi B (perlakuan)
bash scripts/run_experiment.sh static 15 7

# (Opsional) kembali ke none untuk pengujian lanjutan
bash scripts/switch_cpu_manager_policy.sh none
```

## Bagian 7 — Analisis

```bash
pip3 install pandas scipy --break-system-packages
python3 scripts/analyze_results.py --results-dir /mnt/experiment-data/results
```

---

## Pengingat biaya & keamanan

- **Matikan VM saat tidak dipakai**: `gcloud compute instances stop crossover-experiment-vm --zone="$ZONE"`
  — Anda tetap dikenai biaya disk persisten saat VM stop, tapi tidak biaya compute.
- **Hapus VM setelah eksperimen selesai** (sebelum kredit/91 hari trial habis,
  dan setelah Anda mengunduh seluruh hasil ke laptop):
  ```bash
  gcloud compute scp --recurse crossover-experiment-vm:~/crossover-experiment/results ./hasil-eksperimen --zone="$ZONE"
  gcloud compute instances delete crossover-experiment-vm --zone="$ZONE"
  ```
- **Jangan commit** Secret/kredensial WLS ke git, termasuk file `.bash_history`
  atau screenshot terminal yang menampilkannya.

## Troubleshooting & Recovery

### Kubelet Crash / "1 node(s) were unschedulable"
Jika skrip `switch_cpu_manager_policy.sh` gagal di tengah jalan (karena interupsi paksa atau kesalahan konfigurasi cadangan), Node eksperimen (VM) akan tertinggal dalam status terkunci (`SchedulingDisabled` / Cordoned) dan layanan `kubelet` berpotensi mati. 

Untuk memulihkan klaster ke kondisi siap eksperimen, lakukan langkah berikut secara berurutan:

1. **Timpa konfigurasi Node yang rusak** dengan konfigurasi bersih:
   ```bash
   sudo cp ~/crossover-experiment/kubelet-configs/condition-A-none.yaml /var/lib/kubelet/config.yaml
