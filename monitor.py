import json, sys
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

CATALOG_URL = "https://secure.rec1.com/CA/calabasas-ca/catalog/index"
BASELINE_FILE = Path("baseline.json")
AQUATICS_KEYWORDS = ["Aquatics", "Swim", "Swimming", "Aquatic"]

def get_items(page):
    page.goto(CATALOG_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    # Click Aquatics if visible
    clicked = False
    for label in ["Aquatics", "Aquatics Programs"]:
        loc = page.locator(f"text={label}")
        if loc.count() > 0:
            try:
                loc.first.click(timeout=1500)
                clicked = True
                break
            except:
                pass

    if not clicked:
        try:
            search = page.get_by_placeholder("Search")
            search.fill("Aquatics")
            search.press("Enter")
            page.wait_for_timeout(1500)
        except:
            pass

    anchors = page.locator("a").all()
    items = []
    for a in anchors:
        href = (a.get_attribute("href") or "").strip()
        text = (a.inner_text() or "").strip()
        if "/catalog/item/" in href and (clicked or any(k.lower() in text.lower() for k in AQUATICS_KEYWORDS)):
            full_url = href if href.startswith("http") else f"https://secure.rec1.com{href}"
            items.append({"title": text, "url": full_url})

    # Deduplicate & sort
    seen = set()
    clean = []
    for i in items:
        if i["url"] not in seen:
            seen.add(i["url"])
            clean.append(i)
    clean.sort(key=lambda x: (x["title"].lower(), x["url"]))
    return clean

def load_baseline():
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text())
    return {"items": [], "last_updated": None}

def save_baseline(data):
    BASELINE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def diff_items(old, new):
    old_urls = {i["url"] for i in old}
    new_urls = {i["url"] for i in new}
    added = [i for i in new if i["url"] not in old_urls]
    removed = [i for i in old if i["url"] not in new_urls]
    return added, removed

def format_report(added, removed):
    lines = [f"### Aquatics Monitor — {datetime.utcnow().isoformat()}Z"]
    if added:
        lines.append("\n**Added:**")
        for a in added:
            lines.append(f"- {a['title']} — {a['url']}")
    if removed:
        lines.append("\n**Removed:**")
        for r in removed:
            lines.append(f"- {r['title']} — {r['url']}")
    if not added and not removed:
        lines.append("\nNo changes detected.")
    return "\n".join(lines)

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        items = get_items(page)
        browser.close()

    baseline = load_baseline()
    added, removed = diff_items(baseline["items"], items)
    report = format_report(added, removed)
    print(report)
    save_baseline({"items": items, "last_updated": datetime.utcnow().isoformat()})

    # Exit code 1 means "something changed" → GitHub will email you
    if added or removed:
        sys.exit(1)

if __name__ == "__main__":
    main()
