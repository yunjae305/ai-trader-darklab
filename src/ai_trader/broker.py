"""주문 집행 계층. 6:4 슬리브 회계는 여기서만 강제된다.

PaperBroker   — 키 없이 도는 모의 계좌 (자체 장부)
RoutedBroker  — 회계는 PaperBroker 그대로, 주문만 시장별 창구로 내려보낸다

    국내 (6자리 코드) → KiwoomVenue  (KIWOOM_MODE=demo 면 모의투자, 돈 안 걸림)
    해외 (영문 티커)  → TossVenue    (모의투자 서버 없음 — AI_TRADER_LIVE=1 필요)

한 포트폴리오가 두 시장을 같이 담으므로 창구는 종목마다 갈린다.
매수·매도·보유 중 무엇을 할지는 brain 이 정한다. 브로커는 "할 수 있나"만 답한다.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import requests

from . import config as C

FEE_RATE = 0.00015  # 위탁수수료 근사
TAX_RATE = 0.0018   # 매도 시 거래세 근사

# 국내(KRX)에는 소수점 주식이 없다. 토스 해외주식은 소수점 6자리까지 쓴다.
# 한 장부가 두 시장을 담으므로 정밀도도 종목마다 갈린다.
US_QTY_DECIMALS = 6
QTY_EPSILON = 10 ** -(US_QTY_DECIMALS + 3)  # float 비교용. 이보다 작으면 0 으로 본다


def base_price(symbol: str, price: float) -> float:
    """원화 기준 장부 가격. 해외 가격은 설정된 USD/KRW로 환산한다."""
    return float(price) if C.market_of(symbol) == "KR" else float(price) * C.USD_KRW


def round_qty(symbol: str, qty: float) -> float:
    """시장이 허용하는 최소 단위로 **내림**한다. 올림하면 없는 돈을 쓴다."""
    try:
        value = Decimal(str(qty))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    if not value.is_finite() or value <= 0:
        return 0.0
    if C.market_of(symbol) == "KR":
        return float(value.to_integral_value(rounding=ROUND_DOWN))
    quantum = Decimal(1).scaleb(-US_QTY_DECIMALS)
    return float(value.quantize(quantum, rounding=ROUND_DOWN))


def _usd_amount(qty: float, price: float) -> str:
    """소수점 매수 금액을 센트 단위로 내린다. 반올림으로 예산을 넘기지 않는다."""
    amount = Decimal(str(qty)) * Decimal(str(price))
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN), "f")


def _us_limit_price(price: float) -> str:
    """토스 미국주식 지정가: $1 이상 2자리, 미만 4자리에서 내림."""
    value = Decimal(str(price))
    quantum = Decimal("0.01") if value >= 1 else Decimal("0.0001")
    return format(value.quantize(quantum, rounding=ROUND_DOWN), "f")


class Rejected(Exception):
    """주문이 가드레일이나 잔고에 막혔다. 랩은 멈추지 않고 다음 판단으로 넘어간다."""


@dataclass
class Position:
    symbol: str
    sleeve: str
    qty: float  # 해외는 소수점 주식이 있다 (NVDA 0.113154주)
    avg: float

    def value(self, price: float) -> float:
        return self.qty * base_price(self.symbol, price)

    def pnl_pct(self, price: float) -> float:
        return 0.0 if self.avg <= 0 else (price - self.avg) / self.avg * 100


@dataclass
class PaperBroker:
    cash: dict[str, float] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    realized: dict[str, float] = field(default_factory=dict)
    day: str = ""
    day_start_equity: float = 0.0
    # 기준자산을 '실계좌를 보고' 세운 거래일. 비어 있으면 아직 설정값으로만 세운 것이다.
    baseline_day: str = ""
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
            baseline_day=raw.get("baseline_day", ""),
            halted=raw.get("halted", False),
        )
        return b

    def save(self, path=None) -> None:
        """장부를 디스크에 남긴다. 쓰다가 죽어도 반쪽 파일이 남지 않게 바꿔 끼운다.

        write_text 로 덮어쓰다 프로세스가 죽으면 JSON 이 잘린 채 남고, 다음 실행의
        load() 가 거기서 터진다 — 현금도 보유도 통째로 사라진다. 임시 파일에 다 쓴 뒤
        이름만 바꾸면 파일은 항상 '이전 것' 아니면 '새 것' 둘 중 하나다.
        """
        path = path or C.STATE
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({
            "cash": self.cash,
            "positions": {s: vars(p) for s, p in self.positions.items()},
            "realized": self.realized,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "baseline_day": self.baseline_day,
            "halted": self.halted,
        }, ensure_ascii=False, indent=2)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)

    # ---------- 조회 ----------
    def equity(self, prices: dict[str, float]) -> float:
        held = sum(p.value(prices.get(s, p.avg)) for s, p in self.positions.items())
        return sum(self.cash.values()) + held

    def sleeve_equity(self, sleeve: str, prices: dict[str, float]) -> float:
        held = sum(p.value(prices.get(s, p.avg))
                   for s, p in self.positions.items() if p.sleeve == sleeve)
        return self.cash.get(sleeve, 0.0) + held

    def snapshot(self, prices: dict[str, float]) -> dict:
        market_values = {m: round(sum(p.value(prices.get(s, p.avg))
                                            for s, p in self.positions.items()
                                            if C.market_of(s) == m))
                         for m in C.MARKETS}
        total = self.equity(prices)
        return {
            "mode": self.mode,
            "halted": self.halted,
            "equity": round(self.equity(prices)),
            "day_start_equity": round(self.day_start_equity),
            "day_return_pct": round(self.day_return_pct(prices), 3),
            "base_currency": "KRW", "usd_krw": C.USD_KRW,
            "market_allocation": {
                m: {"max_weight": C.MARKET_WEIGHTS[m], "value": market_values[m],
                    "weight": round(market_values[m] / total, 4) if total else 0}
                for m in C.MARKETS},
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

    def adopt_account_baseline(self, equity: float) -> bool:
        """실계좌에서 알게 된 자산을 당일 기준자산으로 채택한다. 세웠으면 True.

        fresh() 의 기준자산은 C.START_CASH 라는 설정값이고 실계좌와 무관하다 —
        1,000 만으로 출발한 장부가 500 만 계좌를 처음 보면 거래 한 건 없이 당일
        -50% 가 되어 킬스위치가 내려갔다. 반대로 같은 날 재시작이 기준을 다시
        세우면 그날 쌓인 손실이 0 으로 지워진다. 그래서 '그날 한 번만' 세운다.
        """
        today = date.today().isoformat()
        if self.day_start_equity > 0 and self.baseline_day == today:
            return False
        self.day_start_equity = equity
        self.baseline_day = today
        return True

    def roll_day(self, prices: dict[str, float], today: str | None = None) -> bool:
        """날짜가 바뀌면 당일 기준자산과 킬스위치를 리셋한다."""
        today = today or date.today().isoformat()
        if today == self.day:
            return False
        self.day = today
        self.day_start_equity = self.equity(prices)
        self.baseline_day = ""      # 새 거래일의 기준은 다음 계좌 동기화가 다시 세운다
        self.halted = False
        return True

    # ---------- 집행 ----------
    def buy(self, symbol: str, sleeve: str, qty: float, price: float) -> dict:
        if self.halted:
            raise Rejected("lab halted by daily loss kill-switch")
        if sleeve not in C.SLEEVES:
            raise Rejected(f"unknown sleeve {sleeve}")
        qty = round_qty(symbol, qty)  # 시장이 못 받는 단위는 여기서 잘라낸다
        if qty <= 0 or price <= 0:
            raise Rejected("qty/price must be positive")
        cost = qty * base_price(symbol, price) * (1 + FEE_RATE)
        if cost > self.cash.get(sleeve, 0.0):
            raise Rejected(f"insufficient {sleeve} cash: need {cost:.0f}, have {self.cash.get(sleeve, 0):.0f}")

        existing = self.positions.get(symbol)
        if existing and existing.sleeve != sleeve:
            raise Rejected(f"{symbol} already held in {existing.sleeve} sleeve")
        current_prices = {s: p.avg for s, p in self.positions.items()}
        current_prices[symbol] = price
        sleeve_eq = self.sleeve_equity(sleeve, current_prices)
        held_after = ((existing.value(price) if existing else 0.0)
                      + qty * base_price(symbol, price))
        if sleeve_eq > 0 and held_after / sleeve_eq > C.MAX_POSITION_PCT:
            raise Rejected(
                f"position cap: {symbol} would be {held_after / sleeve_eq:.1%} of {sleeve} "
                f"(max {C.MAX_POSITION_PCT:.0%})")
        market = C.market_of(symbol)
        total_eq = self.equity(current_prices)
        market_now = sum(p.value(current_prices.get(s, p.avg))
                         for s, p in self.positions.items() if C.market_of(s) == market)
        market_after = market_now + qty * base_price(symbol, price)
        if total_eq > 0 and market_after / total_eq > C.MARKET_WEIGHTS[market]:
            raise Rejected(f"market cap: {market} would be {market_after / total_eq:.1%} "
                           f"(max {C.MARKET_WEIGHTS[market]:.0%})")

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

    def sell(self, symbol: str, qty: float, price: float) -> dict:
        pos = self.positions.get(symbol)
        if pos is None:
            raise Rejected(f"no position in {symbol}")
        # 보유량은 이미 시장 단위에 맞다. 요청만 자르고, 남은 전량 매도는 그대로 통과시킨다.
        qty = min(round_qty(symbol, qty), pos.qty)
        if qty <= 0 or price <= 0:
            raise Rejected("qty/price must be positive")
        gross = qty * base_price(symbol, price)
        proceeds = gross * (1 - FEE_RATE - TAX_RATE)
        realized = proceeds - base_price(symbol, pos.avg) * qty
        self.cash[pos.sleeve] += proceeds
        self.realized[pos.sleeve] = self.realized.get(pos.sleeve, 0.0) + realized
        pos.qty -= qty
        sleeve = pos.sleeve
        # float 뺄셈은 0 대신 1e-17 을 남긴다. 그걸 보유로 들고 있으면 재매수가 막힌다.
        if pos.qty < QTY_EPSILON:
            del self.positions[symbol]  # 전량 매도 → 언제든 재매수 가능
        fill = {"side": "SELL", "symbol": symbol, "sleeve": sleeve, "qty": qty,
                "price": price, "proceeds": round(proceeds),
                "realized_pnl": round(realized), "ts": time.time()}
        self.fills.append(fill)
        return fill

    def stop_loss_breaches(self, prices: dict[str, float]) -> list[dict]:
        """손절선을 넘긴 보유 종목. 판단이 아니라 경계다 — brain 에게 묻지 않는다.

        여기서 목록만 돌려주고 실제 매도는 호출부가 한다. 매도 자체가 실패할 수 있고
        (호가 없음·원장 거절), 그때도 랩은 서지 않고 기록만 남겨야 하기 때문이다.
        """
        out = []
        for sym, pos in self.positions.items():
            price = prices.get(sym)
            if not price or price <= 0:
                continue  # 가격을 모르면 손실도 모른다. 모른 채로 팔지 않는다.
            pnl = pos.pnl_pct(price)
            if pnl <= C.STOP_LOSS_PCT:
                out.append({"symbol": sym, "sleeve": pos.sleeve, "qty": pos.qty,
                            "price": price, "pnl_pct": round(pnl, 2)})
        return out

    def check_kill_switch(self, prices: dict[str, float]) -> str | None:
        if self.halted:
            return "already halted"
        if self.day_return_pct(prices) <= -abs(C.DAILY_LOSS_KILL_PCT * 100):
            self.halted = True
            return f"daily loss {self.day_return_pct(prices):.2f}% breached kill-switch"
        return None

# ---------------------------------------------------------------------------
# 주문 창구. 회계는 하지 않는다 — 주문을 내고 결과를 돌려줄 뿐이다.
# 국내(6자리) → 키움, 해외(영문 티커) → 토스.
# ---------------------------------------------------------------------------

def _num(v) -> float:
    """'+70,000' / '-1200' / None → float. 키움은 부호·콤마가 붙어 온다."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return abs(float(v))
    s = str(v).strip().replace(",", "").lstrip("+")
    try:
        return abs(float(s)) if s else 0.0
    except ValueError:
        return 0.0


def _first_list(body, *keys) -> list:
    """응답에서 목록이 들어 있는 첫 키. 증권사마다 감싸는 이름이 달라서 필요하다."""
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for k in keys:
        v = body.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            inner = _first_list(v, *keys)
            if inner:
                return inner
    return []


def _avail(node: dict) -> float:
    """주문가능금액. 0 은 유효한 값이므로 예수금으로 대체하지 않는다.

    `ord_alow_amt or entr` 는 숫자 0 을 falsy 로 먹어 예수금을 주문가능금액으로
    승격시킨다 — 실제로 한 주도 못 사는 계좌를 살 수 있다고 본다. 문자열 '0' 일
    때는 truthy 라서 통과하므로, 같은 계좌가 응답 표기에 따라 다르게 동작했다.
    필드가 아예 없을 때만 예수금으로 물러난다.
    """
    v = node.get("ord_alow_amt")
    return _num(v if v is not None and str(v).strip() != "" else node.get("entr"))


class Venue:
    market = ""
    name = ""
    CAN_CONFIRM = False        # confirm() 이 원장에 실제로 물어보는 창구만 True

    def send(self, symbol: str, side: str, qty: float, price: float) -> dict:
        raise NotImplementedError

    def confirm(self, sent: dict, symbol: str, want_qty: float,
                want_price: float) -> dict:
        """주문이 실제로 얼마나, 얼마에 체결됐는지 원장에 물어본다.

        접수는 체결이 아니다. 창구가 확인을 지원하지 않으면 확인했다고 하지 않는다 —
        state='UNKNOWN' 으로 돌려주고, 장부를 얼마나 움직일지는 호출부가 정한다.
        """
        return {"filled_qty": 0.0, "fill_price": want_price, "open_qty": want_qty,
                "state": "UNKNOWN", "detail": "이 창구는 체결 확인을 지원하지 않는다"}

    def positions(self) -> list[dict]:
        """실계좌 보유. [{symbol, qty, avg}] — 못 읽으면 예외를 던진다(빈 목록 아님)."""
        raise NotImplementedError

    def cash(self) -> float:
        """주문 가능 현금."""
        raise NotImplementedError

    @staticmethod
    def unavailable() -> str:
        """쓸 수 없으면 그 이유. 쓸 수 있으면 빈 문자열."""
        return ""


class TossVenue(Venue):
    """토스증권 OpenAPI — 해외주식 주문. 토스에는 모의투자 서버가 없다.

    그래서 실주문은 AI_TRADER_LIVE=1 일 때만 나간다. 시세 조회 토큰은 이 클래스가 쥔다
    (TossFeed 도 이 토큰을 쓴다 — 계좌 헤더 없이 시세만 부르는 경로다).
    """
    market = "US"
    name = "토스증권"

    def __init__(self):
        self._token = ""
        self._token_exp = 0.0
        self._seq = C.TOSS_ACCOUNT
        self.session = requests.Session()

    @staticmethod
    def unavailable() -> str:
        return "" if C.have_broker_keys() else "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 없음"

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

    def accounts(self) -> list[dict]:
        """계좌 목록. 계좌·주문 API 가 요구하는 accountSeq 가 여기서 나온다."""
        r = self.session.get(f"{C.TOSS_BASE}/api/v1/accounts",
                             headers={"Authorization": f"Bearer {self.token()}"}, timeout=10)
        r.raise_for_status()
        return _first_list(r.json(), "accounts", "result", "items")

    def account_seq(self) -> str:
        """TOSS_ACCOUNT 가 비어 있으면 계좌 목록에서 찾아낸다.

        사람이 accountSeq 를 미리 알 방법이 마땅치 않다 — 키만 넣으면 되게 하려면
        여기서 직접 물어보는 편이 낫다. .env 에 적어두면 그 값이 우선이다.
        """
        if self._seq:
            return self._seq
        rows = self.accounts()
        if not rows:
            raise Rejected("토스 계좌 목록이 비어 있다 — WTS 에서 Open API 사용 계좌를 확인하라")
        seq = rows[0].get("accountSeq") or rows[0].get("account_seq") or rows[0].get("seq")
        if seq is None:
            raise Rejected(f"계좌 응답에서 accountSeq 를 못 찾았다: {sorted(rows[0])}")
        self._seq = str(seq)
        return self._seq

    def _headers(self, account: bool = False) -> dict:
        h = {"Authorization": f"Bearer {self.token()}"}
        if account:
            h["X-Tossinvest-Account"] = self.account_seq()
        return h

    def holdings(self) -> dict:
        r = self.session.get(f"{C.TOSS_BASE}/api/v1/holdings",
                             headers=self._headers(account=True), timeout=10)
        r.raise_for_status()
        return r.json()

    def positions(self) -> list[dict]:
        body = self.holdings()
        rows = _first_list(body, "items", "holdings", "result", "positions")
        out = []
        for row in rows:
            sym = str(row.get("symbol") or row.get("stockCode") or "").strip()
            qty = _num(row.get("quantity", row.get("qty")))
            if not sym or qty <= 0:
                continue
            out.append({"symbol": sym, "qty": round_qty(sym, qty),
                        "avg": _num(row.get("averagePurchasePrice",
                                            row.get("avgPrice", row.get("purchasePrice"))))})
        return out

    def cash(self) -> float:
        r = self.session.get(f"{C.TOSS_BASE}/api/v1/buying-power",
                             headers=self._headers(account=True),
                             params={"currency": "USD"}, timeout=10)
        r.raise_for_status()
        body = r.json()
        node = body.get("result", body)
        return _num(node.get("cashBuyingPower", node.get("cash", node.get("amount"))))

    @staticmethod
    def order_body(symbol: str, side: str, qty: float, price: float) -> dict:
        """토스가 받는 주문 본문. 소수점 규칙이 매수·매도에서 서로 다르다.

        - 정수 수량      → 지정가(LIMIT) + quantity. 가격을 통제할 수 있으니 이쪽이 낫다.
        - 소수점 매도    → 시장가(MARKET) + quantity (6자리까지)
        - 소수점 매수    → 시장가(MARKET) + orderAmount (달러 금액). quantity 로 보내면
                          400 invalid-request 다. 정규장 밖이면 422 가 온다.
        """
        body = {"symbol": symbol, "side": side, "clientOrderId": uuid.uuid4().hex[:32]}
        if float(qty).is_integer():
            return {**body, "orderType": "LIMIT", "price": _us_limit_price(price),
                    "quantity": str(int(qty))}
        if side == "SELL":
            return {**body, "orderType": "MARKET", "quantity": f"{qty:.6f}"}
        # ponytail: 체결 수량은 시장가라 요청과 다르다. 다음 sync() 가 장부를 맞춘다.
        return {**body, "orderType": "MARKET", "orderAmount": _usd_amount(qty, price)}

    def send(self, symbol: str, side: str, qty: float, price: float) -> dict:
        if not C.LIVE_TRADING:
            return {"skipped": "AI_TRADER_LIVE != 1", "venue": self.name,
                    "symbol": symbol, "side": side, "qty": qty}
        r = self.session.post(
            f"{C.TOSS_BASE}/api/v1/orders",
            headers={**self._headers(account=True), "Content-Type": "application/json"},
            json=self.order_body(symbol, side, qty, price),
            timeout=10)
        if r.status_code >= 400:
            raise Rejected(f"toss {r.status_code}: {r.text[:200]}")
        return r.json()

    def buy_amount(self, symbol: str, amount_usd: float) -> dict:
        """해외주식을 달러 금액으로 시장가 매수한다. 소액 실계좌 검증에도 같은 경로를 쓴다."""
        amount = Decimal(str(amount_usd)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if amount <= 0:
            raise Rejected("order amount must be positive")
        if not C.LIVE_TRADING:
            return {"skipped": "AI_TRADER_LIVE != 1", "venue": self.name,
                    "symbol": symbol, "side": "BUY", "orderAmount": format(amount, "f")}
        body = {"symbol": symbol, "side": "BUY", "orderType": "MARKET",
                "orderAmount": format(amount, "f"), "clientOrderId": uuid.uuid4().hex[:32]}
        r = self.session.post(
            f"{C.TOSS_BASE}/api/v1/orders",
            headers={**self._headers(account=True), "Content-Type": "application/json"},
            json=body, timeout=10)
        if r.status_code >= 400:
            raise Rejected(f"toss {r.status_code}: {r.text[:200]}")
        return r.json()

    def order_detail(self, order_id: str) -> dict:
        """주문 상태와 실제 체결 수량을 조회한다."""
        r = self.session.get(f"{C.TOSS_BASE}/api/v1/orders/{order_id}",
                             headers=self._headers(account=True), timeout=10)
        if r.status_code >= 400:
            raise Rejected(f"toss {r.status_code}: {r.text[:200]}")
        return r.json()


class KiwoomVenue(Venue):
    """키움 REST — 국내주식 주문. 토스와 달리 모의투자 서버(mockapi)가 따로 있다.

    demo 주문은 돈이 걸리지 않으므로 AI_TRADER_LIVE 없이도 나간다.
    real 은 토스와 같은 규칙이다: AI_TRADER_LIVE=1 이어야만 나간다.
    """
    market = "KR"
    name = "키움증권"
    CAN_CONFIRM = True

    def __init__(self):
        from .kiwoom import Kiwoom
        self.api = Kiwoom()
        self.name = f"키움증권({self.api.mode})"

    @staticmethod
    def unavailable() -> str:
        if C.have_kiwoom_keys():
            return ""
        return (f"KIWOOM_MODE={C.KIWOOM_MODE} 에 맞는 "
                f"APP_KEY{C._KW_SUFFIX}/APP_SECRET{C._KW_SUFFIX} 없음")

    def positions(self) -> list[dict]:
        return self._positions_from(self.api.balance())

    @staticmethod
    def _positions_from(bal: dict) -> list[dict]:
        """잔고 응답 → 보유 종목. 파싱은 kiwoom.balance_rows 한 곳에서만 한다.

        symbol/qty/avg 는 RoutedBroker.sync 가 장부를 맞출 때 쓰고, 나머지(현재가·평가금액·
        평가손익)는 화면이 쓴다. 증권사가 안 준 값은 None 으로 남는다 — 매입가에서
        되계산해 채우면 손익이 영원히 0 으로 보인다.
        """
        from .kiwoom import balance_rows
        return [{**row, "qty": round_qty(row["symbol"], row["qty"])}
                for row in balance_rows(bal)]

    def orders(self) -> list[dict]:
        """오늘 주문 현황(미체결+체결). 주문이 원장에 닿았는지 화면에서 확인하는 용도."""
        return self.api.orders()

    def summary(self) -> dict:
        """대시보드용 모의계좌 요약. 같은 잔고 TR을 중복 호출하지 않는다."""
        from .kiwoom import balance_total
        bal = self.api.balance()
        dep = self.api.deposit()
        cash = _avail(dep)
        tot = balance_total(bal)
        # 추정예탁자산이 비면 예수금+평가금액으로 대신한다. 둘 다 없으면 지어내지 않는다.
        assets = tot["assets"] or ((cash + tot["eval_amount"])
                                   if tot["eval_amount"] is not None else None)
        return {"cash": cash, "equity": assets,
                "pnl": tot["pnl"], "return_pct": tot["return_pct"],
                "invested": tot["invested"], "eval_amount": tot["eval_amount"],
                "positions": self._positions_from(bal)}

    def cash(self) -> float:
        dep = self.api.deposit()
        node = dep if isinstance(dep, dict) else {}
        return _avail(node)

    # 접수 직후 원장에 체결이 찍히기까지는 시차가 있다. 지정가는 몇 초로 안 끝나므로
    # 여기서 오래 기다리지 않는다 — 안 찍혔으면 미체결로 남기고 다음 사이클이 대조한다.
    CONFIRM_TRIES, CONFIRM_WAIT = 3, 0.7

    def confirm(self, sent: dict, symbol: str, want_qty: float,
                want_price: float) -> dict:
        """주문번호로 실제 체결 수량·가격을 읽는다. 모르면 UNKNOWN 이라고 말한다.

        이것이 없으면 접수 응답만 보고 장부를 움직인다 — 지정가가 호가에 닿지도
        않았는데 보유가 늘고 현금이 빠지고 저널에 FILLED 가 찍혔다. 그 뒤의 모든
        숫자(주문가능금액·손절선·20% 상한·당일 손익)가 거짓 위에서 계산된다.
        """
        ord_no = str(sent.get("ord_no") or "").strip()
        if not ord_no:
            return {"filled_qty": 0.0, "fill_price": want_price, "open_qty": want_qty,
                    "state": "UNKNOWN", "detail": "주문번호가 안 왔다"}
        last = None
        for i in range(self.CONFIRM_TRIES):
            try:
                rows = [r for r in self.api.orders(symbol) if r["order_no"] == ord_no]
            except Exception as exc:
                last = f"{type(exc).__name__}: {str(exc)[:80]}"
                rows = []
            if rows:
                r = rows[0]
                filled = float(r.get("filled_qty") or 0)
                open_qty = float(r.get("open_qty") or 0)
                # 체결가가 비면 체결수량도 못 믿는다 — 지어내지 않는다.
                px = r.get("filled_price") or (want_price if filled else None)
                if filled > 0 and not px:
                    return {"filled_qty": 0.0, "fill_price": want_price,
                            "open_qty": filled + open_qty, "state": "UNKNOWN",
                            "detail": f"체결 {filled} 인데 체결가가 없다"}
                state = ("FILLED" if filled > 0 and open_qty == 0 else
                         "PARTIALLY_FILLED" if filled > 0 else "ACCEPTED")
                return {"filled_qty": filled, "fill_price": float(px or want_price),
                        "open_qty": open_qty, "state": state,
                        "detail": r.get("state") or ""}
            if i < self.CONFIRM_TRIES - 1:
                time.sleep(self.CONFIRM_WAIT)
        # 주문번호는 받았는데 목록에 없다. 접수는 됐으니 미체결로 본다 — 재주문 금지.
        return {"filled_qty": 0.0, "fill_price": want_price, "open_qty": want_qty,
                "state": "ACCEPTED" if last is None else "UNKNOWN",
                "detail": last or "원장 주문목록에 아직 안 보인다"}

    def send(self, symbol: str, side: str, qty: int, price: float) -> dict:
        if self.api.mode == "real" and not C.LIVE_TRADING:
            return {"skipped": "AI_TRADER_LIVE != 1", "venue": self.name,
                    "symbol": symbol, "side": side, "qty": qty}
        from .kiwoom import snap_price
        try:
            return self.api.order(symbol, side, qty, snap_price(price))
        except Exception as exc:  # 원장 거절로 랩을 세우지 않는다 — 기록하고 다음 판단으로
            raise Rejected(f"kiwoom: {exc}") from None


VENUES = {"KR": KiwoomVenue, "US": TossVenue}


def _settle(broker, venue, sent, symbol, sleeve, side, want_qty, want_price) -> dict:
    """창구에 실제 체결을 물어 **그만큼만** 장부에 넣는다.

    전에는 접수 응답을 받자마자 요청 수량 전부를 관측가로 장부에 넣었다. 지정가가
    호가에 닿지도 않았는데 보유가 늘고 현금이 빠지고 저널에 FILLED 가 찍혔다 —
    그 뒤의 주문가능금액·손절선·20% 상한·당일 손익이 전부 거짓 위에서 계산됐다.

    확인을 지원하지 않는 창구(토스)는 예전처럼 요청대로 넣되 state='ASSUMED' 로
    남긴다. 동작을 바꾸지 않으면서, 확인하지 않은 것을 확인했다고 적지는 않는다.
    """
    if venue is None:
        # 로컬 모의원장은 우리가 체결시키므로 즉시체결이 사실이다.
        actual = {"filled_qty": want_qty, "fill_price": want_price, "open_qty": 0.0,
                  "state": "FILLED", "detail": "로컬 모의원장"}
    elif venue.CAN_CONFIRM:
        actual = venue.confirm(sent, symbol, want_qty, want_price)
    else:
        actual = {"filled_qty": want_qty, "fill_price": want_price, "open_qty": 0.0,
                  "state": "ASSUMED",
                  "detail": f"{venue.name} 은 체결 확인을 지원하지 않는다 — 요청대로 가정했다"}

    filled = float(actual.get("filled_qty") or 0)
    if filled <= 0:
        # 미체결은 보유가 아니다. 장부를 건드리지 않고 무엇이 됐는지만 돌려준다.
        return {"qty": 0.0, "price": actual.get("fill_price"), "cost": 0.0,
                "proceeds": 0.0, "realized_pnl": None,
                "broker": sent, "confirm": actual}

    px = float(actual.get("fill_price") or want_price)
    fill = (PaperBroker.buy(broker, symbol, sleeve, filled, px) if side == "BUY"
            else PaperBroker.sell(broker, symbol, filled, px))
    fill["broker"] = sent
    fill["confirm"] = actual
    return fill


class RoutedBroker(PaperBroker):
    """실계좌 집행. 회계·가드레일은 PaperBroker 그대로, 주문만 시장별 창구로 내려보낸다.

    한 포트폴리오가 국내와 해외를 같이 담으므로 창구는 종목마다 갈린다.
    키가 없는 시장은 조용히 넘어가지 않고 주문을 거절한다 — 안 나간 주문을
    체결된 것처럼 장부에 적으면 그 뒤 모든 숫자가 거짓말이 된다.
    """
    mode = "routed"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.venues: dict[str, Venue] = {}
        self.blocked: dict[str, str] = {}
        for market, cls in VENUES.items():
            why = cls.unavailable()
            if why:
                self.blocked[market] = why
                continue
            try:
                self.venues[market] = cls()
            except Exception as exc:
                self.blocked[market] = f"{type(exc).__name__}: {exc}"
        if not self.venues:
            raise RuntimeError("쓸 수 있는 증권사가 없다 — " +
                               " / ".join(f"{m}: {w}" for m, w in self.blocked.items()))
        self.mode = "routed:" + "+".join(
            f"{m}={v.name}" for m, v in sorted(self.venues.items()))

    def sync(self) -> dict:
        """실계좌 잔고를 내부 장부로 끌어온다. 라이브 시작 전에 반드시 한 번 맞춘다.

        내부 장부가 실계좌와 어긋나면 슬리브 회계·20% 상한·손절 -15% 가 전부 허구 위에서
        계산된다. 그래서 한 창구라도 못 읽으면 예외를 던진다 — 모른 채로 실매매하지 않는다.

        슬리브는 실계좌에 없는 개념이라 기존 장부의 배정을 유지하고, 처음 보는 종목은
        변동성을 모르므로 AGGRESSIVE 로 둔다(더 보수적인 상한이 걸리는 쪽).
        """
        known = {s: p.sleeve for s, p in self.positions.items()}
        positions, cash_by_market, failed = {}, {}, {}
        for market, venue in self.venues.items():
            try:
                rows = venue.positions()
                cash_by_market[market] = venue.cash()
            except Exception as exc:
                failed[market] = f"{type(exc).__name__}: {str(exc)[:160]}"
                continue
            for row in rows:
                sym = row["symbol"]
                if row.get("avg") is None:
                    # 매입가를 모르면 손절 -15% 도 20% 상한도 계산할 수 없다.
                    # 0 으로 채우고 넘어가면 그 종목은 영원히 수익률 +∞ 로 보인다.
                    failed[market] = f"{sym} 의 매입가를 증권사가 주지 않았다"
                    break
                sleeve = known.get(sym, "AGGRESSIVE")
                positions[sym] = Position(sym, sleeve, round_qty(sym, row["qty"]), float(row["avg"]))
        if failed:
            raise RuntimeError("실계좌 잔고를 못 읽었다 — 장부를 맞추지 못한 채로는 실매매하지 않는다: "
                               + " / ".join(f"{m}: {w}" for m, w in failed.items()))

        self.positions = positions
        total_cash = sum(c * (C.USD_KRW if m == "US" else 1)
                         for m, c in cash_by_market.items())
        # 슬리브별 현금은 증권사가 모른다. 사람이 정한 6:4 비율로 나눈다.
        self.cash = {k: total_cash * w for k, w in C.SLEEVES.items()}
        # 기준자산은 '오늘 이 계좌가 시작한 자산'이다. fresh() 가 넣어 둔 C.START_CASH 는
        # 실계좌와 아무 상관이 없는 설정값이다 — 1,000 만으로 출발한 장부가 500 만 계좌를
        # 처음 보면 거래 한 건 없이 당일 -50% 가 되어 킬스위치가 내려갔다.
        # 그래서 이 거래일에 아직 기준을 세운 적이 없으면 실제 자산으로 세운다.
        # 같은 날 재시작은 already_started 가 막는다 — 누적 손실을 0 으로 지우지 않는다.
        self.adopt_account_baseline(self.equity({s: p.avg for s, p in positions.items()}))
        return {"positions": len(positions), "cash": round(total_cash),
                "by_market": {m: round(c, 2) for m, c in cash_by_market.items()},
                "usd_krw": C.USD_KRW}

    def venue_for(self, symbol: str) -> Venue:
        market = C.market_of(symbol)
        venue = self.venues.get(market)
        if venue is None:
            raise Rejected(f"{symbol} 은 {market} 인데 창구가 없다: "
                           f"{self.blocked.get(market, '미지원 시장')}")
        return venue

    def _send(self, symbol: str, side: str, qty: float, price: float) -> dict:
        return self.venue_for(symbol).send(symbol, side, qty, price)

    def _probe(self) -> PaperBroker:
        return PaperBroker(
            cash=dict(self.cash),
            positions={s: Position(p.symbol, p.sleeve, p.qty, p.avg)
                       for s, p in self.positions.items()},
            realized=dict(self.realized), day=self.day,
            day_start_equity=self.day_start_equity, baseline_day=self.baseline_day,
            halted=self.halted)

    def buy(self, symbol: str, sleeve: str, qty: float, price: float) -> dict:
        self.venue_for(symbol)               # 창구가 없으면 장부를 건드리기 전에 막는다
        self._probe().buy(symbol, sleeve, qty, price)
        sent = self._send(symbol, "BUY", qty, price)
        if sent.get("skipped"):
            raise Rejected(str(sent["skipped"]))
        return _settle(self, self.venue_for(symbol), sent, symbol, sleeve, "BUY", qty, price)

    def sell(self, symbol: str, qty: float, price: float) -> dict:
        self.venue_for(symbol)
        probe_fill = self._probe().sell(symbol, qty, price)
        sent = self._send(symbol, "SELL", probe_fill["qty"], price)
        if sent.get("skipped"):
            raise Rejected(str(sent["skipped"]))
        return _settle(self, self.venue_for(symbol), sent, symbol, None, "SELL",
                       probe_fill["qty"], price)


class HybridMockBroker(PaperBroker):
    """국내는 키움 모의주문, 해외는 로컬 모의체결을 쓰는 브로커."""
    mode = "mock:KR=키움+US=paper"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.kr = KiwoomVenue() if C.have_kiwoom_keys() and C.KIWOOM_MODE == "demo" else None

    @classmethod
    def load(cls, path=None):
        return super().load(path or C.MOCK_STATE)

    def save(self, path=None) -> None:
        return super().save(path or C.MOCK_STATE)

    def _probe(self) -> PaperBroker:
        return PaperBroker(
            cash=dict(self.cash),
            positions={s: Position(p.symbol, p.sleeve, p.qty, p.avg)
                       for s, p in self.positions.items()},
            realized=dict(self.realized), day=self.day,
            day_start_equity=self.day_start_equity, baseline_day=self.baseline_day,
            halted=self.halted)

    def buy(self, symbol: str, sleeve: str, qty: float, price: float) -> dict:
        self._probe().buy(symbol, sleeve, qty, price)
        sent = None
        if C.market_of(symbol) == "KR":
            if self.kr is None:
                raise Rejected("키움 모의투자 창구가 없다")
            sent = self.kr.send(symbol, "BUY", qty, price)
            return _settle(self, self.kr, sent, symbol, sleeve, "BUY", qty, price)
        return _settle(self, None, {"venue": "해외 로컬 모의원장", "paper": True},
                       symbol, sleeve, "BUY", qty, price)

    def sell(self, symbol: str, qty: float, price: float) -> dict:
        probe_fill = self._probe().sell(symbol, qty, price)
        sent = None
        if C.market_of(symbol) == "KR":
            if self.kr is None:
                raise Rejected("키움 모의투자 창구가 없다")
            sent = self.kr.send(symbol, "SELL", probe_fill["qty"], price)
            return _settle(self, self.kr, sent, symbol, None, "SELL",
                           probe_fill["qty"], price)
        return _settle(self, None, {"venue": "해외 로컬 모의원장", "paper": True},
                       symbol, None, "SELL", probe_fill["qty"], price)


def make_broker(paper: bool = True, mock: bool = False):
    """paper=True 면 자체 장부, False 면 시장별로 실제 증권사에 낸다."""
    if mock:
        return HybridMockBroker.load()
    if paper:
        return PaperBroker.load()
    return RoutedBroker.load()
