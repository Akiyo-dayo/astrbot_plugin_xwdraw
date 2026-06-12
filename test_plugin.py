import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

from aiohttp import web


def install_astrbot_stubs():
    class StubLogger:
        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

        def debug(self, *_args, **_kwargs):
            pass

        def exception(self, *_args, **_kwargs):
            pass

    class StubFilter:
        def command(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    class MessageChain:
        def __init__(self):
            self.items = []

        def file_image(self, path):
            self.items.append(("image", path))
            return self

        def file(self, path):
            self.items.append(("file", path))
            return self

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir():
            return str(Path(tempfile.gettempdir()) / "xwdraw_test_data")

    def register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    class Plain:
        def __init__(self, text):
            self.text = text

        def __repr__(self):
            return f"Plain({self.text!r})"

    class Image:
        def __init__(self, data=None, file=None, url=None):
            self.data = data
            self.file = file
            self.url = url

        @classmethod
        def fromBytes(cls, data):
            return cls(data=data)

        @classmethod
        def fromFileSystem(cls, path):
            return cls(file=path)

    class Reply:
        def __init__(self, chain=None):
            self.chain = chain or []

    class Node:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Nodes:
        def __init__(self, nodes):
            self.nodes = nodes

    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.components": types.ModuleType("astrbot.core.message.components"),
        "astrbot.core.platform": types.ModuleType("astrbot.core.platform"),
        "astrbot.core.platform.astr_message_event": types.ModuleType("astrbot.core.platform.astr_message_event"),
    }
    modules["astrbot"].logger = StubLogger()
    modules["astrbot.api.event"].filter = StubFilter()
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].StarTools = StarTools
    modules["astrbot.api.star"].register = register
    modules["astrbot.core.message.components"].Image = Image
    modules["astrbot.core.message.components"].Plain = Plain
    modules["astrbot.core.message.components"].Reply = Reply
    modules["astrbot.core.message.components"].Node = Node
    modules["astrbot.core.message.components"].Nodes = Nodes
    modules["astrbot.core.platform.astr_message_event"].AstrMessageEvent = object
    sys.modules.update(modules)


install_astrbot_stubs()
sys.path.insert(0, str(Path(__file__).parent))
main = importlib.import_module("main")


class MockContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, origin, message_chain):
        self.sent.append((origin, message_chain))


class MockEvent:
    def __init__(self, message_str, message=None, session="test-session", sender_id="10001", role=None, is_admin=False):
        self.message_str = message_str
        self.message_obj = types.SimpleNamespace(message=message or [])
        self.message_obj.sender = types.SimpleNamespace(user_id=sender_id, role=role)
        self.unified_msg_origin = session
        self._sender_id = sender_id
        self._role = role
        self._is_admin = is_admin

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def get_self_id(self):
        return "10000"

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self.unified_msg_origin

    def is_admin(self):
        return self._is_admin


async def collect_asyncgen(asyncgen):
    results = []
    async for item in asyncgen:
        results.append(item)
    return results


class FakeClient:
    def __init__(self, asset=None):
        self.api_key = "token"
        self.asset = asset
        self.generate_payload = None
        self.video_payload = None
        self.updated_tags = None

    async def generate(self, payload):
        self.generate_payload = payload
        return self.asset

    async def generation_config(self):
        return {"models": 3, "default_steps": 28}

    async def queue_status(self):
        return {"queue_count": 2}

    async def video_generate(self, image_bytes, prompt, negative_prompt="", duration="4", fps="16"):
        self.video_payload = {
            "image_bytes": image_bytes,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "duration": duration,
            "fps": fps,
        }
        return {"status": "queued", "timestamp": "20260611220000", "username": "tester"}

    async def update_image_tags(self, date_folder, filename, is_r18, is_r18g):
        self.updated_tags = {
            "date_folder": date_folder,
            "filename": filename,
            "is_r18": is_r18,
            "is_r18g": is_r18g,
        }
        return {"ok": True}


class XWDrawUnitTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, config=None):
        base_config = {
            "api_url": "https://sd.loping151.com/api/generate",
            "api_key": "token",
            "timeout": 5,
            "r18_review_enabled": True,
            "r18_block_r18": True,
            "r18_block_r18g": True,
            "r18_nsfw_score_threshold": 0.65,
            "external_review_enabled": False,
            "plugin_enabled": True,
            "group_admin_can_toggle": True,
            "switch_admin_user_ids": "",
        }
        base_config.update(config or {})
        plugin = main.XWDrawPlugin(MockContext(), base_config)
        plugin.switch_state_path = Path(tempfile.mkdtemp()) / "switches.json"
        plugin.switch_state = {"session_overrides": {}}
        return plugin

    def test_url_helpers_encode_and_split_image_refs(self):
        client = main.XWDrawApiClient("https://sd.loping151.com/api/generate", "token", 5)
        url = client.image_url("20231215/测试 图片.png")
        self.assertEqual(client.base_url, "https://sd.loping151.com")
        self.assertIn("/api/image/20231215/", url)
        self.assertIn("%E6%B5%8B%E8%AF%95%20%E5%9B%BE%E7%89%87.png", url)
        self.assertEqual(client.split_image_ref(url), ("20231215", "测试 图片.png"))

    def test_parse_generate_args_keeps_server_side_advanced_flags(self):
        args = main.XWDrawPlugin.parse_generate_args("1girl --r18g -d 0.8 -s 30 -w 1024")
        self.assertEqual(args.prompt, "1girl -s 30 -w 1024")
        self.assertTrue(args.is_r18)
        self.assertTrue(args.is_r18g)
        self.assertEqual(args.denoising_strength, 0.8)

    async def test_service_review_blocks_r18_metadata(self):
        plugin = self.make_plugin()
        asset = main.GeneratedAsset(image_bytes=b"png", metadata={"is_r18": True, "nsfw_score": 0.2})
        decision = await plugin._review_generated_asset(asset, MockEvent("来点 test"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, "r18")

    async def test_service_review_blocks_threshold(self):
        plugin = self.make_plugin()
        asset = main.GeneratedAsset(image_bytes=b"png", metadata={"nsfw_score": 0.9})
        decision = await plugin._review_generated_asset(asset, MockEvent("来点 test"))
        self.assertFalse(decision.allowed)
        self.assertIn("nsfw_score", decision.reason)

    async def test_generate_command_blocks_after_generation(self):
        plugin = self.make_plugin()
        plugin.client = FakeClient(main.GeneratedAsset(image_bytes=b"png", metadata={"is_r18": True}, elapsed=1.2))
        results = await collect_asyncgen(plugin.on_generate(MockEvent("来点 test prompt")))
        self.assertEqual(results[0][0], "plain")
        self.assertIn("正在文生图生成", results[0][1])
        self.assertEqual(results[-1][0], "plain")
        self.assertIn("已停止发送图片", results[-1][1])

    async def test_generate_command_sends_safe_image(self):
        plugin = self.make_plugin()
        plugin.client = FakeClient(main.GeneratedAsset(image_bytes=b"png", metadata={"nsfw_score": 0.01}, elapsed=0.5))
        results = await collect_asyncgen(plugin.on_generate(MockEvent("来点 safe prompt -d 0.4")))
        self.assertEqual(results[-1][0], "chain")
        self.assertEqual(plugin.client.generate_payload["is_r18"], False)
        self.assertNotIn("denoising_strength", plugin.client.generate_payload)

    async def test_generate_command_blocks_explicit_r18_flag_when_metadata_missing(self):
        plugin = self.make_plugin()
        plugin.client = FakeClient(main.GeneratedAsset(image_bytes=b"png", metadata={}, elapsed=0.5))
        results = await collect_asyncgen(plugin.on_generate(MockEvent("来点 adult prompt --r18")))
        self.assertEqual(plugin.client.generate_payload["is_r18"], True)
        self.assertEqual(results[-1][0], "plain")
        self.assertIn("已停止发送图片", results[-1][1])

    async def test_generation_config_command(self):
        plugin = self.make_plugin()
        plugin.client = FakeClient()
        results = await collect_asyncgen(plugin.on_generation_config(MockEvent("生成配置")))
        self.assertIn("models", results[0][1])

    async def test_video_generate_command_uses_attached_image(self):
        plugin = self.make_plugin()
        plugin.client = FakeClient()

        async def fake_image(_event, _timeout):
            return b"source"

        plugin._get_image_from_event = fake_image
        results = await collect_asyncgen(plugin.on_video_generate(MockEvent("视频生成 spin camera -t 6 -fps 20 -n blurry")))
        self.assertIn("视频任务已提交", results[-1][1])
        self.assertEqual(plugin.client.video_payload["duration"], "6")
        self.assertEqual(plugin.client.video_payload["fps"], "20")
        self.assertEqual(plugin.client.video_payload["negative_prompt"], "blurry")

    async def test_update_image_tags_r18g_flag_does_not_set_r18(self):
        plugin = self.make_plugin()
        plugin.client = FakeClient()
        results = await collect_asyncgen(plugin.on_update_image_tags(MockEvent("更新图片标签 20260611/test.png --r18g")))
        self.assertIn("图片标签已更新", results[0][1])
        self.assertEqual(plugin.client.updated_tags["date_folder"], "20260611")
        self.assertEqual(plugin.client.updated_tags["filename"], "test.png")
        self.assertIsNone(plugin.client.updated_tags["is_r18"])
        self.assertTrue(plugin.client.updated_tags["is_r18g"])

    async def test_group_admin_can_disable_and_block_generate(self):
        plugin = self.make_plugin()
        plugin.client = FakeClient(main.GeneratedAsset(image_bytes=b"png", metadata={"nsfw_score": 0.01}, elapsed=0.5))

        close_results = await collect_asyncgen(plugin.on_plugin_switch(MockEvent("绘图关闭", role="admin")))
        self.assertIn("已关闭", close_results[0][1])
        self.assertFalse(plugin._is_plugin_enabled_for_event(MockEvent("绘图状态")))

        generate_results = await collect_asyncgen(plugin.on_generate(MockEvent("来点 safe prompt")))
        self.assertIn("总开关已关闭", generate_results[0][1])
        self.assertIsNone(plugin.client.generate_payload)

    async def test_normal_member_cannot_toggle_switch(self):
        plugin = self.make_plugin()
        results = await collect_asyncgen(plugin.on_plugin_switch(MockEvent("绘图关闭", role="member")))
        self.assertIn("只有群管理员", results[0][1])
        self.assertTrue(plugin._is_plugin_enabled_for_event(MockEvent("绘图状态")))

    async def test_configured_switch_admin_can_toggle(self):
        plugin = self.make_plugin({"plugin_enabled": False, "switch_admin_user_ids": "42"})
        event = MockEvent("绘图开启", sender_id="42", role="member")
        results = await collect_asyncgen(plugin.on_plugin_switch(event))
        self.assertIn("已开启", results[0][1])
        self.assertTrue(plugin._is_plugin_enabled_for_event(event))


class XWDrawClientIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app.router.add_post("/api/generate", self.handle_generate)
        app.router.add_get("/api/image/{date}/{filename}", self.handle_image)
        app.router.add_get("/api/image-metadata/{date}/{filename}", self.handle_metadata)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sock = self.site._server.sockets[0]
        host, port = sock.getsockname()[:2]
        self.base_url = f"http://{host}:{port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def handle_generate(self, request):
        self.assertEqual(request.headers.get("Authorization"), "Bearer token")
        payload = await request.json()
        self.assertEqual(payload["prompt"], "safe")
        return web.json_response(
            {
                "filename": "20260611/test image.png",
                "final_prompt": "safe translated",
                "metadata": {"nsfw_score": 0.01},
            }
        )

    async def handle_image(self, request):
        self.assertEqual(request.match_info["date"], "20260611")
        self.assertEqual(request.match_info["filename"], "test image.png")
        return web.Response(body=b"fake-png", content_type="image/png")

    async def handle_metadata(self, request):
        return web.json_response({"is_r18": False, "nsfw_score": 0.02})

    async def test_generate_resolves_filename_download_and_metadata(self):
        client = main.XWDrawApiClient(f"{self.base_url}/api/generate", "token", 5)
        asset = await client.generate({"prompt": "safe", "is_r18": False, "is_r18g": False})
        self.assertEqual(asset.image_bytes, b"fake-png")
        self.assertEqual(asset.date_folder, "20260611")
        self.assertEqual(asset.filename, "test image.png")
        self.assertEqual(asset.final_prompt, "safe translated")
        self.assertEqual(asset.metadata["nsfw_score"], 0.02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
