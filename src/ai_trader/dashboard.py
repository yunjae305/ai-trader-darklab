"""개인용 대시보드 — stdlib 만 쓴다. 빌드 도구도 프레임워크도 없다.

    python3 -m ai_trader.dashboard          127.0.0.1:8765 (이 PC 만)
    python3 -m ai_trader.dashboard --lan    0.0.0.0:8765   (같은 와이파이의 폰에서)

조회 전용이다. 여기서 주문을 내지 않는다 — 판단은 루프가 하고, 이 화면은 그 기록을 읽는다.
계좌 잔고가 보이므로 토큰을 요구한다. AI_TRADER_DASH_TOKEN 이 없으면 실행할 때 만들어 띄운다.

데이터가 없는 항목은 빈 값이 아니라 이유와 함께 내려간다. 화면이 "0원"이라고 적는 것과
"못 읽었다"고 적는 것은 전혀 다른 말이다.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (benchmark, brain, broker as bk, config as C, correlation,
               data, journal)

WEB = Path(__file__).resolve().parent / "web"
TOKEN = os.getenv("AI_TRADER_DASH_TOKEN") or secrets.token_urlsafe(9)

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def cached(key: str, ttl: float, build):
    """같은 화면을 여러 번 새로고침해도 시세를 매번 다시 받지 않게 한다."""
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    value = build()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


# ---------------------------------------------------------------- 데이터 조립

def _feed():
    return cached("feed", 3600, lambda: data.make_feed(paper=True, live_data=True))


def lab_state() -> dict:
    """계좌 현황. 자체 장부(lab/paper_state.json)가 원본이다."""
    broker = bk.PaperBroker.load()
    feed = _feed()
    try:
        prices = feed.prices(C.UNIVERSE)
    except Exception as exc:
        prices = {}
        journal.jot("incidents", {"kind": "dashboard_price_failed", "detail": str(exc)[:200]})
    snap = broker.snapshot(prices)
    snap["source"] = feed.source
    snap["market_open"] = bool(getattr(feed, "is_open", lambda: True)())
    return snap


def guardrails() -> dict:
    return {
        "sleeves": C.SLEEVES,
        "max_position_pct": C.MAX_POSITION_PCT * 100,
        "stop_loss_pct": C.STOP_LOSS_PCT,
        "daily_loss_kill_pct": -C.DAILY_LOSS_KILL_PCT * 100,
        "max_orders_per_cycle": C.MAX_ORDERS_PER_CYCLE,
        "cycle_seconds": C.CYCLE_SECONDS,
        "benchmark": C.BENCHMARK,
        "live_trading": C.LIVE_TRADING,
        "universe": len(C.UNIVERSE),
    }


def strategy_stats() -> list[dict]:
    """전략별 누적 성과. 백테스트 기록을 판단 주체(brain)별로 묶는다."""
    rows = journal.read("backtests", limit=200)
    by_brain: dict[str, list[dict]] = {}
    for r in rows:
        by_brain.setdefault(str(r.get("brain") or "unknown"), []).append(r)
    out = []
    for name, group in by_brain.items():
        last = group[-1]
        wins = sum(1 for g in group if (g.get("total_return_pct") or 0) > 0)
        vs = last.get("vs_benchmark") or {}
        out.append({
            "name": name,
            "is_ai": brain.is_ai(name),
            "runs": len(group),
            "win_rate": round(wins / len(group) * 100, 1),
            "last_return_pct": last.get("total_return_pct"),
            "last_mdd_pct": last.get("max_drawdown_pct"),
            "trades": last.get("trades"),
            "stop_loss_exits": last.get("stop_loss_exits"),
            "alpha_pct": vs.get("alpha_pct"),
            "beat_benchmark": vs.get("beat_benchmark"),
            "days": last.get("days"),
            "source": last.get("source"),
        })
    out.sort(key=lambda r: (not r["is_ai"], -(r["last_return_pct"] or -999)))
    return out


def trades(limit: int = 120) -> list[dict]:
    """실시간 거래 기록. 체결·거부·오류·손절을 시간순으로 하나의 타임라인에 담는다."""
    rows = journal.read("decisions", limit=limit)
    out = []
    for r in rows:
        out.append({
            "ts": r.get("ts"),
            "symbol": r.get("symbol"),
            "name": data.NAMES.get(r.get("symbol"), r.get("symbol")),
            "action": r.get("action"),
            "sleeve": r.get("sleeve"),
            "quantity": r.get("quantity"),
            "price": r.get("price"),
            "status": r.get("status"),
            "realized_pnl": r.get("realized_pnl"),
            "confidence": r.get("confidence"),
            "reason": r.get("reason"),
            "rejected": r.get("reason_rejected"),
            "brain": r.get("brain"),
            "forced": r.get("forced"),
            "mode": r.get("mode"),
        })
    out.reverse()
    return out


def ai_analysis(limit: int = 12) -> list[dict]:
    """최근 판단과 근거. 종목당 가장 최근 것 하나씩."""
    seen, out = set(), []
    for r in reversed(journal.read("decisions", limit=400)):
        sym = r.get("symbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append({
            "symbol": sym,
            "name": data.NAMES.get(sym, sym),
            "action": r.get("action"),
            "status": r.get("status"),
            "confidence": r.get("confidence"),
            "reason": r.get("reason") or r.get("reason_rejected") or "",
            "brain": r.get("brain"),
            "ts": r.get("ts"),
        })
        if len(out) >= limit:
            break
    return out


def radar() -> dict:
    """퀀트 점수 랭킹. 관측 팩을 그대로 쓴다 — 별도 채점 기준을 또 만들지 않는다."""
    feed = _feed()
    pack = data.observe(feed, C.UNIVERSE, with_news=False)
    rows = []
    for o in pack:
        q = o.get("quant") or {}
        ind = o.get("indicators") or {}
        obs = o.get("observed") or {}
        rows.append({
            "symbol": o["symbol"], "name": o.get("name"), "market": o.get("market"),
            "score": q.get("score"),
            "unavailable": q.get("unavailable") or ind.get("unavailable"),
            "reasons": q.get("reasons", []),
            "parts": q.get("parts", {}),
            "last": obs.get("last"),
            "chg_1d_pct": obs.get("chg_1d_pct"),
            "chg_20d_pct": obs.get("chg_20d_pct"),
            "rsi": ind.get("rsi14"),
            "rsi_zone": ind.get("rsi_zone"),
            "alignment": (ind.get("ma") or {}).get("alignment"),
            "adx": (ind.get("adx") or {}).get("strength"),
            "room": (ind.get("profile_gap") or {}).get("room"),
        })
    rows.sort(key=lambda r: -(r["score"] if r["score"] is not None else -1))
    return {"rows": rows, "source": feed.source,
            "buy_above": C.QUANT_BUY_ABOVE, "sell_below": C.QUANT_SELL_BELOW}


def portfolio() -> dict:
    broker = bk.PaperBroker.load()
    feed = _feed()
    prices = feed.prices(C.UNIVERSE)
    snap = broker.snapshot(prices)
    held = list(broker.positions)
    div = {"symbols": len(held), "note": "보유 2종목 미만 — 상관관계 없음"}
    if len(held) >= 2:
        try:
            series = {s: [b["close"] for b in feed.candles(s, 60)] for s in held}
            div = correlation.diversification({s: c for s, c in series.items() if len(c) >= 31})
        except Exception as exc:
            div = {"unavailable": f"{type(exc).__name__}: {exc}"}
    return {"snapshot": snap, "diversification": div,
            "fills": [f for f in journal.read("decisions", limit=300)
                      if f.get("status") == "FILLED"][-40:][::-1]}


def market_insight(limit: int = 8) -> list[dict]:
    """뉴스 헤드라인. 유니버스 상위 종목 것을 모아 최신순으로."""
    items = []
    for sym in C.UNIVERSE[:6]:
        for n in data.news(sym, limit=2):
            title = n.get("title", "")
            if title.startswith("[news unavailable"):
                continue
            items.append({"symbol": sym, "name": data.NAMES.get(sym, sym),
                          "title": title, "published": n.get("published", "")})
    return items[:limit]


def command_state() -> dict:
    cycles = journal.read("cycles", limit=1)
    incidents = journal.read("incidents", limit=8)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lab": lab_state(),
        "last_cycle": cycles[-1] if cycles else None,
        "brain": C.BRAIN_MODEL if C.have_brain_key() else "quant-fallback",
        "has_llm_key": C.have_brain_key(),
        "guardrails": guardrails(),
        "strategy_stats": strategy_stats(),
        "indices": cached("indices", 300, benchmark.quotes),
        "ai_analysis": ai_analysis(),
        "incidents": incidents[::-1],
        "news": cached("news", 600, market_insight),
    }


def control_state() -> dict:
    from . import check
    rows = cached("check", 60, check.describe)
    return {"checks": [{"mark": m, "label": l, "detail": d} for m, l, d in rows],
            "guardrails": guardrails(),
            "env_loaded": C.ENV_LOADED,
            "universe": [{"symbol": s, "market": C.market_of(s),
                          "name": data.NAMES.get(s, s)} for s in C.UNIVERSE]}


ROUTES = {
    "/api/command": lambda: command_state(),
    "/api/radar": lambda: cached("radar", 120, radar),
    "/api/trades": lambda: {"rows": trades()},
    "/api/portfolio": lambda: portfolio(),
    "/api/control": lambda: control_state(),
}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 콘솔을 요청 로그로 덮지 않는다
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        token = (query.get("t") or [""])[0] or self.headers.get("X-Dash-Token", "")

        if url.path in ("/", "/index.html"):
            page = (WEB / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")

        if url.path not in ROUTES:
            return self._json(404, {"error": "not found"})
        if not secrets.compare_digest(token, TOKEN):
            return self._json(401, {"error": "토큰이 맞지 않는다"})
        try:
            return self._json(200, ROUTES[url.path]())
        except Exception as exc:
            journal.jot("incidents", {"kind": "dashboard_error", "path": url.path,
                                      "trace": traceback.format_exc()[-800:]})
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ai_trader.dashboard", description="다크랩 대시보드 (조회 전용)")
    ap.add_argument("--lan", action="store_true", help="같은 와이파이의 폰에서 접속 허용")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    shown = lan_ip() if args.lan else "127.0.0.1"
    url = f"http://{shown}:{args.port}/?t={TOKEN}"
    print(f"[dashboard] 조회 전용 — 여기서 주문은 나가지 않는다")
    print(f"[dashboard] {url}")
    if not args.lan:
        print("[dashboard] 폰에서 보려면 --lan 을 붙여라")
    ThreadingHTTPServer((host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
