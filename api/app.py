"""FastAPI application — REST API + web dashboard."""

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from utils.auth import require_auth, authenticate_user, create_access_token
from config import settings

from database import init_db, get_db
from database.models import (
    BusinessProfile, Lender, Application, ActiveAccount,
    PaymentSchedule, CreditScoreHistory, ApplicationStatus,
    CreditTier, PaymentStatus
)
from engine.campaign import CampaignEngine
from engine.qualification import QualificationEngine
from engine.progression import ProgressionEngine
from engine.payment_monitor import PaymentMonitor
from engine.credit_monitor import CreditBureauMonitor
from engine.scheduler import start_scheduler, stop_scheduler
from agents.orchestrator import CreditOrchestrator
from utils.logger import log


campaign_engine = CampaignEngine()
qualifier = QualificationEngine()
progression = ProgressionEngine()
payment_monitor = PaymentMonitor()
credit_monitor = CreditBureauMonitor()
orchestrator = CreditOrchestrator()

_active_campaigns: dict[int, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with get_db_context() as db:
        campaign_engine.seed_lenders(db)
    start_scheduler()
    log.info("Business Credit AI started — scheduler active")
    yield
    stop_scheduler()


from database.db import get_db_context

app = FastAPI(
    title="Business Credit AI — God Mode",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Schemas ──────────────────────────────────────────────────────────────────

class BusinessCreate(BaseModel):
    legal_name: str
    dba_name: Optional[str] = None
    entity_type: str = "LLC"
    state_of_incorporation: str = "DE"
    date_of_incorporation: Optional[str] = None
    ein: Optional[str] = None
    business_address: str = ""
    business_city: str = ""
    business_state: str = ""
    business_zip: str = ""
    business_phone: Optional[str] = None
    business_email: Optional[str] = None
    website: Optional[str] = None
    owner_first_name: str = ""
    owner_last_name: str = ""
    owner_ssn: Optional[str] = None
    owner_dob: Optional[str] = None
    owner_address: Optional[str] = None
    owner_city: Optional[str] = None
    owner_state: Optional[str] = None
    owner_zip: Optional[str] = None
    owner_phone: Optional[str] = None
    owner_email: Optional[str] = None
    owner_percentage: int = 100
    annual_revenue: float = 0
    monthly_revenue: float = 0
    years_in_business: float = 0
    num_employees: int = 1
    business_checking_account: bool = False
    bank_name: Optional[str] = None
    average_bank_balance: float = 0
    personal_credit_score: int = 0
    business_credit_score: int = 0
    dnb_paydex: int = 0
    experian_intelliscore: int = 0
    equifax_business_score: int = 0
    existing_tradelines: int = 0
    industry: Optional[str] = None
    naics_code: Optional[str] = None
    business_description: Optional[str] = None
    duns_number: Optional[str] = None


class CampaignRequest(BaseModel):
    business_id: int
    max_applications: Optional[int] = None
    dry_run: bool = True
    include_conditional: bool = False
    categories: Optional[list[str]] = None
    tier_filter: Optional[str] = None


class ChatMessage(BaseModel):
    message: str
    business_id: Optional[int] = None
    history: list[dict] = []


class PaymentUpdate(BaseModel):
    payment_id: int
    amount: float
    method: str = "bank_transfer"
    confirmation: Optional[str] = None


class AccountUpdate(BaseModel):
    credit_limit: Optional[float] = None
    current_balance: Optional[float] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class CreditScoreUpdate(BaseModel):
    business_id: int
    personal_credit_score: Optional[int] = None
    dnb_paydex: Optional[int] = None
    experian_intelliscore: Optional[int] = None
    equifax_business_score: Optional[int] = None
    existing_tradelines: Optional[int] = None
    notes: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


# ─── Root ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent.parent / "static" / "index.html"
    if html_path.exists():
        return FileResponse(str(html_path))
    return HTMLResponse("<h1>Business Credit AI</h1>")


# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def login(data: LoginRequest):
    if not authenticate_user(data.username, data.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": data.username})
    return {"access_token": token, "token_type": "bearer"}


@app.get("/api/auth/me")
def me(user: str = Depends(require_auth)):
    return {"username": user}


# ─── Business Profiles ────────────────────────────────────────────────────────

@app.post("/api/business")
def create_business(data: BusinessCreate, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    business = BusinessProfile(**data.model_dump())
    business.current_tier = CreditTier.FOUNDATION
    db.add(business)
    db.commit()
    db.refresh(business)
    return {"id": business.id, "name": business.legal_name}


@app.get("/api/business")
def list_businesses(db: Session = Depends(get_db), _: str = Depends(require_auth)):
    businesses = db.query(BusinessProfile).all()
    return [_serialize_business_summary(b) for b in businesses]


@app.get("/api/business/{business_id}")
def get_business(business_id: int, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    b = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
    if not b:
        raise HTTPException(404, "Not found")
    data = {c.name: getattr(b, c.name) for c in b.__table__.columns}
    data.pop("owner_ssn", None)
    return data


@app.put("/api/business/{business_id}")
def update_business(business_id: int, data: BusinessCreate, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    b = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
    if not b:
        raise HTTPException(404, "Not found")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(b, k, v)
    b.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"updated": True}


def _serialize_business_summary(b: BusinessProfile) -> dict:
    return {
        "id": b.id,
        "legal_name": b.legal_name,
        "entity_type": b.entity_type,
        "current_tier": str(b.current_tier) if b.current_tier else "foundation",
        "years_in_business": b.years_in_business,
        "annual_revenue": b.annual_revenue,
        "personal_credit_score": b.personal_credit_score,
        "dnb_paydex": b.dnb_paydex,
        "experian_intelliscore": b.experian_intelliscore,
        "existing_tradelines": b.existing_tradelines,
        "has_ein": bool(b.ein),
        "has_duns": bool(b.duns_number),
        "has_checking": b.business_checking_account,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


# ─── Lenders ─────────────────────────────────────────────────────────────────

@app.get("/api/lenders")
def list_lenders(
    category: Optional[str] = None,
    tier: Optional[str] = None,
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    query = db.query(Lender).filter(Lender.is_active == True)
    if category:
        query = query.filter(Lender.category == category)
    if tier:
        query = query.filter(Lender.tier == tier)
    lenders = query.order_by(Lender.tier, Lender.name).all()
    return [_serialize_lender(l) for l in lenders]


@app.get("/api/lenders/qualify/{business_id}")
def qualify_lenders(business_id: int, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    """Qualify all lenders for a business — returns sorted, reasoned results."""
    b = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
    if not b:
        raise HTTPException(404, "Business not found")

    results = campaign_engine.qualify_all_lenders(b, db)

    def serialize_group(group):
        return [{
            "lender": _serialize_lender(item["lender"]),
            "qual_score": round(item["qual"].score, 1),
            "qual_result": item["qual"].result,
            "passes": item["qual"].passes,
            "soft_fails": item["qual"].soft_fails,
            "hard_fails": item["qual"].hard_fails,
            "requalify_months": item["qual"].requalify_months,
            "summary": item["qual"].summary,
        } for item in group]

    return {
        "qualified": serialize_group(results["qualified"]),
        "conditional": serialize_group(results["conditional"]),
        "disqualified": serialize_group(results["disqualified"]),
        "summary": results["summary"],
    }


def _serialize_lender(l: Lender) -> dict:
    return {
        "id": l.id,
        "name": l.name,
        "category": l.category,
        "tier": l.tier,
        "product_type": l.product_type,
        "credit_limit_min": l.credit_limit_min,
        "credit_limit_max": l.credit_limit_max,
        "interest_rate_min": l.interest_rate_min,
        "req_min_personal_credit": l.req_min_personal_credit,
        "req_min_months": l.req_min_months_in_business,
        "req_min_revenue": l.req_min_annual_revenue,
        "hard_pull": l.hard_pull,
        "requires_personal_guarantee": l.requires_personal_guarantee,
        "reports_to_dnb": l.reports_to_dnb,
        "reports_to_experian_biz": l.reports_to_experian_biz,
        "reports_to_equifax_biz": l.reports_to_equifax_biz,
        "approval_time": l.approval_time,
        "auto_apply": l.auto_apply,
        "description": l.description,
        "website": l.website,
        "application_url": l.application_url,
    }


# ─── Campaign ─────────────────────────────────────────────────────────────────

@app.post("/api/campaign/start")
async def start_campaign(req: CampaignRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    b = db.query(BusinessProfile).filter(BusinessProfile.id == req.business_id).first()
    if not b:
        raise HTTPException(404, "Business not found")

    if req.business_id in _active_campaigns and _active_campaigns[req.business_id].get("status") == "running":
        return {"message": "Campaign already running", "task_id": req.business_id}

    _active_campaigns[req.business_id] = {
        "status": "running",
        "business": b.legal_name,
        "started_at": datetime.now().isoformat(),
        "dry_run": req.dry_run,
    }

    background_tasks.add_task(
        _run_campaign_bg,
        req.business_id, req.max_applications, req.dry_run,
        req.include_conditional, req.categories, req.tier_filter,
    )

    return {
        "task_id": req.business_id,
        "status": "running",
        "message": f"Campaign {'(DRY RUN) ' if req.dry_run else ''}started for {b.legal_name}",
    }


async def _run_campaign_bg(business_id, max_apps, dry_run, include_conditional, categories, tier_filter):
    with get_db_context() as db:
        business = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
        try:
            results = await campaign_engine.run_campaign(
                business=business, db=db,
                max_applications=max_apps, dry_run=dry_run,
                include_conditional=include_conditional,
                categories=categories, tier_filter=tier_filter,
            )
            _active_campaigns[business_id] = {
                "status": "complete",
                "results": results,
                "completed_at": datetime.now().isoformat(),
            }
        except Exception as e:
            log.error(f"Campaign error: {e}")
            _active_campaigns[business_id] = {
                "status": "error",
                "error": str(e),
                "completed_at": datetime.now().isoformat(),
            }


@app.get("/api/campaign/status/{task_id}")
def campaign_status(task_id: int, _: str = Depends(require_auth)):
    if task_id not in _active_campaigns:
        raise HTTPException(404, "No campaign found")
    return _active_campaigns[task_id]


@app.post("/api/apply/single")
async def apply_single_lender(
    business_id: int, lender_id: int, dry_run: bool = True,
    db: Session = Depends(get_db), _: str = Depends(require_auth),
):
    """Apply to one specific lender."""
    b = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
    l = db.query(Lender).filter(Lender.id == lender_id).first()
    if not b or not l:
        raise HTTPException(404, "Business or lender not found")

    result = await campaign_engine.apply_to_single_lender(b, l, db, dry_run=dry_run)
    return result


# ─── Applications ─────────────────────────────────────────────────────────────

@app.get("/api/applications")
def list_applications(business_id: Optional[int] = None, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    query = db.query(Application)
    if business_id:
        query = query.filter(Application.business_id == business_id)
    apps = query.order_by(Application.created_at.desc()).all()
    return [_serialize_application(a) for a in apps]


@app.put("/api/applications/{app_id}")
def update_application(app_id: int, status: str, amount: Optional[float] = None, notes: Optional[str] = None, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(404, "Not found")
    try:
        app.status = ApplicationStatus(status)
    except ValueError:
        raise HTTPException(400, f"Invalid status: {status}")
    if amount:
        app.approved_amount = amount
    if notes:
        app.follow_up_notes = notes
    if status == "approved" and amount:
        # Update account credit limit
        account = db.query(ActiveAccount).filter(ActiveAccount.application_id == app_id).first()
        if account:
            account.credit_limit = amount
            account.available_credit = amount - (account.current_balance or 0)
    app.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"updated": True}


def _serialize_application(a: Application) -> dict:
    return {
        "id": a.id,
        "business_id": a.business_id,
        "lender_id": a.lender_id,
        "lender_name": a.lender.name if a.lender else "Unknown",
        "lender_category": a.lender.category if a.lender else "",
        "lender_tier": a.lender.tier if a.lender else "",
        "status": str(a.status) if a.status else "unknown",
        "qualification_result": a.qualification_result,
        "qualification_score": a.qualification_score,
        "qualification_reasons": a.qualification_reasons or [],
        "disqualification_reasons": a.disqualification_reasons or [],
        "approved_amount": a.approved_amount,
        "reference_number": a.reference_number,
        "ai_notes": a.ai_notes,
        "error_message": a.error_message,
        "screenshot_path": a.screenshot_path,
        "requalify_date": a.requalify_date.isoformat() if a.requalify_date else None,
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


# ─── Active Accounts ─────────────────────────────────────────────────────────

@app.get("/api/accounts")
def list_accounts(business_id: Optional[int] = None, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    query = db.query(ActiveAccount)
    if business_id:
        query = query.filter(ActiveAccount.business_id == business_id)
    accounts = query.all()
    return [_serialize_account(a) for a in accounts]


@app.put("/api/accounts/{account_id}")
def update_account(account_id: int, data: AccountUpdate, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    acc = db.query(ActiveAccount).filter(ActiveAccount.id == account_id).first()
    if not acc:
        raise HTTPException(404, "Not found")
    if data.credit_limit is not None:
        acc.credit_limit = data.credit_limit
        acc.available_credit = data.credit_limit - (acc.current_balance or 0)
        if data.credit_limit > 0:
            acc.utilization_pct = ((acc.current_balance or 0) / data.credit_limit) * 100
    if data.current_balance is not None:
        acc.current_balance = data.current_balance
        acc.available_credit = (acc.credit_limit or 0) - data.current_balance
    if data.status:
        acc.status = data.status
    if data.notes:
        acc.notes = data.notes
    acc.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"updated": True}


def _serialize_account(a: ActiveAccount) -> dict:
    return {
        "id": a.id,
        "business_id": a.business_id,
        "account_name": a.account_name,
        "account_type": a.account_type,
        "status": str(a.status) if a.status else "active",
        "credit_limit": a.credit_limit,
        "current_balance": a.current_balance,
        "available_credit": a.available_credit,
        "utilization_pct": a.utilization_pct,
        "payment_terms": a.payment_terms,
        "payment_due_date": a.payment_due_date.isoformat() if a.payment_due_date else None,
        "on_time_payments": a.on_time_payments or 0,
        "late_payments": a.late_payments or 0,
        "reports_to_dnb": a.reports_to_dnb,
        "reports_to_experian_biz": a.reports_to_experian_biz,
        "reports_to_equifax_biz": a.reports_to_equifax_biz,
        "opened_date": a.opened_date.isoformat() if a.opened_date else None,
        "interest_rate": a.interest_rate,
        "notes": a.notes,
    }


# ─── Payments ─────────────────────────────────────────────────────────────────

@app.get("/api/payments")
def list_payments(business_id: Optional[int] = None, status: Optional[str] = None, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    query = db.query(PaymentSchedule)
    if business_id:
        query = query.filter(PaymentSchedule.business_id == business_id)
    if status:
        query = query.filter(PaymentSchedule.payment_status == status)
    payments = query.order_by(PaymentSchedule.due_date).all()
    return [_serialize_payment(p) for p in payments]


@app.get("/api/payments/summary/{business_id}")
def payment_summary(business_id: int, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    return payment_monitor.get_payment_summary(business_id, db)


@app.post("/api/payments/mark-paid")
def mark_payment_paid(data: PaymentUpdate, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    p = payment_monitor.mark_paid(
        data.payment_id, data.amount, data.method,
        data.confirmation or "", db
    )
    return {"paid": True, "payment_id": p.id}


@app.post("/api/payments/check")
async def run_payment_check(db: Session = Depends(get_db), _: str = Depends(require_auth)):
    """Manually trigger the daily payment check."""
    await payment_monitor.run_daily_check(db)
    return {"checked": True}


def _serialize_payment(p: PaymentSchedule) -> dict:
    acc = p.account
    return {
        "id": p.id,
        "business_id": p.business_id,
        "account_id": p.account_id,
        "account_name": acc.account_name if acc else "Unknown",
        "account_type": acc.account_type if acc else "",
        "due_date": p.due_date.isoformat() if p.due_date else None,
        "amount_due": p.amount_due,
        "minimum_due": p.minimum_due,
        "payment_status": str(p.payment_status) if p.payment_status else "upcoming",
        "paid_date": p.paid_date.isoformat() if p.paid_date else None,
        "paid_amount": p.paid_amount,
        "confirmation": p.confirmation,
        "days_until_due": (p.due_date - date.today()).days if p.due_date else None,
        "overdue_days": max(0, (date.today() - p.due_date).days) if p.due_date and p.due_date < date.today() else 0,
    }


# ─── Credit Monitoring ────────────────────────────────────────────────────────

@app.get("/api/credit/report/{business_id}")
def credit_report(business_id: int, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    b = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
    if not b:
        raise HTTPException(404, "Not found")
    return credit_monitor.get_credit_health_report(b, db)


@app.get("/api/credit/history/{business_id}")
def credit_history(business_id: int, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    return credit_monitor.get_score_history(business_id, db)


@app.post("/api/credit/update")
def update_credit_scores(data: CreditScoreUpdate, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    """Manually update credit scores — enter data from Nav, D&B, Experian."""
    b = db.query(BusinessProfile).filter(BusinessProfile.id == data.business_id).first()
    if not b:
        raise HTTPException(404, "Not found")

    if data.personal_credit_score is not None:
        b.personal_credit_score = data.personal_credit_score
    if data.dnb_paydex is not None:
        b.dnb_paydex = data.dnb_paydex
    if data.experian_intelliscore is not None:
        b.experian_intelliscore = data.experian_intelliscore
    if data.equifax_business_score is not None:
        b.equifax_business_score = data.equifax_business_score
    if data.existing_tradelines is not None:
        b.existing_tradelines = data.existing_tradelines

    # Save history snapshot
    snapshot = CreditScoreHistory(
        business_id=b.id,
        personal_credit_score=b.personal_credit_score,
        dnb_paydex=b.dnb_paydex,
        experian_intelliscore=b.experian_intelliscore,
        equifax_business_score=b.equifax_business_score,
        num_tradelines=b.existing_tradelines,
        source="manual",
        notes=data.notes,
    )
    db.add(snapshot)
    db.commit()

    # Re-assess progression after score update
    assessment = progression.assess(b, db)
    db.commit()

    return {
        "updated": True,
        "progression": {
            "current_tier": str(assessment["current_tier"]),
            "advanced_to": str(assessment.get("advanced_to")) if assessment.get("advanced_to") else None,
            "newly_unlocked": assessment.get("newly_unlocked_lenders", []),
        }
    }


# ─── Progression ─────────────────────────────────────────────────────────────

@app.get("/api/progression/{business_id}")
def get_progression(business_id: int, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    b = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
    if not b:
        raise HTTPException(404, "Not found")
    assessment = progression.assess(b, db)
    db.commit()
    return assessment


# ─── AI Chat ─────────────────────────────────────────────────────────────────

@app.post("/api/chat")
def chat(msg: ChatMessage, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    context = ""
    if msg.business_id:
        b = db.query(BusinessProfile).filter(BusinessProfile.id == msg.business_id).first()
        if b:
            accounts = db.query(ActiveAccount).filter(ActiveAccount.business_id == b.id).count()
            context = (
                f"\n\nCurrent business context: {b.legal_name}, {b.entity_type}, "
                f"{b.years_in_business:.1f} years, ${b.annual_revenue:,.0f}/yr revenue, "
                f"personal credit {b.personal_credit_score}, D&B PAYDEX {b.dnb_paydex}, "
                f"{b.existing_tradelines} tradelines, {accounts} active accounts, "
                f"current tier: {b.current_tier}."
            )
    messages = msg.history[-10:] + [{"role": "user", "content": msg.message + context}]
    response = orchestrator.get_chat_response(messages)
    return {"response": response}


@app.get("/api/plan/{business_id}")
def get_plan(business_id: int, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    b = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
    if not b:
        raise HTTPException(404, "Not found")
    return orchestrator.generate_credit_building_plan(b)


# ─── Stats ────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats(business_id: Optional[int] = None, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    app_q = db.query(Application)
    acc_q = db.query(ActiveAccount)
    pay_q = db.query(PaymentSchedule)

    if business_id:
        app_q = app_q.filter(Application.business_id == business_id)
        acc_q = acc_q.filter(ActiveAccount.business_id == business_id)
        pay_q = pay_q.filter(PaymentSchedule.business_id == business_id)

    apps = app_q.all()
    accounts = acc_q.filter(ActiveAccount.status == "active").all()
    payments = pay_q.all()

    total_credit = sum(a.credit_limit or 0 for a in accounts)
    total_used = sum(a.current_balance or 0 for a in accounts)
    overdue = [p for p in payments if str(p.payment_status) in ("overdue", "PaymentStatus.OVERDUE")]
    due_soon = [p for p in payments if str(p.payment_status) in ("due_soon", "PaymentStatus.DUE_SOON")]

    return {
        "total_applications": len(apps),
        "submitted": sum(1 for a in apps if str(a.status) in ("submitted", "ApplicationStatus.SUBMITTED")),
        "approved": sum(1 for a in apps if str(a.status) in ("approved", "ApplicationStatus.APPROVED")),
        "denied": sum(1 for a in apps if str(a.status) in ("denied", "ApplicationStatus.DENIED")),
        "disqualified": sum(1 for a in apps if str(a.status) in ("disqualified", "ApplicationStatus.DISQUALIFIED")),
        "active_accounts": len(accounts),
        "total_credit_available": total_credit,
        "total_credit_used": total_used,
        "overall_utilization": round((total_used / total_credit * 100) if total_credit > 0 else 0, 1),
        "overdue_payments": len(overdue),
        "payments_due_soon": len(due_soon),
        "total_lenders": db.query(Lender).filter(Lender.is_active == True).count(),
        "total_businesses": db.query(BusinessProfile).count(),
    }


# ─── Brain Endpoints ─────────────────────────────────────────────────────────

from agents.brain import CreditBrain

brain = CreditBrain()
_brain_sessions: dict[str, dict] = {}


class BrainMessage(BaseModel):
    message: str
    business_id: int
    session_id: Optional[str] = None


class BrainAuthorize(BaseModel):
    session_id: str
    authorized: bool
    business_id: int


@app.post("/api/brain/think")
async def brain_think(msg: BrainMessage, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    """Send a message to the Credit Brain — it will reason, plan, and act."""
    import uuid
    session_id = msg.session_id or str(uuid.uuid4())

    # Get conversation history for this session
    history = _brain_sessions.get(session_id, {}).get("messages", [])

    result = await brain.think(
        user_message=msg.message,
        business_id=msg.business_id,
        db=db,
        conversation_history=history,
    )

    # Save session state
    _brain_sessions[session_id] = {
        "messages": result["messages"],
        "business_id": msg.business_id,
        "pending_authorization": result.get("authorization_required"),
        "last_updated": datetime.now().isoformat(),
    }

    return {
        "session_id": session_id,
        "response": result["response"],
        "tool_calls": result["tool_calls"],
        "authorization_required": result.get("authorization_required"),
    }


@app.post("/api/brain/authorize")
async def brain_authorize(data: BrainAuthorize, db: Session = Depends(get_db), _: str = Depends(require_auth)):
    """Approve or deny the Brain's pending action plan."""
    session = _brain_sessions.get(data.session_id)
    if not session:
        raise HTTPException(404, "Brain session not found")

    result = await brain.authorize_and_continue(
        authorized=data.authorized,
        conversation_messages=session["messages"],
        business_id=data.business_id,
        db=db,
    )

    # Update session
    _brain_sessions[data.session_id]["messages"] = result["messages"]
    _brain_sessions[data.session_id]["pending_authorization"] = result.get("authorization_required")

    return {
        "session_id": data.session_id,
        "response": result["response"],
        "tool_calls": result["tool_calls"],
        "authorization_required": result.get("authorization_required"),
        "authorized": data.authorized,
    }
