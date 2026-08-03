"""Tests for pulling service times out of parish-website prose.

The bias throughout is towards saying nothing. A missing service time is an
inconvenience; a wrong one sends somebody to a locked door on a Sunday morning.
Several of these tests are real pages that produced a wrong answer.

    python -m pytest scraper/test_prose_times.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import parse_prose_times as ppt  # noqa: E402

SUN, MON, TUE, WED, THU, FRI, SAT = 6, 0, 1, 2, 3, 4, 5


# -- the shapes parishes actually write ---------------------------------------

def test_simple_sunday_service():
    assert ppt.extract("We meet every Sunday at 9:00 am for Holy Eucharist") == [(SUN, 9 * 60)]


def test_several_times_on_one_day():
    assert ppt.extract("Sunday Worship: 8:00 and 10:30 a.m. Holy Eucharist") == \
        [(SUN, 8 * 60), (SUN, 10 * 60 + 30)]


def test_heading_then_times():
    assert ppt.extract("SUNDAY SERVICE 8:00 a.m. (without music) 10:30 a.m.") == \
        [(SUN, 8 * 60), (SUN, 10 * 60 + 30)]


def test_plural_day_and_bare_hour():
    assert ppt.extract("Join us for Sunday worship at 10 am") == [(SUN, 10 * 60)]


def test_afternoon_and_evening():
    assert ppt.extract("Sunday Mass at 6:30 pm") == [(SUN, 18 * 60 + 30)]


def test_weekday_services_are_kept():
    slots = ppt.extract("Wednesday Healing Eucharist 5:00pm")
    assert slots == [(WED, 17 * 60)]


# -- the false positives, each found on a real page ---------------------------

def test_a_days_times_do_not_leak_into_the_next_day():
    """From All Saints, Austin. The window ran past "Wednesday:" and gave Sunday
    a 7:05am service that does not exist."""
    text = ("Sunday: 8:00 AM, 10:15 AM, 6:30 PM Holy Communion "
            "Wednesday: 7:05 Morning Prayer, 12:05 Holy Eucharist with Unction")
    slots = ppt.extract(text)
    assert (SUN, 8 * 60) in slots and (SUN, 18 * 60 + 30) in slots
    assert (SUN, 7 * 60 + 5) not in slots, "Wednesday's time leaked into Sunday"
    assert (SUN, 12 * 60 + 5) not in slots


def test_an_annual_service_is_not_a_weekly_one():
    """From Saint Mark's Cathedral, Seattle: "11 p.m. Christmas Eve" sat inside a
    Sunday block and became a weekly 11pm Sunday service."""
    text = ("Sunday services at 8 a.m. and 11 a.m. We use incense at "
            "11 p.m. Christmas Eve and the Great Vigil of Easter (8:30 p.m.)")
    slots = ppt.extract(text)
    assert (SUN, 8 * 60) in slots
    assert (SUN, 23 * 60) not in slots, "an annual service became a weekly one"
    assert (SUN, 20 * 60 + 30) not in slots


def test_a_dated_announcement_is_not_a_schedule():
    """"Sunday 4 August at 3pm" is one concert, not every Sunday."""
    assert ppt.extract("Choral concert Sunday 4 August at 3:00 pm in the church") == []
    assert ppt.extract("Join us Sunday, August 4 at 3:00 pm for Evensong") == []


def test_office_hours_are_not_a_service():
    assert ppt.extract("The parish office is open Monday to Friday, 9:00 am to 5:00 pm") == []


def test_other_parish_activities_are_not_services():
    for text in ("Sunday School meets at 9:15 am",
                 "AA meeting Wednesday at 7:00 pm",
                 "Food pantry open Saturday 10:00 am",
                 "Choir practice Thursday at 7:30 pm",
                 "Coffee hour follows on Sunday at 11:00 am"):
        assert ppt.extract(text) == [], text


def test_a_time_needs_a_day():
    """A bare time is as likely to be an office hour or a phone fragment."""
    assert ppt.extract("Holy Eucharist at 10:30 am") == []


def test_a_time_needs_worship_nearby():
    assert ppt.extract("Sunday parking is available from 8:00 am") == []


def test_nothing_found_is_empty_not_a_guess():
    assert ppt.extract("") == []
    assert ppt.extract(None) == []
    assert ppt.extract("St Mary's is at 119 East 74th Street. Call 555-0100.") == []


def test_implausible_clock_times_are_dropped():
    """3am is not a service, and "13pm" is a typo rather than a time."""
    assert ppt.extract("Sunday Mass at 3:00 am") == []
    assert ppt.extract("Sunday Mass at 13:00 pm") == []


def test_a_page_full_of_times_is_rejected_wholesale():
    """Past a dozen slots this is a parse failure, not a parish, and publishing
    it would be worse than publishing nothing."""
    text = " ".join(f"Sunday Eucharist at {h}:00 am" for h in range(6, 12)) + \
           " ".join(f"Monday Mass at {h}:00 pm" for h in range(1, 12))
    assert ppt.extract(text) == []


# -- output shape, shared with the OSM parser ---------------------------------

def test_pairs_match_the_osm_parsers_format():
    """queries.py does a LIKE over this string; the shape is a contract."""
    assert ppt.to_pairs("Sunday Eucharist at 9:00 am") == ",6-0900,"
    assert ppt.to_pairs("nothing here") == ""


def test_pairs_are_comma_wrapped_so_a_prefix_cannot_match():
    """",6-" must not match "16-"."""
    pairs = ppt.to_pairs("Sunday Mass 9:00 am Wednesday Mass 6:00 pm")
    assert pairs.startswith(",") and pairs.endswith(",")


def test_describe_is_readable():
    text = "Sunday Eucharist at 8:00 am and 10:30 am. Wednesday Mass at 12:10 pm."
    assert ppt.describe(text) == "Wednesday 12:10pm; Sunday 8am, 10:30am"
