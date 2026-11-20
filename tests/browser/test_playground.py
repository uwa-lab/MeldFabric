from __future__ import annotations

import asyncio

from playwright.sync_api import expect, sync_playwright

from meldfabric import AgentKit
from tests.support.strands_model import ScriptedStrandsModel
from tests.support.workspace import configure_strands_workspace


def test_playground_creates_streams_reopens_and_deletes_history(
    tmp_path,
    monkeypatch,
    serve_admin,
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")
    kit = AgentKit(state_dir=tmp_path)
    model = ScriptedStrandsModel(lambda _messages, _tools, index: f"browser response {index}")
    asyncio.run(configure_strands_workspace(kit, model, include_orchestrations=False))
    origin = serve_admin(kit)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/admin/ui/playground")
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()

        expect(page.get_by_role("link", name="端点调试")).to_be_visible()
        page.get_by_role("button", name="选择端点").click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_role("heading", name="新建调试会话")).to_be_visible()
        endpoint = dialog.locator(".endpoint-picker > button").first
        expect(endpoint).to_contain_text("agent_endpoint")
        endpoint.click()
        dialog.get_by_role("button", name="创建会话").click()

        layout = page.locator(".playground-layout")
        expect(layout).to_be_visible()
        expect(page.locator(".playground-page")).to_have_count(0)
        history_box = page.locator(".playground-history").bounding_box()
        chat_box = page.locator(".playground-chat").bounding_box()
        assert history_box is not None and chat_box is not None
        assert history_box["x"] < chat_box["x"]
        assert history_box["height"] == chat_box["height"]
        assert (
            page.locator(".conversation-list").evaluate(
                "element => getComputedStyle(element).overflowY"
            )
            == "auto"
        )

        composer = page.locator(".composer-surface textarea")
        expect(composer).to_be_visible()
        composer.fill("pinyin-in-progress")
        composer.dispatch_event("compositionstart", {"data": "pinyin-in-progress"})
        composer.dispatch_event(
            "keydown",
            {
                "key": "Enter",
                "code": "Enter",
                "keyCode": 229,
                "isComposing": True,
            },
        )
        expect(composer).to_have_value("pinyin-in-progress")
        expect(page.locator(".timeline-entry.message.user")).to_have_count(0)
        composer.dispatch_event("compositionend", {"data": "pinyin-in-progress"})
        page.evaluate(
            """() => {
                window.__playgroundLoadingTransitions = 0;
                const chat = document.querySelector('.playground-chat');
                new MutationObserver(() => {
                  if (chat.querySelector('.chat-loading'))
                    window.__playgroundLoadingTransitions += 1;
                }).observe(chat, { childList: true, subtree: true });
            }"""
        )
        composer.fill("hello from browser")
        page.get_by_role("button", name="发送").click()
        page.wait_for_function(
            """() => {
                const messages = [...document.querySelectorAll(
                  '.timeline-entry.assistant .message-copy > p'
                )];
                const text = messages.at(-1)?.textContent?.trim() || '';
                return text && text !== '正在生成…' && text !== 'browser response 1';
            }"""
        )
        expect(
            page.locator(".message-timeline").get_by_text("browser response 1", exact=True)
        ).to_be_visible()
        user_entry = page.locator(".timeline-entry.message.user").last
        assistant_entry = page.locator(".timeline-entry.message.assistant").last
        user_avatar = user_entry.locator(".speaker-mark").bounding_box()
        user_bubble = user_entry.locator(".message-copy > p").bounding_box()
        assistant_avatar = assistant_entry.locator(".speaker-mark").bounding_box()
        assistant_copy = assistant_entry.locator(".message-copy > p").bounding_box()
        assert all(
            item is not None
            for item in (user_avatar, user_bubble, assistant_avatar, assistant_copy)
        )
        assert user_avatar["x"] > user_bubble["x"]
        assert assistant_avatar["x"] < assistant_copy["x"]
        expect(user_entry.locator(".message-copy > header strong")).to_have_text(
            kit.config.workspace.name
        )
        assert (
            user_entry.locator(".message-copy > p").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            == "rgb(37, 37, 34)"
        )
        assert (
            user_entry.locator(".message-copy > p").evaluate(
                "element => getComputedStyle(element).color"
            )
            == "rgb(255, 255, 255)"
        )
        assert (
            assistant_entry.locator(".message-copy > p").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            == "rgba(0, 0, 0, 0)"
        )
        assert page.evaluate("window.__playgroundLoadingTransitions") == 0
        expect(page.locator(".conversation-list")).to_contain_text("hello from browser")

        page.reload()
        expect(page.get_by_text("hello from browser", exact=True).first).to_be_visible()
        expect(
            page.locator(".message-timeline").get_by_text("browser response 1", exact=True)
        ).to_be_visible()
        expect(page.locator(".chat-header .ui-description")).to_contain_text(
            f"playground:{kit.config.workspace.id}"
        )
        expect(page.locator(".chat-composer > small")).to_have_count(0)

        page.set_viewport_size({"width": 760, "height": 900})
        sidebar = page.locator(".sidebar")
        assert sidebar.evaluate("element => getComputedStyle(element).boxShadow") == "none"
        page.locator(".mobile-menu").click()
        expect(sidebar).to_have_class("sidebar open")
        page.wait_for_function(
            "getComputedStyle(document.querySelector('.sidebar')).boxShadow !== 'none'"
        )
        page.evaluate("document.querySelector('.mobile-menu').click()")
        expect(sidebar).to_have_class("sidebar")
        page.wait_for_function(
            "getComputedStyle(document.querySelector('.sidebar')).boxShadow === 'none'"
        )
        page.set_viewport_size({"width": 1440, "height": 900})

        page.get_by_role("button", name="删除历史").click()
        confirm = page.get_by_role("dialog")
        expect(confirm).to_contain_text("不会清除 Agent 的会话快照或长期记忆")
        confirm.get_by_role("button", name="删除").click()
        expect(page.get_by_role("heading", name="在这里验证已发布的 Agent")).to_be_visible()
        expect(page.locator(".conversation-list")).to_contain_text("暂无会话")
        assert not errors
        browser.close()


def test_playground_streams_multiagent_node_output_before_run_finishes(
    tmp_path,
    monkeypatch,
    serve_admin,
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")
    kit = AgentKit(state_dir=tmp_path)
    model = ScriptedStrandsModel(lambda *_args: "multiagent streaming response")
    asyncio.run(configure_strands_workspace(kit, model, include_orchestrations=True))
    origin = serve_admin(kit)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(origin + "/admin/ui/playground")
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()
        page.get_by_role("button", name="选择端点").click()
        dialog = page.get_by_role("dialog")
        swarm = dialog.locator(".endpoint-picker > button").filter(has_text="swarm_endpoint")
        swarm.click()
        dialog.get_by_role("button", name="创建会话").click()

        composer = page.locator(".composer-surface textarea")
        expect(composer).to_be_visible()
        composer.fill("stream the team answer")
        page.get_by_role("button", name="发送").click()
        expect(page.locator(".stream-pending")).to_be_visible()
        page.wait_for_function(
            """() => {
                const text = document.querySelector(
                  '.timeline-entry.assistant:not(.stream-pending) .message-copy > p'
                )?.textContent || '';
                return text.includes('coordinator:') &&
                  text.length < 'coordinator: multiagent streaming response'.length;
            }"""
        )
        expect(
            page.locator(".timeline-entry.assistant:not(.stream-pending) .message-copy > p")
        ).to_have_text("coordinator: multiagent streaming response")
        expect(page.locator(".stream-pending")).to_have_count(0)
        browser.close()
