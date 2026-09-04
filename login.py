"""Đăng nhập ChatGPT cho MỘT profile (chạy 1 lần, phiên lưu vào profile).

    python login.py acc1

Mở Chrome với đúng thư mục user-data mà pool sẽ dùng (.chrome-profiles/<name>),
để bạn đăng nhập ChatGPT. Sau đó pool chạy là đã có phiên, khỏi đăng nhập lại.
"""
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    base = Path(cfg["browser"].get("profiles_dir", "./.chrome-profiles")).resolve()
    udir = base / name
    udir.mkdir(parents=True, exist_ok=True)
    print(f"Mở Chrome cho profile '{name}' -> {udir}")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(udir), headless=False, channel="chrome",
            viewport={"width": 1400, "height": 950},
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=90_000)
        print("\n" + "=" * 56)
        print(f"  Đăng nhập ChatGPT trong cửa sổ vừa mở (profile: {name}).")
        print("  Xong thì quay lại đây nhấn Enter để đóng.")
        print("=" * 56)
        input()
        ctx.close()
    print("✓ Phiên đã lưu. Thêm profile khác: python login.py <name>")


if __name__ == "__main__":
    main()
