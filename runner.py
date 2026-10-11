"""执行层：换号重试、续签、对话流与生图/生视频的统一运行入口。"""
from __future__ import annotations

import threading
import time


from engine import MuseAuthError, MuseGenerationError
from state import ACCOUNT_POOL, GEN_LOCK, engine, log, store

def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """调用 /api/session 续签账号 cookie 并写回 store，返回最新账号 dict。
    近期（10分钟内）已续签且状态正常的账号直接复用，避免每次请求阻塞 2~3 秒 HTTP 往返。"""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm)
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"),
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"会话正常 (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except Exception as exc:
        log.warning("HTTP 预续签账号 %s 异常: %s", acc_id, exc)
    return store.get_account(acc_id)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None,
                     main_chat: bool = False, fallback_prompt: str | None = None,
                     on_options=None, on_prompt=None, on_turn=None, on_partial=None):
    """在独立线程中执行 chat_stream 并持有 GEN_LOCK，通过 Queue 往外吐。
    无论下游客户端何时断连、异常或超时，stop_event + finally 块保证 100% 立即释放 GEN_LOCK，绝不死锁。
    若首字前遇到单账号 VM 卡死或会话异常，自动切换下一个健康账号重试一次。"""
    import queue
    q = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        leased_id = None
        try:
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    if leased_id:
                        ACCOUNT_POOL.release(leased_id)
                        leased_id = None
                    try:
                        alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=timeout)
                    except Exception:
                        break
                    if not alt or alt["id"] == cur_id:
                        if alt:
                            leased_id = alt["id"]
                        break
                    cur_id = alt["id"]
                    leased_id = cur_id
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("【对话自动切号】切换到备用账号 %s (%s) 重试...", alt.get("label"), cur_id)
                elif cur_id:
                    ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)
                    leased_id = cur_id

                yielded = False
                try:
                    if cur_id:
                        refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                        if refreshed:
                            cur_cookies = refreshed["cookies"]
                            cur_exp = refreshed.get("cookies_exp")
                    with GEN_LOCK:
                        if stop_event.is_set():
                            return
                        engine.start()
                        stream = engine.chat_stream_warm if main_chat else engine.chat_stream
                        for chunk in stream(
                            cur_cookies, prompt, cur_exp, timeout,
                            account_id=cur_id, stop_event=stop_event,
                            main_chat=main_chat, fallback_prompt=fallback_prompt,
                            on_options=on_options, on_prompt=on_prompt,
                            on_turn=on_turn, on_partial=on_partial
                        ):
                            yielded = True
                            q.put(("data", chunk))
                            if stop_event.is_set():
                                return
                    if cur_id:
                        store.mark(cur_id, True, "")
                        _sync_cookies(cur_id)
                    return
                except MuseAuthError as exc:
                    last_exc = exc
                    if cur_id:
                        store.mark(cur_id, False, str(exc))
                    engine.close_current_page()
                    if yielded:
                        break
                except Exception as exc:
                    last_exc = exc
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                    if yielded:
                        break
            if last_exc is not None:
                q.put(("error", last_exc))
        finally:
            if leased_id:
                ACCOUNT_POOL.release(leased_id)
            if not main_chat:        # 主要聊天的页面由常驻线程持有，保持打开以便下一条消息秒回
                engine.close_current_page()
            q.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


# ------------------------- 核心生成 -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """生成完从浏览器读回 cookie 并写回账号池。

    实测结论：**核心 cookie 的 expires 不会被使用行为续期**（hatch_vml 固定
    约 2 天就到期），这里同步回来的主要是那些每次访问都会重新下发的非核心
    cookie（_fbp / wd / dpr 等）以及可能新增的条目，避免账号信息比实际更旧。

    注意：这个函数**绝不能把生成结果搞失败** —— 生成已经成功了，
    同步只是锦上添花，出任何问题都只记一条日志。
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # 有效期要「合并」而不是「覆盖」：会话 cookie 读回的 expires 是 -1，
        # 若整体覆盖会把原有有效期记录清空（实测踩过这个坑）。
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()))
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("cookie 同步失败（不影响本次生成）: %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    on_acquired=None) -> tuple[dict, str | None]:
    # Browser ownership covers account selection, retry and cleanup, not just generate().
    deadline = time.monotonic() + max(1, timeout)
    if not GEN_LOCK.acquire(timeout=max(1, timeout)):
        raise MuseGenerationError("等待浏览器队列超时，请稍后重试")
    try:
        if on_acquired:
            try:
                on_acquired()
            except Exception:
                pass
        return _run_generation_locked(prompt, kind, timeout, account_id,
                                      on_progress, reference_image, deadline=deadline)
    finally:
        GEN_LOCK.release()


def _run_generation_locked(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None, deadline=None) -> tuple[dict, str | None]:
    remaining_wait = int(deadline - time.monotonic()) if deadline is not None else timeout
    acc = ACCOUNT_POOL.acquire(account_id=account_id,
                               preferred_id=getattr(engine, "current_acc_id", None),
                               timeout=max(1, remaining_wait))
    cur_acc = acc
    last_exc = None
    try:
        for attempt in range(2):
            if deadline is not None and time.monotonic() >= deadline:
                raise MuseGenerationError("任务总等待时限已到，停止重试")
            if attempt > 0:
                if last_exc and "未产出媒体附件，仅返回了文本回复" in str(last_exc):
                    break
                old_id = cur_acc["id"] if cur_acc else None
                if old_id:
                    ACCOUNT_POOL.release(old_id)
                cur_acc = None
                rem = int(deadline - time.monotonic()) if deadline is not None else timeout
                if rem <= 0:
                    break
                alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=rem)
                if not alt or (old_id and alt["id"] == old_id):
                    if alt:
                        cur_acc = alt
                    break
                cur_acc = alt
                log.info("【生图/视频自动切号】切换到备用账号 %s (%s) 重试...", cur_acc.get("label"), cur_acc["id"])
            try:
                refreshed = _renew_and_persist(cur_acc["id"], wake_vm=True, force=(attempt > 0))
                if refreshed:
                    cur_acc = refreshed
                engine.start()
                remaining = int(deadline - time.monotonic()) if deadline is not None else timeout
                if remaining <= 0:
                    raise MuseGenerationError("任务总等待时限已到，停止重试")
                res = engine.generate(cur_acc["cookies"], prompt, expect=kind,
                                      timeout=remaining, expires=cur_acc.get("cookies_exp"),
                                      account_id=cur_acc["id"], on_progress=on_progress,
                                      reference_image=reference_image)
                store.mark(cur_acc["id"], True, "")
                _sync_cookies(cur_acc["id"])
                return res, cur_acc["id"]
            except MuseAuthError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], False, str(exc))
                engine.close_current_page()
            except MuseGenerationError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], True, f"任务异常: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                engine.close_current_page()
                last_exc = MuseGenerationError(f"生成失败: {exc}")
        raise last_exc
    finally:
        if cur_acc:
            ACCOUNT_POOL.release(cur_acc.get("id"))
        engine.close_current_page()


