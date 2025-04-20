import json
import os
import shutil
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from re import compile as re_compile
from typing import Any, Final, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.plugins.strmgenerator.cloud115helper import Cloud115Helper
from app.utils.system import SystemUtils

CRE_SET_COOKIE: Final = re_compile(r"[0-9a-f]{32}=[0-9a-f]{32}.*")
lock = threading.Lock()


class StrmGenerator(_PluginBase):
    # 插件名称
    plugin_name = "115云盘Strm助手"
    # 插件描述
    plugin_desc = "115云盘全量快速生成工具，并可实时处理MP新整理入库的文件。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/LunaticXJ/MoviePilot-Plugins/main/icons/115strm.png"
    # 插件版本
    plugin_version = "3.1.1"
    # 插件作者
    plugin_author = "LunaticXJ"
    # 作者主页
    author_url = "https://github.com/LunaticXJ"
    # 插件配置项ID前缀
    plugin_config_prefix = "strmGenerator_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _monitor_confs = None
    _onlyonce = False
    _rebuild = False
    _cover = False
    _monitor = False
    _uriencode = False
    _strm_dir_conf = {}
    _cloud_dir_conf = {}
    _format_conf = {}
    _cloud_files = set()
    _dirty = False  # _cloud_files是否有未写入json文件的脏数据
    _rmt_mediaext = ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
    _115_cookie = None
    _path_replacements = {}  # Strm文件内容的路径替换规则
    _cloud_files_json = "cloud_files.json"
    _cloud_115_helper = None
    _rmt_mediaext_set = set()

    _sync_type: str = ""
    _sync_del = False
    _cloud_del_paths = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        logger.debug(f"初始化插件 {self.plugin_name}")
        self._strm_dir_conf = {}
        self._cloud_dir_conf = {}
        self._format_conf = {}
        self._path_replacements = {}
        self._cloud_files_json = os.path.join(
            self.get_data_path(), self._cloud_files_json
        )

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._rebuild = config.get("rebuild")
            self._monitor = config.get("monitor")
            self._cover = config.get("cover")
            self._uriencode = config.get("uriencode")
            self._monitor_confs = config.get("monitor_confs")
            self._rmt_mediaext = config.get("rmt_mediaext") or self._rmt_mediaext
            self._rmt_mediaext_set = {
                ext.strip().lower() for ext in self._rmt_mediaext.split(",")
            }
            if config.get("path_replacements"):
                for replacement in str(config.get("path_replacements")).split("\n"):
                    if replacement and ":" in replacement:
                        source, target = replacement.split(":", 1)
                        self._path_replacements[source.strip()] = target.strip()
            self._115_cookie = config.get("115_cookie")
            self._cloud_115_helper = Cloud115Helper(cookie=self._115_cookie)

            self._sync_type = config.get("sync_type")
            self._sync_del = config.get("sync_del")
            if config.get("del_paths"):
                for path in str(config.get("del_paths")).split("\n"):
                    self._cloud_del_paths[path.split(":")[0]] = path.split(":")[1]

        # 停止现有任务
        self.stop_service()

        # 清理索引，可单独执行（不依赖启用或全量运行一次开关）
        if self._rebuild:
            logger.info("开始清理文件索引")
            self._rebuild = False
            self._cloud_files = set()
            if Path(self._cloud_files_json).exists():
                Path(self._cloud_files_json).unlink()
            logger.info("文件索引清理完成")
            self.__update_config()
        elif Path(self._cloud_files_json).exists():
            logger.info("加载文件索引")
            with open(self._cloud_files_json, "r") as file:
                content = file.read()
                if content:
                    self._cloud_files = set(json.loads(content))

        # 当插件的启用或全量运行一次开关打开时
        if self._enabled or self._onlyonce:
            # 初始化调度器
            if not self._scheduler:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 查看是否存在已有任务
            existing_jobs = self._scheduler.get_jobs()
            job_names = [job.name for job in existing_jobs]
            logger.info(f"当前存在调度任务: {job_names}")

            # 添加定时保存JSON文件任务
            if "保存JSON文件" not in job_names:
                self._scheduler.add_job(
                    self.__save_json_if_dirty,
                    trigger="interval",
                    seconds=30,
                    name="保存JSON文件",
                )

            # 读取目录配置，每行格式：MP中云盘挂载本地的路径#MoviePilot中strm生成路径#alist/cd2上115路径#strm格式化
            monitor_confs = self._monitor_confs.split("\n")
            if not monitor_confs:
                return
            for monitor_conf in monitor_confs:
                # 忽略空行和注释行
                if not monitor_conf or str(monitor_conf).startswith("#"):
                    continue

                # 处理配置
                if str(monitor_conf).count("#") == 3:
                    local_dir = str(monitor_conf).split("#")[0]
                    strm_dir = str(monitor_conf).split("#")[1]
                    cloud_dir = str(monitor_conf).split("#")[2]
                    format_str = str(monitor_conf).split("#")[3]
                else:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue
                # 存储目录监控配置
                self._strm_dir_conf[local_dir] = strm_dir
                self._cloud_dir_conf[local_dir] = cloud_dir
                self._format_conf[local_dir] = format_str

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("添加Strm全量扫描一次性任务")
                self._scheduler.add_job(
                    func=self.full_scan,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ))
                    + timedelta(seconds=3),
                )

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs() and not self._scheduler.running:
                self._scheduler.start()
                logger.info("调度器启动")

    # 实时监控MP整理事件，增量生成strm文件
    @eventmanager.register(EventType.TransferComplete)
    def strm_one(self, event: Event = None):
        if event and self._monitor:
            event_data = event.event_data
            file_path = event_data["transferinfo"].file_list_new[0]
            if not file_path:
                logger.error(
                    f"收到文件整理完成事件，获取文件路径参数失败：{event_data}"
                )
                return
            logger.info(f"收到文件整理完成事件，文件路径：{file_path}")
            # 遍历所有监控目录
            mon_path = None
            for mon in self._strm_dir_conf.keys():
                if str(file_path).startswith(mon):
                    mon_path = mon
                    break

            if not mon_path:
                logger.error(f"未找到文件 {file_path} 对应的监控目录")
                return

            # 处理单文件
            self.__handle_file(event_path=file_path, mon_path=mon_path)

    def full_scan(self):
        # 记录开始时间
        start_time = time.time()
        if not self._strm_dir_conf or not self._strm_dir_conf.keys():
            logger.error("未获取到可用目录监控配置，请检查")
            return
        if not self._115_cookie:
            logger.error("115_cookie 未配置")
            return
        logger.info("云盘Strm同步生成任务开始")
        # 获取所有云盘文件集合
        all_cloud_files = set()
        dir_file_map = {}  # 存储每个目录对应的文件集合

        # 遍历云盘目录，收集所有文件
        for local_dir in self._cloud_dir_conf.keys():
            cloud_dir = self._cloud_dir_conf.get(local_dir)
            tree_content = self._cloud_115_helper.retrieve_directory_structure(
                cloud_dir
            )
            if not tree_content:
                continue

            # 获取当前目录下的所有文件
            current_files = set(
                cloud_file
                for cloud_file in self._cloud_115_helper.parse_tree_structure(
                    content=tree_content, dir_path=cloud_dir
                )
                if Path(cloud_file).suffix
            )
            all_cloud_files.update(current_files)
            dir_file_map[local_dir] = current_files

        # 使用集合差集找出新增文件
        new_files = all_cloud_files - self._cloud_files
        if not new_files:
            logger.info("没有发现新的云盘文件需要处理")
            return

        # 记录成功处理的文件
        processed_files = set()
        has_changes = False

        # 处理新增文件
        for local_dir in self._cloud_dir_conf.keys():
            cloud_dir = self._cloud_dir_conf.get(local_dir)
            strm_dir = self._strm_dir_conf.get(local_dir)
            format_str = self._format_conf.get(local_dir)

            # 获取当前目录需要处理的新文件
            dir_new_files = new_files & dir_file_map[local_dir]

            for cloud_file in dir_new_files:
                local_file = cloud_file.replace(cloud_dir, local_dir)
                target_file = cloud_file.replace(cloud_dir, strm_dir)

                try:
                    if Path(local_file).suffix.lower() in self._rmt_mediaext_set:
                        strm_content = self.__format_content(
                            format_str=format_str,
                            local_file=local_file,
                            cloud_file=cloud_file,
                            uriencode=self._uriencode,
                        )
                        if self.__create_strm_file(
                            strm_file=target_file, strm_content=strm_content
                        ):
                            has_changes = True
                            processed_files.add(cloud_file)

                except Exception as e:
                    logger.error(
                        f"处理文件 {cloud_file} 失败：{str(e)}- {traceback.format_exc()}"
                    )

        # 更新云盘文件集合并保存
        if has_changes:
            self._cloud_files = all_cloud_files
            self._dirty = True  # 标记为脏数据
            self.__save_json_if_dirty()

        # 计算并记录耗时
        elapsed_time = time.time() - start_time
        elapsed_time_str = None
        if elapsed_time < 60:
            elapsed_time_str = f"{elapsed_time:.2f} 秒"
        else:
            minutes = elapsed_time // 60
            seconds = elapsed_time % 60
            elapsed_time_str = f"{int(minutes)} 分 {seconds:.2f} 秒"
        logger.info(
            f"云盘Strm同步生成任务完成，共处理 {len(processed_files)} 个媒体文件，耗时 {elapsed_time_str}"
        )

    def __handle_file(self, event_path: str, mon_path: str):

        cloud_dir = self._cloud_dir_conf.get(mon_path)
        strm_dir = self._strm_dir_conf.get(mon_path)
        format_str = self._format_conf.get(mon_path)
        target_file = str(event_path).replace(mon_path, strm_dir)
        cloud_file = str(event_path).replace(mon_path, cloud_dir)
        file_suffix = Path(event_path).suffix.lower()

        try:
            if file_suffix in self._rmt_mediaext_set:
                strm_content = self.__format_content(
                    format_str=format_str,
                    local_file=event_path,
                    cloud_file=cloud_file,
                    uriencode=self._uriencode,
                )
                if self.__create_strm_file(
                    strm_file=target_file, strm_content=strm_content
                ):
                    with lock:
                        self._cloud_files.add(cloud_file)
                        self._dirty = True
        except Exception as e:
            logger.error(f"处理单个文件出错：{str(e)} - {traceback.format_exc()}")

    def __save_json_if_dirty(self):
        with lock:
            if self._dirty:
                try:
                    logger.info(f"写入本地索引文件 {self._cloud_files_json}")
                    with open(self._cloud_files_json, "w") as file:
                        json.dump(list(self._cloud_files), file)
                    self._dirty = False
                except Exception as e:
                    logger.error(
                        f"写入本地索引文件失败：{str(e)} - {traceback.format_exc()}"
                    )

    def __format_content(
        self, format_str: str, local_file: str, cloud_file: str, uriencode: bool
    ):
        if "{local_file}" in format_str:
            return format_str.replace("{local_file}", local_file)
        elif "{cloud_file}" in format_str:
            if uriencode:
                # 对盘符之后的所有内容进行url转码
                cloud_file = urllib.parse.quote(cloud_file, safe="")
            else:
                # 替换路径中的\为/
                cloud_file = cloud_file.replace("\\", "/")
            return format_str.replace("{cloud_file}", cloud_file)
        else:
            return None

    def __create_strm_file(self, strm_file: str, strm_content: str):
        try:
            # 创建Strm文件存储文件夹
            if not Path(strm_file).parent.exists():
                os.makedirs(Path(strm_file).parent)

            # 构造Strm文件路径
            strm_file = os.path.join(
                Path(strm_file).parent,
                f"{os.path.splitext(Path(strm_file).name)[0]}.strm",
            )

            # 目标Strm文件若存在且不覆盖，则跳过
            if Path(strm_file).exists() and not self._cover:
                logger.info(f"目标strm文件已存在，跳过: {strm_file} ")
                return True
            # 应用Strm内容路径替换规则
            for source, target in self._path_replacements.items():
                if source in strm_content:
                    strm_content = strm_content.replace(source, target)
                    logger.debug(f"应用路径替换规则: {source} -> {target}")

            # 创建strm文件
            with open(strm_file, "w", encoding="utf-8") as f:
                f.write(strm_content)
            logger.info(f"创建strm文件成功: {strm_file}")
            return True
        except Exception as e:
            logger.error(
                f"创建strm文件{strm_file}失败：{str(e)} - {traceback.format_exc()}"
            )
        return False

    @eventmanager.register(EventType.WebhookMessage)
    def sync_del(self, event):
        if not self._sync_del or not self._sync_type:
            return
        event_data = event.event_data
        event_type = event_data.event
        # ScripterX插件 event_type = media_del，Emby Webhook event_type = library.deleted
        if not event_type or str(event_type) not in ["media_del", "library.deleted"]:
            return
        logger.info(f"收到媒体删除请求：{event.event_data}")
        scripterx_matched = (
            str(self._sync_type) == "scripterx" and event_type == "media_del"
        )
        webhook_matched = (
            str(self._sync_type) == "webhook" and event_type == "library.deleted"
        )

        if scripterx_matched or webhook_matched:
            # Scripter X插件方式，item_isvirtual参数未配置，为防止误删除，暂停插件运行
            if scripterx_matched:
                if not event_data.item_isvirtual:
                    logger.error(
                        "ScripterX插件未配置item_isvirtual参数，为防止误删除不进行处理"
                    )
                    return
                # 如果是虚拟item，则直接return，不进行删除
                if event_data.item_isvirtual == "True":
                    logger.info("ScripterX插件item_isvirtual参数为True，不进行处理")
                    return
            media_path = event_data.item_path
            if not media_path:
                logger.error("未获取到删除媒体路径，不进行处理")
            else:
                self.__clouddisk_del(media_path)
        else:
            logger.error(f"媒体删除请求事件类型与配置不匹配，不做处理")

    def __clouddisk_del(self, media_path: str):
        # 删除云盘文件
        cloud_file = self.__get_path(self._cloud_del_paths, str(media_path))
        if not cloud_file:
            return
        logger.info(
            f"获取到云盘同步删除映射配置：{self._cloud_del_paths}，替换后云盘文件路径 {cloud_file}"
        )

        if Path(cloud_file).suffix:
            cloud_file_path = Path(cloud_file)
            # 删除文件名开头相同的文件
            pattern = f"{cloud_file_path.stem}*"
            files = list(cloud_file_path.parent.glob(pattern))
            logger.info(
                f"筛选到 {cloud_file_path.parent} 下匹配的文件 {pattern} {files}"
            )
            for file in files:
                file.unlink()
                logger.info(f"云盘文件 {file} 已删除")

            # 删除空目录
            # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
            if not SystemUtils.exits_files(
                cloud_file_path.parent, settings.RMT_MEDIAEXT
            ):
                # 判断父目录是否为空, 为空则删除
                i = 0
                for parent_path in cloud_file_path.parents:
                    i += 1
                    if i > 3:
                        break
                    logger.debug(f"开始检查父目录 {parent_path} 是否可删除")
                    if str(parent_path.parent) != str(cloud_file_path.root):
                        # 父目录非根目录，才删除父目录
                        if not SystemUtils.exits_files(
                            parent_path, settings.RMT_MEDIAEXT
                        ):
                            # 当前路径下没有媒体文件则删除
                            shutil.rmtree(parent_path)
                            logger.warn(f"云盘目录 {parent_path} 已删除")
        else:
            if Path(cloud_file).exists():
                shutil.rmtree(cloud_file)
                logger.warn(f"云盘目录 {cloud_file} 已删除")

    def __get_path(self, paths, file_path: str):
        """
        路径转换
        """
        if paths and paths.keys():
            for library_path in paths.keys():
                if str(file_path).startswith(str(library_path)):
                    # 替换网盘路径
                    return str(file_path).replace(
                        str(library_path), str(paths.get(str(library_path)))
                    )
        # 未匹配到路径，返回原路径
        return file_path

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cover": self._cover,
                "rebuild": self._rebuild,
                "monitor": self._monitor,
                "cron": self._cron,
                "monitor_confs": self._monitor_confs,
                "115_cookie": self._115_cookie,
                "rmt_mediaext": self._rmt_mediaext,
                "path_replacements": (
                    "\n".join(
                        [
                            f"{source}:{target}"
                            for source, target in self._path_replacements.items()
                        ]
                    )
                    if self._path_replacements
                    else ""
                ),
            }
        )

    def get_state(self) -> bool:
        return self._enabled or self._monitor or self._sync_del

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [
            {
                "cmd": "/115_full_scan",
                "event": EventType.PluginAction,
                "desc": "115云盘全量扫描",
                "category": "",
                "data": {"action": "115_full_scan"},
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        logger.debug("调用get_service方法33")
        if self._enabled and self._cron:
            return [
                {
                    "id": "115_full_scan",
                    "name": "115云盘全量同步",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.full_scan,
                    "kwargs": {},
                }
            ]
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        logger.debug("调用get_form方法")
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSubheader",
                                        "text": "基础设置",
                                        "props": {
                                            "class": "font-weight-bold",
                                        },
                                    }
                                ],
                            }
                        ],
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
                                            "model": "enabled",
                                            "label": "全量周期同步",
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
                                            "model": "onlyonce",
                                            "label": "全量同步一次",
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
                                            "model": "monitor",
                                            "label": "文件整理实时监控",
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "同步周期",
                                            "placeholder": "0 0 * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "115_cookie",
                                            "label": "115Cookie",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "monitor_confs",
                                            "label": "目录配置",
                                            "rows": 2,
                                            "placeholder": "MoviePilot中云盘挂载本地的路径#MoviePilot中strm生成路径#alist/cd2上115路径#strm格式化",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSubheader",
                                        "text": "额外设置",
                                        "props": {
                                            "class": "font-weight-bold",
                                        },
                                    }
                                ],
                            }
                        ],
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
                                            "model": "cover",
                                            "label": "覆盖已有Strm文件",
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
                                            "model": "rebuild",
                                            "label": "忽略Strm生成记录缓存",
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
                                            "model": "uriencode",
                                            "label": "Strm内容URL编码",
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
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rmt_mediaext",
                                            "label": "视频格式",
                                            "rows": 2,
                                            "placeholder": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_replacements",
                                            "label": "Strm内容路径替换规则（确保emby等媒体服务器可访问）",
                                            "rows": 2,
                                            "placeholder": "源字符串:目标字符串（每行一条规则）",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSubheader",
                                        "text": "同步删除设置",
                                        "props": {
                                            "class": "font-weight-bold",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "sync_del",
                                            "label": "云盘同步删除",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "sync_type",
                                            "label": "媒体库同步方式",
                                            "items": [
                                                {
                                                    "title": "Webhook",
                                                    "value": "webhook",
                                                },
                                                {
                                                    "title": "Scripter X",
                                                    "value": "scripterx",
                                                },
                                            ],
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
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "del_paths",
                                            "rows": "2",
                                            "label": "云盘同步删除路径映射",
                                            "placeholder": "Emby服务器strm访问路径:MoviePilot云盘文件挂载路径（一行一个）",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "MoviePilot中云盘挂载本地的路径：/mnt/media/series/国产剧/雪迷宫 (2024)；"
                                            "MoviePilot中strm生成路径：/mnt/library/series/国产剧/雪迷宫 (2024)；"
                                            "云盘路径：/cloud/media/series/国产剧/雪迷宫 (2024)；"
                                            "则目录配置为：/mnt/media#/mnt/library#/cloud/media#{local_file}",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "strm格式化方式，自行把()替换为alist/cd2上路径："
                                            "1.本地源文件路径：{local_file}。"
                                            "2.alist路径：http://192.168.31.103:5244/d/115{cloud_file}。"
                                            "3.cd2路径：http://192.168.31.103:19798/static/http/192.168.31.103:19798/False/115{cloud_file}。"
                                            "4.其他api路径：http://192.168.31.103:2001/{cloud_file}",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "云盘同步删除路径映射："
                                            "Emby服务器Strm文件访问路径:/data/series/A.strm,"
                                            "MoviePilot云盘文件挂载路径:/mnt/cloud/series/A.mp4。"
                                            "路径映射填/data:/mnt/cloud",
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
            "cron": "",
            "onlyonce": False,
            "rebuild": False,
            "monitor": False,
            "cover": False,
            "uriencode": False,
            "refresh_emby": False,
            "monitor_confs": "",
            "115_cookie": "",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v",
            "path_replacements": "",
            "sync_del": False,
            "sync_type": "",
            "del_paths": "",
            "_tabs": "generator_tab",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown(wait=True)  # 等待线程结束
                    self._event.clear()
                else:
                    logger.info("调度器未运行，无需关闭")
            except Exception as e:
                logger.error(f"停止调度器失败: {str(e)} - {traceback.format_exc()}")
            finally:
                self._scheduler = None
