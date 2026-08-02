import re
from datetime import datetime


RETAILER = "Best Buy"
CATALOG_URL = (
    "https://www.bestbuy.com/site/searchpage.jsp"
    "?id=pcat17071&st=macbook+air+m5+24gb+1tb"
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


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canonical_url(url: str) -> str:
    clean = (url or "").split("#", 1)[0]
    return clean.split("?", 1)[0]


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
        r"\b(8|16|18|24|32|36|48|64|96|128)\s*GB"
        r"(?:\s+(?:Memory|RAM|Unified Memory))?\b",
        text or "",
        re.I,
    )
    return int(match.group(1)) if match else None


def detect_storage(text: str) -> int | None:
    match = re.search(
        r"\b(1|2|4)\s*TB\s*(?:SSD|Storage)?\b",
        text or "",
        re.I,
    )
    if match:
        return int(match.group(1)) * 1024

    match = re.search(
        r"\b(256|512|1000|1024|2000|2048|4000|4096)\s*GB"
        r"\s*(?:SSD|Storage|Solid State Drive)?\b",
        text or "",
        re.I,
    )
    if not match:
        return None

    value = int(match.group(1))
    if value == 1000:
        return 1024
    if value == 2000:
        return 2048
    if value == 4000:
        return 4096
    return value


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


def extract_best_buy_price(visible_text: str) -> float | None:
    normalized = compact(visible_text[:18000])

    match = re.search(
        r"Sold by Best Buy\s+\$\s*"
        r"([0-9]{1,2}(?:,[0-9]{3})+(?:\.\d{2})?)",
        normalized,
        re.I,
    )

    if not match:
        match = re.search(
            r"SKU:\s*\d+.*?Sold by Best Buy.*?\$\s*"
            r"([0-9]{1,2}(?:,[0-9]{3})+(?:\.\d{2})?)",
            normalized,
            re.I,
        )

    if not match:
        return None

    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None

    return value if 500 <= value <= 5000 else None


def detect_availability(visible_text: str) -> str:
    normalized = compact(visible_text[:18000]).lower()

    if any(
        phrase in normalized
        for phrase in (
            "no longer available",
            "currently unavailable",
            "sold out",
            "this item is unavailable",
        )
    ):
        return "Out of stock"

    if (
        "sold by best buy" in normalized
        and "add to cart" in normalized
    ):
        return "Likely in stock"

    return "Unknown"


def discover_candidates(page) -> list[dict]:
    page.goto(
        CATALOG_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass

    page.wait_for_timeout(2500)

    for _ in range(4):
        page.evaluate(
            "window.scrollTo(0, document.body.scrollHeight)"
        )
        page.wait_for_timeout(750)

    body_text = page.locator("body").inner_text(timeout=15000)
    if "access denied" in body_text.lower():
        raise RuntimeError("Best Buy returned an access-denied page")

    raw_candidates = page.evaluate(
        """
        () => {
          const results = [];

          for (
            const link of document.querySelectorAll(
              'a[href*="/product/"]'
            )
          ) {
            const url = link.href;
            if (!url) continue;

            const container =
              link.closest(
                'li, article, [class*="product"], [class*="sku"], [class*="card"]'
              ) || link.parentElement;

            const text = (
              container?.innerText ||
              link.innerText ||
              link.getAttribute("aria-label") ||
              ""
            ).trim();

            if (!/macbook air/i.test(text)) continue;
            if (!/15(?:\\.3)?[- ]?inch/i.test(text)) continue;
            if (!/\\bm5\\b/i.test(text)) continue;
            if (!/24\\s*gb\\s*(?:memory|ram)/i.test(text)) continue;
            if (!/1\\s*tb\\s*ssd/i.test(text)) continue;

            results.push({url, text});
          }

          return results;
        }
        """
    )

    candidates = []
    seen_urls = set()

    for candidate in raw_candidates:
        url = canonical_url(candidate.get("url", ""))
        if not url or url in seen_urls:
            continue

        seen_urls.add(url)
        candidates.append(
            {
                "retailer": RETAILER,
                "url": url,
                "text": compact(candidate.get("text", "")),
            }
        )

    return candidates


def verify_product(page, url: str) -> dict:
    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass

    page.wait_for_timeout(1800)

    visible_text = page.locator("body").inner_text(timeout=15000)
    if "access denied" in visible_text.lower():
        raise RuntimeError("Best Buy returned an access-denied page")

    try:
        title = compact(page.locator("h1").first.inner_text(timeout=5000))
    except Exception:
        title = compact(page.title())

    authoritative_text = f"{title} {visible_text[:18000]}"
    seller_is_best_buy = "sold by best buy" in visible_text[:18000].lower()

    product = {
        "retailer": RETAILER,
        "condition": "New",
        "title": title,
        "url": canonical_url(page.url),
        "screen": detect_screen(title),
        "chip": detect_chip(title),
        "memory": detect_memory(title),
        "storage": detect_storage(title),
        "color": detect_color(title),
        "price": extract_best_buy_price(visible_text),
        "availability": detect_availability(visible_text),
        "sold_by_retailer": seller_is_best_buy,
        "checked_at": datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
    }

    product["exact_match"] = all(
        [
            "macbook air" in authoritative_text.lower(),
            product["screen"] is not None
            and abs(product["screen"] - TARGET["screen"]) <= 0.4,
            product["chip"] == TARGET["chip"],
            product["memory"] == TARGET["memory"],
            product["storage"] == TARGET["storage"],
            product["color"] in TARGET["colors"],
            product["price"] is not None,
            product["sold_by_retailer"],
        ]
    )

    return product
