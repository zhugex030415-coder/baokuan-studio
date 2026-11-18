#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爆款文案工作台 · 自动版

L1 选题 → L2 钩子 → L3 结构 → L4 去AI味 → L5 复盘，由模型自动跑完。

用法：
    python server.py                # 打开 http://127.0.0.1:8770
    python server.py --mock         # 不调模型，用假数据验证界面

只用 Python 标准库，不需要 pip install。
"""

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
DEFAULT_API = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

# 读取本地配置（key 只存在这里，不进前端）
CONFIG_PATH = ROOT / "config.json"
def _load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
CONFIG = _load_config()
# 部署到云端时优先读环境变量，key 不写进文件
CONFIG_KEY = os.environ.get("DEEPSEEK_KEY", "").strip() or CONFIG.get("key", "")
CONFIG_MODEL = os.environ.get("DEEPSEEK_MODEL", "").strip() or CONFIG.get("model", DEFAULT_MODEL)
CONFIG_API = os.environ.get("DEEPSEEK_API", "").strip() or CONFIG.get("api", DEFAULT_API)
# 双模型分工：writer 负责写稿，polisher 负责改语气和其余环节
CONFIG_WRITER = CONFIG.get("writer") or {"key": CONFIG_KEY, "api": CONFIG_API, "model": CONFIG_MODEL}
CONFIG_POLISHER = CONFIG.get("polisher") or {"key": CONFIG_KEY, "api": CONFIG_API, "model": CONFIG_MODEL}
CONFIG_REVIEWER = CONFIG.get("reviewer") or {"key": CONFIG_KEY, "api": CONFIG_API, "model": CONFIG_MODEL}
# 云端审核模型（Kimi）从环境变量读，避免 key 写进仓库
if os.environ.get("REVIEWER_KEY", "").strip():
    CONFIG_REVIEWER = {
        "key": os.environ.get("REVIEWER_KEY", "").strip(),
        "api": os.environ.get("REVIEWER_API", "").strip() or "https://api.moonshot.cn/v1/chat/completions",
        "model": os.environ.get("REVIEWER_MODEL", "").strip() or "kimi-k3",
    }
# 访问码：公开到公网时设置，防止别人白用你的额度
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()

# ---------------------------------------------------------------- 赛道档案
NICHE = """受众：小学 3-6 年级孩子的家长，一二线城市，重视英语，有择校/分班考压力
他们最常说的三句话：他单词都认识就是读不懂句子 / 别人家孩子都在学什么 / 报了班怎么没效果
真实痛点：怕孩子掉队；分不清「学得慢」和「学错了」；被机构话术反复收割
他们信什么：具体案例、可执行动作、老师本人的态度
他们不信什么：方法论名词、成功学、任何形式的保证
红线词（出现即改写）：提分、保过、包过、冲刺班、100%、最有效、保证、孩子一定能"""

# 同赛道真实爆款（2026-11 采集，按赞排序）
HITS = [
    (80000, "来，刷刷英语语法！"),
    (65000, "刚开始做英语启蒙，只做这两件事就够了"),
    (46000, "英语启蒙唯一正确的路（超全版）！"),
    (41000, "全英课堂丨坚持听一个月，听力会变强的"),
    (26000, "记单词最快的方法！记字母组合！"),
    (21000, "提升英语唯一正确的方案"),
    (19000, "小学英语专项训练be动词"),
    (14000, "孩子四年级了，我才发现学英语的好物"),
    (12000, "不会语法的请看❗一篇吃透小学基础英语语法"),
    (11000, "你个老外怎么几句话就把冠词讲明白了？！"),
    (9051, "锐评11部全网热门英语动画！红榜蓝榜自选！"),
    (8142, "拜托把它打印出来🔥孩子英语语法稳了❗️"),
    (4672, "16部英语启蒙动画黄金顺序✅ 新手爸妈直接抄"),
    (3108, "我发现英语启蒙失败的根源之一：没有主线"),
]
HIT_TEXT = "\n".join(f"- {t}（{str(n)[:-4]}万赞）" if n >= 10000 else f"- {t}（{n}赞）" for n, t in HITS)

# ---------------------------------------------------------------- 模型调用
class ModelError(Exception):
    pass


def call_model(messages, key, model, api, want_json=True, temperature=0.85):
    # 推理模型（Kimi K3 / DeepSeek V4 Pro）会先思考，需要大 token 预算；
    # Kimi 还只接受 temperature=1
    m = str(model).lower()
    is_kimi = "kimi" in m
    if is_kimi:
        temperature = 1
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 16000 if is_kimi else 8000,
    }
    # DeepSeek 推理模型默认会先深度思考（慢），显式关掉思考
    if "v4-pro" in m or "reasoner" in m:
        payload["reasoning_effort"] = "none"
    if want_json:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        api,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=(600 if is_kimi else 200)) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:400]
        raise ModelError(f"HTTP {e.code}：{detail}")
    except Exception as e:
        raise ModelError(f"请求失败：{e}")
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise ModelError(f"返回格式异常：{str(data)[:300]}")


def extract_json(text):
    """从模型输出里抠出第一个完整 JSON 对象。"""
    if not text:
        raise ValueError("空输出")
    s = text.strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    start = s.find("{")
    if start < 0:
        raise ValueError("输出里没有 JSON")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("JSON 不完整")


# ---------------------------------------------------------------- 四道关
SYS = """你是一位在长沙带小学 3-6 年级英语的老师，教了七八年，学生大多是普娃，家长普遍焦虑。
你说话直接、有点不耐烦，但真心为孩子着急。
你从不说官话、套话、方法论黑话，也不堆砌金句，不对仗，不喊口号。
你举的例子都来自你真实带过的孩子：有年级、有场景、有孩子亲口说的话、有真实的数字。
你写东西像在跟一个家长面对面聊天，想到哪说到哪，会自我纠正，会承认「这个我不打包票」。
你只输出 JSON，不要任何解释。"""

# 纯文本输出版本（不用 JSON，避免截断和解析问题）
SYS_TEXT = SYS.replace("你只输出 JSON，不要任何解释。", "直接输出成稿正文，不要解释，不要代码块。")

# 干货型：用 oil-tone 的规范（清楚、自然、不拔高）
STYLE_DRY = """这一篇是干货型，按下面的规范写：

1 句子写完整、按自然语序。不要为了显得利落把话压成生硬短句；「的」「了」只要承担语法或语气作用就保留，别当成赘词删掉。
2 把关系讲完整。家长不知道的那一步要补上，事实、原因、结果之间不能跳。
3 结尾给一个能照着做的办法，步骤要具体到今晚就能做，再引导收藏和报年级。
4 不要金句，不要拔高，不要喊口号，不要模板化转场。删掉之后不影响意思的句子，直接删。
5 不要为了显得口语而机械加语气词。
6 有一个地方承认你不确定就够了，别反复说。
7 标题说清真实对象和判断，不夸张。"""

# 事件型：用卡兹克那套（有态度、口语、留一半给评论区）
STYLE_EVENT = """这一篇是事件型，按下面的规范写：

1 从一个具体的人、一个具体的时刻切入，不宏大叙事。
2 给一句明确的立场判断，敢说。观点可以先站到对方角度（「我非常理解」），再说你的。
3 可以让情绪起伏：有一处用一个极短句独立成段，制造停顿，但别每段都断。
4 有一个金句，能单独拿出来传播的那一句。
5 不塞方法论，不给步骤，把结论留一半给评论区。
6 结尾问一个家长能立刻回答的问题，引导讨论，不引导收藏。
7 编不出来的真实信息（名字、年龄、数字）写【】留空。"""


def run_l1(topic, key, model, api, mock=False):
    if mock:
        return {"反差": 4, "具体": 3, "立场": 3, "代入": 5, "缺口": 4, "总分": 19,
                "死刑": [], "判定": "过", "理由": "痛点命中强，但缺一个真实数字，补上就是 22 分。"}
    prompt = f"""赛道档案：
{NICHE}

素材：{topic}

按这五个维度各打 1-5 分（1分最弱，5分最强）：
反差=是否推翻读者已有做法；具体=是否有数字/场景/原话；立场=是否有只有你能下的判断；
代入=目标读者会不会觉得「说我」；缺口=知不知道「有点什么」但不知道「是什么」。

再检查四条死刑（命中任一条即退回）：
1 没有具体案例/数字/场景，只有道理
2 结论是「要努力/要坚持/多练习」这类正确的废话
3 出现红线词且改写后失去力量
4 只讲知识，没讲「谁做错了什么」

再判内容类型（二选一，决定结尾怎么写）：
- 干货：素材指向「教一个方法 / 纠正一个做错的做法」——家长看完该学会做点什么
- 事件：素材指向「一个现象 / 一个判断 / 一个槽点 / 一个共鸣」——家长看完该想说点什么

输出 JSON：
{{"反差":int,"具体":int,"立场":int,"代入":int,"缺口":int,"总分":int,
"类型":"干货|事件",
"死刑":[命中条目原文, ...],"判定":"过|重评|退回","理由":"一句话，指出最该补的那一维"}}

总分>=16 判过；13-15 判重评；<=12 判退回。有死刑项一律判退回。"""
    raw = call_model([{"role": "system", "content": SYS}, {"role": "user", "content": prompt}], key, model, api, temperature=0.3)
    return extract_json(raw)


def run_l2(topic, l1, key, model, api, mock=False):
    if mock:
        return {
            "候选": [
                {"类型": "反常识", "标题": "他单词都认识，却不是词汇量的问题"},
                {"类型": "反常识", "标题": "别再让孩子背单词了"},
                {"类型": "划线", "标题": "英语能走远的孩子，都在做同一件事"},
                {"类型": "劝退", "标题": "我劝你先停掉单词打卡"},
                {"类型": "揭秘", "标题": "KET 真正考的东西，没人提前说"},
                {"类型": "算账", "标题": "每天多背 20 个词，你换走了什么"},
                {"类型": "自检", "标题": "如果你家孩子也说读不懂，往下看"},
            ],
            "打分": [
                {"标题": "他单词都认识，却不是词汇量的问题", "痛点": 5, "缺口": 4, "可兑现": 5, "平台适配": 4, "合计": 18},
                {"标题": "我劝你先停掉单词打卡", "痛点": 5, "缺口": 4, "可兑现": 4, "平台适配": 5, "合计": 18},
                {"标题": "KET 真正考的东西，没人提前说", "痛点": 4, "缺口": 5, "可兑现": 3, "平台适配": 5, "合计": 17},
            ],
            "定稿": ["他单词都认识，却不是词汇量的问题", "我劝你先停掉单词打卡", "KET 真正考的东西，没人提前说"],
        }
    prompt = f"""赛道档案：
{NICHE}

同赛道真实爆款标题（只学句式，不要抄内容）：
{HIT_TEXT}

素材：{topic}
L1 判定：{json.dumps(l1, ensure_ascii=False)}

任务：
1. 按 7 类钩子各出 2 条候选标题，共 14 条。
   类型只能是：反常识、划线、劝退、揭秘、对比、算账、自检。
   硬规则：不出现「最/第一/保证/必/100%/立刻/永久」的绝对化承诺；不出现红线词；
   每条不超过 20 字；至少一半要含具体名词或数字；用陈述句，不用疑问句收尾。
   参考对标：头部爆款高频用「唯一正确/最快/就够了」这类确定性表达，和数字划边界。
2. 对全部 14 条按四维打 1-5 分：痛点强度、缺口大小、可兑现、平台适配。
3. 选出 3 条，要求性格各异：最狠的（反差最大）、最稳的（搜索词最全）、最反常识的。

输出 JSON：
{{"候选":[{{"类型":"","标题":""}}, ...14条],
"打分":[{{"标题":"","痛点":int,"缺口":int,"可兑现":int,"平台适配":int,"合计":int}}, ...14条],
"定稿":["最狠的","最稳的","最反常识的"]}}"""
    raw = call_model([{"role": "system", "content": SYS}, {"role": "user", "content": prompt}], key, model, api, temperature=0.9)
    return extract_json(raw)


def run_l3(topic, l1, l2, platform, key, model, api, mock=False):
    if mock:
        return {
            "小红书": {
                "标题": "他单词都认识，却不是词汇量的问题",
                "封面大字": "不是词汇量",
                "正文": "「老师，他单词都认识，怎么就是读不懂句子？」\n\n这个问题我一年被问几十次。\n\n家长的默认反应是加背单词。但五年级这个阶段，大多数孩子卡住的不是词，是不会断句。\n\n英文不是逐词读的。孩子一个词一个词翻译，读到句尾，前面已经忘了。\n\n我给学生的三步：\n\n① 数动词。一个句子有几个真正的动词，就至少有几个分句。\n② 圈连接词。who / that / because / before，每一个都是断句点。\n③ 分层写下来。主句一行，从句缩进。\n\n孩子一旦看见「主句其实只有两个词」，那种读不懂的恐惧会立刻消一半。\n\n图 3-6 是我给五年级学生做的拆解页，可以直接打印。\n\n背单词解决认不认识，断句解决读不读得懂。两件事，别混在一起。",
                "标签": "#小学英语 #英语阅读 #长难句 #KET #小学高年级 #英语学习方法 #家长必读 #英语启蒙"
            },
            "视频号": {
                "口播稿": "很多家长问我，孩子单词都认识，为什么还是读不懂句子。\n\n我的回答通常是：问题不在单词。\n\n英文句子不是按词读的，是按意群读的。孩子一个词一个词翻译，读到后面忘了前面，句子一长就崩。\n\n所以我不先让他背词，我让他先数动词。一个句子有几个动词，就有几个分句。再圈出连接词，那就是该断的地方。\n\n然后用纸写下来：主句一行，从句缩进。写完他自己就看见结构了。\n\n背单词解决认不认识，断句解决读不读得懂。这是两件事。"
            }
        }
    kind = (l1.get("类型") or "干货") if isinstance(l1, dict) else "干货"
    style = STYLE_DRY if kind == "干货" else STYLE_EVENT
    prompt = f"""语气参考（学语气，别学它的选题和结构）：

例1——
「他单词都认识，就是读不懂句子。」
这句话我一年听几十遍。
家长的第一反应都是加背单词。但五年级这个阶段，大多数孩子卡住的不是词，是不会断句。
我让学生先数动词，一个句子有几个动词，就有几层。再圈连接词，那就是该断的地方。
写完分层，孩子自己就看见「主句其实只有两个词」。那一瞬间，恐惧就散了。

例2——
昨天一个妈妈发微信，说家里原版书买了一柜子，孩子一本没读完。她问我是不是书不好。
我说先别买了。
她愣了一下。
问题不在书，在级别。挑书有个特别土但特别准的办法，叫五指法则。随便翻一页让他读，一个生词伸一根手指。2 到 3 根，就是刚好。
书多不多不是关键，匹配才关键。

素材：{topic}
内容类型：{kind}

{style}

通用要求：从具体当下的事切入，不宏大叙事；数字用阿拉伯数字；知识要「随手掏」——说「我突然想起来」，不说「下面介绍」。

输出 JSON：
{{"小红书":{{"标题":"","封面大字":"","正文":"","标签":""}},
"视频号":{{"口播稿":""}}}}"""
    # 写稿用 writer（Kimi K3），其余环节用 DeepSeek
    w = CONFIG_WRITER
    raw = call_model([{"role": "system", "content": SYS}, {"role": "user", "content": prompt}],
                     w["key"], w["model"], w["api"], temperature=0.9)
    return extract_json(raw)


# ---------------------------------------------------------------- L4 本地扫描
BANNED = {
    "连接腔": ["值得注意的是", "综上所述", "不仅如此", "与此同时", "由此可见", "换言之", "不难发现", "总的来说"],
    "概念腔": ["赋能", "闭环", "抓手", "维度", "底层逻辑", "生态", "链路", "颗粒度", "打法", "本质上"],
    "总结腔": ["首先其次最后", "总而言之", "让我们", "在这个", "深度剖析", "全方位", "意味着"],
    "情绪腔": ["震惊", "炸裂", "绝了", "逆天", "天花板", "神级"],
}
CJK = re.compile(r"[\u4e00-\u9fa5]")

# ---- AI 味指纹：四个可测指标 ----
COLLOQ = ["说实话", "其实", "反正", "怎么说呢", "真的", "挺", "特别", "吧", "啊", "嘛",
          "你懂", "就是", "老实说", "说白了", "弄", "搞", "唉", "哈", "呗", "这么一说"]
PSEUDO_CUT = re.compile(r"([^。！？\n]{2,6})，([^。！？\n]{2,6})。")
SELFCORR = re.compile(r"一开始|后来才|本来以为|其实不是|我原以为|说实话|说真的|我一开始|后来发现")


def count_pseudo(text):
    """对仗伪短句：整句只含一个逗号，两半都短且长度相近。

    真人不会刚好对得这么齐，AI 特别爱写「X，就 Y。」「不是 A，是 B。」。
    """
    n = 0
    for sent in re.split(r"[。！？\n]", text):
        s = sent.strip()
        if "，" not in s:
            continue
        parts = [p.strip() for p in s.split("，") if p.strip()]
        if len(parts) != 2:
            continue
        a, b = parts
        if 2 <= len(a) <= 6 and 2 <= len(b) <= 6 and abs(len(a) - len(b)) <= 1:
            n += 1
    return n


def scan_ai(text):
    """检测 AI 味。真人写作的标志：段落长短不齐、口语多、对仗少、会自我修正。"""
    paras = [l.strip() for l in text.split("\n") if l.strip()]
    lens = [len(CJK.findall(p)) for p in paras]
    if lens:
        mean = sum(lens) / len(lens)
        var = sum((x - mean) ** 2 for x in lens) / len(lens)
        unif = (var ** 0.5) / mean if mean else 1.0
    else:
        mean, unif = 0, 1.0
    colloq = sum(text.count(w) for w in COLLOQ)
    pseudo = count_pseudo(text)
    density = pseudo / max(1, len(text)) * 100
    selfcorr = bool(SELFCORR.search(text))
    flags = []
    if unif < 0.45:
        flags.append(f"段落长度太均匀（{unif:.2f}，真人文案通常 >0.6）")
    if colloq < 8:
        flags.append(f"口语词只有 {colloq} 个，目标 ≥8")
    if pseudo >= 3 and density >= 0.8:
        flags.append(f"对仗式伪短句偏密（{pseudo} 处，每百字 {density:.1f}）")
    if not selfcorr:
        flags.append("没有自我修正或犹豫，读起来太笃定")
    return {
        "段落均匀度": round(unif, 2),
        "平均段长": round(mean, 1),
        "口语词": colloq,
        "对仗短句": pseudo,
        "对仗密度": round(density, 2),
        "有自我修正": selfcorr,
        "问题": flags,
        "像真人": not flags,
    }



def scan_l4(text):
    hits, puncts = [], []
    for cat, words in BANNED.items():
        for w in words:
            if w in text:
                hits.append(w)
    for name, ch in [("冒号", "："), ("破折号", "——"), ("双引号", "“"), ("双引号", "”")]:
        if ch in text:
            puncts.append(name + ch)
    has_num = bool(re.search(r"\d", text)) or bool(re.search(r"[一二三四五六七八九十百千万]+(年|月|天|周|分钟|页|个|部|岁|年级|遍|次|万|条|张|句)", text))
    has_quote = bool(re.search(r"[「“]", text))
    short_line = any(0 < len(l.strip()) <= 6 for l in text.split("\n"))
    hedge = bool(re.search(r"可能|也许|不敢|不一定|有的孩子|说实话|不保证", text))
    musts = [("具体数字", has_num), ("一句原话", has_quote), ("5字内独立成段", short_line), ("一处不确定", hedge)]
    missing = [n for n, ok in musts if not ok]
    score = max(0.0, 10 - len(hits) * 2 - len(puncts) * 2 - len(missing) * 1.5)
    return {
        "字数": len(CJK.findall(text)),
        "禁用词": hits,
        "禁用标点": puncts,
        "必备": {n: ok for n, ok in musts},
        "缺失": missing,
        "得分": round(score, 1),
        "通过": not hits and not puncts and not missing,
    }


def run_l4(text, key, model, api, mock=False):
    rep = scan_l4(text)
    rep["AI味"] = scan_ai(text)
    fixed = None
    if not rep["通过"] and not mock and key:
        ai = rep["AI味"]
        prompt = f"""下面这段是 AI 味重的一版文案。请用你自己的口吻整篇重写，而不是逐句修修补补。

待改文案：
{text}

问题：禁用词{'、'.join(rep['禁用词']) or '无'}；标点{'、'.join(rep['禁用标点']) or '无'}；{'；'.join(ai['问题']) or ''}

重写时：
- 保留全部真实信息、观点、数据，一个都不许丢。
- 把「工整」拆掉：对仗句改散，段落有长有短，书面词换成口语。
- 语气要像你在跟家长当面聊天，有真实的犹豫和不确定，但不硬凑语气词。
- 不编造。素材里没有的信息就保持原样或留【】。

输出 JSON：{{"改写后":"","note":"改了什么"}}"""
        try:
            raw = call_model([{"role": "system", "content": SYS}, {"role": "user", "content": prompt}], key, model, api, temperature=0.7)
            d = extract_json(raw)
            fixed = d.get("改写后")
            rep["note"] = d.get("note", "")
            if fixed:
                rep["复扫"] = scan_l4(fixed)
        except Exception as e:
            rep["note"] = f"自动修订失败：{e}"
    return {"报告": rep, "修订稿": fixed}


def run_l5(topic, l1, l2, l3, mock=False):
    """L5 不调模型：给复盘表头 + 归因提示。"""
    tips = []
    if l1.get("具体", 5) <= 2:
        tips.append("L1「具体」偏低——点击高但收藏低多半是这个原因，补真实数字和原话。")
    if l1.get("代入", 5) <= 2:
        tips.append("L1「代入」偏低——涨粉多但不转化，通常是爆在泛情绪，人群跑偏。")
    if l1.get("立场", 5) <= 2:
        tips.append("L1「立场」偏低——收藏高但涨粉少，说明全是干货没有你这个人。")
    return {
        "记录表头": ["标题", "平台", "曝光", "点赞", "收藏", "评论", "48h 结论"],
        "归因提示": tips or ["各项尚可，重点看 48 小时后的点击率和收藏率。"],
        "观察": ["点击率 < 8% → 改 L2 钩子", "点击高但收藏低 → 回 L1 补「具体」", "收藏高但涨粉少 → L3 加 C 版立场"],
    }


# ---------------------------------------------------------------- HTTP
# ---------------------------------------------------------------- 一次调用出稿
FMT_HINT = """严格按下面格式输出，不要任何多余的话、不要代码块：

【类型】干货 或 事件
【标题】
【封面】
【正文】
【标签】
【口播】"""


def parse_single(text):
    parts = re.split(r"【(类型|标题|封面|正文|标签|口播)】", text or "")
    d = {}
    for i in range(1, len(parts) - 1, 2):
        d[parts[i]] = parts[i + 1].strip()
    return {
        "类型": "事件" if "事件" in d.get("类型", "") else "干货",
        "标题": d.get("标题", ""),
        "封面大字": d.get("封面", ""),
        "正文": d.get("正文", ""),
        "标签": d.get("标签", ""),
        "口播稿": d.get("口播", ""),
    }


def run_single(topic, mock=False, use="writer"):
    """判类型 + 出标题 + 写成稿，一次调用搞定。use=writer(K3) 或 polisher(DeepSeek)"""
    if mock:
        return {"类型": "干货", "标题": "示例标题", "封面大字": "示例",
                "正文": "示例正文。", "标签": "#示例", "口播稿": "示例口播。"}
    w = CONFIG_WRITER if use == "writer" else CONFIG_POLISHER
    prompt = f"""素材：{topic}

先自己判断这份素材属于哪一类：
- 干货：素材指向「教一个方法 / 纠正一个做错的做法」，读者看完该学会做点什么
- 事件：素材指向「一个现象 / 一个判断 / 一个槽点 / 一个共鸣」，读者看完该想说点什么

然后按对应规范写。

【判为干货，用这套】
{STYLE_DRY}

【判为事件，用这套】
{STYLE_EVENT}

通用要求：从具体当下的事切入，不宏大叙事；数字用阿拉伯数字；知识要「随手掏」——说「我突然想起来」，不说「下面介绍」；编不出来的真实信息（名字 / 年龄 / 具体数字）写【】留空，不要瞎编。

小红书正文 350-600 字，视频号口播 180-220 字。

{FMT_HINT}"""
    raw = call_model([{"role": "system", "content": SYS_TEXT}, {"role": "user", "content": prompt}],
                     w["key"], w["model"], w["api"], want_json=False, temperature=0.9)
    return parse_single(raw)


def tone_polish(text, kind, mock=False):
    """只改语气，不动内容。用 K3 做这一层。"""
    if mock or not text:
        return text
    w = CONFIG_WRITER
    style = STYLE_DRY if kind == "干货" else STYLE_EVENT
    prompt = f"""下面是初稿。请只改语气，不动内容。

{style}

硬要求：改完还是同一篇，观点、事实、数字、步骤一条都不许丢，也不许加新信息。
直接输出改好的正文，不要解释、不要加标题。"""

    raw = call_model([{"role": "system", "content": SYS_TEXT},
                      {"role": "user", "content": prompt + "\n\n初稿：\n" + text}],
                     w["key"], w["model"], w["api"], want_json=False, temperature=0.9)
    return (raw or "").strip() or text


# ---------------------------------------------------------------- 范文库 + 多写法生成
def _load_styles():
    try:
        return json.loads((ROOT / "styles.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
STYLES = _load_styles()

SYS_PERSONA = """你是赤舟国际教育（长沙）的老师，带小学 3-6 年级的普娃，家长普遍焦虑。
你说话直接、有点不耐烦，但真心为孩子着急。
你举的例子都有具体细节：时间、场景、孩子亲口说的话、真实的数字。
你不说官话套话，不堆金句，不喊口号。
编不出来的真实信息（名字 / 年龄 / 数字）写【】留空，绝不瞎编。"""


def parse_multi(text):
    """把「=====」分隔的多版本文本解析成 {写法: {标题,封面,正文,标签}}"""
    out = {}
    blocks = [b.strip() for b in re.split(r"=+\s*", text or "") if b.strip()]
    for b in blocks:
        d = {}
        for tag in ("写法", "标题", "封面", "正文", "标签"):
            m = re.search(r"【" + tag + r"】\s*(.*?)(?=\n【|\Z)", b, re.S)
            d[tag] = m.group(1).strip() if m else ""
        name = d.get("写法", "").strip()
        if name:
            out[name] = {"标题": d.get("标题", ""), "封面大字": d.get("封面", ""),
                         "正文": d.get("正文", ""), "标签": d.get("标签", "")}
    return out


def run_multi(topic, mode, style_keys, mock=False):
    """一次调用，同时生成多种写法。"""
    style_keys = [s for s in style_keys if s in STYLES] or list(STYLES.keys())
    if mock:
        return {k: {"标题": "示例标题", "封面大字": "示例", "正文": "示例正文。", "标签": "#示例"} for k in style_keys}

    w = CONFIG_WRITER
    style_block = ""
    for k in style_keys:
        s = STYLES[k]
        style_block += f"\n【{k}】{s['desc']}\n范文（学口吻，不学内容）：\n标题：{s['title']}\n{s['body']}\n"

    if mode == "改写":
        mode_desc = "这是一条爆款标题。先想它为什么能爆（钩子、选题角度），保留这个骨架，但用你自己的话、你的赛道、你的真实经验重写。"
    else:
        mode_desc = "这是你自己要写的素材，按素材展开成文。"

    prompt = f"""素材：{topic}

{mode_desc}

要写下面 {len(style_keys)} 种写法，每种的口吻照着各自的范文：
{style_block}

先想清楚（最重要的一步）：每种写法，这一版的核心观点是什么？用一句话说出来。全文都围绕这一句话，不跑题、不重复。

内容结构（每种写法固定骨架）：
- 立场型：具体场景(1-2句) → 一句尖锐的立场 → 两个支撑理由 → 反问收尾
- 反讽型：具体场景 → 点破某个词的荒诞 → 一句反讽 → 反问收尾
- 共情型：先承认情绪 → 再讲道理 → 给台阶 → 温和提问
- 短打型：一句钩子 → 核心观点 → 金句 → 反问
- 干货型：痛点场景 → 一句结论(方法名) → 方法步骤 → 收藏引导+报年级

清晰度要求：
1 开头 2-3 句内，读者要知道这版在说什么（要么亮观点，要么抛矛盾）。
2 每段只讲一件事，段和段有明确的逻辑顺序，别跳。
3 结尾回到核心观点，抛一个能讨论的问题。

行文铁律：
1 开头从具体当下的事切入，能用家长原话就引原话，不宏大叙事。
2 段落有长有短，至少一段 5 字以内独立成段。
3 口语词自然（其实、说实话、反正、挺、吧、嘛），但不硬凑。
4 有一处自我修正或不确定（「我一开始也以为」「这个我不打包票」）。
5 不用冒号、破折号、双引号，用「」。
6 数字用阿拉伯数字。编不出来的真实信息写【】留空。

正文 400-550 字，短打型是 180-220 字的口播稿。

写完后自查一遍：读者能不能用一句话复述这版在说什么？不能，就重写开头，把观点提前。

严格按下面格式输出，每个版本之间用单独一行「=====」隔开，不要代码块：
【写法】立场型
【标题】
【封面】
【正文】
【标签】"""
    raw = call_model([{"role": "system", "content": SYS_PERSONA},
                      {"role": "user", "content": prompt}],
                     w["key"], w["model"], w["api"], want_json=False, temperature=0.9)
    return parse_multi(raw)


def k3_review(versions, mock=False):
    """K3 通顺审核：把不通顺、读不懂的句子改通顺。失败就原样返回，绝不报错。"""
    if not versions or mock:
        return versions, False
    keys = [k for k in versions.keys() if (versions[k].get("正文") or "").strip()]
    if not keys:
        return versions, False
    blocks = "\n\n".join(f"【{k}】\n{versions[k]['正文']}" for k in keys)
    w = CONFIG_REVIEWER
    prompt = f"""下面是几段小红书文案。逐段重写，只做一件事：把不通顺、读不懂、拗口的句子改得通顺、好懂。

硬要求：
- 只改通顺度和可读性。观点、事实、数字、语气、口语感、结构一律不动。
- 一段里如果本来就通顺，就原样保留，别乱改。
- 不要加新信息，不要删原有信息。

输出格式：每段先写【标签】再写改好的正文，标签和原文一致。

{blocks}"""
    try:
        raw = call_model([{"role": "system", "content": "你是文案编辑，只做润色，不改内容。"},
                          {"role": "user", "content": prompt}],
                         w["key"], w["model"], w["api"], want_json=False, temperature=0.3)
    except Exception:
        return versions, False
    reviewed = {}
    for m in re.finditer(r"【(.+?)】\s*(.*?)(?=\n【|\Z)", raw or "", re.S):
        reviewed[m.group(1).strip()] = m.group(2).strip()
    if not reviewed:
        return versions, False
    changed = False
    for k in keys:
        if k in reviewed and reviewed[k] and reviewed[k] != versions[k].get("正文", ""):
            versions[k]["正文"] = reviewed[k]
            changed = True
    return versions, changed


class Handler(BaseHTTPRequestHandler):
    server_version = "BaokuanStudio"

    def log_message(self, *a):
        pass

    def _auth(self):
        """设了 ACCESS_CODE 就校验，否则放行。"""
        if not ACCESS_CODE:
            return True
        code = self.headers.get("X-Access-Code") or ""
        if not code and "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if kv.startswith("code="):
                    code = kv[5:]
        return code == ACCESS_CODE

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path.split("?")[0]
        name = "auto.html" if p in ("/", "/auto.html") else p.lstrip("/")
        # 素材库和接口要校验访问码，页面本身放行（页面会弹框要码）
        if name in ("library.json",) and not self._auth():
            self._send(401, {"error": "需要访问码"})
            return
        f = (ROOT / name).resolve()
        if not str(f).startswith(str(ROOT.resolve())) or not f.is_file():
            self._send(404, {"error": "not found"})
            return
        ctype = {"html": "text/html; charset=utf-8", "json": "application/json; charset=utf-8"}.get(f.suffix.lstrip("."), "text/plain; charset=utf-8")
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self):
        if self.path != "/api/run":
            self._send(404, {"error": "not found"})
            return
        if not self._auth():
            self._send(401, {"error": "需要访问码"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            self._send(400, {"error": f"bad json: {e}"})
            return

        topic = (req.get("素材") or req.get("topic") or "").strip()
        mode = (req.get("模式") or req.get("mode") or "原创").strip()
        style_keys = req.get("写法") or req.get("styles") or list(STYLES.keys())
        mock = bool(req.get("mock")) or SERVER_MOCK
        if not topic:
            self._send(400, {"error": "素材不能为空"})
            return
        try:
            versions = run_multi(topic, mode, style_keys, mock)
            review_on = req.get("审核", True) not in (False, "false", 0, "0", "no", "否")
            reviewed = False
            if review_on and not mock:
                versions, reviewed = k3_review(versions, mock)
            checked = {}
            for k, v in versions.items():
                rep = scan_l4(v.get("正文", ""))
                rep["AI味"] = scan_ai(v.get("正文", ""))
                checked[k] = {"通过": rep["通过"], "字数": rep["字数"],
                              "口语词": rep["AI味"]["口语词"], "像真人": rep["AI味"]["像真人"]}
            self._send(200, {"ok": True, "versions": versions, "质检": checked, "已审核": reviewed})
        except ModelError as e:
            self._send(502, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


SERVER_MOCK = False

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8770")))
    # 云端会注入 PORT，此时默认监听 0.0.0.0；本机跑仍用 127.0.0.1
    _default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    ap.add_argument("--host", default=os.environ.get("HOST", _default_host))
    ap.add_argument("--mock", action="store_true", help="不调模型，用假数据验证界面")
    a = ap.parse_args()
    SERVER_MOCK = a.mock
    print(f"爆款文案工作台  →  http://{a.host}:{a.port}")
    print(f"模型 {CONFIG_MODEL} ｜ {'MOCK 模式' if a.mock else '真实调用'}")
    print(f"访问码 {'已开启' if ACCESS_CODE else '未设置（仅限本机使用）'}")
    print("Ctrl+C 停止")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
