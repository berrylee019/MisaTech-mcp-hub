"""한국 생활 법정 계산기 - Korea Life Calc MCP 서버

주휴수당·퇴직금·부동산 중개보수·주택 취득세를 결정적으로 계산하는 참고용 도구입니다.
- 외부 API 호출 없음(순수 계산) → 응답이 빠르고, 인증/개인정보/유료 요소 없음
- 요율·산식은 아래 RATES 블록에만 두고, 법 개정 시 이 블록과 AS_OF만 고치면 됩니다.
- 법률·세무 자문이 아니며, 모든 응답에 적용 기준일과 근거를 함께 표시합니다.
- 사용자가 입력한 금액·날짜는 서버에 저장하지 않으며, 로그에는 툴 이름·처리시간·상태만 남깁니다.
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

SERVICE = "한국 생활 법정 계산기 - Korea Life Calc"   # 콘솔 등록명과 글자 하나까지 동일해야 함(심사 DESCRIPTION_POLICY)
KST = timezone(timedelta(hours=9))

mcp = FastMCP(
    "한국 생활 법정 계산기 - Korea Life Calc",
    instructions=(
        "주휴수당, 퇴직금, 부동산 중개보수(주택), 주택 취득세를 법정 산식으로 계산하는 참고용 도구입니다. "
        "응답에 표시된 적용 기준일과 가정(입력값)을 그대로 전달하고, 응답에 없는 법률 판단은 추측하지 말고 "
        "고용노동부·지자체·세무사 등 전문가 확인을 안내하세요. 법률·세무 자문이 아닙니다."
    ),
)

# ================================================================ RATES (법 개정 시 여기만 수정)
AS_OF = "2026-10-27"   # 요율·산식 확인 기준일

MIN_WAGE = {  # 시간급(원). 출처: 고용노동부 고시 제2025-47호(2026), 2027년 적용 최저임금(2026.8.5 고시)
    2026: 10_320,
    2027: 10_700,
}
MONTHLY_BASE_HOURS_40H = 209  # 주 40시간 + 주휴 8시간 기준 월 환산시간(고시)

# 주택 중개보수 상한요율 (국토교통부 2021.10 개편 기준, 시·도 조례로 확정). (상한 미만 금액, 요율, 한도액)
BROKERAGE_SALE = [
    (50_000_000, 0.006, 250_000),
    (200_000_000, 0.005, 800_000),
    (900_000_000, 0.004, None),
    (1_200_000_000, 0.005, None),
    (1_500_000_000, 0.006, None),
    (None, 0.007, None),
]
BROKERAGE_LEASE = [
    (50_000_000, 0.005, 200_000),
    (100_000_000, 0.004, 300_000),
    (600_000_000, 0.003, None),
    (1_200_000_000, 0.004, None),
    (1_500_000_000, 0.005, None),
    (None, 0.006, None),
]

# 주택 취득세(유상취득). 출처: 지방세법 제11조, 제13조의2, 제151조, 농어촌특별세법
ACQ_AREA_EXEMPT_M2 = 85.0


# ================================================================ 공통
DISCLAIMER = "참고용 계산이며 법률·세무 자문이 아닙니다. 실제 적용은 관련 기관·전문가에게 확인하세요."


def won(n) -> str:
    return f"{int(round(n)):,}원"


def pct(x: float) -> str:
    s = f"{x * 100:.4f}".rstrip("0").rstrip(".")
    return f"{s}%"


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


def min_wage_for(year: int):
    return MIN_WAGE.get(year)


def _annotations(title: str) -> dict:
    # PlayMCP 필수: 5개 속성 모두 지정. 외부 호출이 없으므로 openWorldHint=False
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


# ---------------------------------------------------------------- 호출 로깅 (문자열 인자·금액은 기록하지 않음)
logger = logging.getLogger("lifecalc")
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


# ================================================================ Tool 1: 주휴수당
@mcp.tool(
    name="calculate_weekly_holiday_pay",
    description=(
        f"Calculates the weekly holiday allowance (주휴수당) under Korean labor law using {SERVICE}. "
        "Formula: min(weekly scheduled hours, 40) / 40 x 8 hours x hourly wage. Eligible only if the weekly "
        "scheduled hours are 15 or more and the worker attended every scheduled day that week. Also returns the "
        "monthly-converted hours and checks the hourly wage against the statutory minimum wage (2026: 10,320 KRW, "
        "2027: 10,700 KRW). Reference calculation, not legal advice."
    ),
    annotations=_annotations("Weekly holiday pay"),
)
@_logged
def calculate_weekly_holiday_pay(
    weekly_hours: Annotated[float, Field(description="Weekly scheduled working hours in the contract, e.g. 20, 40")],
    hourly_wage: Annotated[int, Field(description="Hourly wage in KRW. 0 means use the minimum wage of the year")] = 0,
    attended_all_scheduled_days: Annotated[bool, Field(description="True if the worker attended every scheduled day that week")] = True,
    year: Annotated[int, Field(description="Year for the minimum wage, 2026 or 2027. 0 means the current year")] = 0,
) -> str:
    if not (0 < weekly_hours <= 80):
        return bad("주 소정근로시간은 0 초과 80 이하로 입력해주세요.")
    year = year or datetime.now(KST).year
    mw = min_wage_for(year)
    note_mw = ""
    if hourly_wage <= 0:
        if mw is None:
            return bad(f"{year}년 최저임금 정보가 없습니다. 시급을 직접 입력해주세요(지원: {sorted(MIN_WAGE)}년).")
        hourly_wage = mw
        note_mw = f"- 시급 미입력: {year}년 최저시급 {won(mw)}을 적용했습니다."
    elif hourly_wage < 0:
        return bad("시급은 0 이상의 정수로 입력해주세요.")

    lines = [f"[주휴수당 계산] 주 {weekly_hours:g}시간, 시급 {won(hourly_wage)}"]
    if note_mw:
        lines.append(note_mw)
    if mw is not None and hourly_wage < mw:
        lines.append(f"- 주의: 입력한 시급이 {year}년 최저시급 {won(mw)}보다 낮습니다.")

    holiday_hours = min(weekly_hours, 40) / 40 * 8
    pay = holiday_hours * hourly_wage
    if weekly_hours < 15:
        lines.append("- 결과: 1주 소정근로시간이 15시간 미만이라 주휴수당 대상이 아닙니다.")
    elif not attended_all_scheduled_days:
        lines.append("- 결과: 그 주 소정근로일을 개근하지 않아 주휴수당이 발생하지 않습니다.")
    else:
        lines.append(f"- 주휴시간: min({weekly_hours:g}, 40) ÷ 40 × 8 = {holiday_hours:g}시간")
        lines.append(f"- 1주 주휴수당: {holiday_hours:g}시간 × {won(hourly_wage)} = {won(pay)}")
        monthly_hours = round((min(weekly_hours, 40) + holiday_hours) * 365 / 7 / 12)
        lines.append(f"- 월 환산(주휴 포함): {monthly_hours}시간 × {won(hourly_wage)} = {won(monthly_hours * hourly_wage)}"
                     f" (주 40시간 기준 고시 환산시간 {MONTHLY_BASE_HOURS_40H}시간)")
        if weekly_hours > 40:
            lines.append("- 참고: 주 40시간을 넘는 소정근로시간은 주휴시간 계산에서 40시간으로 제한됩니다.")
    lines += [f"적용 기준일: {AS_OF}",
              "근거: 근로기준법 제55조(주휴일), 최저임금 고시(고용노동부 제2025-47호 등).",
              "월급제는 주휴수당이 월급에 포함돼 있을 수 있어 별도로 더해 받는 금액이 아닙니다.", DISCLAIMER]
    return "\n".join(lines)


# ================================================================ Tool 2: 퇴직금
@mcp.tool(
    name="calculate_severance_pay",
    description=(
        f"Calculates statutory severance pay (퇴직금) under the Korean Employee Retirement Benefit Security Act "
        f"using {SERVICE}. Severance = daily average wage x 30 x total service days / 365, where the daily average "
        "wage is the last 3 months' wages plus 3/12 of annual bonuses and 3/12 of annual leave pay, divided by the "
        "days in those 3 months (floored by the daily ordinary wage if given). Requires 1+ year of continuous service "
        "and 15+ weekly hours. Dates are YYYY-MM-DD. Always use this tool instead of estimating by hand; if the hire date, "
        "last working day or last-3-months pay is missing, ask the user for it first. Reference calculation, not legal advice."
    ),
    annotations=_annotations("Severance pay"),
)
@_logged
def calculate_severance_pay(
    start_date: Annotated[str, Field(description="Hire date, YYYY-MM-DD")],
    end_date: Annotated[str, Field(description="Last working day (retirement date), YYYY-MM-DD")],
    last_3_months_pay: Annotated[int, Field(description="Total gross wages for the last 3 months before retirement in KRW, including regular allowances")],
    annual_bonus: Annotated[int, Field(description="Bonuses paid in the last 12 months in KRW, 0 if none")] = 0,
    annual_leave_pay: Annotated[int, Field(description="Annual leave allowance counted for the last year in KRW, 0 if none")] = 0,
    weekly_hours: Annotated[float, Field(description="Average weekly scheduled hours, default 40")] = 40,
    daily_ordinary_wage: Annotated[int, Field(description="Daily ordinary wage in KRW, 0 if unknown")] = 0,
) -> str:
    s, e = parse_date(start_date), parse_date(end_date)
    if not s or not e:
        return bad("날짜는 YYYY-MM-DD 형식으로 입력해주세요. 예: 2023-03-02")
    if e < s:
        return bad("퇴직일이 입사일보다 빠릅니다.")
    if last_3_months_pay <= 0:
        return bad("퇴직 전 3개월 임금 총액(세전)을 입력해주세요.")
    if min(annual_bonus, annual_leave_pay, daily_ordinary_wage) < 0:
        return bad("금액은 0 이상으로 입력해주세요.")

    service_days = (e - s).days + 1
    one_year = (e + timedelta(days=1)) >= add_months(s, 12)
    lines = [f"[퇴직금 계산] 재직 {s} ~ {e} ({service_days:,}일)"]
    if weekly_hours < 15:
        lines.append("- 결과: 4주 평균 1주 소정근로시간이 15시간 미만이면 법정 퇴직금 대상이 아닙니다.")
    elif not one_year:
        lines.append("- 결과: 계속근로기간이 1년 미만이라 법정 퇴직금 대상이 아닙니다.")
    else:
        p_end = e + timedelta(days=1)
        p_start = add_months(p_end, -3)
        p_days = (p_end - p_start).days
        wage_base = last_3_months_pay + annual_bonus * 3 / 12 + annual_leave_pay * 3 / 12
        avg_daily = wage_base / p_days
        lines.append(f"- 평균임금 산정기간: {p_start} ~ {e} ({p_days}일)")
        lines.append(f"- 3개월 임금 {won(last_3_months_pay)} + 상여금 3/12 {won(annual_bonus * 3 / 12)} "
                     f"+ 연차수당 3/12 {won(annual_leave_pay * 3 / 12)} = {won(wage_base)}")
        lines.append(f"- 1일 평균임금: {won(wage_base)} ÷ {p_days}일 = {won(avg_daily)}")
        if daily_ordinary_wage > 0 and daily_ordinary_wage > avg_daily:
            lines.append(f"- 통상임금(일 {won(daily_ordinary_wage)})이 평균임금보다 커서 통상임금을 기준으로 계산했습니다.")
            avg_daily = daily_ordinary_wage
        severance = avg_daily * 30 * service_days / 365
        lines.append(f"- 퇴직금: {won(avg_daily)} × 30일 × {service_days:,}일 ÷ 365 = {won(severance)}")
        lines.append("- 참고: 퇴직소득세는 별도로 공제되며, 퇴직금은 퇴직 후 14일 이내 지급이 원칙입니다.")
    lines += [f"적용 기준일: {AS_OF}",
              "근거: 근로자퇴직급여 보장법 제8조(30일분 이상의 평균임금), 근로기준법상 평균임금 산정.",
              "통상임금 여부, 임금 항목 포함 범위, 중간정산 등은 개별 사안에 따라 달라질 수 있습니다.", DISCLAIMER]
    return "\n".join(lines)


# ================================================================ Tool 3: 중개보수
def _brokerage_rate(table, amount: int):
    for limit, rate, cap in table:
        if limit is None or amount < limit:
            return rate, cap
    return table[-1][1], table[-1][2]


@mcp.tool(
    name="calculate_brokerage_fee",
    description=(
        f"Calculates the maximum real-estate brokerage fee (중개보수 상한) for housing in Korea using {SERVICE}. "
        "Supports sale ('매매'), jeonse ('전세') and monthly rent ('월세'). For monthly rent the transaction amount is "
        "deposit + monthly rent x 100 (x 70 if that total is under 50 million KRW). Returns the ceiling rate, the cap "
        "and the maximum fee before VAT, based on the 2021 national standard rates. Actual ceilings are set by each "
        "city/province ordinance and VAT may be added. Housing only (not officetel or commercial). Not legal advice."
    ),
    annotations=_annotations("Brokerage fee ceiling"),
)
@_logged
def calculate_brokerage_fee(
    deal_type: Annotated[str, Field(description="One of '매매', '전세', '월세'")],
    amount: Annotated[int, Field(description="Sale price (매매) or deposit (전세/월세) in KRW")],
    monthly_rent: Annotated[int, Field(description="Monthly rent in KRW for 월세, otherwise 0")] = 0,
) -> str:
    dt = (deal_type or "").strip()
    if dt in ("매매", "교환", "sale"):
        table, kind = BROKERAGE_SALE, "매매·교환"
    elif dt in ("전세", "월세", "반전세", "임대차", "lease", "rent"):
        table, kind = BROKERAGE_LEASE, "임대차"
    else:
        return bad("거래유형은 '매매', '전세', '월세' 중 하나로 입력해주세요.")
    if amount <= 0 or monthly_rent < 0:
        return bad("금액은 0보다 큰 정수(원)로 입력해주세요.")

    lines = [f"[중개보수 상한 계산] {dt}"]
    base = amount
    if table is BROKERAGE_LEASE and monthly_rent > 0:
        base = amount + monthly_rent * 100
        mult = 100
        if base < 50_000_000:
            base = amount + monthly_rent * 70
            mult = 70
        lines.append(f"- 거래금액 환산: 보증금 {won(amount)} + 월세 {won(monthly_rent)} × {mult} = {won(base)}")
    else:
        lines.append(f"- 거래금액: {won(base)}")
    rate, cap = _brokerage_rate(table, base)
    raw = base * rate
    fee = min(raw, cap) if cap else raw
    lines.append(f"- 상한요율({kind}): {pct(rate)}" + (f", 한도액 {won(cap)}" if cap else ", 한도액 없음"))
    lines.append(f"- 요율 계산액: {won(raw)}" + (f" → 한도 적용 {won(fee)}" if cap and raw > cap else ""))
    lines.append(f"- 상한 중개보수(부가세 별도): {won(fee)}")
    lines.append(f"- 일반과세자 부가세 10% 포함 시: {won(fee * 1.1)}")
    lines += [f"적용 기준일: {AS_OF}",
              "근거: 공인중개사법 시행규칙 및 시·도 조례의 중개보수 상한요율(국토교통부 2021.10 개편안). "
              "지역 조례에 따라 달라질 수 있고, 위 금액은 상한이며 중개인과 협의로 정합니다. "
              "중개보수는 매도·매수(임대인·임차인) 각각에게 적용됩니다.",
              "주거용 오피스텔·상가 등은 요율이 달라 이 계산에 포함하지 않았습니다.", DISCLAIMER]
    return "\n".join(lines)


# ================================================================ Tool 4: 취득세
def _acq_rate(price: int, housing_count: int, regulated: bool, corporation: bool, temporary: bool):
    """(취득세율, 중과 여부, 설명)"""
    if corporation:
        return 0.12, True, "법인의 주택 유상취득 중과 12%"
    if price <= 600_000_000:
        base = 0.01
    elif price <= 900_000_000:
        base = round((price / 100_000_000 * 2 / 3 - 3) / 100, 4)   # 소수점 셋째 자리 반올림(%)
    else:
        base = 0.03
    if housing_count <= 1:
        return base, False, "1주택 기본세율"
    if housing_count == 2:
        if temporary:
            return base, False, "일시적 2주택(요건 충족 가정) 기본세율"
        if regulated:
            return 0.08, True, "조정대상지역 2주택 중과 8%"
        return base, False, "비조정대상지역 2주택 기본세율"
    if housing_count == 3:
        if regulated:
            return 0.12, True, "조정대상지역 3주택 중과 12%"
        return 0.08, True, "비조정대상지역 3주택 중과 8%"
    return 0.12, True, "4주택 이상 중과 12%"


@mcp.tool(
    name="calculate_acquisition_tax",
    description=(
        f"Calculates the acquisition tax (취득세) for buying a house in Korea by purchase, using {SERVICE}: base rate "
        "1% up to 600 million KRW, a sliding rate (price/100M x 2/3 - 3 %) from 600 to 900 million, 3% above, and "
        "surcharge rates of 8% or 12% for multi-home owners, regulated areas and corporations, plus the local education "
        "tax and the rural special tax (waived for 85 m2 or less). Inputs: purchase price, number of homes owned after "
        "this purchase, regulated-area flag, floor area. Excludes reductions (e.g. first-time buyer) and "
        "gift/inheritance. Not tax advice."
    ),
    annotations=_annotations("Acquisition tax for housing"),
)
@_logged
def calculate_acquisition_tax(
    price: Annotated[int, Field(description="Purchase price (taxable base) in KRW")],
    homes_after_purchase: Annotated[int, Field(description="Number of homes owned after this purchase, including this one. 1 for a first home")] = 1,
    regulated_area: Annotated[bool, Field(description="True if the home is in a designated adjustment (regulated) area")] = False,
    area_m2: Annotated[float, Field(description="Exclusive floor area in square meters, e.g. 84")] = 84.0,
    corporation: Annotated[bool, Field(description="True if the buyer is a corporation")] = False,
    temporary_two_homes: Annotated[bool, Field(description="True if this is a temporary two-home case that meets the legal disposal requirements")] = False,
) -> str:
    if price <= 0:
        return bad("취득가액은 0보다 큰 정수(원)로 입력해주세요.")
    if homes_after_purchase < 1:
        return bad("취득 후 보유 주택 수는 1 이상이어야 합니다(이번 취득 주택 포함).")
    if area_m2 <= 0:
        return bad("전용면적은 0보다 커야 합니다(㎡).")

    rate, surcharge, why = _acq_rate(price, homes_after_purchase, regulated_area, corporation, temporary_two_homes)
    acq = price * rate
    edu = price * 0.004 if surcharge else acq * 0.1
    if area_m2 <= ACQ_AREA_EXEMPT_M2:
        rural, rural_note = 0.0, "전용 85㎡ 이하 면제"
    else:
        rate_rural = {0.08: 0.006, 0.12: 0.010}.get(round(rate, 4), 0.002)
        rural, rural_note = price * rate_rural, f"과세표준의 {pct(rate_rural)}"
    total = acq + edu + rural
    lines = [f"[주택 취득세 계산] 취득가액 {won(price)}, 취득 후 {homes_after_purchase}주택, 전용 {area_m2:g}㎡",
             f"- 적용 세율: {why} → 취득세율 {pct(rate)}",
             f"- 취득세: {won(acq)}",
             f"- 지방교육세: {won(edu)}" + (" (중과: 과세표준의 0.4%)" if surcharge else " (취득세율의 50%의 20%)"),
             f"- 농어촌특별세: {won(rural)} ({rural_note})",
             f"- 합계: {won(total)} (실효세율 {pct(total / price)})",
             "- 신고·납부기한: 취득일(잔금일 등)부터 60일 이내(유상취득).",
             f"적용 기준일: {AS_OF}",
             "근거: 지방세법 제11조·제13조의2·제151조, 농어촌특별세법.",
             "미반영: 생애최초 등 감면, 증여·상속·분양권·입주권, 주택 수 산정 특례(예: 시가표준액 1억 원 이하 주택 등). "
             "조정대상지역 지정과 주택 수는 시점마다 달라지니 위택스·관할 지자체에서 확인하세요.", DISCLAIMER]
    return "\n".join(lines)


# ================================================================ Tool 5: 기준 정보
@mcp.tool(
    name="get_rate_reference",
    description=(
        f"Returns the statutory rates and formulas used by {SERVICE} with their effective date: the minimum wage, "
        "the weekly holiday pay and severance formulas, the housing brokerage fee ceiling table, and the housing "
        "acquisition tax rates. Use it to explain or double-check the numbers behind other calculations. "
        "Reference information, not legal advice."
    ),
    annotations=_annotations("Statutory rate reference"),
)
@_logged
def get_rate_reference() -> str:
    lines = [f"[요율·산식 기준표] 적용 기준일 {AS_OF}", "",
             "■ 최저임금(시간급): " + ", ".join(f"{y}년 {won(w)}" for y, w in sorted(MIN_WAGE.items())),
             f"  월 환산(주 40시간+주휴 8시간): {MONTHLY_BASE_HOURS_40H}시간",
             "■ 주휴수당: min(주 소정근로시간, 40) ÷ 40 × 8 × 시급 (주 15시간 이상, 소정근로일 개근)",
             "■ 퇴직금: 1일 평균임금 × 30 × 재직일수 ÷ 365 (1년 이상, 주 15시간 이상). "
             "평균임금 = (퇴직 전 3개월 임금 + 상여금×3/12 + 연차수당×3/12) ÷ 3개월 일수",
             "■ 중개보수 상한(주택, 부가세 별도)"]
    def tbl(name, table):
        prev = 0
        out = [f"  [{name}]"]
        for limit, rate, cap in table:
            rng = f"{prev // 10_000:,}만원 이상" + (f" ~ {limit // 10_000:,}만원 미만" if limit else "")
            out.append(f"   - {rng}: {pct(rate)}" + (f" (한도 {won(cap)})" if cap else ""))
            prev = limit or prev
        return out
    lines += tbl("매매·교환", BROKERAGE_SALE) + tbl("임대차", BROKERAGE_LEASE)
    lines += ["  월세 환산: 보증금 + 월세×100 (합계 5천만원 미만이면 ×70)",
              "■ 주택 취득세(유상취득): 6억 이하 1%, 6~9억 (가액÷1억×2/3−3)%, 9억 초과 3%",
              "  중과: 조정대상지역 2주택 8%, 3주택 12%, 비조정 3주택 8%, 4주택 이상·법인 12%",
              "  지방교육세: 일반 취득세율×10%(=취득세율의 50%의 20%), 중과 0.4% / 농어촌특별세: 85㎡ 이하 면제, "
              "초과 0.2%(중과 8%→0.6%, 12%→1.0%)",
              "출처: 고용노동부 최저임금 고시, 근로기준법 제55조, 근로자퇴직급여 보장법 제8조, "
              "공인중개사법 시행규칙·시도 조례(국토교통부 2021.10 개편), 지방세법 제11조·제13조의2·제151조, 농어촌특별세법.",
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
