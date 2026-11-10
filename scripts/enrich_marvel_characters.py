#!/usr/bin/env python3
"""Bulk-import character profile facts from Marvel Database (Fandom).

The archive is split into site-data/part-NNN.txt files. Each execution imports
one persistent batch and writes a compact JSON shard. Biography lead text and
history sections are extracted from page wikitext when API extracts are absent.
GitHub Actions resumes from progress.json until the complete index is rebuilt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "site-data" / "character-enrichment"
PROGRESS = DATA_DIR / "progress.json"
API = "https://marvel.fandom.com/api.php"
USER_AGENT = "MarvelUniverseArchiveResearch/1.0 (public character archive; contact via repository)"
PAGE_BATCH = 20

FIELD_MAP = {
    "real_name": ("RealName", "Real Name"),
    "current_alias": ("CurrentAlias",),
    "aliases": ("Codenames", "Aliases", "EditorialNames", "Nicknames", "Titles"),
    "affiliations": ("Affiliation",),
    "family": ("Pets", "PetOf", "Ancestors", "Grandparents", "Parents", "Clones", "Siblings", "Spouses", "Children", "Descendants", "InLaw", "Relatives", "Hosts", "HostOf"),
    "marital_status": ("MaritalStatus",),
    "publisher": ("Publisher", "Marvel"),
    "gender": ("Gender",),
    "height": ("Height",),
    "weight": ("Weight",),
    "eyes": ("Eyes",),
    "hair": ("Hair",),
    "unusual_features": ("UnusualFeatures",),
    "origin": ("Origin",),
    "reality": ("Reality",),
    "place_of_birth": ("PlaceOfBirth",),
    "identity": ("Identity",),
    "citizenship": ("Citizenship",),
    "occupation": ("Occupation",),
    "education": ("Education",),
    "base_of_operations": ("BaseOfOperations",),
    "creators": ("Creators",),
    "first_appearance": ("First",),
    "history": ("History",),
    "personality": ("Personality",),
    "powers": ("Powers",),
    "abilities": ("Abilities",),
    "weaknesses": ("Weaknesses",),
    "additional_attributes": ("AdditionalAttributes",),
    "equipment": ("Equipment",),
    "transportation": ("Transportation",),
    "weapons": ("Weapons",),
    "notes": ("Notes",),
    "trivia": ("Trivia",),
}

DISPLAY_FIELDS = {
    "real_name": "Real name",
    "current_alias": "Current alias",
    "aliases": "Aliases",
    "creators": "Creators",
    "first_appearance": "First appearance",
    "occupation": "Occupation",
    "species": "Species / origin",
    "gender": "Gender",
    "height": "Height",
    "weight": "Weight",
    "eyes": "Eye colour",
    "hair": "Hair",
    "unusual_features": "Physical features",
    "identity": "Identity",
    "citizenship": "Citizenship",
    "education": "Education",
    "base_of_operations": "Base of operations",
    "affiliations": "Affiliations",
    "history": "History",
    "personality": "Personality",
    "powers": "Powers",
    "abilities": "Abilities",
    "weaknesses": "Weaknesses",
    "additional_attributes": "Additional attributes",
    "equipment": "Equipment",
    "transportation": "Transportation",
    "weapons": "Weapons",
    "notes": "Notes",
    "trivia": "Trivia",
    "appearance_count": "Recorded appearances",
    "first_appearance_year": "First appearance year",
    "alignment": "Alignment",
    "living_status": "Living status",
    "eye_colour_source": "Eye colour (dataset)",
    "hair_colour_source": "Hair colour (dataset)",
    "source_page": "Character page",
}


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).lower()
    value = value.replace("’", "'").replace("'", "")
    value = value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def repair_unescaped_quotes(raw: str) -> str:
    """Repair the one known malformed CSV-derived quote in the embedded JSON."""
    out: list[str] = []
    inside = False
    escaped = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if inside:
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                out.append(ch)
                escaped = True
            elif ch == '"':
                j = i + 1
                while j < len(raw) and raw[j].isspace():
                    j += 1
                if j == len(raw) or raw[j] in ",:}]":
                    out.append(ch)
                    inside = False
                else:
                    out.append('\\"')
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                inside = True
        i += 1
    return "".join(out)


def extract_archive_json() -> dict[str, Any]:
    pieces = []
    for i in range(1, 18):
        path = ROOT / "site-data" / f"part-{i:03d}.txt"
        if not path.exists():
            raise FileNotFoundError(f"Missing archive segment: {path}")
        pieces.append(path.read_text(encoding="utf-8"))

    # The site's loader relocates this appended CSS from the first chunk.
    marker = "/* Directory comfort + stable cinematic backdrop */"
    css_at = pieces[0].rfind(marker)
    if css_at >= 0:
        pieces[0] = pieces[0][:css_at]

    combined = "".join(pieces)
    prefix = "window.MARVEL_ARCHIVE_DATA="
    marker_at = combined.find(prefix)
    if marker_at < 0:
        raise RuntimeError("Could not locate MARVEL_ARCHIVE_DATA in site segments")
    start = combined.find("{", marker_at)
    if start < 0:
        raise RuntimeError("Character database object is malformed")

    depth = 0
    inside = False
    escaped = False
    end = -1
    i = start
    while i < len(combined):
        ch = combined[i]
        if inside:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                j = i + 1
                while j < len(combined) and combined[j].isspace():
                    j += 1
                if j == len(combined) or combined[j] in ",:}]":
                    inside = False
        else:
            if ch == '"':
                inside = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        i += 1
    if end < 0:
        raise RuntimeError("Could not find the end of the embedded character database")
    return json.loads(repair_unescaped_quotes(combined[start:end]))


def clean_wikitext(value: str) -> str:
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
    value = re.sub(r"<ref\b[^>]*>.*?</ref\s*>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<ref\b[^>]*/\s*>", " ", value, flags=re.I)
    value = re.sub(r"<(br|br\s*/|li|/li|p|/p)\s*/?>", "; ", value, flags=re.I)
    value = re.sub(r"\{\{\s*(?:cite|ref|sfn|efn)[^{}]*\}\}", " ", value, flags=re.I)
    value = re.sub(r"\{\{[^{}]*\}\}", " ", value)
    value = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[(?:https?://\S+)\s+([^\]]+)\]", r"\1", value)
    value = re.sub(r"'''?", "", value)
    value = re.sub(r"''", "", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("&nbsp;", " ").replace("&amp;", "&")
    value = re.sub(r"^\s*[*#:;]+\s*", "", value, flags=re.M)
    value = re.sub(r"\s+", " ", value).strip(" ;,\n\t")
    return value[:5000]


def strip_nested_templates(value: str) -> str:
    """Remove nested MediaWiki templates without leaving their inner parameters."""
    out = []
    i = 0
    while i < len(value):
        if value.startswith("{{", i):
            depth = 1
            i += 2
            while i < len(value) and depth:
                if value.startswith("{{", i):
                    depth += 1
                    i += 2
                elif value.startswith("}}", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def extract_section(wikitext: str, headings: tuple[str, ...]) -> str:
    """Extract a named top-level character-history section from wikitext."""
    pattern = re.compile(r"(?im)^==+\\s*([^=]+?)\\s*==+\\s*$")
    matches = list(pattern.finditer(wikitext or ""))
    wanted = {norm(x) for x in headings}
    for index, match in enumerate(matches):
        if norm(match.group(1)) in wanted:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(wikitext)
            body = wikitext[match.end():end]
            body = re.sub(r"(?im)^==+.*?==+\\s*$", " ", body)
            return clean_wikitext(strip_nested_templates(body))
    return ""


def extract_lead_biography(wikitext: str) -> str:
    """Build a readable lead biography from the source page when API extracts are absent."""
    text = strip_nested_templates(wikitext or "")
    text = re.split(r"(?m)^==+\\s*[^=].*?==+\\s*$", text, maxsplit=1)[0]
    text = re.sub(r"(?im)^\\s*\\[\\[Category:[^]]+\\]\\]\\s*$", " ", text)
    text = clean_wikitext(text)
    if len(text) < 80:
        return ""
    return text[:2400].rsplit(" ", 1)[0]


def find_infobox(wikitext: str) -> str:
    candidates = []
    pattern = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9 _:/-]{1,80})")
    for match in pattern.finditer(wikitext):
        template_name = match.group(1).strip().lower()
        template_name = template_name.replace("_", " ")
        if not any(word in template_name for word in (
            "character", "superhero", "super villain", "supervillain",
            "vigilante", "mutant", "marvel character"
        )):
            continue
        depth = 1
        j = match.end()
        while j < len(wikitext) - 1 and depth:
            if wikitext.startswith("{{", j):
                depth += 1
                j += 2
            elif wikitext.startswith("}}", j):
                depth -= 1
                j += 2
            else:
                j += 1
        if depth == 0:
            candidates.append(wikitext[match.start():j])
    if not candidates:
        return ""
    # Prefer the most character-like template containing identity / power fields.
    for block in candidates:
        low = block.lower()
        if "powers" in low or "affiliation" in low or "realname" in low:
            return block
    return candidates[0]


def parse_template_params(block: str) -> dict[str, str]:
    if not block:
        return {}
    # Character template parameters are conventionally written one per line.
    matches = list(re.finditer(
        r"(?m)^\s*\|\s*([A-Za-z][A-Za-z0-9 _-]{0,60})\s*=\s*",
        block
    ))
    params: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else block.rfind("}}")
        if end < match.end():
            continue
        key = re.sub(r"[\s_-]+", "", match.group(1)).lower()
        value = clean_wikitext(block[match.end():end])
        if value:
            params[key] = value
    return params


def split_values(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"(?:\s*;\s*|\s*·\s*|\s*\n+\s*)", value)
    cleaned = []
    seen = set()
    for part in parts:
        item = clean_wikitext(part)
        if not item or len(item) > 240:
            continue
        key = norm(item)
        if key not in seen:
            seen.add(key)
            cleaned.append(item)
        if len(cleaned) >= 30:
            break
    return cleaned


def row_fallback(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    out: dict[str, Any] = {}
    alignment = str(row.get("Alignment") or "").strip()
    alignment = re.sub(r"\s+Characters?$", "", alignment, flags=re.I)
    if alignment and alignment.lower() not in ("unknown", "not available"):
        out["alignment"] = alignment
    sex = str(row.get("SEX") or "").strip()
    sex = re.sub(r"\s+Characters?$", "", sex, flags=re.I)
    if sex:
        out["gender"] = sex
    for source, target in (("EYE", "eye_colour_source"), ("HAIR", "hair_colour_source")):
        value = str(row.get(source) or "").strip()
        value = re.sub(r"\s+(Eyes|Hair)$", "", value, flags=re.I)
        if value:
            out[target] = value
    living = str(row.get("ALIVE") or "").strip()
    living = re.sub(r"\s+Characters?$", "", living, flags=re.I)
    if living:
        out["living_status"] = living
    count = str(row.get("APPEARANCES") or "").strip()
    if count:
        try:
            out["appearance_count"] = int(float(count))
        except ValueError:
            pass
    first = str(row.get("FIRST APPEARANCE") or "").strip()
    if first:
        out["first_appearance"] = first
    year = str(row.get("Year") or "").strip()
    if year:
        try:
            out["first_appearance_year"] = int(float(year))
        except ValueError:
            pass
    identity = str(row.get("ID") or "").strip()
    if identity and "identity" not in out:
        out["identity"] = identity
    return out


def make_record(name: str, page: dict[str, Any] | None, row: dict[str, Any] | None) -> dict[str, Any]:
    fields = row_fallback(row)
    title = name
    bio = ""
    categories: list[str] = []
    page_id = None
    page_url = ""

    if page and not page.get("missing"):
        title = page.get("title") or name
        page_id = page.get("pageid")
        bio = clean_wikitext(page.get("extract") or "")
        categories = [
            clean_wikitext(x.get("title", "").replace("Category:", ""))
            for x in (page.get("categories") or [])
            if x.get("title")
        ]
        page_url = "https://marvel.fandom.com/wiki/" + requests.utils.quote(title.replace(" ", "_"), safe="()'_-")

        revision = (page.get("revisions") or [{}])[0]
        slot = (revision.get("slots") or {}).get("main") or {}
        wikitext = slot.get("content") or slot.get("*") or revision.get("content") or revision.get("*") or ""
        params = parse_template_params(find_infobox(wikitext))
        if not bio:
            bio = extract_lead_biography(wikitext)
        if not params.get("history"):
            history_text = extract_section(wikitext, ("History", "Biography", "Origin"))
            if history_text:
                fields["history"] = history_text[:5000]

        list_fields = {"aliases", "affiliations", "family", "powers", "abilities", "weaknesses",
                       "equipment", "transportation", "weapons"}
        for field, aliases in FIELD_MAP.items():
            collected = []
            for alias in aliases:
                value = params.get(re.sub(r"[\s_-]+", "", alias).lower())
                if value:
                    collected.extend(split_values(value) if field in list_fields else [value])
            if collected:
                if field in list_fields:
                    fields[field] = list(dict.fromkeys(collected))[:30]
                else:
                    fields[field] = collected[0]

        # Story-arc categories give a conservative story index without invented titles.
        storyline_categories = [
            x for x in categories
            if re.search(r"(storyline|storylines|story arc|story arcs)", x, re.I)
        ]
        if storyline_categories:
            fields["major_storylines"] = storyline_categories[:20]

        # The wiki's own category labels provide a useful backup for team memberships.
        if not fields.get("affiliations"):
            team_cats = [
                x for x in categories
                if re.search(r"(members|member|affiliation|team|organization|corps)", x, re.I)
                and not re.search(r"(needing|without|former categories)", x, re.I)
            ]
            if team_cats:
                fields["affiliations"] = team_cats[:20]

        if not fields.get("classification"):
            lower = " ".join(categories).lower()
            if "villain" in lower:
                fields["classification"] = "Villain"
            elif "hero" in lower:
                fields["classification"] = "Hero"
            elif "antihero" in lower or "anti-hero" in lower:
                fields["classification"] = "Antihero"

        if not fields.get("species") and fields.get("origin"):
            fields["species"] = fields["origin"]

    # The source database row is a fallback, not a source of invented powers or trivia.
    row_values = row_fallback(row)
    for key, value in row_values.items():
        fields.setdefault(key, value)

    first_appearance = fields.get("first_appearance", "")
    year = fields.get("first_appearance_year")
    if not year and first_appearance:
        match = re.search(r"\b(?:18|19|20)\d{2}\b", str(first_appearance))
        if match:
            year = int(match.group(0))
            fields["first_appearance_year"] = year

    # Shape the values into the existing archive's character-detail schema.
    def as_list(value: Any) -> list[str]:
        if not value:
            return []
        return value if isinstance(value, list) else split_values(str(value))

    aliases = as_list(fields.get("aliases"))
    current_alias = fields.get("current_alias")
    if current_alias and current_alias not in aliases:
        aliases.insert(0, current_alias)

    origin = fields.get("history") or fields.get("origin") or ""
    if fields.get("real_name") and norm(fields["real_name"]) == norm(name):
        fields.pop("real_name", None)
    affiliations = as_list(fields.get("affiliations"))
    return {
        "name": name,
        "page_title": title,
        "page_id": page_id,
        "real_name": fields.get("real_name") or "",
        "classification": fields.get("classification") or fields.get("alignment") or "",
        "species": fields.get("species") or "",
        "first_appearance": first_appearance or "",
        "first_appearance_year": year or 0,
        "origin": origin,
        "bio": bio,
        "powers": as_list(fields.get("powers")),
        "abilities": as_list(fields.get("abilities")),
        "weapons": as_list(fields.get("weapons")),
        "weaknesses": as_list(fields.get("weaknesses")),
        "aliases": aliases,
        "affiliations": affiliations,
        "major_storylines": fields.get("major_storylines") or [],
        "fields": fields,
        "categories": categories[:100],
        "source_url": page_url,
        "source_status": "Marvel Database page imported" if page and not page.get("missing") else "CSV fallback / source page not matched",
        "source": "Marvel Database (Fandom)" if page and not page.get("missing") else "Existing Marvel character CSV",
    }


def fetch_pages(session: requests.Session, titles: list[str]) -> dict[str, dict[str, Any]]:
    pages_by_norm: dict[str, dict[str, Any]] = {}
    params = {
        "action": "query",
        "prop": "extracts|categories|revisions",
        "titles": "|".join(titles),
        "redirects": "1",
        "exintro": "1",
        "explaintext": "1",
        "exchars": "2400",
        "cllimit": "100",
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
        "formatversion": "2",
        "maxlag": "5",
    }
    for attempt in range(4):
        try:
            response = session.get(API, params=params, timeout=(20, 75))
            if response.status_code in (429, 503):
                time.sleep(2 ** (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            resolved: dict[str, str] = {}
            for item in data.get("query", {}).get("normalized", []):
                resolved[norm(item.get("from", ""))] = norm(item.get("to", ""))
            for item in data.get("query", {}).get("redirects", []):
                resolved[norm(item.get("from", ""))] = norm(item.get("to", ""))
            for page in data.get("query", {}).get("pages", []):
                key = norm(page.get("title", ""))
                pages_by_norm[key] = page
            for original, resolved_title in resolved.items():
                if resolved_title in pages_by_norm:
                    pages_by_norm[original] = pages_by_norm[resolved_title]
            return pages_by_norm
        except Exception as exc:
            if attempt == 3:
                print(f"API batch failed ({len(titles)} titles): {exc}", file=sys.stderr)
                raise RuntimeError(f"Marvel Database API batch failed; progress was not advanced: {exc}")
            time.sleep(2 ** attempt)
    return {}


def read_progress(total: int) -> dict[str, Any]:
    if PROGRESS.exists():
        try:
            state = json.loads(PROGRESS.read_text(encoding="utf-8"))
            if state.get("total") == total:
                return state
        except Exception:
            pass
    return {
        "next_index": 0,
        "total": total,
        "batch_size": 1000,
        "completed": False,
        "runs": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = extract_archive_json()
    names = database.get("characters") or []
    rows = database.get("characterRows") or []
    if not names:
        raise RuntimeError("The archive character index is empty")

    row_map = {
        norm(row.get("Character_Name", "")): row
        for row in rows
        if row.get("Character_Name")
    }
    state = read_progress(len(names))
    start = int(state.get("next_index") or 0)
    if state.get("completed") or start >= len(names):
        print(f"Character enrichment already complete: {len(names)} indexed names.")
        return 0

    batch_size = max(100, min(args.batch_size, 2500))
    end = min(start + batch_size, len(names))
    selected = names[start:end]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    pages: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(selected), PAGE_BATCH):
        title_batch = selected[offset:offset + PAGE_BATCH]
        pages.update(fetch_pages(session, title_batch))
        time.sleep(0.20)

    records: dict[str, Any] = {}
    for name in selected:
        page = pages.get(norm(name))
        row = row_map.get(norm(name))
        record = make_record(name, page, row)
        record_key = norm(name)
        if record_key in records:
            record_key = f"{record_key}__{record.get('page_id') or start + len(records)}"
        records[record_key] = record

    shard_index = start // batch_size
    shard_path = DATA_DIR / f"characters-{shard_index:03d}.json"
    shard = {
        "start": start,
        "end": end,
        "total": len(names),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "records": records,
    }
    shard_path.write_text(
        json.dumps(shard, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8"
    )

    state.update({
        "next_index": end,
        "total": len(names),
        "batch_size": batch_size,
        "completed": end >= len(names),
        "runs": int(state.get("runs", 0)) + 1,
        "last_batch": {
            "start": start,
            "end": end,
            "records": len(records),
            "matched_source_pages": sum(1 for x in records.values() if x.get("page_id")),
            "generated_at": shard["generated_at"],
        },
    })
    PROGRESS.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(
        f"Imported character records {start + 1}-{end} of {len(names)}; "
        f"Marvel Database pages matched: {state['last_batch']['matched_source_pages']}; "
        f"complete={state['completed']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
