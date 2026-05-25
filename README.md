# StudApp

StudApp is a Flask + PostgreSQL student productivity app with login/signup, student reminders, an admin dashboard, profile editing, StudBot chat, and AI notes summarization.

## Features

- Student and admin sessions
- Account revocation and restoration from the admin dashboard
- Reminder creation with date, time, priority, and completion state
- Calendar and daily task views
- Profile viewing and password updates
- StudBot chatbot powered by the Groq OpenAI-compatible API
- PDF/DOCX notes summarization with copy and download actions

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file:

```env
SECRET_KEY=replace-with-a-long-random-secret
DB_HOST=localhost
DB_NAME=studapp
DB_USER=postgres
DB_PASS=your-password
GROQ_API_KEY=your-groq-key
FLASK_DEBUG=true
```

4. Run the app:

```bash
python app.py
```

The app creates or upgrades the required `users` and `reminders` tables when database-backed routes are used.

## Production Notes

- Set `FLASK_DEBUG=false`.
- Use a strong fixed `SECRET_KEY`.
- Set `SESSION_COOKIE_SECURE=true` when serving over HTTPS.
- Keep `.env` out of source control.
