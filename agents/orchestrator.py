"""
Master AI Orchestrator — uses Claude to coordinate the entire business credit campaign.
Scores eligibility, prioritizes applications, and directs the automation agents.
"""

import json
import asyncio
from datetime import datetime, timezone
from typing import Any

from utils.llm import LLMClient
from database.models import BusinessProfile, Lender, Application, ApplicationStatus
from database.db import get_db_context
from utils.logger import log


SYSTEM_PROMPT = """You are an elite business credit specialist AI with deep expertise in:
- Business credit building strategies (D&B PAYDEX, Experian Intelliscore, Equifax Business)
- Lender underwriting criteria and approval factors
- Optimal application sequencing to maximize approvals
- Trade reference establishment and net-30 vendor relationships
- Business credit card strategies
- SBA loan qualification

Your mission is to help businesses build the strongest possible credit profile and secure
maximum funding. You analyze business profiles, score lender eligibility, and provide
strategic guidance. Always prioritize the business's long-term credit health.

When analyzing applications, consider:
1. Hard vs soft credit pulls (minimize hard pulls)
2. Optimal application sequencing (start with easy approvals)
3. Bureau reporting (maximize tradeline diversity)
4. Business age, revenue, and credit score thresholds
5. Industry-specific considerations

Respond in JSON format when asked for structured data."""


class CreditOrchestrator:
    def __init__(self):
        self.client = LLMClient()

    def _chat(self, messages: list[dict], system: str = SYSTEM_PROMPT) -> str:
        return self.client.chat(messages=messages, system=system, max_tokens=4096)

    def score_lender_eligibility(
        self, business: BusinessProfile, lender_data: dict
    ) -> dict:
        """Use Claude to score how likely this business is to get approved by this lender."""
        prompt = f"""Analyze this business profile and determine approval probability for this lender.

BUSINESS PROFILE:
- Entity: {business.entity_type} in {business.state_of_incorporation}
- Time in business: {business.years_in_business} years ({int(business.years_in_business * 12)} months)
- Annual revenue: ${business.annual_revenue:,.0f}
- Monthly revenue: ${business.monthly_revenue:,.0f}
- Personal credit score: {business.personal_credit_score}
- Business credit score: {business.business_credit_score}
- Has business checking: {business.business_checking_account}
- Bank name: {business.bank_name}
- Average bank balance: ${business.average_bank_balance:,.0f}
- Existing tradelines: {business.existing_tradelines}
- Industry: {business.industry}
- Has EIN: {'Yes' if business.ein else 'No'}
- DUNS number: {'Yes' if business.duns_number else 'No'}

LENDER: {lender_data['name']}
- Category: {lender_data['category']}
- Tier: {lender_data['tier']}
- Min time in business: {lender_data.get('min_time_in_business_months', 0)} months
- Min annual revenue: ${lender_data.get('min_annual_revenue', 0):,.0f}
- Min personal credit: {lender_data.get('min_personal_credit_score', 0)}
- Requires PG: {lender_data.get('requires_personal_guarantee', True)}
- Hard pull: {lender_data.get('hard_pull', False)}
- Approval time: {lender_data.get('approval_time', 'Unknown')}
- Description: {lender_data.get('description', '')}

Return JSON with:
{{
  "approval_probability": <0-100>,
  "recommended": <true/false>,
  "priority": <1-10, 10=highest>,
  "reasons": ["reason1", "reason2"],
  "concerns": ["concern1"],
  "recommended_action": "apply now|wait X months|skip",
  "estimated_approval_amount": <number or null>
}}"""

        response = self._chat([{"role": "user", "content": prompt}])
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            start = response.find("{")
            end = response.rfind("}") + 1
            data = json.loads(response[start:end]) if start != -1 else {}
        return data

    def generate_credit_building_plan(self, business: BusinessProfile) -> dict:
        """Generate a comprehensive credit building roadmap."""
        prompt = f"""Create a detailed business credit building plan for this business.

BUSINESS PROFILE:
- Name: {business.legal_name}
- Entity: {business.entity_type}, {business.state_of_incorporation}
- Age: {business.years_in_business} years
- Annual revenue: ${business.annual_revenue:,.0f}
- Personal credit: {business.personal_credit_score}
- Business credit: {business.business_credit_score}
- Existing tradelines: {business.existing_tradelines}
- Has EIN: {'Yes' if business.ein else 'No'}
- Has DUNS: {'Yes' if business.duns_number else 'No'}
- Has business bank account: {business.business_checking_account}
- Industry: {business.industry}

Create a strategic 12-month credit building plan. Return JSON:
{{
  "plan_name": "string",
  "summary": "string",
  "current_assessment": {{
    "strengths": ["..."],
    "weaknesses": ["..."],
    "opportunities": ["..."]
  }},
  "immediate_actions": [
    {{"action": "...", "why": "...", "timeline": "This week"}}
  ],
  "phase_1_month_1_3": {{
    "goal": "...",
    "steps": [{{"step": "...", "detail": "..."}}],
    "target_tradelines": 5,
    "expected_credit_score": 40
  }},
  "phase_2_month_4_6": {{
    "goal": "...",
    "steps": [{{"step": "...", "detail": "..."}}],
    "target_tradelines": 10,
    "expected_credit_score": 60
  }},
  "phase_3_month_7_12": {{
    "goal": "...",
    "steps": [{{"step": "...", "detail": "..."}}],
    "target_tradelines": 15,
    "expected_credit_score": 75
  }},
  "estimated_credit_available_12mo": <number>,
  "tips": ["pro tip 1", "pro tip 2"],
  "warnings": ["avoid this mistake"]
}}"""

        response = self._chat([{"role": "user", "content": prompt}])
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"error": "Failed to parse plan", "raw": response}

    def prioritize_lenders(
        self, business: BusinessProfile, scored_lenders: list[dict]
    ) -> list[dict]:
        """Given scored lenders, determine optimal application order."""
        prompt = f"""You are advising a business on the optimal order to apply for credit.

Business has {business.personal_credit_score} personal credit, {business.years_in_business:.1f} years in business,
${business.annual_revenue:,.0f} annual revenue, and {business.existing_tradelines} existing tradelines.

Here are the scored lenders (JSON):
{json.dumps([{
    'name': l['lender']['name'],
    'category': l['lender']['category'],
    'tier': l['lender']['tier'],
    'hard_pull': l['lender'].get('hard_pull', False),
    'probability': l['score'].get('approval_probability', 0),
    'priority': l['score'].get('priority', 5),
    'recommended': l['score'].get('recommended', False),
} for l in scored_lenders[:20]], indent=2)}

Return the optimal application ORDER as JSON array of lender names:
{{
  "order": ["lender1", "lender2", ...],
  "strategy": "brief explanation of the sequencing strategy",
  "skip": ["lenders to skip and why"]
}}

Key rules:
1. Net-30/no-credit-check vendors FIRST (build tradelines without hard pulls)
2. Soft-pull options before hard pulls
3. Group hard pulls that might be at same bureau (minimize impact)
4. Higher probability approvals before lower (momentum builds)
5. Starter tier → Builder → Established → Advanced → Premium"""

        response = self._chat([{"role": "user", "content": prompt}])
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"order": [l["lender"]["name"] for l in scored_lenders], "strategy": "default order"}

    def analyze_denial(self, lender_name: str, error_info: str, business: BusinessProfile) -> str:
        """Analyze why an application was denied and what to do."""
        response = self._chat([{
            "role": "user",
            "content": f"""An application to {lender_name} was denied or had an issue.

Error/Response: {error_info}

Business profile:
- Credit: {business.personal_credit_score} personal, {business.business_credit_score} business
- Age: {business.years_in_business} years
- Revenue: ${business.annual_revenue:,.0f}/yr

What likely caused this, and what specific steps should the business take to improve
their chances of approval? Be concise and actionable."""
        }])
        return response

    def generate_application_data(
        self, business: BusinessProfile, lender: dict
    ) -> dict:
        """Generate the exact form field values for a specific lender application."""
        prompt = f"""Generate the exact form field values to fill out a {lender['name']} application.

BUSINESS DATA:
{{
  "legal_name": "{business.legal_name}",
  "dba_name": "{business.dba_name or ''}",
  "entity_type": "{business.entity_type}",
  "ein": "{business.ein or ''}",
  "address": "{business.business_address}",
  "city": "{business.business_city}",
  "state": "{business.business_state}",
  "zip": "{business.business_zip}",
  "phone": "{business.business_phone}",
  "email": "{business.business_email}",
  "website": "{business.website or ''}",
  "date_incorporated": "{business.date_of_incorporation or ''}",
  "annual_revenue": {business.annual_revenue},
  "monthly_revenue": {business.monthly_revenue},
  "years_in_business": {business.years_in_business},
  "num_employees": {business.num_employees},
  "industry": "{business.industry}",
  "naics_code": "{business.naics_code or ''}",
  "owner_first": "{business.owner_first_name}",
  "owner_last": "{business.owner_last_name}",
  "owner_email": "{business.owner_email}",
  "owner_phone": "{business.owner_phone}",
  "owner_percentage": {business.owner_percentage},
  "bank_name": "{business.bank_name or ''}",
  "bank_balance": {business.average_bank_balance}
}}

LENDER FORM FIELDS: {json.dumps(lender.get('form_fields', {}))}

Return a JSON object mapping each form field to its value. Use exact business data.
Format dates as MM/DD/YYYY. Format phone as (XXX) XXX-XXXX. EIN as XX-XXXXXXX."""

        response = self._chat([{"role": "user", "content": prompt}])
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {}

    def get_chat_response(self, conversation: list[dict]) -> str:
        """General chat interface for the AI assistant."""
        return self._chat(conversation)
