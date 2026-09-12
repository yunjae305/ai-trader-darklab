"""연구 기록. 다크랩은 사람이 보지 않는 동안 돌기 때문에, 기록이 유일한 증인이다.

jsonl  — 기계가 다시 읽는 원장 (research/*.jsonl)
md     — 사람이 아침에 읽는 보고서 (research/*.md)
mlflow — 실험 지표 추적 (없으면 조용히 건너뛴다)
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime

from . import config as C


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def jot(stream: str, record: dict) -> dict:
    """research/<stream>.jsonl 에 한 줄 추가하고, 기록된 레코드를 돌려준다."""
    C.RESEARCH.mkdir(parents=True, exist_ok=True)
    record = {"ts": _ts(), **record}
    with (C.RESEARCH / f"{stream}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read(stream: str, limit: int | None = None) -> list[dict]:
    path = C.RESEARCH / f"{stream}.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return rows[-limit:] if limit else rows


def note(name: str, title: str, body: str) -> None:
    """research/<name>.md 에 제목이 붙은 섹션을 덧붙인다."""
    C.RESEARCH.mkdir(parents=True, exist_ok=True)
    path = C.RESEARCH / f"{name}.md"
    head = "" if path.exists() else f"# {name}\n\n_다크랩 자동 생성. 사람이 편집하지 않는다._\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{head}\n## {title} — {_ts()}\n\n{body.rstrip()}\n")


def cycle_report(snapshot: dict, decisions: list[dict], results: list[dict]) -> str:
    lines = [
        f"- 모드: `{snapshot['mode']}` / 정지: `{snapshot['halted']}`",
        f"- 총자산: **{snapshot['equity']:,}원** (당일 {snapshot['day_return_pct']:+.2f}%)",
    ]
    for name, s in snapshot["sleeves"].items():
        lines.append(
            f"- {name} (목표 {s['target_weight']:.0%}): 평가 {s['equity']:,}원, "
            f"현금 {s['cash']:,}원, 실현 {s['realized_pnl']:+,}원")
    lines.append("")
    lines.append("| 종목 | 판단 | 슬리브 | 수량 | 확신 | 근거 | 결과 |")
    lines.append("|---|---|---|---|---|---|---|")
    by_symbol = {r.get("symbol"): r for r in results}
    for d in decisions:
        r = by_symbol.get(d.get("symbol"), {})
        outcome = r.get("status", "-")
        if r.get("reason_rejected"):
            outcome += f" ({r['reason_rejected']})"
        lines.append(
            f"| {d.get('symbol','-')} | {d.get('action','-')} | {d.get('sleeve','-')} | "
            f"{d.get('quantity','-')} | {d.get('confidence','-')} | "
            f"{str(d.get('reason','')).replace('|', '/')[:90]} | {outcome} |")
    return "\n".join(lines)


@contextlib.contextmanager
def mlflow_run(run_name: str, params: dict | None = None, tags: dict | None = None):
    """mlflow 가 있으면 추적하고, 없으면 아무 일도 없던 것처럼 통과시킨다."""
    try:
        import mlflow
    except ImportError:
        yield None
        return
    mlflow.set_tracking_uri(C.MLFLOW_URI)
    mlflow.set_experiment(C.MLFLOW_EXPERIMENT)
    with mlflow.start_run(run_name=run_name):
        if params:
            mlflow.log_params({k: str(v)[:250] for k, v in params.items()})
        if tags:
            mlflow.set_tags(tags)
        yield mlflow


def log_metrics(mlf, metrics: dict, step: int | None = None) -> None:
    if mlf is None:
        return
    clean = {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))}
    if clean:
        mlf.log_metrics(clean, step=step)
