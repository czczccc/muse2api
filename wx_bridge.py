"""微信 ClawBot 与对话/生图的桥接，及其管理接口。"""
from __future__ import annotations

import asyncio
import io
import os
import re
import threading
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException

import direct
from prompts import build_chat_prompt, build_image_prompt, build_video_prompt
from runner import _run_generation, safe_chat_stream
from schemas import ChatMessage, ImageRequest, VideoRequest
from state import CFG, auth, engine, log, store
from weixin import WeixinBot

router = APIRouter()

# ------------------------- 微信 ClawBot 接入 -------------------------
WX_HELP = ("直接发文字即可对话。\n/画 描述：生成图片\n/视频 描述：生成视频（需要等一两分钟）\n"
           "/重置：开始新话题（若接入的是 muse 主要聊天，其中的历史仍保留）\n/帮助：显示本说明")
_wx_history: dict[str, list[ChatMessage]] = {}
_wx_user_bot: dict[str, WeixinBot] = {}   # 微信用户 -> 接收他消息的 bot（授权弹窗等主动推送用）
_bots: dict[str, WeixinBot] = {}          # muse 账号 -> 绑定在它上面的微信 bot
_bots_lock = threading.Lock()


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
        (push or _wx_push)(user, msg)
        st["event"].wait(timeout)
    finally:
        _wx_prompts.pop(user, None)
    return st["answer"]


def _wx_push(user: str, text: str):
    bot = _wx_user_bot.get(user)
    if bot is None:
        raise RuntimeError("找不到该微信用户所属的 bot")
    bot.push_text(user, text)


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


def _wx_chat(user: str, text: str, emit=None, acc_id: str | None = None) -> list:
    """微信对话走 muse.ai 的聊天：只发新消息，上下文由 muse 自己记。
    一轮回复的多个气泡、文件、代码块都按顺序转发；取不到分段信息时退回合并文本。"""
    text = wx_resolve_option(text, _wx_options.pop(user, []))
    hist = _wx_history.setdefault(user, [])
    hist.append(ChatMessage(role="user", content=text))
    del hist[:-12]
    fallback = build_chat_prompt(hist)
    # 每个 bot 绑定一个 muse 账号：同一微信用户始终用它，对话会话按账号记住
    if acc_id:
        acc = store.get_account(acc_id)
        if not acc or not acc.get("enabled", True) or not acc.get("cookies"):
            raise RuntimeError("这个微信绑定的 muse 账号不可用（已删除、被停用或 cookie 为空），请在管理页检查")
    else:
        acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
        if not acc:
            raise RuntimeError("没有可用账号，请先在管理页面导入 cookie")
    if direct.enabled():
        # 浏览器对话方案已停用：只走直连，失败直接报错
        return _wx_chat_direct(user, text, emit, acc, hist)
    opts: list[str] = []
    turn: list[dict] = []
    early: list[dict] = []      # 生成过程中已经先发出去的气泡/附件

    def on_partial(items):
        rs = wx_turn_replies(items, [])
        if rs and emit(rs) is False:
            return False        # 条数预算不够了，留到结束时合并发送
        early.extend(items)
        return True

    kwargs = {"on_partial": on_partial} if emit is not None else {}
    out = "".join(safe_chat_stream(acc["cookies"], text, acc.get("cookies_exp"),
                                   CFG.chat_timeout, account_id=acc["id"],
                                   main_chat=True, fallback_prompt=fallback,
                                   on_options=opts.extend, on_turn=turn.extend,
                                   on_prompt=lambda t, b: wx_ask_permission(user, t, b),
                                   **kwargs)).strip()
    if opts:
        _wx_options[user] = list(opts)
    replies = wx_turn_replies(turn, opts) if turn else []
    texts = [it["v"] for it in early + turn if it.get("k") == "t"] or ([out] if out else [])
    if texts:
        hist.append(ChatMessage(role="assistant", content="\n\n".join(texts)))
        del hist[:-12]
    if replies:
        return replies
    if early:        # 内容都已经先发过了，结束时没有新增内容；只补上选项
        return [("text", wx_format_options("", opts))] if opts else []
    if out:
        return [("text", wx_format_options(out, opts) if opts else out)]
    return [("text", "（没有收到回复，请稍后重试）")]


def _wx_chat_direct(user: str, text: str, emit, acc: dict, hist: list) -> list:
    """直连网关：每条助手回复（文字/图片/文件）完成就先发给微信，不开浏览器。"""
    early: list = []
    rest: list = []
    budget_ok = [emit is not None]

    def to_reply(it):
        if it[0] == "text":
            return ("text", it[1])
        _, data, kind, name = it
        if kind == "image" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            try:   # 微信不一定收 webp，转成 png
                from PIL import Image
                buf = io.BytesIO()
                Image.open(io.BytesIO(data)).save(buf, "PNG")
                data, name = buf.getvalue(), os.path.splitext(name or "image")[0] + ".png"
            except Exception as exc:  # noqa: BLE001
                log.warning("webp 转 png 失败，按原图发送: %s", exc)
        ext = {"image": ".png", "video": ".mp4"}.get(kind, "")
        d = os.path.join(CFG.download_dir, "wx")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{uuid.uuid4().hex[:8]}_{_wx_safe_name(name) if name else 'file' + ext}")
        if kind != "file" and not os.path.splitext(path)[1]:
            path += ext
        with open(path, "wb") as f:
            f.write(data)
        return ("media", path, kind)

    def on_message(it):
        r = to_reply(it)
        if r[0] == "text" and not r[1]:
            return
        if budget_ok[0] and emit([r]) is not False:
            early.append(r)
        else:
            budget_ok[0] = False          # 预算用完后的内容留到结束时合并发送
            rest.append(r)

    direct.get_client(acc["id"]).turn(acc["cookies"], text, CFG.chat_timeout, on_message,
                                      on_prompt=lambda t, b: wx_ask_permission(user, t, b))
    texts = [r[1] for r in early + rest if r[0] == "text"]
    if texts:
        hist.append(ChatMessage(role="assistant", content="\n\n".join(texts)))
        del hist[:-12]
    if rest:
        return rest
    return [] if early else [("text", "（没有收到回复，请稍后重试）")]


def _wx_handle(user: str, text: str, emit=None, acc_id: str | None = None) -> list:
    """微信私聊消息 → 回复列表：("text", s) 或 ("media", path, kind)。"""
    answered = wx_answer_prompt(user, text)
    if answered is not None:       # 回答授权弹窗不能被同一用户正在跑的任务挡住
        return [("text", answered)]
    with _wx_locks.setdefault(user, threading.Lock()):   # 同一用户的消息按顺序处理
        return _wx_handle_locked(user, text, emit, acc_id=acc_id)


def _wx_handle_locked(user: str, text: str, emit=None, acc_id: str | None = None) -> list:
    m = re.match(r"^/(画|图|image|img|视频|video)\s*(.*)$", text, re.S | re.I)
    if m:
        kind = "video" if m.group(1).lower() in ("视频", "video") else "image"
        prompt = m.group(2).strip()
        if not prompt:
            return [("text", "请在命令后面写上描述，例如：/画 一只戴宇航头盔的柴犬")]
        if kind == "image":
            res, _ = _run_generation(build_image_prompt(ImageRequest(prompt=prompt)),
                                     "image", CFG.image_timeout, account_id=acc_id)
        else:
            res, _ = _run_generation(build_video_prompt(VideoRequest(prompt=prompt)),
                                     "video", CFG.video_timeout, account_id=acc_id)
        path = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        return [("media", path, "video" if res.get("kind") == "video" else "image")]
    if text.strip() in ("/重置", "/reset", "/new"):
        _wx_history.pop(user, None)
        _wx_options.pop(user, None)
        engine.forget_chat_binding(acc_id)
        return [("text", "已清空对话上下文。")]
    if text.strip() in ("/帮助", "/help", "/?"):
        return [("text", WX_HELP)]
    return _wx_chat(user, text, emit, acc_id=acc_id)


def _state_path(acc_id: str) -> str:
    return os.path.join(CFG.data_dir, "weixin_" + re.sub(r"[^\w\-]", "_", acc_id) + ".json")


def _make_bot(acc_id: str) -> WeixinBot:
    def handler(user, text, emit=None):
        _wx_user_bot[user] = bot
        return _wx_handle(user, text, emit, acc_id=acc_id)

    bot = WeixinBot(_state_path(acc_id), handler, allow_env=CFG.weixin_allow_from)
    return bot


def get_bot(acc_id: str) -> WeixinBot:
    if not store.get_account(acc_id):
        raise HTTPException(404, "账号不存在")
    with _bots_lock:
        if acc_id not in _bots:
            _bots[acc_id] = _make_bot(acc_id)
        return _bots[acc_id]


def _migrate_legacy():
    """旧版只有一个 weixin.json（单 bot）：归到第一个可用账号名下。"""
    old = CFG.weixin_file
    if not os.path.exists(old):
        return
    first = next((a for a in store.accounts if a.get("enabled", True)), None) or next(iter(store.accounts), None)
    if first and not os.path.exists(_state_path(first["id"])):
        os.replace(old, _state_path(first["id"]))
        log.info("旧的微信连接已归到账号 %s 名下", first.get("label") or first["id"])


def start_bots():
    """启动所有已扫码的 bot，并预热各自账号的对话页。"""
    _migrate_legacy()
    for acc in list(store.accounts):
        if not os.path.exists(_state_path(acc["id"])):
            continue
        bot = get_bot(acc["id"])
        if bot.state.get("bot_token"):
            bot.start()
            threading.Thread(target=warm_wx, args=(acc["id"],), daemon=True,
                             name="wx-warm-" + acc["id"][:6]).start()


def stop_bots():
    for bot in list(_bots.values()):
        bot.stop()


def warm_wx(acc_id: str):
    """提前把该账号的微信对话页面打开，让第一条消息也是快的。"""
    try:
        acc = store.get_account(acc_id)
        if not acc or not acc.get("cookies"):
            return
        if direct.enabled():
            try:
                direct.get_client(acc_id).warm(acc["cookies"])
            except Exception as exc:  # noqa: BLE001
                log.warning("直连网关预热失败（发消息时会重试）: %s", str(exc)[:160])
        if not direct.enabled():
            engine.start()
            engine.prewarm_chat(acc["cookies"], acc.get("cookies_exp"), acc_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("微信对话页面预热失败（不影响使用）: %s", exc)


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
    """每个 muse 账号一行：有没有绑定微信、连接状态。"""
    rows = []
    for a in store.accounts:
        bot = _bots.get(a["id"])
        if bot is None and os.path.exists(_state_path(a["id"])):
            bot = get_bot(a["id"])
        rows.append({"account_id": a["id"], "label": a.get("label") or a["id"],
                     **(bot.info() if bot else {"status": "disconnected", "allow_from": [], "login": None})})
    return {"enabled": CFG.weixin_enabled, "bots": rows}


@router.post("/admin/weixin/{acc_id}/login")
async def weixin_login(acc_id: str, _=Depends(auth)):
    if not CFG.weixin_enabled:
        raise HTTPException(400, "微信接入已被 MUSE2API_WEIXIN_ENABLED=0 关闭")
    bot = get_bot(acc_id)
    try:
        info = await asyncio.to_thread(bot.start_login)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"获取二维码失败: {exc}") from exc
    return {**info, "svg": _qr_svg(info.get("content") or "")}


@router.get("/admin/weixin/{acc_id}/login/status")
async def weixin_login_status(acc_id: str, _=Depends(auth)):
    bot = get_bot(acc_id)
    try:
        await asyncio.to_thread(bot.poll_login)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"查询扫码状态失败: {exc}") from exc
    if bot.status == "connected":
        threading.Thread(target=warm_wx, args=(acc_id,), daemon=True).start()
    return bot.info()


@router.post("/admin/weixin/{acc_id}/logout")
def weixin_logout(acc_id: str, _=Depends(auth)):
    bot = get_bot(acc_id)
    bot.disconnect()
    return bot.info()


@router.post("/admin/weixin/{acc_id}/allow")
def weixin_allow(acc_id: str, payload: dict = Body(default={}), _=Depends(auth)):
    users = payload.get("users") or []
    if not isinstance(users, list):
        raise HTTPException(400, "users 需为数组")
    bot = get_bot(acc_id)
    bot.set_allow([str(u) for u in users])
    return bot.info()
