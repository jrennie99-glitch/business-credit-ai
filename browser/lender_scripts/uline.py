"""Uline Net-30 account application automation."""

import asyncio
from browser.lender_scripts.base import BaseLenderScript, ApplyResult


class UlineScript(BaseLenderScript):
    lender_name = "Uline"

    async def apply(self) -> ApplyResult:
        try:
            await self.navigate("https://www.uline.com/BL_8/Open-Account")

            if await self.check_captcha():
                shot = await self.screenshot("captcha")
                return self.result_captcha(shot)

            await asyncio.sleep(2)

            # Step 1: Company info
            await self.fill_first(
                ["input[name='CompanyName']", "input[placeholder*='Company']", "#CompanyName"],
                self.data.get("legal_name", "")
            )
            await self.fill_first(
                ["input[name='Address1']", "input[placeholder*='Address']", "#Address1"],
                self.data.get("business_address", "")
            )
            await self.fill_first(
                ["input[name='City']", "input[placeholder*='City']", "#City"],
                self.data.get("business_city", "")
            )
            await self.fill_first(
                ["input[name='Zip']", "input[placeholder*='Zip']", "#Zip"],
                self.data.get("business_zip", "")
            )
            await self.fill_first(
                ["input[name='Phone']", "input[placeholder*='Phone']", "#Phone"],
                self.data.get("business_phone", "")
            )
            await self.fill_first(
                ["input[name='Email']", "input[placeholder*='Email']", "#Email"],
                self.data.get("business_email", "")
            )

            # Select state
            await self.fill_first(
                ["select[name='State']", "#State"],
                self.data.get("business_state", "")
            )

            # Tax ID / EIN
            await self.fill_first(
                ["input[name='FederalTaxId']", "input[placeholder*='Tax']", "input[placeholder*='EIN']"],
                self.data.get("ein", "")
            )

            # Business type
            if self.data.get("entity_type"):
                await self.fill_first(
                    ["select[name='BusinessType']", "#BusinessType"],
                    self.data.get("entity_type", "")
                )

            shot_pre = await self.screenshot("pre_submit")

            # Submit
            submitted = await self.click_first([
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('Submit')",
                "button:has-text('Open Account')",
                "a:has-text('Submit')",
            ])

            if not submitted:
                return self.result_error("Could not find submit button", shot_pre)

            await asyncio.sleep(3)

            if await self.check_captcha():
                shot = await self.screenshot("captcha_post")
                return self.result_captcha(shot)

            if await self.page_contains("thank you", "application received", "will review", "open account"):
                ref = await self.extract_reference()
                shot = await self.screenshot("success")
                result = self.result_ok(ref=ref)
                result.screenshot_path = shot
                return result

            if await self.page_contains("error", "invalid", "required"):
                shot = await self.screenshot("error")
                return self.result_error("Form validation error — check required fields", shot)

            shot = await self.screenshot("submitted")
            return self.result_ok()

        except Exception as e:
            try:
                shot = await self.screenshot("exception")
                return self.result_error(str(e), shot)
            except Exception:
                return self.result_error(str(e))
