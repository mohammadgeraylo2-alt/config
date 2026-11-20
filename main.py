import os, json, time, uuid, base64, secrets, subprocess, threading, io, hmac, urllib.parse as U
from pathlib import Path
import bcrypt, qrcode
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, JSONResponse

D = Path(os.environ.get("DATA_DIR", "/app/data")); D.mkdir(parents=True, exist_ok=True)
DB, CORE, NGX = D / "db.json", D / "core.json", Path("/tmp/n.conf")
PORT = os.environ.get("PORT", "8080")
LOCK, PROC, SESS = threading.RLock(), {}, {}
GB = 1024 ** 3
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def load():
    if DB.exists():
        return json.loads(DB.read_text())
    pw = os.environ.get("ADMIN_PASS") or secrets.token_urlsafe(9)
    print(f"[panel] first run -> user: admin  pass: {pw}", flush=True)
    d = {"admin": "admin", "hash": bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(),
         "path": "/" + secrets.token_hex(8), "users": {}}
    save(d); return d


def save(d):
    with LOCK:
        if DB.exists():
            (D / "db.bak.json").write_bytes(DB.read_bytes())  # پشتیبان خودکار قبل از هر ذخیره
        tmp = D / "db.tmp"; tmp.write_text(json.dumps(d, ensure_ascii=False)); tmp.replace(DB)


db = load()


def alive(u):
    return u["on"] and (not u["gb"] or u["used"] < u["gb"] * GB) and (not u["exp"] or u["exp"] > time.time())


def write_core():
    ids = [{"id": i, "email": i[:8]} for i, u in db["users"].items() if alive(u)]
    core = {"log": {"loglevel": "warning"}, "stats": {}, "api": {"tag": "api", "services": ["StatsService"]},
            "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
            "inbounds": [
                {"tag": "in", "listen": "127.0.0.1", "port": 10000, "protocol": "vless",
                 "settings": {"clients": ids, "decryption": "none"},
                 "streamSettings": {"network": "ws", "wsSettings": {"path": db["path"]}}},
                {"tag": "api", "listen": "127.0.0.1", "port": 10085, "protocol": "dokodemo-door",
                 "settings": {"address": "127.0.0.1"}}],
            "outbounds": [{"protocol": "freedom"}],
            "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]}}
    CORE.write_text(json.dumps(core)); return {c["id"] for c in ids}


def run_core():
    if PROC.get("c"):
        PROC["c"].terminate(); PROC["c"].wait()
    PROC["ids"] = write_core()
    PROC["c"] = subprocess.Popen(["/usr/local/bin/core", "run", "-c", str(CORE)])


def watch():
    while True:
        time.sleep(30)
        try:
            with LOCK:
                out = subprocess.run(["/usr/local/bin/core", "api", "statsquery", "--server=127.0.0.1:10085", "-reset"],
                                     capture_output=True, text=True, timeout=10).stdout
                for s in json.loads(out or "{}").get("stat", []):
                    if not s["name"].startswith("user>>>"): continue
                    mail = s["name"].split(">>>")[1]
                    for i, u in db["users"].items():
                        if i.startswith(mail):
                            u["used"] += int(s.get("value", 0))
                save(db)
                if {i for i, u in db["users"].items() if alive(u)} != PROC.get("ids") or PROC["c"].poll() is not None:
                    run_core()
        except Exception as e:
            print("[watch]", e, flush=True)


def need(r: Request):
    t = r.cookies.get("t")
    if not t or SESS.get(t, 0) < time.time():
        raise HTTPException(401)


@app.get("/", response_class=HTMLResponse)
def home():
    return (Path(__file__).parent / "panel.html").read_text(encoding="utf-8")


@app.post("/api/in")
async def login(r: Request):
    b = await r.json()
    if b.get("u") == db["admin"] and bcrypt.checkpw(str(b.get("p", "")).encode(), db["hash"].encode()):
        t = secrets.token_urlsafe(24); SESS[t] = time.time() + 86400 * 7
        res = JSONResponse({"ok": 1}); res.set_cookie("t", t, httponly=True, samesite="strict", secure=True, max_age=604800)
        return res
    time.sleep(1); raise HTTPException(403, "نام کاربری یا رمز اشتباه است")


@app.get("/api/users")
def users(r: Request):
    need(r)
    return {"users": [{"id": i, **u, "alive": alive(u)} for i, u in db["users"].items()]}


@app.post("/api/users")
async def add(r: Request):
    need(r); b = await r.json()
    with LOCK:
        i = str(uuid.uuid4())
        db["users"][i] = {"name": str(b.get("name") or "کاربر")[:40], "gb": float(b.get("gb") or 0), "used": 0,
                          "exp": time.time() + float(b["days"]) * 86400 if b.get("days") else 0,
                          "on": True, "sub": secrets.token_urlsafe(10)}
        save(db); run_core()
    return {"id": i}


@app.post("/api/users/{i}/{act}")
def act(i: str, act: str, r: Request):
    need(r)
    with LOCK:
        u = db["users"].get(i) or (_ for _ in ()).throw(HTTPException(404))
        if act == "del": del db["users"][i]
        elif act == "toggle": u["on"] = not u["on"]
        elif act == "reset": u["used"] = 0
        else: raise HTTPException(400)
        save(db); run_core()
    return {"ok": 1}


def link(u, host):
    p = U.quote(db["path"], safe="")
    i = next(k for k, v in db["users"].items() if v is u)
    return f"vless://{i}@{host}:443?encryption=none&security=tls&type=ws&host={host}&sni={host}&path={p}#{U.quote(u['name'])}"


@app.get("/s/{tok}")
def sub(tok: str, r: Request):
    u = next((v for v in db["users"].values() if hmac.compare_digest(v["sub"], tok)), None)
    if not u or not alive(u): raise HTTPException(404)
    left = max(u["gb"] * GB - u["used"], 0) if u["gb"] else 0
    h = {"subscription-userinfo": f"upload=0; download={int(u['used'])}; total={int(u['gb'] * GB)}; expire={int(u['exp'])}",
         "profile-title": u["name"]}
    return PlainTextResponse(base64.b64encode(link(u, r.headers.get("host", "")).encode()).decode(), headers=h)


@app.get("/api/qr/{tok}")
def qr(tok: str, r: Request):
    need(r); b = io.BytesIO()
    qrcode.make(f"https://{r.headers.get('host')}/s/{tok}").save(b, "PNG")
    return Response(b.getvalue(), media_type="image/png")


@app.get("/api/backup")
def backup(r: Request):
    need(r)
    return Response(DB.read_bytes(), media_type="application/json",
                    headers={"content-disposition": f"attachment; filename=backup-{int(time.time())}.json"})


@app.post("/api/restore")
async def restore(r: Request, f: UploadFile = File(...)):
    global db
    need(r); d = json.loads(await f.read())
    if not {"admin", "hash", "path", "users"} <= d.keys(): raise HTTPException(400, "فایل پشتیبان معتبر نیست")
    with LOCK: db = d; save(db); run_core()
    return {"ok": 1}


def boot():
    NGX.write_text(f"""pid /tmp/n.pid; error_log /dev/stderr warn; events {{}}
http {{ access_log off; client_body_temp_path /tmp/nb; proxy_temp_path /tmp/np; client_max_body_size 20m;
 server {{ listen {PORT};
  location = {db['path']} {{ proxy_pass http://127.0.0.1:10000; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade;
   proxy_set_header Connection "upgrade"; proxy_set_header Host $host; proxy_read_timeout 3600s; }}
  location / {{ proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }} }} }}""")
    run_core()
    subprocess.Popen(["nginx", "-c", str(NGX), "-g", "daemon off;"])
    threading.Thread(target=watch, daemon=True).start()
    import uvicorn; uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    boot()
