import pandas as pd
import streamlit as st

from pipeline import run_pipeline, supabase_client

st.set_page_config(page_title="Outreach Lead Finder", layout="wide")
st.title("Outreach Lead Finder")

# ---------------------------------------------------------------------------
# Run form
# ---------------------------------------------------------------------------

with st.form("run_form"):
    col1, col2 = st.columns(2)
    business_type = col1.text_input("Business type", placeholder="import trading company")
    country = col2.text_input("Country", placeholder="United Arab Emirates")
    max_results = st.slider("Max businesses to collect (controls Apify usage)", 10, 300, 50, step=10)
    submitted = st.form_submit_button("Run pipeline")

if submitted:
    if not business_type or not country:
        st.error("Fill in both fields.")
    else:
        with st.spinner("Collecting businesses, finding emails, verifying... this can take a few minutes."):
            try:
                result = run_pipeline(business_type, country, max_results)
                st.success(
                    f"Done. {len(result['leads'])} businesses collected, "
                    f"{sum(1 for l in result['leads'] if l['email'])} emails found."
                )
            except Exception as e:
                st.error(f"Pipeline failed: {e}")

st.divider()

# ---------------------------------------------------------------------------
# Note on verification from Streamlit Cloud
# ---------------------------------------------------------------------------

st.caption(
    "Note: Streamlit Cloud blocks outbound port 25 like most cloud hosts, so the verifier "
    "will mark most emails 'unknown' when run from here. Run the verify step from a machine "
    "or VPS that allows port 25 if you need real valid/invalid results — the rest of the "
    "pipeline (collecting + finding emails) works fine on Streamlit Cloud."
)

# ---------------------------------------------------------------------------
# Results table — pulled fresh from Supabase, not from the run above,
# so past runs are browsable too
# ---------------------------------------------------------------------------

st.subheader("Results")

status_filter = st.selectbox("Filter by verification status", ["all", "valid", "invalid", "risky", "unknown", "not found"])

query = supabase_client().table("leads").select("*").order("created_at", desc=True)
rows = query.execute().data

df = pd.DataFrame(rows)

if not df.empty:
    if status_filter == "not found":
        df = df[df["email"].isna()]
    elif status_filter != "all":
        df = df[df["verified"] == status_filter]

    st.dataframe(df, use_container_width=True)

    with_email = df[df["email"].notna()] if "email" in df.columns else df

    dl_col1, dl_col2 = st.columns(2)
    dl_col1.download_button(
        "Download all with email (CSV)",
        with_email.to_csv(index=False),
        file_name="leads_with_email.csv",
        mime="text/csv",
    )
    dl_col2.download_button(
        "Download all with email (JSON)",
        with_email.to_json(orient="records", indent=2),
        file_name="leads_with_email.json",
        mime="application/json",
    )
else:
    st.info("No leads yet — run the pipeline above.")
