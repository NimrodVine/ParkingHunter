"""
ParkingHunter Telegram Bot
==========================
A community-driven parking spot sharing bot for Israel.
Hunters report free blue-white curb spots, seekers get notified when one opens nearby.

All configuration lives in config.py — edit that file to change keys, timings, messages.
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs

import anthropic
import httpx
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    SUPABASE_URL,
    SUPABASE_HEADERS,
    ANTHROPIC_API_KEY,
    MATCH_INTERVAL_SECONDS,
    CLEANUP_INTERVAL_SECONDS,
    SEARCH_RADIUS_METERS,
    NOTIFY_COOLDOWN_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    CLAUDE_MODEL,
    CLAUDE_MAX_TOKENS,
    VALIDATION_PROMPT,
    WEBAPP_PORT,
    WEBAPP_URL,
    # Messages
    MSG_WELCOME,
    MSG_CHOOSE,
    MSG_CANCELLED,
    MSG_POINTS,
    MSG_HUNTER_SEND_LOCATION,
    MSG_HUNTER_SEND_PHOTO,
    MSG_HUNTER_MISSING_LOCATION,
    MSG_HUNTER_PHOTO_ERROR,
    MSG_HUNTER_VALIDATING,
    MSG_HUNTER_INVALID,
    MSG_HUNTER_SPOT_SAVED,
    MSG_HUNTER_SAVE_ERROR,
    MSG_SEEKER_SEND_LOCATION,
    MSG_SEEKER_SEARCHING,
    MSG_SEEKER_GARAGE,
    MSG_SEEKER_SESSION_ERROR,
    MSG_SPOT_FOUND,
    MSG_SPOT_SKIPPED,
    # Button labels
    BTN_HUNTER,
    BTN_SEEKER,
    BTN_POINTS,
    BTN_CANCEL,
    BTN_BACK,
    BTN_NAVIGATE,
    BTN_SKIP,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("parkinghunter")

# ---------------------------------------------------------------------------
# In-memory user state
# ---------------------------------------------------------------------------
# States: None (idle), "hunter_location", "hunter_photo", "seeker_location"
user_state: dict[int, str] = {}
# Temporary data (e.g. hunter's location while waiting for photo)
user_data: dict[int, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
http_client: httpx.AsyncClient = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)


async def sb_request(
    method: str,
    path: str,
    *,
    json: Any = None,
    params: Optional[dict[str, str]] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> httpx.Response:
    """Make an authenticated request to Supabase REST API."""
    headers = {**SUPABASE_HEADERS, **(extra_headers or {})}
    url = f"{SUPABASE_URL}{path}"
    resp = await http_client.request(method, url, json=json, params=params, headers=headers)
    resp.raise_for_status()
    return resp


async def upsert_user(telegram_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    """Create or update user record."""
    await sb_request(
        "POST",
        "/rest/v1/users",
        json={
            "telegram_id": telegram_id,
            "telegram_username": username or "",
            "telegram_first_name": first_name or "",
        },
        extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )


async def get_hunter_points(telegram_id: int) -> int:
    """Return the user's current hunter points."""
    resp = await sb_request(
        "GET",
        "/rest/v1/users",
        params={"telegram_id": f"eq.{telegram_id}", "select": "hunter_points"},
    )
    rows = resp.json()
    return rows[0]["hunter_points"] if rows else 0


async def increment_hunter_points(telegram_id: int) -> int:
    """Atomically add 1 hunter point via Supabase RPC. Returns new total."""
    resp = await sb_request(
        "POST",
        "/rest/v1/rpc/increment_hunter_points",
        json={"p_telegram_id": telegram_id},
    )
    return resp.json() or 0


async def save_spot(telegram_id: int, lat: float, lng: float, photo_url: str) -> dict:
    """Insert a new active parking spot."""
    resp = await sb_request(
        "POST",
        "/rest/v1/spots",
        json={
            "hunter_telegram_id": telegram_id,
            "latitude": lat,
            "longitude": lng,
            "photo_url": photo_url,
            "status": "active",
        },
    )
    return resp.json()[0]


async def get_active_spots() -> list[dict]:
    """Return all active spots."""
    resp = await sb_request("GET", "/rest/v1/spots", params={"status": "eq.active"})
    return resp.json()


async def update_spot(spot_id: int, data: dict) -> None:
    """Patch a spot by id."""
    await sb_request("PATCH", "/rest/v1/spots", params={"id": f"eq.{spot_id}"}, json=data)


async def create_seeker_session(telegram_id: int, lat: float, lng: float) -> dict:
    """Create a new seeker session."""
    resp = await sb_request(
        "POST",
        "/rest/v1/seeker_sessions",
        json={
            "seeker_telegram_id": telegram_id,
            "latitude": lat,
            "longitude": lng,
            "is_active": True,
        },
    )
    return resp.json()[0]


async def update_seeker_location(telegram_id: int, lat: float, lng: float) -> None:
    """Update the active seeker session location."""
    await sb_request(
        "PATCH",
        "/rest/v1/seeker_sessions",
        params={"seeker_telegram_id": f"eq.{telegram_id}", "is_active": "eq.true"},
        json={"latitude": lat, "longitude": lng},
    )


async def find_nearby_seekers(lat: float, lng: float, radius: int = SEARCH_RADIUS_METERS) -> list[dict]:
    """Call the find_nearby_seekers RPC."""
    resp = await sb_request(
        "POST",
        "/rest/v1/rpc/find_nearby_seekers",
        json={"spot_lat": lat, "spot_lng": lng, "radius_meters": radius},
    )
    return resp.json()


async def find_nearest_garage(lat: float, lng: float) -> Optional[dict]:
    """Call the find_nearest_cheap_garage RPC."""
    resp = await sb_request(
        "POST",
        "/rest/v1/rpc/find_nearest_cheap_garage",
        json={"seeker_lat": lat, "seeker_lng": lng, "limit_count": 1},
    )
    rows = resp.json()
    return rows[0] if rows else None


async def cleanup_expired() -> None:
    """Run both cleanup RPCs."""
    try:
        await sb_request("POST", "/rest/v1/rpc/cleanup_expired_spots", json={})
        await sb_request("POST", "/rest/v1/rpc/cleanup_expired_sessions", json={})
    except Exception:
        logger.exception("Cleanup RPC failed")


# ---------------------------------------------------------------------------
# Claude Vision validation
# ---------------------------------------------------------------------------
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


async def validate_photo(photo_url: str) -> tuple[bool, str]:
    """Send the photo to Claude Vision for curb validation (via URL). Returns (is_valid, reason)."""
    try:
        message = await asyncio.to_thread(
            claude_client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": photo_url}},
                        {"type": "text", "text": VALIDATION_PROMPT},
                    ],
                }
            ],
        )
        answer = message.content[0].text.strip()
        if answer.upper().startswith("VALID"):
            return True, "Photo validated"
        reason = answer.replace("INVALID:", "").strip() if "INVALID" in answer.upper() else answer
        return False, reason
    except Exception as e:
        logger.exception("Claude Vision call failed")
        return False, f"Validation error — please try again ({type(e).__name__})"


async def validate_photo_bytes(photo_data: bytes) -> tuple[bool, str]:
    """Send the photo to Claude Vision as base64 (for Mini App). Returns (is_valid, reason)."""
    b64 = base64.standard_b64encode(photo_data).decode("utf-8")
    try:
        message = await asyncio.to_thread(
            claude_client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": VALIDATION_PROMPT},
                    ],
                }
            ],
        )
        answer = message.content[0].text.strip()
        if answer.upper().startswith("VALID"):
            return True, "Photo validated"
        reason = answer.replace("INVALID:", "").strip() if "INVALID" in answer.upper() else answer
        return False, reason
    except Exception as e:
        logger.exception("Claude Vision (base64) call failed")
        return False, f"Validation error — please try again ({type(e).__name__})"


# ---------------------------------------------------------------------------
# Keyboard helpers
# ---------------------------------------------------------------------------
def main_menu_keyboard(points: int = 0) -> InlineKeyboardMarkup:
    """Build the main menu inline keyboard."""
    if WEBAPP_URL:
        hunter_btn = InlineKeyboardButton(BTN_HUNTER, web_app=WebAppInfo(url=f"{WEBAPP_URL}/hunter"))
        seeker_btn = InlineKeyboardButton(BTN_SEEKER, web_app=WebAppInfo(url=f"{WEBAPP_URL}/seeker"))
    else:
        hunter_btn = InlineKeyboardButton(BTN_HUNTER, callback_data="hunter_start")
        seeker_btn = InlineKeyboardButton(BTN_SEEKER, callback_data="seeker_start")
    return InlineKeyboardMarkup(
        [
            [hunter_btn],
            [seeker_btn],
            [InlineKeyboardButton(BTN_POINTS.format(points=points), callback_data="show_points")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_CANCEL, callback_data="cancel")]])


def spot_keyboard(spot_id: int, lat: float, lng: float) -> InlineKeyboardMarkup:
    """Keyboard sent to a seeker when a spot is found."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗺️ נווט אליה ונשמור לך אותה", callback_data=f"take_spot:{spot_id}:{lat}:{lng}")],
            [InlineKeyboardButton("❌ דלג, תן למישהו אחר", callback_data=f"skip_spot:{spot_id}")],
        ]
    )


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — upsert user and show main menu."""
    u = update.effective_user
    if not u:
        return
    try:
        await upsert_user(u.id, u.username, u.first_name)
        points = await get_hunter_points(u.id)
    except Exception:
        logger.exception("Failed to upsert user / get points")
        points = 0

    user_state.pop(u.id, None)
    user_data.pop(u.id, None)

    await update.message.reply_text(  # type: ignore[union-attr]
        MSG_WELCOME,
        reply_markup=main_menu_keyboard(points),
    )


async def send_main_menu(chat_id: int, telegram_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper to send the main menu as a new message."""
    try:
        points = await get_hunter_points(telegram_id)
    except Exception:
        points = 0
    await context.bot.send_message(
        chat_id=chat_id,
        text=MSG_CHOOSE,
        reply_markup=main_menu_keyboard(points),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline-button callbacks."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    u = query.from_user
    data = query.data

    # --- Cancel ---
    if data == "cancel":
        user_state.pop(u.id, None)
        user_data.pop(u.id, None)
        await query.edit_message_text(MSG_CANCELLED)
        await send_main_menu(query.message.chat_id, u.id, context)
        return

    # --- Show points ---
    if data == "show_points":
        try:
            points = await get_hunter_points(u.id)
        except Exception:
            points = 0
        await query.edit_message_text(
            MSG_POINTS.format(points=points),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(BTN_BACK, callback_data="back_menu")]]
            ),
        )
        return

    # --- Back to menu ---
    if data == "back_menu":
        user_state.pop(u.id, None)
        user_data.pop(u.id, None)
        try:
            points = await get_hunter_points(u.id)
        except Exception:
            points = 0
        await query.edit_message_text(MSG_CHOOSE, reply_markup=main_menu_keyboard(points))
        return

    # --- Hunter start ---
    if data == "hunter_start":
        user_state[u.id] = "hunter_location"
        user_data[u.id] = {}
        await query.edit_message_text(
            MSG_HUNTER_SEND_LOCATION,
            reply_markup=cancel_keyboard(),
        )
        return

    # --- Seeker start ---
    if data == "seeker_start":
        user_state[u.id] = "seeker_location"
        user_data[u.id] = {}
        await query.edit_message_text(
            MSG_SEEKER_SEND_LOCATION,
            reply_markup=cancel_keyboard(),
        )
        return

    # --- Skip spot (seeker declines — release it back) ---
    if data and data.startswith("skip_spot"):
        parts = data.split(":")
        if len(parts) > 1:
            try:
                await update_spot(int(parts[1]), {
                    "status": "active",
                    "reserved_by": None,
                })
            except Exception:
                logger.warning("Failed to release spot %s", parts[1])
        await query.edit_message_text("דילגת. ממשיכים לחפש! 🔍")
        return

    # --- Navigate to spot (mark as taken + send Waze link) ---
    if data and data.startswith("take_spot:"):
        parts = data.split(":")
        spot_id_str = parts[1]
        lat = parts[2] if len(parts) > 2 else "0"
        lng = parts[3] if len(parts) > 3 else "0"
        try:
            await update_spot(int(spot_id_str), {
                "status": "taken",
                "reserved_by": u.id,
            })
        except Exception:
            logger.warning("Failed to mark spot %s as taken", spot_id_str)

        waze_url = f"https://waze.com/ul?ll={lat},{lng}&navigate=yes"
        await query.edit_message_text(
            "🔒 החנייה שמורה לך! לחץ לניווט:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🗺️ פתח ניווט ב-Waze", url=waze_url)]]
            ),
        )
        return


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle received location from a new message."""
    msg = update.message
    if not msg or not msg.location:
        return
    u = msg.from_user
    if not u:
        return

    lat = msg.location.latitude
    lng = msg.location.longitude
    state = user_state.get(u.id)

    # --- Hunter: received location ---
    if state == "hunter_location":
        user_data.setdefault(u.id, {})["lat"] = lat
        user_data[u.id]["lng"] = lng
        user_state[u.id] = "hunter_photo"
        await msg.reply_text(
            MSG_HUNTER_SEND_PHOTO,
            reply_markup=cancel_keyboard(),
        )
        return

    # --- Seeker: received location ---
    if state == "seeker_location":
        user_state.pop(u.id, None)
        user_data.pop(u.id, None)
        try:
            await create_seeker_session(u.id, lat, lng)
        except Exception:
            logger.exception("Failed to create seeker session")
            await msg.reply_text(MSG_SEEKER_SESSION_ERROR)
            await send_main_menu(msg.chat_id, u.id, context)
            return

        text = MSG_SEEKER_SEARCHING

        # Try to suggest nearest garage
        try:
            garage = await find_nearest_garage(lat, lng)
            if garage:
                dist_m = int(garage["distance_meters"])
                text += MSG_SEEKER_GARAGE.format(
                    name=garage["name"],
                    price=garage["price_per_hour"],
                    distance=dist_m,
                )
        except Exception:
            logger.debug("Garage lookup failed")

        await msg.reply_text(text)
        await send_main_menu(msg.chat_id, u.id, context)
        return


async def handle_edited_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle live location updates (edited_message with location)."""
    msg = update.edited_message
    if not msg or not msg.location:
        return
    u = msg.from_user
    if not u:
        return

    try:
        await update_seeker_location(u.id, msg.location.latitude, msg.location.longitude)
        logger.info("Updated live location for seeker %s", u.id)
    except Exception:
        logger.debug("Live location update failed for %s (may not be active seeker)", u.id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages (hunter photo validation)."""
    msg = update.message
    if not msg or not msg.photo:
        return
    u = msg.from_user
    if not u:
        return

    if user_state.get(u.id) != "hunter_photo":
        return

    data = user_data.get(u.id, {})
    lat = data.get("lat")
    lng = data.get("lng")
    if lat is None or lng is None:
        user_state.pop(u.id, None)
        await msg.reply_text(MSG_HUNTER_MISSING_LOCATION)
        await send_main_menu(msg.chat_id, u.id, context)
        return

    # Get photo URL from Telegram
    photo = msg.photo[-1]  # highest resolution
    try:
        file = await context.bot.get_file(photo.file_id)
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file.file_path}"
    except Exception:
        logger.exception("Failed to get file from Telegram")
        await msg.reply_text(MSG_HUNTER_PHOTO_ERROR, reply_markup=cancel_keyboard())
        return

    # Validate with Claude Vision
    status_msg = await msg.reply_text(MSG_HUNTER_VALIDATING)
    is_valid, reason = await validate_photo(photo_url)

    if not is_valid:
        await status_msg.edit_text(
            MSG_HUNTER_INVALID.format(reason=reason),
            reply_markup=cancel_keyboard(),
        )
        return

    # Valid — save spot and award point
    try:
        await save_spot(u.id, lat, lng, photo_url)
        new_points = await increment_hunter_points(u.id)
    except Exception:
        logger.exception("Failed to save spot")
        await status_msg.edit_text(MSG_HUNTER_SAVE_ERROR)
        user_state.pop(u.id, None)
        user_data.pop(u.id, None)
        await send_main_menu(msg.chat_id, u.id, context)
        return

    user_state.pop(u.id, None)
    user_data.pop(u.id, None)

    await status_msg.edit_text(MSG_HUNTER_SPOT_SAVED.format(points=new_points))
    await send_main_menu(msg.chat_id, u.id, context)


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------
async def matching_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: match active spots to nearby seekers (1-min stagger)."""
    try:
        spots = await get_active_spots()
    except Exception:
        logger.debug("matching_job: failed to fetch spots")
        return

    now = datetime.now(timezone.utc)

    for spot in spots:
        spot_id: int = spot["id"]
        lat: float = spot["latitude"]
        lng: float = spot["longitude"]
        status: str = spot.get("status", "active")
        notified: list[int] = spot.get("notified_seekers") or []
        idx: int = spot.get("current_notify_index") or 0

        # Skip spots already taken by a seeker
        if status == "taken":
            continue

        # Stagger: wait NOTIFY_COOLDOWN_SECONDS since last notification
        last_notified_str = spot.get("last_notified_at")
        if last_notified_str:
            last_notified = datetime.fromisoformat(last_notified_str.replace("Z", "+00:00"))
            elapsed = (now - last_notified).total_seconds()
            if elapsed < NOTIFY_COOLDOWN_SECONDS:
                continue  # too soon, skip this spot for now

        try:
            seekers = await find_nearby_seekers(lat, lng)
        except Exception:
            continue

        # Filter out already-notified seekers
        candidates = [s for s in seekers if s["seeker_telegram_id"] not in notified]
        if not candidates:
            continue

        # Pick the next one (closest not yet notified)
        seeker = candidates[0]
        seeker_tid: int = seeker["seeker_telegram_id"]
        distance_m: float = seeker["distance_meters"]

        # Send notification with photo if available
        photo_url = spot.get("photo_url", "")
        try:
            if photo_url:
                await context.bot.send_photo(
                    chat_id=seeker_tid,
                    photo=photo_url,
                    caption=MSG_SPOT_FOUND.format(distance=int(distance_m)),
                    reply_markup=spot_keyboard(spot_id, lat, lng),
                )
            else:
                await context.bot.send_message(
                    chat_id=seeker_tid,
                    text=MSG_SPOT_FOUND.format(distance=int(distance_m)),
                    reply_markup=spot_keyboard(spot_id, lat, lng),
                )
        except Exception:
            logger.warning("Could not notify seeker %s", seeker_tid)
            continue

        # Update spot tracking (spot stays active until seeker clicks navigate)
        notified.append(seeker_tid)
        try:
            await update_spot(spot_id, {
                "notified_seekers": notified,
                "current_notify_index": idx + 1,
                "last_notified_at": now.isoformat(),
            })
        except Exception:
            logger.warning("Failed to update spot %s notified list", spot_id)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: remove expired spots and sessions."""
    await cleanup_expired()
    logger.info("Cleanup job ran")


# ---------------------------------------------------------------------------
# Telegram WebApp init-data verification
# ---------------------------------------------------------------------------
def verify_telegram_init_data(init_data: str) -> Optional[dict]:
    """
    Verify that the init data was signed by Telegram.
    Returns the parsed user dict or None if invalid.
    See: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    try:
        parsed = parse_qs(init_data)
        check_hash = parsed.get("hash", [None])[0]
        if not check_hash:
            return None

        # Build the data-check-string (all params except hash, sorted)
        data_pairs = []
        for key, values in parsed.items():
            if key == "hash":
                continue
            data_pairs.append(f"{key}={values[0]}")
        data_pairs.sort()
        data_check_string = "\n".join(data_pairs)

        # HMAC-SHA256
        secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if computed_hash != check_hash:
            logger.warning("Telegram init data hash mismatch")
            return None

        # Extract user info
        user_json = parsed.get("user", [None])[0]
        if user_json:
            return json.loads(user_json)
        return None
    except Exception:
        logger.exception("Failed to verify Telegram init data")
        return None


# ---------------------------------------------------------------------------
# Web server (serves Mini App + API)
# ---------------------------------------------------------------------------
WEBAPP_DIR = Path(__file__).parent / "webapp"


async def handle_hunter_page(request: web.Request) -> web.Response:
    """Serve the hunter Mini App HTML."""
    html_path = WEBAPP_DIR / "hunter.html"
    if not html_path.exists():
        return web.Response(text="Mini App not found", status=404)
    return web.FileResponse(html_path)


async def handle_submit_spot(request: web.Request) -> web.Response:
    """
    API endpoint: receive photo + location from the Mini App.
    Validates the photo with Claude (base64), saves to Supabase, awards points.
    """
    # Verify Telegram auth
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = verify_telegram_init_data(init_data)
    if not user_info:
        return web.json_response({"success": False, "reason": "🔐 לא הצלחנו לזהות אותך. נסה שוב מהתפריט."}, status=401)

    telegram_id = user_info["id"]
    username = user_info.get("username", "")
    first_name = user_info.get("first_name", "")

    # Upsert user (required — spots have foreign key to users)
    try:
        await upsert_user(telegram_id, username, first_name)
    except Exception:
        logger.exception("upsert_user failed for %s", telegram_id)
        return web.json_response({"success": False, "reason": "😅 לא הצלחנו ליצור משתמש. נסה שוב."}, status=500)

    # Parse multipart form
    try:
        reader = await request.multipart()
        photo_data = None
        lat = None
        lng = None

        async for part in reader:
            if part.name == "photo":
                photo_data = await part.read()
            elif part.name == "latitude":
                lat = float((await part.read()).decode())
            elif part.name == "longitude":
                lng = float((await part.read()).decode())

        if not photo_data or lat is None or lng is None:
            return web.json_response({"success": False, "reason": "📸 חסרים נתונים. נסה לצלם שוב."}, status=400)
    except Exception:
        logger.exception("Failed to parse Mini App submission")
        return web.json_response({"success": False, "reason": "📸 משהו השתבש עם התמונה. נסה שוב."}, status=400)

    # Validate with Claude Vision (send base64 directly — no Telegram upload needed)
    is_valid, reason = await validate_photo_bytes(photo_data)
    if not is_valid:
        # Send rejection to chat too
        try:
            await http_client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": telegram_id, "text": f"❌ {reason}"},
            )
        except Exception:
            pass
        return web.json_response({"success": False, "reason": reason})

    # Photo approved! Now upload to Telegram to get a URL for storage
    photo_url = ""
    try:
        upload_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        upload_resp = await http_client.post(
            upload_url,
            files={"photo": ("spot.jpg", photo_data, "image/jpeg")},
            data={"chat_id": str(telegram_id), "caption": "✅ חנייה אושרה!"},
        )
        upload_result = upload_resp.json()
        if upload_result.get("ok"):
            photos = upload_result["result"]["photo"]
            file_id = photos[-1]["file_id"]
            file_resp = await http_client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            file_result = file_resp.json()
            file_path = file_result["result"]["file_path"]
            photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    except Exception:
        logger.warning("Failed to upload approved photo to Telegram (saving without URL)")

    # Save spot + award points
    try:
        await save_spot(telegram_id, lat, lng, photo_url)
        new_points = await increment_hunter_points(telegram_id)
    except Exception:
        logger.exception("Failed to save spot from Mini App")
        return web.json_response({"success": False, "reason": "😅 החנייה אושרה אבל לא הצלחנו לשמור. נסה שוב?"}, status=500)

    # Send success message in chat
    try:
        await http_client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": telegram_id,
                "text": MSG_HUNTER_SPOT_SAVED.format(points=new_points),
            },
        )
    except Exception:
        pass

    return web.json_response({"success": True, "points": new_points})


async def handle_seeker_page(request: web.Request) -> web.Response:
    """Serve the seeker Mini App HTML."""
    html_path = WEBAPP_DIR / "seeker.html"
    if not html_path.exists():
        return web.Response(text="Mini App not found", status=404)
    return web.FileResponse(html_path)


async def handle_start_search(request: web.Request) -> web.Response:
    """
    API endpoint: receive location from the seeker Mini App.
    Creates a seeker session and returns nearest garage.
    """
    # Verify Telegram auth
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = verify_telegram_init_data(init_data)
    if not user_info:
        return web.json_response({"success": False, "reason": "Unauthorized"}, status=401)

    telegram_id = user_info["id"]
    username = user_info.get("username", "")
    first_name = user_info.get("first_name", "")

    # Upsert user
    try:
        await upsert_user(telegram_id, username, first_name)
    except Exception:
        logger.exception("Failed to upsert user from seeker Mini App")

    # Parse JSON body
    try:
        body = await request.json()
        lat = float(body["latitude"])
        lng = float(body["longitude"])
    except Exception:
        return web.json_response({"success": False, "reason": "Missing location"}, status=400)

    # Create seeker session
    try:
        await create_seeker_session(telegram_id, lat, lng)
    except Exception:
        logger.exception("Failed to create seeker session from Mini App")
        return web.json_response({"success": False, "reason": "Failed to start search"}, status=500)

    # Find nearest garage as fallback
    garage_data = None
    try:
        garage = await find_nearest_garage(lat, lng)
        if garage:
            garage_data = {
                "name": garage["name"],
                "price_per_hour": float(garage["price_per_hour"]),
                "distance_meters": float(garage["distance_meters"]),
            }
    except Exception:
        logger.debug("Garage lookup failed in seeker Mini App")

    # Send confirmation message in chat
    try:
        text = MSG_SEEKER_SEARCHING
        if garage_data:
            text += MSG_SEEKER_GARAGE.format(
                name=garage_data["name"],
                price=garage_data["price_per_hour"],
                distance=int(garage_data["distance_meters"]),
            )
        await http_client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": telegram_id, "text": text},
        )
    except Exception:
        pass

    return web.json_response({
        "success": True,
        "garage": garage_data,
    })


def create_web_app() -> web.Application:
    """Create the aiohttp web application."""
    webapp = web.Application()
    webapp.router.add_get("/hunter", handle_hunter_page)
    webapp.router.add_get("/seeker", handle_seeker_page)
    webapp.router.add_post("/api/submit-spot", handle_submit_spot)
    webapp.router.add_post("/api/start-search", handle_start_search)
    # Serve static files from webapp/ directory
    if WEBAPP_DIR.exists():
        webapp.router.add_static("/static/", WEBAPP_DIR)
    return webapp


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------
def main() -> None:
    """Build and run the bot + web server."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Live location updates come as edited_message — handle via dedicated handler
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edited_location))

    # Background jobs
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(matching_job, interval=MATCH_INTERVAL_SECONDS, first=MATCH_INTERVAL_SECONDS)
        job_queue.run_repeating(cleanup_job, interval=CLEANUP_INTERVAL_SECONDS, first=CLEANUP_INTERVAL_SECONDS)

    if WEBAPP_URL:
        # Run bot + web server together
        logger.info("Starting bot (polling) + web server on port %s…", WEBAPP_PORT)

        async def run_all():
            # Start bot
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            # Start web server
            webapp = create_web_app()
            runner = web.AppRunner(webapp)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
            await site.start()
            logger.info("Web server running on port %s", WEBAPP_PORT)

            # Keep running until interrupted
            try:
                await asyncio.Event().wait()
            finally:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
                await runner.cleanup()

        asyncio.run(run_all())
    else:
        # No WEBAPP_URL set — run bot only (original behavior)
        logger.info("ParkingHunter bot starting (polling mode, no web server)…")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
