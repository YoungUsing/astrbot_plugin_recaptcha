
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
import json
import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, At

# 注册插件，添加 print 确认加载成功
print("Loading GroupVerification Plugin...")

@register("group_verification", "AstrDeveloper", "入群自动验证插件", "1.0.4")
class GroupVerification(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pending_users = {}
        print("GroupVerification Plugin initialized.")

    def _generate_random_code(self, length=8):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    def _is_admin(self, user_id: str):
        is_global_admin = user_id in self.context.config.super_users
        extra_admins = self.config.get("extra_admins", [])
        return is_global_admin or (user_id in extra_admins)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        # 调试日志：打印收到的事件类型，确保能收到通知
        # raw_message 是 OneBot 协议的原始字典
        raw = event.raw_message
        
        # 1. 识别群成员增加事件 (notice / group_increase)
        if isinstance(raw, dict) and raw.get('post_type') == 'notice' and raw.get('notice_type') == 'group_increase':
            await self._handle_new_member(event, raw)
            return

        # 2. 识别群消息 (用于接收验证码)
        if event.message_obj and event.message_obj.group_id:
            await self._handle_group_message(event)

    async def _handle_new_member(self, event: AstrMessageEvent, raw: dict):
        user_id = str(raw.get('user_id'))
        group_id = str(raw.get('group_id'))
        
        # 排除机器人自己入群的情况
        self_id = str(raw.get('self_id', ''))
        if user_id == self_id:
            return

        code = self._generate_random_code()
        self.pending_users[f"{group_id}-{user_id}"] = {
            "code": code,
            "time": time.time()
        }
        
        site_url = self.config.get("site", "http://example.com")
        
        msg_chain = [
            At(qq=user_id),
            Plain(f"\n欢迎入群！请在5分钟内完成人机验证。\n"),
            Plain(f"1. 访问验证地址: {site_url}\n"),
            Plain(f"2. 获取代码后，请直接在群内发送。\n"),
            Plain(f"(管理员可回复“强制通过 @用户”跳过)")
        ]
        
        # 尝试发送消息
        try:
            await self.context.send_message(event.unified_msg_origin, msg_chain)
        except Exception:
            pass

    async def _handle_group_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id
        content = event.message_str.strip()
        key = f"{group_id}-{user_id}"

        # --- 管理员强制通过 ---
        if content.startswith("强制通过") and self._is_admin(user_id):
            target_id = None
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    target_id = str(comp.qq)
                    break
            
            if target_id:
                target_key = f"{group_id}-{target_id}"
                if target_key in self.pending_users:
                    del self.pending_users[target_key]
                    yield event.plain_result(f"已强制通过用户 {target_id}。")
                else:
                    yield event.plain_result("该用户不在等待列表中。")
            return

        # --- 验证逻辑 ---
        if key in self.pending_users:
            data = self.pending_users[key]
            
            # 1. 检查超时
            if time.time() - data['time'] > 300: # 5分钟
                del self.pending_users[key]
                admin_id = self.config.get("timeout_admin_id", "")
                chain = [Plain("验证超时，已移除等待状态。")]
                if admin_id:
                    chain.append(At(qq=admin_id))
                yield event.chain_result(chain)
                return

            # 2. 提交接口验证
            verify_res = await self._check_api(content)
            
            if verify_res.get("success"):
                decrypted = verify_res.get("decrypted", "")
                if data["code"] in decrypted:
                    del self.pending_users[key]
                    yield event.plain_result("验证通过！")
                else:
                    yield event.plain_result("验证失败：代码无效。")
            else:
                yield event.plain_result(f"接口错误: {verify_res.get('error')}")

    async def _check_api(self, code: str):
        base_url = self.config.get("site", "").rstrip("/")
        url = f"{base_url}/verify"
        encsec = self.config.get("encsec", "")
        
        payload = {
            "action": "decrypt",
            "encsec": encsec,
            "code": code
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json()
                        except:
                            return {"success": False, "error": "JSON解析失败"}
                    else:
                        return {"success": False, "error": f"HTTP {resp.status}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # 测试指令
    @filter.command("verify_test", alias=["测试验证"])
    async def verify_test(self, event: AstrMessageEvent, code: str):
        if not self._is_admin(event.get_sender_id()):
            return
        res = await self._check_api(code)
        yield event.plain_result(f"接口测试返回: {res}")