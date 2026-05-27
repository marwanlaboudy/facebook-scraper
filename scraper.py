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
SEARCH_URL     = "https://www.facebook.com/marketplace/cairo/search/?category_id=1270772586445798&query=Home%20Sales&referral_ui_component=category_menu_item"
HREFS_FILE     = "marketplace_hrefs.json"
RESULTS_FILE   = "marketplace_results.json"
CUSTOM_MESSAGE = "ممكن رقم التواصل لأنه مخفي في البوست؟"
PHONE_REGEX    = r"01[0-9]{9}"

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

# ── GOOGLE SHEETS ─────────────────────────────────────────
def send_to_sheets(results):
    print("\n=== SENDING TO GOOGLE SHEETS ===")
    try:
        google_creds_raw = os.environ.get("GOOGLE_CREDS", "")
        if not google_creds_raw:
            print("❌ GOOGLE_CREDS is empty")
            return

        print(f"GOOGLE_CREDS first 50 chars: {google_creds_raw[:50]}")

        creds_dict = json.loads(google_creds_raw)
        print(f"✅ JSON parsed — type: {creds_dict.get('type')} | email: {creds_dict.get('client_email')}")

        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open("marketplace_leads").sheet1

        existing = sheet.get_all_values()
        if not existing:
            sheet.append_row(["URL", "Phone", "Message Sent"])

        for r in results:
            sheet.append_row([
                r.get("url", ""),
                r.get("phone") or "",
                str(r.get("message_sent", ""))
            ])

        print(f"✅ Sent {len(results)} rows to Google Sheets")

    except json.JSONDecodeError as e:
        print(f"❌ GOOGLE_CREDS is not valid JSON: {e}")
        print(f"   Raw value starts with: {google_creds_raw[:100]}")
    except Exception as e:
        print(f"❌ Google Sheets Error: {e}")

# ── HELPERS ──────────────────────────────────────────────
def bbox_ok(el):
    try:
        bb = el.bounding_box()
        return bb and bb["width"] > 10 and bb["height"] > 5
    except Exception:
        return False

def click_continue_if_present(page):
    try:
        btns = page.query_selector_all('[role="button"]')
        for b in btns:
            try:
                if (b.inner_text() or "").strip().lower() == "continue":
                    if bbox_ok(b):
                        b.click()
                        ok("Clicked Continue button")
                        page.wait_for_timeout(5000)
                        return True
            except Exception:
                pass
    except Exception:
        pass
    return False

def stop_background_scroll(page):
    page.evaluate("""() => {
        document.body.style.overflow = 'hidden';
        document.documentElement.style.overflow = 'hidden';
        const highId = window.setInterval(() => {}, 99999);
        for (let i = 0; i <= highId; i++) window.clearInterval(i);
    }""")
    ok("Background scroll frozen")

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
    for label in ["Send message", "Send", "إرسال الرسالة", "إرسال"]:
        try:
            els = page.query_selector_all(f'[aria-label="{label}"]')
            for el in els:
                if bbox_ok(el):
                    el.click()
                    ok("Clicked send button")
                    return True
        except Exception:
            continue
    try:
        for btn in page.query_selector_all('[role="button"], button'):
            if bbox_ok(btn):
                txt = (btn.inner_text() or "").strip()
                if txt.lower() in ["send", "send message", "إرسال", "إرسال الرسالة"]:
                    btn.click()
                    ok("Clicked send via text")
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
        browser = pw.chromium.launch(headless=False, args=BROWSER_ARGS, slow_mo=60)
        context = browser.new_context()

        # ── PHASE 1: Collect listings & Handle Login ──
        print("\n=== PHASE 1: COLLECTING LISTINGS & LOGIN ===")
        _step = 0

        # Load session safely
        session_file = "facebook_session.json"
        session = {}
        try:
            with open(session_file) as f:
                session = json.load(f)
            context.add_cookies(session.get("cookies", []))
            log("Loaded existing cookies")
        except (FileNotFoundError, json.JSONDecodeError):
            warn("No valid session found. Will start fresh.")

        page = context.new_page()

        # Restore localStorage
        log("Navigating to Facebook...")
        page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        if "origins" in session:
            for origin in session["origins"]:
                if origin["origin"] == "https://www.facebook.com":
                    for item in origin.get("localStorage", []):
                        try:
                            page.evaluate(
                                f"localStorage.setItem({json.dumps(item['name'])}, {json.dumps(item['value'])})"
                            )
                        except Exception:
                            pass

        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # Click Continue if present
        click_continue_if_present(page)
        page.wait_for_timeout(3000)

        # ── THE MANUAL LOGIN CHECK ──
        title = page.title().lower()
        url = page.url.lower()
        
        email_input = page.locator('input[name="email"]')
        
        if "log in" in title or "login" in url or email_input.is_visible():
            warn("⚠️ NOT LOGGED IN! The bot is pausing for you.")
            warn("👉 Please log in manually in the opened browser window.")
            warn("⏳ You have 5 minutes to enter your email, password, and any 2FA codes...")
            
            try:
                # Wait for the email input to disappear (meaning login was successful and we navigated away)
                email_input.wait_for(state="hidden", timeout=300000) # 5 minute timeout
                
                # Give Facebook a few seconds to fully load the feed
                page.wait_for_timeout(5000)
                
                ok("✅ Detected successful manual login!")
                
                # Save the new session so you don't have to do this next time
                context.storage_state(path=session_file)
                ok(f"✅ Saved fresh session to {session_file}")
                
            except Exception:
                err("❌ Did not detect a successful login within 5 minutes.")
                page.screenshot(path="login_timeout.png")
                context.close()
                sys.exit(1)
        else:
            ok("✅ Logged in successfully via existing session")

        # Go to marketplace
        for attempt in range(3):
            try:
                page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=120000)
                break
            except Exception as e:
                warn(f"goto attempt {attempt+1} failed: {str(e)[:80]}")
                if attempt == 2:
                    err("Could not load Facebook Marketplace after 3 attempts.")
                    context.close()
                    sys.exit(1)
                time.sleep(3)

        page.wait_for_timeout(8000)
        page.screenshot(path="marketplace_loaded.png")

        posts = page.locator('a[href*="/marketplace/item/"]')
        count = posts.count()
        ok(f"Found {count} listing links")

        for i in range(min(count, 5)):
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
                        results.append({"url": href, "phone": None, "message_sent": "FAILED_LOAD"})
                        page.close()
                        continue

                page.wait_for_timeout(5000)

                log("Extracting page text")
                body_text = page.locator("body").inner_text()

                log("Searching for phone number")
                match = re.search(PHONE_REGEX, body_text)

                if match:
                    phone = match.group(0)
                    ok(f"Phone found: {phone}")
                    results.append({"url": href, "phone": phone, "message_sent": False})
                    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                        json.dump(results, f, ensure_ascii=False, indent=4)
                    page.close()
                    continue

                warn("No phone found → opening message dialog")
                opened = open_message_dialog(page)
                if not opened:
                    warn("Could not open message dialog")
                    results.append({"url": href, "phone": None, "message_sent": "FAILED_NO_DIALOG"})
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
                    results.append({"url": href, "phone": None, "message_sent": "FAILED_NO_INPUT"})
                    page.close()
                    continue

                log("Typing custom message")
                clear_and_type(page, input_el, CUSTOM_MESSAGE)
                time.sleep(1)
                click_send(page, input_el)
                ok("Message sent!")

                results.append({"url": href, "phone": None, "message_sent": True})
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
                results.append({"url": href, "phone": None, "message_sent": "FAILED_CRASH"})
                try:
                    page.close()
                except Exception:
                    pass

        context.close()

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
