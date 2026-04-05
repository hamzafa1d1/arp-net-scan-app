"""
Cross-platform traffic shaping module.

Linux:  tc (HTB qdisc) + iptables marks
macOS:  pfctl anchors + dnctl dummynet pipes

Both require root. The class auto-detects the OS and delegates to
the appropriate backend. On macOS, pf must be enabled (it usually is).
"""
import hashlib
import logging
import platform
import subprocess
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ─── Helpers ──────────────────────────────────────────────────────────────

def _run(cmd: str) -> tuple[int, str, str]:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _ip_to_id(ip: str) -> int:
    """Stable numeric ID for an IP (1-9000)."""
    return int(hashlib.md5(ip.encode()).hexdigest()[:4], 16) % 9000 + 100


IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# ─── macOS backend (pfctl + dnctl) ────────────────────────────────────────

PF_ANCHOR = "com.netwatch"


class MacOSTrafficController:
    """
    Uses dummynet pipes (dnctl) + pf anchor rules (pfctl) to limit
    per-device bandwidth.

    Pipe numbering:
      download pipe = id * 2
      upload pipe   = id * 2 + 1
    """

    def __init__(self):
        self.iface: Optional[str] = None
        self._limits: Dict[str, dict] = {}

    def configure(self, iface: str):
        self.iface = iface
        self._ensure_anchor()

    def _ensure_anchor(self):
        """Add netwatch anchor to pf if not already present."""
        rc, out, _ = _run("pfctl -s Anchors 2>/dev/null")
        if PF_ANCHOR not in out:
            # Add anchor reference to main ruleset via a temp file
            rc2, rules, _ = _run("pfctl -s rules 2>/dev/null")
            anchor_line = f'\nanchor "{PF_ANCHOR}"\n'
            dummynet_line = f'dummynet-anchor "{PF_ANCHOR}"\n'
            if PF_ANCHOR not in rules:
                combined = rules + dummynet_line + anchor_line
                tmpfile = "/tmp/pf_netwatch_main.conf"
                with open(tmpfile, "w") as f:
                    f.write(combined)
                _run(f"pfctl -f {tmpfile} 2>/dev/null")
                os.unlink(tmpfile)

        # Enable pf if not running
        _run("pfctl -e 2>/dev/null")
        logger.info(f"[TC/macOS] pf anchor '{PF_ANCHOR}' ready on {self.iface}")

    def set_limit(self, ip: str, download_kbps: Optional[int], upload_kbps: Optional[int]):
        self.remove_limit(ip)
        if download_kbps is None and upload_kbps is None:
            return

        pid = _ip_to_id(ip)
        dl_pipe = pid * 2
        ul_pipe = pid * 2 + 1

        dl = download_kbps or 0
        ul = upload_kbps or 0

        # Configure dummynet pipes
        if dl:
            _run(f"dnctl pipe {dl_pipe} config bw {dl}kbit/s")
        if ul:
            _run(f"dnctl pipe {ul_pipe} config bw {ul}kbit/s")

        self._limits[ip] = {
            "download_kbps": download_kbps,
            "upload_kbps": upload_kbps,
            "dl_pipe": dl_pipe,
            "ul_pipe": ul_pipe,
        }

        # Build pf anchor rules for this device
        self._reload_anchor_rules()
        logger.info(f"[TC/macOS] Limit set for {ip}: DL={download_kbps}kbps UL={upload_kbps}kbps")

    def remove_limit(self, ip: str):
        if ip not in self._limits:
            return
        info = self._limits.pop(ip)
        _run(f"dnctl pipe {info['dl_pipe']} delete 2>/dev/null")
        _run(f"dnctl pipe {info['ul_pipe']} delete 2>/dev/null")
        self._reload_anchor_rules()
        logger.info(f"[TC/macOS] Limit removed for {ip}")

    def _reload_anchor_rules(self):
        """Rewrite the pf anchor with current limit rules."""
        lines = []
        for ip, info in self._limits.items():
            dl_pipe = info["dl_pipe"]
            ul_pipe = info["ul_pipe"]
            iface = self.iface or "en0"
            if info.get("download_kbps"):
                lines.append(
                    f'dummynet out quick on {iface} proto {{tcp,udp,icmp}} '
                    f'from any to {ip} pipe {dl_pipe}'
                )
            if info.get("upload_kbps"):
                lines.append(
                    f'dummynet in quick on {iface} proto {{tcp,udp,icmp}} '
                    f'from {ip} to any pipe {ul_pipe}'
                )

        rule_text = "\n".join(lines) + "\n" if lines else "\n"
        tmpfile = "/tmp/pf_netwatch_anchor.conf"
        with open(tmpfile, "w") as f:
            f.write(rule_text)
        _run(f"pfctl -a '{PF_ANCHOR}' -f {tmpfile} 2>/dev/null")
        os.unlink(tmpfile)

    def remove_all(self):
        for ip in list(self._limits.keys()):
            self.remove_limit(ip)
        # Flush anchor
        _run(f"pfctl -a '{PF_ANCHOR}' -F all 2>/dev/null")
        logger.info("[TC/macOS] All rules removed")

    def is_limited(self, ip: str) -> bool:
        return ip in self._limits

    def get_limits(self) -> Dict[str, dict]:
        return dict(self._limits)


# ─── Linux backend (tc HTB + iptables) ────────────────────────────────────

HTB_DEFAULT = 999
IFB_DEV = "ifb0"


class LinuxTrafficController:
    def __init__(self):
        self.iface: Optional[str] = None
        self._limits: Dict[str, dict] = {}
        self._initialized = False
        self._mark_counter = 10

    def configure(self, iface: str):
        self.iface = iface
        self._setup_root_qdisc()

    def _setup_root_qdisc(self):
        if not self.iface:
            return
        _run(f"tc qdisc del dev {self.iface} root 2>/dev/null")
        rc, _, err = _run(
            f"tc qdisc add dev {self.iface} root handle 1: htb default {HTB_DEFAULT}"
        )
        if rc != 0:
            logger.error(f"[TC/Linux] Failed to add root qdisc: {err}")
            return
        _run(
            f"tc class add dev {self.iface} parent 1: classid 1:{HTB_DEFAULT} "
            f"htb rate 1000mbit burst 1500"
        )
        _run("modprobe ifb 2>/dev/null")
        _run(f"ip link add {IFB_DEV} type ifb 2>/dev/null")
        _run(f"ip link set {IFB_DEV} up 2>/dev/null")
        _run(f"tc qdisc del dev {self.iface} ingress 2>/dev/null")
        _run(f"tc qdisc add dev {self.iface} ingress")
        _run(
            f"tc filter add dev {self.iface} parent ffff: protocol ip u32 match u32 0 0 "
            f"action mirred egress redirect dev {IFB_DEV}"
        )
        _run(f"tc qdisc del dev {IFB_DEV} root 2>/dev/null")
        _run(f"tc qdisc add dev {IFB_DEV} root handle 1: htb default {HTB_DEFAULT}")
        _run(
            f"tc class add dev {IFB_DEV} parent 1: classid 1:{HTB_DEFAULT} "
            f"htb rate 1000mbit burst 1500"
        )
        self._initialized = True
        logger.info(f"[TC/Linux] Root qdisc initialized on {self.iface}")

    def set_limit(self, ip: str, download_kbps: Optional[int], upload_kbps: Optional[int]):
        if not self._initialized:
            logger.warning("[TC/Linux] Not initialized")
            return
        self.remove_limit(ip)
        if download_kbps is None and upload_kbps is None:
            return

        class_id = str(_ip_to_id(ip))
        self._mark_counter += 1
        mark = self._mark_counter
        dl = download_kbps or 100000
        ul = upload_kbps or 100000

        _run(
            f"tc class add dev {self.iface} parent 1: classid 1:{class_id} "
            f"htb rate {dl}kbit ceil {dl}kbit burst 32k"
        )
        _run(f"iptables -t mangle -A POSTROUTING -d {ip} -j MARK --set-mark {mark}")
        _run(
            f"tc filter add dev {self.iface} parent 1: protocol ip handle {mark} fw flowid 1:{class_id}"
        )
        ifb_id = str(_ip_to_id(ip) + 500)
        ifb_mark = mark + 1000
        _run(
            f"tc class add dev {IFB_DEV} parent 1: classid 1:{ifb_id} "
            f"htb rate {ul}kbit ceil {ul}kbit burst 32k"
        )
        _run(f"iptables -t mangle -A PREROUTING -s {ip} -j MARK --set-mark {ifb_mark}")
        _run(
            f"tc filter add dev {IFB_DEV} parent 1: protocol ip handle {ifb_mark} fw flowid 1:{ifb_id}"
        )
        self._limits[ip] = {
            "download_kbps": download_kbps, "upload_kbps": upload_kbps,
            "class_id": class_id, "mark": mark,
        }
        logger.info(f"[TC/Linux] Limit set for {ip}: DL={download_kbps}kbps UL={upload_kbps}kbps")

    def remove_limit(self, ip: str):
        if ip not in self._limits:
            return
        info = self._limits.pop(ip)
        class_id = info["class_id"]
        mark = info["mark"]
        ifb_mark = mark + 1000
        ifb_id = str(int(class_id) + 500)
        _run(f"iptables -t mangle -D POSTROUTING -d {ip} -j MARK --set-mark {mark} 2>/dev/null")
        _run(f"iptables -t mangle -D PREROUTING -s {ip} -j MARK --set-mark {ifb_mark} 2>/dev/null")
        _run(f"tc filter del dev {self.iface} parent 1: protocol ip handle {mark} fw 2>/dev/null")
        _run(f"tc filter del dev {IFB_DEV} parent 1: protocol ip handle {ifb_mark} fw 2>/dev/null")
        _run(f"tc class del dev {self.iface} parent 1: classid 1:{class_id} 2>/dev/null")
        _run(f"tc class del dev {IFB_DEV} parent 1: classid 1:{ifb_id} 2>/dev/null")
        logger.info(f"[TC/Linux] Limit removed for {ip}")

    def remove_all(self):
        for ip in list(self._limits.keys()):
            self.remove_limit(ip)
        if self.iface:
            _run(f"tc qdisc del dev {self.iface} root 2>/dev/null")
            _run(f"tc qdisc del dev {self.iface} ingress 2>/dev/null")
            _run(f"tc qdisc del dev {IFB_DEV} root 2>/dev/null")
        logger.info("[TC/Linux] All rules removed")

    def is_limited(self, ip: str) -> bool:
        return ip in self._limits

    def get_limits(self) -> Dict[str, dict]:
        return dict(self._limits)


# ─── No-op fallback ────────────────────────────────────────────────────────

class NoOpTrafficController:
    """Used when no supported traffic shaping backend is available."""

    def __init__(self):
        self._limits: Dict[str, dict] = {}
        logger.warning(
            "[TC] No supported traffic shaping backend found for this OS. "
            "Bandwidth limits will not be applied."
        )

    def configure(self, iface: str):
        logger.info(f"[TC/noop] Skipping tc setup on {iface}")

    def set_limit(self, ip: str, download_kbps, upload_kbps):
        self._limits[ip] = {"download_kbps": download_kbps, "upload_kbps": upload_kbps}
        logger.warning(f"[TC/noop] Limit recorded but NOT enforced for {ip}")

    def remove_limit(self, ip: str):
        self._limits.pop(ip, None)

    def remove_all(self):
        self._limits.clear()

    def is_limited(self, ip: str) -> bool:
        return ip in self._limits

    def get_limits(self) -> Dict[str, dict]:
        return dict(self._limits)


# ─── Factory ───────────────────────────────────────────────────────────────

def TrafficController():
    """Return the appropriate traffic controller for the current OS."""
    if IS_MACOS:
        logger.info("[TC] Using macOS backend (pfctl + dnctl dummynet)")
        return MacOSTrafficController()
    if IS_LINUX:
        logger.info("[TC] Using Linux backend (tc HTB + iptables)")
        return LinuxTrafficController()
    return NoOpTrafficController()
