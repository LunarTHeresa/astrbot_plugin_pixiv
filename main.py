from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PIXIV_OAUTH = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_API_BASE = "https://app-api.pixiv.net"
UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"


class PixivClient:
    def __init__(self, refresh_token: str, proxy_url: str = "", timeout_sec: int = 30):
        self.refresh_token = refresh_token.strip()
        self.proxy_url = proxy_url.strip()
        self.timeout_sec = max(5, int(timeout_sec))
        self.access_token: Optional[str] = None
        self.expire_at: float = 0.0

    async def _refresh(self) -> None:
        headers = {
            "User-Agent": UA,
            "App-OS": "android",
            "App-OS-Version": "11",
            "App-Version": "5.0.234",
        }
        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "include_policy": "true",
        }
        client_kwargs: Dict[str, Any] = {"timeout": self.timeout_sec}
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(PIXIV_OAUTH, headers=headers, data=data)
            resp.raise_for_status()
            payload = resp.json()
        self.access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        self.expire_at = time.time() + max(60, expires_in - 120)

    async def _auth_headers(self) -> Dict[str, str]:
        if (not self.access_token) or (time.time() >= self.expire_at):
            await self._refresh()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": UA,
            "App-OS": "android",
            "App-OS-Version": "11",
            "App-Version": "5.0.234",
        }

    async def get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        headers = await self._auth_headers()
        client_kwargs: Dict[str, Any] = {"timeout": self.timeout_sec}
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(f"{PIXIV_API_BASE}{path}", headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_raw_bytes(self, url: str) -> bytes:
        headers = await self._auth_headers()
        parsed = urlparse(url)
        referer = "https://www.pixiv.net/"
        if parsed.scheme and parsed.netloc:
            referer = f"{parsed.scheme}://{parsed.netloc}/"

        req_headers = dict(headers)
        req_headers["Referer"] = referer

        client_kwargs: Dict[str, Any] = {"timeout": self.timeout_sec}
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url, headers=req_headers)
            resp.raise_for_status()
            return resp.content

    async def search_illust(self, word: str, r18: bool) -> Optional[Dict[str, Any]]:
        data = await self.get(
            "/v1/search/illust",
            {
                "word": word,
                "search_target": "partial_match_for_tags",
                "sort": "date_desc",
                "filter": "for_ios",
            },
        )
        for item in data.get("illusts", []):
            x = item.get("x_restrict", 0)
            if r18 and x == 1:
                return item
            if (not r18) and x == 0:
                return item
        return None

    async def search_novel(self, word: str, r18: bool) -> Optional[Dict[str, Any]]:
        data = await self.get(
            "/v1/search/novel",
            {
                "word": word,
                "search_target": "partial_match_for_tags",
                "sort": "date_desc",
                "filter": "for_ios",
            },
        )
        for item in data.get("novels", []):
            x = item.get("x_restrict", 0)
            if r18 and x == 1:
                return item
            if (not r18) and x == 0:
                return item
        return None

    async def novel_text(self, novel_id: int) -> str:
        data = await self.get("/v1/novel/text", {"novel_id": novel_id})
        return data.get("novel_text", "")


@register(
    "astrbot_plugin_pixiv",
    "LunarTHeresa",
    "Pixiv官方API 普通/R18 图片与小说发送",
    "1.0.5",
    "https://github.com/LunarTHeresa/astrbot_plugin_pixiv",
)
class PixivPlugin(Star):
    # 兼容旧版加载流程中对 proxy 的访问
    proxy = None

    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        self.proxy = None
        super().__init__(context)
        self.config = config or {}

        self.refresh_token = ""
        self.allow_r18 = False
        self.pixiv_proxy = ""
        self.request_timeout_sec = 30
        self.send_image_as_file = True
        self.client: Optional[PixivClient] = None
        self._load_conf()

    async def initialize(self):
        # 避免某些版本在 initialize 后才注入配置
        self._load_conf()

    async def terminate(self):
        return

    def _load_conf(self) -> None:
        conf: Dict[str, Any] = {}

        for candidate in (
            getattr(self, "config", None),
            getattr(getattr(self, "context", None), "config", None),
        ):
            if isinstance(candidate, dict):
                conf.update(candidate)
            elif hasattr(candidate, "get"):
                try:
                    conf.update(dict(candidate))
                except Exception:
                    pass

        getter = getattr(self, "get_config", None)
        if callable(getter):
            try:
                dynamic_conf = getter() or {}
                if isinstance(dynamic_conf, dict):
                    conf.update(dynamic_conf)
            except Exception:
                pass

        token = str(conf.get("pixiv_refresh_token", "")).strip()
        allow = conf.get("allow_r18", False)
        proxy_url = str(conf.get("pixiv_proxy", "")).strip()
        timeout_raw = conf.get("request_timeout_sec", 30)
        send_image_as_file_raw = conf.get("send_image_as_file", True)
        try:
            timeout_sec = int(timeout_raw)
        except Exception:
            timeout_sec = 30

        if isinstance(allow, str):
            allow_flag = allow.strip().lower() in {"1", "true", "yes", "on"}
        else:
            allow_flag = bool(allow)

        if isinstance(send_image_as_file_raw, str):
            send_image_as_file = send_image_as_file_raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            send_image_as_file = bool(send_image_as_file_raw)

        self.refresh_token = token
        self.allow_r18 = allow_flag
        self.pixiv_proxy = proxy_url
        self.request_timeout_sec = max(5, timeout_sec)
        self.send_image_as_file = send_image_as_file
        self.client = PixivClient(
            token,
            proxy_url=self.pixiv_proxy,
            timeout_sec=self.request_timeout_sec,
        ) if token else None

    def _keyword(self, text: str) -> str:
        text = re.sub(r"^/[a-zA-Z0-9_]+", "", text.strip()).strip()
        return text or "女の子"

    def _guard(self, r18: bool) -> Optional[str]:
        if not self.client:
            return "未配置 pixiv_refresh_token，请先在插件配置中填写。"
        if r18 and not self.allow_r18:
            return "管理员未开启 R18（allow_r18=false）。"
        return None

    async def _send_image(self, event: AstrMessageEvent, item: Dict[str, Any]):
        title = item.get("title", "")
        uid = item.get("id", "")
        user = (item.get("user") or {}).get("name", "unknown")
        urls = item.get("image_urls") or {}
        image_url = urls.get("large") or urls.get("medium")
        caption = f"Pixiv ID: {uid}\n标题: {title}\n作者: {user}"

        if image_url and hasattr(event, "image_result"):
            try:
                # 部分平台在发送 URL 图片时会走平台侧下载，容易超时
                if self.send_image_as_file and self.client:
                    raw = await self.client.get_raw_bytes(image_url)
                    ext = Path(urlparse(image_url).path).suffix or ".jpg"
                    tmp_dir = Path(tempfile.gettempdir()) / "astrbot_pixiv"
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    img_path = tmp_dir / f"pixiv_{uid}{ext}"
                    img_path.write_bytes(raw)
                    yield event.image_result(str(img_path))
                else:
                    yield event.image_result(image_url)

                yield event.plain_result(caption)
                return
            except Exception:
                pass

        yield event.plain_result((caption + "\n" + (image_url or "")).strip())

    async def _send_novel(self, event: AstrMessageEvent, novel: Dict[str, Any]):
        novel_id = int(novel.get("id", 0))
        title = novel.get("title", "untitled")
        author = (novel.get("user") or {}).get("name", "unknown")
        web_url = f"https://www.pixiv.net/novel/show.php?id={novel_id}"

        text = await self.client.novel_text(novel_id)
        if not text.strip():
            yield event.plain_result(f"未获取到小说正文，可直接查看：{web_url}")
            return

        safe_title = re.sub(r"[\\/:*?\"<>|]", "_", title)[:60] or f"novel_{novel_id}"
        txt_dir = Path(tempfile.gettempdir()) / "astrbot_pixiv"
        txt_dir.mkdir(parents=True, exist_ok=True)
        txt_path = txt_dir / f"{safe_title}_{novel_id}.txt"

        txt_path.write_text(
            f"标题: {title}\n作者: {author}\n链接: {web_url}\n\n{text}",
            encoding="utf-8",
        )

        if hasattr(event, "file_result"):
            try:
                yield event.file_result(str(txt_path))
                return
            except Exception:
                pass

        yield event.plain_result(f"文件发送失败，请直接查看：{web_url}")

    @filter.command("pix")
    async def pix(self, event: AstrMessageEvent):
        err = self._guard(False)
        if err:
            yield event.plain_result(err)
            return

        kw = self._keyword(event.message_str)
        try:
            item = await self.client.search_illust(kw, False)
        except httpx.ConnectTimeout:
            yield event.plain_result(
                "连接 Pixiv 超时。请检查服务器网络；如在国内服务器，请在插件配置里填写 pixiv_proxy。"
            )
            return
        except httpx.HTTPError as e:
            yield event.plain_result(f"Pixiv 请求失败：{str(e)[:120]}")
            return
        if not item:
            yield event.plain_result(f"没有找到普通插画：{kw}")
            return

        async for out in self._send_image(event, item):
            yield out

    @filter.command("pixr")
    async def pixr(self, event: AstrMessageEvent):
        err = self._guard(True)
        if err:
            yield event.plain_result(err)
            return

        kw = self._keyword(event.message_str)
        try:
            item = await self.client.search_illust(kw, True)
        except httpx.ConnectTimeout:
            yield event.plain_result(
                "连接 Pixiv 超时。请检查服务器网络；如在国内服务器，请在插件配置里填写 pixiv_proxy。"
            )
            return
        except httpx.HTTPError as e:
            yield event.plain_result(f"Pixiv 请求失败：{str(e)[:120]}")
            return
        if not item:
            yield event.plain_result(f"没有找到 R18 插画：{kw}")
            return

        async for out in self._send_image(event, item):
            yield out

    @filter.command("novel")
    async def novel(self, event: AstrMessageEvent):
        err = self._guard(False)
        if err:
            yield event.plain_result(err)
            return

        kw = self._keyword(event.message_str)
        try:
            item = await self.client.search_novel(kw, False)
        except httpx.ConnectTimeout:
            yield event.plain_result(
                "连接 Pixiv 超时。请检查服务器网络；如在国内服务器，请在插件配置里填写 pixiv_proxy。"
            )
            return
        except httpx.HTTPError as e:
            yield event.plain_result(f"Pixiv 请求失败：{str(e)[:120]}")
            return
        if not item:
            yield event.plain_result(f"没有找到普通小说：{kw}")
            return

        async for out in self._send_novel(event, item):
            yield out

    @filter.command("novelr")
    async def novelr(self, event: AstrMessageEvent):
        err = self._guard(True)
        if err:
            yield event.plain_result(err)
            return

        kw = self._keyword(event.message_str)
        try:
            item = await self.client.search_novel(kw, True)
        except httpx.ConnectTimeout:
            yield event.plain_result(
                "连接 Pixiv 超时。请检查服务器网络；如在国内服务器，请在插件配置里填写 pixiv_proxy。"
            )
            return
        except httpx.HTTPError as e:
            yield event.plain_result(f"Pixiv 请求失败：{str(e)[:120]}")
            return
        if not item:
            yield event.plain_result(f"没有找到 R18 小说：{kw}")
            return

        async for out in self._send_novel(event, item):
            yield out

