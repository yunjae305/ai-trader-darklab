"""기술적 지표 계산 — LLM 이 아니라 파이썬이 한다.

LLM 에게 원시 캔들을 던지고 RSI 를 계산시키면 느리고 비싸고 틀린다.
여기서 숫자를 확정해서 넘기고, LLM 은 판단만 한다.

출처: invest/signals.py. 여기에는 매수·매도 결론이 없다 — 전부 측정값이다.

캔들은 과거→최신 순서의 list[dict]: {date, open, high, low, close, volume}
"""
from statistics import fmean, pstdev

RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BB_PERIOD, BB_SIGMA = 20, 2.0
ADX_PERIOD = 14
OBV_TREND = 20          # OBV 추세 판정 구간
PROFILE_BINS = 24       # 매물대 구간 수
VOLUME_SPIKE = 2.0   # 20일 평균 거래량 대비 배수


def _num(v):
    """'+70,000' / '-1200' / '72000' → float. 키움은 부호·콤마가 붙어 온다."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").lstrip("+")
    return float(s) if s else 0.0


def from_kiwoom(rows):
    """키움 ka10081(주식일봉차트) 응답 → 공통 캔들. 응답은 최신→과거라 뒤집는다.

    장 시작 전에는 오늘 자리에 거래량 0·시고저종이 모두 같은 기준가 봉이 하나 붙어 온다.
    체결이 없었으므로 그건 캔들이 아니다 — 지표에 넣으면 변동성 0 인 하루로 읽힌다.
    """
    out = [{"date": r["dt"], "open": abs(_num(r["open_pric"])), "high": abs(_num(r["high_pric"])),
            "low": abs(_num(r["low_pric"])), "close": abs(_num(r["cur_prc"])),
            "volume": _num(r["trde_qty"])} for r in rows]
    return [c for c in out[::-1] if c["volume"] > 0]


def from_toss(candles):
    """토스 GET /api/v1/candles 응답의 candles → 공통 캔들. 최신→과거라 뒤집는다."""
    out = [{"date": c["timestamp"][:10], "open": _num(c["openPrice"]), "high": _num(c["highPrice"]),
            "low": _num(c["lowPrice"]), "close": _num(c["closePrice"]),
            "volume": _num(c["volume"])} for c in candles]
    return out[::-1]


def sma(values, period):
    if len(values) < period:
        return None
    return fmean(values[-period:])


def ema_series(values, period):
    """전 구간 EMA. 첫 값은 SMA 로 시드한다."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [fmean(values[:period])]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, period=RSI_PERIOD):
    """Wilder RSI. 기간+1 개 종가가 필요하다."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100 - 100 / (1 + avg_g / avg_l)


def macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    """MACD. 절대값은 가격 단위라 종목 간 비교가 안 되므로 가격 대비 %도 함께 낸다.
    (NVDA 히스토 -0.5 와 삼성전자 +1314 를 그대로 비교하면 LLM 이 오판한다.)"""
    if len(closes) < slow + signal:
        return None
    fast_e, slow_e = ema_series(closes, fast), ema_series(closes, slow)
    fast_e = fast_e[len(fast_e) - len(slow_e):]          # 길이 맞추기
    line = [f - s for f, s in zip(fast_e, slow_e)]
    sig = ema_series(line, signal)
    if len(sig) < 2:
        return None
    line = line[len(line) - len(sig):]
    hist = [l - s for l, s in zip(line, sig)]
    # EMA 누적 오차 때문에 평평한 구간의 히스토그램이 정확히 0 이 아니라 1e-15 수준으로
    # 남는다. 그대로 두면 0 을 걸쳐 올라가는 교차를 놓친다.
    prev, cur = (0.0 if abs(h) < 1e-9 else h for h in (hist[-2], hist[-1]))
    cross = "golden" if prev <= 0 < cur else "dead" if prev >= 0 > cur else None
    last = closes[-1]
    return {"macd": line[-1], "signal": sig[-1], "histogram": hist[-1], "cross": cross,
            "macd_pct": line[-1] / last * 100 if last else 0.0,
            "histogram_pct": hist[-1] / last * 100 if last else 0.0}


def bollinger(closes, period=BB_PERIOD, sigma=BB_SIGMA):
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = fmean(window)
    sd = pstdev(window)
    upper, lower = mid + sigma * sd, mid - sigma * sd
    last = closes[-1]
    touch = "upper" if last >= upper else "lower" if last <= lower else None
    width = (upper - lower) / mid * 100 if mid else 0.0
    return {"upper": upper, "middle": mid, "lower": lower, "touch": touch, "width_pct": width}


def ma_alignment(closes):
    """5/20/60 정배열·역배열. 60일치가 없으면 None."""
    m5, m20, m60 = sma(closes, 5), sma(closes, 20), sma(closes, 60)
    if None in (m5, m20, m60):
        return {"ma5": m5, "ma20": m20, "ma60": m60, "alignment": None}
    align = "정배열" if m5 > m20 > m60 else "역배열" if m5 < m20 < m60 else "혼조"
    return {"ma5": m5, "ma20": m20, "ma60": m60, "alignment": align}


def volume_spikes(candles, lookback=20, factor=VOLUME_SPIKE):
    """평균 대비 factor 배 이상 터진 날과 그날의 가격 변동률."""
    out = []
    for i in range(lookback, len(candles)):
        base = fmean([c["volume"] for c in candles[i - lookback:i]])
        if base and candles[i]["volume"] >= base * factor:
            prev_close = candles[i - 1]["close"]
            change = (candles[i]["close"] - prev_close) / prev_close * 100 if prev_close else 0.0
            out.append({"date": candles[i]["date"], "volume": candles[i]["volume"],
                        "vs_avg": candles[i]["volume"] / base, "change_pct": change})
    return out


def _wilder(values, period):
    """Wilder 평활. 첫 값은 합, 이후 prev - prev/period + current."""
    if len(values) < period:
        return []
    out = [sum(values[:period])]
    for v in values[period:]:
        out.append(out[-1] - out[-1] / period + v)
    return out


def adx(candles, period=ADX_PERIOD):
    """추세 강도. ADX 25 이상이면 추세 있음, 15 이하면 추세 소실."""
    if len(candles) < period * 2 + 1:
        return None
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        ph, pl, pc = candles[i-1]["high"], candles[i-1]["low"], candles[i-1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, down = h - ph, pl - l
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    str_, sp, sm = _wilder(tr, period), _wilder(plus_dm, period), _wilder(minus_dm, period)
    dx = []
    for t, p, m in zip(str_, sp, sm):
        if t == 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        s = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / s if s else 0.0)
    if len(dx) < period:
        return None
    adx_series = _wilder(dx, period)
    if not adx_series:
        return None
    last_tr = str_[-1]
    pdi = 100 * sp[-1] / last_tr if last_tr else 0.0
    mdi = 100 * sm[-1] / last_tr if last_tr else 0.0
    return {"adx": adx_series[-1] / period, "plus_di": pdi, "minus_di": mdi,
            "trend": "상승" if pdi > mdi else "하락",
            "strength": "강함" if adx_series[-1] / period >= 25 else
                        "없음" if adx_series[-1] / period <= 15 else "보통"}


def obv(candles, trend_period=OBV_TREND):
    """매집도. 종가가 오른 날 거래량을 더하고 내린 날 뺀다."""
    if len(candles) < trend_period + 1:
        return None
    acc, series = 0.0, []
    for i in range(1, len(candles)):
        d = candles[i]["close"] - candles[i-1]["close"]
        acc += candles[i]["volume"] if d > 0 else -candles[i]["volume"] if d < 0 else 0.0
        series.append(acc)
    recent, past = series[-1], series[-trend_period]
    scale = max(abs(x) for x in series) or 1.0
    change = (recent - past) / scale
    return {"obv": recent, "change_ratio": change,
            "trend": "매집" if change > 0.05 else "분산" if change < -0.05 else "중립"}


def volume_profile_gap(candles, lookback=60, bins=PROFILE_BINS):
    """매물대 공백. 현재가 위쪽으로 거래가 적었던 구간이 넓으면 저항이 약하다."""
    window = candles[-lookback:]
    if len(window) < bins:
        return None
    lo = min(c["low"] for c in window)
    hi = max(c["high"] for c in window)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    buckets = [0.0] * bins
    for c in window:
        idx = min(int((c["close"] - lo) / step), bins - 1)
        buckets[idx] += c["volume"]
    avg = fmean(buckets)
    last = candles[-1]["close"]
    cur_bin = min(int((last - lo) / step), bins - 1)
    # 현재가 바로 위 구간들 중 거래량이 평균의 30% 미만인 연속 구간 폭
    above = bins - 1 - cur_bin
    if above == 0:
        # 구간 최상단 = 조회기간 신고가권. 위에 매물이 아예 없다 (막힌 게 아니라 뚫린 것)
        return {"gap_bins": 0, "gap_pct": 0.0, "bin_width_pct": step / last * 100 if last else 0.0,
                "room": "신고가권", "above_bins": 0}
    gap = 0
    for b in range(cur_bin + 1, bins):
        if buckets[b] < avg * 0.3:
            gap += 1
        else:
            break
    return {"gap_bins": gap, "gap_pct": gap * step / last * 100 if last else 0.0,
            "bin_width_pct": step / last * 100 if last else 0.0,
            "room": "넓음" if gap >= 3 else "보통" if gap >= 1 else "막힘",
            "above_bins": above}


def analyze(candles):
    """LLM 에 넘길 사실 묶음. 계산에 데이터가 모자라면 해당 항목은 None 으로 남긴다."""
    if not candles:
        raise ValueError("캔들이 비어 있음")
    closes = [c["close"] for c in candles]
    last = candles[-1]
    prev_close = candles[-2]["close"] if len(candles) > 1 else last["close"]
    r = rsi(closes)
    return {
        "date": last["date"],
        "close": last["close"],
        "change_pct": (last["close"] - prev_close) / prev_close * 100 if prev_close else 0.0,
        "candles_used": len(candles),
        "ma": ma_alignment(closes),
        "rsi14": r,
        "rsi_zone": None if r is None else ("과매수" if r >= 70 else "과매도" if r <= 30 else "중립"),
        "macd": macd(closes),
        "bollinger": bollinger(closes),
        "volume_spikes": volume_spikes(candles)[-5:],
        "adx": adx(candles),
        "obv": obv(candles),
        "profile_gap": volume_profile_gap(candles),
        "bullish_candle": last["close"] > last["open"],
        "volume_ratio": (last["volume"] / fmean([c["volume"] for c in candles[-21:-1]])
                         if len(candles) > 21 and fmean([c["volume"] for c in candles[-21:-1]]) else None),
    }
