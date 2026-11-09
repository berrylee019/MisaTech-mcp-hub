"""경조사 가이드 - Event Money Guide MCP 서버

결혼식 축의금·장례식 조의금의 최근 설문 분포, 공직자 경조사비 한도(청탁금지법), 경조사 휴가 일수를 정리해 주는 참고용 도구입니다.
- 외부 API 호출 없음(순수 정리·계산) → 응답이 빠르고, 인증/유료 요소 없음
- 통계·한도·휴가 일수는 아래 DATA 블록에만 두고, 바뀌면 이 블록과 AS_OF만 고치면 됩니다.
- 경조사 금액에는 정답이 없습니다. 모든 응답에 출처·기준 시점과 '관행·참고용' 표기를 함께 냅니다.
- 특정 상품·판매처 추천이나 구매 유도는 하지 않으며, 입력값은 서버에 저장하지 않습니다(로그에는 툴 이름·처리시간·상태만 남김).
"""
import functools
import logging
import os
import time
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

SERVICE = "경조사 가이드 - Event Money Guide"   # 콘솔 등록명과 글자 하나까지 동일해야 함(심사 DESCRIPTION_POLICY)

mcp = FastMCP(
    "경조사 가이드 - Event Money Guide",
    instructions=(
        "결혼식 축의금, 장례식 조의금, 공직자 경조사비 한도, 경조사 휴가 일수를 안내하는 참고용 도구입니다. "
        "금액에는 정답이 없으므로 응답에 표시된 출처·기준 시점·'관행' 표기를 그대로 전달하고, "
        "특정 상품이나 판매처를 추천하지 마세요. 응답에 없는 내용은 추측하지 말고 소속 기관·회사 규정 확인을 안내하세요."
    ),
)

# ================================================================ DATA (바뀌면 여기만 수정)
AS_OF = "2026-11-04"   # 자료 확인 기준일

# 조선일보 '아무튼, 주말' × 틸리언 프로 설문(2026.5, 전국 성인 1,500명)
SURVEY = "조선일보 '아무튼, 주말'×틸리언 프로 설문(2026.5, 전국 성인 1,500명)"
WEDDING = {
    "avg": 97_900,
    "dist": [("5만원 이하", 21.9), ("10만원", 65.3), ("15만원", 6.3), ("20만원", 5.7)],
    "hotel": [("10만원", 55.9), ("15만원", 18.7), ("20만원", 15.2)],
}
CONDOLENCE = {
    "avg": 95_300,
    "dist": [("5만원 이하", 25.7), ("10만원", 61.8), ("15만원", 7.2), ("20만원", 4.6)],
    "avg_by_age": {"20대": 89_000, "30대": 93_700, "40대": 94_700, "50대": 97_500, "60대 이상": 108_400},
}
# 참고: 은행·간편송금 데이터 (농협은행 2023~2025 축의금 송금 평균 11만→11.7만원, 카카오페이 송금 10주년 리포트)
BANK_NOTE = ("농협은행 축의금 송금 데이터(2023~2025)에서는 평균이 11만 원(2023)→11.7만 원(2025)이고, 5만 원 비중은 46.5%→42.3%, "
             "10만 원 비중은 36.1%→39.7%였습니다. 카카오페이 송금 데이터에서도 2023년부터 10만 원이 가장 많았습니다.")

# 청탁금지법 시행령 별표 1(공직자등 대상): 경조사비(축의금·조의금) 5만원, 다만 축의금·조의금을 대신하는 화환·조화는 10만원
OFFICIAL_LIMIT = {"경조사비": 50_000, "화환·조화": 100_000}

# 국가공무원 복무규정 별표 2 경조사휴가 일수
LEAVE_PUBLIC = {
    "본인 결혼": 5, "자녀 결혼": 1, "배우자 출산": 20, "배우자 사망": 5,
    "부모 사망": 5, "조부모 사망": 3, "자녀 사망": 3, "형제자매 사망": 3,
}
LEAVE_NOTES = {
    "부모 사망": "본인 및 배우자의 부모",
    "조부모 사망": "본인 및 배우자의 조부모·외조부모",
    "형제자매 사망": "본인 및 배우자의 형제자매",
}
LEAVE_CAUTION = {}   # 휴가 일수 안내가 자료마다 다를 때만 항목별 주의 문구를 넣습니다
CLOSENESS = ("가까운 사이", "보통", "가벼운 관계")
VENUES = ("호텔", "일반", "모름")
AGE_GROUPS = ("20대", "30대", "40대", "50대", "60대 이상")

RELAY_NOTE = "※ 답변 시 위 수치·출처·기준 시점을 그대로 전달하고, 이 응답에 없는 금액이나 일수를 덧붙이지 마세요."
DISCLAIMER = "경조사 금액은 관계·지역·형편에 따라 달라지는 관행이며 정답이 없습니다. 참고용이고 법률 자문이 아닙니다."


# ================================================================ 공통
def won(n) -> str:
    return f"{int(round(n)):,}원"


def dist_text(dist) -> str:
    return ", ".join(f"{k} {v:g}%" for k, v in dist)


def _annotations(title: str) -> dict:
    # PlayMCP 필수: 5개 속성 모두 지정. 외부 호출이 없으므로 openWorldHint=False
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


logger = logging.getLogger("eventguide")
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


# ================================================================ Tool 1: 축의금
@mcp.tool(
    name="get_wedding_gift_guide",
    description=(
        f"Gives guidance on how much wedding gift money (축의금) people commonly give in Korea, using {SERVICE}. "
        "Returns the latest survey distribution (average, most common amounts, hotel-venue distribution), a reference range "
        "based on closeness ('가까운 사이', '보통', '가벼운 관계') and venue ('호텔', '일반', '모름'), and the sources with their dates. "
        "Amounts are social customs, not rules; the reference range is a rule of thumb derived from the survey, not an official statistic. "
        "Always call this tool instead of answering from memory, because the figures come from specific dated sources. "
        "No product or store recommendations."
    ),
    annotations=_annotations("Wedding gift money guide"),
)
@_logged
def get_wedding_gift_guide(
    closeness: Annotated[str, Field(description="One of '가까운 사이', '보통', '가벼운 관계'")] = "보통",
    venue: Annotated[str, Field(description="Wedding venue: '호텔', '일반', or '모름'")] = "모름",
    attending: Annotated[bool, Field(description="True if the person will attend in person")] = True,
) -> str:
    if closeness not in CLOSENESS:
        return bad(f"closeness는 {', '.join(CLOSENESS)} 중 하나로 입력해 주세요.")
    if venue not in VENUES:
        return bad(f"venue는 {', '.join(VENUES)} 중 하나로 입력해 주세요.")
    w = WEDDING
    out = [f"■ 결혼식 축의금 가이드 (자료 확인일 {AS_OF})",
           f"【설문 분포】 {SURVEY}",
           f"· 평균 {won(w['avg'])}, 금액별 비중: {dist_text(w['dist'])}",
           f"· 호텔 예식 응답: {dist_text(w['hotel'])}"]
    if closeness == "보통":
        rng = "10만 원 (설문에서 가장 많이 낸 금액, 65.3%)"
    elif closeness == "가까운 사이":
        rng = "10만~20만 원 (관계가 가까울수록 최빈 금액 이상으로 올리는 경우가 있음. 15만·20만 원 합계 12%)"
    else:
        rng = "5만~10만 원 (5만 원 이하가 21.9%, 업무상·가벼운 관계에서 주로 쓰임)"
    if venue == "호텔":
        rng += " / 호텔 예식은 식대 부담이 커서 10만~15만 원 이상이 흔함(호텔 응답 중 15만 원 이상 33.9%)"
    out.append(f"【참고 범위】 {closeness}, 장소 {venue}: {rng}")
    out.append("※ 참고 범위는 설문 분포를 바탕으로 정리한 선택 요령이며, 관계별 공식 통계가 아닙니다.")
    if not attending:
        out.append("※ 참석하지 않을 때의 별도 통계는 확보하지 못했습니다. 관계와 식대 부담을 기준으로 정하세요.")
    out.append(f"【참고】 {BANK_NOTE}")
    out.append(DISCLAIMER)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 2: 조의금
@mcp.tool(
    name="get_condolence_gift_guide",
    description=(
        f"Gives guidance on how much condolence money (조의금, 부의금) people commonly give at Korean funerals, using {SERVICE}. "
        "Returns the latest survey distribution (average, most common amounts, average by the giver's age group), a reference range "
        "based on closeness ('가까운 사이', '보통', '가벼운 관계'), envelope wording tips, and the sources with their dates. "
        "Amounts are social customs, not rules; the reference range is a rule of thumb derived from the survey. "
        "Always call this tool instead of answering from memory, because the figures come from specific dated sources. "
        "Respectful, no product recommendations."
    ),
    annotations=_annotations("Condolence money guide"),
)
@_logged
def get_condolence_gift_guide(
    closeness: Annotated[str, Field(description="One of '가까운 사이', '보통', '가벼운 관계'")] = "보통",
    giver_age_group: Annotated[str, Field(description="Giver's age group: '20대','30대','40대','50대','60대 이상', or empty")] = "",
) -> str:
    if closeness not in CLOSENESS:
        return bad(f"closeness는 {', '.join(CLOSENESS)} 중 하나로 입력해 주세요.")
    if giver_age_group and giver_age_group not in AGE_GROUPS:
        return bad(f"giver_age_group은 {', '.join(AGE_GROUPS)} 중 하나이거나 비워 주세요.")
    c = CONDOLENCE
    out = [f"■ 장례식 조의금 가이드 (자료 확인일 {AS_OF})",
           f"【설문 분포】 {SURVEY}",
           f"· 평균 {won(c['avg'])}, 금액별 비중: {dist_text(c['dist'])}",
           "· 연령대별 평균: " + ", ".join(f"{k} {won(v)}" for k, v in c["avg_by_age"].items())]
    if giver_age_group:
        out.append(f"· 입력하신 연령대({giver_age_group}) 평균: {won(c['avg_by_age'][giver_age_group])}")
    if closeness == "보통":
        rng = "10만 원 (설문에서 가장 많이 낸 금액, 61.8%)"
    elif closeness == "가까운 사이":
        rng = "10만~20만 원 (15만·20만 원 합계 11.8%)"
    else:
        rng = "5만~10만 원 (5만 원 이하가 25.7%)"
    out.append(f"【참고 범위】 {closeness}: {rng}")
    out.append("※ 참고 범위는 설문 분포를 바탕으로 정리한 선택 요령이며, 관계별 공식 통계가 아닙니다.")
    out.append("【봉투 표기】 겉봉에는 '부의(賻儀)', '근조(謹弔)', '조의(弔意)' 등을 쓰고, 뒷면에 이름을 적는 것이 일반적입니다. 종교·지역별 관습은 다를 수 있습니다.")
    out.append(DISCLAIMER)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 3: 공직자 경조사비 한도
@mcp.tool(
    name="get_official_gift_limits",
    description=(
        f"Explains the Korean Improper Solicitation and Graft Act (청탁금지법) ceilings on congratulatory and condolence money "
        f"for public officials and covered persons, using {SERVICE}: 50,000 KRW for cash 경조사비 (축의금·조의금), and 100,000 KRW for wreaths "
        "(화환·조화) given in place of that cash. "
        "Use it when the user is a public official, teacher, journalist or other covered person, or gives money to one in a job-related setting. "
        "Always call this tool instead of answering from memory, because the figures come from specific dated sources. "
        "These ceilings do not apply to ordinary private relationships. Reference only, not legal advice."
    ),
    annotations=_annotations("Official gift limits"),
)
@_logged
def get_official_gift_limits() -> str:
    o = OFFICIAL_LIMIT
    return "\n".join([
        f"■ 공직자등 경조사비 한도 — 청탁금지법 (자료 확인일 {AS_OF})",
        f"· 경조사비(축의금·조의금): 1인당 {won(o['경조사비'])} 이내",
        f"· 화환·조화: 축의금·조의금을 대신하는 경우 {won(o['화환·조화'])} 이내",
        "· 적용 대상: 공직자등(공무원, 공공기관 임직원, 교직원, 언론사 임직원 등)이 직무와 관련해 주고받는 경우",
        "· 일반 개인 간 경조사(친구·친척 등)에는 이 한도가 적용되지 않습니다. 같은 조직 내 상하 관계 등 예외는 사안별로 다를 수 있습니다.",
        "※ 한도 조정 여부는 국민권익위원회 청탁금지법 안내(clean.go.kr)에서 최신 내용을 확인하세요.",
        "법률 자문이 아닌 참고용 안내입니다.", RELAY_NOTE])


# ================================================================ Tool 4: 경조사 휴가
@mcp.tool(
    name="get_condolence_leave_days",
    description=(
        f"Returns the number of special leave days for family events in Korea, using {SERVICE}. "
        "Events: '본인 결혼', '자녀 결혼', '배우자 출산', '배우자 사망', '부모 사망', '조부모 사망', '자녀 사망', '형제자매 사망'. "
        "For central-government employees it gives the days from the National Civil Servant Service Regulations (Saturdays and "
        "public holidays are not counted); for private-sector employees there is no general statutory congratulatory/condolence leave, "
        "Always call this tool instead of answering from memory, because the figures come from specific dated sources. "
        "so company rules apply, except spouse childbirth leave which is statutory. Reference only, not legal advice."
    ),
    annotations=_annotations("Event leave days"),
)
@_logged
def get_condolence_leave_days(
    event: Annotated[str, Field(description="One of '본인 결혼','자녀 결혼','배우자 출산','배우자 사망','부모 사망','조부모 사망','자녀 사망','형제자매 사망'")],
    sector: Annotated[str, Field(description="'공무원' or '민간'")] = "민간",
) -> str:
    if event not in LEAVE_PUBLIC:
        return bad("event는 " + ", ".join(LEAVE_PUBLIC) + " 중 하나로 입력해 주세요.")
    if sector not in ("공무원", "민간"):
        return bad("sector는 '공무원' 또는 '민간' 중 하나로 입력해 주세요.")
    days = LEAVE_PUBLIC[event]
    note = LEAVE_NOTES.get(event, "")
    out = [f"■ 경조사 휴가 — {event} ({sector}) (자료 확인일 {AS_OF})"]
    if sector == "공무원":
        out.append(f"· 국가공무원 복무규정 별표 2 기준: {days}일" + (f" ({note})" if note else ""))
        out.append("· 휴가 기간 중 토요일과 공휴일은 일수에 넣지 않습니다. 원거리일 때는 왕복에 필요한 일수를 기관 판단으로 더할 수 있습니다.")
        out.append("· 지방공무원은 결혼·출산·입양은 같지만 사망 휴가는 소속 자치단체 조례를 확인해야 합니다.")
    else:
        if event == "배우자 출산":
            out.append("· 배우자 출산휴가는 법정 휴가(유급 20일, 출산일부터 120일 이내, 최대 3회 분할)입니다. 근거: 남녀고용평등과 일·가정 양립 지원에 관한 법률. 세부는 고용노동부 안내를 확인하세요.")
        else:
            out.append(f"· 민간 사업장에는 '{event}'에 대한 법정 경조사 휴가가 없습니다. 취업규칙·단체협약·사내 규정에 따릅니다.")
            out.append(f"· 참고로 국가공무원 기준은 {days}일입니다(회사마다 다릅니다).")
    if event in LEAVE_CAUTION:
        out.append(LEAVE_CAUTION[event])
    out.append("※ 실제 적용은 법제처 국가법령정보센터 원문과 소속 기관·회사 규정으로 확인하세요.")
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 5: 기준·출처
@mcp.tool(
    name="get_guide_reference",
    description=(
        f"Returns the sources, survey dates and assumptions behind the figures in {SERVICE}: wedding and condolence money surveys, "
        "public-official ceilings and civil servant leave days. Use it when the user asks where the numbers come from or how reliable they are. "
        "Reference only, not legal advice."
    ),
    annotations=_annotations("Guide sources"),
)
@_logged
def get_guide_reference() -> str:
    return "\n".join([
        f"■ 경조사 가이드 기준 (자료 확인일 {AS_OF})",
        f"■ 축의금·조의금 분포: {SURVEY}. 조사 시점이 최근이지만 표본·문항에 따라 결과가 달라질 수 있습니다.",
        f"■ 참고 데이터: {BANK_NOTE}",
        "■ 공직자 한도: 청탁금지법 시행령 별표 1(경조사비 5만 원, 축의금·조의금을 대신하는 화환·조화 10만 원)",
        "■ 경조사 휴가: 국가공무원 복무규정 별표 2, 남녀고용평등과 일·가정 양립 지원에 관한 법률(배우자 출산휴가)",
        "■ '참고 범위'는 설문 분포를 바탕으로 정리한 선택 요령이며, 관계별 공식 통계가 아닙니다. 지역·세대·조직 문화에 따라 다를 수 있습니다.",
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
