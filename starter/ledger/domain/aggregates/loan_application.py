"""
ledger/domain/aggregates/loan_application.py

LoanApplication aggregate: replays loan stream to rebuild state.
Enforces state machine and business rules.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from ledger.exceptions import DomainError


class ApplicationState(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    DOCUMENTS_PENDING = "DOCUMENTS_PENDING"
    DOCUMENTS_UPLOADED = "DOCUMENTS_UPLOADED"
    DOCUMENTS_PROCESSED = "DOCUMENTS_PROCESSED"
    CREDIT_ANALYSIS_REQUESTED = "CREDIT_ANALYSIS_REQUESTED"
    CREDIT_ANALYSIS_COMPLETE = "CREDIT_ANALYSIS_COMPLETE"
    FRAUD_SCREENING_REQUESTED = "FRAUD_SCREENING_REQUESTED"
    FRAUD_SCREENING_COMPLETE = "FRAUD_SCREENING_COMPLETE"
    COMPLIANCE_CHECK_REQUESTED = "COMPLIANCE_CHECK_REQUESTED"
    COMPLIANCE_CHECK_COMPLETE = "COMPLIANCE_CHECK_COMPLETE"
    PENDING_DECISION = "PENDING_DECISION"
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    DECLINED_COMPLIANCE = "DECLINED_COMPLIANCE"
    REFERRED = "REFERRED"


VALID_TRANSITIONS = {
    ApplicationState.NEW: [ApplicationState.SUBMITTED],
    ApplicationState.SUBMITTED: [ApplicationState.DOCUMENTS_PENDING],
    ApplicationState.DOCUMENTS_PENDING: [ApplicationState.DOCUMENTS_UPLOADED],
    ApplicationState.DOCUMENTS_UPLOADED: [ApplicationState.DOCUMENTS_PROCESSED],
    ApplicationState.DOCUMENTS_PROCESSED: [ApplicationState.CREDIT_ANALYSIS_REQUESTED],
    ApplicationState.CREDIT_ANALYSIS_REQUESTED: [ApplicationState.CREDIT_ANALYSIS_COMPLETE],
    ApplicationState.CREDIT_ANALYSIS_COMPLETE: [ApplicationState.FRAUD_SCREENING_REQUESTED],
    ApplicationState.FRAUD_SCREENING_REQUESTED: [ApplicationState.FRAUD_SCREENING_COMPLETE],
    ApplicationState.FRAUD_SCREENING_COMPLETE: [ApplicationState.COMPLIANCE_CHECK_REQUESTED],
    ApplicationState.COMPLIANCE_CHECK_REQUESTED: [ApplicationState.COMPLIANCE_CHECK_COMPLETE],
    ApplicationState.COMPLIANCE_CHECK_COMPLETE: [ApplicationState.PENDING_DECISION, ApplicationState.DECLINED_COMPLIANCE],
    ApplicationState.PENDING_DECISION: [ApplicationState.APPROVED, ApplicationState.DECLINED, ApplicationState.PENDING_HUMAN_REVIEW],
    ApplicationState.PENDING_HUMAN_REVIEW: [ApplicationState.APPROVED, ApplicationState.DECLINED],
}


@dataclass
class LoanApplicationAggregate:
    application_id: str
    state: ApplicationState = ApplicationState.NEW
    applicant_id: str | None = None
    requested_amount_usd: float | Decimal | None = None
    approved_amount_usd: float | Decimal | None = None
    loan_purpose: str | None = None
    risk_tier: str | None = None
    compliance_blocked: bool = False
    version: int = -1
    events: list[dict] = field(default_factory=list)

    @classmethod
    async def load(cls, store, application_id: str) -> "LoanApplicationAggregate":
        stream_id = f"loan-{application_id}"
        events = await store.load_stream(stream_id)
        agg = cls(application_id=application_id)
        for event in events:
            agg.apply(event)
        return agg

    def apply(self, event: dict) -> None:
        et = event.get("event_type")
        p = event.get("payload", {})
        self.version = event.get("stream_position", self.version + 1)
        self.events.append(event)

        handler = getattr(self, f"_on_{et}", None)
        if handler:
            handler(p)
        else:
            pass

    def _on_ApplicationSubmitted(self, p: dict) -> None:
        self._transition(ApplicationState.SUBMITTED)
        self.applicant_id = p.get("applicant_id")
        amt = p.get("requested_amount_usd")
        self.requested_amount_usd = float(amt) if amt is not None else None
        self.loan_purpose = str(p.get("loan_purpose", "")) if p.get("loan_purpose") else None

    def _on_DocumentUploadRequested(self, p: dict) -> None:
        self._transition(ApplicationState.DOCUMENTS_PENDING)

    def _on_DocumentUploaded(self, p: dict) -> None:
        self._transition(ApplicationState.DOCUMENTS_UPLOADED)

    def _on_CreditAnalysisRequested(self, p: dict) -> None:
        self._transition(ApplicationState.CREDIT_ANALYSIS_REQUESTED)

    def _on_FraudScreeningRequested(self, p: dict) -> None:
        if self.state == ApplicationState.CREDIT_ANALYSIS_REQUESTED:
            self.state = ApplicationState.CREDIT_ANALYSIS_COMPLETE
        self._transition(ApplicationState.FRAUD_SCREENING_REQUESTED)

    def _on_ComplianceCheckRequested(self, p: dict) -> None:
        if self.state == ApplicationState.FRAUD_SCREENING_REQUESTED:
            self.state = ApplicationState.FRAUD_SCREENING_COMPLETE
        self._transition(ApplicationState.COMPLIANCE_CHECK_REQUESTED)

    def _on_ComplianceRuleFailed(self, p: dict) -> None:
        if p.get("is_hard_block"):
            self.compliance_blocked = True

    def _on_DecisionRequested(self, p: dict) -> None:
        if self.state == ApplicationState.COMPLIANCE_CHECK_REQUESTED:
            self.state = ApplicationState.COMPLIANCE_CHECK_COMPLETE
        self._transition(ApplicationState.PENDING_DECISION)

    def _on_DecisionGenerated(self, p: dict) -> None:
        rec = p.get("recommendation", "")
        confidence = p.get("confidence") or p.get("confidence_score")
        if confidence is not None and float(confidence) < 0.6 and rec != "REFER":
            raise DomainError("confidence < 0.6 requires recommendation=REFER", rule="confidence_floor")
        if self.state == ApplicationState.COMPLIANCE_CHECK_COMPLETE:
            self._transition(ApplicationState.PENDING_DECISION)
        amt = p.get("approved_amount_usd")
        if amt is not None:
            self.approved_amount_usd = float(amt)

    def _on_HumanReviewRequested(self, p: dict) -> None:
        self._transition(ApplicationState.PENDING_HUMAN_REVIEW)

    def _on_HumanReviewCompleted(self, p: dict) -> None:
        pass

    def _on_ApplicationApproved(self, p: dict) -> None:
        self._transition(ApplicationState.APPROVED)
        amt = p.get("approved_amount_usd")
        if amt is not None:
            self.approved_amount_usd = float(amt)

    def _on_ApplicationDeclined(self, p: dict) -> None:
        reasons = p.get("decline_reasons", [])
        codes = p.get("adverse_action_codes", [])
        if "COMPLIANCE_BLOCK" in codes or any("Compliance" in str(r) for r in reasons):
            if self.state == ApplicationState.COMPLIANCE_CHECK_REQUESTED:
                self.state = ApplicationState.COMPLIANCE_CHECK_COMPLETE
            self._transition(ApplicationState.DECLINED_COMPLIANCE)
        else:
            self._transition(ApplicationState.DECLINED)

    def _transition(self, target: ApplicationState) -> None:
        if self.state == ApplicationState.NEW:
            self.state = target
            return
        allowed = VALID_TRANSITIONS.get(self.state, [])
        if target not in allowed:
            raise DomainError(
                f"Invalid transition {self.state} → {target}. Allowed: {allowed}",
                rule="state_machine",
            )
        self.state = target

    def assert_valid_transition(self, target: ApplicationState) -> None:
        allowed = VALID_TRANSITIONS.get(self.state, [])
        if target not in allowed:
            raise DomainError(
                f"Invalid transition {self.state} → {target}. Allowed: {allowed}",
                rule="state_machine",
            )

    def assert_awaiting_credit_analysis(self) -> None:
        if self.state != ApplicationState.CREDIT_ANALYSIS_REQUESTED:
            raise DomainError(
                f"Expected CREDIT_ANALYSIS_REQUESTED, got {self.state}",
                rule="state_machine",
            )

    def assert_awaiting_fraud_screening(self) -> None:
        if self.state != ApplicationState.FRAUD_SCREENING_REQUESTED:
            raise DomainError(
                f"Expected FRAUD_SCREENING_REQUESTED, got {self.state}",
                rule="state_machine",
            )

    def assert_awaiting_compliance(self) -> None:
        if self.state != ApplicationState.COMPLIANCE_CHECK_REQUESTED:
            raise DomainError(
                f"Expected COMPLIANCE_CHECK_REQUESTED, got {self.state}",
                rule="state_machine",
            )

    def assert_awaiting_decision(self) -> None:
        if self.state != ApplicationState.PENDING_DECISION:
            raise DomainError(
                f"Expected PENDING_DECISION, got {self.state}",
                rule="state_machine",
            )

    def assert_pending_human_review(self) -> None:
        if self.state != ApplicationState.PENDING_HUMAN_REVIEW:
            raise DomainError(
                f"Expected PENDING_HUMAN_REVIEW, got {self.state}",
                rule="state_machine",
            )
