"""Parse OpenStreetMap `service_times` into something searchable.

OSM stores worship times in the `opening_hours` grammar: `Su 09:00,11:00`,
`Su[1] 10:30`, `We 19:00; Su 08:00-12:00`, `Sa 17:00; Su 09:00,10:45 "Spanish"`.
It is a real grammar with a real specification, and a full implementation is a
library in its own right.

This module implements the subset that actually appears in church data, and is
deliberate about the rest: anything it cannot parse produces no slots at all
rather than a guess. A church whose times we could not read is excluded from a
time search instead of being asserted into the wrong bucket -- someone turning
up to a service that isn't happening is a worse failure than not finding it.

The output is a set of `(weekday, minutes-from-midnight)` pairs, flattened by
the build step into two searchable columns:

    service_pairs  ",6-0900,6-1100,3-1900,"   -- day-time, Monday = 0

They are stored as *pairs*, not as a set of days and a separate set of times.
Storing them separately makes "Sunday evening" match a church with a Sunday
morning service and a Thursday evening one, which is a confidently wrong answer
to a question someone asked in order to turn up somewhere.

A leading day of `x` means the time is known but the day is not (`11:00` with no
weekday), so it answers a time-of-day question without claiming a weekday.
"""

import re

WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
WEEKDAY_INDEX = {day: i for i, day in enumerate(WEEKDAYS)}

# Coarse buckets the UI offers. A service is in a bucket if it starts inside it.
PERIODS = {
    "early":     (0, 8 * 60),           # before 08:00
    "morning":   (8 * 60, 12 * 60),     # 08:00-11:59
    "midday":    (12 * 60, 15 * 60),    # 12:00-14:59
    "afternoon": (15 * 60, 18 * 60),    # 15:00-17:59
    "evening":   (18 * 60, 24 * 60),    # 18:00 onwards
}

PERIOD_LABELS = {
    "early": "Before 8am",
    "morning": "Morning (8am–12pm)",
    "midday": "Midday (12–3pm)",
    "afternoon": "Afternoon (3–6pm)",
    "evening": "Evening (after 6pm)",
}

# 24-hour (`09:30`) and the 12-hour forms people actually type (`11am`,
# `9:30AM`, `7.30pm`). The grammar only specifies the first; the rest are
# unambiguous enough to parse rather than discard.
_TIME = re.compile(
    r"^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?$|^(?P<h4>\d{2})(?P<m4>\d{2})$",
    re.IGNORECASE
)
# `Su[1]`, `Su[1,3]`, `Su[-1]` -- nth-of-month qualifiers. We keep the weekday
# and drop the qualifier: "some Sundays" is still a Sunday for search purposes.
_NTH = re.compile(r"\[[^\]]*\]")


def _parse_time(text, allow_bare_four_digits=False):
    """`09:30` -> 570, `7pm` -> 1140. None if it is not a time we understand.

    The colon-less `0900` form is only accepted alongside a weekday. On its own
    a four-digit number is far more likely to be a year -- `2019` is a perfectly
    valid 20:19 and almost never means that.
    """
    match = _TIME.match(text.strip())
    if not match:
        return None
    if match.group("h4"):
        if not allow_bare_four_digits:
            return None
        hour, minute = int(match.group("h4")), int(match.group("m4"))
        return hour * 60 + minute if hour <= 23 and minute <= 59 else None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()

    # A bare number with no colon and no am/pm is not a time -- it is a year, a
    # street number, or noise. Require one or the other.
    if not match.group(2) and not meridiem:
        return None

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0

    # `24:00` is legal in the grammar as an end time; as a start it is midnight.
    if hour == 24 and minute == 0:
        return 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _parse_days(text):
    """`Su`, `Mo-Fr`, `Sa,Su`, `Su[1]` -> list of weekday indexes."""
    text = _NTH.sub("", text).strip()
    if not text:
        return []

    days = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            start_index = WEEKDAY_INDEX.get(start.strip()[:2].title())
            end_index = WEEKDAY_INDEX.get(end.strip()[:2].title())
            if start_index is None or end_index is None:
                return []
            # Ranges wrap: `Sa-Su` and `Fr-Mo` are both meaningful.
            index = start_index
            while True:
                days.append(index)
                if index == end_index:
                    break
                index = (index + 1) % 7
        else:
            index = WEEKDAY_INDEX.get(part[:2].title())
            if index is None:
                return []
            days.append(index)
    return days


def parse(value):
    """Return a sorted list of (weekday, minutes) for a `service_times` value.

    Empty for anything unparseable -- see the module docstring on why that is
    the right failure mode here.
    """
    if not value or not isinstance(value, str):
        return []
    text = value.strip()
    if not text or text.lower() in ("yes", "no", "unknown"):
        return []

    slots = set()
    # `;` separates independent rules; `||` is a fallback rule separator.
    for rule in re.split(r"[;|]+|\band\b", text, flags=re.IGNORECASE):
        rule = rule.strip()
        if not rule:
            continue
        # Drop trailing quoted comments: `Su 10:00 "Spanish"`.
        rule = re.sub(r'"[^"]*"', " ", rule).strip()
        if not rule:
            continue

        # A rule is a day spec followed by one or more times. Split at the first
        # token that starts with a digit -- days never do.
        tokens = rule.split()
        day_tokens, time_tokens = [], []
        for token in tokens:
            if time_tokens or (token and token[0].isdigit()):
                time_tokens.append(token)
            else:
                day_tokens.append(token)

        days = _parse_days(" ".join(day_tokens))
        if not days:
            # A time with no weekday ("11:00") is the single most common
            # unparseable value. The time itself is unambiguous; the day is
            # genuinely unknown, so record it as day None -- searchable by
            # time of day, absent from any weekday filter. Guessing "Sunday"
            # would be right most of the time and wrong often enough to send
            # someone to a locked building.
            if day_tokens:
                continue          # there was a day spec and we failed to read it
            days = [None]

        for token in re.split(r"[,&]", " ".join(time_tokens)):
            token = token.strip().rstrip("+").strip()
            if not token:
                continue
            # A range like `09:00-12:00` is a period the building is open for
            # worship; index its start, which is when people need to arrive.
            start = token.split("-")[0].strip()
            minutes = _parse_time(start, allow_bare_four_digits=days != [None])
            if minutes is None:
                continue
            for day in days:
                slots.add((day, minutes))

    # None sorts before ints on the day key, so key explicitly.
    return sorted(slots, key=lambda slot: (-1 if slot[0] is None else slot[0], slot[1]))


def to_pairs(value):
    """Flatten to a comma-wrapped `,D-HHMM,` string, or "" if nothing parsed.

    Wrapped in commas so a LIKE for ",6-" cannot match "16-". The day is `x`
    when the source gave a time with no weekday.
    """
    slots = parse(value)
    if not slots:
        return ""
    parts = [
        f"{'x' if day is None else day}-{minutes // 60:02d}{minutes % 60:02d}"
        for day, minutes in slots
    ]
    return "," + ",".join(parts) + ","


def periods_for(value):
    """Which coarse time-of-day buckets a `service_times` value falls into."""
    found = set()
    for _, minutes in parse(value):
        for name, (start, end) in PERIODS.items():
            if start <= minutes < end:
                found.add(name)
    return sorted(found)


def describe(value):
    """Human-readable rendering, for when the raw tag is too cryptic to show."""
    slots = parse(value)
    if not slots:
        return ""
    by_day = {}
    for day, minutes in slots:
        by_day.setdefault(day, []).append(minutes)

    parts = []
    for day in sorted(by_day, key=lambda d: -1 if d is None else d):
        times = ", ".join(_clock(m) for m in sorted(by_day[day]))
        parts.append(times if day is None else f"{WEEKDAYS[day]} {times}")
    return "; ".join(parts)


def _clock(minutes):
    hour, minute = divmod(minutes, 60)
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    return f"{display}:{minute:02d}{suffix}" if minute else f"{display}{suffix}"
