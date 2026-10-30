# 코인 신호판

업비트·빗썸 시세를 10분마다 점검해서
- 신호가 바뀌면 텔레그램으로 알림
- 폰에서 볼 수 있는 대시보드(GitHub Pages) 갱신

설정은 config.json 에서 바꿔요 (종목, 급등락 기준, 목표가·손절가).
GitHub 에 필요한 비밀 값은 저장소 Settings > Secrets and variables > Actions 에 넣어요.
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

내 PC 에서 돌리고 싶다면:
  python monitor.py --loop 300
(텔레그램을 쓰려면 환경변수 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 를 설정해야 해요)

참고용 신호이며 수익을 보장하지 않아요. 이 프로그램은 주문을 넣지 않아요.
