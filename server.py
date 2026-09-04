"""Backend FastAPI cho ChatGPT Mockup Automation.

Luồng: người dùng upload template -> tạo/sửa prompt -> tích chọn template + 1
prompt -> Gen. Backend chạy ChatGPTPool (đa tab) ở nền, poll status trả về UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from chatgpt_pool import ChatGPTPool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("watchfiles").setLevel(logging.WARNING)   # tắt spam "N changes detected"
log = logging.getLogger("chatgpt.server")

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "data" / "templates"
OUTPUTS = ROOT / "data" / "outputs"
STATE_FILE = ROOT / "data" / "state.json"
for d in (TEMPLATES, OUTPUTS):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ChatGPT Mockup Automation")

# ---- state (prompts) ----
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"prompts": []}


def save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


# ---- jobs & collections (in-memory) ----
JOBS: dict[str, dict] = {}               # id -> job
COLLECTIONS: dict[str, dict] = {}        # id -> collection
RUN = {
    "active": False,
    "started": 0,
    "mode": "single",                    # "single" | "bulk"
    "profile": None,                     # profile chính (cho single)
    "profiles": [],                      # danh sách profiles tham gia (cho bulk)
    "fleet": {},                         # profile -> {status, collection, collection_name, prompt_name}
    "exhausted": {},                     # profile -> lý do hết lượt
    "warnings": []                       # cảnh báo
}


# =============================================================== templates
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


@app.post("/api/templates/upload")
async def upload_templates(files: list[UploadFile]):
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in IMG_EXT:
            continue
        name = f"{Path(f.filename).stem}-{uuid.uuid4().hex[:6]}{ext}"
        (TEMPLATES / name).write_bytes(await f.read())
        saved.append(name)
    return {"saved": saved}


@app.get("/api/templates")
def list_templates():
    items = []
    for p in sorted(TEMPLATES.iterdir()):
        if p.suffix.lower() in IMG_EXT:
            items.append({"name": p.name,
                          "url": f"/files/templates/{p.name}",
                          "size_kb": round(p.stat().st_size / 1024, 1)})
    return {"items": items}


@app.delete("/api/templates")
def delete_all_templates():
    n = 0
    for p in TEMPLATES.iterdir():
        if p.suffix.lower() in IMG_EXT:
            p.unlink()
            n += 1
    return {"ok": True, "deleted": n}


@app.delete("/api/templates/{name}")
def delete_template(name: str):
    p = TEMPLATES / name
    if p.exists() and p.suffix.lower() in IMG_EXT:
        p.unlink()
    return {"ok": True}


# =============================================================== prompts
@app.get("/api/prompts")
def list_prompts():
    return {"items": load_state().get("prompts", [])}


@app.post("/api/prompts")
def add_prompt(payload: dict):
    s = load_state()
    p = {"id": uuid.uuid4().hex[:8],
         "name": (payload.get("name") or "Prompt").strip(),
         "text": (payload.get("text") or "").strip()}
    s.setdefault("prompts", []).append(p)
    save_state(s)
    return p


@app.put("/api/prompts/{pid}")
def edit_prompt(pid: str, payload: dict):
    s = load_state()
    for p in s.get("prompts", []):
        if p["id"] == pid:
            p["name"] = (payload.get("name") or p["name"]).strip()
            p["text"] = (payload.get("text") or p["text"]).strip()
            save_state(s)
            return p
    raise HTTPException(404, "Không thấy prompt")


@app.delete("/api/prompts/{pid}")
def delete_prompt(pid: str):
    s = load_state()
    s["prompts"] = [p for p in s.get("prompts", []) if p["id"] != pid]
    save_state(s)
    return {"ok": True}


# =============================================================== generate
def _prompt_by_id(pid: str) -> dict | None:
    for p in load_state().get("prompts", []):
        if p["id"] == pid:
            return p
    return None


ACTIVE_POOL: ChatGPTPool | None = None
ACTIVE_LOOP: asyncio.AbstractEventLoop | None = None


async def _run_pool(jobs: list[dict] | None = None,
                    collections: list[dict] | None = None,
                    profiles: list[str] | None = None):
    """Worker pool chạy trong background."""
    global ACTIVE_POOL, ACTIVE_LOOP
    cfg = load_cfg()
    profs = profiles or ["acc1"]
    cfg.setdefault("browser", {})["profiles"] = [{"name": p, "tabs": 1} for p in profs]
    RUN["active"] = True
    RUN["started"] = time.time()
    RUN["profiles"] = profs
    RUN["profile"] = profs[0] if profs else None
    RUN["fleet"] = {p: {"status": "starting", "collection": None, "collection_name": None} for p in profs}
    RUN["exhausted"] = {}
    RUN["warnings"] = []

    def on_fleet(p_name: str, info: dict):
        RUN["fleet"][p_name] = info

    try:
        pool = ChatGPTPool(cfg)
        ACTIVE_POOL = pool
        ACTIVE_LOOP = asyncio.get_running_loop()
        RUN["exhausted"] = pool.exhausted      # dict dùng chung, UI poll thấy ngay
        RUN["warnings"] = pool.warnings        # vd: DOM ChatGPT đổi cấu trúc
        async with pool:
            if collections:
                await pool.run_collections(collections, on_update=lambda j: None, on_fleet_update=on_fleet)
            else:
                await pool.run_batch(jobs or [], on_update=lambda j: None)
    except Exception as e:  # noqa: BLE001
        log.exception("Pool lỗi: %s", e)
        if collections:
            for c in collections:
                if c.get("status") in ("pending", "running"):
                    c["status"] = "failed"
                    c["error"] = f"pool: {e}"
                    for j in c.get("jobs", []):
                        if j.get("status") in ("pending", "running"):
                            j["status"] = "failed"
                            j["error"] = f"pool: {e}"
        if jobs:
            for j in jobs:
                if j["status"] in ("pending", "running"):
                    j["status"] = "failed"
                    j["error"] = f"pool: {e}"
    finally:
        ACTIVE_POOL = None
        ACTIVE_LOOP = None
        RUN["active"] = False


def _run_pool_thread(jobs: list[dict] | None = None,
                     collections: list[dict] | None = None,
                     profiles: list[str] | None = None):
    """Chạy pool trong THREAD riêng với ProactorEventLoop trên Windows."""
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_pool(jobs=jobs, collections=collections, profiles=profiles))
    finally:
        loop.close()


# Tên file/thư mục Windows không được trùng mấy tên thiết bị này
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


def _safe_folder_name(name: str) -> str:
    """Tên thư mục an toàn cho collection (tên này do người dùng đặt)."""
    import re
    cleaned = re.sub(r'[\/*?:"<>|]', "", name).strip()
    # strip('. ') để '..' không thành đường lùi thư mục, và Windows cấm tên kết
    # thúc bằng dấu chấm
    cleaned = cleaned.replace(" ", "_").strip(". ")[:60]
    if not cleaned or cleaned.split(".")[0].lower() in _WIN_RESERVED:
        return f"Collection_{cleaned}" if cleaned else "Collection"
    return cleaned


async def _enqueue_on_pool(pool, cols: list[dict]) -> bool:
    """Gọi enqueue trong loop của pool (asyncio.Queue không an toàn liên thread)."""
    return pool.enqueue_collections(cols)


@app.post("/api/generate")
async def generate(payload: dict):
    """Gen đơn lẻ trên 1 tài khoản (tương thích ngược)."""
    if RUN["active"]:
        raise HTTPException(409, "Đang chạy một lượt gen khác.")
    names = payload.get("templates") or []
    pid = payload.get("prompt_id")
    prompt = _prompt_by_id(pid)
    if not names:
        raise HTTPException(400, "Chưa chọn template nào.")
    if not prompt or not prompt["text"]:
        raise HTTPException(400, "Chưa chọn prompt hợp lệ.")

    profs = [p["name"] for p in _get_profiles()]
    profile = (payload.get("profile") or "").strip()
    if not profs:
        raise HTTPException(400, "Chưa có tài khoản ChatGPT nào.")
    if profile not in profs:
        raise HTTPException(400, "Chưa chọn tài khoản hợp lệ để gen.")

    JOBS.clear()
    COLLECTIONS.clear()
    RUN["mode"] = "single"
    RUN["exhausted"] = {}
    RUN["warnings"] = []
    jobs = []
    for name in names:
        tp = TEMPLATES / name
        if not tp.exists():
            continue
        jid = uuid.uuid4().hex[:8]
        out_name = f"{Path(name).stem}__{prompt['id']}-{jid}.png"
        job = {"id": jid, "template": str(tp), "template_name": name,
               "template_url": f"/files/templates/{name}",
               "prompt": prompt["text"], "prompt_name": prompt["name"],
               "dest": str(OUTPUTS / out_name),
               "result_url": f"/files/outputs/{out_name}",
               "status": "pending", "error": None}
        JOBS[jid] = job
        jobs.append(job)

    threading.Thread(target=_run_pool_thread,
                     kwargs={"jobs": jobs, "profiles": [profile]},
                     daemon=True).start()
    return {"started": len(jobs), "profile": profile, "jobs": _public_jobs()}


@app.post("/api/collections/generate")
async def generate_collections(payload: dict):
    """Tạo Collections hàng loạt từ nhiều tài khoản ChatGPT chạy song song (hỗ trợ nối hàng đợi)."""
    global ACTIVE_POOL, ACTIVE_LOOP
    is_running = (
        RUN["active"]
        and ACTIVE_POOL is not None
        and ACTIVE_LOOP is not None
        and not ACTIVE_LOOP.is_closed()
    )

    selected_profiles = payload.get("profiles") or []
    all_profs = [p["name"] for p in _get_profiles()]
    # BỎ TRÙNG: mỗi thư mục profile chỉ mở được đúng 1 Chrome
    # (launch_persistent_context khoá user-data-dir). Chọn 'acc1' hai lần là cái
    # thứ hai chết ngay hoặc tệ hơn là hỏng profile.
    valid_profiles = list(dict.fromkeys(p for p in selected_profiles if p in all_profs))

    if not is_running and not valid_profiles:
        raise HTTPException(400, "Cần chọn ít nhất 1 tài khoản ChatGPT hợp lệ.")

    raw_collections = payload.get("collections")
    templates = payload.get("templates") or []
    prompt_ids = payload.get("prompt_ids") or []
    single_pid = payload.get("prompt_id")
    if single_pid and single_pid not in prompt_ids:
        prompt_ids.append(single_pid)

    count = max(1, min(100, int(payload.get("count", 1))))

    if not raw_collections and (not prompt_ids or not templates):
        raise HTTPException(400, "Cần chọn ít nhất 1 prompt và 1 template.")

    if not is_running:
        COLLECTIONS.clear()
        JOBS.clear()
        RUN["mode"] = "bulk"
        RUN["exhausted"] = {}
        RUN["warnings"] = []

    built_collections = []

    if raw_collections:
        for item in raw_collections:
            c_name = _safe_folder_name(item.get("name") or "Collection")
            c_prompt = item.get("prompt", "")
            c_pname = item.get("prompt_name", "Prompt")
            c_tpls = item.get("templates", [])
            cid = uuid.uuid4().hex[:8]
            col_folder = OUTPUTS / c_name   # tạo khi lưu ảnh đầu tiên
            c_jobs = []
            for tname in c_tpls:
                tp = TEMPLATES / tname
                if not tp.exists():
                    continue
                jid = uuid.uuid4().hex[:8]
                out_name = f"{Path(tname).stem}__{jid}.png"
                dest_path = col_folder / out_name
                job = {
                    "id": jid,
                    "template": str(tp),
                    "template_name": tname,
                    "template_url": f"/files/templates/{tname}",
                    "prompt": c_prompt,
                    "prompt_name": c_pname,
                    "dest": str(dest_path),
                    "result_url": f"/files/outputs/{c_name}/{out_name}",
                    "status": "pending",
                    "error": None
                }
                c_jobs.append(job)
                JOBS[jid] = job

            col = {
                "id": cid,
                "name": c_name,
                "prompt": c_prompt,
                "prompt_name": c_pname,
                "jobs": c_jobs,
                "status": "pending",
                "worker": None,
                "error": None
            }
            built_collections.append(col)
            COLLECTIONS[cid] = col
    else:
        # Mỗi phiên chat là 1 Collection độc lập (chốt 1 hướng concept riêng)
        for pid in prompt_ids:
            p_obj = _prompt_by_id(pid)
            if not p_obj or not p_obj.get("text"):
                continue
            existing_count = sum(1 for c in COLLECTIONS.values()
                                 if c.get("prompt_name", "").startswith(p_obj["name"]))
            for idx in range(1, count + 1):
                col_num = existing_count + idx
                suffix = f"_Col_{col_num:02d}_{uuid.uuid4().hex[:4]}"
                c_name = _safe_folder_name(f"{p_obj['name']}{suffix}")
                cid = uuid.uuid4().hex[:8]
                col_folder = OUTPUTS / c_name   # tạo khi lưu ảnh đầu tiên
                c_jobs = []
                for tname in templates:
                    tp = TEMPLATES / tname
                    if not tp.exists():
                        continue
                    jid = uuid.uuid4().hex[:8]
                    out_name = f"{Path(tname).stem}__{jid}.png"
                    dest_path = col_folder / out_name
                    job = {
                        "id": jid,
                        "template": str(tp),
                        "template_name": tname,
                        "template_url": f"/files/templates/{tname}",
                        "prompt": p_obj["text"],
                        "prompt_name": p_obj["name"],
                        "dest": str(dest_path),
                        "result_url": f"/files/outputs/{c_name}/{out_name}",
                        "status": "pending",
                        "error": None
                    }
                    c_jobs.append(job)
                    JOBS[jid] = job

                col = {
                    "id": cid,
                    "name": c_name,
                    "prompt": p_obj["text"],
                    "prompt_name": f"{p_obj['name']} (#{col_num:02d})",
                    "jobs": c_jobs,
                    "status": "pending",
                    "worker": None,
                    "error": None
                }
                built_collections.append(col)
                COLLECTIONS[cid] = col

    if not built_collections:
        raise HTTPException(400, "Không có collection hợp lệ nào được tạo.")

    if is_running:
        # Chạy trong loop của pool rồi CHỜ kết quả: pool có thể từ chối (đang chạy
        # chế độ đơn lẻ, hoặc vừa kết thúc). Trước đây cứ bắn đi rồi báo "đã xếp
        # hàng" -> collection rơi vào hư không mà UI vẫn hiện chờ mãi.
        fut = asyncio.run_coroutine_threadsafe(
            _enqueue_on_pool(ACTIVE_POOL, built_collections), ACTIVE_LOOP)
        try:
            accepted = fut.result(timeout=10)
        except Exception as e:  # noqa: BLE001
            accepted = False
            log.warning("Nạp thêm collection lỗi: %s", e)
        if not accepted:
            for c in built_collections:          # dọn sạch, đừng để rác treo trên UI
                COLLECTIONS.pop(c["id"], None)
                for j in c.get("jobs", []):
                    JOBS.pop(j["id"], None)
            raise HTTPException(
                409, "Lượt gen đang chạy không nhận thêm collection "
                     "(đang chạy chế độ đơn lẻ hoặc vừa kết thúc). Thử lại sau.")
        log.info("Đã nối thêm %d collection vào hàng đợi của worker pool đang chạy.",
                 len(built_collections))
        return {
            "enqueued": True,
            "started_collections": len(built_collections),
            "total_collections": len(COLLECTIONS),
            "total_jobs": len(JOBS),
            "profiles": RUN.get("profiles", valid_profiles),
            "collections": _public_collections()
        }

    RUN["profiles"] = valid_profiles
    threading.Thread(
        target=_run_pool_thread,
        kwargs={"collections": built_collections, "profiles": valid_profiles},
        daemon=True
    ).start()

    return {
        "enqueued": False,
        "started_collections": len(built_collections),
        "total_collections": len(COLLECTIONS),
        "total_jobs": len(JOBS),
        "profiles": valid_profiles,
        "collections": _public_collections()
    }



def _public_jobs() -> list[dict]:
    out = []
    for j in JOBS.values():
        has = Path(j["dest"]).exists()
        out.append({
            "id": j["id"], "template_name": j["template_name"],
            "template_url": j["template_url"], "prompt_name": j["prompt_name"],
            "status": j["status"], "error": j.get("error"),
            "worker": j.get("worker"),
            "result_url": j["result_url"] if has else None,
        })
    return out


def _public_collections() -> list[dict]:
    out = []
    for c in COLLECTIONS.values():
        c_jobs = []
        for j in c.get("jobs", []):
            has = Path(j["dest"]).exists()
            c_jobs.append({
                "id": j["id"],
                "template_name": j["template_name"],
                "template_url": j["template_url"],
                "prompt_name": j["prompt_name"],
                "status": j["status"],
                "error": j.get("error"),
                "worker": j.get("worker"),
                "result_url": j["result_url"] if has else None,
            })
        done_count = sum(1 for j in c_jobs if j["status"] == "done")
        total_count = len(c_jobs)
        out.append({
            "id": c["id"],
            "name": c["name"],
            "prompt_name": c["prompt_name"],
            "prompt_text": c.get("prompt", ""),
            "status": c.get("status", "pending"),
            "worker": c.get("worker"),
            "error": c.get("error"),
            "done_count": done_count,
            "total_count": total_count,
            "jobs": c_jobs,
        })
    return out


@app.get("/api/jobs")
def get_jobs():
    return {
        "active": RUN["active"],
        "mode": RUN.get("mode", "single"),
        "profile": RUN.get("profile"),
        "profiles": RUN.get("profiles", []),
        "fleet": RUN.get("fleet", {}),
        "warnings": list(RUN.get("warnings") or []),
        "jobs": _public_jobs(),
        "collections": _public_collections(),
        "exhausted": [{"profile": k, "reason": v}
                      for k, v in (RUN.get("exhausted") or {}).items()]
    }


@app.get("/api/jobs/zip")
def download_jobs_zip(cid: str | None = None):
    import io
    import zipfile
    from fastapi.responses import StreamingResponse
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if cid and cid in COLLECTIONS:
            c = COLLECTIONS[cid]
            folder_name = c["name"]
            for j in c.get("jobs", []):
                p = Path(j["dest"])
                if p.exists():
                    zf.write(p, arcname=f"{folder_name}/{p.name}")
                    count += 1
            filename = f"collection_{folder_name}.zip"
        elif COLLECTIONS:
            for c in COLLECTIONS.values():
                folder_name = c["name"]
                for j in c.get("jobs", []):
                    p = Path(j["dest"])
                    if p.exists():
                        zf.write(p, arcname=f"{folder_name}/{p.name}")
                        count += 1
            filename = f"campaign_collections_{int(time.time())}.zip"
        else:
            for j in JOBS.values():
                p = Path(j["dest"])
                if p.exists():
                    zf.write(p, arcname=p.name)
                    count += 1
            if count == 0:
                for p in OUTPUTS.glob("*.png"):
                    zf.write(p, arcname=p.name)
                    count += 1
            filename = f"mockups_{int(time.time())}.zip"

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.delete("/api/jobs")
def clear_jobs():
    JOBS.clear()
    COLLECTIONS.clear()
    return {"ok": True}



# =============================================================== profiles
def _profiles_dir() -> Path:
    return Path(load_cfg().get("browser", {}).get(
        "profiles_dir", "./.chrome-profiles")).resolve()


def _get_profiles() -> list[dict]:
    return load_cfg().get("browser", {}).get("profiles", []) or []


def _save_profiles(profs: list[dict]) -> None:
    cfg = load_cfg()
    cfg.setdefault("browser", {})["profiles"] = profs
    (ROOT / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


LOGIN: dict[str, dict] = {}   # name -> {stop, thread, logged_in, error}


def _check_logged_in(page) -> bool:
    """Kiểm tra toàn diện xem trang ChatGPT đã đăng nhập thành công hay chưa."""
    try:
        url = page.url.lower()
        if "auth.openai.com" in url or "auth0" in url:
            return False
        
        # 1. Kiểm tra các selector giao diện ChatGPT đã đăng nhập
        selectors = [
            "#prompt-textarea",
            "div[contenteditable='true']",
            "textarea[data-id='root']",
            "textarea",
            "button[data-testid='send-button']",
            "button[data-testid='profile-button']",
            "button[aria-label*='User']",
            "button[aria-label*='Hồ sơ']",
            "nav"
        ]
        for sel in selectors:
            try:
                if page.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass
        
        # 2. Kiểm tra cookies phiên đăng nhập
        try:
            cookies = page.context.cookies()
            for c in cookies:
                cname = c.get("name", "").lower()
                if "session-token" in cname or "oai-did" in cname or "__secure-next-auth" in cname:
                    if c.get("value"):
                        return True
        except Exception:
            pass

        # 3. Fallback kiểm tra URL chính thức
        if "chatgpt.com" in url and "/login" not in url and "/auth" not in url:
            return True
    except Exception:
        pass
    return False


def _login_worker(name: str, udir: Path, box: dict):
    """Mở Chrome (sync Playwright) cho 1 profile để người dùng đăng nhập ChatGPT.
    Giữ cửa sổ mở tới khi UI bấm 'Xong' (set stop), rồi kiểm tra + đóng."""
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(udir), headless=False, channel="chrome",
                viewport={"width": 1400, "height": 950},
                args=["--disable-blink-features=AutomationControlled",
                      "--no-first-run", "--no-default-browser-check"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=90_000)
            box["stop"].wait()                 # chờ UI bấm "Tôi đã đăng nhập xong"
            time.sleep(1.0)
            box["logged_in"] = _check_logged_in(page)
            ctx.close()
    except Exception as e:  # noqa: BLE001
        box["error"] = str(e)
    finally:
        LOGIN.pop(name, None)


@app.get("/api/profiles")
def api_profiles():
    base = _profiles_dir()
    out = []
    for p in _get_profiles():
        out.append({"name": p["name"], "tabs": int(p.get("tabs", 1)),
                    "exists": (base / p["name"]).exists(),
                    "login_open": p["name"] in LOGIN})
    total = sum(p["tabs"] for p in out)
    return {"profiles": out, "total_tabs": total}


@app.post("/api/profiles")
def add_profile(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Thiếu tên profile")
    profs = _get_profiles()
    if any(p["name"] == name for p in profs):
        raise HTTPException(409, "Tên profile đã tồn tại")
    profs.append({"name": name, "tabs": 1})   # 1 lượt gen = 1 tab, giữ field cho tương thích
    _save_profiles(profs)
    return {"ok": True}


def _profile_dir_of(name: str) -> Path | None:
    """Thư mục user-data của profile, chỉ trả về khi nằm trong profiles_dir."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    base = _profiles_dir()
    d = (base / name).resolve()
    try:
        d.relative_to(base)
    except ValueError:
        return None
    return d


def _rm_profile_dir(d: Path) -> bool:
    """Xoá thư mục profile. Chrome trên Windows hay giữ lock -> thử vài lần."""
    import shutil
    import stat

    def _on_exc(func, path, exc):
        """File read-only (Chrome hay đặt) -> bỏ cờ rồi thử xoá lại."""
        try:
            Path(path).chmod(stat.S_IWRITE)
            func(path)
        except Exception:  # noqa: BLE001
            pass

    kw = ({"onexc": _on_exc} if sys.version_info >= (3, 12)
          else {"onerror": lambda f, pth, ei: _on_exc(f, pth, ei[1])})

    for i in range(3):
        if not d.exists():
            return True
        shutil.rmtree(d, **kw)
        if not d.exists():
            return True
        time.sleep(0.7 * (i + 1))
    return not d.exists()


@app.delete("/api/profiles/{name}")
def delete_profile(name: str):
    # đang mở cửa sổ đăng nhập cho profile này -> đóng trước cho hết lock
    box = LOGIN.get(name)
    if box:
        box["stop"].set()
        box["thread"].join(timeout=15)
        LOGIN.pop(name, None)

    _save_profiles([p for p in _get_profiles() if p["name"] != name])

    d = _profile_dir_of(name)
    if d is None:
        raise HTTPException(400, "Tên profile không hợp lệ")
    removed = _rm_profile_dir(d)
    if not removed:
        log.warning("Không xoá được thư mục profile: %s", d)
    return {"ok": True, "dir_removed": removed}


@app.post("/api/profiles/{name}/login")
def profile_login(name: str):
    if name in LOGIN:
        return {"opened": True, "already": True}
    udir = _profiles_dir() / name
    udir.mkdir(parents=True, exist_ok=True)
    box = {"stop": threading.Event(), "logged_in": None, "error": None}
    t = threading.Thread(target=_login_worker, args=(name, udir, box), daemon=True)
    box["thread"] = t
    LOGIN[name] = box
    t.start()
    return {"opened": True}


@app.post("/api/profiles/{name}/login/close")
def profile_login_close(name: str):
    box = LOGIN.get(name)
    if not box:
        return {"logged_in": None, "closed": True}
    box["stop"].set()
    box["thread"].join(timeout=15)
    return {"logged_in": box.get("logged_in"), "error": box.get("error")}


@app.post("/api/profiles/{name}/check")
def check_profile_login(name: str):
    """Kiểm tra nhanh xem profile này đã đăng nhập ChatGPT hay chưa."""
    from playwright.sync_api import sync_playwright
    udir = _profiles_dir() / name
    if not udir.exists():
        return {"logged_in": False, "exists": False}
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(udir), headless=True, channel="chrome",
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled",
                      "--no-first-run", "--no-default-browser-check"]
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=25_000)
            time.sleep(1.0)
            logged_in = _check_logged_in(page)
            ctx.close()
            return {"logged_in": logged_in, "exists": True}
    except Exception as e:
        return {"logged_in": False, "error": str(e), "exists": True}



# =============================================================== config (read)
@app.get("/api/config")
def get_config():
    return load_cfg()


# =============================================================== static / files
@app.get("/files/templates/{name}")
def serve_template(name: str):
    p = TEMPLATES / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p))


@app.get("/files/outputs/{name:path}")
def serve_output(name: str):
    p = (OUTPUTS / name).resolve()
    try:
        p.relative_to(OUTPUTS.resolve())
    except ValueError:
        raise HTTPException(403)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p))


app.mount("/", StaticFiles(directory=str(ROOT / "static"), html=True), name="static")


if __name__ == "__main__":
    import os

    import uvicorn

    # MẶC ĐỊNH TẮT reload. Bật reload thì watchfiles soi cả .chrome-profiles/ (Chrome
    # ghi file liên tục -> log rác + tốn CPU/IO trong lúc đang gen), và nguy hiểm hơn:
    # lỡ sửa 1 file .py giữa lượt gen là server restart, giết thread đang chạy và bỏ
    # lại cửa sổ Chrome mồ côi. Muốn dev thì: set DEV_RELOAD=1 rồi chạy.
    if os.environ.get("DEV_RELOAD") == "1":
        uvicorn.run("server:app", host="127.0.0.1", port=8010, reload=True,
                    reload_dirs=[str(ROOT)], reload_includes=["*.py"],
                    reload_excludes=[".chrome-profiles/*", "data/*", "__pycache__/*"])
    else:
        uvicorn.run(app, host="127.0.0.1", port=8010)
