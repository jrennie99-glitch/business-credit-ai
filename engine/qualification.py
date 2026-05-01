"""
Qualification Engine — the gatekeeper.
Evaluates every lender against your business profile using hard rules + AI scoring.
Never wastes a hard credit pull. Never applies where you won't qualify.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session

from database.models import BusinessProfile, Lender, QualificationCheck, CreditTier
from utils.logger import log


TIER_ORDER = [
    CreditTier.FOUNDATION,
    CreditTier.STARTER,
    CreditTier.BUILDER,
    CreditTier.ESTABLISHED,
    CreditTier.ADVANCED,
    CreditTier.PREMIUM,
]


@dataclass
class QualResult:
    result: str           # QUALIFIED / CONDITIONAL / DISQUALIFIED
    score: float          # 0-100
    hard_fails: list      # absolute disqualifiers
    soft_fails: list      # score reducers
    passes: list          # positive factors
    requalify_months: int # how many months until re-check
    summary: str


class QualificationEngine:
    """
    Two-stage qualification:
      Stage 1: Hard rules — instant DISQUALIFY if any fail
      Stage 2: Soft scoring — calculate approval probability 0-100
    """

    def qualify(self, business: BusinessProfile, lender: Lender, db: Session) -> QualResult:
        hard_fails = []
        soft_fails = []
        passes = []
        score = 100.0
        requalify_months = 999

        business_months = int(business.years_in_business * 12)

        # ── Stage 1: Hard Rules ──────────────────────────────────────────────

        # EIN required
        if lender.req_ein and not business.ein:
            hard_fails.append("EIN required — register at IRS.gov (free, takes 10 min)")
            requalify_months = 0  # can get this immediately

        # DUNS number required
        if lender.req_duns and not business.duns_number:
            hard_fails.append("DUNS number required — register free at Dun & Bradstreet (takes 24-48h)")
            requalify_months = min(requalify_months, 0)

        # Time in business
        if lender.req_min_months_in_business and business_months < lender.req_min_months_in_business:
            months_needed = lender.req_min_months_in_business - business_months
            hard_fails.append(
                f"Needs {lender.req_min_months_in_business}mo in business — you have {business_months}mo "
                f"(qualify in {months_needed} month{'s' if months_needed != 1 else ''})"
            )
            requalify_months = min(requalify_months, months_needed)

        # Annual revenue
        if lender.req_min_annual_revenue and business.annual_revenue < lender.req_min_annual_revenue:
            hard_fails.append(
                f"Needs ${lender.req_min_annual_revenue:,.0f}/yr revenue — "
                f"you have ${business.annual_revenue:,.0f}/yr"
            )
            requalify_months = min(requalify_months, 6)

        # Personal credit (hard floor)
        if (lender.req_min_personal_credit and
                business.personal_credit_score > 0 and
                business.personal_credit_score < lender.req_min_personal_credit - 30):
            hard_fails.append(
                f"Credit score too low — needs {lender.req_min_personal_credit}, "
                f"you have {business.personal_credit_score} "
                f"(need +{lender.req_min_personal_credit - 30 - business.personal_credit_score} points)"
            )
            requalify_months = min(requalify_months, 6)

        # Business checking account
        if lender.req_business_checking and not business.business_checking_account:
            hard_fails.append("Business checking account required — open one at Chase, BofA, or Bluevine")
            requalify_months = min(requalify_months, 0)

        # State exclusions
        if lender.req_states_excluded and business.business_state in lender.req_states_excluded:
            hard_fails.append(f"Not available in {business.business_state}")
            requalify_months = 999  # never

        # Minimum bank balance
        if (lender.req_min_bank_balance and
                business.average_bank_balance > 0 and
                business.average_bank_balance < lender.req_min_bank_balance):
            hard_fails.append(
                f"Bank balance too low — needs ${lender.req_min_bank_balance:,.0f}, "
                f"you have ${business.average_bank_balance:,.0f}"
            )
            requalify_months = min(requalify_months, 3)

        # If any hard fails — DISQUALIFIED
        if hard_fails:
            result = "DISQUALIFIED"
            if requalify_months == 999:
                requalify_months = 0
            self._save_check(db, business, lender, result, 0, hard_fails, soft_fails, passes, requalify_months)
            return QualResult(
                result=result, score=0, hard_fails=hard_fails, soft_fails=soft_fails,
                passes=passes, requalify_months=requalify_months,
                summary=f"Disqualified: {hard_fails[0]}"
            )

        passes.append(f"Time in business: {business_months} months ✓")

        # ── Stage 2: Soft Scoring ────────────────────────────────────────────

        # Personal credit score gradient
        if lender.req_min_personal_credit and business.personal_credit_score > 0:
            gap = business.personal_credit_score - lender.req_min_personal_credit
            if gap < 0:
                deduction = min(40, abs(gap) * 2)
                score -= deduction
                soft_fails.append(f"Credit {business.personal_credit_score} vs {lender.req_min_personal_credit} minimum (−{deduction:.0f}pts)")
            elif gap < 20:
                score -= 10
                soft_fails.append(f"Credit score {business.personal_credit_score} is close to minimum (borderline)")
            elif gap >= 50:
                score += 5
                passes.append(f"Strong credit score: {business.personal_credit_score} ({gap} above minimum)")
            else:
                passes.append(f"Credit score {business.personal_credit_score} meets requirement ✓")

        # Revenue
        if lender.ideal_min_revenue and business.annual_revenue > 0:
            if business.annual_revenue < lender.ideal_min_revenue:
                deduction = 15
                score -= deduction
                soft_fails.append(f"Revenue ${business.annual_revenue:,.0f} below ideal ${lender.ideal_min_revenue:,.0f}")
            else:
                passes.append(f"Revenue ${business.annual_revenue:,.0f} meets ideal ✓")

        # Bank balance (important for fintech lenders like Brex/Ramp)
        if lender.ideal_bank_balance and business.average_bank_balance > 0:
            if business.average_bank_balance < lender.ideal_bank_balance:
                score -= 10
                soft_fails.append(f"Bank balance ${business.average_bank_balance:,.0f} below ideal ${lender.ideal_bank_balance:,.0f}")
            else:
                passes.append(f"Bank balance ${business.average_bank_balance:,.0f} is solid ✓")

        # Business age vs ideal
        if lender.ideal_min_months and business_months < lender.ideal_min_months:
            deduction = min(20, (lender.ideal_min_months - business_months) * 1.5)
            score -= deduction
            soft_fails.append(f"Only {business_months}mo vs {lender.ideal_min_months}mo ideal age (−{deduction:.0f}pts)")
        elif lender.ideal_min_months:
            passes.append(f"Business age {business_months}mo meets ideal ✓")

        # Business credit score (bonus)
        if business.dnb_paydex > 0:
            if business.dnb_paydex >= 80:
                score += 10
                passes.append(f"Excellent D&B PAYDEX: {business.dnb_paydex}")
            elif business.dnb_paydex >= 60:
                score += 5
                passes.append(f"Good D&B PAYDEX: {business.dnb_paydex}")

        # Existing tradelines (positive signal)
        if business.existing_tradelines >= 5:
            score += 10
            passes.append(f"Strong tradeline history: {business.existing_tradelines} accounts")
        elif business.existing_tradelines >= 2:
            score += 5
            passes.append(f"Has existing tradelines: {business.existing_tradelines}")

        # Hard pull penalty (we prefer soft pulls early)
        if lender.hard_pull and business.existing_tradelines < 5:
            score -= 5
            soft_fails.append("Hard credit pull — applying early in credit journey")

        # EIN established
        if business.ein:
            passes.append("EIN established ✓")

        # Website bonus
        if business.website:
            passes.append("Business website established ✓")

        # Clamp
        score = max(0.0, min(100.0, score))

        if score >= 65:
            result = "QUALIFIED"
        elif score >= 40:
            result = "CONDITIONAL"
        else:
            result = "DISQUALIFIED"
            requalify_months = 3

        summary = self._build_summary(result, score, passes, soft_fails)
        self._save_check(db, business, lender, result, score, hard_fails, soft_fails, passes, requalify_months)

        return QualResult(
            result=result, score=score, hard_fails=hard_fails, soft_fails=soft_fails,
            passes=passes, requalify_months=requalify_months if requalify_months < 999 else 0,
            summary=summary,
        )

    def _build_summary(self, result: str, score: float, passes: list, soft_fails: list) -> str:
        if result == "QUALIFIED":
            return f"Qualified ({score:.0f}/100) — {len(passes)} positive factors"
        elif result == "CONDITIONAL":
            return f"Conditional ({score:.0f}/100) — may qualify, some risk factors: {'; '.join(soft_fails[:2])}"
        else:
            return f"Does not qualify yet ({score:.0f}/100)"

    def _save_check(
        self, db: Session, business: BusinessProfile, lender: Lender,
        result: str, score: float, hard_fails: list, soft_fails: list,
        passes: list, requalify_months: int
    ):
        try:
            check = QualificationCheck(
                business_id=business.id,
                lender_id=lender.id,
                result=result,
                score=score,
                hard_fails=hard_fails,
                soft_fails=soft_fails,
                passes=passes,
                requalify_in_months=requalify_months,
            )
            db.add(check)
            db.flush()
        except Exception as e:
            log.warning(f"Could not save qual check: {e}")

    def bulk_qualify(
        self, business: BusinessProfile, lenders: list, db: Session
    ) -> tuple[list, list, list]:
        """Returns (qualified, conditional, disqualified) lists."""
        qualified, conditional, disqualified = [], [], []
        for lender in lenders:
            result = self.qualify(business, lender, db)
            entry = {"lender": lender, "qual": result}
            if result.result == "QUALIFIED":
                qualified.append(entry)
            elif result.result == "CONDITIONAL":
                conditional.append(entry)
            else:
                disqualified.append(entry)
        return qualified, conditional, disqualified
