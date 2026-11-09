"""허브 테스트 (python -m unittest test_hub -v): 경로 분배, lifespan 결합, 불러오기 실패 격리"""
import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import main


def child(name, events):
    async def mcp(request):
        return PlainTextResponse(f"{name}:{request.scope['path'].rsplit('/', 1)[-1]}")

    @asynccontextmanager
    async def lifespan(app):
        events.append(f"start-{name}")
        yield
        events.append(f"stop-{name}")
    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST"]), Route("/health", mcp)], lifespan=lifespan)


async def call(app, path, method="GET"):
    out = {"status": None, "body": b""}
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"host", b"x")], "scheme": "http", "server": ("x", 80), "client": ("c", 1), "root_path": ""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    await app(scope, receive, send)
    return out["status"], out["body"].decode()


class Hub(unittest.TestCase):
    def test_routing_and_health(self):
        ev = []
        hub = main.build_hub([("calc", child("calc", ev)), ("school", child("school", ev))])

        async def run():
            self.assertEqual(await call(hub, "/calc/mcp"), (200, "calc:mcp"))
            self.assertEqual(await call(hub, "/school/mcp", "POST"), (200, "school:mcp"))
            self.assertEqual(await call(hub, "/calc/health"), (200, "calc:health"))
            self.assertEqual((await call(hub, "/health"))[0], 200)
            self.assertEqual((await call(hub, "/health", "HEAD"))[0], 200)
            self.assertEqual((await call(hub, "/"))[1], "OK")
            self.assertEqual((await call(hub, "/nope/mcp"))[0], 404)
        asyncio.run(run())

    def test_lifespans_all_started(self):
        ev = []
        hub = main.build_hub([("a", child("a", ev)), ("b", child("b", ev))])

        async def run():
            async with hub.router.lifespan_context(hub):
                self.assertEqual(ev, ["start-a", "start-b"])
        asyncio.run(run())
        self.assertEqual(ev, ["start-a", "start-b", "stop-b", "stop-a"])

    def test_broken_service_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "services").mkdir()
            (Path(d) / "services" / "ok.py").write_text(
                "from starlette.applications import Starlette\napp = Starlette()\n", encoding="utf-8")
            (Path(d) / "services" / "bad.py").write_text("raise RuntimeError('secret-detail')\n", encoding="utf-8")
            (Path(d) / "services" / "noapp.py").write_text("x = 1\n", encoding="utf-8")
            orig = main.SERVICES
            main.SERVICES = [("ok", "ok.py"), ("bad", "bad.py"), ("noapp", "noapp.py"), ("missing", "missing.py")]
            try:
                kids = main.load_children(Path(d))
            finally:
                main.SERVICES = orig
        self.assertEqual([k for k, _ in kids], ["ok"])

    def test_service_table(self):
        ids = [s for s, _ in main.SERVICES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {"hanam", "calc", "dog", "baby", "event", "date", "school", "tour", "holiday"})
        for _, f in main.SERVICES:
            self.assertTrue((Path(main.__file__).parent / "services" / f).exists(), f)


if __name__ == "__main__":
    unittest.main()
