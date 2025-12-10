import time
import random
import string
import json
import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, At

# 这是一个调试标记，如果你在日志里没看到这句话，说明文件没更新成功
print("Loaded: GroupVerification Plugin - v1.0.5 (Clean Version)")

@register("group_verification", "AstrDeveloper", "入群自动验证插件", "1.0.5")
class GroupVerification(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pending_users = {}

    def _generate_random_code(self, length=8):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    def _is_admin(self, user_id: str):
        is_global_admin = user_id in self.context.config.super_users
        extra_admins = self.config.get("extra_admins", [])
        return is_global_admin or (user_id in extra_admins)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        raw = event.raw_message
        
        # 1. 识别群成员增加事件 (notice / group_increase)
        if isinstance(raw, dict) and raw.get('post_type') == 'notice' and raw.get('notice_type') == 'group_increase':
            await self._handle_new_member(event, raw)
            return

        # 2. 识别群消息
        if event.message_obj and event.message_obj.group_id:
            await self._handle_group_message(event)

    async def _handle_new_member(self, event: AstrMessageEvent, raw: dict):
        user_id = str(raw.get('user_id'))
        group_id = str(raw.get('group_id'))
        
        # 忽略自身
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
            Plain(f"\n欢迎入群！请在5分钟内完成验证。\n"),
            Plain(f"1. 访问验证地址: {site_url}\n"),
            Plain(f"2. 获取代码后，请直接在群内发送。\n"),
            Plain(f"(管理员可回复“强制通过 @用户”跳过)")
        ]
        
        try:
            await self.context.send_message(event.unified_msg_origin, msg_chain)
        except Exception:
            pass

    async def _handle_group_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id
        content = event.message_str.strip()
        key = f"{group_id}-{user_id}"

        # 管理员逻辑
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
                    yield event.plain_result(f"管理员已强制通过用户 {target_id}。")
                else:
                    yield event.plain_result("用户不在等待列表中。")
            return

        # 验证逻辑
        if key in self.pending_users:
            data = self.pending_users[key]
            
            if time.time() - data['time'] > 300:
                del self.pending_users[key]
                admin_id = self.config.get("timeout_admin_id", "")
                chain = [Plain("验证超时。")]
                if admin_id:
                    chain.append(At(qq=admin_id))
                yield event.chain_result(chain)
                return

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
        payload = {"action": "decrypt", "encsec": encsec, "code": code}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {"success": False, "error": f"HTTP {resp.status}"}
        except Exception as e:
            return {"success": False, "error": str(e)}