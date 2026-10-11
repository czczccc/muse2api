"""Run: python tests/test_chat_binding.py ; no live account needed."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["MUSE2API_HOME"] = tempfile.mkdtemp()
os.environ["MUSE2API_KEY"] = "k"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as engine_mod  # noqa: E402
import wx_bridge  # noqa: E402


def test_close_page_keeps_chat_binding():
    e = object.__new__(engine_mod.MuseEngine)
    e.page, e.current_context_id, e.current_acc_id, e.browser = None, None, "a1", None
    e._contexts_lock = __import__("threading").Lock()
    e._active_contexts = set()
    e._chat_urls = {"a1": "https://muse.ai/thread/abc"}
    e._close_page()
    assert e._chat_urls == {"a1": "https://muse.ai/thread/abc"}, "每次请求结束关页面不能丢掉会话绑定"
    e.forget_chat_binding()
    assert e._chat_urls == {}


def test_turn_media_is_forwarded():
    turn = [{"k": "t", "v": "随机到一张雪山温泉"},
            {"k": "m", "kind": "image", "src": "blob:x", "data": b"\x89PNG", "mime": "image/png"},
            {"k": "t", "v": "想要其他风格告诉我"},
            {"k": "m", "kind": "image", "src": "blob:y", "data": None}]
    r = wx_bridge.wx_turn_replies(turn, [])
    kinds = [x[0] for x in r]
    assert kinds == ["text", "media", "text", "text"], kinds
    assert r[1][2] == "image" and os.path.isfile(r[1][1])
    assert "没能取回" in r[3][1]


def test_new_turn_items_ignores_history():
    e = object.__new__(engine_mod.MuseEngine)
    hist = [{"k": "t", "v": "旧回复"}, {"k": "m", "kind": "image", "src": "blob:old"}]
    page = {"items": list(hist)}
    e._poll_turn = lambda: list(page["items"])
    base = e._turn_baseline()
    # 旧内容的下标发生变化（按下标切片会把旧图再发一遍），只应取回基线之后新增的内容
    page["items"] = [{"k": "t", "v": "新回复"}] + hist + [{"k": "m", "kind": "image", "src": "blob:new"}]
    got = [i.get("v") or i.get("src") for i in e._new_turn_items(base)]
    assert got == ["新回复", "blob:new"], got


def test_new_turn_items_after_last_user_bubble():
    e = object.__new__(engine_mod.MuseEngine)
    e._poll_turn = lambda: [
        {"k": "u"}, {"k": "t", "v": "旧回复"}, {"k": "m", "kind": "image", "src": "blob:old"},
        {"k": "u"}, {"k": "t", "v": "新回复"}, {"k": "m", "kind": "image", "src": "blob:new"}]
    got = [i.get("v") or i.get("src") for i in e._new_turn_items(e._turn_baseline())]
    assert got == ["新回复", "blob:new"], got


def test_warm_runner_keeps_one_thread_and_closes_on_error():
    import threading
    import time
    seen, closed = [], []

    class FakeEngine:
        def chat_stream(self, cookies, prompt, expires, timeout, account_id=None, **kw):
            seen.append(threading.current_thread().name)
            if prompt == "boom":
                raise RuntimeError("x")
            yield "a"
            yield "b"

        def _close_page(self):
            closed.append(threading.current_thread().name)

    r = engine_mod._ChatRunner(FakeEngine(), idle=0.2)
    assert list(r.run({}, "hi", None, 5, account_id="a")) == ["a", "b"]
    assert list(r.run({}, "hi", None, 5, account_id="a")) == ["a", "b"]
    assert len(set(seen)) == 1 and seen[0] == "muse-chat-runner", "两条消息必须在同一个常驻线程里，页面才能复用"
    assert closed == [], "正常结束不能关页面"
    try:
        list(r.run({}, "boom", None, 5, account_id="a"))
        raise AssertionError("应抛出异常")
    except RuntimeError:
        pass
    assert len(closed) == 1, "出错后应关掉可能已坏的页面"
    time.sleep(0.5)
    assert len(closed) >= 2, "空闲超时后应释放页面"


if __name__ == "__main__":
    test_close_page_keeps_chat_binding()
    test_turn_media_is_forwarded()
    test_new_turn_items_ignores_history()
    test_new_turn_items_after_last_user_bubble()
    test_warm_runner_keeps_one_thread_and_closes_on_error()
    print("PASS")
