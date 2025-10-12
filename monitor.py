# monitor.py
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

CATALOG_URL = "https://secure.rec1.com/CA/calabasas-ca/catalog/index"
BASELINE_FILE = Path("baseline.json")

# Limit to just these two classes
TARGET_TITLES = [
    "Swim Lesson Level 1: Baby Pups & Parent Seals",
    "Swim Lesson Level 2: Sea Horses",
]

# Helpful keywords as a fallback filter on the list page
AQUATICS_KEYWORDS = ["Aquatics", "Swim", "Swimming", "Aquatic"]

# Regexes for dates/times commonly used by CivicRec
DATE_RANGE = re.compile(r"\b\d{1,2}/\d{1,2}\s*[-–]\s*\d{1,2}/\d{1,2}\b")
DATE_SINGLE = re.compile(r"\b\d{1,2}/\d{1,2}\b")
TIME_RANGE = re.compile(r"\b\d{1,2}:\d{2}\s*[AP]M\s*[-–]\s*\d{1,2}:\d{2}\s*[AP]M\b", re.I)
TIME_SINGLE = re.compile(r"\b\d{1,2}:\d{2}\s*[AP]M\b", re.I)

def extract_dates_times(text: str):
    """Return sorted unique lists of date snippets and time snippets."""
    dates = set(DATE_RANGE.findall(text))
    if not dates:
        dates = set(DATE_SINGLE.findall(text))
    times = set(TIME_RANGE.findall(text))
    if not times:
        times = set(TIME_SINGLE.findall(text))
    return sorted(dates), sorted(times)

def open_aquatics(page):
    """Navigate to the Aquatics catalog view (category or search)."""
    page.goto(CATALOG_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    # Try category buttons
    for label in ["Aquatics Programs", "Aquatics"]:
        loc = page.locator(f"text={label}")
        if loc.count() > 0:
            try:
                loc.first.click(timeout=2000)
                page.wait_for_timeout(1200)
                return
            except:
                pass
    # Fallback to search
    for placeholder in ["Keyword or code", "Search"]:
        try:
            s = page.get_by_placeholder(placeholder)
            s.fill("Aquatics")
            s.press("Enter")
            page.wait_for_timeout(1500)
            return
        except:
            continue

def click_item_by_title(page, title):
    """Open an item by its link text (exact first, then case-insensitive partial)."""
    link = page.get_by_role("link", name=title, exact=True)
    if link.count() == 0:
        link = page.get_by_role("link", name=re.compile(re.escape(title), re.I))
    link.first.click(timeout=5000)
    page.wait_for_timeout(1200)

def get_catalog_frame(page):
    """
    CivicRec sometimes renders detail content in an <iframe>.
    Prefer a frame whose URL contains secure.rec1.com; else fall back to the page.
    """
    # Give frames a moment to mount
    page.wait_for_timeout(600)
    for f in page.frames:
        try:
            if "secure.rec1.com" in (f.url or ""):
                return f
        except Exception:
            pass
    return page  # fallback

def list_sessions_for_item(page, title):
    """Return a normalized sessions list for a given item (list of dicts with dates/times)."""
    click_item_by_title(page, title)
    frame = get_catalog_frame(page)

    # Prefer a structured table with Dates/Time headers
    table = frame.locator("table:has(th:has-text('Dates')), table:has(th:has-text('Time'))")
    sessions = []

    if table.count() > 0:
        # Grab all rows under table body (fallback to all <tr> if needed)
        rows = table.locator("tbody tr")
        if rows.count() == 0:
            rows = table.locator("tr").nth(1)  # skip header if no <tbody>
        # Ensure rows is iterable
        try:
            row_count = rows.count()
        except:
            row_count = 0

        for i in range(row_count):
            r = rows.nth(i)
            row_text = r.inner_text()
            dates, times = extract_dates_times(row_text)
            # Normalize a session representation
            sessions.append({
                "dates": dates or ["n/a"],
                "times": times or ["n/a"],
            })
    else:
        # Fallback: scrape entire visible pane
        body = frame.locator("body")
        if body.count() == 0:
            body = page.locator("body")
        dates, times = extract_dates_times(body.inner_text())
        # Treat as a single session when only free text is available
        sessions.append({
            "dates": dates or ["n/a"],
            "times": times or ["n/a"],
        })

    # Go back to the listing page for the next item
    page.go_back(wait_until="domcontentloaded")
    page.wait_for_timeout(800)

    # Sort sessions for stable diffs
    sessions.sort(key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))
    return sessions

def get_items_with_sessions():
    """Return only the two target items with title, url, and sessions (dates+times)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        open_aquatics(page)

        # Find candidate anchors for our two titles
        items = []
        for title in TARGET_TITLES:
            # Find the anchor to capture a stable URL
            link = page.get_by_role("link", name=title, exact=True)
            if link.count() == 0:
                link = page.get_by_role("link", name=re.compile(re.escape(title), re.I))

            if link.count() == 0:
                # Not present at all in the list right now
                items.append({"title": title, "url": None, "sessions": []})
                continue

            href = link.first.get_attribute("href") or ""
            full_url = href if href.startswith("http") else f"https://secure.rec1.com{href}" if href.startswith("/") else href

            # Open and extract sessions
            sessions = list_sessions_for_item(page, title)

            items.append({"title": title, "url": full_url, "sessions": sessions})

        browser.close()

    # Sort by title for a stable order
    items.sort(key=lambda x: (x["title"].lower(), x["url"] or ""))
    return items

def load_baseline():
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text())
    return {"items": [], "last_updated": None}

def save_baseline(data):
    BASELINE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def diff_items(old_items, new_items):
    """
    Detect:
      - added (target title now present with URL where it was missing)
      - removed (target title previously had URL, now missing)
      - changed (sessions list differs)
    Compare by title (since we’re tracking two fixed classes), but also keep URL for context.
    """
    old_map = {i["title"]: i for i in old_items}
    new_map = {i["title"]: i for i in new_items}

    added, removed, changed = [], [], []

    titles = set(old_map.keys()) | set(new_map.keys()) | set(TARGET_TITLES)
    for t in sorted(titles):
        old = old_map.get(t, {"title": t, "url": None, "sessions": []})
        new = new_map.get(t, {"title": t, "url": None, "sessions": []})

        old_present = bool(old.get("url"))
        new_present = bool(new.get("url"))

        if not old_present and new_present:
            added.append(new)
        elif old_present and not new_present:
            removed.append(old)
        else:
            # Present both before and now; check sessions diff
            if old.get("sessions", []) != new.get("sessions", []):
                changed.append({
                    "title": t,
                    "url": new.get("url") or old.get("url"),
                    "old_sessions": old.get("sessions", []),
                    "new_sessions": new.get("sessions", []),
                })

    return added, removed, changed

def format_report(current_items, added, removed, changed):
    lines = [f"### Aquatics Monitor — {datetime.utcnow().isoformat()}Z",
             "Tracking sessions (dates & times) for:",
             f"- {TARGET_TITLES[0]}",
             f"- {TARGET_TITLES[1]}"]

    # Always show the current snapshot
    lines.append("\n**Current sessions (now):**")
    for it in current_items:
        title = it.get("title", "(unknown)")
        url = it.get("url") or "(not currently listed)"
        lines.append(f"- {title} — {url}")
        if it.get("sessions"):
            for s in it["sessions"]:
                lines.append(f"  • dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")
        else:
            lines.append("  • (no sessions found)")

    if added:
        lines.append("\n**Added (now present):**")
        for a in added:
            lines.append(f"- {a['title']} — {a.get('url','')}")
            for s in a.get("sessions", []):
                lines.append(f"  • dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")

    if removed:
        lines.append("\n**Removed (now missing):**")
        for r in removed:
            lines.append(f"- {r['title']} — {r.get('url','')}")
            for s in r.get("sessions", []):
                lines.append(f"  • last dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")

    if changed:
        lines.append("\n**Changed sessions:**")
        for c in changed:
            lines.append(f"- {c['title']} — {c.get('url','')}")
            lines.append("  old:")
            if c["old_sessions"]:
                for s in c["old_sessions"]:
                    lines.append(f"    • dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")
            else:
                lines.append("    • (none)")
            lines.append("  new:")
            if c["new_sessions"]:
                for s in c["new_sessions"]:
                    lines.append(f"    • dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")
            else:
                lines.append("    • (none)")

    return "\n".join(lines)

def main():
    items = get_items_with_sessions()

    baseline = load_baseline()
    added, removed, changed = diff_items(baseline["items"], items)

    report = format_report(items, added, removed, changed)
    print(report)

    save_baseline({"items": items, "last_updated": datetime.utcnow().isoformat()})

    # Exit 1 if anything changed (added/removed/changed sessions)
    if added or removed or changed:
        sys.exit(1)

if __name__ == "__main__":
    main()
