"""키움증권 REST API 클라이언트 — 국내주식 주문용. 해외 주문은 토스 담당.

스펙 출처: github.com/Kiwoom-Securities/Kiwoom-REST-API (공식 샘플코드)
  운영     https://api.kiwoom.com      / wss://api.kiwoom.com:10000
  모의투자 https://mockapi.kiwoom.com  / wss://mockapi.kiwoom.com:10000

환경변수 (키움 공식 .env.example 과 동일한 이름):
  KIWOOM_MODE=demo|real
  APP_KEY / APP_SECRET            (운영)
  APP_KEY_MOCK / APP_SECRET_MOCK  (모의투자)
"""
import gzip, hashlib, json, os, threading, time, urllib.error, urllib.request
from datetime import datetime

from . import config as C

HOSTS = {"real": "https://api.kiwoom.com", "demo": "https://mockapi.kiwoom.com"}

# 발급받은 토큰을 프로세스 사이에서 나눠 쓴다. 자격증명이므로 .gitignore 에 올려 뒀다.
TOKEN_CACHE = C.LAB / "kiwoom_token.json"

# 키움은 TR 마다 유량 제한을 건다. 대시보드 새로고침과 매매 사이클이 겹치면 429 가 나고,
# 그 순간 계좌·시세·주문이 한꺼번에 죽는다. 그래서 모든 요청은 이 문 하나를 지나간다.
MIN_GAP = float(os.getenv("AI_TRADER_KIWOOM_GAP", "0.4"))   # 초
RETRIES = int(os.getenv("AI_TRADER_KIWOOM_RETRIES", "3"))
_gate = threading.Lock()
_last_call = 0.0


def _wait_turn():
    """직전 요청과 최소 MIN_GAP 초를 띄운다. 스레드가 여럿이어도 줄은 하나다."""
    global _last_call
    with _gate:
        gap = MIN_GAP - (time.monotonic() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.monotonic()

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
        self.mode = (mode or C.KIWOOM_MODE).lower()
        if self.mode not in HOSTS:
            raise ValueError(f"mode must be real/demo: {self.mode}")
        suffix = "" if self.mode == "real" else "_MOCK"
        self.key = app_key or (C.KIWOOM_KEY if self.mode == C.KIWOOM_MODE
                               else os.environ.get(f"APP_KEY{suffix}", ""))
        self.secret = app_secret or (C.KIWOOM_SECRET if self.mode == C.KIWOOM_MODE
                                     else os.environ.get(f"APP_SECRET{suffix}", ""))
        if not self.key or not self.secret:
            raise ValueError(f"APP_KEY{suffix}/APP_SECRET{suffix} 없음")
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
        data = json.dumps(body).encode()

        for attempt in range(RETRIES + 1):
            _wait_turn()
            req = urllib.request.Request(self.host + path, data=data,
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    out = json.loads(_body(r) or b"{}")
                    out["_cont_yn"] = r.headers.get("cont-yn")
                    out["_next_key"] = r.headers.get("next-key")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < RETRIES:
                    time.sleep(1.0 * 2 ** attempt)   # 유량 초과는 기다리면 풀린다
                    continue
                raise KiwoomError(e.code, _body(e).decode(errors="replace"),
                                  api_id or path) from None
        if out.get("return_code") not in (None, 0):
            raise KiwoomError(out.get("return_code"), out.get("return_msg", ""), api_id or path)
        return out

    def _cache_id(self):
        """토큰 캐시의 키. 앱키를 그대로 적지 않으려고 해시 앞부분만 쓴다."""
        digest = hashlib.sha256(self.key.encode()).hexdigest()[:12]
        return f"{self.mode}:{digest}"

    def token(self):
        if self._token and time.time() < self._expires_at:
            return self._token
        # 키움은 토큰 발급(au10001)에도 유량 제한을 건다. 대시보드와 매매 루프는 서로 다른
        # 프로세스라 각자 발급받으면 금세 429 가 나고, 그 순간 계좌·시세가 통째로 죽는다.
        # 그래서 토큰은 파일로 나눠 쓴다 — 유효한 동안은 아무도 다시 발급받지 않는다.
        hit = _load_token(self._cache_id())
        if hit:
            self._token, self._expires_at = hit
            return self._token
        r = self._post("/oauth2/token", {"grant_type": "client_credentials",
                                         "appkey": self.key, "secretkey": self.secret}, auth=False)
        self._token = r["token"]
        # expires_dt 는 'YYYYMMDDHHMMSS'. 파싱 실패해도 동작하도록 보수적으로 짧게 잡는다.
        self._expires_at = _parse_expiry(r.get("expires_dt")) or (time.time() + 3600)
        _save_token(self._cache_id(), self._token, self._expires_at)
        return self._token

    # --- reads -------------------------------------------------------------
    def deposit(self, qry_tp="2"):
        """예수금상세현황 [kt00001]. qry_tp 2=일반조회"""
        return self._post(ACNT, {"qry_tp": qry_tp}, api_id="kt00001")

    def balance(self, qry_tp="1"):
        """계좌평가잔고내역 [kt00018]. qry_tp 1=합산"""
        return self._post(ACNT, {"qry_tp": qry_tp, "dmst_stex_tp": self.exchange}, api_id="kt00018")

    def unfilled(self, symbol=None):
        """미체결 주문 [ka10075]. 목록은 'oso' 키에 들어온다."""
        body = {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0"}
        if symbol:
            body["stk_cd"] = symbol
        return self._post(ACNT, body, api_id="ka10075")

    def fills(self, symbol=None, qry_tp="0"):
        """체결 [ka10076]. 목록은 'cntr' 키에 들어온다."""
        body = {"qry_tp": qry_tp, "sell_tp": "0", "stex_tp": "0"}
        if symbol:
            body["stk_cd"] = symbol
        return self._post(ACNT, body, api_id="ka10076")

    def orders(self, symbol=None):
        """오늘 주문 현황 — 미체결과 체결을 주문번호로 합친 하나의 목록.

        모의투자 중 "주문이 진짜 나갔나, 체결됐나"를 보는 화면이 쓴다. 같은 주문이 양쪽에
        다 나오면 체결 쪽을 남긴다 — 체결수량과 체결가가 거기 있다.
        """
        merged = {}
        for row in order_rows(self.unfilled(symbol)) + order_rows(self.fills(symbol)):
            merged[row["order_no"] or f"{row['symbol']}:{row['time']}"] = row
        return sorted(merged.values(), key=lambda r: r["time"] or "", reverse=True)

    def quote(self, symbol):
        """주식호가 [ka10004]"""
        return self._post(MRKCOND, {"stk_cd": symbol}, api_id="ka10004")

    def candles(self, symbol, count=200):
        """주식일봉차트 [ka10081]. 과거→최신 순의 공통 캔들로 돌려준다.

        upd_stkpc_tp=1 은 수정주가다. 액면분할·유상증자를 반영하지 않으면 과거 지표가 튄다.
        응답은 최신→과거 순이고 부호·콤마가 붙어 온다 — 정규화는 indicators.from_kiwoom 이 한다.

        base_dt 는 필수다. 빈 문자열을 보내면 원장이 1511(필수 입력 값 없음)로 거절한다 —
        키움 국내 시세가 통째로 죽으므로 기준일은 항상 채워 보낸다.

        기준일은 서버 시계가 아니라 KST 로 잡는다. 국장의 '오늘'은 한국 날짜다 —
        UTC 로 도는 서버(AWS 기본값)에서는 새벽에 전날을 보내게 되고, 그러면
        그날 봉이 통째로 빠진다.
        """
        from .data import KST
        r = self._post(CHART, {"stk_cd": symbol,
                               "base_dt": datetime.now(KST).strftime("%Y%m%d"),
                               "upd_stkpc_tp": "1"}, api_id="ka10081")
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


# --- 응답 정규화 -----------------------------------------------------------
# 키움은 숫자를 '000000010000000' 처럼 0 으로 채우고, 부호와 콤마를 붙여 보낸다.
# 그 문자열을 그대로 화면에 올리거나 0 으로 뭉개면 계좌 내역이 거짓말이 된다.

def num(v, signed=False):
    """키움 숫자 문자열 → float. 못 읽으면 0 이 아니라 None.

    0원과 '못 읽었다'는 다른 말이다. 없는 값을 0 으로 채우면 화면이 "손익 0원"이라고
    단언하게 되는데, 그건 우리가 모르는 것을 안다고 적는 것이다.

    signed=False 는 가격·수량·금액용이다. 키움은 현재가에 전일 대비 방향을 뜻하는
    '+'/'-' 를 붙이므로 부호를 그대로 쓰면 가격이 음수가 된다. 평가손익·수익률만
    signed=True 로 부호를 살린다.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if signed else abs(f)


def _rows(body, *keys):
    """응답에서 목록이 들어 있는 첫 키. TR 마다 감싸는 이름이 다르다."""
    for k in keys:
        v = (body or {}).get(k)
        if isinstance(v, list):
            return v
    return []


def _text(v):
    return (str(v).strip() or None) if v is not None else None


def balance_rows(body):
    """kt00018(계좌평가잔고내역) → 보유 종목. 증권사가 준 값만 담는다.

    현재가·평가금액·평가손익을 매입가에서 되계산하지 않는다 — 그건 우리 추정이지
    계좌 내역이 아니다.
    """
    out = []
    for r in _rows(body, "acnt_evlt_remn_indv_tot", "output", "result"):
        symbol = str(r.get("stk_cd") or "").strip().lstrip("A")
        qty = num(r.get("rmnd_qty"))
        if not symbol or not qty:
            continue
        out.append({
            "symbol": symbol,
            "name": _text(r.get("stk_nm")),
            "qty": qty,
            "sellable": num(r.get("trde_able_qty")),   # 매매가능수량
            "avg": num(r.get("pur_pric")),             # 매입가
            "last": num(r.get("cur_prc")),             # 현재가
            "invested": num(r.get("pur_amt")),         # 매입금액
            "value": num(r.get("evlt_amt")),           # 평가금액
            "pnl": num(r.get("evltv_prft"), signed=True),   # 평가손익
            "pnl_pct": num(r.get("prft_rt"), signed=True),  # 수익률
            "weight": num(r.get("poss_rt")),           # 보유비중
        })
    return out


def balance_total(body):
    """kt00018 의 계좌 합계. prsm_dpst_aset_amt 는 추정예탁자산이다."""
    return {
        "invested": num(body.get("tot_pur_amt")),
        "eval_amount": num(body.get("tot_evlt_amt")),
        "pnl": num(body.get("tot_evlt_pl"), signed=True),
        "return_pct": num(body.get("tot_prft_rt"), signed=True),
        "assets": num(body.get("prsm_dpst_aset_amt")),
    }


def order_rows(body):
    """ka10075(미체결)·ka10076(체결) → 공통 주문 행.

    두 TR 은 목록 키(oso / cntr)만 다르고 행의 필드 이름은 같다 — 한 함수로 읽는다.
    """
    out = []
    for r in _rows(body, "oso", "cntr", "output"):
        out.append({
            "order_no": _text(r.get("ord_no")),
            "orig_order_no": _text(r.get("orig_ord_no")),
            "symbol": (str(r.get("stk_cd") or "").strip().lstrip("A") or None),
            "name": _text(r.get("stk_nm")),
            "side": _text(r.get("io_tp_nm")),      # 매수 / 매도
            "trade_type": _text(r.get("trde_tp")),  # 지정가 / 시장가 …
            "state": _text(r.get("ord_stt")),       # 접수 / 확인 / 체결 …
            "order_qty": num(r.get("ord_qty")),
            "order_price": num(r.get("ord_pric")),
            "filled_qty": num(r.get("cntr_qty")),
            "filled_price": num(r.get("cntr_pric")),
            "open_qty": num(r.get("oso_qty")),      # 미체결 잔량
            "time": _text(r.get("tm") or r.get("ord_tm")),
        })
    return out


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


def _load_token(cache_id):
    """아직 살아 있는 토큰이면 (토큰, 만료시각). 없거나 만료면 None."""
    try:
        saved = json.loads(TOKEN_CACHE.read_text(encoding="utf-8")).get(cache_id) or {}
        if saved.get("token") and time.time() < float(saved.get("expires_at") or 0):
            return saved["token"], float(saved["expires_at"])
    except Exception:
        pass       # 캐시가 깨졌으면 그냥 새로 받는다 — 여기서 랩을 세울 이유가 없다
    return None


def _save_token(cache_id, token, expires_at):
    """다른 프로세스가 읽다가 반쪽 파일을 보지 않도록 임시 파일에 쓰고 바꿔 끼운다."""
    try:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        try:
            all_tokens = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        except Exception:
            all_tokens = {}
        all_tokens[cache_id] = {"token": token, "expires_at": expires_at}
        tmp = TOKEN_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_tokens), encoding="utf-8")
        os.chmod(tmp, 0o600)     # 계좌를 열 수 있는 자격증명이다
        os.replace(tmp, TOKEN_CACHE)
    except Exception:
        pass       # 캐시에 못 써도 매매는 계속된다. 다음 번에 한 번 더 발급받을 뿐이다


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

    # --- 응답 정규화. 키움이 실제로 주는 모양(0 채움·부호·콤마)을 그대로 넣는다. ---
    assert num("000000010000000") == 10_000_000
    assert num("+000000071,500") == 71_500          # 가격의 '+' 는 전일 대비 방향이다
    assert num("-000000015000", signed=True) == -15_000
    assert num("-000000002.14", signed=True) == -2.14
    assert num("") is None and num(None) is None and num("abc") is None
    assert num("0") == 0.0                          # 0 은 값이다 — None 이 아니다

    bal = {
        "tot_pur_amt": "000000000700000", "tot_evlt_amt": "000000000715000",
        "tot_evlt_pl": "-000000000015000", "tot_prft_rt": "-000000002.14",
        "prsm_dpst_aset_amt": "000000010715000",
        "acnt_evlt_remn_indv_tot": [
            {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "0000000010",
             "trde_able_qty": "0000000010", "pur_pric": "000000070000",
             "cur_prc": "+000000071500", "pur_amt": "000000000700000",
             "evlt_amt": "000000000715000", "evltv_prft": "-000000000015000",
             "prft_rt": "-000000002.14", "poss_rt": "000000006.67"},
            {"stk_cd": "A000660", "rmnd_qty": "0000000000"},   # 수량 0 은 보유가 아니다
        ],
    }
    rows = balance_rows(bal)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["symbol"] == "005930" and r["name"] == "삼성전자"     # 'A' 접두어를 뗀다
    assert r["qty"] == 10 and r["avg"] == 70_000 and r["last"] == 71_500
    assert r["value"] == 715_000 and r["invested"] == 700_000
    assert r["pnl"] == -15_000 and r["pnl_pct"] == -2.14          # 손실은 음수로 남는다
    tot = balance_total(bal)
    assert tot["eval_amount"] == 715_000 and tot["pnl"] == -15_000
    assert tot["return_pct"] == -2.14 and tot["assets"] == 10_715_000

    # 안 준 필드는 0 이 아니라 None 이다 — 화면이 "0원"이라고 단언하면 안 된다.
    bare = balance_rows({"acnt_evlt_remn_indv_tot": [{"stk_cd": "005930", "rmnd_qty": "5"}]})
    assert bare[0]["qty"] == 5 and bare[0]["last"] is None and bare[0]["pnl"] is None

    assert balance_rows({}) == [] and order_rows({}) == []

    # 미체결(oso)과 체결(cntr)은 목록 키만 다르고 행 필드는 같다.
    oso = order_rows({"oso": [{"ord_no": "0000123", "stk_cd": "A005930", "stk_nm": "삼성전자",
                               "io_tp_nm": "매수", "ord_qty": "10", "ord_pric": "000070000",
                               "cntr_qty": "0", "oso_qty": "10", "ord_stt": "접수", "tm": "090012"}]})
    assert oso[0]["order_no"] == "0000123" and oso[0]["symbol"] == "005930"
    assert oso[0]["open_qty"] == 10 and oso[0]["filled_qty"] == 0 and oso[0]["side"] == "매수"
    cntr = order_rows({"cntr": [{"ord_no": "0000123", "cntr_qty": "10",
                                 "cntr_pric": "000070000", "oso_qty": "0", "ord_stt": "체결"}]})
    assert cntr[0]["filled_qty"] == 10 and cntr[0]["filled_price"] == 70_000
    print("kiwoom selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
