import lark_oapi as lark
from lark_oapi.api.im.v1 import *
import json
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from apscheduler.schedulers.background import BackgroundScheduler
import uuid
from manager.chat_manager import ChatManager, ChatConfig
NEW_PAPER_CARD_VERSION = "1.0.4"
class ArxivBot:
    def get_help_text(self):
        return (
            "【ArxivBot 指令帮助】\n"
            "/help —— 显示所有指令说明\n"
            "/config optional:A or B, C or D required:keyword1,keyword2\n"
            "    设置群聊关键词过滤，optional为可选关键词组（逗号分组，组内or），required为必需关键词（逗号分隔）\n"
            "    例如，/config optional:agent or LLM, PPO or GRPO 含义是搜索标题或摘要中包含agent或者LLM 并且 包含llm或GRPO的论文"
            "/daily_arxiv [起始日期],[结束日期]\n"
            "    查询指定日期区间（最多31天）内的论文，如：/daily_arxiv 2025-08-01,2025-08-10\n"
            "    只写一个日期则查该日到今天，如：/daily_arxiv 2025-08-01\n"
            "    不加参数则查今天的论文，日期可能有差异，不保真\n"
            "卡片内可点击“下一篇”按钮浏览更多论文。"
        )
    def __init__(self, app_id: str, app_secret: str):
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self.chat_manager = ChatManager()
        self.open_id_list = ['ou_a3e2ab794639d3cb462ec3846902457f']
        
        # 注册事件处理器
        self.event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.do_p2_im_message_receive_v1)
            .register_p2_card_action_trigger(self.do_p2_card_action_trigger)
            .build()
        )
        
        self.wsClient = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=self.event_handler,
            log_level=lark.LogLevel.DEBUG,
        )
    
    def find_instruction(self, content:str):
        instr_pos = content.find('/')
        if instr_pos == -1:
            return None
        return content[instr_pos:].strip()

    def do_p2_im_message_receive_v1(self, data: P2ImMessageReceiveV1) -> None:
        chat_id = data.event.message.chat_id
        res_content = ""
        if data.event.message.message_type == "text":
            message_content = json.loads(data.event.message.content)["text"].strip()
            instruction_content = self.find_instruction(message_content)
            if instruction_content is None:
                return
            if instruction_content.startswith("/help"):
                help_text = self.get_help_text()
                self.send_text_message("chat_id", chat_id, json.dumps({"text": help_text}))
                return
            if instruction_content.startswith("/config"):
                self.handle_config_command(chat_id, instruction_content)
                return
            elif instruction_content.startswith("/daily_arxiv"):
                import asyncio
                import threading
                from datetime import datetime
                date_from = None
                date_until = None
                parts = instruction_content.split()
                if len(parts) > 1:
                    date_args = parts[1].split(",")
                    now_str = datetime.now().strftime("%Y-%m-%d")
                    if len(date_args) >= 1:
                        date_from = date_args[0]
                    if len(date_args) >= 2:
                        date_until = date_args[1]
                        try:
                            if date_until > now_str:
                                date_until = now_str
                        except:
                            date_until = now_str
                    else:
                        date_until = now_str
                def run_async_task():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(self._handle_daily_arxiv(chat_id, data.event.message.chat_type, data.event.message.message_id, date_from, date_until))
                    finally:
                        loop.close()
                thread = threading.Thread(target=run_async_task)
                thread.start()
        else:
            res_content = "解析消息失败，请发送文本消息"
            if data.event.message.chat_type == "p2p":
                self.send_text_message("chat_id", chat_id, res_content)

    async def _handle_daily_arxiv(self, chat_id: str, chat_type: str, message_id: str, date_from=None, date_until=None):
        """异步处理每日论文请求，支持日期范围"""
        try:
            from datetime import datetime
            if not date_from:
                date_until = date_from = datetime.now().strftime("%Y-%m-%d")
            papers = await self.chat_manager.update_papers_for_chat(chat_id, date_from=date_from, date_until=date_until)
            if not papers:
                null_msg = f"当前查询要求: {self.chat_manager.get_chat_config(chat_id)}\n 指定日期范围内没有满足查询条件的论文被挂到arxiv上哦~"
                null_msg = json.dumps({"text": null_msg})
                self.send_text_message("chat_id", chat_id, null_msg)
            else:
                # 生成卡片内容
                card_content = self.create_paper_card(chat_id, papers)
                self.send_card_message("chat_id", chat_id, card_content)
        except Exception as e:
            error_text = f"获取论文失败: {str(e)}"
            self.send_text_message("chat_id", chat_id, error_text)

    def update_group_ids(self):
        """
        更新机器人已经加入的群列表
        """
        try:
            request: ListChatRequest = ListChatRequest.builder() \
            .sort_type("ByCreateTimeAsc") \
            .page_size(100) \
            .build()

            # 发起请求 - 使用self.client而不是self.wsClient
            response: ListChatResponse = self.client.im.v1.chat.list(request)
            
            if not response.success():
                print(f"获取群聊列表失败: {response.code}, {response.msg}")
                return False
                
            chat_list = response.data.items if response.data else []
            old_count = len(self.chat_manager.chat_id_set)
            
            for chat in chat_list:
                if chat.chat_status == "normal":
                    self.chat_manager.chat_id_set.add(chat.chat_id)
            
            new_count = len(self.chat_manager.chat_id_set)
            print(f"群聊列表更新完成: 原有{old_count}个群聊，现有{new_count}个群聊")
            return True
            
        except Exception as e:
            print(f"更新群聊列表时出错: {e}")
            return False
    
    def get_group_ids(self):
        """获取已加入的群聊ID列表"""
        return list(self.chat_manager.chat_id_set)

    def handle_config_command(self, chat_id: str, command: str):
        """
        处理配置命令
        新语法示例：/config optional:A or B, C or D, E or F or G required:keyword1,keyword2
        """
        try:
            parts = command.split(" ", 1)
            if len(parts) < 2:
                return
                
            config_str = parts[1]
            required_keywords = []
            optional_keywords = []
            
            # 分别查找 required: 和 optional: 的位置
            required_pos = config_str.find('required:')
            optional_pos = config_str.find('optional:')
            
            # 处理 required: 部分（保持原有逻辑）
            if required_pos != -1:
                start = required_pos + len('required:')
                if optional_pos != -1 and optional_pos > required_pos:
                    end = optional_pos
                else:
                    end = len(config_str)
                
                required_str = config_str[start:end].strip()
                if required_str:
                    required_keywords = [kw.strip() for kw in required_str.split(',') if kw.strip()]
                else:
                    required_keywords = []
            
            # 处理 optional: 部分（新的二维数组语法）
            if optional_pos != -1:
                start = optional_pos + len('optional:')
                if required_pos != -1 and required_pos > optional_pos:
                    end = required_pos
                else:
                    end = len(config_str)
                
                optional_str = config_str[start:end].strip()
                if optional_str:
                    # 解析新语法：A or B, C or D, E or F or G
                    # 先按逗号分组
                    groups = [group.strip() for group in optional_str.split(',') if group.strip()]
                    optional_keywords = []
                    for group in groups:
                        # 每组内按 "or" 分割关键词
                        keywords_in_group = [kw.strip() for kw in group.split(' or ') if kw.strip()]
                        if keywords_in_group:
                            optional_keywords.append(keywords_in_group)
                else:
                    optional_keywords = []
            
            # 更新配置
            new_config = ChatConfig(
                chat_id=chat_id,
                required_keywords=required_keywords,
                optional_keywords=optional_keywords
            )
            self.chat_manager.add_chat_config(chat_id, new_config)
            
            # 发送确认消息
            confirm_text = f"已更新群聊配置:\n必需关键词: {required_keywords}\n可选关键词组: {optional_keywords}"
            self.send_text_message("chat_id", chat_id, json.dumps({"text": confirm_text}))
            
        except Exception as e:
            error_text = f"配置格式错误: {str(e)}"
            self.send_text_message("chat_id", chat_id, json.dumps({"text": error_text}))

    def do_p2_card_action_trigger(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        chat_id = data.event.context.open_chat_id or "default"
        action = data.event.action

        if action.value["action"] == "next_article":
            paper, index = self.chat_manager.get_next_paper(chat_id)
            
            if not paper:
                content = {
                    "toast": {
                        "type": "warning",
                        "content": "暂无可切换的文章",
                    }
                }
                return P2CardActionTriggerResponse(content)
            
            chat_papers = self.chat_manager.get_chat_papers(chat_id)
            content = {
                "toast": {
                    "type": "info",
                    "content": f"已切换到下一篇文章 (第{index+1}篇), 共{len(chat_papers.papers)}篇",
                },
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": "AAqzQKpE1cGWO",
                        "template_version_name": NEW_PAPER_CARD_VERSION,
                        "template_variable": {
                            "title": paper.title,
                            "author": paper.authors,
                            "date": paper.first_announced_date.strftime("%Y-%m-%d"),
                            "abstract": paper.abstract,
                            "translated_abstract": paper.abstract_translated if paper.abstract_translated else "暂无",
                            "link": paper.url.strip()
                        },
                    },
                },
            }
            return P2CardActionTriggerResponse(content)

    def create_paper_card(self, chat_id: str, papers: list) -> str:
        """创建论文卡片"""
        if not papers:
            return ""
            
        paper = papers[0]
        return json.dumps({
            "type": "template",
            "data": {
                "template_id": "AAqzQKpE1cGWO",
                "template_version_name": NEW_PAPER_CARD_VERSION,
                "template_variable": {
                    "title": paper.title,
                    "author": paper.authors,
                    "date": paper.first_announced_date.strftime("%Y-%m-%d"),
                    "abstract": paper.abstract,
                    "translated_abstract": paper.abstract_translated if paper.abstract_translated else "暂无",
                    "link": paper.url.strip()
                }
            }
        })

    def send_card_message(self, id_type:str, chat_id: str, card_content: str):
        """发送卡片消息"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(card_content)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise Exception(f"发送卡片消息失败: {response.code}, {response.msg}")

    def send_text_message(self, id_type:str, chat_id: str, text_content: str):
        """发送文本消息"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(text_content)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise Exception(f"发送文本消息失败: {response.code}, {response.msg}")

    def reply_text_message(self, message_id: str, text_content: str):
        """回复文本消息"""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(text_content)
                .msg_type("text")
                .build()
            )
            .build()
        )
        
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise Exception(f"回复消息失败: {response.code}, {response.msg}")

    def send_daily_papers(self):
        """定时发送每日论文"""
        import asyncio
        import threading
        from datetime import datetime
        
        # 记录任务开始时间
        start_time = datetime.now()
        print(f"[{start_time.strftime('%Y-%m-%d %H:%M:%S.%f')}] 定时任务开始执行")
        
        def run_async_task():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._send_daily_papers_async())
                end_time = datetime.now()
                duration = end_time - start_time
                print(f"[{end_time.strftime('%Y-%m-%d %H:%M:%S.%f')}] 定时任务执行完成，耗时: {duration}")
            except Exception as e:
                print(f"定时任务执行失败: {e}")
            finally:
                loop.close()
        
        # 在新线程中运行异步任务
        thread = threading.Thread(target=run_async_task)
        thread.start()
    
    async def _send_daily_papers_async(self):
        """异步发送每日论文"""
        print(f"开始执行每日论文发送任务...")
        
        # 先更新群聊列表
        try:
            print("开始更新群聊列表...")
            update_success = self.update_group_ids()
            if update_success:
                print(f"群聊列表更新成功，当前有 {len(self.chat_manager.chat_id_set)} 个群聊")
            else:
                print("群聊列表更新失败，但继续执行任务")
        except Exception as e:
            print(f"更新群聊列表失败: {e}")
            # 不return，继续执行其他任务
        
        # 向所有用户发送每日论文
        print(f"开始向 {len(self.open_id_list)} 个用户发送每日论文...")
        for i, open_id in enumerate(self.open_id_list, 1):
            try:
                print(f"处理用户 {i}/{len(self.open_id_list)}: {open_id[:8]}...")
                # 为每个用户使用默认配置
                papers = await self.chat_manager.update_papers_for_chat("default")
                
                if papers:
                    card_content = self.create_paper_card("default", papers)
                    self.send_card_to_user(open_id, card_content)
                    print(f"成功发送 {len(papers)} 篇论文给用户")
                else:
                    text_content = json.dumps({"text": "今日暂无满足要求的新文章"})
                    self.send_text_to_user(open_id, text_content)
                    print(f"发送空结果消息给用户")
            except Exception as e:
                print(f"发送每日论文给用户 {open_id[:8]} 失败: {e}")
                try:
                    text_content = json.dumps({"text": f"获取每日论文失败: {str(e)}"})
                    self.send_text_to_user(open_id, text_content)
                except Exception as inner_e:
                    print(f"发送错误消息给用户也失败了: {inner_e}")
        
        # 向所有群聊发送每日论文
        group_count = len(self.chat_manager.chat_id_set)
        print(f"开始向 {group_count} 个群聊发送每日论文...")
        
        for i, chat_id in enumerate(self.chat_manager.chat_id_set, 1):
            try:
                print(f"处理群聊 {i}/{group_count}: {chat_id[:8]}...")
                # 为每个群聊获取对应的配置
                papers = await self.chat_manager.update_papers_for_chat(chat_id)
                
                if papers:
                    card_content = self.create_paper_card(chat_id, papers)
                    self.send_card_message("chat_id", chat_id, card_content)
                    print(f"成功发送 {len(papers)} 篇论文到群聊: {chat_id[:8]}")
                else:
                    config = self.chat_manager.get_chat_config(chat_id)
                    null_msg = f"当前查询要求: 必需关键词{config.required_keywords}, 可选关键词组{config.optional_keywords}\n今天没有满足查询条件的论文被挂到arxiv上哦~"
                    self.send_text_message("chat_id", chat_id, json.dumps({"text": null_msg}))
                    print(f"发送空结果消息到群聊: {chat_id[:8]}")
            except Exception as e:
                print(f"发送每日论文到群聊 {chat_id[:8]} 失败: {e}")
                try:
                    error_text = f"获取每日论文失败: {str(e)}"
                    self.send_text_message("chat_id", chat_id, json.dumps({"text": error_text}))
                except Exception as inner_e:
                    print(f"发送错误消息到群聊 {chat_id[:8]} 也失败了: {inner_e}")
        
        print(f"每日论文发送任务完成！")

    def send_card_to_user(self, open_id: str, card_content: str):
        """发送卡片给用户"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("interactive")
                .content(card_content)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise Exception(f"发送用户卡片失败: {response.code}, {response.msg}")

    def send_text_to_user(self, open_id: str, text_content: str):
        """发送文本给用户"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(text_content)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise Exception(f"发送用户文本失败: {response.code}, {response.msg}")

    def start(self):
        """启动机器人"""
        self.wsClient.start()

import os
from dotenv import load_dotenv
load_dotenv()
# 读取环境变量
APP_ID = os.environ.get("APP_ID")
APP_SECRET = os.environ.get("APP_SECRET")
def main():
    bot = ArxivBot(APP_ID, APP_SECRET)

    # 设置定时任务，添加更宽松的错过策略
    scheduler = BackgroundScheduler(
        timezone='Asia/Shanghai', 
        daemon=True,
        job_defaults={
            'coalesce': False,  # 不合并错过的任务
            'max_instances': 1,  # 最多只允许一个实例运行
            'misfire_grace_time': 100  # 允许100秒的延迟容忍度
        }
    )
    job = scheduler.add_job(bot.send_daily_papers, 'cron', hour=20, minute=30)
    scheduler.start()
    print(f"定时任务已添加: {job}")
    print(f"调度器状态: {scheduler.state}")
    print(f"所有任务: {scheduler.get_jobs()}")
    bot.start() # 是阻塞事件

if __name__ == "__main__":
    main()