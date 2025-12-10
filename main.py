
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


import json
import random
import string
import time
import aiohttp
from astrbot.api import AstrBotConfig  # 修正导入路径
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, At

@register("group_verification", "AstrDeveloper", "入群自动验证插件", "1.0.1")
class GroupVerification(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 用于存储待验证用户: {group_id-user_id: {"code": "xyz", "time": 1234567890, "origin": event_origin}}
        self.pending_users = {}

    def _generate_random_code(self, length=8):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    def _is_admin(self, user_id: str):
        # 检查 AstrBot 全局配置的管理员或插件配置的额外管理员
        is_global_admin = user_id in self.context.config.super_users
        extra_admins = self.config.get("extra_admins", [])
        is_plugin_admin = user_id in extra_admins
        return is_global_admin or is_plugin_admin

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        # 1. 处理群成员增加事件 (通过 raw_message 判断)
        raw = event.raw_message
        if isinstance(raw, dict) and raw.get('post_type') == 'notice' and raw.get('notice_type') == 'group_increase':
            await self._handle_group_increase(event, raw)
            return

        # 2. 处理消息事件 (用户提交代码 或 管理员强制通过)
        # 确保是群消息
        if event.message_obj.group_id:
            await self._handle_group_message(event)

    async def _handle_group_increase(self, event: AstrMessageEvent, raw: dict):
        user_id = str(raw.get('user_id'))
        group_id = str(raw.get('group_id'))
        key = f"{group_id}-{user_id}"

        # 生成验证信息
        verify_code = self._generate_random_code()
        site_url = self.config.get("site", "http://localhost")
        
        # 记录状态
        self.pending_users[key] = {
            "code": verify_code,
            "time": time.time(),
            "origin": event.unified_msg_origin
        }

        # 发送提示
        msg_chain = [
            At(qq=user_id),
            Plain(f"\n欢迎入群！请在 5 分钟内完成人机验证。\n"),
            Plain(f"1. 访问验证地址: {site_url}\n"),
            Plain(f"2. 获取代码后，请在此群内直接发送该代码。\n"),
            Plain(f"管理员可回复“强制通过 @用户”跳过验证。")
        ]
        
        # 注意：notice 事件可能没有标准的 message_chain 构造能力，使用 unified_msg_origin 尝试发送
        # 如果 raw_message 中包含 sender_id 等信息，AstrBot 通常能构建出 origin
        try:
            await self.context.send_message(event.unified_msg_origin, msg_chain)
        except Exception as e:
            # 如果 notice 事件无法直接回传，尝试构建一个新的 MessageChain (视具体 Adapter 实现而定)
            pass

    async def _handle_group_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id
        text = event.message_str.strip()
        key = f"{group_id}-{user_id}"

        # --- 逻辑 A: 管理员强制通过 ---
        if text.startswith("强制通过") and self._is_admin(user_id):
            # 检查是否有 @用户
            target_id = None
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    target_id = str(comp.qq)
                    break
            
            if target_id:
                target_key = f"{group_id}-{target_id}"
                if target_key in self.pending_users:
                    del self.pending_users[target_key]
                    yield event.plain_result(f"管理员已强制通过用户 {target_id} 的验证。")
                else:
                    yield event.plain_result(f"用户 {target_id} 不在等待验证列表中。")
            return

        # --- 逻辑 B: 用户提交验证码 ---
        if key in self.pending_users:
            user_data = self.pending_users[key]
            
            # 1. 检查超时
            if time.time() - user_data["time"] > 300: # 300秒 = 5分钟
                del self.pending_users[key]
                # 超时通知管理员
                admin_id = self.config.get("timeout_admin_id", "")
                chain = [Plain("验证超时。")]
                if admin_id:
                    chain.append(At(qq=admin_id))
                yield event.chain_result(chain)
                return

            # 2. 调用接口验证
            verify_result = await self._check_verification(text)
            
            if verify_result.get("success"):
                decrypted_text = verify_result.get("decrypted", "")
                if user_data["code"] in decrypted_text:
                    del self.pending_users[key]
                    yield event.plain_result("验证成功！欢迎加入。")
                else:
                    yield event.plain_result("验证失败：代码无效或不匹配，请检查后重新发送。")
            else:
                yield event.plain_result(f"验证接口错误: {verify_result.get('error', '未知错误')}")

    async def _check_verification(self, user_code: str) -> dict:
        base_url = self.config.get("site", "").rstrip('/')
        api_url = f"{base_url}/verify"
        encsec = self.config.get("encsec", "")
        
        payload = {
            "action": "decrypt",
            "encsec": encsec,
            "code": user_code
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json()
                        except:
                            return {"success": False, "error": "Invalid JSON"}
                    else:
                        return {"success": False, "error": f"HTTP {resp.status}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # 手动测试指令
    @filter.command("verify_test", alias=["测试验证"])
    async def manual_verify_test(self, event: AstrMessageEvent, code: str):
        '''测试解密接口连通性'''
        res = await self._check_verification(code)
        yield event.plain_result(f"接口返回: {res}")