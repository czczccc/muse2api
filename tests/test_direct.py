"""直连网关：用假的 Gateway 验证发送、轮询、分条回复和回退逻辑。"""
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ["MUSE2API_HOME"] = tempfile.mkdtemp()
os.environ["MUSE2API_KEY"] = "k"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import direct  # noqa: E402
import wx_bridge  # noqa: E402
from state import store  # noqa: E402


def ev(seq, text, status="completed", reply_to="", name="message.assistant"):
    return {"seq": seq, "event_name": name, "reply_to_message_id": reply_to,
            "payload": {"display_text": text, "status": status}}


class FakeGW:
    opened = []
    reads = []
    approvals = []
    decided = []
    script = []          # 每次 history 调用依次返回的事件列表

    def __init__(self, cookie):
        self.cookie = cookie
        self.n = 0

    def call_json(self, method, body=None, **kw):
        if method == "fs.stat":
            return {"size": 7}
        if method == "egress.approvals":
            return FakeGW.approvals
        if method == "egress.approval.decide":
            FakeGW.decided.append((kw.get("path_params"), body))
            FakeGW.approvals = []
            return {}
        assert method == "chat.history"
        if body.get("limit") == 1:
            return {"chat_events": [ev(10, "旧消息")]}
        i = min(self.n, len(FakeGW.script) - 1)
        self.n += 1
        return {"chat_events": [ev(10, "旧消息")] + FakeGW.script[i]}

    def request(self, method, body=None, **kw):
        assert method == "fs.read" and "len" in body
        FakeGW.reads.append(body["path"])
        return b'{"ok": true, "result": {"data": "UE5HREFUQQ==", "eof": true}}'

    def _open(self, method, body=None, **kw):
        FakeGW.opened.append((method, body))

    def close(self):
        pass


def setup_module(_m):
    fake = types.ModuleType("mgateway.client")
    fake.Gateway = FakeGW
    sys.modules["mgateway.client"] = fake
    direct.POLL, direct.QUIET = 0.01, 0.05
    os.environ["MUSE2API_DIRECT"] = "1"


def test_turn_delivers_each_reply_once_and_skips_noise():
    FakeGW.opened = []
    FakeGW.script = [
        [ev(11, "半截", status="")],                                   # 还在写：不算
        [ev(11, "你好呀"), ev(12, "后台推送", reply_to="self"), ev(13, "第二段")],
        [ev(11, "你好呀"), ev(13, "第二段")],
    ]
    c = direct.DirectClient("acc1")
    got = []
    out = c.turn({"hatch_sess": "x"}, "你好", 5, got.append)
    assert out == got == [("text", "你好呀"), ("text", "第二段")], out
    assert FakeGW.opened[0][0] == "chat.stream"
    assert FakeGW.opened[0][1]["items"] == [{"type": "text", "text": "你好"}]


def test_connect_failure_is_not_sent():
    class Boom:
        def __init__(self, cookie):
            raise RuntimeError("auth/check -> 401")
    sys.modules["mgateway.client"].Gateway = Boom
    try:
        direct.DirectClient("acc2").turn({"a": "b"}, "hi", 1, lambda t: None)
        assert False
    except direct.DirectNotSent as exc:
        assert "401" in str(exc)
    finally:
        sys.modules["mgateway.client"].Gateway = FakeGW


def test_wx_chat_direct_emits_early():
    FakeGW.script = [[ev(11, "第一段"), ev(12, "第二段")]]
    store.get_account = lambda aid: {"id": aid, "cookies": {"hatch_sess": "x"}, "enabled": True}
    sent = []
    r = wx_bridge._wx_chat("u", "你好", emit=lambda rs: sent.extend(rs) or True, acc_id="a1")
    assert [x[1] for x in sent] == ["第一段", "第二段"] and r == [], (sent, r)
    # 预算用完：剩下的合并到最终回复
    r = wx_bridge._wx_chat("u", "你好", emit=lambda rs: False, acc_id="a1")
    assert [x[1] for x in r] == ["第一段", "第二段"], r
    # 要图的请求不走直连
    assert direct.NEEDS_BROWSER.search("帮我画一只猫") and not direct.NEEDS_BROWSER.search("你好")


def test_approval_is_forwarded_and_decided():
    FakeGW.script = [[ev(11, "好了")]]
    FakeGW.approvals = {"pending": [{"approval_id": "ap1", "host": "1.1.1.1", "port": 22,
                                     "decision_options": ["allow_once", "allow_always", "deny"]}]}
    FakeGW.decided = []
    asked = []
    c = direct.DirectClient("acc3")
    c.turn({"a": "b"}, "ssh", 5, lambda t: None,
           on_prompt=lambda t, b: (asked.append((t, b)), 1)[1])
    assert "1.1.1.1" in asked[0][0] and asked[0][1][1] == "总是允许此站点"
    assert FakeGW.decided == [({"approval_id": "ap1"}, {"decision": "allow_always"})]


def test_find_media():
    p = {"display_text": "图", "attachments": [{"url": "https://x/y.png", "mime_type": "image/png", "name": "y.png"}]}
    assert direct._find_media(p) == [{"url": "https://x/y.png", "artifact_id": None, "path": None, "name": "y.png", "mime": "image/png"}]


def test_presentation_image_is_read_via_fs():
    pres = {"seq": 12, "event_name": "delta.presentation",
            "payload": {"kind": "image", "state": "ready", "display_text": "咖啡馆",
                        "image_path": "workspace/user/media_library/image/b8/abc.png"}}
    FakeGW.script = [[ev(11, "画好了"), pres]]
    FakeGW.approvals, FakeGW.reads = [], []
    got = []
    direct.DirectClient("acc4").turn({"a": "b"}, "画", 5, got.append)
    assert got[0] == ("text", "画好了")
    assert got[1] == ("media", b"PNGDATA", "image", "abc.png"), got
    assert FakeGW.reads == ["workspace/user/media_library/image/b8/abc.png"]


def test_presentation_media_uses_data_images():
    p = {"image_path": "workspace/a.webp", "data": {"images": [{"mime": "image/webp", "missing": False,
         "path": "sandbox://workspace/imagine_media/x.webp",
         "variants": {"original": "https://vm.metaaivm.com/media/raw/workspace/imagine_media/x.webp"}}]}}
    m = direct._presentation_media(p)
    assert m[0]["path"] == "workspace/imagine_media/x.webp" and m[0]["alt_url"].endswith("x.webp")
    assert m[0]["mime"] == "image/webp"
