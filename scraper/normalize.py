"""Turn raw OpenStreetMap tags into the flat records the site consumes.

OSM tagging is free-form, so most of this file is the mapping from the messy
`denomination=*` values people actually type into a short list of families the
UI can offer as filter chips.
"""

import re

from states import STATE_NAMES

_CODES = set(STATE_NAMES)
_NAMES_TO_CODE = {name.lower(): code for code, name in STATE_NAMES.items()}


def canonical_state(raw, fallback):
    """Return a two-letter code for a free-text `addr:state`.

    OSM has "Ohio", "Tx", "tx", "W. Va." and "-IL" in this field. Anything that
    does not resolve falls back to the state whose boundary the Overpass query
    matched, which is authoritative -- an area query cannot be wrong about which
    state contains a point, whereas a hand-typed tag can.
    """
    if not raw:
        return fallback
    letters = re.sub(r"[^A-Za-z]", "", raw).upper()
    if letters in _CODES:
        return letters
    name = re.sub(r"[^a-z ]", "", raw.strip().lower()).strip()
    return _NAMES_TO_CODE.get(name, fallback)


# OSM denomination value -> (display label, family). The family is what the site
# filters on; the label is what it prints. Values not listed here fall through to
# `_titleize` with family "other", so an unmapped denomination still renders.
DENOMINATIONS = {
    "roman_catholic": ("Roman Catholic", "catholic"),
    "catholic": ("Catholic", "catholic"),
    "greek_catholic": ("Greek Catholic", "catholic"),
    "ukrainian_greek_catholic": ("Ukrainian Greek Catholic", "catholic"),
    "old_catholic": ("Old Catholic", "catholic"),
    "byzantine_catholic": ("Byzantine Catholic", "catholic"),
    "maronite": ("Maronite", "catholic"),

    "baptist": ("Baptist", "baptist"),
    "southern_baptist": ("Southern Baptist", "baptist"),
    "independent_baptist": ("Independent Baptist", "baptist"),
    "missionary_baptist": ("Missionary Baptist", "baptist"),
    "free_will_baptist": ("Free Will Baptist", "baptist"),
    "primitive_baptist": ("Primitive Baptist", "baptist"),
    "american_baptist": ("American Baptist", "baptist"),
    "national_baptist": ("National Baptist", "baptist"),

    "methodist": ("Methodist", "methodist"),
    "united_methodist": ("United Methodist", "methodist"),
    "african_methodist_episcopal": ("African Methodist Episcopal", "methodist"),
    "african_methodist_episcopal_zion": ("AME Zion", "methodist"),
    "free_methodist": ("Free Methodist", "methodist"),
    "wesleyan": ("Wesleyan", "methodist"),
    "nazarene": ("Church of the Nazarene", "methodist"),

    "lutheran": ("Lutheran", "lutheran"),
    "evangelical_lutheran": ("Evangelical Lutheran", "lutheran"),
    "missouri_synod": ("Lutheran (Missouri Synod)", "lutheran"),
    "wisconsin_synod": ("Lutheran (Wisconsin Synod)", "lutheran"),

    "presbyterian": ("Presbyterian", "presbyterian"),
    "reformed": ("Reformed", "presbyterian"),
    "christian_reformed": ("Christian Reformed", "presbyterian"),
    "dutch_reformed": ("Dutch Reformed", "presbyterian"),
    "congregational": ("Congregational", "presbyterian"),
    "united_church_of_christ": ("United Church of Christ", "presbyterian"),

    "anglican": ("Anglican", "anglican"),
    "episcopal": ("Episcopal", "anglican"),
    "episcopalian": ("Episcopal", "anglican"),

    "orthodox": ("Orthodox", "orthodox"),
    "greek_orthodox": ("Greek Orthodox", "orthodox"),
    "russian_orthodox": ("Russian Orthodox", "orthodox"),
    "serbian_orthodox": ("Serbian Orthodox", "orthodox"),
    "romanian_orthodox": ("Romanian Orthodox", "orthodox"),
    "antiochian_orthodox": ("Antiochian Orthodox", "orthodox"),
    "coptic_orthodox": ("Coptic Orthodox", "orthodox"),
    "ethiopian_orthodox": ("Ethiopian Orthodox", "orthodox"),
    "oriental_orthodox": ("Oriental Orthodox", "orthodox"),
    "armenian_apostolic": ("Armenian Apostolic", "orthodox"),

    "pentecostal": ("Pentecostal", "pentecostal"),
    "assemblies_of_god": ("Assemblies of God", "pentecostal"),
    "assembly_of_god": ("Assemblies of God", "pentecostal"),
    "church_of_god": ("Church of God", "pentecostal"),
    "church_of_god_in_christ": ("Church of God in Christ", "pentecostal"),
    "foursquare": ("Foursquare", "pentecostal"),
    "apostolic": ("Apostolic", "pentecostal"),
    "charismatic": ("Charismatic", "pentecostal"),
    "full_gospel": ("Full Gospel", "pentecostal"),

    "nondenominational": ("Non-denominational", "nondenominational"),
    "non-denominational": ("Non-denominational", "nondenominational"),
    "evangelical": ("Evangelical", "nondenominational"),
    "community": ("Community Church", "nondenominational"),
    "bible": ("Bible Church", "nondenominational"),
    "interdenominational": ("Interdenominational", "nondenominational"),

    "church_of_christ": ("Church of Christ", "restorationist"),
    "disciples_of_christ": ("Disciples of Christ", "restorationist"),
    "christian": ("Christian Church", "restorationist"),
    "mormon": ("Latter-day Saints", "restorationist"),
    "latter_day_saints": ("Latter-day Saints", "restorationist"),
    "jehovahs_witness": ("Jehovah's Witnesses", "restorationist"),
    "seventh_day_adventist": ("Seventh-day Adventist", "restorationist"),
    "adventist": ("Adventist", "restorationist"),

    "quaker": ("Quaker", "peace"),
    "mennonite": ("Mennonite", "peace"),
    "amish": ("Amish", "peace"),
    "brethren": ("Brethren", "peace"),
    "moravian": ("Moravian", "peace"),
    "salvation_army": ("Salvation Army", "peace"),
    "unitarian_universalist": ("Unitarian Universalist", "peace"),
    "unitarian": ("Unitarian", "peace"),
}

FAMILY_LABELS = {
    "catholic": "Catholic",
    "baptist": "Baptist",
    "methodist": "Methodist & Wesleyan",
    "lutheran": "Lutheran",
    "presbyterian": "Presbyterian & Reformed",
    "anglican": "Anglican & Episcopal",
    "orthodox": "Orthodox",
    "pentecostal": "Pentecostal & Charismatic",
    "nondenominational": "Non-denominational",
    "restorationist": "Restorationist",
    "peace": "Anabaptist & Peace Churches",
    "other": "Other",
    "unknown": "Unspecified",
}

# Tags checked in order; first non-empty wins.
WEBSITE_TAGS = ("website", "contact:website", "url", "website:official")
PHONE_TAGS = ("phone", "contact:phone", "telephone", "contact:mobile")
EMAIL_TAGS = ("email", "contact:email")


def _titleize(value):
    """`greek_orthodox` -> `Greek Orthodox`, for denominations we have no map for."""
    cleaned = re.sub(r"[_\-]+", " ", value).strip()
    return " ".join(word.capitalize() for word in cleaned.split())


def classify_denomination(raw):
    """Return (label, family) for a raw OSM denomination value."""
    if not raw:
        return ("", "unknown")
    key = raw.strip().lower().replace(" ", "_")
    if key in DENOMINATIONS:
        return DENOMINATIONS[key]
    # Multi-value tags look like `baptist;pentecostal` -- take the first we know.
    for part in re.split(r"[;,]", key):
        part = part.strip()
        if part in DENOMINATIONS:
            return DENOMINATIONS[part]
    return (_titleize(raw), "other")


def _first(tags, keys):
    for key in keys:
        value = tags.get(key, "").strip()
        if value:
            return value
    return ""


def _clean_website(url):
    if not url:
        return ""
    url = url.strip().split(";")[0].strip()
    if not url:
        return ""
    # People type "Http://", "HTTPS://" and bare hostnames; normalize all three.
    match = re.match(r"^(https?)://+(.*)$", url, flags=re.IGNORECASE)
    if match:
        url = f"{match.group(1).lower()}://{match.group(2)}"
    else:
        url = "https://" + url.lstrip("/")
    # A handful of OSM entries hold prose rather than a URL; drop those.
    if " " in url or "." not in url:
        return ""
    return url


def _clean_phone(phone):
    if not phone:
        return ""
    phone = phone.split(";")[0].strip()
    digits = re.sub(r"[^\d+]", "", phone)
    if len(re.sub(r"\D", "", digits)) < 7:
        return ""
    return phone


def build_address(tags):
    """Assemble a street line from the addr:* tags that happen to be present."""
    number = tags.get("addr:housenumber", "").strip()
    street = tags.get("addr:street", "").strip()
    if number and street:
        return f"{number} {street}"
    return street or number


def normalize(element, state_code):
    """Convert one Overpass element into a record, or None if unusable.

    Anonymous entries are dropped: a church with no name cannot be searched for
    and only adds noise to the map.
    """
    tags = element.get("tags") or {}
    name = tags.get("name", "").strip()
    if not name:
        return None

    if element["type"] == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None

    label, family = classify_denomination(tags.get("denomination", ""))

    return {
        "id": f"{element['type'][0]}{element['id']}",
        "name": name,
        "denomination": label,
        "family": family,
        "address": build_address(tags),
        "city": tags.get("addr:city", "").strip(),
        "state": canonical_state(tags.get("addr:state", ""), state_code),
        "postcode": tags.get("addr:postcode", "").strip(),
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "website": _clean_website(_first(tags, WEBSITE_TAGS)),
        "phone": _clean_phone(_first(tags, PHONE_TAGS)),
        "email": _first(tags, EMAIL_TAGS),
        "services": tags.get("service_times", "").strip(),
        "hours": tags.get("opening_hours", "").strip(),
        "wheelchair": tags.get("wheelchair", "").strip(),
        # Accessibility beyond the wheelchair flag. These are real OSM tags that
        # earlier versions of this scraper threw away.
        "hearing_loop": tags.get("hearing_loop", "").strip(),
        "toilets_wheelchair": tags.get("toilets:wheelchair", "").strip(),
        # Last edit in OSM, so the site can mark records nobody has touched in
        # years rather than presenting every row with equal confidence.
        "updated": (element.get("timestamp") or "")[:10],
        # An exact join to Wikipedia. Matching by name would be guesswork --
        # there are hundreds of St Mary's -- but these tags are set by mappers
        # who looked at the specific building.
        "wikipedia": tags.get("wikipedia", "").strip(),
        "wikidata": tags.get("wikidata", "").strip(),
        "denomination_raw": tags.get("denomination", "").strip(),
    }
