#!/usr/bin/env python3
"""Daily scraper for management PhD faculty jobs.
Scans configured sources, filters by field/country/position-type,
appends new postings to jobs.json, and records source health in
sources_status.json. Runs in GitHub Actions cron.
"""
import csv
import io
import json
import re
import sys
import datetime
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent.parent
JOBS_FILE = HERE / "jobs.json"
SOURCES_FILE = HERE / "sources_status.json"

FIELD_KEYWORDS = {
    "Strategy": [r"\bstrateg", r"strategic management", r"corporate strategy",
                 r"\bstrat[/\s]", r"[/\s]strat[/\s]"],
    "AI": [r"\bartificial intelligence\b", r"\bai/ml\b", r"machine learning",
           r"business analytics", r"data scien", r"\bai\b"],
    "Entrepreneurship": [r"\bentrepreneur", r"innovation management",
                         r"\bent[/\s]", r"[/\s]ent[/\s]", r"[/\s]ent\b"],
}
TT_KEYWORDS = [
    r"tenure[- ]track", r"assistant professor", r"associate professor",
    r"open rank", r"\blecturer\b", r"senior lecturer",
]
COUNTRY_KEYWORDS = {
    "Canada": [r"\bcanada\b", r"ontario", r"quebec", r"british columbia",
               r"alberta", r"toronto", r"montreal", r"vancouver", r"calgary",
               r"waterloo", r"mcgill", r"ubc"],
    "Australia": [r"\baustralia\b", r"sydney", r"melbourne", r"brisbane",
                  r"perth", r"adelaide", r"canberra", r"unsw", r"monash"],
    "New Zealand": [r"new zealand", r"auckland", r"wellington",
                    r"christchurch", r"otago"],
    "UAE": [r"\buae\b", r"united arab emirates", r"abu dhabi", r"dubai",
            r"sharjah", r"khalifa"],
    "USA": [r"\busa\b", r"united states", r"u\.s\."],
}
SALARY = {
    "USA_elite": "$210,000 - $275,000 (est.)",
    "USA_strong": "$170,000 - $210,000 (est.)",
    "USA_regional": "$110,000 - $150,000 (est.)",
    "Canada_R1": "CAD $160,000 - $210,000 (est.)",
    "Canada_other": "CAD $115,000 - $155,000 (est.)",
    "Australia": "AUD $115,000 - $165,000 (est.)",
    "New Zealand": "NZD $100,000 - $145,000 (est.)",
    "UAE": "USD $115,000 - $180,000 + housing (est.)",
}
ELITE_US = ["harvard", "wharton", "stanford", "yale", "booth", "kellogg",
            "mit sloan", "columbia business", "haas", "fuqua", "tuck",
            "stern", "ross", "anderson", "kelley", "mccombs"]
STRONG_US = ["fisher", "olin", "owen", "darden", "georgetown", "carey",
             "broad", "kenan-flagler", "smith school", "carlson",
             "krannert", "scheller", "tippie", "marshall"]
R1_CA = ["rotman", "ivey", "smith school", "sauder", "schulich", "desautels"]

# Phrases that indicate a job posting has been removed or expired
DELETION_MARKERS = [
    "view deleted positions",
    "this position has been deleted",
    "this job posting is no longer available",
    "this posting has expired",
    "position has been filled",
    "this listing has expired",
    "job listing not found",
    "no longer accepting applications",
    "posting has been removed",
    "this job is no longer available",
    "vacancy has been filled",
    "position is no longer available",
]

DEADLINE_FORMATS = [
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%m/%d/%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y",
]

def parse_deadline(deadline_str):
    """Try to parse a deadline string into a date. Returns None if unparseable."""
    if not deadline_str:
        return None
    s = deadline_str.strip().rstrip(".")
    # Remove ordinal suffixes: 1st → 1, 2nd → 2, etc.
    s = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", s)
    for fmt in DEADLINE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def deadline_clearly_passed(job):
    """Return True if the job has a specific deadline that passed >14 days ago."""
    deadline = job.get("deadline", "")
    if not deadline or deadline.lower() in (
        "open until filled", "rolling", "see posting", "tbd", "", "varies"
    ):
        return False
    dl = parse_deadline(deadline)
    if dl is None:
        return False
    cutoff = datetime.date.today() - datetime.timedelta(days=14)
    return dl < cutoff

def job_is_stale(job):
    """Return True if the job's posted date is >120 days old (for URL validation)."""
    posted = job.get("posted", "")
    if not posted:
        return False
    try:
        posted_date = datetime.date.fromisoformat(posted)
        return (datetime.date.today() - posted_date).days > 120
    except ValueError:
        return False

def url_is_dead(link, timeout=10):
    """Return True if the URL returns a clear deletion/expiry signal."""
    if not link or not link.startswith("http"):
        return False
    try:
        r = requests.get(
            link, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (saeed-jobs-bot/1.0)"},
            allow_redirects=True,
        )
        if r.status_code == 410:        # HTTP Gone
            return True
        if r.status_code == 404:
            return True
        text_lower = r.text.lower()
        if any(marker in text_lower for marker in DELETION_MARKERS):
            return True
    except Exception:
        pass  # network error → assume still live
    return False

def validate_jobs(existing, max_url_checks=20):
    """
    Filter out expired jobs from the existing list.

    Rules (applied in order, no HTTP call needed for the first two):
      1. Deadline passed >14 days ago → remove.
      2. Job is stale (posted >120 days ago) → check URL; remove if dead.
      3. Otherwise keep.

    max_url_checks caps the number of HTTP requests per run so the
    GitHub Action stays fast.
    """
    kept = []
    removed = []
    checks_done = 0

    for job in existing:
        # Rule 1: deadline clearly past
        if deadline_clearly_passed(job):
            removed.append((job.get("school", "?"), "deadline passed"))
            continue

        # Rule 2: stale posting — validate URL (up to cap)
        if job_is_stale(job) and checks_done < max_url_checks:
            checks_done += 1
            if url_is_dead(job.get("link", "")):
                removed.append((job.get("school", "?"), "URL dead/deleted"))
                continue

        kept.append(job)

    if removed:
        print(f"Removed {len(removed)} expired jobs:")
        for school, reason in removed:
            print(f"  - {school}: {reason}")

    return kept


def estimate_salary(country, school):
    s = (school or "").lower()
    if country == "USA":
        if any(e in s for e in ELITE_US):
            return SALARY["USA_elite"]
        if any(e in s for e in STRONG_US):
            return SALARY["USA_strong"]
        return SALARY["USA_regional"]
    if country == "Canada":
        if any(e in s for e in R1_CA):
            return SALARY["Canada_R1"]
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
    found = []
    for field, pats in FIELD_KEYWORDS.items():
        if any(re.search(p, t) for p in pats):
            found.append(field)
    return ", ".join(found) if found else None

def detect_type(text):
    t = (text or "").lower()
    if "open rank" in t:
        return "Open Rank"
    if re.search(r"tenure[- ]track", t):
        return "Tenure Track"
    if "non-tenure" in t or "non tenure" in t:
        return "Non-Tenure Track"
    if "senior lecturer" in t:
        return "Tenure Track"
    if "lecturer" in t:
        return "Lecturer"
    if "assistant professor" in t or "associate professor" in t:
        return "Tenure Track"
    return None

def extract_school(title):
    if " at " in title:
        return title.split(" at ", 1)[1][:80].strip()
    for sep in [" - ", " | "]:
        if sep in title:
            parts = [p.strip() for p in title.split(sep)]
            return max(parts, key=len)[:80]
    return (title or "")[:80].strip()

def make_job(title, link, summary, source):
    body = (title or "") + " " + (summary or "")
    if not any(re.search(p, body, re.I) for p in TT_KEYWORDS):
        return None
    field = detect_field(body)
    if not field:
        return None
    country = detect_country(body) or "USA"
    pos_type = detect_type(body) or "Tenure Track"
    school = extract_school(title)
    deadline = "See posting"
    m = re.search(r"deadline[:\s]+([A-Z][a-z]+ \d{1,2},?\s+\d{4})", body, re.I)
    if m:
        deadline = m.group(1)
    return {
        "school": school,
        "dept": "-",
        "position": (title or "").strip()[:160],
        "type": pos_type,
        "country": country,
        "field": field,
        "salary": estimate_salary(country, school),
        "salaryConfirmed": False,
        "deadline": deadline,
        "start": "TBD",
        "link": link or "",
        "posted": datetime.date.today().isoformat(),
        "notes": "Auto-detected from " + source,
    }

def scrape_rss(url, source):
    feed = feedparser.parse(url)
    out = []
    for e in feed.entries[:60]:
        j = make_job(e.get("title", ""), e.get("link", ""),
                     e.get("summary", "") or e.get("description", ""), source)
        if j:
            out.append(j)
    return out, None

def scrape_html(url, source, selectors):
    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 (saeed-jobs-bot/1.0)"})
        r.raise_for_status()
    except Exception as ex:
        return [], str(ex)
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for item in soup.select(selectors["item"])[:40]:
        try:
            t_el = item.select_one(selectors["title"])
            l_el = item.select_one(selectors["link"])
            if not t_el or not l_el:
                continue
            link = l_el.get("href", "")
            if link and not link.startswith("http"):
                link = urljoin(url, link)
            title = t_el.get_text(strip=True)
            summary = item.get_text(" ", strip=True)[:600]
            j = make_job(title, link, summary, source)
            if j:
                out.append(j)
        except Exception:
            continue
    return out, None

def scrape_gsheet_csv(sheet_id, gid, source):
    """Fetch a Google Sheets tab as CSV and parse rows."""
    url = ("https://docs.google.com/spreadsheets/d/" + sheet_id +
           "/export?format=csv&gid=" + gid)
    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 (saeed-jobs-bot/1.0)"})
        r.raise_for_status()
    except Exception as ex:
        return [], str(ex)
    reader = csv.DictReader(io.StringIO(r.text))
    out = []
    TARGET_COUNTRIES = {"USA", "Canada", "Australia", "New Zealand", "UAE"}
    TYPE_MAP = {"tt": "Tenure Track", "ntt": "Non-Tenure Track",
                "postdoc": "Non-Tenure Track", "post-doc": "Non-Tenure Track",
                "open rank": "Open Rank", "lecturer": "Lecturer",
                "senior lecturer": "Tenure Track"}

    def get(row, *names):
        keys_lower = {k.lower().strip(): k for k in row.keys() if k}
        for n in names:
            if n.lower() in keys_lower:
                v = (row[keys_lower[n.lower()]] or "").strip()
                if v:
                    return v
        return ""

    for row in reader:
        try:
            university = get(row, "University", "School", "Institution")
            if not university:
                continue
            if "DO NOT" in university.upper() or "SORT" in university.upper():
                continue
            expired = get(row, "Expired?", "Expired")
            if expired and expired.lower() not in ("no", "n", "false", "0"):
                continue
            location = get(row, "Location")
            region = get(row, "Region")
            blob = " ".join(str(v) for v in row.values() if v)
            country = detect_country(location + " " + region + " " + blob)
            if country not in TARGET_COUNTRIES:
                continue
            area = get(row, "Area", "Field", "Discipline")
            field = detect_field(area + " " + blob)
            if not field:
                continue
            rank = get(row, "Rank", "Position", "Title")
            tt_raw = get(row, "TT-NTT-PostDoc", "TT/NTT", "Type", "Appointment")
            pos_type = TYPE_MAP.get(tt_raw.lower().strip(),
                                    detect_type(rank + " " + tt_raw) or "Tenure Track")
            position = (rank + " Professor of " + area).strip() if rank and area else (rank or area or "Faculty position")
            salary = get(row, "Salary", "Compensation")
            salary_confirmed = bool(salary)
            if not salary:
                salary = estimate_salary(country, university)
            link = get(row, "Link", "URL", "Apply")
            notes_parts = []
            tl = get(row, "Teaching load", "Teaching Load")
            if tl:
                notes_parts.append("Teaching: " + tl)
            nc = get(row, "NOTES/COMMENTS", "Notes", "Comments")
            if nc:
                notes_parts.append(nc[:120])
            h1b = get(row, "H1B Allowed?", "H1B")
            if h1b:
                notes_parts.append("H1B: " + h1b)
            out.append({
                "school": university[:160],
                "dept": area or "-",
                "position": position[:160],
                "type": pos_type,
                "country": country,
                "field": field,
                "salary": salary,
                "salaryConfirmed": salary_confirmed,
                "deadline": get(row, "Due Date", "Deadline") or "See posting",
                "start": get(row, "Start Date", "Start") or "TBD",
                "link": link if link.startswith("http") else "",
                "posted": datetime.date.today().isoformat(),
                "notes": " | ".join(notes_parts) if notes_parts else ("From " + source),
            })
        except Exception:
            continue
    return out, None

SOURCES = [
    {"name": "HigherEdJobs - Management", "type": "rss",
     "url": "https://www.higheredjobs.com/rss/articleFeed.cfm?CatNum=46"},
    {"name": "HigherEdJobs - Business", "type": "rss",
     "url": "https://www.higheredjobs.com/rss/articleFeed.cfm?CatNum=44"},
    {"name": "Inside Higher Ed - Business & Management", "type": "rss",
     "url": "https://careers.insidehighered.com/jobs/business-and-management/feed"},
    {"name": "Chronicle - Business & Management", "type": "rss",
     "url": "https://jobs.chronicle.com/jobs/business-and-management/feed/"},
    {"name": "Times Higher Ed unijobs - Business", "type": "rss",
     "url": "https://www.timeshighereducation.com/unijobs/rss/?keywords=management"},
    {"name": "ASAC Canada - Placements", "type": "html",
     "url": "https://asac.ca/job-postings/placements/",
     "selectors": {"item": "article, .post, .job-listing, .entry",
                   "title": "h1, h2, h3, .entry-title, .title",
                   "link": "a"}},
    {"name": "University Affairs Canada - Management", "type": "html",
     "url": "https://www.universityaffairs.ca/search-job/?_search_job_title=management",
     "selectors": {"item": "article, .job, .search-result",
                   "title": "h2, h3, .title", "link": "a"}},
    {"name": "AKADEUS - Announcements", "type": "html",
     "url": "https://www.akadeus.com/announcements",
     "selectors": {"item": "article, .announcement, .post, li",
                   "title": "h2, h3, .title, a", "link": "a"}},
    {"name": "AOM - Placement Board", "type": "html",
     "url": "https://placement.aom.org/jobs",
     "selectors": {"item": ".job-listing, article, tr, li",
                   "title": "h2, h3, .job-title, td, a", "link": "a"}},
    {"name": "SIOP - Career Center", "type": "html",
     "url": "https://www.siop.org/Career-Center/Job-Search",
     "selectors": {"item": "article, .job, .career-listing, li",
                   "title": "h2, h3, .title, a", "link": "a"}},
    {"name": "Google Sheet - Faculty Jobs (community)", "type": "gsheet",
     "sheet_id": "1_GJuEMKVgGc6qq3IflXVtq4miDR-p4sQYHQYhHwNveQ",
     "gid": "1242106999",
     "url": "https://docs.google.com/spreadsheets/d/1_GJuEMKVgGc6qq3IflXVtq4miDR-p4sQYHQYhHwNveQ/edit?gid=1242106999"},
]

def main():
    existing = json.loads(JOBS_FILE.read_text()) if JOBS_FILE.exists() else []

    # --- VALIDATE: remove expired / dead jobs before adding new ones ---
    print(f"Validating {len(existing)} existing jobs...")
    existing = validate_jobs(existing, max_url_checks=20)
    print(f"{len(existing)} jobs remain after validation.")

    seen = {(j["school"].lower().strip(), j["position"].lower().strip())
            for j in existing}
    sources_status = {}
    new_jobs = []
    for src in SOURCES:
        try:
            if src["type"] == "rss":
                found, err = scrape_rss(src["url"], src["name"])
            elif src["type"] == "gsheet":
                found, err = scrape_gsheet_csv(src["sheet_id"], src["gid"], src["name"])
            else:
                found, err = scrape_html(src["url"], src["name"], src["selectors"])
            new_from_src = 0
            for j in found:
                key = (j["school"].lower().strip(),
                       j["position"].lower().strip())
                if key in seen:
                    continue
                seen.add(key)
                new_jobs.append(j)
                new_from_src += 1
            sources_status[src["name"]] = {
                "ok": err is None, "matched": len(found),
                "new": new_from_src, "url": src["url"], "error": err,
            }
        except Exception as e:
            sources_status[src["name"]] = {
                "ok": False, "matched": 0, "new": 0,
                "url": src.get("url", ""), "error": str(e),
            }
    merged = existing + new_jobs
    JOBS_FILE.write_text(json.dumps(merged, indent=2))
    SOURCES_FILE.write_text(json.dumps({
        "last_run": datetime.datetime.utcnow().isoformat() + "Z",
        "total_jobs": len(merged),
        "new_this_run": len(new_jobs),
        "sources": sources_status,
    }, indent=2))
    print("Added", len(new_jobs), "new jobs. Total:", len(merged))
    return 0

if __name__ == "__main__":
    sys.exit(main())
