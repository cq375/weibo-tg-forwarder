from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

LOGGER = logging.getLogger(__name__)

LOGIN_URL = (
    "https://passport.weibo.com/sso/signin"
    "?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F"
)


class CookieRefreshError(RuntimeError):
    pass


class WeiboQrRefresher:
    def __init__(self, cookie_file: Path, timeout_seconds: int):
        self.cookie_file = cookie_file
        self.timeout_seconds = timeout_seconds
        self.qr_path = cookie_file.parent / "weibo-login-qr.png"

    def refresh(self, on_qr_ready: Callable[[Path], None]) -> str:
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.qr_path.unlink(missing_ok=True)

        try:
            with sync_playwright() as playwright:
                executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
                launch_options = {
                    "headless": True,
                    "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                }
                if executable:
                    launch_options["executable_path"] = executable
                browser = playwright.chromium.launch(**launch_options)
                context = browser.new_context(
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": 1280, "height": 900},
                )
                page = context.new_page()
                try:
                    page.goto(
                        LOGIN_URL,
                        wait_until="domcontentloaded",
                        timeout=min(self.timeout_seconds, 60) * 1000,
                    )
                    qr = page.locator('img[src*="v2.qr.weibo.cn"]').first
                    qr.wait_for(state="visible", timeout=60_000)
                    self._save_qr(context, qr)
                    on_qr_ready(self.qr_path)
                    cookie = self._wait_for_login(context, page)
                    return cookie
                finally:
                    context.close()
                    browser.close()
        except PlaywrightTimeoutError as exc:
            raise CookieRefreshError("等待微博登录二维码或扫码确认超时") from exc
        except CookieRefreshError:
            raise
        except Exception as exc:
            raise CookieRefreshError(f"微博扫码登录失败：{exc}") from exc
        finally:
            self.qr_path.unlink(missing_ok=True)

    def _save_qr(self, context, qr) -> None:
        qr_url = qr.get_attribute("src")
        if qr_url:
            response = context.request.get(qr_url, timeout=30_000)
            if response.ok:
                self.qr_path.write_bytes(response.body())
                return
        qr.screenshot(path=str(self.qr_path))

    def _wait_for_login(self, context, page) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            cookies = context.cookies()
            if any(cookie.get("name") == "SUB" for cookie in cookies):
                try:
                    page.goto("https://weibo.com/", wait_until="domcontentloaded", timeout=30_000)
                except PlaywrightTimeoutError:
                    pass
                cookie = self._format_cookie(context.cookies())
                if cookie:
                    return cookie
            page.wait_for_timeout(1000)
        raise CookieRefreshError(f"{self.timeout_seconds} 秒内未完成微博扫码确认")

    @staticmethod
    def _format_cookie(cookies: list[dict]) -> str:
        selected: dict[str, str] = {}
        for cookie in cookies:
            domain = str(cookie.get("domain") or "")
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if name and value and ("weibo.com" in domain or "weibo.cn" in domain):
                selected[name] = value
        if "SUB" not in selected:
            return ""
        return "; ".join(f"{name}={value}" for name, value in sorted(selected.items()))

    def persist(self, cookie: str) -> None:
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cookie_file.with_name(f"{self.cookie_file.name}.tmp")
        temporary.write_text(cookie.strip(), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.cookie_file)
        os.chmod(self.cookie_file, 0o600)
        LOGGER.info("新的微博登录态已写入持久化 Cookie 文件")
