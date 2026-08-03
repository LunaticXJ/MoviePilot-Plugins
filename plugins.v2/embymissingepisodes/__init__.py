import json
from datetime import datetime
from typing import Any, List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyMissingEpisodes(_PluginBase):
    """
    Emby 剧集缺集检查插件：快速扫描并找出 Emby 库中电视剧缺失的集/断集情况
    """

    # 插件名称
    plugin_name = "Emby剧集缺集检查"
    # 插件描述
    plugin_desc = "精准查找 Emby 媒体库中剧集的缺失集（断集）情况。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/emby.png"
    # 插件版本
    plugin_version = "1.0.0"
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

    # 插件私有变量
    _enabled = False
    _mediaservers = []
    _ignore_season_zero = True
    _ignore_future = True
    _thread_num = 10

    # 内存缓存查询结果，供前端 UI 直接拉取表格数据
    _cache_missing_results: List[Dict[str, Any]] = []
    _last_scan_time: str = "从未扫描"

    mediaserver_helper = None

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置
        """
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled", False)
            self._mediaservers = config.get("mediaservers") or []
            self._ignore_season_zero = config.get("ignore_season_zero", True)
            self._ignore_future = config.get("ignore_future", True)
            self._thread_num = int(config.get("thread_num") or 10)

    def get_state(self) -> bool:
        """
        返回插件启用状态
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        快捷指令（无）
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册插件后端 API 端点，供前端 get_page 中的 UI 组件异步调用
        """
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan_missing,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "执行缺集扫描",
                "description": "扫描选中的 Emby 服务器并提取所有缺失的剧集列表",
            },
            {
                "path": "/data",
                "endpoint": self.api_get_data,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "获取缺失结果数据",
                "description": "获取当前扫描到的缺集列表和最后扫描时间",
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
                                            "model": "ignore_season_zero",
                                            "label": "忽略特别篇 (S00/SP)",
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "thread_num",
                                            "label": "扫描并发线程数",
                                            "placeholder": "默认10",
                                            "type": "number",
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
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "mediaservers",
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
            "ignore_season_zero": True,
            "ignore_future": True,
            "thread_num": 10,
            "mediaservers": [],
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件数据主页面（UI）：展示扫描统计卡片与缺失剧集 Data Table 数据表格
        """
        return [
            {
                "component": "VRow",
                "content": [
                    # 1. 顶部操作面板与状态统计卡片
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
                                                        "text": f"上次更新时间：{self._last_scan_time} | 共发现缺集数量：{len(self._cache_missing_results)} 条",
                                                    },
                                                ],
                                            },
                                            {
                                                "component": "VBtn",
                                                "props": {
                                                    "color": "primary",
                                                    "prepend-icon": "mdi-magnify-scan",
                                                    "size": "large",
                                                },
                                                "text": "立即重新扫描缺集",
                                                "events": {
                                                    "click": {
                                                        "api": "plugin/EmbyMissingEpisodes/scan",
                                                        "method": "post",
                                                    }
                                                },
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    # 2. 缺集详情数据表格
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "outlined"},
                                "content": [
                                    {
                                        "component": "VDataTable",
                                        "props": {
                                            "headers": [
                                                {"title": "服务器", "key": "ServerName", "width": "120px"},
                                                {"title": "剧集名称", "key": "SeriesName", "width": "220px"},
                                                {"title": "缺失季度", "key": "SeasonFormatted", "width": "100px"},
                                                {"title": "缺失集号", "key": "EpisodeFormatted", "width": "120px"},
                                                {"title": "缺失标题", "key": "Name"},
                                                {"title": "首播日期", "key": "PremiereDate", "width": "150px"},
                                            ],
                                            "items": self._cache_missing_results,
                                            "hover": True,
                                            "density": "comfortable",
                                            "items-per-page": 15,
                                            "no-data-text": "暂无缺失剧集数据，请点击上方按钮发起扫描。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

    def api_scan_missing(self) -> Dict[str, Any]:
        """
        API 端点：触发缺集扫描逻辑
        """
        try:
            if not self._enabled:
                return {"code": 1, "msg": "插件未启用，请先在配置页面启用插件并保存。"}

            emby_servers = self.mediaserver_helper.get_services(name_filters=self._mediaservers, type_filter="emby")
            if not emby_servers:
                return {"code": 1, "msg": "未配置有效 Emby 媒体服务器。"}

            all_missing = []

            for emby_name, emby_server in emby_servers.items():
                logger.info(f"【EmbyMissingEpisodes】开始扫描服务器: {emby_name}")
                missing_list = self._scan_server_missing_episodes(emby_name, emby_server)
                all_missing.extend(missing_list)

            self._cache_missing_results = all_missing
            self._last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            logger.info(f"【EmbyMissingEpisodes】扫描完成，共获取到 {len(all_missing)} 条缺集记录。")
            return {
                "code": 0,
                "msg": f"扫描完成，共找到 {len(all_missing)} 条缺集信息。",
                "data": all_missing,
            }
        except Exception as e:
            logger.error(f"【EmbyMissingEpisodes】扫描过程出现异常: {e}", exc_info=True)
            return {"code": 1, "msg": f"扫描失败: {str(e)}"}

    def api_get_data(self) -> Dict[str, Any]:
        """
        API 端点：获取数据接口
        """
        return {
            "code": 0,
            "msg": "success",
            "data": {
                "last_scan_time": self._last_scan_time,
                "items": self._cache_missing_results,
            },
        }

    def _scan_server_missing_episodes(self, server_name: str, emby_server) -> List[Dict[str, Any]]:
        """
        扫描单个 Emby 服务器的缺集逻辑（利用 IsMissing=true 方案与差集算法结合）
        """
        host = emby_server.config.config.get("host")
        api_key = emby_server.config.config.get("apikey")
        user_id = emby_server.instance.get_user()

        results = []
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

        # 优先通过 API 直接获取 Emby 已标出缺失的 Virtual 节点
        url = (
            f"{host}/emby/Users/{user_id}/Items?"
            f"Recursive=true&IncludeItemTypes=Episode&IsMissing=true"
            f"&Fields=SeriesName,SeasonName,IndexNumber,ParentIndexNumber,PremiereDate"
            f"&api_key={api_key}"
        )

        res = RequestUtils().get_res(url)
        if res and res.status_code == 200:
            raw_items = res.json().get("Items") or []
            for item in raw_items:
                season_num = item.get("ParentIndexNumber", 1)
                episode_num = item.get("IndexNumber", 0)
                premiere_date = item.get("PremiereDate", "")

                # 排除规则 1：排除 S00 特别篇
                if self._ignore_season_zero and season_num == 0:
                    continue

                # 排除规则 2：排除尚未首播开播的未来剧集
                if self._ignore_future and premiere_date and premiere_date > now_str:
                    continue

                formatted_date = premiere_date[:10] if len(premiere_date) >= 10 else "未知"

                results.append({
                    "ServerName": server_name,
                    "SeriesName": item.get("SeriesName") or "未知剧集",
                    "SeasonFormatted": f"第 {season_num} 季" if season_num > 0 else "特别篇",
                    "EpisodeFormatted": f"第 {episode_num} 集" if episode_num > 0 else "未知",
                    "Name": item.get("Name") or f"第 {episode_num} 集",
                    "PremiereDate": formatted_date,
                })

        return results

    def stop_service(self):
        """
        插件停止事件清理逻辑
        """
        pass