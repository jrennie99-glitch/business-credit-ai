"""
Credit Progression Engine — moves your business up the credit ladder.
Runs weekly to assess when you've earned the right to access better credit products.
"""

from datetime import datetime, timezone
from sqlalchemy.orm import Session

from database.models import (
    BusinessProfile, Lender, Application, ActiveAccount,
    ApplicationStatus, CreditTier, ProgressionEvent
)
from utils.logger import log


TIER_MILESTONES = {
    CreditTier.FOUNDATION: {
        "label": "Foundation",
        "description": "Get your business legally set up with proper credit infrastructure.",
        "checklist": [
            ("EIN registered", lambda b: bool(b.ein)),
            ("Business bank account open", lambda b: b.business_checking_account),
            ("Business address established", lambda b: bool(b.business_address)),
            ("Business phone number", lambda b: bool(b.business_phone)),
            ("Business email", lambda b: bool(b.business_email)),
        ],
        "advance_to": CreditTier.STARTER,
        "advance_conditions": "EIN + business bank account required to advance.",
    },
    CreditTier.STARTER: {
        "label": "Starter (Net-30 Vendors)",
        "description": "Build your first trade lines with no-credit-check Net-30 vendors.",
        "checklist": [
            ("3+ active tradelines", lambda b: b.existing_tradelines >= 3),
            ("D&B PAYDEX 50+", lambda b: b.dnb_paydex >= 50),
            ("3+ months of payment history", lambda b: b.years_in_business * 12 >= 3),
        ],
        "advance_to": CreditTier.BUILDER,
        "advance_conditions": "3 tradelines + D&B PAYDEX 50+ + 3 months history.",
    },
    CreditTier.BUILDER: {
        "label": "Builder (Store/Fuel Cards)",
        "description": "Graduate to soft-pull business cards and fuel cards.",
        "checklist": [
            ("5+ active tradelines", lambda b: b.existing_tradelines >= 5),
            ("D&B PAYDEX 60+", lambda b: b.dnb_paydex >= 60),
            ("6+ months business history", lambda b: b.years_in_business * 12 >= 6),
            ("No late payments", lambda b: True),
        ],
        "advance_to": CreditTier.ESTABLISHED,
        "advance_conditions": "5 tradelines + PAYDEX 60+ + 6 months.",
    },
    CreditTier.ESTABLISHED: {
        "label": "Established (Business Credit Cards)",
        "description": "Access major business credit cards with real spending power.",
        "checklist": [
            ("8+ active tradelines", lambda b: b.existing_tradelines >= 8),
            ("D&B PAYDEX 70+", lambda b: b.dnb_paydex >= 70),
            ("Personal credit 680+", lambda b: b.personal_credit_score >= 680),
            ("12+ months business history", lambda b: b.years_in_business * 12 >= 12),
        ],
        "advance_to": CreditTier.ADVANCED,
        "advance_conditions": "8 tradelines + PAYDEX 70+ + credit 680+ + 12 months.",
    },
    CreditTier.ADVANCED: {
        "label": "Advanced (Lines of Credit & Loans)",
        "description": "Access revolving credit lines and term loans.",
        "checklist": [
            ("12+ active tradelines", lambda b: b.existing_tradelines >= 12),
            ("D&B PAYDEX 75+", lambda b: b.dnb_paydex >= 75),
            ("Experian 65+", lambda b: b.experian_intelliscore >= 65),
            ("Personal credit 680+", lambda b: b.personal_credit_score >= 680),
            ("2+ years in business", lambda b: b.years_in_business >= 2),
            ("$100k+ annual revenue", lambda b: b.annual_revenue >= 100000),
        ],
        "advance_to": CreditTier.PREMIUM,
        "advance_conditions": "12 tradelines + PAYDEX 75+ + 2 years + $100k revenue.",
    },
    CreditTier.PREMIUM: {
        "label": "Premium (SBA & Large Lines)",
        "description": "Access SBA loans and large credit facilities at best rates.",
        "checklist": [
            ("15+ active tradelines", lambda b: b.existing_tradelines >= 15),
            ("D&B PAYDEX 80+", lambda b: b.dnb_paydex >= 80),
            ("Personal credit 700+", lambda b: b.personal_credit_score >= 700),
            ("2+ years in business", lambda b: b.years_in_business >= 2),
            ("$250k+ annual revenue", lambda b: b.annual_revenue >= 250000),
        ],
        "advance_to": None,
        "advance_conditions": "Maximum tier achieved.",
    },
}

TIER_ORDER = [
    CreditTier.FOUNDATION,
    CreditTier.STARTER,
    CreditTier.BUILDER,
    CreditTier.ESTABLISHED,
    CreditTier.ADVANCED,
    CreditTier.PREMIUM,
]


class ProgressionEngine:
    def assess(self, business: BusinessProfile, db: Session) -> dict:
        """
        Full progression assessment for a business.
        Returns current tier status, what's needed to advance, and newly unlocked lenders.
        """
        current_tier = business.current_tier or CreditTier.FOUNDATION

        # Count active accounts to update tradeline count
        active_count = db.query(ActiveAccount).filter(
            ActiveAccount.business_id == business.id,
            ActiveAccount.status == "active"
        ).count()
        if active_count > business.existing_tradelines:
            business.existing_tradelines = active_count
            db.flush()

        milestone = TIER_MILESTONES.get(current_tier, {})
        checklist_results = []
        all_pass = True

        for label, check_fn in milestone.get("checklist", []):
            passed = False
            try:
                passed = check_fn(business)
            except Exception:
                pass
            checklist_results.append({"item": label, "passed": passed})
            if not passed:
                all_pass = False

        # Check if ready to advance
        next_tier = milestone.get("advance_to")
        advanced = False
        newly_unlocked = []

        if all_pass and next_tier:
            advanced = True
            old_tier = current_tier
            business.current_tier = next_tier
            db.flush()

            event = ProgressionEvent(
                business_id=business.id,
                from_tier=old_tier,
                to_tier=next_tier,
                trigger_reason=f"All {current_tier} milestones completed",
            )
            db.add(event)

            # Find newly unlocked lenders
            newly_unlocked = [
                l.name for l in db.query(Lender).filter(
                    Lender.credit_tier_required == next_tier,
                    Lender.is_active == True
                ).all()
            ]
            event.new_lenders_unlocked = newly_unlocked
            log.info(f"PROGRESSION: {business.legal_name} advanced {old_tier} → {next_tier}")

        # What's missing for next advance
        gaps = self._compute_gaps(business, current_tier)

        return {
            "business_id": business.id,
            "current_tier": current_tier,
            "tier_label": milestone.get("label", current_tier),
            "tier_description": milestone.get("description", ""),
            "checklist": checklist_results,
            "all_milestones_met": all_pass,
            "advanced_to": next_tier if advanced else None,
            "newly_unlocked_lenders": newly_unlocked,
            "gaps": gaps,
            "next_tier": next_tier,
            "next_tier_conditions": milestone.get("advance_conditions", ""),
            "credit_summary": self._credit_summary(business),
        }

    def _compute_gaps(self, business: BusinessProfile, tier: CreditTier) -> list[dict]:
        """What does the business still need to advance to the next tier?"""
        milestone = TIER_MILESTONES.get(tier, {})
        gaps = []

        for label, check_fn in milestone.get("checklist", []):
            try:
                if not check_fn(business):
                    gaps.append({"requirement": label, "current": self._current_value(business, label)})
            except Exception:
                pass
        return gaps

    def _current_value(self, business: BusinessProfile, requirement: str) -> str:
        if "tradeline" in requirement.lower():
            return f"You have {business.existing_tradelines}"
        if "paydex" in requirement.lower():
            return f"Your PAYDEX is {business.dnb_paydex}"
        if "experian" in requirement.lower():
            return f"Your Intelliscore is {business.experian_intelliscore}"
        if "credit" in requirement.lower():
            return f"Your score is {business.personal_credit_score}"
        if "revenue" in requirement.lower():
            return f"You have ${business.annual_revenue:,.0f}/yr"
        if "month" in requirement.lower() or "year" in requirement.lower():
            return f"You have {int(business.years_in_business * 12)} months"
        return "Not yet met"

    def _credit_summary(self, business: BusinessProfile) -> dict:
        return {
            "personal_credit": business.personal_credit_score,
            "dnb_paydex": business.dnb_paydex,
            "experian_intelliscore": business.experian_intelliscore,
            "equifax_business_score": business.equifax_business_score,
            "tradelines": business.existing_tradelines,
            "years_in_business": business.years_in_business,
            "annual_revenue": business.annual_revenue,
        }

    def get_recommended_lenders(
        self, business: BusinessProfile, db: Session
    ) -> list[Lender]:
        """
        Return lenders appropriate for the business's current tier.
        Always includes current and previous tiers (don't skip steps).
        """
        current_tier = business.current_tier or CreditTier.FOUNDATION
        tier_idx = TIER_ORDER.index(current_tier) if current_tier in TIER_ORDER else 0

        # Include current and all previous tiers
        eligible_tiers = TIER_ORDER[:tier_idx + 1]

        # Don't re-apply to already submitted lenders
        applied_lender_ids = set(
            row[0] for row in db.query(Application.lender_id).filter(
                Application.business_id == business.id,
                Application.status.notin_([ApplicationStatus.ERROR, ApplicationStatus.DISQUALIFIED])
            ).all()
        )

        lenders = db.query(Lender).filter(
            Lender.credit_tier_required.in_(eligible_tiers),
            Lender.is_active == True,
            Lender.id.notin_(applied_lender_ids),
        ).all()

        return lenders
