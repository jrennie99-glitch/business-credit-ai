"""Nav — free business credit monitoring + financing marketplace."""

import asyncio
from browser.lender_scripts.base import BaseLenderScript, ApplyResult


class NavScript(BaseLenderScript):
    lender_name = "Nav"

    async def apply(self) -> ApplyResult:
        """
        Nav is not just a lender — it's the credit monitoring hub.
        Signing up gives access to free D&B, Experian, and Equifax business scores
        + a marketplace of financing options.
        """
        try:
            await self.navigate("https://app.nav.com/register")
            await asyncio.sleep(2)

            if await self.check_captcha():
                return self.result_captcha(await self.screenshot("captcha"))

            # Email
            await self.fill_first(
                ["input[name='email']", "input[type='email']", "#email"],
                self.data.get("owner_email") or self.data.get("business_email", "")
            )

            # Password
            await self.fill_first(
                ["input[name='password']", "input[type='password']", "#password"],
                self._make_password()
            )

            await self.click_first([
                "button:has-text('Create Account')",
                "button:has-text('Sign Up')",
                "button:has-text('Get Started')",
                "button[type='submit']",
            ])
            await asyncio.sleep(3)

            # Business name
            await self.fill_first(
                ["input[name='business_name']", "input[placeholder*='business']"],
                self.data.get("legal_name", "")
            )

            # Phone
            await self.fill_first(
                ["input[name='phone']", "input[type='tel']"],
                self.data.get("business_phone", "")
            )

            # EIN
            await self.fill_first(
                ["input[name='ein']", "input[placeholder*='EIN']", "input[placeholder*='Tax']"],
                self.data.get("ein", "")
            )

            await self.click_first([
                "button:has-text('Continue')",
                "button:has-text('Next')",
                "button[type='submit']",
            ])
            await asyncio.sleep(2)

            shot = await self.screenshot("submitted")
            result = self.result_ok()
            result.screenshot_path = shot
            result.status_message = (
                "Nav account created — verify your email, then log in to see your "
                "free D&B PAYDEX, Experian Intelliscore, and financing options"
            )
            return result

        except Exception as e:
            try:
                return self.result_error(str(e), await self.screenshot("exception"))
            except Exception:
                return self.result_error(str(e))

    def _make_password(self) -> str:
        name = self.data.get("legal_name", "Biz").replace(" ", "")[:6]
        return f"Nav_{name}_2024!"
