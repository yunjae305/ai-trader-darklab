"""실데이터 다운로더. 국장(KIND) · 미장(nasdaqtrader) 전종목 목록을 받아 Yahoo 에서 캔들을 내린다.

무료 소스에는 하드 천장이 있고(아래 LIMITS), 이 모듈은 그 천장을 넘겨 채우지 않는다.
요청한 기간을 못 받으면 추정치로 메우지 않고 '확보 불가'로 기록한다.

    python -m ai_trader.download --market kr,us --top 300
    python -m ai_trader.download --market kr,us --all      # 전종목 (~16,000, 수십 분)
"""
from __future__ import annotations

import argparse
import io
import json
import time
import warnings

import pandas as pd
import requests

from . import config as C, journal

warnings.filterwarnings("ignore")

DATA = C.ROOT / "data"

# Yahoo 가 인터벌별로 돌려주는 최대 소급 기간. 실측으로 확인한 값이다(2026-09).
LIMITS = {
    "15m": ("60d", 60),
    "1h": ("730d", 730),
    "4h": ("730d", 730),   # 주식에 네이티브 4시간봉은 없다 — 1시간봉에서 파생된다
    "1d": ("10y", 3650),
}
TIMEFRAMES = ["4h", "1h", "15m", "1d"]

KIND_URL = "http://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


def kr_universe() -> pd.DataFrame:
    """KIND 상장법인목록. 로그인 불필요.

    보드를 하나씩 명시해서 받는다. 파라미터 없이 한 번에 받으면 KIND 가 조용히 한 보드만
    돌려주는 일이 있었고(2026-09 실측: 코스피 847종목이 통째로 누락된 채 저장됨), 그러면
    백테스트가 코스닥만 보면서 전종목을 본 척한다. 보드별로 받고, 하나라도 비면 세운다.
    """
    # KIND 는 코스피를 "유가"(유가증권)로 적는다. "코스피"로 찾으면 847종목이 통째로 빠진다.
    BOARD = {"유가": ".KS", "코스피": ".KS", "코스닥": ".KQ", "코넥스": ".KQ"}
    frames = []
    for market_type, label in (("stockMkt", "코스피"), ("kosdaqMkt", "코스닥")):
        r = requests.get(f"{KIND_URL}&marketType={market_type}", timeout=60,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        part = pd.read_html(io.StringIO(r.text), header=0)[0]
        if part.empty:
            raise RuntimeError(f"KIND 가 {label}({market_type}) 를 빈 목록으로 줬다 — "
                               "여기서 세우지 않으면 그 보드 전체가 조용히 빠진다")
        frames.append(part)
    df = pd.concat(frames, ignore_index=True)

    df["code"] = df["종목코드"].astype(str).str.zfill(6)
    df = df[df["code"].str.fullmatch(r"\d{6}")]  # 숫자 6자리만 — 스팩 신형코드 등 제외
    known = df["시장구분"].isin(BOARD)
    if not known.all():
        raise RuntimeError(
            f"KIND 에 모르는 시장구분이 있다: {sorted(set(df.loc[~known, '시장구분']))} — "
            "매핑을 고치기 전에는 종목이 조용히 누락된다")
    df = df[known].drop_duplicates("code")
    # KIND 가 업종을 같이 준다 — 섹터 때문에 따로 수집할 필요가 없다 (실측 125종류).
    out = pd.DataFrame({
        "symbol": df["code"], "ticker": df["code"] + df["시장구분"].map(BOARD),
        "name": df["회사명"], "market": "kr", "board": df["시장구분"],
        "sector": df["업종"].fillna("").astype(str).str.strip(),
    }).reset_index(drop=True)

    suffixes = set(out["ticker"].str.split(".").str[-1])
    if suffixes != {"KS", "KQ"}:
        raise RuntimeError(f"국장 유니버스에 한 보드만 있다: {suffixes} — "
                           "코스피나 코스닥이 통째로 빠진 채로는 받지 않는다")
    return out


def us_universe() -> pd.DataFrame:
    """nasdaqtrader 공식 심볼 디렉터리. 테스트 이슈와 비보통주 기호를 걸러낸다."""
    frames = []
    nas = pd.read_csv(NASDAQ_URL, sep="|")
    nas = nas[nas.get("Test Issue", "N") == "N"]
    frames.append(pd.DataFrame({"symbol": nas["Symbol"], "name": nas["Security Name"],
                                "board": "NASDAQ"}))
    oth = pd.read_csv(OTHER_URL, sep="|")
    oth = oth[oth.get("Test Issue", "N") == "N"]
    frames.append(pd.DataFrame({"symbol": oth["ACT Symbol"], "name": oth["Security Name"],
                                "board": oth.get("Exchange", "NYSE")}))
    df = pd.concat(frames, ignore_index=True).dropna(subset=["symbol"])
    df["symbol"] = df["symbol"].astype(str)
    df = df[df["symbol"].str.fullmatch(r"[A-Z]{1,5}")]  # 워런트·우선주 기호 제외
    df["ticker"] = df["symbol"]
    df["market"] = "us"
    return df.drop_duplicates("symbol").reset_index(drop=True)


def universe(markets: list[str]) -> pd.DataFrame:
    parts = []
    if "kr" in markets:
        parts.append(kr_universe())
    if "us" in markets:
        parts.append(us_universe())
    return pd.concat(parts, ignore_index=True)


def _tidy(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """yfinance 의 wide MultiIndex 를 [ticker, ts, ohlcv] long 포맷으로 편다."""
    rows = []
    for t in tickers:
        try:
            sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        sub = sub.dropna(how="all")
        if sub.empty or "Close" not in sub:
            continue
        rows.append(pd.DataFrame({
            "ticker": t, "ts": sub.index,
            "open": sub["Open"].to_numpy(), "high": sub["High"].to_numpy(),
            "low": sub["Low"].to_numpy(), "close": sub["Close"].to_numpy(),
            "volume": sub["Volume"].to_numpy(),
        }))
    if not rows:
        return pd.DataFrame(columns=["ticker", "ts", "open", "high", "low", "close", "volume"])
    out = pd.concat(rows, ignore_index=True).dropna(subset=["close"])
    out["ts"] = pd.to_datetime(out["ts"], utc=True, errors="coerce")
    return out.dropna(subset=["ts"])


def fetch(tickers: list[str], interval: str, batch: int = 100,
          pause: float = 0.4, verbose: bool = True) -> pd.DataFrame:
    import yfinance as yf
    period = LIMITS[interval][0]
    frames, n = [], len(tickers)
    for i in range(0, n, batch):
        chunk = tickers[i:i + batch]
        for attempt in range(3):
            try:
                raw = yf.download(chunk, period=period, interval=interval, progress=False,
                                  auto_adjust=False, group_by="ticker", threads=True)
                frames.append(_tidy(raw, chunk))
                break
            except Exception as exc:
                if attempt == 2:
                    journal.jot("incidents", {"kind": "download_failed", "interval": interval,
                                              "batch_start": i, "error": str(exc)[:200]})
                time.sleep(2 ** attempt)
        if verbose:
            print(f"    {interval}: {min(i + batch, n)}/{n}", end="\r", flush=True)
        time.sleep(pause)
    if verbose:
        print(" " * 40, end="\r")
    return pd.concat(frames, ignore_index=True) if frames else _tidy(pd.DataFrame(), [])


def merge_save(df: pd.DataFrame, path, verbose: bool = True) -> pd.DataFrame:
    """기존 파일을 덮어쓰지 않고 합친다 — 국장 받고 미장 받으면 국장이 날아가던 문제."""
    if path.exists():
        old = pd.read_parquet(path)
        old["ts"] = pd.to_datetime(old["ts"], utc=True)
        if not df.empty:
            old = old[~old["ticker"].isin(df["ticker"].unique())]  # 새로 받은 종목만 교체
        df = pd.concat([old, df], ignore_index=True) if not old.empty else df
        if verbose:
            print(f"    (기존 {path.name} 과 병합)")
    df = df.drop_duplicates(["ticker", "ts"]).sort_values(["ticker", "ts"]).reset_index(drop=True)
    df.to_parquet(path, index=False)
    return df


def coverage(df: pd.DataFrame, interval: str, want_days: int) -> dict:
    """실제로 뭘 받았는지. 요청 기간을 못 채웠으면 그렇다고 적는다."""
    if df.empty:
        return {"interval": interval, "tickers": 0, "bars": 0, "days": 0,
                "requested_days": want_days, "status": "확보 불가"}
        
    span = (df["ts"].max() - df["ts"].min()).days
    cap = LIMITS[interval][1]
    return {
        "interval": interval, "tickers": int(df["ticker"].nunique()), "bars": int(len(df)),
        "first": str(df["ts"].min().date()), "last": str(df["ts"].max().date()),
        "days": span, "requested_days": want_days,
        "source_cap_days": cap,
        "status": "충족" if span >= want_days * 0.95 else
                  f"부족 — 소스 천장 {cap}일 (요청 {want_days}일의 {span / want_days:.0%})",
    }


def rank_by_liquidity(daily: pd.DataFrame, top: int, lookback: int = 60) -> list[str]:
    """거래대금 상위 N. 매매 전략이 아니라 '체결 가능한가'라는 거래 가능성 필터다."""
    recent = daily[daily["ts"] >= daily["ts"].max() - pd.Timedelta(days=lookback)].copy()
    recent["turnover"] = recent["close"] * recent["volume"]
    med = recent.groupby("ticker")["turnover"].median().sort_values(ascending=False)
    return med.head(top).index.tolist()


def run(markets: list[str], top: int | None, timeframes: list[str],
        want_days: int = 1825) -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    uni = universe(markets)
    print(f"[download] 유니버스: {len(uni)} 종목 ({', '.join(markets)})")
    # 저장은 다른 시장과 합치되, 이번에 받을 목록(uni)은 요청한 시장만이어야 한다.
    upath = DATA / "universe.parquet"
    saved = uni
    if upath.exists():
        prev = pd.read_parquet(upath)
        saved = pd.concat([prev[~prev["market"].isin(markets)], uni], ignore_index=True)
    saved.to_parquet(upath, index=False)

    # 1단계: 일봉을 전종목 받는다 (유일하게 5년이 되는 타임프레임)
    print(f"[download] 1d 전종목 {len(uni)} …")
    daily = fetch(uni["ticker"].tolist(), "1d")
    daily = merge_save(daily, DATA / "bars_1d.parquet")
    reports = [coverage(daily, "1d", want_days)]
    print(f"    1d: {reports[0]['tickers']}종목 {reports[0]['bars']:,}봉 "
          f"{reports[0].get('first')}~{reports[0].get('last')} [{reports[0]['status']}]")

    # 2단계: 인트라데이는 거래대금 상위로 좁힌다 (전종목 x 3TF 는 수 GB·수십 분)
    if top and not daily.empty:
        picked = rank_by_liquidity(daily, top)
        print(f"[download] 인트라데이 대상: 거래대금 상위 {len(picked)} 종목")
    else:
        picked = uni["ticker"].tolist()
        print(f"[download] 인트라데이 대상: 전종목 {len(picked)}")

    for tf in [t for t in timeframes if t != "1d"]:
        print(f"[download] {tf} {len(picked)}종목 …")
        df = fetch(picked, tf)
        df = merge_save(df, DATA / f"bars_{tf}.parquet")
        rep = coverage(df, tf, want_days)
        reports.append(rep)
        print(f"    {tf}: {rep['tickers']}종목 {rep['bars']:,}봉 "
              f"{rep.get('first')}~{rep.get('last')} [{rep['status']}]")

    for rep in reports:
        journal.jot("data_coverage", rep)
    journal.note("data_coverage", f"{'+'.join(markets)} 데이터 확보 현황",
                 "| TF | 종목 | 봉 | 시작 | 끝 | 확보일 | 요청일 | 판정 |\n"
                 "|---|---|---|---|---|---|---|---|\n" + "\n".join(
                     f"| {r['interval']} | {r['tickers']} | {r['bars']:,} | {r.get('first','-')} | "
                     f"{r.get('last','-')} | {r['days']} | {r['requested_days']} | {r['status']} |"
                     for r in reports))
    return {"universe": len(uni), "intraday_universe": len(picked), "coverage": reports}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_trader.download", description="국장·미장 실데이터 수집")
    ap.add_argument("--market", default="kr,us")
    ap.add_argument("--top", type=int, default=300, help="인트라데이를 받을 거래대금 상위 종목 수")
    ap.add_argument("--all", action="store_true", help="인트라데이도 전종목 (수십 분, 수 GB)")
    ap.add_argument("--timeframes", default="4h,1h,15m,1d")
    ap.add_argument("--want-days", type=int, default=1825, help="원하는 기간(일). 기본 5년")
    args = ap.parse_args(argv)

    out = run([m.strip() for m in args.market.split(",") if m.strip()],
              None if args.all else args.top,
              [t.strip() for t in args.timeframes.split(",") if t.strip()],
              want_days=args.want_days)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
