"""Operating rules as pure functions: office hours per WIB/WITA/WIT, the Friday prayer window, holidays.

    check_slot(start, duration_min, participants, rules=None)            -> [] when allowed, else violations
    next_allowed_slots(after, duration_min, participants, n=3, rules=None) -> next n allowed starts (UTC)

Participants are Contact objects (anything with .id and .tz). Times are timezone-aware datetimes; Indonesia
has no daylight saving, so each zone is a fixed UTC offset. Assumption (documented in the data file): the
Friday prayer window is approx. 11:30-13:00 local time for every participant; real times vary by city.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "operating_rules.json"
SEARCH_DAYS = 21


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Rules:
    zones: dict
    office_start: time
    office_end: time
    workdays: tuple
    prayer_start: time
    prayer_end: time
    prayer_assumption: str
    step: int
    holidays: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Rules":
        p = Path(path or os.environ.get("REIMBURSE_OFFICE_RULES") or DEFAULT_PATH)
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(zones=d["time_zones"], office_start=_hm(d["office_hours"]["start"]),
                   office_end=_hm(d["office_hours"]["end"]), workdays=tuple(d["office_hours"]["workdays"]),
                   prayer_start=_hm(d["friday_prayer"]["start"]), prayer_end=_hm(d["friday_prayer"]["end"]),
                   prayer_assumption=d["friday_prayer"]["assumption"], step=int(d["slot_step_minutes"]),
                   holidays=dict(d["holidays"]))

    def tz(self, zone: str) -> timezone:
        return timezone(timedelta(hours=self.zones[zone]), zone)


_DEFAULT: Rules | None = None


def default_rules() -> Rules:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Rules.load()
    return _DEFAULT


def local(rules: Rules, dt: datetime, zone: str) -> datetime:
    return dt.astimezone(rules.tz(zone))


def fmt_local(rules: Rules, dt: datetime, zone: str) -> str:
    """'Fri 30 Oct 2026 13:00 WIB'"""
    return local(rules, dt, zone).strftime("%a %d %b %Y %H:%M ") + zone


def check_slot(start: datetime, duration_min: int, participants, rules: Rules | None = None) -> list[str]:
    rules = rules or default_rules()
    if start.tzinfo is None:
        return ["start time has no time zone"]
    end = start + timedelta(minutes=duration_min)
    out: list[str] = []
    for p in participants:
        a, b = local(rules, start, p.tz), local(rules, end, p.tz)
        who = f"{p.id} ({p.tz})"
        if a.date() != b.date() and not (b.time() == time(0) and b.date() == a.date() + timedelta(days=1)):
            out.append(f"{who}: spans two days")
        if a.weekday() not in rules.workdays:
            out.append(f"{who}: {a:%A} is not a working day")
        hol = rules.holidays.get(a.date().isoformat())
        if hol:
            out.append(f"{who}: {a.date().isoformat()} is a public holiday ({hol})")
        if a.time() < rules.office_start or (b.time() > rules.office_end or a.date() != b.date()):
            out.append(f"{who}: {a:%H:%M}-{b:%H:%M} is outside office hours "
                       f"{rules.office_start:%H:%M}-{rules.office_end:%H:%M} {p.tz}")
        if a.weekday() == 4 and a.time() < rules.prayer_end and b.time() > rules.prayer_start:
            out.append(f"{who}: overlaps the Friday prayer window "
                       f"{rules.prayer_start:%H:%M}-{rules.prayer_end:%H:%M} {p.tz} (assumed)")
    return out


def _ceil(rules: Rules, dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    extra = (-dt.minute) % rules.step
    return dt + timedelta(minutes=extra)


def next_allowed_slots(after: datetime, duration_min: int, participants, n: int = 3,
                       rules: Rules | None = None) -> list[datetime]:
    rules = rules or default_rules()
    """The first n allowed starts at or after `after`, on the rules' step grid, in UTC. [] if none in 21 days."""
    t, stop, out = _ceil(rules, after), after + timedelta(days=SEARCH_DAYS), []
    while t < stop and len(out) < n:
        if not check_slot(t, duration_min, participants, rules):
            out.append(t)
        t += timedelta(minutes=rules.step)
    return out


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
