# RepoPulse – GitHub Repository & Service Control Center

Eine selbst gehostete, deutschsprachige Dashboard-Anwendung mit **GitHub-Synchronisierung**, **HTTP/TCP-Healthchecks**, **IP-/Domain-Direktlinks**, **Repository-Zuordnung**, **SQLite-Persistenz** und **Remote-Zugriff über Tailscale**.

## Schnellstart (Docker Compose)

Voraussetzungen: Docker Engine mit `docker compose`; ein GitHub Personal Access Token; für sicheren Fernzugriff optional Tailscale auf dem Dashboard-Host.

```bash
cp .env.example .env
# .env bearbeiten: DASHBOARD_PASSWORD durch ein langes zufälliges Passwort ersetzen
# GITHUB_TOKEN ergänzen (kein Token im Git-Repository speichern!)
docker compose pull
docker compose up -d
```

Dashboard lokal am **Host**: <http://127.0.0.1:8080>. Der Browser fragt nach Benutzername/Passwort aus `.env`. Docker bindet den Port **nur an localhost**, nicht an das öffentliche Netzwerk. Auf einem anderen Computer im LAN ist localhost **nicht** die Serveradresse: nutze Tailscale Serve oder SSH-Portweiterleitung.

**Wichtig:** HTTP Basic Authentication über unverschlüsseltes HTTP ist nur für Loopback/SSH-Tunnel geeignet. Nicht öffentlich auf Port 8080 freigeben!

## Dockge / Container-Image

GitHub Actions baut und veröffentlicht bei Pushes auf `main` das Image `ghcr.io/rexx09/repo-service-dashboard:latest`. Warte nach einer Änderung auf den erfolgreichen Workflow, bevor du den Stack in Dockge aktualisierst. Die Compose-Datei zieht das Image bei jedem Start automatisch.

Für Dockge reichen `compose.yaml` und eine danebenliegende `.env`; Quellcode und `Dockerfile` müssen nicht auf dem Docker-Host liegen. Vor dem ersten Start muss der GHCR-Paketzugriff öffentlich sein, damit der Host das Image ohne Registry-Anmeldung ziehen kann. Die Paket-Sichtbarkeit lässt sich nach dem ersten erfolgreichen Build in den Paket-Einstellungen ändern.

## Remote sicher öffnen (empfohlen: Tailscale)

1. Installiere Tailscale auf **dem Docker-Host** und auf deinen Geräten; melde sie in demselben Tailnet an: <https://tailscale.com/download>.
2. Aktiviere auf dem Host `tailscale serve --bg 8080` (je nach Tailscale-Version `tailscale serve --bg --https=443 http://127.0.0.1:8080`; die lokale Hilfe `tailscale serve --help` nennt die gültigen Optionen).
3. Öffne die von Tailscale gemeldete **HTTPS-Adresse** deines Hosts von deinen angemeldeten Geräten. `tailscale serve status` zeigt die Adresse und Zuordnung. `tailscale serve reset` entfernt die Weiterleitung.
4. Schränke die Zugriffe bei Bedarf per Tailscale-ACL / Grants ein. **Nicht** `tailscale funnel` verwenden: Funnel würde einen öffentlichen Internetzugang eröffnen.

Tailscale verschlüsselt den Remote-Zugriff auf das Dashboard; die zusätzliche App-Anmeldung bleibt aktiv. Die *Zielserver* müssen separat erreichbar sein: Für private IP-Adressen benötigt der Docker-Host passende Netzwerk-Routen, ein VPN oder einen Tailscale-Subnet-Router. Ein Link auf `192.168.x.x` lässt sich **auf dem entfernten Browser** nur öffnen, wenn dessen Gerät ebenfalls einen Netzwerkpfad zum Ziel hat (z. B. über einen Tailscale-Subnet-Router). Alternativ als Öffnen-URL eine über Tailscale erreichbare Adresse oder eine eigene HTTPS-Domain verwenden.

## GitHub-Token

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Wähle die gewünschten Repositorys und mindestens **Metadata: Read-only** (und die relevanten Organisationen, sofern die Org-Regeln dies erlauben). RepoPulse verwendet `GET /user/repos` mit 100 Einträgen je Seite. Ein Token sieht nur Repositories, für die er berechtigt ist. Für spezielle Organisationen kann ein GitHub-App-Token oder die Zustimmung der Organisation erforderlich sein. Standardintervall: 15 Minuten. Nach Änderungen an `.env`: `docker compose up -d --force-recreate`.

Die Synchronisierung lädt Metadaten (nicht den Quellcode). Sie zeigt `pushed_at` als letzte Code-Aktivität; **keinen CI/CD- oder Deployment-Status**. Letztere wären separat anzubinden.

## Service anlegen

Dashboard → **+ Service hinzufügen**:

| Feld | Beispiel | Zweck |
|---|---|---|
| Name | `API Produktion` | Anzeigename |
| Repository | `mein-account/mein-repo` | Zuordnung zu GitHub |
| Öffnen über | `https://api.example.com` | Anklickbare URL |
| HTTP-Check | `https://api.example.com/health` | Antwortstatus prüfen |
| TCP-Check | `tcp://192.168.1.50:5432` | Alternativ TCP-Port prüfen |

HTTP 2xx–4xx gilt als erreichbar (z. B. Login mit HTTP 401), HTTP 5xx als gestört, Verbindungsfehler als offline. Redirects werden nicht automatisch verfolgt, aber HTTP-3xx zählt als erreichbar. Checks laufen auf dem **Server**, nicht im Browser. Standardintervall: 60 Sekunden. Laufzeit und Fehler werden angezeigt. Services können hinzugefügt, geändert und gelöscht werden.

## Sicherheit / Grenzen

- Die App ist für **Einzelbetreiber oder ein kleines vertrauenswürdiges Team** konzipiert. Keine Benutzerverwaltung, keine Rollen/Rechte pro Repository, kein SSO und keine Audit-Logs.
- Authentifizierung erfolgt über HTTP Basic. Verwende ein starkes Passwort und nur HTTPS/Tailscale für Remote-Zugriff. Für eine öffentliche Mehrbenutzerinstanz wäre zusätzlich ein professioneller Identity-/Reverse-Proxy mit Rate-Limit sinnvoll.
- Angemeldete Administratoren dürfen frei konfigurierbare Monitoring-Ziele eingeben. Dadurch kann der Server Anfragen an interne Hosts senden. Kein untrusted Benutzerzugriff auf die Admin-Oberfläche erlauben (SSRF-Risiko).
- SQLite liegt in einem persistenten Docker-Volume. Backup: `docker compose stop` und das Volume sichern; nicht nur den Container sichern.
- Ein Repository ohne zugeordneten Dienst hat keinen Online-/Offline-Status. Ein Dienst kann ohne Repo existieren.
- GitHub-Repos, die nach erfolgreicher Synchronisierung für den Token nicht mehr sichtbar sind, verschwinden aus der Repo-Liste. Verknüpfte Service-Einträge bleiben erhalten.
- Ein öffentlicher Domain-Link funktioniert meist überall; private IPs funktionieren nur mit passendem Netzwerkzugang. HTTPS-Verbindungen zu lokalen Geräten mit selbstsignierten Zertifikaten werden beim Monitoring standardmäßig **nicht** als gültig betrachtet.
- Basis-Integration enthält kein Docker-Socket-Mount. Das ist absichtlich sicherer; Docker-Container werden per HTTP/TCP-Endpunkt überwacht und nicht automatisch erforscht.

## Betrieb

```bash
docker compose logs -f dashboard
docker compose down         # stoppt den Container, Daten-Volume bleibt erhalten
docker compose pull
docker compose up -d         # startet bzw. aktualisiert das Image
```

Ohne Docker lokal entwickeln:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./dashboard.sqlite3
export DASHBOARD_USER=admin
export DASHBOARD_PASSWORD='ein-wirklich-langes-passwort'
export GITHUB_TOKEN='github_pat_...'
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## API

`GET /api/dashboard`, `POST /api/sync`, `POST /api/check`, `POST /api/services`, `PUT /api/services/{id}`, `DELETE /api/services/{id}`. Alle API-Routen sind geschützt. Schreibende Anfragen aus dem Frontend senden `X-Dashboard-Request: 1`.

## Projektstruktur

```text
app/main.py             FastAPI, SQLite, GitHub API, Monitoring
app/static/index.html   Dashboard-Oberfläche
app/static/styles.css   Responsive Dark-Mode-Design
app/static/app.js       Suchfilter, API, Formulare und Live-Refresh
compose.yaml            Docker Compose mit localhost-Portbindung
Dockerfile              Unprivilegierter Python-Container
.env.example            Konfigurationsvorlage
.github/workflows/      Automatischer Build und Veröffentlichung auf GHCR
```
