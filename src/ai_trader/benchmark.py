"""벤치마크 대비 성과. "이겼다"를 주장이 아니라 숫자로 만든다.

목표는 S&P500 초과수익이지만, 이 파일이 하는 일은 이기는 것이 아니라 **재는 것**이다.
같은 구간·같은 시작점으로 지수를 재생해서 초과수익(alpha)을 낸다. 구간이 다르면 비교하지 않는다.

지수 데이터가 없으면 0 을 채우지 않는다 — 없다고 말한다. 벤치마크 없는 초과수익은 거짓말이다.
"""
from __future__ import annotations

import time

import pandas as pd

from . import config as C

DATA = C.ROOT / "data"


def path(symbol: str = None):
    sym = (symbol or C.BENCHMARK).lstrip("^").lower()
    return DATA / f"bench_{sym}.parquet"


def fetch_index(symbol: str = None, period: str = "10y") -> pd.DataFrame:
    """지수 일봉을 받아 저장한다. 종목 다운로더와 분리한 건 지수는 유니버스가 아니기 때문이다."""
    import yfinance as yf
    symbol = symbol or C.BENCHMARK
    raw = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    if raw.empty or "Close" not in raw:
        raise RuntimeError(f"{symbol} 지수를 못 받았다 — 초과수익을 계산할 근거가 없다")
    out = pd.DataFrame({"ts": pd.to_datetime(raw.index, utc=True),
                        "close": raw["Close"].to_numpy()}).dropna()
    DATA.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path(symbol), index=False)
    return out


def load(symbol: str = None) -> pd.DataFrame:
    p = path(symbol)
    if not p.exists():
        return pd.DataFrame(columns=["ts", "close"])
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def curve(days: int, end=None, symbol: str = None) -> list[float]:
    """마지막 days 개 종가. 백테스트 구간 길이에 맞춘다."""
    df = load(symbol)
    if df.empty:
        return []
    if end is not None:
        df = df[df["ts"] <= pd.Timestamp(end, tz="UTC")]
    return [float(x) for x in df["close"].tail(days)]


def alpha(equity_curve: list[float], days: int = None, end=None, symbol: str = None) -> dict:
    """초과수익. 지수가 없거나 구간이 안 맞으면 숫자를 지어내지 않고 사유를 돌려준다."""
    symbol = symbol or C.BENCHMARK
    if len(equity_curve) < 2:
        return {"benchmark": symbol, "unavailable": "자산 곡선이 2점 미만"}
    bench = curve(days or len(equity_curve), end=end, symbol=symbol)
    if len(bench) < 2:
        return {"benchmark": symbol,
                "unavailable": f"{symbol} 데이터 없음 — python -m ai_trader.benchmark 로 먼저 받아라"}
    n = min(len(equity_curve), len(bench))
    mine = (equity_curve[-1] - equity_curve[-n]) / equity_curve[-n] * 100
    theirs = (bench[-1] - bench[-n]) / bench[-n] * 100
    return {
        "benchmark": symbol,
        "bars_compared": n,
        "strategy_return_pct": round(mine, 3),
        "benchmark_return_pct": round(theirs, 3),
        "alpha_pct": round(mine - theirs, 3),
        "beat_benchmark": mine > theirs,
    }


# 대시보드가 보여주는 지수·환율. 매매에 쓰이지 않는다 — 사람이 보는 배경이다.
INDICES = [("KOSPI", "^KS11"), ("S&P500", "^GSPC"), ("USD/KRW", "KRW=X"), ("JPY/KRW", "JPYKRW=X")]

_QUOTE_CACHE: tuple[float, list] = (0.0, [])


def quotes(ttl: int = 300) -> list[dict]:
    """지수·환율 현재가와 전일 대비. 못 받으면 0 으로 채우지 않고 unavailable 로 남긴다."""
    global _QUOTE_CACHE
    if _QUOTE_CACHE[1] and time.time() - _QUOTE_CACHE[0] < ttl:
        return _QUOTE_CACHE[1]
    out = []
    try:
        import yfinance as yf
        raw = yf.download([s for _, s in INDICES], period="7d", interval="1d",
                          progress=False, auto_adjust=False, group_by="ticker")
        for name, sym in INDICES:
            try:
                closes = raw[sym]["Close"].dropna()
                last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                out.append({"name": name, "symbol": sym, "last": round(last, 2),
                            "change_pct": round((last - prev) / prev * 100, 2)})
            except Exception:
                out.append({"name": name, "symbol": sym, "unavailable": "응답 없음"})
    except Exception as exc:
        out = [{"name": n, "symbol": s, "unavailable": type(exc).__name__} for n, s in INDICES]
    _QUOTE_CACHE = (time.time(), out)
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ai_trader.benchmark", description="벤치마크 지수 수집")
    ap.add_argument("--symbol", default=C.BENCHMARK)
    ap.add_argument("--period", default="10y")
    args = ap.parse_args(argv)
    df = fetch_index(args.symbol, args.period)
    print(f"[benchmark] {args.symbol}: {len(df):,}봉 "
          f"{str(df['ts'].min())[:10]} ~ {str(df['ts'].max())[:10]} → {path(args.symbol)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
