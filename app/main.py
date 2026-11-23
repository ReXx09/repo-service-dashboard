"""Repo & Service Control Center. No third party JS, telemetry or tracking."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DATABASE_PATH", "/data/dashboard.sqlite3")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SYNC_SECS = max(60, int(os.environ.get("GITHUB_SYNC_SECONDS", "900")))
MONITOR_SECS = max(15, int(os.environ.get("MONITOR_SECONDS", "60")))
SESSION_COOKIE = "dashboard_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
PASSWORD_ITERATIONS = 600_000
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 8
state = {"github_sync": None, "github_error": None, "monitor": None, "monitor_error": None}
work_lock = asyncio.Lock()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys = ON")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connection() as con:
        con.execute("CREATE TABLE IF NOT EXISTS repos (full_name TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, url TEXT NOT NULL, branch TEXT, language TEXT, visibility TEXT, pushed_at TEXT, archived INTEGER, last_seen TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS services (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, repo_full_name TEXT, url TEXT NOT NULL, check_url TEXT NOT NULL, check_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', latency_ms INTEGER, last_checked TEXT, error TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_service_repo ON services(repo_full_name)")
        con.execute("CREATE TABLE IF NOT EXISTS service_tiles (service_id INTEGER PRIMARY KEY REFERENCES services(id) ON DELETE CASCADE, position INTEGER NOT NULL, width INTEGER NOT NULL DEFAULT 1 CHECK(width BETWEEN 1 AND 4), height INTEGER NOT NULL DEFAULT 1 CHECK(height BETWEEN 1 AND 3), color TEXT NOT NULL DEFAULT '#7254ce', shape TEXT NOT NULL DEFAULT 'rounded' CHECK(shape IN ('rounded','square','pill')))")
        con.execute("INSERT OR IGNORE INTO service_tiles(service_id,position) SELECT id,id FROM services")
        con.execute("CREATE TABLE IF NOT EXISTS admin_user (id INTEGER PRIMARY KEY CHECK(id = 1), username TEXT NOT NULL UNIQUE, password_salt BLOB NOT NULL, password_hash BLOB NOT NULL, created_at TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS auth_sessions (token_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_auth_session_expiry ON auth_sessions(expires_at)")
        con.execute("CREATE TABLE IF NOT EXISTS auth_failures (client_ip TEXT NOT NULL, attempted_at TEXT NOT NULL)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_auth_failure_ip_time ON auth_failures(client_ip,attempted_at)")


def admin_is_configured() -> bool:
    with connection() as con:
        return con.execute("SELECT 1 FROM admin_user WHERE id=1").fetchone() is not None


def request_is_authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connection() as con:
        row = con.execute(
            "SELECT expires_at FROM auth_sessions WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
    return row is not None and row["expires_at"] > utcnow()


def issue_session(con: sqlite3.Connection, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat(timespec="seconds")
    con.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (utcnow(),))
    con.execute(
        "INSERT INTO auth_sessions(token_hash,expires_at) VALUES(?,?)",
        (hashlib.sha256(token.encode("utf-8")).hexdigest(), expires_at),
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )


def request_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_login_limit(client_ip: str) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LOGIN_WINDOW_SECONDS)).isoformat(timespec="seconds")
    with connection() as con:
        con.execute("DELETE FROM auth_failures WHERE attempted_at <= ?", (cutoff,))
        attempts = con.execute(
            "SELECT COUNT(*) FROM auth_failures WHERE client_ip=? AND attempted_at>?",
            (client_ip, cutoff),
        ).fetchone()[0]
    if attempts >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(429, "Zu viele fehlgeschlagene Anmeldungen. Bitte später erneut versuchen.")


def record_login_failure(client_ip: str) -> None:
    with connection() as con:
        con.execute(
            "INSERT INTO auth_failures(client_ip,attempted_at) VALUES(?,?)",
            (client_ip, utcnow()),
        )


def require_auth(request: Request):
    if not request_is_authenticated(request):
        raise HTTPException(status_code=401, detail="Anmeldung erforderlich")


class AuthCredentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=12, max_length=1024)


class TileSettingsIn(BaseModel):
    width: int = Field(ge=1, le=4)
    height: int = Field(ge=1, le=3)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    shape: str = Field(pattern=r"^(rounded|square|pill)$")


class TileOrderIn(BaseModel):
    service_ids: list[int] = Field(max_length=1000)


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
async def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/static/{filename}")
async def static_file(filename: str):
    if filename not in {"app.js", "styles.css", "auth.css", "tiles.css"}:
        raise HTTPException(404)
    return FileResponse(Path(__file__).parent / "static" / filename)


@app.get("/api/auth/status")
async def auth_status(request: Request):
    configured = admin_is_configured()
    return {
        "setup_required": not configured,
        "authenticated": configured and request_is_authenticated(request),
    }


@app.post("/api/auth/setup")
async def setup_admin(credentials: AuthCredentials, request: Request):
    check_origin(request)
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        credentials.password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    response = JSONResponse({"ok": True})
    try:
        with connection() as con:
            con.execute(
                "INSERT INTO admin_user(id,username,password_salt,password_hash,created_at) VALUES(1,?,?,?,?)",
                (credentials.username.casefold(), salt, password_hash, utcnow()),
            )
            issue_session(con, request, response)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Die Ersteinrichtung wurde bereits abgeschlossen") from exc
    return response


@app.post("/api/auth/login")
async def login(credentials: AuthCredentials, request: Request):
    check_origin(request)
    client_ip = request_client_ip(request)
    enforce_login_limit(client_ip)
    with connection() as con:
        admin = con.execute(
            "SELECT username,password_salt,password_hash FROM admin_user WHERE id=1"
        ).fetchone()
    if admin is None:
        raise HTTPException(409, "Bitte zuerst das Admin-Konto einrichten")
    candidate_hash = hashlib.pbkdf2_hmac(
        "sha256",
        credentials.password.encode("utf-8"),
        admin["password_salt"],
        PASSWORD_ITERATIONS,
    )
    username_matches = hmac.compare_digest(
        credentials.username.casefold().encode("utf-8"),
        admin["username"].encode("utf-8"),
    )
    password_matches = hmac.compare_digest(candidate_hash, admin["password_hash"])
    if not (username_matches and password_matches):
        record_login_failure(client_ip)
        raise HTTPException(401, "Benutzername oder Passwort ist falsch")
    response = JSONResponse({"ok": True})
    with connection() as con:
        con.execute("DELETE FROM auth_failures WHERE client_ip=?", (client_ip,))
        issue_session(con, request, response)
    return response


@app.post("/api/auth/logout", status_code=204)
async def logout(request: Request):
    check_origin(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with connection() as con:
            con.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
    response = Response(status_code=204)
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/dashboard")
async def dashboard(_: None = Depends(require_auth)):
    with connection() as con:
        repos = [dict(r) for r in con.execute("SELECT * FROM repos ORDER BY lower(full_name)")]
        services = [dict(r) for r in con.execute("""SELECT s.*, COALESCE(t.position,s.id) AS tile_position,
            COALESCE(t.width,1) AS tile_width, COALESCE(t.height,1) AS tile_height,
            COALESCE(t.color,'#7254ce') AS tile_color, COALESCE(t.shape,'rounded') AS tile_shape
            FROM services s LEFT JOIN service_tiles t ON t.service_id=s.id
            ORDER BY COALESCE(t.position,s.id), lower(s.name)""")]
    return {"repos":repos,"services":services,"meta":state,"config":{"sync_seconds":SYNC_SECS,"monitor_seconds":MONITOR_SECS}}


@app.get("/api/tiles.css")
async def tile_styles(_: None = Depends(require_auth)):
    with connection() as con:
        rows = con.execute("SELECT service_id,color FROM service_tiles ORDER BY service_id").fetchall()
    rules = []
    for row in rows:
        color = row["color"] if re.fullmatch(r"#[0-9A-Fa-f]{6}", row["color"]) else "#7254ce"
        rules.append(f"#service-tile-{row['service_id']}{{--tile-color:{color};}}")
    return Response("\n".join(rules), media_type="text/css")


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


@app.put("/api/tiles/order")
async def update_tile_order(order: TileOrderIn, request: Request, _: None = Depends(require_auth)):
    check_origin(request)
    if len(order.service_ids) != len(set(order.service_ids)):
        raise HTTPException(422, "Jeder Service darf nur einmal im Grid vorkommen")
    with connection() as con:
        current_ids = {row["id"] for row in con.execute("SELECT id FROM services")}
        if set(order.service_ids) != current_ids:
            raise HTTPException(422, "Die Kachelliste entspricht nicht den vorhandenen Services")
        con.executemany(
            "UPDATE service_tiles SET position=? WHERE service_id=?",
            ((position, service_id) for position, service_id in enumerate(order.service_ids)),
        )
    return {"ok": True}


@app.put("/api/services/{service_id}/tile")
async def update_tile(service_id: int, tile: TileSettingsIn, request: Request, _: None = Depends(require_auth)):
    check_origin(request)
    with connection() as con:
        if con.execute("SELECT 1 FROM services WHERE id=?", (service_id,)).fetchone() is None:
            raise HTTPException(404, "Service nicht gefunden")
        con.execute(
            """INSERT INTO service_tiles(service_id,position,width,height,color,shape)
               VALUES(?,COALESCE((SELECT MAX(position)+1 FROM service_tiles),0),?,?,?,?)
               ON CONFLICT(service_id) DO UPDATE SET
               width=excluded.width,height=excluded.height,color=excluded.color,shape=excluded.shape""",
            (service_id, tile.width, tile.height, tile.color, tile.shape),
        )
    return {"ok": True}


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
        position = con.execute("SELECT COALESCE(MAX(position)+1,0) FROM service_tiles").fetchone()[0]
        con.execute("INSERT INTO service_tiles(service_id,position) VALUES(?,?)", (service_id, position))
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
