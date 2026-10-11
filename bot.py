"""
Academy Lecture Bot
--------------------
- Admins upload PDF lectures tagged with "Subject | Chapter | Type | Title"
  e.g. "Paediatric | Growth | Slides | Lecture 3 - Growth"
  (The old format "Subject | Type | Title" still works; the chapter is then "General".)
- Students browse Subject -> Chapter -> Type (Slides/A4/etc) -> Lecture via a
  persistent button keyboard, and get the PDF sent back.
- Metadata (subject, chapter, type, title, file_id) is stored in a local SQLite database.
  The PDF bytes themselves stay on Telegram's servers; we only remember the file_id.
 
Setup:
  1. pip install -r requirements.txt
  2. Copy .env.example to .env and fill in BOT_TOKEN and ADMIN_IDS
  3. python bot.py
"""
 
import os
import sqlite3
import logging
from dotenv import load_dotenv
 
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
 
# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
 
load_dotenv()
 
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", "lectures.db")
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
 
 
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
 
 
def chunk_buttons(buttons, per_row: int = 2):
    """Arrange a flat list of InlineKeyboardButtons into rows of `per_row`."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
 
 
BACK_LABEL = "⬅️ Back"
CLOSE_LABEL = "🏠 Close menu"
 
 
def build_reply_keyboard(labels, per_row: int = 2, show_back: bool = True):
    """Build a persistent keyboard (sits above the text box) from a list of labels."""
    rows = [labels[i:i + per_row] for i in range(0, len(labels), per_row)]
    footer = [BACK_LABEL, CLOSE_LABEL] if show_back else [CLOSE_LABEL]
    rows.append(footer)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)
 
 
# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
 
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lectures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL DEFAULT 'General',
            material_type TEXT NOT NULL,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            uploaded_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Migration: add columns to databases created before these features existed.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(lectures)").fetchall()}
    if "download_count" not in existing_columns:
        conn.execute("ALTER TABLE lectures ADD COLUMN download_count INTEGER NOT NULL DEFAULT 0")
    if "chapter" not in existing_columns:
        conn.execute("ALTER TABLE lectures ADD COLUMN chapter TEXT NOT NULL DEFAULT 'General'")
    conn.commit()
    conn.close()
 
 
def add_lecture(subject: str, chapter: str, material_type: str, title: str, file_id: str, uploaded_by: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO lectures (subject, chapter, material_type, title, file_id, uploaded_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (subject.strip(), chapter.strip(), material_type.strip(), title.strip(), file_id, uploaded_by),
    )
    conn.commit()
    conn.close()
 
 
def get_subjects():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT subject FROM lectures ORDER BY subject").fetchall()
    conn.close()
    return [r[0] for r in rows]
 
 
def get_chapters_for_subject(subject: str):
    """Chapters appear in the order they were first uploaded."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT chapter FROM lectures WHERE subject = ? GROUP BY chapter ORDER BY MIN(id)",
        (subject,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]
 
 
def get_types_for_subject_chapter(subject: str, chapter: str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT material_type FROM lectures WHERE subject = ? AND chapter = ? "
        "ORDER BY material_type",
        (subject, chapter),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]
 
 
def get_lectures_for_subject_chapter_type(subject: str, chapter: str, material_type: str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title FROM lectures WHERE subject = ? AND chapter = ? AND material_type = ? "
        "ORDER BY id",
        (subject, chapter, material_type),
    ).fetchall()
    conn.close()
    return rows  # list of (id, title)
 
 
def get_lecture(lecture_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT subject, chapter, material_type, title, file_id FROM lectures WHERE id = ?",
        (lecture_id,),
    ).fetchone()
    conn.close()
    return row  # (subject, chapter, material_type, title, file_id) or None
 
 
def increment_download_count(lecture_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE lectures SET download_count = download_count + 1 WHERE id = ?", (lecture_id,)
    )
    conn.commit()
    conn.close()
 
 
def get_most_downloaded(limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, subject, material_type, title, download_count FROM lectures
           ORDER BY download_count DESC, id LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows  # list of (id, subject, material_type, title, download_count)
 
 
def search_lectures(keyword: str, limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    like = f"%{keyword}%"
    rows = conn.execute(
        """SELECT id, subject, material_type, title FROM lectures
           WHERE title LIKE ? OR subject LIKE ? OR material_type LIKE ? OR chapter LIKE ?
           LIMIT ?""",
        (like, like, like, like, limit),
    ).fetchall()
    conn.close()
    return rows  # list of (id, subject, material_type, title)
 
 
def update_lecture_field(lecture_id: int, field: str, value: str) -> bool:
    column = "material_type" if field == "type" else field
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        f"UPDATE lectures SET {column} = ? WHERE id = ?", (value.strip(), lecture_id)
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated
 
 
def delete_lecture(lecture_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM lectures WHERE id = ?", (lecture_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted
 
 
def get_all_lectures():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, subject, chapter, material_type, title, download_count FROM lectures
           ORDER BY subject, chapter, material_type, id"""
    ).fetchall()
    conn.close()
    return rows  # list of (id, subject, chapter, material_type, title, download_count)
 
 
def get_latest_lectures(limit: int = 5):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, subject, material_type, title FROM lectures ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows  # list of (id, subject, material_type, title)
 
 
# ---------------------------------------------------------------------------
# Student-facing handlers
# ---------------------------------------------------------------------------
 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Welcome! 📚\n\n"
        "/subjects — browse lectures by subject (opens a button menu; "
        "tap 🏠 Close menu when you're done)\n"
        "/latest — see the most recently added lectures\n"
        "/search [keyword] — find a lecture by name\n"
    )
    if is_admin(update.effective_user.id):
        text += (
            "\nYou're an admin. Extra commands:\n"
            "/list — see every lecture with its ID and download count\n"
            "/popular — see your top 10 most downloaded lectures\n"
            "/edit [id] [subject|chapter|type|title] [new value] — fix a lecture's details\n"
            "/delete [id] — remove a lecture\n\n"
            "To upload a lecture, send me a PDF with a caption "
            "formatted like:\n<code>Paediatric | Growth | Slides | Lecture 3</code>\n\n"
            "You can send several PDFs one after another, each with its own caption — "
            "there's no need to wait between uploads."
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
 
 
async def latest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_latest_lectures(5)
    if not rows:
        await update.message.reply_text("No lectures uploaded yet.")
        return
    buttons = [
        InlineKeyboardButton(f"{subject} - {mtype} - {title}", callback_data=f"lec:{lid}")
        for lid, subject, mtype, title in rows
    ]
    await update.message.reply_text(
        "🆕 Most recently added:", reply_markup=InlineKeyboardMarkup(chunk_buttons(buttons))
    )
 
 
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = get_all_lectures()
    if not rows:
        await update.message.reply_text("No lectures uploaded yet.")
        return
    lines = [
        f"#{lid} — {subject} — {chapter} — {mtype} — {title} ({count} downloads)"
        for lid, subject, chapter, mtype, title, count in rows
    ]
    # Telegram messages max out around 4096 characters; chunk if the list is long.
    chunk = []
    length = 0
    for line in lines:
        if length + len(line) + 1 > 3500:
            await update.message.reply_text("\n".join(chunk))
            chunk = []
            length = 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        await update.message.reply_text("\n".join(chunk))
 
 
async def subjects_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subjects = get_subjects()
    if not subjects:
        await update.message.reply_text("No lectures uploaded yet.")
        return
    context.user_data["nav_level"] = "subjects"
    context.user_data.pop("nav_subject", None)
    context.user_data.pop("nav_chapter", None)
    context.user_data.pop("nav_type", None)
    await update.message.reply_text(
        "Choose a subject:", reply_markup=build_reply_keyboard(subjects, show_back=False)
    )
 
 
async def nav_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the persistent subject/chapter/type/lecture keyboard (plain text messages)."""
    text = update.message.text
    level = context.user_data.get("nav_level")
 
    if not level:
        return  # No active menu session; ignore stray text.
 
    if text == CLOSE_LABEL:
        context.user_data.clear()
        await update.message.reply_text("Menu closed.", reply_markup=ReplyKeyboardRemove())
        return
 
    if text == BACK_LABEL:
        if level == "lectures":
            subject = context.user_data.get("nav_subject")
            chapter = context.user_data.get("nav_chapter")
            types = get_types_for_subject_chapter(subject, chapter)
            context.user_data["nav_level"] = "types"
            context.user_data.pop("nav_type", None)
            await update.message.reply_text(
                f"{subject} — {chapter} — choose a type:",
                reply_markup=build_reply_keyboard(types),
            )
        elif level == "types":
            subject = context.user_data.get("nav_subject")
            chapters = get_chapters_for_subject(subject)
            context.user_data["nav_level"] = "chapters"
            context.user_data.pop("nav_chapter", None)
            await update.message.reply_text(
                f"{subject} — choose a chapter:", reply_markup=build_reply_keyboard(chapters)
            )
        elif level == "chapters":
            subjects = get_subjects()
            context.user_data["nav_level"] = "subjects"
            context.user_data.pop("nav_subject", None)
            await update.message.reply_text(
                "Choose a subject:", reply_markup=build_reply_keyboard(subjects, show_back=False)
            )
        return
 
    if level == "subjects":
        subjects = get_subjects()
        if text in subjects:
            context.user_data["nav_subject"] = text
            context.user_data["nav_level"] = "chapters"
            chapters = get_chapters_for_subject(text)
            await update.message.reply_text(
                f"{text} — choose a chapter:", reply_markup=build_reply_keyboard(chapters)
            )
 
    elif level == "chapters":
        subject = context.user_data.get("nav_subject")
        chapters = get_chapters_for_subject(subject)
        if text in chapters:
            context.user_data["nav_chapter"] = text
            context.user_data["nav_level"] = "types"
            types = get_types_for_subject_chapter(subject, text)
            await update.message.reply_text(
                f"{subject} — {text} — choose a type:",
                reply_markup=build_reply_keyboard(types),
            )
 
    elif level == "types":
        subject = context.user_data.get("nav_subject")
        chapter = context.user_data.get("nav_chapter")
        types = get_types_for_subject_chapter(subject, chapter)
        if text in types:
            context.user_data["nav_type"] = text
            context.user_data["nav_level"] = "lectures"
            lectures = get_lectures_for_subject_chapter_type(subject, chapter, text)
            titles = [title for _, title in lectures]
            await update.message.reply_text(
                f"{subject} — {chapter} — {text} — choose a lecture:",
                reply_markup=build_reply_keyboard(titles),
            )
 
    elif level == "lectures":
        subject = context.user_data.get("nav_subject")
        chapter = context.user_data.get("nav_chapter")
        mtype = context.user_data.get("nav_type")
        lectures = get_lectures_for_subject_chapter_type(subject, chapter, mtype)
        match = next((lid for lid, title in lectures if title == text), None)
        if match:
            row = get_lecture(match)
            file_id = row[4]
            await context.bot.send_document(chat_id=update.effective_chat.id, document=file_id)
            increment_download_count(match)
 
 
async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search [keyword]")
        return
    keyword = " ".join(context.args)
    results = search_lectures(keyword)
    if not results:
        await update.message.reply_text("No matching lectures found.")
        return
    buttons = [
        InlineKeyboardButton(f"{subject} - {mtype} - {title}", callback_data=f"lec:{lid}")
        for lid, subject, mtype, title in results
    ]
    await update.message.reply_text(
        f"Results for '{keyword}':", reply_markup=InlineKeyboardMarkup(chunk_buttons(buttons))
    )
 
 
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on inline buttons from /latest and /search results."""
    query = update.callback_query
    await query.answer()
    data = query.data
 
    if data.startswith("lec:"):
        lecture_id = int(data[len("lec:"):])
        row = get_lecture(lecture_id)
        if not row:
            await query.edit_message_text("Sorry, that lecture is no longer available.")
            return
        file_id = row[4]
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=file_id,
        )
        increment_download_count(lecture_id)
 
 
# ---------------------------------------------------------------------------
# Admin-facing handlers
# ---------------------------------------------------------------------------
 
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(
            "Sorry, only academy admins can upload lectures."
        )
        return
 
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Please send a PDF file.")
        return
 
    caption = update.message.caption
    # New format has 3 separators (Subject | Chapter | Type | Title).
    # The old format with 2 separators (Subject | Type | Title) still works.
    if not caption or caption.count("|") not in (2, 3):
        await update.message.reply_text(
            "Please add a caption formatted like:\n"
            "Paediatric | Growth | Slides | Lecture 3\n\n"
            "(Subject | Chapter | Type | Title)\n"
            "Resend the PDF with that caption."
        )
        return
 
    parts = [p.strip() for p in caption.split("|")]
    if len(parts) == 3:
        subject, mtype, title = parts
        chapter = "General"
    else:
        subject, chapter, mtype, title = parts
 
    if not all([subject, chapter, mtype, title]):
        await update.message.reply_text(
            "None of the four parts can be empty:\n"
            "Subject | Chapter | Type | Title"
        )
        return
 
    add_lecture(subject, chapter, mtype, title, doc.file_id, user_id)
    await update.message.reply_text(
        f"Saved!\nSubject: {subject}\nChapter: {chapter}\nType: {mtype}\nTitle: {title}"
    )
 
 
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delete [lecture_id] (use /search to find IDs)")
        return
    lecture_id = int(context.args[0])
    if delete_lecture(lecture_id):
        await update.message.reply_text(f"Deleted lecture #{lecture_id}.")
    else:
        await update.message.reply_text("No lecture with that ID.")
 
 
async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram user ID is: {update.effective_user.id}")
 
 
async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    valid_fields = ("subject", "chapter", "type", "title")
    if len(args) < 3 or not args[0].isdigit() or args[1].lower() not in valid_fields:
        await update.message.reply_text(
            "Usage: /edit [id] [subject|chapter|type|title] [new value]\n"
            "Example: /edit 5 chapter Growth\n"
            "Example: /edit 5 title Lecture 2 - Updated Growth Chart\n\n"
            "Use /list or /search to find a lecture's ID."
        )
        return
 
    lecture_id = int(args[0])
    field = args[1].lower()
    new_value = " ".join(args[2:]).strip()
    if not new_value:
        await update.message.reply_text("The new value can't be empty.")
        return
 
    row = get_lecture(lecture_id)
    if not row:
        await update.message.reply_text("No lecture with that ID.")
        return
 
    subject, chapter, mtype, title, _file_id = row
    old_value = {"subject": subject, "chapter": chapter, "type": mtype, "title": title}[field]
 
    update_lecture_field(lecture_id, field, new_value)
    await update.message.reply_text(
        f"Updated lecture #{lecture_id}.\n"
        f"{field.capitalize()}: \"{old_value}\" → \"{new_value}\""
    )
 
 
async def popular_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = get_most_downloaded(10)
    if not rows:
        await update.message.reply_text("No lectures uploaded yet.")
        return
    lines = [
        f"#{lid} — {subject} — {mtype} — {title}: {count} downloads"
        for lid, subject, mtype, title, count in rows
    ]
    await update.message.reply_text("📊 Most downloaded:\n" + "\n".join(lines))
 
 
# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
 
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Main menu"),
        BotCommand("subjects", "Browse subjects"),
        BotCommand("search", "Search lectures"),
        BotCommand("latest", "Latest uploads"),
    ])
 
 
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
 
    init_db()
 
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
 
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subjects", subjects_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("latest", latest_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("popular", popular_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, nav_handler))
 
    logger.info("Bot starting...")
    app.run_polling()
 
 
if __name__ == "__main__":
    main()