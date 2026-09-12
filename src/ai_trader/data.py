"""시장 관측. 여기에는 매매 신호가 없다 — 사실만 모아서 brain 에 넘긴다.

TossFeed      — 토스증권 OpenAPI 캔들/현재가 (키 있을 때)
SyntheticFeed — 시드 고정 랜덤워크. 키 없이도 랩 전체가 돌아가게 하는 대역
news()        — 구글 뉴스 RSS (키 불필요). 실시간 헤드라인.
"""
from __future__ import annotations

import math
import random
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from . import config as C, indicators, quant

KST = timezone(timedelta(hours=9))
# ponytail: KRX 정규장만 본다. NXT 연장세션(08:00~20:00)이 필요해지면 여기만 넓힌다.
SESSION_KST = (9 * 60, 15 * 60 + 30)

NAMES = {
    "005930": "삼성전자", "000660": "SK하이닉스", "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스", "005380": "현대차", "035420": "NAVER",
    "035720": "카카오", "068270": "셀트리온", "105560": "KB금융",
    "051910": "LG화학", "247540": "에코프로비엠", "086520": "에코프로",
    "196170": "알테오젠", "058470": "리노공업", "277810": "레인보우로보틱스",
    "042700": "한미반도체", "022100": "포스코DX", "095340": "ISC",
    "900140": "엘브이엠씨홀딩스", "053610": "프로텍",
}


def session_now(now: datetime | None = None) -> bool:
    """지금이 정규장 시간대인가. 휴장일 여부는 모른다 — 개장일 판정과 AND 로 쓴다."""
    now = now or datetime.now(KST)
    return SESSION_KST[0] <= now.hour * 60 + now.minute < SESSION_KST[1]


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

    def is_open(self) -> bool:
        return True  # 시뮬레이터에는 주말이 없다


@dataclass
class TossFeed:
    source = "toss"

    def __post_init__(self):
        from .broker import TossBroker
        self._auth = TossBroker()
        self.session = requests.Session()
        self._cal: tuple[float, bool] | None = None

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

    def is_open(self) -> bool:
        """휴장일은 토스 달력이, 세션 시간은 시계가 판단한다. 달력이 죽으면 요일로 대체한다."""
        now = datetime.now(KST)
        if not session_now(now):
            return False
        if self._cal and time.time() - self._cal[0] < 3600:
            return self._cal[1]
        try:
            trading = trading_day_from(self._get("/api/v1/market-calendar/KR"))
        except Exception:
            trading = None
        if trading is None:
            trading = now.weekday() < 5  # 달력을 못 읽었다 — 공휴일은 못 거른다
        self._cal = (time.time(), trading)
        return trading

    def candles(self, symbol: str, n: int = 60) -> list[dict]:
        body = self._get("/api/v1/candles", symbols=symbol, interval="1d", count=n)
        rows = body.get("candles") or body.get("result") or []
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
        return {r.get("symbol"): float(r.get("close") or r.get("price") or 0) for r in rows}

    def price(self, symbol: str) -> float:
        return self.prices([symbol]).get(symbol, 0.0)

    def advance(self) -> bool:
        return False


_NEWS_CACHE: dict[str, tuple[float, list[dict]]] = {}


def news(symbol: str, limit: int = 4, ttl: int = 900) -> list[dict]:
    """구글 뉴스 RSS 헤드라인. 네트워크가 죽어도 랩은 멈추지 않는다 — 빈 리스트를 준다."""
    hit = _NEWS_CACHE.get(symbol)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    q = urllib.parse.quote(f"{NAMES.get(symbol, symbol)} 주가")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    items: list[dict] = []
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        for node in ET.fromstring(r.content).iter("item"):
            title = (node.findtext("title") or "").strip()
            if not title:
                continue
            items.append({"title": re.sub(r"\s+", " ", title),
                          "published": (node.findtext("pubDate") or "").strip()})
            if len(items) >= limit:
                break
    except Exception as exc:  # 뉴스는 있으면 좋은 것. 없다고 랩을 세우지 않는다.
        items = [{"title": f"[news unavailable: {type(exc).__name__}]", "published": ""}]
    _NEWS_CACHE[symbol] = (time.time(), items)
    return items


def make_feed(paper: bool = True, live_data: bool = False):
    """모의투자 = 페이퍼 회계 + 실시세. live_data 가 그 조합을 만든다."""
    if (not paper or live_data) and C.have_broker_keys():
        return TossFeed()
    return SyntheticFeed()


def observe(feed, symbols: list[str], with_news: bool = True,
            quant_mode: str = "trend") -> list[dict]:
    """brain 에게 넘길 관측 팩. 원자료 + 서술 통계 + 기술지표 + 퀀트 점수 + 헤드라인.

    지표와 점수는 '측정'이지 '판단'이 아니다 — 무엇을 할지는 brain 만 정한다.
    그래서 quant 결과에서 매수/매도 결론(verdict·buy)은 애초에 만들지 않는다.
    """
    pack = []
    for sym in symbols:
        bars = feed.candles(sym, 60)
        closes = [b["close"] for b in bars]
        vols = [b["volume"] for b in bars]
        row = {
            "symbol": sym,
            "name": NAMES.get(sym, sym),
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
        pack.append(row)
    return pack
