MisaTech MCP 허브 배포 요약
- 시작 명령: python main.py   (Render가 PORT를 지정)
- 환경변수: KAKAO_REST_API_KEY(하남미사), NEIS_KEY(학교), TOUR_KEY(관광지), HOLIDAY_KEY(공휴일)
- 엔드포인트: https://<허브주소>/<id>/mcp  id = hanam, calc, dog, baby, event, date, school, tour, holiday, leave, lease, world, space, body, unemploy, brain
- 헬스체크: https://<허브주소>/health  (UptimeRobot은 이 주소 1개만 등록)
- 선택 환경변수 WIKI_CONTACT: 위키미디어 User-Agent용 연락처(이메일 등, world 서비스)
- 선택 환경변수 UB_UPPER, UB_LOWER: 실업급여 1일 상한액·하한액(원, 매년 8월 고시 후 갱신; 없으면 2026년 값 68100/66048)
- 선택 환경변수 GAME_SALT: 두뇌 게임(숫자 야구) 정답 생성용 비밀 문자열(아무 긴 문자열; 없으면 기본값으로 동작)
