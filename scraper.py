import json
import re
import time
import sys
import os

from playwright.sync_api import sync_playwright

import gspread
from oauth2client.service_account import ServiceAccountCredentials

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SEARCH_URL = "https://www.facebook.com/marketplace/cairo/search/?category_id=1270772586445798&query=Home%20Sales&referral_ui_component=category_menu_item"
CUSTOM_MESSAGE = "ممكن رقم التواصل لأنه مخفي في البوست؟"
PHONE_REGEX = r"01[0-9]{9}"

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    # Spoof real Chrome to avoid headless detection
    "--window-size=1920,1080",
    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

def get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = json.loads(os.environ["GOOGLE_CREDS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open("marketplace_leads").sheet1

def append_to_sheet(sheet, url, phone, message_sent, status):
    try:
        sheet.append_row([url, phone if phone else "", str(message_sent), status])
        print(f"[SHEETS] Added row: {status}")
    except Exception as e:
        print("[SHEETS ERROR]", e)

def ok(msg):   print(f"    [OK]   {msg}", flush=True)
def warn(msg): print(f"    [WARN] {msg}", flush=True)
def err(msg):  print(f"    [ERR]  {msg}", flush=True)

def check_logged_in(page):
    """Verify Facebook actually loaded and we're logged in."""
    try:
        # If we see the login form, we're not logged in
        if page.query_selector('input[name="email"]'):
            return False
        # If we see the marketplace search results or profile, we're in
        if page.query_selector('[aria-label="Facebook"]'):
            return True
        # Check URL
        if "login" in page.url or "checkpoint" in page.url:
            return False
        return True
    except Exception:
        return False

def open_message_dialog(page):
    # Wait a bit for dynamic content
    page.wait_for_timeout(3000)

    # Try multiple selectors for the message button
    selectors = [
        '[aria-label="Message"]',
        '[aria-label="Send message"]',
        '[aria-label="Message seller"]',
        '[aria-label="Contact seller"]',
        '[aria-label="مراسلة"]',
        '[aria-label="إرسال رسالة"]',
    ]

    for sel in selectors:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                bb = el.bounding_box()
                if bb and bb["width"] > 10 and bb["height"] > 5:
                    el.scroll_into_view_if_needed()
                    el.click(force=True)
                    ok("Opened message dialog")
                    return True
        except Exception:
            continue

    # Fallback: find any button with message-related text
    try:
        btns = page.query_selector_all('div[role="button"], button')
        for btn in btns:
            txt = (btn.text_content() or "").strip().lower()
            if txt in ["message", "send message", "مراسلة", "إرسال رسالة"]:
                bb = btn.bounding_box()
                if bb and bb["width"] > 10:
                    btn.click(force=True)
                    ok("Opened message dialog (text fallback)")
                    return True
    except Exception:
        pass

    return False

def find_dialog_textarea(page):
    page.wait_for_timeout(2000)
    try:
        textboxes = page.get_by_role("textbox")
        if textboxes.count() > 0:
            el = textboxes.last
            if el.is_visible():
                return el
    except Exception:
        pass
    return None

def clear_and_type(page, el, text):
    try:
        el.click(force=True)
        time.sleep(0.5)
        page.keyboard.press("Control+a")
        page.keyboard.type(text, delay=30)
        return True
    except Exception:
        return False

def main():
    hrefs = []
    sheet = get_sheet()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=BROWSER_ARGS,
        )

        # Load session + override user agent at context level too
        context = browser.new_context(
            storage_state=json.loads(os.environ["FB_SESSION"]),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            # Spoof webdriver flag
            java_script_enabled=True,
        )

        # Hide automation fingerprint
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['ar-EG', 'ar', 'en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        # ── COLLECT LISTINGS ────────────────────────────────
        print("\n=== COLLECTING LISTINGS ===")
        page = context.new_page()
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(8000)

        # Check login
        if not check_logged_in(page):
            err("Not logged in! FB_SESSION may be expired.")
            print("Page URL:", page.url)
            print("Page title:", page.title())
            browser.close()
            return

        ok("Logged in confirmed")

        posts = page.locator('a[href*="/marketplace/item/"]')
        count = posts.count()
        ok(f"Found {count} listing links")

        for i in range(min(count, 30)):
            try:
                href = posts.nth(i).get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = "https://www.facebook.com" + href
                    href = href.split("&__tn__")[0]
                    href = href.split("?")[0]  # clean URL, no tracking params
                    if href not in hrefs:
                        hrefs.append(href)
            except Exception:
                pass

        page.close()
        ok(f"Collected {len(hrefs)} unique listings")

        # ── PROCESS LISTINGS ────────────────────────────────
        print("\n=== PROCESSING LISTINGS ===")

        for index, href in enumerate(hrefs):
            print(f"\n{'='*60}")
            print(f"[LISTING {index+1}/{len(hrefs)}] {href}")

            page = context.new_page()

            try:
                page.goto(href, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)

                # Extra wait for dynamic content
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                body_text = page.locator("body").inner_text()
                match = re.search(PHONE_REGEX, body_text)

                if match:
                    phone = match.group(0)
                    ok(f"Phone found: {phone}")
                    append_to_sheet(sheet, href, phone, False, "PHONE_FOUND")
                    page.close()
                    continue

                warn("No phone in page text — trying message dialog")

                opened = open_message_dialog(page)

                if not opened:
                    warn("No message dialog found — saving screenshot info")
                    # Log what's on the page for debugging
                    title = page.title()
                    warn(f"Page title: {title}")
                    append_to_sheet(sheet, href, "", False, "FAILED_NO_DIALOG")
                    page.close()
                    continue

                time.sleep(2)
                input_el = find_dialog_textarea(page)

                if not input_el:
                    append_to_sheet(sheet, href, "", False, "FAILED_NO_INPUT")
                    page.close()
                    continue

                clear_and_type(page, input_el, CUSTOM_MESSAGE)
                time.sleep(1)
                page.keyboard.press("Enter")
                ok("Message sent")
                append_to_sheet(sheet, href, "", True, "MESSAGE_SENT")
                page.wait_for_timeout(2000)
                page.close()

            except Exception as e:
                err(str(e)[:200])
                append_to_sheet(sheet, href, "", False, "FAILED_CRASH")
                try:
                    page.close()
                except Exception:
                    pass

        print("\n[DONE] Finished")
        context.close()
        browser.close()

if __name__ == "__main__":
    main()
