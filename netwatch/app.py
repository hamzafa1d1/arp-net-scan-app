"""
NetWatch - Network Management Tool
REST API + WebSocket server for network visualization, device control, and QoS.

IMPORTANT: Must be run as root (requires raw socket access and tc privileges).
Usage:  sudo python app.py
"""
import asyncio
import json
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scanner import (
    scan_network,
    get_default_interface,
    get_interface_info,
    get_gateway_ip,
    get_gateway_mac,
)
from arp_manager import ARPManager
from traffic_control import TrafficController

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("netwatch")

# ─── Global state ────────────────────────────────────────────────────────────
# ─── Default QoS policy ─────────────────────────────────────────────────────
policy_enabled:        bool          = True
DEFAULT_DOWNLOAD_KBPS: Optional[int] = 2048   # 2 Mbps download cap for all devices
DEFAULT_UPLOAD_KBPS:   Optional[int] = None   # no upload cap by default

devices: Dict[str, dict] = {}          # MAC -> device dict
iface: Optional[str] = None
gateway_ip: Optional[str] = None
gateway_mac: Optional[str] = None
our_ip: Optional[str] = None
our_mac: Optional[str] = None
network: Optional[str] = None

arp_manager = ARPManager()
tc = TrafficController()
ws_clients: Set[WebSocket] = set()
scan_task: Optional[asyncio.Task] = None
last_scan_time: float = 0


# ─── Startup / Shutdown ──────────────────────────────────────────────────────
def check_root():
    if os.geteuid() != 0:
        print("\n[ERROR] NetWatch requires root privileges.")
        print("  Run:  sudo python app.py\n")
        sys.exit(1)


def init_network():
    global iface, gateway_ip, gateway_mac, our_ip, our_mac, network

    iface = get_default_interface()
    if not iface:
        logger.error("Could not detect network interface")
        sys.exit(1)

    info = get_interface_info(iface)
    our_ip = info["ip"]
    our_mac = info["mac"]
    network = info["network"]

    gateway_ip = get_gateway_ip()
    if gateway_ip:
        gateway_mac = get_gateway_mac(gateway_ip, iface)

    logger.info(f"Interface: {iface} | IP: {our_ip} | MAC: {our_mac}")
    logger.info(f"Gateway: {gateway_ip} ({gateway_mac})")
    logger.info(f"Network: {network}")

    arp_manager.configure(iface, gateway_ip, gateway_mac)
    tc.configure(iface)


async def background_scanner():
    """Periodically rescan network and push updates to WebSocket clients."""
    global last_scan_time
    while True:
        try:
            await asyncio.sleep(15)  # Scan every 15 seconds
            if network:
                loop = asyncio.get_event_loop()
                found = await loop.run_in_executor(
                    None, scan_network, iface, network, 2
                )
                _merge_devices(found)
                last_scan_time = time.time()
                await broadcast({"type": "devices", "data": list(devices.values())})
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Scanner error: {e}")


def _enforce_default_limit(dev: dict):
    """
    Apply the global default QoS policy to a device if:
      - it is not the gateway or this machine
      - policy is enabled and limits are configured
      - the device's current limit doesn't already match (avoids redundant tc calls)
    Starts ARP spoofing automatically so traffic is routed through us.
    """
    if dev.get("is_gateway") or dev.get("is_self"):
        return
    if not policy_enabled or (DEFAULT_DOWNLOAD_KBPS is None and DEFAULT_UPLOAD_KBPS is None):
        return

    # Only re-apply if limits differ from current state
    if (dev.get("download_limit") == DEFAULT_DOWNLOAD_KBPS and
            dev.get("upload_limit") == DEFAULT_UPLOAD_KBPS):
        return

    ip  = dev["ip"]
    mac = dev["mac"]

    # ARP spoof first so traffic flows through us
    if not arp_manager.is_cutting(ip):
        arp_manager.cut(ip, mac)
        dev["is_cut"] = True

    tc.set_limit(ip, DEFAULT_DOWNLOAD_KBPS, DEFAULT_UPLOAD_KBPS)
    dev["download_limit"] = DEFAULT_DOWNLOAD_KBPS
    dev["upload_limit"]   = DEFAULT_UPLOAD_KBPS
    dev["bandwidth_limit"] = (DEFAULT_DOWNLOAD_KBPS or 0) + (DEFAULT_UPLOAD_KBPS or 0)
    logger.info(
        f"[QoS] Default limit applied to {ip} ({mac}): "
        f"DL={DEFAULT_DOWNLOAD_KBPS}kbps UL={DEFAULT_UPLOAD_KBPS}kbps"
    )


def _merge_devices(found: list):
    """Merge scan results into device state, preserving cut/limit state."""
    global devices
    found_macs = set()

    # Add gateway as special device
    if gateway_ip and gateway_mac:
        gw_mac = gateway_mac.upper()
        found_macs.add(gw_mac)
        if gw_mac not in devices:
            from scanner import get_hostname, get_vendor
            devices[gw_mac] = {
                "ip": gateway_ip,
                "mac": gw_mac,
                "hostname": get_hostname(gateway_ip),
                "vendor": get_vendor(gw_mac),
                "is_gateway": True,
                "is_self": False,
                "is_cut": False,
                "bandwidth_limit": None,
                "upload_limit": None,
                "download_limit": None,
                "online": True,
                "last_seen": time.time(),
            }
        else:
            devices[gw_mac]["online"] = True
            devices[gw_mac]["last_seen"] = time.time()

    # Add our own device
    if our_mac:
        self_mac = our_mac.upper()
        found_macs.add(self_mac)
        if self_mac not in devices:
            from scanner import get_vendor
            devices[self_mac] = {
                "ip": our_ip,
                "mac": self_mac,
                "hostname": "This Device",
                "vendor": get_vendor(self_mac),
                "is_gateway": False,
                "is_self": True,
                "is_cut": False,
                "bandwidth_limit": None,
                "upload_limit": None,
                "download_limit": None,
                "online": True,
                "last_seen": time.time(),
            }
        else:
            devices[self_mac]["online"] = True
            devices[self_mac]["last_seen"] = time.time()

    for d in found:
        mac = d["mac"].upper()
        found_macs.add(mac)

        is_gw = (d["ip"] == gateway_ip)
        is_self = (d["ip"] == our_ip)

        if mac in devices:
            # Update mutable fields, preserve control state
            existing = devices[mac]
            existing["ip"] = d["ip"]
            existing["hostname"] = d["hostname"]
            existing["vendor"] = d["vendor"]
            existing["online"] = True
            existing["last_seen"] = time.time()
            existing["is_gateway"] = is_gw
            existing["is_self"] = is_self
            _enforce_default_limit(existing)
        else:
            devices[mac] = {
                **d,
                "mac": mac,
                "is_gateway": is_gw,
                "is_self": is_self,
                "is_cut": False,
                "bandwidth_limit": None,
                "upload_limit": None,
                "download_limit": None,
                "online": True,
                "last_seen": time.time(),
            }
            _enforce_default_limit(devices[mac])

    # Mark devices not seen as offline (but keep them in the list)
    cutoff = time.time() - 60
    for mac, dev in devices.items():
        if mac not in found_macs and dev.get("last_seen", 0) < cutoff:
            dev["online"] = False


# ─── WebSocket broadcast ─────────────────────────────────────────────────────
async def broadcast(data: dict):
    dead = set()
    message = json.dumps(data)
    for ws in ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


# ─── FastAPI App ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scan_task
    check_root()
    init_network()

    # Initial scan
    loop = asyncio.get_event_loop()
    found = await loop.run_in_executor(None, scan_network, iface, network, 3)
    _merge_devices(found)
    last_scan_time = time.time()

    scan_task = asyncio.create_task(background_scanner())
    logger.info("NetWatch startup complete")
    yield

    # Cleanup
    if scan_task:
        scan_task.cancel()
    arp_manager.restore_all(list(devices.values()))
    tc.remove_all()
    logger.info("NetWatch stopped, ARP tables restored")


app = FastAPI(title="NetWatch", lifespan=lifespan)


# ─── API Models ──────────────────────────────────────────────────────────────
class ThrottleRequest(BaseModel):
    download_kbps: Optional[int] = None  # None = unlimited
    upload_kbps: Optional[int] = None

class PolicyRequest(BaseModel):
    enabled: bool
    download_kbps: Optional[int] = None
    upload_kbps: Optional[int] = None


# ─── API Routes ──────────────────────────────────────────────────────────────
@app.get("/api/status")
async def get_status():
    return {
        "iface": iface,
        "our_ip": our_ip,
        "our_mac": our_mac,
        "gateway_ip": gateway_ip,
        "gateway_mac": gateway_mac,
        "network": network,
        "device_count": len(devices),
        "last_scan": last_scan_time,
    }


@app.get("/api/devices")
async def get_devices():
    return list(devices.values())


@app.get("/api/policy")
async def get_policy():
    return {
        "enabled": policy_enabled,
        "download_kbps": DEFAULT_DOWNLOAD_KBPS,
        "upload_kbps": DEFAULT_UPLOAD_KBPS,
    }


@app.post("/api/policy")
async def set_policy(req: PolicyRequest):
    global policy_enabled, DEFAULT_DOWNLOAD_KBPS, DEFAULT_UPLOAD_KBPS
    policy_enabled        = req.enabled
    DEFAULT_DOWNLOAD_KBPS = req.download_kbps
    DEFAULT_UPLOAD_KBPS   = req.upload_kbps

    # Re-apply or lift limits on all existing controllable devices
    for dev in devices.values():
        if dev.get("is_gateway") or dev.get("is_self"):
            continue
        if policy_enabled and (DEFAULT_DOWNLOAD_KBPS or DEFAULT_UPLOAD_KBPS):
            # Force re-apply by resetting tracked limits so _enforce sees a change
            dev["download_limit"] = None
            dev["upload_limit"]   = None
            _enforce_default_limit(dev)
        else:
            # Policy disabled — remove limits and stop ARP spoofing
            tc.remove_limit(dev["ip"])
            arp_manager.restore(dev["ip"], dev["mac"])
            dev["is_cut"]        = False
            dev["download_limit"] = None
            dev["upload_limit"]   = None
            dev["bandwidth_limit"] = None

    policy_data = {
        "enabled": policy_enabled,
        "download_kbps": DEFAULT_DOWNLOAD_KBPS,
        "upload_kbps": DEFAULT_UPLOAD_KBPS,
    }
    await broadcast({"type": "policy", "data": policy_data})
    await broadcast({"type": "devices", "data": list(devices.values())})
    logger.info(f"[QoS] Policy updated: {policy_data}")
    return policy_data


@app.post("/api/scan")
async def trigger_scan():
    """Manually trigger a network scan."""
    global last_scan_time
    loop = asyncio.get_event_loop()
    found = await loop.run_in_executor(None, scan_network, iface, network, 3)
    _merge_devices(found)
    last_scan_time = time.time()
    devs = list(devices.values())
    await broadcast({"type": "devices", "data": devs})
    return {"count": len(devs), "scanned_at": last_scan_time}


@app.post("/api/devices/{mac}/cut")
async def cut_device(mac: str):
    """Cut off a device from the network via ARP spoofing."""
    mac = mac.upper()
    if mac not in devices:
        raise HTTPException(status_code=404, detail="Device not found")

    dev = devices[mac]
    if dev.get("is_gateway") or dev.get("is_self"):
        raise HTTPException(status_code=400, detail="Cannot cut gateway or this device")

    success = arp_manager.cut(dev["ip"], dev["mac"])
    if not success:
        raise HTTPException(status_code=500, detail="Failed to start ARP spoofing")

    dev["is_cut"] = True
    await broadcast({"type": "device_update", "data": dev})
    logger.info(f"Cut device: {dev['ip']} ({dev['hostname']})")
    return {"status": "cut", "device": dev}


@app.post("/api/devices/{mac}/restore")
async def restore_device(mac: str):
    """Restore a device's network access."""
    mac = mac.upper()
    if mac not in devices:
        raise HTTPException(status_code=404, detail="Device not found")

    dev = devices[mac]
    arp_manager.restore(dev["ip"], dev["mac"])
    dev["is_cut"] = False
    await broadcast({"type": "device_update", "data": dev})
    logger.info(f"Restored device: {dev['ip']} ({dev['hostname']})")
    return {"status": "restored", "device": dev}


@app.post("/api/devices/{mac}/throttle")
async def throttle_device(mac: str, req: ThrottleRequest):
    """Set bandwidth limits for a device."""
    mac = mac.upper()
    if mac not in devices:
        raise HTTPException(status_code=404, detail="Device not found")

    dev = devices[mac]
    if dev.get("is_gateway") or dev.get("is_self"):
        raise HTTPException(status_code=400, detail="Cannot throttle gateway or this device")

    if not dev.get("is_cut"):
        # Need to ARP spoof first to intercept traffic
        arp_manager.cut(dev["ip"], dev["mac"])
        dev["is_cut"] = True

    tc.set_limit(dev["ip"], req.download_kbps, req.upload_kbps)
    dev["download_limit"] = req.download_kbps
    dev["upload_limit"] = req.upload_kbps
    dev["bandwidth_limit"] = (req.download_kbps or 0) + (req.upload_kbps or 0)

    await broadcast({"type": "device_update", "data": dev})
    logger.info(
        f"Throttled {dev['ip']}: DL={req.download_kbps}kbps UL={req.upload_kbps}kbps"
    )
    return {"status": "throttled", "device": dev}


@app.delete("/api/devices/{mac}/throttle")
async def remove_throttle(mac: str):
    """Remove bandwidth limit from a device."""
    mac = mac.upper()
    if mac not in devices:
        raise HTTPException(status_code=404, detail="Device not found")

    dev = devices[mac]
    tc.remove_limit(dev["ip"])
    dev["download_limit"] = None
    dev["upload_limit"] = None
    dev["bandwidth_limit"] = None

    # If device was only throttled (not fully cut), restore it
    if not dev.get("is_cut"):
        arp_manager.restore(dev["ip"], dev["mac"])

    await broadcast({"type": "device_update", "data": dev})
    return {"status": "unlimited", "device": dev}


# ─── WebSocket ───────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    # Send initial state
    await websocket.send_text(json.dumps({
        "type": "init",
        "status": {
            "iface": iface, "our_ip": our_ip, "gateway_ip": gateway_ip, "network": network
        },
        "policy": {
            "enabled": policy_enabled,
            "download_kbps": DEFAULT_DOWNLOAD_KBPS,
            "upload_kbps": DEFAULT_UPLOAD_KBPS,
        },
        "data": list(devices.values()),
    }))
    try:
        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)
            if data.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        ws_clients.discard(websocket)
    except Exception:
        ws_clients.discard(websocket)


# ─── Static files ────────────────────────────────────────────────────────────
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(static_dir, "index.html"))


# ─── Entry point ─────────────────────────────────────────────────────────────
def find_free_port(start: int = 8080) -> int:
    import socket
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start  # fallback, uvicorn will report the error


if __name__ == "__main__":
    port = find_free_port(8080)
    if port != 8080:
        print(f"[NetWatch] Port 8080 in use, using port {port}")
    print(f"[NetWatch] Open http://localhost:{port}")
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
