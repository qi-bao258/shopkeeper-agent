"""
Embedding 客户端管理器

负责按配置初始化 Embedding 服务客户端，并为字段、指标和用户问题的向量化
提供统一访问入口
"""

import asyncio
from typing import Optional

from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.conf.app_config import EmbeddingConfig, app_config


class EmbeddingClientManager:
    """管理 Embedding 服务客户端的初始化与复用"""

    def __init__(self, config: EmbeddingConfig):
        self.client: Optional[HuggingFaceEndpointEmbeddings] = None
        self.config = config

    def _get_url(self) -> str:
        """拼接 Embedding 服务地址"""
        return f"http://{self.config.host}:{self.config.port}"

    def init(self):
        """显式初始化客户端，避免模块导入时立即建立外部连接"""
        self.client = HuggingFaceEndpointEmbeddings(model=self._get_url())

    async def close(self):
        """
        释放 Embedding 客户端持有的底层连接池。

        HuggingFaceEndpointEmbeddings 内部使用 httpx.AsyncClient，
        不显式关闭会在每次进程重启时留下未回收的连接资源。
        """
        client = self.client
        self.client = None
        if client is None:
            return

        # 不同版本 langchain-huggingface 暴露的内部属性名不完全一致，做兼容处理
        inner = getattr(client, "client", None) or getattr(client, "async_client", None)
        aclose = getattr(inner, "aclose", None)
        if callable(aclose):
            await aclose()

    async def probe(self) -> bool:
        """
        探活：对固定短文本做一次最小向量化，确认推理服务可用。

        比起只判断 client 是否为 None，真实发一次请求才能发现「服务已启动但模型
        未加载完成」这类就绪态问题——这正是 readiness 与 liveness 的区别所在。
        """
        if self.client is None:
            return False
        try:
            await self.client.aembed_query("ping")
            return True
        except Exception:  # noqa: BLE001 —— 探针只关心可用性
            return False


# 模块级单例，供整个项目复用同一套 Embedding 客户端管理器
embedding_client_manager = EmbeddingClientManager(app_config.embedding)


if __name__ == "__main__":
    embedding_client_manager.init()
    client = embedding_client_manager.client

    async def test():
        """执行一次最小化向量化调用，验证服务是否可用"""
        text = "What is deep learning?"
        query_result = await client.aembed_query(text)
        print(query_result[:3])

    asyncio.run(test())
