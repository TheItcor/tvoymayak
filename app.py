import base64
import json
import os
from datetime import date, datetime
from functools import wraps

import openai
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tvoymaak-secret-2024")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///tvoymaak.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max upload

db = SQLAlchemy(app)


load_dotenv()

# Yandex AI client
ai_client = openai.OpenAI(
    api_key=os.getenv("YANDEX_API_KEY"),
    base_url=os.getenv("YANDEX_BASE_URL"),
    project=os.getenv("YANDEX_PROJECT"),
)

PROMPT_ID = "fvt3d23jdon27pet2o0v"

# ─── Models ───────────────────────────────────────────────────────────────────


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    days = db.relationship(
        "DayEntry", backref="user", lazy=True, cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class DayEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    mood = db.Column(db.Integer, nullable=True)  # 1-5
    description = db.Column(db.Text, nullable=True)
    checkboxes = db.Column(db.Text, nullable=True)  # JSON array
    photo_data = db.Column(db.Text, nullable=True)  # base64
    ai_summary = db.Column(db.Text, nullable=True)
    ai_recommendation = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self):
        return {
            "id": self.id,
            "entry_date": self.entry_date.isoformat(),
            "mood": self.mood,
            "description": self.description,
            "checkboxes": json.loads(self.checkboxes) if self.checkboxes else [],
            "photo_data": self.photo_data,
            "ai_summary": self.ai_summary,
            "ai_recommendation": self.ai_recommendation,
        }


# ─── Auth decorator ───────────────────────────────────────────────────────────


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


# ─── AI helpers ───────────────────────────────────────────────────────────────


def ask_ai(message: str) -> str:
    try:
        response = ai_client.responses.create(
            prompt={"id": PROMPT_ID},
            input=message,
        )
        return response.output_text
    except Exception as e:
        return f"[ИИ временно недоступен: {str(e)[:80]}]"


def generate_day_summary(description: str, mood: int, checkboxes: list) -> str:
    mood_labels = {1: "ужасно", 2: "плохо", 3: "нейтрально", 4: "хорошо", 5: "отлично"}
    done = [c["label"] for c in checkboxes if c.get("done")]
    prompt = (
        f"Пользователь заполнил дневник. Настроение: {mood_labels.get(mood, '?')}. "
        f"Выполненные задачи: {', '.join(done) if done else 'нет'}. "
        f"Описание дня: {description or 'не заполнено'}. "
        "Напиши краткое тёплое саммари дня (2-3 предложения), как будто ты дружелюбный ИИ-дневник. "
        "Используй эмпатию. Не используй markdown."
    )
    return ask_ai(prompt)


def generate_recommendation(description: str, mood: int) -> str:
    if mood in (1, 2):
        prompt = (
            f"Пользователю сегодня {('ужасно' if mood == 1 else 'плохо')}. "
            f"Описание: {description or 'не указано'}. "
            "Предложи практическую поддержку: технику дыхания или короткую медитацию (опиши шаги), "
            "и одну тёплую рекомендацию на вечер. Пиши как заботливый друг. Без markdown."
        )
    else:
        prompt = (
            f"Пользователю сегодня {['нейтрально', 'хорошо', 'отлично'][mood - 3]}. "
            f"Описание: {description or 'не указано'}. "
            "Дай одну вдохновляющую рекомендацию или инсайт на основе его дня (2-3 предложения). Без markdown."
        )
    return ask_ai(prompt)


def generate_weekly_insight(entries: list) -> str:
    if not entries:
        return None
    moods = [e.mood for e in entries if e.mood]
    avg = sum(moods) / len(moods) if moods else 3
    descriptions = ". ".join([e.description for e in entries if e.description])
    prompt = (
        f"За неделю пользователь сделал {len(entries)} записей. "
        f"Средний балл настроения: {avg:.1f}/5. "
        f"Краткие заметки: {descriptions[:500]}. "
        "Напиши инсайт недели (3-4 предложения): что было хорошо, что можно улучшить. "
        "Тон: тёплый, поддерживающий. Начни с короткого заголовка (например 'Ты стал спокойнее 🌙'). Без markdown."
    )
    return ask_ai(prompt)


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    if "user_id" in session:
        return render_template("app.html")
    return render_template("index.html")


@app.route("/api/register", methods=["POST"])
def register():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Заполни все поля"}), 400
    if len(username) < 3:
        return jsonify({"error": "Имя минимум 3 символа"}), 400
    if len(password) < 6:
        return jsonify({"error": "Пароль минимум 6 символов"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Такой пользователь уже существует"}), 400

    user = User(username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    session["user_id"] = user.id
    session["username"] = user.username
    return jsonify({"ok": True, "username": user.username})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    user = User.query.filter_by(username=data.get("username", "").strip()).first()
    if not user or not user.check_password(data.get("password", "")):
        return jsonify({"error": "Неверное имя или пароль"}), 401
    session["user_id"] = user.id
    session["username"] = user.username
    return jsonify({"ok": True, "username": user.username})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
@login_required
def me():
    user = User.query.get(session["user_id"])
    streak = get_streak(user.id)
    return jsonify({"username": user.username, "streak": streak})


def get_streak(user_id):
    entries = (
        DayEntry.query.filter_by(user_id=user_id)
        .order_by(DayEntry.entry_date.desc())
        .all()
    )
    if not entries:
        return 0
    streak = 0
    today = date.today()
    for i, e in enumerate(entries):
        expected = today - __import__("datetime").timedelta(days=i)
        if e.entry_date == expected:
            streak += 1
        else:
            break
    return streak


@app.route("/api/today", methods=["GET"])
@login_required
def get_today():
    today = date.today()
    entry = DayEntry.query.filter_by(
        user_id=session["user_id"], entry_date=today
    ).first()
    if entry:
        return jsonify(entry.to_dict())
    return jsonify(None)


@app.route("/api/today", methods=["POST"])
@login_required
def save_today():
    data = request.json
    today = date.today()
    entry = DayEntry.query.filter_by(
        user_id=session["user_id"], entry_date=today
    ).first()
    if not entry:
        entry = DayEntry(user_id=session["user_id"], entry_date=today)
        db.session.add(entry)

    entry.mood = data.get("mood")
    entry.description = data.get("description", "")
    entry.checkboxes = json.dumps(data.get("checkboxes", []))
    if data.get("photo_data"):
        entry.photo_data = data["photo_data"]

    # Generate AI content
    checkboxes = data.get("checkboxes", [])
    entry.ai_summary = generate_day_summary(entry.description, entry.mood, checkboxes)
    entry.ai_recommendation = generate_recommendation(entry.description, entry.mood)

    db.session.commit()
    return jsonify(entry.to_dict())


@app.route("/api/feed")
@login_required
def feed():
    entries = (
        DayEntry.query.filter_by(user_id=session["user_id"])
        .order_by(DayEntry.entry_date.desc())
        .limit(30)
        .all()
    )

    result = [e.to_dict() for e in entries]

    # Weekly insight (last 7 days)
    week_entries = entries[:7]
    weekly_insight = generate_weekly_insight(week_entries) if week_entries else None

    return jsonify({"entries": result, "weekly_insight": weekly_insight})


@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    """AI emotional support chat"""
    data = request.json
    message = data.get("message", "")
    mood = data.get("mood", 3)
    history = data.get("history", [])

    context = (
        f"Ты — заботливый ИИ-дневник по имени Маяк. Пользователю сейчас "
        f"{'плохо' if mood <= 2 else 'нормально'}. "
        "Помогай ему осмыслить чувства, предлагай практики (дыхание, медитация). "
        "Отвечай коротко (2-4 предложения), тепло, как друг. Без markdown.\n\n"
    )
    if history:
        for h in history[-6:]:
            role = "Пользователь" if h["role"] == "user" else "Маяк"
            context += f"{role}: {h['content']}\n"
    context += f"Пользователь: {message}\nМаяк:"

    reply = ask_ai(context)
    return jsonify({"reply": reply})


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000, debug=True)
