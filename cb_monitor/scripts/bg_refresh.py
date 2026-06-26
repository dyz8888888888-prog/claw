#!/usr/bin/env python3
"""后台持续刷新脚本: 每60秒获取实时数据 → 生成HTML → CloudStudio部署
用法: python bg_refresh.py
"""
import json, os, time, sys, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN_SCRIPT = os.path.join(ROOT, 'scripts', 'gen_live_html.py')
DEPLOY_DIR = os.path.join(ROOT, 'outputs', 'deploy')
PYTHON = r"C:\Users\DYZ\.workbuddy\binaries\python\versions\3.13.12\python.exe"

INTERVAL = 60  # 秒

print(f"[bg_refresh] 启动, 每 {INTERVAL}s 刷新一次 CloudStudio 外链")
print(f"[bg_refresh] 按 Ctrl+C 停止")

count = 0
while True:
    try:
        count += 1
        start = time.time()

        # Step 1: 生成最新 HTML
        result = subprocess.run([PYTHON, GEN_SCRIPT], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            print(f"[{count}] gen_live_html 失败: {result.stderr.strip()[:200]}")
            time.sleep(INTERVAL)
            continue

        # Step 2: 部署到 CloudStudio (通过 workbuddy CLI 内部机制不可直接调用,
        # 所以用一个 curl 触发方式... 实际上部署需要通过 WorkBuddy 工具)
        # 这里把 HTML 生成好, 部署由外部定时触发完成

        elapsed = time.time() - start
        print(f"[{count}] HTML 已生成 ({elapsed:.1f}s), 等待部署触发...")
        
        # 信号文件: 表示有新 HTML 待部署
        signal_file = os.path.join(DEPLOY_DIR, '.needs_deploy')
        with open(signal_file, 'w') as f:
            f.write(str(count))
        
    except Exception as e:
        print(f"[{count}] 错误: {e}")

    time.sleep(INTERVAL)
