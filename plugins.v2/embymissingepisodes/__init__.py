import base64
import csv
import io
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils


class EmbyMissingEpisodes(_PluginBase):
    """
    Emby 剧集缺集检查插件 v18.0.0 (底层算法觉醒版)
    - 修复 MoviePilot Vuetify 2 表头 text/value 渲染规范，解决页面白屏。
    - 独创 Dual-Fetch 双轨拉取算法：物理集取边界，虚拟集取差集，完美避开漏检与长篇漫误检。
    - 纯前端 Base64 导出，零后端 API 依赖，免疫 401 拦截。
    - 保留连续集数智能合并格式化 (如 1-3、5)。
    """

    PAGE_SIZE: int = 1500  # API 流式分页单页条数

    plugin_name = "Emby剧集缺集检查"
    plugin_desc = "精准查找 Emby 库中剧集的缺失集情况，聚合显示并支持导出 CSV。"
    plugin_icon = ""
    plugin_version = "1.0.2"
    plugin_author = "LunaticXJ"
    author_url = "https://github.com/LunaticXJ"
    plugin_config_prefix = "embymissingepisodes_"
    plugin_order = 16
    auth_level = 1

    _onlyonce = False
    _mediaserver = ""
    _ignore_season_zero = True
    _ignore_future = True

    _STORAGE_DATA_KEY = "missing_episodes_data"
    _STORAGE_TIME_KEY = "missing_episodes_last_time"

    _cache_missing_results: List[Dict[str, Any]] = []
    _last_scan_time: str = "从未扫描"
    _is_scanning: bool = False

    mediaserver_helper = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
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

    def _build_csv_base64_href(self) -> str:
        output = io.StringIO()
        output.write('\ufeff')
        writer = csv.writer(output)
        writer.writerow(["剧集名称", "缺失季度", "缺失集号"])

        for row in self._cache_missing_results:
            writer.writerow([
                row.get("SeriesName", ""),
                row.get("SeasonFormatted", ""),
                row.get("MissingEpisodes", "")
            ])

        csv_bytes = output.getvalue().encode('utf-8')
        base64_str = base64.b64encode(csv_bytes).decode('utf-8')
        return f"data:text/csv;charset=utf-8;base64,{base64_str}"

    def get_page(self) -> List[dict]:
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

        csv_download_href = self._build_csv_base64_href()
        download_filename = f"Emby缺集清单_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

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
                                                            "href": csv_download_href,
                                                            "download": download_filename,
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
                                    "item-key": "id",
                                    # 🚨 完全对齐 MoviePilot Vuetify 2 规范，修复白屏问题
                                    "headers": [
                                        {"text": "剧集名称", "value": "SeriesName"},
                                        {"text": "缺失季度", "value": "SeasonFormatted", "width": "120px"},
                                        {"text": "缺失集号", "value": "MissingEpisodes"},
                                    ],
                                    "items": table_items,
                                    "hover": True,
                                    "density": "comfortable",
                                    "items-per-page": 15,
                                    "no-data-text": "暂无缺失剧集数据。请在插件设置中选择 Emby 服务器，勾选【立即运行一次】并保存。",
                                },
                            }
                        ],
                    },
                ],
            }
        ]

    def scan_missing_episodes(self):
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

    def _has_real_media(self, ep: Dict[str, Any]) -> bool:
        """
        抛弃不可靠的 LocationType，严格通过物理路径判定实体文件
        """
        if ep.get("Path"):
            return True
        sources = ep.get("MediaSources") or []
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and source.get("Path"):
                    return True
        return False

    def _format_episode_ranges(self, episodes: set) -> str:
        """连续集数合并 (如 1,2,3,5 -> 1-3、5)"""
        if not episodes:
            return ""
        nums = sorted(episodes)
        result = []
        start = prev = nums[0]
        for n in nums[1:]:
            if n == prev + 1:
                prev = n
            else:
                result.append(f"{start}-{prev}" if start != prev else str(start))
                start = prev = n
        result.append(f"{start}-{prev}" if start != prev else str(start))
        return "、".join(result)

    def _fetch_episodes_from_emby(self, host, user_id, api_key, is_missing: bool) -> List[Dict]:
        """
        高度解耦的流式拉取器
        """
        results = []
        limit = self.PAGE_SIZE
        start_index = 0
        missing_param = "&IsMissing=true" if is_missing else ""

        while True:
            url = (
                f"{host}/emby/Users/{user_id}/Items?"
                f"Recursive=true&IncludeItemTypes=Episode"
                f"&StartIndex={start_index}&Limit={limit}"
                f"{missing_param}"
                f"&Fields=ParentId,IndexNumber,Path,MediaSources,PremiereDate"
                f"&api_key={api_key}"
            )
            res = RequestUtils().get_res(url)
            if not res or res.status_code != 200:
                break

            items = res.json().get("Items") or []
            results.extend(items)

            if len(items) < limit:
                break

            start_index += limit
            
        return results

    def _scan_server_by_diff(self, server_name: str, emby_server) -> List[Dict[str, Any]]:
        raw_host = emby_server.config.config.get("host") or ""
        host = raw_host.rstrip('/')
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        if not host or not api_key or not user_id:
            return []

        today_date_str = datetime.now().strftime("%Y-%m-%d")

        # 1. 拉取季列表
        seasons_url = (
            f"{host}/emby/Users/{user_id}/Items?"
            f"Recursive=true&IncludeItemTypes=Season"
            f"&Fields=SeriesName,SeriesId,IndexNumber"
            f"&api_key={api_key}"
        )
        res_seasons = RequestUtils().get_res(seasons_url)
        if not res_seasons or res_seasons.status_code != 200:
            return []
        raw_seasons = res_seasons.json().get("Items") or []

        # 2. 🚨 Dual-Fetch 双轨拉取：分别拉取真实文件和虚拟缺失集
        raw_real_episodes = self._fetch_episodes_from_emby(host, user_id, api_key, is_missing=False)
        raw_missing_episodes = self._fetch_episodes_from_emby(host, user_id, api_key, is_missing=True)

        # 3. 建立映射字典
        real_dict = defaultdict(set)
        missing_dict = defaultdict(list)

        for ep in raw_real_episodes:
            if self._has_real_media(ep):
                season_id = ep.get("ParentId")
                ep_num_raw = ep.get("IndexNumber")
                try:
                    ep_num = int(ep_num_raw) if ep_num_raw is not None else None
                except (ValueError, TypeError):
                    continue
                if season_id and ep_num is not None:
                    real_dict[season_id].add(ep_num)

        for ep in raw_missing_episodes:
            season_id = ep.get("ParentId")
            premiere = ep.get("PremiereDate", "")
            ep_num_raw = ep.get("IndexNumber")
            try:
                ep_num = int(ep_num_raw) if ep_num_raw is not None else None
            except (ValueError, TypeError):
                continue
            if season_id and ep_num is not None:
                missing_dict[season_id].append((ep_num, premiere))

        missing_results = []

        # 4. 🚨 动态截断与清洗
        for season in raw_seasons:
            season_id = season.get("Id")
            series_name = season.get("SeriesName") or "未知剧集"
            
            try:
                season_num_raw = season.get("IndexNumber")
                season_num = int(season_num_raw) if season_num_raw is not None else 1
            except (ValueError, TypeError):
                season_num = 1

            if self._ignore_season_zero and season_num == 0:
                continue

            real_nums = real_dict.get(season_id, set())
            missing_candidates = missing_dict.get(season_id, [])

            if not missing_candidates:
                continue

            # 获取真实文件最小边界（若整季无文件，默认为 1）
            min_real = min(real_nums) if real_nums else 1

            valid_missing_set = set()

            for ep_num, premiere_date in missing_candidates:
                # 刀锋一：砍掉长篇漫的历史虚拟占位符 (绝对集数防坑)
                if ep_num < min_real:
                    continue

                # 刀锋二：过滤未来未开播剧集
                if self._ignore_future:
                    formatted_date = premiere_date[:10] if len(premiere_date) >= 10 else "未知/未开播"
                    if formatted_date != "未知/未开播" and formatted_date > today_date_str:
                        continue
                
                valid_missing_set.add(ep_num)

            if valid_missing_set:
                season_display = f"S{season_num}" if season_num > 0 else "SP"
                missing_results.append({
                    "SeriesName": series_name,
                    "SeasonNum": season_num,
                    "SeasonFormatted": season_display,
                    "MissingEpisodes": self._format_episode_ranges(valid_missing_set),
                })

        # 多级自然排序
        missing_results.sort(key=lambda x: (x["SeriesName"], x["SeasonNum"]))

        for item in missing_results:
            item.pop("SeasonNum", None)

        return missing_results

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】停止服务失败: {e}")
