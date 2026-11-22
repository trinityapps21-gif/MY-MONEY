"""PDF receipts for every activity that moves money or shares, plus the helpers for sharing them.

Kinds: contribution, share (a share purchase), share_transfer, repayment (a loan instalment payment),
disbursement, withdrawal, fine, transaction (any other ledger entry).

Each receipt can be downloaded as a PDF or shared on WhatsApp: as the PDF itself where the phone allows it, or as a
link to the PDF. A link carries a signed, expiring token, so only people who were sent it can open it, and it always
shows the CURRENT state of the record (a reversed payment is marked REVERSED, never silently still valid)."""
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from flask import current_app, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import db
from app.models import (Contribution, Loan, LoanFine, LoanRepayment, SavingsWithdrawal, ShareEntry, ShareTransfer,
                        Transaction)
from app.services import documents, ledger
from app.services.timeutil import to_local

KINDS = ("contribution", "share", "share_transfer", "repayment", "disbursement", "withdrawal", "fine", "transaction")
OFFICIAL_ONLY = {"repayment", "disbursement", "fine", "transaction"}   # borrower information never goes to ordinary members


class ReceiptError(Exception):
    """The receipt does not exist (or is not available) for this group."""


@dataclass
class Receipt:
    kind: str
    obj_id: int
    title: str
    number: str
    amount: int
    fields: list
    date: object
    note: str | None = None
    party: str = ""
    phone: str | None = None
    member_ids: set = field(default_factory=set)      # members who may open it besides officials

    @property
    def official_only(self):
        return self.kind in OFFICIAL_ONLY

    def allowed(self, is_official, membership_id):
        if is_official:
            return True
        return not self.official_only and membership_id in self.member_ids



def _repayment_breakdown(group, r, m):
    """Rows and note for a loan repayment receipt: principal / interest / late fine, and the loan's own fine terms.

    The late fine is a separate charge raised because this payment was late (it is not taken out of the amount paid).
    The terms shown are the ones frozen on the loan, so a later change to the group's rules never alters an old receipt."""
    from app.models import LoanRepaymentSchedule as Sched
    from app.services import fine_terms
    from app.services.groupcfg import get_settings
    loan, cc, dd = r.loan, group.currency, group.currency_decimals
    nums = sorted({sc.number for sc in (db.session.get(Sched, a.schedule_id) for a in r.allocations) if sc})
    fines = [f for f in LoanFine.query.filter_by(group_id=group.id, repayment_id=r.id) if f.status != "VOID"]
    fine_total = sum(f.amount for f in fines)
    shown = {"UNPAID": "not yet paid", "PAID": "paid", "WAIVED": "waived"}
    fields = [("Received from", r.borrower.name), ("Loan", loan.reference), ("Category", "Loan instalment payment"),
              ("Instalment(s) paid", ", ".join(f"#{n}" for n in nums) or None),
              ("Amount paid", m(r.amount)),
              ("Principal portion", m(r.principal_part)),
              ("Interest portion", m(r.interest_part))]
    if fines:
        f = fines[0]
        fields += [("Late fine charged", f"{m(fine_total)} ({shown.get(f.status, f.status.lower())})"),
                   ("Days late", str(max(x.days_late for x in fines))),
                   ("Fine charged on", m(sum(x.late_amount for x in fines))),
                   ("Total with late fine", m(r.amount + fine_total))]
    else:
        fields.append(("Late fine charged", "None - paid on time" if not fine_terms.is_free(loan) else "None (no late fine on this loan)"))
    fields += [("Late-fine terms on this loan", fine_terms.describe(loan, cc, dd)),
               ("Date", f"{r.payment_date:%d %b %Y}"), ("Payment method", r.payment_method),
               ("Payment reference", r.payment_reference), ("Recorded by", r.recorder.full_name)]
    note = None
    if not fine_terms.same(loan, get_settings(group.id)):
        note = "The group has since changed its late-fine terms. This loan keeps the terms it was created with."
    return fields, note

def _money(group, v):
    return documents.fmt(v, group.currency, group.currency_decimals)


def _user_name(user_id):
    from app.models import User
    u = db.session.get(User, user_id) if user_id else None
    return u.full_name if u else "-"


def _tx_label(tx_type):
    from app.services.reports import TX_LABELS
    return TX_LABELS.get(tx_type, tx_type)


def _scoped(model, obj_id, group):
    row = db.session.get(model, obj_id)
    if row is None or getattr(row, "group_id", group.id) != group.id:
        raise ReceiptError("not found")
    return row


def describe(group, kind, obj_id) -> Receipt:
    """Work out the content of a receipt. Does no permission check: callers use Receipt.allowed()."""
    if kind not in KINDS:
        raise ReceiptError("unknown kind")
    m = lambda v: _money(group, v)  # noqa: E731
    if kind == "contribution":
        c = _scoped(Contribution, obj_id, group)
        fields = [("Received from", c.member.user.full_name), ("Category", _tx_label(c.tx.tx_type)),
                  ("Contribution type", c.category.title()), ("Date", f"{c.contribution_date:%d %b %Y}"),
                  ("Payment method", c.payment_method), ("Payment reference", c.payment_reference),
                  ("Transaction reference", c.tx.reference), ("Recorded by", c.recorder.full_name)]
        return Receipt(kind, c.id, "Contribution Receipt", c.receipt_no, c.amount, fields, c.contribution_date,
                       "This contribution has been REVERSED." if ledger.is_reversed(c.tx_id) else None,
                       c.member.user.full_name, c.member.user.phone, {c.member_id})
    if kind == "share":
        e = _scoped(ShareEntry, obj_id, group)
        if e.kind != "PURCHASE":
            raise ReceiptError("not a purchase")
        from app.services import shares as share_svc
        tx = db.session.get(Transaction, e.tx_id) if e.tx_id else None
        fields = [("Received from", e.member.user.full_name), ("Category", "Share purchase"),
                  ("Shares bought", f"{e.units:,}"), ("Price per share", m(e.unit_price)), ("Total paid", m(e.amount)),
                  ("Shares held now", f"{share_svc.member_units(group.id, e.member_id):,}"),
                  ("Date", f"{e.entry_date:%d %b %Y}"), ("Payment method", tx.payment_method if tx else None),
                  ("Transaction reference", tx.reference if tx else None), ("Recorded by", _user_name(e.recorded_by))]
        return Receipt(kind, e.id, "Share Purchase Receipt", e.receipt_no or (tx.reference if tx else f"SH-{e.id}"),
                       e.amount, fields, e.entry_date, "This share purchase has been REVERSED." if e.reversed_at else None,
                       e.member.user.full_name, e.member.user.phone, {e.member_id})
    if kind == "share_transfer":
        t = _scoped(ShareTransfer, obj_id, group)
        if t.status != "APPROVED":
            raise ReceiptError("transfer not approved")
        when = to_local(t.decided_at, group.timezone).date() if t.decided_at else t.created_at.date()
        fields = [("From (seller)", t.seller.user.full_name), ("To (buyer)", t.buyer.user.full_name),
                  ("Category", "Transfer of shares"), ("Shares transferred", f"{t.units:,}"),
                  ("Price per share", m(t.unit_price)), ("Total value", m(t.units * t.unit_price)),
                  ("Date", f"{when:%d %b %Y}"), ("Approved by", _user_name(t.decided_by)), ("Note", t.note)]
        return Receipt(kind, t.id, "Share Transfer Receipt", f"ST-{t.id:06d}", t.units * t.unit_price, fields, when, None,
                       f"{t.seller.user.full_name} to {t.buyer.user.full_name}", None, {t.seller_id, t.buyer_id})
    if kind == "withdrawal":
        w = _scoped(SavingsWithdrawal, obj_id, group)
        if w.status not in ("APPROVED", "REVERSED") or not w.receipt_no:
            raise ReceiptError("withdrawal not approved")
        tx = db.session.get(Transaction, w.tx_id) if w.tx_id else None
        when = to_local(w.decided_at, group.timezone).date() if w.decided_at else w.created_at.date()
        fields = [("Paid to", w.member.user.full_name), ("Category", "Savings withdrawal"), ("Date", f"{when:%d %b %Y}"),
                  ("Payment method", w.payment_method), ("Transaction reference", tx.reference if tx else None),
                  ("Approved by", _user_name(w.decided_by))]
        return Receipt(kind, w.id, "Savings Withdrawal Receipt", w.receipt_no, w.amount, fields, when,
                       "This withdrawal has been REVERSED." if w.status == "REVERSED" else None,
                       w.member.user.full_name, w.member.user.phone, {w.member_id})
    if kind == "repayment":
        r = _scoped(LoanRepayment, obj_id, group)
        fields, note = _repayment_breakdown(group, r, m)
        if r.status == "REVERSED":
            note = " ".join(x for x in ("This repayment has been REVERSED.", note) if x)
        return Receipt(kind, r.id, "Loan Repayment Receipt", r.receipt_no, r.amount, fields, r.payment_date,
                       note or None, r.borrower.name, getattr(r.borrower, "phone", None))
    if kind == "disbursement":
        loan = _scoped(Loan, obj_id, group)
        if not loan.disbursement_tx_id:
            raise ReceiptError("not disbursed")
        tx = db.session.get(Transaction, loan.disbursement_tx_id)
        fields = [("Paid to", loan.borrower.name), ("Loan", loan.reference), ("Category", "Loan disbursement"),
                  ("Interest rate", f"{loan.rate_bp / 100:g}% per {loan.rate_period.lower()} "
                                    f"({'flat' if loan.method == 'FLAT' else 'reducing balance'})"),
                  ("Term", f"{loan.duration} {loan.duration_unit.lower()}(s), {loan.frequency.lower()} repayments"),
                  ("Total repayable", m(loan.total_repayable)), ("Date", f"{tx.tx_date:%d %b %Y}"),
                  ("Transaction reference", tx.reference), ("Recorded by", tx.recorder.full_name)]
        return Receipt(kind, loan.id, "Loan Disbursement Receipt", tx.reference, loan.principal, fields, tx.tx_date, None,
                       loan.borrower.name, getattr(loan.borrower, "phone", None))
    if kind == "fine":
        f = _scoped(LoanFine, obj_id, group)
        if f.status != "PAID" or not f.tx_id:
            raise ReceiptError("fine not paid")
        tx = db.session.get(Transaction, f.tx_id)
        fields = [("Received from", f.borrower.name), ("Loan", f.loan.reference), ("Category", "Late fine payment"),
                  ("Deadline", f"{f.deadline:%d %b %Y}"), ("Days late", str(f.days_late)),
                  ("Date paid", f"{(f.settled_on or tx.tx_date):%d %b %Y}"), ("Payment method", tx.payment_method),
                  ("Transaction reference", tx.reference), ("Recorded by", tx.recorder.full_name)]
        return Receipt(kind, f.id, "Late Fine Receipt", tx.reference, f.amount, fields, f.settled_on or tx.tx_date,
                       "This payment has been REVERSED." if ledger.is_reversed(tx.id) else None,
                       f.borrower.name, getattr(f.borrower, "phone", None))
    tx = _scoped(Transaction, obj_id, group)                                     # kind == "transaction"
    who = tx.member.user.full_name if tx.member else (tx.borrower.name if tx.borrower else "")
    fields = [("Party", who), ("Category", _tx_label(tx.tx_type)),
              ("Direction", "Money in" if tx.direction == "IN" else "Money out"), ("Description", tx.description),
              ("Date", f"{tx.tx_date:%d %b %Y}"), ("Payment method", tx.payment_method),
              ("Payment reference", tx.payment_reference), ("Recorded by", tx.recorder.full_name)]
    return Receipt(kind, tx.id, "Transaction Receipt", tx.reference, tx.amount, fields, tx.tx_date,
                   "This entry has been REVERSED." if ledger.is_reversed(tx.id) else None, who, None)


def render_pdf(group, r: Receipt, now) -> bytes:
    return documents.receipt_pdf(group, r.title, r.number, r.fields, r.amount, now, r.note)


# ------------------------------------------------------------------ sharing
def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="receipt-link")


def make_token(group_id, kind, obj_id) -> str:
    return _serializer().dumps({"g": group_id, "k": kind, "i": obj_id})


def read_token(token):
    """(group_id, kind, obj_id) or None when the link is forged or older than RECEIPT_LINK_DAYS."""
    days = int(current_app.config.get("RECEIPT_LINK_DAYS", 30))
    try:
        d = _serializer().loads(token, max_age=days * 86400)
        return int(d["g"]), str(d["k"]), int(d["i"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None


def wa_number(phone):
    """International digits for wa.me (0772... -> 256772...), or None when it does not look like a phone number."""
    digits = re.sub(r"\D", "", phone or "")
    if (phone or "").strip().startswith("+") or digits.startswith("00"):
        digits = digits[2:] if digits.startswith("00") else digits
    elif digits.startswith("0"):
        digits = current_app.config.get("DEFAULT_COUNTRY_CODE", "256") + digits[1:]
    return digits if 9 <= len(digits) <= 15 else None


def share_text(group, r: Receipt, url) -> str:
    lines = [f"*{group.name}* - {r.title}", f"Receipt no: {r.number}", f"Amount: {_money(group, r.amount)}"]
    if r.party:
        lines.append(f"{'Parties' if r.kind == 'share_transfer' else 'Name'}: {r.party}")
    lines.append(f"Date: {r.date:%d %b %Y}")
    if r.note:
        lines.append(f"Note: {r.note}")
    lines += ["", "View or download your PDF receipt:", url]
    return "\n".join(lines)


def wa_url(phone, text):
    n = wa_number(phone)
    return f"https://wa.me/{n or ''}?text={quote(text)}"


# ------------------------------------------------------- "receipt ready" prompt
def offer(group, kind, obj_id):
    """Remember that a receipt was just created, so the next page offers Download / WhatsApp."""
    session["receipt_offer"] = {"g": group.id, "k": kind, "i": obj_id}


def pop_offer(group, is_official, membership_id):
    data = session.pop("receipt_offer", None)
    if not data or data.get("g") != group.id:
        return None
    try:
        r = describe(group, data["k"], data["i"])
    except ReceiptError:
        return None
    return r if r.allowed(is_official, membership_id) else None
