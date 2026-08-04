import base64
import csv
import io
import re
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
    Emby 剧集缺集检查插件 v16.1.0 (极致审查定版)
    - 修复 Base64 下载由于缺少 download 属性导致浏览器直接打开文本而非下载的瑕疵
    - 纯前端 Base64 导出，零后端 API 路由依赖，100% 免疫 401/404 及 Vue 渲染崩溃
    - 一元归一化差集算法，自然升序排列，全方位强类型脏数据防御
    """

    # ----------------------------------------------------
    # ⚙️ 开发者/高级配置变量
    # ----------------------------------------------------
    PAGE_SIZE: int = 1000  # API 流式分页单页条数

    # 插件名称
    plugin_name = "Emby剧集缺集检查"
    # 插件描述
    plugin_desc = "精准查找 Emby 库中剧集的缺失集情况，聚合显示并支持导出 CSV。"
    # 插件图标 (已置空)
    plugin_icon = ""
    # 插件版本
    plugin_version = "16.5.0"
    # 插件作者
    plugin_author = "LunaticXJ"
    # 作者主页
    author_url = "https://github.com/LunaticXJ"
    # 插件配置项ID前缀
    plugin_config_prefix = "embymissingepisodes_"
    # 加载顺序
    plugin_order = 16
    # 插件权限 (1 为管理员权限)
    auth_level = 1

    # 插件私有属性
    _onlyonce = False
    _mediaserver = ""
    _ignore_season_zero = True
    _ignore_future = True
    _ignore_special_episode = True
    _ignore_invalid_episode = True
    _min_episode_number = 1
    _max_episode_number = 9999

    # 持久化存储 Key
    _STORAGE_DATA_KEY = "missing_episodes_data"
    _STORAGE_TIME_KEY = "missing_episodes_last_time"
    

    # 内存缓存与状态锁
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
        从本地持久化文件读取数据
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
        配置表单页面
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

    def _build_csv_base64_href(self) -> str:
        """
        生成安全、稳定的 Base64 Data URI 下载流
        """
        output = io.StringIO()
        output.write('\ufeff')  # 写入唯一 BOM 头防中文乱码
        writer = csv.writer(output)
        writer.writerow(["剧集名称", "缺失季度", "缺失集号"])

        for row in self._cache_missing_results:
            writer.writerow([
                row.get("SeriesName", ""),
                row.get("SeasonFormatted", ""),
                row.get("MissingEpisodes", "")
            ])

        # 仅用普通 utf-8 编码，防止生成双重 BOM，转为安全 Base64
        csv_bytes = output.getvalue().encode('utf-8')
        base64_str = base64.b64encode(csv_bytes).decode('utf-8')
        return f"data:text/csv;charset=utf-8;base64,{base64_str}"

    def get_page(self) -> List[dict]:
        """
        拼装插件数据主页面：完美兼顾渲染与文件导出
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
                                                            "download": download_filename, # 🚨 核心修复：强制浏览器将其作为文件下载，而非文本展示
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
                                    "item-value": "id",
                                    "headers": [
                                        {"title": "剧集名称", "key": "SeriesName", "text": "剧集名称", "value": "SeriesName"},
                                        {"title": "缺失季度", "key": "SeasonFormatted", "text": "缺失季度", "value": "SeasonFormatted"},
                                        {"title": "缺失集号", "key": "MissingEpisodes", "text": "缺失集号", "value": "MissingEpisodes"},
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
        """
        后台扫描任务 (自带并发锁)
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
            scan_result = self._scan_server_by_diff(self._mediaserver, emby_server)

            # 扫描失败不覆盖历史正确结果
            if not scan_result.get("success", False):
                logger.error("【EmbyMissingEpisodes】扫描失败，保留旧缓存数据")
                return

            missing_list = scan_result.get("data", [])
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

    def _safe_int(self, value, default=None):
        try:
            if value is None or value == "":
                return default
            return int(value)
        except Exception:
            return default

    def _has_real_media(self, ep: Dict[str, Any]) -> bool:
        if ep.get("Path"):
            return True
        sources = ep.get("MediaSources") or []
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and source.get("Path"):
                    return True
        return False

    def _is_valid_episode(self, ep: Dict[str, Any], today_date_str: str) -> bool:
        """Episode数据过滤，避免特殊集、未来集、垃圾元数据进入计算"""
        if self._ignore_invalid_episode:
            try:
                ep_num = int(ep.get("IndexNumber"))
            except (TypeError, ValueError):
                return False
            if ep_num < self._min_episode_number or ep_num > self._max_episode_number:
                return False

        if self._ignore_future:
            premiere = ep.get("PremiereDate") or ""
            if len(premiere) >= 10 and premiere[:10] > today_date_str:
                return False

        if self._ignore_special_episode:
            # 特别集优先由Season 0过滤，不通过名称误伤正常剧集
            if self._safe_int(ep.get("ParentIndexNumber"), -1) == 0:
                return False

        return True

    def _merge_episode_set(self, data: dict, key1, key2) -> set:
        """合并SeasonId和SeriesId映射，避免or导致数据丢失"""
        result = set()
        if key1 and data.get(key1):
            result.update(data.get(key1, {}).keys())
        if key2 and data.get(key2):
            result.update(data.get(key2, {}).keys())
        return result

    def _scan_server_by_diff(self, server_name: str, emby_server) -> Dict[str, Any]:
        """精准缺集扫描：完整Metadata全集 - 实际文件全集"""
        raw_host = emby_server.config.config.get("host") or ""
        host = raw_host.rstrip('/')
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        if not host or not api_key or not user_id:
            return {"success": False, "data": None}

        today_date_str = datetime.now().strftime("%Y-%m-%d")

        seasons_url = (
            f"{host}/emby/Users/{user_id}/Items?"
            f"Recursive=true&IncludeItemTypes=Season"
            f"&Fields=SeriesName,SeriesId,ParentId,IndexNumber,ChildCount,PremiereDate"
            f"&api_key={api_key}"
        )

        res_seasons = RequestUtils().get_res(seasons_url)
        if not res_seasons or res_seasons.status_code != 200:
            logger.error(f"【EmbyMissingEpisodes】[{server_name}] 拉取Season失败")
            return {"success": False, "data": None}

        raw_seasons = res_seasons.json().get("Items") or []

        raw_episodes = []
        limit = self.PAGE_SIZE
        start_index = 0

        while True:
            url = (
                f"{host}/emby/Users/{user_id}/Items?"
                f"Recursive=true&IncludeItemTypes=Episode"
                f"&StartIndex={start_index}&Limit={limit}"
                f"&Fields=ParentId,SeriesId,IndexNumber,ParentIndexNumber,Path,MediaSources,PremiereDate,Name,ProductionYear"
                f"&api_key={api_key}"
            )

            res = RequestUtils().get_res(url)
            if not res or res.status_code != 200:
                break

            items = res.json().get("Items") or []
            raw_episodes.extend(items)

            if len(items) < limit:
                break

            start_index += limit

        meta = defaultdict(dict)
        real = defaultdict(dict)

        for ep in raw_episodes:
            if not self._is_valid_episode(ep, today_date_str):
                continue

            try:
                ep_num = int(ep.get("IndexNumber"))
            except Exception:
                continue

            season_num = self._safe_int(ep.get("ParentIndexNumber"), None)
            if season_num is None:
                continue

            premiere = ep.get("PremiereDate") or ""
            keys = [
                ep.get("ParentId"),
                (ep.get("SeriesId"), season_num)
            ]

            for key in keys:
                if key:
                    meta[key][ep_num] = premiere
                    if self._has_real_media(ep):
                        real[key][ep_num] = premiere

        result = []

        for season in raw_seasons:
            season_id = season.get("Id")
            series_id = season.get("SeriesId")
            name = season.get("SeriesName") or "未知剧集"

            try:
                season_num_raw = season.get("IndexNumber")
                if season_num_raw is None:
                    continue
                season_num = int(season_num_raw)
            except Exception:
                continue

            if self._ignore_season_zero and season_num == 0:
                continue

            key1 = season_id
            key2 = (series_id, season_num)

            expected = self._merge_episode_set(meta, key1, key2)
            actual = self._merge_episode_set(real, key1, key2)

            # 没有元数据无法判断，有元数据但无文件属于整季缺失，需要保留
            if not expected:
                continue

            missing = sorted(expected - actual)

            if not missing:
                continue

            # 逐集过滤未来集，不使用尾部假设
            if self._ignore_future:
                valid_missing = []
                for ep in missing:
                    date = ""
                    if meta.get(key1):
                        date = meta.get(key1, {}).get(ep, "") or date
                    if meta.get(key2):
                        date = meta.get(key2, {}).get(ep, "") or date
                    if len(date) >= 10 and date[:10] > today_date_str:
                        continue
                    valid_missing.append(ep)
                missing = valid_missing

            if missing:
                result.append({
                    "SeriesName": name,
                    "SeasonNum": season_num,
                    "SeasonFormatted": f"S{season_num}" if season_num else "SP",
                    "MissingEpisodes": self._format_episode_ranges(missing)
                })

                logger.info(
                    f"【EmbyMissingEpisodes】{name} S{season_num}: "
                    f"总集{len(expected)} 已有{len(actual)} 缺失{missing}"
                )

        # 结果去重
        dedup = {}
        for item in result:
            dedup[(item["SeriesName"], item["SeasonNum"])] = item
        result = list(dedup.values())

        result.sort(key=lambda x: (x["SeriesName"], x["SeasonNum"]))

        for item in result:
            item.pop("SeasonNum", None)

        return {"success": True, "data": result}

    def _format_episode_ranges(self, episodes):
        if not episodes:
            return ""
        nums = sorted(set(int(x) for x in episodes))
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
