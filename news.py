#!/usr/bin/env python3
"""
코인 뉴스: 구글 뉴스 검색 RSS(무료, 키 불필요)에서 주제별로 기사 제목을 모아요.
- 제목·매체·시간·원문 링크만 저장하고, 기사 내용은 가져오지 않아요.
- 기본 10분마다 새로 받아요(직접 실행하면 바로 받아요).
"""
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = {"User-Agent": "Mozilla/5.0 (compatible; CoinTime/1.0)"}
PER_TOPIC = 25   # 주제마다 최대 이만큼만 가져와요


def feed_url(q, days=3):
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{q} when:{days}d") + "&hl=ko&gl=KR&ceid=KR:ko"


def fetch_bytes(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read()


def parse_feed(data):
    """RSS → [{t:제목, u:링크, s:매체, d:시각(UTC)}]"""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:   # 이상한 XML 은 받아들이지 않아요
        raise ValueError("의심스러운 XML")
    root = ET.fromstring(data)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        se = it.find("source")
        src = (se.text or "").strip() if se is not None else ""
        if not title or not link.startswith(("http://", "https://")):
            continue
        if src and title.endswith(" - " + src):        # 구글은 제목 끝에 매체 이름을 붙여요
            title = title[:-(len(src) + 3)].rstrip()
        elif not src and " - " in title:
            title, src = title.rsplit(" - ", 1)
        try:
            d = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except Exception:
            continue
        out.append({"t": title[:140], "u": link, "s": src[:30], "d": d.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out


def _norm(t):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t)[:40]


def collect(topics, days, max_age_h, max_items, getter, now):
    seen, errors = {}, 0
    for name, q in topics:
        try:
            items = parse_feed(getter(feed_url(q, days)))
        except Exception as e:
            errors += 1
            print(f"뉴스 '{name}' 실패: {str(e)[:60]}")
            continue
        for it in items[:PER_TOPIC]:
            k = _norm(it["t"])
            if not k:
                continue
            if k in seen:
                if name not in seen[k]["tp"]:
                    seen[k]["tp"].append(name)
            else:
                it["tp"] = [name]
                seen[k] = it
        time.sleep(0.3)
    cutoff = (now - timedelta(hours=max_age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = sorted([x for x in seen.values() if x["d"] >= cutoff], key=lambda x: x["d"], reverse=True)
    return items[:max_items], errors


# ───────── 시험용(가짜) 피드 ─────────
def demo_feed(url):
    q = urllib.parse.unquote(url.split("q=")[1].split("&")[0]).split(" when:")[0]
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(6):
        d = (now - timedelta(minutes=25 + i * 70 + len(q) * 3)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        rows.append(f"<item><title>{q} 관련 예시 기사 {i + 1} - 예시일보</title><link>https://example.com/{urllib.parse.quote(q)}/{i}</link>"
                    f"<pubDate>{d}</pubDate><source url=\"https://example.com\">예시일보</source></item>")
    # 주제끼리 겹치는 기사도 하나 넣어서 합쳐지는지 보게 해요
    d = (now - timedelta(minutes=40)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    rows.append(f"<item><title>코인 시장 공통 예시 기사 - 샘플경제</title><link>https://example.com/common</link><pubDate>{d}</pubDate><source url=\"https://example.com\">샘플경제</source></item>")
    return ("<?xml version=\"1.0\"?><rss><channel>" + "".join(rows) + "</channel></rss>").encode("utf-8")


def update(cfg, st, now, manual=False, watch_names=None, demo=False, getter=None):
    """상태(st)에 쌓아 둔 뉴스를 필요하면 새로 받고, 화면용 news.json 내용을 돌려줘요(꺼져 있으면 None)"""
    nc = cfg.get("news") or {}
    if not nc.get("enabled", True):
        return None
    n = st.setdefault("news", {"t": 0, "items": []})
    # 자동 실행 간격이 조금씩 흔들려도 건너뛰지 않게 8할만 지나면 새로 받아요(10분 설정이면 8분)
    due = manual or not n["items"] or (time.time() - n["t"]) >= nc.get("every_minutes", 10) * 60 * 0.8
    failed = False
    if due:
        get = getter or (demo_feed if demo else fetch_bytes)
        topics = [(a, b) for a, b in nc.get("topics", [])]
        items, errors = collect(topics, 3, nc.get("max_age_hours", 72), nc.get("max_items", 120), get, now)
        if items and errors < len(topics):
            n["items"], n["t"] = items, time.time()
        else:
            failed = True
            print("뉴스를 새로 받지 못해서 이전 기사를 그대로 보여 줘요")
    t = n["t"] or time.time()
    return {"updated": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "items": n["items"], "topics": [a for a, _ in nc.get("topics", [])],
            "watch": list(watch_names or []), "stale": failed}
