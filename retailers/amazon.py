import re
from datetime import datetime


RETAILER = "Amazon"

# Known exact configuration used as the discovery seed. Amazon's variation
# controls are inspected for sibling colors/ASINs during each catalog scan.
SEED_URL = (
    "https://www.amazon.com/"
    "Apple-2026-MacBook-15-inch-Laptop/dp/B0GR1RWSMF"
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
    match = re.search(
        r"amazon\.com/(?:[^/]+/)?dp/([A-Z0-9]{10})",
        url or "",
        re.I,
    )

    if not match:
        match = re.search(
            r"amazon\.com/gp/product/([A-Z0-9]{10})",
            url or "",
            re.I,
        )

    if match:
        asin = match.group(1).upper()
        return f"https://www.amazon.com/dp/{asin}"

    clean = (url or "").split("#", 1)[0]
    return clean.split("?", 1)[0]


def asin_from_url(url: str) -> str:
    match = re.search(
        r"/(?:dp|gp/product)/([A-Z0-9]{10})",
        url or "",
        re.I,
    )
    return match.group(1).upper() if match else ""


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
        r"(?:\s+(?:Unified\s+Memory|Memory|RAM))?\b",
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


def parse_price(raw_text: str) -> float | None:
    for raw in re.findall(
        r"\$\s*([0-9]{1,2}(?:,[0-9]{3})(?:\.\d{2})?)",
        raw_text or "",
    ):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue

        if 500 <= value <= 5000:
            return value

    return None


def extract_buy_box_price(page, visible_text: str) -> float | None:
    selectors = (
        "#corePrice_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#price_inside_buybox",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#newBuyBoxPrice",
    )

    for selector in selectors:
        locator = page.locator(selector)

        if locator.count() == 0:
            continue

        try:
            text = compact(locator.first.inner_text(timeout=3000))
        except Exception:
            continue

        price = parse_price(text)
        if price is not None:
            return price

    # The main buy box appears before used/alternate offers in Amazon's text.
    buy_new_match = re.search(
        r"\bBuy New\b(.*?)(?:\bUsed\s*[-–]|\bOther sellers on Amazon\b|$)",
        visible_text[:18000],
        re.I | re.S,
    )

    if buy_new_match:
        price = parse_price(buy_new_match.group(1))
        if price is not None:
            return price

    return None


def seller_and_shipper(page, visible_text: str) -> tuple[str, str]:
    seller = ""
    shipper = ""

    # Standard tabular buy box used on many Amazon product pages.
    rows = page.locator(
        "#tabular-buybox .tabular-buybox-text, "
        "#tabular-buybox-container .tabular-buybox-text"
    )

    for index in range(rows.count()):
        try:
            text = compact(rows.nth(index).inner_text(timeout=2000))
        except Exception:
            continue

        lowered = text.lower()

        if "amazon.com" not in lowered:
            continue

        # Amazon frequently renders the shipper first and seller second.
        if not shipper:
            shipper = "Amazon.com"
        elif not seller:
            seller = "Amazon.com"

    try:
        merchant_info = compact(
            page.locator("#merchant-info").first.inner_text(timeout=3000)
        )
    except Exception:
        merchant_info = ""

    if re.search(r"\bships from\s+amazon(?:\.com)?\b", merchant_info, re.I):
        shipper = "Amazon.com"

    if re.search(r"\bsold by\s+amazon(?:\.com)?\b", merchant_info, re.I):
        seller = "Amazon.com"

    # Newer layouts may collapse both into a "Shipper / Seller" field.
    combined_match = re.search(
        r"\bShipper\s*/\s*Seller\s+Amazon\.com\b",
        visible_text[:18000],
        re.I,
    )
    if combined_match:
        shipper = "Amazon.com"
        seller = "Amazon.com"

    ships_match = re.search(
        r"\bShips from:\s*([^\n\r]{1,80})",
        visible_text[:18000],
        re.I,
    )
    if ships_match and "amazon" in ships_match.group(1).lower():
        shipper = "Amazon.com"

    sold_match = re.search(
        r"\bSold by:\s*([^\n\r]{1,80})",
        visible_text[:18000],
        re.I,
    )
    if sold_match and "amazon.com" in sold_match.group(1).lower():
        seller = "Amazon.com"

    return seller, shipper


def detect_availability(page, visible_text: str) -> str:
    try:
        availability_text = compact(
            page.locator("#availability").first.inner_text(timeout=3000)
        )
    except Exception:
        availability_text = ""

    normalized = compact(
        f"{availability_text} {visible_text[:12000]}"
    ).lower()

    if any(
        phrase in normalized
        for phrase in (
            "currently unavailable",
            "temporarily out of stock",
            "we don't know when or if this item will be back in stock",
            "unavailable",
        )
    ):
        return "Out of stock"

    if (
        "in stock" in normalized
        and (
            "add to cart" in normalized
            or "buy now" in normalized
        )
    ):
        return "Likely in stock"

    return "Unknown"


def raise_for_challenge(page, visible_text: str) -> None:
    title = compact(page.title()).lower()
    lowered = visible_text.lower()

    if (
        "robot check" in title
        or "enter the characters you see below" in lowered
        or "sorry, we just need to make sure you're not a robot" in lowered
        or "automated access to amazon data" in lowered
    ):
        raise RuntimeError("Amazon returned a bot-verification page")


def discover_candidates(page) -> list[dict]:
    page.goto(
        SEED_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass

    page.wait_for_timeout(2500)

    visible_text = page.locator("body").inner_text(timeout=15000)
    raise_for_challenge(page, visible_text)

    variant_data = page.evaluate(
        """
        () => {
          const results = [];

          const add = (asin, label) => {
            const cleanAsin = String(asin || "").trim().toUpperCase();

            if (!/^[A-Z0-9]{10}$/.test(cleanAsin)) return;

            results.push({
              asin: cleanAsin,
              label: String(label || "").replace(/\\s+/g, " ").trim()
            });
          };

          const currentUrl = location.href;
          const currentMatch = currentUrl.match(
            /\\/(?:dp|gp\\/product)\\/([A-Z0-9]{10})/i
          );

          if (currentMatch) {
            add(currentMatch[1], document.title);
          }

          const scopes = [
            "#variation_color_name",
            "#variation_size_name",
            "#variation_style_name",
            "#twister",
            "#inline-twister-row-color_name",
            "#inline-twister-row-size_name"
          ];

          for (const scopeSelector of scopes) {
            const scope = document.querySelector(scopeSelector);
            if (!scope) continue;

            for (
              const element of scope.querySelectorAll(
                "[data-defaultasin], [data-asin], a[href*='/dp/']"
              )
            ) {
              const href = element.getAttribute("href") || "";
              const hrefMatch = href.match(
                /\\/(?:dp|gp\\/product)\\/([A-Z0-9]{10})/i
              );

              const asin =
                element.getAttribute("data-defaultasin") ||
                element.getAttribute("data-asin") ||
                hrefMatch?.[1] ||
                "";

              const label =
                element.getAttribute("title") ||
                element.getAttribute("aria-label") ||
                element.innerText ||
                "";

              add(asin, label);
            }
          }

          return results;
        }
        """
    )

    candidates = []
    seen_asins = set()

    for item in variant_data:
        asin = compact(item.get("asin", "")).upper()

        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue

        if asin in seen_asins:
            continue

        seen_asins.add(asin)

        candidates.append(
            {
                "retailer": RETAILER,
                "url": f"https://www.amazon.com/dp/{asin}",
                "text": compact(item.get("label", "")),
            }
        )

    # Always retain the known exact listing even if Amazon changes the
    # variation markup.
    seed_asin = asin_from_url(SEED_URL)

    if seed_asin and seed_asin not in seen_asins:
        candidates.append(
            {
                "retailer": RETAILER,
                "url": canonical_url(SEED_URL),
                "text": "15-inch MacBook Air M5 24GB 1TB",
            }
        )

    return candidates


def verify_product(page, url: str) -> dict:
    page.goto(
        canonical_url(url),
        wait_until="domcontentloaded",
        timeout=60000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass

    page.wait_for_timeout(1800)

    visible_text = page.locator("body").inner_text(timeout=15000)
    raise_for_challenge(page, visible_text)

    try:
        title = compact(
            page.locator("#productTitle").first.inner_text(timeout=5000)
        )
    except Exception:
        title = compact(page.title())

    seller, shipper = seller_and_shipper(page, visible_text)
    price = extract_buy_box_price(page, visible_text)
    availability = detect_availability(page, visible_text)

    product = {
        "retailer": RETAILER,
        "condition": "New",
        "title": title,
        "url": canonical_url(page.url),
        "asin": asin_from_url(page.url),
        "screen": detect_screen(title),
        "chip": detect_chip(title),
        "memory": detect_memory(title),
        "storage": detect_storage(title),
        "color": detect_color(title),
        "price": price,
        "availability": availability,
        "seller": seller,
        "shipper": shipper,
        "sold_by_retailer": seller == "Amazon.com",
        "shipped_by_retailer": shipper == "Amazon.com",
        "verification_source": "Amazon live new-offer buy box",
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
            product["availability"] == "Likely in stock",
            product["sold_by_retailer"],
            product["shipped_by_retailer"],
        ]
    )

    return product
