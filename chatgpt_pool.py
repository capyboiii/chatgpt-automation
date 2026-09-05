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
import re
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
# Nút gỡ một ảnh khỏi khung soạn (dùng để dọn sạch trước khi đính bộ mới)
SEL_ATTACH_REMOVE = ('form button[aria-label*="Remove" i], '
                     'form button[aria-label*="Xoá" i], '
                     'form button[aria-label*="Xóa" i], '
                     'form [data-testid*="remove" i]')
SEL_NEW_CHAT = [
    'a[data-testid="create-new-chat-button"]',
    'button[data-testid="create-new-chat-button"]',
    'nav a[href="/"]',
]

# Mốc nhận diện "đây là lượt trả lời của ChatGPT". ChatGPT đã BỎ
# [data-message-author-role] (ghi nhận 2026-09-05: log báo "DOM không còn
# [data-message-author-role]" trên mọi tab), nên phải thử nhiều mốc:
#   - data-message-author-role : bản cũ
#   - article[data-turn]       : bản hiện tại, data-turn="assistant" | "user"
#   - data-testid="conversation-turn-N" : lớp bọc ngoài, chẵn/lẻ không đáng tin
#     nên chỉ dùng khi hai cái trên đều không có.
# Mất mốc này là mọi thứ tụt xuống nhánh dự phòng: quét cả <main> nên vơ luôn ảnh
# template của chính mình, đọc nhầm chữ lỗi của lượt trước, và ORDER_JS không xếp
# được thứ tự -> đúng loạt cảnh báo "DOM chỉ nhận ra 1/2 ảnh".
TURN_JS = """
    const asstTurns = () => {
        let a = document.querySelectorAll('[data-message-author-role="assistant"]');
        if (a.length) return Array.from(a);
        a = document.querySelectorAll('article[data-turn="assistant"], [data-turn="assistant"]');
        if (a.length) return Array.from(a);
        a = document.querySelectorAll('[data-testid^="conversation-turn"]');
        if (a.length) {
            // Không có data-turn thì loại các lượt CÓ chứa nút sửa tin nhắn của
            // user (chỉ tin của mình mới có), phần còn lại coi là của ChatGPT.
            const out = Array.from(a).filter((el) =>
                !el.querySelector('button[aria-label*="Edit message" i], '
                                + 'button[data-testid="edit-message-button"]'));
            if (out.length) return out;
        }
        return [];
    };
    const userTurns = () => {
        let u = document.querySelectorAll('[data-message-author-role="user"]');
        if (u.length) return Array.from(u);
        return Array.from(document.querySelectorAll(
            'article[data-turn="user"], [data-turn="user"]'));
    };
"""

# 1 lần evaluate lấy CẢ trạng thái đang gen (nút Stop) LẪN danh sách ảnh. Gộp lại
# vì mỗi round-trip Playwright tốn thời gian; nhiều tab cùng poll thì khoản tiết
# kiệm này cộng dồn rất nhanh.
POLL_JS = """() => {""" + TURN_JS + """
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    // "Còn đang gen không" - CHỐT quan trọng nhất: sai cái này là chốt ảnh giữa
    // chừng lúc ChatGPT mới vẽ xong một nửa. Thử nhiều mốc vì ChatGPT đã đổi DOM
    // một lần rồi (mất data-message-author-role).
    let busy = false;
    for (const s of ['button[data-testid="stop-button"]',
                     'button[data-testid*="stop" i]',
                     'button[aria-label*="Stop" i]',
                     'button[aria-label*="Dừng" i]',
                     '[data-testid="stop-generating"]']) {
        if (vis(document.querySelector(s))) { busy = true; break; }
    }
    // Dự phòng: còn ảnh trong câu trả lời chưa nạp xong (naturalWidth = 0) thì
    // vẫn coi là đang chạy, đừng vội chốt.
    if (!busy) {
        const a = asstTurns();
        const last = a.length ? a[a.length - 1] : null;
        if (last) {
            for (const im of last.querySelectorAll('img')) {
                if (im.closest('form')) continue;
                const r = im.getBoundingClientRect();
                if (r.width > 120 && !im.naturalWidth) { busy = true; break; }
            }
        }
    }
    // CHỈ lấy ảnh trong lượt trả lời của ChatGPT. Quét cả trang thì ảnh template
    // mình vừa gửi (trong tin nhắn của user + thumbnail ở composer) cũng bị tính
    // là "ảnh mới" -> đếm đủ số quá sớm và tải nhầm chính ảnh gốc.
    const asst = asstTurns();
    const users = userTurns();
    const roots = asst.length ? asst : [document.querySelector('main') || document.body];
    const imgs = [];
    const seen = new Set();
    for (const root of roots) {
        for (const im of root.querySelectorAll('img')) {
            if (im.closest('form')) continue;                  // thumbnail composer
            if (!asst.length &&                                 // fallback: bỏ ảnh
                users.some((u) => u.contains(im))) continue;    // của user
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
ORDER_JS = r"""() => {""" + TURN_JS + r"""
    // CHỈ tin nhắn trả lời CUỐI CÙNG có ảnh. Quét cả hội thoại thì ảnh của mấy
    // lô trước cũng lọt vào bảng thứ tự, mà lô nào cũng có ảnh -> lệch chỗ.
    const asst = asstTurns();
    let root = null;
    for (let i = asst.length - 1; i >= 0; i--) {
        if (asst[i].querySelector('img')) { root = asst[i]; break; }
    }
    if (!root) root = document.querySelector('main') || document.body;
    const out = [];
    for (const im of root.querySelectorAll('img')) {
        if (im.closest('form')) continue;
        // Trả TẤT CẢ url ứng viên: <img> hay đổi giữa currentSrc / src / srcset
        // (bản thumbnail vs bản gốc), khớp đúng một cái là đủ nhận ra ảnh.
        const cands = [];
        if (im.currentSrc) cands.push(im.currentSrc);
        if (im.src) cands.push(im.src);
        for (const part of (im.getAttribute('srcset') || '').split(',')) {
            const u = part.trim().split(/\s+/)[0];
            if (u) cands.push(u);
        }
        if (!cands.length) continue;
        const r = im.getBoundingClientRect();
        out.push({urls: cands, w: Math.round(r.width), h: Math.round(r.height)});
    }
    return out;
}"""


def _match_order(jobs: list[dict], names: list[str]) -> list[dict] | None:
    """Sắp lại `jobs` theo thứ tự tên file `names` đọc từ khung soạn.

    Trả None khi không ghép chắc chắn được (tên bị cắt ngắn, trùng nhau, thiếu...)
    - lúc đó thà giữ nguyên thứ tự cũ còn hơn sắp bừa.
    """
    if len(names) != len(jobs):
        return None

    def norm(x: str) -> str:
        return re.sub(r"\s+", " ", (x or "").strip().lower()).rstrip(". …")

    remaining = list(jobs)
    out: list[dict] = []
    for raw in names:
        n = norm(raw)
        if not n:
            return None
        hits = [j for j in remaining if norm(Path(j["template"]).name) == n]
        if not hits:      # tên trên giao diện có thể bị cắt bớt đuôi
            hits = [j for j in remaining
                    if norm(Path(j["template"]).name).startswith(n)
                    or n.startswith(norm(Path(j["template"]).stem))]
        if len(hits) != 1:
            return None
        out.append(hits[0])
        remaining.remove(hits[0])
    return out if not remaining else None


def _img_key(url: str) -> str:
    """Khoá nhận dạng một ảnh, chịu được việc URL đổi dạng.

    Cùng một ảnh nhưng tầng mạng thấy '.../files/file-AbC123/raw?se=...&sig=...'
    còn DOM lại bày '.../files/file-AbC123/thumb' - so nguyên đường dẫn là trượt,
    rồi rơi xuống nhánh đoán mò và đặt sai tên file. File-id thì luôn giống nhau.
    """
    u = (url or "").split("#")[0]
    m = re.search(r"file[-_][A-Za-z0-9]{6,}", u)
    if m:
        return m.group(0)
    m = re.search(r"[?&]file_id=([^&]+)", u)
    if m:
        return m.group(1)
    return u.split("?")[0].rsplit("/", 1)[-1] or u


# ---- mức độ suy nghĩ (đọc từ DOM thật, 2026-09) ----------------------------
# Nút hiện mức nằm trong khung soạn: <button class="__composer-pill"
# aria-haspopup="menu"> với chữ là mức hiện tại ("High" / "Vừa"...). Bấm vào mở
# menu data-testid="composer-intelligence-picker-content", bên trong KHÔNG phải
# danh sách nút mà là THANH TRƯỢT 4 nấc:
#     role="slider" aria-valuemin=0 aria-valuemax=3   ("High" = nấc 2)
# Đổi nấc bằng phím mũi tên trái/phải; tên mức hiện ra trong chữ của menu.
PILL_SEL = 'button.__composer-pill[aria-haspopup="menu"]'
PICKER_SEL = '[data-testid="composer-intelligence-picker-content"]'
SLIDER_SEL = '[role="slider"]'

THINK_ALIASES = {
    "vừa": ("medium", "vừa", "standard", "trung bình"),
    "cao": ("high", "cao"),
    "nhanh": ("fast", "light", "instant", "nhanh", "thấp"),
    "tối đa": ("max", "pro", "highest", "tối đa"),
}


# Đếm ảnh đang đính kèm trong khung soạn + còn đang upload không.
ATTACH_JS = """() => {
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
}"""


# Ảnh ĐANG HIỂN THỊ trong lượt trả lời cuối, theo ĐÚNG thứ tự trên màn hình, kèm
# kích thước THẬT của ảnh (naturalWidth) để loại thumbnail/icon.
#
# Đây là NGUỒN SỰ THẬT DUY NHẤT về "ChatGPT trả mấy ảnh, ảnh nào trước ảnh nào sau".
# Tầng mạng thấy cả bản dựng dở lẫn bản vẽ lại - những thứ không bao giờ lên màn
# hình - nên đếm và xếp theo nó là phải đoán, mà đoán thì có lúc trúng lúc trượt.
FINAL_IMAGES_JS = r"""() => {""" + TURN_JS + r"""
    const asst = asstTurns();
    let root = null;
    for (let i = asst.length - 1; i >= 0; i--) {
        if (asst[i].querySelector('img')) { root = asst[i]; break; }
    }
    if (!root) return [];
    const out = [];
    const seen = new Set();
    for (const im of root.querySelectorAll('img')) {
        if (im.closest('form')) continue;
        const src = im.currentSrc || im.src || '';
        if (!src || seen.has(src)) continue;
        const nw = im.naturalWidth || 0, nh = im.naturalHeight || 0;
        const r = im.getBoundingClientRect();
        seen.add(src);
        out.push({src, nw, nh, w: Math.round(r.width), h: Math.round(r.height)});
    }
    return out;
}"""


# Tên file của các ảnh ĐANG đính kèm, theo ĐÚNG thứ tự chúng nằm trong khung soạn.
# Đây mới là thứ tự ChatGPT thật sự nhận được, và nó KHÔNG chắc trùng thứ tự mình
# truyền vào set_input_files: trình duyệt upload song song nên khung soạn xếp theo
# thứ tự upload xong. Không đối chiếu lại là ảnh túi lưu thành tên ly giữ nhiệt.
ATTACH_ORDER_JS = """() => {
    const form = document.querySelector('form') || document.body;
    const pick = (el) => {
        const a = el.getAttribute && (el.getAttribute('alt')
                  || el.getAttribute('title') || el.getAttribute('aria-label'));
        const t = (a || el.innerText || '').split('\\n')[0].trim();
        return t;
    };
    let nodes = form.querySelectorAll('[data-testid*="attachment" i]');
    if (!nodes.length) {
        nodes = form.querySelectorAll('img[src^="blob:"], img[src^="data:"]');
    }
    const out = [];
    for (const n of nodes) {
        const name = pick(n);
        if (name) out.push(name);
    }
    return out;
}"""


# Lấy phần chữ CÓ Ý NGHĨA để đoán lỗi: hộp thoại + 2 lượt trả lời cuối. Không quét
# cả trang vì sidebar luôn có chữ "Upgrade plan" -> dễ báo nhầm hết lượt.
TAIL_JS = """() => {""" + TURN_JS + """
    const parts = [];
    const dlg = document.querySelector('[role="dialog"]');
    if (dlg) parts.push(dlg.innerText || '');
    const last = asstTurns().slice(-2);
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

# ChatGPT hay VẼ LẠI một ảnh ở phút chót (đổi vài chi tiết nền). Cả hai bản đều
# được tải về, khác byte -> khác sha256 -> tool tưởng là HAI ảnh khác nhau. Với lô
# 2 ảnh mà bắt được 4 luồng, khâu chốt phải đoán, và nó đã đoán sai: giữ lại đúng
# hai bản của CÙNG một cái túi, vứt mất ảnh áo, rồi đặt cho chúng hai tên khác nhau.
#
# Băm tri giác (dHash 16x16 = 256 bit) phân biệt được hai chuyện đó. Đo trên 62 ảnh
# thật đã gen: hai BẢN của cùng một ảnh cách nhau 8-31 bit, hai ảnh KHÁC NHAU gần
# nhất cũng cách 69 bit. Lấy 45 làm ranh giới thì rộng rãi cho cả hai phía.
DUP_HASH_DIST = 45


def _phash(im) -> int | None:
    """dHash 16x16 của một ảnh Pillow đã mở."""
    try:
        g = im.convert("L").resize((17, 16), Image.LANCZOS)
        px = list(g.getdata())
        bits = 0
        for r in range(16):
            row = r * 17
            for c in range(16):
                bits = (bits << 1) | (px[row + c] < px[row + c + 1])
        return bits
    except Exception:  # noqa: BLE001
        return None
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

    __slots__ = ("url", "data", "ts", "w", "h", "ph")

    def __init__(self, url: str, data: bytes | None, seq: float,
                 w: int = 0, h: int = 0, ph: int | None = None):
        # seq = số thứ tự response về; dùng để xếp ảnh đúng thứ tự ChatGPT trả ra
        self.url, self.data, self.ts, self.w, self.h = url, data, seq, w, h
        self.ph = ph          # băm tri giác, để nhận ra hai bản của cùng một ảnh


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
        self._logged_dup: set = set()       # url đã báo "vẽ lại" rồi, khỏi log lặp
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
        self._logged_dup.clear()

    def shots(self) -> list[_Shot]:
        """Ảnh theo ĐÚNG thứ tự response về, ĐÃ GỘP các bản vẽ lại của cùng một ảnh.

        Gộp ở ĐÂY chứ không phải lúc chốt, vì số lượng ảnh quyết định cả việc "đã
        đủ chưa": 2 ảnh mà đếm thành 4 thì vòng chờ dừng sớm ngay khi mới có hai
        bản của cùng một tấm.

        Mỗi nhóm giữ VỊ TRÍ của bản đầu (đó là chỗ ChatGPT đặt tấm ảnh này trong
        câu trả lời) nhưng lấy BYTES của bản cuối (bản vẽ lại là bản chốt)."""
        ordered = sorted(self._shots.values(), key=lambda s: s.ts)
        groups: list[_Shot] = []
        for sh in ordered:
            if sh.ph is None:
                groups.append(sh)
                continue
            for i, g in enumerate(groups):
                if g.ph is not None and bin(g.ph ^ sh.ph).count("1") <= DUP_HASH_DIST:
                    if sh.url not in self._logged_dup:
                        # shots() bị gọi mỗi nhịp poll -> chỉ báo một lần cho mỗi ảnh
                        self._logged_dup.add(sh.url)
                        log.info("ChatGPT vẽ lại một ảnh - gộp làm một, giữ bản mới "
                                 "(bỏ %s).", g.url.split("?")[0][-46:])
                    groups[i] = _Shot(sh.url, sh.data, g.ts, sh.w, sh.h, sh.ph)
                    break
            else:
                groups.append(sh)
        return groups

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
        ph = None
        if Image is not None:
            # KÍCH THƯỚC THẬT mới là thước đo đáng tin. Dung lượng thì tuỳ nội dung:
            # một mockup nền trắng nén rất nhẹ, chặn theo KB là loại nhầm ảnh thật.
            try:
                with Image.open(BytesIO(data)) as im:
                    w, h = im.size
                    if min(w, h) >= MIN_IMG_SIDE:
                        ph = _phash(im)
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
        self._shots[digest] = _Shot(response.url, data, seq, w, h, ph)
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


class Refused(NoImage):
    """ChatGPT từ chối vì nội dung/chính sách.

    Tách riêng khỏi NoImage vì cách xử KHÁC HẲN: lỗi tạm thì xin làm lại có cơ
    hội qua, còn từ chối thì gửi lại đúng prompt đó bao nhiêu lần cũng bị từ chối
    y như vậy - retry chỉ tổ mất mấy phút rồi vẫn fail."""


def _classify(text: str) -> tuple[str, str, str]:
    """(loại lỗi, câu trích, MẪU đã khớp) từ phần chữ cuối của trang."""
    flat = " ".join(text.split())
    low = flat.lower()
    for pats, kind in ((QUOTA_PAT, "quota"),
                       (REFUSE_PAT, "refused"),
                       (TEMP_ERR_PAT, "error")):
        for p in pats:
            i = low.find(p)
            if i >= 0:
                return kind, flat[max(0, i - 60): i + 160].strip(), p
    return "", flat[-160:].strip(), ""


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
        # <= 0 = gửi CẢ BỘ trong một tin nhắn (mặc định, xem chú thích ở chỗ chia
        # lô trong _run_collection_on_slot). Số dương = trần số ảnh mỗi tin nhắn.
        self.batch_size = int(r.get("batch_size", 0) or 0)
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
        # Tài khoản "đứng hình": mở chat rồi mà không nhúc nhích (không đính kèm
        # được ảnh, không gửi được...). Khác hết lượt nhưng cùng cách xử: ngừng
        # giao việc, đẩy collection sang tài khoản khác.
        self.stalled: dict[str, str] = {}
        # Tạo NGAY từ đầu. Trước đây queue chỉ ra đời khi run_collections chạy, mà
        # lúc đó Chrome còn đang khởi động vài giây -> collection nạp thêm trong
        # khoảng đó rơi vào hư không, server vẫn báo "đã xếp hàng".
        self.collection_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._last_active_time: float = 0.0
        self.running_collections = False   # đang ở chế độ collections (nạp thêm được)
        self.stopped: bool = False         # cờ dừng khẩn cấp
        # True = giữ ảnh đã gen khi collection phải chuyển sang tài khoản khác
        # (nhanh hơn nhưng bộ ảnh sẽ pha trộn 2 hướng design)
        self.resume_partial = bool(r.get("resume_partial_on_other_account", False))
        # Sau khi hết việc trong hàng đợi, worker chỉ cần chờ 2-3s xác nhận không
        # còn collection nào nạp thêm là hoàn tất ngay, không bắt người dùng chờ.
        self.idle_exit = float(r.get("idle_exit_seconds", 2))
        # đủ ảnh rồi vẫn phải thấy danh sách ảnh đứng yên chừng này giây mới chốt
        # 5s: đủ để bắt cú "ChatGPT vẽ lại ảnh ở phút chót" (lần nào cũng mất hơn
        # 20s mới ra ảnh thay), mà không bắt mỗi tin nhắn phải đứng chờ 8 giây.
        self.settle_seconds = float(r.get("settle_seconds", 5))
        # Quá ngần này giây mà một collection chưa ra nổi ảnh nào -> coi tài khoản
        # là đứng hình, bỏ sang tài khoản khác thay vì ngồi thử lại đủ 4 vòng.
        self.stall_timeout = float(r.get("stall_timeout", 120))
        # mức suy nghĩ đặt trước khi gửi: vừa | cao | nhanh | tối đa ("" = không đụng)
        self.thinking = str(r.get("thinking", "vừa") or "").strip().lower()
        # Thời gian im lặng tối đa khi ChatGPT trả thiếu ảnh: nếu đã có ít nhất 1 ảnh
        # và ChatGPT ngừng tạo quá 10s, lập tức lưu ảnh và gửi yêu cầu gen tiếp.
        self.quiet_limit_got = float(r.get("quiet_limit_seconds", 10))
        # Trần cứng cho MỘT lô, bất kể lô có bao nhiêu ảnh. Trước đây ngân sách là
        # generation_timeout * số ảnh = 300 x 6 = 30 PHÚT: ChatGPT ra được 1 ảnh
        # rồi treo là tài khoản đó ngồi không nửa tiếng, mà stall_timeout không
        # cứu được vì nó chỉ tính khi CHƯA ra nổi ảnh nào.
        self.batch_timeout = float(r.get("batch_timeout", 900))
        # Bao lâu KHÔNG có thêm ảnh mới thì coi như chết hẳn. Đây mới là cái chốt
        # thật sự: đang ra ảnh đều thì chạy tiếp thoải mái, ngừng ra là cắt sớm.
        self.no_progress_timeout = float(r.get("no_progress_timeout", 180))

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

    def blocked(self, profile: str) -> str | None:
        """Lý do tài khoản này không nhận việc nữa (hết lượt / đứng hình)."""
        return self.exhausted.get(profile) or self.stalled.get(profile)

    def add_profiles(self, names: list[str]) -> int:
        """Bổ sung tài khoản vào đội hình đang chạy (dùng khi UI chọn thêm acc).

        Chỉ tạo slot + task boot; bộ điều phối trong `run_collections` thấy slot mới
        là tự sinh worker trong vòng nửa giây, không phải chờ ai nghỉ.
        """
        added = 0
        for name in names:
            if not name or self.blocked(name):
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

    def stop(self) -> None:
        """Dừng khẩn cấp: hủy hàng đợi và ngắt các worker."""
        self.stopped = True
        cleared = 0
        while not self.collection_queue.empty():
            try:
                col = self.collection_queue.get_nowait()
                col["status"] = "paused"
                for j in col.get("jobs", []):
                    if j.get("status") in ("pending", "running"):
                        j["status"] = "paused"
                self.collection_queue.task_done()
                cleared += 1
            except (asyncio.QueueEmpty, ValueError):
                break
        log.warning("Đã kích hoạt DỪNG KHẨN CẤP: xả %d collection khỏi hàng đợi.", cleared)

    async def recruit_reserve(self) -> _Slot | None:
        """Bật thêm 1 tài khoản dự bị và trả về slot của nó.

        Dùng khi đội hình đang chạy hết lượt: thay vì để collection chết trong
        hàng đợi, kéo một tài khoản rảnh đã đăng nhập vào chạy tiếp.
        """
        while self.reserves:
            name = self.reserves.pop(0)
            if self.blocked(name) or any(s.profile == name for s in self._slots):
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

    async def _set_thinking(self, page: Page) -> bool:
        """Đặt mức suy nghĩ trước khi gửi. Đúng mức rồi thì không đụng vào.

        Hỏng kiểu gì cũng chỉ ghi log rồi chạy tiếp - không được làm chết lượt gen.
        """
        want = self.thinking
        if not want:
            return True
        aliases = THINK_ALIASES.get(want, (want,))
        try:
            pill = page.locator(PILL_SEL).first
            await pill.wait_for(state="visible", timeout=8_000)
            cur = (await pill.inner_text() or "").strip().lower()
            if any(a in cur for a in aliases):
                return True                       # đang đúng mức rồi

            await pill.click(timeout=5_000)
            await page.locator(PICKER_SEL).first.wait_for(state="visible", timeout=5_000)
            slider = page.locator(SLIDER_SEL).first
            await slider.focus()

            async def level_now() -> str:
                txt = await page.locator(PICKER_SEL).first.inner_text()
                return (txt or "").strip().splitlines()[0].strip().lower()[:20]

            for _ in range(4):                    # về nấc thấp nhất cho chắc mốc
                await slider.press("ArrowLeft")
                await page.wait_for_timeout(120)

            # `any(... await ...)` trong generator sẽ tạo async generator và vỡ ->
            # phải await ra biến trước rồi mới so.
            hit = False
            for _ in range(4):                    # rồi bò lên tới đúng mức
                lvl = await level_now()
                if any(a in lvl for a in aliases):
                    hit = True
                    break
                await slider.press("ArrowRight")
                await page.wait_for_timeout(180)
            if not hit:
                lvl = await level_now()
                hit = any(a in lvl for a in aliases)

            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            now = (await pill.inner_text() or "").strip()
            if hit or any(a in now.lower() for a in aliases):
                log.info("Đã đặt mức suy nghĩ: %s", now)
                return True
            msg = f"Không đặt được mức suy nghĩ '{want}' (đang là '{now}')"
            log.warning(msg)
            self.warnings.append(msg)
            return False
        except Exception as e:  # noqa: BLE001
            msg = f"Không đặt được mức suy nghĩ ({str(e)[:80]})"
            log.warning(msg)
            self.warnings.append(msg)
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            return False

    async def _attached_names(self, page: Page) -> list[str]:
        """Tên file các ảnh đang đính kèm, theo đúng thứ tự trong khung soạn."""
        try:
            return [str(x) for x in (await page.evaluate(ATTACH_ORDER_JS) or [])]
        except Exception:  # noqa: BLE001
            return []

    async def _count_attached(self, page: Page) -> int:
        """Số ảnh đang đính kèm trong khung soạn."""
        try:
            return int((await page.evaluate(ATTACH_JS)).get("n", 0))
        except Exception:  # noqa: BLE001
            return 0

    async def _clear_attachments(self, page: Page) -> bool:
        """Gỡ sạch ảnh còn sót trong khung soạn TRƯỚC khi đính bộ mới.

        VÌ SAO PHẢI CÓ: `set_input_files` chỉ THÊM ảnh vào khung soạn, không thay
        thế. Lần đính kèm trước hụt giờ (mới lên 3/6 ảnh) hoặc gửi hụt là mấy ảnh
        đó nằm nguyên đấy; vòng sau đính thêm 6 nữa thành 9. Điều kiện "n >= want"
        vẫn thoả nên tool tưởng ngon, gửi đi 9 ảnh, ChatGPT làm đủ 9, rồi tool lấy
        6 ảnh ĐẦU -> 3 ảnh trùng của lượt hỏng + 3 ảnh đầu của lô, và TOÀN BỘ tên
        file lệch. Đây là đường ngắn nhất dẫn tới 'ảnh cốc mang tên áo'.
        """
        if await self._count_attached(page) == 0:
            return True
        for _ in range(30):                     # tối đa 30 ảnh, gỡ từng cái
            try:
                btn = page.locator(SEL_ATTACH_REMOVE).first
                if not await btn.count():
                    break
                await btn.click(timeout=2_000)
                await page.wait_for_timeout(160)
            except Exception:  # noqa: BLE001
                break
        left = await self._count_attached(page)
        if left:
            log.warning("Còn %d ảnh cũ trong khung soạn không gỡ được.", left)
            return False
        log.info("Đã gỡ ảnh cũ còn sót trong khung soạn.")
        return True

    async def _attach_via_chooser(self, page: Page, files: list[str]) -> bool:
        """Dự phòng: bấm nút "+" rồi hứng hộp thoại chọn file.

        Không phụ thuộc cấu trúc DOM, dùng khi mọi input[type=file] đều không ăn.
        """
        try:
            btn = page.get_by_role(
                "button",
                name=re.compile(r"add files|attach|thêm tệp|đính kèm", re.I)).first
            async with page.expect_file_chooser(timeout=10_000) as fc:
                await btn.click(timeout=5_000)
                try:                       # có bản hiện menu con trước
                    item = page.get_by_role(
                        "menuitem", name=re.compile(r"photo|file|ảnh|tệp", re.I)).first
                    await item.click(timeout=3_000)
                except Exception:  # noqa: BLE001
                    pass
            chooser = await fc.value
            await chooser.set_files(files)
            log.info("Đính kèm qua hộp thoại chọn file.")
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("Đính kèm qua file chooser không được: %s", e)
            return False

    async def _attach_once(self, page: Page, templates: list[Path], wait_s: int) -> bool:
        """Nạp ảnh vào khung soạn rồi chờ đủ thumbnail upload xong.

        QUAN TRỌNG: trang ChatGPT có nhiều input[type=file], trong đó mấy cái
        `mobile-composer-*` là của khung soạn BẢN MOBILE. Nhét ảnh vào đó thì khung
        desktop không nhận gì: composer trống trơn, tool ngồi chờ thumbnail không
        bao giờ tới rồi treo. Vì vậy phải thử LẦN LƯỢT từng input (ưu tiên cái không
        phải mobile) và kiểm tra thật sự có thumbnail chưa, thay vì tin cái đầu tiên.
        """
        want = len(templates)
        files = [str(t) for t in templates]
        # Dọn trước, luôn luôn. Đính đè lên đống cũ là hỏng hết thứ tự (xem
        # _clear_attachments).
        await self._clear_attachments(page)
        try:
            await page.locator(SEL_FILE_INPUT).first.wait_for(state="attached",
                                                              timeout=10_000)
        except PWTimeout:
            log.warning("Không thấy input[type=file] nào.")

        ranked = []
        for inp in await page.locator(SEL_FILE_INPUT).all():
            try:
                ident = ((await inp.get_attribute("id")) or "").lower()
            except Exception:  # noqa: BLE001
                ident = ""
            if "camera" in ident:
                continue                            # chụp ảnh, không dùng được
            ranked.append((1 if ident.startswith("mobile-") else 0, ident, inp))
        ranked.sort(key=lambda x: x[0])

        attached = False
        for _, ident, inp in ranked:
            try:
                await inp.set_input_files(files)
            except Exception:  # noqa: BLE001
                continue
            for _ in range(10):                     # ~5s xem input này có ăn không
                if await self._count_attached(page) >= 1:
                    attached = True
                    break
                await page.wait_for_timeout(500)
            if attached:
                log.debug("Đính kèm được qua input '%s'.", ident or "(không id)")
                break
            log.info("Input '%s' không nhận ảnh - thử input khác.",
                     ident or "(không id)")
            # Có thể input đó VẪN ăn, chỉ là upload chậm hơn 5s. Không dọn thì lát
            # nữa ảnh của nó hiện ra cộng với ảnh của input sau -> nhân đôi cả lô.
            await self._clear_attachments(page)

        if not attached and not await self._attach_via_chooser(page, files):
            log.warning("Không nhét được ảnh vào khung soạn (đã thử %d input).",
                        len(ranked))
            return False

        # chờ ĐỦ số thumbnail hiện + không còn spinner nào
        deadline = asyncio.get_event_loop().time() + wait_s
        last_n = -1
        while asyncio.get_event_loop().time() < deadline:
            st = await page.evaluate(ATTACH_JS)
            if st["n"] != last_n:
                last_n = st["n"]
                log.debug("đính kèm %d/%d", st["n"], want)
            if st["n"] == want and not st["uploading"]:
                return True
            if st["n"] > want and not st["uploading"]:
                # Thừa ảnh = còn sót của lượt trước hoặc đính hai lần. Gửi đi là
                # sai tên cả lô, thà dọn rồi làm lại từ đầu.
                log.warning("Khung soạn có %d ảnh trong khi lô chỉ cần %d - dọn lại.",
                            st["n"], want)
                return False
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

    async def _user_msgs(self, page: Page) -> int:
        """Số tin nhắn của mình trong hội thoại - mốc để biết đã gửi đi thật chưa."""
        try:
            return await page.locator('[data-message-author-role="user"]').count()
        except Exception:  # noqa: BLE001
            return -1

    async def _sent_ok(self, page: Page, before: int, timeout: float = 12.0) -> bool:
        """Tin nhắn đã RỜI khung soạn chưa.

        Dấu hiệu chắc nhất: có thêm một tin nhắn của user. Dự phòng: khung soạn
        sạch ảnh (ChatGPT chỉ xoá đính kèm khi đã gửi thành công)."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if before >= 0 and await self._user_msgs(page) > before:
                return True
            if await self._count_attached(page) == 0:
                return True
            await page.wait_for_timeout(400)
        return False

    async def _send(self, page: Page, prompt: str) -> None:
        before = await self._user_msgs(page)
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

        # XÁC NHẬN đã gửi. Trước đây bấm xong là coi như xong: gửi hụt thì phải chờ
        # hết 30s pha 1 của _wait_images mới biết, mà tệ hơn là bộ ảnh vẫn treo
        # trong khung soạn để vòng sau đính chồng lên.
        if await self._sent_ok(page, before):
            return
        log.warning("Bấm gửi rồi mà tin nhắn chưa đi - thử Enter lần nữa.")
        try:
            await box.click()
            await page.keyboard.press("Enter")
        except Exception:  # noqa: BLE001
            pass
        if await self._sent_ok(page, before, timeout=8.0):
            return
        # Ném lỗi để vòng retry lo: vòng sau _attach_once sẽ dọn sạch khung soạn
        # trước khi đính lại, nên không bị cộng dồn.
        raise RuntimeError("không gửi được tin nhắn (nút gửi không ăn)")

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
            msg = ("Không nhận ra được lượt trả lời của ChatGPT (DOM đã đổi) - "
                   "đang quét cả trang, nhận diện ảnh kém chính xác hơn.")
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

    async def _diagnose(self, page: Page) -> tuple[str, str, str]:
        """Đọc chữ cuối trang để biết vì sao chưa có ảnh."""
        return _classify(await self._tail(page))

    async def _raise_if_bad(self, page: Page, stale: str | None = None) -> None:
        """`stale` = chữ đã có TRƯỚC khi gửi tin này.

        Thiếu nó thì vòng xin làm nốt sẽ đọc lại đúng dòng "Something went wrong"
        của lượt hỏng trước rồi fail ngay, dù lượt mới đang chạy ngon lành."""
        text = await self._tail(page)
        kind, msg, pat = _classify(text)
        # Lỗi tạm / từ chối là chuyện của TỪNG LƯỢT: câu lỗi ĐÃ CÓ SẴN trước khi
        # gửi thì đó là tàn dư của lượt cũ, bỏ qua. So khớp theo MẪU chứ không so
        # nguyên khối chữ: khối chữ luôn đổi vài ký tự (đồng hồ "Worked for 2m 35s",
        # nút "Show more") nên so nguyên khối là không bao giờ khớp, và tool báo
        # lỗi của lượt trước cho lượt đang chạy ngon lành.
        # Riêng HẾT LƯỢT thì không hoãn - nó có thể hiện ra ngay trước khi mình kịp
        # chụp lại màn hình, mà bỏ sót là gửi tiếp trong vô vọng.
        if kind != "quota" and pat and stale and pat in " ".join(stale.split()).lower():
            return
        if kind == "quota":
            raise QuotaExceeded(f"tài khoản hết lượt tạo ảnh — {msg}")
        if kind == "refused":
            raise Refused(f"ChatGPT từ chối tạo ảnh — {msg}")
        if kind == "error":
            raise NoImage(f"ChatGPT báo lỗi — {msg}")

    async def _dom_order(self, page: Page) -> dict[str, int]:
        """khoá ảnh -> vị trí, theo ĐÚNG thứ tự ảnh hiện trong câu trả lời cuối.

        Đây là căn cứ duy nhất đáng tin để biết ảnh nào ứng với template nào:
        ChatGPT bày ảnh theo đúng thứ tự file mình đính kèm, còn thứ tự response
        về thì không - trình duyệt tải mấy ảnh song song, ảnh nhẹ về trước."""
        try:
            items = await page.evaluate(ORDER_JS)
        except Exception:  # noqa: BLE001
            return {}
        rank: dict[str, int] = {}
        idx = 0
        for it in items or []:
            if 0 < (it.get("w") or 0) < 64:      # avatar / icon
                continue
            keys = {_img_key(u) for u in it.get("urls") or [] if u}
            if not keys or any(k in rank for k in keys):
                continue                          # cùng một ảnh bày hai chỗ
            for k in keys:
                rank[k] = idx
            idx += 1
        return rank

    async def _screen_images(self, page: Page) -> list[dict]:
        """Ảnh đang hiển thị trong câu trả lời cuối, theo thứ tự, đã bỏ icon nhỏ."""
        try:
            raw = await page.evaluate(FINAL_IMAGES_JS) or []
        except Exception:  # noqa: BLE001
            return []
        out = []
        for it in raw:
            nw, nh = int(it.get("nw") or 0), int(it.get("nh") or 0)
            # naturalWidth là kích thước THẬT của file ảnh, không phải cỡ hiển thị:
            # ChatGPT bày ảnh 1024px trong khung 200px, lọc theo cỡ hiển thị là
            # vứt nhầm ảnh thật.
            if nw and nh and min(nw, nh) < MIN_IMG_SIDE:
                continue
            if not nw and (it.get("w") or 0) < 120:
                continue                      # chưa nạp xong mà lại bé -> icon
            out.append(it)
        return out

    async def _bytes_of(self, page: Page, src: str) -> bytes | None:
        """Tải đúng tấm ảnh đang hiển thị. Chạy fetch TRONG trang nên mang cookie
        đăng nhập, và đọc được cả `blob:` do chính trang tạo ra."""
        try:
            if src.startswith("data:"):
                return base64.b64decode(src.split(",", 1)[1])
            b64 = await page.evaluate(
                """async u => {
                    const r = await fetch(u, {credentials:'include'});
                    if (!r.ok) return null;
                    const b = await r.blob();
                    return await new Promise(res=>{const f=new FileReader();
                        f.onload=()=>res(f.result.split(',')[1]);f.readAsDataURL(b);});
                }""", src)
            return base64.b64decode(b64) if b64 else None
        except Exception as e:  # noqa: BLE001
            log.debug("Không tải được ảnh %s: %s", src[:70], e)
            return None

    async def _from_screen(self, page: Page, shots: list[_Shot],
                           want: int) -> list[_Shot] | None:
        """Dựng danh sách kết quả TỪ MÀN HÌNH: thứ tự và danh tính đều lấy từ DOM.

        Ảnh nào đã bắt được ở tầng mạng thì dùng lại bytes đó (khỏi tải lần nữa);
        ảnh nào không khớp - điển hình là ảnh vừa gen xong ChatGPT hiển thị bằng
        `blob:` - thì tải thẳng từ trang. Không khớp nổi cái nào cũng không sao,
        vì bytes luôn lấy được.

        Trả None khi màn hình không cho đủ `want` ảnh, để caller quay về cách cũ."""
        dom = await self._screen_images(page)
        if len(dom) < want:
            return None

        by_key = {}
        for sh in shots:
            if sh.data:
                by_key.setdefault(_img_key(sh.url), sh)

        out: list[_Shot] = []
        reused = 0
        for i, it in enumerate(dom[:want]):
            src = it["src"]
            sh = by_key.get(_img_key(src))
            if sh is not None and sh.data:
                reused += 1
                out.append(_Shot(src, sh.data, i, sh.w, sh.h, sh.ph))
                continue
            data = await self._bytes_of(page, src)
            if not data:
                log.warning("Ảnh thứ %d trên màn hình không tải được (%s).",
                            i + 1, src[:60])
                return None
            w = h = 0
            ph = None
            if Image is not None:
                try:
                    with Image.open(BytesIO(data)) as im:
                        w, h = im.size
                        ph = _phash(im)
                except Exception:  # noqa: BLE001
                    pass
            out.append(_Shot(src, data, i, w, h, ph))

        log.info("Lấy %d ảnh theo đúng thứ tự trên màn hình (%d dùng lại bytes đã "
                 "bắt, %d tải thêm).", len(out), reused, len(out) - reused)
        return out

    async def _finalize(self, page: Page, shots: list[_Shot], want: int) -> list[_Shot]:
        """Chốt danh sách ảnh: xếp đúng thứ tự và bỏ ảnh thừa.

        Căn cứ là ẢNH ĐANG HIỆN TRÊN MÀN HÌNH lúc chốt. ChatGPT hay vẽ lại một ảnh ở
        phút chót; bản bị thay vẫn nằm trong mớ response đã bắt nhưng KHÔNG còn trong
        DOM nữa - đó là cách phân biệt chắc chắn. Trước đây chỗ này đoán bằng "ảnh nào
        nặng hơn", mà 4 ảnh cùng cỡ 2.4-2.7 MB thì đoán là hên xui.

        Chỉ khi DOM không cho đủ thông tin mới quay về suy đoán theo dung lượng.
        """
        # ƯU TIÊN TUYỆT ĐỐI: lấy đúng những ảnh đang hiển thị, theo thứ tự hiển
        # thị. Chỉ khi màn hình không cho đủ ảnh mới quay về ghép với tầng mạng.
        for attempt in range(4):
            picked = await self._from_screen(page, shots, want)
            if picked is not None:
                return picked
            if attempt < 3:
                await page.wait_for_timeout(1_200)

        if len(shots) <= 1:
            return shots

        # DOM có thể còn đang gắn ảnh (lazy-load) ngay lúc mình chốt. Chờ thêm vài
        # nhịp còn hơn rơi xuống nhánh đoán mò - nhánh đó xếp theo thứ tự response
        # về, tức là sai thứ tự upload, tức là đặt nhầm tên giữa các loại sản phẩm.
        rank: dict[str, int] = {}
        on_screen: list[_Shot] = []
        for attempt in range(4):
            rank = await self._dom_order(page)
            on_screen = [sh for sh in shots if _img_key(sh.url) in rank]
            on_screen.sort(key=lambda sh: rank[_img_key(sh.url)])
            if len(on_screen) >= min(want, len(shots)):
                break
            if attempt < 3:
                await page.wait_for_timeout(1_200)

        if len(on_screen) >= want:
            if len(shots) > want:
                log.info("Bỏ %d ảnh đã bị ChatGPT thay / không còn hiển thị.",
                         len(shots) - want)
            return on_screen[:want]

        # DOM thiếu (ảnh lazy-load, DOM đổi cấu trúc...) -> ghép nốt theo thứ tự
        # response, và chỉ tới lúc này mới phải suy đoán bằng dung lượng.
        rest = sorted((sh for sh in shots if _img_key(sh.url) not in rank),
                      key=lambda sh: sh.ts)
        merged = on_screen + rest
        if rest:
            # Cảnh báo to: từ đây trở xuống thứ tự là thứ tự RESPONSE VỀ, không
            # đảm bảo trùng thứ tự upload -> ảnh có thể bị gán nhầm template.
            log.warning("DOM chỉ nhận ra %d/%d ảnh - %d ảnh phải xếp theo thứ tự "
                        "response, có nguy cơ gán nhầm template.",
                        len(on_screen), len(shots), len(rest))
        if len(merged) <= want:
            return merged
        log.warning("Giữ %d ảnh nặng nhất trong %d ảnh (%s KB).", want, len(merged),
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

        # pha 2: chờ đủ ảnh. HAI mốc dừng, cái nào tới trước thì thôi:
        #   - deadline: trần cứng cho cả lô (min của gen_timeout*want và batch_timeout)
        #   - idle_deadline: cuộn theo tiến độ, cứ có thêm một ảnh là gia hạn tiếp.
        # Nhờ mốc thứ hai mà lô "ra 1 ảnh rồi treo" chết sau vài phút chứ không
        # ngồi hết cả trần.
        budget = min(self.gen_timeout * want, self.batch_timeout)
        deadline = loop.time() + budget
        idle_deadline = loop.time() + self.no_progress_timeout
        seen_n = 0
        quiet_since = None
        ready_since = None            # từ lúc nào thì "đủ ảnh và không đổi nữa"
        ready_keys = None
        next_diag = 0.0
        got: list[_Shot] = []
        while loop.time() < deadline and loop.time() < idle_deadline:
            busy, srcs = await self._poll(page)
            arm(busy)
            got = best(srcs)

            # Có thêm ảnh = còn sống -> gia hạn mốc "không tiến triển".
            if len(got) > seen_n:
                seen_n = len(got)
                idle_deadline = loop.time() + self.no_progress_timeout

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
                    except NoImage as e:
                        # ĐÃ CÓ ảnh trong tay mà trang lại hiện "Something went
                        # wrong": đó là băng lỗi của lần thử trước hoặc lỗi phụ,
                        # không phải lượt này hỏng. Ném ra lúc này là bỏ ngang khi
                        # ảnh đã về, phải nhờ nhánh vớt mới cứu - mà nhánh vớt xếp
                        # thứ tự kém chính xác hơn hẳn. Cứ chờ, quiet_limit sẽ chốt.
                        if not got:
                            raise
                        log.info("Trang có báo lỗi nhưng đã nhận %d/%d ảnh - chờ "
                                 "tiếp thay vì bỏ lượt (%s).",
                                 len(got), want, str(e)[:90])
                    next_diag = now + 8
                # Chưa đủ ảnh mà đã im: nếu đã có ít nhất 1 ảnh và ChatGPT ngừng busy quá 10s,
                # lập tức chốt ảnh đã có và gửi yêu cầu gen tiếp, không bắt người dùng chờ 75s.
                quiet_limit = self.quiet_limit_got if got else 25
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

        stuck = loop.time() >= idle_deadline
        if got:
            log.warning("%s, mới có %d/%d ảnh.",
                        f"Đứng im {int(self.no_progress_timeout)}s" if stuck
                        else "Hết giờ", len(got), want)
            return await self._finalize(page, got, want)
        await self._raise_if_bad(page, stale)
        raise NoImage(
            f"đứng im {int(self.no_progress_timeout)}s không ra thêm ảnh nào" if stuck
            else f"quá {int(budget)}s vẫn chưa có ảnh")

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
    async def _handoff(self, slot: _Slot, col: dict, reason: str,
                       on_update: UpdateCb = None) -> bool:
        """Bỏ tài khoản này, trả collection về hàng đợi cho tài khoản khác làm.

        Dùng cho mọi kiểu "slot hỏng": đứng hình, tab chết, lỗi ngoài dự kiến.
        Job chưa xong phải quay về `pending` - để nguyên `running` là UI quay mãi
        mà chẳng worker nào nhận lại."""
        self.stalled[slot.profile] = reason
        log.error("[%s] %s -> chuyển collection '%s' sang tài khoản khác.",
                  slot.label, reason, col.get("name"))
        for j in col.get("jobs", []):
            if j.get("status") != "done":
                j["status"] = "pending"
                j["error"] = None
                if on_update:
                    res = on_update(j)
                    if asyncio.iscoroutine(res):
                        await res
        col["status"] = "pending"
        col["worker"] = None
        return False

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

        # đặt mức suy nghĩ TRƯỚC tin nhắn đầu tiên của collection này
        await self._set_thinking(page)

        seen = set(await self._collect(page))

        # CẢ BỘ trong MỘT tin nhắn, một khung chat. Không tách lô, không gom nhóm.
        # Khuôn prompt yêu cầu ChatGPT "coi mọi mockup trong cùng một request là
        # một bộ sản phẩm" và bắt mỗi ảnh phải có nền KHÁC nhau - nó chỉ làm được
        # khi nhìn thấy cả bộ cùng lúc. Chia nhỏ ra là mỗi lô chốt một hướng
        # design/nền riêng, mất đúng cái tính đồng nhất mà cả kiến trúc này sinh
        # ra để giữ.
        #
        # `run.batch_size` <= 0 (mặc định) nghĩa là cả bộ. Đặt số dương chỉ khi
        # buộc phải chia (bộ quá lớn so với giới hạn đính kèm của ChatGPT).
        if self.batch_size > 0:
            chunks = [pending_jobs[i:i + self.batch_size]
                      for i in range(0, len(pending_jobs), self.batch_size)]
        else:
            chunks = [pending_jobs]
        log.info("[%s] Bắt đầu gen collection '%s' (%d ảnh%s) trên tài khoản %s.",
                 slot.label, col.get("name"), len(pending_jobs),
                 "" if len(chunks) == 1 else f", {len(chunks)} lô", slot.profile)

        quota: str | None = None
        refused: str | None = None      # bị từ chối vì nội dung -> khỏi thử tiếp
        stall_since = asyncio.get_event_loop().time()   # mốc để phát hiện đứng hình
        for ci, chunk in enumerate(chunks):
            if quota or refused or self.stopped:
                break
            for j in chunk:
                j["status"] = "running"
                j["worker"] = slot.label
                await emit(j)

            pending = list(chunk)
            first_msg = (ci == 0)
            last_err: Exception | None = None
            # solo = từ giờ mỗi tin nhắn chỉ hỏi ĐÚNG MỘT ảnh. Bật lên khi lô này
            # đã một lần trả thiếu, tức là không còn tin được thứ tự nữa.
            solo = False

            for round_no in range(1, self.max_retries + 2 + len(chunk)):
                if not pending or quota or refused or self.stopped:
                    break
                batch = pending[:1] if solo else pending
                tpls = [Path(j["template"]) for j in batch]
                if slot.net:
                    slot.net.clear()          # chỉ tính ảnh của VÒNG NÀY
                    slot.net.armed = False
                    slot.net.ignore(tpls)     # đừng nhận nhầm ảnh vừa gửi lên
                imgs: list[_Shot] = []
                try:
                    if not await self._upload(
                            page, tpls, allow_new_chat=(first_msg and round_no == 1)):
                        raise RuntimeError("không đính kèm được ảnh template")

                    # THỨ TỰ THẬT: đọc lại khung soạn xem ChatGPT sẽ nhận ảnh theo
                    # thứ tự nào. Trình duyệt upload song song nên nó hay khác thứ
                    # tự mình truyền vào - không đối chiếu là toàn bộ tên file lệch.
                    if len(batch) > 1:
                        names = await self._attached_names(page)
                        fixed = _match_order(batch, names)
                        if fixed is None:
                            log.warning(
                                "[%s] Không đọc chắc được thứ tự đính kèm (%s) - "
                                "giữ thứ tự gửi đi, tên file có thể lệch.",
                                slot.label, ", ".join(names) or "trống")
                        elif [id(x) for x in fixed] != [id(x) for x in batch]:
                            log.info("[%s] Khung soạn xếp lại thứ tự ảnh -> đổi theo: %s",
                                     slot.label,
                                     " | ".join(Path(j["template"]).name for j in fixed))
                            batch = fixed
                    if first_msg:
                        text = col.get("prompt") or chunk[0].get("prompt")
                    elif round_no == 1:
                        text = self.followup_prompt
                    else:
                        text = self.topup_prompt
                    await self._send(page, text)
                    first_msg = False
                    imgs = await self._wait_images(page, seen, len(batch), slot.net)
                except QuotaExceeded as e:
                    quota = str(e)
                    imgs = e.images               # ảnh kịp nhận trước khi bị chặn
                    log.error("[%s] %s", slot.label, quota)
                except Refused as e:
                    # Từ chối vì nội dung: gửi lại y hệt cũng bị từ chối y hệt.
                    refused = str(e)
                    log.error("[%s] col '%s': %s", slot.label, col.get("name"), refused)
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    log.warning("[%s] col '%s' lô %d/%d vòng %d lỗi: %s",
                                slot.label, col.get("name"), ci + 1, len(chunks), round_no, e)
                    if _is_dead(e):
                        # Tab/trình duyệt đã chết: thử lại trên page này vô nghĩa, mà
                        # để slot chạy tiếp là nó nuốt nốt các collection sau rồi fail
                        # sạch trong vài giây. Bỏ hẳn tài khoản, đẩy việc sang acc khác.
                        return await self._handoff(
                            slot, col, f"tab/trình duyệt đã chết ({e})", on_update)
                    have = list(slot.net.shots()) if slot.net else []
                    if not have:
                        try:
                            have = [_Shot(u, None, i)
                                    for i, u in enumerate(await self._collect(page))
                                    if u not in seen]
                        except Exception:  # noqa: BLE001
                            have = []
                    if have:
                        imgs = await self._finalize(page, have, len(batch))
                        log.warning("[%s] vẫn nhặt được %d ảnh trước khi lỗi.",
                                    slot.label, len(imgs))

                # TRẢ THIẾU thì KHÔNG được gán theo vị trí. Gán kiểu đó ngầm cho
                # rằng mấy ảnh nhận được ứng với mấy template ĐẦU danh sách; ChatGPT
                # bỏ qua một cái ở giữa là toàn bộ tên phía sau lệch một nấc, và
                # vòng "xin làm nốt" sau đó lệch tiếp. Thà bỏ lượt này rồi hỏi lại
                # từng ảnh một - lúc đó mỗi tin nhắn một template, không thể lệch.
                if imgs and len(imgs) < len(batch) and len(batch) > 1:
                    log.warning("[%s] col '%s': gửi %d template mà chỉ nhận %d ảnh "
                                "-> bỏ lượt này, hỏi lại từng ảnh một cho khỏi lệch tên.",
                                slot.label, col.get("name"), len(batch), len(imgs))
                    solo = True
                    imgs = []

                imgs = imgs[:len(batch)]
                seen |= {sh.url for sh in imgs}
                for j, shot in zip(batch, imgs):     # gán theo thứ tự ảnh hiện ra
                    ok = False
                    try:
                        ok = await self._save(page, shot, Path(j["dest"]))
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                    if ok:
                        j["status"] = "done"
                        j["error"] = None
                        stall_since = asyncio.get_event_loop().time()   # còn sống
                    else:
                        # Nhận được ảnh nhưng ghi ra đĩa hỏng (mạng đứt lúc tải lại,
                        # ổ đầy...). Giữ job ở "running" để vòng sau xin gen lại -
                        # trước đây đánh "failed" ngay là mất luôn ảnh đó dù còn dư
                        # mấy vòng retry.
                        j["error"] = "tải ảnh về thất bại, đang xin gen lại"
                        last_err = last_err or RuntimeError("tải ảnh về thất bại")
                    await emit(j)

                # Cắt theo TRẠNG THÁI chứ không theo số ảnh nhận được: job lưu hỏng
                # phải nằm lại trong `pending` để còn được làm lại.
                pending = [j for j in pending if j.get("status") != "done"]

                # ĐỨNG HÌNH: quá stall_timeout mà chưa ra nổi ảnh nào (thường là
                # không đính kèm được, hoặc trang treo). Thử lại thêm mấy vòng nữa
                # cũng vô ích và mất hàng phút - bỏ tài khoản, đẩy việc sang acc khác.
                idle = asyncio.get_event_loop().time() - stall_since
                if pending and not quota and not refused and idle > self.stall_timeout:
                    return await self._handoff(
                        slot, col,
                        f"đứng hình {int(idle)}s không ra ảnh nào "
                        f"({last_err or 'không rõ nguyên nhân'})", on_update)

                if pending and not quota and not refused:
                    log.warning("[%s] col '%s' còn thiếu %d ảnh -> xin ChatGPT làm nốt%s.",
                                slot.label, col.get("name"), len(pending),
                                " (từng ảnh một)" if solo else "")
                    await asyncio.sleep(2)

            for j in pending:
                j["status"] = "failed"
                j["error"] = quota or refused or (
                    str(last_err) if last_err else
                    "ChatGPT không trả đủ ảnh sau nhiều lần xin làm nốt")
                await emit(j)

        # Hết lượt là thoát vòng lô ngay -> job ở các lô SAU chưa hề được đụng tới.
        # Không quét nốt thì chúng kẹt "pending" vĩnh viễn: UI quay mãi, lượt gen
        # không bao giờ coi như kết thúc.
        for j in jobs:
            if j.get("status") in (None, "pending", "running"):
                j["status"] = "failed"
                j["error"] = quota or refused or "Lượt gen dừng giữa chừng"
                await emit(j)

        if self.stopped:
            col["status"] = "paused"
            for j in jobs:
                if j.get("status") in (None, "pending", "running"):
                    j["status"] = "paused"
                    j["error"] = "Dừng khẩn cấp"
                    await emit(j)
            return False

        if refused:
            # Không phải lỗi tài khoản -> KHÔNG đánh dấu hết lượt, tài khoản vẫn
            # nhận collection khác bình thường.
            col["status"] = "partial" if any(j["status"] == "done" for j in jobs) else "failed"
            col["error"] = refused
            return True

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
                if self.stopped:
                    break
                if self.blocked(profile):
                    if on_fleet_update:
                        on_fleet_update(profile, {
                            "status": "exhausted",
                            "reason": self.blocked(profile),
                            "collection": None
                        })
                    break

                col = None
                try:
                    # Chờ lấy collection mới trong hàng đợi (timeout 1.5s để thăm dò trạng thái rảnh)
                    col = await asyncio.wait_for(self.collection_queue.get(), timeout=1.5)
                except asyncio.TimeoutError:
                    now = loop.time()
                    if not busy_slots and self.collection_queue.empty() and (now - self._last_active_time > idle_limit):
                        # Toàn bộ worker đều rảnh và hàng đợi rỗng -> hoàn tất ngay
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
                crashed = False
                try:
                    ok = await self._run_collection_on_slot(slot, page, col, on_update=on_update)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    # Lỗi ngoài dự kiến (page.evaluate vỡ, callback UI ném...) giết
                    # worker trong khi collection ĐÃ RỜI hàng đợi: đợt dọn cuối của
                    # run_collections không với tới nó, job kẹt "running" vĩnh viễn
                    # và UI quay mãi. Trả collection về hàng đợi rồi mới nghỉ.
                    crashed = True
                    log.exception("[%s] Worker lỗi khi chạy collection '%s': %s",
                                  slot.label, col.get("name"), e)
                    await self._handoff(slot, col, f"worker lỗi ({e})", on_update)
                    await self.collection_queue.put(col)
                    if on_fleet_update:
                        on_fleet_update(profile, {"status": "error",
                                                  "error": str(e)[:200],
                                                  "collection": None})
                finally:
                    busy_slots.discard(slot)
                    self._last_active_time = loop.time()
                    self.collection_queue.task_done()

                if crashed:
                    break        # slot đã bị đánh dấu hỏng, nhường việc cho acc khác

                if not ok and not self.stopped and self.blocked(profile):
                    if on_fleet_update:
                        on_fleet_update(profile, {
                            "status": "exhausted",
                            "reason": self.blocked(profile),
                            "collection": None
                        })
                    # Hết quota / đứng hình khi đang dở collection -> đẩy lại cho worker khác
                    unfinished = [j for j in col.get("jobs", []) if j.get("status") != "done"]
                    if unfinished:
                        log.warning("[%s] Ngừng giữa chừng ở collection '%s'. Đẩy lại hàng đợi.",
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
                      if sl.page is not None and not self.blocked(sl.profile)]
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
            if self._slots and all(self.blocked(s.profile) for s in self._slots):
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

