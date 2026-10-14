#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MP3 乱码标签清理工具
====================

作用
----
把 MP3 里显示为乱码的 ID3 标签删掉，让播放器回退去显示文件名。

不重新解码、不猜原始编码，只做「删除」这一件事，所以不会猜错。

两种清理方式
------------
1. 清除全部标签      —— 删掉整个 ID3v2 + ID3v1 标签（可选择保留封面图片）。
                        最彻底，播放器一定显示文件名。
2. 仅删除乱码标签    —— 只删被判定为乱码的那些字段，正常的标签保留。

注意
----
直接修改原文件，不生成备份，写入后无法撤销。不确定的话先跑只读的命令行模式
看一眼（不加 --strip / --strip-garbled 就是只读），或者自己先复制一份。

用法
----
    python mp3_tag_cleaner.py                 # 图形界面
    python mp3_tag_cleaner.py a.mp3 b.mp3     # 命令行：只看不删
    python mp3_tag_cleaner.py --strip *.mp3   # 命令行：清除全部标签
    python mp3_tag_cleaner.py --strip-garbled *.mp3
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

try:
    from mutagen.id3 import ID3, APIC, COMM, USLT, TXXX, TextFrame, ID3NoHeaderError
except ImportError:
    sys.stderr.write(
        "缺少 mutagen，请先安装：\n    pip install mutagen\n"
    )
    raise SystemExit(1)


def fix_console_encoding() -> None:
    """让命令行能打印出乱码字符本身。

    Windows 控制台默认是 GBK（cp936），而乱码文本里全是 `Ò¹Çú` 这种
    GBK 编不出来的 Latin-1 字符，直接 print 会抛 UnicodeEncodeError ——
    偏偏这些字符正是用户要看的东西。
    """
    encoding = None
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            if kernel32.GetConsoleOutputCP() == 65001:
                encoding = "utf-8"
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            # encoding=None 表示沿用原编码，只把报错改成替换
            stream.reconfigure(encoding=encoding, errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------------
# 乱码判定
# --------------------------------------------------------------------------

# Latin-1 / cp1252 区间里那些“正常中文和英文标题里几乎不会出现”的字符。
# 一段文本里这类字符占比很高，基本就是 GBK / UTF-8 字节被当成 Latin-1 读了。
_CP1252_ONLY = {
    0x0192, 0x02C6, 0x02DC, 0x0152, 0x0153, 0x0160, 0x0161,
    0x0178, 0x017D, 0x017E, 0x2020, 0x2021, 0x2030, 0x2039,
    0x203A, 0x2122, 0xFFFD,
}
# 0xA0-0xBF 段：¡¢£¤¥¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿
# 0xC0-0xFF 段：À-ÿ（德语/法语标题可能真有，所以阈值放宽）
_HIGH_LATIN = set(range(0xC0, 0x100))
_MID_LATIN = set(range(0xA0, 0xC0))


def is_garbled(text: str, threshold: float = 0.30) -> bool:
    """粗略判断一段标签文本是不是乱码。"""
    if not text:
        return False
    # 1) 出现了替换字符，说明解码时就已经坏了
    if "�" in text:
        return True
    # 2) 出现了 cp1252 独有字符（Œ š Ž ‹ › ƒ ˜ † ‡ ‰ 之类），正常标题不会有
    if sum(1 for c in text if ord(c) in _CP1252_ONLY) >= 1:
        return True
    # 3) 高位拉丁字符占比过高
    total = sum(1 for c in text if not c.isspace())
    if total == 0:
        return False
    high = sum(1 for c in text if ord(c) in _HIGH_LATIN)
    mid = sum(1 for c in text if ord(c) in _MID_LATIN)
    if high / total >= threshold:
        return True
    if (high + mid) / total >= 0.45:
        return True
    return False



# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

FRAME_LABEL = {
    "TIT2": "标题", "TPE1": "艺术家", "TALB": "专辑", "TPE2": "专辑艺术家",
    "TCON": "流派", "TRCK": "音轨号", "TPOS": "碟号", "TDRC": "年份",
    "TYER": "年份", "TCOM": "作曲", "TCOP": "版权", "TPUB": "发行商",
    "TSSE": "编码器", "TENC": "编码", "TBPM": "BPM", "TLAN": "语言",
    "TIT1": "分组", "TIT3": "副标题", "TSRC": "ISRC", "TOPE": "原艺术家",
    "TOAL": "原专辑", "TEXT": "作词", "TPE3": "指挥", "TKEY": "调号",
    "TSST": "套装", "MVNM": "乐章名", "COMM": "注释", "USLT": "歌词",
    "TXXX": "自定义", "WXXX": "自定义链接", "WCOM": "商业信息",
    "WOAR": "艺术家网址", "APIC": "封面图片", "PCNT": "播放次数",
}

# 会被清理的「文字类」帧
TEXT_FRAME_IDS = {"TIT2", "TPE1", "TALB", "TPE2", "TCON", "TRCK", "TPOS",
                  "TDRC", "TYER", "TCOM", "TCOP", "TPUB", "TSSE", "TENC",
                  "TBPM", "TLAN", "TIT1", "TIT3", "TSRC", "TOPE", "TOAL",
                  "TEXT", "TPE3", "TKEY", "TSST", "MVNM", "COMM", "USLT",
                  "TXXX", "WXXX", "WCOM", "WOAR"}


def _frame_value(fr) -> str:
    """把 mutagen 的帧对象转成给人看的字符串。"""
    if isinstance(fr, APIC):
        return f"[图片 {fr.mime or '?'}, {len(fr.data)} 字节]"
    if isinstance(fr, TXXX):
        desc = fr.desc or ""
        val = " / ".join(fr.text) if fr.text else ""
        return f"{desc} = {val}" if desc else val
    if isinstance(fr, COMM):
        txt = fr.text[0] if isinstance(fr.text, list) and fr.text else (fr.text or "")
        return f"[{fr.desc or '注释'}] {txt}" if fr.desc else txt
    if isinstance(fr, USLT):
        head = fr.text[:40] if isinstance(fr.text, str) else ""
        return (fr.desc + ": " if fr.desc else "") + head + ("…" if len(fr.text or "") > 40 else "")
    if isinstance(fr, TextFrame):
        return " / ".join(fr.text)
    return str(fr)[:200]


def read_v1_raw(path: str):
    """读取文件末尾的 ID3v1 块原始字节，没有就返回 None。"""
    try:
        size = os.path.getsize(path)
        if size < 128:
            return None
        with open(path, "rb") as f:
            f.seek(-128, 2)
            blk = f.read(128)
    except OSError:
        return None
    return blk if blk[:3] == b"TAG" else None


def read_v1_fields(path: str):
    """返回 [(字段名, 文本)]，按播放器显示的样子解码（Latin-1）。"""
    blk = read_v1_raw(path)
    if blk is None:
        return None
    spec = [("标题", 3, 33), ("艺术家", 33, 63), ("专辑", 63, 93),
            ("年份", 93, 97), ("注释", 97, 127)]
    out = []
    for name, a, b in spec:
        raw = blk[a:b].rstrip(b"\x00 ")
        if raw:
            out.append((name, raw.decode("latin-1", "replace")))
    return out


def analyze(path: str) -> dict:
    """扫描一个文件，返回标签概况。"""
    info = {
        "path": path,
        "name": os.path.basename(path),
        "has_v2": False,
        "has_v1": False,
        "frames": [],        # [(fid, 中文名, 内容, 是否乱码)]
        "v1_fields": [],     # [(字段名, 文本, 是否乱码)]
        "garbled": 0,
        "error": None,
    }
    try:
        tag = ID3(path)
        info["has_v2"] = True
        for key in tag.keys():
            # mutagen 的 keys() 给的是哈希键（COMM::eng、APIC:cover、TXXX:xxx），
            # 要砍掉冒号后面那截才是真正的帧 ID
            fid = key.split(":", 1)[0]
            for fr in tag.getall(key):
                label = FRAME_LABEL.get(fid, fid)
                value = _frame_value(fr)
                bad = fid in TEXT_FRAME_IDS and is_garbled(value)
                info["frames"].append((fid, label, value, bad))
                if bad:
                    info["garbled"] += 1
    except ID3NoHeaderError:
        pass
    except Exception as exc:                      # 损坏的标签
        info["error"] = str(exc)

    v1 = read_v1_fields(path)
    if v1:
        info["has_v1"] = True
        for name, text in v1:
            bad = is_garbled(text)
            info["v1_fields"].append((name, text, bad))
            if bad:
                info["garbled"] += 1

    # 给列表显示用的摘要
    d = {fid: value for fid, _l, value, _b in info["frames"]}
    info["title"] = d.get("TIT2", "")
    info["artist"] = d.get("TPE1", "")
    info["album"] = d.get("TALB", "")
    if not info["has_v2"] and not info["has_v1"]:
        info["status"] = "无标签"
    elif info["garbled"]:
        info["status"] = f"乱码 {info['garbled']} 处"
    else:
        info["status"] = "正常"
    return info


# --------------------------------------------------------------------------
# 写入：字节级删除 ID3v2 / ID3v1
# --------------------------------------------------------------------------

def _syncsafe(b: bytes) -> int:
    return ((b[0] & 0x7F) << 21) | ((b[1] & 0x7F) << 14) | ((b[2] & 0x7F) << 7) | (b[3] & 0x7F)


def remove_id3v2_raw(path: str) -> bool:
    """把文件开头的 ID3v2 标签整块切掉，音频数据原样保留。"""
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if len(head) < 10 or head[:3] != b"ID3":
                return False
            size = _syncsafe(head[6:10])
            flags = head[5]
        tag_len = 10 + size + (10 if (flags & 0x10) else 0)   # 0x10 = 有 footer

        tmp = path + ".tmp_clean~"
        with open(path, "rb") as fi, open(tmp, "wb") as fo:
            fi.seek(tag_len)
            shutil.copyfileobj(fi, fo, 1024 * 512)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def remove_id3v1_raw(path: str) -> bool:
    """砍掉文件末尾 128 字节的 ID3v1 块。"""
    try:
        size = os.path.getsize(path)
        if size < 128:
            return False
        with open(path, "r+b") as f:
            f.seek(-128, 2)
            if f.read(3) != b"TAG":
                return False
            f.truncate(size - 128)
        return True
    except OSError:
        return False


def write_id3v2(path: str, frames) -> None:
    """用给定的帧新建一个干净的 ID3v2 标签。"""
    tag = ID3()
    for fr in frames:
        tag.add(fr)
    tag.save(path, v1=0)


# --------------------------------------------------------------------------
# 清理动作
# --------------------------------------------------------------------------

def strip_all(path: str, keep_cover: bool = True, remove_v1: bool = True,
              title_from_filename: bool = False) -> str:
    """清除全部标签。返回一句结果说明。"""
    info = analyze(path)
    if not info["has_v2"] and not info["has_v1"]:
        return "跳过（本来就没有标签）"

    covers = []
    if keep_cover and info["has_v2"]:
        try:
            covers = list(ID3(path).getall("APIC"))
        except Exception:
            covers = []

    new_frames = list(covers)
    if title_from_filename:
        from mutagen.id3 import TIT2
        new_frames.append(TIT2(encoding=3, text=[os.path.splitext(os.path.basename(path))[0]]))

    removed_v2 = remove_id3v2_raw(path)
    removed_v1 = remove_id3v1_raw(path) if remove_v1 else False

    if new_frames:
        write_id3v2(path, new_frames)

    parts = []
    if removed_v2:
        parts.append("ID3v2")
    if removed_v1:
        parts.append("ID3v1")
    extra = []
    if keep_cover and covers:
        extra.append(f"保留封面 {len(covers)} 张")
    if title_from_filename:
        extra.append("标题=文件名")
    return "已删除 " + "+".join(parts) + ("（" + "，".join(extra) + "）" if extra else "")


def strip_garbled(path: str) -> str:
    """只删除判定为乱码的文字帧，正常标签原样保留。"""
    try:
        tag = ID3(path)
    except ID3NoHeaderError:
        tag = None
    except Exception as exc:
        return f"读取失败：{exc}"

    removed = []
    if tag is not None:
        for key in list(tag.keys()):
            fid = key.split(":", 1)[0]        # COMM::eng -> COMM
            if fid not in TEXT_FRAME_IDS:
                continue
            frames = list(tag.getall(key))
            keep = [fr for fr in frames if not is_garbled(_frame_value(fr))]
            if len(keep) != len(frames):
                removed.append(f"{fid}×{len(frames) - len(keep)}")
                if keep:
                    tag.setall(key, keep)
                else:
                    tag.delall(key)
        if not tag.keys():
            tag = None

    v1_change = False
    v1 = read_v1_fields(path)
    if v1:
        for name, text in v1:
            if is_garbled(text):
                v1_change = True
                break

    if not removed and not v1_change:
        return "跳过（没发现乱码）"

    if tag is not None:
        tag.save(path, v1=1)
    else:
        remove_id3v2_raw(path)

    if v1_change:
        # v1 里能安全清空的只有文字字段，直接整块去掉最省事
        remove_id3v1_raw(path)

    parts = []
    if removed:
        parts.append("ID3v2: " + " ".join(removed))
    if v1_change:
        parts.append("ID3v1 有乱码 → 已移除")
    return "已删除 " + "；".join(parts)


# ==========================================================================
# 图形界面
# ==========================================================================

def launch_gui():                                            # pragma: no cover
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("MP3 乱码标签清理工具")
            self.geometry("1080x740")
            self.minsize(860, 560)
            self.files: list[str] = []
            self.info: dict[str, dict] = {}
            # 打开后列表里只留有乱码的文件
            self.only_garbled = tk.BooleanVar(value=False)
            self._build()

        # ---------------- 布局 ----------------
        def _build(self):
            toolbar = ttk.Frame(self, padding=(8, 6))
            toolbar.pack(fill="x")
            ttk.Button(toolbar, text="添加文件", command=self.add_files).pack(side="left")
            ttk.Button(toolbar, text="添加文件夹", command=self.add_folder).pack(side="left", padx=(6, 0))
            ttk.Button(toolbar, text="移除选中", command=self.remove_selected).pack(side="left", padx=(6, 0))
            ttk.Button(toolbar, text="清空列表", command=self.clear_all).pack(side="left", padx=(6, 0))
            ttk.Button(toolbar, text="重新扫描", command=self.rescan).pack(side="left", padx=(6, 0))
            ttk.Checkbutton(toolbar, text="只看乱码文件", variable=self.only_garbled,
                            command=self.on_filter_toggle).pack(side="left", padx=(16, 0))

            # --- 文件列表 ---
            top = ttk.LabelFrame(self, text="文件", padding=6)
            top.pack(fill="both", expand=True, padx=8, pady=(0, 4))

            cols = ("name", "status", "title", "artist", "album")
            self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="extended")
            for key, text, width in (
                ("name", "文件名", 300), ("status", "状态", 100),
                ("title", "标题", 220), ("artist", "艺术家", 180), ("album", "专辑", 180),
            ):
                self.tree.heading(key, text=text)
                self.tree.column(key, width=width, anchor="w")
            self.tree.tag_configure("bad", foreground="#c00000")
            self.tree.tag_configure("none", foreground="#888888")

            tsb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=tsb.set)
            self.tree.pack(side="left", fill="both", expand=True)
            tsb.pack(side="right", fill="y")
            self.tree.bind("<<TreeviewSelect>>", self.on_select)

            # --- 预览 ---
            mid = ttk.LabelFrame(self, text="标签明细", padding=6)
            mid.pack(fill="both", expand=True, padx=8, pady=(0, 4))

            pcols = ("frame", "label", "value", "verdict")
            self.preview = ttk.Treeview(mid, columns=pcols, show="headings")
            for key, text, width in (
                ("frame", "帧", 90), ("label", "含义", 90),
                ("value", "内容", 560), ("verdict", "判定", 90),
            ):
                self.preview.heading(key, text=text)
                self.preview.column(key, width=width, anchor="w")
            self.preview.tag_configure("bad", foreground="#c00000")
            psb = ttk.Scrollbar(mid, orient="vertical", command=self.preview.yview)
            self.preview.configure(yscrollcommand=psb.set)
            self.preview.pack(side="left", fill="both", expand=True)
            psb.pack(side="right", fill="y")

            # --- 选项 + 操作 ---
            opts = ttk.Frame(self, padding=(10, 4))
            opts.pack(fill="x")
            self.opt_cover = tk.BooleanVar(value=True)
            self.opt_v1 = tk.BooleanVar(value=True)
            self.opt_rename = tk.BooleanVar(value=False)
            ttk.Checkbutton(opts, text="保留封面图片", variable=self.opt_cover).pack(side="left")
            ttk.Checkbutton(opts, text="同时清除 ID3v1", variable=self.opt_v1).pack(side="left", padx=12)
            ttk.Checkbutton(opts, text="用文件名重建标题", variable=self.opt_rename).pack(side="left", padx=12)

            actions = ttk.Frame(self, padding=(10, 4))
            actions.pack(fill="x")
            ttk.Button(actions, text="清除全部标签", command=self.do_strip_all).pack(side="left")
            ttk.Button(actions, text="仅删除乱码标签", command=self.do_strip_garbled).pack(side="left", padx=(8, 0))
            ttk.Button(actions, text="预览结果", command=self.do_preview).pack(side="left", padx=(8, 0))

            self.status = tk.StringVar(value="就绪。先添加文件，再选「清除全部标签」即可。")
            ttk.Label(self, textvariable=self.status, relief="sunken",
                      anchor="w", padding=(8, 4)).pack(fill="x", side="bottom")

        # ---------------- 文件管理 ----------------
        def add_files(self):
            paths = filedialog.askopenfilenames(
                title="选择 MP3 文件",
                filetypes=[("MP3 文件", "*.mp3"), ("所有文件", "*.*")])
            added = self._add(paths)
            self.rescan(keep_selection=False)
            hidden = 0
            if self.only_garbled.get():
                hidden = sum(1 for p in (os.path.abspath(x) for x in paths)
                             if self.info.get(p, {}).get("garbled") == 0)
            extra = f"，其中 {hidden} 个因为没乱码被「只看乱码文件」筛掉了" if hidden else ""
            self.status.set(f"新增 {added} 个文件{extra}。")

        def add_folder(self):
            folder = filedialog.askdirectory(title="选择文件夹（含子目录）")
            if not folder:
                return
            found = []
            for root, _dirs, names in os.walk(folder):
                for n in names:
                    if n.lower().endswith(".mp3"):
                        found.append(os.path.join(root, n))
            if not found:
                messagebox.showinfo("提示", "这个文件夹里没找到 .mp3 文件。")
                return

            added = self._add(found)
            # 先扫一遍拿到标签状况，再决定要不要开筛选
            self.only_garbled.set(False)
            self.rescan(keep_selection=False)
            n_bad = sum(1 for p in self.files if self.info.get(p, {}).get("garbled"))

            if not n_bad:
                self.status.set(f"扫描完成：新增 {added} 个，共 {len(self.files)} 个文件，没有发现乱码标签。")
                return

            # 自动筛出有乱码的，并全部选中 —— 写入仍然要手动点按钮。
            # 复用刚才的分析结果，不重新读盘。
            self.only_garbled.set(True)
            self._rebuild_tree(keep_selection=False)
            self.select_all_visible()
            self.status.set(
                f"扫描完成：新增 {added} 个，共 {len(self.files)} 个文件，"
                f"自动筛出并选中 {n_bad} 个有乱码的 —— 点「清除全部标签」或「仅删除乱码标签」开始处理。")

        def _add(self, paths) -> int:
            """把文件加进列表，返回真正新增的数量。不触发扫描。"""
            added = 0
            for p in paths:
                p = os.path.abspath(p)
                if p not in self.files:
                    self.files.append(p)
                    added += 1
            return added

        def remove_selected(self):
            # 从后往前删，否则前面的下标一移除，后面的就全错位了
            for idx in sorted((int(i) for i in self.tree.selection()), reverse=True):
                self.files.pop(idx)
            self.rescan(keep_selection=False)

        def clear_all(self):
            self.files.clear()
            self.info.clear()
            self.tree.delete(*self.tree.get_children())
            self.preview.delete(*self.preview.get_children())
            self.status.set("已清空。")

        def rescan(self, keep_selection: bool = True) -> int:
            """重新读一遍所有文件的标签，再重建列表。"""
            self.info = {p: analyze(p) for p in self.files}
            return self._rebuild_tree(keep_selection)

        def _rebuild_tree(self, keep_selection: bool = True) -> int:
            """按 self.info 里已有的分析结果重建列表，不重新读文件。

            用于切换「只看乱码文件」筛选 —— 扫描结果没变，没必要再读一遍盘。
            返回被筛掉（没显示出来）的文件数。
            """
            prev = ({self.files[int(i)] for i in self.tree.selection()}
                    if keep_selection else set())
            self.tree.delete(*self.tree.get_children())
            hidden = 0
            for i, path in enumerate(self.files):
                info = self.info.get(path)
                if info is None or (self.only_garbled.get() and not info["garbled"]):
                    hidden += 1
                    continue
                tag = "none" if info["status"] == "无标签" else ("bad" if info["garbled"] else "")
                self.tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                    info["name"], info["status"], info["title"], info["artist"], info["album"]))
            if prev:
                restore = [iid for iid in self.tree.get_children() if self.files[int(iid)] in prev]
                if restore:
                    self.tree.selection_set(restore)
            self._update_status()
            return hidden

        def _update_status(self):
            total = len(self.files)
            n_bad = sum(1 for p in self.files if self.info.get(p, {}).get("garbled"))
            if self.only_garbled.get():
                shown = len(self.tree.get_children())
                self.status.set(
                    f"只显示乱码文件：{shown} / {total} 个（其余 {total - shown} 个正常文件已隐藏）"
                    f" —— 没勾选行时，按钮作用于列表里全部 {shown} 个。")
            else:
                self.status.set(f"共 {total} 个文件，其中 {n_bad} 个有乱码标签。")

        def on_filter_toggle(self):
            self._rebuild_tree(keep_selection=False)
            if self.only_garbled.get():
                self.select_all_visible()

        def visible_paths(self) -> list[str]:
            return [self.files[int(iid)] for iid in self.tree.get_children()]

        def select_all_visible(self):
            kids = self.tree.get_children()
            if kids:
                self.tree.selection_set(kids)

        def selected_paths(self):
            """选中的文件；一行都没勾选时，作用于当前列表里可见的全部文件。"""
            sel = [int(i) for i in self.tree.selection()]
            if not sel:
                return self.visible_paths()
            return [self.files[i] for i in sel]

        def on_select(self, _event=None):
            sel = self.tree.selection()
            self.preview.delete(*self.preview.get_children())
            if not sel:
                return
            info = self.info.get(self.files[int(sel[0])])
            if not info:
                return
            for fid, label, value, bad in info["frames"]:
                self.preview.insert("", "end", tags=("bad",) if bad else (),
                                    values=(fid, label, value, "乱码" if bad else "正常"))
            for name, text, bad in info["v1_fields"]:
                self.preview.insert("", "end", tags=("bad",) if bad else (),
                                    values=("ID3v1", name, text, "乱码" if bad else "正常"))
            if not info["frames"] and not info["v1_fields"]:
                self.preview.insert("", "end", values=("—", "—", "没有标签", "—"))

        # ---------------- 动作 ----------------
        def do_preview(self):
            paths = self.selected_paths()
            bad = sum(1 for p in paths if self.info.get(p, {}).get("garbled"))
            nolabel = sum(1 for p in paths if self.info.get(p, {}).get("status") == "无标签")
            messagebox.showinfo(
                "预览",
                f"将处理 {len(paths)} 个文件：\n\n"
                f"  含乱码标签：{bad}\n"
                f"  本来就没标签（会跳过）：{nolabel}\n"
                f"  其余正常：{len(paths) - bad - nolabel}\n\n"
                f"勾选项：{'保留封面' if self.opt_cover.get() else '不保留封面'}，"
                f"{'清除 ID3v1' if self.opt_v1.get() else '保留 ID3v1'}。\n\n"
                f"注意：直接改写原文件，不做备份，无法撤销。")

        def _run(self, func):
            paths = self.selected_paths()
            if not paths:
                messagebox.showinfo("提示", "列表是空的。")
                return
            ok = skip = fail = 0
            for p in paths:
                try:
                    msg = func(p)
                except Exception as exc:
                    fail += 1
                    self.info.setdefault(p, {})["status"] = f"失败: {exc}"
                    continue
                if msg.startswith("跳过"):
                    skip += 1
                else:
                    ok += 1
            self.rescan()
            summary = f"完成：处理 {ok} 个，跳过 {skip} 个"
            if fail:
                summary += f"，失败 {fail} 个"
            self.status.set(summary)
            if fail:
                messagebox.showwarning("部分失败", summary + "\n详情见列表「状态」列。")

        def do_strip_all(self):
            self._run(lambda p: strip_all(
                p, self.opt_cover.get(), self.opt_v1.get(), self.opt_rename.get()))

        def do_strip_garbled(self):
            self._run(lambda p: strip_garbled(p))

    App().mainloop()


# ==========================================================================
# 命令行
# ==========================================================================

def main(argv=None):
    fix_console_encoding()
    ap = argparse.ArgumentParser(
        description="MP3 乱码标签清理：删掉乱码 ID3 标签，让播放器显示文件名。")
    ap.add_argument("files", nargs="*", help="MP3 文件（支持通配符由 shell 展开）")
    ap.add_argument("--strip", action="store_true", help="清除全部标签并写入")
    ap.add_argument("--strip-garbled", action="store_true", help="只删除乱码标签并写入")
    ap.add_argument("--no-cover", action="store_true", help="清除全部标签时不保留封面")
    ap.add_argument("--keep-v1", action="store_true", help="清除全部标签时保留 ID3v1")
    ap.add_argument("--title-from-name", action="store_true", help="用文件名重建标题")
    ap.add_argument("--gui", action="store_true", help="强制打开图形界面")
    args = ap.parse_args(argv)

    if args.gui or not args.files:
        launch_gui()
        return 0

    for pattern in args.files:
        import glob
        for path in (glob.glob(pattern) or [pattern]):
            if not os.path.isfile(path):
                print(f"[跳过] 找不到 {path}")
                continue
            if args.strip:
                print(f"[{strip_all(path, not args.no_cover, not args.keep_v1, args.title_from_name)}] {path}")
            elif args.strip_garbled:
                print(f"[{strip_garbled(path)}] {path}")
            else:
                info = analyze(path)
                print(f"\n=== {info['name']} ===  状态：{info['status']}")
                for fid, label, value, bad in info["frames"]:
                    print(f"  {'!!' if bad else '  '} {fid:<6} {label:<8} {value[:90]}")
                for name, text, bad in info["v1_fields"]:
                    print(f"  {'!!' if bad else '  '} ID3v1  {name:<8} {text[:90]}")
                if not info["frames"] and not info["v1_fields"]:
                    print("  (没有标签)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
