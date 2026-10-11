"""微信 ClawBot 与对话/生图的桥接，及其管理接口。"""
from __future__ import annotations

import asyncio
import io
import os
import re
import threading
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException

from prompts import build_chat_prompt, build_image_prompt, build_video_prompt
from runner import _run_generation, safe_chat_stream
from schemas import ChatMessage, ImageRequest, VideoRequest
from state import CFG, auth, engine, store
from weixin import WeixinBot

router = APIRouter()

# ------------------------- 微信 ClawBot 接入 -------------------------
WX_HELP = ("直接发文字即可对话。\n/画 描述：生成图片\n/视频 描述：生成视频（需要等一两分钟）\n"
           "/重置：开始新话题（若接入的是 muse 主要聊天，其中的历史仍保留）\n/帮助：显示本说明")
_wx_history: dict[str, list[ChatMessage]] = {}
_wx_accounts: dict[str, str] = {}   # 微信用户 -> 固定使用的账号


_wx_options: dict[str, list[str]] = {}
_wx_locks: dict[str, threading.Lock] = {}
_wx_prompts: dict[str, dict] = {}   # 等待用户答复的授权弹窗：{event, buttons, answer}
WX_PROMPT_TIMEOUT = 180


def wx_ask_permission(user: str, text: str, buttons: list[str], push=None,
                      timeout: float = WX_PROMPT_TIMEOUT) -> int | None:
    """muse.ai 弹出授权/确认窗：把正文和编号按钮发到微信，阻塞等用户回复，返回按钮下标。
    超时返回 None（引擎会点「拒绝」类按钮）。"""
    st = {"event": threading.Event(), "buttons": buttons, "answer": None}
    _wx_prompts[user] = st
    lines = [f"{i}. {b}" for i, b in enumerate(buttons, 1)]
    msg = ("muse.ai 需要你确认：\n" + text.strip()[:500] + "\n\n" + "\n".join(lines)
           + f"\n\n回复数字选择；{int(timeout)} 秒内没回复将自动拒绝。")
    try:
        (push or weixin.push_text)(user, msg)
        st["event"].wait(timeout)
    finally:
        _wx_prompts.pop(user, None)
    return st["answer"]


def wx_answer_prompt(user: str, text: str) -> str | None:
    """若该用户有待答复的弹窗，用这条消息作答并返回提示语；没有则返回 None。"""
    st = _wx_prompts.get(user)
    if not st:
        return None
    t = text.strip()
    idx = None
    if t.isdigit() and 1 <= int(t) <= len(st["buttons"]):
        idx = int(t) - 1
    else:
        for i, b in enumerate(st["buttons"]):
            if t and (t == b or t in b):
                idx = i
                break
    if idx is None:
        return f"请回复 1 到 {len(st['buttons'])} 之间的数字来选择。"
    st["answer"] = idx
    st["event"].set()
    return f"已选择：{st['buttons'][idx]}"


def wx_format_options(text: str, opts: list[str]) -> str:
    """把 muse 回复里的选项按钮渲染成编号列表（按钮文字在网页里会被拼成一行）。"""
    idx = text.rfind(opts[0])
    if idx > 0 and all(o in text[idx:] for o in opts):
        text = text[:idx].rstrip()
    lines = [f"{i}. {o}" for i, o in enumerate(opts, 1)]
    return (text + "\n\n" if text else "") + "\n".join(lines) + "\n\n回复数字选择，或直接回复文字。"


def wx_resolve_option(text: str, opts: list[str]) -> str:
    """用户回数字 → 对应选项原文；其他输入原样发送。"""
    t = text.strip()
    if t.isdigit() and 1 <= int(t) <= len(opts):
        return opts[int(t) - 1]
    return text


def _wx_safe_name(name: str) -> str:
    name = os.path.basename(name or "").strip().replace("\\", "_") or "file"
    return re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name)[:80] or "file"


def wx_turn_replies(turn: list[dict], opts: list[str]) -> list:
    """把一轮回复里的多个气泡/文件按顺序转成微信消息；文件先落盘，取不回来就给文字说明。"""
    replies: list = []
    for it in turn:
        if it.get("k") == "t":
            replies.append(("text", it["v"]))
        elif it.get("k") == "m":
            kind = "video" if it.get("kind") == "video" else "image"
            data = it.get("data")
            if data:
                ext = ".mp4" if kind == "video" else (".jpg" if "jpeg" in (it.get("mime") or "") else ".png")
                d = os.path.join(CFG.download_dir, "wx")
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, f"{uuid.uuid4().hex[:8]}{ext}")
                with open(path, "wb") as f:
                    f.write(data)
                replies.append(("media", path, kind))
            else:
                replies.append(("text", f"（{'视频' if kind == 'video' else '图片'}没能取回，请到 muse.ai 页面查看）"))
        elif it.get("k") == "f":
            name = _wx_safe_name(it.get("name", ""))
            data = it.get("data")
            if data:
                d = os.path.join(CFG.download_dir, "wx")
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, f"{uuid.uuid4().hex[:8]}_{name}")
                with open(path, "wb") as f:
                    f.write(data)
                replies.append(("media", path, "file"))
            else:
                replies.append(("text", f"（文件 {name} 没能取回，请到 muse.ai 页面下载）"))
    if opts:
        for k in range(len(replies) - 1, -1, -1):
            if replies[k][0] == "text":
                replies[k] = ("text", wx_format_options(replies[k][1], opts))
                break
        else:
            replies.append(("text", wx_format_options("", opts)))
    return replies


def _wx_chat(user: str, text: str) -> list:
    """微信对话走 muse.ai 的聊天：只发新消息，上下文由 muse 自己记。
    一轮回复的多个气泡、文件、代码块都按顺序转发；取不到分段信息时退回合并文本。"""
    text = wx_resolve_option(text, _wx_options.pop(user, []))
    hist = _wx_history.setdefault(user, [])
    hist.append(ChatMessage(role="user", content=text))
    del hist[:-12]
    fallback = build_chat_prompt(hist)
    # 同一微信用户固定用同一个账号：对话会话是按账号记住的，轮换账号就会每条消息新开一个旁聊
    acc = store.get_account(_wx_accounts.get(user) or "")
    if not acc or not acc.get("enabled", True) or not acc.get("cookies"):
        acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
        if acc:
            _wx_accounts[user] = acc["id"]
    if not acc:
        raise RuntimeError("没有可用账号，请先在管理页面导入 cookie")
    opts: list[str] = []
    turn: list[dict] = []
    out = "".join(safe_chat_stream(acc["cookies"], text, acc.get("cookies_exp"),
                                   CFG.chat_timeout, account_id=acc["id"],
                                   main_chat=True, fallback_prompt=fallback,
                                   on_options=opts.extend, on_turn=turn.extend,
                                   on_prompt=lambda t, b: wx_ask_permission(user, t, b))).strip()
    if opts:
        _wx_options[user] = list(opts)
    replies = wx_turn_replies(turn, opts) if turn else []
    texts = [it["v"] for it in turn if it.get("k") == "t"] or ([out] if out else [])
    if texts:
        hist.append(ChatMessage(role="assistant", content="\n\n".join(texts)))
        del hist[:-12]
    if replies:
        return replies
    if out:
        return [("text", wx_format_options(out, opts) if opts else out)]
    return [("text", "（没有收到回复，请稍后重试）")]


def _wx_handle(user: str, text: str) -> list:
    """微信私聊消息 → 回复列表：("text", s) 或 ("media", path, kind)。"""
    answered = wx_answer_prompt(user, text)
    if answered is not None:       # 回答授权弹窗不能被同一用户正在跑的任务挡住
        return [("text", answered)]
    with _wx_locks.setdefault(user, threading.Lock()):   # 同一用户的消息按顺序处理
        return _wx_handle_locked(user, text)


def _wx_handle_locked(user: str, text: str) -> list:
    m = re.match(r"^/(画|图|image|img|视频|video)\s*(.*)$", text, re.S | re.I)
    if m:
        kind = "video" if m.group(1).lower() in ("视频", "video") else "image"
        prompt = m.group(2).strip()
        if not prompt:
            return [("text", "请在命令后面写上描述，例如：/画 一只戴宇航头盔的柴犬")]
        if kind == "image":
            res, _ = _run_generation(build_image_prompt(ImageRequest(prompt=prompt)),
                                     "image", CFG.image_timeout)
        else:
            res, _ = _run_generation(build_video_prompt(VideoRequest(prompt=prompt)),
                                     "video", CFG.video_timeout)
        path = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        return [("media", path, "video" if res.get("kind") == "video" else "image")]
    if text.strip() in ("/重置", "/reset", "/new"):
        _wx_history.pop(user, None)
        _wx_options.pop(user, None)
        _wx_accounts.pop(user, None)
        engine.forget_chat_binding()
        return [("text", "已清空对话上下文。")]
    if text.strip() in ("/帮助", "/help", "/?"):
        return [("text", WX_HELP)]
    return _wx_chat(user, text)


weixin = WeixinBot(CFG.weixin_file, _wx_handle, allow_env=CFG.weixin_allow_from)


def _qr_svg(content: str) -> str:
    """把扫码内容渲染成内联 SVG（segno 缺失时返回空串，页面回退为显示链接）。"""
    try:
        import segno
    except ImportError:
        return ""
    buf = io.BytesIO()
    segno.make(content, error="m").save(buf, kind="svg", scale=1, border=2, xmldecl=False,
                                        omitsize=True, svgclass=None, lineclass=None)
    return buf.getvalue().decode()


@router.get("/admin/weixin")
def weixin_status(_=Depends(auth)):
    return {"enabled": CFG.weixin_enabled, **weixin.info()}


@router.post("/admin/weixin/login")
async def weixin_login(_=Depends(auth)):
    if not CFG.weixin_enabled:
        raise HTTPException(400, "微信接入已被 MUSE2API_WEIXIN_ENABLED=0 关闭")
    try:
        info = await asyncio.to_thread(weixin.start_login)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"获取二维码失败: {exc}") from exc
    return {**info, "svg": _qr_svg(info.get("content") or "")}


@router.get("/admin/weixin/login/status")
async def weixin_login_status(_=Depends(auth)):
    try:
        await asyncio.to_thread(weixin.poll_login)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"查询扫码状态失败: {exc}") from exc
    return weixin.info()


@router.post("/admin/weixin/logout")
def weixin_logout(_=Depends(auth)):
    weixin.disconnect()
    return weixin.info()


@router.post("/admin/weixin/allow")
def weixin_allow(payload: dict = Body(default={}), _=Depends(auth)):
    users = payload.get("users") or []
    if not isinstance(users, list):
        raise HTTPException(400, "users 需为数组")
    weixin.set_allow([str(u) for u in users])
    return weixin.info()


