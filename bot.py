import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Optional

import httpx
import telegram.error
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0, connect=2.0),
    limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)
)

BOT_TOKEN        = "8780191059:AAFZchV5l8-TPUW9DwmCQAId5zc0jTx8ztM"
SUPABASE_URL     = "https://bwrwwihwunryuwsrkwzv.supabase.co"
SUPABASE_KEY     = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJ3cnd3aWh3dW5yeXV3c3Jrd3p2Iiwicm9sZSI6"
    "InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDkwNTIxNiwiZXhwIjoyMDk2NDgxMjE2fQ."
    "pXnoe0x9gd4WNt_sOwFv_scK6qAx_B4rCoCC7PbS_s8"
)
ADMIN_ID         = 64552009
SUPPORT_USERNAME = "@exchangeofcryptoo"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ShopBot")

_SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

_settings_cache: dict = {}
_user_cache: dict = {}
_CACHE_TTL = 15

def bg_task(coro):
    """Runs Telegram API calls in the background to prevent UI freezing."""
    async def _wrap():
        try:
            await coro
        except Exception:
            pass
    asyncio.create_task(_wrap())


async def sb_select(table: str, filters_str: str = "", limit: int = 100, order: str = "") -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}?select=*"
    if filters_str:
        url += f"&{filters_str}"
    if order:
        url += f"&order={order}"
    if limit:
        url += f"&limit={limit}"
    r = await _client.get(url, headers=_SB_HEADERS)
    r.raise_for_status()
    return r.json()

async def sb_insert(table: str, data: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = await _client.post(url, headers=_SB_HEADERS, json=data)
    r.raise_for_status()
    result = r.json()
    return result[0] if result else {}

async def sb_update(table: str, data: dict, filters_str: str) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters_str}"
    r = await _client.patch(url, headers=_SB_HEADERS, json=data)
    r.raise_for_status()
    return r.json()

async def sb_upsert(table: str, data: dict, on_conflict: str = "") -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    headers = {**_SB_HEADERS}
    if on_conflict:
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    r = await _client.post(url, headers=headers, json=data)
    r.raise_for_status()
    result = r.json()
    return result[0] if result else {}

async def sb_rpc(rpc_name: str, params: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/rpc/{rpc_name}"
    r = await _client.post(url, headers=_SB_HEADERS, json=params)
    r.raise_for_status()
    return r.json()

ST         = "state"
RECIPIENTS = "recipients"
PLAN       = "plan"
PRICE      = "price"
QUANTITY   = "quantity"
CHANNEL    = "channel"

S_MAIN          = "MAIN"
S_PREM_RECIP    = "PREM_RECIP"
S_PREM_PLAN     = "PREM_PLAN"
S_PREM_PAY      = "PREM_PAY"
S_STARS_RECIP   = "STARS_RECIP"
S_STARS_QTY     = "STARS_QTY"
S_STARS_PAY     = "STARS_PAY"
S_BOOST_CHAN    = "BOOST_CHAN"
S_BOOST_QTY     = "BOOST_QTY"
S_BOOST_PAY     = "BOOST_PAY"

_PROCESSING: set = set()

async def db_ensure_user(user_id: int, username: str) -> dict:
    u = await db_get_user(user_id)
    if u:
        if u.get("username") != username:
            u["username"] = username
            _user_cache[user_id] = (u, time.monotonic())
            bg_task(sb_update("users", {"username": username or ""}, f"user_id=eq.{user_id}"))
        return u
    data = {
        "user_id": user_id,
        "username": username or "",
        "balance": 0.0,
        "created_at": datetime.utcnow().isoformat(),
    }
    _user_cache[user_id] = (data, time.monotonic())
    bg_task(sb_insert("users", data))
    return data

async def db_get_user(user_id: int) -> Optional[dict]:
    now = time.monotonic()
    if user_id in _user_cache:
        data, ts = _user_cache[user_id]
        if now - ts < _CACHE_TTL:
            return data
    try:
        rows = await sb_select("users", f"user_id=eq.{user_id}")
        if rows:
            _user_cache[user_id] = (rows[0], now)
            return rows[0]
    except Exception:
        pass
    return None

async def db_get_balance(user_id: int) -> float:
    u = await db_get_user(user_id)
    return float(u["balance"]) if u else 0.0

async def db_add_balance(user_id: int, amount: float) -> float:
    u = await db_get_user(user_id)
    new_bal = amount
    if u:
        new_bal = float(u["balance"]) + amount
        u["balance"] = new_bal
        _user_cache[user_id] = (u, time.monotonic())
    bg_task(sb_update("users", {"balance": new_bal}, f"user_id=eq.{user_id}"))
    return new_bal

async def db_remove_balance(user_id: int, amount: float) -> float:
    u = await db_get_user(user_id)
    new_bal = 0.0
    if u:
        new_bal = max(0.0, float(u["balance"]) - amount)
        u["balance"] = new_bal
        _user_cache[user_id] = (u, time.monotonic())
    bg_task(sb_update("users", {"balance": new_bal}, f"user_id=eq.{user_id}"))
    return new_bal

async def db_deduct_atomic(user_id: int, amount: float) -> tuple[bool, float]:
    try:
        res = await sb_rpc("deduct_balance", {"p_user_id": user_id, "p_amount": amount})
        success = res.get("success", False)
        new_balance = float(res.get("new_balance", 0.0))
        if success:
            u = await db_get_user(user_id)
            if u:
                u["balance"] = new_balance
                _user_cache[user_id] = (u, time.monotonic())
        return success, new_balance
    except Exception as e:
        logger.error(f"db_deduct_atomic error: {e}")
        return False, await db_get_balance(user_id)

async def db_create_order(user_id, product_type, recipient, quantity, plan, price) -> str:
    oid = str(uuid.uuid4())[:8].upper()
    bg_task(sb_insert("orders", {
        "order_id": oid,
        "user_id": user_id,
        "product_type": product_type,
        "recipient": recipient,
        "quantity": int(quantity),
        "plan": plan,
        "price": float(price),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }))
    return oid

async def db_get_setting(key: str, default: str = "") -> str:
    now = time.monotonic()
    if key in _settings_cache:
        val, ts = _settings_cache[key]
        if now - ts < 120:
            return val
    try:
        rows = await sb_select("settings", f"key=eq.{key}", limit=1)
        val = rows[0]["value"] if rows else default
        _settings_cache[key] = (val, now)
        return val
    except Exception:
        return default

async def db_set_setting(key: str, value) -> None:
    val_str = str(value)
    _settings_cache[key] = (val_str, time.monotonic())
    bg_task(sb_upsert("settings", {"key": key, "value": val_str}, on_conflict="key"))

async def db_get_orders(status: str = None) -> list:
    f = f"status=eq.{status}" if status else ""
    return await sb_select("orders", f, limit=50, order="created_at.desc")

async def db_get_user_orders(user_id: int) -> list:
    return await sb_select("orders", f"user_id=eq.{user_id}", limit=20, order="created_at.desc")

async def db_get_user_lang(user_id: int, context=None) -> str:
    if context and context.user_data.get("_lang"):
        return context.user_data["_lang"]
    lang = await db_get_setting(f"lang_{user_id}", "en")
    if context: context.user_data["_lang"] = lang
    return lang

async def db_set_user_lang(user_id: int, lang: str, context=None):
    await db_set_setting(f"lang_{user_id}", lang)
    if context: context.user_data["_lang"] = lang

async def get_usdt_balance(wallet_address: str) -> float:
    usdt_contract = "0x55d398326f99059ff775485246999027b3197955"
    
    clean_addr = wallet_address.lower().replace("0x", "")
    padded_addr = clean_addr.rjust(64, "0")
    data_payload = "0x70a08231" + padded_addr
    
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {
                "to": usdt_contract,
                "data": data_payload
            },
            "latest"
        ],
        "id": 1
    }
    
    rpc_nodes = ["https://binance.llamarpc.com", "https://bsc-dataseed.binance.org", "https://bsc.drpc.org"]
    
    for node_url in rpc_nodes:
        try:
            r = await _client.post(node_url, json=payload, timeout=4)
            res = r.json()
            balance_hex = res.get("result")
            if balance_hex and balance_hex != "0x":
                clean_hex = balance_hex.replace("0x", "")
                wei_val = int(clean_hex, 16)
                return wei_val / (10**18)
        except Exception as e:
            logger.error(f"Error checking balance from {node_url}: {e}")
            continue
            
    return -1.0

TEXTS = {
    "en": {
        "btn_premium": "Telegram Premium",
        "btn_stars": "Stars",
        "btn_boosts": "Boosts",
        "btn_wallet": "Wallet",
        "btn_purchases": "Purchases",
        "btn_lang": "Change Language",
        "btn_support": "Contact Support",
        "btn_back_main": "Back To Main Menu",
        "btn_buy_myself": "Buy For Myself",
        "btn_change_recip": "Change Recipient",
        "btn_pay_balance": "Pay with Balance",
        "btn_cancel": "Cancel",
        "btn_deposit": "Deposit USDT (BEP20)",
        "msg_welcome": "<tg-emoji emoji-id='5994750571041525522'>👋</tg-emoji> <b>Welcome!</b>\n\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Current Balance:</b> <code>${bal:.2f}</code>\n\nPlease choose an option below.",
        "msg_no_orders": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> You have no past purchases.",
        "msg_purchases": "<tg-emoji emoji-id=\'5924720918826848520\'>📦</tg-emoji> <b>Your Purchases</b>\n\n",
        "order_status_pending": "Pending",
        "order_status_completed": "Completed",
        "order_status_failed": "Failed",
        "order_status_cancelled": "Cancelled",
        "lang_selected": "✅ Language changed to English!",
        "lang_choose": "🌐 Please select your language:",
        "plan_3mo": "3 Months",
        "plan_6mo": "6 Months",
        "plan_12mo": "12 Months",
        "order_id_lbl": "ID:",
        "order_status_lbl": "Status:",
        "err_max5": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Maximum 5 usernames allowed.",
        "err_enter_user": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Please enter at least one valid username.",
        "err_valid_stars": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Please enter a valid positive number of stars.",
        "err_valid_boosts": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Please enter a valid positive number of boosts.",
        "err_only_stars": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Only <b>{stock}</b> stars available right now.",
        "err_only_boosts": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Only <b>{stock}</b> boosts available right now.",
        "err_channel": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Please send a valid channel username or link.",
        "err_invalid_price": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Invalid price. Please start over.",
        "err_session": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Session expired — please start over.",
        "err_insufficient": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>Insufficient Balance</b>\n\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Wallet Balance:</b> <code>${bal:.2f}</code>\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Required:</b> <code>${price:.2f}</code>",
        "err_invalid_user": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> @{u} is not a valid Telegram username",
    },
    "hinglish": {
        "btn_premium": "Telegram Premium",
        "btn_stars": "Stars",
        "btn_boosts": "Boosts",
        "btn_wallet": "Wallet",
        "btn_purchases": "Purchases",
        "btn_lang": "Language Badlein",
        "btn_support": "Contact Support",
        "btn_back_main": "Main Menu par Jayein",
        "btn_buy_myself": "Apne Liye Buy Karein",
        "btn_change_recip": "Recipient Badlein",
        "btn_pay_balance": "Balance se Pay Karein",
        "btn_cancel": "Cancel Karein",
        "btn_deposit": "Deposit USDT (BEP20)",
        "msg_welcome": "<tg-emoji emoji-id='5994750571041525522'>👋</tg-emoji> <b>Swagat hai!</b>\n\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Aapka Balance:</b> <code>${bal:.2f}</code>\n\nKripya neeche diye gaye option ko chunein.",
        "msg_no_orders": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Aapne pehle koi purchase nahi kiya hai.",
        "msg_purchases": "<tg-emoji emoji-id=\'5924720918826848520\'>📦</tg-emoji> <b>Aapki Purchases</b>\n\n",
        "order_status_pending": "Pending",
        "order_status_completed": "Completed",
        "order_status_failed": "Failed",
        "order_status_cancelled": "Cancelled",
        "lang_selected": "✅ Language badal kar Hinglish ho gayi hai!",
        "lang_choose": "🌐 Kripya apni language chunein:",
        "plan_3mo": "3 Months",
        "plan_6mo": "6 Months",
        "plan_12mo": "12 Months",
        "order_id_lbl": "ID:",
        "order_status_lbl": "Status:",
        "err_max5": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Maximum 5 usernames allowed hain.",
        "err_enter_user": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Kripya kam se kam ek sahi username dalein.",
        "err_valid_stars": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Kripya sahi stars ki sankhya dalein.",
        "err_valid_boosts": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Kripya sahi boosts ki sankhya dalein.",
        "err_only_stars": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Abhi sirf <b>{stock}</b> stars hi available hain.",
        "err_only_boosts": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Abhi sirf <b>{stock}</b> boosts hi available hain.",
        "err_channel": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Kripya sahi channel ka username ya link dalein.",
        "err_invalid_price": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Galat price. Kripya shuru se start karein.",
        "err_session": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Session expire ho gaya hai — please shuru se start karein.",
        "err_insufficient": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>Balance Kam Hai</b>\n\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Wallet Balance:</b> <code>${bal:.2f}</code>\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Chahiye:</b> <code>${price:.2f}</code>",
        "err_invalid_user": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> @{u} ek sahi Telegram username nahi hai.",
    },
    "ru": {
        "btn_premium": "Telegram Премиум",
        "btn_stars": "Звёзды",
        "btn_boosts": "Бусты",
        "btn_wallet": "Кошелёк",
        "btn_purchases": "Покупки",
        "btn_lang": "Изменить язык",
        "btn_support": "Поддержка",
        "btn_back_main": "На главную",
        "btn_buy_myself": "Купить себе",
        "btn_change_recip": "Изменить получателя",
        "btn_pay_balance": "Оплатить балансом",
        "btn_cancel": "Отмена",
        "btn_deposit": "Пополнить USDT (BEP20)",
        "msg_welcome": "<tg-emoji emoji-id='5994750571041525522'>👋</tg-emoji> <b>Добро пожаловать!</b>\n\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Текущий баланс:</b> <code>${bal:.2f}</code>\n\nПожалуйста, выберите опцию ниже.",
        "msg_no_orders": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> У вас нет прошлых покупок.",
        "msg_purchases": "<tg-emoji emoji-id=\'5924720918826848520\'>📦</tg-emoji> <b>Ваши покупки</b>\n\n",
        "order_status_pending": "В ожидании",
        "order_status_completed": "Выполнено",
        "order_status_failed": "Ошибка",
        "order_status_cancelled": "Отменено",
        "lang_selected": "✅ Язык изменён на Русский!",
        "lang_choose": "🌐 Пожалуйста, выберите язык:",
        "plan_3mo": "3 Месяца",
        "plan_6mo": "6 Месяцев",
        "plan_12mo": "12 Месяцев",
        "order_id_lbl": "ID:",
        "order_status_lbl": "Статус:",
        "err_max5": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Максимум 5 юзернеймов.",
        "err_enter_user": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Введите хотя бы один действительный юзернейм.",
        "err_valid_stars": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Введите положительное число звёзд.",
        "err_valid_boosts": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Введите положительное число бустов.",
        "err_only_stars": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Доступно только <b>{stock}</b> звёзд.",
        "err_only_boosts": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Доступно только <b>{stock}</b> бустов.",
        "err_channel": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Отправьте действительный юзернейм или ссылку канала.",
        "err_invalid_price": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Неверная цена. Начните сначала.",
        "err_session": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Сессия истекла — начните сначала.",
        "err_insufficient": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>Недостаточно средств</b>\n\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Баланс:</b> <code>${bal:.2f}</code>\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Требуется:</b> <code>${price:.2f}</code>",
        "err_invalid_user": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> @{u} — недействительный юзернейм Telegram",
    },
    "zh": {
        "btn_premium": "Telegram 会员",
        "btn_stars": "星星",
        "btn_boosts": "助力",
        "btn_wallet": "钱包",
        "btn_purchases": "购买记录",
        "btn_lang": "更改语言",
        "btn_support": "联系客服",
        "btn_back_main": "返回主菜单",
        "btn_buy_myself": "给自己购买",
        "btn_change_recip": "更改接收者",
        "btn_pay_balance": "余额支付",
        "btn_cancel": "取消",
        "btn_deposit": "存入 USDT (BEP20)",
        "msg_welcome": "<tg-emoji emoji-id='5994750571041525522'>👋</tg-emoji> <b>欢迎！</b>\n\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>当前余额:</b> <code>${bal:.2f}</code>\n\n请在下方选择一个选项。",
        "msg_no_orders": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 您没有历史购买记录。",
        "msg_purchases": "<tg-emoji emoji-id=\'5924720918826848520\'>📦</tg-emoji> <b>您的购买记录</b>\n\n",
        "order_status_pending": "待处理",
        "order_status_completed": "已完成",
        "order_status_failed": "失败",
        "order_status_cancelled": "已取消",
        "lang_selected": "✅ 语言已更改为中文！",
        "lang_choose": "🌐 请选择您的语言：",
        "plan_3mo": "3 个月",
        "plan_6mo": "6 个月",
        "plan_12mo": "12 个月",
        "order_id_lbl": "编号:",
        "order_status_lbl": "状态:",
        "err_max5": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 最多只能输入 5 个用户名。",
        "err_enter_user": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 请输入至少一个有效的用户名。",
        "err_valid_stars": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 请输入有效的星星数量。",
        "err_valid_boosts": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 请输入有效的助力数量。",
        "err_only_stars": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 当前仅有 <b>{stock}</b> 颗星可用。",
        "err_only_boosts": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 当前仅有 <b>{stock}</b> 个助力可用。",
        "err_channel": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 请发送有效的频道用户名或链接。",
        "err_invalid_price": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 无效价格。请重新开始。",
        "err_session": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 会话已过期 — 请重新开始。",
        "err_insufficient": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>余额不足</b>\n\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>钱包余额：</b> <code>${bal:.2f}</code>\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>需要：</b> <code>${price:.2f}</code>",
        "err_invalid_user": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> @{u} 不是有效的 Telegram 用户名",
    }
}

async def price_premium(months: int) -> float:
    defaults = {3: "9.99", 6: "17.99", 12: "29.99"}
    return float(await db_get_setting(f"premium{months}_price", defaults.get(months, "9.99")))

async def price_star() -> float:
    return float(await db_get_setting("star_price", "0.02"))

async def price_boost() -> float:
    return float(await db_get_setting("boost_price", "0.69"))

async def boost_stock() -> int:
    return int(await db_get_setting("boost_stock", "0"))

async def star_stock() -> int:
    return int(await db_get_setting("star_stock", "0"))

def parse_usernames(text: str) -> list[str]:
    parts = re.split(r'[\s,;|]+', text.strip())
    out = []
    for p in parts:
        p = p.strip().lstrip("@")
        if p:
            out.append(p)
    return out[:5]

def validate_username(username: str) -> tuple[bool, str]:
    clean = username.lstrip("@").strip()
    if not clean or not re.match(r'^[a-zA-Z][a-zA-Z0-9_]{3,31}$', clean):
        return False, f"@{clean} is not a valid Telegram username"
    return True, clean

support_url = f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"

def _(key: str, lang: str) -> str:
    return TEXTS.get(lang, TEXTS["en"]).get(key, TEXTS["en"].get(key, key))

def kb_main(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_premium", lang), callback_data="m:premium", api_kwargs={"icon_custom_emoji_id": "5274026806477857971"}),
         InlineKeyboardButton(_("btn_stars", lang), callback_data="m:stars", api_kwargs={"icon_custom_emoji_id": "5346309121794659890"})],
        [InlineKeyboardButton(_("btn_boosts", lang), callback_data="m:boosts", api_kwargs={"icon_custom_emoji_id": "5436068999068662274"}),
         InlineKeyboardButton(_("btn_wallet", lang), callback_data="m:wallet", api_kwargs={"icon_custom_emoji_id": "5215420556089776398"})],
        [InlineKeyboardButton(_("btn_purchases", lang), callback_data="m:purchases", api_kwargs={"icon_custom_emoji_id": "5472250091332993630"}),
         InlineKeyboardButton(_("btn_lang", lang), callback_data="m:lang", api_kwargs={"icon_custom_emoji_id": "5447410659077661506"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

def kb_support_back(lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_back_main", lang), callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

def kb_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data="lang:en", api_kwargs={"icon_custom_emoji_id": "5888289745548612151"})],
        [InlineKeyboardButton("Hinglish", callback_data="lang:hinglish", api_kwargs={"icon_custom_emoji_id": "5888289745548612151"})],
        [InlineKeyboardButton("Русский", callback_data="lang:ru", api_kwargs={"icon_custom_emoji_id": "5888289745548612151"})],
        [InlineKeyboardButton("中文", callback_data="lang:zh", api_kwargs={"icon_custom_emoji_id": "5888289745548612151"})],
        [InlineKeyboardButton("Back", callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})]
    ])

def kb_premium_recip(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_buy_myself", lang), callback_data="p:myself", api_kwargs={"icon_custom_emoji_id": "5920344347152224466"})],
        [InlineKeyboardButton(_("btn_back_main", lang), callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

def kb_premium_plan(count: int, p3: float, p6: float, p12: float, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{_('plan_3mo', lang)} — ${p3 * count:.2f}",  callback_data="plan:3", api_kwargs={"icon_custom_emoji_id": "5987929428536075624"})],
        [InlineKeyboardButton(f"{_('plan_6mo', lang)} — ${p6 * count:.2f}",  callback_data="plan:6", api_kwargs={"icon_custom_emoji_id": "5987929428536075624"})],
        [InlineKeyboardButton(f"{_('plan_12mo', lang)} — ${p12 * count:.2f}", callback_data="plan:12", api_kwargs={"icon_custom_emoji_id": "5987929428536075624"})],
        [InlineKeyboardButton(_("btn_change_recip", lang), callback_data="p:change", api_kwargs={"icon_custom_emoji_id": "5877597667231534929"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

def kb_pay(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_pay_balance", lang), callback_data="pay:balance", api_kwargs={"icon_custom_emoji_id": "5409048419211682843"})],
        [InlineKeyboardButton(_("btn_cancel", lang), callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
    ])

def kb_wallet(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_deposit", lang), callback_data="w:deposit", api_kwargs={"icon_custom_emoji_id": "5778139491810155937"})],
        [InlineKeyboardButton(_("btn_back_main", lang), callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

def kb_stars_recip(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_buy_myself", lang), callback_data="s:myself", api_kwargs={"icon_custom_emoji_id": "5920344347152224466"})],
        [InlineKeyboardButton(_("btn_back_main", lang), callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

def kb_stars_qty(recipients: list, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_change_recip", lang), callback_data="s:change", api_kwargs={"icon_custom_emoji_id": "5877597667231534929"})],
        [InlineKeyboardButton(_("btn_back_main", lang), callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

def kb_boost_input(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_back_main", lang), callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

def kb_insufficient(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_("btn_deposit", lang), callback_data="w:deposit", api_kwargs={"icon_custom_emoji_id": "5778139491810155937"})],
        [InlineKeyboardButton(_("btn_back_main", lang), callback_data="m:main", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
        [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
    ])

# OPTIMIZATION 3: Async/Background Message Deletions to avoid UI stutters
async def _render_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, kb: InlineKeyboardMarkup, image_key: Optional[str] = None):
    uid = update.effective_user.id
    query = update.callback_query
    
    raw_img = await db_get_setting(image_key) if image_key else ""
    img = raw_img if raw_img and str(raw_img).strip().lower() != "none" else None
    
    if query:
        try:
            if img:
                if query.message.photo:
                    await query.edit_message_media(
                        media=InputMediaPhoto(media=img, caption=text, parse_mode=ParseMode.HTML),
                        reply_markup=kb
                    )
                else:
                    bg_task(query.message.delete()) # Background delete
                    sent = await context.bot.send_photo(uid, photo=img, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
                    context.user_data["_menu_msg_id"] = sent.message_id
            else:
                if query.message.photo:
                    bg_task(query.message.delete()) # Background delete
                    sent = await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=kb)
                    context.user_data["_menu_msg_id"] = sent.message_id
                else:
                    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except telegram.error.BadRequest as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str:
                return
            elif "wrong file identifier" in err_str or "http url specified" in err_str:
                logger.warning(f"Invalid image file_id for {image_key}. Falling back to text.")
                img = None 
            else:
                img = None
        except Exception as e:
            img = None 
            
    if not query and update.message:
        pass  # auto-delete disabled

    old_mid = context.user_data.get("_menu_msg_id")
    if old_mid:
        bg_task(context.bot.delete_message(uid, old_mid)) 

    sent = None
    if img:
        try:
            sent = await context.bot.send_photo(uid, photo=img, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except telegram.error.BadRequest as e:
            logger.warning(f"Failed to send image {image_key} (Invalid ID). Sending text instead.")
            sent = await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        sent = await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    
    if sent:
        context.user_data["_menu_msg_id"] = sent.message_id

async def _validate_and_set_premium_recip(update: Update, context: ContextTypes.DEFAULT_TYPE, usernames: list, lang: str):
    valid = []
    for u in usernames:
        valid.append(u.replace("@", ""))

    context.user_data[RECIPIENTS] = valid
    context.user_data[ST] = S_PREM_PLAN
    rdisp = "\n".join(f"@{r}" for r in valid)
    
    text_map = {
        "en": f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product: Premium</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\nSelect how many months:",
        "hinglish": f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product: Premium</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\nMonths chunein:",
        "ru": f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Продукт: Премиум</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Получатель(и):</b>\n{rdisp}\n\nВыберите количество месяцев:",
        "zh": f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>产品：会员</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>接收者：</b>\n{rdisp}\n\n选择几个月："
    }
    msg = text_map.get(lang, text_map["en"])
    
    
    p3, p6, p12 = await asyncio.gather(price_premium(3), price_premium(6), price_premium(12))
    kb = kb_premium_plan(len(valid), p3, p6, p12, lang)
    
    await _render_menu(update, context, msg, kb, "premium_image")

async def _execute_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    query = update.callback_query
    state = context.user_data.get(ST)
    price = float(context.user_data.get(PRICE, 0))
    lang = await db_get_user_lang(uid, context)

    img_key = "start_image"
    if state == S_PREM_PAY: img_key = "premium_image"
    elif state == S_STARS_PAY: img_key = "stars_image"
    elif state == S_BOOST_PAY: img_key = "boosts_image"

    if price <= 0:
        await _render_menu(update, context, _("err_invalid_price", lang), kb_support_back(lang), img_key)
        return

    ok, new_bal = await db_deduct_atomic(uid, price)
    if not ok:
        bal = await db_get_balance(uid)
        await _render_menu(update, context, _("err_insufficient", lang).format(bal=bal, price=price), kb_insufficient(lang), img_key)
        return

    uname = query.from_user.username or ""
    receipt_text = ""
    admin_text = ""

    if state == S_PREM_PAY:
        recips  = context.user_data.get(RECIPIENTS, [])
        months  = context.user_data.get(PLAN, 3)
        rstr    = ", ".join(f"@{r}" for r in recips)
        rdisp   = "\n".join(f"@{r}" for r in recips)
        oid     = await db_create_order(uid, "premium", rstr, len(recips), f"{months}mo", price)
        text_map = {
            "en": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Order Placed!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order ID:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product:</b> Premium\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>Plan:</b> {months} Months\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>New Balance:</b> <code>${new_bal:.2f}</code>\n\nYour order is being processed! <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>",
            "hinglish": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Order Submit Ho Gaya!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order ID:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product:</b> Premium\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>Plan:</b> {months} Months\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Naya Balance:</b> <code>${new_bal:.2f}</code>\n\nAapka order process ho raha hai! <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>",
            "ru": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Заказ оформлен!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>ID заказа:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Продукт:</b> Премиум\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Получатель(и):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>План:</b> {months} Месяцев\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Цена:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Новый баланс:</b> <code>${new_bal:.2f}</code>\n\nВаш заказ обрабатывается! <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>",
            "zh": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>订单已提交！</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>订单编号：</b> <code>{oid}</code>\n<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>产品：</b> 会员\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>接收者：</b>\n{rdisp}\n\n<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>计划：</b> {months} 个月\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>价格：</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>新余额：</b> <code>${new_bal:.2f}</code>\n\n您的订单正在处理中！ <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>"
        }
        receipt_text = text_map.get(lang, text_map["en"])
        admin_text = (
            f"<tg-emoji emoji-id='5458603043203327669'>🔔</tg-emoji> <b>NEW ORDER</b>\n\n"
            f"<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order:</b> <code>{oid}</code>\n"
            f"<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>User:</b> @{uname} (<code>{uid}</code>)\n"
            f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product:</b> Premium\n"
            f"<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>Plan:</b> {months} Months\n"
            f"<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\n"
            f"<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>"
        )

    elif state == S_STARS_PAY:
        recips  = context.user_data.get(RECIPIENTS, [])
        qty     = context.user_data.get(QUANTITY, 0)
        total_stars = qty * len(recips)
        rstr    = ", ".join(f"@{r}" for r in recips)
        rdisp   = "\n".join(f"@{r}" for r in recips)
        oid     = await db_create_order(uid, "stars", rstr, total_stars, "", price)
        cur_stock = await star_stock()
        bg_task(db_set_setting("star_stock", max(0, cur_stock - total_stars)))
        text_map = {
            "en": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Order Placed!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order ID:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product:</b> Stars\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>Quantity:</b> {qty}\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>New Balance:</b> <code>${new_bal:.2f}</code>\n\nYour order is being processed! <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>",
            "hinglish": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Order Submit Ho Gaya!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order ID:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product:</b> Stars\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>Quantity:</b> {qty}\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Naya Balance:</b> <code>${new_bal:.2f}</code>\n\nAapka order process ho raha hai! <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>",
            "ru": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Заказ оформлен!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>ID заказа:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Продукт:</b> Звёзды\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Получатель(и):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>Количество:</b> {qty}\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Цена:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Новый баланс:</b> <code>${new_bal:.2f}</code>\n\nВаш заказ обрабатывается! <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>",
            "zh": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>订单已提交！</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>订单编号：</b> <code>{oid}</code>\n<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>产品：</b> 星星\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>接收者：</b>\n{rdisp}\n\n<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>数量：</b> {qty}\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>价格：</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>新余额：</b> <code>${new_bal:.2f}</code>\n\n您的订单正在处理中！ <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>"
        }
        receipt_text = text_map.get(lang, text_map["en"])
        admin_text = (
            f"<tg-emoji emoji-id='5458603043203327669'>🔔</tg-emoji> <b>NEW ORDER</b>\n\n"
            f"<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order:</b> <code>{oid}</code>\n"
            f"<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>User:</b> @{uname} (<code>{uid}</code>)\n"
            f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product:</b> Stars\n"
            f"<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>Quantity:</b> {qty}\n"
            f"<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\n"
            f"<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>"
        )

    elif state == S_BOOST_PAY:
        channel = context.user_data.get(CHANNEL, "")
        qty     = context.user_data.get(QUANTITY, 0)
        oid     = await db_create_order(uid, "boosts", channel, qty, "", price)
        cur_stock = await boost_stock()
        bg_task(db_set_setting("boost_stock", max(0, cur_stock - qty)))
        text_map = {
            "en": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Order Placed!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order ID:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product:</b> Boosts\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Channel:</b> {channel}\n\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Quantity:</b> {qty} boosts\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>New Balance:</b> <code>${new_bal:.2f}</code>\n\nYour order is being processed! <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>",
            "hinglish": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Order Submit Ho Gaya!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order ID:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product:</b> Boosts\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Channel:</b> {channel}\n\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Quantity:</b> {qty} boosts\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Naya Balance:</b> <code>${new_bal:.2f}</code>\n\nAapka order process ho raha hai! <tg-emoji emoji-id='5456140674028019486'>⚡</tg-emoji>",
            "ru": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Заказ оформлен!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>ID заказа:</b> <code>{oid}</code>\n<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Продукт:</b> Бусты\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Количество:</b> {qty} бустов\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Цена:</b> <code>${price:.2f}</code>\n\nВыберите метод оплаты:",
            "zh": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>订单已提交！</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>订单编号：</b> <code>{oid}</code>\n<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>产品：</b> 助力\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>频道：</b> {channel}\n\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>数量：</b> {qty} 助力\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>价格：</b> <code>${price:.2f}</code>\n\n选择付款方式："
        }
        receipt_text = text_map.get(lang, text_map["en"])
        admin_text = (
            f"<tg-emoji emoji-id='5458603043203327669'>🔔</tg-emoji> <b>NEW ORDER</b>\n\n"
            f"<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order:</b> <code>{oid}</code>\n"
            f"<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>User:</b> @{uname} (<code>{uid}</code>)\n"
            f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product:</b> Boosts\n"
            f"<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Channel:</b> {channel}\n"
            f"<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Quantity:</b> {qty} boosts\n\n"
            f"<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>"
        )
    else:
        await _render_menu(update, context, _("err_session", lang), kb_support_back(lang), "start_image")
        return

    # Render success receipt to user
    await _render_menu(update, context, receipt_text, kb_support_back(lang), img_key)

    # Notify Admin
    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Complete", callback_data=f"adm:complete:{oid}:{uid}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"adm:cancel:{oid}:{uid}")],
    ])
    try:
        bg_task(context.bot.send_message(
            ADMIN_ID, admin_text,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_kb
        ))
    except Exception:
        pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid   = update.effective_user.id
    uname = update.effective_user.username or ""
    try:
        
        user_info, bal, lang = await asyncio.gather(
            db_ensure_user(uid, uname),
            db_get_balance(uid),
            db_get_user_lang(uid, context)
        )
    except Exception as e:
        logger.error(f"start cmd error: {e}")
        bal, lang = 0.0, "en"
        
    text = _("msg_welcome", lang).format(bal=bal)
    await _render_menu(update, context, text, kb_main(lang), "start_image")
    context.user_data[ST] = S_MAIN


async def handle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    bg_task(query.answer())
    
    data  = query.data
    uid   = query.from_user.id
    uname = query.from_user.username or ""
    
    
    lang, bal = await asyncio.gather(
        db_get_user_lang(uid, context),
        db_get_balance(uid)
    )

    if data == "m:main":
        context.user_data.clear()
        text = _("msg_welcome", lang).format(bal=bal)
        await _render_menu(update, context, text, kb_main(lang), "start_image")
        context.user_data[ST] = S_MAIN
        return

    if data == "m:lang":
        await _render_menu(update, context, _("lang_choose", lang), kb_language(), "language_image")
        return

    if data.startswith("lang:"):
        new_lang = data.split(":")[1]
        bg_task(db_set_user_lang(uid, new_lang, context))
        bg_task(query.answer(_("lang_selected", new_lang), show_alert=True))
        text = _("msg_welcome", new_lang).format(bal=bal)
        await _render_menu(update, context, text, kb_main(new_lang), "start_image")
        context.user_data[ST] = S_MAIN
        return

    if data == "m:purchases":
        orders = await db_get_user_orders(uid)
        if not orders:
            await _render_menu(update, context, _("msg_no_orders", lang), kb_support_back(lang), "purchases_image")
            return
        
        text = _("msg_purchases", lang)
        for o in orders:
            dt = o.get("created_at", "").replace("T", " ")[:16]
            st = o.get("status", "pending")
            st_trans = _(f"order_status_{st}", lang)
            prod = o.get('product_type', '').lower()
            if prod == "premium":
                prod_trans = _("btn_premium", lang)
                prod_emoji = "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji>"
            elif prod == "stars":
                prod_trans = _("btn_stars", lang)
                prod_emoji = "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji>"
            elif prod == "boosts":
                prod_trans = _("btn_boosts", lang)
                prod_emoji = "<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji>"
            else:
                prod_trans = prod.capitalize()
                prod_emoji = "<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji>"
                
            qty = o.get('quantity', 0)
            text += f"{prod_emoji} <b>{prod_trans}</b> ({qty})\n"
            text += f"   <tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> {_('order_id_lbl', lang)} <code>{o.get('order_id')}</code>\n"
            text += f"   <tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> {_('order_status_lbl', lang)} {st_trans} | {dt}\n\n"
            
        await _render_menu(update, context, text, kb_support_back(lang), "purchases_image")
        return

    if data == "m:premium":
        context.user_data[ST] = S_PREM_RECIP
        text_map = {
            "en": "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product: Premium</b>\n\nWho is the premium for?\n\nSend up to <b>5 usernames</b> separated by spaces or commas.\n\nExample: <code>@user1 @user2 @user3</code>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>PLEASE TYPE AND SEND USERNAME(S)</b>",
            "hinglish": "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product: Premium</b>\n\nPremium kiske liye chahiye?\n\nEk sath maximum <b>5 usernames</b> spaces ya commas se separate karke bhejein.\n\nExample: <code>@user1 @user2 @user3</code>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>KRIPYA USERNAME(S) TYPE KARKE BHEJEIN</b>",
            "ru": "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Продукт: Премиум</b>\n\nКому отправить Premium?\n\nОтправьте до <b>5 юзернеймов</b>, разделенных пробелом или запятой.\n\nПример: <code>@user1 @user2 @user3</code>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>ПОЖАЛУЙСТА, ВВЕДИТЕ И ОТПРАВЬТЕ ЮЗЕРНЕЙМ(Ы)</b>",
            "zh": "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>产品：会员</b>\n\n会员接收者是谁？\n\n发送最多 <b>5 个用户名</b>，用空格或逗号分隔。\n\n例如：<code>@user1 @user2 @user3</code>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>请键入并发送用户名</b>"
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_premium_recip(lang), "premium_image")
        return

    if data == "p:myself":
        if not uname:
            text_map = {
                "en": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Your account has no Telegram username set.\nPlease add a username in Telegram settings first.",
                "hinglish": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Aapke account ka Telegram username set nahi hai.\nKripya pehle Telegram settings me jakar username set karein.",
                "ru": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> У вашего аккаунта нет юзернейма.\nСначала добавьте юзернейм в настройках Telegram.",
                "zh": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 您的账户未设置 Telegram 用户名。\n请先在 Telegram 设置中添加用户名。"
            }
            await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_support_back(lang), "premium_image")
            return
        context.user_data[ST] = S_PREM_RECIP
        await _validate_and_set_premium_recip(update, context, [uname], lang)
        return

    if data == "p:change":
        context.user_data[ST] = S_PREM_RECIP
        context.user_data.pop(RECIPIENTS, None)
        text_map = {
            "en": "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product: Premium</b>\n\nWho is the premium for?\n\nSend up to <b>5 usernames</b>:\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>PLEASE TYPE AND SEND USERNAME(S)</b>",
            "hinglish": "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product: Premium</b>\n\nPremium kiske liye chahiye?\n\nMaximum 5 usernames bhejein:\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>KRIPYA USERNAME(S) TYPE KARKE BHEJEIN</b>",
            "ru": "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Продукт: Премиум</b>\n\nКому отправить Premium?\n\nОтправьте до <b>5 юзернеймов</b>:\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>ПОЖАЛУЙСТА, ВВЕДИТЕ И ОТПРАВЬТЕ ЮЗЕРНЕЙМ(Ы)</b>",
            "zh": "<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>产品：会员</b>\n\n会员接收者是谁？\n\n发送最多 <b>5 个用户名</b>：\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>请键入并发送用户名</b>"
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_premium_recip(lang), "premium_image")
        return

    if data.startswith("plan:"):
        months = int(data.split(":")[1])
        recips = context.user_data.get(RECIPIENTS, [])
        if not recips:
            await _render_menu(update, context, _("err_session", lang), kb_support_back(lang), "premium_image")
            return
        p = await price_premium(months) * len(recips)
        context.user_data[PLAN]  = months
        context.user_data[PRICE] = p
        context.user_data[ST]    = S_PREM_PAY
        rstr = "\n".join(f"@{r}" for r in recips)
        text_map = {
            "en": f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product: Premium</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rstr}\n\n<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>Plan:</b> {months} Months\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${p:.2f}</code>\n\nSelect payment method:",
            "hinglish": f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product: Premium</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rstr}\n\n<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>Plan:</b> {months} Months\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${p:.2f}</code>\n\nPayment ka tarika chunein:",
            "ru": f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Продукт: Премиум</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Получатель(и):</b>\n{rstr}\n\n<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>План:</b> {months} Месяцев\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Цена:</b> <code>${p:.2f}</code>\n\nВыберите метод оплаты:",
            "zh": f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>产品：会员</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>接收者：</b>\n{rstr}\n\n<tg-emoji emoji-id='5987929428536075624'>📅</tg-emoji> <b>计划：</b> {months} 个月\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>价格：</b> <code>${p:.2f}</code>\n\n选择付款方式："
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_pay(lang), "premium_image")
        return

    if data == "m:stars":
        context.user_data[ST] = S_STARS_RECIP
        text_map = {
            "en": "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\nWho are the stars for?\n\nSend up to <b>5 usernames</b>.\n\nExample: <code>@user1 @user2</code>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>PLEASE TYPE USERNAME(S)</b>",
            "hinglish": "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\nStars kiske liye chahiye?\n\nMaximum 5 usernames bhejein.\n\nExample: <code>@user1 @user2</code>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>KRIPYA USERNAME(S) TYPE KAREIN</b>",
            "ru": "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Продукт: Звёзды</b>\n\nКому отправить звёзды?\n\nОтправьте до <b>5 юзернеймов</b>.\n\nПример: <code>@user1 @user2</code>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>ПОЖАЛУЙСТА, ВВЕДИТЕ ЮЗЕРНЕЙМ(Ы)</b>",
            "zh": "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>产品：星星</b>\n\n星星接收者是谁？\n\n发送最多 <b>5 个用户名</b>。\n\n例如：<code>@user1 @user2</code>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>请键入并发送用户名</b>"
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_stars_recip(lang), "stars_image")
        return

    if data == "s:myself":
        if not uname:
            text_map = {
                "en": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Your account has no Telegram username set.\nPlease add a username in Telegram settings first.",
                "hinglish": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Aapke account ka Telegram username set nahi hai.\nKripya pehle Telegram settings me jakar username set karein.",
                "ru": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> У вашего аккаунта нет юзернейма.\nСначала добавьте юзернейм в настройках Telegram.",
                "zh": "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> 您的账户未设置 Telegram 用户名。\n请先在 Telegram 设置中添加用户名。"
            }
            await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_support_back(lang), "stars_image")
            return
        context.user_data[RECIPIENTS] = [uname]
        context.user_data[ST] = S_STARS_QTY
        text_map = {
            "en": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient:</b> @{uname}\n\nHow many stars would you like?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Type a number and send</b>",
            "hinglish": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient:</b> @{uname}\n\nAapko kitne stars chahiye?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Ek number type karke bhejein</b>",
            "ru": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Продукт: Звёзды</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Получатель:</b> @{uname}\n\nСколько звёзд вы хотите?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Введите число и отправьте</b>",
            "zh": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>产品：星星</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>接收者：</b> @{uname}\n\n您想要多少颗星？\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>键入数字并发送</b>"
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_stars_qty([uname], lang), "stars_image")
        return

    if data == "s:change":
        context.user_data[ST] = S_STARS_RECIP
        context.user_data.pop(RECIPIENTS, None)
        text_map = {
            "en": "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\nWho are the stars for?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>PLEASE TYPE USERNAME(S)</b>",
            "hinglish": "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\nStars kiske liye chahiye?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>KRIPYA USERNAME(S) TYPE KAREIN</b>",
            "ru": "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Продукт: Звёзды</b>\n\nКому отправить звёзды?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>ПОЖАЛУЙСТА, ВВЕДИТЕ ЮЗЕРНЕЙМ(Ы)</b>",
            "zh": "<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>产品：星星</b>\n\n星星接收者是谁？\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>请键入并发送用户名</b>"
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_stars_recip(lang), "stars_image")
        return

    if data == "m:boosts":
        stock = await boost_stock()
        context.user_data[ST] = S_BOOST_CHAN
        text_map = {
            "en": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product: Boosts</b>\n\nWhich channel/group should be boosted?\n\nSend: <code>@username</code>  or  <code>t.me/link</code>  or invite link\n\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Available now:</b> {stock} boosts\n<tg-emoji emoji-id='5778496382117613636'>⏱</tg-emoji> Boosts last minimum <b>3 months</b>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Type and send the channel</b>",
            "hinglish": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product: Boosts</b>\n\nKaunse channel/group ko boost karna hai?\n\nBhejein: <code>@username</code> ya <code>t.me/link</code> ya invite link\n\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Available boosts:</b> {stock}\n<tg-emoji emoji-id='5778496382117613636'>⏱</tg-emoji> Boosts minimum <b>3 months</b> tak rahenge\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Channel type karke bhejein</b>",
            "ru": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Продукт: Бусты</b>\n\nКакой канал/группу бустить?\n\nОтправьте: <code>@username</code> или <code>t.me/link</code> или ссылку-приглашение\n\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Доступно:</b> {stock} бустов\n<tg-emoji emoji-id='5778496382117613636'>⏱</tg-emoji> Бусты работают минимум <b>3 месяца</b>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Введите и отправьте канал</b>",
            "zh": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>产品：助力</b>\n\n要助力哪个频道/群组？\n\n发送：<code>@username</code> 或 <code>t.me/link</code> 或邀请链接\n\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>当前可用：</b> {stock} 助力\n<tg-emoji emoji-id='5778496382117613636'>⏱</tg-emoji> 助力持续最少 <b>3 个月</b>\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>键入并发送频道</b>"
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_boost_input(lang), "boosts_image")
        return

    if data == "m:wallet":
        context.user_data[ST] = "WALLET"
        text_map = {
            "en": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Wallet Balance:</b> <code>${bal:.2f}</code>\n\nChoose an option:",
            "hinglish": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Wallet Balance:</b> <code>${bal:.2f}</code>\n\nOption chunein:",
            "ru": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Баланс Кошелька:</b> <code>${bal:.2f}</code>\n\nВыберите опцию:",
            "zh": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>钱包余额：</b> <code>${bal:.2f}</code>\n\n选择一个选项："
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_wallet(lang), "wallet_image")
        return

    if data == "w:deposit":
        now = int(time.time())
        lock_user = await db_get_setting("deposit_lock_user", "")
        lock_until_str = await db_get_setting("deposit_lock_until", "0")
        lock_until = int(lock_until_str) if lock_until_str.isdigit() else 0
        bep20_addr = await db_get_setting("bep20_address", "0x91Cc7f72821FFFb6f205e95AC7cf572Fe3Bab92a")
        
        if not bep20_addr or bep20_addr == "NOT_SET":
            bg_task(query.answer("❌ Deposit wallet is not configured by admin.", show_alert=True))
            return
            
        if lock_user and lock_until > now and lock_user != str(uid):
            remaining = lock_until - now
            text_map = {
                "en": f"⚠️ <b>Wallet is Busy</b>\n\nAnother user is currently making a deposit.\n\n⏱ Please wait up to <b>{remaining} seconds</b> and try again.",
                "hinglish": f"⚠️ <b>Wallet abhi busy hai</b>\n\nKoi doosra user abhi deposit kar raha hai.\n\n⏱ Kripya <b>{remaining} seconds</b> wait karein aur fir try karein.",
                "ru": f"⚠️ <b>Кошелек занят</b>\n\nДругой пользователь сейчас совершает пополнение.\n\n⏱ Подождите до <b>{remaining} сек.</b> и попробуйте снова.",
                "zh": f"⚠️ <b>钱包繁忙</b>\n\n另一位用户目前正在进行充值。\n\n⏱ 请等待最长 <b>{remaining} 秒</b> 后重试。"
            }
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh / Try Again", callback_data="w:deposit")],
                [InlineKeyboardButton("Back", callback_data="m:wallet", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})]
            ])
            await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb, "wallet_image")
            return
            
        bg_task(query.answer("⏳ Checking wallet state...", show_alert=True))
        starting_bal = await get_usdt_balance(bep20_addr)
        if starting_bal == -1.0:
            bg_task(query.answer("❌ Connection error. BSC nodes are busy. Try again in 5 seconds.", show_alert=True))
            return
            
        await db_set_setting("deposit_lock_user", str(uid))
        await db_set_setting("deposit_lock_until", str(now + 300))
        await db_set_setting("deposit_starting_balance", f"{starting_bal:.6f}")
            
        remaining = 300
        deposit_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 I Have Paid", callback_data="w:check_auto_deposit"),
             InlineKeyboardButton("❌ Cancel / Release Lock", callback_data="w:cancel_lock")]
        ])
        text_map = {
            "en": f"<tg-emoji emoji-id='5778139491810155937'>💎</tg-emoji> <b>Automated Deposit</b>\n\n1. Send USDT (BEP-20) to this address:\n<code>{bep20_addr}</code>\n\n2. After sending, click <b>I Have Paid</b> below!\n\n⏱ <b>Remaining Lock Time:</b> {remaining} seconds",
            "hinglish": f"<tg-emoji emoji-id='5778139491810155937'>💎</tg-emoji> <b>Automated Deposit</b>\n\n1. Is address par USDT (BEP-20) bhejein:\n<code>{bep20_addr}</code>\n\n2. Bhejne ke baad, <b>I Have Paid</b> par click karein!\n\n⏱ <b>Bacha hua samay:</b> {remaining} seconds",
            "ru": f"<tg-emoji emoji-id='5778139491810155937'>💎</tg-emoji> <b>Автоматический Депозит</b>\n\n1. Отправьте USDT (BEP-20) на адрес:\n<code>{bep20_addr}</code>\n\n2. Нажмите <b>Я оплатил</b> ниже для проверки!\n\n⏱ <b>Оставшееся время:</b> {remaining} сек.",
            "zh": f"<tg-emoji emoji-id='5778139491810155937'>💎</tg-emoji> <b>自动充值</b>\n\n1. 将 USDT (BEP-20) 发送至此地址：\n<code>{bep20_addr}</code>\n\n2. 发送后，点击下方 <b>我已支付</b>！\n\n⏱ <b>剩余锁定时间：</b> {remaining} 秒"
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), deposit_kb, "wallet_image")
        return

    if data == "w:cancel_lock":
        bg_task(db_set_setting("deposit_lock_user", ""))
        bg_task(db_set_setting("deposit_lock_until", "0"))
        bg_task(db_set_setting("deposit_starting_balance", "0"))
        
        context.user_data[ST] = "WALLET"
        text_map = {
            "en": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Wallet Balance:</b> <code>${bal:.2f}</code>\n\nChoose an option:",
            "hinglish": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Wallet Balance:</b> <code>${bal:.2f}</code>\n\nOption chunein:",
            "ru": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>Баланс Кошелька:</b> <code>${bal:.2f}</code>\n\nВыберите опцию:",
            "zh": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>钱包余额：</b> <code>${bal:.2f}</code>\n\n选择一个选项："
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_wallet(lang), "wallet_image")
        return

    if data == "w:check_auto_deposit":
        now = int(time.time())
        lock_user = await db_get_setting("deposit_lock_user", "")
        lock_until_str = await db_get_setting("deposit_lock_until", "0")
        lock_until = int(lock_until_str) if lock_until_str.isdigit() else 0
        starting_bal_str = await db_get_setting("deposit_starting_balance", "0")
        try:
            starting_bal = float(starting_bal_str)
        except ValueError:
            starting_bal = 0.0
        
        bep20_addr = await db_get_setting("bep20_address", "0x91Cc7f72821FFFb6f205e95AC7cf572Fe3Bab92a")
        
        if lock_user != str(uid) or lock_until <= now:
            bg_task(query.answer("❌ Lock expired. Click Deposit to restart.", show_alert=True))
            return
            
        bg_task(query.answer("⏳ Checking on-chain...", show_alert=True))
        current_bal = await get_usdt_balance(bep20_addr)
        
        if current_bal == -1.0:
            bg_task(query.answer("❌ Blockchain nodes busy. Try again.", show_alert=True))
            return
            
        amt = current_bal - starting_bal
        if amt > 0.01:
            new_bal = await db_add_balance(uid, amt)
            bg_task(db_set_setting("deposit_lock_user", ""))
            bg_task(db_set_setting("deposit_lock_until", "0"))
            
            success_txt = {
                "en": f"✅ <b>Deposit Successful!</b>\n\nAdded: <code>${amt:.2f}</code>\nNew Balance: <code>${new_bal:.2f}</code>",
                "hinglish": f"✅ <b>Deposit Successful!</b>\n\nAdded: <code>${amt:.2f}</code>\nNaya Balance: <code>${new_bal:.2f}</code>",
                "ru": f"✅ <b>Пополнение успешно!</b>\n\nДобавлено: <code>${amt:.2f}</code>\nНовый баланс: <code>${new_bal:.2f}</code>",
                "zh": f"✅ <b>充值成功！</b>\n\n已添加: <code>${amt:.2f}</code>\n新余额: <code>${new_bal:.2f}</code>"
            }
            await _render_menu(update, context, success_txt.get(lang, success_txt["en"]), kb_support_back(lang), "wallet_image")
            try:
                uname_text = f"@{query.from_user.username}" if query.from_user.username else f"ID: {uid}"
                bg_task(context.bot.send_message(ADMIN_ID, f"💰 <b>Auto-Deposit:</b> ${amt:.2f} added to {uname_text}", parse_mode=ParseMode.HTML))
            except Exception: pass
        else:
            remaining = lock_until - now
            bg_task(query.answer(f"❌ No deposits found. ({remaining}s remaining)", show_alert=True))
        return

    if data == "pay:balance":
        lock_key = f"{uid}:{context.user_data.get(ST)}"
        if lock_key in _PROCESSING:
            bg_task(query.answer("<tg-emoji emoji-id='5386367538735104399'>⏳</tg-emoji> Processing...", show_alert=True))
            return
        _PROCESSING.add(lock_key)
        try:
            await _execute_payment(update, context, uid)
        finally:
            _PROCESSING.discard(lock_key)
        return

    if data.startswith("adm:complete:") or data.startswith("adm:cancel:"):
        if uid != ADMIN_ID:
            bg_task(query.answer("⛔ Unauthorized.", show_alert=True))
            return
        parts = data.split(":")
        action, oid, target_uid = parts[1], parts[2], int(parts[3]) 

        new_status = "completed" if action == "complete" else "cancelled"
        try:
            await sb_update("orders", {"status": new_status}, f"order_id=eq.{oid}")
        except Exception:
            bg_task(query.answer("❌ DB error", show_alert=True))
            return

        if action == "cancel":
            orders = await sb_select("orders", f"order_id=eq.{oid}")
            if orders:
                order = orders[0]
                refund = float(order.get("price", 0))
                if refund > 0:
                    bg_task(db_add_balance(target_uid, refund))
                prod = order.get("product_type", "")
                qty = int(order.get("quantity", 0))
                if prod == "boosts":
                    bg_task(db_set_setting("boost_stock", await boost_stock() + qty))
                elif prod == "stars":
                    bg_task(db_set_setting("star_stock", await star_stock() + qty))

        old_text = query.message.text_html or query.message.text or ""
        status_line = "\n\n<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>COMPLETED</b>" if action == "complete" else "\n\n<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>CANCELLED</b> (refunded)"
        
        bg_task(query.edit_message_text(old_text + status_line, parse_mode=ParseMode.HTML))
        target_lang = await db_get_user_lang(target_uid)

        if action == "complete":
            msg_map = {
                "en": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Order Completed!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order ID:</b> <code>{oid}</code>",
                "ru": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Заказ выполнен!</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>ID заказа:</b> <code>{oid}</code>",
                "zh": f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>订单已完成！</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>订单编号：</b> <code>{oid}</code>"
            }
        else:
            msg_map = {
                "en": f"<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>Order Cancelled & Refunded</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order ID:</b> <code>{oid}</code>",
                "ru": f"<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>Заказ отменён и возвращен</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>ID заказа:</b> <code>{oid}</code>",
                "zh": f"<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>订单已取消并退款</b>\n\n<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>订单编号：</b> <code>{oid}</code>"
            }
        
        bg_task(context.bot.send_message(target_uid, msg_map.get(target_lang, msg_map["en"]), parse_mode=ParseMode.HTML, reply_markup=kb_support_back(target_lang)))
        bg_task(query.answer(f"Order {oid} {new_status}!"))
        return


async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    text  = (update.message.text or "").strip()
    state = context.user_data.get(ST, S_MAIN)
    lang  = await db_get_user_lang(uid, context)

    if state == S_PREM_RECIP:
        unames = parse_usernames(text)
        if not unames:
            await _render_menu(update, context, _("err_enter_user", lang), kb_premium_recip(lang), "premium_image")
            return
        if len(unames) > 5:
            await _render_menu(update, context, _("err_max5", lang), kb_premium_recip(lang), "premium_image")
            return
        await _validate_and_set_premium_recip(update, context, unames, lang)
        return

    if state == S_STARS_RECIP:
        unames = parse_usernames(text)
        if not unames:
            await _render_menu(update, context, _("err_enter_user", lang), kb_stars_recip(lang), "stars_image")
            return
        if len(unames) > 5:
            await _render_menu(update, context, _("err_max5", lang), kb_stars_recip(lang), "stars_image")
            return
        valid = []
        for u in unames:
            ok, res = validate_username(u)
            if not ok:
                await _render_menu(update, context, _("err_invalid_user", lang).format(u=u), kb_stars_recip(lang), "stars_image")
                context.user_data[ST] = S_STARS_RECIP
                return
            valid.append(res)

        context.user_data[RECIPIENTS] = valid
        context.user_data[ST] = S_STARS_QTY
        rdisp = "\n".join(f"@{r}" for r in valid)
        text_map = {
            "en": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\nHow many stars would you like?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Type a number and send</b>",
            "hinglish": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\nAapko kitne stars chahiye?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Ek number type karke bhejein</b>",
            "ru": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Продукт: Звёзды</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Получатель(и):</b>\n{rdisp}\n\nСколько звёзд вы хотите?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Введите число и отправьте</b>",
            "zh": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>产品：星星</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>接收者：</b>\n{rdisp}\n\n您想要多少颗星？\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>键入数字并发送</b>"
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_stars_qty(valid, lang), "stars_image")
        return

    if state == S_STARS_QTY:
        try:
            qty = int(text.replace(",", "").replace(" ", ""))
            if qty <= 0: raise ValueError
        except ValueError:
            await _render_menu(update, context, _("err_valid_stars", lang), kb_stars_qty(context.user_data.get(RECIPIENTS, []), lang), "stars_image")
            return
            
        recips = context.user_data.get(RECIPIENTS, [])
        total_stars = qty * len(recips)
        stock = await star_stock()
        if total_stars > stock:
            await _render_menu(update, context, _("err_only_stars", lang).format(stock=stock), kb_stars_qty(recips, lang), "stars_image")
            return

        p = await price_star() * total_stars
        context.user_data[QUANTITY] = qty
        context.user_data[PRICE]    = p
        context.user_data[ST]       = S_STARS_PAY
        rdisp = "\n".join(f"@{r}" for r in recips)
        text_map = {
            "en": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>Quantity:</b> {qty} × {len(recips)}\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${p:.2f}</code>\n\nSelect payment method:",
            "hinglish": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Product: Stars</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient(s):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>Quantity:</b> {qty} × {len(recips)}\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${p:.2f}</code>\n\nPayment ka tarika chunein:",
            "ru": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Продукт: Звёзды</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Получатель(и):</b>\n{rdisp}\n\n<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>Количество:</b> {qty} × {len(recips)}\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Цена:</b> <code>${p:.2f}</code>\n\nВыберите метод оплаты:",
            "zh": f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>产品：星星</b>\n\n<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>接收者：</b>\n{rdisp}\n\n<tg-emoji emoji-id='5438496463044752972'>⭐</tg-emoji> <b>数量：</b> {qty} × {len(recips)}\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>价格：</b> <code>${p:.2f}</code>\n\n选择付款方式："
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_pay(lang), "stars_image")
        return

    if state == S_BOOST_CHAN:
        channel = text
        if not channel:
            await _render_menu(update, context, _("err_channel", lang), kb_boost_input(lang), "boosts_image")
            return
        context.user_data[CHANNEL] = channel
        context.user_data[ST]      = S_BOOST_QTY
        text_map = {
            "en": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product: Boosts</b>\n\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Channel:</b> {channel}\n\nHow many boosts would you like?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Type a number and send</b>",
            "hinglish": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product: Boosts</b>\n\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Channel:</b> {channel}\n\nAapko kitne boosts chahiye?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Number type karke bhejein</b>",
            "ru": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Продукт: Бусты</b>\n\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Канал:</b> {channel}\n\nСколько бустов вы хотите?\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>Введите число и отправьте</b>",
            "zh": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>产品：助力</b>\n\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>频道：</b> {channel}\n\n您想要多少助力？\n\n<tg-emoji emoji-id='5253742260054409879'>📩</tg-emoji> <b>键入数字并发送</b>"
        }
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(_("btn_change_recip", lang), callback_data="m:boosts", api_kwargs={"icon_custom_emoji_id": "5877597667231534929"})],
            [InlineKeyboardButton(_("btn_support", lang), url=support_url, api_kwargs={"icon_custom_emoji_id": "5377858151959770314"})],
        ])
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb, "boosts_image")
        return

    if state == S_BOOST_QTY:
        try:
            qty = int(text.replace(",", "").replace(" ", ""))
            if qty <= 0: raise ValueError
        except ValueError:
            await _render_menu(update, context, _("err_valid_boosts", lang), kb_boost_input(lang), "boosts_image")
            return
            
        stock = await boost_stock()
        if qty > stock:
            await _render_menu(update, context, _("err_only_boosts", lang).format(stock=stock), kb_boost_input(lang), "boosts_image")
            return
            
        channel = context.user_data.get(CHANNEL, "")
        p = await price_boost() * qty
        context.user_data[QUANTITY] = qty
        context.user_data[PRICE]    = p
        context.user_data[ST]       = S_BOOST_PAY
        text_map = {
            "en": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product: Boosts</b>\n\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Channel:</b> {channel}\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Quantity:</b> {qty} boosts\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${p:.2f}</code>\n\nSelect payment method:",
            "hinglish": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Product: Boosts</b>\n\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Channel:</b> {channel}\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Quantity:</b> {qty} boosts\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${p:.2f}</code>\n\nPayment ka tarika chunein:",
            "ru": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>Продукт: Бусты</b>\n\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Канал:</b> {channel}\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Количество:</b> {qty} бустов\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Цена:</b> <code>${p:.2f}</code>\n\nВыберите метод оплаты:",
            "zh": f"<tg-emoji emoji-id='5456140674028019486'>🚀</tg-emoji> <b>产品：助力</b>\n\n<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>频道：</b> {channel}\n<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>数量：</b> {qty} 助力\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>价格：</b> <code>${p:.2f}</code>\n\n选择付款方式："
        }
        await _render_menu(update, context, text_map.get(lang, text_map["en"]), kb_pay(lang), "boosts_image")
        return

def admin_only(func):
    async def _wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            bg_task(update.message.reply_text("<tg-emoji emoji-id='5260293700088511294'>⛔</tg-emoji> Unauthorized.", parse_mode=ParseMode.HTML))
            return
        return await func(update, ctx)
    return _wrap


def create_image_setter(key_name: str, display_name: str):
    @admin_only
    async def _cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        photo = update.message.photo
        if not photo and update.message.reply_to_message and update.message.reply_to_message.photo:
            photo = update.message.reply_to_message.photo

        if photo:
            try:
                fid = photo[-1].file_id
                await db_set_setting(key_name, fid)
                await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> {display_name} updated successfully!", parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Error setting image {key_name}: {e}")
                await update.message.reply_text(f"❌ Database error while saving image: {e}")
        else:
            cmd_used = (update.message.text or update.message.caption or f"/{key_name}").split()[0]
            await update.message.reply_text(
                f"<tg-emoji emoji-id='5884290437459480896'>📸</tg-emoji> Reply to an image with <code>{cmd_used}</code>, or send an image with that caption.", 
                parse_mode=ParseMode.HTML
            )
    return _cmd

@admin_only
async def cmd_setbep20address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setbep20address 0xYourWalletAddress")
        return
    await db_set_setting("bep20_address", context.args[0])
    await update.message.reply_text(f"✅ BEP-20 address set to: <code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)

@admin_only
async def cmd_setbscscanapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setbscscanapi YourAPIKey")
        return
    await db_set_setting("bscscan_api", context.args[0])
    await update.message.reply_text(f"✅ BscScan API Key set successfully!")

@admin_only
async def cmd_setpremium3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await db_set_setting("premium3_price", float(context.args[0]))
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> 3-month premium → ${float(context.args[0]):.2f}", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /setpremium3 9.99")

@admin_only
async def cmd_setpremium6(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await db_set_setting("premium6_price", float(context.args[0]))
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> 6-month premium → ${float(context.args[0]):.2f}", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /setpremium6 17.99")

@admin_only
async def cmd_setpremium12(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await db_set_setting("premium12_price", float(context.args[0]))
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> 12-month premium → ${float(context.args[0]):.2f}", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /setpremium12 29.99")

@admin_only
async def cmd_setstarprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await db_set_setting("star_price", float(context.args[0]))
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> Star price → ${float(context.args[0]):.4f}/star", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /setstarprice 0.02")

@admin_only
async def cmd_setboostprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await db_set_setting("boost_price", float(context.args[0]))
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> Boost price → ${float(context.args[0]):.4f}/boost", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /setboostprice 0.69")

@admin_only
async def cmd_setbooststock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await db_set_setting("boost_stock", int(context.args[0]))
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> Boost stock → {int(context.args[0])}", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /setbooststock 523")

@admin_only
async def cmd_setstarstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await db_set_setting("star_stock", int(context.args[0]))
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> Star stock → {int(context.args[0])}", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /setstarstock 10000")

@admin_only
async def cmd_addbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target = int(context.args[0])
        amt    = float(context.args[1])
        assert amt > 0
        user = await db_get_user(target)
        if not user:
            await update.message.reply_text(f"<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> User {target} not found.", parse_mode=ParseMode.HTML)
            return
        new_bal = await db_add_balance(target, amt)
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> Added ${amt:.2f} to {target}\nNew balance: ${new_bal:.2f}", parse_mode=ParseMode.HTML)
        try:
            target_lang = await db_get_user_lang(target)
            money_msg_map = {
                "en": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>${amt:.2f} added to your wallet!</b>\nNew balance: <code>${new_bal:.2f}</code>",
                "hinglish": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>${amt:.2f} aapke wallet me add ho gaya hai!</b>\nNaya balance: <code>${new_bal:.2f}</code>",
                "ru": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>${amt:.2f} добавлено на ваш кошелёк!</b>\nНовый баланс: <code>${new_bal:.2f}</code>",
                "zh": f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> <b>${amt:.2f} 已加入您的钱包！</b>\n新余额：<code>${new_bal:.2f}</code>"
            }
            bg_task(context.bot.send_message(target, money_msg_map.get(target_lang, money_msg_map["en"]), parse_mode=ParseMode.HTML))
        except Exception:
            pass
    except Exception:
        await update.message.reply_text("Usage: /addbalance userid amount")

@admin_only
async def cmd_removebalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target = int(context.args[0])
        amt    = float(context.args[1])
        assert amt > 0
        new_bal = await db_remove_balance(target, amt)
        await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> Removed ${amt:.2f} from {target}\nNew balance: ${new_bal:.2f}", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /removebalance userid amount")

@admin_only
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target = int(context.args[0])
        user   = await db_get_user(target)
        if not user:
            await update.message.reply_text(f"<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> User {target} not found.", parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(
            f"<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> @{user.get('username','?')} (ID: {target})\n"
            f"<tg-emoji emoji-id='5987583383021034169'>💰</tg-emoji> Balance: ${float(user['balance']):.2f}", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Usage: /balance userid")

@admin_only
async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def fmt(orders, label):
        if not orders:
            return f"<b>{label}:</b> None\n"
        lines = [f"<b>{label} ({len(orders)}):</b>"]
        for o in orders[:10]:
            lines.append(f"  [{o['order_id']}] {o['product_type'].upper()} | {o['recipient']} | ${float(o['price']):.2f}")
        if len(orders) > 10:
            lines.append(f"  … and {len(orders)-10} more")
        return "\n".join(lines) + "\n"
    text = (
        "<tg-emoji emoji-id='5877597667231534929'>📋</tg-emoji> <b>Orders Overview</b>\n\n"
        + fmt(await db_get_orders("pending"),   "<tg-emoji emoji-id='5386367538735104399'>⏳</tg-emoji> Pending")
        + "\n" + fmt(await db_get_orders("completed"), "<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> Completed")
        + "\n" + fmt(await db_get_orders("failed"),    "<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> Failed")
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@admin_only
async def cmd_pendingorders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = await db_get_orders("pending")
    if not orders:
        await update.message.reply_text("<tg-emoji emoji-id='5386367538735104399'>⏳</tg-emoji> <b>Pending Orders:</b> None", parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(f"<tg-emoji emoji-id='5386367538735104399'>⏳</tg-emoji> <b>Pending Orders ({len(orders)})</b>", parse_mode=ParseMode.HTML)
    for o in orders[:20]:
        oid, uid, prod = o['order_id'], o['user_id'], o['product_type'].upper()
        recip, qty, plan, price = o.get('recipient', ''), o.get('quantity', 0), o.get('plan', ''), float(o.get('price', 0))
        dt = o.get('created_at', '').replace('T', ' ')[:16]
        
        text = (
            f"<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <b>Order:</b> <code>{oid}</code>\n"
            f"<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>User:</b> <code>{uid}</code>\n"
            f"<tg-emoji emoji-id='5440539497383087970'>🏆</tg-emoji> <b>Product:</b> {prod}\n"
            f"<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>Recipient:</b> {recip}\n"
            f"<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>Qty:</b> {qty}"
        )
        if plan: text += f" | <b>Plan:</b> {plan}"
        text += (f"\n<tg-emoji emoji-id='5974217466270716579'>💵</tg-emoji> <b>Price:</b> <code>${price:.2f}</code>\n<tg-emoji emoji-id='5778496382117613636'>⏱</tg-emoji> {dt}")
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Complete", callback_data=f"adm:complete:{oid}:{uid}", api_kwargs={"icon_custom_emoji_id": "5985596818912712352"}),
             InlineKeyboardButton("Cancel", callback_data=f"adm:cancel:{oid}:{uid}", api_kwargs={"icon_custom_emoji_id": "5985346521103604145"})],
        ])
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

@admin_only
async def cmd_completedorders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = await db_get_orders("completed")
    if not orders:
        await update.message.reply_text("<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Completed Orders:</b> None", parse_mode=ParseMode.HTML)
        return
    lines = [f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> <b>Completed Orders ({len(orders)})</b>\n"]
    for o in orders[:25]:
        dt = o.get('created_at', '').replace('T', ' ')[:16]
        lines.append(f"<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <code>{o['order_id']}</code> | {o['product_type'].upper()} | {o.get('recipient', '')} | ${float(o.get('price', 0)):.2f} | {dt}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

@admin_only
async def cmd_cancelledorders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = await db_get_orders("cancelled")
    if not orders:
        await update.message.reply_text("<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>Cancelled Orders:</b> None", parse_mode=ParseMode.HTML)
        return
    lines = [f"<tg-emoji emoji-id='5985346521103604145'>❌</tg-emoji> <b>Cancelled Orders ({len(orders)})</b>\n"]
    for o in orders[:25]:
        dt = o.get('created_at', '').replace('T', ' ')[:16]
        lines.append(f"<tg-emoji emoji-id='5222079954421818267'>🆔</tg-emoji> <code>{o['order_id']}</code> | {o['product_type'].upper()} | {o.get('recipient', '')} | ${float(o.get('price', 0)):.2f} | {dt}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /broadcast your message")
        return
    msg  = " ".join(context.args)
    rows = await sb_select("users", limit=5000)
    sent = fail = 0
    for u in rows:
        try:
            await context.bot.send_message(
                u["user_id"],
                f"<tg-emoji emoji-id='5771511103141975115'>📢</tg-emoji> <b>Announcement</b>\n\n{msg}",
                parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            fail += 1
    await update.message.reply_text(f"<tg-emoji emoji-id='5985596818912712352'>✅</tg-emoji> Sent: {sent} | Failed: {fail}", parse_mode=ParseMode.HTML)

@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p3, p6, p12, sp, bp, bs, ss = await asyncio.gather(
        price_premium(3), price_premium(6), price_premium(12),
        price_star(), price_boost(), boost_stock(), star_stock()
    )
    bep20 = await db_get_setting("bep20_address", "0x91Cc7f72821FFFb6f205e95AC7cf572Fe3Bab92a")
    bscapi = await db_get_setting("bscscan_api", "Not Set")
    
    await update.message.reply_text(
        "<tg-emoji emoji-id='5988023995125993550'>🔧</tg-emoji> <b>Admin Panel — All Commands</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<tg-emoji emoji-id='5409048419211682843'>💵</tg-emoji> <b>PRICING COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>/setpremium3 &lt;price&gt;</code>\n  Current: <b>${p3:.2f}</b>\n\n"
        f"<code>/setpremium6 &lt;price&gt;</code>\n  Current: <b>${p6:.2f}</b>\n\n"
        f"<code>/setpremium12 &lt;price&gt;</code>\n  Current: <b>${p12:.2f}</b>\n\n"
        f"<code>/setstarprice &lt;price&gt;</code>\n  Current: <b>${sp:.4f}</b> /star\n\n"
        f"<code>/setboostprice &lt;price&gt;</code>\n  Current: <b>${bp:.4f}</b> /boost\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<tg-emoji emoji-id='5778139491810155937'>💎</tg-emoji> <b>AUTO DEPOSIT (BEP-20)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>/setbep20address &lt;0x...&gt;</code>\n  Current: <code>{bep20}</code>\n\n"
        f"<code>/setbscscanapi &lt;key&gt;</code>\n  Current: <code>{bscapi[:8]}...</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<tg-emoji emoji-id='5884290437459480896'>📸</tg-emoji> <b>IMAGE COMMANDS</b> (Send photo with caption)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<code>/setimage</code> — Main Menu Image\n"
        "<code>/setpremiumimage</code> — Premium Menu Image\n"
        "<code>/setstarsimage</code> — Stars Menu Image\n"
        "<code>/setboostsimage</code> — Boosts Menu Image\n"
        "<code>/setwalletimage</code> — Wallet Menu Image\n"
        "<code>/setpurchasesimage</code> — Purchases Menu Image\n"
        "<code>/setlanguageimage</code> — Language Menu Image\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<tg-emoji emoji-id='5924720918826848520'>📦</tg-emoji> <b>INVENTORY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>/setstarstock &lt;count&gt;</code>\n  Current: <b>{ss}</b> stars\n\n"
        f"<code>/setbooststock &lt;count&gt;</code>\n  Current: <b>{bs}</b> boosts\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<tg-emoji emoji-id='5920344347152224466'>👤</tg-emoji> <b>USER MANAGEMENT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<code>/addbalance &lt;user_id&gt; &lt;amount&gt;</code>\n"
        "<code>/removebalance &lt;user_id&gt; &lt;amount&gt;</code>\n"
        "<code>/balance &lt;user_id&gt;</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<tg-emoji emoji-id='5341715473882955310'>⚙️</tg-emoji> <b>BOT MANAGEMENT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<code>/orders</code>, <code>/pendingorders</code>\n"
        "<code>/completedorders</code>, <code>/cancelledorders</code>\n"
        "<code>/broadcast &lt;message&gt;</code>\n",
        parse_mode=ParseMode.HTML)


def main():
    logger.info("Bot is spinning up... getting ready for lightning speeds.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setbep20address", cmd_setbep20address))
    app.add_handler(CommandHandler("setbscscanapi", cmd_setbscscanapi))
    app.add_handler(CommandHandler("setpremium3",    cmd_setpremium3))
    app.add_handler(CommandHandler("setpremium6",    cmd_setpremium6))
    app.add_handler(CommandHandler("setpremium12",   cmd_setpremium12))
    app.add_handler(CommandHandler("setstarprice",   cmd_setstarprice))
    app.add_handler(CommandHandler("setstarstock",   cmd_setstarstock))
    app.add_handler(CommandHandler("setboostprice",  cmd_setboostprice))
    app.add_handler(CommandHandler("setbooststock",  cmd_setbooststock))
    
    app.add_handler(CommandHandler("setimage", create_image_setter("start_image", "Start image")))
    app.add_handler(CommandHandler("setpremiumimage", create_image_setter("premium_image", "Premium menu image")))
    app.add_handler(CommandHandler("setstarsimage", create_image_setter("stars_image", "Stars menu image")))
    app.add_handler(CommandHandler("setboostsimage", create_image_setter("boosts_image", "Boosts menu image")))
    app.add_handler(CommandHandler("setwalletimage", create_image_setter("wallet_image", "Wallet menu image")))
    app.add_handler(CommandHandler("setpurchasesimage", create_image_setter("purchases_image", "Purchases menu image")))
    app.add_handler(CommandHandler("setlanguageimage", create_image_setter("language_image", "Language menu image")))

    app.add_handler(CommandHandler("addbalance",     cmd_addbalance))
    app.add_handler(CommandHandler("removebalance",  cmd_removebalance))
    app.add_handler(CommandHandler("balance",        cmd_balance))
    app.add_handler(CommandHandler("orders",         cmd_orders))
    app.add_handler(CommandHandler("pendingorders",  cmd_pendingorders))
    app.add_handler(CommandHandler("completedorders",cmd_completedorders))
    app.add_handler(CommandHandler("cancelledorders",cmd_cancelledorders))
    app.add_handler(CommandHandler("broadcast",      cmd_broadcast))
    app.add_handler(CommandHandler("help",            cmd_help))
    app.add_handler(CommandHandler("adminhelp",      cmd_help))

    app.add_handler(CallbackQueryHandler(handle_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_msg))

    logger.info("Bot is running — Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
