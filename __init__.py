import json
import os
from typing import Dict
import httpx
from pydantic import Field

from nekro_agent.services.plugin.base import NekroPlugin, ConfigBase, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core import logger

# -------------------- 插件元数据 --------------------
plugin = NekroPlugin(
    name="Anuneko 多模型聊天",
    module_name="anuneko_chat",
    description="通过 Anuneko 前端与橘猫/黑猫模型进行多轮对话",
    version="1.0.0",
    author="XGGM",
    url="https://github.com/XGGM2020/anuneko-chat",
)

# -------------------- 配置定义 --------------------
@plugin.mount_config()
class AnunekoConfig(ConfigBase):
    """Anuneko 接口配置"""
    CHAT_API_URL: str = Field(
        default="https://anuneko.com/api/v1/chat",
        title="创建会话 API",
        description="POST 新会话的地址",
    )
    STREAM_API_URL: str = Field(
        default="https://anuneko.com/api/v1/msg/{uuid}/stream",
        title="流式对话 API",
        description="需要格式化填入 uuid",
    )
    SELECT_CHOICE_URL: str = Field(
        default="https://anuneko.com/api/v1/msg/select-choice",
        title="分支确认 API",
        description="自动选择分支 idx=0",
    )
    SELECT_MODEL_URL: str = Field(
        default="https://anuneko.com/api/v1/user/select_model",
        title="切换模型 API",
        description="切换会话模型",
    )
    DEFAULT_TOKEN: str = Field(
        default="x-token 自己改",
        title="默认鉴权 token",
        description="也可通过环境变量 ANUNEKO_TOKEN 覆盖",
    )
    WATERMARK: str = Field(
        default="\n\n—— 内容由 anuneko.com 提供，该服务只是一个第三方前端",
        title="回复水印",
        description="追加在每次返回内容末尾",
    )
    TIMEOUT: int = Field(
        default=10,
        title="普通请求超时(秒)",
        description="创建会话、切换模型等操作超时",
    )
    STREAM_TIMEOUT: int = Field(
        default=10,
        title="流式请求超时(秒)",
        description="None 表示无超时",
    )


config = plugin.get_config(AnunekoConfig)

# -------------------- 运行期内存数据 --------------------
user_sessions: Dict[str, str] = {}  # qq -> chat_id
user_models: Dict[str, str] = {}  # qq -> "Orange Cat" / "Exotic Shorthair"


# -------------------- 内部工具函数 --------------------
def _build_headers() -> Dict[str, str]:
    """构造通用请求头"""
    token = os.environ.get("ANUNEKO_TOKEN", config.DEFAULT_TOKEN)
    cookie = os.environ.get("ANUNEKO_COOKIE")

    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://anuneko.com",
        "referer": "https://anuneko.com/",
        "user-agent": "Mozilla/5.0",
        "x-app_id": "com.anuttacon.neko",
        "x-client_type": "4",
        "x-device_id": "7b75a432-6b24-48ad-b9d3-3dc57648e3e3",
        "x-token": token,
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


async def _create_new_session(user_id: str) -> str:
    """为用户创建新会话并返回 chat_id，失败返回空字符串"""
    headers = _build_headers()
    model = user_models.get(user_id, "Orange Cat")
    payload = {"model": model}

    try:
        async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
            resp = await client.post(config.CHAT_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            chat_id = data.get("chat_id") or data.get("id")
            if chat_id:
                user_sessions[user_id] = chat_id
                await _switch_model(user_id, chat_id, model)
                return chat_id
    except Exception as e:
        logger.exception(f"创建会话失败 for {user_id}: {e}")
    return ""


async def _switch_model(user_id: str, chat_id: str, model_name: str) -> bool:
    """切换指定会话的模型"""
    headers = _build_headers()
    payload = {"chat_id": chat_id, "model": model_name}

    try:
        async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
            resp = await client.post(config.SELECT_MODEL_URL, headers=headers, json=payload)
            if resp.status_code == 200:
                user_models[user_id] = model_name
                return True
    except Exception as e:
        logger.warning(f"切换模型失败 for {user_id}: {e}")
    return False


async def _send_choice(msg_id: str) -> None:
    """自动选择分支 idx=0"""
    headers = _build_headers()
    payload = {"msg_id": msg_id, "choice_idx": 0}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(config.SELECT_CHOICE_URL, headers=headers, json=payload)
    except Exception:
        pass


async def _stream_reply(session_uuid: str, text: str) -> str:
    """流式对话，返回完整回复字符串"""
    headers = {
        "x-token": os.environ.get("ANUNEKO_TOKEN", config.DEFAULT_TOKEN),
        "Content-Type": "text/plain",
    }
    if os.environ.get("ANUNEKO_COOKIE"):
        headers["Cookie"] = os.environ["ANUNEKO_COOKIE"]

    url = config.STREAM_API_URL.format(uuid=session_uuid)
    data = json.dumps({"contents": [text]}, ensure_ascii=False)

    result = ""
    current_msg_id: str = ""

    try:
        async with httpx.AsyncClient(timeout=config.STREAM_TIMEOUT) as client:
            async with client.stream("POST", url, headers=headers, content=data) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        # 错误提示
                        try:
                            if json.loads(line).get("code") == "chat_choice_shown":
                                return "⚠️ 检测到对话分支未选择，请重试或新建会话。"
                        except Exception:
                            continue
                        continue

                    try:
                        raw = line[6:]
                        if not raw.strip():
                            continue
                        j = json.loads(raw)

                        if "msg_id" in j:
                            current_msg_id = j["msg_id"]

                        # 多分支内容
                        if "c" in j and isinstance(j["c"], list):
                            for choice in j["c"]:
                                if choice.get("c", 0) == 0 and "v" in choice:
                                    result += choice["v"]
                        elif "v" in j and isinstance(j["v"], str):
                            result += j["v"]
                    except Exception:
                        continue

        if current_msg_id:
            await _send_choice(current_msg_id)

    except Exception as e:
        logger.exception(f"流式对话异常: {e}")
        return "请求失败，请稍后再试。"

    return result


# -------------------- 插件方法 --------------------
@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="切换模型",
    description="在橘猫(Orange Cat)与黑猫(Exotic Shorthair)之间切换当前会话模型",
)
async def switch_model(_ctx: AgentCtx, user_id: str, target: str) -> str:
    """切换模型并返回提示文本
    
    Args:
        user_id: QQ 用户唯一标识
        target: 目标模型关键词，支持"橘猫/orange"或"黑猫/exotic"
    
    Returns:
        str: 切换结果提示
    """
    if "橘猫" in target or "orange" in target.lower():
        target_model, target_name = "Orange Cat", "橘猫"
    elif "黑猫" in target or "exotic" in target.lower():
        target_model, target_name = "Exotic Shorthair", "黑猫"
    else:
        return "请指定要切换的模型：橘猫 / 黑猫"

    if user_id not in user_sessions:
        chat_id = await _create_new_session(user_id)
        if not chat_id:
            return "❌ 切换失败：无法创建会话"
    else:
        chat_id = user_sessions[user_id]

    ok = await _switch_model(user_id, chat_id, target_model)
    return f"✨ 已切换为：{target_name}" if ok else f"❌ 切换为 {target_name} 失败"


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="新建会话",
    description="为用户创建新的独立会话",
)
async def new_session(_ctx: AgentCtx, user_id: str) -> str:
    """新建会话并返回提示
    
    Args:
        user_id: QQ 用户唯一标识
    
    Returns:
        str: 创建结果提示
    """
    new_id = await _create_new_session(user_id)
    if new_id:
        model_name = "橘猫" if user_models.get(user_id) == "Orange Cat" else "黑猫"
        return f"已创建新的会话（当前模型：{model_name}）！"
    return "❌ 创建会话失败，请稍后再试。"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="/chat 指令对话",
    description="当用户消息以 /chat 开头时，自动调用本方法完成模型对话",
)
async def handle_chat_command(_ctx: AgentCtx, user_id: str, full_text: str) -> str:
    """处理以 /chat 开头的消息,并发送返回的消息

    Args:
        user_id: QQ 用户唯一标识
        full_text: 用户完整输入

    Returns:
        str: 完整模型回复（已追加水印），失败返回空字符串。
    """
    if not full_text.lstrip().startswith("/chat"):
        return ""

    content = full_text.lstrip()[5:].lstrip()  # 去掉 /chat 及前后空格
    if not content:
        return "❗ 请输入内容，例如：/chat 你好"

    # 获取或创建会话
    if user_id not in user_sessions:
        chat_id = await _create_new_session(user_id)
        if not chat_id:
            return "❌ 创建会话失败，请稍后再试。"
    session_id = user_sessions[user_id]

    reply = await _stream_reply(session_id, content)
    return reply + config.WATERMARK


# -------------------- 清理资源 --------------------
@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件运行期占用的内存会话数据"""
    user_sessions.clear()
    user_models.clear()
    logger.info("Anuneko 聊天插件会话数据已清理")
