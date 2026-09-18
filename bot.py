"""
Academy Lecture Bot
--------------------
- Admins upload PDF lectures tagged with "Subject | Type | Title"
  e.g. "Paediatric | Slides | Lecture 3 - Growth"
- Students browse Subject -> Type (Slides/A4/etc) -> Lecture via inline buttons
  and get the PDF sent back.
- Metadata (subject, type, title, file_id) is stored in a local SQLite database.
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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
            material_type TEXT NOT NULL,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            uploaded_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def add_lecture(subject: str, material_type: str, title: str, file_id: str, uploaded_by: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO lectures (subject, material_type, title, file_id, uploaded_by) VALUES (?, ?, ?, ?, ?)",
        (subject.strip(), material_type.strip(), title.strip(), file_id, uploaded_by),
    )
    conn.commit()
    conn.close()


def get_subjects():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT subject FROM lectures ORDER BY subject").fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_types_for_subject(subject: str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT material_type FROM lectures WHERE subject = ? ORDER BY material_type",
        (subject,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_lectures_for_subject_type(subject: str, material_type: str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title FROM lectures WHERE subject = ? AND material_type = ? ORDER BY id",
        (subject, material_type),
    ).fetchall()
    conn.close()
    return rows


def get_lecture(lecture_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT subject, material_type, title, file_id FROM lectures WHERE id = ?", (lecture_id,)
    ).fetchone()
    conn.close()
    return row


def search_lectures(keyword: str, limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, subject, material_type, title FROM lectures
           WHERE title LIKE ? OR subject LIKE ? OR material_type LIKE ? LIMIT ?""",
        (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit),
    ).fetchall()
    conn.close()
    return rows


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
        "SELECT id, subject, material_type, title FROM lectures ORDER BY subject, material_type, id"
    ).fetchall()
    conn.close()
    return rows


def get_latest_lectures(limit: int = 5):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, subject, material_type, title FROM lectures ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Student-facing handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Welcome! 📚\n\n"
        "/subjects — browse lectures by subject\n"
        "/latest — see the most recently added lectures\n"
        "/search [keyword] — find a lecture by name\n"
    )
    if is_admin(update.effective_user.id):
        text += (
            "\nYou're an admin. Extra commands:\n"
            "/list — see every lecture with its ID\n"
            "/delete [id] — remove a lecture\n\n"
            "To upload a lecture, send me a PDF with a caption "
            "formatted like:\n<code>Paediatric | Slides | Lecture 3 - Growth</code>\n\n"
            "You can send several PDFs one after another, each with its own caption — "
            "there's no need to wait between uploads."
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def latest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_latest_lectures(5)
    if not rows:
        await update.message.reply_text("No lectures uploaded yet.")
        return
    keyboard = [
        [InlineKeyboardButton(f"{subject} - {mtype} - {title}", callback_data=f"lec:{lid}")]
        for lid, subject, mtype, title in rows
    ]
    await update.message.reply_text(
        "🆕 Most recently added:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = get_all_lectures()
    if not rows:
        await update.message.reply_text("No lectures uploaded yet.")
        return
    lines = [f"#{lid} — {subject} — {mtype} — {title}" for lid, subject, mtype, title in rows]
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
    keyboard = [
        [InlineKeyboardButton(subject, callback_data=f"subj:{subject}")]
        for subject in subjects
    ]
    await update.message.reply_text(
        "Choose a subject:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search [keyword]")
        return
    keyword = " ".join(context.args)
    results = search_lectures(keyword)
    if not results:
        await update.message.reply_text("No matching lectures found.")
        return
    keyboard = [
        [InlineKeyboardButton(f"{subject} - {mtype} - {title}", callback_data=f"lec:{lid}")]
        for lid, subject, mtype, title in results
    ]
    await update.message.reply_text(
        f"Results for '{keyword}':", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("subj:"):
        subject = data[len("subj:"):]
        types = get_types_for_subject(subject)
        if not types:
            await query.edit_message_text("No materials found for this subject.")
            return
        keyboard = [
            [InlineKeyboardButton(mtype, callback_data=f"type:{subject}|{mtype}")]
            for mtype in types
        ]
        keyboard.append([InlineKeyboardButton("Back to subjects", callback_data="back:subjects")])
        await query.edit_message_text(
            f"{subject} — choose a type:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("type:"):
        subject, mtype = data[len("type:"):].split("|", 1)
        lectures = get_lectures_for_subject_type(subject, mtype)
        if not lectures:
            await query.edit_message_text("No lectures found here.")
            return
        keyboard = [
            [InlineKeyboardButton(title, callback_data=f"lec:{lid}")]
            for lid, title in lectures
        ]
        keyboard.append([InlineKeyboardButton("Back", callback_data=f"subj:{subject}")])
        await query.edit_message_text(
            f"{subject} — {mtype} — choose a lecture:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("lec:"):
        lecture_id = int(data[len("lec:"):])
        row = get_lecture(lecture_id)
        if not row:
            await query.edit_message_text("Sorry, that lecture is no longer available.")
            return
        subject, mtype, title, file_id = row
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=file_id,
        )

    elif data == "back:subjects":
        subjects = get_subjects()
        keyboard = [
            [InlineKeyboardButton(subject, callback_data=f"subj:{subject}")]
            for subject in subjects
        ]
        await query.edit_message_text(
            "Choose a subject:", reply_markup=InlineKeyboardMarkup(keyboard)
        )


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
    if not caption or caption.count("|") != 2:
        await update.message.reply_text(
            "Please add a caption formatted like:\n"
            "Paediatric | Slides | Lecture 3 - Growth\n\n"
            "Resend the PDF with that caption."
        )
        return

    subject, mtype, title = caption.split("|", 2)
    add_lecture(subject, mtype, title, doc.file_id, user_id)
    await update.message.reply_text(
        f"Saved!\nSubject: {subject.strip()}\nType: {mtype.strip()}\nTitle: {title.strip()}"
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subjects", subjects_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("latest", latest_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()