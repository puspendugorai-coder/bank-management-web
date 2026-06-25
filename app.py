from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from supabase import create_client, Client
import os, re, io, random, csv
from datetime import datetime
from dotenv import load_dotenv
import bcrypt

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Load variables from the local .env file
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "temporary-local-dev-string-123")


@app.context_processor
def inject_session_globals():
    """Makes session.cashier_id available in every template as session_cashier_id."""
    return {"session_cashier_id": session.get("cashier_id")}


# --- SUPABASE CONFIG ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing Supabase Environmental variables! Check your configuration keys.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─────────────────────────────────────────
# PORTAL ACCESS CODES (staff portals only)
# ─────────────────────────────────────────
PORTAL_CODES = {
    "manager":  "1122",
    "cashier":  "7890",
    "cse":      "2508",
    "loan":     "7206",
}

PORTAL_LABELS = {
    "manager":  "Manager",
    "cashier":  "Cashier",
    "cse":      "Customer Service Employee",
    "loan":     "Loan Manager",
}

# Cash drawer denominations the cashier portal tracks
DENOMINATIONS = [10, 20, 50, 100, 200, 500]

# Cashier transactions at or above this amount require Manager approval before completing
HIGH_VALUE_THRESHOLD = 50000.0

# Default per-account transaction limits (can be changed by the customer)
DEFAULT_DOMESTIC_LIMIT = 100000.0
DEFAULT_INTERNATIONAL_LIMIT = 50000.0

# Maximum number of accounts one identity (Aadhar No) may hold
MAX_ACCOUNTS_PER_IDENTITY = 3

# Spend Analytics: map raw transaction types to friendly category buckets
TRANSACTION_CATEGORY_MAP = {
    "CREDIT": "Deposits & Credits",
    "DEBIT": "Withdrawals & Spending",
    "TRANSFER_OUT": "Transfers Sent",
    "TRANSFER_IN": "Transfers Received",
    "EMI_PAYMENT": "Loan EMI Payments",
    "EMI_DEDUCTION": "Loan EMI Payments",
}


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def hash_secret(raw: str) -> str:
    return bcrypt.hashpw(str(raw).encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_secret(raw: str, hashed: str) -> bool:
    if not raw or not hashed:
        return False
    try:
        return bcrypt.checkpw(str(raw).encode("utf-8"), str(hashed).encode("utf-8"))
    except ValueError:
        # Stored value isn't a valid bcrypt hash (e.g. legacy plaintext row)
        return False


def generate_account_number() -> str:
    """10-digit account number, prefixed 5 so it never starts with 0."""
    return "5" + "".join(str(random.randint(0, 9)) for _ in range(9))


def generate_card_and_pin():
    card = "".join(str(random.randint(0, 9)) for _ in range(12))
    pin = "".join(str(random.randint(0, 9)) for _ in range(4))
    return card, pin


def months_from_input(years_str, months_str) -> int:
    years = int(years_str) if years_str else 0
    months = int(months_str) if months_str else 0
    return (years * 12) + months


def calculate_emi(principal: float, annual_rate: float, tenure_months: int):
    """
    Simple-interest EMI per the spec:
    total_interest = (P * R * T) / 100   where T is in YEARS
    total_payable  = P + total_interest
    emi             = total_payable / tenure_months
    """
    tenure_years = tenure_months / 12.0
    total_interest = (principal * annual_rate * tenure_years) / 100.0
    total_payable = principal + total_interest
    emi = total_payable / tenure_months if tenure_months else 0
    return round(total_interest, 2), round(total_payable, 2), round(emi, 2)


def split_emi_payment(loan: dict, pay_amount: float):
    """
    Splits a repayment amount into its principal and interest portions, based on the loan's
    fixed principal/interest ratio (simple interest, so this ratio is constant for the
    whole loan). Returns (principal_portion, interest_portion).
    """
    total_payable = float(loan.get("Total_Payable") or 0)
    loan_amount = float(loan.get("Loan_Amount") or 0)
    if total_payable <= 0:
        return 0.0, pay_amount
    principal_fraction = loan_amount / total_payable
    principal_portion = round(pay_amount * principal_fraction, 2)
    interest_portion = round(pay_amount - principal_portion, 2)
    return principal_portion, interest_portion


def get_outstanding_loan_principal(account_no: str) -> float:
    """
    Returns the sum of Outstanding_Principal across every ACTIVE loan on this account.
    This is the "withdrawal cushion" — the portion of the customer's balance that is still
    loan money which already left the bank's vault at disbursement, so withdrawing it
    should NOT deduct from the vault a second time.
    """
    try:
        loans = supabase.table("loans").select("*").eq("Account_No", account_no).eq("Status", "ACTIVE").execute().data or []
    except Exception:
        loans = []
    return sum(float(l.get("Outstanding_Principal") or 0) for l in loans)


def log_transaction(account_no, txn_type, amount, balance_after, description, performed_by):
    try:
        supabase.table("transactions").insert({
            "Account_No": account_no,
            "Type": txn_type,
            "Amount": amount,
            "Balance_After": balance_after,
            "Description": description,
            "Performed_By": performed_by,
            "Created_At": datetime.utcnow().isoformat(),
        }).execute()
    except Exception as e:
        print("Transaction log failed:", e)


def get_customer_by_account(account_no):
    res = supabase.table("registration").select("*").eq("Account_No", account_no).execute()
    return res.data[0] if res.data else None


def require_role(role):
    return session.get("role") == role


# ─────────────────────────────────────────
# BANK VAULT — central cash reserve (separate from the sum of customer balances)
# ─────────────────────────────────────────
BANK_VAULT_ROW_ID = 1
BANK_VAULT_OPENING_BALANCE = 1000000.0  # ₹10,00,000 starting reserve


def get_bank_cash() -> float:
    """
    Returns the bank's central cash reserve. Creates the single tracking row with the
    opening balance the first time this is called (e.g. on a brand-new database).
    Returns None if the `bank_vault` table itself doesn't exist yet (a setup step the
    admin still needs to do in Supabase) — callers can check for None to show a warning.
    """
    try:
        res = supabase.table("bank_vault").select("*").eq("id", BANK_VAULT_ROW_ID).execute()
        if res.data:
            return float(res.data[0]["Cash_Balance"])
        # Row doesn't exist yet — initialize it
        supabase.table("bank_vault").insert({
            "id": BANK_VAULT_ROW_ID,
            "Cash_Balance": BANK_VAULT_OPENING_BALANCE,
        }).execute()
        return BANK_VAULT_OPENING_BALANCE
    except Exception as e:
        print("get_bank_cash failed (likely missing 'bank_vault' table):", e)
        return None


def adjust_bank_cash(delta: float) -> float:
    """
    Adds (or subtracts, if delta is negative) `delta` from the bank's cash reserve.
    Returns the new balance, or None if the `bank_vault` table doesn't exist yet.
    Loan disbursement should NOT call this — granting a loan converts vault cash into a
    loan asset, it doesn't remove it from the bank's books.
    """
    current = get_bank_cash()
    if current is None:
        return None
    new_balance = current + delta
    try:
        supabase.table("bank_vault").update({"Cash_Balance": new_balance}).eq("id", BANK_VAULT_ROW_ID).execute()
    except Exception as e:
        print("adjust_bank_cash failed:", e)
    return new_balance


# ─────────────────────────────────────────
# CASHIER DRAWER — per-cashier denomination counts
# ─────────────────────────────────────────
def get_cashier_drawer(cashier_id: str):
    """Returns the denomination counts for a given cashier, creating an empty drawer if needed."""
    try:
        res = supabase.table("cashier_drawers").select("*").eq("Cashier_ID", cashier_id).execute()
        if res.data:
            return res.data[0]
        new_row = {"Cashier_ID": cashier_id}
        for d in DENOMINATIONS:
            new_row[f"Note_{d}"] = 0
        supabase.table("cashier_drawers").insert(new_row).execute()
        return new_row
    except Exception as e:
        print("get_cashier_drawer failed (likely missing 'cashier_drawers' table):", e)
        return None


def adjust_cashier_drawer(cashier_id: str, denomination_changes: dict):
    """
    denomination_changes: {10: +5, 100: -2, ...} — positive = notes added to drawer,
    negative = notes removed from drawer. Returns the updated drawer row, or None on failure.
    """
    drawer = get_cashier_drawer(cashier_id)
    if drawer is None:
        return None
    update_payload = {}
    for denom, change in denomination_changes.items():
        col = f"Note_{denom}"
        current_count = int(drawer.get(col, 0) or 0)
        update_payload[col] = current_count + change
    try:
        supabase.table("cashier_drawers").update(update_payload).eq("Cashier_ID", cashier_id).execute()
    except Exception as e:
        print("adjust_cashier_drawer failed:", e)
        return None
    drawer.update(update_payload)
    return drawer


# ─────────────────────────────────────────
# PENDING TRANSACTIONS — high-value cashier ops awaiting Manager approval
# ─────────────────────────────────────────
def create_pending_transaction(account_no, txn_type, amount, cashier_name, denomination_breakdown=None):
    import json as _json
    payload = {
        "Account_No": account_no,
        "Type": txn_type,
        "Amount": amount,
        "Cashier_Name": cashier_name,
        "Status": "PENDING",
        "Denomination_Breakdown": _json.dumps(denomination_breakdown or {}),
        "Requested_At": datetime.utcnow().isoformat(),
    }
    try:
        supabase.table("pending_transactions").insert(payload).execute()
        return True
    except Exception as e:
        print("create_pending_transaction failed (likely missing 'pending_transactions' table):", e)
        return False


# ─────────────────────────────────────────
# MULTI-ACCOUNT HELPERS
# ─────────────────────────────────────────
def get_accounts_for_identity(aadhar_no: str):
    """Returns every account in the bank registered under the same Aadhar No."""
    try:
        res = supabase.table("registration").select("*").eq("Aadhar_No", aadhar_no).execute()
        return res.data or []
    except Exception:
        return []


# ─────────────────────────────────────────
# HOME → PORTAL SELECTOR
# ─────────────────────────────────────────
@app.route("/")
def home():
    return render_template("portal_select.html")


# ─────────────────────────────────────────
# STAFF LOGIN (Manager / Cashier / CSE / Loan Manager) — code only
# ─────────────────────────────────────────
@app.route("/staff-login/<portal>", methods=["GET", "POST"])
def staff_login(portal):
    if portal not in PORTAL_CODES:
        flash("Unknown portal.", "error")
        return redirect(url_for("home"))

    if request.method == "POST":
        code = request.form.get("code", "")
        cashier_name = request.form.get("cashier_name", "").strip()

        if portal == "cashier" and not cashier_name:
            flash("Please enter your Cashier Name/ID before logging in.", "error")
            return redirect(url_for("staff_login", portal=portal))

        if code == PORTAL_CODES[portal]:
            session.clear()
            session["role"] = portal
            session["staff_label"] = PORTAL_LABELS[portal]
            if portal == "cashier":
                session["cashier_id"] = cashier_name
            if portal == "cse":
                return redirect(url_for("cse_menu"))
            return redirect(url_for(f"{portal}_dashboard"))
        else:
            flash("Incorrect access code.", "error")
            return redirect(url_for("staff_login", portal=portal))

    return render_template("staff_login.html", portal=portal, label=PORTAL_LABELS[portal])


# ─────────────────────────────────────────
# CUSTOMER LOGIN — Email + Bank_ID
# ─────────────────────────────────────────
@app.route("/customer-login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        bank_id = request.form.get("bank_id", "").strip()

        if not email or not bank_id:
            flash("Please enter both Email and Bank ID.", "error")
            return redirect(url_for("customer_login"))

        try:
            res = supabase.table("registration").select("*") \
                .ilike("Email", email).eq("Bank_ID", bank_id).execute()
            if res.data:
                customer = res.data[0]
                session.clear()
                session["role"] = "customer"
                session["account_no"] = customer["Account_No"]
                session["customer_name"] = customer.get("Name", "")
                return redirect(url_for("customer_dashboard"))
            else:
                flash("You are not registered with us. Please visit a branch to register.", "error")
        except Exception as e:
            flash(f"Database error: {e}", "error")

    return render_template("customer_login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ═════════════════════════════════════════
# CSE PORTAL — Client Onboarding (formerly "dashboard")
# ═════════════════════════════════════════
@app.route("/cse")
def cse_menu():
    """CSE landing page: choose between registering a new customer or fetching an existing one."""
    if not require_role("cse"):
        flash("Please log in to the CSE portal first.", "error")
        return redirect(url_for("staff_login", portal="cse"))
    return render_template("cse_menu.html", label=session.get("staff_label"))


@app.route("/cse/register")
def cse_dashboard():
    if not require_role("cse"):
        flash("Please log in to the CSE portal first.", "error")
        return redirect(url_for("staff_login", portal="cse"))

    try:
        res = supabase.table("registration").select("*").order("Created_At", desc=True).execute()
        records = res.data
    except Exception:
        records = []

    return render_template("cse_dashboard.html", records=records, label=session.get("staff_label"))


@app.route("/cse/fetch", methods=["GET", "POST"])
def cse_fetch_customer():
    """CSE looks up an existing customer by account number and views/downloads their details."""
    if not require_role("cse"):
        flash("Please log in to the CSE portal first.", "error")
        return redirect(url_for("staff_login", portal="cse"))

    customer = None
    other_accounts = []
    loans = []

    if request.method == "POST":
        account_no = request.form.get("account_no", "").strip()
        customer = get_customer_by_account(account_no)
        if not customer:
            flash("No customer found with that account number.", "error")
        else:
            if customer.get("Aadhar_No"):
                other_accounts = [
                    a for a in get_accounts_for_identity(customer["Aadhar_No"])
                    if a["Account_No"] != account_no
                ]
            try:
                loans = supabase.table("loans").select("*").eq("Account_No", account_no).execute().data or []
            except Exception:
                loans = []

    return render_template("cse_fetch_customer.html", label=session.get("staff_label"),
                            customer=customer, other_accounts=other_accounts, loans=loans)


@app.route("/cse/add", methods=["POST"])
def cse_add_record():
    if not require_role("cse"):
        return redirect(url_for("staff_login", portal="cse"))

    email    = request.form.get("email", "").strip()
    age      = request.form.get("age", "0")
    amount   = request.form.get("amount", "0")
    pan      = request.form.get("pan_no", "").strip()
    bank_id  = request.form.get("bank_id", "").strip()
    pin      = request.form.get("pin_no", "")
    card     = request.form.get("card_no", "")
    confirm_card = request.form.get("confirm_card_no", "")
    confirm_pin  = request.form.get("confirm_pin_no", "")
    contact  = request.form.get("contact", "")

    if not card or not card.isdigit() or len(card) != 12:
        flash("Card number must be auto-generated (click the Generate button).", "error")
        return redirect(url_for("cse_dashboard"))
    if card != confirm_card:
        flash("Card numbers do not match.", "error")
        return redirect(url_for("cse_dashboard"))
    if not pin or not pin.isdigit() or len(pin) != 4:
        flash("PIN must be auto-generated (click the Generate button).", "error")
        return redirect(url_for("cse_dashboard"))
    if pin != confirm_pin:
        flash("PIN numbers do not match.", "error")
        return redirect(url_for("cse_dashboard"))

    email_pattern = r'^[a-z0-9]+[\._]?[a-z0-9]+[@]\w+[.]\w{2,3}$'
    if not re.search(email_pattern, email):
        flash("Invalid email format.", "error")
        return redirect(url_for("cse_dashboard"))
    if int(age) < 18:
        flash("Age must be 18 or above.", "error")
        return redirect(url_for("cse_dashboard"))
    if int(amount) < 1000 or int(amount) > 1000000:
        flash("Amount must be between ₹1,000 and ₹10,00,000.", "error")
        return redirect(url_for("cse_dashboard"))
    if pan.isdigit() or bank_id.isdigit():
        flash("Pan No and Bank ID must be alphanumeric.", "error")
        return redirect(url_for("cse_dashboard"))
    if len(bank_id) > 6 or not bank_id.isalnum():
        flash("Bank ID must be alphanumeric and max 6 characters.", "error")
        return redirect(url_for("cse_dashboard"))
    if len(contact) != 10 or not contact.isdigit():
        flash("Contact number must be exactly 10 digits.", "error")
        return redirect(url_for("cse_dashboard"))

    aadhar_no = request.form.get("aadhar_no", "").strip()
    existing_accounts = get_accounts_for_identity(aadhar_no) if aadhar_no else []
    if len(existing_accounts) >= MAX_ACCOUNTS_PER_IDENTITY:
        flash(f"This identity (Aadhar No {aadhar_no}) already holds the maximum of "
              f"{MAX_ACCOUNTS_PER_IDENTITY} accounts. A new account cannot be opened for them.", "error")
        return redirect(url_for("cse_dashboard"))

    account_no = generate_account_number()

    data = {
        "Account_No": account_no,
        "Name":      request.form.get("name"),
        "Address":   request.form.get("address"),
        "Email":     email,
        "Age":       age,
        "City":      request.form.get("city"),
        "Country":   request.form.get("country"),
        "Aadhar_No": request.form.get("aadhar_no"),
        "Pan_No":    pan,
        "Contact":   contact,
        "Card_No":   card,   # real card number — needed by external ATM systems / lookups
        "Pin_Hash":  hash_secret(pin),
        "Bank_ID":   bank_id,
        "Amount":    amount,
        "Balance":   amount,
        "Gender":    request.form.get("gender"),
        "Created_At": datetime.utcnow().isoformat(),
    }

    try:
        supabase.table("registration").insert(data).execute()
        log_transaction(account_no, "CREDIT", float(amount), float(amount),
                         "Initial deposit at account opening", session.get("staff_label", "CSE"))
        adjust_bank_cash(float(amount))  # the customer's opening deposit becomes bank cash
        flash(f"Account created! Account No: {account_no} — Card: {card} — PIN: {pin} "
              f"(share these with the customer now, they will not be shown again).", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("cse_dashboard"))


@app.route("/cse/update", methods=["POST"])
def cse_update_record():
    if not require_role("cse"):
        return redirect(url_for("staff_login", portal="cse"))

    account_no = request.form.get("account_no", "")
    if not account_no:
        flash("No account selected to update.", "error")
        return redirect(url_for("cse_dashboard"))

    bank_id = request.form.get("bank_id", "")
    amount  = request.form.get("amount", "0")

    if int(amount) < 1000 or int(amount) > 1000000:
        flash("Amount must be between ₹1,000 and ₹10,00,000.", "error")
        return redirect(url_for("cse_dashboard"))
    if len(bank_id) > 6 or not bank_id.isalnum() or bank_id.isdigit():
        flash("Bank ID must be alphanumeric (with at least one letter), max 6 characters.", "error")
        return redirect(url_for("cse_dashboard"))

    data = {
        "Name":      request.form.get("name"),
        "Address":   request.form.get("address"),
        "Email":     request.form.get("email"),
        "Age":       request.form.get("age"),
        "City":      request.form.get("city"),
        "Country":   request.form.get("country"),
        "Contact":   request.form.get("contact"),
        "Bank_ID":   bank_id,
        "Gender":    request.form.get("gender"),
        # Aadhar_No, Pan_No, Card_No, Pin_Hash, Account_No, Amount/Balance excluded —
        # KYC identity fields are locked after creation and not editable here.
    }

    try:
        supabase.table("registration").update(data).eq("Account_No", account_no).execute()
        flash("Record updated successfully!", "success")
    except Exception as e:
        err_text = str(e)
        if "23503" in err_text:
            flash("Update blocked by a database link to this customer's loan/transaction records. "
                  "If this persists, contact your administrator.", "error")
        else:
            flash(f"Error: {e}", "error")

    return redirect(url_for("cse_dashboard"))


@app.route("/cse/delete/<account_no>")
def cse_delete_record(account_no):
    if not require_role("cse"):
        return redirect(url_for("staff_login", portal="cse"))

    try:
        loans = supabase.table("loans").select("*").eq("Account_No", account_no).execute().data or []
    except Exception:
        loans = []

    active_loans = [l for l in loans if l.get("Status") == "ACTIVE"]
    if active_loans:
        flash("Cannot delete this customer — they have an ACTIVE loan on record. "
              "The loan must be fully paid off (or closed) before this account can be removed.", "error")
        return redirect(url_for("cse_dashboard"))

    try:
        # All loans (if any) are CLOSED — safe to clear their history along with the customer.
        # Closed-loan history for a removed customer no longer needs to be queryable on its own.
        if loans:
            supabase.table("loans").delete().eq("Account_No", account_no).execute()
        supabase.table("transactions").delete().eq("Account_No", account_no).execute()
        supabase.table("registration").delete().eq("Account_No", account_no).execute()
        flash("Record deleted.", "success")
    except Exception as e:
        err_text = str(e)
        if "23503" in err_text:
            flash("Cannot delete this customer — a database link still references this account. "
                  "Contact your administrator.", "error")
        else:
            flash(f"Error: {e}", "error")

    return redirect(url_for("cse_dashboard"))


# ═════════════════════════════════════════
# MANAGER PORTAL — Overview / Analytics
# ═════════════════════════════════════════
@app.route("/manager")
def manager_dashboard():
    if not require_role("manager"):
        flash("Please log in to the Manager portal first.", "error")
        return redirect(url_for("staff_login", portal="manager"))

    try:
        customers = supabase.table("registration").select("*").execute().data or []
    except Exception:
        customers = []

    try:
        loans = supabase.table("loans").select("*").execute().data or []
    except Exception:
        loans = []

    total_customers = len(customers)
    total_cash = get_bank_cash()
    vault_missing = total_cash is None
    if vault_missing:
        total_cash = 0.0
    total_customer_deposits = sum(float(c.get("Balance") or 0) for c in customers)
    total_loans_disbursed = sum(float(l.get("Loan_Amount") or 0) for l in loans if l.get("Status") == "ACTIVE")
    active_loan_count = len([l for l in loans if l.get("Status") == "ACTIVE"])

    # attach customer name to each loan for display
    acc_to_name = {c["Account_No"]: c.get("Name", "Unknown") for c in customers}
    for l in loans:
        l["Customer_Name"] = acc_to_name.get(l.get("Account_No"), "Unknown")

    try:
        pending_txns = supabase.table("pending_transactions").select("*") \
            .eq("Status", "PENDING").order("Requested_At", desc=True).execute().data or []
    except Exception:
        pending_txns = []
    for p in pending_txns:
        p["Customer_Name"] = acc_to_name.get(p.get("Account_No"), "Unknown")

    return render_template(
        "manager_dashboard.html",
        customers=customers,
        loans=loans,
        total_customers=total_customers,
        total_cash=total_cash,
        vault_missing=vault_missing,
        total_customer_deposits=total_customer_deposits,
        total_loans_disbursed=total_loans_disbursed,
        active_loan_count=active_loan_count,
        pending_txns=pending_txns,
        label=session.get("staff_label"),
    )


@app.route("/manager/add-cash", methods=["POST"])
def manager_add_cash():
    if not require_role("manager"):
        return redirect(url_for("staff_login", portal="manager"))

    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        amount = 0

    note = request.form.get("note", "").strip() or "Manual cash reserve top-up by Manager"

    if amount <= 0:
        flash("Enter a valid positive amount to add to the cash reserve.", "error")
        return redirect(url_for("manager_dashboard"))

    new_balance = adjust_bank_cash(amount)
    if new_balance is None:
        flash("Could not update the cash reserve — the 'bank_vault' table doesn't exist yet "
              "in the database. Create it first (see README), then try again.", "error")
    else:
        flash(f"₹{amount:,.2f} added to the bank cash reserve. New reserve: ₹{new_balance:,.2f}. "
              f"({note})", "success")

    return redirect(url_for("manager_dashboard"))


@app.route("/manager/approve/<int:pending_id>", methods=["POST"])
def manager_approve_transaction(pending_id):
    if not require_role("manager"):
        return redirect(url_for("staff_login", portal="manager"))

    try:
        pending = supabase.table("pending_transactions").select("*").eq("id", pending_id).execute().data
        pending = pending[0] if pending else None
    except Exception:
        pending = None

    if not pending or pending.get("Status") != "PENDING":
        flash("This request was not found or has already been processed.", "error")
        return redirect(url_for("manager_dashboard"))

    account_no = pending["Account_No"]
    txn_type = pending["Type"]  # CREDIT or DEBIT
    amount = float(pending["Amount"])
    cashier_id = pending.get("Cashier_Name", "Unknown")

    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Linked customer account no longer exists.", "error")
        return redirect(url_for("manager_dashboard"))

    current_balance = float(customer.get("Balance") or 0)

    if txn_type == "DEBIT" and amount > current_balance:
        flash("Cannot approve — the customer's balance is now insufficient for this withdrawal.", "error")
        return redirect(url_for("manager_dashboard"))

    new_balance = current_balance - amount if txn_type == "DEBIT" else current_balance + amount
    description = ("High-value cash withdrawal (Manager-approved)" if txn_type == "DEBIT"
                    else "High-value cash deposit (Manager-approved)")

    try:
        supabase.table("registration").update({"Balance": new_balance}).eq("Account_No", account_no).execute()
        log_transaction(account_no, txn_type, amount, new_balance, description,
                         f"{cashier_id} (approved by {session.get('staff_label', 'Manager')})")
        if txn_type == "DEBIT":
            # Same loan-cushion rule as the cashier's instant withdrawal path — only the
            # portion beyond the customer's outstanding loan principal hits the vault.
            outstanding_principal = get_outstanding_loan_principal(account_no)
            vault_deduction = max(0.0, amount - outstanding_principal)
            if vault_deduction > 0:
                adjust_bank_cash(-vault_deduction)
        else:
            adjust_bank_cash(amount)

        # Apply the denomination breakdown to the originating cashier's drawer, if one was given
        import json as _json
        try:
            breakdown = _json.loads(pending.get("Denomination_Breakdown") or "{}")
        except Exception:
            breakdown = {}
        if breakdown:
            sign = -1 if txn_type == "DEBIT" else 1
            changes = {int(note): sign * count for note, count in breakdown.items()}
            adjust_cashier_drawer(cashier_id, changes)

        supabase.table("pending_transactions").update({
            "Status": "APPROVED",
            "Approved_By": session.get("staff_label", "Manager"),
            "Approved_At": datetime.utcnow().isoformat(),
        }).eq("id", pending_id).execute()

        flash(f"Approved: ₹{amount:,.2f} {txn_type.lower()} for account {account_no}. "
              f"New balance: ₹{new_balance:,.2f}.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("manager_dashboard"))


@app.route("/manager/reject/<int:pending_id>", methods=["POST"])
def manager_reject_transaction(pending_id):
    if not require_role("manager"):
        return redirect(url_for("staff_login", portal="manager"))

    try:
        supabase.table("pending_transactions").update({
            "Status": "REJECTED",
            "Approved_By": session.get("staff_label", "Manager"),
            "Approved_At": datetime.utcnow().isoformat(),
        }).eq("id", pending_id).execute()
        flash("Transaction request rejected.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("manager_dashboard"))


# ═════════════════════════════════════════
# CASHIER PORTAL — Balance check / update / withdraw
# ═════════════════════════════════════════
@app.route("/cashier")
def cashier_dashboard():
    if not require_role("cashier"):
        flash("Please log in to the Cashier portal first.", "error")
        return redirect(url_for("staff_login", portal="cashier"))

    cashier_id = session.get("cashier_id", "Unknown")
    drawer = get_cashier_drawer(cashier_id)
    return render_template("cashier_dashboard.html", label=session.get("staff_label"), customer=None,
                            drawer=drawer, denominations=DENOMINATIONS,
                            high_value_threshold=HIGH_VALUE_THRESHOLD)


@app.route("/cashier/lookup", methods=["POST"])
def cashier_lookup():
    if not require_role("cashier"):
        return redirect(url_for("staff_login", portal="cashier"))

    account_no = request.form.get("account_no", "").strip()
    customer = get_customer_by_account(account_no)
    cashier_id = session.get("cashier_id", "Unknown")
    drawer = get_cashier_drawer(cashier_id)

    if not customer:
        flash("No customer found with that account number.", "error")
        return render_template("cashier_dashboard.html", label=session.get("staff_label"), customer=None,
                                drawer=drawer, denominations=DENOMINATIONS,
                                high_value_threshold=HIGH_VALUE_THRESHOLD)

    # Only expose what the cashier is allowed to see
    limited = {
        "Account_No": customer["Account_No"],
        "Name": customer.get("Name"),
        "Bank_ID": customer.get("Bank_ID"),
        "Balance": customer.get("Balance"),
    }
    return render_template("cashier_dashboard.html", label=session.get("staff_label"), customer=limited,
                            drawer=drawer, denominations=DENOMINATIONS,
                            high_value_threshold=HIGH_VALUE_THRESHOLD)


@app.route("/cashier/transact", methods=["POST"])
def cashier_transact():
    if not require_role("cashier"):
        return redirect(url_for("staff_login", portal="cashier"))

    cashier_id = session.get("cashier_id", "Unknown")
    account_no = request.form.get("account_no", "").strip()
    txn_type = request.form.get("txn_type")  # 'deposit' or 'withdraw'
    try:
        txn_amount = float(request.form.get("amount", "0"))
    except ValueError:
        txn_amount = 0

    # Parse denomination breakdown submitted by the cashier (note value -> count)
    denom_breakdown = {}
    for d in DENOMINATIONS:
        try:
            count = int(request.form.get(f"denom_{d}", "0") or 0)
        except ValueError:
            count = 0
        if count > 0:
            denom_breakdown[d] = count

    customer = get_customer_by_account(account_no)
    drawer = get_cashier_drawer(cashier_id)

    def render_with(cust=None, extra_drawer=None, last_amount=None, last_type=None):
        return render_template("cashier_dashboard.html", label=session.get("staff_label"), customer=cust,
                                drawer=extra_drawer or drawer, denominations=DENOMINATIONS,
                                high_value_threshold=HIGH_VALUE_THRESHOLD,
                                last_txn_amount=last_amount, last_txn_type=last_type)

    if not customer:
        flash("No customer found with that account number.", "error")
        return redirect(url_for("cashier_dashboard"))

    if txn_amount <= 0:
        flash("Enter a valid amount.", "error")
        return render_with({"Account_No": customer["Account_No"], "Name": customer.get("Name"),
                             "Bank_ID": customer.get("Bank_ID"), "Balance": customer.get("Balance")})

    # Verify the denomination breakdown actually sums to the transaction amount
    denom_total = sum(note * count for note, count in denom_breakdown.items())
    if denom_breakdown and denom_total != txn_amount:
        flash(f"Denomination breakdown (₹{denom_total:,.2f}) does not match the transaction "
              f"amount (₹{txn_amount:,.2f}). Please correct the note counts.", "error")
        return render_with({"Account_No": customer["Account_No"], "Name": customer.get("Name"),
                             "Bank_ID": customer.get("Bank_ID"), "Balance": customer.get("Balance")})

    current_balance = float(customer.get("Balance") or 0)

    if txn_type == "withdraw":
        if txn_amount > current_balance:
            flash("Insufficient balance for this withdrawal.", "error")
            return render_with({"Account_No": customer["Account_No"], "Name": customer.get("Name"),
                                 "Bank_ID": customer.get("Bank_ID"), "Balance": current_balance})

        # Check the drawer actually has enough of each requested note before going further
        if denom_breakdown:
            for note, count in denom_breakdown.items():
                available = int((drawer or {}).get(f"Note_{note}", 0) or 0)
                if count > available:
                    flash(f"Drawer only has {available} note(s) of ₹{note} — cannot give out {count}.", "error")
                    return render_with({"Account_No": customer["Account_No"], "Name": customer.get("Name"),
                                        "Bank_ID": customer.get("Bank_ID"), "Balance": current_balance})

    # ── High-value check: route to Manager approval instead of completing immediately ──
    if txn_amount >= HIGH_VALUE_THRESHOLD:
        txn_label_for_pending = "DEBIT" if txn_type == "withdraw" else "CREDIT"
        created = create_pending_transaction(account_no, txn_label_for_pending, txn_amount,
                                              cashier_id, denom_breakdown)
        if created:
            flash(f"This {txn_type} of ₹{txn_amount:,.2f} exceeds ₹{HIGH_VALUE_THRESHOLD:,.0f} and "
                  f"requires Manager approval. It is now Pending Manager Approval and will complete "
                  f"once approved.", "success")
        else:
            flash("Could not create the pending approval request — the 'pending_transactions' "
                  "table may not exist yet in the database. Contact your administrator.", "error")
        return render_with({"Account_No": customer["Account_No"], "Name": customer.get("Name"),
                             "Bank_ID": customer.get("Bank_ID"), "Balance": current_balance})

    # ── Below threshold: complete immediately ──
    if txn_type == "withdraw":
        new_balance = current_balance - txn_amount
        description = "Cash withdrawal by cashier"
        txn_label = "DEBIT"
    else:
        new_balance = current_balance + txn_amount
        description = "Cash deposit by cashier"
        txn_label = "CREDIT"

    try:
        supabase.table("registration").update({"Balance": new_balance}).eq("Account_No", account_no).execute()
        log_transaction(account_no, txn_label, txn_amount, new_balance, description, session.get("staff_label", "Cashier"))
        if txn_type == "withdraw":
            # Loan cushion: if this customer has loan principal still sitting in their
            # balance (already deducted from the vault at disbursement), withdrawing that
            # portion should NOT hit the vault a second time. Only the amount withdrawn
            # BEYOND the outstanding loan principal — i.e. money that must be the
            # customer's own deposited funds — reduces the vault.
            outstanding_principal = get_outstanding_loan_principal(account_no)
            vault_deduction = max(0.0, txn_amount - outstanding_principal)
            if vault_deduction > 0:
                adjust_bank_cash(-vault_deduction)
        else:
            adjust_bank_cash(txn_amount)

        # Update the cashier's drawer counts if a denomination breakdown was provided
        if denom_breakdown:
            sign = -1 if txn_type == "withdraw" else 1
            changes = {note: sign * count for note, count in denom_breakdown.items()}
            adjust_cashier_drawer(cashier_id, changes)

        flash(f"{txn_label.title()} successful. New balance: ₹{new_balance:,.2f}", "success")
        txn_succeeded = True
    except Exception as e:
        flash(f"Error: {e}", "error")
        new_balance = current_balance
        txn_succeeded = False

    updated_customer = {
        "Account_No": account_no, "Name": customer.get("Name"),
        "Bank_ID": customer.get("Bank_ID"), "Balance": new_balance,
    }
    refreshed_drawer = get_cashier_drawer(cashier_id)
    if txn_succeeded:
        return render_with(updated_customer, refreshed_drawer, last_amount=txn_amount, last_type=txn_label)
    return render_with(updated_customer, refreshed_drawer)


@app.route("/cashier/drawer/restock", methods=["POST"])
def cashier_drawer_restock():
    """Lets the cashier manually add notes into their own drawer (e.g. at shift start)."""
    if not require_role("cashier"):
        return redirect(url_for("staff_login", portal="cashier"))

    cashier_id = session.get("cashier_id", "Unknown")
    changes = {}
    for d in DENOMINATIONS:
        try:
            count = int(request.form.get(f"restock_{d}", "0") or 0)
        except ValueError:
            count = 0
        if count != 0:
            changes[d] = count

    if not changes:
        flash("Enter at least one note count to restock.", "error")
        return redirect(url_for("cashier_dashboard"))

    result = adjust_cashier_drawer(cashier_id, changes)
    if result is None:
        flash("Could not update the drawer — the 'cashier_drawers' table may not exist yet "
              "in the database. Contact your administrator.", "error")
    else:
        flash("Drawer restocked successfully.", "success")

    return redirect(url_for("cashier_dashboard"))


# ═════════════════════════════════════════
# LOAN MANAGER PORTAL — Grant loans, auto-EMI deduction
# ═════════════════════════════════════════
@app.route("/loan")
def loan_dashboard():
    if not require_role("loan"):
        flash("Please log in to the Loan Manager portal first.", "error")
        return redirect(url_for("staff_login", portal="loan"))

    try:
        loans = supabase.table("loans").select("*").order("Granted_At", desc=True).execute().data or []
    except Exception:
        loans = []

    try:
        customers = supabase.table("registration").select("*").execute().data or []
    except Exception:
        customers = []
    acc_lookup = {c["Account_No"]: c for c in customers}
    for l in loans:
        cust = acc_lookup.get(l.get("Account_No"))
        l["Customer_Name"] = cust.get("Name", "Unknown") if cust else "Unknown"
        l["Bank_ID"] = cust.get("Bank_ID", "-") if cust else "-"

    return render_template("loan_dashboard.html", loans=loans, label=session.get("staff_label"))


@app.route("/loan/check/<account_no>")
def loan_check_eligibility(account_no):
    """AJAX-style helper: confirms a customer exists before granting a loan."""
    if not require_role("loan"):
        return redirect(url_for("staff_login", portal="loan"))
    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Customer is not registered. They must be onboarded by a CSE first.", "error")
    return redirect(url_for("loan_dashboard"))


@app.route("/loan/lookup/<account_no>")
def loan_lookup_customer(account_no):
    """JSON endpoint: fetches a customer's details for the loan-grant auto-fill UI."""
    if not require_role("loan"):
        return {"error": "unauthorized"}, 401

    customer = get_customer_by_account(account_no.strip())
    if not customer:
        return {"found": False}

    return {
        "found": True,
        "name": customer.get("Name", ""),
        "address": customer.get("Address", ""),
        "city": customer.get("City", ""),
        "country": customer.get("Country", ""),
        "contact": customer.get("Contact", ""),
        "balance": float(customer.get("Balance") or 0),
        "aadhar_last4": str(customer.get("Aadhar_No", ""))[-4:] if customer.get("Aadhar_No") else "",
        "pan_last4": str(customer.get("Pan_No", ""))[-4:] if customer.get("Pan_No") else "",
    }


@app.route("/loan/grant", methods=["POST"])
def loan_grant():
    if not require_role("loan"):
        return redirect(url_for("staff_login", portal="loan"))

    account_no = request.form.get("account_no", "").strip()
    aadhar_no  = request.form.get("aadhar_no", "").strip()
    pan_no     = request.form.get("pan_no", "").strip()
    confirm_aadhar = request.form.get("confirm_aadhar_no", "").strip()
    confirm_pan    = request.form.get("confirm_pan_no", "").strip()
    loan_amount_raw = request.form.get("loan_amount", "0")
    interest_rate_raw = request.form.get("interest_rate", "0")
    years  = request.form.get("years", "0")
    months = request.form.get("months", "0")

    if aadhar_no != confirm_aadhar:
        flash("Aadhar No and its confirmation do not match. Please re-check and try again.", "error")
        return redirect(url_for("loan_dashboard"))
    if pan_no != confirm_pan:
        flash("Pan No and its confirmation do not match. Please re-check and try again.", "error")
        return redirect(url_for("loan_dashboard"))

    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Customer is not registered. They must be onboarded by a CSE before a loan can be granted.", "error")
        return redirect(url_for("loan_dashboard"))

    if customer.get("Aadhar_No") != aadhar_no or customer.get("Pan_No") != pan_no:
        flash("Aadhar/PAN does not match our records for this account.", "error")
        return redirect(url_for("loan_dashboard"))

    try:
        loan_amount = float(loan_amount_raw)
        interest_rate = float(interest_rate_raw)
    except ValueError:
        flash("Loan amount and interest rate must be valid numbers.", "error")
        return redirect(url_for("loan_dashboard"))

    if loan_amount <= 0 or loan_amount > 1000000:
        flash("Loan amount must be greater than 0 and not exceed ₹10,00,000.", "error")
        return redirect(url_for("loan_dashboard"))

    bank_cash = get_bank_cash()
    if bank_cash is None:
        flash("Cannot verify available cash reserve — the 'bank_vault' table doesn't exist yet "
              "in the database. Ask your administrator to set it up before granting loans.", "error")
        return redirect(url_for("loan_dashboard"))
    if loan_amount > bank_cash:
        flash(f"Loan amount exceeds the bank's available cash reserve (₹{bank_cash:,.2f}). "
              f"The loan amount is paid out of the bank's reserve, so it cannot exceed what "
              f"the bank currently holds.", "error")
        return redirect(url_for("loan_dashboard"))

    tenure_months = months_from_input(years, months)
    if tenure_months <= 0:
        flash("Please enter a valid loan tenure.", "error")
        return redirect(url_for("loan_dashboard"))

    total_interest, total_payable, emi = calculate_emi(loan_amount, interest_rate, tenure_months)

    loan_data = {
        "Account_No": account_no,
        "Aadhar_No": aadhar_no,
        "Pan_No": pan_no,
        "Loan_Amount": loan_amount,
        "Interest_Rate": interest_rate,
        "Tenure_Months": tenure_months,
        "Total_Interest": total_interest,
        "Total_Payable": total_payable,
        "EMI_Amount": emi,
        "Months_Paid": 0,
        "Remaining_Amount": total_payable,
        "Outstanding_Principal": loan_amount,
        "Status": "ACTIVE",
        "Granted_By": session.get("staff_label", "Loan Manager"),
        "Granted_At": datetime.utcnow().isoformat(),
    }

    try:
        supabase.table("loans").insert(loan_data).execute()
        # Credit the loan amount into the customer's account immediately
        new_balance = float(customer.get("Balance") or 0) + loan_amount
        supabase.table("registration").update({"Balance": new_balance}).eq("Account_No", account_no).execute()
        log_transaction(account_no, "CREDIT", loan_amount, new_balance,
                         f"Loan disbursed ({tenure_months} months @ {interest_rate}%)",
                         session.get("staff_label", "Loan Manager"))
        # The loan principal physically leaves the bank's cash reserve and moves into the
        # customer's account. Only the INTEREST portion of each repayment returns to the
        # reserve later (see customer_pay_emi / loan_deduct_emi) — the principal portion of
        # a repayment doesn't re-enter the reserve as "new" cash, since that principal cash
        # is what's sitting in the customer's balance as a withdrawal cushion (see
        # get_outstanding_loan_principal and the cashier withdrawal logic).
        adjust_bank_cash(-loan_amount)
        flash(f"Loan granted. EMI: ₹{emi:,.2f}/month for {tenure_months} months.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("loan_dashboard"))


@app.route("/loan/deduct-emi/<int:loan_id>", methods=["POST"])
def loan_deduct_emi(loan_id):
    """
    Manually-triggered EMI deduction: pulls one or more months' EMI out of the customer's
    account balance and credits it toward the loan. Supports deducting several months at once.
    (Use a scheduled job / cron to call this automatically once a day in production.)
    """
    if not require_role("loan"):
        return redirect(url_for("staff_login", portal="loan"))

    months_raw = request.form.get("months_to_deduct", "1")
    try:
        months_to_deduct = max(1, int(months_raw))
    except ValueError:
        months_to_deduct = 1

    try:
        loan = supabase.table("loans").select("*").eq("id", loan_id).execute().data
        loan = loan[0] if loan else None
    except Exception:
        loan = None

    if not loan or loan.get("Status") != "ACTIVE":
        flash("Loan not found or already closed.", "error")
        return redirect(url_for("loan_dashboard"))

    account_no = loan["Account_No"]
    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Linked customer account not found.", "error")
        return redirect(url_for("loan_dashboard"))

    emi = float(loan["EMI_Amount"])
    months_remaining = int(loan["Tenure_Months"]) - int(loan["Months_Paid"])
    months_to_deduct = min(months_to_deduct, months_remaining)
    if months_to_deduct <= 0:
        flash("This loan has no remaining months to deduct.", "error")
        return redirect(url_for("loan_dashboard"))

    pay_amount = round(emi * months_to_deduct, 2)
    balance = float(customer.get("Balance") or 0)

    if balance < pay_amount:
        flash(f"Customer balance is insufficient for {months_to_deduct} month(s) of EMI "
              f"(₹{pay_amount:,.2f} needed).", "error")
        return redirect(url_for("loan_dashboard"))

    new_balance = balance - pay_amount
    new_remaining = max(0.0, float(loan["Remaining_Amount"]) - pay_amount)
    new_months_paid = int(loan["Months_Paid"]) + months_to_deduct
    new_status = "CLOSED" if new_months_paid >= int(loan["Tenure_Months"]) or new_remaining <= 0 else "ACTIVE"

    principal_portion, interest_portion = split_emi_payment(loan, pay_amount)
    new_outstanding_principal = max(0.0, float(loan.get("Outstanding_Principal") or 0) - principal_portion)

    try:
        supabase.table("registration").update({"Balance": new_balance}).eq("Account_No", account_no).execute()
        supabase.table("loans").update({
            "Months_Paid": new_months_paid,
            "Remaining_Amount": new_remaining,
            "Outstanding_Principal": new_outstanding_principal,
            "Status": new_status,
        }).eq("id", loan_id).execute()
        log_transaction(account_no, "EMI_DEDUCTION", pay_amount, new_balance,
                         f"EMI deduction — {months_to_deduct} month(s) "
                         f"({new_months_paid}/{loan['Tenure_Months']} total paid)",
                         "System (Loan Manager portal)")
        # Only the INTEREST portion returns to the bank's cash reserve. The principal portion
        # doesn't re-enter the reserve as new cash — that principal was already counted as
        # "out" at disbursement, and is now simply leaving the customer's balance (which was
        # itself already vault-deducted money) rather than the bank gaining anything new.
        adjust_bank_cash(interest_portion)
        flash(f"EMI deducted for {months_to_deduct} month(s): ₹{pay_amount:,.2f} "
              f"(₹{interest_portion:,.2f} interest added to reserve). "
              f"{loan['Tenure_Months'] - new_months_paid} months remaining.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("loan_dashboard"))


@app.route("/loan/schedule/<int:loan_id>")
def loan_schedule_pdf(loan_id):
    """Generates a downloadable EMI schedule PDF for a loan."""
    if not require_role("loan") and not require_role("customer"):
        return redirect(url_for("home"))

    try:
        loan = supabase.table("loans").select("*").eq("id", loan_id).execute().data
        loan = loan[0] if loan else None
    except Exception:
        loan = None

    if not loan:
        flash("Loan not found.", "error")
        return redirect(url_for("home"))

    # If a customer is requesting this, make sure it's their own loan
    if session.get("role") == "customer" and loan["Account_No"] != session.get("account_no"):
        flash("You can only download your own loan schedule.", "error")
        return redirect(url_for("customer_dashboard"))

    customer = get_customer_by_account(loan["Account_No"]) or {}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Title"], textColor=colors.HexColor("#0B3D91"))
    normal = styles["Normal"]

    story = [
        Paragraph("🏦 Bank Portal — Loan EMI Schedule", title_style),
        Spacer(1, 8),
        Paragraph(f"Customer: {customer.get('Name', '-')}", normal),
        Paragraph(f"Account No: {loan['Account_No']}", normal),
        Paragraph(f"Loan Amount: ₹{float(loan['Loan_Amount']):,.2f}", normal),
        Paragraph(f"Interest Rate: {loan['Interest_Rate']}% per annum (simple interest)", normal),
        Paragraph(f"Tenure: {loan['Tenure_Months']} months", normal),
        Paragraph(f"Total Payable: ₹{float(loan['Total_Payable']):,.2f}", normal),
        Paragraph(f"Monthly EMI: ₹{float(loan['EMI_Amount']):,.2f}", normal),
        Paragraph(f"Status: {loan['Status']} — Months Paid: {loan['Months_Paid']}/{loan['Tenure_Months']}", normal),
        Spacer(1, 16),
    ]

    table_data = [["Month #", "EMI Amount (₹)", "Status"]]
    months_paid = int(loan["Months_Paid"])
    for m in range(1, int(loan["Tenure_Months"]) + 1):
        status = "Paid" if m <= months_paid else "Upcoming"
        table_data.append([str(m), f"{float(loan['EMI_Amount']):,.2f}", status])

    tbl = Table(table_data, colWidths=[100, 200, 150])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B3D91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fc")]),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)

    doc.build(story)
    buf.seek(0)
    filename = f"EMI_Schedule_{loan['Account_No']}_{loan_id}.pdf"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/pdf")


@app.route("/statement/pdf/<account_no>")
def account_statement_pdf(account_no):
    """
    Generates a downloadable bank/account statement PDF — usable by both the CSE
    (looking a customer up by account number) and the customer themselves (their own account).
    """
    if not require_role("cse") and not require_role("customer"):
        return redirect(url_for("home"))

    # If a customer is requesting this, they may only download their own statement
    if session.get("role") == "customer" and session.get("account_no") != account_no:
        flash("You can only download your own statement.", "error")
        return redirect(url_for("customer_dashboard"))

    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Account not found.", "error")
        return redirect(url_for("home"))

    try:
        txns = supabase.table("transactions").select("*") \
            .eq("Account_No", account_no).order("Created_At", desc=True).execute().data or []
    except Exception:
        txns = []

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Title"], textColor=colors.HexColor("#0B3D91"))
    normal = styles["Normal"]

    story = [
        Paragraph("🏦 Vault &amp; Crest Bank — Account Statement", title_style),
        Spacer(1, 8),
        Paragraph(f"Customer: {customer.get('Name', '-')}", normal),
        Paragraph(f"Account No: {customer.get('Account_No', '-')}", normal),
        Paragraph(f"Bank ID: {customer.get('Bank_ID', '-')}", normal),
        Paragraph(f"Current Balance: ₹{float(customer.get('Balance') or 0):,.2f}", normal),
        Paragraph(f"Statement generated: {datetime.utcnow().strftime('%d %b %Y, %H:%M UTC')}", normal),
        Spacer(1, 16),
    ]

    table_data = [["Date", "Type", "Description", "Amount (₹)", "Balance After (₹)"]]
    for t in txns:
        date_str = t.get("Created_At", "-")[:10] if t.get("Created_At") else "-"
        table_data.append([
            date_str,
            t.get("Type", "-"),
            (t.get("Description", "-") or "-")[:40],
            f"{float(t.get('Amount') or 0):,.2f}",
            f"{float(t.get('Balance_After') or 0):,.2f}",
        ])

    if len(table_data) == 1:
        table_data.append(["-", "-", "No transactions on record", "-", "-"])

    tbl = Table(table_data, colWidths=[70, 70, 180, 80, 90])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B3D91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fc")]),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)

    doc.build(story)
    buf.seek(0)
    filename = f"Statement_{account_no}.pdf"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/pdf")


# ═════════════════════════════════════════
# CUSTOMER PORTAL
# ═════════════════════════════════════════
@app.route("/customer")
def customer_dashboard():
    if not require_role("customer"):
        flash("Please log in to access your account.", "error")
        return redirect(url_for("customer_login"))

    account_no = session.get("account_no")
    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Account not found. Please contact your branch.", "error")
        session.clear()
        return redirect(url_for("customer_login"))

    try:
        loans = supabase.table("loans").select("*").eq("Account_No", account_no).execute().data or []
    except Exception:
        loans = []

    other_accounts = []
    if customer.get("Aadhar_No"):
        other_accounts = [
            a for a in get_accounts_for_identity(customer["Aadhar_No"])
            if a["Account_No"] != account_no
        ]

    # Spend analytics: bucket this account's transaction history into categories
    try:
        all_txns = supabase.table("transactions").select("*").eq("Account_No", account_no).execute().data or []
    except Exception:
        all_txns = []

    category_totals = {}
    for t in all_txns:
        raw_type = t.get("Type", "")
        category = TRANSACTION_CATEGORY_MAP.get(raw_type, "Other")
        category_totals[category] = category_totals.get(category, 0) + float(t.get("Amount") or 0)

    domestic_limit = customer.get("Domestic_Limit")
    international_limit = customer.get("International_Limit")
    intl_enabled = customer.get("Intl_Enabled", False)

    return render_template(
        "customer_dashboard.html",
        customer=customer,
        loans=loans,
        statement=None,
        other_accounts=other_accounts,
        category_totals=category_totals,
        domestic_limit=domestic_limit if domestic_limit is not None else DEFAULT_DOMESTIC_LIMIT,
        international_limit=international_limit if international_limit is not None else DEFAULT_INTERNATIONAL_LIMIT,
        intl_enabled=bool(intl_enabled),
    )


@app.route("/customer/statement", methods=["POST"])
def customer_statement():
    if not require_role("customer"):
        return redirect(url_for("customer_login"))

    account_no = session.get("account_no")
    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Account not found.", "error")
        return redirect(url_for("customer_login"))

    pin = request.form.get("pin", "")
    bank_id = request.form.get("bank_id", "")
    entered_account = request.form.get("account_no", "")

    if entered_account != account_no or bank_id != customer.get("Bank_ID"):
        flash("Account number or Bank ID does not match your profile.", "error")
        statement = None
    elif not check_secret(pin, customer.get("Pin_Hash")):
        flash("Incorrect PIN.", "error")
        statement = None
    else:
        try:
            statement = supabase.table("transactions").select("*") \
                .eq("Account_No", account_no).order("Created_At", desc=True).execute().data or []
        except Exception:
            statement = []

    try:
        loans = supabase.table("loans").select("*").eq("Account_No", account_no).execute().data or []
    except Exception:
        loans = []

    return render_template("customer_dashboard.html", customer=customer, loans=loans, statement=statement)


@app.route("/customer/pay-emi-preview/<int:loan_id>/<int:months>")
def customer_pay_emi_preview(loan_id, months):
    """JSON endpoint: returns the total amount payable for N months, for the confirm dialog."""
    if not require_role("customer"):
        return {"error": "unauthorized"}, 401

    account_no = session.get("account_no")
    try:
        loan = supabase.table("loans").select("*").eq("id", loan_id).eq("Account_No", account_no).execute().data
        loan = loan[0] if loan else None
    except Exception:
        loan = None

    if not loan or loan.get("Status") != "ACTIVE":
        return {"error": "Loan not found or already closed."}, 404

    emi = float(loan["EMI_Amount"])
    months_remaining = int(loan["Tenure_Months"]) - int(loan["Months_Paid"])
    months_clamped = max(1, min(int(months), months_remaining))
    total = round(emi * months_clamped, 2)

    return {
        "months": months_clamped,
        "emi_per_month": emi,
        "total_amount": total,
        "months_remaining_after": months_remaining - months_clamped,
    }


@app.route("/customer/transfer-lookup/<account_no>")
def customer_transfer_lookup(account_no):
    """JSON endpoint: fetches the receiver's display name for a transfer confirmation step."""
    if not require_role("customer"):
        return {"error": "unauthorized"}, 401

    account_no = account_no.strip()
    if account_no == session.get("account_no"):
        return {"found": False, "error": "You cannot transfer to the account you're logged into. "
                                          "Pick one of your other accounts from the dropdown instead."}

    receiver = get_customer_by_account(account_no)
    if not receiver:
        return {"found": False}

    return {
        "found": True,
        "name": receiver.get("Name", ""),
        "bank_id": receiver.get("Bank_ID", ""),
    }


@app.route("/customer/transfer", methods=["POST"])
def customer_transfer():
    if not require_role("customer"):
        return redirect(url_for("customer_login"))

    sender_account = session.get("account_no")
    receiver_account = request.form.get("receiver_account", "").strip()
    pin = request.form.get("pin", "")

    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        amount = 0

    sender = get_customer_by_account(sender_account)
    if not sender:
        flash("Your account could not be found. Please log in again.", "error")
        return redirect(url_for("customer_login"))

    if not check_secret(pin, sender.get("Pin_Hash")):
        flash("Incorrect PIN. Transfer cancelled.", "error")
        return redirect(url_for("customer_dashboard"))

    if amount <= 0:
        flash("Enter a valid amount to transfer.", "error")
        return redirect(url_for("customer_dashboard"))

    if receiver_account == sender_account:
        flash("You cannot transfer money to the same account you're sending from.", "error")
        return redirect(url_for("customer_dashboard"))

    receiver = get_customer_by_account(receiver_account)
    if not receiver:
        flash("Receiver account not found. Please check the account number.", "error")
        return redirect(url_for("customer_dashboard"))

    sender_balance = float(sender.get("Balance") or 0)
    if amount > sender_balance:
        flash("Insufficient balance for this transfer.", "error")
        return redirect(url_for("customer_dashboard"))

    # Enforce the sender's domestic transaction limit on this single transfer
    domestic_limit = sender.get("Domestic_Limit")
    domestic_limit = float(domestic_limit) if domestic_limit is not None else DEFAULT_DOMESTIC_LIMIT
    if amount > domestic_limit:
        flash(f"This transfer exceeds your domestic transaction limit of ₹{domestic_limit:,.2f}. "
              f"You can raise your limit from the Settings section below.", "error")
        return redirect(url_for("customer_dashboard"))

    new_sender_balance = sender_balance - amount
    new_receiver_balance = float(receiver.get("Balance") or 0) + amount

    try:
        supabase.table("registration").update({"Balance": new_sender_balance}).eq("Account_No", sender_account).execute()
        supabase.table("registration").update({"Balance": new_receiver_balance}).eq("Account_No", receiver_account).execute()
        log_transaction(sender_account, "TRANSFER_OUT", amount, new_sender_balance,
                         f"Transfer to {receiver_account} ({receiver.get('Name', 'Unknown')})",
                         sender.get("Name", "Customer"))
        log_transaction(receiver_account, "TRANSFER_IN", amount, new_receiver_balance,
                         f"Transfer from {sender_account} ({sender.get('Name', 'Unknown')})",
                         sender.get("Name", "Customer"))
        # Internal transfer between two of the bank's own accounts — no change to total bank cash,
        # since the money never leaves the bank, it just moves between two customers' balances.
        flash(f"₹{amount:,.2f} transferred to {receiver.get('Name', 'the recipient')} "
              f"(Account {receiver_account}).", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("customer_dashboard"))


@app.route("/customer/change-pin", methods=["POST"])
def customer_change_pin():
    if not require_role("customer"):
        return redirect(url_for("customer_login"))

    account_no = session.get("account_no")
    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Account not found.", "error")
        return redirect(url_for("customer_login"))

    old_pin = request.form.get("old_pin", "")
    new_pin = request.form.get("new_pin", "")
    confirm_new_pin = request.form.get("confirm_new_pin", "")

    if not check_secret(old_pin, customer.get("Pin_Hash")):
        flash("Your current PIN is incorrect.", "error")
        return redirect(url_for("customer_dashboard"))

    if not new_pin.isdigit() or len(new_pin) != 4:
        flash("New PIN must be exactly 4 digits.", "error")
        return redirect(url_for("customer_dashboard"))

    if new_pin != confirm_new_pin:
        flash("New PIN and its confirmation do not match.", "error")
        return redirect(url_for("customer_dashboard"))

    if new_pin == old_pin:
        flash("New PIN must be different from your current PIN.", "error")
        return redirect(url_for("customer_dashboard"))

    try:
        supabase.table("registration").update({"Pin_Hash": hash_secret(new_pin)}).eq("Account_No", account_no).execute()
        flash("Your PIN has been changed successfully.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("customer_dashboard"))


@app.route("/customer/update-limits", methods=["POST"])
def customer_update_limits():
    if not require_role("customer"):
        return redirect(url_for("customer_login"))

    account_no = session.get("account_no")
    customer = get_customer_by_account(account_no)
    if not customer:
        flash("Account not found.", "error")
        return redirect(url_for("customer_login"))

    pin = request.form.get("pin", "")
    if not check_secret(pin, customer.get("Pin_Hash")):
        flash("Incorrect PIN. Limit changes were not saved.", "error")
        return redirect(url_for("customer_dashboard"))

    try:
        domestic_limit = float(request.form.get("domestic_limit", DEFAULT_DOMESTIC_LIMIT))
        international_limit = float(request.form.get("international_limit", DEFAULT_INTERNATIONAL_LIMIT))
    except ValueError:
        flash("Limits must be valid numbers.", "error")
        return redirect(url_for("customer_dashboard"))

    intl_enabled = request.form.get("intl_enabled") == "on"

    if domestic_limit <= 0 or international_limit <= 0:
        flash("Limits must be greater than zero.", "error")
        return redirect(url_for("customer_dashboard"))

    try:
        supabase.table("registration").update({
            "Domestic_Limit": domestic_limit,
            "International_Limit": international_limit,
            "Intl_Enabled": intl_enabled,
        }).eq("Account_No", account_no).execute()
        flash("Your transaction limits and settings have been updated.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("customer_dashboard"))


@app.route("/customer/pay-emi", methods=["POST"])
def customer_pay_emi():
    if not require_role("customer"):
        return redirect(url_for("customer_login"))

    account_no = session.get("account_no")
    customer = get_customer_by_account(account_no)
    loan_id = request.form.get("loan_id")
    months_to_pay_raw = request.form.get("months_to_pay", "1")
    pin = request.form.get("pin", "")

    if not customer or not check_secret(pin, customer.get("Pin_Hash")):
        flash("Incorrect PIN. EMI payment cancelled.", "error")
        return redirect(url_for("customer_dashboard"))

    try:
        months_to_pay = max(1, int(months_to_pay_raw))
    except ValueError:
        months_to_pay = 1

    try:
        loan = supabase.table("loans").select("*").eq("id", loan_id).eq("Account_No", account_no).execute().data
        loan = loan[0] if loan else None
    except Exception:
        loan = None

    if not loan or loan.get("Status") != "ACTIVE":
        flash("Loan not found or already closed.", "error")
        return redirect(url_for("customer_dashboard"))

    emi = float(loan["EMI_Amount"])
    months_remaining = int(loan["Tenure_Months"]) - int(loan["Months_Paid"])
    months_to_pay = min(months_to_pay, months_remaining)
    pay_amount = round(emi * months_to_pay, 2)

    balance = float(customer.get("Balance") or 0)
    if balance < pay_amount:
        flash("Insufficient balance to make this EMI payment.", "error")
        return redirect(url_for("customer_dashboard"))

    new_balance = balance - pay_amount
    new_months_paid = int(loan["Months_Paid"]) + months_to_pay
    new_remaining = max(0.0, float(loan["Remaining_Amount"]) - pay_amount)
    new_status = "CLOSED" if new_months_paid >= int(loan["Tenure_Months"]) or new_remaining <= 0 else "ACTIVE"

    principal_portion, interest_portion = split_emi_payment(loan, pay_amount)
    new_outstanding_principal = max(0.0, float(loan.get("Outstanding_Principal") or 0) - principal_portion)

    try:
        supabase.table("registration").update({"Balance": new_balance}).eq("Account_No", account_no).execute()
        supabase.table("loans").update({
            "Months_Paid": new_months_paid,
            "Remaining_Amount": new_remaining,
            "Outstanding_Principal": new_outstanding_principal,
            "Status": new_status,
        }).eq("id", loan["id"]).execute()
        log_transaction(account_no, "EMI_PAYMENT", pay_amount, new_balance,
                         f"EMI self-payment — {months_to_pay} month(s)", customer.get("Name", "Customer"))
        # Only the INTEREST portion returns to the bank's cash reserve — see the matching
        # comment in loan_deduct_emi for why the principal portion does not.
        adjust_bank_cash(interest_portion)
        flash(f"Paid ₹{pay_amount:,.2f} towards loan ({months_to_pay} month(s)) — "
              f"₹{interest_portion:,.2f} interest added to the bank's reserve. "
              f"{int(loan['Tenure_Months']) - new_months_paid} months remaining.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("customer_dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=False)
