"""
美股夜盘实时报价 API
数据源优先级：Pyth Network Hermes > TradingView BOATS 页面抓取
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import time
import re

try:
    import requests
except ImportError:
    import urllib.request
    import urllib.error
    requests = None

HERMES = "https://hermes.pyth.network"
JINA = "https://r.jina.ai"
TRADINGVIEW_BOATS_URL = "https://www.tradingview.com/symbols/BOATS-{symbol}/"

# 简单内存缓存：{key: (timestamp, data)}
_cache = {}
CACHE_TTL = 30  # 秒


def _http_get(url, headers=None, timeout=10):
    """兼容 requests 和 urllib 的 GET 请求"""
    if requests:
        resp = requests.get(url, headers=headers or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    else:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")


def _http_get_json(url, params=None, timeout=10):
    """GET 请求返回 JSON"""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    text = _http_get(url, timeout=timeout)
    return json.loads(text)


def _get_cache(key):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _cache[key] = (time.time(), data)


def _search_pyth_feed(symbol, session="overnight"):
    """搜索 Pyth feed ID"""
    cache_key = f"pyth_feed:{symbol}:{session}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    session_map = {
        "overnight": "OVERNIGHT",
        "pre": "PRE MARKET",
        "post": "POST MARKET",
        "regular": "",
    }
    keyword = session_map.get(session, "OVERNIGHT")

    data = _http_get_json(
        f"{HERMES}/v2/price_feeds",
        params={"query": symbol, "asset_type": "equity"},
    )

    for f in data:
        desc = f.get("attributes", {}).get("description", "")
        disp = f.get("attributes", {}).get("display_symbol", "")
        if symbol.upper() not in disp.upper():
            continue
        if keyword and keyword in desc.upper():
            feed_id = f"0x{f['id']}"
            _set_cache(cache_key, feed_id)
            return feed_id
        if not keyword and "OVERNIGHT" not in desc and "PRE" not in desc and "POST" not in desc:
            feed_id = f"0x{f['id']}"
            _set_cache(cache_key, feed_id)
            return feed_id

    return None


def _get_pyth_price(feed_id):
    """从 Pyth Hermes 获取实时价格"""
    url = f"{HERMES}/v2/updates/price/latest"
    # 手动拼接，因为 ids[] 参数格式特殊
    full_url = f"{url}?ids%5B%5D={feed_id}&parsed=true"
    text = _http_get(full_url)
    data = json.loads(text)

    if "parsed" not in data or len(data["parsed"]) == 0:
        return None

    p = data["parsed"][0]["price"]
    ema = data["parsed"][0].get("ema_price", {})

    price = int(p["price"]) * (10 ** int(p["expo"]))
    conf = int(p["conf"]) * (10 ** int(p["expo"]))
    publish_time = int(p["publish_time"])

    ema_price = None
    if ema:
        ema_price = int(ema.get("price", 0)) * (10 ** int(ema.get("expo", 0)))

    return {
        "price": round(price, 4),
        "confidence": round(conf, 4),
        "ema_price": round(ema_price, 4) if ema_price else None,
        "publish_time": publish_time,
    }


def _get_tradingview_price(symbol):
    """从 TradingView BOATS 页面抓取夜盘价格"""
    url = TRADINGVIEW_BOATS_URL.format(symbol=symbol.upper())
    jina_url = f"{JINA}/{url}"

    text = _http_get(
        jina_url,
        headers={"Accept": "text/plain", "User-Agent": "Mozilla/5.0"},
        timeout=15,
    )

    # 匹配价格模式: "72.06 R USD" 或 "72.06 USD R"
    patterns = [
        r"(\d+\.\d+)\s*R?\s*USD",
        r"RKLB Market (?:open|closed)\s*\n\s*(\d+\.\d+)",
        r"(\d+\.\d+)\s+USD\s+R",
    ]

    # 先找 "Market open" 附近的价格（最可靠）
    market_match = re.search(
        r"Market\s+(?:open|closed)\s*\n\s*(\d+\.\d+)", text, re.IGNORECASE
    )
    if market_match:
        return float(market_match.group(1))

    # 通用标题匹配
    title_match = re.search(
        rf"{symbol.upper()}.*?(\d+\.\d+)\s*R?\s*USD", text, re.IGNORECASE
    )
    if title_match:
        return float(title_match.group(1))

    # 退而求其次，匹配第一个合理的价格
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            price = float(m.group(1))
            if 0.01 < price < 100000:
                return price

    return None


def get_overnight_quote(symbol, session="overnight"):
    """获取夜盘报价，优先 Pyth，fallback TradingView"""
    symbol = symbol.upper().strip()

    cache_key = f"quote:{symbol}:{session}"
    cached = _get_cache(cache_key)
    if cached:
        cached["cached"] = True
        return cached

    # 1. 尝试 Pyth
    feed_id = _search_pyth_feed(symbol, session)
    if feed_id:
        pyth_data = _get_pyth_price(feed_id)
        if pyth_data and (time.time() - pyth_data["publish_time"]) < 300:
            result = {
                "symbol": symbol,
                "price": pyth_data["price"],
                "confidence": pyth_data["confidence"],
                "ema_price": pyth_data["ema_price"],
                "session": session,
                "source": "pyth_hermes",
                "feed_id": feed_id,
                "timestamp": pyth_data["publish_time"],
                "cached": False,
            }
            _set_cache(cache_key, result)
            return result

    # 2. Fallback: TradingView BOATS
    if session == "overnight":
        tv_price = _get_tradingview_price(symbol)
        if tv_price:
            result = {
                "symbol": symbol,
                "price": tv_price,
                "confidence": None,
                "ema_price": None,
                "session": session,
                "source": "tradingview_boats",
                "feed_id": None,
                "timestamp": int(time.time()),
                "cached": False,
            }
            _set_cache(cache_key, result)
            return result

    return None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        symbol = params.get("symbol", [None])[0]
        session = params.get("session", ["overnight"])[0]

        # CORS headers
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=30")
        self.end_headers()

        if not symbol:
            resp = {
                "error": "Missing 'symbol' parameter",
                "usage": "/api/quote?symbol=AAPL&session=overnight",
                "sessions": ["overnight", "pre", "post", "regular"],
            }
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
            return

        try:
            result = get_overnight_quote(symbol, session)
            if result:
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
            else:
                resp = {
                    "error": f"No data found for {symbol} ({session})",
                    "hint": "This symbol may not have overnight feed on Pyth or TradingView BOATS",
                }
                self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
        except Exception as e:
            resp = {"error": str(e)}
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
