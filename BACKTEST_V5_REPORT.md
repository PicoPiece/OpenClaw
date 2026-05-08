# Backtest v5 Report — Timeout-aware + Pyramiding

**Ran**: 2026-04-22 | **Period**: 30 days × 10 top coins | **Total signals**: 243

## Kết quả tóm tắt

| Variant | Signals | Win rate | Total R | Avg R | Max DD | Avg Hold | PF |
|---|---:|---:|---:|---:|---:|---:|---:|
| V5_BASELINE | 243 | 37.0% | **-35.57** | -0.146 | 45.73 | 12.6h | 0.78 |
| V5_TIMEOUT  | 243 | **41.6%** | **-19.76** | **-0.081** | **32.59** | **5.3h** | **0.82** |
| V5_PYRAMID  | 243 | 39.1% | -22.59 | -0.093 | 34.45 | 5.3h | 0.80 |

## Phán quyết

### 1) TIMEOUT — THẮNG RÕ RỆT vs BASELINE
- **Win rate +4.5pp** (37.0 → 41.6%)
- **Loss giảm 44%** (-35.57R → -19.76R)
- **Max DD giảm 29%** (45.73 → 32.59)
- **Hold time giảm 58%** (12.6h → 5.3h) — capital free nhanh hơn
- Profit factor cải thiện 0.78 → 0.82

⇒ **Patch timeout vừa apply lên live là quyết định đúng.** Kết quả backtest xác nhận giả thuyết "lệnh ko kéo dài quá lâu" cải thiện EV thật sự.

Outcomes breakdown TIMEOUT:
- TP: 48 (20%)
- SL: 85 (35%)
- TIMEOUT_PROFIT: 43 (18%) — lock được lời
- TIMEOUT_BE: 36 (15%) — thoát hòa, tránh lỗ thêm
- TIMEOUT_LOSS: 31 (13%) — cắt sớm trước SL

→ 33% trade thoát qua timeout policy, trong đó **lock profit gấp 1.4× cut loss** ⇒ policy đúng hướng.

### 2) PYRAMID — GẦN HÒA vs TIMEOUT, NHƯNG CÓ INSIGHT QUAN TRỌNG
- Total R: **-22.59** (kém TIMEOUT -2.83R)
- Win rate: **39.1%** (kém 2.5pp)
- Max DD: 34.45 (xấu hơn 1.86R)

**Nhưng**:
- 36/243 trades (15%) actually got pyramid add
- **R từ trades có pyramid: +26.66R** (rất tốt)
- **R từ trades không pyramid: -49.25R** (chính là phần đáng kể của tổng lỗ)

⇒ Khi pyramid HOẠT ĐỘNG (HTF align + giá chạy đủ +1.5 ATR), nó RẤT lời. Vấn đề là chase trail SL trong pyramid LÀM LỠ một số trade lẽ ra sẽ hit TP — vì SL bị lift quá sớm khi +3 ATR profit.

### 3) Vấn đề LỚN HƠN backtest hé lộ

**Baseline ăn -35.57R / 30 ngày = -1.18R/ngày = -2.4% portfolio/ngày @ 2% risk per trade.** Cả 3 variant đều âm. Điều đó nghĩa là:

- **Tín hiệu EMA20/50 cross v4 hiện không profitable** trong regime 30 ngày qua
- Win rate 37–42% với R:R ~1.3 → break-even cần win rate ≥ 43%
- Phần lớn coin lỗ (7/10 lỗ baseline, 6/10 lỗ timeout)
- Coin sáng giá nhất: AAVE (+10R baseline), ETH (sau timeout)
- Coin tệ nhất: SOL (-13R baseline), DOGE (-9R)

⇒ **Trước khi nói chuyện pyramid lớn, phải fix signal source trước.**

## Đề xuất tiếp theo

### Ưu tiên 1: KEEP timeout patch (đã apply live, backtest confirm)
✓ Already done.

### Ưu tiên 2: CẢI THIỆN signal quality (signal-level fix)
Cần làm 1 trong 4:

a. **Coin universe filter động**: chỉ trade top 5 coin có win rate > 45% rolling 14 ngày. Backtest cho thấy AAVE/ETH/BNB/LINK/XRP đều > 40% với timeout, còn lại tệ.

b. **EMA cross + RSI + MACD divergence** (thêm 1 filter): hiện chỉ EMA + RSI momentum. Add divergence check lọc bớt false breakout.

c. **Volatility regime filter**: skip signal khi ATR/price > 2% (high vol = low win rate trong v4)

d. **Time-of-day filter**: backtest theo session (Asia/EU/US) xem session nào win rate cao hơn

### Ưu tiên 3: PYRAMID — cần chỉnh lại 2 thứ trước khi enable

a. **Bỏ chase trail SL trong pyramid**: chase ăn lẹm vào TP. Chỉ giữ leg SL anchoring (leg 1 SL = entry gốc, leg 2 SL = entry leg 1).

b. **Add condition chặt hơn**: đợi profit ≥ 2.0 ATR (thay vì 1.5) để filter false breakouts. Chỉ pyramid SWING mode (skip SCALP).

→ Cần backtest lần 2 với 2 thay đổi này trước khi enable live.

## Files

- Code: [`openclaw/backtest_v5.py`](openclaw/backtest_v5.py)
- Raw results: [`openclaw/data/backtest_v5_results.json`](openclaw/data/backtest_v5_results.json)
- Reuse: [`openclaw/backtest_v3_v4.py`](openclaw/backtest_v3_v4.py) (indicators + signal detection)

## Cách chạy lại

```bash
cd /home/picopiece/openclaw
python3 backtest_v5.py
```
