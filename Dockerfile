# Dockerfile — image solver untuk eksperimen CPU pinning x Gurobi crossover
# Base image resmi Gurobi (sudah berisi Gurobi Optimizer + Python API)
FROM gurobi/python:11.0.3@sha256:d631ee5d6c3a26d084fedf11bf44bd3568e5af743a259c9c65ece6f9ea1343f9

# Validasi versi Gurobi pada saat build
RUN python -c "import gurobipy; assert gurobipy.gurobi.version() == (11, 0, 3), f'Expected Gurobi version (11, 0, 3) but got {gurobipy.gurobi.version()}'"

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
