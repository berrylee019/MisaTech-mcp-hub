"""관광지 사진 안내 - Tour Photo Guide MCP 서버
한국관광공사 TourAPI(국문 관광정보 서비스)로 관광지를 찾고 사진과 소개를 보여 줍니다.
- 인증키는 환경변수 TOUR_KEY로만 받습니다(코드·로그·응답에 넣지 않음). 일일 트래픽 1,000건 → 캐시 사용.
- 사진은 저작권 구분이 공공누리 1유형·3유형(출처표시, 원본 그대로 이용)으로 확인된 것만 마크다운 이미지로 보여 주고,
  그 외에는 사진을 빼고 글만 보여 줍니다. 모든 사진에 출처와 유형을 표기합니다.
- 숙박·쇼핑·음식점은 다루지 않습니다(구매 유도 금지 규정). 입력값은 저장·로그하지 않습니다.
"""
import functools
import logging
import os
import re
import time
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

SERVICE = "관광지 사진 안내 - Tour Photo Guide"   # 콘솔 등록명과 글자까지 동일해야 함
mcp = FastMCP(
    SERVICE,
    instructions=("관광지·문화시설·행사를 검색해 사진과 소개를 보여 주는 도구입니다. 응답의 마크다운 이미지(![...](...))와 출처 표기를 "
                  "그대로 전달하고, 응답에 없는 정보를 지어내지 마세요."),
)

BASE = "https://apis.data.go.kr/B551011/KorService2"
TIMEOUT = 2.5
CACHE_TTL = 12 * 3600       # 일일 1,000건 한도 보호
ALLOWED_TYPES = {"12": "관광지", "14": "문화시설", "15": "행사·공연·축제", "28": "레포츠"}   # 숙박·쇼핑·음식점 제외
TYPE_IDS = {v: k for k, v in ALLOWED_TYPES.items()}
TYPE_IDS["축제"] = "15"
OK_LICENSE = {"Type1": "공공누리 1유형", "Type3": "공공누리 3유형"}   # 출처표시 + 원본 그대로 이용 가능한 유형
SOURCE = "출처: 한국관광공사 TourAPI(공공데이터포털)"
RELAY_NOTE = "※ 답변 시 위 이름·주소·사진·출처 표기를 그대로 전달하고, 이 응답에 없는 내용을 덧붙이지 마세요."


class TourUnavailable(Exception):
    pass


class TourConfigError(Exception):
    pass


class TourLimit(Exception):
    pass


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


for _n in ("httpx", "httpcore"):   # httpx INFO 로그에는 인증키가 든 요청 주소가 찍힌다
    logging.getLogger(_n).setLevel(logging.WARNING)
logger = logging.getLogger("tourphoto")
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


# ---------------------------------------------------------------- API 호출
_CACHE = {}


def tour_get(op, params):
    """items(list[dict]) 반환. 인증키는 요청에만 쓴다."""
    key = os.environ.get("TOUR_KEY", "").strip()
    if not key:
        raise TourConfigError()
    ck = (op, tuple(sorted(params.items())))
    hit = _CACHE.get(ck)
    if hit and hit[0] > time.time():
        return hit[1]
    t0 = time.perf_counter()
    try:
        import httpx
        r = httpx.get(f"{BASE}/{op}", params={"serviceKey": key, "MobileOS": "ETC", "MobileApp": "TourPhotoGuide",
                                              "_type": "json", **params}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        head = data["response"]["header"]
        code = str(head.get("resultCode", ""))
    except Exception:   # 예외 문자열에 인증키가 든 주소가 있을 수 있어 쓰지 않는다
        logger.info("TOUR op=%s code=HTTP_OR_PARSE_ERROR", op)
        raise TourUnavailable() from None
    logger.info("TOUR op=%s code=%s ms=%d", op, code, int((time.perf_counter() - t0) * 1000))
    if code == "22":
        raise TourLimit()
    if code in ("10", "30", "31", "32", "33"):
        raise TourConfigError()
    if code != "0000":
        raise TourUnavailable()
    try:
        items = data["response"]["body"].get("items")
        items = items.get("item", []) if isinstance(items, dict) else []
        if isinstance(items, dict):
            items = [items]
    except (AttributeError, KeyError, TypeError):
        raise TourUnavailable() from None
    _CACHE_put(ck, items)
    return items


def _CACHE_put(k, v):
    if len(_CACHE) > 300:
        _CACHE.clear()
    _CACHE[k] = (time.time() + CACHE_TTL, v)


def call(op, params):
    try:
        return tour_get(op, params), None
    except TourConfigError:
        return None, fail("서비스 설정이 아직 완료되지 않았습니다(관리자 확인 필요).")
    except TourLimit:
        return None, fail("오늘 조회 가능한 횟수를 넘었습니다. 내일 다시 시도해 주세요.")
    except TourUnavailable:
        return None, fail("관광정보 서비스에서 정상 응답을 받지 못했습니다. 잠시 후 다시 시도해 주세요.")


# ---------------------------------------------------------------- 표시 도우미
def strip_html(s, limit=500):
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s).strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def https(u):
    u = (u or "").strip()
    return "https://" + u[7:] if u.startswith("http://") else u


def photo_md(url, lic, alt):
    """허용된 유형만 마크다운 이미지로, 아니면 None"""
    url = https(url)
    if not url or lic not in OK_LICENSE:
        return None
    alt = re.sub(r"\s+", " ", re.sub(r"[\[\]\(\)]", " ", alt or "관광지 사진")).strip()
    return f"![{alt}]({url})\n(사진 {OK_LICENSE[lic]} · {SOURCE.replace('출처: ', '')})"


def type_name(it):
    return ALLOWED_TYPES.get(str(it.get("contenttypeid", "")), "")


COMMERCIAL = ("리조트", "호텔", "펜션", "콘도", "모텔", "게스트하우스", "호스텔", "글램핑", "카지노")


def allowed(it):
    """관광지·문화시설·행사만. 관광지로 분류됐어도 숙박업소 성격의 이름은 제외(구매 유도 금지 규정)."""
    if str(it.get("contenttypeid", "")) not in ALLOWED_TYPES:
        return False
    return not any(w in (it.get("title") or "") for w in COMMERCIAL)


def spot_block(it, with_photo=True):
    lines = [f"■ {it.get('title','')} ({type_name(it)}) — 콘텐츠ID {it.get('contentid','')}"]
    if it.get("addr1"):
        lines.append(f"주소: {(it.get('addr1') or '').strip()} {(it.get('addr2') or '').strip()}".strip())
    if with_photo:
        p = photo_md(it.get("firstimage"), it.get("cpyrhtDivCd"), it.get("title"))
        lines.append(p if p else "(사진: 이용 조건이 확인된 이미지가 없어 글로만 안내합니다)")
    return "\n".join(lines)


# ---------------------------------------------------------------- Tool 1
@mcp.tool(
    name="search_tourist_spot",
    description=(
        f"Searches Korean tourist attractions, cultural facilities and festivals by keyword using {SERVICE} (Korea Tourism Organization TourAPI) "
        "and shows up to 5 results with address and a photo when the photo's public license (KOGL type 1 or 3) is confirmed. "
        "Optional region (e.g. 경주, 제주) filters by address and content_type narrows to 관광지, 문화시설 or 행사. "
        "Lodging, shopping and restaurants are not covered. Always call this tool instead of answering from memory. Reference data only."
    ),
    annotations=_annotations("Search tourist spots"),
)
@_logged
def search_tourist_spot(
    keyword: Annotated[str, Field(description="Place or theme keyword, e.g. 불국사, 한옥마을 (2+ characters)")] = "",
    region: Annotated[str, Field(description="Optional city/province to filter by address, e.g. 경주, 제주")] = "",
    content_type: Annotated[str, Field(description="Optional: 관광지, 문화시설, 행사; empty means all")] = "",
) -> str:
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return bad("검색어를 2글자 이상 입력해 주세요. 예: 불국사")
    if content_type and content_type not in TYPE_IDS:
        return bad("content_type은 관광지, 문화시설, 행사 중 하나이거나 비워 주세요.")
    params = {"keyword": kw, "numOfRows": "30", "pageNo": "1", "arrange": "A"}
    if content_type:
        params["contentTypeId"] = TYPE_IDS[content_type]
    items, err = call("searchKeyword2", params)
    if err:
        return err
    items = [i for i in items if allowed(i)]
    reg = (region or "").strip()
    if reg:
        items = [i for i in items if reg in f"{i.get('addr1','')} {i.get('addr2','')}"]
    if content_type:
        items = [i for i in items if str(i.get("contenttypeid")) == TYPE_IDS[content_type]]
    if not items:
        return bad("조건에 맞는 장소를 찾지 못했습니다. 검색어나 지역을 바꿔 보세요.")
    out = [f"■ '{kw}' 검색 결과 {len(items)}곳 중 {min(5, len(items))}곳"]
    out += [spot_block(i) for i in items[:5]]
    out.append("자세한 소개와 사진 더 보기는 콘텐츠ID로 get_spot_detail을 호출하세요.")
    out.append(SOURCE)
    out.append(RELAY_NOTE)
    return "\n\n".join(out[:1] + out[1:-3]) + "\n" + "\n".join(out[-3:])


# ---------------------------------------------------------------- Tool 2
@mcp.tool(
    name="get_spot_detail",
    description=(
        f"Shows one tourist spot's introduction and up to 3 photos using {SERVICE} (Korea Tourism Organization TourAPI). "
        "Requires the content_id returned by search_tourist_spot. Photos are shown only when their public license (KOGL type 1 or 3) is confirmed. "
        "Always call this tool instead of answering from memory. Reference data only."
    ),
    annotations=_annotations("Spot detail"),
)
@_logged
def get_spot_detail(
    content_id: Annotated[str, Field(description="Content ID from search_tourist_spot, digits only")] = "",
) -> str:
    cid = (content_id or "").strip()
    if not cid.isdigit():
        return bad("콘텐츠ID는 숫자입니다. search_tourist_spot 결과의 콘텐츠ID를 사용하세요.")
    common, err = call("detailCommon2", {"contentId": cid})
    if err:
        return err
    if not common:
        return bad("해당 콘텐츠를 찾지 못했습니다.")
    c = common[0]
    if not allowed(c) and c.get("contenttypeid"):
        return bad("이 서비스는 관광지·문화시설·행사만 안내합니다.")
    imgs, err = call("detailImage2", {"contentId": cid, "imageYN": "Y", "numOfRows": "10", "pageNo": "1"})
    if err:
        imgs = []
    out = [f"■ {c.get('title','')} ({type_name(c)})"]
    if c.get("addr1"):
        out.append(f"주소: {(c.get('addr1') or '').strip()} {(c.get('addr2') or '').strip()}".strip())
    ov = strip_html(c.get("overview"))
    out.append(ov if ov else "소개 글이 등록되어 있지 않습니다.")
    shown = 0
    for im in imgs:
        p = photo_md(im.get("originimgurl") or im.get("smallimageurl"), im.get("cpyrhtDivCd"), im.get("imgname") or c.get("title"))
        if p:
            out.append(p)
            shown += 1
        if shown == 3:
            break
    if not shown:
        p = photo_md(c.get("firstimage"), c.get("cpyrhtDivCd"), c.get("title"))
        if p:
            out.append(p)
            shown = 1
    if not shown:
        out.append("(사진: 이용 조건이 확인된 이미지가 없어 글로만 안내합니다)")
    out.append(SOURCE)
    out.append(RELAY_NOTE)
    return "\n\n".join(out)


# ---------------------------------------------------------------- Tool 3
@mcp.tool(
    name="get_spot_photos",
    description=(
        f"Shows photos of one tourist spot (up to 5) with license labels using {SERVICE} (Korea Tourism Organization TourAPI). "
        "Requires the content_id from search_tourist_spot. Only photos with a confirmed public license (KOGL type 1 or 3) are shown. "
        "Always call this tool instead of answering from memory. Reference data only."
    ),
    annotations=_annotations("Spot photos"),
)
@_logged
def get_spot_photos(
    content_id: Annotated[str, Field(description="Content ID from search_tourist_spot, digits only")] = "",
) -> str:
    cid = (content_id or "").strip()
    if not cid.isdigit():
        return bad("콘텐츠ID는 숫자입니다. search_tourist_spot 결과의 콘텐츠ID를 사용하세요.")
    imgs, err = call("detailImage2", {"contentId": cid, "imageYN": "Y", "numOfRows": "20", "pageNo": "1"})
    if err:
        return err
    out, hidden = [], 0
    for im in imgs:
        p = photo_md(im.get("originimgurl") or im.get("smallimageurl"), im.get("cpyrhtDivCd"), im.get("imgname"))
        if p and len(out) < 5:
            out.append(p)
        elif not p:
            hidden += 1
    if not out:
        return bad("이용 조건이 확인된 사진이 없습니다.") if imgs else bad("등록된 사진이 없습니다.")
    head = f"■ 사진 {len(out)}장"
    if hidden:
        head += f" (이용 조건이 확인되지 않은 {hidden}장은 제외)"
    return "\n\n".join([head] + out + [SOURCE, RELAY_NOTE])


# ---------------------------------------------------------------- Tool 4
@mcp.tool(
    name="get_tour_service_reference",
    description=(
        f"Returns the data source, covered categories, photo license rule and limits of {SERVICE}. "
        "Use it when the user asks where the data and photos come from or why a photo is missing. Reference only."
    ),
    annotations=_annotations("Tour data sources"),
)
@_logged
def get_tour_service_reference() -> str:
    return "\n".join([
        "■ 관광지 사진 안내 기준",
        f"■ 데이터: {SOURCE.replace('출처: ', '')} 국문 관광정보 서비스",
        "■ 다루는 분류: 관광지, 문화시설, 행사·공연·축제 (숙박·쇼핑·음식점은 다루지 않습니다)",
        "■ 사진: 저작권 구분이 공공누리 1유형·3유형(출처표시, 원본 그대로 이용)으로 확인된 사진만 보여 줍니다. 그 외에는 사진 없이 글로만 안내합니다.",
        "■ 신선도: 같은 조회는 최대 12시간 전 결과를 재사용할 수 있습니다. 운영시간·행사 일정은 현장 공지로 확인하세요.",
        "입력한 검색어는 저장하지 않습니다."])


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
