"""매매 루프 프로세스 관리 — 대시보드의 시작/정지 버튼이 쓰는 층.

여기서 주문을 내지 않는다. `python -m ai_trader.loop` 를 띄우고 세울 뿐이고,
무엇을 사고 팔지는 그 루프가, 실주문이 나가는지는 AI_TRADER_LIVE 가 정한다.

**버튼은 AI_TRADER_LIVE 를 바꾸지 못한다.** 실계좌로 넘어가는 결정은 .env 에서
사람이 내리는 것이고, 폰 화면의 토글이 대신할 수 있는 일이 아니다.

프로세스는 하나만 돈다. 두 개가 같은 장부(lab/paper_state.json)를 동시에 쓰면
서로의 체결을 덮어쓴다.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

from . import config as C, journal

STATE = C.LAB / "engine.json"
LOG = C.LAB / "engine.log"


def _read() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _argv() -> tuple[list[str], str]:
    """어떤 모드로 띄울지. 사람이 .env 로 정해 둔 것을 그대로 따른다."""
    args = [sys.executable, "-m", "ai_trader.loop"]
    if C.LIVE_TRADING and (C.have_broker_keys() or C.have_kiwoom_keys()):
        args += ["--live", "--live-data"]
        mode = "실계좌 (국내 키움 / 해외 토스)"
    elif C.have_broker_keys():
        args += ["--paper", "--live-data"]
        mode = "모의 — 토스 실시세 + 자체 장부"
    else:
        args += ["--paper"]
        mode = "모의 — 합성 시장 (증권사 키 없음)"
    return args, mode


def _env() -> dict:
    env = dict(os.environ)
    src = str(C.ROOT / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def status() -> dict:
    saved = _read()
    pid = saved.get("pid")
    running = _alive(pid)
    if saved and not running:
        STATE.unlink(missing_ok=True)  # 죽은 프로세스 기록은 지운다
    _, mode = _argv()

    cycles = journal.read("cycles", limit=1)
    last_ts = cycles[-1].get("ts") if cycles else None
    next_scan = None
    if last_ts:
        try:
            next_scan = (datetime.fromisoformat(last_ts)
                         + timedelta(seconds=C.CYCLE_SECONDS)).isoformat(timespec="seconds")
        except Exception:
            next_scan = None
    return {
        "running": running,
        "pid": pid if running else None,
        "started_at": saved.get("started_at") if running else None,
        "argv": " ".join(saved.get("argv", [])) if running else None,
        "mode": saved.get("mode") if running else mode,
        "would_start_as": mode,
        "live_trading": C.LIVE_TRADING,
        "cycle_seconds": C.CYCLE_SECONDS,
        "last_cycle_at": last_ts,
        "next_scan_at": next_scan,
        "log": LOG.name,
    }


def start() -> dict:
    if _alive(_read().get("pid")):
        return {"ok": False, "reason": "이미 돌고 있다", **status()}
    args, mode = _argv()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    fh = LOG.open("a", encoding="utf-8")
    fh.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: {' '.join(args)} ({mode}) ===\n")
    fh.flush()
    proc = subprocess.Popen(args, cwd=str(C.ROOT), env=_env(), stdout=fh, stderr=fh,
                            start_new_session=True)
    saved = {"pid": proc.pid, "started_at": datetime.now().isoformat(timespec="seconds"),
             "argv": args, "mode": mode}
    STATE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    journal.jot("incidents", {"kind": "engine_start", "pid": proc.pid, "mode": mode})

    time.sleep(0.6)  # 곧바로 죽는 경우(import 오류 등)를 여기서 잡는다
    if not _alive(proc.pid):
        STATE.unlink(missing_ok=True)
        tail = LOG.read_text(encoding="utf-8", errors="replace")[-600:]
        return {"ok": False, "reason": f"시작 직후 종료됐다:\n{tail}", **status()}
    return {"ok": True, **status()}


def stop() -> dict:
    saved = _read()
    pid = saved.get("pid")
    if not _alive(pid):
        STATE.unlink(missing_ok=True)
        return {"ok": False, "reason": "돌고 있지 않다", **status()}
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", **status()}
    for _ in range(20):  # 사이클 중이면 끝날 시간을 준다
        if not _alive(pid):
            break
        time.sleep(0.25)
    STATE.unlink(missing_ok=True)
    journal.jot("incidents", {"kind": "engine_stop", "pid": pid})
    return {"ok": True, **status()}


def run_once() -> dict:
    """지금 한 사이클만. 연속 운용 중이면 장부가 겹치므로 거절한다."""
    if _alive(_read().get("pid")):
        return {"ok": False, "reason": "연속 운용 중이다 — 먼저 정지하라", **status()}
    args, mode = _argv()
    args = args + ["--once"]
    proc = subprocess.run(args, cwd=str(C.ROOT), env=_env(),
                          capture_output=True, text=True, timeout=600)
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
    return {"ok": proc.returncode == 0, "mode": mode,
            "output": "\n".join(tail), **status()}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ai_trader.engine", description="매매 루프 프로세스 관리")
    ap.add_argument("action", choices=["start", "stop", "status", "once"])
    args = ap.parse_args(argv)
    out = {"start": start, "stop": stop, "status": status, "once": run_once}[args.action]()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
