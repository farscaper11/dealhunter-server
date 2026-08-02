import html
import json
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

REQUEST_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
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
    return {1000: 1024, 2000: 2048, 4000: 4096}.get(value, value)


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
    normalized = compact(visible_text[:30000])

    patterns = (
        (
            r"Sold by Best Buy\s+\$\s*"
            r"([0-9]{1,2}(?:,[0-9]{3})+(?:\.\d{2})?)"
        ),
        (
            r"SKU:\s*\d+.*?Sold by Best Buy.*?\$\s*"
            r"([0-9]{1,2}(?:,[0-9]{3})+(?:\.\d{2})?)"
        ),
        r"\$\s*([0-9]{1,2}(?:,[0-9]{3})+(?:\.\d{2})?)",
    )

    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if not match:
            continue

        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue

        if 500 <= value <= 5000:
            return value

    return None


def detect_availability(visible_text: str) -> str:
    normalized = compact(visible_text[:30000]).lower()

    if any(
        phrase in normalized
        for phrase in (
            "no longer available",
            "currently unavailable",
            "sold out",
            "this item is unavailable",
            "out of stock",
        )
    ):
        return "Out of stock"

    if any(
        phrase in normalized
        for phrase in (
            "add to cart",
            "add to basket",
            "get it today",
            "shipping available",
        )
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
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
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


def _walk_json(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_json(item)
        return

    if not isinstance(value, dict):
        return

    raw_type = value.get("@type", [])
    types = raw_type if isinstance(raw_type, list) else [raw_type]

    if any(str(item).lower() == "product" for item in types):
        yield value

    for item in value.values():
        yield from _walk_json(item)


def _extract_json_ld_product(raw_html: str) -> dict:
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>"
        r"(.*?)</script>",
        raw_html or "",
        re.I | re.S,
    )

    for script in scripts:
        try:
            data = json.loads(html.unescape(script).strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        for product in _walk_json(data):
            combined = " ".join(
                str(product.get(key, ""))
                for key in ("name", "description", "sku", "mpn")
            )
            if "macbook air" in combined.lower():
                return product

    return {}


def _offers(product: dict) -> list[dict]:
    offers = product.get("offers", [])
    if isinstance(offers, dict):
        return [offers]
    if isinstance(offers, list):
        return [offer for offer in offers if isinstance(offer, dict)]
    return []


def _price_from_structured(product: dict) -> float | None:
    prices = []

    for offer in _offers(product):
        for key in ("price", "lowPrice"):
            raw_value = offer.get(key)
            try:
                value = float(
                    str(raw_value).replace("$", "").replace(",", "")
                )
            except (TypeError, ValueError):
                continue

            if 500 <= value <= 5000:
                prices.append(value)

    unique = sorted(set(prices))
    return unique[0] if len(unique) == 1 else None


def _availability_from_structured(product: dict) -> str:
    values = [
        str(offer.get("availability", "")).lower()
        for offer in _offers(product)
    ]
    combined = " ".join(values)

    if "outofstock" in combined or "soldout" in combined:
        return "Out of stock"

    if "instock" in combined or "limitedavailability" in combined:
        return "Likely in stock"

    return "Unknown"


def _seller_is_best_buy_structured(product: dict) -> bool:
    seller_names = []

    for offer in _offers(product):
        seller = offer.get("seller")
        if isinstance(seller, dict):
            seller_names.append(str(seller.get("name", "")))
        elif seller:
            seller_names.append(str(seller))

    return any("best buy" in name.lower() for name in seller_names)


def _text_from_html(raw_html: str) -> str:
    cleaned = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        raw_html or "",
        flags=re.I | re.S,
    )
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return compact(html.unescape(cleaned))


def _title_from_html(raw_html: str) -> str:
    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        raw_html or "",
        re.I | re.S,
    )
    return compact(html.unescape(match.group(1))) if match else ""


def _build_product(
    *,
    title: str,
    url: str,
    authoritative_text: str,
    price: float | None,
    availability: str,
    seller_is_best_buy: bool,
) -> dict:
    product = {
        "retailer": RETAILER,
        "condition": "New",
        "title": title,
        "url": canonical_url(url),
        "screen": detect_screen(title),
        "chip": detect_chip(title),
        "memory": detect_memory(title),
        "storage": detect_storage(title),
        "color": detect_color(title),
        "price": price,
        "availability": availability,
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


def _verify_from_html(raw_html: str, requested_url: str) -> dict:
    structured_product = _extract_json_ld_product(raw_html)
    visible_text = _text_from_html(raw_html)

    title = compact(
        str(structured_product.get("name", ""))
        or _title_from_html(raw_html)
    )
    authoritative_text = f"{title} {visible_text[:30000]}"

    seller_is_best_buy = (
        _seller_is_best_buy_structured(structured_product)
        or "sold by best buy" in visible_text[:30000].lower()
        or "best buy" in raw_html[:100000].lower()
    )

    price = _price_from_structured(structured_product)
    if price is None:
        price = extract_best_buy_price(visible_text)

    availability = _availability_from_structured(structured_product)
    if availability == "Unknown":
        availability = detect_availability(visible_text)

    return _build_product(
        title=title,
        url=requested_url,
        authoritative_text=authoritative_text,
        price=price,
        availability=availability,
        seller_is_best_buy=seller_is_best_buy,
    )


def _request_product_html(page, url: str) -> str:
    response = page.context.request.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=60000,
        fail_on_status_code=False,
    )

    if response.status >= 400:
        raise RuntimeError(
            f"Best Buy fallback request returned HTTP {response.status}"
        )

    raw_html = response.text()

    if "access denied" in raw_html.lower():
        raise RuntimeError("Best Buy fallback returned access denied")

    return raw_html


def verify_product(page, url: str) -> dict:
    navigation_error = None

    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )
    except Exception as exc:
        navigation_error = exc
        if "ERR_HTTP2_PROTOCOL_ERROR" not in str(exc):
            raise

    if navigation_error is None:
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass

        page.wait_for_timeout(1800)
        visible_text = page.locator("body").inner_text(timeout=15000)

        if "access denied" not in visible_text.lower():
            try:
                title = compact(
                    page.locator("h1").first.inner_text(timeout=5000)
                )
            except Exception:
                title = compact(page.title())

            authoritative_text = f"{title} {visible_text[:30000]}"
            seller_is_best_buy = (
                "sold by best buy" in visible_text[:30000].lower()
            )

            return _build_product(
                title=title,
                url=page.url,
                authoritative_text=authoritative_text,
                price=extract_best_buy_price(visible_text),
                availability=detect_availability(visible_text),
                seller_is_best_buy=seller_is_best_buy,
            )

    raw_html = _request_product_html(page, url)
    return _verify_from_html(raw_html, url)
