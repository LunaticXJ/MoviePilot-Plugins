import csv
import json
import io
import urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.responses import StreamingResponse, Response

from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyMissingEpisodes(_PluginBase):
    """
    Emby 剧集缺集检查插件：精简版，聚合输出剧集缺失季集情况，支持导出 CSV
    """

    # 插件名称
    plugin_name = "Emby剧集缺集检查"
    # 插件描述
    plugin_desc = "精准查找 Emby 库中剧集的缺失集情况，聚合显示并支持导出 CSV。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/emby.png"
    # 插件版本
    plugin_version = "2.4.0"
    # 插件作者
    plugin_author = "LunaticXJ"
    # 作者主页
    author_url = "https://github.com/LunaticXJ"
    # 插件配置项ID前缀
    plugin_config_prefix = "embymissingepisodes_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1

    # 插件私有属性
    _enabled = False
    _onlyonce = False
    _mediaserver = ""
    _ignore_season_zero = True
    _ignore_future = True

    # 持久化存储 Key 定义
    _STORAGE_DATA_KEY = "missing_episodes_data"
    _STORAGE_TIME_KEY = "missing_episodes_last_time"

    # 内存缓存查询结果与状态
    _cache_missing_results: List[Dict[str, Any]] = []
    _last_scan_time: str = "从未扫描"
    _is_scanning: bool = False

    mediaserver_helper = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置与持久化加载
        """
        self.stop_service()
        self.mediaserver_helper = MediaServerHelper()

        # 读取持久化缓存
        self._load_saved_data()

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._mediaserver = config.get("mediaserver") or ""
            self._ignore_season_zero = config.get("ignore_season_zero", True)
            self._ignore_future = config.get("ignore_future", True)

            if self._enabled and self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                logger.info("【EmbyMissingEpisodes】触发“立即运行一次”，将在 3 秒后执行缺集扫描...")
                self._scheduler.add_job(
                    self.scan_missing_episodes,
                    'date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="Emby缺集扫描"
                )

                self._onlyonce = False
                self.__update_config()

                if self._scheduler.get_jobs():
                    self._scheduler.start()

    def _load_saved_data(self):
        """
        显式从存储读取数据到内存
        """
        try:
            saved_data = self.get_data(self._STORAGE_DATA_KEY)
            saved_time = self.get_data(self._STORAGE_TIME_KEY)
            if saved_data and isinstance(saved_data, list):
                self._cache_missing_results = saved_data
            if saved_time:
                self._last_scan_time = str(saved_time)
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】读取持久化数据失败: {e}")

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        """
        更新插件持久化配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "mediaserver": self._mediaserver,
            "ignore_season_zero": self._ignore_season_zero,
            "ignore_future": self._ignore_future,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/export",
                "endpoint": self.api_export_csv,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "导出 CSV",
                "description": "导出缺失剧集结果为 CSV 文件",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件系统设置配置页面
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "ignore_season_zero",
                                            "label": "忽略特别篇 (S0/SP)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "ignore_future",
                                            "label": "忽略未上映剧集",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": False,
                                            "clearable": True,
                                            "model": "mediaserver",
                                            "label": "选择 Emby 媒体服务器",
                                            "items": [
                                                {"title": config.name, "value": config.name}
                                                for config in self.mediaserver_helper.get_configs().values()
                                                if config.type == "emby"
                                            ],
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "ignore_season_zero": True,
            "ignore_future": True,
            "mediaserver": "",
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件数据主页面（UI）
        """
        self._load_saved_data()

        status_text = "后台扫描进行中..." if self._is_scanning else f"上次更新时间：{self._last_scan_time}"

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "outlined", "class": "mb-4"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "d-flex align-center justify-space-between flex-wrap gap-4"},
                                        "content": [
                                            {
                                                "component": "div",
                                                "content": [
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-h6 font-weight-bold"},
                                                        "text": "Emby 剧集缺集检查看板",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-caption text-medium-emphasis mt-1"},
                                                        "text": f"{status_text} | 缺失项：{len(self._cache_missing_results)} 条",
                                                    },
                                                ],
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "d-flex gap-2"},
                                                "content": [
                                                    {
                                                        "component": "VBtn",
                                                        "props": {
                                                            "color": "success",
                                                            "prepend-icon": "mdi-download",
                                                            "variant": "tonal",
                                                            "href": "/api/v1/plugin/EmbyMissingEpisodes/export",
                                                            "target": "_blank",
                                                        },
                                                        "text": "导出 CSV",
                                                    },
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VDataTable",
                                "props": {
                                    "headers": [
                                        {"title": "剧集名称", "key": "SeriesName", "width": "300px"},
                                        {"title": "缺失季度", "key": "SeasonFormatted", "width": "120px"},
                                        {"title": "缺失集号", "key": "MissingEpisodes"},
                                    ],
                                    "items": self._cache_missing_results,
                                    "hover": True,
                                    "density": "comfortable",
                                    "items-per-page": 15,
                                    "items-per-page-text": "每页显示条数：",  # 👈 核心修改：将 'Items per page:' 修改为中文
                                    "page-text": "{0}-{1} 共 {2} 条",  # 可选：汉化底部的范围文本 '1-15 of 98'
                                    "no-data-text": "暂无缺失剧集数据。请在设置中选择 Emby 服务器，勾选【立即运行一次】并保存。",
                                },
                            }
                        ],
                    },
                ],
            }
        ]

    def api_export_csv(self) -> Any:
        """
        API 端点：导出 CSV 文件
        """
        self._load_saved_data()

        output = io.StringIO()
        output.write('\ufeff')  # UTF-8 BOM 标识
        writer = csv.writer(output)

        writer.writerow(["剧集名称", "缺失季度", "缺失集号"])

        for row in self._cache_missing_results:
            writer.writerow([
                row.get("SeriesName", ""),
                row.get("SeasonFormatted", ""),
                row.get("MissingEpisodes", "")
            ])

        csv_bytes = output.getvalue().encode('utf-8-sig')
        raw_filename = f"Emby_缺集清单_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        encoded_filename = urllib.parse.quote(raw_filename)

        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=\"{encoded_filename}\"; filename*=UTF-8''{encoded_filename}",
                "Access-Control-Expose-Headers": "Content-Disposition",
            }
        )

    def scan_missing_episodes(self):
        """
        异步后台执行扫描的任务函数
        """
        if self._is_scanning:
            logger.warn("【EmbyMissingEpisodes】上一次扫描任务尚未完成，跳过本次执行。")
            return

        self._is_scanning = True
        start_time = datetime.now()
        logger.info("【EmbyMissingEpisodes】开始缺集扫描...")

        try:
            if not self._mediaserver:
                logger.error("【EmbyMissingEpisodes】未选择 Emby 媒体服务器！")
                return

            emby_servers = self.mediaserver_helper.get_services(name_filters=[self._mediaserver], type_filter="emby")
            if not emby_servers:
                logger.error(f"【EmbyMissingEpisodes】未找到匹配的 Emby 服务器: {self._mediaserver}")
                return

            emby_server = list(emby_servers.values())[0]
            missing_list = self._scan_server_by_diff(self._mediaserver, emby_server)

            # 覆盖上一次历史数据
            self._cache_missing_results = missing_list
            self._last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 持久化存储
            self.save_data(self._STORAGE_DATA_KEY, self._cache_missing_results)
            self.save_data(self._STORAGE_TIME_KEY, self._last_scan_time)

            elapsed_seconds = (datetime.now() - start_time).total_seconds()
            logger.info(
                f"【EmbyMissingEpisodes】>>> 扫描完成！耗时 {elapsed_seconds:.2f} 秒，"
                f"找到 {len(missing_list)} 季存在缺集，已覆盖存入本地持久化存储。"
            )
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】扫描过程出现异常: {e}", exc_info=True)
        finally:
            self._is_scanning = False

    def _scan_server_by_diff(self, server_name: str, emby_server) -> List[Dict[str, Any]]:
        """
        高效差集比对逻辑
        """
        host = emby_server.config.config.get("host")
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        if not host or not api_key or not user_id:
            return []

        today_date_str = datetime.now().strftime("%Y-%m-%d")

        # 1. 获取 Season 列表
        seasons_url = (
            f"{host}/emby/Users/{user_id}/Items?"
            f"Recursive=true&IncludeItemTypes=Season"
            f"&Fields=SeriesName,SeriesId,IndexNumber,ChildCount"
            f"&api_key={api_key}"
        )
        res_seasons = RequestUtils().get_res(seasons_url)
        if not res_seasons or res_seasons.status_code != 200:
            logger.error(f"【EmbyMissingEpisodes】[{server_name}] 拉取 Season 列表失败！")
            return []

        raw_seasons = res_seasons.json().get("Items") or []

        # 2. 获取 Episode 列表
        episodes_url = (
            f"{host}/emby/Users/{user_id}/Items?"
            f"Recursive=true&IncludeItemTypes=Episode"
            f"&Fields=IndexNumber,ParentIndexNumber,LocationType,ParentId,PremiereDate"
            f"&api_key={api_key}"
        )
        res_episodes = RequestUtils().get_res(episodes_url)
        if not res_episodes or res_episodes.status_code != 200:
            logger.error(f"【EmbyMissingEpisodes】[{server_name}] 拉取 Episode 列表失败！")
            return []

        raw_episodes = res_episodes.json().get("Items") or []

        # 3. 内存字典索引
        season_real_eps = defaultdict(dict)
        season_all_meta_eps = defaultdict(dict)

        for ep in raw_episodes:
            season_id = ep.get("ParentId")
            ep_num = ep.get("IndexNumber")
            premiere_date = ep.get("PremiereDate", "")

            if not season_id or ep_num is None:
                continue

            season_all_meta_eps[season_id][ep_num] = premiere_date

            if ep.get("LocationType") != "Virtual":
                season_real_eps[season_id][ep_num] = premiere_date

        missing_results = []

        # 4. 遍历 Seasons 聚合缺集
        for season_item in raw_seasons:
            season_id = season_item.get("Id")
            series_name = season_item.get("SeriesName") or "未知剧集"
            season_num = int(season_item.get("IndexNumber") if season_item.get("IndexNumber") is not None else 1)
            target_child_count = int(season_item.get("ChildCount") if season_item.get("ChildCount") is not None else 0)

            # 忽略特别篇
            if self._ignore_season_zero and season_num == 0:
                continue

            real_eps_dict = season_real_eps.get(season_id, {})
            meta_eps_dict = season_all_meta_eps.get(season_id, {})

            max_local_ep = max(real_eps_dict.keys()) if real_eps_dict else 0
            max_meta_ep = max(meta_eps_dict.keys()) if meta_eps_dict else 0

            total_target = max(max_local_ep, target_child_count, max_meta_ep)

            if total_target == 0:
                continue

            missing_ep_numbers = []

            for i in range(1, total_target + 1):
                if i not in real_eps_dict:
                    ep_premiere_date = meta_eps_dict.get(i, "")
                    formatted_date = ep_premiere_date[:10] if len(ep_premiere_date) >= 10 else "未知/未开播"

                    # 忽略未上映剧集
                    if self._ignore_future and formatted_date != "未知/未开播" and formatted_date > today_date_str:
                        continue

                    missing_ep_numbers.append(str(i))

            if missing_ep_numbers:
                season_display = f"S{season_num}" if season_num > 0 else "SP"
                missing_results.append({
                    "SeriesName": series_name,
                    "SeasonFormatted": season_display,
                    "MissingEpisodes": "、".join(missing_ep_numbers),
                })

        return missing_results

    def stop_service(self):
        """
        停止任务清理
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】停止服务失败: {e}")
