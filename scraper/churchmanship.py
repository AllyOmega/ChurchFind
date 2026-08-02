"""Estimate Anglican churchmanship on two axes.

Churchmanship is usually described as one line from "low" to "high", but that
collapses two things that genuinely vary independently:

    ceremonial   -1 plain, informal, preaching-centred
                 +1 elaborate ceremonial -- vestments, incense, chant, procession

    theology     -1 Reformed / Evangelical
                 +1 Anglo-Catholic

They correlate, but the off-diagonal corners are real parishes. A Prayer Book
parish using 1662 with dignity and Reformed doctrine is high-ceremonial and
Protestant. A charismatic parish with a strong sacramental theology and a
worship band is low-ceremonial and catholic-leaning. One axis cannot describe
either without lying about the other.

WHAT THIS CAN AND CANNOT KNOW

The honest answer is that OpenStreetMap knows almost nothing about this. Of
2,815 Anglican and Episcopal churches in the dataset, seven have a descriptor in
their service times. The signal that exists is a parish's own website -- how it
describes its own worship -- and only about a third have one recorded.

So the scraped score is a weak prior, not an answer. Every estimate carries a
confidence, the UI shows which phrases produced it, and user submissions
outweigh it quickly (see `blend`). A parish with no website and no votes gets no
score at all rather than a made-up one.

WHAT IS DELIBERATELY NOT USED

- **Dedications.** St Mary the Virgin and All Souls read as Anglo-Catholic to a
  British ear, but a dedication records the fashion of the founding decade, not
  what happens there on Sunday. Scoring on it would systematically mislabel
  older parishes.
- **"Holy Communion" or "Eucharist" alone.** Both are now used across the whole
  spectrum and separate nothing.
- **Denomination or province.** ACNA and TEC parishes both span the full range;
  membership predicts nothing useful here.
"""

import re

# (pattern, theology, ceremonial, strength)
#
# theology and ceremonial are the direction each phrase points, -1 to +1.
# strength is how much weight to give it -- a parish calling itself
# "Anglo-Catholic" is telling you directly; owning an organ is not.
LEXICON = [
    # -- self-identification: the strongest signals there are ------------------
    (r"anglo[- ]?catholic",                    +1.0, +0.7, 3.0),
    (r"\bevangelical\b",                       -0.9, -0.5, 2.5),
    (r"catholic tradition|catholic faith and (?:order|practice)", +0.9, +0.5, 2.5),
    (r"reformed (?:catholic|anglican|episcopal)", -0.8, -0.1, 2.0),
    (r"\bbroad church\b",                       0.0,  0.0, 1.0),
    (r"\blow church\b",                        -0.7, -0.9, 2.5),
    (r"\bhigh church\b",                       +0.6, +0.9, 2.5),
    # "The Society" is what Anglo-Catholics call the Society of St Wilfrid and
    # St Hilda, but the bare phrase cannot be used: in ordinary prose it means
    # the historical society, the missionary society, the altar guild society.
    # Measured against 384 linked Wikipedia articles it fired 20 times, as often
    # as "book of common prayer", and almost every hit was a different society.
    (r"forward in faith|society of (?:st\.? )?wilfrid", +1.0, +0.6, 2.5),
    (r"walsingham|society of mary",            +1.0, +0.5, 2.0),

    # -- what the liturgy is called -------------------------------------------
    (r"\bsolemn mass\b|\bhigh mass\b",         +0.9, +1.0, 2.5),
    (r"\bsung mass\b|\bsaid mass\b|\blow mass\b", +0.8, +0.5, 2.0),
    (r"\bthe mass\b|\bdaily mass\b",           +0.7, +0.4, 1.5),
    (r"sung eucharist|choral eucharist",       +0.3, +0.8, 1.5),
    (r"choral evensong|\bevensong\b",          +0.1, +0.8, 1.2),
    (r"\bmatins\b|\bmattins\b",                 0.0, +0.6, 1.0),
    (r"\bcompline\b",                          +0.3, +0.6, 1.0),
    (r"\bangelus\b|\bbenediction\b",           +0.9, +0.8, 2.5),
    (r"blessed sacrament|adoration",           +0.9, +0.7, 2.0),
    (r"\brosary\b|\bmarian\b|our lady of",     +0.9, +0.4, 2.0),
    (r"stations of the cross",                 +0.7, +0.6, 1.5),
    (r"\brequiem\b",                           +0.6, +0.6, 1.2),
    (r"confession|sacrament of reconciliation", +0.7, +0.4, 1.5),
    (r"\bchrism\b|holy oils",                  +0.6, +0.5, 1.0),

    # -- ceremonial furniture and roles ---------------------------------------
    (r"\bincense\b|\bthurifer\b|\bthurible\b", +0.5, +1.0, 2.0),
    (r"\bchasuble\b|\bcope\b|\bbiretta\b|\bcassock\b", +0.6, +0.9, 1.5),
    (r"\bvestment",                            +0.3, +0.7, 1.2),
    (r"\bacolyte|\bserver'?s? guild|\bcrucifer\b", +0.3, +0.7, 1.2),
    (r"anglican missal|english missal",        +0.8, +0.8, 2.0),
    (r"\bplainsong\b|\bgregorian chant\b",     +0.5, +0.9, 1.5),
    (r"\bprocession(?:al)?\b",                  0.0, +0.5, 0.8),
    (r"\bsanctus bell|\bgenuflect",            +0.7, +0.8, 1.5),

    # -- Prayer Book: traditional, but says little about theology --------------
    # A bare year cannot stand in for the Prayer Book. Across the same 384
    # articles only 28% of "1662"/"1928" mentions sat anywhere near prayer-book
    # words; the rest were ordinary dates -- a hall built in 1928, a parish
    # founded in 1662. The year now has to name what it is a year of.
    (r"book of common prayer|\bbcp\b"
     r"|\b(?:1662|1928|1979)\s+(?:prayer ?book|liturgy|rite|missal)"
     r"|(?:prayer ?book)\s+of\s+(?:1662|1928|1979)", +0.1, +0.5, 1.0),
    (r"\btraditional language\b|\bthee and thou\b", +0.1, +0.6, 1.0),

    # -- evangelical / low-church markers -------------------------------------
    (r"expository preaching|\bbible teaching\b|verse[- ]by[- ]verse", -0.8, -0.6, 2.0),
    (r"thirty[- ]nine articles|\b39 articles\b", -0.7, -0.1, 1.5),
    (r"\balpha course\b",                      -0.5, -0.5, 1.2),
    (r"worship band|worship team|contemporary worship", -0.4, -0.9, 1.8),
    (r"praise (?:and|&) worship",              -0.4, -0.8, 1.5),
    (r"small groups?|home groups?|life groups?|\bcell groups?\b", -0.4, -0.5, 1.2),
    # "Gospel-centred" is evangelical branding. Bare "the gospel" is not: every
    # tradition preaches it, and in a parish history it is what missionaries
    # brought to the frontier. It matched 16 of the 384 articles and separated
    # nothing, so only the compound survives.
    (r"gospel[- ]cent(?:re|er)ed", -0.6, -0.4, 1.2),
    (r"\baltar call\b|\bcome to (?:christ|faith)\b", -0.7, -0.7, 1.5),
    (r"\bchurch plant(?:ing)?\b",              -0.5, -0.6, 1.2),
    (r"\binformal\b|\bcasual\b|\bcome as you are\b", -0.2, -0.8, 1.2),
    (r"\bsermon series\b",                     -0.4, -0.4, 1.0),
    (r"reformed episcopal",                    -0.9, -0.3, 2.0),
    (r"\bcharismatic\b|spirit[- ]filled|\brenewal\b", -0.2, -0.5, 1.0),
]

_COMPILED = [(re.compile(pattern, re.IGNORECASE), theology, ceremonial, strength)
             for pattern, theology, ceremonial, strength in LEXICON]

# A single phrase is not much to go on. Confidence approaches 1 as the matched
# strength accumulates; this is the scale, not a threshold.
CONFIDENCE_SCALE = 8.0
MAX_SCRAPED_CONFIDENCE = 0.65   # a website is evidence, not testimony

# Not every source is worth the same. A parish website is the parish describing
# its own worship now. A Wikipedia article is usually about a *building* -- its
# architect, its NRHP listing, the fire of 1908 -- written by someone else,
# possibly years ago. It is real evidence and it reaches parishes with no
# website at all, but it is weaker and it is more likely to be stale, so it
# carries its own lower ceiling.
SOURCE_CAPS = {
    "website": 0.65,
    "wikipedia": 0.45,
}

# How much a scraped estimate counts against user submissions, in units of
# votes. At full confidence the prior is worth two votes: a third real
# submission outweighs it, which is the intent.
PRIOR_VOTES = 2.0

LABELS = {
    "ceremonial": [
        (-1.01, -0.55, "Very low church"),
        (-0.55, -0.2, "Low church"),
        (-0.2, 0.2, "Middle"),
        (0.2, 0.55, "High church"),
        (0.55, 1.01, "Very high church"),
    ],
    "theology": [
        (-1.01, -0.55, "Strongly evangelical"),
        (-0.55, -0.2, "Evangelical"),
        (-0.2, 0.2, "Central"),
        (0.2, 0.55, "Catholic-leaning"),
        (0.55, 1.01, "Anglo-Catholic"),
    ],
}


def label(axis, value):
    if value is None:
        return "Unknown"
    for low, high, name in LABELS[axis]:
        if low <= value < high:
            return name
    return "Unknown"


def score(text, source="website"):
    """Score a blob of text. Returns None when nothing at all matched.

    Returning None matters: a parish we know nothing about must show as unknown,
    not as a confident "middle". Zero and no-data are different answers.

    `source` picks the confidence ceiling from SOURCE_CAPS and is recorded on
    the result, so the UI can say where a reading came from.
    """
    if not text:
        return None

    theology_sum = ceremonial_sum = weight_sum = 0.0
    matched = []

    for pattern, theology, ceremonial, strength in _COMPILED:
        found = pattern.search(text)
        if not found:
            continue
        # Each phrase counts once however often it appears -- a page that says
        # "mass" forty times is one parish, not forty pieces of evidence.
        theology_sum += theology * strength
        ceremonial_sum += ceremonial * strength
        weight_sum += strength
        matched.append(found.group(0).lower())

    if not matched:
        return None

    cap = SOURCE_CAPS.get(source, MAX_SCRAPED_CONFIDENCE)
    confidence = min(weight_sum / CONFIDENCE_SCALE, 1.0) * cap

    return {
        "theology": _clamp(theology_sum / weight_sum),
        "ceremonial": _clamp(ceremonial_sum / weight_sum),
        "confidence": round(confidence, 3),
        "source": source,
        "matched": sorted(set(matched))[:12],
    }


def _clamp(value):
    return round(max(-1.0, min(1.0, value)), 3)


def merge_sources(scores):
    """Combine per-source estimates for one parish into a single reading.

    Two independent sources that agree are worth more than either alone, and two
    that disagree are worth less -- the same principle as `blend` applies to
    votes. A parish website calling itself Anglo-Catholic while its Wikipedia
    article describes a plain preaching box is a parish that changed, or a
    source that is wrong, and either way the meter should hedge.

    The combined confidence can never exceed the best cap among the contributing
    sources, so adding a weak source can sharpen a reading but cannot manufacture
    certainty the evidence does not support.
    """
    scores = [s for s in scores if s]
    if not scores:
        return None
    if len(scores) == 1:
        return dict(scores[0])

    weights = [max(s["confidence"], 1e-6) for s in scores]
    total = sum(weights)

    def axis(name):
        return _clamp(sum(s[name] * w for s, w in zip(scores, weights)) / total)

    # Distance between the two furthest-apart readings, over the 2-D box whose
    # diagonal is 2*sqrt(2). 0 is perfect agreement, 1 is opposite corners.
    spread = 0.0
    for i, a in enumerate(scores):
        for b in scores[i + 1:]:
            distance = ((a["ceremonial"] - b["ceremonial"]) ** 2
                        + (a["theology"] - b["theology"]) ** 2) ** 0.5
            spread = max(spread, distance / (2 * 2 ** 0.5))

    best = max(s["confidence"] for s in scores)
    cap = max(SOURCE_CAPS.get(s.get("source", "website"), MAX_SCRAPED_CONFIDENCE)
              for s in scores)
    # Agreement adds up to a third again; disagreement takes away up to half.
    confidence = best * (1.0 + 0.33 * (1 - spread) - 0.5 * spread)

    matched = []
    for s in scores:
        for phrase in s.get("matched", []):
            if phrase not in matched:
                matched.append(phrase)

    return {
        "ceremonial": axis("ceremonial"),
        "theology": axis("theology"),
        "confidence": round(min(max(confidence, 0.0), cap), 3),
        "source": "+".join(sorted({s.get("source", "website") for s in scores})),
        "matched": sorted(matched)[:12],
    }


def blend(scraped, votes):
    """Combine a scraped estimate with user submissions.

    Shrinkage, not replacement: the scraped estimate acts as a prior worth up to
    PRIOR_VOTES votes, scaled by its own confidence. Three real submissions
    outweigh even a confident scrape, which is the point of asking people.

    `votes` is a list of (ceremonial, theology) in -1..+1.
    """
    votes = list(votes or [])
    prior_weight = (scraped["confidence"] / MAX_SCRAPED_CONFIDENCE * PRIOR_VOTES) if scraped else 0.0

    if not votes and not scraped:
        return None

    def axis(name, index):
        total = sum(vote[index] for vote in votes)
        weight = len(votes) + prior_weight
        if scraped:
            total += scraped[name] * prior_weight
        return _clamp(total / weight) if weight else None

    # Confidence rises with agreement, not just count: five submissions that
    # contradict each other describe a parish people read differently, and the
    # meter should say so rather than average them into a confident middle.
    scraped_confidence = scraped["confidence"] if scraped else 0.0
    if votes:
        spread = _spread(votes)
        vote_confidence = min(len(votes) / 5.0, 1.0) * (1.0 - spread * 0.6)
        # Votes take over as the confidence signal as they accumulate, rather
        # than only ever raising it. Three people who flatly contradict each
        # other describe a parish that is genuinely hard to place, and the meter
        # has to get *less* sure -- taking the max would let a confident scrape
        # paper over exactly the disagreement worth surfacing.
        takeover = min(len(votes) / 3.0, 1.0)
        confidence = scraped_confidence * (1 - takeover) + vote_confidence * takeover
    else:
        confidence = scraped_confidence

    return {
        "ceremonial": axis("ceremonial", 0),
        "theology": axis("theology", 1),
        "confidence": round(min(confidence, 1.0), 3),
        "votes": len(votes),
        "source": "community" if len(votes) >= 3 else ("blended" if votes else "estimated"),
    }


def _spread(votes):
    """Mean absolute deviation across both axes, normalised to 0..1."""
    if len(votes) < 2:
        return 0.0
    deviations = []
    for index in (0, 1):
        values = [vote[index] for vote in votes]
        mean = sum(values) / len(values)
        deviations.append(sum(abs(value - mean) for value in values) / len(values))
    return min(sum(deviations) / len(deviations), 1.0)
