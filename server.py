"""Backend FastAPI cho ChatGPT Mockup Automation.

Luồng: người dùng upload template -> tạo/sửa prompt -> tích chọn template + 1
prompt -> Gen. Backend chạy ChatGPTPool (đa tab) ở nền, poll status trả về UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import auth_login
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
# Khuôn prompt: phần thiết kế của từng dòng CSV được nhét vào chỗ placeholder,
# phần RULES phía sau là cố định cho mọi prompt. Sửa được trong file, không phải
# sửa code.
PROMPT_TEMPLATE_FILE = ROOT / "data" / "prompt_template.txt"
DESIGN_PLACEHOLDER = "PASTE DESIGN PROMPT HERE"


def load_prompt_template() -> str:
    try:
        return PROMPT_TEMPLATE_FILE.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return DESIGN_PLACEHOLDER


def build_prompt(design: str) -> str:
    """Ghép phần thiết kế vào khuôn. Khuôn hỏng thì trả về phần thiết kế thôi."""
    tpl = load_prompt_template()
    if DESIGN_PLACEHOLDER not in tpl:
        log.warning("Khuôn prompt thiếu '%s' - dùng tạm phần thiết kế trần.",
                    DESIGN_PLACEHOLDER)
        return design.strip()
    return tpl.replace(DESIGN_PLACEHOLDER, design.strip())


@app.get("/api/prompt-template")
def get_prompt_template():
    return {"template": load_prompt_template(),
            "placeholder": DESIGN_PLACEHOLDER}


@app.put("/api/prompt-template")
def set_prompt_template(payload: dict):
    tpl = (payload or {}).get("template") or ""
    if DESIGN_PLACEHOLDER not in tpl:
        raise HTTPException(400, f"Khuôn phải chứa dòng '{DESIGN_PLACEHOLDER}'.")
    PROMPT_TEMPLATE_FILE.write_text(tpl, encoding="utf-8")
    return {"ok": True}


@app.post("/api/prompts/import-csv")
async def import_prompts_csv(file: UploadFile):
    """Nhập prompt hàng loạt từ CSV.

    Cần cột PROMPT (phần thiết kế). TOPIC và Style dùng để đặt tên thư mục ảnh
    theo quy tắc <ProductType>__<Topic>__<Design>, thiếu thì suy từ tên prompt.
    """
    import csv
    import io

    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        rows = list(csv.DictReader(io.StringIO(raw)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Không đọc được CSV: {e}")
    if not rows:
        raise HTTPException(400, "CSV rỗng.")

    def col(row: dict, *names: str) -> str:
        for k, v in row.items():
            if k and k.strip().lower() in names:
                return (v or "").strip()
        return ""

    state = load_state()
    prompts = state.setdefault("prompts", [])
    added, skipped = [], 0
    for row in rows:
        design_text = col(row, "prompt", "design prompt", "noi dung", "nội dung")
        if not design_text:
            skipped += 1
            continue
        topic = col(row, "topic", "chu de", "chủ đề")
        style = col(row, "style", "design", "phong cach", "phong cách")
        name = topic or col(row, "name", "ten", "tên") or f"Prompt {len(prompts) + 1}"
        prompts.append({
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "text": build_prompt(design_text),
            "topic": topic or name,
            "design": style or "Design",
        })
        added.append(name)
    if not added:
        raise HTTPException(400, "Không có dòng nào có cột PROMPT.")
    save_state(state)
    log.info("Nhập %d prompt từ CSV (bỏ qua %d dòng trống).", len(added), skipped)
    return {"added": len(added), "skipped": skipped, "names": added[:20],
            "total": len(prompts)}



@app.get("/api/prompts")
def list_prompts():
    return {"items": load_state().get("prompts", [])}


@app.post("/api/prompts")
def add_prompt(payload: dict):
    s = load_state()
    name = (payload.get("name") or "Prompt").strip()
    p = {"id": uuid.uuid4().hex[:8],
         "name": name,
         "text": (payload.get("text") or "").strip(),
         "topic": (payload.get("topic") or name).strip(),
         "design": (payload.get("design") or "Design").strip()}
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


@app.delete("/api/prompts")
def delete_all_prompts():
    st = load_state()
    n = len(st.get("prompts", []))
    st["prompts"] = []
    save_state(st)
    log.info("Đã xoá toàn bộ %d prompt.", n)
    return {"ok": True, "deleted": n}


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
    # MẶC ĐỊNH 1 TAB / 1 TÀI KHOẢN. Mở nhiều tab trên cùng một tài khoản thì nhanh
    # hơn nhưng đốt lượt của tài khoản đó nhanh tương ứng, và ChatGPT dễ chặn bớt
    # khi một tài khoản gen nhiều thứ cùng lúc. Song song vẫn diễn ra ở mức TÀI
    # KHOẢN: mỗi tài khoản một collection.
    tabs = max(1, int(cfg.get("browser", {}).get("tabs_per_account", 1)))
    cfg.setdefault("browser", {})["profiles"] = [{"name": p, "tabs": tabs}
                                                 for p in profs]
    # Tài khoản còn lại trong config = quân dự bị. Đội hình chính hết lượt thì pool
    # tự kéo mấy tài khoản này vào chạy tiếp thay vì bỏ dở collection.
    cfg["browser"]["reserves"] = [p["name"] for p in _get_profiles()
                                  if p["name"] not in profs]
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


def _product_type(template_name: str) -> str:
    """Suy loại sản phẩm từ tên file template.

    'T-shirt-4d94ad.png' -> 'T-Shirt' | 'Tote bag-522486.png' -> 'Tote-Bag'
    (hậu tố -abc123 do lúc upload sinh ra nên phải cắt đi).
    """
    stem = re.sub(r"-[0-9a-f]{6}$", "", Path(template_name).stem)
    words = []
    for part in re.split(r"[\s_]+", stem.strip()):
        piece = "-".join(w[:1].upper() + w[1:] for w in part.split("-") if w)
        if piece:
            words.append(piece)
    return "-".join(words) or "Product"


def _name_part(value: str, fallback: str) -> str:
    """Một thành phần trong tên thư mục: bỏ ký tự cấm, khoảng trắng -> gạch nối."""
    cleaned = re.sub(r'[\/*?:"<>|]', "", value or "").strip()
    cleaned = re.sub(r"\s+", "-", cleaned).strip("-. ")[:40]
    return cleaned or fallback


def _folder_has_image(folder: str) -> bool:
    """Thư mục output này đã có ảnh chưa.

    Đây là mốc để GEN LẠI MÀ KHÔNG LÀM LẠI: tên thư mục
    <ProductType>__<Topic>__<Design> đã định danh duy nhất từng ảnh cần có, nên
    chỉ cần nhìn đĩa là biết cái nào xong. Không cần sổ sách riêng, và còn sống
    sót qua cả restart server lẫn mất điện.
    """
    d = OUTPUTS / folder
    if not d.is_dir():
        return False
    return any(f.suffix.lower() in IMG_EXT and f.stat().st_size > 0
               for f in d.iterdir() if f.is_file())


def _out_folder(template_name: str, topic: str, design: str) -> str:
    """<ProductType>__<Topic>__<Design> - mỗi ảnh sản phẩm một thư mục."""
    return "__".join((_product_type(template_name),
                      _name_part(topic, "Topic"),
                      _name_part(design, "Design")))


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


async def _enqueue_on_pool(pool, cols: list[dict],
                           profiles: list[str] | None = None) -> bool:
    """Gọi enqueue trong loop của pool (asyncio.Queue không an toàn liên thread)."""
    return pool.enqueue_collections(cols, profiles)


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
        folder = _out_folder(name, prompt.get("topic") or prompt["name"],
                             prompt.get("design") or "Design")
        out_name = f"{Path(name).stem}__{jid}.png"
        job = {"id": jid, "template": str(tp), "template_name": name,
               "template_url": f"/files/templates/{name}",
               "prompt": prompt["text"], "prompt_name": prompt["name"],
               "dest": str(OUTPUTS / folder / out_name),
               "result_url": f"/files/outputs/{folder}/{out_name}",
               "folder": folder,
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
    # Mặc định BỎ QUA ảnh đã có: chạy lại sau khi hết token thì chỉ làm phần thiếu.
    skip_done = payload.get("skip_done", True) is not False
    skipped_jobs = 0
    skipped_cols = 0

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
            c_topic = item.get("topic") or c_pname
            c_design = item.get("design") or "Design"
            c_jobs = []
            for tname in c_tpls:
                tp = TEMPLATES / tname
                if not tp.exists():
                    continue
                jid = uuid.uuid4().hex[:8]
                # Mỗi ảnh nằm trong thư mục riêng: <ProductType>__<Topic>__<Design>
                folder = _out_folder(tname, c_topic, c_design)
                if skip_done and _folder_has_image(folder):
                    skipped_jobs += 1        # đã gen rồi -> khỏi làm lại
                    continue
                out_name = f"{Path(tname).stem}__{jid}.png"
                dest_path = OUTPUTS / folder / out_name
                job = {
                    "id": jid,
                    "template": str(tp),
                    "template_name": tname,
                    "template_url": f"/files/templates/{tname}",
                    "prompt": c_prompt,
                    "prompt_name": c_pname,
                    "dest": str(dest_path),
                    "result_url": f"/files/outputs/{folder}/{out_name}",
                    "folder": folder,
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
            if not c_jobs:
                skipped_cols += 1        # cả bộ đã có ảnh -> khỏi mở chat
                continue
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
                c_topic = p_obj.get("topic") or p_obj["name"]
                c_design = p_obj.get("design") or "Design"
                c_jobs = []
                for tname in templates:
                    tp = TEMPLATES / tname
                    if not tp.exists():
                        continue
                    jid = uuid.uuid4().hex[:8]
                    folder = _out_folder(tname, c_topic, c_design)
                    if skip_done and _folder_has_image(folder):
                        skipped_jobs += 1        # đã gen rồi -> khỏi làm lại
                        continue
                    out_name = f"{Path(tname).stem}__{jid}.png"
                    dest_path = OUTPUTS / folder / out_name
                    job = {
                        "id": jid,
                        "template": str(tp),
                        "template_name": tname,
                        "template_url": f"/files/templates/{tname}",
                        "prompt": p_obj["text"],
                        "prompt_name": p_obj["name"],
                        "dest": str(dest_path),
                        "result_url": f"/files/outputs/{folder}/{out_name}",
                        "folder": folder,
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
                if not c_jobs:
                    skipped_cols += 1
                    continue
                built_collections.append(col)
                COLLECTIONS[cid] = col

    if not built_collections:
        if skipped_jobs or skipped_cols:
            raise HTTPException(
                409, f"Mọi ảnh đã có sẵn ({skipped_jobs} ảnh, {skipped_cols} bộ) - "
                     "không còn gì để gen. Bỏ tick 'Bỏ qua ảnh đã có' nếu muốn làm lại.")
        raise HTTPException(400, "Không có collection hợp lệ nào được tạo.")
    if skipped_jobs or skipped_cols:
        log.info("Bỏ qua %d ảnh và %d bộ đã có sẵn.", skipped_jobs, skipped_cols)

    if is_running:
        # Chạy trong loop của pool rồi CHỜ kết quả: pool có thể từ chối (đang chạy
        # chế độ đơn lẻ, hoặc vừa kết thúc). Trước đây cứ bắn đi rồi báo "đã xếp
        # hàng" -> collection rơi vào hư không mà UI vẫn hiện chờ mãi.
        fut = asyncio.run_coroutine_threadsafe(
            _enqueue_on_pool(ACTIVE_POOL, built_collections, valid_profiles),
            ACTIVE_LOOP)
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
            "skipped_jobs": skipped_jobs,
            "skipped_collections": skipped_cols,
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
        "skipped_jobs": skipped_jobs,
        "skipped_collections": skipped_cols,
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
                    zf.write(p, arcname=f"{j.get('folder', folder_name)}/{p.name}")
                    count += 1
            filename = f"collection_{folder_name}.zip"
        elif COLLECTIONS:
            for c in COLLECTIONS.values():
                folder_name = c["name"]
                for j in c.get("jobs", []):
                    p = Path(j["dest"])
                    if p.exists():
                        zf.write(p, arcname=f"{j.get('folder', folder_name)}/{p.name}")
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


LOGIN: dict[str, dict] = {}        # name -> {stop, thread, logged_in, error, phase}
LAST_LOGIN: dict[str, dict] = {}   # name -> kết quả lần đăng nhập gần nhất


# Đo trên chính profile của tool (2026-09), thử đủ mọi tín hiệu:
#
#   tín hiệu                     | chưa đăng nhập | ĐÃ đăng nhập
#   -----------------------------|----------------|-------------
#   <textarea>, <nav> hiển thị   | CÓ             | có     -> vô dụng
#   cookie oai-did               | CÓ             | có     -> vô dụng
#   cookie *session-token*       | không          | KHÔNG  -> ChatGPT bỏ rồi
#   /api/auth/session            | WARNING_BANNER | y hệt  -> vô dụng
#   /backend-api/me              | 200, email rỗng| 200, email RỖNG -> vô dụng
#   nút "Log in"/"Sign up"       | CÒN            | mất    -> ĐÂY
#
# `/backend-api/me` gọi ngoài ngữ cảnh trang chỉ trả hồ sơ ẩn danh, nên đừng tin.
# Dấu hiệu chắc chắn là giao diện: còn nút mời đăng nhập nghĩa là chưa đăng nhập.
LOGIN_STATE_JS = """() => {
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const txts = [...document.querySelectorAll('button,a')].filter(vis)
        .map((b) => (b.innerText || '').trim().toLowerCase());
    const hasLoginBtn = txts.some(
        (t) => /^(log in|sign up|đăng nhập|đăng ký)/.test(t));
    // trang đã dựng xong chưa (đừng kết luận khi còn trắng)
    const appReady =
        !!document.querySelector('#prompt-textarea, div.ProseMirror[contenteditable="true"]')
        || document.querySelectorAll('a[href^="/c/"]').length > 0;
    return {hasLoginBtn, appReady};
}"""


def _check_logged_in(page) -> bool:
    """Đã đăng nhập THẬT hay chưa (xem bảng đo ở trên).

    LƯU Ý: phải chạy ở chế độ hiện cửa sổ. Chạy headless thì Cloudflare trả về
    giao diện khách vãng lai kể cả khi profile có phiên hợp lệ - đo thế nào cũng
    ra "chưa đăng nhập".
    """
    try:
        if "auth.openai.com" in (page.url or "").lower():
            return False
        st = page.evaluate(LOGIN_STATE_JS)
        return bool(st.get("appReady")) and not st.get("hasLoginBtn")
    except Exception:  # noqa: BLE001
        return False


LOGIN_DEBUG = ROOT / "data" / "login-debug"


def _save_login_debug(name: str, snap: dict | None) -> None:
    """Ghi cấu trúc trang lúc đăng nhập hỏng, để sửa selector khỏi phải mò.

    Chỉ có siêu dữ liệu ô nhập và chữ trên nút - `auth_login.snapshot` không đọc
    `value` nên mật khẩu/mã 2FA không thể lọt vào đây.
    """
    if not snap:
        return
    try:
        LOGIN_DEBUG.mkdir(parents=True, exist_ok=True)
        f = LOGIN_DEBUG / f"{name}-{int(time.time())}.json"
        f.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        log.warning("Đã lưu cấu trúc trang lúc hỏng vào %s - gửi file này là sửa "
                    "được selector.", f)
    except Exception as e:  # noqa: BLE001
        log.warning("Không lưu được ảnh chụp trang: %s", e)


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

            # Lấy credential ra rồi XOÁ khỏi box ngay: box còn bị API status đọc tới.
            creds = box.pop("creds", None)
            if creds:
                ok = auth_login.auto_login(page, creds, box, _check_logged_in)
                creds = None                   # bỏ tham chiếu càng sớm càng tốt
                box["logged_in"] = ok
                if ok:
                    log.info("[%s] Đăng nhập tự động thành công.", name)
                    LAST_LOGIN[name] = {"logged_in": True, "error": None,
                                        "needs_human": False, "phase": "done"}
                    ctx.close()
                    return                     # xong thì tự đóng, khỏi bấm "Xong"
                # Đánh dấu để UI biết mà hiện lỗi + nút "Tôi đã đăng nhập xong".
                # Thiếu cờ này thì worker nằm chờ stop.wait() còn UI vẫn quay -> treo.
                box["phase"] = "failed"
                log.warning("[%s] Đăng nhập tự động chưa xong (%s) - để cửa sổ mở "
                            "cho bạn làm nốt.", name,
                            box.get("error") or "đang chờ bạn xác minh")
                _save_login_debug(name, box.pop("snapshot", None))

            box["stop"].wait()                 # chờ UI bấm "Tôi đã đăng nhập xong"
            time.sleep(1.0)
            box["logged_in"] = _check_logged_in(page)
            ctx.close()
    except Exception as e:  # noqa: BLE001
        box["error"] = str(e)
    finally:
        LAST_LOGIN[name] = {"logged_in": box.get("logged_in"),
                            "error": box.get("error"),
                            "needs_human": box.get("needs_human", False),
                            "phase": box.get("phase", "done")}
        LOGIN.pop(name, None)


@app.get("/api/profiles")
def api_profiles():
    base = _profiles_dir()
    out = []
    for p in _get_profiles():
        out.append({"name": p["name"], "tabs": int(p.get("tabs", 1)),
                    "email": p.get("email"),          # chỉ email, không có mật khẩu
                    "exists": (base / p["name"]).exists(),
                    "login_open": p["name"] in LOGIN})
    total = sum(p["tabs"] for p in out)
    return {"profiles": out, "total_tabs": total}


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


# =============================================================== đăng nhập hàng loạt
BULK = {"active": False, "items": [], "started": 0}


def _next_profile_name(taken: set[str]) -> str:
    i = 1
    while f"acc{i}" in taken:
        i += 1
    return f"acc{i}"


def _bulk_worker(jobs: list[tuple[str, dict]]):
    """Đăng nhập LẦN LƯỢT từng tài khoản.

    Cố tình không chạy song song: mỗi lượt mở một cửa sổ Chrome thật, bật một loạt
    cùng lúc vừa nặng máy vừa giống hệt hành vi bị OpenAI gắn cờ.
    """
    from playwright.sync_api import sync_playwright

    for idx, (name, creds) in enumerate(jobs):
        item = BULK["items"][idx]
        item["status"] = "running"
        udir = _profiles_dir() / name
        udir.mkdir(parents=True, exist_ok=True)
        box = {"phase": "starting", "needs_human": False, "error": None}
        try:
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=str(udir), headless=False, channel="chrome",
                    viewport={"width": 1400, "height": 950},
                    args=["--disable-blink-features=AutomationControlled",
                          "--no-first-run", "--no-default-browser-check"])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                          timeout=90_000)
                ok = auth_login.auto_login(page, creds, box, _check_logged_in)
                if ok:
                    profs = _get_profiles()
                    if not any(x["name"] == name for x in profs):
                        profs.append({"name": name, "tabs": 1,
                                      "email": creds["email"]})
                        _save_profiles(profs)
                    item["status"] = "done"
                    log.info("[%s] Đăng nhập tự động thành công (%s).",
                             name, creds["email"])
                else:
                    item["status"] = "needs_human" if box.get("needs_human") else "failed"
                    item["error"] = box.get("error") or "Cần bạn xác minh thủ công"
                    _save_login_debug(name, box.pop("snapshot", None))
                ctx.close()
        except Exception as e:  # noqa: BLE001
            item["status"] = "failed"
            item["error"] = str(e)[:200]
            log.warning("[%s] Đăng nhập hàng loạt lỗi: %s", name, e)
        finally:
            creds.clear()          # xoá mật khẩu khỏi RAM ngay khi dùng xong
            jobs[idx] = (name, {})
        time.sleep(2)

    BULK["active"] = False
    done = sum(1 for x in BULK["items"] if x["status"] == "done")
    log.info("Đăng nhập hàng loạt xong: %d/%d tài khoản.", done, len(BULK["items"]))


@app.post("/api/profiles/bulk-login")
def bulk_login(payload: dict):
    """Dán nhiều dòng `email|mật khẩu|seed2fa`, mỗi dòng một tài khoản.

    Tự đặt tên profile (accN) và tự thêm vào config khi đăng nhập xong. Mật khẩu
    chỉ nằm trong RAM của lượt chạy: không ghi đĩa, không ghi log, không trả về API.
    """
    if BULK["active"]:
        raise HTTPException(409, "Đang chạy một lượt đăng nhập hàng loạt khác.")
    if LOGIN:
        raise HTTPException(409, "Đang có cửa sổ đăng nhập mở, đóng nó trước đã.")

    raw = (payload or {}).get("creds") or ""
    profs = _get_profiles()
    by_email = {(p.get("email") or "").lower(): p["name"] for p in profs}
    taken = {p["name"] for p in profs} | {d.name for d in _profiles_dir().glob("*")
                                          if d.is_dir()}
    jobs, items, seen, bad = [], [], set(), []
    for lineno, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        creds = auth_login.parse_creds(line)
        if not creds:
            bad.append(f"dòng {lineno}")
            continue
        if not auth_login.check_totp_seed(creds["totp"]):
            bad.append(f"dòng {lineno} (mã 2FA sai)")
            continue
        email = creds["email"].lower()
        if email in seen:
            continue                       # dán trùng trong cùng một lần
        seen.add(email)
        name = by_email.get(email)         # đã có tài khoản này -> đăng nhập lại
        if not name:
            name = _next_profile_name(taken)
            taken.add(name)
        jobs.append((name, creds))
        items.append({"profile": name, "email": creds["email"],
                      "status": "pending", "error": None})

    if bad and not jobs:
        raise HTTPException(400, "Không dòng nào hợp lệ: " + ", ".join(bad))
    if not jobs:
        raise HTTPException(400, "Chưa dán tài khoản nào.")

    BULK.update({"active": True, "items": items, "started": time.time()})
    threading.Thread(target=_bulk_worker, args=(jobs,), daemon=True).start()
    return {"started": len(jobs), "skipped": bad, "items": items}


@app.get("/api/profiles/bulk-login/status")
def bulk_login_status():
    return {"active": BULK["active"], "items": BULK["items"]}


@app.post("/api/profiles/{name}/login")
def profile_login(name: str, payload: dict | None = None):
    """Mở cửa sổ đăng nhập.

    Không có `creds` -> y như cũ: bạn tự đăng nhập rồi bấm "Xong".
    Có `creds` ("email | mật khẩu | seed2fa") -> tool tự điền. Chuỗi này chỉ nằm
    trong RAM của lần chạy đó: không ghi đĩa, không ghi log, không trả về qua API.
    """
    if name in LOGIN:
        return {"opened": True, "already": True}
    udir = _profiles_dir() / name
    udir.mkdir(parents=True, exist_ok=True)
    box = {"stop": threading.Event(), "logged_in": None, "error": None,
           "needs_human": False, "phase": "starting"}

    raw = (payload or {}).get("creds") or ""
    auto = False
    if raw.strip():
        creds = auth_login.parse_creds(raw)
        if not creds:
            raise HTTPException(
                400, "Sai định dạng. Cần: email | mật khẩu | mã bí mật 2FA "
                     "(phần 2FA có thể bỏ trống).")
        if not auth_login.check_totp_seed(creds["totp"]):
            raise HTTPException(400, "Mã bí mật 2FA không hợp lệ (phải là chuỗi base32).")
        box["creds"] = creds
        box["email"] = creds["email"]      # chỉ email được phép hiện lại trên UI
        auto = True

    t = threading.Thread(target=_login_worker, args=(name, udir, box), daemon=True)
    box["thread"] = t
    LAST_LOGIN.pop(name, None)
    LOGIN[name] = box
    t.start()
    return {"opened": True, "auto": auto}


@app.get("/api/profiles/{name}/login/status")
def profile_login_status(name: str):
    """UI hỏi tiến độ đăng nhập tự động. Không bao giờ trả mật khẩu/seed."""
    box = LOGIN.get(name)
    if box:
        return {"open": True, "phase": box.get("phase"),
                "needs_human": box.get("needs_human", False),
                "logged_in": box.get("logged_in"), "error": box.get("error"),
                "email": box.get("email")}
    done = LAST_LOGIN.get(name)
    if done:
        return {"open": False, **done}
    return {"open": False, "phase": None, "logged_in": None, "error": None}


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
                # KHÔNG headless: xem chú thích ở _check_logged_in
                user_data_dir=str(udir), headless=False, channel="chrome",
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
