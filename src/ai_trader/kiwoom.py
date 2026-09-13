"""키움증권 REST API 클라이언트 — 국내주식 주문용. 해외 주문은 토스 담당.

스펙 출처: github.com/Kiwoom-Securities/Kiwoom-REST-API (공식 샘플코드)
  운영     https://api.kiwoom.com      / wss://api.kiwoom.com:10000
  모의투자 https://mockapi.kiwoom.com  / wss://mockapi.kiwoom.com:10000

환경변수 (키움 공식 .env.example 과 동일한 이름):
  KIWOOM_MODE=demo|real
  APP_KEY / APP_SECRET            (운영)
  APP_KEY_MOCK / APP_SECRET_MOCK  (모의투자)
"""
import gzip, json, os, time, urllib.error, urllib.request

HOSTS = {"real": "https://api.kiwoom.com", "demo": "https://mockapi.kiwoom.com"}

# 국내주식 주문 TR
BUY, SELL, MODIFY, CANCEL = "kt10000", "kt10001", "kt10002", "kt10003"
ORDR = "/api/dostk/ordr"
ACNT = "/api/dostk/acnt"
MRKCOND = "/api/dostk/mrkcond"
CHART = "/api/dostk/chart"

# 매매구분 (trde_tp) — 자주 쓰는 것만. 전체 목록은 키움 문서 참조.
TRDE = {"지정가": "0", "시장가": "3", "조건부지정가": "5", "최유리": "6", "최우선": "7",
        "장전시간외": "61", "시간외단일가": "62", "장후시간외": "81",
        "IOC지정가": "10", "IOC시장가": "13", "FOK지정가": "20", "FOK시장가": "23"}


def _body(r):
    """게이트웨이가 gzip 으로 내려주면 그냥 읽을 때 깨진다. 에러 본문도 마찬가지."""
    raw = r.read()
    return gzip.decompress(raw) if r.headers.get("Content-Encoding") == "gzip" else raw


class KiwoomError(RuntimeError):
    def __init__(self, code, msg, api_id=""):
        self.code, self.msg, self.api_id = code, msg, api_id
        super().__init__(f"[{api_id}] return_code={code} {msg}")


class Kiwoom:
    def __init__(self, app_key=None, app_secret=None, mode=None, exchange="KRX"):
        self.mode = (mode or os.environ.get("KIWOOM_MODE", "demo")).lower()
        if self.mode not in HOSTS:
            raise ValueError(f"mode must be real/demo: {self.mode}")
        suffix = "" if self.mode == "real" else "_MOCK"
        self.key = app_key or os.environ[f"APP_KEY{suffix}"]
        self.secret = app_secret or os.environ[f"APP_SECRET{suffix}"]
        self.host = HOSTS[self.mode]
        self.exchange = exchange          # KRX | NXT | SOR
        self._token = None
        self._expires_at = 0

    # --- transport ---------------------------------------------------------
    def _post(self, path, body, *, api_id=None, cont_yn=None, next_key=None, auth=True):
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        if auth:
            headers["authorization"] = f"Bearer {self.token()}"
        if api_id:
            headers["api-id"] = api_id
        if cont_yn:
            headers["cont-yn"] = cont_yn
        if next_key:
            headers["next-key"] = next_key
        req = urllib.request.Request(self.host + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                out = json.loads(_body(r) or b"{}")
                out["_cont_yn"] = r.headers.get("cont-yn")
                out["_next_key"] = r.headers.get("next-key")
        except urllib.error.HTTPError as e:
            raise KiwoomError(e.code, _body(e).decode(errors="replace"), api_id or path) from None
        if out.get("return_code") not in (None, 0):
            raise KiwoomError(out.get("return_code"), out.get("return_msg", ""), api_id or path)
        return out

    def token(self):
        if self._token and time.time() < self._expires_at:
            return self._token
        r = self._post("/oauth2/token", {"grant_type": "client_credentials",
                                         "appkey": self.key, "secretkey": self.secret}, auth=False)
        self._token = r["token"]
        # expires_dt 는 'YYYYMMDDHHMMSS'. 파싱 실패해도 동작하도록 보수적으로 짧게 잡는다.
        self._expires_at = _parse_expiry(r.get("expires_dt")) or (time.time() + 3600)
        return self._token

    # --- reads -------------------------------------------------------------
    def deposit(self, qry_tp="2"):
        """예수금상세현황 [kt00001]. qry_tp 2=일반조회"""
        return self._post(ACNT, {"qry_tp": qry_tp}, api_id="kt00001")

    def balance(self, qry_tp="1"):
        """계좌평가잔고내역 [kt00018]. qry_tp 1=합산"""
        return self._post(ACNT, {"qry_tp": qry_tp, "dmst_stex_tp": self.exchange}, api_id="kt00018")

    def unfilled(self, symbol=None):
        """미체결 주문 [ka10075]"""
        body = {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0"}
        if symbol:
            body["stk_cd"] = symbol
        return self._post(ACNT, body, api_id="ka10075")

    def quote(self, symbol):
        """주식호가 [ka10004]"""
        return self._post(MRKCOND, {"stk_cd": symbol}, api_id="ka10004")

    def candles(self, symbol, count=200):
        """주식일봉차트 [ka10081]. 과거→최신 순의 공통 캔들로 돌려준다.

        upd_stkpc_tp=1 은 수정주가다. 액면분할·유상증자를 반영하지 않으면 과거 지표가 튄다.
        응답은 최신→과거 순이고 부호·콤마가 붙어 온다 — 정규화는 indicators.from_kiwoom 이 한다.
        """
        r = self._post(CHART, {"stk_cd": symbol, "base_dt": "", "upd_stkpc_tp": "1"},
                       api_id="ka10081")
        rows = r.get("stk_dt_pole_chart_qry") or r.get("output") or []
        from .indicators import from_kiwoom
        return from_kiwoom(rows)[-count:]

    # --- writes ------------------------------------------------------------
    def order(self, symbol, side, quantity, price=None, *, trde_tp=None, cond_uv=""):
        """매수/매도 주문. price 를 주면 지정가, 생략하면 시장가."""
        trde_tp = trde_tp or ("0" if price is not None else "3")
        self._validate(symbol, quantity, price, trde_tp)
        body = {"dmst_stex_tp": self.exchange, "stk_cd": symbol,
                "ord_qty": str(quantity), "trde_tp": trde_tp,
                "ord_uv": "" if price is None else str(price), "cond_uv": cond_uv}
        return self._post(ORDR, body, api_id=BUY if side == "BUY" else SELL)

    def cancel(self, orig_ord_no, symbol, quantity=0):
        """취소주문 [kt10003]. quantity=0 이면 잔량 전부 취소."""
        return self._post(ORDR, {"dmst_stex_tp": self.exchange, "orig_ord_no": str(orig_ord_no),
                                 "stk_cd": symbol, "cncl_qty": str(quantity)}, api_id=CANCEL)

    @staticmethod
    def _validate(symbol, quantity, price, trde_tp):
        """원장에서 거절될 조합을 요청 전에 차단한다."""
        if not (symbol and len(symbol) >= 6):
            raise ValueError(f"종목코드는 6자리: {symbol}")
        q = float(quantity)
        if q <= 0 or q != int(q):
            raise ValueError(f"국내주식은 양의 정수 수량만 가능: {quantity}")
        market_tp = trde_tp in ("3", "13", "23")
        if market_tp and price is not None:
            raise ValueError("시장가 주문에 가격 전달 불가")
        if not market_tp and price is None:
            raise ValueError(f"지정가 계열 주문(trde_tp={trde_tp})은 price 필수")
        if price is not None:
            p = float(price)
            if p <= 0 or p != int(p):
                raise ValueError(f"국내주식 주문가는 양의 정수(원): {price}")
            if int(p) % tick_size(int(p)) != 0:
                raise ValueError(f"호가단위 불일치: {int(p)}원은 {tick_size(int(p))}원 단위여야 함")


def tick_size(price, market="KOSPI"):
    """KRX 호가단위 (2023-01 개편 기준).
    ponytail: KOSDAQ 은 5만원 이상 100원 상한. 종목별 시장 구분은 호출부가 넘긴다.
    """
    for limit, tick in ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
                        (200_000, 100), (500_000, 500)):
        if price < limit:
            return tick
    return 100 if market == "KOSDAQ" else 1_000


def snap_price(price, market="KOSPI"):
    """호가단위에 맞춰 내림. 주문가를 말없이 바꾸지 않으려고 order() 와 분리해 둔다."""
    t = tick_size(int(price), market)
    return int(price) // t * t


def _parse_expiry(expires_dt):
    """'YYYYMMDDHHMMSS' → epoch, 만료 60초 전. 파싱 실패 시 None."""
    if not expires_dt or len(str(expires_dt)) != 14:
        return None
    try:
        return time.mktime(time.strptime(str(expires_dt), "%Y%m%d%H%M%S")) - 60
    except ValueError:
        return None


def _selfcheck():
    v = Kiwoom._validate
    v("005930", 10, 70000, "0")        # 지정가 (70,000원 → 100원 단위 OK)
    v("005930", 10, None, "3")         # 시장가
    v("005930", 1, 1999, "0")          # 1원 단위 구간

    def rejects(msg, *a):
        try:
            v(*a)
        except ValueError:
            return
        raise AssertionError(f"거절했어야 함: {msg}")

    rejects("시장가에 가격",   "005930", 10, 70000, "3")
    rejects("지정가에 가격없음", "005930", 10, None, "0")
    rejects("소수점 수량",     "005930", 0.5, 70000, "0")
    rejects("수량 0",          "005930", 0, 70000, "0")
    rejects("소수점 가격",     "005930", 10, 70000.5, "0")
    rejects("호가단위 불일치", "005930", 10, 70050, "0")   # 5만~20만은 100원 단위
    rejects("짧은 종목코드",   "0059", 10, 70000, "0")

    assert tick_size(1999) == 1 and tick_size(2000) == 5
    assert tick_size(19999) == 10 and tick_size(20000) == 50
    assert tick_size(49999) == 50 and tick_size(50000) == 100
    assert tick_size(199999) == 100 and tick_size(200000) == 500
    assert tick_size(600000) == 1000 and tick_size(600000, "KOSDAQ") == 100
    assert snap_price(70050) == 70000 and snap_price(1999) == 1999
    assert _parse_expiry("20260912153000") is not None
    assert _parse_expiry("bad") is None and _parse_expiry(None) is None
    print("kiwoom selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
