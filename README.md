# Dip Radar

업비트·빗썸 시세를 5분마다 점검해서
- 신호가 바뀌면 텔레그램으로 알림
- 폰에서 볼 수 있는 대시보드(GitHub Pages) 갱신

설정은 config.json 에서 바꿔요 (종목, 급등락 기준, 목표가·손절가).
GitHub 에 필요한 비밀 값은 저장소 Settings > Secrets and variables > Actions 에 넣어요.
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

내 PC 에서 돌리고 싶다면:
  python monitor.py --loop 300
(텔레그램을 쓰려면 환경변수 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 를 설정해야 해요)

참고용 신호이며 수익을 보장하지 않아요. 이 프로그램은 주문을 넣지 않아요.

## 라이선스와 고지
- 프로그램 코드는 [MIT 라이선스](LICENSE)예요. 시세·뉴스 제목·상표의 권리는 각 출처에 있어요.
- 개인이 만든 비공식 프로젝트이며 빗썸·업비트·구글 뉴스·각 언론사와 관련이 없어요.
- 자세한 출처와 제3자 라이선스는 [NOTICE.md](NOTICE.md)에 있어요.
