"""사다리 게임 - Ladder Pick MCP 서버
점심·커피·당번 같은 가벼운 '정하기'를 서버가 공정하게 뽑아 주는 사다리타기 도구입니다. 돈이 오가는 내기·금액은 받지 않습니다.
- 사다리와 결과는 AI가 아닌 서버가 무작위로 정하며, 한 명씩 공개할 수 있도록 '사다리 코드'만으로 같은 결과를 다시 계산합니다(서버에 저장 없음).
- 참가자 이름과 결과 문구는 저장하거나 로그에 남기지 않고, 툴 이름·상태·처리시간만 기록합니다.
"""
import functools
import hashlib
import logging
import os
import random
import re
import time
import unicodedata
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

SERVICE = "사다리 게임 - Ladder Pick"   # 콘솔 등록명과 글자까지 동일해야 함
ID_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MIN_N, MAX_N = 2, 8
RELAY_NOTE = "※ 답변 시 이 응답의 사다리 그림·연출 문구·결과를 그대로(줄이지 말고) 전달하고, 결과를 바꾸거나 직접 추측해 덧붙이지 마세요."
MONEY_RE = re.compile(r"판돈|배팅|베팅|현금|입금|송금|계좌|도박|\d[\d,]*\s*(?:원|만\s?원|천\s?원|달러|엔)|[0-9]\s*만(?!\w)|\$")
SYSTEM_RNG = random.SystemRandom()

mcp = FastMCP(SERVICE, instructions=(
    "점심·커피·당번 정하기용 사다리타기 도구입니다. 참가자 이름(2~8명)과 결과 문구를 받아 서버가 무작위로 결과를 정합니다. "
    "결과는 항상 도구 응답 그대로 전달하세요. 돈이 걸린 내기나 금액은 다루지 않습니다."))


def _annotations(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


logger = logging.getLogger("ladderpick")
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
        except Exception:
            result = "[조회 실패] 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            return result
        finally:
            logger.info("TOOL name=%s status=%s ms=%d len=%d", func.__name__, status,
                        int((time.perf_counter() - t0) * 1000), len(result or ""))
    return wrapper


def bad(m):
    return f"[안내] {m}"


def _salt():
    return os.environ.get("GAME_SALT", "misatech-brain-games-v1")


# ---------------------------------------------------------------- 입력 처리
def split_items(text):
    s = (text or "").strip()
    if not s:
        return []
    if re.search(r"[,，、/\n;]", s):
        parts = re.split(r"[,，、/\n;]+", s)
    else:
        parts = s.split()
    return [p.strip() for p in parts if p.strip()]


def parse_names(names):
    items = split_items(names)
    if not MIN_N <= len(items) <= MAX_N:
        return None, bad(f"참가자는 {MIN_N}~{MAX_N}명으로 쉼표로 구분해 입력해 주세요. 예: 민수, 지영, 철수")
    if any(len(x) > 10 for x in items):
        return None, bad("이름은 한 명당 10자 이내로 입력해 주세요.")
    if len(set(items)) != len(items):
        return None, bad("같은 이름이 두 번 들어 있어요. 이름을 구분해 입력해 주세요(예: 김철수A, 김철수B).")
    if any(MONEY_RE.search(x) for x in items):
        return None, bad(MONEY_MSG)
    return items, None


MONEY_MSG = "이 서비스는 돈이 걸린 내기나 금액은 다루지 않아요. 점심·커피·당번처럼 벌칙이나 역할을 정하는 용도로 입력해 주세요."


def fill_outcomes(outcomes, n):
    items = split_items(outcomes)
    if any(len(x) > 15 for x in items):
        return None, bad("결과 문구는 한 칸당 15자 이내로 입력해 주세요.")
    if any(MONEY_RE.search(x) for x in items):
        return None, bad(MONEY_MSG)
    if len(items) > n:
        return None, bad(f"결과 칸({len(items)}개)이 참가자 수({n}명)보다 많아요. 참가자 수 이하로 입력해 주세요.")
    if not items:
        items = ["꽝"]
    items += ["통과(PASS)"] * (n - len(items))
    return items, None


# ---------------------------------------------------------------- 사다리 만들기
def checksum(names, outcomes):
    canon = "|".join(sorted(names)) + "#" + "|".join(sorted(outcomes))
    h = hashlib.sha256(canon.encode()).hexdigest()
    return "".join(ID_CHARS[int(h[i:i + 2], 16) % len(ID_CHARS)] for i in (0, 2))


def new_code(names, outcomes):
    return "".join(SYSTEM_RNG.choice(ID_CHARS) for _ in range(6)) + checksum(names, outcomes)


def build(code, names, outcomes):
    """(정렬된 이름, 사다리 가로줄 목록, 이름→결과 dict, 이름→도착칸 dict). 같은 입력이면 항상 같은 결과."""
    cols = sorted(names)
    n = len(cols)
    seed = int(hashlib.sha256((_salt() + code[:6]).encode()).hexdigest(), 16)
    rnd = random.Random(seed)
    rows = 10 if n <= 4 else 12
    rungs = []   # 각 행의 가로줄 위치 집합(j: j번째와 j+1번째 사이)
    for _ in range(rows):
        for _try in range(6):   # 바로 윗줄과 똑같은 줄(두 번 건너 제자리)이 되지 않도록 다시 뽑는다
            row, j = set(), 0
            while j < n - 1:
                if rnd.random() < 0.5:
                    row.add(j)
                    j += 2
                else:
                    j += 1
            if not (rungs and row == rungs[-1]):
                break
        rungs.append(row)
    shuffled = sorted(outcomes)
    rnd.shuffle(shuffled)
    end = {}
    for i, name in enumerate(cols):
        pos = i
        for row in rungs:
            if pos in row:
                pos += 1
            elif pos - 1 in row:
                pos -= 1
        end[name] = pos
    result = {name: shuffled[end[name]] for name in cols}
    return cols, rungs, result, end


def trace(rungs, start):
    """start 칸에서 내려가는 길: 각 행의 (들어갈 때 칸, 나갈 때 칸) 목록과 도착 칸"""
    pos, steps = start, []
    for row in rungs:
        nxt = pos + 1 if pos in row else (pos - 1 if pos - 1 in row else pos)
        steps.append((pos, nxt))
        pos = nxt
    return steps, pos


def dwidth(t):
    """글자 표시 폭(한글·이모지 등 넓은 글자는 2칸)"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in t)


def pad(t, w):
    return t + " " * max(0, w - dwidth(t))


FENCE_LABEL = "사다리게임"   # 코드 블록 왼쪽 위에 표시되는 이름(기본은 text)
MAX_PIC = 46   # 그림 한 줄 최대 폭(칸): 넘으면 이름·결과 대신 숫자·알파벳으로 그린다


def draw(cols, rungs, start=None, top=None, bottom=None):
    """사다리 그림(코드 블록). top/bottom에 이름·결과 문구를 주면 폭이 맞을 때 그대로 표시, 안 맞으면 숫자·알파벳.
    start를 주면 그 칸의 길을 * 로 표시한다. 반환: (그림, 이름·결과 표시 여부)"""
    n = len(cols)
    labels = (top or []) + (bottom or [])
    w = max([4] + [dwidth(x) + 1 for x in labels]) if labels else 4
    fancy = bool(labels) and n * w <= MAX_PIC
    if not fancy:
        w = 4
    steps = trace(rungs, start)[0] if start is not None else None
    t_lab = top if (fancy and top) else [str(i + 1) for i in range(n)]
    b_lab = bottom if (fancy and bottom) else [chr(65 + i) for i in range(n)]
    lines = ["".join(pad(x, w) for x in t_lab).rstrip()]
    for r, row in enumerate(rungs):
        a_, b_ = steps[r] if steps else (-1, -1)
        line = ""
        for j in range(n):
            line += "*" if j in (a_, b_) else "|"
            if j < n - 1:
                if j in row:
                    line += ("*" if (steps and {a_, b_} == {j, j + 1}) else "-") * (w - 1)
                else:
                    line += " " * (w - 1)
        lines.append(line)
    lines.append("".join(pad(x, w) for x in b_lab).rstrip())
    return "```" + FENCE_LABEL + "\n" + "\n".join(lines) + "\n```", fancy


INTROS = ["🥁 두구두구두구…", "🪜 사다리를 타고 슝슝 내려가는 중…", "👀 과연 누가 걸릴까요?", "🎲 서버가 공정하게 흔드는 중!", "😳 숨죽이고 지켜보세요…"]
REACT_KEYS = [("꽝", "😱 꽝!"), ("PASS", "😮‍💨 살았다!"), ("통과", "😮‍💨 살았다!"), ("커피", "☕ 오늘의 주인공!"), ("점심", "🍱 오늘의 주인공!"),
              ("밥", "🍚 오늘의 주인공!"), ("청소", "🧹 오늘의 주인공!"), ("설거지", "🧽 오늘의 주인공!"), ("발표", "🎤 오늘의 주인공!"), ("간식", "🍪 오늘의 주인공!")]


def reaction(outcome):
    for k, v in REACT_KEYS:
        if k in outcome:
            return v
    return "🎯 당첨!"


def is_pass(outcome):
    return "PASS" in outcome or "통과" in outcome


def intro():
    return SYSTEM_RNG.choice(INTROS)


def result_lines(names, result):
    return [f"  - {c} → {result[c]}  {reaction(result[c])}" for c in names]


def stars(names, result):
    w = [c for c in names if not is_pass(result[c]) and "꽝" not in result[c]]
    k = [c for c in names if "꽝" in result[c]]
    out = []
    if k:
        out.append("😱 꽝의 주인공: " + ", ".join(f"{c}" for c in k))
    if w:
        out.append("🎯 오늘의 주인공: " + ", ".join(f"{c}({result[c]})" for c in w))
    return out


def legend(cols, fancy=False):
    return "" if fancy else "참가자: " + " / ".join(f"{i + 1}={c}" for i, c in enumerate(cols))


@mcp.tool(
    name="make_ladder",
    description=(
        f"Draws a ladder game (사다리타기) with {SERVICE} and shows every participant's result at once. Pass participant names (2-8, comma separated) and result labels "
        "(e.g. 꽝, 통과, 커피 사기, 점심 쏘기; empty means one 꽝 and the rest PASS). The server randomizes fairly; never decide results yourself. "
        "Money bets and amounts are refused. Keywords: 사다리 게임, 사다리타기, 점심 내기, 커피 내기, 누가 쏠지, 당번 정하기, 꽝 통과."
    ),
    annotations=_annotations("Ladder game, all results"),
)
@_logged
def make_ladder(
    names: Annotated[str, Field(description="Participant names, 2-8, comma separated, e.g. 민수, 지영, 철수")] = "",
    outcomes: Annotated[str, Field(description="Result labels, comma separated, no more than participants, e.g. 꽝, 커피 사기; empty = 꽝 + PASS")] = "",
) -> str:
    ns, err = parse_names(names)
    if err:
        return err
    os_, err = fill_outcomes(outcomes, len(ns))
    if err:
        return err
    code = new_code(ns, os_)
    cols, rungs, result, end = build(code, ns, os_)
    bottom = [None] * len(cols)
    for c in cols:
        bottom[end[c]] = result[c]
    pic, fancy = draw(cols, rungs, top=cols, bottom=bottom)
    out = [intro(), "■ 사다리 결과", pic]
    if not fancy:
        out += [legend(cols), "도착 칸 결과: " + " / ".join(f"{chr(65 + i)}={bottom[i]}" for i in range(len(cols)))]
    out += ["· 결과"]
    out += result_lines(ns, result)
    out += stars(ns, result)
    out += [f"· 사다리 코드: {code} (같은 참가자·결과로 reveal_ladder에 넣으면 같은 결과를 다시 볼 수 있어요)", RELAY_NOTE]
    return "\n".join(out)


@mcp.tool(
    name="create_hidden_ladder",
    description=(
        f"Creates a ladder game of {SERVICE} but keeps the results hidden so players can reveal them one by one with reveal_ladder. Pass participant names (2-8) and result labels. "
        "Returns the ladder picture and a ladder code; do not guess or reveal results yourself. Money bets and amounts are refused. "
        "Keywords: 사다리 한 명씩, 결과 숨기고, 사다리 시작, 한 명씩 공개."
    ),
    annotations=_annotations("Ladder game, hidden results"),
)
@_logged
def create_hidden_ladder(
    names: Annotated[str, Field(description="Participant names, 2-8, comma separated")] = "",
    outcomes: Annotated[str, Field(description="Result labels, comma separated; empty = 꽝 + PASS")] = "",
) -> str:
    ns, err = parse_names(names)
    if err:
        return err
    os_, err = fill_outcomes(outcomes, len(ns))
    if err:
        return err
    code = new_code(ns, os_)
    cols, rungs, _, _ = build(code, ns, os_)
    final = ", ".join(os_)
    pic, fancy = draw(cols, rungs, top=cols, bottom=["?"] * len(cols))
    return "\n".join([
        "🔒 사다리를 만들었어요! 결과는 아직 비밀이에요. 😏", pic] + ([] if fancy else [legend(cols)]) + [
        f"· 결과 칸({len(os_)}개): {final} (어느 칸에 있는지는 비밀)",
        f"· 사다리 코드: {code}",
        "· 한 명씩 공개하려면 이름을 말해 주세요. 공개할 때마다 코드, 참가자 이름 전체, 결과 문구 전체를 처음 입력한 그대로 reveal_ladder에 전달하세요.",
        RELAY_NOTE])


@mcp.tool(
    name="reveal_ladder",
    description=(
        f"Reveals the result of one participant (or everyone) of a hidden ladder created with create_hidden_ladder in {SERVICE}. "
        "Pass the ladder code, the SAME full participant names and result labels used at creation, and the name to reveal (empty = everyone). "
        "Keywords: 사다리 결과 공개, 내 결과, 한 명씩 확인."
    ),
    annotations=_annotations("Reveal ladder result"),
)
@_logged
def reveal_ladder(
    code: Annotated[str, Field(description="Ladder code, 8 characters, e.g. K7M2QXAB")] = "",
    names: Annotated[str, Field(description="The same full participant names as at creation, comma separated")] = "",
    outcomes: Annotated[str, Field(description="The same result labels as at creation, comma separated (empty if it was empty)")] = "",
    who: Annotated[str, Field(description="Participant to reveal; empty reveals everyone")] = "",
) -> str:
    code = (code or "").strip().upper()
    if not re.fullmatch(r"[%s]{8}" % ID_CHARS, code):
        return bad("사다리 코드를 확인해 주세요(8자리). 새 사다리는 create_hidden_ladder로 만들 수 있어요.")
    ns, err = parse_names(names)
    if err:
        return err
    os_, err = fill_outcomes(outcomes, len(ns))
    if err:
        return err
    if checksum(ns, os_) != code[6:]:
        return bad("참가자나 결과 문구가 처음 만들 때와 달라요. 처음 입력한 이름 전체와 결과 문구 전체를 그대로 넣어 주세요.")
    cols, rungs, result, end = build(code, ns, os_)
    who = (who or "").strip()
    if who:
        if who not in result:
            return bad("그 이름은 참가자에 없어요. 참가자: " + ", ".join(ns))
        i = cols.index(who)
        steps, endpos = trace(rungs, i)
        pic, fancy = draw(cols, rungs, i, top=cols, bottom=["?"] * len(cols))
        return "\n".join([intro(), f"🪜 {who}({i + 1}번째 칸)의 길 — * 표시를 따라가요", pic,
                          f"도착: 왼쪽에서 {endpos + 1}번째 칸", f"👉 {who} → {result[who]}  {reaction(result[who])}", RELAY_NOTE])
    out = [intro(), "■ 전체 결과"] + result_lines(ns, result) + stars(ns, result) + [RELAY_NOTE]
    return "\n".join(out)


@mcp.tool(
    name="pick_one_person",
    description=(
        f"Picks one participant at random with {SERVICE} (e.g. who buys coffee today, who goes first). Pass participant names (2-8, comma separated). "
        "The server draws fairly; never choose yourself. Money bets and amounts are refused. Keywords: 한 명 뽑기, 당번 뽑기, 커피 누가 사올지, 순서 정하기."
    ),
    annotations=_annotations("Pick one person"),
)
@_logged
def pick_one_person(
    names: Annotated[str, Field(description="Participant names, 2-8, comma separated")] = "",
) -> str:
    ns, err = parse_names(names)
    if err:
        return err
    return "\n".join([intro(), f"🎉 뽑힌 사람: {SYSTEM_RNG.choice(ns)}! (참가자 {len(ns)}명 중)", RELAY_NOTE])


@mcp.tool(
    name="get_ladder_reference",
    description=(
        f"Explains how {SERVICE} works: how to enter names and results, the limits (2-8 people), fairness and what it does not handle. Use it when the user asks how to play. Keywords: 사다리 게임 사용법, 사다리 규칙."
    ),
    annotations=_annotations("Ladder game help"),
)
@_logged
def get_ladder_reference() -> str:
    return "\n".join([
        "■ 사다리 게임 안내",
        f"■ 참가자 {MIN_N}~{MAX_N}명, 이름은 10자 이내, 결과 문구는 15자 이내(쉼표로 구분).",
        "■ 결과 칸을 비우면 '꽝' 1개와 나머지 '통과(PASS)'로 구성돼요. 예: 꽝, 커피 사기, 통과",
        "■ 한 번에 보기: make_ladder / 한 명씩 공개: create_hidden_ladder → reveal_ladder / 한 명만 뽑기: pick_one_person",
        "■ 사다리와 결과는 서버가 무작위로 정하며 각 결과 칸이 나올 확률은 같아요. 이름과 결과 문구는 저장하지 않아요.",
        "■ 점심·커피·당번 정하기 같은 가벼운 용도예요. 돈이 걸린 내기나 금액은 받지 않아요."])


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
