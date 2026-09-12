"""뉴스 수집 — 언론사가 공개한 RSS 를 직접 받는다.

여기에는 판단이 없다. 제목·링크·시각·매체만 모아서 brain 과 대시보드에 넘긴다.

**본문을 복제하지 않는다.** RSS 가 주는 제목과 원문 링크만 들고, 기사를 읽으려면
그 매체로 간다. 남의 서비스 내부 API 도 부르지 않는다 — 아래 FEEDS 는 전부
언론사가 스스로 공개한 피드다.

FEEDS 는 2026-09 에 실제로 받아본 것만 남겼다. 죽은 피드는 추측으로 두지 않는다
(한국경제는 rss.hankyung.com 이 HTML 을 돌려주고, 매일경제는 403, 서울경제는 404 다).
"""
from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent import futures
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

UA = {"User-Agent": "Mozilla/5.0"}

# 증권·경제 전용 피드만 쓴다. 종합 피드는 야구·골프 기사가 섞여 들어와
# brain 의 프롬프트만 늘리고 판단을 흐린다.
FEEDS = [
    {"source": "연합뉴스", "category": "증시", "url": "https://www.yna.co.kr/rss/market.xml"},
    {"source": "연합뉴스", "category": "경제", "url": "https://www.yna.co.kr/rss/economy.xml"},
    {"source": "한국경제", "category": "증권", "url": "https://www.hankyung.com/feed/finance"},
    {"source": "이데일리", "category": "증권", "url": "http://rss.edaily.co.kr/stock_news.xml"},
    {"source": "아시아경제", "category": "증권", "url": "https://www.asiae.co.kr/rss/stock.htm"},
    {"source": "뉴시스", "category": "경제", "url": "https://newsis.com/RSS/economy.xml"},
    {"source": "구글뉴스", "category": "경제",
     "url": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko"},
]

_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def _when(node: ET.Element) -> str:
    """pubDate 를 ISO 로. 매체마다 형식이 달라서 못 읽으면 빈 문자열로 둔다."""
    raw = (node.findtext("pubDate") or node.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip()
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat(timespec="seconds")


def fetch_feed(spec: dict, limit: int = 30) -> list[dict]:
    """피드 하나. 실패해도 예외를 올리지 않는다 — 한 매체가 죽어도 나머지는 온다."""
    try:
        r = requests.get(spec["url"], timeout=10, headers=UA)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as exc:
        return [{"unavailable": f"{spec['source']}: {type(exc).__name__}",
                 "source": spec["source"], "category": spec.get("category", "")}]
    out = []
    for node in root.iter("item"):
        title = _clean(node.findtext("title"))
        if not title:
            continue
        out.append({
            "title": title,
            "link": (node.findtext("link") or "").strip(),
            "published": _when(node),
            "source": spec["source"],
            "category": spec.get("category", ""),
        })
        if len(out) >= limit:
            break
    return out


def headlines(limit: int = 40, per_feed: int = 20, ttl: int = 300) -> list[dict]:
    """여러 매체를 한 번에 받아 최신순으로 합친다. 같은 기사는 제목으로 걸러낸다."""
    hit = _CACHE.get("headlines")
    if hit and time.time() - hit[0] < ttl:
        return hit[1][:limit]

    with futures.ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        batches = list(pool.map(lambda s: fetch_feed(s, per_feed), FEEDS))

    seen, merged, failed = set(), [], []
    for batch in batches:
        for item in batch:
            if item.get("unavailable"):
                failed.append(item["unavailable"])
                continue
            # 같은 기사가 여러 매체·구글뉴스로 중복해서 들어온다. 제목으로 한 번만 남긴다.
            key = re.sub(r"[^\w가-힣]", "", item["title"])[:40]
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    merged.sort(key=lambda x: x["published"] or "", reverse=True)
    if failed:
        merged.append({"unavailable": " / ".join(sorted(set(failed))),
                       "title": "", "source": "", "category": "", "published": ""})
    _CACHE["headlines"] = (time.time(), merged)
    return merged[:limit]


def for_symbol(symbol: str, name: str | None = None, limit: int = 4, ttl: int = 900) -> list[dict]:
    """종목별 뉴스. 매체 RSS 에는 종목 검색이 없어서 구글 뉴스 검색을 쓴다."""
    key = f"sym:{symbol}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    q = urllib.parse.quote(f"{name or symbol} 주가")
    spec = {"source": "구글뉴스", "category": "종목",
            "url": f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"}
    items = fetch_feed(spec, limit)
    if items and items[0].get("unavailable"):
        items = [{"title": f"[news unavailable: {items[0]['unavailable']}]", "published": "",
                  "source": "", "category": "", "link": ""}]
    _CACHE[key] = (time.time(), items)
    return items


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ai_trader.news", description="뉴스 피드 점검")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    print(f"[news] 피드 {len(FEEDS)}개에서 수집\n")
    for item in headlines(args.limit):
        if item.get("unavailable"):
            print(f"\n  못 받은 피드: {item['unavailable']}")
            continue
        print(f"  {item['published'][5:16].replace('T', ' '):<12} "
              f"{item['source']:<6} {item['category']:<4} {item['title'][:56]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
