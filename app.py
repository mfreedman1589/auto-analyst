import streamlit as st
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
import concurrent.futures
import matplotlib.pyplot as plt
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Auto-Sales Intelligence Agent", layout="wide")

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

# --- THE VALUATION ENGINE (v3.0 - Model Specific) ---

def estimate_value(row):
    """
    Tiered Valuation Algorithm:
    1. Checks for specific MODEL match (e.g. 'F-150' vs 'Focus').
    2. Falls back to BRAND match if model not found.
    3. Applies Age Depreciation.
    """
    name = str(row['Vehicle Name']).lower()
    
    # --- LEVEL 1: MODEL SPECIFIC BASELINES (2025/26 Avg MSRP) ---
    MODEL_PRICES = {
        # FORD
        'f-150': 60000, 'f 150': 60000, 'f150': 60000,
        'f-250': 75000, 'f 250': 75000, 'super duty': 80000,
        'ranger': 40000, 'maverick': 28000, 'bronco': 50000,
        'explorer': 48000, 'expedition': 70000, 'edge': 40000,
        'escape': 32000, 'mustang': 45000, 'mach-e': 55000,
        'focus': 22000, 'fusion': 26000, 'fiesta': 18000, 'taurus': 28000,
        
        # CHEVROLET / GMC
        'silverado': 55000, 'sierra': 62000, 'colorado': 38000, 'canyon': 42000,
        'tahoe': 72000, 'suburban': 78000, 'yukon': 78000, 'escalade': 110000,
        'corvette': 90000, 'camaro': 42000, 'blazer': 40000, 'equinox': 32000,
        'traverse': 42000, 'malibu': 28000, 'trax': 24000, 'trailblazer': 28000,
        'envision': 40000, 'enclave': 55000, 'encore': 28000,
        
        # JEEP / RAM / DODGE
        'grand cherokee': 55000, 'wagoneer': 80000, 'wrangler': 48000,
        'gladiator': 50000, 'ram 1500': 60000, 'ram 2500': 70000,
        'durango': 50000, 'charger': 40000, 'challenger': 45000,
        'pacifica': 45000, 'compass': 30000, 'renegade': 28000, 'hornet': 35000,
        
        # TOYOTA / HONDA
        'tundra': 58000, 'tacoma': 42000, '4runner': 50000, 'sequoia': 75000,
        'land cruiser': 85000, 'highlander': 45000, 'rav4': 35000, 'camry': 30000,
        'corolla': 25000, 'prius': 32000, 'sienna': 48000,
        'pilot': 48000, 'passport': 42000, 'cr-v': 36000, 'odyssey': 45000,
        'civic': 28000, 'accord': 32000, 'ridgeline': 42000,
        
        # LUXURY / OTHER
        '3 series': 50000, '5 series': 65000, 'x3': 55000, 'x5': 75000, 'x7': 90000,
        'c-class': 52000, 'e-class': 70000, 'gle': 75000, 'gls': 95000, 'g-class': 160000,
        'rx': 58000, 'nx': 48000, 'es': 48000, 'gx': 70000, 'lx': 100000,
        'gv80': 70000, 'gv70': 55000, 'g70': 45000,
        'telluride': 48000, 'palisade': 50000, 'ioniq': 45000, 'ev6': 50000,
        'lyriq': 65000, 'optiq': 54000, 'vistiq': 78000, 'ct4': 40000, 'ct5': 50000, 'xt4': 40000, 'xt5': 50000, 'xt6': 60000
    }
    
    # --- LEVEL 2: BRAND FALLBACKS ---
    BRAND_DEFAULTS = {
        'cadillac': 65000, 'mercedes': 70000, 'bmw': 65000, 'audi': 60000,
        'lexus': 60000, 'lincoln': 65000, 'land rover': 85000, 'porsche': 100000,
        'ford': 45000, 'chevrolet': 40000, 'gmc': 55000, 'ram': 55000,
        'jeep': 45000, 'dodge': 40000, 'toyota': 38000, 'honda': 35000,
        'nissan': 32000, 'hyundai': 30000, 'kia': 30000, 'subaru': 32000,
        'volkswagen': 32000, 'mazda': 32000, 'volvo': 55000, 'acura': 50000
    }

    # 1. Determine Baseline Price
    baseline = 35000 # Ultimate fallback
    found_model = False
    
    # Sort models by length (longest first) to catch "Grand Cherokee" before "Cherokee"
    sorted_models = sorted(MODEL_PRICES.keys(), key=len, reverse=True)
    
    for model in sorted_models:
        # Use word boundaries to avoid partial matches (e.g. 'es' in 'Series')
        if re.search(r'\b' + re.escape(model) + r'\b', name):
            baseline = MODEL_PRICES[model]
            found_model = True
            break
            
    if not found_model:
        for brand, price in BRAND_DEFAULTS.items():
            if brand in name:
                baseline = price
                break

    # 2. Extract Year & Apply Depreciation
    year_match = re.search(r'\d{4}', name)
    year = int(year_match.group(0)) if year_match else 2025
    
    current_year = datetime.datetime.now().year + 1 # Assume 2026 market context
    age = current_year - year
    
    if age <= 0:
        value = baseline
    else:
        # Curve: -15% Yr 1, -10% Yrs 2+
        value = baseline * 0.85
        for _ in range(age - 1):
            value = value * 0.90
            
    return int(value)

def get_price_tier(price):
    if price < 30000: return "Budget (<$30k)"
    if price < 60000: return "Core ($30k-$60k)"
    return "Premium ($60k+)"

# --- THE SCANNING ENGINE ---

def get_year(url):
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def extract_vin(url):
    match = re.search(r'([a-zA-Z0-9]{10,})(?:\.htm|\.html|$|\?)', str(url))
    return match.group(1).upper() if match else "N/A"

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
    if any(x in u for x in ['search', 'inventory']):
        if 'new' in u: return 'New Car Search'
        if 'used' in u or 'preowned' in u: return 'Used Car Search'
        return 'General Search'
    if any(x in u for x in ['service', 'parts', 'collision', 'appointment', 'maintenance']): return 'Service'
    if get_year(u): return 'VDP'
    return 'Other'

def check_universal_status(url, session):
    year = get_year(url)
    if not year: return "N/A"
    try:
        response = session.get(url, timeout=3, allow_redirects=True)
        final_url = response.url.lower().rstrip('/')
        orig_url = url.lower().split('?')[0].rstrip('/')
        
        search_path = ['search', 'inventory', 'results', '.aspx', 'all-inventory', 'index.htm']
        if any(x in final_url for x in search_path) and orig_url not in final_url:
            return "SOLD (Hard Redirect)"

        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        
        if year not in page_title and len(page_title) > 5:
            return "SOLD (Soft Redirect)"

        page_text = response.text.lower()
        if "vehicle not found" in page_text or "no longer available" in page_text:
            return "SOLD (Content)"

        return "Available"
    except:
        return "Available"

# --- UI DASHBOARD ---
st.title("🚗 Auto-Sales Intelligence Agent v2.2")
uploaded_file = st.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

if uploaded_file is not None:
    df_raw = pd.read_csv(uploaded_file)
    
    if st.button("🚀 Run Diagnostic Analysis"):
        # 1. Pre-process
        df_raw['Category'] = df_raw['Page Url'].apply(categorize)
        vdp_urls = df_raw[df_raw['Category'] == 'VDP']['Page Url'].tolist()
        
        st.info(f"Scanning {len(vdp_urls)} Vehicles. Applying AI Valuation...")
        progress_bar = st.progress(0)
        
        # 2. Session
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'})
        
        # 3. Scan
        vdp_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            future_to_url = {executor.submit(check_universal_status, url, session): url for url in vdp_urls}
            for i, future in enumerate(concurrent.futures.as_completed(future_to_url)):
                url = future_to_url[future]
                vdp_results[url] = future.result()
                progress_bar.progress((i + 1) / len(vdp_urls))

        # 4. Process Data
        df_raw['Sold_Status'] = df_raw['Page Url'].map(vdp_results).fillna('N/A')
        df = df_raw.copy()
        df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
        df['Vehicle Name'] = df['Page Url'].apply(clean_name_universal)
        df['VIN'] = df['Page Url'].apply(extract_vin)
        df['Type'] = df['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', str(x)) else 'Used')
        
        # 5. Apply Model-Specific Valuation
        df['Est. Value'] = df.apply(estimate_value, axis=1)
        df['Price Tier'] = df['Est. Value'].apply(get_price_tier)
        
        # --- METRICS ---
        sold_df = df[df['Is Sold']]
        vdp_df = df[df['Category'] == 'VDP']
        
        total_rev_sold = sold_df['Est. Value'].sum()
        pipeline_val = vdp_df['Est. Value'].sum()
        
        st.markdown("### 📊 Executive Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Units Sold", len(sold_df))
        m2.metric("Est. Revenue Sold", f"${total_rev_sold:,.0f}")
        m3.metric("Pipeline Value (Active)", f"${pipeline_val:,.0f}")
        m4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

        st.divider()
        
        # --- CHARTS ---
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**Traffic Mix**")
            st.bar_chart(df.groupby('Category')['Attributed Unique Visitors'].sum())
        with c2:
            st.markdown("**Sales Mix (New vs Used)**")
            if not sold_df.empty:
                fig, ax = plt.subplots()
                sold_df['Type'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#4F81BD', '#C0504D'])
                ax.set_ylabel('')
                st.pyplot(fig)
        with c3:
            st.markdown("**Sold Value Tiers**")
            if not sold_df.empty:
                fig, ax = plt.subplots()
                sold_df['Price Tier'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#9BBB59', '#4F81BD', '#8064A2'])
                ax.set_ylabel('')
                st.pyplot(fig)

        # --- TABLES ---
        t1, t2 = st.columns(2)
        
        with t1:
            st.subheader("🏆 Top Sold Units")
            if not sold_df.empty:
                top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
                display_sold = top_sold[['Vehicle Name', 'Type', 'Est. Value', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
                display_sold.index += 1
                st.dataframe(
                    display_sold,
                    column_config={
                        "Page Url": st.column_config.LinkColumn("Link", display_text="Open"),
                        "Est. Value": st.column_config.NumberColumn("Est. Price", format="$%d"),
                        "Attributed Unique Visitors": st.column_config.NumberColumn("Visits")
                    }, use_container_width=True
                )
            else:
                st.info("No sales identified.")

        with t2:
            st.subheader("⚠️ Missed Opportunities")
            if not sold_df.empty:
                avg_v = sold_df['Attributed Unique Visitors'].mean()
                missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_v)]
                missed = missed.sort_values('Attributed Unique Visitors', ascending=False).head(10)
                display_missed = missed[['Vehicle Name', 'Type', 'Est. Value', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
                display_missed.index += 1
                st.dataframe(
                    display_missed,
                    column_config={
                        "Page Url": st.column_config.LinkColumn("Link", display_text="Open"),
                        "Est. Value": st.column_config.NumberColumn("Est. Price", format="$%d"),
                        "Attributed Unique Visitors": st.column_config.NumberColumn("Visits")
                    }, use_container_width=True
                )
            else:
                st.info("Data pending.")

        # --- EXPORT ---
        st.divider()
        ex1, ex2 = st.columns(2)
        with ex1:
            st.download_button("📥 Download Sold Report (CSV)", 
                               sold_df.to_csv(index=False), 
                               "Sold_Vehicles_Report.csv", "text/csv")
        with ex2:
            st.download_button("📥 Download Full Analysis (CSV)", 
                               df.to_csv(index=False), 
                               "Full_Market_Analysis.csv", "text/csv")
