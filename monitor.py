# monitor.py  — robust, logs heavily, always prints a report
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

CATALOG_URL = "https://secure.rec1.com/CA/calabasas-ca/catalog/index"
BASELINE_FILE = Path("baseline.json")

TARGET_TITLES = [
    "Swim Lesson Level 1: Baby Pups & Parent Seals",
    "Swim Lesson Level 2: Sea Horses",
]

# regexes
DATE_RANGE = re.compile(r"\b\d{1,2}/\d{1,2}\s*[-–]\s*\d{1,2}/\d{1,2}\b")
DATE_SINGLE = re.compile(r"\b\d{1,2}/\d{1,2}\b")
TIME_RANGE = re.compile(r"\b\d{1,2}:\d{2}\s*[AP]M\s*[-–]\s*\d{1,2}:\d{2}\s*[AP]M\b", re.I)
TIME_SINGLE = re.compile(r"\b\d{1,2}:\d{2}\s*[AP]M\b", re.I)

def log(msg: str):
    print(f"[monitor] {msg}", flush=True)

def extract_dates_times(text: str):
    dates = set(DATE_RANGE.findall(text))
    if not dates:
        dates = set(DATE_SINGLE.findall(text))
    times = set(TIME_RANGE.findall(text))
    if not times:
        times = set(TIME_SINGLE.findall(text))
    return sorted(dates), sorted(times)

def open_aquatics(page):
    log(f"goto {CATALOG_URL}")
    page.goto(CATALOG_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    # Try clicking the category tab if present
    for label in ["Aquatics Programs", "Aquatics"]:
        loc = page.locator(f"text={label}")
        if loc.count():
            log(f"clicking category: {label}")
            try:
                loc.first.click(timeout=2500)
                page.wait_for_timeout(800)
                break
            except Exception as e:
                log(f"warn: category click failed: {e}")
    # Soft scroll to load content
    for _ in range(10):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(120)
    log("finished initial scroll")

def _frames(page):
    fr = [page]
    for f in page.frames:
        try:
            if "secure.rec1.com" in (f.url or ""):
                fr.append(f)
        except Exception:
            continue
    return fr

def _find_heading_anywhere(page, title):
    """
    Find the visible heading element containing the title text,
    on the main page or civicrec iframes.
    """
    patt = re.compile(re.escape(title), re.I)
    for scope in _frames(page):
        # Try role=link first (fast path)
        link = scope.get_by_role("link", name=patt)
        if link.count() > 0:
            log(f"heading found as link in scope {getattr(scope,'url',None)}")
            return link.first
        # Generic text search
        el = scope.get_by_text(patt).first
        if el.count() > 0:
            log(f"heading found by text in scope {getattr(scope,'url',None)}")
            return el
    log(f"heading NOT found: {title}")
    return None

def parse_aria_grid(scope):
    """
    Parse a CivicRec-style div grid that uses WAI-ARIA roles instead of <table>.
    Looks for [role="columnheader"], [role="row"], [role="cell"].
    Returns a list of {"dates":[...], "times":[...]}.
    """
    sessions = []

    # Headers → figure out column indices
    headers = scope.locator('[role="columnheader"]')
    hcount = headers.count()
    dates_idx = times_idx = None
    header_texts = []
    for i in range(hcount):
        txt = (headers.nth(i).inner_text() or "").strip()
        header_texts.append(txt)
        low = txt.lower()
        if dates_idx is None and "date" in low:
            dates_idx = i
        if times_idx is None and ("time" in low or "times" in low):
            times_idx = i

    # Hard fallback to common CivicRec positions if headers are odd/missing
    if dates_idx is None:
        dates_idx = 4  # SESSION|LOCATION|AGE|DAYS|DATES|TIMES|...
    if times_idx is None:
        times_idx = 5

    # Data rows (skip header-like rows that contain columnheaders)
    rows = scope.locator('[role="row"]')
    rcount = rows.count()
    for r in range(rcount):
        row = rows.nth(r)
        # Skip header row(s)
        if row.locator('[role="columnheader"]').count() > 0:
            continue
        cells = row.locator('[role="cell"]')
        ccount = cells.count()
        if ccount == 0:
            continue

        def safe_cell(idx):
            if idx is None or idx >= ccount:
                return ""
            try:
                return (cells.nth(idx).inner_text() or "").strip()
            except Exception:
                return ""

        dates_txt = safe_cell(dates_idx)
        times_txt = safe_cell(times_idx)

        # Run the same regex extraction on the concatenated text
        d_dates, d_times = extract_dates_times(f"{dates_txt} {times_txt}")
        if d_dates or d_times:
            sessions.append({
                "dates": d_dates or ["n/a"],
                "times": d_times or ["n/a"],
            })

    return sessions

def _wait_for_sessions_region(page, heading, timeout=4000):
    """
    After clicking the heading, wait for a sessions region to appear:
    - a real <table>,
    - an ARIA grid/table,
    - or an <iframe> that likely contains one.
    Returns a dict of locators we can try in order.
    """
    # give the DOM a moment to react
    page.wait_for_timeout(200)

    region = {
        "tables_same": heading.locator('xpath=following::*[self::table][1]'),
        "grid_same":   heading.locator('xpath=following::*[@role="grid" or @role="table"][1]'),
        "iframe_same": heading.locator('xpath=following::iframe[1]'),
    }

    # Wait, but don’t die if nothing appears quickly
    try:
        page.wait_for_timeout(200)
        if region["tables_same"].count() == 0 and region["grid_same"].count() == 0 and region["iframe_same"].count() == 0:
            page.wait_for_timeout(timeout)
    except Exception:
        pass
    return region

def _all_candidate_scopes(page, heading):
    """
    Build a list of candidate scopes to search for sessions, in priority order:
    - first few following tables in same doc,
    - first few ARIA grids in same doc,
    - first few iframes following the heading (we’ll parse inside).
    """
    scopes = []
    # up to three following tables/grids/iframes (bounded breadth)
    tables = heading.locator('xpath=following::table[position()<=3]')
    grids  = heading.locator('xpath=following::*[@role="grid" or @role="table"][position()<=3]')
    iframes = heading.locator('xpath=following::iframe[position()<=3]')

    scopes.append(("tables_same", tables))
    scopes.append(("grids_same", grids))
    scopes.append(("iframes", iframes))
    return scopes

def list_sessions_for_item(page, title):
    """
    Click the heading for `title`, then parse sessions from:
    - nearest tables,
    - nearest ARIA grids,
    - or the first few following iframes (table/grid inside).
    Returns a list of {"dates":[...], "times":[...]} (could be empty).
    """
    sessions = []
    heading = _find_heading_anywhere(page, title)
    if not heading:
        return sessions  # not listed

    # Always click to expand (some pages don’t set aria-expanded)
    try:
        heading.click(timeout=2500)
    except Exception:
        pass
    page.wait_for_timeout(350)

    # Wait for something to render near the heading (table/grid/iframe)
    _ = _wait_for_sessions_region(page, heading)

    # Build candidate scopes (bounded search)
    scopes = _all_candidate_scopes(page, heading)

    # ------ helper: parse a plain HTML table by header indices ------
    def parse_table_by_headers(tbl):
        out = []
        # ensure rendered
        try:
            tbl.wait_for(state="visible", timeout=5000)
        except Exception:
            pass

        # find header indices
        ths = tbl.locator("thead tr th")
        dates_col = times_col = None
        if ths.count() > 0:
            for i in range(ths.count()):
                h = (ths.nth(i).inner_text() or "").strip().lower()
                if "date" in h and dates_col is None:
                    dates_col = i
                if ("time" in h or "times" in h) and times_col is None:
                    times_col = i
        else:
            # fallback: use first row as header
            first = tbl.locator("tr").first.locator("th,td")
            for i in range(first.count()):
                h = (first.nth(i).inner_text() or "").strip().lower()
                if "date" in h and dates_col is None:
                    dates_col = i
                if ("time" in h or "times" in h) and times_col is None:
                    times_col = i

        # hard fallback to common CivicRec positions (0-based)
        if dates_col is None:
            dates_col = 4
        if times_col is None:
            times_col = 5

        # rows
        rows = tbl.locator("tbody tr")
        if rows.count() == 0:
            all_rows = tbl.locator("tr")
            if all_rows.count() > 1:
                rows = all_rows.nth(1)
            else:
                rows = tbl.locator("tr")

        def cell_text(row, col_idx):
            if col_idx is None: return ""
            return (row.locator(f"td:nth-child({col_idx+1})").inner_text() or "").strip()

        count = rows.count() if hasattr(rows, "count") else 0
        for i in range(count):
            r = rows.nth(i)
            dates_txt = cell_text(r, dates_col)
            times_txt = cell_text(r, times_col)
            d_dates, d_times = extract_dates_times(f"{dates_txt} {times_txt}")
            if d_dates or d_times:
                out.append({"dates": d_dates or ["n/a"], "times": d_times or ["n/a"]})
        return out

    # 1) Try plain HTML tables in the same document
    for label, loc in scopes:
        if label != "tables_same":
            continue
        count = loc.count()
        for i in range(min(3, count)):
            tbl = loc.nth(i)
            if tbl.count() == 0 or not tbl.is_visible():
                continue
            parsed = parse_table_by_headers(tbl)
            if parsed:
                sessions.extend(parsed)
        if sessions:
            sessions.sort(key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))
            return sessions

    # 2) Try ARIA grids in the same document
    for label, loc in scopes:
        if label != "grids_same":
            continue
        count = loc.count()
        for i in range(min(3, count)):
            grid = loc.nth(i)
            if grid.count() == 0 or not grid.is_visible():
                continue
            parsed = parse_aria_grid(grid)
            if parsed:
                sessions.extend(parsed)
        if sessions:
            sessions.sort(key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))
            return sessions

    # 3) Try iframes that follow the heading
    for label, loc in scopes:
        if label != "iframes":
            continue
        count = loc.count()
        for i in range(min(3, count)):
            iframe_el = loc.nth(i)
            if iframe_el.count() == 0:
                continue
            try:
                handle = iframe_el.element_handle()
                fr = handle.content_frame() if handle else None
            except Exception:
                fr = None
            if not fr:
                continue

            # Prefer table in the frame
            tbl = fr.locator("table:has(th:has-text('Dates')), table:has(th:has-text('Time'))").first
            if tbl.count() == 0:
                tbl = fr.locator("table").first

            parsed = []
            if tbl.count() > 0:
                parsed = parse_table_by_headers(tbl)
            if not parsed:
                # Try ARIA grid inside the frame
                grid = fr.locator('[role="grid"], [role="table"]').first
                if grid.count() > 0:
                    parsed = parse_aria_grid(grid)

            if parsed:
                sessions.extend(parsed)

        if sessions:
            sessions.sort(key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))
            return sessions

    # 4) Last resort: nearby text block
    try:
        block = heading.locator("xpath=following::*[self::div or self::section][1]")
        txt = block.inner_text()
    except Exception:
        txt = page.locator("body").inner_text()
    d, t = extract_dates_times(txt)
    if d or t:
        sessions.append({"dates": d or ["n/a"], "times": t or ["n/a"]})
    sessions.sort(key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))
    return sessions


def get_items_with_sessions():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        open_aquatics(page)

        items = []
        for title in TARGET_TITLES:
            # fabricate a stable inline tag; titles are fixed
            url = "inline:" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            try:
                sessions = list_sessions_for_item(page, title)
                log(f"{title}: sessions found = {len(sessions)}")
            except Exception as e:
                log(f"ERROR collecting sessions for {title}: {e}")
                sessions = []
            items.append({"title": title, "url": url, "sessions": sessions})

        browser.close()

    items.sort(key=lambda x: (x["title"].lower(), x["url"] or ""))
    return items

def load_baseline():
    if BASELINE_FILE.exists():
        try:
            return json.loads(BASELINE_FILE.read_text())
        except Exception:
            return {"items": [], "last_updated": None}
    return {"items": [], "last_updated": None}

def save_baseline(data):
    BASELINE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def _has_real_sessions(item):
    for s in item.get("sessions", []):
        if (s.get("dates") and s["dates"] != ["n/a"]) or (s.get("times") and s["times"] != ["n/a"]):
            return True
    return False

def diff_items(old_items, new_items):
    old_map = {i["title"]: i for i in old_items}
    new_map = {i["title"]: i for i in new_items}
    added, removed, changed = [], [], []
    for t in TARGET_TITLES:
        old = old_map.get(t, {"title": t, "url": None, "sessions": []})
        new = new_map.get(t, {"title": t, "url": None, "sessions": []})
        old_present = _has_real_sessions(old)
        new_present = _has_real_sessions(new)
        if not old_present and new_present:
            added.append(new)
        elif old_present and not new_present:
            removed.append(old)
        else:
            if old.get("sessions", []) != new.get("sessions", []):
                changed.append({
                    "title": t,
                    "url": new.get("url") or old.get("url"),
                    "old_sessions": old.get("sessions", []),
                    "new_sessions": new.get("sessions", []),
                })
    return added, removed, changed

def format_report(current_items, added, removed, changed):
    lines = [
        "### Aquatics Monitor - " + datetime.utcnow().isoformat() + "Z",
        "Tracking sessions (dates & times) for:",
        "- " + TARGET_TITLES[0],
        "- " + TARGET_TITLES[1],
        "",
        "**Current sessions (now):**",
    ]
    for it in current_items:
        title = it["title"]
        url = it.get("url") or "(inline)"
        lines.append(f"- {title} - {url}")
        if it.get("sessions"):
            for s in it["sessions"]:
                lines.append(f"  * dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")
        else:
            lines.append("  * (no sessions found)")

    if added:
        lines.append("")
        lines.append("**Added (now present):**")
        for a in added:
            lines.append(f"- {a['title']} - {a.get('url','')}")
            for s in a.get("sessions", []):
                lines.append(f"  * dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")

    if removed:
        lines.append("")
        lines.append("**Removed (now missing):**")
        for r in removed:
            lines.append(f"- {r['title']} - {r.get('url','')}")
            for s in r.get("sessions", []):
                lines.append(f"  * last dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")

    if changed:
        lines.append("")
        lines.append("**Changed sessions:**")
        for c in changed:
            lines.append(f"- {c['title']} - {c.get('url','')}")
            lines.append("  old:")
            for s in (c["old_sessions"] or [{"dates":["(none)"],"times":["(none)"]}]):
                lines.append(f"    * dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")
            lines.append("  new:")
            for s in (c["new_sessions"] or [{"dates":["(none)"],"times":["(none)"]}]):
                lines.append(f"    * dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")

    return "\n".join(lines)

def main():
    try:
        items = get_items_with_sessions()
        baseline = load_baseline()
        added, removed, changed = diff_items(baseline["items"], items)
        report = format_report(items, added, removed, changed)
        print(report, flush=True)
        save_baseline({"items": items, "last_updated": datetime.utcnow().isoformat()})
        # Signal change (so your email subject + red run works)
        if added or removed or changed:
            sys.exit(1)
    except Exception as e:
        # Never leave report.txt empty
        print("### Aquatics Monitor - ERROR\n\n" + str(e), flush=True)
        # Do not raise further; let workflow continue to email the error report.
        sys.exit(0)

if __name__ == "__main__":
    main()
