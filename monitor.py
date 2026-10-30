#!/usr/bin/env python3
"""
코인 신호 모니터
  시세 점검 → 텔레그램 알림 → 대시보드용 data.json 생성

GitHub Actions 에서 10분마다 자동 실행되고, 내 PC 에서도 돌릴 수 있습니다.

  python monitor.py                 # 한 번 실행 (site/ 폴더에 대시보드 생성)
  python monitor.py --loop 300      # 5분마다 계속 실행 (PC 에서 쓸 때)
  python monitor.py --demo          # 인터넷 없이 가짜 데이터로 시험

텔레그램 알림은 환경변수 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 가 있을 때만 전송합니다.
없으면 화면에 출력만 합니다.
참고용 신호이며 수익을 보장하지 않습니다. 이 프로그램은 주문을 넣지 않습니다.
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import coin_signal as cs
import news
import universe

KST = timezone(timedelta(hours=9))
SITE, WEB, STATE_DIR = "site", "web", "state"
ROOT_WEB_FILES = ["index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png"]
PERIODS = [("w", 7), ("m", 30), ("q", 90), ("y", 365)]
STATE_FILE = os.path.join(STATE_DIR, "state.json")

NAMES = {
    "BTC": "비트코인", "ETH": "이더리움", "XRP": "리플", "SOL": "솔라나", "DOGE": "도지코인",
    "ADA": "에이다", "AVAX": "아발란체", "DOT": "폴카닷", "LINK": "체인링크", "TRX": "트론",
    "POL": "폴리곤", "MATIC": "폴리곤", "BCH": "비트코인캐시", "ETC": "이더리움클래식",
    "LTC": "라이트코인", "ATOM": "코스모스", "SUI": "수이", "APT": "앱토스", "ARB": "아비트럼",
    "NEAR": "니어프로토콜", "XLM": "스텔라루멘", "SHIB": "시바이누", "HBAR": "헤데라",
    "USDT": "테더", "PCI": "페이코인", "EOS": "이오스", "STX": "스택스", "SEI": "세이", "ONDO": "온도파이낸스",
}

DEFAULTS = {
    "exchange": "upbit",
    "fallback_exchange": "bithumb",
    "unit": 60,
    "markets": ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-PCI"],
    "market_exchange": {"KRW-PCI": "bithumb"},
    "buy_threshold": 4,
    "sell_threshold": -4,
    "fee": 0.0025,
    "dashboard_url": "",
    "min_trade_value_24h": 0,
    "upbit": True,
    "pin": ["KRW-PCI"],
    "news": {"enabled": True, "every_minutes": 10, "max_items": 120, "max_age_hours": 72,
             "topics": [["시장", "코인 시세"], ["비트코인", "비트코인"], ["이더리움", "이더리움"], ["리플", "리플 XRP"],
                        ["솔라나", "솔라나"], ["도지코인", "도지코인"], ["페이코인", "페이코인"], ["알트코인", "알트코인"],
                        ["빗썸", "빗썸"], ["업비트", "업비트"], ["상장·상폐", "코인 상장 폐지"],
                        ["규제·정책", "가상자산 규제"], ["스테이블코인", "스테이블코인"]]},
    "telegram": {"digest": True, "digest_size": 20, "digest_start": 7, "digest_every_hours": 6,
                 "quiet_from": 22, "quiet_to": 7, "instant_alerts": False},
    "alerts": {"signal_change": True, "spike": True, "spike_pct": 5, "spike_minutes": 60,
               "targets": True, "low_zone": True, "new_low": True, "rebound": True,
               "cooldown_minutes": 180},
    "targets": {},
}


# ───────────────────────── 유틸 ─────────────────────────
def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 100:
        return f"{p:,.1f}"
    if p >= 10:
        return f"{p:,.2f}"
    return f"{p:,.4f}"


def fmt_pct(x, d=1):
    return f"{x * 100:+.{d}f}%"


def display_name(market):
    sym = market.split("-")[-1]
    return NAMES.get(sym, sym)


def load_config(path="config.json"):
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k != "targets":
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def dashboard_url(cfg):
    if cfg.get("dashboard_url"):
        return cfg["dashboard_url"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


# ───────────────────────── 상태 저장 ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("tags", {})
    st.setdefault("last", {})
    st.setdefault("hit", {})
    st.setdefault("history", [])
    st.setdefault("refs", {})
    return st


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))


def cooldown_ok(st, key, now, minutes):
    t = st["last"].get(key)
    if not t:
        return True
    return (now - parse_iso(t)) >= timedelta(minutes=minutes)


# ───────────────────────── 텔레그램 ─────────────────────────
def tg_send(text):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[텔레그램 미설정 · 화면에만 표시]\n" + text + "\n")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15):
            return True
    except urllib.error.HTTPError as e:
        print("텔레그램 전송 실패:", e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:
        print("텔레그램 전송 실패:", e)
    return False


# ───────────────────────── 시세 분석 ─────────────────────────
def exchange_order(cfg, market):
    order = []
    for ex in [cfg.get("market_exchange", {}).get(market), cfg["exchange"], cfg.get("fallback_exchange")]:
        if ex and ex not in order:
            order.append(ex)
    return order


def candles_for(cfg, market, unit, count, demo):
    if demo:  # 시험용: 같은 가짜 시세에서 잘라 쓰기
        return cs.demo_candles(market, 200)[-count:], "demo"
    order = exchange_order(cfg, market)
    last = None
    for ex in order:
        try:
            c = cs.get_candles(ex, market, unit, count, demo)
            if c:
                return c, ex
        except Exception as e:
            last = e
    raise last or RuntimeError("캔들을 가져오지 못했어요")


def analyze(cfg, market, demo):
    cand, ex = candles_for(cfg, market, cfg["unit"], 200, demo)
    ind = cs.compute(cand)
    i = len(ind["c"]) - 1
    score, why = cs.score_at(ind, i)
    tag = "BUY" if score >= cs.BUY_TH else "SELL" if score <= cs.SELL_TH else "WAIT"
    a = cfg["alerts"]
    steps = max(1, int(a["spike_minutes"]) // 5)
    try:
        c5, _ = candles_for(cfg, market, 5, steps + 2, demo)
    except Exception:
        c5 = []
    price = c5[-1].c if c5 else ind["c"][-1]
    chg = (c5[-1].c / c5[-1 - steps].c - 1) if len(c5) > steps else (ind["c"][-1] / ind["c"][-2] - 1)
    r = ind["rsi"][i]
    return {
        "market": market, "name": display_name(market), "status": "ok", "ex": ex,
        "price": price, "chg": round(chg, 5), "signal": tag, "score": score,
        "rsi": round(r, 1) if r is not None else None, "reasons": why,
        "spark": [round(x, 6) for x in ind["c"][-48:]],
    }


# ───────────────────────── 기간별 변동 (일봉 종가 기준) ─────────────────────────
def fetch_days_v1(base, market, total):
    rows_all, to = [], None
    while len(rows_all) < total:
        n = min(200, total - len(rows_all))
        q = {"market": market, "count": n}
        if to:
            q["to"] = to
        rows = cs.http_json(f"{base}/v1/candles/days?" + urllib.parse.urlencode(q))
        if not rows:
            break
        rows_all.extend(rows)
        to = rows[-1]["candle_date_time_utc"] + "Z"
        time.sleep(0.15)
        if len(rows) < n:
            break
    return {r["candle_date_time_kst"][:10]: float(r["trade_price"]) for r in rows_all}


def fetch_days_bithumb_legacy(market, total):
    quote, coin = market.split("-")
    js = cs.http_json(f"{cs.BASES['bithumb']}/public/candlestick/{coin}_{quote}/24h")
    if js.get("status") != "0000":
        raise RuntimeError(f"빗썸 응답 오류: {js}")
    out = {}
    for r in js["data"][-total:]:
        d = datetime.fromtimestamp(int(r[0]) / 1000, KST).strftime("%Y-%m-%d")
        out[d] = float(r[2])
    return out


def daily_closes(cfg, market, demo, total=380):
    """날짜(YYYY-MM-DD) → 일봉 종가"""
    if demo:
        big = cs.demo_candles(market, 400)
        k = cs.demo_candles(market, 200)[-1].c / big[-1].c
        today = datetime.now(KST).date()
        return {(today - timedelta(days=len(big) - 1 - i)).strftime("%Y-%m-%d"): c.c * k for i, c in enumerate(big)}
    last = None
    for ex in exchange_order(cfg, market):
        try:
            d = fetch_days_v1(cs.BASES[ex], market, total)
            if d:
                return d
        except Exception as e:
            last = e
        if ex == "bithumb":  # 새 API 가 안 되면 구형 API 로 한 번 더
            try:
                return fetch_days_bithumb_legacy(market, total)
            except Exception as e:
                last = e
    raise last or RuntimeError("일봉을 가져오지 못했어요")


def compute_refs(daily):
    """오늘 기준 N일 전 일봉 종가 (없으면 None)"""
    if not daily:
        return {k: None for k, _ in PERIODS}
    base = datetime.strptime(max(daily), "%Y-%m-%d").date()
    refs = {}
    for key, n in PERIODS:
        ref = None
        for back in range(0, 4):  # 그날 또는 그 전 3일 안의 가장 가까운 값
            d = (base - timedelta(days=n + back)).strftime("%Y-%m-%d")
            if d in daily:
                ref = daily[d]
                break
        refs[key] = ref
    return refs


def get_refs(cfg, st, market, demo):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    c = st["refs"].get(market)
    if c and c.get("date") == today:
        return c["refs"]
    try:
        refs = compute_refs(daily_closes(cfg, market, demo))
        st["refs"][market] = {"date": today, "refs": refs}
        return refs
    except Exception as e:
        print(f"{market}: 기간별 변동 계산 실패 {e}")
        return c["refs"] if c else None


# ───────────────────────── 알림 문구 ─────────────────────────
def head(m):
    return f"{m['name']} ({m['market']})"


def msg_signal(m):
    icon = "🟢 매수 후보" if m["signal"] == "BUY" else "🔴 매도 후보"
    rsi = f" · RSI {m['rsi']:.0f}" if m["rsi"] is not None else ""
    return (f"{icon} · {head(m)}\n현재가 {fmt_price(m['price'])}원 (1시간 {fmt_pct(m['chg'])})\n"
            f"점수 {m['score']:+d}{rsi}\n이유: {', '.join(m['reasons']) or '-'}")


def msg_spike(m, minutes):
    icon = "⚡ 급등" if m["chg"] > 0 else "⚡ 급락"
    return f"{icon} · {head(m)}\n현재가 {fmt_price(m['price'])}원 ({minutes}분 {fmt_pct(m['chg'])})"


def msg_target(m, kind, level):
    icon = "🎯 목표가 도달" if kind == "target" else "🛑 손절가 도달"
    return f"{icon} · {head(m)}\n현재가 {fmt_price(m['price'])}원 (설정 {fmt_price(level)}원)"


def describe_error(e):
    import traceback
    tb = traceback.extract_tb(e.__traceback__)
    where = f"{os.path.basename(tb[-1].filename)} {tb[-1].lineno}번째 줄" if tb else ""
    return f"{type(e).__name__}: {str(e)[:200]}\n위치: {where}"


def msg_low(m, coin, kind):
    lo, p, age = coin["lo"], coin["p"], coin.get("age", 0)
    head_line = f"{m['name']} ({m['market']})"
    now_line = f"현재가 {fmt_price(p)}원 (1시간 {fmt_pct(m['chg'])})"
    if kind == "LOW":
        parts = [f"{universe.PERIOD_LABEL[k]} {(p / lo[k][0] - 1) * 100:+.0f}%" for k in ("a", "q", "h", "y")
                 if universe.in_zone(k, p, lo.get(k), age)]
        return (f"저점권 진입 · {head_line}\n{now_line}\n저점 근처(최저가 대비): {' · '.join(parts)}\n"
                "아직 반등 확인 전이에요")
    if kind == "NEWLOW":
        key = next((k for k in ("a", "y", "h", "q", "m", "w") if lo.get(k) and lo[k][2] == 0 and (k != "a" or age >= universe.MIN_AGE_A)), "w")
        prev = coin.get("pl", {}).get(key)
        extra = f" (이전 저점 {fmt_price(prev)}원)" if prev else ""
        return (f"신저가 갱신 · {head_line}\n{now_line}\n{universe.PERIOD_LABEL[key]} 최저가를 새로 썼어요{extra}\n"
                "더 내려갈 수 있어요. 반등이 확인되면 다시 알려 드려요.")
    key = next((k for k in ("q", "h", "y") if lo.get(k) and 0 < lo[k][2] <= universe.REBOUND_DAYS), "q")
    base = lo[key][0]
    return (f"저점 반등 확인 · {head_line}\n{now_line}\n{universe.PERIOD_LABEL[key]} 저점 {fmt_price(base)}원에서 "
            f"{fmt_pct(p / base - 1)} 반등\n3일 전보다 높아요")


# ───────────────────────── 텔레그램 시간표(하루 3번 하락 TOP) ─────────────────────────
PERIOD_NAMES = [("d", "1일"), ("w", "1주"), ("m", "1개월"), ("q", "3개월"), ("h", "6개월"), ("y", "12개월"), ("y2", "24개월")]
TAG_SHORT = {"NEWLOW": "신저가", "LOW": "저점권", "REBOUND": "반등"}


def in_quiet_hour(hour, qf, qt):
    return (hour >= qf or hour < qt) if qf > qt else (qf <= hour < qt)


def digest_hours(tc):
    """보낼 시각(시): 시작 시각부터 N시간마다, 조용한 시간대는 건너뛰어요 (기본 7시·13시·19시)"""
    out, h = [], tc["digest_start"]
    while h < tc["digest_start"] + 24:
        if not in_quiet_hour(h % 24, tc["quiet_from"], tc["quiet_to"]):
            out.append(h % 24)
        h += tc["digest_every_hours"]
    return sorted(set(out))


def due_digest(tc, now, st, window_min=120):
    """지금 보낼 차례인 시각 표시(예: '2026-10-30 13')를 돌려줘요. 없으면 None"""
    if not tc.get("digest", True):
        return None
    k = now.astimezone(KST)
    for hh in digest_hours(tc):
        slot = k.replace(hour=hh, minute=0, second=0, microsecond=0)
        if slot <= k <= slot + timedelta(minutes=window_min):
            sid = slot.strftime("%Y-%m-%d %H")
            return None if st.get("digest_last") == sid else sid
    return None


def msg_digest(coins, n, now, url, limit=3800):
    """기간별 하락 TOP n. 텔레그램은 한 메시지가 4096자까지라서 길면 여러 개로 나눠요."""
    k = now.astimezone(KST)
    title = f"Dip Radar · 하락폭 TOP {n} (빗썸 · {k.month}/{k.day} {k.hour:02d}:{k.minute:02d})"
    sections = []
    for key, label in PERIOD_NAMES:
        rows = sorted([c for c in coins if c["ch"].get(key) is not None], key=lambda c: c["ch"][key])[:n]
        lines = [f"[{label}]"]
        if not rows:
            lines.append("데이터가 아직 없어요")
        for i, c in enumerate(rows, 1):
            tag = f" · {TAG_SHORT[c['tag']]}" if c.get("tag") in TAG_SHORT else ""
            lines.append(f"{i}. {c['n']} {fmt_pct(c['ch'][key])} · {fmt_price(c['p'])}원{tag}")
        sections.append("\n".join(lines))
    msgs, cur = [], title
    for sec in sections:
        if len(cur) + len(sec) + 2 > limit and cur != title:
            msgs.append(cur)
            cur = f"{title} - 이어서"
        cur += "\n\n" + sec
    msgs.append(cur)
    if url:
        msgs[-1] += f"\n\n📊 {url}"
    return msgs


# ───────────────────────── 한 번 실행 ─────────────────────────
def run_once(cfg, demo=False):
    cs.BUY_TH, cs.SELL_TH, cs.FEE = cfg["buy_threshold"], cfg["sell_threshold"], cfg["fee"]
    a = cfg["alerts"]
    now = utcnow()
    st = load_state()
    first = not st.get("init")
    markets, alerts = [], []

    def add_alert(m, kind, text):
        alerts.append(text)
        st["history"].append({"t": iso(now), "market": m["market"], "name": m["name"], "kind": kind, "text": text})

    for mk in cfg["markets"]:
        try:
            m = analyze(cfg, mk, demo)
        except Exception as e:
            markets.append({"market": mk, "name": display_name(mk), "status": "error", "error": str(e)[:120]})
            print(f"{mk}: 오류 {e}")
            continue
        tg = cfg["targets"].get(mk) or {}
        m["target"], m["stop"] = tg.get("target"), tg.get("stop")
        refs = get_refs(cfg, st, mk, demo)
        m["perf"] = ({k: (round(m["price"] / v - 1, 5) if v else None) for k, v in refs.items()}
                     if refs else None)
        markets.append(m)

        # 1) 신호 변경
        prev = st["tags"].get(mk)
        if a["signal_change"] and not first and m["signal"] in ("BUY", "SELL") and prev != m["signal"]:
            if cooldown_ok(st, f"sig:{mk}", now, a["cooldown_minutes"]):
                add_alert(m, "signal", msg_signal(m))
                st["last"][f"sig:{mk}"] = iso(now)
        st["tags"][mk] = m["signal"]

        # 2) 급등락
        if a["spike"] and abs(m["chg"]) * 100 >= a["spike_pct"]:
            if cooldown_ok(st, f"spk:{mk}", now, 60):
                add_alert(m, "spike", msg_spike(m, a["spike_minutes"]))
                st["last"][f"spk:{mk}"] = iso(now)

        # 3) 목표가·손절가
        if a["targets"]:
            tgt, stp, p = m["target"], m["stop"], m["price"]
            hit = st["hit"].get(mk)
            if tgt and p >= tgt:
                if hit != "target":
                    add_alert(m, "target", msg_target(m, "target", tgt))
                    st["hit"][mk] = "target"
            elif stp and p <= stp:
                if hit != "stop":
                    add_alert(m, "stop", msg_target(m, "stop", stp))
                    st["hit"][mk] = "stop"
            else:
                st["hit"].pop(mk, None)

    url = dashboard_url(cfg)
    tail = f"\n\n📊 {url}" if url else ""
    manual = os.environ.get("EVENT_NAME") == "workflow_dispatch"
    if manual:   # 직접 실행했다면 오래 걸리는 전체 코인 작업 전에 먼저 알려요
        lines = [f"{x['name']} {fmt_price(x['price'])}원" for x in markets if x["status"] == "ok"]
        tg_send("✅ Dip Radar 실행을 시작했어요 (전체 코인 처리 중이라 몇 분 걸려요)\n" + ("\n".join(lines) or "시세를 가져오지 못했어요") + tail)
    print("관심 종목 처리 끝, 전체 코인 처리 시작", flush=True)

    # 빗썸 전체 코인 + 저점 판정 (관심 종목은 저점 알림도 보내요)
    try:
        coins, stale = universe.build(cfg, st, demo, "bithumb", 60)
    except Exception as e:  # 전체 코인 처리에서 오류가 나도 관심 종목 알림은 계속 보내요
        import traceback
        traceback.print_exc()
        coins, stale = st.get("u", {}).get("bithumb", {}).get("last", []), True
        if cooldown_ok(st, "uerr", now, 360):
            alerts.append(f"⚠️ 전체 코인 처리 중 오류가 났어요 (이전 데이터를 보여 줘요)\n{describe_error(e)}")
            st["last"]["uerr"] = iso(now)
    cmap = {c["m"]: c for c in coins}
    watch_set = {x["market"] for x in markets}
    pins = cfg.get("pin") or []
    for c in coins:
        c["w"] = 1 if c["m"] in watch_set else 0
        if c["m"] in pins:
            c["pin"] = 1   # 관심 목록에서 맨 위에 보여 줄 코인
    ltags = st.setdefault("ltags", {})
    low_first = not st.get("uinit")  # 처음 한 번은 현재 상태만 기록하고 알림은 건너뛰어요
    st["uinit"] = True
    kind_flag = {"LOW": "low_zone", "NEWLOW": "new_low", "REBOUND": "rebound"}
    for m in markets:
        c = cmap.get(m["market"])
        if m["status"] != "ok" or not c:
            continue
        prev, cur = ltags.get(m["market"]), c["tag"]
        if cur in kind_flag and prev != cur and not first and not low_first and a.get(kind_flag[cur], True):
            if cooldown_ok(st, f"low:{m['market']}", now, a["cooldown_minutes"]):
                add_alert(m, "low", msg_low(m, c, cur))
                st["last"][f"low:{m['market']}"] = iso(now)
        ltags[m["market"]] = cur

    print("빗썸 전체 코인 처리 끝", flush=True)
    upbit = None
    if cfg.get("upbit", True):
        try:
            ucoins, ustale = universe.build(cfg, st, demo, "upbit", 60)
            upbit = {"updated": iso(now), "coins": ucoins, "stale": ustale}
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("업비트 처리 오류:", describe_error(e))
            ulast = st.get("u", {}).get("upbit", {}).get("last", [])
            if ulast:
                upbit = {"updated": iso(now), "coins": ulast, "stale": True}

    newsdata = None
    try:
        wnames = [display_name(x["market"]) for x in markets]
        newsdata = news.update(cfg, st, now, manual, wnames, demo)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("뉴스 처리 오류:", describe_error(e))
    print("뉴스 처리 끝", flush=True)

    # 모든 종목 실패 → 한 번 알려주기(6시간에 한 번)
    if markets and all(x["status"] == "error" for x in markets):
        if cooldown_ok(st, "err", now, 360):
            alerts.append("⚠️ 시세를 가져오지 못했어요. 거래소 연결이나 종목 이름을 확인해 주세요.\n" + markets[0].get("error", ""))
            st["last"]["err"] = iso(now)

    st["init"] = True
    st["history"] = st["history"][-30:]
    save_state(st)

    # 텔레그램: 기본은 하루 3번(7·13·19시) 하락 TOP 묶음만 보내요. 밤 10시~아침 7시에는 보내지 않아요.
    tc = cfg["telegram"]
    quiet = in_quiet_hour(now.astimezone(KST).hour, tc["quiet_from"], tc["quiet_to"])
    if alerts and tc.get("instant_alerts") and not quiet:
        if len(alerts) <= 3:
            for t in alerts:
                tg_send(t + tail)
        else:
            tg_send("\n\n".join(alerts[:8]) + tail)
    sid = due_digest(tc, now, st)
    if (sid or manual) and coins and not stale:
        sent = True
        for i, text in enumerate(msg_digest(coins, tc["digest_size"], now, url)):
            if i:
                time.sleep(1.2)   # 텔레그램은 같은 대화에 1초에 한 번 정도만 받아 줘요
            sent = tg_send(text) and sent
        if sid and (sent or not os.environ.get("TELEGRAM_TOKEN")):
            st["digest_last"] = sid   # 전송에 실패하면 표시하지 않아서 다음 실행에서 다시 시도해요
    if manual:
        tg_send("✅ Dip Radar 처리를 마쳤어요. 화면을 새로고침해 보세요." + tail)
    save_state(st)

    for c in coins + (upbit["coins"] if upbit else []):  # 화면 데이터가 작아지도록 빈 값은 빼요
        if not c.get("pl"):
            c.pop("pl", None)
    data = {
        "updated": iso(now),
        "updated_kst": now.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
        "exchange": cfg["exchange"], "unit": cfg["unit"],
        "thresholds": {"buy": cs.BUY_TH, "sell": cs.SELL_TH},
        "markets": markets,
        "coins": coins, "stale": stale, "exchanges": ["bithumb", "upbit"] if upbit else ["bithumb"],
        "alerts": list(reversed(st["history"]))[:20],
    }
    charts = {"bithumb": universe.charts_for(st, "bithumb", coins)}
    if upbit:
        charts["upbit"] = universe.charts_for(st, "upbit", upbit["coins"])
    publish(data, upbit, charts, newsdata)
    return data


def publish(data, upbit=None, charts=None, newsdata=None):
    os.makedirs(SITE, exist_ok=True)
    if os.path.isdir(WEB):
        shutil.copytree(WEB, SITE, dirs_exist_ok=True)
    for f in ROOT_WEB_FILES:  # 저장소 맨 바깥에 올린 화면 파일도 사용 (새로 올린 것이 우선)
        if os.path.isfile(f):
            shutil.copy(f, os.path.join(SITE, f))
    for name, obj in (("data.json", data), ("upbit.json", upbit), ("news.json", newsdata)):
        if obj is None:
            continue
        tmp = os.path.join(SITE, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, os.path.join(SITE, name))
    for ex, per in (charts or {}).items():  # 코인별 차트 점들: c/거래소/기호.json (상세 화면을 열 때만 불러와요)
        d = os.path.join(SITE, "c", ex)
        os.makedirs(d, exist_ok=True)
        for sym, payload in per.items():
            if not sym.replace("-", "").replace("_", "").isalnum():
                continue
            with open(os.path.join(d, sym + ".json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))


def print_table(data):
    print(f"\n[{data['updated_kst']} KST] {data['exchange']} {data['unit']}분봉")
    for m in data["markets"]:
        if m["status"] != "ok":
            print(f"  {m['market']:<10} 오류: {m.get('error', '')}")
            continue
        pf = m.get("perf") or {}
        pf_txt = " ".join(f"{lb} {fmt_pct(pf[k]) if pf.get(k) is not None else '-'}"
                          for k, lb in (("w", "1주"), ("m", "1달"), ("q", "3달"), ("y", "1년")))
        print(f"  {m['market']:<10} {fmt_price(m['price']):>14}원  1h {fmt_pct(m['chg']):>7}  "
              f"{m['signal']:<5} 점수 {m['score']:+d}  {pf_txt}")


def print_universe(data):
    coins = [c for c in data.get("coins", []) if c["ch"].get("m") is not None]
    print(f"\n빗썸 원화마켓 {len(data.get('coins', []))}개 · 1개월 하락폭 상위 8")
    for c in coins[:8]:
        print(f"  {c['n']:<10} {fmt_price(c['p']):>12}원  1개월 {fmt_pct(c['ch']['m']):>8}  {c['tag']:<7} {c['tg']}")


def main():
    ap = argparse.ArgumentParser(description="코인 신호 모니터")
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 시험")
    ap.add_argument("--loop", type=int, default=0, help="N초마다 반복 실행 (PC에서 쓸 때)")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    cfg = load_config(args.config)
    while True:
        try:
            data = run_once(cfg, args.demo)
        except Exception as e:
            import traceback
            traceback.print_exc()
            tg_send("⚠️ Dip Radar 프로그램 오류\n" + describe_error(e) + "\n이 메시지를 그대로 알려 주시면 고칠 수 있어요.")
            if not args.loop:
                sys.exit(1)
            time.sleep(args.loop)
            continue
        print_table(data)
        print_universe(data)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
