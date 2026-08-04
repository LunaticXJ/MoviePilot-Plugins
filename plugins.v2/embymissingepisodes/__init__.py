import base64
import csv
import io
import concurrent.futures
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
    Emby 剧集缺集检查插件 v20.0.0 (TMDB 真理溯源版)
    - 底层重构：抛弃 Emby 虚假元数据，完全参照 TMDB API 获取标准集数。
    - 算法降维：通过 TMDB(理论集) - Emby(物理集) = 绝对缺集，彻底消灭长篇漫误报与季播剧漏检。
    - 性能优化：引入 ThreadPoolExecutor 并发扫描引擎，大幅提升扫描速度。
    - UI 修复：严守 Vuetify 2 规范，修复白屏问题，保留纯前端 Base64 极速导出。
    """

    plugin_name = "Emby剧集缺集检查"
    plugin_desc = "基于 TMDB 数据源，精准比对并查找 Emby 库中的缺失剧集。"
    plugin_icon = ""
    plugin_version = "20.0.0"
    plugin_author = "LunaticXJ"
    author_url = "https://github.com/LunaticXJ"
    plugin_config_prefix = "embymissingepisodes_"
    plugin_order = 16
    auth_level = 1

    _onlyonce = False
    _mediaserver = ""
    _tmdb_api_key = ""
    _concurrency_workers = 4
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
            self._mediaserver = config.get("mediaserver", "")
            self._tmdb_api_key = config.get("tmdb_api_key", "").strip()
            self._concurrency_workers = int(config.get("concurrency_workers", 4))
            self._ignore_season_zero = config.get("ignore_season_zero", True)
            self._ignore_future = config.get("ignore_future", True)

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("【EmbyMissingEpisodes】检查到“立即运行一次”，将在 3 秒后执行并发缺集扫描...")
                self._scheduler.add_job(
                    self.scan_missing_episodes,
                    'date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="EmbyTMDB并发缺集扫描"
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
            "tmdb_api_key": self._tmdb_api_key,
            "concurrency_workers": self._concurrency_workers,
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "tmdb_api_key",
                                            "label": "TMDB API Key (必填，用于提供绝对精准的理论集数参考)",
                                            "placeholder": "请输入 v3 API Key",
                                            "clearable": True,
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "concurrency_workers",
                                            "label": "并发请求线程数",
                                            "items": [
                                                {"title": "单线程 (最稳)", "value": 1},
                                                {"title": "4 线程 (推荐)", "value": 4},
                                                {"title": "8 线程 (极速)", "value": 8},
                                            ],
                                        }
                                    }
                                ]
                            }
                        ]
                    },
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
                                            "label": "保存并立即运行扫描",
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
            "tmdb_api_key": "",
            "concurrency_workers": 4,
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
        csv_download_href = self._build_csv_base64_href()
        status_text = f"正在进行 TMDB 并发比对扫描中..." if self._is_scanning else f"上次扫描: {self._last_scan_time}"

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "success",
                                    "href": csv_download_href,
                                    "download": f"EmbyTMDB缺集比对_{datetime.now().strftime('%Y%m%d%H%M')}.csv",
                                    "variant": "tonal",
                                    "prepend-icon": "mdi-download",
                                },
                                "text": f"导出 CSV ({status_text})"
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [{
                    "component": "VDataTable",
                    "props": {
                        "headers": [
                            {"text": "剧集名称", "value": "SeriesName"},
                            {"text": "缺失季度", "value": "SeasonFormatted", "width": "120px"},
                            {"text": "缺失集号", "value": "MissingEpisodes"}
                        ],
                        "items": self._cache_missing_results,
                        "hover": True,
                        "density": "comfortable",
                        "items-per-page": 15,
                        "no-data-text": "暂无数据。请确保已填入 TMDB API Key 并执行了扫描。"
                    }
                }]
            }
        ]

    def scan_missing_episodes(self):
        if self._is_scanning:
            logger.warn("【EmbyMissingEpisodes】上次并发扫描尚未结束，跳过本次触发。")
            return

        if not self._tmdb_api_key:
            logger.error("【EmbyMissingEpisodes】未配置 TMDB API Key，无法执行溯源比对扫描！")
            return

        self._is_scanning = True
        start_time = datetime.now()
        logger.info(f"【EmbyMissingEpisodes】启动 TMDB 并发扫描引擎 (线程数: {self._concurrency_workers})...")

        try:
            if not self._mediaserver:
                logger.error("【EmbyMissingEpisodes】未选择 Emby 媒体服务器！")
                return

            emby_servers = self.mediaserver_helper.get_services(name_filters=[self._mediaserver], type_filter="emby")
            if not emby_servers:
                logger.error(f"【EmbyMissingEpisodes】未找到对应的 Emby 服务器。")
                return

            emby_server = list(emby_servers.values())[0]
            missing_list = self._execute_concurrent_scan(emby_server)

            self._cache_missing_results = missing_list
            self._last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self.save_data(self._STORAGE_DATA_KEY, self._cache_missing_results)
            self.save_data(self._STORAGE_TIME_KEY, self._last_scan_time)

            elapsed = (datetime.now() - start_time).total_seconds()
            logger.info(f"【EmbyMissingEpisodes】>>> 扫描圆满结束！耗时 {elapsed:.1f} 秒，检出 {len(missing_list)} 季存在缺集。")
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】并发扫描崩溃: {e}", exc_info=True)
        finally:
            self._is_scanning = False

    def _has_real_media(self, ep: Dict[str, Any]) -> bool:
        """只认证物理存在的真理标准"""
        if ep.get("Path"):
            return True
        sources = ep.get("MediaSources") or []
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and source.get("Path"):
                    return True
        return False

    def _format_episode_ranges(self, episodes: set) -> str:
        """格式化连续集数 (如 1,2,3,5 -> 1-3、5)"""
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

    def _process_single_series(self, series_item: dict, host: str, api_key: str, user_id: str, today_str: str) -> List[Dict[str, Any]]:
        """
        工作线程：单个剧集的完整 TMDB 校验与 Emby 实体查漏闭环
        """
        series_id = series_item.get("Id")
        series_name = series_item.get("Name", "未知剧集")
        provider_ids = series_item.get("ProviderIds") or {}
        
        tmdb_id_str = provider_ids.get("Tmdb") or provider_ids.get("TMDB") or provider_ids.get("tmdb")
        if not tmdb_id_str:
            return []

        # 1. 问责 TMDB：这部剧理论上该有多少季？
        tmdb_series_url = f"https://api.themoviedb.org/3/tv/{tmdb_id_str}?api_key={self._tmdb_api_key}&language=zh-CN"
        res_tmdb = RequestUtils().get_res(tmdb_series_url)
        if not res_tmdb or res_tmdb.status_code != 200:
            return []
            
        tmdb_seasons = res_tmdb.json().get("seasons", [])
        expected_map = defaultdict(set)

        # 2. 问责 TMDB 详情：每一季每一集是否开播？
        for season in tmdb_seasons:
            s_num = season.get("season_number")
            if s_num is None or (s_num == 0 and self._ignore_season_zero):
                continue
                
            season_url = f"https://api.themoviedb.org/3/tv/{tmdb_id_str}/season/{s_num}?api_key={self._tmdb_api_key}&language=zh-CN"
            res_season = RequestUtils().get_res(season_url)
            if not res_season or res_season.status_code != 200:
                continue
                
            episodes = res_season.json().get("episodes", [])
            for ep in episodes:
                ep_num = ep.get("episode_number")
                if ep_num is None:
                    continue
                    
                if self._ignore_future:
                    air_date = ep.get("air_date") or ""
                    if not air_date or air_date > today_str:
                        continue
                        
                expected_map[s_num].add(ep_num)

        if not expected_map:
            return []

        # 3. 问责 Emby 物理硬盘：这部剧到底存了什么物理文件？
        emby_eps_url = (
            f"{host}/emby/Users/{user_id}/Items?"
            f"ParentId={series_id}&Recursive=true&IncludeItemTypes=Episode"
            f"&Fields=ParentIndexNumber,IndexNumber,Path,MediaSources"
            f"&api_key={api_key}"
        )
        res_emby = RequestUtils().get_res(emby_eps_url)
        existing_map = defaultdict(set)
        
        if res_emby and res_emby.status_code == 200:
            emby_eps = res_emby.json().get("Items", [])
            for ep in emby_eps:
                if self._has_real_media(ep):
                    s_num_raw = ep.get("ParentIndexNumber")
                    ep_num_raw = ep.get("IndexNumber")
                    try:
                        s_num = int(s_num_raw) if s_num_raw is not None else 1
                        ep_num = int(ep_num_raw) if ep_num_raw is not None else None
                    except (ValueError, TypeError):
                        continue
                    if ep_num is not None:
                        existing_map[s_num].add(ep_num)

        # 4. 纯粹的绝对减法 (Expected - Existing)
        results = []
        for s_num in sorted(expected_map.keys()):
            expected_eps = expected_map[s_num]
            existing_eps = existing_map.get(s_num, set())
            
            missing_eps = expected_eps - existing_eps
            
            if missing_eps:
                results.append({
                    "SeriesName": series_name,
                    "SeasonNum": s_num,
                    "SeasonFormatted": f"S{s_num}" if s_num > 0 else "SP",
                    "MissingEpisodes": self._format_episode_ranges(missing_eps),
                })
                
        return results

    def _execute_concurrent_scan(self, emby_server) -> List[Dict[str, Any]]:
        """并发任务调度中心"""
        raw_host = emby_server.config.config.get("host") or ""
        host = raw_host.rstrip('/')
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        if not host or not api_key or not user_id:
            return []

        today_str = datetime.now().strftime("%Y-%m-%d")

        # 仅拉取顶层剧集元数据
        series_url = (
            f"{host}/emby/Users/{user_id}/Items?"
            f"Recursive=true&IncludeItemTypes=Series"
            f"&Fields=ProviderIds"
            f"&api_key={api_key}"
        )
        res_series = RequestUtils().get_res(series_url)
        if not res_series or res_series.status_code != 200:
            return []
            
        all_series = res_series.json().get("Items", [])
        
        final_missing_results = []
        workers = max(1, min(self._concurrency_workers, 16)) # 限制最大 16 线程防止炸库

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_series = {
                executor.submit(self._process_single_series, series, host, api_key, user_id, today_str): series.get("Name")
                for series in all_series
            }
            
            for future in concurrent.futures.as_completed(future_to_series):
                try:
                    result = future.result()
                    if result:
                        final_missing_results.extend(result)
                except Exception as e:
                    s_name = future_to_series[future]
                    logger.debug(f"【EmbyMissingEpisodes】扫描剧集 {s_name} 时发生内部错误: {e}")

        # 结果排序
        final_missing_results.sort(key=lambda x: (x["SeriesName"], x["SeasonNum"]))
        for item in final_missing_results:
            item.pop("SeasonNum", None)

        return final_missing_results

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】停止服务失败: {e}")
