import base64
import csv
import io
import os
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


class EmbyDuplicateMedia(_PluginBase):
    """
    Emby 分散多版本媒体检查插件 v2.0.0
    - 扫描 Emby 库中未被自动合并、独立分散存在的电影和电视剧记录
    - 支持 TMDB ID、TVDB ID、IMDb ID 以及【名称+年份】三维精准归类比对
    - 使用原生 VTable 渲染页面，彻底解决前端白屏兼容问题
    - 纯前端 Base64 CSV 无感导出，自带 UTF-8 BOM，Excel 打开无乱码
    """

    # ⚙️ 分页拉取单页大小（固定 1000 条）
    PAGE_SIZE: int = 1000

    plugin_name = "Emby分散多版本媒体检查"
    plugin_desc = "查找 Emby 中未被自动合并、分散记录的重复/多版本电影与电视剧。"
    plugin_icon = ""
    plugin_version = "2.0.0"
    plugin_author = "LunaticXJ"
    author_url = "https://github.com/LunaticXJ"
    plugin_config_prefix = "embyduplicatemedia_"
    plugin_order = 17
    auth_level = 1

    _onlyonce = False
    _mediaserver = ""
    _media_types = ["Movie", "Series"]

    _STORAGE_DATA_KEY = "duplicate_media_data"
    _STORAGE_TIME_KEY = "duplicate_media_last_time"

    _cache_results: List[Dict[str, Any]] = []
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
            self._media_types = config.get("media_types") or ["Movie", "Series"]

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("【EmbyDuplicateMedia】检测到“立即运行一次”，将在 3 秒后执行多版本影视扫描...")
                self._scheduler.add_job(
                    self.scan_duplicate_media,
                    'date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="Emby分散多版本影视扫描"
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
                self._cache_results = saved_data
            if saved_time:
                self._last_scan_time = str(saved_time)
        except Exception as e:
            logger.error(f"【EmbyDuplicateMedia】读取持久化数据失败: {e}")

    def get_state(self) -> bool:
        return True

    def __update_config(self):
        self.update_config({
            "onlyonce": self._onlyonce,
            "mediaserver": self._mediaserver,
            "media_types": self._media_types,
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
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "model": "media_types",
                                            "label": "检查媒体类型",
                                            "items": [
                                                {"title": "电影", "value": "Movie"},
                                                {"title": "电视剧", "value": "Series"}
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                            },
                        ],
                    },
                ],
            }
        ], {
            "onlyonce": False,
            "mediaserver": "",
            "media_types": ["Movie", "Series"],
        }

    def _build_csv_base64_href(self) -> str:
        """生成带 UTF-8 BOM 标头的 Base64 CSV 导出流"""
        output = io.StringIO()
        output.write('\ufeff')
        writer = csv.writer(output)
        writer.writerow(["ID", "媒体类型", "名称", "年份", "重复类别", "版本数量", "版本详情"])

        for row in self._cache_results:
            writer.writerow([
                row.get("ID", ""),
                row.get("TypeLabel", ""),
                row.get("Name", ""),
                row.get("Year", ""),
                row.get("Category", ""),
                row.get("VersionCount", ""),
                row.get("Details", "")
            ])

        csv_bytes = output.getvalue().encode('utf-8')
        base64_str = base64.b64encode(csv_bytes).decode('utf-8')
        return f"data:text/csv;charset=utf-8;base64,{base64_str}"

    def get_page(self) -> List[dict]:
        """使用底层原生的 VTable 渲染看板页面，防止前端白屏"""
        self._load_saved_data()
        csv_download_href = self._build_csv_base64_href()
        status_text = "后台扫描中..." if self._is_scanning else f"上次扫描: {self._last_scan_time}"

        # 顶部操作工具栏
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
                                "download": f"Emby多版本影视清单_{datetime.now().strftime('%Y%m%d%H%M')}.csv",
                                "variant": "tonal",
                                "prepend-icon": "mdi-download",
                            },
                            "text": f"导出 CSV ({status_text})"
                        }
                    ]
                }
            ]
        }

        if not self._cache_results:
            empty_row = {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                'component': 'div',
                                'text': '暂无多版本影视数据或尚未运行扫描。请选择 Emby 服务器后勾选【保存并立即运行一次】。',
                                'props': {'class': 'text-center mt-4'}
                            }
                        ]
                    }
                ]
            }
            return [btn_row, empty_row]

        # 构造原生 tbody -> tr -> td 数据列
        contents = [
            {
                'component': 'tr',
                'props': {'class': 'text-sm'},
                'content': [
                    {
                        'component': 'td',
                        'props': {'class': 'whitespace-nowrap text-high-emphasis'},
                        'text': str(item.get("TypeLabel", ""))
                    },
                    {
                        'component': 'td',
                        'props': {'class': 'whitespace-nowrap text-high-emphasis'},
                        'text': str(item.get("Name", ""))
                    },
                    {
                        'component': 'td',
                        'text': str(item.get("Year", ""))
                    },
                    {
                        'component': 'td',
                        'text': str(item.get("Category", ""))
                    },
                    {
                        'component': 'td',
                        'text': str(item.get("VersionCount", ""))
                    },
                    {
                        'component': 'td',
                        'text': str(item.get("Details", ""))
                    }
                ]
            } for item in self._cache_results
        ]

        # 构造原生 VTable 外壳
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
                                            'text': '媒体类型'
                                        },
                                        {
                                            'component': 'th',
                                            'props': {'class': 'text-start ps-4'},
                                            'text': '名称'
                                        },
                                        {
                                            'component': 'th',
                                            'props': {'class': 'text-start ps-4'},
                                            'text': '年份'
                                        },
                                        {
                                            'component': 'th',
                                            'props': {'class': 'text-start ps-4'},
                                            'text': '重复类别'
                                        },
                                        {
                                            'component': 'th',
                                            'props': {'class': 'text-start ps-4'},
                                            'text': '版本数量'
                                        },
                                        {
                                            'component': 'th',
                                            'props': {'class': 'text-start ps-4'},
                                            'text': '版本详情 (文件名与格式)'
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

    @staticmethod
    def _get_resolution(source: dict) -> str:
        """多层级精准获取视频分辨率与轨道参数"""
        w = source.get("Width")
        h = source.get("Height")

        if not w or not h:
            for stream in source.get("MediaStreams", []):
                if stream.get("Type") == "Video":
                    w = w or stream.get("Width")
                    h = h or stream.get("Height")
                    break

        if w and h and str(w) != "?" and str(h) != "?":
            return f"{w}x{h}"
        return "未知分辨率"

    def scan_duplicate_media(self):
        """执行后台流式扫描任务"""
        if self._is_scanning:
            logger.warn("【EmbyDuplicateMedia】上次扫描尚未结束，跳过本次触发。")
            return

        self._is_scanning = True
        start_time = datetime.now()
        logger.info("【EmbyDuplicateMedia】开始全量扫描 Emby 影视库比对分散版本...")

        try:
            if not self._mediaserver:
                logger.error("【EmbyDuplicateMedia】未选择 Emby 媒体服务器！")
                return

            emby_servers = self.mediaserver_helper.get_services(name_filters=[self._mediaserver], type_filter="emby")
            if not emby_servers:
                logger.error(f"【EmbyDuplicateMedia】未找到匹配的 Emby 服务器。")
                return

            emby_server = list(emby_servers.values())[0]
            duplicates = self._scan_server(emby_server)

            self._cache_results = duplicates
            self._last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self.save_data(self._STORAGE_DATA_KEY, self._cache_results)
            self.save_data(self._STORAGE_TIME_KEY, self._last_scan_time)

            elapsed = (datetime.now() - start_time).total_seconds()
            logger.info(f"【EmbyDuplicateMedia】>>> 扫描完成！耗时 {elapsed:.1f} 秒，检出 {len(duplicates)} 组分散多版本影视。")
        except Exception as e:
            logger.error(f"【EmbyDuplicateMedia】扫描过程出现异常: {e}", exc_info=True)
        finally:
            self._is_scanning = False

    def _scan_server(self, emby_server) -> List[Dict[str, Any]]:
        raw_host = emby_server.config.config.get("host") or ""
        host = raw_host.rstrip('/')
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        if not host or not api_key or not user_id:
            logger.error("【EmbyDuplicateMedia】Emby API 凭证或地址配置不完整！")
            return []

        # 拼装包含类型
        types = [t for t in self._media_types if t in ["Movie", "Series"]]
        if not types:
            types = ["Movie", "Series"]
        include_types = ",".join(types)

        provider_dict = defaultdict(list)
        name_dict = defaultdict(list)

        start_index = 0
        limit = self.PAGE_SIZE

        while True:
            url = (
                f"{host}/emby/Users/{user_id}/Items?"
                f"Recursive=true&IncludeItemTypes={include_types}"
                f"&Fields=MediaSources,ProductionYear,ProviderIds,Path,Type"
                f"&StartIndex={start_index}&Limit={limit}"
                f"&api_key={api_key}"
            )
            res = RequestUtils(timeout=15).get_res(url)
            if not res or res.status_code != 200:
                logger.error(f"【EmbyDuplicateMedia】请求 Emby 影视列表失败 (StartIndex={start_index})")
                break

            data = res.json()
            items = data.get("Items", [])
            if not items:
                break

            for item in items:
                item_type = str(item.get("Type") or "Movie")
                type_label = "电视剧" if item_type == "Series" else "电影"

                movie_id = str(item.get("Id", ""))
                movie_name = str(item.get("Name", "未知"))
                movie_year = str(item.get("ProductionYear") or "未知")
                p_ids = item.get("ProviderIds") or {}
                path = str(item.get("Path") or "")

                media_sources = item.get("MediaSources") or []
                info_list = []
                for src in media_sources:
                    resolution = self._get_resolution(src)
                    container = str(src.get("Container") or "?").upper()
                    info_list.append(f"[{container} | {resolution}]")

                version_str = " | ".join(info_list) if info_list else ""

                movie_info = {
                    "ID": movie_id,
                    "ItemType": item_type,
                    "TypeLabel": type_label,
                    "Name": movie_name,
                    "Year": movie_year,
                    "Details": version_str,
                    "Path": path,
                }

                tmdb_id = p_ids.get("Tmdb") or p_ids.get("tmdb") or p_ids.get("TMDB")
                tvdb_id = p_ids.get("Tvdb") or p_ids.get("tvdb") or p_ids.get("TVDB")
                imdb_id = p_ids.get("Imdb") or p_ids.get("imdb") or p_ids.get("IMDB")

                # 比对维度划分
                if tmdb_id:
                    provider_dict[(item_type, "TMDB", str(tmdb_id))].append(movie_info)
                elif tvdb_id and item_type == "Series":
                    provider_dict[(item_type, "TVDB", str(tvdb_id))].append(movie_info)
                elif imdb_id:
                    provider_dict[(item_type, "IMDB", str(imdb_id))].append(movie_info)
                else:
                    name_dict[(item_type, movie_name, movie_year)].append(movie_info)

            total_record_count = data.get("TotalRecordCount", 0)
            start_index += len(items)

            if start_index >= total_record_count:
                break

        final_list = []

        # 1. 处理 TMDB/TVDB/IMDb ID 一致的分散组
        for (item_type, pid_type, pid), group in provider_dict.items():
            if len(group) > 1:
                all_ids = [m["ID"] for m in group]
                all_names = list(dict.fromkeys([m["Name"] for m in group]))

                all_details = []
                for m in group:
                    file_name = os.path.basename(m["Path"]) if m["Path"] else m["Name"]
                    detail_suffix = f": {m['Details']}" if m['Details'] else ""
                    all_details.append(f"{file_name}{detail_suffix}")

                category_label = f"{pid_type} ID一致"

                final_list.append({
                    "ID": ", ".join(all_ids),
                    "TypeLabel": group[0]["TypeLabel"],
                    "Name": " / ".join(all_names),
                    "Year": group[0]["Year"],
                    "Category": category_label,
                    "VersionCount": str(len(group)),
                    "Details": " ‖ ".join(all_details),
                })

        # 2. 处理无唯一 ID 但【名称 + 发行年份】一致的分散组
        for (item_type, name, year), group in name_dict.items():
            if len(group) > 1:
                all_ids = [m["ID"] for m in group]

                all_details = []
                for m in group:
                    file_name = os.path.basename(m["Path"]) if m["Path"] else m["Name"]
                    detail_suffix = f": {m['Details']}" if m['Details'] else ""
                    all_details.append(f"{file_name}{detail_suffix}")

                final_list.append({
                    "ID": ", ".join(all_ids),
                    "TypeLabel": group[0]["TypeLabel"],
                    "Name": name,
                    "Year": year,
                    "Category": "名称年份一致（无唯一ID）",
                    "VersionCount": str(len(group)),
                    "Details": " ‖ ".join(all_details),
                })

        return final_list

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"【EmbyDuplicateMedia】停止服务失败: {e}")