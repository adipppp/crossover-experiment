# Dockerfile — image solver untuk eksperimen CPU pinning x Gurobi crossover
# Base image resmi Gurobi (sudah berisi Gurobi Optimizer + Python API)
FROM gurobi/python:13.0.2_3.12@sha256:a5565d83180d08e378397671b7e4aa15cb2866ad23c212b4bde97703efffcca9

# Validasi versi Gurobi pada saat build
RUN python -c "import gurobipy; assert gurobipy.gurobi.version()[:2] == (13, 0), f'Expected Gurobi version 13.0.x but got {gurobipy.gurobi.version()}'"

WORKDIR /app

COPY scripts/run_solver.py /app/run_solver.py

# Direktori untuk benchmark instance (di-mount via hostPath ke tmpfs saat runtime)
RUN mkdir -p /app/instances /app/results

# Buat group dan user non-root solver yang cocok dengan securityContext di manifests/pod-template.yaml
RUN groupadd -g 1002 solver && \
    useradd -u 1001 -g 1002 -m solver && \
    chown -R solver:solver /app

USER solver

ENTRYPOINT ["python", "/app/run_solver.py"]
