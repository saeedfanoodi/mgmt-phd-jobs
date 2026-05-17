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
    "Strategy": [r"\bstrateg", r"strategic management", r"corporate strategy"],
    "AI": [r"\bartificial intelligence\b", r"\bai/ml\b", r"machine learning",
           r"business analytics", r"data scien"],
    "Entrepreneurship": [r"\bentrepreneur", r"innovation management"],
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
    SCHOOL_COLS = ["school", "institution", "university", "employer", "org"]
    POS_COLS = ["position", "title", "job title", "role", "rank"]
    DEPT_COLS = ["department", "dept", "area"]
    LINK_COLS = ["link", "url", "apply", "website", "posting"]
    DEADLINE_COLS = ["deadline", "due date", "close date", "closing date",
                     "application deadline"]
    START_COLS = ["start date", "start", "begin date"]
    COUNTRY_COLS = ["country", "location", "region"]
    FIELD_COLS = ["field", "area", "discipline", "specialty",
                  "research area", "specialization"]
    TYPE_COLS = ["type", "rank", "position type", "appointment"]
    SALARY_COLS = ["salary", "compensation", "pay"]

    def pick(row, cols):
        keys_lower = {k.lower().strip(): k for k in row.keys() if k}
        for c in cols:
            if c in keys_lower:
                v = (row[keys_lower[c]] or "").strip()
                if v:
                    return v
        return ""

    for row in reader:
        try:
            school = pick(row, SCHOOL_COLS)
            position = pick(row, POS_COLS)
            if not school and not position:
                continue
            link = pick(row, LINK_COLS) or ""
            blob = " ".join(str(v) for v in row.values() if v)
            if not any(re.search(p, blob, re.I) for p in TT_KEYWORDS):
                continue
            field = pick(row, FIELD_COLS) or detect_field(blob)
            if not field:
                continue
            country = pick(row, COUNTRY_COLS) or detect_country(blob) or "USA"
            pos_type = pick(row, TYPE_COLS) or detect_type(blob) or "Tenure Track"
            salary = pick(row, SALARY_COLS)
            salary_confirmed = bool(salary)
            if not salary:
                salary = estimate_salary(country, school or position)
            out.append({
                "school": (school or position)[:160],
                "dept": pick(row, DEPT_COLS) or "-",
                "position": (position or school)[:160],
                "type": pos_type,
                "country": country,
                "field": field,
                "salary": salary,
                "salaryConfirmed": salary_confirmed,
                "deadline": pick(row, DEADLINE_COLS) or "See posting",
                "start": pick(row, START_COLS) or "TBD",
                "link": link if link.startswith("http") else "",
                "notes": "From " + source,
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
    print("Added " + str(len(new_jobs)) + " new jobs. Total: " + str(len(merged)) + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
