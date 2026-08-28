import os
import csv
import math
import time
import requests
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from datetime import datetime, timedelta, time as dtime
from scipy.stats import norm
from scipy.optimize import brentq
from dotenv import load_dotenv

# =========================================================
# 1. Environment Variable & Secret Loading
# =========================================================
load_dotenv()

DEFAULT_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DEFAULT_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

# =========================================================
# 2. Streamlit Page Configuration
# =========================================================
st.set_page_config(
    page_title="Nifty Live GEX, DEX, VEX & CHEX Web Engine",
    page_icon="📊",
    layout="wide"
)

OI_CSV_FILE = "nifty_gex_dex_log.csv"
VOL_CSV_FILE = "nifty_vol_gex_dex_log.csv"
VEX_CHEX_CSV_FILE = "nifty_vex_chex_log.csv"
ARCHIVE_DIR = "csv_archives"

OI_EXPECTED_HEADERS = [
    "Timestamp", "Spot_Price", "Expiry_Date", "DTE",
    "Call_GEX_Cr", "Put_GEX_Cr", "Net_GEX_Cr", "Zero_Gamma_Level", "GD", "GEX_Regime",
    "Call_DEX_Cr", "Put_DEX_Cr", "Net_DEX_Cr", "Zero_Delta_Level", "DD", "DEX_Regime",
    "Call_GEX_Wall", "Put_GEX_Wall", "Call_DEX_Wall", "Put_DEX_Wall",
    "Net_GEX_Change", "Net_DEX_Change", "GEX_Regime_Shift", "DEX_Regime_Shift", "Reversal_Signal"
]

VOL_EXPECTED_HEADERS = [
    "Timestamp", "Spot_Price", "Expiry_Date", "DTE",
    "Call_Vol_GEX_Cr", "Put_Vol_GEX_Cr", "Net_Vol_GEX_Cr", "Vol_Zero_Gamma_Level", "Vol_GD", "Vol_GEX_Regime",
    "Call_Vol_DEX_Cr", "Put_Vol_DEX_Cr", "Net_Vol_DEX_Cr", "Vol_Zero_Delta_Level", "Vol_DD", "Vol_DEX_Regime",
    "Vol_GEX_ZScore", "Vol_DEX_ZScore",
    "Vol_Net_GEX_Change", "Vol_Net_DEX_Change", "Vol_Regime_Shift", "Vol_Reversal_Signal"
]

VEX_CHEX_EXPECTED_HEADERS = [
    "Timestamp", "Spot_Price", "Expiry_Date", "DTE",
    "Total_Call_VEX_Cr", "Total_Put_VEX_Cr", "Net_VEX_Cr",
    "Total_Call_CHEX_Cr", "Total_Put_CHEX_Cr", "Net_CHEX_Cr"
]

if "iv_history" not in st.session_state:
    st.session_state["iv_history"] = {}

# =========================================================
# 3. Sidebar Configuration (Including Reversal Monitor Controls)
# =========================================================
st.sidebar.title("⚙️ Dhan API Settings")

CLIENT_ID = st.sidebar.text_input("Client ID", value=DEFAULT_CLIENT_ID, type="password")
ACCESS_TOKEN = st.sidebar.text_input("Access Token", value=DEFAULT_ACCESS_TOKEN, type="password")

UNDERLYING_SCRIP = 13  # 13 = NIFTY 50 Index
UNDERLYING_SEGMENT = "IDX_I"
LOT_SIZE = st.sidebar.number_input("Lot Size", value=65)

OI_GEX_STRIKES = st.sidebar.slider(
    "OI GEX ATM Strikes (Each Side)",
    min_value=3,
    max_value=15,
    value=3,
    step=3
)

STRIKE_RANGE_PCT = st.sidebar.slider(
    "OI DEX Strike Range (% of Spot)",
    min_value=5.0,
    max_value=30.0,
    value=20.0,
    step=5.0
) / 100.0

VOL_STRIKE_RANGE_PCT = st.sidebar.slider(
    "Volume Strike Range (% of Spot)",
    min_value=0.5,
    max_value=5.0,
    value=1.5,
    step=0.1
) / 100.0

VEX_CHEX_STRIKES = st.sidebar.slider(
    "VEX & CHEX ATM Strikes (Each Side)",
    min_value=3,
    max_value=15,
    value=3,
    step=3
)

REFRESH_RATE = st.sidebar.slider("Auto-Refresh (Seconds)", min_value=60, max_value=1800, value=60)

st.sidebar.markdown("---")
st.sidebar.title("🔄 Reversal Monitor Thresholds")
GEX_SURGE_THRESHOLD = st.sidebar.slider("Net GEX Surge Threshold (Cr)", min_value=10.0, max_value=500.0, value=50.0, step=10.0)
DEX_SURGE_THRESHOLD = st.sidebar.slider("Net DEX Surge Threshold (Cr)", min_value=10.0, max_value=500.0, value=50.0, step=10.0)


# =========================================================
# 4. Helper Functions & Metric Renderer
# =========================================================
def render_custom_metric(label, main_value, main_color, sub_value="", sub_color="#FFFFFF"):
    """Renders styled HTML metric cards."""
    sub_html = f'<div style="color: {sub_color} !important; font-size: 13px !important; font-weight: 600 !important; margin-top: 4px !important;">{sub_value}</div>' if sub_value else '<div style="height: 19px !important;"></div>'
    return f"""
    <div style="
        background-color: #1E222D !important;
        padding: 14px 16px !important;
        border-radius: 10px !important;
        border: 1px solid #2A2E39 !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.2) !important;
        margin-bottom: 10px !important;
    ">
        <div style="color: #9E9E9E !important; font-size: 13px !important; font-weight: 500 !important; margin-bottom: 6px !important;">{label}</div>
        <div style="color: {main_color} !important; font-size: 24px !important; font-weight: 700 !important; font-family: monospace !important;">{main_value}</div>
        {sub_html}
    </div>
    """


def get_upcoming_tuesday():
    """Calculates upcoming Tuesday expiry date (YYYY-MM-DD)."""
    today = datetime.now().date()
    days_ahead = (1 - today.weekday()) % 7
    if days_ahead == 0 and datetime.now().time() > dtime(15, 30):
        days_ahead = 7
    return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def calculate_exact_dte(expiry_date_str):
    """Calculates fractional DTE remaining until Tuesday 3:30 PM."""
    now = datetime.now()
    expiry_dt = datetime.strptime(f"{expiry_date_str} 15:30:00", "%Y-%m-%d %H:%M:%S")
    remaining_seconds = (expiry_dt - now).total_seconds()
    return max(remaining_seconds / (24 * 3600), 0.0001)


def bs_greeks(S, K, T, r, sigma):
    """Computes Black-Scholes Gamma, Call Delta, and Put Delta."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0, 0.0, 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        gamma = norm.pdf(d1) / (S * sigma * math.sqrt(T))
        call_delta = norm.cdf(d1)
        put_delta = call_delta - 1.0
        return gamma, call_delta, put_delta
    except Exception:
        return 0.0, 0.0, 0.0


def bs_all_greeks(S, K, T, r, sigma):
    """Computes Gamma, Delta, Vanna, and Charm for VEX and CHEX calculations."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    try:
        sqrt_T = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T

        pdf_d1 = norm.pdf(d1)
        gamma = pdf_d1 / (S * sigma * sqrt_T)
        call_delta = norm.cdf(d1)
        put_delta = call_delta - 1.0

        vanna = -pdf_d1 * d2 / sigma if sigma > 0 else 0.0

        term1 = pdf_d1 * (2 * r * T - d2 * sigma * sqrt_T) / (2 * T * sigma * sqrt_T)
        call_charm = term1 - r * math.exp(-r * T) * norm.cdf(d1)
        put_charm = term1 + r * math.exp(-r * T) * norm.cdf(-d1)

        return gamma, call_delta, put_delta, vanna, call_charm, put_charm
    except Exception:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0


def total_gex(S, options_chain, lot_size=65, r=0.07):
    """Root-finding objective function for Open Interest Zero Gamma Level (GEX)."""
    total_gex_val = 0.0
    for opt in options_chain:
        iv = opt.get('avg_iv', 0.15)
        gamma, _, _ = bs_greeks(S, opt['strike'], opt['dte'] / 365.0, r, iv)
        strike_gex = (opt['call_oi'] - opt['put_oi']) * gamma * lot_size * (S ** 2) * 0.01
        total_gex_val += strike_gex
    return total_gex_val


def total_dex(S, options_chain, lot_size=65, r=0.07):
    """Root-finding objective function for Open Interest Zero Delta Level (DEX)."""
    total_dex_val = 0.0
    for opt in options_chain:
        iv = opt.get('avg_iv', 0.15)
        _, c_delta, p_delta = bs_greeks(S, opt['strike'], opt['dte'] / 365.0, r, iv)
        strike_call_dex = opt['call_oi'] * c_delta * lot_size * S
        strike_put_dex = opt['put_oi'] * p_delta * lot_size * S
        total_dex_val += (strike_call_dex + strike_put_dex)
    return total_dex_val


def total_vol_gex(S, options_chain, lot_size=65, r=0.07):
    """Root-finding objective function for Volume Zero Gamma Level."""
    total_gex_val = 0.0
    for opt in options_chain:
        iv = opt.get('avg_iv', 0.15)
        gamma, _, _ = bs_greeks(S, opt['strike'], opt['dte'] / 365.0, r, iv)
        strike_gex = (opt['call_vol'] - opt['put_vol']) * gamma * lot_size * (S ** 2) * 0.01
        total_gex_val += strike_gex
    return total_gex_val


def total_vol_dex(S, options_chain, lot_size=65, r=0.07):
    """Root-finding objective function for Volume Zero Delta Level."""
    total_dex_val = 0.0
    for opt in options_chain:
        iv = opt.get('avg_iv', 0.15)
        _, c_delta, p_delta = bs_greeks(S, opt['strike'], opt['dte'] / 365.0, r, iv)
        strike_call_dex = opt['call_vol'] * c_delta * lot_size * S
        strike_put_dex = opt['put_vol'] * p_delta * lot_size * S
        total_dex_val += (strike_call_dex + strike_put_dex)
    return total_dex_val


def find_zero_level(func, options_chain, spot_price, lot_size=65, r=0.07):
    """Finds zero crossover level using brentq across restricted options chain."""
    if not options_chain or len(options_chain) < 2:
        return spot_price, 0.0

    strikes = [opt['strike'] for opt in options_chain]
    low_bound = min(strikes)
    high_bound = max(strikes)

    try:
        f_low = func(low_bound, options_chain, lot_size, r)
        f_high = func(high_bound, options_chain, lot_size, r)

        if f_low * f_high > 0:
            return spot_price, 0.0

        val = round(brentq(func, low_bound, high_bound, args=(options_chain, lot_size, r)), 2)
        return val, round(spot_price - val, 2)
    except Exception:
        return spot_price, 0.0


def calculate_z_score(val, history_series, window=20):
    """Calculates Z-Score relative to 20-period/day history window."""
    if history_series is None or len(history_series) < 2:
        return 0.0
    recent_series = pd.Series(history_series).tail(window)
    std_dev = recent_series.std()
    if std_dev == 0 or pd.isna(std_dev):
        return 0.0
    mean_val = recent_series.mean()
    return (val - mean_val) / std_dev


def safe_read_csv(filename):
    """Safely reads CSV using pandas, skipping malformed legacy rows."""
    if not os.path.exists(filename):
        return pd.DataFrame()
    try:
        return pd.read_csv(filename, on_bad_lines='skip')
    except Exception:
        return pd.DataFrame()


def init_csv(filename, headers):
    """Creates CSV log or aligns headers if structure changed."""
    if not os.path.exists(filename):
        with open(filename, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
    else:
        with open(filename, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            existing_headers = next(reader, None)

        if existing_headers != headers:
            with open(filename, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(headers)


def handle_csv_rollover(filename, headers):
    """Archives CSV log daily and resets table for a new day."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    if not os.path.exists(filename):
        init_csv(filename, headers)
        return

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    try:
        df = safe_read_csv(filename)
        if df.empty or 'Timestamp' not in df.columns:
            return

        first_timestamp = str(df['Timestamp'].iloc[0]).strip()
        file_date = first_timestamp.split(" ")[0]
        base_name = os.path.splitext(filename)[0]
        archive_path = os.path.join(ARCHIVE_DIR, f"{base_name}_{file_date}.csv")

        if file_date == today_str and now.time() >= dtime(15, 45):
            if not os.path.exists(archive_path):
                df.to_csv(archive_path, index=False)

        if file_date != today_str:
            if not os.path.exists(archive_path):
                df.to_csv(archive_path, index=False)

            with open(filename, mode='w', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(headers)

    except Exception:
        pass


def log_to_csv(filename, headers, row_data):
    """Appends entry to CSV log after rollover check."""
    handle_csv_rollover(filename, headers)
    init_csv(filename, headers)
    with open(filename, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(row_data)


# =========================================================
# 5. Live Data Fetcher & Calculator (With Reversal Monitor Integration)
# =========================================================
def fetch_and_calculate_exposures():
    if not CLIENT_ID or not ACCESS_TOKEN or CLIENT_ID == "YOUR_ACTUAL_CLIENT_ID":
        st.warning("⚠️ Please provide valid Dhan credentials in your `.env` file or sidebar.")
        return None

    headers = {
        "access-token": ACCESS_TOKEN,
        "client-id": CLIENT_ID,
        "Content-Type": "application/json"
    }

    expiry_date = get_upcoming_tuesday()
    days_to_expiry = calculate_exact_dte(expiry_date)

    url = "https://api.dhan.co/v2/optionchain"
    payload = {
        "UnderlyingScrip": UNDERLYING_SCRIP,
        "UnderlyingSeg": UNDERLYING_SEGMENT,
        "Expiry": expiry_date
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code != 200:
            st.error(f"API Connection Failed ({response.status_code}): {response.text}")
            return None

        data = response.json()
        if data.get("status") != "success":
            st.error("Option Chain data fetch failed from Dhan API.")
            return None

        spot_price = data["data"].get("last_price", 0.0)
        oc_data = data["data"].get("oc", {})

        T = days_to_expiry / 365.0
        r = 0.07

        full_raw_chain = []
        for strike_str, val in oc_data.items():
            strike_price = float(strike_str)

            ce_dict = val.get("ce", {}) or {}
            pe_dict = val.get("pe", {}) or {}

            call_oi = ce_dict.get("oi", 0)
            put_oi = pe_dict.get("oi", 0)
            call_vol = ce_dict.get("volume", 0)
            put_vol = pe_dict.get("volume", 0)

            c_iv_raw = ce_dict.get("implied_volatility") or ce_dict.get("iv") or ce_dict.get("impliedVolatility") or 15.0
            p_iv_raw = pe_dict.get("implied_volatility") or pe_dict.get("iv") or pe_dict.get("impliedVolatility") or 15.0

            try:
                call_iv = float(c_iv_raw)
                if call_iv > 1.0:
                    call_iv /= 100.0
            except (ValueError, TypeError):
                call_iv = 0.15

            try:
                put_iv = float(p_iv_raw)
                if put_iv > 1.0:
                    put_iv /= 100.0
            except (ValueError, TypeError):
                put_iv = 0.15

            avg_iv = (call_iv + put_iv) / 2.0 if (call_iv + put_iv) > 0 else 0.15

            call_gamma, call_delta, _ = bs_greeks(spot_price, strike_price, T, r, call_iv if call_iv > 0 else avg_iv)
            put_gamma, _, put_delta = bs_greeks(spot_price, strike_price, T, r, put_iv if put_iv > 0 else avg_iv)

            full_raw_chain.append({
                'strike': strike_price,
                'call_oi': call_oi,
                'put_oi': put_oi,
                'call_vol': call_vol,
                'put_vol': put_vol,
                'call_iv': call_iv if call_iv > 0 else avg_iv,
                'put_iv': put_iv if put_iv > 0 else avg_iv,
                'avg_iv': avg_iv,
                'call_gamma': call_gamma,
                'put_gamma': put_gamma,
                'call_delta': call_delta,
                'put_delta': put_delta,
                'dte': days_to_expiry
            })

        if not full_raw_chain:
            st.error("No option chain data retrieved.")
            return None

        full_raw_chain.sort(key=lambda x: x['strike'])

        atm_strike_obj = min(full_raw_chain, key=lambda x: abs(x['strike'] - spot_price))
        atm_index = full_raw_chain.index(atm_strike_obj)

        gex_start_idx = max(0, atm_index - OI_GEX_STRIKES)
        gex_end_idx = min(len(full_raw_chain), atm_index + OI_GEX_STRIKES + 1)
        gex_chain = full_raw_chain[gex_start_idx:gex_end_idx]
        gex_strike_set = set(opt['strike'] for opt in gex_chain)

        lower_strike_bound = spot_price * (1.0 - STRIKE_RANGE_PCT)
        upper_strike_bound = spot_price * (1.0 + STRIKE_RANGE_PCT)

        vol_lower_strike_bound = spot_price * (1.0 - VOL_STRIKE_RANGE_PCT)
        vol_upper_strike_bound = spot_price * (1.0 + VOL_STRIKE_RANGE_PCT)

        dex_chain = [opt for opt in full_raw_chain if lower_strike_bound <= opt['strike'] <= upper_strike_bound]
        vol_chain = [opt for opt in full_raw_chain if vol_lower_strike_bound <= opt['strike'] <= vol_upper_strike_bound]

        gex_strike_exposure_list = []
        dex_strike_exposure_list = []

        total_call_gex_rs = 0.0
        total_put_gex_rs = 0.0
        total_call_dex_rs = 0.0
        total_put_dex_rs = 0.0

        total_call_vol_gex_rs = 0.0
        total_put_vol_gex_rs = 0.0
        total_call_vol_dex_rs = 0.0
        total_put_vol_dex_rs = 0.0

        for item in full_raw_chain:
            strike_price = item['strike']
            call_oi = item['call_oi']
            put_oi = item['put_oi']
            call_vol = item['call_vol']
            put_vol = item['put_vol']
            call_gamma = item['call_gamma']
            put_gamma = item['put_gamma']
            call_delta = item['call_delta']
            put_delta = item['put_delta']

            if strike_price in gex_strike_set:
                c_gex = call_gamma * call_oi * LOT_SIZE * (spot_price ** 2) * 0.01
                p_gex = -1.0 * (put_gamma * put_oi * LOT_SIZE * (spot_price ** 2) * 0.01)

                total_call_gex_rs += c_gex
                total_put_gex_rs += p_gex

                gex_strike_exposure_list.append({
                    'strike': strike_price,
                    'call_gex_cr': round((c_gex / 1e7) / 1000.0, 2),
                    'put_gex_cr': round((p_gex / 1e7) / 1000.0, 2),
                    'net_gex_cr': round(((c_gex + p_gex) / 1e7) / 1000.0, 2),
                    'call_oi': call_oi,
                    'put_oi': put_oi
                })

            if lower_strike_bound <= strike_price <= upper_strike_bound:
                c_dex = call_delta * call_oi * LOT_SIZE * spot_price
                p_dex = put_delta * put_oi * LOT_SIZE * spot_price

                total_call_dex_rs += c_dex
                total_put_dex_rs += p_dex

                dex_strike_exposure_list.append({
                    'strike': strike_price,
                    'call_dex_cr': round((c_dex / 1e7) / 1000.0, 2),
                    'put_dex_cr': round((p_dex / 1e7) / 1000.0, 2),
                    'net_dex_cr': round(((c_dex + p_dex) / 1e7) / 1000.0, 2),
                    'call_oi': call_oi,
                    'put_oi': put_oi
                })

            if vol_lower_strike_bound <= strike_price <= vol_upper_strike_bound:
                c_vol_gex = call_gamma * call_vol * LOT_SIZE * (spot_price ** 2) * 0.01
                p_vol_gex = -1.0 * (put_gamma * put_vol * LOT_SIZE * (spot_price ** 2) * 0.01)

                c_vol_dex = call_delta * call_vol * LOT_SIZE * spot_price
                p_vol_dex = put_delta * put_vol * LOT_SIZE * spot_price

                total_call_vol_gex_rs += c_vol_gex
                total_put_vol_gex_rs += p_vol_gex
                total_call_vol_dex_rs += c_vol_dex
                total_put_vol_dex_rs += p_vol_dex

        if not gex_strike_exposure_list:
            st.error("No option strikes found within selected GEX ATM range.")
            return None

        # VEX & CHEX Strike Tables with Strike IV Z-Scores
        vex_start_idx = max(0, atm_index - VEX_CHEX_STRIKES)
        vex_end_idx = min(len(full_raw_chain), atm_index + VEX_CHEX_STRIKES + 1)
        vex_chex_chain = full_raw_chain[vex_start_idx:vex_end_idx]

        vex_chex_table_rows = []
        vex_chex_strike_exposure_list = []

        total_call_vex_rs = 0.0
        total_put_vex_rs = 0.0
        total_call_chex_rs = 0.0
        total_put_chex_rs = 0.0

        for opt in vex_chex_chain:
            k = opt['strike']
            c_iv = opt['call_iv']
            p_iv = opt['put_iv']

            c_key = f"{int(k)}_CE"
            p_key = f"{int(k)}_PE"

            if c_key not in st.session_state["iv_history"]:
                st.session_state["iv_history"][c_key] = []
            if p_key not in st.session_state["iv_history"]:
                st.session_state["iv_history"][p_key] = []

            st.session_state["iv_history"][c_key].append(c_iv * 100.0)
            st.session_state["iv_history"][p_key].append(p_iv * 100.0)

            c_iv_zscore = calculate_z_score(c_iv * 100.0, st.session_state["iv_history"][c_key], window=20)
            p_iv_zscore = calculate_z_score(p_iv * 100.0, st.session_state["iv_history"][p_key], window=20)

            _, _, _, c_vanna, c_charm, _ = bs_all_greeks(spot_price, k, T, r, c_iv)
            _, _, _, p_vanna, _, p_charm = bs_all_greeks(spot_price, k, T, r, p_iv)

            c_vex = c_vanna * opt['call_oi'] * LOT_SIZE * spot_price * 0.01
            p_vex = -1.0 * (p_vanna * opt['put_oi'] * LOT_SIZE * spot_price * 0.01)
            net_vex_strike = c_vex + p_vex

            c_chex = c_charm * opt['call_oi'] * LOT_SIZE * spot_price / 365.0
            p_chex = p_charm * opt['put_oi'] * LOT_SIZE * spot_price / 365.0
            net_chex_strike = c_chex + p_chex

            total_call_vex_rs += c_vex
            total_put_vex_rs += p_vex
            total_call_chex_rs += c_chex
            total_put_chex_rs += p_chex

            vex_chex_strike_exposure_list.append({
                'strike': k,
                'call_vex_cr': round((c_vex / 1e7) / 1000.0, 4),
                'put_vex_cr': round((p_vex / 1e7) / 1000.0, 4),
                'net_vex_cr': round((net_vex_strike / 1e7) / 1000.0, 4),
                'call_chex_cr': round((c_chex / 1e7) / 1000.0, 4),
                'put_chex_cr': round((p_chex / 1e7) / 1000.0, 4),
                'net_chex_cr': round((net_chex_strike / 1e7) / 1000.0, 4)
            })

            vex_chex_table_rows.append({
                "Strike": int(k),
                "Call/Put": "Call",
                "IV (%)": round(c_iv * 100, 2),
                "IV Z-Score": round(c_iv_zscore, 2),
                "Call/Put VEX": round((c_vex / 1e7) / 1000.0, 4),
                "Net VEX": round((net_vex_strike / 1e7) / 1000.0, 4),
                "Call/Put CHEX": round((c_chex / 1e7) / 1000.0, 4),
                "Net CHEX": round((net_chex_strike / 1e7) / 1000.0, 4)
            })

            vex_chex_table_rows.append({
                "Strike": int(k),
                "Call/Put": "Put",
                "IV (%)": round(p_iv * 100, 2),
                "IV Z-Score": round(p_iv_zscore, 2),
                "Call/Put VEX": round((p_vex / 1e7) / 1000.0, 4),
                "Net VEX": round((net_vex_strike / 1e7) / 1000.0, 4),
                "Call/Put CHEX": round((p_chex / 1e7) / 1000.0, 4),
                "Net CHEX": round((net_chex_strike / 1e7) / 1000.0, 4)
            })

        total_call_gex_cr = round((total_call_gex_rs / 1e7) / 1000.0, 2)
        total_put_gex_cr = round((total_put_gex_rs / 1e7) / 1000.0, 2)
        net_gex_cr = round(((total_call_gex_rs + total_put_gex_rs) / 1e7) / 1000.0, 2)

        total_call_dex_cr = round((total_call_dex_rs / 1e7) / 1000.0, 2)
        total_put_dex_cr = round((total_put_dex_rs / 1e7) / 1000.0, 2)
        net_dex_cr = round(((total_call_dex_rs + total_put_dex_rs) / 1e7) / 1000.0, 2)

        call_gex_wall = max(gex_strike_exposure_list, key=lambda x: x['call_gex_cr'])['strike']
        put_gex_wall = min(gex_strike_exposure_list, key=lambda x: x['put_gex_cr'])['strike']

        call_dex_wall = max(dex_strike_exposure_list, key=lambda x: x['call_dex_cr'])['strike'] if dex_strike_exposure_list else 0
        put_dex_wall = min(dex_strike_exposure_list, key=lambda x: x['put_dex_cr'])['strike'] if dex_strike_exposure_list else 0

        total_call_vol_gex_cr = round((total_call_vol_gex_rs / 1e7) / 1000.0, 2)
        total_put_vol_gex_cr = round((total_put_vol_gex_rs / 1e7) / 1000.0, 2)
        net_vol_gex_cr = round(((total_call_vol_gex_rs + total_put_vol_gex_rs) / 1e7) / 1000.0, 2)

        total_call_vol_dex_cr = round((total_call_vol_dex_rs / 1e7) / 1000.0, 2)
        total_put_vol_dex_cr = round((total_put_vol_dex_rs / 1e7) / 1000.0, 2)
        net_vol_dex_cr = round(((total_call_vol_dex_rs + total_put_vol_dex_rs) / 1e7) / 1000.0, 2)

        zero_gamma_val, gd_val = find_zero_level(total_gex, gex_chain, spot_price, LOT_SIZE)
        zero_delta_val, dd_val = find_zero_level(total_dex, dex_chain, spot_price, LOT_SIZE)

        vol_zero_gamma_val, vol_gd_val = find_zero_level(total_vol_gex, vol_chain, spot_price, LOT_SIZE)
        vol_zero_delta_val, vol_dd_val = find_zero_level(total_vol_dex, vol_chain, spot_price, LOT_SIZE)

        gex_regime = "POSITIVE GEX" if net_gex_cr >= 0 else "NEGATIVE GEX"
        dex_regime = "BULLISH DEX" if net_dex_cr >= 0 else "BEARISH DEX"

        vol_gex_regime = "POSITIVE VOL GEX" if net_vol_gex_cr >= 0 else "NEGATIVE VOL GEX"
        vol_dex_regime = "BULLISH VOL DEX" if net_vol_dex_cr >= 0 else "BEARISH VOL DEX"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Historical Volume Z-Scores (20-period window)
        vol_gex_zscore = 0.0
        vol_dex_zscore = 0.0
        df_vol_hist = safe_read_csv(VOL_CSV_FILE)
        if not df_vol_hist.empty and 'Net_Vol_GEX_Cr' in df_vol_hist.columns:
            try:
                vol_gex_zscore = calculate_z_score(net_vol_gex_cr, pd.to_numeric(df_vol_hist['Net_Vol_GEX_Cr'], errors='coerce'), window=20)
                vol_dex_zscore = calculate_z_score(net_vol_dex_cr, pd.to_numeric(df_vol_hist['Net_Vol_DEX_Cr'], errors='coerce'), window=20)
            except Exception:
                pass

        total_call_vex_cr = round((total_call_vex_rs / 1e7) / 1000.0, 4)
        total_put_vex_cr = round((total_put_vex_rs / 1e7) / 1000.0, 4)
        net_vex_cr = round(((total_call_vex_rs + total_put_vex_rs) / 1e7) / 1000.0, 4)

        total_call_chex_cr = round((total_call_chex_rs / 1e7) / 1000.0, 4)
        total_put_chex_cr = round((total_put_chex_rs / 1e7) / 1000.0, 4)
        net_chex_cr = round(((total_call_chex_rs + total_put_chex_rs) / 1e7) / 1000.0, 4)

        # =========================================================
        # REVERSAL MONITOR CALCULATIONS
        # =========================================================
        prev_net_gex = 0.0
        prev_net_dex = 0.0
        prev_gex_regime = gex_regime
        prev_dex_regime = dex_regime

        df_oi_hist = safe_read_csv(OI_CSV_FILE)
        if not df_oi_hist.empty and 'Net_GEX_Cr' in df_oi_hist.columns:
            try:
                prev_net_gex = float(df_oi_hist['Net_GEX_Cr'].iloc[-1])
                prev_net_dex = float(df_oi_hist['Net_DEX_Cr'].iloc[-1])
                prev_gex_regime = str(df_oi_hist['GEX_Regime'].iloc[-1])
                prev_dex_regime = str(df_oi_hist['DEX_Regime'].iloc[-1])
            except Exception:
                pass

        net_gex_change = round(net_gex_cr - prev_net_gex, 2)
        net_dex_change = round(net_dex_cr - prev_net_dex, 2)
        gex_regime_shift = 1 if gex_regime != prev_gex_regime else 0
        dex_regime_shift = 1 if dex_regime != prev_dex_regime else 0
        reversal_signal = 1 if (abs(net_gex_change) >= GEX_SURGE_THRESHOLD or abs(net_dex_change) >= DEX_SURGE_THRESHOLD or gex_regime_shift or dex_regime_shift) else 0

        # Volume Reversal Calculations
        prev_vol_net_gex = 0.0
        prev_vol_net_dex = 0.0
        prev_vol_gex_regime = vol_gex_regime
        prev_vol_dex_regime = vol_dex_regime

        if not df_vol_hist.empty and 'Net_Vol_GEX_Cr' in df_vol_hist.columns:
            try:
                prev_vol_net_gex = float(df_vol_hist['Net_Vol_GEX_Cr'].iloc[-1])
                prev_vol_net_dex = float(df_vol_hist['Net_Vol_DEX_Cr'].iloc[-1])
                prev_vol_gex_regime = str(df_vol_hist['Vol_GEX_Regime'].iloc[-1])
                prev_vol_dex_regime = str(df_vol_hist['Vol_DEX_Regime'].iloc[-1])
            except Exception:
                pass

        vol_net_gex_change = round(net_vol_gex_cr - prev_vol_net_gex, 2)
        vol_net_dex_change = round(net_vol_dex_cr - prev_vol_net_dex, 2)
        vol_regime_shift = 1 if (vol_gex_regime != prev_vol_gex_regime or vol_dex_regime != prev_vol_dex_regime) else 0
        vol_reversal_signal = 1 if (abs(vol_net_gex_change) >= GEX_SURGE_THRESHOLD or abs(vol_net_dex_change) >= DEX_SURGE_THRESHOLD or vol_regime_shift) else 0

        # Log Data to CSV Files (Including Reversal Monitor Outputs)
        log_to_csv(OI_CSV_FILE, OI_EXPECTED_HEADERS, [
            now_str, f"{spot_price:.2f}", expiry_date, f"{days_to_expiry:.3f}",
            f"{total_call_gex_cr:.2f}", f"{total_put_gex_cr:.2f}", f"{net_gex_cr:.2f}",
            f"{zero_gamma_val:.2f}", f"{gd_val:.2f}", gex_regime,
            f"{total_call_dex_cr:.2f}", f"{total_put_dex_cr:.2f}", f"{net_dex_cr:.2f}",
            f"{zero_delta_val:.2f}", f"{dd_val:.2f}", dex_regime,
            int(call_gex_wall), int(put_gex_wall), int(call_dex_wall), int(put_dex_wall),
            f"{net_gex_change:.2f}", f"{net_dex_change:.2f}", gex_regime_shift, dex_regime_shift, reversal_signal
        ])

        log_to_csv(VOL_CSV_FILE, VOL_EXPECTED_HEADERS, [
            now_str, f"{spot_price:.2f}", expiry_date, f"{days_to_expiry:.3f}",
            f"{total_call_vol_gex_cr:.2f}", f"{total_put_vol_gex_cr:.2f}", f"{net_vol_gex_cr:.2f}",
            f"{vol_zero_gamma_val:.2f}", f"{vol_gd_val:.2f}", vol_gex_regime,
            f"{total_call_vol_dex_cr:.2f}", f"{total_put_vol_dex_cr:.2f}", f"{net_vol_dex_cr:.2f}",
            f"{vol_zero_delta_val:.2f}", f"{vol_dd_val:.2f}", vol_dex_regime,
            f"{vol_gex_zscore:.2f}", f"{vol_dex_zscore:.2f}",
            f"{vol_net_gex_change:.2f}", f"{vol_net_dex_change:.2f}", vol_regime_shift, vol_reversal_signal
        ])

        log_to_csv(VEX_CHEX_CSV_FILE, VEX_CHEX_EXPECTED_HEADERS, [
            now_str, f"{spot_price:.2f}", expiry_date, f"{days_to_expiry:.3f}",
            f"{total_call_vex_cr:.4f}", f"{total_put_vex_cr:.4f}", f"{net_vex_cr:.4f}",
            f"{total_call_chex_cr:.4f}", f"{total_put_chex_cr:.4f}", f"{net_chex_cr:.4f}"
        ])

        return {
            "spot_price": spot_price,
            "expiry_date": expiry_date,
            "dte": days_to_expiry,
            "total_call_gex_cr": total_call_gex_cr,
            "total_put_gex_cr": total_put_gex_cr,
            "net_gex_cr": net_gex_cr,
            "zero_gamma_val": zero_gamma_val,
            "gd_val": gd_val,
            "gex_regime": gex_regime,
            "call_gex_wall": call_gex_wall,
            "put_gex_wall": put_gex_wall,
            "total_call_dex_cr": total_call_dex_cr,
            "total_put_dex_cr": total_put_dex_cr,
            "net_dex_cr": net_dex_cr,
            "zero_delta_val": zero_delta_val,
            "dd_val": dd_val,
            "dex_regime": dex_regime,
            "call_dex_wall": call_dex_wall,
            "put_dex_wall": put_dex_wall,
            "gex_strike_exposure_list": gex_strike_exposure_list,
            "dex_strike_exposure_list": dex_strike_exposure_list,
            "vex_chex_strike_exposure_list": vex_chex_strike_exposure_list,
            "total_call_vol_gex_cr": total_call_vol_gex_cr,
            "total_put_vol_gex_cr": total_put_vol_gex_cr,
            "net_vol_gex_cr": net_vol_gex_cr,
            "vol_zero_gamma_val": vol_zero_gamma_val,
            "vol_gd_val": vol_gd_val,
            "vol_gex_regime": vol_gex_regime,
            "total_call_vol_dex_cr": total_call_vol_dex_cr,
            "total_put_vol_dex_cr": total_put_vol_dex_cr,
            "net_vol_dex_cr": net_vol_dex_cr,
            "vol_zero_delta_val": vol_zero_delta_val,
            "vol_dd_val": vol_dd_val,
            "vol_dex_regime": vol_dex_regime,
            "vol_gex_zscore": vol_gex_zscore,
            "vol_dex_zscore": vol_dex_zscore,
            "total_call_vex_cr": total_call_vex_cr,
            "total_put_vex_cr": total_put_vex_cr,
            "net_vex_cr": net_vex_cr,
            "total_call_chex_cr": total_call_chex_cr,
            "total_put_chex_cr": total_put_chex_cr,
            "net_chex_cr": net_chex_cr,
            "vex_chex_table": vex_chex_table_rows,
            "timestamp": now_str,
            "lower_strike_bound": lower_strike_bound,
            "upper_strike_bound": upper_strike_bound,
            "vol_lower_strike_bound": vol_lower_strike_bound,
            "vol_upper_strike_bound": vol_upper_strike_bound,
            "net_gex_change": net_gex_change,
            "net_dex_change": net_dex_change,
            "gex_regime_shift": gex_regime_shift,
            "dex_regime_shift": dex_regime_shift,
            "reversal_signal": reversal_signal
        }

    except Exception as e:
        st.error(f"Execution Error: {e}")
        return None


# =========================================================
# 6. Dashboard Interface Rendering
# =========================================================
st.title("📈 Nifty Live GEX, DEX, VEX & CHEX Dashboard")

data = fetch_and_calculate_exposures()

if data:
    GREEN_COLOR = "#00E676"
    RED_COLOR = "#FF3D00"
    BLUE_COLOR = "#29B6F6"

    spot = data['spot_price']
    zg = data['zero_gamma_val']
    zd = data['zero_delta_val']
    net_gex = data['net_gex_cr']
    net_dex = data['net_dex_cr']
    gd = data['gd_val']
    dd = data['dd_val']

    spot_color = GREEN_COLOR if spot > zg and spot > zd else (RED_COLOR if spot < zg and spot < zd else BLUE_COLOR)
    net_gex_color = GREEN_COLOR if net_gex >= 0 else RED_COLOR
    net_dex_color = GREEN_COLOR if net_dex >= 0 else RED_COLOR
    gd_color = GREEN_COLOR if gd >= 0 else RED_COLOR
    dd_color = GREEN_COLOR if dd >= 0 else RED_COLOR

    col_h1, col_h2, col_h3 = st.columns(3)
    col_h1.caption(f"📅 Target Expiry: **{data['expiry_date']} (Tuesday)**")
    col_h2.caption(f"⏱️ DTE Remaining: **{data['dte']:.3f} Days**")
    col_h3.caption(
        f"🎯 GEX: **ATM ±{OI_GEX_STRIKES} Strikes** | DEX Range: **₹{data['lower_strike_bound']:,.0f} - ₹{data['upper_strike_bound']:,.0f} (±{STRIKE_RANGE_PCT * 100:.1f}%)** | Vol: **±{VOL_STRIKE_RANGE_PCT * 100:.1f}%**"
    )

    st.markdown("---")

    # Reversal Monitor Alert Banner
    if data['reversal_signal'] == 1:
        st.error(f"🚨 **REVERSAL MONITOR ALERT:** Significant surge or regime shift detected! Net GEX Δ: `{data['net_gex_change']:+,.2f} Cr`, Net DEX Δ: `{data['net_dex_change']:+,.2f} Cr`")
    else:
        st.success("✅ **Reversal Monitor:** Market stable within threshold boundaries.")

    st.markdown("### 📍 OI Price Levels & Regimes")
    m1, m2, m3, m4, m5 = st.columns(5)

    with m1:
        st.markdown(render_custom_metric("Nifty Spot", f"₹{spot:,.2f}", spot_color), unsafe_allow_html=True)
    with m2:
        st.markdown(render_custom_metric("Net GEX", f"₹{net_gex:,.2f}", net_gex_color, data["gex_regime"], net_gex_color), unsafe_allow_html=True)
    with m3:
        st.markdown(render_custom_metric("Net DEX", f"₹{net_dex:,.2f}", net_dex_color, data["dex_regime"], net_dex_color), unsafe_allow_html=True)
    with m4:
        st.markdown(render_custom_metric("Zero Gamma Level", f"₹{zg:,.2f}", net_gex_color, f"GD: {gd:+,.2f}", gd_color), unsafe_allow_html=True)
    with m5:
        st.markdown(render_custom_metric("Zero Delta Level", f"₹{zd:,.2f}", net_dex_color, f"DD: {dd:+,.2f}", dd_color), unsafe_allow_html=True)

    st.markdown("---")

    st.markdown(f"### 🌊 Vanna (VEX) & Charm Exposure (CHEX) — ATM ±{VEX_CHEX_STRIKES} Strikes")
    vx1, vx2 = st.columns(2)

    net_vex = data['net_vex_cr']
    net_chex = data['net_chex_cr']

    vex_color = GREEN_COLOR if net_vex >= 0 else RED_COLOR
    chex_color = GREEN_COLOR if net_chex >= 0 else RED_COLOR

    with vx1:
        st.markdown(render_custom_metric("Net VEX (Vanna)", f"₹{net_vex:,.4f} Cr", vex_color, "Volatility Sensitivity"), unsafe_allow_html=True)
    with vx2:
        st.markdown(render_custom_metric("Net CHEX (Charm)", f"₹{net_chex:,.4f} Cr", chex_color, "Time Sensitivity"), unsafe_allow_html=True)

    st.markdown("---")

    st.markdown(f"### ⚡ Intraday Volume Exposure & Z-Scores (±{VOL_STRIKE_RANGE_PCT * 100:.1f}% Range)")
    vm1, vm2, vm3, vm4, vm5, vm6 = st.columns(6)

    vol_net_gex = data['net_vol_gex_cr']
    vol_net_dex = data['net_vol_dex_cr']
    vol_zg = data['vol_zero_gamma_val']
    vol_zd = data['vol_zero_delta_val']

    v_gex_color = GREEN_COLOR if vol_net_gex >= 0 else RED_COLOR
    v_dex_color = GREEN_COLOR if vol_net_dex >= 0 else RED_COLOR

    z_gex = data['vol_gex_zscore']
    z_dex = data['vol_dex_zscore']

    with vm1:
        st.markdown(render_custom_metric("Net Vol GEX", f"₹{vol_net_gex:,.2f}", v_gex_color, data["vol_gex_regime"], v_gex_color), unsafe_allow_html=True)
    with vm2:
        st.markdown(render_custom_metric("Net Vol DEX", f"₹{vol_net_dex:,.2f}", v_dex_color, data["vol_dex_regime"], v_dex_color), unsafe_allow_html=True)
    with vm3:
        st.markdown(render_custom_metric("Vol Zero Gamma", f"₹{vol_zg:,.2f}", v_gex_color, f"GD: {data['vol_gd_val']:+,.2f}", v_gex_color), unsafe_allow_html=True)
    with vm4:
        st.markdown(render_custom_metric("Vol Zero Delta", f"₹{vol_zd:,.2f}", v_dex_color, f"DD: {data['vol_dd_val']:+,.2f}", v_dex_color), unsafe_allow_html=True)
    with vm5:
        st.markdown(render_custom_metric("Vol GEX Z-Score", f"{z_gex:+.2f}", GREEN_COLOR if z_gex >= 0 else RED_COLOR, "20-Period Window"), unsafe_allow_html=True)
    with vm6:
        st.markdown(render_custom_metric("Vol DEX Z-Score", f"{z_dex:+.2f}", GREEN_COLOR if z_dex >= 0 else RED_COLOR, "20-Period Window"), unsafe_allow_html=True)

    st.markdown("---")

    st.markdown("### 🧱 Exposure Walls (Key Support & Resistance)")
    w1, w2, w3, w4 = st.columns(4)
    w1.metric("Call GEX Wall", f"₹{data['call_gex_wall']:,.0f}")
    w2.metric("Put GEX Wall", f"₹{data['put_gex_wall']:,.0f}")
    w3.metric("Call DEX Wall", f"₹{data['call_dex_wall']:,.0f}")
    w4.metric("Put DEX Wall", f"₹{data['put_dex_wall']:,.0f}")

    st.markdown("---")

    df_gex_strikes = pd.DataFrame(data["gex_strike_exposure_list"])
    df_dex_strikes = pd.DataFrame(data["dex_strike_exposure_list"])
    df_vex_chex_strikes = pd.DataFrame(data["vex_chex_strike_exposure_list"])

    col_chart_gex, col_chart_dex = st.columns(2)

    with col_chart_gex:
        st.subheader("🎯 Strike-wise GEX Profile")
        fig_gex = go.Figure()
        fig_gex.add_trace(go.Bar(x=df_gex_strikes['strike'], y=df_gex_strikes['call_gex_cr'], name='Call GEX (+)', marker_color='#00F5D4', width=35))
        fig_gex.add_trace(go.Bar(x=df_gex_strikes['strike'], y=df_gex_strikes['put_gex_cr'], name='Put GEX (-)', marker_color='#FF0055', width=35))
        fig_gex.add_vline(x=spot, line_dash="dash", line_color="#E0E0E0", annotation_text=f"Spot: ₹{spot:,.0f}")
        fig_gex.add_vline(x=data['zero_gamma_val'], line_dash="dot", line_color="#FFB703", annotation_text="Zero Gamma")
        fig_gex.update_layout(barmode='relative', bargap=0.05, template='plotly_dark', height=420, xaxis_title="Strike Price", yaxis_title="GEX Level")
        st.plotly_chart(fig_gex, width='stretch')

    with col_chart_dex:
        st.subheader("🎯 Strike-wise DEX Profile")
        fig_dex = go.Figure()
        fig_dex.add_trace(go.Bar(x=df_dex_strikes['strike'], y=df_dex_strikes['call_dex_cr'], name='Call DEX (+)', marker_color='#00B4D8', width=35))
        fig_dex.add_trace(go.Bar(x=df_dex_strikes['strike'], y=df_dex_strikes['put_dex_cr'], name='Put DEX (-)', marker_color='#FF70A6', width=35))
        fig_dex.add_vline(x=spot, line_dash="dash", line_color="#E0E0E0", annotation_text=f"Spot: ₹{spot:,.0f}")
        fig_dex.add_vline(x=data['zero_delta_val'], line_dash="dot", line_color="#70E000", annotation_text="Zero Delta")
        fig_dex.update_layout(barmode='relative', bargap=0.05, template='plotly_dark', height=420, xaxis_title="Strike Price", yaxis_title="DEX Level")
        st.plotly_chart(fig_dex, width='stretch')

    col_chart_vex, col_chart_chex = st.columns(2)

    with col_chart_vex:
        st.subheader("🌊 Strike-wise VEX Profile")
        fig_vex = go.Figure()
        fig_vex.add_trace(go.Bar(x=df_vex_chex_strikes['strike'], y=df_vex_chex_strikes['call_vex_cr'], name='Call VEX (+)', marker_color='#A06CD5', width=35))
        fig_vex.add_trace(go.Bar(x=df_vex_chex_strikes['strike'], y=df_vex_chex_strikes['put_vex_cr'], name='Put VEX (-)', marker_color='#FF9F1C', width=35))
        fig_vex.add_vline(x=spot, line_dash="dash", line_color="#E0E0E0", annotation_text=f"Spot: ₹{spot:,.0f}")
        fig_vex.update_layout(barmode='relative', bargap=0.05, template='plotly_dark', height=420, xaxis_title="Strike Price", yaxis_title="VEX Level")
        st.plotly_chart(fig_vex, width='stretch')

    with col_chart_chex:
        st.subheader("⏳ Strike-wise CHEX Profile")
        fig_chex = go.Figure()
        fig_chex.add_trace(go.Bar(x=df_vex_chex_strikes['strike'], y=df_vex_chex_strikes['call_chex_cr'], name='Call CHEX', marker_color='#3A86FF', width=35))
        fig_chex.add_trace(go.Bar(x=df_vex_chex_strikes['strike'], y=df_vex_chex_strikes['put_chex_cr'], name='Put CHEX', marker_color='#FF006E', width=35))
        fig_chex.add_vline(x=spot, line_dash="dash", line_color="#E0E0E0", annotation_text=f"Spot: ₹{spot:,.0f}")
        fig_chex.update_layout(barmode='relative', bargap=0.05, template='plotly_dark', height=420, xaxis_title="Strike Price", yaxis_title="CHEX Level")
        st.plotly_chart(fig_chex, width='stretch')

    st.subheader("📜 Live Intraday CSV Logs & VEX / CHEX Data")

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Spot vs Zero Exposure Levels Trend",
        "📋 Raw OI & Reversal Monitor CSV Table",
        "⚡ Volume-Based CSV Table",
        "🌊 Minute-wise VEX & CHEX Log Table",
        "📑 Strike-wise VEX & CHEX Raw Data Table"
    ])

    with tab1:
        df_csv = safe_read_csv(OI_CSV_FILE)
        if not df_csv.empty:
            fig_levels = go.Figure()
            fig_levels.add_trace(go.Scatter(x=df_csv['Timestamp'], y=pd.to_numeric(df_csv['Spot_Price'], errors='coerce'), mode='lines+markers', name='Spot Price', line=dict(color='#29B6F6', width=2)))
            fig_levels.add_trace(go.Scatter(x=df_csv['Timestamp'], y=pd.to_numeric(df_csv['Zero_Gamma_Level'], errors='coerce'), mode='lines+markers', name='Zero Gamma (OI)', line=dict(color='#FF3D00', width=2)))
            fig_levels.add_trace(go.Scatter(x=df_csv['Timestamp'], y=pd.to_numeric(df_csv['Zero_Delta_Level'], errors='coerce'), mode='lines+markers', name='Zero Delta (OI)', line=dict(color='#00E676', width=2)))

            df_v_csv = safe_read_csv(VOL_CSV_FILE)
            if not df_v_csv.empty:
                fig_levels.add_trace(go.Scatter(x=df_v_csv['Timestamp'], y=pd.to_numeric(df_v_csv['Vol_Zero_Gamma_Level'], errors='coerce'), mode='lines', name='Zero Gamma (Vol)', line=dict(color='#FFB703', width=1.5, dash='dot')))
                fig_levels.add_trace(go.Scatter(x=df_v_csv['Timestamp'], y=pd.to_numeric(df_v_csv['Vol_Zero_Delta_Level'], errors='coerce'), mode='lines', name='Zero Delta (Vol)', line=dict(color='#A06CD5', width=1.5, dash='dot')))

            fig_levels.update_layout(template='plotly_dark', height=420, xaxis_title="Timestamp", yaxis_title="Index Level (₹)")
            st.plotly_chart(fig_levels, width='stretch')

    with tab2:
        df_oi_show = safe_read_csv(OI_CSV_FILE)
        if not df_oi_show.empty:
            st.dataframe(df_oi_show, width='stretch')

    with tab3:
        df_vol_show = safe_read_csv(VOL_CSV_FILE)
        if not df_vol_show.empty:
            st.dataframe(df_vol_show, width='stretch')

    with tab4:
        df_vc_show = safe_read_csv(VEX_CHEX_CSV_FILE)
        if not df_vc_show.empty:
            st.dataframe(df_vc_show, width='stretch')

    with tab5:
        st.subheader(f"🌊 Strike-wise VEX, CHEX & Implied Volatility Z-Score (ATM ±{VEX_CHEX_STRIKES} Window)")
        st.dataframe(pd.DataFrame(data["vex_chex_table"]), width='stretch')

        st.markdown("#### 📊 VEX Summary for Selected Strikes")
        sum_c1, sum_c2, sum_c3 = st.columns(3)

        call_v_color = GREEN_COLOR if data['total_call_vex_cr'] >= 0 else RED_COLOR
        put_v_color = GREEN_COLOR if data['total_put_vex_cr'] >= 0 else RED_COLOR
        net_v_color = GREEN_COLOR if data['net_vex_cr'] >= 0 else RED_COLOR

        with sum_c1:
            st.markdown(render_custom_metric("Total Call VEX", f"₹{data['total_call_vex_cr']:,.4f} Cr", call_v_color), unsafe_allow_html=True)
        with sum_c2:
            st.markdown(render_custom_metric("Total Put VEX", f"₹{data['total_put_vex_cr']:,.4f} Cr", put_v_color), unsafe_allow_html=True)
        with sum_c3:
            st.markdown(render_custom_metric("Total Net VEX", f"₹{data['net_vex_cr']:,.4f} Cr", net_v_color), unsafe_allow_html=True)

time.sleep(REFRESH_RATE)
st.rerun()