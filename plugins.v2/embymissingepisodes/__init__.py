import csv
import json
import io
import urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyMissingEpisodes(_PluginBase):
    """
    Emby 剧集缺集检查插件：极简版，基于 SeriesId 维度精准比对，彻底解决海贼王/火影忍者等长篇漫误报
    """

    # 插件名称
    plugin_name = "Emby剧集缺集检查"
    # 插件描述
    plugin_desc = "精准查找 Emby 库中剧集的缺失集情况，聚合显示并支持导出 CSV。"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "4.0.0"
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
    _onlyonce = False
    _mediaserver = ""
    _ignore_season_zero = True
    _ignore_future = True

    # 持久化存储 Key
    _STORAGE_DATA_KEY = "missing_episodes_data"
    _STORAGE_TIME_KEY = "missing_episodes_last_time"

    # 内存缓存
    _cache_missing_results: List[Dict[str, Any]] = []
    _last_scan_time: str = "从未扫描"
    _is_scanning: bool = False

    mediaserver_helper = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置与历史数据加载
        """
        self.stop_service()
        self.mediaserver_helper = MediaServerHelper()

        self._load_saved_data()

        if config:
            self._onlyonce = config.get("onlyonce", False)
            self._mediaserver = config.get("mediaserver") or ""
            self._ignore_season_zero = config.get("ignore_season_zero", True)
            self._ignore_future = config.get("ignore_future", True)

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                logger.info("【EmbyMissingEpisodes】检查到“立即运行一次”，将在 3 秒后执行缺集扫描...")
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
        读取本地持久化数据
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
        return True

    def __update_config(self):
        """
        更新插件持久化配置
        """
        self.update_config({
            "onlyonce": self._onlyonce,
            "mediaserver": self._mediaserver,
            "ignore_season_zero": self._ignore_season_zero,
            "ignore_future": self._ignore_future,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        极简配置页面
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
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
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
            "onlyonce": False,
            "ignore_season_zero": True,
            "ignore_future": True,
            "mediaserver": "",
        }

    def _generate_csv_data_url(self, table_items: List[Dict[str, Any]]) -> str:
        """
        纯前端 Data URI 生成
        """
        csv_lines = ["\ufeff剧集名称,缺失季度,缺失集号"]
        for row in table_items:
            series = f'"{row.get("SeriesName", "")}"'
            season = f'"{row.get("SeasonFormatted", "")}"'
            episodes = f'"{row.get("MissingEpisodes", "")}"'
            csv_lines.append(f"{series},{season},{episodes}")
        
        csv_content = "\n".join(csv_lines)
        encoded_csv = urllib.parse.quote(csv_content)
        return f"data:text/csv;charset=utf-8-sig,{encoded_csv}"

    def get_page(self) -> List[dict]:
        """
        拼装插件数据主页面
        """
        self._load_saved_data()

        status_text = "后台扫描进行中..." if self._is_scanning else f"上次更新时间：{self._last_scan_time}"

        table_items = []
        for idx, item in enumerate(self._cache_missing_results, start=1):
            table_items.append({
                "id": str(idx),
                "SeriesName": str(item.get("SeriesName", "")),
                "SeasonFormatted": str(item.get("SeasonFormatted", "")),
                "MissingEpisodes": str(item.get("MissingEpisodes", "")),
            })

        csv_data_url = self._generate_csv_data_url(table_items)
        csv_filename = f"Emby缺集清单_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

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
                                                        "text": f"{status_text} | 缺失项：{len(table_items)} 条",
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
                                                            "color": "info",
                                                            "prepend-icon": "mdi-refresh",
                                                            "variant": "tonal",
                                                        },
                                                        "text": "刷新页面",
                                                        "events": {
                                                            "click": {
                                                                "action": "refresh",
                                                            }
                                                        },
                                                    },
                                                    {
                                                        "component": "VBtn",
                                                        "props": {
                                                            "color": "success",
                                                            "prepend-icon": "mdi-download",
                                                            "variant": "tonal",
                                                            "href": csv_data_url,
                                                            "download": csv_filename,
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
                                    "item-value": "id",
                                    "headers": [
                                        {"title": "剧集名称", "key": "SeriesName", "value": "SeriesName", "align": "start"},
                                        {"title": "缺失季度", "key": "SeasonFormatted", "value": "SeasonFormatted", "align": "start", "width": "120px"},
                                        {"title": "缺失集号", "key": "MissingEpisodes", "value": "MissingEpisodes", "align": "start"},
                                    ],
                                    "items": table_items,
                                    "hover": True,
                                    "density": "comfortable",
                                    "items-per-page": 15,
                                    "items-per-page-text": "每页显示条数：",
                                    "page-text": "{0}-{1} 共 {2} 条",
                                    "no-data-text": "暂无缺失剧集数据。请在设置中选择 Emby 服务器，勾选【立即运行一次】并保存。",
                                },
                            }
                        ],
                    },
                ],
            }
        ]

    def scan_missing_episodes(self):
        """
        后台扫描任务
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

            self._cache_missing_results = missing_list
            self._last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 数据持久化
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
        基于 SeriesId 维度的绝对聚合差集算法：完美解决长篇连载动画误报
        """
        host = emby_server.config.config.get("host")
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        if not host or not api_key or not user_id:
            return []

        today_date_str = datetime.now().strftime("%Y-%m-%d")

        # 1. 批量拉取 Season 列表
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

        # 2. 全量拉取 Episode 列表（包含 SeriesId 与 ParentIndexNumber 季号）
        episodes_url = (
            f"{host}/emby/Users/{user_id}/Items?"
            f"Recursive=true&IncludeItemTypes=Episode"
            f"&Limit=20000"
            f"&Fields=SeriesId,IndexNumber,ParentIndexNumber,LocationType,PremiereDate"
            f"&api_key={api_key}"
        )
        res_episodes = RequestUtils().get_res(episodes_url)
        if not res_episodes or res_episodes.status_code != 200:
            logger.error(f"【EmbyMissingEpisodes】[{server_name}] 拉取 Episode 列表失败！")
            return []

        raw_episodes = res_episodes.json().get("Items") or []

        # 3. 构造以 (SeriesId, SeasonNum) 为核心 Key 的本地与元数据双层映射
        series_season_real_eps = defaultdict(dict)
        series_season_meta_eps = defaultdict(dict)

        for ep in raw_episodes:
            series_id = ep.get("SeriesId")
            # 如果没有显式 ParentIndexNumber，默认归为 Season 1
            season_num = int(ep.get("ParentIndexNumber") if ep.get("ParentIndexNumber") is not None else 1)
            ep_num = ep.get("IndexNumber")
            premiere_date = ep.get("PremiereDate", "")

            if not series_id or ep_num is None:
                continue

            composite_key = (series_id, season_num)
            series_season_meta_eps[composite_key][ep_num] = premiere_date

            if ep.get("LocationType") != "Virtual":
                series_season_real_eps[composite_key][ep_num] = premiere_date

        missing_results = []

        # 4. 遍历 Seasons 精准比对
        for season_item in raw_seasons:
            series_id = season_item.get("SeriesId")
            series_name = season_item.get("SeriesName") or "未知剧集"
            season_num = int(season_item.get("IndexNumber") if season_item.get("IndexNumber") is not None else 1)
            target_child_count = int(season_item.get("ChildCount") if season_item.get("ChildCount") is not None else 0)

            # 忽略特别篇
            if self._ignore_season_zero and season_num == 0:
                continue

            composite_key = (series_id, season_num)
            real_eps_dict = series_season_real_eps.get(composite_key, {})
            meta_eps_dict = series_season_meta_eps.get(composite_key, {})

            # 🚨【核心防御 1】：如果当前 Season 在本地无任何物理文件，直接 100% 跳过！
            # 绝对不凭空拉取 TMDB 的标称总集数（1000+）来强行判定整季缺失
            if not real_eps_dict:
                continue

            max_local_ep = max(real_eps_dict.keys())
            max_meta_ep = max(meta_eps_dict.keys()) if meta_eps_dict else 0

            # 计算合理的目标比对集数上限
            total_target = max(max_local_ep, target_child_count, max_meta_ep)

            # 🚨【核心防御 2】：如果本地真实文件数已经覆盖到了最大集号且达到了目标集数，说明完结且无断集
            if len(real_eps_dict) >= total_target and max_local_ep >= total_target:
                continue

            missing_ep_numbers = []

            # 在 1 到 total_target 范围内精准查找空缺
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
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】停止服务失败: {e}")
