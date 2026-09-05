# -*- coding: utf-8 -*-
"""
step1_clean_csmar.py —— 读取并清洗 CSMAR 下载的 A 股公司名单
=============================================================

输入 : ../data/raw/STK_LISTEDCOINFOANL.csv （CSMAR 上市公司基本信息，UTF-8）
输出 : ../data/a_share_list.csv（仅两列：代码、名称，编码 UTF-8-sig）

数据说明（已对原始文件核对确认）：
  * 该文件是「年度快照面板」：同一只股票（Symbol）在多个年份各占一行
    （EndDate = 各年 12-31），共 74,354 行 / 5,850 个唯一代码，并非一行一股，
    因此必须按股票代码去重。
  * 实际列名：Symbol, ShortName, FullName, EndDate, IndustryName,
    EstablishDate, Crcd, Website, EMAIL, LISTINGDATE, LISTINGSTATE 等。
  * LISTINGDATE（上市日期）对同一代码几乎恒定不变（仅个别因重组换代码而变），
    所以「按上市日期最早保留」≈「保留文件第一条（最早一期快照）」，其结果会让
    名称停留在旧年份（例如 000001 会显示旧简称「深发展A」而非「平安银行」）。

清洗策略（已按你确认的方案设置，可用下方常量随时切换）：
  1. 按股票代码去重；
  2. 默认保留每个代码「最新一期」（EndDate 最大）快照：
     名称、上市状态均为最新年份，便于下一步剔除 ST / 退市、匹配公司仓库；
  3. 输出仅两列：代码（统一改名为「代码」）、名称（默认取证券简称 ShortName）。
"""

import os

import pandas as pd

# ---------------------------------------------------------------------------
# 0. 路径与关键常量
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # 本脚本所在目录 scripts/
PROJECT_ROOT = os.path.dirname(BASE_DIR)                        # 项目根目录
RAW_FILE = os.path.join(PROJECT_ROOT, "data", "raw", "STK_LISTEDCOINFOANL.csv")
OUT_FILE = os.path.join(PROJECT_ROOT, "data", "a_share_list.csv")

# 读取时依次尝试的编码（CSMAR 文件通常是 UTF-8，此处留足备选以保证鲁棒性）
ENCODINGS = ["utf-8-sig", "utf-8", "gb18030", "gbk"]

# 各关键列的名称候选（按优先级排列，忽略不存在者；大小写不敏感匹配）
CODE_CANDIDATES    = ["Stkcd", "Symbol", "证券代码", "股票代码", "代码"]          # 股票代码
SHORTNAME_CANDIDATES = ["ShortName", "证券简称", "简称", "名称"]                  # 证券简称
FULLNAME_CANDIDATES = ["FullName", "公司全称", "公司名称"]                        # 公司全称（备用）
LISTDATE_CANDIDATES = ["LISTINGDATE", "Listdate", "IPOdate", "IPODate", "上市日期"]  # 上市日期
SNAPSHOT_CANDIDATES = ["EndDate", "报告期", "年度", "SnapshotDate"]               # 快照/报告期
STATE_CANDIDATES    = ["LISTINGSTATE", "ListingState", "上市状态", "状态"]        # 上市状态（仅用于可选过滤）

# ---- 清洗模式（可按需修改后重跑）----
DEDUPE_MODE = "latest"   # 去重保留哪一期快照：
                         #   "latest"          保留每个代码最新一期（EndDate 最大），默认推荐
                         #   "earliest"        保留最早一期快照（≈原任务“最早上市日期/第一条”语义）
NAME_SOURCE = "short"    # 名称取哪一列：
                         #   "short"  证券简称 ShortName（如“平安银行”），默认推荐
                         #   "full"   公司全称 FullName（如“平安银行股份有限公司”）


def pick_col(df: pd.DataFrame, candidates: list) -> str:
    """从候选列表中返回第一个真实存在的列名（先精确匹配，再大小写不敏感匹配），找不到返回 None。"""
    for cand in candidates:
        if cand in df.columns:
            return cand
    upper_columns = {str(c).strip().upper(): c for c in df.columns}
    for cand in candidates:
        hit = upper_columns.get(str(cand).strip().upper())
        if hit is not None:
            return hit
    return None


def read_csv_robust(path: str) -> tuple:
    """依次尝试多种编码读取 CSV（dtype=str 防止股票代码前导 0 被吞掉）。"""
    last_err = None
    for enc in ENCODINGS:
        try:
            df = pd.read_csv(path, dtype=str, encoding=enc)
            print(f"[读取] 成功，使用编码 {enc}")
            return df, enc
        except UnicodeDecodeError as e:  # 编码不对时换下一种
            last_err = e
            print(f"[读取] 编码 {enc} 失败（{e}），尝试下一种…")
    raise RuntimeError(f"无法用 {ENCODINGS} 中的任一种编码读取文件 {path}，最后错误：{last_err}")


def normalize_code(s) -> str:
    """股票代码规整：去空白；纯数字且不足 6 位时补足前导 0（如 '1' -> '000001'）。"""
    if s is None:
        return ""
    s = str(s).strip()
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def main() -> None:
    # ------------------------------------------------------------------
    # 1. 读取文件（自动探测编码）
    # ------------------------------------------------------------------
    print("=" * 70)
    print("[步骤1] 读取原始 CSV")
    print("=" * 70)
    if not os.path.exists(RAW_FILE):
        raise FileNotFoundError(f"原始文件不存在：{RAW_FILE}")
    df, used_enc = read_csv_robust(RAW_FILE)
    print(f"[读取] 文件路径：{RAW_FILE}")

    # ------------------------------------------------------------------
    # 2. 探索数据：打印全部列名 / 行数 / 前 5 行
    # ------------------------------------------------------------------
    print("-" * 70)
    print("[探索] 全部列名如下：")
    for i, col in enumerate(df.columns):
        print(f"    {i}: {col}")
    print(f"[探索] 数据总行数：{len(df):,}")
    print("[探索] 前 5 行样例：")
    print(df.head(5).to_string(index=False))

    # ------------------------------------------------------------------
    # 3. 识别关键列（异常处理：缺少必选列时给出清晰报错，而不是中途崩溃）
    # ------------------------------------------------------------------
    code_col = pick_col(df, CODE_CANDIDATES)
    if code_col is None:
        raise KeyError("未找到股票代码列！请确认候选 CODE_CANDIDATES 是否包含实际列名（见上面打印的列名）。")
    print(f"[清洗] 股票代码列 → {code_col}")

    name_col = None
    if NAME_SOURCE == "full":
        name_col = pick_col(df, FULLNAME_CANDIDATES) or pick_col(df, SHORTNAME_CANDIDATES)
        print("[清洗] 名称来源 → 公司全称（FullName 优先）")
    else:
        name_col = pick_col(df, SHORTNAME_CANDIDATES) or pick_col(df, FULLNAME_CANDIDATES)
        print("[清洗] 名称来源 → 证券简称（ShortName 优先）")
    if name_col is None:
        raise KeyError("未找到公司名称列！请确认 SHORTNAME_CANDIDATES / FULLNAME_CANDIDATES 是否包含实际列名。")

    listdate_col = pick_col(df, LISTDATE_CANDIDATES)   # 上市日期（本次仅作信息展示）
    snapshot_col = pick_col(df, SNAPSHOT_CANDIDATES)   # 快照/报告期（用于“最新一期”判定）
    state_col    = pick_col(df, STATE_CANDIDATES)      # 上市状态（本次仅作信息展示/可选过滤）

    print(f"[清洗] 上市日期列 → {listdate_col}")
    print(f"[清洗] 快照/报告期列 → {snapshot_col}")
    print(f"[清洗] 上市状态列 → {state_col}")

    # 统一股票代码格式（去空白、补前导 0），并剔除代码为空的行
    df = df.copy()
    df[code_col] = df[code_col].map(normalize_code)
    df = df[df[code_col] != ""]
    print(f"[清洗] 剔除空代码行后行数：{len(df):,}")

    # ------------------------------------------------------------------
    # 4. 按股票代码去重
    # ------------------------------------------------------------------
    print("-" * 70)
    print(f"[清洗] 去重模式：{DEDUPE_MODE}")

    unique_before = df[code_col].nunique()

    if DEDUPE_MODE == "latest" and snapshot_col is not None:
        # 最新一期：按 代码 + 快照日期 升序排列，每组保留最后一行（EndDate 最大）
        df["__sort__"] = pd.to_datetime(df[snapshot_col], errors="coerce")
        df = df.sort_values([code_col, "__sort__"], kind="stable")
        df = df.drop_duplicates(subset=[code_col], keep="last")
        df = df.drop(columns=["__sort__"])
        print(f"[清洗] 按 {snapshot_col} 保留每个代码最新一期快照")
    elif DEDUPE_MODE == "earliest" and listdate_col is not None:
        # 最早一期（原任务语义：按上市日期最早保留，等价于保留文件首条）
        df["__sort__"] = pd.to_datetime(df[listdate_col], errors="coerce")
        df = df.sort_values([code_col, "__sort__"], kind="stable")
        df = df.drop_duplicates(subset=[code_col], keep="first")
        df = df.drop(columns=["__sort__"])
        print(f"[清洗] 按 {listdate_col} 保留上市日期最早的一条")
    else:
        # 既无快照列也无上市日期列时，直接保留文件中的第一条记录
        df = df.drop_duplicates(subset=[code_col], keep="first")
        print("[清洗] 无可用日期列，直接保留每个代码在文件中的第一条记录")

    print(f"[清洗] 去重前唯一代码数：{unique_before:,}，去重后行数：{len(df):,}")

    # ------------------------------------------------------------------
    # 5.（可选）剔除 ST / 退市等过滤逻辑 —— 默认注释关闭，确认列名后可按需启用
    #    注意：只有在上方选择“latest（最新一期）”模式时，state_col 才代表股票当前状态；
    #    若保留的是最早一期快照，该状态字段不能用于判断是否退市。
    # ------------------------------------------------------------------
    # if state_col is not None:
    #     # 只保留“正常上市”（可自行加入其他允许的状态）
    #     allowed_states = ["正常上市"]
    #     df = df[df[state_col].isin(allowed_states)]
    #     print(f"[过滤] 按 {state_col} ∈ {allowed_states} 过滤后行数：{len(df):,}")
    #
    # if name_col is not None:
    #     # 同时剔除名称带 ST / *ST / 退 前缀的股票（可选）
    #     mask_st = df[name_col].astype(str).str.upper().str.startswith(("ST", "*ST", "S", "N", "退"))
    #     df = df[~mask_st]
    #     print(f"[过滤] 剔除名称带 ST/*ST/退 标记后行数：{len(df):,}")

    # ------------------------------------------------------------------
    # 6. 只保留 代码、名称 两列并统一列名
    # ------------------------------------------------------------------
    result = df[[code_col, name_col]].copy()
    result.columns = ["代码", "名称"]
    result = result.dropna(subset=["名称"])
    result["名称"] = result["名称"].astype(str).str.strip()
    result = result[result["名称"] != ""]
    result = result.reset_index(drop=True)

    print("-" * 70)
    print(f"[输出] 最终记录数：{len(result):,}（唯一股票代码 {result['代码'].nunique():,}）")
    print("[输出] 代码前缀分布（前 2 位，供判断板块，B股/北交所是否保留请自行决定）：")
    print(result["代码"].str[:2].value_counts().sort_index().to_string())

    # ------------------------------------------------------------------
    # 7. 保存结果：UTF-8-sig（带 BOM，Excel 可直接打开不乱码）
    # ------------------------------------------------------------------
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    result.to_csv(OUT_FILE, index=False, encoding="utf-8-sig")
    print(f"[输出] 已保存：{OUT_FILE}")
    print(f"[输出] 预览前 5 行：")
    print(result.head(5).to_string(index=False))
    print("=" * 70)
    print("完成！请核对上方打印的列名 / 行数是否符合预期，再决定是否启用第 5 步的过滤。")
    print("=" * 70)


if __name__ == "__main__":
    main()
