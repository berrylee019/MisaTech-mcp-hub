"""반려견 체형·수명 체크 - Dog Body Check MCP 서버 (PlayMCP 등록용 설계 초안)

특징
- 외부 API 호출 없음(순수 계산/참고표) → 응답이 빠르고 안정적, 인증/개인정보/유료 요소 없음
- 사진·LLM 호출 없음 (사용자 프롬프트를 다른 LLM에 전달하지 않음)

데이터 출처(응답에도 명시)
- 체형점수(BCS) 9점 척도: Laflamme(1997), WSAVA Body Condition Score chart
- 생애단계 정의: AAHA Canine Life Stage Guidelines (2019)
- 사람 나이 환산: Wang et al., Cell Systems (2020), 래브라도 리트리버 기반 공식
- 식이 제한과 수명: Kealy et al., JAVMA (2002), 래브라도 48마리 14년 연구
- (선택) 노화 속도 참고 지수: 운영 서비스 자체 산출식. 의학적 근거 없음 → 기본 비활성

환경변수: ENABLE_PACE_INDEX=1 이면 참고 지수 툴을 등록 (기본 0)
"""
import math
import os
from typing import Annotated, Optional

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

SERVICE = "반려견 체형·수명 체크 - Dog Body Check"   # 콘솔 등록명과 글자 하나까지 동일해야 함(심사 DESCRIPTION_POLICY)

mcp = FastMCP(
    "반려견 체형·수명 체크 - Dog Body Check",
    instructions=(
        "반려견의 체형점수(BCS), 생애단계, 사람 나이 환산을 참고용으로 계산·안내하는 도구입니다. "
        "수의학적 진단이 아니며, 응답에 없는 의학적 판단은 추측하지 말고 수의사 상담을 안내하세요."
    ),
)


def _annotations(title: str) -> dict:
    # PlayMCP 필수: 5개 속성 모두 지정. 외부 호출이 없으므로 openWorldHint=False
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


# ------------------------------------------------------------ 참고 데이터
# 출처: WSAVA Body Condition Score chart (개). 표현은 요약·재서술.
BCS_TABLE = {
    1: ("저체중", "갈비뼈·허리뼈·골반뼈가 멀리서도 보이고 체지방이 거의 없음. 근육 손실이 뚜렷함."),
    2: ("저체중", "갈비뼈·허리뼈·골반뼈가 쉽게 보이고 만져지는 지방이 없음. 근육 손실은 약간."),
    3: ("저체중", "갈비뼈가 쉽게 만져지고 보일 수 있음. 허리와 복부 올림이 뚜렷하고 골반뼈가 두드러지기 시작."),
    4: ("약간 마름", "갈비뼈가 쉽게 만져지고 지방이 거의 없음. 위에서 보면 허리가 뚜렷하고 복부 올림이 분명."),
    5: ("이상 체중", "갈비뼈가 과한 지방 없이 만져짐. 위에서 보면 갈비뼈 뒤로 허리가 보이고 옆에서 보면 복부가 올라가 있음."),
    6: ("약간 과체중", "갈비뼈는 만져지나 지방이 약간 많음. 위에서 보면 허리가 보이나 두드러지지 않고 복부 올림은 있음."),
    7: ("과체중", "지방 때문에 갈비뼈를 만지기 어려움. 허리뼈·꼬리 뿌리에 지방이 뚜렷하고 허리가 거의 없거나 희미함."),
    8: ("비만", "갈비뼈가 만져지지 않거나 강하게 눌러야 만져짐. 허리와 복부 올림이 없고 복부가 부풀어 보일 수 있음."),
    9: ("심한 비만", "가슴·척추·꼬리 뿌리에 지방이 매우 많고 목과 사지에도 쌓임. 허리와 복부 올림이 없고 복부 팽만이 뚜렷함."),
}

DISCLAIMER = "참고용 정보이며 수의학적 진단이 아닙니다. 건강 판단은 수의사와 상담하세요."


def _valid_bcs(bcs) -> Optional[int]:
    try:
        v = int(bcs)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 9 else None


# ------------------------------------------------------------ Tool 1
@mcp.tool(
    name="get_body_condition_guide",
    description=(
        f"Returns the 9-point body condition score (BCS) reference for dogs from {SERVICE}: how to check ribs, waist "
        "and abdominal tuck, and what each score from 1 (underweight) to 9 (severely obese) looks like. "
        "Use it to help an owner choose a BCS before calling assess_body_condition. "
        "Source: WSAVA Body Condition Score chart, Laflamme (1997). Reference information, not a diagnosis."
    ),
    annotations=_annotations("Dog body condition guide"),
)
def get_body_condition_guide() -> str:
    lines = ["[체형점수(BCS) 9점 척도 안내 - 개]",
             "확인 방법: 갈비뼈를 손으로 만져보고, 위에서 허리선이, 옆에서 복부 올림이 보이는지 확인합니다.", ""]
    for s in range(1, 10):
        cat, desc = BCS_TABLE[s]
        lines.append(f"- {s}점 {cat}: {desc}")
    lines += ["", "출처: WSAVA Body Condition Score chart, Laflamme (1997).", DISCLAIMER]
    return "\n".join(lines)


# ------------------------------------------------------------ Tool 2
@mcp.tool(
    name="assess_body_condition",
    description=(
        f"Interprets a dog's body condition score (BCS, 1-9) using {SERVICE}: returns the category (underweight, "
        "ideal, overweight, obese), the typical physical signs for that score, and a general next-step note. "
        "For overweight scores it adds a cited research finding on calorie restriction and lifespan in Labrador "
        "retrievers (Kealy et al., 2002). Requires the BCS number; use get_body_condition_guide to help choose it. "
        "Reference information, not a diagnosis."
    ),
    annotations=_annotations("Assess dog body condition"),
)
def assess_body_condition(
    bcs: Annotated[int, Field(description="Body condition score from 1 (very thin) to 9 (severely obese)")],
    breed: Annotated[str, Field(description="Optional breed name for display only, e.g. '말티즈'")] = "",
) -> str:
    v = _valid_bcs(bcs)
    if v is None:
        return "BCS는 1~9 사이의 정수여야 합니다. 기준이 필요하면 get_body_condition_guide를 먼저 호출하세요."
    cat, desc = BCS_TABLE[v]
    head = f"[체형 평가] {breed.strip() + ' ' if breed and breed.strip() else ''}BCS {v}/9 - {cat}"
    lines = [head, f"- 일반적인 특징: {desc}"]
    if v <= 3:
        lines.append("- 안내: 마른 정도가 큰 편입니다. 식사량·질병 가능성 확인을 위해 수의사 상담을 권합니다.")
    elif v == 4:
        lines.append("- 안내: 이상 체중에 가깝지만 약간 마른 쪽입니다. 체중 변화를 정기적으로 기록해 보세요.")
    elif v == 5:
        lines.append("- 안내: WSAVA 차트에서 이상 체중으로 표시된 점수입니다. 현재 관리 방식을 유지하며 정기 점검하세요.")
    elif v == 6:
        lines.append("- 안내: 약간 과체중입니다. 급여량·간식·운동량을 점검하고 수의사와 목표 체중을 상의해 보세요.")
    else:
        lines.append("- 안내: 과체중 이상입니다. 수의사와 함께 단계적인 체중 관리 계획을 세우는 것을 권합니다.")
    if v >= 6:
        lines.append("- 참고 연구: 래브라도 리트리버 48마리를 14년간 추적한 연구에서, 칼로리를 25% 줄여 급여한 군의 "
                     "중앙 수명이 13.0년, 대조군이 11.2년이었습니다(Kealy et al., JAVMA, 2002). "
                     "특정 견종 대상 결과이며 개체별 수명을 예측하지는 않습니다.")
    lines += ["출처: WSAVA Body Condition Score chart, Laflamme (1997).", DISCLAIMER]
    return "\n".join(lines)


# ------------------------------------------------------------ Tool 3
@mcp.tool(
    name="get_dog_life_stage",
    description=(
        f"Classifies a dog's life stage (puppy, young adult, mature adult, senior) from age using {SERVICE}, "
        "following the AAHA Canine Life Stage Guidelines (2019). Puppy ends around 6-9 months and most dogs reach "
        "young-adult maturity at 3-4 years. AAHA defines senior as the last 25% of the estimated lifespan, so "
        "provide expected_lifespan_years (breed-based) to get the senior boundary. Reference information only."
    ),
    annotations=_annotations("Dog life stage"),
)
def get_dog_life_stage(
    age_years: Annotated[float, Field(description="Dog's age in years, e.g. 0.5, 7")],
    expected_lifespan_years: Annotated[float, Field(description="Optional expected lifespan in years for the breed, e.g. 12. 0 means unknown")] = 0,
) -> str:
    try:
        age = float(age_years)
        life = float(expected_lifespan_years or 0)
    except (TypeError, ValueError):
        return "나이와 기대수명은 숫자(년 단위)로 입력해주세요."
    if not (0 <= age <= 30):
        return "나이는 0~30년 범위로 입력해주세요."
    if life and not (5 <= life <= 25):
        return "기대수명은 5~25년 범위로 입력하거나, 모르면 0으로 두세요."

    lines = [f"[생애단계] 나이 {age:g}세"]
    if age < 0.5:
        stage = "Puppy(퍼피)"
    elif age < 0.75:
        stage = "Puppy → Young adult 전환 구간 (퍼피는 약 6~9개월까지, 견종·크기에 따라 다름)"
    elif age < 3:
        stage = "Young adult(젊은 성견)"
    elif age < 4:
        stage = "Young adult 후반 (대부분 3~4세에 신체적·사회적 성숙에 도달)"
    elif life:
        senior_from = round(life * 0.75, 1)
        stage = ("Senior(노견)" if age >= senior_from else "Mature adult(성숙한 성견)")
        lines.append(f"- 기준: 기대수명 {life:g}년의 마지막 25% → 약 {senior_from:g}세부터 Senior")
    else:
        stage = "Mature adult(성숙한 성견) 이상 - 시니어 시작 시점은 기대수명 입력 필요"
    lines.insert(1, f"- 단계: {stage}")
    lines += ["- 참고: AAHA는 나이와 개체 특성을 함께 보고 단계를 정하라고 안내하며, 단계 구분은 임상적 판단이 필요합니다.",
              "출처: AAHA Canine Life Stage Guidelines (2019).", DISCLAIMER]
    return "\n".join(lines)


# ------------------------------------------------------------ Tool 4
@mcp.tool(
    name="convert_dog_age_to_human",
    description=(
        f"Converts a dog's age to an approximate human-equivalent age using {SERVICE}, with the 2020 epigenetic "
        "formula: human age = 16 x ln(dog age) + 31 (Wang et al., Cell Systems, 2020). The formula is based mainly "
        "on Labrador retrievers, so other breeds may differ. It replaces the old 'multiply by 7' rule. "
        "Reference information only."
    ),
    annotations=_annotations("Dog age to human age"),
)
def convert_dog_age_to_human(
    age_years: Annotated[float, Field(description="Dog's age in years, between 0.25 and 20")],
) -> str:
    try:
        age = float(age_years)
    except (TypeError, ValueError):
        return "나이는 숫자(년 단위)로 입력해주세요."
    if not (0.25 <= age <= 20):
        return "이 공식은 0.25~20세 범위에서 사용합니다."
    human = 16 * math.log(age) + 31
    return "\n".join([
        f"[사람 나이 환산] 개 나이 {age:g}세 ≈ 사람 나이 약 {human:.0f}세",
        "- 공식: 사람 나이 = 16 × ln(개 나이) + 31",
        "- 한계: 연구진이 래브라도 리트리버 위주 데이터라고 밝혔으며, 견종에 따라 다를 수 있습니다.",
        "출처: Wang et al., Cell Systems (2020), 'Quantitative Translation of Dog-to-Human Aging by Conserved "
        "Remodeling of the DNA Methylome'.",
        DISCLAIMER,
    ])


# ------------------------------------------------------------ Tool 5 (선택, 기본 비활성)
def calculate_pace_index(bcs: int, breed: str) -> float:
    """운영 서비스(app.py)의 자체 산출식 그대로"""
    pace = 1.0 + (abs(5 - bcs) * 0.15)
    if breed == "리트리버":
        pace *= 1.15
    return round(pace, 2)


if os.environ.get("ENABLE_PACE_INDEX") == "1":
    @mcp.tool(
        name="estimate_aging_pace_index",
        description=(
            f"Returns a service-defined reference index of body-condition-related aging pace from {SERVICE}: "
            "1.0 + 0.15 x |5 - BCS|, multiplied by 1.15 for retrievers. This is an in-house heuristic for "
            "comparison only and is NOT based on clinical research; it treats thin and heavy dogs the same way. "
            "Not a diagnosis."
        ),
        annotations=_annotations("Aging pace reference index"),
    )
    def estimate_aging_pace_index(
        bcs: Annotated[int, Field(description="Body condition score from 1 to 9")],
        breed: Annotated[str, Field(description="Breed name; '리트리버' applies the retriever factor")] = "",
    ) -> str:
        v = _valid_bcs(bcs)
        if v is None:
            return "BCS는 1~9 사이의 정수여야 합니다."
        pace = calculate_pace_index(v, (breed or "").strip())
        return "\n".join([
            f"[노화 속도 참고 지수] BCS {v}/9 → {pace}",
            "- 산식: 1.0 + 0.15 × |5 − BCS|, 리트리버는 ×1.15",
            "- 주의: 서비스 자체 산출 지수로 임상 연구에 근거하지 않으며, 마른 경우와 과체중 경우를 같은 방식으로 계산합니다. "
            "실제 노화 속도나 수명을 예측하지 않습니다.",
            "출처: Dog Body Check 자체 산출식.",
            DISCLAIMER,
        ])


# ------------------------------------------------------------ 서버
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


try:
    app = mcp.http_app(path="/mcp", stateless_http=True)  # PlayMCP 권장: Stateless
except TypeError:
    app = mcp.http_app(path="/mcp")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
