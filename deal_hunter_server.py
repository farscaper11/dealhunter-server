import json
import os
import re
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib import request

from playwright.sync_api import sync_playwright

from retailers import best_buy, bh_photo


APPLE_CATALOG_URL = "https://www.apple.com/shop/refurbished/mac"

INTERVAL_MINUTES = max(
    5,
    int(os.getenv("DH_SCAN_INTERVAL_MINUTES", "15")),
)

FULL_REVERIFY_HOURS = max(
    1,
    int(os.getenv("DH_FULL_REVERIFY_HOURS", "6")),
)

ENABLE_BH = os.getenv(
    "DH_ENABLE_BH",
    "false",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DATA_DIR = Path(os.getenv("DH_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

STATE_FILE = DATA_DIR / "alert-state.json"
RESULTS_FILE = DATA_DIR / "last-results.json"
SCAN_FILE = DATA_DIR / "last-scan.txt"
CACHE_FILE = DATA_DIR / "product-cache.json"
PRICE_HISTORY_FILE = DATA_DIR / "price-history.json"

REFERENCE_PRICE = max(
    1.0,
    float(os.getenv("DH_REFERENCE_PRICE", "1999")),
)

HISTORY_MAX_POINTS = max(
    10,
    int(os.getenv("DH_HISTORY_MAX_POINTS", "200")),
)

ALERT_MIN_SCORE = min(
    100,
    max(
        0,
        int(os.getenv("DH_ALERT_MIN_SCORE", "70")),
    ),
)

ALERT_SCORE_IMPROVEMENT = max(
    1,
    int(os.getenv("DH_ALERT_SCORE_IMPROVEMENT", "5")),
)

TARGET = {
    "screen": 15.0,
    "chip": "M5",
    "memory": 24,
    "storage": 1024,
    "colors": {
        "Midnight",
        "Starlight",
        "Silver",
        "Sky Blue",
    },
}

COLOR_ORDER = {
    "Midnight": 0,
    "Starlight": 1,
    "Silver": 2,
    "Sky Blue": 3,
}

RETAILER_ORDER = {
    "Apple Certified Refurbished": 0,
    "Best Buy": 1,
    "B&H Photo": 2,
}

GROUP_ALERT_VERSION = 3

USER_AGENT = (
    "DealHunter/0.8.1 "
    "(+https://github.com/farscaper11/dealhunter-server)"
)


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canonical_url(url: str) -> str:
    return (url or "").split("?", 1)[0]


def catalog_fingerprint(text: str) -> str:
    normalized = compact(text).lower()
    return sha256(normalized.encode("utf-8")).hexdigest()


def retailer_for_url(url: str) -> str:
    lowered = (url or "").lower()

    if "bestbuy.com" in lowered:
        return "Best Buy"

    if "bhphotovideo.com" in lowered:
        return "B&H Photo"

    if "apple.com" in lowered:
        return "Apple Certified Refurbished"

    return ""


def load_cache() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2),
        encoding="utf-8",
    )


def load_price_history() -> dict:
    try:
        data = json.loads(
            PRICE_HISTORY_FILE.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_price_history(history: dict) -> None:
    PRICE_HISTORY_FILE.write_text(
        json.dumps(history, indent=2),
        encoding="utf-8",
    )


def deal_tier(score: int) -> str:
    if score >= 90:
        return "Exceptional"
    if score >= 80:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 55:
        return "Fair"
    return "Wait"


def record_price_history(
    products: list[dict],
    history: dict,
    checked_at: str,
) -> int:
    points_added = 0

    for product in products:
        url = canonical_url(product.get("url", ""))
        price = product.get("price")

        if not url or price is None:
            continue

        try:
            price = float(price)
        except (TypeError, ValueError):
            continue

        entry = history.get(url)
        if not isinstance(entry, dict):
            entry = {}

        points = entry.get("points", [])
        if not isinstance(points, list):
            points = []

        availability = product.get("availability", "Unknown")

        last_point = points[-1] if points else {}
        price_changed = (
            not points
            or last_point.get("price") != price
        )
        availability_changed = (
            not points
            or last_point.get("availability") != availability
        )

        if price_changed or availability_changed:
            points.append(
                {
                    "checked_at": checked_at,
                    "price": price,
                    "availability": availability,
                }
            )
            points_added += 1

        points = points[-HISTORY_MAX_POINTS:]

        previous_low = entry.get("low_price")
        previous_high = entry.get("high_price")

        numeric_low = (
            float(previous_low)
            if isinstance(previous_low, (int, float))
            else price
        )
        numeric_high = (
            float(previous_high)
            if isinstance(previous_high, (int, float))
            else price
        )

        try:
            checks = int(entry.get("checks", 0)) + 1
        except (TypeError, ValueError):
            checks = 1

        history[url] = {
            "retailer": product.get("retailer"),
            "condition": product.get("condition"),
            "title": product.get("title"),
            "color": product.get("color"),
            "first_seen": entry.get("first_seen") or checked_at,
            "last_seen": checked_at,
            "checks": checks,
            "low_price": min(numeric_low, price),
            "high_price": max(numeric_high, price),
            "latest_price": price,
            "latest_availability": availability,
            "points": points,
        }

    return points_added


def apply_deal_metrics(
    products: list[dict],
    history: dict,
) -> None:
    for product in products:
        url = canonical_url(product.get("url", ""))
        entry = history.get(url, {})

        if not isinstance(entry, dict):
            entry = {}

        try:
            price = float(product["price"])
        except (KeyError, TypeError, ValueError):
            continue

        low_price = entry.get("low_price", price)
        high_price = entry.get("high_price", price)

        try:
            low_price = float(low_price)
        except (TypeError, ValueError):
            low_price = price

        try:
            high_price = float(high_price)
        except (TypeError, ValueError):
            high_price = price

        try:
            checks = int(entry.get("checks", 1))
        except (TypeError, ValueError):
            checks = 1

        points = entry.get("points", [])
        if not isinstance(points, list):
            points = []

        distinct_prices = {
            point.get("price")
            for point in points
            if isinstance(point, dict)
            and isinstance(point.get("price"), (int, float))
        }

        savings_amount = max(0.0, REFERENCE_PRICE - price)
        savings_percent = (
            savings_amount / REFERENCE_PRICE * 100
        )

        savings_points = min(
            40.0,
            max(0.0, savings_percent * 2.0),
        )

        if len(distinct_prices) < 2:
            history_points = 15.0
            history_label = "Building history"
        else:
            above_low_percent = (
                max(0.0, price - low_price)
                / max(low_price, 1.0)
                * 100
            )

            if above_low_percent <= 0.01:
                history_points = 30.0
                history_label = "Historical low"
            elif above_low_percent <= 2:
                history_points = 24.0
                history_label = "Within 2% of low"
            elif above_low_percent <= 5:
                history_points = 16.0
                history_label = "Within 5% of low"
            elif above_low_percent <= 10:
                history_points = 8.0
                history_label = "Within 10% of low"
            else:
                history_points = 0.0
                history_label = "Above recent low"

        availability = product.get("availability")

        if availability == "Likely in stock":
            availability_points = 20.0
        elif availability == "Unknown":
            availability_points = 8.0
        else:
            availability_points = 0.0

        verification_points = (
            10.0 if product.get("exact_match") else 0.0
        )

        score = round(
            min(
                100.0,
                savings_points
                + history_points
                + availability_points
                + verification_points,
            )
        )

        product.update(
            {
                "reference_price": REFERENCE_PRICE,
                "savings_amount": round(savings_amount, 2),
                "savings_percent": round(savings_percent, 1),
                "historical_low": round(low_price, 2),
                "historical_high": round(high_price, 2),
                "history_checks": checks,
                "history_label": history_label,
                "deal_score": score,
                "deal_tier": deal_tier(score),
            }
        )


def normalize_state_urls(state: dict) -> None:
    for key in list(state):
        if not key.startswith("http") or "?" not in key:
            continue

        clean_key = canonical_url(key)

        if clean_key not in state:
            state[clean_key] = state[key]

        del state[key]


def product_information(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    patterns = [
        (
            r"Product Information\s+Overview\s+"
            r"(.*?)"
            r"(?:Apple Certified Refurbished Products|"
            r"What(?:’|'|’)s in the Box|$)"
        ),
        (
            r"(?:^|\n)Overview\s+"
            r"(.*?)"
            r"(?:Apple Certified Refurbished Products|"
            r"What(?:’|'|’)s in the Box|$)"
        ),
    ]

    for pattern in patterns:
        match = re.search(pattern, normalized, re.I | re.S)
        if match:
            return match.group(1)[:5000]

    return ""


def detect_screen(text: str) -> float | None:
    match = re.search(
        r"\b(13(?:\.3|\.6)?|14(?:\.2)?|15(?:\.3)?|16(?:\.2)?)"
        r"\s*(?:-| )?inch\b",
        text or "",
        re.I,
    )
    return float(match.group(1)) if match else None


def detect_chip(text: str) -> str:
    match = re.search(
        r"\b(M[1-9](?:\s+(?:Pro|Max|Ultra))?)\b",
        text or "",
        re.I,
    )
    return compact(match.group(1)).upper() if match else ""


def detect_memory(text: str) -> int | None:
    match = re.search(
        r"\b(8|16|18|24|32|36|48|64|96|128)"
        r"\s*GB\s+unified memory\b",
        text or "",
        re.I,
    )
    return int(match.group(1)) if match else None


def detect_storage(text: str) -> int | None:
    match = re.search(
        r"\b(1|2|4)\s*TB\s*SSD",
        text or "",
        re.I,
    )
    if match:
        return int(match.group(1)) * 1024

    match = re.search(
        r"\b(256|512|1024|2048|4096)\s*GB\s*SSD",
        text or "",
        re.I,
    )
    return int(match.group(1)) if match else None


def detect_color(text: str) -> str:
    for color in (
        "Midnight",
        "Starlight",
        "Silver",
        "Sky Blue",
        "Space Gray",
        "Space Black",
    ):
        if color.lower() in (text or "").lower():
            return color

    return ""


def extract_structured_product(page) -> dict:
    products = page.evaluate(
        """
        () => {
          const found = [];

          const walk = value => {
            if (!value) return;

            if (Array.isArray(value)) {
              value.forEach(walk);
              return;
            }

            if (typeof value !== "object") return;

            const rawType = value["@type"];
            const types = Array.isArray(rawType)
              ? rawType
              : [rawType];

            if (
              types.some(
                item => String(item).toLowerCase() === "product"
              )
            ) {
              found.push(value);
            }

            Object.values(value).forEach(walk);
          };

          for (
            const script of document.querySelectorAll(
              'script[type="application/ld+json"]'
            )
          ) {
            try {
              walk(JSON.parse(script.textContent));
            } catch (_) {}
          }

          return found;
        }
        """
    )

    for product in products:
        combined = " ".join(
            str(product.get(key, ""))
            for key in (
                "name",
                "description",
                "model",
                "sku",
                "mpn",
            )
        )

        if "macbook air" in combined.lower():
            return product

    return {}


def extract_price(product: dict, visible_text: str) -> float | None:
    prices: list[float] = []

    offers = product.get("offers", [])
    if not isinstance(offers, list):
        offers = [offers]

    for offer in offers:
        if not isinstance(offer, dict):
            continue

        for key in ("price", "lowPrice"):
            raw_value = offer.get(key)

            try:
                value = float(
                    str(raw_value)
                    .replace("$", "")
                    .replace(",", "")
                )
            except (TypeError, ValueError):
                continue

            if 1000 <= value <= 5000:
                prices.append(value)

    if prices:
        unique = sorted(set(prices))
        if len(unique) == 1:
            return unique[0]

    fallback_prices = []

    for raw in re.findall(
        r"\$\s*([0-9]{1,2}(?:,[0-9]{3})(?:\.[0-9]{2})?)",
        visible_text[:12000],
    ):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue

        if 1000 <= value <= 5000:
            fallback_prices.append(value)

    unique = sorted(set(fallback_prices))

    return unique[0] if len(unique) == 1 else None


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


def numeric_score(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def should_alert(product: dict, state: dict) -> str | None:
    previous = state.get(product["url"])
    current_score = numeric_score(product.get("deal_score"))
    current_price = product.get("price")
    current_availability = product.get(
        "availability",
        "Unknown",
    )

    if previous is None:
        if (
            current_availability == "Likely in stock"
            and current_score >= ALERT_MIN_SCORE
        ):
            return (
                f"New {product.get('deal_tier', 'qualified')} "
                f"deal ({current_score}/100)"
            )

        return None

    previous_price = previous.get("price")

    if (
        previous_price is not None
        and current_price is not None
        and current_price < previous_price
    ):
        return f"Price dropped from ${previous_price:,.2f}"

    if (
        previous.get("availability") != "Likely in stock"
        and current_availability == "Likely in stock"
    ):
        return "Back in stock"

    previous_score = numeric_score(
        previous.get("deal_score")
    )

    if (
        current_availability == "Likely in stock"
        and current_score >= ALERT_MIN_SCORE
    ):
        if previous_score < ALERT_MIN_SCORE:
            return (
                f"Deal score reached {current_score}/100 "
                f"{product.get('deal_tier', '')}".strip()
            )

        if (
            current_score - previous_score
            >= ALERT_SCORE_IMPROVEMENT
        ):
            return (
                f"Deal score improved from "
                f"{previous_score}/100 to {current_score}/100"
            )

    return None


def availability_icon(availability: str) -> str:
    if availability == "Likely in stock":
        return "✅"
    if availability == "Out of stock":
        return "❌"
    return "⚠️"


def send_discord_group(
    products: list[dict],
    triggered: list[tuple[dict, str]],
) -> None:
    if not WEBHOOK_URL:
        log("Discord webhook is not configured.")
        return

    triggered_retailers = {
        product.get("retailer", "Unknown retailer")
        for product, _ in triggered
    }

    grouped: dict[str, list[dict]] = {}

    for product in products:
        retailer = product.get("retailer", "Unknown retailer")

        if (
            triggered_retailers
            and retailer not in triggered_retailers
        ):
            continue

        grouped.setdefault(retailer, []).append(product)

    embeds = []

    for retailer in sorted(
        grouped,
        key=lambda name: RETAILER_ORDER.get(name, 99),
    ):
        retailer_products = sorted(
            grouped[retailer],
            key=lambda item: (
                -item.get("deal_score", 0),
                item["price"],
                COLOR_ORDER.get(item["color"], 99),
            ),
        )

        best = retailer_products[0]
        best_price = min(
            product["price"]
            for product in retailer_products
        )

        listing_lines = []

        for product in retailer_products:
            listing_lines.append(
                f"{availability_icon(product['availability'])} "
                f"[{product['color']} — "
                f"${product['price']:,.0f} • "
                f"{product.get('deal_score', 0)}/100 "
                f"{product.get('deal_tier', '')}]"
                f"({canonical_url(product['url'])})"
            )

        reason_lines = []
        seen_reasons = set()

        for product, reason in triggered:
            if product.get("retailer") != retailer:
                continue

            if reason in {
                "Deal scoring enabled",
                "Relevance gate enabled",
            }:
                reason_text = reason
            else:
                reason_text = f"{reason}: {product['color']}"

            if reason_text in seen_reasons:
                continue

            seen_reasons.add(reason_text)
            reason_lines.append(f"• {reason_text}")

        if not reason_lines:
            reason_lines.append("• Verified retailer lineup")

        condition = best.get("condition", "Verified")
        in_stock = sum(
            product["availability"] == "Likely in stock"
            for product in retailer_products
        )

        history_value = (
            f"Low ${best.get('historical_low', best['price']):,.0f} • "
            f"High ${best.get('historical_high', best['price']):,.0f}\n"
            f"{best.get('history_label', 'Building history')} • "
            f"{best.get('history_checks', 1)} verified checks"
        )

        savings_value = (
            f"${best.get('savings_amount', 0):,.0f} "
            f"({best.get('savings_percent', 0):.1f}%) below "
            f"${best.get('reference_price', REFERENCE_PRICE):,.0f} reference"
        )

        embeds.append(
            {
                "title": (
                    f"{retailer} — ${best_price:,.0f} best • "
                    f"{best.get('deal_score', 0)}/100 "
                    f"{best.get('deal_tier', '')}"
                ),
                "url": canonical_url(best["url"]),
                "description": (
                    f"**{condition}**\n"
                    "Every listing was independently verified "
                    "from the retailer's live product data."
                ),
                "fields": [
                    {
                        "name": "Best price",
                        "value": f"${best_price:,.2f}",
                        "inline": True,
                    },
                    {
                        "name": "Deal score",
                        "value": (
                            f"{best.get('deal_score', 0)}/100 • "
                            f"{best.get('deal_tier', '')}"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Availability",
                        "value": f"{in_stock} in stock",
                        "inline": True,
                    },
                    {
                        "name": "Price history",
                        "value": history_value[:1024],
                        "inline": False,
                    },
                    {
                        "name": "Savings context",
                        "value": savings_value[:1024],
                        "inline": False,
                    },
                    {
                        "name": "Choose a listing",
                        "value": "\n".join(listing_lines)[:1024],
                        "inline": False,
                    },
                    {
                        "name": "Why now",
                        "value": "\n".join(reason_lines)[:1024],
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": (
                        "Deal Hunter Server 0.8.1 • "
                        f"{len(retailer_products)} verified listings"
                    )
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    payload = {
        "username": "Deal Hunter",
        "content": (
            "⚡ **Relevant MacBook deal alert**\n"
            "15-inch MacBook Air M5 • 24 GB • 1 TB"
        ),
        "embeds": embeds[:10],
    }

    body = json.dumps(payload).encode("utf-8")

    webhook_request = request.Request(
        WEBHOOK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    with request.urlopen(
        webhook_request,
        timeout=20,
    ) as response:
        log(f"Discord returned HTTP {response.status}.")


def scan_apple_product_page(page, url: str) -> dict:
    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=12000,
        )
    except Exception:
        pass

    page.wait_for_timeout(1500)

    title = page.title().strip()
    visible_text = page.locator("body").inner_text(
        timeout=15000
    )

    overview = product_information(visible_text)
    structured_product = extract_structured_product(page)

    structured_text = " ".join(
        str(structured_product.get(key, ""))
        for key in (
            "name",
            "description",
            "model",
            "sku",
            "mpn",
            "color",
        )
    )

    authoritative_text = " ".join(
        [
            title,
            structured_text,
            overview,
        ]
    )

    availability_text = visible_text.lower()

    if (
        "out of stock" in availability_text
        or "currently unavailable" in availability_text
    ):
        availability = "Out of stock"
    elif any(
        phrase in availability_text
        for phrase in (
            "add to bag",
            "add to cart",
            "order today",
        )
    ):
        availability = "Likely in stock"
    else:
        availability = "Unknown"

    product = {
        "retailer": "Apple Certified Refurbished",
        "condition": "Refurbished",
        "title": str(
            structured_product.get("name") or title
        ),
        "url": canonical_url(page.url),
        "screen": detect_screen(authoritative_text),
        "chip": detect_chip(authoritative_text),
        "memory": detect_memory(overview),
        "storage": detect_storage(overview),
        "color": detect_color(authoritative_text),
        "price": extract_price(
            structured_product,
            visible_text,
        ),
        "availability": availability,
        "checked_at": datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
    }

    product["exact_match"] = all(
        [
            product["screen"] is not None
            and abs(product["screen"] - TARGET["screen"]) <= 0.4,
            product["chip"] == TARGET["chip"],
            product["memory"] == TARGET["memory"],
            product["storage"] == TARGET["storage"],
            product["color"] in TARGET["colors"],
            product["price"] is not None,
        ]
    )

    return product


def scan() -> None:
    log("Opening retailer catalogs.")

    state = load_state()
    normalize_state_urls(state)

    cache = load_cache()
    price_history = load_price_history()
    results = []
    exact_products = []
    triggered = []
    candidates = []
    source_errors = []
    successful_retailers = set()

    pages_opened = 0
    cached_pages = 0
    now_epoch = time.time()
    checked_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        apple_context = browser.new_context(
            locale="en-US",
            viewport={
                "width": 1440,
                "height": 1100,
            },
        )
        apple_page = apple_context.new_page()

        try:
            apple_page.goto(
                APPLE_CATALOG_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            try:
                apple_page.wait_for_load_state(
                    "networkidle",
                    timeout=12000,
                )
            except Exception:
                pass

            apple_page.wait_for_timeout(2500)

            raw_links = apple_page.evaluate(
                """
                () => {
                  const results = [];

                  for (
                    const link of document.querySelectorAll(
                      'a[href*="/shop/product/"]'
                    )
                  ) {
                    const url = link.href;

                    if (!url) continue;

                    const container =
                      link.closest(
                        'li, article, [class*="product"], [class*="card"]'
                      ) ||
                      link.parentElement;

                    const text = (
                      container?.innerText ||
                      link.innerText ||
                      ""
                    ).trim();

                    if (!/macbook air/i.test(text)) continue;
                    if (!/15(?:\\.3)?[- ]?inch/i.test(text)) continue;
                    if (!/\\bm5\\b/i.test(text)) continue;

                    results.push({url, text});
                  }

                  return results;
                }
                """
            )

            seen_apple_urls = set()

            for link in raw_links:
                clean_url = canonical_url(link.get("url", ""))

                if not clean_url or clean_url in seen_apple_urls:
                    continue

                seen_apple_urls.add(clean_url)
                candidates.append(
                    {
                        "adapter": "apple",
                        "retailer": "Apple Certified Refurbished",
                        "url": clean_url,
                        "text": link.get("text", ""),
                        "page": apple_page,
                    }
                )

            successful_retailers.add(
                "Apple Certified Refurbished"
            )
            log(
                f"Apple candidates: {len(seen_apple_urls)}."
            )

        except Exception as exc:
            message = f"Apple catalog failed: {exc}"
            source_errors.append(message)
            log(message)

        best_buy_context = browser.new_context(
            locale="en-US",
            viewport={
                "width": 1440,
                "height": 1100,
            },
        )
        best_buy_page = best_buy_context.new_page()

        try:
            best_buy_candidates = best_buy.discover_candidates(
                best_buy_page
            )

            for candidate in best_buy_candidates:
                candidates.append(
                    {
                        "adapter": "best_buy",
                        "retailer": candidate.get(
                            "retailer",
                            "Best Buy",
                        ),
                        "url": canonical_url(
                            candidate.get("url", "")
                        ),
                        "text": candidate.get("text", ""),
                        "page": best_buy_page,
                    }
                )

            successful_retailers.add("Best Buy")
            log(
                f"Best Buy candidates: "
                f"{len(best_buy_candidates)}."
            )

        except Exception as exc:
            message = f"Best Buy catalog failed: {exc}"
            source_errors.append(message)
            log(message)

        if ENABLE_BH:
            bh_context = browser.new_context(
                locale="en-US",
                viewport={
                    "width": 1440,
                    "height": 1100,
                },
            )
            bh_page = bh_context.new_page()

            try:
                bh_candidates = bh_photo.discover_candidates(
                    bh_page
                )

                for candidate in bh_candidates:
                    candidates.append(
                        {
                            "adapter": "bh_photo",
                            "retailer": candidate.get(
                                "retailer",
                                "B&H Photo",
                            ),
                            "url": canonical_url(
                                candidate.get("url", "")
                            ),
                            "text": candidate.get("text", ""),
                            "page": bh_page,
                        }
                    )

                successful_retailers.add("B&H Photo")
                log(
                    f"B&H candidates: "
                    f"{len(bh_candidates)}."
                )

            except Exception as exc:
                message = f"B&H catalog failed: {exc}"
                source_errors.append(message)
                log(message)
        else:
            log(
                "B&H source disabled: Cloudflare verification "
                "blocks server-side access."
            )

        deduped_candidates = []
        seen_urls = set()

        for candidate in candidates:
            url = candidate["url"]

            if not url or url in seen_urls:
                continue

            seen_urls.add(url)
            deduped_candidates.append(candidate)

        candidates = deduped_candidates
        catalog_urls = {
            candidate["url"]
            for candidate in candidates
        }

        log(
            f"Found {len(candidates)} total candidate pages."
        )

        for index, candidate in enumerate(
            candidates,
            start=1,
        ):
            url = candidate["url"]
            fingerprint = catalog_fingerprint(
                candidate["text"]
            )

            cached_entry = cache.get(url, {})
            cached_product = cached_entry.get("product")

            try:
                last_verified_epoch = float(
                    cached_entry.get(
                        "last_verified_epoch",
                        0,
                    )
                )
            except (TypeError, ValueError):
                last_verified_epoch = 0

            cache_age_seconds = (
                now_epoch - last_verified_epoch
            )

            cache_is_stale = (
                cache_age_seconds
                >= FULL_REVERIFY_HOURS * 3600
            )

            exact_cached_product = bool(
                isinstance(cached_product, dict)
                and cached_product.get("exact_match")
            )

            catalog_changed = (
                cached_entry.get("catalog_fingerprint")
                != fingerprint
            )

            should_open = (
                not isinstance(cached_product, dict)
                or catalog_changed
                or exact_cached_product
                or cache_is_stale
            )

            if should_open:
                try:
                    log(
                        f"Opening {candidate['retailer']} "
                        f"product {index}/{len(candidates)}."
                    )

                    if candidate["adapter"] == "best_buy":
                        product = best_buy.verify_product(
                            candidate["page"],
                            url,
                        )
                    elif candidate["adapter"] == "bh_photo":
                        product = bh_photo.verify_product(
                            candidate["page"],
                            url,
                        )
                    else:
                        product = scan_apple_product_page(
                            candidate["page"],
                            url,
                        )

                    pages_opened += 1

                    cache[url] = {
                        "catalog_fingerprint": fingerprint,
                        "last_verified_epoch": time.time(),
                        "product": product,
                    }

                except Exception as exc:
                    log(
                        f"Product page failed: {url}: {exc}"
                    )
                    continue

            else:
                product = dict(cached_product)
                product["catalog_checked_at"] = checked_at
                cached_pages += 1

            results.append(product)

            if not product.get("exact_match"):
                continue

            exact_products.append(product)

        browser.close()

    for key in list(cache):
        if key in catalog_urls:
            continue

        cached_entry = cache.get(key, {})
        cached_product = cached_entry.get("product", {})
        retailer = (
            cached_product.get("retailer")
            if isinstance(cached_product, dict)
            else ""
        ) or retailer_for_url(key)

        if retailer in successful_retailers:
            del cache[key]

    for key, previous in list(state.items()):
        if not key.startswith("http"):
            continue

        if key in catalog_urls:
            continue

        if not isinstance(previous, dict):
            continue

        retailer = (
            previous.get("retailer")
            or retailer_for_url(key)
        )

        if retailer not in successful_retailers:
            continue

        if previous.get("availability") == "Likely in stock":
            previous["availability"] = "Out of stock"
            previous["last_seen"] = checked_at

    history_points_added = record_price_history(
        exact_products,
        price_history,
        checked_at,
    )
    apply_deal_metrics(
        exact_products,
        price_history,
    )

    suppressed_new_matches = 0

    for product in exact_products:
        reason = should_alert(product, state)

        if reason:
            triggered.append((product, reason))
            continue

        if (
            state.get(product["url"]) is None
            and numeric_score(product.get("deal_score"))
            < ALERT_MIN_SCORE
        ):
            suppressed_new_matches += 1

    high_score_products = [
        product
        for product in exact_products
        if (
            product.get("availability") == "Likely in stock"
            and numeric_score(product.get("deal_score"))
            >= ALERT_MIN_SCORE
        )
    ]

    force_group_alert = (
        state.get("__group_alert_version__")
        != GROUP_ALERT_VERSION
        and bool(high_score_products)
    )

    alert_needed = bool(triggered) or (
        force_group_alert and bool(exact_products)
    )

    alert_sent = not alert_needed

    if alert_needed:
        grouped_reasons = list(triggered)

        if force_group_alert:
            retailers_with_reasons = {
                product.get("retailer")
                for product, _ in grouped_reasons
            }

            for product in high_score_products:
                retailer = product.get("retailer")

                if retailer in retailers_with_reasons:
                    continue

                grouped_reasons.append(
                    (
                        product,
                        "Relevance gate enabled",
                    )
                )
                retailers_with_reasons.add(retailer)

        try:
            send_discord_group(
                exact_products,
                grouped_reasons,
            )
            alert_sent = True
        except Exception as exc:
            log(f"Discord alert failed: {exc}")

    if alert_sent:
        for product in exact_products:
            state[product["url"]] = {
                "retailer": product.get("retailer"),
                "price": product["price"],
                "availability": product["availability"],
                "deal_score": product.get("deal_score"),
                "historical_low": product.get("historical_low"),
                "last_seen": product["checked_at"],
            }

        if exact_products:
            state["__group_alert_version__"] = (
                GROUP_ALERT_VERSION
            )

    save_state(state)
    save_cache(cache)
    save_price_history(price_history)

    RESULTS_FILE.write_text(
        json.dumps(results, indent=2),
        encoding="utf-8",
    )

    apple_matches = sum(
        product.get("retailer")
        == "Apple Certified Refurbished"
        for product in exact_products
    )
    best_buy_matches = sum(
        product.get("retailer") == "Best Buy"
        for product in exact_products
    )
    bh_matches = sum(
        product.get("retailer") == "B&H Photo"
        for product in exact_products
    )

    summary_lines = [
        (
            "Checked: "
            f"{datetime.now().astimezone().isoformat()}"
        ),
        f"Total candidate pages: {len(candidates)}",
        f"Product pages opened: {pages_opened}",
        f"Cached pages reused: {cached_pages}",
        f"Apple exact matches: {apple_matches}",
        f"Best Buy exact matches: {best_buy_matches}",
        f"B&H exact matches: {bh_matches}",
        f"Total exact matches: {len(exact_products)}",
        f"Price history points added: {history_points_added}",
        f"Alert score threshold: {ALERT_MIN_SCORE}/100",
        f"Relevant alert events: {len(triggered)}",
        f"Suppressed new matches: {suppressed_new_matches}",
        (
            "Best deal score: "
            + (
                f"{max(product.get('deal_score', 0) for product in exact_products)}"
                f"/100"
                if exact_products
                else "n/a"
            )
        ),
        (
            "Grouped alert sent: "
            f"{'yes' if alert_needed and alert_sent else 'no'}"
        ),
    ]

    if source_errors:
        summary_lines.append(
            f"Retailer errors: {len(source_errors)}"
        )

    summary = "\n".join(summary_lines) + "\n"

    SCAN_FILE.write_text(
        summary,
        encoding="utf-8",
    )

    log(summary.strip())

def main() -> None:
    log("Deal Hunter Server 0.8.1 starting.")

    if not WEBHOOK_URL:
        log(
            "Warning: DISCORD_WEBHOOK_URL is not set."
        )

    while True:
        started = time.monotonic()

        try:
            scan()
        except Exception as exc:
            log(f"Scan failed: {exc}")

        elapsed = time.monotonic() - started

        sleep_seconds = max(
            60,
            INTERVAL_MINUTES * 60 - int(elapsed),
        )

        log(
            f"Sleeping for {sleep_seconds} seconds."
        )

        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
