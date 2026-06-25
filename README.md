# 🏦 PG BANK — Multi-Portal Banking System

A 5-portal bank management web app: **Manager, Cashier, Customer Service Employee (CSE),
Loan Manager**, and **Customer**. Built with Flask + Supabase (PostgreSQL).

---

## 📁 Project Structure
```
bank_project/
├── app.py                       ← Flask backend & all routes
├── requirements.txt             ← Python dependencies
└── templates/
    ├── portal_select.html       ← Landing page — pick your portal
    ├── staff_login.html         ← Shared access-code login (Manager/Cashier/CSE/Loan Manager)
    ├── customer_login.html      ← Customer login (Email + Bank ID)
    ├── cse_dashboard.html       ← CSE portal — client onboarding & record management
    ├── manager_dashboard.html   ← Manager portal — branch-wide analytics
    ├── cashier_dashboard.html   ← Cashier portal — balance lookup, deposit/withdraw
    ├── loan_dashboard.html      ← Loan Manager portal — grant loans, EMI tracking
    └── customer_dashboard.html  ← Customer portal — balance, statement, EMI payment
```
---

## 🔑 Access Codes (staff portals)
| Portal | Code |
|---|---|
| Manager | `1122` |
| Cashier | `7890` |
| CSE | `2508` |
| Loan Manager | `7206` |

<h2>**🔑 These Access codes are mandatory to get logged in into the particular portal so note it for each portal.**</h2>

<h4> Note:- The **Customer** portal is different: customers log in with the **Email + Bank ID** that the
CSE entered when registering them. If it does not gets matched, they see
*"You are not registered with us."*</h4>

---
## <h1><i>**👉WORKING OF APP AND INDIVIDUALS👈**</i></h1>
---
<h3>⭐MANAGER PORTAL⭐</h3>
<p><ul>
    <li>Manager can check all activities performed over the app by any employee</li>
    <li>Transaction for more than 50000 requires manager approval</li>
    <li>Manager can check and add up money in the total cash reserve of the bank</li>
    <li>Manager can check the details of any customer</li>
    <li>Manager can check how many customer has loans and the tenure</li>
</ul></p>
<hr>
<h3>⭐CASHIER PORTAL⭐</h3>
<p><ul>
    <li>Cashier can credit or withdraw money from any account but without account number it is not possible.</li>
    <li>Cashier maintains a drawer of different types of currencies.</li>
    <li>Total counting of notes and total money is displayed.</li>
</ul></p>
<hr>
<h3>⭐CUSTOMER SERVICE EMPLOYEE(CSE) PORTAL⭐</h3>
<p><ul>
    <li>This portal is there for customer service</li>
    <li>CSE can either register a customer or check account details of a customer fetched by a registered account number.</li>
    <li>He can also delete or update customer details</li>
    <li>Customer records cannot be deleted if he/she has an active loan</li>
</ul></p>
<hr>
<h3>⭐LOAN MANAGER PORTAL⭐</h3>
<p><ul>
    <li>Loan officer work is to grant loan to a customer (max 1000000)</li>
    <li>He/she can pay loan which will get deducted from the customer account</li>
    <li>Have the option to download the current loan receipt of a customer</li>
</ul></p>
<hr>
<h3>⭐CUSTOMER PORTAL⭐</h3>
<p><ul>
    <li>Portal is accessed by customer **E-mail id and Bank_ID** only if registered.</li>
    <li>Customer can see his/her total number of accounts</li>
    <li>Customer can transfer money within their own accounts or any other customer account registered in the same bank</li>
    <li>Customer can get their bank statement in a pdf form and download it.</li>
    <li>Customer can pay their loan from home by authorizing through their pin</li>
    <li>Customer can get their loan statement in a pdf form and download it</li>
    <li>Customer can change their pin thereselves by using authentication</li>
    <li>Customer can set their transaction limit</li>
    <li>Customer can see their spend chart analysis</li>
</ul></p>
<hr>
<h1><i>**👉FEATURES OF THE APP👈**</i></h1> 
<hr>
<ol>
    <li>AUTHORIZED ACCESS THROUGH PARTICULAR ACCESS CODES</li>
    <li>DEPOSIT OR WITHDRAW MONEY EASILY THROUGH CASHIER PORTAL</li>
    <li>GRANT EASY LOAN </li>
    <li>CUSTOMER CAN SEND MONEY TO ANYBODY REGISTERED</li>
    <li>PAY LOAN AT HOME</li>
    <li>AUTO EMI DEDUCTION</li>
    <li>CHECK ACCOUNT AND LOAN STATEMENTS</li>
    <li>DOWNLOAD STATEMENTS AS PDF FORMAT</li>
</ol>
<hr>
<h1><i>**👉SECURITIES👈**</i></h1><hr>
<p>
    <ol>
        <li>**PINs are bcrypt-hashed** (`Pin_Hash`) instead of stored as plaintext.</li>
        <li>**Card numbers are stored as plain text** in `Card_No`</li>
        <li>The CSE success message shows the raw card number and PIN once at creation time so they can be
  handed to the customer.</li>
        <li>Staff portals authenticate purely by access code</li>
        <li>Customer records cannot be deleted if he/she has an active loan</li>
    </ol>
</p>
<hr>

## 🧮 EMI Formula (as specified)
```
Total Interest = (Principal × Rate × Time_in_years) / 100
Total Payable  = Principal + Total Interest
Monthly EMI    = Total Payable / Tenure_in_months
```
Tenure can be entered in years and months in the Loan Manager portal — months are auto-summed
(`years × 12 + months`) before any calculation happens.

---
  The Manager dashboard now shows "Bank Cash Reserve" and "Total Customer Deposits" as two
  separate figures.
