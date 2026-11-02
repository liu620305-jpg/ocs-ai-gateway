"""OCS AI 网关：兼容 AnswererWrapper 的多模型答题服务。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import config
from .cache import AnswerCache
from .llm import ProviderError, call_provider
from .parsing import build_messages, normalize_type, parse_answer, parse_options
from .store import ProviderStore

store = ProviderStore(config.PROVIDERS_PATH, seed=config.SEED)
cache = AnswerCache(config.CACHE_PATH, config.CACHE_TTL_HOURS)



class Stats:
    def __init__(self) -> None:
        self.total = 0
        self.cached = 0
        self.failovers = 0
        self.errors = 0
        self.started = time.time()
        self.per: dict[str, dict[str, Any]] = {}

    def _slot(self, pid: str) -> dict[str, Any]:
        return self.per.setdefault(
            pid, {"calls": 0, "fails": 0, "ms": 0, "last_ms": 0, "last_error": "", "last_used": 0.0}
        )

    def success(self, provider: dict[str, Any], elapsed_ms: int) -> None:
        slot = self._slot(provider["id"])
        slot["calls"] += 1
        slot["ms"] += elapsed_ms
        slot["last_ms"] = elapsed_ms
        slot["last_used"] = time.time()
        slot["last_error"] = ""

    def failure(self, provider: dict[str, Any], message: str) -> None:
        slot = self._slot(provider["id"])
        slot["calls"] += 1
        slot["fails"] += 1
        slot["last_error"] = message
        slot["last_used"] = time.time()
        self.failovers += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "cached": self.cached,
            "failovers": self.failovers,
            "errors": self.errors,
            "uptime": int(time.time() - self.started),
            "per": self.per,
        }


stats = Stats()


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def _provider_public(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": provider["id"],
        "name": provider["name"],
        "base_url": provider["base_url"],
        "model": provider["model"],
        "api_type": provider["api_type"],
        "temperature": provider["temperature"],
        "timeout": provider["timeout"],
        "enabled": provider["enabled"],
        "api_key_masked": _mask(provider["api_key"]),
        "has_key": bool(provider["api_key"]),
        "stat": stats.per.get(provider["id"], {}),
    }


async def require_token(request: Request) -> None:
    if not config.GATEWAY_TOKEN:
        return
    supplied = request.headers.get("X-Gateway-Token") or request.query_params.get("token")
    if supplied != config.GATEWAY_TOKEN:
        raise HTTPException(status_code=401, detail="管理口令不正确")


app = FastAPI(title="OCS AI 网关", version="1.0.0", docs_url="/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 基础页面

@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse("/admin")


@app.get("/health")
async def health() -> dict[str, Any]:
    enabled = len(store.enabled())
    return {
        "status": "ok" if enabled else "no_provider",
        "providers": len(store.all()),
        "enabled": enabled,
        "cache": cache.count(),
    }


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(config.ADMIN_HTML, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- 答题接口

def _fail(code: int, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message})


async def _resolve(title: str, qtype_raw: str | None, options_raw) -> JSONResponse:
    title = (title or "").strip()
    if not title:
        return _fail(40001, "missing title")
    if len(title) > config.MAX_TITLE_LENGTH:
        title = title[: config.MAX_TITLE_LENGTH]

    qtype = normalize_type(qtype_raw)
    if qtype is None:
        return _fail(40002, f"暂不支持该题型：{qtype_raw}")

    options = parse_options(options_raw)
    if len(options) > config.MAX_OPTIONS_COUNT:
        options = options[: config.MAX_OPTIONS_COUNT]
    if qtype in ("single", "multiple") and len(options) < 2:
        return _fail(40003, "options 至少需要两个选项")

    stats.total += 1

    key = cache.make_key(qtype, title, options)
    hit = cache.get(key)
    if hit:
        stats.cached += 1
        return JSONResponse(
            {
                "code": 0,
                "data": {
                    "answer": hit["answer"],
                    "question": title,
                    "ai": True,
                    "provider": hit["provider"],
                    "cached": True,
                    "elapsed_ms": 0,
                },
            }
        )

    providers = store.enabled()
    if not providers:
        stats.errors += 1
        return _fail(50002, "还没有启用任何 AI，请打开 /admin 添加一个")

    messages = build_messages(qtype, title, options)
    errors: list[str] = []

    for provider in providers:
        started = time.perf_counter()
        budget = min(float(provider["timeout"]), float(config.REQUEST_TIMEOUT_SECONDS))
        try:
            text = await asyncio.wait_for(call_provider(provider, messages), timeout=budget)
            elapsed = int((time.perf_counter() - started) * 1000)
            answer = parse_answer(qtype, text, options)
            if not answer:
                raise ProviderError(f"答案无法解析：{_clip(text)}")
        except asyncio.TimeoutError:
            elapsed = int((time.perf_counter() - started) * 1000)
            stats.failure(provider, f"超时（>{budget:.0f}s）")
            errors.append(f"{provider['name']}：超时")
            continue
        except ProviderError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            stats.failure(provider, exc.message)
            errors.append(f"{provider['name']}：{exc.message}")
            continue
        except Exception as exc:  # noqa: BLE001 - 任何意外都只跳过这一个 AI
            stats.failure(provider, f"内部错误: {exc}")
            errors.append(f"{provider['name']}：内部错误 {exc}")
            continue

        stats.success(provider, elapsed)
        cache.set(key, title, answer, provider["name"])
        return JSONResponse(
            {
                "code": 0,
                "data": {
                    "answer": answer,
                    "question": title,
                    "ai": True,
                    "provider": provider["name"],
                    "cached": False,
                    "elapsed_ms": elapsed,
                },
            }
        )

    stats.errors += 1
    detail = "；".join(errors[:4])
    if len(errors) > 4:
        detail += f"；等 {len(errors)} 个错误"
    return _fail(50001, f"全部 AI 调用失败：{detail}")


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


async def _check_query_token(request: Request) -> JSONResponse | None:
    if not config.GATEWAY_TOKEN:
        return None
    supplied = request.query_params.get("token") or request.headers.get("X-Gateway-Token")
    if supplied != config.GATEWAY_TOKEN:
        return _fail(40100, "token 不正确")
    return None


@app.get("/query")
async def query_get(request: Request) -> JSONResponse:
    denied = await _check_query_token(request)
    if denied:
        return denied
    params = request.query_params
    return await _resolve(
        params.get("title", ""),
        params.get("type"),
        params.get("options"),
    )


@app.post("/query")
async def query_post(request: Request) -> JSONResponse:
    denied = await _check_query_token(request)
    if denied:
        return denied

    payload: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                payload = body
        except (json.JSONDecodeError, ValueError):
            return _fail(40000, "请求体不是合法 JSON")
    else:
        form = await request.form()
        payload = dict(form)

    return await _resolve(
        str(payload.get("title", "")),
        str(payload.get("type", "single")),
        payload.get("options"),
    )


# ---------------------------------------------------------------- 管理接口

@app.get("/api/state", dependencies=[Depends(require_token)])
async def api_state() -> dict[str, Any]:
    return {
        "providers": [_provider_public(p) for p in store.all()],
        "stats": stats.snapshot(),
        "cache": {"count": cache.count(), "ttl_hours": config.CACHE_TTL_HOURS},
        "requires_token": bool(config.GATEWAY_TOKEN),
    }


@app.post("/api/providers", dependencies=[Depends(require_token)])
async def api_add_provider(request: Request) -> dict[str, Any]:
    body = await _json_body(request)
    error = _validate_provider(body, creating=True)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return _provider_public(store.add(body))


@app.put("/api/providers/{pid}", dependencies=[Depends(require_token)])
async def api_update_provider(pid: str, request: Request) -> dict[str, Any]:
    body = await _json_body(request)
    error = _validate_provider(body, creating=False)
    if error:
        raise HTTPException(status_code=400, detail=error)
    updated = store.update(pid, body)
    if not updated:
        raise HTTPException(status_code=404, detail="找不到该 AI")
    return _provider_public(updated)


@app.delete("/api/providers/{pid}", dependencies=[Depends(require_token)])
async def api_delete_provider(pid: str) -> dict[str, Any]:
    if not store.delete(pid):
        raise HTTPException(status_code=404, detail="找不到该 AI")
    return {"ok": True}


@app.post("/api/providers/{pid}/toggle", dependencies=[Depends(require_token)])
async def api_toggle_provider(pid: str, request: Request) -> dict[str, Any]:
    body = await _json_body(request, allow_empty=True)
    updated = store.toggle(pid, body.get("enabled"))
    if not updated:
        raise HTTPException(status_code=404, detail="找不到该 AI")
    return _provider_public(updated)


@app.post("/api/providers/{pid}/move", dependencies=[Depends(require_token)])
async def api_move_provider(pid: str, request: Request) -> dict[str, Any]:
    body = await _json_body(request)
    direction = 1 if int(body.get("direction", 0)) > 0 else -1
    if not store.move(pid, direction):
        return {"ok": False}
    return {"ok": True}


@app.post("/api/providers/{pid}/test", dependencies=[Depends(require_token)])
async def api_test_provider(pid: str) -> dict[str, Any]:
    provider = store.get(pid)
    if not provider:
        raise HTTPException(status_code=404, detail="找不到该 AI")

    sample_options = ["北京", "上海", "广州", "深圳"]
    messages = build_messages("single", "以下哪个城市是中国的首都？", sample_options)
    started = time.perf_counter()
    try:
        text = await asyncio.wait_for(
            call_provider(provider, messages),
            timeout=min(float(provider["timeout"]), 20) + 2,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "请求超时", "elapsed_ms": int((time.perf_counter() - started) * 1000)}
    except ProviderError as exc:
        return {
            "ok": False,
            "error": exc.message,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"内部错误: {exc}",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    elapsed = int((time.perf_counter() - started) * 1000)
    answer = parse_answer("single", text, sample_options)
    if not answer:
        return {
            "ok": False,
            "error": f"连通了，但返回内容无法解析：{_clip(text, 120)}",
            "raw": text[:400],
            "elapsed_ms": elapsed,
        }
    return {
        "ok": True,
        "answer": answer,
        "expected": "A",
        "raw": text[:200],
        "elapsed_ms": elapsed,
    }


@app.post("/api/cache/clear", dependencies=[Depends(require_token)])
async def api_clear_cache() -> dict[str, Any]:
    return {"ok": True, "cleared": cache.clear()}


async def _json_body(request: Request, allow_empty: bool = False) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        if allow_empty:
            return {}
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    return body


def _validate_provider(body: dict[str, Any], creating: bool) -> str | None:
    key = str(body.get("api_key") or "").strip()
    if key:
        try:
            key.encode("ascii")
        except UnicodeEncodeError:
            return "API Key 含有中文或全角字符，请检查是否粘贴错了"

    if creating:
        if not str(body.get("name") or "").strip():
            return "请填写名称"
        if not str(body.get("base_url") or "").strip():
            return "请填写 Base URL"
        if not str(body.get("api_key") or "").strip():
            return "请填写 API Key"
        if not str(body.get("model") or "").strip():
            return "请填写模型名称"
        return None

    for field, label in (("name", "名称"), ("base_url", "Base URL"), ("model", "模型名称")):
        if field in body and not str(body.get(field) or "").strip():
            return f"{label}不能为空"
    return None
