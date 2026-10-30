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

KST = timezone(timedelta(hours=9))
SITE, WEB, STATE_DIR = "site", "web", "state"
STATE_FILE = os.path.join(STATE_DIR, "state.json")

NAMES = {
    "BTC": "비트코인", "ETH": "이더리움", "XRP": "리플", "SOL": "솔라나", "DOGE": "도지코인",
    "ADA": "에이다", "AVAX": "아발란체", "DOT": "폴카닷", "LINK": "체인링크", "TRX": "트론",
    "POL": "폴리곤", "MATIC": "폴리곤", "BCH": "비트코인캐시", "ETC": "이더리움클래식",
    "LTC": "라이트코인", "ATOM": "코스모스", "SUI": "수이", "APT": "앱토스", "ARB": "아비트럼",
    "NEAR": "니어프로토콜", "XLM": "스텔라루멘", "SHIB": "시바이누", "HBAR": "헤데라",
    "USDT": "테더", "EOS": "이오스", "STX": "스택스", "SEI": "세이", "ONDO": "온도파이낸스",
}

DEFAULTS = {
    "exchange": "upbit",
    "fallback_exchange": "bithumb",
    "unit": 60,
    "markets": ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE"],
    "buy_threshold": 4,
    "sell_threshold": -4,
    "fee": 0.0025,
    "dashboard_url": "",
    "alerts": {"signal_change": True, "spike": True, "spike_pct": 5, "spike_minutes": 60,
               "targets": True, "cooldown_minutes": 180},
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


def fmt_pct(x):
    return f"{x * 100:+.1f}%"


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
    return st


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


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
def candles_for(cfg, market, unit, count, demo):
    if demo:  # 시험용: 같은 가짜 시세에서 잘라 쓰기
        return cs.demo_candles(market, 200)[-count:], "demo"
    order = [cfg["exchange"]]
    if cfg.get("fallback_exchange") and cfg["fallback_exchange"] != cfg["exchange"]:
        order.append(cfg["fallback_exchange"])
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

    # 모든 종목 실패 → 한 번 알려주기(6시간에 한 번)
    if markets and all(x["status"] == "error" for x in markets):
        if cooldown_ok(st, "err", now, 360):
            alerts.append("⚠️ 시세를 가져오지 못했어요. 거래소 연결이나 종목 이름을 확인해 주세요.\n" + markets[0].get("error", ""))
            st["last"]["err"] = iso(now)

    st["init"] = True
    st["history"] = st["history"][-30:]
    save_state(st)

    # 텔레그램 전송 (많으면 묶어서)
    url = dashboard_url(cfg)
    tail = f"\n\n📊 {url}" if url else ""
    if alerts:
        if len(alerts) <= 3:
            for t in alerts:
                tg_send(t + tail)
        else:
            tg_send("\n\n".join(alerts[:8]) + tail)
    if os.environ.get("EVENT_NAME") == "workflow_dispatch":
        lines = [f"{x['name']} {fmt_price(x['price'])}원 {x['signal']}" for x in markets if x["status"] == "ok"]
        tg_send("✅ 코인 신호판 연결 확인\n" + ("\n".join(lines) or "시세를 가져오지 못했어요") + tail)

    data = {
        "updated": iso(now),
        "updated_kst": now.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
        "exchange": cfg["exchange"], "unit": cfg["unit"],
        "thresholds": {"buy": cs.BUY_TH, "sell": cs.SELL_TH},
        "markets": markets,
        "alerts": list(reversed(st["history"]))[:20],
    }
    publish(data)
    return data


def publish(data):
    os.makedirs(SITE, exist_ok=True)
    if os.path.isdir(WEB):
        shutil.copytree(WEB, SITE, dirs_exist_ok=True)
    tmp = os.path.join(SITE, "data.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(SITE, "data.json"))


def print_table(data):
    print(f"\n[{data['updated_kst']} KST] {data['exchange']} {data['unit']}분봉")
    for m in data["markets"]:
        if m["status"] != "ok":
            print(f"  {m['market']:<10} 오류: {m.get('error', '')}")
            continue
        print(f"  {m['market']:<10} {fmt_price(m['price']):>14}원  1h {fmt_pct(m['chg']):>7}  "
              f"{m['signal']:<5} 점수 {m['score']:+d}  {', '.join(m['reasons']) or '-'}")


def main():
    ap = argparse.ArgumentParser(description="코인 신호 모니터")
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 시험")
    ap.add_argument("--loop", type=int, default=0, help="N초마다 반복 실행 (PC에서 쓸 때)")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    cfg = load_config(args.config)
    while True:
        print_table(run_once(cfg, args.demo))
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
