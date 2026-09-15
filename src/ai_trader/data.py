"""시장 관측. 여기에는 매매 신호가 없다 — 사실만 모아서 brain 에 넘긴다.

TossFeed      — 토스증권 OpenAPI 캔들/현재가 (키 있을 때)
SyntheticFeed — 시드 고정 랜덤워크. 키 없이도 랩 전체가 돌아가게 하는 대역
news()        — 구글 뉴스 RSS (키 불필요). 실시간 헤드라인.
"""
from __future__ import annotations

import math
import os
import random
import threading
import time
from concurrent import futures
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from . import config as C, indicators, news as news_feed, quant, sectors

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
# KRX 세션 (KST 기준 분). 2026-09-14 애프터마켓이 신설되면서 같은 날
# 기존 시간외단일가(16:00~18:00, 10분 단일가)는 폐지됐다.
# NXT 는 여기 없다 — 주문 경로가 kiwoom.Kiwoom(exchange="KRX") 로 고정이라
# 넥스트레이드 세션(08:00~08:50 / 15:40~20:00)으로는 주문이 나가지 않는다.
KRX_SESSIONS = {
    "pre_close": (8 * 60 + 30, 8 * 60 + 40),    # 장전 시간외종가 — 전일 종가
    "regular": (9 * 60, 15 * 60 + 30),          # 정규장
    "post_close": (15 * 60 + 40, 16 * 60),      # 장후 시간외종가 — 당일 종가
    "after": (16 * 60, 20 * 60),                # 애프터마켓 — 2026-09-14 신설
}
# 어느 세션에 주문을 낼지. 기본은 정규장만 — 모의투자 서버가 연장세션 주문을
# 받는지 확인되지 않았다. 실제로 받는 것을 확인한 뒤에 넓힌다.
KR_SESSIONS = tuple(s.strip() for s in
                    os.getenv("AI_TRADER_KR_SESSIONS", "regular").split(",") if s.strip())
_unknown = set(KR_SESSIONS) - set(KRX_SESSIONS)
if _unknown:  # .env 오타로 매매가 조용히 멈추거나 엉뚱한 때 도는 것을 막는다
    raise ValueError(f"AI_TRADER_KR_SESSIONS 에 모르는 세션 {sorted(_unknown)} "
                     f"— 가능한 값: {sorted(KRX_SESSIONS)}")
SESSION_ET = (9 * 60 + 30, 16 * 60)
# 관측 팩을 만들 때 종목별로 병렬 조회한다. 토스 시세 한도(초당 15~20)를 넘지 않는 선.
FETCH_WORKERS = int(os.getenv("AI_TRADER_FETCH_WORKERS", "8"))

NAMES = {
    "005930": "삼성전자", "000660": "SK하이닉스", "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스", "005380": "현대차", "035420": "NAVER",
    "035720": "카카오", "068270": "셀트리온", "105560": "KB금융",
    "051910": "LG화학", "247540": "에코프로비엠", "086520": "에코프로",
    "196170": "알테오젠", "058470": "리노공업", "277810": "레인보우로보틱스",
    "042700": "한미반도체", "022100": "포스코DX", "095340": "ISC",
    "900140": "엘브이엠씨홀딩스", "053610": "프로텍",
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "AMZN": "Amazon",
    "GOOGL": "Alphabet", "META": "Meta", "TSLA": "Tesla",
    "SPY": "SPDR S&P 500", "QQQ": "Invesco QQQ", "SPYM": "SPDR Portfolio S&P 500",
}


def session_now(now: datetime | None = None) -> bool:
    """지금이 주문을 낼 KRX 세션인가. 휴장일 여부는 모른다 — 개장일 판정과 AND 로 쓴다."""
    now = now or datetime.now(KST)
    minute = now.hour * 60 + now.minute
    return any(KRX_SESSIONS[name][0] <= minute < KRX_SESSIONS[name][1]
               for name in KR_SESSIONS)


def session_label(now: datetime | None = None) -> str:
    """지금 열려 있는 세션 이름. 어디에도 안 걸리면 빈 문자열 — 화면과 로그가 쓴다."""
    now = now or datetime.now(KST)
    minute = now.hour * 60 + now.minute
    for name, (start, end) in KRX_SESSIONS.items():
        if start <= minute < end:
            return name
    return ""


def us_session_now(now: datetime | None = None, order_amount: bool = False) -> bool:
    """미국 정규장 시간대인가. 금액·소수점 주문은 마감 1시간 전까지만 허용된다."""
    now = now.astimezone(ET) if now is not None else datetime.now(ET)
    end = 15 * 60 if order_amount else SESSION_ET[1]
    return SESSION_ET[0] <= now.hour * 60 + now.minute < end


def trading_day_from(body) -> bool | None:
    """장 운영 정보에서 '오늘 개장일인가'만 뽑는다. 모르는 형태면 None — 추측하지 않는다."""
    node = body.get("result", body) if isinstance(body, dict) else body
    if isinstance(node, list):
        node = node[0] if node else {}
    if not isinstance(node, dict):
        return None
    for key in ("isTradingDay", "tradingDay", "isOpen", "open", "opened"):
        v = node.get(key)
        if isinstance(v, bool):
            return v
    return None


def describe(closes: list[float], volumes: list[float]) -> dict:
    """서술 통계. 판단이 아니라 관측 — 임계값도 없고 매수/매도 결론도 없다."""
    if len(closes) < 2:
        return {}
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    def chg(n: int) -> float | None:
        if len(closes) <= n:
            return None
        return round((closes[-1] - closes[-1 - n]) / closes[-1 - n] * 100, 2)
    recent_vol = volumes[-5:] or [0]
    return {
        "last": round(closes[-1], 2),
        "chg_1d_pct": chg(1), "chg_5d_pct": chg(5), "chg_20d_pct": chg(20),
        "daily_vol_pct": round(math.sqrt(var) * 100, 2),
        "range_60d_pct": round((max(closes) - min(closes)) / min(closes) * 100, 2),
        "pos_in_60d_range": round(
            (closes[-1] - min(closes)) / (max(closes) - min(closes) + 1e-9), 3),
        "volume_last": int(volumes[-1]) if volumes else None,
        "volume_vs_20d": round(
            (sum(recent_vol) / len(recent_vol)) / (sum(volumes[-20:]) / max(len(volumes[-20:]), 1) + 1e-9), 2
        ) if len(volumes) >= 5 else None,
    }


@dataclass
class SyntheticFeed:
    """키 없이 도는 결정론적 시장. 한 달 테스트와 self-check 를 여기서 돌린다."""
    seed: int = 20260913
    days: int = 120
    source = "synthetic"

    def __post_init__(self):
        self._series: dict[str, list[dict]] = {}
        start = datetime.now(KST).date() - timedelta(days=self.days - 1)
        for i, sym in enumerate(C.UNIVERSE):
            rnd = random.Random(self.seed + i)
            price = rnd.uniform(20_000, 180_000)
            drift = rnd.uniform(-0.0012, 0.0018)
            sigma = rnd.uniform(0.012, 0.045)  # 종목마다 변동성이 다르다 → 안정/공격 구분 여지
            bars = []
            for d in range(self.days):
                prev = price
                price = max(500.0, price * (1 + rnd.gauss(drift, sigma)))
                # 고가·저가가 없으면 ADX 와 매물대가 계산되지 않는다 — 봉을 온전히 만든다
                wick = abs(rnd.gauss(0, sigma / 3))
                bars.append({
                    "date": (start + timedelta(days=d)).isoformat(),
                    "open": round(prev, 1),
                    "high": round(max(prev, price) * (1 + wick), 1),
                    "low": round(min(prev, price) * (1 - wick), 1),
                    "close": round(price, 1),
                    "volume": float(int(rnd.uniform(1e5, 9e6))),
                })
            self._series[sym] = bars
        self.cursor = self.days - 1

    def candles(self, symbol: str, n: int = 60) -> list[dict]:
        bars = self._series.get(symbol, [])
        end = min(self.cursor + 1, len(bars))
        return bars[max(0, end - n):end]

    def price(self, symbol: str) -> float:
        c = self.candles(symbol, 1)
        return c[-1]["close"] if c else 0.0

    def prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.price(s) for s in symbols}

    def advance(self) -> bool:
        if self.cursor + 1 >= self.days:
            return False
        self.cursor += 1
        return True

    def is_open(self, market: str | None = None) -> bool:
        return True  # 시뮬레이터에는 주말이 없다

    def is_open_for(self, symbol: str) -> bool:
        return True


@dataclass
class TossFeed:
    source = "toss"

    def __post_init__(self):
        from .broker import TossVenue
        self._auth = TossVenue()  # 시세는 계좌 헤더 없이 토큰만으로 부른다
        self.session = requests.Session()
        self._cal: dict[str, tuple[float, bool]] = {}

    def _get(self, path: str, **params):
        for _ in range(3):
            r = self.session.get(f"{C.TOSS_BASE}{path}",
                                 headers={"Authorization": f"Bearer {self._auth.token()}"},
                                 params=params, timeout=10)
            if r.status_code == 429:  # 토스가 재시도 시점을 헤더로 알려준다
                time.sleep(min(float(r.headers.get("Retry-After", 1)), 5))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def is_open(self, market: str | None = None) -> bool:
        """휴장일은 토스 달력이, 시장별 세션 시간은 현지 시계가 판단한다."""
        if market is None:
            markets = {C.market_of(s) for s in C.UNIVERSE}
            return any(self.is_open(m) for m in markets)
        if market not in C.MARKETS:
            return False
        now = datetime.now(KST if market == "KR" else ET)
        in_session = session_now(now) if market == "KR" else us_session_now(now)
        if not in_session:
            return False
        cached = self._cal.get(market)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        try:
            trading = trading_day_from(self._get(f"/api/v1/market-calendar/{market}"))
        except Exception:
            trading = None
        if trading is None:
            trading = now.weekday() < 5  # 달력을 못 읽었다 — 공휴일은 못 거른다
        self._cal[market] = (time.time(), trading)
        return trading

    def is_open_for(self, symbol: str) -> bool:
        return self.is_open(C.market_of(symbol))

    def candles(self, symbol: str, n: int = 60) -> list[dict]:
        body = self._get("/api/v1/candles", symbol=symbol, interval="1d", count=n,
                         adjusted="true")
        node = body.get("result", body)
        rows = node.get("candles", []) if isinstance(node, dict) else []
        out = []
        for row in rows:
            close = row.get("close", row.get("closePrice"))
            if close is None:
                continue
            def f(*keys, default=close):
                for k in keys:
                    if row.get(k) is not None:
                        return float(row[k])
                return float(default)
            out.append({
                "date": str(row.get("timestamp") or row.get("date") or "")[:10],
                "open": f("open", "openPrice"),
                "high": f("high", "highPrice"),
                "low": f("low", "lowPrice"),
                "close": float(close),
                "volume": f("volume", default=0),
            })
        out.sort(key=lambda c: c["date"])  # 토스는 최신→과거로 준다
        return out[-n:]

    def prices(self, symbols: list[str]) -> dict[str, float]:
        body = self._get("/api/v1/prices", symbols=",".join(symbols))
        rows = body.get("prices") or body.get("result") or []
        return {r.get("symbol"): float(r.get("lastPrice") or r.get("close") or
                                         r.get("price") or 0) for r in rows}

    def price(self, symbol: str) -> float:
        return self.prices([symbol]).get(symbol, 0.0)

    def advance(self) -> bool:
        return False


def news(symbol: str, limit: int = 4, ttl: int = 900) -> list[dict]:
    """종목별 뉴스 헤드라인. 수집은 news.py 가 한다 — 뉴스 코드를 한 곳에 둔다.

    네트워크가 죽어도 랩은 멈추지 않는다 — 없으면 없다고 적힌 한 줄이 온다.
    """
    return news_feed.for_symbol(symbol, NAMES.get(symbol, symbol), limit=limit, ttl=ttl)


@dataclass
class KiwoomFeed:
    """키움 국내 일봉. 주문과 같은 창구에서 시세도 받는다 (ka10081)."""
    source = "kiwoom"

    def __post_init__(self):
        from .kiwoom import Kiwoom
        self.api = Kiwoom()
        self._candles: dict[str, tuple[float, list[dict]]] = {}
        self._quotes: dict[str, tuple[float, float, str]] = {}
        self._request_lock = threading.Lock()
        self._last_request = 0.0

    def candles(self, symbol: str, n: int = 60) -> list[dict]:
        cached = self._candles.get(symbol)
        if cached and time.time() - cached[0] < 300 and len(cached[1]) >= n:
            return cached[1][-n:]
        # ka10081은 모의 API의 호출 한도가 낮다. 병렬 관측도 이 구간에서는 직렬화한다.
        with self._request_lock:
            cached = self._candles.get(symbol)
            if cached and time.time() - cached[0] < 300 and len(cached[1]) >= n:
                return cached[1][-n:]
            wait = 1.05 - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            for attempt in range(3):
                try:
                    rows = self.api.candles(symbol, max(n, 60))
                    break
                except Exception as exc:
                    if "429" not in str(exc) or attempt == 2:
                        raise
                    time.sleep(1.2 * (attempt + 1))
            self._last_request = time.monotonic()
            self._candles[symbol] = (time.time(), rows)
            return rows[-n:]

    # 감시용 시세는 전략용 캔들과 신선도 요구가 다르다. 캔들은 지표를 만드는 것이라
    # 5 분 캐시로 충분하지만, 손절·익절·킬스위치는 그 5 분 안에 일어난다 —
    # 같은 캐시에서 가격을 꺼내면 3 초마다 봐도 최대 5 분 묵은 값을 본다.
    QUOTE_TTL = 2.0

    def quote_price(self, symbol: str) -> tuple[float, str]:
        """지금 팔면 받는 값(최우선 매수호가)과 원장이 찍은 호가 기준시간.

        최종가 대신 매수호가를 쓴다 — 보유를 정리할 때 실제로 닿는 값이 그것이고,
        손절선을 스프레드만큼 낙관적으로 보지 않게 된다. 읽지 못하면 (0, "") 다.
        """
        from .kiwoom import num
        cached = self._quotes.get(symbol)
        if cached and time.time() - cached[0] < self.QUOTE_TTL:
            return cached[1], cached[2]
        with self._request_lock:
            cached = self._quotes.get(symbol)
            if cached and time.time() - cached[0] < self.QUOTE_TTL:
                return cached[1], cached[2]
            try:
                q = self.api.quote(symbol)
            except Exception:
                return 0.0, ""
            # 부호는 전일 대비 방향이지 음수 가격이 아니다 — num 이 떼어낸다.
            px = num(q.get("buy_fpr_bid")) or num(q.get("sel_fpr_bid")) or 0.0
            at = str(q.get("bid_req_base_tm") or "")
            self._quotes[symbol] = (time.time(), float(px), at)
            return float(px), at

    def price(self, symbol: str) -> float:
        px, _ = self.quote_price(symbol)
        if px > 0:
            return px
        # 호가를 못 읽었으면 캔들로 물러난다. 다만 그것은 최대 5 분 묵은 값이다 —
        # 0 을 돌려주면 감시가 종목을 통째로 건너뛰므로, 묵은 값이라도 주고 밝힌다.
        c = self.candles(symbol, 60)
        return c[-1]["close"] if c else 0.0

    def price_age(self, symbol: str) -> float | None:
        """이 가격이 몇 초 전 것인가. 모르면 None."""
        cached = self._quotes.get(symbol)
        return None if not cached else round(time.time() - cached[0], 2)

    def prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.price(s) for s in symbols}

    def advance(self) -> bool:
        return False

    def is_open(self, market: str | None = None) -> bool:
        # 키움에는 휴장일 조회 TR 이 없다 — 요일로 대체하므로 공휴일은 못 거른다.
        if market not in (None, "KR"):
            return False
        now = datetime.now(KST)
        return session_now(now) and now.weekday() < 5

    def is_open_for(self, symbol: str) -> bool:
        return C.market_of(symbol) == "KR" and self.is_open()


@dataclass
class RoutedFeed:
    """시세도 주문과 같은 증권사에서 받는다 — 국내는 키움, 해외는 토스.

    한쪽 키만 있으면 그쪽 시장만 실시세다. 없는 쪽은 0 을 주고, 0 인 종목은
    brain.validate 가 걸러낸다 — 모르는 가격으로 주문을 만들지 않는다.
    """
    kr: object = None
    us: object = None
    source: str = "routed"

    def __post_init__(self):
        have = [n for n, f in (("KR=키움", self.kr), ("US=토스", self.us)) if f is not None]
        self.source = ("routed:" + "+".join(have)) if have else "routed:none"

    def _feed(self, symbol: str):
        try:
            return self.kr if C.market_of(symbol) == "KR" else self.us
        except ValueError:
            return None

    def candles(self, symbol: str, n: int = 60) -> list[dict]:
        f = self._feed(symbol)
        return f.candles(symbol, n) if f is not None else []

    def price(self, symbol: str) -> float:
        f = self._feed(symbol)
        return f.price(symbol) if f is not None else 0.0

    def prices(self, symbols: list[str]) -> dict[str, float]:
        groups: list[tuple[object, list[str]]] = []
        for s in symbols:
            f = self._feed(s)
            if f is not None:
                hit = next((names for known, names in groups if known is f), None)
                if hit is None:
                    hit = []
                    groups.append((f, hit))
                hit.append(s)
        out = {s: 0.0 for s in symbols}
        for f, names in groups:
            out.update(f.prices(names))
        return out

    def advance(self) -> bool:
        return False

    def is_open(self) -> bool:
        markets = {C.market_of(s) for s in C.UNIVERSE}
        return (("KR" in markets and self.kr is not None and self.kr.is_open("KR")) or
                ("US" in markets and self.us is not None and self.us.is_open("US")))

    def is_open_for(self, symbol: str) -> bool:
        f = self._feed(symbol)
        if f is None:
            return False
        method = getattr(f, "is_open_for", None)
        return method(symbol) if method else f.is_open()


def feed_open_for(feed, symbol: str) -> bool:
    """종목 시장이 지금 주문 가능한지 피드별 공통 방식으로 묻는다."""
    method = getattr(feed, "is_open_for", None)
    if method:
        return bool(method(symbol))
    return bool(feed.is_open())


@dataclass
class SessionFeed:
    """열린 시장은 실시세, 닫힌 시장은 저장 일봉으로 보여주는 대시보드 피드."""
    live: object
    closed: object
    source: str = "session-aware"

    def _feed(self, symbol: str):
        return self.live if feed_open_for(self.live, symbol) else self.closed

    def candles(self, symbol: str, n: int = 60) -> list[dict]:
        return self._feed(symbol).candles(symbol, n)

    def price(self, symbol: str) -> float:
        return self._feed(symbol).price(symbol)

    def prices(self, symbols: list[str]) -> dict[str, float]:
        active = [s for s in symbols if feed_open_for(self.live, s)]
        inactive = [s for s in symbols if s not in active]
        out = self.closed.prices(inactive)
        out.update(self.live.prices(active))
        return out

    def advance(self) -> bool:
        return False

    def is_open(self) -> bool:
        return self.live.is_open()

    def is_open_for(self, symbol: str) -> bool:
        return feed_open_for(self.live, symbol)


def make_feed(paper: bool = True, live_data: bool = False):
    """모의투자 = 페이퍼 회계 + 실시세. live_data 가 그 조합을 만든다.

    시세는 주문과 같은 창구에서 받는다. 토스는 국내·해외를 다 주므로 토스 키만 있으면
    토스 하나로 끝나고, 키움 키만 있으면 국내만 실시세다(해외는 창구가 없다).
    """
    if paper and not live_data:
        return SyntheticFeed()
    toss = TossFeed() if C.have_broker_keys() else None
    kiwoom = KiwoomFeed() if C.have_kiwoom_keys() else None
    if toss is not None and kiwoom is None:
        return toss
    if kiwoom is not None:
        return RoutedFeed(kr=kiwoom, us=toss)
    return SyntheticFeed()


def observe(feed, symbols: list[str], with_news: bool = True,
            quant_mode: str = "trend") -> list[dict]:
    """brain 에게 넘길 관측 팩. 원자료 + 서술 통계 + 기술지표 + 퀀트 점수 + 헤드라인.

    지표와 점수는 '측정'이지 '판단'이 아니다 — 무엇을 할지는 brain 만 정한다.
    그래서 quant 결과에서 매수/매도 결론(verdict·buy)은 애초에 만들지 않는다.
    """
    def one(sym: str) -> dict:
        bars = feed.candles(sym, 60)
        closes = [b["close"] for b in bars]
        vols = [b["volume"] for b in bars]
        row = {
            "symbol": sym,
            "as_of": bars[-1].get("date", "") if bars else "",
            "market": C.market_of(sym),
            "name": NAMES.get(sym, sym),
            "sector": sectors.of(sym),  # 모르면 빈 문자열. 추측해서 채우지 않는다.
            "closes_60d": [round(c, 1) for c in closes],
            "observed": describe(closes, vols),
            "news": news(sym) if with_news else [],
        }
        try:
            facts = indicators.analyze(bars)
            row["indicators"] = facts
            row["quant"] = quant.measure(facts, mode=quant_mode)
        except Exception as exc:
            # 봉이 모자라면 지표가 안 나온다. 없는 값을 지어내느니 없다고 말한다.
            row["indicators"] = {"unavailable": f"{type(exc).__name__}: {exc}"}
            row["quant"] = {"unavailable": True}
        return row

    # 종목마다 캔들·뉴스가 각각 네트워크를 탄다. 20종목이면 왕복 40번이라 순차로는 사이클이
    # 분 단위로 늘어난다. 순서는 symbols 그대로 유지한다 — 팩 순서가 흔들리면 기록 비교가 깨진다.
    workers = min(FETCH_WORKERS, len(symbols)) or 1
    if workers == 1:
        return [one(s) for s in symbols]
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, symbols))
