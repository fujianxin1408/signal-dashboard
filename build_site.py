#!/usr/bin/env python3
"""把 data.json 内联进 index.html，产出单文件静态页（供 GitHub Pages）"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "tpl.html")
OUT = os.path.join(HERE, "index.html")
JS  = os.path.join(HERE, "page.js")
DATA = os.path.join(HERE, "data.json")

def main():
    tpl = open(TPL, encoding="utf-8").read()
    js = open(JS, encoding="utf-8").read()
    obj = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    snap = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    body = js.replace("__SNAPSHOT__", snap)
    html = tpl.replace("<!--PAGE_JS-->", "<script>\n" + body + "\n</script>")
    open(OUT, "w", encoding="utf-8").write(html)
    print("生成 index.html %d 字节，标的 %d 只" %
          (len(html.encode()),
           len(obj.get("slots", {}).get("1510", {}).get("rows", []))))
    return 0

if __name__ == "__main__":
    sys.exit(main())
