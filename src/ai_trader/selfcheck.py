"""자체 점검. 돈이 걸린 부분만 본다 — 슬리브 회계, 가드레일, 킬스위치, 판단 검증, 정책 경계.

    python -m ai_trader.selfcheck
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from . import autoresearch, backtest, brain, broker as bk, config as C, data, journal, loop


def check(name: str, fn) -> bool:
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return False
    except Exception as exc:
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
        return False
    print(f"  ok    {name}")
    return True


def sleeves_are_separate():
    b = bk.PaperBroker.fresh(10_000_000)
    assert b.cash["STABLE"] == 6_000_000, b.cash
    assert b.cash["AGGRESSIVE"] == 4_000_000, b.cash
    b.buy("005930", "STABLE", 10, 70_000)
    assert b.cash["AGGRESSIVE"] == 4_000_000, "공격 슬리브 현금이 안정 매수에 쓰였다"
    b.sell("005930", 10, 80_000)
    assert b.cash["AGGRESSIVE"] == 4_000_000, "매도 대금이 다른 슬리브로 샜다"
    assert b.realized["STABLE"] > 0 and b.realized["AGGRESSIVE"] == 0


def position_cap_holds():
    b = bk.PaperBroker.fresh(10_000_000)
    try:
        b.buy("005930", "STABLE", 60, 70_000)  # 420만 = STABLE 의 70%
    except bk.Rejected:
        return
    raise AssertionError("20% 상한을 넘는 매수가 통과했다")


def cannot_spend_more_than_sleeve_cash():
    b = bk.PaperBroker.fresh(10_000_000)
    try:
        b.buy("005930", "AGGRESSIVE", 100, 70_000)  # 700만 > 공격 400만
    except bk.Rejected:
        return
    raise AssertionError("슬리브 현금을 초과한 매수가 통과했다")


def buy_sell_rebuy_is_free():
    b = bk.PaperBroker.fresh(10_000_000)
    b.buy("005930", "STABLE", 10, 70_000)
    b.sell("005930", 10, 71_000)
    assert "005930" not in b.positions
    b.buy("005930", "AGGRESSIVE", 5, 69_000)  # 판 종목을 다른 슬리브로 재매수
    assert b.positions["005930"].sleeve == "AGGRESSIVE"
    b.sell("005930", 2, 72_000)               # 부분 매도
    assert b.positions["005930"].qty == 3


def kill_switch_halts_trading():
    b = bk.PaperBroker.fresh(10_000_000)
    b.buy("005930", "STABLE", 15, 70_000)
    crash = {"005930": 70_000 * 0.1}
    assert b.check_kill_switch(crash), "폭락에도 킬스위치가 안 걸렸다"
    try:
        b.buy("000660", "STABLE", 1, 1_000)
    except bk.Rejected:
        return
    raise AssertionError("정지된 랩이 계속 매수했다")


def validate_rejects_impossible_orders():
    b = bk.PaperBroker.fresh(10_000_000)
    snap = b.snapshot({})
    prices = {"005930": 70_000, "000660": 150_000}
    out = brain.validate([
        {"symbol": "005930", "action": "BUY", "sleeve": "STABLE", "quantity": 5, "confidence": 1, "reason": ""},
        {"symbol": "000660", "action": "SELL", "sleeve": "STABLE", "quantity": 5, "confidence": 1, "reason": ""},
        {"symbol": "999999", "action": "BUY", "sleeve": "STABLE", "quantity": 5, "confidence": 1, "reason": ""},
        {"symbol": "005930", "action": "BUY", "sleeve": "STABLE", "quantity": 3, "confidence": 1, "reason": ""},
        {"symbol": "035420", "action": "HOLD", "sleeve": "STABLE", "quantity": 0, "confidence": 1, "reason": ""},
    ], snap, prices)
    syms = [d["symbol"] for d in out]
    assert syms == ["005930"], f"검증이 이상한 주문을 통과시켰다: {syms}"


def observations_carry_no_signal():
    feed = data.SyntheticFeed()
    pack = data.observe(feed, C.UNIVERSE[:2], with_news=False)
    assert len(pack) == 2 and pack[0]["closes_60d"], "관측 팩이 비었다"
    keys = set(pack[0]["observed"])
    banned = {"signal", "action", "buy", "sell", "score", "recommendation"}
    assert not (keys & banned), f"관측 팩에 판단이 섞였다: {keys & banned}"


def score_punishes_volatility():
    calm = [100, 101, 102, 103, 104, 105]
    wild = [100, 130, 80, 120, 90, 105]  # 같은 끝값, 훨씬 큰 흔들림
    assert backtest.score(calm)["risk_adjusted"] > backtest.score(wild)["risk_adjusted"]
    assert backtest.score(wild)["max_drawdown_pct"] > backtest.score(calm)["max_drawdown_pct"]


def policy_guardrails_are_protected():
    current = C.POLICY.read_text(encoding="utf-8")
    assert autoresearch.GUARD_HEADING in current, "policy.md 에 경계 섹션이 없다"
    assert autoresearch._guard_intact(current + "\n\n새 문장\n", current)
    tampered = current.replace("20%", "90%")
    assert not autoresearch._guard_intact(tampered, current), "경계 훼손을 못 잡았다"


def backtest_runs_headless():
    result = backtest.run(days=3, quiet=True)
    assert result["days"] >= 1, result
    assert "risk_adjusted" in result and "equity_curve" in result
    assert set(result["final"]["sleeves"]) == {"STABLE", "AGGRESSIVE"}


def state_survives_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        b = bk.PaperBroker.fresh(10_000_000)
        b.buy("005930", "AGGRESSIVE", 5, 70_000)
        b.save(path)
        again = bk.PaperBroker.load(path)
        assert again.positions["005930"].qty == 5
        assert again.cash == b.cash


def stub_records_never_feed_learning():
    stub = {"brain": "offline-stub", "symbol": "005930", "action": "BUY", "price": 70_000}
    real = {"brain": "claude-opus-5", "symbol": "000660", "action": "BUY", "price": 150_000}
    assert autoresearch._real([stub, real]) == [real], "스텁 기록이 정책 개선 근거로 샜다"
    before = len(journal.read("decisions"))
    journal.jot("decisions", stub)
    try:
        syms = [h["symbol"] for h in loop.recent_history(data.SyntheticFeed())]
        assert "005930" not in syms or before, "스텁 판단이 AI 의 과거 기록으로 들어갔다"
    finally:
        path = C.RESEARCH / "decisions.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + ("\n" if lines[:-1] else ""), encoding="utf-8")


def ported_indicators_still_compute_correctly():
    """invest/signals.py 를 옮겨오면서 계산이 깨지지 않았는지. 검증값은 원본 테스트에서 가져왔다."""
    from . import indicators as I
    assert I.rsi(list(range(1, 30))) == 100.0, "단조 상승 RSI 가 100 이 아니다"
    assert I.rsi(list(range(30, 1, -1))) == 0.0, "단조 하락 RSI 가 0 이 아니다"
    assert I.rsi([1, 2, 3]) is None, "데이터가 모자라면 None 이어야 한다"
    assert I.ma_alignment([100 + i for i in range(60)])["alignment"] == "정배열"
    assert I.ma_alignment([100 - i for i in range(60)])["alignment"] == "역배열"
    # 가격대가 달라도 비교 가능해야 한다 (삼성전자 1314 와 NVDA -0.5 를 그대로 비교하면 오판)
    cheap = I.macd([1 * 1.01 ** i for i in range(60)])
    rich = I.macd([1000 * 1.01 ** i for i in range(60)])
    assert abs(cheap["histogram_pct"] - rich["histogram_pct"]) < 1e-9, "가격 정규화가 깨졌다"
    bars = [{"date": f"d{i}", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
            for i in range(60)]
    assert I.adx(bars)["strength"] == "없음", "평평한 구간에서 추세가 있다고 한다"


def quant_measures_but_never_decides():
    """퀀트 점수는 관측이다. 주문으로 번역하는 순간 판단 주체가 둘이 된다."""
    from . import indicators as I, quant as Q
    assert not hasattr(Q, "position_size_pct"), "비중 결정 함수가 따라 들어왔다"
    bars = [{"date": f"d{i:03d}", "open": 100 * 1.01 ** i, "high": 101 * 1.01 ** i,
             "low": 99 * 1.01 ** i, "close": 100 * 1.01 ** i, "volume": 1000.0 + i}
            for i in range(80)]
    m = Q.measure(I.analyze(bars))
    assert set(m) == {"score", "parts", "reasons", "mode"}, f"측정 외의 것이 섞였다: {sorted(m)}"
    assert set(m["parts"]) == set(Q.COMPONENTS), "구성요소가 빠졌다"
    assert 0 <= m["score"] <= 100 and len(m["reasons"]) == 5

    # 관측 팩 어디에도 '무엇을 해라'가 없어야 한다. macd 의 signal 은 지표 선 이름이라 예외.
    banned = {"verdict", "buy", "sell", "action", "recommendation", "order"}
    pack = data.observe(data.SyntheticFeed(), C.UNIVERSE[:2], with_news=False)

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)

    found = banned & set(keys(pack))
    assert not found, f"관측 팩에 판단이 섞였다: {found}"
    assert pack[0]["quant"]["score"] >= 0, "퀀트 점수가 팩에 안 실렸다"
    assert pack[0]["indicators"]["adx"], "ADX 가 안 실렸다 — OHLC 가 안 넘어온다"


def kiwoom_rejects_bad_orders_before_sending():
    """원장에 가기 전에 걸러야 한다. 호가단위·수량 검증은 invest/kiwoom.py 에서 온 것."""
    from . import kiwoom as K
    K._selfcheck()  # 호가단위 표 + 잘못된 주문 조합 거절 (네트워크 없음)
    assert K.snap_price(70_050) == 70_000, "호가단위 내림이 틀렸다"
    try:
        bk.make_broker(kiwoom=True)
    except RuntimeError:
        return  # 키가 없으면 여기서 막히는 게 맞다
    assert C.have_kiwoom_keys(), "키도 없이 키움 브로커가 만들어졌다"


def market_gate_knows_when_it_is_guessing():
    from datetime import datetime
    assert data.session_now(datetime(2026, 9, 16, 11, 0, tzinfo=data.KST)), "정규장인데 닫혔다고 한다"
    assert not data.session_now(datetime(2026, 9, 16, 22, 0, tzinfo=data.KST)), "밤 10시에 장이 열렸다"
    assert not data.session_now(datetime(2026, 9, 16, 8, 30, tzinfo=data.KST)), "개장 전에 장이 열렸다"
    assert data.trading_day_from({"result": {"isTradingDay": False}}) is False, "휴장일을 못 읽었다"
    assert data.trading_day_from({"isOpen": True}) is True
    # 모르는 응답 형태를 '열림'으로 때려맞히면 휴장일에 주문이 나간다
    assert data.trading_day_from({"sessions": [{"start": "09:00"}]}) is None, "모르는 형태를 추측했다"


def paper_never_trades_on_real_orders():
    synthetic = data.make_feed(paper=True)
    assert synthetic.source == "synthetic" and synthetic.is_open(), "기본 모의는 시뮬레이터여야 한다"
    sim = data.make_feed(paper=True, live_data=True)
    want = "toss" if C.have_broker_keys() else "synthetic"
    assert sim.source == want, f"모의투자 피드가 {sim.source} (기대: {want})"
    assert bk.make_broker(paper=True).mode == "paper", "모의투자가 실계좌 브로커를 잡았다"


CHECKS = [
    ("6:4 슬리브 현금은 섞이지 않는다", sleeves_are_separate),
    ("한 종목 20% 상한이 지켜진다", position_cap_holds),
    ("슬리브 현금을 넘겨 못 산다", cannot_spend_more_than_sleeve_cash),
    ("사고·팔고·재매수가 자유롭다", buy_sell_rebuy_is_free),
    ("일일 손실 킬스위치가 랩을 세운다", kill_switch_halts_trading),
    ("집행 불가능한 판단은 걸러진다", validate_rejects_impossible_orders),
    ("관측 팩에 매매 신호가 없다", observations_carry_no_signal),
    ("평가 지표가 변동성을 벌준다", score_punishes_volatility),
    ("정책의 사람 경계는 못 바꾼다", policy_guardrails_are_protected),
    ("백테스트가 키 없이 돈다", backtest_runs_headless),
    ("재시작해도 계좌가 남는다", state_survives_a_restart),
    ("스텁 기록은 학습에 안 쓰인다", stub_records_never_feed_learning),
    ("이식한 지표 계산이 안 깨졌다", ported_indicators_still_compute_correctly),
    ("퀀트는 측정만 하고 판단 안 한다", quant_measures_but_never_decides),
    ("키움이 잘못된 주문을 먼저 막는다", kiwoom_rejects_bad_orders_before_sending),
    ("장 마감 판정이 추측을 안 한다", market_gate_knows_when_it_is_guessing),
    ("모의투자는 실주문을 안 낸다", paper_never_trades_on_real_orders),
]


def main() -> int:
    print(f"[selfcheck] brain={'claude' if C.have_brain_key() else 'offline-stub'} "
          f"broker={'toss' if C.have_broker_keys() else 'paper'}")
    failed = sum(0 if check(n, f) else 1 for n, f in CHECKS)
    print(f"[selfcheck] {len(CHECKS) - failed}/{len(CHECKS)} 통과")
    return 1 if failed else 0



# ---------- 멀티 타임프레임 / 워크포워드 ----------

def _fake_frames():
    import pandas as pd
    h = pd.date_range("2026-01-01 09:00", periods=24, freq="1h", tz="UTC")
    d = pd.date_range("2026-01-01", periods=10, freq="1D", tz="UTC")
    hourly = pd.DataFrame({"ticker": "TEST", "ts": h, "open": range(1, 25),
                           "high": range(2, 26), "low": range(0, 24),
                           "close": range(1, 25), "volume": [100.0] * 24})
    daily = pd.DataFrame({"ticker": "TEST", "ts": d, "open": range(1, 11),
                          "high": range(2, 12), "low": range(0, 10),
                          "close": range(1, 11), "volume": [100.0] * 10})
    return hourly, daily


def feed_never_shows_the_future():
    from . import history
    hourly, daily = _fake_frames()
    feed = history.HistoryFeed.from_frames({"1h": hourly, "1d": daily})
    feed.cursor = 3                      # 시계는 4번째 일봉에 있다
    for tf, df in (("1d", daily), ("1h", hourly)):
        got = feed.bars("TEST", tf, 100)
        assert not got.empty, f"{tf} 봉이 비었다"
        assert got["ts"].max() <= feed.now, f"{tf} 에서 미래 봉이 새어나왔다: {got['ts'].max()} > {feed.now}"
    assert len(feed.bars("TEST", "1d", 100)) == 4, "일봉 개수가 시계와 안 맞는다"


def resample_4h_aggregates_correctly():
    from . import history
    hourly, _ = _fake_frames()
    bars = history.resample_4h(hourly)
    assert not bars.empty and len(bars) < len(hourly), "4시간봉이 1시간봉보다 안 줄었다"
    first = bars.iloc[0]
    assert first["high"] >= first["low"], "4시간봉 고가가 저가보다 낮다"
    assert bars["ts"].is_monotonic_increasing, "4시간봉 시간이 뒤죽박죽이다"
    assert bars["volume"].sum() == hourly["volume"].sum(), "리샘플에서 거래량이 샜다"


def missing_timeframe_is_reported_not_faked():
    import pandas as pd
    from . import history
    hourly, daily = _fake_frames()
    feed = history.HistoryFeed.from_frames(
        {"1d": daily, "1h": hourly,
         "15m": pd.DataFrame(columns=["ticker", "ts", "open", "high", "low", "close", "volume"])})
    feed.cursor = 5
    pack = history.observe_mtf(feed, ["TEST"], ["1d", "1h", "15m"])
    tfs = pack[0]["timeframes"]
    assert tfs["15m"]["status"] == "unavailable", "없는 타임프레임이 있는 척했다"
    assert "closes" not in tfs["15m"], "없는 타임프레임에 값이 채워졌다"
    assert tfs["1d"]["status"] == "ok" and tfs["1h"]["status"] == "ok"


def walkforward_segments_do_not_overlap():
    import pandas as pd
    from . import history, walkforward
    _, daily = _fake_frames()
    long_daily = pd.DataFrame({
        "ticker": "TEST",
        "ts": pd.date_range("2020-01-01", periods=1800, freq="1D", tz="UTC"),
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.0, "volume": 100.0})
    feed = history.HistoryFeed.from_frames({"1d": long_daily})
    plan = walkforward.split(feed, 3, 1, 1)
    tr, bl, lv = (plan["bounds"][k] for k in ("train", "blind", "live"))
    assert tr[1] <= bl[0] and bl[1] <= lv[0], f"구간이 겹친다: {tr} {bl} {lv}"
    assert tr[0] < tr[1] < lv[1], "구간 순서가 시간순이 아니다"
    assert plan["want_days"] == int(5 * 365.25)
    short = walkforward.split(history.HistoryFeed.from_frames({"1d": daily}), 3, 1, 1)
    assert not short["sufficient"] and short["shortfall_days"] > 0, "데이터 부족을 못 잡았다"


def second_market_does_not_erase_the_first():
    """국장 받고 미장 받으면 국장이 날아가던 문제를 다시 만들지 않는다."""
    import tempfile, pandas as pd
    from . import download
    def frame(tk):
        return pd.DataFrame({"ticker": tk, "ts": pd.date_range("2026-01-01", periods=3, freq="1D", tz="UTC"),
                             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.0, "volume": 10.0})
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bars_1d.parquet"
        download.merge_save(frame("005930.KS"), path, verbose=False)
        merged = download.merge_save(frame("AAPL"), path, verbose=False)
        assert set(merged["ticker"]) == {"005930.KS", "AAPL"}, f"시장이 덮어써졌다: {set(merged['ticker'])}"
        again = download.merge_save(frame("AAPL"), path, verbose=False)
        assert len(again) == 6, f"같은 종목 재수신이 중복을 만들었다: {len(again)}행"


CHECKS += [
    ("피드가 미래 봉을 안 보여준다", feed_never_shows_the_future),
    ("4시간봉 리샘플이 맞다", resample_4h_aggregates_correctly),
    ("없는 타임프레임은 대체 안 한다", missing_timeframe_is_reported_not_faked),
    ("워크포워드 구간이 안 겹친다", walkforward_segments_do_not_overlap),
    ("두 번째 시장이 첫 시장을 안 지운다", second_market_does_not_erase_the_first),
]


if __name__ == "__main__":
    sys.exit(main())
