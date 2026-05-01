"""Base class for all lender automation scripts."""

import asyncio
import base64
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional


SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)


@dataclass
class ApplyResult:
    success: bool
    submitted: bool
    reference_number: Optional[str] = None
    account_number: Optional[str] = None
    approved_amount: Optional[float] = None
    status_message: str = ""
    screenshot_path: Optional[str] = None
    needs_manual: bool = False
    captcha_detected: bool = False
    error: Optional[str] = None


class BaseLenderScript:
    """
    Base class for lender-specific automation.
    Each lender gets its own subclass with the exact navigation flow.
    """
    lender_name: str = "Unknown"
    requires_manual: bool = False

    def __init__(self, page, business_data: dict):
        self.page = page
        self.data = business_data

    async def apply(self) -> ApplyResult:
        raise NotImplementedError

    # ─── Helpers ──────────────────────────────────────────────────────────────

    async def fill(self, selector: str, value: str, clear: bool = True):
        """Fill a text input."""
        if not value:
            return
        try:
            el = self.page.locator(selector).first
            await el.wait_for(state="visible", timeout=5000)
            await el.scroll_into_view_if_needed()
            if clear:
                await el.fill("")
            await el.type(str(value), delay=40)
        except Exception:
            pass

    async def fill_first(self, selectors: list[str], value: str):
        """Try multiple selectors, fill the first one found."""
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.fill("")
                    await el.type(str(value), delay=40)
                    return True
            except Exception:
                continue
        return False

    async def select(self, selector: str, value: str, by: str = "value"):
        """Select a dropdown option."""
        try:
            el = self.page.locator(selector).first
            await el.wait_for(state="visible", timeout=5000)
            if by == "label":
                await el.select_option(label=value)
            elif by == "value":
                await el.select_option(value=value)
        except Exception:
            pass

    async def click(self, selector: str, timeout: int = 8000):
        """Click an element."""
        try:
            el = self.page.locator(selector).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.click()
        except Exception:
            pass

    async def click_first(self, selectors: list[str]):
        """Try multiple selectors, click the first visible one."""
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    return True
            except Exception:
                continue
        return False

    async def wait_and_click(self, selector: str, delay: float = 1.0):
        await asyncio.sleep(delay)
        await self.click(selector)

    async def navigate(self, url: str):
        await self.page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(1.5)

    async def wait_for_url(self, pattern: str, timeout: int = 15000):
        await self.page.wait_for_url(f"**{pattern}**", timeout=timeout)

    async def is_visible(self, selector: str) -> bool:
        try:
            el = self.page.locator(selector).first
            return await el.is_visible()
        except Exception:
            return False

    async def get_text(self, selector: str) -> str:
        try:
            return await self.page.locator(selector).first.inner_text()
        except Exception:
            return ""

    async def screenshot(self, suffix: str) -> str:
        safe_name = self.lender_name.replace(" ", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOTS_DIR / f"{safe_name}_{suffix}_{timestamp}.png"
        await self.page.screenshot(path=str(path), full_page=False)
        return str(path)

    async def check_captcha(self) -> bool:
        content = (await self.page.content()).lower()
        return any(x in content for x in ["recaptcha", "hcaptcha", "cf-turnstile", "challenge-form"])

    async def extract_reference(self) -> Optional[str]:
        import re
        content = await self.page.content()
        patterns = [
            r"(?:reference|application|confirmation|app)[\s#:]+([A-Z0-9\-]{6,25})",
            r"(?:order|case|ticket)[\s#:]+([A-Z0-9\-]{6,25})",
            r"#([A-Z0-9]{8,20})",
        ]
        for p in patterns:
            m = re.search(p, content, re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    async def page_contains(self, *phrases) -> bool:
        content = (await self.page.content()).lower()
        return any(p.lower() in content for p in phrases)

    def result_ok(self, ref: Optional[str] = None, amount: Optional[float] = None) -> ApplyResult:
        return ApplyResult(success=True, submitted=True, reference_number=ref, approved_amount=amount,
                           status_message="Application submitted successfully")

    def result_manual(self, reason: str) -> ApplyResult:
        return ApplyResult(success=False, submitted=False, needs_manual=True,
                           status_message=f"Manual required: {reason}")

    def result_captcha(self, screenshot: str) -> ApplyResult:
        return ApplyResult(success=False, submitted=False, captcha_detected=True,
                           screenshot_path=screenshot, status_message="CAPTCHA detected — manual completion required")

    def result_error(self, error: str, screenshot: str = None) -> ApplyResult:
        return ApplyResult(success=False, submitted=False, error=error,
                           screenshot_path=screenshot, status_message=f"Error: {error}")
