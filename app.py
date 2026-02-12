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
    """The Universal Sold Check: Focuses on URL Behavior & Title Pivots."""
    year = get_year(url)
    if not year: return "N/A"
    
    try:
        response = session.get(url, timeout=5, allow_redirects=True)
        final_url = response.url.lower()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        
        # Logic 1: Landed on a search/inventory page
        search_path = ['search', 'inventory', 'results', '.aspx', 'all-inventory']
        if any(x in final_url for x in search_path) and url.lower().split('?')[0] not in final_url:
            return "SOLD (Redirected)"

        # Logic 2: Page Title contains 'Sold' or 'Not Found'
        if any(x in page_title for x in ['sold', 'not found', 'no longer available']):
            return "SOLD (Status)"
            
        # Logic 3: Title Pivot (Year in URL is missing from Title)
        # This catches "Soft Redirects" where they send you to a search page but keep the URL
        if year not in page_title and any(x in page_title for x in ['inventory', 'search', 'cars for sale']):
            return "SOLD (Title Pivot)"

        return "Available"
    except:
        return "Available"

def clean_name_universal(url):
    """Universal Vehicle Name Extractor: Year + Next 3 Words."""
    url_path = urlparse(url).path
    match = re.search(r'((?:19|20)\d{2}.*)', url_path)
    if match:
        raw = match.group(1).replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '')
        tokens = raw.split()
        # Kill the VIN/Codes (Words > 10 chars or containing many numbers)
        clean = [t for t in tokens if not (len(t) > 10 and any(c.isdigit() for c in t))]
        # Kill common Dealer/Platform junk
        junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Twin', 'Pine', 'Heritage', 'Used', 'New', 'Wholesale', 'Inventory']
        final = [t for t in clean if t.title() not in junk]
        return " ".join(final).title().strip()
    return "Unknown Vehicle"

@st.cache_data(show_spinner=False)
def analyze_data(df_input):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'})
    urls = df_input['Page Url'].tolist()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        df_input['Sold_Status'] = list(executor.map(lambda u: check_universal_status(u, session), urls))
    
    df_input['Is Sold'] = df_input['Sold_Status'].str.startswith('SOLD')
    df_input['Vehicle Name'] = df_input['Page Url'].apply(clean_name_universal)
    df_input['Type'] = df_input['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', str(x)) else 'Used')
    
    # Categorization
    def categorize(u):
        u = str(u).lower()
        if u.endswith('.com/') or u.endswith('.com'): return 'Homepage'
        if any(x in u for x in ['search', 'inventory']): return 'Search Results'
        if get_year(u): return 'VDP'
        return 'Other'
    df_input['Category'] = df_input['Page Url'].apply(categorize)
    return df_input

# --- UI DASHBOARD ---
st.title("🚗 Universal Sales Intelligence Agent")

uploaded_file = st.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

if uploaded_file is not None:
    df_raw = pd.read_csv(uploaded_file)
    with st.spinner("Analyzing live inventory..."):
        df = analyze_data(df_raw)

    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Traffic Mix (Total Visitors)**")
        st.bar_chart(df.groupby('Category')['Attributed Unique Visitors'].sum())
    with col2:
        st.markdown("**Sales Mix (New vs Used)**")
        if not sold_df.empty:
            fig, ax = plt.subplots()
            sold_df['Type'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#4F81BD', '#C0504D'])
            ax.set_ylabel('')
            st.pyplot(fig)

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
