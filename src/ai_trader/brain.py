"""판단 엔진. 선정·매수·매도·보유를 전부 여기서 LLM 이 정한다.

사람이 쓴 매매 규칙은 이 파일에 없다. 있는 것은 관측치를 어떻게 담아 보낼지와,
돌아온 판단을 어떻게 검증할지뿐이다. '어떻게 판단할지'는 lab/policy.md 안에 있고,
그 파일은 autoresearch 가 스스로 고쳐 쓴다.
"""
from __future__ import annotations

import json
import hashlib
import math
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import broker, config as C

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
                    "quantity": {"type": "number"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "action", "sleeve", "quantity", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
        "watchlist": {
            "type": "array", "maxItems": 8,
            "description": "이번 관측에서 직접 선정한 관심 후보. 주문 여부와 별개다.",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "conviction": {"type": "number", "minimum": 0, "maximum": 1},
                    "thesis": {"type": "string"},
                    "risk": {"type": "string"},
                },
                "required": ["symbol", "conviction", "thesis", "risk"],
                "additionalProperties": False,
            },
        },
        "lesson": {"type": "string", "description": "이번 사이클에서 배운 것 한 줄. 없으면 빈 문자열."},
    },
    "required": ["market_view", "decisions", "watchlist", "lesson"],
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

watchlist에는 관측 팩 중 다시 볼 가치가 있는 후보만 최대 8개 고르고, 확신도·선정 근거·
무효화 위험을 적어라. Radar는 이 목록만 보여준다. 퀀트 점수 순위를 그대로 복사하지 마라.

거래를 위한 거래는 하지 마라. 아무것도 할 이유가 없으면 decisions 를 비워도 된다.
reason 에는 결론이 아니라 근거를 써라 — 어떤 관측치와 어떤 뉴스가 그 판단을 만들었는지.

<policy>
{policy}
</policy>"""


class Decision(dict):
    pass


def _policy() -> str:
    return C.POLICY.read_text(encoding="utf-8") if C.POLICY.exists() else "(policy 파일 없음)"


def remember_lesson(lesson: str, brain_name: str) -> bool:
    """실제 모의 사이클에서 LLM이 배운 점을 다음 판단의 정책 기억에 남긴다."""
    lesson = " ".join(str(lesson or "").split()).strip()[:500]
    if not lesson or not is_ai(brain_name) or not C.POLICY.exists():
        return False
    text = C.POLICY.read_text(encoding="utf-8")
    heading, next_heading = "## 지금까지 배운 것", "## 폐기한 생각"
    start, end = text.find(heading), text.find(next_heading)
    if start < 0 or end <= start:
        return False
    block_start = start + len(heading)
    current = text[block_start:end].strip()
    if lesson in current:
        return False
    rows = [r for r in current.splitlines()
            if r.strip() and "_(비어 있음" not in r]
    rows.append(f"- [{datetime.now():%Y-%m-%d}] {lesson} ({brain_name})")
    rows = rows[-30:]
    updated = text[:block_start] + "\n\n" + "\n".join(rows) + "\n\n" + text[end:]
    C.POLICY.write_text(updated, encoding="utf-8")
    return True


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
            qty = broker.round_qty(sym, budget / broker.base_price(sym, price))
            if qty <= 0:
                continue
            action = "BUY"
        else:
            continue
        decisions.append({"symbol": sym, "action": action, "sleeve": sleeve, "quantity": qty,
                          "confidence": 0.0, "reason": "offline stub — 판단 아님, 배선 점검용"})
    return {"market_view": "offline stub", "decisions": decisions, "watchlist": [], "lesson": "",
            "brain": "offline-stub", "usage": {}}


# LLM 이 낸 판단이 아닌 brain 값들. 이 기록은 "과거 네 판단"으로 되먹이지 않는다.
NOT_AI = ("offline-stub", "quant-fallback", "guardrail", "error:", "refusal")


def is_ai(brain_name) -> bool:
    return not str(brain_name or "").startswith(NOT_AI)


def _quant_fallback(snapshot: dict, observations: list[dict]) -> dict:
    """ANTHROPIC_API_KEY 가 없을 때 실제로 매매하는 경로.

    이건 스텁이 아니다 — 이식해 온 퀀트 점수(quant.py)로 판단한다. LLM 키 없이도
    프로그램이 무작위가 아니라 규칙대로 돌게 하는 것이 목적이다.

    다만 이것은 **사람이 정한 임계값**이고, 이 랩의 전제(사람이 전략을 안 쓴다)와는 다르다.
    그래서 기록에 brain='quant-fallback' 이 찍히고, policy.md 학습 근거로는 쓰이지 않는다.
    ANTHROPIC_API_KEY 를 넣는 순간 판단 주체는 다시 LLM 하나가 된다.
    """
    buy_above = float(C.QUANT_BUY_ABOVE)
    sell_below = float(C.QUANT_SELL_BELOW)
    held = {p["symbol"]: p for p in snapshot["positions"]}
    scored = []
    for obs in observations:
        q = obs.get("quant") or {}
        if q.get("unavailable") or "score" not in q:
            continue
        scored.append((float(q["score"]), obs))
    scored.sort(key=lambda x: -x[0])

    decisions = []
    for score, obs in scored:
        sym = obs["symbol"]
        price = _last_price(obs)
        if sym in held:
            if score < sell_below:
                decisions.append({
                    "symbol": sym, "action": "SELL", "sleeve": held[sym]["sleeve"],
                    "quantity": held[sym]["qty"], "confidence": round((sell_below - score) / 100, 2),
                    "reason": f"퀀트 {score:.0f}점 < 매도선 {sell_below:.0f} · "
                              + " · ".join(q for q in (obs.get("quant") or {}).get("reasons", [])[:2])})
            continue
        if score < buy_above or price <= 0:
            continue
        # 변동성이 큰 종목을 공격 슬리브로. 슬리브 상한 안에서만 산다.
        vol = (obs.get("observed") or {}).get("daily_vol_pct") or 0
        sleeve = "AGGRESSIVE" if vol >= C.QUANT_AGGRESSIVE_VOL else "STABLE"
        room = snapshot["sleeves"][sleeve]
        budget = min(room["cash"], room["equity"] * C.MAX_POSITION_PCT * 0.9)
        # 수수료만큼 못 사면 주문이 거절된다. 예산에서 먼저 빼고 나눈다.
        qty = broker.round_qty(
            sym, budget / (broker.base_price(sym, price) * (1 + broker.FEE_RATE)))
        if qty <= 0:
            continue
        decisions.append({
            "symbol": sym, "action": "BUY", "sleeve": sleeve, "quantity": qty,
            "confidence": round((score - buy_above) / max(100 - buy_above, 1), 2),
            "reason": f"퀀트 {score:.0f}점 ≥ 매수선 {buy_above:.0f} · "
                      + " · ".join((obs.get("quant") or {}).get("reasons", [])[:2])})
        if len(decisions) >= C.MAX_ORDERS_PER_CYCLE:
            break

    top = ", ".join(f"{o['symbol']} {s:.0f}" for s, o in scored[:5])
    return {"market_view": f"퀀트 점수 상위: {top}" if top else "점수를 낼 수 있는 종목이 없다",
            "decisions": decisions, "watchlist": [], "lesson": "",
            "brain": "quant-fallback", "usage": {}}


CLI_SCHEMA_HINT = """

출력 형식: 아래 스키마의 JSON 객체 **하나만** 내라. 설명도 코드펜스도 붙이지 마라.
{"market_view": "...", "lesson": "...", "watchlist": [
  {"symbol": "005930", "conviction": 0.0, "thesis": "...", "risk": "..."}], "decisions": [
  {"symbol": "005930", "action": "BUY|SELL|HOLD", "sleeve": "STABLE|AGGRESSIVE",
   "quantity": 0.0, "confidence": 0.0, "reason": "..."}]}
quantity 는 해외 종목이면 소수점을 써도 된다. 판단할 게 없으면 decisions 를 빈 배열로 둬라."""

# CLI 가 파일을 읽거나 명령을 실행하면 안 된다. 판단만 받아온다.
CLI_DENY = "Bash Read Edit Write Glob Grep WebSearch WebFetch Task NotebookEdit"


def _first_json_object(text: str) -> dict:
    """CLI 응답에서 JSON 객체 하나를 끄집어낸다. 코드펜스와 앞뒤 설명을 견딘다."""
    s = str(text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start < 0:
        raise ValueError(f"JSON 이 없다: {s[:120]}")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(s[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("JSON 객체가 안 닫혔다")


def cli_complete(system: str, prompt: str, model: str | None = None) -> tuple[str, dict]:
    """이 PC 의 Claude Code CLI 에서 도구 없는 단일 응답을 받는다.

    print 모드는 호출마다 Claude Code 시스템 프롬프트를 싣는다 — 측정값으로 호출당
    약 2만 토큰이다. 그래서 백테스트처럼 호출이 많은 경로에는 쓰지 마라.
    """
    cmd = [C.cli_path(C.BRAIN_CLI), "-p", "--output-format", "json", "--model", model or C.BRAIN_MODEL,
           "--system-prompt", system, "--disallowedTools", CLI_DENY,
           "--permission-mode", "dontAsk",
           "--disable-slash-commands", "--no-session-persistence", "--max-turns", "1"]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=C.BRAIN_CLI_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"claude cli exit {proc.returncode}: {proc.stderr[:200]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude cli error: {str(envelope.get('result'))[:200]}")
    usage = envelope.get("usage") or {}
    meta = {"input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
            "cost_usd": envelope.get("total_cost_usd")}
    return str(envelope.get("result") or ""), meta


def codex_complete(system: str, prompt: str, schema: dict | None = None) -> tuple[str, dict]:
    """로그인된 Codex CLI 구독으로 도구 없는 단일 응답을 받는다."""
    with tempfile.TemporaryDirectory(prefix="ai-trader-codex-") as tmp:
        root = Path(tmp)
        output = root / "last.txt"
        cmd = [C.cli_path(C.CODEX_CLI), "exec", "--sandbox", "read-only", "--skip-git-repo-check",
               "--ephemeral", "--ignore-rules", "--json", "-C", tmp,
               "--output-last-message", str(output)]
        if C.CODEX_MODEL:
            cmd += ["--model", C.CODEX_MODEL]
        if schema is not None:
            schema_path = root / "schema.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            cmd += ["--output-schema", str(schema_path)]
        cmd.append("-")
        full_prompt = f"# 지시\n{system}\n\n# 입력\n{prompt}"
        proc = subprocess.run(cmd, input=full_prompt, capture_output=True, text=True,
                              timeout=C.BRAIN_CLI_TIMEOUT)
        if proc.returncode != 0:
            raise RuntimeError(f"codex cli exit {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
        if not output.exists():
            raise RuntimeError("codex cli가 최종 응답 파일을 만들지 않았다")
        usage = {}
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
        return output.read_text(encoding="utf-8"), {
            "input": usage.get("input_tokens"), "output": usage.get("output_tokens")}


# Claude 가 한도로 막힌 동안만 Codex 를 쓴다. 0 이면 평소처럼 Claude 부터.
# 한 번 막혔다고 영구히 Codex 에 남으면, 한도가 풀려도 돌아오지 못한다.
_CLAUDE_QUOTA_UNTIL = 0.0


def local_complete(system: str, prompt: str, schema: dict | None = None,
                   claude_model: str | None = None) -> tuple[str, dict, str]:
    """로컬 구독 CLI를 고른다. auto에서 Claude 실패 시 Codex로 이어간다."""
    global _CLAUDE_QUOTA_UNTIL
    backend = C.brain_backend()
    if backend == "codex":
        text, usage = codex_complete(system, prompt, schema)
        return text, usage, f"{C.CODEX_MODEL or 'codex'}(cli)"
    if backend != "cli":
        raise RuntimeError(f"로컬 CLI 백엔드가 아니다: {backend}")
    if time.time() >= _CLAUDE_QUOTA_UNTIL:
        try:
            text, usage = cli_complete(system, prompt, model=claude_model)
            _CLAUDE_QUOTA_UNTIL = 0.0   # 돌아왔다
            return text, usage, f"{claude_model or C.BRAIN_MODEL}(cli)"
        except Exception as exc:
            msg = str(exc).lower()
            if any(word in msg for word in ("limit", "quota", "credit", "billing", "rate")):
                _CLAUDE_QUOTA_UNTIL = time.time() + C.BRAIN_QUOTA_COOLDOWN
            if C.BRAIN_BACKEND.lower() != "auto" or not C.have_codex():
                raise
    text, usage = codex_complete(system, prompt, schema)
    return text, usage, f"{C.CODEX_MODEL or 'codex'}(cli)"


def _local_decide(system: str, prompt: str) -> dict:
    """Claude/Codex CLI 응답을 매매 판단 객체로 바꾼다."""
    result, usage, name = local_complete(system + CLI_SCHEMA_HINT, prompt,
                                         schema=DECISION_SCHEMA)
    out = _first_json_object(result)
    if not isinstance(out.get("market_view"), str) or not isinstance(out.get("lesson"), str):
        raise ValueError("CLI 판단에 market_view/lesson 문자열이 없다")
    if not isinstance(out.get("decisions"), list) or not isinstance(out.get("watchlist"), list):
        raise ValueError("CLI 판단의 decisions/watchlist 가 배열이 아니다")
    out["brain"] = name
    out["usage"] = usage
    return out


# 정책을 읽지 않고 나온 판단들. 퀀트 대역은 임계값으로, 스텁은 배선 점검으로 돌고,
# 오류·거부는 판단이 없는 것이다 — 어느 것도 policy_text 를 보지 않는다.
POLICY_BLIND = ("quant-fallback", "offline-stub", "error:", "refusal")


def decide(snapshot: dict, observations: list[dict], history: list[dict] | None = None,
           policy_text: str | None = None, allow_cli: bool = True) -> dict:
    """한 사이클의 판단. 정책을 실제로 읽었는지 policy_used 에 같이 담아 돌려준다.

    이 표시가 없으면 정책 평가가 거짓말을 한다 — allow_cli=False 인 과거 재생은
    CLI 백엔드를 퀀트 대역으로 바꾸므로, 상반된 정책을 넣어도 같은 주문이 나온다.
    그 결과로 KEEP/REVERT 를 정하면 읽지도 않은 글을 채점하는 것이 된다.
    """
    out = _decide(snapshot, observations, history, policy_text, allow_cli)
    out["policy_used"] = not str(out.get("brain", "")).startswith(POLICY_BLIND)
    return out


def _decide(snapshot: dict, observations: list[dict], history: list[dict] | None = None,
            policy_text: str | None = None, allow_cli: bool = True) -> dict:
    """실패해도 예외를 던지지 않는다 — 랩은 멈추지 않는다."""
    backend = C.brain_backend()
    if backend in ("cli", "codex") and not allow_cli:
        # 수십~수천 번 도는 과거 재생은 CLI 대신 같은 관측 팩의 퀀트 점수를 쓴다.
        backend = "none"
    if backend == "none":
        # 관측 팩에 퀀트 점수가 있으면 그걸로 판단한다. 없으면(백테스트 배선 점검 등) 스텁.
        if any((o.get("quant") or {}).get("score") is not None for o in observations):
            return _quant_fallback(snapshot, observations)
        return _offline(snapshot, observations)

    if backend in ("cli", "codex"):
        system = SYSTEM.format(policy=policy_text if policy_text is not None else _policy())
        try:
            return _local_decide(system, _prompt(snapshot, observations, history or []))
        except Exception as exc:
            # 판단을 못 받았으면 아무것도 하지 않는다. 추측 매매 금지.
            return {"market_view": "", "decisions": [], "watchlist": [], "lesson": "",
                    "brain": f"error:{type(exc).__name__}", "error": str(exc)[:300], "usage": {}}
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
        return {"market_view": "", "decisions": [], "watchlist": [], "lesson": "",
                "brain": f"error:{type(exc).__name__}", "error": str(exc)[:300], "usage": {}}

    if resp.stop_reason == "refusal":
        # 거부당한 판단을 다른 모델로 우회해 돈을 굴리지 않는다. 그냥 아무것도 안 한다.
        return {"market_view": "", "decisions": [], "watchlist": [], "lesson": "",
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
        # 해외는 소수점 주식이 있다. 시장이 못 받는 자리는 broker 가 잘라낸다.
        try:
            qty = broker.round_qty(sym, d.get("quantity") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(qty):
            continue
        if qty <= 0:
            continue
        sleeve = held[sym]["sleeve"] if sym in held else str(d.get("sleeve", "")).upper()
        if sleeve not in C.SLEEVES:
            continue
        clean.append({**d, "symbol": sym, "action": action, "sleeve": sleeve, "quantity": qty})
        if len(clean) >= C.MAX_ORDERS_PER_CYCLE:
            break
    return clean
