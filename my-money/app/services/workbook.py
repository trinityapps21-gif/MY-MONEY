"""Full workbook import: ONE Excel file holding everything a group already has, placed in the right places at once.

The workbook has one sheet per kind of record: Members, Contributions, Shares, Loans, Repayments, Income, Expenses.
Every sheet goes through the same validators and posting code as the single-sheet imports (so the ledger, loan
schedules, interest, share ranks and duplicate protection behave exactly as if each record had been typed in), but:

  * the sheets are checked TOGETHER, in dependency order, so a contribution, share or repayment may refer to a member
    or loan that is listed in the same workbook and does not exist yet;
  * nothing is saved until an official confirms once, then everything is applied in order
    (members -> contributions -> shares -> loans -> repayments -> income -> expenses);
  * one undo reverses the whole workbook, in the opposite order.

Each sheet becomes one ImportBatch; the batches share a `bundle` token."""
import io
import secrets

from app.extensions import db
from app.models import ImportBatch
from app.services import importer
from app.services.importer import ImportFileError, ORDER, SPECS

# sheet-name spellings accepted for each kind (compared case-insensitively, ignoring spaces and punctuation)
SHEET_NAMES = {
    "members": ("members", "member", "memberlist", "memberslist"),
    "contributions": ("contributions", "contribution", "savings", "savingscontributions"),
    "shares": ("shares", "share", "shareholding", "shareholdings"),
    "loans": ("loans", "loan"),
    "repayments": ("repayments", "repayment", "loanrepayments"),
    "income": ("income", "otherincome", "groupincome"),
    "expenses": ("expenses", "expense"),
}
SKIP_SHEETS = {"instructions", "readme", "readme", "guide", "help", "notes", "example", "examples"}
APPLY_HINT = ("Everything was added in order: members, contributions, shares, loans, repayments, income, expenses. "
              "Loan interest and instalment schedules were worked out from each loan's terms.")


def _key(name):
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _kind_for(title):
    k = _key(title)
    for kind, names in SHEET_NAMES.items():
        if k in names:
            return kind
    return None


# ---------------------------------------------------------------- reading
def read_workbook(raw, filename):
    """-> ({kind: (headers, numbered_rows)}, [names of sheets that were ignored]). Only .xlsx can hold several sheets."""
    if not filename.lower().endswith(".xlsx"):
        raise ImportFileError("The full import needs an Excel workbook (.xlsx) with one sheet per kind of record. "
                              "Download the workbook template.")
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise ImportFileError("That file could not be opened as an Excel workbook. Save it as .xlsx and try again.")
    sheets, ignored = {}, []
    for ws in wb.worksheets:
        kind = _kind_for(ws.title)
        if kind is None:
            if _key(ws.title) not in SKIP_SHEETS:
                ignored.append(ws.title)
            continue
        numbered = []
        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            cells = [importer._cell(c) for c in row]
            if any(cells):
                numbered.append((i, cells))
        if not numbered:
            continue
        headers = [importer.norm_header(h) for h in numbered[0][1]]
        body = numbered[1:]
        if not body:                       # headers only: nothing to import from this sheet
            continue
        if len(body) > importer.MAX_ROWS:
            raise ImportFileError(f"The {SPECS[kind]['label']} sheet has {len(body)} rows. Import at most "
                                  f"{importer.MAX_ROWS} rows per sheet.")
        sheets[kind] = (headers, body)
    wb.close()
    return sheets, ignored


# ---------------------------------------------------------------- preview
def build_bundle(group, user, filename, raw, source_file=None):
    """Validate every sheet together and save one preview batch per sheet. Returns (token, batches, ignored)."""
    sheets, ignored = read_workbook(raw, filename)
    if not sheets:
        raise ImportFileError("No data was found. Fill in at least one sheet (Members, Contributions, Shares, Loans, "
                              "Repayments, Income or Expenses) below its header row, using the workbook template.")
    virtual = {"members": {}, "loans": []}        # what earlier sheets will create, for later sheets to refer to
    parsed, problems = [], []
    for kind in ORDER:
        if kind not in sheets:
            continue
        headers, body = sheets[kind]
        try:
            rows = importer.validate(group, kind, headers, body, virtual=virtual, lenient=True)
        except ImportFileError as e:
            problems.append(f"{SPECS[kind]['label']} sheet: {e}")
            continue
        if kind == "members":
            for r in rows:
                if not r["errors"] and not r["d"].get("skip"):
                    virtual["members"][importer.phone_key(r["d"]["phone"])] = r["d"]["full_name"]
        elif kind == "loans":
            refs = set()
            for r in rows:
                d = r["d"]
                if r["errors"]:
                    continue
                ref = (d.get("loan_ref") or "").lower()
                if ref and ref in refs:
                    r["errors"].append(f"loan_ref '{d['loan_ref']}' is used by another loan in this file")
                    continue
                refs.add(ref)
                virtual["loans"].append({"key": f"L{r['n']}", "ref": d.get("loan_ref"), "borrower": d["borrower_name"],
                                         "disb": d["disbursement_date"], "total": d["total_due"], "n": r["n"]})
        parsed.append((kind, rows))
    if problems:
        raise ImportFileError("Nothing was imported. Please fix this in the workbook and upload it again. " +
                              " ".join(problems))
    token = secrets.token_hex(8)
    batches = []
    for kind, rows in parsed:
        b = importer.create_batch(group, user, kind, filename, rows, source_file)
        b.bundle = token
        batches.append(b)
    return token, batches, ignored


# ---------------------------------------------------------------- bundle state
def get_bundle(group_id, token):
    rank = {k: i for i, k in enumerate(ORDER)}
    rows = ImportBatch.query.filter_by(group_id=group_id, bundle=token).all()
    return sorted(rows, key=lambda b: rank.get(b.kind, 99))


def status_of(batches):
    states = {b.status for b in batches}
    for s in ("PREVIEW", "APPLIED", "UNDONE"):
        if s in states:
            return s
    return "CANCELLED"


def apply_bundle(group, user, token):
    batches = get_bundle(group.id, token)
    if not batches or status_of(batches) != "PREVIEW":
        raise ImportFileError("This import has already been handled.")
    applied = {}
    for b in batches:
        if b.status == "PREVIEW":
            applied[b.kind] = importer.apply_batch(group, user, b)
            db.session.flush()
    return applied


def cancel_bundle(group, user, token):
    for b in get_bundle(group.id, token):
        if b.status == "PREVIEW":
            b.status = "CANCELLED"


def undo_bundle(group, user, token, today):
    """Reverse the whole workbook, newest kind first, so loans are undone after their repayments."""
    batches = get_bundle(group.id, token)
    if status_of(batches) != "APPLIED":
        raise ImportFileError("Only an applied import can be undone.")
    kept = []
    for b in reversed(batches):
        if b.status == "APPLIED":
            kept += importer.undo_batch(group, user, b, today)
    return kept


# ---------------------------------------------------------------- template
CHOICES = {  # column -> allowed values, shown as a drop-down in Excel
    "role": "MEMBER,TREASURER,VICE_CHAIR",
    "category@contributions": "MONTHLY,WEEKLY,VOLUNTARY,CUSTOM,OPENING",
    "payment_method": "Cash,Mobile Money,Bank Transfer,Cheque,Other",
    "rate_period": "WEEK,MONTH,YEAR",
    "method": "FLAT,REDUCING",
    "duration_unit": "WEEK,MONTH",
    "frequency": "WEEKLY,MONTHLY",
}
TEXT_COLS = {"phone", "borrower_phone", "reference", "loan_ref"}     # keep leading zeros


def template_workbook():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="142F66")
    wb = Workbook()
    guide = wb.active
    guide.title = "Instructions"
    guide.append(["MY MONEY: full workbook import"])
    guide["A1"].font = Font(bold=True, size=14)
    for line in ("",
                 "Fill in the sheets you need and leave the others empty. You do not need every sheet.",
                 "Type your data under the header row of each sheet. There are no example rows to delete.",
                 "Order of the sheets: Members, Contributions, Shares, Loans, Repayments, Income, Expenses.",
                 "A row may refer to a member or loan listed on an earlier sheet of this same workbook.",
                 "Dates are day/month/year, e.g. 31/12/2025. Amounts are plain numbers, e.g. 50000.",
                 "Loan interest is worked out from each loan's rate and terms; list the money already paid on the Repayments sheet.",
                 "After uploading you see a full preview. Nothing is saved until you confirm, and you can undo everything."):
        guide.append([line])
    guide.append([])
    for kind in ORDER:
        spec = SPECS[kind]
        guide.append([f"{spec['label']} sheet: {spec['help']}"])
        guide.cell(guide.max_row, 1).font = Font(bold=True)
        guide.append(["Column", "Required", "What to enter", "Example"])
        for c in guide[guide.max_row]:
            c.font = Font(bold=True)
        for col, req, help_, ex in spec["columns"]:
            guide.append([col, "Yes" if req else "No", help_, ex])
        guide.append([])
    for col, w in zip("ABCD", (26, 10, 90, 22)):
        guide.column_dimensions[col].width = w
    for kind in ORDER:
        spec = SPECS[kind]
        ws = wb.create_sheet({"members": "Members", "contributions": "Contributions", "shares": "Shares",
                              "loans": "Loans", "repayments": "Repayments", "income": "Income",
                              "expenses": "Expenses"}[kind])
        cols = [c[0] for c in spec["columns"]]
        ws.append(cols)
        ws.freeze_panes = "A2"
        for i, (col, req, help_, _ex) in enumerate(spec["columns"], 1):
            cell = ws.cell(1, i)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center")
            letter = cell.column_letter
            ws.column_dimensions[letter].width = max(16, len(col) + 6)
            if col in TEXT_COLS:
                for r in range(2, 502):
                    ws.cell(r, i).number_format = "@"
            allowed = CHOICES.get(f"{col}@{kind}") or CHOICES.get(col)
            if allowed:
                dv = DataValidation(type="list", formula1=f'"{allowed}"', allow_blank=True, showErrorMessage=False)
                ws.add_data_validation(dv)
                dv.add(f"{letter}2:{letter}501")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
