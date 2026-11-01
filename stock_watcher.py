#!/usr/bin/env python3
"""Restock watcher for Target, Walmart and Best Buy (online + local), official seller only.

    pip install requests beautifulsoup4
    python stock_watcher.py            # loop forever
    python stock_watcher.py --once     # single pass (cron / GitHub Actions)
"""
import argparse
import json
import os
import sqlite3
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
TARGET_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"  # public key used by target.com
TARGET_API = "https://redsky.target.com/redsky_aggregations/v1/web"
OFFICIAL_WALMART_SELLERS = {"walmart.com", "walmart"}


def get_json(url, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


# Each checker returns (in_stock, price, where) where `where` lists e.g. ["online", "Store 1234"].

def check_bestbuy_api(item, cfg):
    """Official Best Buy API. Needs a free key from developer.bestbuy.com."""
    key, base = cfg["bestbuy_api_key"], f"https://api.bestbuy.com/v1/products/{item['sku']}"
    p = get_json(f"{base}.json", apiKey=key, show="onlineAvailability,salePrice")
    where = []
    if p.get("onlineAvailability"):
        where.append("online")
    if cfg.get("zip"):
        area = f"https://api.bestbuy.com/v1/stores(area({cfg['zip']},{cfg.get('radius_miles', 25)}))+products(sku={item['sku']})"
        stores = get_json(area, apiKey=key, format="json", show="storeId,name").get("stores", [])
        where += [f"store: {s.get('name', s.get('storeId'))}" for s in stores]
    return bool(where), p.get("salePrice"), where


BB_BUTTON = "https://www.bestbuy.com/api/tcfb/model.json"


def _bb_button_state(sku, zip_code, store_id=""):
    paths = [["shop", "buttonstate", "v5", "item", "skus", int(sku), "conditions", "NONE",
              "destinationZipCode", zip_code, "storeId", store_id, "context", "cyp",
              "addAll", "false"]]
    r = requests.get(BB_BUTTON, params={"paths": json.dumps(paths, separators=(",", ":")),
                                        "method": "get"},
                     headers={**HEADERS, "Referer": "https://www.bestbuy.com/"}, timeout=20)
    if r.status_code in (403, 429):
        raise RuntimeError("BLOCKED by Best Buy bot protection")
    r.raise_for_status()
    for d in _walk(r.json()):
        if "buttonState" in d:
            return d["buttonState"]
    return None


def _jsonld_price(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        for tag in BeautifulSoup(r.text, "html.parser").find_all("script", type="application/ld+json"):
            for d in _walk(json.loads(tag.string or "")):
                if "price" in d:
                    return float(str(d["price"]).replace(",", ""))
    except Exception:
        pass
    return None


def check_bestbuy_web(item, cfg):
    """No API key: uses the same button-state endpoint bestbuy.com uses (unofficial).
    Online = zip-based check. Local = the store IDs you list in `bestbuy_store_ids`."""
    zip_code, where = cfg["zip"], []
    if _bb_button_state(item["sku"], zip_code) == "ADD_TO_CART":
        where.append("online")
    for sid in cfg.get("bestbuy_store_ids", []):
        time.sleep(1)
        if _bb_button_state(item["sku"], zip_code, str(sid)) == "ADD_TO_CART":
            where.append(f"store #{sid}")
    price = _jsonld_price(item["url"]) if where and item.get("url") else None
    return bool(where), price, where


def check_bestbuy(item, cfg):
    key = cfg.get("bestbuy_api_key", "")
    if key and not key.startswith("YOUR_"):
        return check_bestbuy_api(item, cfg)
    return check_bestbuy_web(item, cfg)


def check_target(item, cfg):
    """Unofficial target.com (RedSky) endpoints. Item needs a `tcin` (the A-######## number in the URL)."""
    zip_code = cfg["zip"]
    stores = get_json(f"{TARGET_API}/nearby_stores_v1", key=TARGET_KEY, place=zip_code,
                      limit=cfg.get("max_stores", 20), within=cfg.get("radius_miles", 25))
    store_ids = [s["store_id"] for s in stores["data"]["nearby_stores"]["stores"]]
    where, price = [], None
    for i, sid in enumerate(store_ids):
        d = get_json(f"{TARGET_API}/pdp_client_v1", key=TARGET_KEY, tcin=item["tcin"],
                     store_id=sid, pricing_store_id=sid, zip=zip_code)["data"]["product"]
        price = (d.get("price") or {}).get("current_retail", price)
        ful = d.get("fulfillment") or {}
        if i == 0 and (ful.get("shipping_options") or {}).get("availability_status") == "IN_STOCK":
            where.append("online")
        for so in ful.get("store_options") or []:
            if (so.get("location_available_to_promise_quantity") or 0) > 0 or \
               (so.get("order_pickup") or {}).get("availability_status") == "IN_STOCK":
                where.append(f"store: {so.get('location_name', sid)}")
        time.sleep(1)
    return bool(where), price, where


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def check_walmart(item, cfg):
    """Reads the product page's embedded JSON. Online only, sold by Walmart itself.
    Walmart bot-blocks often; if you see BLOCKED errors, move this one to Playwright."""
    r = requests.get(item["url"], headers=HEADERS, timeout=20)
    if r.status_code in (403, 412, 429) or "robot or human" in r.text.lower():
        raise RuntimeError("BLOCKED by Walmart bot protection")
    tag = BeautifulSoup(r.text, "html.parser").find("script", id="__NEXT_DATA__")
    if not tag:
        raise RuntimeError("no product data found on page")
    for d in _walk(json.loads(tag.string)):
        if "availabilityStatus" in d and "sellerName" in d:
            if str(d["sellerName"]).lower() not in OFFICIAL_WALMART_SELLERS:
                return False, None, []  # third-party seller: ignore
            price = ((d.get("priceInfo") or {}).get("currentPrice") or {}).get("price")
            ok = d["availabilityStatus"] == "IN_STOCK"
            return ok, price, ["online"] if ok else []
    return None, None, []


def check_nintendo(item, cfg):
    """Nintendo's store loads stock status with JavaScript, so this uses a headless browser.
    Needs: pip install playwright && playwright install chromium. Online only."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(item["url"], wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # let the stock button render
        text = page.inner_text("body").lower()
        browser.close()
    if any(t in text for t in ("sold out", "out of stock", "currently unavailable")):
        return False, None, []
    if "add to cart" in text:
        return True, None, ["online"]
    return None, None, []  # couldn't tell


CHECKERS = {"nintendo": check_nintendo, "bestbuy": check_bestbuy, "target": check_target, "walmart": check_walmart}


def notify(cfg, title, msg, url):
    topic = cfg.get("ntfy_topic")
    if not topic:
        print(f"ALERT: {title} | {msg} | {url}")
        return
    requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(),
                  headers={"Title": title, "Click": url, "Priority": "high"}, timeout=20)


def run_pass(cfg, db):
    for item in cfg["items"]:
        name = item["name"]
        try:
            in_stock, price, where = CHECKERS[item["retailer"]](item, cfg)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            continue
        if in_stock is None:
            print(f"UNKNOWN {name}")
            continue

        # price guard against upsell pricing
        if in_stock and price and item.get("max_price") and float(price) > item["max_price"]:
            print(f"PRICE TOO HIGH {name}: ${price} > ${item['max_price']}")
            in_stock = False

        row = db.execute("SELECT in_stock FROM state WHERE name=?", (name,)).fetchone()
        was = bool(row[0]) if row else False
        db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (name, int(in_stock)))
        db.commit()
        print(f"{'IN STOCK' if in_stock else 'out'}: {name} {price or ''} {where}")

        if in_stock and not was:  # alert only on the transition
            notify(cfg, f"In stock: {name}", f"${price} - {', '.join(where)}", item.get("url", ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(args.config))
    cfg["ntfy_topic"] = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    cfg["zip"] = os.environ.get("ZIP") or cfg.get("zip", "")
    db = sqlite3.connect("state.db")
    db.execute("CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, in_stock INTEGER)")
    while True:
        run_pass(cfg, db)
        if args.once:
            break
        time.sleep(cfg.get("interval_minutes", 10) * 60)


if __name__ == "__main__":
    main()
