
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
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, At

@register("group_verification", "AstrDeveloper", "入群自动验证插件", "1.0.2")
class GroupVerification(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 内存中存储等待验证的用户信息
        # 格式: { "group_id-user_id": { "code": "验证码", "time": 时间戳, "origin": 消息来源对象 } }
        self.pending_users = {}

    # 生成随机验证码
    def _generate_random_code(self, length=8):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    # 检查是否为管理员
    def _is_admin(self, user_id: str):
        is_global_admin = user_id in self.context.config.super_users
        extra_admins = self.config.get("extra_admins", [])
        return is_global_admin or (user_id in extra_admins)

    # 监听所有消息事件
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        # 1. 优先处理 OneBot 通知的群成员增加事件
        # 注意：AstrBot 将通知也封装在事件中，我们需要检查 raw_message
        raw = event.raw_message
        if isinstance(raw, dict) and raw.get('post_type') == 'notice' and raw.get('notice_type') == 'group_increase':
            await self._handle_new_member(event, raw)
            return

        # 2. 处理普通的群聊消息（用于接收用户发送的验证码或管理员指令）
        if event.message_obj.group_id:
            await self._handle_group_message(event)

    # 处理新成员入群逻辑
    async def _handle_new_member(self, event: AstrMessageEvent, raw: dict):
        user_id = str(raw.get('user_id'))
        group_id = str(raw.get('group_id'))
        
        # 生成并记录验证码
        code = self._generate_random_code()
        self.pending_users[f"{group_id}-{user_id}"] = {
            "code": code,
            "time": time.time(),
            "origin": event.unified_msg_origin
        }
        
        site_url = self.config.get("site", "http://example.com")
        
        # 构建欢迎消息
        chain = [
            At(qq=user_id),
            Plain(f"\n欢迎加入！请在5分钟内完成验证。\n"),
            Plain(f"1. 前往: {site_url}\n"),
            Plain(f"2. 获取代码并发回群内。"),
        ]
        
        # 发送消息
        # 注意：notice 事件可能没有上下文，尝试使用 unified_msg_origin 发送
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception:
            pass

    # 处理消息回复逻辑
    async def _handle_group_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id
        content = event.message_str.strip()
        key = f"{group_id}-{user_id}"

        # --- 管理员强制通过 ---
        if content.startswith("强制通过") and self._is_admin(user_id):
            # 获取被 @ 的用户 ID
            target_id = None
            for component in event.message_obj.message:
                if isinstance(component, At):
                    target_id = str(component.qq)
                    break
            
            if target_id:
                target_key = f"{group_id}-{target_id}"
                if target_key in self.pending_users:
                    del self.pending_users[target_key]
                    yield event.plain_result(f"已强制通过用户 {target_id} 的验证。")
                else:
                    yield event.plain_result("该用户不在等待验证列表中。")
            return

        # --- 普通用户验证流程 ---
        if key in self.pending_users:
            data = self.pending_users[key]
            
            # 检查是否超时 (300秒)
            if time.time() - data['time'] > 300:
                del self.pending_users[key]
                admin_id = self.config.get("timeout_admin_id", "")
                
                # 构建超时提示
                timeout_msg = [Plain("验证超时。")]
                if admin_id:
                    timeout_msg.append(At(qq=admin_id))
                    timeout_msg.append(Plain(" 请关注。"))
                
                yield event.chain_result(timeout_msg)
                return

            # 请求接口验证
            verify_res = await self._verify_code_api(content)
            
            if verify_res.get("success"):
                decrypted = verify_res.get("decrypted", "")
                # 检查解密后的文本是否包含我们在入群时生成的随机码
                if data["code"] in decrypted:
                    del self.pending_users[key]
                    yield event.plain_result("验证通过，欢迎！")
                else:
                    yield event.plain_result("验证失败：提交的代码无效，请重新检查。")
            else:
                yield event.plain_result(f"验证系统错误: {verify_res.get('error', '未知')}")

    # 调用外部 API
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
                        return await resp.json()
                    return {"success": False, "error": f"HTTP {resp.status}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # 调试指令
    @filter.command("test_verify", alias=["测试验证"])
    async def debug_verify(self, event: AstrMessageEvent, code: str):
        '''测试解密接口'''
        if not self._is_admin(event.get_sender_id()):
            return
        res = await self._verify_code_api(code)
        yield event.plain_result(str(res))