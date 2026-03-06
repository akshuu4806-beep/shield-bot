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
    ContextTypes,
    TypeHandler,             # ADD THIS
    ApplicationHandlerStop   # ADD THIS
)

import time
from collections import defaultdict

# Add this right below your imports
BULK_DELETE_QUEUE = defaultdict(list)

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
admin_env = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_env.split(",") if x.strip().isdigit()]
IST = pytz.timezone('Asia/Kolkata')
# Sightengine Multiple API Keys Setup
# Add your keys in Render Environment Variables as SE_USER_1, SE_SECRET_1, etc.
SIGHTENGINE_KEYS = [
    {"user": os.environ.get("SE_USER_1"), "secret": os.environ.get("SE_SECRET_1")},
    {"user": os.environ.get("SE_USER_2"), "secret": os.environ.get("SE_SECRET_2")},
    {"user": os.environ.get("SE_USER_3"), "secret": os.environ.get("SE_SECRET_3")}
]

# ========== DATABASE CLASS ==========
class PersistentDB:
    def __init__(self):
        self.client = pymongo.MongoClient(MONGO_URL)
        self.db = self.client["shield_bot_db"]
        self.group_config = self.db["group_config"]
        self.allowlist = self.db["allowlist"]
        self.warnings = self.db["warnings"]
        self.users = self.db["users"]
        self.groups = self.db["groups"]
        self.global_stats = self.db["global_stats"]
        self.sudos = self.db["sudos"]
        self.blocked_stickers = self.db["blocked_stickers"] # NAYI LINE
        self.blocked_words = self.db["blocked_words"]       # NAYI LINE
        self._init_stats()
        # Add these two lines inside def __init__(self):
        self.local_blocked_words = self.db["local_blocked_words"]
        self.local_blocked_stickers = self.db["local_blocked_stickers"]
        # 'def __init__(self):' ke andar ye line add karein:
        self.gbans = self.db["gbans"]
        
    def _init_stats(self):
        stats = self.global_stats.find_one({"_id": 1})
        if not stats:
            self.global_stats.insert_one({
                "_id": 1, "scanned": 0, "bio_caught": 0, 
                "media_deleted": 0, "warnings_issued": 0,
                "nsfw_blocked": 0, "abuse_caught": 0, "bot_start_time": datetime.now(IST).timestamp()
            })

    def update_stat(self, column):
        self.global_stats.update_one({"_id": 1}, {"$inc": {column: 1}})

    def get_global_stats(self):
        stats = self.global_stats.find_one({"_id": 1})
        # Niche wali line dhyan se replace karna, isme abuse_caught add kiya hai
        return (stats.get("scanned", 0), stats.get("bio_caught", 0), stats.get("media_deleted", 0),
                stats.get("warnings_issued", 0), stats.get("nsfw_blocked", 0), stats.get("abuse_caught", 0), 
                stats.get("bot_start_time", datetime.now(IST).timestamp()))
        

    # (Fir isi class mein niche ye naye functions add kar dein)
    def add_blocked_sticker(self, set_name):
        self.blocked_stickers.update_one({"_id": set_name}, {"$set": {"_id": set_name}}, upsert=True)

    def remove_blocked_sticker(self, set_name):
        return self.blocked_stickers.delete_one({"_id": set_name}).deleted_count > 0

    def get_blocked_stickers(self):
        return [s["_id"] for s in self.blocked_stickers.find()]

    def add_blocked_word(self, word):
        word = word.lower()
        self.blocked_words.update_one({"_id": word}, {"$set": {"_id": word}}, upsert=True)

    def remove_blocked_word(self, word):
        word = word.lower()
        return self.blocked_words.delete_one({"_id": word}).deleted_count > 0

    def get_blocked_words(self):
        return [w["_id"] for w in self.blocked_words.find()]
        
    def get_config(self, chat_id):
        s = self.group_config.find_one({"_id": chat_id})
        # 'mute_hours' ki jagah hum 'action' return kar rahe hain (Index 2 par)
        return (s.get("delay_minutes", 1), s.get("warn_limit", 3), s.get("action", "mute"), 
                s.get("copyright_enabled", 0), s.get("anti_channel", 1), s.get("nsfw_enabled", 1)) if s else (1, 3, "mute", 0, 1, 1)

    def set_warn_limit(self, chat_id, warn_limit):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"warn_limit": warn_limit}}, upsert=True)

    def set_action(self, chat_id, action):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"action": action}}, upsert=True)
        
    def set_delay(self, chat_id, minutes):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"delay_minutes": minutes}}, upsert=True)

    def set_anti_channel(self, chat_id, enabled):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"anti_channel": 1 if enabled else 0}}, upsert=True)

    def set_nsfw(self, chat_id, enabled):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"nsfw_enabled": 1 if enabled else 0}}, upsert=True)

    def add_user(self, user_id):
        self.users.update_one({"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True)

    def add_group(self, chat_id, title="Unknown Group"):
        self.groups.update_one({"_id": chat_id}, {"$set": {"title": title}}, upsert=True)

    def get_groups(self):
        return [(g["_id"], g.get("title", "Unknown Group")) for g in self.groups.find()]

    def remove_group(self, chat_id):
        self.groups.delete_one({"_id": chat_id})
        self.group_config.delete_one({"_id": chat_id})

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

    def decrease_warning(self, user_id):
        # Ye function warning count ko 1 kam karega
        row = self.warnings.find_one({"_id": user_id})
        if row and row["count"] > 0:
            new_count = row["count"] - 1
            if new_count <= 0:
                self.warnings.delete_one({"_id": user_id})
            else:
                self.warnings.update_one({"_id": user_id}, {"$set": {"count": new_count}})

    def set_edit_guard(self, chat_id, enabled):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"edit_guard": 1 if enabled else 0}}, upsert=True)

    def is_edit_guard_enabled(self, chat_id):
        s = self.group_config.find_one({"_id": chat_id})
        if s and "edit_guard" in s:
            return s["edit_guard"] == 1
        return True # Default is ON
    
    # ==========================================
    # LOCAL BLOCKLIST METHODS
    # ==========================================
    def add_local_word(self, chat_id, word):
        word = word.lower()
        self.local_blocked_words.update_one(
            {"chat_id": chat_id, "word": word}, 
            {"$set": {"chat_id": chat_id, "word": word}}, 
            upsert=True
        )

    def remove_local_word(self, chat_id, word):
        word = word.lower()
        return self.local_blocked_words.delete_one({"chat_id": chat_id, "word": word}).deleted_count > 0

    def get_local_words(self, chat_id):
        return [w["word"] for w in self.local_blocked_words.find({"chat_id": chat_id})]

    def add_local_sticker(self, chat_id, set_name):
        self.local_blocked_stickers.update_one(
            {"chat_id": chat_id, "set_name": set_name}, 
            {"$set": {"chat_id": chat_id, "set_name": set_name}}, 
            upsert=True
        )

    def remove_local_sticker(self, chat_id, set_name):
        return self.local_blocked_stickers.delete_one({"chat_id": chat_id, "set_name": set_name}).deleted_count > 0

    def get_local_stickers(self, chat_id):
        return [s["set_name"] for s in self.local_blocked_stickers.find({"chat_id": chat_id})]

    # ==========================================
    # GBAN METHODS (Isko PersistentDB class ke kisi bhi function ke niche paste karein)
    # ==========================================
    def add_gban(self, user_id, reason="No reason"):
        self.gbans.update_one({"_id": user_id}, {"$set": {"reason": reason}}, upsert=True)

    def remove_gban(self, user_id):
        return self.gbans.delete_one({"_id": user_id}).deleted_count > 0

    def is_gbanned(self, user_id):
        row = self.gbans.find_one({"_id": user_id})
        if row:
            return True, row.get("reason", "No reason")
        return False, ""

    def get_gbans(self):
        return [(u["_id"], u.get("reason", "No reason")) for u in self.gbans.find()]

db = PersistentDB()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== HELPERS & COMMANDS ==========
# [Extract Target, Admin Checks, etc. codes yahan honge jo aapne likhe hain]

# ... [Aapka baki pura code yahan aayega, jaise start_command, message_handler, etc.] ...
# Note: Maine code length ki wajah se yahan functions skip kiye hain, par aapko apne baki commands as it is rakhne hain.

async def process_bulk_delete(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    
    if chat_id in BULK_DELETE_QUEUE and BULK_DELETE_QUEUE[chat_id]:
        # Copy the list of message IDs and clear the original queue
        msg_ids_to_delete = BULK_DELETE_QUEUE[chat_id].copy()
        BULK_DELETE_QUEUE[chat_id].clear()
        
        try:
            # This PTB method deletes up to 100 messages at once!
            await context.bot.delete_messages(chat_id=chat_id, message_ids=msg_ids_to_delete)
        except Exception as e:
            pass # Ignore if messages are already deleted

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
    try: 
        await context.bot.delete_message(chat_id=context.job.chat_id, message_id=context.job.data)
    except Exception as e:
        error_msg = str(e).lower()
        if "can't be deleted" in error_msg or "not enough rights" in error_msg:
            try: await context.bot.send_message(context.job.chat_id, "⚠️ **Please give me delete messages permission.**", parse_mode='Markdown')
            except: pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # 1. DELETE MESSAGES (Har koi use kar sakta hai)
    if "delete_msg" in query.data or "delmsg" in query.data:
        try: 
            await query.message.delete()
        except Exception as e: 
            error_msg = str(e).lower()
            if "can't be deleted" in error_msg or "not enough rights" in error_msg:
                await query.answer("⚠️ Please give me delete messages permission.", show_alert=True)
        return
        
    # 2. HELP MENU (Button Click Logic)
    if query.data == "help_main":
        is_private = update.effective_chat.type == 'private'

        if is_private:
            help_text = (
        "🤖 **BOT COMMANDS MENU**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 **USER COMMANDS**\n"
        "• `/start` : Check bot status\n"
        "• `/status` : Check security stats\n"
        "• `/help` : Show this menu\n\n"
        "🛡️ **ADMIN SECURITY**\n"
        "• `/nsfw on/off` : AI Media Filter\n"
        "• `/antichannel on/off` : Stop channel posts\n"
        "• `/edit on/off` : Toggle edited messages\n" # <--- ADD THIS LINE        
        "• `/config` : Set warn limits & actions\n"
        "• `/delay <min>` : Media auto-delete timer\n\n"
        "🚫 **LOCAL BLOCKLIST (Group Admins)**\n"
        "• `/blockword` : Block a word locally\n"
        "• `/unblockword` : Remove local word block\n"
        "• `/blocksticker` : Block sticker pack locally\n"
        "• `/unblocksticker` : Unblock pack locally\n"
        "• `/listlocal` : View local blocked list\n\n"
        "👥 **USER MANAGEMENT**\n"
        "• `/approve` : Whitelist a user\n"
        "• `/unapprove` : Remove from whitelist\n"
        "• `/aplist` : List whitelist users\n"
    )
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]]
            await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            bot_info = await context.bot.get_me()
            user_name = update.effective_user.first_name
            dm_url = f"https://t.me/{bot_info.username}?start=help"
            group_text = f"💡 **Hey {html.escape(user_name)}!**\n\nPlease click the button below to get the help menu in your DMs.."
            keyboard = [
                [InlineKeyboardButton("💬 Open DM", url=dm_url)],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"), InlineKeyboardButton("🗑 Close", callback_data="delete_msg")]
            ]
            await query.edit_message_text(group_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        try: await query.answer()
        except: pass
        return
        
    elif query.data == "back_to_start":
        await start_command(update, context)
        try: await query.answer()
        except: pass
        return

    # 👇 YAHAN PAR ADD KARNA HAI 👇
    # ==========================================
    # 👑 OWNER & SUDO LOCKED MENU
    # ==========================================
    if query.data == "sudo_menu":
        # Security Check: Agar user Owner ya Sudo nahi hai, toh popup alert dedo
        if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
            await query.answer("❌ ACCESS DENIED!\n\nThis menu is locked. Only the Bot Owner and Sudo Admins can open it.", show_alert=True)
            return
            
        # Agar Owner/Sudo hai, toh commands ki list dikhao
        sudo_text = (
            "👑 **OWNER & SUDO COMMANDS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• `/broadcast <text>` : Message to all active groups\n"
            "• `/grouplist` : List all monitored groups\n"
            "• `/getlink <s_no>` : Get invite link of a group\n"
            "• `/gmsg <s_no> <text>` : Send a direct message\n"
            "• `/greply <s_no> <msg_id> <txt>` : Reply to a group message\n"
            "• `/greact <s_no> <msg_id> <emoji>` : Add reaction to message\n"
            "• `/cleangroups` : Remove dead groups from database\n"
            "• `/nsfw all on/off` : Global NSFW Control\n\n"
            "🛠️ **CUSTOM BLOCKLISTS**\n"
            "• `/addsticker`, `/rmsticker`, `/stickerlist`\n"
            "• `/addword`, `/rmword`, `/wordlist`\n\n"
            "👮‍♂️ **ADMIN MANAGEMENT**\n"
            "• `/addsudo` : Promote user to Sudo\n"
            "• `/rmsudo` : Demote Sudo Admin\n"
            "• `/sudolist` : List all Sudo Admins\n"
            "• `/gban` : Ban globally\n"
            "• `/ungban` : Unban globally\n"
            "• `/gbanlist` : List of gban user\n"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]]
        
        await query.edit_message_text(sudo_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        try: await query.answer()
        except: pass
        return
    # 👆 YAHAN TAK 👆

    
    # ==========================================
    # 🔴 RESTRICTED BUTTONS (ADMINS ONLY)
    # ==========================================
    is_private = update.effective_chat.type == 'private'
    is_admin = is_private or await is_user_admin(update, context)

    if not is_admin:
        await query.answer("❌ You are not an administrator", show_alert=True)
        return

    # --- CONFIGURATION LOGIC (INSTANT TICK) ---
    if query.data.startswith("cfg_") or query.data.startswith("setwarn_"):
        config = db.get_config(chat_id)
        warn_limit, action = config[1], config[2]
        
        # 0. Database se current status check kiya
        edit_guard_enabled = db.is_edit_guard_enabled(chat_id)

        # 1. Action: Warn Limit Change
        if query.data.startswith("setwarn_"):
            limit = int(query.data.split("_")[1])
            if limit == warn_limit:
                return await query.answer("✅ Already selected!", show_alert=False)
            db.set_warn_limit(chat_id, limit)
            warn_limit = limit 
            await query.answer(f"✅ Warning limit changed to {limit}")

        # 2. Action: Mute/Ban Change
        elif query.data in ["cfg_mute", "cfg_ban"]:
            new_action = query.data.split("_")[1]
            if new_action == action:
                return await query.answer("✅ Already selected!", show_alert=False)
            db.set_action(chat_id, new_action)
            action = new_action 
            await query.answer(f"✅ Punishment set to {action.upper()}")

        # 3. Action: Edit Guard Toggle
        elif query.data == "cfg_edit":
            edit_guard_enabled = not edit_guard_enabled # Toggle
            db.set_edit_guard(chat_id, edit_guard_enabled)
            await query.answer(f"✅ Edit Guard turned {'ON' if edit_guard_enabled else 'OFF'}")

        # 4. Menu: Render Warn Limits Page (Dusre page par jana)
        elif query.data == "cfg_warn":
            def get_btn(num):
                txt = f"✅ {num}" if num == warn_limit else str(num)
                return InlineKeyboardButton(txt, callback_data=f"setwarn_{num}")
            
            keyboard = [
                [get_btn(3), get_btn(4), get_btn(5), get_btn(6)],
                [get_btn(7), get_btn(8), get_btn(9), get_btn(10)],
                [InlineKeyboardButton("⬅️ Back", callback_data="cfg_main")]
            ]
            try: 
                await query.edit_message_text("⚠️ **Select Warning Limit:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            except Exception: pass
            return

        # ==========================================
        # 5. UI REFRESHER (Sab kuch yahan update hoga instantly!)
        # ==========================================
        mute_btn = "✅ 🔇 Mute" if action == "mute" else "🔇 Mute"
        ban_btn = "✅ 🚫 Ban" if action == "ban" else "🚫 Ban"
        
        edit_status = "ON ✅" if edit_guard_enabled else "OFF ❌"
        edit_btn = "✅ ✏️ Edit Guard" if edit_guard_enabled else "❌ ✏️ Edit Guard"
        
        text = (
            f"⚙️ **Group Configuration**\n\n"
            f"⚠️ **Limit:** {warn_limit}\n"
            f"🔨 **Action:** {action.upper()}\n"
            f"✏️ **Edit Guard:** {edit_status}"
        )
        
        keyboard = [
            [InlineKeyboardButton(f"⚠️ Warn ({warn_limit})", callback_data="cfg_warn")],
            [InlineKeyboardButton(mute_btn, callback_data="cfg_mute"), InlineKeyboardButton(ban_btn, callback_data="cfg_ban")],
            [InlineKeyboardButton(edit_btn, callback_data="cfg_edit")],
            [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]
        ]
        
        # Pura message naye buttons ke sath recreate kar diya
        try: 
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except Exception: 
            pass 
            
        return

    # --- OTHER ADMIN BUTTONS (Approve, Unban, Unmute) ---
    try: await query.answer() 
    except: pass
    
    if "_" in query.data:
        parts = query.data.split("_")
        action = parts[0]
        
        if len(parts) > 1 and parts[-1].lstrip('-').isdigit():
            target_id = int(parts[-1])

            if action == "approve":
                db.add_to_allowlist(target_id)
                db.reset_warnings(target_id)
                keyboard = [[InlineKeyboardButton("❌ Unapprove", callback_data=f"unapprove_{target_id}"), InlineKeyboardButton("🧹 Cancel warning", callback_data=f"cancle warning_{target_id}")],
                            [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
                await context.bot.send_message(chat_id, f"✅ **Approved:** User `{target_id}` has been whitelisted.", parse_mode='Markdown')

            elif action == "unapprove":
                db.remove_from_allowlist(target_id)
                keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_id}"), InlineKeyboardButton("🧹 Cancel warning", callback_data=f"cancle warning_{target_id}")],
                            [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
                await context.bot.send_message(chat_id, f"❌ **Unapproved:** User `{target_id}` removed from whitelist.", parse_mode='Markdown')

            elif action in ["unwarn", "cancle warning"]:
                db.reset_warnings(target_id)
                await context.bot.send_message(chat_id, f"🧹 **Warnings Cleared:** User `{target_id}` is now warning-free.", parse_mode='Markdown')
            
            elif action == "unban":
                # 1. First, try to unban the user
                try:
                    await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
                    db.reset_warnings(target_id)
                except Exception as e:
                    # If the bot lacks permissions or the unban fails, send the error and stop
                    await context.bot.send_message(chat_id, "❌ Failed to unban. Make sure I am an admin.")
                    return

                # 2. If the unban is successful, update the button message
                try:
                    await query.edit_message_text(f"🔓 User `{target_id}` has been Unbanned. Warnings restarted!", parse_mode='Markdown')
                except Exception:
                    # Ignore minor errors like "Message is not modified" from double clicks
                    pass
                    
            elif action == "unmute":
                # 1. Pehle sirf user ko unmute karne ka try karenge
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
                except Exception as e:
                    # Agar restrict FAILED hua (jaise admin rights nahi hain), tabhi ye message aayega
                    await context.bot.send_message(chat_id, f"❌ **Error:** Could not unmute. Please check my admin permissions.", parse_mode='Markdown')
                    return # Code yahan se ruk jayega

                # 2. Agar unmute SUCCESSFUL ho gaya, tab message ko edit karenge
                try:
                    await query.edit_message_text(text=f"✅ User `{target_id}` has been **Unmuted**.", parse_mode='Markdown')
                    # Note: Maine yahan se ek extra send_message hata diya hai, 
                    # kyunki aapka 'auto_reset_on_unmute' function pehle se hi group mein 
                    # "🔄 User has been unmuted" ka alert bhej raha hai. 
                    # Isse bot spam nahi karega.
                except Exception:
                    # Agar user button par 2 baar click kar de, toh ye minor error ko chup chap ignore karega
                    pass
            
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
    # 1. SAFELY Check if the user clicked the deep link from the group
    # We check 'update.message' first so it doesn't crash when clicked via inline button
    if update.message and context.args and context.args[0] == "help":
        await help_command(update, context)
        return

    bot_user = await context.bot.get_me()
    chat = update.effective_chat
    
    # 2. Database Logging
    if chat.type == 'private':
        db.add_user(update.effective_user.id) 
    else:
        db.add_group(chat.id, chat.title)

    # 3. Universal Message & Keyboard
    CHANNEL_URL = "https://t.me/+rjE5xZlIK4U3ODA1"
    user_name = update.effective_user.first_name
    text = (
        f"🛡️ **𝗪𝗲𝗹𝗰𝗼𝗺𝗲, {html.escape(user_name)}!**\n\n" 
        f"🛡️ **𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 {bot_user.first_name}!**\n\n"
        "𝗜 𝗮𝗺 𝗮𝗻 𝗔𝗱𝘃𝗮𝗻𝗰𝗲𝗱 𝗔𝗜-𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗦𝗲𝗰𝘂𝗿𝗶𝘁𝘆 𝗦𝘆𝘀𝘁𝗲𝗺, "
        "𝗱𝗲𝘀𝗶𝗴𝗻𝗲𝗱 𝘁𝗼 𝗸𝗲𝗲𝗽 𝘆𝗼𝘂𝗿 𝗰𝗵𝗮𝘁𝘀 𝗰𝗹𝗲𝗮𝗻, 𝘀𝗮𝗳𝗲, 𝗮𝗻𝗱 𝗽𝗿𝗼𝗳𝗲𝘀𝘀𝗶𝗼𝗻𝗮𝗹. ⚡\n\n"
        "✨ **𝗞𝗲𝘆 𝗦𝗵𝗶𝗲𝗹𝗱𝘀:**\n"
        "🔞 **𝗔𝗜 𝗡𝗦𝗙𝗪 𝗚𝘂𝗮𝗿𝗱**: Scans & deletes explicit media using AI.\n"
        "🤬 **𝗔𝗯𝘂𝘀𝗲 𝗦𝗵𝗶𝗲𝗹𝗱**: Instantly removes abusive words.\n"
        "🚫 **𝗔𝗻𝘁𝗶-𝗟𝗶𝗻𝗸**: Blocks URLs instantly.\n"
        "🛡️ **𝗕𝗶𝗼 𝗚𝘂𝗮𝗿𝗱**: Scan bios for links and restrict users.\n"
        "🔒 **𝗔𝗻𝘁𝗶-𝗖𝗵𝗮𝗻𝗻𝗲𝗹**: Blocks anonymous channel posts.\n"
        "✏️ **𝗘𝗱𝗶𝘁 𝗚𝘂𝗮𝗿𝗱**: Deletes edited messages to prevent spam.\n"
        "🤖 **𝗔𝗻𝘁𝗶-𝗕𝗼𝘁**: Automatically kicks malicious bots (except admins).\n\n"
        "💡 _Click the buttons below to explore more!_"
    )
    
    keyboard = [
        [InlineKeyboardButton("➕ 𝐀𝐝𝐝 𝐭𝐨 𝐆𝐫𝐨𝐮𝐩", url=f"https://t.me/{bot_user.username}?startgroup=true")],
        [InlineKeyboardButton("𝐇𝐞𝐥𝐩❓", callback_data="help_main"), InlineKeyboardButton("📢 Support Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("👑 Owner & Sudo Menu 🔒", callback_data="sudo_menu")], # <--- NAYA BUTTON
        [InlineKeyboardButton("𝗖𝗹𝗼𝘀𝗲 🗑", callback_data="delete_msg")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 4. Send or Edit the Output
    if update.callback_query:
        # If triggered by "⬅️ Back" button, edit the current message
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        # If triggered by typing /start, send a new message
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **BOT COMMANDS MENU**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 **USER COMMANDS**\n"
        "• `/start` : Check bot status\n"
        "• `/status` : Check security stats\n"
        "• `/help` : Show this menu\n\n"
        "🛡️ **ADMIN SECURITY**\n"
        "• `/nsfw on/off` : AI Media Filter\n"
        "• `/antichannel on/off` : Stop channel posts\n"
        "• `/edit on/off` : Toggle edited messages\n"
        "• `/config` : Set warn limits & actions\n"
        "• `/delay <min>` : Media auto-delete timer\n\n"
        "🚫 **LOCAL BLOCKLIST (Group Admins)**\n"
        "• `/blockword` : Block a word locally\n"
        "• `/unblockword` : Remove local word block\n"
        "• `/blocksticker` : Block sticker pack locally\n"
        "• `/unblocksticker` : Unblock pack locally\n"
        "• `/listlocal` : View local blocked list\n\n"
        "👥 **USER MANAGEMENT**\n"
        "• `/approve` : Whitelist a user\n"
        "• `/unapprove` : Remove from whitelist\n"
        "• `/aplist` : List whitelist users\n"
    )

    chat_type = update.effective_chat.type

    # If the user uses /help directly in the bot's DM
    if chat_type == 'private':
        await update.message.reply_text(help_text, parse_mode='Markdown')
        return

    # If the user uses /help in a group
    bot_info = await context.bot.get_me()
    user_name = update.effective_user.first_name
    dm_url = f"https://t.me/{bot_info.username}?start=help"
    
    group_text = (
        f"💡 **Hey {html.escape(user_name)}!**\n\n"
        "I've sent the **Help Menu** to your DMs to keep this group clean. "
        "Click the button below to see it! 🚀"
    )
    
    keyboard = [
        [InlineKeyboardButton("💬 Open DM", url=dm_url)],
        [InlineKeyboardButton("🗑 Close", callback_data="delete_msg")]
    ]
    
    await update.message.reply_text(group_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # 1. Fetch the bot's name
    bot_info = await context.bot.get_me()
    bot_name = bot_info.first_name

    # 2. Fetch Stats from MongoDB
    stats = db.get_global_stats()
    scanned, bio_caught, media_del, warns_issued, nsfw_blocked, abuse_caught, start_timestamp = stats

    # 3. Monitored Groups calculation
    groups = db.get_groups()
    group_count = len(groups) if groups else 0

    # 4. Permanent Uptime Calculation
    bot_start_time = datetime.fromtimestamp(start_timestamp, IST)
    uptime_delta = datetime.now(IST) - bot_start_time

    total_seconds = int(uptime_delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    uptime_str = f"{hours}h {minutes}m {seconds}s"

    # 🔥 Threat calculation
    total_threats = bio_caught + media_del + warns_issued + nsfw_blocked + abuse_caught

    if scanned == 0:
        threat_percent = 0
    else:
        threat_percent = int((total_threats / scanned) * 100)

    if threat_percent < 10:
        threat_level = "LOW 🟢"
    elif threat_percent < 30:
        threat_level = "MODERATE 🟡"
    else:
        threat_level = "HIGH 🔴"

    # 📊 Progress bar function
    def progress_bar(percent):
        bars = int(percent / 10)
        return "█" * bars + "░" * (10 - bars)

    # 💻 Terminal UI
    text = (
        f"<b>{bot_name}</b>\n"
        "<code>┌──────────────────────────────┐</code>\n"
        "<code>│ MATRIX AI SECURITY TERMINAL │</code>\n"
        "<code>└──────────────────────────────┘</code>\n\n"

        "🟢 <b>STATUS</b> : <code>LIVE PROTECTION</code>\n"
        "<code>system.scan() running...</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "<b>[ THREAT ANALYTICS ]</b>\n\n"

        f"<code>> scanned_total     {progress_bar(100)} {scanned}</code>\n"
        f"<code>> bio_link_caught   {progress_bar(bio_caught)} {bio_caught}</code>\n"
        f"<code>> media_deleted    {progress_bar(media_del)} {media_del}</code>\n"
        f"<code>> warnings_issued  {progress_bar(warns_issued)} {warns_issued}</code>\n"
        f"<code>> nsfw_blocked     {progress_bar(nsfw_blocked)} {nsfw_blocked}</code>\n"
        f"<code>> abuse_caught     {progress_bar(abuse_caught)} {abuse_caught}</code>\n\n"

        "<b>[ NETWORK ]</b>\n"
        f"<code>> monitored_groups : {group_count}</code>\n\n"

        "<b>[ SYSTEM ]</b>\n"
        f"<code>> uptime : {uptime_str}</code>\n\n"

        "<b>[ AI THREAT LEVEL ]</b>\n"
        f"<code>{progress_bar(threat_percent)} {threat_percent}%</code>\n"
        f"<b>{threat_level}</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<code>AI core :: scanning • filtering • neutralizing</code>\n"
    )

    keyboard = [[InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
    
async def set_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context): 
        msg = await update.message.reply_text("❌ You have not permission.")
        # Make sure you have delete_after_delay defined, or remove this task
        # asyncio.create_task(delete_after_delay(msg, 10)) 
        return
        
    chat_id = update.effective_chat.id
    config = db.get_config(chat_id)
    warn_limit = config[1]
    action = config[2]

    # 👇 Fetch Edit Guard status
    edit_guard_enabled = db.is_edit_guard_enabled(chat_id)
    edit_status = "ON ✅" if edit_guard_enabled else "OFF ❌"
    edit_btn = "✅ ✏️ Edit Guard" if edit_guard_enabled else "❌ ✏️ Edit Guard"

    mute_btn = "✅ 🔇 Mute" if action == "mute" else "🔇 Mute"
    ban_btn = "✅ 🚫 Ban" if action == "ban" else "🚫 Ban"

    text = (
        "⚙️ **Group Configuration**\n\n"
        f"⚠️ **Current Warn Limit:** {warn_limit}\n"
        f"🔨 **Current Action:** {action.upper()}\n"
        f"✏️ **Edit Guard:** {edit_status}" # <--- Shows status in text
    )

    keyboard = [
        [InlineKeyboardButton(f"⚠️ Warn ({warn_limit})", callback_data="cfg_warn")],
        [InlineKeyboardButton(mute_btn, callback_data="cfg_mute"), InlineKeyboardButton(ban_btn, callback_data="cfg_ban")],
        [InlineKeyboardButton(edit_btn, callback_data="cfg_edit")], # <--- The new toggle button
        [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    
async def set_delay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin Permission Check
    if not await is_user_admin(update, context): 
        await update.message.reply_text(
            "❌ <b>Permission Denied!</b>\nOnly admins can change the media delay config.", 
            parse_mode='HTML'
        )
        return

    chat_id = update.effective_chat.id

    if context.args:
        try:
            mins = int(context.args[0])
            if mins < 0:
                raise ValueError("Negative time not allowed")
                
            # Update Database
            db.set_delay(chat_id, mins)
            
            # Attractive Success Message
            success_text = (
                f"✅ <b>MEDIA CLEANER UPDATED</b>\n\n"
                f"⏱️ <b>New Delay:</b> <code>{mins} Minutes</code>\n\n"
                f"🗑️ <i>Now deleted all new media will be automatically</i>"
            )
            sent_msg = await update.message.reply_text(success_text, parse_mode='HTML')
            
        except ValueError:
            # Attractive Error/Usage Message
            error_text = (
                f"❗ <b>Invalid Format!</b> Please use numbers only.\n\n"
                f"💡 <b>Usage:</b>\n<code>/delay <minutes></code>\n"
                f"<i>Example: <code>/delay 5</code> (Auto-deletes media after 5 mins)</i>"
            )
            sent_msg = await update.message.reply_text(error_text, parse_mode='HTML')
    else:
        # Attractive Current Status Message (When no arguments are passed)
        current_delay = db.get_config(chat_id)[0]
        status_text = (
            f"⏱️ <b>Current Media Delay:</b> <code>{current_delay} Minutes</code>\n\n"
            f"<i>Media files are currently being auto-deleted after {current_delay} minutes.</i>\n\n"
            f"💡 <b>To change this, use:</b>\n<code>/delay <minutes></code>"
        )
        sent_msg = await update.message.reply_text(status_text, parse_mode='HTML')

    # Auto-delete the bot's response after 30 seconds to keep the group clean
    context.job_queue.run_once(delete_msg_job, 30, chat_id=chat_id, data=sent_msg.message_id)

async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Admin verification
    if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id):
        msg = await update.message.reply_text("❌ You do not have permission to use this command.")
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        return

    args = context.args
    if not args or args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("❗ **Usage:** `/edit on` or `/edit off`", parse_mode='Markdown')
        return

    state_str = args[0].lower()
    state = (state_str == "on")
    
    # Update database
    db.set_edit_guard(chat_id, state)
    
    status = "ENABLED" if state else "DISABLED"
    await update.message.reply_text(
        f"✏️ **Edit Guard** is now **{status}** in this group.\n\n"
        f"_{'Edited messages will now be deleted.' if state else 'Edited messages will NO LONGER be deleted.'}_", 
        parse_mode='Markdown'
    )

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

# ==========================================
# CUSTOM BLOCKLIST COMMANDS (SUDO/OWNER)
# ==========================================
async def addsticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id): return
    
    set_name = None
    if context.args:
        set_name = context.args[0]
    elif update.message.reply_to_message and update.message.reply_to_message.sticker:
        set_name = update.message.reply_to_message.sticker.set_name
        
    if not set_name:
        await update.message.reply_text("❗ **Usage:** Reply to a sticker, or type `/addsticker <pack_name>`", parse_mode='Markdown')
        return
        
    db.add_blocked_sticker(set_name)
    await update.message.reply_text(f"✅ Sticker pack `{set_name}` blocked globally!", parse_mode='Markdown')

async def rmsticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id): return
    
    set_name = None
    if context.args:
        set_name = context.args[0]
    elif update.message.reply_to_message and update.message.reply_to_message.sticker:
        set_name = update.message.reply_to_message.sticker.set_name
        
    if not set_name:
        await update.message.reply_text("❗ **Usage:** Reply to a sticker, or type `/rmsticker <pack_name>`", parse_mode='Markdown')
        return
        
    if db.remove_blocked_sticker(set_name):
        await update.message.reply_text(f"✅ Sticker pack `{set_name}` unblocked.", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Pack not found in blocklist.")

async def stickerlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id): return
    packs = db.get_blocked_stickers()
    if not packs:
        await update.message.reply_text("📭 Blocked sticker list is empty.")
        return
    text = "🚫 **Blocked Sticker Packs:**\n\n" + "\n".join([f"• `{p}`" for p in packs])
    await update.message.reply_text(text, parse_mode='Markdown')

async def addword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id): return
    
    word = ""
    if context.args:
        word = " ".join(context.args).lower()
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        word = update.message.reply_to_message.text.strip().lower()
        
    if not word:
        await update.message.reply_text("❗ **Usage:** Reply to a text message, or type `/addword <word>`", parse_mode='Markdown')
        return
        
    db.add_blocked_word(word)
    await update.message.reply_text(f"✅ Word/Text `{word}` blocked globally!", parse_mode='Markdown')

async def rmword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id): return
    
    word = ""
    if context.args:
        word = " ".join(context.args).lower()
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        word = update.message.reply_to_message.text.strip().lower()
        
    if not word:
        await update.message.reply_text("❗ **Usage:** Reply to a text message, or type `/rmword <word>`", parse_mode='Markdown')
        return
        
    if db.remove_blocked_word(word):
        await update.message.reply_text(f"✅ Word/Text `{word}` unblocked.", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Word not found in blocklist.")

async def wordlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id): return
    words = db.get_blocked_words()
    if not words:
        await update.message.reply_text("📭 Blocked word list is empty.")
        return
    text = "🚫 **Blocked Words:**\n\n" + "\n".join([f"• `{w}`" for w in words])
    await update.message.reply_text(text, parse_mode='Markdown')
    
 # ==========================================
# GBAN COMMANDS (OWNER & SUDO ONLY)
# ==========================================
async def gban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
        await update.message.reply_text("❌ Only Owner and Sudo Admins can use this command.")
        return

    target_id, target_name, reason = await extract_target(update, context)
    if not target_id:
        await update.message.reply_text(reason) 
        return

    # Owner ya Sudo ko gban karne se rokna
    if target_id in ADMIN_IDS or db.is_sudo(target_id):
        await update.message.reply_text("❌ You cannot GBan an Admin or Sudo user.")
        return

    db.add_gban(target_id, reason)
    
    # Text ko safe banane ke liye html.escape ka use kiya
    safe_reason = html.escape(reason)
    safe_name = html.escape(target_name or str(target_id))
    
    # 👇 User ko turant DM bhejna (HTML mode ke sath) 👇
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🚨 <b>GLOBAL BAN NOTICE</b> 🚨\n\nYou have been Globally Banned from all groups managed by this bot.\n\n📝 <b>Reason:</b> {safe_reason}\n\n<i>Contact the bot owner (@anurag_9X) if you think this is a mistake.</i>",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"GBAN DM Error: {e}") # Agar DM fail hua toh Render logs me dikh jayega
    # 👆 DM END 👆
    
    # Current group se turant ban karne ki koshish
    if update.effective_chat.type in ['group', 'supergroup']:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target_id)
        except:
            pass

    # Group me confirmation message (Ye bhi HTML mode me)
    await update.message.reply_text(
        f"🌍 <b>GBANNED SUCCESSFULLY!</b>\n\n👤 <b>User:</b> {safe_name} (<code>{target_id}</code>)\n📝 <b>Reason:</b> {safe_reason}\n\n<i>This user will now be banned from all groups where I am admin.</i>", 
        parse_mode='HTML'
    )
    
async def ungban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
        await update.message.reply_text("❌ Only Owner and Sudo Admins can use this command.")
        return

    target_id, target_name, reason = await extract_target(update, context)
    if not target_id:
        await update.message.reply_text(reason)
        return

    if db.remove_gban(target_id):
        # 👇 NAYI LINE: User ko turant DM me Unban ka message bhejna 👇
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="✅ **UNBAN NOTICE** ✅\n\nYour Global Ban has been lifted! You can now join the groups again.",
                parse_mode='Markdown'
            )
        except Exception:
            pass # Ignore karega agar DM nahi ja sakta
        # 👆 NAYI LINE END 👆
        
        safe_name = html.escape(target_name or str(target_id))
        await update.message.reply_text(f"✅ **UN-GBANNED!**\n\n👤 **User:** {safe_name} (`{target_id}`) has been removed from the Global Ban list.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ User `{target_id}` is not globally banned.", parse_mode='Markdown')
        
async def gbanlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
        await update.message.reply_text("❌ Only Owner and Sudo Admins can use this command.")
        return

    gbans = db.get_gbans()
    if not gbans:
        await update.message.reply_text("📭 GBan list is empty.")
        return

    text = "🌍 **Globally Banned Users:**\n\n"
    for idx, (uid, reason) in enumerate(gbans, 1):
        text += f"{idx}. `{uid}` - {reason}\n"
    
    # Agar list bahut lambi ho jaye toh limit lagane ke liye
    if len(text) > 4000:
        text = text[:4000] + "\n... (List too long, truncated)"

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
    """Sightengine API with Image Format Fix & Multiple Keys Fallback"""
    
    # 🛠️ FIX 1: Format Mismatch Fix (Convert any WebP/Sticker to strict JPEG)
    try:
        from PIL import Image
        img = Image.open(file_path)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(file_path, 'JPEG') # Ab ye 100% proper JPG ban gaya
    except Exception as e:
        logger.warning(f"Image conversion skipped/failed (Might be video/animated): {e}")

    for creds in SIGHTENGINE_KEYS:
        api_user = creds.get("user")
        api_secret = creds.get("secret")

        # Skip if environment variables are not set properly
        if not api_user or not api_secret:
            continue 

        try:
            def call_api():
                with open(file_path, 'rb') as f:
                    files = {'media': f}
                    params = {
                        'models': 'nudity-2.0',
                        'api_user': api_user,
                        'api_secret': api_secret
                    }
                    # Sightengine API Endpoint
                    return requests.post('https://api.sightengine.com/1.0/check.json', files=files, data=params, timeout=20)

            # Call API in a background thread to prevent bot blocking
            response = await asyncio.to_thread(call_api)
            result = response.json()

            if result.get('status') == 'success':
                nudity = result.get('nudity', {})
                
                # If any of these parameters cross 50% (0.5), it marks it as NSFW
                if (nudity.get('sexual_activity', 0) > 0.45 or 
                    nudity.get('sexual_display', 0) > 0.45 or 
                    nudity.get('erotica', 0) > 0.45):
                    return True
                
                return False # Image is clean
            
            # If the current API key is out of credits/limits
            elif result.get('error', {}).get('type') == 'limit_reached':
                logger.warning(f"Sightengine key {api_user} limit reached. Trying next key...")
                continue # Moves to the next key in the list
                
            else:
                logger.error(f"Sightengine API error: {result}")
                continue # Try the next key on other errors

        except Exception as e:
            logger.error(f"Sightengine exception with key {api_user}: {e}")
            continue # If network fails, try the next key

    logger.error("All Sightengine API keys failed or are out of limits.")
    return False # Fallback to False if all keys are dead
    
    
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

# ==========================================
# LOCAL BLOCKLIST COMMANDS (GROUP ADMINS)
# ==========================================

async def blockword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # 👇 Admin ki command ko 5 second baad delete karne ka timer
    if update.message:
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)

    if update.effective_chat.type == 'private':
        msg = await update.message.reply_text("❌ Ye command sirf groups mein kaam aati hai.")
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        return
        
    if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id):
        msg = await update.message.reply_text("❌ Aapke paas ye use karne ki permission nahi hai.")
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        return
        
    word = ""
    is_reply = False
    if context.args:
        word = " ".join(context.args).lower()
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        word = update.message.reply_to_message.text.strip().lower()
        is_reply = True # 👈 Pata lagaya ki ye reply hai
        
    if not word:
        msg = await update.message.reply_text("❗ **Usage:** Kisi message par reply karein, ya type karein `/blockword <word>`", parse_mode='Markdown')
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        return
        
    db.add_local_word(chat_id, word)
    msg = await update.message.reply_text(f"✅ Word `{word}` ab **sirf is group ke liye** block ho gaya hai.", parse_mode='Markdown')
    context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)

    # 👇 NAYI LINE: Jis kharab word wale message par reply kiya tha, usko turant delete karna
    if is_reply:
        try:
            await update.message.reply_to_message.delete()
        except Exception:
            pass

async def blocksticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # 👇 Admin ki command ko 5 second baad delete karne ka timer
    if update.message:
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)

    if update.effective_chat.type == 'private': return
    if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id): return
        
    set_name = None
    is_reply = False
    if context.args:
        set_name = context.args[0]
    elif update.message.reply_to_message and update.message.reply_to_message.sticker:
        set_name = update.message.reply_to_message.sticker.set_name
        is_reply = True # 👈 Pata lagaya ki ye reply hai
        
    if not set_name:
        msg = await update.message.reply_text("❗ **Usage:** Kisi sticker par reply karein, ya type karein `/blocksticker <pack_name>`", parse_mode='Markdown')
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        return
        
    db.add_local_sticker(chat_id, set_name)
    msg = await update.message.reply_text(f"✅ Sticker pack `{set_name}` blocked in the group.", parse_mode='Markdown')
    context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)

async def unblockword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if update.message:
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)

    if update.effective_chat.type == 'private': return
    if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id): return
        
    word = ""
    if context.args:
        word = " ".join(context.args).lower()
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        word = update.message.reply_to_message.text.strip().lower()
        
    if not word:
        msg = await update.message.reply_text("❗ **Usage:** Kisi message par reply karein, ya type karein `/unblockword <word>`", parse_mode='Markdown')
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        return
        
    if db.remove_local_word(chat_id, word):
        msg = await update.message.reply_text(f"✅ Word `{word}` allowed in the group.", parse_mode='Markdown')
    else:
        msg = await update.message.reply_text("❌ Ye word yahan ki blocklist mein nahi mila.")
    context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
    
async def unblocksticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if update.message:
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)

    if update.effective_chat.type == 'private': return
    if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id): return
        
    set_name = None
    if context.args:
        set_name = context.args[0]
    elif update.message.reply_to_message and update.message.reply_to_message.sticker:
        set_name = update.message.reply_to_message.sticker.set_name
        
    if not set_name:
        msg = await update.message.reply_text("❗ **Usage:** Kisi sticker par reply karein, ya type karein `/unblocksticker <pack_name>`", parse_mode='Markdown')
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        return
        
    if db.remove_local_sticker(chat_id, set_name):
        msg = await update.message.reply_text(f"✅ Sticker pack `{set_name}` allowed in the group.", parse_mode='Markdown')
    else:
        msg = await update.message.reply_text("❌ This Sticker pack is not in list .")
    context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)

async def listlocal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if update.message:
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)

    if update.effective_chat.type == 'private': return
    if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id): return
        
    words = db.get_local_words(chat_id)
    stickers = db.get_local_stickers(chat_id)
    
    if not words and not stickers:
        msg = await update.message.reply_text("📭 Is group ki custom blocklist ekdum khali hai.")
        context.job_queue.run_once(delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        return
        
    text = f"⚙️ **Local Blocklist for {update.effective_chat.title}**\n\n"
    if words:
        text += "🚫 **Blocked Words:**\n" + "\n".join([f"• `{w}`" for w in words]) + "\n\n"
    if stickers:
        text += "🚫 **Blocked Stickers:**\n" + "\n".join([f"• `{s}`" for s in stickers])
        
    msg = await update.message.reply_text(text, parse_mode='Markdown')
    context.job_queue.run_once(delete_msg_job, 15, chat_id=chat_id, data=msg.message_id)

# ========== HANDLERS ==========
async def edited_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    if not msg or not msg.from_user: return

    # 👇 ADD THIS CHECK BEFORE DOING ANYTHING ELSE 👇
    if not db.is_edit_guard_enabled(msg.chat_id):
        return # If Edit Guard is OFF, do nothing and let the user edit!
    # 👆 ADD THIS CHECK 👆

    # 👇 NEW ADMIN CHECK: Do nothing if the user is an Admin, Owner, or Sudo 👇
    if await is_user_admin(update, context) or db.is_sudo(msg.from_user.id):
        return 
    # 👆 NEW ADMIN CHECK 👆
    
    # 1. Message ko turant delete karna
    try:
        await msg.delete()
    except Exception as e:
        error_msg = str(e).lower()
        if "can't be deleted" in error_msg or "not enough rights" in error_msg:
            try:
                await context.bot.send_message(msg.chat_id, "⚠️ **Please give me delete messages permission.**", parse_mode='Markdown')
            except:
                pass
                
    # 2. Edit kiye hue message ka text nikalna
    edited_text = msg.text or msg.caption or "Media/Unsupported Content"
    safe_text = html.escape(edited_text)
    
    # ... (iske niche ka baaki pura code bilkul same rahega)
    
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

    # 👇 GBAN AUTO-KICK CHECK 👇
    is_gbanned, gban_reason = db.is_gbanned(user.id)
    if is_gbanned:
        try:
            await update.message.delete() # Unka message uda do
            await context.bot.ban_chat_member(chat_id, user.id) # Unko group se ban kar do
        except:
            pass
        return # Code aage run hone se rok do
    # 👆 GBAN CHECK END 👆
    
    # UPDATE SCANNED STAT
    db.update_stat('scanned')

    # 1. Identify Channel Posts (BULLETPROOF VERSION)
    is_channel_post = False
    
    # Case A: Forwarded from a channel (Naye aur Purane dono PTB versions ke liye)
    if getattr(update.message, 'forward_origin', None):
        if update.message.forward_origin.type == 'channel':
            is_channel_post = True
    elif getattr(update.message, 'forward_from_chat', None):  
        if update.message.forward_from_chat.type == 'channel':
            is_channel_post = True

    # Case B: Send as Channel (Jab log Group mein Channel ban kar chat karte hain)
    if getattr(update.message, 'sender_chat', None) and update.message.sender_chat.type == 'channel':
        if not getattr(update.message, 'is_automatic_forward', False):
            is_channel_post = True

    # Get config
    config = db.get_config(chat_id)
    anti_channel_enabled = config[4] if len(config) > 4 else 1

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
            # Agar log 'Send As Channel' use karte hain, toh unhe delete karo (Even Admins!)
            # Par agar admin normal forward kar raha hai, toh allow karo (is_exempt kaam aayega)
            is_anonymous_channel_chat = getattr(update.message, 'sender_chat', None) is not None
            
            if not is_exempt or is_anonymous_channel_chat:
                try:
                    await update.message.delete()
                    return # Stop execution here
                except Exception as e: 
                    error_msg = str(e).lower()
                    if "can't be deleted" in error_msg or "not enough rights" in error_msg:
                        try:
                            await context.bot.send_message(chat_id, "⚠️ **Please give me delete messages permission.**", parse_mode='Markdown')
                        except: pass
                    return # Stop execution even if delete fails
        else:
            # 💡 MAIN FIX: If OFF, bypass all other strict filters (Link/Virus) for this channel post!
            is_exempt = True
            

    # 2. Private / Group Logic
    if update.effective_chat.type == 'private':
        if user: db.add_user(user.id)
        return

    # ---> IGNORE JOIN/LEFT MESSAGES <---
    if update.message.new_chat_members or update.message.left_chat_member:
        return
    
    db.add_group(chat_id, update.effective_chat.title)
    # 'mute_hrs' ki jagah ab 'action' fetch kar rahe hain
    delay_min, warn_limit, action, _, anti_ch, nsfw_enabled = db.get_config(chat_id)
    
    # Media Logic (Applies to everyone)
    is_media = any([update.message.photo, update.message.video, update.message.document, 
                    update.message.animation, update.message.voice, update.message.sticker])
    if is_media:
        db.update_stat('media_deleted')
        context.job_queue.run_once(delete_msg_job, delay_min * 60, chat_id=chat_id, data=update.message.message_id)

    if not user: return 
    msg_text = update.message.text or update.message.caption
    
    # ===================================================================
    # CUSTOM BLOCKLISTS (Words & Sticker Packs) -> Runs only if NSFW is ON
    # ===================================================================
    if nsfw_enabled:
        blocked_word_found = False
        blocked_sticker_found = False
        caught_word = ""
        
        # 1. Check Custom Words (Global + Local combined)
        if msg_text:
            all_blocked_words = db.get_blocked_words() + db.get_local_words(chat_id)
            for word in all_blocked_words:
                if re.search(r'\b' + re.escape(word) + r'\b', msg_text.lower()):
                    blocked_word_found = True
                    caught_word = word  
                    break
                    
        # 2. Check Custom Sticker Packs (Global + Local combined)
        if not blocked_word_found and update.message.sticker and update.message.sticker.set_name:
            all_blocked_stickers = db.get_blocked_stickers() + db.get_local_stickers(chat_id)
            if update.message.sticker.set_name in all_blocked_stickers:
                blocked_sticker_found = True
                
        # 🟢 CASE A: AGAR BLOCKED WORD MILA (Abuse)
        if blocked_word_found:
            db.update_stat('abuse_caught')
            
            # Add message to the bulk delete queue
            BULK_DELETE_QUEUE[chat_id].append(update.message.message_id)

            # Trigger the bulk delete job (runs after 1.5 seconds to catch all spam)
            job_name = f"bulk_del_{chat_id}"
            if not context.job_queue.get_jobs_by_name(job_name):
                context.job_queue.run_once(process_bulk_delete, 1.5, chat_id=chat_id, name=job_name)

            # Anti-Spam Alert Cooldown: Only send 1 warning every 10 seconds per user
            current_time = time.time()
            if current_time - context.chat_data.get(f"last_word_alert_{user.id}", 0) > 10:
                context.chat_data[f"last_word_alert_{user.id}"] = current_time
                try:
                    alert_msg = await context.bot.send_message(
                        chat_id=chat_id, 
                        text=f"🚫 {user.mention_html()}, you cannot use the blocked word: <b>{html.escape(caught_word)}</b>", 
                        parse_mode='HTML'
                    )
                    context.job_queue.run_once(delete_msg_job, 3, chat_id=chat_id, data=alert_msg.message_id)
                except Exception: pass
            
            return # Yahan code ruk jayega

        # 🔴 CASE B: AGAR BLOCKED STICKER MILA
        elif blocked_sticker_found:
            db.update_stat('nsfw_blocked')
            
            # Add sticker to the bulk delete queue
            BULK_DELETE_QUEUE[chat_id].append(update.message.message_id)

            # Trigger the bulk delete job
            job_name = f"bulk_del_{chat_id}"
            if not context.job_queue.get_jobs_by_name(job_name):
                context.job_queue.run_once(process_bulk_delete, 1.5, chat_id=chat_id, name=job_name)

            # Anti-Spam Alert Cooldown: Only send 1 admin alert every 10 seconds per user
            current_time = time.time()
            if current_time - context.chat_data.get(f"last_sticker_alert_{user.id}", 0) > 10:
                context.chat_data[f"last_sticker_alert_{user.id}"] = current_time
                
                admin_tags = "".join([f'<a href="tg://user?id={aid}">&#8203;</a>' for aid in ADMIN_IDS])
                admin_alert = (
                    f"🚨 <b>Blocked Sticker Detected & Deleted</b>\n\n"
                    f"👤 <b>Sender:</b> {user.mention_html()}\n"
                    f"{admin_tags}" 
                )
                try:
                    alert_msg = await context.bot.send_message(chat_id=chat_id, text=admin_alert, parse_mode='HTML', disable_notification=True)
                    context.job_queue.run_once(delete_msg_job, 30, chat_id=chat_id, data=alert_msg.message_id)
                except Exception: pass
            
            return # Yahan code ruk jayega
            
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
                # 👇 NSFW counter ko badhane ke liye
                db.update_stat('nsfw_blocked')

                # 1. Add image to the bulk delete queue
                BULK_DELETE_QUEUE[chat_id].append(update.message.message_id)

                # Trigger the bulk delete job (runs after 1.5 seconds)
                job_name = f"bulk_del_{chat_id}"
                if not context.job_queue.get_jobs_by_name(job_name):
                    context.job_queue.run_once(process_bulk_delete, 1.5, chat_id=chat_id, name=job_name)

                # 2. Anti-Spam Alert Cooldown: Only send 1 admin alert every 10 seconds per user
                current_time = time.time()
                if current_time - context.chat_data.get(f"last_nsfw_alert_{user.id}", 0) > 10:
                    context.chat_data[f"last_nsfw_alert_{user.id}"] = current_time
                    
                    # 3. Silently Tag Admins in the Group
                    admin_tags = "".join([f'<a href="tg://user?id={aid}">&#8203;</a>' for aid in ADMIN_IDS])
                    
                    admin_alert = (
                        f"🚨 <b>NSFW Content Detected Please Take Action</b>\n\n"
                        f"👤 <b>Sender:</b> {user.mention_html()}"
                        f"{admin_tags}"
                    )
                    
                    try:
                        nsfw_alert_msg = await context.bot.send_message(
                            chat_id=chat_id, 
                            text=admin_alert, 
                            parse_mode='HTML', 
                            disable_notification=True 
                        )
                        # 👇 NSFW wale admin alert ko bhi 30 second baad delete karne ka timer
                        context.job_queue.run_once(delete_msg_job, 30, chat_id=chat_id, data=nsfw_alert_msg.message_id)
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

# ===================================================================
        # PUNISHMENT LOGIC (PERFECTLY SPLIT CASES & ERROR FIXES)
        # ===================================================================
        if violation:
            db.update_stat('warnings_issued')
            try: 
                await update.message.delete()
            except Exception as e:
                error_msg = str(e).lower()
                if "can't be deleted" in error_msg or "not enough rights" in error_msg:
                    try:
                        await context.bot.send_message(chat_id, "⚠️ **Please give me delete messages permission.**", parse_mode='Markdown')
                    except:
                        pass
            
            count = db.add_warning(user.id)
            warn_limit, action = config[1], config[2]
            safe_name = html.escape(user.full_name)
            
            # ... (iske niche ka baaki pura code bilkul waisa hi rahega)

            # CASE 1: LIMIT CROSS HO CHUKI HAI (User already Muted/Banned hai aur Spam kar raha hai)
            if count > warn_limit:
                if action == "mute":
                    msg = await context.bot.send_message(chat_id, f"🚫 <b>User {safe_name} is already muted.</b>", parse_mode='HTML')
                    asyncio.create_task(delete_after_delay(msg, 30))
                elif action == "ban":
                    msg = await context.bot.send_message(chat_id, f"🚫 <b>User {safe_name} is already banned.</b>", parse_mode='HTML')
                    asyncio.create_task(delete_after_delay(msg, 30))
                return

            # CASE 2: EXACT LIMIT PAR PAHUH GAYA (Pehli baar Mute/Ban karna hai)
            elif count == warn_limit:
                if action == "mute":
                    try:
                        # Attempt to mute
                        await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False))
                        txt = f"🚫 <b>User is muted indefinitely</b>\n👤 <b>Name:</b> {safe_name}\n🆔 <b>ID:</b> <code>{user.id}</code>\n📝 <b>Reason:</b> {reason}"
                        kb = [[InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute_{user.id}")], [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
                        await context.bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                    except Exception:
                        # Agar mute FAILED ho gaya (No permission)
                        await context.bot.send_message(chat_id, "🚨 <b>MUTE FAILED:</b> I need admin rights to restrict users.", parse_mode='HTML')
                        # Warning count ko 1 kam karna (NAYA FUNCTION USE KIYA HAI)
                        db.decrease_warning(user.id)
                
                elif action == "ban":
                    try:
                        # Attempt to ban
                        await context.bot.ban_chat_member(chat_id, user.id)
                        txt = f"🚫 <b>User has been BANNED</b>\n👤 <b>Name:</b> {safe_name}\n🆔 <b>ID:</b> <code>{user.id}</code>\n📝 <b>Reason:</b> {reason}"
                        kb = [[InlineKeyboardButton("🔓 Unban", callback_data=f"unban_{user.id}"), InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
                        await context.bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                    except Exception:
                        # Agar ban FAILED ho gaya
                        await context.bot.send_message(chat_id, "🚨 <b>BAN FAILED:</b> I need admin rights to ban users.", parse_mode='HTML')
                        # Warning count ko 1 kam karna (NAYA FUNCTION USE KIYA HAI)
                        db.decrease_warning(user.id)
                return

            # CASE 3: NORMAL WARNINGS (Limit se kam hai)
            else:
                base_info_text = (
                    f"👤 <b>User:</b> {user.mention_html()}\n"
                    f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
                    f"🚫 <b>Reason:</b> {reason}\n"
                    f"⚠️ <b>Warnings:</b> {count}/{warn_limit}" 
                )   
                notice_text = (
                    "\n\n🛑 NOTICE: PLEASE REMOVE ANY LINKS FROM YOUR BIO IMMEDIATELY.\n\n"
                    "📌 REPEATED VIOLATIONS WILL LEAD TO MUTE/BAN."
                )
                is_app = db.is_allowed(user.id)
                app_btn = InlineKeyboardButton("❌ Unapprove", callback_data=f"unapprove_{user.id}") if is_app else InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}")
                keyboard = [[app_btn, InlineKeyboardButton("🧹 Cancel warning", callback_data=f"cancle warning_{user.id}")],
                            [InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
                await context.bot.send_message(chat_id, f"⚠️ **MESSAGE REMOVED**\n\n{base_info_text}{notice_text}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
                
# ========== ANTI-BOT & GBAN JOIN SYSTEM ==========
async def anti_bot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members: return
    
    chat_id = update.effective_chat.id
    adder = update.message.from_user
    
    # Bypass check for Admins and Approved users (For Anti-Bot)
    is_adder_admin = False
    if adder.id in ADMIN_IDS:
        is_adder_admin = True
    else:
        try:
            mem = await context.bot.get_chat_member(chat_id, adder.id)
            if mem.status in ['administrator', 'creator']:
                is_adder_admin = True
        except: pass
        
    is_adder_exempt = is_adder_admin or db.is_allowed(adder.id)
        
    # 👇 EK HI LOOP MEIN DONO CHECKS HONGE 👇
    for new_member in update.message.new_chat_members:
        
        # 1. NAYA GBAN JOIN CHECK (Priority par chalega)
        is_gbanned, _ = db.is_gbanned(new_member.id)
        if is_gbanned:
            try:
                # Pehle user ko ban karo
                await context.bot.ban_chat_member(chat_id, new_member.id)
                
                # Group mein message bhejo
                alert_msg = await context.bot.send_message(
                    chat_id, 
                    f"🚨 {new_member.mention_html()} was globally banned and has been removed.\n\n"
                    "**Reason:** you are global ban contact bot owner for free (@anurag_9X)",
                    parse_mode='HTML'
                )
                # Group clean rakhne ke liye ye alert 10 sec baad delete
                context.job_queue.run_once(delete_msg_job, 10, chat_id=chat_id, data=alert_msg.message_id)

                # User ko DMs mein private message bhejo
                try:
                    await context.bot.send_message(
                        new_member.id,
                        "you are global ban contact bot owner for free"
                    )
                except Exception:
                    pass # Agar block kiya hoga toh ignore karega
            except Exception: 
                pass
            continue # Agar ye GBanned hai, toh agli bot checking mat karo, sidha next member par jao

        # 2. ANTI-BOT CHECK (Agar join karne wala GBanned nahi hai, tab ye check hoga)
        # Ye tabhi check hoga jab add karne wala admin ya approved nahi hai
        if not is_adder_exempt:
            if new_member.is_bot and new_member.id != context.bot.id:
                try:
                    # KICK the bot instantly (ban followed by immediate unban)
                    await context.bot.ban_chat_member(chat_id, new_member.id)
                    await context.bot.unban_chat_member(chat_id, new_member.id)
                    
                    # Send the exact warning notification requested for the user
                    alert_text = f"{adder.mention_html()} you cannot add bots in the group otherwise you restricted from this chat ."
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Delete Message", callback_data="delete_msg")]])
                    await context.bot.send_message(chat_id, alert_text, parse_mode='HTML', reply_markup=kb)
                    
                except Exception as e:
                    # When the bot lacks ban permission
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

    # 👇 ADD THIS LINE to keep the admin cache perfectly updated
    context.chat_data['is_bot_admin'] = (new_status == ChatMemberStatus.ADMINISTRATOR)

    if new_status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.BANNED]:
        # Agar bot ko group se nikala gaya
        db.remove_group(chat.id)
        logger.info(f"Bot removed from group: {chat.title} ({chat.id})")
    elif new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]:
        # Agar bot naye group me add hua
        db.add_group(chat.id, chat.title)
        logger.info(f"Bot added to group: {chat.title} ({chat.id})")
    
# ========== ADMIN CHECK MIDDLEWARE ==========
async def enforce_bot_admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # 1. Apply rule ONLY for groups and supergroups (DMs work normally)
        if not update.effective_chat or update.effective_chat.type not in ['group', 'supergroup']:
            return

        # 2. ALWAYS process 'my_chat_member' updates so the bot knows when it is promoted/demoted
        if update.my_chat_member:
            return

        chat_id = update.effective_chat.id

        # 3. Check cached admin status to avoid Telegram API Rate Limits
        is_admin = context.chat_data.get('is_bot_admin')

        # 4. If cache is empty (e.g., bot just restarted), do one API call and save it
        if is_admin is None:
            try:
                bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
                is_admin = bot_member.status in ['administrator', 'creator']
                context.chat_data['is_bot_admin'] = is_admin
            except Exception:
                is_admin = False  # Assume not admin if there's an API error

        # 5. If bot is an admin, exit middleware and allow normal processing
        if is_admin:
            return

        # 6. ❌ Bot is NOT an admin. Block ALL commands, messages, and buttons silently.
        raise ApplicationHandlerStop()

    except ApplicationHandlerStop:
        raise  # This tells the Python-Telegram-Bot application to halt the update completely
    except Exception as e:
        print(f"Admin Check Error: {e}")
        # If any unexpected error occurs, stay silent to prevent spam
        raise ApplicationHandlerStop()
    
# ========== MAIN EXECUTION ==========
def main():
    # Application builder
    app_bot = Application.builder().token(TOKEN).connect_timeout(60).read_timeout(60).write_timeout(60).pool_timeout(60).build()

    # 👇 ADD THIS LINE RIGHT HERE (group=-1 makes it run before everything else)
    app_bot.add_handler(TypeHandler(Update, enforce_bot_admin_status), group=-1)
    
    # ✅ FIX: All handlers now use app_bot instead of app
    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(CommandHandler("help", help_command))
    app_bot.add_handler(CommandHandler("broadcast", broadcast_command))
    app_bot.add_handler(CommandHandler("delay", set_delay_command))
    app_bot.add_handler(CommandHandler("config", set_config_command))
    app_bot.add_handler(CommandHandler("status", status_command))
    app_bot.add_handler(CommandHandler("grouplist", grouplist_command))
    app_bot.add_handler(CommandHandler("aplist", aplist_command))
    app_bot.add_handler(CommandHandler("getlink", getlink_command))
    app_bot.add_handler(CommandHandler("gmsg", gmsg_command))
    app_bot.add_handler(CommandHandler("approve", approve_command))
    app_bot.add_handler(CommandHandler("unapprove", unapprove_command))
    app_bot.add_handler(CommandHandler("antichannel", antichannel_command))
    app_bot.add_handler(CommandHandler("edit", edit_command))
    app_bot.add_handler(CommandHandler("cleangroups", cleangroups_command))
    app_bot.add_handler(CommandHandler("nsfw", nsfw_command))
    app_bot.add_handler(CommandHandler("addsudo", addsudo_command))
    app_bot.add_handler(CommandHandler("rmsudo", rmsudo_command))
    app_bot.add_handler(CommandHandler("sudolist", sudolist_command))
    app_bot.add_handler(CommandHandler("greply", greply_command))
    app_bot.add_handler(CommandHandler("greact", greact_command))

    app_bot.add_handler(CommandHandler("addsticker", addsticker_command))
    app_bot.add_handler(CommandHandler("rmsticker", rmsticker_command))
    app_bot.add_handler(CommandHandler("stickerlist", stickerlist_command))
    app_bot.add_handler(CommandHandler("addword", addword_command))
    app_bot.add_handler(CommandHandler("rmword", rmword_command))
    app_bot.add_handler(CommandHandler("wordlist", wordlist_command))

    # Add these lines inside your main() function
    app_bot.add_handler(CommandHandler("blockword", blockword_command))
    app_bot.add_handler(CommandHandler("unblockword", unblockword_command))
    app_bot.add_handler(CommandHandler("blocksticker", blocksticker_command))
    app_bot.add_handler(CommandHandler("unblocksticker", unblocksticker_command))
    app_bot.add_handler(CommandHandler("listlocal", listlocal_command))

    # Add these lines inside your main() function
    app_bot.add_handler(CommandHandler("gban", gban_command))
    app_bot.add_handler(CommandHandler("ungban", ungban_command))
    app_bot.add_handler(CommandHandler("gbanlist", gbanlist_command))

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
