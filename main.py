
# from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
# from astrbot.api.star import Context, Star, register
# from astrbot.api import logger

# @register("helloworld", "YourName", "一个简单的 Hello World 插件", "1.0.0")
# class MyPlugin(Star):
#     def __init__(self, context: Context):
#         super().__init__(context)

#     async def initialize(self):
#         """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

#     # 注册指令的装饰器。指令名为 helloworld。注册成功后，发送 `/helloworld` 就会触发这个指令，并回复 `你好, {user_name}!`
#     @filter.command("helloworld")
#     async def helloworld(self, event: AstrMessageEvent):
#         """这是一个 hello world 指令""" # 这是 handler 的描述，将会被解析方便用户了解插件内容。建议填写。
#         user_name = event.get_sender_name()
#         message_str = event.message_str # 用户发的纯文本消息字符串
#         message_chain = event.get_messages() # 用户所发的消息的消息链 # from astrbot.api.message_components import *
#         logger.info(message_chain)
#         yield event.plain_result(f"Hello, {user_name}, 你发了 {message_str}!") # 发送一条纯文本消息

#     async def terminate(self):
#         """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""

import time
import random
import string
import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, At

@register("group_verification", "AstrDeveloper", "入群自动验证插件", "1.0.3")
class GroupVerification(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 内存中存储等待验证的用户信息
        self.pending_users = {}

    def _generate_random_code(self, length=8):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    def _is_admin(self, user_id: str):
        is_global_admin = user_id in self.context.config.super_users
        extra_admins = self.config.get("extra_admins", [])
        return is_global_admin or (user_id in extra_admins)

    # 监听所有类型的事件 (包括 OneBot 的通知事件)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        # 获取原始数据包，这是判断 OneBot notice 事件的唯一标准方式
        raw = event.raw_message
        
        # 1. 处理群成员增加事件 (notice)
        if isinstance(raw, dict) and raw.get('post_type') == 'notice' and raw.get('notice_type') == 'group_increase':
            await self._handle_new_member(event, raw)
            return

        # 2. 处理普通群消息 (message)
        # 只有当消息有具体的 message_obj 且来自群组时才处理
        if event.message_obj and event.message_obj.group_id:
            await self._handle_group_message(event)

    async def _handle_new_member(self, event: AstrMessageEvent, raw: dict):
        user_id = str(raw.get('user_id'))
        group_id = str(raw.get('group_id'))
        
        # 生成验证码
        code = self._generate_random_code()
        # 记录状态：验证码、入群时间
        self.pending_users[f"{group_id}-{user_id}"] = {
            "code": code,
            "time": time.time()
        }
        
        site_url = self.config.get("site", "http://example.com")
        
        # 构建欢迎消息
        chain = [
            At(qq=user_id),
            Plain(f"\n欢迎入群！请在 5 分钟内完成人机验证。\n"),
            Plain(f"1. 访问: {site_url}\n"),
            Plain(f"2. 获取代码后，请在此群内发送该代码。\n"),
            Plain(f"管理员可回复“强制通过 @用户”跳过验证。")
        ]
        
        # 尝试通过 unified_msg_origin 发送消息
        # 注意：notice 事件不一定能直接 reply，所以直接发到群里
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception as e:
            # 如果无法直接回复 notice 事件，尝试构建通用发送逻辑（依赖 AstrBot 实现）
            pass

    async def _handle_group_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id
        content = event.message_str.strip()
        key = f"{group_id}-{user_id}"

        # --- 管理员强制通过 ---
        if content.startswith("强制通过") and self._is_admin(user_id):
            target_id = None
            # 解析被 @ 的用户
            for component in event.message_obj.message:
                if isinstance(component, At):
                    target_id = str(component.qq)
                    break
            
            if target_id:
                target_key = f"{group_id}-{target_id}"
                if target_key in self.pending_users:
                    del self.pending_users[target_key]
                    yield event.plain_result(f"管理员已强制通过用户 {target_id} 的验证。")
                else:
                    yield event.plain_result("该用户不在等待验证列表中。")
            return

        # --- 用户提交验证码 ---
        if key in self.pending_users:
            data = self.pending_users[key]
            
            # 检查超时 (5分钟 = 300秒)
            if time.time() - data['time'] > 300:
                del self.pending_users[key]
                admin_id = self.config.get("timeout_admin_id", "")
                
                # 构建超时通知
                timeout_chain = [Plain("验证超时。")]
                if admin_id:
                    timeout_chain.append(At(qq=admin_id))
                    timeout_chain.append(Plain(" 请关注。"))
                yield event.chain_result(timeout_chain)
                return

            # 调用接口验证
            verify_res = await self._verify_code_api(content)
            
            if verify_res.get("success"):
                decrypted = verify_res.get("decrypted", "")
                # 比对解密后的内容是否包含随机码
                if data["code"] in decrypted:
                    del self.pending_users[key]
                    yield event.plain_result("验证通过，欢迎加入！")
                else:
                    yield event.plain_result("验证失败：提交的代码无效，请检查后重试。")
            else:
                yield event.plain_result(f"验证接口错误: {verify_res.get('error', '未知错误')}")

    async def _verify_code_api(self, code_str: str) -> dict:
        base_url = self.config.get("site", "").rstrip("/")
        url = f"{base_url}/verify"
        encsec = self.config.get("encsec", "")
        
        payload = {
            "action": "decrypt",
            "encsec": encsec,
            "code": code_str
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json()
                        except:
                            return {"success": False, "error": "Invalid JSON"}
                    return {"success": False, "error": f"HTTP {resp.status}"}
        except Exception as e:
            return {"success": False, "error": str(e)}