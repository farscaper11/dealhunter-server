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

# Refreshed by discover_candidates() on every scan. This lets verification
# use Best Buy's live search-result card instead of the product endpoint,
# which currently fails from the TrueNAS container with HTTP/2/time-out errors.
_CANDIDATE_CACHE: dict[str, dict] = {}


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


def extract_best_buy_price(card_text: str) -> float | None:
    normalized = compact(card_text)

    # Exclude comparison/MSRP text so it cannot be mistaken for the live price.
    live_price_section = re.split(
        r"\b(?:The comparable value is|Comp\.?\s*Value|Was Price)\b",
        normalized,
        maxsplit=1,
        flags=re.I,
    )[0]

    values: list[float] = []

    for raw in re.findall(
        r"\$\s*([0-9]{1,2}(?:,[0-9]{3})+(?:\.\d{2})?)",
        live_price_section,
    ):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue

        if 500 <= value <= 5000:
            values.append(value)

    if not values:
        return None

    # Best Buy often renders the current price twice. min() also handles
    # a lower member/deal price appearing alongside the regular price.
    return min(values)


def detect_availability(card_text: str) -> str:
    normalized = compact(card_text).lower()

    if any(
        phrase in normalized
        for phrase in (
            "no longer available",
            "currently unavailable",
            "sold out",
            "this item is unavailable",
            "unavailable nearby",
        )
    ):
        return "Out of stock"

    if "add to cart" in normalized:
        return "Likely in stock"

    return "Unknown"


def sold_by_best_buy(card_text: str) -> bool:
    normalized = compact(card_text)

    seller_match = re.search(
        r"\bSold by\s+(.{1,80}?)(?:\s{2,}|Add to cart|$)",
        normalized,
        re.I,
    )

    if seller_match:
        return seller_match.group(1).strip().lower().startswith("best buy")

    # On Best Buy's first-party search cards, no seller label is shown.
    # Marketplace cards identify the outside seller. A direct Best Buy card
    # with an Add to cart control and no third-party seller marker is treated
    # as first-party inventory.
    return (
        "add to cart" in normalized.lower()
        and "marketplace" not in normalized.lower()
    )


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
              'a.product-list-item-link[href*="/product/"], '
              + 'a[href*="/product/"]'
            )
          ) {
            const card =
              link.closest('.sku-block') ||
              link.closest('li.product-list-item') ||
              link.closest('li, article');

            if (!card) continue;

            const titleElement =
              card.querySelector('h3.product-title') ||
              card.querySelector('[data-testid="product-title"]');

            const title = (
              titleElement?.getAttribute('title') ||
              titleElement?.innerText ||
              link.getAttribute('aria-label') ||
              link.innerText ||
              ''
            ).replace(/\\s+/g, ' ').trim();

            const text = (
              card.innerText || ''
            ).replace(/\\s+/g, ' ').trim();

            const combined = `${title} ${text}`;

            if (!/macbook air/i.test(combined)) continue;
            if (!/15(?:\\.3)?[- ]?inch/i.test(combined)) continue;
            if (!/\\bm5\\b/i.test(combined)) continue;
            if (!/24\\s*gb\\s*(?:memory|ram)/i.test(combined)) continue;
            if (!/1\\s*tb\\s*ssd/i.test(combined)) continue;

            const buttons = [...card.querySelectorAll('button')]
              .map(button => (
                button.innerText ||
                button.getAttribute('aria-label') ||
                ''
              ).replace(/\\s+/g, ' ').trim())
              .filter(Boolean);

            results.push({
              url: link.href,
              title,
              text,
              buttons
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
        if not url or url in seen_urls:
            continue

        seen_urls.add(url)

        normalized = {
            "retailer": RETAILER,
            "url": url,
            "title": compact(candidate.get("title", "")),
            "text": compact(candidate.get("text", "")),
            "buttons": [
                compact(button)
                for button in candidate.get("buttons", [])
                if compact(button)
            ],
        }

        _CANDIDATE_CACHE[url] = normalized

        candidates.append(
            {
                "retailer": RETAILER,
                "url": url,
                "text": normalized["text"],
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
            "Best Buy listing disappeared from the live search results"
        )

    title = candidate["title"]
    card_text = candidate["text"]

    if candidate["buttons"]:
        card_text = compact(
            f"{card_text} {' '.join(candidate['buttons'])}"
        )

    product = {
        "retailer": RETAILER,
        "condition": "New",
        "title": title,
        "url": clean_url,
        "screen": detect_screen(title),
        "chip": detect_chip(title),
        "memory": detect_memory(title),
        "storage": detect_storage(title),
        "color": detect_color(title),
        "price": extract_best_buy_price(card_text),
        "availability": detect_availability(card_text),
        "sold_by_retailer": sold_by_best_buy(card_text),
        "verification_source": "Best Buy live search-result card",
        "checked_at": datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
    }

    product["exact_match"] = all(
        [
            "macbook air" in title.lower(),
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
