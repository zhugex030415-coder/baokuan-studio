#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刷新素材（分板块 + 打标签 + 去重 + 沉淀）。

每天一次：爬小红书两个板块 → 给新内容打「主题 / 钩子类型」标签 →
与历史库去重（标题一致跳过）→ 追加进 素材库.json。

产出：素材库.json（累积），供 团队 UI 的「选题雷达」读取。
"""
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
MC = Path(r"D:\deepseek\AI探索研究\MediaCrawler")
PY = MC / ".venv" / "Scripts" / "python.exe"

CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
KEY = CONFIG.get("key", "")
API = CONFIG.get("api", "https://api.deepseek.com/chat/completions")
MODEL = CONFIG.get("model", "deepseek-chat")

# 只保留 2025-10-01 之后发布的笔记（本地时区）
CUTOFF_TS = datetime.datetime(2025, 10, 1).timestamp()
CUTOFF_STR = "2025-10-01"
MIN_LIKES = 20

BOARDS = [
    ("原版教材·考试", [
        # 考试
        "KET", "PET", "FCE", "剑桥少儿英语", "剑桥英语", "小小托福",
        # 教材 / 分级读物
        "Power Up", "Think 教材", "Reading Explorer", "新概念英语",
        "原版教材", "原版阅读", "分级阅读", "牛津树", "RAZ", "海尼曼",
        # 能力项
        "自然拼读", "音标", "音标发音", "字母音", "单词记忆", "背单词",
        # 启蒙
        "英语启蒙", "英语启蒙路线", "磨耳朵", "英语动画", "英语绘本",
        # 年级 / 提分
        "小学英语", "小学英语提分", "英语超前学", "四年级英语", "五年级英语", "六年级英语",
    ]),
    ("长沙本地", [
        # 学段
        "长沙小学", "长沙幼升小", "长沙小升初", "长沙中考",
        # 政策 / 择校
        "长沙教育", "长沙教育新政", "长沙择校", "长沙学区",
        "长沙民办小学", "长沙公办小学", "长沙小学排名", "长沙阳光分班",
        # 学校
        "长沙初中", "长郡", "雅礼", "长郡双语", "雅礼中学", "湖南师大附中", "麓山国际",
        # 家长 / 英语
        "长沙家长", "长沙英语", "长沙英语培训", "长沙小学英语", "长沙升学",
    ]),
]
BATCH_SIZE = 6    # 每批关键词数，小批更不容易触发安全验证
BATCH_SLEEP = 30  # 批间停顿（秒），给人工过验证留时间
PER_RUN = 6       # 每板块每次轮换抓几个词（合计 12 个，接近账号单次上限）

# 干净的两维标签：主题（内容方向）+ 钩子（为什么能爆）
THEMES = ["自然拼读", "单词记忆", "语法", "阅读", "考试备考", "启蒙方法", "长沙本地", "其他"]
HOOKS = ["反常识", "清单干货", "故事", "对比", "避坑", "揭秘", "其他"]


def parse_num(s):
    s = str(s or "0").replace("+", "").strip()
    if "万" in s:
        try:
            return int(float(s.split("万")[0]) * 10000)
        except Exception:
            return 0
    try:
        return int(s)
    except Exception:
        return 0


def norm(t):
    """去空白、去 emoji 和标点，用于去重比较。"""
    s = re.sub(r"[\s\u3000]+", "", t or "")
    s = re.sub(r"[\u2600-\u27BF\uD800-\uDFFF\uFE0F]", "", s)
    s = re.sub(r"[，。！？、：；,.!?;:()（）【】\[\]「」『』""''·…—\-_#@|/]", "", s)
    return s


def crawl(kws):
    """一次浏览器里抓多个关键词（逗号分隔），避免反复开浏览器。"""
    kw = kws if isinstance(kws, str) else ",".join(kws)
    cmd = [str(PY), "main.py", "--platform", "xhs", "--type", "search",
           "--keywords", kw, "--crawler_max_notes_count", "20",
           "--get_comment", "false", "--save_data_option", "jsonl"]
    try:
        r = subprocess.run(cmd, cwd=str(MC), capture_output=True, timeout=1200)
        # 把爬虫内部输出打出来（诊断验证/登录问题）
        err = (r.stderr or b"").decode("utf-8", "ignore")
        out = (r.stdout or b"").decode("utf-8", "ignore")
        tail = (out + err).strip().splitlines()[-12:]
        if tail:
            print("  [爬虫输出]")
            for ln in tail:
                try:
                    safe = ln.encode("gbk", "ignore").decode("gbk", "ignore")
                    print("   ", safe[:160], flush=True)
                except Exception:
                    pass
        if r.returncode != 0:
            print(f"  [warn] 退出码 {r.returncode}")
    except Exception as e:
        print(f"  [warn] 抓取失败：{e}")


def load_rows():
    d = MC / "data" / "xhs" / "jsonl"
    rows = []
    for f in sorted(d.glob("search_contents_*.jsonl"))[-4:]:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _tag_chunk(titles):
    """给一小批标题打标签（主题 + 钩子），只接受白名单内的值。"""
    if not titles or not KEY:
        return {}
    prompt = (
        "给下面每条小红书标题打两个标签，只能从给定选项里选。\n"
        f"主题：{'、'.join(THEMES)}\n"
        f"钩子：{'、'.join(HOOKS)}\n"
        "主题规则：发音/音标/拼读规则→自然拼读；记单词/背单词→单词记忆；"
        "语法/时态/句型→语法；原版书/分级/绘本/阅读→阅读；KET/PET/FCE/剑桥/考试→考试备考；"
        "启蒙路线/磨耳朵/动画儿歌→启蒙方法；长沙政策/学校/择校→长沙本地；都不沾→其他。\n"
        "钩子规则：反常理→反常识；合集/完整版/N个/保姆级→清单干货；讲一个学生或家长的故事→故事；"
        "A和B对比/算账→对比；劝退/别再/千万别→避坑；内幕/真相/揭秘→揭秘；都不沾→其他。\n\n"
    )
    for i, t in enumerate(titles, 1):
        prompt += f"{i}. {t}\n"
    prompt += '\n只输出 JSON：{"tags":[{"i":1,"theme":"...","hook":"..."}]}'
    payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "response_format": {"type": "json_object"},
               "max_tokens": 2000}
    if "v4-pro" in str(MODEL).lower() or "reasoner" in str(MODEL).lower():
        payload["reasoning_effort"] = "none"   # 打标签无需深度思考，关掉才快
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
    except Exception as e:
        print("  [warn] 打标签失败：", e)
        return {}
    try:
        obj = json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return {}
    arr = obj.get("tags") if isinstance(obj, dict) else None
    if not isinstance(arr, list):
        arr = obj.get("result") if isinstance(obj, dict) else None
    if not isinstance(arr, list) and isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list):
                arr = v
                break
    if not isinstance(arr, list):
        return {}
    out = {}
    for it in arr:
        if not isinstance(it, dict):
            continue
        idx = it.get("i")
        if isinstance(idx, str) and idx.isdigit():
            idx = int(idx)
        if not (isinstance(idx, int) and 1 <= idx <= len(titles)):
            continue
        th = str(it.get("theme", "")).strip()
        hk = str(it.get("hook", "")).strip()
        out[titles[idx - 1]] = (th if th in THEMES else "其他", hk if hk in HOOKS else "其他")
    return out


def tag_batch(titles, chunk=12):
    """分批打标签，每批 chunk 条，避免一次太多导致失败。"""
    out = {}
    for i in range(0, len(titles), chunk):
        out.update(_tag_chunk(titles[i:i + chunk]))
        time.sleep(1)
    return out


def load_library():
    f = HERE / "library.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def build_and_append(rows, kw2board, do_crawl_log=True):
    lib = load_library()
    # 只保留 2025-10 之后的历史，老的一律清掉
    lib = [i for i in lib if str(i.get("first_seen", "") or "") >= CUTOFF_STR]
    seen = {norm(i.get("title")) for i in lib}
    new = []
    for r in rows:
        kw = r.get("source_keyword") or ""
        board = kw2board.get(kw)
        if not board:
            continue
        t = (r.get("title") or "").strip()
        if len(t) < 6:
            continue
        nk = norm(t)
        if not nk or nk in seen:
            continue
        ts = r.get("time") or 0
        if ts and ts / 1000 < CUTOFF_TS:
            continue   # 2025-10 之前的跳过
        likes = parse_num(r.get("liked_count"))
        if likes < MIN_LIKES:
            continue
        dstr = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else "?"
        new.append({"title": t, "likes": likes, "collected": parse_num(r.get("collected_count")),
                    "board": board, "url": r.get("note_url") or "",
                    "first_seen": dstr})
        seen.add(nk)
    if new:
        tags = tag_batch([n["title"] for n in new])
        for n in new:
            n["theme"], n["hook"] = tags.get(n["title"], ("其他", "其他"))
        lib.extend(new)
    lib.sort(key=lambda x: -x.get("likes", 0))
    (HERE / "library.json").write_text(json.dumps(lib, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(lib), len(new)


def main():
    kw2board = {}
    for b, kws in BOARDS:
        for kw in kws:
            kw2board[kw] = b

    # 每日按日期轮换，每次每板块抓 PER_RUN 个词（账号单次约 12 词上限）
    seed = datetime.date.today().toordinal()
    chosen = []
    for b, kws in BOARDS:
        n = len(kws)
        start = (seed * PER_RUN) % n   # 每天往后挪一整批，避免重叠
        pick = [kws[(start + i) % n] for i in range(PER_RUN)]
        print(f"【{b}】本次：{'、'.join(pick)}", flush=True)
        chosen.extend(pick)

    nbatch = (len(chosen) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\n共 {len(chosen)} 个词，分 {nbatch} 批…", flush=True)
    for i in range(0, len(chosen), BATCH_SIZE):
        batch = chosen[i:i + BATCH_SIZE]
        print(f"\n[第 {i // BATCH_SIZE + 1}/{nbatch} 批] {'、'.join(batch)}", flush=True)
        crawl(batch)
        if i + BATCH_SIZE < len(chosen):
            print(f"  …停顿 {BATCH_SLEEP}s（有安全验证就趁现在过）", flush=True)
            time.sleep(BATCH_SLEEP)

    rows = load_rows()
    total, added = build_and_append(rows, kw2board)
    print(f"\n完成。素材库共 {total} 条，本次新增 {added} 条。")
    print("打开团队 UI 的「选题雷达」即可看到分板块 + 标签。")


if __name__ == "__main__":
    main()
