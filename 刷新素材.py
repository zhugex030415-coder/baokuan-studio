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
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
MC = Path(r"D:\deepseek\AI探索研究\MediaCrawler")
PY = MC / ".venv" / "Scripts" / "python.exe"

CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
KEY = CONFIG.get("key", "")
API = CONFIG.get("api", "https://api.deepseek.com/chat/completions")
MODEL = CONFIG.get("model", "deepseek-chat")

BOARDS = [
    ("原版教材·考试", ["KET", "PET", "Power Up 教材", "Think 教材", "原版教材",
                       "自然拼读", "Reading Explorer", "剑桥英语"]),
    ("长沙本地", ["长沙小升初", "长沙小学排名", "长沙阳光分班", "长沙教育新政",
                  "长沙择校", "长沙初中", "长郡", "雅礼"]),
]
PER_RUN = 4

THEMES = ["单词记忆", "自然拼读", "语法", "听力", "口语", "阅读·原版书", "写作",
          "KET·PET", "启蒙方法", "动画·儿歌", "学习习惯", "选课·选班",
          "择校", "政策", "分班", "学校", "其他"]
HOOKS = ["反常识", "划线", "劝退", "揭秘", "对比", "算账", "自检", "清单干货", "故事亲历", "其他"]


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


def crawl(kw):
    cmd = [str(PY), "main.py", "--platform", "xhs", "--type", "search",
           "--keywords", kw, "--crawler_max_notes_count", "15",
           "--get_comment", "false", "--save_data_option", "jsonl"]
    try:
        subprocess.run(cmd, cwd=str(MC), capture_output=True, timeout=300)
    except Exception as e:
        print(f"  [warn] {kw} 失败：{e}")


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


def tag_batch(titles):
    """用 DeepSeek 给一批标题打「主题 / 钩子」标签。"""
    if not KEY:
        return {}
    prompt = ("下面是小红书教育类标题。给每条打两个标签。\n"
              "主题从这些里选一个：%s\n"
              "钩子类型从这些里选一个：%s\n\n" % ("、".join(THEMES), "、".join(HOOKS)))
    for i, t in enumerate(titles, 1):
        prompt += f"{i}. {t}\n"
    prompt += ('\n只输出 JSON 数组：[{"i":序号,"theme":"主题","hook":"钩子类型"}]，不要解释。')
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "response_format": {"type": "json_object"},
                       "max_tokens": 3000}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            txt = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
    except Exception as e:
        print("  [warn] 打标签失败：", e)
        return {}
    try:
        obj = json.loads(txt)
    except Exception:
        # 兜底：从文本里抠数组
        m = re.search(r"\[.*\]", txt, re.S)
        if not m:
            return {}
        obj = json.loads(m.group(0))
    # 兼容 {"result":[...]} 或直接 [...]
    arr = obj.get("result", obj) if isinstance(obj, dict) else obj
    if isinstance(arr, dict):
        arr = list(arr.values())[0] if arr else []
    out = {}
    for it in arr:
        if isinstance(it, dict) and "i" in it:
            idx = it["i"]
            if isinstance(idx, str) and idx.isdigit():
                idx = int(idx)
            if isinstance(idx, int) and 1 <= idx <= len(titles):
                out[titles[idx - 1]] = (it.get("theme", "其他"), it.get("hook", "其他"))
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
        likes = parse_num(r.get("liked_count"))
        if likes < 150:
            continue
        ts = r.get("time") or 0
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
    seed = datetime.date.today().toordinal()

    print("开始分板块抓取…")
    for b, kws in BOARDS:
        n = len(kws)
        start = seed % n
        chosen = [kws[(start + i) % n] for i in range(PER_RUN)]
        print(f"\n【{b}】本次：{'、'.join(chosen)}")
        for kw in chosen:
            print(f"  抓 {kw} …")
            crawl(kw)

    rows = load_rows()
    total, added = build_and_append(rows, kw2board)
    print(f"\n完成。素材库共 {total} 条，本次新增 {added} 条。")
    print("打开团队 UI 的「选题雷达」即可看到分板块 + 标签。")


if __name__ == "__main__":
    main()
