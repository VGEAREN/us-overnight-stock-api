# Overnight Stock API

美股夜盘(20:00-04:00 ET)实时报价 API，部署在 Vercel 上。

## 在线地址

```
https://us-overnight-stock-api.vercel.app
```

## 快速调用

```bash
# 获取 AAPL 夜盘实时价格（Pyth 数据源，毫秒级）
curl "https://us-overnight-stock-api.vercel.app/api?symbol=AAPL"

# 获取 RKLB 夜盘价格（TradingView fallback）
curl "https://us-overnight-stock-api.vercel.app/api?symbol=RKLB"

# 指定交易时段
curl "https://us-overnight-stock-api.vercel.app/api?symbol=AAPL&session=pre"
```

## API 文档

### 请求

```
GET /api?symbol={symbol}&session={session}
```

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `symbol` | 是 | - | 股票代码，如 AAPL、TSLA、RKLB |
| `session` | 否 | overnight | 交易时段：overnight / pre / post / regular |

### 返回

#### Pyth 数据源（热门股票）

```json
{
  "symbol": "AAPL",
  "price": 249.60,
  "confidence": 0.155,
  "ema_price": 249.57,
  "session": "overnight",
  "source": "pyth_hermes",
  "feed_id": "0x241b...",
  "timestamp": 1773976587,
  "cached": false,
  "cache_from": null
}
```

#### TradingView fallback（如 RKLB）

```json
{
  "symbol": "RKLB",
  "price": 72.06,
  "confidence": null,
  "ema_price": null,
  "session": "overnight",
  "source": "tradingview_boats",
  "feed_id": null,
  "timestamp": 1773976591,
  "cached": false,
  "cache_from": null
}
```

#### 缓存命中时

```json
{
  "symbol": "AAPL",
  "price": 249.60,
  "cached": true,
  "cache_from": "memory"
}
```

#### 错误

```json
{
  "error": "No data found for XYZ (overnight)",
  "hint": "This symbol may not have overnight feed on Pyth or TradingView BOATS"
}
```

### 返回字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | string | 股票代码 |
| `price` | float | 实时价格 |
| `confidence` | float/null | 价格置信区间（仅 Pyth） |
| `ema_price` | float/null | 指数移动平均价（仅 Pyth） |
| `session` | string | 交易时段 |
| `source` | string | 数据源：`pyth_hermes` 或 `tradingview_boats` |
| `feed_id` | string/null | Pyth feed ID（仅 Pyth） |
| `timestamp` | int | 数据时间戳（Unix 秒） |
| `cached` | bool | 是否命中缓存 |
| `cache_from` | string/null | 缓存来源：`memory` / `redis` / null |

## 数据源

优先级：
1. **Pyth Network Hermes API** — 免费实时，覆盖热门股票（AAPL、TSLA、NVDA、MSFT 等 380+ 标的）
2. **TradingView BOATS 页面** — 作为 fallback，覆盖 Blue Ocean ATS 所有交易标的

| 数据源 | source 字段 | 延迟 | 有 confidence | 适用 |
|--------|-------------|------|:-------------:|------|
| Pyth Hermes | `pyth_hermes` | <1秒 | 是 | 热门股票/ETF |
| TradingView | `tradingview_boats` | ~3秒 | 否 | Pyth 未覆盖的标的 |

## 缓存架构

```
请求 → [L0 Vercel CDN] → [L1 内存] → [L2 Redis] → 上游数据源
         15s + 45s SWR      30s TTL     30s TTL     Pyth / TradingView
```

| 层级 | 说明 | TTL |
|------|------|-----|
| L0 Vercel CDN | 同 URL 直接在边缘节点返回 | 15s + 45s stale-while-revalidate |
| L1 内存 | 同一函数实例内 0ms 响应 | 30s |
| L2 Upstash Redis | 跨实例共享（可选，需配置环境变量） | 30s |
| Feed ID 缓存 | Pyth feed ID 几乎不变 | 24h |

### 配置 Upstash Redis（可选）

在 Vercel 项目设置中添加环境变量：

```
UPSTASH_REDIS_REST_URL=https://xxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=AXxx...
```

不配置也能正常工作，仅依赖 CDN + 内存缓存。

## 部署

### 方式一：Vercel 导入（推荐）

1. 在 [vercel.com](https://vercel.com) 用 GitHub 登录
2. Import → 选择本仓库
3. 点 Deploy

### 方式二：Vercel CLI

```bash
npm i -g vercel
vercel login
vercel --prod
```

## 本地测试

```bash
pip install requests
python -c "
from api.index import get_overnight_quote
import json
print(json.dumps(get_overnight_quote('RKLB'), indent=2))
"
```

## 限制

- Pyth 限速 30 请求/10 秒/IP
- TradingView fallback 较慢（~3 秒），高频调用可能被限流
- 夜盘仅在 ET 20:00-04:00 有实时数据，其他时段返回最后成交价
- Pyth 与 Blue Ocean ATS 合作到 2026 年底
