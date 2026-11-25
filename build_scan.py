#!/usr/bin/env python3
"""把 scan_data.json 内联进 scan_tpl.html，产出单文件 scan.html（供 GitHub Pages）"""
import json, os, sys
HERE=os.path.dirname(os.path.abspath(__file__))
TPL=os.path.join(HERE,"scan_tpl.html")
OUT=os.path.join(HERE,"scan.html")
DATA=os.path.join(HERE,"scan_data.json")
def main():
    tpl=open(TPL,encoding="utf-8").read()
    obj=json.load(open(DATA,encoding="utf-8")) if os.path.exists(DATA) else {"date":"—","results":[]}
    snap=json.dumps(obj,ensure_ascii=False,separators=(",",":"))
    html=tpl.replace("__SCAN_DATA__",snap)
    open(OUT,"w",encoding="utf-8").write(html)
    core=sum(1 for r in obj.get("results",[]) if r.get("core"))
    print("生成 scan.html %d 字节，日期 %s，核心信号 %d 只"%(len(html.encode()),obj.get("date"),core))
if __name__=="__main__": sys.exit(main())
