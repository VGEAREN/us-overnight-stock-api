# Overnight Stock API

美股夜盘(20:00-04:00 ET)实时报价 API，部署在 Vercel 上。

## API 用法

```
GET /api/quote?symbol=AAPL&session=overnight
```

### 参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `symbol` | 是 | - | 股票代码，如 AAPL、TSLA、RKLB |
| `session` | 否 | overnight | 交易时段：overnight / pre / post / regular |

### 返回示例

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
  "cached": false
}
```

### 数据源

优先级：
1. **Pyth Network Hermes API** — 免费实时，覆盖热门股票（AAPL、TSLA、NVDA、MSFT 等）
2. **TradingView BOATS 页面** — 作为 fallback，覆盖 Blue Ocean ATS 所有交易标的

| 数据源 | source 字段 | 延迟 | 有 confidence |
|--------|-------------|------|:-------------:|
| Pyth Hermes | `pyth_hermes` | <1秒 | 是 |
| TradingView | `tradingview_boats` | ~3秒 | 否 |

## 部署

### 1. Fork 或 clone 本仓库

### 2. 连接 Vercel

```bash
npm i -g vercel
vercel login
vercel --prod
```

或直接在 [vercel.com](https://vercel.com) 导入 GitHub 仓库，零配置自动部署。

### 3. 访问

部署后 API 地址为：
```
https://your-project.vercel.app/api/quote?symbol=RKLB
```

## 本地测试

```bash
pip install requests
python -c "
from api.quote import get_overnight_quote
import json
print(json.dumps(get_overnight_quote('RKLB'), indent=2))
"
```

## 限制

- Pyth 限速 30 请求/10 秒/IP
- TradingView fallback 较慢（~3 秒），有被限流风险
- 内置 30 秒内存缓存减少上游请求
- 夜盘仅在 ET 20:00-04:00 有实时数据，其他时段返回最后成交价
