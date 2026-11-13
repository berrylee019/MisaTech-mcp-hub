"""전월세 도우미 - Lease Helper MCP 서버
주택임대차보호법의 전월세 전환율, 5% 증액 상한, 갱신요구 기간·신고 기한을 계산으로 알려 주는 참고용 도구입니다.
- 외부 API·인증키가 없고, 입력값(보증금·월세·날짜)은 저장하거나 로그에 남기지 않습니다. 로그에는 툴 이름·상태·처리시간만 남깁니다.
- 기준금리는 BASE_RATE 환경변수(없으면 아래 기본값)로 바꿀 수 있어 금통위 결정 후 코드 수정 없이 갱신합니다.
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

SERVICE = "전월세 도우미 - Lease Helper"   # 콘솔 등록명과 글자까지 동일해야 함
KST = timezone(timedelta(hours=9))
WD = ["월", "화", "수", "목", "금", "토", "일"]
AS_OF = "2026-11-13"   # 법령·기준금리 확인 시점
DEFAULT_BASE = "3.00"  # 한국은행 기준금리(%), 2026-10-22 금융통화위원회 동결 기준
SOURCE = "근거: 주택임대차보호법 제7조의2·제7조·제6조의3, 같은 법 시행령 제9조 (확인 시점 " + AS_OF + ")"
RELAY_NOTE = "※ 답변 시 위 금액·비율·날짜와 가정 조건을 그대로 전달하고, 이 응답에 없는 내용을 덧붙이지 마세요."
CAUTION = "※ 참고용 계산이며 법률 자문이 아닙니다. 계약 내용·지역·예외에 따라 달라질 수 있으니 중요한 결정은 법률구조공단·변호사 등에 확인하세요."

mcp = FastMCP(SERVICE, instructions=(
    "전세↔월세 전환, 보증금·월세 5% 증액 상한, 계약갱신 기간·임대차 신고 기한을 계산하는 도구입니다. 금액이나 날짜가 없으면 먼저 사용자에게 물어보고, "
    "응답의 금액·비율·날짜와 가정을 그대로 전달하세요."))


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


logger = logging.getLogger("leasehelper")
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


# ---------------------------------------------------------------- 전환율·금액 도우미
def base_rate():
    """현재 기준금리(%) — BASE_RATE 환경변수가 올바르면 그 값, 아니면 기본값"""
    try:
        v = float(os.environ.get("BASE_RATE", DEFAULT_BASE))
        if 0 <= v <= 20:
            return v
    except ValueError:
        pass
    return float(DEFAULT_BASE)


def legal_rate():
    """법정 전월세 전환율(%) = min(연 10%, 기준금리 + 연 2%)"""
    return round(min(10.0, base_rate() + 2.0), 4)


def pct(x):
    return f"{x:.2f}".rstrip("0").rstrip(".") + "%"


def man(x):
    return f"{int(x):,}원"


def money_ok(x):
    return x is not None and 0 <= x <= 100_000_000_000


RATE_LINE = lambda: (f"· 법정 전환율: 연 {pct(legal_rate())} = min(연 10%, 기준금리 {pct(base_rate())} + 연 2%) "
                     f"(기준금리 확인 {AS_OF})")


# ---------------------------------------------------------------- Tool 1
@mcp.tool(
    name="convert_jeonse_to_wolse",
    description=(
        f"Converts a South Korean jeonse deposit into monthly rent (전세→월세, 반전세) using {SERVICE}: monthly rent = (jeonse deposit - new deposit) x rate / 12, "
        "with the statutory conversion rate = min(10%, Bank of Korea base rate + 2%). If the landlord offered a specific monthly rent, pass it to see the implied rate and whether it exceeds the legal cap. "
        "If amounts are missing, ask the user first. Always call this tool instead of calculating by hand. Keywords: 전세 월세 전환, 반전세, 전월세전환율, 월세 얼마, 보증금. Reference only, not legal advice."
    ),
    annotations=_annotations("Jeonse to monthly rent"),
)
@_logged
def convert_jeonse_to_wolse(
    jeonse_deposit: Annotated[int, Field(description="Current jeonse deposit in KRW (won), e.g. 300000000")] = 0,
    new_deposit: Annotated[int, Field(description="Deposit to keep after conversion in KRW; 0 means full monthly rent")] = 0,
    offered_monthly_rent: Annotated[int, Field(description="Optional: monthly rent the landlord asked for in KRW, to compare with the legal cap; 0 if none")] = 0,
) -> str:
    if not money_ok(jeonse_deposit) or jeonse_deposit <= 0:
        return bad("전세보증금을 원 단위 숫자로 입력해 주세요. 예: 300000000 (3억 원)")
    if not money_ok(new_deposit) or new_deposit >= jeonse_deposit:
        return bad("월세 전환 후 남기는 보증금은 0 이상이고 기존 전세보증금보다 작아야 합니다.")
    if not money_ok(offered_monthly_rent):
        return bad("집주인이 제시한 월세는 0 이상의 원 단위 숫자로 입력해 주세요(없으면 비워 두세요).")
    conv = jeonse_deposit - new_deposit
    r = legal_rate()
    rent = int(conv * r / 100 / 12 + 0.5)
    out = [f"■ 전세 → 월세 전환 — 전세보증금 {man(jeonse_deposit)}, 월세 전환 후 보증금 {man(new_deposit)}",
           f"· 월세로 바꾸는 금액: {man(conv)}", RATE_LINE(),
           f"· 법정 상한 월세 = {man(conv)} × {pct(r)} ÷ 12 = {man(rent)}"]
    if offered_monthly_rent > 0:
        imp = offered_monthly_rent * 12 / conv * 100
        out.append(f"· 제시 월세 {man(offered_monthly_rent)} → 환산 전환율 연 {pct(imp)}")
        if offered_monthly_rent > rent:
            out.append(f"· 판단: 법정 상한보다 월 {man(offered_monthly_rent - rent)} 높습니다(법정 전환율을 넘는 부분은 인정되지 않는 것이 원칙입니다. 구체적 구제 방법은 법률구조공단·변호사에게 확인하세요).")
        else:
            out.append("· 판단: 법정 상한 이내입니다.")
    out += ["※ 위 월세는 '법정 상한'이며, 실제 월세는 이보다 낮게 협상할 수 있습니다. 전세→월세로 바꾸는 계약에 법정 전환율을 적용하는 근거는 같은 법 제7조의2(보증금 일부를 월세로 바꿀 때)입니다.",
            CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 2
@mcp.tool(
    name="convert_wolse_to_jeonse",
    description=(
        f"Converts monthly rent into a deposit equivalent (월세→전세 환산, 보증금 환산) using {SERVICE}: converted deposit = deposit + monthly rent x 12 / rate. "
        "Uses the statutory conversion rate = min(10%, Bank of Korea base rate + 2%) unless the user supplies a rate. "
        "If amounts are missing, ask the user first. Always call this tool instead of calculating by hand. Keywords: 월세 전세 환산, 보증금 환산, 월세 보증금으로, 전월세전환율. Reference only, not legal advice."
    ),
    annotations=_annotations("Monthly rent to deposit"),
)
@_logged
def convert_wolse_to_jeonse(
    deposit: Annotated[int, Field(description="Current deposit in KRW (won), e.g. 50000000")] = 0,
    monthly_rent: Annotated[int, Field(description="Monthly rent in KRW (won), e.g. 800000")] = 0,
    rate_percent: Annotated[float, Field(description="Optional conversion rate in % per year (e.g. 5); 0 means the statutory rate")] = 0,
) -> str:
    if not money_ok(deposit) or not money_ok(monthly_rent) or monthly_rent <= 0:
        return bad("보증금(0 이상)과 월세(0보다 큼)를 원 단위 숫자로 입력해 주세요. 예: 보증금 50000000, 월세 800000")
    if rate_percent is None or not 0 <= rate_percent <= 100:
        return bad("전환율은 0~100 사이 연 %로 입력해 주세요(비우면 법정 전환율 적용).")
    r = rate_percent if rate_percent > 0 else legal_rate()
    add = int(monthly_rent * 12 / (r / 100) + 0.5)
    out = [f"■ 월세 → 전세 환산 — 보증금 {man(deposit)}, 월세 {man(monthly_rent)}"]
    out.append(f"· 적용 전환율: 연 {pct(r)}" + ("(사용자 지정)" if rate_percent > 0 else ""))
    if rate_percent <= 0:
        out.append(RATE_LINE())
    out += [f"· 월세 환산 보증금 = {man(monthly_rent)} × 12 ÷ {pct(r)} = {man(add)}",
            f"· 전세 환산 총액 = {man(deposit)} + {man(add)} = {man(deposit + add)}",
            "※ 사용자 지정 전환율이 법정 전환율보다 높으면 월세 인상 협상 시 법정 한도를 넘을 수 있습니다.",
            CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 3
@mcp.tool(
    name="check_rent_increase",
    description=(
        f"Checks a proposed deposit/rent increase against the 5% cap (주택임대차보호법 Art. 7 and 6-3) using {SERVICE}: "
        "the maximum new deposit and monthly rent, the actual increase rates, and whether each is within 5%. "
        "If amounts are missing, ask the user first. Always call this tool instead of calculating by hand. Keywords: 보증금 증액, 월세 인상, 5% 상한, 갱신 인상률, 계약갱신청구권 인상. Reference only, not legal advice."
    ),
    annotations=_annotations("5% increase cap check"),
)
@_logged
def check_rent_increase(
    current_deposit: Annotated[int, Field(description="Current deposit in KRW (won)")] = 0,
    current_monthly_rent: Annotated[int, Field(description="Current monthly rent in KRW; 0 for jeonse")] = 0,
    new_deposit: Annotated[int, Field(description="Proposed new deposit in KRW; 0 if not given (then only the cap is shown)")] = 0,
    new_monthly_rent: Annotated[int, Field(description="Proposed new monthly rent in KRW; 0 if not given")] = 0,
) -> str:
    if not money_ok(current_deposit) or not money_ok(current_monthly_rent) or (current_deposit <= 0 and current_monthly_rent <= 0):
        return bad("현재 보증금과 월세를 원 단위 숫자로 입력해 주세요(전세면 월세는 0). 예: 보증금 300000000")
    if not money_ok(new_deposit) or not money_ok(new_monthly_rent):
        return bad("제안받은 보증금·월세는 0 이상의 원 단위 숫자로 입력해 주세요.")
    max_d = int(current_deposit * 1.05)
    max_r = int(current_monthly_rent * 1.05)
    out = [f"■ 5% 증액 상한 — 현재 보증금 {man(current_deposit)}, 월세 {man(current_monthly_rent)}",
           f"· 보증금 상한: {man(current_deposit)} × 1.05 = {man(max_d)} (최대 증액 {man(max_d - current_deposit)})"]
    if current_monthly_rent > 0:
        out.append(f"· 월세 상한: {man(current_monthly_rent)} × 1.05 = {man(max_r)} (최대 증액 {man(max_r - current_monthly_rent)})")
    if new_deposit > 0 or new_monthly_rent > 0:
        nd = new_deposit if new_deposit > 0 else current_deposit
        nr = new_monthly_rent if (new_monthly_rent > 0 or current_monthly_rent == 0) else current_monthly_rent
        if current_deposit > 0:
            inc = (nd - current_deposit) / current_deposit * 100
            out.append(f"· 제안 보증금 {man(nd)} → 증감률 {inc:+.2f}% → " + ("5% 이내" if nd <= max_d else f"5% 초과 (상한보다 {man(nd - max_d)} 많음)"))
        if current_monthly_rent > 0:
            inc = (nr - current_monthly_rent) / current_monthly_rent * 100
            out.append(f"· 제안 월세 {man(nr)} → 증감률 {inc:+.2f}% → " + ("5% 이내" if nr <= max_r else f"5% 초과 (상한보다 {man(nr - max_r)} 많음)"))
        elif nr > 0:
            out.append("· 전세에서 월세가 새로 생기는 경우는 '전환'이므로 5% 증액이 아니라 전월세 전환율(convert_jeonse_to_wolse) 기준으로 확인하세요.")
    out += ["※ 5% 상한은 계약 중 증액 청구와 계약갱신청구권 행사 시 갱신 계약에 적용됩니다. 새로운 임차인과 맺는 신규 계약이나 합의된 갱신에는 적용되지 않을 수 있습니다.",
            "※ 증액은 약정한 차임·보증금을 올린 후 1년 이내에는 할 수 없습니다.",
            CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 4
@mcp.tool(
    name="get_lease_dates",
    description=(
        f"Calculates key South Korean residential lease dates using {SERVICE}: the window for exercising the renewal right (계약갱신요구, 6 to 2 months before expiry), "
        "the 30-day lease contract report deadline (임대차 신고), the 14-day move-in registration deadline (전입신고), and when the tenant's priority right takes effect (대항력, next day 00:00). "
        "Give the lease end date and/or the contract date and/or the move-in date. If all are missing, ask the user first. "
        "Always call this tool instead of counting by hand. Keywords: 계약갱신 기간, 갱신요구 언제까지, 전입신고, 임대차 신고, 대항력, 확정일자. Reference only, not legal advice."
    ),
    annotations=_annotations("Lease key dates"),
)
@_logged
def get_lease_dates(
    lease_end_date: Annotated[str, Field(description="Lease expiry date YYYY-MM-DD; empty if not needed")] = "",
    contract_date: Annotated[str, Field(description="Contract signing date YYYY-MM-DD; empty if not needed")] = "",
    move_in_date: Annotated[str, Field(description="Move-in / address registration date YYYY-MM-DD; empty if not needed")] = "",
) -> str:
    ed, cd, md = (parse_date(x) if (x or "").strip() else None for x in (lease_end_date, contract_date, move_in_date))
    given = [bool((x or "").strip()) for x in (lease_end_date, contract_date, move_in_date)]
    if not any(given):
        return bad("계약 만료일, 계약 체결일, 입주일 중 하나 이상을 YYYY-MM-DD 형식으로 입력해 주세요. 예: 만료일 2027-03-14")
    for flag, d in zip(given, (ed, cd, md)):
        if flag and (d is None or d.year < 1990 or d.year > 2100):
            return bad("날짜는 YYYY-MM-DD 형식으로 올바르게 입력해 주세요. 예: 2027-03-14")
    out = ["■ 전월세 주요 날짜"]
    if ed:
        s, e = add_months(ed, -6), add_months(ed, -2)
        out += [f"· 계약 만료일: {fmt(ed)}",
                f"· 계약갱신요구 가능 기간: {fmt(s)} ~ {fmt(e)} (만료 6개월 전부터 2개월 전까지, 임차인이 명시적으로 요구, 1회·2년 연장)",
                f"· 묵시적 갱신 방지: 집주인은 같은 기간({fmt(s)} ~ {fmt(e)})에 갱신 거절·조건 변경을 통지하지 않으면 같은 조건으로 갱신된 것으로 봅니다."]
    if cd:
        out.append(f"· 임대차 계약 신고 기한: {fmt(cd + timedelta(days=30))}까지 (계약 체결일 {fmt(cd)}부터 30일, 보증금 6천만 원 초과 또는 월세 30만 원 초과, 수도권·광역시·세종·제주시·도 소재 시 지역. 일부 군 지역 제외)")
    if md:
        out += [f"· 전입신고 기한: {fmt(md + timedelta(days=14))}까지 (이사 후 14일 이내) — 대항력은 전입신고와 입주를 마친 다음 날 0시부터 생깁니다.",
                f"· 대항력 발생(전입신고·입주를 {fmt(md)}에 마친 경우): {fmt(md + timedelta(days=1))} 0시",
                "· 확정일자: 계약서를 갖고 주민센터·등기소·온라인에서 받으며, 임대차 신고를 하면 확정일자가 자동으로 부여됩니다(우선변제권 순위 기준)."]
    out += ["※ 휴일에 걸리는 기한의 처리 등은 개별 확인이 필요합니다. 임대차 신고 대상 여부와 과태료는 지역·금액에 따라 다릅니다.",
            CAUTION, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 5
@mcp.tool(
    name="get_lease_rules_reference",
    description=(
        f"Returns the statutory rules, formulas, current base rate and limits used by {SERVICE}. "
        "Use it when the user asks how the conversion rate is set, what the current legal rate is, or what the assumptions of a result are. Reference only."
    ),
    annotations=_annotations("Lease rules and sources"),
)
@_logged
def get_lease_rules_reference() -> str:
    return "\n".join([
        "■ 전월세 도우미 기준",
        f"■ 법정 전월세 전환율: 연 {pct(legal_rate())} = min(연 10%, 한국은행 기준금리 {pct(base_rate())} + 연 2%). 기준금리 확인 {AS_OF}(2026-10-22 금융통화위원회 동결).",
        "■ 전세→월세: 월세 = (전세보증금 − 월세 전환 후 보증금) × 전환율 ÷ 12. 월세→보증금 환산: 월세 × 12 ÷ 전환율.",
        "■ 증액 상한: 계약 중 증액 청구나 계약갱신청구권 행사 때 보증금·월세 각각 직전 금액의 5% 이내, 약정 후 1년 이내 재증액 불가.",
        "■ 계약갱신청구권: 만료 6개월 전~2개월 전에 명시적으로 요구, 1회, 2년 연장. 집주인 실거주·월세 2기 연체 등 거절 사유가 있습니다.",
        "■ 임대차 신고: 계약 체결일부터 30일 이내(보증금 6천만 원 초과 또는 월세 30만 원 초과, 일부 지역 제외). 신고하면 확정일자가 자동 부여됩니다.",
        "■ 대항력: 입주(점유)와 전입신고를 마친 다음 날 0시부터. 확정일자를 받으면 우선변제권 순위가 정해집니다.",
        "■ 한계: 소액임차인 보호금액, 지역별 예외, 신규 계약의 시세 협상 등은 포함하지 않습니다. 기준금리가 바뀌면 법정 전환율도 바뀝니다.",
        SOURCE,
        "입력한 금액과 날짜는 저장하지 않습니다."])


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
