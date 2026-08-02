"""Tests for the parts of the scrape that are not just network calls.

The scrape itself needs Overpass, so it is not tested here. What is tested is
the bookkeeping around it -- specifically that a partial run does not corrupt
the file every visitor loads.

    python -m pytest scraper/test_scrape.py
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scrape_churches  # noqa: E402


def write_fake_state(directory, code, count, timestamp="2026-01-01T00:00:00Z"):
    """A minimal state file in the real on-disk shape."""
    rows = [
        [f"n{code}{n}", f"Church {n}", "Baptist", "baptist", "1 Main St", "Town",
         code, "00000", 40.0, -100.0, "https://example.invalid", "555", "", "",
         "", "", "", "", "2026-05-14"]
        for n in range(count)
    ]
    payload = {
        "state": code,
        "name": scrape_churches.STATE_NAMES[code],
        "count": count,
        "osm_timestamp": timestamp,
        "fields": scrape_churches.FIELDS,
        "churches": rows,
    }
    (directory / f"{code}.json").write_text(json.dumps(payload))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the scraper's paths at a temporary tree."""
    states = tmp_path / "states"
    states.mkdir(parents=True)
    monkeypatch.setattr(scrape_churches, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scrape_churches, "STATE_DIR", states)
    return tmp_path, states


def test_index_covers_every_state_on_disk_not_just_this_run(data_dir):
    """The bug this exists to prevent.

    `--states CO UT` used to rebuild index.json from only the states it had
    just fetched, so the file the site always loads would claim the country
    held two states. Everything else stayed on disk and became unreachable:
    the browse-by-state grid lost 49 entries and the national total was wrong
    by two orders of magnitude. The index is a function of the whole directory,
    never of one invocation's arguments.
    """
    root, states = data_dir
    write_fake_state(states, "CO", 3)
    write_fake_state(states, "UT", 2)
    write_fake_state(states, "TX", 5)

    scrape_churches.write_index()

    index = json.loads((root / "index.json").read_text())
    assert {s["code"] for s in index["states"]} == {"CO", "UT", "TX"}
    assert index["totals"]["churches"] == 10


def test_index_totals_match_the_sum_of_the_files(data_dir):
    root, states = data_dir
    write_fake_state(states, "CO", 4)
    write_fake_state(states, "WY", 1)

    scrape_churches.write_index()
    index = json.loads((root / "index.json").read_text())

    assert index["totals"]["churches"] == sum(s["count"] for s in index["states"])
    # Every fake row carries a website and a phone but no wheelchair tag.
    assert index["totals"]["with_website"] == 5
    assert index["totals"]["with_phone"] == 5


def test_index_ignores_states_with_no_file(data_dir):
    """A state that has never been fetched is absent, not zero.

    A zero entry would render an empty state in the browse grid and read as
    "no churches in Wyoming" rather than "not scraped yet".
    """
    root, states = data_dir
    write_fake_state(states, "CO", 3)

    scrape_churches.write_index()
    index = json.loads((root / "index.json").read_text())

    assert [s["code"] for s in index["states"]] == ["CO"]


def test_osm_snapshot_is_the_newest_across_all_states(data_dir):
    """The snapshot date shown to visitors is the freshest file, not the last
    one written -- a rerun of one state should not date the whole dataset."""
    root, states = data_dir
    write_fake_state(states, "CO", 1, timestamp="2026-08-02T00:00:00Z")
    write_fake_state(states, "TX", 1, timestamp="2026-03-01T00:00:00Z")

    scrape_churches.write_index()
    index = json.loads((root / "index.json").read_text())

    assert index["osm_snapshot"] == "2026-08-02T00:00:00Z"


def test_states_are_listed_alphabetically_by_name(data_dir):
    """The browse grid renders in file order, so the order is a contract."""
    root, states = data_dir
    for code in ("WY", "AL", "CO"):
        write_fake_state(states, code, 1)

    scrape_churches.write_index()
    index = json.loads((root / "index.json").read_text())

    names = [s["name"] for s in index["states"]]
    assert names == sorted(names)
