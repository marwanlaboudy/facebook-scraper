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

# ── CONFIG ──────────────────────────────────────────────
SEARCH_URL     = "https://www.facebook.com/marketplace/cairo/search?minPrice=2500000&daysSinceListed=1&query=Home%20Sales&category_id=1270772586445798&exact=false&referral_ui_component=category_menu_item"
HREFS_FILE     = "marketplace_hrefs.json"
RESULTS_FILE   = "marketplace_results.json"
SESSION_FILE   = "facebook_session.json"
CUSTOM_MESSAGE = "ممكن رقم التواصل لأنه مخفي في البوست؟"
PHONE_REGEX    = r"\b01[0-9]{9}\b"
PHONE_BLACKLIST = {
    "01281283097",
}

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--no-proxy-server",
    "--ignore-certificate-errors",
]

# ── LOGGING ──────────────────────────────────────────────
_step = 0
def log(msg):
    global _step
    _step += 1
    print(f"[{_step}] {msg}", flush=True)
def ok(msg):   print(f"    [OK]   {msg}", flush=True)
def info(msg): print(f"    [...]  {msg}", flush=True)
def warn(msg): print(f"    [WARN] {msg}", flush=True)
def err(msg):  print(f"    [ERR]  {msg}", flush=True)


# ── SESSION MANAGEMENT ────────────────────────────────────
def is_session_valid(session_data: dict) -> bool:
    """
    Check if the saved session is still likely valid.
    Looks for key Facebook auth cookies (c_user, xs) and checks expiry.
    """
    if not session_data or "cookies" not in session_data:
        return False

    cookies = {c["name"]: c for c in session_data["cookies"]}

    # These two cookies must exist for a Facebook session to be active
    if "c_user" not in cookies or "xs" not in cookies:
        warn("Session missing critical cookies (c_user / xs)")
        return False

    # Check expiry of c_user cookie (Facebook sets this ~90 days out)
    c_user = cookies["c_user"]
    expiry = c_user.get("expires", -1)
    if expiry != -1 and expiry < time.time():
        warn("Session cookie has expired")
        return False

    ok(f"Session looks valid — logged in as user ID: {c_user.get('value', '?')}")
    return True


def login_and_save_session(pw) -> dict:
    """
    Opens a visible (headed) browser so you can log in manually.
    Saves cookies to SESSION_FILE once you're on the home feed.
    Returns the session dict.
    """
    print("\n" + "=" * 60)
    print("SESSION LOGIN — Manual login required")
    print("=" * 60)
    print("  A browser window will open. Log in to Facebook.")
    print("  Once you see your home feed, come back here and press ENTER.")
    print("=" * 60)

    browser = pw.chromium.launch(
        headless=False,   # Must be headed so you can type credentials
        args=BROWSER_ARGS,
        slow_mo=60,
    )
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=60000)

    input("\n  >>> Press ENTER after you have fully logged in to Facebook <<<\n")

    # Save cookies + localStorage snapshot
    cookies = context.cookies()
    session_data = {"cookies": cookies}

    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=4)

    ok(f"Session saved to '{SESSION_FILE}' ({len(cookies)} cookies)")

    context.close()
    browser.close()

    return session_data


def load_or_refresh_session(pw) -> dict:
    """
    Loads session from disk. If it doesn't exist or is expired,
    triggers a fresh manual login and saves the new session.
    """
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                session_data = json.load(f)
            if is_session_valid(session_data):
                ok(f"Reusing existing session from '{SESSION_FILE}'")
                return session_data
            else:
                warn("Existing session is invalid or expired — re-logging in")
        except (json.JSONDecodeError, KeyError) as e:
            warn(f"Could not read session file: {e} — re-logging in")
    else:
        info(f"No session file found at '{SESSION_FILE}' — first-time login")

    return login_and_save_session(pw)


def refresh_session_if_redirected(page, context) -> bool:
    """
    Call this after navigating to a page. Returns True if we got
    redirected to login (meaning the session expired mid-run).
    When that happens, the caller should abort and re-run the script
    so load_or_refresh_session() triggers a fresh login.
    """
    current_url = page.url
    if "login" in current_url or "checkpoint" in current_url:
        err("Facebook redirected to login — session expired mid-run!")
        err(f"Delete '{SESSION_FILE}' and re-run the script to log in again.")
        return True
    return False


# ── GOOGLE SHEETS ─────────────────────────────────────────
def send_to_sheets(results):
    print("\n" + "=" * 60)
    print("SENDING TO GOOGLE SHEETS")
    print("=" * 60)

    print("\n[STEP 1] Loading credentials...")
    try:
        google_creds_raw = os.environ.get("GOOGLE_CREDS", "")
        if google_creds_raw:
            creds_dict = json.loads(google_creds_raw)
            print("  Source : GOOGLE_CREDS environment variable")
        else:
            creds_path = "google_creds.json"
            if not os.path.exists(creds_path):
                print(f"  [ERR] '{creds_path}' not found and GOOGLE_CREDS env var is not set.")
                return
            with open(creds_path) as f:
                creds_dict = json.load(f)
            print(f"  Source : {creds_path}")
        print(f"  Email  : {creds_dict.get('client_email', '(not found)')}")
        print("  [OK] Credentials loaded")
    except json.JSONDecodeError as e:
        print(f"  [ERR] Credentials JSON is malformed: {e}")
        return
    except Exception as e:
        print(f"  [ERR] Unexpected error loading credentials: {e}")
        return

    print("\n[STEP 2] Authorizing with Google API...")
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    try:
        creds  = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        print("  [OK] Authorization successful")
    except Exception as e:
        print(f"  [ERR] Authorization failed: {e}")
        return

    print("\n[STEP 3] Opening spreadsheet 'marketplace_leads'...")
    try:
        spreadsheet = client.open("marketplace_leads")
        sheet = spreadsheet.sheet1
        print(f"  [OK] Opened sheet: '{sheet.title}'")
    except gspread.exceptions.SpreadsheetNotFound:
        print("  [ERR] Spreadsheet 'marketplace_leads' not found.")
        print(f"  Fix   : Share it with: {creds_dict.get('client_email')}")
        return
    except Exception as e:
        print(f"  [ERR] Error opening sheet: {e}")
        return

    print("\n[STEP 4] Checking header row...")
    try:
        existing = sheet.get_all_values()
        if not existing:
            sheet.append_row(["URL", "Seller Name", "Phone", "Message Sent"])
            print("  [OK] Header row written")
        else:
            print(f"  [OK] Sheet has {len(existing)} row(s) — skipping header")
    except Exception as e:
        print(f"  [ERR] Error checking header: {e}")
        return

    print(f"\n[STEP 5] Appending {len(results)} rows...")
    success_count = 0
    fail_count = 0

    for i, r in enumerate(results):
        row = [
            r.get("url", ""),
            r.get("seller_name") or "",
            r.get("phone") or "",
            str(r.get("message_sent", ""))
        ]
        try:
            sheet.append_row(row)
            success_count += 1
            print(f"  [OK]  Row {i+1}/{len(results)} | name={row[1]} | phone={row[2]} | {row[3]} | {row[0][:50]}")
        except gspread.exceptions.APIError as e:
            fail_count += 1
            status = getattr(e, 'response', None)
            code   = status.status_code if status else "?"
            print(f"  [ERR] Row {i+1}/{len(results)} failed (HTTP {code}): {e}")
            if code == 429:
                print("        Rate limited — waiting 60s before retrying...")
                time.sleep(60)
                try:
                    sheet.append_row(row)
                    success_count += 1
                    fail_count -= 1
                    print(f"        Retry [OK]")
                except Exception as retry_e:
                    print(f"        Retry [ERR]: {retry_e}")
            elif code == 403:
                print("        Fix: Service account needs Editor access on the sheet.")
                break
        except Exception as e:
            fail_count += 1
            print(f"  [ERR] Row {i+1} unexpected error: {e}")

    print("\n" + "-" * 40)
    print(f"  Rows written : {success_count}")
    print(f"  Rows failed  : {fail_count}")
    if fail_count == 0:
        print("  Status       : ✅ All rows written successfully")
    else:
        print("  Status       : ⚠️  Some rows failed")
    print("-" * 40)


# ── HELPERS ──────────────────────────────────────────────
def bbox_ok(el):
    try:
        bb = el.bounding_box()
        return bb and bb["width"] > 10 and bb["height"] > 5
    except Exception:
        return False

def stop_background_scroll(page):
    page.evaluate("""() => {
        document.body.style.overflow = 'hidden';
        document.documentElement.style.overflow = 'hidden';
        const highId = window.setInterval(() => {}, 99999);
        for (let i = 0; i <= highId; i++) window.clearInterval(i);
    }""")
    ok("Background scroll frozen")

def close_any_dialog(page):
    try:
        for label in ["Close", "إغلاق", "Cancel", "إلغاء"]:
            btn = page.query_selector(f'[aria-label="{label}"][role="button"]')
            if btn and bbox_ok(btn):
                btn.click()
                page.wait_for_timeout(1000)
                ok(f"Closed dialog via '{label}'")
                return True
    except Exception:
        pass
    return False

def extract_seller_name(page):
    try:
        els = page.query_selector_all('a[href*="/marketplace/profile/"]')
        for el in els:
            spans = el.query_selector_all('span')
            for span in spans:
                txt = (span.inner_text() or "").strip()
                if txt and len(txt) > 2 and txt.lower() not in [
                    "seller details", "seller info", "view profile",
                    "see profile", "see seller", "تفاصيل البائع",
                    "profile", "بائع"
                ]:
                    return txt
    except Exception:
        pass

    try:
        els = page.query_selector_all('h2 span')
        for el in els:
            txt = (el.inner_text() or "").strip()
            if txt and len(txt) > 2:
                return txt
    except Exception:
        pass

    return None

def extract_phone_from_page(page):
    try:
        close_any_dialog(page)
        page.wait_for_timeout(500)

        description_selectors = [
            '[data-testid="marketplace-pdp-description"]',
            '[class*="marketplace"][class*="description"]',
            'div[dir="auto"]',
        ]

        for selector in description_selectors:
            try:
                els = page.query_selector_all(selector)
                for el in els:
                    if bbox_ok(el):
                        text = el.inner_text()
                        for match in re.finditer(PHONE_REGEX, text):
                            phone = match.group(0)
                            if phone not in PHONE_BLACKLIST:
                                return phone
            except Exception:
                continue

        body_text = page.evaluate("""() => {
            const dialogs = document.querySelectorAll('[role="dialog"]');
            const hidden = [];
            for (const d of dialogs) {
                hidden.push([d, d.style.display]);
                d.style.display = 'none';
            }
            const text = document.body.innerText;
            for (const [d, display] of hidden) {
                d.style.display = display;
            }
            return text;
        }""")

        for match in re.finditer(PHONE_REGEX, body_text):
            phone = match.group(0)
            if phone not in PHONE_BLACKLIST:
                return phone

    except Exception as e:
        warn(f"Phone extraction error: {e}")

    return None

def open_message_dialog(page):
    log("Clicking Message button")
    for label in ["Message", "Send message", "Message seller", "Contact seller", "مراسلة", "إرسال رسالة"]:
        try:
            els = page.query_selector_all(f'[aria-label="{label}"]')
            for el in els:
                if bbox_ok(el):
                    el.click()
                    ok(f"Opened dialog using '{label}'")
                    return True
        except Exception:
            continue
    try:
        for btn in page.query_selector_all('[role="button"], button'):
            if bbox_ok(btn):
                txt = (btn.inner_text() or "").strip().lower()
                if any(k in txt for k in ["message", "chat", "send", "contact", "مراسلة", "إرسال"]):
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    ok("Opened dialog via button text")
                    return True
    except Exception:
        pass
    return False

def wait_for_dialog(page, timeout=10):
    log("Waiting for message dialog")
    deadline = time.time() + timeout
    while time.time() < deadline:
        el = page.query_selector('textarea[placeholder]')
        if el and bbox_ok(el):
            ok("Dialog textarea found")
            return True
        el = page.query_selector('[aria-label="Send message"], [aria-label="Send"]')
        if el and bbox_ok(el):
            ok("Send button found")
            return True
        time.sleep(0.5)
    return False

def find_dialog_textarea(page):
    for ph in ["Please type your message to the seller", "Type a message",
               "Write a message", "اكتب رسالة", "اكتب رسالتك"]:
        try:
            el = page.query_selector(f'textarea[placeholder="{ph}"]')
            if el and bbox_ok(el):
                ok("Found textarea via placeholder")
                return el
        except Exception:
            continue
    try:
        el = page.query_selector('[role="dialog"] textarea, [role="dialog"] [contenteditable="true"]')
        if el and bbox_ok(el):
            ok("Found textarea inside dialog")
            return el
    except Exception:
        pass
    try:
        for ta in page.query_selector_all("textarea"):
            if bbox_ok(ta):
                ok("Using visible textarea")
                return ta
    except Exception:
        pass
    try:
        for ce in page.query_selector_all('div[contenteditable="true"]'):
            if bbox_ok(ce):
                ok("Using contenteditable div")
                return ce
    except Exception:
        pass
    return None

def clear_and_type(page, el, text):
    tag = el.evaluate("e => e.tagName.toLowerCase()")
    info(f"Element type: {tag}")
    try:
        el.scroll_into_view_if_needed()
        time.sleep(0.2)
        el.click()
        time.sleep(0.3)
        page.keyboard.press("Control+a")
        time.sleep(0.2)
        page.keyboard.type(text, delay=35)
        time.sleep(0.3)
        current = el.input_value() if tag == "textarea" else el.inner_text()
        if text[:10] in current:
            ok("Typing method 1 worked")
            return True
    except Exception as e:
        warn(f"Typing method 1 failed: {e}")
    try:
        if tag == "textarea":
            page.evaluate("""([el, val]) => {
                const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""", [el, text])
        else:
            page.evaluate("""([el, val]) => {
                el.focus();
                document.execCommand('insertText', false, val);
            }""", [el, text])
        ok("Typing method 2 worked")
        return True
    except Exception as e:
        warn(f"Typing method 2 failed: {e}")
    return False

def click_send(page, input_el):
    log("Finding send button")

    try:
        for label in ["Send Message", "Send message", "إرسال الرسالة", "إرسال"]:
            btn = page.query_selector(f'[aria-label="{label}"][role="button"]')
            if btn and bbox_ok(btn):
                btn.click()
                ok(f"Clicked send button via aria-label '{label}'")
                return True
    except Exception:
        pass

    try:
        for btn in page.query_selector_all('[role="dialog"] [role="button"]'):
            if bbox_ok(btn):
                txt = (btn.inner_text() or "").strip()
                if txt.lower() in ["send message", "send", "إرسال الرسالة", "إرسال"]:
                    btn.click()
                    ok(f"Clicked send via text: '{txt}'")
                    return True
    except Exception:
        pass

    try:
        btns = page.query_selector_all('[role="dialog"] [role="button"]')
        if btns:
            last_btn = btns[-1]
            if bbox_ok(last_btn):
                last_btn.click()
                ok("Clicked last button in dialog")
                return True
    except Exception:
        pass

    warn("Send button not found → pressing Enter")
    input_el.press("Enter")
    ok("Pressed Enter")
    return True


# ── MAIN ─────────────────────────────────────────────────
def main():
    global _step
    hrefs = []
    results = []

    with sync_playwright() as pw:

        # ── SESSION: load saved or trigger fresh login ──
        print("\n=== SESSION CHECK ===")
        session_data = load_or_refresh_session(pw)

        browser = pw.chromium.launch(
            headless=True,
            args=BROWSER_ARGS,
            slow_mo=60,
        )
        context = browser.new_context()
        context.add_cookies(session_data["cookies"])

        # ── PHASE 1: Collect listings ─────────────────
        print("\n=== PHASE 1: COLLECTING LISTINGS ===")
        _step = 0

        page = context.new_page()

        for attempt in range(3):
            try:
                page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=120000)
                break
            except Exception as e:
                warn(f"goto attempt {attempt+1} failed: {str(e)[:80]}")
                if attempt == 2:
                    err("Could not load Facebook after 3 attempts.")
                    context.close()
                    return
                time.sleep(3)

        page.wait_for_timeout(8000)

        # ── Check if session was silently rejected ──
        if refresh_session_if_redirected(page, context):
            # Delete the bad session file so next run forces re-login
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
                warn(f"Deleted stale '{SESSION_FILE}' — please re-run the script.")
            context.close()
            browser.close()
            return

        page.screenshot(path="marketplace_loaded.png")

        posts = page.locator('a[href*="/marketplace/item/"]')
        count = posts.count()
        ok(f"Found {count} listing links")

        for i in range(min(count, 30)):
            try:
                href = posts.nth(i).get_attribute("href")
                if href:
                    href = "https://www.facebook.com" + href if href.startswith("/") else href
                    href = href.split("&__tn__")[0]
                    if href not in hrefs:
                        hrefs.append(href)
            except Exception:
                pass

        ok(f"Collected {len(hrefs)} unique listings")
        with open(HREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(hrefs, f, ensure_ascii=False, indent=4)

        # ── Save refreshed cookies after a successful page load ──
        # This extends the effective session life on each run
        refreshed_cookies = context.cookies()
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump({"cookies": refreshed_cookies}, f, ensure_ascii=False, indent=4)
        ok("Session cookies refreshed and saved")

        page.close()

        # ── PHASE 2: Extract phones or send messages ──
        print("\n=== PHASE 2: EXTRACTION & MESSAGING ===")
        _step = 0

        for index, href in enumerate(hrefs):
            print("\n" + "=" * 60)
            print(f"[LISTING {index+1}/{len(hrefs)}]")
            print(href)

            page = context.new_page()

            try:
                log("Opening listing")
                try:
                    page.goto(href, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    try:
                        page.goto(href, wait_until="load", timeout=60000)
                    except Exception as e2:
                        err(f"Could not load page: {str(e2)[:80]}")
                        results.append({"url": href, "seller_name": None, "phone": None, "message_sent": "FAILED_LOAD"})
                        page.close()
                        continue

                # Check for session expiry mid-run
                if refresh_session_if_redirected(page, context):
                    if os.path.exists(SESSION_FILE):
                        os.remove(SESSION_FILE)
                    err("Stopping — please re-run the script to log in again.")
                    page.close()
                    break

                page.wait_for_timeout(5000)

                log("Extracting seller name")
                seller_name = extract_seller_name(page)
                if seller_name:
                    ok(f"Seller name: {seller_name}")
                else:
                    warn("No seller name found")

                log("Searching for phone number")
                phone = extract_phone_from_page(page)

                if phone:
                    ok(f"Phone found: {phone}")
                    results.append({"url": href, "seller_name": seller_name, "phone": phone, "message_sent": False})
                    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                        json.dump(results, f, ensure_ascii=False, indent=4)
                    page.close()
                    continue

                warn("No phone found → opening message dialog")
                opened = open_message_dialog(page)
                if not opened:
                    warn("Could not open message dialog")
                    results.append({"url": href, "seller_name": seller_name, "phone": None, "message_sent": "FAILED_NO_DIALOG"})
                    page.close()
                    continue

                time.sleep(2)
                stop_background_scroll(page)
                time.sleep(1)
                wait_for_dialog(page, timeout=10)

                log("Finding textarea")
                input_el = None
                for attempt in range(5):
                    input_el = find_dialog_textarea(page)
                    if input_el:
                        break
                    time.sleep(1)

                if not input_el:
                    warn("Could not find textarea")
                    page.screenshot(path=f"error_no_input_{index}.png")
                    results.append({"url": href, "seller_name": seller_name, "phone": None, "message_sent": "FAILED_NO_INPUT"})
                    page.close()
                    continue

                log("Typing custom message")
                clear_and_type(page, input_el, CUSTOM_MESSAGE)
                time.sleep(1)

                click_send(page, input_el)
                page.wait_for_timeout(2000)

                dialog_still_open = page.query_selector('[role="dialog"]')
                if not dialog_still_open:
                    ok("Message sent — dialog closed!")
                else:
                    warn("Dialog still open — trying Enter key")
                    input_el.press("Enter")
                    page.wait_for_timeout(2000)

                results.append({"url": href, "seller_name": seller_name, "phone": None, "message_sent": True})
                with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=4)

                page.wait_for_timeout(2000)
                page.close()

            except Exception as e:
                err(str(e))
                try:
                    page.screenshot(path=f"failure_{index}.png")
                except Exception:
                    pass
                results.append({"url": href, "seller_name": None, "phone": None, "message_sent": "FAILED_CRASH"})
                try:
                    page.close()
                except Exception:
                    pass

        context.close()
        browser.close()

    # ── Summary ───────────────────────────────────────
    print("\n" + "=" * 60)
    found  = [r for r in results if r.get("phone")]
    sent   = [r for r in results if r.get("message_sent") is True]
    failed = [r for r in results if str(r.get("message_sent", "")).startswith("FAILED")]

    print(f"Phones found:  {len(found)}/{len(results)}")
    print(f"Messages sent: {len(sent)}/{len(results)}")
    print(f"Failures:      {len(failed)}/{len(results)}")

    send_to_sheets(results)

if __name__ == "__main__":
    main()
