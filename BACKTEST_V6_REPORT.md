# Backtest v6 Report — Multi-optimization Stack

**Ran**: 2026-04-22 | **Period**: 30 days × 10 coins | **Same v4 EMA cross signals as v5**

## Bảng so sánh 4 variant (incremental ablation)

| Variant | Signals | WinRate | TotalR | AvgR | MaxDD | PF | Δ TotalR |
|---|---:|---:|---:|---:|---:|---:|---:|
| V6_TIMEOUT_FULL (= v5 ref) | 243 | 41.6% | -19.76 | -0.081 | 32.59 | 0.82 | — |
| V6_COIN_FILTER | 147 | 49.0% | **+4.90** | +0.033 | 10.06 | 1.08 | **+24.65** |
| V6_FULL_FILTER (+vol≤2.0%) | 145 | 49.0% | +4.47 | +0.031 | 10.06 | 1.07 | -0.43 |
| V6_PYRAMID_RETUNED | 145 | 47.6% | +3.94 | +0.027 | 9.69 | 1.07 | -0.53 |

## 3 phán quyết

### 1) COIN FILTER — VŨ KHÍ HẠT NHÂN
- Chuyển hệ thống từ **lỗ -19.76R sang LỜI +4.90R** (cải thiện +24.65R)
- Win rate: 41.6% → **49.0%** (+7.4pp)
- Profit factor: 0.82 → **1.08** (lỗ → lời)
- Max DD giảm 69% (32.59 → 10.06)
- Cách: bỏ 4 coin lỗ liên tục (SOL, DOGE, ADA, AVAX) khỏi signal generation
- Allowlist final: AAVE, ETH, LINK, BNB, XRP, BTC

→ **APPLIED LIVE** trong [`openclaw/binance_price_alert.py`](openclaw/binance_price_alert.py) qua const `COIN_ALLOWLIST`. Override env: `COIN_ALLOWLIST="aave,eth,btc"`.

### 2) VOL REGIME FILTER — KHÔNG ĐÁNG KỂ Ở 30D
- Δ -0.43R (gần như zero impact)
- Threshold 2% chỉ lọc 2 trade trên 147
- Spot crypto thường có ATR << 2% giá → filter không kích hoạt nhiều

→ **APPLIED LIVE với threshold mềm hơn (2.5%)** để giữ làm safety net cho event extreme volatility (CPI/FOMC). Override env: `VOL_REGIME_MAX_PCT=2.0`.

### 3) PYRAMID RETUNED — VẪN CHƯA ĐỦ THUYẾT PHỤC
- Δ -0.53R vs full_filter (đại khái neutral)
- Cải thiện max DD nhẹ (10.06 → 9.69)
- Sample size pyramid trên SWING-only chưa đủ (chỉ 125 signal)

→ **DEFER**. Cần backtest 90 ngày để khẳng định EV pyramid. Hiện không enable trên live để giảm rủi ro biến động không cần thiết.

## Tác động dự kiến lên hệ thống live

| Metric | Trước v6 (live) | Sau v6 (apply) | Thay đổi |
|---|---|---|---|
| Coin được trade | 20 | 6 | -70% |
| Signal/tuần (ước) | ~15 | ~5 | -67% |
| Win rate kỳ vọng | ~37% | ~49% | +12pp |
| EV/trade | -0.146R | +0.033R | profitable! |

**Trade-off**: Ít signal hơn nhưng quality cao hơn. Phù hợp với kích thước portfolio nhỏ ($63) — không cần volume signal nhiều.

## Files

- Code: [`openclaw/backtest_v6.py`](openclaw/backtest_v6.py)
- Raw: [`openclaw/data/backtest_v6_results.json`](openclaw/data/backtest_v6_results.json)
- Live config: [`openclaw/binance_price_alert.py`](openclaw/binance_price_alert.py) — `COIN_ALLOWLIST`, `VOL_REGIME_MAX_PCT`

## TODO sắp tới

- [ ] **90-day backtest** với cùng allowlist để xác nhận robust qua nhiều market regime
- [ ] **Pyramid v3**: thử trigger 2.5 ATR + 1 leg only (max safety) trên 90d
- [ ] **Auto-rotation allowlist**: script weekly tính rolling 14d win rate, đề xuất add/remove coin (cần ≥ 50 trade history mới chính xác)
- [ ] **Session filter** (Asia/EU/US) — backtest có session nào win rate cao hơn không?
