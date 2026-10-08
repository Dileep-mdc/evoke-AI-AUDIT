"""Company Identity Agent: who the audited website belongs to, before any off-page research starts.

Every off-page check searches the web for the organisation, so its name decides what they find.
It used to be one guess -- the og:site_name tag, else the first schema name, else the homepage
title -- taken as written. aaos.org declares "American Academy of Orthopaedic Surgeons AAOS", so
every search looked for that exact phrase and found almost nothing; a site that declares only its
abbreviation, or nothing at all, was searched as "Aaos" from its domain. The short "brand term"
the checks match with was the name's first word -- "american" for the academy, which matches
thousands of unrelated organisations.

This agent works it out the way a researcher would, and records how:

  1. Collect every name the site gives itself, each weighted by how deliberate the source is:
     schema.org legalName / name / alternateName, og:site_name, the copyright line, the homepage
     and About-page titles, and the domain.
  2. Expand an abbreviation from the site's own text: "American Academy of Orthopaedic Surgeons
     (AAOS)" names AAOS.
  3. Rank the candidates, rewarding agreement between sources and a name whose initials or words
     match the domain.
  4. Confirm against Wikidata: search the leading names and accept an entity only when its own
     official website is on the audited domain. A confirmed entity supplies the official name, its
     Wikidata ID and its Wikipedia article, which the knowledge checks (OFF-01, OFF-02) then use
     directly instead of searching by name again.

It never fails a scan: with Wikidata unreachable it returns the best on-site name, marked unconfirmed.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import quote_plus, urlparse

from ..config import REFERENCE_LANGUAGE, WIKIDATA_API
from .common import drop_acronym, flatten_schema
from . import offpage_tools as tools

NAME = "Company Identity Agent"

# How deliberate each source of a name is. A legal name in structured data is a declaration; a
# page title is often a tagline; a domain is the last resort.
SOURCE_WEIGHT = {"schema_legal_name": 6, "schema_name": 5, "og_site_name": 4, "copyright": 4,
                 "schema_alternate_name": 2, "homepage_title": 2, "about_page": 2, "domain": 1}
_ORG_TYPE = re.compile(r"organization|organisation|corporation|company|business|ngo|store|hospital|clinic|"
                       r"university|college|school|bank|agency|association|institution", re.I)
_GENERIC = {"home", "homepage", "home page", "welcome", "official site", "official website", "index", "main", "untitled"}
# Joining words skipped when forming initials, and words too common to identify a brand on their own.
_GLUE = {"of", "the", "and", "for", "in", "on", "at", "to", "de", "du", "la", "&"}
_COMMON_WORDS = _GLUE | {"american", "national", "international", "global", "united", "general", "first", "new",
                         "world", "group", "company", "services", "solutions", "technologies", "systems", "association",
                         "society", "institute", "academy", "college", "university", "bank", "inc", "ltd", "plc", "llc"}
_COPYRIGHT = re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*[,.]?\s*"
                        r"([A-Z][\w&.,' -]{2,90}?)(?=\s*(?:\.|,|\||•|-|–|all rights|\d{4}|$))", re.I)
_TITLE_SPLIT = re.compile(r"\s+[|\-–—:•]\s+")


def _host(url: str) -> str:
    return (urlparse(url if "//" in url else f"//{url}").netloc or "").lower().removeprefix("www.")


def _stem(domain: str) -> str:
    """The registrable label of a domain: www.aaos.org -> aaos, shop.nvent.co.uk -> nvent."""
    parts = [p for p in _host(domain).split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"}:
        parts = parts[:-2]
    elif len(parts) >= 2:
        parts = parts[:-1]
    return parts[-1] if parts else ""


def initials(name: str, skip_glue: bool = True) -> str:
    words = re.findall(r"[A-Za-z][\w'-]*", name or "")
    return "".join(w[0] for w in words if not (skip_glue and w.lower() in _GLUE)).upper()


def _is_acronym(text: str) -> bool:
    t = (text or "").strip()
    return 2 <= len(t) <= 8 and " " not in t and (t.isupper() or t.lower() == t)


def _clean(text: str) -> str:
    # Invisible formatting characters (bidi marks, zero-width spaces) ride along in copied names.
    text = "".join(ch for ch in (text or "") if unicodedata.category(ch) != "Cf")
    text = re.sub(r"\s+", " ", text).strip(" -|:•,\u00a0")
    # A sentence's full stop goes; an abbreviation's stays: "Apple Inc." keeps it, "Acme Corp" too.
    if text.endswith(".") and not re.search(r"\b(?:inc|co|corp|ltd|llc|plc|s\.a|n\.v|b\.v|ag|gmbh)\.$", text, re.I):
        text = text.rstrip(".").rstrip()
    return text


def _candidates(ctx) -> list[dict]:
    """Every name the site gives itself, with its source."""
    found: list[dict] = []

    def add(name, source):
        name = _clean(name)
        if not name or name.lower() in _GENERIC or len(name) > 90 or len(name.split()) > 10:
            return
        found.append({"name": name, "source": source})

    home = getattr(ctx, "homepage", None)
    about = next((p for p in getattr(ctx, "pages", None) or [] if getattr(p, "page_type", "") == "about"), None)
    for page in filter(None, (home, about)):
        for item in flatten_schema(page.schema_blocks or []):
            if not _ORG_TYPE.search(str(item.get("@type"))):
                continue
            if isinstance(item.get("legalName"), str):
                add(item["legalName"], "schema_legal_name")
            if isinstance(item.get("name"), str):
                add(item["name"], "schema_name")
            alt = item.get("alternateName")
            for a in (alt if isinstance(alt, list) else [alt]):
                if isinstance(a, str):
                    add(a, "schema_alternate_name")
    if home is not None and getattr(home, "soup", None) is not None:
        og = home.soup.find("meta", attrs={"property": "og:site_name"})
        if og and og.get("content"):
            add(og["content"], "og_site_name")
        footer = home.soup.find("footer")
        text = footer.get_text(" ", strip=True) if footer else home.soup.get_text(" ", strip=True)[-3000:]
        m = _COPYRIGHT.search(text)
        if m:
            # "© 2026 ... published by the American Academy ...": the name starts after "by (the)".
            add(re.sub(r"^.*?\bby\s+(?:the\s+)?", "", m.group(1), flags=re.I), "copyright")
    if home is not None and getattr(home, "title", ""):
        for part in _TITLE_SPLIT.split(home.title):
            add(part, "homepage_title")
    if about is not None and getattr(about, "title", ""):
        for part in _TITLE_SPLIT.split(about.title):
            add(re.sub(r"^about( us)?\s*[-:|]?\s*", "", part, flags=re.I), "about_page")
    stem = _stem(getattr(ctx, "domain", "") or getattr(ctx, "origin", ""))
    if stem:
        add(stem.upper() if len(stem) <= 5 else stem.title(), "domain")
    return found


def _expand(acronym: str, text: str) -> Optional[str]:
    """The full name the site writes next to an abbreviation: "Full Name (ACR)" or "ACR (Full Name)"."""
    acr = re.escape(acronym)
    for m in re.finditer(rf"((?:[A-Z][\w'&-]*\s+(?:of|the|and|for|&|de)?\s*){{2,9}})\(\s*{acr}\s*\)", text):
        name = _clean(m.group(1))
        # The run of capitalised words can start too early ("The American Society ... (ASCE)"):
        # drop leading words until the initials are the abbreviation's.
        words = name.split()
        for start in range(len(words) - 1):
            if words[start].lower() in _GLUE:
                continue                    # "The" is not part of AAOS or ASCE
            candidate = " ".join(words[start:])
            if initials(candidate) == acronym.upper() or initials(candidate, skip_glue=False) == acronym.upper():
                return candidate
    m = re.search(rf"\b{acr}\s*\(\s*([A-Z][^()]{{5,90}})\)", text)
    if m and initials(m.group(1)) == acronym.upper():
        return _clean(m.group(1))
    return None


def _rank(cands: list[dict], stem: str) -> list[dict]:
    """Group candidates by the name they denote and score each group."""
    groups: dict[str, dict] = {}
    for c in cands:
        key = tools._core_name(c["name"])
        if not key:
            continue
        g = groups.setdefault(key, {"key": key, "forms": [], "sources": [], "score": 0.0})
        g["forms"].append(c["name"])
        if c["source"] not in g["sources"]:
            g["sources"].append(c["source"])
            g["score"] += SOURCE_WEIGHT.get(c["source"], 1)
    # The same name written short and in full ("nVent" and "nVent Electric plc"): the declared full
    # name collects the short form's support, since it is what directories and Wikipedia list.
    deliberate = {"schema_legal_name", "schema_name", "og_site_name", "copyright"}
    for short in list(groups.values()):
        fuller = [g for g in groups.values() if g is not short and g["key"].startswith(short["key"] + " ")
                  and deliberate & set(g["sources"])]
        if fuller:
            target = max(fuller, key=lambda g: g["score"])
            target["score"] += short["score"]
            target["sources"] += [s for s in short["sources"] if s not in target["sources"]]
            del groups[short["key"]]
    for g in groups.values():
        compact = re.sub(r"[^a-z0-9]", "", g["key"])
        if stem and (initials(g["forms"][0]) == stem.upper() or stem in compact or compact in stem):
            g["score"] += 3            # agrees with the domain: aaos <- American Academy of Orthopaedic Surgeons
        if _is_acronym(g["forms"][0]):
            g["score"] -= 1            # an abbreviation is a fallback for the full name, not the name itself
        if len(compact) <= 2:
            g["score"] -= 5
    return sorted(groups.values(), key=lambda g: -g["score"])


def _display(group: dict) -> str:
    """The best-written form of a name: declared casing, legal form kept, any abbreviation dropped."""
    forms = sorted(group["forms"], key=lambda f: (f.isupper() or f.islower(), -len(f)))
    return drop_acronym(forms[0])


def brand_term(name: str, acronym: Optional[str], stem: str) -> str:
    """The short, distinctive term the checks match the organisation by -- never a common word."""
    if acronym:
        return acronym.lower()
    all_words = re.findall(r"[a-z0-9][\w'-]*", (name or "").lower())
    words = [w for w in all_words if w not in _COMMON_WORDS and len(w) > 1]
    # The words the domain is spelled from: redcross.org -> "red cross". The first of them alone is
    # enough when it is distinctive (evoketechnologies.com -> "evoke"); a short or common one is not.
    for i in range(len(all_words)):
        for j in range(i + 1, len(all_words) + 1):
            if stem and "".join(all_words[i:j]) == stem:
                first = all_words[i]
                if j - i > 1 and (len(first) < 4 or first in _COMMON_WORDS):
                    return " ".join(all_words[i:j])
                return first
    compact = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if stem and stem in compact:
        return stem
    if words:
        return words[0]
    return stem or (name or "").split()[0].lower()


async def _confirm(names: list[str], host: str, ctx) -> Optional[dict]:
    """The Wikidata entity among the name matches whose official website is the audited domain."""
    stem = _stem(host)
    ids: list[str] = []
    for name in names:
        data, _ = await tools._json(tools.wikidata_search_url(name, limit=7), ctx)
        for hit in (data or {}).get("search") or []:
            if hit.get("id") and hit["id"] not in ids:
                ids.append(hit["id"])
    if not ids:
        return None
    url = (f"{WIKIDATA_API}?action=wbgetentities&ids={'|'.join(ids[:40])}&props=labels|descriptions|aliases|claims|sitelinks"
           f"&languages={REFERENCE_LANGUAGE}&sitefilter={REFERENCE_LANGUAGE}wiki&format=json")
    data, _ = await tools._json(url, ctx)
    lang = REFERENCE_LANGUAGE
    wanted = {tools._core_name(n) for n in names if n}
    matches = []
    for rank, qid in enumerate(ids):
        ent = ((data or {}).get("entities") or {}).get(qid) or {}
        sites = [s.get("mainsnak", {}).get("datavalue", {}).get("value") for s in ent.get("claims", {}).get("P856", [])]
        # The audited domain itself only: a subdomain belongs to a sub-brand (schroff.nvent.com is
        # Schroff, an nVent brand), and taking it would audit nVent under Schroff's name.
        on_domain = [s for s in sites if isinstance(s, str) and _host(s) == host]
        if not on_domain:
            continue
        label = ent.get("labels", {}).get(lang, {}).get("value") or ""
        aliases = [a["value"] for a in ent.get("aliases", {}).get(lang, [])]
        # Several entities can share one website -- ups.com is United Parcel Service and UPS Airlines.
        # The one the site's own name IS (label, else an alias) wins; a mere mention ranks last.
        exact = (2 if tools._core_name(label) in wanted else 1 if any(tools._core_name(a) in wanted for a in aliases) else 0)
        # The organisation whose official website IS the site (the home page) beats one that only owns
        # a section of it: United Parcel Service lists ups.com/, UPS Airlines lists ups.com/aircargo/.
        root = any(urlparse(s if "//" in s else f"//{s}").path.strip("/") in ("", "en", "en-us", "us", "home", "home.asp", "index.html")
                   for s in on_domain)
        mentions = any(w and w in tools._core_name(label) for w in wanted)
        # A website match also needs the entity's name to relate to the site: its exact name or an
        # alias, or a name the domain abbreviates or contains (ups <- United Parcel Service).
        names_of = [label] + aliases
        related = exact or any(initials(n) == stem.upper() or (stem and stem in re.sub(r"[^a-z0-9]", "", n.lower()))
                               for n in names_of if n)
        if not related:
            continue
        matches.append((-exact, -int(root), -int(mentions), -len(ent.get("sitelinks", {})), rank, {
            "id": qid, "label": _clean(label), "description": ent.get("descriptions", {}).get(lang, {}).get("value"),
            "aliases": aliases[:8], "wikipedia_title": ent.get("sitelinks", {}).get(f"{lang}wiki", {}).get("title"),
            "official_website": on_domain[0]}))
    return min(matches, key=lambda m: m[:5])[5] if matches else None


async def resolve(ctx) -> dict:
    """The organisation behind the site: name, short name, abbreviation, brand term, and how sure."""
    host = _host(getattr(ctx, "domain", "") or getattr(ctx, "origin", ""))
    stem = _stem(host)
    cands = _candidates(ctx)
    ranked = _rank(cands, stem)
    best = ranked[0] if ranked else {"forms": [stem.title() or host], "sources": ["domain"], "score": 0, "key": stem}
    name = _display(best)

    # An abbreviation as the best name: expand it from the site's own text.
    acronym = None
    site_text = " ".join(filter(None, [getattr(getattr(ctx, "homepage", None), "text", "")] +
                                [getattr(p, "text", "") for p in (getattr(ctx, "pages", None) or [])[:30]]))
    for form in best["forms"] + ([stem.upper()] if stem else []):
        if _is_acronym(form):
            full = _expand(form.upper(), site_text)
            if full:
                acronym, name = form.upper(), full
                break
    if acronym is None:
        for cand in cands:          # "American Academy of Orthopaedic Surgeons AAOS": keep the AAOS
            dropped = drop_acronym(cand["name"])
            if dropped != _clean(cand["name"]) and tools._core_name(dropped) == tools._core_name(name):
                acronym = _clean(cand["name"])[len(dropped):].strip(" ()|-:")[:8].upper() or None
                break
    if acronym is None and stem and initials(name) == stem.upper() and len(stem) <= 6:
        acronym = stem.upper()

    entity = None
    try:
        # The domain's own name is searched too: bosch.com calls itself "Bosch Global", while its
        # Wikidata entity is "Robert Bosch GmbH", found by "bosch".
        searched = [name, tools.short_name(name)] + ([acronym] if acronym else []) + ([stem] if stem else [])
        entity = await _confirm(list(dict.fromkeys(n for n in searched if n)), host, ctx)
    except Exception:
        entity = None
    source = "+".join(best["sources"])
    if entity and entity.get("label"):
        name = entity["label"]
        source = f"Wikidata (official website {host}) + {source}"
        if acronym is None:
            # The confirmed name may be one the site only abbreviates: who.int -> World Health Organization.
            short_alias = next((a for a in entity.get("aliases", []) if _is_acronym(a) and a.isupper()
                                and initials(name) == a.replace(".", "")), None)
            if short_alias or (stem and initials(name) == stem.upper() and len(stem) <= 6):
                acronym = short_alias or stem.upper()

    short = tools.short_name(name)
    aliases = list(dict.fromkeys([a for a in [short, acronym] + (entity or {}).get("aliases", []) +
                                  [f for g in ranked[:3] for f in g["forms"]] if a and a != name]))[:10]
    confidence = 0.95 if entity else min(0.85, 0.35 + 0.08 * best.get("score", 0))
    return {
        "agent": NAME,
        "name": name,
        "short_name": short,
        "acronym": acronym,
        "brand": brand_term(name, acronym, stem),
        "aliases": aliases,
        "wikidata_id": (entity or {}).get("id"),
        "wikidata_description": (entity or {}).get("description"),
        "wikipedia_title": (entity or {}).get("wikipedia_title"),
        "confirmed": bool(entity),
        "source": source,
        "confidence": round(confidence, 2),
        "candidates": [{"name": _display(g), "sources": g["sources"], "score": g["score"]} for g in ranked[:6]],
    }


def wikidata_page(qid: str) -> str:
    return f"https://{urlparse(WIKIDATA_API).netloc}/wiki/{qid}"


def wikipedia_page(title: str) -> str:
    from ..config import WIKIPEDIA_API
    return f"https://{urlparse(WIKIPEDIA_API).netloc}/wiki/{quote_plus(title.replace(' ', '_'), safe='_()')}"
