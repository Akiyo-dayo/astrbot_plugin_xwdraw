import asyncio
import base64
import io
from pathlib import Path
from typing import Optional, Dict, Any, List

import aiohttp

from astrbot import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Plain, Reply, Node, Nodes
from astrbot.core.platform.astr_message_event import AstrMessageEvent
import time
from urllib.parse import quote, unquote, urlparse


@register(
    "astrbot_plugin_xwdraw",
    "xwdraw",
    "集成远端 SD 绘图服务，支持文生图、图生图、预设管理等功能",
    "0.2.0",
    "",
)
class XWDrawPlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.conf = config
        # 插件数据目录，用于临时保存生成图片
        try:
            self.plugin_data_dir = StarTools.get_data_dir()
        except Exception:
            self.plugin_data_dir = Path("./data")
        if not self.plugin_data_dir.exists():
            try:
                self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        
        # 缓存预设数据
        self.presets_cache = None
        self.cache_time = 0
        self.cache_duration = 300  # 缓存5分钟

    async def initialize(self):
        # 读取配置并打印提示
        api_url = self.conf.get("api_url", "https://sd.loping151.com/api/generate")
        timeout = self.conf.get("timeout", 60)
        logger.info(f"astrbot_plugin_xwdraw 已加载, 默认 api_url={api_url}, timeout={timeout}s")

    async def _fetch_image_bytes(self, url_or_path: str, timeout: int, headers: dict = None) -> Optional[bytes]:
        # 支持本地文件路径或 http(s) url
        try:
            if Path(url_or_path).is_file():
                return Path(url_or_path).read_bytes()
            if url_or_path.startswith("http"):
                timeout_obj = aiohttp.ClientTimeout(total=timeout)
                async with aiohttp.ClientSession(timeout=timeout_obj) as sess:
                    async with sess.get(url_or_path, headers=headers) as resp:
                        resp.raise_for_status()
                        return await resp.read()
        except Exception as e:
            logger.warning(f"_fetch_image_bytes 失败: {e}")
        return None

    def _build_image_url(self, api_url: str, filename: str) -> str:
        """构造图片 URL，使用 /api/image/{date_folder}/{filename} 格式，自动对 path 部分做 URL 编码以兼容中文/特殊字符。"""
        if not filename:
            return ""
        s = str(filename)
        if s.startswith("http"):
            return s

        # 取出主机根（例如 https://example.com）
        try:
            p = urlparse(api_url)
            root = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else api_url.split("/api/", 1)[0]
        except Exception:
            root = api_url.split("/api/", 1)[0]

        path = s.lstrip('/')
        # 如果 filename 含有百分号（可能已经被编码），先做一次解码再按 segment 编码，避免双重编码
        try:
            maybe_unq = unquote(path)
        except Exception:
            maybe_unq = path

        # 对每个 path segment 分别编码，保留分隔符 /
        try:
            segments = [quote(unquote(seg), safe='') for seg in maybe_unq.split('/')]
            encoded = '/'.join(segments)
        except Exception:
            encoded = quote(maybe_unq, safe="/")

        # 使用 /api/image/ 路径格式
        return f"{root.rstrip('/')}/api/image/{encoded}"

    async def _download_with_fallbacks(self, url: str, timeout: int, api_key: str = None) -> Optional[bytes]:
        """尝试多种方式下载图片：先使用 iwf._download_image（若存在），
        然后尝试直接下载（带 Bearer token），还会尝试 URL 解码/再编码的变体。
        返回 bytes 或 None。
        """
        attempts = []
        if not url:
            return None

        # 准备认证头
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 解析原始 URL，构造更多可尝试的变体：原样、解码后、按 segment 编码、只对最后段编码等
        try:
            parsed = urlparse(url)
            root = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ''
            path = parsed.path.lstrip('/')
        except Exception:
            root = ''
            path = url

        raw_url = url
        attempts.append(raw_url)

        # 原始 path 解码
        try:
            unq_path = unquote(path)
            if unq_path and unq_path != path:
                if root:
                    attempts.append(f"{root}/{unq_path}")
                else:
                    attempts.append(unq_path)
        except Exception:
            unq_path = path

        # 每段分别编码（safe=''，避免双重编码问题）
        try:
            perseg = '/'.join(quote(unquote(seg), safe='') for seg in unq_path.split('/'))
            if root:
                perseg_url = f"{root}/{perseg}"
            else:
                perseg_url = perseg
            if perseg_url not in attempts:
                attempts.append(perseg_url)
        except Exception:
            perseg_url = None

        # 只对最后一段进行编码的变体
        try:
            segs = unq_path.split('/')
            if segs:
                last_enc = quote(unquote(segs[-1]), safe='')
                last_variant = '/'.join(segs[:-1] + [last_enc])
                if root:
                    last_url = f"{root}/{last_variant}"
                else:
                    last_url = last_variant
                if last_url not in attempts:
                    attempts.append(last_url)
        except Exception:
            pass

        # 去重并尝试
        seen = set()
        for u in attempts:
            if not u or u in seen:
                continue
            seen.add(u)
            logger.info(f"尝试下载图片：{u}")
            # 1) 如果有 ImageWorkflow，优先调用它（会处理代理/ssl 等）
            try:
                if hasattr(self, 'iwf') and self.iwf:
                    img = await self.iwf._download_image(u)
                    if img:
                        logger.info(f"使用 iwf 下载成功: {u}")
                        return img
            except Exception as e:
                logger.debug(f"iwf 下载尝试失败 ({u}): {e}")

            # 2) 直接用 http 下载（带认证头）
            try:
                data = await self._fetch_image_bytes(u, timeout, headers)
                if data:
                    logger.info(f"直接下载成功: {u}")
                    return data
            except Exception as e:
                logger.debug(f"直接下载尝试失败 ({u}): {e}")

        return None

    async def _get_image_from_event(self, event: AstrMessageEvent, timeout: int) -> Optional[bytes]:
        """从消息事件中获取图片（支持直接发送和回复引用）"""
        # 1. 检查回复链
        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and hasattr(seg, 'chain') and seg.chain:
                for s_chain in seg.chain:
                    if isinstance(s_chain, Image):
                        img_src = getattr(s_chain, "url", None) or getattr(s_chain, "file", None)
                        if img_src:
                            return await self._fetch_image_bytes(img_src, timeout)
        
        # 2. 检查当前消息
        for seg in event.message_obj.message:
            if isinstance(seg, Image):
                img_src = getattr(seg, "url", None) or getattr(seg, "file", None)
                if img_src:
                    return await self._fetch_image_bytes(img_src, timeout)
        return None

    @filter.command("测试来点", aliases={"test_xwdraw"}, prefix_optional=True)
    async def on_test_generate(self, event: AstrMessageEvent):
        """测试绘图功能，不实际请求API"""
        raw = event.message_str.strip()
        parts = raw.split(maxsplit=1)
        prompt = parts[1].strip() if len(parts) > 1 else "无提示词"
        
        timeout_sec = int(self.conf.get("timeout", 60))
        img_bytes = await self._get_image_from_event(event, timeout_sec)
        
        msg_chain = [Plain(f"🧪 测试模式\n提示词: {prompt}\n")]
        
        if img_bytes:
            msg_chain.append(Plain("检测到图片输入，已成功获取图片数据。\n"))
            if hasattr(Image, 'fromBytes'):
                msg_chain.append(Image.fromBytes(img_bytes))
            else:
                # Fallback: save to temp file
                try:
                    out_dir = self.plugin_data_dir / "test"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_path = out_dir / f"test_{int(time.time())}.png"
                    out_path.write_bytes(img_bytes)
                    msg_chain.append(Image.fromFileSystem(str(out_path)))
                except Exception:
                    msg_chain.append(Plain("(图片回显失败)"))
        else:
            msg_chain.append(Plain("未检测到图片输入 (文生图模式)"))
            
        yield event.chain_result(msg_chain)

    @filter.command("来点", aliases={"小千来点", "xwdraw"}, prefix_optional=True)
    async def on_generate(self, event: AstrMessageEvent):
        """用法：小千来点 <prompt>
        该命令会调用配置中的远端 API（Bearer Token）进行生成。
        如果消息中包含图片，将自动进行图生图。"""

        raw = event.message_str.strip()
        parts = raw.split(maxsplit=1)

        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result("用法：小千来点 <提示词>\n提示：可以在发送提示词的同时附带图片进行图生图")
            return

        # 支持 -d 0.5 形式自定义去噪强度
        import re
        prompt_line = parts[1].strip()
        denoise = 0.7
        d_match = re.search(r"-d\s*([0-9.]+)", prompt_line)
        if d_match:
            try:
                denoise = float(d_match.group(1))
                prompt_line = re.sub(r"-d\s*[0-9.]+", "", prompt_line).strip()
            except Exception:
                pass
        prompt = prompt_line

        api_url = self.conf.get("api_url", "https://sd.loping151.com/api/generate")
        api_key = self.conf.get("api_key", "")
        timeout_sec = int(self.conf.get("timeout", 60))

        if not api_key:
            yield event.plain_result("请在插件配置中填写 `api_key` 后再使用本功能。")
            return

        # 获取图片（支持图生图）
        img_bytes = await self._get_image_from_event(event, timeout_sec)
        has_image = img_bytes is not None

        # 发送合并的提示：先显示正在生成，再换行显示感谢语
        display_prompt = prompt[:20] + "..." if len(prompt) > 20 else prompt
        mode_text = "图生图" if has_image else "文生图"
        yield event.plain_result(f"🎨 收到请求，正在{mode_text}生成 [{display_prompt}]\n感谢由小维151提供的绘图服务支持")

        payload = {"prompt": prompt}

        if has_image:
            try:
                b64 = base64.b64encode(img_bytes).decode("utf-8")
                payload["image"] = f"data:image/png;base64,{b64}"
                payload["denoising_strength"] = denoise
            except Exception as e:
                logger.warning(f"处理图片数据失败: {e}")

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

        # 发起请求并处理超时/错误，记录耗时；支持三类返回：直接图片bytes、json里返回base64、json里返回filename/url
        start_ts = time.time()
        try:
            timeout_obj = aiohttp.ClientTimeout(total=timeout_sec)
            async with aiohttp.ClientSession(timeout=timeout_obj) as sess:
                async with sess.post(api_url, json=payload, headers=headers) as resp:
                    elapsed = time.time() - start_ts

                    # 1) 如果直接返回 image/*
                    ctype = resp.headers.get("Content-Type", "").lower()
                    if resp.status == 200 and ctype.startswith("image/"):
                        img_bytes = await resp.read()
                        caption = f"✅ 生成成功 ({elapsed:.1f}s)"
                        try:
                            if hasattr(Image, 'fromBytes'):
                                yield event.chain_result([Image.fromBytes(img_bytes), Plain(caption)])
                                return
                        except Exception:
                            pass

                        # fallback: 保存并主动发送
                        try:
                            out_dir = self.plugin_data_dir / "generated"
                            out_dir.mkdir(parents=True, exist_ok=True)
                            out_path = out_dir / f"generated_{int(time.time())}.png"
                            out_path.write_bytes(img_bytes)
                            message_chain = MessageChain().file_image(str(out_path))
                            await self.context.send_message(event.unified_msg_origin, message_chain)
                            yield event.plain_result(caption)
                            return
                        except Exception as e:
                            logger.error(f"发送图片失败: {e}")
                            yield event.plain_result(f"生成成功，但发送图片失败: {e}")
                            return

                    # 否则尝试解析为 JSON
                    text = await resp.text()
                    try:
                        result = await resp.json()
                    except Exception:
                        result = {"raw_text": text}

                    if resp.status != 200:
                        msg = f"绘图服务返回错误 (HTTP {resp.status})，耗时 {elapsed:.1f}s。"
                        if isinstance(result, dict) and result.get("detail"):
                            msg += f" 原因: {result.get('detail')}"
                        else:
                            msg += f" 响应: {text}"
                        yield event.plain_result(msg)
                        return

                    # 2) JSON 中包含 image_base64
                    if isinstance(result, dict) and result.get("image_base64"):
                        try:
                            img_bytes = base64.b64decode(result.get("image_base64"))
                        except Exception:
                            img_bytes = None

                        if img_bytes:
                            caption = f"✅ 生成成功 ({elapsed:.1f}s)"
                            try:
                                if hasattr(Image, 'fromBytes'):
                                    yield event.chain_result([Image.fromBytes(img_bytes), Plain(caption)])
                                    return
                            except Exception:
                                pass
                            out_dir = self.plugin_data_dir / "generated"
                            out_dir.mkdir(parents=True, exist_ok=True)
                            out_path = out_dir / f"generated_{int(time.time())}.png"
                            out_path.write_bytes(img_bytes)
                            message_chain = MessageChain().file_image(str(out_path))
                            await self.context.send_message(event.unified_msg_origin, message_chain)
                            yield event.plain_result(caption)
                            return

                    # 3) JSON 中包含 filename/url
                    filename = None
                    if isinstance(result, dict):
                        filename = result.get("filename") or result.get("file") or result.get("url")

                    if filename:
                        # 构造并编码 URL，再使用多种回退策略下载（带认证）
                        url_or_b64 = self._build_image_url(api_url, filename)
                        img_bytes = await self._download_with_fallbacks(url_or_b64, timeout_sec, api_key)

                        if img_bytes:
                            caption = f"✅ 生成成功 ({elapsed:.1f}s)"
                            try:
                                if hasattr(Image, 'fromBytes'):
                                    yield event.chain_result([Image.fromBytes(img_bytes), Plain(caption)])
                                    return
                            except Exception:
                                pass
                            out_dir = self.plugin_data_dir / "generated"
                            out_dir.mkdir(parents=True, exist_ok=True)
                            out_path = out_dir / Path(str(filename)).name
                            out_path.write_bytes(img_bytes)
                            message_chain = MessageChain().file_image(str(out_path))
                            await self.context.send_message(event.unified_msg_origin, message_chain)
                            yield event.plain_result(caption)
                            return
                        else:
                            yield event.plain_result(f"生成成功，但图片下载失败，请稍后重试或联系管理员")
                            return

                    # 4) 其它：返回文本结果
                    yield event.plain_result(f"生成完成 (耗时 {elapsed:.1f}s)")
        except asyncio.TimeoutError:
            # 超时参考 shoubanhua：提示可能需要增加 timeout 或使用代理
            yield event.plain_result(f"请求超时（>{timeout_sec}s）。可能是生成耗时较长或网络问题，请稍后重试或在插件配置中增加 timeout。")
        except aiohttp.ClientError as e:
            logger.error(f"调用绘图API失败: {e}")
            yield event.plain_result(f"调用绘图服务失败：{e}")
        except Exception as e:
            logger.exception("未知错误")
            yield event.plain_result(f"发生未知错误：{e}")

    async def _api_request(self, method: str, endpoint: str, **kwargs) -> Any:
        """通用API请求方法"""
        api_url = self.conf.get("api_url", "https://sd.loping151.com/api/generate")
        api_key = self.conf.get("api_key", "")
        timeout_sec = int(self.conf.get("timeout", 60))
        
        # 构建完整URL
        try:
            p = urlparse(api_url)
            base = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else api_url.split("/api/", 1)[0]
        except Exception:
            base = api_url.split("/api/", 1)[0]
        
        url = f"{base.rstrip('/')}{endpoint}"
        
        headers = kwargs.pop("headers", {})
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        
        timeout_obj = aiohttp.ClientTimeout(total=timeout_sec)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout_obj) as sess:
                async with sess.request(method, url, headers=headers, **kwargs) as resp:
                    resp.raise_for_status()
                    if 'application/json' in resp.headers.get('Content-Type', ''):
                        return await resp.json()
                    else:
                        return await resp.read()
        except Exception as e:
            logger.error(f"API请求失败 ({method} {endpoint}): {e}")
            raise

    async def _get_presets(self, force_refresh: bool = False) -> Optional[Dict]:
        """获取预设数据（带缓存）"""
        current_time = time.time()
        if not force_refresh and self.presets_cache and (current_time - self.cache_time < self.cache_duration):
            return self.presets_cache
        
        try:
            presets = await self._api_request("GET", "/api/presets")
            self.presets_cache = presets
            self.cache_time = current_time
            return presets
        except Exception as e:
            logger.error(f"获取预设失败: {e}")
            return None

    @filter.command("绘图帮助", aliases={"xw帮助", "xwhelp"}, prefix_optional=True)
    async def on_help(self, event: AstrMessageEvent):
        """显示插件帮助信息"""
        help_text = """🎨 XW绘图插件使用指南

【基础绘图】
• 小千来点/来点 <提示词> - 文生图
• 小千来点/来点 <提示词> [附带图片] - 图生图
  提示：发送提示词的同时附带图片即可进行图生图

【高级参数】（可在提示词中使用）
• -s <数字> : 设置步数 (10-50)
• -c <数字> : 设置CFG (3-20)
• -w <数字> : 设置宽度 (768-2048)
• -h <数字> : 设置高度 (768-2048)
• -m <数字> : 设置模型索引
• -d <数字> : 去噪强度 (0.0-1.0，仅图生图)

示例：
  文生图：来点 1girl, solo -s 30 -w 1024 -h 1536
  图生图：来点 add details -d 0.6 [同时发送图片]

【预设管理】
• 预设列表 - 查看所有可用预设分类
• 角色列表 [页码] - 查看角色预设
• 风格列表 [页码] - 查看风格预设
• 服装列表 [页码] - 查看服装预设
• 预设详情 <类型> <名称> - 查看预设详细信息
  类型：character/style/costume

【自定义预设】
• 添加预设 <名称>|<内容> - 添加自定义预设
• 删除预设 <名称> - 删除自己的预设
• 我的预设 - 查看所有自定义预设

感谢由小维151提供的绘图服务支持"""
        yield event.plain_result(help_text)

    @filter.command("预设列表", aliases={"presets", "预设"}, prefix_optional=True)
    async def on_list_presets(self, event: AstrMessageEvent):
        """列出所有预设分类"""
        presets = await self._get_presets()
        if not presets:
            yield event.plain_result("❌ 获取预设列表失败，请稍后重试")
            return
        
        msg = "📋 预设分类列表\n\n"
        
        if "character_lora" in presets:
            msg += f"👤 角色预设: {len(presets['character_lora'])} 个\n"
        if "style" in presets:
            msg += f"🎨 风格预设: {len(presets['style'])} 个\n"
        if "costume" in presets:
            msg += f"👗 服装预设: {len(presets['costume'])} 个\n"
        if "user_presets" in presets:
            msg += f"⭐ 用户预设: {len(presets['user_presets'])} 个\n"
        
        msg += "\n使用 '角色列表'、'风格列表'、'服装列表' 查看详情\n"
        msg += "使用 '我的预设' 查看用户自定义预设"
        yield event.plain_result(msg)

    @filter.command("角色列表", aliases={"角色预设", "characters"}, prefix_optional=True)
    async def on_list_characters(self, event: AstrMessageEvent):
        """列出角色预设"""
        presets = await self._get_presets()
        if not presets or "character_lora" not in presets:
            yield event.plain_result("❌ 获取角色预设失败")
            return
        
        characters = list(presets["character_lora"].keys())
        
        # 构建合并转发消息节点
        nodes = []
        
        # 头部信息
        header_node = Node(
            uin=event.get_self_id(),
            name="小千",
            content=[Plain(f"👤 角色预设列表 (共 {len(characters)} 个)")]
        )
        nodes.append(header_node)
        
        # 分批构建节点，每批50个，避免单条消息过长
        batch_size = 50
        for i in range(0, len(characters), batch_size):
            batch = characters[i:i+batch_size]
            msg_content = ""
            for j, char in enumerate(batch, start=i+1):
                aliases = presets["character_lora"][char]
                alias_str = ", ".join(aliases[:2]) if isinstance(aliases, list) else char
                msg_content += f"{j}. {alias_str}\n"
            
            node = Node(
                uin=event.get_self_id(),
                name="小千",
                content=[Plain(msg_content.strip())]
            )
            nodes.append(node)
            
        yield event.chain_result([Nodes(nodes)])

    @filter.command("风格列表", aliases={"风格预设", "styles"}, prefix_optional=True)
    async def on_list_styles(self, event: AstrMessageEvent):
        """列出风格预设"""
        presets = await self._get_presets()
        if not presets or "style" not in presets:
            yield event.plain_result("❌ 获取风格预设失败")
            return
        
        styles = list(presets["style"].keys())
        
        # 构建合并转发消息节点
        nodes = []
        
        # 头部信息
        header_node = Node(
            uin=event.get_self_id(),
            name="小千",
            content=[Plain(f"🎨 风格预设列表 (共 {len(styles)} 个)")]
        )
        nodes.append(header_node)
        
        # 分批构建节点
        batch_size = 50
        for i in range(0, len(styles), batch_size):
            batch = styles[i:i+batch_size]
            msg_content = ""
            for j, style in enumerate(batch, start=i+1):
                aliases = presets["style"][style]
                alias_str = ", ".join(aliases[:2]) if isinstance(aliases, list) else style
                msg_content += f"{j}. {alias_str}\n"
            
            node = Node(
                uin=event.get_self_id(),
                name="小千",
                content=[Plain(msg_content.strip())]
            )
            nodes.append(node)
            
        yield event.chain_result([Nodes(nodes)])

    @filter.command("服装列表", aliases={"服装预设", "costumes"}, prefix_optional=True)
    async def on_list_costumes(self, event: AstrMessageEvent):
        """列出服装预设"""
        presets = await self._get_presets()
        if not presets or "costume" not in presets:
            yield event.plain_result("❌ 获取服装预设失败")
            return
        
        costumes = list(presets["costume"].keys())
        
        # 构建合并转发消息节点
        nodes = []
        
        # 头部信息
        header_node = Node(
            uin=event.get_self_id(),
            name="小千",
            content=[Plain(f"👗 服装预设列表 (共 {len(costumes)} 个)")]
        )
        nodes.append(header_node)
        
        # 分批构建节点
        batch_size = 50
        for i in range(0, len(costumes), batch_size):
            batch = costumes[i:i+batch_size]
            msg_content = ""
            for j, costume in enumerate(batch, start=i+1):
                msg_content += f"{j}. {costume}\n"
            
            node = Node(
                uin=event.get_self_id(),
                name="小千",
                content=[Plain(msg_content.strip())]
            )
            nodes.append(node)
            
        yield event.chain_result([Nodes(nodes)])

    @filter.command("预设详情", aliases={"preset", "预设信息"}, prefix_optional=True)
    async def on_preset_detail(self, event: AstrMessageEvent):
        """查看预设详情
        用法：预设详情 <类型> <名称>
        类型：character/style/costume
        """
        raw = event.message_str.strip()
        parts = raw.split(maxsplit=2)
        
        if len(parts) < 3:
            yield event.plain_result("用法：预设详情 <类型> <名称>\n类型：character/style/costume")
            return
        
        preset_type = parts[1].lower()
        preset_name = parts[2]
        
        # 类型映射
        type_map = {
            "character": "character_lora",
            "角色": "character_lora",
            "style": "style",
            "风格": "style",
            "costume": "costume",
            "服装": "costume"
        }
        
        api_type = type_map.get(preset_type)
        if not api_type:
            yield event.plain_result("❌ 未知类型，请使用：character/style/costume")
            return
        
        try:
            # URL编码预设名称
            encoded_name = quote(preset_name)
            detail = await self._api_request("GET", f"/api/preset-detail/{api_type}/{encoded_name}")
            
            msg = f"📝 预设详情\n\n"
            msg += f"名称: {detail.get('name', preset_name)}\n"
            msg += f"类型: {detail.get('type', api_type)}\n"
            msg += f"提示词: {detail.get('prompt', '未知')}\n"
            
            if detail.get('full_prompt'):
                msg += f"完整提示词: {detail['full_prompt'][:100]}...\n"
            
            images = detail.get('images', [])
            if images:
                msg += f"\n预览图: {len(images)} 张 (预览图功能已禁用)"
            
            yield event.plain_result(msg)
            
        except Exception as e:
            logger.error(f"获取预设详情失败: {e}")
            yield event.plain_result(f"❌ 获取预设详情失败: {e}")

    @filter.command("添加预设", aliases={"新建预设", "addpreset"}, prefix_optional=True)
    async def on_add_preset(self, event: AstrMessageEvent):
        """添加自定义预设
        用法：添加预设 <名称>|<内容>
        """
        raw = event.message_str.strip()
        parts = raw.split(maxsplit=1)
        
        if len(parts) < 2:
            yield event.plain_result("用法：添加预设 <名称>|<内容>\n示例：添加预设 我的风格|beautiful, detailed")
            return
        
        content = parts[1]
        if "|" not in content:
            yield event.plain_result("❌ 格式错误，请使用 | 分隔名称和内容")
            return
        
        preset_name, preset_content = content.split("|", 1)
        preset_name = preset_name.strip()
        preset_content = preset_content.strip()
        
        if not preset_name or not preset_content:
            yield event.plain_result("❌ 名称和内容不能为空")
            return
        
        try:
            payload = {
                "preset_name": preset_name,
                "preset_content": preset_content
            }
            result = await self._api_request("POST", "/api/user-presets", json=payload)
            
            # 刷新缓存
            await self._get_presets(force_refresh=True)
            
            yield event.plain_result(f"✅ {result.get('message', '预设添加成功')}")
            
        except Exception as e:
            logger.error(f"添加预设失败: {e}")
            yield event.plain_result(f"❌ 添加预设失败: {e}")

    @filter.command("删除预设", aliases={"移除预设", "delpreset"}, prefix_optional=True)
    async def on_delete_preset(self, event: AstrMessageEvent):
        """删除自定义预设
        用法：删除预设 <名称>
        """
        raw = event.message_str.strip()
        parts = raw.split(maxsplit=1)
        
        if len(parts) < 2:
            yield event.plain_result("用法：删除预设 <名称>")
            return
        
        preset_name = parts[1].strip()
        
        try:
            encoded_name = quote(preset_name)
            await self._api_request("DELETE", f"/api/user-presets/{encoded_name}")
            
            # 刷新缓存
            await self._get_presets(force_refresh=True)
            
            yield event.plain_result(f"✅ 预设 '{preset_name}' 已删除")
            
        except Exception as e:
            logger.error(f"删除预设失败: {e}")
            yield event.plain_result(f"❌ 删除预设失败: {e}")

    @filter.command("我的预设", aliases={"自定义预设", "mypresets"}, prefix_optional=True)
    async def on_my_presets(self, event: AstrMessageEvent):
        """查看所有自定义预设"""
        presets = await self._get_presets()
        if not presets or "user_presets" not in presets:
            yield event.plain_result("❌ 获取自定义预设失败")
            return
        
        user_presets = presets["user_presets"]
        if not user_presets:
            yield event.plain_result("📝 暂无自定义预设")
            return
        
        msg = f"⭐ 自定义预设列表 (共{len(user_presets)}个)\n\n"
        for i, (name, info) in enumerate(user_presets.items(), 1):
            content = info.get("content", "")
            content_preview = content[:50] + "..." if len(content) > 50 else content
            added_by = info.get("added_by", "未知")
            msg += f"{i}. {name}\n"
            msg += f"   内容: {content_preview}\n"
            msg += f"   创建者: {added_by}\n\n"
        
        yield event.plain_result(msg)
