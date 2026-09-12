"""다크랩 러너. 사람이 보지 않아도 돌고, 하나가 실패해도 서지 않는다."""
from __future__ import annotations

import argparse
import time
import traceback

from . import brain, broker as bk, config as C, correlation, data, journal


def recent_history(feed, limit: int = 12) -> list[dict]:
    """과거 판단 + 그 뒤 실제로 가격이 어떻게 됐는지. 이것이 학습 신호다."""
    out = []
    for rec in journal.read("decisions", limit=limit * 3):
        # 스텁이 만든 가짜 판단을 진짜 AI 의 '과거 내 판단'으로 먹이지 않는다
        if str(rec.get("brain", "")).startswith("offline-stub"):
            continue
        sym = rec.get("symbol")
        then = rec.get("price") or 0
        now = feed.price(sym) if sym else 0
        out.append({
            "ts": rec.get("ts"), "symbol": sym, "action": rec.get("action"),
            "sleeve": rec.get("sleeve"), "quantity": rec.get("quantity"),
            "reason": rec.get("reason"),
            "price_then": then, "price_now": round(now, 1),
            "move_since_pct": round((now - then) / then * 100, 2) if then else None,
            "executed": rec.get("status"),
        })
    return out[-limit:]


def held_correlation(broker, feed) -> dict:
    """보유 종목끼리 같이 움직이는가. 종목 수가 아니라 덩어리 수가 실제 분산이다.

    막지 않는다 — brain 에게 보여주기만 한다. 무엇을 살지는 brain 이 정한다.
    """
    held = list(broker.positions)
    if len(held) < 2:
        return {"symbols": len(held), "note": "보유 2종목 미만 — 상관관계 없음"}
    try:
        series = {s: [b["close"] for b in feed.candles(s, 60)] for s in held}
        return correlation.diversification({s: c for s, c in series.items() if len(c) >= 31})
    except Exception as exc:  # 상관관계가 없다고 랩을 세우지 않는다
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def enforce_stop_loss(broker, prices: dict[str, float]) -> list[dict]:
    """손절선을 넘긴 보유를 전량 정리한다. brain 에게 묻지 않는다 — 사람이 정한 경계다.

    매도가 실패해도 랩은 서지 않는다. 실패했다는 사실을 기록하고 다음 사이클에 다시 시도한다
    (보유가 남아 있으면 다음 사이클에도 같은 종목이 다시 걸린다).
    """
    out = []
    for hit in broker.stop_loss_breaches(prices):
        rec = {"symbol": hit["symbol"], "action": "SELL", "sleeve": hit["sleeve"],
               "quantity": hit["qty"], "confidence": None, "forced": "stop_loss",
               "reason": f"손절 가드레일 {C.STOP_LOSS_PCT}% — 평가손익 {hit['pnl_pct']}%",
               "price": round(hit["price"], 1), "brain": "guardrail", "mode": broker.mode}
        try:
            fill = broker.sell(hit["symbol"], hit["qty"], hit["price"])
            rec.update(status="FILLED", realized_pnl=fill.get("realized_pnl"))
        except bk.Rejected as exc:
            rec.update(status="REJECTED", reason_rejected=str(exc))
        except Exception as exc:
            rec.update(status="ERROR", reason_rejected=f"{type(exc).__name__}: {exc}")
        journal.jot("incidents", {"kind": "stop_loss", "symbol": hit["symbol"],
                                  "pnl_pct": hit["pnl_pct"], "status": rec["status"]})
        out.append(journal.jot("decisions", rec))
    return out


def cycle(broker, feed, mlf=None, step: int | None = None, with_news: bool = True) -> dict:
    """한 사이클: 관측 → 판단 → 집행 → 기록. 어떤 단계가 죽어도 랩은 다음 사이클로 간다."""
    prices = feed.prices(C.UNIVERSE)
    broker.roll_day(prices)
    snapshot = broker.snapshot(prices)
    snapshot["diversification"] = held_correlation(broker, feed)

    killed = broker.check_kill_switch(prices)
    if killed:
        journal.jot("incidents", {"kind": "kill_switch", "detail": killed,
                                  "equity": snapshot["equity"]})

    # 손절은 brain 보다 먼저다. 경계는 판단을 기다리지 않는다.
    results = enforce_stop_loss(broker, prices)

    observations = data.observe(feed, C.UNIVERSE, with_news=with_news)
    verdict = brain.decide(snapshot, observations, recent_history(feed))
    orders = brain.validate(verdict.get("decisions", []), snapshot, prices)

    for d in orders:
        sym = d["symbol"]
        price = prices.get(sym, 0)
        rec = {"symbol": sym, "action": d["action"], "sleeve": d["sleeve"],
               "quantity": d["quantity"], "confidence": d.get("confidence"),
               "reason": d.get("reason", "")[:400], "price": round(price, 1),
               "brain": verdict.get("brain"), "mode": broker.mode}
        try:
            fill = (broker.buy(sym, d["sleeve"], d["quantity"], price) if d["action"] == "BUY"
                    else broker.sell(sym, d["quantity"], price))
            rec.update(status="FILLED", realized_pnl=fill.get("realized_pnl"))
        except bk.Rejected as exc:
            rec.update(status="REJECTED", reason_rejected=str(exc))
        except Exception as exc:  # 브로커가 예상 못 한 이유로 죽어도 랩은 계속 돈다
            rec.update(status="ERROR", reason_rejected=f"{type(exc).__name__}: {exc}")
            journal.jot("incidents", {"kind": "order_error", "symbol": sym,
                                      "trace": traceback.format_exc()[-800:]})
        results.append(journal.jot("decisions", rec))

    prices = feed.prices(C.UNIVERSE)
    after = broker.snapshot(prices)
    broker.save()

    journal.jot("cycles", {
        "mode": broker.mode, "brain": verdict.get("brain"),
        "market_view": verdict.get("market_view", "")[:600],
        "lesson": verdict.get("lesson", "")[:400],
        "proposed": len(verdict.get("decisions", [])), "executed": len(results),
        "filled": sum(1 for r in results if r["status"] == "FILLED"),
        "equity": after["equity"], "day_return_pct": after["day_return_pct"],
        "usage": verdict.get("usage", {}), "error": verdict.get("error"),
    })
    journal.log_metrics(mlf, {
        "equity": after["equity"],
        "day_return_pct": after["day_return_pct"],
        "stable_equity": after["sleeves"]["STABLE"]["equity"],
        "aggressive_equity": after["sleeves"]["AGGRESSIVE"]["equity"],
        "filled_orders": sum(1 for r in results if r["status"] == "FILLED"),
    }, step=step)

    return {"snapshot": after, "verdict": verdict, "decisions": orders, "results": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_trader.loop", description="다크랩 매매 루프")
    ap.add_argument("--paper", action="store_true", help="모의 계좌로 실행 (기본값)")
    ap.add_argument("--live", action="store_true",
                    help="실계좌로 라우팅 — 국내는 키움, 해외는 토스")
    ap.add_argument("--live-data", action="store_true",
                    help="모의투자: 페이퍼 계좌 + 토스 실시세 (주문은 안 나간다)")
    ap.add_argument("--once", action="store_true", help="한 사이클만 돌고 종료")
    ap.add_argument("--no-news", action="store_true", help="뉴스 수집 건너뛰기")
    args = ap.parse_args(argv)

    paper = not args.live
    broker = bk.make_broker(paper=paper)
    feed = data.make_feed(paper=paper, live_data=args.live_data)
    print(f"[darklab] broker={broker.mode} feed={feed.source} "
          f"brain={'claude' if C.have_brain_key() else 'OFFLINE-STUB(키 없음)'}")

    with journal.mlflow_run("darklab-loop", params={
            "broker": broker.mode, "feed": feed.source, "model": C.BRAIN_MODEL,
            "sleeves": C.SLEEVES, "universe": len(C.UNIVERSE)}) as mlf:
        step, was_open = 0, True
        while True:
            if not feed.is_open():
                # 장이 닫힌 동안의 가격은 어제 종가다. 그걸로 판단시키면 없는 매매를 만든다.
                if was_open:  # 마감 순간에만 남긴다 — 15분마다 쌓으면 킬스위치 기록이 묻힌다
                    journal.jot("incidents", {"kind": "market_closed", "source": feed.source})
                was_open = False
                print("[darklab] 장 마감 — 사이클 건너뜀")
                if args.once:
                    return 0
                time.sleep(C.CYCLE_SECONDS)
                continue
            was_open = True
            try:
                out = cycle(broker, feed, mlf, step=step, with_news=not args.no_news)
                journal.note("daily_log",
                             f"cycle {step} · {out['verdict'].get('brain')}",
                             (out["verdict"].get("market_view", "") + "\n\n" +
                              journal.cycle_report(out["snapshot"], out["decisions"], out["results"])))
                print(f"[darklab] cycle {step}: 자산 {out['snapshot']['equity']:,}원 "
                      f"({out['snapshot']['day_return_pct']:+.2f}%), "
                      f"체결 {sum(1 for r in out['results'] if r['status'] == 'FILLED')}건")
            except Exception:
                journal.jot("incidents", {"kind": "cycle_crash",
                                          "trace": traceback.format_exc()[-1500:]})
                print("[darklab] 사이클 실패 — 기록하고 계속 간다")
            step += 1
            if args.once:
                return 0
            time.sleep(C.CYCLE_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
