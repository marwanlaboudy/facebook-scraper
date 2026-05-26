import json
import re
import time
import sys
import os

from playwright.sync_api import sync_playwright

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ── WINDOWS UTF-8 FIX ───────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ──────────────────────────────────────────────
SEARCH_URL = "https://www.facebook.com/marketplace/cairo/search/?category_id=1270772586445798&query=Home%20Sales&referral_ui_component=category_menu_item"

CUSTOM_MESSAGE = "ممكن رقم التواصل لأنه مخفي في البوست؟"

PHONE_REGEX = r"01[0-9]{9}"

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--no-sandbox",
    "--disable-setuid-sandbox",
]

# ────────────────────────────────────────────────────────
# GOOGLE SHEETS
# ────────────────────────────────────────────────────────
def get_sheet():

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds_dict = json.loads(
        os.environ["GOOGLE_CREDS"]
    )

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict,
        scope
    )

    client = gspread.authorize(creds)

    return client.open(
        "marketplace_leads"
    ).sheet1

# ────────────────────────────────────────────────────────
# APPEND ROW
# ────────────────────────────────────────────────────────
def append_to_sheet(sheet, url, phone, message_sent, status):

    try:

        sheet.append_row([
            url,
            phone if phone else "",
            str(message_sent),
            status
        ])

        print(f"[SHEETS] Added row")

    except Exception as e:

        print("[SHEETS ERROR]", e)

# ────────────────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────────────────
_step = 0

def log(msg):
    global _step
    _step += 1
    print(f"[{_step}] {msg}", flush=True)

def ok(msg):
    print(f"    [OK]   {msg}", flush=True)

def warn(msg):
    print(f"    [WARN] {msg}", flush=True)

def err(msg):
    print(f"    [ERR]  {msg}", flush=True)

# ────────────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────────────
def bbox_ok(el):

    try:

        bb = el.bounding_box()

        return bb and bb["width"] > 10 and bb["height"] > 5

    except Exception:

        return False

# ────────────────────────────────────────────────────────
# OPEN MESSAGE DIALOG
# ────────────────────────────────────────────────────────
def open_message_dialog(page):

    labels = [
        "Message",
        "Send message",
        "Message seller",
        "Contact seller",
        "مراسلة",
        "إرسال رسالة"
    ]

    for label in labels:

        try:

            els = page.query_selector_all(
                f'[aria-label="{label}"]'
            )

            for el in els:

                if bbox_ok(el):

                    el.click(force=True)

                    ok(f"Opened dialog")

                    return True

        except Exception:
            continue

    return False

# ────────────────────────────────────────────────────────
# FIND TEXTBOX
# ────────────────────────────────────────────────────────
def find_dialog_textarea(page):

    try:

        textboxes = page.get_by_role("textbox")

        if textboxes.count() > 0:

            el = textboxes.last

            if el.is_visible():

                return el

    except Exception:
        pass

    return None

# ────────────────────────────────────────────────────────
# TYPE MESSAGE
# ────────────────────────────────────────────────────────
def clear_and_type(page, el, text):

    try:

        el.click(force=True)

        time.sleep(0.5)

        page.keyboard.press("Control+a")

        page.keyboard.type(text, delay=30)

        return True

    except Exception:

        return False

# ────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────
def main():

    global _step

    hrefs = []

    # CONNECT TO GOOGLE SHEETS
    sheet = get_sheet()

    with sync_playwright() as pw:

        browser = pw.chromium.launch(
            headless=True,
            args=BROWSER_ARGS,
        )

        context = browser.new_context(
            storage_state=json.loads(
                os.environ["FB_SESSION"]
            )
        )

        # ══════════════════════════════════════════════
        # COLLECT LISTINGS
        # ══════════════════════════════════════════════
        print("\n=== COLLECTING LISTINGS ===")

        page = context.new_page()

        page.goto(
            SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=120000
        )

        page.wait_for_timeout(8000)

        posts = page.locator(
            'a[href*="/marketplace/item/"]'
        )

        count = posts.count()

        ok(f"Found {count} listing links")

        for i in range(min(count, 30)):

            try:

                href = posts.nth(i).get_attribute("href")

                if href:

                    if href.startswith("/"):
                        href = "https://www.facebook.com" + href

                    href = href.split("&__tn__")[0]

                    if href not in hrefs:
                        hrefs.append(href)

            except Exception:
                pass

        page.close()

        # ══════════════════════════════════════════════
        # PROCESS LISTINGS
        # ══════════════════════════════════════════════
        print("\n=== PROCESSING LISTINGS ===")

        for index, href in enumerate(hrefs):

            print("\n" + "=" * 60)

            print(f"[LISTING {index+1}/{len(hrefs)}]")

            page = context.new_page()

            try:

                page.goto(
                    href,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

                page.wait_for_timeout(5000)

                body_text = page.locator(
                    "body"
                ).inner_text()

                match = re.search(
                    PHONE_REGEX,
                    body_text
                )

                # ───────────────────────────────
                # PHONE FOUND
                # ───────────────────────────────
                if match:

                    phone = match.group(0)

                    ok(f"Phone found: {phone}")

                    append_to_sheet(
                        sheet,
                        href,
                        phone,
                        False,
                        "PHONE_FOUND"
                    )

                    page.close()

                    continue

                # ───────────────────────────────
                # SEND MESSAGE
                # ───────────────────────────────
                warn("No phone found")

                opened = open_message_dialog(page)

                if not opened:

                    append_to_sheet(
                        sheet,
                        href,
                        "",
                        False,
                        "FAILED_NO_DIALOG"
                    )

                    page.close()

                    continue

                time.sleep(2)

                input_el = find_dialog_textarea(page)

                if not input_el:

                    append_to_sheet(
                        sheet,
                        href,
                        "",
                        False,
                        "FAILED_NO_INPUT"
                    )

                    page.close()

                    continue

                clear_and_type(
                    page,
                    input_el,
                    CUSTOM_MESSAGE
                )

                time.sleep(1)

                page.keyboard.press("Enter")

                ok("Message sent")

                append_to_sheet(
                    sheet,
                    href,
                    "",
                    True,
                    "MESSAGE_SENT"
                )

                page.wait_for_timeout(2000)

                page.close()

            except Exception as e:

                err(str(e))

                append_to_sheet(
                    sheet,
                    href,
                    "",
                    False,
                    "FAILED_CRASH"
                )

                try:
                    page.close()
                except Exception:
                    pass

        print("\n[DONE] Finished")

        context.close()

# ────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
