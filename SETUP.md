# Pandora – Setup Guide

Step-by-step instructions for getting a Firecracker microVM running inside Lima on macOS.

All commands below run **inside the Lima shell** unless noted otherwise.

---

## 1. Enter Lima

```bash
# On macOS
limactl shell pandora
```

You should see a prompt like `daulet@lima-pandora:`. Confirm you're in the project directory:

```bash
cd ~/Desktop/pandora
```

---

## 2. Install Firecracker

```bash
FC_VERSION="1.14.1"
ARCH="$(uname -m)"

# Download and extract to a temp location (Lima home mount must be writable)
curl -L "https://github.com/firecracker-microvm/firecracker/releases/download/v${FC_VERSION}/firecracker-v${FC_VERSION}-${ARCH}.tgz" \
  -o /tmp/firecracker.tgz

tar xzf /tmp/firecracker.tgz -C /tmp

# Copy the binary into the project
cp "/tmp/release-v${FC_VERSION}-${ARCH}/firecracker-v${FC_VERSION}-${ARCH}" ./bin/firecracker
chmod +x ./bin/firecracker

# Verify
./bin/firecracker --version
```

You should see something like `Firecracker v1.14.1`.

---

## 3. Get a kernel and rootfs

The images are discovered dynamically from Firecracker's CI S3 bucket. Run each
block in order.

### 3a. Discover and download the kernel

```bash
ARCH="$(uname -m)"
CI_VERSION="v1.14"

# Find the latest kernel available for this arch
latest_kernel_key=$(curl -s \
  "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/${CI_VERSION}/${ARCH}/vmlinux-&list-type=2" \
  | grep -oP "(?<=<Key>)(firecracker-ci/${CI_VERSION}/${ARCH}/vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3})(?=</Key>)" \
  | sort -V | tail -1)

echo "Kernel key: ${latest_kernel_key}"

curl -L -o ./images/hello-vmlinux.bin \
  "https://s3.amazonaws.com/spec.ccfc.min/${latest_kernel_key}"
```

### 3b. Discover and download the rootfs

The CI rootfs is a squashfs — it needs to be converted to ext4.

```bash
# Install tools needed for the conversion
sudo apt update && sudo apt install -y squashfs-tools e2fsprogs

# Find the latest Ubuntu rootfs
latest_ubuntu_key=$(curl -s \
  "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/${CI_VERSION}/${ARCH}/ubuntu-&list-type=2" \
  | grep -oP "(?<=<Key>)(firecracker-ci/${CI_VERSION}/${ARCH}/ubuntu-[0-9]+\.[0-9]+\.squashfs)(?=</Key>)" \
  | sort -V | tail -1)

echo "Rootfs key: ${latest_ubuntu_key}"

curl -L -o /tmp/rootfs.squashfs \
  "https://s3.amazonaws.com/spec.ccfc.min/${latest_ubuntu_key}"
```

### 3c. Convert squashfs → ext4

```bash
# Unpack the squashfs
cd /tmp
sudo unsquashfs -f rootfs.squashfs

# Create a 1 GB ext4 image from the unpacked tree
sudo chown -R root:root squashfs-root
truncate -s 1G rootfs.ext4
sudo mkfs.ext4 -d squashfs-root -F rootfs.ext4

# Copy into the project
cp rootfs.ext4 ~/Desktop/pandora/images/hello-rootfs.ext4

# Cleanup
sudo rm -rf /tmp/squashfs-root /tmp/rootfs.squashfs /tmp/rootfs.ext4

cd ~/Desktop/pandora
```

### 3d. Verify

```bash
ls -lh ./images/
# hello-vmlinux.bin  ~30-40 MB
# hello-rootfs.ext4  1.0 GB
file ./images/hello-vmlinux.bin
# Should say "Linux kernel ARM64 boot executable Image" on aarch64
#         or "ELF 64-bit LSB executable" on x86_64
```

---

## 4. Install Python dependencies

```bash
# Install uv if not already present
which uv || curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync deps from pyproject.toml
uv sync
```

This creates `.venv/` with all dependencies (FastAPI, paramiko, prometheus-client, etc.).

---

## 5. Prepare the rootfs (inject SSH key)

The API server executes commands inside the microVM via SSH. This step
generates an ed25519 key pair and injects the public key into the rootfs.

```bash
sudo ./prepare_rootfs.sh
```

This creates `keys/pandora` (private) and `keys/pandora.pub` (public), mounts
the rootfs image, copies the public key to `/root/.ssh/authorized_keys`, and
ensures sshd is enabled.

---

## 6. Check /dev/kvm

Firecracker needs KVM. Verify it exists:

```bash
ls -l /dev/kvm
```

If it's missing, your Lima VM wasn't started with KVM support. Stop here and recreate it:

```bash
# On macOS (not inside Lima)
limactl stop pandora
limactl delete pandora

# Recreate with VZ backend + nested virtualization + writable mount
limactl create --name=pandora \
  --vm-type=vz \
  --mount-writable \
  --set '.nestedVirtualization = true' \
  template://ubuntu-24.04
limactl start pandora
limactl shell pandora
```

> On Apple Silicon, `--vm-type=vz` uses Apple's Virtualization.framework.
> `nestedVirtualization: true` is required to expose `/dev/kvm` to the guest —
> without it, Firecracker cannot start.

If `/dev/kvm` exists but has wrong permissions:

```bash
sudo chmod 666 /dev/kvm
```

---

## 7. Run the API server

```bash
cd ~/Desktop/pandora
sudo .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
```

You should see:

```
[pandora:net] creating TAP device tap0
[pandora:net] network plumbing ready – microVM can reach the internet via eth0
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## 8. Test it

From another terminal (on your Mac or inside Lima):

```bash
# Health check
curl http://localhost:8000/health

# Execute a command inside a fresh microVM
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "echo hello from the sandbox && uname -a"}'

# Check Prometheus metrics
curl http://localhost:8000/metrics
```

Or using the Python SDK:

```python
from client import PandoraClient

with PandoraClient("http://localhost:8000") as p:
    result = p.execute("echo hello")
    print(result.stdout, result.boot_ms, result.exec_ms)
```

---

## 9. Quick self-test (no API server)

To test the VM lifecycle directly without the API server:

```bash
sudo .venv/bin/python vm_manager.py
```

This boots one VM, runs a test command over SSH, prints the output, and tears
everything down.

---

## 8. Manual cleanup (if something goes wrong)

If the script crashes without cleaning up:

```bash
# Kill any lingering firecracker process
sudo pkill -9 firecracker || true

# Remove stale socket
sudo rm -f /tmp/firecracker.socket

# Tear down network
sudo ./setup_network.sh teardown
```

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| `Read-only file system` on tar/cp | Lima mounts home as read-only by default | Recreate with `--mount-writable` or edit `~/.lima/pandora/lima.yaml` and set `writable: true` under mounts |
| `/dev/kvm: No such file` | Lima VM doesn't have KVM exposed | Use `--vm-type=vz` on Apple Silicon |
| `Permission denied` on `/dev/kvm` | KVM device has restrictive perms | `sudo chmod 666 /dev/kvm` |
| `API socket not ready after 5s` | Firecracker crashed on startup | Check `sudo .venv/bin/python vm_manager.py` stderr; likely missing kernel/rootfs or no KVM |
| `invalid Image magic number` | Kernel binary is corrupt or wrong format | Re-download using the S3 discovery commands in step 3a; verify with `file ./images/hello-vmlinux.bin` |
| `iptables: command not found` | Missing iptables in Lima guest | `sudo apt update && sudo apt install -y iptables` |
| `ip: command not found` | Missing iproute2 | `sudo apt update && sudo apt install -y iproute2` |
