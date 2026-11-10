#!/usr/bin/env python3
"""Incrementally enrich the complete Marvel character index from Marvel Database pages."""
import json, os, re, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "site-data" / "character-name-index.json"
PROGRESS = ROOT / "site-data" / "character-enrichment-progress.json"
OUT = ROOT / "site-data" / "character-enrichment"
BATCH = int(os.environ.get("ENRICH_BATCH_SIZE", "200"))
API = "https://marvel.fandom.com/api.php"
UA = "MarvelUniverseArchive/1.0 (character reference import; educational fan archive)"

FIELDS = [
 "Real name","Aliases","Creators","First appearance","Publication date","Publisher",
 "Occupation","Species","Nationality","Base of operations","Affiliations","Teams",
 "Powers","Abilities","Weapons and equipment","Weaknesses","Major storylines",
 "Appearances","Family","Allies","Enemies","Identity","Status","Gender",
 "Eye colour","Hair","Alignment","Biography"
]
MAP = {
 "Real name":["realname","real name","fullname","full name","civilian name"],
 "Aliases":["alias","aliases","other names","known aliases"],
 "Creators":["creator","creators"],
 "First appearance":["firstappearance","first appearance","debut","debut issue"],
 "Publication date":["debut date","first appearance date","publication date"],
 "Publisher":["publisher"],
 "Occupation":["occupation","occupations"],
 "Species":["species","race"],
 "Nationality":["nationality","citizenship"],
 "Base of operations":["baseofoperations","base of operations","residence","home"],
 "Affiliations":["affiliation","affiliations","group affiliation","organizations"],
 "Teams":["team","teams"],
 "Powers":["powers","superpowers","power"],
 "Abilities":["abilities","skills","special abilities"],
 "Weapons and equipment":["weapons","equipment","weapon"],
 "Weaknesses":["weaknesses","weakness"],
 "Major storylines":["major storylines","story arcs","notable stories"],
 "Appearances":["appearances","comics"],
 "Family":["relatives","family"],
 "Allies":["allies"],
 "Enemies":["enemies"],
 "Identity":["identity","secret identity"],
 "Status":["status","living status"]
}

def norm(s):
    s = re.sub(r"\s*\([^)]*\)\s*", " ", str(s or ""))
    s = re.sub(r"earth[- ]?\d+", " ", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def bucket(name):
    c = (name or "").strip()[:1].upper()
    return c.lower() if "A" <= c <= "Z" else ("0-9" if c.isdigit() else "other")

def clean(value):
    if value is None: return ""
    s = str(value)
    s = re.sub(r"<ref[^>]*>.*?</ref>", " ", s, flags=re.I|re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", s)
    s = re.sub(r"\[\[|\]\]", "", s)
    s = re.sub(r"\{\{[^{}]*\}\}", " ", s)
    s = re.sub(r"'''?|\[\d+\]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;|")
    if len(s) > 360: s = s[:357].rsplit(" ",1)[0] + "..."
    if not s or re.match(r"^(unknown|none|n/?a|not applicable|not listed)$", s, re.I): return ""
    return s

def infobox(raw):
    m = re.search(r"\{\{\s*(?:infobox\s+character|character|superhero|supervillain|villain|hero|marvel character)\b", raw, re.I)
    if not m: return {}
    block = raw[m.start():m.start()+26000]
    values = {}
    for line in block.splitlines():
        m = re.match(r"\s*\|\s*([^=]+?)\s*=\s*(.*?)\s*$", line)
        if m:
            k = re.sub(r"[^a-z0-9 ]", "", m.group(1).lower()).strip()
            v = clean(m.group(2))
            if k and v: values[k] = v
        if line.strip() == "}}": break
    return values

def request_pages(names):
    result = {}
    for start in range(0, len(names), 25):
        batch = names[start:start+25]
        params = {
          "action":"query","prop":"extracts|categories|revisions",
          "titles":"|".join(batch),"redirects":"1","explaintext":"1","exintro":"1",
          "exchars":"4500","rvprop":"content","rvslots":"main","cllimit":"100",
          "format":"json","formatversion":"2"
        }
        url = API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent":UA,"Accept":"application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=35) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                for page in payload.get("query",{}).get("pages",[]):
                    if page.get("missing") is not True:
                        raw = ""
                        revisions = page.get("revisions") or []
                        if revisions:
                            slot = revisions[0].get("slots",{}).get("main",{})
                            raw = slot.get("content") or slot.get("*") or revisions[0].get("*") or ""
                        result[norm(page.get("title"))] = {
                          "title":page.get("title",""),"extract":page.get("extract",""),
                          "raw":raw,"categories":[x.get("title","").replace("Category:","") for x in page.get("categories",[])]
                        }
                break
            except Exception:
                if attempt == 3: break
                time.sleep(2 ** attempt)
        time.sleep(0.4)
    return result

def add(fields, label, value):
    if isinstance(value, (list,tuple)):
        vals = [clean(x) for x in value]
    else:
        vals = [clean(value)]
    vals = [v for v in vals if v]
    if vals:
        fields[label] = list(dict.fromkeys((fields.get(label,[]) + vals)))[:15]

def build_record(name, row, page):
    raw = page.get("raw","") if page else ""
    box = infobox(raw)
    fields = {}
    for label, keys in MAP.items():
        for key in keys:
            if box.get(key):
                add(fields,label,box[key]); break
    cats = page.get("categories",[]) if page else []
    add(fields,"Affiliations",[c for c in cats if re.search(r"\b(team|affiliation|organization|corps|avengers|x-men|fantastic four|guardians)\b",c,re.I)][:8])
    add(fields,"Major storylines",[c for c in cats if re.search(r"storyline|story arc|comic series",c,re.I)][:8])
    if row:
        add(fields,"Identity",row.get("ID"))
        add(fields,"Alignment",str(row.get("Alignment","")).replace(" Characters",""))
        add(fields,"Gender",str(row.get("SEX","")).replace(" Characters",""))
        add(fields,"Status",str(row.get("ALIVE","")).replace(" Characters",""))
        add(fields,"Eye colour",str(row.get("EYE","")).replace(" Eyes",""))
        add(fields,"Hair",str(row.get("HAIR","")).replace(" Hair",""))
        app = clean(row.get("FIRST APPEARANCE",""))
        year = clean(row.get("Year","")).replace(".0","")
        add(fields,"First appearance",f"{app} ({year})" if app and year else app or year)
        add(fields,"Appearances",clean(row.get("APPEARANCES","")).replace(".0",""))
    if not fields.get("Real name"):
        base = re.sub(r"\s*\(Earth[- ]?\d+\)\s*$","",name,flags=re.I).strip()
        match = re.search(r"\(([^()]+)\)$",base)
        if match and not re.search(r"legion personality|lmd|earth|warp world",match.group(1),re.I):
            add(fields,"Real name",match.group(1))
        elif base and not re.fullmatch(r"[\d\W_]+",base):
            add(fields,"Real name",base)
    if not fields.get("Publisher"): add(fields,"Publisher","Marvel Comics")
    summary = clean(page.get("extract","")) if page else ""
    if summary: add(fields,"Biography",summary)
    for label in FIELDS:
        if not fields.get(label):
            fields[label] = ["Not listed"]
    return {"name":name,"fields":fields,"summary":summary,"record_status":"source page imported" if page else "archive index record","updated_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}

def load_source_data():
    parts = sorted((ROOT/"site-data").glob("part-*.txt"))
    if not parts: return {}
    pieces = [p.read_text(encoding="utf-8") for p in parts]
    marker = "/* Directory comfort + stable cinematic backdrop */"
    if marker in pieces[0]: pieces[0] = pieces[0][:pieces[0].rfind(marker)]
    source = "".join(pieces)
    start = source.find("window.MARVEL_ARCHIVE_DATA=")
    begin = source.find("{",start)
    end = source.find("};</script>",begin)
    raw = source[begin:end+1]
    raw = raw.replace('"SEX":"Male Characters" Bentley (Earth-616)","urlslug"','"SEX":"Male Characters","urlslug"')
    return json.loads(raw)

def main():
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    names = index["characters"]
    progress = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else {"cursor":0}
    cursor = int(progress.get("cursor",0))
    if cursor >= len(names):
        print(f"Complete: {len(names)} indexed character names processed.")
        return
    source = load_source_data()
    row_map = {}
    for row in source.get("characterRows",[]):
        key = norm(row.get("Character_Name"))
        if key:
            old = row_map.get(key)
            if not old or float(row.get("APPEARANCES") or 0) > float(old.get("APPEARANCES") or 0):
                row_map[key] = row
    batch = names[cursor:cursor+BATCH]
    pages = request_pages(batch)
    changed = {}
    for name in batch:
        row = row_map.get(norm(name))
        page = pages.get(norm(name))
        record = build_record(name,row,page)
        b = bucket(name)
        if b not in changed:
            path = OUT / f"{b}.json"
            changed[b] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        changed[b][name] = record
    OUT.mkdir(parents=True,exist_ok=True)
    for b,records in changed.items():
        (OUT/f"{b}.json").write_text(json.dumps(records,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    next_cursor = cursor + len(batch)
    PROGRESS.write_text(json.dumps({"cursor":next_cursor,"total":len(names),"last_batch":len(batch),"complete":next_cursor>=len(names)},indent=2),encoding="utf-8")
    print(f"Processed {cursor+1}-{next_cursor} of {len(names)} character names; source pages matched: {len(pages)}.")
if __name__ == "__main__":
    main()
