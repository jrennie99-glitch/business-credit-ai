from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Float, JSON,
    ForeignKey, Enum as SAEnum, Date
)
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime, timezone
import enum


class Base(DeclarativeBase):
    pass


class ApplicationStatus(str, enum.Enum):
    QUEUED = "queued"
    QUALIFYING = "qualifying"
    DISQUALIFIED = "disqualified"
    QUALIFIED = "qualified"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    DENIED = "denied"
    MORE_INFO = "more_info_needed"
    ERROR = "error"
    SKIPPED = "skipped"
    MANUAL_REQUIRED = "manual_required"


class CreditTier(str, enum.Enum):
    FOUNDATION = "foundation"   # 0 tradelines — get EIN, DUNS, business bank
    STARTER = "starter"         # 0–3 tradelines — net-30 vendors, no credit check
    BUILDER = "builder"         # 3–7 tradelines — soft-pull cards, fuel cards
    ESTABLISHED = "established" # 7–12 tradelines — major business credit cards
    ADVANCED = "advanced"       # 12+ tradelines — LOC, term loans
    PREMIUM = "premium"         # 18+ tradelines, 24mo+ history — SBA, large LOC


class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    COLLECTIONS = "collections"
    DELINQUENT = "delinquent"


class PaymentStatus(str, enum.Enum):
    UPCOMING = "upcoming"
    DUE_SOON = "due_soon"     # within 7 days
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"
    PAID = "paid"
    AUTO_PAY = "auto_pay"


class BusinessProfile(Base):
    __tablename__ = "business_profiles"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Legal Info
    legal_name = Column(String(255), nullable=False)
    dba_name = Column(String(255))
    entity_type = Column(String(50))
    state_of_incorporation = Column(String(2))
    date_of_incorporation = Column(String(20))
    ein = Column(String(20))

    # Contact
    business_address = Column(String(255))
    business_city = Column(String(100))
    business_state = Column(String(2))
    business_zip = Column(String(10))
    business_phone = Column(String(20))
    business_email = Column(String(255))
    website = Column(String(255))

    # Owner Info
    owner_first_name = Column(String(100))
    owner_last_name = Column(String(100))
    owner_ssn = Column(String(11))
    owner_dob = Column(String(20))
    owner_address = Column(String(255))
    owner_city = Column(String(100))
    owner_state = Column(String(2))
    owner_zip = Column(String(10))
    owner_phone = Column(String(20))
    owner_email = Column(String(255))
    owner_percentage = Column(Integer, default=100)

    # Financial
    annual_revenue = Column(Float, default=0)
    monthly_revenue = Column(Float, default=0)
    years_in_business = Column(Float, default=0)
    num_employees = Column(Integer, default=1)
    business_checking_account = Column(Boolean, default=False)
    bank_name = Column(String(255))
    average_bank_balance = Column(Float, default=0)

    # Credit
    personal_credit_score = Column(Integer, default=0)
    business_credit_score = Column(Integer, default=0)
    dnb_paydex = Column(Integer, default=0)
    experian_intelliscore = Column(Integer, default=0)
    equifax_business_score = Column(Integer, default=0)
    existing_tradelines = Column(Integer, default=0)
    has_business_credit = Column(Boolean, default=False)

    # IDs
    duns_number = Column(String(20))
    nav_score = Column(Integer)

    # Industry
    industry = Column(String(100))
    naics_code = Column(String(10))
    sic_code = Column(String(10))
    business_description = Column(Text)

    # Current credit tier (auto-calculated)
    current_tier = Column(SAEnum(CreditTier), default=CreditTier.FOUNDATION)

    # Documents
    documents = Column(JSON, default=dict)

    # Relationships
    applications = relationship("Application", back_populates="business")
    active_accounts = relationship("ActiveAccount", back_populates="business")
    payment_schedules = relationship("PaymentSchedule", back_populates="business")
    credit_scores = relationship("CreditScoreHistory", back_populates="business")
    qualification_checks = relationship("QualificationCheck", back_populates="business")


class Lender(Base):
    __tablename__ = "lenders"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    name = Column(String(255), nullable=False)
    category = Column(String(100))
    tier = Column(String(50), default="starter")

    # URLs
    website = Column(String(500))
    application_url = Column(String(500))

    # Products
    product_type = Column(String(100))
    credit_limit_min = Column(Float)
    credit_limit_max = Column(Float)
    interest_rate_min = Column(Float)
    interest_rate_max = Column(Float)
    term_months = Column(Integer)
    payment_terms = Column(String(50))  # "net30", "monthly", "weekly", "daily"

    # Hard Requirements (disqualify if not met)
    req_min_months_in_business = Column(Integer, default=0)
    req_min_annual_revenue = Column(Float, default=0)
    req_min_personal_credit = Column(Integer, default=0)
    req_min_business_credit = Column(Integer, default=0)
    req_min_bank_balance = Column(Float, default=0)
    req_business_checking = Column(Boolean, default=False)
    req_ein = Column(Boolean, default=True)
    req_duns = Column(Boolean, default=False)
    req_no_recent_bankruptcies = Column(Boolean, default=False)
    req_states_excluded = Column(JSON, default=list)  # states where not available

    # Soft Requirements (scoring factors)
    ideal_min_personal_credit = Column(Integer, default=0)
    ideal_min_months = Column(Integer, default=0)
    ideal_min_revenue = Column(Float, default=0)
    ideal_bank_balance = Column(Float, default=0)

    # Pull type
    hard_pull = Column(Boolean, default=False)
    requires_personal_guarantee = Column(Boolean, default=True)
    requires_collateral = Column(Boolean, default=False)

    # Bureau reporting
    reports_to_dnb = Column(Boolean, default=False)
    reports_to_experian_biz = Column(Boolean, default=False)
    reports_to_equifax_biz = Column(Boolean, default=False)
    reports_to_nav = Column(Boolean, default=False)

    # Automation
    automation_script = Column(String(100))  # e.g. "uline", "brex", "generic"
    auto_apply = Column(Boolean, default=True)
    approval_time = Column(String(100))
    application_type = Column(String(50), default="online")

    # Form field mappings
    form_fields = Column(JSON, default=dict)

    description = Column(Text)
    notes = Column(Text)
    is_active = Column(Boolean, default=True)

    # Which credit tier this lender belongs to
    credit_tier_required = Column(SAEnum(CreditTier), default=CreditTier.STARTER)

    applications = relationship("Application", back_populates="lender")


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    submitted_at = Column(DateTime)

    business_id = Column(Integer, ForeignKey("business_profiles.id"))
    lender_id = Column(Integer, ForeignKey("lenders.id"))

    status = Column(SAEnum(ApplicationStatus), default=ApplicationStatus.QUEUED)

    # Qualification
    qualification_result = Column(String(50))  # QUALIFIED / CONDITIONAL / DISQUALIFIED
    qualification_score = Column(Float)         # 0-100
    qualification_reasons = Column(JSON, default=list)
    disqualification_reasons = Column(JSON, default=list)
    requalify_date = Column(DateTime)           # when to re-check if disqualified

    # Results
    approved_amount = Column(Float)
    approved_terms = Column(Text)
    interest_rate = Column(Float)
    credit_limit = Column(Float)
    reference_number = Column(String(255))
    account_number = Column(String(255))

    # Tracking
    ai_notes = Column(Text)
    error_message = Column(Text)
    screenshot_path = Column(String(500))
    response_data = Column(JSON, default=dict)
    retry_count = Column(Integer, default=0)
    next_retry = Column(DateTime)

    # Follow-up
    follow_up_date = Column(DateTime)
    follow_up_notes = Column(Text)

    business = relationship("BusinessProfile", back_populates="applications")
    lender = relationship("Lender", back_populates="applications")


class ActiveAccount(Base):
    """Approved credit accounts — tracks utilization and payments."""
    __tablename__ = "active_accounts"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    business_id = Column(Integer, ForeignKey("business_profiles.id"))
    lender_id = Column(Integer, ForeignKey("lenders.id"))
    application_id = Column(Integer, ForeignKey("applications.id"))

    account_name = Column(String(255))
    account_number = Column(String(255))
    account_type = Column(String(100))  # net30, credit_card, loc, loan
    status = Column(SAEnum(AccountStatus), default=AccountStatus.ACTIVE)

    # Credit
    credit_limit = Column(Float, default=0)
    current_balance = Column(Float, default=0)
    available_credit = Column(Float, default=0)
    utilization_pct = Column(Float, default=0)

    # Payment info
    payment_due_day = Column(Integer)        # day of month (1-31)
    payment_due_date = Column(Date)          # next specific due date
    minimum_payment = Column(Float, default=0)
    auto_pay_enabled = Column(Boolean, default=False)
    payment_terms = Column(String(50))       # net30, monthly, weekly

    # History
    opened_date = Column(Date)
    account_age_months = Column(Integer, default=0)
    on_time_payments = Column(Integer, default=0)
    late_payments = Column(Integer, default=0)
    highest_balance = Column(Float, default=0)

    # Bureau reporting
    last_reported_date = Column(Date)
    reports_to_dnb = Column(Boolean, default=False)
    reports_to_experian_biz = Column(Boolean, default=False)
    reports_to_equifax_biz = Column(Boolean, default=False)

    interest_rate = Column(Float)
    notes = Column(Text)

    business = relationship("BusinessProfile", back_populates="active_accounts")
    payments = relationship("PaymentSchedule", back_populates="account")


class PaymentSchedule(Base):
    """Payment tracking — never miss a payment."""
    __tablename__ = "payment_schedules"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    business_id = Column(Integer, ForeignKey("business_profiles.id"))
    account_id = Column(Integer, ForeignKey("active_accounts.id"))

    due_date = Column(Date, nullable=False)
    amount_due = Column(Float, nullable=False)
    minimum_due = Column(Float)
    payment_status = Column(SAEnum(PaymentStatus), default=PaymentStatus.UPCOMING)

    # Reminders sent
    reminder_7d_sent = Column(Boolean, default=False)
    reminder_3d_sent = Column(Boolean, default=False)
    reminder_1d_sent = Column(Boolean, default=False)
    overdue_alert_sent = Column(Boolean, default=False)

    # Payment record
    paid_date = Column(Date)
    paid_amount = Column(Float)
    payment_method = Column(String(100))
    confirmation = Column(String(255))

    notes = Column(Text)

    business = relationship("BusinessProfile", back_populates="payment_schedules")
    account = relationship("ActiveAccount", back_populates="payments")


class CreditScoreHistory(Base):
    """Historical credit scores — track progress over time."""
    __tablename__ = "credit_score_history"

    id = Column(Integer, primary_key=True)
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    business_id = Column(Integer, ForeignKey("business_profiles.id"))

    # Personal
    personal_credit_score = Column(Integer)
    personal_bureau = Column(String(50))  # Experian, Equifax, TransUnion

    # Business bureaus
    dnb_paydex = Column(Integer)          # 0-100 (80+ = excellent)
    experian_intelliscore = Column(Integer)  # 0-100
    equifax_business_score = Column(Integer) # 0-100
    nav_score = Column(Integer)

    # Tradelines
    num_tradelines = Column(Integer)
    total_credit_available = Column(Float)
    total_credit_used = Column(Float)
    utilization_pct = Column(Float)

    # Derogatory
    derogatory_marks = Column(Integer, default=0)
    bankruptcies = Column(Integer, default=0)
    judgments = Column(Integer, default=0)
    liens = Column(Integer, default=0)

    source = Column(String(100))  # manual, nav_api, dnb_api
    notes = Column(Text)

    business = relationship("BusinessProfile", back_populates="credit_scores")


class QualificationCheck(Base):
    """Log every qualification decision."""
    __tablename__ = "qualification_checks"

    id = Column(Integer, primary_key=True)
    checked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    business_id = Column(Integer, ForeignKey("business_profiles.id"))
    lender_id = Column(Integer, ForeignKey("lenders.id"))

    result = Column(String(50))           # QUALIFIED / CONDITIONAL / DISQUALIFIED
    score = Column(Float)
    hard_fails = Column(JSON, default=list)  # deal-breakers
    soft_fails = Column(JSON, default=list)  # score reducers
    passes = Column(JSON, default=list)
    requalify_in_months = Column(Integer)   # when to re-check
    ai_reasoning = Column(Text)

    business = relationship("BusinessProfile", back_populates="qualification_checks")


class ProgressionEvent(Base):
    """Track when the system moves a business up a credit tier."""
    __tablename__ = "progression_events"

    id = Column(Integer, primary_key=True)
    occurred_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    business_id = Column(Integer, ForeignKey("business_profiles.id"))
    from_tier = Column(SAEnum(CreditTier))
    to_tier = Column(SAEnum(CreditTier))
    trigger_reason = Column(Text)
    new_lenders_unlocked = Column(JSON, default=list)
    ai_analysis = Column(Text)
