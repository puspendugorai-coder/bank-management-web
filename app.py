from flask import Flask, render_template, request, redirect, url_for, session, flash
from supabase import create_client, Client
import os, re

app = Flask(__name__)
app.secret_key = "your_secret_key_here"  # Change this to any random string

# --- SUPABASE CONFIG (use environment variables for safety) ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://bryhzndcduqvfnjhgeub.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJyeWh6bmRjZHVxdmZuamhnZXViIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NTI4NjY2NSwiZXhwIjoyMDkwODYyNjY1fQ.Vg2c_mQTAQzBH9dsMdOh8WtetBuGKtQY4dnDolipma0")  
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ─────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            flash("Please enter both ID and Password.", "error")
            return redirect(url_for("login"))

        try:
            res = supabase.table("users").select("password").eq("name", username).execute()
            if res.data and res.data[0]["password"] == password:
                session["user"] = username
                return redirect(url_for("dashboard"))
            else:
                flash("Invalid credentials. Please try again.", "error")
        except Exception as e:
            flash(f"Database error: {e}", "error")

    return render_template("login.html")


# ─────────────────────────────────────────
# SIGNUP
# ─────────────────────────────────────────
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirm  = request.form.get("confirm_password")

        if not username or not password or not confirm:
            flash("All fields are required.", "error")
            return redirect(url_for("signup"))

        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("signup"))

        try:
            check = supabase.table("users").select("*").eq("name", username).execute()
            if check.data:
                flash("Username already exists.", "error")
                return redirect(url_for("signup"))

            supabase.table("users").insert({"name": username, "password": password}).execute()
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            flash(f"Database error: {e}", "error")

    return render_template("signup.html")


# ─────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))

    try:
        res = supabase.table("registration").select("*").execute()
        records = res.data
    except:
        records = []

    return render_template("dashboard.html", records=records, user=session["user"])


# ─────────────────────────────────────────
# ADD RECORD
# ─────────────────────────────────────────
@app.route("/add", methods=["POST"])
def add_record():
    if "user" not in session:
        return redirect(url_for("login"))

    email    = request.form.get("email", "")
    age      = request.form.get("age", "0")
    amount   = request.form.get("amount", "0")
    pan      = request.form.get("pan_no", "")
    bank_id  = request.form.get("bank_id", "")
    pin      = request.form.get("pin_no", "")
    card     = request.form.get("card_no", "")
    confirm_card = request.form.get("confirm_card_no", "")
    confirm_pin  = request.form.get("confirm_pin_no", "")

    if card != confirm_card:
        flash("Card numbers do not match.", "error")
        return redirect(url_for("dashboard"))
    if pin != confirm_pin:
        flash("PIN numbers do not match.", "error")
        return redirect(url_for("dashboard"))
    # Validations
    email_pattern = r'^[a-z0-9]+[\._]?[a-z0-9]+[@]\w+[.]\w{2,3}$'
    if not re.search(email_pattern, email):
        flash("Invalid email format.", "error")
        return redirect(url_for("dashboard"))
    if int(age) < 18:
        flash("Age must be 18 or above.", "error")
        return redirect(url_for("dashboard"))
    if int(amount) < 1000 or int(amount) > 1000000:
        flash("Amount must be between ₹1,000 and ₹10,00,000.", "error")
        return redirect(url_for("dashboard"))
    if pan.isdigit() or bank_id.isdigit():
        flash("Pan No and Bank ID must be alphanumeric.", "error")
        return redirect(url_for("dashboard"))
    if len(bank_id) > 6:
        flash("Bank ID must be maximum 6 characters.", "error")
        return redirect(url_for("dashboard"))
    if not bank_id.isalnum():
        flash("Bank ID must be alphanumeric only (letters and numbers).", "error")
        return redirect(url_for("dashboard"))
    if len(pin) != 4 or not pin.isdigit():
        flash("Pin must be exactly 4 digits.", "error")
        return redirect(url_for("dashboard"))
    contact = request.form.get("contact", "")
    if len(contact) != 10 or not contact.isdigit():
        flash("Contact number must be exactly 10 digits.", "error")
        return redirect(url_for("dashboard"))
    data = {
        "Name":      request.form.get("name"),
        "Address":   request.form.get("address"),
        "Email":     email,
        "Age":       age,
        "City":      request.form.get("city"),
        "Country":   request.form.get("country"),
        "Aadhar_No": request.form.get("aadhar_no"),
        "Pan_No":    pan,
        "Contact":   request.form.get("contact"),
        "Card_No":   request.form.get("card_no"),
        "Pin_No":    pin,
        "Bank_ID":   bank_id,
        "Amount":    amount,
        "Gender":    request.form.get("gender"),
    }

    try:
        supabase.table("registration").insert(data).execute()
        flash("Record added successfully!", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("dashboard"))


# ─────────────────────────────────────────
# UPDATE RECORD
# ─────────────────────────────────────────
@app.route("/update", methods=["POST"])
def update_record():
    if "user" not in session:
        return redirect(url_for("login"))

    bank_id = request.form.get("bank_id", "")
    amount  = request.form.get("amount", "0")

    # ── Amount validation ──
    if int(amount) < 1000 or int(amount) > 1000000:
        flash("Amount must be between ₹1,000 and ₹10,00,000.", "error")
        return redirect(url_for("dashboard"))

    # ── Bank ID validation ──
    if len(bank_id) > 6:
        flash("Bank ID must be maximum 6 characters.", "error")
        return redirect(url_for("dashboard"))
    if not bank_id.isalnum():
        flash("Bank ID must be alphanumeric only.", "error")
        return redirect(url_for("dashboard"))
    if bank_id.isdigit():
        flash("Bank ID must contain at least one letter.", "error")
        return redirect(url_for("dashboard"))

    # Card_No and Pin_No are excluded — they cannot be updated
    data = {
        "Name":      request.form.get("name"),
        "Address":   request.form.get("address"),
        "Email":     request.form.get("email"),
        "Age":       request.form.get("age"),
        "City":      request.form.get("city"),
        "Country":   request.form.get("country"),
        "Aadhar_No": request.form.get("aadhar_no"),
        "Pan_No":    request.form.get("pan_no"),
        "Contact":   request.form.get("contact"),
        "Amount":    request.form.get("amount"),
        "Gender":    request.form.get("gender"),
    }

    try:
        supabase.table("registration").update(data).eq("Bank_ID", bank_id).execute()
        flash("Record updated successfully!", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("dashboard"))


# ─────────────────────────────────────────
# DELETE RECORD
# ─────────────────────────────────────────
@app.route("/delete/<bank_id>")
def delete_record(bank_id):
    if "user" not in session:
        return redirect(url_for("login"))

    try:
        supabase.table("registration").delete().eq("Bank_ID", bank_id).execute()
        flash("Record deleted.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    return redirect(url_for("dashboard"))


# ─────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=False)
