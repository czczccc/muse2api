"""Run: python tests/test_weixin.py ; mocks iLink, no network or live account needed."""
import base64
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import weixin as W  # noqa: E402


class FakeClient:
    sent = []
    updates = []
    status_seq = []

    def __init__(self, base="", token="", *a, **k):
        self.token = token

    def get_qrcode(self):
        return {"qrcode": "QR1", "qrcode_img_content": "https://liteapp.weixin.qq.com/q/abc"}

    def qrcode_status(self, qr):
        return FakeClient.status_seq.pop(0)

    def get_updates(self, buf, timeout=40):
        if FakeClient.updates:
            return {"ret": 0, "get_updates_buf": "B2", "msgs": FakeClient.updates.pop(0)}
        time.sleep(0.05)
        return {"ret": 0, "get_updates_buf": buf, "msgs": []}

    def send_text(self, user, text, ctx):
        FakeClient.sent.append(("text", user, text, ctx))

    def send_media(self, user, data, kind, ctx):
        FakeClient.sent.append((kind, user, len(data), ctx))


def msg(mid, user, text, group=""):
    return {"message_id": mid, "message_type": 1, "from_user_id": user, "group_id": group,
            "context_token": "CT" + str(mid), "item_list": [{"type": 1, "text_item": {"text": text}}]}


def test_helpers():
    assert W.padded_size(16) == 32 and W.padded_size(15) == 16
    key = b"k" * 16
    assert len(W.aes_ecb_encrypt(b"x" * 20, key)) == W.padded_size(20)
    parts = W.split_text("a" * 50 + "\n" + "b" * 50, limit=60)
    assert parts == ["a" * 50, "b" * 50], parts
    assert W.split_text("   ") == []


def test_login_and_chat():
    d = tempfile.mkdtemp()
    sf = os.path.join(d, "weixin.json")
    # patch the real client used for login with the fake
    W.ILinkClient_real = W.ILinkClient
    W.ILinkClient = FakeClient
    try:
        calls = []
        bot = W.WeixinBot(sf, lambda u, t: calls.append((u, t)) or [("text", "echo " + t)],
                          client_factory=FakeClient)
        info = bot.start_login()
        assert info["status"] == "wait" and info["content"].startswith("https://")
        FakeClient.status_seq = [{"status": "scaned"},
                                 {"status": "confirmed", "bot_token": "TK", "baseurl": "https://x",
                                  "ilink_bot_id": "b@im.bot", "ilink_user_id": "me@im.wechat"}]
        assert bot.poll_login()["status"] == "scaned"
        FakeClient.updates = [[msg(1, "me@im.wechat", "你好"),
                               msg(1, "me@im.wechat", "重复"),            # 去重
                               msg(2, "stranger@im.wechat", "hi"),         # 白名单外
                               msg(3, "me@im.wechat", "群里", group="g1")]]  # 群消息
        assert bot.poll_login()["status"] == "none"
        assert bot.status == "connected" and bot.allow_list() == ["me@im.wechat"]
        for _ in range(100):
            if FakeClient.sent and bot.state.get("updates_buf") == "B2":
                break
            time.sleep(0.05)
        bot.stop()
        assert calls == [("me@im.wechat", "你好")], calls
        assert FakeClient.sent == [("text", "me@im.wechat", "echo 你好", "CT1")], FakeClient.sent
        saved = W.WeixinBot(sf, lambda u, t: [], client_factory=FakeClient)
        assert saved.state["bot_token"] == "TK" and saved.state["updates_buf"] == "B2"
        saved.disconnect()
        assert saved.status == "disconnected" and "bot_token" not in saved.state
    finally:
        W.ILinkClient = W.ILinkClient_real


def test_send_file_item():
    class R:
        def __init__(s, code=200, js=None, h=None):
            s.status_code, s._js, s.headers, s.content, s.text = code, js, h or {}, b"1", ""

        def json(s):
            return s._js

        def raise_for_status(s):
            pass
    bodies = []

    class Sess:
        def post(s, url, json=None, data=None, headers=None, timeout=None):
            if "getuploadurl" in url:
                return R(js={"upload_full_url": "https://cdn/up?x=1"})
            if url.startswith("https://cdn"):
                return R(h={"x-encrypted-param": "DL"})
            bodies.append(json)
            return R(js={"ret": 0})
    c = W.ILinkClient(token="t", session=Sess())
    c.send_media("u@im.wechat", b"print(1)\n", "file", "CT", name="hello_world.py")
    item = bodies[-1]["msg"]["item_list"][0]
    assert item["type"] == 4
    assert item["file_item"]["file_name"] == "hello_world.py" and item["file_item"]["len"] == "9"
    assert item["file_item"]["media"]["encrypt_query_param"] == "DL"


def test_expired_session():
    d = tempfile.mkdtemp()
    sf = os.path.join(d, "weixin.json")

    class Expired(FakeClient):
        def get_updates(self, buf, timeout=40):
            return {"ret": -14}
    bot = W.WeixinBot(sf, lambda u, t: [], client_factory=Expired)
    bot.state.update(bot_token="TK")
    bot.start()
    for _ in range(40):
        if bot.status == "expired":
            break
        time.sleep(0.05)
    assert bot.status == "expired"


if __name__ == "__main__":
    test_helpers()
    test_login_and_chat()
    test_send_file_item()
    test_expired_session()
    print("weixin tests OK")


def test_compact_replies_limits_messages():
    many = [("text", f"段落{i}") for i in range(11)] + [("media", "/x.png", "image")] * 3
    out = W.compact_replies(many)
    n = sum(len(W.split_text(r[1])) if r[0] == "text" else 1 for r in out)
    assert n <= W.MAX_REPLIES, (n, out)
    assert out[0][0] == "text" and "段落10" in out[0][1], "相邻文本应合并成一条"
    big = [("text", "a" * 1700 + "\n" + "b" * 1700 * 12)]
    out = W.compact_replies(big)
    assert sum(len(W.split_text(r[1])) for r in out) <= W.MAX_REPLIES


def test_send_retries_without_token_on_prepare_failed():
    calls = []

    class C(W.ILinkClient):
        def _post(self, path, body, timeout=15):
            calls.append(body["msg"]["context_token"])
            return {"ret": -2, "errmsg": "prepare failed"} if body["msg"]["context_token"] else {}

    C(token="t").send_text("u", "hi", "tok")
    assert calls == ["tok", ""], calls


def test_typing_and_ctx_persist():
    d = tempfile.mkdtemp()
    seen = []

    class TC(FakeClient):
        def get_typing_ticket(self, user, ctx):
            return "TICKET"

        def send_typing(self, user, ticket, typing=True):
            seen.append((ticket, typing))

    FakeClient.sent = []
    bot = W.WeixinBot(os.path.join(d, "wx.json"), lambda u, t: (time.sleep(0.2), [("text", "ok")])[1],
                      client_factory=TC)
    bot.state["bot_token"] = "x"
    bot._dispatch({"message_type": 1, "message_id": 1, "from_user_id": "u1", "context_token": "ctx1",
                   "item_list": [{"type": 1, "text_item": {"text": "hi"}}]})
    bot.allow_env = ["u1"]
    bot._dispatch({"message_type": 1, "message_id": 2, "from_user_id": "u1", "context_token": "ctx2",
                   "item_list": [{"type": 1, "text_item": {"text": "hi"}}]})
    time.sleep(0.8)
    assert seen and seen[0] == ("TICKET", True) and seen[-1] == ("TICKET", False), seen
    bot2 = W.WeixinBot(os.path.join(d, "wx.json"), lambda u, t: [], client_factory=TC)
    assert bot2._ctx.get("u1") == "ctx2", "context_token 应该存盘，重启后还在"
