import json
import threading
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyLatestMediaSort(_PluginBase):
    # 插件名称
    plugin_name = "Emby最新媒体排序"
    # 插件描述
    plugin_desc = "将Emby媒体加入时间设置为发布时间，让首页最新媒体列表按发布日期倒序排列。"
    # 插件图标
    plugin_icon = "Element_A.png"
    # 插件版本
    plugin_version = "1.3.0"
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
    _mediaservers = None
    _media_types = None
    _thread_num = 10  # 默认并发线程数
    _batch_size = 1000  # 每批次查询数量
    _default_premiere_date = "2000-01-01T00:00:00.0000000Z"  # 默认PremiereDate

    mediaserver_helper = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._onlyonce = config.get("onlyonce")
            self._mediaservers = config.get("mediaservers") or []
            self._media_types = config.get("media_types") or []
            # 获取配置的并发线程数，默认为 10
            self._thread_num = int(config.get("thread_num") or 10)

            # 加载模块
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                logger.info(f"Emby媒体排序刷新服务启动，立即运行一次")
                self._scheduler.add_job(self.collection_sort, 'date',
                                        run_date=datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="Emby媒体排序")

                # 关闭一次性开关
                self._onlyonce = False

                # 保存配置
                self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return False

    def __update_config(self):
        self.update_config(
            {
                "onlyonce": self._onlyonce,
                "mediaservers": self._mediaservers,
                "media_types": self._media_types,
                "thread_num": self._thread_num,
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

        for emby_name, emby_server in emby_servers.items():
            logger.info(f"开始处理媒体服务器 {emby_name}，当前并发线程数：{self._thread_num}")

            for media_type in self._media_types:
                logger.info(f"开始处理媒体类型: {media_type}")
                start_index = 0
                total_count = None
                total_success_count = 0

                while total_count is None or start_index < total_count:
                    # 1. 批量快速查询列表（带入必要字段，显式 DateCreated 倒序排列）
                    items = self.__get_items(emby_server=emby_server, media_type=media_type, start_index=start_index, limit=self._batch_size)
                    if not items:
                        logger.info(f"未获取到{media_type}信息，start_index={start_index}")
                        break

                    if total_count is None:
                        total_count = self.__get_total_items(emby_server=emby_server, media_type=media_type)
                        logger.info(f"总计需要处理 {total_count} 条{media_type}信息")

                    # 2. 筛选真正需要更新的媒体项
                    items_to_process = []
                    skipped_count = 0

                    for item in items:
                        premiere_date = item.get("PremiereDate") or self._default_premiere_date
                        date_created = item.get("DateCreated")

                        if premiere_date == date_created:
                            skipped_count += 1
                            continue

                        # 保存基本标识与需设置的目标日期
                        items_to_process.append({
                            "Id": item.get("Id"),
                            "Name": item.get("Name"),
                            "TargetPremiereDate": premiere_date
                        })

                        if premiere_date == self._default_premiere_date:
                            logger.info(f"{item.get('Name')} ({media_type}) 缺失PremiereDate，使用默认日期 {premiere_date}")

                    # 🚀 提前终止判断（哨兵校验机制）：
                    # 如果当前批次中的所有数据都已经对齐，说明后续更早入库的历史数据也已全部对齐，安全终止循环。
                    if skipped_count == len(items):
                        logger.info(f"当前批次（start_index={start_index}）数据已全部是对齐状态，触发展开安全终止，跳过后续历史批次！")
                        break

                    if skipped_count > 0:
                        logger.info(f"当前批次（start_index={start_index}）跳过 {skipped_count} 条时间相同的记录")

                    if not items_to_process:
                        start_index += self._batch_size
                        continue

                    # 3. 多线程并发：拉取完整元数据 -> 赋予新时间 -> 推送更新
                    batch_success = 0
                    with ThreadPoolExecutor(max_workers=self._thread_num) as executor:
                        future_to_item = {
                            executor.submit(self.__process_single_item_update, emby_server, item_info["Id"], item_info["TargetPremiereDate"]): item_info
                            for item_info in items_to_process
                        }

                        for future in as_completed(future_to_item):
                            item_info = future_to_item[future]
                            try:
                                if future.result():
                                    logger.info(f"{item_info.get('Name')} ({media_type}) 更新入库时间到 {item_info.get('TargetPremiereDate')} 成功")
                                    batch_success += 1
                                else:
                                    logger.error(f"{item_info.get('Name')} ({media_type}) 更新入库时间到 {item_info.get('TargetPremiereDate')} 失败")
                            except Exception as e:
                                logger.error(f"{item_info.get('Name')} ({media_type}) 处理更新发生异常：{str(e)}")

                    total_success_count += batch_success
                    logger.info(f"{media_type}批次处理完成（start_index={start_index}，本次成功更新={batch_success}/{len(items_to_process)}）")
                    start_index += self._batch_size

                logger.info(f"更新 {emby_name} {media_type} 排序完成，总计成功更新 {total_success_count} 条记录")

    def __process_single_item_update(self, emby_server, item_id: str, target_date: str) -> bool:
        """
        线程内部调用的完整更新流程：抓取完整 Item 字典 -> 修改 DateCreated -> POST 保存
        """
        # 1. 抓取正规完整版本的 Item 信息
        full_item_info = self.__get_item_info(emby_server, item_id)
        if not full_item_info:
            return False

        # 2. 赋予新时间
        full_item_info["DateCreated"] = target_date

        # 3. 推送更新
        return self.__update_item_info(emby_server, item_id, full_item_info)

    def __get_items(self, emby_server, media_type: str, start_index: int = 0, limit: int = 1000):
        """
        获取指定类型的媒体项（显式指定按 DateCreated 倒序排列，带出 PremiereDate, DateCreated 字段）
        """
        host = emby_server.config.config.get("host")
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        url = (f"{host}/emby/Users/{user_id}/Items?"
               f"Recursive=true&IncludeItemTypes={media_type}"
               f"&SortBy=DateCreated&SortOrder=Descending"
               f"&Fields=PremiereDate,DateCreated"
               f"&StartIndex={start_index}&Limit={limit}&api_key={api_key}")

        res = RequestUtils().get_res(url)
        if res and res.status_code == 200:
            return res.json().get("Items") or []
        return []

    def __get_total_items(self, emby_server, media_type: str):
        """
        获取指定类型媒体的总记录数
        """
        host = emby_server.config.config.get("host")
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        res = RequestUtils().get_res(
            f"{host}/emby/Users/{user_id}/Items?Recursive=true&IncludeItemTypes={media_type}&Limit=1&api_key={api_key}")
        if res and res.status_code == 200:
            return res.json().get("TotalRecordCount", 0)
        return 0

    def __get_item_info(self, emby_server, item_id):
        """
        获取单个媒体项的完整详细信息
        """
        host = emby_server.config.config.get("host")
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        res = RequestUtils().get_res(
            f"{host}/emby/Users/{user_id}/Items/{item_id}?api_key={api_key}")
        if res and res.status_code == 200:
            return res.json()
        return {}

    def __update_item_info(self, emby_server, item_id, data):
        """
        更新媒体项信息
        """
        host = emby_server.config.config.get("host")
        api_key = emby_server.config.config.get("apikey")

        headers = {
            'accept': '*/*',
            'Content-Type': 'application/json'
        }
        res = RequestUtils(headers=headers).post(
            f"{host}/emby/Items/{item_id}?api_key={api_key}",
            data=json.dumps(data))
        if res and res.status_code in (200, 204):
            return True
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/embylatestmediasort",
            "event": EventType.PluginAction,
            "desc": "更新Emby最新媒体排序",
            "category": "",
            "data": {
                "action": "embylatestmediasort"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
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
                                    'md': 3
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
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'thread_num',
                                            'label': '并发更新线程数',
                                            'placeholder': '默认10',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
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
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
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
                                                {'title': '电影', 'value': 'Movie'},
                                                {'title': '剧集', 'value': 'Episode'},
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ], {
            "onlyonce": False,
            "thread_num": 10,
            "mediaservers": [],
            "media_types": [],
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
