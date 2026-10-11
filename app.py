"""muse2api 入口：创建 FastAPI 应用、挂载各路由模块、启动后台任务。

模块分工：
  state.py        全局状态（配置/账号池/引擎/并发锁/鉴权）
  schemas.py      模型表与请求体
  prompts.py      prompt 拼装与工具调用解析
  runner.py       换号重试、续签、对话流与生成的运行入口
  keepalive.py    账号保活
  routes_*.py     OpenAI 兼容接口与管理接口
  wx_bridge.py    微信接入
"""
from __future__ import annotations

import asyncio
import secrets
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import routes_admin
import routes_chat
import routes_images
import wx_bridge
from config import CFG
from keepalive import _keepalive_loop
from state import _persist_env, engine, log, store

app = FastAPI(title="muse2api", version="1.5.4")

# 本服务用 Bearer Key 鉴权、不依赖 Cookie，放行所有来源不会带来越权风险；
# 反之限死来源会让浏览器端客户端（Open WebUI / LobeChat 等）被 CORS 拦住。
app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)

for _r in (routes_images.router, routes_chat.router, routes_admin.router, wx_bridge.router):
    app.include_router(_r)


# ------------------------- OpenAI 风格的错误响应 -------------------------
# 各类智能体基本都按 OpenAI 的 {"error": {"message": ...}} 取错误信息；
# FastAPI 默认返回的是 {"detail": ...}，客户端会读不到原因、只显示"未知错误"。
# 所以 /v1/* 统一转成 OpenAI 格式，管理接口保持原样（前端依赖 detail）。
def _err_type(status: int) -> str:
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "message": "请求参数校验失败：" + str(exc.errors())[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.on_event("startup")
async def _startup():
    for task in list(store.tasks.values()):
        if task.get("kind") in ("image", "video") and task.get("status") in ("queued", "processing"):
            store.update_task(task["id"], status="failed", error="服务重启中断了任务，请重新提交")
    if not CFG.api_key:
        new_key = "m2a_" + secrets.token_hex(24)
        CFG.api_key = new_key
        _persist_env("MUSE2API_KEY", new_key)
        log.info("🔑 未检测到 MUSE2API_KEY，已自动生成初始密钥: %s", new_key)
    asyncio.create_task(_keepalive_loop())
    if CFG.weixin_enabled:
        wx_bridge.weixin.start()
        if wx_bridge.weixin.state.get("bot_token"):
            threading.Thread(target=wx_bridge.warm_wx, daemon=True, name="wx-warm").start()


@app.on_event("shutdown")
def _shutdown():
    wx_bridge.weixin.stop()
    engine.stop()
