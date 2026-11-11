"""MisaTech MCP 허브 - 여러 PlayMCP 서비스를 Render 서비스 1개에서 경로별로 제공합니다.
무료 플랜은 워크스페이스당 월 750시간이라 서비스를 따로 상시 가동하면 한도를 넘기므로, 한 서버에 합칩니다.
엔드포인트: https://<허브주소>/<서비스id>/mcp  (예: /calc/mcp)
각 서비스는 services/ 아래 원래 코드를 그대로 쓰며, 인증키는 환경변수(KAKAO_REST_API_KEY, NEIS_KEY, TOUR_KEY, HOLIDAY_KEY)로만 받습니다.
"""
import importlib.util
import logging
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

# (경로 id, 파일 이름) — 경로 id가 곧 엔드포인트 경로입니다. 바꾸면 콘솔 주소를 다시 고쳐야 합니다.
SERVICES = [
    ("hanam", "hanam.py"),
    ("calc", "calc.py"),
    ("dog", "dog.py"),
    ("baby", "baby.py"),
    ("event", "event.py"),
    ("date", "date.py"),
    ("school", "school.py"),
    ("tour", "tour.py"),
    ("holiday", "holiday.py"),
    ("leave", "leave.py"),
]

log = logging.getLogger("hub")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def load_children(base_dir: Path):
    """[(id, ASGI app)] — 한 서비스가 불러오기에 실패해도 나머지는 계속 제공한다(오류 내용은 id만 기록)."""
    children = []
    for sid, fname in SERVICES:
        try:
            spec = importlib.util.spec_from_file_location(f"svc_{sid}", base_dir / "services" / fname)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"svc_{sid}"] = mod
            spec.loader.exec_module(mod)
            app = getattr(mod, "app", None)
            if app is None:
                raise RuntimeError("no app")
            children.append((sid, app))
            log.info("HUB loaded id=%s", sid)
        except Exception as e:   # 이름·형식만 기록
            log.info("HUB load_failed id=%s type=%s", sid, type(e).__name__)
    return children


def _lifespan_of(app):
    ls = getattr(app, "lifespan", None)
    return ls if ls is not None else app.router.lifespan_context


def build_hub(children):
    @asynccontextmanager
    async def lifespan(app):
        # 각 FastMCP 앱의 세션 관리자가 시작되어야 하므로 모든 자식 lifespan을 함께 연다
        async with AsyncExitStack() as stack:
            for _, child in children:
                await stack.enter_async_context(_lifespan_of(child)(child))
            yield

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    routes = [Route("/health", health, methods=["GET", "HEAD"]), Route("/", health, methods=["GET", "HEAD"])]
    routes += [Mount(f"/{sid}", app=child) for sid, child in children]
    return Starlette(routes=routes, lifespan=lifespan)


app = build_hub(load_children(Path(__file__).parent))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
