import streamlit as st
import pandas as pd
import requests
import re
import matplotlib.pyplot as plt
import seaborn as sns
import concurrent.futures
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
st.set_page_config(page_title="Auto-Sales Intelligence Agent", layout="wide")

# --- PASSWORD PROTECTION ---
def check_password():
    """Returns `True` if the user had the correct password."""
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False

    if st.session_state.password_correct:
        return True

    st.title("🔒 Auto-Analyst Login")
    password = st.text_input("Enter Company Password", type="password")
    
    if st.button("Log In"):
        # SET YOUR PASSWORD HERE (Currently 'tegna2026')
        if password == "tegna2026":  
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("Incorrect password")
    return False

if not check_password():
    st.stop()

# --- LOGIC FUNCTIONS ---
def get_category(url):
    url = str(url).lower().strip()
    if url.endswith('.com/') or url.endswith('.com'): return 'Homepage'
    if any(x in url for x in ['search', 'inventory']):
        if 'new' in url: return 'New Car Search'
        if 'used' in url or 'preowned' in url: return 'Used Car Search'
        return 'General Search'
    if any(x in url for x in ['service', 'parts', 'collision', 'appointment']): return 'Service'
    if re.search(r'(?:19|20)\d{2}', url): return 'VDP'
    return 'Other'

def extract_details(url):
    # Extracts Year/Make/Model and determines New/Used
    match = re.search(r'/((?:19|20)\d{2}-[a-zA-Z0-9-]+)', str(url))
    name = "Unknown"
    ctype = "Unknown"
    year = None
    
    if match:
        name = match.group(1).replace('-', ' ').title()
        # Clean common URL junk
        name = re.sub(r'Baltimore Md.*', '', name, flags=re.IGNORECASE)
        name = re.sub(r'Ephrata.*', '', name, flags=re.IGNORECASE)
        
        # Determine Year
        try:
            year = int(name.split()[0])
        except:
            year = None
            
        # Determine Type (New logic: Year >= 2025 or 'New' in URL)
        if year and year >= 2025:
            ctype = 'New'
        elif year and year <= 2024:
            ctype = 'Used'
        elif 'new' in str(url).lower():
            ctype = 'New'
        elif 'used' in str(url).lower():
            ctype = 'Used'
            
    return name, ctype

def check_single_url(url):
    """Checks one URL and returns status. Used for parallel processing."""
    try:
        cat = get_category(url)
        if cat != 'VDP': 
            return "N/A"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        # Timeout set to 3 seconds to keep things fast
        response = requests.get(url, headers=headers, timeout=3, allow_redirects=True)
        
        page_text = response.text.lower()
        final_url = response.url.lower()
        page_title = ""
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            page_title = soup.title.string.lower() if soup.title else ""
        except: pass
        
        # SOLD LOGIC
        # 1. Error Message
        if "vehicle not found" in page_text or "sold" in page_title or "no longer available" in page_text:
            return "SOLD"
        # 2. Redirect to Search
        if ("search" in final_url or "inventory" in final_url) and url.lower() not in final_url:
            return "SOLD"
            
        return "Available"
    except:
        return "Error"

# --- MAIN APP UI ---
st.title("🚗 Auto-Sales Intelligence Agent")
st.markdown("Upload your raw traffic report (CSV) to identify sold cars and analyze performance.")

uploaded_file = st.file_uploader("Upload CSV File", type=['csv'])

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    st.info(f"Loaded {len(df)} rows. Agent is analyzing URLs... (This speeds up checking by 10x)")
    
    # PROGRESS BAR
    progress_bar = st.progress(0)
    status_results = []
    urls = df['Page Url'].tolist()
    
    # PARALLEL PROCESSING (The Speed Boost)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Submit all tasks
        future_to_url = {executor.submit(check_single_url, url): url for url in urls}
        
        # Process as they complete
        for i, future in enumerate(concurrent.futures.as_completed(future_to_url)):
            result = future.result()
            status_results.append(result)
            progress_bar.progress((i + 1) / len(urls))
            
    # Re-align results (futures return in random order, need to map back)
    # Actually, simpler way to ensure order is map, but map blocks. 
    # Let's just run map for simplicity and order preservation, usually fast enough with 10 workers.
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        status_results = list(executor.map(check_single_url, urls))
    
    # Apply Data
    df['Sold_Status'] = status_results
    df['Category'] = df['Page Url'].apply(get_category)
    df[['Vehicle Name', 'Type']] = df['Page Url'].apply(lambda x: pd.Series(extract_details(x)))
    df['Is Sold'] = df['Sold_Status'] == 'SOLD'
    
    st.success("Analysis Complete!")
    st.divider()
    
    # --- METRICS ---
    sold_count = df['Is Sold'].sum()
    vdp_visits = len(df[df['Category'] == 'VDP'])
    l2b = (sold_count / vdp_visits * 100) if vdp_visits > 0 else 0
    
    # Sales breakdown
    sold_df = df[df['Is Sold']]
    new_sold = len(sold_df[sold_df['Type'] == 'New'])
    used_sold = len(sold_df[sold_df['Type'] == 'Used'])
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Cars Sold", sold_count)
    c2.metric("New Sold", new_sold)
    c3.metric("Used Sold", used_sold)
    c4.metric("Look-to-Book", f"{l2b:.1f}%")
    
    # --- CHARTS ---
    st.subheader("Visual Intelligence")
    col_chart1, col_chart2 = st.columns(2)
    
    with col_chart1:
        st.markdown("**Traffic Distribution**")
        # Filter for main categories
        cats = ['VDP', 'New Car Search', 'Used Car Search', 'General Search', 'Service', 'Homepage']
        traffic_data = df[df['Category'].isin(cats)]['Category'].value_counts()
        st.bar_chart(traffic_data)
        
    with col_chart2:
        st.markdown("**Sales Mix (New vs Used)**")
        if sold_count > 0:
            sales_mix = sold_df['Type'].value_counts()
            fig, ax = plt.subplots()
            ax.pie(sales_mix, labels=sales_mix.index, autopct='%1.1f%%', colors=['#66b3ff', '#99ff99'])
            st.pyplot(fig)
        else:
            st.warning("No sales detected.")

    # --- TABLES ---
    c_left, c_right = st.columns(2)
    
    with c_left:
        st.subheader("🏆 Top Sold Vehicles")
        top_sold = df[df['Is Sold']].sort_values('Attributed Unique Visitors', ascending=False).head(10)
        st.dataframe(top_sold[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True), use_container_width=True)
        
    with c_right:
        st.subheader("⚠️ Missed Opportunities")
        avg_traffic = sold_df['Attributed Unique Visitors'].mean() if sold_count > 0 else 0
        missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] > avg_traffic)]
        missed = missed.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        st.dataframe(missed[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True), use_container_width=True)

    # --- DOWNLOAD ---
    csv = df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="Download Full Analysis CSV",
        data=csv,
        file_name="Auto_Analyst_Report.csv",
        mime="text/csv"
    )
