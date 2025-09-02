from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime
import json
from arxiv_crawler.arxiv_crawler import get_daily_llm_papers

@dataclass
class ChatConfig:
    """群聊配置"""
    chat_id: str
    translate: bool = True
    
    # 使用新的关键词系统
    required_keywords: List[str] = None
    optional_keywords: List[str] = None

@dataclass
class ChatPapers:
    """群聊论文数据"""
    papers: List = None
    last_update: datetime = None
    current_index: int = 0
    
    def __post_init__(self):
        if self.papers is None:
            self.papers = []

class ChatManager:
    """群聊管理器"""
    
    def __init__(self):
        self.chat_config: Dict[str, ChatConfig] = {}
        self.chat_papers: Dict[str, ChatPapers] = {}
        self.load_chat_configs()
        self.chat_id_set = set()
        
    def load_chat_configs(self):
        """加载群聊配置（可以从文件或数据库加载）"""
        # 默认配置示例（使用二维数组格式）
        default_configs = {
            "default": ChatConfig(
                chat_id="default",
                optional_keywords=[
                    ["research",  "browse"],
                ],
                required_keywords=['agent']
            ),
        }
        
        for config_id, config in default_configs.items():
            self.chat_config[config_id] = config
            self.chat_papers[config_id] = ChatPapers()
    
    def get_chat_config(self, chat_id: str) -> ChatConfig:
        """获取群聊配置，如果不存在则使用默认配置"""
        return self.chat_config.get(chat_id, self.chat_config.get("default"))
    
    def get_chat_papers(self, chat_id: str) -> ChatPapers:
        """获取群聊论文数据"""
        if chat_id not in self.chat_papers:
            self.chat_papers[chat_id] = ChatPapers()
        return self.chat_papers[chat_id]
    
    async def update_papers_for_chat(self, chat_id: str, date_from: str = None, date_until: str = None) -> List:
        """更新指定群聊的论文数据，支持区间查询"""
        config = self.get_chat_config(chat_id)
        print(f'\nconfig如下: {config}\n')
        papers = await get_daily_llm_papers(
            date_from=date_from,
            date_until=date_until,
            translate=config.translate,
            required_keywords=config.required_keywords,
            optional_keywords=config.optional_keywords
        )
        chat_papers = self.get_chat_papers(chat_id)
        chat_papers.papers = papers
        chat_papers.last_update = datetime.now()
        chat_papers.current_index = 0
        return papers
    
    def get_current_paper(self, chat_id: str, index: int = 0):
        """获取当前显示的论文"""
        chat_papers = self.get_chat_papers(chat_id)
        
        if not chat_papers.papers:
            return None
            
        if 0 <= index < len(chat_papers.papers):
            return chat_papers.papers[index]
        return None
    
    def get_next_paper(self, chat_id: str):
        """获取下一篇论文"""
        chat_papers = self.get_chat_papers(chat_id)
        
        if not chat_papers.papers:
            return None, -1
            
        # 移动到下一篇，如果到了最后一篇则回到第一篇
        chat_papers.current_index = (chat_papers.current_index + 1) % len(chat_papers.papers)
        return chat_papers.papers[chat_papers.current_index], chat_papers.current_index
    
    def get_random_paper(self, chat_id: str):
        """获取随机论文"""
        import random
        chat_papers = self.get_chat_papers(chat_id)
        
        if not chat_papers.papers:
            return None, -1
            
        random_index = random.randint(0, len(chat_papers.papers) - 1)
        chat_papers.current_index = random_index
        return chat_papers.papers[random_index], random_index
    
    def search_papers_by_keywords(self, chat_id: str, required_keywords: list[str] = None, 
                                  optional_keywords: list[str] = None, limit: int = 10) -> list:
        """根据关键词搜索论文"""
        from arxiv_crawler.paper import PaperDatabase
        
        config = self.get_chat_config(chat_id)
        db = PaperDatabase()
        
        papers = db.search_papers_by_keywords(
            required_keywords=required_keywords or [],
            optional_keywords=optional_keywords or config.optional_keywords,
            limit=limit
        )
        
        return papers
    
    def search_papers_by_text(self, chat_id: str, search_text: str, limit: int = 10) -> list:
        """根据文本搜索论文"""
        from arxiv_crawler.paper import PaperDatabase
        
        db = PaperDatabase()
        
        papers = db.search_papers_by_text(
            search_text=search_text,
            limit=limit
        )
        
        return papers
    
    def add_chat_config(self, chat_id: str, config: ChatConfig):
        """添加群聊配置"""
        self.chat_config[chat_id] = config
        if chat_id not in self.chat_papers:
            self.chat_papers[chat_id] = ChatPapers()
    
    def save_chat_configs(self):
        """保存群聊配置到文件"""
        # 可以实现保存到JSON文件或数据库
        pass