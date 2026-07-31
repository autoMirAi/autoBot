from __future__ import annotations

import asyncio
import logging

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.exception import FinishedException

from autobot.codex import CodexRunner
from autobot.config import Settings

logger = logging.getLogger(__name__)
settings = Settings.from_env()
runner = CodexRunner(settings)

matcher = on_message(priority=20, block=False)

HELP = """可用命令：
/ask 问题 - 让 Codex 只读回答
/do 任务 - 让 Codex 执行操作（仅授权用户）
/reset - 清除本群对话上下文
/status - 查看状态
/help - 显示帮助
群里也可以直接 @我 提问。"""


def _scope(event: GroupMessageEvent) -> str:
    return f"group:{event.group_id}"


def _parse(event: GroupMessageEvent) -> tuple[str, str] | None:
    text = event.get_plaintext().strip()
    for command in ("/ask", "/do", "/reset", "/status", "/help"):
        if text == command:
            return command, ""
        if text.startswith(command + " "):
            return command, text[len(command) :].strip()
    if event.is_tome():
        return "/ask", text
    return None


@matcher.handle()
async def handle(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = str(event.group_id)
    if settings.allowed_group_ids and group_id not in settings.allowed_group_ids:
        return
    parsed = _parse(event)
    if parsed is None:
        return

    command, prompt = parsed
    user_id = str(event.user_id)
    scope = _scope(event)

    if command == "/help":
        await matcher.finish(Message(HELP))
    if command == "/status":
        status = "可用" if runner.available else "未安装或未登录"
        await matcher.finish(
            Message(
                f"autoBot 正常运行\nCodex: {status}\n"
                f"任务并发上限: {settings.max_parallel}\n"
                f"你是操作员: {'是' if user_id in settings.operator_qq_ids else '否'}"
            )
        )
    if command == "/reset":
        runner.reset(f"{scope}:read")
        runner.reset(f"{scope}:write")
        await matcher.finish(Message("本群的 Codex 对话上下文已清除。"))
    if command == "/do" and user_id not in settings.operator_qq_ids:
        await matcher.finish(Message("你没有执行操作的权限。"))

    writable = command == "/do"
    if not prompt:
        await matcher.finish(Message("请在命令后写明问题或任务。"))

    await matcher.send(Message("收到，Codex 正在处理…"))
    try:
        result = await runner.ask(scope, prompt, writable=writable)
        await matcher.finish(Message(result.text))
    except FinishedException:
        raise
    except (RuntimeError, ValueError) as exc:
        await matcher.finish(Message(f"处理失败：{exc}"))
    except Exception:
        logger.exception("Unhandled bot task failure")
        await matcher.finish(Message("处理失败：内部错误，请让管理员查看服务日志。"))
    finally:
        await asyncio.sleep(0)
