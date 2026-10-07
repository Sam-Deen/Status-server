# Status Server

Status Server is a self hosted status page for HTTP, HTTPS, TCP, and Docker containers. It runs scheduled checks, tracks incidents and 30 day uptime, and provides a public dashboard plus a single administrator interface.

## Features and monitor types

- Public, automatically updating status page with response times, recent incidents, and compact uptime history.
- Administrator controls for adding, editing, disabling, and deleting monitors.
- HTTP and HTTPS checks: GET request; 2xx and 3xx are healthy, redirects are not followed.
- TCP checks: verifies that a connection opens to `host:port`.
- Docker Container checks: reads running or stopped state and Docker HEALTHCHECK status without requiring ports, heartbeats, or application changes.
- SQLite persistence with 35 day raw check retention. Unknown Docker checks are excluded from uptime calculations.

## Screenshots

Screenshots are not included yet. The public dashboard is available at `/` and the admin interface at `/admin` after deployment.

## Architecture

The `status` application container serves the HTML/CSS/JavaScript UI and JSON API, runs the check worker, and stores data in SQLite. `status_server/` separates configuration, transports, monitor decisions, authentication, scheduling, and SQL persistence. The `docker_proxy` container is a separate filtered reader for Docker state. The status container has no Docker socket mount. Only its HTTP port is published; the proxy is on a private Compose network. Caddy or another existing reverse proxy may provide HTTPS separately.

Both images support ARM64. The application uses Python's standard library and has no Node, Redis, ORM, or external database dependency. No development tools need to be installed on the host for Compose deployment.

## Requirements and quick start

- Docker Engine and Docker Compose; an ARM64 or x86-64 Linux host.
- A writable Docker volume for SQLite.
- A unique admin password of at least 16 characters.

```sh
cp .env.example .env
# Edit .env and replace ADMIN_PASSWORD with a unique generated value.
docker compose up -d --build
docker compose ps
```

The default host URL is `http://127.0.0.1:8088/`; admin is at `/admin`. The default binding accepts connections only from the host. For local HTTP admin access, set `COOKIE_SECURE=false` in `.env`. Set it to `true` when using HTTPS. Keep `.env` private; it is ignored by Git and the Docker build context.

Useful commands:

```sh
docker compose logs -f status docker_proxy
docker compose restart status
docker compose up -d --build     # update images and containers
docker compose down             # stop; keeps the SQLite volume
```

The main container is named `status-server`; the helper is `status-docker-proxy`. `docker compose down -v` deletes persistent data.

## Configuration and admin authentication

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADMIN_USERNAME` | `admin` | Single administrator username |
| `ADMIN_PASSWORD` | required | Unique password, at least 16 characters |
| `COOKIE_SECURE` | `true` | Require HTTPS for the session cookie |
| `BIND_ADDRESS` | `127.0.0.1` | Host address of the published app port |
| `HOST_PORT` | `8088` | Published app HTTP port |
| `INCLUDE_PROXY_CONTAINER` | `false` | Explicitly include the helper in Docker discovery |

Compose sets `DB_PATH=/data/status.db`, `HOST=0.0.0.0`, `PORT=8080`, and `DOCKER_PROXY_URL=http://docker_proxy:8090/containers` inside the app container. The application validates these settings at startup. `BIND_ADDRESS` and `HOST_PORT` are Compose settings and do not enter the app container. The proxy validates its own name and inclusion setting.

Sign in at `/admin` with the values from `.env`. Sessions last 12 hours and survive restarts. Changing the admin password does not immediately revoke existing sessions; sign out or clear the `sessions` table if immediate revocation is needed. Mutating admin requests require a CSRF token.

## Add monitors

Use **Add monitor** in Admin. HTTP/HTTPS targets are full URLs. TCP targets are `host:port` or `[IPv6]:port`. The check interval is 10–3600 seconds; timeout is 1–30 seconds and below the interval. HTTP/TCP failure threshold is 1–10 consecutive failures: the first failure is Degraded, then Down at the threshold. Docker states map directly.

For **Docker Container**, choose a discovered running or stopped container from the dropdown. Use **Refresh containers** after adding a container. The container name is stored as the monitor target, so recreation under the same name continues monitoring. A reused name could refer to a different workload; review its monitor when reusing names. The admin interface states when a running container has no HEALTHCHECK: only its running state is monitored.

| Docker state | Status |
| --- | --- |
| Running + healthy, or running without HEALTHCHECK | Operational |
| Running + starting, or restarting | Degraded |
| Running + unhealthy, paused, exited, dead, created, removing, or missing | Down |
| Proxy unavailable or unrecognized state | Unknown |

Docker start or exit time is shown when Docker provides one. The status app records its own check and incident history. Unknown checks are stored but excluded from uptime percentages. Disabled monitors are omitted from the public page. Deleting a monitor deletes its check and incident history.

## Persistent data, backup, and restore

The named `status_data` volume contains `/data/status.db`. Startup preserves existing tables and rows. It adds missing Docker state columns for older databases and updates monitors targeting the previous generated application/proxy container names to the new fixed names; monitor IDs and history remain intact. Checks and completed incidents older than 35 days are removed hourly. SQLite reuses freed database pages.

For a consistent backup while the app is running:

```sh
docker compose exec -T status python -c 'import sqlite3; src=sqlite3.connect("/data/status.db"); dst=sqlite3.connect("/data/status-backup.db"); src.backup(dst); dst.close(); src.close()'
docker compose cp status:/data/status-backup.db ./status-backup.db
```

Keep the backup outside the Docker volume. To restore, stop the stack, copy the database into the volume, and start again:

```sh
docker compose down
docker run --rm -v status_status_data:/data -v "$PWD":/backup python:3.13-slim-bookworm sh -c 'rm -f /data/status.db-wal /data/status.db-shm && cp /backup/status-backup.db /data/status.db && chown 10001:10001 /data/status.db'
docker compose up -d
```

The volume prefix depends on the Compose project name; check `docker volume ls` first.

## Docker monitoring security

Only the proxy container mounts `/var/run/docker.sock`. Its HTTP server accepts `GET /containers` and `GET /healthz`; it sends only container list and inspect GET requests to Docker. It strips the raw inspect response down to name, state, health, and state time before returning it to Status Server. It rejects Docker write methods and all other routes. Its port is not published and it lives on the private `docker_api` network. The proxy excludes itself unless `INCLUDE_PROXY_CONTAINER=true` is set. If it fails, Docker monitors become Unknown while the status page remains available.

The socket gives the proxy process powerful Docker daemon access if the proxy itself is compromised; a `:ro` socket mount does not remove that risk. [Docker's socket security guide](https://docs.docker.com/engine/security/protect-access/) explains the underlying access model. Keep the proxy image updated and do not attach untrusted containers to `docker_api`. The public API never returns container IDs, environment variables, images, labels, mounts, or raw Docker inspect data. Monitor names and descriptions are public, so avoid putting secrets in them.

Only administrators can configure HTTP/TCP targets. Those targets may intentionally reach private addresses, so protect admin credentials. The app does not follow HTTP redirects or execute shell commands. Use HTTPS through your reverse proxy for remote admin access.

## Reverse proxy example

If the reverse proxy runs on the host and DNS points to it:

```caddyfile
status.example.com {
    reverse_proxy 127.0.0.1:8088
}
```

If the reverse proxy runs in another container, its `127.0.0.1` is different. Connect it to a network that reaches the app container, or bind the app port on a reachable host interface and restrict that port with a firewall. Do not connect the reverse proxy to `docker_api`.

## Development and testing

Python 3.13 or a recent Python 3 is sufficient for local HTTP/TCP development:

```sh
ADMIN_PASSWORD=local-development-example-only-12345 COOKIE_SECURE=false DB_PATH=./status.db python3 app.py
python3 -m unittest discover -s tests -v
```

The example password is for local development only. Use Compose to test Docker monitoring because local Python cannot resolve the private `docker_proxy` hostname. The tests use temporary SQLite databases, local sockets, and fake Docker responses; they do not need real homelab containers. The frontend is plain files in `static/`.

## Troubleshooting

- `docker compose ps` shows health. `GET /healthz` tests SQLite connectivity.
- If login fails over plain HTTP, set `COOKIE_SECURE=false` for that local session. Use `true` again under HTTPS.
- If Docker discovery is unavailable, check `docker compose logs docker_proxy` and that the proxy is healthy. Docker monitors become Unknown until it recovers.
- If a monitor is Down, check its target from Admin. HTTP redirects are not followed, and a container without HEALTHCHECK only reports its running state.
- If Docker reports a name conflict, check for an older Status Server deployment using the fixed container names before starting another copy.
