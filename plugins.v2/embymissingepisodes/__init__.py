import base64
import csv
import io
import time
import requests
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
    Emby 剧集缺集检查插件 v1.0.0 (Gaps 算法独立插件版)
    - 修复依赖：摒弃侵入式核心 cfg 调用，回归标准的插件表单输入与 settings 环境。
    - 智能合集：支持 IndexNumberEnd 字段，完美解析 S01E01-E03 合集文件，杜绝误报。
    - 内存比对：全局库存单次内存拉取构建，避免 API 洪泛导致 Emby 服务崩溃。
    - UI 兼容：严格遵守 Vuetify 2 规范，解决渲染白屏，保留原生前端 Base64 导出。
    """

    plugin_name = "Emby剧集缺集检查"
    plugin_desc = "利用 TMDB 溯源精准比对并查找 Emby 库中的缺失剧集。"
    plugin_icon = ""
    plugin_version = "22.0.0"
    plugin_author = "LunaticXJ"
    author_url = "https://github.com/LunaticXJ"
    plugin_config_prefix = "embymissingepisodes_"
    plugin_order = 16
    auth_level = 1

    _onlyonce = False
    _mediaserver = ""
    _tmdb_api_key = ""
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
            self._ignore_season_zero = config.get("ignore_season_zero", True)
            self._ignore_future = config.get("ignore_future", True)

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("【EmbyMissingEpisodes】检查到“立即运行一次”，将在 3 秒后执行全局内存并发扫描...")
                self._scheduler.add_job(
                    self.scan_missing_episodes,
                    'date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="EmbyTMDB全局内存缺集扫描"
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
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "tmdb_api_key",
                                            "label": "TMDB API Key (必填，用于拉取最准确的应有集数)",
                                            "placeholder": "请输入 TMDB v3 API Key",
                                            "clearable": True,
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
                                            "label": "保存并立即运行一次",
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
        status_text = f"后台扫描中..." if self._is_scanning else f"上次扫描: {self._last_scan_time}"

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
                                    "download": f"EmbyTMDB缺集清单_{datetime.now().strftime('%Y%m%d%H%M')}.csv",
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
                        "no-data-text": "暂无数据。请确保配置了 TMDB API Key 并执行了扫描。"
                    }
                }]
            }
        ]

    def _get_proxies(self):
        """安全读取 MoviePilot 系统代理设置，避免断网"""
        proxy = getattr(settings, "PROXY", None)
        if proxy:
            return {"http": proxy, "https": proxy}
        return None

    def _format_episode_ranges(self, episodes: set) -> str:
        """智能合并连续集数 (如 1,2,3,5 -> 1-3、5)"""
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

    def _process_single_series(self, series: dict, global_inventory: dict, tmdb_key: str, proxies: dict, today: str) -> List[Dict]:
        """独立线程处理单个剧集，基于内存查漏补缺"""
        series_id = series.get("Id")
        series_name = series.get("Name", "未知剧集")
        tmdb_id = series.get("ProviderIds", {}).get("Tmdb")
        
        if not tmdb_id:
            return []

        local_inventory = global_inventory.get(series_id, {})
        
        try:
            tmdb_series_data = requests.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}?language=zh-CN&api_key={tmdb_key}", 
                proxies=proxies, 
                timeout=10
            ).json()
            tmdb_seasons = tmdb_series_data.get("seasons", [])
        except Exception:
            return []

        series_gaps = []
        
        for season in tmdb_seasons:
            s_num = season.get("season_number")
            if s_num is None or season.get("episode_count", 0) == 0: 
                continue
            
            if self._ignore_season_zero and s_num == 0:
                continue
                
            local_season_inventory = local_inventory.get(s_num, set())
            
            if len(local_season_inventory) >= season.get("episode_count", 0):
                continue

            try:
                tmdb_episodes = requests.get(
                    f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{s_num}?language=zh-CN&api_key={tmdb_key}", 
                    proxies=proxies, 
                    timeout=10
                ).json().get("episodes", [])
            except Exception:
                continue
                
            missing_eps = set()
            for tmdb_ep in tmdb_episodes:
                e_num = tmdb_ep.get("episode_number")
                air_date = tmdb_ep.get("air_date")
                
                if self._ignore_future and (not air_date or air_date >= today):
                    continue
                    
                if e_num not in local_season_inventory:
                    missing_eps.add(e_num)
                    
            if missing_eps:
                series_gaps.append({
                    "SeriesName": series_name,
                    "SeasonNum": s_num,
                    "SeasonFormatted": f"S{s_num}" if s_num > 0 else "SP",
                    "MissingEpisodes": self._format_episode_ranges(missing_eps)
                })
                
        return series_gaps

    def scan_missing_episodes(self):
        if self._is_scanning:
            logger.warn("【EmbyMissingEpisodes】上次并发扫描尚未结束，跳过本次执行。")
            return

        if not self._tmdb_api_key:
            logger.error("【EmbyMissingEpisodes】未配置插件专用的 TMDB API Key！扫描中止。")
            return

        self._is_scanning = True
        start_time = datetime.now()
        logger.info("【EmbyMissingEpisodes】启动基于全局内存缓存的并发缺集扫描...")

        try:
            if not self._mediaserver:
                logger.error("【EmbyMissingEpisodes】未选择 Emby 媒体服务器！")
                return

            emby_servers = self.mediaserver_helper.get_services(name_filters=[self._mediaserver], type_filter="emby")
            if not emby_servers:
                logger.error(f"【EmbyMissingEpisodes】未找到匹配的 Emby 服务器。")
                return

            emby_server = list(emby_servers.values())[0]
            
            raw_host = emby_server.config.config.get("host") or ""
            host = raw_host.rstrip('/')
            api_key = emby_server.config.config.get("apikey")
            user_id = emby_server.instance.get_user()
            
            if not host or not api_key or not user_id:
                logger.error("【EmbyMissingEpisodes】获取 Emby API 配置失败！")
                return

            proxies = self._get_proxies()
            today = datetime.now().strftime("%Y-%m-%d")

            # 1. 极速提取剧集外壳
            logger.info("【EmbyMissingEpisodes】步骤 1/3: 正在拉取剧集外壳...")
            all_series_url = (
                f"{host}/emby/Users/{user_id}/Items?"
                f"Recursive=true&IncludeItemTypes=Series&Fields=ProviderIds&api_key={api_key}"
            )
            res_series = RequestUtils().get_res(all_series_url)
            if not res_series or res_series.status_code != 200:
                logger.error("【EmbyMissingEpisodes】无法获取 Emby 剧集列表。")
                return
            all_series = res_series.json().get("Items", [])

            # 2. 核心：全局构建真实物理库存 (过滤虚拟卡片，完美处理 IndexNumberEnd 合集文件)
            logger.info("【EmbyMissingEpisodes】步骤 2/3: 正在构建全局单集内存缓冲池...")
            all_eps_url = (
                f"{host}/emby/Users/{user_id}/Items?"
                f"Recursive=true&IncludeItemTypes=Episode&Fields=IndexNumberEnd,LocationType&api_key={api_key}"
            )
            res_eps = RequestUtils().get_res(all_eps_url)
            all_eps_data = res_eps.json().get("Items", []) if res_eps and res_eps.status_code == 200 else []

            global_inventory = defaultdict(lambda: defaultdict(set))
            for ep in all_eps_data:
                if ep.get("LocationType") == "Virtual":
                    continue
                    
                ser_id = ep.get("SeriesId")
                s_num = ep.get("ParentIndexNumber")
                e_num = ep.get("IndexNumber")
                e_end = ep.get("IndexNumberEnd")
                
                if not ser_id or s_num is None or e_num is None: 
                    continue
                
                for i in range(e_num, (e_end if e_end else e_num) + 1):
                    global_inventory[ser_id][s_num].add(i)

            logger.info(f"【EmbyMissingEpisodes】步骤 3/3: 内存池就绪，开启 8 线程并发比对 ({len(all_series)} 部剧集)...")

            # 3. TMDB 并发查漏补缺
            final_missing_results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(self._process_single_series, s, global_inventory, self._tmdb_api_key, proxies, today) 
                    for s in all_series
                ]
                
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res:
                        final_missing_results.extend(res)

            # 4. 去重排序与保存
            final_missing_results.sort(key=lambda x: (x["SeriesName"], x["SeasonNum"]))
            for item in final_missing_results:
                item.pop("SeasonNum", None)

            self._cache_missing_results = final_missing_results
            self._last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self.save_data(self._STORAGE_DATA_KEY, self._cache_missing_results)
            self.save_data(self._STORAGE_TIME_KEY, self._last_scan_time)

            elapsed = (datetime.now() - start_time).total_seconds()
            logger.info(f"【EmbyMissingEpisodes】>>> 扫描圆满完成！耗时 {elapsed:.1f} 秒，检出 {len(final_missing_results)} 条缺失项。")
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】扫描过程崩溃: {e}", exc_info=True)
        finally:
            self._is_scanning = False

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】停止服务失败: {e}")
