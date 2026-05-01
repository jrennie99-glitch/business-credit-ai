"""
Application Agent — orchestrates the end-to-end application process for each lender.
Scores eligibility, fills forms via browser automation, records results.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session

from database.models import BusinessProfile, Lender, Application, ApplicationStatus
from database.db import get_db_context
from agents.orchestrator import CreditOrchestrator
from browser.automation import BrowserAgent
from lenders.catalog import LENDERS
from utils.logger import log
from utils.notifications import send_notification


class ApplicationAgent:
    def __init__(self):
        self.orchestrator = CreditOrchestrator()
        self.browser = BrowserAgent()

    def seed_lenders(self, db: Session):
        """Seed the database with the lender catalog."""
        existing = {l.name for l in db.query(Lender).all()}
        added = 0
        for lender_data in LENDERS:
            if lender_data["name"] not in existing:
                lender = Lender(
                    name=lender_data["name"],
                    category=lender_data["category"],
                    tier=lender_data["tier"],
                    website=lender_data.get("website"),
                    application_url=lender_data.get("application_url"),
                    product_type=lender_data.get("product_type"),
                    credit_limit_min=lender_data.get("credit_limit_min"),
                    credit_limit_max=lender_data.get("credit_limit_max"),
                    interest_rate_min=lender_data.get("interest_rate_min"),
                    interest_rate_max=lender_data.get("interest_rate_max"),
                    term_months=lender_data.get("term_months"),
                    min_time_in_business_months=lender_data.get("min_time_in_business_months", 0),
                    min_annual_revenue=lender_data.get("min_annual_revenue", 0),
                    min_personal_credit_score=lender_data.get("min_personal_credit_score", 0),
                    min_business_credit_score=lender_data.get("min_business_credit_score", 0),
                    requires_personal_guarantee=lender_data.get("requires_personal_guarantee", True),
                    requires_collateral=lender_data.get("requires_collateral", False),
                    reports_to_dnb=lender_data.get("reports_to_dnb", False),
                    reports_to_experian_biz=lender_data.get("reports_to_experian_biz", False),
                    reports_to_equifax_biz=lender_data.get("reports_to_equifax_biz", False),
                    hard_pull=lender_data.get("hard_pull", False),
                    auto_apply=lender_data.get("auto_apply", True),
                    approval_time=lender_data.get("approval_time"),
                    form_fields=lender_data.get("form_fields", {}),
                    description=lender_data.get("description"),
                    notes=lender_data.get("notes"),
                    is_active=True,
                )
                db.add(lender)
                added += 1
        db.commit()
        log.info(f"Seeded {added} new lenders (total catalog: {len(LENDERS)})")
        return added

    def score_all_lenders(self, business: BusinessProfile, db: Session) -> list[dict]:
        """Score all lenders for eligibility and return prioritized list."""
        lenders = db.query(Lender).filter(Lender.is_active == True).all()
        scored = []

        log.info(f"Scoring {len(lenders)} lenders for {business.legal_name}")

        for lender in lenders:
            lender_data = {
                "name": lender.name,
                "category": lender.category,
                "tier": lender.tier,
                "min_time_in_business_months": lender.min_time_in_business_months,
                "min_annual_revenue": lender.min_annual_revenue,
                "min_personal_credit_score": lender.min_personal_credit_score,
                "requires_personal_guarantee": lender.requires_personal_guarantee,
                "hard_pull": lender.hard_pull,
                "approval_time": lender.approval_time,
                "description": lender.description,
                "form_fields": lender.form_fields,
            }

            # Fast rule-based pre-filter before calling AI
            business_months = int(business.years_in_business * 12)
            if lender.min_time_in_business_months > business_months + 3:
                scored.append({
                    "lender": lender,
                    "score": {
                        "approval_probability": 5,
                        "recommended": False,
                        "priority": 1,
                        "recommended_action": f"wait {lender.min_time_in_business_months - business_months} more months",
                    },
                })
                continue

            if (lender.min_personal_credit_score > 0 and
                    business.personal_credit_score > 0 and
                    business.personal_credit_score < lender.min_personal_credit_score - 50):
                scored.append({
                    "lender": lender,
                    "score": {
                        "approval_probability": 10,
                        "recommended": False,
                        "priority": 1,
                        "recommended_action": "improve credit first",
                    },
                })
                continue

            # AI scoring for viable lenders
            try:
                score = self.orchestrator.score_lender_eligibility(business, lender_data)
                scored.append({"lender": lender, "score": score})
            except Exception as e:
                log.warning(f"Could not score {lender.name}: {e}")
                scored.append({
                    "lender": lender,
                    "score": {"approval_probability": 50, "recommended": True, "priority": 5},
                })

        # Sort by priority then probability
        scored.sort(
            key=lambda x: (
                x["score"].get("priority", 5),
                x["score"].get("approval_probability", 0),
            ),
            reverse=True,
        )
        return scored

    async def apply_to_lender(
        self, business: BusinessProfile, lender: Lender, db: Session, dry_run: bool = False
    ) -> Application:
        """Execute a single application."""
        # Check if already applied
        existing = (
            db.query(Application)
            .filter(
                Application.business_id == business.id,
                Application.lender_id == lender.id,
                Application.status.notin_([ApplicationStatus.ERROR]),
            )
            .first()
        )
        if existing:
            log.info(f"Already applied to {lender.name} (status: {existing.status})")
            return existing

        app = Application(
            business_id=business.id,
            lender_id=lender.id,
            status=ApplicationStatus.IN_PROGRESS,
        )
        db.add(app)
        db.commit()
        db.refresh(app)

        if not lender.auto_apply or not lender.application_url:
            app.status = ApplicationStatus.SKIPPED
            app.ai_notes = "Manual application required — see lender notes"
            db.commit()
            return app

        if dry_run:
            app.status = ApplicationStatus.PENDING
            app.ai_notes = "Dry run — not submitted"
            db.commit()
            return app

        try:
            # Generate form data using AI
            lender_data = {
                "name": lender.name,
                "category": lender.category,
                "form_fields": lender.form_fields or {},
            }
            form_data = self.orchestrator.generate_application_data(business, lender_data)

            # Execute browser automation
            result = await self.browser.fill_application(
                url=lender.application_url,
                form_data=form_data,
                lender_name=lender.name,
            )

            app.screenshot_path = result.get("screenshot")
            app.ai_notes = result.get("message")
            app.reference_number = result.get("reference")

            if result.get("submitted"):
                app.status = ApplicationStatus.SUBMITTED
                app.submitted_at = datetime.now(timezone.utc)
                await send_notification(
                    subject=f"Application Submitted: {lender.name}",
                    body=f"Successfully submitted application to {lender.name}.\nReference: {result.get('reference', 'N/A')}\nMessage: {result.get('message')}",
                )
            elif "captcha" in result.get("message", "").lower():
                app.status = ApplicationStatus.ERROR
                app.error_message = result.get("message")
            else:
                app.status = ApplicationStatus.ERROR
                app.error_message = result.get("message")

        except Exception as e:
            log.error(f"Application to {lender.name} failed: {e}")
            app.status = ApplicationStatus.ERROR
            app.error_message = str(e)

        db.commit()
        return app

    async def run_campaign(
        self,
        business: BusinessProfile,
        db: Session,
        max_applications: Optional[int] = None,
        dry_run: bool = False,
        categories: Optional[list[str]] = None,
    ) -> dict:
        """Run a full credit application campaign for a business."""
        log.info(f"Starting credit campaign for {business.legal_name}")

        # Seed lenders if needed
        self.seed_lenders(db)

        # Score all lenders
        scored = self.score_all_lenders(business, db)

        # Filter by category if specified
        if categories:
            scored = [
                s for s in scored
                if s["lender"].category in categories
            ]

        # Filter to recommended only
        recommended = [s for s in scored if s["score"].get("recommended", False)]

        if max_applications:
            recommended = recommended[:max_applications]

        log.info(f"Applying to {len(recommended)} lenders")

        await self.browser.start()
        results = {
            "total": len(recommended),
            "submitted": 0,
            "skipped": 0,
            "errors": 0,
            "applications": [],
        }

        try:
            for item in recommended:
                lender = item["lender"]
                log.info(f"Applying to {lender.name} (probability: {item['score'].get('approval_probability', '?')}%)")

                app = await self.apply_to_lender(business, lender, db, dry_run=dry_run)

                status = app.status
                results["applications"].append({
                    "lender": lender.name,
                    "status": status,
                    "reference": app.reference_number,
                    "notes": app.ai_notes,
                })

                if status == ApplicationStatus.SUBMITTED:
                    results["submitted"] += 1
                elif status == ApplicationStatus.SKIPPED:
                    results["skipped"] += 1
                elif status == ApplicationStatus.ERROR:
                    results["errors"] += 1

                # Pause between applications to avoid rate limiting
                await asyncio.sleep(3)

        finally:
            await self.browser.stop()

        return results
