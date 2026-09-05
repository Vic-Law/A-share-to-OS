# -*- coding: utf-8 -*-
"""
step2_search_github_org.py —— 基于 A 股公司名单搜索 GitHub 组织
================================================================

输入 : ../data/a_share_list.csv（代码/名称，由 step1 生成）
        ../data/raw/STK_LISTEDCOINFOANL.csv（用于关联 Website / EMAIL 字段）
输出 : ../data/github_org_candidates.json（JSON 数组）

对每家公司：
  1. 用 PyGithub 搜索 "<公司名称> type:org"，取前 5 个候选组织；
  2. 逐个获取候选组织的 name / blog（官网）/ html_url；
  3. 判定置信度：
       - blog 包含公司主域名            -> confidence: "high"（自动确认）
       - 名称完全匹配但官网不含域名      -> confidence: "medium"
       - 名称模糊匹配                   -> confidence: "low"
  4. 结果：
       - 存在 high -> candidates 只保留这一个，confirmed: true
       - 否则保留最多 3 个 medium/low，confirmed: false
       - 无任何候选 -> candidates 为空数组，confirmed: false

技术要点：
  * 请求间隔 SLEEP_SECONDS 秒/次（含搜索与组织详情请求）；
  * TEST_MODE = True 时仅处理前 100 家公司（请自行改为 False 跑全量）；
  * RESUME = True 时自动续跑：已存在于输出文件中的代码会跳过，进度不会因中断丢失；
  * 单家公司失败不影响整体；每 10 家打印一次进度；
  * 域名解析失败时 domain 置为 null。

注意：脚本不会打印 Token 本身，运行需要网络与 GitHub 配额
      （search API 认证用户 30 次/分钟，自动退避等待）。
"""

import json
import os
import re
import sys
import time
import urllib.parse

import pandas as pd
try:
    from pypinyin import pinyin, Style
    HAS_PYPINYIN = True
except ImportError:
    HAS_PYPINYIN = False
    print("[警告] pypinyin 未安装，拼音匹配功能不可用。建议安装：pip install pypinyin")

# ---------------------------------------------------------------------------
# 0. 常量与路径
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))           # scripts/
PROJECT_ROOT = os.path.dirname(BASE_DIR)                         # 项目根目录
LIST_FILE = os.path.join(PROJECT_ROOT, "data", "a_share_list.csv")
RAW_FILE = os.path.join(PROJECT_ROOT, "data", "raw", "STK_LISTEDCOINFOANL.csv")
OUT_FILE = os.path.join(PROJECT_ROOT, "data", "github_org_candidates.json")

TEST_MODE = True          # True: 只处理前 LIMIT_TEST 家公司（便于快速验证）
LIMIT_TEST = 100          # 测试模式下处理的公司数
RESUME = True             # True: 跳过输出文件中已有的代码，支持断点续跑
SLEEP_SECONDS = 2.0       # GitHub API 请求间隔（秒/次）
KEEP_CANDIDATES = 3       # 无 high 匹配时最多保留的中/低置信度候选数
MAX_CANDIDATES = 5        # 每次搜索取前几个候选组织

# 需要从 config/config_local.py 导入的 Token
CONFIG_MODULE = "config.config_local"


# ---------------------------------------------------------------------------
# 1. 纯函数工具：域名解析 / 名称匹配
# ---------------------------------------------------------------------------
def _clean_host(host: str):
    """规整主机名：去空白/尾部点、剥离端口与用户信息、去掉常见 www 前缀。"""
    if not host:
        return None
    host = host.strip().lower().rstrip(".")
    if not host:
        return None
    if "@" in host:                       # 去掉 userinfo@host 形式
        host = host.rsplit("@", 1)[1]
    if ":" in host:                       # 去掉 :port
        host = host.split(":", 1)[0]
    if host.startswith("www."):           # www.abc.com.cn -> abc.com.cn
        host = host[4:]
    host = host.strip().rstrip(".")
    if not host or "." not in host:       # 必须是域名（含至少一个点）
        return None
    if not re.fullmatch(r"[a-z0-9\-\.]+", host):   # 只保留正常域名字符
        return None
    return host


def _domain_from_url(url):
    """从官网 URL 中解析主机名（可无 scheme，自动补 http://）。"""
    if url is None or (isinstance(url, float) and pd.isna(url)):
        return None
    s = str(url).strip().lower()
    if not s or s == "nan":
        return None
    if "://" not in s:
        s = "http://" + s
    try:
        parsed = urllib.parse.urlparse(s)
    except ValueError:
        return None
    host = parsed.netloc or parsed.path.split("/")[0]
    return _clean_host(host)


def _domain_from_email(email):
    """从 Email 中解析出 @ 后面的域名。"""
    if email is None or (isinstance(email, float) and pd.isna(email)):
        return None
    s = str(email).strip().lower()
    if not s or s == "nan":
        return None
    # 兼容一个单元格里塞多个邮箱（; 或 , 分隔）的情况
    first = re.split(r"[;,，；\s]+", s)[0]
    if "@" not in first:
        return None
    return _clean_host(first.rsplit("@", 1)[1])


def extract_main_domain(website, email=None):
    """提取公司主域名：优先取官网域名，官网缺失/无效时退回 Email 域名；
    两者都失败返回 None（输出中 domain 为 null）。"""
    d = _domain_from_url(website)
    if d:
        return d
    if email:
        return _domain_from_email(email)
    return None


def _norm_text(s):
    """名称规整：小写、去首尾空白、合并连续空白。"""
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _tokens(s):
    """切分为 token：连续英文/数字一段、连续汉字一段。"""
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", _norm_text(s))


def name_match_score(company_name, org_name, org_login):
    """名称匹配打分：返回 'exact' / 'fuzzy' / None。

    exact: 公司名与组织 name 或 login 完全相等；
    fuzzy: 一侧包含另一侧（较长者长度足够），或 token 重合度较高。
    """
    comp = _norm_text(company_name)
    if not comp:
        return None
    for cand in (org_name, org_login):
        if not cand:
            continue
        cand_n = _norm_text(cand)
        if not cand_n:
            continue
        if cand_n == comp:
            return "exact"
    # 拼音匹配（如果公司名是中文，生成拼音并与组织名比较）
    if HAS_PYPINYIN:
        pinyin_list = pinyin(comp, style=Style.NORMAL, heteronym=False)
        pinyin_str = "".join([p[0] for p in pinyin_list])
        if pinyin_str:
            for cand in (org_name, org_login):
                if not cand:
                    continue
                cand_l = _norm_text(cand)
                if pinyin_str in cand_l or cand_l in pinyin_str:
                    return "fuzzy"
                # 首字母缩写，如 "lxkj"
                initials = "".join([p[0][0] for p in pinyin_list if p[0]])
                if initials and (initials in cand_l or cand_l in initials):
                    return "fuzzy"
    # token 重合度
    comp_tokens = set(_tokens(company_name))
    for cand in (org_name, org_login):
        cand_tokens = set(_tokens(cand))
        if not comp_tokens or not cand_tokens:
            continue
        inter = len(comp_tokens & cand_tokens)
        if inter > 0 and 2.0 * inter / (len(comp_tokens) + len(cand_tokens)) >= 0.5:
            return "fuzzy"
    # 包含关系：公司名显著包含于组织名/登录名，或反之
    comp_l = _norm_text(company_name)
    for cand in (org_name, org_login):
        cand_l = _norm_text(cand)
        if not cand_l:
            continue
        if comp_l in cand_l and len(comp_l) >= 3:
            return "fuzzy"
        if cand_l in comp_l and len(cand_l) >= 3:
            return "fuzzy"
    return None


def sanitize_query(name):
    """清理搜索词：去掉 GitHub 搜索语法中的特殊字符（如 * 是通配符），
    保留中文/字母/数字/常见标点，避免 400 语法错误。"""
    s = re.sub(r"[^\w\u4e00-\u9fff.\-()% ]+", " ", str(name))
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------------
# 2. GitHub API 封装（带节流与自动退避）
# ---------------------------------------------------------------------------
def _github():
    """导入并读取 GITHUB_TOKEN，返回 Github 客户端（绝不打印 Token 内容）。"""
    sys.path.insert(0, PROJECT_ROOT)     # 使 config 包可导入
    try:
        module = __import__(CONFIG_MODULE, fromlist=["GITHUB_TOKEN"])
    except Exception as e:               # noqa: BLE001
        raise RuntimeError(f"无法导入 {CONFIG_MODULE}：{e}") from e
    token = getattr(module, "GITHUB_TOKEN", None)
    if not token:
        raise RuntimeError("config/config_local.py 中未找到非空的 GITHUB_TOKEN。")
    from github import Github
    return Github(token, per_page=100)


def gh_call(fn, *args, **kwargs):
    """执行一次 GitHub API 请求：
    - 成功后 sleep SLEEP_SECONDS（节流 2 秒/次）；
    - 命中速率限制（RateLimit / 403 / 429）时退避 60 秒重试；
    - 重试 6 次仍失败则抛出，由外层按公司捕获。"""
    from github import GithubException, RateLimitExceededException

    for attempt in range(6):
        try:
            result = fn(*args, **kwargs)
            if SLEEP_SECONDS > 0:
                time.sleep(SLEEP_SECONDS)
            return result
        except RateLimitExceededException:
            print(f"[限流] 触发速率限制，退避 60 秒后重试（第 {attempt + 1} 次）…")
            time.sleep(60)
        except GithubException as e:
            if e.status in (403, 429, 502, 503):   # 次生限流/临时故障同样退避
                print(f"[限流] HTTP {e.status}，退避 60 秒后重试（第 {attempt + 1} 次）…")
                time.sleep(60)
            else:
                raise
    raise RuntimeError("GitHub API 请求连续 6 次失败（速率限制）")


# ---------------------------------------------------------------------------
# 3. 数据处理
# ---------------------------------------------------------------------------
def load_company_records():
    """读取公司名单并与原始 CSMAR（最新一期快照）关联出 Website / EMAIL。

    返回 [{code, name, website, email, domain}]。"""
    # 3.1 干净名单（step1 输出）
    listing = pd.read_csv(LIST_FILE, dtype=str)
    listing = listing.rename(columns={"代码": "code", "名称": "name"})
    listing["code"] = listing["code"].astype(str).str.strip()

    # 3.2 原始 CSMAR：每个代码保留最新一期（EndDate 最大）快照，取 Website/EMAIL
    raw = pd.read_csv(RAW_FILE, dtype=str, usecols=["Symbol", "EndDate", "Website", "EMAIL"])
    raw["Symbol"] = raw["Symbol"].astype(str).str.strip()
    raw = raw.sort_values("EndDate", kind="stable")          # 字符串 ISO 日期可直接比较
    raw = raw.drop_duplicates(subset=["Symbol"], keep="last")  # 每组留 EndDate 最大行
    raw = raw.rename(columns={"Symbol": "code", "Website": "website", "EMAIL": "email"})

    merged = listing.merge(raw[["code", "website", "email"]], on="code", how="left")

    records = []
    for _, row in merged.iterrows():
        code = str(row["code"]).strip()
        if not code:
            continue
        website = row.get("website")
        email = row.get("email")
        # 域名解析失败时 domain 为 None（JSON 中序列化为 null）
        domain = extract_main_domain(website, email)
        records.append({
            "code": code,
            "name": str(row["name"]).strip(),
            "website": None if _is_missing(website) else str(website).strip(),
            "email": None if _is_missing(email) else str(email).strip(),
            "domain": domain,
        })
    return records


def _is_missing(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip().lower() in ("", "nan")


# ---------------------------------------------------------------------------
# 4. 单家公司：搜索 GitHub 组织并打分
# ---------------------------------------------------------------------------
# def search_org_candidates(g, company, org_cache):
#     """对一家公司执行搜索与验证，返回 (record, 出错信息或 None)。"""
#     code, name, domain = company["code"], company["name"], company["domain"]
#
#     query = sanitize_query(name)
#     if not query:                       # 名称清洗后为空（全特殊字符）
#         raise ValueError(f"公司名无法构造搜索词：{name!r}")
#
#     # 4.1 搜索 "<公司名称> type:org"，取前 MAX_CANDIDATES 个
#     #     注意：只取第 0 页（get_page(0) 一次搜索请求），不要 list(search)——
#     #     那会遍历分页、一次公司消耗多次搜索配额。
#     #已修改：放宽名称，只要仓库描述里有就返回
#     search = gh_call(g.search_users, query, per_page=MAX_CANDIDATES)
#     items = gh_call(search.get_page, 0)
#     print(f"[调试] {code} {name} 搜索返回 {len(items)} 个结果")
#     items = items[:MAX_CANDIDATES]
#
#     # 4.2 逐个候选获取完整信息（name / blog / html_url），带缓存避免重复请求
#     raw_candidates = []
#     for item in items:
#         login = getattr(item, "login", None)
#         if not login:
#             continue
#         if login not in org_cache:
#             org_cache[login] = gh_call(g.get_user, login)
#         org = org_cache[login]
#         if getattr(org, "type", None) != "Organization":
#             continue
#         blog = (getattr(org, "blog", None) or "").strip()
#         raw_candidates.append({
#             "org_name": getattr(org, "name", None) or login,
#             "login": login,
#             "url": getattr(org, "html_url", None) or f"https://github.com/{login}",
#             "blog": blog or None,
#             "org": org,
#         })
#
#     # 4.3 打分：high / medium / low
#     scored = []
#     for rc in raw_candidates:
#         conf, matched = score_org(name, domain, rc["org_name"], rc["login"], rc["blog"])
#         if conf is None:
#             continue                       # 名称与官网均无关的组织直接丢弃
#         scored.append({
#             "org_name": rc["org_name"],
#             "url": rc["url"],
#             "blog": rc["blog"],
#             "confidence": conf,
#             "matched_by_domain": matched,
#         })
#
#     # 4.4 汇总：high 只留 1 个并确认；否则保留最多 KEEP_CANDIDATES 个中/低
#     # highs = [c for c in scored if c["confidence"] == "high"]
#     # if highs:
#     #     confirmed, candidates = True, highs[:1]
#     # else:
#     #     confirmed = False
#     #     order = {"medium": 0, "low": 1}
#     #     ranked = sorted(scored, key=lambda c: (order.get(c["confidence"], 9), scored.index(c)))
#     #     candidates = ranked[:KEEP_CANDIDATES]
#     # 4.4 汇总：所有候选按置信度排序保留，不强制要求 high
#     order = {"high": 0, "medium": 1, "low": 2}
#     ranked = sorted(scored, key=lambda c: (order.get(c["confidence"], 9), scored.index(c)))
#     candidates = ranked[:KEEP_CANDIDATES]
#     confirmed = any(c["confidence"] == "high" for c in candidates)
#
#     return {
#         "code": code,
#         "name": name,
#         "domain": domain,
#         "confirmed": confirmed,
#         "candidates": candidates,
#     }, None
def search_org_candidates(g, company, org_cache):
    """
    基于公司域名和股票代码，直接验证候选组织是否存在。
    不使用搜索API，避免中文搜索索引问题。
    """
    code = company["code"]
    name = company["name"]
    domain = company.get("domain")

    # ---- 1. 生成候选组织名列表 ----
    candidates_login = []

    # 1.1 从域名提取：去掉 www.，取主域名前缀
    if domain:
        # 去掉 www. 前缀
        clean_domain = domain.replace("www.", "")
        # 提取主域名部分（如 bank.pingan.com -> pingan）
        parts = clean_domain.split(".")
        if len(parts) >= 2:
            # 主域名通常是倒数第二个部分（如 pingan）
            main_part = parts[-2]
            if main_part and main_part not in candidates_login:
                candidates_login.append(main_part)
            # 如果域名有三级，也加入完整前缀（如 bankpingan）
            if len(parts) >= 3:
                full_prefix = "".join(parts[:-2])
                if full_prefix and full_prefix not in candidates_login:
                    candidates_login.append(full_prefix)
            # 如果主域名是纯英文（去掉数字），也加入
            clean_main = re.sub(r"[^a-z]", "", main_part.lower())
            if clean_main and clean_main not in candidates_login:
                candidates_login.append(clean_main)

    # 1.2 股票代码作为兜底
    if code and code not in candidates_login:
        candidates_login.append(code)

    # 1.3 公司名的拼音首字母（如果有 pypinyin）
    if HAS_PYPINYIN:
        try:
            from pypinyin import pinyin, Style
            pinyins = pinyin(name, style=Style.NORMAL, heteronym=False)
            pinyin_str = "".join([p[0] for p in pinyins])
            if pinyin_str and pinyin_str not in candidates_login:
                candidates_login.append(pinyin_str)
            initials = "".join([p[0][0] for p in pinyins if p[0]])
            if initials and initials not in candidates_login:
                candidates_login.append(initials)
        except Exception:
            pass

    # 1.4 去重，按长度排序（短名优先，通常更可能是组织名）已修正：不再按长度排序，把纯英文的、长度适中的候选排在前面
    candidates_login = list(set(candidates_login))
    candidates_login.sort(key=lambda x: (0 if x.isalpha() and 3 <= len(x) <= 15 else 1, len(x), x))

    # ---- 插入：将域名主词提到最前面 ----
    if domain:
        # 提取主域名（如 espressif.com -> espressif）
        domain_main = domain.replace("www.", "").split(".")[0]
        if domain_main in candidates_login:
            candidates_login.remove(domain_main)
            candidates_login.insert(0, domain_main)

    # 在 candidates_login 生成并去重之后，添加这一行
    # print(f"[候选名] {code} ({name}): {candidates_login}")

    # ---- 2. 逐个验证候选组织是否存在 ----
    for login in candidates_login:
        if not login or len(login) < 2:
            continue

        # 从缓存获取或请求
        if login in org_cache:
            org = org_cache[login]
        else:
            try:
                org = gh_call(g.get_user, login)
                org_cache[login] = org
            except Exception as e:
                # 404 表示用户/组织不存在，继续下一个
                if hasattr(e, 'status') and e.status == 404:
                    continue
                # 其他错误（限流、网络等）打印但继续
                print(f"[警告] 获取 {login} 失败: {e}")
                continue

        # 检查是否为组织类型
        if getattr(org, "type", None) != "Organization":
            continue

        # ---- 3. 命中！获取组织详情 ----
        org_name = getattr(org, "name", None) or login
        blog = getattr(org, "blog", None) or ""
        url = getattr(org, "html_url", None) or f"https://github.com/{login}"

        # 验证域名匹配（用于高置信度判定）
        matched_domain = False
        if domain and blog:
            blog_domain = _domain_from_url(blog)
            if blog_domain:
                matched_domain = (blog_domain == domain) or blog_domain.endswith("." + domain)

        # 获取 followers 数量（可选指标）
        followers = getattr(org, "followers_count", 0)

        # 构建候选结果
        return {
            "code": code,
            "name": name,
            "domain": domain,
            "confirmed": matched_domain,  # 只有域名匹配才算 high
            "candidates": [{
                "org_name": org_name,
                "login": login,
                "url": url,
                "blog": blog,
                "confidence": "high" if matched_domain else "medium",
                "matched_by_domain": matched_domain,
                "followers": followers,
            }],
        }, None

    # ---- 4. 未找到任何组织 ----
    return {
        "code": code,
        "name": name,
        "domain": domain,
        "confirmed": False,
        "candidates": [],
    }, None

def score_org(company_name, company_domain, org_name, org_login, blog):
    """返回 (confidence, matched_by_domain)。

    high 判定：把 blog 解析为主机名，与公司主域名「相等」或「是它的子域」。
    即 org.blog 的主机确实是 company_domain 或其下级域名（如 corp.pingan.com
    对 pingan.com），而不是把域名当普通文本做子串匹配（避免 abc.com 误中
    abc.com.cn 这类误报）。
    """
    matched = False
    if company_domain:
        blog_domain = _domain_from_url(blog)          # 已是小写、去 www
        if blog_domain:
            matched = (blog_domain == company_domain) or blog_domain.endswith("." + company_domain)
    if matched:
        return "high", True
    score = name_match_score(company_name, org_name, org_login)
    if score == "exact":
        return "medium", False
    if score == "fuzzy":
        return "low", False
    return "low", False


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("[step2] 搜索 GitHub 组织")
    print("=" * 70)
    g = _github()
    print("[配置] GitHub 客户端初始化成功（Token 已加载，不做展示）")

    # 5.1 加载公司列表（含 domain）
    records = load_company_records()
    print(f"[数据] 共加载 {len(records):,} 家公司")
    # ---- 临时测试：只保留指定的4家公司 ----
    # target_codes = {"688018", "300339", "300033", "603019"}
    # records = [r for r in records if r["code"] in target_codes]
    # print(f"[测试] 过滤后仅保留 {len(records)} 家目标公司")

    if TEST_MODE:
        records = records[:LIMIT_TEST]
        print(f"[测试模式] TEST_MODE=True：仅处理前 {len(records):,} 家（改顶部 TEST_MODE=False 跑全量）")

    # 5.2 断点续跑：读取已有输出，跳过已完成代码
    done = {}
    if RESUME and os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, "r", encoding="utf-8") as f:
                for rec in json.load(f):
                    done[rec["code"]] = rec
            print(f"[续跑] 输出文件已有 {len(done):,} 条记录，将跳过这些代码")
        except (json.JSONDecodeError, KeyError, TypeError):
            print("[续跑] 输出文件无法解析，将重新开始（可删除该文件后重跑）")

    todo = [r for r in records if r["code"] not in done]
    print(f"[续跑] 本次实际处理 {len(todo):,} 家")
    if not todo:
        print("[续跑] 没有待处理的公司，结束。")
        return

    org_cache = {}                       # login -> NamedUser（跨公司复用，省配额）
    results = [done[r["code"]] for r in records if r["code"] in done]  # 保持输入顺序

    # 5.3 逐家处理
    n_ok, n_confirm, n_cand, n_fail = 0, 0, 0, 0
    start_ts = time.time()
    for idx, company in enumerate(todo, start=1):
        code = company["code"]
        try:
            rec, err = search_org_candidates(g, company, org_cache)
            if err:
                raise RuntimeError(err)
            results.append(rec)
            if rec["confirmed"]:
                n_confirm += 1
            if rec["candidates"]:
                n_cand += 1
            n_ok += 1
        except Exception as e:           # noqa: BLE001 —— 单家公司失败不影响整体
            n_fail += 1
            print(f"[失败] {code} {company['name']}: {type(e).__name__}: {e}")
            results.append({
                "code": code,
                "name": company["name"],
                "domain": company.get("domain"),
                "confirmed": False,
                "candidates": [],
            })

        # 5.4 进度与阶段性落盘（每 10 家）
        if idx % 10 == 0 or idx == len(todo):
            elapsed = time.time() - start_ts
            speed = idx / elapsed if elapsed > 0 else 0
            eta = (len(todo) - idx) / speed / 60 if speed > 0 else float("nan")
            print(f"[进度] {idx:,}/{len(todo):,} | 命中确认 {n_confirm} | "
                  f"有候选 {n_cand} | 失败 {n_fail} | 用时 {elapsed / 60:.1f} 分 | "
                  f"预计剩余 {eta:.1f} 分")
            _dump_results(results)

    # 5.5 汇总并保存最终结果（JSON 数组，UTF-8，保留中文）
    _dump_results(results)
    print("=" * 70)
    print(f"[完成] 处理 {len(todo):,} 家：成功 {n_ok:,}，失败 {n_fail:,}")
    print(f"[完成] high 自动确认 {n_confirm:,} 家，含候选 {n_cand:,} 家")
    print(f"[完成] 结果已保存：{OUT_FILE}")
    print("=" * 70)


def _dump_results(results):
    """把结果写为 JSON 数组（UTF-8，ensure_ascii=False 保留中文）。"""
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
