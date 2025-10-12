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
    page.wait_for_timeout(2000)
    
    # Try clicking the category tab if present
    for label in ["Aquatics Programs", "Aquatics"]:
        loc = page.locator(f"text={label}")
        if loc.count():
            log(f"clicking category: {label}")
            try:
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1200)
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
        try:
            tbl.wait_for(state="visible", timeout=3000)
        except Exception:
            pass

        # Header cells
        ths = tbl.locator("thead tr th, tr th")
        dates_col = times_col = None

        if ths.count() > 0:
            for i in range(ths.count()):
                h = (ths.nth(i).inner_text() or "").strip().lower()
                if dates_col is None and "date" in h:
                    dates_col = i
                if times_col is None and ("time" in h or "times" in h):
                    times_col = i

        # Hard fallback to CivicRec's typical column order
        if dates_col is None:
            dates_col = 4  # DATES is usually 5th column (0-indexed = 4)
        if times_col is None:
            times_col = 5  # TIMES is usually 6th column (0-indexed = 5)

        log(f"[DEBUG] Using dates_col={dates_col}, times_col={times_col}")

        # Data rows
        rows = tbl.locator("tbody tr")
        if rows.count() == 0:
            # No tbody, try all rows except first (header)
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

def list_sessions_for_item(page, title):
    """
    Click the program title to open a modal, then parse the session table in the modal.
    """
    sessions = []
    
    # Find and click the heading
    heading = _find_heading_anywhere(page, title)
    if not heading:
        log(f"[DEBUG] Heading not found for: {title}")
        return sessions
    
    log(f"[DEBUG] Found heading for: {title}, clicking to open modal")
    
    try:
        # Click to open modal
        heading.click(timeout=3000)
        
        # Wait longer for modal to appear and animate
        page.wait_for_timeout(3000)
        
        log("[DEBUG] Clicked, waiting for modal to appear")
        
        # The modal might take a moment to become visible
        # Look for any element that appeared and contains a table with our title
        
        # Strategy 1: Find any visible element containing the title AND a table
        # The modal should have both the program title and the session table
        modal_found = False
        modal_element = None
        
        # Try multiple selector strategies
        modal_selectors = [
            f"text={title}",  # Any element with the title
            '[class*="modal"]',
            '[class*="dialog"]',
            '[class*="overlay"]',
            '[class*="popup"]',
            'div[style*="z-index"]',  # Modals often have high z-index
        ]
        
        for selector in modal_selectors:
            elements = page.locator(selector)
            for i in range(elements.count()):
                elem = elements.nth(i)
                try:
                    if elem.is_visible():
                        text = elem.inner_text()
                        # Check if this element contains both the title and table data
                        if title.lower() in text.lower() and ("SESSION" in text or "DATES" in text or "TIMES" in text):
                            log(f"[DEBUG] Found modal element with selector: {selector}")
                            modal_element = elem
                            modal_found = True
                            break
                except:
                    continue
            if modal_found:
                break
        
        # Strategy 2: If no modal found above, look for tables that appeared recently
        if not modal_found:
            log("[DEBUG] Modal not found with title, looking for any new visible table")
            tables = page.locator("table")
            for i in range(tables.count()):
                tbl = tables.nth(i)
                try:
                    if tbl.is_visible():
                        text = tbl.inner_text()
                        # Check if table has session-like headers
                        if "DATES" in text and "TIMES" in text:
                            log(f"[DEBUG] Found table {i} with DATES and TIMES columns")
                            # Use the table's parent as the modal
                            modal_element = tbl.locator("xpath=ancestor::*[self::div or self::section][1]")
                            if modal_element.count() > 0:
                                modal_element = modal_element.first
                                modal_found = True
                                break
                except:
                    continue
        
        if modal_found and modal_element:
            log(f"[DEBUG] Modal found, extracting sessions")
            
            # Look for the table in the modal
            table = modal_element.locator("table").first
            if table.count() > 0:
                log(f"[DEBUG] Found table in modal")
                sessions = parse_table_by_headers(table)
                log(f"[DEBUG] Parsed {len(sessions)} sessions from table")
            else:
                log(f"[DEBUG] No table found in modal, trying text extraction")
                # Fallback: extract dates/times from modal text
                text = modal_element.inner_text()
                d_dates, d_times = extract_dates_times(text)
                if d_dates or d_times:
                    sessions.append({"dates": d_dates or ["n/a"], "times": d_times or ["n/a"]})
        else:
            log(f"[DEBUG] Modal not found for {title}")
        
        # Close the modal by pressing Escape or clicking X
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            log("[DEBUG] Closed modal with Escape")
        except:
            # Try finding and clicking the X button
            try:
                close_btn = page.locator('button:has-text("×"), button:has-text("X"), [class*="close"]').first
                if close_btn.count() > 0 and close_btn.is_visible():
                    close_btn.click()
                    page.wait_for_timeout(500)
                    log("[DEBUG] Closed modal with X button")
            except:
                log("[DEBUG] Could not close modal, continuing anyway")
        
    except Exception as e:
        log(f"[DEBUG] Error processing {title}: {e}")
        import traceback
        log(f"[DEBUG] Traceback: {traceback.format_exc()}")
    
    sessions.sort(key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))
    log(f"[DEBUG] Total sessions found for {title}: {len(sessions)}")
    return sessions

def get_items_with_sessions():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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
            
            # Wait between items
            page.wait_for_timeout(1000)

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
        sys.exit(0)

if __name__ == "__main__":
    main()