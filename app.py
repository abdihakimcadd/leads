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
    submitted = st.form_submit_button("Run pipeline")

if submitted:
    if not business_type or not country:
        st.error("Fill in both fields.")
    else:
        with st.spinner("Collecting businesses, finding emails, verifying... this can take a few minutes."):
            try:
                result = run_pipeline(business_type, country)
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

    valid_only = df[df["verified"] == "valid"] if "verified" in df.columns else df
    st.download_button(
        "Download valid leads as CSV",
        valid_only.to_csv(index=False),
        file_name="valid_leads.csv",
        mime="text/csv",
    )
else:
    st.info("No leads yet — run the pipeline above.")
