import os
import logging
from typing import Optional

# 设置模块级logger
logger = logging.getLogger(__name__)

# 错误解释字典
ERROR_EXPLANATIONS = {
    # HTTP 致命错误
    "401": {
        "explanation": "API认证失败",
        "suggestion": "请检查您的API Key是否正确、有效，或是否已过期。"
    },
    "402": {
        "explanation": "API账户计费问题",
        "suggestion": "请登录您的API服务商账户，检查账户余额或绑定的支付方式是否正常。"
    },
    "403": {
        "explanation": "API访问被拒绝",
        "suggestion": "您的API Key或无权限访问所请求的模型或资源。"
    },
    "404": {
        "explanation": "API资源未找到",
        "suggestion": "请检查您的API Base URL是否正确，以及所请求的模型名称是否存在。"
    },
    # HTTP 瞬时错误
    "429": {
        "explanation": "API请求过于频繁",
        "suggestion": "程序正在自动等待并重试，请耐心等候。如果频繁出现，可以尝试在config.ini中降低'max_workers'的值，或启用API速率限制（设置TPM/RPM）。"
    },
    "500": {
        "explanation": "API服务器内部错误",
        "suggestion": "这是API服务商一侧的临时问题。程序将自动重试。"
    },
    "502": {
        "explanation": "API服务器网关错误",
        "suggestion": "这是API服务商一侧的网络问题。程序将自动重试。"
    },
    "503": {
        "explanation": "API服务暂时不可用",
        "suggestion": "API服务商或正在进行维护或遇到高负载。程序将自动重试。"
    },
    "504": {
        "explanation": "API服务器网关超时",
        "suggestion": "API服务商一侧处理请求超时。程序将自动重试。"
    },
    # 网络错误
    "connectionerror": {
        "explanation": "网络连接失败",
        "suggestion": "无法连接到API服务器。请检查您的网络连接、代理设置以及API Base URL是否正确。"
    },
    "timeout": {
        "explanation": "网络连接超时",
        "suggestion": "向API服务器发送请求后等待响应超时。或因网络延迟过高，或API服务器当前负载较重。"
    },
    # 文件系统错误
    "filenotfound": {
        "explanation": "文件未找到",
        "suggestion": "程序无法在指定路径找到所需的文件（如Zotero报告或PDF文件）。请检查config.ini中的路径配置是否正确，以及文件是否存在。"
    },
    # PDF处理错误
    "pdfpassword": {
        "explanation": "PDF文件已加密",
        "suggestion": "该PDF文件需要密码才能读取。请使用未加密的PDF文件，或提供密码。"
    },
    "pdfdamaged": {
        "explanation": "PDF文件已损坏",
        "suggestion": "该PDF文件或已损坏或格式不正确。请尝试使用另一个版本的PDF文件。"
    },
    # 通用错误
    "unknown": {
        "explanation": "未知错误",
        "suggestion": "程序遇到了一个未预期的错误。请检查网络连接和配置文件，或联系技术支持。"
    }
}


def get_error_explanation(error_keyword: str) -> str:
    """
    根据错误关键字获取用户友好的错误解释与建议
    
    Args:
        error_keyword: 错误关键字（如'401'或'connectionerror'）
        
    Returns:
        str: 格式化的错误解释和建议
    """
    error_info = ERROR_EXPLANATIONS.get(error_keyword.lower(), ERROR_EXPLANATIONS["unknown"])
    
    explanation = error_info["explanation"]
    suggestion = error_info["suggestion"]
    
    return f"\n[错误解释] {explanation}\n[解决方案] {suggestion}\n"


def sanitize_path_component(path_component: str) -> str:
    """
    清理路径组件，移除或替换Windows文件系统中的非法字符
    
    Args:
        path_component: 路径组件字符串
        
    Returns:
        str: 清理后的路径组件
    """
    if not path_component:
        return "unknown"
    
    # 移除引号和特殊字符
    cleaned = path_component.strip('\'"')
    
    # Windows文件系统非法字符
    illegal_chars = '<>:"/\\|?*'
    for char in illegal_chars:
        cleaned = cleaned.replace(char, '_')
    
    # 移除控制字符
    cleaned = ''.join(char for char in cleaned if ord(char) >= 32)
    
    # 确保不以点或空格开头或结尾
    cleaned = cleaned.strip('. ')
    
    # 如果结果为空，返回默认值
    if not cleaned:
        return "unknown"
    
    return cleaned


def ensure_dir(path: str) -> Optional[str]:
    """
    确保一个目录存在，如果不存在则创建它
    
    Args:
        path: 目录路径
        
    Returns:
        Optional[str]: 成功时返回目录路径，失败时返回 None
        
    Raises:
        OSError: 当创建目录失败时（权限不足、路径无效等）
    """
    if not path:
        raise ValueError("目录路径不能为空")
    
    try:
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        return path
    except OSError as e:
        raise OSError(f"创建目录 '{path}' 失败: {e}")


if __name__ == "__main__":
    # 测试函数
    test_dir = "test_output"
    try:
        result = ensure_dir(test_dir)
        logger.info(f"目录创建成功: {result}")

        # 清理测试目录
        import shutil
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)
            logger.info(f"测试目录已清理: {test_dir}")
    except Exception as e:
        logger.error(f"目录创建失败: {e}")