from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Image, Record
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.core.platform.sources.wcf.client import SimpleWcfClient

try:
    import pydub
except Exception:
    logger.warning(
        "检测到 pydub 库未安装，企业微信将无法语音收发。如需使用语音，请前往管理面板 -> 控制台 -> 安装 Pip 库安装 pydub。"
    )
    pass


class WcfPlatformEvent(AstrMessageEvent):
    def __init__(
        self,
        client: SimpleWcfClient,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client = client


    @staticmethod
    async def send_with_client(client: SimpleWcfClient, message: MessageChain, to_wx_id: str):
        for comp in message.chain:
            if isinstance(comp, Plain):
                await client.post_text(to_wx_id, comp.text)
            elif isinstance(comp, Image):
                await client.post_image(to_wx_id, comp.url)
            elif isinstance(comp, Record):
                pass
        

    async def send(self, message: MessageChain):
        message_obj = self.message_obj
        await self.send_with_client(self.client, message, message_obj.session_id)
        await super().send(message)
