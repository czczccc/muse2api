"""账号保活与静默续期。"""
from __future__ import annotations

import asyncio
import time


from engine import MuseAuthError
from runner import _renew_and_persist
from state import CFG, GEN_LOCK, engine, log, store

# ------------------------- 账号自动保活与静默续期 -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "next_run": None,
}


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """通过 muse.ai/api/session 触发 Meta 网关签发新 hatch_vml (+48h) / hatch_sess (+30d) 并唤醒云端 VM。"""
    acc = store.get_account(aid)
    if not acc or not acc.get("cookies"):
        return {"ok": False, "id": aid, "label": (acc or {}).get("label", aid), "error": "账号无有效 cookie"}
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=True)
        store.update_account(
            aid,
            cookies=res["cookies"],
            cookies_exp=res["cookies_exp"],
            ok=True if res.get("ok") else False,
            synced_at=int(time.time()),
        )
        vm_state = res.get("vm_state") or "RUNNING"
        store.touch_keepalive(aid, True, f"会话有效 · 自动保活 (VM: {vm_state})")
        quota = acc.get("quota")
        if check_quota and GEN_LOCK.acquire(blocking=False):
            try:
                engine.start()
                quota = engine.quota(res["cookies"], res["cookies_exp"])
                quota["checked_at"] = int(time.time())
                store.update_account(aid, quota=quota)
            except Exception as qe:  # noqa: BLE001
                log.warning("读取账号 %s 额度失败: %s", aid, qe)
            finally:
                GEN_LOCK.release()
        updated = store.get_account(aid) or {}
        return {
            "ok": True,
            "id": aid,
            "label": acc.get("label", aid),
            "vm_id": res.get("vm_id"),
            "vm_state": vm_state,
            "wake_ok": res.get("wake_ok"),
            "expires_at": updated.get("expires_at"),
            "quota": quota,
        }
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, f"保活认证失败: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        store.touch_keepalive(aid, None, f"保活未确认（保留账号状态）: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": str(exc)}


async def run_keepalive_all(force: bool = False) -> dict:
    """执行账号保活：force=True 时强制刷新所有启用账号，否则刷新临期/未检账号并保持 VM 热备。"""
    async with KEEPALIVE_LOCK:
        now = int(time.time())
        KEEPALIVE_STATE["running"] = True
        KEEPALIVE_STATE["last_run"] = now
        results = []
        skipped = []

        try:
            include_disabled = getattr(CFG, "keepalive_disabled_accounts", False)
            accounts = [a for a in store.list_accounts() if include_disabled or a.get("enabled", True)]
            for a in accounts:
                aid = a["id"]
                label = a.get("label", aid)
                exp_at = a.get("expires_at")
                last_ka = a.get("last_keepalive") or 0

                needs_run = (
                    force
                    or not exp_at
                    or (exp_at - now < 36 * 3600)
                    or ((now - last_ka) > 15 * 60)
                    or (a.get("ok") is not True)
                )
                if not needs_run:
                    skipped.append({"id": aid, "label": label, "reason": "会话充足且近期已保活"})
                    continue

                log.info("【自动保活】正在为账号 %s (%s) 执行静默续期与 VM 唤醒...", label, aid)
                res = await asyncio.to_thread(_probe_account_sync, aid, False)
                results.append(res)
                log.info("【自动保活】账号 %s 执行结果: ok=%s expires_at=%s", label, res.get("ok"), res.get("expires_at"))
                await asyncio.sleep(0.5)

            summary = {
                "checked_at": now,
                "refreshed_count": len(results),
                "refreshed": results,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
            KEEPALIVE_STATE["last_result"] = summary
            KEEPALIVE_STATE["next_run"] = now + 900
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


def _warmup_browser_sync():
    """后台静默预热浏览器与首个可用账号的 WebSocket 隧道，使重启后首条请求也秒回。"""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    acc = store.pick_account(rotate=False)
    if not acc or not acc.get("cookies"):
        return
    if not GEN_LOCK.acquire(blocking=False):
        return
    try:
        log.info("【浏览器预热】正在后台预热账号 %s (%s) 的热备标签页...", acc.get("label"), acc["id"])
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("【浏览器预热】账号 %s (%s) 热备标签页与 WebSocket 已就绪", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("【浏览器预热】预热异常: %s", exc)
    finally:
        GEN_LOCK.release()


async def _keepalive_loop():
    """后台常驻守护任务：每 15 分钟轮询一次账号健康状态并保持 VM 热备。"""
    log.info("【自动保活守护进程】已启动，检测周期: 15 分钟")
    await asyncio.sleep(1)
    try:
        await asyncio.to_thread(_warmup_browser_sync)
    except Exception as e:  # noqa: BLE001
        log.warning("【浏览器预热】异常: %s", e)
    while True:
        try:
            await run_keepalive_all(force=False)
            if not getattr(engine, "current_acc_id", None) or engine.page is None:
                await asyncio.to_thread(_warmup_browser_sync)
        except Exception as e:  # noqa: BLE001
            log.error("【自动保活守护进程】轮询异常: %s", e)
        await asyncio.sleep(900)

