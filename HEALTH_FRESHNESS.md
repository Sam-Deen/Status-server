# Idle health monitoring changes

Health monitoring now runs independently of commands, visitors, wake requests,
and an open dashboard. Regression tests simulate 24 idle hours; isolated Docker
fixtures verify the complete publisher, proxy, consumer, and history path.

## Root causes and fixes

- Monkee processing evidence depended on interactions, REST evidence depended on
  requests, and unused feature components participated in overall availability.
  Processing readiness and REST now have active probes; feature observations are
  explicitly passive. Database write failures can recover through a lightweight
  background write reservation without changing rows.
- Brainrot rechecked every resource on every Docker invocation and discarded
  measurement history between runs. Its component schedules and counters now
  persist in a locked tmpfs cache. Cached measurements keep their original times.
- WWOL measured application/wake readiness on HTTP requests and exposed an
  incomplete timestamp history. Its existing Spring scheduler now maintains all
  three components. The healthcheck and monitoring changes are entirely Java;
  transport checks open a socket without sending wake packets.
- Status Server could promote old Docker liveness into current application health,
  renew success timestamps when polling cached reports, and lose an entire group
  of checks after one probe exception. It now expires source/component evidence,
  preserves measurement timestamps, isolates failures, and displays passive mode
  and all five component timestamps. Additive database migrations retain history;
  each new historical observation stores its own freshness duration.
- Probe cache age plus Docker and consumer polling exceeded the previous
  90-second report lifetime in some healthy idle cases. Publisher reports now
  allow 180 seconds at the default cadences. Shorter local stale limits still
  apply before publication, and consumer polling never renews source validity.

## Default active intervals

| Project | Component intervals | Local stale thresholds |
| --- | --- | --- |
| Monkee | Application/Discord 1s; processing 5s; database 30s; REST/game API 5m | Application 7s effective; processing/Discord 15s; database 90s; REST/game API 15m |
| Brainrot | Application 30s; assets 60s; bundled videos 5m | 90s; 180s; 15m |
| WWOL | Status monitor 10s; application/wake transport 30s | 30s; 90s; 90s |
| Status Server | Configured monitor interval; 60s recommended for these Docker publishers | Twice the monitor interval by default, additionally bounded by source/component expiry |

Intervals, freshness limits, slow thresholds, and failure/recovery thresholds are
configurable. Limits account for probe deadlines and scheduling margin. Component
transport freshness permits at least 180 seconds for delivery through Docker and
the proxy. Docker's existing 30-second healthchecks remain in place. Keep the
fastest measurement interval comfortably below the 180-second report lifetime;
Docker must also run at least as frequently as that component's configured probe.

Components distinguish `lastAttempt`, `lastSuccess`, `lastFailure`,
`lastMeasurement`, and `lastStatusChange`. Slow successful probes update success
time and report Degraded. Confirmed failures report Down according to thresholds;
missing or expired evidence reports Unknown. Passive evidence remains visible but
does not establish application availability.

## Validation and limits

- Monkee: full existing suite passed, followed by 32 final health regressions,
  including simulated 24-hour inactivity, clock adjustment, failure/recovery,
  nonoverlapping probes, and real SQLite contention/recovery.
- Status Server: 52 tests passed, including 24 hours of independent polling,
  cached source expiry, per-component expiry, failure isolation, timestamp
  persistence, and preservation of historical freshness settings.
- Brainrot: 13 tests passed, including 24 hours of scheduled resource checks,
  slow/failing/recovered results, and preserved success history after manifest
  failures. The production image builds successfully.
- WWOL: the Java Docker build runs all 16 tests, including an actual Spring
  scheduler without HTTP requests and component idle/failure/recovery/stale tests.
- Isolated live Docker smoke testing confirms Operational, Degraded, and Down
  propagation, successful timestamp transport, healthy Docker liveness for an
  optionally degraded app, polling with the dashboard closed, and history after
  restarting the same Status Server container. No production deployment occurred.

Processing readiness cannot prove every Discord command works. Monkee feature
integrations remain passive; an unconfigured game API is explicitly unmeasured.
Brainrot checks Nginx and bundled resources, not browser playback or external
video sources. WWOL checks local socket readiness, not packet delivery or SSH
shutdown; an offline target is a valid status measurement. Application health
state resets on container restart; historical availability remains in Status
Server's existing database. Simulated 24-hour tests are not a real-time 24-hour
hardware soak, and live external API credentials were not used in validation.

See the per-project configuration documentation:
[Status Server](HEALTH.md), [Monkee](../monkee/HEALTH.md),
[Brainrot](../Build-a-Brainrot/README.md), and
[WWOL](../Wireless-wake-on-lan/README.md).
