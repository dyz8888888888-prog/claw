"""
规模数据自动刷新器
读取 JSON 中的CB列表 → 调用 mx-finance-data → 合并未转股余额 → 写回 JSON
"""

import json
import os
import re
import subprocess
import shutil
import tempfile
from pathlib import Path

# 配置
SKILL_SCRIPT = "C:/Users/DYZ/.workbuddy/skills/mx-finance-data/mx-finance-data/scripts/get_data.py"
PYTHON = "C:/Users/DYZ/.workbuddy/binaries/python/versions/3.13.12/python.exe"
PROJECT_DIR = Path(__file__).parent.parent
DATA_FILE = PROJECT_DIR / "cb_top50_full.json"
BATCH_SIZE = 5  # mx-finance-data 免费限额


def load_cb_list(json_path: Path) -> list:
    """加载CB列表"""
    with open(json_path, "r", encoding="utf-8") as f:
        items = json.load(f)
    return items


def split_batches(cb_names: list, size: int) -> list:
    """将CB名单按 size 分批"""
    return [cb_names[i : i + size] for i in range(0, len(cb_names), size)]


def fetch_batch(names: list, work_dir: Path) -> list:
    """
    调用 mx-finance-data 查询一批CB的未转股余额。
    返回从 md 文件解析出的 (code, scale) 列表。
    """
    query = "、".join(names) + "的未转股余额"
    cmd = [PYTHON, SKILL_SCRIPT, "--query", query, "--indicators", "未转股余额"]
    # 先记录已有文件
    md_dir = work_dir / "miaoxiang" / "mx_finance_data"
    existing = set(os.listdir(md_dir)) if md_dir.exists() else set()

    result = subprocess.run(
        cmd, cwd=str(work_dir), capture_output=True, text=True, timeout=120
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(f"  ⚠ 查询失败: {result.stderr.strip()}")
        return []

    # 找到新生成的 md 文件
    if not md_dir.exists():
        return []
    new_files = set(os.listdir(md_dir)) - existing
    md_files = [f for f in new_files if f.endswith(".md")]

    results = []
    for md_file in md_files:
        with open(md_dir / md_file, encoding="utf-8") as f:
            for line in f:
                if "亿元" not in line and "万元" not in line:
                    continue
                m = re.match(r"\| .+?\((\d+)\.S[ZH]\) \| (.+?) \|", line)
                if not m:
                    continue
                code = m.group(1)
                val_str = m.group(2).strip()
                if "亿元" in val_str:
                    scale = float(val_str.replace("亿元", ""))
                elif "万元" in val_str:
                    scale = float(val_str.replace("万元", "")) / 10000
                else:
                    continue
                results.append((code, round(scale, 4)))
                break  # 每只CB取最新值
    return results


def main():
    print(f"读取数据: {DATA_FILE}")
    items = load_cb_list(DATA_FILE)
    names = [item.get("f14", "") for item in items if item.get("f14")]
    print(f"  CB总数: {len(names)}")

    batches = split_batches(names, BATCH_SIZE)
    print(f"  批次: {len(batches)} ({BATCH_SIZE}只/批)")

    # 创建临时工作目录
    tmpdir = Path(tempfile.mkdtemp(prefix="scale_fetch_"))
    os.makedirs(tmpdir / "miaoxiang" / "mx_finance_data", exist_ok=True)

    scale_map = {}
    try:
        for i, batch in enumerate(batches, 1):
            print(f"\n批次 {i}/{len(batches)}: {', '.join(batch[:3])}...")
            results = fetch_batch(batch, tmpdir)
            for code, scale in results:
                scale_map[code] = scale
            print(f"  获取: {len(results)}/{len(batch)} 只")

        # 合并到 JSON
        merged = 0
        for item in items:
            code = str(item.get("f12", ""))
            if code in scale_map:
                item["_scale"] = scale_map[code]
                merged += 1

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        print(f"\n✓ 完成: {merged}/{len(items)} 只CB已更新规模数据")
        print(f"  保存: {DATA_FILE}")

    finally:
        # 清理临时文件
        shutil.rmtree(tmpdir, ignore_errors=True)
        # 清理 miaoxiang 目录（get_data.py 可能在 PROJECT_DIR 下也创建了文件）
        local_md = PROJECT_DIR / "miaoxiang"
        if local_md.exists():
            shutil.rmtree(local_md, ignore_errors=True)


if __name__ == "__main__":
    main()
