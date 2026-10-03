# 药品法规知识库 · Drug Regulation Knowledge Hub

面向药品注册、药物警戒与药事管理从业者的**本地法规检索与版本追踪**工具。

把散落在政府网站上的药品法规、公告、指导原则汇总为一个可全文检索、可按 ICH/CTD
归类、可追踪修订历史的本地知识库。**纯 Python 标准库实现，零第三方依赖，一个
SQLite 文件即全部数据。**

---

## 解决什么问题

做注册和药事管理，最耗时的不是理解法规，而是**找到并确认法规**：

- 某条要求出自哪份文件？现行有效还是已被替代？
- 某个 ICH 指导原则在国内是否已适用？对应的公告文号是什么？
- 三年前那份公告和现在的版本差在哪？
- 手上有一批从各处下载的公告 PDF，怎么统一检索？

通用搜索引擎做不到「按发文字号精确检索」「只看现行有效」「比对两个版本」。
本项目把这件事变成一个可以离线运行、数据完全自持的库。

---

## 功能

| 功能 | 说明 |
|---|---|
| **全文检索** | SQLite FTS5 trigram 索引；中文两字词自动回退 LIKE，不会出现「搜不到」 |
| **检索语法** | `药品注册 -化妆品`（排除）、`"精确短语"`、多词隐式 AND |
| **ICH / CTD 自动归类** | 按 ICH 编号（Q1–Q12、S1–S11、E1–E14、M1–M8）与 CTD 模块归类 |
| **主题分类** | 药品注册 / 药物警戒 / 药品生产 / 价格与采购 / 中医药 等 18 类，带标题级排除规则 |
| **发文字号归一化** | `药监局公告2021年第32号` → `国药监〔2021〕32号`，同一文件只有一个标识 |
| **版本追踪与 diff** | 文件内容变化时自动归档旧版本，可输出 unified diff |
| **效力状态推断** | 从正文尾部识别「自…起施行」/「起废止」，保守标注，不确定即 `unknown` |
| **多源采集** | 中国政府网政策文件库、国家药典委员会；NMPA/CDE 材料走离线导入 |
| **本地导入** | HTML / PDF / TXT / Markdown 拖入即入库，保留来源路径便于审计 |
| **Web 界面** | 零依赖 `http.server` 实现，支持分面筛选与文档阅读 |

---

## 数据来源与合规说明

本项目的采集范围经过刻意限制：

| 来源 | 状态 | 说明 |
|---|---|---|
| **中国政府网政策文件库** `gov.cn` | ✅ 自动采集 | 国务院政策文件库的公开 JSON 检索接口 + 服务端渲染的文章页 |
| **国家药典委员会** `chp.org.cn` | ✅ 自动采集 | 服务端渲染页面 |
| **NMPA / CDE / 卫健委** | ⛔ 不做自动采集 | 见下 |
| **本地文件** | ✅ 离线导入 | 你自己另存的公告，支持 HTML / PDF / TXT / MD |

> **关于 NMPA、CDE、卫健委**
>
> `nmpa.gov.cn`、`cde.org.cn`、`nhc.gov.cn` 均部署了 JavaScript 反爬验证
> （阿里云盾 WAF），对普通 HTTP 客户端返回 HTTP 412/202 与一段混淆的挑战脚本，
> 必须执行该脚本生成签名 Cookie 才能访问。
>
> **本项目不实现绕过该保护机制。** 绕过网站明确部署的安全控制，既违背站点所有者
> 的意愿，也不适合出现在一个以合规为主题的求职作品集中。这些机构的文件通过
> **离线导入**进入知识库 —— 你在浏览器中正常打开并另存，再用 `import` 命令入库，
> 这是完全合规且可审计的路径。

离线导入会记录来源文件路径（`imported_from`），保证每条数据的出处可追溯。

---

## 快速开始

无需安装任何依赖，只需要 Python 3.9+。

```bash
# 1. 采集中国政府网药品相关法规（默认 10 组关键词）
python regkb.py crawl

# 2. 采集国家药典委员会公告
python regkb.py crawl-chp

# 3. 导入你本地保存的公告（HTML / PDF / TXT / MD 均可，支持整个目录）
python regkb.py import D:\我的法规资料

# 4. 检索
python regkb.py search 药物警戒
python regkb.py search "药品注册 -化妆品"
python regkb.py search 集采 --year-from 2020

# 5. 查看分面统计
python regkb.py facets

# 6. 读全文 / 比对版本
python regkb.py show 11
python regkb.py diff 11

# 7. 启动 Web 界面
python webui.py            # → http://127.0.0.1:8765
```

### 常用参数

```bash
python regkb.py crawl --keyword 药品注册 --keyword 药物警戒 \
                      --pages 3 --page-size 20 --delay 1.5
python regkb.py search 中药 --tag 中医药 --agency 国家药品监督管理局 \
                      --status in_force --json
python regkb.py reindex              # 规则更新后重新分类，无需重新采集
```

---

## 检索语法

| 输入 | 含义 |
|---|---|
| `药品注册` | 同时包含「药品注册」 |
| `药品注册 化妆品` | 两个词都包含（隐式 AND） |
| `药品注册 -化妆品` | 包含前者，排除后者 |
| `-化妆品` | 排除含「化妆品」的文件（纯排除查询） |
| `"药品上市许可持有人"` | 精确短语 |

**关于中文检索的一个实现细节**：本库用 FTS5 的 `trigram` 分词器建立索引。
它对中文子串匹配的效果符合中文读者直觉，但**要求查询词至少 3 个字符** ——
两字词（`药品`、`注册`、`医保`）在 FTS 中恒为 0 结果。因此本项目对两字词自动
回退到 `LIKE` 查询，保证这类最常见的检索不会静默失败。

---

## 设计要点

**为什么用规则分类而不是模型？** 注册专员需要知道某份文件**为什么**被归到某一类。
关键词表是可审计、可复现、可解释的；不透明的分类器三者皆无。每一个标签都能追溯到
正文中的字面匹配。

**为什么保留历史版本？** 重复采集时静默覆盖正文，恰好会摧毁合规用户最关心的信息 ——
法规什么时候改了什么。

**为什么效力状态标注保守？** 把一份现行有效的法规误标为"已废止"，比留成
`unknown` 危害大得多，所以只在正文尾部出现明确的废止表述时才标注。

**为什么排除条件用 SQL 而非 FTS 的 `NOT`？** FTS5 的 `NOT` 必须有正向词才能
成立，纯排除查询（`-化妆品`）会静默返回 0 条。排除统一用 SQL 谓词实现，并且
必须包 `COALESCE` —— `doc_number NOT LIKE ?` 在 `doc_number` 为 NULL 时结果是
NULL，而 `NULL AND x` 永不为真，会悄悄丢掉所有无发文字号的文件。这两点都有
回归测试覆盖。

---

## 项目结构

```
NMPARegHub/
├── regkb.py               # CLI 入口
├── webui.py               # 零依赖 Web 界面
├── src/
│   ├── db.py              # SQLite 模式、FTS5 索引与触发器
│   ├── classify.py        # ICH/CTD/主题归类、发文字号归一化、效力推断
│   ├── fetchers.py        # gov.cn 与药典委采集器
│   ├── offline_import.py  # HTML/PDF/TXT/MD 离线导入
│   ├── store.py           # 入库、版本归档、diff
│   ├── search.py          # 检索、分面
│   └── cli.py             # 命令行实现
├── tests/test_regkb.py    # 单元测试（22 项）
└── data/regkb.sqlite      # 生成的数据库（未纳入版本控制）
```

---

## 测试

```bash
python tests/test_regkb.py          # 独立运行，无需 pytest
```

覆盖发文字号解析（含反例）、归类排除规则、效力推断、FTS 索引同步、检索语义
（含纯排除查询与两字词回退）、版本归档与 diff。

---

## 局限

- **分类规则是启发式的**，不是法律意见。标签用于辅助检索，不能替代对原文的阅读。
- **效力状态为推断值**，来源页面通常不提供结构化的废止标记。标注为 `in_force` /
  `abolished` 的，仍应以官方最新公告为准。
- **gov.cn 收录范围有限**：其政策文件库主要收录国务院及部门层面的文件，NMPA 的
  大量技术指导原则并不在其中，这是需要离线导入的原因。
- **网站结构可能变化**：采集器依赖页面结构，上游改版后可能需要调整。
- 本项目**不是**法律依据，正式注册申报请以官方发布文本为准。

---

## English summary

A zero-dependency, local knowledge base for Chinese drug-regulation documents,
aimed at regulatory-affairs and pharmacy-administration work. It harvests
public policy documents from `gov.cn` and the Chinese Pharmacopoeia Commission,
stores them in SQLite with an FTS5 trigram index, auto-classifies them against
ICH guideline codes and CTD modules, normalises 发文字号 document numbers, and
tracks revisions with unified diffs.

NMPA, CDE and NHC are deliberately **not** scraped: they are protected by a
JavaScript anti-bot WAF. Material from those bodies enters the knowledge base
through an offline importer (`html` / `pdf` / `txt` / `md`), which keeps the
provenance of every record auditable.

Pure standard library — Python 3.9+, no `pip install`.

```bash
python regkb.py crawl
python regkb.py import /path/to/saved/notices
python regkb.py search "药品注册 -化妆品"
python webui.py
```

## License

MIT
