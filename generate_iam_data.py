"""
Synthetic IAM dataset generator for the Access Review Dashboard project.

Creates seven CSV tables that mimic a real identity environment (HR feed,
AD accounts, applications, entitlements, SoD rules, certification campaigns,
access tickets), with realistic problems planted in them. It also writes an
answer key so you can check that your SQL queries find every planted issue.

All data is fake. Never mix in real employer or client data.

Usage:
    pip install pandas faker
    python generate_iam_data.py            # writes CSVs to ./iam_data
    python generate_iam_data.py --out data --employees 2000
"""

import argparse
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from faker import Faker

SEED = 42
REF_DATE = date(2026, 10, 29)  # "today" for the dataset, fixed so results repeat

DEPARTMENTS = ["Finance", "Operations", "Technology", "Risk", "HR",
               "Sales", "Compliance", "Customer Service"]

# app_name: (criticality, roles)
APPLICATIONS = {
    "Payments Hub":      ("High",   ["Payments-Read", "Payments-Create", "Payments-Approve"]),
    "Vendor Master":     ("High",   ["Vendor-Read", "Vendor-Create", "Vendor-Approve"]),
    "General Ledger":    ("High",   ["GL-Read", "GL-Post", "GL-Admin"]),
    "Core Banking":      ("High",   ["CB-Read", "CB-Operator", "CB-Admin"]),
    "Loan Origination":  ("High",   ["Loan-Read", "Loan-Submit", "Loan-Approve"]),
    "HR Portal":         ("Medium", ["HR-Read", "HR-Edit", "HR-Admin"]),
    "CRM":               ("Medium", ["CRM-Read", "CRM-Edit"]),
    "Treasury":          ("High",   ["Treasury-Read", "Treasury-Trade", "Treasury-Settle"]),
    "Reporting Portal":  ("Low",    ["Report-Viewer", "Report-Author"]),
    "Document Store":    ("Low",    ["Docs-Read", "Docs-Write"]),
    "Ticketing":         ("Low",    ["Ticket-User", "Ticket-Agent"]),
    "Data Warehouse":    ("Medium", ["DW-Read", "DW-Write", "DW-Admin"]),
    "Email Gateway":     ("Medium", ["Mail-User", "Mail-Admin"]),
    "Cloud Console":     ("High",   ["Cloud-Read", "Cloud-Contributor", "Cloud-Owner"]),
    "Firewall Manager":  ("High",   ["FW-Read", "FW-Change", "FW-Approve"]),
}

# Pairs of roles one person must never hold together
SOD_RULES = [
    ("Payments-Create", "Payments-Approve"),
    ("Vendor-Create", "Vendor-Approve"),
    ("Loan-Submit", "Loan-Approve"),
    ("Treasury-Trade", "Treasury-Settle"),
    ("FW-Change", "FW-Approve"),
]

PRIVILEGED_ROLES = {"GL-Admin", "CB-Admin", "HR-Admin", "DW-Admin",
                    "Mail-Admin", "Cloud-Owner", "FW-Approve"}

TICKET_SLA_HOURS = {"Provision": 48, "Modify": 48, "Deprovision": 24,
                    "Password Reset": 4}


def rand_date(start, end):
    return start + timedelta(days=random.randint(0, (end - start).days))


def rand_ts(start, end):
    span = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, span))


# ---------------------------------------------------------------- HR feed
def build_hr(fake, n):
    rows = []
    for i in range(1, n + 1):
        hire = rand_date(date(2015, 1, 1), date(2026, 6, 30))
        terminated = random.random() < 0.10
        term = rand_date(max(hire, date(2025, 1, 1)), REF_DATE - timedelta(days=5)) \
            if terminated else None
        rows.append({
            "emp_id": f"E{i:05d}",
            "name": fake.name(),
            "department": random.choice(DEPARTMENTS),
            "manager_id": None,
            "hire_date": hire,
            "status": "Terminated" if terminated else "Active",
            "termination_date": term,
        })
    hr = pd.DataFrame(rows)
    active_ids = hr.loc[hr.status == "Active", "emp_id"].tolist()
    managers = random.sample(active_ids, max(10, n // 25))
    hr["manager_id"] = [random.choice([m for m in managers if m != e] or managers)
                        for e in hr.emp_id]
    return hr, managers


# ---------------------------------------------------------------- AD accounts
def build_accounts(hr):
    rows = []
    acct_no = 1

    def new_account(emp_id, enabled, last_logon, privileged):
        nonlocal acct_no
        rows.append({
            "account_id": f"U{acct_no:05d}",
            "emp_id": emp_id,
            "enabled": int(enabled),
            "last_logon": last_logon,
            "is_privileged": int(privileged),
        })
        acct_no += 1

    for e in hr.itertuples():
        privileged = random.random() < 0.08
        if e.status == "Terminated":
            # PLANTED: about 30% of leavers keep an enabled account
            enabled = random.random() < 0.30
            last = rand_ts(datetime.combine(e.termination_date, datetime.min.time())
                           - timedelta(days=60),
                           datetime.combine(e.termination_date, datetime.min.time()))
        else:
            enabled = True
            # PLANTED: about 6% of active users are dormant (90+ days)
            if random.random() < 0.06:
                last = rand_ts(datetime(2025, 9, 1), datetime(2026, 7, 15))
            else:
                last = rand_ts(datetime(2026, 8, 15), datetime(2026, 10, 28))
        new_account(e.emp_id, enabled, last, privileged)

    # PLANTED: duplicate accounts for a few active employees (data-quality issue)
    for emp in hr[hr.status == "Active"].sample(6, random_state=SEED).emp_id:
        new_account(emp, True, rand_ts(datetime(2026, 8, 1), datetime(2026, 10, 28)), False)

    # PLANTED: orphaned accounts whose emp_id is not in the HR feed
    for k in range(1, 26):
        new_account(f"E9{k:04d}", random.random() < 0.8,
                    rand_ts(datetime(2025, 6, 1), datetime(2026, 10, 28)),
                    random.random() < 0.2)

    return pd.DataFrame(rows)


# ---------------------------------------------------------------- applications
def build_apps(managers):
    rows = []
    for i, (name, (crit, _)) in enumerate(APPLICATIONS.items(), start=1):
        rows.append({"app_id": f"A{i:03d}", "app_name": name,
                     "owner_emp_id": random.choice(managers), "criticality": crit})
    apps = pd.DataFrame(rows)
    # PLANTED: two applications with no owner (data-quality issue)
    apps.loc[apps.app_name.isin(["Document Store", "Email Gateway"]), "owner_emp_id"] = None
    return apps


# ---------------------------------------------------------------- entitlements
def build_entitlements(accounts, apps):
    app_by_name = dict(zip(apps.app_name, apps.app_id))
    rows = []
    seen = set()

    def grant(acct, app_name, role, granted):
        key = (acct, role)
        if key in seen:
            return
        seen.add(key)
        rows.append({"account_id": acct, "app_id": app_by_name[app_name],
                     "role": role, "granted_date": granted})

    for a in accounts.itertuples():
        for app_name in random.sample(list(APPLICATIONS), random.randint(1, 4)):
            roles = APPLICATIONS[app_name][1]
            # mostly the lowest role, sometimes higher
            role = roles[0] if random.random() < 0.6 else random.choice(roles)
            if role in PRIVILEGED_ROLES and not a.is_privileged:
                role = roles[0]
            grant(a.account_id, app_name, role,
                  rand_date(date(2020, 1, 1), date(2026, 9, 30)))

    # PLANTED: SoD conflicts on 18 enabled accounts
    role_app = {r: n for n, (_, rs) in APPLICATIONS.items() for r in rs}
    enabled = accounts[accounts.enabled == 1].account_id.tolist()
    for acct in random.sample(enabled, 18):
        ra, rb = random.choice(SOD_RULES)
        for r in (ra, rb):
            grant(acct, role_app[r], r, rand_date(date(2023, 1, 1), date(2026, 9, 30)))

    ent = pd.DataFrame(rows)
    ent.insert(0, "entitlement_id", [f"ENT{i:06d}" for i in range(1, len(ent) + 1)])

    # PLANTED: a few grant dates in the future (data-quality issue)
    bad = ent.sample(5, random_state=SEED).index
    ent.loc[bad, "granted_date"] = [REF_DATE + timedelta(days=random.randint(10, 200))
                                    for _ in bad]
    return ent


# ---------------------------------------------------------------- certifications
def build_certifications(ent, accounts, hr):
    acct_emp = dict(zip(accounts.account_id, accounts.emp_id))
    emp_mgr = dict(zip(hr.emp_id, hr.manager_id))
    campaigns = [("Q1-2026", date(2026, 3, 1), date(2026, 3, 31)),
                 ("Q2-2026", date(2026, 6, 1), date(2026, 6, 30)),
                 ("Q3-2026", date(2026, 9, 15), date(2026, 10, 31))]
    rows = []
    for idx, (name, start, due) in enumerate(campaigns):
        eligible = ent[pd.to_datetime(ent.granted_date) < pd.Timestamp(start)]
        sample = eligible.sample(frac=0.55, random_state=SEED + idx)
        in_progress = due >= REF_DATE
        for e in sample.itertuples():
            reviewer = emp_mgr.get(acct_emp[e.account_id], "UNASSIGNED")
            r = random.random()
            if in_progress and r < 0.40 or not in_progress and r < 0.04:
                decision, decided = "Pending", None   # open or never closed
            else:
                decision = "Revoked" if random.random() < 0.07 else "Approved"
                # most on time, about 12% after the due date
                late = random.random() < 0.12
                hi = min(due + timedelta(days=25), REF_DATE) if late else min(due, REF_DATE)
                lo = due + timedelta(days=1) if late and hi > due else start
                decided = rand_date(lo, hi)
            rows.append({"campaign": name, "entitlement_id": e.entitlement_id,
                         "account_id": e.account_id, "app_id": e.app_id,
                         "role": e.role, "reviewer_emp_id": reviewer,
                         "campaign_start": start, "due_date": due,
                         "decision": decision, "decision_date": decided})
    cert = pd.DataFrame(rows)
    cert.insert(0, "cert_item_id", [f"C{i:06d}" for i in range(1, len(cert) + 1)])

    # PLANTED: decisions dated before the campaign started (data-quality issue)
    decided_idx = cert[cert.decision != "Pending"].sample(4, random_state=SEED).index
    cert.loc[decided_idx, "decision_date"] = (
        pd.to_datetime(cert.loc[decided_idx, "campaign_start"]) - pd.Timedelta(days=20)).dt.date
    return cert


# ---------------------------------------------------------------- tickets
def build_tickets(n=2500):
    rows = []
    start, end = datetime(2026, 1, 1), datetime(2026, 10, 28, 18)
    for i in range(1, n + 1):
        ttype = random.choices(list(TICKET_SLA_HOURS), weights=[35, 25, 15, 25])[0]
        sla = TICKET_SLA_HOURS[ttype]
        opened = rand_ts(start, end)
        if (end - opened).days < 3 and random.random() < 0.5:
            closed = None  # still open
        else:
            # about 10% breach the SLA
            factor = random.uniform(1.05, 3.0) if random.random() < 0.10 \
                else random.uniform(0.05, 0.95)
            closed = opened + timedelta(hours=sla * factor)
        rows.append({"ticket_id": f"T{i:06d}", "request_type": ttype,
                     "opened_at": opened, "closed_at": closed, "sla_hours": sla})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- answer key
def build_answer_key(hr, accounts, apps, ent, cert, tickets):
    """Computed from the final data, so it matches exactly what your SQL should find."""
    key = []

    def add(kind, rid, detail=""):
        key.append({"anomaly_type": kind, "record_id": rid, "detail": detail})

    m = accounts.merge(hr, on="emp_id", how="left")
    for r in m[(m.status == "Terminated") & (m.enabled == 1)].itertuples():
        add("Terminated user with enabled account", r.account_id,
            f"{r.emp_id} left {r.termination_date}")
    for r in m[m.status.isna()].itertuples():
        add("Orphaned account (no HR record)", r.account_id, r.emp_id)

    cutoff = pd.Timestamp(REF_DATE - timedelta(days=90))
    dormant = accounts[(accounts.enabled == 1) & (pd.to_datetime(accounts.last_logon) < cutoff)]
    for r in dormant.itertuples():
        add("Dormant enabled account (90+ days)", r.account_id,
            "privileged" if r.is_privileged else "standard")

    dup = accounts.dropna(subset=["emp_id"])
    for emp, g in dup[dup.duplicated("emp_id", keep=False)].groupby("emp_id"):
        add("Duplicate accounts for one employee", emp, ", ".join(g.account_id))

    roles = ent.groupby("account_id")["role"].apply(set)
    for acct, rs in roles.items():
        for a, b in SOD_RULES:
            if a in rs and b in rs:
                add("SoD conflict", acct, f"{a} + {b}")

    for r in apps[apps.owner_emp_id.isna()].itertuples():
        add("Application with no owner", r.app_id, r.app_name)

    for r in ent[pd.to_datetime(ent.granted_date) > pd.Timestamp(REF_DATE)].itertuples():
        add("Grant date in the future", r.entitlement_id, str(r.granted_date))

    c = cert.copy()
    c["decision_date"] = pd.to_datetime(c.decision_date)
    for r in c[c.decision_date < pd.to_datetime(c.campaign_start)].itertuples():
        add("Decision dated before campaign start", r.cert_item_id, r.campaign)
    overdue = c[(c.decision == "Pending") & (pd.to_datetime(c.due_date) < pd.Timestamp(REF_DATE))]
    for r in overdue.itertuples():
        add("Certification overdue (pending past due date)", r.cert_item_id, r.campaign)

    t = tickets.dropna(subset=["closed_at"]).copy()
    hours = (pd.to_datetime(t.closed_at) - pd.to_datetime(t.opened_at)).dt.total_seconds() / 3600
    for r in t[hours > t.sla_hours].itertuples():
        add("Ticket SLA breach", r.ticket_id, r.request_type)

    return pd.DataFrame(key)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="iam_data", help="output folder")
    p.add_argument("--employees", type=int, default=1000)
    args = p.parse_args()

    random.seed(SEED)
    fake = Faker("en_IN")
    Faker.seed(SEED)

    hr, managers = build_hr(fake, args.employees)
    accounts = build_accounts(hr)
    apps = build_apps(managers)
    ent = build_entitlements(accounts, apps)
    sod = pd.DataFrame(SOD_RULES, columns=["role_a", "role_b"])
    cert = build_certifications(ent, accounts, hr)
    tickets = build_tickets()
    key = build_answer_key(hr, accounts, apps, ent, cert, tickets)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tables = {"hr_employees": hr, "ad_accounts": accounts, "applications": apps,
              "entitlements": ent, "sod_rules": sod, "certifications": cert,
              "access_tickets": tickets}
    for name, df in tables.items():
        df.to_csv(out / f"{name}.csv", index=False)
        print(f"{name:<15} {len(df):>6} rows")

    key.to_csv(out / "answer_key.csv", index=False)
    print("\nPlanted issues (answer_key.csv):")
    for kind, n in key.anomaly_type.value_counts().items():
        print(f"  {n:>4}  {kind}")
    print(f"\nFiles written to {out.resolve()}")


if __name__ == "__main__":
    main()
