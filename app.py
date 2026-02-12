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

# --- UNIVERSAL ENGINE ---

def extract_year(url):
    """Finds the 4-digit year anchor in any URL structure."""
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def check_single_url(url):
    """Surgical Sold Detection: Accuracy over Aggression."""
    year = extract_year(url)
    if not year: return "N/A"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        # Allow 6 seconds for slow dealer sites
        response = requests.get(url, headers=headers, timeout=6, allow_redirects=True)
        
        final_url = response.url.lower()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        page_text = response.text.lower()

        # 1. HARD REDIRECT: Did we land on a search page?
        search_indicators = ['search', 'inventory', 'results', '.aspx', 'all-inventory', 'searchall']
        if any(ind in final_url for ind in search_indicators) and url.lower().split('?')[0] not in final_url:
            return "SOLD (Redirected)"

        # 2. CLEAR CONTENT: Does the page say it's gone?
        sold_phrases = ["vehicle not found", "no longer available", "sorry", "similar vehicles"]
        if any(phrase in page_text for phrase in sold_phrases):
            return "SOLD (Content Flag)"
        
        # 3. CONSERVATIVE TITLE CHECK: Only if it's explicitly an inventory page
        if year not in page_title and any(x in page_title for x in ['inventory', 'results', 'search']):
            return "SOLD (Title Soft-Redirect)"

        return "Available"
    except:
        # If site is busy/timed out, we assume Available to prevent false sold counts
        return "Available"

def clean_vehicle_name(url):
    """Strips VINs, dealership names, and cities for a professional report."""
    url_path = urlparse(url).path
    match = re.search(r'((?:19|20)\d{2}.*)', url_path)
    if match:
        name = match.group(1).replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '')
        # Delete any word longer than 10 chars (likely a VIN or ID)
        tokens = [w for w in name.split() if not (len(w) > 10 and any(c.isdigit() for c in w))]
        # Delete dealer/location junk
        junk = ['Baltimore', 'Ephrata', 'Maryland', 'Jeep', 'Chrysler', 'Ford', 'Dodge', 'Ram', 'Heritage', 'Twin', 'Pine', 'Md', 'Wholesale']
        clean_tokens = [t for t in tokens if t.title() not in junk]
        return " ".join(clean_tokens).title().strip()
    return "Unknown Vehicle"

@st.cache_data(show_spinner=False)
def process_data(df_input):
    """Cached analysis to stop the spinning circle."""
    urls = df_input['Page Url'].tolist()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        status_results = list(executor.map(check_single_url, urls))
    
    df_input['Sold_Status'] = status_results
    df_input['Is Sold'] = df_input['Sold_Status'].str.startswith('SOLD')
    df_input['Vehicle Name'] = df_input['Page Url'].apply(clean_vehicle_name)
    df_input['Category'] = df_input['Page Url'].apply(lambda u: 'Homepage' if str(u).endswith('.com/') else ('Search/Inventory' if 'search' in str(u).lower() or 'inventory' in str(u).lower() else ('VDP' if extract_year(u) else 'Other')))
    df_input['Type'] = df_input['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', str(x)) else 'Used')
    return df_input

# --- UI ---
st.title("🚗 Universal Sales Intelligence Agent")

uploaded_file = st.file_uploader("Upload Traffic CSV", type=['csv'])

if uploaded_file is not None:
    raw_df = pd.read_csv(uploaded_file)
    
    with st.spinner("Agent is analyzing live URLs... (takes ~20 seconds)"):
        df = process_data(raw_df)

    # --- DASHBOARD ---
    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Cars Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

    # --- VISUALS ---
    st.divider()
    col_chart1, col_chart2 = st.columns(2)
    with col_chart1:
        st.markdown("**Traffic Mix (Total Visitors)**")
        traffic = df.groupby('Category')['Attributed Unique Visitors'].sum().sort_values(ascending=False)
        st.bar_chart(traffic)
    with col_chart2:
        st.markdown("**Sales Mix**")
        if len(sold_df) > 0:
            mix = sold_df['Type'].value_counts()
            fig, ax = plt.subplots()
            ax.pie(mix, labels=mix.index, autopct='%1.1f%%', colors=['#4F81BD', '#C0504D'])
            st.pyplot(fig)

    # --- TABLES ---
    t1, t2 = st.columns(2)
    with t1:
        st.subheader("🏆 Top Sold Units")
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        top_sold.index += 1
        st.dataframe(top_sold, use_container_width=True)
    with t2:
        st.subheader("⚠️ High Interest / Unsold")
        avg_v = sold_df['Attributed Unique Visitors'].mean() if len(sold_df)>0 else 0
        missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_v)].sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        missed.index += 1
        st.dataframe(missed, use_container_width=True)

    st.divider()
    st.download_button("📥 Download Full Analysis (CSV)", df.to_csv(index=False), "Sales_Analysis.csv", "text/csv")
