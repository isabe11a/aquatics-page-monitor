# Deep diagnostic - find where the session data actually is
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

CATALOG_URL = "https://secure.rec1.com/CA/calabasas-ca/catalog/index"

TARGET_TITLES = [
    "Swim Lesson Level 1: Baby Pups & Parent Seals",
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
                page.wait_for_timeout(1500)
                break
            except Exception as e:
                log(f"warn: category click failed: {e}")
    
    for i in range(15):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(150)
    log("finished initial scroll")

def deep_diagnose(page, title):
    """Deep dive to find where session data lives."""
    
    log(f"\n{'='*80}")
    log(f"[DIAG] Deep diagnostic for: {title}")
    log(f"{'='*80}")
    
    # Find the group name element
    patt = re.compile(re.escape(title), re.I)
    heading = page.get_by_text(patt).first
    
    if heading.count() == 0:
        log(f"[DIAG] Heading not found!")
        return
    
    log(f"[DIAG] Found heading")
    
    # Find the parent row/container that contains this group
    # CivicRec often uses specific classes like 'rec1-catalog-group-row'
    parent_row = heading.locator("xpath=ancestor::*[contains(@class,'rec1-catalog-group') or contains(@class,'catalog-group') or contains(@class,'program-group')][1]")
    
    if parent_row.count() > 0:
        log(f"[DIAG] Found parent group row")
        parent_classes = parent_row.evaluate("el => el.className")
        log(f"[DIAG] Parent row classes: {parent_classes}")
        
        # Look for following siblings (session rows after the group header)
        following_siblings = parent_row.locator("xpath=following-sibling::*[position()<=10]")
        log(f"[DIAG] Following siblings: {following_siblings.count()}")
        
        for i in range(min(5, following_siblings.count())):
            sib = following_siblings.nth(i)
            sib_classes = sib.evaluate("el => el.className")
            sib_text = sib.inner_text()[:150]
            log(f"[DIAG] Sibling {i} classes: {sib_classes}")
            log(f"[DIAG] Sibling {i} text: {sib_text}")
            
            # Check if this sibling has date/time data
            dates, times = extract_dates_times(sib_text)
            if dates or times:
                log(f"[DIAG] ✓✓✓ Sibling {i} HAS DATES/TIMES! dates={dates}, times={times}")
    else:
        log(f"[DIAG] No parent group row found")
    
    # Alternative: Look for ALL elements containing the title and check nearby content
    all_matches = page.get_by_text(patt).all()
    log(f"[DIAG] Found {len(all_matches)} elements containing title")
    
    for i, elem in enumerate(all_matches[:3]):
        log(f"\n[DIAG] --- Match {i} ---")
        try:
            # Get a larger ancestor that might contain session rows
            ancestor = elem.locator("xpath=ancestor::*[self::div or self::section or self::table][position()<=3]").first
            if ancestor.count() > 0:
                anc_text = ancestor.inner_text()
                log(f"[DIAG] Ancestor text length: {len(anc_text)}")
                log(f"[DIAG] Ancestor text preview: {anc_text[:400]}")
                
                dates, times = extract_dates_times(anc_text)
                if dates or times:
                    log(f"[DIAG] ✓✓✓ Ancestor HAS SESSION DATA! dates={dates}, times={times}")
                    
                    # Show the structure
                    tables = ancestor.locator("table").count()
                    iframes = ancestor.locator("iframe").count()
                    log(f"[DIAG] Ancestor contains: {tables} tables, {iframes} iframes")
        except Exception as e:
            log(f"[DIAG] Error with match {i}: {e}")
    
    # Check if clicking actually toggles visibility of session rows
    log(f"\n[DIAG] Testing click behavior...")
    heading.click(timeout=3000)
    page.wait_for_timeout(2000)
    
    # Check visible modals AFTER click
    modals = page.locator('[role="dialog"], .modal, [class*="modal"], [class*="popup"], [class*="overlay"]')
    log(f"[DIAG] Checking {modals.count()} potential modals...")
    
    found_visible_modal = False
    for i in range(modals.count()):
        modal = modals.nth(i)
        try:
            if modal.is_visible():
                found_visible_modal = True
                log(f"[DIAG] ✓ Modal {i} IS VISIBLE after click!")
                txt = modal.inner_text()
                log(f"[DIAG] Modal text length: {len(txt)}")
                log(f"[DIAG] Modal text: {txt[:500]}")
                
                dates, times = extract_dates_times(txt)
                if dates or times:
                    log(f"[DIAG] ✓✓✓ Modal HAS SESSION DATA! dates={dates}, times={times}")
                
                # Check modal contents
                tables = modal.locator("table").count()
                iframes = modal.locator("iframe").count()
                log(f"[DIAG] Modal contains: {tables} tables, {iframes} iframes")
                break
        except Exception as e:
            continue
    
    if not found_visible_modal:
        log(f"[DIAG] No visible modals found after click")
    
    # Check ALL iframes on the page
    log(f"\n[DIAG] Inspecting ALL iframes on page...")
    all_iframes = page.locator("iframe")
    log(f"[DIAG] Total iframes: {all_iframes.count()}")
    
    for i in range(all_iframes.count()):
        iframe = all_iframes.nth(i)
        try:
            src = iframe.get_attribute("src") or "no-src"
            is_vis = iframe.is_visible()
            log(f"[DIAG] Iframe {i}: src={src[:80]}, visible={is_vis}")
            
            if is_vis:
                handle = iframe.element_handle()
                fr = handle.content_frame() if handle else None
                if fr:
                    # Get iframe content
                    try:
                        iframe_text = fr.locator("body").inner_text()
                        log(f"[DIAG] Iframe {i} text length: {len(iframe_text)}")
                        
                        # Check if it mentions our program
                        if title.lower() in iframe_text.lower():
                            log(f"[DIAG] ✓ Iframe {i} MENTIONS our program!")
                            log(f"[DIAG] Iframe text: {iframe_text[:500]}")
                            
                            dates, times = extract_dates_times(iframe_text)
                            if dates or times:
                                log(f"[DIAG] ✓✓✓ Iframe {i} HAS SESSION DATA! dates={dates}, times={times}")
                        
                        # Check for tables in iframe
                        tables = fr.locator("table")
                        if tables.count() > 0:
                            log(f"[DIAG] Iframe {i} has {tables.count()} tables")
                            for t in range(min(2, tables.count())):
                                tbl = tables.nth(t)
                                tbl_text = tbl.inner_text()
                                log(f"[DIAG] Iframe table {t}: {tbl_text[:200]}")
                                dates, times = extract_dates_times(tbl_text)
                                if dates or times:
                                    log(f"[DIAG] ✓✓✓ Iframe table {t} HAS SESSION DATA! dates={dates}, times={times}")
                    except Exception as e:
                        log(f"[DIAG] Error reading iframe {i} content: {e}")
        except Exception as e:
            log(f"[DIAG] Error with iframe {i}: {e}")
    
    # Look for date/time patterns ANYWHERE on the page after the click
    log(f"\n[DIAG] Scanning entire page for date/time patterns...")
    try:
        page_text = page.locator("body").inner_text()
        log(f"[DIAG] Total page text length: {len(page_text)}")
        
        # Find where our program title appears in the text
        title_pos = page_text.lower().find(title.lower())
        if title_pos >= 0:
            log(f"[DIAG] Title found at position {title_pos}")
            # Get text around the title (500 chars after)
            context = page_text[title_pos:title_pos+600]
            log(f"[DIAG] Context around title: {context}")
            
            dates, times = extract_dates_times(context)
            if dates or times:
                log(f"[DIAG] ✓✓✓ Found dates/times near title! dates={dates}, times={times}")
    except Exception as e:
        log(f"[DIAG] Error scanning page: {e}")
    
    log(f"{'='*80}\n")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        open_aquatics(page)

        deep_diagnose(page, TARGET_TITLES[0])

        browser.close()
    
    log("\n[DIAG] Deep diagnostic complete!")

if __name__ == "__main__":
    main()