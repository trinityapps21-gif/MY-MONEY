"""Whole-of-loan summary, available at every stage including after the loan is fully repaid.

    principal             what was lent
    interest              interest on the loan (after any early-settlement rebate)
    repaid                principal repaid + interest repaid (from what was actually paid on the instalments)
    fines                 every late fine charged on this loan over the period of payments, with what was paid or waived
    accruing              fine building up on instalments that are overdue today and not yet charged (0 once repaid)
    total_with_fines      principal + interest + fines (net of waived) + accruing
    balance               total_with_fines - repaid - fines paid

Everything is read from records that already exist (schedule, repayments, fines), so a loan that was repaid years ago shows
exactly what it cost. Informational: nothing here posts to the ledger."""


def _fine_rows(loan, due_by_sched):
    """One row per late fine on the loan, whichever system charged it (group fines, or a lender's per-instalment penalty)."""
    rows = []
    for f in getattr(loan, "fines", None) or []:
        if f.status == "VOID":
            continue
        rows.append({"charged_on": f.paid_on, "due_date": f.deadline, "days_late": f.days_late, "late_amount": f.late_amount,
                     "amount": f.amount, "paid": f.amount if f.status == "PAID" else 0,
                     "waived": f.amount if f.status == "WAIVED" else 0, "settled_on": f.settled_on,
                     "status": {"PAID": "Paid", "WAIVED": "Waived", "UNPAID": "Unpaid"}.get(f.status, f.status.title())})
    for p in getattr(loan, "penalties", None) or []:
        due = due_by_sched.get(p.schedule_id)
        left = p.amount - p.paid - p.waived
        rows.append({"charged_on": p.assessed_on, "due_date": due, "days_late": max(0, (p.assessed_on - due).days) if due else None,
                     "late_amount": None, "amount": p.amount, "paid": p.paid, "waived": p.waived, "settled_on": None,
                     "status": "Unpaid" if left and not p.paid and not p.waived else "Part paid" if left else
                               "Waived" if p.waived and not p.paid else "Paid"})
    rows.sort(key=lambda r: (r["charged_on"], r["amount"]))
    return rows


def summary(loan, today, accruing=0):
    """The summary of one loan, or None if it was never disbursed (nothing has been lent yet)."""
    if not loan.disbursement_tx_id or loan.status in ("DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED"):
        return None
    sched = sorted(loan.schedule, key=lambda s: s.number)
    reps = sorted((r for r in loan.repayments if r.status != "REVERSED"), key=lambda r: (r.payment_date, r.id))
    when = {}                                   # schedule id -> date of the payment that last touched it
    for r in reps:
        for a in r.allocations:
            if a.principal or a.interest:
                when[a.schedule_id] = r.payment_date
    fines = _fine_rows(loan, {s.id: s.due_date for s in sched})
    fine_by_due = {}
    for f in fines:
        if f["due_date"] is not None:
            fine_by_due[f["due_date"]] = fine_by_due.get(f["due_date"], 0) + f["amount"] - f["waived"]
    lines = []
    for s in sched:
        paid_on = when.get(s.id) if s.total_paid else None
        done = s.balance <= 0
        if done:
            late = max(0, (paid_on - s.due_date).days) if paid_on else 0          # how late it was when it was cleared
        else:
            late = max(0, (today - s.due_date).days)                              # how late it is today
        fine = fine_by_due.get(s.due_date, 0)
        if done:
            status = "Paid late" if late > 0 else "Paid on time"
        elif s.total_paid:
            status = "Part paid, overdue" if today > s.due_date else "Part paid"
        else:
            status = "Overdue" if today > s.due_date else "Not yet due"
        lines.append({"number": s.number, "due_date": s.due_date, "principal": s.principal_due, "interest": s.interest_due,
                      "due": s.principal_due + s.interest_due, "paid": s.total_paid, "paid_on": paid_on,
                      "days_late": late, "fine": fine, "status": status, "done": done})
    principal = loan.principal
    interest = sum(s.interest_due for s in sched)
    principal_repaid = sum(s.principal_paid for s in sched)
    interest_repaid = sum(s.interest_paid for s in sched)
    charged = sum(f["amount"] for f in fines)
    waived = sum(f["waived"] for f in fines)
    fines_paid = sum(f["paid"] for f in fines)
    fines_net = charged - waived
    total = principal + interest + fines_net + accruing
    repaid = principal_repaid + interest_repaid
    last_paid = reps[-1].payment_date if reps else None
    finished = loan.status == "FULLY_REPAID"
    return {
        "principal": principal, "interest": interest, "interest_rebate": loan.interest_rebate or 0,
        "total_repayable": principal + interest,
        "principal_repaid": principal_repaid, "interest_repaid": interest_repaid, "repaid": repaid,
        "fines": fines, "fines_charged": charged, "fines_waived": waived, "fines_paid": fines_paid,
        "fines_unpaid": fines_net - fines_paid, "accruing": accruing,
        "total_with_fines": total, "total_paid": repaid + fines_paid, "balance": total - repaid - fines_paid,
        "lines": lines, "finished": finished, "started": loan.disbursement_date, "last_payment": last_paid,
        "days_taken": (last_paid - loan.disbursement_date).days if finished and last_paid else None,
        "late_instalments": sum(1 for x in lines if x["days_late"] > 0), "instalments": len(lines),
        "fine_pct_of_loan": round(fines_net * 100 / principal, 1) if principal and fines_net else 0,
    }
