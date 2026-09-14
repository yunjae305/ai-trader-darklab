"""개인용 대시보드 — stdlib 만 쓴다. 빌드 도구도 프레임워크도 없다.

    python3 -m ai_trader.dashboard          127.0.0.1:8765 (이 PC 만)
    python3 -m ai_trader.dashboard --lan    0.0.0.0:8765   (같은 와이파이의 폰에서)

화면은 기록을 읽고, 버튼은 매매 루프를 띄우고 세운다. 주문 자체는 여기서 만들지 않는다 —
무엇을 사고 팔지는 루프가 정하고, 실주문이 나가는지는 AI_TRADER_LIVE 가 정한다.
버튼은 그 환경변수를 바꾸지 못한다.
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (benchmark, brain, broker as bk, config as C, correlation,
               dart, data, engine, journal, news)

WEB = Path(__file__).resolve().parent / "web"
TOKEN = os.getenv("AI_TRADER_DASH_TOKEN") or secrets.token_urlsafe(9)
DAY_ANCHOR = C.LAB / "kiwoom_day.json"   # 당일 수익률의 기준점. 증권사는 이걸 주지 않는다.

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
    live = cached("live-feed", 3600, lambda: data.make_feed(paper=True, live_data=True))
    from .history import StoredDailyFeed
    closed = cached("closed-feed", 3600, lambda: StoredDailyFeed.build(C.UNIVERSE))
    return cached("session-feed", 3600, lambda: data.SessionFeed(live, closed))


def day_anchor(equity: float) -> float | None:
    """오늘 처음 본 계좌 평가액. 증권사는 '당일 수익률'을 주지 않는다 — 직접 기준점을 잡는다.

    키움이 주는 tot_prft_rt 는 매입가 대비 **누적** 수익률이다. 그것을 당일 손익이라고
    적으면 거짓말이 되므로, 당일은 우리가 관측한 첫 평가액으로만 계산한다.
    """
    if equity <= 0:
        return None
    today = datetime.now(data.KST).strftime("%Y-%m-%d")
    try:
        saved = json.loads(DAY_ANCHOR.read_text(encoding="utf-8"))
    except Exception:
        saved = {}
    if saved.get("date") == today and float(saved.get("equity") or 0) > 0:
        return float(saved["equity"])
    DAY_ANCHOR.parent.mkdir(parents=True, exist_ok=True)
    DAY_ANCHOR.write_text(json.dumps({"date": today, "equity": equity}, ensure_ascii=False),
                          encoding="utf-8")
    return equity


def lab_state() -> dict:
    """상단 계좌 현황. demo에서는 키움 모의계좌가 원본이다.

    계좌가 말해준 값만 적는다. 현재가·평가금액·평가손익은 증권사가 주는 것이고,
    슬리브(안정/공격)는 실계좌에 없는 개념이라 지어내지 않고 비워 둔다.
    """
    if C.KIWOOM_MODE == "demo" and C.have_kiwoom_keys():
        account = cached("account", 30, account_state)
        row = account.get("markets", {}).get("KR", {})
        if not row.get("unavailable"):
            cash, equity = float(row.get("cash") or 0), float(row.get("equity") or 0)
            start = day_anchor(equity)
            day_pct = round((equity - start) / start * 100, 2) if start else None
            return {
                "mode": "kiwoom-demo", "source": "kiwoom-mock",
                "market_open": data.KiwoomFeed().is_open(),
                "market_label": "키움 모의투자 · " + ("장 운영중" if data.session_now() else "장 마감"),
                "equity": round(equity), "cash": round(cash),
                "day_start_equity": round(start) if start else None,
                "day_return_pct": day_pct,
                "total_return_pct": row.get("return_pct"), "pnl_amount": row.get("pnl"),
                "invested": row.get("invested"), "eval_amount": row.get("eval_amount"),
                "halted": False,
                # 슬리브는 증권사 계좌에 없다. 비율로 쪼개 만들어 내면 화면이 거짓말을 한다.
                "sleeves": {},
                "sleeves_unavailable": "슬리브(안정6:공격4)는 자체 장부의 개념이다 — 증권사 계좌에는 없다",
                "positions": [{"symbol": p["symbol"], "name": p.get("name") or p["symbol"],
                               "qty": p["qty"], "avg_price": p["avg"],
                               "last_price": p.get("last"), "unrealized_pct": p.get("pnl_pct"),
                               "unrealized_pnl": p.get("pnl"),
                               "value": p.get("value"), "sleeve": "계좌"}
                              for p in row.get("positions", [])],
            }
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
    # 합성 시장에는 장 시간이 없다. 새벽 3시에 "장 운영중"이라고 적으면 거짓말이 된다.
    snap["market_label"] = ("합성 시장" if feed.source == "synthetic"
                            else "장 운영중" if snap["market_open"] else "장 마감")
    return _named(snap)


def _named(snap: dict) -> dict:
    """보유 종목에 이름을 붙인다. 계좌 스냅샷을 쓰는 화면이 여럿이라 한 곳에서만 한다."""
    for p in snap.get("positions", []):
        p["name"] = data.NAMES.get(p["symbol"], p["symbol"])
    return snap


def guardrails() -> dict:
    return {
        "sleeves": C.SLEEVES,
        "market_weights": C.MARKET_WEIGHTS,
        "usd_krw": C.USD_KRW,
        # 0.07*100 은 7.000000000000001 이 된다. 화면에 그대로 내보내지 않는다.
        "max_position_pct": round(C.MAX_POSITION_PCT * 100, 2),
        "stop_loss_pct": round(C.STOP_LOSS_PCT, 2),
        "daily_loss_kill_pct": round(-C.DAILY_LOSS_KILL_PCT * 100, 2),
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
    """최근 LLM 사이클이 직접 고른 후보. 퀀트는 후보 설명의 참고 측정값이다."""
    watch, selected_at, selected_by = [], "", ""
    for cycle in reversed(journal.read("cycles", limit=100)):
        if brain.is_ai(cycle.get("brain")) and isinstance(cycle.get("watchlist"), list):
            if cycle["watchlist"]:
                watch, selected_at, selected_by = cycle["watchlist"][:8], cycle.get("ts", ""), cycle.get("brain", "")
                break
    symbols = [str(w.get("symbol") or "") for w in watch]
    symbols = [s for s in symbols if s in C.UNIVERSE]
    if not symbols:
        return {"rows": [], "source": "LLM", "as_of": "",
                "selected_at": selected_at, "selected_by": selected_by}
    feed = _feed()
    pack = data.observe(feed, symbols, with_news=False)
    measured = {o["symbol"]: o for o in pack}
    rows = []
    for w in watch:
        o = measured.get(str(w.get("symbol") or ""))
        if not o:
            continue
        q = o.get("quant") or {}
        ind = o.get("indicators") or {}
        obs = o.get("observed") or {}
        rows.append({
            "symbol": o["symbol"], "name": o.get("name"), "market": o.get("market"),
            "as_of": o.get("as_of"),
            "sector": o.get("sector") or "",
            "score": q.get("score"),
            "conviction": max(0, min(1, float(w.get("conviction") or 0))),
            "thesis": str(w.get("thesis") or ""), "risk": str(w.get("risk") or ""),
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
    rows.sort(key=lambda r: -r["conviction"])
    as_of = max((str(r.get("as_of") or "") for r in rows), default="")
    return {"rows": rows, "source": feed.source, "as_of": as_of,
            "selected_at": selected_at, "selected_by": selected_by}


def portfolio() -> dict:
    state = C.MOCK_STATE if C.KIWOOM_MODE == "demo" else C.STATE
    broker = bk.PaperBroker.load(state)
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
    return {"snapshot": _named(snap), "diversification": div,
            "fills": [f for f in journal.read("decisions", limit=300)
                      if f.get("status") == "FILLED"][-40:][::-1]}


def market_insight(limit: int = 20) -> list[dict]:
    """시장 전체 헤드라인. 증권·경제 매체 RSS 를 직접 받아 합친 것 (news.py)."""
    return news.headlines(limit=limit)


def command_state() -> dict:
    cycles = journal.read("cycles", limit=1)
    incidents = journal.read("incidents", limit=8)
    feed = _feed()
    quotes = cached("indices", 300, benchmark.quotes)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lab": lab_state(),
        "last_cycle": cycles[-1] if cycles else None,
        "brain": ({"api": C.BRAIN_MODEL, "cli": f"{C.BRAIN_MODEL}(cli→codex)",
                   "codex": f"{C.CODEX_MODEL or 'codex'}(cli)"}
                  .get(C.brain_backend(), "quant-fallback")),
        "has_llm_key": C.have_brain(),
        "brain_backend": C.brain_backend(),
        "guardrails": guardrails(),
        "strategy_stats": strategy_stats(),
        "engine": engine.status(),
        "market_sessions": {
            "KR": {"open": data.feed_open_for(feed, "005930"),
                   "local_time": datetime.now(data.KST).strftime("%H:%M"), "zone": "KST"},
            "US": {"open": data.feed_open_for(feed, "AAPL"),
                   "local_time": datetime.now(data.ET).strftime("%H:%M"), "zone": "ET"},
        },
        "indices": [q for q in quotes if q.get("symbol") in {"^KS11", "^KQ11", "^GSPC"}],
        "fx": [q for q in quotes if q.get("symbol") in {"KRW=X", "JPYKRW=X"}],
        "ai_analysis": ai_analysis(),
        "incidents": incidents[::-1],
        "news": cached("news", 600, market_insight),
    }


CURRENCY = {"KR": "원", "US": "$"}


def account_state() -> dict:
    """대시보드에 표시할 증권사 계좌 — 조회만 한다. 주문은 이 경로로 나가지 않는다.

    KIWOOM_MODE=demo 동안에는 키움 모의계좌만 표시한다. 사용자가 실계좌 화면을 명시적으로
    요청하기 전에는 토스 실계좌를 대시보드에 섞지 않는다.
    """
    mock = C.KIWOOM_MODE == "demo"
    venues = {"KR": bk.KiwoomVenue} if mock else bk.VENUES
    out = {"markets": {}, "mode": "mock" if mock else "live",
           "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    for market, cls in venues.items():
        why = cls.unavailable()
        if why:
            out["markets"][market] = {"unavailable": why, "venue": cls.name}
            continue
        try:
            venue = cls()
            if mock and hasattr(venue, "summary"):
                summary = venue.summary()
                rows, cash = summary["positions"], summary["cash"]
            else:
                summary = {}
                rows, cash = venue.positions(), venue.cash()
        except Exception as exc:
            out["markets"][market] = {"venue": cls.name,
                                      "unavailable": f"{type(exc).__name__}: {str(exc)[:160]}"}
            continue
        out["markets"][market] = {
            "venue": venue.name, "currency": CURRENCY.get(market, ""), "cash": cash,
            "equity": summary.get("equity"), "pnl": summary.get("pnl"),
            "return_pct": summary.get("return_pct"),
            "invested": summary.get("invested"), "eval_amount": summary.get("eval_amount"),
            # value 는 평가금액이다. 증권사가 안 주면 매입금액으로 덮지 않고 비워 둔다 —
            # 매입금액을 평가금액 자리에 적으면 손익이 항상 0 으로 보인다.
            "positions": [{**r, "name": r.get("name") or data.NAMES.get(r["symbol"], r["symbol"])}
                          for r in rows],
        }
    return out


def orders_state() -> dict:
    """오늘 낸 주문 — 미체결과 체결. "주문이 진짜 나갔나"를 확인하는 화면이 쓴다.

    장부(decisions 기록)는 우리가 적은 것이고, 이건 증권사 원장이 말하는 것이다.
    둘이 어긋나면 어긋난 대로 보여야 한다 — 그래서 한쪽으로 합치지 않는다.
    """
    if not (C.KIWOOM_MODE == "demo" and C.have_kiwoom_keys()):
        return {"rows": [], "unavailable": f"키움 모의투자 계좌가 아니다 (KIWOOM_MODE={C.KIWOOM_MODE})"}
    why = bk.KiwoomVenue.unavailable()
    if why:
        return {"rows": [], "unavailable": why}
    try:
        venue = bk.KiwoomVenue()
        return {"rows": venue.orders(), "venue": venue.name,
                "generated_at": time.strftime("%H:%M:%S")}
    except Exception as exc:
        return {"rows": [], "unavailable": f"{type(exc).__name__}: {str(exc)[:160]}"}


def financials(symbol: str) -> dict:
    """DART 재무제표. 공시는 분기에 한 번 바뀌므로 종목당 하루를 캐시한다."""
    symbol = (symbol or "").strip()
    if not symbol:
        return {"unavailable": "종목을 지정하지 않았다"}
    try:
        return cached(f"dart:{symbol}", 86400, lambda: dart.statements(symbol))
    except dart.Unavailable as exc:
        # 못 읽은 이유를 그대로 화면에 보낸다 — 빈 표는 0원이라는 거짓말이 된다.
        return {"symbol": symbol, "name": data.NAMES.get(symbol, symbol),
                "unavailable": str(exc)}


def control_state() -> dict:
    from . import check
    rows = cached("check", 60, check.describe)
    return {"checks": [{"mark": m, "label": l, "detail": d} for m, l, d in rows],
            "guardrails": guardrails(),
            "env_loaded": C.ENV_LOADED,
            "universe": [{"symbol": s, "market": C.market_of(s),
                          "name": data.NAMES.get(s, s)} for s in C.UNIVERSE]}


# 상태를 바꾸는 것은 POST 로만 받는다. 링크를 여는 것만으로 매매가 시작되면 안 된다.
ACTIONS = {
    "/api/engine/start": engine.start,
    "/api/engine/stop": engine.stop,
    "/api/engine/once": engine.run_once,
}

# 조회 경로는 쿼리스트링(parse_qs 결과)을 받는다. 대부분은 쓰지 않는다.
ROUTES = {
    "/api/command": lambda q: command_state(),
    "/api/engine": lambda q: engine.status(),
    "/api/radar": lambda q: cached("radar", 120, radar),
    "/api/trades": lambda q: {"rows": trades()},
    "/api/portfolio": lambda q: portfolio(),
    "/api/control": lambda q: control_state(),
    "/api/financials": lambda q: financials((q.get("symbol") or [""])[0]),
    # 증권사를 매번 부르므로 30초만 캐시한다. 새로고침을 눌러도 폭주하지 않는다.
    "/api/account": lambda q: cached("account", 30, account_state),
    # 주문은 초 단위로 바뀐다 — 잔고보다 짧게 잡는다.
    "/api/orders": lambda q: cached("orders", 15, orders_state),
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

    def _token_ok(self, url) -> bool:
        token = (parse_qs(url.query).get("t") or [""])[0] or self.headers.get("X-Dash-Token", "")
        return secrets.compare_digest(token, TOKEN)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path not in ACTIONS:
            return self._json(404, {"error": "not found"})
        if not self._token_ok(url):
            return self._json(401, {"error": "토큰이 맞지 않는다"})
        try:
            out = ACTIONS[url.path]()
            _cache.pop("check", None)  # 엔진 상태가 바뀌었으니 점검 캐시를 버린다
            return self._json(200, out)
        except Exception as exc:
            journal.jot("incidents", {"kind": "engine_error", "path": url.path,
                                      "trace": traceback.format_exc()[-800:]})
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

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
            return self._json(200, ROUTES[url.path](query))
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
