"""基础接口、生图、生视频、媒体文件。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from engine import MuseAuthError, MuseGenerationError
from fastapi import Header
from prompts import build_image_prompt, build_video_prompt
from runner import _run_generation
from schemas import ImageRequest, MODELS, VideoRequest
from state import CFG, IMAGE_TASK_LOCK, auth, engine, media_url, store

router = APIRouter()

# ------------------------- 基础接口 -------------------------
@router.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@router.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}


@router.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": MODELS}


# ------------------------- 生图 -------------------------
def _image_response(req: ImageRequest, res: dict) -> dict:
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"]),
            "size": req.size or "auto", "kind": res["kind"], "bytes": res["size"]}
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        with open(fpath, "rb") as f:
            item["b64_json"] = base64.b64encode(f.read()).decode()
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _queue_image(req: ImageRequest, prompt: str, reference_image: str | None,
                 idempotency_key: str | None = None):
    """Opt-in polling avoids reverse-proxy timeouts; no generation is repeated by polling."""
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "reference": reference_image},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with IMAGE_TASK_LOCK:
        tasks = list(store.tasks.values())
        if key_hash:
            for existing in tasks:
                if existing.get("image_request_key") == key_hash:
                    if existing.get("image_request_hash") != request_hash:
                        raise HTTPException(409, "Idempotency-Key reused with different request")
                    return JSONResponse(status_code=202, content={
                        "id": existing["id"], "task_id": existing["id"],
                        "object": "image.task", "status": existing["status"],
                        "progress": existing.get("progress", 0),
                        "created_at": existing["created_at"]})
        # ponytail: one Chromium worker; cap admission instead of adding a broker.
        if sum(t.get("kind") == "image" and t.get("status") in ("queued", "processing")
               for t in tasks) >= 8:
            raise HTTPException(429, "Image queue is full; retry later")
        task = store.create_task("image", req.prompt)
        tid = task["id"]
        store.update_task(tid, api_prompt=prompt, size=req.size,
                          response_format=req.response_format, progress=0,
                          image_request_key=key_hash, image_request_hash=request_hash)

    def worker():
        timing = {"t0": time.time()}
        def _on_acquired():
            timing["t0"] = time.time()
            store.update_task(tid, status="processing", progress=5)
        try:
            res, acc_id = _run_generation(
                prompt, "image", req.timeout or CFG.image_timeout,
                reference_image=reference_image,
                on_progress=lambda p: store.update_task(tid, progress=p),
                on_acquired=_on_acquired)
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - timing["t0"], 1),
                              url=media_url(res["filename"]),
                              result={**{k: res[k] for k in ("filename", "size", "kind")},
                                      "url": media_url(res["filename"])})
        except Exception as exc:  # noqa: BLE001
            store.update_task(tid, status="failed", error=str(exc),
                              elapsed=round(time.time() - timing["t0"], 1))

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse(status_code=202, content={
        "id": tid, "task_id": tid, "object": "image.task", "status": "queued",
        "progress": 0, "created_at": task["created_at"]})


@router.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: str | None = Header(default=None), _=Depends(auth)):
    ref_img = req.reference_image or req.image
    if isinstance(ref_img, dict):
        ref_img = ref_img.get("url") or ref_img.get("b64_json")
    if not ref_img and req.images:
        first = req.images[0]
        ref_img = first.get("image_url") or first.get("url") if isinstance(first, dict) else first
    return _queue_image(req, build_image_prompt(req), ref_img, idempotency_key)


@router.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "image task 不存在")
    out = dict(task)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"]))
    return out


@router.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(auth)):
    ref_img = req.reference_image or req.image
    if isinstance(ref_img, dict):
        ref_img = ref_img.get("url") or ref_img.get("b64_json")
    if not ref_img and req.images and isinstance(req.images, list):
        first = req.images[0]
        ref_img = first.get("image_url") or first.get("url") if isinstance(first, dict) else first

    prompt = build_image_prompt(req)
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_img)
    try:
        res, _acc = await asyncio.to_thread(_run_generation, prompt, "image", timeout, reference_image=ref_img)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _image_response(req, res)


@router.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(auth)):
    """OpenAI 兼容的图生图/图像编辑接口，兼容 multipart/form-data 与 application/json。"""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    ref_image_data = None
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form()
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        img_field = form.get("image")
        if img_field and hasattr(img_field, "read"):
            content = await img_field.read()
            ref_mime = getattr(img_field, "content_type", "image/png") or "image/png"
            ref_image_data = f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}"
        elif isinstance(img_field, str):
            ref_image_data = img_field
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        img_val = body.get("image")
        if isinstance(img_val, dict):
            ref_image_data = img_val.get("url") or img_val.get("b64_json")
        elif isinstance(img_val, str):
            ref_image_data = img_val
        if not ref_image_data and body.get("images") and isinstance(body.get("images"), list):
            first = body["images"][0]
            if isinstance(first, dict):
                ref_image_data = first.get("image_url") or first.get("url")
            elif isinstance(first, str):
                ref_image_data = first
        if not ref_image_data:
            ref_image_data = body.get("reference_image") or body.get("image_url")

    if not prompt:
        prompt = "参考此图片并进行生图创作"

    req_obj = ImageRequest(
        prompt=prompt,
        model=model,
        size=size,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        reference_image=ref_image_data,
        **{"async": async_mode}
    )
    full_prompt = build_image_prompt(req_obj)
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data)
    try:
        res, _acc = await asyncio.to_thread(_run_generation, full_prompt, "image", gen_timeout, reference_image=ref_image_data)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- 生视频（异步任务） -------------------------
@router.post("/v1/videos")
@router.post("/v1/videos/generations")
async def create_video(req: VideoRequest, _=Depends(auth)):
    ref_img = None
    if req.reference_image:
        ref_img = req.reference_image
    elif req.image_url:
        ref_img = req.image_url if isinstance(req.image_url, str) else (req.image_url.get("url") if isinstance(req.image_url, dict) else None)
    elif req.image:
        ref_img = req.image.get("url") if isinstance(req.image, dict) else req.image

    prompt = build_video_prompt(req)
    timeout = req.timeout or CFG.video_timeout
    task = store.create_task("video", req.prompt)
    store.update_task(task["id"], api_prompt=prompt)

    store.update_task(task["id"], progress=10)

    def worker():
        timing = {"t0": time.time()}
        def _on_acquired():
            timing["t0"] = time.time()
            store.update_task(task["id"], status="processing", progress=15)
        try:
            def prog_cb(p):
                store.update_task(task["id"], progress=p)
            res, acc_id = _run_generation(prompt, "video", timeout, on_progress=prog_cb,
                                          reference_image=ref_img, on_acquired=_on_acquired)
            vurl = media_url(res["filename"])
            store.update_task(task["id"], status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - timing["t0"], 1),
                              url=vurl,
                              video={"url": vurl},
                              result={"url": vurl,
                                      "filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"]})
        except Exception as exc:  # noqa: BLE001
            store.update_task(task["id"], status="failed",
                              elapsed=round(time.time() - timing["t0"], 1), error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"id": task["id"], "task_id": task["id"], "object": "video.task", "status": "queued",
            "progress": 10, "created_at": task["created_at"]}


@router.get("/v1/videos/{task_id}")
@router.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "task 不存在")
    out = dict(t)
    status = out.get("status")
    if status in ("succeeded", "success", "done"):
        out["status"] = "completed"
    if out.get("status") == "completed":
        out["progress"] = 100
    elif out.get("status") == "processing":
        elapsed = time.time() - out.get("created_at", time.time())
        calc_prog = min(92, int(20 + elapsed * 1.1))
        out["progress"] = max(out.get("progress", 0) or 0, calc_prog)
    vurl = out.get("url")
    if not vurl and isinstance(out.get("result"), dict):
        vurl = out["result"].get("url")
    if vurl:
        out["url"] = vurl
        if "video" not in out:
            out["video"] = {"url": vurl}
    return out




@router.get("/v1/media/{name}")
def get_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    return FileResponse(p)


