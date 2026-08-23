# Ubuntu + NVIDIA GPU runbook

Use this document when JobOS/Ollama should use an NVIDIA GPU, especially after
seeing either of these messages:

- `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver`
- `environment does not provide /dev/nvidia` or an Ollama GPU discovery error

This applies to native Ubuntu 22.04/24.04/26.04 **when that Ubuntu machine is
the compute host**. WSL2 has a different host driver model; use the Windows
NVIDIA driver plus Docker Desktop WSL integration instead of installing a Linux
GPU driver inside the distro.

The project can also be split across machines. In the workflow described in
the Gemini PDF, the ThinkPad Ubuntu machine is a capture/client machine while
the large Windows workstation runs DaVinci, SAM3, and simulator workloads. In
that arrangement, do **not** try to install NVIDIA Container Toolkit on the
ThinkPad unless it actually has a supported NVIDIA GPU. Use a token API or a
secured remote Ollama endpoint from the Ubuntu JobOS client instead.

## Choose the correct deployment profile

| Machine role | Install Ollama/GPU stack there? | JobOS LLM setting |
|---|---|---|
| Ubuntu GPU worker with NVIDIA GPU | Yes: native Ollama or Docker GPU overlay | `JOBOS_LLM_BACKEND=ollama`, local `OLLAMA_URL` |
| Ubuntu ThinkPad/capture-client without NVIDIA GPU | No | API backend, or SSH tunnel to compute machine |
| Windows RTX workstation | Run GPU workload natively/WSL2 on Windows, not inside the Ubuntu client | Tunnel its loopback Ollama endpoint or use API |

### Secure remote Ollama path

Ollama has no authentication by default. Do not bind port `11434` to `0.0.0.0`
or expose it to the Internet. If the GPU Ollama service is on the Windows
workstation and JobOS runs on Ubuntu, keep Ollama bound to the workstation's
loopback and create an SSH tunnel from Ubuntu:

```bash
# On Ubuntu JobOS client; requires Windows OpenSSH Server or another SSH host.
ssh -N -L 11434:127.0.0.1:11434 <windows-user>@<windows-host>
```

Then keep this in the Ubuntu JobOS `.env`:

```env
JOBOS_LLM_BACKEND=ollama
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_BASE_URL=http://127.0.0.1:11434
```

This leaves all requests encrypted over SSH and avoids sharing model API keys
or opening an unauthenticated inference service on the LAN.

## Safety boundary

Run the read-only doctor first:

```bash
cd ~/job-apply-os
bash scripts/ubuntu_ollama_gpu_doctor.sh
```

It never reads `.env`, browser profiles, cookies, or API keys. Share its output
with Gemini if needed. Run the optional Docker test only when a public image
pull is acceptable:

```bash
bash scripts/ubuntu_ollama_gpu_doctor.sh --docker-smoke
```

Do not use `sudo rm`, manually delete `/dev/nvidia*`, or install a `.run`
driver over Ubuntu's packaged driver while diagnosing. Those actions can leave
the kernel and DKMS state inconsistent.

## Decision tree

| Doctor result | Root cause | Repair path |
|---|---|---|
| `nvidia-smi` fails on host | Linux driver/kernel/DKMS problem | Repair host driver first; Ollama/Docker cannot fix it. |
| Host `nvidia-smi` works, Docker smoke fails | Docker lacks NVIDIA runtime | Install/configure NVIDIA Container Toolkit, restart Docker. |
| Host + Docker smoke work, native Ollama is CPU | Ollama service/discovery/model placement | Restart service, inspect logs and `ollama ps`. |
| Host + Docker smoke work, Docker Ollama is CPU | Compose service lacks GPU reservation or Docker runtime lost devices | Use the provided overlay; restart Docker/Ollama. |

## 1. Fix host-driver failures first

The only first test that matters is:

```bash
nvidia-smi
```

If it shows the GPU table, proceed to Ollama. If it cannot communicate with the
driver, reboot once. A recent Ubuntu kernel update can temporarily leave an
NVIDIA DKMS module unloaded. After reboot, repeat `nvidia-smi`.

If it still fails, inspect rather than guessing:

```bash
uname -r
lsmod | grep -E '^nvidia|nvidia_uvm' || true
dkms status
sudo journalctl -k -b | grep -Ei 'nvrm|nvidia' | tail -100
```

On Secure Boot machines, `mokutil --sb-state` may show that a newly built DKMS
module has not been enrolled. Repair/reinstall the Ubuntu-packaged NVIDIA
driver appropriate for the machine, complete any MOK enrollment prompt, reboot,
then re-run `nvidia-smi`. Do not move to Docker/Ollama until this passes.

## 2. Native Ollama (recommended)

This is the least complicated Linux path: Docker runs Postgres/n8n/other JobOS
services while Ollama runs as a host systemd service and uses the host driver
directly.

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
sudo systemctl restart ollama
ollama pull qwen3:8b
ollama pull deepseek-r1:14b
ollama pull nomic-embed-text
ollama ps
```

After a request is in flight, `ollama ps` reports the processor split. For an
RTX 4090, start with one loaded model and modest context; it is more stable than
loading several large models concurrently:

```bash
sudo systemctl edit ollama
```

Add this drop-in, then save:

```ini
[Service]
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_KEEP_ALIVE=10m"
```

Apply it:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
journalctl -u ollama -f
```

For the initial JobOS setup, keep `JOBOS_LLM_BACKEND=ollama` and
`OLLAMA_URL=http://127.0.0.1:11434` in the untracked `.env`. Start with
`qwen3:8b`/`deepseek-r1:14b`; move a 32B model into one serial audit role only
after checking VRAM and latency.

## 3. Ollama in Docker

Use this only if you intentionally want model files inside a Docker volume. Do
not also run native Ollama, because both listen on port `11434`.

First install the official NVIDIA Container Toolkit on Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg2
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Prove Docker sees the GPU before starting Ollama:

```bash
bash scripts/ubuntu_ollama_gpu_doctor.sh --docker-smoke
```

Then start the project overlay and pull models inside that container:

```bash
cd ~/job-apply-os
docker compose -f docker-compose.yml -f docker-compose.ollama-gpu.yml up -d ollama
docker exec -it jobos-ollama ollama pull qwen3:8b
docker exec -it jobos-ollama ollama pull deepseek-r1:14b
docker exec -it jobos-ollama ollama pull nomic-embed-text
docker exec -it jobos-ollama ollama ps
```

The overlay requests all NVIDIA GPUs via Compose's device reservation and binds
Ollama only to `127.0.0.1:11434`. Do not add `--disable-gpu`; that flag belongs
only to the separate headless Chrome browser container.

## 4. If GPU worked, then Ollama switched to CPU

Collect the facts:

```bash
nvidia-smi
ollama ps
journalctl -u ollama -n 200 --no-pager
# Docker mode instead:
docker logs --tail 200 jobos-ollama
```

For Docker, a `systemctl daemon-reload` can cause containers using systemd
cgroups to lose requested GPU devices. Restart Docker and recreate the Ollama
container, then re-run the smoke test:

```bash
sudo systemctl restart docker
docker compose -f docker-compose.yml -f docker-compose.ollama-gpu.yml up -d --force-recreate ollama
bash scripts/ubuntu_ollama_gpu_doctor.sh --docker-smoke
```

For native Ollama, NVIDIA's documented recovery steps include restarting
Ollama, checking that the `nvidia_uvm` module is loaded, and rebooting if GPU
initialization remains unavailable:

```bash
sudo systemctl restart ollama
sudo nvidia-modprobe -u
```

Only run `sudo rmmod nvidia_uvm` / `sudo modprobe nvidia_uvm` when no GPU job is
running; reboot instead if unsure.

## 5. Handoff prompt for Gemini

Paste the following with the doctor output, after removing only machine names
or paths you do not want to share. Never include `.env` or API keys.

```text
I am running JobOS on native Ubuntu with an NVIDIA GPU. I ran:
bash scripts/ubuntu_ollama_gpu_doctor.sh [--docker-smoke]

Diagnose only from the output below. Do not suggest deleting NVIDIA packages,
editing .env, copying browser cookies, or disabling security controls. First
classify the problem as host driver, Docker NVIDIA runtime, native Ollama
service, or model/VRAM placement. Give the smallest reversible next command
and explain what successful output should look like.
```

## References

- [NVIDIA Container Toolkit installation guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [NVIDIA Container Toolkit supported platforms](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/supported-platforms.html)
- [Ollama NVIDIA/Docker troubleshooting](https://docs.ollama.com/troubleshooting)
