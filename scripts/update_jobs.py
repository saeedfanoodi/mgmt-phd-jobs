#!/usr/bin/env python3
"""
Daily scraper + validator for management PhD faculty jobs.

Every run at 8 AM:
  1. Load existing jobs from jobs.json
  2. Validate EVERY specific-URL job concurrently — drop dead/deleted postings
  3. Scrape all sources for new postings
  4. Merge, deduplicate, save jobs.json + sources_status.json
"""
import csv, io, json, re, sys, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import feedparser, requests
from bs4 import BeautifulSoup

HERE        = Path(__file__).resolve().parent.parent
JOBS_FILE   = HERE / "jobs.json"
SOURCES_FILE= HERE / "sources_status.json"

# ── Keywords ──────────────────────────────────────────────────────────────────
FIELD_KEYWORDS = {
    "Strategy":       [r"\bstrateg", r"strategic management", r"corporate strategy"],
    "AI":             [r"\bartificial intelligence\b", r"\bai/ml\b", r"machine learning",
                       r"business analytics", r"data scien"],
    "Entrepreneurship":[r"\bentrepreneur", r"innovation management"],
}
TT_KEYWORDS = [
    r"tenure[- ]track", r"assistant professor", r"associate professor",
    r"open rank", r"\blecturer\b", r"senior lecturer",
]
COUNTRY_KEYWORDS = {
    "Canada":      [r"\bcanada\b",r"ontario",r"quebec",r"british columbia",r"alberta",
                    r"toronto",r"montreal",r"vancouver",r"calgary",r"waterloo",r"mcgill",r"ubc"],
    "Australia":   [r"\baustralia\b",r"sydney",r"melbourne",r"brisbane",r"perth",
                    r"adelaide",r"canberra",r"unsw",r"monash"],
    "New Zealand": [r"new zealand",r"auckland",r"wellington",r"christchurch",r"otago"],
    "UAE":         [r"\buae\b",r"united arab emirates",r"abu dhabi",r"dubai",r"sharjah",r"khalifa"],
    "USA":         [r"\busa\b",r"united states",r"u\.s\."],
}
SALARY = {
    "USA_elite":    "$210,000 – $275,000 (est.)",
    "USA_strong":   "$170,000 – $210,000 (est.)",
    "USA_regional": "$110,000 – $150,000 (est.)",
    "Canada_R1":    "CAD $160,000 – $210,000 (est.)",
    "Canada_other": "CAD $115,000 – $155,000 (est.)",
    "Australia":    "AUD $115,000 – $165,000 (est.)",
    "New Zealand":  "NZD $100,000 – $145,000 (est.)",
    "UAE":          "USD $115,000 – $180,000 + housing (est.)",
}
ELITE_US  = ["harvard","wharton","stanford","yale","booth","kellogg","mit sloan",
             "columbia business","haas","fuqua","tuck","stern","ross","anderson","kelley","mccombs"]
STRONG_US = ["fisher","olin","owen","darden","georgetown","carey","broad","kenan-flagler",
             "smith school","carlson","krannert","scheller","tippie","marshall"]
R1_CA     = ["rotman","ivey","smith school","sauder","schulich","desautels"]

# ── Dead-URL detection ────────────────────────────────────────────────────────
DEAD_PHRASES = [
    "view deleted positions",
    "this position has been deleted",
    "position is no longer available",
    "this job is no longer available",
    "this posting has been removed",
    "this posting has expired",
    "this listing is no longer active",
    "listing is no longer available",
    "posting has been filled",
    "job no longer active",
    "position has been filled",
    "vacancy has been filled",
    "no longer accepting applications",
    "application period has closed",
    "job listing not found",
    "this job has expired",
    "this position is closed",
    "this role is no longer available",
]

# URL patterns that mean it's a SPECIFIC job posting (not a generic dept page).
# Only these get HTTP-validated; generic pages like /careers/ can't be reliably checked.
SPECIFIC_PATTERNS = [
    r"[?&]JobCode=",
    r"[?&]jobId=",
    r"[?&]job_id=",
    r"[?&]req_id=",
    r"[?&]CompetitionId=",
    r"/postings/\d",
    r"/job/[A-Za-z0-9\-]+/\d",
    r"/jobs/[A-Za-z0-9\-]+-\d{4,}",
    r"/competition/[A-Za-z0-9]",
    r"/position/\d",
    r"/go/[^/]+/\d{5,}",
    r"academicwork\.ca/jobs/[a-z0-9\-]+",
    r"akadeus\.com/announcement/\d",
    r"asac\.ca/[a-z0-9\-]+-\d*/?$",
    r"seek\.com/job/\d",
    r"anu\.edu\.au/jobs/",
    r"careers\.ualberta\.ca/Competition/",
    r"usasbe\.org/news/",
    r"omt\.aom\.org/discussion/",
    r"ent\.aom\.org/discussion/",
    r"career-center\.aom\.org/job/",
]

def is_specific_posting(url):
    if not url or not url.startswith("http"):
        return False
    return any(re.search(p, url, re.I) for p in SPECIFIC_PATTERNS)

def check_url_live(job):
    """Return (job, is_live, reason). Validates specific posting URLs only."""
    link = job.get("link", "")
    if not is_specific_posting(link):
        return job, True, "general-page-skipped"
    try:
        r = requests.get(link, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (saeed-jobs-bot/1.0)"},
                         allow_redirects=True)
        if r.status_code in (404, 410):
            return job, False, f"HTTP {r.status_code}"
        text_lower = r.text.lower()
        for phrase in DEAD_PHRASES:
            if phrase in text_lower:
                return job, False, f"dead-phrase: {phrase!r}"
    except Exception as exc:
        # Network error: assume still live to avoid false removals
        return job, True, f"network-error-kept: {exc}"
    return job, True, "ok"

def validate_all_jobs(jobs, max_workers=12):
    """Concurrently check every job URL. Return only the live ones."""
    kept, removed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(check_url_live, j): j for j in jobs}
        for fut in as_completed(futures):
            job, is_live, reason = fut.result()
            if is_live:
                kept.append(job)
            else:
                removed.append((job.get("school","?"), job.get("link",""), reason))
    if removed:
        print(f"  Removed {len(removed)} dead postings:")
        for school, link, reason in removed:
            print(f"    - {school} | {reason} | {link}")
    else:
        print("  All existing jobs passed URL validation.")
    return kept

# ── Helpers ───────────────────────────────────────────────────────────────────
def estimate_salary(country, school):
    s = (school or "").lower()
    if country == "USA":
        if any(e in s for e in ELITE_US):  return SALARY["USA_elite"]
        if any(e in s for e in STRONG_US): return SALARY["USA_strong"]
        return SALARY["USA_regional"]
    if country == "Canada":
        if any(e in s for e in R1_CA): return SALARY["Canada_R1"]
        return SALARY["Canada_other"]
    return SALARY.get(country, "Range not estimated")

def detect_country(text):
    t = (text or "").lower()
    for country, pats in COUNTRY_KEYWORDS.items():
        if any(re.search(p, t) for p in pats):
            return country
    return None

def detect_field(text):
    t = (text or "").lower()
    found = [f for f, pats in FIELD_KEYWORDS.items()
             if any(re.search(p, t) for p in pats)]
    return ", ".join(found) if found else None

def detect_type(text):
    t = (text or "").lower()
    if "open rank"                              in t: return "Open Rank"
    if re.search(r"tenure[- ]track", t)            : return "Tenure Track"
    if "non-tenure" in t or "non tenure"       in t: return "Non-Tenure Track"
    if "senior lecturer"                        in t: return "Tenure Track"
    if "lecturer"                               in t: return "Lecturer"
    if "assistant professor" in t or "associate professor" in t: return "Tenure Track"
    return None

def extract_school(title):
    if " at " in title:
        return title.split(" at ", 1)[1][:80].strip()
    for sep in [" - ", " | "]:
        if sep in title:
            return max([p.strip() for p in title.split(sep)], key=len)[:80]
    return (title or "")[:80].strip()

def make_job(title, link, summary, source):
    body = (title or "") + " " + (summary or "")
    if not any(re.search(p, body, re.I) for p in TT_KEYWORDS): return None
    field = detect_field(body)
    if not field: return None
    country  = detect_country(body) or "USA"
    pos_type = detect_type(body) or "Tenure Track"
    school   = extract_school(title)
    deadline = "See posting"
    m = re.search(r"deadline[:\s]+([A-Z][a-z]+ \d{1,2},?\s+\d{4})", body, re.I)
    if m: deadline = m.group(1)
    return {
        "school": school, "dept": "-",
        "position": (title or "").strip()[:160],
        "type": pos_type, "country": country, "field": field,
        "salary": estimate_salary(country, school), "salaryConfirmed": False,
        "deadline": deadline, "start": "TBD",
        "link": link or "",
        "posted": datetime.date.today().isoformat(),
        "notes": "Auto-detected from " + source,
    }

# ── Scrapers ──────────────────────────────────────────────────────────────────
def scrape_rss(url, source):
    feed = feedparser.parse(url)
    out  = []
    for e in feed.entries[:60]:
        j = make_job(e.get("title",""), e.get("link",""),
                     e.get("summary","") or e.get("description",""), source)
        if j: out.append(j)
    return out, None

def scrape_html(url, source, selectors):
    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent":"Mozilla/5.0 (saeed-jobs-bot/1.0)"})
        r.raise_for_status()
    except Exception as ex:
        return [], str(ex)
    soup = BeautifulSoup(r.text, "html.parser")
    out  = []
    for item in soup.select(selectors["item"])[:40]:
        try:
            t_el = item.select_one(selectors["title"])
            l_el = item.select_one(selectors["link"])
            if not t_el or not l_el: continue
            link = l_el.get("href","")
            if link and not link.startswith("http"): link = urljoin(url, link)
            j = make_job(t_el.get_text(strip=True), link,
                         item.get_text(" ", strip=True)[:600], source)
            if j: out.append(j)
        except Exception: continue
    return out, None

def scrape_gsheet_csv(sheet_id, gid, source):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent":"Mozilla/5.0 (saeed-jobs-bot/1.0)"})
        r.raise_for_status()
    except Exception as ex:
        return [], str(ex)
    reader  = csv.DictReader(io.StringIO(r.text))
    TARGET  = {"USA","Canada","Australia","New Zealand","UAE"}
    TYPE_MAP= {"tt":"Tenure Track","ntt":"Non-Tenure Track","postdoc":"Non-Tenure Track",
               "post-doc":"Non-Tenure Track","open rank":"Open Rank","lecturer":"Lecturer",
               "senior lecturer":"Tenure Track"}
    def get(row, *names):
        kl = {k.lower().strip(): k for k in row if k}
        for n in names:
            if n.lower() in kl:
                v = (row[kl[n.lower()]] or "").strip()
                if v: return v
        return ""
    out = []
    for row in reader:
        try:
            university = get(row,"University","School","Institution")
            if not university: continue
            if "DO NOT" in university.upper() or "SORT" in university.upper(): continue
            expired = get(row,"Expired?","Expired")
            if expired and expired.lower() not in ("no","n","false","0"): continue
            blob    = " ".join(str(v) for v in row.values() if v)
            country = detect_country(get(row,"Location")+" "+get(row,"Region")+" "+blob)
            if country not in TARGET: continue
            area   = get(row,"Area","Field","Discipline")
            field  = detect_field(area+" "+blob)
            if not field: continue
            rank   = get(row,"Rank","Position","Title")
            tt_raw = get(row,"TT-NTT-PostDoc","TT/NTT","Type","Appointment")
            pos_type= TYPE_MAP.get(tt_raw.lower().strip(),
                                   detect_type(rank+" "+tt_raw) or "Tenure Track")
            position= (rank+" Professor of "+area).strip() if rank and area else (rank or area or "Faculty position")
            salary  = get(row,"Salary","Compensation")
            link    = get(row,"Link","URL","Apply")
            notes   = []
            tl = get(row,"Teaching load","Teaching Load")
            if tl: notes.append("Teaching: "+tl)
            nc = get(row,"NOTES/COMMENTS","Notes","Comments")
            if nc: notes.append(nc[:120])
            h1b = get(row,"H1B Allowed?","H1B")
            if h1b: notes.append("H1B: "+h1b)
            out.append({
                "school": university[:160], "dept": area or "-",
                "position": position[:160], "type": pos_type,
                "country": country, "field": field,
                "salary": salary or estimate_salary(country, university),
                "salaryConfirmed": bool(salary),
                "deadline": get(row,"Due Date","Deadline") or "See posting",
                "start": get(row,"Start Date","Start") or "TBD",
                "link": link if link.startswith("http") else "",
                "posted": datetime.date.today().isoformat(),
                "notes": " | ".join(notes) or ("From "+source),
            })
        except Exception: continue
    return out, None

# ── Sources ───────────────────────────────────────────────────────────────────
SOURCES = [
    {"name":"HigherEdJobs - Management","type":"rss",
     "url":"https://www.higheredjobs.com/rss/articleFeed.cfm?CatNum=46"},
    {"name":"HigherEdJobs - Business","type":"rss",
     "url":"https://www.higheredjobs.com/rss/articleFeed.cfm?CatNum=44"},
    {"name":"Inside Higher Ed - Business & Management","type":"rss",
     "url":"https://careers.insidehighered.com/jobs/business-and-management/feed"},
    {"name":"Chronicle - Business & Management","type":"rss",
     "url":"https://jobs.chronicle.com/jobs/business-and-management/feed/"},
    {"name":"Times Higher Ed unijobs - Business","type":"rss",
     "url":"https://www.timeshighereducation.com/unijobs/rss/?keywords=management"},
    {"name":"ASAC Canada - Placements","type":"html",
     "url":"https://asac.ca/job-postings/placements/",
     "selectors":{"item":"article, .post, .job-listing, .entry",
                  "title":"h1, h2, h3, .entry-title, .title","link":"a"}},
    {"name":"University Affairs Canada - Management","type":"html",
     "url":"https://www.universityaffairs.ca/search-job/?_search_job_title=management",
     "selectors":{"item":"article, .job, .search-result","title":"h2, h3, .title","link":"a"}},
    {"name":"AKADEUS - Announcements","type":"html",
     "url":"https://www.akadeus.com/announcements",
     "selectors":{"item":"article, .announcement, .post, li",
                  "title":"h2, h3, .title, a","link":"a"}},
    {"name":"AOM - Placement Board","type":"html",
     "url":"https://placement.aom.org/jobs",
     "selectors":{"item":".job-listing, article, tr, li",
                  "title":"h2, h3, .job-title, td, a","link":"a"}},
    {"name":"SIOP - Career Center","type":"html",
     "url":"https://www.siop.org/Career-Center/Job-Search",
     "selectors":{"item":"article, .job, .career-listing, li",
                  "title":"h2, h3, .title, a","link":"a"}},
    {"name":"Google Sheet - Faculty Jobs (community)","type":"gsheet",
     "sheet_id":"1_GJuEMKVgGc6qq3IflXVtq4miDR-p4sQYHQYhHwNveQ","gid":"1242106999",
     "url":"https://docs.google.com/spreadsheets/d/1_GJuEMKVgGc6qq3IflXVtq4miDR-p4sQYHQYhHwNveQ/edit?gid=1242106999"},
]

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    existing = json.loads(JOBS_FILE.read_text()) if JOBS_FILE.exists() else []
    print(f"Loaded {len(existing)} existing jobs.")

    # Step 1: validate all — drop dead postings
    print("Validating all job URLs concurrently...")
    existing = validate_all_jobs(existing)
    print(f"{len(existing)} jobs remain after validation.")

    # Step 2: scrape for new jobs
    seen = {(j["school"].lower().strip(), j["position"].lower().strip()) for j in existing}
    sources_status, new_jobs = {}, []
    for src in SOURCES:
        try:
            if   src["type"] == "rss":    found, err = scrape_rss(src["url"], src["name"])
            elif src["type"] == "gsheet": found, err = scrape_gsheet_csv(src["sheet_id"], src["gid"], src["name"])
            else:                         found, err = scrape_html(src["url"], src["name"], src["selectors"])
            added = 0
            for j in found:
                key = (j["school"].lower().strip(), j["position"].lower().strip())
                if key not in seen:
                    seen.add(key); new_jobs.append(j); added += 1
            sources_status[src["name"]] = {"ok": err is None, "matched": len(found),
                                            "new": added, "url": src["url"], "error": err}
        except Exception as e:
            sources_status[src["name"]] = {"ok": False, "matched": 0, "new": 0,
                                            "url": src.get("url",""), "error": str(e)}

    merged = existing + new_jobs
    JOBS_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    SOURCES_FILE.write_text(json.dumps({
        "last_run":     datetime.datetime.utcnow().isoformat() + "Z",
        "total_jobs":   len(merged),
        "new_this_run": len(new_jobs),
        "sources":      sources_status,
    }, indent=2))
    print(f"Done. Added {len(new_jobs)} new jobs. Total: {len(merged)}.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
