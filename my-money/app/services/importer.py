"""Bulk import of a group's existing records from CSV, Excel or text-based PDF.

Flow: parse -> validate every row into a preview (nothing is saved) -> an official
confirms -> rows are posted through the *same* services as manual entry (so the
ledger, duplicate protection and loan engine behave identically) -> the batch can
be undone with ledger reversals. Dates are read day-first (31/12/2025)."""
import csv
import io
import json
import re
import secrets
from datetime import date, datetime

from types import SimpleNamespace

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import (Borrower, Contribution, Expense, GroupMember, ImportBatch, ImportItem, IncomeCategory,
                        IncomeRecord, Loan, LoanRepayment, Notification, ShareEntry, Transaction, User, utcnow)
from app.services import (finance as fin, importctx, interest, ledger, loans as loans_svc, money, savings as savings_svc,
                          shares as share_svc)
from app.services.groupcfg import get_settings
from app.services.audit import audit

MAX_ROWS = 2000
MAX_BYTES = 5 * 1024 * 1024
ALLOWED_EXT = ("csv", "xlsx", "pdf")


class ImportFileError(ValueError):
    pass


class ScannedPdf(ImportFileError):
    pass


# --------------------------------------------------------------- column specs
# (column, required, help, example)
SPECS = {
    "members": {
        "label": "Members",
        "help": "Add your existing members. They can sign in later by tapping Forgot password; you approve it under Password Resets.",
        "columns": [("full_name", True, "Member's full name", "Grace Nambi"),
                    ("phone", True, "Phone number (used to match the member later)", "0772123456"),
                    ("email", False, "Optional", "grace@example.com"),
                    ("role", False, "MEMBER (default), TREASURER or VICE_CHAIR", "MEMBER"),
                    ("joined_date", False, "Date joined, e.g. 15/03/2023", "15/03/2023")],
    },
    "contributions": {
        "label": "Contributions / savings",
        "help": "Past contributions, or one row per member with category OPENING for their savings balance today.",
        "columns": [("phone", True, "Member's phone (or use full_name)", "0772123456"),
                    ("full_name", False, "Only used when phone is empty", "Grace Nambi"),
                    ("amount", True, "Amount paid", "50000"),
                    ("date", True, "Date paid, e.g. 05/06/2024", "05/06/2024"),
                    ("category", False, "MONTHLY (default), WEEKLY, VOLUNTARY, CUSTOM or OPENING", "MONTHLY"),
                    ("payment_method", False, "Cash, Mobile Money, Bank Transfer, Cheque, Other", "Cash"),
                    ("reference", False, "Receipt / transaction number", "R-0012"),
                    ("notes", False, "Optional", "")],
    },
    "loans": {
        "label": "Loans",
        "help": "Loans with their ORIGINAL terms. For a loan where you only know today's balance, use that balance as the amount and today's date. Import borrowers' repayments afterwards.",
        "columns": [("borrower_name", True, "Borrower's full name", "John Okello"),
                    ("borrower_phone", False, "Optional, helps match the same borrower", "0701234567"),
                    ("principal", True, "Amount lent", "1000000"),
                    ("rate", True, "Interest rate in percent", "10"),
                    ("rate_period", True, "WEEK, MONTH or YEAR (rate is per this period)", "MONTH"),
                    ("method", True, "FLAT or REDUCING", "FLAT"),
                    ("duration", True, "Length of the loan", "6"),
                    ("duration_unit", True, "WEEK or MONTH", "MONTH"),
                    ("frequency", False, "WEEKLY or MONTHLY repayments (default follows duration_unit)", "MONTHLY"),
                    ("disbursement_date", True, "Date the money was lent", "01/02/2025"),
                    ("first_repayment_date", False, "Default: one period after disbursement", "01/03/2025"),
                    ("loan_ref", False, "Your own loan number (lets repayments find the loan)", "L-101"),
                    ("purpose", False, "Optional", "Stock for shop")],
    },
    "repayments": {
        "label": "Loan repayments",
        "help": "Payments already received. Import the loans first. Use loan_ref (or the app's LN- number); borrower_name works if the borrower has one active loan.",
        "columns": [("loan_ref", False, "Your loan number or the app's LN-0001", "L-101"),
                    ("borrower_name", False, "Used when loan_ref is empty", "John Okello"),
                    ("amount", True, "Amount paid", "200000"),
                    ("date", True, "Date paid", "01/03/2025"),
                    ("payment_method", False, "Cash, Mobile Money, ...", "Mobile Money"),
                    ("reference", False, "Receipt / transaction number", "MM-9981"),
                    ("notes", False, "Optional", "")],
    },
}

SPECS["shares"] = {
    "label": "Shares",
    "help": "Each member's shares. Give the number of shares, or just the amount paid and the shares are worked out at the share price. Whole shares only.",
    "columns": [("phone", True, "Member's phone (or use full_name)", "0772123456"),
                ("full_name", False, "Only used when phone is empty", "Grace Nambi"),
                ("shares", False, "Number of shares (or give the amount instead)", "5"),
                ("amount", False, "Amount paid; shares = amount / share price", "50000"),
                ("date", True, "Date bought, e.g. 05/06/2024", "05/06/2024"),
                ("unit_price", False, "Price per share, only if different from the group price", ""),
                ("payment_method", False, "Cash, Mobile Money, Bank Transfer, Cheque, Other", "Cash"),
                ("notes", False, "Optional", "")],
}
SPECS["income"] = {
    "label": "Other income",
    "help": "Registration fees, donations and other money the group earned. Loan interest is not income here; it comes from the loans.",
    "columns": [("category", True, "e.g. Registration fees, Donations, Events", "Registration fees"),
                ("amount", True, "Amount received", "20000"),
                ("date", True, "Date received", "10/01/2025"),
                ("description", False, "Optional", "New members"),
                ("payment_method", False, "Cash, Mobile Money, ...", "Cash")],
}
SPECS["expenses"] = {
    "label": "Expenses",
    "help": "Money the group spent. Import contributions, income and repayments first so the group has the cash.",
    "columns": [("title", True, "What it was for", "Annual meeting"),
                ("category", False, "e.g. Transport, Rent, Stationery (default Other)", "Other"),
                ("amount", True, "Amount spent", "150000"),
                ("date", True, "Date spent", "20/02/2025"),
                ("payment_method", False, "Cash, Mobile Money, ...", "Cash"),
                ("notes", False, "Optional", "")],
}
ORDER = ["members", "contributions", "shares", "loans", "repayments", "income", "expenses"]
SPECS = {k: SPECS[k] for k in ORDER}

ALIASES = {
    "units": "shares", "number_of_shares": "shares", "no_of_shares": "shares", "shares_bought": "shares",
    "share_count": "shares", "price_per_share": "unit_price", "share_price": "unit_price",
    "item": "title", "expense": "title", "expense_title": "title", "expense_category": "category",
    "income_category": "category", "source": "category", "details": "description",
    "name": "full_name", "member": "full_name", "member_name": "full_name", "fullname": "full_name",
    "mobile": "phone", "phone_number": "phone", "tel": "phone", "telephone": "phone", "contact": "phone",
    "mobile_number": "phone", "e_mail": "email", "joined": "joined_date", "date_joined": "joined_date",
    "amount_paid": "amount", "paid": "amount", "contribution": "amount", "savings": "amount",
    "payment_date": "date", "date_paid": "date", "paid_on": "date", "contribution_date": "date",
    "type": "category", "payment_type": "category", "method_of_payment": "payment_method", "mode": "payment_method",
    "receipt": "reference", "receipt_no": "reference", "receipt_number": "reference", "ref": "reference",
    "payment_reference": "reference", "borrower": "borrower_name", "client": "borrower_name",
    "client_name": "borrower_name", "borrower_phone_number": "borrower_phone", "amount_lent": "principal",
    "loan_amount": "principal", "interest": "rate", "interest_rate": "rate", "rate_percent": "rate",
    "interest_period": "rate_period", "period": "rate_period", "interest_type": "method",
    "loan_type": "method", "interest_method": "method", "term": "duration", "loan_period": "duration",
    "term_unit": "duration_unit", "repayment_frequency": "frequency", "date_lent": "disbursement_date",
    "disbursed": "disbursement_date", "disbursed_on": "disbursement_date", "start_date": "disbursement_date",
    "loan_date": "disbursement_date", "first_payment_date": "first_repayment_date", "first_due_date": "first_repayment_date",
    "loan_number": "loan_ref", "loan_no": "loan_ref", "loan_reference": "loan_ref", "loan_id": "loan_ref",
    "note": "notes", "remarks": "notes", "comment": "notes", "loan_purpose": "purpose",
}


def norm_header(h):
    k = re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")
    return ALIASES.get(k, k)


# ---------------------------------------------------------------- file reading
def _cell(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else repr(v)
    return str(v).strip()


def _csv_rows(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    sample = text[:4096]
    delim = max((",", ";", "\t"), key=sample.count)
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]


def _xlsx_rows(raw):
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise ImportFileError("This Excel file could not be read. Save it as .xlsx and try again.")
    sheets = [w for w in wb.worksheets if w.title.strip().lower() not in ("instructions", "readme", "help")]
    ws = (sheets or wb.worksheets)[0]
    return [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]


def _pdf_rows(raw):
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover
        raise ImportFileError("PDF reading is not available on this server.")
    rows, has_text = [], False
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages[:60]:
                if (page.extract_text() or "").strip():
                    has_text = True
                for table in page.extract_tables():
                    rows.extend([[_cell(c).replace("\n", " ") for c in r] for r in table])
    except Exception:
        raise ImportFileError("This PDF could not be read.")
    if not has_text:
        raise ScannedPdf("This PDF is a scan (a picture of paper), so its text cannot be read automatically.")
    if not rows:
        raise ImportFileError("No table was found in this PDF. Use a PDF with a clear table, or the Excel template.")
    return rows


def read_table(raw, filename):
    """-> (headers, rows) with normalised headers; rows are lists of strings with their file row number."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise ImportFileError("Upload a .csv, .xlsx or .pdf file.")
    table = {"csv": _csv_rows, "xlsx": _xlsx_rows, "pdf": _pdf_rows}[ext](raw)
    numbered = [(i + 1, [_cell(c) for c in r]) for i, r in enumerate(table)]
    numbered = [(n, r) for n, r in numbered if any(r)]
    if not numbered:
        raise ImportFileError("The file is empty.")
    hdr_n, hdr = numbered[0]
    headers = [norm_header(h) for h in hdr]
    body = [(n, r) for n, r in numbered[1:] if [norm_header(c) for c in r] != headers]  # repeated PDF headers
    if len(body) > MAX_ROWS:
        raise ImportFileError(f"Too many rows ({len(body)}). Import at most {MAX_ROWS} rows at a time.")
    return headers, body


# --------------------------------------------------------------- value parsing
class RowError(ValueError):
    pass


def p_date(v, label="date", required=True):
    v = (v or "").strip()
    if not v:
        if required:
            raise RowError(f"{label} is required")
        return None
    v = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", v)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y",
                "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise RowError(f"{label} '{v}' is not a valid date (use day/month/year, e.g. 31/12/2025)")


def p_amount(v, decimals, label="amount"):
    v = re.sub(r"[A-Za-z]{3}\b|[^\d.,\-]", "", str(v or ""))  # drop currency words/symbols
    try:
        a = money.to_minor(v, decimals)
    except ValueError as e:
        raise RowError(f"{label}: {e}")
    if a <= 0:
        raise RowError(f"{label} must be greater than zero")
    return a


def p_choice(v, mapping, label, default=None):
    key = re.sub(r"[^a-z]+", " ", (v or "").lower()).strip()
    if not key:
        if default:
            return default
        raise RowError(f"{label} is required")
    for needle, value in mapping:
        if key == needle or key.startswith(needle):
            return value
    raise RowError(f"{label} '{v}' is not recognised")


PERIOD_MAP = [("week", "WEEK"), ("month", "MONTH"), ("year", "YEAR"), ("annual", "YEAR"), ("per annum", "YEAR")]
UNIT_MAP = [("week", "WEEK"), ("month", "MONTH")]
FREQ_MAP = [("week", "WEEKLY"), ("month", "MONTHLY")]
METHOD_MAP = [("flat", "FLAT"), ("simple", "FLAT"), ("reducing", "REDUCING"), ("declining", "REDUCING"),
              ("diminishing", "REDUCING")]
ROLE_MAP = [("member", "MEMBER"), ("treasurer", "TREASURER"), ("vice", "VICE_CHAIR")]
CAT_MAP = [("weekly", "WEEKLY"), ("monthly", "MONTHLY"), ("voluntary", "VOLUNTARY"), ("custom", "CUSTOM"),
           ("opening", "OPENING"), ("balance", "OPENING"), ("savings", "VOLUNTARY")]


def p_method(v):
    key = (v or "").strip().lower()
    for m in savings_svc.PAYMENT_METHODS:
        if key == m.lower():
            return m
    if not key:
        return None
    return "Mobile Money" if "mobile" in key or "momo" in key or "mtn" in key or "airtel" in key else \
        "Bank Transfer" if "bank" in key else "Other"


def phone_key(p):
    return re.sub(r"\D", "", p or "")[-9:]  # compare the last 9 digits (0772.. == +256772..)


# ------------------------------------------------------------------ validating
def validate(group, kind, headers, body, virtual=None, lenient=False):
    """Return a list of row dicts: {n, d (normalised data), errors, warnings}."""
    spec = SPECS[kind]
    required_cols = [c for c, req, *_ in spec["columns"] if req]
    if kind in ("contributions", "shares") and "phone" not in headers and "full_name" in headers:
        required_cols = [c for c in required_cols if c != "phone"]
    if kind == "repayments" and "loan_ref" not in headers and "borrower_name" in headers:
        pass
    missing = [c for c in required_cols if c not in headers]
    if kind == "repayments" and "loan_ref" not in headers and "borrower_name" not in headers:
        missing.append("loan_ref (or borrower_name)")
    if kind == "shares" and "shares" not in headers and "amount" not in headers:
        missing.append("shares (or amount)")
    if missing:
        raise ImportFileError("Missing column(s): " + ", ".join(missing) + ". Download the template to see the layout.")
    ctx = _context(group, kind, virtual, lenient)
    out, seen = [], set()
    for n, cells in body:
        row = {h: (cells[i] if i < len(cells) else "") for i, h in enumerate(headers) if h}
        r = {"n": n, "d": {}, "errors": [], "warnings": []}
        try:
            r["d"] = _VALIDATORS[kind](group, row, ctx, seen, r["warnings"])
        except RowError as e:
            r["errors"].append(str(e))
        except (ValueError, loans_svc.LoanError) as e:
            r["errors"].append(str(e))
        out.append(r)
    return out


def _context(group, kind, virtual=None, lenient=False):
    """`virtual` holds what the SAME workbook creates earlier on (members, loans), so later sheets can refer to
    people and loans that do not exist yet. `lenient` lets a member who is already in the group be kept as is."""
    ctx = {"dec": group.currency_decimals, "today": date.today(), "lenient": lenient,
           "virtual": virtual or {"members": {}, "loans": []}}
    if kind == "shares":
        ctx["share_price"] = get_settings(group.id).share_price
    members = GroupMember.query.filter_by(group_id=group.id).all()
    ctx["members"] = {phone_key(m.user.phone): m for m in members if m.status == "ACTIVE"}
    ctx["members_all"] = {phone_key(m.user.phone): m for m in members}
    ctx["names"] = {}
    for m in members:
        if m.status == "ACTIVE":
            ctx["names"].setdefault(m.user.full_name.strip().lower(), []).append(m)
    if kind == "members":
        ctx["users_by_phone"] = {phone_key(u.phone): u for u in User.query.all()} if User.query.count() < 50000 else {}
        ctx["users_by_email"] = {u.email: u for u in User.query.filter(User.email.isnot(None)).all()}
    if kind in ("loans", "repayments"):
        ctx["borrowers"] = Borrower.query.filter_by(group_id=group.id).all()
        ctx["loans"] = Loan.query.filter_by(group_id=group.id).all()
    if kind == "repayments":
        ctx["remaining"] = {l.id: sum(loans_svc.outstanding(l)) for l in ctx["loans"]
                            if l.status in loans_svc.ACTIVE_STATES}
        for v in ctx["virtual"]["loans"]:
            ctx["remaining"][v["key"]] = v["total"]
    return ctx


class _VM:
    """A member that is listed in the same workbook but has not been created yet."""
    id = None

    def __init__(self, key, name):
        self.key, self.user = key, SimpleNamespace(full_name=name)


class _VL:
    """A loan that is listed in the same workbook but has not been created yet."""
    id = None
    status = "ACTIVE"

    def __init__(self, v):
        self.v, self.vkey = v, v["key"]
        self.reference = v["ref"] or f"{v['borrower']} (loans sheet row {v['n']})"
        self.disbursement_date = date.fromisoformat(v["disb"])


def _find_member(row, ctx):
    ph = phone_key(row.get("phone"))
    vmembers = ctx["virtual"]["members"]
    if ph:
        m = ctx["members"].get(ph)
        if m:
            return m
        if ph in vmembers:
            return _VM(ph, vmembers[ph])
        raise RowError(f"No active member with phone {row.get('phone')}. Import members first.")
    nm = (row.get("full_name") or "").strip().lower()
    if not nm:
        raise RowError("phone (or full_name) is required")
    found = ctx["names"].get(nm, [])
    vfound = [(k, n) for k, n in vmembers.items() if n.strip().lower() == nm]
    if len(found) + len(vfound) == 1:
        return found[0] if found else _VM(*vfound[0])
    found = found + vfound
    raise RowError(f"Member '{row.get('full_name')}' " + ("matches several members; use the phone number"
                                                           if found else "was not found"))


def _v_members(group, row, ctx, seen, warns):
    name = (row.get("full_name") or "").strip()
    if len(name) < 2:
        raise RowError("full_name is required")
    from app.auth import normalize_phone, valid_phone
    phone = normalize_phone(row.get("phone"))
    if not valid_phone(phone):
        raise RowError("phone is missing or not valid")
    email = (row.get("email") or "").strip().lower() or None
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise RowError("email is not valid")
    role = p_choice(row.get("role"), ROLE_MAP, "role", default="MEMBER")
    joined = p_date(row.get("joined_date"), "joined_date", required=False)
    key = phone_key(phone)
    if key in seen:
        raise RowError("This phone number appears twice in the file")
    seen.add(key)
    existing = ctx["members_all"].get(key)
    if existing and existing.status == "ACTIVE":
        if ctx.get("lenient"):       # a full workbook may list people who are already in the group: keep them as they are
            warns.append("Already a member: kept as is")
            return {"full_name": name, "phone": phone, "email": email, "role": role, "joined_date": None, "skip": True}
        raise RowError("Already a member of this group")
    if existing and existing.status == "SUSPENDED":
        raise RowError("This member is suspended in this group")
    user = ctx["users_by_phone"].get(key)
    if user and user.status != "ACTIVE":
        raise RowError("This phone belongs to a suspended account")
    if not user and email and email in ctx["users_by_email"]:
        raise RowError("This email already belongs to another account")
    if user:
        warns.append("Existing MY MONEY account: will be added to this group and notified")
    return {"full_name": name, "phone": phone, "email": email, "role": role,
            "joined_date": joined.isoformat() if joined else None}


def _v_contributions(group, row, ctx, seen, warns):
    m = _find_member(row, ctx)
    amount = p_amount(row.get("amount"), ctx["dec"])
    d = p_date(row.get("date"))
    if d > ctx["today"]:
        raise RowError("date is in the future")
    cat = p_choice(row.get("category"), CAT_MAP, "category", default="MONTHLY")
    notes = (row.get("notes") or "").strip()
    if cat == "OPENING":
        cat, notes = "CUSTOM", ("Opening balance (imported). " + notes).strip()
    key = (m.id or f"v{m.key}", d, amount, cat)
    if key in seen:
        raise RowError("Duplicate of another row in this file (same member, date and amount)")
    seen.add(key)
    if m.id and Contribution.query.filter_by(group_id=group.id, member_id=m.id, contribution_date=d, amount=amount).first():
        raise RowError("Already recorded (same member, date and amount)")
    return {"member_id": m.id, "member_key": getattr(m, "key", None), "member": m.user.full_name, "amount": amount, "date": d.isoformat(), "category": cat,
            "payment_method": p_method(row.get("payment_method")), "reference": (row.get("reference") or "").strip()[:80] or None,
            "notes": notes[:300]}


def _v_shares(group, row, ctx, seen, warns):
    m = _find_member(row, ctx)
    d = p_date(row.get("date"))
    if d > ctx["today"]:
        raise RowError("date is in the future")
    price_raw = (row.get("unit_price") or "").strip()
    price = p_amount(price_raw, ctx["dec"], "unit_price") if price_raw else ctx["share_price"]
    if price <= 0:
        raise RowError("Set a share price in Share settings first")
    units_raw = re.sub(r"[^\d.]", "", row.get("shares") or "")
    amount_raw = (row.get("amount") or "").strip()
    if units_raw:
        try:
            if float(units_raw) != int(float(units_raw)):
                raise ValueError
            units = int(float(units_raw))
        except ValueError:
            raise RowError("shares must be a whole number")
        if units < 1:
            raise RowError("shares must be at least 1")
        if amount_raw and p_amount(amount_raw, ctx["dec"]) != units * price:
            warns.append("amount does not match shares x price: the shares were used")
    elif amount_raw:
        paid = p_amount(amount_raw, ctx["dec"])
        units, left = paid // price, paid % price
        if units < 1:
            raise RowError(f"amount is less than the price of one share ({money.fmt(price, group.currency, group.currency_decimals)})")
        if left:
            warns.append(f"{money.fmt(left, group.currency, group.currency_decimals)} is less than one share and is not recorded")
    else:
        raise RowError("Enter the number of shares or the amount paid")
    amount = units * price
    key = (m.id or f"v{m.key}", d, units, "share")
    if key in seen:
        raise RowError("Duplicate of another row in this file (same member, date and shares)")
    seen.add(key)
    if m.id and ShareEntry.query.filter_by(group_id=group.id, member_id=m.id, kind="PURCHASE", entry_date=d, units=units,
                                           reversed_at=None).first():
        raise RowError("Already recorded (same member, date and shares)")
    warns.append(f"{units} share(s) = {money.fmt(amount, group.currency, group.currency_decimals)}")
    return {"member_id": m.id, "member_key": getattr(m, "key", None), "member": m.user.full_name, "units": units,
            "unit_price": price, "amount": amount, "date": d.isoformat(),
            "payment_method": p_method(row.get("payment_method")), "notes": (row.get("notes") or "").strip()[:300]}


def _v_income(group, row, ctx, seen, warns):
    cat = (row.get("category") or "").strip()[:60]
    if len(cat) < 2:
        raise RowError("category is required")
    amount = p_amount(row.get("amount"), ctx["dec"])
    d = p_date(row.get("date"))
    if d > ctx["today"]:
        raise RowError("date is in the future")
    desc = (row.get("description") or "").strip()[:300]
    key = (cat.lower(), d, amount, desc.lower())
    if key in seen:
        raise RowError("Duplicate of another row in this file (same category, date and amount)")
    seen.add(key)
    if (IncomeRecord.query.join(IncomeCategory, IncomeCategory.id == IncomeRecord.category_id)
            .filter(IncomeRecord.group_id == group.id, IncomeRecord.income_date == d, IncomeRecord.amount == amount,
                    db.func.lower(IncomeCategory.name) == cat.lower()).first()):
        raise RowError("Already recorded (same category, date and amount)")
    return {"category": cat, "amount": amount, "date": d.isoformat(), "description": desc,
            "payment_method": p_method(row.get("payment_method"))}


def _v_expenses(group, row, ctx, seen, warns):
    title = (row.get("title") or "").strip()[:120]
    if len(title) < 2:
        raise RowError("title is required")
    amount = p_amount(row.get("amount"), ctx["dec"])
    d = p_date(row.get("date"))
    if d > ctx["today"]:
        raise RowError("date is in the future")
    cat = (row.get("category") or "").strip()[:60] or "Other"
    key = (title.lower(), d, amount)
    if key in seen:
        raise RowError("Duplicate of another row in this file (same expense, date and amount)")
    seen.add(key)
    if Expense.query.filter_by(group_id=group.id, expense_date=d, amount=amount, description=title).first():
        raise RowError("Already recorded (same expense, date and amount)")
    return {"title": title, "category": cat, "amount": amount, "date": d.isoformat(),
            "payment_method": p_method(row.get("payment_method")), "notes": (row.get("notes") or "").strip()[:300]}


def _first_due(disb, unit, freq):
    return interest.add_months(disb, 1) if freq == "MONTHLY" else date.fromordinal(disb.toordinal() + 7)


def _v_loans(group, row, ctx, seen, warns):
    name = (row.get("borrower_name") or "").strip()
    if len(name) < 2:
        raise RowError("borrower_name is required")
    principal = p_amount(row.get("principal"), ctx["dec"], "principal")
    try:
        rate_bp = money.parse_rate_bp(row.get("rate"))
    except ValueError as e:
        raise RowError(str(e))
    period = p_choice(row.get("rate_period"), PERIOD_MAP, "rate_period")
    method = p_choice(row.get("method"), METHOD_MAP, "method")
    try:
        duration = int(float(str(row.get("duration") or "").replace(",", "")))
    except ValueError:
        raise RowError("duration must be a whole number")
    if duration < 1:
        raise RowError("duration must be at least 1")
    unit = p_choice(row.get("duration_unit"), UNIT_MAP, "duration_unit")
    freq = p_choice(row.get("frequency"), FREQ_MAP, "frequency", default="MONTHLY" if unit == "MONTH" else "WEEKLY")
    disb = p_date(row.get("disbursement_date"), "disbursement_date")
    if disb > ctx["today"]:
        raise RowError("disbursement_date is in the future")
    first = p_date(row.get("first_repayment_date"), "first_repayment_date", required=False) or _first_due(disb, unit, freq)
    data = {"principal": principal, "rate_bp": rate_bp, "rate_period": period, "method": method,
            "duration": duration, "duration_unit": unit, "frequency": freq, "disbursement_date": disb,
            "first_repayment_date": first}
    res = loans_svc.preview(data)  # validates platform rules and builds the schedule
    ref = (row.get("loan_ref") or "").strip()[:40] or None
    key = (name.lower(), principal, disb)
    if key in seen:
        raise RowError("Duplicate of another row in this file (same borrower, amount and date)")
    seen.add(key)
    for b in ctx["borrowers"]:
        if b.name.strip().lower() == name.lower() and Loan.query.filter_by(
                group_id=group.id, borrower_id=b.id, principal=principal, disbursement_date=disb).first():
            raise RowError("Already recorded (same borrower, amount and date)")
    if ref and any(f"[ref:{ref}]" in (l.notes or "") for l in ctx["loans"]):
        raise RowError(f"loan_ref '{ref}' is already used by another loan")
    warns.append(f"Total interest {money.fmt(res.total_interest, group.currency, group.currency_decimals)}; "
                 f"{len(res.instalments)} instalments")
    return {"borrower_name": name, "borrower_phone": (row.get("borrower_phone") or "").strip() or None,
            "principal": principal, "rate_bp": rate_bp, "rate_period": period, "method": method,
            "duration": duration, "duration_unit": unit, "frequency": freq, "disbursement_date": disb.isoformat(),
            "first_repayment_date": first.isoformat(), "loan_ref": ref, "total_due": principal + res.total_interest,
            "purpose": (row.get("purpose") or "").strip()[:300]}


def _find_loan(row, ctx):
    ref = (row.get("loan_ref") or "").strip()
    if ref:
        for l in ctx["loans"]:
            if l.reference.lower() == ref.lower() or f"[ref:{ref}]" in (l.notes or ""):
                return l
        for v in ctx["virtual"]["loans"]:
            if v["ref"] and v["ref"].lower() == ref.lower():
                return _VL(v)
        raise RowError(f"Loan '{ref}' was not found. Import the loans first.")
    name = (row.get("borrower_name") or "").strip().lower()
    if not name:
        raise RowError("loan_ref (or borrower_name) is required")
    ids = {b.id for b in ctx["borrowers"] if b.name.strip().lower() == name}
    found = [l for l in ctx["loans"] if l.borrower_id in ids and l.status in loans_svc.ACTIVE_STATES]
    vfound = [v for v in ctx["virtual"]["loans"] if v["borrower"].strip().lower() == name]
    if len(found) + len(vfound) == 1:
        return found[0] if found else _VL(vfound[0])
    found = found + vfound
    raise RowError(f"Borrower '{row.get('borrower_name')}' has " + ("several active loans; add loan_ref"
                                                                 if found else "no active loan"))


def _v_repayments(group, row, ctx, seen, warns):
    loan = _find_loan(row, ctx)
    if loan.status not in loans_svc.ACTIVE_STATES:
        raise RowError(f"Loan {loan.reference} is not active ({loan.status})")
    amount = p_amount(row.get("amount"), ctx["dec"])
    d = p_date(row.get("date"))
    if d > ctx["today"]:
        raise RowError("date is in the future")
    if loan.disbursement_date and d < loan.disbursement_date:
        raise RowError(f"date is before the loan was disbursed ({loan.disbursement_date.isoformat()})")
    ref = (row.get("reference") or "").strip()[:80] or None
    lkey = loan.id or loan.vkey
    key = (lkey, d, amount, ref)
    if key in seen:
        raise RowError("Duplicate of another row in this file")
    seen.add(key)
    if ref and LoanRepayment.query.filter_by(group_id=group.id, payment_reference=ref).first():
        raise RowError("A repayment with this reference number already exists")
    if loan.id and LoanRepayment.query.filter_by(group_id=group.id, loan_id=loan.id, payment_date=d, amount=amount,
                                                 status="POSTED").first():
        raise RowError("Already recorded (same loan, date and amount)")
    left = ctx["remaining"].get(lkey, 0)
    if amount > left:
        raise RowError("More than the outstanding balance of this loan")
    ctx["remaining"][lkey] = left - amount
    return {"loan_id": loan.id, "virtual_loan": getattr(loan, "v", None), "loan": loan.reference, "amount": amount, "date": d.isoformat(),
            "payment_method": p_method(row.get("payment_method")), "reference": ref,
            "notes": (row.get("notes") or "").strip()[:300]}


_VALIDATORS = {"members": _v_members, "contributions": _v_contributions, "loans": _v_loans,
               "repayments": _v_repayments, "shares": _v_shares, "income": _v_income, "expenses": _v_expenses}


# -------------------------------------------------------------------- batches
def create_batch(group, user, kind, filename, rows, source_file=None):
    ok = sum(1 for r in rows if not r["errors"])
    b = ImportBatch(group_id=group.id, kind=kind, filename=filename[:200], status="PREVIEW",
                    total_rows=len(rows), ok_rows=ok, error_rows=len(rows) - ok, rows_json=json.dumps(rows),
                    created_by=user.id, source_file_id=source_file.id if source_file else None)
    db.session.add(b)
    db.session.flush()
    audit("import.previewed", "import_batch", b.id, {"kind": kind, "rows": len(rows), "errors": len(rows) - ok},
          group_id=group.id, user_id=user.id)
    return b


def batch_rows(batch):
    return json.loads(batch.rows_json or "[]")


# ---------------------------------------------------------------------- apply
def apply_batch(group, user, batch):
    """Post every valid row. Each row runs in its own savepoint, so one bad row
    never blocks the rest; failures are recorded on the row."""
    if batch.status != "PREVIEW" or batch.group_id != group.id:
        raise ImportFileError("This import has already been handled.")
    rows = batch_rows(batch)
    todo = [r for r in rows if not r["errors"]]
    if batch.kind == "repayments":
        todo.sort(key=lambda r: (r["d"]["date"], r["n"]))
    applied = 0
    with importctx.importing():
        for r in todo:
            try:
                with db.session.begin_nested():
                    _APPLIERS[batch.kind](group, user, batch, r)
                applied += 1
            except (ValueError, loans_svc.LoanError, savings_svc.DuplicateError) as e:
                r["errors"].append(f"Not imported: {e}")
    batch.applied_rows = applied
    batch.error_rows = sum(1 for r in rows if r["errors"])
    batch.rows_json = json.dumps(rows)
    batch.status, batch.applied_at = "APPLIED", utcnow()
    audit("import.applied", "import_batch", batch.id, {"kind": batch.kind, "applied": applied},
          group_id=group.id, user_id=user.id)
    return applied


def _item(batch, etype, eid, detail=None):
    db.session.add(ImportItem(batch_id=batch.id, entity_type=etype, entity_id=eid, detail=detail))


def _resolve_member(group, d):
    """The member a row is for: by id, or (for a member created earlier in the same workbook) by phone number."""
    if d.get("member_id"):
        return db.session.get(GroupMember, d["member_id"])
    for m in GroupMember.query.filter_by(group_id=group.id, status="ACTIVE").all():
        if phone_key(m.user.phone) == d.get("member_key"):
            return m
    raise ImportFileError("The member for this row was not created, so it was skipped")


def _resolve_loan(group, d):
    """The loan a repayment is for: by id, or (for a loan created earlier in the same workbook) by its reference."""
    if d.get("loan_id"):
        return db.session.get(Loan, d["loan_id"])
    v = d.get("virtual_loan") or {}
    q = Loan.query.filter(Loan.group_id == group.id, Loan.disbursement_tx_id.isnot(None))
    if v.get("ref"):
        for l in q:
            if f"[ref:{v['ref']}]" in (l.notes or ""):
                return l
    else:
        for l in q.filter(Loan.status.in_(loans_svc.ACTIVE_STATES)):
            if l.borrower.name.strip().lower() == v.get("borrower", "").strip().lower() and \
                    l.disbursement_date.isoformat() == v.get("disb"):
                return l
    raise ImportFileError("The loan for this row was not created, so it was skipped")


def _a_member(group, user, batch, r):
    d = r["d"]
    if d.get("skip"):          # already in the group: nothing to create
        return
    key = phone_key(d["phone"])
    u = next((x for x in User.query.filter(User.phone.like(f"%{key}")).all() if phone_key(x.phone) == key), None)
    new_user = u is None
    if new_user:
        u = User(full_name=d["full_name"], phone=d["phone"], email=d["email"],
                 password_hash=generate_password_hash(secrets.token_urlsafe(32)))  # must claim via Forgot password
        db.session.add(u)
        db.session.flush()
    gm = GroupMember.query.filter_by(group_id=group.id, user_id=u.id).first()
    joined = datetime.fromisoformat(d["joined_date"]) if d.get("joined_date") else utcnow()
    if gm is None:
        gm = GroupMember(group_id=group.id, user_id=u.id, role=d["role"], status="ACTIVE", approved_by=user.id,
                         joined_at=joined)
        db.session.add(gm)
    else:  # re-import after an undo
        gm.status, gm.role, gm.approved_by, gm.joined_at = "ACTIVE", d["role"], user.id, joined
    db.session.flush()
    _item(batch, "member", gm.id, "new_user" if new_user else "existing_user")
    if not new_user:
        with importctx_off():
            from app.services import notify
            notify.notify([u.id], group.id, "MEMBERSHIP", "Added to a group",
                          f"You were added to {group.name}.", f"/g/{group.id}/")


class importctx_off:
    """Temporarily leave import mode (used to notify an existing account that it was added)."""
    def __enter__(self):
        self._t = importctx._active.set(False)

    def __exit__(self, *a):
        importctx._active.reset(self._t)


def _a_contribution(group, user, batch, r):
    d = r["d"]
    m = _resolve_member(group, d)
    c = savings_svc.record_contribution(group, user, m, d["amount"], d["category"], date.fromisoformat(d["date"]),
                                        d["payment_method"], d["reference"], d["notes"],
                                        idempotency_key=f"imp-{batch.id}-{r['n']}")
    _item(batch, "contribution", c.id)


def _a_loan(group, user, batch, r):
    d = r["d"]
    name, phone = d["borrower_name"], d.get("borrower_phone")
    b = None
    for cand in Borrower.query.filter_by(group_id=group.id).all():
        if cand.name.strip().lower() == name.lower() or (phone and cand.phone and phone_key(cand.phone) == phone_key(phone)
                                                         and cand.name.strip().lower() == name.lower()):
            b = cand
            break
    if b is None:
        b = Borrower(group_id=group.id, name=name, phone=phone, category="Individual",
                     registered_on=date.fromisoformat(d["disbursement_date"]), created_by=user.id,
                     notes="Created by import")
        db.session.add(b)
        db.session.flush()
        _item(batch, "borrower", b.id)
    data = {"borrower_id": b.id, "principal": d["principal"], "rate_bp": d["rate_bp"], "rate_period": d["rate_period"],
            "method": d["method"], "duration": d["duration"], "duration_unit": d["duration_unit"],
            "frequency": d["frequency"], "disbursement_date": date.fromisoformat(d["disbursement_date"]),
            "first_repayment_date": date.fromisoformat(d["first_repayment_date"]), "purpose": d["purpose"]}
    loan = loans_svc.create_loan(group, user, data)
    loan.status = "APPROVED"  # historical loan: already approved and paid out before it was entered
    loan.notes = "Imported from existing records." + (f" [ref:{d['loan_ref']}]" if d.get("loan_ref") else "")
    loans_svc.disburse(group, loan, user, data["disbursement_date"])
    _item(batch, "loan", loan.id)


def _a_repayment(group, user, batch, r):
    d = r["d"]
    loan = _resolve_loan(group, d)
    rep = loans_svc.record_repayment(group, loan, user, d["amount"], date.fromisoformat(d["date"]),
                                     d["payment_method"], d["reference"], d["notes"],
                                     idempotency_key=f"imp-{batch.id}-{r['n']}")
    _item(batch, "repayment", rep.id)


def _a_share(group, user, batch, r):
    d = r["d"]
    m = _resolve_member(group, d)
    e = share_svc.record_purchase(group, user, m, d["units"], date.fromisoformat(d["date"]), d["unit_price"],
                                  d["notes"], d["payment_method"])
    _item(batch, "share", e.id)


def _a_income(group, user, batch, r):
    d = r["d"]
    cat = fin.add_category(IncomeCategory, group.id, d["category"])
    rec = fin.record_income(group, user, cat.id, d["amount"], date.fromisoformat(d["date"]), d["description"],
                            d["payment_method"])
    _item(batch, "income", rec.id)


def _a_expense(group, user, batch, r):
    d = r["d"]
    exp = fin.record_expense(group, user, d["title"], d["category"], date.fromisoformat(d["date"]),
                             [(d["title"], d["amount"])], d["payment_method"])
    _item(batch, "expense", exp.id)


_APPLIERS = {"members": _a_member, "contributions": _a_contribution, "loans": _a_loan, "repayments": _a_repayment,
             "shares": _a_share, "income": _a_income, "expenses": _a_expense}


# ----------------------------------------------------------------------- undo
def undo_batch(group, user, batch, today):
    """Reverse everything an applied batch created (ledger reversal rows; nothing is deleted)."""
    if batch.status != "APPLIED" or batch.group_id != group.id:
        raise ImportFileError("Only an applied import can be undone.")
    reason = f"Import #{batch.id} undone"
    kept = []
    items = ImportItem.query.filter_by(batch_id=batch.id).order_by(ImportItem.id.desc()).all()
    with importctx.importing():
        for it in items:
            if it.entity_type == "repayment":
                rep = db.session.get(LoanRepayment, it.entity_id)
                if rep and rep.status == "POSTED":
                    loans_svc.reverse_repayment(group, rep, user, reason, today)
            elif it.entity_type == "contribution":
                c = db.session.get(Contribution, it.entity_id)
                if c and not ledger.is_reversed(c.tx_id):
                    savings_svc.reverse_contribution(group, c, user, reason, today)
            elif it.entity_type == "share":
                e = db.session.get(ShareEntry, it.entity_id)
                if e and not e.reversed_at:
                    share_svc.reverse_purchase(group, e, user, reason, today)
            elif it.entity_type == "income":
                rec = db.session.get(IncomeRecord, it.entity_id)
                if rec and not ledger.is_reversed(rec.tx_id):
                    fin.reverse_income(group, rec, user, reason, today)
            elif it.entity_type == "expense":
                exp = db.session.get(Expense, it.entity_id)
                if exp and not ledger.is_reversed(exp.tx_id):
                    fin.reverse_expense(group, exp, user, reason, today)
            elif it.entity_type == "loan":
                loan = db.session.get(Loan, it.entity_id)
                if loan is None:
                    continue
                if LoanRepayment.query.filter_by(loan_id=loan.id, status="POSTED").first():
                    raise ImportFileError(f"Loan {loan.reference} has repayments. Undo the repayments import first.")
                if loan.status == "ACTIVE" and loan.disbursement_tx_id:
                    loans_svc.reverse_disbursement(group, loan, user, reason, today)
                    loan.status = "REJECTED"
                    loan.notes = (loan.notes or "") + " (import undone)"
            elif it.entity_type == "member":
                gm = db.session.get(GroupMember, it.entity_id)
                if gm is None:
                    continue
                has = Transaction.query.filter_by(group_id=group.id, member_id=gm.id).first() or \
                    Contribution.query.filter_by(group_id=group.id, member_id=gm.id).first()
                if has:
                    kept.append(gm.user.full_name)
                else:
                    gm.status = "REJECTED"
    batch.status, batch.undone_at, batch.undone_by = "UNDONE", utcnow(), user.id
    audit("import.undone", "import_batch", batch.id, {"kind": batch.kind, "kept_members": kept},
          group_id=group.id, user_id=user.id)
    return kept


# ------------------------------------------------------------------ templates
def template_rows(kind):
    cols = SPECS[kind]["columns"]
    return [c[0] for c in cols], [c[3] for c in cols], [c[2] for c in cols]


def template_csv(kind):
    head, example, _ = template_rows(kind)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerow(example)
    return buf.getvalue().encode("utf-8-sig")


def template_xlsx(kind):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    head, example, helps = template_rows(kind)
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(head)
    ws.append(example)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="142F66")
    for i, h in enumerate(head, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = max(16, len(h) + 4)
    guide = wb.create_sheet("Instructions")
    guide.append(["Column", "Required", "What to enter"])
    for (c, req, help_, _ex) in SPECS[kind]["columns"]:
        guide.append([c, "Yes" if req else "No", help_])
    guide.append([])
    guide.append(["Delete the example row in the Data sheet, then add your own. Dates are day/month/year."])
    guide.column_dimensions["A"].width, guide.column_dimensions["C"].width = 24, 90
    for c in guide[1]:
        c.font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
