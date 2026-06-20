# Dockerfile — image solver untuk eksperimen CPU pinning x Gurobi crossover
# Base image resmi Gurobi (sudah berisi Gurobi Optimizer + Python API)
FROM gurobi/python:11.0.3

WORKDIR /app

# Dependensi tambahan untuk pengukuran metrik sistem (context switches, dsb.)
# psutil untuk membaca /proc dari dalam container saat dibutuhkan sebagai cross-check.
RUN pip install --no-cache-dir psutil==6.0.0

COPY scripts/run_solver.py /app/run_solver.py

# Direktori untuk benchmark instance (di-mount via ConfigMap/PVC saat runtime)
RUN mkdir -p /app/instances /app/results

ENTRYPOINT ["python", "/app/run_solver.py"]
