# core.py — merged: config + kucoin client + indicators + strategy + backtest + walk-forward
import os
import time
import itertools
from dataclasses import dataclass, asdict

import httpx
import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ============================== CONFIG ==============================

@dataclass
class Config:
    token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    base_url: str = os.getenv("KUCOIN_BASE_URL", "https://api-futures.kucoin.com")
    symbol: str = os.getenv("DEFAULT_SYMBOL", "XBTUSDTM")
    capital: float = float(os.getenv("DEFAULT_CAPITAL", "10000"))
    risk: float = float(os.getenv("DEFAULT_RISK", "0.005"))
    fee: float = float(os.getenv("DEFAULT_FEE", "0.0006"))
    slippage: float = float(os.getenv("DEFAULT_SLIPPAGE", "0.0002"))

CFG = Config()

# ============================== KUCOIN CLIENT ==============================

INTERVALS = {"15m": 15, "1h": 60, "4h": 240}

class KuCoinFutures:
    def __init__(self, base_url="https://api-futures.kucoin.com", uta_url="https://api.kucoin.com"):
        self.base_url = base_url.rstrip("/")
        self.uta_url = uta_url.rstrip("/")

    def _get(self, url, params):
        with httpx.Client(timeout=30) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            payload = r.json()
        if payload.get("code") != "200000":
            raise RuntimeError(payload)
        return payload.get("data")

    def klines(self, symbol, start, end, interval="15m"):
        if interval not in INTERVALS:
            raise ValueError("Supported intervals: 15m, 1h, 4h")
        granularity = INTERVALS[interval]
        start_ts = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
        end_ts = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
        step = granularity * 60 * 1000
        rows, cursor = [], start_ts
        while cursor < end_ts:
            data = self._get(
                f"{self.base_url}/api/v1/kline/query",
                {"symbol": symbol, "granularity": granularity, "from": cursor, "to": end_ts}
            ) or []
            if not data: break
            rows.extend(data)
            last = max(int(x[0]) for x in data)
            nxt = last + step
            if nxt <= cursor: break
            cursor = nxt
            time.sleep(0.12)
            if len(data) < 500: break

        if not rows:
            raise RuntimeError("KuCoin returned no Futures candles.")
        df = pd.DataFrame(rows).iloc[:, :7]
        df.columns = ["timestamp","open","close","high","low","volume","turnover"]
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for c in ["open","high","low","close","volume","turnover"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)[
            ["timestamp","open","high","low","close","volume"]
        ]

    def funding_history(self, symbol, start, end):
        # UTA public endpoint: historical settlement funding.
        start_ts = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
        end_ts = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
        data = self._get(
            f"{self.uta_url}/api/ua/v1/market/funding-rate-history",
            {"symbol": symbol, "startAt": start_ts, "endAt": end_ts}
        ) or {}
        rows = data.get("list", []) if isinstance(data, dict) else data
        if not rows:
            return pd.DataFrame(columns=["timestamp","funding_rate"])
        out = pd.DataFrame(rows)
        out["timestamp"] = pd.to_datetime(out["ts"], unit="ms", utc=True)
        out["funding_rate"] = pd.to_numeric(out["fundingRate"], errors="coerce")
        return out[["timestamp","funding_rate"]].dropna().drop_duplicates("timestamp").sort_values("timestamp")

    def open_interest_history(self, symbol, start, end, interval="15min", page_size=200):
        # UTA OI endpoint. KuCoin documents intraday OI history retention as limited;
        # this method raises a clear error when the requested range is unavailable.
        start_ts = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
        end_ts = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
        rows, cursor = [], start_ts
        while cursor < end_ts:
            data = self._get(
                f"{self.uta_url}/api/ua/v1/market/open-interest",
                {"symbol": symbol, "interval": interval, "startAt": cursor, "endAt": end_ts,
                 "pageSize": page_size}
            ) or []
            if not data: break
            rows.extend(data)
            ts = [int(x["ts"]) for x in data if "ts" in x]
            if not ts: break
            nxt = max(ts) + 1
            if nxt <= cursor: break
            cursor = nxt
            time.sleep(0.12)
            if len(data) < page_size: break

        if not rows:
            raise RuntimeError(
                "KuCoin returned no historical OI. KuCoin currently limits intraday historical OI retention; "
                "try a range within the documented retention window or run without OI."
            )
        out = pd.DataFrame(rows)
        out["timestamp"] = pd.to_datetime(out["ts"], unit="ms", utc=True)
        out["open_interest"] = pd.to_numeric(out["openInterest"], errors="coerce")
        return out[["timestamp","open_interest"]].dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)

    def market_features(self, symbol, start, end, require_oi=False):
        funding = self.funding_history(symbol, start, end)
        try:
            oi = self.open_interest_history(symbol, start, end, "15min")
            oi_available = True
        except RuntimeError:
            if require_oi:
                raise
            oi = pd.DataFrame(columns=["timestamp","open_interest"])
            oi_available = False
        return funding, oi, oi_available

# ============================== INDICATORS ==============================

def ema(s,n): return s.ewm(span=n, adjust=False).mean()

def atr(df,n=14):
    p=df.close.shift(1)
    tr=pd.concat([df.high-df.low,(df.high-p).abs(),(df.low-p).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False).mean()

def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    au=up.ewm(alpha=1/n,adjust=False).mean()
    ad=dn.ewm(alpha=1/n,adjust=False).mean()
    rs=au/ad.replace(0,np.nan)
    return 100-100/(1+rs)

def adx(df,n=14):
    up=df.high.diff(); dn=-df.low.diff()
    plus=up.where((up>dn)&(up>0),0.0)
    minus=dn.where((dn>up)&(dn>0),0.0)
    p=df.close.shift(1)
    tr=pd.concat([df.high-df.low,(df.high-p).abs(),(df.low-p).abs()],axis=1).max(axis=1)
    av=tr.ewm(alpha=1/n,adjust=False).mean()
    pi=100*plus.ewm(alpha=1/n,adjust=False).mean()/av.replace(0,np.nan)
    mi=100*minus.ewm(alpha=1/n,adjust=False).mean()/av.replace(0,np.nan)
    dx=100*(pi-mi).abs()/(pi+mi).replace(0,np.nan)
    return dx.ewm(alpha=1/n,adjust=False).mean()

def indicators(df):
    x=df.copy()
    for n in (20,50,200): x[f"ema{n}"]=ema(x.close,n)
    x["atr"]=atr(x); x["rsi"]=rsi(x.close); x["adx"]=adx(x)
    x["vol_ma"]=x.volume.rolling(20).mean()
    return x

# ============================== STRATEGY ==============================

def resample(df, rule):
    x=df.set_index("timestamp")
    return x.resample(rule,label="right",closed="right").agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna().reset_index()

def attach_features(m, funding=None, oi=None):
    z=m.copy().sort_values("timestamp")
    if funding is not None and not funding.empty:
        f=funding.sort_values("timestamp").copy()
        # Do not use the funding settlement that occurs at the same candle:
        # the backtest only sees funding that was already settled/known.
        z=pd.merge_asof(z, f, on="timestamp", direction="backward", allow_exact_matches=False)
    else:
        z["funding_rate"]=0.0
    if oi is not None and not oi.empty:
        o=oi.sort_values("timestamp").copy()
        z=pd.merge_asof(z, o, on="timestamp", direction="backward", allow_exact_matches=False)
        z["oi_change_24h"]=z["open_interest"].pct_change(96)
        z["oi_change_4h"]=z["open_interest"].pct_change(16)
    else:
        z["open_interest"]=float("nan")
        z["oi_change_24h"]=0.0
        z["oi_change_4h"]=0.0
    return z

def signals(df15, funding=None, oi=None, params=None):
    p={"adx_min":22.0,"funding_long_max":0.0010,"funding_short_min":-0.0010,
       "oi_min_change":-1.0}
    if params: p.update(params)
    m=indicators(df15)
    h1=indicators(resample(df15,"1h"))
    h4=indicators(resample(df15,"4h"))

    a=attach_features(m,funding,oi).set_index("timestamp")
    b=h1.set_index("timestamp")[["close","ema50","ema200","adx"]].add_prefix("h1_")
    c=h4.set_index("timestamp")[["close","ema50","ema200"]].add_prefix("h4_")
    z=a.join(b,how="left").join(c,how="left").ffill().reset_index()

    long_reg=(z.h4_close>z.h4_ema200)&(z.h4_ema50>z.h4_ema200)
    short_reg=(z.h4_close<z.h4_ema200)&(z.h4_ema50<z.h4_ema200)
    long_tr=(z.h1_close>z.h1_ema200)&(z.h1_ema50>z.h1_ema200)&(z.h1_adx>=p["adx_min"])
    short_tr=(z.h1_close<z.h1_ema200)&(z.h1_ema50<z.h1_ema200)&(z.h1_adx>=p["adx_min"])
    vol=z.volume>z.vol_ma

    funding_long_ok=z.funding_rate.fillna(0)<=p["funding_long_max"]
    funding_short_ok=z.funding_rate.fillna(0)>=p["funding_short_min"]
    oi_ok=(z.oi_change_24h.fillna(0)>=p["oi_min_change"])

    z["long_entry"]=long_reg&long_tr&(z.close>z.ema20)&(z.close.shift(1)<=z.ema20.shift(1))&vol&(z.rsi>50)&funding_long_ok&oi_ok
    z["short_entry"]=short_reg&short_tr&(z.close<z.ema20)&(z.close.shift(1)>=z.ema20.shift(1))&vol&(z.rsi<50)&funding_short_ok&oi_ok
    return z.dropna(subset=["atr","h1_ema200","h4_ema200"]).reset_index(drop=True)

# ============================== BACKTEST ==============================

@dataclass
class Trade:
    side:str; entry_time:str; exit_time:str; entry:float; exit:float; qty:float; pnl:float; r:float; reason:str

def run(df, capital=10000, risk=0.005, fee=0.0006, slippage=0.0002,
        atr_mult=1.5, rr=2.0, max_bars=96, funding=None, oi=None, params=None):
    z=signals(df,funding,oi,params); equity=capital; peak=capital; maxdd=0; pos=None; trades=[]; curve=[]
    for i,row in z.iterrows():
        price=float(row.close)
        if pos:
            out=None; reason=None
            if pos["side"]=="LONG":
                if row.low<=pos["sl"]: out,reason=pos["sl"],"SL"
                elif row.high>=pos["tp"]: out,reason=pos["tp"],"TP"
            else:
                if row.high>=pos["sl"]: out,reason=pos["sl"],"SL"
                elif row.low<=pos["tp"]: out,reason=pos["tp"],"TP"
            if out is None and i-pos["i"]>=max_bars: out,reason=price,"TIME"
            if out is not None:
                gross=((out-pos["entry"]) if pos["side"]=="LONG" else (pos["entry"]-out))*pos["qty"]
                costs=(pos["entry"]+out)*pos["qty"]*fee
                pnl=gross-costs; equity+=pnl
                trades.append(Trade(pos["side"],str(pos["time"]),str(row.timestamp),pos["entry"],out,pos["qty"],pnl,pnl/pos["risk_cash"],reason))
                pos=None
        if pos is None:
            side="LONG" if bool(row.long_entry) else ("SHORT" if bool(row.short_entry) else None)
            if side:
                entry=price*(1+slippage if side=="LONG" else 1-slippage)
                dist=max(float(row.atr)*atr_mult,price*0.002)
                risk_cash=equity*risk; qty=risk_cash/dist
                sl,tp=((entry-dist,entry+dist*rr) if side=="LONG" else (entry+dist,entry-dist*rr))
                equity-=entry*qty*fee
                pos={"side":side,"entry":entry,"sl":sl,"tp":tp,"qty":qty,"risk_cash":risk_cash,"time":row.timestamp,"i":i}
        peak=max(peak,equity); maxdd=max(maxdd,(peak-equity)/peak if peak else 0)
        curve.append({"timestamp":row.timestamp,"equity":equity})
    if pos:
        row=z.iloc[-1]; out=float(row.close)
        gross=((out-pos["entry"]) if pos["side"]=="LONG" else (pos["entry"]-out))*pos["qty"]
        pnl=gross-(pos["entry"]+out)*pos["qty"]*fee; equity+=pnl
        trades.append(Trade(pos["side"],str(pos["time"]),str(row.timestamp),pos["entry"],out,pos["qty"],pnl,pnl/pos["risk_cash"],"END"))
    t=pd.DataFrame([asdict(x) for x in trades]); c=pd.DataFrame(curve)
    wins=int((t.pnl>0).sum()) if not t.empty else 0
    gp=t.loc[t.pnl>0,"pnl"].sum() if not t.empty else 0
    gl=-t.loc[t.pnl<0,"pnl"].sum() if not t.empty else 0
    pf=gp/gl if gl else (float("inf") if gp else 0)
    rets=c.equity.pct_change().dropna()
    sharpe=rets.mean()/rets.std()*np.sqrt(365*24*4) if len(rets)>1 and rets.std() else 0
    return {"start_capital":capital,"final_equity":equity,"pnl":equity-capital,"roi":equity/capital-1,
            "trades":len(t),"win_rate":wins/len(t) if len(t) else 0,"profit_factor":pf,
            "max_drawdown":maxdd,"sharpe":sharpe},t,c

# ============================== WALK-FORWARD ==============================

@dataclass
class WFResult:
    fold:int
    train_start:str
    train_end:str
    test_start:str
    test_end:str
    atr_mult:float
    rr:float
    adx_min:float
    funding_long_max:float
    funding_short_min:float
    oi_min_change:float
    train_score:float
    test_roi:float
    test_pf:float
    test_dd:float
    test_trades:int

def objective(s):
    if s["trades"] < 5: return -999
    pf=min(s["profit_factor"],4.0) if np.isfinite(s["profit_factor"]) else 4.0
    return s["roi"] + 0.08*(pf-1) - 0.7*s["max_drawdown"]

def optimize(train, funding, oi, capital, risk, fee, slippage, grid):
    best=None
    for vals in itertools.product(*grid.values()):
        params=dict(zip(grid.keys(),vals))
        s,_,_=run(train,capital,risk,fee,slippage,
                  atr_mult=params["atr_mult"],rr=params["rr"],
                  funding=funding,oi=oi,params=params)
        score=objective(s)
        if best is None or score>best["score"]:
            best={"params":params,"score":score,"summary":s}
    return best

def walk_forward(df, funding=None, oi=None, capital=10000, risk=.005, fee=.0006, slippage=.0002,
                 train_days=180, test_days=30, step_days=30, grid=None):
    if grid is None:
        grid={"atr_mult":[1.25,1.5,1.75,2.0],"rr":[1.5,2.0,2.5,3.0],
              "adx_min":[20,22,25],"funding_long_max":[0.0005,0.001,0.002],
              "funding_short_min":[-0.002,-0.001,-0.0005],"oi_min_change":[-1.0,0.0,0.01]}
    x=df.sort_values("timestamp").reset_index(drop=True)
    start=x.timestamp.min(); end=x.timestamp.max()
    fold=0; rows=[]; tests=[]; cursor=start
    while cursor + pd.Timedelta(days=train_days+test_days) <= end:
        tr_end=cursor+pd.Timedelta(days=train_days)
        te_end=tr_end+pd.Timedelta(days=test_days)
        train=x[(x.timestamp>=cursor)&(x.timestamp<tr_end)]
        test=x[(x.timestamp>=tr_end)&(x.timestamp<te_end)]
        ftr=funding[(funding.timestamp>=cursor)&(funding.timestamp<tr_end)] if funding is not None and not funding.empty else funding
        fte=funding[(funding.timestamp>=tr_end)&(funding.timestamp<te_end)] if funding is not None and not funding.empty else funding
        otr=oi[(oi.timestamp>=cursor)&(oi.timestamp<tr_end)] if oi is not None and not oi.empty else oi
        ote=oi[(oi.timestamp>=tr_end)&(oi.timestamp<te_end)] if oi is not None and not oi.empty else oi
        if len(train)>1000 and len(test)>100:
            best=optimize(train,ftr,otr,capital,risk,fee,slippage,grid)
            ts,tt,_=run(test,capital,risk,fee,slippage,best["params"]["atr_mult"],best["params"]["rr"],96,fte,ote,best["params"])
            p=best["params"]
            rows.append(asdict(WFResult(fold,str(cursor),str(tr_end),str(tr_end),str(te_end),
                p["atr_mult"],p["rr"],p["adx_min"],p["funding_long_max"],p["funding_short_min"],
                p["oi_min_change"],best["score"],ts["roi"],ts["profit_factor"],ts["max_drawdown"],ts["trades"])))
            if not tt.empty: tests.append(tt)
            fold+=1
        cursor += pd.Timedelta(days=step_days)
    result=pd.DataFrame(rows)
    trades=pd.concat(tests,ignore_index=True) if tests else pd.DataFrame()
    summary={
        "folds":len(result),
        "oos_roi_compounded": float((1+result.test_roi).prod()-1) if len(result) else 0,
        "median_test_roi": float(result.test_roi.median()) if len(result) else 0,
        "median_test_pf": float(result.test_pf.replace([np.inf,-np.inf],np.nan).median()) if len(result) else 0,
        "worst_test_dd": float(result.test_dd.max()) if len(result) else 0,
        "total_test_trades": int(result.test_trades.sum()) if len(result) else 0,
    }
    return summary,result,trades

# ============================== TELEGRAM BOT ==============================

import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
paused=False

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "KuCoin Research Bot\n\n"
        "/backtest XBTUSDTM 2025-01-01 2026-08-01 10000 0.005\n"
        "/walkforward XBTUSDTM 2025-01-01 2026-08-01\n"
        "/status\n/pause\n/resume"
    )

async def status(update,context): await update.message.reply_text("Status: PAUSED" if paused else "Status: READY")
async def pause(update,context):
    global paused; paused=True; await update.message.reply_text("Paused.")
async def resume(update,context):
    global paused; paused=False; await update.message.reply_text("Resumed.")

async def backtest(update,context):
    try:
        a=context.args; symbol=a[0] if len(a)>0 else CFG.symbol
        start=a[1] if len(a)>1 else "2025-01-01"; end=a[2] if len(a)>2 else "2026-08-01"
        capital=float(a[3]) if len(a)>3 else CFG.capital; risk=float(a[4]) if len(a)>4 else CFG.risk
        api=KuCoinFutures(CFG.base_url)
        await update.message.reply_text("⏳ Downloading KuCoin candles + funding + OI...")
        df=api.klines(symbol,start,end,"15m"); funding=api.funding_history(symbol,start,end)
        try: oi=api.open_interest_history(symbol,start,end,"15min")
        except RuntimeError: oi=None
        s,t,c=run(df,capital,risk,CFG.fee,CFG.slippage,funding=funding,oi=oi)
        Path("reports").mkdir(exist_ok=True); t.to_csv("reports/trades.csv",index=False); c.to_csv("reports/equity_curve.csv",index=False)
        await update.message.reply_text(
            f"📊 {symbol} Backtest\n{start} → {end}\n\n"
            f"Initial: ${s['start_capital']:,.2f}\nFinal: ${s['final_equity']:,.2f}\nROI: {s['roi']:.2%}\n"
            f"Trades: {s['trades']}\nWin rate: {s['win_rate']:.2%}\nProfit factor: {s['profit_factor']:.2f}\n"
            f"Max DD: {s['max_drawdown']:.2%}\nSharpe: {s['sharpe']:.2f}\n"
            f"Funding: REAL\nOI: {'REAL' if oi is not None else 'UNAVAILABLE'}"
        )
    except Exception as e:
        logging.exception("backtest failed"); await update.message.reply_text(f"❌ Error: {e}")

async def walkforward_cmd(update,context):
    try:
        a=context.args; symbol=a[0] if len(a)>0 else CFG.symbol
        start=a[1] if len(a)>1 else "2025-01-01"; end=a[2] if len(a)>2 else "2026-08-01"
        api=KuCoinFutures(CFG.base_url)
        await update.message.reply_text("⏳ Running walk-forward: downloading data and optimizing each training fold...")
        df=api.klines(symbol,start,end,"15m"); funding=api.funding_history(symbol,start,end)
        try: oi=api.open_interest_history(symbol,start,end,"15min")
        except RuntimeError: oi=None
        s,folds,trades=walk_forward(df,funding,oi,CFG.capital,CFG.risk,CFG.fee,CFG.slippage)
        Path("reports").mkdir(exist_ok=True); folds.to_csv("reports/walk_forward_folds.csv",index=False); trades.to_csv("reports/walk_forward_trades.csv",index=False)
        await update.message.reply_text(
            f"🧪 Walk-Forward {symbol}\nFolds: {s['folds']}\n"
            f"OOS compounded ROI: {s['oos_roi_compounded']:.2%}\n"
            f"Median OOS ROI: {s['median_test_roi']:.2%}\n"
            f"Median OOS PF: {s['median_test_pf']:.2f}\n"
            f"Worst OOS DD: {s['worst_test_dd']:.2%}\n"
            f"OOS trades: {s['total_test_trades']}\n"
            f"Funding: REAL\nOI: {'REAL' if oi is not None else 'UNAVAILABLE'}"
        )
    except Exception as e:
        logging.exception("walkforward failed"); await update.message.reply_text(f"❌ Error: {e}")

def main():
    if not CFG.token: raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    app=Application.builder().token(CFG.token).build()
    for cmd,fn in [("start",start),("help",start),("status",status),("pause",pause),("resume",resume),("backtest",backtest),("walkforward",walkforward_cmd)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.run_polling()

if __name__=="__main__": main()
