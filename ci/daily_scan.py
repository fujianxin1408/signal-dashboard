# -*- coding: utf-8 -*-
"""每日急跌稳股筛选：腾讯实时快照 -> 急跌候选 -> 前复权日线算指标 -> 分级输出"""
import requests, re, time, json, warnings
import pandas as pd, numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
import akshare as ak
warnings.filterwarnings('ignore')

def _ak(name, *a, **kw):
    """兼容 akshare 新旧版本接口改名"""
    fn = getattr(ak, name, None)
    if fn is None:
        alias = {'stock_zh_a_spot':'stock_zh_a_spot_sina',
                 'stock_zh_a_daily':'stock_zh_a_daily'}.get(name)
        fn = getattr(ak, alias, None)
    if fn is None:
        raise AttributeError('akshare 无接口: '+name)
    return fn(*a, **kw)
import os
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE=ROOT
CACHE=os.path.join(ROOT,'.scan_cache'); os.makedirs(CACHE,exist_ok=True)

# 1. 拉全A快照（腾讯批量）
def sym(code):
    if code.startswith(('60','68','9')) : return 'sh'+code
    if code.startswith(('00','30','2')): return 'sz'+code
    if code.startswith(('8','4')): return 'bj'+code
    return 'sh'+code

def fetch_snap(codes):
    out={}
    for k in range(0,len(codes),80):
        batch=codes[k:k+80]
        url='http://qt.gtimg.cn/q='+','.join(sym(c) for c in batch)
        try:
            r=requests.get(url,timeout=15); r.encoding='gbk'
            for line in r.text.strip().split(';'):
                if '="' not in line: continue
                f=line.split('"')[1].split('~')
                if len(f)<35: continue
                code=f[2]
                try: price=float(f[3]); chg=float(f[32]); name=f[1]
                except: continue
                if price<=0: continue
                try: pe=float(f[39])   # 滚动市盈率(TTM)
                except: pe=None
                out[code]=dict(name=name,price=price,chg=chg,pe=pe)
        except Exception as e:
            pass
    return out

# 全A代码列表（用之前的新浪spot缓存代码，或akshare）
def all_codes():
    # 优先用仓库内置的代码种子文件（稳定，不依赖任何外部接口）
    seed=os.path.join(ROOT,'codes_seed.json')
    if os.path.exists(seed):
        try:
            codes=json.load(open(seed))
            if len(codes)>4000: return codes
        except: pass
    f=os.path.join(ROOT,'.scan_codes.json')
    if os.path.exists(f):
        try: return json.load(open(f))
        except: pass
    # 兜底：尝试网络拉取（失败也不致命，可稍后更新种子文件）
    df=_ak("stock_zh_a_spot")
    codes=[re.sub(r'[^0-9]','',str(c))[-6:] for c in df['代码'].tolist()]
    json.dump(codes,open(f,'w')); return codes

codes=all_codes()
print('全A代码数',len(codes))
import os as _os
if _os.path.exists('/tmp/snap_cache.json'):
    snap=json.load(open('/tmp/snap_cache.json')); print('快照缓存',len(snap))
else:
    t=time.time(); snap=fetch_snap(codes)
json.dump(snap,open('/tmp/snap_cache.json','w'),ensure_ascii=False)
print('快照获取',len(snap),'耗时%.1fs'%(time.time()-t))
# 急跌候选：当日跌<=-2.5%（放宽一点，3日累计在日线里再算）
cand=[c for c,v in snap.items() if v['chg']<=-2.5]
print('当日跌超2.5%候选',len(cand))

# 2. 拉候选前复权日线（腾讯），带缓存
def get_hist(code):
    f=CACHE+'/'+code+'.pkl'
    if os.path.exists(f):
        try: return pd.read_pickle(f)
        except: pass
    import time as _t
    for attempt in range(3):   # 新浪接口偶发 ConnectionReset，重试
        try:
            df=_ak("stock_zh_a_daily",symbol=sym(code),adjust="qfq")
            if df is not None and len(df)>=300:
                df.to_pickle(f); return df
        except: pass
        _t.sleep(0.6*(attempt+1))
    return None

t=time.time()
hists={}
with ThreadPoolExecutor(max_workers=16) as ex:
    futs={ex.submit(get_hist,c):c for c in cand}
    for fu in as_completed(futs):
        c=futs[fu]; d=fu.result()
        if d is not None: hists[c]=d
print('日线获取',len(hists),'耗时%.1fs'%(time.time()-t))

# 3. 算指标 + 筛选
def inds(df):
    s=df['close']; c=s.values; h=df['high'].values; l=df['low'].values
    v=df['volume'].values.astype(float)
    ma6=s.rolling(6).mean().values; ma20=s.rolling(20).mean().values; ma60=s.rolling(60).mean().values
    bias6=(c-ma6)/ma6*100; bias20=(c-ma20)/ma20*100; dev60=(c-ma60)/ma60*100
    d=s.diff(); g=d.clip(lower=0); ls=-d.clip(upper=0)
    rsi14=(100-100/(1+g.ewm(alpha=1/14,adjust=False).mean()/ls.ewm(alpha=1/14,adjust=False).mean())).values
    rsi6=(100-100/(1+g.ewm(alpha=1/6,adjust=False).mean()/ls.ewm(alpha=1/6,adjust=False).mean())).values
    ll=pd.Series(l).rolling(9).min().values; hh=pd.Series(h).rolling(9).max().values
    k=pd.Series((c-ll)/(hh-ll+1e-12)*100).ewm(alpha=1/3,adjust=False).mean().values
    dd=pd.Series(k).ewm(alpha=1/3,adjust=False).mean().values; j=3*k-2*dd
    ret=s.pct_change().values*100; cum3=(s/s.shift(3)-1).values*100
    vma5=pd.Series(v).shift(1).rolling(5).mean().values
    vr=v/(vma5+1e-9)
    turncol='turnover' if 'turnover' in df.columns else None
    turn=df[turncol].values*100 if turncol else np.full(len(c),np.nan)
    span=h-l; shadow=np.where(span>1e-12,(np.minimum(df['open'].values,c)-l)/(span+1e-12),0)
    return dict(c=c,bias6=bias6,bias20=bias20,dev60=dev60,rsi14=rsi14,rsi6=rsi6,j=j,ret=ret,cum3=cum3,vr=vr,turn=turn,shadow=shadow,n=len(c))

results=[]
for code,df in hists.items():
    d=inds(df); i=d['n']-1
    # 用今天收盘快照价校正最后一根（前复权最后一根=实际收盘，一致）
    sharp = (d['ret'][i]<=-3) or (d['cum3'][i]<=-8)
    if not sharp: continue
    b6=d['bias6'][i]; r14=d['rsi14'][i]; r6=d['rsi6'][i]; j=d['j'][i]
    core = (b6<=-8) and (r14<=25)
    near = (b6<=-6 and r14<=30) or (b6<=-8) or (r14<=25)  # 接近
    if not (core or near): continue
    results.append(dict(code=code,name=snap[code]['name'],price=snap[code]['price'],chg=snap[code]['chg'],
        pe=snap[code].get('pe'),
        bias6=round(b6,2),rsi14=round(r14,1),rsi6=round(r6,1),j=round(j,1),
        bias20=round(d['bias20'][i],2),dev60=round(d['dev60'][i],2),
        ret=round(d['ret'][i],2),cum3=round(d['cum3'][i],2),
        vr=round(d['vr'][i],2),turn=round(d['turn'][i],2) if not np.isnan(d['turn'][i]) else None,
        core=bool(core),
        add_J=bool(j<0), add_vol=bool(d['vr'][i]>1.5), add_lowopen=bool(snap[code]['chg']<=-5)))

# 3.5 补充股息率：近12个月每股派息 / 现价（腾讯快照无股息率字段，理杏仁接口已失效）
import datetime as _dt
_todayDate=_dt.date.today()
_cutoff=(_todayDate-_dt.timedelta(days=365)).isoformat()
def _divyield(r):
    try:
        df=_ak("stock_history_dividend_detail",symbol=r['code'],indicator="分红")
        if df is None or len(df)==0: return None
        ex=df['除权除息日'].astype(str)
        recent=df[(ex>=_cutoff)&(df['进度']=='实施')]
        cash=float(recent['派息'].sum())     # 每10股派息(元)
        return round(cash/10.0/r['price']*100,2) if r['price']>0 else None
    except: return None
with ThreadPoolExecutor(max_workers=12) as ex:
    fu_map={ex.submit(_divyield,r):r for r in results}
    for fu in as_completed(fu_map):
        fu_map[fu]['dv']=fu.result()
print('股息率补充完成',sum(1 for r in results if r.get('dv') is not None))

results.sort(key=lambda x:(not x['core'], x['bias6']))
import datetime as _dt
_today=_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).strftime('%Y-%m-%d')
json.dump(dict(date=_today,total_scanned=len(snap),candidates=len(cand),results=results),
          open(os.path.join(ROOT,'scan_data.json'),'w'),ensure_ascii=False,indent=1)
print('\n符合结果数',len(results),'其中核心双超卖',sum(r['core'] for r in results))
print('核心双超卖',sum(r['core'] for r in results),'精筛',sum(1 for r in results if r['core'] and r['add_J'] and r['add_vol']))
