import streamlit as st
import pandas as pd
import requests
import re
import concurrent.futures
import matplotlib.pyplot as plt
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
st.set_page_config(page_title="Universal Auto-Sales Intelligence", layout="wide")

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

# --- HIGH-SPEED UNIVERSAL LOGIC ---

def extract_car_info_universal(url):
    if not isinstance(url, str): return None, None
    url_clean = url.lower().strip()
    if any(x in url_clean.split('/')[-1] for x in ['search', 'inventory', 'all', 'index', '.aspx']):
        return None, None
    year_match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', url_clean)
    if not year_match: return None, None
    year = year_match.group(1)
    make_match = re.search(rf'{year}[- ]([a-z0-9+]+)', url_clean)
    make = make_match.group(1).replace('+', ' ').title() if make_match else "Vehicle"
    return year, make

def check_single_url(url):
    """Speed-optimized checker"""
    year, make = extract_car_info_universal(url)
    if not year: return "N/A (Not a VDP)"
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        # Shorter timeout (5s) to prevent hanging on dead sites
        response = requests.get(url, headers=headers, timeout=5, allow_redirects=True)
        final_url = response.url.lower()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        page_text = response.text.lower()

        if any(p in page_text for p in ["vehicle not found", "no longer available"]): return "SOLD (Error)"
        if "sold" in page_title: return "SOLD (Title)"
        
        # Universal Redirect Check
        search_indicators = ['search', 'inventory', 'results', '.aspx']
        if any(ind in final_url for ind in search_indicators) and url.lower().split('?')[0] not in final_url:
            return "SOLD (Redirected)"

        # Soft Redirect Check
        if year not in page_title and len(page_title) > 5:
            return "SOLD (Title Mismatch)"

        return "Available"
    except:
        return "Error (Link Dead)"

def get_display_category(url):
    url = str(url).lower()
    if url.endswith('.com/') or url.endswith('.com'): return 'Homepage'
    if any(x in url for x in ['search', 'inventory', '.aspx']):
        if 'new' in url: return 'New Car Search'
        if 'used' in url or 'preowned' in url: return 'Used Car Search'
        return 'General Search'
    if re.search(r'(?:19|20)\d{2}', url): return 'VDP'
    return 'Other'

def clean_vehicle_name(url):
    url_path = urlparse(url).path
    match = re.search(r'((?:19|20)\d{2}.*)', url_path)
    if match:
        name = match.group(1).replace('/', '-').replace('.htm', '').replace('-', ' ').replace('+', ' ')
        name = re.sub(r' Baltimore.*| Ephrata.*| [a-z0-9]{20,}', '', name, flags=re.IGNORECASE)
        return name.title().strip()
    return "Unknown Vehicle"

# --- UI ---
st.title("🚀 High-Speed Sales Intelligence")
uploaded_file = st.file_uploader("Upload Raw Traffic CSV", type=['csv'])

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    st.info(f"Analyzing {len(df)} URLs using 10-way Parallel Processing...")
    
    # Progress visualization
    progress_bar = st.progress(0)
    urls = df['Page Url'].tolist()
    
    # THE SPEED BOOSTER: concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        status_results = list(executor.map(check_single_url, urls))
        
    df['Sold_Status'] = status_results
    df['Category'] = df['Page Url'].apply(get_display_category)
    df['Vehicle Name'] = df['Page Url'].apply(clean_vehicle_name)
    df['Type'] = df['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', x) else 'Used')
    df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
    
    st.success("Analysis Finished!")
    st.divider()
    
    # Metrics and Visuals (Shortened for brevity)
    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")
    
    col_chart1, col_chart2 = st.columns(2)
    with col_chart1:
        st.markdown("**Traffic Mix**")
        traffic = df[df['Category'] != 'Other'].groupby('Category')['Attributed Unique Visitors'].sum()
        st.bar_chart(traffic)
    with col_chart2:
        st.markdown("**Sales Mix**")
        if len(sold_df) > 0:
            mix = sold_df['Type'].value_counts()
            fig, ax = plt.subplots()
            ax.pie(mix, labels=mix.index, autopct='%1.1f%%', colors=['#66b3ff', '#99ff99'])
            st.pyplot(fig)

    t1, t2 = st.columns(2)
    with t1:
        st.subheader("🏆 Top Sold Units")
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        top_sold.index += 1
        st.dataframe(top_sold, use_container_width=True)
    with t2:
        st.subheader("⚠️ High Interest / Unsold")
        avg_s = sold_df['Attributed Unique Visitors'].mean() if len(sold_df)>0 else 0
        missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_s)].sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        missed.index += 1
        st.dataframe(missed, use_container_width=True)

    st.download_button("Download Report", df.to_csv(index=False), "Full_Report.csv", "text/csv")
