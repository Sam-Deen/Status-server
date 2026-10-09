# Detailed health and measured availability

The existing Docker proxy still uses only list/inspect GET requests. It now
accepts a bounded `application-health/v1` JSON snapshot from the **latest**
Docker healthcheck output and forwards only validated contract fields. Arbitrary
output, raw inspect data and unknown reason strings are never forwarded. The
status container still has no Docker socket mount; neither image needs new
network connections to monitored applications. Both existing `/healthz` checks
and their Docker HEALTHCHECK commands are preserved and independent of workloads.

## Contract

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

Statuses are `operational`, `degraded`, `down`, `unknown`. Core availability is
`true`, `false`, or `null` when not established. Components can additionally
include `lastAttempt`, `lastSuccess`, `consecutiveFailures`, and
`consecutiveRecoveries`. Use timezone-qualified timestamps. Publishing
applications should never include secrets or arbitrary errors in this contract.

The shared parser limits components to 16, component names to 32 characters,
latencies/counters to bounded finite values, and validity to 1–300 seconds.
Only fixed safe reasons or HTTP error status codes are accepted. Unsupported
versions and malformed/truncated responses fall back to ordinary Docker state.
The 4,096-byte Docker output limit requires a compact publisher snapshot.
There is no special case for Monkee or an application-specific URL in the checker.

A fresh valid snapshot determines application status while the container is
running. A stopped/paused/missing container overrides a previous healthy
snapshot. Source timestamps expire independently of Status Server polling and
must be no earlier than the container start time. Repeated polling of one
snapshot cannot extend its validity. Clock timestamps more than five seconds
in the future are rejected as stale evidence.

If detailed data is absent, ordinary Docker mapping continues unchanged. For a
previously observed publisher, missing/stale detail is shown as Unknown detail;
fallback Docker health does not manufacture full operational or core application
availability. Confirmed Docker Down still establishes unavailability. Ordinary
containers without a contract continue to record their existing state checks.

## Persistence and history

The existing `status.db` database and tables are reused. Additive migrations add
component snapshots and availability metadata to `monitors`/`checks` and an
incident ending reason. Existing monitor IDs, check rows, incidents and sessions
are preserved. There is no second database or new historical subsystem.

The public API exposes `application_health`, `monitoring_stale`, and
`measured_availability`. The existing administrator history endpoint includes
the snapshots, component latencies/reasons and source timestamps for its last
200 checks. This supplies historical component performance and transitions.
Unknown closes an observed incident at its coverage boundary with the reason
`monitoring gap`, rather than claiming recovery; disabling/configuration changes
also have explicit ending reasons. Fresh successful observations establish
recovery. Restarting Monkee or Status Server does not delete this history.

Raw checks and completed incidents retain the existing 35-day cleanup policy.
The existing `uptime`/`history` API fields remain check-count statistics for API
compatibility; the dashboard now uses the explicitly time-weighted metrics below.

## Availability and missing time

Availability is an observation-based measurement, **not a contractual SLA**.
The dashboard displays a rolling 30-day window in 90 consecutive eight-hour
blocks. Each metric reports availability, coverage, measured seconds,
available seconds and unknown seconds. Each entry in
`measured_availability.history` contains `from`/`to` timestamps, core/full
measurements and the worst observed `status` in that block. Tooltips display
the time range in the viewer's local timezone.

Block colors follow status severity, not an availability percentage: Down takes
precedence over Degraded, then Unknown, then Operational. Even one minute of
observed downtime makes its block red after recovery. Monitoring gaps make an
otherwise Operational block Unknown; they do not hide observed downtime or
degradation. Availability percentages remain time-weighted.

- **Core availability:** available measured seconds / known core measured
  seconds. A detailed Degraded snapshot can count as available when the
  publisher explicitly establishes `coreAvailable=true`.
- **Full operational availability:** fully Operational measured seconds /
  known full operational measured seconds. Degraded and Down count as failures.
- **Coverage:** known measured seconds / all seconds in the displayed window.
  Unknown time is excluded from the availability denominator and explicitly
  reported as missing coverage. A 100% availability result with low coverage
  does not establish continuous uptime.

These are independent measurements: an overall Unknown caused by optional
component evidence can still have an explicitly known core-availability value.

An observation covers time from its recording until the next check, capped at
twice its recorded monitor interval and, for detailed snapshots, the publisher's
source expiry. Coverage never reaches backward into time before an observation.
Unknown observations terminate the previous observation. Disabled/configuration
boundaries explicitly terminate coverage. Missing data before the first check,
after expiry, or during an unobserved restart/maintenance interval remains Unknown.
There is no assumption that unobserved planned downtime was successful or failed.

Older check rows have no recorded validity/interval or explicit core evidence.
Their recorded statuses still determine history block colors and they remain in
legacy check-count metrics, but are excluded from time-weighted measurements;
no retroactive duration is invented. New historical
intervals survive later monitor interval edits because each check records its
own interval. The public current status becomes Unknown if worker observations
are older than twice the monitor interval, even when the web server still works.

Availability queries stream narrow rows rather than loading component JSON or
whole histories into memory. Summaries are cached for up to 30 seconds. Latest
health is read locally in the application; no external dependency is probed on
a dashboard request.

## Deployment and tests

Rebuild the status and proxy images and Monkee; preserve `status_data` and the
existing Docker monitor targeting `monkee`. No additional environment variables,
published Monkee ports, mounts, Docker write permissions or Compose networks
are needed. At defaults, Docker's 30-second sample interval and the monitor's
60-second interval add approximately 30–90 seconds of reporting delay. Shorter
monitors still cannot obtain a newer sample than the publisher's healthcheck.

```sh
python -m unittest discover -s tests -v
```

The tests use fake inspect output, local HTTP servers and temporary SQLite files.
They cover contract filtering, backward compatibility, outages, migration,
staleness, source expiry, repeated snapshots, core/full availability, coverage
gaps and day boundaries without external network services.

An optional Docker smoke test uses Monkee's offline `tests/fixtures/healthBot.js`
with `HEALTH_FIXTURE_STATUS=operational`, `degraded`, or `down`. Run three fixture
containers named `PREFIX-operational`, `PREFIX-degraded`, and `PREFIX-down` with
Node 20, Monkee's source mounted read-only at `/app`, working directory `/app`,
command `node tests/fixtures/healthBot.js`, and Docker health command
`node /app/healthcheck.js`. Place them and fresh status/proxy images on a temporary
internal network; publish no ports. Give the status container
`ADMIN_PASSWORD=temporary-validation-password-123456`, `COOKIE_SECURE=false`, and
the temporary proxy's `/containers` URL. Copy `tests/docker_smoke_client.py` into
the status container and run `python /tmp/docker_smoke_client.py PREFIX` there.
The client verifies status propagation, degraded Docker liveness, component
outages, history and independent `/healthz`. Repeat after restarting the same
status container to verify persistence, then remove only these temporary resources.
