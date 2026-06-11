"""
Crypto Arbitrage Dashboard
Запуск: streamlit run dashboard.py
"""

import asyncio
import time
import httpx
import streamlit as st
import plotly.graph_objects as go
from datetime import datetime
from collections import deque

import config
from arb_monitor import (COINS, MIN_NET_PCT, MIN_DEPTH_USD,
                         EXCHANGE_FETCHERS, best_spread_for_coin)

# ─── Сторінка ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Crypto Arbitrage Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .block-container { padding-top: 1rem; }
    .metric-card {
        background: #1e1e2e;
        border-radius: 10px;
        padding: 16px;
        border: 1px solid #313244;
    }
    .opp-card {
        background: #1e3a2f;
        border-radius: 10px;
        padding: 16px;
        border: 1px solid #40a070;
        margin-bottom: 8px;
    }
    .no-opp {
        background: #2a2a3e;
        border-radius: 10px;
        padding: 16px;
        border: 1px solid #44445a;
        color: #888;
        text-align: center;
    }
</style>
""", unsafe_allow_html=True)

# ─── Ініціалізація history в session_state ──────────────────────────────────
if "p2p_history" not in st.session_state:
    st.session_state.p2p_history = deque(maxlen=120)   # 120 точок = 10 хв при 5 сек
if "arb_history" not in st.session_state:
    st.session_state.arb_history = {}
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = 0

# ─── Дані ───────────────────────────────────────────────────────────────────
@st.cache_data(ttl=5)
def fetch_p2p():
    """Binance P2P + Bybit P2P — USDT/UAH (купівля + продаж)"""
    import urllib.request, json, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    MIN_UAH = 8200
    UA_PAY  = ["Monobank", "PrivatBank", "BankTransfer",
               "A-Bank", "Pumb", "OtpBank", "Sportbank", "IziBank"]

    def _filter_prices_binance(items, sort_asc=True):
        """Фільтр: від $200, не merchant, топ-3 середнє."""
        prices = [
            float(d["adv"]["price"]) for d in items
            if float(d["adv"].get("minSingleTransAmount", 0)) >= MIN_UAH
            and d["advertiser"].get("userType", "user") != "merchant"
        ] or [
            float(d["adv"]["price"]) for d in items
            if d["advertiser"].get("userType", "user") != "merchant"
        ] or [float(d["adv"]["price"]) for d in items]
        top3 = sorted(prices)[:3] if sort_asc else sorted(prices, reverse=True)[:3]
        return round(sum(top3) / len(top3), 2) if top3 else 0

    def _filter_prices_bybit(items, sort_asc=True):
        """Фільтр: онлайн, від $200, топ-3 середнє."""
        prices = [
            float(i["price"]) for i in items
            if i.get("isOnline", False)
            and float(i.get("minAmount", 0)) >= MIN_UAH
        ] or [float(i["price"]) for i in items if i.get("isOnline", False)]
        top3 = sorted(prices)[:3] if sort_asc else sorted(prices, reverse=True)[:3]
        return round(sum(top3) / len(top3), 2) if top3 else 0

    def _binance_request(trade_type):
        body = json.dumps({
            "asset": "USDT", "fiat": "UAH", "tradeType": trade_type,
            "page": 1, "rows": 10, "payTypes": UA_PAY,
            "merchantCheck": False, "publisherType": None
        }).encode()
        req = urllib.request.Request(
            config.BINANCE_P2P_URL, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, context=ctx, timeout=6) as r:
            return json.loads(r.read())

    def _bybit_request(side):
        body = json.dumps({
            "tokenId": "USDT", "currencyId": "UAH",
            "side": side, "size": "10", "page": "1", "payment": []
        }).encode()
        req = urllib.request.Request(
            config.BYBIT_P2P_URL, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, context=ctx, timeout=6) as r:
            return json.loads(r.read())

    results = {}

    # ── Binance ──────────────────────────────────────────────────────────────
    try:
        data = _binance_request("BUY")   # продавці → ти купуєш
        items = data.get("data", [])
        results["binance_ask"] = _filter_prices_binance(items, sort_asc=True)
        results["binance_count"] = data.get("total", 0)
    except Exception as e:
        results["binance_ask"] = 0
        results["binance_count"] = 0

    try:
        data = _binance_request("SELL")  # покупці → ти продаєш
        items = data.get("data", [])
        results["binance_bid"] = _filter_prices_binance(items, sort_asc=False)
    except Exception:
        results["binance_bid"] = 0

    # ── Bybit ─────────────────────────────────────────────────────────────────
    try:
        data = _bybit_request("0")       # продавці → ти купуєш
        items = data.get("result", {}).get("items", [])
        results["bybit_ask"] = _filter_prices_bybit(items, sort_asc=True)
        results["bybit_count"] = data.get("result", {}).get("count", 0)
    except Exception:
        results["bybit_ask"] = 0
        results["bybit_count"] = 0

    try:
        data = _bybit_request("1")       # покупці → ти продаєш
        items = data.get("result", {}).get("items", [])
        results["bybit_bid"] = _filter_prices_bybit(items, sort_asc=False)
    except Exception:
        results["bybit_bid"] = 0

    return results

@st.cache_data(ttl=5)
def fetch_arb():
    """Gate / OKX / MEXC — кращий спред для кожної монети"""
    async def _fetch():
        async with httpx.AsyncClient() as client:
            all_tasks = {
                ex: [fn(client, c) for c in COINS]
                for ex, fn in EXCHANGE_FETCHERS.items()
            }
            gathered = await asyncio.gather(
                *[asyncio.gather(*tasks) for tasks in all_tasks.values()]
            )
            all_results = {
                ex: list(res)
                for ex, res in zip(all_tasks.keys(), gathered)
            }
            results = []
            for i, coin in enumerate(COINS):
                prices = {ex: all_results[ex][i] for ex in EXCHANGE_FETCHERS}
                r = best_spread_for_coin(coin, prices)
                if r:
                    results.append(r)
            return sorted(results, key=lambda x: x.spread_pct, reverse=True)
    return asyncio.run(_fetch())

# ─── HEADER ─────────────────────────────────────────────────────────────────
col_title, col_time = st.columns([4, 1])
with col_title:
    st.markdown("## 📊 Crypto Arbitrage Dashboard")
with col_time:
    now = datetime.now().strftime("%H:%M:%S")
    st.markdown(f"<br><span style='color:#888;font-size:14px'>🔄 {now}</span>", unsafe_allow_html=True)

st.divider()

# ─── ЗАВАНТАЖЕННЯ ДАНИХ ──────────────────────────────────────────────────────
p2p   = fetch_p2p()
spreads = fetch_arb()

# Зберігаємо в history
ts    = time.time()
b_ask = p2p.get("binance_ask", 0)
y_ask = p2p.get("bybit_ask",   0)
if b_ask or y_ask:
    avg = (b_ask + y_ask) / 2 if b_ask and y_ask else (b_ask or y_ask)
    st.session_state.p2p_history.append((ts, avg))

for r in spreads:
    if r.coin not in st.session_state.arb_history:
        st.session_state.arb_history[r.coin] = deque(maxlen=120)
    st.session_state.arb_history[r.coin].append((ts, r.spread_pct))

# ─── ROW 1: P2P + ARB OPPORTUNITIES ─────────────────────────────────────────
col_p2p, col_arb = st.columns([1, 2])

with col_p2p:
    st.markdown("### 💱 P2P USDT/UAH")
    st.caption("🏦 UA банки · від $200 · без merchant · топ-3 середнє")

    b_ask = p2p.get("binance_ask", 0)
    b_bid = p2p.get("binance_bid", 0)
    y_ask = p2p.get("bybit_ask",   0)
    y_bid = p2p.get("bybit_bid",   0)
    b_cnt = p2p.get("binance_count", 0)
    y_cnt = p2p.get("bybit_count",   0)

    # Таблиця 4 цін
    st.markdown(f"""
| | 🟡 Binance | 🔵 Bybit |
|---|---|---|
| 🛒 **Купити USDT** | `{b_ask:.2f} ₴` | `{y_ask:.2f} ₴` |
| 💰 **Продати USDT** | `{b_bid:.2f} ₴` | `{y_bid:.2f} ₴` |
| 📋 Оголошень | {b_cnt} | {y_cnt} |
""")

    # Спред купівля/продаж
    if b_ask and b_bid:
        sp_b = (b_ask - b_bid) / b_bid * 100
        st.caption(f"Binance спред: {sp_b:.2f}%")
    if y_ask and y_bid:
        sp_y = (y_ask - y_bid) / y_bid * 100
        st.caption(f"Bybit спред: {sp_y:.2f}%")
    if b_ask and y_ask:
        diff = abs(b_ask - y_ask) / min(b_ask, y_ask) * 100
        cheaper = "Binance" if b_ask < y_ask else "Bybit"
        st.info(f"Різниця між біржами: **{diff:.2f}%** — {cheaper} дешевше")

    # Графік USDT/UAH history
    history = list(st.session_state.p2p_history)
    if len(history) > 2:
        times  = [datetime.fromtimestamp(t) for t, _ in history]
        prices = [p for _, p in history]
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=times, y=prices,
            mode="lines", line=dict(color="#f0b90b", width=2),
            fill="tozeroy", fillcolor="rgba(240,185,11,0.1)"
        ))
        fig.update_layout(
            height=150, margin=dict(l=0, r=0, t=0, b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False, showticklabels=False),
            yaxis=dict(showgrid=True, gridcolor="#333", tickformat=".2f")
        )
        st.plotly_chart(fig, use_container_width=True)

with col_arb:
    st.markdown("### 🔀 Арбітраж — Gate / OKX / MEXC")

    opportunities = [r for r in spreads if r.is_opportunity]

    if opportunities:
        for r in opportunities:
            st.markdown(f"""
<div class="opp-card">
🚨 <b>{r.coin}/USDT</b><br>
Спред: <b style="color:#4ade80">{r.spread_pct:+.2f}%</b> &nbsp;|&nbsp;
Угода: <b>${r.max_trade_usd:,.0f}</b> &nbsp;|&nbsp;
Прибуток: <b style="color:#4ade80">${r.profit_usd:.1f}</b><br>
🟢 Купи <b>{r.buy_exchange.upper()}</b> ask <code>${r.buy_ask:.4f}</code> &nbsp;→&nbsp;
🔴 Продай <b>{r.sell_exchange.upper()}</b> bid <code>${r.sell_bid:.4f}</code>
</div>
""", unsafe_allow_html=True)
    else:
        best = spreads[0] if spreads else None
        best_txt = (f"Найкращий: <b>{best.coin}</b> {best.spread_pct:+.2f}% | "
                    f"Купи {best.buy_exchange.upper()} ${best.buy_ask:.4f} → "
                    f"Продай {best.sell_exchange.upper()} ${best.sell_bid:.4f}") if best else ""
        st.markdown(f"""
<div class="no-opp">
😴 Немає можливостей зараз<br>
<small>Поріг: реальний спред >{MIN_NET_PCT}% + глибина >${MIN_DEPTH_USD}<br>{best_txt}</small>
</div>
""", unsafe_allow_html=True)

# ─── ROW 2: ТАБЛИЦЯ (тільки прибуткові спреди > 0) ─────────────────────────
st.markdown("### 📋 Можливості — реальний спред > 0%")
st.caption("Показує тільки де buy ask < sell bid (реально прибуткові до комісій)")

visible = [r for r in spreads if r.spread_pct > 0]

if not visible:
    st.info("Зараз всі NET спреди від'ємні — біржі синхронні. Очікуй руху ринку.")
else:
    cols = st.columns([1, 1.4, 1.4, 0.9, 0.9, 0.9, 1.2, 1.1])
    headers = ["Монета", "Купити (ask)", "Продати (bid)", "Gross", "Fees", "NET", "MAX угода", "$NET profit"]
    for col, h in zip(cols, headers):
        col.markdown(f"**{h}**")

    for r in visible:
        cols = st.columns([1, 1.4, 1.4, 0.9, 0.9, 0.9, 1.2, 1.1])
        gross_color = "#f0b90b"
        net_color   = "#4ade80" if r.is_opportunity else ("#f0b90b" if r.net_spread_pct > 0 else "#f87171")
        fees_total  = r.buy_fee_pct + r.sell_fee_pct

        cols[0].markdown(f"**{r.coin}**" + (" 🚨" if r.is_opportunity else ""))
        cols[1].markdown(f"**{r.buy_exchange.upper()}**<br><code>${r.buy_ask:.4f}</code>", unsafe_allow_html=True)
        cols[2].markdown(f"**{r.sell_exchange.upper()}**<br><code>${r.sell_bid:.4f}</code>", unsafe_allow_html=True)
        cols[3].markdown(f"<span style='color:{gross_color}'>{r.gross_spread_pct:+.2f}%</span>", unsafe_allow_html=True)
        cols[4].markdown(f"<span style='color:#888'>-{fees_total:.2f}%</span>", unsafe_allow_html=True)
        cols[5].markdown(f"<span style='color:{net_color};font-weight:bold'>{r.net_spread_pct:+.2f}%</span>", unsafe_allow_html=True)
        cols[6].markdown(f"**${r.max_trade_usd:,.0f}**" + (" ✅" if r.is_opportunity else ""))
        cols[7].markdown(f"**${r.net_profit_usd:.1f}**" if r.net_profit_usd > 0 else "<span style='color:#f87171'>збиток</span>", unsafe_allow_html=True)

# ─── ROW 3: СПРЕД HISTORY ────────────────────────────────────────────────────
st.markdown("### 📈 Динаміка спреду (останні 10 хв)")

top_coins = [r.coin for r in spreads[:4]]
if any(c in st.session_state.arb_history for c in top_coins):
    fig2 = go.Figure()
    colors = ["#f0b90b", "#4ade80", "#60a5fa", "#f87171"]
    for coin, color in zip(top_coins, colors):
        hist = list(st.session_state.arb_history.get(coin, []))
        if len(hist) > 1:
            times  = [datetime.fromtimestamp(t) for t, _ in hist]
            values = [v for _, v in hist]
            fig2.add_trace(go.Scatter(
                x=times, y=values,
                name=coin, mode="lines",
                line=dict(color=color, width=2)
            ))

    fig2.add_hline(y=MIN_NET_PCT,  line_dash="dash", line_color="#4ade80",
                   annotation_text=f"Поріг +{MIN_NET_PCT}%")
    fig2.add_hline(y=-MIN_NET_PCT, line_dash="dash", line_color="#4ade80",
                   annotation_text=f"Поріг -{MIN_NET_PCT}%")
    fig2.add_hline(y=0, line_color="#555")

    fig2.update_layout(
        height=250, margin=dict(l=0, r=0, t=20, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.2),
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor="#333", tickformat="+.2f",
                   title="Спред %")
    )
    st.plotly_chart(fig2, use_container_width=True)
else:
    st.info("Накопичую дані для графіку... (~30 секунд)")

# ─── SIDEBAR — НАЛАШТУВАННЯ ──────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Налаштування")
    refresh = st.slider("Оновлення (сек)", 3, 60, 5)
    st.markdown("---")
    st.markdown("**Пороги алерту:**")
    min_spread = st.slider("Мін. спред %", 0.3, 3.0, float(MIN_NET_PCT), 0.1)
    min_depth  = st.slider("Мін. глибина $", 100, 5000, MIN_DEPTH_USD, 100)
    st.markdown("---")
    st.markdown("**Монети (реальний спред):**")
    for coin in COINS:
        r = next((x for x in spreads if x.coin == coin), None)
        if r:
            color = "🟢" if r.spread_pct >= min_spread else ("🟡" if r.spread_pct > 0 else "⚪")
            exs = f"{r.buy_exchange.upper()}→{r.sell_exchange.upper()}"
            st.markdown(f"{color} **{coin}**: {r.spread_pct:+.2f}% ({exs})")
    st.markdown("---")
    st.caption(f"⚠️ Тільки інформація. Не фін. порада.")

# ─── АВТО-ОНОВЛЕННЯ ──────────────────────────────────────────────────────────
time.sleep(refresh if 'refresh' in dir() else 5)
st.rerun()
