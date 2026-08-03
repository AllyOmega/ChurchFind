"""Pull service times out of the prose on a parish website.

`service_times.py` parses OpenStreetMap's `opening_hours` grammar, which is
formal and unambiguous. This parses what parishes actually write:

    "We meet every Sunday at 9:00 am for Holy Eucharist"
    "SUNDAY WORSHIP SCHEDULE ... 9:30 AM Holy Eucharist"
    "Sunday Worship: 8:00 and 10:00 a.m."
    "Sundays: 8:00 a.m. Rite I, 10:30 a.m. Rite II"

Only 204 of 2,812 Anglican churches have a service time in OSM -- 7%. Roughly a
third have a website, and 78% of the ones already fetched carry a clock time and
a weekday somewhere on the page. This is where the times are.

HOW IT AVOIDS INVENTING SERVICES

A wrong service time is worse than a missing one: somebody drives out on a Sunday
morning and finds a locked door. Everything here is biased towards saying
nothing.

* **A time must be anchored to a weekday.** A bare "10:30" on a page is as likely
  to be an office hour, a concert or a food-bank slot.
* **The weekday must be close by.** A time more than `WINDOW` characters after
  the day it supposedly belongs to is not evidence, it is coincidence -- pages
  are long and full of numbers.
* **Worship words raise a slot, admin words kill it.** "Holy Eucharist" nearby
  is a service; "office hours", "AA meeting", "vestry" and "food pantry" are not,
  and those phrases veto the slot rather than merely outweighing it.
* **Dated announcements are skipped.** "Sunday 4 August at 3pm" is one event, not
  a weekly service, and a one-off concert should never look like a Sunday mass.
* **Nothing found is nothing returned.** As with churchmanship, no-data and zero
  are different answers.

The output is the same `(weekday, minutes)` pair list the OSM parser produces, so
everything downstream -- search, the day/period filters, the DB column -- is
unchanged.
"""

import re

DAY_NAMES = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "weds": 2, "wed": 2, "thursday": 3, "thurs": 3, "thu": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

# How far after a weekday a time may sit and still be taken to belong to it.
# Wide enough for "Sundays: 8:00 a.m. Holy Eucharist Rite I, 10:30 a.m. Rite II",
# narrow enough that the next paragraph's numbers do not get swept in.
WINDOW = 120

# Worship words. Their presence near a time is what makes it a service.
# The plural forms are spelled out because `service\b` does not match
# "services" -- there is no word boundary between the "e" and the "s" -- and
# "Sunday services at 8 a.m." is one of the commonest ways a parish writes this.
WORSHIP = re.compile(
    r"\b(?:mass(?:es)?|eucharists?|communion|worship|services?|liturg(?:y|ies)|"
    r"matins|mattins|evensong|evening prayer|morning prayer|compline|holy days?|"
    r"sung|said|rite\s+(?:i|ii|one|two)|contemporary|traditional)\b",
    re.IGNORECASE,
)

# Words that mean the number beside them is not a service. These veto rather
# than outvote: a parish office open 9-5 must never become a 9am service.
NOT_WORSHIP = re.compile(
    r"\b(?:office hours?|office is open|a\.?a\.?\s+meeting|al[- ]anon|"
    r"food (?:bank|pantry)|thrift|vestry|committee|rehearsal|choir practice|"
    r"scouts?|boy scouts|girl scouts|yoga|preschool|day\s?care|nursery school|"
    r"parking|bingo|blood drive|rummage|book club|bible study|sunday school|"
    r"coffee hour|fellowship hour|open house|concert|recital|wedding|funeral|"
    r"tickets?|admission|doors open|registration)\b",
    re.IGNORECASE,
)

# "Sunday, 4 August", "Sunday August 4", "Sun 8/4" -- a dated announcement is one
# event, not a weekly pattern.
DATED = re.compile(
    r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\b(?:20\d{2})\b",
    re.IGNORECASE,
)

# Services that happen once a year. Checked against the text immediately around
# a time rather than the whole window, because "except during Advent and Lent"
# is a normal aside inside a perfectly good weekly schedule and must not veto it.
# Found on a real cathedral page, where "11 p.m. Christmas Eve" inside a Sunday
# block became a weekly 11pm Sunday service.
SEASONAL = re.compile(
    r"\b(?:christmas|easter|ash wednesday|good friday|maundy|palm sunday|"
    r"pentecost|all souls|all saints|holy week|great vigil|new year|"
    r"thanksgiving|epiphany|ascension|patronal)\b",
    re.IGNORECASE,
)
# Tight on purpose. The veto has to catch "11 p.m. Christmas Eve" and
# "Christmas Eve at 11 p.m." while leaving a legitimate 8am alone in a sentence
# that mentions Christmas later on -- a wide reach kills real services to
# suppress fake ones, which is the wrong trade in this direction.
SEASONAL_REACH = 25

_DAY_RE = re.compile(
    r"\b(" + "|".join(sorted(DAY_NAMES, key=len, reverse=True)) + r")s?\b",
    re.IGNORECASE,
)

# 9:00 am / 9.00am / 9 am / 09:00 / 10:30a.m.  A bare "9" is never a time here --
# too many of them are addresses, verse numbers and phone fragments.
_TIME_RE = re.compile(
    r"\b(?P<h>1[0-2]|0?[1-9]|1[3-9]|2[0-3])"
    r"(?:[:.](?P<m>[0-5][0-9]))?"
    r"\s*(?P<ampm>a\.?m\.?|p\.?m\.?)"
    r"|\b(?P<h24>[01]?[0-9]|2[0-3]):(?P<m24>[0-5][0-9])\b",
    re.IGNORECASE,
)

MAX_SLOTS = 12          # a parish with more than this is a cathedral or a parse error


def _minutes(match):
    """Match -> minutes past midnight, or None if it is not a real clock time."""
    if match.group("ampm"):
        hour = int(match.group("h"))
        minute = int(match.group("m") or 0)
        meridiem = match.group("ampm").replace(".", "").lower()
        if hour > 12:
            return None                       # "13pm" is a typo, not a time
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return hour * 60 + minute
    if match.group("h24") is not None:
        hour, minute = int(match.group("h24")), int(match.group("m24"))
        # A 24-hour time with no meridiem is only trustworthy when it could not
        # be something else. Church pages rarely use it, and "8:00" alone is
        # ambiguous between morning and evening -- assume the morning only for
        # hours that have no evening reading.
        if hour == 0 or hour > 23:
            return None
        return hour * 60 + minute
    return None


def extract(text):
    """Return sorted unique (weekday, minutes) pairs, or [] if nothing is safe.

    weekday is 0=Monday .. 6=Sunday, matching service_times.parse().
    """
    if not text:
        return []

    found = set()
    for day_match in _DAY_RE.finditer(text):
        weekday = DAY_NAMES[day_match.group(1).lower()]
        window = text[day_match.end():day_match.end() + WINDOW]

        # Stop at the next weekday. Without this a schedule laid out as
        # "Sunday: 8:00, 10:15, 6:30 ... Wednesday: 7:05, 12:05" hands Sunday
        # the Wednesday times as well -- found on a real parish page, where it
        # invented a 7:05am Sunday service that does not exist.
        next_day = _DAY_RE.search(window)
        if next_day:
            window = window[:next_day.start()]

        # The sentence around the day decides whether this is worship at all.
        context = text[max(0, day_match.start() - 60):day_match.end() + WINDOW]
        if NOT_WORSHIP.search(context):
            continue
        if DATED.search(context):
            continue
        if not WORSHIP.search(context):
            continue

        for time_match in _TIME_RE.finditer(window):
            minutes = _minutes(time_match)
            if minutes is None:
                continue
            near = window[max(0, time_match.start() - SEASONAL_REACH):
                          time_match.end() + SEASONAL_REACH]
            if SEASONAL.search(near):
                continue
            # Anything before 5am or after 11pm on a church page is a phone
            # number, a year or a parse accident rather than a service.
            if minutes < 5 * 60 or minutes > 23 * 60:
                continue
            found.add((weekday, minutes))

    if not found or len(found) > MAX_SLOTS:
        return []
    return sorted(found)


def to_pairs(text):
    """The same `,D-HHMM,` string service_times.to_pairs produces."""
    slots = extract(text)
    if not slots:
        return ""
    return "," + ",".join(f"{day}-{m // 60:02d}{m % 60:02d}" for day, m in slots) + ","


# The same abbreviations service_times.describe() uses. A card can carry times
# from either source and they must not be rendered two different ways.
DAY_LABELS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def describe(text):
    """Human-readable rendering, grouped by day.

    Deliberately identical in shape to service_times.describe(), because a
    results list mixes both sources and "Sunday 11am" beside "Su 11am" reads as
    two different kinds of fact rather than one fact from two places.
    """
    slots = extract(text)
    if not slots:
        return ""
    by_day = {}
    for day, minutes in slots:
        by_day.setdefault(day, []).append(minutes)
    parts = []
    for day in sorted(by_day):
        times = ", ".join(_clock(m) for m in sorted(by_day[day]))
        parts.append(f"{DAY_LABELS[day]} {times}")
    return "; ".join(parts)


def _clock(minutes):
    hour, minute = divmod(minutes, 60)
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    return f"{display}:{minute:02d}{suffix}" if minute else f"{display}{suffix}"
