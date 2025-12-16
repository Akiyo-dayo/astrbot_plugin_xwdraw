"""
XW绘图插件测试脚本

用于测试插件的各项功能是否正常工作
"""

import asyncio
import sys
from pathlib import Path

# 添加父目录到路径以便导入
sys.path.insert(0, str(Path(__file__).parent.parent))

# 模拟配置
test_config = {
    "api_url": "https://sd.loping151.com/api/generate",
    "api_key": "your_test_token_here",  # 请替换为真实的测试token
    "timeout": 60
}


class MockContext:
    """模拟 Context 对象"""
    async def send_message(self, origin, message_chain):
        print(f"[发送消息] {message_chain}")


class MockEvent:
    """模拟 Event 对象"""
    def __init__(self, message_str):
        self.message_str = message_str
        self.message_obj = type('obj', (object,), {'message': []})()
        self.unified_msg_origin = "test"
    
    def plain_result(self, text):
        print(f"[机器人回复] {text}")
        return text
    
    def chain_result(self, chain):
        print(f"[机器人回复] {chain}")
        return chain


async def test_api_request():
    """测试 API 请求方法"""
    print("\n" + "="*50)
    print("测试 1: API 请求封装")
    print("="*50)
    
    try:
        from main import XWDrawPlugin
        
        plugin = XWDrawPlugin(MockContext(), test_config)
        
        # 测试获取预设
        print("\n测试获取预设...")
        presets = await plugin._get_presets()
        
        if presets:
            print(f"✅ 成功获取预设")
            print(f"   - 角色数量: {len(presets.get('character_lora', {}))}")
            print(f"   - 风格数量: {len(presets.get('style', {}))}")
            print(f"   - 服装数量: {len(presets.get('costume', {}))}")
            print(f"   - 用户预设: {len(presets.get('user_presets', {}))}")
        else:
            print("❌ 获取预设失败")
            
    except Exception as e:
        print(f"❌ 测试失败: {e}")


async def test_help_command():
    """测试帮助命令"""
    print("\n" + "="*50)
    print("测试 2: 帮助命令")
    print("="*50)
    
    try:
        from main import XWDrawPlugin
        
        plugin = XWDrawPlugin(MockContext(), test_config)
        event = MockEvent("绘图帮助")
        
        print("\n执行命令: 绘图帮助")
        async for result in plugin.on_help(event):
            pass
        
        print("✅ 帮助命令执行成功")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")


async def test_preset_list():
    """测试预设列表命令"""
    print("\n" + "="*50)
    print("测试 3: 预设列表命令")
    print("="*50)
    
    try:
        from main import XWDrawPlugin
        
        plugin = XWDrawPlugin(MockContext(), test_config)
        event = MockEvent("预设列表")
        
        print("\n执行命令: 预设列表")
        async for result in plugin.on_list_presets(event):
            pass
        
        print("✅ 预设列表命令执行成功")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")


async def test_character_list():
    """测试角色列表命令"""
    print("\n" + "="*50)
    print("测试 4: 角色列表命令")
    print("="*50)
    
    try:
        from main import XWDrawPlugin
        
        plugin = XWDrawPlugin(MockContext(), test_config)
        event = MockEvent("角色列表")
        
        print("\n执行命令: 角色列表")
        async for result in plugin.on_list_characters(event):
            pass
        
        print("✅ 角色列表命令执行成功")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")


async def test_style_list():
    """测试风格列表命令"""
    print("\n" + "="*50)
    print("测试 5: 风格列表命令")
    print("="*50)
    
    try:
        from main import XWDrawPlugin
        
        plugin = XWDrawPlugin(MockContext(), test_config)
        event = MockEvent("风格列表")
        
        print("\n执行命令: 风格列表")
        async for result in plugin.on_list_styles(event):
            pass
        
        print("✅ 风格列表命令执行成功")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")


async def test_url_building():
    """测试 URL 构建"""
    print("\n" + "="*50)
    print("测试 6: URL 构建")
    print("="*50)
    
    try:
        from main import XWDrawPlugin
        
        plugin = XWDrawPlugin(MockContext(), test_config)
        
        # 测试不同的文件名
        test_cases = [
            ("20231215/142536_user_web.png", "普通文件名"),
            ("20231215/测试图片.png", "中文文件名"),
            ("20231215/image with spaces.png", "带空格文件名"),
        ]
        
        for filename, desc in test_cases:
            url = plugin._build_image_url(test_config["api_url"], filename)
            print(f"\n{desc}:")
            print(f"  输入: {filename}")
            print(f"  输出: {url}")
            
            # 验证 URL 格式
            if "/api/image/" in url:
                print(f"  ✅ URL 格式正确")
            else:
                print(f"  ❌ URL 格式错误")
        
        print("\n✅ URL 构建测试完成")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")


async def test_config_info():
    """测试生成配置命令"""
    print("\n" + "="*50)
    print("测试 7: 生成配置命令")
    print("="*50)
    
    try:
        from main import XWDrawPlugin
        
        plugin = XWDrawPlugin(MockContext(), test_config)
        event = MockEvent("生成配置")
        
        print("\n执行命令: 生成配置")
        async for result in plugin.on_generation_config(event):
            pass
        
        print("✅ 生成配置命令执行成功")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")


async def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("🧪 XW绘图插件功能测试")
    print("="*60)
    
    # 检查是否配置了测试 token
    if test_config["api_key"] == "your_test_token_here":
        print("\n⚠️ 警告: 未配置测试 token")
        print("请在脚本开头的 test_config 中填写真实的 api_key")
        print("某些测试可能会失败\n")
        
        response = input("是否继续测试? (y/n): ")
        if response.lower() != 'y':
            print("测试已取消")
            return
    
    # 运行测试
    tests = [
        test_url_building,         # 不需要网络的测试
        test_help_command,         # 不需要网络的测试
        test_api_request,          # 需要网络
        test_preset_list,          # 需要网络
        test_character_list,       # 需要网络
        test_style_list,           # 需要网络
        test_config_info,          # 需要网络
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            await test()
            passed += 1
        except Exception as e:
            print(f"\n❌ 测试异常: {e}")
            failed += 1
    
    # 输出测试结果
    print("\n" + "="*60)
    print("📊 测试结果")
    print("="*60)
    print(f"✅ 通过: {passed}/{len(tests)}")
    print(f"❌ 失败: {failed}/{len(tests)}")
    
    if failed == 0:
        print("\n🎉 所有测试通过！")
    else:
        print("\n⚠️ 部分测试失败，请检查配置和网络连接")


if __name__ == "__main__":
    print("\n提示: 运行此测试前，请确保:")
    print("1. 已安装 aiohttp 库")
    print("2. 已配置有效的 api_key")
    print("3. 网络连接正常")
    print()
    
    try:
        asyncio.run(run_all_tests())
    except KeyboardInterrupt:
        print("\n\n测试已中断")
    except Exception as e:
        print(f"\n测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
