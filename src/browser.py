"""
browser.py — Browser lifecycle and authentication.
"""
from __future__ import annotations

import asyncio
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from apify import Actor

LOGIN_URL = "https://coursology-qbank.com/auth/signin"


# ── Browser factory ──────────────────────────────────────────────────────────

async def launch_browser(headless: bool = True) -> tuple[Browser, BrowserContext, Page]:
    pw = await async_playwright().start()

    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
    )

    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    """)

    page = await context.new_page()
    return browser, context, page


# ── Debug helpers ─────────────────────────────────────────────────────────────

async def _save_screenshot(page: Page, name: str) -> None:
    try:
        png = await page.screenshot(full_page=True)
        store = await Actor.open_key_value_store()
        await store.set_value(name, png, content_type="image/png")
        print(f"[debug] Screenshot saved → KV:{name}")
    except Exception as e:
        print(f"[debug] Could not save screenshot: {e}")


async def _save_html(page: Page, name: str) -> None:
    try:
        html = await page.content()
        store = await Actor.open_key_value_store()
        await store.set_value(name, html.encode("utf-8"), content_type="text/html; charset=utf-8")
        print(f"[debug] HTML saved → KV:{name}")
    except Exception as e:
        print(f"[debug] Could not save HTML: {e}")


async def _fill_react_input(page: Page, selector: str, value: str) -> bool:
    """
    Type into a field character-by-character to trigger React's onChange.
    Returns True if the field was found and filled.
    """
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=8_000)
        await el.click()
        await asyncio.sleep(0.2)
        await el.triple_click()
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.1)
        await el.type(value, delay=40)
        await asyncio.sleep(0.2)
        return True
    except Exception:
        return False


# ── Authentication ────────────────────────────────────────────────────────────

async def login(page: Page, email: str, password: str, **_kwargs) -> None:
    """
    Log in to coursology-qbank.com using the email/password form at /auth/signin.
    """
    print(f"[*] Navigating to login page: {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)

    # Wait for the form to mount (React SPA needs a moment)
    try:
        await page.wait_for_selector("input", timeout=10_000)
    except Exception:
        await _save_screenshot(page, "debug_no_inputs.png")
        await _save_html(page, "debug_no_inputs.html")
        raise RuntimeError(
            f"No <input> elements found on {LOGIN_URL} after 10s. "
            "Check KV Store → debug_no_inputs.png for what the browser saw."
        )

    await _save_screenshot(page, "debug_login_page.png")

    # ── Fill username / email ────────────────────────────────────────────────
    email_selectors = [
        'input[name="username"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[placeholder*="Username" i]',
        'input[placeholder*="Email" i]',
        'input[id*="username" i]',
        'input[id*="email" i]',
        'input[autocomplete="username"]',
        'input[autocomplete="email"]',
        'input:not([type="password"]):not([type="hidden"]):not([type="checkbox"])',
    ]
    email_filled = False
    for sel in email_selectors:
        if await _fill_react_input(page, sel, email):
            print(f"[+] Username/email filled via: {sel}")
            email_filled = True
            break
    if not email_filled:
        await _save_screenshot(page, "debug_email_not_found.png")
        raise RuntimeError("Could not locate the username/email input on the login page.")

    # ── Fill password ────────────────────────────────────────────────────────
    password_selectors = [
        'input[type="password"]',
        'input[name="password"]',
        'input[placeholder*="password" i]',
        'input[id*="password" i]',
        'input[autocomplete="current-password"]',
    ]
    password_filled = False
    for sel in password_selectors:
        if await _fill_react_input(page, sel, password):
            print(f"[+] Password filled via: {sel}")
            password_filled = True
            break
    if not password_filled:
        await _save_screenshot(page, "debug_password_not_found.png")
        raise RuntimeError("Could not locate the password input on the login page.")

    await _save_screenshot(page, "debug_filled_form.png")

    # ── Click Submit ─────────────────────────────────────────────────────────
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'button:has-text("Login")',
        'form button',
    ]
    submitted = False
    for sel in submit_selectors:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=3_000)
            await btn.click()
            print(f"[+] Submit clicked via: {sel}")
            submitted = True
            break
        except Exception:
            continue

    if not submitted:
        print("[!] No submit button found — pressing Enter.")
        await page.keyboard.press("Enter")

    # ── Verify login success ─────────────────────────────────────────────────
    # Success: URL moved away from /auth/signin while staying on coursology-qbank.com.
    # Failure: still on /auth/signin (wrong credentials) or redirected elsewhere.
    try:
        await page.wait_for_url(
            lambda url: "coursology-qbank.com" in url and "/auth/signin" not in url,
            timeout=20_000,
        )
        print(f"[+] Logged in! Current URL: {page.url}")
        await _save_screenshot(page, "debug_post_login.png")

    except Exception:
        current = page.url
        await _save_screenshot(page, "debug_login_failed.png")
        await _save_html(page, "debug_login_failed.html")

        if "/auth/signin" in current:
            raise RuntimeError(
                f"Login failed — still on {current}.\n"
                "  → Check your email and password are correct.\n"
                "  → Check KV Store → debug_login_failed.png for any error message on the page."
            )
        raise RuntimeError(
            f"Login ended on unexpected URL: {current}\n"
            "  → Check KV Store → debug_login_failed.png for details."
        )
