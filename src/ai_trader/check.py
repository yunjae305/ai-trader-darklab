"""연결 점검 — 키를 넣은 뒤 제일 먼저 돌린다. 조회만 하고 주문은 내지 않는다.

    python -m ai_trader.check

무엇이 되고 무엇이 안 되는지, 안 되면 무엇을 넣어야 하는지를 한 화면에 보여준다.
"되는 척"을 하지 않는 것이 이 파일의 유일한 일이다.
"""
from __future__ import annotations

from . import broker as bk, config as C

OK, NO, WARN = "✓", "✗", "·"


def _env() -> list[tuple[str, str, str]]:
    src = str(C.ROOT / ".env")
    if C.ENV_LOADED:
        return [(OK, ".env", f"로드됨 ({src})")]
    return [(WARN, ".env", f"없음 — {src} 에 키를 넣어라 (.env.example 복사)")]


def _brain() -> list[tuple[str, str, str]]:
    if not C.have_brain_key():
        return [(WARN, "판단 엔진", "ANTHROPIC_API_KEY 없음 → 퀀트 점수로 매매한다 "
                                   f"(매수 {C.QUANT_BUY_ABOVE:.0f} / 매도 {C.QUANT_SELL_BELOW:.0f})")]
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return [(NO, "판단 엔진", "ANTHROPIC_API_KEY 는 있는데 anthropic 미설치 "
                                  "— pip3 install --user anthropic")]
    return [(OK, "판단 엔진", f"{C.BRAIN_MODEL}")]


def _venue(market: str) -> list[tuple[str, str, str]]:
    cls = bk.VENUES[market]
    label = f"{market} 주문"
    why = cls.unavailable()
    if why:
        return [(NO, label, why)]
    try:
        venue = cls()
    except Exception as exc:
        return [(NO, label, f"{type(exc).__name__}: {str(exc)[:120]}")]

    rows = [(OK, label, venue.name)]
    try:
        held = venue.positions()
        cash = venue.cash()
        rows.append((OK, f"{market} 잔고", f"보유 {len(held)}종목 · 주문가능 {cash:,.0f}"))
    except Exception as exc:
        rows.append((NO, f"{market} 잔고",
                     f"{type(exc).__name__}: {str(exc)[:120]} — 실매매는 여기서 막힌다"))
    live = "나감" if (market == "KR" and getattr(venue, "api", None)
                    and venue.api.mode == "demo") or C.LIVE_TRADING else "안 나감"
    rows.append((WARN, f"{market} 실주문", f"{live} (AI_TRADER_LIVE={'1' if C.LIVE_TRADING else '0'})"))
    return rows


def _feed() -> list[tuple[str, str, str]]:
    from . import data
    if not C.have_broker_keys():
        return [(WARN, "시세", "토스 키 없음 → 합성 시장으로 돈다 (실매매 불가)")]
    try:
        feed = data.TossFeed()
        got = feed.prices(C.UNIVERSE[:3])
        live = {k: v for k, v in got.items() if v > 0}
        status = OK if live else NO
        return [(status, "시세", f"토스 — {len(live)}/{len(C.UNIVERSE[:3])}종목 응답 {live}"),
                (WARN, "장 운영", "열림" if feed.is_open() else "닫힘 (사이클은 건너뛴다)")]
    except Exception as exc:
        return [(NO, "시세", f"{type(exc).__name__}: {str(exc)[:140]}")]


def _data() -> list[tuple[str, str, str]]:
    from . import benchmark, history
    rows = []
    try:
        hf = history.HistoryFeed.build(timeframes=["1d"])
        view = history.SymbolView(hf)
        missing = view.missing(C.UNIVERSE)
        rows.append((OK if not missing else WARN, "백테스트 데이터",
                     f"{len(hf.tickers):,}종목"
                     + (f" · 유니버스 {len(missing)}개 없음 {missing[:5]}" if missing else "")))
    except Exception as exc:
        rows.append((NO, "백테스트 데이터",
                     f"{str(exc)[:100]} — python3 -m ai_trader.download --market kr"))
    if benchmark.load().empty:
        rows.append((NO, "벤치마크", f"{C.BENCHMARK} 없음 — python3 -m ai_trader.benchmark"))
    else:
        rows.append((OK, "벤치마크", f"{C.BENCHMARK} {len(benchmark.load()):,}봉"))
    return rows


def describe() -> list[tuple[str, str, str]]:
    rows = _env() + _brain() + _feed()
    for market in C.MARKETS:
        rows += _venue(market)
    return rows + _data()


def main(argv=None) -> int:
    rows = describe()
    width = max(len(label) for _, label, _ in rows)
    print("[check] 조회만 한다 — 주문은 내지 않는다\n")
    for mark, label, detail in rows:
        print(f"  {mark} {label:<{width}}  {detail}")
    blocked = [f"{label}: {detail}" for mark, label, detail in rows if mark == NO]
    tradable = [m for m in C.MARKETS if not bk.VENUES[m].unavailable()]
    print()
    if tradable:
        print(f"[check] 주문 가능한 시장: {', '.join(tradable)}")
    else:
        print("[check] 주문 가능한 시장이 없다 — 모의 장부로만 돈다")
    if blocked:
        print(f"[check] 막힌 항목 {len(blocked)}개:")
        for b in blocked:
            print(f"    - {b}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
