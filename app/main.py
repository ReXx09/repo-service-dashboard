"""Repo & Service Control Center. No third party JS, telemetry or tracking."""
import asyncio
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DATABASE_PATH", "/data/dashboard.sqlite3")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
USER = os.environ.get("DASHBOARD_USER", "")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SYNC_SECS = max(60, int(os.environ.get("GITHUB_SYNC_SECONDS", "900")))
MONITOR_SECS = max(15, int(os.environ.get("MONITOR_SECONDS", "60")))
security = HTTPBasic(auto_error=False)
state = {"github_sync": None, "github_error": None, "monitor": None, "monitor_error": None}
work_lock = asyncio.Lock()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connection():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connection() as con:
        con.execute("CREATE TABLE IF NOT EXISTS repos (full_name TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, url TEXT NOT NULL, branch TEXT, language TEXT, visibility TEXT, pushed_at TEXT, archived INTEGER, last_seen TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS services (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, repo_full_name TEXT, url TEXT NOT NULL, check_url TEXT NOT NULL, check_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', latency_ms INTEGER, last_checked TEXT, error TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_service_repo ON services(repo_full_name)")


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)):
    if not USER or not PASSWORD or PASSWORD == "HIER_EIN_LANGES_ZUFAELLIGES_PASSWORT_EINTRAGEN":
        raise HTTPException(status_code=503, detail="Zuerst DASHBOARD_USER und DASHBOARD_PASSWORD sicher konfigurieren")
    if not credentials or not (hmac.compare_digest(credentials.username.encode(), USER.encode()) and hmac.compare_digest(credentials.password.encode(), PASSWORD.encode())):
        raise HTTPException(status_code=401, detail="Anmeldung erforderlich", headers={"WWW-Authenticate": 'Basic realm="Repository Dashboard"'})


def validate_url(value: str, *, check: bool = False) -> str:
    value = value.strip()
    if any(x in value for x in "\r\n\\") or len(value) > 2048:
        raise ValueError("Ungültige URL")
    parsed = urlsplit(value)
    if parsed.scheme not in (("http", "https", "tcp") if check else ("http", "https")) or not parsed.hostname:
        raise ValueError("URL muss http://, https:// oder (für TCP-Prüfungen) tcp:// verwenden")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Benutzerdaten und Fragment sind nicht erlaubt")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Ungültiger Port")
    if parsed.scheme == "tcp" and port is None:
        raise ValueError("TCP-Check benötigt einen Port")
    # DNS and arbitrary redirects are allowed here only because service editing requires admin credentials.
    return value


class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    repo_full_name: str | None = Field(default=None, max_length=255)
    url: str = Field(min_length=1, max_length=2048)
    check_url: str = Field(min_length=1, max_length=2048)
    check_type: str = Field(default="http", pattern="^(http|tcp)$")


def clean_service(service: ServiceIn):
    name = service.name.strip()
    if not name:
        raise HTTPException(422, "Name darf nicht leer sein")
    try:
        url = validate_url(service.url)
        check_url = validate_url(service.check_url, check=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    scheme = urlsplit(check_url).scheme
    if (service.check_type == "tcp") != (scheme == "tcp"):
        raise HTTPException(422, "check_type und check_url passen nicht zusammen")
    return name, (service.repo_full_name or "").strip() or None, url, check_url, service.check_type


async def sync_github():
    if not GH_TOKEN:
        state["github_error"] = "GITHUB_TOKEN fehlt – bitte in .env eintragen"
        return
    records = []
    try:
        async with httpx.AsyncClient(timeout=20, headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Repo-Control-Center"}) as client:
            for page in range(1, 101):
                response = await client.get("https://api.github.com/user/repos", params={"per_page": 100, "page": page, "affiliation": "owner,collaborator,organization_member", "sort": "updated"})
                response.raise_for_status()
                batch = response.json()
                if not isinstance(batch, list):
                    raise RuntimeError("Unerwartete GitHub-API-Antwort")
                records.extend(batch)
                if len(batch) < 100:
                    break
            else:
                raise RuntimeError("Mehr als 10.000 Repositories: Paginierungslimit erreicht")
        seen_at = utcnow()
        with connection() as con:
            for r in records:
                con.execute("""INSERT INTO repos(full_name,name,description,url,branch,language,visibility,pushed_at,archived,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(full_name) DO UPDATE SET
                   name=excluded.name, description=excluded.description, url=excluded.url, branch=excluded.branch,
                   language=excluded.language,visibility=excluded.visibility,pushed_at=excluded.pushed_at,
                   archived=excluded.archived,last_seen=excluded.last_seen""", (
                    r["full_name"],r["name"],r.get("description"),r["html_url"],r.get("default_branch"),r.get("language"),r.get("visibility", "private" if r.get("private") else "public"),r.get("pushed_at"),int(bool(r.get("archived"))),seen_at))
            # Keep services and their association even if a repo becomes inaccessible; remove stale repo cache.
            con.execute("DELETE FROM repos WHERE last_seen != ?", (seen_at,))
        state["github_sync"] = seen_at
        state["github_error"] = None
    except Exception as e:
        state["github_error"] = f"GitHub-Synchronisierung fehlgeschlagen: {type(e).__name__} ({str(e)[:150]})"


async def probe(row):
    begin = time.monotonic()
    status, error = "offline", None
    try:
        url = urlsplit(row["check_url"])
        if row["check_type"] == "tcp":
            # For local / VPN addresses, the dashboard host needs a route to the target.
            _, writer = await asyncio.wait_for(asyncio.open_connection(url.hostname, url.port), timeout=5)
            writer.close()
            await writer.wait_closed()
        else:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
                response = await client.get(row["check_url"])
                # reachable when responding, even 4xx may indicate login protection
                if response.status_code >= 500:
                    status = "degraded"
                    error = f"HTTP {response.status_code}"
        if status != "degraded":
            status = "online"
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:160]}"
    latency = round((time.monotonic()-begin)*1000)
    return row["id"], status, latency, utcnow(), error


async def monitor_services():
    try:
        with connection() as con:
            rows = [dict(r) for r in con.execute("SELECT id,check_url,check_type FROM services")]
        sem = asyncio.Semaphore(12)
        async def limited(row):
            async with sem:
                return await probe(row)
        results = await asyncio.gather(*(limited(r) for r in rows))
        with connection() as con:
            con.executemany("UPDATE services SET status=?,latency_ms=?,last_checked=?,error=? WHERE id=?", [(s,l,t,e,i) for i,s,l,t,e in results])
        state["monitor"] = utcnow()
        state["monitor_error"] = None
    except Exception as e:
        state["monitor_error"] = f"Monitoring fehlgeschlagen: {type(e).__name__}"


async def periodic(operation, interval, delay=0):
    await asyncio.sleep(delay)
    while True:
        async with work_lock:
            await operation()
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks = [asyncio.create_task(periodic(sync_github, SYNC_SECS)),asyncio.create_task(periodic(monitor_services, MONITOR_SECS, 2))]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Repository & Service Control Center", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def browser_security(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.get("/")
async def home(_: None = Depends(require_auth)):
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/static/{filename}")
async def static_file(filename: str, _: None = Depends(require_auth)):
    if filename not in {"app.js", "styles.css"}:
        raise HTTPException(404)
    return FileResponse(Path(__file__).parent / "static" / filename)


@app.get("/api/dashboard")
async def dashboard(_: None = Depends(require_auth)):
    with connection() as con:
        repos = [dict(r) for r in con.execute("SELECT * FROM repos ORDER BY lower(full_name)")]
        services = [dict(r) for r in con.execute("SELECT * FROM services ORDER BY lower(name)")]
    return {"repos":repos,"services":services,"meta":state,"config":{"sync_seconds":SYNC_SECS,"monitor_seconds":MONITOR_SECS}}


@app.post("/api/sync")
async def sync_now(request: Request, _: None = Depends(require_auth)):
    check_origin(request)
    if work_lock.locked():
        raise HTTPException(409, "Aktualisierung läuft bereits")
    async with work_lock:
        await sync_github()
    return {"ok":state["github_error"] is None,"error":state["github_error"]}


def check_origin(request: Request):
    # Explicit same-origin check protects write endpoints from cross-site requests
    origin = request.headers.get("origin")
    if origin:
        target = urlsplit(str(request.base_url))
        supplied = urlsplit(origin)
        if supplied.scheme not in ("http", "https") or supplied.netloc != target.netloc:
            raise HTTPException(403, "Ungültiger Origin-Header")
    if request.headers.get("x-dashboard-request") != "1":
        raise HTTPException(403, "Fehlender API-Header")


@app.post("/api/check")
async def check_now(request: Request, _: None = Depends(require_auth)):
    check_origin(request)
    if work_lock.locked():
        raise HTTPException(409, "Prüfung läuft bereits")
    async with work_lock:
        await monitor_services()
    return {"ok":state["monitor_error"] is None}


@app.post("/api/services", status_code=201)
async def add_service(service: ServiceIn, request: Request, _: None = Depends(require_auth)):
    check_origin(request)
    name,repo,url,check_url,check_type = clean_service(service)
    with connection() as con:
        cursor = con.execute("INSERT INTO services(name,repo_full_name,url,check_url,check_type) VALUES(?,?,?,?,?)", (name,repo,url,check_url,check_type))
        service_id = cursor.lastrowid
    return {"id":service_id}


@app.put("/api/services/{service_id}")
async def update_service(service_id: int, service: ServiceIn, request: Request, _: None = Depends(require_auth)):
    check_origin(request)
    name,repo,url,check_url,check_type = clean_service(service)
    with connection() as con:
        cursor = con.execute("UPDATE services SET name=?,repo_full_name=?,url=?,check_url=?,check_type=?,status='pending',latency_ms=NULL,last_checked=NULL,error=NULL WHERE id=?", (name,repo,url,check_url,check_type,service_id))
        if not cursor.rowcount:
            raise HTTPException(404, "Service nicht gefunden")
    return {"ok":True}


@app.delete("/api/services/{service_id}")
async def delete_service(service_id: int, request: Request, _: None = Depends(require_auth)):
    check_origin(request)
    with connection() as con:
        cursor = con.execute("DELETE FROM services WHERE id=?", (service_id,))
        if not cursor.rowcount:
            raise HTTPException(404, "Service nicht gefunden")
    return {"ok":True}
