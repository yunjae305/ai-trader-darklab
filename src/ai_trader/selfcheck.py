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
    """LLM 키가 없어도 무작위가 아니라 퀀트 점수로 매매해야 한다."""
    feed = data.SyntheticFeed()
    b = bk.PaperBroker.fresh(10_000_000)
    prices = feed.prices(C.UNIVERSE)
    obs = data.observe(feed, C.UNIVERSE, with_news=False)
    v = brain.decide(b.snapshot(prices), obs)
    assert v["brain"] == "quant-fallback", f"퀀트 대역이 안 잡혔다: {v['brain']}"
    assert not brain.is_ai(v["brain"]), "퀀트 판단이 LLM 판단으로 분류됐다"
    assert all(brain.is_ai(x) is False for x in
               ("offline-stub", "quant-fallback", "guardrail", "error:Timeout", "refusal"))
    assert brain.is_ai("claude-opus-5"), "진짜 LLM 기록이 걸러졌다"

    # 점수가 매수선을 넘으면 실제로 사야 한다
    high = [{**o, "quant": {"score": 90.0, "reasons": ["테스트"], "parts": {}, "mode": "trend"}}
            for o in obs[:2]]
    out = brain.decide(b.snapshot(prices), high)
    orders = brain.validate(out["decisions"], b.snapshot(prices), prices)
    assert orders and all(d["action"] == "BUY" for d in orders), f"90점인데 안 샀다: {out['decisions']}"
    assert all(d["quantity"] > 0 for d in orders)

    # 점수가 매도선 아래면 보유를 정리해야 한다
    b.buy(obs[0]["symbol"], "STABLE", 3, prices[obs[0]["symbol"]])
    low = [{**obs[0], "quant": {"score": 5.0, "reasons": ["테스트"], "parts": {}, "mode": "trend"}}]
    out = brain.decide(b.snapshot(prices), low)
    assert [d["action"] for d in out["decisions"]] == ["SELL"], f"5점인데 안 팔았다: {out['decisions']}"

    # 관측 팩에 점수가 아예 없으면 배선 점검 스텁으로 떨어진다
    bare = [{k: x for k, x in o.items() if k != "quant"} for o in obs]
    assert brain.decide(b.snapshot(prices), bare)["brain"] == "offline-stub"


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
    ("손절 -15% 는 넘을 수 없다", stop_loss_is_a_boundary_not_a_suggestion),
    ("사기·팔기·보유가 전부 가능하다", buy_sell_hold_are_all_reachable),
    ("벤치마크 없이 초과수익을 안 지어낸다", benchmark_refuses_to_invent_alpha),
    ("이식한 지표 계산이 안 깨졌다", ported_indicators_still_compute_correctly),
    ("퀀트는 측정만 하고 판단 안 한다", quant_measures_but_never_decides),
    ("키움이 잘못된 주문을 먼저 막는다", kiwoom_rejects_bad_orders_before_sending),
    ("국내는 키움·해외는 토스로 갈린다", orders_route_by_market),
    ("죽은 뉴스 피드를 조용히 안 넘긴다", news_reports_dead_feeds_instead_of_going_quiet),
    ("시작 버튼이 실주문을 못 켠다", start_button_cannot_turn_on_live_trading),
    ("토스 경로가 전부 문서에 있다", every_toss_path_exists_in_the_docs),
    ("시세가 가진 키를 따라간다", feeds_follow_the_keys_you_have),
    ("섹터는 사람이 안 넣는다", sector_comes_from_data_not_from_a_human),
    (".env 키가 실제로 읽힌다", env_file_actually_reaches_config),
    ("LLM 키 없어도 규칙으로 매매한다", no_llm_key_still_trades_on_rules),
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
