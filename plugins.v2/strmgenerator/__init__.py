import json
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from posixpath import join as join_path
from re import compile as re_compile
from typing import Any, Final, List, Dict, Mapping, Tuple, Optional

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from urllib.parse import quote
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfoPath
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo
from app.schemas.types import EventType, NotificationType, MediaType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils

CRE_SET_COOKIE: Final = re_compile(r"[0-9a-f]{32}=[0-9a-f]{32}.*")
lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event):
        self.sync.event_handler(event=event, text="创建",
                                mon_path=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.sync.event_handler(event=event, text="移动",
                                mon_path=self._watch_path, event_path=event.dest_path)


class StrmGenerator(_PluginBase):
    # 插件名称
    plugin_name = "Strm生成器"
    # 插件描述
    plugin_desc = "实时/定时监控生成strm文件"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/LunaticXJ/MoviePilot-Plugins/main/icons/cloudcompanion.png"
    # 插件版本
    plugin_version = "3.0.0"
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
    _copy_files = False
    _copy_subtitles = False
    _url = None
    _notify = False
    _refresh_emby = False
    _uriencode = False
    _strm_dir_conf = {}
    _cloud_dir_conf = {}
    _category_conf = {}
    _format_conf = {}
    _cloud_files = set()
    _observer = []
    _medias = {}
    _rmt_mediaext = None
    _other_mediaext = None
    _115_cookie = None
    _interval: int = 10
    _mediaservers = None
    mediaserver_helper = None
    _emby_paths = {}
    _path_replacements = {} # 新增：路径替换规则属性
    _cloud_files_json = "cloud_files.json"
    _headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.192 Safari/537.36",
        "Cookie": "",
    }

    # 新增：预计算的扩展名集合
    _rmt_mediaext_set = set()
    _other_mediaext_set = set()

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    # 退出事件
    _event = threading.Event()


    def __init__(self):
        """
        构造方法，初始化基本属性和扩展名集合
        """
        super().__init__()  # 调用父类 _PluginBase 的构造方法
        
        # 设置默认的媒体扩展名（如果未在配置中指定）
        if not self._rmt_mediaext:
            self._rmt_mediaext = ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
        
        # 预先将扩展名转换为 set，提高查找效率
        self._rmt_mediaext_set = {ext.strip().lower() for ext in self._rmt_mediaext.split(",")}
        self._other_mediaext_set = {ext.strip().lower() for ext in (self._other_mediaext or "").split(",") if ext.strip()}

    def init_plugin(self, config: dict = None):
        logger.debug(f"初始化插件 {self.plugin_name}")  # 添加调试日志
        # 清空配置
        self._strm_dir_conf = {}
        self._cloud_dir_conf = {}
        self._format_conf = {}
        self._category_conf = {}
        self._path_replacements = {}  # 新增：清空路径替换规则        
        self._cloud_files_json = os.path.join(self.get_data_path(), self._cloud_files_json)
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._rebuild = config.get("rebuild")
            self._interval = config.get("interval") or 10
            self._monitor = config.get("monitor")
            self._cover = config.get("cover")
            self._copy_files = config.get("copy_files")
            self._copy_subtitles = config.get("copy_subtitles")
            self._refresh_emby = config.get("refresh_emby")
            self._notify = config.get("notify")
            self._uriencode = config.get("uriencode")
            self._monitor_confs = config.get("monitor_confs")
            self._url = config.get("url")
            self._mediaservers = config.get("mediaservers") or []
            self._other_mediaext = config.get("other_mediaext")

            self._rmt_mediaext = config.get("rmt_mediaext") or self._rmt_mediaext
            # 更新扩展名集合
            self._rmt_mediaext_set = {ext.strip().lower() for ext in self._rmt_mediaext.split(",")}
            self._other_mediaext_set = {ext.strip().lower() for ext in (self._other_mediaext or "").split(",") if ext.strip()}
            
            # 读取路径替换规则
            if config.get("path_replacements"):
                for replacement in str(config.get("path_replacements")).split("\n"):
                    if replacement and ":" in replacement:
                        source, target = replacement.split(":", 1)
                        self._path_replacements[source.strip()] = target.strip()

            self._115_cookie = config.get("115_cookie")
            if self._115_cookie:
                self._headers["Cookie"] = self._115_cookie
            if config.get("emby_path"):
                for path in str(config.get("emby_path")).split(","):
                    self._emby_paths[path.split(":")[0]] = path.split(":")[1]

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
            with open(self._cloud_files_json, 'r') as file:
                content = file.read()
                if content:
                    self._cloud_files = set(json.loads(content))

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            if not self._scheduler:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            # 检查现有任务，避免重复添加
            existing_jobs = self._scheduler.get_jobs()
            job_names = [job.name for job in existing_jobs]
            logger.info(f"当前存在调度任务: {job_names}")

            if self._notify and "发送消息" not in job_names:
                self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15, name="发送消息")

            # 读取目录配置
            monitor_confs = self._monitor_confs.split("\n")
            if not monitor_confs:
                return
            for monitor_conf in monitor_confs:
                # 格式 MoviePilot中云盘挂载本地的路径#MoviePilot中strm生成路径#alist/cd2上115路径#strm格式化
                if not monitor_conf:
                    continue
                # 注释
                if str(monitor_conf).startswith("#"):
                    continue

                monitor = None
                if monitor_conf.count("$") == 1:
                    monitor = str(monitor_conf.split("$")[1])
                    monitor_conf = monitor_conf.split("$")[0]
                category = None
                if monitor_conf.count("@") == 1:
                    category = str(monitor_conf.split("@")[1])
                    monitor_conf = monitor_conf.split("@")[0]
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
                self._category_conf[local_dir] = category
                # 检查媒体库目录是不是下载目录的子目录
                try:
                    if strm_dir and Path(strm_dir).is_relative_to(Path(local_dir)):
                        logger.warn(f"{strm_dir} 是 {local_dir} 的子目录，无法监控")
                        self.systemmessage.put(f"{strm_dir} 是 {local_dir} 的子目录，无法监控")
                        continue
                except Exception as e:
                    logger.debug(str(e))
                    pass

                if not monitor:
                    try:
                        if self._monitor:
                            # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                            observer = PollingObserver(timeout=10)
                            self._observer.append(observer)
                            observer.schedule(FileMonitorHandler(local_dir, self), path=local_dir, recursive=True)
                            observer.daemon = True
                            observer.start()
                            logger.info(f"{local_dir} 的Strm生成实时监控服务启动")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"云盘实时监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                + """
                                                        echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                                        echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                                        sudo sysctl -p
                                                        """)
                        else:
                            logger.error(f"{local_dir} 启动x实时监控失败：{err_msg}")
                        self.systemmessage.put(f"{local_dir} 启动实时监控失败：{err_msg}")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("添加Strm同步一次性任务")
                self._scheduler.add_job(func=self.scan, trigger='date', run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3))

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 可能会与MP框架注册的服务重复
            # if self._cron:
            #     try:
            #         self._scheduler.add_job(func=self.scan, trigger=CronTrigger.from_crontab(self._cron))
            #         logger.info(f"添加Strm同步定时任务: {self._cron}")
            #     except Exception as err:
            #         logger.error(f"定时任务配置错误：{err}")
            #         self.systemmessage.put(f"执行周期配置错误：{err}")

            # 启动任务
            if self._scheduler.get_jobs() and not self._scheduler.running:
                self._scheduler.start()
                logger.info("调度器启动")

    @eventmanager.register(EventType.PluginAction)
    def strm_one(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cloudstrm_file":
                return
            file_path = event_data.get("file_path")
            if not file_path:
                logger.error(f"缺少参数：{event_data}")
                return

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

    @eventmanager.register(EventType.PluginAction)
    def scan(self, event: Event = None):
        """
        云盘全量扫描
        """
        
        # 记录开始时间
        start_time = time.time()
        if not self._strm_dir_conf or not self._strm_dir_conf.keys():
            logger.error("未获取到可用目录监控配置，请检查")
            return
        if not self._115_cookie:
            logger.error("115_cookie 未配置")
            return

        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "StrmGenerator":
                return
            logger.info("收到命令，开始云盘Strm生成 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始云盘Strm生成 ...",
                              userid=event.event_data.get("user"))
        logger.info("云盘Strm同步生成任务开始")
        # 获取所有云盘文件集合
        all_cloud_files = set()
        dir_file_map = {}  # 存储每个目录对应的文件集合
        
        # 遍历云盘目录，收集所有文件
        for local_dir in self._cloud_dir_conf.keys():
            cloud_dir = self._cloud_dir_conf.get(local_dir)
            tree_content = self.retrieve_directory_structure(cloud_dir)
            if not tree_content:
                continue
                
            # 获取当前目录下的所有文件
            current_files = set(
                cloud_file
                for cloud_file in self.parse_tree_structure(content=tree_content, dir_path=cloud_dir)
                if Path(cloud_file).suffix
            )
            all_cloud_files.update(current_files)
            dir_file_map[local_dir] = current_files

        # 使用集合差集找出新增文件
        new_files = all_cloud_files - self._cloud_files
        if not new_files:
            logger.info("没有发现新的云盘文件需要处理")
            if event:
                self.post_message(
                    channel=event.event_data.get("channel"),
                    title="云盘Strm助手同步生成任务完成！(无新文件)",
                    userid=event.event_data.get("user")
                )
            return

        logger.info(f"发现 {len(new_files)} 个新文件需要处理")
        
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
                    success_flag = False
                    file_suffix = Path(local_file).suffix.lower()
                    
                    # 处理媒体文件
                    if file_suffix in self._rmt_mediaext_set:  # 预先定义为 set 的媒体扩展名
                        strm_content = self.__format_content(
                            format_str=format_str,
                            local_file=local_file,
                            cloud_file=cloud_file,
                            uriencode=self._uriencode
                        )
                        success_flag = self.__create_strm_file(
                            strm_file=target_file,
                            strm_content=strm_content
                        )
                    # 处理非媒体文件
                    elif self._copy_files and file_suffix in self._other_mediaext_set:  # 预先定义为 set 的非媒体扩展名
                        os.makedirs(os.path.dirname(target_file), exist_ok=True)
                        shutil.copy2(local_file, target_file)
                        logger.info(f"复制非媒体文件 {local_file} 到 {target_file}")
                        success_flag = True
                    # 处理字幕文件
                    elif self._copy_subtitles and file_suffix in {'.srt', '.ass', '.ssa', '.sub'}:
                        os.makedirs(os.path.dirname(target_file), exist_ok=True)
                        shutil.copy2(local_file, target_file)
                        logger.info(f"复制字幕文件 {local_file} 到 {target_file}")
                        success_flag = True

                    if success_flag:
                        has_changes = True
                        processed_files.add(cloud_file)

                except Exception as e:
                    logger.error(f"处理文件 {cloud_file} 失败：{str(e)}")

        # 更新云盘文件集合并保存
        if has_changes:
            self._cloud_files = all_cloud_files
            self.__save_json()

        # 计算并记录耗时
        elapsed_time = time.time() - start_time
        elapsed_time_str = None
        if elapsed_time < 60:
            elapsed_time_str = f"{elapsed_time:.2f} 秒"
        else:
            minutes = elapsed_time // 60
            seconds = elapsed_time % 60
            elapsed_time_str = f"{int(minutes)} 分 {seconds:.2f} 秒"        
        logger.info(f"云盘Strm同步生成任务完成，共处理 {len(processed_files)} 个文件，耗时 {elapsed_time_str}")


        if event:
            self.post_message(
                channel=event.event_data.get("channel"),
                title="云盘Strm助手同步生成任务完成！",
                userid=event.event_data.get("user")
            )

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param mon_path: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            if '.fuse_hidden' in event_path:
                return

            # 文件发生变化
            logger.debug("监控到文件%s：%s" % (text, event_path))
            self.__handle_file(event_path=event_path, mon_path=mon_path)

    def __handle_file(self, event_path: str, mon_path: str):
        try:
            if not Path(event_path).exists():
                return
            with lock:
                cloud_dir = self._cloud_dir_conf.get(mon_path)
                strm_dir = self._strm_dir_conf.get(mon_path)
                format_str = self._format_conf.get(mon_path)
                target_file = str(event_path).replace(mon_path, strm_dir)
                cloud_file = str(event_path).replace(mon_path, cloud_dir)
                file_suffix = Path(event_path).suffix.lower()

                success_flag = False
                if file_suffix in self._rmt_mediaext_set:
                    strm_content = self.__format_content(
                        format_str=format_str,
                        local_file=event_path,
                        cloud_file=cloud_file,
                        uriencode=self._uriencode
                    )
                    success_flag = self.__create_strm_file(strm_file=target_file, strm_content=strm_content)
                    if success_flag:
                        self._cloud_files.add(cloud_file)  # 更新缓存
                elif self._copy_files and file_suffix in self._other_mediaext_set:
                    os.makedirs(os.path.dirname(target_file), exist_ok=True)
                    shutil.copy2(event_path, target_file)
                    logger.info(f"复制非媒体文件 {event_path} 到 {target_file}")
                    success_flag = True
                elif self._copy_subtitles and file_suffix in {'.srt', '.ass', '.ssa', '.sub'}:
                    os.makedirs(os.path.dirname(target_file), exist_ok=True)
                    shutil.copy2(event_path, target_file)
                    logger.info(f"复制字幕文件 {event_path} 到 {target_file}")
                    success_flag = True

                if success_flag:
                    self.__save_json()  # 保存更新后的缓存
        except Exception as e:
            logger.error(f"目录监控发生错误：{str(e)} - {traceback.format_exc()}")

    def __save_json(self):
        try:
            logger.info(f"写入本地索引文件 {self._cloud_files_json}")
            with open(self._cloud_files_json, 'w') as file:
                json.dump(list(self._cloud_files), file)
        except Exception as e:
            logger.error(f"写入本地索引文件失败：{str(e)}")

    @staticmethod
    def __format_content(format_str: str, local_file: str, cloud_file: str, uriencode: bool):
        """
        格式化strm内容
        """
        if "{local_file}" in format_str:
            return format_str.replace("{local_file}", local_file)
        elif "{cloud_file}" in format_str:
            if uriencode:
                # 对盘符之后的所有内容进行url转码
                cloud_file = urllib.parse.quote(cloud_file, safe='')
            else:
                # 替换路径中的\为/
                cloud_file = cloud_file.replace("\\", "/")
            return format_str.replace("{cloud_file}", cloud_file)
        else:
            return None

    def __create_strm_file(self, strm_file: str, strm_content: str):

        """
        生成strm文件
        :param library_dir:
        :param dest_dir:
        :param dest_file:
        """
        try:
            # 创建目标文件夹
            if not Path(strm_file).parent.exists():
                os.makedirs(Path(strm_file).parent)

            # 构造.strm文件路径
            strm_file = os.path.join(Path(strm_file).parent, f"{os.path.splitext(Path(strm_file).name)[0]}.strm")

            # 目标文件若存在且不覆盖，则跳过
            if Path(strm_file).exists() and not self._cover:
                return True
            # 新增：应用自定义路径替换规则
            for source, target in self._path_replacements.items():
                if source in strm_content:
                    strm_content = strm_content.replace(source, target)
                    logger.debug(f"应用路径替换规则: {source} -> {target}")

            # 写入.strm文件
            with open(strm_file, 'w', encoding='utf-8') as f:
                f.write(strm_content)

            if self._url and Path(strm_content).suffix in settings.RMT_MEDIAEXT:
                RequestUtils(content_type="application/json").post(
                    url=self._url,
                    json={"path": str(strm_content), "type": "add"},
                )

            if self._notify and Path(strm_content).suffix in settings.RMT_MEDIAEXT:
                # 发送消息汇总
                file_meta = MetaInfoPath(Path(strm_file))

                pattern = r'tmdbid=(\d+)'
                # 提取 tmdbid
                match = re.search(pattern, strm_file)
                if match:
                    tmdbid = match.group(1)
                    file_meta.tmdbid = tmdbid

                key = f"{file_meta.cn_name} ({file_meta.year}){f' {file_meta.season}' if file_meta.season else ''}"
                media_list = self._medias.get(key) or {}
                if media_list:
                    episodes = media_list.get("episodes") or []
                    if file_meta.begin_episode:
                        if episodes:
                            if int(file_meta.begin_episode) not in episodes:
                                episodes.append(int(file_meta.begin_episode))
                        else:
                            episodes = [int(file_meta.begin_episode)]
                    media_list = {
                        "episodes": episodes,
                        "file_meta": file_meta,
                        "type": "tv" if file_meta.season else "movie",
                        "time": datetime.now()
                    }
                else:
                    media_list = {
                        "episodes": [int(file_meta.begin_episode)] if file_meta.begin_episode else [],
                        "file_meta": file_meta,
                        "type": "tv" if file_meta.season else "movie",
                        "time": datetime.now()
                    }
                self._medias[key] = media_list

            # 通知emby刷新
            if self._refresh_emby and self._mediaservers:
                time.sleep(0.1)
                self.__refresh_emby_file(strm_file)
            return True
        except Exception as e:
            logger.error(f"创建strm文件失败 {strm_file} -> {str(e)}")
        return False

    def __refresh_emby_file(self, strm_file: str):
        """
        通知emby刷新文件
        """
        emby_servers = self.mediaserver_helper.get_services(name_filters=self._mediaservers, type_filter="emby")
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        strm_file = self.__get_path(paths=self._emby_paths, file_path=strm_file)
        for emby_name, emby_server in emby_servers.items():
            emby = emby_server.instance
            self._EMBY_USER = emby_server.instance.get_user()
            self._EMBY_APIKEY = emby_server.config.config.get("apikey")
            self._EMBY_HOST = emby_server.config.config.get("host")

            logger.info(f"开始通知媒体服务器 {emby_name} 刷新增量文件 {strm_file}")
            try:
                res = emby.post_data(
                    url=f'[HOST]emby/Library/Media/Updated?api_key=[APIKEY]&reqformat=json',
                    data=json.dumps({
                        "Updates": [
                            {
                                "Path": strm_file,
                                "UpdateType": "Created",
                            }
                        ]
                    }),
                    headers={
                        "Content-Type": "application/json"
                    }
                )
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"通知媒体服务器 {emby_name} 刷新新增文件 {strm_file} 失败，错误码：{res.status_code}")
                    return False
            except Exception as err:
                logger.error(f"通知媒体服务器刷新新增文件失败：{str(err)}")
            return False

    def __get_path(self, paths, file_path: str):
        """
        路径转换
        """
        if paths and paths.keys():
            for library_path in paths.keys():
                if str(file_path).startswith(str(library_path)):
                    # 替换网盘路径
                    return str(file_path).replace(str(library_path), str(paths.get(str(library_path))))
        # 未匹配到路径，返回原路径
        return file_path

    def export_dir(self, dir_id, destination_id="0"):
        """
        获取目录导出id
        """
        export_api = "https://webapi.115.com/files/export_dir"
        response = requests.post(url=export_api,
                                 headers=self._headers,
                                 data={"file_ids": dir_id, "target": f"U_1_{destination_id}"})
        if response.status_code == 200:
            result = response.json()
            if result.get("state"):
                export_id = result.get("data", {}).get("export_id")

                logger.info(f"等待目录树导出...")
                retry_cnt = 60
                while retry_cnt > 0:
                    response = requests.get(url=export_api,
                                            headers=self._headers,
                                            data={"export_id": export_id})
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("state"):
                            if str(export_id) == str(result.get("data", {}).get("export_id")):
                                return result.get("data", {}).get("pick_code"), result.get("data", {}).get("file_id")
                    retry_cnt -= 1
                    time.sleep(5)
        return None
    
    def fs_dir_getid(self, path):
        """
        115路径转成ID
        """
        export_api = "https://webapi.115.com/files/getid"
        response = requests.get(url=export_api,
                                 headers=self._headers,
                                 params={"path": path})
        if response.status_code == 200:
            result = response.json()
            if result.get("state"):
                return result.get("id")
        return None
    
    def download_url(self, pickcode):
        """
        获取115文件下载地址
        """
        export_api = "https://webapi.115.com/files/download"
        response = requests.get(url=export_api,
                                 headers=self._headers,
                                 params={"pickcode": pickcode,"dl": 1}) 
        download_url = None
        if response.status_code == 200:
            result = response.json()
            if result.get("state"):
                response = requests.get(result.get("file_url_302"), headers=self._headers)
                if response.status_code == 200:
                    result = response.json()
                    if result.get("state"):
                        download_url = result.get("file_url")
                        # 处理Set-Cookie
                        if isinstance(response.headers, Mapping):
                            match = CRE_SET_COOKIE.search(response.headers["Set-Cookie"])
                            if match is not None:
                                self._headers["Cookie"] += f";{match[0]}"
                        else:
                            for k, v in reversed(response.headers.items()):
                                if k == "Set-Cookie" and CRE_SET_COOKIE.match(v) is not None:
                                    self._headers["Cookie"] += f";{v}"
                                    break
        return download_url
    


    
    def fs_delete(self,fid,pid):
        """
        删除115文件
        """
        export_api = "https://webapi.115.com/rb/delete"
        response = requests.post(url=export_api,
                                 headers=self._headers,
                                 data={"fid[0]": fid, "pid": pid})
        if response.status_code == 200:
            result = response.json()
            if not result.get("state"):
                    logger.info(f"目录树删除失败,{result.get('error')}")

    def retrieve_directory_structure(self, directory_path):
        """
        获取目录树结构
        """
        file_id = None
        try:
            logger.info(f"开始生成 {directory_path} 目录树")
            dir_id = self.fs_dir_getid(directory_path)
            if not dir_id:
                logger.error(f"{directory_path} 目录不存在或路径错误")
                return
            pick_code, file_id = self.export_dir(dir_id)
            if not pick_code:
                logger.error(f"{directory_path} 生成目录树失败")
                return
            # 获取目录树下载链接
            download_url = self.download_url(pick_code)
            directory_content = self.fetch_content(download_url)
            logger.info(f"{directory_path} 目录树下载成功")
            return directory_content
        except Exception as e:
            logger.error(f"{directory_path} 目录树生成失败: {str(e)}")
        finally:
            if file_id:
                try:
                    self.fs_delete(file_id,"U_1_0")
                except:
                    pass

    def fetch_content(self, url):
        """
        下载目录树文件内容
        """
        try:
            with requests.get(url, headers=self._headers, stream=True, timeout=60) as response:
                response.raise_for_status()
                content = BytesIO()
                for chunk in response.iter_content(chunk_size=8192):
                    content.write(chunk)
                return content.getvalue().decode("utf-16")
        except:
            logger.error(f"文件下载失败: {traceback.format_exc()}")
            return None

    @staticmethod
    def parse_tree_structure(content: str, dir_path: str):
        """
        解析目录树内容并生成每个路径
        """
        tree_pattern = re_compile(r"^(?:\| )+\|-")
        dir_path = Path(dir_path)
        current_path = [str(dir_path.parent)] if dir_path.parent != Path("/") or (dir_path.parent == dir_path and (
                dir_path.is_absolute() or ':' in dir_path.name)) else ["/"]  # 初始化当前路径为根目录

        for line in content.splitlines():
            # 匹配目录树的每一行
            match = tree_pattern.match(line)
            if not match:
                continue  # 跳过不符合格式的行

            # 计算当前行的深度
            level_indicator = match.group(0)
            depth = (len(level_indicator) // 2) - 1
            # 获取当前行的目录名称，去掉前面的 '| ' 或 '- '
            item_name = line.strip()[len(level_indicator):].strip()
            #item_name = escape(line.strip()[len(level_indicator):].strip())
            #item_name = quote(line.strip()[len(level_indicator):].strip(), safe='')


            # 根据深度更新当前路径
            if depth < len(current_path):
                current_path[depth] = item_name  # 更新已有深度的名称
            else:
                current_path.append(item_name)  # 添加新的深度名称

            # 生成并返回当前深度的完整路径
            yield join_path(*current_path[:depth + 1]).replace('\\', '/')

    @eventmanager.register(EventType.PluginAction)
    def remote_sync_one(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "strm_one":
                return
            args = event_data.get("arg_str")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return
            all_args = args

            # 使用正则表达式匹配
            category = None
            args_arr = args.split(maxsplit=1)
            limit = None
            if len(args_arr) == 2:
                category = args_arr[0]
                args = args_arr[1]
                if str(args).isdigit():
                    limit = int(args)

            if category:
                # 判断是不是目录
                if Path(category).is_dir() and Path(category).exists() and limit is not None:
                    # 遍历所有监控目录
                    mon_path = None
                    for mon in self._category_conf.keys():
                        if str(category).startswith(mon):
                            mon_path = mon
                            break

                    # 指定路径
                    if not mon_path:
                        logger.error(f"未找到 {category} 对应的监控目录")
                        self.post_message(channel=event.event_data.get("channel"),
                                          title=f"未找到 {category} 对应的监控目录",
                                          userid=event.event_data.get("user"))
                        return

                    self.__handle_limit(path=category, mon_path=mon_path, limit=limit, event=event)
                    return
                else:
                    for mon_path in self._category_conf.keys():
                        mon_category = self._category_conf.get(mon_path)
                        logger.info(f"开始检查 {mon_path} {mon_category}")
                        if mon_category and str(category) in mon_category:
                            parent_path = os.path.join(mon_path, category)
                            if limit:
                                logger.info(f"获取到 {category} 对应的监控目录 {parent_path}")
                                self.__handle_limit(path=parent_path, mon_path=mon_path, limit=limit, event=event)
                            else:
                                logger.info(f"获取到 {category} {args} 对应的监控目录 {parent_path}")
                                target_path = os.path.join(str(parent_path), args)
                                logger.info(f"开始处理 {target_path}")
                                target_paths = self.__find_related_paths(os.path.join(str(parent_path), args))
                                if not target_paths:
                                    logger.error(f"未查找到 {category} {args} 对应的具体目录")
                                    self.post_message(channel=event.event_data.get("channel"),
                                                      title=f"未查找到 {category} {args} 对应的具体目录",
                                                      userid=event.event_data.get("user"))
                                    return
                                for target_path in target_paths:
                                    logger.info(f"开始定向处理文件夹 ...{target_path}")
                                    for sroot, sdirs, sfiles in os.walk(target_path):
                                        for file_name in sdirs + sfiles:
                                            src_file = os.path.join(sroot, file_name)
                                            if Path(src_file).is_file():
                                                self.__handle_file(event_path=str(src_file), mon_path=mon_path)

                                    if event.event_data.get("user"):
                                        self.post_message(channel=event.event_data.get("channel"),
                                                          title=f"{target_path} Strm生成完成！",
                                                          userid=event.event_data.get("user"))

                                    if limit is None and event_data and event_data.get("action") == "strm_one":
                                        return
                            return
            else:
                # 遍历所有监控目录
                mon_path = None
                for mon in self._category_conf.keys():
                    if str(args).startswith(mon):
                        mon_path = mon
                        break

                # 指定路径
                if mon_path:
                    if not Path(args).exists():
                        logger.info(f"同步路径 {args} 不存在")
                        return
                    # 处理单文件
                    if Path(args).is_file():
                        self.__handle_file(event_path=str(args), mon_path=mon_path)
                        return
                    else:
                        # 处理指定目录
                        logger.info(f"获取到 {args} 对应的监控目录 {mon_path}")

                        logger.info(f"开始定向处理文件夹 ...{args}")
                        for sroot, sdirs, sfiles in os.walk(args):
                            for file_name in sdirs + sfiles:
                                src_file = os.path.join(sroot, file_name)
                                if Path(str(src_file)).is_file():
                                    self.__handle_file(event_path=str(src_file), mon_path=mon_path)
                        if event.event_data.get("user"):
                            self.post_message(channel=event.event_data.get("channel"),
                                              title=f"{all_args} Strm生成完成！", userid=event.event_data.get("user"))
                        return
                else:
                    for mon_path in self._category_conf.keys():
                        mon_category = self._category_conf.get(mon_path)
                        logger.info(f"开始检查 {mon_path} {mon_category}")
                        if mon_category and str(args) in mon_category:
                            parent_path = os.path.join(mon_path, args)
                            logger.info(f"获取到 {args} 对应的监控目录 {parent_path}")
                            for sroot, sdirs, sfiles in os.walk(parent_path):
                                for file_name in sdirs + sfiles:
                                    src_file = os.path.join(sroot, file_name)
                                    if Path(str(src_file)).is_file():
                                        self.__handle_file(event_path=str(src_file), mon_path=mon_path)
                            if event.event_data.get("user"):
                                self.post_message(channel=event.event_data.get("channel"),
                                                  title=f"{all_args} Strm生成完成！",
                                                  userid=event.event_data.get("user"))
                            return
            if event.event_data.get("user"):
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"{all_args} 未检索到，请检查输入是否正确！",
                                  userid=event.event_data.get("user"))

    @staticmethod
    def __find_related_paths(base_path):
        related_paths = []
        base_dir = os.path.dirname(base_path)
        base_name = os.path.basename(base_path)

        for entry in os.listdir(base_dir):
            if entry.startswith(base_name):
                full_path = os.path.join(base_dir, entry)
                if os.path.isdir(full_path):
                    related_paths.append(full_path)

        # 按照修改时间倒序排列
        related_paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)

        return related_paths

    def __handle_limit(self, path, limit, mon_path, event):
        """
        处理文件数量限制
        """
        sub_paths = []
        for entry in os.listdir(path):
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path):
                sub_paths.append(full_path)

        if not sub_paths:
            logger.error(f"未找到 {path} 目录下的文件夹")
            return

        # 按照修改时间倒序排列
        sub_paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        logger.info(f"开始定向处理文件夹 ...{path}, 最新 {limit} 个文件夹")
        for sub_path in sub_paths[:limit]:
            logger.info(f"开始定向处理文件夹 ...{sub_path}")
            for sroot, sdirs, sfiles in os.walk(sub_path):
                for file_name in sdirs + sfiles:
                    src_file = os.path.join(sroot, file_name)
                    if Path(src_file).is_file():
                        self.__handle_file(event_path=str(src_file), mon_path=mon_path)
            if event.event_data.get("user"):
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"{sub_path} Strm生成完成！", userid=event.event_data.get("user"))

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._medias or not self._medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._medias.keys()):
            media_list = self._medias.get(medis_title_year_season)
            logger.info(f"开始处理媒体 {medis_title_year_season} 消息")

            if not media_list:
                continue

            # 获取最后更新时间
            last_update_time = media_list.get("time")
            file_meta = media_list.get("file_meta")
            mtype = media_list.get("type")
            episodes = media_list.get("episodes")
            if not last_update_time:
                continue

            # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
            if (datetime.now() - last_update_time).total_seconds() > int(self._interval) \
                    or str(mtype) == "movie":
                # 发送通知
                if self._notify:
                    file_count = len(episodes) if episodes else 1

                    # 剧集季集信息 S01 E01-E04 || S01 E01、E02、E04
                    # 处理文件多，说明是剧集，显示季入库消息
                    media_type = None
                    if str(mtype) == "tv":
                        # 季集文本
                        season_episode = f"{medis_title_year_season} {StringUtils.format_ep(episodes)}"
                        media_type = MediaType.TV
                    else:
                        # 电影文本
                        season_episode = f"{medis_title_year_season}"
                        media_type = MediaType.MOVIE

                    # 获取封面图片
                    mediainfo: MediaInfo = self.chain.recognize_media(meta=file_meta,
                                                                      mtype=media_type,
                                                                      tmdbid=file_meta.tmdbid)

                    # 发送消息
                    self.send_transfer_message(msg_title=season_episode,
                                               file_count=file_count,
                                               image=(
                                                   mediainfo.backdrop_path if mediainfo.backdrop_path else mediainfo.poster_path) if mediainfo else None)
                # 发送完消息，移出key
                del self._medias[medis_title_year_season]
                continue

    def send_transfer_message(self, msg_title, file_count, image):
        """
        发送消息
        """
        # 发送
        self.post_message(
            mtype=NotificationType.Plugin,
            title=f"{msg_title} Strm已生成", text=f"共{file_count}个文件",
            image=image,
            link=settings.MP_DOMAIN('#/history'))

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cover": self._cover,
            "notify": self._notify,
            "rebuild": self._rebuild,
            "monitor": self._monitor,
            "interval": self._interval,
            "copy_files": self._copy_files,
            "copy_subtitles": self._copy_subtitles,
            "refresh_emby": self._refresh_emby,
            "cron": self._cron,
            "url": self._url,
            "monitor_confs": self._monitor_confs,
            "115_cookie": self._115_cookie,
            "rmt_mediaext": self._rmt_mediaext,
            "other_mediaext": self._other_mediaext,
            "mediaservers": self._mediaservers,
            # 新增：路径替换规则
            "path_replacements": "\n".join([f"{source}:{target}" for source, target in self._path_replacements.items()]) if self._path_replacements else "",
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [
            {
                "cmd": "/strm_generator",
                "event": EventType.PluginAction,
                "desc": "云盘Strm生成器",
                "category": "",
                "data": {
                    "action": "StrmGenerator"
                }
            },
            {
                "cmd": "/strm",
                "event": EventType.PluginAction,
                "desc": "定向云盘Strm同步",
                "category": "",
                "data": {
                    "action": "strm_one"
                }
            },
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
        if self._enabled and self._cron:
            return [{
                "id": "StrmGenerator",
                "name": "云盘Strm生成器",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.scan,
                "kwargs": {}
            }]
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '云盘实时监控任何问题不予处理，请自行消化。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'monitor',
                                            'label': '实时监控',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'copy_files',
                                            'label': '复制非媒体文件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '全量同步一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'rebuild',
                                            'label': '重建缓存',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'cover',
                                            'label': '覆盖已存在文件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'refresh_emby',
                                            'label': '刷新媒体库（Emby）',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'copy_subtitles',
                                            'label': '复制字幕文件',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'uriencode',
                                            'label': 'url编码',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '同步周期',
                                            'placeholder': '0 0 * * *'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': '115_cookie',
                                            'label': '115Cookie',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '消息延迟',
                                            'placeholder': '10'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_confs',
                                            'label': '目录配置',
                                            'rows': 5,
                                            'placeholder': 'MoviePilot中云盘挂载本地的路径#MoviePilot中strm生成路径#alist/cd2上115路径#strm格式化'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'rmt_mediaext',
                                            'label': '视频格式',
                                            'rows': 2,
                                            'placeholder': ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'other_mediaext',
                                            'label': '非媒体文件格式',
                                            'rows': 2,
                                            'placeholder': ".nfo, .jpg"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': '媒体服务器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.mediaserver_helper.get_configs().values() if
                                                      config.type == "emby"]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 8
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'emby_path',
                                            'rows': '1',
                                            'label': '媒体库路径映射',
                                            'placeholder': 'MoviePilot本地文件路径:Emby文件路径（多组路径英文逗号拼接）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 新增：路径替换规则文本框
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path_replacements',
                                            'label': '路径替换规则',
                                            'rows': 3,
                                            'placeholder': '源路径:目标路径（每行一条规则）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'MoviePilot中云盘挂载本地的路径：/mnt/media/series/国产剧/雪迷宫 (2024)；'
                                                    'MoviePilot中strm生成路径：/mnt/library/series/国产剧/雪迷宫 (2024)；'
                                                    '云盘路径：/cloud/media/series/国产剧/雪迷宫 (2024)；'
                                                    '则目录配置为：/mnt/media#/mnt/library#/cloud/media#{local_file}'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'strm格式化方式，自行把()替换为alist/cd2上路径：'
                                                    '1.本地源文件路径：{local_file}。'
                                                    '2.alist路径：http://192.168.31.103:5244/d/115{cloud_file}。'
                                                    '3.cd2路径：http://192.168.31.103:19798/static/http/192.168.31.103:19798/False/115{cloud_file}。'
                                                    '4.其他api路径：http://192.168.31.103:2001/{cloud_file}'
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
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "url",
                                            "label": "任务推送url",
                                            "placeholder": "post请求json方式推送path和type(add)字段",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ]
            }
        ], {
            "enabled": False,
            "cron": "",
            "onlyonce": False,
            "rebuild": False,
            "notify": False,
            "monitor": False,
            "cover": False,
            "copy_files": False,
            "uriencode": False,
            "copy_subtitles": False,
            "refresh_emby": False,
            "mediaservers": [],
            "monitor_confs": "",
            "emby_path": "",
            "interval": 10,
            "115_cookie": "",
            "url": "",
            "other_mediaext": ".nfo, .jpg, .png, .json",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v",
            "path_replacements": ""  # 新增：路径替换规则默认值
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    logger.error(f"停止观察者失败: {str(e)}")
            self._observer = []

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
                logger.error(f"停止调度器失败: {str(e)}")
            finally:
                self._scheduler = None