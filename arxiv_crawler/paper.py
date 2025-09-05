import asyncio
import csv
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path

# Python 3.9+ compatibility for UTC
try:
    from datetime import UTC
except ImportError:
    from datetime import timezone
    UTC = timezone.utc

from rich.console import Console
from typing_extensions import Iterable

from async_translator import async_translate
from categories import parse_categories



@dataclass
class Paper:
    first_submitted_date: datetime
    title: str
    categories: list
    url: str
    authors: str
    abstract: str
    comments: str
    title_translated: str | None = None
    abstract_translated: str | None = None
    first_announced_date: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row):
        return cls(
            first_submitted_date=datetime.strptime(row["first_submitted_date"], "%Y-%m-%d"),
            title=row["title"],
            categories=row["categories"].split(","),
            url=row["url"],
            authors=row["authors"],
            abstract=row["abstract"],
            comments=row["comments"],
            title_translated=row["title_translated"],
            abstract_translated=row["abstract_translated"],
            first_announced_date=datetime.strptime(row["first_announced_date"], "%Y-%m-%d"),
        )
    @property
    def papers_cool_url(self):
        return self.url.replace("https://arxiv.org/abs", "https://papers.cool/arxiv")
    
    @property
    def pdf_url(self):
        return self.url.replace("https://arxiv.org/abs", "https://arxiv.org/pdf")

    def to_markdown(self):
        categories = ",".join(parse_categories(self.categories))
        abstract = (
            f"- **摘要**: {self.abstract_translated}"
            if self.abstract_translated
            else f"- **Abstract**: {self.abstract}"
        )

        return f"""### {self.title} 
[[arxiv]({self.url})] [[cool]({self.papers_cool_url})] [[pdf]({self.pdf_url})]
> **Authors**: {self.authors}
> **First submission**: {self.first_submitted_date.strftime("%Y-%m-%d")}
> **First announcement**: {self.first_announced_date.strftime("%Y-%m-%d")}
> **comment**: {self.comments}
- **标题**: {self.title_translated}
- **领域**: {categories}
{abstract}

"""

    async def translate(self, langto="zh-CN"):
        self.title_translated = await async_translate(self.title, langto=langto)
        self.abstract_translated = await async_translate(self.abstract, langto=langto)


@dataclass
class PaperRecord:
    paper: Paper
    comment: str

    def to_markdown(self):
        if self.comment != "-":
            return f"""- [{self.paper.title}]({self.paper.url})
  - **标题**: {self.paper.title_translated}
  - **Filtered Reason**: {self.comment}
"""
        else:
            return self.paper.to_markdown()


class PaperDatabase:
    def __init__(self, db_path="papers.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = self._row_factory
        self._create_table()

    @staticmethod
    def _row_factory(cursor, row):
        row = sqlite3.Row(cursor, row)
        # all fields in Paper, plus `update_time`
        if len(row.keys()) == len(fields(Paper)) + 1:
            return Paper.from_row(row)
        else:
            return row

    def _create_table(self):
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    url TEXT PRIMARY KEY,
                    authors TEXT NOT NULL,
                    title_translated TEXT,
                    first_submitted_date DATE NOT NULL,
                    first_announced_date DATE NOT NULL,
                    update_time DATETIME NOT NULL,
                    categories TEXT NOT NULL,
                    title TEXT NOT NULL,
                    comments TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    abstract_translated TEXT
                )
            """
            )

    def add_papers(self, papers: Iterable[Paper]):
        assert all([paper.first_announced_date is not None for paper in papers])
        with self.conn:
            data_to_insert = [
                (
                    paper.url,
                    paper.authors,
                    paper.abstract,
                    paper.title,
                    ",".join(paper.categories),
                    paper.first_submitted_date.strftime("%Y-%m-%d"),
                    paper.first_announced_date.strftime("%Y-%m-%d"),
                    paper.title_translated,
                    paper.abstract_translated,
                    paper.comments,
                    datetime.now(UTC).replace(tzinfo=None)
                )
                for paper in papers
            ]
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO papers 
                (url, authors, abstract, title, categories, first_submitted_date, first_announced_date, title_translated, abstract_translated, comments, update_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                data_to_insert,
            )

    def count_new_papers(self, papers: Iterable[Paper]) -> int:
        cnt = 0
        for paper in papers:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    SELECT * FROM papers WHERE url = ?
                    """,
                    (paper.url,),
                )
                if cursor.fetchone():
                    break
                else:
                    cnt += 1
        return cnt

    def fetch_papers_on_date(self, date: datetime) -> list[Paper]:
        with self.conn:
            cursor = self.conn.execute(
                """
                SELECT * FROM papers WHERE first_announced_date = ?
                """,
                (date.strftime("%Y-%m-%d"),),
            )
            return cursor.fetchall()

    def delete_papers_on_date(self, date) -> int:
        """
        删除指定日期的所有论文
        
        Args:
            date: 要删除论文的日期，可以是 datetime 对象或 'YYYY-MM-DD' 格式的字符串
            
        Returns:
            int: 删除的论文数量
        """
        # 处理输入参数，支持字符串和datetime对象
        if isinstance(date, str):
            date_str = date
        elif isinstance(date, datetime):
            date_str = date.strftime("%Y-%m-%d")
        else:
            raise ValueError("date 参数必须是字符串 'YYYY-MM-DD' 或 datetime 对象")
        
        # 先查询有多少篇论文会被删除
        with self.conn:
            cursor = self.conn.execute(
                """
                SELECT COUNT(*) as count FROM papers WHERE first_announced_date = ?
                """,
                (date_str,),
            )
            count = cursor.fetchone()["count"]
        
        # 执行删除
        with self.conn:
            self.conn.execute(
                """
                DELETE FROM papers WHERE first_announced_date = ?
                """,
                (date_str,),
            )
        
        return count

    def delete_papers_in_date_range(self, start_date, end_date) -> int:
        """
        删除指定日期范围内的所有论文
        
        Args:
            start_date: 开始日期（包含），可以是 datetime 对象或 'YYYY-MM-DD' 格式的字符串
            end_date: 结束日期（包含），可以是 datetime 对象或 'YYYY-MM-DD' 格式的字符串
            
        Returns:
            int: 删除的论文数量
        """
        # 处理开始日期
        if isinstance(start_date, str):
            start_str = start_date
        elif isinstance(start_date, datetime):
            start_str = start_date.strftime("%Y-%m-%d")
        else:
            raise ValueError("start_date 参数必须是字符串 'YYYY-MM-DD' 或 datetime 对象")
        
        # 处理结束日期
        if isinstance(end_date, str):
            end_str = end_date
        elif isinstance(end_date, datetime):
            end_str = end_date.strftime("%Y-%m-%d")
        else:
            raise ValueError("end_date 参数必须是字符串 'YYYY-MM-DD' 或 datetime 对象")
        
        # 先查询有多少篇论文会被删除
        with self.conn:
            cursor = self.conn.execute(
                """
                SELECT COUNT(*) as count FROM papers 
                WHERE first_announced_date BETWEEN ? AND ?
                """,
                (start_str, end_str),
            )
            count = cursor.fetchone()["count"]
        
        # 执行删除
        with self.conn:
            self.conn.execute(
                """
                DELETE FROM papers 
                WHERE first_announced_date BETWEEN ? AND ?
                """,
                (start_str, end_str),
            )
        
        return count

    def fetch_all(self) -> list[Paper]:
        with self.conn:
            cursor = self.conn.execute(
                """
                SELECT * FROM papers ORDER BY url DESC
                """
            )
            return cursor.fetchall()

    def newest_update_time(self) -> datetime:
        """
        最新更新时间是“上一次爬取最新论文的时间”
        由于数据库可能补充爬取过去的论文，所以先选最新论文，再从其中选最新的爬取时间
        """
        with self.conn:
            cursor = self.conn.execute(
                """
                SELECT MAX(update_time) as max_updated_time
                FROM papers
                WHERE first_announced_date = (SELECT MAX(first_announced_date) FROM papers)
                """
            )
        time = cursor.fetchone()["max_updated_time"].split(".")[0]
        return datetime.strptime(time, "%Y-%m-%d %H:%M:%S")

    async def translate_missing(self, langto="zh-CN"):
        with self.conn:
            cursor = self.conn.execute(
                "SELECT url, title, abstract FROM papers WHERE title_translated IS NULL OR abstract_translated IS NULL"
            )
            papers = cursor.fetchall()

        async def worker(url, title, abstract):
            title_translated = await async_translate(title, langto=langto) if title else None
            abstract_translated = await async_translate(abstract, langto=langto) if abstract else None
            with self.conn:
                self.conn.execute(
                    "UPDATE papers SET title_translated = ?, abstract_translated = ? WHERE url = ?",
                    (title_translated, abstract_translated, url),
                )

        await asyncio.gather(*[worker(url, title, abstract) for url, title, abstract in papers])

    def search_papers_by_keywords(self, 
                                   required_keywords: list[str] = None, 
                                   optional_keywords: list[list[str]] = None,
                                   date_from: str = None,
                                   date_until: str = None,
                                   limit: int = None) -> list[Paper]:
        """
        根据关键词搜索论文
        
        Args:
            required_keywords: 必需关键词，必须全部出现 (AND关系)
            optional_keywords: 可选关键词二维数组，外层AND，内层OR (例如: [["A", "B"], ["C", "D"]] 表示 (A OR B) AND (C OR D))
            date_from: 开始日期 'YYYY-MM-DD'，可选
            date_until: 结束日期 'YYYY-MM-DD'，可选
            limit: 限制返回数量
            
        Returns:
            list[Paper]: 匹配的论文列表
        """
        if not required_keywords:
            required_keywords = []
        if not optional_keywords:
            optional_keywords = []
        
        # 默认搜索字段：标题和摘要
        search_fields = ["title", "abstract"]
        
        # 构建搜索条件
        conditions = []
        params = []
        
        # 处理必需关键词 (AND关系)
        for keyword in required_keywords:
            field_conditions = []
            for field in search_fields:
                field_conditions.append(f"{field} LIKE ?")
                params.append(f"%{keyword}%")
            if field_conditions:
                conditions.append(f"({' OR '.join(field_conditions)})")
        
        # 处理可选关键词 (二维数组，外层AND，内层OR)
        if optional_keywords:
            for keyword_group in optional_keywords:
                if keyword_group:  # 确保组不为空
                    group_conditions = []
                    for keyword in keyword_group:
                        field_conditions = []
                        for field in search_fields:
                            field_conditions.append(f"{field} LIKE ?")
                            params.append(f"%{keyword}%")
                        if field_conditions:
                            group_conditions.append(f"({' OR '.join(field_conditions)})")
                    if group_conditions:
                        # 组内用OR连接
                        conditions.append(f"({' OR '.join(group_conditions)})")
        
        # 处理日期范围
        if date_from:
            conditions.append("first_announced_date >= ?")
            params.append(date_from)
        if date_until:
            conditions.append("first_announced_date <= ?")
            params.append(date_until)
        
        # 构建完整的SQL查询
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        query = f"""
            SELECT * FROM papers 
            WHERE {where_clause}
            ORDER BY first_announced_date DESC
        """
        
        if limit:
            query += f" LIMIT {limit}"
        
        with self.conn:
            cursor = self.conn.execute(query, params)
            return cursor.fetchall()

    def search_papers_by_text(self, search_text: str, limit: int = None) -> list[Paper]:
        """
        根据文本内容搜索论文（简单的全文搜索）
        
        Args:
            search_text: 搜索文本
            limit: 限制返回数量
            
        Returns:
            list[Paper]: 匹配的论文列表
        """
        # 默认搜索字段：标题、摘要及其翻译
        search_fields = ["title", "abstract", "title_translated", "abstract_translated"]
        
        # 构建搜索条件
        field_conditions = []
        params = []
        
        for field in search_fields:
            field_conditions.append(f"{field} LIKE ?")
            params.append(f"%{search_text}%")
        
        where_clause = " OR ".join(field_conditions)
        query = f"""
            SELECT * FROM papers 
            WHERE {where_clause}
            ORDER BY first_announced_date DESC
        """
        
        if limit:
            query += f" LIMIT {limit}"
        
        with self.conn:
            cursor = self.conn.execute(query, params)
            return cursor.fetchall()

    def get_date_statistics(self) -> dict:
        """
        获取数据库中各日期的论文统计信息
        
        Returns:
            dict: 日期为键，论文数量为值的字典
        """
        with self.conn:
            cursor = self.conn.execute(
                """
                SELECT first_announced_date, COUNT(*) as count 
                FROM papers 
                GROUP BY first_announced_date 
                ORDER BY first_announced_date DESC
                """
            )
            results = cursor.fetchall()
        
        return {row["first_announced_date"]: row["count"] for row in results}


class PaperExporter:
    def __init__(
        self,
        date_from: str,
        date_until: str,
        categories_blacklist: list[str] = [],
        categories_whitelist: list[str] = ["cs.CV", "cs.AI", "cs.LG", "cs.CL", "cs.IR", "cs.MA"],
        database_path="papers.db",
    ):
        self.db = PaperDatabase(database_path)
        self.date_from = datetime.strptime(date_from, "%Y-%m-%d")
        self.date_until = datetime.strptime(date_until, "%Y-%m-%d")
        self.date_range_days = (self.date_until - self.date_from).days + 1
        self.categories_blacklist = set(categories_blacklist)
        self.categories_whitelist = set(categories_whitelist)
        self.console = Console()

    def filter_papers(self, papers: list[Paper]) -> tuple[list[PaperRecord], list[PaperRecord]]:
        filtered_paper_records = []
        chosen_paper_records = []
        print(f"白名单类别: {self.categories_whitelist}")
        for paper in papers:
            categories = set(paper.categories)
            if not (self.categories_whitelist & categories):
                categories_str = ",".join(categories)
                filtered_paper_records.append(PaperRecord(paper, f"none of {categories_str} in whitelist"))
            elif black := self.categories_blacklist & categories:
                black_str = ",".join(black)
                filtered_paper_records.append(PaperRecord(paper, f"cat:{black_str} in blacklist"))
            else:
                chosen_paper_records.append(PaperRecord(paper, "-"))
        return chosen_paper_records, filtered_paper_records

    def to_markdown(self, output_dir="./output_llms", filename_format="%Y-%m-%d", metadata=None):
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)

        if metadata:
            repo_url = metadata["repo_url"]
            categories = ",".join(metadata["category_whitelist"])
            optional_keywords = ", ".join(metadata["optional_keywords"])
            preface_str = f"""
> 本文由 [{repo_url}]({repo_url}) 自动生成
>
> 领域白名单：{categories}
> 关键词： {optional_keywords}

""".lstrip()
        else:
            preface_str = ""

        for i in range(self.date_range_days):
            current = self.date_from + timedelta(days=i)
            current_filename = current.strftime(filename_format)

            with open(output_dir / f"{current_filename}.md", "w", encoding="utf-8") as file:
                papers = self.db.fetch_papers_on_date(current)
                chosen_records, filtered_records = self.filter_papers(papers)
                papers_str = f"# 论文全览：{current_filename}\n\n共有{len(chosen_records)}篇相关领域论文, 另有{len(filtered_records)}篇其他\n\n"

                chosen_dict = defaultdict(list)
                for record in chosen_records:
                    chosen_dict[record.paper.categories[0]].append(record)
                for category in sorted(chosen_dict.keys()):
                    category_en = parse_categories([category], lang="en")[0]
                    category_zh = parse_categories([category], lang="zh-CN")[0]
                    papers_str += f"## {category_zh}({category}:{category_en})\n\n"
                    for record in chosen_dict[category]:
                        papers_str += record.to_markdown()

                papers_str += "## 其他论文\n\n"
                for record in filtered_records:
                    papers_str += record.to_markdown()

                file.write(preface_str + papers_str)

            self.console.log(
                f"[bold green]Output {current_filename}.md completed. {len(chosen_records)} papers chosen, {len(filtered_records)} papers filtered"
            )

    def to_csv(self, output_dir="./output_llms", filename_format="%Y-%m-%d", header=True, csv_config={}):
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)

        csv_table = {
            "Title": lambda record: record.paper.title,
            "Interest": lambda record: ("chosen" if record.comment == "-" else "filtered"),
            "Title Translated": lambda record: (
                record.paper.title_translated if record.paper.title_translated else "-"
            ),
            "Categories": lambda record: ",".join(record.paper.categories),
            "Authors": lambda record: record.paper.authors,
            "URL": lambda record: record.paper.url,
            "PapersCool": lambda record: record.paper.url.replace("https://arxiv.org/abs", "https://papers.cool/arxiv"),
            "First Submitted Date": lambda record: record.paper.first_submitted_date.strftime("%Y-%m-%d"),
            "First Announced Date": lambda record: record.paper.first_announced_date.strftime("%Y-%m-%d"),
            "Abstract": lambda record: record.paper.abstract,
            "Abstract Translated": lambda record: (
                record.paper.abstract_translated if record.paper.abstract_translated else "-"
            ),
            "Comments": lambda record: record.paper.comments,
            "Note": lambda record: record.comment,
        }

        headers = list(csv_table.keys())

        for i in range(self.date_range_days):
            current = self.date_from + timedelta(days=i)
            current_filename = current.strftime(filename_format)
            self.console.log(f"目标日期:{current}")
            with open(output_dir / f"{current_filename}.csv", "w", encoding="utf-8") as file:
                if "lineterminator" not in csv_config:
                    csv_config["lineterminator"] = "\n"
                writer = csv.writer(file, **csv_config)
                if header:
                    writer.writerow(headers)

                papers = self.db.fetch_papers_on_date(current)
                self.console.log(f"目标日期含有论文:{len(papers)}")
                chosen_records, filtered_records = self.filter_papers(papers)
                for record in chosen_records + filtered_records:
                    writer.writerow([fn(record) for fn in csv_table.values()])

                self.console.log(
                    f"[bold green]Output {current_filename}.csv completed. {len(chosen_records)} papers chosen, {len(filtered_records)} papers filtered"
                )


if __name__ == "__main__":
    from datetime import date, timedelta

    today = date.today()

    exporter = PaperExporter(today.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
    exporter.to_markdown()
    exporter.to_csv(csv_config=dict(delimiter="\t", header=False))
