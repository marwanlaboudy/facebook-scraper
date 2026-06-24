import time
import sys
import json
import re
import os
from playwright.sync_api import sync_playwright
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ── WINDOWS UTF-8 FIX ───────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ──────────────────────────────────────────────
START_URL    = "https://www.facebook.com"
RESULTS_FILE = "marketplace_chat_numbers.json"

PHONE_REGEX = r"(?:(?:\+|00)?2)?(01[0125](?:[\s\-\.]*[0-9]){8})"

SKIP_PREFIXES = ["You:", "أنت:", "All", "Unread", "Groups", "Communities",
                 "الكل", "غير مقروء", "المجموعات", "Marketplace", "ماركت بليس", "السوق"]

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--no-proxy-server",
    "--ignore-certificate-errors",
]

# ── GOOGLE SHEETS ─────────────────────────────────────────
def send_to_sheets(results):
    print("\n" + "=" * 60)
    print("SENDING TO GOOGLE SHEETS (Sheet 2)")
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

    print("\n[STEP 3] Opening spreadsheet 'marketplace_leads' — Sheet 2...")
    try:
        spreadsheet = client.open("marketplace_leads")
        worksheets = spreadsheet.worksheets()
        if len(worksheets) >= 2:
            sheet = worksheets[1]
            print(f"  [OK] Opened existing sheet: '{sheet.title}'")
        else:
            sheet = spreadsheet.add_worksheet(title="Sheet2", rows="1000", cols="2")
            print(f"  [OK] Created new sheet: '{sheet.title}'")
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
            sheet.append_row(["Name", "Number"])
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
        if not r.get("phones"):
            continue
        phones_str = ", ".join(r["phones"])
        seller = r.get("seller_name", "").strip()
        row = [seller, phones_str]
        try:
            sheet.append_row(row)
            success_count += 1
            print(f"  [OK]  Row {i+1} | name={seller[:40]} | phone={phones_str}")
        except gspread.exceptions.APIError as e:
            fail_count += 1
            status = getattr(e, 'response', None)
            code   = status.status_code if status else "?"
            print(f"  [ERR] Row {i+1} failed (HTTP {code}): {e}")
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
    print("-" * 40)

# ── HELPERS ─────────────────────────────────────────────
def normalize_arabic_numerals(text):
    if not text:
        return ""
    arabic_to_english = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')
    return text.translate(arabic_to_english)

def extract_phones(text):
    if not text:
        return []
    normalized = normalize_arabic_numerals(text)
    matches = re.findall(PHONE_REGEX, normalized)
    results = []
    for m in matches:
        clean = re.sub(r'[\s\-\.]', '', m)
        if clean.startswith('201'):
            clean = clean[1:]
        if clean not in results:
            results.append(clean)
    return results

def is_within_24h(aria_label):
    if not aria_label:
        return False
    label = aria_label.lower().strip()
    if "minute" in label or "just now" in label or "second" in label:
        return True
    match = re.match(r'(\d+)\s+hour', label)
    if match and int(match.group(1)) <= 23:
        return True
    return False

def should_skip(name):
    if not name or len(name.strip()) < 5:
        return True
    for prefix in SKIP_PREFIXES:
        if name.strip().startswith(prefix):
            return True
    return False

# ── MESSENGER POPUP (IMPROVED) ───────────────────────────
def open_messenger_popup(page):
    labels = ["Messenger", "مراسلة", "Chats", "الدردشات"]
    for label in labels:
        el = page.query_selector(f'[aria-label="{label}"]')
        if el:
            try:
                el.click(force=True)
                page.wait_for_timeout(1500)
                print(f"    [OK] Found Messenger button with label: '{label}'")
                return True
            except Exception:
                continue

    # Fallback: find by href link
    el = page.query_selector('a[href*="messenger.com"], a[href*="/messages/"]')
    if el:
        try:
            el.click(force=True)
            page.wait_for_timeout(1500)
            print("    [OK] Found Messenger via href fallback")
            return True
        except Exception:
            pass

    # Fallback: find SVG messenger icon by role
    try:
        btns = page.query_selector_all('[role="button"]')
        for btn in btns:
            aria = (btn.get_attribute("aria-label") or "").lower()
            if "mess" in aria or "chat" in aria or "دردش" in aria or "رسائل" in aria:
                btn.click(force=True)
                page.wait_for_timeout(1500)
                print(f"    [OK] Found Messenger button via fuzzy aria: '{aria}'")
                return True
    except Exception as e:
        print(f"    [WARN] Fuzzy button search failed: {e}")

    # Debug: dump all aria-labels on the page to help diagnose
    try:
        all_labels = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('[aria-label]'))
                .map(el => el.getAttribute('aria-label'))
                .filter(l => l && l.length < 60);
        }""")
        print(f"    [DBG] All aria-labels on page: {all_labels[:30]}")
    except Exception:
        pass

    return False

def click_marketplace_tab(page):
    print("    Looking for Marketplace INSIDE messenger popup...")
    try:
        popup = page.locator('[aria-label*="Chats"], [aria-label*="الدردشات"]').first
        marketplace = popup.locator('text="Marketplace"').first
        if marketplace.is_visible():
            marketplace.click(force=True)
            page.wait_for_timeout(2000)
            print("    [OK] Clicked Messenger Marketplace tab")
            return True
    except Exception as e:
        print(f"    [WARN] Primary method failed: {e}")

    try:
        dialogs = page.locator('[role="dialog"]')
        for i in range(dialogs.count()):
            dialog = dialogs.nth(i)
            el = dialog.locator('text="Marketplace"').first
            if el.count() > 0 and el.is_visible():
                el.click(force=True)
                page.wait_for_timeout(2000)
                print("    [OK] Clicked Marketplace inside dialog")
                return True
    except Exception as e:
        print(f"    [WARN] Fallback failed: {e}")

    return False

def scroll_chat_list(page, scrolls=8):
    print(f"    Scrolling chat list ({scrolls} scrolls)...")
    try:
        scrolled = page.evaluate("""(scrolls) => {
            const abbr = document.querySelector('abbr[aria-label]');
            if (!abbr) return false;
            let node = abbr;
            for (let i = 0; i < 20; i++) {
                node = node.parentElement;
                if (!node) break;
                const style = window.getComputedStyle(node);
                if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                    for (let s = 0; s < scrolls; s++) {
                        node.scrollTop += 300;
                    }
                    return true;
                }
            }
            return false;
        }""", scrolls)
        if scrolled:
            page.wait_for_timeout(2000)
            print("    [OK] Scrolled chat list")
        else:
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(2000)
    except Exception as e:
        print(f"    [WARN] Scroll failed: {e}")

def collect_chat_data(page):
    return page.evaluate("""() => {
        const results = [];
        const seen = new Set();
        const abbrs = document.querySelectorAll('abbr[aria-label]');
        for (const abbr of abbrs) {
            const timeLabel = abbr.getAttribute('aria-label') || '';
            let node = abbr;
            let chatName = '';
            for (let i = 0; i < 25; i++) {
                node = node.parentElement;
                if (!node) break;
                const titleSpan = node.querySelector(
                    'span.x1lliihq.x6ikm8r.x10wlt62.x1n2onr6.xlyipyv.xuxw1ft:not(.x1j85h84)'
                );
                if (titleSpan) {
                    const txt = titleSpan.textContent.trim();
                    if (txt.length > 5 && !seen.has(txt)) {
                        chatName = txt;
                        seen.add(txt);
                        break;
                    }
                }
            }
            if (chatName) {
                results.push({ name: chatName, time: timeLabel });
            }
        }
        return results;
    }""")

def find_and_click_chat(page, chat_name):
    return page.evaluate("""(targetName) => {
        const spans = document.querySelectorAll(
            'span.x1lliihq.x6ikm8r.x10wlt62.x1n2onr6.xlyipyv.xuxw1ft:not(.x1j85h84)'
        );
        for (const span of spans) {
            if (span.textContent.trim() === targetName) {
                let node = span;
                for (let i = 0; i < 10; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    if (node.getAttribute('role') === 'link' ||
                        node.getAttribute('role') === 'button' ||
                        node.tagName === 'A') {
                        node.click();
                        return true;
                    }
                }
                span.click();
                return true;
            }
        }
        const allSpans = document.querySelectorAll('span');
        for (const span of allSpans) {
            const txt = span.textContent.trim();
            if (txt === targetName || (targetName.length > 30 && targetName.startsWith(txt) && txt.length > 20)) {
                let node = span;
                for (let i = 0; i < 15; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    if (node.getAttribute('role') === 'link' ||
                        node.getAttribute('role') === 'button' ||
                        node.tagName === 'A') {
                        node.click();
                        return true;
                    }
                }
            }
        }
        return false;
    }""", chat_name)

def go_back_to_chat_list(page):
    try:
        for label in ["Back", "رجوع"]:
            back = page.query_selector(f'[aria-label="{label}"][role="button"]')
            if back and back.is_visible():
                back.click(force=True)
                page.wait_for_timeout(1500)
                return True
    except Exception:
        pass
    try:
        open_messenger_popup(page)
        click_marketplace_tab(page)
        page.wait_for_timeout(1500)
        return True
    except Exception:
        pass
    return False

def wait_for_chat_load(page, chat_name, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = page.locator("body").text_content() or ""
            if chat_name[:20] in body:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

def scan_chat_for_phones(page, already_seen):
    candidates = page.evaluate("""() => {
        const results = [];
        const labeled = [
            document.querySelector('[role="main"]'),
            document.querySelector('[data-pagelet="MWChat"]'),
            document.querySelector('[aria-label="Messages"]'),
            document.querySelector('[aria-label="رسائل"]'),
        ];
        for (const c of labeled) {
            if (c) {
                const txt = Array.from(c.querySelectorAll('[dir="auto"]'))
                    .map(s => s.textContent).join(' ');
                if (txt.trim().length > 50) results.push(txt);
            }
        }
        const allScrollable = Array.from(document.querySelectorAll('*')).filter(el => {
            const s = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return (s.overflowY === 'auto' || s.overflowY === 'scroll') &&
                   el.scrollHeight > 200 &&
                   rect.width > 400;
        });
        for (const el of allScrollable) {
            const txt = Array.from(el.querySelectorAll('[dir="auto"]'))
                .map(s => s.textContent).join(' ');
            if (txt.trim().length > 50) results.push(txt);
        }
        return results;
    }""")

    if not candidates:
        print(f"    [DBG] No candidates found")
        return []

    best_text = max(candidates, key=len)
    print(f"    [DBG] Best candidate: {len(best_text)} chars from {len(candidates)} sources")
    all_phones = extract_phones(best_text)
    new_phones = [p for p in all_phones if p not in already_seen]
    print(f"    [DBG] All phones: {all_phones} → new only: {new_phones}")
    return new_phones

# ── SELLER PROFILE ───────────────────────────────────────

def get_seller_profile_url(page):
    url = page.evaluate("""() => {
        const links = Array.from(document.querySelectorAll('a[href*="/marketplace/profile/"], a[href*="profile.php"]'));
        for (const link of links) {
            const txt = (link.textContent || '').trim().toLowerCase();
            const aria = (link.getAttribute('aria-label') || '').toLowerCase();
            if (txt.includes('seller') || txt.includes('profile') ||
                txt.includes('بائع') || aria.includes('seller') ||
                aria.includes('profile') || aria.includes('بائع')) {
                return link.href;
            }
        }
        for (const link of links) {
            const rect = link.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) return link.href;
        }
        return null;
    }""")
    return url

def get_seller_name_from_page(page):
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    try:
        name = page.evaluate("""() => {
            const allSpans = Array.from(document.querySelectorAll('span[dir="auto"]'));
            let joinedIndex = -1;
            for (let i = 0; i < allSpans.length; i++) {
                if (allSpans[i].textContent.trim().startsWith('Joined Facebook') ||
                    allSpans[i].textContent.trim().startsWith('انضم إلى فيسبوك') ||
                    allSpans[i].textContent.trim().startsWith('انضمت إلى فيسبوك')) {
                    joinedIndex = i;
                    break;
                }
            }
            if (joinedIndex < 1) return '';
            for (let i = joinedIndex - 1; i >= 0; i--) {
                const txt = allSpans[i].textContent.trim();
                const lower = txt.toLowerCase();
                if (txt.length < 2 || txt.length > 80) continue;
                if (lower.includes('listing') || lower.includes('active') ||
                    lower.includes('joined') || lower.includes('facebook') ||
                    lower.includes('follow') || lower.includes('message') ||
                    lower.includes('marketplace') || lower.includes('notification') ||
                    lower.includes('inbox') || lower.includes('selling') ||
                    lower.includes('buying') || lower.includes('browse') ||
                    /^\\d+$/.test(txt)) continue;
                return txt;
            }
            return '';
        }""")
        if name and len(name) > 1:
            print(f"    [OK] Seller name (before 'Joined Facebook'): {name}")
            return name
    except Exception as e:
        print(f"    [WARN] Strategy 1 failed: {e}")

    try:
        name = page.evaluate("""() => {
            const m = document.querySelector('meta[property="og:title"]');
            return m ? m.getAttribute('content') : '';
        }""")
        if name and len(name) > 1 and "facebook" not in name.lower() and "marketplace" not in name.lower():
            print(f"    [OK] Seller name from og:title: {name}")
            return name
    except Exception as e:
        print(f"    [WARN] Strategy 2 failed: {e}")

    try:
        title = page.title()
        for sep in [" | ", " - "]:
            if sep in title:
                candidate = title.split(sep)[0].strip()
                if candidate and "facebook" not in candidate.lower() and "marketplace" not in candidate.lower():
                    print(f"    [OK] Seller name from page title: {candidate}")
                    return candidate
    except Exception as e:
        print(f"    [WARN] Strategy 3 failed: {e}")

    try:
        name = page.evaluate("""() => {
            const el = document.querySelector('span.x14qwyeo.xw06pyt.x579bpy.xjkpybl');
            return el ? el.textContent.trim() : '';
        }""")
        if name and len(name) > 1:
            print(f"    [OK] Seller name (CSS class fallback): {name}")
            return name
    except Exception as e:
        print(f"    [WARN] Strategy 4 failed: {e}")

    print("    [WARN] Could not extract seller name")
    return ""

def fetch_seller_name(page, chat_name):
    print("    Getting seller profile URL...")
    profile_url = get_seller_profile_url(page)

    if not profile_url:
        print("    [WARN] No seller profile link found in chat")
        return ""

    print(f"    [DBG] Profile URL: {profile_url[:80]}")

    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        print(f"    [ERR] Could not navigate to profile: {e}")
        _restore_marketplace(page)
        return ""

    current_url = page.url
    print(f"    [DBG] Landed on: {current_url[:80]}")

    if "facebook.com" not in current_url or current_url == "https://www.facebook.com/":
        print("    [WARN] Profile navigation redirected to homepage")
        _restore_marketplace(page)
        return ""

    seller_name = get_seller_name_from_page(page)
    _restore_marketplace(page)
    return seller_name

def _restore_marketplace(page):
    print("    Returning to Facebook and reopening Marketplace...")
    try:
        page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"    [WARN] Could not navigate to homepage: {e}")
        return

    if not open_messenger_popup(page):
        print("    [WARN] Could not reopen Messenger popup")
        return

    if not click_marketplace_tab(page):
        print("    [WARN] Could not reopen Marketplace tab")
        return

    page.wait_for_timeout(1500)
    print("    [OK] Back at Marketplace chat list")

# ── MAIN ────────────────────────────────────────────────
def main():
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=BROWSER_ARGS,
            slow_mo=50,
        )
        context = browser.new_context()

        session_file = "facebook_session.json"
        if not os.path.exists(session_file):
            print(f"[ERR] '{session_file}' not found.")
            browser.close()
            return

        with open(session_file) as f:
            session = json.load(f)
        context.add_cookies(session["cookies"])
        print("[OK] Loaded cookies from facebook_session.json")

        page = context.new_page()

        print("[1] Opening Facebook...")
        page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)  # increased from 4000 to 8000

        # Screenshot to help debug if things go wrong
        page.screenshot(path="debug_after_load.png")
        print("    [DBG] Screenshot saved: debug_after_load.png")

        # Check if we're actually logged in
        current_url = page.url
        print(f"    [DBG] Current URL: {current_url}")
        if "login" in current_url or "checkpoint" in current_url:
            print("    [ERR] Session expired or login required. Please refresh facebook_session.json")
            browser.close()
            return

        print("[2] Opening Messenger popup...")
        if not open_messenger_popup(page):
            page.screenshot(path="debug_messenger_fail.png")
            print("    [ERR] Could not find Messenger button. Screenshot saved: debug_messenger_fail.png")
            browser.close()
            return
        print("    [OK] Messenger popup opened")

        print("[3] Clicking Marketplace tab...")
        if not click_marketplace_tab(page):
            print("    [ERR] Could not find Marketplace tab")
            browser.close()
            return
        print("    [OK] Marketplace tab opened")
        page.wait_for_timeout(2000)

        print("[4] Scrolling to load all chats...")
        scroll_chat_list(page, scrolls=10)

        print("[5] Scanning chat list...")
        chat_data = collect_chat_data(page)
        print(f"    Found {len(chat_data)} entries from JS scan")

        seen_names = set()
        recent_chats = []
        skipped_old = 0

        for chat in chat_data:
            name = chat["name"].strip()
            time_label = chat["time"]
            if should_skip(name):
                continue
            if name in seen_names:
                continue
            if not is_within_24h(time_label):
                skipped_old += 1
                continue
            seen_names.add(name)
            recent_chats.append(chat)
            print(f"    [+] ({time_label}): {name[:70]}")

        print(f"    Skipped {skipped_old} chats older than 24h")
        print(f"\n[6] Found {len(recent_chats)} valid chats within 24h")

        all_seen_phones = set()

        for i, chat in enumerate(recent_chats):
            chat_name = chat["name"].strip()
            time_label = chat["time"]

            print(f"\n{'='*60}")
            print(f"[Chat {i+1}/{len(recent_chats)}] ({time_label})")
            print(f"    Name: {chat_name[:80]}")

            seller_name = ""

            try:
                go_back_to_chat_list(page)
                scroll_chat_list(page, scrolls=3)
                page.wait_for_timeout(1000)

                clicked = False
                for attempt in range(3):
                    clicked = find_and_click_chat(page, chat_name)
                    if clicked:
                        break
                    print(f"    [RETRY {attempt+1}] Chat not found, scrolling more...")
                    scroll_chat_list(page, scrolls=3)
                    page.wait_for_timeout(1000)

                if not clicked:
                    print(f"    [ERR] Could not find chat after retries — skipping")
                    results.append({"chat_name": chat_name, "seller_name": "",
                                    "time": time_label, "phones": [], "note": "NOT_FOUND"})
                    continue

                print("    [OK] Clicked chat")
                wait_for_chat_load(page, chat_name, timeout=8)
                page.wait_for_timeout(2000)

                current_url = page.url
                if "photo" in current_url or "/posts/" in current_url:
                    print(f"    [WARN] Wrong page detected")
                    go_back_to_chat_list(page)
                    results.append({"chat_name": chat_name, "seller_name": "",
                                    "time": time_label, "phones": [], "note": "WRONG_PAGE"})
                    continue

                phones = scan_chat_for_phones(page, already_seen=all_seen_phones)

                if phones:
                    print(f"    [OK] Numbers found: {phones}")
                    all_seen_phones.update(phones)
                    seller_name = fetch_seller_name(page, chat_name)
                    if seller_name:
                        print(f"    [OK] Seller name: {seller_name}")
                    else:
                        print(f"    [--] Seller name not retrieved")
                else:
                    print(f"    [--] No phone number found")

                results.append({
                    "chat_name": chat_name,
                    "seller_name": seller_name,
                    "time": time_label,
                    "phones": phones
                })

                with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=4)

            except Exception as e:
                print(f"    [ERR] {str(e)[:100]}")
                try:
                    go_back_to_chat_list(page)
                except Exception:
                    pass
                results.append({"chat_name": chat_name, "seller_name": seller_name,
                                "time": time_label, "phones": [], "note": f"CRASH: {str(e)[:60]}"})

        print(f"\n{'='*60}")
        print(f"[DONE] Processed {len(recent_chats)} chats")
        print(f"Results saved to: {RESULTS_FILE}")
        found = [r for r in results if r["phones"]]
        print(f"\nNumbers found in {len(found)}/{len(results)} chats:")
        for r in found:
            print(f"  {r['phones']} — seller: {r.get('seller_name', '')} — chat: {r['chat_name'][:50]}")

        browser.close()

    send_to_sheets(results)

if __name__ == "__main__":
    main()
