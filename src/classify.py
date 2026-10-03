"""Rule-based classification of Chinese drug-regulation documents.

Why rules instead of a model: a registration specialist has to be able to see
*why* a document was filed where it was.  A keyword table is auditable and
reproducible; an opaque classifier is neither.  Every tag this module assigns is
traceable to a literal string found in the document.
"""

from __future__ import annotations

import re

# --- ICH guideline mapping -------------------------------------------------
# ICH publishes under codes like Q1A(R2), S9, E6(R3), M4.  Chinese implementing
# notices usually quote the code, so a regex over the text is a reliable signal.
ICH_PATTERNS: list[tuple[str, str]] = [
    (r"Q1[A-E]?(?:\(R\d\))?", "质量/稳定性"),
    (r"Q2(?:\(R\d\))?", "质量/分析方法验证"),
    (r"Q3[A-D]?(?:\(R\d\))?", "质量/杂质"),
    (r"Q5[A-E]?(?:\(R\d\))?", "质量/生物技术产品"),
    (r"Q6[A-B]?(?:\(R\d\))?", "质量/质量标准"),
    (r"Q7(?:A)?", "质量/原料药GMP"),
    (r"Q8(?:\(R\d\))?", "质量/药品研发"),
    (r"Q9(?:\(R\d\))?", "质量/风险管理"),
    (r"Q10", "质量/药品质量体系"),
    (r"Q11", "质量/原料药开发"),
    (r"Q12", "质量/产品生命周期"),
    (r"S1[A-C]?(?:\(R\d\))?", "安全性/致癌性"),
    (r"S2(?:\(R\d\))?", "安全性/遗传毒性"),
    (r"S3[A-B]?(?:\(R\d\))?", "安全性/毒代动力学"),
    (r"S4", "安全性/重复给药毒性"),
    (r"S5(?:\(R\d\))?", "安全性/生殖毒性"),
    (r"S6(?:\(R\d\))?", "安全性/生物技术产品"),
    (r"S7[A-B]?", "安全性/药理学"),
    (r"S8", "安全性/免疫毒性"),
    (r"S9", "安全性/抗肿瘤药物"),
    (r"S10", "安全性/光安全性"),
    (r"S11", "安全性/儿科药物"),
    (r"E1(?:\(R\d\))?", "有效性/临床安全性"),
    (r"E2[A-F]?(?:\(R\d\))?", "有效性/临床研究"),
    (r"E3", "有效性/临床研究报告"),
    (r"E4", "有效性/剂量反应"),
    (r"E5(?:\(R\d\))?", "有效性/种族因素"),
    (r"E6(?:\(R\d\))?", "有效性/GCP"),
    (r"E7", "有效性/老年人群"),
    (r"E8", "有效性/临床研究总体考虑"),
    (r"E9(?:\(R\d\))?", "有效性/统计学"),
    (r"E10", "有效性/对照组选择"),
    (r"E11(?:\(R\d\))?", "有效性/儿科人群"),
    (r"E14", "有效性/QT间期"),
    (r"M1", "多学科/MEDDRA"),
    (r"M2", "多学科/电子标准"),
    (r"M3", "多学科/非临床安全性研究"),
    (r"M4(?:Q|S|E)?(?:\(R\d\))?", "多学科/CTD"),
    (r"M7(?:\(R\d\))?", "多学科/遗传毒性杂质"),
    (r"M8", "多学科/eCTD"),
]

# --- CTD module mapping ----------------------------------------------------
CTD_PATTERNS: list[tuple[str, str]] = [
    (r"模块\s*1|行政文件和药品信息|CTD\s*1\b", "CTD模块1-行政信息"),
    (r"模块\s*2|质量综述|非临床综述|临床综述|CTD\s*2\b", "CTD模块2-综述"),
    (r"模块\s*3|药学研究资料|CTD\s*3\b", "CTD模块3-质量"),
    (r"模块\s*4|非临床研究报告|CTD\s*4\b", "CTD模块4-非临床"),
    (r"模块\s*5|临床研究报告|CTD\s*5\b", "CTD模块5-临床"),
    (r"eCTD|电子通用技术文档", "eCTD"),
]

# --- Domain topics ---------------------------------------------------------
# Ordered from most specific to most general: the first match wins for the
# primary category so that "药品注册" is not shadowed by "药品".
#
# Each entry is ``(pattern, label, exclude)``.  ``exclude`` is an optional
# pattern that, when present in the title, suppresses the label.  This matters
# because Chinese regulation titles reuse a small vocabulary across product
# classes: "化妆品注册备案资料管理规定" contains 注册 but is not a drug
# registration document, and mis-filing it would send a specialist to the wrong
# text.
TOPIC_RULES: list[tuple[str, str, str | None]] = [
    (r"药物警戒|不良反应|药品安全信号|PSUR|定期安全性更新报告", "药物警戒", r"医疗器械"),
    (r"药品注册|注册申请|审评审批|优先审评|附条件批准|突破性治疗", "药品注册",
     r"^关于?.*化妆品|化妆品.*(?:注册|备案)|医疗器械.*注册"),
    (r"药品生产|生产质量管理|GMP|生产许可|委托生产", "药品生产", None),
    (r"药品经营|经营质量管理|GSP|药品流通|零售药店", "药品经营", None),
    (r"药物临床试验|临床试验|GCP|研究者|伦理委员会|知情同意", "药物临床试验",
     r"医疗器械"),
    (r"药品标准|药典|质量标准|中药饮片", "药品标准", None),
    (r"药品价格|集中采购|集采|医保|支付标准|价格谈判", "价格与采购", None),
    (r"药品上市许可持有人|MAH|持有人", "上市许可持有人", None),
    (r"药品追溯|追溯体系|信息化追溯", "药品追溯", None),
    (r"疫苗|免疫规划", "疫苗管理", None),
    (r"医疗器械|体外诊断", "医疗器械", None),
    (r"药品广告|广告审查", "药品广告", None),
    (r"执业药师|药师", "执业药师", None),
    (r"药品分类管理|处方药|非处方药|OTC", "分类管理", None),
    (r"药品召回|召回管理", "药品召回", None),
    (r"中药|中成药|中医|经典名方|民族药", "中医药", None),
    (r"化妆品", "化妆品", None),
    (r"特殊管理药品|麻醉药品|精神药品|医疗用毒性药品|放射性药品", "特殊管理药品", None),
]

# --- Document-number prefix -> issuing authority ---------------------------
# Helps fill in `agency` when the source page omits it.
AGENCY_BY_PREFIX: dict[str, str] = {
    "国令": "国务院",
    "国发": "国务院",
    "国函": "国务院",
    "国办发": "国务院办公厅",
    "国办函": "国务院办公厅",
    "国办发明电": "国务院办公厅",
    "国卫": "国家卫生健康委员会",
    "国卫办": "国家卫生健康委员会",
    "国药监": "国家药品监督管理局",
    "药监": "国家药品监督管理局",
    "国家药监局": "国家药品监督管理局",
    "发改": "国家发展和改革委员会",
    "医保": "国家医疗保障局",
    "医保发": "国家医疗保障局",
    "人社": "人力资源和社会保障部",
    "市监": "国家市场监督管理总局",
    "工信": "工业和信息化部",
    "科技": "科学技术部",
}

# gov.cn carries NMPA announcements in a long-form shape
# ("药监局公告2021年第32号") while the authority's own numbering is
# "国家药监局2021年第32号公告".  Normalise to the bracketed national form so
# that one document has one identifier regardless of where it was found.
DOC_NUMBER_NORMALISERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^药监局公告\s*(\d{4})\s*年?\s*第?\s*(\d+)\s*号$"),
     r"国药监〔\1〕\2号"),
    (re.compile(r"^国家药监局(?:公告)?\s*(\d{4})\s*年?\s*第?\s*(\d+)\s*号$"),
     r"国药监〔\1〕\2号"),
    (re.compile(r"^(\d{4})\s*年?\s*第?\s*(\d+)\s*号$"), r"〔\1〕\2号"),
]

# A bare "2024年第38号" with no agency prefix.  NMPA announcements on gov.cn are
# rendered this way, with the issuing body only in the title, so this pattern is
# accepted *only* when a known drug authority is named nearby (see
# ``document_number_from_text``) — otherwise any "第N号" in prose would match.
_BARE_NUMBER_RE = re.compile(
    r"(\d{4})\s*年\s*第\s*(\d+)\s*号|(\d{4})\s*年第\s*(\d+)\s*号"
)

_DRUG_AUTHORITY_RE = re.compile(
    r"国家药监局|国家药品监督管理局|药监局|药品监督管理局|国家药典委员会"
)
#   * 国办发〔2026〕9号 / 药监综〔2021〕1号   -> bracketed, prefix before bracket
#   * 药监局公告2021年第32号                 -> long form used by gov.cn
#   * 国令第828号                            -> decree form
#
# The prefix is restricted to known issuing-authority abbreviations rather than
# any CJK run, because a permissive prefix happily swallows ordinary prose
# ("...补充资料时限的公告2022年第86号" would yield a bogus prefix).
_AGENCY_PREFIX_ALT = "|".join(
    re.escape(p) for p in sorted(AGENCY_BY_PREFIX, key=len, reverse=True)
)

DOC_NUMBER_RE = re.compile(
    rf"({_AGENCY_PREFIX_ALT})\s*(?:〔|\[|【)\s*(\d{{4}})\s*(?:〕|\]|】)\s*(?:第)?\s*(\d+)\s*号"
    rf"|({_AGENCY_PREFIX_ALT})\s*(?:公告)?\s*(\d{{4}})\s*年\s*第\s*(\d+)\s*号"
    rf"|({_AGENCY_PREFIX_ALT})第\s*(\d+)\s*号"
)

STATUS_ABOLISHED_PATTERNS = [
    r"予以废止", r"决定废止", r"同时废止", r"起废止", r"宣布失效",
]
# "施行"/"实施" are frequently separated from the date by an adverb such as
# 正式/同步, so the pattern allows a short run of characters in between.
STATUS_IN_FORCE_PATTERNS = [
    r"自.{0,40}?起.{0,6}?(?:施行|实施|执行|生效)",
    r"自.{0,40}?起.{0,6}?施行的",
    r"现予公布", r"现予发布", r"现予印发",
]


def _find_tags(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    """Return the deterministic set of labels whose pattern matches ``text``."""
    found: list[str] = []
    for pattern, label in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            if label not in found:
                found.append(label)
    return found


def classify(title: str, body: str, doc_number: str | None = None) -> list[tuple[str, str]]:
    """Classify a document into ``(tag_name, tag_kind)`` pairs.

    Topic matching is title-first: a document's title is the strongest signal
    of what it is about, so a topic found in the title is preferred, and a
    body-only match is accepted only when the title does not exclude it.
    """
    head = title or ""
    full = f"{title or ''}\n{body or ''}"

    tags: list[tuple[str, str]] = []

    for label in _find_tags(full, ICH_PATTERNS):
        tags.append((f"ICH {label}", "ich"))

    for label in _find_tags(full, CTD_PATTERNS):
        tags.append((label, "ctd"))

    # --- topic selection -------------------------------------------------
    title_topics: list[str] = []
    body_topics: list[str] = []
    for pattern, label, exclude in TOPIC_RULES:
        in_title = re.search(pattern, head, flags=re.IGNORECASE) is not None
        in_full = re.search(pattern, full, flags=re.IGNORECASE) is not None
        if not in_full:
            continue
        # A title-level exclusion vetoes the label outright.
        if exclude and re.search(exclude, head, flags=re.IGNORECASE):
            continue
        (title_topics if in_title else body_topics).append(label)

    # Title matches are trustworthy; body-only matches are weaker, so only one
    # body-derived topic is kept and only if no title topic was found.
    topics = title_topics[:2]
    if not topics and body_topics:
        topics = body_topics[:1]
    for label in topics:
        tags.append((label, "topic"))

    if doc_number:
        for prefix in sorted(AGENCY_BY_PREFIX, key=len, reverse=True):
            if doc_number.startswith(prefix):
                canon = AGENCY_BY_PREFIX[prefix]
                # Skip when an agency *topic* tag would duplicate this: tagging
                # every NMPA notice with both "国家药品监督管理局" (from the doc
                # number) and again as an authority adds no information.
                if not any(name == canon for name, kind in tags if kind == "topic"):
                    tags.append((canon, "agency"))
                break

    # De-duplicate while preserving order.
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def normalise_doc_number(raw: str | None) -> str | None:
    """Normalise a 发文字号 to the canonical bracketed form where possible."""
    if not raw:
        return None
    value = raw.strip()
    value = re.sub(r"\s+", " ", value)
    for pattern, repl in DOC_NUMBER_NORMALISERS:
        if pattern.match(value):
            return pattern.sub(repl, value)
    return value


def document_number_from_text(text: str) -> str | None:
    """Extract a 发文字号 from free text, or ``None``.

    Prefers an explicitly prefixed number (``国办发〔2026〕9号``).  Falls back to
    a bare ``2024年第38号`` only when a drug authority is named in the same text,
    which is how NMPA announcements appear on gov.cn.
    """
    text = text or ""
    m = DOC_NUMBER_RE.search(text)
    if m:
        groups = m.groups()
        if groups[0] is not None:
            return normalise_doc_number(f"{groups[0]}〔{groups[1]}〕{groups[2]}号")
        if groups[3] is not None:
            # "药监局公告2021年第32号" style.
            return normalise_doc_number(f"{groups[3]}公告{groups[4]}年第{groups[5]}号")
        if groups[6] is not None:
            return normalise_doc_number(f"{groups[6]}第{groups[7]}号")

    if _DRUG_AUTHORITY_RE.search(text):
        b = _BARE_NUMBER_RE.search(text)
        if b:
            year = b.group(1) or b.group(3)
            num = b.group(2) or b.group(4)
            return normalise_doc_number(f"国药监〔{year}〕{num}号")

    return None


def agency_from_doc_number(doc_number: str | None) -> str | None:
    if not doc_number:
        return None
    for prefix in sorted(AGENCY_BY_PREFIX, key=len, reverse=True):
        if doc_number.startswith(prefix):
            return AGENCY_BY_PREFIX[prefix]
    return None


# Canonical authority names.  Source pages write the same body several ways
# ("国家药监局", "药监局", "国家药品监督管理局"), which would otherwise fragment
# the agency facet into near-duplicate buckets.
AGENCY_ALIASES: list[tuple[str, str]] = [
    ("国家药品监督管理局", "国家药品监督管理局"),
    ("国家药监局", "国家药品监督管理局"),
    ("药监局", "国家药品监督管理局"),
    ("国家药典委员会", "国家药典委员会"),
    ("国家卫生健康委员会", "国家卫生健康委员会"),
    ("国家医疗保障局", "国家医疗保障局"),
    ("国家市场监督管理总局", "国家市场监督管理总局"),
    ("国务院办公厅", "国务院办公厅"),
    ("国务院", "国务院"),
]


def canonical_agency(value: str | None) -> str | None:
    """Map a free-text authority name onto its canonical form."""
    if not value:
        return None
    text = value.strip()
    for alias, canon in AGENCY_ALIASES:
        if alias in text:
            return canon
    return text or None


def infer_status(body: str, pub_date: str | None) -> str:
    """Best-effort 现行有效/已废止 inference.

    Deliberately conservative: we only claim ``abolished`` when the text says so
    explicitly, because wrongly marking an in-force regulation as abolished is a
    far more damaging error than leaving the status as ``unknown``.
    """
    text = body or ""
    # The repeal clause normally sits at the end of the document, so check the
    # tail first and only there: "同时废止" in a preamble usually refers to
    # other documents rather than to this one.
    tail = text[-2000:]
    for pattern in STATUS_ABOLISHED_PATTERNS:
        if re.search(pattern, tail, flags=re.S):
            return "abolished"
    for pattern in STATUS_IN_FORCE_PATTERNS:
        if re.search(pattern, text, flags=re.S):
            return "in_force"
    return "unknown"
