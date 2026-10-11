"""直连网关自检：python tools/direct_probe.py [账号标签或ID]
连接指定账号的网关，打印耗时和主要聊天最近几条记录的字段（不打印 token/cookie），
用来确认直连可用、并看清附件等字段长什么样。"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import direct  # noqa: E402
from state import store  # noqa: E402

key = sys.argv[1] if len(sys.argv) > 1 else ""
accs = [a for a in store.accounts if a.get("cookies") and (not key or key in (a["id"], a.get("label")))]
if not accs:
    sys.exit("没有找到可用账号")
acc = accs[0]
c = direct.get_client(acc["id"])
t0 = time.time()
c.warm(acc["cookies"])
print(f"连接成功 {time.time() - t0:.1f}s（账号 {acc.get('label') or acc['id']}）")
t0 = time.time()
evs = c._history(8)
print(f"读取历史 {time.time() - t0:.2f}s，共 {len(evs)} 条")
for e in evs:
    p = direct._payload(e)
    print(e.get("seq"), e.get("event_name"), "reply_to=%r" % (e.get("reply_to_message_id") or p.get("reply_to_message_id")),
          "status=%r" % p.get("status"), "字段=", sorted(p.keys()), "文本=", direct._text(e)[:40].replace("\n", " "))

print("\n=== 最近 3 条记录的完整 payload（文字截断）===")
for e in evs[-3:]:
    p = dict(direct._payload(e))
    for k in ("display_text", "content"):
        if isinstance(p.get(k), str):
            p[k] = p[k][:60]
    print(json.dumps({k: v for k, v in e.items() if k != "payload"}, ensure_ascii=False)[:400])
    print(json.dumps(p, ensure_ascii=False)[:1500])
for m in ("egress.approvals", "artifacts.list"):
    print(f"\n=== {m} ===")
    try:
        print(json.dumps(c.gw.call_json(m), ensure_ascii=False)[:2000])
    except Exception as exc:  # noqa: BLE001
        print("失败:", exc)

print("\n=== 取图测试（最近一张生成图）===")
pres = [e for e in evs if e.get("event_name") == "delta.presentation"]
if not pres:
    print("最近没有生成图")
else:
    m = direct._presentation_media(direct._payload(pres[-1]))[0]
    p = m["path"]
    print("path =", p, "| url =", (m.get("alt_url") or "")[:80])
    for meth, body in (("fs.read", {"path": p, "offset": 0, "len": 64}),
                       ("fs.read", {"path": p, "offset": 0, "len": 64, "encoding": "base64"})):
        try:
            raw = c.gw.request(meth, body=body, timeout=30)
            print(meth, body, "-> OK", len(raw), "字节，开头:", raw[:300])
        except Exception as exc:  # noqa: BLE001
            print(meth, body, "-> 失败:", str(exc)[:300])
    from curl_cffi import requests as rq
    url = m.get("alt_url")
    if url:
        for name, h in (("cookie", {"Cookie": c.cookie}),
                        ("hatch", {"Authorization": "Bearer " + c.gw.hatch_token}),
                        ("access", {"Authorization": "Bearer " + c.gw.access_token}),
                        ("hatch-query", None)):
            try:
                u = url + ("?auth_token=" + c.gw.hatch_token if h is None else "")
                r = rq.get(u, headers={**(h or {}), "Referer": "https://muse.ai/"}, impersonate="chrome", timeout=30)
                print("GET", name, "->", r.status_code, r.headers.get("content-type"), len(r.content))
            except Exception as exc:  # noqa: BLE001
                print("GET", name, "-> 失败:", str(exc)[:200])
    data, mime = c._fs_read(p)
    print("_fs_read 结果:", len(data or b""), "字节", mime, (data or b"")[:16])
