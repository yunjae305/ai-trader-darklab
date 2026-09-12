"""퀀트 점수 — 5요소 × 20점 = 0~100. 판단이 아니라 측정이다.

출처: invest/quant.py. 원본에서는 이 점수가 매매 판단의 주체였다 (score >= 65 면 매수).
여기서는 그 역할을 떼었다 — 다크랩의 판단 주체는 brain 하나뿐이고, 이 점수는 brain 이
보는 여러 관측치 중 하나다. 그래서 원본의 verdict("강매수"/"매수"/"관망") 와 buy(bool),
그리고 buy_threshold 게이트는 옮기지 않았다. 점수와 그 근거만 남긴다.

mode: trend = 오른 종목 추종 / dip = 비이성적 하락 매수. 어느 쪽으로 볼지도 결국 brain 몫이라
둘 다 계산해서 넘길 수 있다 (measure_both).
"""

COMPONENTS = ("momentum", "volume", "volatility", "trend", "oscillator")
MODES = ("trend", "dip")    # trend=오른 종목 추종 / dip=비이성적 하락 매수


def _clamp(v, lo=0.0, hi=20.0):
    return max(lo, min(hi, v))


def momentum_score(facts):
    """가격 모멘텀 — 5일·20일 이평 대비 이격. 0~20점."""
    ma = facts["ma"]
    close = facts["close"]
    if ma["ma5"] is None or ma["ma20"] is None:
        return 0.0, "모멘텀 계산 불가"
    d5 = (close - ma["ma5"]) / ma["ma5"] * 100
    d20 = (close - ma["ma20"]) / ma["ma20"] * 100
    # +10% 이격이면 만점. 음수면 0점.
    raw = (d5 * 0.4 + d20 * 0.6) / 10 * 20
    return _clamp(raw), f"이격 5일 {d5:+.1f}% / 20일 {d20:+.1f}%"


def volume_score(facts):
    """거래량 변화 — 20일 평균 대비 배수. 0~20점."""
    vr = facts["volume_ratio"]
    if vr is None:
        return 0.0, "거래량 비율 계산 불가"
    # 1배=0점, 3배 이상=만점
    return _clamp((vr - 1.0) / 2.0 * 20), f"거래량 {vr:.1f}배"


def volatility_score(facts):
    """변동성 — 볼린저 폭. 너무 좁으면 움직임이 없고, 너무 넓으면 위험. 0~20점."""
    b = facts["bollinger"]
    if not b:
        return 0.0, "변동성 계산 불가"
    w = b["width_pct"]
    # 4~12% 구간을 최적으로 본다. 벗어날수록 감점.
    if w <= 0:
        return 0.0, "변동성 0"
    if 4 <= w <= 12:
        s = 20.0
    elif w < 4:
        s = w / 4 * 20
    else:
        s = max(0.0, 20 - (w - 12) * 1.5)
    return _clamp(s), f"밴드폭 {w:.1f}%"


def trend_score(facts):
    """이동평균 추세 — 정배열 + ADX 추세 강도. 0~20점."""
    ma, adx = facts["ma"], facts["adx"]
    s, parts = 0.0, []
    if ma["alignment"] == "정배열":
        s += 10
        parts.append("정배열")
    elif ma["alignment"] == "혼조":
        s += 4
        parts.append("혼조")
    else:
        parts.append(ma["alignment"] or "판정불가")
    if adx:
        if adx["trend"] == "상승":
            s += min(adx["adx"] / 40 * 10, 10)
            parts.append(f"ADX {adx['adx']:.0f} 상승")
        else:
            parts.append(f"ADX {adx['adx']:.0f} 하락")
    return _clamp(s), " · ".join(parts)


def oscillator_score(facts):
    """RSI / MACD — 과열도와 모멘텀 전환. 0~20점."""
    rsi, m = facts["rsi14"], facts["macd"]
    if rsi is None or not m:
        return 0.0, "오실레이터 계산 불가"
    # RSI 45~65 를 최적으로 본다. 과매수(>75)·과매도(<30)는 감점.
    if 45 <= rsi <= 65:
        r = 10.0
    elif rsi < 45:
        r = max(0.0, 10 - (45 - rsi) * 0.4)
    else:
        r = max(0.0, 10 - (rsi - 65) * 0.5)
    # MACD 히스토그램이 양수면 가점, 골든크로스면 만점
    h = m["histogram_pct"]
    mm = 10.0 if m["cross"] == "golden" else _clamp(h * 10 + 5, 0, 10)
    return _clamp(r + mm), f"RSI {rsi:.0f} · MACD히스토 {h:+.2f}%" + (
        " · 골든크로스" if m["cross"] == "golden" else "")


# ── dip 모드 ──────────────────────────────────────────────────────────────
# "비이성적 하락 시 추가 매수". trend 모드와 방향이 반대다.
# 전제는 펀더멘털 게이트다 (fundamentals.is_intact). 게이트 없이 쓰면 그냥 떨어지는 칼이다.

def dip_momentum_score(facts):
    """얼마나 빠졌나 — 20일선 아래로 내려갈수록 가점. 0~20점."""
    ma, close = facts["ma"], facts["close"]
    if ma["ma20"] is None:
        return 0.0, "이격 계산 불가"
    d20 = (close - ma["ma20"]) / ma["ma20"] * 100
    # -15% 이격이면 만점, 0% 이상이면 0점
    return _clamp(-d20 / 15 * 20), f"20일선 이격 {d20:+.1f}%"


def dip_trend_score(facts):
    """장기 추세는 살아 있는가 — 60일선 위면 가점. 하락 추세 전체는 피한다. 0~20점."""
    ma, adx, close = facts["ma"], facts["adx"], facts["close"]
    if ma["ma60"] is None:
        return 0.0, "60일선 없음"
    d60 = (close - ma["ma60"]) / ma["ma60"] * 100
    s, parts = 0.0, [f"60일선 이격 {d60:+.1f}%"]
    if d60 > 0:
        s += 12          # 장기 상승 추세 안에서의 눌림
        parts.append("장기추세 유지")
    elif d60 > -10:
        s += 6
        parts.append("60일선 부근")
    else:
        parts.append("장기추세 이탈")
    if adx and adx["strength"] == "없음":
        s += 8           # 추세 소실 = 패닉이 끝나가는 국면
        parts.append("추세 소실")
    elif adx and adx["trend"] == "하락" and adx["adx"] >= 40:
        parts.append(f"강한 하락추세 ADX {adx['adx']:.0f}")   # 가점 없음
    else:
        s += 4
    return _clamp(s), " · ".join(parts)


def dip_oscillator_score(facts):
    """과매도일수록 가점. trend 모드와 정확히 반대. 0~20점."""
    rsi, b = facts["rsi14"], facts["bollinger"]
    if rsi is None:
        return 0.0, "RSI 계산 불가"
    # RSI 30 이하 만점, 60 이상 0점
    r = _clamp((60 - rsi) / 30 * 12, 0, 12)
    bb = 8.0 if (b and b["touch"] == "lower") else 0.0
    return _clamp(r + bb), f"RSI {rsi:.0f}" + (" · 볼린저 하단 접촉" if bb else "")


DIP_SCORERS = {
    "momentum": dip_momentum_score,
    "volume": volume_score,          # 투매 거래량은 양쪽 모드에서 같은 의미
    "volatility": volatility_score,
    "trend": dip_trend_score,
    "oscillator": dip_oscillator_score,
}

SCORERS = {
    "momentum": momentum_score,
    "volume": volume_score,
    "volatility": volatility_score,
    "trend": trend_score,
    "oscillator": oscillator_score,
}


def measure(facts, mode="trend", weights=None):
    """0~100 점과 구성요소별 내역. 여기서 끝난다 — 살지 말지는 brain 이 정한다.

    원본 score() 에 있던 verdict·buy·buy_threshold·position_size_pct 는 옮기지 않았다.
    점수를 주문으로 바꾸는 순간 판단 주체가 둘이 되고, 그러면 어느 쪽을 믿을지 알 수 없다.
    """
    if mode not in MODES:
        raise ValueError(f"mode 는 {MODES} 중 하나: {mode}")
    scorers = DIP_SCORERS if mode == "dip" else SCORERS
    weights = weights or {}

    parts, reasons, total = {}, [], 0.0
    for name in COMPONENTS:
        raw, why = scorers[name](facts)
        w = float(weights.get(name, 1.0))
        s = raw * w
        parts[name] = round(s, 1)
        total += s
        reasons.append(f"{name} {s:.0f}/{20 * w:.0f} ({why})")
    # 가중치를 줘도 만점이 100 이 되도록 정규화한다
    max_total = sum(20 * float(weights.get(n, 1.0)) for n in COMPONENTS) or 1.0
    return {"score": round(total / max_total * 100, 1), "parts": parts,
            "reasons": reasons, "mode": mode}


def measure_both(facts):
    """추세 관점과 눌림목 관점을 같이 낸다. 어느 쪽으로 읽을지도 brain 이 고른다."""
    return {m: measure(facts, mode=m) for m in MODES}
