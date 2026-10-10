# Healthcheck integration spec

Use this spec when adding a service to Status Server. It defines what to request,
what the service must return, and how to publish that response through Docker so
the dashboard can show application and component health.

The implemented contract is `application-health/v1`. The parser in
[`health_contract.py`](health_contract.py) is the source of truth. See
[`HEALTH.md`](HEALTH.md) for availability calculations and historical data.

## 1. Expose a local application health endpoint

For a new integration, use an endpoint such as:

```http
GET /health/application HTTP/1.1
Host: 127.0.0.1:3000
Accept: application/json
```

The path and port are examples: choose the ones your application actually uses.
The container's healthcheck requests this endpoint inside its own container.
Status Server reads the resulting Docker healthcheck output through its proxy;
it does not request this application endpoint directly. An application can also
produce the same JSON directly from a healthcheck command without an HTTP endpoint.

Return `Content-Type: application/json` and the following body when all measured
components are operational:

```json
{
  "contract": "application-health/v1",
  "status": "operational",
  "coreAvailable": true,
  "timestamp": "2026-10-10T12:00:00.000Z",
  "validForMs": 90000,
  "components": {
    "application": {
      "status": "operational",
      "critical": true,
      "latencyMs": 12
    }
  }
}
```

Replace the example timestamp with the actual time of the health measurement.
Refreshing a cached response must preserve its original measurement timestamp.
Use HTTP `200` for a successful health report, including a report describing
degradation. If you use HTTP `503` for an unavailable application, still return
the contract JSON; the healthcheck command must preserve that body.

The endpoint must report measured application health. A responding HTTP server
alone does not establish that its database, workers, or other critical components
are working. Reuse recent internal measurements when appropriate.

## 2. Return the required fields

| Field | Requirement |
| --- | --- |
| `contract` | Exactly `"application-health/v1"`. |
| `status` | `"operational"`, `"degraded"`, `"down"`, or `"unknown"`. |
| `coreAvailable` | JSON `true`, `false`, or `null` if core availability has not been established. |
| `timestamp` | ISO 8601 measurement timestamp with a timezone, such as a UTC value ending in `Z`. Maximum 40 characters. |
| `validForMs` | Finite number from `1000` to `300000` inclusive. `90000` means the measurement expires after 90 seconds. |
| `components` | Object containing 1 to 16 components. Names must match `[a-z][A-Za-z0-9]{0,31}`, for example `application`, `database`, or `gameApi`. |
| `components.<name>.status` | Required; one of the same four statuses. |

Each component can also include:

| Field | Requirement |
| --- | --- |
| `critical` | Boolean; defaults to `false`. Mark components required for core functionality as `true`. |
| `latencyMs` | Finite number from `0` to `86400000`, or `null`. The `application` component's latency supplies the dashboard's Response column. |
| `reason` | One of the allowed reasons below, an HTTP error such as `"HTTP 503"` matching `HTTP [45][0-9]{2}`, or `null`. Other strings are replaced with `null`. |
| `lastAttempt`, `lastSuccess`, `lastFailure`, `lastMeasurement`, `lastStatusChange` | Timezone-qualified timestamps, or `null`. Attempt is probe start; measurement is completion; success/failure retain their most recent respective result; status change is the most recent transition. |
| `measurementType` | Optional `"active"` or `"passive"`. Passive observations cannot be critical and do not determine availability. Omission preserves legacy active semantics. |
| `intervalMs`, `staleAfterMs` | Optional finite numbers from 1000 to 604800000. Freshness includes the interval, timeout, scheduling, and Docker/proxy delivery margin. `lastMeasurement` is required to establish freshness when `staleAfterMs` is supplied. |
| `consecutiveFailures`, `consecutiveRecoveries`, `reconnectAttempts` | Integers from `0` to `1000000`. Failure and recovery counters default to `0`; reconnect attempts are optional. |

Unknown fields are removed before forwarding. Do not include credentials, URLs,
request bodies, stack traces, or arbitrary exception messages.

Allowed reason strings are:

```text
Not measured
Monitoring result stale
Response latency elevated
Operation failed
Database contention
Database query failed
Request timed out
Rate limited
Connection failed
Recovering
Request still pending
Event loop stalled
Gateway heartbeat not measured
Discord disconnected
Discord reconnecting
Invalid API response
Interaction dispatch failed
Health endpoint unavailable
```

## 3. Keep the reported status consistent

To report **Operational**, set `status` to `"operational"`, `coreAvailable` to
`true`, and every actively measured component's status to `"operational"`.
Passive observations may be Unknown or failed without determining availability.

To report **Degraded** while core functionality still works, use
`status: "degraded"` and `coreAvailable: true`. No critical component may be
`"down"` or `"unknown"` when `coreAvailable` is `true`. For example, an optional
API outage can return:

```json
{
  "contract": "application-health/v1",
  "status": "degraded",
  "coreAvailable": true,
  "timestamp": "2026-10-10T12:00:00.000Z",
  "validForMs": 90000,
  "components": {
    "application": { "status": "operational", "critical": true, "latencyMs": 12 },
    "gameApi": { "status": "down", "critical": false, "reason": "Request timed out" }
  }
}
```

To report **Down**, set `status: "down"` and `coreAvailable: false`, and describe
the failed components. A `false` core-availability value is accepted only with
overall `"down"` status.

To report **Unknown** when health cannot be established, use `status: "unknown"`
and `coreAvailable: null`, with `"unknown"` for the unmeasured components.

Inconsistent combinations are rejected rather than accepted as healthy.

## 4. Publish the JSON through Docker HEALTHCHECK

Your Docker healthcheck command must print exactly one JSON object as its entire
output. Use compact JSON, at most **4096 UTF-8 bytes**, including any trailing
newline. Extra log lines, HTML, multiple JSON objects, and truncated output prevent
the proxy from parsing the contract. Keep both stdout and stderr free of extra logs.

For an image that includes Python 3, the following example requests the local
endpoint and preserves JSON returned with an HTTP error. Save it in the application
image as `/app/healthcheck.py` and change the URL to match your service:

```python
import json
import sys
import urllib.error
import urllib.request

try:
    request = urllib.request.Request(
        "http://127.0.0.1:3000/health/application",
        headers={"Accept": "application/json"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(4097)
    if len(raw) > 4096:
        sys.exit(1)
    report = json.loads(raw)
    output = json.dumps(report, separators=(",", ":"), ensure_ascii=True)
    if len((output + "\n").encode("utf-8")) > 4096:
        sys.exit(1)
    print(output, flush=True)
    sys.exit(0 if report.get("contract") == "application-health/v1"
             and report.get("status") in {"operational", "degraded"}
             and report.get("coreAvailable") is True else 1)
except (OSError, ValueError, TypeError, AttributeError):
    sys.exit(1)
```

This command assumes the endpoint implements the field validation above. The
proxy independently validates the complete contract before accepting it.
If using another runtime, implement the same request, output, and exit behavior.

Add the command to the application's Dockerfile:

```dockerfile
COPY healthcheck.py /app/healthcheck.py
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]
```

The suggested command exits `0` when core functionality is available, including
Degraded reports, and `1` for Down, Unknown, or request/format failures. Docker's
health status and the application's overall status are separate: a valid fresh
Degraded snapshot still appears as Degraded even if Docker calls the container
healthy. The proxy reads the latest healthcheck output regardless of its exit code.

## 5. Add the monitor and verify it

1. Rebuild and start the application container with its HEALTHCHECK configured.
2. In Status Server Admin, add a **Docker Container** monitor and select that
   container. Enable it and choose the polling interval, for example 60 seconds.
3. Check the latest healthcheck output:

   ```sh
   docker inspect --format '{{json .State.Health}}' YOUR_CONTAINER_NAME
   ```

4. After the next scheduled check and dashboard refresh, verify the service
   status, component details, and Response value. You can also request
   `GET /api/status` from Status Server and inspect the monitor's
   `application_health` field.

Measurements expire independently of dashboard refreshes and Status Server
polling. They must be no earlier than the current container's start time and no
more than five seconds in the future. Generate a new measurement regularly;
repeatedly returning an old snapshot cannot extend its validity. Choose the
healthcheck interval, monitor interval, and `validForMs` so a fresh snapshot is
available between checks. A 30-second healthcheck, 60-second monitor, and
90-second validity are example settings; allow more margin if scheduling is slow.

A stopped, paused, or missing container overrides an earlier healthy snapshot.
Without accepted detailed data, ordinary Docker state/health mapping applies.
For an application previously observed publishing the contract, absent or expired
detail cannot establish full application availability and is shown as Unknown
detail. Status Server observations older than twice the monitor interval also
become Unknown.

## Basic HTTP monitors

If you only need an HTTP/HTTPS reachability monitor, configure its target as the
full health endpoint URL. Status Server sends `GET` with `User-Agent:
StatusServer/1.0` and `Accept: */*`, without a request body. Return HTTP `200` when
healthy and `503` when unavailable. The checker accepts any `2xx` or `3xx` status
as success; redirects are not followed. Failures first show Degraded and become
Down at the configured consecutive-failure threshold.

HTTP monitor response bodies are not interpreted as `application-health/v1`.
Use the Docker integration above for component details and separate core/full
availability. Status Server's own `GET /healthz` returns `200 {"ok":true}` when
its database is reachable, or `503 {"ok":false}` when it is not; that endpoint
checks Status Server itself.
