"""
Generic lender script — extracts form fields from the DOM as text and uses
the configured LLM (Ollama or Anthropic) to decide what to fill.
No vision/screenshot required — works with any text LLM.
"""

import asyncio
import json
from browser.lender_scripts.base import BaseLenderScript, ApplyResult
from utils.llm import LLMClient
from utils.logger import log


_llm = LLMClient()

_FILL_SYSTEM = """You are a business credit application bot. Given a list of visible form fields
and business data, return exact JSON fill instructions. Use only the selectors provided.

Return ONLY this JSON structure — no other text:
{
  "done": false,
  "captcha": false,
  "submit": true,
  "fields": [
    {"selector": "#company_name", "value": "Acme LLC", "type": "text"}
  ]
}

Rules:
- "done": true if there are no fillable fields (success/confirmation page)
- "captcha": true if fields list mentions a CAPTCHA or reCAPTCHA
- "submit": true when all visible fields are filled and the form is ready to submit
- Only include fields you have data for — skip fields where data is unknown/empty
- type is one of: text, email, tel, select, checkbox, textarea
- For "select" type, set value to the exact option text
- For "checkbox" type, set value to "true" to check it
- Format phone as (XXX) XXX-XXXX, EIN as XX-XXXXXXX, dates as MM/DD/YYYY
- Do not invent data — only use what's in the business profile"""


class GenericScript(BaseLenderScript):
    lender_name = "Generic"

    def __init__(self, page, business_data: dict, application_url: str = None):
        super().__init__(page, business_data)
        self.application_url = application_url

    async def apply(self) -> ApplyResult:
        try:
            if not self.application_url:
                return self.result_error("No application URL configured")

            await self.navigate(self.application_url)
            await asyncio.sleep(2)

            if await self.check_captcha():
                shot = await self.screenshot("captcha")
                return self.result_captcha(shot)

            # Multi-step form handling — up to 6 steps
            for step in range(6):
                fields = await self._extract_form_fields()
                page_text = await self._get_page_text()

                # Check for success page
                if await self.page_contains("thank you", "received", "submitted",
                                            "confirmation", "we'll be in touch",
                                            "application number", "application received"):
                    ref = await self.extract_reference()
                    result = self.result_ok(ref=ref)
                    result.screenshot_path = await self.screenshot("success")
                    return result

                if not fields:
                    # No form fields found — might be success or a navigation page
                    if step > 0:
                        shot = await self.screenshot("no_fields")
                        return ApplyResult(
                            success=True, submitted=True,
                            screenshot_path=shot,
                            status_message="Application process completed — verify status in screenshot",
                        )
                    break

                instructions = await self._llm_fill_instructions(fields, page_text, step)

                if instructions.get("done"):
                    break

                if instructions.get("captcha"):
                    shot = await self.screenshot("captcha")
                    return self.result_captcha(shot)

                # Execute fill instructions
                filled = 0
                for field in instructions.get("fields", []):
                    selector = field.get("selector", "")
                    value = field.get("value", "")
                    ftype = field.get("type", "text")
                    if not selector or value in (None, "", False):
                        continue
                    try:
                        el = self.page.locator(selector).first
                        if await el.count() == 0:
                            # Try fallback selectors
                            continue
                        await el.scroll_into_view_if_needed()
                        if ftype == "select":
                            try:
                                await el.select_option(label=str(value))
                            except Exception:
                                await el.select_option(value=str(value).lower())
                        elif ftype == "checkbox":
                            if str(value).lower() in ("true", "yes", "1"):
                                await el.check()
                        else:
                            await el.fill("")
                            await el.type(str(value), delay=30)
                        filled += 1
                        await asyncio.sleep(0.15)
                    except Exception:
                        continue

                log.info(f"GenericScript step {step+1}: filled {filled} fields")

                if instructions.get("submit") and filled >= 0:
                    submitted = await self.click_first([
                        "button[type='submit']",
                        "input[type='submit']",
                        "button:has-text('Submit Application')",
                        "button:has-text('Submit')",
                        "button:has-text('Apply Now')",
                        "button:has-text('Apply')",
                        "button:has-text('Continue')",
                        "button:has-text('Next')",
                        "button:has-text('Proceed')",
                        "a:has-text('Continue')",
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
            log.error(f"GenericScript error: {e}")
            try:
                shot = await self.screenshot("exception")
                return self.result_error(str(e), shot)
            except Exception:
                return self.result_error(str(e))

    # ── DOM field extraction ───────────────────────────────────────────────────

    async def _extract_form_fields(self) -> list[dict]:
        """Extract all visible form fields from the page without taking a screenshot."""
        try:
            return await self.page.evaluate("""() => {
                const fields = [];
                const els = document.querySelectorAll(
                    'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]),' +
                    'select, textarea'
                );
                for (const el of els) {
                    if (!el.offsetParent) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;

                    // Build label
                    let label = '';
                    if (el.labels && el.labels.length > 0) {
                        label = el.labels[0].innerText.trim();
                    } else if (el.id) {
                        const lbl = document.querySelector('label[for="' + el.id + '"]');
                        if (lbl) label = lbl.innerText.trim();
                    }
                    if (!label && el.placeholder) label = el.placeholder;
                    if (!label && el.name) label = el.name;
                    if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');

                    // Best selector
                    let selector = '';
                    if (el.id) selector = '#' + el.id;
                    else if (el.name) selector = '[name="' + el.name + '"]';
                    else if (el.getAttribute('data-testid')) selector = '[data-testid="' + el.getAttribute('data-testid') + '"]';
                    if (!selector) continue;

                    const field = {
                        selector,
                        label: label.replace(/\\n/g, ' ').substring(0, 80),
                        type: el.tagName === 'SELECT' ? 'select' : (el.type || 'text'),
                        placeholder: el.placeholder || '',
                        required: el.required,
                    };
                    if (el.tagName === 'SELECT') {
                        field.options = Array.from(el.options).slice(0, 20).map(o => o.text.trim()).filter(Boolean);
                    }
                    fields.push(field);
                }
                return fields.slice(0, 30); // cap at 30 fields per step
            }""")
        except Exception as e:
            log.error(f"field extraction error: {e}")
            return []

    async def _get_page_text(self) -> str:
        """Get visible page text for context (headings, labels, etc.)."""
        try:
            text = await self.page.evaluate("""() => {
                const el = document.querySelector('main, form, [role=main], body');
                return (el ? el.innerText : document.body.innerText).substring(0, 800);
            }""")
            return text or ""
        except Exception:
            return ""

    async def _llm_fill_instructions(self, fields: list[dict], page_text: str, step: int) -> dict:
        """Ask the LLM (any provider) which fields to fill and with what values."""
        biz = {
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
            "monthly_revenue": self.data.get("monthly_revenue"),
            "years_in_business": self.data.get("years_in_business"),
            "industry": self.data.get("industry"),
            "naics_code": self.data.get("naics_code"),
            "bank_name": self.data.get("bank_name"),
            "avg_bank_balance": self.data.get("average_bank_balance"),
            "num_employees": self.data.get("num_employees"),
            "ssn_last4": self.data.get("ssn_last4"),
        }

        prompt = f"""Step {step + 1} of a business credit application.

Page context: {page_text[:400]}

Visible form fields (JSON):
{json.dumps(fields, indent=2)}

Business profile (JSON):
{json.dumps(biz, indent=2, default=str)}

Return fill instructions as JSON."""

        try:
            text = _llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system=_FILL_SYSTEM,
                max_tokens=1000,
            )
            start, end = text.find("{"), text.rfind("}") + 1
            if start != -1:
                return json.loads(text[start:end])
        except Exception as e:
            log.error(f"LLM fill error: {e}")

        return {"done": False, "captcha": False, "submit": True, "fields": []}
