"""Time-weighted availability over bounded observations, with explicit coverage."""
from datetime import datetime, timedelta, timezone
from health_contract import instant


def measured_availability(rows, start: datetime, end: datetime) -> dict:
    totals = {"core": [0.0, 0.0], "full": [0.0, 0.0]}
    daily = {}

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
            midnight = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
            segment_end = min(until, midnight)
            seconds = (segment_end - cursor).total_seconds()
            day = daily.setdefault(cursor.date().isoformat(), {"core": [0.0, 0.0], "full": [0.0, 0.0]})
            for metric, column in (("core", "core_available"), ("full", "full_available")):
                value = row[column]
                if value is not None:
                    totals[metric][1] += seconds
                    day[metric][1] += seconds
                    if value:
                        totals[metric][0] += seconds
                        day[metric][0] += seconds
            cursor = segment_end

    previous = None
    for row in rows:
        accumulate(previous, instant(row["checked_at"]))
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
    date = start.date()
    while date <= end.date():
        day_start = datetime.combine(date, datetime.min.time(), timezone.utc)
        duration = max(0, (min(end, day_start + timedelta(days=1)) - max(start, day_start)).total_seconds())
        if duration:
            values = daily.get(date.isoformat(), {"core": [0, 0], "full": [0, 0]})
            history.append({"day": date.isoformat(), "core": report(values["core"], duration),
                            "full": report(values["full"], duration)})
        date += timedelta(days=1)
    return {"core": report(totals["core"], window), "full": report(totals["full"], window),
            "history": history, "from": start.isoformat(), "to": end.isoformat()}
