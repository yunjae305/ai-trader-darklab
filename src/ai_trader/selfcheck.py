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


def market_cap_is_half_each():
    assert C.MARKET_WEIGHTS == {"KR": 0.5, "US": 0.5}
    b = bk.PaperBroker.fresh(10_000_000)
    old = C.MARKET_WEIGHTS["US"]
    C.MARKET_WEIGHTS["US"] = 0.01
    try:
        try:
            b.buy("AAPL", "AGGRESSIVE", 1, 100)
        except bk.Rejected as exc:
            assert "market cap" in str(exc), exc
        else:
            raise AssertionError("해외 시장 자본 상한을 넘긴 주문이 통과했다")
    finally:
        C.MARKET_WEIGHTS["US"] = old
    snap = b.snapshot({})
    assert snap["market_allocation"]["KR"]["max_weight"] == 0.5


def buy_sell_rebuy_is_free():
    b = bk.PaperBroker.fresh(10_000_000)
    b.buy("005930", "STABLE", 10, 70_000)
    b.sell("005930", 10, 71_000)
    assert "005930" not in b.positions
    b.buy("005930", "AGGRESSIVE", 5, 69_000)  # 판 종목을 다른 슬리브로 재매수
    assert b.positions["005930"].sleeve == "AGGRESSIVE"
    b.sell("005930", 2, 72_000)               # 부분 매도
    assert b.positions["005930"].qty == 3


def fractional_shares_survive_the_round_trip():
    """$3.71 로 NVDA 한 주는 못 산다. 0.017주는 살 수 있어야 한다.

    국내는 반대다 — KRX 에 소수점 주식이 없으므로 0.5주 주문은 0주로 잘려야 한다.
    """
    assert bk.round_qty("NVDA", 0.1131549) == 0.113154, "해외 수량이 6자리에서 안 잘린다"
    assert bk.round_qty("NVDA", "0.123456") == 0.123456, "정확한 6자리가 float 오차로 줄었다"
    assert bk.round_qty("NVDA", "not-a-number") == 0, "잘못된 수량이 주문 후보로 남았다"
    assert bk.round_qty("NVDA", float("nan")) == 0, "NaN 수량이 주문 후보로 남았다"
    assert bk.round_qty("005930", 3.9) == 3.0, "국내에 소수점 주식이 생겼다"
    assert bk.round_qty("NVDA", 0) == 0 and bk.round_qty("NVDA", -5) == 0

    b = bk.PaperBroker.fresh(1_000_000)
    b.buy("NVDA", "AGGRESSIVE", 0.017, 212.04)
    assert b.positions["NVDA"].qty == 0.017, "소수점 매수가 장부에 안 남았다"

    b.sell("NVDA", 0.007, 213.0)
    assert abs(b.positions["NVDA"].qty - 0.01) < bk.QTY_EPSILON, "부분 매도 후 수량이 틀렸다"
    b.sell("NVDA", 0.01, 213.0)
    assert "NVDA" not in b.positions, "전량 매도인데 먼지가 남아 재매수가 막힌다"

    # 국내는 한 주 미만이면 아예 주문이 안 나가야 한다 (0 으로 잘리므로 거절)
    try:
        b.buy("005930", "STABLE", 0.5, 70_000)
        raise AssertionError("국내에서 0.5주가 체결됐다")
    except bk.Rejected:
        pass


def fractional_buy_goes_out_as_amount():
    """토스는 소수점 매수를 quantity 로 안 받는다. 금액(orderAmount)으로 바꿔 보내야 한다.

    quantity 로 보내면 400 invalid-request 다 — 주문이 나간 줄 알고 장부에만 적히는 게
    제일 위험하다.
    """
    body = bk.TossVenue.order_body("NVDA", "BUY", 0.017, 212.04)
    assert body["orderType"] == "MARKET", "소수점 매수가 지정가로 나간다"
    assert "quantity" not in body, "소수점 매수에 quantity 를 실었다 — 400 난다"
    assert body["orderAmount"] == "3.60", f"금액이 틀렸다: {body}"

    sell = bk.TossVenue.order_body("NVDA", "SELL", 0.113154, 212.04)
    assert sell["orderType"] == "MARKET" and sell["quantity"] == "0.113154"
    assert "orderAmount" not in sell, "매도에 금액을 실었다"

    whole = bk.TossVenue.order_body("NVDA", "BUY", 3.0, 212.04)
    assert whole["orderType"] == "LIMIT" and whole["quantity"] == "3", "정수 주문까지 시장가로 나간다"
    assert whole["price"] == "212.04", "토스 decimal 필드가 문자열이 아니다"
    assert bk.TossVenue.order_body("PENNY", "BUY", 1, 0.12349)["price"] == "0.1234"

    # 소수점 6자리를 넘기면 토스가 400 을 준다. round_qty 가 그 전에 잘라야 한다.
    assert len(bk.TossVenue.order_body("NVDA", "SELL",
                                       bk.round_qty("NVDA", 0.1234567), 1)["quantity"]
               .split(".")[1]) == 6


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


def stop_loss_is_a_boundary_not_a_suggestion():
    """-15% 는 사람이 정한 경계다. brain 이 버티기로 해도 넘길 수 없어야 한다."""
    assert C.STOP_LOSS_PCT == -15.0, f"손절선이 -15% 가 아니다: {C.STOP_LOSS_PCT}"
    b = bk.PaperBroker.fresh(10_000_000)
    b.buy("005930", "STABLE", 10, 100_000)
    assert not b.stop_loss_breaches({"005930": 90_000}), "-10% 인데 손절이 걸렸다"
    hits = b.stop_loss_breaches({"005930": 85_000})          # 정확히 -15%
    assert len(hits) == 1 and hits[0]["qty"] == 10, f"-15% 에서 안 걸렸다: {hits}"
    assert b.stop_loss_breaches({"005930": 50_000}), "-50% 인데 손절이 안 걸렸다"
    # 가격을 모르면 손실도 모른다 — 모른 채로 팔지 않는다
    assert not b.stop_loss_breaches({}), "가격 없이 손절을 집행했다"
    assert not b.stop_loss_breaches({"005930": 0}), "가격 0 으로 손절을 집행했다"

    # 루프가 실제로 정리하는지 (brain 호출 없이).
    # enforce_stop_loss 는 저널에 쓴다 — 테스트가 진짜 거래 기록을 오염시키면 안 되므로 되돌린다.
    before = {s: len(journal.read(s)) for s in ("decisions", "incidents")}
    try:
        out = loop.enforce_stop_loss(b, {"005930": 85_000})
        assert out and out[0]["status"] == "FILLED", f"손절 매도가 안 나갔다: {out}"
        assert "005930" not in b.positions, "손절했는데 보유가 남았다"
        assert out[0]["brain"] == "guardrail", "손절이 AI 판단으로 기록됐다"
    finally:
        for stream, kept in before.items():
            path = C.RESEARCH / f"{stream}.jsonl"
            if path.exists():
                lines = path.read_text(encoding="utf-8").splitlines()[:kept]
                path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def buy_sell_hold_are_all_reachable():
    """세 가지가 전부 가능해야 한다. HOLD 는 '주문 없음'으로 나타난다."""
    b = bk.PaperBroker.fresh(10_000_000)
    snap = b.snapshot({})
    prices = {"005930": 70_000, "000660": 150_000}
    base = {"sleeve": "STABLE", "confidence": 1, "reason": ""}

    buys = brain.validate([{"symbol": "005930", "action": "BUY", "quantity": 5, **base}], snap, prices)
    assert [d["action"] for d in buys] == ["BUY"], "BUY 가 막혔다"
    b.buy("005930", "STABLE", 5, 70_000)

    snap = b.snapshot(prices)
    sells = brain.validate([{"symbol": "005930", "action": "SELL", "quantity": 5, **base}], snap, prices)
    assert [d["action"] for d in sells] == ["SELL"], "SELL 이 막혔다"

    holds = brain.validate([{"symbol": "005930", "action": "HOLD", "quantity": 0, **base}], snap, prices)
    assert holds == [], "HOLD 가 주문으로 바뀌었다 — 보유는 아무것도 하지 않는 것이다"
    assert brain.validate([], snap, prices) == [], "무거래 사이클이 불가능하다"
    assert b.positions["005930"].qty == 5, "HOLD 인데 보유가 바뀌었다"

    b.sell("005930", 5, 71_000)
    b.buy("005930", "AGGRESSIVE", 3, 69_000)   # 판 종목을 다른 슬리브로 재매수
    assert b.positions["005930"].sleeve == "AGGRESSIVE", "재매수가 막혔다"


def benchmark_refuses_to_invent_alpha():
    """지수가 없으면 초과수익을 0 으로 채우지 않는다. 없으면 없다고 말해야 한다."""
    from . import benchmark
    flat = benchmark.alpha([100.0, 110.0], symbol="^NOSUCHINDEX")
    assert flat.get("unavailable"), f"없는 지수로 초과수익을 만들었다: {flat}"
    got = benchmark.alpha([100.0] * 30)
    if got.get("unavailable"):
        return  # 아직 지수를 안 받았다 — 그렇다고 말하는 것이 맞다
    assert got["benchmark"] == C.BENCHMARK == "^GSPC"
    assert got["alpha_pct"] == round(
        got["strategy_return_pct"] - got["benchmark_return_pct"], 3), "alpha 계산이 안 맞는다"
    assert got["beat_benchmark"] == (
        got["strategy_return_pct"] > got["benchmark_return_pct"]), "승패 판정이 뒤집혔다"


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
        bk.make_broker(paper=False)
    except RuntimeError as exc:
        assert "쓸 수 있는 증권사가 없다" in str(exc), f"엉뚱한 이유로 막혔다: {exc}"
        return  # 키가 하나도 없으면 여기서 막히는 게 맞다
    assert C.have_kiwoom_keys() or C.have_broker_keys(), "키도 없이 실계좌 브로커가 만들어졌다"


def telegram_commands_cannot_place_orders():
    """텔레그램으로 받을 수 있는 것은 조회와 '멈추기'뿐이다.

    봇 토큰은 언제든 샐 수 있다. 매수·매도 명령을 받게 만들면 그 토큰이 계좌를 여는
    열쇠가 된다. 켜는 명령도 없다 — 멈추는 것은 안전한 방향이지만 켜는 것은 아니다.
    그리고 발신자가 내가 아니면 아무 일도 일어나지 않아야 한다.
    """
    from . import telegramctl as TC

    allowed = {"/status", "/positions", "/orders", "/stop", "/help", "/start"}
    assert set(TC.COMMANDS) == allowed, f"명령이 늘었다: {set(TC.COMMANDS) ^ allowed}"

    # '/' 를 쳤을 때 뜨는 목록과 실제로 먹는 명령이 어긋나면 안 된다.
    # 메뉴에 있는데 안 먹으면 거짓말이고, 먹는데 메뉴에 없으면 외워야 한다.
    menu = {"/" + name for name in TC.COMMAND_HELP}
    assert menu <= allowed, f"메뉴에 없는 명령이 있다: {menu - allowed}"
    assert allowed - menu == {"/start"}, f"메뉴에서 빠진 명령: {allowed - menu - {'/start'}}"
    for word in ("/buy", "/sell", "/order", "/live", "/flat"):
        assert TC.handle(word) is None, f"{word} 에 반응한다"

    src = Path(TC.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]          # 모듈 독스트링은 빼고 코드만 본다
    for banned in (".buy(", ".sell(", ".order(", "make_broker", "AI_TRADER_LIVE",
                   "engine.start", "E.start"):
        assert banned not in body, f"명령 모듈이 주문/실행을 건드린다: {banned}"
    # 켜는 것은 화면에서만 한다. stop 은 있고 start 는 없어야 한다.
    assert "E.stop()" in body and "def cmd_stop" in body, "정지 명령이 사라졌다"

    # 발신자 확인이 살아 있어야 한다 — 이게 빠지면 아무나 /stop 을 누를 수 있다.
    assert "C.TELEGRAM_CHAT_ID" in body and "continue" in body, "발신자 확인이 없다"


def ledger_survives_a_crash_mid_cycle():
    """체결은 났는데 장부에 안 남는 구간이 없어야 한다.

    사이클 끝에 한 번만 저장하면 체결과 저장 사이에 관측(20초)·판단(25초)·다음 주문이
    다 들어간다. 그 1분 사이에 프로세스가 죽으면 증권사에는 체결이 남고 우리 장부에는
    안 남는다 — 이미 쓴 현금을 남아 있다고 여기게 되고, 그 다음부터 20% 상한도
    손절 -15% 도 전부 허구 위에서 계산된다.

    저장 자체도 원자적이어야 한다. 덮어쓰다 죽어서 JSON 이 잘리면 다음 실행의 load()
    가 거기서 터지고, 장부가 통째로 사라진다.
    """
    src = (Path(__file__).parent / "loop.py").read_text(encoding="utf-8")
    body = src.split("def cycle(", 1)[1].split("\ndef ", 1)[0]
    orders_block = body.split("for d in orders:", 1)[1]
    assert "broker.save()" in orders_block.split("prices = feed.prices", 1)[0], \
        "주문 루프 안에서 장부를 저장하지 않는다 — 체결과 저장 사이에 크래시 창이 열린다"
    stop_block = src.split("def enforce_stop_loss(", 1)[1].split("\ndef ", 1)[0]
    assert "broker.save()" in stop_block, "손절 체결 뒤 장부를 저장하지 않는다"

    # 저장은 원자적이어야 한다 — 반쪽 파일이 남으면 load() 가 터진다.
    bsrc = (Path(__file__).parent / "broker.py").read_text(encoding="utf-8")
    save_block = bsrc.split("def save(", 1)[1].split("\n    # ---------- 조회", 1)[0]
    assert "os.replace" in save_block, "장부를 제자리에서 덮어쓴다 — 쓰다 죽으면 잘린다"

    # 실제로 저장하고 다시 읽었을 때 보유·현금·실현손익이 그대로 돌아와야 한다.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        b = bk.PaperBroker.fresh(1_000_000)
        b.buy("005930", "STABLE", 1, 70_000)      # 20% 상한 안에 들어가는 수량
        b.save(path)
        assert not list(Path(tmp).glob("*.tmp")), "임시 파일이 남았다"
        back = bk.PaperBroker.load(path)
        assert back.positions["005930"].qty == 1, back.positions
        assert round(back.cash["STABLE"]) == round(b.cash["STABLE"]), (back.cash, b.cash)
        assert back.realized == b.realized


def telegram_notifies_but_never_orders():
    """알림은 관찰 장치다 — 랩의 일부가 아니다.

    두 가지를 못 하게 막아 둔다.
      1) 텔레그램이 죽거나 꺼져 있어도 기록은 그대로 남아야 한다. 알림이 기록을
         막으면 랩의 유일한 증인이 사라진다.
      2) 텔레그램 경로로 주문이 나가면 안 된다. 봇 토큰이 새는 순간 그게 계좌를
         여는 문이 된다. 무엇을 사고 팔지는 brain 이, 실주문 여부는 .env 가 정한다.
    """
    from . import telegram as T

    src = Path(T.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]          # 모듈 독스트링은 빼고 코드만 본다
    # 주문번호를 '표시'하는 것과 주문을 '내는' 것은 다르다 — 후자만 막는다.
    for banned in ("import broker", "from .broker", "from .kiwoom", "import kiwoom",
                   ".buy(", ".sell(", ".order(", ".send(symbol", "AI_TRADER_LIVE"):
        assert banned not in body, f"알림 모듈이 주문을 낼 수 있다: {banned}"

    # 꺼져 있으면 조용히 아무 일도 안 한다 — 예외를 올리지 않는다.
    assert T.enabled() is False or C.TELEGRAM_TOKEN, "토큰 없이 켜졌다고 한다"
    T.on_record("decisions", {"status": "FILLED", "symbol": "005930", "quantity": 1})
    T.on_record("incidents", {"kind": "kill_switch"})

    # 알림이 터져도 기록은 남는다.
    sent = []
    original_enabled, original_send = T.enabled, T.send
    T.enabled = lambda: True
    T.send = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("텔레그램 죽음"))
    original_research = C.RESEARCH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            C.RESEARCH = Path(tmp)
            rec = journal.jot("decisions", {"symbol": "005930", "status": "FILLED"})
            assert rec["symbol"] == "005930", rec
            written = (Path(tmp) / "decisions.jsonl").read_text(encoding="utf-8")
            assert "005930" in written, "알림이 터지자 기록이 사라졌다"
    finally:
        C.RESEARCH, T.enabled, T.send = original_research, original_enabled, original_send

    # 평범한 기록까지 다 보내면 알림이 배경소음이 된다 — 보낼 것만 보낸다.
    assert T._decision_text({"status": "HOLD"}) is None, "체결도 아닌 것을 알린다"
    assert "market_closed" not in T.INCIDENT_KINDS, "매일 나오는 장 마감을 알린다"
    assert sent == []


def every_trade_is_fully_recorded():
    """한 번의 매매가 남기는 기록에 구멍이 없어야 한다.

    한때 증권사 응답(fill['broker'])을 통째로 버렸다. 그러면 주문번호가 사라지고,
    우리 장부 한 줄과 증권사 원장 한 줄을 맞춰 볼 방법이 영원히 없어진다 —
    "주문이 진짜 나갔나"에 답할 수 없게 된다.

    요청 수량도 체결 수량과 다를 수 있다. 브로커는 시장 단위에 맞춰 잘라내고,
    전량 매도는 보유량까지만 나간다. 요청만 적으면 장부 차이를 설명할 수 없다.
    """
    kr = loop._filled({"side": "BUY", "qty": 7, "price": 70_000, "cost": 490_073,
                       "broker": {"ord_no": "0001234", "venue": "키움증권(demo)",
                                  "return_code": 0}}, "005930")
    for field in ("status", "filled_qty", "fill_price", "amount", "order_no", "venue"):
        assert kr.get(field) is not None, f"체결 기록에 {field} 가 없다: {kr}"
    assert kr["order_no"] == "0001234", "증권사 주문번호를 버렸다"
    assert kr["filled_qty"] == 7 and kr["amount"] == 490_073, kr
    assert kr["broker_response"], "증권사 원본 응답을 안 남겼다"
    assert "usd_krw" not in kr, "국내 거래에 환산율을 적었다"

    us = loop._filled({"side": "SELL", "qty": 0.5, "price": 190.0, "proceeds": 127_800,
                       "realized_pnl": -3_200, "broker": {"venue": "토스증권"}}, "AAPL")
    assert us["usd_krw"] == C.USD_KRW, "해외 거래인데 환산율을 안 적었다"
    assert us["amount"] == 127_800 and us["realized_pnl"] == -3_200, us

    # 체결 경로가 _filled 를 실제로 쓰는가 — 옛날처럼 realized_pnl 만 적으면 안 된다.
    src = (Path(__file__).parent / "loop.py").read_text(encoding="utf-8")
    assert src.count("_filled(") >= 3, "체결 기록 경로가 _filled 를 안 쓰는 데가 있다"
    assert 'status="FILLED"' not in src, "FILLED 를 _filled 밖에서 직접 적는 곳이 남아 있다"


def kiwoom_does_not_burn_its_rate_limit():
    """유량 제한에 걸리면 계좌·시세·주문이 한꺼번에 죽는다. 두 구멍을 막아 뒀다.

    1) 일봉은 base_dt 를 채워 보낸다. 빈 값이면 원장이 1511(필수 입력 값 없음)로 거절한다.
    2) 토큰은 프로세스 사이에서 나눠 쓴다. 대시보드와 매매 루프가 각자 발급받으면 429 다.
    """
    import time as _time
    from . import kiwoom as K

    sent = []
    api = K.Kiwoom(app_key="k", app_secret="s", mode="demo")
    api._token, api._expires_at = "tok", _time.time() + 600
    api._post = lambda path, body, **kw: (sent.append((kw.get("api_id"), body))
                                          or {"stk_dt_pole_chart_qry": []})
    api.candles("005930")
    (api_id, body), = sent
    assert api_id == "ka10081", sent
    assert body.get("base_dt"), "일봉이 base_dt 없이 나간다 — 원장이 1511 로 거절한다"
    assert len(body["base_dt"]) == 8 and body["base_dt"].isdigit(), body

    original = K.TOKEN_CACHE
    with tempfile.TemporaryDirectory() as tmp:
        K.TOKEN_CACHE = Path(tmp) / "kiwoom_token.json"
        try:
            K._save_token("demo:test", "shared-token", _time.time() + 600)
            fresh = K.Kiwoom(app_key="k", app_secret="s", mode="demo")
            fresh._cache_id = lambda: "demo:test"
            fresh._post = lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("캐시에 살아있는 토큰이 있는데 또 발급받았다"))
            assert fresh.token() == "shared-token", "저장된 토큰을 안 쓴다"
            K._save_token("demo:expired", "old", _time.time() - 1)
            assert K._load_token("demo:expired") is None, "만료된 토큰을 그대로 쓴다"
            assert K._load_token("demo:없음") is None
        finally:
            K.TOKEN_CACHE = original


def account_screen_never_invents_numbers():
    """계좌 화면은 증권사가 준 값만 적는다.

    한때 현재가 자리에 매입가를, 평가금액 자리에 매입금액을 넣고 손익을 0 으로 적었다.
    그러면 화면은 언제나 "손익 0원"이라고 단언한다 — 모르는 것을 안다고 말하는 것이다.
    없는 값은 None 으로 내려가서 화면에 '—' 로 찍혀야 한다.
    """
    from . import kiwoom as K

    full = {"acnt_evlt_remn_indv_tot": [
        {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "0000000010",
         "pur_pric": "000000070000", "cur_prc": "+000000068500",
         "pur_amt": "000000000700000", "evlt_amt": "000000000685000",
         "evltv_prft": "-000000000015000", "prft_rt": "-000000002.14"}]}
    row = bk.KiwoomVenue._positions_from(full)[0]
    assert row["last"] == 68_500, f"현재가를 매입가로 덮었다: {row}"
    assert row["value"] == 685_000, f"평가금액이 매입금액으로 적혔다: {row}"
    assert row["pnl"] == -15_000 and row["pnl_pct"] == -2.14, f"손실이 사라졌다: {row}"

    # 증권사가 현재가·평가금액을 안 줬을 때 — 매입가에서 되계산하면 안 된다.
    bare = bk.KiwoomVenue._positions_from(
        {"acnt_evlt_remn_indv_tot": [{"stk_cd": "005930", "rmnd_qty": "10",
                                      "pur_pric": "000000070000"}]})[0]
    assert bare["avg"] == 70_000, bare
    for field in ("last", "value", "pnl", "pnl_pct"):
        assert bare[field] is None, f"{field} 를 지어냈다: {bare[field]!r}"

    total = K.balance_total({"tot_evlt_pl": "-000000000015000", "tot_prft_rt": "-000000002.14"})
    assert total["pnl"] == -15_000 and total["return_pct"] == -2.14, total
    assert total["eval_amount"] is None, "안 준 합계를 0 으로 채웠다"

    # 슬리브는 증권사 계좌에 없는 개념이다 — 비율로 쪼개 만들어 내면 안 된다.
    src = (Path(__file__).parent / "dashboard.py").read_text(encoding="utf-8")
    assert '"sleeves": {}' in src, "계좌 화면이 슬리브를 지어낸다"


def env_file_actually_reaches_config():
    """.env 에 키를 넣으면 실제로 읽혀야 한다. 안 읽으면 키를 넣어도 아무 일이 안 일어난다."""
    import os
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / ".env"
        p.write_text('# 주석\n\nAI_TRADER_ENVTEST=  값  \nBAD_LINE\n'
                     'AI_TRADER_ENVQ="사이 공백"\n'
                     "AI_TRADER_ENVTEST2=이미있음\n", encoding="utf-8")
        for k in ("AI_TRADER_ENVTEST", "AI_TRADER_ENVQ"):
            os.environ.pop(k, None)
        os.environ["AI_TRADER_ENVTEST2"] = "셸값"
        try:
            assert C._load_env(p) is True, ".env 를 못 읽었다"
            # 따옴표 없는 값은 앞뒤 공백을 턴다 — 키 뒤에 붙은 공백은 인증을 조용히 깨뜨린다
            assert os.environ["AI_TRADER_ENVTEST"] == "값", "따옴표 없는 값의 공백이 안 털렸다"
            # 따옴표 안의 공백은 보존한다 (따옴표를 쓴 이유가 그것이다)
            assert os.environ["AI_TRADER_ENVQ"] == "사이 공백", "따옴표 안 내용이 바뀌었다"
            assert os.environ["AI_TRADER_ENVTEST2"] == "셸값", "셸 값을 .env 가 덮었다"
            assert C._load_env(Path(tmp) / "없는파일") is False
        finally:
            for k in ("AI_TRADER_ENVTEST", "AI_TRADER_ENVQ", "AI_TRADER_ENVTEST2"):
                os.environ.pop(k, None)


def no_llm_key_still_trades_on_rules():
    """LLM 이 아예 없어도 무작위가 아니라 퀀트 점수로 매매해야 한다.

    이 PC 에 claude CLI 가 깔려 있으면 그쪽이 잡히므로, 여기서는 백엔드를 명시적으로 끈다.
    끈 상태에서도 매매가 되는지가 검사의 요지다.
    """
    feed = data.SyntheticFeed()
    b = bk.PaperBroker.fresh(10_000_000)
    prices = feed.prices(C.UNIVERSE)
    obs = data.observe(feed, C.UNIVERSE, with_news=False)
    before = C.BRAIN_BACKEND
    C.BRAIN_BACKEND = "off"
    try:
        v = brain.decide(b.snapshot(prices), obs)
    finally:
        C.BRAIN_BACKEND = before
    assert v["brain"] == "quant-fallback", f"퀀트 대역이 안 잡혔다: {v['brain']}"
    assert not brain.is_ai(v["brain"]), "퀀트 판단이 LLM 판단으로 분류됐다"
    assert all(brain.is_ai(x) is False for x in
               ("offline-stub", "quant-fallback", "guardrail", "error:Timeout", "refusal"))
    assert brain.is_ai("claude-opus-5"), "진짜 LLM 기록이 걸러졌다"

    # 점수가 매수선을 넘으면 실제로 사야 한다
    high = [{**o, "quant": {"score": 90.0, "reasons": ["테스트"], "parts": {}, "mode": "trend"}}
            for o in obs[:2]]
    out = brain.decide(b.snapshot(prices), high, allow_cli=False)
    orders = brain.validate(out["decisions"], b.snapshot(prices), prices)
    assert orders and all(d["action"] == "BUY" for d in orders), f"90점인데 안 샀다: {out['decisions']}"
    assert all(d["quantity"] > 0 for d in orders)

    # 점수가 매도선 아래면 보유를 정리해야 한다
    b.buy(obs[0]["symbol"], "STABLE", 3, prices[obs[0]["symbol"]])
    low = [{**obs[0], "quant": {"score": 5.0, "reasons": ["테스트"], "parts": {}, "mode": "trend"}}]
    out = brain.decide(b.snapshot(prices), low, allow_cli=False)
    assert [d["action"] for d in out["decisions"]] == ["SELL"], f"5점인데 안 팔았다: {out['decisions']}"

    # 관측 팩에 점수가 아예 없으면 배선 점검 스텁으로 떨어진다
    bare = [{k: x for k, x in o.items() if k != "quant"} for o in obs]
    assert brain.decide(b.snapshot(prices), bare, allow_cli=False)["brain"] == "offline-stub"


def claude_limit_falls_back_to_codex():
    """auto에서 Claude 구독 한도가 끝나면 같은 판단을 Codex CLI가 이어받아야 한다."""
    old_backend = C.BRAIN_BACKEND
    old_claude = brain.cli_complete
    old_codex = brain.codex_complete
    old_quota = brain._CLAUDE_QUOTA_EXHAUSTED
    C.BRAIN_BACKEND = "auto"
    brain._CLAUDE_QUOTA_EXHAUSTED = False
    brain.cli_complete = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("usage limit reached"))
    brain.codex_complete = lambda *a, **kw: (
        '{"market_view":"ok","lesson":"","watchlist":[],"decisions":[]}',
        {"input": 10, "output": 5})
    try:
        out = brain._local_decide("system", "prompt")
        assert out["brain"].endswith("(cli)") and out["brain"].startswith("codex"), out
        assert brain._CLAUDE_QUOTA_EXHAUSTED, "한도 초과를 다음 사이클에 또 Claude로 보낸다"
    finally:
        C.BRAIN_BACKEND = old_backend
        brain.cli_complete = old_claude
        brain.codex_complete = old_codex
        brain._CLAUDE_QUOTA_EXHAUSTED = old_quota


def news_reports_dead_feeds_instead_of_going_quiet():
    """매체 피드가 죽으면 조용히 빈 화면이 되면 안 된다 — 못 받았다고 말해야 한다."""
    from . import news as N
    assert len(N.FEEDS) >= 5, f"수집 매체가 너무 적다: {len(N.FEEDS)}"
    for f in N.FEEDS:
        assert {"source", "category", "url"} <= set(f), f"피드 정의가 불완전하다: {f}"
        assert f["url"].startswith("http"), f
    # 죽은 주소는 예외를 올리지 않고 unavailable 로 돌려준다 (네트워크 없이 즉시 실패)
    dead = N.fetch_feed({"source": "테스트", "category": "x",
                         "url": "http://127.0.0.1:9/없는피드"}, 3)
    assert dead and dead[0].get("unavailable"), f"죽은 피드가 조용히 넘어갔다: {dead}"
    assert dead[0]["source"] == "테스트", "어느 매체가 죽었는지 안 적혔다"

    # 시각 파싱: RFC822 를 읽고, 못 읽으면 지어내지 않고 빈 문자열
    import xml.etree.ElementTree as ET
    node = ET.fromstring("<item><title>t</title><pubDate>Fri, 12 Sep 2026 16:19:00 +0900</pubDate></item>")
    assert N._when(node).startswith("2026-09-12"), N._when(node)
    assert N._when(ET.fromstring("<item><pubDate>엉터리</pubDate></item>")) == ""
    assert N._when(ET.fromstring("<item></item>")) == ""
    assert N._clean(" <b>제목</b>\n 둘 ") == "제목 둘", N._clean(" <b>제목</b>\n 둘 ")


def start_button_cannot_turn_on_live_trading():
    """버튼은 루프를 띄울 뿐이다. 실계좌로 넘어가는 결정은 .env 에서 사람이 한다."""
    from . import engine as E
    args, mode = E._argv()
    assert args[1:3] == ["-m", "ai_trader.loop"], args
    if C.LIVE_TRADING and (C.have_broker_keys() or C.have_kiwoom_keys()):
        assert "--live" in args, f"실주문 허용인데 모의로 뜬다: {args}"
    elif C.KIWOOM_MODE == "demo" and C.have_kiwoom_keys():
        assert "--mock" in args and "--live" not in args, args
    else:
        assert "--live" not in args, f"실주문 차단인데 --live 가 붙었다: {args}"
        assert "--paper" in args, args
    # 버튼 경로 어디에서도 AI_TRADER_LIVE 를 쓰지 않는다
    src = (Path(E.__file__).read_text(encoding="utf-8"))
    assert "AI_TRADER_LIVE" not in src.split('"""', 2)[2], "엔진이 실주문 스위치를 건드린다"
    assert not E._alive(None) and not E._alive(0), "없는 pid 를 살아있다고 한다"
    assert E.stop()["ok"] is False, "안 돌고 있는데 정지가 성공했다고 한다"

    from . import dashboard as D
    assert set(D.ACTIONS) == {"/api/engine/start", "/api/engine/stop", "/api/engine/once"}
    for path in D.ACTIONS:  # 상태를 바꾸는 경로가 GET 으로 열리면 안 된다
        assert path not in D.ROUTES, f"{path} 가 GET 으로도 열린다"


def every_toss_path_exists_in_the_docs():
    """코드가 부르는 토스 경로가 문서에 실재해야 한다. 지어낸 경로는 키를 넣는 순간 404 다."""
    import re
    doc = (C.RESEARCH / "toss_openapi_reference.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(?:GET|POST) (/[^`{]*)", doc))
    called = set()
    for mod in ("broker", "data"):
        src = (Path(__file__).parent / f"{mod}.py").read_text(encoding="utf-8")
        called |= set(re.findall(r'TOSS_BASE\}(/[\w/.-]+)', src))
        called |= set(re.findall(r'_get\("(/[\w/.-]+)"', src))
    assert called, "토스 경로를 하나도 못 찾았다 — 이 검사가 헛돌고 있다"
    unknown = {p for p in called if not any(p.startswith(d.rstrip("/")) for d in documented)}
    assert not unknown, f"문서에 없는 토스 경로를 부른다: {sorted(unknown)}"


def feeds_follow_the_keys_you_have():
    """시세는 주문과 같은 창구에서 온다. 키가 한쪽만 있으면 그쪽만 실시세다."""
    kr_only = data.RoutedFeed(kr=data.SyntheticFeed(), us=None)
    assert kr_only._feed("005930") is not None, "국내 종목이 국내 창구로 안 간다"
    assert kr_only._feed("AAPL") is None, "창구도 없는데 해외 종목에 피드를 붙였다"
    assert kr_only.price("AAPL") == 0.0, "모르는 가격을 0 이 아닌 값으로 지어냈다"
    assert kr_only.candles("AAPL") == []
    assert kr_only._feed("깨진표기") is None, "이상한 표기를 추측해서 라우팅했다"
    assert "KR=키움" in kr_only.source, kr_only.source

    # 가격 0 인 종목은 주문이 되면 안 된다 — validate 가 걸러야 한다
    b = bk.PaperBroker.fresh(10_000_000)
    out = brain.validate([{"symbol": "005930", "action": "BUY", "sleeve": "STABLE",
                           "quantity": 1, "confidence": 1, "reason": ""}],
                         b.snapshot({}), {"005930": 0})
    assert out == [], "가격 0 인 종목에 주문이 나갔다"

    if not (C.have_broker_keys() or C.have_kiwoom_keys()):
        assert data.make_feed(paper=True, live_data=True).source == "synthetic"

    from . import check as CK
    for key in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "APP_KEY_MOCK", "APP_SECRET_MOCK",
                "KIWOOM_MODE", "ANTHROPIC_API_KEY", "AI_TRADER_LIVE"):
        assert key in CK.ENV_TEMPLATE, f".env 안내에 {key} 가 빠졌다"
    assert "AI_TRADER_LIVE=0" in CK.ENV_TEMPLATE, ".env 기본값이 실주문 허용이면 안 된다"


def sector_comes_from_data_not_from_a_human():
    """섹터는 사람이 입력하지 않는다. KIND 업종(국장) · yfinance sector(미장) 에서 온다."""
    from . import sectors as S
    table = S.load(refresh=True)
    assert isinstance(table, dict)
    if table:  # 유니버스를 아직 안 받았으면 비어 있는 게 맞다
        assert S.of("005930"), "국장 대장주 섹터가 비었다 — universe.parquet 에 sector 열이 없다"
    assert S.of("없는종목코드") == "", "모르는 종목의 섹터를 지어냈다"

    pack = data.observe(data.SyntheticFeed(), C.UNIVERSE[:2], with_news=False)
    assert all("sector" in r for r in pack), "관측 팩에 섹터가 안 실렸다"
    assert all(isinstance(r["sector"], str) for r in pack), "섹터가 문자열이 아니다"

    # 섹터는 관측치다 — 여기서 매매 규칙이 되면 판단 주체가 둘이 된다
    src = Path(S.__file__).read_text(encoding="utf-8")
    for banned in ("BUY", "SELL", "buy_above", "sell_below", "STOP_LOSS"):
        assert banned not in src, f"섹터 모듈이 매매 판단을 한다: {banned}"


def orders_route_by_market():
    """국내는 키움, 해외는 토스. 잘못 라우팅된 주문은 엉뚱한 계좌에서 체결된다."""
    assert C.market_of("005930") == "KR" and C.market_of("247540") == "KR"
    assert C.market_of("AAPL") == "US" and C.market_of("BRK.B") == "US"
    for bad in ("", "12345", "1234567", "005930.KS", "한국"):
        try:
            C.market_of(bad)
        except ValueError:
            continue
        raise AssertionError(f"모르는 표기를 추측해서 라우팅했다: {bad!r}")
    assert set(bk.VENUES) == set(C.MARKETS), "시장과 창구가 안 맞는다"
    assert bk.VENUES["KR"].market == "KR" and bk.VENUES["US"].market == "US"

    # 키가 없는 시장은 조용히 넘어가지 않고 이유를 말해야 한다
    for market, cls in bk.VENUES.items():
        why = cls.unavailable()
        have = C.have_kiwoom_keys() if market == "KR" else C.have_broker_keys()
        assert bool(why) != have, f"{market} 창구의 가용 판정이 키 상태와 어긋난다: {why!r}"

    # 창구가 없는 시장 주문은 장부를 건드리기 전에 막혀야 한다
    class Deaf(bk.RoutedBroker):
        def __init__(self, **kw):
            bk.PaperBroker.__init__(self, **kw)
            self.venues, self.blocked = {}, {"KR": "키 없음", "US": "키 없음"}
            self.mode = "routed:none"
    d = Deaf.fresh(10_000_000)
    before = dict(d.cash)
    try:
        d.buy("005930", "STABLE", 1, 70_000)
    except bk.Rejected as exc:
        assert "KR" in str(exc), exc
    else:
        raise AssertionError("창구도 없이 주문이 나갔다")
    assert d.cash == before and not d.positions, "막힌 주문이 장부를 바꿨다"


def market_gate_knows_when_it_is_guessing():
    from datetime import datetime
    assert data.session_now(datetime(2026, 9, 16, 11, 0, tzinfo=data.KST)), "정규장인데 닫혔다고 한다"
    assert not data.session_now(datetime(2026, 9, 16, 22, 0, tzinfo=data.KST)), "밤 10시에 장이 열렸다"
    assert not data.session_now(datetime(2026, 9, 16, 8, 30, tzinfo=data.KST)), "개장 전에 장이 열렸다"
    assert data.us_session_now(datetime(2026, 9, 16, 10, 0, tzinfo=data.ET)), "미국 정규장이 닫혔다"
    assert not data.us_session_now(datetime(2026, 9, 16, 16, 0, tzinfo=data.ET)), "미국 마감 뒤 장이 열렸다"
    assert not data.us_session_now(datetime(2026, 9, 16, 15, 1, tzinfo=data.ET),
                                   order_amount=True), "소수점 주문 마감 뒤 주문을 허용했다"
    assert data.trading_day_from({"result": {"isTradingDay": False}}) is False, "휴장일을 못 읽었다"
    assert data.trading_day_from({"isOpen": True}) is True
    # 모르는 응답 형태를 '열림'으로 때려맞히면 휴장일에 주문이 나간다
    assert data.trading_day_from({"sessions": [{"start": "09:00"}]}) is None, "모르는 형태를 추측했다"


def paper_never_trades_on_real_orders():
    synthetic = data.make_feed(paper=True)
    assert synthetic.source == "synthetic" and synthetic.is_open(), "기본 모의는 시뮬레이터여야 한다"
    sim = data.make_feed(paper=True, live_data=True)
    if C.have_kiwoom_keys():
        assert sim.source.startswith("routed:KR=키움"), f"키움 시세가 안 잡혔다: {sim.source}"
        assert ("US=토스" in sim.source) == C.have_broker_keys(), sim.source
    else:
        want = "toss" if C.have_broker_keys() else "synthetic"
        assert sim.source == want, f"모의투자 피드가 {sim.source} (기대: {want})"
    assert bk.make_broker(paper=True).mode == "paper", "모의투자가 실계좌 브로커를 잡았다"


CHECKS = [
    ("6:4 슬리브 현금은 섞이지 않는다", sleeves_are_separate),
    ("한 종목 20% 상한이 지켜진다", position_cap_holds),
    ("슬리브 현금을 넘겨 못 산다", cannot_spend_more_than_sleeve_cash),
    ("국내·해외 자본은 각각 50%를 못 넘는다", market_cap_is_half_each),
    ("사고·팔고·재매수가 자유롭다", buy_sell_rebuy_is_free),
    ("해외는 소수점 주식이 산다", fractional_shares_survive_the_round_trip),
    ("소수점 매수는 금액으로 나간다", fractional_buy_goes_out_as_amount),
    ("일일 손실 킬스위치가 랩을 세운다", kill_switch_halts_trading),
    ("집행 불가능한 판단은 걸러진다", validate_rejects_impossible_orders),
    ("관측 팩에 매매 신호가 없다", observations_carry_no_signal),
    ("평가 지표가 변동성을 벌준다", score_punishes_volatility),
    ("정책의 사람 경계는 못 바꾼다", policy_guardrails_are_protected),
    ("백테스트가 키 없이 돈다", backtest_runs_headless),
    ("재시작해도 계좌가 남는다", state_survives_a_restart),
    ("스텁 기록은 학습에 안 쓰인다", stub_records_never_feed_learning),
    ("손절 -15% 는 넘을 수 없다", stop_loss_is_a_boundary_not_a_suggestion),
    ("사기·팔기·보유가 전부 가능하다", buy_sell_hold_are_all_reachable),
    ("벤치마크 없이 초과수익을 안 지어낸다", benchmark_refuses_to_invent_alpha),
    ("이식한 지표 계산이 안 깨졌다", ported_indicators_still_compute_correctly),
    ("퀀트는 측정만 하고 판단 안 한다", quant_measures_but_never_decides),
    ("키움이 잘못된 주문을 먼저 막는다", kiwoom_rejects_bad_orders_before_sending),
    ("계좌 화면이 숫자를 안 지어낸다", account_screen_never_invents_numbers),
    ("매매 기록에 구멍이 없다", every_trade_is_fully_recorded),
    ("알림은 보내기만 한다", telegram_notifies_but_never_orders),
    ("체결이 장부에서 사라지지 않는다", ledger_survives_a_crash_mid_cycle),
    ("텔레그램 명령은 주문을 못 낸다", telegram_commands_cannot_place_orders),
    ("키움 유량 제한을 안 태운다", kiwoom_does_not_burn_its_rate_limit),
    ("국내는 키움·해외는 토스로 갈린다", orders_route_by_market),
    ("죽은 뉴스 피드를 조용히 안 넘긴다", news_reports_dead_feeds_instead_of_going_quiet),
    ("시작 버튼이 실주문을 못 켠다", start_button_cannot_turn_on_live_trading),
    ("토스 경로가 전부 문서에 있다", every_toss_path_exists_in_the_docs),
    ("시세가 가진 키를 따라간다", feeds_follow_the_keys_you_have),
    ("섹터는 사람이 안 넣는다", sector_comes_from_data_not_from_a_human),
    (".env 키가 실제로 읽힌다", env_file_actually_reaches_config),
    ("LLM 키 없어도 규칙으로 매매한다", no_llm_key_still_trades_on_rules),
    ("Claude 한도 초과 시 Codex가 이어받는다", claude_limit_falls_back_to_codex),
    ("장 마감 판정이 추측을 안 한다", market_gate_knows_when_it_is_guessing),
    ("모의투자는 실주문을 안 낸다", paper_never_trades_on_real_orders),
]


def main() -> int:
    print(f"[selfcheck] brain={C.brain_backend()} "
          f"broker={'toss' if C.have_broker_keys() else 'paper'}")
    # 점검은 가짜 손절과 가짜 킬스위치를 일부러 일으킨다. 그게 실제 연구 기록에 남거나
    # 텔레그램으로 나가면, 그 다음부터 무엇이 진짜 매매였는지 구분할 수 없다.
    # 기록은 랩의 유일한 증인이다 — 점검이 증인을 오염시키면 안 된다.
    saved_research, saved_token = C.RESEARCH, C.TELEGRAM_TOKEN
    try:
        with tempfile.TemporaryDirectory() as tmp:
            C.RESEARCH = Path(tmp)
            C.TELEGRAM_TOKEN = ""       # 점검 중에는 알림을 보내지 않는다
            # 쓰기는 격리하되, 읽기 전용 참조 문서는 따라와야 한다.
            doc = saved_research / "toss_openapi_reference.md"
            if doc.exists():
                (C.RESEARCH / doc.name).write_text(doc.read_text(encoding="utf-8"),
                                                   encoding="utf-8")
            failed = sum(0 if check(n, f) else 1 for n, f in CHECKS)
    finally:
        C.RESEARCH, C.TELEGRAM_TOKEN = saved_research, saved_token
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
