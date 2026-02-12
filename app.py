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
    """Returns `True` if the user had the correct password."""
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False

    if st.session_state.password_correct:
        return True

    st.title("🔒 Auto-Analyst Login")
    password = st.text_input("Enter Company Password", type="password")
    
    if st.button("Log In"):
        if password == "tegna2026":  # <--- PASSWORD
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("Incorrect password")
    return False

if not check_password():
    st.stop()

# --- UNIVERSAL PARSING LOGIC ---
def extract_car_info_universal(url):
    """
    Attempts to extract Year, Make from ANY dealership URL.
    """
    if not isinstance(url, str):
        return None, None
        
    url = url.lower().strip()
    
    # Skip search/index pages
    if "search" in url or "index" in url or "inventory" in url.split('/')[-1]:
        return None, None

    # Parse path to find the segment with the car info
    parsed = urlparse(url)
    path_segments = parsed.path.split('/')
    
    car_segment = None
    for segment in path_segments:
        # Check if segment starts with 4 digits (19xx or 20xx)
        if re.match(r'^(19|20)\d{2}', segment):
            car_segment = segment
            break
            
    if not car_segment:
        return None, None
        
    # Extract Year and Make
    tokens = car_segment.split('-')
    if len(tokens) >= 2:
        year = tokens[0]
        make = tokens[1]
        return year, make
        
    return None, None

def check_single_url(url):
    """
    Checks status using the Universal Logic (Title Mismatch, Redirects, Error Text).
    """
    # 1. Parse URL to get expected car info
    year, make = extract_car_info_universal(url)
    
    if not year:
        return "N/A"

    try:
        # 2. Request the page
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        # Timeout set to 5 seconds for balance of speed/reliability
        response = requests.get(url, headers=headers, timeout=5, allow_redirects=True)
        
        # 3. Analyze Result
        final_url = response.url.lower()
        page_title = ""
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            page_title = soup.title.string.strip().lower() if soup.title else ""
        except:
            pass
        
        page_text = response.text.lower()

        # --- SOLD INDICATORS ---
        
        # A. Explicit "Sold" or "Not Found" text
        if "vehicle not found" in page_text or "no longer available" in page_text or "sold" in page_title:
            return "SOLD (Error Message)"

        # B. Redirected to a Search/Index Page
        if ("search" in final_url or "inventory" in final_url) and url.lower() not in final_url:
             return "SOLD (Redirected)"

        # C. Title Mismatch (The "Soft Redirect" Check)
        if year and str(year) not in page_title:
            if len(page_title) > 3:
                return "SOLD (Title Mismatch)"

        return "Available"

    except Exception as e:
        return "Error"

# --- HELPER: Display Formatting ---
def get_display_category(url):
    url = str(url).lower().strip()
    if url.endswith('.com/') or url.endswith('.com'): return 'Homepage'
    if any(x in url for x in ['search', 'inventory']):
        if 'new' in url: return 'New Car Search'
        if 'used' in url or 'preowned' in url: return 'Used Car Search'
        return 'General Search'
    if any(x in url for x in ['service', 'parts', 'collision', 'appointment']): return 'Service'
    if re.search(r'(?:19|20)\d{2}', url): return 'VDP'
    return 'Other'

def extract_display_details(url):
    """Clean extraction for the Dataframe display (Year/Make/Model + Type)"""
    match = re.search(r'/((?:19|20)\d{2}-[a-zA-Z0-9-]+)', str(url))
    name = "Unknown"
    ctype = "Unknown"
    
    if match:
        name = match.group(1).replace('-', ' ').title()
        # Clean common URL junk
        name = re.sub(r'Baltimore Md.*', '', name, flags=re.IGNORECASE)
        name = re.sub(r'Ephrata.*', '', name, flags=re.IGNORECASE)
        name = name.strip()
        
        # Determine Type
        try:
            year_str = name.split()[0]
            year = int(year_str)
            if year >= 2025:
                ctype = 'New'
            elif year <= 2024:
                ctype = 'Used'
        except:
            if 'new' in str(url).lower(): ctype = 'New'
            elif 'used' in str(url).lower(): ctype = 'Used'
            
    return name, ctype

# --- MAIN APP UI ---
st.title("🚗 Auto-Sales Intelligence Agent")
st.markdown("Upload your raw traffic report (CSV) to identify sold cars and analyze performance.")

uploaded_file = st.file_uploader("Upload CSV File", type=['csv'])

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    st.info(f"Loaded {len(df)} rows. Agent is analyzing URLs with Universal Logic...")
    
    # 1. RUN ANALYSIS
    progress_bar = st.progress(0)
    status_results = []
    urls = df['Page Url'].tolist()
    
    # Parallel Processing
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        status_results = list(executor.map(check_single_url, urls))
        
    df['Sold_Status'] = status_results
    
    # 2. PREPARE DATA
    df['Category'] = df['Page Url'].apply(get_display_category)
    
    # Extract details
    details = df['Page Url'].apply(lambda x: extract_display_details(x))
    df['Vehicle Name'] = [d[0] for d in details]
    df['Type'] = [d[1] for d in details]
    
    # Define "Is Sold" (Starts with SOLD)
    df['Is Sold'] = df['Sold_Status'].astype(str).str.startswith('SOLD')
    
    st.success("Analysis Complete!")
    st.divider()
    
    # --- DASHBOARD METRICS ---
    sold_count = df['Is Sold'].sum()
    vdp_visits = len(df[df['Category'] == 'VDP'])
    l2b = (sold_count / vdp_visits * 100) if vdp_visits > 0 else 0
    
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
        st.markdown("**Traffic Distribution (Visitors)**")
        cats = ['VDP', 'New Car Search', 'Used Car Search', 'General Search', 'Service', 'Homepage']
        
        # FIX: Sum the 'Attributed Unique Visitors' instead of counting rows
        traffic_data = df[df['Category'].isin(cats)].groupby('Category')['Attributed Unique Visitors'].sum()
        
        if not traffic_data.empty:
            st.bar_chart(traffic_data)
        else:
            st.info("No traffic data.")
        
    with col_chart2:
        st.markdown("**Sales Mix (New vs Used)**")
        if sold_count > 0:
            sales_mix = sold_df['Type'].value_counts()
            fig, ax = plt.subplots()
            ax.pie(sales_mix, labels=sales_mix.index, autopct='%1.1f%%', colors=['#66b3ff', '#99ff99'])
            st.pyplot(fig)
        else:
            st.warning("No sales detected yet.")

    # --- TABLES ---
    c_left, c_right = st.columns(2)
    
    with c_left:
        st.subheader("🏆 Top Sold Vehicles")
        if sold_count > 0:
            top_sold = df[df['Is Sold']].sort_values('Attributed Unique Visitors', ascending=False).head(10)
            st.dataframe(top_sold[['Vehicle Name', 'Type', 'Attributed Unique Visitors', 'Sold_Status']].reset_index(drop=True), use_container_width=True)
        else:
            st.info("No sold vehicles identified.")
            
    with c_right:
        st.subheader("⚠️ Missed Opportunities (New & Used)")
        # Calculate Average traffic of SOLD cars
        avg_traffic = sold_df['Attributed Unique Visitors'].mean() if sold_count > 0 else 0
        
        # Filter: Available + VDP + High Traffic
        missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_traffic)]
        missed = missed.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        
        if not missed.empty:
            st.dataframe(missed[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True), use_container_width=True)
        else:
            st.info("No missed opportunities found.")

    # --- DOWNLOAD ---
    csv = df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="Download Full Analysis CSV",
        data=csv,
        file_name="Auto_Analyst_Report.csv",
        mime="text/csv"
    )
