import asyncio
import base64
import hashlib
import inspect
import json
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import aiohttp

from astrbot import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Node, Nodes, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent


DEFAULT_API_URL = "https://sd.loping151.com/api/generate"
DEFAULT_TIMEOUT = 90
PLUGIN_VERSION = "0.4.0"

MAX_REFERENCE_IMAGES = 4

# 服务端 /api/document/{name} 实际接受的名字（实测得到），以及能不能直接发到聊天里。
# 「涩涩词条大全」就是 77MB 的 dictionary.txt，下载下来只会拖垮消息通道，改成给链接。
DOCUMENTS = (
    ("常规法典", "所长常规 NovalAI 个人法典 · docx 约 0.8MB", True),
    ("色色法典", "所长色色 NovalAI 个人法典 · docx 约 1.4MB", True),
    ("花花魔法书", "花花的魔法书 单人版 MS151 · docx 约 0.4MB", True),
    ("涩涩词条大全", "全量词条字典 · txt 约 77MB，只给链接", False),
)
DOCUMENT_NAMES = tuple(name for name, _desc, _downloadable in DOCUMENTS)


# ---------------------------------------------------------------------------
# R18 / R18G 审核
# ---------------------------------------------------------------------------
#
# 基于 sd.loping151.com 实测数据校准：
#
# - metadata 里的 is_r18 / is_r18g 是**用户生成时自己勾选的标记**，不是内容检测结果。
#   实测存在提示词为「色图」但 is_r18=false、nsfw_score=0.997 的样本。
#   所以这两个字段只能用来拦截，绝不能拿来证明一张图是安全的。
# - metadata.nsfw_score 才是服务端真实的分类器输出。40 张样本的分布是：
#   明确安全 0.000~0.480，泳装/比基尼等擦边 0.683~0.843，明确 R18 0.988~0.999。
#   0.85 到 0.98 之间有一段干净的间隔，三档阈值就落在这段间隔的不同位置。
# - 没有分数 = 未知，不等于安全。

LEVEL_SAFE = "safe"
LEVEL_R18 = "r18"
LEVEL_R18G = "r18g"
LEVEL_UNKNOWN = "unknown"

POLICY_THRESHOLDS = {"strict": 0.80, "standard": 0.90, "loose": 0.97}
DEFAULT_POLICY = "standard"

# 提示词预筛词表：命中即认为用户在主动索取该类内容，可以在花掉额度之前拦下来。
# ASCII 词按单词边界匹配，中文词按子串匹配。
_R18_TERMS_EN = (
    "nsfw", "hentai", "explicit", "porn", "porno", "pornography", "erotic",
    "nude", "nudity", "naked", "topless", "bottomless", "undressing", "no panties",
    "nipples", "nipple", "areolae", "areola", "pussy", "vagina", "vaginal", "vulva",
    "clitoris", "penis", "testicles", "anus", "anal sex", "ass fuck",
    "sex", "sexual intercourse", "fucked", "fucking", "gangbang", "bukkake",
    "creampie", "cumshot", "ejaculation", "semen", "precum",
    "fellatio", "blowjob", "cunnilingus", "paizuri", "titfuck", "footjob", "handjob",
    "masturbation", "masturbating", "fingering", "orgasm", "ahegao", "squirting",
    "futanari", "bestiality", "tentacle sex", "rape", "molestation",
    "sex toy", "dildo", "vibrator", "buttplug", "lactation",
    "spread pussy", "pussy juice", "after sex", "cum on body", "cum in pussy",
)
_R18_TERMS_ZH = (
    "涩图", "色图", "涩涩", "瑟瑟", "黄图", "裸体", "全裸", "半裸", "脱衣", "无码",
    "露点", "乳头", "乳首", "奶头", "阴部", "阴道", "阴茎", "私处",
    "性交", "做爱", "口交", "足交", "乳交", "手淫", "自慰", "抽插",
    "高潮", "潮吹", "精液", "内射", "中出", "颜射", "射精", "淫乱", "淫荡", "淫水",
    "触手", "强奸", "轮奸", "凌辱", "肛交", "情趣内衣", "巨乳露出", "性爱", "发情",
)
_R18G_TERMS_EN = (
    "guro", "gore", "gory", "disembowel", "disembowelment", "dismember",
    "dismemberment", "decapitation", "decapitated", "beheading", "entrails",
    "intestines", "eviscerate", "mutilation", "mutilated", "necrophilia",
    "impalement", "torture", "snuff",
)
_R18G_TERMS_ZH = (
    "猎奇", "血腥", "肢解", "碎尸", "断头", "内脏", "残肢", "剖腹", "开膛",
    "分尸", "虐杀", "酷刑",
)


def _build_patterns(terms_en: Tuple[str, ...], terms_zh: Tuple[str, ...]) -> List[Tuple[re.Pattern, str]]:
    patterns: List[Tuple[re.Pattern, str]] = []
    for term in terms_en:
        escaped = re.escape(term).replace(r"\ ", r"[\s_-]+")
        patterns.append((re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.I), term))
    for term in terms_zh:
        patterns.append((re.compile(re.escape(term)), term))
    return patterns


R18_PATTERNS = _build_patterns(_R18_TERMS_EN, _R18_TERMS_ZH)
R18G_PATTERNS = _build_patterns(_R18G_TERMS_EN, _R18G_TERMS_ZH)

REVIEW_METADATA_KEYS = (
    "is_r18", "is_r18g", "r18", "r18g", "nsfw", "nsfw_score", "score",
    "status", "date_folder", "filename", "is_img2img", "nai_ref_mode",
)


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
    matches: Dict[str, Any] = field(default_factory=dict)
    raw_response: Any = None


@dataclass
class ReviewDecision:
    """一次审核的结论。allowed=False 表示不得发送。"""

    allowed: bool
    level: str = LEVEL_SAFE
    source: str = "disabled"
    score: Optional[float] = None
    reason: str = ""
    matched: List[str] = field(default_factory=list)

    def detail(self) -> str:
        parts = [f"判定={self.level}", f"来源={self.source}"]
        if self.score is not None:
            parts.append(f"score={self.score:.3f}")
        if self.matched:
            parts.append("命中=" + "/".join(self.matched[:5]))
        if self.reason:
            parts.append(self.reason)
        return "，".join(parts)


@dataclass
class Allowance:
    """当前会话被允许发送到什么级别的内容。"""

    r18: bool = False
    r18g: bool = False
    source: str = "默认策略"

    def allows(self, level: str) -> bool:
        if level == LEVEL_R18G:
            return self.r18g
        # 未知等级来自「拿不到判定」，只有已经放行 R18 的会话才接受它。
        if level in (LEVEL_R18, LEVEL_UNKNOWN):
            return self.r18
        return True

    def describe(self) -> str:
        return f"R18 {'✅' if self.r18 else '❌'} / R18G {'✅' if self.r18g else '❌'}（{self.source}）"


@dataclass
class GenerateArgs:
    prompt: str
    denoising_strength: float = 0.6
    is_r18: bool = False
    is_r18g: bool = False
    nai_ref_mode: str = "i2i"
    uses_nai: bool = False


def normalize_policy(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"off", "disabled", "none", "关闭", "关"}:
        return "off"
    if text in POLICY_THRESHOLDS:
        return text
    if text in {"严格"}:
        return "strict"
    if text in {"宽松"}:
        return "loose"
    if text in {"标准", "默认"}:
        return "standard"
    return DEFAULT_POLICY


def scan_prompt(text: str, extra_r18: Optional[List[re.Pattern]] = None) -> Tuple[str, List[str]]:
    """扫描提示词，返回 (级别, 命中词)。级别为 safe / r18 / r18g。"""
    if not text:
        return LEVEL_SAFE, []
    gore = [term for pattern, term in R18G_PATTERNS if pattern.search(text)]
    if gore:
        return LEVEL_R18G, gore
    adult = [term for pattern, term in R18_PATTERNS if pattern.search(text)]
    for pattern in extra_r18 or []:
        if pattern.search(text):
            adult.append(pattern.pattern.replace("\\", ""))
    if adult:
        return LEVEL_R18, adult
    return LEVEL_SAFE, []


def flatten_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 {"metadata": {...}} 这类嵌套结构摊平成一层，只保留审核关心的键。"""
    if not isinstance(metadata, dict):
        return {}
    flat: Dict[str, Any] = {}
    queue = [metadata]
    visited = 0
    while queue and visited < 8:
        current = queue.pop(0)
        visited += 1
        if not isinstance(current, dict):
            continue
        for key, value in current.items():
            if key in ("metadata", "image_metadata", "meta", "data") and isinstance(value, dict):
                queue.append(value)
                continue
            if key in REVIEW_METADATA_KEYS and key not in flat:
                flat[key] = value
    return flat


class ReviewLog:
    """最近若干次审核记录，供管理员排查误判与漏判。"""

    def __init__(self, maxlen: int = 60):
        self.entries: Deque[Dict[str, Any]] = deque(maxlen=maxlen)

    def add(self, session: str, sender: str, decision: ReviewDecision, filename: str = ""):
        self.entries.append(
            {
                "time": time.strftime("%m-%d %H:%M:%S"),
                "session": session or "未知会话",
                "sender": sender or "未知用户",
                "allowed": decision.allowed,
                "level": decision.level,
                "source": decision.source,
                "score": decision.score,
                "reason": decision.reason,
                "filename": filename,
            }
        )

    def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        return list(self.entries)[-limit:][::-1]

    def stats(self) -> Dict[str, int]:
        blocked = sum(1 for entry in self.entries if not entry["allowed"])
        return {"total": len(self.entries), "blocked": blocked, "passed": len(self.entries) - blocked}


class DecisionCache:
    """按图片内容哈希缓存审核结论，避免同一张图重复走外部审核。"""

    def __init__(self, maxsize: int = 256):
        self.maxsize = maxsize
        self._data: "OrderedDict[str, ReviewDecision]" = OrderedDict()

    @staticmethod
    def key(image_bytes: Optional[bytes], *signature: Any) -> Optional[str]:
        """缓存键必须覆盖全部判定输入，否则同一张图换了 metadata 会读到旧结论。"""
        if not image_bytes:
            return None
        parts = "|".join(str(item) for item in signature)
        return f"{hashlib.sha256(image_bytes).hexdigest()}:{parts}"

    def get(self, key: Optional[str]) -> Optional[ReviewDecision]:
        if not key or key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: Optional[str], decision: ReviewDecision):
        if not key:
            return
        self._data[key] = decision
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


# ---------------------------------------------------------------------------
# API 客户端
# ---------------------------------------------------------------------------


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
        auth: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        req_headers = self._auth_headers(auth=auth, headers=headers)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, self.endpoint_url(endpoint), params=params, json=json_data, headers=req_headers
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
    ) -> Tuple[bytes, str]:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        req_headers = self._auth_headers(auth=auth)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, self.endpoint_url(endpoint), params=params, headers=req_headers) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    text = body.decode("utf-8", errors="replace")
                    raise XWDrawApiError(
                        self._error_message(self._decode_json_text(text), text, resp.status), resp.status
                    )
                return body, resp.headers.get("Content-Type", "")

    async def fetch_url_bytes(self, url: str, *, auth: bool = False) -> bytes:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        req_headers = self._auth_headers(auth=auth) if auth else {}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=req_headers) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    text = body.decode("utf-8", errors="replace")
                    raise XWDrawApiError(
                        self._error_message(self._decode_json_text(text), text, resp.status), resp.status
                    )
                return body

    async def download_image(self, filename_or_url: str) -> Optional[bytes]:
        seen = set()
        for url in self._download_attempts(filename_or_url):
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
            if root:
                attempts.append(f"{root}/{self.encode_path_segments(unquoted)}")
        except Exception:
            pass
        return attempts

    # --- 生成 ---------------------------------------------------------------

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
                return await self._asset_from_generation_result(result if result is not None else text, elapsed)

    async def _asset_from_generation_result(self, result: Any, elapsed: float) -> GeneratedAsset:
        asset = GeneratedAsset(elapsed=elapsed, raw_response=result)
        if not isinstance(result, dict):
            return asset

        asset.metadata = flatten_metadata(result)
        asset.final_prompt = self._first_string(result, "final_prompt", "actual_prompt", "translated_prompt", "prompt")
        if isinstance(result.get("matches"), dict):
            asset.matches = result["matches"]

        image_b64 = self._first_string(result, "image_base64", "image_data", "base64")
        if image_b64:
            asset.image_bytes = self._decode_image_base64(image_b64)

        image_ref = self._first_string(result, "filename", "file", "url", "image_url", "path")
        if not image_ref:
            return asset

        asset.url = self.image_url(image_ref)
        asset.date_folder, asset.filename = self.split_image_ref(image_ref)
        if not asset.image_bytes:
            asset.image_bytes = await self.download_image(image_ref)
        if asset.date_folder and asset.filename:
            asset.metadata.update(await self.fetch_score_metadata(asset.date_folder, asset.filename))
        return asset

    async def fetch_score_metadata(self, date_folder: str, filename: str, attempts: int = 3) -> Dict[str, Any]:
        """拉取图片 metadata。nsfw_score 有可能比生成响应稍晚落库，所以带重试。"""
        for attempt in range(attempts):
            try:
                payload = await self.image_metadata(date_folder, filename)
                flat = flatten_metadata(payload if isinstance(payload, dict) else {})
                if flat.get("nsfw_score") is not None or attempt == attempts - 1:
                    return flat
            except Exception as exc:
                logger.debug(f"读取图片 metadata 失败 (第 {attempt + 1} 次): {exc}")
            await asyncio.sleep(0.6 * (attempt + 1))
        return {}

    # --- 只读接口 -----------------------------------------------------------

    async def verify(self) -> Any:
        return await self.request_json("POST", "/api/verify")

    async def version(self) -> Any:
        return await self.request_json("GET", "/api/version", auth=False)

    async def generation_config(self) -> Any:
        return await self.request_json("GET", "/api/generation-config")

    async def nai_config(self) -> Any:
        return await self.request_json("GET", "/api/nai/config")

    async def queue_status(self) -> Any:
        return await self.request_json("GET", "/api/queue-status", auth=False)

    async def announcements(self) -> Any:
        return await self.request_json("GET", "/api/announcements", auth=False)

    async def presets(self) -> Any:
        return await self.request_json("GET", "/api/presets")

    async def preset_detail(self, preset_type: str, preset_name: str) -> Any:
        return await self.request_json(
            "GET", f"/api/preset-detail/{quote(preset_type, safe='')}/{quote(preset_name, safe='')}"
        )

    async def preset_image(self, preset_type: str, image_name: str) -> Tuple[bytes, str]:
        return await self.request_bytes(
            "GET", f"/api/preset-image/{quote(preset_type, safe='')}/{quote(image_name, safe='')}"
        )

    async def costume_image(self, costume_name: str) -> Tuple[bytes, str]:
        return await self.request_bytes("GET", f"/costume/{quote(costume_name, safe='')}")

    async def recommended_prompts(self) -> Any:
        return await self.request_json("GET", "/api/recommended-prompts")

    async def user_presets(self) -> Any:
        return await self.request_json("GET", "/api/user-presets")

    async def add_user_preset(self, preset_name: str, preset_content: str) -> Any:
        return await self.request_json(
            "POST", "/api/user-presets", json_data={"preset_name": preset_name, "preset_content": preset_content}
        )

    async def delete_user_preset(self, preset_name: str) -> Any:
        return await self.request_json("DELETE", f"/api/user-presets/{quote(preset_name, safe='')}")

    async def recent_images(self, limit: int = 12, exclude_r18: bool = True, exclude_r18g: bool = True) -> Any:
        return await self.request_json(
            "GET",
            "/api/recent-images",
            params={
                "limit": limit,
                "exclude_r18": str(exclude_r18).lower(),
                "exclude_r18g": str(exclude_r18g).lower(),
            },
        )

    async def image_metadata(self, date_folder: str, filename: str) -> Any:
        return await self.request_json(
            "GET", f"/api/image-metadata/{quote(date_folder, safe='')}/{quote(filename, safe='')}"
        )

    async def thumbnail(self, date_folder: str, filename: str) -> Tuple[bytes, str]:
        return await self.request_bytes(
            "GET", f"/api/thumbnail/{quote(date_folder, safe='')}/{quote(filename, safe='')}"
        )

    async def update_image_tags(
        self, date_folder: str, filename: str, is_r18: Optional[bool], is_r18g: Optional[bool]
    ) -> Any:
        body: Dict[str, Any] = {}
        if is_r18 is not None:
            body["is_r18"] = is_r18
        if is_r18g is not None:
            body["is_r18g"] = is_r18g
        return await self.request_json(
            "PUT", f"/api/image-tags/{quote(date_folder, safe='')}/{quote(filename, safe='')}", json_data=body
        )

    async def document(self, doc_name: str) -> Tuple[bytes, str]:
        return await self.request_bytes("GET", f"/api/document/{quote(doc_name, safe='')}")

    # --- 画廊 ---------------------------------------------------------------

    async def gallery_request(self, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """画廊接口。Bearer 通常可直接访问，遇到 401/403 才回退到 /gallery/login 建会话。"""
        try:
            return await self.request_json(method, endpoint, params=params)
        except XWDrawApiError as exc:
            if exc.status not in (401, 403):
                raise
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = self._auth_headers()
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar()) as session:
            async with session.post(self.endpoint_url("/gallery/login"), headers=headers) as login_resp:
                login_text = await login_resp.text()
                if login_resp.status >= 400:
                    raise XWDrawApiError(
                        self._error_message(self._decode_json_text(login_text), login_text, login_resp.status),
                        login_resp.status,
                    )
            async with session.request(
                method, self.endpoint_url(endpoint), params=params, headers=headers
            ) as resp:
                text = await resp.text()
                payload = self._decode_json_text(text)
                if resp.status >= 400:
                    raise XWDrawApiError(self._error_message(payload, text, resp.status), resp.status, payload)
                return payload if payload is not None else text

    async def gallery_images(self, params: Dict[str, Any]) -> Any:
        return await self.gallery_request("GET", "/gallery/images", params)

    async def gallery_filters(self) -> Any:
        return await self.gallery_request("GET", "/gallery/filters")

    async def gallery_delete_image(self, date_folder: str, filename: str) -> Any:
        return await self.gallery_request(
            "DELETE", f"/gallery/image/{quote(date_folder, safe='')}/{quote(filename, safe='')}"
        )

    # --- 工具 ---------------------------------------------------------------

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
                value = path.split("/api/image/", 1)[1] if "/api/image/" in path else path.strip("/")
            value = unquote(value).strip("/")
            parts = value.split("/")
            if len(parts) >= 2:
                return parts[-2], parts[-1]
            return None, parts[-1] if parts else None
        except Exception:
            return None, None


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


@register(
    "astrbot_plugin_xwdraw",
    "xwdraw",
    "对接小维远端 SD/NovelAI 绘图服务：文生图、图生图、NAI 参考图、预设、画廊与分级 R18 审核",
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
        self.review_log = ReviewLog()
        self.review_cache = DecisionCache()
        self.client = self._build_client()

    async def initialize(self):
        logger.info(
            f"astrbot_plugin_xwdraw v{PLUGIN_VERSION} 已加载, "
            f"api_url={self._conf('api_url', DEFAULT_API_URL)}, "
            f"版本形态={self._edition_label()}, R18 策略={self._policy()}"
        )

    # --- 配置读取 -----------------------------------------------------------

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
            timeout=self._int_conf("timeout", DEFAULT_TIMEOUT),
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
        return [item.strip() for item in str(value or "").replace("，", ",").split(",") if item.strip()]

    # --- 版本形态与 R18 放行 -------------------------------------------------

    def _is_custom_edition(self) -> bool:
        return str(self._conf("edition", "shared")).strip().lower() in {"custom", "定制版", "custom_edition"}

    def _edition_label(self) -> str:
        return "定制版" if self._is_custom_edition() else "共用版"

    def _policy(self) -> str:
        return normalize_policy(self._conf("r18_policy", DEFAULT_POLICY))

    def _score_threshold(self) -> float:
        override = self._float_conf("r18_nsfw_score_threshold", 0.0)
        if override > 0:
            return override
        return POLICY_THRESHOLDS.get(self._policy(), POLICY_THRESHOLDS[DEFAULT_POLICY])

    def _extra_block_patterns(self) -> List[re.Pattern]:
        return [re.compile(re.escape(item), re.I) for item in self._list_conf("r18_extra_blocklist")]

    def _allowance(self, event: Optional[AstrMessageEvent]) -> Allowance:
        """决定当前会话可以放行到哪一级。优先级：会话指令 > 版本形态 > 白名单 > 默认全拦。"""
        session_id = self._session_id(event) if event else ""
        override = self._r18_overrides().get(session_id) if session_id else None
        if isinstance(override, dict):
            return Allowance(bool(override.get("r18")), bool(override.get("r18g")), "本会话设置")

        if self._is_custom_edition():
            return Allowance(
                self._bool_conf("custom_edition_allow_r18", True),
                self._bool_conf("custom_edition_allow_r18g", False),
                "定制版默认",
            )

        # 名单只放行 R18；R18G 必须由管理员在群里显式发「R18开启 含G」。
        if session_id and session_id in self._list_conf("r18_allowed_session_ids"):
            return Allowance(True, False, "放行会话名单")

        return Allowance(False, False, "共用版默认")

    def _r18_overrides(self) -> Dict[str, Any]:
        overrides = self.switch_state.setdefault("r18_overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
            self.switch_state["r18_overrides"] = overrides
        return overrides

    def _set_r18_override(self, event: AstrMessageEvent, allow_r18: bool, allow_r18g: bool):
        session_id = self._session_id(event)
        if not session_id:
            raise XWDrawApiError("无法识别当前会话，不能保存 R18 放行状态。")
        self._r18_overrides()[session_id] = {"r18": allow_r18, "r18g": allow_r18g}
        self._save_switch_state()

    def _clear_r18_override(self, event: AstrMessageEvent):
        session_id = self._session_id(event)
        self._r18_overrides().pop(session_id, None)
        self._save_switch_state()

    # --- 总开关持久化 -------------------------------------------------------

    def _load_switch_state(self) -> Dict[str, Any]:
        try:
            if self.switch_state_path.exists():
                data = json.loads(self.switch_state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("session_overrides", {})
                    data.setdefault("r18_overrides", {})
                    return data
        except Exception as exc:
            logger.warning(f"读取绘图开关状态失败: {exc}")
        return {"session_overrides": {}, "r18_overrides": {}}

    def _save_switch_state(self):
        try:
            self.switch_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.switch_state_path.write_text(
                json.dumps(self.switch_state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
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

    def _set_session_switch(self, event: AstrMessageEvent, enabled: bool):
        session_id = self._session_id(event)
        if not session_id:
            raise XWDrawApiError("无法识别当前会话，不能保存绘图开关状态。")
        self._session_switch_overrides()[session_id] = enabled
        self._save_switch_state()

    def _disabled_result(self, event: AstrMessageEvent):
        if self._is_plugin_enabled_for_event(event):
            return None
        return event.plain_result("本群/当前会话的绘图总开关已关闭。请联系群管理员发送「绘图开启」后再使用。")

    # --- 身份识别 -----------------------------------------------------------

    async def _maybe_await(self, value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

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
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    def _sender_role_values(self, event: AstrMessageEvent) -> List[str]:
        values: List[str] = []
        for obj in self._event_candidate_objects(event):
            for attr in ("role", "permission", "user_role", "sender_role", "group_role"):
                value = self._get_attr_or_key(obj, attr)
                if value:
                    values.append(str(value).strip().lower())
            for attr in ("is_admin", "admin", "is_owner", "owner"):
                if self._truthy(self._get_attr_or_key(obj, attr)):
                    values.append("admin")
        return values

    async def _is_bot_admin(self, event: AstrMessageEvent) -> bool:
        try:
            is_admin_attr = getattr(event, "is_admin", None)
            is_admin = await self._maybe_await(is_admin_attr() if callable(is_admin_attr) else is_admin_attr)
            return self._truthy(is_admin)
        except Exception:
            return False

    async def _admin_only_result(self, event: AstrMessageEvent):
        if await self._is_bot_admin(event):
            return None
        return event.plain_result("该功能仅 bot 管理员可用。")

    async def _is_switch_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = self._sender_id(event)
        if sender_id and sender_id in self._list_conf("switch_admin_user_ids"):
            return True
        if await self._is_bot_admin(event):
            return True
        if self._bool_conf("group_admin_can_toggle", True):
            roles = set(self._sender_role_values(event))
            if roles.intersection({"owner", "admin", "administrator", "群主", "管理员"}):
                return True
        return False

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

    # --- 审核核心 -----------------------------------------------------------

    async def review_asset(
        self, asset: GeneratedAsset, event: Optional[AstrMessageEvent] = None, prompt_level: str = LEVEL_SAFE
    ) -> ReviewDecision:
        """对准备发送的图片做分级审核。

        判定顺序刻意把「自述标记」和「分类器分数」分开：自述标记只用于拦截，
        分数才是放行依据，两者都拿不到时是 unknown 而不是 safe。
        """
        policy = self._policy()
        if policy == "off":
            return ReviewDecision(True, source="policy_off", reason="R18 审核已关闭")

        allowance = self._allowance(event)
        threshold = self._score_threshold()
        flat = flatten_metadata(asset.metadata)
        cache_key = DecisionCache.key(
            asset.image_bytes,
            policy,
            threshold,
            allowance.r18,
            allowance.r18g,
            prompt_level,
            flat.get("is_r18"),
            flat.get("is_r18g"),
            flat.get("nsfw_score"),
        )
        decision = self.review_cache.get(cache_key)
        if decision is None:
            decision = await self._decide(asset, allowance, threshold, prompt_level)
            self.review_cache.put(cache_key, decision)

        # 命中缓存也要记一笔：审计关心的是「谁在哪个会话触发了这次判定」，不是判定算了几次。
        session = self._session_id(event) if event else ""
        sender = self._sender_id(event) if event else ""
        self.review_log.add(session, sender, decision, asset.filename or "")
        if not decision.allowed:
            logger.warning(
                f"图片已拦截: {decision.detail()}, session={session}, sender={sender}, filename={asset.filename}"
            )
        return decision

    async def _decide(
        self, asset: GeneratedAsset, allowance: Allowance, threshold: float, prompt_level: str
    ) -> ReviewDecision:
        flat = flatten_metadata(asset.metadata)
        score = self._float_value(flat.get("nsfw_score", flat.get("score")))
        declared_r18g = self._truthy(flat.get("is_r18g", flat.get("r18g")))
        declared_r18 = self._truthy(flat.get("is_r18", flat.get("r18", flat.get("nsfw"))))

        # 1) 自述标记：只能拦截，不能放行。
        if declared_r18g and not allowance.allows(LEVEL_R18G):
            return ReviewDecision(False, LEVEL_R18G, "metadata_flag", score, "图片被标记为 R18G")
        if declared_r18 and not allowance.allows(LEVEL_R18):
            return ReviewDecision(False, LEVEL_R18, "metadata_flag", score, "图片被标记为 R18")

        # 2) 提示词预筛结论同样是硬信号。
        if prompt_level == LEVEL_R18G and not allowance.allows(LEVEL_R18G):
            return ReviewDecision(False, LEVEL_R18G, "prompt_scan", score, "提示词命中 R18G 词条")
        if prompt_level == LEVEL_R18 and not allowance.allows(LEVEL_R18):
            return ReviewDecision(False, LEVEL_R18, "prompt_scan", score, "提示词命中 R18 词条")

        # 3) 服务端分类器分数。
        if score is not None and score >= threshold and not allowance.allows(LEVEL_R18):
            return ReviewDecision(
                False, LEVEL_R18, "nsfw_score", score, f"nsfw_score={score:.3f} ≥ 阈值 {threshold:.2f}"
            )

        # 4) 外部视觉审核：作为额外一道关，也可以给出「安全」结论来补上缺失的分数。
        if self._bool_conf("external_review_enabled", False):
            external = await self._external_review(asset.image_bytes, flat)
            if not external.allowed and not allowance.allows(external.level):
                return external
            if external.source == "external_review":
                return ReviewDecision(True, external.level, "external_review", external.score, external.reason)

        # 5) 既没有分数也没有外审结论 —— 未知，按配置决定是否保守拦截。
        if score is None:
            if self._bool_conf("r18_block_without_score", True) and not allowance.allows(LEVEL_R18):
                return ReviewDecision(
                    False, LEVEL_UNKNOWN, "missing_score", None, "拿不到 nsfw_score，按保守策略不发送"
                )
            return ReviewDecision(True, LEVEL_UNKNOWN, "missing_score", None, "缺少分数但按配置放行")

        # 走到这里分数若仍然超阈值，说明是被会话放行的，审核记录要如实写成 r18。
        if score >= threshold:
            return ReviewDecision(
                True, LEVEL_R18, "nsfw_score", score, f"nsfw_score={score:.3f} ≥ 阈值 {threshold:.2f}，当前会话已放行"
            )
        return ReviewDecision(True, LEVEL_SAFE, "nsfw_score", score, f"nsfw_score={score:.3f} < 阈值 {threshold:.2f}")

    async def review_local_image(self, image_bytes: bytes, event: Optional[AstrMessageEvent] = None) -> ReviewDecision:
        """审核不是本服务生成、因此没有 nsfw_score 的图片（预设图、服装图、输入图）。"""
        return await self.review_asset(GeneratedAsset(image_bytes=image_bytes), event)

    async def _external_review(self, image_bytes: Optional[bytes], metadata: Dict[str, Any]) -> ReviewDecision:
        fail_closed = self._bool_conf("external_review_fail_closed", True)

        def unavailable(reason: str) -> ReviewDecision:
            if fail_closed:
                return ReviewDecision(False, LEVEL_UNKNOWN, "external_review", None, reason)
            return ReviewDecision(True, LEVEL_UNKNOWN, "external_review_skipped", None, f"{reason}（按配置放行）")

        if not image_bytes:
            return unavailable("外部审核启用但没有图片数据")
        api_url = str(self._conf("external_review_api_url", "") or "").strip()
        if not api_url:
            return unavailable("外部审核启用但未配置接口地址")

        protocol = self._external_review_protocol(api_url)
        image_data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("utf-8")
        if protocol == "openai":
            post_url = self._openai_review_url(api_url)
            payload = self._openai_review_payload(image_data_url, metadata)
        else:
            post_url = api_url
            payload = {
                "model": self._conf("external_review_model", ""),
                "image": image_data_url,
                "image_base64": base64.b64encode(image_bytes).decode("utf-8"),
                "metadata": self._external_review_metadata(metadata),
            }
        headers = {"Content-Type": "application/json"}
        api_key = str(self._conf("external_review_api_key", "") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            timeout = aiohttp.ClientTimeout(total=self._int_conf("external_review_timeout", 30))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(post_url, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    result = self._extract_external_review_payload(XWDrawApiClient._decode_json_text(text), text)
                    if resp.status >= 400:
                        raise XWDrawApiError(f"外部审核接口返回 HTTP {resp.status}: {text[:300]}", resp.status)
                    if not isinstance(result, dict):
                        raise XWDrawApiError("外部审核接口没有返回 JSON 对象")
                    return self._decision_from_external_payload(result)
        except Exception as exc:
            logger.warning(f"外部审核失败: {exc}")
            return unavailable(f"外部审核失败: {exc}")

    def _decision_from_external_payload(self, payload: Dict[str, Any]) -> ReviewDecision:
        label = str(
            payload.get("level") or payload.get("label") or payload.get("category") or payload.get("status") or ""
        ).lower()
        reason = str(payload.get("reason") or payload.get("message") or "外部审核判定")
        score = self._float_value(payload.get("score", payload.get("nsfw_score")))
        safe = payload.get("safe")
        unsafe_labels = {"unsafe", "nsfw", "adult", "sexual", "porn", "pornography", "nudity", "nude", "explicit"}
        is_r18g = self._truthy(payload.get("r18g", payload.get("is_r18g"))) or label in {"r18g", "gore", "violence"}
        is_r18 = self._truthy(payload.get("r18", payload.get("is_r18", payload.get("nsfw")))) or label in {
            "r18",
            *unsafe_labels,
        }

        # 一个既没给 safe、也没给 level/score 的响应等于什么都没说，不能当作安全结论。
        if safe is None and not label and score is None and not is_r18 and not is_r18g:
            if self._bool_conf("external_review_fail_closed", True):
                return ReviewDecision(False, LEVEL_UNKNOWN, "external_review", None, "外部审核响应缺少判定字段")
            return ReviewDecision(True, LEVEL_UNKNOWN, "external_review_skipped", None, "外部审核响应无判定字段")

        if is_r18g:
            return ReviewDecision(False, LEVEL_R18G, "external_review", score, reason)
        if is_r18:
            return ReviewDecision(False, LEVEL_R18, "external_review", score, reason)
        if score is not None and score >= self._score_threshold():
            return ReviewDecision(False, LEVEL_R18, "external_review", score, f"{reason}; score 超过阈值")
        if self._falsey(safe):
            return ReviewDecision(False, label or LEVEL_R18, "external_review", score, reason)
        return ReviewDecision(True, LEVEL_SAFE, "external_review", score, reason)

    def _external_review_protocol(self, api_url: str) -> str:
        configured = str(self._conf("external_review_protocol", "auto") or "auto").strip().lower()
        if configured in {"openai", "openai_chat", "chat_completions"}:
            return "openai"
        if configured in {"custom", "json"}:
            return "custom"
        normalized = api_url.rstrip("/").lower()
        if normalized.endswith("/v1") or "/chat/completions" in normalized:
            return "openai"
        return "custom"

    @staticmethod
    def _openai_review_url(api_url: str) -> str:
        url = api_url.rstrip("/")
        return url if url.endswith("/chat/completions") else f"{url}/chat/completions"

    def _openai_review_payload(self, image_data_url: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        model = str(self._conf("external_review_model", "") or "").strip() or "gpt-4o-mini"
        system_prompt = (
            "你是图片安全审核器。只返回一个 JSON 对象，不要 Markdown，不要解释。"
            '字段必须包含 safe(boolean), level("safe"|"r18"|"r18g"), score(number), reason(string)。'
            "score 必须是不安全/NSFW/违规风险分，0 表示完全安全，1 表示高度违规；不要把 score 当作安全置信度。"
            "当 safe=true 且 level=safe 时，score 应小于 0.2。"
            "若图片包含裸露、性行为、色情暗示、成人内容、未成年人性化、血腥猎奇或其他不适合群聊发送的内容，safe=false。"
        )
        user_prompt = "请审核这张图片。服务自审字段：" + json.dumps(
            self._external_review_metadata(metadata), ensure_ascii=False
        )[:800]
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": self._int_conf("external_review_max_tokens", 300),
        }

    def _external_review_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """只把审核字段交给外部模型，绝不把用户提示词带过去（防提示注入）。"""
        flat = flatten_metadata(metadata)
        allowed_keys = ("is_r18", "is_r18g", "r18", "r18g", "nsfw", "nsfw_score", "score", "date_folder")
        return {key: flat[key] for key in allowed_keys if key in flat}

    def _extract_external_review_payload(self, decoded: Any, raw_text: str = "") -> Any:
        if isinstance(decoded, dict):
            content = self._openai_message_content(decoded)
            if content:
                return self._parse_json_object_text(content)
            if "choices" in decoded:
                return None
            return decoded
        if isinstance(decoded, str):
            return self._parse_json_object_text(decoded)
        return self._parse_json_object_text(raw_text)

    @staticmethod
    def _openai_message_content(payload: Dict[str, Any]) -> str:
        try:
            content = payload["choices"][0]["message"].get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
        except Exception:
            return ""
        return ""

    @staticmethod
    def _parse_json_object_text(text: str) -> Optional[Dict[str, Any]]:
        if not isinstance(text, str) or not text.strip():
            return None
        cleaned = text.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.S | re.I)
        if fence:
            cleaned = fence.group(1).strip()
        if not cleaned.startswith("{"):
            match = re.search(r"\{.*\}", cleaned, flags=re.S)
            if match:
                cleaned = match.group(0)
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _falsey(value: Any) -> bool:
        if isinstance(value, bool):
            return not value
        if value is None:
            return False
        return str(value).strip().lower() in {"0", "false", "no", "n", "off", "unsafe", "不安全", "否"}

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
            if value is None or value == "" or isinstance(value, bool):
                return None
            return float(value)
        except Exception:
            return None

    async def _blocked_text(self, event: AstrMessageEvent, prefix: str, decision: ReviewDecision) -> str:
        if await self._is_bot_admin(event):
            return f"{prefix}未通过自审，已停止发送。\n{decision.detail()}"
        return f"{prefix}未通过安全审核，已停止发送。"

    # --- 提示词解析 ---------------------------------------------------------

    @staticmethod
    def parse_generate_args(prompt_line: str) -> GenerateArgs:
        """解析本插件自己消费的参数。-n/-w/-h/-s/-c/-m 等一律留在提示词里交给服务端。"""
        text = prompt_line.strip()

        is_r18g = bool(re.search(r"(^|\s)--r18g(\s|$)", text, flags=re.I))
        is_r18 = bool(re.search(r"(^|\s)--r18(\s|$)", text, flags=re.I)) or is_r18g
        text = re.sub(r"(^|\s)--r18g(\s|$)", " ", text, flags=re.I)
        text = re.sub(r"(^|\s)--r18(\s|$)", " ", text, flags=re.I)

        ref_mode = "i2i"
        match = re.search(r"(^|\s)--(vt|pr|i2i)(\s|$)", text, flags=re.I)
        if match:
            ref_mode = match.group(2).lower()
            text = re.sub(r"(^|\s)--(vt|pr|i2i)(\s|$)", " ", text, count=1, flags=re.I)

        denoise = 0.6
        match = re.search(r"(?:^|\s)-d\s*([0-9]*\.?[0-9]+)", text)
        if match:
            try:
                denoise = max(0.0, min(1.0, float(match.group(1))))
            except Exception:
                denoise = 0.6
            text = re.sub(r"(?:^|\s)-d\s*[0-9]*\.?[0-9]+", " ", text, count=1)

        prompt = re.sub(r"\s+", " ", text).strip()
        uses_nai = bool(
            re.search(r"(^|[\s,，])nai[01]?([\s,，]|$)", prompt, flags=re.I)
            or re.search(r"(?:^|\s)-m\s*9[01](?:\s|$)", prompt)
        )
        return GenerateArgs(prompt, denoise, is_r18, is_r18g, ref_mode, uses_nai)

    @staticmethod
    def _has_sensitive_flag(text_or_tokens: Any) -> bool:
        text = (
            text_or_tokens
            if isinstance(text_or_tokens, str)
            else " ".join(str(token) for token in text_or_tokens)
        )
        return bool(re.search(r"(^|\s)--(?:r18g?|all)(\s|$)", text, flags=re.I))

    def _sensitive_denied_result(self, event: AstrMessageEvent, level: str, matched: List[str]):
        allowance = self._allowance(event)
        lines = [
            f"当前会话不允许生成 {level.upper()} 内容，已在提交前取消，未消耗额度。",
            f"放行状态：{allowance.describe()}",
        ]
        if matched:
            lines.append("命中词：" + "、".join(matched[:6]))
        command = "R18开启 含G" if level == LEVEL_R18G else "R18开启"
        lines.append(f"如需放行，请让 bot 管理员发送「{command}」。")
        return event.plain_result("\n".join(lines))

    # --- 图片收集与发送 -----------------------------------------------------

    async def _fetch_image_bytes(self, url_or_path: str, timeout: int) -> Optional[bytes]:
        try:
            if not url_or_path:
                return None
            value = str(url_or_path)
            if value.startswith("file://"):
                parsed_path = unquote(urlparse(value).path)
                if re.match(r"^/[A-Za-z]:/", parsed_path):
                    parsed_path = parsed_path[1:]
                value = parsed_path
            if Path(value).is_file():
                return Path(value).read_bytes()
            if value.startswith("http"):
                timeout_obj = aiohttp.ClientTimeout(total=timeout)
                async with aiohttp.ClientSession(timeout=timeout_obj) as session:
                    async with session.get(value) as resp:
                        resp.raise_for_status()
                        return await resp.read()
        except Exception as exc:
            logger.warning(f"_fetch_image_bytes 失败: {exc}")
        return None

    async def _image_segment_bytes(self, seg: Any, timeout: int) -> Optional[bytes]:
        if not isinstance(seg, Image):
            return None
        data = getattr(seg, "data", None)
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            decoded = XWDrawApiClient._decode_image_base64(data)
            if decoded:
                return decoded
        image_src = getattr(seg, "url", None) or getattr(seg, "file", None) or getattr(seg, "path", None)
        return await self._fetch_image_bytes(str(image_src), timeout) if image_src else None

    async def _get_images_from_event(self, event: AstrMessageEvent, timeout: int) -> List[bytes]:
        """收集消息（含引用回复）里的图片，最多 MAX_REFERENCE_IMAGES 张。"""
        segments = getattr(getattr(event, "message_obj", None), "message", []) or []
        ordered: List[Any] = []
        for seg in segments:
            if isinstance(seg, Reply) and getattr(seg, "chain", None):
                ordered.extend(seg.chain)
        ordered.extend(segments)

        images: List[bytes] = []
        for seg in ordered:
            image_bytes = await self._image_segment_bytes(seg, timeout)
            if image_bytes and image_bytes not in images:
                images.append(image_bytes)
            if len(images) >= MAX_REFERENCE_IMAGES:
                break
        return images

    async def _get_image_from_event(self, event: AstrMessageEvent, timeout: int) -> Optional[bytes]:
        images = await self._get_images_from_event(event, timeout)
        return images[0] if images else None

    async def _send_image_bytes(self, event: AstrMessageEvent, image_bytes: bytes, caption: str, filename: str = ""):
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
            await self.context.send_message(event.unified_msg_origin, MessageChain().file_image(str(out_path)))
            return event.plain_result(caption)
        except Exception as exc:
            logger.error(f"发送图片失败: {exc}")
            if await self._is_bot_admin(event):
                return event.plain_result(f"生成成功，但发送图片失败: {exc}")
            return event.plain_result("生成成功，但发送图片失败，请联系 bot 管理员。")

    async def _send_file_bytes(self, event: AstrMessageEvent, data: bytes, filename: str):
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
        return event.plain_result(f"文件已保存到插件数据目录：{out_path.name}")

    async def _send_generated_asset(self, event: AstrMessageEvent, asset: GeneratedAsset, prompt_level: str):
        decision = await self.review_asset(asset, event, prompt_level)
        if not decision.allowed:
            return event.plain_result(await self._blocked_text(event, "生成完成，但图片", decision))

        caption = self._generation_caption(asset, decision)
        if not asset.image_bytes:
            return event.plain_result(f"生成完成 ({asset.elapsed:.1f}s)，但没有拿到可发送的图片数据。")

        img_result = await self._send_image_bytes(event, asset.image_bytes, caption, asset.filename or "")
        if not asset.final_prompt:
            return img_result
        try:
            node = Node(uin=self._self_id(event), name="小千", content=[Plain(f"实际提示词：\n{asset.final_prompt}")])
            return [img_result, event.chain_result([Nodes([node])])]
        except Exception as exc:
            logger.debug(f"转发实际提示词失败: {exc}")
            return img_result

    def _generation_caption(self, asset: GeneratedAsset, decision: ReviewDecision) -> str:
        lines = [f"✅ 生成完成 · {asset.elapsed:.1f}s"]
        matched = []
        for key, label in (("character", "角色"), ("style", "风格"), ("costume", "服装")):
            values = asset.matches.get(key) if isinstance(asset.matches, dict) else None
            if values:
                matched.append(f"{label}:{'/'.join(str(v) for v in values[:3])}")
        if matched:
            lines.append("命中预设 " + "  ".join(matched))
        dropped = asset.matches.get("arch_dropped") if isinstance(asset.matches, dict) else None
        if dropped:
            lines.append(f"⚠️ 架构冲突已丢弃：{'/'.join(str(v) for v in dropped[:5])}")
        if decision.score is not None:
            lines.append(f"自审 {decision.score:.2f} / 阈值 {self._score_threshold():.2f}")
        return "\n".join(lines)

    # --- 展示工具 -----------------------------------------------------------

    @staticmethod
    def mask_endpoint(url: str) -> str:
        """脱敏服务地址：只保留子域和顶级域，中间打码，避免把绘图站点暴露给群成员。"""
        host = urlparse(url).netloc or str(url)
        host = host.split("@")[-1].split(":")[0]
        parts = [part for part in host.split(".") if part]
        if len(parts) < 2:
            return "***"
        if len(parts) == 2:
            return f"***.{parts[-1]}"
        masked = ["*" * len(part) for part in parts[1:-1]]
        return ".".join([parts[0]] + masked + [parts[-1]])

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(str(filename)).name or f"file_{int(time.time())}"
        return re.sub(r'[<>:"/\\|?*]+', "_", name)

    @staticmethod
    def _truncate(text: Any, limit: int = 1600) -> str:
        value = str(text)
        return value if len(value) <= limit else value[:limit] + "…"

    def _format_data(self, data: Any, limit: int = 1800) -> str:
        text = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)
        return self._truncate(text, limit)

    @staticmethod
    def render_card(title: str, sections: List[Tuple[str, List[str]]], footer: str = "") -> str:
        """统一的卡片式排版。左侧竖线在任意字体下都能对齐，比表格框稳。"""
        lines = [f"╭─ {title}"]
        for index, (heading, items) in enumerate(sections):
            if index:
                lines.append("│")
            if heading:
                lines.append(f"│ {heading}")
            for item in items:
                lines.extend(f"│   {part}" for part in str(item).split("\n"))
        lines.append(f"╰─ {footer}" if footer else "╰────────")
        return "\n".join(lines)

    def _format_items(self, data: Any, title: str, limit: int = 10) -> str:
        items = data
        if isinstance(data, dict):
            for key in ("images", "items", "results", "announcements", "prompts"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
        if not isinstance(items, list):
            return f"{title}\n{self._format_data(data)}"
        rows = []
        for idx, item in enumerate(items[:limit], 1):
            if isinstance(item, dict):
                name = (
                    item.get("title")
                    or item.get("filename")
                    or item.get("prompt")
                    or item.get("id")
                    or "未命名"
                )
                extra = [f"{key}={item[key]}" for key in ("date", "date_folder", "is_r18", "is_r18g") if key in item]
                rows.append(f"{idx}. {self._truncate(name, 80)}" + (f"  [{', '.join(extra)}]" if extra else ""))
            else:
                rows.append(f"{idx}. {self._truncate(item, 140)}")
        return self.render_card(f"{title} · {min(len(items), limit)}/{len(items)}", [("", rows)])

    def _forward_nodes(self, event: AstrMessageEvent, title: str, lines: List[str], chunk: int = 50):
        nodes = [Node(uin=self._self_id(event), name="小千", content=[Plain(title)])]
        for start in range(0, len(lines), chunk):
            nodes.append(
                Node(uin=self._self_id(event), name="小千", content=[Plain("\n".join(lines[start : start + chunk]))])
            )
        return event.chain_result([Nodes(nodes)])

    @staticmethod
    def _parse_limit(tokens: List[str], default: int = 12, max_value: int = 50) -> int:
        for token in tokens:
            if token.isdigit():
                return max(1, min(max_value, int(token)))
        return default

    @staticmethod
    def _parse_bool_token(value: str) -> Optional[bool]:
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "是", "开"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "否", "关"}:
            return False
        return None

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

    def _extract_image_ref(self, raw: str) -> Tuple[Optional[str], Optional[str]]:
        parts = raw.strip().split(maxsplit=2)
        if len(parts) < 2:
            return None, None
        if "/" in parts[1]:
            return XWDrawApiClient.split_image_ref(parts[1])
        if len(parts) >= 3:
            return parts[1], parts[2].split(maxsplit=1)[0]
        return None, parts[1]

    async def _get_presets(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        now = time.time()
        if not force_refresh and self.presets_cache and (now - self.cache_time < self.cache_duration):
            return self.presets_cache
        try:
            presets = await self._api().presets()
            if isinstance(presets, dict):
                self.presets_cache = presets
                self.cache_time = now
                return presets
        except Exception as exc:
            logger.error(f"获取预设失败: {exc}")
        return None

    async def _handle_api_error(self, event: AstrMessageEvent, exc: Exception):
        if not await self._is_bot_admin(event):
            logger.warning(f"XWDraw 命令执行失败: {exc}")
            if isinstance(exc, asyncio.TimeoutError):
                return event.plain_result("请求超时，请稍后重试。")
            return event.plain_result("请求失败，请稍后重试或联系 bot 管理员。")
        if isinstance(exc, XWDrawApiError):
            return event.plain_result(f"请求失败：{exc.message}")
        if isinstance(exc, asyncio.TimeoutError):
            return event.plain_result(f"请求超时（>{self._int_conf('timeout', 60)}s），请稍后重试或调大 timeout。")
        logger.exception("XWDraw 命令执行失败")
        return event.plain_result(f"发生未知错误：{exc}")

    # =======================================================================
    # 指令：生成
    # =======================================================================

    @filter.command("来点", aliases={"小千来点", "xwdraw"}, prefix_optional=True)
    async def on_generate(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result(
                "用法：来点 <提示词> [-d 0.6] [--vt|--pr]\n附带图片即图生图；详细语法见「绘图帮助 参数」。"
            )
            return

        args = self.parse_generate_args(parts[1])
        if not args.prompt:
            yield event.plain_result("提示词不能为空。")
            return

        prompt_level, matched = scan_prompt(args.prompt, self._extra_block_patterns())
        if args.is_r18g or prompt_level == LEVEL_R18G:
            requested_level = LEVEL_R18G
        elif args.is_r18 or prompt_level == LEVEL_R18:
            requested_level = LEVEL_R18
        else:
            requested_level = LEVEL_SAFE

        allowance = self._allowance(event)
        if self._policy() != "off" and requested_level != LEVEL_SAFE and not allowance.allows(requested_level):
            yield self._sensitive_denied_result(event, requested_level, matched)
            return

        if not self._api().api_key:
            yield event.plain_result("请在插件配置中填写 api_key 后再使用本功能。")
            return

        try:
            images = await self._get_images_from_event(event, self._int_conf("timeout", DEFAULT_TIMEOUT))
            yield event.plain_result(self._generation_start_text(args, images, requested_level))

            payload: Dict[str, Any] = {
                "prompt": args.prompt,
                "is_r18": args.is_r18 or requested_level in (LEVEL_R18, LEVEL_R18G),
                "is_r18g": args.is_r18g or requested_level == LEVEL_R18G,
            }
            if images:
                payload["image"] = base64.b64encode(images[0]).decode("utf-8")
                payload["images"] = [base64.b64encode(item).decode("utf-8") for item in images]
                payload["nai_ref_mode"] = args.nai_ref_mode
                payload["denoising_strength"] = args.denoising_strength

            asset = await self._api().generate(payload)
            result = await self._send_generated_asset(event, asset, prompt_level)
            if isinstance(result, list):
                for item in result:
                    yield item
            else:
                yield result
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    def _generation_start_text(self, args: GenerateArgs, images: List[bytes], requested_level: str) -> str:
        if not images:
            mode = "文生图"
        elif args.nai_ref_mode == "vt":
            mode = f"风格参考 VT ×{len(images)}"
        elif args.nai_ref_mode == "pr":
            mode = f"角色参考 PR ×{len(images)}"
        else:
            mode = f"图生图 (去噪 {args.denoising_strength:g})"
        lines = [f"🎨 已提交 · {mode}", f"提示词：{self._truncate(args.prompt, 60)}"]
        if args.uses_nai:
            lines.append("模型：NovelAI（走独立 NAI 额度）")
        if requested_level != LEVEL_SAFE:
            lines.append(f"分级：{requested_level.upper()}（当前会话已放行）")
        lines.append("绘图服务由 小维151 提供支持")
        return "\n".join(lines)

    @filter.command("测试来点", aliases={"test_xwdraw"}, prefix_optional=True)
    async def on_test_generate(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=1)
        prompt = parts[1].strip() if len(parts) > 1 else "无提示词"
        args = self.parse_generate_args(prompt)
        level, matched = scan_prompt(args.prompt, self._extra_block_patterns())
        images = await self._get_images_from_event(event, self._int_conf("timeout", DEFAULT_TIMEOUT))

        lines = [
            "🧪 测试模式（不会真的生成）",
            f"解析提示词：{self._truncate(args.prompt, 120)}",
            f"去噪强度：{args.denoising_strength:g}　参考图用途：{args.nai_ref_mode}",
            f"提示词预筛：{level}" + ("　命中：" + "、".join(matched[:6]) if matched else ""),
            f"分级放行：{self._allowance(event).describe()}",
            f"检测到图片：{len(images)} 张",
        ]
        chain: List[Any] = [Plain("\n".join(lines))]
        if images and self._bool_conf("test_echo_image_enabled", False):
            decision = await self.review_local_image(images[0], event)
            if decision.allowed and hasattr(Image, "fromBytes"):
                chain.append(Image.fromBytes(images[0]))
            elif not decision.allowed:
                chain.append(Plain("\n" + await self._blocked_text(event, "图片回显", decision)))
        elif images:
            chain.append(Plain("\n图片回显默认关闭，仅确认已检测到图片。"))
        yield event.chain_result(chain)

    # =======================================================================
    # 指令：帮助
    # =======================================================================

    @filter.command("绘图帮助", aliases={"xw帮助", "xwhelp"}, prefix_optional=True)
    async def on_help(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        topic = parts[1].strip().lower() if len(parts) > 1 else ""
        if topic in {"参数", "param", "params", "语法"}:
            yield event.plain_result(self._help_params())
            return
        if topic in {"nai", "novelai"}:
            yield event.plain_result(self._help_nai())
            return
        if topic in {"预设", "preset", "presets"}:
            yield event.plain_result(self._help_presets())
            return
        if topic in {"审核", "r18", "review"}:
            yield event.plain_result(self._help_review(event))
            return
        if topic in {"管理", "admin", "管理员"}:
            if not await self._is_bot_admin(event):
                yield event.plain_result("管理员菜单仅 bot 管理员可见。")
                return
            yield event.plain_result(self._help_admin())
            return
        yield event.plain_result(self._help_overview(await self._is_bot_admin(event), await self._is_switch_admin(event)))

    def _help_overview(self, is_bot_admin: bool, is_switch_admin: bool) -> str:
        sections: List[Tuple[str, List[str]]] = [
            (
                "✦ 生成",
                [
                    "来点 <提示词>　　　文生图",
                    "附图 + 来点 <提示词>　图生图",
                    "测试来点 <提示词>　　只解析不生成",
                ],
            ),
            (
                "✦ 预设",
                [
                    "预设列表 / 角色列表 / 风格列表 / 服装列表",
                    "预设搜索 <关键词>",
                    "预设详情 <角色|风格|服装> <名称>",
                    "预设图片 <类型> <名称> · 服装预览 <名称>",
                ],
            ),
            (
                "✦ 信息",
                [
                    "绘图状态　当前开关与放行状态",
                    "绘图公告 · 推荐提示 · 绘图队列 · 绘图模型",
                ],
            ),
            (
                "✦ 更多",
                [
                    "绘图帮助 参数　　参数与多角色语法",
                    "绘图帮助 nai　　 NovelAI 与参考图",
                    "绘图帮助 预设　　预设怎么用",
                    "绘图帮助 审核　　R18 分级说明",
                ],
            ),
        ]
        if is_switch_admin:
            sections.append(("✦ 群管理", ["绘图开启 / 绘图关闭 / 绘图开关 开|关"]))
        if is_bot_admin:
            sections.append(("✦ Bot 管理员", ["绘图帮助 管理　查看全部维护指令"]))
        return self.render_card(f"🎨 小维绘图 · 帮助 v{PLUGIN_VERSION}", sections, "服务由 小维151 提供")

    def _help_params(self) -> str:
        return self.render_card(
            "⚙️ 绘图参数速查",
            [
                (
                    "✦ 插件解析",
                    [
                        "-d <0~1>　图生图去噪强度，默认 0.6，越低越像原图",
                        "--vt / --pr　参考图用途改为风格参考 / 角色参考（NAI）",
                        "--r18 / --r18g　标记分级，仅在放行会话可用",
                    ],
                ),
                (
                    "✦ 服务端解析（原样写在提示词里）",
                    [
                        "-n <词条>　负面提示，其后全部算负面直到下一个参数",
                        "-w <宽> -h <高>　分辨率，各自 < 2048",
                        "-s <步数>　默认 30　·　-c <CFG>　控制系数",
                        "-m <模型 id>　默认 0，见「绘图模型」",
                        "也可以直接写 1920x1080 指定尺寸",
                    ],
                ),
                (
                    "✦ 预设与随机",
                    [
                        "弗洛洛,1920x1080,风格009,场景002",
                        "自定义0.9今汐,女仆装　（自定义=去掉默认服装，0.9=lora 权重）",
                        "小维,随机服装 / 随机风格 / 随机场景 / 随机预设",
                    ],
                ),
                (
                    "✦ 多角色",
                    [
                        "按实际人数写 2girl / 3boy（不加 s）",
                        "预设角色自带 BREAK 且自动排在最后",
                        "角色名{英文词条} 给单个角色单独描述",
                        "结尾加 上下 / 左右 切换分区方向（默认左右）",
                        "用 danbooru 标签时角色放最后并加 BREAK（大写）",
                    ],
                ),
            ],
            "正面提示词支持中文自动翻译",
        )

    def _help_nai(self) -> str:
        return self.render_card(
            "☁️ NovelAI V4.5",
            [
                (
                    "✦ 启用",
                    [
                        "提示词里写 nai 或 -m 90　→ V4.5 Curated",
                        "提示词里写 nai1 或 -m 91　→ V4.5 Full",
                        "走独立 NAI 额度，与本地额度不通用",
                    ],
                ),
                (
                    "✦ 额度消耗",
                    [
                        "纯文生图 = 1　·　整图重绘 = 1",
                        "风格参考 VT = 10 × 张数",
                        "角色参考 PR = 25 × 张数（含自动立绘）",
                    ],
                ),
                (
                    "✦ 参考图",
                    [
                        "附图 + --vt　只学画风色调，不复制构图角色",
                        "附图 + --pr　保持角色长相服装一致",
                        "最多 4 张；也可在提示词里写 VT0.8{1}, PR{2,角色}",
                        "底图取第一张没被 VT/PR 占用的图",
                    ],
                ),
                (
                    "✦ 语法差异",
                    [
                        "权重用 1.3::blue eyes:: 或 {tag}，(tag:1.3) 会自动换算",
                        "角色定位 名字{@C3, 词条}，5x5 格 A1 左上 / E5 右下",
                        "风格 039-050 为 NAI 画师串，使用后自动切 nai 架构",
                        "不支持局部重绘与 BREAK 分区",
                    ],
                ),
            ],
            "无需本地 Lora，知名角色直接写 danbooru 标签",
        )

    def _help_presets(self) -> str:
        return self.render_card(
            "🎭 预设用法",
            [
                (
                    "✦ 查看",
                    [
                        "预设列表　　各类预设数量总览",
                        "角色列表 / 风格列表 / 服装列表",
                        "预设搜索 <关键词>　名字太多时优先用它",
                        "预设详情 <角色|风格|服装> <名称>",
                        "预设图片 <类型> <名称> · 服装预览 <名称>",
                    ],
                ),
                (
                    "✦ 用在提示词里",
                    [
                        "直接写预设名即可：长离,风格026,服装010",
                        "自定义<角色> 去掉该角色的默认服装",
                        "自定义0.9<角色> 指定 lora 权重（建议 0.8~1.2）",
                    ],
                ),
                (
                    "✦ 架构限制",
                    [
                        "预设分属 illi / anima / nai 三种架构，不能混用",
                        "一次生成只用一个架构，冲突的预设会被自动丢弃",
                        "用 -m 明确指定模型时以该模型架构为准",
                        "生成结果里会提示哪些预设被丢弃",
                    ],
                ),
            ],
            "角色翻译表是通用 danbooru 标签，不绑架构",
        )

    def _help_review(self, event: AstrMessageEvent) -> str:
        policy = self._policy()
        threshold = self._score_threshold()
        return self.render_card(
            "🛡️ R18 分级审核",
            [
                (
                    "✦ 当前状态",
                    [
                        f"版本形态：{self._edition_label()}",
                        f"审核策略：{policy}　拦截阈值：{threshold:.2f}",
                        f"本会话放行：{self._allowance(event).describe()}",
                    ],
                ),
                (
                    "✦ 判定依据",
                    [
                        "提示词预筛　命中 R18/R18G 词条时在提交前就拦下",
                        "服务端标记　is_r18/is_r18g 只用于拦截，不用于放行",
                        "分类器分数　nsfw_score 是唯一的放行依据",
                        "外部视觉审核　可选，作为额外一道关",
                    ],
                ),
                (
                    "✦ 阈值档位",
                    [
                        "strict 0.80　连泳装擦边也拦",
                        "standard 0.90　只拦明确 R18（默认）",
                        "loose 0.97　几乎只拦最露骨的",
                    ],
                ),
            ],
            "拿不到分数时默认保守不发送",
        )

    def _help_admin(self) -> str:
        return self.render_card(
            "🔧 Bot 管理员指令",
            [
                ("✦ 账号", ["绘图账号　额度与用量", "绘图版本　服务端版本", "生成配置　模型与参数范围"]),
                (
                    "✦ 图库",
                    [
                        "最近图片 [数量] [--r18|--all]",
                        "图片元数据 <日期>/<文件名>",
                        "更新图片标签 <日期>/<文件名> r18=true r18g=false",
                        "画廊列表 [页码] [关键词] · 画廊筛选",
                        "画廊删除 <日期>/<文件名>",
                    ],
                ),
                ("✦ 自定义预设", ["我的预设", "添加预设 <名称>|<内容>", "删除预设 <名称>"]),
                ("✦ 审核", ["R18状态 / R18开启 / R18关闭 [含G]", "绘图审核 [数量]　最近审核记录"]),
                ("✦ 文档", ["绘图文档　列出可下载文档", "绘图文档 <名称>"]),
            ],
            "R18 放行是会话级持久化设置",
        )

    # =======================================================================
    # 指令：开关与状态
    # =======================================================================

    @filter.command("绘图开关", aliases={"绘图开启", "绘图关闭", "xw开关", "xwdraw_switch"}, prefix_optional=True)
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
            yield event.plain_result(f"已{'开启' if action else '关闭'}当前会话的绘图总开关。\n{self._switch_status_text(event)}")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    def _switch_status_text(self, event: AstrMessageEvent) -> str:
        session_id = self._session_id(event) or "unknown"
        overrides = self._session_switch_overrides()
        if session_id in overrides:
            source, enabled = "当前会话设置", bool(overrides[session_id])
        else:
            source, enabled = "配置默认值", self._bool_conf("plugin_enabled", True)
        return (
            f"绘图总开关：{'开启' if enabled else '关闭'}\n"
            f"作用范围：当前会话 ({session_id})\n"
            f"状态来源：{source}"
        )

    @filter.command("绘图状态", aliases={"xw状态", "xwdraw_status"}, prefix_optional=True)
    async def on_status(self, event: AstrMessageEvent):
        enabled = self._is_plugin_enabled_for_event(event)
        is_bot_admin = await self._is_bot_admin(event)
        queue_text = "查询失败"
        try:
            queue = await self._api().queue_status()
            queue_text = str(queue.get("queue_count")) if isinstance(queue, dict) else str(queue)
        except Exception as exc:
            logger.debug(f"查询队列失败: {exc}")
        endpoint = self._api().base_url if is_bot_admin else self.mask_endpoint(self._api().base_url)

        sections: List[Tuple[str, List[str]]] = [
            (
                "✦ 开关",
                [
                    f"绘图总开关：{'开启 ✅' if enabled else '关闭 ❌'}",
                    f"版本形态：{self._edition_label()}",
                    f"分级放行：{self._allowance(event).describe()}",
                ],
            ),
            (
                "✦ 审核",
                [f"策略：{self._policy()}　阈值：{self._score_threshold():.2f}", f"外部审核：{'开启' if self._bool_conf('external_review_enabled', False) else '关闭'}"],
            ),
            ("✦ 服务", [f"当前排队：{queue_text}", f"接口：{endpoint}"]),
        ]
        if is_bot_admin:
            try:
                account = await self._api().verify()
                if isinstance(account, dict):
                    sections.append(
                        (
                            "✦ 额度",
                            [
                                f"本地：{account.get('usage')} / {account.get('quota')}（剩余 {account.get('remaining')}）",
                                f"NAI：{account.get('nai_usage')} / {account.get('nai_quota')}（剩余 {account.get('nai_remaining')}）",
                            ],
                        )
                    )
            except Exception as exc:
                logger.debug(f"查询账号失败: {exc}")
        yield event.plain_result(self.render_card("📊 小维绘图 · 状态", sections, "绘图帮助 查看指令"))

    @filter.command(
        "R18开关",
        aliases={"R18开启", "R18关闭", "R18状态", "r18开关", "r18开启", "r18关闭", "r18状态", "r18_switch"},
        prefix_optional=True,
    )
    async def on_r18_switch(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        action = self._parse_switch_action(raw)
        allowance = self._allowance(event)
        if action is None:
            yield event.plain_result(
                self.render_card(
                    "🛡️ R18 放行状态",
                    [
                        (
                            "",
                            [
                                f"当前会话：{allowance.describe()}",
                                f"版本形态：{self._edition_label()}",
                                f"审核策略：{self._policy()}　阈值：{self._score_threshold():.2f}",
                            ],
                        ),
                        ("✦ 用法", ["R18开启　放行 R18", "R18开启 含G　同时放行 R18G", "R18关闭　恢复拦截", "R18跟随　清除本会话设置"]),
                    ],
                )
            )
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        include_gore = bool(re.search(r"含G|r18g|加G|gore", raw, flags=re.I))
        try:
            self._set_r18_override(event, action, action and include_gore)
            self.review_cache = DecisionCache()
            yield event.plain_result(
                f"已更新本会话 R18 放行设置。\n{self._allowance(event).describe()}"
                + ("\n⚠️ R18G 已放行，请确认这是你要的。" if action and include_gore else "")
            )
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("R18跟随", aliases={"R18默认", "r18跟随", "r18默认", "r18_reset"}, prefix_optional=True)
    async def on_r18_reset(self, event: AstrMessageEvent):
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        self._clear_r18_override(event)
        self.review_cache = DecisionCache()
        yield event.plain_result(f"已清除本会话 R18 设置，改为跟随插件配置。\n{self._allowance(event).describe()}")

    @filter.command("绘图审核", aliases={"审核日志", "review_log"}, prefix_optional=True)
    async def on_review_log(self, event: AstrMessageEvent):
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        limit = self._parse_limit(event.message_str.strip().split()[1:], default=10, max_value=30)
        stats = self.review_log.stats()
        rows = []
        for entry in self.review_log.recent(limit):
            score = f"{entry['score']:.3f}" if entry["score"] is not None else "—"
            head = (
                f"{'✅' if entry['allowed'] else '❌'} {entry['time']}　{entry['level']}"
                f"　score={score}　来源={entry['source']}"
            )
            detail = f"  会话 {entry['session']}　用户 {entry['sender']}"
            if entry["filename"]:
                detail += f"　文件 {entry['filename']}"
            rows.append(head + "\n" + detail)
        yield event.plain_result(
            self.render_card(
                "🛡️ 最近审核记录",
                [("", rows or ["暂无记录"])],
                f"累计 {stats['total']} 次，拦截 {stats['blocked']} 次",
            )
        )

    # =======================================================================
    # 指令：公开信息
    # =======================================================================

    @filter.command("绘图公告", aliases={"xw公告"}, prefix_optional=True)
    async def on_announcements(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            data = await self._api().announcements()
            items = data.get("announcements", []) if isinstance(data, dict) else []
            rows = []
            for item in items[:6]:
                mark = "🔴" if item.get("importance") == "important" else "•"
                rows.append(f"{mark} [{item.get('date', '')}] {item.get('title', '')}")
                rows.append(f"   {self._truncate(item.get('content', ''), 160)}")
            yield event.plain_result(self.render_card("📢 绘图公告", [("", rows or ["暂无公告"])]))
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
            prompts = data.get("prompts", []) if isinstance(data, dict) else []
            rows = [f"{idx}. {self._truncate(item, 140)}" for idx, item in enumerate(prompts[:12], 1)]
            yield event.plain_result(self.render_card("💡 推荐提示", [("", rows or ["暂无内容"])]))
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
            yield event.plain_result(f"⏳ 当前绘图排队数：{count}")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("绘图模型", aliases={"模型列表", "xw模型"}, prefix_optional=True)
    async def on_models(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        try:
            config = await self._api().generation_config()
            if not isinstance(config, dict):
                yield event.plain_result("获取模型列表失败。")
                return
            architectures = config.get("architectures", {}) or {}
            grouped: Dict[str, List[str]] = {}
            for model in config.get("models", []) or []:
                arch = str(model.get("architecture", "其他"))
                desc = (model.get("meta") or {}).get("desc", "")
                line = f"-m {model.get('id')}　{model.get('display_name')}"
                if desc:
                    line += f"　· {desc}"
                grouped.setdefault(arch, []).append(line)
            sections = []
            for arch, lines in grouped.items():
                label = (architectures.get(arch) or {}).get("display_name", arch)
                sections.append((f"✦ {label}（{arch}）", lines))
            sections.append(
                (
                    "✦ 范围",
                    [
                        f"步数 {config.get('steps_range')}　CFG {config.get('cfg_range')}",
                        f"宽 {config.get('width_range')}　高 {config.get('height_range')}",
                        f"默认架构 {config.get('default_architecture')}　默认步数 {config.get('default_steps')}",
                    ],
                )
            )
            yield event.plain_result(self.render_card("🧩 可用模型", sections, "在提示词里用 -m <id> 指定"))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    # =======================================================================
    # 指令：预设
    # =======================================================================

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
        rows = []
        for key, label in (
            ("character_lora", "角色预设"),
            ("character_translation", "角色翻译表"),
            ("style", "风格预设"),
            ("costume", "服装预设"),
            ("user_presets", "用户预设"),
        ):
            if key in presets:
                rows.append(f"{label}：{len(presets[key])} 个")
        yield event.plain_result(
            self.render_card(
                "🎭 预设总览",
                [("", rows)],
                "角色列表 / 风格列表 / 服装列表 / 预设搜索 <关键词>",
            )
        )

    async def _send_preset_list(self, event: AstrMessageEvent, key: str, title: str):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        presets = await self._get_presets()
        if not presets or key not in presets:
            yield event.plain_result(f"获取{title}失败。")
            return
        entries = self._preset_entries(presets[key])
        lines = [
            f"{idx}. {name}" + (f"　别名 {'/'.join(aliases[1:4])}" if len(aliases) > 1 else "")
            for idx, (name, aliases, _content) in enumerate(entries, 1)
        ]
        yield self._forward_nodes(event, f"{title}（共 {len(entries)} 个）", lines)

    @staticmethod
    def _preset_entries(collection: Any) -> List[Tuple[str, List[str], str]]:
        """服务端三种预设结构统一成 (显示名, 全部别名, 内容)。

        character_lora / style / character_translation 是 {内容: [别名…]}，
        costume 是 {名称: 内容}，user_presets 是 {名称: {content: …}}。
        """
        entries: List[Tuple[str, List[str], str]] = []
        if not isinstance(collection, dict):
            return [(str(item), [str(item)], "") for item in collection or []]
        for key, value in collection.items():
            if isinstance(value, list) and value:
                aliases = [str(alias) for alias in value]
                entries.append((aliases[0], aliases, str(key)))
            elif isinstance(value, dict):
                entries.append((str(key), [str(key)], str(value.get("content", ""))))
            else:
                entries.append((str(key), [str(key)], str(value)))
        return entries

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

    @filter.command("预设搜索", aliases={"搜索预设", "preset_search"}, prefix_optional=True)
    async def on_search_presets(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result("用法：预设搜索 <关键词>")
            return
        keyword = parts[1].strip()
        presets = await self._get_presets()
        if not presets:
            yield event.plain_result("获取预设失败，请稍后重试。")
            return

        arch_map = presets.get("preset_arch", {}) if isinstance(presets.get("preset_arch"), dict) else {}
        needle = keyword.lower()
        sections: List[Tuple[str, List[str]]] = []
        for key, label, show_content in (
            ("character_lora", "角色", False),
            ("character_translation", "角色翻译", True),
            ("style", "风格", False),
            ("costume", "服装", True),
            ("user_presets", "用户预设", True),
        ):
            hits = []
            for name, aliases, content in self._preset_entries(presets.get(key, {})):
                if not any(needle in alias.lower() for alias in aliases):
                    continue
                line = name
                if len(aliases) > 1:
                    line += f"　别名 {'/'.join(aliases[1:4])}"
                arch = next((arch_map[alias] for alias in aliases if isinstance(arch_map.get(alias), list)), None)
                if arch:
                    line += f"　[{'/'.join(arch)}]"
                if show_content and content:
                    line += f"\n  {self._truncate(content, 70)}"
                hits.append(line)
                if len(hits) >= 15:
                    break
            if hits:
                sections.append((f"✦ {label}（{len(hits)}）", hits))
        if not sections:
            yield event.plain_result(f"没有找到包含「{keyword}」的预设。")
            return
        yield event.plain_result(self.render_card(f"🔍 预设搜索「{keyword}」", sections, "每类最多显示 15 条"))

    @filter.command("预设详情", aliases={"preset", "预设信息"}, prefix_optional=True)
    async def on_preset_detail(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法：预设详情 <类型> <名称>\n类型：character/style/costume 或 角色/风格/服装")
            return
        type_map = {
            "character": "character_lora",
            "角色": "character_lora",
            "style": "style",
            "风格": "style",
            "costume": "costume",
            "服装": "costume",
        }
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
            decision = await self.review_local_image(data, event)
            if not decision.allowed:
                yield event.plain_result(await self._blocked_text(event, "预设图片", decision))
                return
            yield await self._send_image_bytes(event, data, f"预设图片 · {parts[2]}", parts[2])
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

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
        name = parts[1].strip()
        try:
            data, _ctype = await self._api().costume_image(name)
            decision = await self.review_local_image(data, event)
            if not decision.allowed:
                yield event.plain_result(await self._blocked_text(event, "服装预览", decision))
                return
            yield await self._send_image_bytes(event, data, f"服装预览 · {name}", name + ".png")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    # =======================================================================
    # 指令：Bot 管理员
    # =======================================================================

    @filter.command("绘图账号", aliases={"绘图配额", "xw账号"}, prefix_optional=True)
    async def on_account(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        try:
            data = await self._api().verify()
            if not isinstance(data, dict):
                yield event.plain_result("绘图账号信息\n" + self._format_data(data))
                return
            yield event.plain_result(
                self.render_card(
                    "👤 绘图账号",
                    [
                        ("", [f"备注：{data.get('comment', '—')}"]),
                        (
                            "✦ 本地额度",
                            [f"已用 {data.get('usage')} / {data.get('quota')}", f"剩余 {data.get('remaining')}"],
                        ),
                        (
                            "✦ NAI 额度",
                            [
                                f"已用 {data.get('nai_usage')} / {data.get('nai_quota')}",
                                f"剩余 {data.get('nai_remaining')}",
                            ],
                        ),
                    ],
                )
            )
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("绘图版本", aliases={"xw版本", "draw_version"}, prefix_optional=True)
    async def on_version(self, event: AstrMessageEvent):
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        try:
            data = await self._api().version()
            server = data.get("version") if isinstance(data, dict) else data
            yield event.plain_result(f"插件版本：v{PLUGIN_VERSION}\n服务端版本：{server}\n接口：{self._api().base_url}")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("生成配置", aliases={"绘图配置", "generation_config"}, prefix_optional=True)
    async def on_generation_config(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        try:
            data = await self._api().generation_config()
            yield event.plain_result("生成配置\n" + self._format_data(data))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("最近图片", aliases={"recent_images"}, prefix_optional=True)
    async def on_recent_images(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        tokens = event.message_str.strip().split()[1:]
        allowance = self._allowance(event)
        if self._has_sensitive_flag(tokens) and not allowance.r18:
            yield self._sensitive_denied_result(event, LEVEL_R18, [])
            return
        limit = self._parse_limit(tokens, default=12, max_value=50)
        include_r18 = "--r18" in tokens or "--all" in tokens
        include_r18g = "--r18g" in tokens or "--all" in tokens
        try:
            data = await self._api().recent_images(
                limit=limit, exclude_r18=not include_r18, exclude_r18g=not include_r18g
            )
            images = data.get("images", []) if isinstance(data, dict) else []
            rows = []
            for idx, item in enumerate(images[:limit], 1):
                marks = ("🔞" if item.get("is_r18") else "") + ("🩸" if item.get("is_r18g") else "")
                rows.append(f"{idx}. {item.get('date_folder')}/{item.get('filename')} {marks}")
                rows.append(f"   {self._truncate(item.get('original_input') or item.get('prompt') or '', 90)}")
            yield event.plain_result(
                self.render_card(
                    f"🖼️ 最近图片 · {len(images)}",
                    [("", rows or ["暂无记录"])],
                    "图片元数据 <日期>/<文件名> 查看详情",
                )
            )
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("图片元数据", aliases={"图片metadata", "image_metadata"}, prefix_optional=True)
    async def on_image_metadata(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
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
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
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
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        tokens = event.message_str.strip().split()[1:]
        if self._has_sensitive_flag(tokens) and not self._allowance(event).r18:
            yield self._sensitive_denied_result(event, LEVEL_R18, [])
            return
        page = self._parse_limit(tokens, default=1, max_value=999)
        search_tokens = [token for token in tokens if not token.isdigit() and not token.startswith("--")]
        params: Dict[str, Any] = {
            "page": page,
            "page_size": 10,
            "exclude_r18": str("--r18" not in tokens and "--all" not in tokens).lower(),
            "exclude_r18g": str("--r18g" not in tokens and "--all" not in tokens).lower(),
        }
        if search_tokens:
            params["search"] = " ".join(search_tokens)
        try:
            data = await self._api().gallery_images(params)
            yield event.plain_result(self._format_items(data, f"画廊列表 · 第 {page} 页", limit=10))
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("画廊筛选", aliases={"gallery_filters", "图库筛选"}, prefix_optional=True)
    async def on_gallery_filters(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        try:
            data = await self._api().gallery_filters()
            if not isinstance(data, dict):
                yield event.plain_result("画廊筛选项\n" + self._format_data(data))
                return
            dates = data.get("dates", []) or []
            users = data.get("users", []) or []
            yield event.plain_result(
                self.render_card(
                    "🗂️ 画廊筛选项",
                    [
                        ("✦ 日期", [f"共 {len(dates)} 天", "最近：" + "、".join(str(d) for d in dates[:10])]),
                        ("✦ 用户", [f"共 {len(users)} 个"]),
                    ],
                )
            )
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("画廊删除", aliases={"gallery_delete", "删除图片"}, prefix_optional=True)
    async def on_gallery_delete(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        date_folder, filename = self._extract_image_ref(event.message_str)
        if not date_folder or not filename:
            yield event.plain_result("用法：画廊删除 <日期>/<文件名>\n注意：删除不可撤销。")
            return
        try:
            data = await self._api().gallery_delete_image(date_folder, filename)
            yield event.plain_result(f"已删除 {date_folder}/{filename}\n{self._format_data(data, 400)}")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("我的预设", aliases={"自定义预设", "mypresets"}, prefix_optional=True)
    async def on_my_presets(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        try:
            data = await self._api().user_presets()
            presets = data.get("user_presets", {}) if isinstance(data, dict) else {}
            rows = [
                f"{idx}. {name}　{self._truncate((info or {}).get('content', ''), 60)}"
                for idx, (name, info) in enumerate(list(presets.items())[:30], 1)
            ]
            yield event.plain_result(
                self.render_card(f"📝 我的预设 · {len(presets)}", [("", rows or ["暂无自定义预设"])])
            )
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("添加预设", aliases={"新建预设", "addpreset"}, prefix_optional=True)
    async def on_add_preset(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
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
            yield event.plain_result(result.get("message", "预设添加成功") if isinstance(result, dict) else "预设添加成功")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("删除预设", aliases={"移除预设", "delpreset"}, prefix_optional=True)
    async def on_delete_preset(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：删除预设 <名称>")
            return
        name = parts[1].strip()
        try:
            await self._api().delete_user_preset(name)
            await self._get_presets(force_refresh=True)
            yield event.plain_result(f"预设「{name}」已删除")
        except Exception as exc:
            yield await self._handle_api_error(event, exc)

    @filter.command("绘图文档", aliases={"xw文档", "drawing_doc"}, prefix_optional=True)
    async def on_document(self, event: AstrMessageEvent):
        disabled = self._disabled_result(event)
        if disabled:
            yield disabled
            return
        denied = await self._admin_only_result(event)
        if denied:
            yield denied
            return
        parts = event.message_str.strip().split(maxsplit=1)
        listing = self.render_card(
            "📚 绘图文档",
            [("", [f"{idx}. {name}　{desc}" for idx, (name, desc, _) in enumerate(DOCUMENTS, 1)])],
            "绘图文档 <名称或序号>",
        )
        if len(parts) < 2:
            yield event.plain_result(listing)
            return

        doc_name = parts[1].strip()
        if doc_name.isdigit() and 1 <= int(doc_name) <= len(DOCUMENTS):
            doc_name = DOCUMENT_NAMES[int(doc_name) - 1]
        entry = next((item for item in DOCUMENTS if item[0] == doc_name), None)
        if entry is None:
            yield event.plain_result(f"没有名为「{doc_name}」的文档。\n{listing}")
            return
        if not entry[2]:
            yield event.plain_result(f"「{doc_name}」体积过大，不适合直接发送。\n请到 {self._api().base_url}/dictionary.txt 查看。")
            return
        try:
            data, content_type = await self._api().document(doc_name)
            suffix = ".docx" if "officedocument" in content_type else ".bin"
            yield await self._send_file_bytes(event, data, doc_name + suffix)
        except Exception as exc:
            yield await self._handle_api_error(event, exc)
