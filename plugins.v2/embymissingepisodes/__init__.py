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
    Emby 剧集缺集检查插件
    - UI 重构：参照官方稳定插件彻底抛弃 VDataTable，改用原生 VTable/tr/td 手工构建 DOM，解决白屏问题。
    - 维持双轨内存拉取与动态边界 TMDB 差集算法。
    """

    plugin_name = "Emby剧集缺集检查"
    plugin_desc = "利用 TMDB 溯源精准比对并查找 Emby 库中的缺失剧集。"
    plugin_icon = ""
    plugin_version = "1.0.0"
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
        """
        抛弃 VDataTable，使用稳定底层的 VTable 与循环 tr td 渲染数据
        """
        self._load_saved_data()
        csv_download_href = self._build_csv_base64_href()
        status_text = f"后台扫描中..." if self._is_scanning else f"上次扫描: {self._last_scan_time}"

        # 1. 顶部操作栏 (下载按钮)
        btn_row = {
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
        }

        # 2. 如果无数据，直接返回提示
        if not self._cache_missing_results:
            empty_row = {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                'component': 'div',
                                'text': '暂无缺失数据或尚未运行扫描。请配置 TMDB API Key 后触发扫描。',
                                'props': {'class': 'text-center mt-4'}
                            }
                        ]
                    }
                ]
            }
            return [btn_row, empty_row]

        # 3. 手工构造 tbody -> tr -> td 内容树
        contents = [
            {
                'component': 'tr',
                'props': {'class': 'text-sm'},
                'content': [
                    {
                        'component': 'td',
                        'props': {'class': 'whitespace-nowrap break-keep text-high-emphasis'},
                        'text': str(item.get("SeriesName", ""))
                    },
                    {
                        'component': 'td',
                        'text': str(item.get("SeasonFormatted", ""))
                    },
                    {
                        'component': 'td',
                        'text': str(item.get("MissingEpisodes", ""))
                    }
                ]
            } for item in self._cache_missing_results
        ]

        # 4. 组装 VTable 外壳
        table_row = {
            'component': 'VRow',
            'content': [
                {
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [
                        {
                            'component': 'VTable',
                            'props': {'hover': True},
                            'content': [
                                {
                                    'component': 'thead',
                                    'content': [
                                        {
                                            'component': 'th',
                                            'props': {'class': 'text-start ps-4'},
                                            'text': '剧集名称'
                                        },
                                        {
                                            'component': 'th',
                                            'props': {'class': 'text-start ps-4'},
                                            'text': '缺失季度'
                                        },
                                        {
                                            'component': 'th',
                                            'props': {'class': 'text-start ps-4'},
                                            'text': '缺失集号'
                                        },
                                    ]
                                },
                                {
                                    'component': 'tbody',
                                    'content': contents
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        return [btn_row, table_row]

    def _format_episode_ranges(self, episodes: set) -> str:
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

    def _process_single_series(self, series: dict, global_inventory: dict, tmdb_key: str, tmdb_domain: str, today: str) -> List[Dict]:
        series_id = series.get("Id")
        series_name = series.get("Name", "未知剧集")
        
        provider_ids = series.get("ProviderIds") or {}
        tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("tmdb") or provider_ids.get("TMDB")
        
        if not tmdb_id:
            return []

        local_inventory = global_inventory.get(series_id, {})
        
        try:
            tmdb_series_url = f"https://{tmdb_domain}/3/tv/{tmdb_id}?language=zh-CN&api_key={tmdb_key}"
            res_series = RequestUtils().get_res(tmdb_series_url)
            
            if not res_series or res_series.status_code != 200:
                return []
                
            tmdb_seasons = res_series.json().get("seasons", [])
        except Exception as e:
            logger.debug(f"【EmbyMissingEpisodes】获取剧集 {series_name} 失败: {str(e)}")
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
                tmdb_ep_url = f"https://{tmdb_domain}/3/tv/{tmdb_id}/season/{s_num}?language=zh-CN&api_key={tmdb_key}"
                res_ep = RequestUtils().get_res(tmdb_ep_url)
                
                if not res_ep or res_ep.status_code != 200:
                    continue
                    
                tmdb_episodes = res_ep.json().get("episodes", [])
            except Exception as e:
                logger.debug(f"【EmbyMissingEpisodes】获取剧集 {series_name} 第 {s_num} 季失败: {str(e)}")
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

            today = datetime.now().strftime("%Y-%m-%d")

            tmdb_domain = getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org")
            tmdb_domain = tmdb_domain.replace("https://", "").replace("http://", "").strip("/") if tmdb_domain else "api.themoviedb.org"

            logger.info(f"【EmbyMissingEpisodes】步骤 1/4: 测试 TMDB API 连通性 ({tmdb_domain})...")
            test_url = f"https://{tmdb_domain}/3/configuration?api_key={self._tmdb_api_key}"
            
            try:
                test_res = RequestUtils().get_res(test_url)
                if not test_res or test_res.status_code != 200:
                    logger.error(f"【EmbyMissingEpisodes】⛔ TMDB 连通性测试失败！状态码: {test_res.status_code if test_res else 'None'}。")
                    return
            except Exception as e:
                logger.error(f"【EmbyMissingEpisodes】⛔ TMDB 网络阻断或超时: {e}")
                return
                
            logger.info("【EmbyMissingEpisodes】✅ TMDB 连通性测试通过！")

            logger.info("【EmbyMissingEpisodes】步骤 2/4: 正在拉取剧集外壳...")
            all_series_url = (
                f"{host}/emby/Users/{user_id}/Items?"
                f"Recursive=true&IncludeItemTypes=Series&Fields=ProviderIds&api_key={api_key}"
            )
            res_series = RequestUtils().get_res(all_series_url)
            if not res_series or res_series.status_code != 200:
                logger.error("【EmbyMissingEpisodes】无法获取 Emby 剧集列表。")
                return
            all_series = res_series.json().get("Items", [])

            logger.info("【EmbyMissingEpisodes】步骤 3/4: 正在构建全局单集内存缓冲池 (完美兼容合集解析)...")
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
                s_num_raw = ep.get("ParentIndexNumber")
                e_num_raw = ep.get("IndexNumber")
                e_end_raw = ep.get("IndexNumberEnd")
                
                try:
                    s_num = int(s_num_raw)
                    e_num = int(e_num_raw)
                    e_end = int(e_end_raw) if e_end_raw is not None else e_num
                except (ValueError, TypeError):
                    continue

                if not ser_id: 
                    continue
                
                for i in range(e_num, e_end + 1):
                    global_inventory[ser_id][s_num].add(i)

            logger.info(f"【EmbyMissingEpisodes】步骤 4/4: 内存池就绪，开启 8 线程并发 TMDB 比对 (共 {len(all_series)} 部剧集)...")

            final_missing_results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(self._process_single_series, s, global_inventory, self._tmdb_api_key, tmdb_domain, today) 
                    for s in all_series
                ]
                
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res:
                        final_missing_results.extend(res)

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
