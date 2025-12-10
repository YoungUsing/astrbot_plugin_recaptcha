import time
import random
import string
import json
import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, At

# 启动时打印日志，证明加载的是新文件
print("========== [Recaptcha Plugin] Loading New Version 1.0.6 ==========")

@register("group_verification", "AstrDeveloper", "入群自动验证插件", "1.0.6")
class GroupVerification(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pending_users = {}
        print("========== [Recaptcha Plugin] Initialized ==========")

    def _generate_random_code(self, length=8):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    def _is_admin(self, user_id: str):
        # 兼容 super_users 配置，有些版本是 list 有些是 set
        super_users = self.context.config.super_users
        is_global = user_id in super_users
        
        extra = self.config.get("extra_admins", [])
        is_extra = user_id in extra
        return is_global or is_extra

    # 监听所有消息类型，这是捕获 Notice 事件的关键
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        raw = event.raw_message
        
        # 1. 优先判定是否为群成员增加通知 (Notice Event)
        # OneBot v11 标准: post_type=notice, notice_type=group_increase
        if isinstance(raw, dict) and raw.get('post_type') == 'notice' and raw.get('notice_type') == 'group_increase':
            await self._handle_notice(event, raw)
            return

        # 2. 判定是否为群聊消息 (Message Event)
        if event.message_obj and event.message_obj.group_id:
            await self._handle_message(event)

    async def _handle_notice(self, event: AstrMessageEvent, raw: dict):
        user_id = str(raw.get('user_id'))
        group_id = str(raw.get('group_id'))
        self_id = str(raw.get('self_id', ''))
        
        # 排除机器人自己
        if user_id == self_id:
            return

        # 生成验证码并记录
        code = self._generate_random_code()
        self.pending_users[f"{group_id}-{user_id}"] = {
            "code": code,
            "time": time.time()
        }
        
        site_url = self.config.get("site", "http://example.com")
        
        # 构造消息链
        chain = [
            At(qq=user_id),
            Plain(f"\n欢迎入群！请在5分钟内完成人机验证。\n"),
            Plain(f"1. 访问: {site_url}\n"),
            Plain(f"2. 获取代码后，请直接在此群发送。\n"),
            Plain(f"(管理员回复“强制通过 @用户”可跳过)")
        ]
        
        # 发送入群引导
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception:
            pass

    async def _handle_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id
        text = event.message_str.strip()
        key = f"{group_id}-{user_id}"

        # --- 管理员强制通过 ---
        if text.startswith("强制通过") and self._is_admin(user_id):
            target_id = None
            # 寻找被 @ 的人
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    target_id = str(comp.qq)
                    break
            
            if target_id:
                target_key = f"{group_id}-{target_id}"
                if target_key in self.pending_users:
                    del self.pending_users[target_key]
                    yield event.plain_result(f"管理员已强制通过用户 {target_id}。")
                else:
                    yield event.plain_result("该用户不在验证列表中。")
            return

        # --- 普通用户验证 ---
        if key in self.pending_users:
            data = self.pending_users[key]
            
            # 1. 超时检查
            if time.time() - data['time'] > 300: # 300秒
                del self.pending_users[key]
                yield event.plain_result("验证超时，请重新申请入群或联系管理员。")
                return

            # 2. 提交 API 验证
            verify_res = await self._check_api(text)
            
            if verify_res.get("success"):
                decrypted_text = verify_res.get("decrypted", "")
                if data["code"] in decrypted_text:
                    del self.pending_users[key]
                    yield event.plain_result("验证成功，欢迎加入！")
                else:
                    yield event.plain_result("验证失败：代码无效，请检查。")
            else:
                yield event.plain_result(f"验证接口错误: {verify_res.get('error')}")

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
                            return {"success": False, "error": "Invalid JSON"}
                    else:
                        return {"success": False, "error": f"HTTP {resp.status}"}
        except Exception as e:
            return {"success": False, "error": str(e)}