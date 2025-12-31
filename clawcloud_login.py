#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClawCloud 自动登录脚本 - 青龙面板版
cron: 0 8 */3 * *
new Env('ClawCloud自动登录');
"""

import os
import sys
import time
import re
import requests
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException

# ==================== 配置 ====================
CLAW_CLOUD_URL = os.environ.get("CLAW_CLOUD_URL", "https://console.run.claw.cloud")
SIGNIN_URL = f"{CLAW_CLOUD_URL}/signin"
DEVICE_VERIFY_WAIT = 30
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))
QL_URL = os.environ.get("QL_URL", "http://127.0.0.1:5700")
CHROME_DRIVER_PATH = '/usr/bin/chromedriver'
CHROME_BINARY_PATH = '/usr/bin/chromium-browser'


class QingLong:
    def __init__(self):
        self.client_id = os.environ.get('QL_CLIENT_ID')
        self.client_secret = os.environ.get('QL_CLIENT_SECRET')
        self.base_url = QL_URL
        self.token = None
        self.ok = bool(self.client_id and self.client_secret)
        if self.ok:
            self._get_token()

    def _get_token(self):
        try:
            r = requests.get(f"{self.base_url}/open/auth/token",
                           params={"client_id": self.client_id, "client_secret": self.client_secret}, timeout=30)
            data = r.json()
            if data.get("code") == 200:
                self.token = data["data"]["token"]
                print("✅ 青龙 API Token 获取成功")
                return True
            self.ok = False
        except Exception as e:
            print(f"❌ 获取青龙 Token 异常: {e}")
            self.ok = False
        return False

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def get_env(self, name):
        if not self.ok:
            return None
        try:
            r = requests.get(f"{self.base_url}/open/envs", headers=self._headers(),
                           params={"searchValue": name}, timeout=30)
            data = r.json()
            if data.get("code") == 200:
                for env in data.get("data", []):
                    if env.get("name") == name:
                        return env
        except Exception:
            pass
        return None

    def update_env(self, name, value, remarks=""):
        if not self.ok:
            return False
        try:
            existing = self.get_env(name)
            if existing:
                payload = {"id": existing["id"], "name": name, "value": value, "remarks": remarks or existing.get("remarks", "")}
                r = requests.put(f"{self.base_url}/open/envs", headers=self._headers(), json=payload, timeout=30)
            else:
                r = requests.post(f"{self.base_url}/open/envs", headers=self._headers(),
                                json=[{"name": name, "value": value, "remarks": remarks}], timeout=30)
            if r.json().get("code") == 200:
                print(f"✅ 环境变量 {name} 更新成功")
                return True
        except Exception as e:
            print(f"❌ 更新环境变量异常: {e}")
        return False


class Telegram:
    def __init__(self):
        self.token = os.environ.get('TG_BOT_TOKEN')
        self.chat_id = os.environ.get('TG_CHAT_ID')
        self.ok = bool(self.token and self.chat_id)

    def send(self, msg):
        if not self.ok:
            return
        try:
            requests.post(f"https://telegram.api.boosoyz.nyc.mn/bot{self.token}/sendMessage",
                        data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"}, timeout=30)
        except Exception:
            pass

    def photo(self, path, caption=""):
        if not self.ok or not os.path.exists(path):
            return
        try:
            with open(path, 'rb') as f:
                requests.post(f"https://telegram.api.boosoyz.nyc.mn/bot{self.token}/sendPhoto",
                            data={"chat_id": self.chat_id, "caption": caption[:1024]}, files={"photo": f}, timeout=60)
        except Exception:
            pass

    def flush_updates(self):
        if not self.ok:
            return 0
        try:
            r = requests.get(f"https://telegram.api.boosoyz.nyc.mn/bot{self.token}/getUpdates", params={"timeout": 0}, timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"):
                return data["result"][-1]["update_id"] + 1
        except Exception:
            pass
        return 0

    def wait_code(self, timeout=120):
        if not self.ok:
            return None
        offset = self.flush_updates()
        deadline = time.time() + timeout
        pattern = re.compile(r"^/code\s+(\d{6,8})$")
        while time.time() < deadline:
            try:
                r = requests.get(f"https://telegram.api.boosoyz.nyc.mn/bot{self.token}/getUpdates",
                               params={"timeout": 20, "offset": offset}, timeout=30)
                data = r.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message") or {}
                    chat = msg.get("chat") or {}
                    if str(chat.get("id")) != str(self.chat_id):
                        continue
                    text = (msg.get("text") or "").strip()
                    match = pattern.match(text)
                    if match:
                        return match.group(1)
            except Exception:
                pass
            time.sleep(2)
        return None


class ClawCloudAutoLogin:
    def __init__(self):
        self.username = os.environ.get('GH_USERNAME')
        self.password = os.environ.get('GH_PASSWORD')
        self.gh_session = os.environ.get('GH_SESSION', '').strip()
        self.telegram = Telegram()
        self.qinglong = QingLong()
        self.driver = None
        self.screenshots = []
        self.logs = []
        self.screenshot_counter = 0
        self.new_cookie = None
        self.final_screenshot_path = None
        self.login_verified = False
        self.actual_claw_url = CLAW_CLOUD_URL

    def init_driver(self):
        options = Options()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.binary_location = CHROME_BINARY_PATH
        service = Service(CHROME_DRIVER_PATH)
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.driver.implicitly_wait(10)
        self.log("Chrome 浏览器驱动初始化成功", "SUCCESS")

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line)
        self.logs.append(line)

    def capture_screenshot(self, name):
        self.screenshot_counter += 1
        filename = f"/tmp/{self.screenshot_counter:02d}_{name}.png"
        try:
            self.driver.save_screenshot(filename)
            self.screenshots.append(filename)
            return filename
        except Exception:
            return None

    def find_and_click(self, selectors, description=""):
        for sel_type, sel in selectors:
            try:
                elem = self.driver.find_element(By.XPATH if sel_type == "xpath" else By.CSS_SELECTOR, sel)
                if elem.is_displayed() and elem.is_enabled():
                    elem.click()
                    if description:
                        self.log(f"已点击: {description}", "SUCCESS")
                    return True
            except Exception:
                continue
        return False

    def get_base_url(self, url):
        try:
            parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return url

    def update_actual_claw_url(self, url):
        if 'claw.cloud' in url and '/signin' not in url and '/callback' not in url:
            new_base = self.get_base_url(url)
            if new_base != self.actual_claw_url:
                self.log(f"区域切换: {self.actual_claw_url} -> {new_base}", "WARN")
                self.actual_claw_url = new_base

    def get_github_cookie(self):
        try:
            for cookie in self.driver.get_cookies():
                if cookie['name'] == 'user_session' and 'github' in cookie.get('domain', ''):
                    return cookie['value']
        except Exception:
            pass
        return None

    def inject_github_cookies(self):
        if not self.gh_session:
            return False
        try:
            self.driver.get("https://github.com")
            time.sleep(2)
            self.driver.add_cookie({'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'})
            self.driver.add_cookie({'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'})
            self.driver.refresh()
            time.sleep(3)
            if 'login' in self.driver.current_url:
                self.log("Cookie 已失效", "WARN")
                return False
            self.log("GitHub Cookie 注入成功", "SUCCESS")
            return True
        except Exception as e:
            self.log(f"Cookie 注入失败: {e}", "WARN")
            return False

    def save_cookie_to_env(self, cookie_value):
        if not cookie_value or cookie_value == self.gh_session:
            self.log("Cookie 未变化", "INFO")
            return False
        self.log(f"新 Cookie: {cookie_value[:15]}...{cookie_value[-8:]}", "SUCCESS")
        if self.qinglong.update_env('GH_SESSION', cookie_value, 'GitHub Session Cookie - 自动更新'):
            self.telegram.send("🔑 <b>Cookie 已自动更新</b>")
        else:
            self.telegram.send(f"🔑 请手动更新 GH_SESSION:\n<code>{cookie_value}</code>")
        return True

    def get_page_type(self):
        try:
            url = self.driver.current_url.lower()
            if 'github.com' in url:
                if 'two-factor' in url:
                    return 'github_2fa'
                if '/login' in url or '/session' in url:
                    return 'github_login'
                if '/login/oauth/authorize' in url:
                    return 'github_oauth'
                return 'github_other'
            if 'claw.cloud' in url:
                if '/callback' in url:
                    return 'callback'
                if '/signin' in url:
                    return 'signin'
                return 'console'
            return 'unknown'
        except Exception:
            return 'unknown'

    def is_in_console(self):
        try:
            url = self.driver.current_url.lower()
            if '/signin' in url or '/callback' in url or 'github.com' in url:
                return False
            if 'claw.cloud' not in url:
                return False
            page = self.driver.page_source.lower()
            for sign in ['sign in with github', 'continue with github']:
                if sign in page:
                    return False
            return True
        except Exception:
            return False

    def wait_for_callback_complete(self, timeout=30):
        self.log("等待 OAuth callback 处理...", "STEP")
        for i in range(timeout):
            page_type = self.get_page_type()
            if i % 3 == 0:
                self.log(f"[{i}s] 类型: {page_type}")
            if page_type == 'console':
                self.update_actual_claw_url(self.driver.current_url)
                self.log(f"Callback 完成，域名: {self.actual_claw_url}", "SUCCESS")
                return True
            if page_type == 'signin':
                self.log("Callback 后返回登录页", "ERROR")
                return False
            if page_type in ['github_login', 'github_oauth', 'github_2fa']:
                return 'need_github'
            time.sleep(1)
        self.log("Callback 超时", "ERROR")
        return False

    def handle_device_verification(self):
        self.log(f"需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
        self.telegram.send(f"⚠️ <b>需要设备验证</b>\n请在 {DEVICE_VERIFY_WAIT} 秒内批准")
        shot = self.capture_screenshot("设备验证")
        if shot:
            self.telegram.photo(shot, "设备验证页面")
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            url = self.driver.current_url
            if 'verified-device' not in url and 'device-verification' not in url:
                self.log("设备验证通过！", "SUCCESS")
                return True
            if i % 5 == 0:
                try:
                    self.driver.refresh()
                    time.sleep(2)
                except Exception:
                    pass
        return 'verified-device' not in self.driver.current_url

    def handle_two_factor_mobile(self):
        self.log(f"需要两步验证（GitHub Mobile），等待 {TWO_FACTOR_WAIT} 秒...", "WARN")
        self.telegram.send(f"⚠️ <b>需要两步验证</b>\n请在手机 GitHub App 批准")
        shot = self.capture_screenshot("两步验证_mobile")
        if shot:
            self.telegram.photo(shot, "两步验证页面")
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            url = self.driver.current_url
            if "github.com/sessions/two-factor/" not in url:
                self.log("两步验证通过！", "SUCCESS")
                return True
            if "github.com/login" in url and 'two-factor' not in url:
                return False
            if i % 10 == 0 and i != 0:
                self.log(f"等待中... ({i}/{TWO_FACTOR_WAIT}秒)")
        return False

    def handle_two_factor_code(self):
        self.log("需要输入验证码", "WARN")
        shot = self.capture_screenshot("两步验证_code")
        
        # 尝试切换到验证码模式
        switch_selectors = [
            ("xpath", "//a[contains(text(),'Use your authenticator app')]"),
            ("xpath", "//a[contains(text(),'authentication app')]"),
            ("css", "[href*='two-factor/app']")
        ]
        for sel_type, sel in switch_selectors:
            try:
                elem = self.driver.find_element(By.XPATH if sel_type == "xpath" else By.CSS_SELECTOR, sel)
                if elem.is_displayed():
                    elem.click()
                    self.log("已切换到验证码模式", "SUCCESS")
                    time.sleep(2)
                    break
            except Exception:
                continue

        self.telegram.send(f"🔐 <b>需要验证码</b>\n请发送: <code>/code 123456</code>")
        if shot:
            self.telegram.photo(shot, "两步验证页面")

        code = self.telegram.wait_code(timeout=TWO_FACTOR_WAIT)
        if not code:
            self.log("等待验证码超时", "ERROR")
            return False

        self.log(f"收到验证码: {code}", "SUCCESS")
        original_url = self.driver.current_url

        input_selectors = [
            'input[autocomplete="one-time-code"]',
            'input[name="app_otp"]',
            'input[name="otp"]',
            'input#app_totp'
        ]
        
        for sel in input_selectors:
            try:
                elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                if elem.is_displayed() and elem.is_enabled():
                    elem.clear()
                    for c in code:
                        elem.send_keys(c)
                        time.sleep(0.1)
                    self.log("验证码已输入", "SUCCESS")
                    time.sleep(2)
                    
                    if self.driver.current_url != original_url:
                        cookie = self.get_github_cookie()
                        if cookie:
                            self.new_cookie = cookie
                        return True
                    
                    elem.send_keys(Keys.RETURN)
                    time.sleep(3)
                    
                    if "two-factor" not in self.driver.current_url:
                        cookie = self.get_github_cookie()
                        if cookie:
                            self.new_cookie = cookie
                        return True
                    break
            except Exception:
                continue

        return "two-factor" not in self.driver.current_url

    def login_to_github(self):
        self.log("登录 GitHub...", "STEP")
        self.capture_screenshot("github_登录页")
        try:
            self.driver.find_element(By.CSS_SELECTOR, 'input[name="login"]').send_keys(self.username)
            self.driver.find_element(By.CSS_SELECTOR, 'input[name="password"]').send_keys(self.password)
            self.log("凭据已输入", "SUCCESS")
        except Exception as e:
            self.log(f"输入凭据失败: {e}", "ERROR")
            return False

        try:
            self.driver.find_element(By.CSS_SELECTOR, 'input[type="submit"], button[type="submit"]').click()
        except Exception:
            pass

        time.sleep(3)
        url = self.driver.current_url

        if 'verified-device' in url or 'device-verification' in url:
            if not self.handle_device_verification():
                return False
            time.sleep(2)
            url = self.driver.current_url

        if 'two-factor' in url:
            if 'two-factor/mobile' in url:
                if not self.handle_two_factor_mobile():
                    return False
            else:
                if not self.handle_two_factor_code():
                    return False
            time.sleep(2)

        cookie = self.get_github_cookie()
        if cookie:
            self.new_cookie = cookie
            self.log(f"GitHub 登录成功", "SUCCESS")
        return True

    def handle_oauth_authorization(self):
        if 'github.com/login/oauth/authorize' not in self.driver.current_url:
            return False
        self.log("处理 OAuth 授权...", "STEP")
        cookie = self.get_github_cookie()
        if cookie:
            self.new_cookie = cookie
        selectors = [
            ("xpath", "//button[@name='authorize']"),
            ("xpath", "//button[contains(text(),'Authorize')]"),
            ("css", "button[name='authorize']")
        ]
        self.find_and_click(selectors, "OAuth 授权")
        time.sleep(3)
        return True

    def handle_github_flow(self):
        for _ in range(5):
            page_type = self.get_page_type()
            self.log(f"GitHub 流程: {page_type}")
            if page_type == 'github_login':
                if not self.login_to_github():
                    return False
                time.sleep(2)
            elif page_type == 'github_oauth':
                self.handle_oauth_authorization()
                time.sleep(2)
            elif page_type == 'github_2fa':
                if 'two-factor/mobile' in self.driver.current_url:
                    if not self.handle_two_factor_mobile():
                        return False
                else:
                    if not self.handle_two_factor_code():
                        return False
                time.sleep(2)
            elif page_type in ['console', 'callback', 'signin']:
                return True
            else:
                time.sleep(2)
        return True

    def perform_keepalive(self):
        self.log(f"执行保活 (域名: {self.actual_claw_url})...", "STEP")
        try:
            self.driver.get(f"{self.actual_claw_url}/apps")
            time.sleep(5)
            if '/signin' in self.driver.current_url.lower():
                self.log("被重定向到登录页！", "ERROR")
                return False
            self.log("保活成功！", "SUCCESS")
            self.final_screenshot_path = self.capture_screenshot("控制台")
            return True
        except Exception as e:
            self.log(f"保活异常: {e}", "ERROR")
            return False

    def send_notification(self, success, error_message=""):
        if not self.telegram.ok:
            return
        status = "✅ 成功" if success else "❌ 失败"
        msg = (f"<b>🤖 ClawCloud 自动登录</b>\n\n"
               f"<b>状态:</b> {status}\n"
               f"<b>用户:</b> {self.username}\n"
               f"<b>区域:</b> {self.actual_claw_url}\n"
               f"<b>时间:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}")
        if error_message:
            msg += f"\n<b>错误:</b> {error_message}"
        msg += f"\n\n<b>日志:</b>\n" + "\n".join(self.logs[-8:])
        self.telegram.send(msg)
        if self.final_screenshot_path:
            self.telegram.photo(self.final_screenshot_path, "最终状态")
        elif self.screenshots:
            self.telegram.photo(self.screenshots[-1], "最终状态")

    def cleanup_resources(self):
        for s in self.screenshots:
            try:
                os.remove(s)
            except Exception:
                pass
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

    def execute_login_flow(self):
        print("\n" + "=" * 60)
        print("🚀 ClawCloud 自动登录")
        print("=" * 60 + "\n")

        self.log(f"GitHub 用户名: {self.username}")
        self.log(f"现有 Session: {'有' if self.gh_session else '无'}")
        self.log(f"青龙面板 API: {'已配置' if self.qinglong.ok else '未配置'}")
        self.log(f"Telegram 通知: {'已配置' if self.telegram.ok else '未配置'}")

        if not self.username or not self.password:
            self.log("缺少 GitHub 凭据", "ERROR")
            self.send_notification(False, "凭据未配置")
            sys.exit(1)

        try:
            self.init_driver()
            if self.gh_session:
                self.inject_github_cookies()

            self.log("步骤 1: 访问 ClawCloud", "STEP")
            self.driver.get(SIGNIN_URL)
            time.sleep(3)
            self.capture_screenshot("首页")

            page_type = self.get_page_type()
            self.log(f"当前页面类型: {page_type}")

            if page_type == 'console' and self.is_in_console():
                self.log("已登录控制台", "SUCCESS")
                self.update_actual_claw_url(self.driver.current_url)
                self.login_verified = True
                self.perform_keepalive()
                self.driver.get("https://github.com")
                time.sleep(2)
                cookie = self.get_github_cookie()
                if cookie:
                    self.save_cookie_to_env(cookie)
                self.send_notification(True)
                return

            self.log("步骤 2: 点击 GitHub 登录", "STEP")
            selectors = [
                ("xpath", "//button[contains(text(),'GitHub')]"),
                ("xpath", "//a[contains(text(),'GitHub')]"),
                ("xpath", "//*[contains(text(),'GitHub')]")
            ]
            if not self.find_and_click(selectors, "GitHub 登录"):
                self.log("找不到 GitHub 按钮", "ERROR")
                self.send_notification(False, "找不到登录按钮")
                sys.exit(1)

            time.sleep(3)
            self.log("步骤 3: 处理认证流程", "STEP")

            for loop in range(10):
                self.log(f"认证循环 [{loop+1}/10]")
                page_type = self.get_page_type()
                self.log(f"类型: {page_type}")

                if page_type == 'callback':
                    result = self.wait_for_callback_complete(timeout=30)
                    if result == True:
                        break
                    elif result == 'need_github':
                        continue
                    else:
                        break

                if page_type == 'console':
                    self.update_actual_claw_url(self.driver.current_url)
                    time.sleep(2)
                    if self.is_in_console():
                        self.log("已进入控制台！", "SUCCESS")
                        self.login_verified = True
                        break

                if page_type == 'signin':
                    if loop > 2:
                        self.log("登录失败", "ERROR")
                        break
                    time.sleep(2)
                    continue

                if page_type in ['github_login', 'github_oauth', 'github_2fa', 'github_other']:
                    if not self.handle_github_flow():
                        break
                    continue

                time.sleep(2)

            self.log("步骤 4: 验证登录结果", "STEP")
            self.driver.get(f"{self.actual_claw_url}/apps")
            time.sleep(5)

            url = self.driver.current_url.lower()
            self.log(f"验证URL: {url}")
            self.capture_screenshot("验证结果")

            if '/signin' in url:
                self.log("验证失败：被重定向到登录页", "ERROR")
                self.send_notification(False, "登录验证失败")
                sys.exit(1)

            if 'claw.cloud' in url:
                self.log("登录验证成功！", "SUCCESS")
                self.login_verified = True

            self.perform_keepalive()

            self.log("步骤 5: 更新 Cookie", "STEP")
            if self.new_cookie:
                self.save_cookie_to_env(self.new_cookie)
            else:
                self.driver.get("https://github.com")
                time.sleep(2)
                cookie = self.get_github_cookie()
                if cookie:
                    self.save_cookie_to_env(cookie)

            self.send_notification(True)
            print("\n" + "=" * 60)
            print("✅ 执行成功！")
            print("=" * 60 + "\n")

        except KeyboardInterrupt:
            self.log("用户中断", "WARN")
            self.send_notification(False, "用户中断")
            sys.exit(1)
        except Exception as e:
            self.log(f"异常: {e}", "ERROR")
            self.capture_screenshot("异常")
            import traceback
            traceback.print_exc()
            self.send_notification(False, str(e))
            sys.exit(1)
        finally:
            self.cleanup_resources()


if __name__ == "__main__":
    ClawCloudAutoLogin().execute_login_flow()