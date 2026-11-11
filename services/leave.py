"""연차·휴가 도우미 - Leave Helper MCP 서버
근로기준법 제60조의 연차유급휴가 발생일수와 사용 기한, 미사용 연차수당을 날짜·금액 계산으로 알려 주는 참고용 도구입니다.
- 외부 API·인증키가 없고, 입력값(입사일·임금 등)은 저장하거나 로그에 남기지 않습니다. 로그에는 툴 이름·상태·처리시간만 남깁니다.
- 회사 규정·단체협약·근로계약이 법정 기준보다 유리하면 그쪽이 우선하므로, 모든 결과는 법정 기준의 참고값입니다.
"""
import calendar
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

SERVICE = "연차·휴가 도우미 - Leave Helper"   # 콘솔 등록명과 글자까지 동일해야 함
KST = timezone(timedelta(hours=9))
WD = ["월", "화", "수", "목", "금", "토", "일"]
AS_OF = "2026-11-11"   # 법령·해석 확인 시점
SOURCE = "근거: 근로기준법 제60조(연차 유급휴가)·제61조(사용촉진), 고용노동부 해석 (확인 시점 " + AS_OF + ")"
RELAY_NOTE = "※ 답변 시 위 날짜·일수·금액·가정 조건을 그대로 전달하고, 이 응답에 없는 내용을 덧붙이지 마세요."
CAUTION = "※ 상시 5명 미만 사업장, 주 15시간 미만 근로자는 연차 규정이 적용되지 않을 수 있고, 회사 규정이 법보다 유리하면 그쪽이 우선합니다. 참고용이며 법률 자문이 아닙니다."

mcp = FastMCP(SERVICE, instructions=(
    "연차 발생일수·사용 기한·미사용 연차수당을 계산하는 도구입니다. 입사일이나 임금이 없으면 먼저 사용자에게 물어보고, "
    "응답의 날짜·일수·금액과 가정(80% 이상 출근, 1개월 개근)을 그대로 전달하세요."))


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


logger = logging.getLogger("leavehelper")
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
            r = result or ""
            status = "invalid_input" if r.startswith("[안내]") else "ok"
            return result
        except Exception:   # 예기치 못한 오류도 오류 응답 대신 안전한 문구로 돌려준다(입력값은 기록하지 않음)
            result = "[조회 실패] 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            return result
        finally:
            logger.info("TOOL name=%s status=%s ms=%d len=%d", func.__name__, status,
                        int((time.perf_counter() - t0) * 1000), len(result or ""))
    return wrapper


def bad(m):
    return f"[안내] {m}"


# ---------------------------------------------------------------- 날짜·금액 도우미
def today_kst():
    return datetime.now(KST).date()


def parse_date(s):
    s = (s or "").strip()
    for f in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def fmt(d):
    return f"{d.isoformat()}({WD[d.weekday()]})"


def add_months(d, k):
    y = d.year + (d.month - 1 + k) // 12
    m = (d.month - 1 + k) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def add_years(d, k):
    return add_months(d, 12 * k)


def months_completed(start, end):
    """start부터 end까지 채운 개월 수(말일 보정 포함). end < start면 -1."""
    if end < start:
        return -1
    k = (end.year - start.year) * 12 + (end.month - start.month)
    while k > 0 and add_months(start, k) > end:
        k -= 1
    return k


def annual_days(years):
    """근속 만 years년(1 이상)에 발생하는 연차: 15일 + 최초 1년 초과 근속 2년마다 1일, 최대 25일"""
    return min(25, 15 + (years - 1) // 2)


def won(x):
    return f"{int(x + 0.5):,}원"


def ser(n):
    return int(n) if float(n) == int(n) else n


# ---------------------------------------------------------------- Tool 1
@mcp.tool(
    name="calculate_annual_leave",
    description=(
        f"Calculates South Korean statutory annual leave on the hire-date basis using {SERVICE} (Labor Standards Act Art. 60): "
        "days accrued so far, the leave period's use deadline, and the next accrual date and days. "
        "Under 1 year: 1 day per full month, max 11. From the 1st anniversary: 15 days (80%+ attendance), plus 1 day per 2 years after the 3rd year, max 25. "
        "If the hire date is missing, ask the user first. Always call this tool instead of counting by hand. Reference only, not legal advice."
    ),
    annotations=_annotations("Annual leave by hire date"),
)
@_logged
def calculate_annual_leave(
    hire_date: Annotated[str, Field(description="Hire date YYYY-MM-DD")] = "",
    as_of_date: Annotated[str, Field(description="Base date YYYY-MM-DD; empty means today (KST)")] = "",
    attendance_80_percent: Annotated[bool, Field(description="True if the worker attended 80%+ of the past year (default True)")] = True,
    perfect_months: Annotated[int, Field(description="Only if attendance was under 80%: number of full-attendance months in the past year (0-11); -1 if unknown")] = -1,
) -> str:
    hd = parse_date(hire_date)
    if hd is None:
        return bad("입사일을 YYYY-MM-DD 형식으로 입력해 주세요. 예: 2024-03-15")
    base = parse_date(as_of_date) if (as_of_date or "").strip() else today_kst()
    if base is None:
        return bad("기준일을 YYYY-MM-DD 형식으로 입력해 주세요(비우면 오늘).")
    if hd.year < 1970 or hd > base:
        return bad("입사일이 기준일보다 뒤이거나 올바르지 않습니다. 입사 예정이라면 기준일을 입사일 이후로 지정해 주세요.")
    k = months_completed(hd, base)
    y = k // 12
    head = f"■ 입사일 기준 연차 — 입사 {fmt(hd)}, 기준 {fmt(base)}"
    tenure = f"만 {y}년 {k % 12}개월 근속" if y else f"{k}개월 근속"
    out = [head, f"· 근속: {tenure}"]
    if y < 1:
        n = min(k, 11)
        deadline = add_years(hd, 1) - timedelta(days=1)
        out.append(f"· 지금까지 발생: {n}일 (1개월 개근마다 1일, 최대 11일 — 개근 가정)")
        out.append(f"· 사용 기한: {fmt(deadline)}까지 (최초 1년 근로가 끝나는 날까지 쓰지 않으면 소멸하는 것이 원칙)")
        if k < 11:
            nd = add_months(hd, k + 1)
            out.append(f"· 다음 발생: {fmt(nd)} — 누적 {k + 1}일")
        ann = add_years(hd, 1)
        out.append(f"· 입사 1주년({fmt(ann)}): 80% 이상 출근 시 15일 발생")
    else:
        ann = add_months(hd, 12 * y)
        nxt = add_months(hd, 12 * (y + 1))
        if attendance_80_percent:
            n = annual_days(y)
            out.append(f"· 최근 발생: {fmt(ann)} — {n}일 (근속 만 {y}년, 80% 이상 출근 가정)")
        else:
            if perfect_months < 0:
                return bad("직전 1년 출근율이 80% 미만이면 개근한 달마다 1일씩 발생합니다. 개근한 달 수(0~11)를 알려 주세요.")
            n = min(perfect_months, 11)
            out.append(f"· 최근 발생: {fmt(ann)} — {n}일 (출근율 80% 미만, 개근 {n}개월)")
        out.append(f"· 사용 기한: {fmt(add_years(ann, 1) - timedelta(days=1))}까지 (발생일로부터 1년이 원칙)")
        out.append(f"· 다음 발생: {fmt(nxt)} — {annual_days(y + 1)}일 (80% 이상 출근 시)")
        if y < 3:
            out.append(f"· 가산: 근속 만 3년({fmt(add_months(hd, 36))})부터 16일, 이후 2년마다 1일씩 늘어 최대 25일")
    out += ["※ 출근율은 근로 의무가 있는 날 기준이며, 업무상 부상·질병 휴업, 출산전후휴가, 육아휴직 기간은 출근한 것으로 보는 것이 일반적입니다. 세부 기준은 회사나 노무사에게 확인하세요.",
            CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 2
@mcp.tool(
    name="calculate_fiscal_year_leave",
    description=(
        f"Calculates first-year annual leave under the fiscal-year (January 1) method using {SERVICE}: months-based leave in the hire year and the "
        "proportional leave granted on the following January 1 (15 days x days employed in the hire year / 365). "
        "Use it when the company grants leave on January 1 instead of on the hire anniversary. If the hire date is missing, ask the user first. "
        "Always call this tool instead of counting by hand. Reference only, not legal advice."
    ),
    annotations=_annotations("Fiscal-year leave for the first year"),
)
@_logged
def calculate_fiscal_year_leave(
    hire_date: Annotated[str, Field(description="Hire date YYYY-MM-DD")] = "",
) -> str:
    hd = parse_date(hire_date)
    if hd is None:
        return bad("입사일을 YYYY-MM-DD 형식으로 입력해 주세요. 예: 2025-07-01")
    if hd.year < 1970:
        return bad("입사일이 올바르지 않습니다.")
    ye = date(hd.year, 12, 31)
    days = (ye - hd).days + 1
    prop = 15 * days / 365
    months = min(months_completed(hd, ye), 11)
    nj = date(hd.year + 1, 1, 1)
    out = [f"■ 회계연도(1월 1일) 기준 첫해 연차 — 입사 {fmt(hd)}",
           f"· 입사연도 재직일수: {days}일 ({fmt(hd)} ~ {fmt(ye)}, 입사일 포함)",
           f"· 입사연도 안에 발생: 1개월 개근마다 1일 → 연말까지 {months}일",
           f"· {fmt(nj)} 부여: 15일 × {days} ÷ 365 = {prop:.2f}일 (소수점 처리는 회사 규정에 따라 올림·반올림·버림이 다를 수 있음)",
           "· 이후 해에는 매년 1월 1일에 근속 기준 연차(15일, 3년 이상은 가산)를 부여합니다.",
           "※ 회계연도 기준은 입사일 기준보다 근로자에게 불리하면 안 되고, 퇴직할 때는 입사일 기준으로 다시 계산해 더 유리한 쪽을 적용합니다.",
           CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 3
@mcp.tool(
    name="calculate_leave_pay",
    description=(
        f"Calculates unused annual leave pay (연차수당) in South Korea using {SERVICE}: daily ordinary wage x unused days. "
        "Give either the monthly ordinary wage (daily wage = monthly / 209 hours x 8) or the hourly ordinary wage (x 8). "
        "Half days (0.5) are supported. If the wage or unused days are missing, ask the user first. "
        "Always call this tool instead of calculating by hand. Reference only, not legal advice."
    ),
    annotations=_annotations("Unused leave pay"),
)
@_logged
def calculate_leave_pay(
    unused_days: Annotated[float, Field(description="Unused annual leave days, e.g. 5 or 2.5")] = 0,
    monthly_ordinary_wage: Annotated[int, Field(description="Monthly ordinary wage in KRW (won); 0 if using hourly wage")] = 0,
    hourly_ordinary_wage: Annotated[float, Field(description="Hourly ordinary wage in KRW; used when monthly wage is 0")] = 0,
    monthly_hours: Annotated[float, Field(description="Monthly paid hours, default 209 (40h/week with weekly holiday pay)")] = 209,
    daily_hours: Annotated[float, Field(description="Contracted hours per day, default 8")] = 8,
) -> str:
    if unused_days is None or unused_days <= 0 or unused_days > 100:
        return bad("미사용 연차 일수를 0보다 크고 100 이하로 입력해 주세요. 예: 5 (반차는 0.5)")
    mw, hw = int(monthly_ordinary_wage or 0), float(hourly_ordinary_wage or 0)
    if mw <= 0 and hw <= 0:
        return bad("월 통상임금(원) 또는 시간당 통상임금(원)을 입력해 주세요. 예: 월 3,000,000원")
    if not 0 < monthly_hours <= 400 or not 0 < daily_hours <= 24:
        return bad("월 소정근로시간(기본 209)과 1일 소정근로시간(기본 8)을 올바르게 입력해 주세요.")
    if mw > 0:
        daily = int(mw * daily_hours / monthly_hours + 0.5)
        how = f"월 통상임금 {mw:,}원 ÷ {ser(monthly_hours)}시간 × {ser(daily_hours)}시간 = 일급 {daily:,}원(원 단위 반올림)"
    else:
        daily = int(hw * daily_hours + 0.5)
        how = f"시간당 통상임금 {hw:,.0f}원 × {ser(daily_hours)}시간 = 일급 {daily:,}원"
    pay = won(daily * unused_days)
    out = ["■ 미사용 연차수당 계산", f"· {how}", f"· 연차수당 = 일급 {daily:,}원 × {ser(unused_days)}일 = {pay}",
           "※ 통상임금에 무엇이 포함되는지(기본급, 정기·일률적 수당 등)는 근로계약과 임금 구조에 따라 달라집니다.",
           "※ 회사가 법에서 정한 연차 사용촉진 절차를 서면으로 마쳤다면 수당을 지급하지 않아도 될 수 있습니다.",
           CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 4
@mcp.tool(
    name="get_leave_rules_reference",
    description=(
        f"Returns the statutory rules, formulas, assumptions and limits used by {SERVICE}. "
        "Use it when the user asks how annual leave is accrued, when it expires, or what the assumptions of a result are. Reference only."
    ),
    annotations=_annotations("Leave rules and sources"),
)
@_logged
def get_leave_rules_reference() -> str:
    return "\n".join([
        "■ 연차·휴가 도우미 기준",
        "■ 입사 1년 미만: 1개월 개근마다 1일, 최대 11일. 최초 1년 근로가 끝나는 날까지 쓰지 않으면 소멸하는 것이 원칙입니다.",
        "■ 1년 이상: 직전 1년 출근율 80% 이상이면 15일. 근속 3년부터 최초 1년을 넘는 2년마다 1일 가산, 최대 25일(근속 21년).",
        "■ 출근율 80% 미만: 개근한 달마다 1일(최대 11일)이 발생합니다.",
        "■ 사용 기한: 발생일로부터 1년이 원칙이며, 기한이 지나면 연차 사용권은 사라지고 미사용 수당 문제로 넘어갑니다.",
        "■ 연차수당: 통상임금 일급 × 미사용 일수. 일급 = 월 통상임금 ÷ 209 × 8시간(또는 시간급 × 8). 일급은 원 단위로 반올림해 곱합니다.",
        "■ 회계연도 기준: 입사연도 재직일수 비례(15일 × 재직일수 ÷ 365)이며, 입사일 기준보다 불리하면 안 되고 퇴직 시 입사일 기준으로 정산합니다.",
        "■ 연차 사용촉진(제61조): 사용자가 정해진 서면 절차를 마치면 미사용 연차수당 지급 의무가 면제될 수 있습니다.",
        "■ 가정: 80% 이상 출근, 1개월 개근, 주 40시간 근로를 기본으로 합니다. 휴업·휴직 기간의 처리는 회사나 노무사에게 확인하세요.",
        "■ 제외: 상시 5명 미만 사업장, 주 15시간 미만 근로자 등은 연차 규정이 적용되지 않을 수 있습니다(일반론이며 개별 확인 필요).",
        "■ 법령 개정(출근율 산정 등)이 있을 수 있어 최종 판단은 고용노동부나 노무사에게 확인하세요.",
        SOURCE,
        "입력한 입사일과 임금은 저장하지 않습니다."])


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
