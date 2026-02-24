import os
import re
import pytz
import html
import asyncio
import logging
import pymongo
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta
from PIL import Image
from transformers import pipeline
import requests # Sabse upar imports mein 'import requests' add karein

from telegram import (
    Update, 
    ChatPermissions, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    ReactionTypeEmoji
)
from telegram.error import Forbidden, BadRequest
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
    ContextTypes
)

# ========== RENDER KEEP-ALIVE (FLASK) ==========
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ========== CONFIGURATION (SAFE VERSION) ==========
# Ab ye values Render ke Environment Variables se aayengi
TOKEN = os.environ.get("TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
ADMIN_IDS = [8507307665]
IST = pytz.timezone('Asia/Kolkata')
# NSFW Classifier
# Purana nsfw_classifier = pipeline(...) hata kar ye likhein:
HF_TOKEN = os.environ.get("HF_TOKEN")
NSFW_API_URL = "https://api-inference.huggingface.co/models/Falconsai/nsfw_image_detection"

# ========== DATABASE CLASS ==========
class PersistentDB:
    def __init__(self):
        self.client = pymongo.MongoClient(MONGO_URL)
        self.db = self.client["shield_bot_db"]
        self.group_settings = self.db["group_settings"]
        self.allowlist = self.db["allowlist"]
        self.warnings = self.db["warnings"]
        self.users = self.db["users"]
        self.groups = self.db["groups"]
        self.global_stats = self.db["global_stats"]
        self.sudos = self.db["sudos"]
        self._init_stats()

    def _init_stats(self):
        stats = self.global_stats.find_one({"_id": 1})
        if not stats:
            self.global_stats.insert_one({
                "_id": 1, "scanned": 0, "bio_caught": 0, 
                "media_deleted": 0, "warnings_issued": 0,
                "nsfw_blocked": 0, "bot_start_time": datetime.now(IST).timestamp()
            })

    def update_stat(self, column):
        self.global_stats.update_one({"_id": 1}, {"$inc": {column: 1}})

    def get_global_stats(self):
        stats = self.global_stats.find_one({"_id": 1})
        return (stats.get("scanned", 0), stats.get("bio_caught", 0), stats.get("media_deleted", 0),
                stats.get("warnings_issued", 0), stats.get("nsfw_blocked", 0), 
                stats.get("bot_start_time", datetime.now(IST).timestamp()))

    def get_settings(self, chat_id):
        s = self.group_settings.find_one({"_id": chat_id})
        return (s.get("delay_minutes", 1), s.get("warn_limit", 3), s.get("mute_hours", 1), 
                s.get("copyright_enabled", 0), s.get("anti_channel", 1), s.get("nsfw_enabled", 1)) if s else (1, 3, 1, 0, 1, 1)

    def set_delay(self, chat_id, minutes):
        self.group_settings.update_one({"_id": chat_id}, {"$set": {"delay_minutes": minutes}}, upsert=True)

    def set_limits(self, chat_id, warn_limit, mute_hours):
        self.group_settings.update_one({"_id": chat_id}, {"$set": {"warn_limit": warn_limit, "mute_hours": mute_hours}}, upsert=True)

    def set_anti_channel(self, chat_id, enabled):
        self.group_settings.update_one({"_id": chat_id}, {"$set": {"anti_channel": 1 if enabled else 0}}, upsert=True)

    def set_nsfw(self, chat_id, enabled):
        self.group_settings.update_one({"_id": chat_id}, {"$set": {"nsfw_enabled": 1 if enabled else 0}}, upsert=True)

    def add_user(self, user_id):
        self.users.update_one({"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True)

    def add_group(self, chat_id, title="Unknown Group"):
        self.groups.update_one({"_id": chat_id}, {"$set": {"title": title}}, upsert=True)

    def get_groups(self):
        return [(g["_id"], g.get("title", "Unknown Group")) for g in self.groups.find()]

    def remove_group(self, chat_id):
        self.groups.delete_one({"_id": chat_id})
        self.group_settings.delete_one({"_id": chat_id})

    def get_all_targets(self):
        users = [u["_id"] for u in self.users.find()]
        groups = [g["_id"] for g in self.groups.find()]
        return list(set(users + groups))

    def is_allowed(self, user_id):
        return self.allowlist.find_one({"_id": user_id}) is not None

    def add_to_allowlist(self, user_id):
        if not self.is_allowed(user_id):
            self.allowlist.insert_one({"_id": user_id})
            return True
        return False

    def remove_from_allowlist(self, user_id):
        if self.is_allowed(user_id):
            self.allowlist.delete_one({"_id": user_id})
            return True
        return False

    def get_allowlist(self):
        return [u["_id"] for u in self.allowlist.find()]

    def is_sudo(self, user_id):
        return self.sudos.find_one({"_id": user_id}) is not None

    def add_sudo(self, user_id):
        self.sudos.update_one({"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True)

    def remove_sudo(self, user_id):
        if self.is_sudo(user_id):
            self.sudos.delete_one({"_id": user_id})
            return True
        return False

    def get_sudos(self):
        return [u["_id"] for u in self.sudos.find()]

    def reset_warnings(self, user_id):
        self.warnings.delete_one({"_id": user_id})

    def add_warning(self, user_id):
        w = self.warnings.find_one_and_update({"_id": user_id}, {"$inc": {"count": 1}}, upsert=True, return_document=pymongo.ReturnDocument.AFTER)
        return w["count"]

db = PersistentDB()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== HELPERS & COMMANDS ==========
# [Extract Target, Admin Checks, etc. codes yahan honge jo aapne likhe hain]

# ... [Aapka baki pura code yahan aayega, jaise start_command, message_handler, etc.] ...
# Note: Maine code length ki wajah se yahan functions skip kiye hain, par aapko apne baki commands as it is rakhne hain.

# ========== HELPERS ==========
async def is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS: return True
    try:
        chat_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return chat_member.status in ['administrator', 'creator']
    except: return False

def has_link(text):
    if not text: return False
    link_patterns = [r'http[s]?://\S+', r'www\.\S+', r't\.me/\S+', r'\S+\.(com|org|net|in|co|io|xyz|me|info)\b']
    for pattern in link_patterns:
        if re.search(pattern, text, re.IGNORECASE): return True
    return False

async def extract_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[int | None, str | None, str]:
    """Extracts Target User ID and Name from Reply, ID, Username, or Mention."""
    message = update.message
    args = context.args

    # 1. Reply Check
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        reason = " ".join(args) if args else "No reason"
        return user.id, user.first_name, reason

    if not args:
        return None, None, "❗ Please reply to a user, or provide their ID/Username."

    identifier = args[0]
    reason = " ".join(args[1:]) if len(args) > 1 else "No reason"

    # 2. Text Mention Check (Name tags without @username)
    if message.entities:
        for entity in message.entities:
            if entity.type == 'text_mention':
                return entity.user.id, entity.user.first_name, reason

    # 3. User ID Check
    if identifier.isdigit() or (identifier.startswith('-') and identifier[1:].isdigit()):
        try:
            user_id = int(identifier)
            chat = await context.bot.get_chat(user_id)
            return user_id, chat.first_name, reason
        except:
            pass

    # 4. @Username Check
    if identifier.startswith('@'):
        try:
            chat = await context.bot.get_chat(identifier)
            return chat.id, chat.first_name, reason
        except:
            pass

    return None, None, "❌ User nahi mila. Kripya sahi ID, Username, ya Reply ka use karein."
    
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context):
        await update.message.reply_text("❌ You have not permission.")
        return
        
    target_id, target_name, _ = await extract_target(update, context)

    if not target_id:
        await update.message.reply_text("❗ Reply to a user, or provide their ID/Username to approve.")
        return

    # Check if the target user is an admin
    is_target_admin = False
    if target_id in ADMIN_IDS:
        is_target_admin = True
    elif update.effective_chat.type != 'private':
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, target_id)
            if member.status in ['administrator', 'creator']:
                is_target_admin = True
        except Exception:
            pass
            
    if is_target_admin:
        await update.message.reply_text("user is already admin admins are already approved")
        return

    db.add_to_allowlist(target_id)
    db.reset_warnings(target_id)
    safe_name = target_name or str(target_id)
    await update.message.reply_text(f"✅ **{safe_name}** (`{target_id}`) has been whitelisted.", parse_mode='Markdown')

async def unapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context):
        await update.message.reply_text("❌ You have not permission.")
        return
        
    target_id, target_name, _ = await extract_target(update, context)

    if not target_id:
        await update.message.reply_text("❗ Reply to a user, or provide their ID/Username to unapprove.")
        return

    # Check if the target user is an admin
    is_target_admin = False
    if target_id in ADMIN_IDS:
        is_target_admin = True
    elif update.effective_chat.type != 'private':
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, target_id)
            if member.status in ['administrator', 'creator']:
                is_target_admin = True
        except Exception:
            pass
            
    if is_target_admin:
        await update.message.reply_text("this user is an admin they cannot be unapproved")
        return

    safe_name = target_name or str(target_id)
    if db.remove_from_allowlist(target_id):
        await update.message.reply_text(f"❌ **{safe_name}** (`{target_id}`) removed from whitelist.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"**{safe_name}** (`{target_id}`) was not in the whitelist.", parse_mode='Markdown')

    # ========== JOBS & CALLBACKS ==========
async def delete_msg_job(context: ContextTypes.DEFAULT_TYPE):
    try: await context.bot.delete_message(chat_id=context.job.chat_id, message_id=context.job.data)
    except: pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    clicker_id = update.effective_user.id

    # ==========================================
    # 🟢 OPEN BUTTONS (EVERYONE CAN USE THESE)
    # ==========================================
    if query.data == "help_main":
        help_text = (
            "🤖 **BOT COMMANDS MENU**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👤 **USER COMMANDS**\n"
            "• `/start` : Check bot status\n"
            "• `/status` : Check security stats\n"
            "• `/help` : Show this menu\n\n"
            "🛠 **ADMIN COMMANDS**\n"
             "━━━━━━━━━━━━━━━━━━━━\n"
            "• `/antichannel on/off` : Stop channel posts\n"
            "• `/settings <warn> <hrs>` : Warn/Mute limits\n"
            "• `/delay <min>` : Media auto-delete\n"
            "• `/approve` : Whitelist a user\n"
            "• `/unapprove` : Remove from whitelist\n"
            "• `/aplist` : List whitelist users\n"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        await query.answer()
        return

    elif query.data == "back_to_start":
        await start_command(update, context)
        await query.answer()
        return

    elif query.data == "delete_msg" or query.data.startswith("delmsg_"):
        # Anyone can click these delete buttons (used in status, warnings, and edited messages)
        try: await query.message.delete()
        except: pass
        await query.answer()
        return

    # ==========================================
    # 🔴 RESTRICTED BUTTONS (ADMINS ONLY)
    # ==========================================
    is_private = update.effective_chat.type == 'private'
    is_admin = is_private or await is_user_admin(update, context)

    if not is_admin:
        await query.answer("❌ Only admins can use this button.", show_alert=True)
        return

    await query.answer() # Answer query to stop the loading circle for admins

    if "_" in query.data:
        parts = query.data.split("_")
        action = parts[0]
        
        # Make sure we actually have a target ID before converting to int
        if len(parts) > 1 and parts[-1].lstrip('-').isdigit():
            target_id = int(parts[-1])

            if action == "approve":
                db.add_to_allowlist(target_id)
                db.reset_warnings(target_id)
                keyboard = [[InlineKeyboardButton("❌ Unapprove", callback_data=f"unapprove_{target_id}"), InlineKeyboardButton("🧹 cancle warning", callback_data=f"cancle warning_{target_id}")],
                            [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
                await context.bot.send_message(chat_id, f"✅ **Approved:** User `{target_id}` has been whitelisted.", parse_mode='Markdown')

            elif action == "unapprove":
                db.remove_from_allowlist(target_id)
                keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_id}"), InlineKeyboardButton("🧹 cancle warning", callback_data=f"cancle warning_{target_id}")],
                            [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
                await context.bot.send_message(chat_id, f"❌ **Unapproved:** User `{target_id}` removed from whitelist.", parse_mode='Markdown')

            elif action in ["unwarn", "cancle warning"]:
                db.reset_warnings(target_id)
                await context.bot.send_message(chat_id, f"🧹 **Warnings Cleared:** User `{target_id}` is now warning-free.", parse_mode='Markdown')
            
            elif action == "unmute":
                try:
                    await context.bot.restrict_chat_member(
                        chat_id=chat_id, 
                        user_id=target_id, 
                        permissions=ChatPermissions(
                            can_send_messages=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True,
                            can_invite_users=True
                        )
                    )
                    db.reset_warnings(target_id)
                    await query.edit_message_text(text=f"✅ User `{target_id}` has been **Unmuted**.", parse_mode='Markdown')
                    await context.bot.send_message(chat_id, f"🔓 **Unmuted:** User `{target_id}` can now chat.", parse_mode='Markdown')
                except Exception as e:
                    await context.bot.send_message(chat_id, f"❌ **Error:** Could not unmute. Please check my admin permissions.", parse_mode='Markdown')

async def auto_reset_on_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_member:
        return

    old = update.chat_member.old_chat_member
    new = update.chat_member.new_chat_member

    # Pehle restricted tha (muted)
    if old.status == "restricted" and not old.can_send_messages:

        # Ab normal member ho gaya (unmuted)
        if new.status in ("member", "administrator", "creator") or \
           (new.status == "restricted" and new.can_send_messages):

            user_id = new.user.id
            db.reset_warnings(user_id)

            await context.bot.send_message(
                update.effective_chat.id,
                f"🔄 {new.user.mention_html()} has been unmuted.",
                parse_mode="HTML"
            )

# ========== COMMANDS ==========

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_user = await context.bot.get_me()
    chat = update.effective_chat
    
    # 1. Database Logging (Keep this separate to track DMs vs Groups correctly)
    if chat.type == 'private':
        db.add_user(update.effective_user.id) 
    else:
        db.add_group(chat.id, chat.title)

    # 2. Universal Message & Keyboard (Applies to both DMs and Groups)
    CHANNEL_URL = "https://t.me/+rjE5xZlIK4U3ODA1"
    text = (
        f"👋 **𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 {bot_user.first_name}!**\n\n"
        "𝗜 𝗮𝗺 𝗮𝗻 𝗮𝗱𝘃𝗮𝗻𝗰𝗲𝗱 𝘀𝗲𝗰𝘂𝗿𝗶𝘁𝘆 𝗯𝗼𝘁 𝗱𝗲𝘀𝗶𝗴𝗻𝗲𝗱 𝘁𝗼 𝗽𝗿𝗼𝘁𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗴𝗿𝗼𝘂𝗽.\n\n"
        "🗑 **𝗠𝗲𝗱𝗶𝗮 𝗖𝗹𝗲𝗮𝗻𝗲𝗿**: Auto-deletes media after a set time.\n"
        "✏️ **𝗘𝗱𝗶𝘁 𝗚𝘂𝗮𝗿𝗱**: Deletes edited messages to prevent spam.\n"
        "🚫 **𝗔𝗻𝘁𝗶-𝗟𝗶𝗻𝗸**: Removes URLs instantly.\n"
        "🛡️ **𝐁𝐢𝐨 𝐆𝐮𝐚𝐫𝐝**: Scan bios for links and restrict users.\n"
        "🔒 **𝐀𝐧𝐭𝐢-𝐂𝐡𝐚𝐧𝐧𝐞𝐥**: Blocks anonymous posts sent via Telegram Channels.\n"
        "🔞 **𝐍𝐒𝐅𝐖 𝐁𝐥𝐨𝐜𝐤𝐞𝐝**: Filter unwanted object.\n\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("➕ 𝐀𝐝𝐝 𝐭𝐨 𝐆𝐫𝐨𝐮𝐩", url=f"https://t.me/{bot_user.username}?startgroup=true")],
        [InlineKeyboardButton("𝐇𝐞𝐥𝐩❓", callback_data="help_main"), InlineKeyboardButton("📢 Support Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("𝗖𝗹𝗼𝘀𝗲 🗑", callback_data="delete_msg")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 3. Send the Output
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **BOT COMMANDS MENU**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 **USER COMMANDS**\n"
        "• /start : Check bot status\n"
        "• /status : Check security stats\n"
        "• /help : Show this menu\n\n"
        "🛠 **ADMIN COMMANDS**\n"
         "━━━━━━━━━━━━━━━━━━━━\n"
        "• /antichannel on/off : Stop channel posts\n"
        "• /settings <warn> <hrs> : Warn/Mute limits\n"
        "• /delay <min> : Media auto-delete\n"
        "• /approve : Whitelist a user\n"
        "• /unapprove : Remove from whitelist\n"
        "• /aplist : List whitelist users\n"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Fetch the bot's name (Fixes NameError)
    bot_info = await context.bot.get_me()
    bot_name = bot_info.first_name

    # 2. Fetch Stats from MongoDB
    stats = db.get_global_stats() 
    scanned, bio_caught, media_del, warns_issued, nsfw_blocked, start_timestamp = stats
    
    # 3. Monitored Groups calculation
    groups = db.get_groups()
    group_count = len(groups) if groups else 0
    
    # 4. Permanent Uptime Calculation
    bot_start_time = datetime.fromtimestamp(start_timestamp, IST)
    uptime_delta = datetime.now(IST) - bot_start_time
    days = uptime_delta.days
    hours, remainder = divmod(uptime_delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"
    
    # 5. Build the text using HTML (Very stable for Telegram)
    text = (
        f"<b>{bot_name}</b>\n\n"
        "📊 <b>SYSTEM STATS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👁️ <b>Total Scanned:</b> <code>{scanned}</code>\n"
        f"☣️ <b>Bio Link Caught:</b> <code>{bio_caught}</code>\n"
        f"🗑 <b>Media Deleted:</b> <code>{media_del}</code>\n"
        f"⚠️ <b>Warnings Issued:</b> <code>{warns_issued}</code>\n"
        f"🔞 <b>NSFW Blocked:</b> <code>{nsfw_blocked}</code>\n"
        f"🏘 <b>Monitored Groups:</b> <code>{group_count}</code>\n"
        f"⏳ <b>Uptime:</b> <code>{uptime_str}</code>\n"
    )
    
    keyboard = [[InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)

async def set_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context): 
        await update.message.reply_text("❌ You have not permission.")
        return
    try:
        warns, hours = int(context.args[0]), int(context.args[1])
        db.set_limits(update.effective_chat.id, warns, hours)
        sent_msg = await update.message.reply_text(f"✅ Limit {warns}, Mute {hours}h")
    except: 
        sent_msg = await update.message.reply_text("Usage: `/settings <warns> <hours>`")
    context.job_queue.run_once(delete_msg_job, 30, chat_id=update.effective_chat.id, data=sent_msg.message_id)

async def set_delay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context): 
        await update.message.reply_text("❌ You have not permission.")
        return
    chat_id = update.effective_chat.id
    if context.args:
        try:
            mins = int(context.args[0])
            db.set_delay(chat_id, mins)
            sent_msg = await update.message.reply_text(f"✅ Media delete set to {mins}m.")
        except: 
            sent_msg = await update.message.reply_text("Usage: `/delay <minutes>`")
    else:
        current_delay = db.get_settings(chat_id)[0]
        sent_msg = await update.message.reply_text(f"Current Delay: {current_delay}m")
    context.job_queue.run_once(delete_msg_job, 30, chat_id=chat_id, data=sent_msg.message_id)

async def aplist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context): 
        await update.message.reply_text("❌ You have not permission.")
        return
    allowlist = db.get_allowlist()
    if not allowlist:
        await update.message.reply_text("Approved list is empty.")
        return
    text = "✅ **Approved Users:**\n\n"
    for idx, uid in enumerate(allowlist, 1):
        text += f"{idx}. `{uid}`\n"
    await update.message.reply_text(text, parse_mode='Markdown')

# ==========================================
#      OWNER TOOLS: BROADCAST & GROUP LIST
# ==========================================

async def grouplist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all active groups with a Serial Number."""
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
    
    # SQLite Database se groups nikal rahe hain
    groups = db.get_groups() 
    
    if not groups:
        await update.message.reply_text("📭 I am not currently active in any groups.")
        return
        
    text = "📋 <b>Active Group List:</b>\n\n"
    for idx, (cid, title) in enumerate(groups, 1):
        safe_title = html.escape(title or 'Unknown Group')
        text += f"<b>{idx}.</b> {safe_title} (<code>{cid}</code>)\n"
        
    if len(text) > 4000:
        text = text[:4000] + "\n... (List too long, truncated)"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates an invite link for a group using its Serial Number."""
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
        
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❗ <b>Usage:</b> <code>/getlink <serial_no></code>\nGet the serial number from <code>/grouplist</code>.", parse_mode='HTML')
        return
        
    s_no = int(context.args[0])
    groups = db.get_groups()
    
    if s_no < 1 or s_no > len(groups):
        await update.message.reply_text("❌ Invalid Serial Number.")
        return
        
    target_chat_id = groups[s_no - 1][0]
    target_title = groups[s_no - 1][1]
    
    try:
        chat = await context.bot.get_chat(target_chat_id)
        invite_link = chat.invite_link or await context.bot.export_chat_invite_link(target_chat_id)
        await update.message.reply_text(f"🔗 <b>Link for {html.escape(target_title or 'Group')}:</b>\n{invite_link}", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Could not generate link. Make sure I am an Admin with 'Invite Users' permission.\nError: <code>{e}</code>", parse_mode='HTML')

async def gmsg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a specific message to a group using its Serial Number with Pin/Unpin support."""
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
        
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❗ <b>Usage:</b> <code>/gmsg <serial_no> [-pin/-unpin] <message></code>", parse_mode='HTML')
        return
        
    s_no = int(context.args[0])
    groups = db.get_groups()
    
    if s_no < 1 or s_no > len(groups):
        await update.message.reply_text("❌ Invalid Serial Number.")
        return
        
    target_chat_id = groups[s_no - 1][0]
    target_title = groups[s_no - 1][1]
    
    # Check for pin/unpin tags
    args_text = " ".join(context.args[1:])
    should_pin = "-pin" in args_text
    should_unpin = "-unpin" in args_text
    
    # Message me se -pin aur -unpin tags ko remove kar dena taaki group me na dikhe
    clean_text = args_text.replace("-pin", "").replace("-unpin", "").strip()
    
    try:
        # Agar -unpin likha hai, toh pehle purane sabhi messages unpin kar do
        if should_unpin:
            try: await context.bot.unpin_all_chat_messages(target_chat_id)
            except: pass

        sent_message = None
        if update.message.reply_to_message:
            sent_message = await update.message.reply_to_message.copy(target_chat_id)
        elif clean_text:
            sent_message = await context.bot.send_message(target_chat_id, clean_text)
        elif not should_unpin: # Agar koi message nahi hai aur sirf gmsg likha hai
            await update.message.reply_text("Please provide text or reply to a message/media.")
            return
            
        # Agar -pin likha hai aur message send hua hai, toh use pin kar do
        if should_pin and sent_message:
            try: await context.bot.pin_chat_message(chat_id=target_chat_id, message_id=sent_message.message_id)
            except: pass
            
        status_text = f"✅ Message sent to <b>{html.escape(target_title or 'Group')}</b>."
        if should_pin: status_text += "\n📌 Message Pinned!"
        if should_unpin: status_text += "\n🧹 Previous messages Unpinned!"
        
        await update.message.reply_text(status_text, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Failed.\nError: <code>{e}</code>", parse_mode='HTML')

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcasts a message to all active groups/DMs with Pin/Unpin support."""
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
        
    reply_msg = update.message.reply_to_message
    args_text = " ".join(context.args)
    
    should_pin = "-pin" in args_text
    should_unpin = "-unpin" in args_text
    clean_text = args_text.replace("-pin", "").replace("-unpin", "").strip()
    
    if not reply_msg and not clean_text and not should_unpin:
        await update.message.reply_text("❗ <b>Usage:</b> Reply or type <code>/broadcast [-pin/-unpin] <text></code>", parse_mode='HTML')
        return

    status_msg = await update.message.reply_text("⏳ <b>Starting Broadcast...</b>\nThis may take a moment.", parse_mode='HTML')
    
    targets = db.get_all_targets()
    success, failed, pinned = 0, 0, 0
    
    for target_id in targets:
        try:
            if should_unpin:
                try: await context.bot.unpin_all_chat_messages(target_id)
                except: pass

            sent_message = None
            if reply_msg:
                sent_message = await reply_msg.copy(target_id)
            elif clean_text:
                sent_message = await context.bot.send_message(target_id, clean_text)
                
            if should_pin and sent_message:
                try: 
                    await context.bot.pin_chat_message(chat_id=target_id, message_id=sent_message.message_id)
                    pinned += 1
                except: pass
                
            if sent_message or should_unpin:
                success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
                
    await status_msg.edit_text(
        f"✅ <b>Broadcast Complete!</b>\n\n"
        f"🎯 Successfully Sent: <code>{success}</code>\n"
        f"📌 Successfully Pinned: <code>{pinned}</code>\n"
        f"❌ Failed/Blocked: <code>{failed}</code>", 
        parse_mode='HTML'
    )

async def cleangroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scans the database and removes groups where the bot is no longer a member."""
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
        
    status_msg = await update.message.reply_text("⏳ <b>Scanning Database...</b>\nChecking all groups to see if I am still a member. This might take a minute.", parse_mode='HTML')
    
    groups = db.get_groups()
    removed_count = 0
    active_count = 0
    
    for chat_id, title in groups:
        try:
            # Bot check karega ki kya wo is group me abhi bhi hai
            await context.bot.get_chat(chat_id)
            active_count += 1
            await asyncio.sleep(0.1) # Telegram API ko spam hone se bachane ke liye thoda delay
        except (Forbidden, BadRequest):
            # Agar bot group se nikala ja chuka hai, toh Forbidden error aayega
            db.remove_group(chat_id)
            removed_count += 1
        except Exception as e:
            pass
            
    await status_msg.edit_text(
        f"✅ <b>Database Cleanup Complete!</b>\n\n"
        f"🗑️ <b>Removed Dead Groups:</b> <code>{removed_count}</code>\n"
        f"🟢 <b>Active Groups Left:</b> <code>{active_count}</code>", 
        parse_mode='HTML'
    )

async def addsudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Only the Bot Owner can use this command.")
        return

    target_id, target_name, _ = await extract_target(update, context)
    if not target_id:
        await update.message.reply_text("❗ Reply to a user, or provide their ID/Username to add as Sudo.")
        return

    if target_id in ADMIN_IDS:
        await update.message.reply_text("This user is already the Bot Owner.")
        return

    db.add_sudo(target_id)
    safe_name = target_name or str(target_id)
    await update.message.reply_text(f"👑 **{safe_name}** (`{target_id}`) has been promoted to Sudo Admin.", parse_mode='Markdown')

async def rmsudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Only the Bot Owner can use this command.")
        return

    target_id, target_name, _ = await extract_target(update, context)
    if not target_id:
        await update.message.reply_text("❗ Reply to a user, or provide their ID/Username to remove from Sudo.")
        return

    safe_name = target_name or str(target_id)
    if db.remove_sudo(target_id):
        await update.message.reply_text(f"❌ **{safe_name}** (`{target_id}`) removed from Sudo Admins.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"**{safe_name}** (`{target_id}`) is not a Sudo Admin.", parse_mode='Markdown')

async def sudolist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Only the Bot Owner can use this command.")
        return

    sudos = db.get_sudos()
    if not sudos:
        await update.message.reply_text("📭 The Sudo list is empty.")
        return
    
    text = "👑 **Sudo Admins:**\n\n"
    for idx, uid in enumerate(sudos, 1):
        text += f"{idx}. `{uid}`\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def nsfw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # 🔒 STRICT LOCK: Sirf Owner aur Sudo Admins ke liye
    if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
        await update.message.reply_text("❌ Only the Bot Owner or Sudo Admins can use this command.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ <b>Usage:</b>\nIn Group: <code>/nsfw on/off</code>\nRemote: <code>/nsfw <serial_no> on/off</code>\nGlobal: <code>/nsfw all on/off</code>", parse_mode='HTML')
        return 
    
    # CASE 1: Global Control (e.g., /nsfw all off)
    if args[0].lower() == "all" and len(args) == 2:
        state_str = args[1].lower()
        if state_str not in ['on', 'off']:
            await update.message.reply_text("❗ <b>Usage:</b> <code>/nsfw all on</code> or <code>off</code>", parse_mode='HTML')
            return
            
        state = (state_str == "on")
        groups = db.get_groups()
        for chat_id, _ in groups:
            db.set_nsfw(chat_id, state)
            
        await update.message.reply_text(f"✅ <b>Global Update:</b> NSFW Filter is now <b>{'ENABLED' if state else 'DISABLED'}</b> in ALL {len(groups)} groups.", parse_mode='HTML')
        return

    # CASE 2: Remote Control using Serial Number (e.g., /nsfw 1 off)
    if len(args) == 2 and args[0].isdigit():
        s_no = int(args[0])
        state_str = args[1].lower()
        
        groups = db.get_groups()
        if s_no < 1 or s_no > len(groups):
            await update.message.reply_text("❌ Invalid Serial Number.")
            return
            
        target_chat_id = groups[s_no - 1][0]
        target_title = groups[s_no - 1][1]
        state = (state_str == "on")
        
        db.set_nsfw(target_chat_id, state)
        await update.message.reply_text(f"✅ <b>NSFW Filter</b> is now <b>{'ENABLED' if state else 'DISABLED'}</b> for group:\n📍 <b>{html.escape(target_title)}</b>", parse_mode='HTML')
        return

    # CASE 3: Normal Control in the current group (e.g., /nsfw off)
    state_str = args[0].lower()
    if state_str not in ['on', 'off']:
        await update.message.reply_text("❗ <b>Usage:</b> <code>/nsfw on</code> or <code>off</code>", parse_mode='HTML')
        return
        
    state = (state_str == "on")
    db.set_nsfw(update.effective_chat.id, state)
    await update.message.reply_text(f"🔞 <b>NSFW Filter</b> is now <b>{'ENABLED' if state else 'DISABLED'}</b> in this group.", parse_mode='HTML')

async def antichannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    
    if not args:
        await update.message.reply_text("❗ <b>Usage:</b>\nIn Group: <code>/antichannel on/off</code>\nRemote: <code>/antichannel <serial_no> on/off</code>\nGlobal: <code>/antichannel all on/off</code>", parse_mode='HTML')
        return 
        
    # CASE 1: Global Control (e.g., /antichannel all off)
    if args[0].lower() == "all" and len(args) == 2:
        if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
            await update.message.reply_text("❌ Only Bot Owner or Sudo Admins can use global control.")
            return
            
        state_str = args[1].lower()
        if state_str not in ['on', 'off']:
            await update.message.reply_text("❗ <b>Usage:</b> <code>/antichannel all on</code> or <code>off</code>", parse_mode='HTML')
            return
            
        state = (state_str == "on")
        groups = db.get_groups()
        
        for chat_id, _ in groups:
            db.set_anti_channel(chat_id, state)
            
        await update.message.reply_text(f"✅ <b>Global Update:</b> Anti-Channel is now <b>{'ENABLED' if state else 'DISABLED'}</b> in ALL {len(groups)} groups.", parse_mode='HTML')
        return

    # CASE 2: Remote Control using Serial Number (e.g., /antichannel 1 off)
    if len(args) == 2 and args[0].isdigit():
        if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
            await update.message.reply_text("❌ Only Bot Owner or Sudo Admins can use remote control.")
            return
            
        s_no = int(args[0])
        state_str = args[1].lower()
        
        groups = db.get_groups()
        if s_no < 1 or s_no > len(groups):
            await update.message.reply_text("❌ Invalid Serial Number.")
            return
            
        target_chat_id = groups[s_no - 1][0]
        target_title = groups[s_no - 1][1]
        state = (state_str == "on")
        
        db.set_anti_channel(target_chat_id, state)
        await update.message.reply_text(f"✅ <b>Anti-Channel</b> is now <b>{'ENABLED' if state else 'DISABLED'}</b> for group:\n📍 <b>{html.escape(target_title)}</b>", parse_mode='HTML')
        return

    # CASE 3: Normal Control in the current group (e.g., /antichannel off)
    is_admin = await is_user_admin(update, context)
    if not is_admin and not db.is_sudo(user_id):
        await update.message.reply_text("❌ You do not have permission to use this command here.")
        return

    state_str = args[0].lower()
    if state_str not in ['on', 'off']:
        await update.message.reply_text("❗ <b>Usage:</b> <code>/antichannel on</code> or <code>off</code>", parse_mode='HTML')
        return
        
    state = (state_str == "on")
    db.set_anti_channel(update.effective_chat.id, state)
    await update.message.reply_text(f"🚫 <b>Anti-Channel</b> is now <b>{'ENABLED' if state else 'DISABLED'}</b> in this group.", parse_mode='HTML')

async def check_image_nsfw_api(file_path: str) -> bool:
    """Hugging Face Inference API ka use karke NSFW check karein (0% RAM Usage)"""
    if not HF_TOKEN:
        logger.error("HF_TOKEN is missing!")
        return False
        
    try:
        with open(file_path, "rb") as f:
            image_data = f.read()
        
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        
        # API call in a thread to prevent blocking
        def call_api():
            response = requests.post(NSFW_API_URL, headers=headers, data=image_data, timeout=10)
            return response.json()
            
        results = await asyncio.to_thread(call_api)
        
        # Result format: [{'label': 'nsfw', 'score': 0.98}, ...]
        if isinstance(results, list):
            for result in results:
                if result.get('label') == 'nsfw' and result.get('score', 0) > 0.60:
                    return True
        elif 'error' in results:
            logger.error(f"HF API Error: {results['error']}")
                
    except Exception as e:
        logger.error(f"NSFW API Exception: {e}")
        
    return False

async def greply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies to a specific message in a group."""
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
        
    args = context.args
    if len(args) < 3 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("❗ **Usage:** `/greply <serial_no> <message_id> <your_message>`\nExample: `/greply 1 456 Hello bhai!`", parse_mode='Markdown')
        return
        
    s_no, msg_id = int(args[0]), int(args[1])
    text = " ".join(args[2:])
    groups = db.get_groups()
    
    if s_no < 1 or s_no > len(groups):
        await update.message.reply_text("❌ Invalid Serial Number.")
        return
        
    target_chat_id = groups[s_no - 1][0]
    
    try:
        await context.bot.send_message(chat_id=target_chat_id, text=text, reply_to_message_id=msg_id)
        await update.message.reply_text("✅ Reply sent successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send reply.\nMake sure the message ID is correct.\nError: `{e}`", parse_mode='Markdown')


async def greact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adds a reaction to a specific message in a group."""
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
        
    args = context.args
    if len(args) < 3 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("❗ **Usage:** `/greact <serial_no> <message_id> <emoji>`\nExample: `/greact 1 456 ❤️`", parse_mode='Markdown')
        return
        
    s_no, msg_id = int(args[0]), int(args[1])
    emoji = args[2]
    groups = db.get_groups()
    
    if s_no < 1 or s_no > len(groups):
        await update.message.reply_text("❌ Invalid Serial Number.")
        return
        
    target_chat_id = groups[s_no - 1][0]
    
    try:
        await context.bot.set_message_reaction(chat_id=target_chat_id, message_id=msg_id, reaction=[ReactionTypeEmoji(emoji)])
        await update.message.reply_text(f"✅ Reaction {emoji} added successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to add reaction.\nMake sure the message ID is correct and the emoji is allowed in the group.\nError: `{e}`", parse_mode='Markdown')

# ========== HANDLERS ==========
async def edited_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    if not msg or not msg.from_user: return
    
    # 1. Message ko turant delete karna
    try:
        await msg.delete()
    except:
        pass # Agar bot ke paas delete permission nahi hui toh error nahi aayega
        
    # 2. Edit kiye hue message ka text nikalna
    edited_text = msg.text or msg.caption or "Media/Unsupported Content"
    safe_text = html.escape(edited_text)
    
    # 3. Notification message banana (Spoiler ke sath)
    alert_text = (
        f"<b>❌ Edit Detected & Deleted</b>\n\n"
        f"<b>User:</b>-{msg.from_user.mention_html()}\n"
        f"<b>Action:</b> Attempted to edit message\n"
        f"<b>Edited Message:</b>- <tg-spoiler>{safe_text}</tg-spoiler>\n\n"
        f"<b>Group Rule:</b> Editing is not allowed. Please send a new message instead."
    )
    
    # 4. Custom Inline Button banana (Jisme user ki ID chhupi hogi)
    keyboard = [[InlineKeyboardButton("OK / Delete 🗑", callback_data=f"delmsg_{msg.from_user.id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 5. Notification bhejna
    try:
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=alert_text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
    except:
        pass

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user = update.message.from_user
    chat_id = update.effective_chat.id
    
    # UPDATE SCANNED STAT
    db.update_stat('scanned')

    # 1. Identify Channel Posts
    is_channel_post = False
    
    # Updated logic: Check if the message has a forward origin and if it's from a channel
    if update.message.forward_origin and update.message.forward_origin.type == 'channel':
        is_channel_post = True
        
    # Case B: Send as Channel (Anonymous posting by admins)
    if update.message.sender_chat and update.message.sender_chat.type == 'channel':
        if not getattr(update.message, 'is_automatic_forward', False):
            is_channel_post = True

    # Get Settings
    settings = db.get_settings(chat_id)
    anti_channel_enabled = settings[4] if len(settings) > 4 else 1

    # Determine if user is Admin or Approved
    is_exempt = False
    if user:
        if user.id in ADMIN_IDS or db.is_allowed(user.id):
            is_exempt = True
        else:
            try:
                mem = await context.bot.get_chat_member(chat_id, user.id)
                if mem.status in ['administrator', 'creator']:
                    is_exempt = True
            except: pass

    # ANTI-CHANNEL LOGIC
    if is_channel_post:
        if anti_channel_enabled:
            # If ON and user is NOT an admin/approved, delete immediately
            if not is_exempt:
                try:
                    await update.message.delete()
                    return # Stop execution here
                except: pass
        else:
            # 💡 MAIN FIX: If OFF, bypass all other strict filters (Link/Virus) for this channel post!
            is_exempt = True 

    # 2. Private / Group Logic
    if update.effective_chat.type == 'private':
        if user: db.add_user(user.id)
        return
    
    db.add_group(chat_id, update.effective_chat.title)
    # Update this line to fetch all 6 variables
    delay_min, warn_limit, mute_hrs, _, anti_ch, nsfw_enabled = db.get_settings(chat_id)

    # Media Logic (Applies to everyone)
    is_media = any([update.message.photo, update.message.video, update.message.document, 
                    update.message.animation, update.message.voice, update.message.sticker])
    if is_media:
        db.update_stat('media_deleted')
        context.job_queue.run_once(delete_msg_job, delay_min * 60, chat_id=chat_id, data=update.message.message_id)

    if not user: return 
    msg_text = update.message.text or update.message.caption
    
    # ===================================================================
    # UNIVERSAL NSFW DETECTION (Applies to Admin/Owner/Approved too)
    # Covers Media, Document, Video, Sticker, GIF
    # ===================================================================
    file_id = None
    temp_file_path = f"temp_nsfw_{chat_id}_{update.message.message_id}.jpg"

    # 1. Photos
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        
    # 2. Stickers (Static or Animated, getting thumbnail/image)
    elif update.message.sticker:
        if getattr(update.message.sticker, 'is_animated', False) or getattr(update.message.sticker, 'is_video', False):
            if update.message.sticker.thumbnail:
                file_id = update.message.sticker.thumbnail.file_id
        else:
            file_id = update.message.sticker.file_id
            
    # 3. Videos (Scanning thumbnail instead of full video)
    elif update.message.video and update.message.video.thumbnail:
        file_id = update.message.video.thumbnail.file_id
        
    # 4. Documents (If document has a thumbnail)
    elif update.message.document and update.message.document.thumbnail:
        file_id = update.message.document.thumbnail.file_id
        
    # 5. Animations / GIFs
    elif update.message.animation and update.message.animation.thumbnail:
        file_id = update.message.animation.thumbnail.file_id

    # Check if we found a valid file_id AND nsfw is enabled
    if file_id and nsfw_enabled:
        try:
            file = await context.bot.get_file(file_id)
            await file.download_to_drive(temp_file_path)
            # ... (rest of the NSFW logic stays exactly the same)
            # Call AI Scanner (Hugging Face)
            is_explicit = await check_image_nsfw_api(temp_file_path)
            
            # Remove temp file immediately
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            
            if is_explicit:

                # 👇 NAYI LINE: NSFW counter ko badhane ke liye
                db.update_stat('nsfw_blocked')

                # 1. INSTANT DELETE MESSAGE (SAFE WAY)
                try:
                    await update.message.delete()
                except Exception as e:
                    pass # Agar permission na ho to crash na ho
                
                # 2. Add warning to database
                db.update_stat('warnings_issued')
                db.add_warning(user.id)
                
                # 3. Silently Tag Admins in the Group
                admin_tags = " ".join([f'<a href="tg://user?id={aid}">👮‍♂️ Admin</a>' for aid in ADMIN_IDS])
                
                admin_alert = (
                    f"🚨 <b>NSFW Content Detected Please Take Action</b>\n\n"
                    f"👤 <b>Sender:</b> {user.mention_html()}\n"
                    f"🔔 {admin_tags}\n"
                )
                
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, 
                        text=admin_alert, 
                        parse_mode='HTML', 
                        disable_notification=True 
                    )
                except Exception as e:
                    print(f"Group Alert Error: {e}")
                    
                return # Stop processing this message further
                
        except Exception as e:
            logger.error(f"Universal NSFW Processing Error: {e}")
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

    # ===================================================================
    # VIOLATION CHECKS (Anti-Link, Bio Shield & Anti-Virus)
    # ===================================================================
    
    # Only proceed if User is NOT exempt
    if not is_exempt:
        violation, reason = False, ""
        
        # BIO SHIELD
        try:
            u_chat = await context.bot.get_chat(user.id)
            if u_chat.bio and has_link(u_chat.bio): 
                violation, reason = True, "Link in Bio"
                db.update_stat('bio_caught')
        except: pass
        
        # ANTI-LINK
        if not violation and has_link(msg_text): 
            violation, reason = True, "Link in Message"

        # MALICIOUS FILE BLOCKER (Anti-Virus)
        if not violation and update.message.document:
            file_name = update.message.document.file_name
            if file_name:
                ext = file_name.lower().split('.')[-1]
                if ext in ['apk', 'exe', 'bat', 'scr', 'vbs', 'js', 'zip', 'bin']:
                    violation, reason = True, f"Malicious File (.{ext})"

# PUNISHMENT LOGIC
        if violation:
            db.update_stat('warnings_issued')
            try: await update.message.delete()
            except: pass
            
            count = db.add_warning(user.id)
            
            # 👇 CORRECTED CODE: Limit cross hone par seedha mute bina error ke
            if count > warn_limit:
                try:
                    until = datetime.now(IST) + timedelta(hours=mute_hrs)
                    await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False), until_date=until)
                                        
                    # Notice sent WITHOUT the undefined keyboard and WITHOUT auto-delete
                    await context.bot.send_message(
                        chat_id, 
                        f"🤫 {user.mention_html()} is now muted.", 
                        parse_mode='HTML'
                    )
                    
                except Exception as e:
                    print(f"Mute Error: {e}") 
                    error_msg = f"🚨 <b>MUTE FAILED</b>\n <b>I don't have permission to mute users plz give me ban/restrict rights.</b>"
                    await context.bot.send_message(chat_id, error_msg, parse_mode='HTML')
                    
                return # Stop processing here

            display_count = count if count <= warn_limit else warn_limit
                
            # Isse badal kar HTML style kar dein
            base_info_text = (
            f"👤 <b>User:</b> {user.mention_html()}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
            f"🚫 <b>Reason:</b> {reason}\n"
            f"⚠️ <b>Warnings:</b> {display_count}/{warn_limit}" 
            )   
                
            # Notice lines for 1st and 2nd warnings
            notice_text = (
                "\n\n🛑 NOTICE: PLEASE REMOVE ANY LINKS FROM YOUR BIO IMMEDIATELY.\n\n"
                "📌 REPEATED VIOLATIONS MAY LEAD TO MUTE/BAN."
            )

            if count == warn_limit:
                until = datetime.now(IST) + timedelta(hours=mute_hrs)
                mute_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Unmute Now", callback_data=f"unmute_{user.id}")],
                                                    [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]])
                try:
                    await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False), until_date=until)
                    # Notice lines are REMOVED here when successfully muted
                    await context.bot.send_message(chat_id, f"🚫 **USER MUTED**\n\n{base_info_text}", parse_mode='HTML', reply_markup=mute_kb)
                except Exception as e:
                    # ONLY the permission error shows here if the bot lacks ban rights
                    error_msg = "🚨 <b>MUTE FAILED</b>\n<b>I don't have permission to mute users plz give me ban/restrict rights.</b>"
                    await context.bot.send_message(chat_id, error_msg, parse_mode='HTML')   
                  
            else: 
                is_app = db.is_allowed(user.id)
                app_btn = InlineKeyboardButton("❌ Unapprove", callback_data=f"unapprove_{user.id}") if is_app else InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}")
                keyboard = [[app_btn, InlineKeyboardButton("🧹 cancle warning", callback_data=f"cancle warning_{user.id}")],
                            [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
                # 1st and 2nd warnings show the FULL message including the notice
                await context.bot.send_message(chat_id, f"⚠️ **MESSAGE REMOVED**\n\n{base_info_text}{notice_text}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

# ========== ANTI-BOT SYSTEM ==========
async def anti_bot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members: return
    
    chat_id = update.effective_chat.id
    adder = update.message.from_user
    
    # Bypass check for Admins and Approved users
    is_adder_admin = False
    if adder.id in ADMIN_IDS:
        is_adder_admin = True
    else:
        try:
            mem = await context.bot.get_chat_member(chat_id, adder.id)
            if mem.status in ['administrator', 'creator']:
                is_adder_admin = True
        except: pass
        
    if is_adder_admin or db.is_allowed(adder.id):
        return 
        
    # Check if any new member is a bot
    for new_member in update.message.new_chat_members:
        if new_member.is_bot and new_member.id != context.bot.id:
            try:
                # 1. KICK the bot instantly (ban followed by immediate unban)
                await context.bot.ban_chat_member(chat_id, new_member.id)
                await context.bot.unban_chat_member(chat_id, new_member.id)
                
                # 2. Send the exact warning notification requested for the user
                alert_text = (
                    f"{adder.mention_html()} you cannot add bots in the group otherwise you restricted from this chat ."
                )
                
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Delete Message", callback_data="delete_msg")]
                ])
                await context.bot.send_message(chat_id, alert_text, parse_mode='HTML', reply_markup=kb)
                
            except Exception as e:
                # When the bot lacks ban permission, send exactly this message:
                error_msg = "Bot cannot be kicked because I have not permission to kick."
                try:
                    await context.bot.send_message(chat_id, error_msg)
                except:
                    pass

# ========== BOT STATUS TRACKER ==========
async def track_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically updates the database when the bot is added or kicked from a group."""
    result = update.my_chat_member
    if not result: return
        
    chat = result.chat
    new_status = result.new_chat_member.status

    if new_status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.BANNED]:
        # Agar bot ko group se nikala gaya
        db.remove_group(chat.id)
        logger.info(f"Bot removed from group: {chat.title} ({chat.id})")
    elif new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]:
        # Agar bot naye group me add hua
        db.add_group(chat.id, chat.title)
        logger.info(f"Bot added to group: {chat.title} ({chat.id})")

# ========== MAIN EXECUTION ==========
def main():
    # Application builder
    app_bot = Application.builder().token(TOKEN).connect_timeout(60).read_timeout(60).write_timeout(60).pool_timeout(60).build()
    
    # ✅ FIX: All handlers now use app_bot instead of app
    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(CommandHandler("help", help_command))
    app_bot.add_handler(CommandHandler("broadcast", broadcast_command))
    app_bot.add_handler(CommandHandler("delay", set_delay_command))
    app_bot.add_handler(CommandHandler("settings", set_settings_command))
    app_bot.add_handler(CommandHandler("status", status_command))
    app_bot.add_handler(CommandHandler("grouplist", grouplist_command))
    app_bot.add_handler(CommandHandler("aplist", aplist_command))
    app_bot.add_handler(CommandHandler("getlink", getlink_command))
    app_bot.add_handler(CommandHandler("gmsg", gmsg_command))
    app_bot.add_handler(CommandHandler("approve", approve_command))
    app_bot.add_handler(CommandHandler("unapprove", unapprove_command))
    app_bot.add_handler(CommandHandler("antichannel", antichannel_command))
    app_bot.add_handler(CommandHandler("cleangroups", cleangroups_command))
    app_bot.add_handler(CommandHandler("nsfw", nsfw_command))
    app_bot.add_handler(CommandHandler("addsudo", addsudo_command))
    app_bot.add_handler(CommandHandler("rmsudo", rmsudo_command))
    app_bot.add_handler(CommandHandler("sudolist", sudolist_command))
    app_bot.add_handler(CommandHandler("greply", greply_command))
    app_bot.add_handler(CommandHandler("greact", greact_command))

    app_bot.add_handler(CallbackQueryHandler(button_handler))
    app_bot.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.ChatType.GROUPS, edited_message_handler))
    app_bot.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, anti_bot_handler))
    app_bot.add_handler(MessageHandler((~filters.COMMAND), message_handler))

    app_bot.add_handler(ChatMemberHandler(auto_reset_on_unmute, ChatMemberHandler.CHAT_MEMBER))
    app_bot.add_handler(ChatMemberHandler(track_bot_status, ChatMemberHandler.MY_CHAT_MEMBER))
    
    print("Bot is running...")
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    keep_alive() # Flask server starts in background
    main() # Telegram bot starts
