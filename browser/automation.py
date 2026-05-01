"""
Browser Automation Engine — manages Playwright lifecycle and dispatches lender scripts.
"""

import asyncio
from config import settings
from browser.lender_scripts import get_script, GenericScript, ApplyResult
from utils.logger import log


class BrowserEngine:
    def __init__(self):
        self._browser = None
        self._playwright = None

    async def start(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
            ],
        )
        log.info("Browser engine started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("Browser engine stopped")

    async def execute_application(
        self,
        lender_name: str,
        script_name: str,
        application_url: str,
        business_data: dict,
    ) -> ApplyResult:
        """
        Run a lender-specific application script in an isolated browser context.
        Each application gets its own context (separate cookies/sessions).
        """
        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )

        page = await context.new_page()

        # Stealth: mask automation signals
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        try:
            ScriptClass = get_script(script_name)

            if ScriptClass == GenericScript or ScriptClass.__name__ == "GenericScript":
                script = GenericScript(page, business_data, application_url=application_url)
            else:
                script = ScriptClass(page, business_data)
                script.lender_name = lender_name

            log.info(f"Running {script_name} script for {lender_name}")
            result = await script.apply()
            return result

        except Exception as e:
            log.error(f"Browser execution error for {lender_name}: {e}")
            return ApplyResult(success=False, submitted=False, error=str(e),
                               status_message=f"Automation error: {e}")
        finally:
            await context.close()
