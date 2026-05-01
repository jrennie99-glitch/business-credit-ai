"""Fundbox line of credit — as low as 3 months in business."""

import asyncio
from browser.lender_scripts.base import BaseLenderScript, ApplyResult


class FundboxScript(BaseLenderScript):
    lender_name = "Fundbox"

    async def apply(self) -> ApplyResult:
        try:
            await self.navigate("https://fundbox.com/apply/")
            await asyncio.sleep(2)

            if await self.check_captcha():
                return self.result_captcha(await self.screenshot("captcha"))

            # Email first
            await self.fill_first(
                ["input[name='email']", "input[type='email']", "input[placeholder*='email']"],
                self.data.get("owner_email") or self.data.get("business_email", "")
            )

            # Password
            await self.fill_first(
                ["input[name='password']", "input[type='password']"],
                self._generate_temp_password()
            )

            await self.click_first([
                "button:has-text('Sign Up')",
                "button:has-text('Get Started')",
                "button:has-text('Continue')",
                "button[type='submit']",
            ])
            await asyncio.sleep(3)

            # Business name
            await self.fill_first(
                ["input[name='business_name']", "input[placeholder*='business']", "input[placeholder*='company']"],
                self.data.get("legal_name", "")
            )

            # Phone
            await self.fill_first(
                ["input[name='phone']", "input[type='tel']"],
                self.data.get("business_phone", "")
            )

            await self.click_first([
                "button:has-text('Continue')",
                "button[type='submit']",
            ])
            await asyncio.sleep(2)

            shot = await self.screenshot("submitted")

            if await self.page_contains("connect", "accounting", "bank", "plaid", "quickbooks"):
                result = self.result_ok()
                result.screenshot_path = shot
                result.status_message = "Fundbox account created — connect your bank account or accounting software to get approved"
                result.needs_manual = True
                return result

            return self.result_ok()

        except Exception as e:
            try:
                return self.result_error(str(e), await self.screenshot("exception"))
            except Exception:
                return self.result_error(str(e))

    def _generate_temp_password(self) -> str:
        business_name = self.data.get("legal_name", "Business").replace(" ", "")[:8]
        return f"{business_name}@2024!"
