import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

from playwright.sync_api import sync_playwright


CATALOG_URL = "https://www.apple.com/shop/refurbished/mac"

INTERVAL_MINUTES = max(
    5,
    int(os.getenv("DH_SCAN_INTERVAL_MINUTES", "15")),
)

DATA_DIR = Path(os.getenv("DH_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

STATE_FILE = DATA_DIR / "alert-state.json"
RESULTS_FILE = DATA_DIR / "last-results.json"
SCAN_FILE = DATA_DIR / "last-scan.txt"

TARGET = {
    "screen": 15.0,
    "chip": "M5",
    "memory": 24,
    "storage": 1024,
    "color": "Midnight",
}

USER_AGENT = (
    "DealHunter/0.2 "
    "(+https://github.com/farscaper11/dealhunter-server)"
)


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


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


def should_alert(product: dict, state: dict) -> str | None:
    previous = state.get(product["url"])

    if previous is None:
        return "New exact match"

    previous_price = previous.get("price")
    current_price = product.get("price")

    if (
        previous_price is not None
        and current_price is not None
        and current_price < previous_price
    ):
        return f"Price dropped from ${previous_price:,.2f}"

    if (
        previous.get("availability") != "Likely in stock"
        and product.get("availability") == "Likely in stock"
    ):
        return "Back in stock"

    return None


def send_discord(product: dict, reason: str) -> None:
    if not WEBHOOK_URL:
        log("Discord webhook is not configured.")
        return

    storage_label = (
        f"{product['storage'] // 1024} TB"
        if product["storage"] >= 1024
        else f"{product['storage']} GB"
    )

    payload = {
        "username": "Deal Hunter",
        "content": "🚨 **Verified MacBook deal found**",
        "embeds": [
            {
                "title": product["title"][:250],
                "url": product["url"],
                "description": (
                    "Exact product-page verification passed.\n\n"
                    f"[Open the verified Apple product page]"
                    f"({product['url']})"
                ),
                "fields": [
                    {
                        "name": "Price",
                        "value": f"${product['price']:,.2f}",
                        "inline": True,
                    },
                    {
                        "name": "Availability",
                        "value": product["availability"],
                        "inline": True,
                    },
                    {
                        "name": "Reason",
                        "value": reason,
                        "inline": True,
                    },
                    {
                        "name": "Memory",
                        "value": f"{product['memory']} GB",
                        "inline": True,
                    },
                    {
                        "name": "Storage",
                        "value": storage_label,
                        "inline": True,
                    },
                    {
                        "name": "Color",
                        "value": product["color"],
                        "inline": True,
                    },
                ],
                "footer": {
                    "text": "Deal Hunter Server 0.2"
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
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


def scan_product_page(page, url: str) -> dict:
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
        "title": str(
            structured_product.get("name") or title
        ),
        "url": page.url,
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
            product["color"].lower() == TARGET["color"].lower(),
            product["price"] is not None,
        ]
    )

    return product


def scan() -> None:
    log("Opening Apple refurbished catalog.")

    state = load_state()
    results = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        context = browser.new_context(
            locale="en-US",
            viewport={
                "width": 1440,
                "height": 1100,
            },
        )

        page = context.new_page()

        page.goto(
            CATALOG_URL,
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

        page.wait_for_timeout(2500)

        links = page.evaluate(
            """
            () => {
              const results = [];
              const seen = new Set();

              for (
                const link of document.querySelectorAll(
                  'a[href*="/shop/product/"]'
                )
              ) {
                const url = link.href;

                if (!url || seen.has(url)) continue;

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

                seen.add(url);

                results.push({
                  url,
                  text
                });
              }

              return results;
            }
            """
        )

        log(
            f"Found {len(links)} candidate product pages."
        )

        for index, link in enumerate(links, start=1):
            try:
                log(
                    f"Checking product {index}/{len(links)}."
                )

                product = scan_product_page(
                    page,
                    link["url"],
                )

                results.append(product)

                if not product["exact_match"]:
                    continue

                reason = should_alert(product, state)

                if reason:
                    send_discord(product, reason)

                state[product["url"]] = {
                    "price": product["price"],
                    "availability": product["availability"],
                    "last_seen": product["checked_at"],
                }

            except Exception as exc:
                log(
                    f"Product page failed: "
                    f"{link['url']}: {exc}"
                )

        browser.close()

    save_state(state)

    RESULTS_FILE.write_text(
        json.dumps(results, indent=2),
        encoding="utf-8",
    )

    exact_count = sum(
        1
        for product in results
        if product.get("exact_match")
    )

    summary = (
        f"Checked: "
        f"{datetime.now().astimezone().isoformat()}\n"
        f"Candidate pages: {len(links)}\n"
        f"Exact matches: {exact_count}\n"
    )

    SCAN_FILE.write_text(
        summary,
        encoding="utf-8",
    )

    log(summary.strip())


def main() -> None:
    log("Deal Hunter Server 0.2 starting.")

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
