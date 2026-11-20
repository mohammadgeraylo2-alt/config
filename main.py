"""مدیریت پروسه Xray: ساخت کانفیگ، ری‌استارت امن، و خواندن مصرف."""
import json, os, subprocess, logging

log = logging.getLogger("panel.xray")
BIN = os.environ.get("XRAY_BIN", "xray")
PRIVATE = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
           "100.64.0.0/10", "0.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10"]


class Xray:
    def __init__(self, directory, ws_path, port=10000, api_port=10085):
        os.makedirs(directory, exist_ok=True)
        self.cfg = os.path.join(directory, "config.json")
        self.ws_path, self.port, self.api = ws_path, port, api_port
        self.proc, self.current = None, None

    def build(self, clients):
        return {
            "log": {"loglevel": "warning"}, "stats": {},
            "api": {"tag": "api", "services": ["StatsService"]},
            "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
            "inbounds": [
                {"tag": "api-in", "listen": "127.0.0.1", "port": self.api, "protocol": "dokodemo-door",
                 "settings": {"address": "127.0.0.1"}},
                {"tag": "vless-ws", "listen": "127.0.0.1", "port": self.port, "protocol": "vless",
                 "settings": {"decryption": "none", "clients": [{"id": u, "email": e, "level": 0} for u, e in clients]},
                 "streamSettings": {"network": "ws", "wsSettings": {"path": self.ws_path}}}],
            "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
            # جلوگیری از دسترسی کاربران به شبکه داخلی (از جمله خود پنل)
            "routing": {"domainStrategy": "IPIfNonMatch", "rules": [
                {"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"},
                {"type": "field", "ip": PRIVATE, "outboundTag": "block"}]}}

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.running():
            self.proc.terminate()
            try: self.proc.wait(5)
            except subprocess.TimeoutExpired: self.proc.kill()
        self.proc = None

    def apply(self, clients):
        """clients: لیست (uuid, email). فقط وقتی مجموعه تغییر کرده یا پروسه افتاده ری‌استارت می‌کند."""
        key = sorted(clients)
        if key == self.current and (self.running() or not key): return
        if not key:
            self.stop(); self.current = key; return
        tmp = self.cfg + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f: json.dump(self.build(clients), f)
        t = subprocess.run([BIN, "run", "-test", "-c", tmp], capture_output=True, text=True, timeout=30)
        if t.returncode:
            log.error("xray config test failed: %s", (t.stdout + t.stderr)[-400:]); return
        os.replace(tmp, self.cfg); self.stop()
        self.proc = subprocess.Popen([BIN, "run", "-c", self.cfg]); self.current = key
        log.warning("xray (re)started with %d clients", len(key))

    def drain(self):
        """مصرف تجمیعی از آخرین بار را برمی‌گرداند و شمارنده را صفر می‌کند: {service_id: bytes}"""
        if not self.running(): return {}
        try:
            r = subprocess.run([BIN, "api", "statsquery", "-s", f"127.0.0.1:{self.api}", "-pattern", "user>>>", "-reset"],
                               capture_output=True, text=True, timeout=10)
            data = json.loads(r.stdout or "{}")
        except Exception as e:
            log.warning("stats query failed: %s", e); return {}
        out = {}
        for it in data.get("stat", []):
            p = it.get("name", "").split(">>>")
            if len(p) == 4 and p[1].startswith("s") and p[1][1:].isdigit():
                out[int(p[1][1:])] = out.get(int(p[1][1:]), 0) + int(it.get("value") or 0)
        return out
