"""Run: python tests/test_weixin_options.py ; no live account needed."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["MUSE2API_HOME"] = tempfile.mkdtemp()
os.environ["MUSE2API_KEY"] = "k"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner  # noqa: E402,F401
import state  # noqa: E402
import wx_bridge  # noqa: E402

OPTS = ["全部删除（保留当前聊天）", "只删打招呼/测试的，保留作品记录", "先归档，手动再决定"]
TEXT = "现在左侧有 45 个旁聊。\n\n想怎么处理？\n\n" + " ".join(OPTS)


def test_format_and_resolve():
    out = wx_bridge.wx_format_options(TEXT, OPTS)
    assert "1. 全部删除（保留当前聊天）" in out and "3. 先归档，手动再决定" in out
    assert out.count("先归档") == 1  # 拼成一行的原文被去掉了
    assert wx_bridge.wx_resolve_option("3", OPTS) == OPTS[2]
    assert wx_bridge.wx_resolve_option("9", OPTS) == "9"
    assert wx_bridge.wx_resolve_option("全归档", OPTS) == "全归档"


def test_chat_uses_main_chat_and_options():
    seen = {}

    def fake_stream(cookies, prompt, exp, timeout, account_id=None, main_chat=False,
                    fallback_prompt=None, on_options=None, on_prompt=None, on_turn=None):
        seen.update(prompt=prompt, main=main_chat, fb=fallback_prompt)
        yield TEXT
        on_options(OPTS)

    wx_bridge.safe_chat_stream = fake_stream
    state.store.pick_account = lambda **k: {"id": "a", "cookies": {}, "cookies_exp": None}
    out = wx_bridge._wx_chat("u", "整理旁聊")
    assert seen["prompt"] == "整理旁聊" and seen["main"] is True and "整理旁聊" in seen["fb"]
    assert out[0][0] == "text" and "2. 只删打招呼" in out[0][1]
    wx_bridge._wx_chat("u", "2")
    assert seen["prompt"] == OPTS[1]


def test_turn_replies_keep_order_files_and_code():
    turn = [{"k": "t", "v": "已创建 `hello_world.py`："},
            {"k": "f", "name": "hello_world.py", "idx": 0, "data": b'print("hi")\n'},
            {"k": "f", "name": "bad/名字?.txt", "idx": 1, "data": None},
            {"k": "t", "v": "内容：\n```python\nprint('hi')\n```"}]
    r = wx_bridge.wx_turn_replies(turn, [])
    assert [x[0] for x in r] == ["text", "media", "text", "text"], r
    assert r[1][2] == "file" and open(r[1][1], "rb").read() == b'print("hi")\n'
    assert r[1][1].endswith("hello_world.py")
    assert "没能取回" in r[2][1] and "```python\nprint('hi')\n```" in r[3][1]
    r = wx_bridge.wx_turn_replies(turn, ["A", "B"])
    assert r[-1][1].endswith("回复数字选择，或直接回复文字。") and "1. A" in r[-1][1]


def test_permission_prompt_roundtrip():
    import threading
    import time
    pushed = []
    res = {}

    def ask():
        res["idx"] = wx_bridge.wx_ask_permission("u", "允许 Muse 删除 45 个聊天？", ["允许", "拒绝"],
                                           push=lambda u, m: pushed.append(m), timeout=5)
    th = threading.Thread(target=ask)
    th.start()
    for _ in range(50):
        if pushed:
            break
        time.sleep(0.05)
    assert "1. 允许" in pushed[0] and "2. 拒绝" in pushed[0]
    assert "请回复" in wx_bridge.wx_answer_prompt("u", "abc")      # 听不懂不会解除等待
    assert wx_bridge.wx_answer_prompt("u", "1").startswith("已选择")
    th.join(3)
    assert res["idx"] == 0
    assert wx_bridge.wx_answer_prompt("u", "1") is None            # 已结束，不再拦截普通消息
    # 超时返回 None
    assert wx_bridge.wx_ask_permission("u", "x", ["允许", "拒绝"], push=lambda u, m: None, timeout=0.1) is None


def test_engine_dialog_handling():
    import engine as E
    eng = object.__new__(E.MuseEngine)
    clicked = []
    eng._poll_dialog = lambda: {"text": "允许吗", "buttons": ["允许", "拒绝"]}
    eng._click_dialog_button = lambda i: clicked.append(i) or True
    eng.handle_dialog(lambda t, b: 0)
    eng.handle_dialog(lambda t, b: None)          # 未答复 → 点拒绝
    eng.handle_dialog(lambda t, b: 9)             # 非法下标 → 点拒绝
    eng._poll_dialog = lambda: {"text": "?", "buttons": ["好的", "继续"]}
    eng.handle_dialog(lambda t, b: None)          # 没有拒绝类按钮 → 点最后一个
    assert clicked == [0, 1, 1, 1], clicked


def test_enter_chat_binding():
    import engine as E
    eng = object.__new__(E.MuseEngine)
    eng._chat_urls = {}
    state = {"url": "https://muse.ai/thread/new", "main_ok": False, "navs": []}
    eng._page_url = lambda: state["url"]
    eng._navigate_ready = lambda u: state["navs"].append(u) or state.update(url=u) or True
    eng.open_main_chat = lambda: state["main_ok"] and not state.update(url="https://muse.ai/") 
    eng.reset_thread = lambda *a, **k: None
    # 1) 找不到主要聊天：新会话，发带历史的提示词，发送后绑定 URL
    p, fresh = eng._enter_chat("a1", "新消息", "历史+新消息")
    assert (p, fresh) == ("历史+新消息", True)
    state["url"] = "https://muse.ai/thread/abc"
    eng._remember_thread("a1")
    assert eng._chat_urls == {"a1": "https://muse.ai/thread/abc"}
    # 2) 之后的消息：只发新消息，必要时导航回已绑定会话
    state["url"] = "https://muse.ai/thread/new"
    p, fresh = eng._enter_chat("a1", "第二条", "历史+第二条")
    assert (p, fresh) == ("第二条", False) and state["navs"] == ["https://muse.ai/thread/abc"]
    # 3) 重置后重新开始；主要聊天可用时记住它的 URL
    eng.forget_chat_binding()
    state["main_ok"] = True
    p, fresh = eng._enter_chat("a1", "第三条", "历史+第三条")
    assert (p, fresh) == ("第三条", False) and eng._chat_urls["a1"] == "https://muse.ai/"


def test_same_user_messages_are_serialized():
    import threading
    import time
    order = []

    def slow(user, text, emit=None, **kw):
        order.append(("start", text))
        time.sleep(0.2)
        order.append(("end", text))
        return []
    wx_bridge._wx_handle_locked = slow
    ts = [threading.Thread(target=wx_bridge._wx_handle, args=("u", t)) for t in ("a", "b")]
    for t in ts:
        t.start()
        time.sleep(0.05)
    for t in ts:
        t.join()
    assert [o[0] for o in order] == ["start", "end", "start", "end"], order


if __name__ == "__main__":
    test_format_and_resolve()
    test_chat_uses_main_chat_and_options()
    test_turn_replies_keep_order_files_and_code()
    test_permission_prompt_roundtrip()
    test_engine_dialog_handling()
    test_enter_chat_binding()
    test_same_user_messages_are_serialized()
    print("weixin options tests OK")


def test_each_bot_uses_its_own_account():
    used = []

    def fake_stream(cookies, prompt, exp, timeout, account_id=None, **kw):
        used.append((account_id, cookies.get("who")))
        yield "ok"

    wx_bridge.safe_chat_stream = fake_stream
    accs = {"a1": {"id": "a1", "cookies": {"who": "A"}, "enabled": True},
            "a2": {"id": "a2", "cookies": {"who": "B"}, "enabled": True}}
    state.store.get_account = lambda aid: accs.get(aid)
    for aid, user in (("a1", "uA"), ("a2", "uB")):
        wx_bridge._wx_chat(user, "hi", acc_id=aid)
    assert used == [("a1", "A"), ("a2", "B")], used
    accs["a2"]["enabled"] = False
    try:
        wx_bridge._wx_chat("uB", "hi", acc_id="a2")
        assert False, "停用的账号应报错"
    except RuntimeError as exc:
        assert "不可用" in str(exc)
