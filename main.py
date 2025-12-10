
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
import aiohttp
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import AstrBotConfig
from astrbot.api.message_components import Plain, At
from astrbot.core.utils.session_waiter import SessionController, SessionWaiter

@register("astrbot_plugin_recaptcha", "YoungUsing", "入群自动验证插件", "1.0.0")
class GroupVerification(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # 生成随机验证码
    def _generate_random_code(self, length=8):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    # 检查是否为管理员
    def _is_admin(self, user_id: str):
        # 检查 AstrBot 全局配置的管理员或插件配置的额外管理员
        is_global_admin = user_id in self.context.config.super_users
        is_plugin_admin = user_id in self.config.get("extra_admins", [])
        return is_global_admin or is_plugin_admin

    # 监听所有消息/事件，用于捕获群成员增加
    # 注意：NapCat/OneBot 的 notice 事件在 AstrBot 中可能需要通过 raw_message 判断
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_event(self, event: AstrMessageEvent):
        raw = event.raw_message
        
        # 判断是否为群成员增加事件 (OneBot v11 标准)
        if isinstance(raw, dict) and raw.get('post_type') == 'notice' and raw.get('notice_type') == 'group_increase':
            user_id = str(raw.get('user_id'))
            group_id = str(raw.get('group_id'))
            
            # 这里的 event 对象可能不包含发送消息所需的全部上下文，
            # 因此我们需要构造一个发送目标，或者直接利用 event 对象回传（如果 AstrBot 封装允许）
            # 为了保险，我们通过 raw 数据构建回传链
            
            # 生成本次验证的随机码
            verify_code = self._generate_random_code()
            site_url = self.config.get("site", "http://localhost")
            
            # 提示消息
            msg_chain = [
                At(qq=user_id),
                Plain(f"\n欢迎入群！请在 5 分钟内完成人机验证。\n"),
                Plain(f"1. 访问验证地址: {site_url}\n"),
                Plain(f"2. 获取代码后，请在此群内直接发送该代码。\n"),
                Plain(f"管理员可回复“强制通过”跳过验证。")
            ]
            
            # 发送提示消息
            await self.context.send_message(event.unified_msg_origin, msg_chain)
            
            # 启动会话控制器
            # timeout=300秒 (5分钟)
            controller = SessionController(timeout=300, reset_timeout=False)
            
            try:
                # 使用 session_waiter 等待用户回复
                # 我们只关心当前群、当前用户的回复，或者管理员的指令
                async for new_event in SessionWaiter(controller):
                    # 过滤非本群消息
                    if str(new_event.message_obj.group_id) != group_id:
                        continue
                    
                    sender_id = str(new_event.get_sender_id())
                    text = new_event.message_str.strip()
                    
                    # 情况1：管理员强制通过
                    if text == "强制通过" and self._is_admin(sender_id):
                        yield new_event.plain_result(f"管理员 {sender_id} 已强制通过验证。")
                        controller.stop()
                        return

                    # 情况2：新成员发送了代码
                    if sender_id == user_id:
                        # 提交到 verify 接口
                        verify_result = await self._check_verification(text)
                        
                        if verify_result.get("success"):
                            decrypted_text = verify_result.get("decrypted", "")
                            
                            # 验证解密后的文本中是否包含我们的随机码
                            if verify_code in decrypted_text:
                                yield new_event.plain_result("验证成功！欢迎加入。")
                                controller.stop()
                                return
                            else:
                                yield new_event.plain_result("验证失败：返回代码无效，请检查后重新发送。")
                        else:
                            # 接口请求失败或返回 success: false
                            yield new_event.plain_result("验证接口校验失败，请重试。")
            
            except asyncio.TimeoutError:
                # 超时处理
                admin_id = self.config.get("timeout_admin_id", "")
                timeout_chain = [
                    Plain(f"用户 "),
                    At(qq=user_id),
                    Plain(" 验证超时 (5分钟未通过)。\n"),
                ]
                if admin_id:
                    timeout_chain.append(At(qq=admin_id))
                    timeout_chain.append(Plain(" 请关注。"))
                
                await self.context.send_message(event.unified_msg_origin, timeout_chain)

    async def _check_verification(self, user_code: str) -> dict:
        """
        发送 POST 请求进行验证
        """
        base_url = self.config.get("site", "").rstrip('/')
        api_url = f"{base_url}/verify"
        encsec = self.config.get("encsec", "")
        
        # 构造 form-data 参数
        payload = {
            "action": "decrypt",
            "encsec": encsec,
            "code": user_code
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                # 使用 data 参数发送 application/x-www-form-urlencoded
                async with session.post(api_url, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        try:
                            # 解析 JSON 响应
                            return await resp.json()
                        except:
                            return {"success": False, "error": "Invalid JSON"}
                    else:
                        return {"success": False, "error": f"HTTP {resp.status}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # 手动触发指令（用于测试或补救）
    @filter.command("verify", alias=["验证"])
    async def manual_verify(self, event: AstrMessageEvent, user_code: str):
        '''手动提交验证代码'''
        # 这里只做简单的提交测试，不涉及入群流程的随机码比对，
        # 实际使用中主要依靠 group_increase 触发的自动流程。
        # 如果需要手动验证某人，逻辑会比较复杂（需要知道当时的随机码）。
        # 此指令主要用于测试接口连通性。
        
        verify_result = await self._check_verification(user_code)
        if verify_result.get("success"):
            decrypted = verify_result.get("decrypted", "无内容")
            yield event.plain_result(f"接口测试通过。\n解密内容: {decrypted}")
        else:
            yield event.plain_result(f"接口验证失败。\n原因: {verify_result.get('error', '未知错误')}")