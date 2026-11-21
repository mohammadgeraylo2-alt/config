# -*- coding: utf-8 -*-
import asyncio, base64, hashlib, hmac, json, logging, os, re, secrets, shutil, socket, sqlite3, ssl, subprocess, threading, time, uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import quote

import psutil
import uvicorn
import websockets
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("panel")

VERSION = "1.0.0"
GB = 1024 ** 3
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = BASE_DIR / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.getenv("PORT", "8080"))
TZ_OFF = int(os.getenv("TZ_OFFSET_MIN", "210")) * 60
XRAY_BIN = os.getenv("XRAY_BIN") or ("/opt/xray/xray" if Path("/opt/xray/xray").exists() else (shutil.which("xray") or "/opt/xray/xray"))
XRAY_PORT, API_PORT = 10000, 10085
PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"]

# ============================ database ============================
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins(
 id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL DEFAULT 'owner', created_at INTEGER NOT NULL, last_login INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, ident TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active', quota INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL DEFAULT 0,
 token TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS servers(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, host TEXT NOT NULL DEFAULT '', port INTEGER NOT NULL DEFAULT 443,
 ws_path TEXT NOT NULL DEFAULT '/', tls INTEGER NOT NULL DEFAULT 1, sni TEXT NOT NULL DEFAULT '', is_local INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'unknown', health TEXT NOT NULL DEFAULT 'unknown', latency INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '', last_check INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS services(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE RESTRICT, name TEXT NOT NULL, uuid TEXT NOT NULL UNIQUE,
 token TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active', state TEXT NOT NULL DEFAULT 'active',
 quota INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL DEFAULT 0, used_up INTEGER NOT NULL DEFAULT 0,
 used_down INTEGER NOT NULL DEFAULT 0, warn_exp INTEGER NOT NULL DEFAULT 0, warn_use INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_services_user ON services(user_id);
CREATE INDEX IF NOT EXISTS ix_services_server ON services(server_id);
CREATE TABLE IF NOT EXISTS usage_records(
 id INTEGER PRIMARY KEY AUTOINCREMENT, service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
 ts INTEGER NOT NULL, up INTEGER NOT NULL DEFAULT 0, down INTEGER NOT NULL DEFAULT 0, UNIQUE(service_id, ts));
CREATE INDEX IF NOT EXISTS ix_usage_ts ON usage_records(ts);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, level TEXT NOT NULL DEFAULT 'info', title TEXT NOT NULL,
 body TEXT NOT NULL DEFAULT '', is_read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS activity_logs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, admin TEXT NOT NULL, action TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '');
"""
_db = sqlite3.connect(str(DATA_DIR / "panel.db"), check_same_thread=False, isolation_level=None, timeout=30)
_db.row_factory = sqlite3.Row
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("PRAGMA foreign_keys=ON")
_db.execute("PRAGMA synchronous=NORMAL")
_db.executescript(SCHEMA)
_lock = threading.RLock()


def q(sql, a=()):
    with _lock:
        return [dict(r) for r in _db.execute(sql, a).fetchall()]


def q1(sql, a=()):
    r = q(sql, a)
    return r[0] if r else None


def ex(sql, a=()):
    with _lock:
        return _db.execute(sql, a).lastrowid


@contextmanager
def tx():
    with _lock:
        _db.execute("BEGIN")
        try:
            yield
            _db.execute("COMMIT")
        except Exception:
            _db.execute("ROLLBACK")
            raise


def now():
    return int(time.time())


def get_set(k, d=None):
    r = q1("SELECT v FROM settings WHERE k=?", (k,))
    return r["v"] if r else d


def put_set(k, v):
    ex("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def persisted(k, make):
    v = get_set(k)
    if v is None:
        v = make()
        put_set(k, v)
    return v


def clean_path(v, fallback):
    v = re.sub(r"[^A-Za-z0-9_\-]", "", v or "")
    return v or fallback


SECRET = os.getenv("SECRET_KEY") or persisted("secret_key", lambda: secrets.token_hex(32))
ADMIN_PATH = "/" + clean_path(os.getenv("ADMIN_PATH", ""), persisted("admin_path", lambda: "panel-" + secrets.token_hex(5)))
WS_PATH = "/" + clean_path(os.getenv("WS_PATH", ""), persisted("ws_path", lambda: "ws-" + secrets.token_hex(6)))
if WS_PATH == ADMIN_PATH or WS_PATH == "/sub":
    WS_PATH = "/ws-" + secrets.token_hex(6)

# ============================ helpers ============================
def hash_pw(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (salt.hex(), h.hex())


def check_pw(pw, stored):
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


DUMMY_HASH = hash_pw("dummy-password")


def sign(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return raw + "." + hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def unsign(tok):
    try:
        raw, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest(), sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return data if data.get("e", 0) > time.time() else None
    except Exception:
        return None


def ip_of(request):
    ip = request.headers.get("x-real-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host
    return (ip or "?")[:45]


def is_https(request):
    return (request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme) == "https"


def base_url(request):
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return "%s://%s" % (proto, host)


def bad(msg, code=400):
    raise HTTPException(code, msg)


CTRL = re.compile(r"[\x00-\x1f\x7f]")


def sstr(b, k, label, mn=0, mx=100):
    v = b.get(k)
    if v is None:
        v = ""
    if not isinstance(v, (str, int, float)):
        bad("%s نامعتبر است" % label)
    v = CTRL.sub("", str(v)).strip()
    if len(v) < mn:
        bad("%s را وارد کنید" % label)
    if len(v) > mx:
        bad("%s حداکثر %d کاراکتر می‌تواند باشد" % (label, mx))
    return v


def snum(b, k, label, default=0.0, mn=0.0, mx=1e7):
    v = b.get(k, default)
    if v is None or v == "":
        v = default
    try:
        v = float(v)
    except Exception:
        bad("%s باید عدد باشد" % label)
    if v != v or v < mn or v > mx:
        bad("%s باید بین %g تا %g باشد" % (label, mn, mx))
    return v


def like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def exp_from(days):
    return now() + int(days) * 86400 if days > 0 else 0


def day_start(ts):
    return (ts + TZ_OFF) // 86400 * 86400 - TZ_OFF


def page_of(items, page, size=15):
    total = len(items)
    pages = max(1, (total + size - 1) // size)
    page = min(max(1, page), pages)
    return {"items": items[(page - 1) * size: page * size], "total": total, "pages": pages, "page": page}


def notify(level, title, body=""):
    ex("INSERT INTO notifications(ts,level,title,body) VALUES(?,?,?,?)", (now(), level, title, body))


def act(adm, action, detail="", ip=""):
    name = adm["username"] if isinstance(adm, dict) else str(adm)
    ex("INSERT INTO activity_logs(ts,admin,action,detail,ip) VALUES(?,?,?,?,?)", (now(), name, action, detail[:300], ip))


_fails = {}


def rl_check(key):
    r = _fails.get(key)
    if r and r[2] > time.time():
        bad("تلاش‌های ناموفق زیاد بود. %d دقیقه دیگر دوباره تلاش کنید" % (int((r[2] - time.time()) // 60) + 1), 429)


def rl_fail(key):
    t = time.time()
    if len(_fails) > 5000:
        _fails.clear()
    r = _fails.get(key)
    if not r or t - r[1] > 900:
        r = [0, t, 0]
    r[0] += 1
    if r[0] >= 5:
        r[2] = t + 900
    _fails[key] = r


# ============================ init data ============================
def init_admin():
    env_user = re.sub(r"[^A-Za-z0-9_.\-]", "", os.getenv("ADMIN_USERNAME", "admin"))[:32] or "admin"
    env_pw = os.getenv("ADMIN_PASSWORD", "")
    if not q1("SELECT id FROM admins LIMIT 1"):
        pw = env_pw or secrets.token_urlsafe(12)
        ex("INSERT INTO admins(username,password_hash,role,created_at) VALUES(?,?,?,?)", (env_user, hash_pw(pw), "owner", now()))
        put_set("admin_env_fp", hashlib.sha256(env_pw.encode()).hexdigest() if env_pw else "")
        if not env_pw:
            log.warning("ADMIN_PASSWORD تنظیم نشده بود. رمز موقت (فقط همین یک‌بار): user=%s password=%s", env_user, pw)
    elif env_pw:
        fp = hashlib.sha256(env_pw.encode()).hexdigest()
        if get_set("admin_env_fp") != fp:
            a = q1("SELECT id FROM admins WHERE role='owner' ORDER BY id LIMIT 1")
            if a:
                ex("UPDATE admins SET password_hash=? WHERE id=?", (hash_pw(env_pw), a["id"]))
            put_set("admin_env_fp", fp)


def init_local_server():
    host = os.getenv("PUBLIC_HOST") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or ""
    host = re.sub(r"^https?://", "", host).strip("/ ")
    sv = q1("SELECT * FROM servers WHERE is_local=1")
    if not sv:
        ex("INSERT INTO servers(name,host,port,ws_path,tls,is_local,created_at) VALUES(?,?,?,?,?,?,?)",
           ("سرور اصلی", host, 443, WS_PATH, 1, 1, now()))
    else:
        ex("UPDATE servers SET ws_path=? WHERE id=?", (WS_PATH, sv["id"]))
        if not sv["host"] and host:
            ex("UPDATE servers SET host=? WHERE id=?", (host, sv["id"]))


init_admin()
init_local_server()
_local_ok = False


def ensure_local_host(request):
    global _local_ok
    if _local_ok:
        return
    sv = q1("SELECT * FROM servers WHERE is_local=1")
    if not sv or sv["host"]:
        _local_ok = True
        return
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(":")[0]
    if host:
        if host in ("localhost", "127.0.0.1"):
            ex("UPDATE servers SET host=?, port=?, tls=0 WHERE id=?", (host, PORT, sv["id"]))
        else:
            ex("UPDATE servers SET host=? WHERE id=?", (host, sv["id"]))
        _local_ok = True


# ============================ links ============================
def make_link(svc, srv, label):
    host = srv["host"]
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    sni = srv["sni"] or srv["host"]
    params = [("encryption", "none"), ("security", "tls" if srv["tls"] else "none")]
    if srv["tls"]:
        params += [("sni", sni), ("fp", "chrome"), ("alpn", "http/1.1")]
    params += [("type", "ws"), ("host", sni), ("path", srv["ws_path"])]
    qs = "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in params)
    return "vless://%s@%s:%s?%s#%s" % (svc["uuid"], host, srv["port"], qs, quote(label))


# ============================ core process ============================
def build_config():
    clients = [{"id": r["uuid"], "email": "s%d@p" % r["id"], "level": 0} for r in q("SELECT id,uuid FROM services WHERE state='active'")]
    return {
        "log": {"loglevel": "warning", "access": "none"},
        "stats": {},
        "api": {"tag": "api", "services": ["StatsService"]},
        "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
        "inbounds": [
            {"tag": "api-in", "listen": "127.0.0.1", "port": API_PORT, "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}},
            {"tag": "main", "listen": "127.0.0.1", "port": XRAY_PORT, "protocol": "vless",
             "settings": {"clients": clients, "decryption": "none"},
             "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": WS_PATH}}},
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
        "routing": {"domainStrategy": "AsIs", "rules": [
            {"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"},
            {"type": "field", "ip": PRIVATE_NETS, "outboundTag": "block"}]},
    }


def core_env():
    return {**os.environ, "XRAY_LOCATION_ASSET": os.getenv("XRAY_ASSET_DIR", str(Path(XRAY_BIN).parent))}


class Core:
    def __init__(self):
        self.proc = None
        self.logf = None
        self.err = ""
        self.hash = ""
        self.lock = asyncio.Lock()
        self.last_try = 0

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    async def _stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(p.wait), 5)
            except Exception:
                p.kill()
                await asyncio.to_thread(p.wait)
        if self.logf:
            try:
                self.logf.close()
            except Exception:
                pass
            self.logf = None

    async def _start(self, cfg, h):
        self.last_try = time.time()
        if not Path(XRAY_BIN).exists():
            self.err = "هسته اتصال روی سرور پیدا نشد"
            return False
        cfg_path = DATA_DIR / "core.json"
        cfg_path.write_text(json.dumps(cfg))
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
        self.logf = open(DATA_DIR / "core.log", "w")
        self.proc = subprocess.Popen([XRAY_BIN, "run", "-c", str(cfg_path)], stdout=self.logf, stderr=subprocess.STDOUT, env=core_env())
        await asyncio.sleep(0.8)
        if self.proc.poll() is not None:
            try:
                tail = (DATA_DIR / "core.log").read_text()[-300:]
            except Exception:
                tail = ""
            self.err = "هسته اجرا نشد. " + tail.strip().replace("\n", " ")
            log.error("core failed: %s", tail)
            self.proc = None
            return False
        self.err, self.hash = "", h
        return True

    async def apply(self, force=False):
        async with self.lock:
            cfg = build_config()
            h = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
            if not force and self.alive() and h == self.hash:
                return True
            await self._stop()
            return await self._start(cfg, h)

    async def stop(self):
        async with self.lock:
            await self._stop()


core = Core()
LOOP = None
SYNC_EVENT = None


def request_sync():
    if LOOP is not None and SYNC_EVENT is not None:
        LOOP.call_soon_threadsafe(SYNC_EVENT.set)


async def core_stats():
    try:
        p = await asyncio.create_subprocess_exec(
            XRAY_BIN, "api", "statsquery", "-s", "127.0.0.1:%d" % API_PORT, "-pattern", "user>>>", "-reset=true",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=core_env())
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        data = json.loads(out.decode() or "{}")
    except Exception:
        return {}
    res = {}
    for st in data.get("stat", []) or []:
        parts = str(st.get("name", "")).split(">>>")
        if len(parts) == 4 and parts[0] == "user":
            try:
                val = int(st.get("value", 0) or 0)
            except Exception:
                continue
            e = res.setdefault(parts[1], [0, 0])
            e[0 if parts[3] == "uplink" else 1] += val
    return res


async def collect_usage():
    if not core.alive():
        return
    data = await core_stats()
    if not data:
        return
    hour = now() // 3600 * 3600

    def apply():
        with tx():
            for email, (up, down) in data.items():
                m = re.fullmatch(r"s(\d+)@p", email)
                if not m or (up == 0 and down == 0):
                    continue
                sid = int(m.group(1))
                if not q1("SELECT id FROM services WHERE id=?", (sid,)):
                    continue
                ex("UPDATE services SET used_up=used_up+?, used_down=used_down+? WHERE id=?", (up, down, sid))
                ex("INSERT INTO usage_records(service_id,ts,up,down) VALUES(?,?,?,?) "
                   "ON CONFLICT(service_id,ts) DO UPDATE SET up=up+excluded.up, down=down+excluded.down", (sid, hour, up, down))

    await asyncio.to_thread(apply)


# ============================ states ============================
def calc_state(s, u, uused, n):
    if u["status"] != "active" or s["status"] != "active":
        return "disabled"
    exps = [e for e in (s["expires_at"], u["expires_at"]) if e]
    if exps and min(exps) <= n:
        return "expired"
    if (s["quota"] and s["used_up"] + s["used_down"] >= s["quota"]) or (u["quota"] and uused >= u["quota"]):
        return "limited"
    return "active"


def user_state(u, used, n):
    if u["status"] != "active":
        return "disabled"
    if u["expires_at"] and u["expires_at"] <= n:
        return "expired"
    if u["quota"] and used >= u["quota"]:
        return "limited"
    return "active"


def eff_exp(s, u):
    exps = [e for e in (s["expires_at"], u["expires_at"]) if e]
    return min(exps) if exps else 0


_refresh_lock = threading.Lock()
STATE_MSG = {"expired": ("warn", "سرویس «%s» منقضی شد"), "limited": ("warn", "مصرف سرویس «%s» به سقف رسید"),
             "disabled": ("warn", "سرویس «%s» غیرفعال شد"), "active": ("info", "سرویس «%s» دوباره فعال شد")}


def refresh_states():
    with _refresh_lock:
        n = now()
        wd = int(get_set("warn_days", "3"))
        wp = int(get_set("warn_pct", "80"))
        users = {u["id"]: u for u in q("SELECT * FROM users")}
        uused = {r["user_id"]: r["t"] for r in q("SELECT user_id, SUM(used_up+used_down) t FROM services GROUP BY user_id")}
        changed = False
        for s in q("SELECT * FROM services"):
            u = users.get(s["user_id"])
            if not u:
                continue
            new = calc_state(s, u, uused.get(u["id"], 0), n)
            sets = {}
            if new != s["state"]:
                sets["state"] = new
                if "active" in (new, s["state"]):
                    changed = True
                lv, t = STATE_MSG[new]
                notify(lv, t % s["name"], "کاربر: " + u["name"])
            exp = eff_exp(s, u)
            near = new == "active" and exp and 0 < exp - n <= wd * 86400
            if near and not s["warn_exp"]:
                sets["warn_exp"] = 1
                notify("warn", "سرویس «%s» تا %d روز دیگر منقضی می‌شود" % (s["name"], max(1, (exp - n) // 86400)), "کاربر: " + u["name"])
            elif not near and s["warn_exp"]:
                sets["warn_exp"] = 0
            high = new == "active" and s["quota"] and (s["used_up"] + s["used_down"]) >= s["quota"] * wp / 100
            if high and not s["warn_use"]:
                sets["warn_use"] = 1
                notify("warn", "مصرف سرویس «%s» به %d٪ سقف رسید" % (s["name"], wp), "کاربر: " + u["name"])
            elif not high and s["warn_use"]:
                sets["warn_use"] = 0
            if sets:
                ex("UPDATE services SET " + ",".join("%s=?" % k for k in sets) + " WHERE id=?", (*sets.values(), s["id"]))
        return changed


def post_change():
    refresh_states()
    request_sync()


# ============================ servers health ============================
def probe(host, port, tls, sni):
    t = time.time()
    with socket.create_connection((host, port), timeout=6) as s:
        if tls:
            with ssl.create_default_context().wrap_socket(s, server_hostname=sni or host):
                pass
    return int((time.time() - t) * 1000)


def check_server(sv):
    if sv["is_local"]:
        if not core.alive():
            return "offline", 0, core.err or "هسته اتصال در حال اجرا نیست"
        try:
            t = time.time()
            socket.create_connection(("127.0.0.1", XRAY_PORT), timeout=2).close()
            return "online", int((time.time() - t) * 1000), ""
        except OSError:
            return "offline", 0, "پورت داخلی پاسخ نمی‌دهد"
    if not sv["host"]:
        return "offline", 0, "آدرس سرور تنظیم نشده است"
    try:
        return "online", probe(sv["host"], sv["port"], sv["tls"], sv["sni"]), ""
    except socket.gaierror:
        return "offline", 0, "آدرس پیدا نشد"
    except socket.timeout:
        return "offline", 0, "زمان اتصال به پایان رسید"
    except ConnectionRefusedError:
        return "offline", 0, "اتصال رد شد"
    except ssl.SSLError:
        return "offline", 0, "خطا در گواهی یا اتصال امن"
    except Exception:
        return "offline", 0, "اتصال برقرار نشد"


def run_check(sv):
    st, ms, err = check_server(sv)
    health = "down" if st != "online" else ("slow" if ms >= 1200 else "healthy")
    prev = sv["status"]
    ex("UPDATE servers SET status=?,health=?,latency=?,last_error=?,last_check=? WHERE id=?", (st, health, ms, err, now(), sv["id"]))
    if prev != st and not (prev == "unknown" and st == "online"):
        if st == "offline":
            notify("error", "اتصال سرور «%s» قطع شد" % sv["name"], err)
        elif prev == "offline":
            notify("info", "اتصال سرور «%s» دوباره برقرار شد" % sv["name"])
    return st, ms, err


def check_all_servers():
    for sv in q("SELECT * FROM servers"):
        try:
            run_check(sv)
        except Exception:
            log.exception("server check failed")


def resources():
    try:
        return {"cpu": psutil.cpu_percent(None), "mem": psutil.virtual_memory().percent, "disk": psutil.disk_usage(str(DATA_DIR)).percent}
    except Exception:
        return None


# ============================ background ============================
async def sync_worker():
    while True:
        await SYNC_EVENT.wait()
        await asyncio.sleep(1.0)
        SYNC_EVENT.clear()
        try:
            await collect_usage()
            await asyncio.to_thread(refresh_states)
            await core.apply()
        except Exception:
            log.exception("sync failed")


async def bg_loop():
    tick = 0
    while True:
        await asyncio.sleep(20)
        tick += 1
        try:
            await collect_usage()
            if await asyncio.to_thread(refresh_states):
                request_sync()
            if not core.alive() and time.time() - core.last_try > 30:
                await core.apply(force=True)
            if tick % 3 == 0:
                await asyncio.to_thread(check_all_servers)
            if tick % 360 == 0:
                def cleanup():
                    ex("DELETE FROM usage_records WHERE ts<?", (now() - 400 * 86400,))
                    ex("DELETE FROM activity_logs WHERE id NOT IN (SELECT id FROM activity_logs ORDER BY id DESC LIMIT 20000)")
                    ex("DELETE FROM notifications WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 2000)")
                await asyncio.to_thread(cleanup)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background loop error")


@asynccontextmanager
async def lifespan(app):
    global LOOP, SYNC_EVENT
    LOOP = asyncio.get_running_loop()
    SYNC_EVENT = asyncio.Event()
    psutil.cpu_percent(None)
    await asyncio.to_thread(refresh_states)
    await core.apply(force=True)
    log.info("Panel path: %s | version %s", ADMIN_PATH, VERSION)
    tasks = [asyncio.create_task(bg_loop()), asyncio.create_task(sync_worker()), asyncio.create_task(asyncio.to_thread(check_all_servers))]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await core.stop()


# ============================ app ============================
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
NOT_FOUND = ("<html><head><title>404 Not Found</title></head><body><center><h1>404 Not Found</h1></center>"
             "<hr><center>nginx</center></body></html>")


@app.exception_handler(StarletteHTTPException)
async def http_exc(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith(ADMIN_PATH + "/api"):
        return JSONResponse({"detail": exc.detail if isinstance(exc.detail, str) else "خطا"}, status_code=exc.status_code)
    if exc.status_code == 404:
        return HTMLResponse(NOT_FOUND, 404)
    return PlainTextResponse("", exc.status_code)


@app.exception_handler(RequestValidationError)
async def val_exc(request: Request, exc: RequestValidationError):
    return JSONResponse({"detail": "ورودی نامعتبر است"}, status_code=422)


@app.exception_handler(Exception)
async def any_exc(request: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse({"detail": "خطای داخلی سرور"}, status_code=500)


INDEX_HTML = (BASE_DIR / "index.html").read_text("utf-8")
PAGE_HEADERS = {
    "Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                               "font-src https://cdn.jsdelivr.net data:; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
}


@app.get(ADMIN_PATH, include_in_schema=False)
@app.get(ADMIN_PATH + "/", include_in_schema=False)
def admin_page():
    return HTMLResponse(INDEX_HTML.replace("__BASE__", ADMIN_PATH), headers=PAGE_HEADERS)


@app.get("/", include_in_schema=False)
def root():
    return HTMLResponse("<!doctype html><meta charset=utf-8><title>Welcome</title><body style='font-family:sans-serif;text-align:center;margin-top:20vh;color:#666'><h2>Welcome</h2></body>")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return PlainTextResponse("ok")


@app.get("/sub/{token}", include_in_schema=False)
def subscription(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,64}", token):
        raise HTTPException(404)
    s = q1("SELECT * FROM services WHERE token=?", (token,))
    if s:
        u = q1("SELECT * FROM users WHERE id=?", (s["user_id"],))
        svcs, title, total, exp = [s], s["name"], (s["quota"] or u["quota"]), eff_exp(s, u)
    else:
        u = q1("SELECT * FROM users WHERE token=?", (token,))
        if not u:
            raise HTTPException(404)
        svcs, title, total, exp = q("SELECT * FROM services WHERE user_id=?", (u["id"],)), u["name"], u["quota"], u["expires_at"]
    srv = {x["id"]: x for x in q("SELECT * FROM servers")}
    lines = [make_link(x, srv[x["server_id"]], x["name"]) for x in svcs if x["state"] == "active" and x["server_id"] in srv]
    lines = [l for l in lines if l]
    up, down = sum(x["used_up"] for x in svcs), sum(x["used_down"] for x in svcs)
    headers = {"Subscription-Userinfo": "upload=%d; download=%d; total=%d; expire=%d" % (up, down, total, exp),
               "profile-title": "base64:" + base64.b64encode(title.encode()).decode(), "Profile-Update-Interval": "6",
               "Cache-Control": "no-store"}
    return Response(base64.b64encode("\n".join(lines).encode()).decode(), media_type="text/plain; charset=utf-8", headers=headers)


@app.websocket(WS_PATH)
async def ws_proxy(ws: WebSocket):
    if not core.alive():
        await ws.close(code=1013)
        return
    try:
        up = await websockets.connect("ws://127.0.0.1:%d%s" % (XRAY_PORT, WS_PATH), max_size=None, ping_interval=None, compression=None, open_timeout=5)
    except Exception:
        await ws.close(code=1013)
        return
    await ws.accept()

    async def c2u():
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                d = m.get("bytes")
                if d is None:
                    d = m.get("text")
                if d is not None:
                    await up.send(d)
        except Exception:
            pass

    async def u2c():
        try:
            async for m in up:
                if isinstance(m, bytes):
                    await ws.send_bytes(m)
                else:
                    await ws.send_text(m)
        except Exception:
            pass

    t1, t2 = asyncio.create_task(c2u()), asyncio.create_task(u2c())
    await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in (t1, t2):
        t.cancel()
    try:
        await up.close()
    except Exception:
        pass
    try:
        await ws.close()
    except Exception:
        pass


# ============================ auth ============================
def current_admin(request):
    d = unsign(request.cookies.get("sid", ""))
    if not d:
        return None
    a = q1("SELECT * FROM admins WHERE id=?", (d.get("i"),))
    if not a or d.get("v") != a["password_hash"][-12:]:
        return None
    return a


def issue_cookie(response, request, a):
    tok = sign({"i": a["id"], "v": a["password_hash"][-12:], "e": int(time.time()) + 86400})
    response.set_cookie("sid", tok, max_age=86400, httponly=True, samesite="strict", secure=is_https(request), path=ADMIN_PATH)


def auth(request: Request):
    a = current_admin(request)
    if not a:
        bad("نشست شما منقضی شده است. دوباره وارد شوید", 401)
    if request.method not in ("GET", "HEAD"):
        if request.headers.get("x-requested-with") != "panel":
            bad("درخواست نامعتبر است", 403)
        if a["role"] == "viewer" and not request.url.path.endswith("/me/password"):
            bad("دسترسی شما فقط برای مشاهده است", 403)
    ensure_local_host(request)
    return a


pub = APIRouter(prefix=ADMIN_PATH + "/api")
api = APIRouter(prefix=ADMIN_PATH + "/api", dependencies=[Depends(auth)])


@pub.post("/login")
def login(request: Request, response: Response, b: dict = Body(...)):
    ip = ip_of(request)
    rl_check(ip)
    u = str(b.get("username", "")).strip()[:64]
    p = str(b.get("password", ""))[:128]
    a = q1("SELECT * FROM admins WHERE username=?", (u,))
    ok = check_pw(p, a["password_hash"] if a else DUMMY_HASH)
    if not a or not ok:
        rl_fail(ip)
        act("—", "ورود ناموفق", "", ip)
        bad("نام کاربری یا رمز عبور اشتباه است", 401)
    _fails.pop(ip, None)
    ex("UPDATE admins SET last_login=? WHERE id=?", (now(), a["id"]))
    issue_cookie(response, request, a)
    act(a, "ورود به پنل", "", ip)
    return {"id": a["id"], "username": a["username"], "role": a["role"]}


@pub.post("/logout")
def logout(response: Response):
    response.delete_cookie("sid", path=ADMIN_PATH)
    return {"ok": True}


@api.get("/me")
def me(adm=Depends(auth)):
    return {"id": adm["id"], "username": adm["username"], "role": adm["role"]}


@api.post("/me/password")
def change_password(request: Request, response: Response, b: dict = Body(...), adm=Depends(auth)):
    cur, new = str(b.get("current", ""))[:128], str(b.get("new", ""))[:128]
    if not check_pw(cur, adm["password_hash"]):
        bad("رمز فعلی اشتباه است")
    if len(new) < 8:
        bad("رمز جدید باید حداقل ۸ کاراکتر باشد")
    ex("UPDATE admins SET password_hash=? WHERE id=?", (hash_pw(new), adm["id"]))
    issue_cookie(response, request, q1("SELECT * FROM admins WHERE id=?", (adm["id"],)))
    act(adm, "تغییر رمز عبور", "", ip_of(request))
    return {"ok": True}


# ============================ data builders ============================
def get_user(uid):
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        bad("کاربر پیدا نشد", 404)
    return u


def get_service(sid):
    s = q1("SELECT * FROM services WHERE id=?", (sid,))
    if not s:
        bad("سرویس پیدا نشد", 404)
    return s


def users_data(where="", args=(), base=""):
    n = now()
    rows = q("SELECT u.*, COALESCE(SUM(s.used_up+s.used_down),0) AS used, COUNT(s.id) AS svc_count FROM users u "
             "LEFT JOIN services s ON s.user_id=u.id %s GROUP BY u.id ORDER BY u.id DESC" % where, args)
    for r in rows:
        r["state"] = user_state(r, r["used"], n)
        r["sub_url"] = "%s/sub/%s" % (base, r.pop("token"))
    return rows


def services_data(where="", args=(), base=""):
    srv = {s["id"]: s for s in q("SELECT * FROM servers")}
    rows = q("SELECT s.*, u.name AS user_name, u.expires_at AS u_exp FROM services s JOIN users u ON u.id=s.user_id %s ORDER BY s.id DESC" % where, args)
    out = []
    for s in rows:
        sv = srv.get(s["server_id"])
        out.append({
            "id": s["id"], "user_id": s["user_id"], "user_name": s["user_name"], "server_id": s["server_id"],
            "server_name": sv["name"] if sv else "—", "name": s["name"], "state": s["state"], "status": s["status"],
            "quota": s["quota"], "used": s["used_up"] + s["used_down"], "up": s["used_up"], "down": s["used_down"],
            "expires_at": s["expires_at"], "eff_expires": eff_exp(s, {"expires_at": s["u_exp"]}), "created_at": s["created_at"],
            "link": make_link(s, sv, s["name"]) if sv else "", "sub_url": "%s/sub/%s" % (base, s["token"])})
    return out


def servers_data():
    cnt = {r["server_id"]: r for r in q("SELECT server_id, COUNT(*) svc, COUNT(DISTINCT user_id) usr FROM services GROUP BY server_id")}
    res = resources()
    out = []
    for s in q("SELECT * FROM servers ORDER BY is_local DESC, id"):
        c = cnt.get(s["id"], {})
        s["services"], s["users"] = c.get("svc", 0), c.get("usr", 0)
        s["resources"] = res if s["is_local"] else None
        out.append(s)
    return out


def usage_series(rng, where="", args=()):
    n = now()
    if rng == "24h":
        step, cnt = 3600, 24
        start = n // 3600 * 3600 - (cnt - 1) * 3600
    else:
        step, cnt = 86400, (7 if rng == "7d" else 30)
        start = day_start(n) - (cnt - 1) * 86400
    rows = q("SELECT ts, SUM(up) up, SUM(down) down FROM usage_records WHERE ts>=? %s GROUP BY ts" % where, (start, *args))
    buckets = {start + i * step: [0, 0] for i in range(cnt)}
    for r in rows:
        k = r["ts"] if step == 3600 else day_start(r["ts"])
        if k in buckets:
            buckets[k][0] += r["up"]
            buckets[k][1] += r["down"]
    return [{"t": k, "up": v[0], "down": v[1]} for k, v in sorted(buckets.items())]


def growth_series(days=30):
    start = day_start(now()) - (days - 1) * 86400
    base = q1("SELECT COUNT(*) c FROM users WHERE created_at<?", (start,))["c"]
    per = {}
    for r in q("SELECT created_at FROM users WHERE created_at>=?", (start,)):
        d = day_start(r["created_at"])
        per[d] = per.get(d, 0) + 1
    out, c = [], base
    for i in range(days):
        d = start + i * 86400
        c += per.get(d, 0)
        out.append({"t": d, "n": c})
    return out


# ============================ endpoints: stats ============================
@api.get("/dashboard")
def dashboard(request: Request):
    n, wd = now(), int(get_set("warn_days", "3"))
    users = users_data()
    active = sum(1 for u in users if u["state"] == "active")
    svc = q("SELECT s.state, s.expires_at, u.expires_at AS u_exp FROM services s JOIN users u ON u.id=s.user_id")
    expiring = sum(1 for s in svc if s["state"] == "active" and eff_exp(s, {"expires_at": s["u_exp"]}) and 0 < eff_exp(s, {"expires_at": s["u_exp"]}) - n <= wd * 86400)
    total = q1("SELECT COALESCE(SUM(used_up+used_down),0) t FROM services")["t"]
    today = q1("SELECT COALESCE(SUM(up+down),0) t FROM usage_records WHERE ts>=?", (day_start(n),))["t"]
    return {
        "users": {"total": len(users), "active": active, "inactive": len(users) - active},
        "services": {"total": len(svc), "active": sum(1 for s in svc if s["state"] == "active"), "expiring": expiring,
                     "expired": sum(1 for s in svc if s["state"] == "expired"), "limited": sum(1 for s in svc if s["state"] == "limited"),
                     "disabled": sum(1 for s in svc if s["state"] == "disabled")},
        "usage": {"total": total, "today": today},
        "servers": servers_data(),
        "recent": q("SELECT ts,admin,action,detail FROM activity_logs ORDER BY id DESC LIMIT 8"),
        "unread": q1("SELECT COUNT(*) c FROM notifications WHERE is_read=0")["c"],
    }


@api.get("/stats/usage")
def stats_usage(range: str = "7d"):
    return {"items": usage_series(range if range in ("24h", "7d", "30d") else "7d")}


@api.get("/stats/growth")
def stats_growth():
    return {"items": growth_series(30)}


@api.get("/reports/top")
def reports_top():
    top = q("SELECT u.id,u.name, COALESCE(SUM(s.used_up+s.used_down),0) used, COUNT(s.id) svc FROM users u "
            "LEFT JOIN services s ON s.user_id=u.id GROUP BY u.id ORDER BY used DESC LIMIT 10")
    states = {r["state"]: r["c"] for r in q("SELECT state, COUNT(*) c FROM services GROUP BY state")}
    return {"items": top, "states": states}


@api.get("/options")
def options():
    return {"users": q("SELECT id,name,ident FROM users ORDER BY name LIMIT 3000"),
            "servers": q("SELECT id,name FROM servers ORDER BY is_local DESC, id")}


# ============================ endpoints: users ============================
@api.get("/users")
def users_list(request: Request, q_: str = "", state: str = "", page: int = 1, export: int = 0):
    qs = request.query_params.get("q", "").strip()[:60]
    where, args = "", ()
    if qs:
        where, args = "WHERE (u.name LIKE ? ESCAPE '\\' OR u.ident LIKE ? ESCAPE '\\')", (like(qs), like(qs))
    items = users_data(where, args, base_url(request))
    if state:
        items = [i for i in items if i["state"] == state]
    return page_of(items, page, 100000 if export else 15)


def user_fields(b, cur=None):
    name = sstr(b, "name", "نام کاربر", 1, 60)
    ident = sstr(b, "ident", "شناسه", 0, 32)
    if not ident:
        ident = cur["ident"] if cur else "u" + secrets.token_hex(3)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", ident):
        bad("شناسه فقط می‌تواند شامل حروف انگلیسی، عدد و - _ . باشد (۳ تا ۳۲ کاراکتر)")
    note = sstr(b, "note", "توضیحات", 0, 300)
    quota = int(round(snum(b, "quota_gb", "سهمیه", 0, 0, 100000) * GB))
    return name, ident, note, quota


@api.post("/users")
def user_create(request: Request, b: dict = Body(...), adm=Depends(auth)):
    name, ident, note, quota = user_fields(b)
    days = int(snum(b, "days", "مدت اعتبار", 0, 0, 3650))
    try:
        uid = ex("INSERT INTO users(name,ident,note,quota,expires_at,token,created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, ident, note, quota, exp_from(days), secrets.token_urlsafe(18), now()))
    except sqlite3.IntegrityError:
        bad("این شناسه قبلاً استفاده شده است", 409)
    act(adm, "ایجاد کاربر", name, ip_of(request))
    notify("info", "کاربر جدید ایجاد شد", name)
    return {"id": uid}


@api.get("/users/{uid}")
def user_detail(uid: int, request: Request):
    get_user(uid)
    base = base_url(request)
    return {"user": users_data("WHERE u.id=?", (uid,), base)[0], "services": services_data("WHERE s.user_id=?", (uid,), base),
            "usage": usage_series("30d", "AND service_id IN (SELECT id FROM services WHERE user_id=?)", (uid,))}


@api.put("/users/{uid}")
def user_update(uid: int, request: Request, b: dict = Body(...), adm=Depends(auth)):
    u = get_user(uid)
    name, ident, note, quota = user_fields(b, u)
    status = "disabled" if b.get("status") == "disabled" else "active"
    days = b.get("days")
    exp = u["expires_at"] if days in (None, "") else exp_from(int(snum(b, "days", "مدت اعتبار", 0, 0, 3650)))
    try:
        ex("UPDATE users SET name=?,ident=?,note=?,quota=?,status=?,expires_at=? WHERE id=?", (name, ident, note, quota, status, exp, uid))
    except sqlite3.IntegrityError:
        bad("این شناسه قبلاً استفاده شده است", 409)
    act(adm, "ویرایش کاربر", name, ip_of(request))
    post_change()
    return {"ok": True}


@api.delete("/users/{uid}")
def user_delete(uid: int, request: Request, adm=Depends(auth)):
    u = get_user(uid)
    ex("DELETE FROM users WHERE id=?", (uid,))
    act(adm, "حذف کاربر", u["name"], ip_of(request))
    post_change()
    return {"ok": True}


@api.post("/users/{uid}/renew")
def user_renew(uid: int, request: Request, b: dict = Body(...), adm=Depends(auth)):
    u = get_user(uid)
    days = int(snum(b, "days", "تعداد روز", 0, 1, 3650))
    ex("UPDATE users SET expires_at=? WHERE id=?", (max(now(), u["expires_at"]) + days * 86400, uid))
    if str(b.get("reset")) == "1":
        ex("UPDATE services SET used_up=0, used_down=0, warn_use=0 WHERE user_id=?", (uid,))
    act(adm, "تمدید کاربر", "%s (+%d روز)" % (u["name"], days), ip_of(request))
    post_change()
    return {"ok": True}


@api.post("/users/{uid}/quota")
def user_quota(uid: int, request: Request, b: dict = Body(...), adm=Depends(auth)):
    u = get_user(uid)
    delta = snum(b, "delta_gb", "مقدار تغییر", 0, -100000, 100000)
    if delta == 0:
        bad("مقدار تغییر نباید صفر باشد")
    if u["quota"] == 0:
        bad("سهمیه این کاربر نامحدود است. ابتدا از ویرایش، سهمیه را تعیین کنید")
    new = u["quota"] + int(delta * GB)
    if new < GB // 100:
        bad("سهمیه نمی‌تواند کمتر از ۰٫۰۱ گیگابایت شود")
    ex("UPDATE users SET quota=? WHERE id=?", (new, uid))
    act(adm, "تغییر سهمیه کاربر", "%s (%+g GB)" % (u["name"], delta), ip_of(request))
    post_change()
    return {"ok": True}


@api.post("/users/{uid}/toggle")
def user_toggle(uid: int, request: Request, adm=Depends(auth)):
    u = get_user(uid)
    new = "disabled" if u["status"] == "active" else "active"
    ex("UPDATE users SET status=? WHERE id=?", (new, uid))
    act(adm, "فعال‌سازی کاربر" if new == "active" else "غیرفعال‌سازی کاربر", u["name"], ip_of(request))
    post_change()
    return {"ok": True}


# ============================ endpoints: services ============================
@api.get("/services")
def services_list(request: Request, state: str = "", server_id: int = 0, user_id: int = 0, page: int = 1):
    qs = request.query_params.get("q", "").strip()[:60]
    w, a = [], []
    if qs:
        w.append("(s.name LIKE ? ESCAPE '\\' OR u.name LIKE ? ESCAPE '\\')")
        a += [like(qs), like(qs)]
    if user_id:
        w.append("s.user_id=?")
        a.append(user_id)
    if server_id:
        w.append("s.server_id=?")
        a.append(server_id)
    items = services_data(("WHERE " + " AND ".join(w)) if w else "", a, base_url(request))
    if state:
        items = [i for i in items if i["state"] == state]
    return page_of(items, page)


@api.post("/services")
def service_create(request: Request, b: dict = Body(...), adm=Depends(auth)):
    u = get_user(int(snum(b, "user_id", "کاربر", 0, 0, 1e12)))
    sv = q1("SELECT * FROM servers WHERE id=?", (int(snum(b, "server_id", "سرور", 0, 0, 1e12)),))
    if not sv:
        bad("سرور پیدا نشد", 404)
    name = sstr(b, "name", "نام سرویس", 0, 60) or u["name"]
    quota = int(round(snum(b, "quota_gb", "سهمیه", 0, 0, 100000) * GB))
    days = int(snum(b, "days", "مدت اعتبار", 0, 0, 3650))
    sid = ex("INSERT INTO services(user_id,server_id,name,uuid,token,quota,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
             (u["id"], sv["id"], name, str(uuid.uuid4()), secrets.token_urlsafe(18), quota, exp_from(days), now()))
    act(adm, "ایجاد سرویس", "%s برای %s" % (name, u["name"]), ip_of(request))
    post_change()
    return {"id": sid}


@api.post("/services/purge-expired")
def services_purge(request: Request, adm=Depends(auth)):
    n = q1("SELECT COUNT(*) c FROM services WHERE state='expired'")["c"]
    ex("DELETE FROM services WHERE state='expired'")
    act(adm, "حذف سرویس‌های منقضی", "%d مورد" % n, ip_of(request))
    post_change()
    return {"count": n}


@api.get("/services/{sid}")
def service_detail(sid: int, request: Request):
    get_service(sid)
    return {"service": services_data("WHERE s.id=?", (sid,), base_url(request))[0], "usage": usage_series("30d", "AND service_id=?", (sid,))}


@api.put("/services/{sid}")
def service_update(sid: int, request: Request, b: dict = Body(...), adm=Depends(auth)):
    s = get_service(sid)
    name = sstr(b, "name", "نام سرویس", 1, 60)
    quota = int(round(snum(b, "quota_gb", "سهمیه", 0, 0, 100000) * GB))
    server_id = int(snum(b, "server_id", "سرور", s["server_id"], 0, 1e12))
    if not q1("SELECT id FROM servers WHERE id=?", (server_id,)):
        bad("سرور پیدا نشد", 404)
    days = b.get("days")
    exp = s["expires_at"] if days in (None, "") else exp_from(int(snum(b, "days", "مدت اعتبار", 0, 0, 3650)))
    ex("UPDATE services SET name=?,quota=?,server_id=?,expires_at=? WHERE id=?", (name, quota, server_id, exp, sid))
    act(adm, "ویرایش سرویس", name, ip_of(request))
    post_change()
    return {"ok": True}


@api.delete("/services/{sid}")
def service_delete(sid: int, request: Request, adm=Depends(auth)):
    s = get_service(sid)
    ex("DELETE FROM services WHERE id=?", (sid,))
    act(adm, "حذف سرویس", s["name"], ip_of(request))
    post_change()
    return {"ok": True}


@api.post("/services/{sid}/renew")
def service_renew(sid: int, request: Request, b: dict = Body(...), adm=Depends(auth)):
    s = get_service(sid)
    days = int(snum(b, "days", "تعداد روز", 0, 1, 3650))
    ex("UPDATE services SET expires_at=? WHERE id=?", (max(now(), s["expires_at"]) + days * 86400, sid))
    if str(b.get("reset")) == "1":
        ex("UPDATE services SET used_up=0, used_down=0, warn_use=0 WHERE id=?", (sid,))
    act(adm, "تمدید سرویس", "%s (+%d روز)" % (s["name"], days), ip_of(request))
    post_change()
    return {"ok": True}


@api.post("/services/{sid}/quota")
def service_quota(sid: int, request: Request, b: dict = Body(...), adm=Depends(auth)):
    s = get_service(sid)
    delta = snum(b, "delta_gb", "مقدار تغییر", 0, -100000, 100000)
    if delta == 0:
        bad("مقدار تغییر نباید صفر باشد")
    if s["quota"] == 0:
        bad("سهمیه این سرویس نامحدود است. ابتدا از ویرایش، سهمیه را تعیین کنید")
    new = s["quota"] + int(delta * GB)
    if new < GB // 100:
        bad("سهمیه نمی‌تواند کمتر از ۰٫۰۱ گیگابایت شود")
    ex("UPDATE services SET quota=? WHERE id=?", (new, sid))
    act(adm, "تغییر سهمیه سرویس", "%s (%+g GB)" % (s["name"], delta), ip_of(request))
    post_change()
    return {"ok": True}


@api.post("/services/{sid}/toggle")
def service_toggle(sid: int, request: Request, adm=Depends(auth)):
    s = get_service(sid)
    new = "disabled" if s["status"] == "active" else "active"
    ex("UPDATE services SET status=? WHERE id=?", (new, sid))
    act(adm, "فعال‌سازی سرویس" if new == "active" else "غیرفعال‌سازی سرویس", s["name"], ip_of(request))
    post_change()
    return {"ok": True}


@api.post("/services/{sid}/reset")
def service_reset(sid: int, request: Request, adm=Depends(auth)):
    s = get_service(sid)
    ex("UPDATE services SET used_up=0, used_down=0, warn_use=0 WHERE id=?", (sid,))
    act(adm, "صفر کردن مصرف سرویس", s["name"], ip_of(request))
    post_change()
    return {"ok": True}


@api.post("/services/{sid}/regen")
def service_regen(sid: int, request: Request, adm=Depends(auth)):
    s = get_service(sid)
    ex("UPDATE services SET uuid=?, token=? WHERE id=?", (str(uuid.uuid4()), secrets.token_urlsafe(18), sid))
    act(adm, "ساخت لینک جدید", s["name"], ip_of(request))
    post_change()
    return {"ok": True}


# ============================ endpoints: servers ============================
def server_fields(b, local=False):
    name = sstr(b, "name", "نام سرور", 1, 60)
    host = sstr(b, "host", "آدرس", 1, 253)
    if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
        bad("آدرس فقط می‌تواند شامل حروف انگلیسی، عدد، نقطه و - باشد")
    port = int(snum(b, "port", "پورت", 443, 1, 65535))
    tls = 0 if str(b.get("tls", "1")) in ("0", "false", "False") else 1
    sni = sstr(b, "sni", "SNI", 0, 253)
    if sni and not re.fullmatch(r"[A-Za-z0-9.\-]+", sni):
        bad("SNI نامعتبر است")
    path = WS_PATH if local else (sstr(b, "ws_path", "مسیر اتصال", 0, 120) or WS_PATH)
    if not path.startswith("/"):
        path = "/" + path
    if not re.fullmatch(r"/[A-Za-z0-9_\-./]*", path):
        bad("مسیر اتصال نامعتبر است")
    return name, host, port, tls, sni, path


@api.get("/servers")
def servers_list():
    return {"items": servers_data()}


@api.post("/servers")
def server_create(request: Request, b: dict = Body(...), adm=Depends(auth)):
    name, host, port, tls, sni, path = server_fields(b)
    sid = ex("INSERT INTO servers(name,host,port,ws_path,tls,sni,created_at) VALUES(?,?,?,?,?,?,?)", (name, host, port, path, tls, sni, now()))
    run_check(q1("SELECT * FROM servers WHERE id=?", (sid,)))
    act(adm, "افزودن سرور", name, ip_of(request))
    notify("info", "سرور جدید اضافه شد", name)
    return {"id": sid}


@api.put("/servers/{sid}")
def server_update(sid: int, request: Request, b: dict = Body(...), adm=Depends(auth)):
    s = q1("SELECT * FROM servers WHERE id=?", (sid,))
    if not s:
        bad("سرور پیدا نشد", 404)
    name, host, port, tls, sni, path = server_fields(b, bool(s["is_local"]))
    ex("UPDATE servers SET name=?,host=?,port=?,tls=?,sni=?,ws_path=? WHERE id=?", (name, host, port, tls, sni, path, sid))
    run_check(q1("SELECT * FROM servers WHERE id=?", (sid,)))
    act(adm, "ویرایش سرور", name, ip_of(request))
    notify("info", "تنظیمات سرور تغییر کرد", name)
    return {"ok": True}


@api.delete("/servers/{sid}")
def server_delete(sid: int, request: Request, adm=Depends(auth)):
    s = q1("SELECT * FROM servers WHERE id=?", (sid,))
    if not s:
        bad("سرور پیدا نشد", 404)
    if s["is_local"]:
        bad("سرور اصلی قابل حذف نیست")
    if q1("SELECT COUNT(*) c FROM services WHERE server_id=?", (sid,))["c"]:
        bad("این سرور دارای سرویس است. ابتدا سرویس‌ها را به سرور دیگری منتقل یا حذف کنید", 409)
    ex("DELETE FROM servers WHERE id=?", (sid,))
    act(adm, "حذف سرور", s["name"], ip_of(request))
    notify("info", "سرور حذف شد", s["name"])
    return {"ok": True}


@api.post("/servers/{sid}/test")
def server_test(sid: int):
    s = q1("SELECT * FROM servers WHERE id=?", (sid,))
    if not s:
        bad("سرور پیدا نشد", 404)
    st, ms, err = run_check(s)
    return {"status": st, "latency": ms, "error": err}


# ============================ endpoints: misc ============================
@api.get("/notifications")
def notifications_list():
    return {"items": q("SELECT * FROM notifications ORDER BY id DESC LIMIT 40"),
            "unread": q1("SELECT COUNT(*) c FROM notifications WHERE is_read=0")["c"]}


@api.post("/notifications/read")
def notifications_read():
    ex("UPDATE notifications SET is_read=1")
    return {"ok": True}


@api.delete("/notifications")
def notifications_clear():
    ex("DELETE FROM notifications")
    return {"ok": True}


@api.get("/activity")
def activity_list(request: Request, page: int = 1):
    qs = request.query_params.get("q", "").strip()[:60]
    if qs:
        rows = q("SELECT * FROM activity_logs WHERE (admin LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\') ORDER BY id DESC LIMIT 2000",
                 (like(qs), like(qs), like(qs)))
    else:
        rows = q("SELECT * FROM activity_logs ORDER BY id DESC LIMIT 2000")
    return page_of(rows, page, 20)


@api.get("/settings")
def settings_get():
    return {"warn_days": int(get_set("warn_days", "3")), "warn_pct": int(get_set("warn_pct", "80")), "version": VERSION,
            "core_running": core.alive(), "core_error": core.err}


@api.put("/settings")
def settings_put(request: Request, b: dict = Body(...), adm=Depends(auth)):
    put_set("warn_days", int(snum(b, "warn_days", "روزهای هشدار", 3, 1, 60)))
    put_set("warn_pct", int(snum(b, "warn_pct", "درصد هشدار مصرف", 80, 50, 99)))
    act(adm, "تغییر تنظیمات هشدار", "", ip_of(request))
    return {"ok": True}


@api.post("/system/apply")
def system_apply(request: Request, adm=Depends(auth)):
    ok = asyncio.run_coroutine_threadsafe(core.apply(force=True), LOOP).result(timeout=30)
    act(adm, "اعمال مجدد تنظیمات سرویس‌دهی", "", ip_of(request))
    return {"ok": bool(ok), "error": core.err}


@api.get("/admins")
def admins_list(adm=Depends(auth)):
    if adm["role"] != "owner":
        bad("دسترسی ندارید", 403)
    return {"items": q("SELECT id,username,role,created_at,last_login FROM admins ORDER BY id")}


@api.post("/admins")
def admin_create(request: Request, b: dict = Body(...), adm=Depends(auth)):
    if adm["role"] != "owner":
        bad("دسترسی ندارید", 403)
    username = sstr(b, "username", "نام کاربری", 3, 32)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", username):
        bad("نام کاربری فقط می‌تواند شامل حروف انگلیسی، عدد و - _ . باشد")
    pw = str(b.get("password", ""))[:128]
    if len(pw) < 8:
        bad("رمز عبور باید حداقل ۸ کاراکتر باشد")
    role = "viewer" if b.get("role") == "viewer" else "owner"
    try:
        ex("INSERT INTO admins(username,password_hash,role,created_at) VALUES(?,?,?,?)", (username, hash_pw(pw), role, now()))
    except sqlite3.IntegrityError:
        bad("این نام کاربری قبلاً ثبت شده است", 409)
    act(adm, "افزودن مدیر", username, ip_of(request))
    notify("info", "مدیر جدید اضافه شد", username)
    return {"ok": True}


@api.delete("/admins/{aid}")
def admin_delete(aid: int, request: Request, adm=Depends(auth)):
    if adm["role"] != "owner":
        bad("دسترسی ندارید", 403)
    a = q1("SELECT * FROM admins WHERE id=?", (aid,))
    if not a:
        bad("مدیر پیدا نشد", 404)
    if a["id"] == adm["id"]:
        bad("حذف حساب خودتان ممکن نیست")
    if a["role"] == "owner" and q1("SELECT COUNT(*) c FROM admins WHERE role='owner'")["c"] <= 1:
        bad("حداقل یک مدیر اصلی باید وجود داشته باشد")
    ex("DELETE FROM admins WHERE id=?", (aid,))
    act(adm, "حذف مدیر", a["username"], ip_of(request))
    notify("info", "مدیر حذف شد", a["username"])
    return {"ok": True}


app.include_router(pub)
app.include_router(api)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False, log_level="warning", proxy_headers=True, forwarded_allow_ips="*",
                ws_per_message_deflate=False, ws_ping_interval=30, ws_ping_timeout=60, timeout_keep_alive=65)
