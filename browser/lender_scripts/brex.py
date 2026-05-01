"""Brex corporate card application — no personal guarantee, no credit check."""

import asyncio
from browser.lender_scripts.base import BaseLenderScript, ApplyResult


class BrexScript(BaseLenderScript):
    lender_name = "Brex"

    async def apply(self) -> ApplyResult:
        try:
            await self.navigate("https://www.brex.com/signup")
            await asyncio.sleep(2)

            if await self.check_captcha():
                shot = await self.screenshot("captcha")
                return self.result_captcha(shot)

            # Step 1: Email
            await self.fill_first(
                ["input[name='email']", "input[type='email']", "input[placeholder*='email']"],
                self.data.get("owner_email") or self.data.get("business_email", "")
            )
            await self.click_first([
                "button[type='submit']",
                "button:has-text('Continue')",
                "button:has-text('Get started')",
            ])
            await asyncio.sleep(2)

            # Step 2: Name
            await self.fill_first(
                ["input[name='first_name']", "input[placeholder*='First']", "#first_name"],
                self.data.get("owner_first_name", "")
            )
            await self.fill_first(
                ["input[name='last_name']", "input[placeholder*='Last']", "#last_name"],
                self.data.get("owner_last_name", "")
            )
            await self.fill_first(
                ["input[name='phone']", "input[type='tel']", "input[placeholder*='phone']"],
                self.data.get("owner_phone", "")
            )

            await self.click_first([
                "button[type='submit']",
                "button:has-text('Continue')",
                "button:has-text('Next')",
            ])
            await asyncio.sleep(2)

            # Step 3: Company info
            await self.fill_first(
                ["input[name='company_name']", "input[placeholder*='company']", "input[placeholder*='business']"],
                self.data.get("legal_name", "")
            )
            await self.fill_first(
                ["input[name='website']", "input[placeholder*='website']", "input[type='url']"],
                self.data.get("website", "")
            )

            # Industry/type dropdowns
            if await self.is_visible("select[name='industry']"):
                await self.select("select[name='industry']", "Technology", by="label")
            if await self.is_visible("select[name='entity_type']"):
                entity = self.data.get("entity_type", "LLC")
                await self.select("select[name='entity_type']", entity, by="label")

            await self.click_first([
                "button[type='submit']",
                "button:has-text('Continue')",
                "button:has-text('Next')",
            ])
            await asyncio.sleep(2)

            shot = await self.screenshot("submitted")

            # Check for success (Brex often shows a review page)
            if await self.page_contains("verify", "check your email", "review", "application"):
                ref = await self.extract_reference()
                result = self.result_ok(ref=ref)
                result.screenshot_path = shot
                result.status_message = "Brex application submitted — check email to verify identity and connect bank account"
                return result

            if await self.check_captcha():
                return self.result_captcha(shot)

            return self.result_ok()

        except Exception as e:
            try:
                shot = await self.screenshot("exception")
                return self.result_error(str(e), shot)
            except Exception:
                return self.result_error(str(e))
