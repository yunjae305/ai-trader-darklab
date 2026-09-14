"""텔레그램 명령 수신 — 조회 셋과 '멈추기' 하나만 받는다.

    python3 -m ai_trader.telegramctl     # 명령 대기 (상시 실행)

**여기서도 주문은 내지 않는다.** 받을 수 있는 것은 상태 조회와 정지뿐이다.
매수·매도 명령을 받게 만들면 봇 토큰이 계좌를 여는 열쇠가 된다 — 토큰 하나가
새면 그 문으로 들어온다. 무엇을 사고 팔지는 brain 이 정하고, 실주문 여부는
.env 의 AI_TRADER_LIVE 가 정한다. 그 둘은 텔레그램이 못 건드린다.

켜는 명령도 일부러 뺐다. 멈추는 것은 안전한 방향이지만 켜는 것은 아니다 —
매매를 시작하는 결정은 사람이 화면 앞에서 내린다.

발신자를 확인한다. 봇 주소를 알아낸 누가 말을 걸어도 TELEGRAM_CHAT_ID 가
아니면 아무 일도 일어나지 않는다.
"""
from __future__ import annotations

import json
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from . import config as C, telegram as T

POLL_TIMEOUT = 30       # 롱폴링. 이 초만큼 텔레그램이 들고 있다가 응답한다
HELP = """<b>다크랩 봇</b>
/status — 계좌·엔진 상태
/positions — 보유 종목
/orders — 오늘 주문 (증권사 원장)
/stop — 매매 루프 정지
/help — 이 도움말

매수·매도 명령은 없다. 켜는 것도 화면에서 한다."""


def _get(method: str, **params) -> dict:
    url = T.API.format(token=C.TELEGRAM_TOKEN, method=method)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=POLL_TIMEOUT + 10) as r:
        return json.loads(r.read() or b"{}")


def _won(v) -> str:
    return "—" if v is None else f"{round(float(v)):,}원"


def _pct(v) -> str:
    return "—" if v is None else f"{float(v):+.2f}%"


def cmd_status() -> str:
    from . import dashboard as D, engine as E
    lab, eng = D.lab_state(), E.status()
    return (f"<b>계좌</b> ({lab.get('mode')})\n"
            f"총 평가 {_won(lab.get('equity'))}\n"
            f"예수금 {_won(lab.get('cash'))}\n"
            f"당일 {_pct(lab.get('day_return_pct'))}"
            f" · 누적 {_pct(lab.get('total_return_pct'))}\n"
            f"보유 {len(lab.get('positions') or [])}종목\n\n"
            f"<b>엔진</b> {'돌고 있다' if eng['running'] else '멈춰 있다'}\n"
            f"{eng.get('mode') or ''}\n"
            f"마지막 사이클 {eng.get('last_cycle_at') or '없음'}\n"
            f"실주문 {'ON' if eng.get('live_trading') else 'OFF'}")


def cmd_positions() -> str:
    from . import dashboard as D
    rows = D.lab_state().get("positions") or []
    if not rows:
        return "보유 종목이 없다."
    out = ["<b>보유 종목</b>"]
    for p in rows:
        out.append(f"{p.get('name') or p['symbol']} ({p['symbol']})\n"
                   f"  {p.get('qty')}주 · 평단 {_won(p.get('avg_price'))}"
                   f" → {_won(p.get('last_price'))}\n"
                   f"  평가 {_won(p.get('value'))} · {_pct(p.get('unrealized_pct'))}")
    return "\n".join(out)


def cmd_orders() -> str:
    from . import dashboard as D
    state = D.orders_state()
    if state.get("unavailable"):
        return f"주문을 못 읽었다: {state['unavailable']}"
    rows = state.get("rows") or []
    if not rows:
        return "오늘 이 계좌로 나간 주문이 없다."
    out = [f"<b>오늘 주문</b> ({len(rows)}건)"]
    for r in rows[:15]:
        out.append(f"{r.get('side') or ''} {r.get('name') or r.get('symbol')} "
                   f"[{r.get('state') or ''}]\n"
                   f"  주문 {r.get('order_qty')}주 @ {_won(r.get('order_price'))}\n"
                   f"  체결 {r.get('filled_qty')}주 · 미체결 {r.get('open_qty')}주\n"
                   f"  번호 {r.get('order_no') or '—'}")
    return "\n".join(out)


def cmd_stop() -> str:
    """루프를 세운다. 켜는 명령은 없다 — 멈추는 것만 원격으로 허용한다."""
    from . import engine as E
    out = E.stop()
    if out.get("ok"):
        return "⏹️ 매매 루프를 세웠다. 다시 켜려면 대시보드에서 해라."
    return f"세우지 못했다: {out.get('reason') or '알 수 없음'}"


COMMANDS = {
    "/status": cmd_status,
    "/positions": cmd_positions,
    "/orders": cmd_orders,
    "/stop": cmd_stop,
    "/help": lambda: HELP,
    "/start": lambda: HELP,     # 텔레그램이 첫 대화에서 보내는 인사. 매매 시작이 아니다.
}

# 입력창에 '/' 만 쳐도 텔레그램이 띄워 주는 목록. 외워서 칠 필요가 없어진다.
# /start 는 넣지 않는다 — 첫 대화에서 자동으로 한 번 오는 인사라 메뉴에 있으면
# '매매 시작'으로 읽힌다. 실제로는 도움말만 띄운다.
COMMAND_HELP = {
    "status": "계좌·엔진 상태",
    "positions": "보유 종목",
    "orders": "오늘 주문 (증권사 원장)",
    "stop": "매매 루프 정지",
    "help": "명령 목록",
}


def register_commands() -> bool:
    """봇의 명령 메뉴를 텔레그램에 등록한다. 한 번 등록하면 봇 계정에 남는다."""
    try:
        out = T._call("setMyCommands", {"commands": [
            {"command": name, "description": desc}
            for name, desc in COMMAND_HELP.items()]})
        return bool(out.get("ok"))
    except Exception:
        return False     # 메뉴가 없어도 명령 자체는 그대로 먹는다


def handle(text: str) -> str | None:
    """명령 한 줄 → 답할 말. 모르는 말에는 답하지 않는다."""
    first = (text or "").strip().split()
    if not first:
        return None
    word = first[0].lower().split("@")[0]     # 그룹에서는 /status@봇이름 으로 온다
    fn = COMMANDS.get(word)
    if fn is None:
        return None
    try:
        return fn()
    except Exception as exc:
        return f"⚠️ {type(exc).__name__}: {str(exc)[:300]}"


def serve() -> int:
    if not T.enabled():
        print("[telegramctl] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 .env 에 없다")
        return 1
    # 시작할 때 밀려 있던 메시지는 버린다. 어젯밤에 보낸 /stop 이 지금 실행되면 안 된다.
    offset = 0
    try:
        updates = _get("getUpdates", timeout=0).get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception as exc:
        print(f"[telegramctl] 시작 조회 실패 — 계속 간다: {exc}")
    menu = register_commands()      # '/' 만 쳐도 목록이 뜨게
    print(f"[telegramctl] 명령 대기 중 (chat_id={C.TELEGRAM_CHAT_ID}) "
          f"메뉴={'등록됨' if menu else '등록 실패 — 명령은 그대로 먹는다'}")
    T.send("▶️ 봇 명령 대기 시작\n\n" + HELP)

    while True:
        try:
            out = _get("getUpdates", offset=offset, timeout=POLL_TIMEOUT)
        except Exception:
            time.sleep(5)       # 네트워크가 끊겨도 봇은 계속 기다린다
            continue
        for update in out.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message") or update.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "")
            if chat_id != str(C.TELEGRAM_CHAT_ID):
                # 봇 주소를 알아낸 누가 말을 걸어도 아무 일도 일어나지 않는다.
                print(f"[telegramctl] 모르는 chat_id 무시: {chat_id}")
                continue
            reply = handle(msg.get("text") or "")
            if reply:
                T.send(reply)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ai_trader.telegramctl",
                                 description="텔레그램 명령 수신 (조회 + 정지만)")
    ap.add_argument("--once", metavar="COMMAND",
                    help="명령 하나만 실행해 보고 끝낸다 (예: --once /status)")
    ap.add_argument("--register", action="store_true",
                    help="명령 메뉴만 등록하고 끝낸다 ('/' 입력 시 목록)")
    args = ap.parse_args(argv)
    if args.once:
        print(handle(args.once) or "모르는 명령이다")
        return 0
    if args.register:
        ok = register_commands()
        print("[telegramctl] 메뉴 등록됨" if ok else "[telegramctl] 등록 실패")
        return 0 if ok else 1
    try:
        return serve()
    except KeyboardInterrupt:
        print("\n[telegramctl] 종료")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
