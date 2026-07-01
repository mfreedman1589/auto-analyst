import streamlit as st
import streamlit.components.v1 as components
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
import html
import random
import threading
from streamlit_gsheets import GSheetsConnection

# --- FETCH ENGINE: curl_cffi impersonates a real Chrome TLS/JA3 fingerprint,
# which defeats the flat 403s that plain `requests` triggers on WAF-protected
# dealer sites. If it isn't installed we fall back to `requests` so the app
# never crashes for the whole company.
try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except Exception:
    HAS_CFFI = False

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
if 'min_visitors' not in st.session_state:
    st.session_state.min_visitors = 1
if 'global_usage_count' not in st.session_state:
    st.session_state.global_usage_count = "..."

# --- THE DEALER API VAULT (Google Sheets Powered) ---
FALLBACK_VAULT = {
    'hananiavw.com': {'app_id': 'YL5AFXM3DW', 'api_key': '59d32b7b5842f84284e044c7ca465498', 'index': 'volkswagenoforangepark-sbm0424_production_inventory'},
    'hondasanmarcos.com': {'app_id': 'V3ZOVI2QFZ', 'api_key': 'ec7553dd56e6d4c8bb447a0240e7aab3', 'index': 'hondaofsanmarcos_production_inventory'}
}

def _clean_cell(value):
    """Normalize a sheet cell to a stripped string ('' for blanks/NaN)."""
    text = str(value).strip()
    return "" if text.lower() in ("", "nan", "none") else text

def _row_is_ignored(app_id, api_key, index_name):
    """A domain is IGNORE if any credential column is literally IGNORE (case-insensitive)."""
    return any(v.upper() == "IGNORE" for v in (app_id, api_key, index_name))

def build_vault_dict(vault_df):
    """
    Turn the sheet DataFrame into the in-memory vault.
    Ignored domains are stored as {'ignore': True} so the scanner has a single,
    foolproof flag to check. Domains with missing creds are simply left out
    (they fall through to the visual scraper).
    """
    vault_dict = {}
    for _, row in vault_df.iterrows():
        domain = _clean_cell(row.get('Base Domain', ''))
        if not domain:
            continue
        app_id = _clean_cell(row.get('App ID', ''))
        api_key = _clean_cell(row.get('API Key', ''))
        index_name = _clean_cell(row.get('Index Name', ''))

        if _row_is_ignored(app_id, api_key, index_name):
            vault_dict[domain] = {'ignore': True}          # hard kill switch
        elif app_id and api_key and index_name:
            vault_dict[domain] = {
                'app_id': app_id,
                'api_key': api_key,
                'index': index_name,
            }
        # else: incomplete row -> not added -> scraper handles it
    return vault_dict

def load_vault():
    """
    The Google Sheet is the ONLY source of truth. Returns:
      (vault_dict, vault_df, conn, loaded_ok)
    On failure we fall back to FALLBACK_VAULT but flag loaded_ok=False so the UI
    can warn the user rather than silently running on stale/tiny credentials.
    """
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        vault_df = conn.read(worksheet="Sheet1", ttl=60)
        vault_dict = build_vault_dict(vault_df)

        try:
            usage_df = conn.read(worksheet="UsageStats", ttl=60)
            st.session_state.global_usage_count = len(usage_df)
        except Exception:
            pass

        return vault_dict, vault_df, conn, True
    except Exception:
        return dict(FALLBACK_VAULT), None, None, False

DEALER_API_VAULT, vault_df, gsheets_conn, VAULT_LOADED_OK = load_vault()

if not VAULT_LOADED_OK:
    st.warning(
        "⚠️ Could not reach the Google Sheet vault. Running on a small emergency "
        "fallback list — dealership syncs and IGNORE flags won't be saved until the "
        "connection is restored.",
        icon="⚠️",
    )

def upsert_vault_row(domain, app_id, api_key, index_name):
    """
    Single write path for the vault. Keeps the sheet authoritative:
      1. Re-read the live sheet (ttl=0, no cache)
      2. Drop any existing row(s) for this domain (prevents duplicate/ghost rows)
      3. Append the new row
      4. Write back and bust the cache so the next load re-reads fresh
    Returns True on success. There is NO local session overlay — after this
    returns, the app reruns and reloads straight from the sheet.
    """
    if gsheets_conn is None:
        return False
    try:
        live_df = gsheets_conn.read(worksheet="Sheet1", ttl=0)
        live_df = live_df[live_df['Base Domain'].astype(str).str.strip() != '']
        # Remove existing entries for this domain (case-insensitive) to avoid dupes
        keep_mask = live_df['Base Domain'].astype(str).str.strip().str.lower() != domain.lower()
        live_df = live_df[keep_mask]
        new_row = pd.DataFrame([{
            'Base Domain': domain,
            'App ID': app_id,
            'API Key': api_key,
            'Index Name': index_name,
        }])
        updated_df = pd.concat([live_df, new_row], ignore_index=True)
        gsheets_conn.update(worksheet="Sheet1", data=updated_df)
        st.cache_data.clear()
        return True
    except Exception:
        return False

# --- VAULT ONBOARDING HELPERS (salesperson-friendly) ---
def normalize_domain(raw):
    """Strip protocol / www / paths down to a bare base domain."""
    d = str(raw).strip().lower()
    d = re.sub(r'^https?://', '', d)
    d = d.replace('www.', '')
    d = d.split('/')[0].split('?')[0].strip().strip('/')
    return d

def parse_algolia_credentials(text, fallback_domain=""):
    """
    Forgiving extractor. Accepts ANY of:
      • Bookmarklet output:   domain|APPID|APIKEY|indexname
      • A raw Algolia URL:    ...x-algolia-application-id=APPID&x-algolia-api-key=APIKEY.../indexes/NAME/query
      • Messy pasted config:  loose text containing the three values
    Returns dict(domain, app_id, api_key, index) or None if it can't find all three.
    """
    if not text:
        return None
    text = str(text).strip()
    domain, app_id, api_key, index = fallback_domain, "", "", ""

    # 1) Pipe format from the bookmarklet: domain|appId|apiKey|index
    if text.count('|') >= 3:
        parts = [p.strip() for p in text.split('|')]
        domain = parts[0] or domain
        app_id, api_key, index = parts[1], parts[2], parts[3]

    # 2) URL query-param format (the old DevTools method)
    if not app_id:
        m = re.search(r'x-algolia-application-id=([A-Za-z0-9]+)', text)
        if m: app_id = m.group(1)
    if not api_key:
        m = re.search(r'x-algolia-api-key=([a-f0-9]{32})', text, re.I)
        if m: api_key = m.group(1)
    if not index:
        m = re.search(r'/indexes/([^/?"\']+)/(?:query|queries)', text)
        if m: index = m.group(1)

    # 3) Loose fallbacks — pull the values out of raw page text / config
    if not app_id:
        m = (re.search(r'([A-Z0-9]{10})-dsn\.algolia\.net', text)
             or re.search(r'(?:application-?id|appid|app_id)["\'\s:=]+([A-Z0-9]{10})', text, re.I))
        if m: app_id = m.group(1).upper()
    if not api_key:
        m = re.search(r'(?:api-?key|apikey|api_key|searchapikey)["\'\s:=]+([a-f0-9]{32})', text, re.I)
        if m: api_key = m.group(1)
    if not index:
        m = (re.search(r'["\']?index(?:name)?["\']?\s*[:=]\s*["\']([a-z0-9_\-]*inventory[a-z0-9_\-]*)["\']', text, re.I)
             or re.search(r'([a-z0-9][a-z0-9_\-]*_production_inventory[a-z0-9_]*)', text, re.I))
        if m: index = m.group(1)

    if app_id and api_key and index:
        return {
            'domain': normalize_domain(domain) if domain else "",
            'app_id': app_id.strip(),
            'api_key': api_key.strip(),
            'index': index.strip(),
        }
    return None

def autodetect_algolia(domain):
    """
    Server-side attempt: fetch the dealer's pages and scrape the Algolia creds
    out of the page config. Uses curl_cffi (Chrome impersonation) so it has the
    best shot past a WAF, but an aggressive firewall can still block this — in
    which case the salesperson should use the Bookmarklet (runs in their browser).
    Returns a creds dict or None.
    """
    domain = normalize_domain(domain)
    if not domain:
        return None
    candidate_paths = ['', '/new-inventory/', '/used-inventory/', '/inventory/', '/searchnew.aspx']
    blob = ""
    for path in candidate_paths:
        url = f"https://www.{domain}{path}"
        try:
            if HAS_CFFI:
                r = cffi_requests.get(url, impersonate="chrome", timeout=12, allow_redirects=True)
            else:
                r = requests.get(url, timeout=8, allow_redirects=True,
                                 headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                                                        'Chrome/122.0.0.0 Safari/537.36'})
            if r.status_code == 200 and r.text:
                blob += "\n" + r.text
                creds = parse_algolia_credentials(blob, fallback_domain=domain)
                if creds:  # found everything — stop early
                    return creds
        except Exception:
            continue
    return parse_algolia_credentials(blob, fallback_domain=domain)

# One-click bookmarklet: runs in the salesperson's OWN browser (no firewall can
# block it), scrapes the Algolia creds from the live page, and copies a clean
# "domain|appId|apiKey|index" line to the clipboard for pasting into the Vault.
BOOKMARKLET_JS = (
    "javascript:(function(){try{"
    "var b=document.documentElement.innerHTML+'\\n'+"
    "Array.prototype.map.call(document.scripts,function(s){return s.textContent||''}).join('\\n');"
    "try{b+='\\n'+performance.getEntriesByType('resource').map(function(e){return e.name}).join('\\n')}catch(e){}"
    "function m(r){var x=b.match(r);return x?x[1]:''}"
    "var a=m(/([A-Z0-9]{10})-dsn\\.algolia\\.net/)||m(/(?:application-?id|appid|app_id)[\"'\\s:=]+([A-Z0-9]{10})/i);"
    "var k=m(/(?:api-?key|apikey|api_key|searchapikey)[\"'\\s:=]+([a-f0-9]{32})/i);"
    "var i=m(/[\"']?index(?:name)?[\"']?\\s*[:=]\\s*[\"']([a-z0-9_\\-]*inventory[a-z0-9_\\-]*)[\"']/i)"
    "||m(/([a-z0-9][a-z0-9_\\-]*_production_inventory[a-z0-9_]*)/i);"
    "var d=location.hostname.replace(/^www\\./,'');"
    "var o=[d,a,k,i].join('|');"
    "if(!a||!k||!i){window.prompt('Could not find all 3 keys on this page. Partial result (copy if useful):',o);return}"
    "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(o).then("
    "function(){window.prompt('COPIED! Paste this into the Vault:',o)},"
    "function(){window.prompt('Copy this line into the Vault:',o)})}"
    "else{window.prompt('Copy this line into the Vault:',o)}"
    "}catch(e){alert('Bookmarklet error: '+e.message)}})();"
)

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
    # HELPER: Sanitizes scraped text so it doesn't crash the PDF encoder
    def safe_str(text):
        if pd.isna(text): return ""
        text = str(text)
        reps = {'“':'"', '”':'"', '‘':"'", '’':"'", '—':'-', '–':'-', '®':'', '™':'', '©':''}
        for k, v in reps.items(): text = text.replace(k, v)
        return text.encode('latin-1', 'ignore').decode('latin-1')

    pdf = FPDF()
    pdf.add_page()
    
    eastern = pytz.timezone('US/Eastern')
    current_time = datetime.datetime.now(eastern)
    
    domain_count = df['Dealer'].nunique()
    report_title = "Auto-Sales Intelligence Report"
    if domain_count == 1:
        report_title = f"{df['Dealer'].iloc[0]} Intelligence Report"

    pdf.set_font("Arial", "B", 18)
    pdf.cell(0, 15, safe_str(report_title), ln=True, align="C")
    
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
            pdf.cell(50, 8, safe_str(row['Dealer'])[:28], border=1)
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
        pdf.cell(100, 8, safe_str(row['Category']), border=1)
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
            pdf.cell(70, 8, safe_str(row['Type']), border=1)
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
            pdf.cell(70, 8, safe_str(row['Price Tier']), border=1)
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
                pdf.cell(100, 8, safe_str(row['Make/Model']), border=1)
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
            name = safe_str(row['Vehicle Name'])[:35]
            pdf.set_font("Arial", "", 9) 
            pdf.cell(80, 8, name, border=1)
            pdf.cell(25, 8, safe_str(row['Type']), border=1)
            pdf.cell(20, 8, str(row['Attributed Unique Visitors']), border=1)
            pdf.set_font("Arial", "", 8)
            pdf.cell(65, 8, safe_str(row['VIN']), border=1)
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
                    pdf.cell(100, 8, safe_str(row['Make/Model']), border=1)
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
                 name = safe_str(row['Vehicle Name'])[:35]
                 url = str(row['Page Url'])
                 pdf.set_text_color(0, 0, 255) 
                 pdf.cell(85, 8, name, border=1, link=url)
                 pdf.set_text_color(0, 0, 0)
                 pdf.set_font("Arial", "", 8) 
                 pdf.cell(65, 8, safe_str(row['VIN']), border=1)
                 pdf.set_font("Arial", "", 9)
                 pdf.cell(20, 8, safe_str(row['Type']), border=1)
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
            pdf.cell(0, 10, f" Dealership Profile: {safe_str(dealer)}", ln=True, fill=True)
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
                        pdf.cell(100, 8, safe_str(row['Make/Model']), border=1)
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
                        pdf.cell(100, 8, safe_str(row['Make/Model']), border=1)
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
                    name = safe_str(row['Vehicle Name'])[:35]
                    pdf.set_font("Arial", "", 9) 
                    pdf.cell(80, 8, name, border=1)
                    pdf.cell(25, 8, safe_str(row['Type']), border=1)
                    pdf.cell(20, 8, str(row['Attributed Unique Visitors']), border=1)
                    pdf.set_font("Arial", "", 8)
                    pdf.cell(65, 8, safe_str(row['VIN']), border=1)
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
                     name = safe_str(row['Vehicle Name'])[:35]
                     url = str(row['Page Url'])
                     pdf.set_text_color(0, 0, 255) 
                     pdf.cell(85, 8, name, border=1, link=url)
                     pdf.set_text_color(0, 0, 0)
                     pdf.set_font("Arial", "", 8) 
                     pdf.cell(65, 8, safe_str(row['VIN']), border=1)
                     pdf.set_font("Arial", "", 9)
                     pdf.cell(20, 8, safe_str(row['Type']), border=1)
                     pdf.cell(20, 8, str(row['Attributed Unique Visitors']), border=1)
                     pdf.ln()
                 
    pdf_out = pdf.output(dest='S')
    return pdf_out.encode('latin-1') if isinstance(pdf_out, str) else bytes(pdf_out)

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

def scan_url(url, session, ignored_domains, ignore_lock):
    """
    Decide a VDP's status. Order of operations:
      1. HARD IGNORE: if the sheet flags the domain, or we've already been blocked
         by it this run, go straight to the visual scraper. No API call is built.
      2. If there are no usable credentials, use the scraper.
      3. Otherwise fire the Algolia API. On 200 -> read hits. On 401/403/429 (or any
         network error) -> blacklist the domain for the rest of this run and fall
         back to the scraper immediately. We never return a raw API ERROR for a
         blocked API — the scraper always gets a chance.
    """
    domain = urlparse(str(url)).netloc.replace('www.', '').lower()

    # --- 1. HARD KILL SWITCH ---------------------------------------------
    with ignore_lock:
        blocked_this_run = domain in ignored_domains
    cfg = DEALER_API_VAULT.get(domain)
    sheet_says_ignore = isinstance(cfg, dict) and cfg.get('ignore') is True

    if blocked_this_run or sheet_says_ignore:
        return check_universal_status(url, session)

    # --- 2. NO USABLE CREDENTIALS ----------------------------------------
    has_creds = (
        isinstance(cfg, dict)
        and cfg.get('app_id') and cfg.get('api_key') and cfg.get('index')
    )
    if not has_creds:
        return check_universal_status(url, session)

    vin = extract_vin(url)
    if vin == "N/A":
        # Can't query Algolia without a VIN — let the scraper read the page.
        return check_universal_status(url, session)

    # --- 3. FIRE THE API (with clean fallback on failure) ----------------
    app_id, api_key, index_name = cfg['app_id'], cfg['api_key'], cfg['index']
    api_endpoint = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{index_name}/query"
    api_headers = {
        "x-algolia-application-id": app_id,
        "x-algolia-api-key": api_key,
        "Content-Type": "application/json",
    }

    try:
        resp = session.post(api_endpoint, headers=api_headers,
                            json={"params": f"query={vin}"}, timeout=5)
    except Exception:
        # Network/DNS/timeout talking to the API -> scraper.
        return check_universal_status(url, session)

    if resp.status_code == 200:
        try:
            hits = resp.json().get("nbHits", 0)
        except Exception:
            return check_universal_status(url, session)
        return "Available" if hits > 0 else "SOLD (Not in Dealer Database)"

    # Blocked / unauthorized / rate-limited: kill this domain's API for the run.
    if resp.status_code in (401, 403, 429):
        with ignore_lock:
            ignored_domains.add(domain)

    # Any non-200 -> fall back to the scraper instead of returning an ERROR.
    return check_universal_status(url, session)

def check_universal_status(url, session):
    url = str(url).strip() # STRIP INVISIBLE SPACES FROM CSV
    year = get_year(url)
    vin = extract_vin(url)
    if not year: return "N/A"
    
    # WAF EVASION HEADERS
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0'
    ]
    
    try:
        headers = {
            'User-Agent': random.choice(user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1'
        }
        
        if HAS_CFFI:
            # Real Chrome TLS fingerprint — the disguise plain requests can't wear.
            # impersonate handles headers + JA3/JA4, so no manual header dict needed.
            response = cffi_requests.get(
                url, impersonate="chrome", timeout=10, allow_redirects=True
            )
        else:
            response = session.get(url, headers=headers, timeout=5, allow_redirects=True)
        
        if response.status_code in [403, 406, 429]:
            return f"ERROR (Website Firewall Blocked Scan: {response.status_code})"
            
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
        
        # --- SOFT-SOLD / OVERLAY / JSON SCANNER ---
        soft_sold_phrases = [
            "no longer available",
            "this vehicle is sold",
            "vehicle has been sold",
            "currently out of stock",
            "schema.org/outofstock",     
            "schema.org/soldout",        
            '"inventorystatus":"sold"',  
            '"inventorystatus": "sold"', 
            '"vehiclestatus":"sold"',    
            '"vehiclestatus": "sold"',
            '"isavailable":false',
            '"isavailable": false'
        ]
        if any(phrase in text_lower for phrase in soft_sold_phrases):
            return "SOLD (Out of Stock Overlay)"
        # ----------------------------------------
        
        bot_titles = ['just a moment', 'attention required', 'verify you are human', 'access denied', 'pardon our interruption', 'security check']
        if any(b in page_title for b in bot_titles): return "ERROR (Cloudflare Bot Block)"
            
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

# --- ADD A DEALER TO THE VAULT (salesperson-friendly) ---
with st.sidebar.expander("🔐 Add Dealer to Vault", expanded=False):
    st.caption("Only needed if a dealer's inventory won't scan on its own. Pick the easiest option that works.")
    tab_auto, tab_book, tab_manual = st.tabs(["⚡ Auto-Detect", "🔖 Bookmarklet", "✍️ Manual"])

    # ---- Option A: Auto-Detect (easiest; may be blocked by strict firewalls) ----
    with tab_auto:
        st.markdown("<span style='font-size:0.85em;color:#555;'>Type the dealer's website and we'll try to find the keys automatically.</span>", unsafe_allow_html=True)
        ad_domain = st.text_input("Dealer website", placeholder="e.g., pittstoyota.com", key="ad_domain")
        if st.button("⚡ Auto-Detect & Save", use_container_width=True, key="ad_btn"):
            dom = normalize_domain(ad_domain)
            if not dom:
                st.warning("Enter the dealer's website first.")
            else:
                with st.spinner(f"Searching {dom} for inventory keys..."):
                    creds = autodetect_algolia(dom)
                if creds:
                    if upsert_vault_row(dom, creds['app_id'], creds['api_key'], creds['index']):
                        st.success(f"✅ {dom} synced automatically!")
                        st.rerun()
                    else:
                        st.error("Found the keys, but couldn't write to the Google Sheet.")
                else:
                    st.warning("Couldn't auto-detect (this dealer's firewall likely blocked us). "
                               "Use the **🔖 Bookmarklet** tab — it runs in your own browser and works even when this doesn't.")

    # ---- Option B: Bookmarklet (most reliable; runs in the salesperson's browser) ----
    with tab_book:
        st.markdown(
            "<span style='font-size:0.85em;color:#555;'>"
            "<b>One-time setup:</b> drag the button below up to your browser's bookmarks bar."
            "</span>", unsafe_allow_html=True)

        safe_href = html.escape(BOOKMARKLET_JS, quote=True)
        components.html(
            f"""
            <div style="font-family:sans-serif;padding:4px 0;">
              <a href="{safe_href}"
                 style="display:inline-block;background:#D70015;color:#fff;text-decoration:none;
                        font-weight:bold;padding:8px 14px;border-radius:6px;border:1px solid #A30010;
                        cursor:grab;font-size:14px;">
                 🔑 Get Dealer Keys
              </a>
              <div style="font-size:12px;color:#777;margin-top:6px;">☝️ Drag me to your bookmarks bar</div>
            </div>
            """,
            height=70,
        )

        st.markdown(
            "<span style='font-size:0.85em;color:#555;'>"
            "<b>Every time:</b> open the dealer's website → click your <b>Get Dealer Keys</b> bookmark → "
            "it copies a line → paste it below and Save."
            "</span>", unsafe_allow_html=True)

        with st.popover("Can't drag it? Copy the code instead"):
            st.caption("Create a new bookmark, name it 'Get Dealer Keys', and paste this as the URL/address:")
            st.code(BOOKMARKLET_JS, language=None)

        book_paste = st.text_area("Paste the copied line here", placeholder="fivestarfordga.com|APPID|APIKEY|indexname",
                                  key="book_paste", height=80)
        if st.button("💾 Save to Vault", use_container_width=True, key="book_btn"):
            creds = parse_algolia_credentials(book_paste)
            if creds and creds['domain']:
                if upsert_vault_row(creds['domain'], creds['app_id'], creds['api_key'], creds['index']):
                    st.success(f"✅ {creds['domain']} synced to the vault!")
                    st.rerun()
                else:
                    st.error("Couldn't write to the Google Sheet.")
            else:
                st.error("That didn't look right. Click the bookmark on the dealer's site, then paste what it copies.")

    # ---- Option C: Manual (power users / DevTools method) ----
    with tab_manual:
        st.markdown("<span style='font-size:0.85em;color:#555;'>Paste a full Algolia API URL (or any text containing the keys).</span>", unsafe_allow_html=True)
        qd_domain = st.text_input("Dealer website", placeholder="e.g., pittstoyota.com", key="qd_domain")
        qd_url = st.text_area("Algolia API URL / config", placeholder="Paste here...", key="qd_url", height=100)
        if st.button("💾 Save to Vault", use_container_width=True, key="manual_btn"):
            if qd_domain and qd_url:
                creds = parse_algolia_credentials(qd_url, fallback_domain=qd_domain)
                if creds:
                    dom = creds['domain'] or normalize_domain(qd_domain)
                    if upsert_vault_row(dom, creds['app_id'], creds['api_key'], creds['index']):
                        st.success(f"✅ {dom} synced to the vault!")
                        st.rerun()
                    else:
                        st.error("Couldn't write to the Google Sheet. Check the connection and try again.")
                else:
                    st.error("Couldn't find all three keys (App ID, API Key, Index) in what you pasted.")
            else:
                st.warning("Please fill both fields.")

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
    
    session = requests.Session()
    retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=60, pool_maxsize=60)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    
    vdp_results = {}

    # Per-run blacklist of domains whose API blocked us (403/401/429). Once a
    # domain lands here, every remaining VDP for it skips the API entirely.
    ignored_domains = set()
    ignore_lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        future_to_url = {
            executor.submit(scan_url, url, session, ignored_domains, ignore_lock): url
            for url in vdp_urls
        }
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
                        
                        c1, c2, c3, c4 = st.columns([3, 3, 1, 1])
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
                                    index_match = re.search(r'/indexes/([^/]+)/(?:query|queries)', val)
                                    
                                    if app_id_match and api_key_match and index_match:
                                        if upsert_vault_row(d_domain, app_id_match.group(1),
                                                            api_key_match.group(1), index_match.group(1)):
                                            st.rerun()
                                        else:
                                            st.error("Couldn't write to the Google Sheet.")
                                    else:
                                        st.error("Invalid API URL.")
                                else:
                                    st.warning("Paste a URL first!")
                        with c4:
                            if st.button("🚫 Ignore", key=f"ign_{d_domain}", help="Click this if the dealership does not use Dealer Inspire. The app will remember not to prompt you again.", use_container_width=True):
                                if upsert_vault_row(d_domain, 'IGNORE', 'IGNORE', 'IGNORE'):
                                    st.rerun()
                                else:
                                    st.error("Couldn't write the IGNORE flag to the Google Sheet.")

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
        missed_df = df[(df['Sold_Status'] == 'Available') & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= missed_threshold)].sort_values('Attributed Unique Visitors', ascending=False).head(10)
    else:
        missed_df = df[(df['Sold_Status'] == 'Available') & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= min_visitors)].sort_values('Attributed Unique Visitors', ascending=False).head(10)

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
    st.markdown("### 🚀 Version 4.0 Updates")
    st.markdown("Welcome to the latest version of the Auto-Sales Intelligence Agent! Here is what's new for your workflow:")
    st.markdown("1. **⚡ Faster Dealership Setup:** A new 'Quick Add' tool in the sidebar lets you instantly connect a dealership's hidden inventory without waiting to run a full report first.")
    st.markdown("2. **📄 Bulletproof PDF Exports:** Exporting is now rock-solid. We fixed an issue where fancy formatting (like ™ or ® symbols) in a car's name would crash the PDF downloads.")
    st.markdown("3. **🛡️ Uninterrupted Scanning:** The tool is now much better at bypassing strict dealership security firewalls, resulting in fewer 'Blocked' errors and faster results.")
    st.markdown("4. **🏷️ Catching 'Hidden' Sales:** Dealerships often leave sold cars on their site for SEO but mark them 'Out of Stock.' The tool now reads the fine print and rightfully counts these as sold.")
    st.markdown("5. **🏢 Auto Group Dashboards & Live Filters:** Uploading a multi-site Auto Group report now automatically generates a clean, store-by-store breakdown. Plus, a new interactive slider lets you adjust traffic thresholds on the fly to prove high conversion velocity to your clients.")
