#!/usr/bin/env python3
"""把 signal-app 的 data.json 同步到 signal-site 并推送 GitHub Pages。

用法：
    python3 deploy.py              # 构建 + 提交 + 推送
    python3 deploy.py --dry        # 只构建，不推送

需要的环境变量（或 ~/.gh_token 文件）：
    GH_TOKEN     GitHub Personal Access Token（repo 权限）
    GH_REPO      形如 username/signal-dashboard
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = "/workspace/signal-app"
APP_DATA = os.path.join(APP_DIR, "data.json")
SITE_DATA = os.path.join(HERE, "data.json")
TOKEN_FILE = os.path.expanduser("~/.gh_token")


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not t and os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE).read().strip()
    return t


def repo():
    r = os.environ.get("GH_REPO")
    if r:
        return r
    cfg = os.path.join(HERE, ".gh_repo")
    if os.path.exists(cfg):
        return open(cfg).read().strip()
    return None


def run(cmd, cwd=HERE, check=True):
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        print("命令失败: %s" % cmd)
        print((p.stderr or p.stdout or "")[:800])
        return None
    return (p.stdout or "").strip()


def main():
    dry = "--dry" in sys.argv

    # 1. 同步数据
    if not os.path.exists(APP_DATA):
        print("找不到 %s" % APP_DATA)
        return 1
    shutil.copy2(APP_DATA, SITE_DATA)

    # 2. 构建静态页
    rc = subprocess.run([sys.executable, os.path.join(HERE, "build_site.py")],
                        capture_output=True, text=True)
    print((rc.stdout or "").strip())
    if rc.returncode != 0:
        print((rc.stderr or "")[:500])
        return 1

    if dry:
        print("--dry 模式，跳过推送")
        return 0

    # 3. 推送
    tk, rp = token(), repo()
    if not tk or not rp:
        print("缺少 GH_TOKEN 或 GH_REPO，跳过推送")
        return 2

    # git init 幂等
    if not os.path.exists(os.path.join(HERE, ".git")):
        run("git init -b main")
        run('git config user.name "signal-bot"')
        run('git config user.email "signal-bot@users.noreply.github.com"')

    run("git add -A")
    changed = run("git status --porcelain")
    if not changed:
        print("无变更，跳过提交")
        return 0

    stamp = run("date '+%Y-%m-%d %H:%M'")
    run('git commit -m "data %s"' % stamp)

    url = "https://%s@github.com/%s.git" % (tk, rp)
    run("git remote remove origin", check=False)
    run("git remote add origin %s" % url)

    out = run("git push -u origin main --force")
    if out is None:
        return 1

    print("已推送 %s，页面地址： https://%s.github.io/%s/"
          % (rp, rp.split("/")[0], rp.split("/")[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
