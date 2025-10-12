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
    page.wait_for_timeout(2000)  # Increased wait time
    
    # Try clicking the category tab if present
    for label in ["Aquatics Programs", "Aquatics"]:
        loc = page.locator(f"text={label}")
        if loc.count():
            log(f"clicking category: {label}")
            try:
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1200)  # More time after clicking
                break
            except Exception as e:
                log(f"warn: category click failed: {e}")
    
    # Soft scroll to load content
    for i in range(15):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(150)
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
    """Find the visible heading element containing the title text."""
    patt = re.compile(re.escape(title), re.I)
    for scope in _frames(page):
        # Try role=link first (fast path)
        link = scope.get_by_role("link", name=patt)
        if link.count() > 0:
            log(f"[DEBUG] heading found as link in scope")
            return link.first
        # Generic text search
        el = scope.get_by_text(patt).first
        if el.count() > 0:
            log(f"[DEBUG] heading found by text in scope")
            return el
    log(f"[DEBUG] heading NOT found: {title}")
    return None

def parse_table_by_headers(tbl):
    """Parse a plain HTML table that has session data."""
    out = []
    try:
        try:
            tbl.wait_for(state="visible", timeout=3000)
        except Exception:
            pass

        # Header cells
        ths = tbl.locator("thead tr th")
        dates_col = times_col = None

        if ths.count() > 0:
            for i in range(ths.count()):
                h = (ths.nth(i).inner_text() or "").strip().lower()
                if dates_col is None and "date" in h:
                    dates_col = i
                if times_col is None and ("time" in h or "times" in h):
                    times_col = i
        else:
            # Fallback: first row as header
            first = tbl.locator("tr").first.locator("th,td")
            for i in range(first.count()):
                h = (first.nth(i).inner_text() or "").strip().lower()
                if dates_col is None and "date" in h:
                    dates_col = i
                if times_col is None and ("time" in h or "times" in h):
                    times_col = i

        # Hard fallback to CivicRec's typical column order (0-based)
        if dates_col is None:
            dates_col = 4
        if times_col is None:
            times_col = 5

        log(f"[DEBUG] Using dates_col={dates_col}, times_col={times_col}")

        # Data rows
        rows = tbl.locator("tbody tr")
        if rows.count() == 0:
            all_rows = tbl.locator("tr")
            if all_rows.count() > 1:
                rows = all_rows.nth(1)
            else:
                rows = tbl.locator("tr")

        def cell_text(row, idx):
            if idx is None:
                return ""
            try:
                return (row.locator(f"td:nth-child({idx+1})").inner_text() or "").strip()
            except Exception:
                return ""

        n = rows.count()
        log(f"[DEBUG] Processing {n} table rows")
        for i in range(n):
            r = rows.nth(i)
            dates_txt = cell_text(r, dates_col)
            times_txt = cell_text(r, times_col)
            log(f"[DEBUG] Row {i}: dates='{dates_txt}', times='{times_txt}'")
            d_dates, d_times = extract_dates_times(f"{dates_txt} {times_txt}")
            if d_dates or d_times:
                out.append({"dates": d_dates or ["n/a"], "times": d_times or ["n/a"]})
    except Exception as e:
        log(f"[DEBUG] Error parsing table: {e}")
    return out

def parse_aria_grid(scope):
    """Parse a CivicRec-style ARIA grid."""
    sessions = []

    headers = scope.locator('[role="columnheader"]')
    hcount = headers.count()
    dates_idx = times_idx = None
    for i in range(hcount):
        txt = (headers.nth(i).inner_text() or "").strip().lower()
        if dates_idx is None and "date" in txt:
            dates_idx = i
        if times_idx is None and ("time" in txt or "times" in txt):
            times_idx = i
    if dates_idx is None:
        dates_idx = 4
    if times_idx is None:
        times_idx = 5

    log(f"[DEBUG] ARIA grid using dates_idx={dates_idx}, times_idx={times_idx}")

    rows = scope.locator('[role="row"]')
    rcount = rows.count()
    log(f"[DEBUG] Processing {rcount} ARIA grid rows")
    
    for r in range(rcount):
        row = rows.nth(r)
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
        log(f"[DEBUG] ARIA row {r}: dates='{dates_txt}', times='{times_txt}'")
        d_dates, d_times = extract_dates_times(f"{dates_txt} {times_txt}")
        if d_dates or d_times:
            sessions.append({"dates": d_dates or ["n/a"], "times": d_times or ["n/a"]})

    return sessions

def parse_detail_page_fallback(scope):
    """Last-resort parser for detail pages: scan visible text."""
    try:
        txt = scope.locator("body").inner_text()
        log(f"[DEBUG] Fallback parsing {len(txt)} chars of body text")
    except Exception:
        return []
    d_dates, d_times = extract_dates_times(txt)
    if d_dates or d_times:
        log(f"[DEBUG] Fallback found dates={d_dates}, times={d_times}")
        return [{"dates": d_dates or ["n/a"], "times": d_times or ["n/a"]}]
    return []

def list_sessions_for_item(page, title):
    """
    Click to expand the item on the main page and parse sessions directly,
    without navigating to a separate detail page.
    """
    sessions = []
    
    # Find and click the heading to expand
    heading = _find_heading_anywhere(page, title)
    if not heading:
        log(f"[DEBUG] Heading not found for: {title}")
        return sessions
    
    log(f"[DEBUG] Found heading for: {title}, attempting to click")
    
    try:
        # Click to expand
        heading.click(timeout=3000)
        page.wait_for_timeout(2000)  # Wait for expansion animation and content load
        log("[DEBUG] Clicked to expand, waiting for content")
        
        # Wait for session content to appear (table, grid, or iframe)
        try:
            page.wait_for_selector("table, [role='grid'], iframe", timeout=5000, state="visible")
            log("[DEBUG] Session content appeared")
        except Exception:
            log("[DEBUG] Timeout waiting for session content, proceeding anyway")
        
        # Find the container that was expanded
        container = heading.locator(
            "xpath=ancestor::*[self::div or self::section or self::article or self::li]"
            "[contains(@class,'item') or contains(@class,'card') or contains(@class,'program') "
            "or contains(@class,'expandable') or contains(@class,'row')][1]"
        )
        
        if container.count() == 0:
            container = heading.locator("xpath=ancestor::*[self::div or self::section or self::article][1]")
        
        log(f"[DEBUG] Container found: {container.count()}")
        
        if container.count() > 0:
            cont = container.first
            
            # Method 1: Look for tables within this container
            tables = cont.locator("table")
            log(f"[DEBUG] Tables in container: {tables.count()}")
            
            for i in range(tables.count()):
                tbl = tables.nth(i)
                try:
                    if tbl.is_visible():
                        log(f"[DEBUG] Parsing table {i}")
                        parsed = parse_table_by_headers(tbl)
                        if parsed:
                            log(f"[DEBUG] Found {len(parsed)} sessions in table {i}")
                            sessions.extend(parsed)
                except Exception as e:
                    log(f"[DEBUG] Error with table {i}: {e}")
            
            # Method 2: Look for ARIA grids
            if not sessions:
                grids = cont.locator('[role="grid"], [role="table"]')
                log(f"[DEBUG] ARIA grids in container: {grids.count()}")
                
                for i in range(grids.count()):
                    grid = grids.nth(i)
                    try:
                        if grid.is_visible():
                            log(f"[DEBUG] Parsing ARIA grid {i}")
                            parsed = parse_aria_grid(grid)
                            if parsed:
                                log(f"[DEBUG] Found {len(parsed)} sessions in ARIA grid {i}")
                                sessions.extend(parsed)
                    except Exception as e:
                        log(f"[DEBUG] Error with ARIA grid {i}: {e}")
            
            # Method 3: Look for iframes within the container
            if not sessions:
                iframes = cont.locator("iframe")
                log(f"[DEBUG] Iframes in container: {iframes.count()}")
                
                for i in range(iframes.count()):
                    try:
                        iframe = iframes.nth(i)
                        iframe.wait_for(state="visible", timeout=3000)
                        handle = iframe.element_handle()
                        fr = handle.content_frame() if handle else None
                        
                        if fr:
                            log(f"[DEBUG] Processing iframe {i}")
                            page.wait_for_timeout(1000)  # Let iframe content load
                            
                            # Try table in iframe
                            tbl = fr.locator("table").first
                            if tbl.count() > 0:
                                log(f"[DEBUG] Found table in iframe {i}")
                                parsed = parse_table_by_headers(tbl)
                                if parsed:
                                    log(f"[DEBUG] Found {len(parsed)} sessions in iframe table")
                                    sessions.extend(parsed)
                            
                            # Try ARIA grid in iframe
                            if not sessions:
                                grid = fr.locator('[role="grid"], [role="table"]').first
                                if grid.count() > 0:
                                    log(f"[DEBUG] Found ARIA grid in iframe {i}")
                                    parsed = parse_aria_grid(grid)
                                    if parsed:
                                        log(f"[DEBUG] Found {len(parsed)} sessions in iframe ARIA grid")
                                        sessions.extend(parsed)
                    except Exception as e:
                        log(f"[DEBUG] Error processing iframe {i}: {e}")
            
            # Method 4: Fallback to text parsing in the container
            if not sessions:
                try:
                    text = cont.inner_text()
                    log(f"[DEBUG] Container text length: {len(text)}")
                    if len(text) > 100:
                        log(f"[DEBUG] Container text preview: {text[:300]}")
                    d_dates, d_times = extract_dates_times(text)
                    if d_dates or d_times:
                        sessions.append({"dates": d_dates or ["n/a"], "times": d_times or ["n/a"]})
                        log(f"[DEBUG] Found dates/times via text parsing: {d_dates}, {d_times}")
                except Exception as e:
                    log(f"[DEBUG] Text parsing error: {e}")
        
        # Additional fallback: search the entire page for visible tables after the click
        if not sessions:
            log("[DEBUG] Trying page-wide search for sessions")
            
            all_tables = page.locator("table")
            log(f"[DEBUG] Total tables on page: {all_tables.count()}")
            
            for i in range(all_tables.count()):
                tbl = all_tables.nth(i)
                try:
                    if tbl.is_visible():
                        parsed = parse_table_by_headers(tbl)
                        if parsed and any(s.get("dates", []) != ["n/a"] for s in parsed):
                            log(f"[DEBUG] Found relevant sessions in page table {i}")
                            sessions.extend(parsed)
                            break  # Take first valid one
                except Exception as e:
                    log(f"[DEBUG] Error checking table {i}: {e}")
        
    except Exception as e:
        log(f"[DEBUG] Error expanding/parsing {title}: {e}")
        import traceback
        log(f"[DEBUG] Traceback: {traceback.format_exc()}")
    
    sessions.sort(key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))
    log(f"[DEBUG] Total sessions found for {title}: {len(sessions)}")
    return sessions

def get_items_with_sessions():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # FIXED: Set back to headless
        ctx = browser.new_context()
        page = ctx.new_page()
        open_aquatics(page)

        items = []
        for title in TARGET_TITLES:
            url = "inline:" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            try:
                sessions = list_sessions_for_item(page, title)
                log(f"{title}: sessions found = {len(sessions)}")
            except Exception as e:
                log(f"ERROR collecting sessions for {title}: {e}")
                import traceback
                log(traceback.format_exc())
                sessions = []
            items.append({"title": title, "url": url, "sessions": sessions})
            
            # Wait between items and scroll back up for next item
            page.wait_for_timeout(1000)
            page.mouse.wheel(0, -5000)  # Scroll back up
            page.wait_for_timeout(500)

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
        import traceback
        print("\n" + traceback.format_exc(), flush=True)
        # Do not raise further; let workflow continue to email the error report.
        sys.exit(0)

if __name__ == "__main__":
    main()