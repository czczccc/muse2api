"""管理接口：账号池、额度、API Key、任务、媒体、保活。"""
from __future__ import annotations

import asyncio
import os
import re
import time

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from engine import ESSENTIAL_COOKIES, MuseAuthError, MuseGenerationError
from keepalive import KEEPALIVE_STATE, _probe_account_sync, run_keepalive_all
from runner import _pos, _sync_cookies
from schemas import AccountPatch, AccountRequest
from state import CFG, GEN_LOCK, _persist_env, _public_base, auth, engine, media_url, store

router = APIRouter()

# ------------------------- cookie 解析 -------------------------
def parse_cookie_text(text: str) -> dict[str, str]:
    """把 `a=1; b=2` / JSON / Set-Cookie 行解析成 dict。"""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        import json
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def parse_batch(text: str) -> list[tuple[str, dict]]:
    """批量导入：每行 `标签 | cookie串`，标签可省略。"""
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # `|` 出现在 cookie 里时不算分隔符
                label, body = head.strip(), tail.strip()
        cookies = parse_cookie_text(body)
        if cookies:
            out.append((label, cookies))
    return out




# ------------------------- 管理：总览 -------------------------
@router.get("/admin/status")
def admin_status(_=Depends(auth)):
    base = _public_base()
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "tasks": store.list_tasks(20),
        "media_count": len(os.listdir(CFG.media_dir))
        if os.path.isdir(CFG.media_dir) else 0,
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "base_url": f"{base}/v1" if base else "",
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


# ------------------------- 管理：账号池 -------------------------
@router.get("/admin/accounts")
def list_accounts(_=Depends(auth)):
    return {"accounts": store.list_accounts(), "stats": store.stats()}


@router.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(auth)):
    added: list[dict] = []
    seen: list[dict] = []          # 本次导入的所有 cookie，用来检查核心项是否齐全

    if req.batch:
        for label, cookies in parse_batch(req.batch):
            acc = store.add_account(cookies, label)
            seen.append(cookies)
            added.append({"id": acc["id"], "label": acc["label"],
                          "cookie_count": len(cookies),
                          "expires_at": acc.get("expires_at")})

    cookies = dict(req.cookies)
    if req.cookie_header:
        cookies.update(parse_cookie_text(req.cookie_header))
    if cookies:
        acc = store.add_account(cookies, req.label, cookies_exp=req.expires)
        seen.append(cookies)
        added.append({"id": acc["id"], "label": acc["label"],
                      "cookie_count": len(cookies),
                      "expires_at": acc.get("expires_at")})

    if not added:
        raise HTTPException(400, "未解析到任何 cookie，请检查格式")

    # 只要有一个账号把核心 cookie 凑齐就算通过（批量时按整体判断）
    missing = [n for n in ESSENTIAL_COOKIES
               if not any(n in c for c in seen)]
    return {"added": added, "count": len(added),
            "essential_missing": missing,
            "warning": (f"缺少核心 cookie：{', '.join(missing)}，该账号可能无法生成"
                        if missing else "")}


@router.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    acc = store.update_account(aid, label=req.label, enabled=req.enabled)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {k: v for k, v in acc.items() if k != "cookies"} | {
        "cookie_count": len(acc.get("cookies", {}))}


@router.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "账号不存在")
    return {"deleted": True, "id": aid}


@router.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(auth)):
    """真实打开 muse.ai 验证该账号 cookie 是否仍可登录。"""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"))
                synced = _sync_cookies(aid)
                quota = None
                try:  # 顺带刷新额度；读不到不影响测试结论
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"))
                    quota["checked_at"] = int(time.time())
                    store.update_account(aid, quota=quota)
                except Exception:  # noqa: BLE001
                    quota = None
                store.mark(aid, True, "会话有效")
                return {"ok": True, "message": "会话有效，可正常生成",
                        "synced": synced, "quota": quota}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"测试未确认（保留账号状态）: {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@router.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(auth)):
    return await test_account(aid, _)


@router.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(auth)):
    """更新某个账号的 cookie（用于会话过期后补新 cookie）。"""
    cookies = dict(payload.get("cookies") or {})
    if payload.get("cookie_header"):
        cookies.update(parse_cookie_text(payload["cookie_header"]))
    if not cookies:
        raise HTTPException(400, "未解析到 cookie")
    exp = {k: int(v) for k, v in (payload.get("expires") or {}).items()
           if _pos(v)}
    # 补入的是「全新会话」的 cookie，有效期估算锚点必须重置到当下，
    # 否则会沿用旧会话的锚点，把剩余天数算少。
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="已更新 cookie",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@router.post("/admin/relogin")
def relogin(_=Depends(auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    try:
        engine.start()
        engine.refresh(acc["cookies"], acc.get("cookies_exp"))
        return {"ok": True, "account": acc["id"]}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ------------------------- 管理：额度 -------------------------
@router.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(auth)):
    """打开 muse.ai 的 Settings 面板读该账号的额度（实时），并缓存到账号记录。

    返回例：{"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK:
            engine.start()
            q = engine.quota(acc["cookies"], acc.get("cookies_exp"))
            q["checked_at"] = int(time.time())
            store.update_account(aid, quota=q)
            return q

    try:
        return await asyncio.to_thread(_probe)
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"额度查询失败: {exc}") from exc


@router.post("/admin/quota")
async def query_any_quota(_=Depends(auth)):
    """用当前最久未用的可用账号查一次额度（同池账号共享同一 muse.ai 计划的
    通常只有一人使用时够用；多账号时建议按账号查）。"""
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    return await query_quota(acc["id"], _)


# ------------------------- 管理：接入信息 / API Key -------------------------


@router.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@router.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    """生成新的 API Key，写入 .env 并立即生效（不用重启）。"""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    old = CFG.api_key
    CFG.api_key = new_key
    _persist_env("MUSE2API_KEY", new_key)
    return {"ok": True, "api_key": new_key, "previous": old,
            "message": "已生成新 Key 并立即生效；旧 Key 已失效，请更新下游项目"}



# ------------------------- 管理：任务 / 媒体 -------------------------
@router.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    return {"tasks": store.list_tasks(limit)}


@router.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(auth)):
    if not store.delete_task(tid):
        raise HTTPException(404, "任务不存在")
    return {"deleted": True}


@router.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(auth)):
    return {"removed": store.clear_tasks(int(payload.get("keep") or 0))}


@router.get("/admin/media")
def admin_media(_=Depends(auth)):
    d = CFG.media_dir
    items = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            items.append({"name": name, "url": media_url(name), "bytes": os.path.getsize(p),
                          "mtime": int(os.path.getmtime(p)),
                          "kind": "video" if ext in (".mp4", ".webm", ".mov") else "image"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"media": items, "count": len(items)}


@router.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names 必须是文件名数组")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
            errors.append(f"非法文件名: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@router.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(auth)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    try:
        os.remove(p)
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"删除失败: {e}")




@router.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(auth)):
    """管理员手动触发一次全账号保活续期。"""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "保活任务正在执行中，请稍候"}
    return await run_keepalive_all(force=force)


@router.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(auth)):
    """手动针对单个账号执行保活续期。"""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@router.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(auth)):
    """获取保活守护协程状态。"""
    return KEEPALIVE_STATE




# ------------------------- 前端页面 -------------------------
_ADMIN_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")


@router.get("/", response_class=HTMLResponse)
@router.get("/admin", response_class=HTMLResponse)
def admin_page():
    try:
        with open(_ADMIN_HTML, encoding="utf-8") as f:
            return f.read()
    except OSError:
        raise HTTPException(404, "管理页面文件 admin.html 缺失")
