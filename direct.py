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
               if re.search(r"attach|media|file|image|video|artifact", k, re.I))


def _presentation_media(p: dict) -> list[dict]:
    """delta.presentation 里的图片：data.images[]，带 sandbox:// 路径和 variants.original 原图链接。"""
    out = []
    data = p.get("data") if isinstance(p.get("data"), dict) else {}
    for key in ("images", "videos", "files"):
        for im in data.get(key) or []:
            if not isinstance(im, dict) or im.get("missing"):
                continue
            path = (im.get("path") or "").replace("sandbox://", "")
            v = im.get("variants") if isinstance(im.get("variants"), dict) else {}
            url = v.get("original") or next((x for x in v.values() if isinstance(x, str)), None)
            out.append({"url": None, "alt_url": url, "artifact_id": None, "path": path or None,
                        "name": os.path.basename(path) if path else (im.get("label") or ""),
                        "mime": im.get("mime") or ""})
    return out or _find_media(p)


def _find_media(obj, out=None) -> list[dict]:
    """在回复记录里找附件：带 http(s) 链接，或带 artifact/handle id 的字典。字段名还没实测，尽量宽松。"""
    out = [] if out is None else out
    if isinstance(obj, dict):
        url = next((obj[k] for k in ("url", "signed_url", "download_url", "src", "uri")
                    if isinstance(obj.get(k), str) and obj[k].startswith("http")), None)
        aid = next((obj[k] for k in ("artifact_id", "artifactId", "handle", "file_id")
                    if isinstance(obj.get(k), str)), None)
        path = next((obj[k] for k in ("image_path", "video_path", "file_path", "path")
                     if isinstance(obj.get(k), str) and "/" in obj[k] and not obj[k].startswith("http")
                     and re.search(r"\.[A-Za-z0-9]{1,8}$", obj[k])), None)
        if url or aid or path:
            out.append({"url": url, "artifact_id": aid, "path": path,
                        "name": obj.get("name") or obj.get("filename") or (os.path.basename(path) if path else ""),
                        "mime": obj.get("mime_type") or obj.get("mimeType") or obj.get("content_type") or ""})
            return out
        for v in obj.values():
            _find_media(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_media(v, out)
    return out


APPROVAL_BUTTONS = ["允许一次", "总是允许此站点", "拒绝"]
# 授权决定的取值还没实测，按常见写法；失败会把服务端返回写进日志
APPROVAL_DECISIONS = ["allow_once", "allow_always", "deny"]


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

    def _fetch_media(self, m: dict) -> tuple[bytes | None, str]:
        from curl_cffi import requests as rq
        url = m.get("url")
        try:
            if m.get("path") and not url:
                try:
                    data, mime = self._fs_read(m["path"])
                    if data:
                        return data, mime
                except Exception as exc:  # noqa: BLE001
                    log.warning("fs.read 失败（%s）：%s", m["path"][-60:], str(exc)[:300])
                url = m.get("alt_url")
                if not url:
                    return None, ""
            if not url and m.get("artifact_id"):
                d = self.gw.call_json("artifacts.fetch_url", body={"artifact_id": m["artifact_id"]})
                url = (d.get("url") if isinstance(d, dict) else d) or None
                if not isinstance(url, str):
                    log.info("artifacts.fetch_url 返回：%s", str(d)[:300])
                    return None, ""
            r = rq.get(url, headers={"Cookie": self.cookie, "Referer": "https://muse.ai/",
                                     **({"Authorization": "Bearer " + self.gw.hatch_token}
                                        if getattr(self.gw, "hatch_token", None) else {})},
                       impersonate="chrome", timeout=60)
            if r.status_code == 200 and r.content:
                return r.content, r.headers.get("content-type", "")
            log.warning("取附件失败 %s：%s", r.status_code, url[:80])
        except Exception as exc:  # noqa: BLE001
            log.warning("取附件失败：%s", str(exc)[:160])
        return None, ""

    def _emit_media(self, m: dict, out: list, on_message):
        data, mime = self._fetch_media(m)
        mime = mime or m.get("mime") or ""
        kind = "image" if mime.startswith("image") else "video" if mime.startswith("video") else "file"
        it = ("media", data, kind, m.get("name") or "") if data else \
            ("text", "（附件没能取回，请到 muse.ai 页面查看）")
        out.append(it)
        on_message(it)

    def _fs_read(self, path: str) -> tuple[bytes | None, str]:
        """用网关的 fs.read 分段读 VM 里的文件（生成的图片/文件都在 workspace/ 下）。
        fs.read 必须带 offset；返回格式按 JSON(base64) 或原始字节两种情况处理。"""
        import base64
        import json as _json
        mime = "image/png" if path.lower().endswith(".png") else \
            "image/jpeg" if re.search(r"\.jpe?g$", path, re.I) else \
            "image/webp" if path.lower().endswith(".webp") else \
            "video/mp4" if path.lower().endswith(".mp4") else ""
        size = None
        try:
            st = self.gw.call_json("fs.stat", body={"path": path})
            size = int(st.get("size")) if isinstance(st, dict) and st.get("size") is not None else None
        except Exception:  # noqa: BLE001
            pass
        chunk = 512 * 1024
        buf = b""
        while size is None or len(buf) < size:
            raw = self.gw.request("fs.read", body={"path": path, "offset": len(buf), "len": chunk,
                                                   }, timeout=60)
            part = raw
            try:
                d = _json.loads(raw)
            except ValueError:
                d = None
            if isinstance(d, dict):
                d = d.get("result", d)
                v = next((d[k] for k in ("content_base64", "data_base64", "base64", "data", "content", "bytes")
                          if isinstance(d.get(k), str)), None) if isinstance(d, dict) else None
                if v is None:
                    log.warning("fs.read 返回无法识别：%s", str(d)[:300])
                    return None, ""
                enc = str(d.get("encoding") or "").lower()
                part = v.encode("latin-1") if enc in ("utf8", "utf-8", "text") else base64.b64decode(v)
                if size is None and d.get("size") is not None:
                    size = int(d["size"])
                if d.get("eof") is True:
                    buf += part
                    break
            if not part:
                break
            buf += part
            if size is None and len(part) < chunk:
                break
        return (buf or None), mime

    def _check_approvals(self, seen: set, on_prompt):
        try:
            d = self.gw.call_json("egress.approvals")
        except Exception as exc:  # noqa: BLE001
            log.debug("读取授权请求失败: %s", exc)
            return
        items = d if isinstance(d, list) else (d.get("pending") or d.get("approvals") or d.get("items") or []) if isinstance(d, dict) else []
        for a in items:
            if not isinstance(a, dict):
                continue
            aid = a.get("approval_id") or a.get("id")
            if not aid or aid in seen or a.get("status") not in (None, "pending", "PENDING"):
                continue
            seen.add(aid)
            log.info("直连授权请求：%s", str(a)[:400])
            host = a.get("host") or a.get("destination") or a.get("domain") or a.get("target") or ""
            port = a.get("port")
            what = a.get("display_name") or ""
            cmd = (a.get("observed_command") or "")[:200]
            txt = (f"允许 Muse 与 {host}{':' + str(port) if port else ''} 分享信息？"
                   + (f"\n来源：{what}" if what else "") + (f"\n命令：{cmd}" if cmd else "")).strip()
            idx = on_prompt(txt, APPROVAL_BUTTONS) if on_prompt else None
            decision = APPROVAL_DECISIONS[idx if idx is not None else 2]
            opts = a.get("decision_options") or []
            if opts and decision not in opts:
                decision = "deny" if "deny" in opts else opts[-1]
            for body in ({"decision": decision}, {"decision": decision, "request_id": a.get("request_id")}):
                try:
                    r = self.gw.call_json("egress.approval.decide", path_params={"approval_id": aid}, body=body)
                    log.info("授权已提交 %s：%s", decision, str(r)[:200])
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("提交授权失败（%s）：%s", body, str(exc)[:300])

    def turn(self, cookies: dict, text: str, timeout: float, on_message, on_prompt=None) -> list:
        """发一条消息到主要聊天，回复每完成一条就调用 on_message(item)；item 为 ("text", s)
        或 ("media", bytes, kind, name)。返回全部 item。"""
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
            approvals: set = set()
            last_appr = 0.0
            while True:
                now = time.time()
                if not out and now > deadline:
                    raise TimeoutError("直连等待助手回复超时")
                if out and now > deadline + 60:
                    break
                time.sleep(POLL)
                if time.time() - last_appr > 1.5:
                    last_appr = time.time()
                    t_a = time.time()
                    self._check_approvals(approvals, on_prompt)
                    deadline += time.time() - t_a       # 等用户回授权的时间不算超时
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
                    seq = ev.get("seq")
                    if ev.get("event_name") == "delta.presentation" and (seq or 0) > baseline \
                            and seq not in delivered:
                        media = _presentation_media(_payload(ev))
                        if not media:
                            if str(_payload(ev).get("state") or "").lower() not in ("failed", "error", "cancelled"):
                                pending = True
                            continue
                        delivered.add(seq)
                        log.info("直连：生成内容 %s", str({k: v for k, v in _payload(ev).items()
                                                        if k in ("kind", "state", "image_path", "id")})[:300])
                        for m in media:
                            self._emit_media(m, out, on_message)
                        last_new = time.time()
                        continue
                    if ev.get("event_name") != "message.assistant" or (seq or 0) <= baseline:
                        continue
                    if is_reply(ev, baseline):
                        if seq in delivered:
                            continue
                        delivered.add(seq)
                        if not first_logged:
                            log.info("直连：首条回复 %.1fs", time.time() - t0)
                            first_logged = True
                        items = [("text", _text(ev))]
                        media = _find_media(_payload(ev))
                        if _has_attachment(ev) or media:
                            log.info("直连回复附件：%s", str({k: v for k, v in _payload(ev).items()
                                                            if k not in ("display_text", "content")})[:800])
                        for it in items:
                            out.append(it)
                            on_message(it)
                        for m in media:
                            self._emit_media(m, out, on_message)
                        last_new = time.time()
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
