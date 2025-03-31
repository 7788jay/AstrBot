import sys
import uuid
import asyncio
import quart

from astrbot.api.platform import (
    Platform,
    AstrBotMessage,
    MessageMember,
    PlatformMetadata,
    MessageType,
)
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image, Record
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.api.platform import register_platform_adapter
from astrbot.core import logger
from requests import Response

from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise import WeChatClient
from wechatpy.enterprise.messages import TextMessage, ImageMessage, VoiceMessage
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.enterprise import parse_message
from .wcf_event import WcfPlatformEvent

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


class WcfServer:
    def __init__(self, event_queue: asyncio.Queue, config: dict):
        self.server = quart.Quart(__name__)
        self.port = int(config.get("port"))
        self.callback_server_host = config.get("callback_server_host", "0.0.0.0")
        self.server.add_url_rule(
            "/callback/command", view_func=self.verify, methods=["GET"]
        )
        self.server.add_url_rule(
            "/callback/command", view_func=self.callback_command, methods=["POST"]
        )
        self.event_queue = event_queue

        self.callback = None
        self.shutdown_event = asyncio.Event()

    async def verify(self):
        logger.info(f"验证请求有效性: {quart.request.args}")
        args = quart.request.args
        try:
            logger.info("验证请求有效性成功。")
            return args
        except InvalidSignatureException:
            logger.error("验证请求有效性失败，签名异常，请检查配置。")
            raise

    async def callback_command(self):
        data = await quart.request.get_data()

        logger.info(f"解析成功: {data}")

        if self.callback:
            await self.callback(data)

        return "success"

    async def start_polling(self):
        logger.info(
            f"将在 {self.callback_server_host}:{self.port} 端口启动 WCF 适配器。"
        )
        await self.server.run_task(
            host=self.callback_server_host,
            port=self.port,
            shutdown_trigger=self.shutdown_trigger,
        )

    async def shutdown_trigger(self):
        await self.shutdown_event.wait()


@register_platform_adapter("wcf", "微信wcf适配器")
class WcfPlatformAdapter(Platform):
    def __init__(
        self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue
    ) -> None:
        super().__init__(event_queue)
        self.config = platform_config
        self.settingss = platform_settings
        self.client_self_id = uuid.uuid4().hex[:8]
        self.api_base_url = platform_config.get(
            "api_base_url", "https://qyapi.weixin.qq.com/cgi-bin/"
        )

        if not self.api_base_url:
            self.api_base_url = "https://qyapi.weixin.qq.com/cgi-bin/"

        if self.api_base_url.endswith("/"):
            self.api_base_url = self.api_base_url[:-1]
        if not self.api_base_url.endswith("/cgi-bin"):
            self.api_base_url += "/cgi-bin"

        if not self.api_base_url.endswith("/"):
            self.api_base_url += "/"

        self.server = WcfServer(self._event_queue, self.config)

        async def callback(msg):
            await self.convert_message(msg)

        self.server.callback = callback

    @override
    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ):
        await super().send_by_session(session, message_chain)

    @override
    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            "wcf",
            "wcf 适配器",
        )

    @override
    async def run(self):
        await self.server.start_polling()

    async def convert_message(self, msg):
        abm = AstrBotMessage()
        if msg.type == "text":
            assert isinstance(msg, TextMessage)
            abm.message_str = msg.content
            abm.self_id = str(msg.agent)
            abm.message = [Plain(msg.content)]
            abm.type = MessageType.FRIEND_MESSAGE
            abm.sender = MessageMember(
                msg.source,
                msg.source,
            )
            abm.message_id = msg.id
            abm.timestamp = msg.time
            abm.session_id = abm.sender.user_id
            abm.raw_message = msg
        elif msg.type == "image":
            assert isinstance(msg, ImageMessage)
            abm.message_str = "[图片]"
            abm.self_id = str(msg.agent)
            abm.message = [Image(file=msg.image, url=msg.image)]
            abm.type = MessageType.FRIEND_MESSAGE
            abm.sender = MessageMember(
                msg.source,
                msg.source,
            )
            abm.message_id = msg.id
            abm.timestamp = msg.time
            abm.session_id = abm.sender.user_id
            abm.raw_message = msg
        elif msg.type == "voice":
            assert isinstance(msg, VoiceMessage)

            resp: Response = await asyncio.get_event_loop().run_in_executor(
                None, self.client.media.download, msg.media_id
            )
            path = f"data/temp/wecom_{msg.media_id}.amr"
            with open(path, "wb") as f:
                f.write(resp.content)

            try:
                from pydub import AudioSegment

                path_wav = f"data/temp/wecom_{msg.media_id}.wav"
                audio = AudioSegment.from_file(path)
                audio.export(path_wav, format="wav")
            except Exception as e:
                logger.error(f"转换音频失败: {e}。如果没有安装 ffmpeg 请先安装。")
                path_wav = path
                return

            abm.message_str = ""
            abm.self_id = str(msg.agent)
            abm.message = [Record(file=path_wav, url=path_wav)]
            abm.type = MessageType.FRIEND_MESSAGE
            abm.sender = MessageMember(
                msg.source,
                msg.source,
            )
            abm.message_id = msg.id
            abm.timestamp = msg.time
            abm.session_id = abm.sender.user_id
            abm.raw_message = msg

        logger.info(f"abm: {abm}")
        await self.handle_msg(abm)

    async def handle_msg(self, message: AstrBotMessage):
        message_event = WcfPlatformEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            client=self.client,
        )
        self.commit_event(message_event)

    def get_client(self) -> WeChatClient:
        return self.client

    async def terminate(self):
        self.server.shutdown_event.set()
        await self.server.server.shutdown()
        logger.info("企业微信 适配器已被优雅地关闭")


## [00:12:44] [Core] [INFO] [wcf.wcf_adapter:62]: 解析成功: b'{"is_self":false,"is_group":false,"id":603516295724061438,"type":1,"ts":1743437563,"roomid":"wxid_0t7odu3w4wpw41","content":"\xe4\xbd\xa0\xe5\xa5\xbd","sender":"wxid_0t7odu3w4wpw41","sign":"85bbccf398162943895f6b64488b9a4b","thumb":"","extra":"","xml":"<msgsource>\\n    <bizflag>0</bizflag>\\n    <pua>1</pua>\\n    <eggIncluded>1</eggIncluded>\\n    <signature>N0_V1_1Y3iKWcm|v1_UHj5U0bN</signature>\\n    <tmp_node>\\n        <publisher-id />\\n    </tmp_node>\\n    <sec_msg_node>\\n        <alnode>\\n            <fr>1</fr>\\n        </alnode>\\n    </sec_msg_node>\\n</msgsource>\\n"}'
