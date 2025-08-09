
import streamlit as st
import pandas as pd
import math
from io import BytesIO
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Shotcraft Inventory (Google Sheets Live)", layout="wide")
st.title("📦 Shotcraft — Live Inventory (Google Sheets)")
st.caption("Pass your Sheet ID in the URL (?sheet_id=...), or paste it below. This build tolerates list-valued query params and fixes private_key newlines automatically.")

# -----------------------------
# Helpers for config
# -----------------------------

def normalize_private_key(sa: dict) -> dict:
    sa = dict(sa) if sa else {}
    pk = sa.get("private_key", "")
    if "\\n" in pk:
        sa["private_key"] = pk.replace("\\n", "\n")
    return sa

def read_service_account():
    if "gcp_service_account" not in st.secrets:
        st.error("Missing [gcp_service_account] in Secrets. Add your service account JSON under that header.")
        st.stop()
    return normalize_private_key(st.secrets["gcp_service_account"])

def first_value(x):
    if isinstance(x, (list, tuple)):
        return x[0] if x else None
    return x

def resolve_sheet_id():
    # Try query param
    raw = None
    try:
        raw = st.query_params.get("sheet_id", None)
    except Exception:
        try:
            raw = st.experimental_get_query_params().get("sheet_id", None)
        except Exception:
            raw = None
    raw = first_value(raw)
    if raw:
        return raw.strip().split("/d/")[-1].split("/")[0] if "/d/" in raw else raw.strip()

    # Try secrets (top-level)
    sid = first_value(st.secrets.get("SHEET_ID", None))
    if sid:
        return sid.strip()

    # Try secrets [app]
    appsec = st.secrets.get("app", {})
    sid = first_value(appsec.get("SHEET_ID", None))
    if sid:
        return sid.strip()

    # Manual entry
    st.info("No SHEET_ID found. Paste your Sheet ID or full URL below and press **Use this Sheet**.")
    default_val = st.session_state.get("manual_sheet_input", "")
    user_input = st.text_input("Google Sheet ID **or** full URL", value=default_val, placeholder="1ivuxCDfMu... OR https://docs.google.com/spreadsheets/d/…/edit")
    use_it = st.button("Use this Sheet", type="primary")
    if use_it and user_input:
        txt = user_input.strip()
        if "/d/" in txt:
            try:
                txt = txt.split("/d/")[1].split("/")[0]
            except Exception:
                pass
        st.session_state["manual_sheet_input"] = txt
        st.rerun()
    return st.session_state.get("manual_sheet_input")

def resolve_ws_names():
    form_ws = "FORMULA"
    inv_ws = "INVENTORY"
    # From query params
    try:
        fqp = first_value(st.query_params.get("formula_ws", None))
        iqp = first_value(st.query_params.get("inventory_ws", None))
        if fqp: form_ws = fqp
        if iqp: inv_ws = iqp
    except Exception:
        pass
    # From secrets
    form_ws = st.secrets.get("FORMULA_WS", form_ws)
    inv_ws  = st.secrets.get("INVENTORY_WS", inv_ws)
    appsec = st.secrets.get("app", {})
    form_ws = appsec.get("FORMULA_WS", form_ws)
    inv_ws  = appsec.get("INVENTORY_WS", inv_ws)
    return form_ws, inv_ws

SERVICE_ACCOUNT_INFO = read_service_account()
SHEET_ID = resolve_sheet_id()
FORMULA_WS, INVENTORY_WS = resolve_ws_names()

with st.sidebar:
    st.header("🔧 Debug")
    st.write("Secrets keys:", list(st.secrets.keys()))
    st.write("Using SHEET_ID:", SHEET_ID if SHEET_ID else "(none)")
    st.write("FORMULA_WS:", FORMULA_WS, "| INVENTORY_WS:", INVENTORY_WS)

if not SHEET_ID:
    st.stop()

# -----------------------------
# Google Sheets
# -----------------------------

@st.cache_resource(show_spinner=False)
def get_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=scopes)
    return gspread.authorize(creds)

def read_ws_df(ws):
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame()
    headers = list(values[0]) if values and isinstance(values[0], list) else []
    rows = values[1:] if len(values) > 1 else []
    return pd.DataFrame(rows, columns=headers)

def load_data(gc):
    sh = gc.open_by_key(SHEET_ID)
    fws = sh.worksheet(FORMULA_WS)
    iws = sh.worksheet(INVENTORY_WS)
    formula = read_ws_df(fws)
    inv = read_ws_df(iws)

    for c in ("Per_Case",):
        if c in formula.columns:
            formula[c] = pd.to_numeric(formula[c], errors="coerce")
    if "On_Hand" in inv.columns:
        inv["On_Hand"] = pd.to_numeric(inv["On_Hand"], errors="coerce")

    need = {"Component","Per_Case"}
    if not need.issubset(set(formula.columns)):
        raise ValueError(f"FORMULA must have headers: {sorted(list(need))}. Found: {list(formula.columns)}")

    comps = formula[["Component","Per_Case"]].copy()
    comps["UOM"] = formula["UOM"] if "UOM" in formula.columns else ""
    if {"Component","On_Hand"}.issubset(inv.columns):
        onhand = inv[["Component","On_Hand"]].copy()
    else:
        onhand = pd.DataFrame({"Component": comps["Component"], "On_Hand": 0.0})
    return sh, comps.reset_index(drop=True), onhand.reset_index(drop=True)

def write_onhand(sh, edited_df):
    ws = sh.worksheet(INVENTORY_WS)
    out = edited_df[["Component","On_Hand"]].copy()
    out["On_Hand"] = pd.to_numeric(out["On_Hand"], errors="coerce").fillna(0).astype(float)
    values = [out.columns.tolist()] + out.astype(object).where(pd.notnull(out), "").values.tolist()
    ws.clear()
    ws.update(values)

def compute(comps, onhand, cases):
    df = comps.merge(onhand, on="Component", how="left")
    if "On_Hand" not in df.columns: df["On_Hand"] = 0.0
    df["Per_Case"] = pd.to_numeric(df["Per_Case"], errors="coerce").fillna(0.0)
    df["On_Hand"]  = pd.to_numeric(df["On_Hand"], errors="coerce").fillna(0.0)
    df["Required"] = df["Per_Case"] * float(cases)
    df["Remaining"] = df["On_Hand"] - df["Required"]

    candidates = df[df["Per_Case"] > 0]
    max_sell = int(math.floor((candidates["On_Hand"]/candidates["Per_Case"]).min())) if not candidates.empty else 0

    shortages = df[df["Remaining"] < 0][["Component","UOM","On_Hand","Per_Case","Required","Remaining"]].copy()
    display = df[["Component","UOM","On_Hand","Per_Case","Required","Remaining"]].sort_values("Component")
    return display, max_sell, shortages

def download_excel(formula_name, display_df):
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="xlsxwriter") as writer:
        display_df[["Component","UOM","Per_Case"]].to_excel(writer, sheet_name=formula_name, index=False)
        display_df.to_excel(writer, sheet_name="INVENTORY", index=False)
    bio.seek(0)
    return bio

# -----------------------------
# Main
# -----------------------------

try:
    gc = get_client()
    sh, comps, onhand = load_data(gc)
    st.success("Connected to Google Sheet ✓")
except Exception as e:
    st.error(f"Could not connect to Google Sheets: {e}")
    st.stop()

with st.sidebar:
    st.header("Actions")
    if st.button("Reload from Sheet"):
        st.cache_data.clear()
        st.rerun()

st.subheader("Per-case usage (from FORMULA)")
st.dataframe(comps, hide_index=True, use_container_width=True)

st.subheader("Edit On_Hand (writes back to INVENTORY)")
base = comps.merge(onhand, on="Component", how="left")
base["On_Hand"] = pd.to_numeric(base["On_Hand"], errors="coerce").fillna(0.0)

edited = st.data_editor(
    base[["Component","UOM","On_Hand","Per_Case"]],
    hide_index=True,
    column_config={
        "Component": st.column_config.TextColumn(disabled=True),
        "UOM": st.column_config.TextColumn(disabled=True),
        "Per_Case": st.column_config.NumberColumn(format="%.6f", disabled=True),
        "On_Hand": st.column_config.NumberColumn(help="Type your current stock here"),
    },
    use_container_width=True,
    key="edit_table"
)

c1, c2 = st.columns(2)
with c1:
    if st.button("💾 Sync On_Hand to Google Sheets"):
        try:
            write_onhand(sh, edited)
            st.success(f"Synced at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            st.error(f"Sync failed: {e}")
with c2:
    if st.button("↩️ Revert to current sheet values"):
        st.cache_data.clear()
        st.rerun()

st.subheader("Order size")
cases = st.number_input("Cases sold (e.g., LCBO order)", min_value=0.0, step=1.0, value=0.0)

display, max_sell, shortages = compute(comps, edited[["Component","On_Hand"]].copy(), cases)

st.markdown("### Results")
m1, m2 = st.columns(2)
with m1: st.metric("Max sellable cases from current stock", max_sell)
with m2: st.metric("Order size (cases)", int(cases))

st.dataframe(display, hide_index=True, use_container_width=True)

if not shortages.empty:
    st.warning("Shortages for this order:")
    st.dataframe(shortages, hide_index=True, use_container_width=True)
else:
    st.info("No shortages detected for this order.")

st.markdown("### Download snapshot")
buf = download_excel(FORMULA_WS, display)
st.download_button("Download Excel snapshot", buf, file_name="Shotcraft_Inventory_Snapshot.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
