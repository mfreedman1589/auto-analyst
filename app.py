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
st.set_page_config(page_title="Auto-Sales Intelligence Agent", layout="wide")

if 'history' not in st.session_state:
    st.session_state.history = {} 
if 'current_report_id' not in st.session_state:
    st.session_state.current_report_id = None 

# --- THE DEALER API VAULT ---
DEALER_API_VAULT = {
    'hananiavw.com': {
        'app_id': 'YL5AFXM3DW',
        'api_key': '59d32b7b5842f84284e044c7ca465498',
        'index': 'volkswagenoforangepark-sbm0424_production_inventory'
    },
    'hondasanmarcos.com': {
        'app_id': 'V3ZOVI2QFZ',
        'api_key': 'ec7553dd56e6d4c8bb447a0240e7aab3',
        'index': 'hondaofsanmarcos_production_inventory'
    }
}

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
        model_counts = sold_models['Model_Only'].value_counts().reset_index()
        model_counts.columns = ['Make/Model', 'Units Sold']
        top_models = model_counts[model_counts['Units Sold'] > 1].head(10)
        
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
    year_match = re.search(r'\d{4}', name)
    year = int(year_match.group(0)) if year_match else 2025
    current_year = datetime.datetime.now().year + 1
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

def clean_name_universal(url):
    year = get_year(url)
    if not year: return "Unknown Vehicle"
    path = urlparse(url).path.lower()
    brands = ['Jeep', 'Ford', 'Gmc', 'Toyota', 'Dodge', 'Ram', 'Chrysler', 'Chevrolet', 'Honda', 'Nissan', 'Hyundai', 'Kia', 'Bmw', 'Lexus', 'Volvo', 'Volkswagen', 'Subaru', 'Mazda', 'Mercedes', 'Audi', 'Cadillac', 'Buick', 'Acura', 'Infiniti', 'Lincoln', 'Land Rover', 'Jaguar', 'Porsche', 'Mini']
    make = ""
    for b in brands:
        if b.lower() in path:
            make = b
            break
    rest = url.split(year)[-1].replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '').replace('.html', '')
    tokens = rest.split()
    junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Heritage', 'Twin', 'Pine', 'Wholesale', 'New', 'Used', 'Preowned', 'Inventory', 'Parts', 'Service', 'Finance', 'Global', 'Incentives', 'Offers']
    clean_tokens = [t for t in tokens if not (len(t) > 10 and any(c.isdigit() for c in t)) and t.title() not in junk and t.title() != make]
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

# --- THE STANDARD HTML SCANNING ENGINE ---
def check_universal_status(url, session):
    year = get_year(url)
    vin = extract_vin(url)
    if not year: return "N/A"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
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
        soup = BeautifulSoup(text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        
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

# Sidebar: Cleaned Up Dealer Inspire Fix UI
with st.sidebar.expander("🛠️ Dealer Inspire Fix (Firewall Bypass)"):
    st.markdown("Use this if your report returns **0 Sold** with a **Firewall Blocked** alert.")
    use_algolia_api = st.checkbox("Enable Firewall Override", value=False)
    algolia_url = ""
    if use_algolia_api:
        algolia_url = st.text_input("Paste API Query URL here:")
    st.markdown("---")
    st.markdown("**How to find the API URL:**")
    st.markdown("1. Open Chrome and go to the dealer's Used Inventory.\n2. Right-click > **Inspect**.\n3. Click the **Network** tab.\n4. Click the **Fetch/XHR** filter.\n5. Refresh the page.\n6. Search for **`inventory`**.\n7. Right-click the Request URL and copy it.")

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
        
        # --- DEALER VAULT CHECK ---
        domain_to_check = urlparse(vdp_urls[0]).netloc.replace('www.', '').lower() if len(vdp_urls) > 0 else ""
        vault_config = DEALER_API_VAULT.get(domain_to_check)

        # --- THE API WIRETAP BRANCH (Vault or Manual) ---
        if vault_config or (use_algolia_api and algolia_url):
            if vault_config:
                st.success(f"🔓 Known Firewall Detected for {domain_to_check}. Automatically pulling keys from Vault to bypass!")
                app_id = vault_config['app_id']
                api_key = vault_config['api_key']
                index_name = vault_config['index']
            else:
                st.warning("⚡ Dealer Inspire Override Activated. Bypassing HTML...")
                app_id_match = re.search(r'x-algolia-application-id=([^&]+)', algolia_url)
                api_key_match = re.search(r'x-algolia-api-key=([^&]+)', algolia_url)
                index_match = re.search(r'/indexes/([^/]+)/query', algolia_url)
                
                if app_id_match and api_key_match and index_match:
                    app_id = app_id_match.group(1)
                    api_key = api_key_match.group(1)
                    index_name = index_match.group(1)
                else:
                    st.error("❌ Could not parse valid credentials from the URL provided. Reverting to standard HTML scan...")
                    app_id = None
            
            if app_id:
                api_endpoint = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{index_name}/query"
                api_headers = {
                    "x-algolia-application-id": app_id,
                    "x-algolia-api-key": api_key,
                    "Content-Type": "application/json"
                }
                
                for i, url in enumerate(vdp_urls):
                    vin = extract_vin(url)
                    if vin != "N/A":
                        try:
                            payload = {"params": f"query={vin}"}
                            resp = session.post(api_endpoint, headers=api_headers, json=payload, timeout=5)
                            if resp.status_code == 200:
                                hits = resp.json().get("nbHits", 0)
                                vdp_results[url] = "Available" if hits > 0 else "SOLD (Not in Dealer Database)"
                            else:
                                vdp_results[url] = f"ERROR (Database Code: {resp.status_code})"
                        except Exception as e:
                            vdp_results[url] = "ERROR (Database Request Failed)"
                    else:
                        vdp_results[url] = "ERROR (No VIN in URL)"
                    progress_bar.progress((i + 1) / len(vdp_urls))
        
        # --- THE STANDARD HTML SCAN BRANCH ---
        if not vault_config and not (use_algolia_api and algolia_url):
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
    
    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'].str.contains('VDP', na=False)]
    
    error_df = df[df['Sold_Status'].str.startswith('ERROR', na=False)]
    if not error_df.empty:
        st.warning(f"🚨 **Firewall Block Detected:** The dealer's website actively blocked **{len(error_df)}** of our scans.\n\n👉 *If this is a Dealer Inspire website, try using the **'Dealer Inspire Fix'** in the left sidebar to bypass the firewall!*")
    
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
    
    if not sold_df.empty:
        avg_v = sold_df['Attributed Unique Visitors'].mean()
        missed_df = df[(~df['Is Sold']) & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= avg_v)].sort_values('Attributed Unique Visitors', ascending=False).head(10)
    else:
        missed_df = pd.DataFrame()

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

    st.divider()

    # --- AGGREGATED TABLES (Side-by-Side) ---
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

    # --- DETAILED TABLES (Full Width to prevent scrollbars) ---
    if not sold_df.empty:
        st.subheader("📄 Top Sold Units (Detail)")
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        display_sold = top_sold[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
        display_sold.index += 1
        st.dataframe(display_sold, column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open"), "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")}, use_container_width=True)

    if not missed_df.empty:
        st.write("") # Spacer
        st.subheader("👀 Missed Opportunities (Detail)")
        display_missed = missed_df[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
        display_missed.index += 1
        st.dataframe(display_missed, column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open"), "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")}, use_container_width=True)

    st.divider()
    st.markdown("### 📥 Export Reports")
    
    include_missed_in_pdf = st.checkbox("Include 'Missed Opportunities' in PDF Report?", value=True)
    
    ex1, ex2, ex3 = st.columns(3)
    with ex1:
        metrics_bundle = {'units_sold': m_units, 'rev_sold': m_rev, 'pipeline': m_pipe, 'ltb': f"{m_ltb:.1f}", 'new_ltb': f"{new_ltb:.1f}", 'used_ltb': f"{used_ltb:.1f}"}
        pdf_data = create_pdf_report(df, sold_df, metrics_bundle, missed_df, include_missed_in_pdf)
        st.download_button("📥 Download PDF Summary", data=pdf_data, file_name=f"{st.session_state.current_report_id}_Summary.pdf", mime="application/pdf")
    with ex2:
        st.download_button("📥 Download Sold List (CSV)", sold_df[['Vehicle Name', 'VIN', 'Page Url', 'Attributed Unique Visitors']].to_csv(index=False), f"{st.session_state.current_report_id}_Sold.csv", "text/csv")
    with ex3:
        st.download_button("📥 Download Full Analysis (CSV)", df.to_csv(index=False), f"{st.session_state.current_report_id}_Full_Analysis.csv", "text/csv")

    st.divider()
    with st.expander("ℹ️ Glossary & Guide: How to read this report"):
        st.markdown("""
        ### **Definitions & Insights**
        
        **1. Units Sold**
        The total count of vehicles that were identified as "Sold" (removed from inventory) *after* receiving attributed traffic from our campaign. This confirms that the audience we drove to the site was actively shopping for cars that moved off the lot.
        
        **2. Estimated Value (Rev & Pipeline)**
        A data-driven approximation of the inventory's dollar value. 
        * **New Cars:** Calculated using 2025/2026 Base MSRP for the specific model.
        * **Used Cars:** Calculated using the base MSRP depreciated by age (-15% Yr 1, -10% Yrs 2+).
        * *Note: This is a directional estimate to gauge "Total Pipeline Power" and does not account for specific trim levels, options, or dealer markups.*
        
        **3. Look-to-Book Ratio (New vs. Used)**
        The efficiency metric of your inventory. It measures the conversion velocity of the cars we drove traffic to.
        * *Formula:* `(Sold VDPs ÷ Total Active VDPs) × 100`
        * *Insight:* We split this by **New** and **Used** because they turn at different rates. A high "Used" LTB with a low "New" LTB often indicates a pricing or merchandising issue on the New car inventory.
        
        **4. Top Sold Units**
        The specific "Sold" vehicles that received the highest volume of exposure from our traffic. This highlights the specific models where our audience demand matched your sales success.
        
        **5. Missed Opportunities**
        **"The Watch List."** These are active vehicles receiving **above-average traffic** but haven't sold yet. 
        * *Why this matters:* You are paying for popularity, but not getting the sale. 
        * *Action Item:* Audit these VDPs immediately. Check for **missing photos**, **"Call for Price" buttons** (which lower conversion), or **pricing outliers**. These units are "High Interest" and likely just need a small nudge to sell.
        
        **6. Traffic Mix**
        A breakdown of where our audience lands and navigates.
        * **VDP (Vehicle Detail Page):** The "Money Page." High VDP traffic proves the audience is "Deep Funnel"—shopping for specific VINs rather than just browsing.
        * **Service/Parts:** Captures fixed-ops intent.
        * **New vs. Used:** Helps align your marketing spend with actual inventory interest.
        """)

else:
    st.info("👈 Upload a CSV in the sidebar to begin analysis.")
