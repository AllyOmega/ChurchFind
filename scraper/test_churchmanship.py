"""Tests for the churchmanship scorer.

Several of these exist because a measurement contradicted the lexicon. Where
that is so, the docstring records the number -- a test that says "this used to
fire 20 times in 384 articles" is worth more than one that says "should not
match".

    python -m pytest scraper/test_churchmanship.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import churchmanship as cm  # noqa: E402


# -- the false positives found by measuring against Wikipedia -----------------

def test_a_bare_society_is_not_the_society():
    """Anglo-Catholics call the Society of St Wilfrid and St Hilda "the Society",
    but the bare phrase matched 20 of 384 linked Wikipedia articles -- as often
    as "book of common prayer" -- and nearly every hit was a historical society,
    a missionary society, an aid society. It scored those parishes +1.0 on
    theology from a single coincidental phrase.
    """
    assert cm.score("The parish archives are held by the historical society.") is None
    assert cm.score("Funds came from the society for the propagation of the gospel") is None


def test_the_actual_society_still_scores():
    result = cm.score("The parish is a member of the Society of St Wilfrid and St Hilda.")
    assert result is not None
    assert result["theology"] > 0.5


def test_a_bare_year_is_not_a_prayer_book():
    """Only 28% of "1662"/"1928" mentions across those articles sat anywhere near
    prayer-book words. The rest were ordinary dates, and each one silently moved
    a parish half a point up the ceremonial axis.
    """
    assert cm.score("The parish hall was built in 1928 and rebuilt after the fire.") is None
    assert cm.score("The congregation was founded in 1662 by settlers.") is None


def test_a_year_that_names_what_it_is_a_year_of_still_scores():
    for text in ("We use the 1928 prayer book at the early service.",
                 "Sung according to the 1662 liturgy.",
                 "Services follow the Book of Common Prayer."):
        result = cm.score(text)
        assert result is not None, text
        assert result["ceremonial"] > 0


def test_bare_gospel_is_not_an_evangelical_marker():
    """Every tradition preaches the gospel; in a parish history it is what
    missionaries brought to the frontier. Only the compound survives."""
    assert cm.score("Missionaries carried the gospel across the territory.") is None
    result = cm.score("We are a gospel-centered parish.")
    assert result is not None and result["theology"] < 0


# -- the core contract --------------------------------------------------------

def test_no_match_is_none_not_a_confident_middle():
    """Zero and no-data are different answers. A parish nobody has any
    information about must not be rendered as a confident centrist."""
    assert cm.score("The church is on Main Street. Parking is behind the building.") is None
    assert cm.score("") is None
    assert cm.score(None) is None


def test_self_identification_outweighs_furniture():
    stated = cm.score("We are an Anglo-Catholic parish.")
    implied = cm.score("The processional cross leads the choir.")
    assert stated["confidence"] > implied["confidence"]
    assert stated["theology"] > implied["theology"]


def test_a_phrase_counts_once_however_often_it_appears():
    once = cm.score("We celebrate the Mass.")
    many = cm.score("Mass. " * 40 + "We celebrate the Mass.")
    assert once["confidence"] == many["confidence"]
    assert once["theology"] == many["theology"]


def test_the_two_axes_can_disagree():
    """The whole reason for two axes: a Prayer Book parish with Reformed
    doctrine is ceremonially high and theologically Protestant. One axis would
    have to lie about one of them."""
    result = cm.score(
        "Sung Matins from the Book of Common Prayer, in traditional language, "
        "with expository preaching and firm adherence to the Thirty-Nine Articles."
    )
    assert result["ceremonial"] > 0
    assert result["theology"] < 0


# -- source caps --------------------------------------------------------------

def test_wikipedia_is_capped_below_a_parish_website():
    """An encyclopedia article about a building is weaker and staler evidence
    than a parish describing its own worship, and cannot reach the same ceiling."""
    text = ("An Anglo-Catholic parish in the catholic tradition, with solemn mass, "
            "incense, benediction and the Angelus, using the English Missal.")
    site = cm.score(text, source="website")
    wiki = cm.score(text, source="wikipedia")
    assert wiki["confidence"] < site["confidence"]
    assert wiki["confidence"] <= cm.SOURCE_CAPS["wikipedia"]
    assert site["confidence"] <= cm.SOURCE_CAPS["website"]
    # Same evidence, so the reading itself must be identical -- only the
    # certainty attached to it changes.
    assert wiki["ceremonial"] == site["ceremonial"]
    assert wiki["theology"] == site["theology"]


def test_source_is_recorded_on_the_result():
    assert cm.score("Solemn mass with incense.", source="wikipedia")["source"] == "wikipedia"


# -- merging sources ----------------------------------------------------------

def test_agreement_does_not_raise_confidence():
    """Measured on the 27 parishes with both sources, they correlate at only
    +0.17 on ceremonial and +0.23 on theology. At that strength two sources
    landing near each other is as easily coincidence as corroboration -- most
    readings cluster near the middle anyway -- so agreement must not be rewarded.
    Doing so would manufacture certainty out of noise."""
    text = "Anglo-Catholic parish with solemn mass and incense."
    site = cm.score(text, source="website")
    wiki = cm.score(text, source="wikipedia")
    merged = cm.merge_sources([site, wiki])
    assert merged["confidence"] == site["confidence"]
    assert merged["source"] == "website+wikipedia"


def test_merging_contradictory_sources_lowers_confidence():
    """A website calling itself Anglo-Catholic while the article describes a
    plain preaching box is a parish that changed or a source that is wrong.
    Either way the meter should hedge rather than average confidently."""
    site = cm.score("Anglo-Catholic parish, solemn mass, incense, benediction.",
                    source="website")
    wiki = cm.score("A low church parish with expository preaching and a worship band.",
                    source="wikipedia")
    merged = cm.merge_sources([site, wiki])
    assert merged["confidence"] < site["confidence"]


def test_merging_never_exceeds_the_best_source_cap():
    """Adding a weak source can sharpen a reading but must not manufacture
    certainty. Two wikipedia-capped readings cannot reach the website ceiling."""
    text = ("Anglo-Catholic parish in the catholic tradition with solemn mass, "
            "incense, benediction, the Angelus and the English Missal.")
    a = cm.score(text, source="wikipedia")
    merged = cm.merge_sources([a, dict(a)])
    assert merged["confidence"] <= cm.SOURCE_CAPS["wikipedia"]


def test_merging_one_source_changes_nothing():
    only = cm.score("Solemn mass with incense.", source="website")
    assert cm.merge_sources([only]) == only
    assert cm.merge_sources([None, only]) == only


def test_merging_nothing_is_none():
    assert cm.merge_sources([]) is None
    assert cm.merge_sources([None, None]) is None


def test_merged_evidence_lists_both_sources_phrases():
    site = cm.score("We celebrate solemn mass.", source="website")
    wiki = cm.score("The parish is known for choral evensong.", source="wikipedia")
    merged = cm.merge_sources([site, wiki])
    assert "solemn mass" in merged["matched"]
    assert "evensong" in " ".join(merged["matched"])


# -- blending with votes ------------------------------------------------------

def test_three_votes_outweigh_a_confident_scrape():
    scraped = {"ceremonial": 0.9, "theology": 0.9, "confidence": cm.SOURCE_CAPS["website"]}
    blended = cm.blend(scraped, [(-0.8, -0.8), (-0.8, -0.8), (-0.8, -0.8)])
    assert blended["ceremonial"] < 0
    assert blended["source"] == "community"


def test_disagreeing_votes_lower_confidence():
    scraped = {"ceremonial": 0.5, "theology": 0.5, "confidence": 0.5}
    agree = cm.blend(scraped, [(0.5, 0.5)] * 4)
    conflict = cm.blend(scraped, [(1.0, 1.0), (-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0)])
    assert conflict["confidence"] < agree["confidence"]


def test_a_welcome_mat_is_not_a_churchmanship_claim():
    """"Come as you are" appeared on 33 parish websites and zero Wikipedia
    articles. The most ceremonially elaborate shrine in a diocese still says it
    on the homepage -- it is hospitality copy, and treating it as evidence biased
    the whole website source 0.17 low on ceremonial against Wikipedia."""
    assert cm.score("Come as you are — all are welcome at our table.") is None
    assert cm.score("Casual dress is fine. Parking is behind the church.") is None
    assert cm.score("Join us for an informal coffee after the service.") is None


def test_informality_still_counts_when_it_describes_the_worship():
    for text in ("Our 9am is an informal service in the parish hall.",
                 "Worship is casual and contemporary.",
                 "A casual worship setting with a band."):
        result = cm.score(text)
        assert result is not None, text
        assert result["ceremonial"] < 0, text
