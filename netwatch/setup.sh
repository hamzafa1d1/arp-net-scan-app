#!/bin/bash
# NetWatch setup script — Linux (Debian/Ubuntu/Arch)
set -e

echo "=== NetWatch Setup ==="

# Check Python 3.9+
python3 --version >/dev/null 2>&1 || { echo "[ERROR] Python 3.9+ required"; exit 1; }

# Install system dependencies
if command -v apt-get &>/dev/null; then
    echo "[*] Installing system packages (apt)..."
    sudo apt-get install -y python3-pip python3-venv iproute2 iptables libpcap-dev
elif command -v pacman &>/dev/null; then
    echo "[*] Installing system packages (pacman)..."
    sudo pacman -S --noconfirm python-pip iproute2 iptables libpcap
elif command -v dnf &>/dev/null; then
    echo "[*] Installing system packages (dnf)..."
    sudo dnf install -y python3-pip iproute iptables libpcap-devel
fi

# Create virtualenv
echo "[*] Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python packages
echo "[*] Installing Python dependencies..."
pip install -r requirements.txt

# Load IFB kernel module (needed for ingress shaping)
echo "[*] Loading kernel modules..."
sudo modprobe ifb 2>/dev/null || true
sudo modprobe sch_htb 2>/dev/null || true
sudo modprobe sch_tbf 2>/dev/null || true

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To run NetWatch:"
echo "  source venv/bin/activate"
echo "  sudo venv/bin/python app.py"
echo ""
echo "Then open: http://localhost:8080"
echo ""
echo "NOTE: Root privileges required for ARP/tc operations."
echo "Only use on networks you own or have authorization to manage."
