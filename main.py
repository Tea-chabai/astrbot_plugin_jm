"""JM 本子下载插件。

通过 /jm <编号> 下载本子并转换为 PDF 发送。多章节作品默认拒绝，
开启后可逐章下载并打包成 ZIP（支持加密）。
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import zipfile
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, StarTools, register


def _quiet_noisy_libraries() -> None:
    """压掉本插件用到的几个库的 DEBUG 日志。

    AstrBot 会把根 logger 设为 DEBUG 并把所有记录经拦截器转发给 loguru，
    因此第三方库的 DEBUG 输出会直接刷屏（例如 img2pdf 转 PDF 时，PIL 会为
    每张图打印一串 EXIF 标签）。AstrBot 自己有一份噪音库名单，但里面没有
    这些库，所以在这里补上。只降级 DEBUG，警告和错误仍然正常输出。
    """
    for name in ("PIL", "img2pdf", "pikepdf", "pyzipper"):
        logging.getLogger(name).setLevel(logging.WARNING)


_quiet_noisy_libraries()

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

# 文件名白名单：只保留字母、数字、下划线、连字符、空格和各类文字（含中日韩）。
# 方括号、括号、引号等在 QQ 客户端里容易出现文件名错乱或发送失败，
# 一律替换掉。
_FILENAME_ALLOWED = re.compile(r"[^\w\- ]", re.UNICODE)

# 文件名（不含扩展名）中标题部分的最大长度。过长时截断，
# 避免超出各平台文件系统与聊天平台的文件名限制。
# 多章节本子会在标题后追加章节序号，所以留出余量。
_MAX_TITLE_LEN = 30

# 多章节本子整体图片数的硬性上限，不受 max_chapters 影响。
# 章节数限制管不住「章节少但每章极长」的情况，这里兜底。
_MAX_ALBUM_IMAGES = 3000


def _clean_title(title: str) -> str:
    """把本子标题清洗成安全的文件名片段（不含扩展名）。"""
    # 先折叠清理，再按白名单过滤，多余字符替换为下划线后折叠重复项
    name = re.sub(r"\s+", " ", title).strip().strip(". ")
    name = _FILENAME_ALLOWED.sub("_", name)
    name = re.sub(r"_{2,}", "_", name).strip("_ ")

    if len(name) > _MAX_TITLE_LEN:
        name = name[:_MAX_TITLE_LEN].rstrip("_ ")

    # 保留名加个下划线规避，例如 "CON" -> "CON_"（Windows 不允许直接使用）
    if name and name.upper() in _WIN_RESERVED:
        name += "_"

    return name


def _safe_filename(title: str, book_id: str, chapter: Optional[int] = None) -> str:
    """拼出安全的 PDF 文件名（不含扩展名）。

    ``chapter`` 为章节序号（从 1 起）时，会追加到文件名中，便于区分多章节
    本子的每一章。
    """
    name = _clean_title(title)
    if not name:
        name = f"JM{book_id}"
    if chapter is not None:
        return f"{name}_{chapter}_{book_id}"
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

            # 多章节本子逐章下载时，每章都要有独立的文件名，否则会互相覆盖
            chapter = None
            if photo is not None and getattr(photo, "is_single_album", True) is False:
                chapter = photo.album_index

            filename = f"{_safe_filename(title, book_id, chapter)}.pdf"

            target_dir = base_dir or os.getcwd()
            os.makedirs(target_dir, exist_ok=True)
            return os.path.join(target_dir, filename)

    jmcomic.JmModuleConfig.register_plugin(TitlePdfPlugin)


def _encrypt_pdf(pdf_path: str, password: str) -> None:
    """给 PDF 加上打开密码（就地覆盖）。"""
    import pikepdf

    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        pdf.save(
            pdf_path,
            encryption=pikepdf.Encryption(user=password, owner=password),
        )


class MissingBookError(Exception):
    """本子不存在或不可见。"""


class MultiChapterError(Exception):
    """多章节本子，暂不支持。"""


class TooManyImagesError(Exception):
    """图片数量超过配置上限。"""


class TooManyChaptersError(Exception):
    """章节数量超过配置上限。"""


class TooManyAlbumImagesError(Exception):
    """多章节本子的图片总数超过硬性上限。"""


@register(
    "astrbot_plugin_jm",
    "Tea-chabai",
    "使用 /jm <编号> 下载 JM 本子并转为 PDF 发送",
    "v1.1.1",
)
class JmDownloaderPlugin(Star):
    """JM 本子下载插件

    使用 /jm <编号> 下载本子，合并为 PDF 后发送。

    /jm <编号>
    --下载指定编号的本子，成功后将 PDF 发送到当前会话
    --编号可以在本子详情页的地址栏中获取
    --对多章节作品，给出其中某一章的编号即下载该章

    /jmhelp
    --查看帮助

    多章节作品默认会被拒绝，需要在配置中开启「允许下载多章节本子」。
    开启后会把每一章生成为独立的 PDF，打包成 ZIP 一并发送。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}

        self.max_images = int(self.config.get("max_images", 500))
        self.cookies = (self.config.get("cookies") or "").strip()
        self.username = (self.config.get("username") or "").strip()
        self.password = (self.config.get("password") or "").strip()

        # 多章节下载开关与章节数上限
        self.allow_multi_chapter = bool(self.config.get("allow_multi_chapter", False))
        self.max_chapters = int(self.config.get("max_chapters", 30))

        # PDF / ZIP 的加密密码
        self.lock_password = (self.config.get("lock_password") or "").strip()
        if self.lock_password and len(self.lock_password) < 4:
            logger.warning("[JM] lock_password 少于 4 位，已忽略，本次不加密")
            self.lock_password = ""

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

    def _validate_book(self, book_id: str) -> dict:
        """校验编号对应的本子，返回后续下载所需的信息。

        Raises:
            MissingBookError: 编号不存在或不可见。
            MultiChapterError: 该本子包含多个章节，且未开启多章节下载。
            TooManyImagesError: 图片数量超过配置上限。
            TooManyChaptersError: 章节数量超过配置上限。
        """
        jmcomic = _load_jmcomic()
        client = self._get_client()

        # 先查专辑，只有专辑详情才带章节列表，能可靠判断是否多章节
        try:
            album = client.get_album_detail(book_id)
        except jmcomic.MissingAlbumPhotoException as exc:
            raise MissingBookError(str(exc)) from exc

        episodes = album.episode_list
        is_multi = len(episodes) > 1

        if is_multi and not self.allow_multi_chapter:
            raise MultiChapterError(f"{len(episodes)} 章")
        if is_multi and len(episodes) > self.max_chapters:
            raise TooManyChaptersError(
                f"共 {len(episodes)} 章，超过上限 {self.max_chapters} 章"
            )
        if is_multi and album.page_count > _MAX_ALBUM_IMAGES:
            raise TooManyAlbumImagesError(
                f"共 {album.page_count} 张，超过硬性上限 {_MAX_ALBUM_IMAGES} 张"
            )

        # 章节 ID 要定位到对应那一章，不能一律当作第一章，
        # 否则用户发第 N 章的编号会静默拿到第 1 章
        chapter_index = 0
        for index, episode in enumerate(episodes):
            if str(episode[0]) == book_id:
                chapter_index = index
                break
        else:
            # 用户给的是专辑编号（或不在列表里的编号），退回第一章
            logger.info(f"[JM] {book_id} 未匹配到具体章节，使用第 1 章")

        # create_photo_detail 接收章节下标（0 起）
        photo = album.create_photo_detail(chapter_index)

        # 此时 photo 只有章节信息，补齐图片列表后才知道张数
        # 数据库/缓存会复用这次请求，不会产生额外开销
        client.check_photo(photo)

        images = len(photo)
        if images <= 0:
            raise MissingBookError("该本子没有任何图片")
        if not is_multi and images > self.max_images:
            raise TooManyImagesError(f"共 {images} 张，超过上限 {self.max_images} 张")

        return {
            "is_multi": is_multi,
            "total_chapters": len(episodes),
            "chapter_index": chapter_index,
            "chapter_number": chapter_index + 1,
            "album_title": album.title or f"JM{book_id}",
            "album_id": str(album.id),
            "first_photo_id": str(episodes[0][0]),
            "images": images,
        }

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

    # ------------------------------------------------------------ 多章节下载

    def _download_album_zip(self, info: dict) -> dict:
        """逐章下载多章节本子，生成 PDF 后打包成 ZIP。

        每章单独一个 PDF，再整体压成一个 ZIP（可加密），这样单次请求的
        投递物只有一个文件，也不会因为某章失败而丢掉已下载的内容。

        Returns:
            与单章节下载一致的结果字典，``kind`` 为 ``"zip"``。
        """
        client = self._get_client()
        album = client.get_album_detail(info["album_id"])
        episodes = album.episode_list

        work_dir = tempfile.mkdtemp(prefix="jm_album_", dir=self.output_dir)
        pdfs: list[str] = []
        failures: list[str] = []

        try:
            for index, episode in enumerate(episodes):
                photo_id = str(episode[0])
                number = index + 1
                try:
                    pdf_path = self._download_chapter_pdf(photo_id, work_dir, number)
                except Exception as exc:
                    logger.warning(f"[JM] 第 {number} 章（{photo_id}）下载失败：{exc}")
                    failures.append(f"第 {number} 章")
                    continue

                if self.lock_password:
                    _encrypt_pdf(pdf_path, self.lock_password)

                # 移入工作目录并统一命名，避免与输出目录里的旧文件混淆
                target = os.path.join(
                    work_dir, f"{number:03d}_{os.path.basename(pdf_path)}"
                )
                if os.path.abspath(pdf_path) != os.path.abspath(target):
                    os.replace(pdf_path, target)
                pdfs.append(target)

            if not pdfs:
                raise RuntimeError("所有章节都下载失败")

            zip_name = f"{_safe_filename(info['album_title'], info['album_id'])}.zip"
            zip_path = os.path.join(self.output_dir, zip_name)
            chapters = len(pdfs)

            zip_size = self._make_zip(pdfs, zip_path)
            logger.info(
                f"[JM] 已打包 {chapters}/{len(episodes)} 章 → {zip_name}"
                f"（{zip_size / 1024 / 1024:.1f} MB）"
            )
            return {
                "kind": "zip",
                "path": zip_path,
                "name": zip_name,
                "title": info["album_title"],
                "chapters": chapters,
                "failures": failures,
            }
        finally:
            # 清理本次请求留下的临时目录
            self._cleanup_dir(work_dir)

    def _download_chapter_pdf(self, photo_id: str, work_dir: str, number: int) -> str:
        """下载单个章节并生成 PDF，返回 PDF 路径。"""
        option = self._get_option()

        before = set(os.listdir(self.output_dir))
        # 必须传编号字符串：传实体对象会被当成批量下载的迭代对象
        result = option.download_photo(photo_id)

        pdf_path = next(iter(result.manifest.get_export_filepath_list("pdf")), None)
        if not pdf_path or not os.path.exists(pdf_path):
            pdf_path = self._find_new_pdf(before)
        if not pdf_path:
            raise RuntimeError("未生成 PDF 文件")

        logger.info(f"[JM] 第 {number} 章完成：{os.path.basename(pdf_path)}")
        return pdf_path

    def _make_zip(self, pdf_paths: list[str], zip_path: str) -> int:
        """把若干 PDF 打包成 ZIP（有密码时加密），返回文件大小。"""
        if self.lock_password:
            try:
                import pyzipper
            except ImportError as exc:
                raise RuntimeError(
                    "加密 ZIP 需要 pyzipper，请执行：pip install pyzipper"
                ) from exc

            with pyzipper.AESZipFile(
                zip_path,
                "w",
                compression=pyzipper.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as archive:
                archive.setpassword(self.lock_password.encode("utf-8"))
                archive.setencryption(pyzipper.WZ_AES, nbits=256)
                for path in pdf_paths:
                    archive.write(path, os.path.basename(path))
            return os.path.getsize(zip_path)

        # 无密码时用标准库即可，少一个运行时依赖
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in pdf_paths:
                archive.write(path, os.path.basename(path))
        return os.path.getsize(zip_path)

    @staticmethod
    def _cleanup_dir(path: str) -> None:
        """删除临时目录，失败不影响主流程。"""
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            logger.warning(f"[JM] 临时目录清理失败：{path}")

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
            info = await asyncio.to_thread(self._validate_book, book_id)
        except MissingBookError:
            yield event.plain_result("未查询到相关本子")
            return
        except MultiChapterError:
            yield event.plain_result("暂时无法输出多章节的本子")
            return
        except TooManyImagesError as exc:
            yield event.plain_result(f"本子图片过多（{exc}），无法转换为 PDF")
            return
        except TooManyChaptersError as exc:
            yield event.plain_result(f"本子章节过多（{exc}），无法打包")
            return
        except TooManyAlbumImagesError as exc:
            yield event.plain_result(f"本子图片总数过多（{exc}），无法打包")
            return
        except Exception as exc:
            logger.exception(f"[JM] 查询 {book_id} 失败")
            yield event.plain_result(f"查询失败：{self._brief(exc)}")
            return

        timeout = float(self.config.get("timeout", 1800))

        if info["is_multi"]:
            target = (
                f"{info['album_title']}（共 {info['total_chapters']} 章，"
                f"从第 {info['chapter_number']} 章开始）"
            )
        else:
            target = info["album_title"]
        logger.info(f"[JM] 开始下载 {book_id}：{target}")

        try:
            async with self._download_lock:
                if info["is_multi"]:
                    outcome = await asyncio.wait_for(
                        asyncio.to_thread(self._download_album_zip, info),
                        timeout=timeout,
                    )
                else:
                    pdf_path, title, images = await asyncio.wait_for(
                        asyncio.to_thread(self._download_pdf, book_id),
                        timeout=timeout,
                    )
                    outcome = {
                        "path": pdf_path,
                        "name": os.path.basename(pdf_path),
                        "kind": "pdf",
                        "title": title,
                        "images": images,
                    }
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"下载超时（超过 {int(timeout)} 秒），请稍后重试或调大配置中的 timeout"
            )
            return
        except Exception as exc:
            logger.exception(f"[JM] 下载 {book_id} 失败")
            yield event.plain_result(f"下载失败：{self._brief(exc)}")
            return

        # 发送
        try:
            yield event.chain_result(
                [File(name=outcome["name"], file=outcome["path"])]
            )
        except Exception as exc:
            logger.exception(f"[JM] 发送 {outcome['name']} 失败")
            yield event.plain_result(
                f"{outcome['kind'].upper()} 已生成但发送失败：{self._brief(exc)}"
            )
            return

        yield event.plain_result(self._summary(outcome))

    def _summary(self, outcome: dict) -> str:
        """拼装发送成功后给用户的说明。"""
        size_mb = os.path.getsize(outcome["path"]) / 1024 / 1024
        lines = [outcome["title"]]

        if outcome["kind"] == "zip":
            lines.append(
                f"已打包 {outcome['chapters']} 章，ZIP {size_mb:.1f} MB，已发送"
            )
            if outcome["failures"]:
                lines.append(
                    f"以下章节下载失败：{'、'.join(outcome['failures'])}"
                )
        else:
            lines.append(f"共 {outcome['images']} 张，PDF {size_mb:.1f} MB，已发送")

        if self.lock_password:
            lines.append(f"解压密码：{self.lock_password}")
        return "\n".join(lines)

    @filter.command("jmhelp")
    async def jmhelp(self, event: AstrMessageEvent):
        """查看插件帮助。"""
        yield event.plain_result(
            "JM 本子下载\n"
            "\n"
            "/jm <编号>\n"
            "--下载指定编号的本子，成功后以 PDF 发送\n"
            "--编号可在本子详情页的地址栏中获取\n"
            "--多章节作品给出其中某一章的编号即下载该章\n"
            "\n"
            "/jmhelp\n"
            "--查看帮助\n"
            "\n"
            + (
                f"多章节下载已开启：每章单独一个 PDF，打包成 ZIP 发送，"
                f"单次最多 {self.max_chapters} 章。"
                if self.allow_multi_chapter
                else "多章节作品暂不支持。"
            )
            + ("\n本子已加密码保护，密码会随文件一并告知。" if self.lock_password else "")
        )

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _brief(exc: Exception, limit: int = 200) -> str:
        """把异常压缩成一行，便于直接发给用户。"""
        text = re.sub(r"\s+", " ", str(exc)).strip()
        return text[:limit] if text else exc.__class__.__name__
