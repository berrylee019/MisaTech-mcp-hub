"""실업급여 계산기 - Jobless Calc MCP 서버
회사 사정(권고사직·해고·계약만료 등)으로 퇴직하는 경우의 구직급여(실업급여) 예상 금액·소정급여일수·수급 요건·신청 절차를 계산·안내하는 참고용 도구입니다.
- 외부 API·인증키가 없고, 입력값(급여·나이·가입기간)은 저장하거나 로그에 남기지 않습니다. 로그에는 툴 이름·상태·처리시간만 남깁니다.
- 1일 상한액·하한액은 UB_UPPER, UB_LOWER 환경변수(없으면 아래 기본값)로 바꿀 수 있어, 매년 고시가 바뀌어도 코드 수정·재심사 없이 갱신합니다.
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

SERVICE = "실업급여 계산기 - Jobless Calc"   # 콘솔 등록명과 글자까지 동일해야 함
AS_OF = "2026-11-20"      # 기준값 확인 시점
DEFAULT_UPPER = 68100     # 2026년 구직급여 1일 상한액(원)
DEFAULT_LOWER = 66048     # 2026년 구직급여 1일 하한액(원) = 최저임금 10,320원 × 80% × 8시간
RATE = 0.6                # 평균임금의 60%
SOURCE = "근거: 고용보험법 제40조·제45조~제50조, 같은 법 시행령 (확인 시점 " + AS_OF + ", 2차 자료 기준)"
RELAY_NOTE = "※ 답변 시 위 금액·일수와 가정 조건을 그대로 전달하고, 이 응답에 없는 내용을 덧붙이지 마세요."
CAUTION = "※ 예상 금액이며 실제 수급 여부·금액은 고용센터·고용24에서 확인해 주세요. 법률·노무 자문이 아닙니다."

mcp = FastMCP(SERVICE, instructions=(
    "회사 사정으로 퇴직하는 경우의 구직급여(실업급여) 예상 금액, 소정급여일수, 수급 요건, 신청 절차를 알려 주는 도구입니다. "
    "월 급여·나이·가입기간이 없으면 먼저 사용자에게 물어보고, 응답의 금액·일수와 가정을 그대로 전달하세요."))


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


logger = logging.getLogger("joblesscalc")
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
        except Exception:   # 입력값은 기록하지 않음
            result = "[조회 실패] 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            return result
        finally:
            logger.info("TOOL name=%s status=%s ms=%d len=%d", func.__name__, status,
                        int((time.perf_counter() - t0) * 1000), len(result or ""))
    return wrapper


def bad(m):
    return f"[안내] {m}"


def won(x):
    return f"{int(x + 0.5):,}원"


def _env_int(name, default):
    try:
        v = int(os.environ.get(name, default))
        if 10_000 <= v <= 1_000_000:
            return v
    except ValueError:
        pass
    return default


def upper():
    return _env_int("UB_UPPER", DEFAULT_UPPER)


def lower():
    return _env_int("UB_LOWER", DEFAULT_LOWER)


# 소정급여일수: (가입기간 구간) → 50세 미만, 50세 이상·장애인
BANDS = [(1, 120, 120), (3, 150, 180), (5, 180, 210), (10, 210, 240), (None, 240, 270)]
BAND_NAMES = ["1년 미만", "1년 이상 3년 미만", "3년 이상 5년 미만", "5년 이상 10년 미만", "10년 이상"]


def benefit_days(age, insured_years, disabled=False):
    """(소정급여일수, 구간 이름). insured_years는 가입기간(년, 소수 가능)."""
    senior = age >= 50 or disabled
    for i, (limit, young, old) in enumerate(BANDS):
        if limit is None or insured_years < limit:
            return (old if senior else young), BAND_NAMES[i]
    return 240, BAND_NAMES[-1]


def years_of(years, months):
    return years + months / 12.0


def input_ok(age, years, months):
    return (isinstance(age, int) and 15 <= age <= 100 and 0 <= years <= 60 and 0 <= months <= 11
            and years_of(years, months) > 0)


def daily_benefit(daily_wage):
    """(1일 구직급여, 적용 설명)"""
    raw = daily_wage * RATE
    up, lo = upper(), lower()
    if raw > up:
        return up, f"평균임금의 60%({won(raw)})가 상한액을 넘어 상한액 {won(up)} 적용"
    if raw < lo:
        return lo, f"평균임금의 60%({won(raw)})가 하한액보다 낮아 하한액 {won(lo)} 적용"
    return int(raw + 0.5), f"평균임금의 60%({won(raw)})가 상한·하한 범위 안이라 그대로 적용"


# ---------------------------------------------------------------- Tool 1
@mcp.tool(
    name="estimate_unemployment_benefit",
    description=(
        f"Estimates the unemployment benefit (구직급여, 실업급여) a South Korean worker may receive after losing a job for employer-side reasons, using {SERVICE}: "
        "daily benefit = 60% of the average daily wage within the daily upper/lower limits, times the benefit days set by age and insured period. "
        "Pass the monthly pre-tax pay (average of the last 3 months, including regular allowances), age at leaving, and insured years/months. "
        "Optionally pass the last-3-months total pay and number of days for a more exact average wage. If values are missing, ask the user first. "
        "Always call this tool instead of calculating by hand. Keywords: 실업급여 계산, 실업급여 얼마, 구직급여, 권고사직, 해고, 계약만료, 퇴사 후 받는 돈. Estimate only, not advice."
    ),
    annotations=_annotations("Estimate unemployment benefit"),
)
@_logged
def estimate_unemployment_benefit(
    monthly_wage: Annotated[int, Field(description="Average monthly pre-tax pay over the last 3 months in KRW, e.g. 3000000")] = 0,
    age: Annotated[int, Field(description="Age (full years) at the leaving date, e.g. 35")] = 0,
    insured_years: Annotated[int, Field(description="Whole years of employment insurance coverage, e.g. 3")] = 0,
    insured_months: Annotated[int, Field(description="Extra months (0-11) beyond insured_years, e.g. 6")] = 0,
    disabled: Annotated[bool, Field(description="True if the worker is registered as disabled (uses the 50+ day table)")] = False,
    last3m_total_pay: Annotated[int, Field(description="Optional: total pay for the last 3 months before leaving in KRW (incl. bonus share/leave pay); 0 if unknown")] = 0,
    last3m_days: Annotated[int, Field(description="Optional: number of calendar days in those 3 months (89-92); needed with last3m_total_pay")] = 0,
) -> str:
    if not input_ok(age, insured_years, insured_months):
        return bad("나이(15~100), 고용보험 가입기간(년 0~60, 개월 0~11, 합계 0보다 큼)을 숫자로 입력해 주세요. 예: 나이 35, 가입 3년 6개월")
    exact = last3m_total_pay > 0
    if exact:
        if not 89 <= last3m_days <= 92 or last3m_total_pay > 100_000_000:
            return bad("최근 3개월 임금 총액과 그 기간의 일수(89~92일)를 함께 입력해 주세요. 모르면 둘 다 비워 두고 월 급여만 입력하세요.")
        daily_wage = last3m_total_pay / last3m_days
        basis = f"최근 3개월 임금 {won(last3m_total_pay)} ÷ {last3m_days}일 = 1일 평균임금 {won(daily_wage)}"
    else:
        if monthly_wage is None or not 100_000 <= monthly_wage <= 100_000_000:
            return bad("월 급여(세전, 최근 3개월 평균)를 원 단위 숫자로 입력해 주세요. 예: 3000000 (300만 원)")
        daily_wage = monthly_wage * 12 / 365
        basis = f"월 급여 {won(monthly_wage)} × 12 ÷ 365 = 1일 평균임금 약 {won(daily_wage)} (근사치)"
    daily, why = daily_benefit(daily_wage)
    days, band = benefit_days(age, years_of(insured_years, insured_months), disabled)
    total = daily * days
    senior = age >= 50 or disabled
    out = [
        f"■ 실업급여(구직급여) 예상 — 나이 {age}세{'(장애인 기준 적용)' if disabled else ''}, 가입기간 {insured_years}년 {insured_months}개월",
        f"· 평균임금: {basis}",
        f"· 1일 구직급여: {won(daily)} — {why}",
        f"· 소정급여일수: {days}일 ({'50세 이상·장애인' if senior else '50세 미만'}, {band})",
        f"· 예상 총액: {won(daily)} × {days}일 = {won(total)}",
        f"· 월 환산(30일 기준): 약 {won(daily * 30)}",
        f"· 적용 한도: 1일 상한 {won(upper())}, 하한 {won(lower())} (확인 {AS_OF})",
        "· 가정: 주 40시간 근무, 회사 사정 퇴직으로 수급 요건을 모두 충족, 소정급여일수를 모두 수급. 단시간 근로자의 하한액, 조기재취업수당, 세금·4대보험 공제는 반영하지 않았습니다.",
        "· 평균임금은 상여금·연차수당 등을 포함해 달라질 수 있어 실제 금액은 고용24 모의계산으로 확인하세요.",
        "· 퇴직일로부터 12개월 이내에 수급을 마쳐야 합니다.",
        CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 2
@mcp.tool(
    name="get_benefit_days",
    description=(
        f"Returns the number of unemployment benefit days (소정급여일수) by age and insured period using {SERVICE}: 120 to 240 days under 50, 120 to 270 days for 50+ or disabled. "
        "Use it when the user asks how long the unemployment benefit lasts or what the day table is. If age or period is missing, ask first. "
        "Keywords: 소정급여일수, 실업급여 며칠, 실업급여 기간, 수급 기간. Reference only."
    ),
    annotations=_annotations("Unemployment benefit days"),
)
@_logged
def get_benefit_days(
    age: Annotated[int, Field(description="Age (full years) at the leaving date; 0 returns the whole table")] = 0,
    insured_years: Annotated[int, Field(description="Whole years of employment insurance coverage")] = 0,
    insured_months: Annotated[int, Field(description="Extra months (0-11)")] = 0,
    disabled: Annotated[bool, Field(description="True if registered as disabled")] = False,
) -> str:
    table = ["■ 소정급여일수 표 (가입기간 구간: 50세 미만 / 50세 이상·장애인)"]
    for name, (_, y, o) in zip(BAND_NAMES, BANDS):
        table.append(f"· {name}: {y}일 / {o}일")
    if age == 0 and insured_years == 0 and insured_months == 0:
        return "\n".join(table + ["· 구직급여는 퇴직일로부터 12개월 이내에 수급을 마쳐야 합니다.", CAUTION, SOURCE, RELAY_NOTE])
    if not input_ok(age, insured_years, insured_months):
        return bad("나이(15~100)와 가입기간(년 0~60, 개월 0~11, 합계 0보다 큼)을 숫자로 입력해 주세요. 표만 보려면 모두 비워 두세요.")
    days, band = benefit_days(age, years_of(insured_years, insured_months), disabled)
    return "\n".join([f"■ 나이 {age}세{'(장애인)' if disabled else ''}, 가입기간 {insured_years}년 {insured_months}개월 → 소정급여일수 {days}일 ({band})"]
                     + table + [CAUTION, SOURCE, RELAY_NOTE])


# ---------------------------------------------------------------- Tool 3
REASONS = [
    (("징계해고", "중대한 귀책", "횡령", "무단결근"),
     "limited", "근로자의 중대한 귀책사유로 해고된 경우는 수급이 제한될 수 있습니다. 해고 사유의 구체적 내용은 고용센터에서 판단합니다."),
    (("권고사직", "정리해고", "해고", "구조조정", "폐업", "도산", "휴업", "경영악화", "계약만료", "계약 만료", "기간만료", "계약종료", "임금체불", "회사사정", "회사 사정", "laid off", "layoff", "dismiss", "contract end"),
     "likely", "회사 사정 등 비자발적 이직에 해당할 가능성이 큽니다. 이직확인서의 이직 사유 코드가 수급 판단의 핵심이므로 회사가 어떤 사유로 신고했는지 확인하세요."),
    (("자진퇴사", "개인사정", "개인 사정", "일신상", "이직", "이직준비", "자진 퇴사", "퇴사", "resign", "quit"),
     "unlikely", "자진 퇴사는 원칙적으로 수급 대상이 아닙니다. 다만 임금체불, 출퇴근 곤란, 직장 내 괴롭힘, 근로조건 현저한 저하 등 '정당한 이직 사유'가 인정되면 예외가 될 수 있어 고용센터에서 확인이 필요합니다."),
]


@mcp.tool(
    name="check_unemployment_eligibility",
    description=(
        f"Gives a general checklist of the main eligibility conditions for unemployment benefit in South Korea using {SERVICE}: insured period of 180+ days within the 18 months before leaving, "
        "involuntary reason for leaving, ability and willingness to work, and job-seeking effort. Pass the reason for leaving (e.g. 권고사직, 계약만료, 해고, 자진퇴사) and optionally the insured days within the last 18 months. "
        "It never decides eligibility; the Employment Center does. Keywords: 실업급여 조건, 실업급여 자격, 수급 요건, 자진퇴사 실업급여, 계약만료 실업급여. Reference only."
    ),
    annotations=_annotations("Unemployment benefit eligibility checklist"),
)
@_logged
def check_unemployment_eligibility(
    leaving_reason: Annotated[str, Field(description="Reason for leaving, e.g. 권고사직, 해고, 계약만료, 폐업, 임금체불, 자진퇴사")] = "",
    insured_days_last_18m: Annotated[int, Field(description="Optional: insured days within the 18 months before leaving (e.g. 200); 0 if unknown")] = 0,
) -> str:
    reason = (leaving_reason or "").strip()
    if not reason or len(reason) > 50:
        return bad("퇴직 사유를 짧게 입력해 주세요. 예: 권고사직, 계약만료, 해고, 폐업, 임금체불, 자진퇴사")
    low = reason.lower()
    verdict = None
    for words, kind, text in REASONS:
        if any(w in low for w in words):
            verdict = (kind, text)
            break
    if verdict is None:
        return bad("퇴직 사유를 알아보지 못했습니다. '권고사직', '계약만료', '해고', '폐업', '임금체불', '자진퇴사'처럼 입력해 주세요.")
    if not 0 <= insured_days_last_18m <= 548:
        return bad("이직일 이전 18개월 동안의 가입일수는 0~548 사이 숫자로 입력해 주세요(모르면 비워 두세요).")
    out = [f"■ 실업급여 수급 요건 체크 — 퇴직 사유: {reason}", f"· 이직 사유: {verdict[1]}"]
    if insured_days_last_18m > 0:
        ok = insured_days_last_18m >= 180
        out.append(f"· 가입일수: 이직일 이전 18개월 중 {insured_days_last_18m}일 → 180일 {'이상으로 기준을 충족합니다' if ok else '미만이라 기준에 못 미칩니다'}.")
    else:
        out.append("· 가입일수: 이직일 이전 18개월 중 피보험단위기간(유급일)이 통산 180일 이상이어야 합니다(고용24에서 가입 이력 확인).")
    out += ["· 근로의 의사와 능력이 있는데도 취업하지 못한 상태여야 합니다(질병·임신·육아·학업 등으로 바로 일할 수 없으면 제외).",
            "· 적극적으로 재취업 활동을 해야 하며, 4주마다 실업 인정을 받습니다.",
            "· 수급 여부는 고용센터가 이직확인서와 신청 내용으로 판단합니다. 이 도구는 일반 안내일 뿐 수급을 보장하거나 부정하지 않습니다.",
            CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 4
@mcp.tool(
    name="get_unemployment_process_guide",
    description=(
        f"Explains the application steps for unemployment benefit in South Korea using {SERVICE}: employer files the separation certificate, job-seeker registration, benefit application at Work24 or the Employment Center, "
        "the waiting period, and 4-week unemployment recognition. Use it when the user asks how or where to apply. Keywords: 실업급여 신청 방법, 이직확인서, 고용24, 실업인정, 구직등록, 대기기간. Reference only."
    ),
    annotations=_annotations("Unemployment benefit application steps"),
)
@_logged
def get_unemployment_process_guide() -> str:
    return "\n".join([
        "■ 실업급여 신청 절차(일반)",
        "1. 퇴직 후 회사가 고용센터에 이직확인서를 제출합니다(퇴직 다음 달 15일까지가 원칙). 이직 사유가 수급 판단에 중요하니 내용을 확인하세요.",
        "2. 고용24에서 구직 등록을 하고, 수급자격 신청서를 제출합니다(거주지 관할 고용센터 방문 또는 온라인).",
        "3. 고용센터가 수급자격을 인정하면 수급자 교육을 받습니다.",
        "4. 수급자격 인정 후 7일의 대기기간을 거칩니다.",
        "5. 4주마다 실업 인정을 신청하고 그 기간의 재취업 활동을 증빙하면, 인정된 기간만큼 구직급여가 지급됩니다.",
        "· 퇴직일로부터 12개월이 지나면 남은 소정급여일수가 있어도 수급권이 소멸합니다. 가능하면 퇴직 후 빨리 신청하세요.",
        "· 구체적 서류와 일정은 고용24(work24.go.kr)·고용센터(☎1350)에서 확인하세요.",
        CAUTION, SOURCE, RELAY_NOTE])


# ---------------------------------------------------------------- Tool 5
@mcp.tool(
    name="get_unemployment_reference",
    description=(
        f"Returns the formulas, current daily upper/lower limits, benefit-day table and assumptions used by {SERVICE}. "
        "Use it when the user asks how the amount is calculated, what the current limits are, or what the assumptions of a result are. Keywords: 실업급여 상한액, 실업급여 하한액, 계산 방법. Reference only."
    ),
    annotations=_annotations("Unemployment benefit rules and sources"),
)
@_logged
def get_unemployment_reference() -> str:
    return "\n".join([
        "■ 실업급여 계산기 기준",
        "■ 1일 구직급여 = 퇴직 전 평균임금(1일) × 60%, 단 상·하한액 범위 안에서 적용",
        f"■ 1일 상한액 {won(upper())}, 하한액 {won(lower())} (2026년 기준, 하한액 = 최저임금 10,320원 × 80% × 8시간, 확인 {AS_OF})",
        "■ 소정급여일수: 50세 미만 120·150·180·210·240일, 50세 이상·장애인 120·180·210·240·270일 (가입기간 1년 미만·1~3년·3~5년·5~10년·10년 이상)",
        "■ 평균임금: 퇴직 전 3개월 임금 총액 ÷ 그 기간 일수. 월 급여만 입력하면 월급×12÷365 근사치를 씁니다.",
        "■ 한계: 단시간 근로자 하한액, 조기재취업수당, 수급 제한·감액 사유, 세금·4대보험은 반영하지 않습니다. 수급 여부는 고용센터가 결정합니다.",
        SOURCE, "입력한 금액·나이·가입기간은 저장하지 않습니다."])


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
