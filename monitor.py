# monitor.py — Clean production version
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

def extract_dates_times(text: str):
    dates = set(DATE_RANGE.findall(text))
    if not dates:
        dates = set(DATE_SINGLE.findall(text))
    times = set(TIME_RANGE.findall(text))
    if not times:
        times = set(TIME_SINGLE.findall(text))
    return sorted(dates), sorted(times)

def open_aquatics(page):
    page.goto(CATALOG_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    
    # Click Aquatics category
    for label in ["Aquatics Programs", "Aquatics"]:
        loc = page.locator(f"text={label}")
        if loc.count():
            try:
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                break
            except:
                pass
    
    # Scroll to load content
    for i in range(15):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(150)

def _frames(page):
    fr = [page]
    for f in page.frames:
        try:
            if "secure.rec1.com" in (f.url or ""):
                fr.append(f)
        except:
            continue
    return fr

def _find_heading_anywhere(page, title):
    """Find the visible heading element containing the title text."""
    patt = re.compile(re.escape(title), re.I)
    for scope in _frames(page):
        link = scope.get_by_role("link", name=patt)
        if link.count() > 0:
            return link.first
        el = scope.get_by_text(patt).first
        if el.count() > 0:
            return el
    return None

def parse_table_by_headers(tbl):
    """Parse a plain HTML table that has session data."""
    out = []
    try:
        tbl.wait_for(state="visible", timeout=3000)
    except:
        pass

    try:
        # Find date and time columns
        ths = tbl.locator("thead tr th, tr th")
        dates_col = times_col = None

        if ths.count() > 0:
            for i in range(ths.count()):
                h = (ths.nth(i).inner_text() or "").strip().lower()
                if dates_col is None and "date" in h:
                    dates_col = i
                if times_col is None and ("time" in h or "times" in h):
                    times_col = i

        # Fallback to typical CivicRec column order
        if dates_col is None:
            dates_col = 4
        if times_col is None:
            times_col = 5

        # Get data rows
        rows = tbl.locator("tbody tr")
        if rows.count() == 0:
            all_rows = tbl.locator("tr")
            if all_rows.count() > 1:
                rows = tbl.locator("tr:not(:first-child)")
            else:
                rows = tbl.locator("tr")

        def cell_text(row, idx):
            if idx is None:
                return ""
            try:
                cell = row.locator(f"td:nth-child({idx+1}), th:nth-child({idx+1})")
                return (cell.inner_text() or "").strip()
            except:
                return ""

        # Parse each row
        n = rows.count()
        for i in range(n):
            r = rows.nth(i)
            dates_txt = cell_text(r, dates_col)
            times_txt = cell_text(r, times_col)
            d_dates, d_times = extract_dates_times(f"{dates_txt} {times_txt}")
            if d_dates or d_times:
                out.append({"dates": d_dates or ["n/a"], "times": d_times or ["n/a"]})
    except:
        pass
    
    return out

def list_sessions_for_item(page, title):
    """Click the program title to open a modal, then parse the session table."""
    sessions = []
    modal_found = False
    
    heading = _find_heading_anywhere(page, title)
    if not heading:
        return sessions
    
    try:
        # Click to open modal
        heading.click(timeout=3000)
        page.wait_for_timeout(3000)
        
        # STRATEGY 1: Check all visible iframes for session tables
        all_iframes = page.locator("iframe")
        for i in range(all_iframes.count()):
            iframe = all_iframes.nth(i)
            try:
                if iframe.is_visible():
                    handle = iframe.element_handle()
                    fr = handle.content_frame() if handle else None
                    if fr:
                        iframe_tables = fr.locator("table")
                        for t in range(iframe_tables.count()):
                            tbl = iframe_tables.nth(t)
                            text = tbl.inner_text()
                            if len(text) > 100 and "DATES" in text.upper() and "TIMES" in text.upper():
                                parsed = parse_table_by_headers(tbl)
                                if parsed:
                                    sessions.extend(parsed)
                                    modal_found = True
                                    break
                        if modal_found:
                            break
            except:
                pass
        
        # STRATEGY 2: Check all tables on main page
        if not modal_found:
            tables = page.locator("table")
            for i in range(tables.count()):
                tbl = tables.nth(i)
                try:
                    text = tbl.inner_text()
                    if len(text) < 100:
                        continue
                    
                    if "DATES" in text.upper() and "TIMES" in text.upper():
                        # Verify this table belongs to our program
                        parent = tbl.locator("xpath=ancestor::*[self::div or self::section][1]")
                        if parent.count() > 0:
                            parent_text = parent.inner_text()
                            if title.lower() not in parent_text.lower():
                                continue
                        
                        parsed = parse_table_by_headers(tbl)
                        if parsed:
                            sessions.extend(parsed)
                            modal_found = True
                            break
                except:
                    pass
        
        # STRATEGY 3: Check for proper modal containers
        if not modal_found:
            modals = page.locator('[class*="modal"][class*="show"], [class*="modal"][style*="display: block"], [role="dialog"]')
            
            for i in range(modals.count()):
                try:
                    modal = modals.nth(i)
                    if not modal.is_visible():
                        continue
                    
                    text = modal.inner_text()
                    
                    # Must contain title AND must NOT be navigation
                    if title.lower() not in text.lower():
                        continue
                    if "Clear All Filters" in text or "Log In with Email" in text[:200]:
                        continue
                    
                    # Look for table in this modal
                    tbl = modal.locator("table").first
                    if tbl.count() > 0:
                        tbl_text = tbl.inner_text()
                        if len(tbl_text) > 100 and "DATES" in tbl_text.upper():
                            parsed = parse_table_by_headers(tbl)
                            if parsed:
                                sessions.extend(parsed)
                                modal_found = True
                                break
                except:
                    pass
        
        # Close modal
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except:
            pass
        
    except:
        pass
    
    sessions.sort(key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))
    return sessions

def get_items_with_sessions():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        ctx = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
        )
        page = ctx.new_page()
        
        # Hide webdriver property
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        open_aquatics(page)

        items = []
        for title in TARGET_TITLES:
            url = "inline:" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            try:
                sessions = list_sessions_for_item(page, title)
            except:
                sessions = []
            items.append({"title": title, "url": url, "sessions": sessions})
            page.wait_for_timeout(1000)

        browser.close()

    items.sort(key=lambda x: (x["title"].lower(), x["url"] or ""))
    return items

def load_baseline():
    if BASELINE_FILE.exists():
        try:
            return json.loads(BASELINE_FILE.read_text())
        except:
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
        
        # Exit 1 if changes detected (triggers workflow alert)
        if added or removed or changed:
            sys.exit(1)
    except Exception as e:
        print("### Aquatics Monitor - ERROR\n\n" + str(e), flush=True)
        import traceback
        print("\n" + traceback.format_exc(), flush=True)
        sys.exit(0)

if __name__ == "__main__":
    main()