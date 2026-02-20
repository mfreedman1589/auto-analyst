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
from fpdf import FPDF
import io

# --- CONFIGURATION ---
st.set_page_config(page_title="Auto-Analyst v7.1 (Advanced Bypass)", layout="wide")

if 'history' not in st.session_state:
    st.session_state.history = {} 
if 'current_report_id' not in st.session_state:
    st.session_state.current_report_id = None 

# --- LOGIN ---
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if st.session_state.password_correct:
        return True
    st.title("🔒 Auto-Analyst Login (Dev)")
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
def create_pdf_report(df, sold_df, metrics, missed_df, include_missed):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 20)
    pdf.cell(0, 15, "Auto-Sales Intelligence Report", ln=True, align="C")
    pdf.set_font("Arial", "I", 10)
    pdf.cell(0, 10, f"Generated on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=True, align="C")
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

    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, " 2. Market Insights", ln=True, fill=True)
    pdf.ln(5)
    
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 8, "Traffic Mix (Visitors by Page Type)", ln=True)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(100, 8, "Page Category", border=1)
    pdf.cell(40, 8, "Visitors", border=1)
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
        top_models = sold_models['Model_Only'].value_counts().reset_index()
        top_models.columns = ['Make/Model', 'Units Sold']
        top_models = top_models[top_models['Units Sold'] > 1].head(10)
        if not top_models.empty:
            pdf.set_font("Arial", "B", 14)
            pdf.cell(0, 10, " 3. Top Sold Models (Aggregated > 1 Unit)", ln=True, fill=True)
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
        pdf.set_font("Arial", "B", 14)
        pdf.cell(0, 10, " 4. Missed Opportunities (The Watch List)", ln=True, fill=True)
        pdf.ln(5)
        
        if not missed_df.empty:
            missed_models = missed_df.copy()
            missed_models['Model_Only'] = missed_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
            top_missed = missed_models['Model_Only'].value_counts().reset_index()
            top_missed.columns = ['Make/Model', 'Count']
            top_missed = top_missed[top_missed['Count'] > 1].head(10)
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
             for _, row in missed_df.iterrows():
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

def estimate_value(row):
    name = str(row['Vehicle Name']).lower()
    vehicle_type = str(row['Type']).lower()
    BRAND_DEFAULTS = {'cadillac': 65000, 'mercedes': 70000, 'bmw': 65000, 'audi': 60000, 'lexus': 60000, 'ford': 45000, 'chevrolet': 40000, 'toyota': 38000, 'honda': 35000, 'volkswagen': 32000}
    baseline = 35000
    for brand, price in BRAND_DEFAULTS.items():
        if brand in name:
            baseline = price
            break
    if 'new' in vehicle_type: return int(baseline)
    year_match = re.search(r'\d{4}', name)
    year = int(year_match.group(0)) if year_match else 2025
    age = datetime.datetime.now().year + 1 - year
    value = baseline if age <= 0 else baseline * 0.85 * (0.90 ** max(0, age - 1))
    return int(value)

def get_price_tier(price):
    if price < 30000: return "Budget (<$30k)"
    if price < 60000: return "Core ($30k-$60k)"
    return "Premium ($60k+)"

def get_year(url):
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def extract_vin(url):
    match = re.search(r'([A-HJ-NPR-Z0-9]{17})', str(url).upper())
    if match: return match.group(1)
    match = re.search(r'([a-zA-Z0-9]{10,})(?:\.htm|\.html|/|$|\?)', str(url))
    return match.group(1).upper() if match else "N/A"

def extract_type(url):
    u = str(url).lower()
    if 'used' in u and 'new' not in u: return 'Used'
    if 'new' in u and 'used' not in u: return 'New'
    if '/used' in u or '-used-' in u or '=used' in u or 'preowned' in u: return 'Used'
    if '/new' in u or '-new-' in u or '=new' in u: return 'New'
    return 'New' if re.search(r'202[5-7]', u) else 'Used'

def clean_name_universal(url):
    year = get_year(url)
    if not year: return "Unknown Vehicle"
    make = ""
    rest = url.split(year)[-1].replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '').replace('.html', '')
    tokens = rest.split()
    junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Heritage', 'Twin', 'Pine', 'Wholesale', 'New', 'Used', 'Preowned', 'Inventory', 'Parts', 'Service', 'Finance', 'Global', 'Incentives', 'Offers']
    clean_tokens = [t for t in tokens if not (len(t) > 10 and any(c.isdigit() for c in t)) and t.title() not in junk]
    return f"{year} {make} {' '.join(clean_tokens)}".title().strip()

def categorize(u):
    u = str(u).lower()
    if u.endswith('.com/') or u.endswith('.com'): return 'Homepage'
    if any(x in u for x in ['service', 'parts', 'collision', 'appointment', 'maintenance']): return 'Service'
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

# --- THE ADVANCED SITEMAP ENGINE (v7.1) ---
def fetch_sitemap_vins(base_url, session):
    active_vins = set()
    sitemap_urls = []
    log = [] # Diagnostic log
    
    headers_list = [
        {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
        {'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'}
    ]
    
    # 1. Ask robots.txt where the sitemap is hidden
    robots_url = base_url.rstrip('/') + '/robots.txt'
    for headers in headers_list:
        try:
            resp = session.get(robots_url, headers=headers, timeout=5)
            log.append(f"Robots.txt check ({headers['User-Agent'][:20]}): {resp.status_code}")
            if resp.status_code == 200:
                matches = re.findall(r'(?i)Sitemap:\s*(https?://[^\s]+)', resp.text)
                sitemap_urls.extend(matches)
                if matches: break
        except Exception as e:
            log.append(f"Robots.txt error: {e}")

    # Fallbacks
    if not sitemap_urls:
        sitemap_urls = [
            base_url.rstrip('/') + '/sitemap_index.xml',
            base_url.rstrip('/') + '/sitemap.xml',
            base_url.rstrip('/') + '/vehicle-sitemap.xml',
            base_url.rstrip('/') + '/sitemap-inventory.xml'
        ]
    sitemap_urls = list(set(sitemap_urls))

    # 2. Extract VINs from all discovered sitemaps
    for sitemap_url in sitemap_urls:
        for headers in headers_list:
            try:
                resp = session.get(sitemap_url, headers=headers, timeout=5)
                log.append(f"Sitemap check {sitemap_url}: {resp.status_code}")
                if resp.status_code == 200:
                    text = resp.text
                    sub_sitemaps = re.findall(r'<loc>(.*?\.xml)</loc>', text)
                    vins_in_text = re.findall(r'([A-HJ-NPR-Z0-9]{17})', text.upper())
                    active_vins.update(vins_in_text)

                    # Dive into sub-sitemaps (common on WordPress/DealerInspire)
                    for sub in sub_sitemaps:
                        if any(x in sub.lower() for x in ['inventory', 'vehicle', 'vdp', 'car', 'post']):
                            sub_resp = session.get(sub, headers=headers, timeout=5)
                            log.append(f"Sub-Sitemap check {sub}: {sub_resp.status_code}")
                            if sub_resp.status_code == 200:
                                sub_vins = re.findall(r'([A-HJ-NPR-Z0-9]{17})', sub_resp.text.upper())
                                active_vins.update(sub_vins)
                    if len(active_vins) > 0:
                        return active_vins, log
            except Exception as e:
                log.append(f"Sitemap error {sitemap_url}: {e}")
                
    return active_vins, log


def check_universal_status(url, session):
    year = get_year(url)
    vin = extract_vin(url)
    if not year: return "N/A"
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = session.get(url, headers=headers, timeout=5, allow_redirects=True)
        if response.status_code in [403, 406, 429]: return f"ERROR (Website Firewall Blocked Scan: {response.status_code})"
        if response.status_code in [404, 410]: return "SOLD (404 Error)"
        orig_base = url.lower().split('?')[0].rstrip('/').replace('https://', '').replace('http://', '').replace('www.', '')
        final_base = response.url.lower().split('?')[0].rstrip('/').replace('https://', '').replace('http://', '').replace('www.', '')
        if orig_base != final_base:
            if vin != "N/A" and vin.lower() not in final_base: return "SOLD (HTTP Redirect)"
            if year not in final_base: return "SOLD (HTTP Redirect)"

        text = response.text 
        if '<title>Just a moment...</title>' in text or 'Cloudflare' in text: return "ERROR (Cloudflare Bot Block)"
        meta_refresh = re.search(r'<meta[^>]*url=([^"\'>\s]+)["\']?', text, re.IGNORECASE)
        if meta_refresh and vin != "N/A" and vin.lower() not in meta_refresh.group(1).lower(): return "SOLD (Meta Refresh Redirect)"
        title_match = re.search(r'<title[^>]*>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
        page_title = title_match.group(1).strip().lower() if title_match else ""
        if 'not found' in page_title or '404' in page_title or 'error' in page_title: return "SOLD (Page Not Found)"
        search_indicators = ['search', 'results', 'all vehicles', 'inventory']
        if any(x in page_title for x in search_indicators) and year not in page_title: return "SOLD (Soft Redirect)"
        return "Available"
    except requests.exceptions.Timeout: return "ERROR (Timeout)"
    except Exception as e: return "Available"


# --- UI DASHBOARD ---
st.title("🚗 Auto-Sales Intelligence Agent (v7.1)")

st.sidebar.markdown("### 📥 New Analysis")
uploaded_file = st.sidebar.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

use_sitemap_exploit = st.sidebar.checkbox("🥷 Enable Firewall Bypass (Sitemap Exploit)", value=False, help="Uses robots.txt to find dealer sitemaps and bypass Cloudflare.")

if uploaded_file is not None:
    if st.sidebar.button("🚀 Run Diagnostic Analysis"):
        df_raw = pd.read_csv(uploaded_file)
        
        url_col = 'Page Url' if 'Page Url' in df_raw.columns else 'Page URL' if 'Page URL' in df_raw.columns else df_raw.columns[0]
        df_raw.rename(columns={url_col: 'Page Url'}, inplace=True)
        
        df_raw['Category'] = df_raw['Page Url'].apply(categorize)
        vdp_urls = df_raw[df_raw['Category'] == 'VDP']['Page Url'].tolist()
        
        st.info(f"Scanning {len(vdp_urls)} Vehicles. Calculating Valuations...")
        progress_bar = st.progress(0)
        
        session = requests.Session()
        retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=60, pool_maxsize=60)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        
        vdp_results = {}
        sitemap_log = []
        
        # --- THE SITEMAP BYPASS BRANCH ---
        if use_sitemap_exploit and len(vdp_urls) > 0:
            parsed_uri = urlparse(vdp_urls[0])
            base_url = '{uri.scheme}://{uri.netloc}/'.format(uri=parsed_uri)
            st.warning(f"🥷 Firewall Bypass Activated. Analyzing {base_url} robots.txt...")
            
            active_vins, sitemap_log = fetch_sitemap_vins(base_url, session)
            st.session_state.sitemap_log = sitemap_log # Save log to view later
            
            if len(active_vins) > 0:
                st.success(f"🔓 Success! Found {len(active_vins)} active VINs from Dealer Sitemap. Cross-referencing...")
                for i, url in enumerate(vdp_urls):
                    vin = extract_vin(url)
                    if vin != "N/A":
                        vdp_results[url] = "Available" if vin in active_vins else "SOLD (Missing from Sitemap)"
                    else:
                        vdp_results[url] = "ERROR (No VIN in URL)"
                    progress_bar.progress((i + 1) / len(vdp_urls))
            else:
                st.error("❌ Bypass Failed: Cloudflare blocked the Sitemap fetch. Reverting to standard scan...")
                use_sitemap_exploit = False
                
        # --- THE STANDARD SCAN BRANCH ---
        if not use_sitemap_exploit:
            with concurrent.futures.ThreadPoolExecutor(max_workers=60) as executor:
                future_to_url = {executor.submit(check_universal_status, url, session): url for url in vdp_urls}
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
        
        domain = urlparse(vdp_urls[0]).netloc.replace('www.', '').split('.')[0].title() if len(vdp_urls) > 0 else "Unknown_Dealer"
        report_time = datetime.datetime.now().strftime('%I:%M %p')
        report_id = f"{domain} ({report_time})"
        
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

# --- MAIN DASHBOARD DISPLAY ---
if st.session_state.current_report_id is not None:
    st.subheader(f"Viewing Report: {st.session_state.current_report_id}")
    df = st.session_state.history[st.session_state.current_report_id]
    
    # Optional Diagnostic Log View
    if 'sitemap_log' in st.session_state and st.session_state.sitemap_log:
        with st.expander("🛠️ View Sitemap Diagnostics Log"):
            for l in st.session_state.sitemap_log:
                st.write(l)

    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'].str.contains('VDP', na=False)]
    
    error_df = df[df['Sold_Status'].str.startswith('ERROR', na=False)]
    if not error_df.empty:
        st.warning(f"⚠️ **Diagnostic Alert:** The dealer's firewall actively blocked **{len(error_df)}** of our requests. The Sold count is likely incomplete.")
    
    new_vdp_all = df[(df['Category'].str.contains('VDP', na=False)) & (df['Type'] == 'New')]
    new_sold = sold_df[sold_df['Type'] == 'New']
    new_ltb = (len(new_sold) / len(new_vdp_all) * 100) if len(new_vdp_all) > 0 else 0
    
    used_vdp_all = df[(df['Category'].str.contains('VDP', na=False)) & (df['Type'] == 'Used')]
    used_sold = sold_df[sold_df['Type'] == 'Used']
    used_ltb = (len(used_sold) / len(used_vdp_all) * 100) if len(used_vdp_all) > 0 else 0
    
    m_units = len(sold_df)
    m_rev = sold_df['Est. Value'].sum()
    m_pipe = vdp_df['Est. Value'].sum()
    m_ltb = (len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0)
    
    st.markdown("### 📊 Executive Summary")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Units Sold", m_units)
    m2.metric("Est. Revenue Sold", f"${m_rev:,.0f}")
    m3.metric("Pipeline Value (Active)", f"${m_pipe:,.0f}")
    m4.metric(label="Look-to-Book Ratio", value=f"{m_ltb:.1f}%", delta=f"New: {new_ltb:.1f}% | Used: {used_ltb:.1f}%", delta_color="off")

    st.divider()
    
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Traffic Mix**")
        traffic_data = df.groupby('Category')['Attributed Unique Visitors'].sum().reset_index().sort_values('Attributed Unique Visitors', ascending=False)
        fig1 = px.bar(traffic_data, x='Category', y='Attributed Unique Visitors')
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
                st.divider()

        st.subheader("Top Sold Units (Detail)")
        if not sold_df.empty:
            top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
            display_sold = top_sold[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
            display_sold.index += 1
            st.dataframe(display_sold, column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open"), "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")}, use_container_width=True)

    with t2:
        if not sold_df.empty:
            avg_v = sold_df['Attributed Unique Visitors'].mean()
            missed_df = df[(~df['Is Sold']) & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= avg_v)].sort_values('Attributed Unique Visitors', ascending=False).head(10)
            if not missed_df.empty:
                missed_models = missed_df.copy()
                missed_models['Model_Only'] = missed_models['Vehicle Name'].apply(lambda x: re.sub(r'^\d{4}\s+', '', str(x)))
                missed_counts = missed_models['Model_Only'].value_counts().reset_index()
                missed_counts.columns = ['Make/Model', 'Missed Count']
                top_missed = missed_counts[missed_counts['Missed Count'] > 1].head(10)
                if not top_missed.empty:
                    st.subheader("⚠️ Top Missed Models (Aggregated > 1 Unit)")
                    st.dataframe(top_missed, use_container_width=True, hide_index=True)
                    st.divider()
                st.subheader("Missed Opportunities (Detail)")
                display_missed = missed_df[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
                display_missed.index += 1
                st.dataframe(display_missed, column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open"), "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")}, use_container_width=True)

    st.divider()
    st.markdown("### 📥 Export Reports")
    include_missed_in_pdf = st.checkbox("Include 'Missed Opportunities' in PDF Report?", value=True)
    
    ex1, ex2, ex3 = st.columns(3)
    with ex1:
        metrics_bundle = {'units_sold': m_units, 'rev_sold': m_rev, 'pipeline': m_pipe, 'ltb': f"{m_ltb:.1f}", 'new_ltb': f"{new_ltb:.1f}", 'used_ltb': f"{used_ltb:.1f}"}
        pdf_data = create_pdf_report(df, sold_df, metrics_bundle, missed_df if not sold_df.empty else pd.DataFrame(), include_missed_in_pdf)
        st.download_button("📥 Download PDF Summary", data=pdf_data, file_name=f"{st.session_state.current_report_id}_Summary.pdf", mime="application/pdf")
    with ex2:
        st.download_button("📥 Download Sold List (CSV)", sold_df[['Vehicle Name', 'VIN', 'Page Url', 'Attributed Unique Visitors']].to_csv(index=False), f"{st.session_state.current_report_id}_Sold.csv", "text/csv")
    with ex3:
        st.download_button("📥 Download Full Analysis (CSV)", df.to_csv(index=False), f"{st.session_state.current_report_id}_Full_Analysis.csv", "text/csv")

else:
    st.info("👈 Upload a CSV in the sidebar to begin analysis.")
