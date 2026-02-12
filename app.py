import streamlit as st
import pandas as pd
import requests
import re
import concurrent.futures
import matplotlib.pyplot as plt
from urllib.parse import urlparse
from bs4 import BeautifulSoup

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

# --- THE UNIVERSAL ENGINE ---

def get_year(url):
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def check_universal_status(url, session):
    """Universal Sold Check: Focuses on URL Behavior & Title Pivots."""
    year = get_year(url)
    if not year: return "N/A"
    
    try:
        # Standardize timeout and headers for all platforms
        response = session.get(url, timeout=6, allow_redirects=True)
        final_url = response.url.lower().rstrip('/')
        orig_url = url.lower().split('?')[0].rstrip('/')
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        page_text = response.text.lower()
        
        # Logic 1: Landed on a search/inventory page (Redirect)
        search_path = ['search', 'inventory', 'results', '.aspx', 'all-inventory', 'index.htm']
        if any(x in final_url for x in search_path) and orig_url not in final_url:
            return "SOLD (Redirected)"

        # Logic 2: Hard Content Error
        if any(x in page_text for x in ["vehicle not found", "no longer available", "this vehicle has been sold"]):
            return "SOLD (Content)"
            
        # Logic 3: Title Mismatch (The 'Heritage' Fix)
        # If the URL is for a 2024 but the Page Title doesn't have 2024, it's sold.
        if year not in page_title and len(page_title) > 5:
            return "SOLD (Title Mismatch)"

        return "Available"
    except:
        return "Available" # If link is slow, assume available to prevent inflation

def clean_name_universal(url):
    """Universal Vehicle Name Extractor: Year + Model (No VINs/Codes)."""
    year = get_year(url)
    if not year: return "Unknown Vehicle"
    
    # Take everything after the year in the URL
    parts = url.split(year)
    rest = parts[-1].replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '').replace('.html', '')
    
    # Filter out VINs (long strings with digits) and dealer cities
    tokens = rest.split()
    junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Heritage', 'Twin', 'Pine', 'Wholesale', 'New', 'Used', 'Preowned', 'Inventory']
    
    clean_tokens = []
    for t in tokens:
        # Skip VINs (longer than 10 chars with a number)
        if len(t) > 10 and any(c.isdigit() for c in t): continue
        # Skip Dealer Junk
        if t.title() in junk: continue
        clean_tokens.append(t)
        
    name = f"{year} {' '.join(clean_tokens)}"
    return name.title().strip()

@st.cache_data(show_spinner=False)
def analyze_data(df_input):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'})
    urls = df_input['Page Url'].tolist()
    
    # 15 workers for stability across all platforms
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        df_input['Sold_Status'] = list(executor.map(lambda u: check_universal_status(u, session), urls))
    
    df_input['Is Sold'] = df_input['Sold_Status'].str.startswith('SOLD')
    df_input['Vehicle Name'] = df_input['Page Url'].apply(clean_name_universal)
    
    # Categorization based on exact rules
    def categorize(u):
        u = str(u).lower()
        if u.endswith('.com/') or u.endswith('.com'): return 'Homepage'
        if any(x in u for x in ['search', 'inventory']):
            if 'new' in u: return 'New Car Search'
            if 'used' in u or 'preowned' in u: return 'Used Car Search'
            return 'General Search'
        if any(x in u for x in ['service', 'parts', 'collision', 'appointment']): return 'Service'
        if get_year(u): return 'VDP'
        return 'Other'
    
    df_input['Category'] = df_input['Page Url'].apply(categorize)
    df_input['Type'] = df_input['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', str(x)) else 'Used')
    return df_input

# --- UI DASHBOARD ---
st.title("🚗 Auto-Sales Intelligence Agent")

uploaded_file = st.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

if uploaded_file is not None:
    df_raw = pd.read_csv(uploaded_file)
    with st.spinner("Analyzing live inventory... this takes ~40 seconds."):
        df = analyze_data(df_raw)

    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    
    # Section 1: Metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Cars Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

    st.divider()
    # Section 2: Charts
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Traffic Mix (Total Visitors)**")
        # Ensure categories are sorted for clean presentation
        traffic = df.groupby('Category')['Attributed Unique Visitors'].sum().sort_values(ascending=False)
        st.bar_chart(traffic)
    with col2:
        st.markdown("**Sales Mix (New vs Used)**")
        if not sold_df.empty:
            fig, ax = plt.subplots()
            sold_df['Type'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#4F81BD', '#C0504D'])
            ax.set_ylabel('')
            st.pyplot(fig)

    # Section 3: Tables
    t1, t2 = st.columns(2)
    with t1:
        st.subheader("🏆 Top Sold Units")
        ts = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        ts.index += 1
        st.dataframe(ts, use_container_width=True)
    with t2:
        st.subheader("⚠️ Missed Opportunities")
        avg = sold_df['Attributed Unique Visitors'].mean() if not sold_df.empty else 0
        mo = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg)].sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        mo.index += 1
        st.dataframe(mo, use_container_width=True)

    st.divider()
    st.download_button("📥 Download Analysis CSV", df.to_csv(index=False), "Sales_Analysis.csv", "text/csv")
