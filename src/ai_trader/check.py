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
    backend = C.brain_backend()
    if backend == "cli":
        fallback = " → Codex CLI 자동 전환" if C.have_codex() else ""
        return [(OK, "판단 엔진", f"{C.BRAIN_MODEL} · Claude CLI{fallback} (API 키 불필요)")]
    if backend == "codex":
        return [(OK, "판단 엔진", f"{C.CODEX_MODEL or 'Codex 설정 모델'} · Codex CLI")]
    if backend == "none":
        requested = f"AI_TRADER_BRAIN={C.BRAIN_BACKEND} · " if C.BRAIN_BACKEND != "auto" else ""
        return [(WARN, "판단 엔진", requested + "LLM 없음 → 퀀트 점수로 매매한다 "
                f"(매수 {C.QUANT_BUY_ABOVE:.0f} / 매도 {C.QUANT_SELL_BELOW:.0f})")]
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return [(NO, "판단 엔진", "ANTHROPIC_API_KEY 는 있는데 anthropic 미설치 "
                                  "— pip3 install --user anthropic")]
    return [(OK, "판단 엔진", f"{C.BRAIN_MODEL}")]


def _dart() -> list[tuple[str, str, str]]:
    if C.DART_API_KEY:
        return [(OK, "DART 재무제표", "키 있음 · 상세실적에서 처음 열 때 실제 연결 확인")]
    return [(WARN, "DART 재무제표", "DART_API_KEY 없음 · 국내 종목 상세실적만 비활성")]


def _telegram() -> list[tuple[str, str, str]]:
    """알림 경로. 루프는 사람 없이 도니까, 이게 꺼져 있으면 사고를 몇 시간 뒤에 안다."""
    from . import telegram as T
    if T.enabled():
        return [(OK, "알림", "텔레그램 · 체결·손절·킬스위치·엔진 오류")]
    if C.TELEGRAM_TOKEN:
        return [(WARN, "알림", "TELEGRAM_CHAT_ID 없음 · `python3 -m ai_trader.telegram --chat-id`")]
    return [(WARN, "알림", "TELEGRAM_BOT_TOKEN 없음 · 사고가 나도 화면을 열어야만 안다")]


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
    rows = _env() + _brain() + _dart() + _telegram() + _feed()
    for market in C.MARKETS:
        rows += _venue(market)
    return rows + _data()


ENV_TEMPLATE = """\
# --- 판단 엔진: auto(기본) | api | cli | codex | off ---
# auto는 API → Claude CLI → Codex CLI 순서이며 Claude 한도가 끝나면 Codex로 넘어간다.
ANTHROPIC_API_KEY=
AI_TRADER_BRAIN=auto
AI_TRADER_MODEL=claude-opus-5
AI_TRADER_RESEARCH_MODEL=claude-opus-5
AI_TRADER_CODEX_MODEL=
AI_TRADER_BRAIN_TIMEOUT=240

# --- 토스증권 Open API (해외 주문 + 국내·해외 시세) ---
# WTS 로그인 > 설정 > Open API 에서 발급. 같은 화면의 '허용 IP 관리'에 이 PC 의 IP 도 등록해야 한다.
TOSS_CLIENT_ID=
TOSS_CLIENT_SECRET=
# 비워두면 GET /api/v1/accounts 로 알아서 찾는다. 계좌가 여럿이면 쓸 accountSeq 를 적어라.
TOSS_ACCOUNT=

# --- DART 전자공시 (국내 종목 상세실적) ---
DART_API_KEY=

# --- 텔레그램 알림 (체결·손절·킬스위치·엔진 오류) ---
# 1) 텔레그램에서 @BotFather 에게 /newbot → 토큰을 받는다
# 2) 만든 봇에게 아무 메시지나 보낸 뒤 `python3 -m ai_trader.telegram --chat-id`
# 알림은 보내기만 한다 — 이 경로로 주문은 나가지 않는다.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# --- 키움증권 REST (국내 주문 + 국내 시세) ---
# demo = mockapi.kiwoom.com (모의투자, 돈 안 걸림) / real = api.kiwoom.com
KIWOOM_MODE=demo
APP_KEY_MOCK=
APP_SECRET_MOCK=
# 실계좌용 (KIWOOM_MODE=real 일 때만 읽는다)
APP_KEY=
APP_SECRET=

# --- 실주문 스위치. 1 이어야만 돈이 걸리는 주문이 나간다 ---
# 키움 demo 는 이 값과 무관하게 나간다 (모의투자 서버라 돈이 안 걸린다).
AI_TRADER_LIVE=0

# --- 선택 ---
# GS_QUANT_PATH=
# AI_TRADER_DASH_TOKEN=
"""


def env_template() -> str:
    return ENV_TEMPLATE


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ai_trader.check", description="연결 점검 (조회만)")
    ap.add_argument("--write-env", action="store_true",
                    help="빈 .env 를 만든다. 이미 있으면 덮지 않는다")
    args = ap.parse_args(argv)

    if args.write_env:
        path = C.ROOT / ".env"
        if path.exists():
            print(f"[check] {path} 가 이미 있다 — 덮지 않는다. 직접 열어서 채워라")
            return 1
        path.write_text(ENV_TEMPLATE, encoding="utf-8")
        print(f"[check] {path} 를 만들었다. 값을 채운 뒤 python3 -m ai_trader.check 를 다시 돌려라")
        return 0

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
    if not C.ENV_LOADED:
        path = C.ROOT / ".env"
        print(f"\n[check] {path} 가 없다. 아래를 그대로 붙여넣고 값만 채우면 된다:\n")
        print("\n".join("    " + ln for ln in ENV_TEMPLATE.splitlines()))
        print("\n[check] 한 줄로 만들려면:  python3 -m ai_trader.check --write-env")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
