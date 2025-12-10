
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


import asyncio
import random
import string
import time
import aiohttp
from typing import Dict, Optional, Tuple, List
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import At, Plain
from astrbot.core.event import Event, EventKind, GroupMemberIncreaseData

# 存储验证数据：key为(群ID, 用户ID)，value为(随机代码, 生成时间)
verification_data: Dict[Tuple[str, str], Tuple[str, float]] = {}

@register("new_member_captcha", "开发者", "新成员验证插件", "1.2.0")
class NewMemberCaptchaPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.encsec = config.get("encsec", "")
        self.admin_id = config.get("admin_id", "")
        self.super_admin_ids = set(config.get("super_admin_ids", []))
        self.timeout_seconds = config.get("timeout_minutes", 5) * 60
        self.enable_help_command = config.get("enable_help_command", True)
        
        # 启动后台清理任务
        self.cleanup_task = asyncio.create_task(self._cleanup_expired_verifications())
        
        # 注册群成员增加事件处理器
        self.context.event_bus.register(EventKind.GroupMemberIncrease, self.on_group_member_increase)

    async def on_group_member_increase(self, event: Event):
        """处理新成员入群事件"""
        try:
            if event.kind != EventKind.GroupMemberIncrease:
                return
                
            data: GroupMemberIncreaseData = event.data
            group_id = data.group.id
            user_id = data.member.id
            
            await self._trigger_verification(group_id, user_id, is_admin_triggered=False)
                
        except Exception as e:
            logger.error(f"处理新成员入群事件时出错: {e}")

    async def _trigger_verification(self, group_id: str, user_id: str, is_admin_triggered: bool = False):
        """触发验证流程（可被管理员调用）"""
        try:
            # 生成随机验证码（8位字母数字）
            random_code = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
            current_time = time.time()
            
            # 存储验证数据
            verification_data[(group_id, user_id)] = (random_code, current_time)
            
            # 记录日志
            trigger_type = "管理员触发" if is_admin_triggered else "新成员入群"
            logger.info(f"{trigger_type}验证: 群{group_id}, 用户{user_id}, 验证码: {random_code}")
            
            # 构建消息
            if is_admin_triggered:
                message = [
                    At(user_id=user_id),
                    Plain(" 管理员已重新触发验证，请完成：\n"),
                    Plain(f"1. 访问 https://recaptcha.uslng.eu.org\n"),
                    Plain(f"2. 输入验证码: {random_code}\n"),
                    Plain(f"3. 将返回的代码发送到本群\n"),
                    Plain(f"请在{self.timeout_seconds//60}分钟内完成验证")
                ]
            else:
                message = [
                    At(user_id=user_id),
                    Plain(" 欢迎加入！请完成人机验证：\n"),
                    Plain(f"1. 访问 https://recaptcha.uslng.eu.org\n"),
                    Plain(f"2. 输入验证码: {random_code}\n"),
                    Plain(f"3. 将返回的代码发送到本群\n"),
                    Plain(f"请在{self.timeout_seconds//60}分钟内完成验证")
                ]
            
            # 发送消息
            from astrbot.api.message_components import MessageChain
            chain = MessageChain(message)
            
            # 获取平台适配器并发送消息
            platform = self.context.get_platform("aiocqhttp")  # 根据实际平台调整
            if platform:
                await platform.send_group_message(group_id, chain)
                return True
            else:
                logger.error("无法获取平台适配器，无法发送消息")
                return False
                
        except Exception as e:
            logger.error(f"触发验证时出错: {e}")
            return False

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """处理群消息，验证用户提交的代码"""
        try:
            group_id = event.get_group_id()
            user_id = event.get_sender_id()
            message = event.message_str.strip()
            
            # 检查是否为验证代码提交
            key = (group_id, user_id)
            if key not in verification_data:
                return
                
            random_code, create_time = verification_data[key]
            
            # 检查是否超时
            if time.time() - create_time > self.timeout_seconds:
                # 超时，@管理员
                del verification_data[key]
                if self.admin_id:
                    timeout_msg = [
                        At(user_id=user_id),
                        Plain(" 验证超时！"),
                        At(user_id=self.admin_id),
                        Plain(" 请处理")
                    ]
                    yield event.chain_result(timeout_msg)
                else:
                    yield event.plain_result(f"{At(user_id=user_id)} 验证已超时")
                return
            
            # 验证代码长度（防止过长的无效请求）
            if len(message) > 500:
                yield event.plain_result(f"{At(user_id=user_id)} 验证代码过长，请确认是否正确")
                return
            
            # POST请求验证
            async with aiohttp.ClientSession() as session:
                form_data = {
                    'action': 'decrypt',
                    'encsec': self.encsec,
                    'code': message
                }
                
                try:
                    async with session.post(
                        'https://recaptcha.uslng.eu.org/verify',
                        data=form_data,
                        timeout=10
                    ) as response:
                        if response.status == 200:
                            result = await response.json()
                            
                            if result.get('success', False):
                                decrypted_text = result.get('decrypted', '')
                                
                                # 检查是否包含随机代码
                                if random_code in decrypted_text:
                                    # 验证成功
                                    del verification_data[key]
                                    success_msg = [
                                        At(user_id=user_id),
                                        Plain(" 验证成功！欢迎加入群聊！")
                                    ]
                                    yield event.chain_result(success_msg)
                                    logger.info(f"用户{user_id}验证成功")
                                else:
                                    # 验证失败
                                    yield event.plain_result(f"{At(user_id=user_id)} 验证失败，请重试")
                            else:
                                # API返回失败
                                yield event.plain_result(f"{At(user_id=user_id)} 验证失败，请检查代码是否正确")
                        else:
                            # HTTP请求失败
                            yield event.plain_result(f"{At(user_id=user_id)} 验证服务暂时不可用，请稍后重试")
                            
                except asyncio.TimeoutError:
                    yield event.plain_result(f"{At(user_id=user_id)} 验证请求超时，请稍后重试")
                except Exception as e:
                    logger.error(f"验证请求出错: {e}")
                    yield event.plain_result(f"{At(user_id=user_id)} 验证过程出错，请稍后重试")
                    
        except Exception as e:
            logger.error(f"处理验证消息时出错: {e}")

    @filter.command("recaptcha", alias={'重新验证', '触发验证', '验证触发'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def recaptcha_command(self, event: AstrMessageEvent):
        """管理命令：重新触发验证
        
        用法: /recaptcha @用户 或 /recaptcha <用户ID>
        别名: /重新验证, /触发验证, /验证触发
        """
        try:
            # 解析消息链，查找被@的用户
            from astrbot.api.message_components import At as AtComponent
            
            target_user_id = None
            message_components = event.message_obj.message
            
            # 方法1: 从@消息中提取用户ID
            for component in message_components:
                if isinstance(component, AtComponent):
                    target_user_id = component.qq  # 对于QQ平台
                    break
            
            # 方法2: 从纯文本中提取用户ID（如果未@）
            if not target_user_id:
                message_parts = event.message_str.split()
                if len(message_parts) > 1:
                    # 尝试解析第二个参数为用户ID
                    potential_id = message_parts[1]
                    if potential_id.isdigit():
                        target_user_id = potential_id
            
            if not target_user_id:
                help_msg = [
                    Plain("请@要重新触发验证的用户，或提供用户ID\n"),
                    Plain("用法: /recaptcha @用户 或 /recaptcha <用户ID>\n"),
                    Plain("别名: /重新验证, /触发验证, /验证触发")
                ]
                yield event.chain_result(help_msg)
                return
            
            group_id = event.get_group_id()
            sender_id = event.get_sender_id()
            
            # 检查发送者是否有权限（管理员或超级管理员）
            is_super_admin = sender_id in self.super_admin_ids
            
            if not is_super_admin:
                # 普通管理员只能触发未验证的用户
                key = (group_id, target_user_id)
                if key not in verification_data:
                    yield event.plain_result("只能为未验证成功的用户重新触发验证")
                    return
            
            # 触发验证
            success = await self._trigger_verification(group_id, target_user_id, is_admin_triggered=True)
            
            if success:
                yield event.plain_result(f"已为用户 {target_user_id} 重新触发验证")
            else:
                yield event.plain_result("触发验证失败，请检查日志")
                
        except Exception as e:
            logger.error(f"执行recaptcha命令时出错: {e}")
            yield event.plain_result("命令执行出错")

    @filter.command("check_verification", alias={'检查验证', '验证状态', '验证查询'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def check_verification_command(self, event: AstrMessageEvent):
        """管理命令：检查用户的验证状态
        
        用法: /check_verification @用户 或 /check_verification <用户ID>
        别名: /检查验证, /验证状态, /验证查询
        """
        try:
            # 解析消息链，查找被@的用户
            from astrbot.api.message_components import At as AtComponent
            
            target_user_id = None
            message_components = event.message_obj.message
            
            for component in message_components:
                if isinstance(component, AtComponent):
                    target_user_id = component.qq
                    break
            
            if not target_user_id:
                message_parts = event.message_str.split()
                if len(message_parts) > 1:
                    potential_id = message_parts[1]
                    if potential_id.isdigit():
                        target_user_id = potential_id
            
            if not target_user_id:
                help_msg = [
                    Plain("请@要检查的用户，或提供用户ID\n"),
                    Plain("用法: /check_verification @用户 或 /check_verification <用户ID>\n"),
                    Plain("别名: /检查验证, /验证状态, /验证查询")
                ]
                yield event.chain_result(help_msg)
                return
            
            group_id = event.get_group_id()
            key = (group_id, target_user_id)
            
            if key in verification_data:
                random_code, create_time = verification_data[key]
                elapsed_time = time.time() - create_time
                remaining_time = max(0, self.timeout_seconds - elapsed_time)
                
                status_msg = [
                    Plain(f"用户 {target_user_id} 的验证状态:\n"),
                    Plain(f"• 状态: 等待验证\n"),
                    Plain(f"• 验证码: {random_code}\n"),
                    Plain(f"• 剩余时间: {int(remaining_time // 60)}分{int(remaining_time % 60)}秒\n"),
                    Plain(f"• 开始时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(create_time))}")
                ]
            else:
                status_msg = [Plain(f"用户 {target_user_id} 未在验证中或已验证成功")]
            
            yield event.chain_result(status_msg)
            
        except Exception as e:
            logger.error(f"执行check_verification命令时出错: {e}")
            yield event.plain_result("命令执行出错")

    @filter.command("clear_verification", alias={'清除验证', '强制通过', '跳过验证'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clear_verification_command(self, event: AstrMessageEvent):
        """管理命令：清除用户的验证状态（强制通过）
        
        用法: /clear_verification @用户 或 /clear_verification <用户ID>
        别名: /清除验证, /强制通过, /跳过验证
        """
        try:
            # 检查发送者是否是超级管理员
            sender_id = event.get_sender_id()
            if sender_id not in self.super_admin_ids:
                yield event.plain_result("只有超级管理员可以使用此命令")
                return
            
            # 解析消息链，查找被@的用户
            from astrbot.api.message_components import At as AtComponent
            
            target_user_id = None
            message_components = event.message_obj.message
            
            for component in message_components:
                if isinstance(component, AtComponent):
                    target_user_id = component.qq
                    break
            
            if not target_user_id:
                message_parts = event.message_str.split()
                if len(message_parts) > 1:
                    potential_id = message_parts[1]
                    if potential_id.isdigit():
                        target_user_id = potential_id
            
            if not target_user_id:
                help_msg = [
                    Plain("请@要清除验证的用户，或提供用户ID\n"),
                    Plain("用法: /clear_verification @用户 或 /clear_verification <用户ID>\n"),
                    Plain("别名: /清除验证, /强制通过, /跳过验证")
                ]
                yield event.chain_result(help_msg)
                return
            
            group_id = event.get_group_id()
            key = (group_id, target_user_id)
            
            if key in verification_data:
                del verification_data[key]
                success_msg = [
                    At(user_id=target_user_id),
                    Plain(" 的验证状态已被清除，现在可以正常发言")
                ]
                yield event.chain_result(success_msg)
            else:
                yield event.plain_result(f"用户 {target_user_id} 未在验证中")
            
        except Exception as e:
            logger.error(f"执行clear_verification命令时出错: {e}")
            yield event.plain_result("命令执行出错")

    @filter.command("captcha_help", alias={'验证帮助', '验证指令', '帮助'})
    async def captcha_help_command(self, event: AstrMessageEvent):
        """显示验证插件的帮助信息
        
        用法: /captcha_help 或 /验证帮助
        别名: /验证帮助, /验证指令, /帮助
        """
        if not self.enable_help_command:
            return
        
        help_text = [
            Plain("=== 新成员验证插件帮助 ===\n"),
            Plain("\n【新成员验证流程】\n"),
            Plain("1. 新成员入群自动触发验证\n"),
            Plain("2. 用户访问 https://recaptcha.uslng.eu.org\n"),
            Plain("3. 输入系统提供的验证码\n"),
            Plain("4. 将网站返回的代码发送到群内\n"),
            Plain("5. 系统自动验证并提示结果\n"),
            Plain("\n【管理命令】\n"),
            Plain("• /recaptcha @用户 - 重新触发验证 (别名: /重新验证, /触发验证)\n"),
            Plain("• /check_verification @用户 - 检查验证状态 (别名: /检查验证, /验证状态)\n"),
            Plain("• /clear_verification @用户 - 强制通过验证 (别名: /清除验证, /强制通过)\n"),
            Plain("• /captcha_help - 显示此帮助 (别名: /验证帮助, /验证指令)\n"),
            Plain("\n【注意事项】\n"),
            Plain("• 验证超时时间: {}分钟\n".format(self.timeout_seconds // 60)),
            Plain("• 普通管理员只能为未验证用户重新触发\n"),
            Plain("• 超级管理员可以强制通过验证\n"),
            Plain("• 超时后会@指定管理员: {}\n".format(self.admin_id if self.admin_id else "未设置"))
        ]
        
        yield event.chain_result(help_text)

    @filter.command_group("验证", alias={'captcha', 'recaptcha'})
    def captcha_group(self):
        """验证插件指令组"""
        pass

    @self.captcha_group.command("状态", alias={'status', 'check'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def captcha_status_subcommand(self, event: AstrMessageEvent):
        """指令组子命令：检查验证状态
        
        用法: /验证 状态 @用户
        """
        # 调用check_verification_command的逻辑
        await self.check_verification_command(event)

    @self.captcha_group.command("重新触发", alias={'retrigger', 'renew'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def captcha_retrigger_subcommand(self, event: AstrMessageEvent):
        """指令组子命令：重新触发验证
        
        用法: /验证 重新触发 @用户
        """
        # 调用recaptcha_command的逻辑
        await self.recaptcha_command(event)

    @self.captcha_group.command("强制通过", alias={'forcepass', 'clear'})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def captcha_forcepass_subcommand(self, event: AstrMessageEvent):
        """指令组子命令：强制通过验证
        
        用法: /验证 强制通过 @用户
        """
        # 调用clear_verification_command的逻辑
        await self.clear_verification_command(event)

    @self.captcha_group.command("帮助", alias={'help', 'manual'})
    async def captcha_group_help(self, event: AstrMessageEvent):
        """指令组子命令：显示帮助
        
        用法: /验证 帮助
        """
        # 调用captcha_help_command的逻辑
        await self.captcha_help_command(event)

    async def _cleanup_expired_verifications(self):
        """后台清理过期的验证记录"""
        while True:
            try:
                current_time = time.time()
                expired_keys = []
                
                for key, (_, create_time) in verification_data.items():
                    if current_time - create_time > self.timeout_seconds + 300:  # 额外5分钟缓冲
                        expired_keys.append(key)
                
                for key in expired_keys:
                    del verification_data[key]
                    logger.info(f"清理过期验证记录: {key}")
                    
                await asyncio.sleep(60)  # 每分钟检查一次
                
            except Exception as e:
                logger.error(f"清理验证记录时出错: {e}")
                await asyncio.sleep(60)

    async def terminate(self):
        """插件卸载时清理资源"""
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        
        # 清空验证数据
        verification_data.clear()
        
        # 注销事件处理器
        try:
            self.context.event_bus.unregister(EventKind.GroupMemberIncrease, self.on_group_member_increase)
        except:
            pass