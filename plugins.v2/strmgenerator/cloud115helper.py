import time
import traceback
from io import BytesIO
from pathlib import Path
from typing import Mapping
import requests
from app.log import logger
from re import compile as re_compile
from posixpath import join as join_path


CRE_SET_COOKIE = re_compile(r"[0-9a-f]{32}=[0-9a-f]{32}.*")


class Cloud115Helper:

    _headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.192 Safari/537.36",
        "Cookie": "",
    }

    def __init__(self, cookie: str):
        self._headers["Cookie"] = cookie

    def export_dir(self, dir_id, destination_id="0"):
        """
        获取目录导出id
        """
        export_api = "https://webapi.115.com/files/export_dir"
        response = requests.post(
            url=export_api,
            headers=self._headers,
            data={"file_ids": dir_id, "target": f"U_1_{destination_id}"},
        )
        if response.status_code == 200:
            result = response.json()
            if result.get("state"):
                export_id = result.get("data", {}).get("export_id")

                logger.info(f"等待目录树导出...")
                retry_cnt = 60
                while retry_cnt > 0:
                    time.sleep(5)
                    response = requests.get(
                        url=export_api,
                        headers=self._headers,
                        params={"export_id": export_id},
                    )
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("state"):
                            # 当文件未生成时，返回data为空列表：[]，否则返回对象格式
                            if not isinstance(result.get("data"), list):
                                return result.get("data", {}).get(
                                    "pick_code"
                                ), result.get("data", {}).get("file_id")
                    retry_cnt -= 1
        return None

    def fs_dir_getid(self, path):
        """
        115路径转成ID
        """
        export_api = "https://webapi.115.com/files/getid"
        response = requests.get(
            url=export_api, headers=self._headers, params={"path": path}
        )
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
        response = requests.get(
            url=export_api,
            headers=self._headers,
            params={"pickcode": pickcode, "dl": 1},
        )
        download_url = None
        if response.status_code == 200:
            result = response.json()
            if result.get("state"):
                if not result.get("file_url_302"):
                    logger.error(f"获取115文件302下载地址失败: {str(result)}")
                    return None
                response = requests.get(
                    result.get("file_url_302"), headers=self._headers
                )
                if response.status_code == 200:
                    result = response.json()
                    if result.get("state"):
                        if not result.get("file_url"):
                            logger.error(f"获取115文件直链下载地址失败: {str(result)}")
                            return None
                        download_url = result.get("file_url")
                        # 处理Set-Cookie
                        if isinstance(response.headers, Mapping):
                            match = CRE_SET_COOKIE.search(
                                response.headers["Set-Cookie"]
                            )
                            if match is not None:
                                self._headers["Cookie"] += f";{match[0]}"
                        else:
                            for k, v in reversed(response.headers.items()):
                                if (
                                    k == "Set-Cookie"
                                    and CRE_SET_COOKIE.match(v) is not None
                                ):
                                    self._headers["Cookie"] += f";{v}"
                                    break
        return download_url

    def fs_delete(self, fid, pid):
        """
        删除115文件
        """
        export_api = "https://webapi.115.com/rb/delete"
        response = requests.post(
            url=export_api, headers=self._headers, data={"fid[0]": fid, "pid": pid}
        )
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
                logger.error(f"{directory_path} 请求生成目录树失败")
                return
            # 获取目录树下载链接
            download_url = self.download_url(pick_code)
            if not download_url:
                logger.error(f"{directory_path} 目录树下载链接获取失败")
                return
            directory_content = self.fetch_content(download_url)
            if not directory_content:
                logger.error(f"{directory_path} 目录树下载失败")
                return
            logger.info(f"{directory_path} 目录树下载成功")
            return directory_content
        except Exception as e:
            logger.error(f"{directory_path} 目录树生成失败: {str(e)}")
        finally:
            if file_id:
                try:
                    self.fs_delete(file_id, "U_1_0")
                except:
                    logger.error(f"目录树删除失败: {str(e)}")

    def fetch_content(self, url):
        """
        下载目录树文件内容
        """
        try:
            with requests.get(
                url, headers=self._headers, stream=True, timeout=60
            ) as response:
                response.raise_for_status()
                content = BytesIO()
                for chunk in response.iter_content(chunk_size=8192):
                    content.write(chunk)
                return content.getvalue().decode("utf-16")
        except:
            logger.error(f"文件下载失败: {traceback.format_exc()}")
            return None
        
    def parse_tree_structure(self, content: str, dir_path: str):
        """
        解析目录树内容并生成每个路径
        """
        tree_pattern = re_compile(r"^(?:\| )+\|-")
        dir_path = Path(dir_path)
        current_path = (
            [str(dir_path.parent)]
            if dir_path.parent != Path("/")
            or (
                dir_path.parent == dir_path
                and (dir_path.is_absolute() or ":" in dir_path.name)
            )
            else ["/"]
        )  # 初始化当前路径为根目录

        for line in content.splitlines():
            # 匹配目录树的每一行
            match = tree_pattern.match(line)
            if not match:
                continue  # 跳过不符合格式的行

            # 计算当前行的深度
            level_indicator = match.group(0)
            depth = (len(level_indicator) // 2) - 1
            # 获取当前行的目录名称，去掉前面的 '| ' 或 '- '
            item_name = line.strip()[len(level_indicator) :].strip()
            # item_name = escape(line.strip()[len(level_indicator):].strip())
            # item_name = quote(line.strip()[len(level_indicator):].strip(), safe='')

            # 根据深度更新当前路径
            if depth < len(current_path):
                current_path[depth] = item_name  # 更新已有深度的名称
            else:
                current_path.append(item_name)  # 添加新的深度名称

            # 生成并返回当前深度的完整路径
            yield join_path(*current_path[: depth + 1]).replace("\\", "/")