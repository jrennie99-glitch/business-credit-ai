"""
Payment Monitor — never miss a payment, never damage your credit.
Tracks all due dates, sends multi-stage reminders, and updates account history.
"""

from datetime import date, datetime, timedelta, timezone
from sqlalchemy.orm import Session

from database.models import (
    BusinessProfile, ActiveAccount, PaymentSchedule,
    PaymentStatus, AccountStatus
)
from utils.logger import log
from utils.notifications import send_notification


class PaymentMonitor:

    async def run_daily_check(self, db: Session):
        """Run every morning — update statuses and send reminders."""
        today = date.today()
        log.info(f"Payment monitor running — {today}")

        payments = db.query(PaymentSchedule).filter(
            PaymentSchedule.payment_status.notin_([PaymentStatus.PAID])
        ).all()

        for payment in payments:
            await self._process_payment(payment, today, db)

        db.commit()
        log.info(f"Payment check complete — {len(payments)} payments reviewed")

    async def _process_payment(self, payment: PaymentSchedule, today: date, db: Session):
        days_until = (payment.due_date - today).days

        if days_until < 0:
            # OVERDUE
            if payment.payment_status != PaymentStatus.OVERDUE:
                payment.payment_status = PaymentStatus.OVERDUE
                log.warning(f"OVERDUE: payment {payment.id} was due {payment.due_date}")

            if not payment.overdue_alert_sent:
                account = payment.account
                business = db.query(BusinessProfile).get(payment.business_id)
                await send_notification(
                    subject=f"🚨 OVERDUE PAYMENT — {account.account_name if account else 'Account'}",
                    body=self._overdue_body(payment, account, business, abs(days_until)),
                )
                payment.overdue_alert_sent = True

                # Mark account delinquent
                if account:
                    account.status = AccountStatus.DELINQUENT
                    account.late_payments = (account.late_payments or 0) + 1

        elif days_until == 0:
            payment.payment_status = PaymentStatus.DUE_TODAY
            if not payment.reminder_1d_sent:
                account = payment.account
                business = db.query(BusinessProfile).get(payment.business_id)
                await send_notification(
                    subject=f"⚡ PAYMENT DUE TODAY — {account.account_name if account else 'Account'}",
                    body=self._due_today_body(payment, account, business),
                )
                payment.reminder_1d_sent = True

        elif days_until <= 3:
            payment.payment_status = PaymentStatus.DUE_SOON
            if not payment.reminder_3d_sent:
                account = payment.account
                business = db.query(BusinessProfile).get(payment.business_id)
                await send_notification(
                    subject=f"⚠️ Payment Due in {days_until} Days — {account.account_name if account else 'Account'}",
                    body=self._reminder_body(payment, account, business, days_until),
                )
                payment.reminder_3d_sent = True

        elif days_until <= 7:
            payment.payment_status = PaymentStatus.DUE_SOON
            if not payment.reminder_7d_sent:
                account = payment.account
                business = db.query(BusinessProfile).get(payment.business_id)
                await send_notification(
                    subject=f"📅 Payment Due in 7 Days — {account.account_name if account else 'Account'}",
                    body=self._reminder_body(payment, account, business, days_until),
                )
                payment.reminder_7d_sent = True

    def mark_paid(
        self, payment_id: int, amount: float, method: str,
        confirmation: str, db: Session
    ) -> PaymentSchedule:
        payment = db.query(PaymentSchedule).get(payment_id)
        if not payment:
            raise ValueError(f"Payment {payment_id} not found")

        payment.payment_status = PaymentStatus.PAID
        payment.paid_date = date.today()
        payment.paid_amount = amount
        payment.payment_method = method
        payment.confirmation = confirmation

        # Update account history
        account = payment.account
        if account:
            account.current_balance = max(0, (account.current_balance or 0) - amount)
            account.available_credit = (account.credit_limit or 0) - account.current_balance
            account.on_time_payments = (account.on_time_payments or 0) + 1
            if account.status == AccountStatus.DELINQUENT:
                account.status = AccountStatus.ACTIVE

            # Recalculate utilization
            if account.credit_limit and account.credit_limit > 0:
                account.utilization_pct = (account.current_balance / account.credit_limit) * 100

        db.commit()
        log.info(f"Payment {payment_id} marked paid — ${amount:,.2f}")
        return payment

    def create_payment_schedule(
        self,
        account: ActiveAccount,
        due_date: date,
        amount_due: float,
        db: Session,
    ) -> PaymentSchedule:
        payment = PaymentSchedule(
            business_id=account.business_id,
            account_id=account.id,
            due_date=due_date,
            amount_due=amount_due,
            minimum_due=amount_due,
            payment_status=PaymentStatus.UPCOMING,
        )
        db.add(payment)
        db.commit()
        return payment

    def get_upcoming_payments(self, business_id: int, days: int, db: Session) -> list:
        cutoff = date.today() + timedelta(days=days)
        payments = (
            db.query(PaymentSchedule)
            .filter(
                PaymentSchedule.business_id == business_id,
                PaymentSchedule.payment_status.notin_([PaymentStatus.PAID]),
                PaymentSchedule.due_date <= cutoff,
            )
            .order_by(PaymentSchedule.due_date)
            .all()
        )
        return payments

    def get_payment_summary(self, business_id: int, db: Session) -> dict:
        today = date.today()
        all_payments = db.query(PaymentSchedule).filter(
            PaymentSchedule.business_id == business_id
        ).all()

        overdue = [p for p in all_payments if p.payment_status == PaymentStatus.OVERDUE]
        due_soon = [p for p in all_payments if p.payment_status == PaymentStatus.DUE_SOON]
        upcoming = [p for p in all_payments if p.payment_status == PaymentStatus.UPCOMING]
        paid = [p for p in all_payments if p.payment_status == PaymentStatus.PAID]

        total_overdue_amount = sum(p.amount_due for p in overdue)
        total_upcoming_30d = sum(
            p.amount_due for p in upcoming
            if (p.due_date - today).days <= 30
        )

        on_time = len([p for p in paid if p.paid_date and p.due_date and p.paid_date <= p.due_date])
        total_paid = len(paid)
        on_time_rate = (on_time / total_paid * 100) if total_paid > 0 else 100

        return {
            "overdue_count": len(overdue),
            "overdue_amount": total_overdue_amount,
            "due_soon_count": len(due_soon),
            "upcoming_30d_amount": total_upcoming_30d,
            "paid_total": total_paid,
            "on_time_rate": round(on_time_rate, 1),
            "payment_health": "EXCELLENT" if on_time_rate == 100 and len(overdue) == 0 else
                             "GOOD" if on_time_rate >= 95 and len(overdue) == 0 else
                             "AT_RISK" if len(overdue) > 0 else "FAIR",
        }

    def _overdue_body(self, payment, account, business, days_late) -> str:
        name = account.account_name if account else "Unknown Account"
        biz = business.legal_name if business else "Your business"
        return f"""🚨 CRITICAL: OVERDUE PAYMENT — ACTION REQUIRED NOW

Business: {biz}
Account: {name}
Amount Due: ${payment.amount_due:,.2f}
Was Due: {payment.due_date} ({days_late} days ago)

⚠️  Late payments DESTROY business credit scores.
D&B PAYDEX, Experian Intelliscore, and Equifax scores will drop significantly.

ACTION: Pay this IMMEDIATELY to protect your credit profile.
Log in to the lender portal and make the payment now.

Log into Business Credit AI to mark this paid once complete."""

    def _due_today_body(self, payment, account, business) -> str:
        name = account.account_name if account else "Unknown Account"
        biz = business.legal_name if business else "Your business"
        return f"""⚡ PAYMENT DUE TODAY

Business: {biz}
Account: {name}
Amount Due: ${payment.amount_due:,.2f}
Due Date: {payment.due_date} — TODAY

Pay before midnight to maintain your perfect payment record.
Go to the lender portal and submit your payment now."""

    def _reminder_body(self, payment, account, business, days) -> str:
        name = account.account_name if account else "Unknown Account"
        biz = business.legal_name if business else "Your business"
        return f"""📅 Upcoming Payment Reminder

Business: {biz}
Account: {name}
Amount Due: ${payment.amount_due:,.2f}
Due Date: {payment.due_date} (in {days} day{'s' if days != 1 else ''})

Set up this payment in advance to ensure on-time delivery.
On-time payments are the #1 factor in your D&B PAYDEX score."""
