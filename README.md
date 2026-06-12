# 🏦 Bank Management Web App

![Python](https://img.shields.io/badge/Python-3.10-blue)
![Flask](https://img.shields.io/badge/Flask-Web_App-lightgrey)
![Supabase](https://img.shields.io/badge/Database-Supabase-green)
![Status](https://img.shields.io/badge/Status-Live-brightgreen)

## 🔗 Live Demo
## 👉 [Click Here to Open the App](https://alphacoder7206-bank-management-system.hf.space)

---

## ✨ Features
- 🔐 User Login & Signup with lockout after 3 wrong attempts
- ➕ Add new bank client records
- ✏️ Update records by clicking any row in the table
- 🗑️ Delete records instantly
- 🔍 Real-time search across all fields
- ✅ Full form validation — email, age, PIN, contact, amount
- 📊 Live data table with all registered clients

---

## 🛠️ Tech Stack
| Layer | Technology |
|-------|-----------|
| Frontend | HTML, CSS, JavaScript |
| Backend | Python Flask |
| Database | Supabase (PostgreSQL) |
| Hosting | Hugging Face Spaces (Docker) |

---

## 📁 Project Structure
```
bank_management/
├── app.py              ← Flask backend & all routes
├── requirements.txt    ← Python dependencies
├── Dockerfile          ← Container for deployment
└── templates/
    ├── login.html      ← Login page
    ├── signup.html     ← Registration page
    └── dashboard.html  ← Main dashboard with CRUD
```

---

## 🚀 Run Locally
```bash
git clone https://github.com/puspendugorai-coder/bank-management-web.git
cd bank-management-web
pip install -r requirements.txt
python app.py
```
Open `http://127.0.0.1:5000` in your browser.

---

## 🔒 Environment Variables
Create a `.env` file or set these in your deployment platform:
```
SUPABASE_URL = your_supabase_project_url
SUPABASE_KEY = your_supabase_service_key
```

---

## 👨‍💻 Developer
**Puspendu Gorai**
- GitHub: [puspendugorai-coder](https://github.com/puspendugorai-coder)
- Live App: [Bank Management System](https://alphacoder7206-bank-management.hf.space)
