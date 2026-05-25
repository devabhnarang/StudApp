import os
import tempfile
import secrets
import logging
from functools import wraps
from flask import Flask, request, render_template, redirect, jsonify, session, url_for
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
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
logging.basicConfig(level=logging.INFO)

# Secret key for session management
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)

ALLOWED_PRIORITIES = {"low", "medium", "high"}
ALLOWED_NOTE_EXTENSIONS = {".pdf", ".docx"}
_schema_ready = False

# ---------- PostgreSQL Connection Helper ----------
def get_db_connection():
    return psycopg2.connect(
        host=os.getenv('DB_HOST'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASS'),
        cursor_factory=RealDictCursor
    )

def ensure_database_schema():
    """Create or upgrade the small schema this app expects."""
    global _schema_ready
    if _schema_ready:
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                status TEXT NOT NULL DEFAULT 'active',
                date_joined TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT,
                date DATE NOT NULL,
                time TIME NOT NULL,
                priority TEXT NOT NULL DEFAULT 'medium',
                completed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS date_joined TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
        cur.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'medium'")
        cur.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS completed BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
        conn.commit()
        _schema_ready = True
    except Exception:
        conn.rollback()
        logging.exception("Database schema setup failed")
        raise
    finally:
        cur.close()
        conn.close()

def db_cursor():
    ensure_database_schema()
    conn = get_db_connection()
    return conn, conn.cursor()

def wants_json():
    return request.headers.get("Accept", "").lower().find("application/json") >= 0

def json_message(status, message, http_status=200, **extra):
    payload = {"status": status, "message": message}
    payload.update(extra)
    return jsonify(payload), http_status

def session_user_is_active():
    if not session.get("user_id"):
        return False

    conn, cur = db_cursor()
    try:
        cur.execute(
            "SELECT name, role, COALESCE(status, 'active') AS status FROM users WHERE id = %s",
            (session["user_id"],)
        )
        user = cur.fetchone()
        if not user or user["status"] != "active":
            session.clear()
            return False

        session["name"] = user["name"]
        session["role"] = user["role"]
        return True
    except Exception:
        logging.exception("Session validation failed")
        return False
    finally:
        cur.close()
        conn.close()

# ---------- Groq API config ----------
openai.api_key = os.getenv("GROQ_API_KEY")
openai.base_url = 'https://api.groq.com/openai/v1/'
GROQ_MODEL = 'llama-3.3-70b-versatile'

# ---------- Decorators ----------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session_user_is_active():
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
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    role = 'student'

    if not name or not email or not password:
        return 'Please fill in all fields', 400
    if len(password) < 6:
        return 'Password must be at least 6 characters', 400

    hashed_password = generate_password_hash(password)

    conn, cur = db_cursor()
    try:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            return 'Email already exists', 409
        cur.execute(
            "INSERT INTO users (name, email, password, role, status) VALUES (%s, %s, %s, %s, 'active')",
            (name, email, hashed_password, role)
        )
        conn.commit()
        return 'Signup successful'
    except Exception as e:
        logging.exception("Signup Error")
        return 'Signup failed', 500
    finally:
        cur.close()
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')

    if not email or not password:
        return 'Please fill in all fields', 400

    conn, cur = db_cursor()
    try:
        cur.execute(
            "SELECT id, name, role, password, COALESCE(status, 'active') AS status FROM users WHERE email = %s",
            (email,)
        )
        user = cur.fetchone()
        if user and check_password_hash(user['password'], password):
            if user['status'] != 'active':
                return 'This account has been revoked. Contact an admin.', 403
            session['user_id'] = user['id']
            session['role'] = user['role']
            session['name'] = user['name']
            return "ok"
        return 'Invalid credentials'
    except Exception as e:
        logging.exception("Login Error")
        return 'Login error', 500
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
    conn, cur = db_cursor()
    try:
        cur.execute(
            """
            SELECT id, title, description, date::text, time::text,
                   COALESCE(priority, 'medium') AS priority,
                   COALESCE(completed, FALSE) AS completed
            FROM reminders
            WHERE user_id = %s
            ORDER BY date, time
            """,
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
        logging.exception("Student Dashboard Error")
        return 'Error loading dashboard', 500
    finally:
        cur.close()
        conn.close()

@app.route('/admin_dashboard')
@login_required
@admin_required
def admin_dashboard():
    user_id = session['user_id']
    conn, cur = db_cursor()
    try:
        cur.execute("""
            SELECT id, name, email, role,
                   COALESCE(status, 'active') AS status,
                   TO_CHAR(date_joined, 'DD Mon YYYY') AS date_joined
            FROM users
            ORDER BY id
        """)
        users = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM reminders")
        reminder_count = cur.fetchone()['count']

        cur.execute("""
            SELECT r.id, r.title, r.description, r.date::text, r.time::text,
                   COALESCE(r.priority, 'medium') AS priority,
                   COALESCE(r.completed, FALSE) AS completed,
                   u.name AS user_name
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
        logging.exception("Admin Dashboard Error")
        return 'Error loading dashboard', 500
    finally:
        cur.close()
        conn.close()

# ---------- Admin: Manage Users ----------

@app.route('/revoke_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def revoke_user(user_id):
    if user_id == session.get('user_id'):
        return json_message('error', 'You cannot revoke your own admin account', 400)

    conn, cur = db_cursor()
    try:
        cur.execute("UPDATE users SET status='revoked' WHERE id=%s", (user_id,))
        if cur.rowcount == 0:
            conn.rollback()
            return json_message('error', 'User not found', 404)
        conn.commit()
        return json_message('success', 'User access revoked', user_status='revoked')
    except Exception as e:
        logging.exception("Revoke User Error")
        return json_message('error', 'Failed to revoke user', 500)
    finally:
        cur.close()
        conn.close()

@app.route('/restore_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def restore_user(user_id):
    conn, cur = db_cursor()
    try:
        cur.execute("UPDATE users SET status='active' WHERE id=%s", (user_id,))
        if cur.rowcount == 0:
            conn.rollback()
            return json_message('error', 'User not found', 404)
        conn.commit()
        return json_message('success', 'User access restored', user_status='active')
    except Exception as e:
        logging.exception("Restore User Error")
        return json_message('error', 'Failed to restore user', 500)
    finally:
        cur.close()
        conn.close()



# ---------- Reminders ----------
@app.route('/add_reminder', methods=['POST'])
@login_required
def add_reminder():
    conn = None
    cur = None
    try:
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        date = request.form.get('date', '').strip()
        time = request.form.get('time', '').strip()
        priority = request.form.get('priority', 'medium').strip().lower()
        user_id = session['user_id']

        if not title or not date or not time:
            return json_message('error', 'Title, date, and time are required', 400)
        if priority not in ALLOWED_PRIORITIES:
            priority = 'medium'

        conn, cur = db_cursor()
        cur.execute(
            """
            INSERT INTO reminders (user_id, title, description, date, time, priority, completed)
            VALUES (%s, %s, %s, %s, %s, %s, FALSE)
            RETURNING id, date::text, time::text
            """,
            (user_id, title, description, date, time, priority)
        )
        reminder = cur.fetchone()
        conn.commit()
        return json_message(
            'success',
            'Reminder added successfully',
            reminder={
                'id': reminder['id'],
                'title': title,
                'description': description,
                'date': reminder['date'],
                'time': reminder['time'],
                'priority': priority,
                'completed': False,
            }
        )
    except Exception as e:
        logging.exception("Add Reminder Error")
        return json_message('error', 'Failed to add reminder', 500)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/delete_reminder/<int:reminder_id>', methods=['POST'])
@login_required
def delete_reminder(reminder_id):
    conn, cur = db_cursor()
    try:
        cur.execute("SELECT user_id FROM reminders WHERE id = %s", (reminder_id,))
        row = cur.fetchone()
        if not row:
            return json_message('error', 'Reminder not found', 404)
        if session['role'] != 'admin' and session['user_id'] != row['user_id']:
            return json_message('error', 'Forbidden', 403)

        cur.execute("DELETE FROM reminders WHERE id = %s", (reminder_id,))
        conn.commit()
        return json_message('success', 'Reminder deleted successfully')
    except Exception as e:
        logging.exception("Delete Reminder Error")
        return json_message('error', 'Failed to delete reminder', 500)
    finally:
        cur.close()
        conn.close()

@app.route('/toggle_reminder/<int:reminder_id>', methods=['POST'])
@login_required
def toggle_reminder(reminder_id):
    data = request.get_json(silent=True) or {}
    completed = bool(data.get('completed'))

    conn, cur = db_cursor()
    try:
        cur.execute("SELECT user_id FROM reminders WHERE id = %s", (reminder_id,))
        row = cur.fetchone()
        if not row:
            return json_message('error', 'Reminder not found', 404)
        if session['role'] != 'admin' and session['user_id'] != row['user_id']:
            return json_message('error', 'Forbidden', 403)

        cur.execute(
            "UPDATE reminders SET completed = %s WHERE id = %s",
            (completed, reminder_id)
        )
        conn.commit()
        return json_message('success', 'Reminder updated', completed=completed)
    except Exception:
        logging.exception("Toggle Reminder Error")
        return json_message('error', 'Failed to update reminder', 500)
    finally:
        cur.close()
        conn.close()

# ---------- Profile ----------
@app.route('/profile')
@login_required
def profile():
    user_id = session['user_id']
    conn, cur = db_cursor()
    try:
        cur.execute("""
            SELECT id, name, email, role,
                   TO_CHAR(date_joined, 'DD Mon YYYY') AS date_joined
            FROM users
            WHERE id = %s
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            return "User not found", 404
        user = {
            "id": row['id'],
            "name": row['name'],
            "email": row['email'],
            "role": row['role'],
            "date_joined": row['date_joined'] or ""
        }
        return render_template("profile.html", user=user)
    except Exception as e:
        logging.exception("Profile Error")
        return "Failed to load profile", 500
    finally:
        cur.close()
        conn.close()

@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    user_id = session['user_id']
    conn, cur = db_cursor()
    try:
        if request.method == 'POST':
            new_name = request.form.get('name', '').strip()
            new_password = request.form.get('password', '')

            if not new_name:
                return "Name is required", 400

            if new_password:
                if len(new_password) < 6:
                    return "Password must be at least 6 characters", 400
                hashed_password = generate_password_hash(new_password)
                cur.execute(
                    "UPDATE users SET name=%s, password=%s WHERE id=%s",
                    (new_name, hashed_password, user_id)
                )
            else:
                cur.execute(
                    "UPDATE users SET name=%s WHERE id=%s",
                    (new_name, user_id)
                )
            conn.commit()
            session['name'] = new_name
            return redirect(url_for('profile'))

        cur.execute("SELECT id, name, email FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        user = {"id": row['id'], "name": row['name'], "email": row['email']}
        return render_template('edit_profile.html', user=user)
    except Exception as e:
        logging.exception("Edit Profile Error")
        return "Failed to update profile", 500
    finally:
        cur.close()
        conn.close()

# ---------- Chatbot ----------
@app.route('/chatbot', methods=['POST'])
@login_required
def chatbot():
    payload = request.get_json(silent=True) or {}
    user_msg = (payload.get('message') or '').strip()
    if not user_msg:
        return {'response': 'Please type a message first.'}, 400
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
        logging.exception("Groq API error")
        return {'response': 'Failed to contact StudBot. Please try again later.'}, 500

# ---------- Notes Summarizer ----------
@app.route('/notes_summarizer')
@login_required
def notes_summarizer():
    if session['role'] == 'admin':
        return "Admins cannot summarize notes", 403
    return render_template("notes_summarizer.html")

def extract_text_by_page_pdf(pdf_file):
    reader = PdfReader(pdf_file)
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text = text.strip()
        if text:
            pages.append((i + 1, text))
    return pages

def extract_text_from_docx(docx_file):
    document = Document(docx_file)
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    return [(i + 1, para) for i, para in enumerate(paragraphs)]

def summarize_with_groq(text):
    text = text[:12000]
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
    if session['role'] == 'admin':
        return jsonify({'pages': ['Admins cannot summarize notes']}), 403

    if 'file' not in request.files:
        return jsonify({'pages': ['No file provided']}), 400

    file = request.files['file']
    filename = secure_filename(file.filename or "")
    extension = os.path.splitext(filename)[1].lower()

    if extension not in ALLOWED_NOTE_EXTENSIONS:
        return jsonify({'pages': ['Unsupported file type. Please upload a PDF or DOCX file.']}), 400

    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            file.save(temp.name)
            pages = extract_text_by_page_pdf(temp.name) if extension == '.pdf' else extract_text_from_docx(temp.name)
            if not pages:
                return jsonify({'pages': ['No readable text found in this document. Try a text-based PDF or DOCX file.']}), 400
            summaries = [summarize_with_groq(text).strip() for _, text in pages]
            return jsonify({'pages': summaries})
    except Exception as e:
        logging.exception("Notes summary error")
        return jsonify({'pages': ['Error processing document. Please try another file.']}), 500
    finally:
        if 'temp' in locals() and os.path.exists(temp.name):
            os.remove(temp.name)

# ---------- Run App ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
