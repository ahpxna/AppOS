#!/usr/bin/env bash
# Diagnose, but deliberately do not mutate, an Ubuntu NVIDIA/Ollama setup.
#
# This script is safe to share with Gemini or a maintainer: it reports OS,
# driver, Docker-runtime, and Ollama health, but never reads .env or prints
# tokens/cookies. `--docker-smoke` additionally pulls/runs a public CUDA image
# to prove that Docker can see the GPU; it is opt-in because it uses bandwidth.
set -uo pipefail

RUN_DOCKER_SMOKE=false
if [[ "${1:-}" == "--docker-smoke" ]]; then
  RUN_DOCKER_SMOKE=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--docker-smoke]" >&2
  exit 2
fi

PASS=0
WARN=0
FAIL=0

pass() { printf 'PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
warn() { printf 'WARN  %s\n' "$*"; WARN=$((WARN + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }
section() { printf '\n== %s ==\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

section "Host and NVIDIA driver"
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  printf 'OS: %s\n' "${PRETTY_NAME:-unknown}"
fi
printf 'Kernel: %s\n' "$(uname -r)"

if ! have nvidia-smi; then
  fail "nvidia-smi is not installed or not on PATH. Install/repair the host NVIDIA driver first."
elif NVIDIA_SMI_OUTPUT="$(nvidia-smi 2>&1)"; then
  pass "Host driver communicates with NVIDIA GPU."
  printf '%s\n' "$NVIDIA_SMI_OUTPUT"
else
  fail "nvidia-smi cannot communicate with the NVIDIA driver."
  printf '%s\n' "$NVIDIA_SMI_OUTPUT"
  printf '%s\n' "Next: reboot once after a kernel/driver update. If still broken, repair the Ubuntu NVIDIA driver/DKMS before troubleshooting Ollama or Docker."
fi

if have lsmod && lsmod | awk '$1 ~ /^nvidia(_uvm)?$/ { found=1 } END { exit !found }'; then
  pass "NVIDIA kernel module is loaded."
else
  warn "NVIDIA kernel module is not visible in lsmod (expected when the driver test above fails)."
fi

if compgen -G '/dev/nvidia*' >/dev/null; then
  pass "NVIDIA device nodes exist: $(printf '%s ' /dev/nvidia*)"
else
  warn "No /dev/nvidia* device node is visible to this shell."
fi

if have mokutil && mokutil --sb-state 2>/dev/null | grep -qi 'enabled'; then
  warn "Secure Boot is enabled. An unsigned NVIDIA DKMS module can fail to load until its MOK is enrolled."
fi

section "Ollama native service"
if have ollama; then
  printf 'Ollama CLI: %s\n' "$(ollama --version 2>&1 | head -1)"
  if have systemctl; then
    if systemctl is-active --quiet ollama 2>/dev/null; then
      pass "ollama.service is active."
    else
      warn "ollama.service is not active. For a native installation: sudo systemctl restart ollama"
    fi
  fi
  if curl --fail --silent --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    pass "Ollama API responds on 127.0.0.1:11434."
    printf '%s\n' "Loaded models (processor/GPU split is shown when supported):"
    ollama ps 2>&1 || true
  else
    warn "Ollama API does not respond on 127.0.0.1:11434."
  fi
else
  warn "Ollama CLI is not installed (fine if you use the Docker Ollama overlay or API-only mode)."
fi

section "Docker NVIDIA runtime"
if ! have docker; then
  warn "Docker is not installed (fine for native Ollama)."
elif ! docker info >/dev/null 2>&1; then
  fail "Docker daemon is unavailable to this user. Start Docker or fix Docker group permissions."
else
  pass "Docker daemon is reachable."
  if have nvidia-ctk; then
    pass "nvidia-container-toolkit is installed: $(nvidia-ctk --version 2>&1 | head -1)"
  else
    warn "nvidia-ctk is missing; Docker containers will not receive the GPU until NVIDIA Container Toolkit is installed/configured."
  fi
  if $RUN_DOCKER_SMOKE; then
    printf '%s\n' "Running GPU smoke test (may pull a public CUDA image)..."
    if docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi; then
      pass "Docker container can use the NVIDIA GPU."
    else
      fail "Docker GPU smoke test failed. Install/configure NVIDIA Container Toolkit, restart Docker, then rerun this command."
    fi
  else
    printf '%s\n' "SKIP  Docker GPU smoke test. Run: bash scripts/ubuntu_ollama_gpu_doctor.sh --docker-smoke"
  fi
fi

section "Next action"
printf 'Summary: %d pass, %d warning, %d fail\n' "$PASS" "$WARN" "$FAIL"
if (( FAIL > 0 )); then
  printf '%s\n' "Read docs/ubuntu_gpu.md and share this output (without .env) when asking Gemini for help."
  exit 1
fi
