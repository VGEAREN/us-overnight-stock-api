"""
美股夜盘实时报价 API
数据源优先级：Pyth Network Hermes > TradingView BOATS 页面抓取
缓存层级：Vercel CDN 边缘缓存 > Upstash Redis > 内存缓存
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import time
import re

try:
    import requests as _requests
except ImportError:
    import urllib.request
    import urllib.error
    _requests = None

HERMES = "https://hermes.pyth.network"
JINA = "https://r.jina.ai"
TRADINGVIEW_BOATS_URL = "https://www.tradingview.com/symbols/BOATS-{symbol}/"

# 缓存配置
QUOTE_CACHE_TTL = 30       # 报价缓存 30 秒
FEED_ID_CACHE_TTL = 86400  # feed ID 缓存 24 小时（几乎不变）
CDN_MAXAGE = 15            # Vercel CDN 边缘缓存 15 秒
CDN_SWR = 45               # stale-while-revalidate 45 秒

# Upstash Redis 配置（从环境变量读取，可选）
UPSTASH_REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

# ============================================================
# 缓存层
# ============================================================

# L1: 内存缓存（同一函数实例内有效）
_mem_cache = {}


def _mem_get(key):
    if key in _mem_cache:
        ts, ttl, data = _mem_cache[key]
        if time.time() - ts < ttl:
            return data
    return None


def _mem_set(key, data, ttl):
    _mem_cache[key] = (time.time(), ttl, data)


# L2: Upstash Redis（跨实例持久缓存，可选）
def _redis_get(key):
    if not UPSTASH_REDIS_URL:
        return None
    try:
        url = f"{UPSTASH_REDIS_URL}/get/{key}"
        headers = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}
        text = _http_get(url, headers=headers, timeout=3)
        data = json.loads(text)
        result = data.get("result")
        if result:
            return json.loads(result)
    except Exception:
        pass
    return None


def _redis_set(key, data, ttl):
    if not UPSTASH_REDIS_URL:
        return
    try:
        url = f"{UPSTASH_REDIS_URL}/set/{key}"
        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = json.dumps(data, ensure_ascii=False)
        # SET key value EX ttl
        set_url = f"{UPSTASH_REDIS_URL}/set/{key}/{payload}/ex/{ttl}"
        # Upstash REST: GET-style command encoding
        cmd_url = f"{UPSTASH_REDIS_URL}/pipeline"
        body = json.dumps([["SET", key, payload, "EX", str(ttl)]])
        if _requests:
            _requests.post(cmd_url, headers=headers, data=body, timeout=3)
        else:
            req = urllib.request.Request(
                cmd_url, data=body.encode(), headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


def cache_get(key):
    """三级读取：内存 → Redis"""
    # L1
    val = _mem_get(key)
    if val is not None:
        return val, "memory"
    # L2
    val = _redis_get(key)
    if val is not None:
        _mem_set(key, val, QUOTE_CACHE_TTL)  # 回填内存
        return val, "redis"
    return None, None


def cache_set(key, data, ttl=QUOTE_CACHE_TTL):
    """同时写入内存和 Redis"""
    _mem_set(key, data, ttl)
    _redis_set(key, data, ttl)


# ============================================================
# HTTP 工具
# ============================================================

def _http_get(url, headers=None, timeout=10):
    if _requests:
        resp = _requests.get(url, headers=headers or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    else:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")


def _http_get_json(url, params=None, timeout=10):
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    text = _http_get(url, timeout=timeout)
    return json.loads(text)


# ============================================================
# 数据源
# ============================================================

def _search_pyth_feed(symbol, session="overnight"):
    """搜索 Pyth feed ID（结果缓存 24 小时）"""
    cache_key = f"pyth_feed:{symbol}:{session}"
    cached, _ = cache_get(cache_key)
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
            cache_set(cache_key, feed_id, FEED_ID_CACHE_TTL)
            return feed_id
        if not keyword and "OVERNIGHT" not in desc and "PRE" not in desc and "POST" not in desc:
            feed_id = f"0x{f['id']}"
            cache_set(cache_key, feed_id, FEED_ID_CACHE_TTL)
            return feed_id

    # 缓存"不存在"结果，避免反复搜索
    cache_set(cache_key, "", FEED_ID_CACHE_TTL)
    return None


def _get_pyth_price(feed_id):
    """从 Pyth Hermes 获取实时价格"""
    full_url = f"{HERMES}/v2/updates/price/latest?ids%5B%5D={feed_id}&parsed=true"
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

    # 先找 "Market open/closed" 附近的价格（最可靠）
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

    # 退而求其次
    for pattern in [r"(\d+\.\d+)\s*R?\s*USD", r"(\d+\.\d+)\s+USD\s+R"]:
        m = re.search(pattern, text)
        if m:
            price = float(m.group(1))
            if 0.01 < price < 100000:
                return price

    return None


# ============================================================
# 业务逻辑
# ============================================================

def get_overnight_quote(symbol, session="overnight"):
    """获取报价，三级缓存 + 双数据源"""
    symbol = symbol.upper().strip()

    # 查缓存
    cache_key = f"quote:{symbol}:{session}"
    cached, cache_from = cache_get(cache_key)
    if cached:
        cached["cached"] = True
        cached["cache_from"] = cache_from
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
                "cache_from": None,
            }
            cache_set(cache_key, result, QUOTE_CACHE_TTL)
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
                "cache_from": None,
            }
            cache_set(cache_key, result, QUOTE_CACHE_TTL)
            return result

    return None


# ============================================================
# Vercel Serverless Handler
# ============================================================

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        symbol = params.get("symbol", [None])[0]
        session = params.get("session", ["overnight"])[0]

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        # L0: Vercel CDN 边缘缓存 — 同 URL 请求在 CDN 层直接返回
        self.send_header(
            "Cache-Control",
            f"s-maxage={CDN_MAXAGE}, stale-while-revalidate={CDN_SWR}"
        )
        self.end_headers()

        if not symbol:
            resp = {
                "error": "Missing 'symbol' parameter",
                "usage": "/api/quote?symbol=AAPL&session=overnight",
                "sessions": ["overnight", "pre", "post", "regular"],
                "cache_config": {
                    "cdn_edge": f"{CDN_MAXAGE}s + {CDN_SWR}s SWR",
                    "redis": "enabled" if UPSTASH_REDIS_URL else "disabled (set UPSTASH_REDIS_REST_URL)",
                    "memory": f"{QUOTE_CACHE_TTL}s",
                },
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
