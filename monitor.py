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
    
    # Count tables before clicking
    tables_before = page.locator("table").count()
    log(f"[DEBUG] Tables on page before click: {tables_before}")
    
    try:
        # Click to open modal
        heading.click(timeout=3000)
        log("[DEBUG] Clicked successfully")
        
        # Wait for modal to animate and content to load
        page.wait_for_timeout(2000)
        
        # Check if any iframes appeared or became visible
        iframes_before = page.locator("iframe").count()
        log(f"[DEBUG] Iframes before: {iframes_before}")
        
        # Wait for either new tables OR new/visible iframes
        try:
            page.wait_for_function(
                f"document.querySelectorAll('table').length > {tables_before} || "
                f"document.querySelectorAll('iframe').length > {iframes_before}",
                timeout=5000
            )
            log("[DEBUG] New content appeared (table or iframe)")
        except Exception as e:
            log(f"[DEBUG] No new tables/iframes appeared: {e}")
        
        page.wait_for_timeout(2000)
        
        # Count tables and iframes after clicking
        tables_after = page.locator("table").count()
        iframes_after = page.locator("iframe").count()
        log(f"[DEBUG] Tables: {tables_before} -> {tables_after}")
        log(f"[DEBUG] Iframes: {iframes_before} -> {iframes_after}")
        
        # Check if any iframes became visible
        all_iframes = page.locator("iframe")
        for i in range(all_iframes.count()):
            iframe = all_iframes.nth(i)
            try:
                is_visible = iframe.is_visible()
                if is_visible:
                    src = iframe.get_attribute("src") or "no-src"
                    log(f"[DEBUG] Iframe {i} is visible: src={src[:100]}")
                    
                    # Try to access this iframe
                    handle = iframe.element_handle()
                    fr = handle.content_frame() if handle else None
                    if fr:
                        log(f"[DEBUG] Checking iframe {i} for tables...")
                        iframe_tables = fr.locator("table")
                        iframe_table_count = iframe_tables.count()
                        log(f"[DEBUG] Iframe {i} has {iframe_table_count} tables")
                        
                        if iframe_table_count > 0:
                            # Check if these tables have session data
                            for t in range(iframe_table_count):
                                tbl = iframe_tables.nth(t)
                                try:
                                    text = tbl.inner_text()
                                    log(f"[DEBUG] Iframe {i} table {t} text length: {len(text)}")
                                    if len(text) > 100:
                                        log(f"[DEBUG] Iframe {i} table {t} preview: {text[:200]}")
                                        if "DATES" in text.upper() and "TIMES" in text.upper():
                                            log(f"[DEBUG] ✓ Iframe {i} table {t} has session columns!")
                                            parsed = parse_table_by_headers(tbl)
                                            if parsed:
                                                log(f"[DEBUG] ✓ Parsed {len(parsed)} sessions from iframe table")
                                                sessions.extend(parsed)
                                                modal_found = True
                                                break
                                except Exception as e:
                                    log(f"[DEBUG] Error reading iframe table: {e}")
                        
                        if modal_found:
                            break
            except Exception as e:
                log(f"[DEBUG] Error checking iframe {i}: {e}")
        
        # Strategy 1: Check iframes first (if we found any visible ones above)
        if not modal_found and iframes_after > iframes_before:
            log("[DEBUG] New iframe appeared, already checked above")
        
        # Strategy 2: Check ALL tables on the page
        if not modal_found:
            tables = page.locator("table")
            
            for i in range(tables.count()):
                tbl = tables.nth(i)
                try:
                    # Get the text content regardless of visibility
                    text = tbl.inner_text()
                    log(f"[DEBUG] Table {i} text length: {len(text)}")
                    
                    # Skip tables that are too small (under 100 chars can't be a session table)
                    if len(text) < 100:
                        log(f"[DEBUG] Table {i} too small, skipping")
                        continue
                    
                    log(f"[DEBUG] Table {i} preview: {text[:200]}")
                    
                    # Check if this table has session headers (SESSION, DATES, TIMES columns)
                    text_upper = text.upper()
                    has_session_col = "SESSION" in text_upper
                    has_dates_col = "DATES" in text_upper or "DATE" in text_upper
                    has_times_col = "TIMES" in text_upper or "TIME" in text_upper
                    
                    log(f"[DEBUG] Table {i} - SESSION:{has_session_col}, DATES:{has_dates_col}, TIMES:{has_times_col}")
                    
                    if has_dates_col and has_times_col:
                        log(f"[DEBUG] ✓ Table {i} has session data columns!")
                        
                        # Verify it's actually our program by checking if title appears near this table
                        # Get the parent container
                        parent = tbl.locator("xpath=ancestor::*[self::div or self::section][1]")
                        if parent.count() > 0:
                            parent_text = parent.inner_text()
                            if title.lower() not in parent_text.lower():
                                log(f"[DEBUG] Table {i} doesn't belong to our program (title not in parent)")
                                continue
                        
                        # Try to parse this table
                        parsed = parse_table_by_headers(tbl)
                        if parsed:
                            log(f"[DEBUG] ✓ Successfully parsed {len(parsed)} sessions from table {i}")
                            sessions.extend(parsed)
                            modal_found = True
                            break
                        else:
                            log(f"[DEBUG] Table {i} has headers but parsing returned no sessions")
                except Exception as e:
                    log(f"[DEBUG] Error checking table {i}: {e}")
        
        # Strategy 3: Look for modal containers (but be VERY strict about what we accept)
        if not modal_found:
            log("[DEBUG] No table found, searching for modal containers")
            
            # Look for containers that likely represent a modal
            modal_candidates = page.locator(
                '[class*="modal"], [class*="dialog"], [class*="overlay"], '
                '[role="dialog"], [aria-modal="true"]'
            )
            
            log(f"[DEBUG] Found {modal_candidates.count()} modal-like containers")
            
            for i in range(modal_candidates.count()):
                try:
                    container = modal_candidates.nth(i)
                    
                    # Must be visible
                    if not container.is_visible():
                        continue
                    
                    text = container.inner_text()
                    
                    # Must contain the program title
                    if title.lower() not in text.lower():
                        log(f"[DEBUG] Modal candidate {i} doesn't contain title")
                        continue
                    
                    # Must NOT be the navigation/filter panel (reject if it has "Filter" or "Cart" near the top)
                    if "Clear All Filters" in text[:500] or "Log In with Email" in text[:500]:
                        log(f"[DEBUG] Modal candidate {i} is navigation panel, rejecting")
                        continue
                    
                    # Must have substantial session-related content
                    if len(text) < 300:
                        log(f"[DEBUG] Modal candidate {i} too short")
                        continue
                    
                    log(f"[DEBUG] Modal candidate {i} looks promising, checking for dates/times")
                    log(f"[DEBUG] Preview: {text[:500]}")
                    
                    dates, times = extract_dates_times(text)
                    if dates and times and len(dates) > 0 and len(times) > 0:
                        log(f"[DEBUG] ✓ Modal {i} has {len(dates)} dates and {len(times)} times")
                        
                        # Parse it properly - look for a table inside this modal
                        tbl_in_modal = container.locator("table").first
                        if tbl_in_modal.count() > 0:
                            log(f"[DEBUG] Found table inside modal {i}, parsing...")
                            parsed = parse_table_by_headers(tbl_in_modal)
                            if parsed:
                                sessions.extend(parsed)
                                modal_found = True
                                break
                        
                        # Fallback: manually pair dates with times
                        if not modal_found:
                            log(f"[DEBUG] No table in modal, using text extraction")
                            sessions.append({"dates": dates, "times": times})
                            modal_found = True
                            break
                except Exception as e:
                    log(f"[DEBUG] Error with modal candidate {i}: {e}")
        
        if not modal_found:
            log(f"[DEBUG] Could not find session data for {title}")
            log(f"[DEBUG] This might mean:")
            log(f"[DEBUG] - The modal didn't open (check if site detects headless mode)")
            log(f"[DEBUG] - Session data loads in a way we haven't detected")
            log(f"[DEBUG] - The program has no available sessions")
        
        if not modal_found:
            log(f"[DEBUG] Modal/table not found for {title}")
        
        # Close the modal by pressing Escape or clicking X
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            log("[DEBUG] Closed modal with Escape")
        except:
            # Try finding and clicking the X button
            try:
                close_btn = page.locator('button:has-text("×"), button:has-text("X"), [class*="close"], [aria-label="Close"]').first
                if close_btn.count() > 0:
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