"""
Network scanner using ARP to discover devices on the local subnet.
Requires root privileges.
"""
import ipaddress
import socket
import subprocess
import re
from typing import Optional
import netifaces
from scapy.all import ARP, Ether, srp, conf

# Partial OUI database for vendor lookup (MAC prefix -> vendor)
OUI_DB = {
    "00:50:56": "VMware",
    "00:0C:29": "VMware",
    "00:1A:11": "Google",
    "B8:27:EB": "Raspberry Pi",
    "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi",
    "00:16:3E": "Xen",
    "08:00:27": "VirtualBox",
    "52:54:00": "QEMU/KVM",
    "00:1B:21": "Intel",
    "00:1E:65": "Apple",
    "3C:15:C2": "Apple",
    "A4:5E:60": "Apple",
    "F0:18:98": "Apple",
    "00:25:00": "Apple",
    "40:6C:8F": "Apple",
    "B8:E8:56": "Apple",
    "00:50:F2": "Microsoft",
    "00:15:5D": "Microsoft (Hyper-V)",
    "00:1D:7E": "Cisco",
    "00:1F:CA": "Cisco",
    "58:AC:78": "Cisco",
    "00:0D:60": "Cisco",
    "00:14:69": "Cisco",
    "FC:FB:FB": "Cisco",
    "00:60:2F": "Cisco",
    "00:E0:F9": "Cisco",
    "00:1C:B0": "Cisco",
    "74:DE:2B": "Amazon",
    "F0:27:65": "Amazon",
    "40:B4:CD": "Amazon",
    "00:BB:3A": "Amazon",
    "1C:12:B0": "Samsung",
    "00:07:AB": "Samsung",
    "CC:07:AB": "Samsung",
    "00:1D:25": "Samsung",
    "70:F9:27": "Samsung",
    "F4:09:D8": "Samsung",
    "20:13:E0": "Huawei",
    "00:E0:FC": "Huawei",
    "14:B9:68": "Huawei",
    "44:6E:E5": "Huawei",
    "00:18:82": "Huawei",
    "9C:28:EF": "Huawei",
    "9C:74:1A": "TP-Link",
    "50:C7:BF": "TP-Link",
    "E8:94:F6": "TP-Link",
    "00:1D:0F": "ASUS",
    "00:26:18": "ASUS",
    "AC:9E:17": "ASUS",
    "2C:FD:A1": "ASUS",
    "00:22:6B": "Netgear",
    "00:26:F2": "Netgear",
    "20:4E:7F": "Netgear",
    "C4:3D:C7": "Netgear",
    "00:1F:33": "Netgear",
    "B0:48:7A": "D-Link",
    "00:05:5D": "D-Link",
    "00:1B:11": "D-Link",
    "FC:75:16": "D-Link",
    "1C:BD:B9": "D-Link",
}


def get_vendor(mac: str) -> str:
    """Look up vendor from MAC address OUI prefix."""
    prefix = mac.upper()[:8]
    # Try exact 3-byte OUI
    if prefix in OUI_DB:
        return OUI_DB[prefix]
    # Try 2-byte prefix
    short = mac.upper()[:5]
    for key, val in OUI_DB.items():
        if key.upper().startswith(short):
            return val
    return "Unknown"


def get_hostname(ip: str) -> str:
    """Attempt reverse DNS lookup."""
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ip


def get_default_interface() -> Optional[str]:
    """Get the default network interface."""
    try:
        gws = netifaces.gateways()
        default = gws.get("default", {})
        if netifaces.AF_INET in default:
            return default[netifaces.AF_INET][1]
    except Exception:
        pass
    # Fallback: try common names
    for iface in ["eth0", "wlan0", "en0", "ens33", "ens3", "enp0s3"]:
        if iface in netifaces.interfaces():
            return iface
    interfaces = netifaces.interfaces()
    for iface in interfaces:
        if iface != "lo":
            return iface
    return None


def get_interface_info(iface: str) -> dict:
    """Get IP and MAC for a given interface."""
    info = {"ip": None, "mac": None, "network": None}
    try:
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_INET in addrs:
            ip_info = addrs[netifaces.AF_INET][0]
            info["ip"] = ip_info.get("addr")
            netmask = ip_info.get("netmask", "255.255.255.0")
            if info["ip"]:
                interface = ipaddress.IPv4Interface(f"{info['ip']}/{netmask}")
                info["network"] = str(interface.network)
        if netifaces.AF_LINK in addrs:
            info["mac"] = addrs[netifaces.AF_LINK][0].get("addr", "").upper()
    except Exception:
        pass
    return info


def get_gateway_ip() -> Optional[str]:
    """Get the default gateway IP."""
    try:
        gws = netifaces.gateways()
        default = gws.get("default", {})
        if netifaces.AF_INET in default:
            return default[netifaces.AF_INET][0]
    except Exception:
        pass
    return None


def get_gateway_mac(gateway_ip: str, iface: str) -> Optional[str]:
    """Get the MAC address of the gateway via ARP."""
    try:
        conf.verb = 0
        arp = ARP(pdst=gateway_ip)
        ether = Ether(dst="ff:ff:ff:ff:ff:ff")
        packet = ether / arp
        result = srp(packet, timeout=2, iface=iface, verbose=False)[0]
        if result:
            return result[0][1].hwsrc.upper()
    except Exception:
        pass
    return None


def scan_network(iface: str, network: str, timeout: int = 2) -> list:
    """
    Perform ARP scan on the given network subnet.
    Returns list of device dicts with ip, mac, hostname, vendor.
    """
    conf.verb = 0
    devices = []
    try:
        arp = ARP(pdst=network)
        ether = Ether(dst="ff:ff:ff:ff:ff:ff")
        packet = ether / arp
        result = srp(packet, timeout=timeout, iface=iface, verbose=False)[0]

        for sent, received in result:
            ip = received.psrc
            mac = received.hwsrc.upper()
            hostname = get_hostname(ip)
            vendor = get_vendor(mac)
            devices.append({
                "ip": ip,
                "mac": mac,
                "hostname": hostname,
                "vendor": vendor,
                "is_gateway": False,
                "is_cut": False,
                "bandwidth_limit": None,  # kbps, None = unlimited
                "upload_limit": None,
                "download_limit": None,
                "bytes_sent": 0,
                "bytes_recv": 0,
            })
    except Exception as e:
        print(f"[Scanner] Error scanning network: {e}")
    return devices
