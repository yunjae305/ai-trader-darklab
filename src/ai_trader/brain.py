"""판단 엔진. 선정·매수·매도·보유를 전부 여기서 LLM 이 정한다.

사람이 쓴 매매 규칙은 이 파일에 없다. 있는 것은 관측치를 어떻게 담아 보낼지와,
돌아온 판단을 어떻게 검증할지뿐이다. '어떻게 판단할지'는 lab/policy.md 안에 있고,
그 파일은 autoresearch 가 스스로 고쳐 쓴다.
"""
from __future__ import annotations

import json
import hashlib

from . import config as C

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "market_view": {"type": "string", "description": "오늘 시장을 어떻게 보는지 2~3문장"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                    "sleeve": {"type": "string", "enum": ["STABLE", "AGGRESSIVE"]},
                    "quantity": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "action", "sleeve", "quantity", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
        "lesson": {"type": "string", "description": "이번 사이클에서 배운 것 한 줄. 없으면 빈 문자열."},
    },
    "required": ["market_view", "decisions", "lesson"],
    "additionalProperties": False,
}

SYSTEM = """너는 무인 트레이딩 다크랩의 유일한 트레이더다. 사람은 네 판단에 개입하지 않는다.

아래 <policy> 가 지금까지 네가 만들어 온 생각이다. 처음에는 거의 비어 있다 —
사람이 전략을 써주지 않았기 때문이다. 네가 관측하고, 판단하고, 결과를 보고 채워 나간다.

매 사이클마다 관측 팩과 계좌 현황, 그리고 과거 네 판단이 어떻게 끝났는지를 받는다.
각 종목에 대해 BUY / SELL / HOLD 중 하나를 고른다:
- BUY   : 신규 매수 또는 추가 매수. sleeve 를 직접 정한다. quantity 는 주식 수.
- SELL  : 보유 종목의 일부 또는 전량 매도. quantity 는 팔 주식 수.
- HOLD  : 그대로 둔다. quantity 는 0.
판 종목을 나중에 다시 사도 되고, 오늘 산 종목을 내일 팔아도 된다. 순서에 제약은 없다.
관심 없는 종목은 굳이 HOLD 로 나열하지 않아도 된다.

거래를 위한 거래는 하지 마라. 아무것도 할 이유가 없으면 decisions 를 비워도 된다.
reason 에는 결론이 아니라 근거를 써라 — 어떤 관측치와 어떤 뉴스가 그 판단을 만들었는지.

<policy>
{policy}
</policy>"""


class Decision(dict):
    pass


def _policy() -> str:
    return C.POLICY.read_text(encoding="utf-8") if C.POLICY.exists() else "(policy 파일 없음)"


def _prompt(snapshot: dict, observations: list[dict], history: list[dict]) -> str:
    parts = [
        "# 계좌 현황",
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        "",
        "# 관측 팩 (원자료 · 서술 통계 · 기술지표 · 퀀트 점수 · 실시간 뉴스 헤드라인)",
        "# indicators 와 quant 는 측정값이지 매수 신호가 아니다. 믿을지 말지도 네가 정한다.",
        json.dumps(observations, ensure_ascii=False),
        "",
    ]
    if history:
        parts += [
            "# 과거 네 판단과 그 뒤 벌어진 일",
            json.dumps(history, ensure_ascii=False, indent=2),
            "",
        ]
    parts.append("지금 무엇을 할지 정해라.")
    return "\n".join(parts)


def _last_price(obs: dict) -> float:
    """관측 팩은 단일 TF({observed})와 멀티 TF({timeframes})의 두 모양이 있다."""
    if "observed" in obs:
        return float(obs["observed"].get("last") or 0)
    for tf in ("15m", "1h", "4h", "1d"):
        v = obs.get("timeframes", {}).get(tf, {})
        if v.get("status") == "ok" and v.get("observed", {}).get("last"):
            return float(v["observed"]["last"])
    return 0.0


def _offline(snapshot: dict, observations: list[dict]) -> dict:
    """ANTHROPIC_API_KEY 가 없을 때의 배선 점검용 대역.

    이것은 AI 판단이 아니다. 결정론적 해시로 움직이며, 모든 기록에
    brain='offline-stub' 이 찍혀 진짜 판단과 절대 섞이지 않는다.
    """
    decisions = []
    held = {p["symbol"]: p for p in snapshot["positions"]}
    for obs in observations:
        sym = obs["symbol"]
        h = int(hashlib.sha256(f"{sym}{snapshot['day_start_equity']}".encode()).hexdigest(), 16)
        price = _last_price(obs)
        if sym in held:
            action, sleeve, qty = ("SELL", held[sym]["sleeve"], held[sym]["qty"]) if h % 3 == 0 \
                else ("HOLD", held[sym]["sleeve"], 0)
        elif h % 5 == 0 and price > 0:
            sleeve = "STABLE" if h % 2 == 0 else "AGGRESSIVE"
            budget = snapshot["sleeves"][sleeve]["equity"] * C.MAX_POSITION_PCT * 0.8
            qty = int(budget // price)
            if qty <= 0:
                continue
            action = "BUY"
        else:
            continue
        decisions.append({"symbol": sym, "action": action, "sleeve": sleeve, "quantity": qty,
                          "confidence": 0.0, "reason": "offline stub — 판단 아님, 배선 점검용"})
    return {"market_view": "offline stub", "decisions": decisions, "lesson": "",
            "brain": "offline-stub", "usage": {}}


def decide(snapshot: dict, observations: list[dict], history: list[dict] | None = None,
           policy_text: str | None = None) -> dict:
    """한 사이클의 판단을 돌려준다. 실패해도 예외를 던지지 않는다 — 랩은 멈추지 않는다."""
    if not C.have_brain_key():
        return _offline(snapshot, observations)
    try:
        import anthropic
    except ImportError:
        return {**_offline(snapshot, observations), "brain": "offline-stub(anthropic 미설치)"}

    client = anthropic.Anthropic()
    system = SYSTEM.format(policy=policy_text if policy_text is not None else _policy())
    try:
        resp = client.messages.create(
            model=C.BRAIN_MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": _prompt(snapshot, observations, history or [])}],
            output_config={"effort": "high",
                           "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        )
    except Exception as exc:
        # 네트워크·레이트리밋으로 판단을 못 했으면 아무것도 하지 않는다. 추측 매매 금지.
        return {"market_view": "", "decisions": [], "lesson": "",
                "brain": f"error:{type(exc).__name__}", "error": str(exc)[:300], "usage": {}}

    if resp.stop_reason == "refusal":
        # 거부당한 판단을 다른 모델로 우회해 돈을 굴리지 않는다. 그냥 아무것도 안 한다.
        return {"market_view": "", "decisions": [], "lesson": "",
                "brain": "refusal", "usage": {}}

    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    out = json.loads(text)
    out["brain"] = C.BRAIN_MODEL
    out["usage"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    return out


def validate(decisions: list[dict], snapshot: dict, prices: dict[str, float],
             universe: list[str] | None = None) -> list[dict]:
    """LLM 이 낸 판단을 집행 가능한 형태로만 걸러낸다. 판단 내용은 고치지 않는다."""
    allowed = set(universe if universe is not None else C.UNIVERSE)
    held = {p["symbol"]: p for p in snapshot["positions"]}
    clean, seen = [], set()
    for d in decisions:
        sym = str(d.get("symbol", "")).strip()
        action = str(d.get("action", "")).upper()
        if sym in seen or sym not in allowed or action not in {"BUY", "SELL", "HOLD"}:
            continue
        seen.add(sym)
        if action == "HOLD":
            continue
        if action == "SELL" and sym not in held:
            continue
        if prices.get(sym, 0) <= 0:
            continue
        qty = int(d.get("quantity") or 0)
        if qty <= 0:
            continue
        sleeve = held[sym]["sleeve"] if sym in held else str(d.get("sleeve", "")).upper()
        if sleeve not in C.SLEEVES:
            continue
        clean.append({**d, "symbol": sym, "action": action, "sleeve": sleeve, "quantity": qty})
        if len(clean) >= C.MAX_ORDERS_PER_CYCLE:
            break
    return clean
