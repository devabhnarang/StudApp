import os
import tempfile
import secrets
from functools import wraps
from flask import Flask, request, render_template, redirect, jsonify, session, url_for
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from PyPDF2 import PdfReader
from docx import Document
from dotenv import load_dotenv
import openai
import psycopg2
from psycopg2.extras import RealDictCursor

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)

# Secret key for session management
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)

# ---------- PostgreSQL Connection Helper ----------
def get_db_connection():
    return psycopg2.connect(
        host=os.getenv('DB_HOST'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASS'),
        cursor_factory=RealDictCursor
    )

# ---------- Groq API config ----------
openai.api_key = os.getenv("GROQ_API_KEY")
openai.base_url = 'https://api.groq.com/openai/v1/'
GROQ_MODEL = 'llama-3.3-70b-versatile'

# ---------- Decorators ----------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect("/index.html")
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            return "Forbidden", 403
        return f(*args, **kwargs)
    return wrapper

# ---------- Routes ----------
@app.route('/')
def home_page():
    return render_template('home.html')

@app.route('/index.html')
def index():
    return render_template('index.html')

# ---------- Auth ----------
@app.route('/signup', methods=['POST'])
def signup():
    name = request.form['name']
    email = request.form['email']
    password = request.form['password']
    role = 'student'
    hashed_password = generate_password_hash(password)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            return 'Email already exists'
        cur.execute(
            "INSERT INTO users (name, email, password, role) VALUES (%s, %s, %s, %s)",
            (name, email, hashed_password, role)
        )
        conn.commit()
        return 'Signup successful'
    except Exception as e:
        print("❌ Signup Error:", e)
        return 'Signup failed'
    finally:
        cur.close()
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    email = request.form['email']
    password = request.form['password']

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name, role, password FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['role'] = user['role']
            session['name'] = user['name']
            return "ok"
        return 'Invalid credentials'
    except Exception as e:
        print("❌ Login Error:", e)
        return 'Login error'
    finally:
        cur.close()
        conn.close()

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/index.html')

# ---------- Dashboard ----------
@app.route('/dashboard')
@login_required
def dashboard_redirect():
    role = session['role']
    if role == "admin":
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('student_dashboard'))

@app.route('/student_dashboard')
@login_required
def student_dashboard():
    if session['role'] == 'admin':
        return "Admins cannot access student dashboard", 403
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, title, description, date, time FROM reminders WHERE user_id = %s ORDER BY date, time",
            (user_id,)
        )
        reminders = cur.fetchall()
        return render_template(
            'student_dashboard.html',
            user_id=user_id,
            reminders=reminders,
            username=session['name']
        )
    except Exception as e:
        print("❌ Student Dashboard Error:", e)
        return 'Error loading dashboard'
    finally:
        cur.close()
        conn.close()

@app.route('/admin_dashboard')
@login_required
@admin_required
def admin_dashboard():
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name, email, role FROM users ORDER BY id")
        users = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM reminders")
        reminder_count = cur.fetchone()['count']

        cur.execute("""
            SELECT r.id, r.title, r.description, r.date, r.time, u.name
            FROM reminders r
            JOIN users u ON r.user_id = u.id
            ORDER BY r.date DESC, r.time DESC
            LIMIT 10
        """)
        reminders = cur.fetchall()

        return render_template(
            'admin_dashboard.html',
            user_id=user_id,
            users=users,
            reminder_count=reminder_count,
            reminders=reminders,
            username=session['name']
        )
    except Exception as e:
        print("❌ Admin Dashboard Error:", e)
        return 'Error loading dashboard'
    finally:
        cur.close()
        conn.close()

# ---------- Admin: Manage Users ----------

@app.route('/revoke_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def revoke_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET status='revoked' WHERE id=%s", (user_id,))
        conn.commit()
        return jsonify({'status': 'success', 'message': 'User access revoked'})
    except Exception as e:
        print("❌ Revoke User Error:", e)
        return jsonify({'status': 'error', 'message': 'Failed to revoke user'})
    finally:
        cur.close()
        conn.close()

@app.route('/restore_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def restore_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET status='active' WHERE id=%s", (user_id,))
        conn.commit()
        return jsonify({'status': 'success', 'message': 'User access restored'})
    except Exception as e:
        print("❌ Restore User Error:", e)
        return jsonify({'status': 'error', 'message': 'Failed to restore user'})
    finally:
        cur.close()
        conn.close()



# ---------- Reminders ----------
@app.route('/add_reminder', methods=['POST'])
@login_required
def add_reminder():
    try:
        title = request.form.get('title')
        description = request.form.get('description')
        date = request.form.get('date')
        time = request.form.get('time')
        user_id = session['user_id']

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO reminders (user_id, title, description, date, time) VALUES (%s, %s, %s, %s, %s)",
            (user_id, title, description, date, time)
        )
        conn.commit()
        return 'Reminder added successfully!'
    except Exception as e:
        print("❌ Add Reminder Error:", e)
        return 'Failed to add reminder'
    finally:
        cur.close()
        conn.close()

@app.route('/delete_reminder/<int:reminder_id>', methods=['POST'])
@login_required
def delete_reminder(reminder_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM reminders WHERE id = %s", (reminder_id,))
        row = cur.fetchone()
        if not row:
            return "Not found", 404
        if session['role'] != 'admin' and session['user_id'] != row['user_id']:
            return "Forbidden", 403

        cur.execute("DELETE FROM reminders WHERE id = %s", (reminder_id,))
        conn.commit()
        return 'Reminder deleted successfully'
    except Exception as e:
        print("❌ Delete Error:", e)
        return 'Failed to delete reminder'
    finally:
        cur.close()
        conn.close()

# ---------- Profile ----------
@app.route('/profile')
@login_required
def profile():
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name, email, date_joined FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            return "User not found", 404
        user = {
            "id": row['id'],
            "name": row['name'],
            "email": row['email'],
            "date_joined": row['date_joined'].strftime('%d %B %Y') if row['date_joined'] else ""
        }
        return render_template("profile.html", user=user)
    except Exception as e:
        print("❌ Profile Error:", e)
        return "Failed to load profile"
    finally:
        cur.close()
        conn.close()

@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if request.method == 'POST':
            new_name = request.form.get('name')
            new_email = request.form.get('email')
            new_password = request.form.get('password')

            if new_password:
                hashed_password = generate_password_hash(new_password)
                cur.execute(
                    "UPDATE users SET name=%s, email=%s, password=%s WHERE id=%s",
                    (new_name, new_email, hashed_password, user_id)
                )
            else:
                cur.execute(
                    "UPDATE users SET name=%s, email=%s WHERE id=%s",
                    (new_name, new_email, user_id)
                )
            conn.commit()
            return redirect(url_for('profile'))

        cur.execute("SELECT id, name, email FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        user = {"id": row['id'], "name": row['name'], "email": row['email']}
        return render_template('edit_profile.html', user=user)
    except Exception as e:
        print("❌ Edit Profile Error:", e)
        return "Failed to update profile"
    finally:
        cur.close()
        conn.close()

# ---------- Chatbot ----------
@app.route('/chatbot', methods=['POST'])
@login_required
def chatbot():
    user_msg = request.json.get('message')
    try:
        response = openai.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are StudBot, a smart and friendly assistant inside a student dashboard app called StudApp. Help students with reminders, study tips, motivation, academic queries, and anything related to their student life. Keep answers short."},
                {"role": "user", "content": user_msg}
            ]
        )
        reply = response.choices[0].message.content.strip()
        return {'response': reply}
    except Exception as e:
        print("❌ Groq API error:", e)
        return {'response': '❌ Failed to contact StudBot'}, 500

# ---------- Notes Summarizer ----------
@app.route('/notes_summarizer')
@login_required
def notes_summarizer():
    if session['role'] == 'admin':
        return "Admins cannot summarize notes", 403
    return render_template("notes_summarizer.html")

def extract_text_by_page_pdf(pdf_file):
    reader = PdfReader(pdf_file)
    return [(i + 1, page.extract_text().strip()) for i, page in enumerate(reader.pages) if page.extract_text()]

def extract_text_from_docx(docx_file):
    document = Document(docx_file)
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    return [(i + 1, para) for i, para in enumerate(paragraphs)]

def summarize_with_groq(text):
    try:
        response = openai.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are StudBot. Summarize the following notes into short bullet points or key takeaways:"},
                {"role": "user", "content": text}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"❌ Summary error: {str(e)}"

@app.route('/summarize_notes_all_pages', methods=['POST'])
@login_required
def summarize_notes_all_pages():
    if 'file' not in request.files:
        return jsonify({'pages': ['❌ No file provided']})

    file = request.files['file']
    filename = file.filename

    if not filename.lower().endswith(('.pdf', '.doc', '.docx')):
        return jsonify({'pages': ['❌ Unsupported file type']})

    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            file.save(temp.name)
            pages = extract_text_by_page_pdf(temp.name) if filename.endswith('.pdf') else extract_text_from_docx(temp.name)
            summaries = [summarize_with_groq(text).strip() for _, text in pages]
            return jsonify({'pages': summaries})
    except Exception as e:
        return jsonify({'pages': [f"❌ Error: {str(e)}"]})
    finally:
        if 'temp' in locals() and os.path.exists(temp.name):
            os.remove(temp.name)

# ---------- Run App ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
