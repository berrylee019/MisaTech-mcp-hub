"""육아 일정 도우미 - Baby Schedule Helper MCP 서버

아이 생년월일만으로 국가예방접종 표준 시기와 영유아 건강검진·구강검진 시기를 날짜로 계산해 주는 참고용 도구입니다.
- 외부 API 호출 없음(순수 날짜 계산) → 응답이 빠르고, 인증/유료 요소 없음
- 일정표는 아래 SCHEDULE 블록에만 두고, 제도 변경 시 이 블록과 AS_OF만 고치면 됩니다.
- 의료 자문이 아니며, 접종 여부·개인 기록은 알 수 없습니다(표준 시기만 안내).
- 아이 생년월일 등 입력값은 서버에 저장하지 않으며, 로그에는 툴 이름·처리시간·상태만 남깁니다.
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

SERVICE = "육아 일정 도우미 - Baby Schedule Helper"   # 콘솔 등록명과 글자 하나까지 동일해야 함(심사 DESCRIPTION_POLICY)
KST = timezone(timedelta(hours=9))

mcp = FastMCP(
    "육아 일정 도우미 - Baby Schedule Helper",
    instructions=(
        "아이 생년월일로 국가예방접종 표준 시기와 영유아 건강검진·구강검진 시기를 날짜로 안내하는 참고용 도구입니다. "
        "생년월일을 모르면 먼저 물어보고, 응답에 표시된 기준일·가정을 그대로 전달하세요. "
        "접종 완료 여부는 알 수 없으므로 질병관리청 예방접종도우미·접종 수첩·보건소 확인을 안내하고, "
        "응답에 없는 의학적 판단은 추측하지 말고 소아청소년과 상담을 권하세요. 의료 자문이 아닙니다."
    ),
)

# ================================================================ SCHEDULE (제도 변경 시 여기만 수정)
AS_OF = "2026-11-02"   # 일정표 확인 기준일

# 시기 표기: ("point", 개월) = 해당 월령 도달 시점 / ("range", 시작개월, 끝개월) = 시작 월령 도달일 ~ 끝 월령이 끝나는 날
#            ("days", 시작일, 끝일) = 생후 일수. 연령(세)은 개월로 환산(4세=48개월).
VACCINES = [
    ("출생 시", ("point", 0), "B형간염 1차"),
    ("생후 4주 이내", ("days", 0, 28), "결핵 BCG(피내용) 1회"),
    ("생후 1개월", ("point", 1), "B형간염 2차"),
    ("생후 2개월", ("point", 2), "DTaP 1차·IPV 1차·Hib 1차·폐렴구균 1차·로타바이러스 1차(생후 14주 6일까지 시작)"),
    ("생후 4개월", ("point", 4), "DTaP 2차·IPV 2차·Hib 2차·폐렴구균 2차·로타바이러스 2차"),
    ("생후 6개월", ("point", 6), "DTaP 3차·IPV 3차·Hib 3차·폐렴구균 3차·B형간염 3차·로타바이러스 3차(3회 백신인 경우, 생후 8개월 0일까지)"),
    ("12~15개월", ("range", 12, 15), "Hib 4차·폐렴구균 4차·MMR 1차·수두 1회"),
    ("12~23개월", ("range", 12, 23), "A형간염 1~2차·일본뇌염(사백신 1~2차 또는 생백신 1차)"),
    ("15~18개월", ("range", 15, 18), "DTaP 4차"),
    ("24~35개월", ("range", 24, 35), "일본뇌염(사백신 3차 또는 생백신 2차)"),
    ("4~6세", ("range", 48, 83), "DTaP 5차·IPV 4차·MMR 2차"),
    ("6세", ("range", 72, 83), "일본뇌염 사백신 4차"),
    ("11~12세", ("range", 132, 155), "Tdap 6차"),
    ("12세", ("range", 144, 155), "일본뇌염 사백신 5차·HPV 1차(성별·출생연도별 지원 대상 확인)"),
]

CHECKUPS = [  # 영유아 건강검진 8회
    ("1차 검진", ("days", 14, 35), "영유아 건강검진 1차"),
    ("2차 검진", ("range", 4, 6), "영유아 건강검진 2차"),
    ("3차 검진", ("range", 9, 12), "영유아 건강검진 3차"),
    ("4차 검진", ("range", 18, 24), "영유아 건강검진 4차"),
    ("5차 검진", ("range", 30, 36), "영유아 건강검진 5차"),
    ("6차 검진", ("range", 42, 48), "영유아 건강검진 6차"),
    ("7차 검진", ("range", 54, 60), "영유아 건강검진 7차"),
    ("8차 검진", ("range", 66, 71), "영유아 건강검진 8차"),
]
ORAL = [  # 영유아 구강검진 4회
    ("구강검진 1회", ("range", 18, 29), "영유아 구강검진(18~29개월)"),
    ("구강검진 2회", ("range", 30, 41), "영유아 구강검진(30~41개월)"),
    ("구강검진 3회", ("range", 42, 53), "영유아 구강검진(42~53개월)"),
    ("구강검진 4회", ("range", 54, 65), "영유아 구강검진(54~65개월)"),
]
POINT_GRACE_DAYS = 30   # '생후 N개월' 시점형 항목은 도달일부터 30일간을 '지금 해당'으로 봄(표시용 기준)

DISCLAIMER = ("참고용 표준 일정 안내이며 의료 자문이 아닙니다. 접종 완료 여부는 알 수 없으니 질병관리청 예방접종도우미·"
              "접종 수첩·보건소에서 확인하고, 실제 시기는 소아청소년과·보건소·건보공단 안내를 따르세요.")


# ================================================================ 공통
def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def parse_date(s: str):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def fmt(d: date) -> str:
    return f"{d.year}.{d.month}.{d.day}"


def age_text(birth: date, today: date) -> str:
    if today < birth:
        return f"출생 예정일까지 {(birth - today).days}일"
    months = (today.year - birth.year) * 12 + today.month - birth.month
    if today.day < birth.day:
        months -= 1
    anchor = add_months(birth, months)
    days = (today - anchor).days
    if months < 24:
        return f"생후 {months}개월 {days}일"
    return f"만 {months // 12}세 {months % 12}개월"


def _annotations(title: str) -> dict:
    # PlayMCP 필수: 5개 속성 모두 지정. 외부 호출이 없으므로 openWorldHint=False
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


logger = logging.getLogger("babysched")
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


def today_kst() -> date:
    return datetime.now(KST).date()


def resolve(birth_date: str, as_of_date: str):
    """(birth, today, error) 반환"""
    birth = parse_date(birth_date)
    if birth is None:
        return None, None, bad("생년월일을 YYYY-MM-DD 형식으로 입력해 주세요. 예: 2025-03-15")
    today = parse_date(as_of_date) if (as_of_date or "").strip() else today_kst()
    if today is None:
        return None, None, bad("기준일은 YYYY-MM-DD 형식으로 입력해 주세요(비우면 오늘).")
    if birth > today + timedelta(days=300):
        return None, None, bad("생년월일이 기준일보다 너무 먼 미래입니다.")
    if add_months(birth, 12 * 19) < today:
        return None, None, bad("이 도구는 만 18세 이하 어린이·청소년 일정만 안내합니다.")
    return birth, today, None


def window(birth: date, spec):
    """(start, end, is_point) — end는 포함일"""
    kind = spec[0]
    if kind == "point":
        s = add_months(birth, spec[1])
        return s, s, True
    if kind == "days":
        return birth + timedelta(days=spec[1]), birth + timedelta(days=spec[2]), False
    s = add_months(birth, spec[1])
    e = add_months(birth, spec[2] + 1) - timedelta(days=1)
    return s, e, False


def build_events(birth: date, table, kind: str):
    ev = []
    for when, spec, what in table:
        s, e, point = window(birth, spec)
        eff_end = s + timedelta(days=POINT_GRACE_DAYS) if point else e
        ev.append({"kind": kind, "when": when, "what": what, "start": s, "end": e, "eff_end": eff_end, "point": point})
    return ev


def date_text(e) -> str:
    if e["point"]:
        return f"{fmt(e['start'])} 도달"
    return f"{fmt(e['start'])} ~ {fmt(e['end'])}"


def line(e) -> str:
    return f"· [{e['when']}] {e['what']} ({date_text(e)})"


def classify(events, today: date, lookahead: int):
    now, soon, later, past = [], [], [], []
    for e in sorted(events, key=lambda x: x["start"]):
        if e["start"] <= today <= e["eff_end"]:
            now.append(e)
        elif e["start"] > today:
            (soon if (e["start"] - today).days <= lookahead else later).append(e)
        else:
            past.append(e)
    return now, soon, later, past


def render(birth: date, today: date, events, lookahead: int, title: str, past_note: str) -> str:
    now, soon, later, past = classify(events, today, lookahead)
    out = [f"■ {title} — {age_text(birth, today)} (기준일 {fmt(today)}, 일정표 확인일 {AS_OF})"]
    out.append("【지금 해당하는 시기】")
    out += [line(e) for e in now] or ["· 없음"]
    out.append(f"【{lookahead}일 이내 시작】")
    out += [line(e) for e in soon] or ["· 없음"]
    if later:
        out.append("【그다음 예정(다음 2건)】")
        out += [line(e) for e in later[:2]]
    if past:
        recent = past[-3:]
        out.append(f"【표준 시기가 지난 항목 {len(past)}건 — 최근 {len(recent)}건】")
        out += [line(e) for e in recent]
        out.append(past_note)
    out.append(DISCLAIMER)
    return "\n".join(out)


# ================================================================ Tool 1: 예방접종
@mcp.tool(
    name="get_vaccination_schedule",
    description=(
        f"Shows the Korean national immunization (국가예방접종) standard schedule as concrete dates for a child, using {SERVICE}. "
        "Given the child's birth date (YYYY-MM-DD), it lists vaccinations due now, those starting within the next N days, "
        "the next ones after that, and recently passed standard windows (it cannot know which doses were already given). "
        "Always call this tool instead of answering from memory, because the date windows follow this service's own schedule table. "
        "Ask the user for the birth date if missing. Reference schedule, not medical advice."
    ),
    annotations=_annotations("Vaccination schedule"),
)
@_logged
def get_vaccination_schedule(
    birth_date: Annotated[str, Field(description="Child's birth date or expected due date, YYYY-MM-DD")],
    as_of_date: Annotated[str, Field(description="Reference date YYYY-MM-DD; empty means today (KST)")] = "",
    lookahead_days: Annotated[int, Field(description="How many days ahead to list upcoming vaccinations, 1-365", ge=1, le=365)] = 90,
) -> str:
    birth, today, err = resolve(birth_date, as_of_date)
    if err:
        return err
    events = build_events(birth, VACCINES, "vaccine")
    return render(birth, today, events, lookahead_days, "국가예방접종 표준 시기",
                  "※ 접종 완료 여부는 이 도구가 알 수 없습니다. 접종 수첩이나 질병관리청 예방접종도우미에서 기록을 확인하세요.")


# ================================================================ Tool 2: 영유아 검진
@mcp.tool(
    name="get_checkup_schedule",
    description=(
        f"Shows the Korean infant and child health checkup (영유아 건강검진, 8 rounds from 14 days to 71 months) and "
        f"oral exam (구강검진, 4 times) windows as concrete dates for a child, using {SERVICE}. "
        "Given the birth date (YYYY-MM-DD), it lists the checkups open now, upcoming ones and recently missed windows. "
        "Always call this tool instead of answering from memory, because the date windows follow this service's own schedule table. "
        "Ask the user for the birth date if missing. Reference schedule, not medical advice."
    ),
    annotations=_annotations("Checkup schedule"),
)
@_logged
def get_checkup_schedule(
    birth_date: Annotated[str, Field(description="Child's birth date, YYYY-MM-DD")],
    as_of_date: Annotated[str, Field(description="Reference date YYYY-MM-DD; empty means today (KST)")] = "",
    lookahead_days: Annotated[int, Field(description="How many days ahead to list upcoming checkups, 1-365", ge=1, le=365)] = 90,
) -> str:
    birth, today, err = resolve(birth_date, as_of_date)
    if err:
        return err
    events = build_events(birth, CHECKUPS, "checkup") + build_events(birth, ORAL, "oral")
    return render(birth, today, events, lookahead_days, "영유아 건강검진·구강검진 시기",
                  "※ 검진 이력은 이 도구가 알 수 없습니다. 건강검진 포털이나 건보공단(1577-1000)에서 확인하고, "
                  "기간이 지났어도 가능 여부는 공단에 문의하세요. 발달선별검사(K-DST) 포함 차수는 검진 병원에서 안내받으세요.")


# ================================================================ Tool 3: 한눈에 보는 다음 일정
@mcp.tool(
    name="get_upcoming_checklist",
    description=(
        f"Builds one date-ordered checklist of upcoming vaccinations and health checkups for a child over the next N days, "
        f"using {SERVICE}. Use it for questions like 'what should I do for my baby next month?'. "
        "Given the birth date (YYYY-MM-DD), items open now come first, then items starting within the window. "
        "Always call this tool instead of answering from memory, because the date windows follow this service's own schedule table. "
        "Ask the user for the birth date if missing. Reference schedule, not medical advice."
    ),
    annotations=_annotations("Upcoming checklist"),
)
@_logged
def get_upcoming_checklist(
    birth_date: Annotated[str, Field(description="Child's birth date, YYYY-MM-DD")],
    as_of_date: Annotated[str, Field(description="Reference date YYYY-MM-DD; empty means today (KST)")] = "",
    lookahead_days: Annotated[int, Field(description="Days ahead to include, 1-365", ge=1, le=365)] = 60,
) -> str:
    birth, today, err = resolve(birth_date, as_of_date)
    if err:
        return err
    events = (build_events(birth, VACCINES, "vaccine") + build_events(birth, CHECKUPS, "checkup")
              + build_events(birth, ORAL, "oral"))
    now, soon, _later, _past = classify(events, today, lookahead_days)
    tag = {"vaccine": "접종", "checkup": "검진", "oral": "구강"}
    out = [f"■ 다음 {lookahead_days}일 체크리스트 — {age_text(birth, today)} (기준일 {fmt(today)}, 일정표 확인일 {AS_OF})"]
    out.append("【지금 해당】")
    out += [f"☐ ({tag[e['kind']]}) {e['what']} — {date_text(e)}" for e in now] or ["· 없음"]
    out.append("【곧 시작】")
    out += [f"☐ ({tag[e['kind']]}) {e['what']} — {date_text(e)}" for e in soon] or ["· 없음"]
    out.append("※ 이미 받은 접종·검진은 이 목록에서 제외되지 않습니다. 기록은 접종 수첩·예방접종도우미에서 확인하세요.")
    out.append(DISCLAIMER)
    return "\n".join(out)


# ================================================================ Tool 4: 기준·출처
@mcp.tool(
    name="get_schedule_reference",
    description=(
        f"Returns the sources, assumptions and effective date of the schedules used by {SERVICE}: national immunization "
        "timing, infant checkup rounds and oral exam windows, plus 2026 notes (HPV, influenza). "
        "Use it when the user asks where the dates come from or how reliable they are. Reference only, not medical advice."
    ),
    annotations=_annotations("Schedule sources"),
)
@_logged
def get_schedule_reference() -> str:
    lines = [f"■ 육아 일정 도우미 기준 (일정표 확인일 {AS_OF})",
             "■ 국가예방접종: 출생 시 B형간염 1차 / 4주 내 BCG(피내용) / 1·2·4·6개월 기본접종 / 12~15개월 Hib·폐렴구균 4차·MMR·수두 / "
             "12~23개월 A형간염·일본뇌염 / 15~18개월 DTaP 4차 / 4~6세 DTaP·IPV·MMR / 11~12세 Tdap / 12세 HPV 등",
             "  '생후 N개월' 항목은 해당 월령 도달일 기준이며, 범위 항목은 시작 월령 도달일부터 끝 월령이 끝나는 날까지로 계산합니다. "
             "경계일은 기관 안내와 며칠 차이 날 수 있습니다.",
             "■ 영유아 건강검진 8회: 14~35일 / 4~6 / 9~12 / 18~24 / 30~36 / 42~48 / 54~60 / 66~71개월, "
             "구강검진 4회: 18~29 / 30~41 / 42~53 / 54~65개월",
             "■ 2026년 참고: HPV는 2014년생 12세 남아가 2026.5.6.부터 무료 대상에 포함(여아 12~17세), "
             "정부 2027년 예산안에는 9가 전환·남아 13세 확대가 포함됐으나 확정 전입니다. "
             "인플루엔자는 2026~2027 절기 생후 6개월~만 14세(2012.1.1.~2026.8.31. 출생) 무료입니다.",
             "■ 이 도구는 접종·검진 이력, 백신 종류(혼합백신 등), 미숙아·기저질환 등 개인 사정을 반영하지 않습니다.",
             "출처: 질병관리청 국가예방접종 표준일정·보도자료(2026.4.15.), 국민건강보험공단 영유아 건강검진 안내, 지자체 보건소 안내문. "
             "최종 확인: 질병관리청 예방접종도우미(nip.kdca.go.kr), 건보공단 1577-1000.",
             DISCLAIMER]
    return "\n".join(lines)


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
