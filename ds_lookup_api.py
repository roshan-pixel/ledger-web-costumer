import os
import tempfile
import threading
from playwright.sync_api import sync_playwright

SESSION_STATE_FILE = os.path.join(tempfile.gettempdir(), 'awpl_portal_session.json')
_portal_lock = threading.Lock()   # one headless browser at a time (low-memory host)
SALE_URL = 'https://asclepiuswellness.com/shoppingpoint/SpdistributorSale.aspx'
LOGIN_URL = 'https://asclepiuswellness.com/login.aspx?webid=1'


def fetch_ds_from_portal(ds_code):
    """Live DS lookup in the AWPL portal: log in (session reused), type the DS id, read the name/mobile.
    Needs PORTAL_USER and PORTAL_PASSWORD env vars. Returns dict or None."""
    username = os.environ.get('PORTAL_USER')
    password = os.environ.get('PORTAL_PASSWORD')
    if not username or not password:
        print('Portal lookup skipped: PORTAL_USER / PORTAL_PASSWORD not set')
        return None

    args = ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
            '--disable-gpu', '--no-first-run', '--disable-extensions']
    with _portal_lock, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=args)
        try:
            ctx = None
            if os.path.exists(SESSION_STATE_FILE):
                try:
                    ctx = browser.new_context(storage_state=SESSION_STATE_FILE)
                except Exception:
                    ctx = None
            if ctx is None:
                ctx = browser.new_context()
            page = ctx.new_page()
            page.route('**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot,mp4,mp3,ico}', lambda r: r.abort())

            page.goto(SALE_URL, wait_until='domcontentloaded', timeout=20000)
            if 'login.aspx' in page.url or page.locator('#ctl00_ContentPlaceHolder1_txtid').count() == 0:
                if 'login.aspx' not in page.url:
                    page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=20000)
                page.fill('#ctl00_ContentPlaceHolder1_txtspUserid', username)
                page.fill('#ctl00_ContentPlaceHolder1_txtsppassword', password)
                with page.expect_navigation(wait_until='domcontentloaded', timeout=20000):
                    page.click('#ctl00_ContentPlaceHolder1_btnfranlogin')
                try:
                    ctx.storage_state(path=SESSION_STATE_FILE)
                except Exception:
                    pass
                page.goto(SALE_URL, wait_until='domcontentloaded', timeout=20000)

            page.locator('#ctl00_ContentPlaceHolder1_txtid').fill(ds_code)
            page.keyboard.press('Tab')
            try:
                page.wait_for_function(
                    "() => { const el = document.querySelector('#ctl00_ContentPlaceHolder1_txtname');"
                    " return el && el.value.trim().length > 0; }", timeout=6000)
            except Exception:
                pass
            name = page.input_value('#ctl00_ContentPlaceHolder1_txtname').strip()
            if not name:
                return None
            mobile = page.input_value('#ctl00_ContentPlaceHolder1_txtmobile').strip()
            return {'ds_code': ds_code, 'ds_name': name, 'mobile': mobile}
        except Exception as e:
            print('Error in fetch_ds_from_portal:', e)
            return None
        finally:
            browser.close()
