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

# --- PASSWORD PROTECTION ---
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

# --- THE ENGINE ---

def extract_year_universal(url):
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def check_url_status(url, session):
    """Refined surgical logic to prevent false 'Sold' flags."""
    year = extract_year_universal(url)
    if not year: return "N/A"
    
    try:
        # Fast 4-second timeout for speed
        response = session.get(url, timeout=4, allow_redirects=True)
        final_url = response.url.lower()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        page_text = response.text.lower()

        # 1. Hard Redirect (Landed on a search results page)
        search_indicators = ['search', 'inventory', 'results', '.aspx', 'all-inventory', 'searchall']
        if any(ind in final_url for ind in search_indicators) and url.lower().split('?')[0] not in final_url:
            return "SOLD (Redirected)"

        # 2. Content Flags
        if any(p in page_text for p in ["vehicle not found", "no longer available", "sorry", "similar vehicles"]):
            return "SOLD (Content)"
        
        # 3. Title Mismatch (Only if title is generic)
        if year not in page_title and any(x in page_title for x in ['inventory', 'search', 'results', 'used cars', 'new cars']):
            return "SOLD (Title Soft-Redirect)"

        return "Available"
    except:
        return "Available" # Default to Available if link is dead/slow

def clean_vehicle_name(url):
    """Strips VINs, IDs, and Dealer Locations for clean reporting."""
    url_path = urlparse(url).path
    match = re.search(r'((?:19|20)\d{2}.*)', url_path)
    if match:
        raw = match.group(1).replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '')
        # Delete any word with a mix of letters/numbers or longer than 10 chars (The VIN Killer)
        tokens = raw.split()
        clean_tokens = []
        for t in tokens:
            if len(t) > 10 and any(c.isdigit() for c in t): continue
            if t.title() in ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Twin', 'Pine', 'Heritage', 'Used', 'New']: continue
            clean_tokens.append(t)
        return " ".join(clean_tokens).title().strip()
    return "Unknown Vehicle"

@st.cache_data(show_spinner=False)
def perform_analysis(df_input):
    urls = df_input['Page Url'].tolist()
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'})
    
    # 15 workers for reliability and speed
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        status_results = list(executor.map(lambda u: check_url_status(u, session), urls))
    
    df_input['Sold_Status'] = status_results
    df_input['Is Sold'] = df_input['Sold_Status'].str.startswith('SOLD')
    df_input['Vehicle Name'] = df_input['Page Url'].apply(clean_vehicle_name)
    df_input['Category'] = df_input['Page Url'].apply(lambda u: 'Homepage' if str(u).endswith('.com/') else ('Search/Inventory' if 'search' in str(u).lower() or 'inventory' in str(u).lower() else ('VDP' if extract_year_universal(u) else 'Other')))
    df_input['Type'] = df_input['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', str(x)) else 'Used')
    return df_input

# --- APP UI ---
st.title("🚗 Auto-Sales Intelligence Agent")

uploaded_file = st.file_uploader("Upload Traffic CSV", type=['csv'])

if uploaded_file is not None:
    raw_df = pd.read_csv(uploaded_file)
    
    with st.spinner("Analyzing URLs... please wait ~30 seconds."):
        df = perform_analysis(raw_df)

    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    
    # Metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Traffic Mix**")
        traffic = df.groupby('Category')['Attributed Unique Visitors'].sum().sort_values(ascending=False)
        st.bar_chart(traffic)
    with col2:
        st.markdown("**Sales Mix**")
        if not sold_df.empty:
            fig, ax = plt.subplots()
            sold_df['Type'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#4F81BD', '#C0504D'])
            ax.set_ylabel('')
            st.pyplot(fig)

    # Tables
    t1, t2 = st.columns(2)
    with t1:
        st.subheader("🏆 Top Sold Units")
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        top_sold.index += 1
        st.dataframe(top_sold, use_container_width=True)
    with t2:
        st.subheader("⚠️ High Interest / Unsold")
        avg_v = sold_df['Attributed Unique Visitors'].mean() if not sold_df.empty else 0
        missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_v)].sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        missed.index += 1
        st.dataframe(missed, use_container_width=True)

    st.divider()
    st.download_button("📥 Download Analysis (CSV)", df.to_csv(index=False), "Sales_Analysis.csv", "text/csv")
