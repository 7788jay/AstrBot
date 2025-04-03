import asyncio
import base64
import datetime
import os
import re
import threading

import aiohttp
import anyio

from astrbot.api import logger, sp
from astrbot.api.message_components import Plain, Image, At, Record, Video
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.core.utils.io import download_image_by_url


class SimpleWcfClient:
    """针对 WCF 的简单实现。
    """

    def __init__(
        self,
        base_url: str
    ):
        self.base_url = base_url
        if self.base_url.endswith("/"):
            self.base_url = self.base_url[:-1]


        logger.info(f"WCF API: {self.base_url}")


        self.token = None
        self.headers = {}

        self.userrealnames = {}

        self.shutdown_event = asyncio.Event()


    async def _convert(self, data: dict) -> AstrBotMessage:
        if "TypeName" in data:
            type_name = data["TypeName"]
        elif "type_name" in data:
            type_name = data["type_name"]
        else:
            raise Exception("无法识别的消息类型")

        # 以下没有业务处理，只是避免控制台打印太多的日志
        if type_name == "ModContacts":
            logger.info("gewechat下发：ModContacts消息通知。")
            return
        if type_name == "DelContacts":
            logger.info("gewechat下发：DelContacts消息通知。")
            return

        if type_name == "Offline":
            logger.critical("收到 gewechat 下线通知。")
            return

        d = None
        if "Data" in data:
            d = data["Data"]
        elif "data" in data:
            d = data["data"]

        if not d:
            logger.warning(f"消息不含 data 字段: {data}")
            return

        if "CreateTime" in d:
            # 得到系统 UTF+8 的 ts
            tz_offset = datetime.timedelta(hours=8)
            tz = datetime.timezone(tz_offset)
            ts = datetime.datetime.now(tz).timestamp()
            create_time = d["CreateTime"]
            if create_time < ts - 30:
                logger.warning(f"消息时间戳过旧: {create_time}，当前时间戳: {ts}")
                return

        abm = AstrBotMessage()

        from_user_name = d["FromUserName"]["string"]  # 消息来源
        d["to_wxid"] = from_user_name  # 用于发信息

        abm.message_id = str(d.get("MsgId"))
        abm.session_id = from_user_name
        abm.self_id = data["Wxid"]  # 机器人的 wxid

        user_id = ""  # 发送人 wxid
        content = d["Content"]["string"]  # 消息内容

        at_me = False
        if "@chatroom" in from_user_name:
            abm.type = MessageType.GROUP_MESSAGE
            _t = content.split(":\n")
            user_id = _t[0]
            content = _t[1]
            if "\u2005" in content:
                # at
                # content = content.split('\u2005')[1]
                content = re.sub(r"@[^\u2005]*\u2005", "", content)
            abm.group_id = from_user_name
            # at
            msg_source = d["MsgSource"]
            if (
                f"<atuserlist><![CDATA[,{abm.self_id}]]>" in msg_source
                or f"<atuserlist><![CDATA[{abm.self_id}]]>" in msg_source
            ):
                at_me = True
            if "在群聊中@了你" in d.get("PushContent", ""):
                at_me = True
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            user_id = from_user_name

        # 检查消息是否由自己发送，若是则忽略
        if user_id == abm.self_id:
            logger.info("忽略自己发送的消息")
            return None

        abm.message = []
        if at_me:
            abm.message.insert(0, At(qq=abm.self_id))

        # 解析用户真实名字
        user_real_name = "unknown"
        if abm.group_id:
            if (
                abm.group_id not in self.userrealnames
                or user_id not in self.userrealnames[abm.group_id]
            ):
                # 获取群成员列表，并且缓存
                if abm.group_id not in self.userrealnames:
                    self.userrealnames[abm.group_id] = {}
                member_list = await self.get_chatroom_member_list(abm.group_id)
                logger.debug(f"获取到 {abm.group_id} 的群成员列表。")
                if member_list and "memberList" in member_list:
                    for member in member_list["memberList"]:
                        self.userrealnames[abm.group_id][member["wxid"]] = member[
                            "nickName"
                        ]
                if user_id in self.userrealnames[abm.group_id]:
                    user_real_name = self.userrealnames[abm.group_id][user_id]
            else:
                user_real_name = self.userrealnames[abm.group_id][user_id]
        else:
            user_real_name = d.get("PushContent", "unknown : ").split(" : ")[0]

        abm.sender = MessageMember(user_id, user_real_name)
        abm.raw_message = d
        abm.message_str = ""

        if user_id == "weixin":
            # 忽略微信团队消息
            return

        # 不同消息类型
        match d["MsgType"]:
            case 1:
                # 文本消息
                abm.message.append(Plain(content))
                abm.message_str = content
            case 3:
                # 图片消息
                file_url = await self.multimedia_downloader.download_image(
                    self.appid, content
                )
                logger.debug(f"下载图片: {file_url}")
                file_path = await download_image_by_url(file_url)
                abm.message.append(Image(file=file_path, url=file_path))

            case 34:
                # 语音消息
                if "ImgBuf" in d and "buffer" in d["ImgBuf"]:
                    voice_data = base64.b64decode(d["ImgBuf"]["buffer"])
                    file_path = f"data/temp/gewe_voice_{abm.message_id}.silk"

                    async with await anyio.open_file(file_path, "wb") as f:
                        await f.write(voice_data)
                    abm.message.append(Record(file=file_path, url=file_path))

            # 以下已知消息类型，没有业务处理，只是避免控制台打印太多的日志
            case 37:  # 好友申请
                logger.info("消息类型(37)：好友申请")
            case 42:  # 名片
                logger.info("消息类型(42)：名片")
            case 43:  # 视频
                video = Video(file="", cover=content)
                abm.message.append(video)
            case 47:  # emoji
                data_parser = GeweDataParser(content, abm.group_id == "")
                emoji = data_parser.parse_emoji()
                abm.message.append(emoji)
            case 48:  # 地理位置
                logger.info("消息类型(48)：地理位置")
            case 49:  # 公众号/文件/小程序/引用/转账/红包/视频号/群聊邀请
                data_parser = GeweDataParser(content, abm.group_id == "")
                abm_data = data_parser.parse_mutil_49()
                if abm_data:
                    abm.message.append(abm_data)
            case 51:  # 帐号消息同步?
                logger.info("消息类型(51)：帐号消息同步？")
            case 10000:  # 被踢出群聊/更换群主/修改群名称
                logger.info("消息类型(10000)：被踢出群聊/更换群主/修改群名称")
            case 10002:  # 撤回/拍一拍/成员邀请/被移出群聊/解散群聊/群公告/群待办
                logger.info(
                    "消息类型(10002)：撤回/拍一拍/成员邀请/被移出群聊/解散群聊/群公告/群待办"
                )

            case _:
                logger.info(f"未实现的消息类型: {d['MsgType']}")
                abm.raw_message = d

        logger.debug(f"abm: {abm}")
        return abm

    async def post_text(self, to_wxid, content: str, ats: str = ""):
        """发送文本消息到群聊
        
        Args:
            aters: 要@的用户wxid
            msg: 消息内容,支持\n换行
            receiver: 群聊id
        """

        data = {
            "aters": ats,
            "msg": content,
            "receiver": to_wxid
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/text", headers=self.headers, json=data
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"发送消息结果: {json_blob}")

    async def post_image(self, to_wxid, image_url: str):
        """发送图片消息"""
        payload = {
            "appId": self.appid,
            "toWxid": to_wxid,
            "imgUrl": image_url,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/postImage", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"发送图片结果: {json_blob}")

    async def post_emoji(self, to_wxid, emoji_md5, emoji_size, cdnurl=""):
        """发送emoji消息"""
        payload = {
            "appId": self.appid,
            "toWxid": to_wxid,
            "emojiMd5": emoji_md5,
            "emojiSize": emoji_size,
        }

        # 优先表情包，若拿不到表情包的md5，就用当作图片发
        try:
            if emoji_md5 != "" and emoji_size != "":
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.base_url}/message/postEmoji",
                        headers=self.headers,
                        json=payload,
                    ) as resp:
                        json_blob = await resp.json()
                        logger.info(
                            f"发送emoji消息结果: {json_blob.get('msg', '操作失败')}"
                        )
            else:
                await self.post_image(to_wxid, cdnurl)

        except Exception as e:
            logger.error(e)

    async def post_video(
        self, to_wxid, video_url: str, thumb_url: str, video_duration: int
    ):
        payload = {
            "appId": self.appid,
            "toWxid": to_wxid,
            "videoUrl": video_url,
            "thumbUrl": thumb_url,
            "videoDuration": video_duration,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/postVideo", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"发送视频结果: {json_blob}")

    async def forward_video(self, to_wxid, cnd_xml: str):
        """转发视频

        Args:
            to_wxid (str): 发送给谁
            cnd_xml (str): 视频消息的cdn信息
        """
        payload = {
            "appId": self.appid,
            "toWxid": to_wxid,
            "xml": cnd_xml,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/forwardVideo",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"转发视频结果: {json_blob}")

    async def post_voice(self, to_wxid, voice_url: str, voice_duration: int):
        """发送语音信息

        Args:
            voice_url (str): 语音文件的网络链接
            voice_duration (int): 语音时长，毫秒
        """
        payload = {
            "appId": self.appid,
            "toWxid": to_wxid,
            "voiceUrl": voice_url,
            "voiceDuration": voice_duration,
        }

        logger.debug(f"发送语音: {payload}")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/postVoice", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.info(f"发送语音结果: {json_blob.get('msg', '操作失败')}")

    async def post_file(self, to_wxid, file_url: str, file_name: str):
        """发送文件

        Args:
            to_wxid (string): 微信ID
            file_url (str): 文件的网络链接
            file_name (str): 文件名
        """
        payload = {
            "appId": self.appid,
            "toWxid": to_wxid,
            "fileUrl": file_url,
            "fileName": file_name,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/postFile", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"发送文件结果: {json_blob}")

    async def add_friend(self, v3: str, v4: str, content: str):
        """申请添加好友"""
        payload = {
            "appId": self.appid,
            "scene": 3,
            "content": content,
            "v4": v4,
            "v3": v3,
            "option": 2,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/contacts/addContacts",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"申请添加好友结果: {json_blob}")
                return json_blob

    async def get_group(self, group_id: str):
        payload = {
            "appId": self.appid,
            "chatroomId": group_id,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/group/getChatroomInfo",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def get_group_member(self, group_id: str):
        payload = {
            "appId": self.appid,
            "chatroomId": group_id,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/group/getChatroomMemberList",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def accept_group_invite(self, url: str):
        """同意进群"""
        payload = {"appId": self.appid, "url": url}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/group/agreeJoinRoom",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def add_group_member_to_friend(
        self, group_id: str, to_wxid: str, content: str
    ):
        payload = {
            "appId": self.appid,
            "chatroomId": group_id,
            "content": content,
            "memberWxid": to_wxid,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/group/addGroupMemberAsFriend",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def get_user_or_group_info(self, *ids):
        """
        获取用户或群组信息。

        :param ids: 可变数量的 wxid 参数
        """

        wxids_str = list(ids)

        payload = {
            "appId": self.appid,
            "wxids": wxids_str,  # 使用逗号分隔的字符串
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/contacts/getDetailInfo",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def get_contacts_list(self):
        """
        获取通讯录列表
        见 https://apifox.com/apidoc/shared/69ba62ca-cb7d-437e-85e4-6f3d3df271b1/api-196794504
        """
        payload = {"appId": self.appid}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/contacts/fetchContactsList",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取通讯录列表结果: {json_blob}")
                return json_blob
