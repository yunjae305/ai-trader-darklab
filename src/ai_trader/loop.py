"""다크랩 러너. 사람이 보지 않아도 돌고, 하나가 실패해도 서지 않는다."""
from __future__ import annotations

import argparse
import time
import traceback

from . import brain, broker as bk, config as C, correlation, data, journal


def recent_history(feed, limit: int = 12, symbols: list[str] | None = None) -> list[dict]:
    """과거 판단 + 그 뒤 실제로 가격이 어떻게 됐는지. 이것이 학습 신호다."""
    out = []
    for rec in journal.read("decisions", limit=limit * 3):
        # LLM 이 내지 않은 기록(스텁·퀀트 대역·손절 가드레일)을 '과거 내 판단'으로 먹이지 않는다
        if not brain.is_ai(rec.get("brain")):
            continue
        sym = rec.get("symbol")
        if symbols is not None and sym not in symbols:
            continue
        then = rec.get("price") or 0
        now = feed.price(sym) if sym else 0
        out.append({
            "ts": rec.get("ts"), "symbol": sym, "action": rec.get("action"),
            "sleeve": rec.get("sleeve"), "quantity": rec.get("quantity"),
            "reason": rec.get("reason"),
            "price_then": then, "price_now": round(now, 1),
            "move_since_pct": round((now - then) / then * 100, 2) if then else None,
            "executed": rec.get("status"),
            # 거부 사유를 빼면 LLM 은 '왜 안 됐나'를 추측으로 메운다. 실제로
            # 계좌 오류(RC5006)를 수량 문제로 오인해 네 사이클을 태웠다.
            "rejected_reason": rec.get("reason_rejected"),
        })
    return out[-limit:]


def held_correlation(broker, feed, symbols: list[str] | None = None) -> dict:
    """보유 종목끼리 같이 움직이는가. 종목 수가 아니라 덩어리 수가 실제 분산이다.

    막지 않는다 — brain 에게 보여주기만 한다. 무엇을 살지는 brain 이 정한다.
    """
    held = [s for s in broker.positions if symbols is None or s in symbols]
    if len(held) < 2:
        return {"symbols": len(held), "note": "보유 2종목 미만 — 상관관계 없음"}
    try:
        series = {s: [b["close"] for b in feed.candles(s, 60)] for s in held}
        return correlation.diversification({s: c for s, c in series.items() if len(c) >= 31})
    except Exception as exc:  # 상관관계가 없다고 랩을 세우지 않는다
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def _filled(fill: dict, symbol: str) -> dict:
    """체결 기록. **요청한 것이 아니라 실제로 체결된 것**을 적는다.

    brain 이 10주를 요청해도 브로커는 시장 단위에 맞춰 잘라내고, 전량 매도는 보유량까지만
    나간다. 요청 수량만 남겨 두면 장부와 원장이 왜 다른지 나중에 설명할 수가 없다.

    증권사 주문번호가 특히 중요하다. 그게 없으면 우리 기록 한 줄과 증권사 원장 한 줄을
    맞춰 볼 방법이 없다 — 주문이 실제로 나갔는지를 영원히 확인 못 한다.
    """
    sent = fill.get("broker") or {}
    out = {
        "status": "FILLED",
        "filled_qty": fill.get("qty"),                      # 실제 체결 수량
        "fill_price": fill.get("price"),
        "amount": fill.get("cost", fill.get("proceeds")),   # 수수료·세금 포함 금액
        "realized_pnl": fill.get("realized_pnl"),
        "order_no": sent.get("ord_no"),                     # 증권사 주문번호 — 원장 대조의 열쇠
        "venue": sent.get("venue"),
        "broker_response": sent or None,                    # 주문번호 필드명이 바뀌어도 원본은 남는다
    }
    if C.market_of(symbol) == "US":
        # 환산율을 안 적으면 원화 금액을 나중에 재현할 수 없다.
        out["usd_krw"] = C.USD_KRW
    return out


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
            rec.update(_filled(fill, hit["symbol"]))
        except bk.Rejected as exc:
            rec.update(status="REJECTED", reason_rejected=str(exc))
        except Exception as exc:
            rec.update(status="ERROR", reason_rejected=f"{type(exc).__name__}: {exc}")
        journal.jot("incidents", {"kind": "stop_loss", "symbol": hit["symbol"],
                                  "pnl_pct": hit["pnl_pct"], "status": rec["status"]})
        out.append(journal.jot("decisions", rec))
        broker.save()   # 체결 직후에 남긴다 — 여기서 죽으면 판 사실이 사라진다
    return out


def watch(broker, feed, seconds: int) -> str:
    """사이클 사이를 자지 않고 호가창을 본다. 사람이 화면 앞에 앉아 있는 자리다.

    여기서는 LLM 을 부르지 않는다 — 그래서 3초마다 돌아도 비용이 0이고, 손절 반응이
    다음 사이클(최대 15분)이 아니라 몇 초로 줄어든다. 판단이 필요한 사건을 만나면
    남은 시간을 버리고 그 이유를 돌려준다. 무사히 다 기다렸으면 빈 문자열이다.

    시세 조회가 실패해도 멈추지 않는다. 한 번 못 봤다고 장을 놓치는 것보다,
    다음 3초에 다시 보는 편이 낫다.
    """
    deadline = time.time() + seconds
    base: dict[str, float] = {}
    while time.time() < deadline:
        time.sleep(min(C.WATCH_SECONDS, max(0.0, deadline - time.time())))
        held = [p.symbol for p in broker.positions.values()]
        if not held:
            continue                      # 들고 있는 게 없으면 볼 것도 없다
        try:
            prices = {s: v for s, v in feed.prices(held).items() if v > 0}
        except Exception:
            continue                      # 조회 실패는 사건이 아니다
        if not prices:
            continue
        if not feed.is_open():
            return "market_closed"

        if broker.check_kill_switch(prices):
            journal.jot("incidents", {"kind": "kill_switch", "detail": "감시 중 당일 손실 한도"})
            return "kill_switch"
        if enforce_stop_loss(broker, prices):
            return "stop_loss"            # 현금이 생겼다 — 무엇을 할지는 brain 이 정한다

        for pos in broker.positions.values():
            now = prices.get(pos.symbol)
            if not now:
                continue
            pnl = (now - pos.avg) / pos.avg * 100
            if pnl >= C.TAKE_PROFIT_PCT:
                journal.jot("incidents", {"kind": "take_profit_reached",
                                          "symbol": pos.symbol, "pnl_pct": round(pnl, 2)})
                return f"take_profit:{pos.symbol}"
            first = base.setdefault(pos.symbol, now)
            jolt = (now - first) / first * 100
            if abs(jolt) >= C.WATCH_JOLT_PCT:
                journal.jot("incidents", {"kind": "price_jolt", "symbol": pos.symbol,
                                          "detail": f"감시 중 {jolt:+.2f}%"})
                return f"jolt:{pos.symbol}"
    return ""


def cycle(broker, feed, mlf=None, step: int | None = None, with_news: bool = True) -> dict:
    """한 사이클: 관측 → 판단 → 집행 → 기록. 어떤 단계가 죽어도 랩은 다음 사이클로 간다."""
    active = [s for s in C.UNIVERSE if data.feed_open_for(feed, s)]
    prices = feed.prices(active)
    broker.roll_day(prices)
    snapshot = broker.snapshot(prices)
    snapshot["diversification"] = held_correlation(broker, feed, active)

    killed = broker.check_kill_switch(prices)
    if killed:
        journal.jot("incidents", {"kind": "kill_switch", "detail": killed,
                                  "equity": snapshot["equity"]})

    # 손절은 brain 보다 먼저다. 경계는 판단을 기다리지 않는다.
    active_prices = {s: prices.get(s, 0) for s in active}
    results = enforce_stop_loss(broker, active_prices)

    observations = data.observe(feed, active, with_news=with_news)
    verdict = brain.decide(snapshot, observations, recent_history(feed, symbols=active))
    orders = brain.validate(verdict.get("decisions", []), snapshot, active_prices,
                            universe=active)

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
            rec.update(_filled(fill, sym))
        except bk.Rejected as exc:
            rec.update(status="REJECTED", reason_rejected=str(exc))
        except Exception as exc:  # 브로커가 예상 못 한 이유로 죽어도 랩은 계속 돈다
            rec.update(status="ERROR", reason_rejected=f"{type(exc).__name__}: {exc}")
            journal.jot("incidents", {"kind": "order_error", "symbol": sym,
                                      "trace": traceback.format_exc()[-800:]})
        results.append(journal.jot("decisions", rec))
        # 주문 하나마다 장부를 남긴다. 사이클 끝에 한 번만 저장하면, 체결과 저장 사이에
        # 관측·판단·다음 주문이 다 들어가서 1분 넘는 구간이 생긴다. 그 사이에 프로세스가
        # 죽으면 증권사에는 체결이 남고 우리 장부에는 안 남는다 — 그 다음부터 모든
        # 숫자가 거짓말이 된다(이미 쓴 현금을 남아 있다고 여긴다).
        broker.save()

    prices = feed.prices(active)
    after = broker.snapshot(prices)
    broker.save()

    journal.jot("cycles", {
        "mode": broker.mode, "brain": verdict.get("brain"),
        "market_view": verdict.get("market_view", "")[:600],
        "lesson": verdict.get("lesson", "")[:400],
        "watchlist": verdict.get("watchlist", [])[:8],
        "proposed": len(verdict.get("decisions", [])), "executed": len(results),
        "filled": sum(1 for r in results if r["status"] == "FILLED"),
        "equity": after["equity"], "day_return_pct": after["day_return_pct"],
        "usage": verdict.get("usage", {}), "error": verdict.get("error"),
    })
    brain.remember_lesson(verdict.get("lesson", ""), verdict.get("brain", ""))
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
    ap.add_argument("--mock", action="store_true",
                    help="국내 키움 모의주문 + 해외 로컬 모의원장")
    ap.add_argument("--live-data", action="store_true",
                    help="모의투자: 페이퍼 계좌 + 토스 실시세 (주문은 안 나간다)")
    ap.add_argument("--once", action="store_true", help="한 사이클만 돌고 종료")
    ap.add_argument("--no-news", action="store_true", help="뉴스 수집 건너뛰기")
    args = ap.parse_args(argv)

    paper = not (args.live or args.mock)
    broker = bk.make_broker(paper=paper, mock=args.mock)
    if args.mock:
        feed = data.RoutedFeed(kr=data.KiwoomFeed(),
                               us=data.TossFeed() if C.have_broker_keys() else None)
    else:
        feed = data.make_feed(paper=paper, live_data=args.live_data)
    if args.live:
        # 내부 장부가 실계좌와 어긋나면 가드레일이 허구 위에서 계산된다. 시작 전에 맞춘다.
        try:
            synced = broker.sync()
            print(f"[darklab] 실계좌 동기화: 보유 {synced['positions']}종목 "
                  f"현금 {synced['cash']:,}원 {synced['by_market']}")
        except Exception as exc:
            journal.jot("incidents", {"kind": "sync_failed", "detail": str(exc)[:300]})
            print(f"[darklab] 중단 — {exc}")
            return 1

    if args.mock:
        print("[darklab] 모의자본 시장 상한: 국내 50% / 해외 50%")
    brain_name = ({"api": C.BRAIN_MODEL, "cli": f"{C.BRAIN_MODEL}(cli→codex)",
                   "codex": f"{C.CODEX_MODEL or 'codex'}(cli)"}
                  .get(C.brain_backend(), "quant-fallback(LLM 없음)"))
    print(f"[darklab] broker={broker.mode} feed={feed.source} brain={brain_name}")

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
                time.sleep(C.CYCLE_SECONDS)   # 닫힌 장은 들여다볼 것이 없다
                continue
            was_open = True
            started = time.time()
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
            # 다음 판단까지 자지 않고 본다. 사건이 나면 남은 시간을 버리고 바로 깨운다.
            # 사이클에 쓴 시간을 빼야 CYCLE_SECONDS 가 '주기'가 된다. 안 빼면 판단에
            # 걸린 48초가 매번 더해져, 60초로 맞춰도 실제로는 108초마다 돈다.
            woke = watch(broker, feed, max(0.0, C.CYCLE_SECONDS - (time.time() - started)))
            if woke:
                print(f"[darklab] 감시 중 사건 — {woke} · 판단을 앞당긴다")


if __name__ == "__main__":
    raise SystemExit(main())
