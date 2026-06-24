# Project Analysis: CPU Pinning x Crossover LP Solver in Kubernetes

This document provides a comprehensive overview of the research project **"Pengaruh CPU Pinning terhadap Performa Fase Crossover pada LP Solver di Lingkungan Kubernetes"** (The Influence of CPU Pinning on the Performance of the Crossover Phase of LP Solver in Kubernetes Environment). 

---

## 1. Executive Summary

The project is an empirical, controlled, within-subject experimental framework designed for an undergraduate thesis (*skripsi*). Its goal is to measure and analyze the impact of Kubernetes' **static CPU Manager policy (CPU Pinning)** on the **crossover phase** of the **Gurobi Linear Programming (LP) solver**. 

The crossover phase in LP solving (which transitions nonbasic interior solutions from barrier methods to basic solutions) is highly sequential and sensitive to cache locality and thread migrations. By comparing a baseline condition (Completely Fair Scheduler, CFS) against pinned CPU cores (using Kubernetes Guaranteed QoS and the `static` CPU manager), this research aims to quantify changes in crossover execution time and verify if performance gains correlate with reduced involuntary context switches.

---

## 2. Research and Academic Framework

The academic foundation is detailed in `local/Proposal final revised claude.md`.

### 2.1 Research Questions (RQs)
1. **RQ1 (Crossover Execution Time)**: What is the effect of CPU pinning on the execution time of the crossover phase of LP solvers in Kubernetes?
2. **RQ2 (Thread Migrations & Scheduling)**: Does the reduction in crossover time correlate with a decrease in involuntary context switches?
3. **RQ3 (Barrier Stability Control)**: Is the duration of the barrier phase stable across both CPU configurations, confirming that any overall performance differences originate strictly from the crossover phase?
4. **RQ4 (Effect Size Across Instances)**: How large is the contribution of the CPU Manager policy across different benchmark instances with varying size and sparsity characteristics?

### 2.2 Scope and Methodological Limitations
* **vCPUs as Hyperthreads**: Because the experiment runs on a public cloud VM (Google Compute Engine), a vCPU represents a single hyperthread rather than a dedicated physical core.
* **Shared Physical Hosts**: VM co-location on public cloud servers introduces residual noise, which the experiment mitigates by running **15 repetitions** per instance per condition and reporting **median and Interquartile Range (IQR)** instead of mean values.
* **Involuntary Context Switches as a Proxy**: Thread migration is measured indirectly using involuntary context switches (`nonvoluntary_ctxt_switches` from `/proc/[pid]/status`) rather than direct kernel thread migration tracing.
* **Single Solver**: The study is restricted to the **Gurobi Optimizer** using an Academic Web License Service (WLS) license.

---

## 3. Architecture and Infrastructure Design

The architecture uses a single-node Kubernetes cluster built on a dedicated GCP VM.

```mermaid
graph TD
    subgraph GCP VM Host (4 vCPUs, c2-standard-8 SMT off)
        subgraph Operating System (Ubuntu 22.04)
            crictl[crictl / host procfs]
            metrics_script[collect_system_metrics.py]
            kubelet[Kubelet Service]
        end
        
        subgraph Kubernetes Single-Node Cluster (kubeadm)
            subgraph Namespace: crossover-experiment
                pod[Solver Pod: Guaranteed QoS]
                secret[gurobi-wls-credentials Secret]
                pvc[experiment-data PVC]
            end
        end
    end
    
    pvc -- Mounts hostPath --> host_storage[/mnt/experiment-data]
    metrics_script -- Polls --> crictl
    metrics_script -- Reads --> host_proc["/proc/<pid>/status"]
    pod -- Runs --> run_solver[run_solver.py]
    run_solver -- Requests License --> gurobi_wls((Gurobi WLS Server))
```

### 3.1 VM & Cluster Configuration
* **Hardware**: GCP Compute-Optimized VM (`c2-standard-8`, 8 vCPUs normally, but configured with `--threads-per-core=1` so the guest OS sees **4 vCPUs**, 32GB RAM).
* **Cluster Tooling**: Bootstrapped using `kubeadm` on Ubuntu 22.04 LTS with `containerd` as the container runtime and `systemd` as the cgroup driver.
* **Resource Reservations**:
  * **System & Kubelet Reserved**: 1 vCPU and 1024Mi RAM are reserved for the system and Kubernetes services (500m CPU / 512Mi RAM each for `systemReserved` and `kubeReserved`).
  * **Workload Capacity**: Out of the 4 vCPUs visible on the VM, **3 vCPUs** are available as *Allocatable* to workloads. The solver container is configured to request and limit exactly **2 vCPUs** to leave 1 vCPU of headroom for system DaemonSets and other Kubernetes processes within the 3 allocatable CPUs.

### 3.2 Kubernetes QoS & CPU Manager
To achieve CPU pinning, the Kubernetes scheduler requires the Pod to be in the **Guaranteed QoS class**:
1. `requests.cpu` must equal `limits.cpu`.
2. `requests.memory` must equal `limits.memory`.
3. The CPU request value must be an **integer** (e.g. `3`, not `3000m` or `3.5`).

The CPU manager policy is configured in `kubelet-config.yaml` as:
* **Condition A (Baseline)**: `cpuManagerPolicy: none` (CFS scheduler manages CPU allocation; thread migrations are common).
* **Condition B (Treatment)**: `cpuManagerPolicy: static` (Exclusive cores are assigned via `cpuset` cgroups; thread migrations are restricted).

---

## 4. Codebase Breakdown & Script Workflows

The project contains a set of orchestration bash scripts and Python utilities located in the root and `scripts/` directory:

### 4.1 Orchestration & Scheduling: [run_experiment.sh](file:///Users/fernandanp/ILKOM_UI/Magang%20Riset%20Genap%2025-26/skripshit/crossover-experiment/scripts/run_experiment.sh)
This script orchestrates the experiment loop. It iterates through **5 selected benchmark instances** (from the Mittelmann set) and runs each **15 times** for a given condition.
* **Sequential Solver execution**: Required because the Gurobi Academic WLS license limits concurrent sessions to 2.
* **RFC 1123 Pod Naming Sanitation**: Translates instance names containing capital letters and underscores (e.g., `L1_sixm1000obs`) to lowercase and hyphens (e.g., `l1-sixm1000obs`) to avoid rejection by the Kubernetes API.
* **Gurobi WLS License Safeguards**:
  * Inserts a **10-second cooldown** between successful runs to allow the Gurobi WLS license server to release the token.
  * Inserts a **300-second (5-minute) cooldown** if a Pod fails (e.g., due to `OOMKilled`) to allow the remote WLS token lease to expire.
* **Startup Timeout (120s)**: Prevents the orchestration loop from hanging indefinitely if a container fails to start (e.g., due to `ImagePullBackOff` or `ErrImagePull`).

### 4.2 In-Container Solver: [run_solver.py](file:///Users/fernandanp/ILKOM_UI/Magang%20Riset%20Genap%2025-26/skripshit/crossover-experiment/scripts/run_solver.py)
This script is executed inside the solver container:
* **Connection Resilience**: Implements a linear backoff retry mechanism (3 attempts with `5 * attempt` seconds delay) when building the Gurobi environment to handle transient network blips to the WLS server.
* **Algorithm Constraints**: Sets `Method = 2` (forces pure barrier solving) and `Crossover = 4` (forces crossover) to ensure a clear distinction between the barrier and crossover phases.
* **High-Precision Timing Callback**: Implements `PhaseTimingCallback` which listens to `GRB.Callback.BARRIER` and `GRB.Callback.SIMPLEX` events:
  * Captures the exact Gurobi runtime clock (`GRB.Callback.RUNTIME`) at the start and end of the barrier iterations.
  * Captures the exact runtime clock at the start of the simplex iterations (`crossover_first_seen_runtime`).
  * Subtracts `crossover_first_seen_runtime` from the final model optimization runtime (`model.Runtime`) to calculate `crossover_seconds`.
  * Logs the absolute UNIX epoch (`time.time()`) right before calling `model.optimize()` as `optimize_start_epoch_unix`. This is crucial for host-side metric alignment.
* **License Disposal**: Uses a `try...finally` block to guarantee `model.dispose()` and `env.dispose()` are executed, preventing hung license tokens.

### 4.3 Host-Side System Monitoring: [collect_system_metrics.py](file:///Users/fernandanp/ILKOM_UI/Magang%20Riset%20Genap%2025-26/skripshit/crossover-experiment/scripts/collect_system_metrics.py)
This script runs in the background on the host VM, started in parallel with the solver Pod.
* **CRI Container Tracking**: 
  1. Searches for the Pod sandbox ID using `crictl pods --name <pod_name>`.
  2. Queries the specific container ID inside that sandbox with `crictl ps --pod <sandbox_id> --name solver`.
  3. Inspects the container to find its host process PID.
* **Cgroup v2 Locating**: Extracts the container's cgroup path from `/proc/<pid>/cgroup` (verifying it points under `/sys/fs/cgroup/`).
* **Continuous Polling (50ms)**: Reads `/proc/<pid>/status` for `nonvoluntary_ctxt_switches` at 50ms intervals (`poll_interval=0.05`), recording a time-series of context-switches and timestamps.
* **Temporal Crossover Alignment**: Because the context-switch counts are recorded over the container's entire lifespan (including startup, file loading, and barrier iterations), the script aligns the samples post-run. It reads the solver's JSON output to find the absolute epoch timestamps of the crossover phase:
  $$\text{crossover\_start\_epoch} = \text{optimize\_start\_epoch\_unix} + \text{crossover\_start\_runtime}$$
  $$\text{crossover\_end\_epoch} = \text{optimize\_start\_epoch\_unix} + \text{crossover\_end\_runtime}$$
  It then isolates the context switch counts within this window to compute `involuntary_ctxt_switches_delta_crossover_phase_only`.
* **CFS Throttling Statistics**: Records the container cgroup's `cpu.stat` delta (`nr_throttled`, `throttled_usec`) over the run to confirm that CPU pinning benefits are not confounded by CFS quota throttling.

### 4.4 Policy Management: [switch_cpu_manager_policy.sh](file:///Users/fernandanp/ILKOM_UI/Magang%20Riset%20Genap%2025-26/skripshit/crossover-experiment/scripts/switch_cpu_manager_policy.sh)
Automates the transition of the CPU manager policy on the single-node cluster:
1. `kubectl drain <node>` evicts active workloads.
2. Stops the `kubelet` system service.
3. **Deletes `/var/lib/kubelet/cpu_manager_state`**: A critical step because the Kubelet will fail to start if its state file contains old core assignments that contradict the newly set policy.
4. Overwrites `/var/lib/kubelet/config.yaml` with the target config template.
5. Restarts the `kubelet` system service.
6. `kubectl uncordon <node>` restores scheduling.

### 4.5 Statistical Analysis: [analyze_results.py](file:///Users/fernandanp/ILKOM_UI/Magang%20Riset%20Genap%2025-26/skripshit/crossover-experiment/scripts/analyze_results.py)
This script aggregates the result files and computes statistical validations for the 4 research questions:
* **Validation Filtering**: Excludes non-optimal runs (Gurobi status code $\neq 2$) and logs failures.
* **RQ1 Analysis**: Runs two-sided **Mann-Whitney U tests** on crossover durations between conditions. Applies **Bonferroni correction** to control the family-wise error rate across multiple tests:
  $$\alpha_{\text{corrected}} = \frac{0.05}{N_{\text{instances}}} = \frac{0.05}{5} = 0.01$$
* **RQ2 Analysis**: Computes **Spearman's rank correlation ($\rho$)** between involuntary context switches and crossover duration **separately per condition** (`none` and `static`) to avoid Simpson's Paradox.
* **RQ3 Analysis**: Runs two-sided Mann-Whitney U tests on `barrier_seconds` to check for barrier stability. A non-significant p-value ($p > 0.05$) verifies that the barrier phase is not affected by the policy change, validating crossover comparisons.
* **RQ4 Analysis**: Computes the **rank-biserial correlation ($r$)** as the effect size metric from the Mann-Whitney U statistic:
  $$r = \frac{2U}{n_{\text{none}} \cdot n_{\text{static}}} - 1$$
  Along with the percentage reduction of median crossover times, this is compared across instances to assess correlation with size and sparsity.

---

## 5. Summary of Key Files and Directories

* **`README.md`**: Provides the architectural design, directory mappings, and run guides.
* **`SETUP_GUIDE.md`**: Step-by-step instructions from GCP VM provisioning, Kubernetes installation, Docker setup, to running the experiment and generating analytical results.
* **`Dockerfile`**: Defines the solver container based on `gurobi/python:11.0.3` with `psutil`.
* **`emps.c`**: David M. Gay's C utility to uncompress Netlib LP formats into `.mps` files, kept in the codebase to facilitate benchmark expansion.
* **`kubelet-configs/`**: Kubelet configurations containing `cpuManagerPolicy` mappings and system reservations.
* **`manifests/`**: Kubernetes resources, including namespace, storage claims, and the `pod-template.yaml` parameterized for QoS Guaranteed settings.
* **`local/`**: Contains academic proposal text, debugging journals, and historical context:
  * `Proposal final revised claude.md`: Full thesis proposal draft containing the theory, limitations, and variables.
  * `.debug-journal.md`: Log of the Docker/containerd configuration lock issue.
  * `error.log`: Historical logs showing Kubelet configuration validation failures.

---

## 6. Critical Technical Bugs Addressed

Reviewing the file evolution (`*_old` vs current versions) highlights several engineering improvements:

1. **`crictl` Sandbox ID lookup bug**: 
   * *Problem*: `crictl ps --pod` was originally called with the Kubernetes Pod Name. However, `crictl` requires the pod sandbox ID. This caused the container PID lookup to return empty, causing the monitoring script to fail.
   * *Solution*: Rewritten to perform a two-step lookup: first query the sandbox ID via `crictl pods --name`, then query the container PID.
2. **Context-Switch Window Alignment**:
   * *Problem*: The initial version measured context switches by taking a snapshot before container startup and after container exit. This mixed in Python startup time, MPS model parsing, presolve, and the barrier iterations, diluting the crossover metric.
   * *Solution*: The metrics script was updated to sample context switches continuously at 50ms intervals. A temporal alignment function was introduced to crop the recorded time-series to the exact crossover epoch timestamps computed inside the container.
3. **Kubelet Boot Loop (Cgroup Validation)**:
   * *Problem*: Adding `systemReserved` and `kubeReserved` to the kubelet configurations without specifying the matching cgroups led to systemd boot-loops for the kubelet service.
   * *Solution*: Configs and policy-switching scripts were adjusted to handle configuration overrides safely, and rollback guides were added to the documentation.
4. **Interactive `dpkg` Prompts in Non-Interactive Shells**:
   * *Problem*: During Docker installation, `dpkg` blocked indefinitely on user-modified containerd configuration prompts when stdin was closed, causing VM provisioning to fail.
   * *Solution*: Resolved by configuring dpkg force options:
     `sudo DEBIAN_FRONTEND=noninteractive dpkg --configure --force-confdef --force-confold -a`
5. **Gurobi WLS License Throttling**:
   * *Problem*: Rapidly spawning pods for 150 consecutive runs caused license environment checkouts to fail due to network delay or concurrent session locks.
   * *Solution*: Implemented linearly backing off retries (up to 3 times) for Gurobi env initialization, and added a 10s delay between successful runs and a 300s delay on failed runs.

---

## 7. How to Execute the Experiment Pipeline

For reference, the complete experiment workflow is run as follows:

```bash
# 1. Ensure the node is in Condition A (none policy)
bash scripts/run_experiment.sh none 15 2

# 2. Transition CPU Manager policy to static (drains node, clears state, restarts Kubelet)
bash scripts/switch_cpu_manager_policy.sh static

# 3. Run Condition B (static policy)
bash scripts/run_experiment.sh static 15 2

# 4. Generate statistical analysis and combined reports
python3 scripts/analyze_results.py --results-dir /mnt/experiment-data/results
```
