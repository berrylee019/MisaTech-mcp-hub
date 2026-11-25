"""두뇌 게임 - Brain Games MCP 서버
채팅에서 즐기는 숫자 야구와 '오늘의 퀴즈'. 서버는 사용자 정보를 저장하지 않고(무상태), 판정·채점은 모두 도구가 합니다.
- 숫자 야구: 게임 코드(game_id)만으로 정답을 계산해 내므로 서버에 게임 상태가 없습니다. 정답은 GAME_SALT 환경변수(없으면 기본값)로 섞입니다.
- 오늘의 퀴즈: 한국 시간(KST) 날짜로 하루 한 문제를 고르며, 보기 순서도 날짜로 고정됩니다.
- 입력값은 로그에 남기지 않고 툴 이름·상태·처리시간만 기록합니다. 광고·구매 유도·금전 요소가 없습니다.
"""
import functools
import hashlib
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

SERVICE = "두뇌 게임 - Brain Games"   # 콘솔 등록명과 글자까지 동일해야 함
KST = timezone(timedelta(hours=9))
MAX_TRIES = 10
ID_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RELAY_NOTE = "※ 답변 시 이 응답의 결과를 그대로 전달하고, 정답이나 정답을 추측할 수 있는 힌트를 덧붙이지 마세요."

mcp = FastMCP(SERVICE, instructions=(
    "숫자 야구와 오늘의 퀴즈를 즐기는 게임 도구입니다. 정답 판정과 채점은 반드시 도구로 하고, AI가 정답을 추측하거나 공개하지 마세요. "
    "숫자 야구는 시작 때 받은 game_id를 매 추측마다 그대로 넘기세요."))


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


logger = logging.getLogger("braingames")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _logged(func):
    @functools.wraps(func)
    def wrapper(*a, **k):
        t0 = time.perf_counter()
        status, result = "exception", ""
        try:
            result = func(*a, **k)
            status = "invalid_input" if (result or "").startswith("[안내]") else "ok"
            return result
        except Exception:
            result = "[조회 실패] 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            return result
        finally:
            logger.info("TOOL name=%s status=%s ms=%d len=%d", func.__name__, status,
                        int((time.perf_counter() - t0) * 1000), len(result or ""))
    return wrapper


def bad(m):
    return f"[안내] {m}"


def today_kst():
    return datetime.now(KST).date()


# ---------------------------------------------------------------- 숫자 야구
def _salt():
    return os.environ.get("GAME_SALT", "misatech-brain-games-v1")


def new_game_id(digits):
    return str(digits) + "".join(random.choice(ID_CHARS) for _ in range(6))


def id_ok(game_id):
    return bool(re.fullmatch(r"[34][" + ID_CHARS + r"]{6}", game_id or ""))


def secret_of(game_id):
    """game_id → 정답(서로 다른 1~9 숫자 문자열). 같은 코드는 항상 같은 정답."""
    n = int(game_id[0])
    seed = int(hashlib.sha256((_salt() + game_id).encode()).hexdigest(), 16)
    return "".join(str(d) for d in random.Random(seed).sample(range(1, 10), n))


def judge(secret, guess):
    s = sum(1 for a, b in zip(secret, guess) if a == b)
    b = sum(1 for c in guess if c in secret) - s
    return s, b


@mcp.tool(
    name="start_baseball_game",
    description=(
        f"Starts a Number Baseball (숫자 야구) game with {SERVICE}. The server picks a secret number of distinct digits 1-9 (3 or 4 digits) and returns a game_id. "
        "The player guesses; use guess_baseball for every guess and pass the same game_id. Never reveal or guess the secret yourself. "
        "Keywords: 숫자 야구, 야구 게임, 숫자 맞히기, 게임 시작, 두뇌 게임."
    ),
    annotations=_annotations("Start number baseball"),
)
@_logged
def start_baseball_game(
    digits: Annotated[int, Field(description="Number of digits in the secret: 3 (default) or 4")] = 3,
) -> str:
    if digits not in (3, 4):
        return bad("자릿수는 3 또는 4로 정해 주세요(기본 3).")
    gid = new_game_id(digits)
    return "\n".join([
        f"■ 숫자 야구 시작! — {digits}자리, 게임 코드 {gid}",
        f"· 규칙: 1~9 중 서로 다른 숫자 {digits}개가 정답입니다. 추측한 숫자가 자리까지 맞으면 스트라이크, 숫자만 맞고 자리가 다르면 볼입니다.",
        f"· 최대 {MAX_TRIES}번 안에 맞히면 성공! 숫자 {digits}개(예: {'123' if digits == 3 else '1234'})를 말해 주세요.",
        f"· 매 추측마다 게임 코드 {gid}와 몇 번째 시도인지를 함께 도구에 전달하세요.",
        RELAY_NOTE])


@mcp.tool(
    name="guess_baseball",
    description=(
        f"Judges one guess in a Number Baseball game of {SERVICE}: returns strikes (right digit, right place) and balls (right digit, wrong place). "
        "Pass the game_id from start_baseball_game, the guess (distinct digits 1-9, same length as the secret) and the attempt number (1 for the first guess). "
        "Always call this tool to judge, never judge by yourself. Keywords: 숫자 야구 추측, 스트라이크 볼, 정답 확인."
    ),
    annotations=_annotations("Judge a baseball guess"),
)
@_logged
def guess_baseball(
    game_id: Annotated[str, Field(description="Game code from start_baseball_game, e.g. 3K7M2QX")] = "",
    guess: Annotated[str, Field(description="The guess: distinct digits 1-9, e.g. 123 or 1234")] = "",
    attempt: Annotated[int, Field(description="Which attempt this is, starting from 1")] = 1,
) -> str:
    game_id = (game_id or "").strip().upper()
    guess = (guess or "").strip()
    if not id_ok(game_id):
        return bad("게임 코드를 확인해 주세요. 새 게임은 start_baseball_game으로 시작할 수 있습니다.")
    n = int(game_id[0])
    if not re.fullmatch(r"[1-9]{%d}" % n, guess) or len(set(guess)) != n:
        return bad(f"서로 다른 1~9 숫자 {n}개를 입력해 주세요. 예: {'123' if n == 3 else '1234'}")
    if not 1 <= attempt <= MAX_TRIES:
        attempt = min(max(attempt, 1), MAX_TRIES)
    secret = secret_of(game_id)
    s, b = judge(secret, guess)
    if s == n:
        return "\n".join([f"■ 정답입니다! 🎉 {guess} — {attempt}번 만에 성공했어요.", "· 새 게임은 start_baseball_game으로 시작할 수 있습니다.", RELAY_NOTE])
    left = MAX_TRIES - attempt
    result = "아웃" if s == 0 and b == 0 else f"{s}스트라이크 {b}볼"
    out = [f"■ {attempt}번째 시도: {guess} → {result}"]
    if left <= 0:
        out += [f"· {MAX_TRIES}번을 모두 사용했습니다. 정답은 {secret}였어요. 새 게임에 도전해 보세요!"]
    else:
        out += [f"· 남은 기회 {left}번", RELAY_NOTE]
    return "\n".join(out)


@mcp.tool(
    name="reveal_baseball_answer",
    description=(
        f"Reveals the secret of a Number Baseball game in {SERVICE}. Use it only when the player explicitly gives up. "
        "Keywords: 숫자 야구 포기, 정답 알려줘, 항복."
    ),
    annotations=_annotations("Reveal baseball answer"),
)
@_logged
def reveal_baseball_answer(
    game_id: Annotated[str, Field(description="Game code from start_baseball_game")] = "",
) -> str:
    game_id = (game_id or "").strip().upper()
    if not id_ok(game_id):
        return bad("게임 코드를 확인해 주세요.")
    return f"■ 정답은 {secret_of(game_id)}였습니다. 새 게임은 start_baseball_game으로 시작할 수 있어요."


# ---------------------------------------------------------------- 오늘의 퀴즈
# (문제, 정답, [오답 3개], 해설) — 보기 순서는 날짜로 고정해 섞는다
QUIZ = [
    ("한글을 창제한 조선의 왕은?", "세종대왕", ["태종", "정조", "영조"], "세종대왕이 1443년 창제하고 1446년 훈민정음으로 반포했습니다."),
    ("태양계에서 가장 큰 행성은?", "목성", ["토성", "해왕성", "지구"], "목성은 태양계에서 가장 큰 행성입니다."),
    ("1기압에서 물이 끓는 온도는?", "100℃", ["90℃", "110℃", "120℃"], "1기압에서 물의 끓는점은 100℃입니다."),
    ("식물이 광합성을 위해 흡수하는 기체는?", "이산화탄소", ["산소", "질소", "수소"], "식물은 이산화탄소와 물, 빛으로 포도당과 산소를 만듭니다."),
    ("대한민국 영토에서 가장 높은 산은?", "한라산", ["설악산", "지리산", "북한산"], "한라산은 해발 1,947m입니다."),
    ("독도가 속한 행정구역은?", "경상북도", ["강원도", "전라남도", "경상남도"], "독도는 경상북도 울릉군에 속합니다."),
    ("1592년 학익진 전법으로 일본 수군을 크게 이긴 해전은?", "한산도 대첩", ["노량 해전", "행주 대첩", "진주 대첩"], "이순신 장군이 한산도 앞바다에서 승리했습니다."),
    ("지구에서 가장 큰 바다(대양)는?", "태평양", ["대서양", "인도양", "북극해"], "태평양은 지구 표면의 약 3분의 1을 차지합니다."),
    ("성인의 몸에 있는 뼈는 모두 몇 개?", "206개", ["106개", "156개", "306개"], "어른의 뼈는 보통 206개입니다."),
    ("빛의 삼원색은?", "빨강·초록·파랑", ["빨강·노랑·파랑", "청록·자홍·노랑", "빨강·초록·노랑"], "빛은 빨강·초록·파랑을 섞어 여러 색을 냅니다. 청록·자홍·노랑은 색료의 삼원색입니다."),
    ("1, 1, 2, 3, 5 다음에 오는 피보나치 수는?", "8", ["7", "9", "10"], "앞의 두 수를 더하면 3+5=8입니다."),
    ("송편을 먹는 대표적인 명절은?", "추석", ["설날", "단오", "정월대보름"], "추석에는 햅쌀로 빚은 송편을 먹습니다."),
    ("24절기 중 낮이 가장 긴 절기는?", "하지", ["동지", "춘분", "추분"], "하지는 북반구에서 낮이 가장 긴 날입니다."),
    ("물의 화학식은?", "H2O", ["CO2", "O2", "NaCl"], "물은 수소 2개와 산소 1개로 이루어집니다."),
    ("소금의 주성분은?", "염화나트륨", ["염화칼슘", "탄산나트륨", "황산마그네슘"], "소금은 염화나트륨(NaCl)이 주성분입니다."),
    ("광복절은 몇 월 며칠?", "8월 15일", ["3월 1일", "6월 25일", "10월 3일"], "1945년 8월 15일 광복을 기념하는 날입니다."),
    ("한글날은 몇 월 며칠?", "10월 9일", ["10월 3일", "9월 9일", "10월 1일"], "훈민정음 반포를 기념하는 날입니다."),
    ("어린이날은 몇 월 며칠?", "5월 5일", ["5월 1일", "5월 8일", "5월 15일"], "어린이날은 매년 5월 5일입니다."),
    ("지구가 한 번 자전하는 데 걸리는 시간은 약 얼마?", "24시간", ["12시간", "30일", "365일"], "자전 한 번이 하루입니다."),
    ("사람의 몸에서 가장 큰 기관은?", "피부", ["간", "폐", "심장"], "피부가 가장 큰 기관이고, 몸속 장기 중에서는 간이 가장 큽니다."),
    ("세계에서 가장 높은 산은?", "에베레스트", ["K2", "킬리만자로", "후지산"], "에베레스트는 해발 약 8,849m입니다."),
    ("달이 지구를 한 바퀴 도는 데 걸리는 시간은 약 얼마?", "약 1개월", ["약 1주일", "약 하루", "약 1년"], "달의 공전 주기는 약 27~30일입니다."),
    ("무지개는 보통 몇 가지 색으로 말할까?", "7가지", ["5가지", "6가지", "8가지"], "빨주노초파남보 7가지입니다."),
    ("고려를 세운 사람은?", "왕건", ["이성계", "궁예", "견훤"], "왕건이 918년 고려를 세웠습니다."),
    ("조선을 세운 사람은?", "이성계", ["왕건", "이방원", "정몽주"], "이성계가 1392년 조선을 세웠습니다."),
    ("3·1 운동이 일어난 해는?", "1919년", ["1910년", "1945년", "1950년"], "1919년 3월 1일 전국에서 독립 만세 운동이 일어났습니다."),
    ("한국 전쟁(6·25)이 시작된 해는?", "1950년", ["1945년", "1953년", "1960년"], "1950년 6월 25일에 시작되었습니다."),
    ("서울 올림픽이 열린 해는?", "1988년", ["1986년", "1992년", "2002년"], "1988년 서울에서 하계 올림픽이 열렸습니다."),
    ("2002 FIFA 월드컵을 한국과 함께 개최한 나라는?", "일본", ["중국", "독일", "브라질"], "2002 월드컵은 한국과 일본이 공동 개최했습니다."),
    ("현재 한글의 기본 모음은 몇 개?", "10개", ["8개", "12개", "14개"], "ㅏㅑㅓㅕㅗㅛㅜㅠㅡㅣ 10개입니다."),
    ("지구 대기에서 가장 많은 기체는?", "질소", ["산소", "이산화탄소", "아르곤"], "대기의 약 78%가 질소입니다."),
    ("공기 중에서 소리의 속도는 약 얼마?", "초속 340m", ["초속 34m", "초속 3,400m", "초속 30만km"], "상온 공기에서 소리는 초속 약 340m로 갑니다."),
    ("직각삼각형에서 두 변이 3과 4일 때 빗변의 길이는?", "5", ["6", "7", "8"], "3²+4²=25이므로 빗변은 5입니다."),
    ("대한민국의 나라꽃은?", "무궁화", ["벚꽃", "장미", "국화"], "무궁화는 대한민국의 나라꽃입니다."),
    ("태극기의 네 모서리에 있는 괘는 몇 개?", "4개", ["3개", "5개", "8개"], "건·곤·감·리 4괘가 있습니다."),
    ("가장 작은 소수는?", "2", ["0", "1", "3"], "2는 가장 작은 소수이고 유일한 짝수 소수입니다."),
    ("광년은 무엇을 재는 단위일까?", "거리", ["시간", "속도", "질량"], "광년은 빛이 1년 동안 가는 거리입니다."),
    ("윤년에 2월은 며칠까지 있을까?", "29일", ["28일", "30일", "27일"], "윤년에는 2월 29일이 있습니다."),
    ("혈액에서 산소를 운반하는 성분은?", "헤모글로빈", ["인슐린", "콜라겐", "케라틴"], "적혈구 속 헤모글로빈이 산소를 운반합니다."),
    ("세계에서 가장 작은 나라는?", "바티칸 시국", ["모나코", "산마리노", "리히텐슈타인"], "바티칸 시국은 면적 약 0.44㎢입니다."),
    ("오스트레일리아(호주)의 수도는?", "캔버라", ["시드니", "멜버른", "브리즈번"], "수도는 캔버라입니다."),
    ("캐나다의 수도는?", "오타와", ["토론토", "밴쿠버", "몬트리올"], "수도는 오타와입니다."),
    ("브라질의 수도는?", "브라질리아", ["리우데자네이루", "상파울루", "살바도르"], "수도는 브라질리아입니다."),
    ("튀르키예(터키)의 수도는?", "앙카라", ["이스탄불", "이즈미르", "부르사"], "수도는 앙카라입니다."),
    ("피카소의 대표작으로 전쟁의 비극을 그린 작품은?", "게르니카", ["모나리자", "별이 빛나는 밤", "절규"], "게르니카는 1937년 작품입니다."),
    ("베토벤 교향곡 9번의 별칭은?", "합창", ["운명", "전원", "영웅"], "9번에는 합창이 들어가 '합창 교향곡'이라 부릅니다."),
    ("윷놀이에서 윷가락은 몇 개?", "4개", ["2개", "3개", "5개"], "윷가락 4개를 던져 도·개·걸·윷·모를 가립니다."),
]
EPOCH = date(2026, 1, 1)


def quiz_index(d):
    return (d - EPOCH).days % len(QUIZ)


def quiz_choices(d):
    q, ans, wrongs, _ = QUIZ[quiz_index(d)]
    opts = [ans] + list(wrongs)
    random.Random(f"{d.isoformat()}|{q}").shuffle(opts)
    return opts


def parse_day(s):
    s = (s or "").strip()
    if not s:
        return today_kst()
    for f in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


@mcp.tool(
    name="get_daily_quiz",
    description=(
        f"Returns today's quiz of {SERVICE}: one general-knowledge question with four numbered choices (한국 시간 기준 하루 한 문제). The answer is not included. "
        "Show the question and choices to the player and use check_daily_quiz to grade the answer. Never reveal or guess the answer yourself. "
        "Keywords: 오늘의 퀴즈, 퀴즈 풀기, 상식 퀴즈, 두뇌 게임."
    ),
    annotations=_annotations("Today's quiz"),
)
@_logged
def get_daily_quiz(
    day: Annotated[str, Field(description="Optional date YYYY-MM-DD (KST); empty means today")] = "",
) -> str:
    d = parse_day(day)
    if d is None or d > today_kst() + timedelta(days=1):
        return bad("날짜는 YYYY-MM-DD 형식으로 입력해 주세요(비우면 오늘의 퀴즈).")
    if d < EPOCH:
        return bad("2026-01-01 이후 날짜만 지원합니다.")
    q = QUIZ[quiz_index(d)][0]
    opts = quiz_choices(d)
    lines = [f"■ 오늘의 퀴즈 ({d.isoformat()})", f"Q. {q}"] + [f"{i}. {o}" for i, o in enumerate(opts, 1)]
    lines += ["· 1~4번 중 하나를 골라 알려 주세요. 채점은 check_daily_quiz로 합니다.", RELAY_NOTE]
    return "\n".join(lines)


@mcp.tool(
    name="check_daily_quiz",
    description=(
        f"Grades an answer to the daily quiz of {SERVICE}. Pass the choice number 1-4 (and the quiz date if it is not today). Returns right or wrong and a short explanation. "
        "Always call this tool to grade. Keywords: 퀴즈 정답 확인, 채점, 오늘의 퀴즈 정답."
    ),
    annotations=_annotations("Grade daily quiz"),
)
@_logged
def check_daily_quiz(
    choice: Annotated[int, Field(description="Choice number 1-4")] = 0,
    day: Annotated[str, Field(description="Optional quiz date YYYY-MM-DD (KST); empty means today")] = "",
) -> str:
    d = parse_day(day)
    if d is None or d < EPOCH or d > today_kst() + timedelta(days=1):
        return bad("날짜는 2026-01-01 이후 YYYY-MM-DD 형식으로 입력해 주세요(비우면 오늘).")
    if choice not in (1, 2, 3, 4):
        return bad("1~4번 중 하나의 번호로 답해 주세요.")
    q, ans, _, exp = QUIZ[quiz_index(d)]
    opts = quiz_choices(d)
    ok = opts[choice - 1] == ans
    head = "정답입니다! 🎉" if ok else f"아쉬워요. 정답은 {opts.index(ans) + 1}번 {ans}입니다."
    return "\n".join([f"■ {d.isoformat()} 퀴즈 — {head}", f"Q. {q}", f"· 해설: {exp}", RELAY_NOTE])


@mcp.tool(
    name="get_games_reference",
    description=(
        f"Explains the games and rules of {SERVICE}: Number Baseball rules and limits, and how the daily quiz works. "
        "Use it when the user asks what games are available or how to play. Keywords: 두뇌 게임 규칙, 숫자 야구 규칙, 게임 종류."
    ),
    annotations=_annotations("Games and rules"),
)
@_logged
def get_games_reference() -> str:
    return "\n".join([
        "■ 두뇌 게임 안내",
        "■ 숫자 야구: 1~9 중 서로 다른 숫자 3개(또는 4개)가 정답. 자리까지 맞으면 스트라이크, 숫자만 맞으면 볼. 기회는 최대 10번. start_baseball_game으로 시작, guess_baseball로 추측.",
        "■ 오늘의 퀴즈: 한국 시간 기준 하루 한 문제, 4지선다. get_daily_quiz로 문제를, check_daily_quiz로 채점.",
        f"■ 문제 은행은 현재 {len(QUIZ)}문제이며 날짜에 따라 순환합니다.",
        "■ 판정과 채점은 모두 서버가 하고, 게임 기록이나 입력값은 저장하지 않습니다. 금전·광고·구매 요소가 없는 무료 게임입니다."])


@mcp.custom_route("/health", methods=["GET", "HEAD"])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


@mcp.custom_route("/", methods=["GET", "HEAD"])
async def root(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


try:
    app = mcp.http_app(path="/mcp", stateless_http=True)
except TypeError:
    app = mcp.http_app(path="/mcp")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
