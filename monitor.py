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
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                break
            except:
                pass

    # Fallback: search
    for placeholder in ["Keyword or code", "Search"]:
        try:
            s = page.get_by_placeholder(placeholder)
            s.fill("Aquatics")
            s.press("Enter")
            page.wait_for_timeout(1500)
            break
        except:
            continue

def get_items_with_sessions():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        open_aquatics(page)
        # scroll to trigger lazy load of cards/panels
        for _ in range(8):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(150)

        items = []
        for title in TARGET_TITLES:
            link = _find_anchor_anywhere(page, title)
            url = None
            if link and link.count() > 0:
                href = (link.first.get_attribute("href") or "").strip()
                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = f"https://secure.rec1.com{href}"
                else:
                    url = "inline:" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")

            sessions = list_sessions_for_item(page, title)
            items.append({"title": title, "url": url, "sessions": sessions})

        browser.close()
    items.sort(key=lambda x: (x["title"].lower(), x["url"] or ""))
    return items

def _find_anchor_anywhere(page, title):
    """
    Find the <a> for a class title on the main page or inside civicrec iframes.
    Scrolls to trigger lazy load. Returns a Locator or None.
    """
    def find_in(scope):
        # exact first
        loc = scope.get_by_role("link", name=title, exact=True)
        if loc.count() > 0:
            return loc
        # relaxed (case-insensitive)
        loc = scope.get_by_role("link", name=re.compile(re.escape(title), re.I))
        if loc.count() > 0:
            return loc
        # partial on the left part (e.g., "Swim Lesson Level 2")
        key = title.split(":")[0].strip()
        loc = scope.get_by_role("link", name=re.compile(re.escape(key), re.I))
        if loc.count() > 0:
            return loc
        return None

    link = find_in(page)
    if not link or link.count() == 0:
        # lazily loaded; scroll
        for _ in range(8):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(200)
            link = find_in(page)
            if link and link.count() > 0:
                break

    if not link or link.count() == 0:
        # try civicrec frames
        for f in page.frames:
            try:
                if "secure.rec1.com" not in (f.url or ""):
                    continue
            except Exception:
                continue
            link = find_in(f)
            if link and link.count() > 0:
                break

    return link if link and link.count() > 0 else None

def _panel_locator_for_link(page, link):
    """
    Resolve the accordion panel element that the link controls.
    Looks for aria-controls, data-target, or href="#id".
    Returns a Locator or None.
    """
    # Work with the first match
    l = link.first
    panel_id = None
    for attr in ("aria-controls", "data-target", "data-bs-target", "href"):
        try:
            val = (l.get_attribute(attr) or "").strip()
        except Exception:
            val = ""
        if not val:
            continue
        if val.startswith("#"):            # href="#panel-123"
            panel_id = val[1:]
            break
        if attr in ("aria-controls", "data-target", "data-bs-target"):
            # may be "#id" or "id"
            panel_id = val[1:] if val.startswith("#") else val
            break

    if not panel_id:
        return None

    return page.locator(f"#{panel_id}")

def click_item_by_title(page, title):
    """
    Open an item by its link text. Tries page and iframes, with some scrolling.
    Raises if not found.
    """
    # Ensure list has rendered
    page.wait_for_timeout(800)

    # Try to find without scrolling first
    link = _find_anchor_anywhere(page, title)

    # If not found, scroll the main page a bit to trigger lazy loading
    if (link is None) or link.count() == 0:
        for _ in range(5):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(400)
            link = _find_anchor_anywhere(page, title)
            if link and link.count() > 0:
                break

    # As a last resort, filter via search box to surface the item
    if (link is None) or link.count() == 0:
        for placeholder in ["Keyword or code", "Search"]:
            try:
                s = page.get_by_placeholder(placeholder)
                s.fill(title.split(":")[0])  # search by leading words
                s.press("Enter")
                page.wait_for_timeout(1500)
                link = _find_anchor_anywhere(page, title)
                if link and link.count() > 0:
                    break
            except:
                pass

    if link is None or link.count() == 0:
        raise RuntimeError(f"Could not find link for: {title}")

    link.first.click(timeout=5000)
    page.wait_for_timeout(1200)

def get_catalog_frame(page):
    """
    Prefer a frame whose URL contains secure.rec1.com; else fall back to the page.
    """
    page.wait_for_timeout(600)
    for f in page.frames:
        try:
            if "secure.rec1.com" in (f.url or ""):
                return f
        except Exception:
            pass
    return page

def list_sessions_for_item(page, title):
    """
    Clicks the class title to expand its panel, then parses the sessions table
    inside that panel. If the table is inside an <iframe>, switch to its frame.
    Returns a sorted list of {"dates":[...], "times":[...]}.
    """
    sessions = []

    link = _find_anchor_anywhere(page, title)
    if not link:
        return sessions

    # Find the controlled panel
    panel = _panel_locator_for_link(page, link)
    # If we couldn't resolve, just click and fallback to nearest visible table
    if not panel or panel.count() == 0:
        link.first.click(timeout=5000)
        page.wait_for_timeout(500)
        # nearest visible table below link
        tables = page.locator("table:visible").all()
        if tables:
            rows = tables[0].locator("tbody tr")
            if rows.count() == 0:
                rows = tables[0].locator("tr").nth(1)
            for i in range(rows.count()):
                r = rows.nth(i)
                row_text = r.inner_text()
                d, t = extract_dates_times(row_text)
                sessions.append({"dates": d or ["n/a"], "times": t or ["n/a"]})
        return sorted(sessions, key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))

    # Ensure the panel is expanded (click if needed)
    try:
        aria_expanded = link.first.get_attribute("aria-expanded")
    except Exception:
        aria_expanded = None
    if aria_expanded in (None, "false"):
        link.first.click(timeout=5000)
        page.wait_for_timeout(500)

    # If the site renders an iframe inside the panel, enter it
    iframe_loc = panel.locator("iframe").first
    table_scope = panel
    try:
        if iframe_loc.count() > 0:
            # Get the frame from the iframe element
            handle = iframe_loc.element_handle()
            if handle:
                frame = handle.content_frame()
                if frame:
                    table_scope = frame
    except Exception:
        pass

    # Parse sessions table inside the panel/frame
    table = table_scope.locator("table:has(th:has-text('Dates')), table:has(th:has-text('Time'))").first
    if table.count() == 0:
        # fallback: any visible table in the panel/frame
        table = table_scope.locator("table:visible").first

    if table.count() > 0:
        rows = table.locator("tbody tr")
        if rows.count() == 0:
            rows = table.locator("tr").nth(1)
        for i in range(rows.count()):
            r = rows.nth(i)
            row_text = r.inner_text()
            d, t = extract_dates_times(row_text)
            sessions.append({"dates": d or ["n/a"], "times": t or ["n/a"]})
    else:
        # last resort: text of the panel
        text = table_scope.locator(":scope").inner_text()
        d, t = extract_dates_times(text)
        sessions.append({"dates": d or ["n/a"], "times": t or ["n/a"]})

    return sorted(sessions, key=lambda s: (";".join(s["dates"]), ";".join(s["times"])))

def get_items_with_sessions():
    """Return only the two target items with title, url (or tag), and sessions."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        open_aquatics(page)

        items = []
        for title in TARGET_TITLES:
            # Try to get a stable identifier
            link = _find_anchor_anywhere(page, title)
            url = None
            if link and link.count() > 0:
                href = (link.first.get_attribute("href") or "").strip()
                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = f"https://secure.rec1.com{href}"
                elif href.startswith("javascript"):
                    # Inline expansion; fabricate a stable tag so diffs include context
                    url = "inline:" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")

            sessions = list_sessions_for_item(page, title)
            items.append({"title": title, "url": url, "sessions": sessions})

        browser.close()

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
    lines = [
        "### Aquatics Monitor - " + datetime.utcnow().isoformat() + "Z",
        "Tracking sessions (dates & times) for:",
        "- " + TARGET_TITLES[0],
        "- " + TARGET_TITLES[1],
        "",
        "**Current sessions (now):**",
    ]

    for it in current_items:
        title = it.get("title", "(unknown)")
        url = it.get("url") or "(not currently listed)"
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
            if c["old_sessions"]:
                for s in c["old_sessions"]:
                    lines.append(f"    * dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")
            else:
                lines.append("    * (none)")
            lines.append("  new:")
            if c["new_sessions"]:
                for s in c["new_sessions"]:
                    lines.append(f"    * dates: {', '.join(s['dates'])} | times: {', '.join(s['times'])}")
            else:
                lines.append("    * (none)")

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
