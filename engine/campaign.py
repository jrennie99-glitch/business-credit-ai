"""
Master Campaign Engine — the brain of the entire system.

Flow:
  1. Assess current credit tier
  2. Get lenders appropriate for that tier
  3. Qualify each lender (hard rules + soft scoring)
  4. Sort by priority (soft pulls first, highest probability first)
  5. Apply to qualified lenders one by one
  6. Record results, create accounts for approvals
  7. Schedule payment due dates
  8. After campaign, re-assess tier for progression
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.orm import Session

from database.models import (
    BusinessProfile, Lender, Application, ActiveAccount,
    ApplicationStatus, PaymentSchedule, PaymentStatus, ProgressionEvent
)
from database.db import get_db_context
from engine.qualification import QualificationEngine
from engine.progression import ProgressionEngine
from engine.payment_monitor import PaymentMonitor
from engine.credit_monitor import CreditBureauMonitor
from browser.automation import BrowserEngine
from agents.orchestrator import CreditOrchestrator
from lenders.catalog import LENDERS
from utils.logger import log
from utils.notifications import send_notification


class CampaignEngine:
    def __init__(self):
        self.qualifier = QualificationEngine()
        self.progression = ProgressionEngine()
        self.payment_monitor = PaymentMonitor()
        self.credit_monitor = CreditBureauMonitor()
        self.browser = BrowserEngine()
        self.orchestrator = CreditOrchestrator()

    # ─── Lender Seeding ───────────────────────────────────────────────────────

    def seed_lenders(self, db: Session) -> int:
        existing = {l.name for l in db.query(Lender).all()}
        added = 0
        for d in LENDERS:
            if d["name"] not in existing:
                lender = Lender(
                    name=d["name"],
                    category=d["category"],
                    tier=d.get("tier", "starter"),
                    credit_tier_required=self._map_tier(d.get("tier", "starter")),
                    website=d.get("website"),
                    application_url=d.get("application_url"),
                    product_type=d.get("product_type"),
                    credit_limit_min=d.get("credit_limit_min"),
                    credit_limit_max=d.get("credit_limit_max"),
                    interest_rate_min=d.get("interest_rate_min"),
                    interest_rate_max=d.get("interest_rate_max"),
                    payment_terms=d.get("payment_terms", "net30" if d.get("category") == "net30" else "monthly"),
                    # Hard requirements
                    req_min_months_in_business=d.get("min_time_in_business_months", 0),
                    req_min_annual_revenue=d.get("min_annual_revenue", 0),
                    req_min_personal_credit=d.get("min_personal_credit_score", 0),
                    req_min_bank_balance=d.get("min_bank_balance", 0),
                    req_business_checking=d.get("requires_business_checking", False),
                    req_ein=d.get("requires_ein", True),
                    # Soft requirements
                    ideal_min_personal_credit=d.get("ideal_credit_score", d.get("min_personal_credit_score", 0)),
                    ideal_min_months=d.get("ideal_months", d.get("min_time_in_business_months", 0)),
                    ideal_min_revenue=d.get("ideal_revenue", d.get("min_annual_revenue", 0)),
                    ideal_bank_balance=d.get("ideal_bank_balance", 0),
                    # Other
                    hard_pull=d.get("hard_pull", False),
                    requires_personal_guarantee=d.get("requires_personal_guarantee", True),
                    reports_to_dnb=d.get("reports_to_dnb", False),
                    reports_to_experian_biz=d.get("reports_to_experian_biz", False),
                    reports_to_equifax_biz=d.get("reports_to_equifax_biz", False),
                    automation_script=d.get("automation_script", d["name"].lower().replace(" ", "_").split("(")[0].strip().replace(" ", "_")),
                    auto_apply=d.get("auto_apply", True),
                    approval_time=d.get("approval_time"),
                    form_fields=d.get("form_fields", {}),
                    description=d.get("description"),
                    notes=d.get("notes"),
                    is_active=True,
                )
                db.add(lender)
                added += 1
        db.commit()
        log.info(f"Seeded {added} lenders")
        return added

    def _map_tier(self, tier_str: str):
        from database.models import CreditTier
        mapping = {
            "starter": CreditTier.STARTER,
            "builder": CreditTier.BUILDER,
            "established": CreditTier.ESTABLISHED,
            "advanced": CreditTier.ADVANCED,
            "premium": CreditTier.PREMIUM,
        }
        return mapping.get(tier_str, CreditTier.STARTER)

    # ─── Qualification Assessment ─────────────────────────────────────────────

    def qualify_all_lenders(self, business: BusinessProfile, db: Session) -> dict:
        """
        Qualify every lender for this business.
        Returns categorized results with reasons.
        """
        lenders = db.query(Lender).filter(Lender.is_active == True).all()

        qualified, conditional, disqualified = self.qualifier.bulk_qualify(business, lenders, db)
        db.commit()

        # Sort: soft pulls first, then by score desc
        def sort_key(item):
            lender = item["lender"]
            score = item["qual"].score
            hard_pull_penalty = -15 if lender.hard_pull else 0
            return score + hard_pull_penalty

        qualified.sort(key=sort_key, reverse=True)
        conditional.sort(key=sort_key, reverse=True)

        return {
            "qualified": qualified,
            "conditional": conditional,
            "disqualified": disqualified,
            "summary": {
                "total": len(lenders),
                "qualified": len(qualified),
                "conditional": len(conditional),
                "disqualified": len(disqualified),
            }
        }

    # ─── Full Campaign ─────────────────────────────────────────────────────────

    async def run_campaign(
        self,
        business: BusinessProfile,
        db: Session,
        max_applications: Optional[int] = None,
        dry_run: bool = False,
        include_conditional: bool = False,
        categories: Optional[list[str]] = None,
        tier_filter: Optional[str] = None,
    ) -> dict:
        """
        Run the complete credit campaign:
        1. Assess tier
        2. Qualify lenders
        3. Apply in optimal order
        4. Record results
        5. Re-assess progression
        """
        log.info(f"Campaign starting for {business.legal_name} (dry_run={dry_run})")

        # Ensure lenders are seeded
        if db.query(Lender).count() == 0:
            self.seed_lenders(db)

        # Assess current tier
        tier_assessment = self.progression.assess(business, db)
        log.info(f"Current tier: {tier_assessment['current_tier']} | Tradelines: {business.existing_tradelines}")

        # Qualify all lenders
        qual_results = self.qualify_all_lenders(business, db)

        # Build application queue
        queue = list(qual_results["qualified"])
        if include_conditional:
            queue += list(qual_results["conditional"])

        # Apply category/tier filters
        if categories:
            queue = [q for q in queue if q["lender"].category in categories]
        if tier_filter:
            queue = [q for q in queue if q["lender"].tier == tier_filter]

        if max_applications:
            queue = queue[:max_applications]

        log.info(f"Application queue: {len(queue)} lenders")

        if not dry_run and len(queue) > 0:
            await self.browser.start()

        results = {
            "business": business.legal_name,
            "tier": str(tier_assessment["current_tier"]),
            "dry_run": dry_run,
            "total_queued": len(queue),
            "submitted": 0,
            "approved": 0,
            "skipped": 0,
            "errors": 0,
            "captcha": 0,
            "applications": [],
            "disqualified_count": len(qual_results["disqualified"]),
            "tier_assessment": tier_assessment,
        }

        try:
            for item in queue:
                lender = item["lender"]
                qual = item["qual"]

                app_result = await self._execute_single_application(
                    business=business,
                    lender=lender,
                    qual=qual,
                    db=db,
                    dry_run=dry_run,
                )

                results["applications"].append({
                    "lender": lender.name,
                    "category": lender.category,
                    "tier": lender.tier,
                    "qual_score": qual.score,
                    "status": app_result.get("status"),
                    "reference": app_result.get("reference"),
                    "message": app_result.get("message"),
                    "screenshot": app_result.get("screenshot"),
                    "hard_pull": lender.hard_pull,
                })

                status = app_result.get("status")
                if status == "submitted":
                    results["submitted"] += 1
                elif status == "approved":
                    results["approved"] += 1
                elif status == "skipped":
                    results["skipped"] += 1
                elif status == "captcha":
                    results["captcha"] += 1
                elif status == "error":
                    results["errors"] += 1

                # Pause between apps — respect rate limits
                if not dry_run:
                    await asyncio.sleep(4)

        finally:
            if not dry_run:
                await self.browser.stop()

        # Re-assess progression after campaign
        new_tier_assessment = self.progression.assess(business, db)
        results["new_tier"] = str(new_tier_assessment.get("advanced_to") or tier_assessment["current_tier"])
        results["newly_unlocked"] = new_tier_assessment.get("newly_unlocked_lenders", [])
        db.commit()

        # Notify
        await send_notification(
            subject=f"Campaign Complete — {business.legal_name}",
            body=self._campaign_summary_email(results),
        )

        return results

    async def _execute_single_application(
        self,
        business: BusinessProfile,
        lender: Lender,
        qual,
        db: Session,
        dry_run: bool,
    ) -> dict:
        """Execute one application, record result, create account if approved."""
        # Check if already applied
        existing = db.query(Application).filter(
            Application.business_id == business.id,
            Application.lender_id == lender.id,
            Application.status.notin_([ApplicationStatus.ERROR, ApplicationStatus.DISQUALIFIED]),
        ).first()
        if existing:
            return {"status": "skipped", "message": f"Already applied (status: {existing.status})"}

        # Create application record
        app = Application(
            business_id=business.id,
            lender_id=lender.id,
            status=ApplicationStatus.QUALIFIED,
            qualification_result=qual.result,
            qualification_score=qual.score,
            qualification_reasons=qual.passes,
            disqualification_reasons=qual.hard_fails,
        )
        db.add(app)
        db.commit()
        db.refresh(app)

        if dry_run:
            app.status = ApplicationStatus.QUEUED
            app.ai_notes = f"Dry run — qualified with {qual.score:.0f}/100 score"
            db.commit()
            return {"status": "skipped", "message": f"Dry run — would apply (score: {qual.score:.0f})"}

        if not lender.auto_apply or not lender.application_url:
            app.status = ApplicationStatus.MANUAL_REQUIRED
            app.ai_notes = lender.notes or "Manual application required"
            db.commit()
            return {"status": "skipped", "message": "Manual application required — see notes"}

        # Build business data dict for scripts
        business_data = {
            "legal_name": business.legal_name,
            "dba_name": business.dba_name,
            "entity_type": business.entity_type,
            "ein": business.ein,
            "business_address": business.business_address,
            "business_city": business.business_city,
            "business_state": business.business_state,
            "business_zip": business.business_zip,
            "business_phone": business.business_phone,
            "business_email": business.business_email,
            "website": business.website,
            "owner_first_name": business.owner_first_name,
            "owner_last_name": business.owner_last_name,
            "owner_email": business.owner_email,
            "owner_phone": business.owner_phone,
            "owner_dob": business.owner_dob,
            "owner_ssn": business.owner_ssn,
            "annual_revenue": business.annual_revenue,
            "monthly_revenue": business.monthly_revenue,
            "years_in_business": business.years_in_business,
            "num_employees": business.num_employees,
            "industry": business.industry,
            "naics_code": business.naics_code,
            "bank_name": business.bank_name,
            "average_bank_balance": business.average_bank_balance,
            "date_of_incorporation": business.date_of_incorporation,
            "state_of_incorporation": business.state_of_incorporation,
        }

        app.status = ApplicationStatus.IN_PROGRESS
        db.commit()

        # Determine script name
        script_name = lender.automation_script or "generic"
        known_scripts = {"uline", "brex", "fundbox", "nav", "generic"}
        if script_name not in known_scripts:
            script_name = "generic"

        # Execute
        try:
            apply_result = await self.browser.execute_application(
                lender_name=lender.name,
                script_name=script_name,
                application_url=lender.application_url,
                business_data=business_data,
            )

            app.screenshot_path = apply_result.screenshot_path
            app.reference_number = apply_result.reference_number
            app.ai_notes = apply_result.status_message
            app.submitted_at = datetime.now(timezone.utc)

            if apply_result.captcha_detected:
                app.status = ApplicationStatus.MANUAL_REQUIRED
                db.commit()
                return {
                    "status": "captcha",
                    "message": "CAPTCHA detected — open screenshot and complete manually",
                    "screenshot": apply_result.screenshot_path,
                    "reference": None,
                }

            if apply_result.submitted or apply_result.success:
                app.status = ApplicationStatus.SUBMITTED

                # Create active account (optimistic — will update when confirmed)
                account = self._create_account_from_lender(business, lender, app, db)

                # Schedule first payment
                if account:
                    self._schedule_next_payment(account, lender, db)

                db.commit()
                return {
                    "status": "submitted",
                    "message": apply_result.status_message,
                    "reference": apply_result.reference_number,
                    "screenshot": apply_result.screenshot_path,
                }
            else:
                app.status = ApplicationStatus.ERROR
                app.error_message = apply_result.error or apply_result.status_message
                db.commit()
                return {
                    "status": "error",
                    "message": apply_result.status_message or apply_result.error,
                    "screenshot": apply_result.screenshot_path,
                }

        except Exception as e:
            log.error(f"Application error for {lender.name}: {e}")
            app.status = ApplicationStatus.ERROR
            app.error_message = str(e)
            db.commit()
            return {"status": "error", "message": str(e)}

    def _create_account_from_lender(
        self, business: BusinessProfile, lender: Lender, app: Application, db: Session
    ) -> ActiveAccount:
        """Create a pending ActiveAccount when an application is submitted."""
        account = ActiveAccount(
            business_id=business.id,
            lender_id=lender.id,
            application_id=app.id,
            account_name=f"{lender.name} — {lender.product_type or lender.category}",
            account_type=lender.category,
            credit_limit=lender.credit_limit_min or 0,  # update when approved
            current_balance=0,
            available_credit=lender.credit_limit_min or 0,
            utilization_pct=0,
            payment_terms=lender.payment_terms or "net30",
            opened_date=date.today(),
            reports_to_dnb=lender.reports_to_dnb,
            reports_to_experian_biz=lender.reports_to_experian_biz,
            reports_to_equifax_biz=lender.reports_to_equifax_biz,
            interest_rate=lender.interest_rate_min,
            status="active",
        )
        db.add(account)
        db.flush()
        return account

    def _schedule_next_payment(
        self, account: ActiveAccount, lender: Lender, db: Session
    ):
        """Schedule the first payment due date."""
        today = date.today()
        terms = lender.payment_terms or "net30"

        if terms == "net30":
            due_date = today + timedelta(days=30)
            amount = account.credit_limit * 0.1 if account.credit_limit else 25
        elif terms == "monthly":
            # Next month, same day
            if today.month == 12:
                due_date = today.replace(year=today.year + 1, month=1)
            else:
                due_date = today.replace(month=today.month + 1)
            amount = account.credit_limit * 0.02 if account.credit_limit else 25
        else:
            due_date = today + timedelta(days=30)
            amount = 25

        payment = PaymentSchedule(
            business_id=account.business_id,
            account_id=account.id,
            due_date=due_date,
            amount_due=max(amount, 25),
            minimum_due=max(amount * 0.1, 10),
            payment_status=PaymentStatus.UPCOMING,
        )
        db.add(payment)

    def _campaign_summary_email(self, results: dict) -> str:
        lines = [
            f"Campaign complete for {results['business']}",
            f"Current tier: {results['tier']}",
            "",
            f"Applications submitted: {results['submitted']}",
            f"Errors: {results['errors']}",
            f"CAPTCHAs (manual needed): {results['captcha']}",
            f"Disqualified (saved you bad pulls): {results['disqualified_count']}",
            "",
        ]
        if results.get("newly_unlocked"):
            lines.append(f"🎉 TIER ADVANCED! New lenders unlocked: {', '.join(results['newly_unlocked'])}")

        for app in results.get("applications", []):
            status_emoji = {"submitted": "✅", "approved": "🎉", "error": "❌", "captcha": "🔒", "skipped": "⏭"}.get(app["status"], "•")
            lines.append(f"{status_emoji} {app['lender']} — {app['status']}")
        return "\n".join(lines)

    # ─── Individual Application ───────────────────────────────────────────────

    async def apply_to_single_lender(
        self,
        business: BusinessProfile,
        lender: Lender,
        db: Session,
        dry_run: bool = False,
    ) -> dict:
        """Apply to one specific lender."""
        qual = self.qualifier.qualify(business, lender, db)
        db.commit()

        if qual.result == "DISQUALIFIED":
            return {
                "status": "disqualified",
                "message": qual.summary,
                "reasons": qual.hard_fails,
                "requalify_months": qual.requalify_months,
            }

        if not dry_run:
            await self.browser.start()
        try:
            result = await self._execute_single_application(business, lender, qual, db, dry_run)
        finally:
            if not dry_run:
                await self.browser.stop()

        return result
