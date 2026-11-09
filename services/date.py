"""기념일 일정 도우미 - Date Helper MCP 서버

연인 100일 단위·주년, 아기 삼칠일·백일·돌, 결혼기념일 명칭과 다음 날짜, 임의 날짜 D-day를 계산해 주는 참고용 도구입니다.
- 외부 API 호출 없음(순수 날짜 계산) → 응답이 빠르고, 인증/유료 요소 없음
- 셈법(만난 날을 1일로 셈 등)과 명칭 표는 아래 블록에만 두고, 응답에 항상 적용한 기준을 함께 표시합니다.
- 음력 변환은 검증 가능한 공식 자료로 대조하기 전까지 넣지 않았습니다(양력 계산 전용).
- 입력한 날짜는 서버에 저장하지 않으며, 로그에는 툴 이름·처리시간·상태만 남깁니다.
"""
import functools
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

SERVICE = "기념일 일정 도우미 - Date Helper"   # 콘솔 등록명과 글자 하나까지 동일해야 함(심사 DESCRIPTION_POLICY)
KST = timezone(timedelta(hours=9))

mcp = FastMCP(
    "기념일 일정 도우미 - Date Helper",
    instructions=(
        "연인 100일·주년, 아기 삼칠일·백일·돌, 결혼기념일 명칭, 임의 날짜 D-day를 양력 날짜로 계산하는 참고용 도구입니다. "
        "날짜가 없으면 먼저 물어보고, 응답에 표시된 셈법·기준일을 그대로 전달하세요. 음력 날짜 변환은 지원하지 않습니다. "
        "응답의 날짜 목록은 캘린더나 메모로 옮기기 쉬운 형식이니 그대로 보여 주세요."
    ),
)

# ================================================================ 기준 (바뀌면 여기만 수정)
AS_OF = "2026-11-04"
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

# 결혼기념일 명칭: 서양 풍습에서 유래했으며 자료마다 다를 수 있음 (표준국어대사전·한국일생의례사전 등에 실린 명칭)
WEDDING_NAMES = {
    1: "지혼식(紙婚式, 종이)",
    5: "목혼식(木婚式, 나무)",
    10: "석혼식(錫婚式, 주석)",
    15: "수정혼식(水晶婚式, 수정) — 자료에 따라 동혼식(銅婚式, 구리)",
    20: "도혼식(陶婚式, 도자기)",
    25: "은혼식(銀婚式, 은)",
    30: "진주혼식(眞珠婚式, 진주) — 자료에 따라 상아혼식(象牙婚式)",
    35: "산호혼(珊瑚婚, 산호)",
    40: "녹옥혼(綠玉婚, 녹옥)",
    45: "홍옥혼(紅玉婚, 홍옥)",
    50: "금혼식(金婚式, 금)",
    60: "회혼례(回婚禮) — 자료에 따라 금강혼식(金剛婚式, 다이아몬드)",
}

DISCLAIMER = "참고용 날짜 계산입니다. 기념일을 세는 방식은 사람마다 다를 수 있어 적용한 셈법을 함께 표시했습니다."
RELAY_NOTE = "※ 답변 시 위 날짜·셈법·기준일을 그대로 전달하고, 이 응답에 없는 날짜를 덧붙이지 마세요."


# ================================================================ 공통
def parse_date(s: str):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def today_kst() -> date:
    return datetime.now(KST).date()


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:   # 2월 29일 → 평년은 2월 28일
        return d.replace(year=d.year + n, day=28)


def wd(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def fmt(d: date) -> str:
    return f"{d.isoformat()}({wd(d)})"


def dday(d: date, today: date) -> str:
    n = (d - today).days
    if n == 0:
        return "D-DAY(오늘)"
    return f"D-{n}" if n > 0 else f"D+{-n}"


def _annotations(title: str) -> dict:
    # PlayMCP 필수: 5개 속성 모두 지정. 외부 호출이 없으므로 openWorldHint=False
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


logger = logging.getLogger("datehelper")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _logged(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        status, result = "exception", ""
        try:
            result = func(*args, **kwargs)
            status = "invalid_input" if (result or "").startswith("[입력 확인]") else "ok"
            return result
        finally:
            ms = int((time.perf_counter() - t0) * 1000)
            logger.info("TOOL name=%s status=%s ms=%d len=%d", func.__name__, status, ms, len(result or ""))
    return wrapper


def bad(msg: str) -> str:
    return f"[입력 확인] {msg}"


def get_today(as_of_date: str):
    if (as_of_date or "").strip():
        t = parse_date(as_of_date)
        if t is None:
            return None, bad("기준일은 YYYY-MM-DD 형식으로 입력해 주세요(비우면 오늘).")
        return t, None
    return today_kst(), None


# ================================================================ Tool 1: 연인 기념일
def couple_events(start: date, first_day_one: bool):
    off = -1 if first_day_one else 0
    ev = []
    for n in range(100, 3001, 100):
        ev.append((start + timedelta(days=n + off), f"{n}일"))
    for y in range(1, 51):
        ev.append((add_years(start, y), f"{y}주년"))
    return sorted(ev, key=lambda x: (x[0], x[1]))


@mcp.tool(
    name="get_couple_milestones",
    description=(
        f"Calculates a couple's anniversary dates in Korea (100-day milestones such as 100일, 200일, 1000일, and yearly anniversaries "
        f"like 1주년), using {SERVICE}. Given the day they got together (YYYY-MM-DD), it lists the next upcoming milestones with "
        "weekday and D-day, plus the most recent ones that just passed. By Korean convention the first day counts as day 1 "
        "(so 100일 is start date + 99 days); the user can switch this off. Solar calendar only. "
        "Always call this tool instead of calculating by hand, because the counting convention is applied consistently. "
        "Ask the user for the start date if missing. Reference calculation only."
    ),
    annotations=_annotations("Couple milestones"),
)
@_logged
def get_couple_milestones(
    start_date: Annotated[str, Field(description="Day the couple got together, YYYY-MM-DD")],
    count: Annotated[int, Field(description="How many upcoming milestones to list, 1-20", ge=1, le=20)] = 6,
    first_day_counts_as_one: Annotated[bool, Field(description="True (Korean convention): the start date is day 1")] = True,
    as_of_date: Annotated[str, Field(description="Reference date YYYY-MM-DD; empty means today (KST)")] = "",
) -> str:
    start = parse_date(start_date)
    if start is None:
        return bad("만난 날을 YYYY-MM-DD 형식으로 입력해 주세요. 예: 2025-03-15")
    today, err = get_today(as_of_date)
    if err:
        return err
    if start > today:
        return bad("만난 날이 기준일보다 미래입니다.")
    ev = couple_events(start, first_day_counts_as_one)
    past = [e for e in ev if e[0] < today]
    upcoming = [e for e in ev if e[0] >= today]
    days_together = (today - start).days + (1 if first_day_counts_as_one else 0)
    out = [f"■ 커플 기념일 — 만난 날 {fmt(start)}, 오늘은 {days_together}일째 (기준일 {fmt(today)})",
           "【앞으로】"]
    out += [f"· {fmt(d)} {name} — {dday(d, today)}" for d, name in upcoming[:count]] or ["· 없음"]
    if past:
        out.append("【최근 지난 기념일】")
        out += [f"· {fmt(d)} {name} — {dday(d, today)}" for d, name in past[-2:]]
    conv = "만난 날을 1일로 셈(100일 = 만난 날 + 99일)" if first_day_counts_as_one else "만난 날을 0일로 셈(100일 = 만난 날 + 100일)"
    out.append(f"【셈법】 {conv}. 주년은 같은 월·일(2월 29일은 평년 2월 28일). 양력 기준.")
    out.append(DISCLAIMER)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 2: 아기 기념일
@mcp.tool(
    name="get_baby_celebration_dates",
    description=(
        f"Calculates a baby's traditional celebration dates in Korea using {SERVICE}: 삼칠일 (21st day), 백일 (100th day), "
        "200일, 돌 (first birthday) and the next birthdays. Given the birth date (YYYY-MM-DD), it returns each date with weekday "
        "and D-day. The birth date counts as day 1 (so 백일 is birth date + 99 days). Solar calendar only. "
        "Always call this tool instead of calculating by hand, because the counting convention is applied consistently. "
        "Ask the user for the birth date if missing. Reference calculation only."
    ),
    annotations=_annotations("Baby celebration dates"),
)
@_logged
def get_baby_celebration_dates(
    birth_date: Annotated[str, Field(description="Baby's birth date, YYYY-MM-DD")],
    as_of_date: Annotated[str, Field(description="Reference date YYYY-MM-DD; empty means today (KST)")] = "",
) -> str:
    birth = parse_date(birth_date)
    if birth is None:
        return bad("출생일을 YYYY-MM-DD 형식으로 입력해 주세요. 예: 2026-05-20")
    today, err = get_today(as_of_date)
    if err:
        return err
    if birth > today + timedelta(days=300):
        return bad("출생일이 기준일보다 너무 먼 미래입니다.")
    if add_years(birth, 19) < today:
        return bad("이 도구는 만 18세 이하 아이의 기념일만 안내합니다.")
    ev = [(birth + timedelta(days=20), "삼칠일(21일째)"),
          (birth + timedelta(days=99), "백일(100일째)"),
          (birth + timedelta(days=199), "200일째"),
          (add_years(birth, 1), "돌(첫 생일)"),
          (add_years(birth, 2), "만 2세 생일"),
          (add_years(birth, 3), "만 3세 생일")]
    age_day = (today - birth).days + 1
    head = f"오늘은 {age_day}일째" if age_day >= 1 else f"출생 예정일까지 {(birth - today).days}일"
    out = [f"■ 아기 기념일 — 출생일 {fmt(birth)}, {head} (기준일 {fmt(today)})"]
    out += [f"· {fmt(d)} {name} — {dday(d, today)}" for d, name in ev]
    out.append("【셈법】 출생일을 1일로 셈(백일 = 출생일 + 99일, 삼칠일 = 출생일 + 20일). 돌·생일은 같은 월·일, 양력 기준.")
    out.append("※ 삼칠일·백일·돌은 전통 관행이며 잔치 날짜는 가족 상황에 따라 앞뒤로 조정하기도 합니다.")
    out.append(DISCLAIMER)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 3: 결혼기념일
@mcp.tool(
    name="get_wedding_anniversary",
    description=(
        f"Finds the next wedding anniversary in Korea using {SERVICE}: the number of years since the wedding date (YYYY-MM-DD), "
        "the next anniversary date with weekday and D-day, its traditional name if it has one (e.g. 지혼식 for the 1st, 은혼식 for "
        "the 25th, 금혼식 for the 50th) and the next named milestones. Names come from Western-derived customs and vary by source. "
        "Always call this tool instead of answering from memory, because names and dates are computed consistently. "
        "Ask the user for the wedding date if missing. Reference calculation only."
    ),
    annotations=_annotations("Wedding anniversary"),
)
@_logged
def get_wedding_anniversary(
    wedding_date: Annotated[str, Field(description="Wedding date, YYYY-MM-DD")],
    as_of_date: Annotated[str, Field(description="Reference date YYYY-MM-DD; empty means today (KST)")] = "",
) -> str:
    wed = parse_date(wedding_date)
    if wed is None:
        return bad("결혼일을 YYYY-MM-DD 형식으로 입력해 주세요. 예: 2018-10-13")
    today, err = get_today(as_of_date)
    if err:
        return err
    if wed > add_years(today, 3):
        return bad("결혼일이 기준일보다 너무 먼 미래입니다.")
    if wed > today:
        return "\n".join([f"■ 결혼 예정일 {fmt(wed)} — {dday(wed, today)} (기준일 {fmt(today)})",
                          "· 아직 결혼 전이라 기념일을 계산하지 않았습니다.", DISCLAIMER, RELAY_NOTE])
    years = today.year - wed.year - (1 if add_years(wed, today.year - wed.year) > today else 0)
    nxt_year = years + 1
    nxt = add_years(wed, nxt_year)
    out = [f"■ 결혼기념일 — 결혼일 {fmt(wed)}, 만 {years}년 (기준일 {fmt(today)})",
           f"【다음 기념일】 {fmt(nxt)} {nxt_year}주년 — {dday(nxt, today)}"
           + (f" · {WEDDING_NAMES[nxt_year]}" if nxt_year in WEDDING_NAMES else "")]
    if years in WEDDING_NAMES and add_years(wed, years) <= today:
        out.append(f"【지난 {years}주년】 {fmt(add_years(wed, years))} · {WEDDING_NAMES[years]}")
    named = [y for y in sorted(WEDDING_NAMES) if y > nxt_year][:3]
    if named:
        out.append("【앞으로의 이름 있는 기념일】")
        out += [f"· {y}주년 {fmt(add_years(wed, y))} — {WEDDING_NAMES[y]}" for y in named]
    out.append("※ 결혼기념일 명칭은 서양 풍습에서 유래했고 자료마다 일부 다릅니다.")
    out.append(DISCLAIMER)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 4: D-day
@mcp.tool(
    name="get_days_until",
    description=(
        f"Calculates D-day for any date using {SERVICE}: days until (or since) a target date (YYYY-MM-DD), with weekday. "
        "With repeat_yearly=true it finds the next yearly occurrence of that month and day (for birthdays and anniversaries) "
        "and how many years have passed. Solar calendar only. "
        "Always call this tool instead of calculating by hand. Ask the user for the date if missing. Reference calculation only."
    ),
    annotations=_annotations("D-day calculator"),
)
@_logged
def get_days_until(
    target_date: Annotated[str, Field(description="Target date, YYYY-MM-DD. For yearly events use the original date")],
    repeat_yearly: Annotated[bool, Field(description="True to find the next yearly occurrence of this month and day")] = False,
    as_of_date: Annotated[str, Field(description="Reference date YYYY-MM-DD; empty means today (KST)")] = "",
) -> str:
    target = parse_date(target_date)
    if target is None:
        return bad("날짜를 YYYY-MM-DD 형식으로 입력해 주세요. 예: 2027-01-01")
    today, err = get_today(as_of_date)
    if err:
        return err
    if not repeat_yearly:
        out = [f"■ D-day — {fmt(target)}: {dday(target, today)} (기준일 {fmt(today)})"]
    else:
        y = max(today.year, target.year)
        nxt = add_years(target, y - target.year)
        if nxt < today:
            nxt = add_years(target, y + 1 - target.year)
        k = nxt.year - target.year
        out = [f"■ 다음 {target.month}월 {target.day}일 — {fmt(nxt)}: {dday(nxt, today)} (기준일 {fmt(today)})"]
        if k > 0:
            out.append(f"· 원래 날짜 {fmt(target)} 기준 {k}주년(또는 만 {k}세)")
        out.append("· 2월 29일 기념일은 평년에 2월 28일로 계산합니다.")
    out.append(DISCLAIMER)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 5: 기준
@mcp.tool(
    name="get_date_helper_reference",
    description=(
        f"Returns the counting conventions, wedding anniversary name list and limits of {SERVICE}: how 100일 and 백일 are counted, "
        "how leap-day dates are handled, and that lunar-calendar conversion is not supported. "
        "Use it when the user asks how dates are counted or why a date differs from another app. Reference only."
    ),
    annotations=_annotations("Date helper conventions"),
)
@_logged
def get_date_helper_reference() -> str:
    names = [f"· {y}주년: {n}" for y, n in WEDDING_NAMES.items()]
    return "\n".join([
        f"■ 기념일 일정 도우미 기준 (확인일 {AS_OF})",
        "■ 연인 100일 단위: 만난 날을 1일로 셈하는 한국식(100일 = 만난 날 + 99일)이 기본이며, 0일로 셈하는 방식도 선택할 수 있습니다. 앱마다 다를 수 있습니다.",
        "■ 아기 기념일: 출생일을 1일로 셈(삼칠일 = 출생일 + 20일, 백일 = 출생일 + 99일). 돌·생일은 같은 월·일.",
        "■ 주년: 같은 월·일, 2월 29일은 평년에 2월 28일로 계산합니다.",
        "■ 음력 생일·제사 등 음력 날짜 변환은 지원하지 않습니다(양력 기준).",
        "■ 결혼기념일 명칭(서양 풍습에서 유래, 자료마다 일부 다름):", *names,
        "출처: 표준국어대사전, 국립민속박물관 한국일생의례사전 등에 실린 명칭을 정리했습니다.",
        DISCLAIMER])


# ================================================================ 서버
@mcp.custom_route("/health", methods=["GET", "HEAD"])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


@mcp.custom_route("/", methods=["GET", "HEAD"])
async def root(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


try:
    app = mcp.http_app(path="/mcp", stateless_http=True)  # PlayMCP 권장: Stateless
except TypeError:
    app = mcp.http_app(path="/mcp")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
