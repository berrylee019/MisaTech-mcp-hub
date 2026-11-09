MisaTech MCP 허브 배포 요약
- 시작 명령: python main.py   (Render가 PORT를 지정)
- 환경변수: KAKAO_REST_API_KEY(하남미사), NEIS_KEY(학교), TOUR_KEY(관광지), HOLIDAY_KEY(공휴일)
- 엔드포인트: https://<허브주소>/<id>/mcp  id = hanam, calc, dog, baby, event, date, school, tour, holiday
- 헬스체크: https://<허브주소>/health  (UptimeRobot은 이 주소 1개만 등록)
