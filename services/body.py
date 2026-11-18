"""인체 해부도 안내 - Body Atlas MCP 서버
정해진 인체 부위 목록(장기·뼈·근육·관절·계통)의 교육용 해부도 그림을 위키미디어 공용(Wikimedia Commons)에서 찾아 출처·라이선스와 함께 보여 줍니다.
- 진단·치료·증상·약 정보는 다루지 않습니다. 해당 질문은 안내 문구만 돌려주고, 모든 응답에 교육용 면책 문구를 붙입니다.
- 사진이 아닌 해부도 그림만(Gray's Anatomy 도판, Blausen, NCI 일러스트) 사용해 실제 환자·수술 사진이 나오지 않게 합니다.
- 인증키가 없습니다. 입력값(검색어)은 저장·로그하지 않습니다. 연락처는 선택 환경변수 WIKI_CONTACT.
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

SERVICE = "인체 해부도 안내 - Body Atlas"   # 콘솔 등록명과 글자까지 동일해야 함
mcp = FastMCP(
    SERVICE,
    instructions=("정해진 인체 부위의 교육용 해부도 그림을 보여 주는 도구입니다. 증상·질병·진단·치료·약 질문에는 쓰지 말고 의료진 상담을 안내하세요. "
                  "응답의 마크다운 이미지(![...](...))와 출처 표기, 면책 문구를 그대로 전달하고 응답에 없는 의학 설명을 덧붙이지 마세요."),
)

TIMEOUT = 6.0
CACHE_TTL = 12 * 3600
DISCLAIMER = "※ 교육용 일반 해부도입니다. 진단·치료·증상 판단에 사용할 수 없으며, 건강 문제는 의사·약사 등 전문가와 상담하세요."
SOURCE = "출처: 위키백과·위키미디어 공용(Wikimedia Commons)"
RELAY_NOTE = "※ 답변 시 위 부위 이름·그림·출처·면책 문구를 그대로 전달하고, 이 응답에 없는 의학적 설명이나 건강 조언을 덧붙이지 마세요."
OK_LICENSE = re.compile(r"^(CC BY(-SA)?[ -]\d\.\d|CC0|Public domain|PD\b)", re.I)
# 그림 출처 허용 목록: Gray's Anatomy 도판, Blausen Medical 일러스트(CC BY 3.0), NCI 일러스트
OK_FILE = re.compile(r"^File:(Gray\d+(_az)?\.(png|jpg)|Blausen[ _]\d+|Illu[ _])", re.I)   # Gray 원본·채색판만(스페인어 등 번역판 제외)
MEDICAL_RE = re.compile(r"증상|통증|아프|아파|질환|질병|병원|진단|치료|수술|약|처방|암|염증|감염|응급|부작용|다이어트|임신|낙태|성기|생식기|자궁|고환|음경|유방|\b(pain|symptom|disease|cancer|treatment|surgery|diagnos\w*|drug|medicine)\b", re.I)

# (한국어, 영어, 분류, 별칭)
CATALOG = [
    # 장기
    ("심장", "Heart", "장기", ["하트"]), ("폐", "Lung", "장기", ["허파"]), ("간", "Liver", "장기", []), ("위", "Stomach", "장기", []),
    ("신장", "Kidney", "장기", ["콩팥"]), ("췌장", "Pancreas", "장기", ["이자"]), ("비장", "Spleen", "장기", ["지라"]),
    ("담낭", "Gallbladder", "장기", ["쓸개"]), ("방광", "Urinary bladder", "장기", []), ("소장", "Small intestine", "장기", ["작은창자"]),
    ("대장", "Large intestine", "장기", ["큰창자"]), ("식도", "Esophagus", "장기", []), ("기관", "Trachea", "장기", ["기도"]),
    ("후두", "Larynx", "장기", []), ("갑상선", "Thyroid", "장기", []), ("뇌", "Brain", "장기", ["두뇌"]), ("척수", "Spinal cord", "장기", []),
    ("눈", "Eye", "장기", ["안구"]), ("귀", "Ear", "장기", []), ("코", "Nose", "장기", []), ("혀", "Tongue", "장기", []),
    ("치아", "Tooth", "장기", ["이빨"]), ("피부", "Skin", "장기", []),
    # 뼈
    ("두개골", "Skull", "뼈", ["머리뼈"]), ("척추", "Vertebral column", "뼈", ["등뼈"]), ("갈비뼈", "Rib cage", "뼈", ["늑골", "흉곽"]),
    ("흉골", "Sternum", "뼈", ["가슴뼈"]), ("쇄골", "Clavicle", "뼈", ["빗장뼈"]), ("견갑골", "Scapula", "뼈", ["어깨뼈"]),
    ("상완골", "Humerus", "뼈", ["위팔뼈"]), ("요골", "Radius", "뼈", ["노뼈"]), ("척골", "Ulna", "뼈", ["자뼈"]),
    ("골반", "Pelvis", "뼈", ["엉덩이뼈"]), ("대퇴골", "Femur", "뼈", ["넙다리뼈"]), ("경골", "Tibia", "뼈", ["정강뼈"]),
    ("비골", "Fibula", "뼈", ["종아리뼈"]), ("슬개골", "Patella", "뼈", ["무릎뼈"]), ("하악골", "Mandible", "뼈", ["턱뼈"]),
    ("손뼈", "Bones of the hand", "뼈", ["손 뼈"]), ("발뼈", "Bones of the foot", "뼈", ["발 뼈"]),
    # 근육
    ("이두근", "Biceps brachii", "근육", ["알통"]), ("삼두근", "Triceps brachii", "근육", []), ("대흉근", "Pectoralis major", "근육", ["가슴근육"]),
    ("광배근", "Latissimus dorsi", "근육", ["등근육"]), ("승모근", "Trapezius", "근육", []), ("삼각근", "Deltoid", "근육", ["어깨근육"]),
    ("복직근", "Rectus abdominis", "근육", ["복근"]), ("대둔근", "Gluteus maximus", "근육", ["엉덩이근육"]),
    ("대퇴사두근", "Quadriceps femoris", "근육", ["허벅지근육"]), ("햄스트링", "Hamstring", "근육", ["허벅지 뒤근육"]),
    ("비복근", "Gastrocnemius", "근육", ["종아리근육"]), ("횡격막", "Diaphragm", "근육", ["가로막"]),
    # 관절
    ("어깨관절", "Shoulder joint", "관절", ["어깨 관절"]), ("팔꿈치관절", "Elbow", "관절", ["팔꿈치"]), ("손목관절", "Wrist", "관절", ["손목"]),
    ("고관절", "Hip joint", "관절", ["엉덩관절"]), ("무릎관절", "Knee", "관절", ["무릎"]), ("발목관절", "Ankle", "관절", ["발목"]),
    # 계통
    ("순환계", "Circulatory system", "계통", ["순환기계", "혈액순환계"]), ("소화계", "Digestive system", "계통", ["소화기계"]),
    ("호흡계", "Respiratory system", "계통", ["호흡기계"]), ("신경계", "Nervous system", "계통", []),
    ("골격계", "Human skeleton", "계통", ["뼈대", "전신 뼈"]), ("근육계", "Muscular system", "계통", ["전신 근육"]),
    ("비뇨기계", "Urinary system", "계통", ["비뇨계"]), ("림프계", "Lymphatic system", "계통", []), ("내분비계", "Endocrine system", "계통", []),
]


class WikiUnavailable(Exception):
    pass


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)
logger = logging.getLogger("bodyatlas")
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
    ck = (host, tuple(sorted(params.items())))
    hit = _CACHE.get(ck)
    if hit and hit[0] > time.time():
        return hit[1]
    t0 = time.perf_counter()
    try:
        import httpx
        ua = f"BodyAtlasGuide/1.0 ({os.environ.get('WIKI_CONTACT', '').strip() or 'https://misatech-mcp-hub.onrender.com'}) httpx"
        r = httpx.get(f"https://{host}/w/api.php", params={"format": "json", "formatversion": "2", **params},
                      headers={"User-Agent": ua, "Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or "error" in data:
            raise ValueError
    except Exception as e:
        code = getattr(getattr(e, "response", None), "status_code", "-")
        logger.info("WIKI host=%s status=error type=%s http=%s", host.split(".")[0], type(e).__name__, code)
        raise WikiUnavailable() from None
    logger.info("WIKI host=%s ms=%d", host.split(".")[0], int((time.perf_counter() - t0) * 1000))
    if len(_CACHE) > 300:
        _CACHE.clear()
    _CACHE[ck] = (time.time() + CACHE_TTL, data)
    return data


def call(fn, *a):
    try:
        return fn(*a), None
    except WikiUnavailable:
        return None, fail("위키미디어 서비스에서 정상 응답을 받지 못했습니다. 잠시 후 다시 시도해 주세요.")


# ---------------------------------------------------------------- 도우미
def norm(s):
    return re.sub(r"[\s\-_·]+", "", (s or "").lower())


def find_parts(keyword):
    """목록에서 이름·영문명·별칭이 겹치는 부위 (정확히 같은 것 우선)"""
    k = norm(keyword)
    if not k:
        return []
    exact, part = [], []
    for e in CATALOG:
        names = [norm(e[0]), norm(e[1])] + [norm(a) for a in e[3]]
        if k in names:
            exact.append(e)
        elif len(k) >= 2 and any(k in n or (len(n) >= 2 and n in k) for n in names):
            part.append(e)
    return (exact or part)[:5]


def plain(s, n=60):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def commons_images(en, limit=3):
    """해부도 그림만: 허용 파일(Gray 도판·Blausen·NCI) + 라이선스 확인 + 이미지(jpeg/png)"""
    out, seen = [], set()
    for q in (f"Gray's Anatomy {en}", f"{en} anatomy Blausen"):
        d = wiki_get("commons.wikimedia.org", {"action": "query", "generator": "search", "gsrsearch": f"{q} filetype:bitmap",
                                               "gsrnamespace": "6", "gsrlimit": "20", "prop": "imageinfo",
                                               "iiprop": "url|extmetadata|mime|size", "iiurlwidth": "800"})
        pages = sorted((d.get("query", {}).get("pages") or []), key=lambda p: p.get("index", 99))
        for p in pages:
            title = p.get("title", "")
            ii = (p.get("imageinfo") or [{}])[0]
            md = ii.get("extmetadata") or {}
            lic = (md.get("LicenseShortName") or {}).get("value", "")
            url = ii.get("thumburl") or ""
            if (title in seen or not OK_FILE.match(title) or ii.get("mime") not in ("image/jpeg", "image/png")
                    or (ii.get("width") or 0) < 400 or not OK_LICENSE.match(lic or "")
                    or (md.get("Restrictions") or {}).get("value") or not url.startswith("https://")):
                continue
            seen.add(title)
            out.append({"url": url, "artist": plain((md.get("Artist") or {}).get("value", "")) or "작자 미상",
                        "license": lic, "page": ii.get("descriptionurl", "")})
            if len(out) >= limit:
                return out
    return out


def intro(ko, en):
    """위키백과 소개 첫 부분(한국어 → 영어 순)"""
    for host, title in (("ko.wikipedia.org", ko), ("en.wikipedia.org", en)):
        d = wiki_get(host, {"action": "query", "titles": title, "prop": "extracts", "exintro": "1", "explaintext": "1",
                            "exchars": "350", "redirects": "1"})
        for p in d.get("query", {}).get("pages") or []:
            ex = (p.get("extract") or "").strip()
            if ex and "missing" not in p:
                return ex.split("\n")[0]
    return ""


def image_md(im, alt):
    alt = re.sub(r"\s+", " ", re.sub(r"[\[\]\(\)]", " ", alt or "해부도")).strip()
    return f"![{alt}]({im['url']})\n(그림: {im['artist']} · {im['license']} · 원본 {im['page']})"


def catalog_text():
    by = {}
    for ko, en, cat, _ in CATALOG:
        by.setdefault(cat, []).append(ko)
    return "\n".join(f"· {c}: {', '.join(v)}" for c, v in by.items())


# ---------------------------------------------------------------- Tool 1
@mcp.tool(
    name="search_body_part",
    description=(
        f"Finds a human body part (organ, bone, muscle, joint or body system) in the fixed list of {SERVICE} by Korean or English name "
        "(e.g. 심장, 대퇴골, 이두근, 무릎, 순환계) and shows its Korean/English names and category. With an empty keyword it lists all supported parts. "
        "Only educational anatomy diagrams are provided: no symptoms, diseases, diagnosis, treatment or medicine. "
        "Always call this tool instead of answering from memory. Keywords: 인체 해부도, 장기, 뼈, 근육, 관절. Educational reference only."
    ),
    annotations=_annotations("Find body part"),
)
@_logged
def search_body_part(
    keyword: Annotated[str, Field(description="Body part name in Korean or English, e.g. 심장, femur; empty lists all supported parts")] = "",
) -> str:
    kw = (keyword or "").strip()[:40]
    if kw and MEDICAL_RE.search(kw):
        return bad("증상·질병·진단·치료·약에 관한 질문은 안내하지 않습니다. 건강 문제는 의사·약사 등 전문가와 상담하세요. 이 서비스는 일반 인체 부위의 교육용 해부도만 보여 줍니다.")
    if not kw:
        return "■ 지원하는 인체 부위\n" + catalog_text() + "\n부위 이름으로 get_body_part_images를 호출하면 해부도를 볼 수 있습니다.\n" + DISCLAIMER
    found = find_parts(kw)
    if not found:
        return bad("지원하는 목록에서 해당 부위를 찾지 못했습니다. 검색어를 비우고 호출하면 지원하는 부위 목록을 볼 수 있습니다.")
    out = ["■ 검색 결과"]
    out += [f"· {ko} ({en}) — {cat}" for ko, en, cat, _ in found]
    out += ["해부도는 부위 이름으로 get_body_part_images를 호출하세요.", DISCLAIMER, SOURCE, RELAY_NOTE]
    return "\n".join(out)


# ---------------------------------------------------------------- Tool 2
@mcp.tool(
    name="get_body_part_images",
    description=(
        f"Shows educational anatomy diagrams (Gray's Anatomy plates, Blausen and NCI illustrations, not patient photos) of one human body part from {SERVICE}, "
        "with a short Wikipedia introduction, artist, license and source link. Use a name supported by search_body_part (e.g. 심장, 대퇴골, 이두근). "
        "Never use it for symptoms, diseases, diagnosis, treatment or medicine. Always call this tool instead of answering from memory. "
        "Keywords: 해부도 보여줘, 심장 해부도, 뼈 그림, 근육 그림. Educational reference only."
    ),
    annotations=_annotations("Body part diagrams"),
)
@_logged
def get_body_part_images(
    part: Annotated[str, Field(description="Body part name from the supported list, e.g. 심장, 대퇴골, 이두근, 순환계")] = "",
) -> str:
    kw = (part or "").strip()[:40]
    if not kw:
        return bad("부위 이름을 입력해 주세요. 예: 심장, 대퇴골, 이두근 (목록은 search_body_part를 빈 검색어로 호출)")
    if MEDICAL_RE.search(kw):
        return bad("증상·질병·진단·치료·약에 관한 질문은 안내하지 않습니다. 건강 문제는 의사·약사 등 전문가와 상담하세요.")
    found = find_parts(kw)
    if not found:
        return bad("지원하는 목록에서 해당 부위를 찾지 못했습니다. search_body_part를 빈 검색어로 호출하면 목록을 볼 수 있습니다.")
    ko, en, cat, _ = found[0]
    ex, err = call(intro, ko, en)
    if err:
        return err
    ims, err = call(commons_images, en, 3)
    if err:
        return err
    out = [f"■ {ko} ({en}) — {cat}"]
    if ex:
        out.append(ex)
    out += [image_md(im, f"{ko} 해부도") for im in ims] or ["(이용 조건이 확인된 해부도 그림을 찾지 못했습니다)"]
    out += [DISCLAIMER, SOURCE, RELAY_NOTE]
    return "\n\n".join(out)


# ---------------------------------------------------------------- Tool 3
@mcp.tool(
    name="get_body_atlas_reference",
    description=(
        f"Returns the data source, scope, image rules and limits of {SERVICE}. "
        "Use it when the user asks where the images come from, what is covered, or why a medical question is not answered. Reference only."
    ),
    annotations=_annotations("Body atlas rules"),
)
@_logged
def get_body_atlas_reference() -> str:
    return "\n".join([
        "■ 인체 해부도 안내 기준",
        "■ 다루는 것: 정해진 목록의 인체 부위(장기·뼈·근육·관절·계통)의 교육용 해부도 그림과 위키백과 소개",
        "■ 다루지 않는 것: 증상·질병·진단·치료·약·건강 조언, 환자·수술 사진, 생식기 등 민감 부위",
        "■ 그림 출처: Gray's Anatomy 도판(퍼블릭 도메인), Blausen Medical(CC BY 3.0), NCI 일러스트. 작자·라이선스·원본 링크를 함께 표기하며 다시 사용할 때도 같은 표기를 유지해야 합니다.",
        "■ 한계: 오래된 도판은 영어 표기와 옛 그림체이며, 최신 의학 지식과 다를 수 있습니다. 그림이 없으면 글만 나옵니다. 같은 조회는 최대 12시간 전 결과를 재사용합니다.",
        DISCLAIMER, SOURCE, "입력한 검색어는 저장하지 않습니다."])


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
