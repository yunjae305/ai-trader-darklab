"""텔레그램 알림 — 랩이 사고를 쳤을 때 사람에게 닿는 유일한 경로.

루프는 15분마다 사람 없이 돈다. 킬스위치가 내려가도, 손절이 나가도, 키움 인증이
끊겨도 지금까지는 research/*.jsonl 에 적히고 끝이었다. 대시보드를 열어봐야만
알 수 있었고, 자는 동안에는 몇 시간 뒤에 알았다.

**여기서 주문을 내지 않는다.** 보내기만 한다. 텔레그램으로 매수를 받게 만들면
계좌를 여는 문이 하나 더 생기고, 봇 토큰이 새는 순간 그 문으로 들어온다.
무엇을 사고 팔지는 brain 이 정하고, 실주문 여부는 .env 의 AI_TRADER_LIVE 가 정한다.

    python3 -m ai_trader.telegram --chat-id   # 봇에게 말을 건 뒤 chat_id 를 찾는다
    python3 -m ai_trader.telegram --test      # 연결 확인용 한 줄 보내기
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import config as C

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 10

# 어떤 기록이 사람을 깨울 만한 일인가. 여기 없는 것은 조용히 기록만 된다.
# 사이클마다 나오는 평범한 기록까지 보내면 알림이 배경소음이 되고, 그러면
# 정작 킬스위치가 내려간 날에도 안 읽게 된다.
INCIDENT_KINDS = {
    "kill_switch": "🛑 당일 손실 한도 — 랩 정지",
    "stop_loss": "✂️ 손절 발동",
    "order_error": "⚠️ 주문 중 예외",
    "engine_error": "⚠️ 엔진 오류",
    "engine_start": "▶️ 매매 루프 시작",
    "engine_stop": "⏹️ 매매 루프 정지",
    "cycle_crash": "💥 사이클 실패 — 다음 사이클로 넘어간다",
    "sync_failed": "🔌 실계좌 동기화 실패 — 매매를 시작하지 않았다",
}
# market_closed / dashboard_error 는 일부러 뺐다. 매일·수시로 나오는 것이라
# 알림에 섞이면 배경소음이 되고, 그러면 킬스위치가 내려간 날에도 안 읽게 된다.
# ACCEPTED/UNKNOWN 은 일부러 뺐다 — 지정가는 접수만 되는 것이 정상이라 알리면 소음이 된다.
# 다만 체결이 반쪽만 났거나 확인 못 한 채 장부에 넣은 것은 사람이 알아야 한다.
STATUS_MARK = {"FILLED": "✅", "PARTIALLY_FILLED": "◐", "ASSUMED": "❓",
               "REJECTED": "🚫", "ERROR": "⚠️"}


def enabled() -> bool:
    return bool(C.TELEGRAM_TOKEN and C.TELEGRAM_CHAT_ID)


def _call(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        API.format(token=C.TELEGRAM_TOKEN, method=method),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read() or b"{}")


def send(text: str, silent: bool = False) -> bool:
    """한 줄 보낸다. 실패해도 예외를 올리지 않는다.

    알림이 안 갔다고 매매를 세우면 본말전도다 — 텔레그램은 랩의 관찰 장치이지
    랩의 일부가 아니다.
    """
    if not enabled():
        return False
    try:
        out = _call("sendMessage", {
            "chat_id": C.TELEGRAM_CHAT_ID, "text": text[:4000],
            "parse_mode": "HTML", "disable_web_page_preview": True,
            "disable_notification": silent})
        return bool(out.get("ok"))
    except Exception:
        return False


def _money(v) -> str:
    return "—" if v is None else f"{round(float(v)):,}"


def _brief(text, limit: int = 80) -> str:
    """첫 문장만, 길면 자른다. 한국어는 '~다.' 로 끝나므로 그것도 문장 끝으로 본다."""
    s = " ".join(str(text or "").split())
    head = re.split(r"(?<=다[.!?])\s|(?<=[.!?])\s", s, maxsplit=1)[0]
    return head if len(head) <= limit else head[:limit].rstrip() + "…"


def _decision_text(r: dict) -> str | None:
    """체결·거부·오류만 알린다. 판단만 하고 주문이 안 나간 것은 기록으로 충분하다."""
    status = r.get("status")
    mark = STATUS_MARK.get(status)
    if not mark:
        return None
    qty = r.get("filled_qty") if r.get("filled_qty") is not None else r.get("quantity")
    # 종목코드만 오면 폰에서 무엇을 샀는지 알 수 없다. data 를 함수 안에서 부르는 건
    # journal → telegram 경로와 얽히지 않게 하려는 것이다.
    sym = r.get("symbol") or ""
    try:
        from . import data
        name = data.NAMES.get(sym, "")
    except Exception:
        name = ""
    head = f"{r.get('action', '')} {sym}" + (f" {name}" if name else "")
    lines = [f"{mark} <b>{head}</b> {status}",
             f"{qty}주 @ {_money(r.get('price'))}"]
    if r.get("amount") is not None:
        lines[-1] += f" · {_money(r['amount'])}원"
    if r.get("realized_pnl") is not None:
        lines.append(f"실현 {_money(r['realized_pnl'])}원")
    if r.get("order_no"):
        lines.append(f"주문번호 {r['order_no']}")
    if r.get("forced"):
        lines.append(f"강제: {r['forced']}")
    # 거부는 진단 정보라 조금 길게, 매수 근거는 첫 문장만. 알림은 폰에서 훑는 것이지
    # 읽는 것이 아니다 — 자세한 근거는 대시보드와 daily_log 에 그대로 남는다.
    if r.get("reason_rejected"):
        lines.append(_brief(r["reason_rejected"], 140))
    elif r.get("reason"):
        lines.append(_brief(r["reason"], 80))
    return "\n".join(lines)


def on_record(stream: str, record: dict) -> None:
    """journal.jot 이 기록을 남긴 직후에 불린다. 기록이 먼저고 알림은 그 다음이다."""
    if not enabled():
        return
    try:
        if stream == "decisions":
            text = _decision_text(record)
        elif stream == "incidents":
            head = INCIDENT_KINDS.get(record.get("kind"))
            if not head:
                return
            detail = record.get("detail") or record.get("symbol") or ""
            extra = f"\n{detail}" if detail else ""
            if record.get("pnl_pct") is not None:
                extra += f"\n평가손익 {record['pnl_pct']}%"
            text = f"{head}{extra}"
        else:
            return
        if text:
            send(text)
    except Exception:
        pass       # 알림이 기록을 방해하면 안 된다


def verify() -> str:
    """토큰이 실제로 사는지 원장에 물어보고 봇 이름을 돌려준다. 죽었으면 예외다.

    있다는 것과 되는 것은 다르다 — 폐기·회수된 토큰도 .env 에는 그대로 남아 있고,
    사람 없이 도는 루프는 알림이 유일한 연락 수단이다. getUpdates 는 쓰지 않는다:
    telegramctl 이 그 엔드포인트로 long-polling 중이라 서로 방해한다.
    """
    if not C.TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 이 없다")
    req = urllib.request.Request(API.format(token=C.TELEGRAM_TOKEN, method="getMe"))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        out = json.loads(r.read() or b"{}")
    if not out.get("ok"):
        raise RuntimeError(f"토큰이 거절됐다: {str(out)[:120]}")
    return (out.get("result") or {}).get("username") or "?"


def chat_ids() -> list[dict]:
    """봇에게 말을 건 사람들의 chat_id. 토큰만 있으면 되고 CHAT_ID 는 없어도 된다."""
    if not C.TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 이 없다")
    req = urllib.request.Request(API.format(token=C.TELEGRAM_TOKEN, method="getUpdates"))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        out = json.loads(r.read() or b"{}")
    seen, rows = set(), []
    for u in out.get("result", []):
        chat = ((u.get("message") or u.get("channel_post") or {}).get("chat")) or {}
        if chat.get("id") and chat["id"] not in seen:
            seen.add(chat["id"])
            rows.append({"chat_id": chat["id"], "type": chat.get("type"),
                         "name": chat.get("title") or chat.get("first_name") or ""})
    return rows


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ai_trader.telegram", description="텔레그램 알림 점검")
    ap.add_argument("--chat-id", action="store_true", help="봇에게 말을 건 chat_id 찾기")
    ap.add_argument("--test", action="store_true", help="연결 확인용 한 줄 보내기")
    args = ap.parse_args(argv)

    if args.chat_id:
        rows = chat_ids()
        if not rows:
            print("[telegram] 아직 아무도 봇에게 말을 걸지 않았다 — 봇에게 메시지를 하나 보내라")
            return 1
        for row in rows:
            print(f"  chat_id={row['chat_id']}  {row['type']}  {row['name']}")
        return 0

    if args.test:
        if not enabled():
            print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 .env 에 없다")
            return 1
        ok = send("🧪 다크랩 연결 확인 — 이 메시지가 보이면 알림 경로가 살아 있다")
        print("[telegram] 보냄" if ok else "[telegram] 실패 — 토큰/chat_id 를 확인하라")
        return 0 if ok else 1

    print(f"[telegram] enabled={enabled()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
