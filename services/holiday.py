"""공휴일·연휴 도우미 - Holiday Helper MCP 서버
한국천문연구원 특일 정보(공공데이터포털 SpcdeInfoService)로 공휴일·대체공휴일·임시공휴일을 조회하고,
연휴 길이와 연차를 붙여 쓰는 방법을 날짜 계산으로 알려 줍니다.
- 인증키는 환경변수 HOLIDAY_KEY로만 받습니다(코드·로그·응답에 넣지 않음). 연도 단위로 조회해 24시간 캐시합니다.
- 입력값은 저장·로그하지 않습니다. 로그에는 툴 이름·상태·처리시간만 남깁니다.
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

SERVICE = "공휴일·연휴 도우미 - Holiday Helper"   # 콘솔 등록명과 글자까지 동일해야 함
KST = timezone(timedelta(hours=9))
WD = ["월", "화", "수", "목", "금", "토", "일"]
BASE = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
TIMEOUT = 2.5
CACHE_TTL = 24 * 3600
SOURCE = "출처: 한국천문연구원 특일 정보(공공데이터포털)"
RELAY_NOTE = "※ 답변 시 위 날짜·요일·공휴일 이름·기준 시점을 그대로 전달하고, 이 응답에 없는 내용을 덧붙이지 마세요."
NOTICE = "※ 임시공휴일은 갑자기 지정될 수 있으니 중요한 일정은 정부 발표로 다시 확인하세요."

mcp = FastMCP(SERVICE, instructions=(
    "공휴일·대체공휴일·연휴 길이·연차 활용법을 알려 주는 도구입니다. 응답의 날짜와 요일, 기준 시점을 그대로 전달하고 지어내지 마세요."))


class HolidayUnavailable(Exception):
    pass


class HolidayConfigError(Exception):
    pass


class HolidayLimit(Exception):
    pass


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


for _n in ("httpx", "httpcore"):   # httpx INFO 로그에는 인증키가 든 요청 주소가 찍힌다
    logging.getLogger(_n).setLevel(logging.WARNING)
logger = logging.getLogger("holidayhelper")
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
            status = "invalid_input" if r.startswith("[안내]") else "upstream_error" if r.startswith("[조회 실패]") else "ok"
            return result
        except Exception:   # 예기치 못한 오류도 오류 응답 대신 안전한 문구로 돌려준다(내용·입력값은 기록하지 않음)
            status, result = "exception", "[조회 실패] 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            return result
        finally:
            logger.info("TOOL name=%s status=%s ms=%d len=%d", func.__name__, status,
                        int((time.perf_counter() - t0) * 1000), len(result or ""))
    return wrapper


def bad(m):
    return f"[안내] {m}"


def fail(m):
    return f"[조회 실패] {m}"


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


# ---------------------------------------------------------------- API (연도 단위, 캐시)
_CACHE = {}


def fetch_year(year):
    """{date: 공휴일 이름} 반환. 해당 연도 자료가 없으면 빈 dict. 인증키는 요청에만 쓴다."""
    key = os.environ.get("HOLIDAY_KEY", "").strip()
    if not key:
        raise HolidayConfigError()
    hit = _CACHE.get(year)
    if hit and hit[0] > time.time():
        return hit[1]
    t0 = time.perf_counter()
    try:
        import httpx
        r = httpx.get(BASE, params={"serviceKey": key, "solYear": str(year), "numOfRows": "100", "_type": "json"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        head = data["response"]["header"]
        code = str(head.get("resultCode", ""))
    except Exception:   # 예외 문자열에 인증키가 든 주소가 있을 수 있어 쓰지 않는다
        logger.info("HOLIDAY year=%s code=HTTP_OR_PARSE_ERROR", year)
        raise HolidayUnavailable() from None
    logger.info("HOLIDAY year=%s code=%s ms=%d", year, code, int((time.perf_counter() - t0) * 1000))
    if code in ("22", "99"):
        raise HolidayLimit()
    if code in ("10", "30", "31", "32", "33"):
        raise HolidayConfigError()
    if code not in ("00", "0000"):
        raise HolidayUnavailable()
    result = {}
    try:
        items = data["response"]["body"].get("items")
        items = items.get("item", []) if isinstance(items, dict) else []
        if isinstance(items, dict):
            items = [items]
        for it in items:
            if str(it.get("isHoliday", "")).upper() != "Y":
                continue
            d = datetime.strptime(str(it["locdate"]), "%Y%m%d").date()
            result[d] = str(it.get("dateName", "")).strip()
    except (AttributeError, KeyError, TypeError, ValueError):
        raise HolidayUnavailable() from None
    if len(_CACHE) > 20:
        _CACHE.clear()
    _CACHE[year] = (time.time() + CACHE_TTL, result)
    return result


def load(year):
    """(holidays, 오류문구)"""
    try:
        return fetch_year(year), None
    except HolidayConfigError:
        return None, fail("서비스 설정이 아직 완료되지 않았습니다(관리자 확인 필요).")
    except HolidayLimit:
        return None, fail("오늘 조회 가능한 횟수를 넘었습니다. 내일 다시 시도해 주세요.")
    except HolidayUnavailable:
        return None, fail("특일 정보 서비스에서 정상 응답을 받지 못했습니다. 잠시 후 다시 시도해 주세요.")


def load_range(year):
    """조회 연도와 앞뒤 연도(연휴 경계 계산용, 앞뒤는 실패해도 무시)를 합친 dict."""
    hol, err = load(year)
    if err:
        return None, err
    allh = dict(hol)
    for y in (year - 1, year + 1):
        try:
            allh.update(fetch_year(y))
        except (HolidayConfigError, HolidayLimit, HolidayUnavailable):
            pass
    return (allh, hol), None


# ---------------------------------------------------------------- 날짜 계산 (순수 함수)
def is_off(d, hol):
    return d.weekday() >= 5 or d in hol


def off_blocks(start, end, hol):
    """[start, end] 안에서 연속 휴일(주말+공휴일) 구간 목록: [(시작, 끝)]"""
    blocks, cur = [], None
    d = start
    while d <= end:
        if is_off(d, hol):
            cur = (cur[0], d) if cur else (d, d)
        elif cur:
            blocks.append(cur)
            cur = None
        d += timedelta(days=1)
    if cur:
        blocks.append(cur)
    return blocks


def block_len(b):
    return (b[1] - b[0]).days + 1


def block_holidays(b, hol):
    out, d = [], b[0]
    while d <= b[1]:
        if d in hol:
            out.append((d, hol[d]))
        d += timedelta(days=1)
    return out


def bridges(blocks, hol, max_gap=3):
    """이웃한 두 휴일 구간 사이 평일(연차 필요일)이 1~max_gap일이면 합쳐 얼마나 쉴 수 있는지 계산.
    둘 중 하나는 공휴일을 포함해야 한다."""
    res = []
    for a, b in zip(blocks, blocks[1:]):
        gap = (b[0] - a[1]).days - 1
        if 1 <= gap <= max_gap and (block_holidays(a, hol) or block_holidays(b, hol)):
            res.append({"start": a[0], "end": b[1], "leave": gap, "total": (b[1] - a[0]).days + 1,
                        "leave_days": [a[1] + timedelta(days=i) for i in range(1, gap + 1)]})
    return res


def span(a, b):
    return fmt(a) if a == b else f"{fmt(a)} ~ {fmt(b)}"


# ---------------------------------------------------------------- Tool 1
@mcp.tool(
    name="get_holidays",
    description=(
        f"Lists South Korean public holidays (including substitute and temporary holidays) for a year or one month using {SERVICE} "
        "(official KASI special-day data), with weekday for each date. Use it for questions like 'what holidays are in 2027' or 'holidays in October'. "
        "Always call this tool instead of answering from memory, because substitute and temporary holidays change. Reference data only."
    ),
    annotations=_annotations("Public holidays"),
)
@_logged
def get_holidays(
    year: Annotated[int, Field(description="Year, e.g. 2027 (2020-2035); 0 means this year")] = 0,
    month: Annotated[int, Field(description="Optional month 1-12; 0 means the whole year")] = 0,
) -> str:
    year = int(year or today_kst().year)
    month = int(month or 0)
    if not 2020 <= year <= 2035 or not 0 <= month <= 12:
        return bad("연도는 2020~2035, 월은 1~12(비우면 전체)로 입력해 주세요.")
    hol, err = load(year)
    if err:
        return err
    items = sorted((d, n) for d, n in hol.items() if not month or d.month == month)
    title = f"■ {year}년" + (f" {month}월" if month else "") + " 공휴일"
    if not hol:
        return bad(f"{year}년 공휴일 정보가 아직 공개되지 않았습니다.")
    out = [title + f" ({len(items)}일)"]
    out += [f"· {fmt(d)} {n}" for d, n in items] or ["· 해당 월에는 공휴일이 없습니다."]
    out += [NOTICE, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 2
@mcp.tool(
    name="get_next_holiday",
    description=(
        f"Finds the next South Korean public holidays from today (or a given date) with D-day counts using {SERVICE} "
        "(official KASI special-day data). Returns the next 3 holidays. Use it for 'when is the next holiday'. "
        "Always call this tool instead of answering from memory. Reference data only."
    ),
    annotations=_annotations("Next holiday"),
)
@_logged
def get_next_holiday(
    as_of_date: Annotated[str, Field(description="Base date YYYY-MM-DD; empty means today (KST)")] = "",
) -> str:
    base = parse_date(as_of_date) if as_of_date.strip() else today_kst()
    if base is None:
        return bad("기준일을 YYYY-MM-DD 형식으로 입력해 주세요(비우면 오늘).")
    found = {}
    for y in (base.year, base.year + 1):
        try:
            found.update(fetch_year(y))
        except HolidayConfigError:
            return fail("서비스 설정이 아직 완료되지 않았습니다(관리자 확인 필요).")
        except HolidayLimit:
            return fail("오늘 조회 가능한 횟수를 넘었습니다. 내일 다시 시도해 주세요.")
        except HolidayUnavailable:
            if y == base.year:
                return fail("특일 정보 서비스에서 정상 응답을 받지 못했습니다. 잠시 후 다시 시도해 주세요.")
    nxt = sorted((d, n) for d, n in found.items() if d >= base)[:3]
    if not nxt:
        return bad("기준일 이후 공개된 공휴일 정보가 아직 없습니다.")
    out = [f"■ {fmt(base)} 이후 다음 공휴일"]
    for d, n in nxt:
        k = (d - base).days
        out.append(f"· {fmt(d)} {n} — " + ("오늘" if k == 0 else f"D-{k}"))
    out += [NOTICE, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 3
@mcp.tool(
    name="get_long_weekends",
    description=(
        f"Lists a year's consecutive days off (public holidays plus weekends) of 3 or more days using {SERVICE} "
        "(official KASI special-day data), and shows where 1-3 days of annual leave bridge two off-day blocks into a longer break, "
        "with the exact leave dates and total days off. Use it for 'long weekends this year' or 'how to stretch holidays with leave'. "
        "Always call this tool instead of answering from memory. Reference data only."
    ),
    annotations=_annotations("Long weekends and leave bridges"),
)
@_logged
def get_long_weekends(
    year: Annotated[int, Field(description="Year, e.g. 2027 (2020-2035); 0 means this year")] = 0,
    max_leave_days: Annotated[int, Field(description="Maximum annual-leave days to spend on one break, 1-3")] = 2,
) -> str:
    year = int(year or today_kst().year)
    if not 2020 <= year <= 2035:
        return bad("연도는 2020~2035 사이로 입력해 주세요.")
    max_leave_days = max(1, min(3, int(max_leave_days or 2)))
    res, err = load_range(year)
    if err:
        return err
    allh, own = res
    if not own:
        return bad(f"{year}년 공휴일 정보가 아직 공개되지 않았습니다.")
    start, end = date(year, 1, 1) - timedelta(days=7), date(year, 12, 31) + timedelta(days=7)
    blocks = off_blocks(start, end, allh)
    inyear = lambda b: b[1] >= date(year, 1, 1) and b[0] <= date(year, 12, 31)
    longs = [b for b in blocks if inyear(b) and block_len(b) >= 3 and block_holidays(b, allh)]
    out = [f"■ {year}년 3일 이상 연휴 (공휴일 포함, 주말 포함) {len(longs)}번"]
    for b in longs:
        names = ", ".join(sorted({n for _, n in block_holidays(b, allh)}))
        out.append(f"· {span(*b)} — {block_len(b)}일 ({names})")
    # 연차를 쓰는 날이 모두 해당 연도 안에 있는 경우만 보여 준다(전년도 12월 연차 등은 제외)
    br = [x for x in bridges(blocks, allh, max_leave_days) if all(d.year == year for d in x["leave_days"])]
    out.append(f"■ 연차 {max_leave_days}일 이내로 이어 쓰기")
    if not br:
        out.append("· 해당하는 경우가 없습니다.")
    for x in br:
        days = ", ".join(fmt(d) for d in x["leave_days"])
        out.append(f"· 연차 {x['leave']}일({days}) 사용 → {span(x['start'], x['end'])} 총 {x['total']}일 휴무")
    out += ["※ 회사 휴무일·근무 형태에 따라 다를 수 있으며, 토요일·일요일 휴무 기준으로 계산했습니다.", NOTICE, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 4
@mcp.tool(
    name="check_holiday",
    description=(
        f"Checks whether one date is a South Korean public holiday, a weekend day or a regular working day using {SERVICE} "
        "(official KASI special-day data), and tells whether it is a substitute holiday. Use it for 'is this date a holiday'. "
        "Always call this tool instead of answering from memory. Reference data only."
    ),
    annotations=_annotations("Check a date"),
)
@_logged
def check_holiday(
    date_str: Annotated[str, Field(description="Date YYYY-MM-DD; empty means today (KST)")] = "",
) -> str:
    d = parse_date(date_str) if (date_str or "").strip() else today_kst()
    if d is None:
        return bad("날짜를 YYYY-MM-DD 형식으로 입력해 주세요.")
    if not 2020 <= d.year <= 2035:
        return bad("2020~2035년 사이 날짜만 조회할 수 있습니다.")
    hol, err = load(d.year)
    if err:
        return err
    if not hol:
        return bad(f"{d.year}년 공휴일 정보가 아직 공개되지 않았습니다.")
    if d in hol:
        n = hol[d]
        kind = "대체공휴일" if "대체" in n else "공휴일"
        line = f"{fmt(d)}은(는) {kind}입니다: {n}"
    elif d.weekday() >= 5:
        line = f"{fmt(d)}은(는) 공휴일이 아닌 주말({WD[d.weekday()]}요일)입니다."
    else:
        line = f"{fmt(d)}은(는) 공휴일이 아닌 평일입니다."
    return "\n".join([line, NOTICE, SOURCE, RELAY_NOTE])


# ---------------------------------------------------------------- Tool 5
@mcp.tool(
    name="get_holiday_reference",
    description=(
        f"Returns the data source, what counts as a holiday, and limits of {SERVICE}. "
        "Use it when the user asks where the holiday data comes from or how long breaks are calculated. Reference only."
    ),
    annotations=_annotations("Holiday data sources"),
)
@_logged
def get_holiday_reference() -> str:
    return "\n".join([
        "■ 공휴일·연휴 도우미 기준",
        f"■ 데이터: {SOURCE.replace('출처: ', '')}의 공휴일 정보(대체공휴일·임시공휴일 포함, 지정된 경우)",
        "■ 공휴일: 특일 정보에서 공휴일로 표시된 날만 포함합니다. 법령 개정으로 공휴일 지정이 바뀌면 이 데이터에 따라 달라질 수 있습니다.",
        "■ 연휴 계산: 공휴일과 토·일요일이 이어지는 날을 연휴로 세고, 사이 평일이 1~3일이면 연차로 이을 수 있는 경우를 보여 줍니다.",
        "■ 신선도: 같은 조회는 최대 24시간 전 결과를 재사용할 수 있습니다. 아직 공개되지 않은 해는 조회되지 않을 수 있습니다.",
        NOTICE,
        "입력한 날짜는 저장하지 않습니다."])


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
