"""우주 사진 안내 - Space Photo MCP 서버
NASA 이미지·영상 라이브러리(images-api.nasa.gov) 검색 API로 우주 사진을 찾아 출처와 함께 보여 줍니다.
- 인증키가 없습니다. 입력값(검색어)은 저장·로그하지 않고, 로그에는 툴 이름·상태·처리시간만 남깁니다.
- NASA 이용 지침에 따라 출처를 NASA로 표기하고, NASA가 보증·후원하는 것처럼 쓰지 않으며,
  저작권 표시가 있거나 인물이 중심인 사진은 제외합니다. 설명 글은 영어 원문이며 AI 답변은 요약·번역으로 전달합니다.
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

SERVICE = "우주 사진 안내 - Space Photo"   # 콘솔 등록명과 글자까지 동일해야 함
mcp = FastMCP(
    SERVICE,
    instructions=("NASA 이미지·영상 라이브러리에서 우주 사진을 찾아 보여 주는 도구입니다. 검색어는 영어로 바꿔 넣으세요. "
                  "응답의 마크다운 이미지(![...](...))와 출처 표기를 그대로 전달하고, NASA가 말한 것처럼 쓰지 말고 응답에 없는 내용을 지어내지 마세요."),
)

BASE = "https://images-api.nasa.gov"
TIMEOUT = 6.0
CACHE_TTL = 12 * 3600
SOURCE = "출처: NASA 이미지·영상 라이브러리(images.nasa.gov). 이 서비스는 NASA의 보증·후원을 받지 않은 비공식 서비스입니다."
RELAY_NOTE = "※ 답변 시 위 제목·사진·출처 표기를 그대로 전달하고, NASA가 직접 말한 것처럼 쓰거나 이 응답에 없는 내용을 덧붙이지 마세요. 설명은 영어 원문의 요약입니다."
# 한국어 → 영어 검색어(자주 쓰는 것만; 그 외 한글은 AI가 영어로 바꿔 다시 호출)
KO_EN = {"화성": "mars", "목성": "jupiter", "토성": "saturn", "금성": "venus", "수성": "mercury", "천왕성": "uranus", "해왕성": "neptune",
         "명왕성": "pluto", "달": "moon", "태양": "sun", "지구": "earth", "은하수": "milky way", "은하": "galaxy", "성운": "nebula",
         "블랙홀": "black hole", "혜성": "comet", "소행성": "asteroid", "오로라": "aurora", "일식": "solar eclipse", "월식": "lunar eclipse",
         "국제우주정거장": "international space station", "우주정거장": "space station", "허블": "hubble", "제임스웹": "james webb",
         "웹 망원경": "james webb", "우주왕복선": "space shuttle", "로켓": "rocket", "은하단": "galaxy cluster", "초신성": "supernova",
         "별": "star", "우주": "universe", "토성 고리": "saturn rings", "오리온": "orion nebula", "안드로메다": "andromeda galaxy"}
PEOPLE_WORDS = ("astronaut", "crew", "commander", "portrait", "cosmonaut", "administrator", "celebration", "ceremony", "briefing",
                "press", "conference", "team", "visit", "employees", "interview", "panel", "award", "teacher", "student", "event",
                "reception", "meeting", "tour", "people", "children", "kids", "audience", "attend", "speaks", "poses", "pictured",
                "director", "scientist", "engineer", "officials", "gathered", "celebrate", "party")
COPYRIGHT_RE = re.compile(r"©|\(c\)|copyright|all rights reserved|used with permission", re.I)


class NasaUnavailable(Exception):
    pass


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)
logger = logging.getLogger("spacephoto")
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
        except Exception:   # 입력값·내용은 기록하지 않는다
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


# ---------------------------------------------------------------- NASA 호출
_CACHE = {}


def nasa_get(path, params=None):
    params = params or {}
    ck = (path, tuple(sorted(params.items())))
    hit = _CACHE.get(ck)
    if hit and hit[0] > time.time():
        return hit[1]
    t0 = time.perf_counter()
    try:
        import httpx
        ua = f"SpacePhotoGuide/1.0 ({os.environ.get('WIKI_CONTACT', '').strip() or 'https://misatech-mcp-hub.onrender.com'}) httpx"
        r = httpx.get(BASE + path, params=params, headers={"User-Agent": ua, "Accept": "application/json"},
                      timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError
    except Exception as e:
        code = getattr(getattr(e, "response", None), "status_code", "-")
        logger.info("NASA path=%s status=error type=%s http=%s", path.split("/")[1] if "/" in path else path, type(e).__name__, code)
        raise NasaUnavailable() from None
    logger.info("NASA path=%s ms=%d", path.split("/")[1] if "/" in path else path, int((time.perf_counter() - t0) * 1000))
    if len(_CACHE) > 300:
        _CACHE.clear()
    _CACHE[ck] = (time.time() + CACHE_TTL, data)
    return data


def call(fn, *a):
    try:
        return fn(*a), None
    except NasaUnavailable:
        return None, fail("NASA 이미지 라이브러리에서 정상 응답을 받지 못했습니다. 잠시 후 다시 시도해 주세요.")


# ---------------------------------------------------------------- 표시 도우미
def to_english(kw):
    """한글 검색어를 영어로 바꾼다. 바꿀 수 없는 한글이 남으면 None."""
    kw = (kw or "").strip()
    for ko in sorted(KO_EN, key=len, reverse=True):
        kw = kw.replace(ko, f" {KO_EN[ko]} ")
    kw = re.sub(r"\s+", " ", kw).strip()
    return None if re.search(r"[가-힣]", kw) else kw


def clean(s, n=400):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&nbsp;|&amp;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def https(u):
    u = (u or "").strip()
    return "https://" + u[7:] if u.startswith("http://") else u


def is_science_id(nid):
    return nid.upper().startswith("PIA")


def usable(item):
    """천체·탐사선·망원경 사진 중심: 저작권 표시, 인물·행사 사진(본부 행사 NHQ, 촬영자 표기 사진), 인물 관련 단어가 든 사진은 제외"""
    d = (item.get("data") or [{}])[0]
    nid = d.get("nasa_id") or ""
    if d.get("media_type") != "image" or not nid:
        return False
    blob = " ".join(str(d.get(k) or "") for k in ("title", "description", "description_508", "secondary_creator", "photographer"))
    if COPYRIGHT_RE.search(blob):
        return False
    if nid.upper().startswith("NHQ"):
        return False
    if (d.get("photographer") or d.get("secondary_creator")) and not is_science_id(nid):
        return False
    text = ((d.get("title") or "") + " " + " ".join(d.get("keywords") or []) + " " + (d.get("description") or "")).lower()
    return not any(re.search(r"\b" + re.escape(w) + r"(s|es|ed)?\b", text) for w in PEOPLE_WORDS)


def preview_url(item):
    for l in item.get("links") or []:
        if l.get("render") == "image" or l.get("rel") == "preview":
            return https(l.get("href"))
    return ""


def best_url(files, fallback):
    """asset 목록에서 큰 사진(~large → ~medium) 주소, 없으면 미리보기"""
    hrefs = [https(x if isinstance(x, str) else x.get("href", "")) for x in files or []]
    for suf in ("~large.jpg", "~medium.jpg"):
        for h in hrefs:
            if h.lower().endswith(suf):
                return h
    return fallback


def photo_md(url, title, nasa_id, d):
    alt = re.sub(r"\s+", " ", re.sub(r"[\[\]\(\)]", " ", title or "우주 사진")).strip()
    who = clean(d.get("photographer") or d.get("secondary_creator") or "", 60)
    credit = "Credit: NASA" + (f" / {who}" if who else "")
    return f"![{alt}]({url})\n({credit} · 원본 https://images.nasa.gov/details/{nasa_id})"


def parse_year(d):
    return (d.get("date_created") or "")[:10]


# ---------------------------------------------------------------- Tool 1
@mcp.tool(
    name="search_space_images",
    description=(
        f"Searches space photos (planets, galaxies, nebulae, Mars rover, ISS, eclipses, etc.) in the NASA Image and Video Library using {SERVICE} "
        "and shows up to 5 photos with title, date and credit. Pass the keyword in English (translate Korean first, e.g. 화성 -> mars, 은하수 -> milky way); "
        "common Korean words are also converted automatically. Photos with copyright marks and portraits of people are excluded. "
        "Always call this tool instead of answering from memory. Keywords: 우주 사진, 화성 사진, 은하 사진, 허블, 제임스웹, 행성. Reference data only; not endorsed by NASA."
    ),
    annotations=_annotations("Search space photos"),
)
@_logged
def search_space_images(
    keyword: Annotated[str, Field(description="English search words, e.g. mars rover, andromeda galaxy, saturn rings (2+ characters)")] = "",
    year_start: Annotated[str, Field(description="Optional first year, e.g. 2020; empty means any")] = "",
) -> str:
    kw = to_english(keyword)
    if kw is None:
        return bad("검색어를 영어로 바꿔 다시 호출해 주세요. 예: 화성 탐사 → mars rover")
    if len(kw) < 2 or len(kw) > 80:
        return bad("검색어를 2글자 이상 입력해 주세요. 예: mars, andromeda galaxy")
    params = {"q": kw, "media_type": "image", "page_size": "30"}
    ys = (year_start or "").strip()
    if ys:
        if not (ys.isdigit() and len(ys) == 4 and 1950 <= int(ys) <= 2100):
            return bad("시작 연도는 4자리 숫자로 입력해 주세요. 예: 2020")
        params["year_start"] = ys
    data, err = call(nasa_get, "/search", params)
    if err:
        return err
    items = [i for i in ((data.get("collection") or {}).get("items") or []) if usable(i) and preview_url(i)]
    items.sort(key=lambda i: 0 if is_science_id(i["data"][0]["nasa_id"]) else 1)   # JPL 행성 사진(PIA)을 먼저
    if not items:
        return bad("조건에 맞는 우주 사진을 찾지 못했습니다. 다른 영어 검색어로 시도해 보세요.")
    out = [f"■ '{kw}' 우주 사진 {min(5, len(items))}장"]
    for it in items[:5]:
        d = it["data"][0]
        out.append(f"■ {clean(d.get('title'), 100)} — {parse_year(d)} · nasa_id {d['nasa_id']}\n{photo_md(preview_url(it), d.get('title'), d['nasa_id'], d)}")
    out.append("더 큰 사진과 설명은 nasa_id로 get_space_image_detail을 호출하세요.")
    out += [SOURCE, RELAY_NOTE]
    return "\n\n".join(out[:-3]) + "\n" + "\n".join(out[-3:])


# ---------------------------------------------------------------- Tool 2
@mcp.tool(
    name="get_space_image_detail",
    description=(
        f"Shows one NASA space photo in a larger size with its title, date, NASA center, English description and credit using {SERVICE}. "
        "Requires the nasa_id returned by search_space_images. Always call this tool instead of answering from memory. Reference data only; not endorsed by NASA."
    ),
    annotations=_annotations("Space photo detail"),
)
@_logged
def get_space_image_detail(
    nasa_id: Annotated[str, Field(description="nasa_id from search_space_images, e.g. PIA12345")] = "",
) -> str:
    nid = (nasa_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,80}", nid):
        return bad("nasa_id는 search_space_images 결과의 nasa_id를 그대로 사용하세요.")
    data, err = call(nasa_get, "/search", {"nasa_id": nid})
    if err:
        return err
    items = [i for i in ((data.get("collection") or {}).get("items") or []) if usable(i)]
    if not items:
        return bad("해당 사진을 찾지 못했거나 이 서비스에서 보여 줄 수 없는 사진입니다.")
    it = items[0]
    d = it["data"][0]
    files, err = call(nasa_get, f"/asset/{nid}")
    flist = [x.get("href") if isinstance(x, dict) else x for x in (((files or {}).get("collection") or {}).get("items") or [])]
    url = best_url(flist, preview_url(it))
    if not url:
        return bad("보여 줄 수 있는 이미지 주소가 없습니다.")
    out = [f"■ {clean(d.get('title'), 120)}",
           f"· 날짜: {parse_year(d) or '미상'} · NASA 센터: {d.get('center') or '미상'}",
           clean(d.get("description"), 500) or "(설명이 등록되어 있지 않습니다)",
           photo_md(url, d.get("title"), nid, d), SOURCE, RELAY_NOTE]
    return "\n\n".join(out)


# ---------------------------------------------------------------- Tool 3
@mcp.tool(
    name="get_space_service_reference",
    description=(
        f"Returns the data source, photo rules and limits of {SERVICE}. "
        "Use it when the user asks where the photos come from, why a photo is missing, or how to credit a photo. Reference only."
    ),
    annotations=_annotations("Space data sources"),
)
@_logged
def get_space_service_reference() -> str:
    return "\n".join([
        "■ 우주 사진 안내 기준",
        "■ 데이터: NASA 이미지·영상 라이브러리(images.nasa.gov) 검색 API",
        "■ 사진 규칙: 사진 아래에 'Credit: NASA'와 원본 링크를 표기합니다. 저작권 표시(©, copyright 등)가 있거나 우주비행사 등 인물이 중심인 사진은 제외합니다.",
        "■ 한계: 검색어는 영어가 정확하고, 설명은 영어 원문입니다. 일부 사진은 NASA가 아닌 협력 기관(ESA 등) 크레딧이 원본 설명에 있으니 원본 링크를 확인하세요. 같은 조회는 최대 12시간 전 결과를 재사용합니다.",
        "■ 비공식 서비스: NASA의 보증·후원을 받지 않으며, NASA가 직접 말한 것으로 표현하지 않습니다. NASA 로고는 사용하지 않습니다.",
        SOURCE,
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
