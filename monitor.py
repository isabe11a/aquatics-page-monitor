# Diagnostic version to understand what's happening
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
    patt = re.compile(re.escape(title), re.I)
    for scope in _frames(page):
        link = scope.get_by_role("link", name=patt)
        if link.count() > 0:
            return link.first
        el = scope.get_by_text(patt).first
        if el.count() > 0:
            return el
    return None

def diagnose_item(page, title):
    """Comprehensive diagnostic for what happens when we interact with an item."""
    
    heading = _find_heading_anywhere(page, title)
    if not heading:
        log(f"[DIAG] Heading not found for: {title}")
        return
    
    log(f"\n{'='*60}")
    log(f"[DIAG] Diagnosing: {title}")
    log(f"{'='*60}")
    
    # Check URL before clicking
    url_before = page.url
    log(f"[DIAG] URL before click: {url_before}")
    
    # Get the element's attributes
    try:
        tag = heading.evaluate("el => el.tagName")
        href = heading.evaluate("el => el.href || el.getAttribute('href') || 'none'")
        classes = heading.evaluate("el => el.className")
        log(f"[DIAG] Element tag: {tag}")
        log(f"[DIAG] Element href: {href}")
        log(f"[DIAG] Element classes: {classes}")
    except Exception as e:
        log(f"[DIAG] Error getting element attributes: {e}")
    
    # Get container info before clicking
    try:
        container = heading.locator("xpath=ancestor::*[self::div or self::section or self::article or self::li][1]")
        if container.count() > 0:
            cont_classes = container.evaluate("el => el.className")
            cont_text_before = container.inner_text()
            log(f"[DIAG] Container classes: {cont_classes}")
            log(f"[DIAG] Container text length BEFORE click: {len(cont_text_before)}")
            log(f"[DIAG] Container text BEFORE: {cont_text_before[:200]}")
    except Exception as e:
        log(f"[DIAG] Error getting container info: {e}")
    
    # Click and observe what happens
    log(f"[DIAG] Clicking heading...")
    try:
        heading.click(timeout=3000)
        page.wait_for_timeout(2500)  # Give it more time
        log(f"[DIAG] Click succeeded")
    except Exception as e:
        log(f"[DIAG] Click failed: {e}")
        return
    
    # Check if URL changed (navigation)
    url_after = page.url
    log(f"[DIAG] URL after click: {url_after}")
    if url_before != url_after:
        log(f"[DIAG] ⚠️ URL CHANGED! Navigation occurred to: {url_after}")
        # We navigated to a new page - parse it here
        page.wait_for_timeout(1500)
        
        # Look for tables on the new page
        tables = page.locator("table")
        log(f"[DIAG] Tables on detail page: {tables.count()}")
        
        for i in range(min(3, tables.count())):
            tbl = tables.nth(i)
            try:
                if tbl.is_visible():
                    txt = tbl.inner_text()[:300]
                    log(f"[DIAG] Table {i} text preview: {txt}")
            except:
                pass
        
        # Try to go back
        try:
            page.go_back(wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            log(f"[DIAG] Navigated back to listing")
        except:
            pass
        return
    
    # URL didn't change - check what appeared
    log(f"[DIAG] URL unchanged - checking for expanded content...")
    
    # Check container again
    try:
        container = heading.locator("xpath=ancestor::*[self::div or self::section or self::article or self::li][1]")
        if container.count() > 0:
            cont_text_after = container.inner_text()
            log(f"[DIAG] Container text length AFTER click: {len(cont_text_after)}")
            if len(cont_text_after) > len(cont_text_before):
                log(f"[DIAG] ✓ Container expanded! New text: {cont_text_after[:500]}")
            else:
                log(f"[DIAG] ⚠️ Container did NOT expand (same length)")
            
            # Check for various content types in container
            tables_in = container.locator("table").count()
            grids_in = container.locator('[role="grid"], [role="table"]').count()
            iframes_in = container.locator("iframe").count()
            divs_in = container.locator("div").count()
            
            log(f"[DIAG] Container contents: {tables_in} tables, {grids_in} grids, {iframes_in} iframes, {divs_in} divs")
    except Exception as e:
        log(f"[DIAG] Error checking container after: {e}")
    
    # Check for modals/popups anywhere on the page
    try:
        modals = page.locator('[role="dialog"], .modal, [class*="modal"], [class*="popup"]')
        log(f"[DIAG] Modals/dialogs on page: {modals.count()}")
        
        for i in range(modals.count()):
            modal = modals.nth(i)
            if modal.is_visible():
                log(f"[DIAG] ✓ Modal {i} is visible!")
                txt = modal.inner_text()[:300]
                log(f"[DIAG] Modal text: {txt}")
    except Exception as e:
        log(f"[DIAG] Error checking modals: {e}")
    
    # Check all tables on the page
    try:
        all_tables = page.locator("table")
        log(f"[DIAG] Total tables on page: {all_tables.count()}")
        
        for i in range(all_tables.count()):
            tbl = all_tables.nth(i)
            if tbl.is_visible():
                txt = tbl.inner_text()
                log(f"[DIAG] Table {i} visible, length={len(txt)}, preview: {txt[:200]}")
                
                # Check if this table has dates/times
                dates, times = extract_dates_times(txt)
                if dates or times:
                    log(f"[DIAG] ✓✓✓ Table {i} HAS SESSION DATA! dates={dates}, times={times}")
    except Exception as e:
        log(f"[DIAG] Error checking tables: {e}")
    
    # Check all iframes
    try:
        all_iframes = page.locator("iframe")
        log(f"[DIAG] Total iframes on page: {all_iframes.count()}")
        
        for i in range(all_iframes.count()):
            iframe = all_iframes.nth(i)
            try:
                if iframe.is_visible():
                    src = iframe.get_attribute("src")
                    log(f"[DIAG] Iframe {i} visible, src={src}")
                    
                    handle = iframe.element_handle()
                    fr = handle.content_frame() if handle else None
                    if fr:
                        tables_in_iframe = fr.locator("table").count()
                        log(f"[DIAG] Iframe {i} contains {tables_in_iframe} tables")
                        
                        if tables_in_iframe > 0:
                            tbl = fr.locator("table").first
                            txt = tbl.inner_text()
                            log(f"[DIAG] Iframe table text: {txt[:200]}")
                            dates, times = extract_dates_times(txt)
                            if dates or times:
                                log(f"[DIAG] ✓✓✓ Iframe {i} table HAS SESSION DATA! dates={dates}, times={times}")
            except Exception as e:
                log(f"[DIAG] Error with iframe {i}: {e}")
    except Exception as e:
        log(f"[DIAG] Error checking iframes: {e}")
    
    log(f"{'='*60}\n")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        open_aquatics(page)

        # Diagnose just the first item
        diagnose_item(page, TARGET_TITLES[0])
        
        # Add extra wait and scroll
        page.wait_for_timeout(1000)
        page.mouse.wheel(0, -3000)
        page.wait_for_timeout(500)
        
        # Diagnose second item
        diagnose_item(page, TARGET_TITLES[1])

        browser.close()
    
    log("\n[DIAG] Diagnostic complete!")

if __name__ == "__main__":
    main()