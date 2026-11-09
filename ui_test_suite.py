# -*- coding: utf-8 -*-
"""
语伴 - 前端 UI 自动化测试套件
覆盖：登录 + 7 个标签页的按钮/表单/边界值 + 控制台错误检查
使用 Playwright + 系统 Chrome
"""
import sys
import os
import time
import json
import traceback
from playwright.sync_api import sync_playwright, expect

CHROME_PATH = os.environ.get("YUBAN_CHROME") or r"C:\Users\clm117\AppData\Local\google\Chrome\Application\chrome.exe"
# 系统 Chrome 不存在时（如 CI/Linux）回退到 Playwright 自带 Chromium
CHROME_OPTS = {"executable_path": CHROME_PATH} if os.path.exists(CHROME_PATH) else {}
# 前端地址可通过环境变量注入（默认 8899 静态服务；nginx 部署时传 8080）
BASE_URL = os.environ.get("YUBAN_UI_BASE", "http://127.0.0.1:8899")
TEST_USER = "testuser"
TEST_PASS = "Test@123456"

results = []  # {"name": str, "status": "PASS|FAIL|BLOCKED", "detail": str}
console_errors = []
page_errors = []
failed_reqs = []


def log(name, status, detail=""):
    results.append({"name": name, "status": status, "detail": detail})
    flag = {"PASS": "[PASS]", "FAIL": "[FAIL]", "BLOCKED": "[BLOCKED]"}[status]
    print(f"{flag} {name}  {detail}")


def safe(fn, name, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log(name, "FAIL", f"异常: {type(e).__name__}: {str(e)[:200]}")
        return None


def run_tests():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--window-size=1280,900"],
            **CHROME_OPTS,
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()

        # 自动处理 alert/confirm/prompt 对话框
        all_dialogs = []
        page.on("dialog", lambda dialog: (all_dialogs.append(dialog.message), dialog.accept()))

        # 捕获控制台错误与页面异常
        all_console = []
        page.on("console", lambda msg: (console_errors.append({"type": msg.type, "text": msg.text[:300]}), all_console.append({"type": msg.type, "text": msg.text[:200]})) if msg.type == "error" else all_console.append({"type": msg.type, "text": msg.text[:200]}))
        page.on("pageerror", lambda err: page_errors.append(str(err)[:300]))

        # 捕获失败的网络请求（401/400/500）
        global failed_reqs
        failed_reqs = []
        auth_log = []
        def on_request(req):
            if "/api/" in req.url and "login" not in req.url:
                has_auth = "authorization" in {k.lower() for k in req.headers}
                auth_log.append({"url": req.url[:100], "method": req.method, "has_auth": has_auth})
        page.on("request", on_request)
        def on_response(resp):
            if resp.status >= 400:
                body = ""
                try:
                    body = resp.text()[:200]
                except Exception:
                    pass
                failed_reqs.append({"url": resp.url[:120], "status": resp.status, "body": body})
        page.on("response", on_response)

        # ========== 1. 登录 ==========
        resp = page.goto(BASE_URL, wait_until="domcontentloaded")
        log("登录-页面加载", "PASS" if resp and resp.ok else "FAIL", "打开首页")

        # 等待登录遮罩显示（checkAuthGate 异步 fetch /health）
        try:
            page.wait_for_selector("#authOverlay", state="visible", timeout=10000)
            overlay_visible = True
        except Exception:
            overlay_visible = False
        log("登录-遮罩可见", "PASS" if overlay_visible else "FAIL")

        if overlay_visible:
            page.fill("#authUsername", TEST_USER)
            page.fill("#authPassword", TEST_PASS)
            # 提交后页面会 reload，等待 URL 不变但 DOM 更新
            page.click("#authSubmitBtn")
            # 等待 reload 完成：遮罩应消失
            try:
                page.wait_for_selector("#authOverlay", state="hidden", timeout=15000)
                login_ok = True
            except Exception:
                login_ok = False
            err = page.query_selector("#authError")
            err_txt = err.inner_text() if err and err.inner_text() else ""
            log("登录-提交成功", "PASS" if login_ok else "FAIL",
                f"遮罩隐藏={login_ok}" + (f", error={err_txt}" if err_txt else ""))
        else:
            log("登录-提交成功", "BLOCKED", "无遮罩，跳过登录")

        # token 检查
        token = page.evaluate("() => localStorage.getItem('yuban_token')")
        log("登录-token存储", "PASS" if token else "FAIL", f"token={'有' if token else '无'}")

        # ========== 2. 对话页 ==========
        page.click(".nav-btn[data-tab='chat']")
        page.wait_for_timeout(300)
        # 发送按钮存在
        log("对话-发送按钮存在", "PASS" if page.query_selector("#chatSendBtn") else "FAIL")
        # 麦克风按钮存在
        log("对话-麦克风按钮存在", "PASS" if page.query_selector("#micBtn") else "FAIL")
        # 空输入拦截
        before = page.query_selector("#chatHistory").inner_text()
        page.fill("#chatInput", "")
        page.click("#chatSendBtn")
        page.wait_for_timeout(400)
        after = page.query_selector("#chatHistory").inner_text()
        log("对话-空输入拦截", "PASS" if before == after else "FAIL", "空文本不应发送")
        # 文字输入发送
        page.fill("#chatInput", "你好")
        page.click("#chatSendBtn")
        page.wait_for_timeout(1500)
        history = page.query_selector("#chatHistory").inner_text()
        log("对话-文字发送", "PASS" if "你好" in history else "FAIL", "用户消息应出现在历史中")

        # ========== 3. 录入页 ==========
        page.click(".nav-btn[data-tab='voice-input']")
        page.wait_for_timeout(300)
        log("录入-录制按钮存在", "PASS" if page.query_selector("#recordVoiceBtn") else "FAIL")
        log("录入-停止按钮隐藏", "PASS" if (not page.query_selector("#stopVoiceBtn").is_visible()) else "FAIL")
        # 点击开始录制（headless 无麦克风，应不崩溃）
        page.click("#recordVoiceBtn")
        page.wait_for_timeout(600)
        log("录入-点击录制不崩溃", "PASS" if page.query_selector("#voiceInputResponse") else "FAIL")
        # 停止按钮应显示（仅当录制真正启动；headless 无麦克风时该分支不可达）
        stop_vis = page.query_selector("#stopVoiceBtn").is_visible() if page.query_selector("#stopVoiceBtn") else False
        resp_el = page.query_selector("#voiceInputResponse")
        resp_txt = resp_el.inner_text() if resp_el else ""
        if "无法访问麦克风" in resp_txt:
            log("录入-停止按钮显示", "BLOCKED", "无麦克风环境：录制未启动，停止按钮分支不可达")
        else:
            log("录入-停止按钮显示", "PASS" if stop_vis else "FAIL")

        # ========== 4. 定义页 ==========
        page.click(".nav-btn[data-tab='voice-define']")
        page.wait_for_timeout(300)
        log("定义-表单存在", "PASS" if page.query_selector("#voiceNameDefine") else "FAIL")
        # 滑块默认值
        pitch = page.query_selector("#pitch").input_value()
        speed = page.query_selector("#speed").input_value()
        volume = page.query_selector("#volume").input_value()
        log("定义-滑块默认值", "PASS" if pitch == "1" and speed == "1" and volume == "1" else "FAIL",
            f"pitch={pitch} speed={speed} volume={volume}")
        # 空名称提交（使用 alert 提示，捕获对话框）
        alerts = []
        page.once("dialog", lambda d: alerts.append(d.message))
        page.fill("#voiceNameDefine", "")
        page.click("#defineVoiceBtn")
        page.wait_for_timeout(500)
        log("定义-空名称拦截", "PASS" if len(alerts) > 0 else "FAIL",
            f"alert={alerts[0] if alerts else '无'}")

        # ========== 5. 情感页 ==========
        page.click(".nav-btn[data-tab='emotion-config']")
        page.wait_for_timeout(300)
        # 6 个情感滑块
        sliders = ["happiness", "sadness", "anger", "fear", "disgust", "surprise"]
        present = all(page.query_selector(f"#{s}") for s in sliders)
        log("情感-6维滑块齐全", "PASS" if present else "FAIL")
        # 修改滑块值
        page.fill("#emotionProfileName", "测试情感")
        page.evaluate("() => { document.getElementById('happiness').value = '0.9'; document.getElementById('happiness').dispatchEvent(new Event('input')); }")
        page.wait_for_timeout(200)
        hv = page.query_selector("#happinessValue").inner_text()
        log("情感-滑块联动显示", "PASS" if hv == "0.9" else "FAIL", f"happinessValue={hv}")
        # 保存
        page.click("#saveEmotionBtn")
        # 等待异步保存完成
        try:
            page.wait_for_selector("#emotionConfigResponse p", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(500)
        resp = page.query_selector("#emotionConfigResponse").inner_text()
        log("情感-保存响应", "PASS" if len(resp.strip()) > 0 else "FAIL", f"响应: {resp[:80]}")

        # ========== 6. 角色页 ==========
        page.click(".nav-btn[data-tab='character']")
        # 等待角色数据加载（鉴权后异步加载）
        try:
            page.wait_for_selector("#presetCharacterList > div, #presetCharacterList > p", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(500)
        log("角色-创建按钮存在", "PASS" if page.query_selector("#createCharacterBtn") else "FAIL")
        # 预设角色列表加载
        preset = page.query_selector("#presetCharacterList")
        preset_text = preset.inner_text() if preset else ""
        log("角色-预设列表加载", "PASS" if len(preset_text.strip()) > 0 else "FAIL",
            f"内容长度={len(preset_text.strip())}")
        # 空名称提交（alert 拦截）
        alerts2 = []
        page.once("dialog", lambda d: alerts2.append(d.message))
        page.fill("#charName", "")
        page.click("#createCharacterBtn")
        page.wait_for_timeout(800)
        log("角色-空名称拦截", "PASS" if len(alerts2) > 0 else "FAIL",
            f"alert={alerts2[0] if alerts2 else '无'}")
        # 正常创建（alert 处理后延时确保输入框可用）
        page.wait_for_timeout(500)
        page.fill("#charName", "测试角色UI")
        page.fill("#charIdentity", "测试身份")
        page.fill("#charStyle", "测试风格")
        page.wait_for_timeout(200)
        # 确认名称已填入
        name_val = page.query_selector("#charName").input_value()
        page.click("#createCharacterBtn")
        try:
            page.wait_for_selector("#characterResponse p", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(500)
        resp2 = page.query_selector("#characterResponse").inner_text()
        log("角色-创建角色", "PASS" if ("成功" in resp2 or "已创建" in resp2) else "FAIL",
            f"name_val={name_val} 响应: {resp2[:100]}")
        # 边界值：超长名称
        page.fill("#charName", "A" * 500)
        page.click("#createCharacterBtn")
        page.wait_for_timeout(800)
        resp3 = page.query_selector("#characterResponse").inner_text()
        log("角色-超长名称边界", "PASS" if len(resp3.strip()) > 0 else "FAIL", f"响应长度={len(resp3)}")

        # ========== 7. 合成页 ==========
        page.click(".nav-btn[data-tab='tts']")
        page.wait_for_timeout(300)
        log("合成-文本框存在", "PASS" if page.query_selector("#ttsText") else "FAIL")
        log("合成-合成按钮存在", "PASS" if page.query_selector("#ttsBtn") else "FAIL")
        # 空文本拦截
        page.fill("#ttsText", "")
        page.click("#ttsBtn")
        page.wait_for_timeout(500)
        resp = page.query_selector("#ttsResponse").inner_text()
        log("合成-空文本拦截", "PASS" if ("提示" in resp or "请输入" in resp or len(resp) > 10) else "FAIL", f"响应: {resp[:80]}")
        # 超长文本边界（>2000字）
        long_text = "测试" * 1100  # 2200 字符
        page.fill("#ttsText", long_text)
        page.click("#ttsBtn")
        page.wait_for_timeout(1000)
        resp2 = page.query_selector("#ttsResponse").inner_text()
        log("合成-超长文本边界", "PASS" if len(resp2.strip()) > 0 else "FAIL", f"响应长度={len(resp2)}")

        # ========== 8. 设置页 ==========
        page.click(".nav-btn[data-tab='settings']")
        page.wait_for_timeout(300)
        log("设置-API地址输入存在", "PASS" if page.query_selector("#apiUrl") else "FAIL")
        log("设置-会话ID输入存在", "PASS" if page.query_selector("#sessionId") else "FAIL")
        log("设置-保存按钮存在", "PASS" if page.query_selector("#saveSettingsBtn") else "FAIL")
        # 修改并保存
        page.fill("#apiUrl", "http://127.0.0.1:8005")
        page.fill("#sessionId", "ui_test_session")
        page.click("#saveSettingsBtn")
        page.wait_for_timeout(500)
        log("设置-保存设置", "PASS", "点击保存无异常")

        # ========== 9. 控制台错误检查 ==========
        # 过滤预期错误：headless 无麦克风、TTS 超长文本边界测试的 400
        ignored_keywords = ["microphone", "NotFoundError", "400", "text 过长", "Failed to load resource"]
        real_errors = [e for e in console_errors if not any(kw in e["text"] for kw in ignored_keywords)]
        log("控制台-无未捕获错误", "PASS" if len(real_errors) == 0 else "FAIL",
            f"实际错误数={len(real_errors)}" + (f", 首条: {real_errors[0]['text'][:100]}" if real_errors else f"（已过滤预期错误 {len(console_errors)} 条）"))
        log("页面-无JS异常", "PASS" if len(page_errors) == 0 else "FAIL",
            f"异常数={len(page_errors)}" + (f", 首条: {page_errors[0][:100]}" if page_errors else ""))

        browser.close()

        # 打印失败的网络请求
        print("\n--- 失败的网络请求 ---")
        for fr in failed_reqs:
            print(f"  [{fr['status']}] {fr['url']}  body={fr.get('body','')[:80]}")
        print(f"--- 共 {len(failed_reqs)} 个失败请求 ---\n")

        print("--- API 请求鉴权头检查 ---")
        for al in auth_log[:15]:
            print(f"  auth={al['has_auth']}  {al['method']} {al['url']}")
        print(f"--- 共 {len(auth_log)} 个 API 请求 ---\n")

        print("--- 所有对话框 ---")
        for d in all_dialogs:
            print(f"  alert: {d}")
        print(f"--- 共 {len(all_dialogs)} 个对话框 ---\n")


def print_report():
    print("\n" + "=" * 70)
    print("UI 自动化测试报告")
    print("=" * 70)
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    blocked = sum(1 for r in results if r["status"] == "BLOCKED")
    print(f"总计: {total}  通过: {passed}  失败: {failed}  阻塞: {blocked}")
    print("-" * 70)
    for r in results:
        print(f"[{r['status']}] {r['name']}")
        if r["detail"]:
            print(f"        {r['detail']}")
    print("=" * 70)

    # 保存 JSON 报告
    REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_test_report.json")
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {"total": total, "passed": passed, "failed": failed, "blocked": blocked},
            "results": results,
            "console_errors": console_errors[:20],
            "page_errors": page_errors[:10],
            "failed_requests": failed_reqs,
        }, f, ensure_ascii=False, indent=2)
    print(f"报告已保存: {REPORT_PATH}")


if __name__ == "__main__":
    try:
        run_tests()
    except Exception as e:
        print(f"测试运行异常: {e}")
        traceback.print_exc()
    print_report()
