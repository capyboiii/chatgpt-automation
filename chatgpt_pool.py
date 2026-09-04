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
    "đã xảy ra lỗi", "có lỗi xảy ra", "thử lại sau",
)


# Ảnh mockup thật thì to; thumbnail/icon thì không. Ưu tiên đo bằng kích thước
# thật (Pillow); MIN_IMG_BYTES chỉ là phương án chống cháy khi thiếu Pillow.
MIN_IMG_SIDE = 400
MIN_IMG_BYTES = 20_000


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
        if hashlib.sha256(data).hexdigest() in self._ignore:
            log.debug("Bỏ qua ảnh template mình vừa upload: %s", response.url[:80])
            return
        key = response.url.split("?", 1)[0]
        old = self._shots.get(key)
        if old is None:
            self._shots[key] = _Shot(response.url, data, seq, w, h)
        elif old.data is None or len(data) > len(old.data):
            old.data, old.url, old.w, old.h = data, response.url, w, h   # giữ ts đầu


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
            self.warnings.append(msg)
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

    async def _order_shots(self, page: Page, shots: list[_Shot]) -> list[_Shot]:
        """Xếp ảnh theo thứ tự chúng hiện trong câu trả lời.

        Ảnh gen ra tải song song nên thứ tự response về không đáng tin - xếp theo đó
        là gán nhầm (design của cốc lưu vào file áo). DOM thì bày đúng thứ tự.
        Ảnh nào không khớp được thì đẩy xuống cuối, giữ thứ tự response."""
        if len(shots) < 2:
            return shots
        try:
            keys = await page.evaluate(ORDER_JS)
        except Exception:  # noqa: BLE001
            return shots
        rank = {k: i for i, k in enumerate(keys)}
        big = len(rank) + 1
        return sorted(shots, key=lambda sh: (rank.get(sh.url.split("?")[0], big), sh.ts))

    @staticmethod
    def _trim_shots(shots: list[_Shot], want: int) -> list[_Shot]:
        """Dư ảnh thì giữ `want` ảnh NẶNG NHẤT, rồi xếp lại theo thứ tự đến.

        Lúc đang gen, ChatGPT hay tải về bản xem trước nén rất mạnh của cùng một ảnh;
        bản chốt luôn nặng hơn hẳn. Giữ nguyên thứ tự đến để còn gán đúng template
        nào ra ảnh nào."""
        if len(shots) <= want:
            return shots
        log.warning("Nhận %d ảnh cho lô %d ảnh - giữ %d ảnh nặng nhất (%s KB).",
                    len(shots), want, want,
                    ", ".join(str(len(sh.data or b"") // 1024) for sh in shots))
        heavy = sorted(shots, key=lambda sh: len(sh.data or b""), reverse=True)[:want]
        keep = {id(sh) for sh in heavy}
        return [sh for sh in shots if id(sh) in keep]

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

        def trim(shots: list[_Shot]) -> list[_Shot]:
            return self._trim_shots(shots, want)

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
        next_diag = 0.0
        got: list[_Shot] = []
        while loop.time() < deadline:
            busy, srcs = await self._poll(page)
            arm(busy)
            got = best(srcs)
            if not busy and len(got) >= want:
                await page.wait_for_timeout(1_500)   # ảnh cuối kịp tải nốt
                _, srcs2 = await self._poll(page)
                return trim(await self._order_shots(page, best(srcs2)))
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
                if now - quiet_since > 30:
                    if got:      # trả thiếu nhưng có còn hơn không
                        log.warning("ChatGPT chỉ trả %d/%d ảnh.", len(got), want)
                        return trim(await self._order_shots(page, got))
                    raise NoImage("ChatGPT trả lời xong nhưng không có ảnh")
            await page.wait_for_timeout(1_500)

        if got:
            log.warning("Hết giờ, mới có %d/%d ảnh.", len(got), want)
            return trim(await self._order_shots(page, got))
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
    async def run_batch(self, jobs: list[dict], on_update: UpdateCb = None) -> None:
        """Gen cả lượt trong MỘT phiên chat của MỘT tài khoản.

        Vì sao không chia tab nữa: mỗi chat cho ra một hướng design khác nhau, chia
        ảnh ra nhiều chat thì bộ mockup không đồng nhất. Ở đây tất cả ảnh đi cùng
        một cuộc hội thoại: lô đầu mang prompt gốc, các lô sau nối tiếp trong CHÍNH
        chat đó với `followup_prompt` ("giữ nguyên design ở trên") nên ChatGPT vẫn
        nhìn thấy design đã chốt.

        Chia lô chỉ vì ChatGPT có trần số ảnh đính kèm mỗi tin nhắn (`batch_size`).
        Ảnh trả về được gán cho template theo ĐÚNG THỨ TỰ upload.
        """
        async def emit(job):
            if on_update:
                res = on_update(job)
                if asyncio.iscoroutine(res):
                    await res

        async def fail_all(msg: str) -> None:
            for j in jobs:
                if j.get("status") in ("pending", "running"):
                    j["status"] = "failed"
                    j["error"] = msg
                    await emit(j)

        for j in jobs:
            j.setdefault("status", "pending")
            j.setdefault("error", None)

        if not jobs:
            return
        if not self._slots:
            await fail_all("Chưa chọn tài khoản nào")
            return

        slot = self._slots[0]
        page = await slot.wait_ready()
        if page is None:
            await fail_all(f"Không mở được Chrome cho '{slot.profile}': {slot.error}")
            return

        # mở đúng 1 chat sạch cho cả lượt
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
            await fail_all(f"Không mở được cửa sổ chat: {e}")
            return

        seen = set(await self._collect(page))
        chunks = [jobs[i:i + self.batch_size]
                  for i in range(0, len(jobs), self.batch_size)]
        log.info("[%s] gen %d ảnh trong 1 chat (%d lô, mỗi lô tối đa %d).",
                 slot.label, len(jobs), len(chunks), self.batch_size)

        quota: str | None = None
        for ci, chunk in enumerate(chunks):
            if quota:
                break
            for j in chunk:
                j["status"] = "running"
                j["worker"] = slot.label
                await emit(j)

            # `pending` = những ảnh CHƯA lấy được. Mỗi vòng chỉ gửi lại đúng phần
            # còn thiếu chứ không gen lại cả lô: ChatGPT hay lỗi giữa chừng sau khi
            # đã ra được vài ảnh ("Something went wrong"), gen lại từ đầu vừa phí
            # lượt vừa có nguy cơ ra design khác.
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
                    # chỉ tin nhắn đầu tiên mới được phép mở chat mới
                    if not await self._upload(
                            page, tpls, allow_new_chat=(first_msg and round_no == 1)):
                        raise RuntimeError("không đính kèm được ảnh template")
                    if first_msg:
                        text = jobs[0]["prompt"]          # lô đầu: chốt design
                    elif round_no == 1:
                        text = self.followup_prompt       # lô sau: giữ design
                    else:
                        text = self.topup_prompt          # làm nốt phần thiếu
                    await self._send(page, text)
                    first_msg = False
                    imgs = await self._wait_images(page, seen, len(pending), slot.net)
                except QuotaExceeded as e:
                    quota = str(e)
                    imgs = e.images               # ảnh kịp nhận trước khi bị chặn
                    log.error("[%s] %s", slot.label, quota)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    log.warning("[%s] lô %d/%d vòng %d lỗi: %s",
                                slot.label, ci + 1, len(chunks), round_no, e)
                    if _is_dead(e):
                        break
                    # ChatGPT lỗi nhưng vài ảnh đã kịp về -> giữ lấy, đừng vứt
                    have = list(slot.net.shots()) if slot.net else []
                    if not have:
                        # lưới cuối: bắt mạng hụt (ảnh lấy từ cache, body đọc lỗi...)
                        # thì vẫn còn nhìn được DOM, tải lại bằng URL.
                        try:
                            have = [_Shot(u, None, i)
                                    for i, u in enumerate(await self._collect(page))
                                    if u not in seen]
                        except Exception:  # noqa: BLE001
                            have = []
                    if have:
                        imgs = await self._order_shots(page, have)
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
                    log.warning("[%s] còn thiếu %d ảnh -> xin ChatGPT làm nốt.",
                                slot.label, len(pending))
                    await asyncio.sleep(2)

            for j in pending:
                j["status"] = "failed"
                j["error"] = quota or (
                    str(last_err) if last_err else
                    "ChatGPT không trả đủ ảnh sau nhiều lần xin làm nốt")
                await emit(j)

        if quota:
            self.exhausted[slot.profile] = quota
            await fail_all(f"{quota} — chọn tài khoản khác rồi gen phần còn lại")
        else:
            await fail_all("không chạy tới (lượt gen đã dừng)")

        done = sum(1 for j in jobs if j["status"] == "done")
        log.info("[%s] xong %d/%d ảnh.", slot.label, done, len(jobs))
