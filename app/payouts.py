"""Server-owned payout and pricing tables. The client only names an outcome
(game + event); coins amounts, multipliers, and caps all live here.

The per-game event tables are placeholders sized to the spec's economy
(wheel jackpot 25,000 base) — align event names with the client's games
before launch.
"""

from datetime import timedelta
from decimal import Decimal

# game -> event -> base coins
GAME_PAYOUTS: dict[str, dict[str, int]] = {
    "wheel": {
        "seg_50": 50,
        "seg_100": 100,
        "seg_250": 250,
        "seg_500": 500,
        "seg_1000": 1_000,
        "seg_2500": 2_500,
        "seg_5000": 5_000,
        "jackpot": 25_000,
    },
    "scratch": {
        "win_small": 250,
        "win_medium": 1_000,
        "win_big": 5_000,
    },
    "slots": {
        "two_kind": 150,
        "three_kind": 1_000,
        "jackpot": 10_000,
    },
    "solitaire": {
        "win": 500,
        "fast_win": 800,
    },
}

# Defense-in-depth clamp on the base payout per claim, per game.
MAX_SINGLE_WIN_BASE: dict[str, int] = {
    "wheel": 25_000,
    "scratch": 5_000,
    "slots": 10_000,
    "solitaire": 1_000,
}

# Check-in rewards by streak day; day 7+ pays the last entry.
CHECKIN_REWARDS: list[int] = [100, 200, 300, 400, 500, 750, 1_000]


def checkin_reward(streak: int) -> int:
    idx = min(max(streak, 1), len(CHECKIN_REWARDS)) - 1
    return CHECKIN_REWARDS[idx]


# IAP products (granted by RevenueCat/Stripe webhooks, never by the client).
GOLD_PRODUCT_ID = "gold"
BOOST_PRODUCTS: dict[str, timedelta] = {
    "boost_1h": timedelta(hours=1),
    "boost_24h": timedelta(hours=24),
}


# Redemption catalog: sku -> (usd value, coin cost). 10,000 coins ≈ $1.
REDEMPTION_CATALOG: dict[str, dict] = {
    "amazon_5": {"usd": Decimal("5.00"), "coins": 50_000, "label": "Amazon $5"},
    "amazon_10": {"usd": Decimal("10.00"), "coins": 100_000, "label": "Amazon $10"},
    "amazon_25": {"usd": Decimal("25.00"), "coins": 250_000, "label": "Amazon $25"},
    "visa_10": {"usd": Decimal("10.00"), "coins": 100_000, "label": "Visa $10"},
}
