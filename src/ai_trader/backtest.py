"""한 달 페이퍼 운용. autoresearch 가 정책을 평가할 때도 이 실행기를 쓴다.

백테스트 구간에서는 뉴스를 끈다 — 오늘 헤드라인을 과거 봉에 먹이면 미래를 훔쳐보는 셈이다.
실시간 뉴스는 loop.py 의 라이브 사이클에서만 쓴다.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field

from . import benchmark, brain, broker as bk, config as C, data, journal


@dataclass
class ReplayFeed:
    """어떤 피드에서든 캔들을 한 번 받아와 하루씩 앞으로 재생한다."""
    bars: dict[str, list[tuple[float, float]]]
    warmup: int = 60
    source: str = "replay"
    cursor: int = field(init=False)

    def __post_init__(self):
        self.cursor = min(self.warmup, min((len(b) for b in self.bars.values()), default=0)) - 1

    @classmethod
    def from_feed(cls, feed, symbols: list[str], days: int, warmup: int = 60) -> "ReplayFeed":
        bars = {s: feed.candles(s, days + warmup) for s in symbols}
        return cls(bars={s: b for s, b in bars.items() if len(b) > warmup}, warmup=warmup)

    def candles(self, symbol: str, n: int = 60):
        b = self.bars.get(symbol, [])
        end = min(self.cursor + 1, len(b))
        return b[max(0, end - n):end]

    def price(self, symbol: str) -> float:
        c = self.candles(symbol, 1)
        return c[-1]["close"] if c else 0.0

    def prices(self, symbols):
        return {s: self.price(s) for s in symbols}

    def advance(self) -> bool:
        if self.cursor + 1 >= min((len(b) for b in self.bars.values()), default=0):
            return False
        self.cursor += 1
        return True


def score(equity_curve: list[float]) -> dict:
    """단일 지표로 압축한다. autoresearch 는 이 숫자 하나로 정책을 살리거나 죽인다."""
    if len(equity_curve) < 2 or equity_curve[0] <= 0:
        return {"total_return_pct": 0.0, "max_drawdown_pct": 0.0,
                "daily_vol_pct": 0.0, "risk_adjusted": 0.0}
    rets = [(equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            for i in range(1, len(equity_curve))]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    peak, mdd = equity_curve[0], 0.0
    for v in equity_curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    total = (equity_curve[-1] - equity_curve[0]) / equity_curve[0] * 100
    return {
        "total_return_pct": round(total, 3),
        "max_drawdown_pct": round(mdd * 100, 3),
        "daily_vol_pct": round(vol * 100, 3),
        # 수익을 변동성과 낙폭으로 나눈다. 크게 벌어도 크게 흔들리면 점수를 못 받는다.
        "risk_adjusted": round(total / (vol * 100 + mdd * 100 + 1.0), 4),
    }


def source_feed(source: str, paper: bool = True):
    """백테스트가 무엇을 재생할지 고른다.

    synthetic — 시드 고정 랜덤워크. 배선 점검용이지 시장이 아니다.
    history   — 내려받은 실제 일봉 (data/bars_1d.parquet). 진짜 백테스트는 이것이다.
    toss      — 토스 실시세를 그 자리에서 받아 재생.
    """
    if source == "history":
        from . import history
        hf = history.HistoryFeed.build(timeframes=["1d"])
        hf.cursor = len(hf.clock) - 1          # 전 구간을 다 보이게 두고, 재생은 ReplayFeed 가 한다
        view = history.SymbolView(hf)
        missing = view.missing(C.UNIVERSE)
        if missing:
            print(f"[backtest] 데이터에 없는 종목 {len(missing)}개는 빠진다: {missing}")
        return view
    return data.make_feed(paper=paper, live_data=(source == "toss"))


def run(days: int = 30, policy_text: str | None = None, paper: bool = True,
        start_cash: float | None = None, quiet: bool = False, mlf=None,
        source: str = "synthetic") -> dict:
    base = source_feed(source, paper=paper)
    feed = ReplayFeed.from_feed(base, C.UNIVERSE, days)
    broker = bk.PaperBroker.fresh(start_cash)
    if not feed.bars:
        raise RuntimeError("재생할 캔들이 없다 — 유니버스나 피드를 확인하라")

    curve, day, stopped = [], 0, 0
    while day < days:
        prices = feed.prices(C.UNIVERSE)
        broker.roll_day(prices, today=f"bt-{day:03d}")
        broker.check_kill_switch(prices)  # 라이브와 같은 가드레일을 태운다
        for hit in broker.stop_loss_breaches(prices):
            try:
                broker.sell(hit["symbol"], hit["qty"], hit["price"])
                stopped += 1
            except bk.Rejected:
                pass
        snapshot = broker.snapshot(prices)
        observations = data.observe(feed, C.UNIVERSE, with_news=False)
        verdict = brain_decide(snapshot, observations, policy_text)
        for d in brain.validate(verdict.get("decisions", []), snapshot, prices):
            try:
                (broker.buy(d["symbol"], d["sleeve"], d["quantity"], prices[d["symbol"]])
                 if d["action"] == "BUY" else
                 broker.sell(d["symbol"], d["quantity"], prices[d["symbol"]]))
            except bk.Rejected:
                pass
        curve.append(broker.equity(prices))
        journal.log_metrics(mlf, {"bt_equity": curve[-1]}, step=day)
        if not quiet and day % 5 == 0:
            print(f"  day {day:>3}: {curve[-1]:,.0f}원")
        if not feed.advance():
            break
        day += 1

    prices = feed.prices(C.UNIVERSE)
    final = broker.snapshot(prices)
    result = {
        "days": len(curve), "paper": paper, "source": source, "brain": verdict.get("brain"),
        **score(curve),
        "stable_return_pct": round(
            (final["sleeves"]["STABLE"]["equity"] / (C.START_CASH * C.SLEEVES["STABLE"]) - 1) * 100, 3),
        "aggressive_return_pct": round(
            (final["sleeves"]["AGGRESSIVE"]["equity"] / (C.START_CASH * C.SLEEVES["AGGRESSIVE"]) - 1) * 100, 3),
        "trades": len(broker.fills),
        "stop_loss_exits": stopped,
        "vs_benchmark": benchmark.alpha(curve),
        "final": final,
        "equity_curve": [round(v) for v in curve],
    }
    journal.log_metrics(mlf, {k: v for k, v in result.items() if isinstance(v, (int, float))})
    return result


def brain_decide(snapshot, observations, policy_text):
    return brain.decide(snapshot, observations, history=None, policy_text=policy_text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_trader.backtest", description="한 달 페이퍼 운용")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--source", default="history", choices=["history", "synthetic", "toss"],
                    help="history=내려받은 실제 일봉(기본) / synthetic=랜덤워크 / toss=실시세")
    ap.add_argument("--live-data", action="store_true", help="--source toss 와 같다 (구버전 호환)")
    args = ap.parse_args(argv)

    source = "toss" if args.live_data else args.source
    with journal.mlflow_run("backtest", params={"days": args.days, "model": C.BRAIN_MODEL,
                                                "source": source, "sleeves": C.SLEEVES}) as mlf:
        result = run(days=args.days, paper=not args.live_data, mlf=mlf, source=source)
    journal.jot("backtests", {k: v for k, v in result.items() if k != "final"})
    journal.note("backtests", f"{result['days']}일 페이퍼 운용 ({result['brain']})",
                 "```json\n" + json.dumps(
                     {k: v for k, v in result.items() if k not in ("final", "equity_curve")},
                     ensure_ascii=False, indent=2) + "\n```")
    print(json.dumps({k: v for k, v in result.items() if k not in ("final", "equity_curve")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
