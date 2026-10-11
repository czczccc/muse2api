"""微信 ClawBot（腾讯官方 iLink Bot 通道）接入。

流程：管理页点「生成二维码」→ 微信扫码并确认 → 拿到 bot_token → 后台线程长轮询
收私聊消息 → 交给 app.py 注册的 handler 处理 → 把文字/图片/视频发回微信。

协议参考 Tencent/openclaw-weixin：纯 HTTP/JSON，不需要公网回调。
注意：该机器人身份只能私聊，且回复依赖入站消息的 context_token（约 24 小时有效）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from urllib.parse import quote

import requests

log = logging.getLogger("muse2api.weixin")

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "2.4.6"
ERR_SESSION_EXPIRED = -14
MEDIA_TYPE = {"image": 1, "video": 2, "file": 3}
ITEM_TYPE = {"text": 1, "image": 2, "video": 5, "file": 4}
MAX_TEXT = 1800  # 单条文本保守上限，超长自动分段


# ---------------------------- AES-128-ECB (PKCS7) ----------------------------
def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


def padded_size(n: int) -> int:
    return (n // 16 + 1) * 16


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    return [x for x in out if x.strip()]


# ---------------------------- iLink HTTP 客户端 ----------------------------
class ILinkClient:
    def __init__(self, base_url: str = DEFAULT_BASE, token: str = "",
                 cdn_url: str = DEFAULT_CDN, session=None):
        self.base = (base_url or DEFAULT_BASE).rstrip("/")
        self.token = token
        self.cdn = cdn_url.rstrip("/")
        self.http = session or requests.Session()

    def _headers(self) -> dict:
        uin = base64.b64encode(str(struct.unpack(">I", secrets.token_bytes(4))[0]).encode()).decode()
        h = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
             "X-WECHAT-UIN": uin}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def _get(self, path: str, params: dict, timeout: float = 15) -> dict:
        r = self.http.get(self.base + path, params=params, headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict, timeout: float = 15) -> dict:
        body = dict(body)
        body.setdefault("base_info", {"channel_version": CHANNEL_VERSION})
        r = self.http.post(self.base + path, json=body, headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    # 登录
    def get_qrcode(self) -> dict:
        return self._get("/ilink/bot/get_bot_qrcode", {"bot_type": 3})

    def qrcode_status(self, qrcode: str) -> dict:
        return self._get("/ilink/bot/get_qrcode_status", {"qrcode": qrcode}, timeout=40)

    # 收发
    def get_updates(self, buf: str, timeout: float = 40) -> dict:
        return self._post("/ilink/bot/getupdates", {"get_updates_buf": buf}, timeout=timeout)

    def _send(self, to_user: str, item: dict, context_token: str) -> dict:
        msg = {"from_user_id": "", "to_user_id": to_user,
               "client_id": "muse2api-" + uuid.uuid4().hex, "message_type": 2,
               "message_state": 2, "context_token": context_token, "item_list": [item]}
        res = self._post("/ilink/bot/sendmessage", {"msg": msg})
        code = res.get("ret", res.get("errcode", 0)) or 0
        if code:
            raise RuntimeError(f"sendmessage 失败 ret={code} {res.get('errmsg', '')}")
        return res

    def send_text(self, to_user: str, text: str, context_token: str):
        for part in split_text(text):
            self._send(to_user, {"type": ITEM_TYPE["text"], "text_item": {"text": part}}, context_token)

    def upload_media(self, data: bytes, kind: str, to_user: str) -> dict:
        aeskey = secrets.token_bytes(16)
        filekey = secrets.token_hex(16)
        cipher = aes_ecb_encrypt(data, aeskey)
        res = self._post("/ilink/bot/getuploadurl", {
            "filekey": filekey, "media_type": MEDIA_TYPE[kind], "to_user_id": to_user,
            "rawsize": len(data), "rawfilemd5": hashlib.md5(data).hexdigest(),
            "filesize": len(cipher), "no_need_thumb": True, "aeskey": aeskey.hex()}, timeout=30)
        full = (res.get("upload_full_url") or "").strip()
        if not full:
            param = res.get("upload_param")
            if not param:
                raise RuntimeError("getuploadurl 未返回上传地址")
            full = f"{self.cdn}/upload?encrypted_query_param={quote(param)}&filekey={quote(filekey)}"
        last = None
        for _ in range(3):
            r = self.http.post(full, data=cipher, timeout=120,
                               headers={"Content-Type": "application/octet-stream"})
            if 400 <= r.status_code < 500:
                raise RuntimeError(f"CDN 上传被拒绝: {r.headers.get('x-error-message') or r.text[:100]}")
            dl = r.headers.get("x-encrypted-param")
            if r.status_code == 200 and dl:
                return {"param": dl, "aeskey": aeskey.hex(), "size": len(cipher)}
            last = f"HTTP {r.status_code}"
        raise RuntimeError(f"CDN 上传失败: {last}")

    def send_media(self, to_user: str, data: bytes, kind: str, context_token: str):
        up = self.upload_media(data, kind, to_user)
        media = {"encrypt_query_param": up["param"],
                 "aes_key": base64.b64encode(up["aeskey"].encode()).decode(), "encrypt_type": 1}
        if kind == "image":
            item = {"type": ITEM_TYPE["image"], "image_item": {"media": media, "mid_size": up["size"]}}
        elif kind == "video":
            item = {"type": ITEM_TYPE["video"], "video_item": {"media": media, "video_size": up["size"]}}
        else:
            raise ValueError(kind)
        self._send(to_user, item, context_token)


# ---------------------------- Bot：登录状态 + 轮询 ----------------------------
# handler(user_id, text) -> list of ("text", str) | ("media", path, kind)
Handler = Callable[[str, str], list]


class WeixinBot:
    def __init__(self, state_file: str, handler: Handler, allow_env: str = "",
                 client_factory=ILinkClient, workers: int = 4):
        self.state_file = state_file
        self.handler = handler
        self.allow_env = [x.strip() for x in allow_env.split(",") if x.strip()]
        self.factory = client_factory
        self.lock = threading.Lock()
        self.state: dict = self._load()
        self.login: dict = {}          # 进行中的扫码：{qrcode, content, status, started}
        self.status = "disconnected"   # disconnected | connected | expired
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wx")
        self._seen: list = []
        if self.state.get("bot_token"):
            self.status = "connected"

    # ---- 持久化 ----
    def _load(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_file)
        try:
            os.chmod(self.state_file, 0o600)
        except OSError:
            pass

    def _client(self) -> ILinkClient:
        return self.factory(self.state.get("baseurl") or DEFAULT_BASE, self.state.get("bot_token", ""))

    # ---- 白名单 ----
    def allow_list(self) -> list[str]:
        return list(dict.fromkeys(self.allow_env + self.state.get("allow_from", [])))

    def allowed(self, user_id: str) -> bool:
        return user_id in self.allow_list()

    # ---- 扫码登录 ----
    def start_login(self) -> dict:
        res = ILinkClient().get_qrcode()
        qr = res.get("qrcode")
        if not qr:
            raise RuntimeError(f"获取二维码失败: {res}")
        self.login = {"qrcode": qr, "content": res.get("qrcode_img_content") or qr,
                      "status": "wait", "started": time.time()}
        return self.login_info()

    def login_info(self) -> dict:
        lg = self.login
        return {"qrcode": lg.get("qrcode"), "content": lg.get("content"),
                "status": lg.get("status", "none")}

    def poll_login(self) -> dict:
        """查询一次扫码状态；确认后落盘凭据并启动轮询。"""
        lg = self.login
        if not lg:
            return {"status": "none"}
        if time.time() - lg["started"] > 480:
            lg["status"] = "expired"
            return self.login_info()
        try:
            res = ILinkClient().qrcode_status(lg["qrcode"])
        except requests.exceptions.ReadTimeout:
            return self.login_info()   # 长轮询超时属正常
        st = res.get("status", "wait")
        lg["status"] = st
        if st == "confirmed" and res.get("bot_token"):
            with self.lock:
                self.state.update(bot_token=res["bot_token"],
                                  baseurl=res.get("baseurl") or DEFAULT_BASE,
                                  bot_id=res.get("ilink_bot_id", ""),
                                  user_id=res.get("ilink_user_id", ""),
                                  updates_buf="", ctx={}, connected_at=int(time.time()))
                # 默认只放行扫码本人
                if res.get("ilink_user_id") and not self.state.get("allow_from"):
                    self.state["allow_from"] = [res["ilink_user_id"]]
                self._save()
            self.login = {}
            self.status = "connected"
            self.last_error = ""
            self.start()
        return self.login_info()

    def disconnect(self):
        self.stop()
        with self.lock:
            keep = {"allow_from": self.state.get("allow_from", [])}
            self.state = keep
            self._save()
        self.login = {}
        self.status = "disconnected"

    def set_allow(self, users: list[str]):
        with self.lock:
            self.state["allow_from"] = [u.strip() for u in users if u.strip()]
            self._save()

    # ---- 轮询 ----
    def start(self):
        if not self.state.get("bot_token"):
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="weixin-poll")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        client = self._client()
        fails = 0
        while not self._stop.is_set():
            try:
                res = client.get_updates(self.state.get("updates_buf", ""))
                code = res.get("ret", res.get("errcode", 0)) or 0
                if code == ERR_SESSION_EXPIRED:
                    self.status = "expired"
                    self.last_error = "微信会话已失效，请重新扫码"
                    log.warning(self.last_error)
                    return
                if code:
                    raise RuntimeError(f"getupdates ret={code} {res.get('errmsg', '')}")
                fails = 0
                self.status = "connected"
                buf = res.get("get_updates_buf") or res.get("sync_buf")
                for m in res.get("msgs") or []:
                    self._dispatch(m)
                if buf is not None and buf != self.state.get("updates_buf"):
                    with self.lock:
                        self.state["updates_buf"] = buf
                        self._save()
            except requests.exceptions.ReadTimeout:
                continue
            except Exception as exc:  # noqa: BLE001
                fails += 1
                self.last_error = str(exc)[:200]
                log.warning("微信轮询异常(%d): %s", fails, exc)
                self._stop.wait(min(60, 2 ** min(fails, 6)))

    def _dispatch(self, m: dict):
        if m.get("message_type") != 1 or m.get("group_id"):
            return
        mid = m.get("message_id") or m.get("seq")
        if mid is not None:
            if mid in self._seen:
                return
            self._seen.append(mid)
            del self._seen[:-200]
        user = m.get("from_user_id", "")
        ctx = m.get("context_token", "")
        text = "".join((it.get("text_item") or {}).get("text", "")
                       for it in m.get("item_list") or [] if it.get("type") == 1).strip()
        if not user or not text:
            return
        if not self.allowed(user):
            log.info("微信消息被白名单拦截: %s", user)
            return
        self.pool.submit(self._handle, user, text, ctx)

    def _handle(self, user: str, text: str, ctx: str):
        client = self._client()
        try:
            replies = self.handler(user, text)
        except Exception as exc:  # noqa: BLE001
            log.exception("微信消息处理失败")
            replies = [("text", f"处理失败：{str(exc)[:200]}")]
        for r in replies:
            try:
                if r[0] == "text":
                    client.send_text(user, r[1], ctx)
                else:
                    with open(r[1], "rb") as f:
                        client.send_media(user, f.read(), r[2], ctx)
            except Exception as exc:  # noqa: BLE001
                log.warning("微信发送失败: %s", exc)
                self.last_error = str(exc)[:200]

    # ---- 状态 ----
    def info(self) -> dict:
        return {"status": self.status, "bot_id": self.state.get("bot_id", ""),
                "user_id": self.state.get("user_id", ""),
                "connected_at": self.state.get("connected_at"),
                "allow_from": self.allow_list(), "last_error": self.last_error,
                "login": self.login_info() if self.login else None}
