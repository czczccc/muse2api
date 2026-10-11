"""直连 muse.ai 网关发文字消息（不开浏览器），延迟只剩网络往返。

流程：cookie -> access_token -> hatch token -> Noise WebSocket；用 chat.stream 发消息，
再轮询 chat.history 取回复（每条助手回复是一条独立记录，对应微信里的一个气泡）。
协议来自 nikships/muse-cli（MIT），见 mgateway/。开关：环境变量 MUSE2API_DIRECT=1。
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from state import log

MAX_AGE = 20 * 60        # 连接复用的最长时间；hatch token 会过期，到点重连
POLL = 0.6               # 轮询历史的间隔
QUIET = 4.0              # 最后一条回复之后静默多久就认为这一轮结束
# 带图片/文件/视频的请求仍走浏览器：直连里附件怎么表示还没验证过
NEEDS_BROWSER = re.compile(r"图|画|照片|海报|视频|文件|导出|下载|image|picture|photo|video|draw|file|download", re.I)


def enabled() -> bool:
    return os.environ.get("MUSE2API_DIRECT", "1").strip() not in ("", "0", "false", "off")


class DirectNotSent(RuntimeError):
    """消息还没发出去就失败了（鉴权/连接问题），调用方可以放心退回浏览器方案。"""


def _cookie_str(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _payload(ev: dict) -> dict:
    p = ev.get("payload")
    return p if isinstance(p, dict) else {}


def _text(ev: dict) -> str:
    p = _payload(ev)
    return (p.get("display_text") or p.get("content") or "").strip()


def is_reply(ev: dict, baseline: int) -> bool:
    """真正的助手回复：已完成（有 status）、有文字、不是 reply_to 指向别处的后台推送。"""
    p = _payload(ev)
    return (ev.get("event_name") == "message.assistant" and (ev.get("seq") or 0) > baseline
            and not (ev.get("reply_to_message_id") or p.get("reply_to_message_id"))
            and bool(_text(ev)) and bool(p.get("status")))


def _has_attachment(ev: dict) -> bool:
    return any(v for k, v in _payload(ev).items()
               if re.search(r"attach|media|file|image|video", k, re.I))


class DirectClient:
    def __init__(self, acc_id: str):
        self.acc_id = acc_id
        self.node_id = hashlib.sha1(acc_id.encode()).hexdigest()[:16]
        self.lock = threading.Lock()
        self.gw = None
        self.t_conn = 0.0
        self.cookie = ""

    def close(self):
        if self.gw is not None:
            self.gw.close()
        self.gw = None

    def _ensure(self, cookies: dict):
        cookie = _cookie_str(cookies)
        if self.gw is not None and (cookie != self.cookie or time.time() - self.t_conn > MAX_AGE):
            self.close()
        if self.gw is None:
            from mgateway.client import Gateway
            t0 = time.time()
            self.gw = Gateway(cookie)
            self.cookie, self.t_conn = cookie, time.time()
            log.info("直连网关已连接（%.1fs）", time.time() - t0)
        return self.gw

    def warm(self, cookies: dict):
        with self.lock:
            self._ensure(cookies)

    def _history(self, limit: int) -> list[dict]:
        d = self.gw.call_json("chat.history", body={"limit": limit})
        return sorted(d.get("chat_events", []) if isinstance(d, dict) else [], key=lambda e: e.get("seq", 0))

    def turn(self, cookies: dict, text: str, timeout: float, on_message) -> list[str]:
        """发一条消息到主要聊天，回复每完成一条就调用 on_message(text)；返回全部回复文字。"""
        with self.lock:
            t0 = time.time()
            try:
                self._ensure(cookies)
                evs = self._history(1)
                baseline = max((e.get("seq", 0) for e in evs), default=0)
                self.gw._open("chat.stream", body={"items": [{"type": "text", "text": text}],
                                                   "node_id": self.node_id, "capabilities": {}})
            except Exception as exc:  # noqa: BLE001
                self.close()
                raise DirectNotSent(str(exc)) from exc
            log.info("直连：消息已发出（%.1fs）", time.time() - t0)
            delivered: set = set()
            out: list[str] = []
            deadline = t0 + timeout
            last_new = None
            first_logged = False
            while True:
                now = time.time()
                if not out and now > deadline:
                    raise TimeoutError("直连等待助手回复超时")
                if out and now > deadline + 60:
                    break
                time.sleep(POLL)
                try:
                    evs = self._history(15)
                except Exception as exc:  # noqa: BLE001
                    log.warning("直连读取历史失败: %s", str(exc)[:120])
                    if not isinstance(exc, (TimeoutError, RuntimeError)) or "BAD_DECRYPT" in str(exc):
                        self.close()
                        try:
                            self._ensure(cookies)
                        except Exception:  # noqa: BLE001
                            pass
                    time.sleep(1.5)
                    continue
                pending = False
                for ev in evs:
                    if ev.get("event_name") != "message.assistant" or (ev.get("seq") or 0) <= baseline:
                        continue
                    seq = ev.get("seq")
                    if is_reply(ev, baseline):
                        if seq in delivered:
                            continue
                        delivered.add(seq)
                        t = _text(ev)
                        if _has_attachment(ev):
                            log.info("直连回复带附件字段：%s", sorted(_payload(ev).keys()))
                            t += "\n（这条回复带附件，请到 muse.ai 页面查看）"
                        if not first_logged:
                            log.info("直连：首条回复 %.1fs", time.time() - t0)
                            first_logged = True
                        out.append(t)
                        last_new = time.time()
                        on_message(t)
                    elif not (ev.get("reply_to_message_id") or _payload(ev).get("reply_to_message_id")) \
                            and _text(ev) and not _payload(ev).get("status"):
                        pending = True       # 还在写的回复，继续等
                if out and not pending and last_new and time.time() - last_new > QUIET:
                    break
            return out


_clients: dict[str, DirectClient] = {}
_clients_lock = threading.Lock()


def get_client(acc_id: str) -> DirectClient:
    with _clients_lock:
        if acc_id not in _clients:
            _clients[acc_id] = DirectClient(acc_id)
        return _clients[acc_id]
