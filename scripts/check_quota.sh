#!/usr/bin/env bash
# check_quota.sh — Memeriksa kuota CPU GCP dan (opsional) mengajukan peningkatan
# kuota secara terprogram. Jalankan di LAPTOP sebelum membuat VM (§1.2 SETUP_GUIDE).
#
# Penggunaan:
#   bash scripts/check_quota.sh [REGION] [PROJECT_ID]
#
# Atau via env var:
#   REGION=us-central1 PROJECT_ID=my-project bash scripts/check_quota.sh
#
# Opsional (hanya dipakai saat mengajukan quota request):
#   QUOTA_EMAIL=email@university.ac.id
#   QUOTA_JUSTIFICATION="Eksperimen riset CPU pinning Kubernetes — skripsi S1"
#
# Output:
#   - Ringkasan kuota ke stdout
#   - infra/quota-check-report.json (machine-readable, dibaca Phase 3)

set -euo pipefail

REGION="${1:-${REGION:-us-central1}}"
PROJECT_ID="${2:-${PROJECT_ID:-}}"
QUOTA_EMAIL="${QUOTA_EMAIL:-}"
QUOTA_JUSTIFICATION="${QUOTA_JUSTIFICATION:-Eksperimen riset empiris CPU pinning pada LP solver di Kubernetes (skripsi S1 Ilmu Komputer)}"

# Threshold kebutuhan kuota per opsi mesin
VCPU_C2_STANDARD_8=8    # Option 1/2: c2-standard-8
VCPU_C2_STANDARD_16=16  # Option 3:   c2-standard-16 (fallback eskalasi)

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_DIR="$REPO_DIR/infra"
REPORT_FILE="$INFRA_DIR/quota-check-report.json"
mkdir -p "$INFRA_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# Prerequisit
# ─────────────────────────────────────────────────────────────────────────────

if ! command -v gcloud &>/dev/null; then
  echo "ERROR: gcloud CLI tidak ditemukan." >&2
  echo "       Install dari: https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 tidak ditemukan (dibutuhkan untuk parsing JSON kuota)." >&2
  exit 1
fi

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
  if [[ -z "$PROJECT_ID" ]]; then
    echo "ERROR: PROJECT_ID tidak disetel." >&2
    echo "       Jalankan: gcloud config set project <PROJECT_ID>" >&2
    echo "       Atau:     PROJECT_ID=my-project bash scripts/check_quota.sh" >&2
    exit 1
  fi
fi

echo "========================================================"
echo "  CHECK KUOTA CPU — Phase 0 (check_quota.sh)"
echo "========================================================"
echo "  Project : $PROJECT_ID"
echo "  Region  : $REGION"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Ambil info kuota dari GCP (format JSON untuk parsing yang andal)
# ─────────────────────────────────────────────────────────────────────────────

echo ">>> Mengambil kuota CPUS di region $REGION ..."

QUOTA_JSON_FILE=$(mktemp /tmp/gcp_quota_XXXXXX.json)
trap 'rm -f "$QUOTA_JSON_FILE"' EXIT

gcloud compute regions describe "$REGION" \
  --project="$PROJECT_ID" \
  --format="json(quotas)" > "$QUOTA_JSON_FILE" 2>/dev/null || {
  echo "ERROR: Gagal mengambil informasi kuota dari GCP." >&2
  echo "       Pastikan Anda sudah login  : gcloud auth login" >&2
  echo "       Dan project sudah disetel  : gcloud config set project $PROJECT_ID" >&2
  exit 1
}

# Parse CPUS limit dan usage dengan python3.
# JSON ditulis ke file sementara (bukan disisipkan ke heredoc) agar karakter
# khusus dalam output gcloud tidak merusak string literal Python.
read -r CPUS_LIMIT CPUS_USAGE CPUS_AVAILABLE < <(python3 - "$QUOTA_JSON_FILE" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
quotas = data.get("quotas", [])
for q in quotas:
    if q.get("metric") == "CPUS":
        limit     = int(q.get("limit",  0))
        usage     = int(q.get("usage",  0))
        available = limit - usage
        print(limit, usage, available)
        sys.exit(0)
print("0 0 0")
PYEOF
)

if [[ "$CPUS_LIMIT" == "0" ]]; then
  echo "ERROR: Metrik CPUS tidak ditemukan di output kuota region $REGION." >&2
  echo "       Output mentah (truncated):" >&2
  head -30 "$QUOTA_JSON_FILE" >&2
  exit 1
fi

echo ""
echo "  Kuota CPUS — region $REGION:"
echo "    Limit    : $CPUS_LIMIT vCPU"
echo "    Terpakai : $CPUS_USAGE vCPU"
echo "    Tersedia : $CPUS_AVAILABLE vCPU"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Evaluasi kelayakan per opsi
# ─────────────────────────────────────────────────────────────────────────────

STATUS_8="INSUFFICIENT"
STATUS_16="INSUFFICIENT"

if (( CPUS_AVAILABLE >= VCPU_C2_STANDARD_8 )); then
  STATUS_8="OK"
  echo "  [OK]  c2-standard-8  ($VCPU_C2_STANDARD_8 vCPU)  — cukup untuk Option 1 atau Option 2"
else
  SHORTFALL_8=$(( VCPU_C2_STANDARD_8 - CPUS_AVAILABLE ))
  echo "  [!!]  c2-standard-8  ($VCPU_C2_STANDARD_8 vCPU)  — KURANG $SHORTFALL_8 vCPU (tersedia: $CPUS_AVAILABLE)"
fi

if (( CPUS_AVAILABLE >= VCPU_C2_STANDARD_16 )); then
  STATUS_16="OK"
  echo "  [OK]  c2-standard-16 ($VCPU_C2_STANDARD_16 vCPU) — cukup untuk Option 3 (eskalasi mesin)"
else
  SHORTFALL_16=$(( VCPU_C2_STANDARD_16 - CPUS_AVAILABLE ))
  echo "  [!!]  c2-standard-16 ($VCPU_C2_STANDARD_16 vCPU) — KURANG $SHORTFALL_16 vCPU untuk Option 3"
  echo "        (Option 3 membutuhkan peningkatan kuota jika dipilih nanti)"
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Tulis laporan JSON awal
# ─────────────────────────────────────────────────────────────────────────────

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
python3 - \
  "$TIMESTAMP" "$PROJECT_ID" "$REGION" \
  "$CPUS_LIMIT" "$CPUS_USAGE" "$CPUS_AVAILABLE" \
  "$STATUS_8" "$STATUS_16" \
  "$REPORT_FILE" \
  <<'PYEOF'
import json, sys
report = {
    "generated_at": sys.argv[1],
    "project_id":   sys.argv[2],
    "region":       sys.argv[3],
    "cpus_quota": {
        "limit":     int(sys.argv[4]),
        "usage":     int(sys.argv[5]),
        "available": int(sys.argv[6]),
    },
    "option_feasibility": {
        "option1_or_2_c2_standard_8":  sys.argv[7],
        "option3_c2_standard_16":      sys.argv[8],
    },
    "quota_increase_request": None,
}
with open(sys.argv[9], "w") as f:
    json.dump(report, f, indent=2)
PYEOF

echo ">>> Laporan sementara disimpan: $REPORT_FILE"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Tawarkan pengajuan peningkatan kuota jika diperlukan
# ─────────────────────────────────────────────────────────────────────────────

if [[ "$STATUS_8" == "INSUFFICIENT" || "$STATUS_16" == "INSUFFICIENT" ]]; then
  echo "┌──────────────────────────────────────────────────────────────────────┐"
  echo "│  TINDAKAN DIPERLUKAN: Kuota tidak mencukupi untuk satu atau lebih   │"
  echo "│  opsi mesin. Anda dapat mengajukan peningkatan kuota sekarang.      │"
  echo "└──────────────────────────────────────────────────────────────────────┘"
  echo ""

  # Tentukan target: naikkan ke CPUS_USAGE + 16 agar cukup untuk semua opsi
  TARGET_QUOTA=$(( CPUS_USAGE + VCPU_C2_STANDARD_16 ))
  echo "  Rekomendasi: ajukan peningkatan ke $TARGET_QUOTA vCPU"
  echo "  (= $CPUS_USAGE terpakai sekarang + $VCPU_C2_STANDARD_16 untuk c2-standard-16)"
  echo "  Ini mengamankan Option 1/2 (c2-standard-8) dan Option 3 (c2-standard-16)"
  echo "  sehingga eskalasi nanti tidak menunggu approval kedua."
  echo ""

  # Cek ketersediaan gcloud beta
  if ! gcloud beta --version &>/dev/null 2>&1; then
    echo "  CATATAN: 'gcloud beta' tidak tersedia. Install dengan:"
    echo "    gcloud components install beta"
    echo ""
    echo "  Alternatif: ajukan manual di https://console.cloud.google.com/iam-admin/quotas"
    echo "  Filter: CPUs, region $REGION, lalu klik 'Edit Quotas'."
    echo ""
    CONFIRM_QUOTA="n"
  else
    echo "  Ingin mengajukan peningkatan kuota sekarang via 'gcloud beta quotas'? [y/N]"
    read -r CONFIRM_QUOTA
  fi

  if [[ "${CONFIRM_QUOTA,,}" == "y" ]]; then
    # Validasi email (wajib untuk quota request)
    if [[ -z "$QUOTA_EMAIL" ]]; then
      echo "  Masukkan alamat email kontak untuk quota request:"
      read -r QUOTA_EMAIL
    fi
    if [[ -z "$QUOTA_EMAIL" ]]; then
      echo "ERROR: Email wajib diisi untuk mengajukan quota request." >&2
      echo "       Set: QUOTA_EMAIL=email@university.ac.id bash scripts/check_quota.sh" >&2
      exit 1
    fi

    PREF_ID="crossover-exp-cpus-$(date +%s)"
    echo ""
    echo ">>> Mengajukan peningkatan kuota ..."
    echo "    Service   : compute.googleapis.com"
    echo "    Quota ID  : CPUS-per-project-region"
    echo "    Region    : $REGION"
    echo "    Target    : $TARGET_QUOTA vCPU"
    echo "    Email     : $QUOTA_EMAIL"
    echo "    Pref ID   : $PREF_ID"
    echo ""

    if gcloud beta quotas preferences create \
        --project="$PROJECT_ID" \
        --service="compute.googleapis.com" \
        --quota-id="CPUS-per-project-region" \
        --dimensions="region=$REGION" \
        --preferred-value="$TARGET_QUOTA" \
        --email="$QUOTA_EMAIL" \
        --justification="$QUOTA_JUSTIFICATION" \
        --preference-id="$PREF_ID" \
        2>&1; then

      echo ""
      echo ">>> Pengajuan berhasil dikirim. Preference ID: $PREF_ID"
      echo "    Approval biasanya memakan waktu beberapa menit hingga jam."
      echo "    Pantau status:"
      echo "      gcloud beta quotas preferences describe $PREF_ID \\"
      echo "        --project=$PROJECT_ID"
      echo ""

      # Update laporan JSON dengan info request
      REQ_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
      python3 - <<'PYEOF2' "$REPORT_FILE" "$REQ_TIMESTAMP" "$PREF_ID" "$TARGET_QUOTA" "$QUOTA_EMAIL" "$PROJECT_ID"
import json, sys
report_file, submitted_at, pref_id, target_value, email, project_id = sys.argv[1:]
with open(report_file) as f:
    report = json.load(f)
report["quota_increase_request"] = {
    "submitted_at":   submitted_at,
    "preference_id":  pref_id,
    "target_value":   int(target_value),
    "email":          email,
    "status":         "pending",
    "check_status_cmd": (
        f"gcloud beta quotas preferences describe {pref_id} "
        f"--project={project_id}"
    ),
}
with open(report_file, "w") as f:
    json.dump(report, f, indent=2)
print(">>> Laporan diperbarui:", report_file)
PYEOF2

    else
      echo ""
      echo "WARNING: Pengajuan quota mungkin gagal. Periksa output di atas." >&2
      echo "         Ajukan manual: https://console.cloud.google.com/iam-admin/quotas" >&2
    fi

  else
    echo ">>> Pengajuan ditunda."
    echo "    Ajukan manual di: https://console.cloud.google.com/iam-admin/quotas"
    echo "    Filter: CPUs — region $REGION — klik 'Edit Quotas'."
    echo "    Atau jalankan skrip ini lagi dengan QUOTA_EMAIL=... setelah gcloud beta terpasang."
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Ringkasan akhir
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "========================================================"
echo "  RINGKASAN"
echo "========================================================"
echo ""
if [[ "$STATUS_8" == "OK" ]]; then
  echo "  ✓ Kuota cukup untuk c2-standard-8."
  echo "    Lanjutkan ke §1.3 Buat VM di SETUP_GUIDE.md."
else
  echo "  ✗ TUNGGU persetujuan kuota sebelum membuat VM."
  echo "    Pantau: gcloud compute regions describe $REGION \\"
  echo "              --project=$PROJECT_ID \\"
  echo "              --format=\"table(quotas.filter(metric='CPUS'))\""
fi
echo ""
if [[ "$STATUS_16" == "OK" ]]; then
  echo "  ✓ Option 3 (c2-standard-16) sudah aman — kuota 16 vCPU tersedia."
else
  echo "  ! Option 3 (c2-standard-16) membutuhkan peningkatan kuota tambahan."
  echo "    Lakukan sekarang agar tidak menunggu approval di tengah eksperimen."
fi
echo ""
echo "  Laporan lengkap: $REPORT_FILE"
echo ""
