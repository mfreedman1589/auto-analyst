import streamlit as st
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
import concurrent.futures
import plotly.express as px
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import datetime
import pytz
from fpdf import FPDF
import io
import os
from streamlit_gsheets import GSheetsConnection

# --- CONFIGURATION & CUSTOM CSS ---
st.set_page_config(page_title="Auto-Sales Intelligence Agent", layout="wide")

st.markdown("""
    <style>
        [data-testid="stSidebarHeader"] { position: sticky !important; top: 0 !important; z-index: 999 !important; background-color: var(--secondary-background-color) !important; }
        [data-testid="stSidebarResizer"] { height: 100vh !important; }
        button[kind="primary"] { background-color: #D70015 !important; color: white !important; border: 1px solid #A30010 !important; font-weight: bold !important; border-radius: 6px !important; }
        button[kind="primary"]:hover { background-color: #FF3B30 !important; border: 1px solid #D70015 !important; color: white !important; }
    </style>
""", unsafe_allow_html=True)

# --- SESSION STATE INITIALIZATION ---
if 'history' not in st.session_state:
    st.session_state.history = {} 
if 'current_report_id' not in st.session_state:
    st.session_state.current_report_id = None 
if 'local_vault_updates' not in st.session_state:
    st.session_state.local_vault_updates = {}
if 'min_visitors' not in st.session_state:
    st.session_state.min_visitors = 1
if 'global_usage_count' not in st.session_state:
    st.session_state.global_usage_count = "..."

# --- THE DEALER API VAULT (Google Sheets Powered) ---
FALLBACK_VAULT = {
    'hananiavw.com': {'app_id': 'YL5AFXM3DW', 'api_key': '59d32b7b5842f84284e044c7ca465498', 'index': 'volkswagenoforangepark-sbm0424_production_inventory'},
    'hondasanmarcos.com': {'app_id': 'V3ZOVI2QFZ', 'api_key': 'ec7553dd56e6d4c8bb447a0240e7aab3', 'index': 'hondaofsanmarcos_production_inventory'},
    'basilcars.com': {'app_id': 'V3ZOVI2QFZ', 'api_key': 'ec7553dd56e6d4c8bb447a0240e7aab3', 'index': 'basilautogroup_production_inventory'},
    'joebasilchevrolet.com': {'app_id': 'V3ZOVI2QFZ', 'api_key': 'ec7553dd56e6d4c8bb447a0240e7aab3', 'index': 'joebasilchevy_production_inventory'},
    'robertbasilcars.com': {'app_id': 'V3ZOVI2QFZ', 'api_key': 'ec7553dd56e6d4c8bb447a0240e7aab3', 'index': 'robertbasilbuickgmc_production_inventory'},
    'basilresale.com': {'app_id': 'EHWUW84XVK', 'api_key': 'fb58227032e79f03b9b820cbaea7f8fb', 'index': 'basilresalesheridan_production_inventory'},
    'basilmitsubishi.com': {'app_id': 'EQU6HXB6WG', 'api_key': 'da97ef494552f47ecc6f4706888d120', 'index': 'basilmitsubishi-winback0323_production_inventory'},
    'basilfredonia.com': {'app_id': 'V3ZOVI2QFZ', 'api_key': 'ec7553dd56e6d4c8bb447a0240e7aab3', 'index': 'basilchevybuick_production_inventory'}
}

def load_vault():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        vault_df = conn.read(worksheet="Sheet1", ttl=60) 
        vault_dict = {}
        for _, row in vault_df.iterrows():
            domain = str(row.get('Base Domain', '')).strip()
            if domain and domain != 'nan':
                vault_dict[domain] = {
                    'app_id': str(row.get('App ID', '')).strip(),
                    'api_key': str(row.get('API Key', '')).strip(),
                    'index': str(row.get('Index Name', '')).strip()
                }
        
        try:
            usage_df = conn.read(worksheet="UsageStats", ttl=60)
            st.session_state.global_usage_count = len(usage_df)
        except Exception:
            pass
            
        return vault_dict, vault_df, conn
    except Exception as e:
        return FALLBACK_VAULT, None, None

DEALER_API_VAULT, vault_df, gsheets_conn = load_vault()
DEALER_API_VAULT.update(st.session_state.local_vault_updates)

# --- LOGIN ---
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if st.session_state.password_correct:
        return True
    st.title("🔒 Auto-Analyst Login")
    password = st.text_input("Enter Company Password", type="password")
    if st.button("Log In"):
        if password == "tegna2026": 
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("Incorrect password")
    return False

if not check_password():
    st.stop()

# --- PDF GENERATOR FUNCTION ---
def create_pdf_report(df, sold_df, metrics, missed_df, include_missed, dealer_group=None, include_dealer_details=False):
    pdf = FPDF()
    pdf.add_page()
    
    eastern = pytz.timezone('US/Eastern')
    current_time = datetime.datetime.now(eastern)
    
    domain_count = df['Dealer'].nunique()
    report_title = "Auto-Sales Intelligence Report"
    if domain_count == 1:
        report_title = f"{df['Dealer'].iloc[0]} Intelligence Report"

    pdf.set_font("Arial", "B", 18)
    pdf.cell(0, 15, report_title, ln=True, align="C")
    
    pdf.set_font("Arial", "I", 10)
    pdf.cell(0, 8, f"Generated on: {current_time.strftime('%Y-%m-%d %I:%M %p ET')} | Attribution Threshold: {metrics.get('min_visitors', 1)}+ VDP Visitors", ln=True, align="C")
    pdf.ln(5)

    pdf.set_font("Arial", "B", 14)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(0, 10, " 1. Executive Summary", ln=True, fill=True)
    pdf.set_font("Arial", "", 12)
    pdf.ln(5)
    
    col_width = pdf.w / 2.2
    
    pdf.cell(col_width, 8, f"Total Units Sold: {metrics['units_sold']}", border=0)
    pdf.cell(col_width, 8, f"Est. Revenue Sold: ${metrics['rev_sold']:,.0f}", border=0, ln=True)
    
    pdf.cell(col_width, 8, f"Pipeline Value: ${metrics['pipeline']:,.0f}", border=0)
    pdf.cell(col_width, 8, f"Look-to-Book Ratio: {metrics['ltb']}%", border=0, ln=True)
    
    pdf.set_font("Arial", "I", 10)
    pdf.cell(col_width, 6, "", border=0) 
    pdf.cell(col_width, 6, f"(New: {metrics['new_ltb']}% | Used: {metrics['used_ltb']}%)", border=0, ln=True)
    pdf.ln(8)

    section_offset = 0
    if dealer_group is not None and not dealer_group.empty:
        pdf.set_font("Arial", "B", 14)
        pdf.cell(0, 10, " 2. Auto Group / Tier 2 Breakdown", ln=True, fill=True)
        pdf.ln(5)
        
        pdf.set_font("Arial", "B", 9)
        pdf.cell(50, 8, "Dealer Name", border=1)
        pdf.cell(18, 8, "Traffic", border=1)
        pdf.cell(15, 8, "VDPs", border=1)
        pdf.cell(15, 8, "Sold", border=1)
        pdf.cell(15, 8, "LTB", border=1)
        pdf.cell(35, 8, "Est. Rev Sold", border=1)
        pdf.cell(35, 8, "Pipeline Value", border=1)
        pdf.ln()
        
        pdf.set_font("Arial", "", 8)
        for _, row in dealer_group.head(15).iterrows():
            pdf.cell(50, 8, str(row['Dealer'])[:28], border=1)
            pdf.cell(18, 8, str(row['Total Visitors']), border=1)
            pdf.cell(15, 8, str(row['VDPs Shopped']), border=1)
            pdf.cell(15, 8, str(row['Units Sold']), border=1)
            pdf.cell(15, 8, f"{row['Look-to-Book (%)']}%", border=1)
            pdf.cell(35, 8, f"${row['Est. Rev Sold']:,.0f}", border=1)
            pdf.cell(35, 8, f"${row['Pipeline Value']:,.0f}", border=1)
            pdf.ln()
        pdf.ln(8)
        section_offset = 1

    sec_market = 2 + section_offset
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, f" {sec_market}. Market Insights", ln=True, fill=True)
    pdf.ln(5)
    
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 8, "Traffic Mix (Unique Visits by Page Type)", ln=True)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(100, 8, "Page Category", border=1)
    pdf.cell(40, 8, "Unique Visits", border=1)
    pdf.ln()
    pdf.set_font("Arial", "", 9)
    traffic_mix = df.groupby('Category')['Attributed Unique Visitors'].sum().reset_index().sort_values('Attributed Unique Visitors', ascending=False)
    for _, row in traffic_mix.iterrows():
        pdf.cell(100, 8, str(row['Category']), border=1)
        pdf.cell(40, 8, str(row['Attributed Unique Visitors']), border=1)
        pdf.ln()
    pdf.ln(5)

    if not sold_df.empty:
        pdf.set_font("Arial", "B", 11)
        pdf.cell(0, 8, "Sales Mix (New vs Used)", ln=True)
        pdf.set_font("Arial", "B", 9)
        pdf.cell(70, 8, "Type", border=1)
        pdf.cell(35, 8, "Sold Count", border=1)
        pdf.cell(35, 8, "Share %", border=1)
        pdf.ln()
        pdf.set_font("Arial", "", 9)
        sales_mix = sold_df['Type'].value_counts(normalize=True).mul(100).round(1).reset_index()
        sales_mix.columns = ['Type', 'Share']
        sales_counts = sold_df['Type'].value_counts().reset_index()
        sales_counts.columns = ['Type', 'Count']
        merged_sales = pd.merge(sales_mix, sales_counts, on='Type')
        for _, row in merged_sales.iterrows():
            pdf.cell(70, 8, str(row['Type']), border=1)
            pdf.cell(35, 8, str(row['Count']), border=1)
            pdf.cell(35, 8, f"{row['Share']}%", border=1)
            pdf.ln()
        pdf.ln(5)

    if not sold_df.empty:
        pdf.set_font("Arial", "B", 11)
        pdf.cell(0, 8, "Price Tiers (Sold Units)", ln=True)
        pdf.set_font("Arial", "B", 9)
        pdf.cell(70, 8, "Price Tier", border=1)
        pdf.cell(35, 8, "Sold Count", border=1)
        pdf.cell(35, 8, "Share %", border=1)
        pdf.ln()
        pdf.set_font("Arial", "", 9)
        tier_mix = sold_df['Price Tier'].value_counts(normalize=True).mul(100).round(1).reset_index()
        tier_mix.columns = ['Price Tier', 'Share']
        tier_counts = sold_df['Price Tier'].value_counts().reset_index()
        tier_counts.columns = ['Price Tier', 'Count']
        merged_tiers = pd.merge(tier_mix, tier_counts, on='Price Tier')
        for _, row in merged_tiers.iterrows():
            pdf.cell(70, 8, str(row['Price Tier']), border=1)
            pdf.cell(35, 8, str(row['Count']), border=1)
            pdf.cell(35, 8, f"{row['Share']}%", border=1)
            pdf.ln()
    pdf.ln(10)

    if not sold_df.empty:
        sold_models = sold_df.copy()
        sold_models['Model_Only'] = sold_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
        model_counts = sold_models['Model_Only'].value_counts().reset_index()
        model_counts.columns = ['Make/Model', 'Units Sold']
        top_models = model_counts[model_counts['Units Sold'] > 1].head(10)
        
        if not top_models.empty:
            sec_models = 3 + section_offset
            pdf.set_font("Arial", "B", 14)
            pdf.cell(0, 10, f" {sec_models}. Top Sold Models (Aggregated > 1 Unit)", ln=True, fill=True)
            pdf.ln(5)
            pdf.set_font("Arial", "B", 10)
            pdf.cell(100, 8, "Make/Model", border=1)
            pdf.cell(40, 8, "Units Sold", border=1)
            pdf.ln()
            pdf.set_font("Arial", "", 9)
            for _, row in top_models.iterrows():
                pdf.cell(100, 8, str(row['Make/Model']), border=1)
                pdf.cell(40, 8, str(row['Units Sold']), border=1)
                pdf.ln()
            pdf.ln(5)

    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 10, "Top Sold Units (Detail)", ln=True)
    pdf.set_font("Arial", "B", 10)
    pdf.cell(80, 8, "Vehicle Name", border=1)
    pdf.cell(25, 8, "Type", border=1)
    pdf.cell(20, 8, "Visitors", border=1)
    pdf.cell(65, 8, "VIN", border=1)
    pdf.ln()

    if not sold_df.empty:
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        for _, row in top_sold.iterrows():
            name = str(row['Vehicle Name'])[:35]
            pdf.set_font("Arial", "", 9) 
            pdf.cell(80, 8, name, border=1)
            pdf.cell(25, 8, str(row['Type']), border=1)
            pdf.cell(20, 8, str(row['Attributed Unique Visitors']), border=1)
            pdf.set_font("Arial", "", 8)
            pdf.cell(65, 8, str(row['VIN']), border=1)
            pdf.ln()
    else:
        pdf.set_font("Arial", "", 9)
        pdf.cell(0, 8, "No sales identified.", border=1, ln=True)

    if include_missed:
        pdf.add_page()
        sec_missed = 4 + section_offset
        pdf.set_font("Arial", "B", 14)
        pdf.cell(0, 10, f" {sec_missed}. Missed Opportunities (The Watch List)", ln=True, fill=True)
        pdf.ln(5)
        
        if not missed_df.empty:
            missed_models = missed_df.copy()
            missed_models['Model_Only'] = missed_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
            missed_counts = missed_models['Model_Only'].value_counts().reset_index()
            missed_counts.columns = ['Make/Model', 'Count']
            top_missed = missed_counts[missed_counts['Count'] > 1].head(10)
            
            if not top_missed.empty:
                pdf.set_font("Arial", "B", 11)
                pdf.cell(0, 10, "Top Missed Models (Aggregated > 1 Unit)", ln=True)
                pdf.set_font("Arial", "B", 10)
                pdf.cell(100, 8, "Make/Model", border=1)
                pdf.cell(40, 8, "Missed Count", border=1)
                pdf.ln()
                pdf.set_font("Arial", "", 9)
                for _, row in top_missed.iterrows():
                    pdf.cell(100, 8, str(row['Make/Model']), border=1)
                    pdf.cell(40, 8, str(row['Count']), border=1)
                    pdf.ln()
                pdf.ln(5)
        
        pdf.set_font("Arial", "B", 11)
        pdf.cell(0, 10, "Missed Opportunities (Detail)", ln=True)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(85, 8, "Vehicle Name (Clickable)", border=1)
        pdf.cell(65, 8, "VIN", border=1)
        pdf.cell(20, 8, "Type", border=1)
        pdf.cell(20, 8, "Visitors", border=1)
        pdf.ln()
        pdf.set_font("Arial", "", 9)
        if not missed_df.empty:
             top_missed_detail = missed_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
             for _, row in top_missed_detail.iterrows():
                 name = str(row['Vehicle Name'])[:35]
                 url = str(row['Page Url'])
                 pdf.set_text_color(0, 0, 255) 
                 pdf.cell(85, 8, name, border=1, link=url)
                 pdf.set_text_color(0, 0, 0)
                 pdf.set_font("Arial", "", 8) 
                 pdf.cell(65, 8, str(row['VIN']), border=1)
                 pdf.set_font("Arial", "", 9)
                 pdf.cell(20, 8, str(row['Type']), border=1)
                 pdf.cell(20, 8, str(row['Attributed Unique Visitors']), border=1)
                 pdf.ln()
                 
    # --- DEALER DEEP DIVES EXTENSION IN PDF ---
    if include_dealer_details and dealer_group is not None and not dealer_group.empty:
        for dealer in dealer_group['Dealer'].tolist():
            dealer_sold = sold_df[sold_df['Dealer'] == dealer]
            dealer_missed = missed_df[missed_df['Dealer'] == dealer]
            
            if dealer_sold.empty and dealer_missed.empty:
                continue 
                
            pdf.add_page()
            pdf.set_font("Arial", "B", 14)
            pdf.set_fill_color(220, 220, 220)
            pdf.cell(0, 10, f" Dealership Profile: {dealer}", ln=True, fill=True)
            pdf.ln(5)
            
            if not dealer_sold.empty:
                d_sold_models = dealer_sold.copy()
                d_sold_models['Model_Only'] = d_sold_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
                d_model_counts = d_sold_models['Model_Only'].value_counts().reset_index()
                d_model_counts.columns = ['Make/Model', 'Units Sold']
                d_top_models = d_model_counts[d_model_counts['Units Sold'] > 1].head(5)
                
                if not d_top_models.empty:
                    pdf.set_font("Arial", "B", 11)
                    pdf.cell(0, 10, "Top Sold Models", ln=True)
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(100, 8, "Make/Model", border=1)
                    pdf.cell(40, 8, "Units Sold", border=1)
                    pdf.ln()
                    pdf.set_font("Arial", "", 9)
                    for _, row in d_top_models.iterrows():
                        pdf.cell(100, 8, str(row['Make/Model']), border=1)
                        pdf.cell(40, 8, str(row['Units Sold']), border=1)
                        pdf.ln()
                    pdf.ln(5)
                    
            if not dealer_missed.empty:
                d_missed_models = dealer_missed.copy()
                d_missed_models['Model_Only'] = d_missed_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
                d_missed_counts = d_missed_models['Model_Only'].value_counts().reset_index()
                d_missed_counts.columns = ['Make/Model', 'Count']
                d_top_missed = d_missed_counts[d_missed_counts['Count'] > 1].head(5)
                
                if not d_top_missed.empty:
                    pdf.set_font("Arial", "B", 11)
                    pdf.cell(0, 10, "Top Missed Models", ln=True)
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(100, 8, "Make/Model", border=1)
                    pdf.cell(40, 8, "Missed Count", border=1)
                    pdf.ln()
                    pdf.set_font("Arial", "", 9)
                    for _, row in d_top_missed.iterrows():
                        pdf.cell(100, 8, str(row['Make/Model']), border=1)
                        pdf.cell(40, 8, str(row['Count']), border=1)
                        pdf.ln()
                    pdf.ln(5)

            if not dealer_sold.empty:
                pdf.set_font("Arial", "B", 11)
                pdf.cell(0, 10, "Top Sold Units (Detail)", ln=True)
                pdf.set_font("Arial", "B", 10)
                pdf.cell(80, 8, "Vehicle Name", border=1)
                pdf.cell(25, 8, "Type", border=1)
                pdf.cell(20, 8, "Visitors", border=1)
                pdf.cell(65, 8, "VIN", border=1)
                pdf.ln()
                d_top_sold = dealer_sold.sort_values('Attributed Unique Visitors', ascending=False).head(10)
                for _, row in d_top_sold.iterrows():
                    name = str(row['Vehicle Name'])[:35]
                    pdf.set_font("Arial", "", 9) 
                    pdf.cell(80, 8, name, border=1)
                    pdf.cell(25, 8, str(row['Type']), border=1)
                    pdf.cell(20, 8, str(row['Attributed Unique Visitors']), border=1)
                    pdf.set_font("Arial", "", 8)
                    pdf.cell(65, 8, str(row['VIN']), border=1)
                    pdf.ln()
                pdf.ln(5)

            if not dealer_missed.empty:
                pdf.set_font("Arial", "B", 11)
                pdf.cell(0, 10, "Missed Opportunities (Detail)", ln=True)
                pdf.set_font("Arial", "B", 10)
                pdf.cell(85, 8, "Vehicle Name (Clickable)", border=1)
                pdf.cell(65, 8, "VIN", border=1)
                pdf.cell(20, 8, "Type", border=1)
                pdf.cell(20, 8, "Visitors", border=1)
                pdf.ln()
                d_top_missed_detail = dealer_missed.sort_values('Attributed Unique Visitors', ascending=False).head(10)
                pdf.set_font("Arial", "", 9)
                for _, row in d_top_missed_detail.iterrows():
                     name = str(row['Vehicle Name'])[:35]
                     url = str(row['Page Url'])
                     pdf.set_text_color(0, 0, 255) 
                     pdf.cell(85, 8, name, border=1, link=url)
                     pdf.set_text_color(0, 0, 0)
                     pdf.set_font("Arial", "", 8) 
                     pdf.cell(65, 8, str(row['VIN']), border=1)
                     pdf.set_font("Arial", "", 9)
                     pdf.cell(20, 8, str(row['Type']), border=1)
                     pdf.cell(20, 8, str(row['Attributed Unique Visitors']), border=1)
                     pdf.ln()
                 
    return bytes(pdf.output())

# --- THE VALUATION ENGINE ---
def estimate_value(row):
    name = str(row['Vehicle Name']).lower()
    vehicle_type = str(row['Type']).lower()
    MODEL_PRICES = {
        'f-150': 60000, 'f 150': 60000, 'f150': 60000, 'super duty': 80000,
        'ranger': 40000, 'maverick': 28000, 'bronco': 50000, 'explorer': 48000,
        'expedition': 70000, 'edge': 40000, 'escape': 32000, 'mustang': 45000,
        'mach-e': 55000, 'silverado': 55000, 'sierra': 62000, 'colorado': 38000,
        'tahoe': 72000, 'suburban': 78000, 'yukon': 78000, 'escalade': 110000,
        'corvette': 90000, 'camaro': 42000, 'blazer': 40000, 'equinox': 32000,
        'traverse': 42000, 'malibu': 28000, 'grand cherokee': 55000, 'wagoneer': 80000,
        'wrangler': 48000, 'gladiator': 50000, 'ram 1500': 60000, 'durango': 50000,
        'charger': 40000, 'challenger': 45000, 'pacifica': 45000, 'compass': 30000,
        'tundra': 58000, 'tacoma': 42000, '4runner': 50000, 'sequoia': 75000,
        'highlander': 45000, 'rav4': 35000, 'camry': 30000, 'corolla': 25000,
        'prius': 32000, 'sienna': 48000, 'pilot': 48000, 'passport': 42000,
        'cr-v': 36000, 'odyssey': 45000, 'civic': 28000, 'accord': 32000,
        '3 series': 50000, '5 series': 65000, 'x3': 55000, 'x5': 75000, 'x7': 90000,
        'c-class': 52000, 'e-class': 70000, 'gle': 75000, 'gls': 95000,
        'rx': 58000, 'nx': 48000, 'es': 48000, 'gx': 70000, 'lx': 100000,
        'gv80': 70000, 'gv70': 55000, 'telluride': 48000, 'palisade': 50000,
        'lyriq': 65000, 'optiq': 54000, 'ct4': 40000, 'ct5': 50000, 'xt4': 40000, 'xt5': 50000
    }
    BRAND_DEFAULTS = {
        'cadillac': 65000, 'mercedes': 70000, 'bmw': 65000, 'audi': 60000,
        'lexus': 60000, 'lincoln': 65000, 'land rover': 85000, 'porsche': 100000,
        'ford': 45000, 'chevrolet': 40000, 'gmc': 55000, 'ram': 55000,
        'jeep': 45000, 'dodge': 40000, 'toyota': 38000, 'honda': 35000,
        'nissan': 32000, 'hyundai': 30000, 'kia': 30000, 'subaru': 32000,
        'volkswagen': 32000, 'mazda': 32000, 'volvo': 55000
    }
    baseline = 35000
    found_model = False
    sorted_models = sorted(MODEL_PRICES.keys(), key=len, reverse=True)
    for model in sorted_models:
        if re.search(r'\b' + re.escape(model) + r'\b', name):
            baseline = MODEL_PRICES[model]
            found_model = True
            break
    if not found_model:
        for brand, price in BRAND_DEFAULTS.items():
            if brand in name:
                baseline = price
                break
    if 'new' in vehicle_type:
        return int(baseline)
    
    eastern = pytz.timezone('US/Eastern')
    year_match = re.search(r'\d{4}', name)
    year = int(year_match.group(0)) if year_match else 2025
    current_year = datetime.datetime.now(eastern).year + 1
    age = current_year - year
    if age <= 0:
        value = baseline
    else:
        value = baseline * 0.85
        for _ in range(age - 1):
            value = value * 0.90
    return int(value)

def get_price_tier(price):
    if price < 30000: return "Budget (<$30k)"
    if price < 60000: return "Core ($30k-$60k)"
    return "Premium ($60k+)"

# --- THE SCANNING HELPERS ---
def get_year(url):
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def extract_vin(url):
    try:
        path = urlparse(str(url)).path.upper().strip('/')
        match = re.search(r'(?:^|[-/])([A-HJ-NPR-Z0-9]{17})(?:[-/\.]|$)', path)
        if match: return match.group(1)
        blocks = re.findall(r'[A-Z0-9]{10,}', path)
        if blocks: return blocks[-1]
        return "N/A"
    except:
        return "N/A"

def extract_type(url):
    u = str(url).lower()
    if 'used' in u and 'new' not in u: return 'Used'
    if 'new' in u and 'used' not in u: return 'New'
    if '/used' in u or '-used-' in u or '=used' in u or 'preowned' in u: return 'Used'
    if '/new' in u or '-new-' in u or '=new' in u: return 'New'
    return 'New' if re.search(r'202[5-7]', u) else 'Used'

def smart_dealer_name(url, is_multi=False):
    u = str(url).lower()
    if not u.startswith('http'):
        u = 'http://' + u
    netloc = urlparse(u).netloc
    netloc = re.sub(r'^(www\.|shop\.|inventory\.|cars\.)', '', netloc)
    s = netloc.split('.')[0]
    
    brands = ['honda', 'toyota', 'ford', 'chevrolet', 'chevy', 'nissan', 'jeep', 'chrysler', 'dodge', 'ram',
              'hyundai', 'kia', 'vw', 'volkswagen', 'bmw', 'mercedes', 'audi', 'lexus', 'acura',
              'infiniti', 'subaru', 'mazda', 'volvo', 'porsche', 'buick', 'gmc', 'cadillac', 'lincoln', 'mitsubishi']
    
    modifiers = ['of', 'auto', 'group', 'cars', 'motors', 'dealers', 'cares', 'center', 'city', 'town', 'resale', 'classic', 'one', 'mile', 'superstore']
    
    for mod in modifiers:
        s = s.replace(mod, f" {mod} ")
        
    brands.sort(key=len, reverse=True)
    has_brand = False
    
    for brand in brands:
        if brand in s:
            if brand == 'ram' and ('paramus' in s or 'framingham' in s):
                continue
            if brand == 'ford' and ('oxford' in s or 'crawford' in s or 'stratford' in s):
                continue
            s = s.replace(brand, f" {brand} ")
            has_brand = True
            
    s = re.sub(r'\s+', ' ', s).strip().title()
    s = s.replace(" Vw", " VW").replace(" Bmw", " BMW").replace(" Gmc", " GMC")
    
    if is_multi and not has_brand:
        if any(x in s for x in ["Cares", "Community", "Gives"]):
            s += " (Community)"
        elif any(x in s for x in ["Resale", "Classic", "Used", "Preowned"]):
            pass 
        else:
            s += " (Group/Central Site)"
            
    return s

def clean_name_universal(url):
    year = get_year(url)
    if not year: return "Unknown Vehicle"
    
    parsed = urlparse(str(url))
    path_str = parsed.path + (("?" + parsed.query) if parsed.query else "")
    
    brands = ['Jeep', 'Ford', 'Gmc', 'Toyota', 'Dodge', 'Ram', 'Chrysler', 'Chevrolet', 'Honda', 'Nissan', 'Hyundai', 'Kia', 'Bmw', 'Lexus', 'Volvo', 'Volkswagen', 'Subaru', 'Mazda', 'Mercedes', 'Audi', 'Cadillac', 'Buick', 'Acura', 'Infiniti', 'Lincoln', 'Land Rover', 'Jaguar', 'Porsche', 'Mini']
    make = ""
    for b in brands:
        if b.lower() in path_str.lower():
            make = b.title()
            break
            
    parts = path_str.split(str(year), 1)
    rest = parts[-1] if len(parts) > 1 else path_str
    
    rest = rest.replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '').replace('.html', '')
    tokens = rest.split()
    
    junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Heritage', 'Twin', 'Pine', 'Wholesale', 'New', 'Used', 'Preowned', 'Inventory', 'Parts', 'Service', 'Finance', 'Global', 'Incentives', 'Offers', 'Suv', 'Truck', 'Coupe', 'Sedan', 'Vehicle', 'Vehicles']
    clean_tokens = [t for t in tokens if not (len(t) > 10 and any(c.isdigit() for c in t)) and t.title() not in junk and t.title() != make]
    
    clean_tokens = [t for t in clean_tokens if t != str(year)]
    
    return f"{year} {make} {' '.join(clean_tokens)}".title().strip()

def categorize(u):
    u = str(u).lower()
    
    if any(x in u for x in ['thank', 'confirm', 'success']):
        return 'Online Conversions'
        
    if u.endswith('.com/') or u.endswith('.com'): return 'Homepage'
    if any(x in u for x in ['service', 'parts', 'collision', 'appointment', 'maintenance']): return 'Service'
    
    if any(x in u for x in ['incentive', 'offers', 'specials', 'promotions', 'rebate']): 
        return 'Incentives/Offers'
        
    if 'index.htm' in u or u.endswith('searchnew.aspx') or u.endswith('searchused.aspx') or u.endswith('searchall.aspx'):
        if 'new' in u: return 'New Car Search'
        if 'used' in u or 'preowned' in u: return 'Used Car Search'
        return 'General Search'
        
    if get_year(u): return 'VDP'
    
    if any(x in u for x in ['search', 'inventory', 'vehicles']):
        if 'new' in u: return 'New Car Search'
        if 'used' in u or 'preowned' in u: return 'Used Car Search'
        return 'General Search'
        
    return 'Other'

def scan_url(url, session):
    domain_to_check = urlparse(str(url)).netloc.replace('www.', '').lower()
    vault_config = DEALER_API_VAULT.get(domain_to_check)
    
    app_id, api_key, index_name = None, None, None
    
    if vault_config:
        app_id = vault_config['app_id']
        api_key = vault_config['api_key']
        index_name = vault_config['index']
            
    if app_id:
        api_endpoint = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{index_name}/query"
        api_headers = {
            "x-algolia-application-id": app_id,
            "x-algolia-api-key": api_key,
            "Content-Type": "application/json"
        }
        vin = extract_vin(url)
        if vin != "N/A":
            try:
                payload = {"params": f"query={vin}"}
                resp = session.post(api_endpoint, headers=api_headers, json=payload, timeout=5)
                if resp.status_code == 200:
                    hits = resp.json().get("nbHits", 0)
                    return "Available" if hits > 0 else "SOLD (Not in Dealer Database)"
                else:
                    return f"ERROR (Database Code: {resp.status_code})"
            except Exception as e:
                return "ERROR (Database Request Failed)"
        else:
            return "ERROR (No VIN in URL)"
            
    else:
        return check_universal_status(url, session)
        
def check_universal_status(url, session):
    year = get_year(url)
    vin = extract_vin(url)
    if not year: return "N/A"
    
    try:
        # EXACT ORIGINAL HEADERS - DO NOT CHANGE
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        response = session.get(url, headers=headers, timeout=5, allow_redirects=True)
        
        if response.status_code in [403, 406, 429]:
            return f"ERROR (Inventory Sync Required: {response.status_code})"
            
        if response.status_code in [404, 410]:
            return "SOLD (404 Error)"
            
        orig_base = url.lower().split('?')[0].rstrip('/').replace('https://', '').replace('http://', '').replace('www.', '')
        final_base = response.url.lower().split('?')[0].rstrip('/').replace('https://', '').replace('http://', '').replace('www.', '')
        
        if orig_base != final_base:
            if vin != "N/A" and vin.lower() not in final_base: return "SOLD (HTTP Redirect)"
            if year not in final_base: return "SOLD (HTTP Redirect)"

        text = response.text 
        text_lower = text.lower()
        soup = BeautifulSoup(text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        
        bot_titles = ['just a moment', 'attention required', 'verify you are human', 'access denied', 'pardon our interruption', 'security check']
        if any(b in page_title for b in bot_titles): return "ERROR (Inventory Sync Required)"
            
        meta_refresh = soup.find('meta', attrs={'http-equiv': re.compile(r'^refresh$', re.I)})
        if meta_refresh and meta_refresh.get('content'):
            content = meta_refresh['content']
            match = re.search(r'url=([^"\'>\s]+)', content, re.IGNORECASE)
            if match:
                meta_url = match.group(1).lower()
                if vin != "N/A" and vin.lower() not in meta_url:
                    return "SOLD (Meta Refresh Redirect)"
                    
        js_redirects = re.findall(r'window\.location\.(?:replace|href|assign)\s*=\s*["\']([^"\'>]+)["\']', text, re.IGNORECASE)
        for js_url in js_redirects:
            if vin != "N/A" and vin.lower() not in js_url.lower():
                return "SOLD (JS Redirect)"

        if 'not found' in page_title or '404' in page_title or 'error' in page_title: return "SOLD (Page Not Found)"
            
        search_indicators = ['search', 'results', 'all vehicles', 'inventory']
        if any(x in page_title for x in search_indicators) and year not in page_title: return "SOLD (Soft Redirect)"
            
        # --- 1. YOUR ORIGINAL TEXT OVERLAY SCANNER ---
        soft_sold_phrases = [
            "no longer available",
            "this vehicle is sold",
            "vehicle has been sold",
            "currently out of stock"
        ]
        if any(phrase in text_lower for phrase in soft_sold_phrases):
            return "SOLD (Out of Stock Overlay)"

        # --- 2. FAST REGEX SPA SCANNER (No CPU Bottleneck!) ---
        # This replaces the memory-heavy text replacement with a blazing fast C-level regex.
        # It catches the "hidden" JSON code immediately without delaying the network pool.
        if re.search(r'availability["\'\s:]+http://schema\.org/(?:outofstock|soldout)|(?:inventory|vehicle)status["\'\s:]+sold|isavailable["\'\s:]+false', text_lower):
            return "SOLD (Hidden SPA / JSON Flag)"
            
        return "Available"
        
    except requests.exceptions.Timeout: return "ERROR (Timeout)"
    except requests.exceptions.ConnectionError: return "ERROR (Connection Blocked)"
    except Exception as e: return "Available"
        
# --- UI DASHBOARD ---
st.title("🚗 Auto-Sales Intelligence Agent")

# Sidebar: File Upload
st.sidebar.markdown("### 📥 New Analysis")
uploaded_file = st.sidebar.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

run_analysis_clicked = False
if uploaded_file is not None:
    run_analysis_clicked = st.sidebar.button("🚀 Run Diagnostic Analysis", type="primary", use_container_width=True)
    st.sidebar.info("Upload a CSV and click Run. If we need help locating the dealer's inventory database, we'll give you simple steps to sync it dynamically!")

# Main Execution Logic
if run_analysis_clicked:
    df_raw = pd.read_csv(uploaded_file)
    uploaded_file.seek(0)
    
    url_col = 'Page Url' if 'Page Url' in df_raw.columns else 'Page URL' if 'Page URL' in df_raw.columns else df_raw.columns[0]
    df_raw.rename(columns={url_col: 'Page Url'}, inplace=True)
    
    df_raw = df_raw.dropna(subset=['Page Url'])
    df_raw = df_raw[df_raw['Page Url'].astype(str).str.strip() != '']
    
    df_raw['Category'] = df_raw['Page Url'].apply(categorize)
    
    unique_domains_list = df_raw['Page Url'].apply(lambda x: urlparse(str(x)).netloc.replace('www.', '').lower()).unique()
    unique_domains_list = [d for d in unique_domains_list if d]
    is_multi_dealer = len(unique_domains_list) > 1
    
    df_raw['Dealer'] = df_raw['Page Url'].apply(lambda x: smart_dealer_name(x, is_multi_dealer)) 
    vdp_urls = df_raw[df_raw['Category'] == 'VDP']['Page Url'].tolist()
    
    st.info(f"Scanning {len(vdp_urls)} Vehicles. Calculating Valuations...")
    progress_bar = st.progress(0)
    
    # --- WORKERS AND POOL RESTORED TO 60 (FULL SPEED) ---
    session = requests.Session()
    retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=60, pool_maxsize=60)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    
    vdp_results = {}
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=60) as executor:
        future_to_url = {executor.submit(scan_url, url, session): url for url in vdp_urls}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_url)):
            url = future_to_url[future]
            vdp_results[url] = future.result()
            progress_bar.progress((i + 1) / len(vdp_urls))
            
    df_raw['Sold_Status'] = df_raw['Page Url'].map(vdp_results).fillna('N/A')
    df = df_raw.copy()
    
    df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
    df['Vehicle Name'] = df['Page Url'].apply(clean_name_universal)
    df['VIN'] = df['Page Url'].apply(extract_vin)
    df['Type'] = df['Page Url'].apply(extract_type)
    
    vdp_mask = df['Category'] == 'VDP'
    df.loc[vdp_mask, 'Category'] = df.loc[vdp_mask, 'Type'] + ' VDP'
    
    df['Est. Value'] = df.apply(estimate_value, axis=1)
    df['Price Tier'] = df['Est. Value'].apply(get_price_tier)
    
    domain_count = df['Dealer'].nunique()
    eastern = pytz.timezone('US/Eastern')
    report_time = datetime.datetime.now(eastern).strftime('%I:%M %p ET')

    if domain_count > 1:
        report_id = f"Auto Group ({domain_count} Sites) - {report_time}"
    else:
        domain = df['Dealer'].iloc[0] if len(df) > 0 else "Unknown Dealer"
        report_id = f"{domain} ({report_time})"
    
    # --- USAGE TRACKER ---
    try:
        if gsheets_conn is not None:
            usage_df = gsheets_conn.read(worksheet="UsageStats", ttl=0)
            new_usage = pd.DataFrame([{"Timestamp": str(datetime.datetime.now(eastern)), "Report": report_id}])
            updated_usage = pd.concat([usage_df, new_usage], ignore_index=True)
            gsheets_conn.update(worksheet="UsageStats", data=updated_usage)
            st.session_state.global_usage_count = len(updated_usage)
    except Exception:
        pass 
    
    st.session_state.history[report_id] = df
    st.session_state.current_report_id = report_id
    
    st.rerun()

# --- SIDEBAR: SESSION HISTORY MANAGER ---
if st.session_state.history:
    st.sidebar.divider()
    st.sidebar.markdown("### 📂 Session History")
    
    report_names = list(st.session_state.history.keys())
    selected_report = st.sidebar.radio("Select a report to view:", options=report_names, index=report_names.index(st.session_state.current_report_id))
    if selected_report != st.session_state.current_report_id:
        st.session_state.current_report_id = selected_report
        st.rerun()
        
    st.sidebar.divider()
    if st.sidebar.button("🗑️ Clear History"):
        st.session_state.history = {}
        st.session_state.current_report_id = None
        st.rerun()
        
    st.sidebar.divider()
    st.sidebar.markdown(f"📈 **Total Global Scans:** `{st.session_state.global_usage_count}`")

# --- MAIN DASHBOARD DISPLAY ---
if st.session_state.current_report_id is not None:
    st.subheader(f"Viewing Report: {st.session_state.current_report_id}")
    
    df = st.session_state.history[st.session_state.current_report_id].copy()
    
    # 1. IN-APP ACTION CENTER FOR INVENTORY SYNC
    error_df = df[df['Sold_Status'].str.startswith('ERROR', na=False)].copy()
    if not error_df.empty:
        error_df['_base_domain'] = error_df['Page Url'].apply(lambda x: urlparse(str(x)).netloc.replace('www.', '').lower())
        troubleshoot_df = error_df.groupby(['Dealer', '_base_domain']).size().reset_index(name='Blocked Pages')
        severe_blocks = troubleshoot_df[troubleshoot_df['Blocked Pages'] >= 10]
        
        pending_blocks = severe_blocks[~severe_blocks['_base_domain'].isin(DEALER_API_VAULT.keys())]
        
        if len(severe_blocks) > 0:
            if len(pending_blocks) > 0:
                severe_count = len(pending_blocks)
                alert_text = f"⚙️ Action Required: Sync Inventory Database{'s' if severe_count > 1 else ''} ({severe_count} Remaining)"
                
                with st.expander(alert_text, expanded=True):
                    st.markdown("We need a little help syncing with the inventory databases for the following dealerships. Follow the simple steps below to map their API to your permanent Vault and reveal true sales data!")
                    
                    st.markdown("---")
                    for _, row in pending_blocks.iterrows():
                        d_name = row['Dealer']
                        d_domain = row['_base_domain']
                        d_blocked = row['Blocked Pages']
                        
                        c1, c2, c3 = st.columns([3, 4, 2])
                        with c1:
                            st.markdown(f"👉 **[{d_name}](https://www.{d_domain})** *({d_blocked} unseen pages)*")
                        with c2:
                            st.text_input(f"API URL for {d_domain}", key=f"inp_{d_domain}", label_visibility="collapsed", placeholder="Paste API URL here...")
                        with c3:
                            if st.button("💾 Save", key=f"btn_{d_domain}", use_container_width=True):
                                val = st.session_state[f"inp_{d_domain}"]
                                if val:
                                    app_id_match = re.search(r'x-algolia-application-id=([^&]+)', val)
                                    api_key_match = re.search(r'x-algolia-api-key=([^&]+)', val)
                                    index_match = re.search(r'/indexes/([^/]+)/query', val)
                                    
                                    if app_id_match and api_key_match and index_match:
                                        st.session_state.local_vault_updates[d_domain] = {
                                            'app_id': app_id_match.group(1),
                                            'api_key': api_key_match.group(1),
                                            'index': index_match.group(1)
                                        }
                                        try:
                                            new_row = pd.DataFrame([{'Base Domain': d_domain, 'App ID': app_id_match.group(1), 'API Key': api_key_match.group(1), 'Index Name': index_match.group(1)}])
                                            if vault_df is not None and gsheets_conn is not None:
                                                updated_df = pd.concat([vault_df, new_row], ignore_index=True)
                                                gsheets_conn.update(worksheet="Sheet1", data=updated_df)
                                                st.cache_data.clear()
                                        except Exception:
                                            pass
                                        st.rerun()
                                    else:
                                        st.error("Invalid API URL.")
                                else:
                                    st.warning("Paste a URL first!")

                    st.markdown("---")
                    st.markdown("**How to find the API URL:**")
                    st.markdown("1. Click the dealer link above to open their site in a new tab.\n2. Right-click anywhere on their page and click **Inspect**.\n3. Click the **Network** tab, select the **Fetch/XHR** filter, and refresh the page.\n4. Search for `inventory`. Click the result starting with **`query?`** (it will contain 'algolia' in the URL). Right-click it and select **Copy URL**.")
                    
                    pdf_path = "Dealer Inspire URL Steps.pdf"
                    if os.path.exists(pdf_path):
                        with open(pdf_path, "rb") as pdf_file:
                            pdf_bytes = pdf_file.read()
                        st.download_button(
                            label="📄 Download Visual Guide (PDF)",
                            data=pdf_bytes,
                            file_name="Dealer_Inspire_URL_Steps.pdf",
                            mime="application/pdf"
                        )
            else:
                st.success("✅ All inventory syncs resolved! Click **Run Diagnostic Analysis** in the sidebar to complete the rescan.")

    # 2. REAL-TIME ATTRIBUTION FILTER UI (Sleek Expander Drawer)
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("🎯 Interactive VDP Filter", expanded=False):
        st.markdown(
            "<div style='font-size: 14px; color: #555; margin-bottom: 15px;'>"
            "<i>💡 <b>Share of Demand:</b> A vehicle typically needs ~30 total VDP views to sell. "
            "Adjust the slider to set the minimum visitors required to claim marketing influence. "
            "Driving just 5–15 highly qualified visitors represents a dominant share of the demand needed to move a unit.</i></div>", 
            unsafe_allow_html=True
        )
        st.slider("Minimum Visitors to Claim a Sale", min_value=1, max_value=30, step=1, label_visibility="collapsed", key="min_visitors")
    
    min_visitors = st.session_state.min_visitors
    st.markdown("---")
            
    # 3. APPLY FILTER (The "Velocity" Metric Update)
    vdp_df = df[(df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= min_visitors)]
    sold_df = vdp_df[vdp_df['Is Sold']]
    
    new_vdp_all = vdp_df[vdp_df['Type'] == 'New']
    new_sold = sold_df[sold_df['Type'] == 'New']
    new_ltb = (len(new_sold) / len(new_vdp_all) * 100) if len(new_vdp_all) > 0 else 0
    
    used_vdp_all = vdp_df[vdp_df['Type'] == 'Used']
    used_sold = sold_df[sold_df['Type'] == 'Used']
    used_ltb = (len(used_sold) / len(used_vdp_all) * 100) if len(used_vdp_all) > 0 else 0
    
    m_units = len(sold_df)
    m_rev = sold_df['Est. Value'].sum()
    m_pipe = vdp_df['Est. Value'].sum()
    m_ltb = (len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0)
    
    if not sold_df.empty:
        avg_v = sold_df['Attributed Unique Visitors'].mean()
        missed_threshold = max(avg_v, min_visitors)
        missed_df = df[(df['Sold_Status'] == 'Available') & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= missed_threshold)]
    else:
        missed_df = df[(df['Sold_Status'] == 'Available') & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= min_visitors)]

    st.markdown("### 📊 Executive Summary")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Units Sold (Attributed)", m_units)
    m2.metric("Est. Revenue Sold", f"${m_rev:,.0f}")
    m3.metric("Total Pipeline Value", f"${m_pipe:,.0f}")
    m4.metric(label="Look-to-Book Ratio", value=f"{m_ltb:.1f}%", delta=f"New: {new_ltb:.1f}% | Used: {used_ltb:.1f}%", delta_color="off")

    st.divider()

    domain_count = df['Dealer'].nunique()
    dealer_group_export = None
    
    if domain_count > 1:
        st.markdown(f"### 🏢 Tier 2 / Auto Group Breakdown ({domain_count} Sites)")
        show_tier2 = st.toggle("Display Individual Dealership Insights", value=True)
        
        t2_visitors = df.groupby('Dealer')['Attributed Unique Visitors'].sum().reset_index(name='Total Visitors')
        t2_vdps = vdp_df.groupby('Dealer').agg(
            VDPs_Shopped=('Category', 'count'),
            Pipeline_Value=('Est. Value', 'sum')
        ).reset_index()
        
        t2_sold = sold_df.groupby('Dealer').agg(
            Units_Sold=('Is Sold', 'count'),
            Est_Rev_Sold=('Est. Value', 'sum')
        ).reset_index()
        
        dealer_group = t2_visitors.merge(t2_vdps, on='Dealer', how='left').merge(t2_sold, on='Dealer', how='left').fillna(0)
        dealer_group = dealer_group[dealer_group['VDPs_Shopped'] > 0]
        
        dealer_group['Look-to-Book (%)'] = dealer_group.apply(
            lambda row: round((row['Units_Sold'] / row['VDPs_Shopped'] * 100), 1) if row['VDPs_Shopped'] > 0 else 0.0,
            axis=1
        )
        
        dealer_group = dealer_group.rename(columns={
            'VDPs_Shopped': 'VDPs Shopped',
            'Units_Sold': 'Units Sold',
            'Est_Rev_Sold': 'Est. Rev Sold',
            'Pipeline_Value': 'Pipeline Value'
        })
        
        dealer_group_export = dealer_group.sort_values('Total Visitors', ascending=False)

        if show_tier2:
            cA, cB = st.columns(2)
            with cA:
                fig_traffic = px.bar(dealer_group_export.head(10), x='Dealer', y='Total Visitors', title='Top Dealers by Traffic')
                st.plotly_chart(fig_traffic, use_container_width=True)
            with cB:
                fig_sales = px.bar(dealer_group_export.sort_values('Units Sold', ascending=False).head(10), x='Dealer', y='Units Sold', title='Top Dealers by Attributed Sales')
                st.plotly_chart(fig_sales, use_container_width=True)
                
            display_group = dealer_group_export.copy()
            display_group['Est. Rev Sold'] = display_group['Est. Rev Sold'].apply(lambda x: f"${x:,.0f}")
            display_group['Pipeline Value'] = display_group['Pipeline Value'].apply(lambda x: f"${x:,.0f}")
            
            st.dataframe(display_group, column_config={
                "Look-to-Book (%)": st.column_config.NumberColumn(format="%.1f%%")
            }, use_container_width=True, hide_index=True)
            
        st.divider()
    
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Traffic Mix**")
        traffic_data = df.groupby('Category')['Attributed Unique Visitors'].sum().reset_index().sort_values('Attributed Unique Visitors', ascending=False)
        traffic_data = traffic_data.rename(columns={'Attributed Unique Visitors': 'Unique Visits'})
        fig1 = px.bar(traffic_data, x='Category', y='Unique Visits', labels={'Unique Visits': 'Unique Visits'})
        st.plotly_chart(fig1, use_container_width=True)
    with c2:
        st.markdown("**Sales Mix (New vs Used)**")
        if not sold_df.empty:
            type_counts = sold_df['Type'].value_counts().reset_index()
            type_counts.columns = ['Type', 'Count']
            fig2 = px.pie(type_counts, values='Count', names='Type', color='Type', color_discrete_map={'New':'#4F81BD', 'Used':'#C0504D'}, hover_data=['Count'])
            fig2.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig2, use_container_width=True)
    with c3:
        st.markdown("**Sold Value Tiers**")
        if not sold_df.empty:
            tier_counts = sold_df['Price Tier'].value_counts().reset_index()
            tier_counts.columns = ['Price Tier', 'Count']
            fig3 = px.pie(tier_counts, values='Count', names='Price Tier', color_discrete_sequence=px.colors.qualitative.Set2, hover_data=['Count'])
            fig3.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig3, use_container_width=True)

    st.divider()

    t1, t2 = st.columns(2)
    with t1:
        if not sold_df.empty:
            sold_models = sold_df.copy()
            sold_models['Model_Only'] = sold_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
            model_counts = sold_models['Model_Only'].value_counts().reset_index()
            model_counts.columns = ['Make/Model', 'Units Sold']
            top_models = model_counts[model_counts['Units Sold'] > 1].head(10)
            if not top_models.empty:
                st.subheader("🏆 Top Sold Models (Aggregated > 1 Unit)")
                st.dataframe(top_models, use_container_width=True, hide_index=True)
        else:
            st.info("No sales identified.")

    with t2:
        if not missed_df.empty:
            missed_models = missed_df.copy()
            missed_models['Model_Only'] = missed_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
            missed_counts = missed_models['Model_Only'].value_counts().reset_index()
            missed_counts.columns = ['Make/Model', 'Missed Count']
            top_missed = missed_counts[missed_counts['Missed Count'] > 1].head(10)
            if not top_missed.empty:
                st.subheader("⚠️ Top Missed Models (Aggregated > 1 Unit)")
                st.dataframe(top_missed, use_container_width=True, hide_index=True)
        else:
            st.info("No missed opportunities identified.")

    st.divider()

    if not sold_df.empty:
        st.subheader("📄 Top Sold Units (Detail)")
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        display_sold = top_sold[['Dealer', 'Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
        display_sold.index += 1
        st.dataframe(display_sold, column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open"), "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")}, use_container_width=True)

    if not missed_df.empty:
        st.write("") 
        st.subheader("👀 Missed Opportunities (Detail)")
        display_missed = missed_df[['Dealer', 'Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].sort_values('Attributed Unique Visitors', ascending=False).head(10).reset_index(drop=True)
        display_missed.index += 1
        st.dataframe(display_missed, column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open"), "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")}, use_container_width=True)

    # --- DEALER DEEP DIVES UI ---
    if domain_count > 1 and dealer_group_export is not None:
        st.divider()
        st.markdown("### 🔍 Individual Dealership Deep Dives")
        show_deep_dives = st.toggle("Display Dealership Deep Dives", value=False)
        
        if show_deep_dives:
            expand_all = st.toggle("Expand All Profiles", value=False)
            st.write("")
            
            for dealer_name in dealer_group_export['Dealer']:
                with st.expander(f"🏢 {dealer_name} - Performance Details", expanded=expand_all):
                    d_sold = sold_df[sold_df['Dealer'] == dealer_name]
                    missed_threshold_local = max(avg_v, min_visitors) if not sold_df.empty else min_visitors
                    d_missed = df[(df['Dealer'] == dealer_name) & (df['Sold_Status'] == 'Available') & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= missed_threshold_local)]
                    
                    if d_sold.empty and d_missed.empty:
                        st.info("No actionable sales or high-interest missed opportunities meet the current thresholds for this dealer.")
                        continue
                    
                    c_left, c_right = st.columns(2)
                    with c_left:
                        if not d_sold.empty:
                            d_sold_models = d_sold.copy()
                            d_sold_models['Model_Only'] = d_sold_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
                            d_m_counts = d_sold_models['Model_Only'].value_counts().reset_index()
                            d_m_counts.columns = ['Make/Model', 'Units Sold']
                            st.markdown("**🏆 Top Sold Models**")
                            st.dataframe(d_m_counts.head(5), use_container_width=True, hide_index=True)
                    with c_right:
                        if not d_missed.empty:
                            d_missed_models = d_missed.copy()
                            d_missed_models['Model_Only'] = d_missed_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
                            d_m_counts = d_missed_models['Model_Only'].value_counts().reset_index()
                            d_m_counts.columns = ['Make/Model', 'Missed Count']
                            st.markdown("**⚠️ Top Missed Models**")
                            st.dataframe(d_m_counts.head(5), use_container_width=True, hide_index=True)
                    
                    if not d_sold.empty:
                        st.markdown("**📄 Top Sold Units (Detail)**")
                        st.dataframe(d_sold[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].sort_values('Attributed Unique Visitors', ascending=False).head(10), column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open")}, use_container_width=True, hide_index=True)
                    
                    if not d_missed.empty:
                        st.markdown("**👀 Missed Opportunities (Detail)**")
                        st.dataframe(d_missed[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].sort_values('Attributed Unique Visitors', ascending=False).head(10), column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open")}, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("### 📥 Export Reports")
    
    include_missed_in_pdf = st.checkbox("Include 'Missed Opportunities' in PDF", value=True)
    include_dealer_details = False
    if domain_count > 1:
        include_dealer_details = st.checkbox("Include 'Dealer Deep Dives' in PDF", value=False)
        
    st.write("") 
    
    ex1, ex2, ex3 = st.columns(3)
    with ex1:
        metrics_bundle = {'units_sold': m_units, 'rev_sold': m_rev, 'pipeline': m_pipe, 'ltb': f"{m_ltb:.1f}", 'new_ltb': f"{new_ltb:.1f}", 'used_ltb': f"{used_ltb:.1f}", 'min_visitors': min_visitors}
        pdf_data = create_pdf_report(df, sold_df, metrics_bundle, missed_df if not sold_df.empty else pd.DataFrame(), include_missed_in_pdf, dealer_group_export, include_dealer_details)
        st.download_button("📥 Download PDF Summary", data=pdf_data, file_name=f"{st.session_state.current_report_id}_Summary.pdf", mime="application/pdf")
    with ex2:
        st.download_button("📥 Download Sold List (CSV)", sold_df[['Dealer', 'Vehicle Name', 'VIN', 'Page Url', 'Attributed Unique Visitors']].to_csv(index=False), f"{st.session_state.current_report_id}_Sold.csv", "text/csv")
    with ex3:
        st.download_button("📥 Download Full Analysis (CSV)", df.to_csv(index=False), f"{st.session_state.current_report_id}_Full_Analysis.csv", "text/csv")

    st.divider()
    
    with st.expander("ℹ️ Glossary & Guide: How to read this report"):
        st.markdown("""
        ### **Definitions & Insights**
        
        **1. Units Sold**
        The total count of vehicles identified as "Sold" (removed from inventory) *after* receiving attributed traffic from our campaign. This confirms that the audience we drove to the site actively shopped for cars that moved off the lot.
        
        **2. Estimated Value (Rev & Pipeline)**
        A data-driven approximation of the inventory's dollar value. 
        * **New Cars:** Calculated using Base MSRP for the specific model.
        * **Used Cars:** Calculated using the base MSRP depreciated by age.
        * *Note: This is a directional estimate to gauge "Total Pipeline Power," not accounting for specific trim levels, options, or dealer markups.*
        
        **3. Look-to-Book Ratio (Velocity Metric)**
        The efficiency metric of the inventory. It measures the conversion velocity of the cars we drove traffic to. By using the Interactive VDP Filter, this transforms into a Velocity Metric—proving that highly concentrated traffic yields significantly higher conversion rates.
        * *Formula:* `(Sold VDPs) ÷ (Total Active VDPs)`
        
        **4. Tier 2 / Auto Group Breakdown**
        Aggregates performance across multiple dealerships if a multi-domain report is uploaded. 
        * *Clean Reporting:* Dealerships that received general traffic but had **0 VDPs shopped** are automatically filtered out of this table to keep insights focused on high-intent inventory shoppers.
        
        **5. Top Sold Units & Missed Opportunities ("The Watch List")**
        * **Top Sold:** The specific vehicles that received the highest exposure and subsequently sold.
        * **Missed Opportunities:** Active vehicles receiving **above-average traffic** that haven't sold yet. Audit these VDPs immediately for missing photos, "Call for Price" buttons, or pricing outliers!
        
        **6. Inventory Syncs & Troubleshooting**
        If your report shows **0 Sold** and triggers an "Action Required" alert, the dealer's inventory database requires a direct connection. 
        * *Action:* Check the top of your report for the **Action Required** panel. It will guide you to find the API URL. The app will automatically save it to our permanent database (Vault) so that dealer is seamlessly synced moving forward!
        """)

else:
    st.info("👈 Upload a CSV in the sidebar to begin analysis.")
    st.markdown("---")
    st.markdown("### 🚀 Version 3.0 Updates")
    st.markdown("Welcome to the latest version of the Auto-Sales Intelligence Agent! Here is what's new:")
    st.markdown("1. **🏢 Auto Group & Tier 2 Recognition:** Automatically detects multi-dealer reports and generates a clean, dealership-by-dealership sales breakdown.")
    st.markdown("2. **🎯 Interactive VDP Filter:** A new real-time slider that lets you filter attributed sales based on the number of VDP visits we drove, helping you prove high-intent demand.")
    st.markdown("3. **⚙️ Automated Dealer Sync (The Vault):** If a dealer's inventory is hidden behind a security provider, the app now generates a built-in guide to easily sync their API. Once saved, the tool remembers it forever!")
    st.markdown("4. **📈 Online Conversions Tracking:** Form submissions and appointment bookings are now recognized and displayed in the Traffic Mix!")
