import json
import sys
from playwright.sync_api import sync_playwright

def check_login():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        # Load cookies
        with open("facebook_session.json") as f:
            session = json.load(f)
        context.add_cookies(session["cookies"])

        page = context.new_page()

        # Go to Facebook
        page.goto("https://www.facebook.com", wait_until="networkidle")

        # Restore localStorage
        if "origins" in session:
            for origin in session["origins"]:
                if origin["origin"] == "https://www.facebook.com":
                    for item in origin.get("localStorage", []):
                        try:
                            page.evaluate(
                                f"localStorage.setItem({json.dumps(item['name'])}, {json.dumps(item['value'])})"
                            )
                        except:
                            pass

        page.reload()
        page.wait_for_timeout(3000)

        # Take screenshot
        page.screenshot(path="login_check.png")

        # Check 1: URL should NOT be login page
        current_url = page.url
        print(f"Current URL: {current_url}")

        # Check 2: Look for logged-in elements
        logged_in_signals = [
            '[aria-label="Your profile"]',
            '[aria-label="Account"]',
            'a[href="/me/"]',
            '[data-testid="blue_bar_profile_link"]',
        ]

        found_signal = None
        for selector in logged_in_signals:
            try:
                el = page.query_selector(selector)
                if el:
                    found_signal = selector
                    break
            except:
                pass

        # Check 3: Page title
        title = page.title()
        print(f"Page title: {title}")

        # Check 4: No login form present
        login_form = page.query_selector('input[name="email"]')

        # Final verdict
        is_logged_in = (
            "login" not in current_url.lower()
            and login_form is None
            and "log in" not in title.lower()
        )

        if is_logged_in:
            print("✅ LOGIN SUCCESSFUL — Session is valid")
            if found_signal:
                print(f"   Confirmed by: {found_signal}")
        else:
            print("❌ LOGIN FAILED — Session is expired or invalid")
            if "login" in current_url.lower():
                print("   Reason: Redirected to login page")
            if login_form:
                print("   Reason: Login form is visible on page")
            if "log in" in title.lower():
                print(f"   Reason: Page title is '{title}'")

        browser.close()
        return is_logged_in

if __name__ == "__main__":
    success = check_login()
    sys.exit(0 if success else 1)
