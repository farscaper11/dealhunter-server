import re
from datetime import datetime


RETAILER = "B&H Photo"
CATALOG_URL = (
    "https://www.bhphotovideo.com/c/buy/"
    "apple-macbook-air-15/ci/56582"
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

# Refreshed on every catalog scan. Verification uses the live B&H product
# card, avoiding a second page request while still checking specs, price,
# and availability from current retailer data.
_CANDIDATE_CACHE: dict[str, dict] = {}


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canonical_url(url: str) -> str:
    clean = (url or "").split("#", 1)[0]
    return clean.split("?", 1)[0]


def detect_screen(text: str) -> float | None:
    match = re.search(
        r'\b(13(?:\.3|\.6)?|14(?:\.2)?|15(?:\.3)?|16(?:\.2)?)'
        r'\s*(?:"|(?:-| )?inch)\b',
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
        r"(?:\s+(?:Unified\s+RAM|Unified\s+Memory|RAM|Memory))?\b",
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
    return {
        1000: 1024,
        2000: 2048,
        4000: 4096,
    }.get(value, value)


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


def extract_price(card_text: str) -> float | None:
    normalized = compact(card_text)

    # Ignore monthly financing amounts. Current and crossed-out retail prices
    # remain; choosing the lowest valid laptop price returns the live sale
    # price when B&H displays both.
    normalized = re.sub(
        r"\$\s*\d+(?:\.\d{2})?\s*/?\s*mo\.?",
        "",
        normalized,
        flags=re.I,
    )

    prices: list[float] = []

    for raw in re.findall(
        r"\$\s*([0-9]{1,2}(?:,[0-9]{3})(?:\.\d{2})?)",
        normalized,
    ):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue

        if 1000 <= value <= 5000:
            prices.append(value)

    return min(prices) if prices else None


def detect_availability(card_text: str) -> str:
    normalized = compact(card_text).lower()

    if any(
        phrase in normalized
        for phrase in (
            "discontinued",
            "no longer available",
            "temporarily unavailable",
            "not available",
            "out of stock",
        )
    ):
        return "Out of stock"

    if any(
        phrase in normalized
        for phrase in (
            "in stock",
            "add to cart",
            "limited supply at this price",
            "more on the way",
        )
    ):
        return "Likely in stock"

    if any(
        phrase in normalized
        for phrase in (
            "back-ordered",
            "backordered",
            "special order",
            "coming soon",
        )
    ):
        return "Unknown"

    return "Unknown"


def is_standard_laptop(title: str) -> bool:
    lowered = (title or "").lower()

    excluded = (
        "applecare",
        " kit ",
        "bundle",
        "with 70w",
        "with 35w",
        "power adapter",
    )

    padded = f" {lowered} "
    return not any(term in padded for term in excluded)


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

    page.wait_for_timeout(3000)

    for _ in range(5):
        page.evaluate(
            "window.scrollTo(0, document.body.scrollHeight)"
        )
        page.wait_for_timeout(700)

    body_text = page.locator("body").inner_text(timeout=15000)
    lowered_body = body_text.lower()

    if "access denied" in lowered_body:
        raise RuntimeError("B&H returned an access-denied page")

    if "verify you are human" in lowered_body:
        raise RuntimeError("B&H returned a human-verification page")

    raw_candidates = page.evaluate(
        """
        () => {
          const results = [];

          for (
            const link of document.querySelectorAll(
              'a[href*="/c/product/"]'
            )
          ) {
            const card =
              link.closest(
                'li, article, [data-selenium*="miniProductPage"], '
                + '[class*="productCard"], [class*="product-card"], '
                + '[class*="productList"], [class*="product-list"]'
              ) ||
              link.parentElement?.parentElement ||
              link.parentElement;

            if (!card) continue;

            const heading =
              card.querySelector('h2, h3, h4, [class*="title"]');

            const title = (
              heading?.innerText ||
              link.getAttribute('title') ||
              link.getAttribute('aria-label') ||
              link.innerText ||
              ''
            ).replace(/\\s+/g, ' ').trim();

            const text = (
              card.innerText || ''
            ).replace(/\\s+/g, ' ').trim();

            const combined = `${title} ${text}`;

            if (!/macbook air/i.test(combined)) continue;
            if (!/15(?:\\.3)?(?:"|[- ]?inch)/i.test(combined)) continue;
            if (!/\\bm5\\b/i.test(combined)) continue;
            if (!/24\\s*gb\\s*(?:unified\\s+ram|unified\\s+memory|ram|memory)/i.test(combined)) continue;
            if (!/1\\s*tb\\s*ssd/i.test(combined)) continue;

            results.push({
              url: link.href,
              title,
              text
            });
          }

          return results;
        }
        """
    )

    _CANDIDATE_CACHE.clear()

    candidates = []
    seen_urls = set()

    for candidate in raw_candidates:
        url = canonical_url(candidate.get("url", ""))
        title = compact(candidate.get("title", ""))
        text = compact(candidate.get("text", ""))

        if not url or url in seen_urls:
            continue

        combined = compact(f"{title} {text}")

        # Some links have short or empty anchor text, so recover a title from
        # the card's leading product text when needed.
        if "macbook air" not in title.lower():
            title_match = re.search(
                r'(Apple\s+15["”]?\s*MacBook Air.*?'
                r'(?:Midnight|Starlight|Silver|Sky Blue))',
                combined,
                re.I,
            )
            if title_match:
                title = compact(title_match.group(1))

        if not is_standard_laptop(title):
            continue

        seen_urls.add(url)

        normalized = {
            "retailer": RETAILER,
            "url": url,
            "title": title,
            "text": text,
        }

        _CANDIDATE_CACHE[url] = normalized

        candidates.append(
            {
                "retailer": RETAILER,
                "url": url,
                "text": text,
            }
        )

    return candidates


def verify_product(page, url: str) -> dict:
    clean_url = canonical_url(url)
    candidate = _CANDIDATE_CACHE.get(clean_url)

    if candidate is None:
        discover_candidates(page)
        candidate = _CANDIDATE_CACHE.get(clean_url)

    if candidate is None:
        raise RuntimeError(
            "B&H listing disappeared from the live catalog"
        )

    title = candidate["title"]
    card_text = candidate["text"]
    authoritative_text = compact(f"{title} {card_text}")

    product = {
        "retailer": RETAILER,
        "condition": "New",
        "title": title,
        "url": clean_url,
        "screen": detect_screen(authoritative_text),
        "chip": detect_chip(authoritative_text),
        "memory": detect_memory(authoritative_text),
        "storage": detect_storage(authoritative_text),
        "color": detect_color(authoritative_text),
        "price": extract_price(card_text),
        "availability": detect_availability(card_text),
        "sold_by_retailer": True,
        "verification_source": "B&H live catalog card",
        "checked_at": datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
    }

    product["exact_match"] = all(
        [
            "macbook air" in authoritative_text.lower(),
            is_standard_laptop(title),
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
