#!/usr/bin/env python3
"""
코인 매수/매도 시점 포착기 (업비트 · 빗썸 공개 시세 API, 무료, API 키 불필요)

설치: Python 3.8+ 만 있으면 됩니다. 추가 패키지 설치 없음.

사용법
  python coin_signal.py scan                         # 한 번 점검
  python coin_signal.py watch --every 60             # 60초마다 반복 점검, 신호가 바뀌면 알림
  python coin_signal.py backtest KRW-BTC             # 과거 데이터로 전략 검증
  python coin_signal.py scan --exchange bithumb      # 빗썸 기준
  python coin_signal.py scan --markets KRW-BTC KRW-ETH KRW-XRP --unit 240
  python coin_signal.py scan --demo                  # 인터넷 없이 가짜 데이터로 시험

주의: 지표 신호는 참고용일 뿐 수익을 보장하지 않습니다. 이 프로그램은 주문을 넣지 않고
      '알려주기'만 합니다. 실제 투자는 본인 책임이며, 먼저 backtest 로 확인하세요.
"""
import argparse
import json
import math
import random
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections import namedtuple
from datetime import datetime

# ───────────────────────── 설정 ─────────────────────────
DEFAULT_MARKETS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE"]
FEE = 0.0025          # 한 번 거래당 수수료(0.25% = 보수적). 거래소 실제 수수료에 맞게 조정하세요.
BUY_TH = 4            # 점수가 이 이상이면 '매수 후보'
SELL_TH = -4          # 점수가 이 이하이면 '매도 후보'
MIN_BARS = 35         # 지표 계산에 필요한 최소 캔들 수
UA = {"User-Agent": "coin-signal/1.0"}
SSL_CTX = None        # None = 기본 인증서 검증 사용

BASES = {"upbit": "https://api.upbit.com", "bithumb": "https://api.bithumb.com"}
Candle = namedtuple("Candle", "ts o h l c v")


# ───────────────────────── 데이터 가져오기 ─────────────────────────
def http_json(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(0.6 * (i + 1))  # 요청 제한(429) 등은 잠깐 쉬고 재시도


def fetch_v1(base, market, unit, total):
    """업비트 형식(/v1/candles/minutes/{unit})의 분봉. 빗썸 v1도 같은 형식."""
    rows_all, to = [], None
    while len(rows_all) < total:
        n = min(200, total - len(rows_all))
        q = {"market": market, "count": n}
        if to:
            q["to"] = to
        rows = http_json(f"{base}/v1/candles/minutes/{unit}?" + urllib.parse.urlencode(q))
        if not rows:
            break
        rows_all.extend(rows)
        to = rows[-1]["candle_date_time_utc"] + "Z"  # 가장 오래된 캔들 이전부터 이어서
        time.sleep(0.15)
        if len(rows) < n:
            break
    seen, out = set(), []
    for r in reversed(rows_all):  # 과거 → 최신 순서로
        if r["timestamp"] in seen:
            continue
        seen.add(r["timestamp"])
        out.append(Candle(r["timestamp"], r["opening_price"], r["high_price"],
                          r["low_price"], r["trade_price"], r["candle_acc_trade_volume"]))
    return out


def fetch_bithumb_legacy(market, unit, total):
    """빗썸 구형 공개 API (v1 이 안 될 때 대비). 데이터 순서: [시각, 시가, 종가, 고가, 저가, 거래량]"""
    names = {1: "1m", 3: "3m", 5: "5m", 10: "10m", 30: "30m", 60: "1h",
             360: "6h", 720: "12h", 1440: "24h"}
    if unit not in names:
        raise ValueError(f"빗썸 구형 API 는 unit={sorted(names)} 만 지원합니다.")
    quote, coin = market.split("-")
    js = http_json(f"{BASES['bithumb']}/public/candlestick/{coin}_{quote}/{names[unit]}")
    if js.get("status") != "0000":
        raise RuntimeError(f"빗썸 응답 오류: {js}")
    rows = js["data"][-total:]
    return [Candle(int(r[0]), float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5])) for r in rows]


def demo_candles(market, total):
    """인터넷 없이 시험용 가짜 데이터 (무작위 + 추세 변화)."""
    rnd = random.Random(sum(map(ord, market)))
    price, out, drift = 1000.0, [], 0.0
    for i in range(total):
        if i % 80 == 0:
            drift = rnd.uniform(-0.004, 0.004)
        o = price
        price = max(1.0, price * (1 + drift + rnd.gauss(0, 0.008)))
        h, l = max(o, price) * 1.003, min(o, price) * 0.997
        out.append(Candle(i, o, h, l, price, rnd.random() * 100))
    return out


def get_candles(exchange, market, unit, total, demo=False):
    if demo:
        return demo_candles(market, total)
    if exchange == "bithumb":
        try:
            c = fetch_v1(BASES["bithumb"], market, unit, total)
            if len(c) >= MIN_BARS:
                return c
        except Exception:
            pass
        return fetch_bithumb_legacy(market, unit, total)
    return fetch_v1(BASES["upbit"], market, unit, total)


# ───────────────────────── 지표 계산 ─────────────────────────
def sma(v, n):
    out, s = [None] * len(v), 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(v, n):
    out, k, prev = [None] * len(v), 2 / (n + 1), None
    for i, x in enumerate(v):
        if i == n - 1:
            prev = sum(v[:n]) / n
            out[i] = prev
        elif i >= n:
            prev = x * k + prev * (1 - k)
            out[i] = prev
    return out


def ema_skip_none(v, n):
    idx = [i for i, x in enumerate(v) if x is not None]
    out = [None] * len(v)
    if len(idx) < n:
        return out
    e = ema([v[i] for i in idx], n)
    for j, i in enumerate(idx):
        out[i] = e[j]
    return out


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def bollinger(c, n=20, k=2.0):
    mid = sma(c, n)
    up, lo = [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        sd = math.sqrt(sum((x - mid[i]) ** 2 for x in w) / n)
        up[i], lo[i] = mid[i] + k * sd, mid[i] - k * sd
    return mid, up, lo


def compute(candles):
    c = [x.c for x in candles]
    e12, e26 = ema(c, 12), ema(c, 26)
    macd = [None if a is None or b is None else a - b for a, b in zip(e12, e26)]
    sig = ema_skip_none(macd, 9)
    hist = [None if m is None or s is None else m - s for m, s in zip(macd, sig)]
    mid, up, lo = bollinger(c)
    return {"c": c, "rsi": rsi(c), "hist": hist, "bb_up": up, "bb_lo": lo,
            "ma5": sma(c, 5), "ma20": mid}


# ───────────────────────── 신호 점수 ─────────────────────────
def score_at(ind, i):
    """i번째 캔들 기준 점수와 이유. +면 매수 쪽, -면 매도 쪽."""
    if i < MIN_BARS or i >= len(ind["c"]):
        return 0, []
    price, r = ind["c"][i], ind["rsi"][i]
    h, hp = ind["hist"][i], ind["hist"][i - 1]
    m5, m20, m5p, m20p = ind["ma5"][i], ind["ma20"][i], ind["ma5"][i - 1], ind["ma20"][i - 1]
    if None in (r, h, hp, m5, m20, m5p, m20p, ind["bb_up"][i], ind["bb_lo"][i]):
        return 0, []

    s, why = 0, []
    # 1) RSI: 과매도/과매수
    if r < 30:   s += 2; why.append(f"RSI {r:.0f} 과매도")
    elif r < 40: s += 1; why.append(f"RSI {r:.0f} 낮음")
    elif r > 70: s -= 2; why.append(f"RSI {r:.0f} 과매수")
    elif r > 60: s -= 1; why.append(f"RSI {r:.0f} 높음")
    # 2) MACD 히스토그램: 0선 교차 / 방향
    if hp <= 0 < h:   s += 2; why.append("MACD 상향 돌파")
    elif hp >= 0 > h: s -= 2; why.append("MACD 하향 돌파")
    elif h > 0 and h > hp: s += 1; why.append("MACD 상승 강화")
    elif h < 0 and h < hp: s -= 1; why.append("MACD 하락 강화")
    # 3) 볼린저밴드: 밴드 이탈
    if price < ind["bb_lo"][i]:   s += 2; why.append("볼린저 하단 이탈")
    elif price > ind["bb_up"][i]: s -= 2; why.append("볼린저 상단 돌파")
    # 4) 이동평균 교차 (5 vs 20)
    if m5p <= m20p and m5 > m20:   s += 2; why.append("골든크로스(5/20)")
    elif m5p >= m20p and m5 < m20: s -= 2; why.append("데드크로스(5/20)")
    return s, why


def label(score):
    if score >= BUY_TH:  return "🟢 매수 후보"
    if score <= SELL_TH: return "🔴 매도 후보"
    return "⚪ 관망"


# ───────────────────────── 명령: scan / watch ─────────────────────────
def scan_once(args, prev=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now}] {args.exchange} · {args.unit}분봉 점검")
    print(f"{'종목':<10}{'현재가':>14}{'RSI':>6}{'점수':>5}  신호        이유")
    print("-" * 78)
    result, alert = {}, False
    for m in args.markets:
        try:
            ind = compute(get_candles(args.exchange, m, args.unit, 200, args.demo))
            s, why = score_at(ind, len(ind["c"]) - 1)
        except Exception as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                raise  # 종목마다 같은 오류를 반복해서 보여주지 않고 main 에서 안내합니다
            print(f"{m:<10} 데이터 오류: {e}")
            continue
        tag = label(s)
        result[m] = tag
        r = ind["rsi"][-1]
        print(f"{m:<10}{ind['c'][-1]:>14,.2f}{(f'{r:.0f}' if r else '-'):>6}{s:>5}  {tag:<9} {', '.join(why) or '-'}")
        if prev is not None and prev.get(m) != tag and tag != "⚪ 관망":
            print(f"   🔔 알림: {m} → {tag}")
            alert = True
    if alert:
        sys.stdout.write("\a")
    return result


def cmd_scan(args):
    scan_once(args)


def cmd_watch(args):
    prev = scan_once(args)
    try:
        while True:
            time.sleep(args.every)
            prev = scan_once(args, prev)
    except KeyboardInterrupt:
        print("\n종료합니다.")


# ───────────────────────── 명령: backtest ─────────────────────────
def run_backtest(candles, stop=0.05):
    ind = compute(candles)
    c, pos, entry, equity, trades = ind["c"], False, 0.0, 1.0, []
    for i in range(MIN_BARS, len(c)):
        s, _ = score_at(ind, i)
        p = c[i]
        if not pos:
            if s >= BUY_TH:
                pos, entry = True, p
        else:
            if s <= SELL_TH or p <= entry * (1 - stop):
                ret = (p / entry) * (1 - FEE) ** 2 - 1
                equity *= 1 + ret
                trades.append(ret)
                pos = False
    if pos:  # 마지막에 보유 중이면 현재가로 정리
        ret = (c[-1] / entry) * (1 - FEE) ** 2 - 1
        equity *= 1 + ret
        trades.append(ret)
    hold = c[-1] / c[MIN_BARS] - 1
    return equity - 1, hold, trades


def cmd_backtest(args):
    m = args.market
    n = args.bars
    print(f"{args.exchange} {m} {args.unit}분봉 {n}개 불러오는 중…")
    candles = get_candles(args.exchange, m, args.unit, n, args.demo)
    print(f"받은 캔들: {len(candles)}개")
    if len(candles) < MIN_BARS + 10:
        print("데이터가 너무 적어서 검증할 수 없어요.")
        return
    strat, hold, trades = run_backtest(candles, args.stop)
    wins = sum(1 for t in trades if t > 0)
    print("\n── 백테스트 결과 ──")
    print(f"거래 횟수      : {len(trades)}회")
    if trades:
        print(f"승률           : {wins / len(trades) * 100:.1f}%  (평균 {sum(trades) / len(trades) * 100:+.2f}% / 회)")
    print(f"전략 수익률    : {strat * 100:+.2f}%  (수수료 {FEE * 100:.2f}% 반영, 손절 {args.stop * 100:.0f}%)")
    print(f"그냥 보유 수익률: {hold * 100:+.2f}%")
    verdict = "전략이 보유보다 나았어요 (그래도 미래를 보장하진 않아요)." if strat > hold \
        else "이 구간에서는 그냥 보유가 더 나았어요. 기준 점수를 조정해 보세요."
    print(verdict)
    print("※ 신호가 나온 캔들의 종가로 체결됐다고 가정해서 실제보다 낙관적일 수 있어요.")


# ───────────────────────── 실행 ─────────────────────────
def setup_ssl(insecure):
    global SSL_CTX
    if insecure:
        SSL_CTX = ssl._create_unverified_context()
        print("⚠ SSL 인증서 검증을 끄고 실행합니다 (공개 시세 조회 전용).")
        return
    try:  # pip install truststore 를 해 두었다면 윈도우 인증서 저장소를 그대로 사용
        import truststore
        SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        SSL_CTX = None


def main():
    ap = argparse.ArgumentParser(description="코인 매수/매도 시점 포착기 (공개 시세 API)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--exchange", choices=["upbit", "bithumb"], default="upbit")
    common.add_argument("--unit", type=int, default=60, help="분봉 단위 (1,3,5,10,15,30,60,240 등) 기본 60")
    common.add_argument("--demo", action="store_true", help="가짜 데이터로 시험 (인터넷 불필요)")
    common.add_argument("--insecure", action="store_true",
                        help="SSL 인증서 검증 끄기 (회사/보안프로그램 환경에서 인증서 오류가 날 때만 사용)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("scan", parents=[common], help="한 번 점검")
    p1.add_argument("--markets", nargs="+", default=DEFAULT_MARKETS)
    p1.set_defaults(fn=cmd_scan)

    p2 = sub.add_parser("watch", parents=[common], help="반복 점검 + 신호 바뀌면 알림")
    p2.add_argument("--markets", nargs="+", default=DEFAULT_MARKETS)
    p2.add_argument("--every", type=int, default=60, help="점검 간격(초), 기본 60")
    p2.set_defaults(fn=cmd_watch)

    p3 = sub.add_parser("backtest", parents=[common], help="과거 데이터로 전략 검증")
    p3.add_argument("market", help="예: KRW-BTC")
    p3.add_argument("--bars", type=int, default=1000, help="검증할 캔들 개수, 기본 1000")
    p3.add_argument("--stop", type=float, default=0.05, help="손절 비율, 기본 0.05 (=5%%)")
    p3.set_defaults(fn=cmd_backtest)

    args = ap.parse_args()
    setup_ssl(args.insecure)
    try:
        args.fn(args)
    except Exception as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            print("\n❌ SSL 인증서 검증 오류예요. 회사·학교 보안 프로그램이 인터넷 연결을 중간에서 검사하는 환경에서 자주 생겨요.")
            print("   같은 명령 뒤에 --insecure 를 붙여 다시 실행해 보세요.")
            print("   예) python coin_signal.py scan --insecure")
            print("   (이 프로그램은 공개 시세만 읽고 로그인·주문을 하지 않아서 위험이 낮지만,")
            print("    가능하면 집 같은 일반 네트워크에서는 --insecure 없이 쓰세요.)")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
