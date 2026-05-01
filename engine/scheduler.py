"""
Scheduler — automated daily and weekly jobs.
Runs without any human interaction once started.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from database.db import SessionLocal
from database.models import BusinessProfile
from engine.payment_monitor import PaymentMonitor
from engine.credit_monitor import CreditBureauMonitor
from engine.progression import ProgressionEngine
from utils.logger import log


scheduler = AsyncIOScheduler(timezone="America/New_York")
payment_monitor = PaymentMonitor()
credit_monitor = CreditBureauMonitor()
progression_engine = ProgressionEngine()


def get_db() -> Session:
    return SessionLocal()


async def job_daily_payment_check():
    """Every morning at 8 AM — check all payment due dates and send reminders."""
    log.info("Scheduler: Daily payment check starting")
    db = get_db()
    try:
        await payment_monitor.run_daily_check(db)
    except Exception as e:
        log.error(f"Payment check error: {e}")
    finally:
        db.close()


async def job_weekly_credit_check():
    """Every Monday at 9 AM — pull credit scores and check progression."""
    log.info("Scheduler: Weekly credit check starting")
    db = get_db()
    try:
        businesses = db.query(BusinessProfile).all()
        for business in businesses:
            try:
                # Credit score snapshot
                await credit_monitor.run_weekly_check(business, db)
                # Check progression
                assessment = progression_engine.assess(business, db)
                if assessment.get("advanced_to"):
                    log.info(f"Progression: {business.legal_name} advanced to {assessment['advanced_to']}")
            except Exception as e:
                log.error(f"Credit check error for {business.legal_name}: {e}")
        db.commit()
    except Exception as e:
        log.error(f"Weekly credit check error: {e}")
    finally:
        db.close()


async def job_monthly_progression_review():
    """First of each month — full progression review and campaign recommendations."""
    log.info("Scheduler: Monthly progression review starting")
    db = get_db()
    try:
        from utils.notifications import send_notification
        businesses = db.query(BusinessProfile).all()
        for business in businesses:
            assessment = progression_engine.assess(business, db)
            credit_report = credit_monitor.get_credit_health_report(business, db)

            body = f"""Monthly Credit Report — {business.legal_name}

CREDIT SCORES:
• D&B PAYDEX: {business.dnb_paydex or 'Not established'}
• Experian Intelliscore: {business.experian_intelliscore or 'Not established'}
• Equifax Business: {business.equifax_business_score or 'Not established'}
• Personal Credit: {business.personal_credit_score or 'Unknown'}

CREDIT ACCOUNTS:
• Active accounts: {credit_report['accounts']['active']}
• Total credit limit: ${credit_report['accounts']['total_credit_limit']:,.0f}
• Utilization: {credit_report['accounts']['utilization_pct']:.1f}%

PAYMENT HEALTH: {credit_report['payments']['payment_health']}
• On-time rate: {credit_report['payments']['on_time_rate']:.1f}%

CURRENT TIER: {assessment['tier_label']}
NEXT MILESTONE: {assessment.get('next_tier_conditions', 'Maximum tier achieved')}

WHAT TO WORK ON:
{chr(10).join('• ' + r for r in credit_report['recommendations'])}

Log in to Business Credit AI to see your full report and apply to new lenders."""

            await send_notification(
                subject=f"📊 Monthly Credit Report — {business.legal_name}",
                body=body,
            )
        db.commit()
    except Exception as e:
        log.error(f"Monthly review error: {e}")
    finally:
        db.close()


def start_scheduler():
    """Start all scheduled jobs."""
    # Daily payment check at 8 AM
    scheduler.add_job(
        job_daily_payment_check,
        CronTrigger(hour=8, minute=0),
        id="daily_payment_check",
        replace_existing=True,
    )

    # Weekly credit check every Monday at 9 AM
    scheduler.add_job(
        job_weekly_credit_check,
        CronTrigger(day_of_week="mon", hour=9, minute=0),
        id="weekly_credit_check",
        replace_existing=True,
    )

    # Monthly report on 1st at 7 AM
    scheduler.add_job(
        job_monthly_progression_review,
        CronTrigger(day=1, hour=7, minute=0),
        id="monthly_progression_review",
        replace_existing=True,
    )

    scheduler.start()
    log.info("Scheduler started — daily payment checks, weekly credit monitoring, monthly reports active")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        log.info("Scheduler stopped")
