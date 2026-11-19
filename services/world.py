"""세계 관광지 사진 안내 - World Photo MCP 서버
전 세계(한국 제외) 관광지를 위키백과·위키데이터로 찾고, 위키미디어 공용(Wikimedia Commons)의 자유 이용 사진을 출처·라이선스와 함께 보여 줍니다.
- 인증키가 없습니다. 위키미디어 정책에 따라 요청에 User-Agent를 붙이며, 연락처는 선택 환경변수 WIKI_CONTACT로 받습니다.
- 사진은 라이선스가 CC BY·CC BY-SA·CC0·퍼블릭 도메인으로 확인된 것만 보여 주고, 촬영자·라이선스·원본 링크를 모두 표기합니다.
- 숙박·쇼핑·음식점은 다루지 않습니다(구매 유도 금지 규정). 입력값(검색어)은 저장·로그하지 않습니다.
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

SERVICE = "세계 관광지 사진 안내 - World Photo"   # 콘솔 등록명과 글자까지 동일해야 함
mcp = FastMCP(
    SERVICE,
    instructions=("전 세계(한국 제외) 관광지를 검색해 소개와 사진을 보여 주는 도구입니다. 응답의 마크다운 이미지(![...](...))와 촬영자·라이선스 표기를 "
                  "그대로 전달하고, 응답에 없는 정보를 지어내지 마세요."),
)

TIMEOUT = 6.0
CACHE_TTL = 12 * 3600
# 관광지가 아닌 항목(국가·대륙·기업·단체·인물·행사·영화·동음이의 문서)의 위키데이터 분류
NOT_PLACE = {"Q6256", "Q3624078", "Q5107", "Q4830453", "Q783794", "Q891723", "Q43229", "Q5", "Q1656682", "Q15275719", "Q1190554",
             "Q7278", "Q11424", "Q4167410", "Q431289", "Q2085381",
             "Q82794", "Q1620908", "Q3024240", "Q112099", "Q855697"}   # 지리적 지역·역사 지역·역사 국가·섬나라·아대륙
KOREA = "Q884"   # 한국은 기존 '관광지 사진 안내 - Tour Photo Guide'(한국관광공사)가 담당
LANGS = ("ko", "en", "ja", "zh")
OK_LICENSE = re.compile(r"^(CC BY(-SA)?[ -]\d\.\d|CC0|Public domain|PD\b)", re.I)
BAD_FILE_WORDS = ("map", "logo", "flag", "coat of arms", "locator", "icon", "diagram", "symbol", "seal")
COMMERCIAL = ("리조트", "호텔", "모텔", "펜션", "카지노", "ホテル", "リゾート", "旅館", "hotel", "resort", "casino", "motel", "inn")
SOURCE = "출처: 위키백과·위키데이터·위키미디어 공용(Wikimedia Commons)"
RELAY_NOTE = "※ 답변 시 위 이름·소개·사진·촬영자·라이선스 표기를 그대로 전달하고, 이 응답에 없는 내용을 덧붙이지 마세요."


class WikiUnavailable(Exception):
    pass


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)
logger = logging.getLogger("worldphoto")
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


# ---------------------------------------------------------------- 위키미디어 호출
_CACHE = {}


def wiki_get(host, params):
    """host: 'ko.wikipedia.org' 등. JSON(dict) 반환."""
    ck = (host, tuple(sorted(params.items())))
    hit = _CACHE.get(ck)
    if hit and hit[0] > time.time():
        return hit[1]
    t0 = time.perf_counter()
    try:
        import httpx
        ua = f"WorldPhotoGuide/1.0 ({os.environ.get('WIKI_CONTACT', '').strip() or 'https://misatech-mcp-hub.onrender.com'}) httpx"
        r = httpx.get(f"https://{host}/w/api.php", params={"format": "json", "formatversion": "2", **params},
                      headers={"User-Agent": ua, "Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or "error" in data:
            raise ValueError
    except Exception as e:   # 요청 주소·내용은 기록하지 않고 원인 종류와 HTTP 코드만 남긴다
        code = getattr(getattr(e, "response", None), "status_code", "-")
        logger.info("WIKI host=%s status=error type=%s http=%s", host.split(".")[0], type(e).__name__, code)
        raise WikiUnavailable() from None
    logger.info("WIKI host=%s ms=%d", host.split(".")[0], int((time.perf_counter() - t0) * 1000))
    if len(_CACHE) > 300:
        _CACHE.clear()
    _CACHE[ck] = (time.time() + CACHE_TTL, data)
    return data


def wikidata_host():
    return "www.wikidata.org"


def commons_host():
    return "commons.wikimedia.org"


# ---------------------------------------------------------------- 조회 로직
def find_pages(lang, kw):
    """위키백과 검색 → [{title, extract, qid}] (최대 8)"""
    host = f"{lang}.wikipedia.org"
    d = wiki_get(host, {"action": "query", "list": "search", "srsearch": kw, "srlimit": "8", "srnamespace": "0"})
    titles = [x.get("title") for x in (d.get("query", {}).get("search") or []) if x.get("title")]
    if not titles:
        return []
    d2 = wiki_get(host, {"action": "query", "titles": "|".join(titles), "prop": "extracts|pageprops", "exintro": "1",
                         "explaintext": "1", "exchars": "300", "exlimit": "max", "ppprop": "wikibase_item", "redirects": "1"})
    order = {t: i for i, t in enumerate(titles)}
    pages = []
    for p in d2.get("query", {}).get("pages") or []:
        q = (p.get("pageprops") or {}).get("wikibase_item")
        if p.get("title") and q and "missing" not in p:
            pages.append({"title": p["title"], "extract": (p.get("extract") or "").strip(), "qid": q})
    pages.sort(key=lambda p: order.get(p["title"], 99))
    return pages


def entity_info(qids):
    """{qid: {'country': 국가 QID|None, 'en': 영문 라벨}}"""
    if not qids:
        return {}
    d = wiki_get(wikidata_host(), {"action": "wbgetentities", "ids": "|".join(qids), "props": "claims|labels", "languages": "en"})
    out = {}
    for qid, e in (d.get("entities") or {}).items():
        c = None
        for cl in (e.get("claims") or {}).get("P17", []):
            v = (((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("id")
            if v:
                c = v
                break
        adm = []
        for cl in (e.get("claims") or {}).get("P131", []):
            v = (((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("id")
            if v:
                adm.append(v)
        inst = []
        for cl in (e.get("claims") or {}).get("P31", []):
            v = (((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("id")
            if v:
                inst.append(v)
        out[qid] = {"country": c, "en": ((e.get("labels") or {}).get("en") or {}).get("value", ""), "admin": adm[:3],
                    "instance": inst, "coord": bool((e.get("claims") or {}).get("P625"))}
    return out


def with_country(qids):
    """entity_info + 국가(P17)가 없으면 소재 행정구역(P131)을 최대 3단계 따라 올라가 국가를 채운다"""
    info = entity_info(qids)
    pending = {q: list(v.get("admin") or []) for q, v in info.items() if not v.get("country")}
    for _ in range(3):
        ids = sorted({a for adms in pending.values() for a in adms})
        if not ids:
            break
        ai = entity_info(ids)
        nxt = {}
        for q, adms in pending.items():
            more = []
            for a in adms:
                av = ai.get(a) or {}
                if av.get("country"):
                    info[q]["country"] = av["country"]
                    break
                more += av.get("admin") or []
            else:
                if more:
                    nxt[q] = more[:3]
        pending = nxt
    return info


def country_names(qids):
    """{국가 QID: (한국어 라벨, 영어 라벨)}"""
    qids = sorted(set(q for q in qids if q))
    if not qids:
        return {}
    d = wiki_get(wikidata_host(), {"action": "wbgetentities", "ids": "|".join(qids), "props": "labels", "languages": "ko|en"})
    out = {}
    for qid, e in (d.get("entities") or {}).items():
        lb = e.get("labels") or {}
        en = (lb.get("en") or {}).get("value", "")
        ko = (lb.get("ko") or {}).get("value", "") or en
        out[qid] = (ko, en)
    return out


ALIASES = {"중국": "중화인민공화국", "대만": "중화민국", "타이완": "중화민국", "taiwan": "중화민국", "usa": "united states", "us": "united states",
           "america": "united states", "uk": "united kingdom", "england": "united kingdom", "russia": "russia", "러시아": "러시아",
           "vietnam": "vietnam", "uae": "united arab emirates", "홍콩": "hong kong", "czech": "czech republic"}


def country_match(names, want):
    w = (want or "").strip().lower()
    if not w:
        return True
    ko, en = names[0].lower(), names[1].lower()
    for x in {w, ALIASES.get(w, w).lower()}:
        if x == ko or x == en or (len(x) >= 3 and (x in ko or x in en)):
            return True
    return False


def is_place(info):
    """국가·기업·인물·행사 같은 항목이 아니라 장소(위키데이터에 좌표가 있는 항목)로 볼 수 있는지"""
    info = info or {}
    return bool(info.get("coord")) and not (set(info.get("instance") or []) & NOT_PLACE)


def commercial(title):
    t = (title or "").lower()
    return any(w.lower() in t for w in COMMERCIAL)


def plain(s, n=60):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def commons_photos(label, limit=5):
    """위키미디어 공용 검색 → [{url, title, artist, license, page}] (라이선스 확인된 것만) + 제외 수"""
    d = wiki_get(commons_host(), {"action": "query", "generator": "search", "gsrsearch": f"{label} filetype:bitmap",
                                  "gsrnamespace": "6", "gsrlimit": "20", "prop": "imageinfo",
                                  "iiprop": "url|extmetadata|mime|size", "iiurlwidth": "800"})
    pages = sorted((d.get("query", {}).get("pages") or []), key=lambda p: p.get("index", 99))
    out, hidden = [], 0
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        title = p.get("title", "")
        low = title.lower()
        if ii.get("mime") not in ("image/jpeg", "image/png") or any(w in low for w in BAD_FILE_WORDS):
            continue
        if (ii.get("width") or 0) < 600:
            continue
        md = ii.get("extmetadata") or {}
        lic = (md.get("LicenseShortName") or {}).get("value", "")
        restr = (md.get("Restrictions") or {}).get("value", "")
        url = ii.get("thumburl") or ""
        if not OK_LICENSE.match(lic or "") or restr or not url.startswith("https://"):
            hidden += 1
            continue
        if len(out) < limit:
            out.append({"url": url, "title": re.sub(r"^File:|\.\w+$", "", title), "artist": plain((md.get("Artist") or {}).get("value", "")) or "촬영자 미상",
                        "license": lic, "page": ii.get("descriptionurl", "")})
    return out, hidden


def photo_md(ph, alt):
    alt = re.sub(r"\s+", " ", re.sub(r"[\[\]\(\)]", " ", alt or "관광지 사진")).strip()
    return f"![{alt}]({ph['url']})\n(사진: {ph['artist']} · {ph['license']} · 원본 {ph['page']})"


BAD_VIDEO_WORDS = ("map", "logo", "flag", "animation", "diagram", "screencast", "presentation", "interview", "lecture")


def title_relevant(title, label):
    """영상 파일 이름에 장소 영문명 단어(3글자 이상)가 하나라도 들어 있는지"""
    low = (title or "").lower()
    toks = [t for t in re.findall(r"[a-z0-9]{3,}", (label or "").lower())]
    return any(t in low for t in toks) if toks else True


def fmt_duration(sec):
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ""
    return f"{sec // 60}분 {sec % 60:02d}초" if sec >= 60 else f"{sec}초"


def commons_videos(label, limit=3):
    """위키미디어 공용 영상 검색 → [{thumb, title, artist, license, page, duration}] (라이선스 확인·파일 이름에 장소 영문명 포함)
    ※ 공용 분류(deepcat) 검색은 하위 분류가 너무 넓어 엉뚱한 영상이 섞여 사용하지 않는다."""
    d = wiki_get(commons_host(), {"action": "query", "generator": "search", "gsrsearch": f"{label} filetype:video",
                                  "gsrnamespace": "6", "gsrlimit": "20", "prop": "imageinfo",
                                  "iiprop": "url|extmetadata|mime|size|mediatype", "iiurlwidth": "800"})
    pages = sorted((d.get("query", {}).get("pages") or []), key=lambda p: p.get("index", 99))
    out, hidden = [], 0
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        title = p.get("title", "")
        low = title.lower()
        if not ((ii.get("mime") or "").startswith("video/") or (ii.get("mediatype") or "").upper() == "VIDEO"):
            continue
        if any(w in low for w in BAD_VIDEO_WORDS) or not title_relevant(title, label):
            continue
        md = ii.get("extmetadata") or {}
        lic = (md.get("LicenseShortName") or {}).get("value", "")
        restr = (md.get("Restrictions") or {}).get("value", "")
        thumb = ii.get("thumburl") or ""
        if not OK_LICENSE.match(lic or "") or restr or not thumb.startswith("https://") or not ii.get("descriptionurl"):
            hidden += 1
            continue
        if len(out) < limit * 3:
            out.append({"thumb": thumb, "title": re.sub(r"^File:|\.\w+$", "", title), "artist": plain((md.get("Artist") or {}).get("value", "")) or "촬영자 미상",
                        "license": lic, "page": ii.get("descriptionurl", ""), "duration": fmt_duration(ii.get("duration")),
                        "secs": float(ii.get("duration") or 0)})
    out.sort(key=lambda v: v["secs"] > 900)   # 15분을 넘는 긴 영상은 뒤로(안정 정렬이라 관련도 순서는 유지)
    return out[:limit], hidden


def video_md(v, alt):
    alt = re.sub(r"\s+", " ", re.sub(r"[\[\]\(\)]", " ", alt or "영상 미리보기")).strip()
    dur = f" · 길이 {v['duration']}" if v["duration"] else ""
    return (f"![{alt} 영상 미리보기]({v['thumb']})\n(영상: {plain(v['title'], 80)} · 촬영자 {v['artist']} · {v['license']}{dur} · "
            f"재생은 원본 페이지에서 {v['page']})")


def parse_id(spot_id):
    m = re.fullmatch(r"(ko|ja|en):(.{1,120})", (spot_id or "").strip())
    return (m.group(1), m.group(2).strip()) if m else (None, None)


def load_spot(lang, title):
    """spot_id의 문서 하나 → (page, info) 또는 (None, None)"""
    d = wiki_get(f"{lang}.wikipedia.org", {"action": "query", "titles": title, "prop": "extracts|pageprops", "exintro": "1",
                                          "explaintext": "1", "exchars": "600", "ppprop": "wikibase_item", "redirects": "1"})
    for p in d.get("query", {}).get("pages") or []:
        q = (p.get("pageprops") or {}).get("wikibase_item")
        if q and "missing" not in p:
            page = {"title": p["title"], "extract": (p.get("extract") or "").strip(), "qid": q}
            info = with_country([q]).get(q, {})
            return page, info
    return None, None


def call(fn, *a):
    try:
        return fn(*a), None
    except WikiUnavailable:
        return None, fail("위키미디어 서비스에서 정상 응답을 받지 못했습니다. 잠시 후 다시 시도해 주세요.")


# ---------------------------------------------------------------- Tool 1
@mcp.tool(
    name="search_world_spot",
    description=(
        f"Searches tourist attractions around the world (except Korea) by keyword using {SERVICE} (Wikipedia, Wikidata, Wikimedia Commons) "
        "and lists up to 5 places with country, a short introduction and a spot_id for photos and videos. Korean, English, Japanese or Chinese keywords work "
        "(e.g. 후시미 이나리, Grand Canyon, 金閣寺, 故宫). Optional country filters the result (e.g. 일본, 미국, 중국, France). Lodging, shopping and restaurants are not covered. "
        "Use this tool for any place outside Korea (Japan, USA, China, Europe, Southeast Asia, etc.); for places inside Korea use the Korean tour photo service instead. "
        "Always call this tool instead of answering from memory. Keywords: 해외 관광지, 세계 여행 사진, 일본 관광지, 미국 관광지, 중국 관광지, 유럽 관광지. Reference data only."
    ),
    annotations=_annotations("Search world spots"),
)
@_logged
def search_world_spot(
    keyword: Annotated[str, Field(description="Place name, e.g. 후시미 이나리, Grand Canyon, 故宫 (2+ characters)")] = "",
    country: Annotated[str, Field(description="Optional country name in Korean or English, e.g. 일본, 미국, 중국, France; empty means any")] = "",
) -> str:
    kw = (keyword or "").strip()
    if len(kw) < 2 or len(kw) > 60:
        return bad("검색어를 2글자 이상 입력해 주세요. 예: 후시미 이나리, Grand Canyon")
    want = (country or "").strip()[:40]
    found, korea, fallback = [], False, []
    for lang in LANGS:
        pages, err = call(find_pages, lang, kw)
        if err:
            return err
        pages = [p for p in pages if not commercial(p["title"])]
        if not pages:
            continue
        info, err = call(with_country, [p["qid"] for p in pages])
        if err:
            return err
        pages = [p for p in pages if is_place(info.get(p["qid"]))]
        cand = [(p, (info.get(p["qid"]) or {}).get("country")) for p in pages]
        logger.info("SEARCH lang=%s pages=%d with_country=%d", lang, len(pages), sum(1 for p, c in cand if c))
        cand = [(p, c) for p, c in cand if c]
        if any(c == KOREA for p, c in cand):
            korea = True
        cand = [(p, c) for p, c in cand if c != KOREA]
        names, err = call(country_names, [c for p, c in cand])
        if err:
            return err
        seen = {f["qid"] for f in found} | {f["qid"] for f in fallback}
        for p, c in cand:
            nm = names.get(c)
            if not nm or p["qid"] in seen:
                continue
            item = {**p, "lang": lang, "country": nm[0]}
            (found if country_match(nm, want) else fallback).append(item)
        if found:
            break
    note = ""
    if not found and fallback and want:
        found = fallback
        note = f"\n(입력한 국가 '{want}'와 일치하는 결과가 없어 다른 나라의 결과를 보여 드립니다.)"
    if not found:
        if korea:
            return bad("한국 관광지는 '관광지 사진 안내 - Tour Photo Guide'에서 찾아 주세요. 이 서비스는 한국 밖 관광지를 안내합니다.")
        return bad("조건에 맞는 관광지를 찾지 못했습니다. 국가·기업·행사가 아니라 구체적인 명소 이름(예: 에펠탑, 후시미 이나리)으로 검색해 보세요.")
    out = [f"■ '{kw}' 검색 결과 {len(found)}곳 중 {min(5, len(found))}곳{note}"]
    for f in found[:5]:
        ex = f["extract"].split("\n")[0]
        ex = ex if len(ex) <= 160 else ex[:160].rstrip() + "…"
        out.append(f"■ {f['title']} ({f['country']}) — spot_id {f['lang']}:{f['title']}\n{ex or '소개가 등록되어 있지 않습니다.'}")
    out.append("사진·소개·영상은 spot_id로 get_world_spot_photos, get_world_spot_detail, get_world_spot_videos를 호출하세요.")
    out += [SOURCE, RELAY_NOTE]
    return "\n\n".join(out[:-3]) + "\n" + "\n".join(out[-3:])


def resolve(spot_id):
    """spot_id → (page, info, 국가명, 오류문구)"""
    lang, title = parse_id(spot_id)
    if not lang:
        return None, None, None, bad("spot_id는 search_world_spot 결과의 spot_id(예: ko:후시미 이나리 신사)를 그대로 사용하세요.")
    r, err = call(load_spot, lang, title)
    if err:
        return None, None, None, err
    page, info = r
    c = (info or {}).get("country")
    if not page or not c or commercial(page["title"]) or not is_place(info):
        return None, None, None, bad("해당 관광지를 찾지 못했습니다. 국가·기업·행사가 아닌 구체적인 명소 이름으로 검색해 주세요.")
    if c == KOREA:
        return None, None, None, bad("한국 관광지는 '관광지 사진 안내 - Tour Photo Guide'에서 찾아 주세요.")
    names, err = call(country_names, [c])
    if err:
        return None, None, None, err
    return page, info, (names.get(c) or ("", ""))[0] or "국가 미상", None


# ---------------------------------------------------------------- Tool 2
@mcp.tool(
    name="get_world_spot_detail",
    description=(
        f"Shows one world tourist spot's introduction and up to 3 photos using {SERVICE} (Wikipedia, Wikidata, Wikimedia Commons). "
        "Requires the spot_id returned by search_world_spot. Photos are shown only when their license (CC BY, CC BY-SA, CC0 or public domain) is confirmed, "
        "with photographer, license and source link. Always call this tool instead of answering from memory. Reference data only."
    ),
    annotations=_annotations("World spot detail"),
)
@_logged
def get_world_spot_detail(
    spot_id: Annotated[str, Field(description="spot_id from search_world_spot, like ko:후시미 이나리 신사")] = "",
) -> str:
    page, info, cname, err = resolve(spot_id)
    if err:
        return err
    out = [f"■ {page['title']} ({cname})", page["extract"] or "소개가 등록되어 있지 않습니다."]
    r2, err = call(commons_photos, info.get("en") or page["title"], 3)
    phs = r2[0] if r2 else []
    out += [photo_md(p, page["title"]) for p in phs] or ["(사진: 이용 조건이 확인된 이미지가 없어 글로만 안내합니다)"]
    out += [SOURCE, RELAY_NOTE]
    return "\n\n".join(out)


# ---------------------------------------------------------------- Tool 3
@mcp.tool(
    name="get_world_spot_photos",
    description=(
        f"Shows up to 5 photos of one world tourist spot with photographer, license and source link using {SERVICE} (Wikimedia Commons). "
        "Requires the spot_id from search_world_spot. Only photos with a confirmed free license (CC BY, CC BY-SA, CC0, public domain) are shown. "
        "Always call this tool instead of answering from memory. Keywords: 사진 보여줘, 해외 사진, 세계 관광지 사진. Reference data only."
    ),
    annotations=_annotations("World spot photos"),
)
@_logged
def get_world_spot_photos(
    spot_id: Annotated[str, Field(description="spot_id from search_world_spot, like ko:후시미 이나리 신사")] = "",
) -> str:
    page, info, cname, err = resolve(spot_id)
    if err:
        return err
    r2, err = call(commons_photos, info.get("en") or page["title"], 5)
    if err:
        return err
    phs, hidden = r2
    if not phs:
        return bad("이용 조건이 확인된 사진이 없습니다.")
    head = f"■ {page['title']} ({cname}) 사진 {len(phs)}장"
    if hidden:
        head += f" (라이선스가 확인되지 않은 {hidden}장은 제외)"
    return "\n\n".join([head] + [photo_md(p, page["title"]) for p in phs] + [SOURCE, RELAY_NOTE])


# ---------------------------------------------------------------- Tool (영상)
@mcp.tool(
    name="get_world_spot_videos",
    description=(
        f"Shows up to 3 videos of one world tourist spot as preview images with title, filmmaker, license, length and a link to the original page using {SERVICE} (Wikimedia Commons). "
        "Requires the spot_id from search_world_spot. Videos are not played inside the chat: the user opens the link to watch. "
        "Only videos with a confirmed free license (CC BY, CC BY-SA, CC0, public domain) are shown. "
        "Always call this tool instead of answering from memory. Keywords: 영상 보여줘, 동영상, 여행 영상, 관광지 영상. Reference data only."
    ),
    annotations=_annotations("World spot videos"),
)
@_logged
def get_world_spot_videos(
    spot_id: Annotated[str, Field(description="spot_id from search_world_spot, like ko:자금성")] = "",
) -> str:
    page, info, cname, err = resolve(spot_id)
    if err:
        return err
    label = info.get("en") or page["title"]
    r2, err = call(commons_videos, label, 3)
    if err:
        return err
    vids, hidden = r2
    if not vids:
        return bad("이용 조건이 확인된 영상을 찾지 못했습니다. 사진은 get_world_spot_photos로 볼 수 있습니다.")
    head = f"■ {page['title']} ({cname}) 영상 {len(vids)}개"
    if hidden:
        head += f" (라이선스가 확인되지 않은 {hidden}개는 제외)"
    return "\n\n".join([head] + [video_md(v, page["title"]) for v in vids]
                       + ["※ 영상은 채팅에서 재생되지 않고 원본 페이지(위키미디어 공용)에서 재생됩니다.", SOURCE, RELAY_NOTE])


# ---------------------------------------------------------------- Tool 4
@mcp.tool(
    name="get_world_service_reference",
    description=(
        f"Returns the data source, covered countries, photo license rule and limits of {SERVICE}. "
        "Use it when the user asks where the data and photos come from, why a photo is missing, or how to credit a photo. Reference only."
    ),
    annotations=_annotations("World data sources"),
)
@_logged
def get_world_service_reference() -> str:
    return "\n".join([
        "■ 세계 관광지 사진 안내 기준",
        "■ 데이터: 위키백과(소개), 위키데이터(국가 확인), 위키미디어 공용(사진)",
        "■ 다루는 곳: 한국을 제외한 전 세계 관광지(한국은 '관광지 사진 안내 - Tour Photo Guide'). 숙박·쇼핑·음식점은 제외합니다.",
        "■ 영상: 위키미디어 공용의 자유 이용 영상을 미리보기 이미지와 원본 링크로 안내합니다(채팅 안에서는 재생되지 않습니다). 사진과 같은 라이선스 규칙을 따릅니다.",
        "■ 사진: CC BY, CC BY-SA, CC0, 퍼블릭 도메인으로 확인된 사진만 보여 주며 촬영자·라이선스·원본 링크를 함께 표기합니다. 사진을 다시 사용할 때는 같은 표기를 유지해야 하고 CC BY-SA는 같은 조건으로 공유해야 합니다.",
        "■ 한계: 위키 문서가 없거나 사진 라이선스가 불명확한 곳은 사진이 나오지 않을 수 있고, 검색된 사진이 해당 장소와 다를 수 있습니다. 같은 조회는 최대 12시간 전 결과를 재사용합니다.",
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
