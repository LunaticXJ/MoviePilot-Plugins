import json
import threading
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils

lock = threading.Lock()

class EmbyLatestMediaSort(_PluginBase):
    # 插件名称
    plugin_name = "Emby最新媒体排序"
    # 插件描述
    plugin_desc = "将Emby媒体加入时间初始化为发布时间，让首页最新媒体列表按发布日期倒序排列。"
    # 插件图标
    plugin_icon = "Element_A.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "LunaticXJ"
    # 作者主页
    author_url = "https://github.com/LunaticXJ"
    # 插件配置项ID前缀
    plugin_config_prefix = "embylatestmediasort_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _mediaservers = None
    _media_types = None
    _batch_size = 1000  # 每批次查询数量
    _default_premiere_date = "2000-01-01T00:00:00.0000000Z"  # 默认PremiereDate
    _all_media_types = ["Movie", "Episode"]  # 支持的所有媒体类型

    mediaserver_helper = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._mediaservers = config.get("mediaservers") or []
            self._media_types = config.get("media_types") or ["All"]

            # 加载模块
            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"Emby媒体排序刷新服务启动，立即运行一次")
                    self._scheduler.add_job(self.collection_sort, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="Emby媒体排序")

                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()
                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.collection_sort,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="Emby媒体排序")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{str(err)}")
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        self.update_config(
            {
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "enabled": self._enabled,
                "mediaservers": self._mediaservers,
                "media_types": self._media_types,
            }
        )

    def collection_sort(self):
        """
        将指定类型媒体的DateCreated字段设置为PremiereDate字段值，缺失PremiereDate的媒体使用默认日期
        """
        emby_servers = self.mediaserver_helper.get_services(name_filters=self._mediaservers, type_filter="emby")
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        if not self._media_types:
            logger.error("未配置要处理的媒体类型")
            return

        # 如果选择了"All"，处理所有支持的媒体类型
        types_to_process = self._all_media_types if "All" in self._media_types else [t for t in self._media_types if t != "All"]

        if not types_to_process:
            logger.error("没有有效的媒体类型需要处理")
            return

        for emby_name, emby_server in emby_servers.items():
            logger.info(f"开始处理媒体服务器 {emby_name}")

            for media_type in types_to_process:
                logger.info(f"开始处理媒体类型: {media_type}")
                start_index = 0
                total_items = None

                while total_items is None or start_index < total_items:
                    # 分批查询
                    items = self.__get_items(emby_server=emby_server, media_type=media_type, start_index=start_index, limit=self._batch_size)
                    if not items:
                        logger.info(f"未获取到{media_type}信息，start_index={start_index}")
                        break

                    if total_items is None:
                        total_items = self.__get_total_items(emby_server=emby_server, media_type=media_type)
                        logger.info(f"总计需要处理 {total_items} 条{media_type}信息")

                    item_dict = []
                    for item in items:
                        item_info = self.__get_item_info(emby_server=emby_server, item_id=item.get("Id"))
                        item_dict.append({"Name": item.get("Name"), "Id": item.get("Id"), "item_info": item_info})

                    if not item_dict:
                        logger.info(f"当前{media_type}批次（start_index={start_index}）无有效信息，跳过")
                        start_index += self._batch_size
                        continue

                    # 处理当前批次
                    updated_items = []
                    for item in item_dict:
                        with lock:
                            premiere_date = item["item_info"].get("PremiereDate", self._default_premiere_date)
                            if premiere_date == item["item_info"].get("DateCreated"):
                                logger.debug(
                                    f"媒体: {item.get('Name')} ({media_type}) 原入库时间与发布日期相同 {premiere_date}，跳过")
                                continue

                            item["item_info"]["DateCreated"] = premiere_date
                            updated_items.append(item["item_info"])
                            if premiere_date == self._default_premiere_date:
                                logger.info(f"媒体: {item.get('Name')} ({media_type}) 缺失PremiereDate，使用默认日期 {premiere_date}")

                    if not updated_items:
                        logger.info(f"当前{media_type}批次（start_index={start_index}）无需更新入库时间")
                        start_index += self._batch_size
                        continue

                    # 更新入库时间
                    for item_info in updated_items:
                        update_flag = self.__update_item_info(emby_server=emby_server, item_id=item_info.get("Id"), data=item_info)
                        if update_flag:
                            logger.info(f"{item_info.get('Name')} ({media_type}) 更新入库时间到{item_info.get('DateCreated')}成功")
                        else:
                            logger.error(f"{item_info.get('Name')} ({media_type}) 更新入库时间到{item_info.get('DateCreated')}失败")

                    logger.info(f"{media_type}批次处理完成（start_index={start_index}，数量={len(items)}）")
                    start_index += self._batch_size

                logger.info(f"更新 {emby_name} {media_type} 排序完成，总计处理 {start_index} 条记录")

            logger.info(f"更新 {emby_name} 所有媒体类型排序完成")


    def __get_items(self, emby_server, media_type: str, start_index: int = 0, limit: int = 1000):
        """
        获取指定类型的媒体项
        """
        emby_config = emby_server.get_config()
        host = emby_config.get("host")
        api_key = emby_config.get("api_key")
        user_id = emby_server.get_user().get("id")
        if not host or not api_key or not user_id:
            logger.error(f"Emby服务器 {emby_config.get('name')} 配置不完整")
            return []

        res = RequestUtils().get_res(
            f"{host}/emby/Users/{user_id}/Items?Recursive=true&IncludeItemTypes={media_type}&StartIndex={start_index}&Limit={limit}&api_key={api_key}")
        if res and res.status_code == 200:
            results = res.json().get("Items") or []
            return results
        return []

    def __get_total_items(self, emby_server, media_type: str):
        """
        获取指定类型媒体的总记录数
        """
        emby_config = emby_server.get_config()
        host = emby_config.get("host")
        api_key = emby_config.get("api_key")
        user_id = emby_server.get_user().get("id")
        if not host or not api_key or not user_id:
            logger.error(f"Emby服务器 {emby_config.get('name')} 配置不完整")
            return 0

        res = RequestUtils().get_res(
            f"{host}/emby/Users/{user_id}/Items?Recursive=true&IncludeItemTypes={media_type}&Limit=1&api_key={api_key}")
        if res and res.status_code == 200:
            return res.json().get("TotalRecordCount", 0)
        return 0

    def __get_item_info(self, emby_server, item_id):
        """
        获取单个媒体项的详细信息
        """
        emby_config = emby_server.get_config()
        host = emby_config.get("host")
        api_key = emby_config.get("api_key")
        user_id = emby_server.get_user().get("id")
        if not host or not api_key or not user_id:
            logger.error(f"Emby服务器 {emby_config.get('name')} 配置不完整")
            return {}

        res = RequestUtils().get_res(
            f"{host}/emby/Users/{user_id}/Items/{item_id}?api_key={api_key}")
        if res and res.status_code == 200:
            return res.json()
        return {}

    def __update_item_info(self, emby_server, item_id, data):
        """
        更新媒体项信息
        """
        emby_config = emby_server.get_config()
        host = emby_config.get("host")
        api_key = emby_config.get("api_key")
        if not host or not api_key:
            logger.error(f"Emby服务器 {emby_config.get('name')} 配置不完整")
            return False

        headers = {
            'accept': '*/*',
            'Content-Type': 'application/json'
        }
        res = RequestUtils(headers=headers).post(
            f"{host}/emby/Items/{item_id}?api_key={api_key}",
            data=json.dumps(data))
        if res and res.status_code == 204:
            return True
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/collection_sort",
            "event": EventType.PluginAction,
            "desc": "更新Emby媒体排序",
            "category": "",
            "data": {
                "action": "collection_sort"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': '媒体服务器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.mediaserver_helper.get_configs().values() if
                                                      config.type == "emby"]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'media_types',
                                            'label': '媒体类型',
                                            'items': [
                                                {'title': '全部', 'value': 'All'},
                                                {'title': '电影', 'value': 'Movie'},
                                                {'title': '剧集', 'value': 'Episode'},
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '通过Emby API分批获取用户下指定类型的媒体信息（如电影或剧集，全部时处理所有类型），将DateCreated字段设置为PremiereDate字段值，缺失PremiereDate的媒体使用默认日期2000-01-01。注：只支持Emby。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "mediaservers": [],
            "media_types": ["All"],
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))