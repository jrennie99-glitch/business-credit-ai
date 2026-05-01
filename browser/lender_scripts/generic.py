"""
Generic lender script — Claude Vision analyzes the page and fills it intelligently.
Used as a fallback for any lender without a dedicated script.
"""

import asyncio
import base64
import json
import anthropic
from browser.lender_scripts.base import BaseLenderScript, ApplyResult
from config import settings


class GenericScript(BaseLenderScript):
    lender_name = "Generic"

    def __init__(self, page, business_data: dict, application_url: str = None):
        super().__init__(page, business_data)
        self.application_url = application_url
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    async def apply(self) -> ApplyResult:
        try:
            if not self.application_url:
                return self.result_error("No application URL configured")

            await self.navigate(self.application_url)
            await asyncio.sleep(2)

            if await self.check_captcha():
                shot = await self.screenshot("captcha")
                return self.result_captcha(shot)

            # Multi-step form handling — attempt up to 5 steps
            for step in range(5):
                shot_b64 = await self._get_screenshot_b64()
                instructions = await self._ai_analyze_and_fill(shot_b64, step)

                if instructions.get("done"):
                    break

                if instructions.get("captcha"):
                    shot = await self.screenshot("captcha")
                    return self.result_captcha(shot)

                # Execute fill instructions
                for field in instructions.get("fields", []):
                    selector = field.get("selector", "")
                    value = field.get("value", "")
                    field_type = field.get("type", "text")
                    if not selector or not value:
                        continue
                    try:
                        el = self.page.locator(selector).first
                        if await el.count() == 0:
                            continue
                        await el.scroll_into_view_if_needed()
                        if field_type == "select":
                            await el.select_option(label=str(value))
                        elif field_type == "checkbox":
                            if str(value).lower() in ("true", "yes", "1"):
                                await el.check()
                        else:
                            await el.fill("")
                            await el.type(str(value), delay=35)
                        await asyncio.sleep(0.2)
                    except Exception:
                        continue

                # Submit/continue if instructed
                if instructions.get("submit"):
                    submitted = await self.click_first([
                        "button[type='submit']",
                        "input[type='submit']",
                        "button:has-text('Submit')",
                        "button:has-text('Apply')",
                        "button:has-text('Continue')",
                        "button:has-text('Next')",
                    ])
                    if not submitted:
                        break
                    await asyncio.sleep(3)

            if await self.check_captcha():
                shot = await self.screenshot("captcha_final")
                return self.result_captcha(shot)

            shot = await self.screenshot("final")

            if await self.page_contains("thank you", "received", "submitted", "confirmation",
                                        "review", "we'll be in touch", "application number"):
                ref = await self.extract_reference()
                result = self.result_ok(ref=ref)
                result.screenshot_path = shot
                return result

            return ApplyResult(
                success=True, submitted=True,
                screenshot_path=shot,
                status_message="Application process completed — verify status in screenshot",
            )

        except Exception as e:
            try:
                shot = await self.screenshot("exception")
                return self.result_error(str(e), shot)
            except Exception:
                return self.result_error(str(e))

    async def _get_screenshot_b64(self) -> str:
        screenshot_bytes = await self.page.screenshot(full_page=False)
        return base64.b64encode(screenshot_bytes).decode()

    async def _ai_analyze_and_fill(self, screenshot_b64: str, step: int) -> dict:
        """Use Claude Vision to understand the current form state and return fill instructions."""
        business_context = json.dumps({
            "legal_name": self.data.get("legal_name"),
            "dba_name": self.data.get("dba_name"),
            "entity_type": self.data.get("entity_type"),
            "ein": self.data.get("ein"),
            "address": self.data.get("business_address"),
            "city": self.data.get("business_city"),
            "state": self.data.get("business_state"),
            "zip": self.data.get("business_zip"),
            "phone": self.data.get("business_phone"),
            "email": self.data.get("business_email"),
            "website": self.data.get("website"),
            "owner_first": self.data.get("owner_first_name"),
            "owner_last": self.data.get("owner_last_name"),
            "owner_email": self.data.get("owner_email"),
            "owner_phone": self.data.get("owner_phone"),
            "owner_dob": self.data.get("owner_dob"),
            "annual_revenue": self.data.get("annual_revenue"),
            "years_in_business": self.data.get("years_in_business"),
            "industry": self.data.get("industry"),
            "naics_code": self.data.get("naics_code"),
            "bank_name": self.data.get("bank_name"),
        }, default=str)

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64},
                        },
                        {
                            "type": "text",
                            "text": f"""This is step {step + 1} of a business credit application form.

Business data: {business_context}

Analyze this form and return JSON instructions:
{{
  "done": false,
  "captcha": false,
  "submit": true,
  "fields": [
    {{
      "selector": "input[name='company_name']",
      "value": "Acme LLC",
      "type": "text"
    }}
  ]
}}

Rules:
- "done": true if page shows success/confirmation (no more filling needed)
- "captcha": true if you see a CAPTCHA challenge
- "submit": true if all fields on this step are filled and ready to proceed
- Use exact CSS selectors. Prefer: input[name=X], #id, input[placeholder*=X], select[name=X]
- Types: text, email, tel, select, checkbox, radio, textarea
- Skip fields where you don't have the data
- Format phone as (XXX) XXX-XXXX, EIN as XX-XXXXXXX, dates as MM/DD/YYYY
- Return ONLY the JSON, no other text""",
                        },
                    ],
                }],
            )
            text = response.content[0].text.strip()
            start, end = text.find("{"), text.rfind("}") + 1
            if start != -1:
                return json.loads(text[start:end])
        except Exception:
            pass
        return {"done": False, "captcha": False, "submit": True, "fields": []}
