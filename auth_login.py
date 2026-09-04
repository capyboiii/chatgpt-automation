"""Đăng nhập ChatGPT tự động bằng email + mật khẩu + mã 2FA.

VÌ SAO ĐIỀN FORM CHỨ KHÔNG BẮN HTTP THUẦN
-----------------------------------------
Đo thực tế trên trang https://chatgpt.com/auth/login: trang nạp Cloudflare JSD
(`/cdn-cgi/challenge-platform/.../jsd/main.js`) rồi POST một payload vân tay trình
duyệt lên `/jsd/oneshot/...`; qua được mới có cookie `cf_clearance`. Cookie đó lại
bị buộc vào TLS fingerprint + IP + User-Agent, nên bê cookie sang `requests`/`httpx`
là bị chặn ngay dù credential đúng.

Bắt luồng một lượt đăng nhập thật còn cho thấy trước khi `POST /api/accounts/
password/verify` chạy được, trang đã phải nạp `sentinel.openai.com/.../sdk.js` và
POST lên `/backend-api/sentinel/req` - thêm một lớp nữa chỉ trình duyệt thật vượt
được. Kết luận: chỉ trình duyệt thật mới đăng nhập được, và ta đã có sẵn Chrome
của từng profile.

NGUYÊN TẮC AN TOÀN
------------------
* Mật khẩu và seed 2FA KHÔNG ghi ra đĩa, KHÔNG ghi log, KHÔNG trả về qua API.
* Mã 2FA sinh cục bộ bằng pyotp, seed không rời khỏi máy.
* Gặp captcha / "verify you are human" thì DỪNG và nhường cửa sổ cho người dùng
  tự xác minh, xong thì chạy tiếp. Không có bất kỳ mưu mẹo vượt rào nào ở đây.
"""

from __future__ import annotations

import logging
import re
import time

log = logging.getLogger("chatgpt.login")

# Các selector dưới đây lấy từ một lượt đăng nhập THẬT quan sát được (2026-09):
#   bước 1  chatgpt.com          -> bấm "Log in"
#   bước 2  chatgpt.com          -> input[name="login_hint"] #mobile-auth-email
#   bước 3  auth.openai.com/log-in/password
#                                -> ô email KHÔNG có name, id động kiểu react-aria,
#                                   type=text, placeholder "Email address"
#                                -> input[name="current-password"] type=password
#   bước 4  auth.openai.com/mfa-challenge/<id>
#                                -> input[name="code"] maxlength=6 "One-time code"
#   bước 5  quay lại chatgpt.com
# Vì ô email ở bước 3 chỉ nhận diện được qua placeholder nên danh sách phải có cả
# nhánh placeholder, đừng rút gọn.
SEL_EMAIL = [
    'input[name="login_hint"]',
    "#mobile-auth-email",
    'input[name="email"]',
    'input[type="email"]',
    "#email",
    "#email-input",
    'input[id*="email" i]',
    'input[placeholder*="email address" i]',
    'input[placeholder*="email" i]',
    'input[placeholder*="Địa chỉ" i]',
]
SEL_PASSWORD = [
    'input[name="current-password"]',
    'input[name="password"]',
    'input[type="password"]',
    'input[id*="password" i]',
]
SEL_OTP = [
    'input[name="code"]',
    'input[maxlength="6"]',
    'input[placeholder*="one-time" i]',
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[id*="code" i]',
    'input[inputmode="numeric"]',
]
# Chữ trên nút, cả tiếng Việt lẫn Anh
TXT_LOGIN_ENTRY = ["đăng nhập", "log in", "sign in"]
TXT_CONTINUE = ["tiếp tục", "continue", "next", "đăng nhập", "log in"]

# Banner cookie che mất nút "Log in" -> phải dẹp trước. Chọn phương án hạn chế
# theo dõi nhất ("Reject non-essential"), không bấm "Accept all".
TXT_COOKIE_REJECT = [
    "reject non-essential", "reject all", "only essential", "necessary only",
    "từ chối", "chỉ cần thiết", "chỉ chấp nhận cần thiết",
]

# Dấu hiệu trang đang đòi người thật xác minh
BOT_HINTS = (
    "verify you are human", "xác minh bạn là con người", "are you a robot",
    "captcha", "unusual activity", "hoạt động bất thường",
)
BOT_FRAMES = (
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="arkoselabs"]',
    'iframe[title*="challenge" i]',
)


def parse_creds(raw: str) -> dict | None:
    """'email | mật khẩu | seed2fa' -> dict. Phần seed có thể bỏ trống."""
    parts = [x.strip() for x in (raw or "").split("|")]
    if len(parts) < 2 or "@" not in parts[0] or not parts[1]:
        return None
    seed = parts[2].replace(" ", "").upper() if len(parts) > 2 else ""
    return {"email": parts[0], "password": parts[1], "totp": seed}


def check_totp_seed(seed: str) -> bool:
    """Seed 2FA có sinh được mã không (kiểm tra trước khi mở trình duyệt)."""
    if not seed:
        return True
    try:
        import pyotp
        return len(pyotp.TOTP(seed).now()) == 6
    except Exception:  # noqa: BLE001
        return False


def needs_human(page) -> bool:
    """Trang đang chặn chờ người thật xác minh?"""
    for sel in BOT_FRAMES:
        try:
            if page.locator(sel).count():
                return True
        except Exception:  # noqa: BLE001
            pass
    try:
        txt = (page.inner_text("body") or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(h in txt for h in BOT_HINTS)


PHONE_HINTS = ("phone", "số điện thoại", "sdt", "tel")


def _is_phone_box(loc) -> bool:
    """Ô này là ô số điện thoại? (modal đăng nhập có cả hai, dùng chung tên field)"""
    try:
        if (loc.get_attribute("type") or "").lower() == "tel":
            return True
        meta = " ".join(filter(None, (
            loc.get_attribute("placeholder"), loc.get_attribute("aria-label"),
            loc.get_attribute("autocomplete")))).lower()
        return any(h in meta for h in PHONE_HINTS)
    except Exception:  # noqa: BLE001
        return False


def _fill_first(page, selectors: list[str], value: str, timeout: int = 15_000,
                skip_phone: bool = False) -> bool:
    """Điền vào ô đầu tiên khớp. `value` tuyệt đối không được đưa vào log."""
    per = max(1_500, timeout // max(1, len(selectors)))
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=per)
            if skip_phone and _is_phone_box(loc):
                log.info("Bỏ qua ô điện thoại khớp '%s', tìm tiếp ô email.", sel)
                continue
            loc.click()
            loc.fill(value)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _click_text(page, words: list[str], timeout: int = 9_000,
                allow_submit: bool = False) -> bool:
    """Bấm nút có chữ KHỚP CHÍNH XÁC (đã bỏ khoảng trắng thừa).

    Khớp kiểu "chứa" là hỏng: modal đăng nhập có sẵn "Continue with Google",
    "Continue with Apple", "Continue with phone", nên tìm "continue" rồi lấy
    `.first` là bấm nhầm sang đăng nhập bằng điện thoại - trong khi nút "Continue"
    thật nằm dưới ô email.

    `allow_submit` chỉ bật ở các bước trong FORM đăng nhập. Ở trang chủ mà bật là
    dễ bấm nhầm nút của banner cookie (nó cũng là button[type=submit]).
    """
    per = max(1_200, timeout // max(1, len(words)))
    for w in words:
        try:
            pat = re.compile(rf"^\s*{re.escape(w)}\s*$", re.I)
            loc = page.get_by_role("button", name=pat).first
            loc.wait_for(state="visible", timeout=per)
            loc.click()
            return True
        except Exception:  # noqa: BLE001
            continue
    if not allow_submit:
        return False
    for sel in ('button[type="submit"]', 'input[type="submit"]'):
        try:
            page.locator(sel).first.click(timeout=2_000)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def dismiss_cookie_banner(page) -> bool:
    """Dẹp banner cookie nếu có, chọn mức hạn chế theo dõi nhất."""
    if _click_text(page, TXT_COOKIE_REJECT, timeout=4_000):
        log.info("Đã đóng banner cookie (chọn mức tối thiểu).")
        time.sleep(0.8)
        return True
    return False


# Ảnh chụp cấu trúc trang lúc hỏng, để sửa selector mà không phải mò.
# CHỈ lấy siêu dữ liệu của ô nhập (name/type/placeholder) và chữ trên nút -
# KHÔNG bao giờ lấy `value`, nên mật khẩu và mã 2FA không thể lọt vào file.
SNAPSHOT_JS = r"""() => {
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const norm = (t) => (t || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    return {
        url: location.href.split('?')[0],
        title: document.title,
        inputs: [...document.querySelectorAll('input')].filter(vis).map((i) => ({
            name: i.name || null, type: i.type, id: i.id || null,
            placeholder: i.placeholder || null,
            maxlength: i.getAttribute('maxlength'),
            autocomplete: i.getAttribute('autocomplete')
        })),
        buttons: [...document.querySelectorAll('button,a[role="button"]')]
            .filter(vis).map((b) => norm(b.innerText || b.getAttribute('aria-label')))
            .filter(Boolean).slice(0, 15),
        iframes: [...document.querySelectorAll('iframe')]
            .map((f) => (f.src || '').split('?')[0]).slice(0, 10),
        text_head: norm(document.body ? document.body.innerText : '').slice(0, 400)
    };
}"""


def snapshot(page) -> dict:
    """Cấu trúc trang hiện tại (không chứa nội dung người dùng gõ)."""
    try:
        return page.evaluate(SNAPSHOT_JS)
    except Exception as e:  # noqa: BLE001
        return {"error": f"không chụp được: {e}"}


def _wait_url(page, pattern: str, timeout: int = 20_000) -> bool:
    """Chờ trang chuyển đúng chặng. Nhanh hơn ngồi sleep đoán, và không trượt khi
    mạng chậm. Hết giờ thì trả False chứ không ném lỗi - vòng chờ ở dưới lo tiếp."""
    try:
        page.wait_for_url(pattern, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def _has_otp(page) -> bool:
    for sel in SEL_OTP:
        try:
            if page.locator(sel).count():
                return True
        except Exception:  # noqa: BLE001
            pass
    try:                                   # kiểu 6 ô rời mỗi ô 1 ký tự
        return page.locator('input[maxlength="1"]').count() >= 6
    except Exception:  # noqa: BLE001
        return False


def _fill_otp(page, code: str) -> bool:
    if _fill_first(page, SEL_OTP, code, timeout=6_000):
        return True
    try:
        boxes = page.locator('input[maxlength="1"]')
        if boxes.count() >= len(code):
            for i, ch in enumerate(code):
                boxes.nth(i).fill(ch)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def auto_login(page, creds: dict, box: dict, is_logged_in, timeout: int = 240) -> bool:
    """Điền email -> mật khẩu -> mã 2FA. True = đã vào được ChatGPT.

    `box` là dict trạng thái để UI theo dõi (`phase`, `needs_human`, `error`) -
    KHÔNG bao giờ chứa mật khẩu. `is_logged_in(page)` do server truyền vào để
    dùng lại đúng bộ kiểm tra sẵn có.
    """
    import pyotp

    if is_logged_in(page):
        box["phase"] = "done"
        return True

    box["phase"] = "email"
    dismiss_cookie_banner(page)                 # banner che mất nút Log in
    _click_text(page, TXT_LOGIN_ENTRY)          # nút "Đăng nhập" ngoài trang chủ
    time.sleep(1.0)
    if not _fill_first(page, SEL_EMAIL, creds["email"], timeout=25_000,
                       skip_phone=True):
        if needs_human(page):
            box["needs_human"] = True
        else:
            box["error"] = "Không thấy ô nhập email trên trang đăng nhập."
            box["snapshot"] = snapshot(page)
            return False
    else:
        _click_text(page, TXT_CONTINUE, allow_submit=True)
        # chặng tiếp theo là auth.openai.com/log-in/password (đo từ luồng thật)
        _wait_url(page, "**/log-in/**", timeout=25_000)
        dismiss_cookie_banner(page)             # auth.openai.com có banner riêng

    box["phase"] = "password"
    if _fill_first(page, SEL_PASSWORD, creds["password"], timeout=25_000):
        _click_text(page, TXT_CONTINUE, allow_submit=True)
        # sau đó hoặc sang trang nhập mã 2FA, hoặc về thẳng chatgpt.com
        _wait_url(page, "**/mfa-challenge/**", timeout=15_000)
    elif needs_human(page):
        box["needs_human"] = True
    else:
        box["error"] = "Không thấy ô nhập mật khẩu (trang có thể đã đổi giao diện)."
        box["snapshot"] = snapshot(page)
        return False

    # Từ đây: chờ vào được; trang hỏi 2FA thì điền; gặp bot-check thì nhường người.
    deadline = time.time() + timeout
    otp_tries = 0
    last_code = ""
    last_check = 0.0
    while time.time() < deadline:
        # is_logged_in gọi mạng (/backend-api/me) -> giãn ra ~6s/lần thay vì 2s,
        # khỏi bắn cả trăm request trong lúc chờ.
        if time.time() - last_check >= 6.0:
            last_check = time.time()
            if is_logged_in(page):
                box["needs_human"] = False
                box["phase"] = "done"
                return True

        if needs_human(page):
            if not box.get("needs_human"):
                log.warning("Trang đăng nhập đòi xác minh người thật - chờ bạn thao "
                            "tác trong cửa sổ Chrome đang mở.")
            box["needs_human"] = True
            box["phase"] = "captcha"
            time.sleep(3.0)
            continue
        box["needs_human"] = False

        if _has_otp(page):
            if not creds.get("totp"):
                box["error"] = ("Tài khoản bật 2FA nhưng bạn chưa nhập mã bí mật "
                                "(phần thứ ba, sau dấu |).")
                box["snapshot"] = snapshot(page)
                return False
            if otp_tries >= 3:
                box["error"] = "Nhập mã 2FA 3 lần đều không qua - kiểm tra lại seed."
                box["snapshot"] = snapshot(page)
                return False
            box["phase"] = "2fa"
            code = pyotp.TOTP(creds["totp"]).now()
            if code == last_code:            # mã cũ vừa trượt -> đợi chu kỳ mới
                time.sleep(5.0)
                continue
            last_code = code
            otp_tries += 1
            if _fill_otp(page, code):
                _click_text(page, TXT_CONTINUE, allow_submit=True)
                # mã đúng thì nhảy về chatgpt.com ngay, khỏi chờ đủ 4 giây
                if _wait_url(page, "**chatgpt.com/**", timeout=15_000):
                    last_check = 0.0          # kiểm tra đăng nhập ngay vòng sau
                else:
                    time.sleep(3.0)
                continue

        time.sleep(2.0)

    if not box.get("error"):
        box["error"] = f"Quá {timeout}s vẫn chưa đăng nhập xong."
    box["snapshot"] = snapshot(page)
    return False
