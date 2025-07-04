import arxiv
import argparse
import os
import sys
from dotenv import load_dotenv
load_dotenv(override=True)
os.environ["TOKENIZERS_PARALLELISM"] = "false"
from pyzotero import zotero
from recommender import rerank_paper
from construct_email import render_email, send_email
from tqdm import trange,tqdm
from loguru import logger
from gitignore_parser import parse_gitignore
from tempfile import mkstemp
from paper import ArxivPaper
from llm import set_global_llm
import feedparser
from search import generate_search_keywords, build_arxiv_query
import yaml


    
def get_zotero_corpus(id:str,key:str) -> list[dict]:
    zot = zotero.Zotero(id, 'user', key)
    collections = zot.everything(zot.collections())
    collections = {c['key']:c for c in collections}
    corpus = zot.everything(zot.items(itemType='conferencePaper || journalArticle || preprint'))
    corpus = [c for c in corpus if c['data']['abstractNote'] != '']
    def get_collection_path(col_key:str) -> str:
        if p := collections[col_key]['data']['parentCollection']:
            return get_collection_path(p) + ' / ' + collections[col_key]['data']['name']
        else:
            return collections[col_key]['data']['name']
    for c in corpus:
        paths = [get_collection_path(col) for col in c['data']['collections']]
        c['paths'] = paths

    print(corpus)
    
    return corpus

# def filter_corpus(corpus:list[dict], pattern:str) -> list[dict]:
#     _,filename = mkstemp()
#     with open(filename,'w') as file:
#         file.write(pattern)
#     matcher = parse_gitignore(filename,base_dir='./')
#     new_corpus = []
#     for c in corpus:
#         match_results = [matcher(p) for p in c['paths']]
#         if not any(match_results):
#             new_corpus.append(c)
#     os.remove(filename)
#     return new_corpus

# 获取标题，下载时间，摘要
def choose_corpus(corpus:list[dict]) -> dict:
    new_corpus = []
    for c in corpus:
        c_dict = {'key':c['key'], 'title':c['data']['title'], 'dateAdded':c['data']['dateAdded'], 'abstractNote':c['data']['abstractNote']}
        new_corpus.append(c_dict)
    return new_corpus

def get_authors(authors, first_author = False):
    output = str()
    if first_author == False:
        output = ", ".join(str(author) for author in authors)
    else:
        output = authors[0]
    return output
    
def sort_papers(papers):
    output = dict()
    keys = list(papers.keys())
    keys.sort(reverse=True)
    for key in keys:
        output[key] = papers[key]
    return output    

def get_arxiv_paper(query: str, debug: bool = False, max_results: int = 30) -> list[ArxivPaper]:
    # 创建 arxiv 搜索引擎实例
    search_engine = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate
    )
    papers = []
    for result in search_engine.results():
        # 将论文封装成 ArxivPaper 对象
        paper = ArxivPaper(result)
        if debug:  # 仅在调试模式下打印标签
            logger.debug(f"Generated labels for {paper.title}: {paper.labels}")
        papers.append(paper)
        
    return papers



parser = argparse.ArgumentParser(description='Recommender system for academic papers')

def add_argument(*args, **kwargs):
    def get_env(key:str,default=None):
        # handle environment variables generated at Workflow runtime
        # Unset environment variables are passed as '', we should treat them as None
        v = os.environ.get(key)
        if v == '' or v is None:
            return default
        return v
    parser.add_argument(*args, **kwargs)
    arg_full_name = kwargs.get('dest',args[-1][2:])
    env_name = arg_full_name.upper()
    env_value = get_env(env_name)
    if env_value is not None:
        #convert env_value to the specified type
        if kwargs.get('type') == bool:
            env_value = env_value.lower() in ['true','1']
        else:
            env_value = kwargs.get('type')(env_value)
        parser.set_defaults(**{arg_full_name:env_value})

def update_args(args):
    yaml_file_path = 'config.yaml'
    flag=True
    try:
        # 使用 'with open' 可以确保文件被正确关闭
        with open(yaml_file_path, 'r', encoding='utf-8') as file:
            # 使用 safe_load 将 YAML 文件内容解析为 Python 对象
            data = yaml.safe_load(file)
            for key in args.__dict__.keys():
                if args.__dict__[key] is None:
                    if (data[key] is None or key not in data) and key!='zotero_ignore':
                        flag=False
                        break
                    args.__dict__[key]=data[key]
        1==1
    except FileNotFoundError:
        logger.info(f"Error: File '{yaml_file_path}' is not found.")
    except yaml.YAMLError as e:
        logger.info(f"Error: Error in parsing YAML file.")
    if not flag:
        args=None
    return args

if __name__ == '__main__':
    
    add_argument('--zotero_id', type=str,  help='Zotero user ID')
    add_argument('--zotero_key', type=str, help='Zotero API key')
    add_argument('--zotero_ignore',type=str,help='Zotero collection to ignore, using gitignore-style pattern.')
    add_argument('--send_empty', type=bool, help='If get no arxiv paper, send empty email')
    add_argument('--max_paper_num', type=int, help='Maximum number of papers to recommend')
    add_argument('--max_keywords', type=int, help='Maximum number of keywords')
    add_argument('--domain', type=str, help='Arxiv search query')
    add_argument('--arxiv_query', type=str, help='Arxiv search query')
    add_argument('--smtp_server', type=str, help='SMTP server')
    add_argument('--smtp_port', type=int, help='SMTP port')
    add_argument('--sender', type=str, help='Sender email address')
    add_argument('--receiver', type=str, help='Receiver email address')
    add_argument('--sender_password', type=str, help='Sender email password')
    add_argument('--use_llm_keywords', type=bool, help='Whether to use LLM to generate recommended keywords')
    add_argument('--use_coarse_grained_recommendation', type=bool, help='Whether to use coarse grained recommendation')
    
    add_argument(
        "--use_llm_api",
        type=bool,
        help="Use OpenAI API to generate TLDR",
    )
    add_argument(
        "--openai_api_key",
        type=str,
        help="OpenAI API key",
    )
    add_argument(
        "--openai_api_base",
        type=str,
        help="OpenAI API base URL",
    )
    add_argument(
        "--model_name",
        type=str,
        help="LLM Model Name",
    )
    add_argument(
        "--language",
        type=str,
        help="Language of TLDR",
    )
    parser.add_argument('--debug', action='store_true', help='Debug mode')
    args = parser.parse_args()
    assert (
        not args.use_llm_api or args.openai_api_key is not None
    )  # If use_llm_api is True, openai_api_key must be provided
    if args.debug:
        logger.remove()
        logger.add(sys.stdout, level="DEBUG")
        logger.debug("Debug mode is on.")
    else:
        logger.remove()
        logger.add(sys.stdout, level="INFO")

    args = update_args(args)
    if args is None:
        logger.info("Lack of key configuration")
        exit(0)
    # starting
    logger.info("Retrieving Zotero corpus...")
    corpus = get_zotero_corpus(args.zotero_id, args.zotero_key)
    logger.info(f"Retrieved {len(corpus)} papers from Zotero.")
    # if args.zotero_ignore:
    #     logger.info(f"Ignoring papers in:\n {args.zotero_ignore}...")
    #     # corpus = filter_corpus(corpus, args.zotero_ignore)
    #     corpus = choose_corpus(corpus)
    #     logger.info(f"Remaining {len(corpus)} papers after filtering.")
    # # ending
    # corpus = choose_corpus(corpus)
    if args.use_llm_api:
        set_global_llm(api_key=args.openai_api_key, base_url=args.openai_api_base, model=args.model_name,
                    lang=args.language)
    logger.info("Generate Keywords...")
    

    logger.info("Retrieving Arxiv papers...")

    papers = get_arxiv_paper(args.arxiv_query, args.debug, max_results=args.max_paper_num)
    if args.use_llm_keywords:
        keywords = generate_search_keywords(corpus)
        query = build_arxiv_query(keywords, args.max_keywords)
        papers += get_arxiv_paper(query, args.debug, max_results=args.max_paper_num)
        unique_papers_dict = {paper.title: paper for paper in papers}
        papers = list(unique_papers_dict.values())

    if args.use_coarse_grained_recommendation:
        papers_coarse = get_arxiv_paper(args.domain, args.debug, max_results=args.max_paper_num)

    if len(papers) == 0:
        logger.info("No new papers found. Yesterday maybe a holiday and no one submit their work :). If this is not the case, please check the ARXIV_QUERY.")
        if not args.send_empty:
            exit(0)
    else:
        logger.info("Reranking papers...")
        papers = rerank_paper(papers, corpus)
        papers_coarse = rerank_paper(papers_coarse, corpus)
        if args.max_paper_num != -1:
            papers = papers[:args.max_paper_num]
            papers_coarse = papers_coarse[:2]
        # if args.use_llm_api:
        #     logger.info("Using OpenAI API as global LLM.")
        #     set_global_llm(api_key=args.openai_api_key, base_url=args.openai_api_base, model=args.model_name, lang=args.language)
        # else:
        #     logger.info("Using Local LLM as global LLM.")
        #     set_global_llm(lang=args.language)
    # 测试标签是否生成
    if args.debug:
        for paper in papers:
            logger.debug(f"Paper: {paper.title}")
            logger.debug(f"Labels: {paper.labels}")
    # end
    html = render_email(papers, papers_coarse = papers_coarse)
    logger.info("Sending email...")

    send_email(args.sender, args.receiver, args.sender_password, args.smtp_server, args.smtp_port, html)
    logger.success("Email sent successfully! If you don't receive the email, please check the configuration and the junk box.")
