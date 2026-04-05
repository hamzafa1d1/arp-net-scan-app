# NetWatch

Network management tool that visualizes devices on your local network, enforces bandwidth limits via QoS, and can block devices using ARP spoofing.

> **Only use on networks you own or have explicit authorization to manage.**

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.9+ | |
| Root / sudo | Raw sockets + traffic shaping require elevated privileges |
| Linux **or** macOS | Traffic shaping uses `tc`+`iptables` on Linux, `dnctl`+`pfctl` on macOS |
| libpcap | For packet capture (scapy) |

---

## Installation

### Linux (Debian / Ubuntu)

```bash
sudo apt-get install python3-pip python3-venv iproute2 iptables libpcap-dev

cd netwatch
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Or just run the setup script which does all of the above:

```bash
cd netwatch
bash setup.sh
```

### macOS

```bash
cd netwatch
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`pfctl` and `dnctl` are included with macOS — no extra packages needed.

---

## Running

NetWatch must be run as root so it can send raw ARP packets and configure traffic shaping rules.

```bash
cd netwatch
source venv/bin/activate
sudo venv/bin/python app.py
```

Then open your browser at the URL printed in the terminal (default **http://localhost:8080**).

> If port 8080 is already in use, NetWatch automatically picks the next available port and prints the correct URL.

---

## Features

### Network Visualization
- Automatically scans the local subnet on startup and every 15 seconds
- **Topology view** — D3.js force-directed graph. Nodes are color-coded:
  - Green — online
  - Yellow — throttled
  - Red — blocked
  - Blue — gateway
  - Purple — this machine
- **Device List view** — sortable table with per-device bandwidth bars
- Click any device to open the detail panel

### Default QoS Policy
The sidebar **Default QoS** card applies a bandwidth cap to every device automatically as it is discovered.

Default cap: **2 Mbps download** (no upload cap).

From the UI you can:
- Toggle the policy on/off — limits are applied or lifted on all devices instantly
- Edit the download / upload cap using the inline form or quick presets
- Changes take effect immediately without restarting

To change the default before startup, edit these two lines in `app.py`:

```python
DEFAULT_DOWNLOAD_KBPS: Optional[int] = 2048   # 2 Mbps — set to None to disable
DEFAULT_UPLOAD_KBPS:   Optional[int] = None   # no upload cap
```

### Per-Device Control
Open the detail panel for any device to:
- **Block** — cuts the device off from the network via ARP spoofing
- **Restore** — sends corrected ARP packets to both the device and the gateway
- **Custom throttle** — override the default QoS limit with a specific DL/UL value, or remove limits entirely

### Live Stats
The sidebar shows live counters that update on every scan:

```
[ N Online ]  [ N Throttled ]  [ N Blocked ]
```

---

## How it works

**Device discovery** — sends ARP broadcast requests across the entire subnet and collects replies. Hostname is resolved via reverse DNS. Vendor is looked up from the MAC OUI prefix.

**Blocking (ARP spoofing)** — sends forged ARP replies in a loop to both the target device (telling it the gateway's MAC is ours) and the gateway (telling it the target's MAC is ours). Since packets are not forwarded, the device loses internet access. ARP tables are fully restored on Restore or on app shutdown.

**Bandwidth throttling** — on Linux, uses `tc` HTB qdiscs and `iptables` MARK to rate-limit forwarded traffic per IP. On macOS, uses `dnctl` dummynet pipes and `pfctl` anchor rules. Throttling requires ARP spoofing to be active so traffic flows through this machine.

---

## File structure

```
netwatch/
├── app.py              # FastAPI server — REST API + WebSocket
├── scanner.py          # ARP-based subnet scanner
├── arp_manager.py      # ARP spoofing (cut / restore)
├── traffic_control.py  # Bandwidth limiting (Linux tc / macOS dnctl)
├── requirements.txt
├── setup.sh            # Linux setup script
└── static/
    ├── index.html
    ├── style.css
    └── app.js
```

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Interface, IP, gateway info |
| GET | `/api/devices` | All discovered devices |
| POST | `/api/scan` | Trigger an immediate scan |
| GET | `/api/policy` | Current default QoS policy |
| POST | `/api/policy` | Update default QoS policy |
| POST | `/api/devices/{mac}/cut` | Block a device |
| POST | `/api/devices/{mac}/restore` | Restore a blocked device |
| POST | `/api/devices/{mac}/throttle` | Set per-device bandwidth limit |
| DELETE | `/api/devices/{mac}/throttle` | Remove per-device bandwidth limit |
| WS | `/ws` | Real-time device + policy updates |
