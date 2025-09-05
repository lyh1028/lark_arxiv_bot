import asyncio
import re
from datetime import datetime, timedelta, UTC
from itertools import chain
import os
import sys
# 添加当前目录到sys.path，确保能找到同目录下的模块
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
import aiohttp
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup, NavigableString, Tag
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn
from arxiv_time import next_arxiv_update_day
from paper import Paper, PaperDatabase, PaperExporter
import xml.etree.ElementTree as ET
import urllib.parse

# 全局配置
PREV_DAY = 4  # 检查过去时间的范围（天数）

class ArxivScraper(object):
    def __init__(
        self,
        date_from,
        date_until,
        category_blacklist=[],
        category_whitelist=["cs.CV", "cs.AI", "cs.LG", "cs.CL", "cs.IR", "cs.MA"],
        optional_keywords=None,  # 可选关键词（二维数组，外层AND，内层OR）
        required_keywords=None,  # 必需关键词（AND连接）
        trans_to="zh-CN",
        proxy=None,
        db_path='/Users/bytedance/code/lark-samples-main/echo_bot/python/arxiv_crawler/papers.db',
    ):
        """
        一个抓取指定日期范围内的arxiv文章的类,
        搜索基于https://arxiv.org/search/advanced,
        一个文件被爬取到的条件是：首次提交时间在`date_from`和`date_until`之间，并且包含至少一个关键词。
        一个文章被详细展示（不被过滤）的条件是：至少有一个领域在白名单中，并且没有任何一个领域在黑名单中。
        翻译基于google-translate

        Args:
            date_from (str): 开始日期(含当天)
            date_until (str): 结束日期(含当天)
            category_blacklist (list, optional): 黑名单. Defaults to [].
            category_whitelist (list, optional): 白名单. Defaults to ["cs.CV", "cs.AI", "cs.LG", "cs.CL", "cs.IR", "cs.MA"].
            optional_keywords (list, optional): 可选关键词（二维数组，外层AND，内层OR）. Defaults to [["agent", "research"], ["LLM", "language model"], ["GPT", "transformer"]].
            required_keywords (list, optional): 必需关键词（AND连接）. Defaults to [].
            trans_to: 翻译的目标语言, 若设为可转换为False的值则不会翻译
            proxy (str | None, optional): 用于翻译和爬取arxiv时要使用的代理, 通常是http://127.0.0.1:7890. Defaults to None
        """
        # 保存参数为实例变量
        self.date_from = date_from
        self.date_until = date_until
        self.db_path = db_path
        self.trans_to = trans_to
        self.proxy = proxy
        
        # announced_date_first 日期处理为年月，从from到until的所有月份都会被爬取
        # 如果from和until是同一个月，则until设置为下个月(from+31)
        self.search_from_date = datetime.strptime(date_from[:-3], "%Y-%m")
        self.search_until_date = datetime.strptime(date_until[:-3], "%Y-%m")
        if self.search_from_date.month == self.search_until_date.month:
            self.search_until_date = (self.search_from_date + timedelta(days=31)).replace(day=1)
        # 由于arxiv的奇怪机制，每个月的第一天公布的文章总会被视作上个月的文章, 所以需要将月初文章的首次公布日期往后推一天
        self.first_announced_date = next_arxiv_update_day(next_arxiv_update_day(self.search_from_date) + timedelta(days=1))

        self.category_blacklist = category_blacklist  # used as metadata
        self.category_whitelist = category_whitelist  # used as metadata

        # 处理关键词参数，优先使用新的optional_keywords和required_keywords方式
        if optional_keywords is not None or required_keywords is not None:
            self.optional_keywords = optional_keywords if optional_keywords is not None else []
            self.required_keywords = required_keywords if required_keywords is not None else []
        else:
            # 默认关键词
            self.optional_keywords = [["browse", "research"]]
            self.required_keywords = ["agent"]

        self.filt_date_by = "announced_date_first"  # url
        self.order = "-announced_date_first"  # url(结果默认按首次公布日期的降序排列，这样最新公布的会在前面)
        self.total = None  # fetch_all
        self.step = 50  # url, fetch_all
        self.papers: list[Paper] = []  # fetch_all

        self.paper_db = PaperDatabase(db_path=self.db_path)
        self.paper_exporter = PaperExporter(self.date_from, self.date_until, self.category_blacklist, self.category_whitelist, database_path=self.db_path)
        self.console = Console()

    def get_api_url(self, start=0, max_results=50):
        """
        使用arXiv API接口构建查询URL
        只在标题(ti:)和摘要(abs:)中搜索，required_keywords在标题或摘要中出现一处即可
        例子：http://export.arxiv.org/api/query?search_query=(ti:A+OR+abs:A)+AND+(ti:B+OR+abs:B)&sortBy=submittedDate&sortOrder=descending
        
        Args:
            start (int): 返回结果的起始序号
            max_results (int): 每次查询的最大结果数
        """
        # 构建搜索查询字符串
        query_parts = []
        
        # 处理可选关键词 (二维数组，外层AND，内层OR) - 在标题或摘要中搜索
        if self.optional_keywords:
            for keyword_group in self.optional_keywords:
                if keyword_group and isinstance(keyword_group, list):  # 确保组不为空
                    group_parts = []
                    for kw in keyword_group:
                        # 每个关键词在标题或摘要中出现即可
                        group_parts.append(f"(ti:{kw}+OR+abs:{kw})")
                    # 组内用OR连接
                    group_query = "(" + "+OR+".join(group_parts) + ")"
                    query_parts.append(group_query)
        
        # 处理必需关键词 (AND 关系) - 在标题或摘要中搜索
        for kw in self.required_keywords:
            # 每个必需关键词必须在标题或摘要中出现
            query_parts.append(f"(ti:{kw}+OR+abs:{kw})")
        
        # 用AND连接所有部分
        if query_parts:
            search_query = "+AND+".join(query_parts)
        else:
            search_query = "ti:*+OR+abs:*"  # 如果没有关键词，搜索所有标题和摘要
        
        # URL编码
        search_query = urllib.parse.quote(search_query, safe='+():')
        
        # 构建完整的API URL
        base_url = "http://export.arxiv.org/api/query"
        params = [
            f"search_query={search_query}",
            f"start={start}",
            f"max_results={max_results}",
            "sortBy=submittedDate",
            "sortOrder=descending"
        ]
        
        return f"{base_url}?" + "&".join(params)

    async def request_api(self, start=0, max_results=50):
        """
        异步请求arXiv API，重试至多3次
        """
        error = 0
        url = self.get_api_url(start, max_results)
        while error <= 3:
            try:
                timeout = ClientTimeout(total=30)
                async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
                    async with session.get(url, proxy=self.proxy) as response:
                        response.raise_for_status()
                        content = await response.text()
                        return content
            except Exception as e:
                error += 1
                self.console.log(f"[bold red]API Request {start} cause error: ")
                self.console.print_exception()
                self.console.log(f"[bold red]Retrying {start}... {error}/3")
        
        # 如果所有重试都失败了，返回None
        self.console.log(f"[bold red]All API retries failed for request {start}")
        return None

    def parse_api_xml(self, xml_content):
        """
        解析arXiv API返回的XML内容
        
        Args:
            xml_content (str): API返回的XML内容
            
        Returns:
            list[Paper]: 解析出的论文列表
        """
        if xml_content is None:
            self.console.log("[bold red]Cannot parse None XML content")
            return []
        
        try:
            root = ET.fromstring(xml_content)
            # 定义命名空间
            namespaces = {
                'atom': 'http://www.w3.org/2005/Atom',
                'arxiv': 'http://arxiv.org/schemas/atom'
            }
            
            papers = []
            entries = root.findall('atom:entry', namespaces)
            
            for entry in entries:
                # 获取URL
                url_elem = entry.find('atom:id', namespaces)
                url = url_elem.text if url_elem is not None else "No URL"
                
                # 获取标题
                title_elem = entry.find('atom:title', namespaces)
                title = title_elem.text.strip() if title_elem is not None else "No title"
                
                # 获取摘要
                summary_elem = entry.find('atom:summary', namespaces)
                abstract = summary_elem.text.strip() if summary_elem is not None else "No summary"
                
                # 获取作者
                authors = []
                author_elems = entry.findall('atom:author', namespaces)
                for author_elem in author_elems:
                    name_elem = author_elem.find('atom:name', namespaces)
                    if name_elem is not None:
                        authors.append(name_elem.text)
                authors_str = ", ".join(authors) if authors else "No authors"
                
                # 获取提交日期
                published_elem = entry.find('atom:published', namespaces)
                if published_elem is not None:
                    # 解析ISO格式的日期：2024-08-26T17:59:59Z
                    date_str = published_elem.text
                    # 移除时区信息并解析
                    date_str = date_str.replace('Z', '').split('T')[0]
                    first_submitted_date = datetime.strptime(date_str, "%Y-%m-%d")
                else:
                    first_submitted_date = datetime.now()
                
                # 获取分类
                categories = []
                category_elems = entry.findall('atom:category', namespaces)
                for cat_elem in category_elems:
                    term = cat_elem.get('term')
                    if term:
                        categories.append(term)
                
                # 获取评论（如果有）
                comment_elem = entry.find('arxiv:comment', namespaces)
                comments = comment_elem.text if comment_elem is not None else "No comments"
                
                paper = Paper(
                    url=url,
                    title=title.replace('\n', ' ').strip(),
                    first_submitted_date=first_submitted_date,
                    categories=categories,
                    authors=authors_str,
                    abstract=abstract.replace('\n', ' ').strip(),
                    comments=comments,
                )
                # API模式下，设置首次公布日期等于提交日期
                paper.first_announced_date = first_submitted_date
                papers.append(paper)
                
            return papers
            
        except ET.ParseError as e:
            self.console.log(f"[bold red]XML Parse Error: {e}")
            return []
        except Exception as e:
            self.console.log(f"[bold red]Error parsing API response: {e}")
            return []


    async def fetch_all_api(self):
        """
        使用arXiv API获取所有文章，最大支持1个月区间，严格按date_from和date_until过滤。
        """
        from datetime import datetime, timedelta

        # 计算最大允许的区间（31天）
        date_from:datetime = datetime.strptime(self.date_from, "%Y-%m-%d")
        date_until:datetime = datetime.strptime(self.date_until, "%Y-%m-%d")
        if (date_until - date_from).days > 31:
            self.console.log("API查询区间不能超过31天！")
            return 
        if date_from == date_until:
            date_from = date_until - timedelta(days=PREV_DAY)
        self.console.log(f"[bold green]Fetching papers using arXiv API for range: {date_from} ~ {date_until}")
        self.console.print(f"[grey] {self.get_api_url(0, self.step)}")

        xml_content = await self.request_api(0, self.step)
        if xml_content is None:
            self.console.log("[bold red]Failed to fetch initial API content, aborting...")
            return

        first_batch = self.parse_api_xml(xml_content)
        self.papers.extend(first_batch)

        # 从XML中获取总数信息
        try:
            root = ET.fromstring(xml_content)
            namespaces = {'opensearch': 'http://a9.com/-/spec/opensearch/1.1/'}
            total_elem = root.find('opensearch:totalResults', namespaces)
            if total_elem is not None:
                self.total = int(total_elem.text)
            else:
                if len(first_batch) < self.step:
                    self.total = len(first_batch)
                else:
                    self.total = 1000
        except:
            self.total = len(first_batch) if len(first_batch) < self.step else 1000

        self.console.log(f"[bold green]Total papers found: {self.total}")

        # 分批获取所有结果
        if self.total > self.step:
            with Progress(
                SpinnerColumn(),
                *Progress.get_default_columns(),
                TimeElapsedColumn(),
                console=self.console,
                transient=False,
            ) as p:
                task = p.add_task(
                    description=f"[bold green]Fetching papers via API",
                    total=min(self.total, 1000),
                )
                p.update(task, advance=len(first_batch))

                async def wrapper(start):
                    xml_content = await self.request_api(start, self.step)
                    if xml_content is None:
                        return []
                    papers = self.parse_api_xml(xml_content)
                    p.update(task, advance=len(papers))
                    return papers

                current_start = self.step
                while current_start < min(self.total, 1000):
                    papers_batch = await wrapper(current_start)
                    self.papers.extend(papers_batch)
                    if len(papers_batch) < self.step:
                        break
                    if papers_batch:
                        earliest_date = min(paper.first_submitted_date for paper in papers_batch)
                        if earliest_date < (date_from - timedelta(days=PREV_DAY)):
                            break
                    current_start += self.step

        self.console.log(f"[bold green]API fetching completed. Got {len(self.papers)} papers.")

        # 过滤严格区间
        filtered_papers = []
        for paper in self.papers:
            if date_from <= paper.first_submitted_date <= date_until:
                filtered_papers.append(paper)

        if self.trans_to:
            await self.translate()
        if filtered_papers:
            self.paper_db.add_papers(filtered_papers)
        self.papers = filtered_papers
        # 只保留所有tag都以cs.开头的论文
        if filtered_papers:
            pass
        else:
            self.console.log("[bold yellow]No filtered papers found.")

        self.papers = [paper for paper in self.papers if paper.categories and all(cat.startswith('cs.') for cat in paper.categories)]
        self.console.log(f"[bold green]After cs-only filtering: {len(self.papers)} papers.")
        self.console.log(f"[green]Translate papers to: {self.trans_to}")
        
        
        

    @property
    def meta_data(self):
        """
        返回搜索的元数据
        """
        return dict(repo_url="https://github.com/huiyeruzhou/arxiv_crawler", **self.__dict__)

    def get_url(self, start):
        """
        获取用于搜索的url

        Args:
            start (int): 返回结果的起始序号, 每个页面只会包含序号为[start, start+50)的文章
            filter_date_by (str, optional): 日期筛选方式. Defaults to "submitted_date_first".
        """
        # https://arxiv.org/search/advanced?terms-0-operator=AND&terms-0-term=LLM&terms-0-field=title&terms-1-operator=OR&terms-1-term=language+model&terms-1-field=title&terms-2-operator=OR&terms-2-term=multimodal&terms-2-field=title&terms-3-operator=OR&terms-3-term=finetuning&terms-3-field=title&terms-4-operator=AND&terms-4-term=GPT&terms-4-field=title&classification-computer_science=y&classification-physics_archives=all&classification-include_cross_list=include&date-year=&date-filter_by=date_range&date-from_date=2024-08-08&date-to_date=2024-08-15&date-date_type=submitted_date_first&abstracts=show&size=50&order=submitted_date
        
        # 构建搜索参数：必需关键词使用AND连接，可选关键词使用OR连接
        terms = []
        term_index = 0
        
        # 处理必需关键词 (AND 关系) - 每个关键词在任意字段中存在即可
        for kw in self.required_keywords:
            # 为每个必需关键词创建一个OR组（在多个字段中搜索）
            operator = "AND" if term_index > 0 else "AND"
            terms.append(f"&terms-{term_index}-operator={operator}&terms-{term_index}-term={kw}&terms-{term_index}-field=all")
            term_index += 1

        # 处理可选关键词 (二维数组，外层AND，内层OR) - 每个关键词在任意字段中存在即可
        for keyword_group in self.optional_keywords:
            if keyword_group:  # 确保组不为空
                # 对于每个关键词组，创建OR连接的查询
                for i, kw in enumerate(keyword_group):
                    # 组内第一个关键词与前面的内容用AND连接，组内其余关键词用OR连接
                    operator = "AND" if term_index == 0 or i == 0 else "OR" 
                    if term_index > 0 and i == 0:
                        operator = "AND"  # 组与组之间用AND连接
                    elif i > 0:
                        operator = "OR"   # 组内用OR连接
                    terms.append(f"&terms-{term_index}-operator={operator}&terms-{term_index}-term={kw}&terms-{term_index}-field=all")
                    term_index += 1

        kwargs = "".join(terms)
        date_from = self.search_from_date.strftime("%Y-%m")
        date_until = self.search_until_date.strftime("%Y-%m")
        return (
            f"https://arxiv.org/search/advanced?advanced={kwargs}"
            f"&classification-computer_science=y&classification-physics_archives=all&"
            f"classification-include_cross_list=include&"
            f"date-year=&date-filter_by=date_range&date-from_date={date_from}&date-to_date={date_until}&"
            f"date-date_type={self.filt_date_by}&abstracts=show&size={self.step}&order={self.order}&start={start}"
        )
    async def request(self, start):
        """
        异步请求网页，重试至多3次
        """
        error = 0
        url = self.get_url(start)
        while error <= 3:
            try:
                timeout = ClientTimeout(total=30)  # 使用ClientTimeout对象
                async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
                    async with session.get(url, proxy=self.proxy) as response:
                        response.raise_for_status()
                        content = await response.text()
                        return content
            except Exception as e:
                error += 1
                self.console.log(f"[bold red]Request {start} cause error: ")
                self.console.print_exception()
                self.console.log(f"[bold red]Retrying {start}... {error}/3")
        
        # 如果所有重试都失败了，返回None而不是让程序崩溃
        self.console.log(f"[bold red]All retries failed for request {start}")
        return None

    async def fetch_all(self):
        """
        (aio)获取所有文章
        """
        # 获取前50篇文章并记录总数
        self.console.log(f"[bold green]Fetching the first {self.step} papers...")
        self.console.print(f"[grey] {self.get_url(0)}")
        content = await self.request(0)
        if content is None:
            self.console.log("[bold red]Failed to fetch initial content, aborting...")
            return
        self.papers.extend(self.parse_search_html(content))

        # 获取剩余的内容
        with Progress(
            SpinnerColumn(),
            *Progress.get_default_columns(),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        ) as p:  # rich进度条
            task = p.add_task(
                description=f"[bold green]Fetching {self.total} results",
                total=self.total,
            )
            p.update(task, advance=self.step)

            async def wrapper(start):  # wrapper用于显示进度
                # 异步请求网页，并解析其中的内容
                content = await self.request(start)
                if content is None:
                    return []  # 如果请求失败，返回空列表
                papers = self.parse_search_html(content)
                p.update(task, advance=self.step)
                return papers

            # 创建异步任务
            fetch_tasks = []
            for start in range(self.step, self.total, self.step):
                fetch_tasks.append(wrapper(start))
            papers_list = await asyncio.gather(*fetch_tasks)
            self.papers.extend(chain(*papers_list))

        self.console.log(f"[bold green]Fetching completed. ")
        # 只保留所有tag都以cs.开头的论文
        self.papers = [paper for paper in self.papers if paper.categories and all(cat.startswith('cs.') for cat in paper.categories)]
        if self.trans_to:
            await self.translate()
        self.process_papers()

    async def fetch_update(self):
        """
        更新文章, 这会从最新公布的文章开始更新, 直到遇到已经爬取过的文章为止。
        为了效率，建议在运行fetch_all后再运行fetch_update
        """
        # 当前时间
        utc_now = datetime.now(UTC).replace(tzinfo=None)
        # 上一次更新最新文章的UTC时间. 除了更新新文章外也可能重新爬取了老文章, 数据库只看最新文章的时间戳。
        last_update = self.paper_db.newest_update_time()
        # 检查一下上次之后的最近一个arxiv更新日期
        self.search_from_date = next_arxiv_update_day(last_update)
        self.console.log(f"[bold yellow]last update: {last_update.strftime('%Y-%m-%d %H:%M:%S')}, "
                         f"next arxiv update: {self.search_from_date.strftime('%Y-%m-%d')}" 
                         )
        self.console.log(f"[bold yellow]UTC now: {utc_now.strftime('%Y-%m-%d %H:%M:%S')}")

        # 如果这一次的更新时间恰好是这个月的第一个更新日，那么当日更新的文章都会出现在上个月的搜索结果中
        # 为了正确获得这天的文章，我们上推一个月的搜索时间
        self.first_announced_date = self.search_from_date
        if self.search_from_date == next_arxiv_update_day(self.search_from_date.replace(day=1)):
            self.search_from_date = self.search_from_date - timedelta(days=31)
            self.console.log(f"[bold yellow]The update in {self.first_announced_date.strftime('%Y-%m-%d')} can only be found in the previous month.")
        else:
            self.console.log(
                f"[bold green]Searching from {self.search_from_date.strftime('%Y-%m-%d')} "
                f"to {self.search_until_date.strftime('%Y-%m-%d')}, fetch the first {self.step} papers..."
            )
        self.console.print(f"[grey] {self.get_url(0)}")

        continue_update = await self.update_async(0)
        for start in range(self.step, self.total, self.step):
            if not continue_update:
                break

            continue_update = await self.update_async(start)
        self.console.log(f"[bold green]Fetching completed. {len(self.papers)} new papers.")
        if self.trans_to:
            await self.translate()
        self.process_papers()

    def process_papers(self):
        """
        推断文章的首次公布日期, 并将文章添加到数据库中
        """
        # 从下一个可能的公布日期开始
        announced_date = next_arxiv_update_day(self.first_announced_date)   
        self.console.log(f"first announced date: {announced_date.strftime('%Y-%m-%d')}")
        # 按照从前到后的时间顺序梳理文章
        for paper in reversed(self.papers):
            # 文章于T日美东时间14:00(T UTC+0 18:00)前提交，将于T日美东时间20:00(T+1 UTC+0 00:00)公布，T始终为工作日。
            # 因此可知美东 T日的文章至少在UTC+0 T+1日公布，如果超过14:00甚至会在UTC+0 T+2日公布
            next_possible_annouced_date = next_arxiv_update_day(paper.first_submitted_date + timedelta(days=1))
            if announced_date < next_possible_annouced_date:
                announced_date = next_possible_annouced_date
            paper.first_announced_date = announced_date
        self.paper_db.add_papers(self.papers)
    
    def reprocess_papers(self):
        """
        这会从数据库中获取所有文章, 并重新推断文章的首次公布日期，并打印调试信息
        """
        self.papers = self.paper_db.fetch_all()
        self.process_papers()
        with open("announced_date.csv", "w") as f:
            f.write("url,title,announced_date,submitted_date\n")
            for paper in self.papers:
                f.write(
                    f"{paper.url},{paper.title},{paper.first_announced_date.strftime('%Y-%m-%d')},{paper.first_submitted_date.strftime('%Y-%m-%d')}\n"
                )

    def update(self, start) -> bool:
        """
        同步版本的update，用于fetch_update方法
        """
        content = asyncio.run(self.request(start))
        self.papers.extend(self.parse_search_html(content))
        cnt_new = self.paper_db.count_new_papers(self.papers[start : start + self.step])
        if cnt_new < self.step:
            self.papers = self.papers[: start + cnt_new]
            return False
        else:
            return True

    async def update_async(self, start) -> bool:
        content = await self.request(start)
        if content is None:
            return False  # 如果请求失败，停止更新
        self.papers.extend(self.parse_search_html(content))
        cnt_new = self.paper_db.count_new_papers(self.papers[start : start + self.step])
        if cnt_new < self.step:
            self.papers = self.papers[: start + cnt_new]
            return False
        else:
            return True

    def parse_search_html(self, content) -> list[Paper]:
        """
        解析搜索结果页面, 并将结果保存到self.paper_result中
        初次调用时, 会解析self.total

        Args:
            content (str): 网页内容
        """
        
        # 检查content是否为None
        if content is None:
            self.console.log("[bold red]Cannot parse None content")
            return []

        """下面是一个搜索结果的例子
        <li class="arxiv-result">
            <div class="is-marginless">
                <p class="list-title is-inline-block">
                    <a href="https://arxiv.org/abs/physics/9403001">arXiv:physics/9403001</a>
                    <span>&nbsp;[<a href="https://arxiv.org/pdf/physics/9403001">pdf</a>, <a
                            href="https://arxiv.org/ps/physics/9403001">ps</a>, <a
                            href="https://arxiv.org/format/physics/9403001">other</a>]&nbsp;</span>
                </p>
                <div class="tags is-inline-block">
                    <span class="tag is-small is-link tooltip is-tooltip-top" data-tooltip="Popular Physics">
                        physics.pop-ph</span>
                    <span class="tag is-small is-grey tooltip is-tooltip-top"
                        data-tooltip="High Energy Physics - Theory">hep-th</span>
                </div>
                <div class="is-inline-block" style="margin-left: 0.5rem">
                    <div class="tags has-addons">
                        <span class="tag is-dark is-size-7">doi</span>
                        <span class="tag is-light is-size-7">
                            <a class="" href="https://doi.org/10.1063/1.2814991">10.1063/1.2814991 <i
                                    class="fa fa-external-link" aria-hidden="true"></i></a>
                        </span>
                    </div>
                </div> 
            </div>
            <p class="title is-5 mathjax">
                Desperately Seeking Superstrings
            </p>
            <p class="authors">
                <span class="has-text-black-bis has-text-weight-semibold">Authors:</span>
                    <a href="/search/?searchtype=author&amp;query=Ginsparg%2C+P">Paul Ginsparg</a>, <a href="/search/?searchtype=author&amp;query=Glashow%2C+S">Sheldon Glashow</a> 
            </p> 
            <p class="abstract mathjax">
                <span class="has-text-black-bis has-text-weight-semibold">Abstract</span>: 
                
                <span class="abstract-short has-text-grey-dark mathjax" id="physics/9403001v1-abstract-short"
                    style="display: inline;"> We provide a detailed analysis of the problems and prospects of superstring theory c.
                1986, anticipating much of the progress of the decades to follow. </span>

                <span class="abstract-full has-text-grey-dark mathjax" id="physics/9403001v1-abstract-full"
                    style="display: none;"> We provide a detailed analysis of the problems and prospects of
                superstring theory c. 1986, anticipating much of the progress of the decades to follow. 
                <a class="is-size-7" style="white-space: nowrap;"
                        onclick="document.getElementById('physics/9403001v1-abstract-full').style.display = 'none'; document.getElementById('physics/9403001v1-abstract-short').style.display = 'inline';">△ Less</a>
                </span>
            </p> 
            <p class="is-size-7"><span class="has-text-black-bis has-text-weight-semibold">Submitted</span>
                25 April, 1986; <span class="has-text-black-bis has-text-weight-semibold">originally
                announced</span> March 1994. </p> 
            <p class="comments is-size-7">
                <span class="has-text-black-bis has-text-weight-semibold">Comments:</span>
                <span class="has-text-grey-dark mathjax">originally appeared as a Reference Frame in Physics
                    Today, May 1986</span>
            </p> 
            <p class="comments is-size-7">
                <span class="has-text-black-bis has-text-weight-semibold">Journal ref:</span> Phys.Today
                86N5 (1986) 7-9 </p> 
        </li>
        """

        soup = BeautifulSoup(content, "html.parser")
        if not self.total:
            total = soup.select("#main-container > div.level.is-marginless > div.level-left > h1")[0].text
            # "Showing 1–50 of 2,542,002 results" or "Sorry, your query returned no results"
            if "Sorry" in total:
                self.total = 0
                return []
            total = int(total[total.find("of") + 3 : total.find("results")].replace(",", ""))
            self.total = total

        results = soup.find_all("li", {"class": "arxiv-result"})
        papers = []
        for result in results:

            url_tag = result.find("a")
            url = url_tag["href"] if url_tag else "No link"

            title_tag = result.find("p", class_="title")
            title = self.parse_search_text(title_tag) if title_tag else "No title"
            title = title.strip()

            date_tag = result.find("p", class_="is-size-7")
            date = date_tag.get_text(strip=True) if date_tag else "No date"
            if "v1" in date:
                # Submitted9 August, 2024; v1submitted 8 August, 2024; originally announced August 2024.
                # 注意空格会被吞掉，这里我们要找最早的提交日期
                v1 = date.find("v1submitted")
                date = date[v1 + 12 : date.find(";", v1)]
            else:
                # Submitted8 August, 2024; originally announced August 2024.
                # 注意空格会被吞掉
                submit_date = date.find("Submitted")
                date = date[submit_date + 9 : date.find(";", submit_date)]

            category_tag = result.find_all("span", class_="tag")
            categories = [
                category.get_text(strip=True) for category in category_tag if "tooltip" in category.get("class")
            ]

            authors_tag = result.find("p", class_="authors")
            authors = authors_tag.get_text(strip=True)[len("Authors:") :] if authors_tag else "No authors"

            summary_tag = result.find("span", class_="abstract-full")
            abstract = self.parse_search_text(summary_tag) if summary_tag else "No summary"
            abstract = abstract.strip()

            comments_tag = result.find("p", class_="comments")
            comments = comments_tag.get_text(strip=True)[len("Comments:") :] if comments_tag else "No comments"

            papers.append(
                Paper(
                    url=url,
                    title=title,
                    first_submitted_date=datetime.strptime(date, "%d %B, %Y"),
                    categories=categories,
                    authors=authors,
                    abstract=abstract,
                    comments=comments,
                )
            )
        return papers

    def parse_search_text(self, tag):
        string = ""
        for child in tag.children:
            if isinstance(child, NavigableString):
                string += re.sub(r"\s+", " ", child)
            elif isinstance(child, Tag):
                if child.name == "span" and "search-hit" in child.get("class"):
                    string += re.sub(r"\s+", " ", child.get_text(strip=False))
                elif child.name == "a" and ".style.display" in child.get("onclick"):
                    pass
                else:
                    print(f"出现了unexpected情况, child:{child}")
        return string

    async def translate(self):
        if not self.trans_to:
            raise ValueError("No target language specified.")
        self.console.log("[bold green]Translating...")
        with Progress(
            SpinnerColumn(),
            *Progress.get_default_columns(),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        ) as p:
            total = len(self.papers)
            task = p.add_task(
                description=f"[bold green]Translating {total} papers",
                total=total,
            )

            async def worker(paper):
                await paper.translate(langto=self.trans_to)
                p.update(task, advance=1)

            await asyncio.gather(*[worker(paper) for paper in self.papers])

    def to_markdown(self, output_dir="./output_llms", filename_format="%Y-%m-%d", meta=False):
        self.paper_exporter.to_markdown(output_dir, filename_format, self.meta_data if meta else None)

    def to_csv(self, output_dir="./output_llms", filename_format="%Y-%m-%d",  header=False, csv_config={},):
        self.paper_exporter.to_csv(output_dir, filename_format, header, csv_config)


async def get_daily_llm_papers(date_from=None, date_until=None, translate=True, 
                               optional_keywords=None, required_keywords=None,
                               use_api=True,  # 新增参数：是否使用API
                               db_path='/Users/bytedance/code/lark-samples-main/echo_bot/python/arxiv_crawler/papers.db'):
    """
    获取指定日期范围的LLM相关论文
    
    Args:
        date_from (str, optional): 开始日期，格式为'YYYY-MM-DD'，默认为今天
        date_until (str, optional): 结束日期，格式为'YYYY-MM-DD'，默认为今天
        translate (bool): 是否翻译论文标题和摘要
        optional_keywords (list): 可选关键词二维列表（外层AND，内层OR）
        required_keywords (list): 必需关键词列表（AND连接）
        use_api (bool): 是否使用arXiv API（True）还是网页爬取（False）
        db_path (str): 数据库路径
    
    Returns:
        list[Paper]: 获取到的论文列表
    """
    from datetime import date, datetime
    import asyncio

    if date_from is None:
        date_from = date.today().strftime("%Y-%m-%d")
    if date_until is None:
        date_until = date.today().strftime("%Y-%m-%d")
    
    date_from_datetime = datetime.strptime(date_from, "%Y-%m-%d")
    date_until_datetime = datetime.strptime(date_until, "%Y-%m-%d")

    # 设置默认关键词（二维数组格式）
    if optional_keywords is None:
        optional_keywords = [
            ["agent", "research"],
            ["LLM", "language model", "large language model"],
            ["GPT", "transformer", "attention"]
        ]
    if required_keywords is None:
        required_keywords = []

    # 首先检查数据库中是否已有指定日期范围的论文
    db = PaperDatabase(db_path=db_path)
    existing_papers = db.search_papers_by_keywords(
        required_keywords=required_keywords,
        optional_keywords=optional_keywords,
        date_from=date_from,
        date_until=date_until
    )
    if existing_papers:
        print(f"从数据库中找到 {len(existing_papers)} 篇 {date_from} 至 {date_until} 包含指定关键词的论文")
        return existing_papers

    print(f"开始抓取{date_from} 至 {date_until} 包含指定关键词的论文...")
    print(f"optional_keywords: {optional_keywords}")
    print(f"required_keywords: {required_keywords}")
    print(f"使用方式: {'arXiv API' if use_api else '网页爬取'}")

    # 主要关注AI、ML、CL等领域
    llm_categories = ["cs.AI", "cs.LG", "cs.CL", "cs.IR", "cs.MA", "cs.HC"]
    scraper = ArxivScraper(
        date_from=date_from,
        date_until=date_until,
        category_whitelist=llm_categories,
        optional_keywords=optional_keywords,
        required_keywords=required_keywords,
        trans_to="zh-CN" if translate else None,
    )

    if use_api:
        # 使用API方式获取论文
        await scraper.fetch_all_api()
    else:
        # 检查数据库的最新更新时间
        last_update = db.newest_update_time()
        week_ago = date_from_datetime - timedelta(days=7)

        if last_update and last_update >= week_ago:
            print(f"数据库最新更新时间: {last_update.strftime('%Y-%m-%d %H:%M:%S')}, 使用增量更新...")
            await scraper.fetch_update()
        else:
            print(f"数据库最新更新时间: {last_update.strftime('%Y-%m-%d %H:%M:%S') if last_update else '无数据'}, 使用全量获取...")
            await scraper.fetch_all()

    papers = scraper.papers
    print(f"抓取到的总paper数量:{len(papers)}")
    if papers:
        return papers
    target_papers = db.search_papers_by_keywords(
        required_keywords=required_keywords,
        optional_keywords=optional_keywords,
        date_from=date_from,
        date_until=date_until
    )
    print(f"指定日期 {date_from} - {date_until} 包含指定关键词的paper数量:{len(target_papers)}")
    if not target_papers and date_from == date_until:
        print(f"搜索时间范围扩大为：{date_from_datetime - timedelta(days=PREV_DAY)} - {date_until}")
        target_papers = db.search_papers_by_keywords(
            required_keywords=required_keywords,
            optional_keywords=optional_keywords,
            date_from=date_from_datetime - timedelta(days=PREV_DAY),
            date_until=date_until
        )
        print(f"扩大范围后包含指定关键词的paper数量:{len(target_papers)}")

    return target_papers

async def update_daily_cs_paper():
    """
    更新计算机论文到数据库
    """
    from datetime import date
    today = date.today()
    scraper = ArxivScraper(
        date_from=today.strftime("%Y-%m-%d"),
        date_until=today.strftime("%Y-%m-%d"),
    )
    await scraper.fetch_update()

if __name__ == "__main__":
    from datetime import date, timedelta

    today = date.today()

    scraper = ArxivScraper(
        date_from=(today - timedelta(days=7)).strftime("%Y-%m-%d"),
        date_until=today.strftime("%Y-%m-%d"),
        #optional_keywords=["browse"],  # 使用新的关键词格式
        required_keywords=["agent","research"]
    )
    #asyncio.run(scraper.fetch_all())
    #scraper.to_markdown(meta=True)
    cnt = scraper.paper_db.delete_papers_in_date_range('2025-08-19', '2025-08-25')
    #print(cnt)
    asyncio.run(scraper.fetch_all_api())
    scraper.to_csv(header=False, csv_config=dict(delimiter="\t"))
    scraper.to_markdown(meta=True)