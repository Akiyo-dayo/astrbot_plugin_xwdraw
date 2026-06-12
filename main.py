import asyncio
import base64
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import aiohttp

from astrbot import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Node, Nodes, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent


DEFAULT_API_URL = "https://sd.loping151.com/api/generate"
PLUGIN_VERSION = "0.3.1"


class XWDrawApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


@dataclass
class GeneratedAsset:
    image_bytes: Optional[bytes] = None
    filename: Optional[str] = None
    url: Optional[str] = None
    date_folder: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0
    final_prompt: Optional[str] = None
    raw_response: Any = None


@dataclass
class ReviewDecision:
    allowed: bool
    level: str = "safe"
    source: str = "disabled"
    score: Optional[float] = None
    reason: str = ""


@dataclass
class GenerateArgs:
    prompt: str
    denoising_strength: float = 0.7
    is_r18: bool = False
    is_r18g: bool = False


class XWDrawApiClient:
    def __init__(self, api_url: str = DEFAULT_API_URL, api_key: str = "", timeout: int = 60):
        self.api_url = api_url or DEFAULT_API_URL
        self.api_key = api_key or ""
        self.timeout = int(timeout or 60)
        self.base_url = self.derive_base_url(self.api_url)

    @staticmethod
    def derive_base_url(api_url: str) -> str:
        if not api_url:
            return DEFAULT_API_URL.rsplit("/api/", 1)[0]
        parsed = urlparse(api_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        if "/api/" in api_url:
            return api_url.split("/api/", 1)[0].rstrip("/")
        return api_url.rstrip("/")

    @staticmethod
    def encode_path_segments(path: str) -> str:
        return "/".join(quote(unquote(part), safe="") for part in str(path).strip("/").split("/"))

    def endpoint_url(self, endpoint: str) -> str:
        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        return f"{self.base_url.rstrip('/')}{endpoint}"

    def image_url(self, filename_or_url: str) -> str:
        if not filename_or_url:
            return ""
        value = str(filename_or_url)
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return self.endpoint_url(f"/api/image/{self.encode_path_segments(value)}")

    def _auth_headers(self, auth: bool = True, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        out = dict(headers or {})
        if auth:
            if not self.api_key:
                raise XWDrawApiError("请在插件配置中填写 api_key 后再使用本功能。", status=401)
            out["Authorization"] = f"Bearer {self.api_key}"
        return out

    async def request_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Any = None,
        data: Any = None,
        auth: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        req_headers = self._auth_headers(auth=auth, headers=headers)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                self.endpoint_url(endpoint),
                params=params,
                json=json_data,
                data=data,
                headers=req_headers,
            ) as resp:
                text = await resp.text()
                payload = self._decode_json_text(text)
                if resp.status >= 400:
                    raise XWDrawApiError(self._error_message(payload, text, resp.status), resp.status, payload)
                return payload if payload is not None else text

    async def request_bytes(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[bytes, str]:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        req_headers = self._auth_headers(auth=auth, headers=headers)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                self.endpoint_url(endpoint),
                params=params,
                headers=req_headers,
            ) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    text = body.decode("utf-8", errors="replace")
                    payload = self._decode_json_text(text)
                    raise XWDrawApiError(self._error_message(payload, text, resp.status), resp.status, payload)
                return body, resp.headers.get("Content-Type", "")

    async def fetch_url_bytes(self, url: str, *, auth: bool = False) -> bytes:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        req_headers = self._auth_headers(auth=auth) if auth else {}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=req_headers) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    text = body.decode("utf-8", errors="replace")
                    payload = self._decode_json_text(text)
                    raise XWDrawApiError(self._error_message(payload, text, resp.status), resp.status, payload)
                return body

    async def download_image(self, filename_or_url: str) -> Optional[bytes]:
        attempts = self._download_attempts(filename_or_url)
        seen = set()
        for url in attempts:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                return await self.fetch_url_bytes(url, auth=True)
            except Exception as exc:
                logger.debug(f"图片下载尝试失败 ({url}): {exc}")
        return None

    def _download_attempts(self, filename_or_url: str) -> List[str]:
        url = self.image_url(filename_or_url)
        attempts = [url]
        try:
            parsed = urlparse(url)
            root = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
            path = parsed.path.lstrip("/")
            unquoted = unquote(path)
            if root and unquoted != path:
                attempts.append(f"{root}/{unquoted}")
            encoded = self.encode_path_segments(unquoted)
            if root:
                attempts.append(f"{root}/{encoded}")
            parts = unquoted.split("/")
            if root and parts:
                attempts.append(f"{root}/{'/'.join(parts[:-1] + [quote(unquote(parts[-1]), safe='')])}")
        except Exception:
            pass
        return attempts

    async def generate(self, payload: Dict[str, Any]) -> GeneratedAsset:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = self._auth_headers(headers={"Content-Type": "application/json"})
        start_ts = time.time()
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.api_url, json=payload, headers=headers) as resp:
                elapsed = time.time() - start_ts
                content_type = resp.headers.get("Content-Type", "").lower()
                if resp.status == 200 and content_type.startswith("image/"):
                    return GeneratedAsset(image_bytes=await resp.read(), elapsed=elapsed)

                text = await resp.text()
                result = self._decode_json_text(text)
                if resp.status >= 400:
                    raise XWDrawApiError(self._error_message(result, text, resp.status), resp.status, result)
                asset = await self._asset_from_generation_result(result if result is not None else text, elapsed)
                return asset

    async def _asset_from_generation_result(self, result: Any, elapsed: float) -> GeneratedAsset:
        asset = GeneratedAsset(elapsed=elapsed, raw_response=result)
        if not isinstance(result, dict):
            return asset

        asset.metadata = self._extract_metadata(result)
        asset.final_prompt = self._first_string(result, "final_prompt", "actual_prompt", "translated_prompt", "prompt")
        image_b64 = self._first_string(result, "image_base64", "image_data", "base64")
        if image_b64:
            asset.image_bytes = self._decode_image_base64(image_b64)

        image_ref = self._first_string(result, "filename", "file", "url", "image_url", "path")
        if image_ref:
            asset.filename = image_ref
            asset.url = self.image_url(image_ref)
            asset.date_folder, filename = self.split_image_ref(image_ref)
            if filename:
                asset.filename = filename
            if not asset.image_bytes:
                asset.image_bytes = await self.download_image(image_ref)
            if asset.date_folder and asset.filename:
                try:
                    metadata = await self.image_metadata(asset.date_folder, asset.filename)
                    if isinstance(metadata, dict):
                        asset.metadata.update(metadata)
                except Exception as exc:
                    logger.debug(f"读取图片 metadata 失败: {exc}")

        return asset

    @staticmethod
    def _extract_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        for key in ("metadata", "image_metadata", "meta"):
            if isinstance(result.get(key), dict):
                metadata.update(result[key])
        for key in (
            "is_r18",
            "is_r18g",
            "r18",
            "r18g",
            "nsfw",
            "nsfw_score",
            "score",
            "status",
            "user",
            "username",
            "date_folder",
            "final_prompt",
        ):
            if key in result and key not in metadata:
                metadata[key] = result[key]
        return metadata

    @staticmethod
    def _first_string(data: Dict[str, Any], *keys: str) -> Optional[str]:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _decode_json_text(text: str) -> Any:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _decode_image_base64(value: str) -> Optional[bytes]:
        try:
            data = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
            return base64.b64decode(data)
        except Exception:
            return None

    @staticmethod
    def _error_message(payload: Any, text: str, status: int) -> str:
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("message") or payload.get("error")
            if detail:
                return f"绘图服务返回错误 (HTTP {status})：{detail}"
        return f"绘图服务返回错误 (HTTP {status})：{text[:500]}"

    @staticmethod
    def split_image_ref(filename_or_url: str) -> Tuple[Optional[str], Optional[str]]:
        if not filename_or_url:
            return None, None
        value = str(filename_or_url)
        try:
            if value.startswith("http://") or value.startswith("https://"):
                path = unquote(urlparse(value).path)
                if "/api/image/" in path:
                    value = path.split("/api/image/", 1)[1]
                else:
                    value = path.strip("/")
            value = unquote(value).strip("/")
            parts = value.split("/")
            if len(parts) >= 2:
                return parts[-2], parts[-1]
            return None, parts[-1] if parts else None
        except Exception:
            return None, None

    async def verify(self) -> Any:
        return await self.request_json("POST", "/api/verify")

    async def generation_config(self) -> Any:
        return await self.request_json("GET", "/api/generation-config")

    async def queue_status(self) -> Any:
        return await self.request_json("GET", "/api/queue-status", auth=False)

    async def announcements(self) -> Any:
        return await self.request_json("GET", "/api/announcements", auth=False)

    async def presets(self) -> Any:
        return await self.request_json("GET", "/api/presets")

    async def preset_detail(self, preset_type: str, preset_name: str) -> Any:
        return await self.request_json("GET", f"/api/preset-detail/{quote(preset_type, safe='')}/{quote(preset_name, safe='')}")

    async def preset_image(self, preset_type: str, image_name: str) -> Tuple[bytes, str]:
        return await self.request_bytes("GET", f"/api/preset-image/{quote(preset_type, safe='')}/{quote(image_name, safe='')}")

    async def user_presets(self) -> Any:
        return await self.request_json("GET", "/api/user-presets")

    async def add_user_preset(self, preset_name: str, preset_content: str) -> Any:
        return await self.request_json(
            "POST",
            "/api/user-presets",
            json_data={"preset_name": preset_name, "preset_content": preset_content},
        )

    async def delete_user_preset(self, preset_name: str) -> Any:
        return await self.request_json("DELETE", f"/api/user-presets/{quote(preset_name, safe='')}")

    async def recommended_prompts(self) -> Any:
        return await self.request_json("GET", "/api/recommended-prompts")

    async def recent_images(self, limit: int = 12, exclude_r18: bool = True, exclude_r18g: bool = True) -> Any:
        return await self.request_json(
            "GET",
            "/api/recent-images",
            params={"limit": limit, "exclude_r18": str(exclude_r18).lower(), "exclude_r18g": str(exclude_r18g).lower()},
        )

    async def image_metadata(self, date_folder: str, filename: str) -> Any:
        return await self.request_json(
            "GET",
            f"/api/image-metadata/{quote(date_folder, safe='')}/{quote(filename, safe='')}",
        )

    async def update_image_tags(self, date_folder: str, filename: str, is_r18: Optional[bool], is_r18g: Optional[bool]) -> Any:
        body: Dict[str, Any] = {}
        if is_r18 is not None:
            body["is_r18"] = is_r18
        if is_r18g is not None:
            body["is_r18g"] = is_r18g
        return await self.request_json(
            "PUT",
            f"/api/image-tags/{quote(date_folder, safe='')}/{quote(filename, safe='')}",
            json_data=body,
        )

    async def gallery_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = self._auth_headers()
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar()) as session:
            async with session.post(self.endpoint_url("/gallery/login"), headers=headers) as login_resp:
                login_text = await login_resp.text()
                login_payload = self._decode_json_text(login_text)
                if login_resp.status >= 400:
                    raise XWDrawApiError(self._error_message(login_payload, login_text, login_resp.status), login_resp.status, login_payload)
            async with session.get(self.endpoint_url(endpoint), params=params, headers=headers) as resp:
                text = await resp.text()
                payload = self._decode_json_text(text)
                if resp.status >= 400:
                    raise XWDrawApiError(self._error_message(payload, text, resp.status), resp.status, payload)
                return payload if payload is not None else text

    async def video_generate(
        self,
        image_bytes: bytes,
        prompt: str,
        negative_prompt: str = "",
        duration: str = "4",
        fps: str = "16",
    ) -> Any:
        form = aiohttp.FormData()
        form.add_field("file", image_bytes, filename="source.png", content_type="image/png")
        form.add_field("prompt", prompt)
        form.add_field("negative_prompt", negative_prompt)
        form.add_field("duration", str(duration))
        form.add_field("fps", str(fps))
        return await self.request_json("POST", "/api/video/generate", data=form)

    async def video_history(self) -> Any:
        return await self.request_json("GET", "/api/video/history")

    async def video_file(self, timestamp: str, username: str, kind: str = "video") -> Tuple[bytes, str]:
        if kind == "thumbnail":
            endpoint = f"/api/video/thumbnail/{quote(timestamp, safe='')}/{quote(username, safe='')}"
        elif kind == "last-frame":
            endpoint = f"/api/video/last-frame/{quote(timestamp, safe='')}/{quote(username, safe='')}"
        elif kind == "source-image":
            endpoint = f"/api/video/source-image/{quote(timestamp, safe='')}/{quote(username, safe='')}"
        else:
            endpoint = f"/api/video/{quote(timestamp, safe='')}/{quote(username, safe='')}"
        return await self.request_bytes("GET", endpoint)

    async def document(self, doc_name: str) -> Tuple[bytes, str]:
        return await self.request_bytes("GET", f"/api/document/{quote(doc_name, safe='')}")

    async def costume_image(self, costume_name: str) -> Tuple[bytes, str]:
        return await self.request_bytes("GET", f"/costume/{quote(costume_name, safe='')}")


@register(
    "astrbot_plugin_xwdraw",
    "xwdraw",
    "集成小维远端 SD 绘图服务，支持图片/视频生成、预设、画廊、公告、队列和 R18 自审",
    PLUGIN_VERSION,
    "https://github.com/Akiyo-dayo/astrbot_plugin_xwdraw",
)
class XWDrawPlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.conf = config
        self.plugin_data_dir = self._resolve_data_dir()
        self.presets_cache: Optional[Dict[str, Any]] = None
        self.cache_time = 0.0
        self.cache_duration = 300
        self.switch_state_path = self.plugin_data_dir / "switches.json"
        self.switch_state = self._load_switch_state()
        self.client = self._build_client()

    async def initialize(self):
        logger.info(
            f"astrbot_plugin_xwdraw v{PLUGIN_VERSION} 已加载, "
            f"api_url={self._conf('api_url', DEFAULT_API_URL)}, timeout={self._conf('timeout', 60)}s"
        )

    def _resolve_data_dir(self) -> Path:
        try:
            data_dir = Path(StarTools.get_data_dir())
        except Exception:
            data_dir = Path("./data")
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return data_dir

    def _build_client(self) -> XWDrawApiClient:
        return XWDrawApiClient(
            api_url=str(self._conf("api_url", DEFAULT_API_URL)),
            api_key=str(self._conf("api_key", "")),
            timeout=self._int_conf("timeout", 60),
        )

    def _api(self) -> XWDrawApiClient:
        return self.client

    def _conf(self, key: str, default: Any = None) -> Any:
        try:
            return self.conf.get(key, default)
        except Exception:
            return default

    def _bool_conf(self, key: str, default: bool = False) -> bool:
        value = self._conf(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "是"}

    def _int_conf(self, key: str, default: int) -> int:
        try:
            return int(self._conf(key, default))
        except Exception:
            return default

    def _float_conf(self, key: str, default: float) -> float:
        try:
            return float(self._conf(key, default))
        except Exception:
            return default

    def _list_conf(self, key: str) -> List[str]:
        value = self._conf(key, [])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value or "").split(",") if item.strip()]

    def _load_switch_state(self) -> Dict[str, Any]:
        try:
            if self.switch_state_path.exists():
                data = json.loads(self.switch_state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("session_overrides", {})
                    return data
        except Exception as exc:
            logger.warning(f"读取绘图开关状态失败: {exc}")
        return {"session_overrides": {}}

    def _save_switch_state(self):
        try:
            self.switch_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.switch_state_path.write_text(json.dumps(self.switch_state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"保存绘图开关状态失败: {exc}")

    def _session_switch_overrides(self) -> Dict[str, bool]:
        overrides = self.switch_state.setdefault("session_overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
            self.switch_state["session_overrides"] = overrides
        return overrides

    def _is_plugin_enabled_for_event(self, event: AstrMessageEvent) -> bool:
        session_id = self._session_id(event)
        overrides = self._session_switch_overrides()
        if session_id and session_id in overrides:
            return bool(overrides[session_id])
        return self._bool_conf("plugin_enabled", True)

    def _switch_status_text(self, event: AstrMessageEvent) -> str:
        session_id = self._session_id(event) or "unknown"
        overrides = self._session_switch_overrides()
        if session_id in overrides:
            source = "当前会话设置"
            enabled = bool(overrides[session_id])
        else:
            source = "配置默认值"
            enabled = self._bool_conf("plugin_enabled", True)
        default_text = "开启" if self._bool_conf("plugin_enabled", True) else "关闭"
        current_text = "开启" if enabled else "关闭"
        return f"绘图总开关：{current_text}\n作用范围：当前会话/群 ({session_id})\n状态来源：{source}\n配置默认：{default_text}"

    def _set_session_switch(self, event: AstrMessageEvent, enabled: bool):
        session_id = self._session_id(event)
        if not session_id:
            raise XWDrawApiError("无法识别当前会话，不能保存绘图开关状态。")
        self._session_switch_overrides()[session_id] = enabled
        self._save_switch_state()

    def _disabled_result(self, event: AstrMessageEvent):
        if self._is_plugin_enabled_for_event(event):
            return None
        return event.plain_result("本群/当前会话的绘图插件总开关已关闭。请联系群管理员发送 `绘图开启` 后再使用。")

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _sender_id(self, event: AstrMessageEvent) -> str:
        for method in ("get_sender_id", "get_user_id"):
            try:
                value = getattr(event, method)()
                if value:
                    return str(value)
            except Exception:
                pass
        for obj in self._event_candidate_objects(event):
            for attr in ("user_id", "sender_id", "id", "uin", "qq"):
                value = self._get_attr_or_key(obj, attr)
                if value:
                    return str(value)
        return ""

    def _event_candidate_objects(self, event: AstrMessageEvent) -> List[Any]:
        message_obj = getattr(event, "message_obj", None)
        candidates: List[Any] = [
            event,
            getattr(event, "sender", None),
            message_obj,
            getattr(message_obj, "sender", None),
        ]
        raw_message = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            candidates.append(raw_message)
            candidates.append(raw_message.get("sender"))
        return [item for item in candidates if item is not None]

    @staticmethod
    def _get_attr_or_key(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _sender_role_values(self, event: AstrMessageEvent) -> List[str]:
        values: List[str] = []
        for obj in self._event_candidate_objects(event):
            for attr in ("role", "permission", "user_role", "sender_role", "group_role"):
                value = self._get_attr_or_key(obj, attr)
                if value:
                    values.append(str(value).strip().lower())
            for attr in ("is_admin", "admin", "is_owner", "owner"):
                value = self._get_attr_or_key(obj, attr)
                if self._truthy(value):
                    values.append("admin")
        return values

    async def _is_switch_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = self._sender_id(event)
        if sender_id and sender_id in self._list_conf("switch_admin_user_ids"):
            return True

        try:
            is_admin_attr = getattr(event, "is_admin", None)
            is_admin = await self._maybe_await(is_admin_attr() if callable(is_admin_attr) else is_admin_attr)
            if self._truthy(is_admin):
                return True
        except Exception:
            pass

        if self._bool_conf("group_admin_can_toggle", True):
            roles = set(self._sender_role_values(event))
            if roles.intersection({"owner", "admin", "administrator", "群主", "管理员"}):
                return True
        return False

    @staticmethod
    def _parse_switch_action(raw: str) -> Optional[bool]:
        tokens = raw.strip().split()
        command = tokens[0] if tokens else ""
        if any(word in command for word in ("开启", "打开", "启用")):
            return True
        if any(word in command for word in ("关闭", "禁用", "停止")):
            return False
        if len(tokens) < 2:
            return None
        value = tokens[1].strip().lower()
        if value in {"开", "开启", "打开", "启用", "on", "enable", "enabled", "true", "1"}:
            return True
        if value in {"关", "关闭", "禁用", "停止", "off", "disable", "disabled", "false", "0"}:
            return False
        return None

    @staticmethod
    def parse_generate_args(prompt_line: str) -> GenerateArgs:
        text = prompt_line.strip()
        is_r18g = bool(re.search(r"(^|\s)--r18g(\s|$)", text, flags=re.I))
        is_r18 = bool(re.search(r"(^|\s)--r18(\s|$)", text, flags=re.I)) or is_r18g
        text = re.sub(r"(^|\s)--r18g(\s|$)", " ", text, flags=re.I)
        text = re.sub(r"(^|\s)--r18(\s|$)", " ", text, flags=re.I)

        denoise = 0.7
        match = re.search(r"(?:^|\s)-d\s*([0-9]*\.?[0-9]+)", text)
        if match:
            try:
                denoise = max(0.0, min(1.0, float(match.group(1))))
            except Exception:
                denoise = 0.7
            text = re.sub(r"(?:^|\s)-d\s*[0-9]*\.?[0-9]+", " ", text, count=1)

        return GenerateArgs(prompt=re.sub(r"\s+", " ", text).strip(), denoising_strength=denoise, is_r18=is_r18, is_r18g=is_r18g)

    async def _fetch_image_bytes(self, url_or_path: str, timeout: int, headers: Optional[Dict[str, str]] = None) -> Optional[bytes]:
        try:
            if not url_or_path:
                return None
            if Path(str(url_or_path)).is_file():
                return Path(str(url_or_path)).read_bytes()
            if str(url_or_path).startswith("http"):
                timeout_obj = aiohttp.ClientTimeout(total=timeout)
                async with aiohttp.ClientSession(timeout=timeout_obj) as session:
                    async with session.get(str(url_or_path), headers=headers) as resp:
                        resp.raise_for_status()
                        return await resp.read()
        except Exception as exc:
            logger.warning(f"_fetch_image_bytes 失败: {exc}")
        return None

    async def _get_image_from_event(self, event: AstrMessageEvent, timeout: int) -> Optional[bytes]:
        segments = getattr(getattr(event, "message_obj", None), "message", []) or []
        for seg in segments:
            if isinstance(seg, Reply) and getattr(seg, "chain", None):
                for child in seg.chain:
                    image_bytes = await self._image_segment_bytes(child, timeout)
                    if image_bytes:
                        return image_bytes
        for seg in segments:
            image_bytes = await self._image_segment_bytes(seg, timeout)
            if image_bytes:
                return image_bytes
        return None

    async def _image_segment_bytes(self, seg: Any, timeout: int) -> Optional[bytes]:
        if not isinstance(seg, Image):
            return None
        image_src = getattr(seg, "url", None) or getattr(seg, "file", None) or getattr(seg, "path", None)
        return await self._fetch_image_bytes(str(image_src), timeout) if image_src else None

    def _build_image_url(self, api_url: str, filename: str) -> str:
        return XWDrawApiClient(api_url=api_url, api_key="", timeout=60).image_url(filename)

    async def _download_with_fallbacks(self, url: str, timeout: int, api_key: Optional[str] = None) -> Optional[bytes]:
        return await XWDrawApiClient(self._conf("api_url", DEFAULT_API_URL), api_key or self._conf("api_key", ""), timeout).download_image(url)

    async def _get_presets(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        current_time = time.time()
        if not force_refresh and self.presets_cache and (current_time - self.cache_time < self.cache_duration):
            return self.presets_cache
        try:
            presets = await self._api().presets()
            if isinstance(presets, dict):
                self.presets_cache = presets
                self.cache_time = current_time
                return presets
        except Exception as exc:
            logger.error(f"获取预设失败: {exc}")
        return None

    async def _api_request(self, method: str, endpoint: str, **kwargs) -> Any:
        json_data = kwargs.pop("json", None)
        return await self._api().request_json(method, endpoint, json_data=json_data, **kwargs)

    def _session_id(self, event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            value = getattr(event, attr, None)
            if value:
                return str(value)
        try:
            return str(event.get_group_id())
        except Exception:
            return ""

    def _self_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_self_id())
        except Exception:
            return "0"

    async def _review_generated_asset(self, asset: GeneratedAsset, event: Optional[AstrMessageEvent] = None) -> ReviewDecision:
        if not self._bool_conf("r18_review_enabled", True):
            return ReviewDecision(True, source="disabled", reason="审查已关闭")

        if event and self._session_id(event) in self._list_conf("r18_allowed_session_ids"):
            return ReviewDecision(True, source="allowed_session", reason="当前会话在放行列表中")

        service_decision = self._review_service_metadata(asset.metadata)
        if not service_decision.allowed:
            return service_decision

        if self._bool_conf("external_review_enabled", False):
            external = await self._external_review(asset.image_bytes, asset.metadata)
            if not external.allowed:
                return external

        return service_decision

    def _review_service_metadata(self, metadata: Dict[str, Any]) -> ReviewDecision:
        block_r18 = self._bool_conf("r18_block_r18", True)
        block_r18g = self._bool_conf("r18_block_r18g", True)
        threshold = self._float_conf("r18_nsfw_score_threshold", 0.65)
        is_r18 = self._truthy(metadata.get("is_r18", metadata.get("r18", metadata.get("nsfw"))))
        is_r18g = self._truthy(metadata.get("is_r18g", metadata.get("r18g")))
        score = self._float_value(metadata.get("nsfw_score", metadata.get("score")))

        if block_r18g and is_r18g:
            return ReviewDecision(False, level="r18g", source="service_metadata", score=score, reason="服务 metadata 标记为 R18G")
        if block_r18 and is_r18:
            return ReviewDecision(False, level="r18", source="service_metadata", score=score, reason="服务 metadata 标记为 R18")
        if score is not None and score >= threshold:
            return ReviewDecision(False, level="r18", source="service_metadata", score=score, reason=f"nsfw_score={score:.3f} 超过阈值 {threshold:.3f}")
        return ReviewDecision(True, level="safe", source="service_metadata", score=score, reason="metadata 未触发拦截")

    async def _external_review(self, image_bytes: Optional[bytes], metadata: Dict[str, Any]) -> ReviewDecision:
        if not image_bytes:
            if self._bool_conf("external_review_fail_closed", True):
                return ReviewDecision(False, level="unknown", source="external_review", reason="外部审核启用但没有图片数据")
            return ReviewDecision(True, source="external_review", reason="无图片数据，按配置放行")

        api_url = str(self._conf("external_review_api_url", "") or "").strip()
        if not api_url:
            if self._bool_conf("external_review_fail_closed", True):
                return ReviewDecision(False, level="unknown", source="external_review", reason="外部审核启用但未配置接口地址")
            return ReviewDecision(True, source="external_review", reason="未配置外部审核接口，按配置放行")

        payload = {
            "model": self._conf("external_review_model", ""),
            "image": "data:image/png;base64," + base64.b64encode(image_bytes).decode("utf-8"),
            "image_base64": base64.b64encode(image_bytes).decode("utf-8"),
            "metadata": metadata,
        }
        headers = {"Content-Type": "application/json"}
        api_key = str(self._conf("external_review_api_key", "") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            timeout = aiohttp.ClientTimeout(total=self._int_conf("external_review_timeout", 30))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    result = XWDrawApiClient._decode_json_text(text)
                    if resp.status >= 400:
                        raise XWDrawApiError(f"外部审核接口返回 HTTP {resp.status}: {text[:300]}", resp.status, result)
                    if not isinstance(result, dict):
                        raise XWDrawApiError("外部审核接口没有返回 JSON 对象")
                    return self._decision_from_external_payload(result)
        except Exception as exc:
            logger.warning(f"外部审核失败: {exc}")
            if self._bool_conf("external_review_fail_closed", True):
                return ReviewDecision(False, level="unknown", source="external_review", reason=f"外部审核失败: {exc}")
            return ReviewDecision(True, source="external_review", reason=f"外部审核失败但按配置放行: {exc}")

    def _decision_from_external_payload(self, payload: Dict[str, Any]) -> ReviewDecision:
        label = str(payload.get("level") or payload.get("label") or payload.get("category") or "").lower()
        reason = str(payload.get("reason") or payload.get("message") or "外部审核判定")
        score = self._float_value(payload.get("score", payload.get("nsfw_score")))
        safe = payload.get("safe")
        is_r18g = self._truthy(payload.get("r18g", payload.get("is_r18g"))) or label in {"r18g", "gore", "violence"}
        is_r18 = self._truthy(payload.get("r18", payload.get("is_r18", payload.get("nsfw")))) or label in {"r18", "adult", "nsfw", "sexual"}
        if is_r18g and self._bool_conf("r18_block_r18g", True):
            return ReviewDecision(False, level="r18g", source="external_review", score=score, reason=reason)
        if is_r18 and self._bool_conf("r18_block_r18", True):
            return ReviewDecision(False, level="r18", source="external_review", score=score, reason=reason)
        if safe is False:
            return ReviewDecision(False, level=label or "unsafe", source="external_review", score=score, reason=reason)
        return ReviewDecision(True, level=label or "safe", source="external_review", score=score, reason=reason)

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "r18", "r18g", "nsfw", "adult"}

    @staticmethod
    def _float_value(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except Exception:
            return None

    async def _send_image_bytes(self, event: AstrMessageEvent, image_bytes: bytes, caption: str, filename: Optional[str] = None):
        try:
            if hasattr(Image, "fromBytes"):
                return event.chain_result([Image.fromBytes(image_bytes), Plain(caption)])
        except Exception as exc:
            logger.debug(f"Image.fromBytes 发送失败: {exc}")

        out_dir = self.plugin_data_dir / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / self._safe_filename(filename or f"generated_{int(time.time())}.png")
        out_path.write_bytes(image_bytes)
        try:
            if hasattr(Image, "fromFileSystem"):
                return event.chain_result([Image.fromFileSystem(str(out_path)), Plain(caption)])
        except Exception as exc:
            logger.debug(f"Image.fromFileSystem 发送失败: {exc}")
        try:
            message_chain = MessageChain().file_image(str(out_path))
            await self.context.send_message(event.unified_msg_origin, message_chain)
            return event.plain_result(caption)
        except Exception as exc:
            logger.error(f"发送图片失败: {exc}")
            return event.plain_result(f"生成成功，但发送图片失败: {exc}")

    async def _send_generated_asset(self, event: AstrMessageEvent, asset: GeneratedAsset):
        decision = await self._review_generated_asset(asset, event)
        if not decision.allowed:
            logger.warning(
                f"图片生成结果已拦截: source={decision.source}, level={decision.level}, "
                f"score={decision.score}, reason={decision.reason}, filename={asset.filename}"
            )
            score_text = f"，score={decision.score:.3f}" if decision.score is not None else ""
            return event.plain_result(f"生成完成，但自审判定为 {decision.level}，已停止发送图片（{decision.reason}{score_text}）。")

        caption = f"生成成功 ({asset.elapsed:.1f}s)"
        if asset.final_prompt:
            caption += f"\n实际提示词: {self._truncate(asset.final_prompt, 120)}"
        if asset.image_bytes:
            return await self._send_image_bytes(event, asset.image_bytes, caption, asset.filename)
        return event.plain_result(f"生成完成 ({asset.elapsed:.1f}s)，但没有拿到可发送的图片数据。")

    async def _send_file_bytes(self, event: AstrMessageEvent, data: bytes, filename: str, content_type: str = ""):
        out_dir = self.plugin_data_dir / "downloads"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / self._safe_filename(filename)
        out_path.write_bytes(data)
        try:
            chain = MessageChain()
            if hasattr(chain, "file"):
                chain.file(str(out_path))
                await self.context.send_message(event.unified_msg_origin, chain)
                return event.plain_result(f"文件已发送：{out_path.name}")
        except Exception as exc:
            logger.debug(f"通用文件发送失败: {exc}")
        return event.plain_result(f"文件已保存：{out_path}")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(str(filename)).name or f"file_{int(time.time())}"
        return re.sub(r'[<>:"/\\|?*]+', "_", name)

    @staticmethod
    def _truncate(text: Any, limit: int = 1600) -> str:
        value = str(text)
        return value if len(value) <= limit else value[:limit] + "..."

    def _format_data(self, data: Any, limit: int = 1800) -> str:
        if isinstance(data, (dict, list)):
            text = json.dumps(data, ensure_ascii=False, indent=2)
        else:
            text = str(data)
        return self._truncate(text, limit)

    def _format_items(self, data: Any, title: str, limit: int = 10) -> str:
        items = data
        if isinstance(data, dict):
            for key in ("images", "items", "results", "videos", "announcements", "prompts"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
        if not isinstance(items, list):
            return f"{title}\n{self._format_data(data)}"
        lines = [f"{title} (显示 {min(len(items), limit)}/{len(items)} 条)"]
        for idx, item in enumerate(items[:limit], 1):
            if isinstance(item, dict):
                name = item.get("filename") or item.get("file") or item.get("title") or item.get("prompt") or item.get("timestamp") or item.get("id") or "未命名"
                extra = []
                for key in ("date", "username", "user", "status", "is_r18", "is_r18g", "nsfw_score"):
                    if key in item:
                        extra.append(f"{key}={item[key]}")
                lines.append(f"{idx}. {self._truncate(name, 80)}" + (f" ({', '.join(extra)})" if extra else ""))
            else:
                lines.append(f"{idx}. {self._truncate(item, 120)}")
        return "\n".join(lines)

    @staticmethod
    def _parse_limit(tokens: List[str], default: int = 12, max_value: int = 50) -> int:
        for token in tokens:
            if token.isdigit():
                return max(1, min(max_value, int(token)))
        return default

    @staticmethod
    def _parse_bool_token(value: str) -> Optional[bool]:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "是", "开"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "否", "关"}:
            return False
        return None

    def _extract_image_ref(self, raw: str) -> Tuple[Optional[str], Optional[str]]:
        parts = raw.strip().split(maxsplit=2)
        if len(parts) < 2:
            return None, None
        if "/" in parts[1]:
            return XWDrawApiClient.split_image_ref(parts[1])
        if len(parts) >= 3:
            return parts[1], parts[2].split(maxsplit=1)[0]
        return None, parts[1]

    async def _handle_api_error(self, event: AstrMessageEvent, exc: Exception):
        if isinstance(exc, XWDrawApiError):
            return event.plain_result(f"请求失败：{exc.message}")
        if isinstance(exc, asyncio.TimeoutError):
            return event.plain_result(f"请求超时（>{self._int_conf('timeout', 60)}s），请稍后重试或增加 timeout。")
        logger.exception("XWDraw 命令执行失败")
        return event.plain_result(f"发生未知错误：{exc}")

    @filter.command("绘图开关", aliases={"绘图状态", "绘图开启", "绘图关闭", "xw开关", "xwdraw_switch"}, prefix_optional=True)
    async def on_plugin_switch(self, event: AstrMessageEvent):
        action = self._parse_switch_action(event.message_str)
        if action is None:
            yield event.plain_result(self._switch_status_text(event) + "\n用法：绘图开启 / 绘图关闭 / 绘图开关 开|关")
            return

        if not await self._is_switch_admin(event):
            yield event.plain_result("只有群管理员/群主或配置中的开关管理员可以修改绘图总开关。")
            return

        try:
            self._set_session_switch(event, action)
            state_text = "开启" if action else "关闭"
            yield event.plain_result(f"已{state_text}当前群/会话的绘图插件总开关。\n{self._switch_status_text(event)}")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("测试来点", aliases={"test_xwdraw"}, prefix_optional=True)
    async def on_test_generate(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        raw = event.message_str.strip()
        parts = raw.split(maxsplit=1)
        prompt = parts[1].strip() if len(parts) > 1 else "无提示词"
        img_bytes = await self._get_image_from_event(event, self._int_conf("timeout", 60))
        msg_chain = [Plain(f"测试模式\n提示词: {prompt}\n")]
        if img_bytes:
            msg_chain.append(Plain("检测到图片输入，已成功获取图片数据。\n"))
            if hasattr(Image, "fromBytes"):
                msg_chain.append(Image.fromBytes(img_bytes))
        else:
            msg_chain.append(Plain("未检测到图片输入 (文生图模式)"))
        yield event.chain_result(msg_chain)

    @filter.command("来点", aliases={"小千来点", "xwdraw"}, prefix_optional=True)
    async def on_generate(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        raw = event.message_str.strip()
        parts = raw.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result("用法：小千来点 <提示词> [--r18] [--r18g] [-d 0.6]\n提示：可以附带图片进行图生图。")
            return

        args = self.parse_generate_args(parts[1])
        if not args.prompt:
            yield event.plain_result("提示词不能为空。")
            return
        if not self._api().api_key:
            yield event.plain_result("请在插件配置中填写 api_key 后再使用本功能。")
            return

        try:
            img_bytes = await self._get_image_from_event(event, self._int_conf("timeout", 60))
            mode_text = "图生图" if img_bytes else "文生图"
            yield event.plain_result(f"收到请求，正在{mode_text}生成 [{self._truncate(args.prompt, 24)}]\n感谢由小维151提供的绘图服务支持")

            payload: Dict[str, Any] = {"prompt": args.prompt, "is_r18": args.is_r18, "is_r18g": args.is_r18g}
            if img_bytes:
                payload["image"] = "data:image/png;base64," + base64.b64encode(img_bytes).decode("utf-8")
                payload["denoising_strength"] = args.denoising_strength

            asset = await self._api().generate(payload)
            if args.is_r18 and "is_r18" not in asset.metadata:
                asset.metadata["is_r18"] = True
            if args.is_r18g and "is_r18g" not in asset.metadata:
                asset.metadata["is_r18g"] = True
            yield await self._send_generated_asset(event, asset)
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("绘图帮助", aliases={"xw帮助", "xwhelp"}, prefix_optional=True)
    async def on_help(self, event: AstrMessageEvent):
        help_text = """XW绘图插件使用指南

【图片生成】
来点/小千来点 <提示词> [--r18] [--r18g] [-d 0.6]
测试来点 <提示词>
绘图开关 / 绘图开启 / 绘图关闭 - 群管理员控制当前群总开关

【服务状态】
绘图账号 / 绘图配额
生成配置
绘图公告
绘图队列
推荐提示

【预设】
预设列表
角色列表 / 风格列表 / 服装列表
预设详情 <character|style|costume> <名称>
预设图片 <类型> <图片名>
添加预设 <名称>|<内容>
删除预设 <名称>
我的预设
服装预览 <名称>

【图库】
最近图片 [数量] [--r18] [--r18g]
图片元数据 <日期>/<文件名>
更新图片标签 <日期>/<文件名> r18=true r18g=false
画廊列表 [页码] [关键词] [--r18] [--r18g]
画廊筛选

【视频】
视频生成 <提示词> [-t 秒] [-fps 帧率] [-n 负面提示词]（需附带图片）
视频历史
视频查看/视频缩略图/视频末帧/视频源图 <timestamp> <username>

【文档】
绘图文档 [涩涩词条大全|常规法典|色色法典]
"""
        yield event.plain_result(help_text)

    @filter.command("绘图账号", aliases={"绘图配额", "xw账号"}, prefix_optional=True)
    async def on_account(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().verify()
            yield event.plain_result("绘图账号信息\n" + self._format_data(data))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("生成配置", aliases={"绘图配置", "generation_config"}, prefix_optional=True)
    async def on_generation_config(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().generation_config()
            yield event.plain_result("生成配置\n" + self._format_data(data))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("绘图公告", aliases={"xw公告"}, prefix_optional=True)
    async def on_announcements(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().announcements()
            yield event.plain_result(self._format_items(data, "绘图公告", limit=8))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("绘图队列", aliases={"队列状态", "xw队列"}, prefix_optional=True)
    async def on_queue_status(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().queue_status()
            count = data.get("queue_count") if isinstance(data, dict) else data
            yield event.plain_result(f"当前绘图排队数：{count}")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("推荐提示", aliases={"推荐prompt", "推荐prompts"}, prefix_optional=True)
    async def on_recommended_prompts(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().recommended_prompts()
            yield event.plain_result(self._format_items(data, "推荐提示", limit=12))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("预设列表", aliases={"presets", "预设"}, prefix_optional=True)
    async def on_list_presets(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        presets = await self._get_presets()
        if not presets:
            yield event.plain_result("获取预设列表失败，请稍后重试。")
            return
        msg = "预设分类列表\n\n"
        if "character_lora" in presets:
            msg += f"角色预设: {len(presets['character_lora'])} 个\n"
        if "style" in presets:
            msg += f"风格预设: {len(presets['style'])} 个\n"
        if "costume" in presets:
            msg += f"服装预设: {len(presets['costume'])} 个\n"
        if "user_presets" in presets:
            msg += f"用户预设: {len(presets['user_presets'])} 个\n"
        msg += "\n使用 角色列表 / 风格列表 / 服装列表 查看详情。"
        yield event.plain_result(msg)

    async def _send_preset_list(self, event: AstrMessageEvent, key: str, title: str):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        presets = await self._get_presets()
        if not presets or key not in presets:
            yield event.plain_result(f"获取{title}失败。")
            return
        names = list(presets[key].keys()) if isinstance(presets[key], dict) else list(presets[key])
        nodes = [Node(uin=self._self_id(event), name="小千", content=[Plain(f"{title} (共 {len(names)} 个)")])]
        for start in range(0, len(names), 50):
            lines = []
            for idx, name in enumerate(names[start:start + 50], start=start + 1):
                value = presets[key][name] if isinstance(presets[key], dict) else name
                alias = ", ".join(value[:2]) if isinstance(value, list) else name
                lines.append(f"{idx}. {alias}")
            nodes.append(Node(uin=self._self_id(event), name="小千", content=[Plain("\n".join(lines))]))
        yield event.chain_result([Nodes(nodes)])

    @filter.command("角色列表", aliases={"角色预设", "characters"}, prefix_optional=True)
    async def on_list_characters(self, event: AstrMessageEvent):
        async for result in self._send_preset_list(event, "character_lora", "角色预设列表"):
            yield result

    @filter.command("风格列表", aliases={"风格预设", "styles"}, prefix_optional=True)
    async def on_list_styles(self, event: AstrMessageEvent):
        async for result in self._send_preset_list(event, "style", "风格预设列表"):
            yield result

    @filter.command("服装列表", aliases={"服装预设", "costumes"}, prefix_optional=True)
    async def on_list_costumes(self, event: AstrMessageEvent):
        async for result in self._send_preset_list(event, "costume", "服装预设列表"):
            yield result

    @filter.command("预设详情", aliases={"preset", "预设信息"}, prefix_optional=True)
    async def on_preset_detail(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        raw = event.message_str.strip()
        parts = raw.split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法：预设详情 <类型> <名称>\n类型：character/style/costume 或 角色/风格/服装")
            return
        type_map = {"character": "character_lora", "角色": "character_lora", "style": "style", "风格": "style", "costume": "costume", "服装": "costume"}
        api_type = type_map.get(parts[1].lower(), parts[1])
        try:
            detail = await self._api().preset_detail(api_type, parts[2])
            yield event.plain_result("预设详情\n" + self._format_data(detail))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("预设图片", aliases={"preset_image"}, prefix_optional=True)
    async def on_preset_image(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法：预设图片 <类型> <图片名>")
            return
        try:
            data, _ctype = await self._api().preset_image(parts[1], parts[2])
            decision = await self._review_generated_asset(GeneratedAsset(image_bytes=data), event)
            if not decision.allowed:
                yield event.plain_result(f"预设图片自审未通过，已停止发送（{decision.reason}）。")
                return
            yield await self._send_image_bytes(event, data, "预设图片", parts[2])
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("添加预设", aliases={"新建预设", "addpreset"}, prefix_optional=True)
    async def on_add_preset(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2 or "|" not in parts[1]:
            yield event.plain_result("用法：添加预设 <名称>|<内容>")
            return
        preset_name, preset_content = [part.strip() for part in parts[1].split("|", 1)]
        if not preset_name or not preset_content:
            yield event.plain_result("名称和内容不能为空。")
            return
        try:
            result = await self._api().add_user_preset(preset_name, preset_content)
            await self._get_presets(force_refresh=True)
            msg = result.get("message", "预设添加成功") if isinstance(result, dict) else "预设添加成功"
            yield event.plain_result(msg)
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("删除预设", aliases={"移除预设", "delpreset"}, prefix_optional=True)
    async def on_delete_preset(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：删除预设 <名称>")
            return
        try:
            await self._api().delete_user_preset(parts[1].strip())
            await self._get_presets(force_refresh=True)
            yield event.plain_result(f"预设 '{parts[1].strip()}' 已删除")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("我的预设", aliases={"自定义预设", "mypresets"}, prefix_optional=True)
    async def on_my_presets(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().user_presets()
            yield event.plain_result(self._format_items(data, "我的预设", limit=20))
        except Exception:
            presets = await self._get_presets()
            user_presets = presets.get("user_presets") if isinstance(presets, dict) else None
            if not user_presets:
                yield event.plain_result("暂无自定义预设或获取失败。")
                return
            yield event.plain_result(self._format_data(user_presets))

    @filter.command("服装预览", aliases={"costume_image"}, prefix_optional=True)
    async def on_costume_image(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：服装预览 <服装名称>")
            return
        try:
            data, _ctype = await self._api().costume_image(parts[1].strip())
            decision = await self._review_generated_asset(GeneratedAsset(image_bytes=data), event)
            if not decision.allowed:
                yield event.plain_result(f"服装预览自审未通过，已停止发送（{decision.reason}）。")
                return
            yield await self._send_image_bytes(event, data, "服装预览", parts[1].strip() + ".png")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("最近图片", aliases={"recent_images"}, prefix_optional=True)
    async def on_recent_images(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        tokens = event.message_str.strip().split()[1:]
        limit = self._parse_limit(tokens, default=12, max_value=50)
        include_r18 = "--r18" in tokens or "--all" in tokens
        include_r18g = "--r18g" in tokens or "--all" in tokens
        try:
            data = await self._api().recent_images(limit=limit, exclude_r18=not include_r18, exclude_r18g=not include_r18g)
            yield event.plain_result(self._format_items(data, "最近图片", limit=limit))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("图片元数据", aliases={"图片metadata", "image_metadata"}, prefix_optional=True)
    async def on_image_metadata(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        date_folder, filename = self._extract_image_ref(event.message_str)
        if not date_folder or not filename:
            yield event.plain_result("用法：图片元数据 <日期>/<文件名>")
            return
        try:
            data = await self._api().image_metadata(date_folder, filename)
            yield event.plain_result("图片元数据\n" + self._format_data(data))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("更新图片标签", aliases={"图片标签", "update_image_tags"}, prefix_optional=True)
    async def on_update_image_tags(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        raw = event.message_str.strip()
        tokens = raw.split()
        date_folder, filename = self._extract_image_ref(raw)
        if not date_folder or not filename:
            yield event.plain_result("用法：更新图片标签 <日期>/<文件名> r18=true r18g=false")
            return
        r18 = None
        r18g = None
        for key, value in re.findall(r"(r18g|r18|is_r18g|is_r18)\s*=\s*([^\s]+)", raw, flags=re.I):
            parsed = self._parse_bool_token(value)
            if key.lower() in {"r18g", "is_r18g"}:
                r18g = parsed
            else:
                r18 = parsed
        if r18 is None and "--r18" in tokens:
            r18 = True
        if r18g is None and "--r18g" in tokens:
            r18g = True
        if r18 is None and r18g is None:
            yield event.plain_result("请至少提供一个标签：r18=true/false 或 r18g=true/false")
            return
        try:
            data = await self._api().update_image_tags(date_folder, filename, r18, r18g)
            yield event.plain_result("图片标签已更新\n" + self._format_data(data))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("画廊列表", aliases={"gallery_images", "图库列表"}, prefix_optional=True)
    async def on_gallery_images(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        tokens = event.message_str.strip().split()[1:]
        page = self._parse_limit(tokens, default=1, max_value=999)
        search_tokens = [token for token in tokens if not token.isdigit() and not token.startswith("--")]
        params = {
            "page": page,
            "page_size": 10,
            "exclude_r18": str("--r18" not in tokens and "--all" not in tokens).lower(),
            "exclude_r18g": str("--r18g" not in tokens and "--all" not in tokens).lower(),
        }
        if search_tokens:
            params["search"] = " ".join(search_tokens)
        try:
            data = await self._api().gallery_json("/gallery/images", params=params)
            yield event.plain_result(self._format_items(data, "画廊列表", limit=10))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("画廊筛选", aliases={"gallery_filters", "图库筛选"}, prefix_optional=True)
    async def on_gallery_filters(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().gallery_json("/gallery/filters")
            yield event.plain_result("画廊筛选项\n" + self._format_data(data))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("视频生成", aliases={"生成视频", "video_generate"}, prefix_optional=True)
    async def on_video_generate(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        raw = event.message_str.strip()
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：视频生成 <提示词> [-t 秒] [-fps 帧率] [-n 负面提示词]，并附带图片。")
            return
        prompt_line = parts[1]
        duration = self._regex_arg(prompt_line, r"(?:^|\s)-t\s+(\d+)", "4")
        fps = self._regex_arg(prompt_line, r"(?:^|\s)-fps\s+(\d+)", "16")
        negative = self._regex_arg(prompt_line, r"(?:^|\s)-n\s+(.+)$", "")
        prompt = re.sub(r"(?:^|\s)-t\s+\d+", " ", prompt_line)
        prompt = re.sub(r"(?:^|\s)-fps\s+\d+", " ", prompt)
        prompt = re.sub(r"(?:^|\s)-n\s+.+$", " ", prompt).strip()
        if not prompt:
            yield event.plain_result("视频提示词不能为空。")
            return
        image_bytes = await self._get_image_from_event(event, self._int_conf("timeout", 60))
        if not image_bytes:
            yield event.plain_result("视频生成需要附带一张起始图片。")
            return
        try:
            yield event.plain_result("收到视频生成请求，正在提交任务。")
            data = await self._api().video_generate(image_bytes, prompt, negative_prompt=negative, duration=duration, fps=fps)
            yield event.plain_result("视频任务已提交\n" + self._format_data(data))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @staticmethod
    def _regex_arg(text: str, pattern: str, default: str) -> str:
        match = re.search(pattern, text)
        return match.group(1).strip() if match else default

    @filter.command("视频历史", aliases={"video_history"}, prefix_optional=True)
    async def on_video_history(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().video_history()
            yield event.plain_result(self._format_items(data, "视频历史", limit=10))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    async def _handle_video_file(self, event: AstrMessageEvent, kind: str, image_like: bool):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法：视频查看/视频缩略图/视频末帧/视频源图 <timestamp> <username>")
            return
        try:
            data, content_type = await self._api().video_file(parts[1], parts[2], kind=kind)
            if image_like:
                decision = await self._review_generated_asset(GeneratedAsset(image_bytes=data), event)
                if not decision.allowed:
                    yield event.plain_result(f"图片自审未通过，已停止发送（{decision.reason}）。")
                    return
                yield await self._send_image_bytes(event, data, f"{kind} 获取成功", f"{parts[1]}_{kind}.png")
            else:
                yield await self._send_file_bytes(event, data, f"{parts[1]}_{parts[2]}.mp4", content_type)
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("视频查看", aliases={"video_get"}, prefix_optional=True)
    async def on_video_get(self, event: AstrMessageEvent):
        async for result in self._handle_video_file(event, "video", False):
            yield result

    @filter.command("视频缩略图", aliases={"video_thumbnail"}, prefix_optional=True)
    async def on_video_thumbnail(self, event: AstrMessageEvent):
        async for result in self._handle_video_file(event, "thumbnail", True):
            yield result

    @filter.command("视频末帧", aliases={"video_last_frame"}, prefix_optional=True)
    async def on_video_last_frame(self, event: AstrMessageEvent):
        async for result in self._handle_video_file(event, "last-frame", True):
            yield result

    @filter.command("视频源图", aliases={"video_source_image"}, prefix_optional=True)
    async def on_video_source_image(self, event: AstrMessageEvent):
        async for result in self._handle_video_file(event, "source-image", True):
            yield result

    @filter.command("绘图文档", aliases={"xw文档", "drawing_doc"}, prefix_optional=True)
    async def on_document(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("可下载文档：涩涩词条大全、常规法典、色色法典\n用法：绘图文档 <名称>")
            return
        doc_name = parts[1].strip()
        try:
            data, content_type = await self._api().document(doc_name)
            if "text" in content_type or doc_name.endswith(".txt"):
                yield event.plain_result(self._truncate(data.decode("utf-8", errors="replace"), 1800))
                return
            suffix = ".docx" if "word" in content_type or "officedocument" in content_type else ".bin"
            yield await self._send_file_bytes(event, data, doc_name + suffix, content_type)
        except Exception as exc:
            yield await self._handle_api_error(event, exc)
