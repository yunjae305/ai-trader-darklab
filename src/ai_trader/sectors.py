"""섹터(업종). 사람이 입력하지 않는다 — 이미 받고 있는 목록에서 나온다.

국장: KIND 상장법인목록에 '업종' 열이 있다 (실측 125종류). download.kr_universe() 가
      universe.parquet 에 같이 저장하므로 별도 수집이 없다.
미장: nasdaqtrader 심볼 목록에는 섹터가 없다. yfinance Ticker.info 의 sector 로 채우고
      lab/sectors_us.json 에 캐시한다 — 종목당 한 번만 묻는다.

섹터는 관측치다. 여기서 "같은 섹터면 사지 마라" 같은 규칙을 만들지 않는다 — 그건 brain 몫이다.
모르면 빈 문자열이다. 추측해서 채우지 않는다.
"""
from __future__ import annotations

import json

from . import config as C

DATA = C.ROOT / "data"
US_CACHE = C.LAB / "sectors_us.json"

_map: dict[str, str] | None = None


def load(refresh: bool = False) -> dict[str, str]:
    """{종목코드: 섹터}. universe.parquet(국장) + lab 캐시(미장)."""
    global _map
    if _map is not None and not refresh:
        return _map
    out: dict[str, str] = {}
    path = DATA / "universe.parquet"
    if path.exists():
        try:
            import pandas as pd
            df = pd.read_parquet(path)
            if "sector" in df.columns:
                for sym, sec in zip(df["symbol"], df["sector"]):
                    if isinstance(sec, str) and sec.strip():
                        out[str(sym)] = sec.strip()
        except Exception:
            pass  # 목록을 못 읽으면 섹터가 없는 것이다. 랩을 세우지 않는다.
    if US_CACHE.exists():
        try:
            out.update({k: v for k, v in json.loads(US_CACHE.read_text(encoding="utf-8")).items() if v})
        except Exception:
            pass
    _map = out
    return out


def of(symbol: str) -> str:
    return load().get(str(symbol), "")


def refresh_us(symbols: list[str], quiet: bool = False) -> dict[str, str]:
    """미장 섹터를 yfinance 에서 받아 캐시한다. 이미 아는 종목은 다시 묻지 않는다."""
    known = {}
    if US_CACHE.exists():
        try:
            known = json.loads(US_CACHE.read_text(encoding="utf-8"))
        except Exception:
            known = {}
    todo = [s for s in symbols if s not in known]
    if todo:
        import yfinance as yf
        for sym in todo:
            try:
                known[sym] = (yf.Ticker(sym).info or {}).get("sector") or ""
            except Exception:
                known[sym] = ""
            if not quiet:
                print(f"    {sym}: {known[sym] or '(없음)'}")
    US_CACHE.parent.mkdir(parents=True, exist_ok=True)
    US_CACHE.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")
    load(refresh=True)
    return known


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ai_trader.sectors", description="섹터 확인·수집")
    ap.add_argument("--refresh-us", action="store_true", help="유니버스의 해외 종목 섹터를 받아온다")
    args = ap.parse_args(argv)

    if args.refresh_us:
        us = [s for s in C.UNIVERSE if C.market_of(s) == "US"]
        if not us:
            print("[sectors] 유니버스에 해외 종목이 없다")
            return 0
        print(f"[sectors] 해외 {len(us)}종목 조회")
        refresh_us(us)

    table = load(refresh=True)
    print(f"[sectors] 아는 종목 {len(table):,}개")
    missing = [s for s in C.UNIVERSE if not of(s)]
    for s in C.UNIVERSE:
        from . import data
        print(f"  {s:<8} {C.market_of(s):<3} {data.NAMES.get(s, s):<14} {of(s) or '(모름)'}")
    if missing:
        print(f"\n[sectors] 모르는 종목 {len(missing)}개: {missing}")
        print("  국장 → python3 -m ai_trader.download --market kr --timeframes 1d")
        print("  미장 → python3 -m ai_trader.sectors --refresh-us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
