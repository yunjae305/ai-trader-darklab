"""DART 전자공시 재무제표 — 상세실적 화면의 데이터원.

한 번 호출하면 3개 연도(당기·전기·전전기)가 온다. 그래서 6년을 보려면 두 번 부른다.
숫자는 원 단위로 오는 것을 억원으로 바꿔 내려보낸다 — 화면이 조 단위 숫자를 그대로
쓰면 읽을 수가 없다.

국내 종목만 있다. 해외 티커는 DART 에 없으므로 지어내지 않고 Unavailable 을 던진다.
"""
from __future__ import annotations

import io
import json
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

import requests

from . import config as C

BASE = "https://opendart.fss.or.kr/api"
CORP_CACHE = C.ROOT / "data" / "dart_corp.json"
CORP_TTL = 30 * 86400  # 상장사 고유번호는 자주 바뀌지 않는다
UNIT = 10 ** 8  # 억원
SJ = {"BS": "재무상태표", "IS": "손익계산서", "CF": "현금흐름표"}


class Unavailable(Exception):
    """데이터를 못 얻은 이유. 빈 표 대신 이유를 화면에 보낸다."""


def _key() -> str:
    if not C.DART_API_KEY:
        raise Unavailable("DART_API_KEY 없음 — .env 에 넣어라")
    return C.DART_API_KEY


def _corp_map() -> dict[str, dict]:
    """종목코드 → 고유번호. 상장사 전체가 담긴 zip 을 받아 캐시한다."""
    if CORP_CACHE.exists() and time.time() - CORP_CACHE.stat().st_mtime < CORP_TTL:
        try:
            return json.loads(CORP_CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass  # 깨진 캐시는 아래에서 다시 받는다
    try:
        r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": _key()}, timeout=60)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise Unavailable(f"DART 상장사 목록 연결 실패: {type(exc).__name__}") from None
    if r.content[:2] != b"PK":  # 키가 틀리면 zip 이 아니라 XML 에러가 온다
        try:
            error = ET.fromstring(r.content)
            detail = f"{error.findtext('status') or ''} {error.findtext('message') or ''}".strip()
        except ET.ParseError:
            detail = r.text[:120]
        raise Unavailable(f"DART 상장사 목록을 못 받았다: {detail}")
    try:
        xml = zipfile.ZipFile(io.BytesIO(r.content)).read("CORPCODE.xml")
    except (zipfile.BadZipFile, KeyError):
        raise Unavailable("DART 상장사 목록 압축파일이 손상됐다") from None
    out = {}
    for e in ET.fromstring(xml).iter("list"):
        code = (e.findtext("stock_code") or "").strip()
        if code:
            out[code] = {"corp": (e.findtext("corp_code") or "").strip(),
                         "name": (e.findtext("corp_name") or "").strip()}
    CORP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CORP_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _amount(raw) -> float | None:
    """'13,238,330,251,696' → 132383.3 (억원). 빈 칸과 '-' 는 없는 값이다."""
    s = str(raw or "").strip().replace(",", "")
    if not s or s == "-":
        return None
    neg = s.startswith("(") and s.endswith(")")  # 음수를 괄호로 적는 회사가 있다
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return round((-v if neg else v) / UNIT, 1)


def _fold(rows: list[dict], base_year: int, tables: dict, order: dict) -> set[int]:
    """한 번의 응답(3개 연도)을 누적 표에 접어 넣는다. 먼저 채운 값을 덮지 않는다.

    최신 보고서부터 역순으로 부르므로, 같은 연도가 겹치면 더 최신 공시가 이긴다.
    """
    years: set[int] = set()
    cols = (("thstrm_amount", base_year),
            ("frmtrm_amount", base_year - 1),
            ("bfefrmtrm_amount", base_year - 2))
    for r in rows:
        div = r.get("sj_div")
        # 손익계산서를 안 내고 포괄손익계산서만 내는 해가 있다 (같은 회사라도 해마다 다르다).
        # 한 응답 안에 둘 다 있으면 IS 가 먼저 와서 setdefault 로 이긴다.
        if div == "CIS":
            div = "IS"
        if div not in tables:
            continue
        account = (r.get("account_nm") or "").strip()
        if not account:
            continue
        cell = tables[div].setdefault(account, {})
        if account not in order[div]:
            order[div].append(account)
        for field, year in cols:
            value = _amount(r.get(field))
            if value is not None:
                cell.setdefault(str(year), value)
                years.add(year)
    return years


def _fetch(corp: str, year: int) -> list[dict]:
    """연결재무제표(CFS)를 먼저 보고, 없으면 개별(OFS)로 간다."""
    for fs_div in ("CFS", "OFS"):
        try:
            r = requests.get(f"{BASE}/fnlttSinglAcntAll.json", timeout=30, params={
                "crtfc_key": _key(), "corp_code": corp, "bsns_year": str(year),
                "reprt_code": "11011", "fs_div": fs_div})
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise Unavailable(f"DART 재무제표 연결 실패: {type(exc).__name__}") from None
        if body.get("status") == "000" and body.get("list"):
            return body["list"]
        # 013은 해당 조건의 자료가 없다는 정상 응답이다. CFS가 없으면 OFS를 본다.
        if body.get("status") not in ("000", "013"):
            detail = body.get("message") or "알 수 없는 오류"
            raise Unavailable(f"DART {body.get('status', '?')}: {detail}")
    return []


def latest_year(today: date | None = None) -> int:
    """사업보고서는 이듬해 3월에 나온다. 4월 전이면 아직 재작년이 최신이다."""
    d = today or date.today()
    return d.year - 1 if d.month >= 4 else d.year - 2


def statements(symbol: str, years: int = 6) -> dict:
    if C.market_of(symbol) != "KR":
        raise Unavailable("DART 는 국내 종목만 공시한다")
    hit = _corp_map().get(symbol)
    if not hit:
        raise Unavailable(f"{symbol} 은 DART 상장사 목록에 없다")

    tables: dict[str, dict] = {"BS": {}, "IS": {}, "CF": {}}
    order: dict[str, list] = {k: [] for k in tables}
    if years <= 0:
        raise ValueError("years must be positive")
    seen: set[int] = set()
    base = latest_year()
    rows = _fetch(hit["corp"], base)
    if not rows:  # 최신 사업보고서가 아직 안 올라왔을 수 있다
        base -= 1
        rows = _fetch(hit["corp"], base)
    seen |= _fold(rows, base, tables, order)
    for start in range(base - 3, base - years, -3):
        seen |= _fold(_fetch(hit["corp"], start), start, tables, order)
    if not seen:
        raise Unavailable(f"{hit['name']} 의 사업보고서를 못 읽었다")

    cols = sorted(seen)[-years:]
    out = {}
    for div, label in SJ.items():
        out[div] = {"label": label, "rows": [
            {"account": a, "values": [tables[div][a].get(str(y)) for y in cols]}
            for a in order[div]]}
    return {"symbol": symbol, "name": hit["name"], "unit": "억원",
            "years": cols, "base_year": base, "tables": out}


def demo() -> None:
    """망 없이 도는 자체 점검 — 접기 로직과 금액 파싱만 본다."""
    assert _amount("13,238,330,251,696") == 132383.3
    assert _amount("(1,500,000,000)") == -15.0
    assert _amount("") is None and _amount("-") is None and _amount(None) is None

    tables = {"BS": {}, "IS": {}, "CF": {}}
    order = {k: [] for k in tables}
    rows = [{"sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": "300000000000",
             "frmtrm_amount": "200000000000", "bfefrmtrm_amount": "100000000000"}]
    assert _fold(rows, 2025, tables, order) == {2025, 2024, 2023}
    assert tables["BS"]["자산총계"] == {"2025": 3000.0, "2024": 2000.0, "2023": 1000.0}

    # 오래된 보고서는 이미 채워진 연도를 덮지 않는다
    old = [{"sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": "999000000000",
            "frmtrm_amount": "10000000000", "bfefrmtrm_amount": "20000000000"}]
    _fold(old, 2023, tables, order)
    assert tables["BS"]["자산총계"]["2023"] == 1000.0, "최신 공시가 이겨야 한다"
    assert tables["BS"]["자산총계"]["2022"] == 100.0
    assert order["BS"] == ["자산총계"], "같은 계정이 두 번 들어가면 안 된다"

    # 포괄손익계산서(CIS)는 손익계산서로 접힌다. 둘 다 있으면 IS 가 이긴다.
    both = [{"sj_div": "IS", "account_nm": "매출액", "thstrm_amount": "500000000000"},
            {"sj_div": "CIS", "account_nm": "매출액", "thstrm_amount": "900000000000"}]
    _fold(both, 2022, tables, order)
    only_cis = [{"sj_div": "CIS", "account_nm": "매출액", "thstrm_amount": "700000000000"}]
    _fold(only_cis, 2025, tables, order)
    assert tables["IS"]["매출액"] == {"2022": 5000.0, "2025": 7000.0}
    assert order["IS"] == ["매출액"], "IS 와 CIS 가 같은 계정을 두 줄로 만들면 안 된다"

    assert latest_year(date(2026, 9, 14)) == 2025
    assert latest_year(date(2026, 2, 1)) == 2024
    print("dart selfcheck ok")


if __name__ == "__main__":
    demo()
