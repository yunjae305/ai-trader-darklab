"""내려받은 실데이터를 멀티 타임프레임으로 재생하는 피드.

한 시점에서 4시간봉·1시간봉·15분봉·일봉을 동시에 본다. 단, 어느 시점이든
**그 시각 이전에 마감된 봉만** 돌려준다 — 미래를 훔쳐보지 않는 것이 이 파일의 유일한 일이다.

타임프레임마다 소스가 주는 소급 기간이 다르므로(download.LIMITS), 없는 구간은 빈 리스트다.
비어 있다고 일봉으로 대체하지 않는다. 없으면 없다고 말한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import config as C

DATA = C.ROOT / "data"
TF_ORDER = ["15m", "1h", "4h", "1d"]


def load(interval: str) -> pd.DataFrame:
    path = DATA / f"bars_{interval}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["ticker", "ts", "open", "high", "low", "close", "volume"])
    df = pd.read_parquet(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values(["ticker", "ts"]).reset_index(drop=True)


def resample_4h(hourly: pd.DataFrame) -> pd.DataFrame:
    """주식에 네이티브 4시간봉은 없다. 1시간봉을 4시간 버킷으로 묶어 만든다.

    정규장이 6.5시간이라 마지막 버킷은 짧다 — 그건 시장 구조이지 버그가 아니다.
    """
    if hourly.empty:
        return hourly
    out = []
    for ticker, grp in hourly.groupby("ticker", sort=False):
        g = grp.set_index("ts").sort_index()
        r = g.resample("4h", label="right", closed="right").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        r = r.dropna(subset=["close"])
        r.insert(0, "ticker", ticker)
        out.append(r.reset_index())
    return pd.concat(out, ignore_index=True) if out else hourly


@dataclass
class SymbolView:
    """6자리 종목코드로 HistoryFeed 를 본다.

    내려받은 데이터는 '005930.KS' 로, 나머지 시스템은 '005930' 으로 종목을 부른다.
    둘을 잇는 것이 이 클래스의 전부다. 여기서 값을 만들거나 채우지 않는다.
    """
    feed: "HistoryFeed"
    source: str = "history"

    def __post_init__(self):
        self._map: dict[str, str] = {}
        for t in self.feed.tickers:
            self._map.setdefault(t.split(".")[0], t)

    def candles(self, symbol: str, n: int = 60) -> list[dict]:
        ticker = self._map.get(symbol)
        return self.feed.candles(ticker, n) if ticker else []

    def price(self, symbol: str) -> float:
        c = self.candles(symbol, 1)
        return c[-1]["close"] if c else 0.0

    def prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.price(s) for s in symbols}

    def advance(self) -> bool:
        return self.feed.advance()

    def is_open(self) -> bool:
        return True  # 과거 데이터 재생에는 장 시간이 없다

    def missing(self, symbols: list[str]) -> list[str]:
        """내려받은 데이터에 없는 종목. 조용히 빠지면 백테스트가 거짓말을 한다."""
        return [s for s in symbols if s not in self._map]


@dataclass
class HistoryFeed:
    """시계 하나를 앞으로 밀면 네 타임프레임이 함께 따라온다."""
    frames: dict[str, pd.DataFrame]
    tickers: list[str]
    clock: list[pd.Timestamp]
    cursor: int = 0
    source: str = "history"
    _idx: dict = field(default_factory=dict, repr=False)

    @classmethod
    def build(cls, tickers: list[str] | None = None, timeframes: list[str] | None = None,
              spine: str = "1d") -> "HistoryFeed":
        timeframes = timeframes or TF_ORDER
        frames: dict[str, pd.DataFrame] = {}
        for tf in timeframes:
            df = load(tf)
            if tf == "4h" and df.empty:
                df = resample_4h(load("1h"))  # 소스가 4h 를 안 주면 1h 에서 만든다
            frames[tf] = df
        base = frames.get(spine)
        if base is None or base.empty:
            raise RuntimeError(f"{spine} 봉이 없다 — 먼저 python -m ai_trader.download 를 돌려라")
        if tickers:
            frames = {k: v[v["ticker"].isin(tickers)] for k, v in frames.items()}
            base = frames[spine]
        clock = sorted(base["ts"].unique())
        idx = {tf: {t: g.reset_index(drop=True) for t, g in df.groupby("ticker", sort=False)}
               for tf, df in frames.items()}
        return cls(frames=frames, tickers=sorted(base["ticker"].unique()),
                   clock=[pd.Timestamp(c) for c in clock], _idx=idx)

    @classmethod
    def from_frames(cls, frames: dict[str, pd.DataFrame], spine: str = "1d") -> "HistoryFeed":
        base = frames[spine]
        idx = {tf: {t: g.reset_index(drop=True) for t, g in df.groupby("ticker", sort=False)}
               for tf, df in frames.items()}
        return cls(frames=frames, tickers=sorted(base["ticker"].unique()),
                   clock=[pd.Timestamp(c) for c in sorted(base["ts"].unique())], _idx=idx)

    # ---------- 시계 ----------
    @property
    def now(self) -> pd.Timestamp:
        return self.clock[min(self.cursor, len(self.clock) - 1)]

    def advance(self) -> bool:
        if self.cursor + 1 >= len(self.clock):
            return False
        self.cursor += 1
        return True

    def seek(self, ts: pd.Timestamp) -> None:
        pos = [i for i, c in enumerate(self.clock) if c <= ts]
        self.cursor = (pos[-1] if pos else 0)

    # ---------- 조회 (전부 now 이하로만 자른다) ----------
    def bars(self, ticker: str, interval: str, n: int = 60) -> pd.DataFrame:
        g = self._idx.get(interval, {}).get(ticker)
        if g is None or g.empty:
            return g if g is not None else pd.DataFrame()
        cut = g[g["ts"] <= self.now]
        return cut.tail(n)

    def candles(self, ticker: str, n: int = 60) -> list[dict]:
        b = self.bars(ticker, "1d", n)
        if b.empty:
            return []
        return [{"date": str(r.ts)[:10], "open": float(r.open), "high": float(r.high),
                 "low": float(r.low), "close": float(r.close),
                 "volume": float(r.volume or 0)} for r in b.itertuples()]

    def price(self, ticker: str) -> float:
        b = self.bars(ticker, "1d", 1)
        return float(b["close"].iloc[-1]) if len(b) else 0.0

    def prices(self, tickers: list[str]) -> dict[str, float]:
        return {t: self.price(t) for t in tickers}

    def available(self) -> dict[str, dict]:
        """타임프레임별로 실제 뭐가 있는지. 없으면 없다고 나온다."""
        out = {}
        for tf, df in self.frames.items():
            if df.empty:
                out[tf] = {"bars": 0, "tickers": 0, "status": "확보 불가"}
            else:
                out[tf] = {"bars": int(len(df)), "tickers": int(df["ticker"].nunique()),
                           "first": str(df["ts"].min().date()), "last": str(df["ts"].max().date()),
                           "status": "있음"}
        return out


def _describe(b: pd.DataFrame) -> dict:
    """한 타임프레임의 서술 통계. 판단이 아니라 관측 — 임계값도 매매 결론도 없다."""
    if len(b) < 2:
        return {}
    c = b["close"].astype(float)
    rets = c.pct_change().dropna()
    def chg(n: int):
        return round(float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2) if len(c) > n else None
    lo, hi = float(c.min()), float(c.max())
    v = b["volume"].astype(float).fillna(0)
    return {
        "bars": int(len(b)), "last": round(float(c.iloc[-1]), 2),
        "chg_1_pct": chg(1), "chg_5_pct": chg(5), "chg_20_pct": chg(20),
        "vol_pct": round(float(rets.std()) * 100, 2) if len(rets) > 1 else None,
        "pos_in_range": round((float(c.iloc[-1]) - lo) / (hi - lo + 1e-9), 3),
        "volume_vs_mean": round(float(v.tail(5).mean() / (v.mean() + 1e-9)), 2) if len(v) else None,
    }


def observe_mtf(feed: "HistoryFeed", tickers: list[str], timeframes: list[str] | None = None,
                depth: int = 40, names: dict[str, str] | None = None) -> list[dict]:
    """멀티 타임프레임 관측 팩. 한 종목에 대해 4h·1h·15m·1d 를 한꺼번에 보여준다.

    어떤 타임프레임이 그 구간에 없으면 'unavailable' 이라고 적는다. 일봉으로 대신 채우지 않는다.
    """
    timeframes = timeframes or TF_ORDER
    names = names or {}
    pack = []
    for t in tickers:
        entry = {"symbol": t, "name": names.get(t, t), "as_of": str(feed.now), "timeframes": {}}
        for tf in timeframes:
            b = feed.bars(t, tf, depth)
            if b is None or b.empty:
                entry["timeframes"][tf] = {"status": "unavailable"}
                continue
            entry["timeframes"][tf] = {
                "status": "ok",
                "closes": [round(float(x), 2) for x in b["close"].tail(depth)],
                "observed": _describe(b),
            }
        if any(v.get("status") == "ok" for v in entry["timeframes"].values()):
            pack.append(entry)
    return pack
