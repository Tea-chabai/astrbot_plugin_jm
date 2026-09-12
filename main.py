"""JM 本子下载插件。

通过 /jm <编号> 下载单章节本子，合并为 PDF 后发送。
"""

import asyncio
import os
import re
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, StarTools, register

# jmcomic 按需导入：即便依赖缺失或与 AstrBot 环境冲突，插件本身仍能正常加载，
# 只在真正使用 /jm 时给出明确的安装提示
_jmcomic = None


def _load_jmcomic():
    """按需导入 jmcomic，缺少依赖时抛出带安装提示的错误。"""
    global _jmcomic
    if _jmcomic is None:
        try:
            import jmcomic
        except ImportError as exc:
            raise RuntimeError(
                "缺少依赖 jmcomic 或 img2pdf，请执行：pip install -r requirements.txt"
            ) from exc
        _jmcomic = jmcomic
    return _jmcomic


# JM 编号为纯数字
_ID_PATTERN = re.compile(r"\d+")

# Windows 文件名保留名，出现在文件名中时加前缀规避
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# PDF 文件名（不含扩展名）的最大长度，过长时截断，
# 避免超出各平台文件系统的限制
_MAX_FILENAME_LEN = 40

# 文件名白名单：只保留字母、数字、下划线、连字符、空格和各类文字（含中日韩）。
# 方括号、括号、引号等在 QQ 客户端里容易出现文件名错乱或发送失败，
# 一律替换掉。
_FILENAME_ALLOWED = re.compile(r"[^\w\- ]", re.UNICODE)


def _safe_filename(title: str, book_id: str) -> str:
    """把本子标题清洗成安全的文件名（不含扩展名）。

    除了避开文件系统非法字符，还要保证能安全通过聊天平台的文件发送：
    这里采取白名单策略，只保留字母数字、汉字等文字字符和 ``_ -`` 与空格。
    """
    # 先折叠清理，再按白名单过滤，多余字符替换为下划线后折叠重复项
    name = re.sub(r"\s+", " ", title).strip().strip(". ")
    name = _FILENAME_ALLOWED.sub("_", name)
    name = re.sub(r"_{2,}", "_", name).strip("_ ")

    # 超长时截断标题；编号在末尾，所以截断要在拼接编号之前做
    if len(name) > _MAX_FILENAME_LEN:
        name = name[:_MAX_FILENAME_LEN].rstrip("_ ")

    if not name:
        return f"JM{book_id}"

    # 保留名加个下划线规避，例如 "CON" -> "CON_"（Windows 不允许直接使用）
    if name.upper() in _WIN_RESERVED:
        name += "_"

    # 带上编号，便于识别与去重；编号本身总是安全的
    return f"{name}_{book_id}"


# 自定义 img2pdf 插件的注册名，避免与 jmcomic 自带的 img2pdf 冲突
TITLE_PDF_PLUGIN_KEY = "astrbot_jm_title_pdf"


def _register_title_pdf_plugin():
    """向 jmcomic 注册「按本子标题命名 PDF」的 img2pdf 插件。

    jmcomic 自带的 img2pdf 插件只能按编号等规则命名，这里覆写
    ``decide_filepath``，改用清洗后的本子标题作为文件名。该函数直接拼接
    目录与文件名，不走 ``dir_rule`` 的 DSL 解析，因此标题里的各种符号都
    不会引发解析错误。重复调用是安全的。
    """
    jmcomic = _load_jmcomic()

    class TitlePdfPlugin(jmcomic.Img2pdfPlugin):
        plugin_key = TITLE_PDF_PLUGIN_KEY

        def decide_filepath(
            self,
            album=None,
            photo=None,
            filename_rule=None,
            suffix="pdf",
            base_dir=None,
            dir_rule=None,
        ):
            detail = photo or album
            title = detail.title if detail and detail.title else "JM本子"
            book_id = str(getattr(detail, "id", "") or detail.album_id)
            filename = f"{_safe_filename(title, book_id)}.pdf"

            target_dir = base_dir or os.getcwd()
            os.makedirs(target_dir, exist_ok=True)
            return os.path.join(target_dir, filename)

    jmcomic.JmModuleConfig.register_plugin(TitlePdfPlugin)


class MissingBookError(Exception):
    """本子不存在或不可见。"""


class MultiChapterError(Exception):
    """多章节本子，暂不支持。"""


class TooManyImagesError(Exception):
    """图片数量超过配置上限。"""


@register(
    "astrbot_plugin_jm",
    "Tea-chabai",
    "使用 /jm <编号> 下载 JM 本子并转为 PDF 发送",
    "v1.0.0",
)
class JmDownloaderPlugin(Star):
    """JM 本子下载插件

    使用 /jm <编号> 下载单章节本子，合并为 PDF 后发送。

    /jm <编号>
    --下载指定编号的本子，成功后将 PDF 发送到当前会话
    --编号可以在本子详情页的地址栏中获取

    /jmhelp
    --查看帮助

    注意：多章节作品暂不支持。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}

        self.max_images = int(self.config.get("max_images", 500))
        self.cookies = (self.config.get("cookies") or "").strip()
        self.username = (self.config.get("username") or "").strip()
        self.password = (self.config.get("password") or "").strip()

        # PDF 输出目录：未配置时使用插件数据目录
        configured_dir = (self.config.get("output_dir") or "").strip()
        self.output_dir = (
            configured_dir
            if configured_dir
            else str(StarTools.get_data_dir("astrbot_plugin_jm"))
        )
        os.makedirs(self.output_dir, exist_ok=True)

        # jmcomic 的 option / client 构造有网络开销，缓存复用
        self._option = None
        self._client = None

        # 串行化下载，避免并发时兜底查找挑错文件
        self._download_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 配置

    def _get_option(self):
        """构造 jmcomic 的 JmOption，复用已缓存实例。"""
        if self._option is not None:
            return self._option

        jmcomic = _load_jmcomic()

        # 注册我们的 PDF 插件，按本子标题命名输出文件
        _register_title_pdf_plugin()

        option_dict = {
            # 关掉 jmcomic 自己的日志，统一走 AstrBot 日志。
            # 用配置项而非 disable_jm_log()，免得影响同进程里的其他插件
            "log": False,
            # 章节文件夹保留原名，PDF 输出到 output_dir
            "dir_rule": {
                "base_dir": self.output_dir,
                "rule": "Bd_Aid_Pindex",
            },
            "download": {
                "threading": {
                    "image": int(self.config.get("threads", 8)),
                },
                "image": {
                    "decode": True,  # 解密为普通图片，PDF 才能正常显示
                },
            },
            "client": {
                "impl": "api",
                "retry_times": int(self.config.get("retry_times", 3)),
            },
            "plugins": {
                "after_photo": [
                    {
                        "plugin": TITLE_PDF_PLUGIN_KEY,
                        "kwargs": {
                            "pdf_dir": self.output_dir,
                            "delete_original_file": bool(
                                self.config.get("delete_images", True)
                            ),
                        },
                    }
                ]
            },
        }

        if self.cookies:
            option_dict["client"]["cookies"] = self.cookies

        self._option = jmcomic.JmOption.construct(option_dict)
        return self._option

    def _get_client(self):
        """获取 jmcomic 客户端，复用已缓存实例。

        配置了账号密码时自动登录，登录态写回该客户端维护的 cookies，
        后续查询与下载都会带上。
        """
        if self._client is None:
            self._client = self._get_option().new_jm_client()

            if self.username and self.password:
                try:
                    self._client.login(self.username, self.password)
                    logger.info(f"[JM] 已登录账号 {self.username}")
                except Exception as exc:
                    logger.warning(
                        f"[JM] 登录失败，将以游客身份访问：{self._brief(exc)}"
                    )

        return self._client

    # -------------------------------------------------------------- 业务逻辑

    def _validate_book(self, book_id: str) -> None:
        """校验编号对应的本子，确认可以下载。

        Raises:
            MissingBookError: 编号不存在或不可见。
            MultiChapterError: 该本子包含多个章节。
            TooManyImagesError: 图片数量超过配置上限。
        """
        jmcomic = _load_jmcomic()
        client = self._get_client()

        # 先查专辑，只有专辑详情才带章节列表，能可靠判断是否多章节
        try:
            album = client.get_album_detail(book_id)
        except jmcomic.MissingAlbumPhotoException as exc:
            raise MissingBookError(str(exc)) from exc

        if len(album.episode_list) > 1:
            raise MultiChapterError(f"{len(album.episode_list)} 章")

        # create_photo_detail 接收章节下标（0 起），单章节本子取第 0 个
        photo = album.create_photo_detail(0)

        # 此时 photo 只有章节信息，补齐图片列表后才知道张数
        # 数据库/缓存会复用这次请求，不会产生额外开销
        client.check_photo(photo)

        images = len(photo)
        if images <= 0:
            raise MissingBookError("该本子没有任何图片")
        if images > self.max_images:
            raise TooManyImagesError(f"共 {images} 张，超过上限 {self.max_images} 张")

    def _download_pdf(self, book_id: str) -> tuple[str, str, int]:
        """下载本子并生成 PDF。

        Returns:
            (PDF 绝对路径, 本子标题, 图片张数)
        """
        option = self._get_option()

        # 记录下载前已存在的 PDF，便于插件未登记导出路径时兜底查找
        before = set(os.listdir(self.output_dir))

        # 必须传编号字符串：传实体对象会被当成批量下载的迭代对象
        result = option.download_photo(book_id)

        photo = result.detail
        title = photo.title or f"JM{book_id}"
        images = len(photo)

        pdf_path = next(
            iter(result.manifest.get_export_filepath_list("pdf")), None
        )
        if not pdf_path or not os.path.exists(pdf_path):
            pdf_path = self._find_new_pdf(before)
        if not pdf_path:
            raise RuntimeError("下载完成但未生成 PDF 文件")

        size_mb = os.path.getsize(pdf_path) / 1024 / 1024
        logger.info(
            f"[JM] 已生成 PDF：{pdf_path}（{size_mb:.1f} MB，{images} 张图）"
        )
        return pdf_path, title, images

    def _find_new_pdf(self, before: set[str]) -> Optional[str]:
        """在输出目录中查找本次新增、且最近修改的 PDF。"""
        try:
            candidates = [
                os.path.join(self.output_dir, name)
                for name in os.listdir(self.output_dir)
                if name.lower().endswith(".pdf") and name not in before
            ]
        except OSError:
            return None

        if not candidates:
            return None
        return max(candidates, key=os.path.getmtime)

    # ---------------------------------------------------------------- 指令

    @filter.command("jm")
    async def jm(self, event: AstrMessageEvent, book_id: str = ""):
        """下载指定编号的本子并发送 PDF。"""
        book_id = (book_id or "").strip()

        if not book_id:
            yield event.plain_result("请提供本子编号，例如：/jm 422866")
            return

        match = _ID_PATTERN.search(book_id)
        if not match:
            yield event.plain_result(
                f"编号格式不正确：{book_id}\n请使用纯数字编号，例如：/jm 422866"
            )
            return
        book_id = match.group()

        yield event.plain_result(f"收到，正在查询 JM{book_id}，请稍候…")

        try:
            # 网络与下载均为阻塞操作，放到线程池执行，避免卡住事件循环
            await asyncio.to_thread(self._validate_book, book_id)
        except MissingBookError:
            yield event.plain_result("未查询到相关本子")
            return
        except MultiChapterError:
            yield event.plain_result("暂时无法输出多章节的本子")
            return
        except TooManyImagesError as exc:
            yield event.plain_result(f"本子图片过多（{exc}），无法转换为 PDF")
            return
        except Exception as exc:
            logger.exception(f"[JM] 查询 {book_id} 失败")
            yield event.plain_result(f"查询失败：{self._brief(exc)}")
            return

        logger.info(f"[JM] 开始下载 {book_id}")

        timeout = float(self.config.get("timeout", 1800))
        try:
            async with self._download_lock:
                pdf_path, title, images = await asyncio.wait_for(
                    asyncio.to_thread(self._download_pdf, book_id),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"下载超时（超过 {int(timeout)} 秒），请稍后重试或调大配置中的 timeout"
            )
            return
        except Exception as exc:
            logger.exception(f"[JM] 下载 {book_id} 失败")
            yield event.plain_result(f"下载失败：{self._brief(exc)}")
            return

        filename = os.path.basename(pdf_path)
        size_mb = os.path.getsize(pdf_path) / 1024 / 1024
        try:
            yield event.chain_result([File(name=filename, file=pdf_path)])
        except Exception as exc:
            logger.exception(f"[JM] 发送 {filename} 失败")
            yield event.plain_result(f"PDF 已生成但发送失败：{self._brief(exc)}")
            return

        yield event.plain_result(
            f"{title}\n共 {images} 张，PDF {size_mb:.1f} MB，已发送"
        )

    @filter.command("jmhelp")
    async def jmhelp(self, event: AstrMessageEvent):
        """查看插件帮助。"""
        yield event.plain_result(
            "JM 本子下载\n"
            "\n"
            "/jm <编号>\n"
            "--下载指定编号的本子，成功后以 PDF 发送\n"
            "--编号可在本子详情页的地址栏中获取\n"
            "\n"
            "/jmhelp\n"
            "--查看帮助\n"
            "\n"
            "注意：多章节作品暂不支持。"
        )

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _brief(exc: Exception, limit: int = 200) -> str:
        """把异常压缩成一行，便于直接发给用户。"""
        text = re.sub(r"\s+", " ", str(exc)).strip()
        return text[:limit] if text else exc.__class__.__name__
