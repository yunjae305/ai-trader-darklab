"""포트폴리오 상관관계 — 종목 수가 아니라 '같이 움직이지 않는가'가 분산이다.

NVDA·AVGO·MSFT·GOOGL 에 5%씩 8종목을 담아도 전부 같이 빠지면 분산이 아니다.
여기서 상관관계를 재서 brain 에 넘긴다. 막지는 않는다 — 무엇을 살지는 brain 이 정한다.
출처: invest/portfolio.py (매수 차단 함수 max_correlation_with 는 옮기지 않았다).

계산은 gs-quant(econometrics)를 쓰되, 없으면 stdlib 로 같은 값을 낸다.
외부 라이브러리 유무로 매매가 멈추면 안 된다.
"""
import math, os, sys

GS_QUANT_PATH = os.environ.get("GS_QUANT_PATH", "")
HIGH_CORR = 0.8         # 이 이상이면 사실상 같은 종목
MIN_OVERLAP = 30        # 상관계수를 믿으려면 최소 이만큼 겹쳐야 한다


def _gs():
    """gs-quant 를 쓸 수 있으면 모듈을 돌려준다. 없으면 None."""
    if GS_QUANT_PATH and os.path.isdir(GS_QUANT_PATH) and GS_QUANT_PATH not in sys.path:
        sys.path.insert(0, GS_QUANT_PATH)
    try:
        import warnings
        warnings.filterwarnings("ignore")
        from gs_quant.timeseries import econometrics
        import pandas
        return econometrics, pandas
    except Exception:
        return None


def returns(closes):
    """일간 수익률."""
    return [(b - a) / a for a, b in zip(closes, closes[1:]) if a]


def correlation(a_closes, b_closes):
    """두 종목 수익률의 상관계수. 겹치는 구간이 모자라면 None."""
    n = min(len(a_closes), len(b_closes))
    if n < MIN_OVERLAP + 1:
        return None
    ra, rb = returns(a_closes[-n:]), returns(b_closes[-n:])
    if len(ra) < MIN_OVERLAP:
        return None
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb)


def matrix(series_by_symbol):
    """종목쌍 상관계수 표. {(A,B): corr}"""
    syms = sorted(series_by_symbol)
    out = {}
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            c = correlation(series_by_symbol[a], series_by_symbol[b])
            if c is not None:
                out[(a, b)] = c
    return out


def clusters(corr, threshold=HIGH_CORR):
    """상관계수가 임계 이상인 종목들을 한 덩어리로 묶는다. 덩어리 = 실질 1종목."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), c in corr.items():
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        if c >= threshold:
            parent[find(a)] = find(b)
    groups = {}
    for s in parent:
        groups.setdefault(find(s), []).append(s)
    return sorted((sorted(v) for v in groups.values()), key=len, reverse=True)


def diversification(series_by_symbol, threshold=HIGH_CORR):
    """실질 분산도. 종목 수가 아니라 덩어리 수로 센다."""
    corr = matrix(series_by_symbol)
    groups = clusters(corr, threshold)
    n = len(series_by_symbol)
    effective = len(groups)
    pairs = sorted(corr.items(), key=lambda kv: -kv[1])
    # 비율만 보면 종목이 적을 때 틀린다 (똑같은 2종목도 ratio 0.5).
    # 가장 큰 덩어리가 전체의 절반을 넘으면 그건 집중이다.
    largest = max((len(g) for g in groups), default=0)
    largest_share = largest / n if n else 0.0
    return {
        "symbols": n,
        "effective_groups": effective,
        "ratio": round(effective / n, 2) if n else 0.0,
        "largest_cluster": largest,
        "largest_share": round(largest_share, 2),
        "clusters": [g for g in groups if len(g) > 1],
        "highest_pairs": [{"pair": f"{a}-{b}", "corr": round(c, 3)}
                          for (a, b), c in pairs[:5]],
        "concentration": ("집중 위험" if largest_share > 0.5 else
                    "분산 양호" if n and effective / n >= 0.7 else "부분 집중"),
    }


def metrics(closes, benchmark_closes=None, dates=None):
    """성과 지표. gs-quant 가 있으면 그걸 쓰고, 없으면 직접 계산한다.

    gs-quant 는 날짜 인덱스가 붙은 Series 만 받는다.
    인덱스 없이 넘기면 'int' object has no attribute 'days' 로 죽는다.
    """
    gs = _gs()
    if gs and len(closes) > MIN_OVERLAP:
        E, pd = gs
        try:
            idx = (pd.to_datetime(dates) if dates
                   else pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=len(closes)))
            s = pd.Series(closes, index=idx)
            out = {"volatility_pct": float(E.volatility(s).iloc[-1]),
                   "max_drawdown": float(E.max_drawdown(s).iloc[-1]),
                   "source": "gs-quant"}
            if benchmark_closes and len(benchmark_closes) == len(closes):
                b = pd.Series(benchmark_closes, index=idx)
                out["beta"] = float(E.beta(s, b).iloc[-1])
                out["correlation"] = float(E.correlation(s, b).iloc[-1])
            return out
        except Exception as e:
            out = {**_metrics_stdlib(closes), "source": "stdlib",
                   "gs_error": f"{type(e).__name__}: {str(e)[:80]}"}
            return out
    return {**_metrics_stdlib(closes), "source": "stdlib"}


def _metrics_stdlib(closes):
    r = returns(closes)
    if not r:
        return {"volatility_pct": None, "max_drawdown": None}
    m = sum(r) / len(r)
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / len(r))
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, (c - peak) / peak)
    return {"volatility_pct": sd * math.sqrt(252) * 100, "max_drawdown": mdd}
