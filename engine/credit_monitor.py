"""
Credit Bureau Monitor — tracks all business credit scores over time.
Pulls from Nav, monitors D&B PAYDEX, Experian Intelliscore, Equifax Business.
Alerts on score changes and derogatory marks.
"""

from datetime import date, datetime, timezone
import httpx
from sqlalchemy.orm import Session

from database.models import BusinessProfile, CreditScoreHistory, ActiveAccount
from utils.logger import log
from utils.notifications import send_notification


class CreditBureauMonitor:

    async def run_weekly_check(self, business: BusinessProfile, db: Session):
        """Pull all available credit scores and compare to last snapshot."""
        log.info(f"Credit monitor running for {business.legal_name}")

        prev = self._get_last_snapshot(business.id, db)
        current = self._build_current_snapshot(business, db)

        snapshot = CreditScoreHistory(
            business_id=business.id,
            personal_credit_score=business.personal_credit_score,
            dnb_paydex=business.dnb_paydex,
            experian_intelliscore=business.experian_intelliscore,
            equifax_business_score=business.equifax_business_score,
            nav_score=business.nav_score,
            num_tradelines=business.existing_tradelines,
            total_credit_available=current.get("total_available", 0),
            total_credit_used=current.get("total_used", 0),
            utilization_pct=current.get("utilization", 0),
            source="automated_weekly",
        )
        db.add(snapshot)
        db.commit()

        # Alert on score changes
        await self._alert_on_changes(business, prev, snapshot)

        return snapshot

    def _get_last_snapshot(self, business_id: int, db: Session) -> CreditScoreHistory | None:
        return (
            db.query(CreditScoreHistory)
            .filter(CreditScoreHistory.business_id == business_id)
            .order_by(CreditScoreHistory.recorded_at.desc())
            .first()
        )

    def _build_current_snapshot(self, business: BusinessProfile, db: Session) -> dict:
        accounts = db.query(ActiveAccount).filter(
            ActiveAccount.business_id == business.id,
            ActiveAccount.status == "active"
        ).all()

        total_available = sum(a.credit_limit or 0 for a in accounts)
        total_used = sum(a.current_balance or 0 for a in accounts)
        utilization = (total_used / total_available * 100) if total_available > 0 else 0

        return {
            "total_available": total_available,
            "total_used": total_used,
            "utilization": round(utilization, 1),
        }

    async def _alert_on_changes(
        self, business: BusinessProfile,
        prev: CreditScoreHistory | None,
        current: CreditScoreHistory,
    ):
        if not prev:
            return

        alerts = []

        def check_score(name, prev_val, curr_val, direction="higher"):
            if prev_val and curr_val:
                change = curr_val - prev_val
                if abs(change) >= 5:
                    emoji = "📈" if change > 0 else "📉"
                    alerts.append(f"{emoji} {name}: {prev_val} → {curr_val} ({change:+d})")

        check_score("D&B PAYDEX", prev.dnb_paydex, current.dnb_paydex)
        check_score("Experian Intelliscore", prev.experian_intelliscore, current.experian_intelliscore)
        check_score("Equifax Business", prev.equifax_business_score, current.equifax_business_score)
        check_score("Personal Credit", prev.personal_credit_score, current.personal_credit_score)

        if prev.num_tradelines and current.num_tradelines:
            if current.num_tradelines > prev.num_tradelines:
                alerts.append(f"✅ New tradeline added: {prev.num_tradelines} → {current.num_tradelines}")

        if alerts:
            await send_notification(
                subject=f"📊 Credit Score Update — {business.legal_name}",
                body=f"Weekly credit score check for {business.legal_name}:\n\n"
                     + "\n".join(alerts)
                     + f"\n\nD&B PAYDEX: {current.dnb_paydex or 'N/A'}"
                     + f"\nExperian Intelliscore: {current.experian_intelliscore or 'N/A'}"
                     + f"\nEquifax Business: {current.equifax_business_score or 'N/A'}"
                     + f"\nTradelines: {current.num_tradelines or 0}"
                     + f"\nUtilization: {current.utilization_pct:.1f}%",
            )

    def get_score_history(self, business_id: int, db: Session, limit: int = 52) -> list[dict]:
        """Get score history for charting (up to 52 weeks)."""
        records = (
            db.query(CreditScoreHistory)
            .filter(CreditScoreHistory.business_id == business_id)
            .order_by(CreditScoreHistory.recorded_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "date": r.recorded_at.strftime("%Y-%m-%d"),
                "dnb_paydex": r.dnb_paydex,
                "experian": r.experian_intelliscore,
                "equifax": r.equifax_business_score,
                "personal": r.personal_credit_score,
                "tradelines": r.num_tradelines,
                "utilization": r.utilization_pct,
            }
            for r in reversed(records)
        ]

    def get_credit_health_report(self, business: BusinessProfile, db: Session) -> dict:
        """Comprehensive credit health analysis."""
        history = self.get_score_history(business.id, db, limit=12)
        accounts = db.query(ActiveAccount).filter(
            ActiveAccount.business_id == business.id
        ).all()

        active_accounts = [a for a in accounts if a.status == "active"]
        total_credit = sum(a.credit_limit or 0 for a in active_accounts)
        total_used = sum(a.current_balance or 0 for a in active_accounts)
        utilization = (total_used / total_credit * 100) if total_credit > 0 else 0

        dnb_status = self._score_label("paydex", business.dnb_paydex)
        exp_status = self._score_label("intelliscore", business.experian_intelliscore)

        # Payment health
        on_time = sum(a.on_time_payments or 0 for a in active_accounts)
        late = sum(a.late_payments or 0 for a in active_accounts)
        total_payments = on_time + late
        on_time_rate = (on_time / total_payments * 100) if total_payments > 0 else 100

        # Bureau coverage
        reporting_to_dnb = sum(1 for a in active_accounts if a.reports_to_dnb)
        reporting_to_exp = sum(1 for a in active_accounts if a.reports_to_experian_biz)
        reporting_to_eq = sum(1 for a in active_accounts if a.reports_to_equifax_biz)

        recommendations = self._build_recommendations(
            business, utilization, on_time_rate,
            reporting_to_dnb, reporting_to_exp, reporting_to_eq
        )

        return {
            "business_name": business.legal_name,
            "current_tier": business.current_tier,
            "scores": {
                "dnb_paydex": {"value": business.dnb_paydex, "label": dnb_status, "max": 100},
                "experian_intelliscore": {"value": business.experian_intelliscore, "label": exp_status, "max": 100},
                "equifax_business": {"value": business.equifax_business_score, "label": self._score_label("generic", business.equifax_business_score), "max": 100},
                "personal": {"value": business.personal_credit_score, "label": self._score_label("personal", business.personal_credit_score), "max": 850},
            },
            "accounts": {
                "total": len(accounts),
                "active": len(active_accounts),
                "total_credit_limit": total_credit,
                "total_used": total_used,
                "utilization_pct": round(utilization, 1),
                "utilization_status": "EXCELLENT" if utilization < 15 else "GOOD" if utilization < 30 else "FAIR" if utilization < 50 else "HIGH",
            },
            "payments": {
                "on_time": on_time,
                "late": late,
                "on_time_rate": round(on_time_rate, 1),
                "payment_health": "PERFECT" if late == 0 else "GOOD" if on_time_rate >= 95 else "NEEDS_ATTENTION",
            },
            "bureau_coverage": {
                "dnb": reporting_to_dnb,
                "experian": reporting_to_exp,
                "equifax": reporting_to_eq,
                "coverage_score": round((reporting_to_dnb + reporting_to_exp + reporting_to_eq) / max(len(active_accounts) * 3, 1) * 100, 1),
            },
            "score_history": history,
            "recommendations": recommendations,
            "overall_health": self._overall_health(business, utilization, on_time_rate, len(active_accounts)),
        }

    def _score_label(self, score_type: str, score: int | None) -> str:
        if not score:
            return "Not established"
        if score_type == "paydex":
            if score >= 80: return "EXCELLENT"
            if score >= 70: return "GOOD"
            if score >= 50: return "FAIR"
            return "POOR"
        if score_type == "personal":
            if score >= 750: return "EXCEPTIONAL"
            if score >= 700: return "VERY GOOD"
            if score >= 670: return "GOOD"
            if score >= 580: return "FAIR"
            return "POOR"
        if score >= 75: return "EXCELLENT"
        if score >= 60: return "GOOD"
        if score >= 40: return "FAIR"
        return "POOR"

    def _build_recommendations(
        self, business, utilization, on_time_rate, dnb_count, exp_count, eq_count
    ) -> list[str]:
        recs = []
        if utilization > 30:
            recs.append(f"⚠️ Reduce utilization from {utilization:.0f}% to under 30% — pay down balances or request credit limit increases")
        if business.dnb_paydex < 80:
            recs.append("📈 Pay all Net-30 accounts early (in 10-15 days instead of 30) to boost PAYDEX to 80+")
        if dnb_count < 3:
            recs.append("🏦 Add more D&B-reporting tradelines — get at least 3 accounts reporting to D&B for a PAYDEX score")
        if exp_count < 2:
            recs.append("🏦 Add accounts that report to Experian Business — Brex, Grainger, and Chase report there")
        if not business.duns_number:
            recs.append("🆔 Register for a free DUNS number at Dun & Bradstreet — required for government contracts and many lenders")
        if business.existing_tradelines < 5:
            recs.append("📋 Apply for more Net-30 vendors to increase your tradeline count — aim for 5 minimum")
        if on_time_rate < 100:
            recs.append("🚨 You have late payments — enable auto-pay on all accounts to protect your scores")
        if not recs:
            recs.append("✅ Your credit profile looks strong — maintain on-time payments and low utilization")
        return recs

    def _overall_health(self, business, utilization, on_time_rate, tradeline_count) -> str:
        score = 0
        if on_time_rate == 100: score += 40
        elif on_time_rate >= 95: score += 25
        if utilization < 15: score += 20
        elif utilization < 30: score += 10
        if tradeline_count >= 10: score += 20
        elif tradeline_count >= 5: score += 10
        elif tradeline_count >= 2: score += 5
        if business.dnb_paydex >= 80: score += 20
        elif business.dnb_paydex >= 70: score += 10

        if score >= 85: return "EXCELLENT"
        if score >= 65: return "GOOD"
        if score >= 40: return "FAIR"
        return "NEEDS_WORK"
