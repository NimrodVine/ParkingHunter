"""
ParkingHunter Configuration
============================
All configurable values in one place.
Update your API keys, timeouts, distances, and messages here.
"""

import os

# =============================================================================
# API KEYS & CREDENTIALS
# =============================================================================
# Set these as environment variables (e.g. in Railway dashboard)
# Never hardcode real keys in this file — use env vars.

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL: str = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]

# =============================================================================
# SUPABASE
# =============================================================================
SUPABASE_HEADERS: dict[str, str] = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# =============================================================================
# TIMING & INTERVALS (seconds)
# =============================================================================
MATCH_INTERVAL_SECONDS: int = 10       # How often to check for spot-seeker matches
CLEANUP_INTERVAL_SECONDS: int = 60     # How often to clean up expired spots/sessions
NOTIFY_COOLDOWN_SECONDS: int = 60      # Wait time between notifying each seeker
HTTP_TIMEOUT_SECONDS: int = 15         # Timeout for Supabase API calls

# =============================================================================
# DISTANCES (meters)
# =============================================================================
SEARCH_RADIUS_METERS: int = 1000       # Max distance to match seeker to spot (1km)

# =============================================================================
# WEB APP
# =============================================================================
WEBAPP_PORT: int = int(os.environ.get("PORT", "8080"))
WEBAPP_URL: str = os.environ.get("WEBAPP_URL", "")  # e.g. https://parkinghunter.up.railway.app

# =============================================================================
# AI VALIDATION
# =============================================================================
CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS: int = 256

VALIDATION_PROMPT: str = (
    "You are validating a photo for a parking app in Israel.\n\n"
    "A VALID photo must meet ALL 3 conditions:\n"
    "1. Blue-white painted curb is visible somewhere in the photo "
    "(alternating blue and white stripes on the curbstones — this is standard Israeli paid parking)\n"
    "2. There is an EMPTY SPACE along the blue-white curb where a car could fit. "
    "This could be a gap between two parked cars, or empty curb at the end of a row. "
    "Even if there are parked cars nearby, as long as you can see empty curb space — it's VALID. "
    "The key question: could a car physically park in the empty space shown? If yes → VALID.\n"
    "3. The photo is a real outdoor street photo (not a screenshot, not indoors)\n\n"
    "IMPORTANT: Be LENIENT. If you see blue-white curb AND any empty space — approve it. "
    "The hunter is standing next to the spot. Trust them. Only reject if it's clearly wrong.\n\n"
    "Reject (INVALID) only if:\n"
    "- The curb is red-white or red-yellow (no parking zones)\n"
    "- There is literally NO empty space at all — every centimeter has a car on it\n"
    "- The photo is not a real street scene (screenshot, indoors, etc.)\n"
    "- No blue-white curb is visible at all\n\n"
    "Respond with EXACTLY one line:\n"
    "VALID — if the photo looks like a real available parking spot\n"
    "INVALID: <funny short reason in Hebrew with emoji> — use humor! Examples:\n"
    "  - Red curb: 'אדום-לבן = אסור חניה. גם השוטר לא ילך על זה 🚔'\n"
    "  - All occupied: 'פה צפוף כמו קופסת סרדינים. אין מקום אפילו לאופניים 🐟'\n"
    "  - Not a street: 'זה לא נראה כמו רחוב... ניסיון יפה אבל לא 😅'\n"
    "  - No blue-white: 'איפה הכחול-לבן? בלי מדרכה צבועה אין עסק 🎨'\n"
    "  - Screenshot: 'זו תמונת מסך! צריך לצלם בחוץ, עם שמש ורוח 🌞'\n"
    "Be creative with the humor but keep it short (one sentence + emoji).\n"
    "Do not add any other text."
)

# =============================================================================
# BOT MESSAGES (Hebrew)
# =============================================================================
MSG_WELCOME = "🚗 ברוכים הבאים ל-ParkingHunter!\nבחרו אפשרות:"
MSG_CHOOSE = "בחרו אפשרות:"
MSG_CANCELLED = "❌ בוטל."
MSG_POINTS = "📊 יש לך {points} נקודות ציד."
MSG_HUNTER_SEND_LOCATION = "📍 שתפו את המיקום שלכם.\n(לחצו 📎 ← מיקום ← שלח מיקום נוכחי)"
MSG_HUNTER_SEND_PHOTO = "📸 עכשיו שלחו תמונה של המדרכה הכחול-לבן."
MSG_HUNTER_MISSING_LOCATION = "⚠️ חסר מיקום. התחילו מחדש."
MSG_HUNTER_PHOTO_ERROR = "⚠️ �