"""자기개선 루프 (karpathy/autoresearch 방식).

고칠 수 있는 파일은 lab/policy.md 하나. 실험 예산은 백테스트 1회. 지표는 risk_adjusted 하나.
좋아지면 남기고, 나빠지면 되돌린다. 그게 전부다 — 그리고 그게 학습이다.

사람이 전략을 써주지 않기 때문에, policy.md 는 여기서만 자란다.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime

from . import backtest, brain, config as C, journal

GUARD_HEADING = "## 지켜야 하는 경계"

PROPOSE_SYSTEM = """너는 무인 트레이딩 다크랩의 연구원이다. 트레이더의 생각이 담긴 policy.md 를
한 번 고쳐 쓰는 것이 네 일이다.

규칙:
1. policy.md **전체**를 다시 써서 내라. 일부 발췌나 diff 가 아니다.
2. "{guard}" 섹션은 글자 하나 바꾸지 마라. 사람이 정한 돈의 경계다.
3. 한 번에 하나의 가설만 바꿔라. 뭘 바꿨는지 알 수 없으면 되돌릴 수도 없다.
4. 근거는 아래 제공되는 실제 운용 기록에서만 끌어와라. 일반론적인 투자 격언을 적지 마라.
5. 이미 시도했다가 되돌려진 가설을 다시 제안하지 마라.
6. "지금까지 배운 것"에는 관측된 사실과 그로부터 나온 판단 지침을 적고,
   "폐기한 생각"에는 되돌려진 가설과 왜 실패했는지를 적어 남겨라."""


def _strip_fence(text: str) -> str:
    m = re.search(r"```(?:markdown|md)?\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def _guard_intact(candidate: str, current: str) -> bool:
    def block(t: str) -> str:
        i = t.find(GUARD_HEADING)
        if i < 0:
            return ""
        j = t.find("\n## ", i + 1)
        return t[i:j if j > 0 else len(t)].strip()
    return bool(block(current)) and block(candidate) == block(current)


def propose(current: str, evidence: dict) -> tuple[str, str]:
    """(새 policy 전문, 무엇을 왜 바꿨는지) 를 돌려준다."""
    backend = C.brain_backend()
    if backend == "none":
        stub = current.replace(
            "_(비어 있음 — 한 달 운용 뒤 autoresearch 가 채운다)_",
            f"- [offline-stub {datetime.now():%Y-%m-%d %H:%M}] LLM이 없어 실제 제안이 아니다. "
            "배선 점검용 변경.")
        return stub, "offline-stub: LLM 없음 — 기계만 돌린 것"
    prompt = (
        "# 현재 policy.md\n\n```markdown\n" + current + "\n```\n\n"
        "# 실제 운용 기록 (근거는 여기서만 끌어와라)\n\n```json\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)[:60000] + "\n```\n\n"
        "policy.md 전문을 다시 써라. 마지막 줄에 `CHANGE: <무엇을 왜 바꿨는지 한 줄>` 을 덧붙여라."
    )
    system = PROPOSE_SYSTEM.format(guard=GUARD_HEADING)
    if backend in ("cli", "codex"):
        text, _, _ = brain.local_complete(system, prompt, claude_model=C.RESEARCH_MODEL)
    else:
        try:
            import anthropic
        except ImportError:
            return current, "anthropic 미설치 — 제안 없음"
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=C.RESEARCH_MODEL, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "high"},
        )
        if resp.stop_reason == "refusal":
            return current, "refusal — 제안 없음"
        text = "".join(b.text for b in resp.content if b.type == "text")
    note = ""
    m = re.search(r"^CHANGE:\s*(.+)$", text, re.M)
    if m:
        note = m.group(1).strip()
        text = text[:m.start()] + text[m.end():]
    return _strip_fence(text), note or "(변경 설명 없음)"


def _real(rows: list[dict]) -> list[dict]:
    """LLM 이 내지 않은 기록은 근거가 아니다 — 정책을 고칠 때 쓰지 않는다.

    스텁뿐 아니라 퀀트 대역(quant-fallback)과 손절 가드레일도 제외한다.
    policy.md 는 LLM 의 생각이고, 사람이 정한 임계값의 결과로 그것을 고치면 앞뒤가 안 맞는다.
    """
    return [r for r in rows if brain.is_ai(r.get("brain"))]


def evidence_pack(limit: int = 40) -> dict:
    return {
        "cycles": _real(journal.read("cycles", limit=limit * 2))[-limit:],
        "decisions": _real(journal.read("decisions", limit=limit * 4))[-limit * 2:],
        "past_backtests": journal.read("backtests", limit=10),
        "reverted_hypotheses": [
            {"change": e.get("change"), "score": e.get("score"), "why": e.get("verdict_reason")}
            for e in journal.read("experiments", limit=30) if e.get("verdict") == "REVERT"
        ],
        "incidents": journal.read("incidents", limit=10),
    }


def run(iters: int = 3, days: int = 30, metric: str = "risk_adjusted",
        source: str = "history") -> list[dict]:
    """기본 소스가 history 인 이유: 랜덤워크에 최적화된 정책은 학습이 아니라 과적합이다."""
    C.POLICY.parent.mkdir(parents=True, exist_ok=True)
    history_dir = C.LAB / "policy_history"
    history_dir.mkdir(exist_ok=True)

    current = C.POLICY.read_text(encoding="utf-8")
    with journal.mlflow_run("autoresearch-baseline",
                            params={"days": days, "metric": metric, "source": source}) as mlf:
        base = backtest.run(days=days, policy_text=current, quiet=True, mlf=mlf, source=source)
    best = base[metric]
    baseline_valid = bool(base.get("policy_used"))
    print(f"[autoresearch] baseline {metric}={best} (수익 {base['total_return_pct']}%)"
          + ("" if baseline_valid else "  — 경고: 기준선이 정책을 읽지 않았다, 승격 금지"))
    journal.jot("experiments", {"iter": -1, "verdict": "BASELINE", "score": best,
                                "metrics": {k: base[k] for k in
                                            ("total_return_pct", "max_drawdown_pct", "trades")}})

    log = []
    for i in range(iters):
        candidate, change = propose(current, evidence_pack())
        rec = {"iter": i, "change": change, "metric": metric, "baseline": best, "source": source}

        if candidate.strip() == current.strip():
            rec.update(verdict="REVERT", score=None, verdict_reason="제안이 현재와 동일")
        elif not _guard_intact(candidate, current):
            rec.update(verdict="REVERT", score=None,
                       verdict_reason="사람이 정한 경계 섹션을 건드림 — 자동 기각")
        else:
            with journal.mlflow_run(f"autoresearch-iter-{i}", params={
                    "days": days, "metric": metric, "source": source, "change": change},
                    tags={"phase": "candidate"}) as mlf:
                trial = backtest.run(days=days, policy_text=candidate, quiet=True, mlf=mlf, source=source)
            rec["score"] = trial[metric]
            rec["metrics"] = {k: trial[k] for k in
                              ("total_return_pct", "max_drawdown_pct", "daily_vol_pct", "trades")}
            if not trial.get("policy_used"):
                # 재생이 정책을 읽지 않았다. 점수가 올랐든 내렸든 이 후보에 대한 평가가
                # 아니다 — REVERT 라고 적으면 '제안이 나빴다'는 거짓이 기록된다.
                rec.update(verdict="NOT_EVALUATED", score=None,
                           verdict_reason=f"재생이 정책을 읽지 않았다 (brain={trial.get('brain')}) "
                                          "— 정책을 쓰는 백엔드로 평가해야 판정할 수 있다")
            elif baseline_valid and trial[metric] > best:
                shutil.copy(C.POLICY, history_dir / f"policy_{datetime.now():%Y%m%d_%H%M%S}_v{i}.md")
                C.POLICY.write_text(candidate, encoding="utf-8")
                current, best = candidate, trial[metric]
                rec.update(verdict="KEEP",
                           verdict_reason=f"{metric} {rec['baseline']} → {trial[metric]} 개선")
            else:
                rec.update(verdict="REVERT",
                           verdict_reason=f"{metric} {rec['baseline']} → {trial[metric]} 미개선")

        journal.jot("experiments", rec)
        log.append(rec)
        print(f"[autoresearch] iter {i}: {rec['verdict']} — {rec['verdict_reason']}")

    journal.note("autoresearch", f"{iters}회 자기개선 루프 (지표 {metric})",
                 "| # | 판정 | 점수 | 바꾼 것 | 사유 |\n|---|---|---|---|---|\n" +
                 "\n".join(
                     f"| {r['iter']} | {r['verdict']} | {r.get('score')} | "
                     f"{str(r.get('change',''))[:80]} | {r.get('verdict_reason','')} |"
                     for r in log))
    return log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_trader.autoresearch", description="정책 자기개선 루프")
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--metric", default="risk_adjusted",
                    choices=["risk_adjusted", "total_return_pct"])
    ap.add_argument("--source", default="history", choices=["history", "synthetic", "toss"],
                    help="history=내려받은 실제 일봉(기본). synthetic 은 배선 점검용이다")
    args = ap.parse_args(argv)
    run(iters=args.iters, days=args.days, metric=args.metric, source=args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
