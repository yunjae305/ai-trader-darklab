"""워크포워드 검증: 학습 3년 → 블라인드 1년 → 실전 1년.

세 구간은 시간 순서대로만 흐른다. 블라인드 구간의 데이터는 정책을 고칠 때 쓰지 않고,
실전 구간은 블라인드까지 끝난 정책을 한 번도 손대지 않은 채 그대로 태운다.
이 순서가 깨지면 숫자가 거짓말을 하기 때문에, 구간 경계는 코드가 강제한다.

요청한 기간을 데이터가 못 받쳐주면 그 구간은 '확보 불가'로 건너뛴다 — 짧은 구간을
늘려 채우거나 다른 타임프레임으로 대체하지 않는다.

    python -m ai_trader.walkforward --train-years 3 --blind-years 1 --live-years 1
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from . import autoresearch, backtest, benchmark, brain, broker as bk, config as C, history, journal

SEGMENTS = ("train", "blind", "live")


def split(feed: history.HistoryFeed, train_y: float, blind_y: float, live_y: float) -> dict:
    """시계 끝에서 거꾸로 잘라 세 구간을 만든다. 데이터가 모자라면 모자란다고 적는다."""
    end = feed.clock[-1]
    want = {"train": train_y, "blind": blind_y, "live": live_y}
    total_days = sum(want.values()) * 365.25
    have_days = (end - feed.clock[0]).days

    bounds, cur = {}, end
    for name in reversed(SEGMENTS):
        start = cur - pd.Timedelta(days=int(round(want[name] * 365.25)))
        bounds[name] = (start, cur)
        cur = start
    return {
        "bounds": bounds,
        "have_days": have_days, "want_days": int(total_days),
        "sufficient": have_days >= total_days * 0.95,
        "shortfall_days": max(0, int(total_days - have_days)),
    }


def pick_tickers(feed: history.HistoryFeed, top: int, timeframes: list[str]) -> list[str]:
    """인트라데이가 실제로 있는 종목을 우선하고, 그 안에서 거래대금 순으로 고른다.

    다운로더가 인트라데이를 거래대금 상위에만 받으므로, 일봉 알파벳 순으로 뽑으면
    4h/1h/15m 이 통째로 비어버린다 — 그래서 선택을 다운로더와 같은 기준으로 맞춘다.
    """
    from . import download
    intraday = [tf for tf in timeframes if tf != "1d"]
    have = set()
    for tf in intraday:
        df = feed.frames.get(tf)
        if df is not None and not df.empty:
            have |= set(df["ticker"].unique())
    daily = feed.frames["1d"]
    ranked = download.rank_by_liquidity(daily, top=len(daily["ticker"].unique()))
    preferred = [t for t in ranked if t in have]
    rest = [t for t in ranked if t not in have]
    return (preferred + rest)[:top]


def run_segment(feed: history.HistoryFeed, name: str, lo, hi, policy_text: str,
                tickers: list[str], timeframes: list[str], start_cash: float,
                mlf=None) -> dict:
    """한 구간을 처음부터 끝까지 태운다. 정책은 구간 안에서 바뀌지 않는다."""
    broker = bk.PaperBroker.fresh(start_cash)
    feed.seek(lo)
    curve, days, stopped, tf_days = [], 0, 0, {tf: 0 for tf in timeframes}

    while feed.now <= hi:
        prices = feed.prices(tickers)
        prices = {k: v for k, v in prices.items() if v > 0}
        if prices:
            broker.roll_day(prices, today=f"{name}-{days:04d}")
            broker.check_kill_switch(prices)
            for hit in broker.stop_loss_breaches(prices):  # 경계는 판단보다 먼저다
                try:
                    broker.sell(hit["symbol"], hit["qty"], hit["price"])
                    stopped += 1
                except bk.Rejected:
                    pass
            snapshot = broker.snapshot(prices)
            obs = history.observe_mtf(feed, list(prices), timeframes)
            # "썼다/안 썼다"가 아니라 "구간의 몇 %를 덮었나"를 센다.
            # 3년 구간에서 마지막 1년만 있는 타임프레임을 '썼다'고 적으면 거짓말이 된다.
            for tf in timeframes:
                if any(e["timeframes"].get(tf, {}).get("status") == "ok" for e in obs):
                    tf_days[tf] += 1
            verdict = brain.decide(snapshot, obs, history=None, policy_text=policy_text)
            for d in brain.validate(verdict.get("decisions", []), snapshot, prices,
                                universe=tickers):
                try:
                    (broker.buy(d["symbol"], d["sleeve"], d["quantity"], prices[d["symbol"]])
                     if d["action"] == "BUY" else
                     broker.sell(d["symbol"], d["quantity"], prices[d["symbol"]]))
                except bk.Rejected:
                    pass
            curve.append(broker.equity(prices))
            journal.log_metrics(mlf, {f"{name}_equity": curve[-1]}, step=days)
            days += 1
        if not feed.advance():
            break

    if not curve:
        return {"segment": name, "status": "확보 불가 — 이 구간에 캔들이 없다", "days": 0}
    prices = feed.prices(tickers)
    final = broker.snapshot({k: v for k, v in prices.items() if v > 0})
    return {
        "segment": name, "status": "완료",
        "from": str(lo.date()), "to": str(hi.date()), "days": len(curve),
        **backtest.score(curve),
        "stable_return_pct": round(
            (final["sleeves"]["STABLE"]["equity"] / (start_cash * C.SLEEVES["STABLE"]) - 1) * 100, 3),
        "aggressive_return_pct": round(
            (final["sleeves"]["AGGRESSIVE"]["equity"] / (start_cash * C.SLEEVES["AGGRESSIVE"]) - 1) * 100, 3),
        "trades": len(broker.fills),
        "stop_loss_exits": stopped,
        "vs_benchmark": benchmark.alpha(curve, end=hi),
        "brain": verdict.get("brain"),
        "timeframe_coverage_pct": {k: round(v / len(curve) * 100, 1)
                                  for k, v in tf_days.items() if v},
        "timeframes_missing": [k for k, v in tf_days.items() if not v],
        "equity_curve": [round(v) for v in curve],
    }


def run(train_y: float = 3, blind_y: float = 1, live_y: float = 1, top: int = 20,
        timeframes: list[str] | None = None, improve_iters: int = 0,
        start_cash: float | None = None) -> dict:
    timeframes = timeframes or ["4h", "1h", "15m", "1d"]
    cash = start_cash or C.START_CASH
    feed = history.HistoryFeed.build(timeframes=timeframes)
    avail = feed.available()
    tickers = pick_tickers(feed, top, timeframes)
    feed = history.HistoryFeed.build(tickers=tickers, timeframes=timeframes)

    plan = split(feed, train_y, blind_y, live_y)
    print(f"[walkforward] 종목 {len(tickers)} | 데이터 {plan['have_days']}일 / 요청 {plan['want_days']}일")
    for tf, a in avail.items():
        print(f"    {tf:<4}: {a['status']:<10} {a.get('first','')}~{a.get('last','')} "
              f"({a.get('bars',0):,}봉)")
    if not plan["sufficient"]:
        print(f"[walkforward] 경고: {plan['shortfall_days']}일 부족 — 이른 구간은 잘리거나 비어 나온다")

    results, policy = [], C.POLICY.read_text(encoding="utf-8")
    with journal.mlflow_run("walkforward", params={
            "train_y": train_y, "blind_y": blind_y, "live_y": live_y,
            "timeframes": ",".join(timeframes), "tickers": len(tickers),
            "improve_iters": improve_iters}) as mlf:
        for name in SEGMENTS:
            lo, hi = plan["bounds"][name]
            res = run_segment(feed, name, lo, hi, policy, tickers, timeframes, cash, mlf)
            results.append(res)
            if res["status"] != "완료":
                print(f"  {name:<6}: {res['status']}")
                continue
            print(f"  {name:<6}: {res['from']}~{res['to']} {res['days']}일 | "
                  f"수익 {res['total_return_pct']:+.2f}% 낙폭 {res['max_drawdown_pct']:.2f}% "
                  f"RA {res['risk_adjusted']:.3f} | 거래 {res['trades']}건 "
                  f"| TF {res['timeframe_coverage_pct']}")
            journal.log_metrics(mlf, {
                f"{name}_return_pct": res["total_return_pct"],
                f"{name}_max_drawdown_pct": res["max_drawdown_pct"],
                f"{name}_risk_adjusted": res["risk_adjusted"],
                f"{name}_trades": res["trades"]})

            # 학습 구간이 끝난 뒤에만 정책을 고친다. 블라인드·실전에서는 절대 손대지 않는다.
            if name == "train" and improve_iters:
                print(f"  [train] 자기개선 {improve_iters}회 …")
                autoresearch.run(iters=improve_iters, days=min(90, res["days"]))
                policy = C.POLICY.read_text(encoding="utf-8")

    out = {"plan": {"have_days": plan["have_days"], "want_days": plan["want_days"],
                    "sufficient": plan["sufficient"], "shortfall_days": plan["shortfall_days"]},
           "data_available": avail, "tickers": len(tickers),
           "timeframes": timeframes, "segments": results}
    journal.jot("walkforward", {**out, "segments": [
        {k: v for k, v in r.items() if k != "equity_curve"} for r in results]})
    journal.note("walkforward",
                 f"{train_y}년 학습 / {blind_y}년 블라인드 / {live_y}년 실전 ({len(tickers)}종목)",
                 "| 구간 | 기간 | 일수 | 수익% | 낙폭% | RA | 거래 | TF 커버리지 | 없는 TF |\n"
                 "|---|---|---|---|---|---|---|---|---|\n" + "\n".join(
                     f"| {r['segment']} | {r.get('from','-')}~{r.get('to','-')} | {r.get('days',0)} | "
                     f"{r.get('total_return_pct','-')} | {r.get('max_drawdown_pct','-')} | "
                     f"{r.get('risk_adjusted','-')} | {r.get('trades','-')} | "
                     f"{', '.join(f'{k} {v}%' for k, v in sorted(r.get('timeframe_coverage_pct', {}).items())) or '-'} | "
                     f"{','.join(r.get('timeframes_missing', [])) or '-'} |"
                     for r in results))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_trader.walkforward",
                                 description="3년 학습 / 1년 블라인드 / 1년 실전")
    ap.add_argument("--train-years", type=float, default=3)
    ap.add_argument("--blind-years", type=float, default=1)
    ap.add_argument("--live-years", type=float, default=1)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--timeframes", default="4h,1h,15m,1d")
    ap.add_argument("--improve-iters", type=int, default=0,
                    help="학습 구간 종료 후 policy 자기개선 횟수")
    args = ap.parse_args(argv)
    out = run(args.train_years, args.blind_years, args.live_years, args.top,
              [t.strip() for t in args.timeframes.split(",") if t.strip()],
              args.improve_iters)
    print(json.dumps({"plan": out["plan"], "data_available": out["data_available"],
                      "segments": [{k: v for k, v in s.items() if k != "equity_curve"}
                                   for s in out["segments"]]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
