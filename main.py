from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PIXIV_OAUTH = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_API_BASE = "https://app-api.pixiv.net"
UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"


class PixivClient:
    def __init__(self, refresh_token: str):
        self.refresh_token = refresh_token.strip()
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
        async with httpx.AsyncClient(timeout=30) as client:
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
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{PIXIV_API_BASE}{path}", headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()

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
    "Pixiv 插件",
    "支持普通/R18插画与小说（txt）发送",
    "0.1.1",
    "claude",
)
class PixivPlugin(Star):
    def __init__(self, context: Context):
        # 某些 AstrBot 版本会在初始化流程中读取插件的 proxy 属性
        self.proxy = None
        super().__init__(context)
        self.refresh_token = ""
        self.allow_r18 = False
        self.client: Optional[PixivClient] = None
        self._load_conf()

    def _load_conf(self) -> None:
        conf: Dict[str, Any] = {}

        # 兼容不同 AstrBot 版本的配置读取方式
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

        token = str(conf.get("pixiv_refresh_token", "")).strip()
        allow = conf.get("allow_r18", False)
        if isinstance(allow, str):
            allow_flag = allow.strip().lower() in {"1", "true", "yes", "on"}
        else:
            allow_flag = bool(allow)

        self.refresh_token = token
        self.allow_r18 = allow_flag
        self.client = PixivClient(token) if token else None

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
        item = await self.client.search_illust(kw, False)
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
        item = await self.client.search_illust(kw, True)
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
        item = await self.client.search_novel(kw, False)
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
        item = await self.client.search_novel(kw, True)
        if not item:
            yield event.plain_result(f"没有找到 R18 小说：{kw}")
            return

        async for out in self._send_novel(event, item):
            yield out

