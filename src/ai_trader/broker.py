"""주문 집행 계층. 6:4 슬리브 회계는 여기서만 강제된다.

PaperBroker  — 키 없이 도는 모의 계좌 (한 달 테스트용)
TossBroker   — 토스증권 OpenAPI 실계좌 (AI_TRADER_LIVE=1 일 때만 실주문)

두 브로커는 같은 인터페이스를 갖는다: snapshot / buy / sell / mark
매수·매도·보유 중 무엇을 할지는 brain 이 정한다. 브로커는 "할 수 있나"만 답한다.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date

import requests

from . import config as C

FEE_RATE = 0.00015  # 위탁수수료 근사
TAX_RATE = 0.0018   # 매도 시 거래세 근사


class Rejected(Exception):
    """주문이 가드레일이나 잔고에 막혔다. 랩은 멈추지 않고 다음 판단으로 넘어간다."""


@dataclass
class Position:
    symbol: str
    sleeve: str
    qty: int
    avg: float

    def value(self, price: float) -> float:
        return self.qty * price

    def pnl_pct(self, price: float) -> float:
        return 0.0 if self.avg <= 0 else (price - self.avg) / self.avg * 100


@dataclass
class PaperBroker:
    cash: dict[str, float] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    realized: dict[str, float] = field(default_factory=dict)
    day: str = ""
    day_start_equity: float = 0.0
    halted: bool = False
    fills: list[dict] = field(default_factory=list)

    mode = "paper"

    # ---------- 생성/영속 ----------
    @classmethod
    def fresh(cls, start_cash: float | None = None) -> "PaperBroker":
        cash_total = C.START_CASH if start_cash is None else start_cash
        b = cls(
            cash={k: cash_total * w for k, w in C.SLEEVES.items()},
            realized={k: 0.0 for k in C.SLEEVES},
            day=date.today().isoformat(),
            day_start_equity=cash_total,
        )
        return b

    @classmethod
    def load(cls, path=None) -> "PaperBroker":
        path = path or C.STATE
        if not path.exists():
            return cls.fresh()
        raw = json.loads(path.read_text())
        b = cls(
            cash=raw["cash"],
            positions={s: Position(**p) for s, p in raw["positions"].items()},
            realized=raw["realized"],
            day=raw["day"],
            day_start_equity=raw["day_start_equity"],
            halted=raw.get("halted", False),
        )
        return b

    def save(self, path=None) -> None:
        path = path or C.STATE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "cash": self.cash,
            "positions": {s: vars(p) for s, p in self.positions.items()},
            "realized": self.realized,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "halted": self.halted,
        }, ensure_ascii=False, indent=2))

    # ---------- 조회 ----------
    def equity(self, prices: dict[str, float]) -> float:
        held = sum(p.value(prices.get(s, p.avg)) for s, p in self.positions.items())
        return sum(self.cash.values()) + held

    def sleeve_equity(self, sleeve: str, prices: dict[str, float]) -> float:
        held = sum(p.value(prices.get(s, p.avg))
                   for s, p in self.positions.items() if p.sleeve == sleeve)
        return self.cash.get(sleeve, 0.0) + held

    def snapshot(self, prices: dict[str, float]) -> dict:
        return {
            "mode": self.mode,
            "halted": self.halted,
            "equity": round(self.equity(prices)),
            "day_start_equity": round(self.day_start_equity),
            "day_return_pct": round(self.day_return_pct(prices), 3),
            "sleeves": {
                k: {
                    "target_weight": C.SLEEVES[k],
                    "cash": round(self.cash.get(k, 0.0)),
                    "equity": round(self.sleeve_equity(k, prices)),
                    "realized_pnl": round(self.realized.get(k, 0.0)),
                } for k in C.SLEEVES
            },
            "positions": [
                {
                    "symbol": s, "sleeve": p.sleeve, "qty": p.qty,
                    "avg_price": round(p.avg, 2),
                    "last_price": round(prices.get(s, p.avg), 2),
                    "unrealized_pct": round(p.pnl_pct(prices.get(s, p.avg)), 2),
                    "value": round(p.value(prices.get(s, p.avg))),
                } for s, p in sorted(self.positions.items())
            ],
        }

    def day_return_pct(self, prices: dict[str, float]) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return (self.equity(prices) - self.day_start_equity) / self.day_start_equity * 100

    def roll_day(self, prices: dict[str, float], today: str | None = None) -> bool:
        """날짜가 바뀌면 당일 기준자산과 킬스위치를 리셋한다."""
        today = today or date.today().isoformat()
        if today == self.day:
            return False
        self.day = today
        self.day_start_equity = self.equity(prices)
        self.halted = False
        return True

    # ---------- 집행 ----------
    def buy(self, symbol: str, sleeve: str, qty: int, price: float) -> dict:
        if self.halted:
            raise Rejected("lab halted by daily loss kill-switch")
        if sleeve not in C.SLEEVES:
            raise Rejected(f"unknown sleeve {sleeve}")
        if qty <= 0 or price <= 0:
            raise Rejected("qty/price must be positive")
        cost = qty * price * (1 + FEE_RATE)
        if cost > self.cash.get(sleeve, 0.0):
            raise Rejected(f"insufficient {sleeve} cash: need {cost:.0f}, have {self.cash.get(sleeve, 0):.0f}")

        existing = self.positions.get(symbol)
        if existing and existing.sleeve != sleeve:
            raise Rejected(f"{symbol} already held in {existing.sleeve} sleeve")
        sleeve_eq = self.sleeve_equity(sleeve, {symbol: price})
        held_after = (existing.value(price) if existing else 0.0) + qty * price
        if sleeve_eq > 0 and held_after / sleeve_eq > C.MAX_POSITION_PCT:
            raise Rejected(
                f"position cap: {symbol} would be {held_after / sleeve_eq:.1%} of {sleeve} "
                f"(max {C.MAX_POSITION_PCT:.0%})")

        self.cash[sleeve] -= cost
        if existing:
            total = existing.qty + qty
            existing.avg = (existing.avg * existing.qty + price * qty) / total
            existing.qty = total
        else:
            self.positions[symbol] = Position(symbol, sleeve, qty, price)
        fill = {"side": "BUY", "symbol": symbol, "sleeve": sleeve, "qty": qty,
                "price": price, "cost": round(cost), "ts": time.time()}
        self.fills.append(fill)
        return fill

    def sell(self, symbol: str, qty: int, price: float) -> dict:
        pos = self.positions.get(symbol)
        if pos is None:
            raise Rejected(f"no position in {symbol}")
        qty = min(qty, pos.qty)
        if qty <= 0 or price <= 0:
            raise Rejected("qty/price must be positive")
        gross = qty * price
        proceeds = gross * (1 - FEE_RATE - TAX_RATE)
        realized = proceeds - pos.avg * qty
        self.cash[pos.sleeve] += proceeds
        self.realized[pos.sleeve] = self.realized.get(pos.sleeve, 0.0) + realized
        pos.qty -= qty
        sleeve = pos.sleeve
        if pos.qty == 0:
            del self.positions[symbol]  # 전량 매도 → 언제든 재매수 가능
        fill = {"side": "SELL", "symbol": symbol, "sleeve": sleeve, "qty": qty,
                "price": price, "proceeds": round(proceeds),
                "realized_pnl": round(realized), "ts": time.time()}
        self.fills.append(fill)
        return fill

    def check_kill_switch(self, prices: dict[str, float]) -> str | None:
        if self.halted:
            return "already halted"
        if self.day_return_pct(prices) <= -abs(C.DAILY_LOSS_KILL_PCT * 100):
            self.halted = True
            return f"daily loss {self.day_return_pct(prices):.2f}% breached kill-switch"
        return None


class TossBroker(PaperBroker):
    """토스증권 OpenAPI. 회계는 PaperBroker 를 그대로 쓰고 집행만 실계좌로 보낸다."""

    mode = "toss"

    def __init__(self, **kw):
        super().__init__(**kw)
        self._token = ""
        self._token_exp = 0.0
        self.session = requests.Session()

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.session.post(
            f"{C.TOSS_BASE}/oauth2/token",
            data={"grant_type": "client_credentials",
                  "client_id": C.TOSS_CLIENT_ID,
                  "client_secret": C.TOSS_CLIENT_SECRET},
            timeout=10)
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _headers(self, account: bool = False) -> dict:
        h = {"Authorization": f"Bearer {self.token()}"}
        if account:
            h["X-Tossinvest-Account"] = C.TOSS_ACCOUNT
        return h

    def holdings(self) -> list[dict]:
        r = self.session.get(f"{C.TOSS_BASE}/api/v1/holdings",
                             headers=self._headers(account=True), timeout=10)
        r.raise_for_status()
        return r.json().get("holdings", r.json().get("result", []))

    def _send(self, symbol: str, side: str, qty: int, price: float) -> dict:
        if not C.LIVE_TRADING:
            return {"skipped": "AI_TRADER_LIVE != 1", "symbol": symbol, "side": side, "qty": qty}
        r = self.session.post(
            f"{C.TOSS_BASE}/api/v1/orders",
            headers={**self._headers(account=True), "Content-Type": "application/json"},
            json={"symbol": symbol, "side": side, "quantity": qty,
                  "orderType": "LIMIT", "price": price,
                  "clientOrderId": uuid.uuid4().hex[:32]},
            timeout=10)
        if r.status_code >= 400:
            raise Rejected(f"toss {r.status_code}: {r.text[:200]}")
        return r.json()

    def buy(self, symbol: str, sleeve: str, qty: int, price: float) -> dict:
        fill = super().buy(symbol, sleeve, qty, price)  # 가드레일 먼저 통과해야 한다
        fill["broker"] = self._send(symbol, "BUY", qty, price)
        return fill

    def sell(self, symbol: str, qty: int, price: float) -> dict:
        fill = super().sell(symbol, qty, price)
        fill["broker"] = self._send(symbol, "SELL", fill["qty"], price)
        return fill


class KiwoomBroker(PaperBroker):
    """키움 REST 국내주식. 회계는 PaperBroker, 집행만 증권사로 보낸다.

    토스와 결정적으로 다른 점 — 키움은 모의투자 서버(mockapi)가 따로 있다.
    demo 모드 주문은 돈이 걸리지 않으므로 AI_TRADER_LIVE 없이도 실제로 나간다.
    real 모드는 토스와 같은 규칙이다: AI_TRADER_LIVE=1 이어야만 나간다.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        from .kiwoom import Kiwoom
        self.api = Kiwoom()
        self.mode = f"kiwoom-{self.api.mode}"

    def _send(self, symbol: str, side: str, qty: int, price: float) -> dict:
        if self.api.mode == "real" and not C.LIVE_TRADING:
            return {"skipped": "AI_TRADER_LIVE != 1", "symbol": symbol, "side": side, "qty": qty}
        from .kiwoom import snap_price
        try:
            return self.api.order(symbol, side, qty, snap_price(price))
        except Exception as exc:  # 원장 거절로 랩을 세우지 않는다 — 기록하고 다음 판단으로
            raise Rejected(f"kiwoom: {exc}") from None

    def buy(self, symbol: str, sleeve: str, qty: int, price: float) -> dict:
        fill = super().buy(symbol, sleeve, qty, price)  # 가드레일 먼저 통과해야 한다
        fill["broker"] = self._send(symbol, "BUY", qty, price)
        return fill

    def sell(self, symbol: str, qty: int, price: float) -> dict:
        fill = super().sell(symbol, qty, price)
        fill["broker"] = self._send(symbol, "SELL", fill["qty"], price)
        return fill


def make_broker(paper: bool = True, kiwoom: bool = False):
    if kiwoom:
        if not C.have_kiwoom_keys():
            raise RuntimeError(
                f"키움 키가 없다 — KIWOOM_MODE={C.KIWOOM_MODE} 에 맞는 "
                f"APP_KEY{C._KW_SUFFIX}/APP_SECRET{C._KW_SUFFIX} 를 .env 에 넣어라")
        return KiwoomBroker.load()
    if paper or not C.have_broker_keys():
        return PaperBroker.load()
    return TossBroker.load()
