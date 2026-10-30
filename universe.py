#!/usr/bin/env python3
"""
빗썸 원화마켓 전체 코인: 현재가, 기간별 변동률, 기간별 최저·최고가, 저점 판정

- 현재가와 24시간 변동률은 ticker/ALL_KRW 한 번의 요청으로 가져와요.
- 일봉(기간별 최저·최고·종가)은 코인마다 하루에 한 번만 받아서 상태 파일에 저장해요.
- 그 사이에는 현재가로 새 최저가·최고가만 갱신해요.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import coin_signal as cs

KST = timezone(timedelta(hours=9))
BASE = "https://api.bithumb.com"

# 종가 비교용(일 전): d3 은 반등 판정에, y2 는 24개월 변동률에 써요
CLOSE_REFS = [("d3", 3), ("w", 7), ("m", 30), ("q", 90), ("h", 180), ("y", 365), ("y2", 730)]
# 최저·최고가를 보는 기간: 1주, 1개월, 3개월, 6개월, 12개월
LOW_PERIODS = [("w", 7), ("m", 30), ("q", 90), ("h", 180), ("y", 365)]
PERIOD_LABEL = {"w": "1주", "m": "1개월", "q": "3개월", "h": "6개월", "y": "12개월", "a": "역대"}
ALL_KEYS = [k for k, _ in LOW_PERIODS] + ["a"]   # a = 역대(상장 이후 전체)

ZONE = 0.15        # 범위의 아래쪽 15% 이내면 저점권
A_ZONE = 0.30      # 역대 최저가보다 30% 이내면 역대 저점 근처 (범위가 너무 넓어서 비율로 봐요)
MIN_AGE_A = 90     # 상장한 지 90일이 안 된 코인은 역대 판정을 하지 않아요
REBOUND_PCT = 0.04  # 저점에서 4% 이상 올라서면 반등
REBOUND_DAYS = 14   # 저점이 최근 14일 안이어야 반등으로 봐요


# ───────────────────────── 유틸 ─────────────────────────
def sig(x):
    return float(f"{x:.6g}")


def today_kst():
    return datetime.now(KST).strftime("%Y-%m-%d")


def to_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def pos_of(p, lo):
    low, high = lo[0], lo[1]
    return 0.5 if high <= low else (p - low) / (high - low)


def in_zone(key, p, lo, age=9999):
    """그 기간에서 저점 근처인가 (역대는 최저가 대비 비율, 나머지는 범위의 아래쪽 15%)"""
    if not lo:
        return False
    if key == "a":
        return age >= MIN_AGE_A and p / lo[0] - 1 <= A_ZONE
    return pos_of(p, lo) <= ZONE


# ───────────────────────── 빗썸 API ─────────────────────────
def fetch_tickers():
    js = cs.http_json(f"{BASE}/public/ticker/ALL_KRW")
    if js.get("status") != "0000":
        raise RuntimeError(f"빗썸 응답 오류: {str(js)[:120]}")
    out = {}
    for sym, t in js["data"].items():
        if not isinstance(t, dict):
            continue
        try:
            p = float(t["closing_price"])
            if p <= 0:
                continue
            out[sym] = {"p": p, "d": float(t.get("fluctate_rate_24H", 0) or 0) / 100,
                        "v": float(t.get("acc_trade_value_24H", 0) or 0)}
        except Exception:  # 값이 비었거나 이상한 코인은 건너뛰어요
            continue
    if not out:
        raise RuntimeError("코인 목록이 비어 있어요")
    return out


def fetch_names():
    try:
        rows = cs.http_json(f"{BASE}/v1/market/all?isDetails=false")
        return {r["market"].split("-", 1)[1]: r.get("korean_name") or r["market"].split("-", 1)[1]
                for r in rows if str(r.get("market", "")).startswith("KRW-")}
    except Exception as e:
        print("한글 이름을 가져오지 못했어요:", e)
        return {}


def fetch_daily(sym):
    """[(날짜, 시가, 고가, 저가, 종가)] 오래된 것부터"""
    js = cs.http_json(f"{BASE}/public/candlestick/{sym}_KRW/24h")
    if js.get("status") != "0000":
        raise RuntimeError(f"{sym} 일봉 오류")
    rows = []
    for r in js["data"]:
        ts, o, c, h, l = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
        if l <= 0 or h <= 0:
            continue
        rows.append((datetime.fromtimestamp(ts / 1000, KST).strftime("%Y-%m-%d"), o, h, l, c))
    rows.sort(key=lambda x: x[0])
    return rows[-5000:]


# ───────────────────────── 시험용(가짜) 데이터 ─────────────────────────
DEMO_SYMBOLS = ["BTC", "ETH", "XRP", "SOL", "DOGE", "PCI", "ADA", "AVAX", "DOT", "LINK", "TRX", "BCH", "ETC",
                "LTC", "ATOM", "SUI", "APT", "ARB", "NEAR", "XLM", "SHIB", "HBAR", "EOS", "STX", "SEI", "ONDO",
                "AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ", "KKK", "LLL", "MMM", "NNN",
                "NEWA", "NEWB"]
DEMO_SHORT = {"NEWA": 60, "NEWB": 200}   # 상장한 지 얼마 안 된 코인


def demo_series(sym):
    cands = cs.demo_candles(sym, 800)[-DEMO_SHORT.get(sym, 800):]
    today = datetime.now(KST).date()
    n = len(cands)
    return [((today - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d"), c.o, c.h, c.l, c.c) for i, c in enumerate(cands)]


def demo_tickers():
    out = {}
    for i, s in enumerate(DEMO_SYMBOLS):
        rows = demo_series(s)
        out[s] = {"p": rows[-1][4], "d": rows[-1][4] / rows[-2][4] - 1, "v": 1e9 * (i + 1)}
    return out


# ───────────────────────── 통계 ─────────────────────────
def make_stats(rows, today):
    n = len(rows)
    if n < 2:
        return None
    st = {"date": today, "n": n, "close": {}, "low": {}, "high": {}, "ldate": {}, "hdate": {}}
    for key, k in CLOSE_REFS:
        if n > k:
            st["close"][key] = rows[-1 - k][4]
    for key, P in LOW_PERIODS:
        if n >= P:  # 상장한 지 그 기간이 안 된 코인은 그 기간을 비워 둬요
            win = rows[-P:]
            lo = min(win, key=lambda r: r[3])
            hi = max(win, key=lambda r: r[2])
            st["low"][key], st["ldate"][key] = lo[3], lo[0]
            st["high"][key], st["hdate"][key] = hi[2], hi[0]
    lo = min(rows, key=lambda r: r[3])   # 역대 = 받은 일봉 전체
    hi = max(rows, key=lambda r: r[2])
    st["low"]["a"], st["ldate"]["a"] = lo[3], lo[0]
    st["high"]["a"], st["hdate"]["a"] = hi[2], hi[0]
    return st


def judge(p, coin):
    """저점 판정: NEWLOW > REBOUND > LOW > SHORT > HIGH > MID"""
    lo, ch, age = coin["lo"], coin["ch"], coin.get("age", 0)
    for key in ("a", "y", "h", "q", "m", "w"):
        if key == "a" and age < MIN_AGE_A:
            continue
        l = lo.get(key)
        if l and l[2] == 0 and p <= l[0] * 1.0005:
            return "NEWLOW", f"{PERIOD_LABEL[key]} 최저가를 새로 썼어요"
    d3 = ch.get("d3")
    for key in ("q", "h", "y"):
        l = lo.get(key)
        if l and 0 < l[2] <= REBOUND_DAYS and p / l[0] - 1 >= REBOUND_PCT and pos_of(p, l) <= 0.35 \
                and d3 is not None and d3 > 0:
            return "REBOUND", f"{PERIOD_LABEL[key]} 저점에서 +{(p / l[0] - 1) * 100:.0f}% 반등"
    if in_zone("a", p, lo.get("a"), age):
        return "LOW", "역대 저점 근처"
    zone = [PERIOD_LABEL[k][:-2] for k in ("q", "h", "y") if in_zone(k, p, lo.get(k))]
    if zone:
        return "LOW", "·".join(zone) + "개월 저점 근처"
    short = [PERIOD_LABEL[k] for k in ("w", "m") if in_zone(k, p, lo.get(k))]
    if short:
        return "SHORT", "·".join(short) + " 저점 근처"
    if lo.get("q") and pos_of(p, lo["q"]) >= 0.85:
        return "HIGH", "고점 근처"
    return "MID", "저점과 고점 사이"


def build_coin(sym, name, tk, stat, today):
    p = tk["p"]
    coin = {"m": "KRW-" + sym, "n": name, "s": sym, "p": sig(p), "v": round(tk["v"]),
            "ch": {"d": round(tk["d"], 5)}, "lo": {}, "pl": {}, "tag": "NONE", "tg": "데이터 준비 중",
            "age": (stat or {}).get("n", 0)}
    if not stat:
        return coin
    for key, _ in CLOSE_REFS:
        ref = stat["close"].get(key)
        coin["ch"][key] = round(p / ref - 1, 5) if ref else None
    today_d = to_date(today)
    for key in ALL_KEYS:
        if key not in stat["low"]:
            continue
        low, high = stat["low"][key], stat["high"][key]
        ld = stat["ldate"][key]
        if p < low:  # 하루 안에 새 최저가를 쓴 경우: 이전 저점을 기억해 두고 갱신
            coin["pl"][key] = sig(low)
            low, ld = p, today
            stat["low"][key], stat["ldate"][key] = low, ld
        if p > high:
            high = p
            stat["high"][key], stat["hdate"][key] = high, today
        coin["lo"][key] = [sig(low), sig(high), max(0, (today_d - to_date(ld)).days)]
    coin["tag"], coin["tg"] = judge(p, coin)
    return coin


# ───────────────────────── 전체 만들기 ─────────────────────────
def get_names(st, today, demo):
    c = st.get("names")
    if c and c.get("date") == today and c.get("map"):
        return c["map"]
    m = {s: f"데모{s}" for s in DEMO_SYMBOLS} if demo else fetch_names()
    if m:
        st["names"] = {"date": today, "map": m}
        return m
    return c["map"] if c else {}


def build(cfg, st, demo=False, budget=100):
    """(코인 목록, 오래된 데이터 여부)"""
    today = today_kst()
    ust = st.setdefault("ustats", {})
    try:
        tick = demo_tickers() if demo else fetch_tickers()
    except Exception as e:
        print("전체 시세를 가져오지 못했어요:", e)
        return st.get("last_coins", []), True
    names = get_names(st, today, demo)
    min_v = cfg.get("min_trade_value_24h", 0) or 0
    syms = [s for s, t in tick.items() if t["v"] >= min_v]

    todo = [s for s in syms if (ust.get(s) or {}).get("date") != today or "a" not in (ust.get(s) or {}).get("low", {})]
    fetch = (lambda s: demo_series(s)) if demo else fetch_daily
    deadline = time.time() + budget
    done = 0
    for i in range(0, len(todo), 40):
        if time.time() > deadline:
            print(f"일봉 갱신 시간이 다 돼서 {len(todo) - i}개는 다음 실행에서 이어서 받아요")
            break
        batch = todo[i:i + 40]
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda s: _safe(fetch, s), batch))
        for s, rows in zip(batch, results):
            if rows:
                stat = make_stats(rows, today)
                if stat:
                    ust[s] = stat
                    done += 1
    if todo:
        print(f"일봉 갱신: {done}/{len(todo)}개")

    coins = [build_coin(s, names.get(s, s), tick[s], ust.get(s), today) for s in syms]
    for s in list(ust):  # 상장폐지된 코인 정리
        if s not in tick:
            ust.pop(s, None)
    coins.sort(key=lambda c: (c["ch"].get("m") is None, c["ch"].get("m") or 0))
    st["last_coins"] = coins
    return coins, False


def _safe(fn, s):
    try:
        return fn(s)
    except Exception as e:
        print(f"{s}: 일봉 실패 {str(e)[:60]}")
        return None
