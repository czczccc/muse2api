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
    script = []          # 每次 history 调用依次返回的事件列表

    def __init__(self, cookie):
        self.cookie = cookie
        self.n = 0

    def call_json(self, method, body=None, **kw):
        assert method == "chat.history"
        if body.get("limit") == 1:
            return {"chat_events": [ev(10, "旧消息")]}
        i = min(self.n, len(FakeGW.script) - 1)
        self.n += 1
        return {"chat_events": [ev(10, "旧消息")] + FakeGW.script[i]}

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
    assert out == got == ["你好呀", "第二段"], out
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


def test_wx_chat_direct_emits_early_and_falls_back_for_media():
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
