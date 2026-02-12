import streamlit as st
import pandas as pd
import requests
import re
import concurrent.futures
import matplotlib.pyplot as plt
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import io

# --- CONFIGURATION ---
st.set_page_config(page_title="Universal Sales Intelligence", layout="wide")

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

# --- REFINED UNIVERSAL LOGIC ---

def extract_car_info_universal(url):
    if not isinstance(url, str): return None, None
    url_clean = url.lower().strip()
    if any(x in url_clean.split('/')[-1] for x in ['search', 'inventory', 'all', 'index', '.aspx']):
        return None, None
    year_match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', url_clean)
    if not year_match: return None, None
    year = year_match.group(1)
    return year

def check_single_url(url):
    year = extract_car_info_universal(url)
    if not year: return "N/A"
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=5, allow_redirects=True)
        final_url = response.url.lower()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        page_text = response.text.lower()

        # 1. Platform Hard Redirect (Most Reliable)
        search_indicators = ['search', 'inventory', 'results', '.aspx', 'all-inventory']
        if any(ind in final_url for ind in search_indicators) and url.lower().split('?')[0] not in final_url:
            return "SOLD (Redirect)"

        # 2. Page Content Sold Indicators
        if any(p in page_text for p in ["vehicle not found", "no longer available", "sorry", "similar vehicles"]):
            return "SOLD (Content)"
        
        # 3. Soft Title Check (Only if title is very generic)
        if year not in page_title and any(x in page_title for x in ['inventory', 'search', 'results']):
            return "SOLD (Title Soft Redirect)"

        return "Available"
    except:
        return "Available (Link Busy)" # Assume available if site times out to prevent false sold counts

def clean_vehicle_name(url):
    """Strips VINs, locations, and codes to leave Year Make Model."""
    url_path = urlparse(url).path
    match = re.search(r'((?:19|20)\d{2}.*)', url_path)
    if match:
        name = match.group(1).replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '')
        # Remove VINs (Long strings of random letters/numbers)
        name = re.sub(r'[A-Z0-9]{10,}', '', name)
        # Remove common Dealer Locations
        name = re.sub(r'Baltimore|Ephrata|Maryland|Jeep|Chrysler|Ford', '', name, flags=re.IGNORECASE)
        # Clean double spaces
        name = re.sub(' +', ' ', name)
        return name.title().strip()
    return "Unknown Vehicle"

# --- UI ---
st.title("🚗 Universal Sales Intelligence Agent")
uploaded_file = st.file_uploader("Upload Raw Traffic CSV", type=['csv'])

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    st.info(f"Analyzing {len(df)} URLs using High-Speed 20-worker threading...")
    
    # Run Checks
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        status_results = list(executor.map(check_single_url, df['Page Url'].tolist()))
        
    df['Sold_Status'] = status_results
    df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
    df['Vehicle Name'] = df['Page Url'].apply(clean_vehicle_name)
    df['Type'] = df['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', x) else 'Used')
    
    # Categorize
    def get_cat(url):
        u = url.lower()
        if u.endswith('.com/') or u.endswith('.com'): return 'Homepage'
        if any(x in u for x in ['search', 'inventory']): return 'Search/Inventory'
        if re.search(r'(?:19|20)\d{2}', u): return 'VDP'
        return 'Other'
    df['Category'] = df['Page Url'].apply(get_cat)

    st.success("Analysis Complete!")
    st.divider()

    # --- METRICS ---
    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

    # --- VISUALS ---
    col_chart1, col_chart2 = st.columns(2)
    with col_chart1:
        st.markdown("**Traffic Mix (Visitors)**")
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
        st.subheader("⚠️ Missed Opportunities")
        avg_v = sold_df['Attributed Unique Visitors'].mean() if len(sold_df)>0 else 0
        missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_v)].sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        missed.index += 1
        st.dataframe(missed, use_container_width=True)

    # --- EXPORT ---
    st.divider()
    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        st.download_button("📥 Download Analysis (CSV)", df.to_csv(index=False), "Sales_Analysis.csv", "text/csv")
    with col_dl2:
        # Simplest PDF work-around for Streamlit (printing the current view)
        st.info("💡 To save as PDF: Press **Ctrl+P** (Windows) or **Cmd+P** (Mac) and select 'Save as PDF'.")
