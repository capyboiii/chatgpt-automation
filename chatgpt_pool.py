"""Điều khiển ChatGPT web để gen bộ ảnh mockup ĐỒNG NHẤT.

Một lượt gen = 1 tài khoản, 1 tab, MỘT phiên chat duy nhất cho toàn bộ ảnh:

    lô 1: [ảnh 1..n] + prompt gốc          -> ChatGPT chốt hướng design
    lô 2: [ảnh n+1..] + followup_prompt    -> vẫn trong chat đó, "giữ y design trên"

Trước đây mỗi ảnh chạy một chat riêng trên nhiều tab để nhanh, nhưng mỗi chat lại
cho ra một hướng design khác nhau -> cả bộ mockup không đồng nhất. Đổi lại: chậm
hơn, bù lại cả bộ cùng một concept. Chia lô chỉ vì ChatGPT giới hạn số ảnh đính
kèm mỗi tin nhắn (`run.batch_size`).

Ảnh trả về được gán cho template theo ĐÚNG THỨ TỰ upload trong lô.

KHÔNG dùng MonkeyX: ChatGPT không chặn automation nên gõ/bấm thẳng bằng Playwright.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable

try:
    from PIL import Image           # để loại thumbnail bằng kích thước THẬT
except Exception:                   # noqa: BLE001 - thiếu Pillow thì lọc theo dung lượng
    Image = None

from playwright.async_api import (
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

log = logging.getLogger("chatgpt.pool")

URL = "https://chatgpt.com/"

SEL_PROMPT = [
    "#prompt-textarea",
    'div.ProseMirror[contenteditable="true"]',
    'textarea[data-id]',
]
SEL_SEND = [
    'button[data-testid="send-button"]',
    'button[aria-label*="Send" i]',
]
SEL_STOP = [
    'button[data-testid="stop-button"]',
    'button[aria-label*="Stop" i]',
]
SEL_FILE_INPUT = 'input[type="file"]'
SEL_NEW_CHAT = [
    'a[data-testid="create-new-chat-button"]',
    'button[data-testid="create-new-chat-button"]',
    'nav a[href="/"]',
]

# 1 lần evaluate lấy CẢ trạng thái đang gen (nút Stop) LẪN danh sách ảnh. Gộp lại
# vì mỗi round-trip Playwright tốn thời gian; nhiều tab cùng poll thì khoản tiết
# kiệm này cộng dồn rất nhanh.
POLL_JS = """() => {
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    let busy = false;
    for (const s of ['button[data-testid="stop-button"]',
                     'button[aria-label*="Stop" i]']) {
        if (vis(document.querySelector(s))) { busy = true; break; }
    }
    // CHỈ lấy ảnh trong lượt trả lời của ChatGPT. Quét cả trang thì ảnh template
    // mình vừa gửi (trong tin nhắn của user + thumbnail ở composer) cũng bị tính
    // là "ảnh mới" -> đếm đủ số quá sớm và tải nhầm chính ảnh gốc.
    const asst = Array.from(
        document.querySelectorAll('[data-message-author-role="assistant"]'));
    const roots = asst.length ? asst : [document.querySelector('main') || document.body];
    const imgs = [];
    const seen = new Set();
    for (const root of roots) {
        for (const im of root.querySelectorAll('img')) {
            if (im.closest('form')) continue;                  // thumbnail composer
            if (!asst.length &&                                 // fallback: bỏ ảnh
                im.closest('[data-message-author-role="user"]')) continue;  // của user
            const src = im.currentSrc || im.src || '';
            if (!src || seen.has(src)) continue;
            seen.add(src);
            const r = im.getBoundingClientRect();
            imgs.push({src, w: Math.round(r.width), h: Math.round(r.height)});
        }
    }
    return {busy, imgs, roles: asst.length};
}"""


# Thứ tự ảnh theo DOM trong lượt trả lời. Dùng để xếp ảnh đúng thứ tự ChatGPT bày
# ra: response tải song song nên thứ tự về không đáng tin, còn DOM thì ổn định.
ORDER_JS = """() => {
    const asst = Array.from(
        document.querySelectorAll('[data-message-author-role="assistant"]'));
    const roots = asst.length ? asst : [document.querySelector('main') || document.body];
    const keys = [];
    const seen = new Set();
    for (const root of roots) {
        for (const im of root.querySelectorAll('img')) {
            if (im.closest('form')) continue;
            const src = im.currentSrc || im.src || '';
            if (!src) continue;
            const key = src.split('?')[0];
            if (seen.has(key)) continue;
            seen.add(key);
            keys.push(key);
        }
    }
    return keys;
}"""


# Lấy phần chữ CÓ Ý NGHĨA để đoán lỗi: hộp thoại + 2 lượt trả lời cuối. Không quét
# cả trang vì sidebar luôn có chữ "Upgrade plan" -> dễ báo nhầm hết lượt.
TAIL_JS = """() => {
    const parts = [];
    const dlg = document.querySelector('[role="dialog"]');
    if (dlg) parts.push(dlg.innerText || '');
    const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
    const last = Array.from(msgs).slice(-2);
    for (const m of last) parts.push(m.innerText || '');
    if (!last.length) {
        const main = document.querySelector('main');
        if (main) parts.push((main.innerText || '').slice(-1200));
    }
    return parts.join('\\n').slice(-3000);
}"""

# Hết lượt / hết quota tài khoản -> KHÔNG retry, chuyển job sang tài khoản khác.
QUOTA_PAT = (
    "reached your limit", "reached the limit", "reached our limit",
    "hit your limit", "hit the limit", "hit our limit",
    "limit for image", "image generation limit", "limit on image",
    "you can create more images", "able to create images again",
    "usage limit", "message limit", "you've reached your plan",
    "rate limit", "too many requests",
    "đã đạt giới hạn", "đạt đến giới hạn", "giới hạn tạo ảnh",
    "hết lượt", "vượt quá giới hạn",
)
# ChatGPT từ chối tạo ảnh (policy / nội dung) -> retry thường vô ích.
REFUSE_PAT = (
    "i can't help with that", "i cannot help with that",
    "i'm unable to create", "i can't create", "i cannot create",
    "i'm not able to generate", "can't generate that image",
    "content policy", "usage policies", "violates",
    "tôi không thể tạo", "không thể giúp", "vi phạm chính sách",
)
# Lỗi tạm thời -> retry có cơ hội qua.
TEMP_ERR_PAT = (
    "something went wrong", "an error occurred", "error generating",
    "network error", "please try again", "try again later",
    "error on my side", "wasn't able to generate", "unable to generate",
    "đã xảy ra lỗi", "có lỗi xảy ra", "thử lại sau",
)



# Ảnh mockup thật thì to; thumbnail/icon thì không. Ưu tiên đo bằng kích thước
# thật (Pillow); MIN_IMG_BYTES chỉ là phương án chống cháy khi thiếu Pillow.
#
# 768: ảnh ChatGPT gen ra đo được là 1024 / 1155 / 1254 / 2000 px. Ngưỡng cũ 400
# cho lọt ảnh nền giao diện 512x512 -> tài khoản nào không gen được ảnh là tool
# lưu nhầm chính ảnh nền đó làm kết quả.
MIN_IMG_SIDE = 768
MIN_IMG_BYTES = 20_000

# Ảnh của GIAO DIỆN nằm trên CDN tĩnh, ảnh gen nằm ở host nội dung người dùng.
# Thủ phạm bắt được: chatgpt.com/cdn/assets/noise-watercolor-*.webp (512x512, 32KB).
UI_ASSET_PARTS = ("/cdn/assets/", "/cdn-cgi/", "/static/", "/_next/", "/icons/",
                  "/sprites", "/fonts/")


def _is_ui_asset(url: str) -> bool:
    """Ảnh trang trí của giao diện, không phải ảnh ChatGPT tạo ra."""
    path = url.split("?", 1)[0].lower()
    return any(part in path for part in UI_ASSET_PARTS)


class _Shot:
    """Một ảnh bắt được: URL + bytes gốc."""

    __slots__ = ("url", "data", "ts", "w", "h")

    def __init__(self, url: str, data: bytes | None, seq: float,
                 w: int = 0, h: int = 0):
        # seq = số thứ tự response về; dùng để xếp ảnh đúng thứ tự ChatGPT trả ra
        self.url, self.data, self.ts, self.w, self.h = url, data, seq, w, h


class _ImageNet:
    """Bắt ảnh NGAY Ở TẦNG MẠNG thay vì mò trong DOM.

    DOM bày ảnh thành 1 ảnh lớn + mấy thumbnail bé tí (lại còn srcset, blob:), lọc
    theo kích thước hiển thị là sót ngay - đúng kiểu "ChatGPT gen 2 ảnh mà chỉ lấy
    được 1". Response ảnh thì không nói dối: gen ra mấy ảnh là có bấy nhiêu response,
    kèm luôn bytes gốc nên khỏi phải fetch lại URL đã ký.

    Ảnh gen thường tải về nhiều lần (bản mờ lúc đang gen -> bản nét): gom theo URL
    (bỏ query) và giữ bản NẶNG NHẤT.
    """

    def __init__(self, page: Page):
        self._shots: dict[str, _Shot] = {}
        self._ignore: set[str] = set()      # hash các file MÌNH upload lên
        self._seq = 0                       # số thứ tự response, để giữ ĐÚNG thứ tự
        self.armed = False                  # đã thấy ChatGPT bắt đầu gen chưa
        page.on("response", self._on_response)

    def ignore(self, paths: list[Path]) -> None:
        """Nhớ hash các template vừa gửi đi.

        Gửi xong là ChatGPT tải chính mấy ảnh đó về để hiển thị trong tin nhắn ->
        chúng cũng là response ảnh, to đúng bằng ảnh thật. Không loại ra thì tool
        tưởng "gen xong ngay trong 5 giây" và lưu lại đúng ảnh gốc."""
        for f in paths:
            try:
                self._ignore.add(hashlib.sha256(Path(f).read_bytes()).hexdigest())
            except Exception:  # noqa: BLE001
                pass

    def clear(self) -> None:
        """Xoá ảnh đã bắt (giữ danh sách hash cần bỏ qua)."""
        self._shots.clear()

    def shots(self) -> list[_Shot]:
        """Ảnh theo ĐÚNG thứ tự response về (= thứ tự ChatGPT gen ra)."""
        return sorted(self._shots.values(), key=lambda s: s.ts)

    def _on_response(self, response) -> None:
        try:
            ct = (response.headers or {}).get("content-type", "")
        except Exception:  # noqa: BLE001
            return
        if not ct.startswith("image/") or "svg" in ct:
            return
        low = response.url.lower()
        if any(k in low for k in ("/avatar", "favicon", "sprite", "logo")):
            return
        if _is_ui_asset(response.url):
            return
        # Số thứ tự phải lấy NGAY ĐÂY, theo thứ tự response về. Nếu lấy mốc thời
        # gian lúc đọc xong body thì mấy body tải song song sẽ về không theo thứ tự
        # -> ảnh gán nhầm template (design của cốc lưu thành file áo).
        self._seq += 1
        seq = self._seq
        # đọc body phải async -> đẩy sang task, không chặn event loop của Playwright
        asyncio.create_task(self._store(response, seq))

    async def _store(self, response, seq: int) -> None:
        try:
            data = await response.body()
        except Exception:  # noqa: BLE001 - body hết hạn / lấy từ cache
            return
        if not data or len(data) < 2_000:
            return
        w = h = 0
        if Image is not None:
            # KÍCH THƯỚC THẬT mới là thước đo đáng tin. Dung lượng thì tuỳ nội dung:
            # một mockup nền trắng nén rất nhẹ, chặn theo KB là loại nhầm ảnh thật.
            try:
                with Image.open(BytesIO(data)) as im:
                    w, h = im.size
            except Exception:  # noqa: BLE001 - không phải ảnh đọc được
                return
            if min(w, h) < MIN_IMG_SIDE:
                return                       # thumbnail / icon
        elif len(data) < MIN_IMG_BYTES:      # không có Pillow thì đành đoán theo KB
            return
        digest = hashlib.sha256(data).hexdigest()
        if digest in self._ignore:
            log.debug("Bỏ qua ảnh template mình vừa upload: %s", response.url[:80])
            return
        # Gom theo NỘI DUNG ảnh, không theo URL. Gom theo URL (bỏ query) thì nếu
        # ChatGPT trả mọi ảnh qua cùng một đường dẫn (chỉ khác ?file_id=...) là cả
        # loạt ảnh khác nhau bị nhập làm một -> "gen 3 ảnh mà chỉ lấy được 1".
        if digest in self._shots:
            return                          # đúng ảnh đó, tải lại lần nữa thôi
        self._shots[digest] = _Shot(response.url, data, seq, w, h)
        log.debug("Bắt được ảnh %dx%d (%.0f KB) %s",
                  w, h, len(data) / 1024, response.url[:90])


UpdateCb = Callable[[dict], Awaitable[None] | None]


class QuotaExceeded(RuntimeError):
    """Tài khoản hết lượt/quota giữa chừng.

    `images` giữ những ảnh đã kịp nhận trước khi bị chặn - vẫn lưu được, khỏi phí."""

    def __init__(self, msg: str, images: "list | None" = None):
        super().__init__(msg)
        self.images = images or []


class NoImage(RuntimeError):
    """Gửi được nhưng không nhận được ảnh (từ chối, lỗi, hoặc quá giờ)."""


def _classify(text: str) -> tuple[str, str]:
    """(loại lỗi, câu trích) từ phần chữ cuối của trang."""
    flat = " ".join(text.split())
    low = flat.lower()
    for pats, kind in ((QUOTA_PAT, "quota"),
                       (REFUSE_PAT, "refused"),
                       (TEMP_ERR_PAT, "error")):
        for p in pats:
            i = low.find(p)
            if i >= 0:
                return kind, flat[max(0, i - 60): i + 160].strip()
    return "", flat[-160:].strip()


def _is_dead(err: Exception) -> bool:
    """Lỗi loại 'tab/trình duyệt đã chết' -> retry trên tab này vô nghĩa."""
    m = str(err).lower()
    return any(k in m for k in (
        "target closed", "target page, context or browser has been closed",
        "browser has been closed", "connection closed", "page closed",
        "websocket", "crashed",
    ))


class _Slot:
    """Một tab của pool: boot bất đồng bộ, worker chỉ chờ đúng tab của mình."""

    def __init__(self, profile: str, tab_no: int, index: int):
        self.profile = profile
        self.tab_no = tab_no
        self.index = index                  # số thứ tự toàn cục (để log)
        self.page: Page | None = None
        self.error: str | None = None
        self.ready = asyncio.Event()        # set khi có page HOẶC boot hỏng
        self.done = 0                       # số job đã xong (mốc recycle)
        self.fresh = False                  # vừa mở/recycle -> đang ở chat trống
        self.net: _ImageNet | None = None   # bắt ảnh ở tầng mạng

    @property
    def label(self) -> str:
        return f"{self.profile}#{self.tab_no}"

    def set_page(self, page: Page, fresh: bool = True) -> None:
        self.page = page
        # fresh = đang đứng sẵn ở chat trống dùng được -> job đầu khỏi "chat mới".
        # Trang nạp lỗi thì KHÔNG fresh, để job đầu tự goto lại cho chắc.
        self.fresh = fresh
        self.ready.set()

    def fail(self, err: object) -> None:
        self.error = str(err)
        self.ready.set()

    async def wait_ready(self) -> Page | None:
        await self.ready.wait()
        return self.page


class ChatGPTPool:
    def __init__(self, cfg: dict):
        b = cfg.get("browser", {})
        self.headless = bool(b.get("headless", False))
        self.profiles_dir = Path(b.get("profiles_dir", "./.chrome-profiles")).resolve()
        self.profiles = b.get("profiles") or [{"name": "acc1", "tabs": 1}]
        # Tài khoản đã đăng nhập nhưng KHÔNG được chọn cho lượt này. Khi cả đội
        # hình chính hết lượt, pool gọi dự bị vào thay thay vì bỏ dở collection.
        self.reserves: list[str] = list(b.get("reserves") or [])
        # số tab mỗi tài khoản (mặc định 1); dùng khi gọi dự bị hoặc thêm tài khoản
        self.tabs_per_account = max(1, int(b.get("tabs_per_account", 1)))
        self.gen_timeout = int(b.get("generation_timeout", 300))
        self.nav_timeout = int(b.get("nav_timeout", 90)) * 1000
        # giãn cách nhỏ giữa các lần bật Chrome: bật cùng lúc cả chục cửa sổ dễ
        # nghẽn ổ đĩa/CPU, còn chậm hơn là lệch nhau vài trăm ms.
        self.launch_stagger = float(b.get("launch_stagger", 0.4) or 0)
        r = cfg.get("run") or {}
        self.max_retries = int(r.get("max_retries", 2))
        # số ảnh gửi kèm trong MỘT tin nhắn. ChatGPT có trần đính kèm nên nhiều ảnh
        # phải chia lô - nhưng các lô đi tiếp trong CÙNG chat để giữ nguyên design.
        self.batch_size = max(1, int(r.get("batch_size", 6)))
        self.warnings: list[str] = []      # cảnh báo cho UI (không chặn lượt gen)
        self._warned_dom = False           # đã cảnh báo DOM đổi cấu trúc chưa
        self.topup_prompt = (r.get("topup_prompt") or
            "The previous attempt failed or returned fewer images than uploaded. "
            "Using the EXACT same design as above (same concept, colors, typography, "
            "layout and style), generate the finished mockup for each image uploaded "
            "in THIS message. Return one separate image per uploaded image.").strip()
        self.followup_prompt = (r.get("followup_prompt") or
            "Apply the EXACT same design as above to these additional product "
            "mockups. Keep the same concept, colors, typography, layout and style. "
            "Return one finished mockup image for each uploaded image.").strip()
        self._pw = None
        self._ctxs: list = []
        self._slots: list[_Slot] = []
        self._boot: list[asyncio.Task] = []
        # profile -> lý do hết lượt. Server/UI đọc trực tiếp dict này.
        self.exhausted: dict[str, str] = {}
        # Tạo NGAY từ đầu. Trước đây queue chỉ ra đời khi run_collections chạy, mà
        # lúc đó Chrome còn đang khởi động vài giây -> collection nạp thêm trong
        # khoảng đó rơi vào hư không, server vẫn báo "đã xếp hàng".
        self.collection_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._last_active_time: float = 0.0
        self.running_collections = False   # đang ở chế độ collections (nạp thêm được)
        # True = giữ ảnh đã gen khi collection phải chuyển sang tài khoản khác
        # (nhanh hơn nhưng bộ ảnh sẽ pha trộn 2 hướng design)
        self.resume_partial = bool(r.get("resume_partial_on_other_account", False))
        # Sau khi hết việc, pool nán lại chừng này giây (Chrome vẫn mở) để còn nhận
        # collection nạp thêm từ UI mà không phải bật lại trình duyệt.
        # Nán lại lâu hơn để lượt gen kế tiếp DÙNG LẠI Chrome đang mở thay vì bật
        # lại từ đầu (mỗi lần bật tốn 10-30s cho cả đội hình).
        self.idle_exit = float(r.get("idle_exit_seconds", 90))
        # đủ ảnh rồi vẫn phải thấy danh sách ảnh đứng yên chừng này giây mới chốt
        # 5s: đủ để bắt cú "ChatGPT vẽ lại ảnh ở phút chót" (lần nào cũng mất hơn
        # 20s mới ra ảnh thay), mà không bắt mỗi tin nhắn phải đứng chờ 8 giây.
        self.settle_seconds = float(r.get("settle_seconds", 5))

    # ---------------- lifecycle ----------------
    async def __aenter__(self) -> "ChatGPTPool":
        self._pw = await async_playwright().start()
        idx = 0
        by_profile: list[tuple[str, list[_Slot]]] = []
        for prof in self.profiles:
            name = prof.get("name") or "acc1"
            tabs = max(1, int(prof.get("tabs", 1)))
            slots = []
            for t in range(tabs):
                s = _Slot(name, t, idx)
                idx += 1
                slots.append(s)
                self._slots.append(s)
            by_profile.append((name, slots))

        # KHÔNG await ở đây: mỗi profile tự boot, tab nào xong trước chạy trước.
        for i, (name, slots) in enumerate(by_profile):
            self._boot.append(asyncio.create_task(
                self._boot_profile(name, slots, delay=i * self.launch_stagger)))
        log.info("Đang bật %d profile / %d tab (song song).",
                 len(by_profile), len(self._slots))
        return self

    async def _boot_profile(self, name: str, slots: list[_Slot], delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        udir = self.profiles_dir / name
        try:
            udir.mkdir(parents=True, exist_ok=True)
            ctx = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(udir),
                headless=self.headless,
                channel="chrome",
                viewport={"width": 1400, "height": 950},
                args=["--disable-blink-features=AutomationControlled",
                      "--no-first-run", "--no-default-browser-check"],
            )
        except Exception as e:  # noqa: BLE001
            log.error("Không bật được Chrome cho '%s': %s", name, e)
            for s in slots:
                s.fail(f"không bật được Chrome: {e}")
            return
        self._ctxs.append(ctx)
        first = ctx.pages[0] if ctx.pages else None

        async def open_tab(slot: _Slot, page: Page | None) -> None:
            try:
                p = page or await ctx.new_page()
                okp = await self._prepare(p)
                slot.net = _ImageNet(p)
                slot.set_page(p, fresh=okp)
                log.info("[%s] tab sẵn sàng%s.", slot.label,
                         "" if okp else " (trang chưa nạp xong, sẽ nạp lại ở job đầu)")
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] mở tab lỗi: %s", slot.label, e)
                slot.fail(e)

        # các tab trong cùng 1 profile cũng mở song song
        await asyncio.gather(*(
            open_tab(s, first if i == 0 else None) for i, s in enumerate(slots)
        ))

    def add_profiles(self, names: list[str]) -> int:
        """Bổ sung tài khoản vào đội hình đang chạy (dùng khi UI chọn thêm acc).

        Chỉ tạo slot + task boot; bộ điều phối trong `run_collections` thấy slot mới
        là tự sinh worker trong vòng nửa giây, không phải chờ ai nghỉ.
        """
        added = 0
        for name in names:
            if not name or name in self.exhausted:
                continue
            if any(sl.profile == name for sl in self._slots):
                continue
            slots = [_Slot(name, i, len(self._slots) + i)
                     for i in range(self.tabs_per_account)]
            self._slots.extend(slots)
            self._boot.append(asyncio.create_task(
                self._boot_profile(name, slots, delay=0)))
            self.reserves = [r for r in self.reserves if r != name]
            added += len(slots)
            log.info("Thêm tài khoản '%s' (%d tab) vào đội hình đang chạy.",
                     name, len(slots))
        return added

    async def recruit_reserve(self) -> _Slot | None:
        """Bật thêm 1 tài khoản dự bị và trả về slot của nó.

        Dùng khi đội hình đang chạy hết lượt: thay vì để collection chết trong
        hàng đợi, kéo một tài khoản rảnh đã đăng nhập vào chạy tiếp.
        """
        while self.reserves:
            name = self.reserves.pop(0)
            if name in self.exhausted or any(s.profile == name for s in self._slots):
                continue
            slots = [_Slot(name, i, len(self._slots) + i)
                     for i in range(self.tabs_per_account)]
            self._slots.extend(slots)
            log.info("Gọi tài khoản dự bị '%s' (%d tab) vào thay.",
                     name, len(slots))
            self._boot.append(asyncio.create_task(
                self._boot_profile(name, slots, delay=0)))
            page = await slots[0].wait_ready()
            if page is not None:
                return slots[0]
            log.warning("Tài khoản dự bị '%s' không mở được: %s", name, slot.error)
        return None

    async def __aexit__(self, *exc):
        for t in self._boot:
            if not t.done():
                t.cancel()
        if self._boot:
            await asyncio.gather(*self._boot, return_exceptions=True)
        for ctx in self._ctxs:
            try:
                await ctx.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()

    async def _prepare(self, page: Page) -> bool:
        """True = trang đã nạp xong và thấy ô nhập (tab dùng được ngay)."""
        try:
            await page.goto(URL, wait_until="domcontentloaded",
                            timeout=self.nav_timeout)
            await self._find(page, SEL_PROMPT, 60_000)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Tab chưa thấy ô nhập sau khi mở (%s) - job đầu sẽ nạp lại.", e)
            return False

    # ---------------- helpers ----------------
    async def _find(self, page: Page, selectors, timeout=15_000):
        per = max(1500, timeout // max(1, len(selectors)))
        last = None
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=per)
                return loc
            except PWTimeout as e:
                last = e
        raise PWTimeout(f"Không selector nào khớp: {selectors}") from last

    async def _new_chat(self, page: Page) -> None:
        """Mở hội thoại MỚI sạch (mỗi mockup 1 phiên riêng)."""
        for sel in SEL_NEW_CHAT:
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible():
                    await b.click(timeout=3_000)
                    await page.wait_for_timeout(500)
                    await self._find(page, SEL_PROMPT, 8_000)
                    return
            except Exception:
                continue
        # dự phòng: nạp lại trang gốc
        await page.goto(URL, wait_until="domcontentloaded", timeout=self.nav_timeout)
        await self._find(page, SEL_PROMPT, 30_000)

    async def _attach_once(self, page: Page, templates: list[Path], wait_s: int) -> bool:
        """Nạp CẢ danh sách ảnh vào input rồi chờ đủ thumbnail upload xong."""
        want = len(templates)
        try:
            await page.locator(SEL_FILE_INPUT).first.wait_for(state="attached", timeout=10_000)
        except PWTimeout:
            log.warning("Không thấy input[type=file].")
            return False
        # set_input_files nhận cả list -> đính kèm nhiều ảnh trong 1 tin nhắn
        files = [str(t) for t in templates]
        for inp in await page.locator(SEL_FILE_INPUT).all():
            try:
                await inp.set_input_files(files)
                break
            except Exception:
                continue

        # chờ ĐỦ số thumbnail hiện + không còn spinner nào
        deadline = asyncio.get_event_loop().time() + wait_s
        last_n = -1
        while asyncio.get_event_loop().time() < deadline:
            st = await page.evaluate("""() => {
                const form = document.querySelector('form') || document.body;
                const q = (s) => form.querySelectorAll(s).length;
                // 1 đính kèm có thể khớp nhiều kiểu selector -> lấy max, đừng cộng
                const n = Math.max(
                    q('img[src^="blob:"], img[src^="data:"]'),
                    q('[data-testid*="attachment" i]'),
                    q('button[aria-label*="Remove" i], button[aria-label*="Xoá" i]'));
                const up = form.querySelector(
                    '[role="progressbar"], svg[class*="spin" i], [class*="uploading" i]');
                return {n, uploading: !!up};
            }""")
            if st["n"] != last_n:
                last_n = st["n"]
                log.debug("đính kèm %d/%d", st["n"], want)
            if st["n"] >= want and not st["uploading"]:
                return True
            await page.wait_for_timeout(600)
        log.warning("Mới đính kèm %d/%d ảnh sau %ds.", max(last_n, 0), want, wait_s)
        return False

    async def _upload(self, page: Page, templates: list[Path],
                      allow_new_chat: bool = True) -> bool:
        """Đính kèm TẤT CẢ ảnh của lô vào 1 tin nhắn. True khi upload xong hết.

        Gửi khi ảnh chưa upload xong thì ChatGPT bỏ ảnh, nhận mỗi text -> phải chờ kỹ.
        Mạng chập chờn hay làm lần nạp đầu treo -> thử lại 1 lần.
        `allow_new_chat=False` cho các lô nối tiếp: mở chat mới sẽ mất design đã chốt.
        """
        wait_s = 25 + 5 * len(templates)          # nhiều ảnh thì cho thêm thời gian
        if await self._attach_once(page, templates, wait_s):
            await page.wait_for_timeout(500)
            return True
        log.warning("Đính kèm chưa xong sau %ds - thử lại.", wait_s)
        if allow_new_chat:
            try:
                await self._new_chat(page)
            except Exception as e:  # noqa: BLE001
                log.warning("Mở chat mới để đính kèm lại lỗi: %s", e)
        if await self._attach_once(page, templates, wait_s + 10):
            await page.wait_for_timeout(500)
            return True
        log.warning("Đính kèm thất bại cả 2 lần (mạng chậm hoặc trang chưa nạp xong).")
        return False

    async def _send(self, page: Page, prompt: str) -> None:
        box = await self._find(page, SEL_PROMPT, 15_000)
        await box.click()
        # DÁN cả prompt 1 lần bằng execCommand('insertText'), KHÔNG gõ từng ký tự.
        # Gõ từng ký tự thì dấu xuống dòng trong prompt biến thành phím Enter ->
        # ChatGPT gửi khi prompt mới gõ được một phần. insertText chèn nguyên khối
        # (kể cả \n) qua đúng pipeline soạn thảo nên không kích hoạt gửi.
        await box.evaluate("""(el, txt) => {
            el.focus();
            const sel = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(el);
            sel.removeAllRanges(); sel.addRange(range);
            document.execCommand('insertText', false, txt);
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }""", prompt)
        await page.wait_for_timeout(300)
        # bấm nút Send (chờ enabled), fallback Enter
        deadline = asyncio.get_event_loop().time() + 12
        clicked = False
        while asyncio.get_event_loop().time() < deadline and not clicked:
            for sel in SEL_SEND:
                try:
                    for b in await page.locator(sel).all():
                        if await b.is_visible() and await b.is_enabled():
                            await b.click(timeout=4_000)
                            clicked = True
                            break
                except Exception:
                    continue
                if clicked:
                    break
            if not clicked:
                await page.wait_for_timeout(300)
        if not clicked:
            await box.click()
            await page.keyboard.press("Enter")

    @staticmethod
    def _pick(imgs: list[dict]) -> list[str]:
        """Lọc ảnh mockup thật (bỏ avatar/icon nhỏ, bỏ trùng)."""
        out, seen = [], set()
        for it in imgs:
            src, w, h = it["src"], it["w"], it["h"]
            if w < 200 or h < 200:
                continue
            low = src.lower()
            if any(k in low for k in ("/avatar", "favicon", "sprite", "logo")):
                continue
            if src in seen:
                continue
            seen.add(src)
            out.append(src)
        return out

    async def _poll(self, page: Page) -> tuple[bool, list[str]]:
        """1 round-trip: (đang gen?, danh sách ảnh lớn trên trang)."""
        st = await page.evaluate(POLL_JS)
        if not st.get("roles") and not self._warned_dom:
            # Mất mốc này thì không tách được tin của mình với tin ChatGPT trả lời;
            # tool vẫn chạy bằng nhánh dự phòng nhưng đáng để biết mà cập nhật.
            self._warned_dom = True
            msg = ("DOM ChatGPT không còn [data-message-author-role] - đang dùng "
                   "nhánh dự phòng, việc nhận diện ảnh kém chính xác hơn.")
            log.warning(msg)
        return bool(st["busy"]), self._pick(st["imgs"])

    async def _collect(self, page: Page) -> list[str]:
        _, srcs = await self._poll(page)
        return srcs

    async def _tail(self, page: Page) -> str:
        """Chữ ở cuối hội thoại (hộp thoại + 2 lượt trả lời cuối)."""
        try:
            return await page.evaluate(TAIL_JS)
        except Exception:  # noqa: BLE001
            return ""

    async def _diagnose(self, page: Page) -> tuple[str, str]:
        """Đọc chữ cuối trang để biết vì sao chưa có ảnh."""
        return _classify(await self._tail(page))

    async def _raise_if_bad(self, page: Page, stale: str | None = None) -> None:
        """`stale` = chữ đã có TRƯỚC khi gửi tin này.

        Thiếu nó thì vòng xin làm nốt sẽ đọc lại đúng dòng "Something went wrong"
        của lượt hỏng trước rồi fail ngay, dù lượt mới đang chạy ngon lành."""
        text = await self._tail(page)
        kind, msg = _classify(text)
        # Lỗi tạm / từ chối là chuyện của TỪNG LƯỢT: chữ y hệt lúc trước khi gửi thì
        # đó là tàn dư của lượt cũ, bỏ qua. Riêng HẾT LƯỢT thì không hoãn - nó có thể
        # hiện ra ngay trước khi mình kịp chụp lại màn hình, mà bỏ sót là gửi tiếp
        # trong vô vọng.
        if kind != "quota" and stale is not None and text == stale:
            return
        if kind == "quota":
            raise QuotaExceeded(f"tài khoản hết lượt tạo ảnh — {msg}")
        if kind == "refused":
            raise NoImage(f"ChatGPT từ chối tạo ảnh — {msg}")
        if kind == "error":
            raise NoImage(f"ChatGPT báo lỗi — {msg}")

    async def _dom_keys(self, page: Page) -> list[str]:
        """URL (bỏ query) của các ảnh ĐANG hiện trong câu trả lời, theo thứ tự."""
        try:
            return await page.evaluate(ORDER_JS)
        except Exception:  # noqa: BLE001
            return []

    async def _finalize(self, page: Page, shots: list[_Shot], want: int) -> list[_Shot]:
        """Chốt danh sách ảnh: xếp đúng thứ tự và bỏ ảnh thừa.

        Căn cứ là ẢNH ĐANG HIỆN TRÊN MÀN HÌNH lúc chốt. ChatGPT hay vẽ lại một ảnh ở
        phút chót; bản bị thay vẫn nằm trong mớ response đã bắt nhưng KHÔNG còn trong
        DOM nữa - đó là cách phân biệt chắc chắn. Trước đây chỗ này đoán bằng "ảnh nào
        nặng hơn", mà 4 ảnh cùng cỡ 2.4-2.7 MB thì đoán là hên xui.

        Chỉ khi DOM không cho đủ thông tin mới quay về suy đoán theo dung lượng.
        """
        if len(shots) <= 1:
            return shots
        keys = await self._dom_keys(page)
        rank = {k: i for i, k in enumerate(keys)}
        on_screen = [sh for sh in shots if sh.url.split("?")[0] in rank]
        on_screen.sort(key=lambda sh: rank[sh.url.split("?")[0]])

        if len(on_screen) >= want:
            if len(shots) > want:
                log.info("Bỏ %d ảnh đã bị ChatGPT thay / không còn hiển thị.",
                         len(shots) - want)
            return on_screen[:want]

        # DOM thiếu (ảnh lazy-load, DOM đổi cấu trúc...) -> ghép nốt theo thứ tự
        # response, và chỉ tới lúc này mới phải suy đoán bằng dung lượng.
        rest = sorted((sh for sh in shots if sh.url.split("?")[0] not in rank),
                      key=lambda sh: sh.ts)
        merged = on_screen + rest
        if len(merged) <= want:
            return merged
        log.warning("DOM chỉ thấy %d/%d ảnh - đành giữ %d ảnh nặng nhất (%s KB).",
                    len(on_screen), want, want,
                    ", ".join(str(len(sh.data or b"") // 1024) for sh in merged))
        heavy = sorted(merged, key=lambda sh: len(sh.data or b""), reverse=True)[:want]
        keep = {id(sh) for sh in heavy}
        return [sh for sh in merged if id(sh) in keep]

    async def _wait_images(self, page: Page, before: set, want: int,
                           net: "_ImageNet | None" = None) -> list[_Shot]:
        """Chờ ChatGPT trả về `want` ảnh. Ném QuotaExceeded/NoImage khi hỏng.

        Nguồn ảnh CHÍNH là tầng mạng (`net`): response ảnh phản ánh đúng số ảnh gen
        ra, kể cả những ảnh DOM chỉ bày dưới dạng thumbnail. DOM chỉ dùng để biết
        "còn đang gen không" và làm nguồn dự phòng khi không bắt được response.

        Trả về ít hơn `want` nếu ChatGPT ngừng hẳn mà chỉ ra được vài ảnh - caller
        tự đánh dấu phần thiếu, còn ảnh đã có thì vẫn lưu."""
        loop = asyncio.get_event_loop()
        stale = await self._tail(page)      # chữ CŨ trên màn hình, để khỏi bắt nhầm

        def dom_shots(srcs):
            return [_Shot(u, None, 0.0) for u in srcs if u not in before]

        def best(srcs) -> list[_Shot]:
            """Ưu tiên ảnh bắt từ mạng, thiếu thì lấy tạm từ DOM."""
            netted = net.shots() if net else []
            dom = dom_shots(srcs)
            return netted if len(netted) >= len(dom) else dom

        # pha 1: chờ ChatGPT bắt đầu trả lời (nút Stop hiện). Ảnh mới nhảy ra luôn
        # thì bỏ qua pha này. Sau 8s vẫn im -> soi chữ xem có phải hết lượt không,
        # khỏi ngồi chờ hết cả generation_timeout.
        def arm(busy: bool) -> None:
            """Thấy ChatGPT bắt đầu làm -> bỏ hết ảnh bắt được trước thời điểm này.

            Trước mốc đó chỉ có thể là ảnh template ChatGPT tải về để hiển thị trong
            tin nhắn vừa gửi. Lọc theo hash đã chặn phần lớn, đây là lưới thứ hai
            phòng khi ChatGPT nén lại ảnh (hash đổi)."""
            if busy and net is not None and not net.armed:
                net.clear()
                net.armed = True

        start_deadline = loop.time() + 30
        next_diag = loop.time() + 8
        started = False
        while loop.time() < start_deadline:
            busy, srcs = await self._poll(page)
            arm(busy)
            if busy or best(srcs):
                started = True
                break
            if loop.time() >= next_diag:
                await self._raise_if_bad(page, stale)
                next_diag = loop.time() + 8
            await page.wait_for_timeout(700)
        if not started:
            await self._raise_if_bad(page, stale)
            raise NoImage("gửi xong nhưng ChatGPT không phản hồi")

        # pha 2: chờ đủ ảnh. Ngân sách thời gian nhân theo số ảnh của lô.
        # "quiet" = hết busy mà chưa đủ ảnh -> nhiều khả năng nó trả lời bằng chữ
        # (từ chối / hỏi lại / hết lượt): soi ngay thay vì chờ hết giờ.
        deadline = loop.time() + self.gen_timeout * want
        quiet_since = None
        ready_since = None            # từ lúc nào thì "đủ ảnh và không đổi nữa"
        ready_keys = None
        next_diag = 0.0
        got: list[_Shot] = []
        while loop.time() < deadline:
            busy, srcs = await self._poll(page)
            arm(busy)
            got = best(srcs)

            # Đủ số ảnh CHƯA CHẮC đã xong: ChatGPT hay vẽ lại một ảnh ở phút chót,
            # có khi nút Stop cũng biến mất một nhịp giữa hai lần gọi công cụ. Nên
            # đòi thêm: danh sách ảnh đứng yên suốt `settle_seconds` mới chốt.
            if not busy and len(got) >= want:
                keys = tuple(sh.url for sh in got)
                if ready_since is None or keys != ready_keys:
                    ready_since, ready_keys = loop.time(), keys
                elif loop.time() - ready_since >= self.settle_seconds:
                    return await self._finalize(page, got, want)
            else:
                ready_since = None

            if busy:
                quiet_since = None
            else:
                now = loop.time()
                if quiet_since is None:
                    quiet_since = now
                    next_diag = now + 3
                if now >= next_diag:
                    try:
                        await self._raise_if_bad(page, stale)
                    except QuotaExceeded as e:
                        raise QuotaExceeded(str(e), got) from None   # giữ ảnh đã có
                    next_diag = now + 8
                # Chưa đủ ảnh mà đã im: ChatGPT hay gen từng ảnh một, giữa các ảnh
                # có quãng nghỉ. Có ảnh rồi thì chờ rộng tay hơn trước khi bỏ cuộc.
                quiet_limit = 75 if got else 30
                if now - quiet_since > quiet_limit:
                    if got:      # trả thiếu nhưng có còn hơn không
                        log.warning("ChatGPT chỉ trả %d/%d ảnh sau %ds im lặng. "
                                    "Ảnh bắt được: %s", len(got), want, quiet_limit,
                                    "; ".join(f"{sh.w}x{sh.h} "
                                              f"{len(sh.data or b'') // 1024}KB "
                                              f"{sh.url[-60:]}" for sh in got))
                        return await self._finalize(page, got, want)
                    raise NoImage("ChatGPT trả lời xong nhưng không có ảnh")
            await page.wait_for_timeout(1_000)

        if got:
            log.warning("Hết giờ, mới có %d/%d ảnh.", len(got), want)
            return await self._finalize(page, got, want)
        await self._raise_if_bad(page, stale)
        raise NoImage(f"quá {self.gen_timeout * want}s vẫn chưa có ảnh")

    async def _save(self, page: Page, shot: _Shot, dest: Path) -> bool:
        """Lưu ảnh: bắt được từ mạng thì ghi thẳng bytes, không thì mới tải lại."""
        if shot.data:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(shot.data)
            log.info("Lưu %s (%dx%d, %.0f KB) từ response mạng.",
                     dest.name, shot.w, shot.h, len(shot.data) / 1024)
            return True
        return await self._download(page, shot.url, dest)

    async def _download(self, page: Page, src: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.startswith("data:image"):
            dest.write_bytes(base64.b64decode(src.split(",", 1)[1]))
            return True
        # fetch trong trang (mang cookie đăng nhập) - hợp URL ký oaiusercontent
        try:
            b64 = await page.evaluate(
                """async u => {
                    const r = await fetch(u, {credentials:'include'});
                    if (!r.ok) return null;
                    const b = await r.blob();
                    return await new Promise(res=>{const f=new FileReader();
                        f.onload=()=>res(f.result.split(',')[1]);f.readAsDataURL(b);});
                }""", src)
            if b64:
                data = base64.b64decode(b64)
                if len(data) > 5_000:
                    dest.write_bytes(data)
                    return True
        except Exception as e:  # noqa: BLE001
            log.debug("fetch trong trang lỗi: %s", e)
        try:
            r = await page.request.get(src, timeout=60_000)
            body = await r.body()
            if r.ok and len(body) > 5_000:
                dest.write_bytes(body)
                return True
        except Exception as e:  # noqa: BLE001
            log.debug("request.get lỗi: %s", e)
        return False

    # ---------------- chạy 1 lượt: TẤT CẢ ảnh trong cùng 1 chat ----------------
    async def _run_collection_on_slot(
        self,
        slot: _Slot,
        page: Page,
        col: dict,
        on_update: UpdateCb = None
    ) -> bool:
        """Thực thi trọn vẹn 1 Collection trong 1 phiên chat duy nhất của 1 slot."""
        async def emit(job):
            if on_update:
                res = on_update(job)
                if asyncio.iscoroutine(res):
                    await res

        jobs = col.get("jobs", [])
        pending_jobs = [j for j in jobs if j.get("status") != "done"]
        if not pending_jobs:
            col["status"] = "done"
            return True

        # mở đúng 1 chat sạch cho collection này
        try:
            if slot.fresh:
                slot.fresh = False
                try:
                    await self._find(page, SEL_PROMPT, 5_000)
                except PWTimeout:
                    await self._new_chat(page)
            else:
                await self._new_chat(page)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] Mở chat mới cho collection '%s' lỗi: %s", slot.label, col.get("name"), e)
            for j in pending_jobs:
                j["status"] = "failed"
                j["error"] = f"Không mở được chat: {e}"
                await emit(j)
            col["status"] = "failed"
            col["error"] = f"Không mở được chat: {e}"
            return False

        seen = set(await self._collect(page))
        chunks = [pending_jobs[i:i + self.batch_size]
                  for i in range(0, len(pending_jobs), self.batch_size)]
        log.info("[%s] Bắt đầu gen collection '%s' (%d ảnh, %d lô) trên tài khoản %s.",
                 slot.label, col.get("name"), len(pending_jobs), len(chunks), slot.profile)

        quota: str | None = None
        for ci, chunk in enumerate(chunks):
            if quota:
                break
            for j in chunk:
                j["status"] = "running"
                j["worker"] = slot.label
                await emit(j)

            pending = list(chunk)
            first_msg = (ci == 0)
            last_err: Exception | None = None

            for round_no in range(1, self.max_retries + 2):
                if not pending or quota:
                    break
                tpls = [Path(j["template"]) for j in pending]
                if slot.net:
                    slot.net.clear()          # chỉ tính ảnh của VÒNG NÀY
                    slot.net.armed = False
                    slot.net.ignore(tpls)     # đừng nhận nhầm ảnh vừa gửi lên
                imgs: list[_Shot] = []
                try:
                    if not await self._upload(
                            page, tpls, allow_new_chat=(first_msg and round_no == 1)):
                        raise RuntimeError("không đính kèm được ảnh template")
                    if first_msg:
                        text = col.get("prompt") or chunk[0].get("prompt")
                    elif round_no == 1:
                        text = self.followup_prompt
                    else:
                        text = self.topup_prompt
                    await self._send(page, text)
                    first_msg = False
                    imgs = await self._wait_images(page, seen, len(pending), slot.net)
                except QuotaExceeded as e:
                    quota = str(e)
                    imgs = e.images               # ảnh kịp nhận trước khi bị chặn
                    log.error("[%s] %s", slot.label, quota)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    log.warning("[%s] col '%s' lô %d/%d vòng %d lỗi: %s",
                                slot.label, col.get("name"), ci + 1, len(chunks), round_no, e)
                    if _is_dead(e):
                        break
                    have = list(slot.net.shots()) if slot.net else []
                    if not have:
                        try:
                            have = [_Shot(u, None, i)
                                    for i, u in enumerate(await self._collect(page))
                                    if u not in seen]
                        except Exception:  # noqa: BLE001
                            have = []
                    if have:
                        imgs = await self._finalize(page, have, len(pending))
                        log.warning("[%s] vẫn nhặt được %d ảnh trước khi lỗi.",
                                    slot.label, len(imgs))

                imgs = imgs[:len(pending)]
                seen |= {sh.url for sh in imgs}
                for j, shot in zip(pending, imgs):   # gán theo thứ tự ảnh hiện ra
                    ok = False
                    try:
                        ok = await self._save(page, shot, Path(j["dest"]))
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                    j["status"] = "done" if ok else "failed"
                    j["error"] = None if ok else "tải ảnh về thất bại"
                    await emit(j)

                pending = pending[len(imgs):]
                if pending and not quota and round_no <= self.max_retries:
                    log.warning("[%s] col '%s' còn thiếu %d ảnh -> xin ChatGPT làm nốt.",
                                slot.label, col.get("name"), len(pending))
                    await asyncio.sleep(2)

            for j in pending:
                j["status"] = "failed"
                j["error"] = quota or (
                    str(last_err) if last_err else
                    "ChatGPT không trả đủ ảnh sau nhiều lần xin làm nốt")
                await emit(j)

        # Hết lượt là thoát vòng lô ngay -> job ở các lô SAU chưa hề được đụng tới.
        # Không quét nốt thì chúng kẹt "pending" vĩnh viễn: UI quay mãi, lượt gen
        # không bao giờ coi như kết thúc.
        for j in jobs:
            if j.get("status") in (None, "pending", "running"):
                j["status"] = "failed"
                j["error"] = quota or "Lượt gen dừng giữa chừng"
                await emit(j)

        if quota:
            self.exhausted[slot.profile] = quota
            col["status"] = "partial" if any(j["status"] == "done" for j in jobs) else "failed"
            col["error"] = quota
            return False

        done_count = sum(1 for j in jobs if j["status"] == "done")
        col["status"] = "done" if done_count == len(jobs) else ("partial" if done_count > 0 else "failed")
        log.info("[%s] Hoàn thành collection '%s': %d/%d ảnh.",
                 slot.label, col.get("name"), done_count, len(jobs))
        return True

    # ---------------- chạy 1 lượt đơn lẻ (tương thích ngược) ----------------
    async def run_batch(self, jobs: list[dict], on_update: UpdateCb = None) -> None:
        """Gen một lượt đơn lẻ.

        Đi chung đường với `run_collections` (gói thành đúng 1 collection) để hưởng
        luôn cơ chế hết lượt -> đẩy sang tài khoản khác / gọi tài khoản dự bị, thay
        vì fail cả lượt như trước. `idle_exit=0`: xong là đóng ngay, không nán lại
        chờ nạp thêm như chế độ collections.
        """
        if not jobs:
            return
        col = {"id": "single", "name": "Single", "status": "pending",
               "prompt": jobs[0].get("prompt", ""), "jobs": jobs, "worker": None}
        await self.run_collections([col], on_update=on_update, idle_exit=0)

    def enqueue_collections(self, collections: list[dict],
                            profiles: list[str] | None = None) -> bool:
        """Nạp thêm collections vào hàng đợi đang chạy. False = không nạp được.

        `profiles` là các tài khoản vừa được chọn ở lượt nạp này - tài khoản nào
        chưa có trong đội hình thì bật thêm ngay để cùng gánh việc.
        """
        if not collections:
            return False
        if not self.running_collections:
            log.warning("Pool không ở chế độ collections - không nạp thêm được.")
            return False
        try:
            loop = asyncio.get_running_loop()
            self._last_active_time = loop.time()
        except RuntimeError:
            pass
        if profiles:
            self.add_profiles(profiles)
        for col in collections:
            col["status"] = "pending"
            for j in col.get("jobs", []):
                j["status"] = "pending"
            self.collection_queue.put_nowait(col)
        log.info("Đã nạp thêm %d collection vào hàng đợi của Pool.", len(collections))
        return True

    # ---------------- chạy Collections hàng loạt (Worker Pool) ----------------
    async def run_collections(
        self,
        collections: list[dict],
        on_update: UpdateCb = None,
        on_fleet_update: Callable[[str, dict], None] = None,
        idle_exit: float | None = None
    ) -> None:
        """Chạy danh sách Collections song song qua nhiều tài khoản ChatGPT (Worker Pool với Dynamic Queue)."""
        if not collections:
            return
        if not self._slots:
            for c in collections:
                c["status"] = "failed"
                c["error"] = "Không có tài khoản khả dụng"
                for j in c.get("jobs", []):
                    j["status"] = "failed"
                    j["error"] = "Không có tài khoản khả dụng"
            return

        loop = asyncio.get_running_loop()
        idle_limit = self.idle_exit if idle_exit is None else idle_exit
        self.running_collections = True
        self._last_active_time = loop.time()
        for col in collections:
            col["status"] = "pending"
            for j in col.get("jobs", []):
                j["status"] = "pending"
            await self.collection_queue.put(col)

        busy_slots: set[_Slot] = set()

        async def worker(slot: _Slot):
            profile = slot.profile
            if on_fleet_update:
                on_fleet_update(profile, {"status": "starting", "collection": None, "collection_name": None})

            page = await slot.wait_ready()
            if page is None:
                log.error("Slot %s không mở được: %s", slot.label, slot.error)
                if on_fleet_update:
                    on_fleet_update(profile, {"status": "error", "error": str(slot.error), "collection": None})
                return

            if on_fleet_update:
                on_fleet_update(profile, {"status": "idle", "collection": None, "collection_name": None})

            while True:
                if profile in self.exhausted:
                    if on_fleet_update:
                        on_fleet_update(profile, {
                            "status": "exhausted",
                            "reason": self.exhausted[profile],
                            "collection": None
                        })
                    break

                col = None
                try:
                    # Chờ lấy collection mới trong hàng đợi (timeout 1.5s để thăm dò trạng thái rảnh)
                    col = await asyncio.wait_for(self.collection_queue.get(), timeout=1.5)
                except asyncio.TimeoutError:
                    now = loop.time()
                    if not busy_slots and (now - self._last_active_time > idle_limit):
                        # Toàn bộ worker đều rảnh và đã quá 25s không có thêm collection nào -> hoàn tất
                        break
                    continue

                self._last_active_time = loop.time()
                busy_slots.add(slot)

                # Collection này từng chạy dở trên TÀI KHOẢN KHÁC (chủ tài khoản cũ
                # hết lượt giữa chừng). Chat cũ nằm ở tài khoản đó, tài khoản này
                # phải mở chat mới -> ChatGPT chốt một hướng design khác. Ghép ảnh
                # hai chat vào cùng một bộ là mất đúng cái tính đồng nhất mà cả kiến
                # trúc này sinh ra để giữ. Nên làm lại cả bộ trên tài khoản mới.
                prev = col.get("ran_on")
                if prev and prev != profile and not self.resume_partial:
                    redo = [j for j in col.get("jobs", []) if j.get("status") == "done"]
                    if redo:
                        log.warning("[%s] Collection '%s' dở dang từ '%s' -> gen lại "
                                    "cả %d ảnh cho đồng nhất design.",
                                    slot.label, col.get("name"), prev, len(col["jobs"]))
                        for j in col["jobs"]:
                            j["status"] = "pending"
                            j["error"] = None
                            if on_update:
                                res = on_update(j)
                                if asyncio.iscoroutine(res):
                                    await res
                col["ran_on"] = profile
                col["status"] = "running"
                col["worker"] = slot.label
                if on_fleet_update:
                    on_fleet_update(profile, {
                        "status": "busy",
                        "collection": col["id"],
                        "collection_name": col["name"],
                        "prompt_name": col.get("prompt_name", "")
                    })

                ok = False
                try:
                    ok = await self._run_collection_on_slot(slot, page, col, on_update=on_update)
                finally:
                    busy_slots.discard(slot)
                    self._last_active_time = loop.time()
                    self.collection_queue.task_done()

                if not ok and profile in self.exhausted:
                    if on_fleet_update:
                        on_fleet_update(profile, {
                            "status": "exhausted",
                            "reason": self.exhausted[profile],
                            "collection": None
                        })
                    # Hết quota khi đang dở collection -> đẩy lại cho worker khác
                    unfinished = [j for j in col.get("jobs", []) if j.get("status") != "done"]
                    if unfinished:
                        log.warning("[%s] Hết quota khi dở collection '%s'. Đẩy lại hàng đợi.",
                                    slot.label, col["name"])
                        col["status"] = "pending"
                        col["worker"] = None
                        await self.collection_queue.put(col)
                    break
                else:
                    if on_fleet_update:
                        on_fleet_update(profile, {"status": "idle", "collection": None, "collection_name": None})

        # ĐIỀU PHỐI ĐỘNG: cứ thấy slot chưa có worker là tạo worker cho nó ngay.
        # Trước đây dùng gather() cố định trên danh sách slot lúc bắt đầu, nên tab
        # thêm vào giữa chừng (tài khoản mới chọn, hoặc dự bị được gọi) phải chờ
        # TOÀN BỘ đội hình cũ nghỉ mới được chạy - phí cả phút chờ vô ích.
        workers: dict[int, asyncio.Task] = {}
        while True:
            for sl in list(self._slots):
                if id(sl) not in workers:
                    workers[id(sl)] = asyncio.create_task(worker(sl))
            await asyncio.sleep(0.5)
            if not workers or not all(t.done() for t in workers.values()):
                continue                    # còn worker đang chạy -> để yên

            left = self.collection_queue.qsize()
            if left == 0:
                break
            usable = [sl for sl in self._slots
                      if sl.page is not None and sl.profile not in self.exhausted]
            if not usable:
                # Đội hình hết lượt/chết hết -> kéo tài khoản dự bị vào.
                if await self.recruit_reserve() is None:
                    break
                log.warning("Còn %d collection - chạy tiếp bằng tài khoản dự bị.", left)
            else:
                log.warning("Còn %d collection vừa được nạp thêm - chạy tiếp.", left)
            for sl in usable:               # cho mấy slot còn dùng được chạy lại
                workers.pop(id(sl), None)
            self._last_active_time = loop.time()

        await asyncio.gather(*workers.values(), return_exceptions=True)
        self.running_collections = False    # từ đây nạp thêm là bị từ chối thẳng

        # Còn collection chưa chạy: nói ĐÚNG lý do thay vì mặc định đổ cho hết lượt
        if not self.collection_queue.empty():
            if self._slots and all(s.profile in self.exhausted for s in self._slots):
                reason = ("Tất cả tài khoản ChatGPT (kể cả dự bị) đều đã hết lượt "
                          "tạo ảnh.")
            else:
                reason = ("Không còn tài khoản nào chạy được "
                          "(tab lỗi hoặc chưa đăng nhập).")
            while not self.collection_queue.empty():
                col = self.collection_queue.get_nowait()
                col["status"] = "failed"
                col["error"] = reason
                for j in col.get("jobs", []):
                    if j["status"] in ("pending", "running"):
                        j["status"] = "failed"
                        j["error"] = reason
                self.collection_queue.task_done()

