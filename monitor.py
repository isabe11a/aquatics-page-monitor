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

def list_sessions_for_item(page, title):
    """
    Strategy: find the element with the title text (any tag).
    Then take the **next table in the DOM**; if not present,
    take the **first table in the next iframe**.
    """
    sessions = []
    heading = _find_heading_anywhere(page, title)
    if not heading:
        return sessions  # treat as not listed

    # Try to expand if collapsible
    try:
        ae = heading.get_attribute("aria-expanded")
        if ae == "false":
            heading.click(timeout=2000)
            page.wait_for_timeout(300)
    except Exception:
        pass

    # 1) Next table after heading (same document)
    next_table = heading.locator("xpath=following::table[1]").first
    if next_table.count() > 0 and next_table.is_visible():
        log(f"next table found for {title} (same doc)")
        rows = next_table.locator("tbody tr")
        if rows.count() == 0:
            rows = next_table.locator("tr").nth(1)
        for i in range(rows.count()):
            txt = rows.nth(i).inner_text()
            d, t = extract_dates_times(txt)
            sessions.append({"dates": d or ["n/a"], "times": t or ["n/a"]})
        return sorted(sessions, key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))

    # 2) Next iframe after heading -> first table inside it
    next_iframe = heading.locator("xpath=following::iframe[1]").first
    if next_iframe.count() > 0:
        log(f"iframe found for {title}")
        try:
            handle = next_iframe.element_handle()
            fr = handle.content_frame() if handle else None
        except Exception as e:
            log(f"warn: content_frame failed: {e}")
            fr = None
        if fr:
            tbl = fr.locator("table:has(th:has-text('Dates')), table:has(th:has-text('Time'))").first
            if tbl.count() == 0:
                tbl = fr.locator("table").first
            if tbl.count() > 0:
                rows = tbl.locator("tbody tr")
                if rows.count() == 0:
                    rows = tbl.locator("tr").nth(1)
                for i in range(rows.count()):
                    txt = rows.nth(i).inner_text()
                    d, t = extract_dates_times(txt)
                    sessions.append({"dates": d or ["n/a"], "times": t or ["n/a"]})
                return sorted(sessions, key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))

    # 3) Last resort: parse nearby block text
    try:
        block = heading.locator("xpath=following::*[self::div or self::section][1]")
        txt = block.inner_text()
    except Exception:
        txt = page.locator("body").inner_text()
    d, t = extract_dates_times(txt)
    sessions.append({"dates": d or ["n/a"], "times": t or ["n/a"]})
    return sorted(sessions, key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))

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

def diff_items(old_items, new_items):
    old_map = {i["title"]: i for i in old_items}
    new_map = {i["title"]: i for i in new_items}
    added, removed, changed = [], [], []
    titles = set(TARGET_TITLES)

    for t in titles:
        old = old_map.get(t, {"title": t, "url": None, "sessions": []})
        new = new_map.get(t, {"title": t, "url": None, "sessions": []})
        old_present = bool(old.get("sessions"))
        new_present = bool(new.get("sessions"))

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
