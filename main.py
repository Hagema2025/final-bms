import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import json
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram.helpers import escape_markdown
load_dotenv()  # Loads variables from your local .env file
import asyncio
import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from data_config import Theatres, Languages, Formats, TimePeriods,VENUE_MAP

# ======================================================================
# LOGGING SETUP
# ======================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)

# ======================================================================
# CONFIGURATION
# ======================================================================
load_dotenv()  # Loads variables from your local .env file

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

GITHUB_REPO_WATCHES = os.getenv("GITHUB_REPO_WATCHES")
GITHUB_TOKEN_WATCHES = os.getenv("GITHUB_TOKEN_WATCHES")
GITHUB_WATCHES_PATH = os.getenv("GITHUB_WATCHES_PATH")
GITHUB_WSTATE_PATH = os.getenv("GITHUB_WSTATE_PATH")
GITHUB_BRANCH_WATCHES = os.getenv("GITHUB_BRANCH_WATCHES")
GROUP_CHAT_ID_WATCHES = os.getenv("GROUP_CHAT_ID_WATCHES")


GITHUB_REPO_SHOWS = os.getenv("GITHUB_REPO_SHOWS")
GITHUB_TOKEN_SHOWS = os.getenv("GITHUB_TOKEN_SHOWS")
GITHUB_SHOWS_PATH = os.getenv("GITHUB_SHOWS_PATH")
GITHUB_SSTATE_PATH = os.getenv("GITHUB_SSTATE_PATH")
GITHUB_BRANCH_SHOWS = os.getenv("GITHUB_BRANCH_SHOWS")
GROUP_CHAT_ID_SHOWS = os.getenv("GROUP_CHAT_ID_SHOWS")

THEATRES_PER_PAGE = 6
WATCHES_PER_PAGE = 3  # 3 to 4 is ideal so full names fit comfortably on mobile

WATCHES_CACHE = None  # <--- ADD THIS
SHOWS_CACHE = None

# Conversation States
# Conversation States (Watches + Manual Shows)
# Conversation States (Watches + Manual Shows with Seat/Row Prefs)
(
    STATE_URL,
    STATE_LANGUAGE,
    STATE_FORMAT,
    STATE_THEATRE,
    STATE_DATE,
    STATE_CUSTOM_DATE,
    STATE_TIME,
    STATE_SHOW_URL,
    STATE_SHOW_THEATRE,  # <--- Added
    STATE_SHOW_NAME,
    STATE_SHOW_TIME,
    STATE_SHOW_SEAT_COUNT,
    STATE_SHOW_ADJACENT,
    STATE_SHOW_ROWS,
STATE_SHOW_ROW_SEATS
) = range(15)


# Parse comma-separated IDs into sets of integers
def parse_id_list(env_var: str) -> set[int]:
    raw = os.getenv(env_var, "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}

ALLOWED_USERS = parse_id_list("ALLOWED_USERS")

# ============================================================
# GITHUB JSON STORAGE
# ============================================================

GITHUB_API_BASE = "https://api.github.com"

def _github_headers(isWatch):
    token = GITHUB_TOKEN_WATCHES if isWatch else GITHUB_TOKEN_SHOWS
    return {
        "Authorization": f"Bearer {token}", 
        "Accept": "application/vnd.github+json", 
        "X-GitHub-Api-Version": "2022-11-28", 
        "User-Agent": "bms-telegram-bot"
    }

def _github_get_file(path, isWatch):
    repo = GITHUB_REPO_WATCHES if isWatch else GITHUB_REPO_SHOWS
    r = requests.get(f"{GITHUB_API_BASE}/repos/{repo}/contents/{path}", headers=_github_headers(isWatch), timeout=20)
    if r.status_code == 404: 
        return None, None
    r.raise_for_status()
    payload = r.json()
    raw = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(raw), payload.get("sha")


def _github_put_file(path, data, message, isWatch):
    repo = GITHUB_REPO_WATCHES if isWatch else GITHUB_REPO_SHOWS
    branch = GITHUB_BRANCH_WATCHES if isWatch else GITHUB_BRANCH_SHOWS
    url = f"{GITHUB_API_BASE}/repos/{repo}/contents/{path}"
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    
    for attempt in range(3):
        _, sha = _github_get_file(path, isWatch)
        body = {
            "message": message, 
            "content": base64.b64encode(content.encode()).decode(), 
            "branch": branch
        }
        if sha: 
            body["sha"] = sha
        r = requests.put(url, headers=_github_headers(isWatch), json=body, timeout=20)
        if r.status_code in (200, 201): 
            return
        if r.status_code == 409 and attempt < 2: 
            continue
        r.raise_for_status()
    raise RuntimeError(f"Could not update GitHub file: {path}")


def load_watches() -> list:
    global WATCHES_CACHE
    if WATCHES_CACHE is not None:
        return WATCHES_CACHE
    data, _ = _github_get_file(GITHUB_WATCHES_PATH, True)
    WATCHES_CACHE = data if isinstance(data, list) else []
    return WATCHES_CACHE

def save_watches(watches: list):
    global WATCHES_CACHE
    _github_put_file(GITHUB_WATCHES_PATH, watches, "Update BMS watches", True)
    WATCHES_CACHE = watches
    log.info("WATCHES SAVED TO GITHUB & CACHE UPDATED | count=%d", len(watches))


def load_shows() -> list:
    global SHOWS_CACHE
    if SHOWS_CACHE is not None:
        return SHOWS_CACHE
    data, _ = _github_get_file(GITHUB_SHOWS_PATH, False)
    SHOWS_CACHE = data if isinstance(data, list) else []
    return SHOWS_CACHE

def save_shows(shows: list):
    global SHOWS_CACHE
    _github_put_file(GITHUB_SHOWS_PATH, shows, "Update BMS shows", False)
    SHOWS_CACHE = shows

async def auto_delete_all_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Instantly deletes all service messages and system notifications."""
    msg = update.effective_message
    if not msg:
        return

    # Check if it's any form of service message
    is_service = (
        msg.new_chat_members or
        msg.left_chat_member or
        msg.pinned_message or
        msg.new_chat_title or
        msg.new_chat_photo or
        msg.delete_chat_photo or
        msg.forum_topic_created or 
        msg.forum_topic_closed or 
        msg.forum_topic_reopened or 
        msg.forum_topic_edited or
        msg.general_forum_topic_hidden or
        msg.general_forum_topic_unhidden
    )

    if is_service:
        try:
            await msg.delete()
            print(f"🗑️ Successfully deleted system service message in chat {update.effective_chat.id}")
        except Exception as e:
            print(f"⚠️ Failed to delete service message: {e}")

async def is_authorized(update: Update) -> bool:
    """Verifies if the incoming user ID is in ALLOWED_USERS."""
    user = update.effective_user
    if not user or user.id not in ALLOWED_USERS:
        user_id = user.id if user else "Unknown"
        user_name = user.full_name if user else "Unknown"
        
        log.warning(f"⛔ Unauthorized access attempt | ID: {user_id} ---> Name: {user_name}")

        # Optional: Send callback feedback to prevent Telegram UI freeze
        if update.callback_query:
            await update.callback_query.answer("⛔ Access Denied", show_alert=True)
            
        return False
    return True

async def background_delayed_pin(bot, chat_id, message_id,buffer_message_id=None):
    """Waits 3 seconds, then pins the message to bypass Telegram UI caching bugs."""
    await asyncio.sleep(3)
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=False)

        # 2. Delete the buffer message so the chat looks clean
        if buffer_message_id:
            await bot.delete_message(chat_id=chat_id, message_id=buffer_message_id)
    except Exception as e:
        log.error(f"Background pin failed: {e}")

async def auto_delete_pin_notification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listens for 'Bot pinned a message' service messages and instantly deletes them."""
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass
    
def build_watches_view(watches: list, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    if not watches:
        return "📭 No active watches found.", None

    total_pages = max(1, (len(watches) + WATCHES_PER_PAGE - 1) // WATCHES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * WATCHES_PER_PAGE
    end_idx = start_idx + WATCHES_PER_PAGE
    page_watches = watches[start_idx:end_idx]

    # 1. Build the text display with full, unclipped names
    lines = [f"📋 *Active Watches* (Page {page + 1}/{total_pages}):\n"]
    keyboard = []

    for offset, w in enumerate(page_watches):
        global_idx = start_idx + offset
        display_num = global_idx + 1
        full_name = w.get("name", f"Watch_{global_idx}")

        # Full name displayed in the message bubble
        lines.append(f"*{display_num}.* `{full_name}`")

        # Compact numbered action buttons
        keyboard.append([
            InlineKeyboardButton(f"🔍 Inspect {display_num}", callback_data=f"insp_{global_idx}"),
            InlineKeyboardButton(f"❌ Stop {display_num}", callback_data=f"stop_{global_idx}")
        ])

    # 2. Add pagination navigation row if there are multiple pages
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"wpage_{page - 1}"))
        
        nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"wpage_{page + 1}"))
            
        keyboard.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def build_shows_view(shows: list, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    if not shows:
        return "📭 No active shows found.", None
    total_pages = max(1, (len(shows) + WATCHES_PER_PAGE - 1) // WATCHES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * WATCHES_PER_PAGE
    page_shows = shows[start_idx:start_idx + WATCHES_PER_PAGE]

    lines = [f"📋 *Active Manual Shows* (Page {page + 1}/{total_pages}):\n"]
    keyboard = []
    for offset, s in enumerate(page_shows):
        global_idx = start_idx + offset
        display_num = global_idx + 1
        lines.append(
    f"*{display_num}.* `{s.get('name')}` | Theatre: `{s.get('theatre', s.get('venue_code'))}` (`{s.get('venue_code')}`) | Session: `{s.get('session_id')}`"
)        
        keyboard.append([
            InlineKeyboardButton(f"❌ Remove {display_num}", callback_data=f"delshow_{global_idx}")
        ])
    if total_pages > 1:
        nav_row = []
        if page > 0: nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"spage_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1: nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"spage_{page + 1}"))
        keyboard.append(nav_row)
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)

# RENDER HEALTH SERVER
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    # UptimeRobot relies on HEAD requests to check server status
    def do_HEAD(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy HTTP logs
        return


def start_health_server():
    """
    Render Web Services require the application to listen
    on the PORT supplied by Render.
    """

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            port,
        ),
        HealthHandler,
    )

    thread = Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    log.info(
        "Health server listening on 0.0.0.0:%s",
        port,
    )



# ======================================================================
# HELPER FUNCTIONS
# ======================================================================

def parse_seat_layout_url(url: str) -> dict:
    """Extracts venue, session, and date from a BMS seat layout URL."""
    path = urlparse(url).path.strip("/")
    parts = path.split("/")

    if "seat-layout" in parts:
        idx = parts.index("seat-layout")
        # Structure: .../seat-layout/{event_code}/{venue_code}/{session_id}/{date}
        if idx + 4 < len(parts):
            return {
                "event_code": parts[idx + 1],
                "venue_code": parts[idx + 2],
                "session_id": parts[idx + 3],
                "date": parts[idx + 4],
            }
    raise ValueError("Invalid URL: missing seat-layout information (venue code, session, or date).")

def parse_seat_preferences(text: str) -> list:
    text = text.strip().upper()
    if text in ("ANY", "ALL", ""):
        return []
    
    # Split the input into included and excluded parts
    include_text, exclude_text = text, ""
    if "EXCEPT" in text:
        include_text, exclude_text = text.split("EXCEPT", 1)
    elif "!" in text:
        include_text, exclude_text = text.split("!", 1)
        
    def expand_ranges(part: str) -> set:
        seats = set()
        for item in part.split(","):
            item = item.strip()
            if not item: continue
            
            # If it's a range like "1-30"
            if "-" in item:
                try:
                    start, end = map(int, item.split("-", 1))
                    if start <= end:
                        seats.update(range(start, end + 1))
                except ValueError:
                    pass # Ignore invalid ranges safely
            else:
                try:
                    seats.add(int(item))
                except ValueError:
                    pass
        return seats

    # Parse both sides, then subtract the excluded seats from the included seats
    include_seats = expand_ranges(include_text)
    exclude_seats = expand_ranges(exclude_text)
    
    final_seats = include_seats - exclude_seats
    
    # Return as a list of sorted string numbers (which your existing logic expects)
    return [str(s) for s in sorted(final_seats)]

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("➕ Add Watches", callback_data="menu_new_watch")],
        [InlineKeyboardButton("📋 View Active Watches", callback_data="menu_list_watches")],
        [InlineKeyboardButton("➕ Add Shows", callback_data="menu_new_show")],
        [InlineKeyboardButton("📋 View Active Shows", callback_data="menu_list_shows")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu_help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🍿 *BMS Ticket Watcher Dashboard*\n\nSelect an option below:"

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    
    if query.data == "menu_new_watch":
        await query.edit_message_text("🔗 *New Watch Setup*\n\nPlease paste the full *BookMyShow movie link*:", parse_mode=ParseMode.MARKDOWN)
        return STATE_URL
    elif query.data == "menu_list_watches":
        watches = load_watches()
        text, reply_markup = build_watches_view(watches, page=0)
        kb_list = list(reply_markup.inline_keyboard) if reply_markup and reply_markup.inline_keyboard else []
        kb_list.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_list), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    elif query.data == "menu_new_show":
        await query.edit_message_text(
            "🎬 *Add Manual Show*\n\nPlease paste the full *BookMyShow seat-layout URL*:\n\n"
            "_Example: https://in.bookmyshow.com/movies/chen/seat-layout/ET00442702/RAKK/3934/20260919_", 
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["show"] = {}
        return STATE_SHOW_URL
    elif query.data == "menu_list_shows":
        shows = load_shows()
        text, reply_markup = build_shows_view(shows, page=0)
        kb_list = list(reply_markup.inline_keyboard) if reply_markup and reply_markup.inline_keyboard else []
        kb_list.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_list), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    elif query.data == "menu_help":
        help_text = "ℹ️ *Instructions:*\nUse *Add Watches* for URL filters or *Add Shows* for direct manual session tracking."
        kb = [[InlineKeyboardButton("🏠 Back to Menu", callback_data="menu_main")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    elif query.data == "menu_main":
        await show_main_menu(update, context)
        return ConversationHandler.END


async def receive_show_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    try:
        parsed = parse_seat_layout_url(url)
        v_code = parsed["venue_code"].upper()
        theatre_name = VENUE_MAP.get(v_code)

        context.user_data["show"]["venue_code"] = v_code
        context.user_data["show"]["theatre"] = theatre_name or ""
        context.user_data["show"]["session_id"] = parsed["session_id"]
        context.user_data["show"]["date"] = parsed["date"]
        # --- ADD THIS LINE ---
        context.user_data["show"]["url"] = url

        if theatre_name:
            await update.message.reply_text(
                f"✅ *Extracted Data:*\n"
                f"🏛️ Theatre: `{theatre_name}` (`{v_code}`)\n"
                f"🆔 Session: `{parsed['session_id']}`\n"
                f"📅 Date: `{parsed['date']}`\n\n"
                "Now, enter a reference name for this show (e.g., `Leo - AGS Vivira`)\n"
                "_(or type /cancel to stop)_:",
                parse_mode=ParseMode.MARKDOWN
            )
            return STATE_SHOW_NAME
        else:
            await update.message.reply_text(
                f"✅ *Extracted Data:*\n"
                f"🏛️ Venue Code: `{v_code}`\n"
                f"🆔 Session: `{parsed['session_id']}`\n"
                f"📅 Date: `{parsed['date']}`\n\n"
                f"Please enter the *Theatre Name* for venue `{v_code}`:\n"
                "_(or type /cancel to stop)_:",
                parse_mode=ParseMode.MARKDOWN
            )
            return STATE_SHOW_THEATRE
        
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\n\nPlease paste a valid seat layout URL:")
        return STATE_SHOW_URL


async def receive_show_theatre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for when venue code is not recognized in VENUE_MAP."""
    context.user_data["show"]["theatre"] = update.message.text.strip()
    await update.message.reply_text(
        "Now, enter a reference name for this show (e.g., `Leo - AGS Vivira`)\n"
        "_(or type /cancel to stop)_:",
        parse_mode=ParseMode.MARKDOWN
    )
    return STATE_SHOW_NAME

async def receive_show_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["show"]["name"] = update.message.text.strip()
    
    # Detailed instruction text for the Smart Time Validator
    instructions = (
        "⏰ *Enter Show Time*\n\n"
        "Please provide the showtime. Our smart validator accepts several formats:\n\n"
        "✅ *Valid Examples:*\n"
        "• `10:30 AM`  (Standard)\n"
        "• `02:15 PM`  (Standard)\n"
        "• `10 AM`     (No minutes)\n"
        "• `22:30`     (24-hour format)\n"
        "• `14`        (24-hour hour-only)\n\n"
        "_(or type /cancel to stop)_:"
    )
    
    await update.message.reply_text(instructions, parse_mode=ParseMode.MARKDOWN)
    return STATE_SHOW_TIME



async def receive_show_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_time = update.message.text.strip()
    
    # --- STRICT TIME VALIDATOR ---
    parsed_time = None
    
    # Clean up common typos (like replacing dots with colons "10.30" -> "10:30")
    clean_time = raw_time.replace('.', ':').upper()
    
    # List of exact formats we will accept
    formats_to_try = [
        "%I:%M %p",  # 10:30 AM
        "%I:%M%p",   # 10:30AM (no space)
        "%H:%M",     # 22:30 (military time)
        "%I %p",     # 10 AM (no minutes)
        "%I%p",      # 10AM (no minutes, no space)
        "%H"         # 10 or 22 (just the hour, assumes 24-hour clock)
    ]
    
    for fmt in formats_to_try:
        try:
            parsed_time = datetime.strptime(clean_time, fmt).time()
            break  # Stop looking if we found a match!
        except ValueError:
            continue
            
    # If the user typed gibberish, reject it and ask again
    # If the user typed gibberish, reject it and ask again
    if not parsed_time:
        await update.message.reply_text(
            "⚠️ *Invalid time format!*\n\n"
            "Please try again using one of these supported formats:\n"
            "• `10:30 AM`  (Standard)\n"
            "• `02:15 PM`  (Standard)\n"
            "• `10 AM`     (No minutes)\n"
            "• `22:30`     (24-hour format)\n"
            "• `14`        (24-hour hour-only)\n\n"
            "_(or type /cancel to stop)_:",
            parse_mode=ParseMode.MARKDOWN
        )
        return STATE_SHOW_TIME
        
    # Standardize the valid time perfectly (e.g., changes "02:15 PM" to "2:15 PM")
    formatted_time = parsed_time.strftime("%I:%M %p").lstrip('0')
    
    # Save the strictly validated time
    context.user_data["show"]["show_time"] = formatted_time
    
    # Move to the next step
    await update.message.reply_text(
        "💺 How many seats do you need? (e.g., `2` or `4`)\n"
        "_(or type /cancel to stop)_:", 
        parse_mode=ParseMode.MARKDOWN
    )
    return STATE_SHOW_SEAT_COUNT

async def receive_show_seat_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text.strip())
        if count <= 0: raise ValueError()
        context.user_data["show"]["seat_count"] = count
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid positive number:")
        return STATE_SHOW_SEAT_COUNT

    kb = [
        [
            InlineKeyboardButton("✅ Yes (Strictly Adjacent)", callback_data="show_adj_yes"),
            InlineKeyboardButton("❌ No (Distributed OK)", callback_data="show_adj_no")
        ]
    ]
    await update.message.reply_text(
        "👥 *Adjacency Requirement*\n\nDo you require strictly adjacent/consecutive seats?\n""_(or type /cancel to stop)_:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    return STATE_SHOW_ADJACENT

async def receive_show_adjacency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["show"]["require_adjacent"] = (query.data == "show_adj_yes")

    await query.edit_message_text(
        "🔤 *Preferred Rows*\n\nEnter preferred rows separated by commas (e.g., `H,I,J`) or type `ALL`\n""_(or type /cancel to stop)_:",
        parse_mode=ParseMode.MARKDOWN
    )
    return STATE_SHOW_ROWS
async def receive_show_rows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().upper()
    show = context.user_data["show"]
    
    if text in ("ALL", "ANY"):
        show["row_preferences"] = {} # Empty means any row/seat is fine
        return await finalize_manual_show(update, context)
        
    rows = [r.strip() for r in text.split(",") if r.strip()]
    show["pending_rows"] = rows
    show["row_preferences"] = {}
    
    first_row = rows[0]
    await update.message.reply_text(
        f"🔢 *Preferred Seats for Row {first_row}*\n\n"
        "Enter seats using commas, ranges, or exclusions.\n"
        "Examples:\n"
        "• `1, 2, 3`\n"
        "• `1-30`\n"
        "• `1-30 except 15, 16`\n"
        "• `1-30 ! 10-20`\n"
        "Or type `ANY` if you don't care about specific seats in this row\n"
        "_(or type /cancel to stop)_:",
        parse_mode=ParseMode.MARKDOWN
    )
    return STATE_SHOW_ROW_SEATS

async def receive_row_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().upper()
    show = context.user_data["show"]
    
    pending_rows = show.get("pending_rows", [])
    current_row = pending_rows.pop(0)
    
    # 🔥 Use our new helper function here
    show["row_preferences"][current_row] = parse_seat_preferences(text)
    
    if pending_rows:
        next_row = pending_rows[0]
        await update.message.reply_text(
              f"🔢 *Preferred Seats for Row {next_row}*\n\n"
                    "Enter seats using commas, ranges, or exclusions.\n"
                    "Examples:\n"
                    "• `1, 2, 3`\n"
                    "• `1-30`\n"
                    "• `1-30 except 15, 16`\n"
                    "• `1-30 ! 10-20`\n"
                    "Or type `ANY` if you don't care about specific seats in this row\n"
                    "_(or type /cancel to stop)_:",
            parse_mode=ParseMode.MARKDOWN
        )
        return STATE_SHOW_ROW_SEATS
    else:
        return await finalize_manual_show(update, context)
    
async def finalize_manual_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    show_entry = context.user_data["show"]

    raw_date = show_entry.get("date", "")
    try:
        formatted_date = datetime.strptime(raw_date, "%Y%m%d").strftime("%d%m%y")
    except ValueError:
        formatted_date = raw_date
    
    theatre_display = show_entry.get("theatre") or show_entry.get("venue_code", "Venue")

    thread_id = None
    if GROUP_CHAT_ID_SHOWS:
        try:
            esc = lambda text: escape_markdown(str(text), version=2)
            topic_name = f"{show_entry.get('name', 'Show')}_{theatre_display}_{formatted_date}|{show_entry.get('show_time', 'Time')}"[:128]
            topic = await context.bot.create_forum_topic(chat_id=GROUP_CHAT_ID_SHOWS, name=topic_name)
            thread_id = topic.message_thread_id
            
            if show_entry["row_preferences"]:
                rows_list = [f"Row {r}: {s if s else 'ANY'}" for r, s in show_entry["row_preferences"].items()]
                prefs_summary_text = "\n" + "\n".join([f"  • {esc(rs)}" for rs in rows_list])
            else:
                prefs_summary_text = " " + esc("ALL ROWS / ANY SEATS")

            adj_text = "Yes (Strictly Adjacent)" if show_entry.get("require_adjacent", True) else "No (Distributed OK)"

            # --- NEW HYPERLINK LOGIC ---
            safe_name = esc(show_entry['name'])
            show_url = show_entry.get('url', '')
            
            # Create the MarkdownV2 hyperlink: [Show Name](https://...)
            if show_url:
                name_hyperlink = f"[{safe_name}]({show_url})"
            else:
                name_hyperlink = safe_name

            summary = (
                "🎉 *Manual Show Configuration Summary*\n\n"
                f"🎬 *Show Name:* {name_hyperlink}\n"
                f"🏛️ *Theatre:* {esc(theatre_display)} \\({esc(show_entry['venue_code'])}\\)\n"
                f"🆔 *Session ID:* {esc(show_entry['session_id'])}\n"
                f"📅 *Date:* {esc(show_entry['date'])}\n"
                f"⏰ *Time:* {esc(show_entry['show_time'])}\n"
                f"💺 *Seats Required:* {esc(show_entry['seat_count'])}\n"
                f"👥 *Strict Adjacent:* {esc(adj_text)}\n"
                f"📍 *Row Preferences:*{prefs_summary_text}\n\n"
                "🔔 _Automated alerts for this manual show will appear in this topic\\._"
            )
            # --- NEW: Send a tiny buffer message first ---
            # --- 1. Send Buffer Message ---
            buffer_msg = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID_SHOWS, 
                message_thread_id=thread_id, 
                text="🚀 _Initializing tracker..._", 
                parse_mode=ParseMode.MARKDOWN
            )
            topic_msg = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID_SHOWS, message_thread_id=thread_id, text=summary, parse_mode=ParseMode.MARKDOWN_V2,link_preview_options=LinkPreviewOptions(is_disabled=True)  # <--- ADD THIS
            )
            asyncio.create_task(background_delayed_pin(context.bot, GROUP_CHAT_ID_SHOWS, topic_msg.message_id,buffer_msg.message_id))
        except Exception as e:
            log.error(f"Failed to create show forum topic: {e}")

    show_entry["message_thread_id"] = thread_id
    
    shows = load_shows()
    shows.append(show_entry)
    save_shows(shows)

    await update.message.reply_text(
        f"✅ *Manual Show Added Successfully!*\n\n"
        f"Name: `{show_entry['name']}`\n"
        f"Theatre: `{theatre_display}` (`{show_entry['venue_code']}`)\n"
        f"Session ID: `{show_entry['session_id']}`\n"
        f"A dedicated topic has been created in the group.",
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data.clear()
    return ConversationHandler.END

    
def parse_bms_url(url: str) -> dict:
    path = urlparse(url).path.strip("/")
    parts = path.split("/")

    event_code = None
    region_slug = None
    movie_slug = ""

    for part in parts:
        if re.match(r"^ET\d{8,}$", part):
            event_code = part

    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            region_slug = parts[idx + 1]
        if idx + 2 < len(parts):
            movie_slug = parts[idx + 2]

    if not event_code or not region_slug:
        raise ValueError("Invalid URL: missing event code (ET...) or city region.")

    movie_name = movie_slug.replace("-", " ").title() if movie_slug else "MovieWatch"
    movie_name=movie_name.replace(" ","")
    return {
        "event_code": event_code,
        "region_slug": region_slug.lower(),
        "movie_slug": movie_slug,
        "movie_name": movie_name,
    }


def get_next_10_dates() -> list[tuple[str, str]]:
    dates = []
    today = datetime.now()
    for i in range(10):
        target = today + timedelta(days=i)
        dates.append((target.strftime("%Y%m%d"), target.strftime("%a, %d %b")))
    return dates


async def safe_edit_reply_markup(query, reply_markup):
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def build_multiselect_keyboard(
    options: list,
    selected: set,
    step_prefix: str,
    columns: int = 2,
    allow_custom_date: bool = False,
    page: int = 0,
    paginated: bool = False,
    require_selection: bool = False,
    exclude_any: bool = False,
) -> InlineKeyboardMarkup:
    keyboard = []
    row = []

    active_options = options
    total_pages = 1

    if paginated:
        total_pages = max(1, (len(options) + THEATRES_PER_PAGE - 1) // THEATRES_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        start_idx = page * THEATRES_PER_PAGE
        end_idx = start_idx + THEATRES_PER_PAGE
        active_options = options[start_idx:end_idx]

    for item in active_options:
        val = item[0] if isinstance(item, tuple) else item
        label = item[1] if isinstance(item, tuple) else item

        orig_idx = options.index(item)

        mark = "✅ " if val in selected else ""
        btn_text = f"{mark}{label}"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"tgl_{orig_idx}"))

        if len(row) == columns:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    if paginated and total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page + 1}"))
        keyboard.append(nav_row)

    ctrl_row = []
    
    if not exclude_any:
        ctrl_row.append(
            InlineKeyboardButton(
                "🌐 Any / All" if selected else "✅ Any / All",
                callback_data="tgl_ANY",
            )
        )

    ctrl_row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"back_{step_prefix}"))

    if require_selection and not selected:
        ctrl_row.append(InlineKeyboardButton("⚠️ Select a date to proceed", callback_data="alert_no_selection"))
    else:
        ctrl_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"next_{step_prefix}"))

    keyboard.append(ctrl_row)

    if allow_custom_date:
        keyboard.append([
            InlineKeyboardButton("➕ Add Custom Date (YYYYMMDD)", callback_data="add_custom_date")
        ])

    keyboard.append([
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_watch")
    ])

    return InlineKeyboardMarkup(keyboard)


def append_to_watches_file(watch_entry: dict):
    watches = load_watches()
    watches.append(watch_entry)  # Append to array
    save_watches(watches)
    log.info("WATCH APPENDED TO GITHUB | total_count=%d", len(watches))


# ======================================================================
# CONVERSATION STEP HANDLERS
# ======================================================================
async def handle_smart_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return ConversationHandler.END
    url = (update.message.text or "").strip()
    log.info(f"Smart router received link: {url}")

    # --- BRANCH 1: SHOW LINK ---
    if "seat-layout" in url:
        try:
            parsed = parse_seat_layout_url(url)
            v_code = parsed["venue_code"].upper()
            theatre_name = VENUE_MAP.get(v_code)
            context.user_data["show"] = {
                "venue_code": v_code,
                "theatre": theatre_name or "",
                "session_id": parsed["session_id"],
                "date": parsed["date"],
                # --- ADD THIS LINE ---
                "url": url  
            }
            
            # If theatre is recognized in VENUE_MAP, proceed to Show Name
            if theatre_name:
                await update.message.reply_text(
                    f"🎯 *Smart Detection: Add Manual Show*\n\n"
                    f"✅ *Extracted Data:*\n"
                    f"🏛️ Theatre: `{theatre_name}` (`{v_code}`)\n"
                    f"🆔 Session: `{parsed['session_id']}`\n"
                    f"📅 Date: `{parsed['date']}`\n\n"
                    "Now, enter a reference name for this show (e.g., `Leo - AGS Vivira`)\n"
                    "_(or type /cancel to stop)_:",
                    parse_mode=ParseMode.MARKDOWN
                )
                return STATE_SHOW_NAME
            else:
                # If code is not recognized, ask for Theatre Name
                await update.message.reply_text(
                    f"🎯 *Smart Detection: Add Manual Show*\n\n"
                    f"✅ *Extracted Data:*\n"
                    f"🏛️ Venue Code: `{v_code}`\n"
                    f"🆔 Session: `{parsed['session_id']}`\n"
                    f"📅 Date: `{parsed['date']}`\n\n"
                    f"Please enter the *Theatre Name* for venue `{v_code}` (e.g., `Rakki Cinemas`)\n"
                    "_(or type /cancel to stop)_:",
                    parse_mode=ParseMode.MARKDOWN
                )
                return STATE_SHOW_THEATRE
            
        except ValueError as e:
            await update.message.reply_text(f"⚠️ {e}\n\nPlease paste a valid seat layout URL:")
            return STATE_SHOW_URL

    # --- BRANCH 2: WATCH LINK ---
    else:
        try:
            parsed = parse_bms_url(url)
        except Exception as e:
            await update.message.reply_text(
                f"❌ *Could not parse URL:* {e}\n\nPlease send a valid BookMyShow event URL:",
                parse_mode=ParseMode.MARKDOWN,
            )
            return STATE_URL

        context.user_data["watch"] = {
            "name": parsed["movie_name"],
            "url": url,
            "event_code": parsed["event_code"],
            "region_slug": parsed["region_slug"],
            "languages": set(),
            "formats": set(),
            "theatre": set(),
            "dates": set(),
            "date_time_map": {},    # <--- Added for advanced dates
            "current_times": set(), # <--- Temp storage for the active date
        }

        options = Languages.get_all()
        context.user_data["current_options"] = options
        context.user_data["current_page"] = 0

        kb = build_multiselect_keyboard(
            options=options,
            selected=context.user_data["watch"]["languages"],
            step_prefix="language",
            columns=2,
        )

        await update.message.reply_text(
            f"🎯 *Smart Detection: Add Watch*\n\n"
            f"🎬 *Movie:* {parsed['movie_name']}\n"
            f"📍 *City:* {parsed['region_slug'].title()}\n\n"
            "📌 *Step 1: Select Languages*\n"
            "_(Tap to toggle, select 'Any' to match all, then click Next)_",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_LANGUAGE
    
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info(f"User {update.effective_user.id} requested the main menu.")
    await show_main_menu(update, context)
    return ConversationHandler.END


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    log.info(f"Received URL input: {url}")

    try:
        parsed = parse_bms_url(url)
    except Exception as e:
        log.warning(f"Failed to parse URL '{url}': {e}")
        await update.message.reply_text(
            f"❌ *Could not parse URL:* {e}\n\nPlease send a valid BookMyShow event URL:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_URL

    context.user_data["watch"] = {
        "name": parsed["movie_name"],
        "url": url,
        "event_code": parsed["event_code"],
        "region_slug": parsed["region_slug"],
        "languages": set(),
        "formats": set(),
        "theatre": set(),
        "dates": set(),
        "date_time_map": {},    # <--- Added for advanced dates
        "current_times": set(), # <--- Temp storage for the active date
    }

    options = Languages.get_all()
    context.user_data["current_options"] = options
    context.user_data["current_page"] = 0

    kb = build_multiselect_keyboard(
        options=options,
        selected=context.user_data["watch"]["languages"],
        step_prefix="language",
        columns=2,
    )

    await update.message.reply_text(
        f"🎬 *Movie:* {parsed['movie_name']}\n"
        f"📍 *City:* {parsed['region_slug'].title()}\n\n"
        "📌 *Step 1: Select Languages*\n"
        "_(Tap to toggle, select 'Any' to match all, then click Next)_",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return STATE_LANGUAGE


async def handle_language_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    log.info(f"STATE_LANGUAGE callback data received: {data}")
    watch = context.user_data["watch"]
    options = context.user_data["current_options"]

    if data == "next_language":
        options = Formats.get_all()
        context.user_data["current_options"] = options
        context.user_data["current_page"] = 0
        kb = build_multiselect_keyboard(
            options=options,
            selected=watch["formats"],
            step_prefix="format",
            columns=2,
        )
        await query.edit_message_text(
            f"🎬 *{watch['name']}*\n\n"
            "📌 *Step 2: Select Screen Formats*\n"
            "_(Tap to toggle, select 'Any' to match all, then click Next)_",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_FORMAT

    if data == "back_language":
        await query.edit_message_text("🚫 Setup restarted. Send a valid BookMyShow movie link:")
        return STATE_URL

    if data.startswith("page_"):
        context.user_data["current_page"] = int(data.split("_")[1])
    elif data == "tgl_ANY":
        watch["languages"].clear()
    elif data.startswith("tgl_"):
        idx = int(data.split("_")[1])
        item = options[idx]
        if item in watch["languages"]:
            watch["languages"].remove(item)
        else:
            watch["languages"].add(item)

    kb = build_multiselect_keyboard(
        options=options,
        selected=watch["languages"],
        step_prefix="language",
        columns=2,
        page=context.user_data.get("current_page", 0),
    )
    await safe_edit_reply_markup(query, reply_markup=kb)
    return STATE_LANGUAGE


async def handle_format_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    log.info(f"STATE_FORMAT callback data received: {data}")
    watch = context.user_data["watch"]
    options = context.user_data["current_options"]

    if data == "next_format":
        city_slug = watch["region_slug"]
        city_theatres = Theatres.get_by_city(city_slug) or Theatres.get_all()
        context.user_data["current_options"] = city_theatres
        context.user_data["current_page"] = 0
        kb = build_multiselect_keyboard(
            options=city_theatres,
            selected=watch["theatre"],
            step_prefix="theatre",
            columns=1,
            paginated=True,
        )
        await query.edit_message_text(
            f"🎬 *{watch['name']}*\n\n"
            f"📌 *Step 3: Select Theatres in {city_slug.title()}*\n"
            "_(Use pagination below to browse venues)_",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_THEATRE

    if data == "back_format":
        options = Languages.get_all()
        context.user_data["current_options"] = options
        context.user_data["current_page"] = 0
        kb = build_multiselect_keyboard(options, watch["languages"], "language", 2)
        await query.edit_message_text(
            f"🎬 *{watch['name']}*\n\n📌 *Step 1: Select Languages*",
            reply_markup=kb, parse_mode=ParseMode.MARKDOWN
        )
        return STATE_LANGUAGE

    if data.startswith("page_"):
        context.user_data["current_page"] = int(data.split("_")[1])
    elif data == "tgl_ANY":
        watch["formats"].clear()
    elif data.startswith("tgl_"):
        idx = int(data.split("_")[1])
        item = options[idx]
        if item in watch["formats"]:
            watch["formats"].remove(item)
        else:
            watch["formats"].add(item)

    kb = build_multiselect_keyboard(
        options=options,
        selected=watch["formats"],
        step_prefix="format",
        columns=2,
        page=context.user_data.get("current_page", 0),
    )
    await safe_edit_reply_markup(query, reply_markup=kb)
    return STATE_FORMAT


async def handle_theatre_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    log.info(f"STATE_THEATRE callback data received: {data}")
    watch = context.user_data["watch"]
    options = context.user_data["current_options"]

    if data == "next_theatre":
        date_options = get_next_10_dates()
        context.user_data["current_options"] = date_options
        context.user_data["current_page"] = 0
        kb = build_multiselect_keyboard(
            options=date_options,
            selected=watch["dates"],
            step_prefix="date",
            columns=2,
            allow_custom_date=True,
            require_selection=True,
            exclude_any=True,
        )
        await query.edit_message_text(
            f"🎬 *{watch['name']}*\n\n"
            "📌 *Step 4: Select Dates (Required)*\n"
            "_(Choose at least one date or add a custom date)_",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_DATE

    if data == "back_theatre":
        options = Formats.get_all()
        context.user_data["current_options"] = options
        context.user_data["current_page"] = 0
        kb = build_multiselect_keyboard(options, watch["formats"], "format", 2)
        await query.edit_message_text(
            f"🎬 *{watch['name']}*\n\n📌 *Step 2: Select Formats*",
            reply_markup=kb, parse_mode=ParseMode.MARKDOWN
        )
        return STATE_FORMAT

    if data.startswith("page_"):
        context.user_data["current_page"] = int(data.split("_")[1])
    elif data == "tgl_ANY":
        watch["theatre"].clear()
    elif data.startswith("tgl_"):
        idx = int(data.split("_")[1])
        item = options[idx]
        if item in watch["theatre"]:
            watch["theatre"].remove(item)
        else:
            watch["theatre"].add(item)

    kb = build_multiselect_keyboard(
        options=options,
        selected=watch["theatre"],
        step_prefix="theatre",
        columns=1,
        page=context.user_data.get("current_page", 0),
        paginated=True,
    )
    await safe_edit_reply_markup(query, reply_markup=kb)
    return STATE_THEATRE


async def handle_date_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    log.info(f"STATE_DATE callback data received: {data}")
    watch = context.user_data["watch"]
    options = context.user_data["current_options"]

    if data == "alert_no_selection":
        await query.answer("⚠️ Please select at least one date before proceeding!", show_alert=True)
        return STATE_DATE

    await query.answer()

    if data == "add_custom_date":
        await query.edit_message_text(
            "📅 *Enter Custom Date*\n\n"
            "Please send the date in `YYYYMMDD` format (e.g., `20260915`):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_CUSTOM_DATE

    if data == "next_date":
        if not watch["dates"]:
            await query.answer("⚠️ Please select at least one date!", show_alert=True)
            return STATE_DATE

        # 1. Prepare the Date Loop
        context.user_data["sorted_dates"] = sorted(list(watch["dates"]))
        context.user_data["current_date_idx"] = 0
        watch["date_time_map"] = {}
        watch["current_times"] = set()

        time_options = TimePeriods.get_all()
        context.user_data["current_options"] = time_options
        context.user_data["current_page"] = 0

        # 2. Get the first date to display
        first_date = context.user_data["sorted_dates"][0]
        try:
            formatted_date = datetime.strptime(first_date, "%Y%m%d").strftime("%d %b %Y")
        except:
            formatted_date = first_date

        kb = build_multiselect_keyboard(
            options=time_options,
            selected=watch["current_times"],
            step_prefix="time",
            columns=1,
        )
        await query.edit_message_text(
            f"🎬 *{watch['name']}*\n\n"
            f"📌 *Step 5: Select Show Times for {formatted_date}*\n"
            "_(Select 'Any / All' to track all day)_",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_TIME
    
    if data == "back_date":
        city_slug = watch["region_slug"]
        city_theatres = Theatres.get_by_city(city_slug) or Theatres.get_all()
        context.user_data["current_options"] = city_theatres
        context.user_data["current_page"] = 0
        kb = build_multiselect_keyboard(city_theatres, watch["theatre"], "theatre", 1, paginated=True)
        await query.edit_message_text(
            f"🎬 *{watch['name']}*\n\n📌 *Step 3: Select Theatres*",
            reply_markup=kb, parse_mode=ParseMode.MARKDOWN
        )
        return STATE_THEATRE

    if data.startswith("page_"):
        context.user_data["current_page"] = int(data.split("_")[1])
    elif data.startswith("tgl_"):
        idx = int(data.split("_")[1])
        date_code = options[idx][0]
        if date_code in watch["dates"]:
            watch["dates"].remove(date_code)
        else:
            watch["dates"].add(date_code)

    kb = build_multiselect_keyboard(
        options=options,
        selected=watch["dates"],
        step_prefix="date",
        columns=2,
        allow_custom_date=True,
        page=context.user_data.get("current_page", 0),
        require_selection=True,
        exclude_any=True,
    )
    await safe_edit_reply_markup(query, reply_markup=kb)
    return STATE_DATE


async def receive_custom_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    log.info(f"Custom date received: {text}")
    watch = context.user_data["watch"]

    if not re.match(r"^\d{8}$", text):
        await update.message.reply_text(
            "⚠️ Invalid format! Please enter an 8-digit date code like `20260920`:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_CUSTOM_DATE

    watch["dates"].add(text)

    date_options = get_next_10_dates()
    context.user_data["current_options"] = date_options

    kb = build_multiselect_keyboard(
        options=date_options,
        selected=watch["dates"],
        step_prefix="date",
        columns=2,
        allow_custom_date=True,
        require_selection=True,
        exclude_any=True,
    )

    await update.message.reply_text(
        f"✅ Added date `{text}`.\nSelect more or tap Next:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return STATE_DATE

async def finalize_watch_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Helper function to save the watch once all dates have times selected."""
    query = update.callback_query
    watch = context.user_data["watch"]
    
    IST = timezone(timedelta(hours=5, minutes=30))
    current_time_str = datetime.now(IST).strftime("%Y%m%d%H%M%S")
    watch_name = watch["name"] + "_" + current_time_str

    esc = lambda text: escape_markdown(str(text), version=2)

    raw_langs = esc(", ".join(sorted(list(watch["languages"]))) if watch["languages"] else "ALL")
    raw_formats = esc(", ".join(sorted(list(watch["formats"]))) if watch["formats"] else "ALL")

    # Build advanced Date/Time summary
    dt_lines = []
    for d_code, t_list in watch["date_time_map"].items():
        try:
            pretty_date = datetime.strptime(d_code, "%Y%m%d").strftime("%d %b")
        except:
            pretty_date = d_code
        t_str = ", ".join(t_list).title() if t_list else "All Times"
        dt_lines.append(f"  • {esc(pretty_date)}: {esc(t_str)}")
    dt_summary = "\n" + "\n".join(dt_lines)

    if watch["theatre"]:
        theatre_list_str = "\n" + "\n".join([f"  • {esc(t)}" for t in sorted(list(watch["theatre"]))])
    else:
        theatre_list_str = " " + esc("ALL")

    # --- NEW HYPERLINK LOGIC FOR WATCHES ---
    safe_watch_name = esc(watch["name"])
    watch_url = watch.get("url", "")
    
    # Create the MarkdownV2 hyperlink: [Movie Name_123456789](https://...)
    if watch_url:
        name_hyperlink = f"[{safe_watch_name}]({watch_url})"
    else:
        name_hyperlink = safe_watch_name

    summary = (
        "🎉 *Watch Configuration Summary*\n\n"
        f"🎬 *Movie:* {name_hyperlink}\n"
        f"🌐 *Languages:* {raw_langs}\n"
        f"📦 *Formats:* {raw_formats}\n"
        f"📅 *Dates & Times:*{dt_summary}\n"
        f"🏛️ *Theatres:*{theatre_list_str}\n\n"
        "🔔 _Automated alerts for this watch will appear in this topic\\._"
    )

    thread_id = None
    if GROUP_CHAT_ID_WATCHES:
        try:
            chat_id = int(GROUP_CHAT_ID_WATCHES)
            topic = await context.bot.create_forum_topic(chat_id=GROUP_CHAT_ID_WATCHES, name=watch_name[:128])
            thread_id = topic.message_thread_id

            # --- NEW: Send a tiny buffer message first ---
            # --- 1. Send Buffer Message ---
            buffer_msg = await context.bot.send_message(
                chat_id=chat_id, 
                message_thread_id=thread_id, 
                text="🚀 _Initializing tracker..._", 
                parse_mode=ParseMode.MARKDOWN
            )
            
            topic_msg = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID_WATCHES, message_thread_id=thread_id,
                text=summary, parse_mode=ParseMode.MARKDOWN_V2,
                link_preview_options=LinkPreviewOptions(is_disabled=True)  # <--- ADD THIS
            )
            asyncio.create_task(background_delayed_pin(context.bot, chat_id, topic_msg.message_id,buffer_msg.message_id))
        except Exception as e:
            log.error(f"Failed to create topic or pin message: {e}")

    # Pass the dictionary directly to the 'dates' field for main.py to read
    new_watch_entry = {
        "name": watch_name,
        "url": watch["url"],
        "dates": watch["date_time_map"], 
        "theatre": sorted(list(watch["theatre"])),
        "time_period": [], 
        "discover_variants": True,
        "languages": sorted(list(watch["languages"])),
        "formats": sorted(list(watch["formats"])),
        "message_thread_id": thread_id, 
    }

    append_to_watches_file(new_watch_entry)

    dm_confirmation = summary + f"\n\n✅ *Setup Complete\\!* A dedicated topic has been created in the group\\."
    await query.edit_message_text(dm_confirmation, parse_mode=ParseMode.MARKDOWN_V2,link_preview_options=LinkPreviewOptions(is_disabled=True))  # <--- ADD THIS)
    
    context.user_data.clear()
    return ConversationHandler.END


async def handle_time_toggle_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    watch = context.user_data["watch"]
    options = context.user_data["current_options"]
    
    sorted_dates = context.user_data.get("sorted_dates", [])
    current_idx = context.user_data.get("current_date_idx", 0)

    if not sorted_dates or current_idx >= len(sorted_dates):
        return ConversationHandler.END

    current_date_val = sorted_dates[current_idx]

    # --- 1. HANDLE BACK BUTTON ---
    if data == "back_time":
        if current_idx > 0:
            # Go back to the PREVIOUS date's time selection
            context.user_data["current_date_idx"] -= 1
            prev_date = sorted_dates[context.user_data["current_date_idx"]]
            watch["current_times"] = set(watch["date_time_map"].get(prev_date, []))
            
            try:
                formatted_date = datetime.strptime(prev_date, "%Y%m%d").strftime("%d %b %Y")
            except:
                formatted_date = prev_date

            kb = build_multiselect_keyboard(options=options, selected=watch["current_times"], step_prefix="time", columns=1)
            await query.edit_message_text(
                f"🎬 *{watch['name']}*\n\n📌 *Step 5: Select Show Times for {formatted_date}*",
                reply_markup=kb, parse_mode=ParseMode.MARKDOWN
            )
            return STATE_TIME
        else:
            # Go all the way back to the main Dates selection
            date_options = get_next_10_dates()
            context.user_data["current_options"] = date_options
            kb = build_multiselect_keyboard(date_options, watch["dates"], "date", 2, allow_custom_date=True, require_selection=True, exclude_any=True)
            await query.edit_message_text(
                f"🎬 *{watch['name']}*\n\n📌 *Step 4: Select Dates*",
                reply_markup=kb, parse_mode=ParseMode.MARKDOWN
            )
            return STATE_DATE

    # --- 2. HANDLE TOGGLES ---
    if data == "tgl_ANY":
        watch["current_times"].clear()
    elif data.startswith("tgl_"):
        idx = int(data.split("_")[1])
        raw_option = options[idx]
        time_key = raw_option[0] if isinstance(raw_option, tuple) else raw_option
        
        if time_key in watch["current_times"]:
            watch["current_times"].remove(time_key)
        else:
            watch["current_times"].add(time_key)

    # --- 3. HANDLE NEXT / FINISH ---
    if data == "next_time":
        # Save times for this specific date
        watch["date_time_map"][current_date_val] = sorted(list(watch["current_times"]))
        
        # Advance the loop
        context.user_data["current_date_idx"] += 1
        new_idx = context.user_data["current_date_idx"]
        
        if new_idx < len(sorted_dates):
            # Show screen for the NEXT date
            next_date = sorted_dates[new_idx]
            watch["current_times"].clear() 
            
            try:
                formatted_date = datetime.strptime(next_date, "%Y%m%d").strftime("%d %b %Y")
            except:
                formatted_date = next_date

            kb = build_multiselect_keyboard(options=options, selected=watch["current_times"], step_prefix="time", columns=1)
            await query.edit_message_text(
                f"🎬 *{watch['name']}*\n\n"
                f"📌 *Step 5: Select Show Times for {formatted_date}*\n"
                "_(Select 'Any / All' to track all day)_",
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN,
            )
            return STATE_TIME
        else:
            # All dates processed! Finalize the setup.
            return await finalize_watch_setup(update, context)

    # Redraw keyboard if just a toggle
    kb = build_multiselect_keyboard(options=options, selected=watch["current_times"], step_prefix="time", columns=1)
    await safe_edit_reply_markup(query, reply_markup=kb)
    return STATE_TIME

async def cancel_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("Setup workflow cancelled.")
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("🚫 Setup canceled.")
    elif update.message:
        await update.message.reply_text("🚫 Setup canceled.")
    return ConversationHandler.END


# ======================================================================
# NEW STANDALONE COMMAND HANDLERS (/watches, /stop, /inspect)
# ======================================================================

async def list_watches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info(f"User {update.effective_user.id} requested paginated watch list.")
    watches = load_watches()

    text, reply_markup = build_watches_view(watches, page=0)
    await update.message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def list_shows_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, reply_markup = build_shows_view(load_shows(), page=0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def handle_watch_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- ADD THIS SECURITY CHECK FIRST ---
    if not await is_authorized(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    watches = load_watches()

    # --- 1. HANDLE PAGE FLIP ---
    if data.startswith("wpage_"):
        page_num = int(data.split("_")[1])
        text, reply_markup = build_watches_view(watches, page=page_num)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    # --- 2. INSPECT WATCH ---
    elif data.startswith("insp_"):
        idx = int(data.split("_")[1])
        if idx >= len(watches):
            await query.edit_message_text("⚠️ Watch not found. The list might have changed.")
            return

        matched = watches[idx]
        langs_str = ", ".join(matched.get("languages", [])) or "ALL"
        formats_str = ", ".join(matched.get("formats", [])) or "ALL"
        dates_str = ", ".join(matched.get("dates", []))
        times_str = ", ".join(matched.get("time_period", [])) or "ALL"
        theatres_str = f"{len(matched.get('theatre', []))} selected" if matched.get("theatre") else "ALL"

        details = (
            f"🔍 *Watch Details: {escape_markdown(matched.get('name', ''), version=2)}*\n\n"
            f"🔗 *URL:* {escape_markdown(matched.get('url', ''), version=2)}\n"
            f"🌐 *Languages:* {escape_markdown(langs_str, version=2)}\n"
            f"📦 *Formats:* {escape_markdown(formats_str, version=2)}\n"
            f"🏛️ *Theatres:* {escape_markdown(theatres_str, version=2)}\n"
            f"📅 *Dates:* {escape_markdown(dates_str, version=2)}\n"
            f"⏰ *Times:* {escape_markdown(times_str, version=2)}"
        )

        kb = [[InlineKeyboardButton("⬅️ Back to List", callback_data="back_to_list")]]
        await query.edit_message_text(details, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)

    # --- 3. INTENT TO STOP ---
    elif data.startswith("stop_"):
        idx = int(data.split("_")[1])
        if idx >= len(watches):
            return

        exact_watch_name = watches[idx].get("name")
        kb = [
            [
                InlineKeyboardButton("✅ Yes, Stop It", callback_data=f"confirmstopperm_{idx}"),
                InlineKeyboardButton("❌ Cancel", callback_data="back_to_list")
            ]
        ]
        await query.edit_message_text(
            f"⚠️ *Are you sure you want to stop tracking:*\n`{escape_markdown(exact_watch_name, version=2)}`?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN_V2
        )

    # --- 4. CONFIRM STOP ---
    # --- 4. CONFIRM STOP ---
    elif data.startswith("confirmstop_"):
        idx = int(data.split("_")[1])
        if idx >= len(watches):
            return

        matched = watches[idx]
        exact_watch_name = matched.get("name")

        # Delete Topic
        thread_id = matched.get("message_thread_id")
        if thread_id and GROUP_CHAT_ID_WATCHES:
            try:
                await context.bot.close_forum_topic(chat_id=GROUP_CHAT_ID_WATCHES, message_thread_id=thread_id)
                # await context.bot.delete_forum_topic(chat_id=GROUP_CHAT_ID_WATCHES, message_thread_id=thread_id)
            except Exception as e:
                log.error(f"Failed to delete forum topic: {e}")

        # Update GitHub Watches & Cache
        # updated_watches = [w for i, w in enumerate(watches) if i != idx]
        # save_watches(updated_watches)

        # --- 2. Update watches.json instead of removing (with Asia/Kolkata Timestamps) ---
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        kolkata_now = datetime.now(ZoneInfo("Asia/Kolkata"))
        formatted_closed_time = kolkata_now.strftime("%d/%m/%y %I:%M %p")

        deletion_target = kolkata_now + timedelta(days=14)
        formatted_deletion_time = deletion_target.strftime("%d/%m/%y %I:%M %p")

        matched["status"] = "closed"
        matched["closed_at"] = int(kolkata_now.timestamp())
        matched["closed_at_formatted"] = formatted_closed_time
        matched["deletes_at_formatted"] = formatted_deletion_time
        
        save_watches(watches)

        # --- ROBUST STATE CLEANUP (Resistant to Renaming) ---
        deleted_count = 0
        try:
            # Extract the unique timestamp ID (e.g., "1710923012") from the watch name
            timestamp_match = re.search(r'_(\d{10,})', exact_watch_name)
            unique_id = timestamp_match.group(1) if timestamp_match else exact_watch_name.split('_')[0]

            bms_state, sha = _github_get_file(GITHUB_WSTATE_PATH, True)
            if bms_state and isinstance(bms_state, dict):
                # Delete any state key containing this unique ID or base name
                keys_to_delete = [k for k in bms_state.keys() if unique_id in str(k)]
                if keys_to_delete:
                    for k in keys_to_delete:
                        del bms_state[k]
                    _github_put_file(GITHUB_WSTATE_PATH, bms_state, f"Cleared states for watch ID {unique_id}", True)
                    deleted_count = len(keys_to_delete)
        except Exception as e:
            log.error(f"Failed to clear bms_state.json: {e}")

        if query.message.chat.type in ["group", "supergroup"]:
            # Grab the original message text (e.g., "Tracker Expired!")
            original_text = query.message.text or "⏰ Tracker Expired!"
            
            # Append the closed status to the original text
            updated_text = (
                f"{original_text}\n\n"
                f"🔒 As per ur req *Tracker Closed*\n"
                f"• Closed at: `{formatted_closed_time}`\n"
                f"• Auto-deletes on: `{formatted_deletion_time}`"
            )
            
            # Create a "dead" inline button just for the UI design
            disabled_kb = [[InlineKeyboardButton("🔒 Tracker Closed", callback_data="noop")]]
            
            await query.edit_message_text(
                text=updated_text,
                reply_markup=InlineKeyboardMarkup(disabled_kb),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            # If clicked in the bot's private DM, show success and menu buttons
            kb = [
                [InlineKeyboardButton("⬅️ Back to List", callback_data="back_to_list")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")]
            ]
            
            await query.edit_message_text(
                f"✅ *Successfully stopped watch\\!*\n"
                f"🔒 Closed Topic for: `{escape_markdown(exact_watch_name, version=2)}`\n"
                f"🧹 Cleared `{deleted_count}` state variant\\(s\\)\\.\n\n"
                f"⏱️ *Retention Schedule:*\n"
                f"• Closed at: `{escape_markdown(formatted_closed_time, version=2)}`\n"
                f"• Auto\\-deletes on: `{escape_markdown(formatted_deletion_time, version=2)}`",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN_V2
            )
    elif data.startswith("confirmstopperm_"):
       idx = int(data.split("_")[1])
       if idx >= len(watches):
          return

       matched = watches[idx]
       exact_watch_name = matched.get("name")

    # 1. Permanently Delete Forum Topic
       thread_id = matched.get("message_thread_id")
       if thread_id and GROUP_CHAT_ID_WATCHES:
          try:
            await context.bot.close_forum_topic(chat_id=GROUP_CHAT_ID_WATCHES, message_thread_id=thread_id)
            await context.bot.delete_forum_topic(chat_id=GROUP_CHAT_ID_WATCHES, message_thread_id=thread_id)
          except Exception as e:
            log.error(f"Failed to delete forum topic: {e}")

    # 2. Permanently Remove from watches.json (Hard Delete)
       watches = [w for i, w in enumerate(watches) if i != idx]
       save_watches(watches)

    # 3. Robust State Cleanup in bms_state.json
       deleted_count = 0
       try:
          timestamp_match = re.search(r'_(\d{10,})', exact_watch_name)
          unique_id = timestamp_match.group(1) if timestamp_match else exact_watch_name.split('_')[0]

          bms_state, sha = _github_get_file(GITHUB_WSTATE_PATH, True)
          if bms_state and isinstance(bms_state, dict):
            keys_to_delete = [k for k in bms_state.keys() if unique_id in str(k)]
            if keys_to_delete:
                for k in keys_to_delete:
                    del bms_state[k]
                _github_put_file(GITHUB_WSTATE_PATH, bms_state, f"Permanently cleared states for watch ID {unique_id}", True)
                deleted_count = len(keys_to_delete)
       except Exception as e:
          log.error(f"Failed to clear bms_state.json: {e}")

    # 4. Smart UI Response
       kolkata_now = datetime.now(ZoneInfo("Asia/Kolkata"))
       formatted_time = kolkata_now.strftime("%d/%m/%y %I:%M %p")

       if query.message.chat.type in ["group", "supergroup"]:
        original_text = query.message.text or "⏰ Tracker Expired!"
        updated_text = (
            f"{original_text}\n\n"
            f"🗑️ *Permanently Deleted*\n"
            f"• Deleted at: `{formatted_time}`"
        )
        disabled_kb = [[InlineKeyboardButton("🗑️ Permanently Deleted", callback_data="noop")]]
        await query.edit_message_text(
            text=updated_text,
            reply_markup=InlineKeyboardMarkup(disabled_kb),
            parse_mode=ParseMode.MARKDOWN
        )
       else:
        kb = [
            [InlineKeyboardButton("⬅️ Back to List", callback_data="back_to_list")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")]
        ]
        await query.edit_message_text(
            f"✅ *Watch permanently deleted\\!*\n"
            f"🗑️ Topic deleted and `{deleted_count}` state variant\\(s\\) wiped\\.\n\n"
            f"• Deleted at: `{escape_markdown(formatted_time, version=2)}`",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    # --- 5. NAVIGATE BACK TO LIST ---
    elif data == "back_to_list":
        fresh_watches = load_watches()
        text, reply_markup = build_watches_view(fresh_watches, page=0)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def handle_show_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    shows = load_shows()

    if data.startswith("spage_"):
        page_num = int(data.split("_")[1])
        text, reply_markup = build_shows_view(shows, page=page_num)
        kb_list = list(reply_markup.inline_keyboard) if reply_markup and reply_markup.inline_keyboard else []
        kb_list.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_list), parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("delshow_"):
        idx = int(data.split("_")[1])
        if idx < len(shows):
            matched_show = shows[idx]
            thread_id = matched_show.get("message_thread_id")
            v_code = str(matched_show.get("venue_code", "")).strip().upper()
            s_id = str(matched_show.get("session_id", "")).strip()
            
            # --- 1. Properly Close Topic ---
            if thread_id and GROUP_CHAT_ID_SHOWS:
                try:
                    await context.bot.close_forum_topic(chat_id=GROUP_CHAT_ID_SHOWS, message_thread_id=thread_id)
                except Exception as e:
                    log.error(f"Failed to close show forum topic: {e}")
            
            # --- 2. Remove from shows.json ---
            # updated_shows = [s for i, s in enumerate(shows) if i != idx]
            # save_shows(updated_shows)
            kolkata_now = datetime.now(ZoneInfo("Asia/Kolkata"))

# 2. Format as DD/MM/YY HH:MM AM/PM
            formatted_closed_time = kolkata_now.strftime("%d/%m/%y %I:%M %p")

# 3. Calculate 14 days later for auto-deletion target
            deletion_target = kolkata_now + timedelta(days=14)
            formatted_deletion_time = deletion_target.strftime("%d/%m/%y %I:%M %p")
            matched_show["status"] = "closed"
            matched_show["closed_at"] = int(kolkata_now.timestamp())
            matched_show["closed_at_formatted"] = formatted_closed_time
            matched_show["deletes_at_formatted"] = formatted_deletion_time
            save_shows(shows)
            
            # --- 3. EXACT Clean up in state.json ---
            deleted_count = 0
            try:
                s_state, sha = _github_get_file(GITHUB_SSTATE_PATH, False) 
                if s_state and isinstance(s_state, dict):
                    
                    # 1. Safely handle the thread_id exactly how main.py handles it
                    raw_thread = matched_show.get("message_thread_id", "")
                    thread_id_str = str(raw_thread).strip() if raw_thread is not None else ""
                    
                    # 2. Build the EXACT key
                    exact_state_key = f"{v_code}_{s_id}_{thread_id_str}"
                    
                    # 3. Strictly delete ONLY this exact key
                    if exact_state_key in s_state:
                        del s_state[exact_state_key]
                        _github_put_file(GITHUB_SSTATE_PATH, s_state, f"Cleared exact state for {exact_state_key}", False)
                        deleted_count = 1
                    else:
                        log.warning(f"Exact state key '{exact_state_key}' not found in state.json.")
            except Exception as e:
                log.error(f"Failed to clear shows state.json: {e}")

            # --- 4. SMART UI RESPONSE (Group vs Private DM) ---
            if query.message.chat.type in ["group", "supergroup"]:
                # Grab the original message text
                original_text = query.message.text or "⏰ Showtime Reached!"
                
                # Append the closed status to the original text
                updated_text = (
                    f"{original_text}\n\n"
                    f"🔒As per ur req *Tracker Closed*\n"
                    f"• Closed at: `{formatted_closed_time}`\n"
                    f"• Auto-deletes on: `{formatted_deletion_time}`"
                )
                
                # Create a "dead" inline button just for the UI design
                disabled_kb = [[InlineKeyboardButton("🔒 Tracker Closed", callback_data="noop")]]
                
                await query.edit_message_text(
                    text=updated_text,
                    reply_markup=InlineKeyboardMarkup(disabled_kb),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                # If clicked in the bot's private DM, render the full list and main menu button
                text, reply_markup = build_shows_view(load_shows(), page=0)
                kb_list = list(reply_markup.inline_keyboard) if reply_markup and reply_markup.inline_keyboard else []
                kb_list.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")])
                
                await query.edit_message_text(
                    f"✅ *Manual show successfully updated!*\n"
                    f"🔒 Topic closed and state record cleared (`{deleted_count}`).\n\n"
                    f"⏱️ *Retention Schedule:*\n"
                    f"• Closed at: `{formatted_closed_time}`\n"
                    f"• Auto-deletes on: `{formatted_deletion_time}`", 
                    reply_markup=InlineKeyboardMarkup(kb_list), 
                    parse_mode=ParseMode.MARKDOWN
                )
# ======================================================================
# BOT RUNNER
# ======================================================================
auth_filter = filters.User(user_id=list(ALLOWED_USERS)) if ALLOWED_USERS else filters.ALL
def main():
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE" or not BOT_TOKEN:
        print("❌ Error: Set the TELEGRAM_BOT_TOKEN environment variable first.")
        return

    # --- ADD THESE TWO LINES ---
    print("📥 Pre-loading watches from GitHub into memory...")
    load_watches()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command, filters=auth_filter),
            CommandHandler("newwatch", start_command, filters=auth_filter),
            CallbackQueryHandler(handle_main_menu, pattern="^menu_(new_watch|new_show)$"),
MessageHandler(filters.Regex(r"bookmyshow\.com") & auth_filter, handle_smart_link),  ],
        states={
            STATE_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)
            ],
            STATE_LANGUAGE: [
                CallbackQueryHandler(cancel_watch, pattern="^cancel_watch$"),
                CallbackQueryHandler(handle_language_toggle),
            ],
            STATE_FORMAT: [
                CallbackQueryHandler(cancel_watch, pattern="^cancel_watch$"),
                CallbackQueryHandler(handle_format_toggle),
            ],
            STATE_THEATRE: [
                CallbackQueryHandler(cancel_watch, pattern="^cancel_watch$"),
                CallbackQueryHandler(handle_theatre_toggle),
            ],
            STATE_DATE: [
                CallbackQueryHandler(cancel_watch, pattern="^cancel_watch$"),
                CallbackQueryHandler(handle_date_toggle),
            ],
            STATE_CUSTOM_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_date)
            ],
            STATE_TIME: [
                CallbackQueryHandler(cancel_watch, pattern="^cancel_watch$"),
                CallbackQueryHandler(handle_time_toggle_and_save),
            ],

STATE_SHOW_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_show_url)],
            STATE_SHOW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_show_name)],
            STATE_SHOW_THEATRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_show_theatre)], # <--- Added
            STATE_SHOW_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_show_time)],
            STATE_SHOW_SEAT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_show_seat_count)],
            STATE_SHOW_ADJACENT: [CallbackQueryHandler(receive_show_adjacency, pattern="^show_adj_")],
            STATE_SHOW_ROWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_show_rows)],
            STATE_SHOW_ROW_SEATS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_row_seats)],
        },
        fallbacks=[CommandHandler("cancel", cancel_watch,filters=auth_filter),
                   CallbackQueryHandler(handle_main_menu, pattern="^menu_main$") # Allow going back to menu
                   ],
        allow_reentry=True,  # <--- CRITICAL FIX: Allows /start or URL entry while in an active state
    )

    app.add_handler(conv_handler)
    app.add_handler(
        CallbackQueryHandler(
            handle_main_menu, 
            pattern="^menu_(list_watches|list_shows|new_show|help|main)$"
        )
    )
    # Register the requested standalone handlers
    app.add_handler(CommandHandler("watches", list_watches_command,filters=auth_filter))
    app.add_handler(
    CallbackQueryHandler(
        handle_watch_actions, 
        pattern="^(wpage_|insp_|stop_|confirmstopperm_ |confirmstop_|back_to_list)"
    )
)

    app.add_handler(CallbackQueryHandler(handle_show_actions, pattern="^(spage_|delshow_)"))


    # Combine your custom comprehensive service filter with built-in status updates 

# 1. Build the comprehensive filter combining standard status updates and custom forum/live events
    comprehensive_service_filter = filters.StatusUpdate.ALL | filters.MessageFilter(lambda m: bool(
    # Standard Service Updates
    m.new_chat_members or
    m.left_chat_member or
    m.pinned_message or
    m.new_chat_title or
    m.new_chat_photo or
    m.delete_chat_photo or
    # Forum Topic Events
    m.forum_topic_created or 
    m.forum_topic_closed or 
    m.forum_topic_reopened or 
    m.forum_topic_edited or
    m.general_forum_topic_hidden or
    m.general_forum_topic_unhidden or
    # Video Chat / Live Events
    m.video_chat_scheduled or
    m.video_chat_started or
    m.video_chat_ended or
    m.video_chat_participants_invited or
    # Giveaways
    m.giveaway_created or
    m.giveaway_completed or
    m.giveaway_winners
))

# 2. Register the handler to your application
    app.add_handler(MessageHandler(comprehensive_service_filter, auto_delete_all_service_messages))

    print("🤖 Telegram Watch Builder Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    start_health_server()
    main()
