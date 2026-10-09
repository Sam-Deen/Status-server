"""Time-weighted availability over bounded observations, with explicit coverage."""
from datetime import datetime, timedelta
from health_contract import instant


def measured_availability(rows, start: datetime, end: datetime) -> dict:
    totals = {"core": [0.0, 0.0], "full": [0.0, 0.0]}
    block_duration = timedelta(hours=8)
    severity = {"operational": 0, "unknown": 1, "degraded": 2, "down": 3}
    blocks = []
    cursor = start
    while cursor < end:
        blocks.append({"from": cursor, "to": min(cursor + block_duration, end),
                       "core": [0.0, 0.0], "full": [0.0, 0.0], "status": "operational"})
        cursor += block_duration

    def record_status(block, row):
        status = row["status"]
        if status == "operational" and row["interval_seconds"] and row["full_available"] is None:
            status = "unknown"
        if severity[status] > severity[block["status"]]:
            block["status"] = status

    def accumulate(row, next_at):
        if row is None or not row["interval_seconds"]:
            # Legacy check counts cannot establish how long a measurement was valid.
            return
        at = instant(row["checked_at"])
        until = min(next_at, at + timedelta(seconds=2 * row["interval_seconds"]), end)
        if row["valid_until"]:
            until = min(until, instant(row["valid_until"]))
        cursor = max(at, start)
        while cursor < until:
            block = blocks[(cursor - start) // block_duration]
            segment_end = min(until, block["to"])
            seconds = (segment_end - cursor).total_seconds()
            record_status(block, row)
            for metric, column in (("core", "core_available"), ("full", "full_available")):
                value = row[column]
                if value is not None:
                    totals[metric][1] += seconds
                    block[metric][1] += seconds
                    if value:
                        totals[metric][0] += seconds
                        block[metric][0] += seconds
            cursor = segment_end

    previous = None
    for row in rows:
        at = instant(row["checked_at"])
        if start <= at < end:
            # A recorded outage still counts even without a measurable duration.
            record_status(blocks[(at - start) // block_duration], row)
        accumulate(previous, at)
        previous = row
    accumulate(previous, end)
    window = max(0, (end - start).total_seconds())

    def report(values, duration):
        good, known = values
        return {"availability": round(100 * good / known, 2) if known else None,
                "coverage": round(100 * known / duration, 2) if duration else None,
                "measured_seconds": round(known, 3), "available_seconds": round(good, 3),
                "unknown_seconds": round(max(0, duration - known), 3)}

    history = []
    for block in blocks:
        duration = (block["to"] - block["from"]).total_seconds()
        status = block["status"]
        if duration - block["full"][1] > 0.000001 and severity[status] < severity["unknown"]:
            status = "unknown"
        history.append({"from": block["from"].isoformat(), "to": block["to"].isoformat(),
                        "status": status, "core": report(block["core"], duration),
                        "full": report(block["full"], duration)})
    return {"core": report(totals["core"], window), "full": report(totals["full"], window),
            "history": history, "from": start.isoformat(), "to": end.isoformat()}
