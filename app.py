# -*- coding: utf-8 -*-
import itertools
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
import streamlit as st

APP_TITLE = "Profit Mix Optimizer"
APP_SUBTITLE = "מציע 3 חלופות לשילוב בין 2–3 קרנות השתלמות, לפי יעדים שהגדרת + מגבלות (למשל לא־סחיר), עם משקל משמעותי גם לשירות."

# -----------------------------
# Page + RTL + Dark UI helpers
# -----------------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")

def inject_rtl_dark_css() -> None:
    st.markdown(
        """
<style>
/* RTL + base typography */
html, body, [class*="css"]  { direction: rtl; }
* { font-family: system-ui, -apple-system, Segoe UI, Roboto, "Noto Sans Hebrew", Arial, sans-serif; }

/* Make Streamlit containers feel like a product */
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1400px; }
h1, h2, h3 { letter-spacing: -0.2px; }

/* Dark-only: prevent bright table backgrounds */
[data-testid="stDataFrame"] * { color: #EAEAEA !important; }
[data-testid="stDataFrame"] div[role="grid"] { background: #0E1117 !important; }
[data-testid="stDataFrame"] div[role="columnheader"] { background: #141A22 !important; }
[data-testid="stDataFrame"] div[role="rowheader"] { background: #141A22 !important; }
[data-testid="stDataFrame"] div { border-color: rgba(255,255,255,0.08) !important; }

/* KPI cards */
.pm-kpi-wrap { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.pm-kpi { background: #0E1117; border: 1px solid rgba(255,255,255,0.10); border-radius: 16px; padding: 14px 16px; }
.pm-kpi .t { font-size: 13px; opacity: 0.85; margin-bottom: 6px; }
.pm-kpi .v { font-size: 22px; font-weight: 700; letter-spacing: -0.3px; }
.pm-kpi .s { font-size: 12px; opacity: 0.85; margin-top: 6px; }

/* Color chips */
.pm-chip { display:inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; border: 1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.04); margin-left: 6px; }

/* Slider tick labels / tooltips: center so they don't go off-screen on mobile */
div[data-baseweb="slider"] { padding-left: 10px; padding-right: 10px; }
div[data-baseweb="slider"] [data-testid="stTickBar"] { justify-content: center !important; }
div[data-baseweb="slider"] [data-testid="stTooltipContent"] { 
    transform: translateX(-50%) !important;
    left: 50% !important;
    right: auto !important;
    max-width: 92vw !important;
}
</style>
        """,
        unsafe_allow_html=True,
    )

inject_rtl_dark_css()

# -----------------------------
# Data loading
# -----------------------------
REQUIRED_PARAMS = [
    "חשיפה מנייתית (%)",
    "חשיפה לחו״ל (%)",
    "חשיפה למט״ח (%)",
    "חשיפה ללא-סחיר (%)",
    "שארפ משוקלל",
]

def _normalize_param_name(x: str) -> str:
    x = str(x).strip()
    x = x.replace("חו\"ל", "חו״ל")
    x = x.replace("מט\"ח", "מט״ח")
    x = x.replace("ללא סחיר", "ללא-סחיר")
    x = re.sub(r"\s+", " ", x)
    return x

def manager_from_fund_name(fund_name: str) -> str:
    # Heuristic: first token is usually the managing body
    s = str(fund_name).strip()
    if not s:
        return "לא ידוע"
    return s.split()[0]

def load_funds_from_excel(file) -> pd.DataFrame:
    xls = pd.ExcelFile(file)
    # pick first sheet that contains the required parameters
    chosen = None
    for sh in xls.sheet_names:
        tmp = xls.parse(sh)
        if "פרמטר" in tmp.columns:
            params = set(_normalize_param_name(p) for p in tmp["פרמטר"].dropna().astype(str).tolist())
            if set(_normalize_param_name(p) for p in REQUIRED_PARAMS).issubset(params):
                chosen = sh
                break
    if chosen is None:
        raise ValueError("לא נמצא גיליון עם כל הפרמטרים הנדרשים. ודא שיש עמודה בשם 'פרמטר' ושורות עבור החשיפות והשארפ.")

    df = xls.parse(chosen)
    df["פרמטר"] = df["פרמטר"].map(_normalize_param_name)

    fund_cols = [c for c in df.columns if c != "פרמטר"]
    long = df.melt(id_vars=["פרמטר"], value_vars=fund_cols, var_name="מסלול", value_name="ערך")
    wide = long.pivot_table(index="מסלול", columns="פרמטר", values="ערך", aggfunc="first").reset_index()

    # Ensure required columns exist
    for p in REQUIRED_PARAMS:
        p2 = _normalize_param_name(p)
        if p2 not in wide.columns:
            raise ValueError(f"חסר פרמטר נדרש: {p2}")

    # numeric coerce
    for c in REQUIRED_PARAMS:
        wide[c] = pd.to_numeric(wide[c], errors="coerce")

    wide["גוף מנהל"] = wide["מסלול"].map(manager_from_fund_name)

    # Clean: drop rows with missing essential numbers
    wide = wide.dropna(subset=REQUIRED_PARAMS).copy()

    # Clip exposures to [0, 100] for safety
    for c in ["חשיפה מנייתית (%)","חשיפה לחו״ל (%)","חשיפה למט״ח (%)","חשיפה ללא-סחיר (%)"]:
        wide[c] = wide[c].clip(0, 100)

    return wide

def load_service_scores(uploaded_csv) -> pd.DataFrame:
    """
    CSV columns expected: provider, score
    provider = body name, score = 0..100 (or 0..10). We'll rescale to 0..100 if needed.
    """
    df = pd.read_csv(uploaded_csv)
    cols = {c.lower().strip(): c for c in df.columns}
    if "provider" not in cols or "score" not in cols:
        raise ValueError("קובץ השירות צריך לכלול עמודות בשם provider ו-score")
    out = df[[cols["provider"], cols["score"]]].rename(columns={cols["provider"]: "גוף מנהל", cols["score"]: "ציון שירות"})
    out["גוף מנהל"] = out["גוף מנהל"].astype(str).str.strip()
    out["ציון שירות"] = pd.to_numeric(out["ציון שירות"], errors="coerce")
    out = out.dropna(subset=["גוף מנהל","ציון שירות"])
    # normalize to 0..100
    mx = out["ציון שירות"].max()
    if mx <= 10.5:
        out["ציון שירות"] = out["ציון שירות"] * 10.0
    out["ציון שירות"] = out["ציון שירות"].clip(0, 100)
    return out

def attach_service_scores(funds: pd.DataFrame, service: Optional[pd.DataFrame]) -> pd.DataFrame:
    if service is None or service.empty:
        funds = funds.copy()
        funds["ציון שירות"] = 60.0  # neutral default
        return funds
    merged = funds.merge(service, on="גוף מנהל", how="left")
    merged["ציון שירות"] = merged["ציון שירות"].fillna(60.0)
    return merged

# -----------------------------
# Optimization
# -----------------------------
@dataclass(frozen=True)
class Targets:
    stocks: float
    abroad: float
    fx: float
    illiquid: float

@dataclass(frozen=True)
class Limits:
    max_illiquid: float
    max_fx: float
    max_abroad: float
    min_service: float

@dataclass(frozen=True)
class Weights:
    dev: float
    sharpe: float
    service: float

def combo_metrics(rows: List[pd.Series], weights: List[float]) -> Dict[str, float]:
    w = np.array(weights, dtype=float)
    w = w / w.sum()
    def wavg(col):
        return float(np.sum([w[i] * float(rows[i][col]) for i in range(len(rows))]))
    return {
        "חשיפה מנייתית (%)": wavg("חשיפה מנייתית (%)"),
        "חשיפה לחו״ל (%)": wavg("חשיפה לחו״ל (%)"),
        "חשיפה למט״ח (%)": wavg("חשיפה למט״ח (%)"),
        "חשיפה ללא-סחיר (%)": wavg("חשיפה ללא-סחיר (%)"),
        "שארפ משוקלל": wavg("שארפ משוקלל"),
        "ציון שירות": wavg("ציון שירות"),
    }

def deviation_score(m: Dict[str, float], t: Targets) -> float:
    # L1 deviation (absolute) – stable and explainable
    return (
        abs(m["חשיפה מנייתית (%)"] - t.stocks)
        + abs(m["חשיפה לחו״ל (%)"] - t.abroad)
        + abs(m["חשיפה למט״ח (%)"] - t.fx)
        + abs(m["חשיפה ללא-סחיר (%)"] - t.illiquid)
    )

def feasible(m: Dict[str, float], lim: Limits) -> bool:
    if m["חשיפה ללא-סחיר (%)"] > lim.max_illiquid + 1e-9:
        return False
    if m["חשיפה למט״ח (%)"] > lim.max_fx + 1e-9:
        return False
    if m["חשיפה לחו״ל (%)"] > lim.max_abroad + 1e-9:
        return False
    if m["ציון שירות"] < lim.min_service - 1e-9:
        return False
    return True

def objective(m: Dict[str, float], dev: float, w: Weights) -> float:
    # lower is better
    # - dev (bigger is worse)
    # - sharpe: maximize -> subtract
    # - service: maximize -> subtract
    return w.dev * dev - w.sharpe * m["שארפ משוקלל"] - w.service * (m["ציון שירות"] / 100.0)

def generate_weight_grid(n_funds: int, step_pct: int) -> List[List[float]]:
    step = step_pct / 100.0
    if n_funds == 2:
        return [[x, 1.0 - x] for x in np.arange(step, 1.0, step)]
    if n_funds == 3:
        grid = []
        for a in np.arange(step, 1.0, step):
            for b in np.arange(step, 1.0, step):
                c = 1.0 - a - b
                if c >= step - 1e-9:
                    grid.append([a, b, c])
        return grid
    raise ValueError("supports only 2 or 3 funds")

def enumerate_solutions(
    funds: pd.DataFrame,
    n_funds: int,
    step_pct: int,
    targets: Targets,
    limits: Limits,
    weights: Weights,
    same_manager_only: bool,
) -> pd.DataFrame:
    weights_grid = generate_weight_grid(n_funds=n_funds, step_pct=step_pct)
    rows = []
    fund_records = funds.to_dict("records")

    for idxs in itertools.combinations(range(len(fund_records)), n_funds):
        picked = [fund_records[i] for i in idxs]
        managers = sorted(set(p["גוף מנהל"] for p in picked))
        if same_manager_only and len(managers) != 1:
            continue

        picked_series = [pd.Series(p) for p in picked]

        for wts in weights_grid:
            m = combo_metrics(picked_series, wts)
            if not feasible(m, limits):
                continue
            dev = deviation_score(m, targets)
            obj = objective(m, dev, weights)

            row = {
                "n": n_funds,
                "מנהלים": " / ".join(managers),
                "סטייה מהיעד": dev,
                "שארפ משוקלל": m["שארפ משוקלל"],
                "ציון שירות": m["ציון שירות"],
                "חשיפה לחו״ל (%)": m["חשיפה לחו״ל (%)"],
                "חשיפה מנייתית (%)": m["חשיפה מנייתית (%)"],
                "חשיפה למט״ח (%)": m["חשיפה למט״ח (%)"],
                "חשיפה ללא-סחיר (%)": m["חשיפה ללא-סחיר (%)"],
                "Objective": obj,
            }
            # add fund names + weights columns
            for k in range(n_funds):
                row[f"מסלול {k+1}"] = picked[k]["מסלול"]
                row[f"משקל {k+1}"] = wts[k] * 100.0
            rows.append(row)

    if not rows:
        return pd.DataFrame()

    res = pd.DataFrame(rows).sort_values("Objective", ascending=True).reset_index(drop=True)
    return res

def pick_three_distinct_manager_solutions(sol: pd.DataFrame) -> List[pd.Series]:
    """Pick 3 best solutions with distinct manager sets."""
    if sol.empty:
        return []

    picked = []
    used = set()
    for _, r in sol.iterrows():
        key = r["מנהלים"]
        if key in used:
            continue
        picked.append(r)
        used.add(key)
        if len(picked) == 3:
            break
    return picked

def format_solution_explainer(sol_row: pd.Series, best_dev: float, best_obj: float) -> str:
    # short sharp text
    dev = float(sol_row["סטייה מהיעד"])
    sharpe = float(sol_row["שארפ משוקלל"])
    service = float(sol_row["ציון שירות"])
    if dev == best_dev:
        return f"הכי מדויק ליעד (סטייה כוללת {dev:.1f})."
    # else compare to best objective
    delta_obj = float(sol_row["Objective"]) - best_obj
    if delta_obj <= 0.02:
        return f"שילוב מאוזן: שארפ {sharpe:.2f} עם שירות {service:.0f} וסטייה {dev:.1f}."
    return f"מעדיף איכות/שירות: שירות {service:.0f}, שארפ {sharpe:.2f}, סטייה {dev:.1f}."

def color_chip(label: str) -> str:
    return f'<span class="pm-chip">{label}</span>'

# -----------------------------
# UI
# -----------------------------
st.markdown(f"# {APP_TITLE}")
st.caption(APP_SUBTITLE)

# Load data (bundled excel or upload)
with st.expander("נתוני מקור (Excel / שירות)", expanded=False):
    st.write("ברירת מחדל: האפליקציה משתמשת בקובץ אקסל שמגיע עם הריפו. אפשר להחליף בקובץ מעודכן כאן.")
    upl_xlsx = st.file_uploader("העלה קובץ Excel מעודכן", type=["xlsx"])
    upl_service = st.file_uploader("CSV לציוני שירות (provider, score) – אופציונלי", type=["csv"])

DEFAULT_XLSX_PATH = "קרנות_השתלמות_חשיפות.xlsx"

@st.cache_data(show_spinner=False)
def _cached_load_funds(file_bytes: bytes) -> pd.DataFrame:
    import io
    return load_funds_from_excel(io.BytesIO(file_bytes))

def get_funds_df() -> pd.DataFrame:
    if upl_xlsx is not None:
        return _cached_load_funds(upl_xlsx.getvalue())
    # try local file
    try:
        with open(DEFAULT_XLSX_PATH, "rb") as f:
            b = f.read()
        return _cached_load_funds(b)
    except Exception as e:
        raise RuntimeError("לא נמצא קובץ אקסל בריפו. העלה כאן קובץ Excel.") from e

funds_raw = get_funds_df()

service_df = None
if upl_service is not None:
    try:
        service_df = load_service_scores(upl_service)
    except Exception as e:
        st.error(f"שגיאה בקובץ השירות: {e}")

funds = attach_service_scores(funds_raw, service_df)

# ---- Tabs
tab1, tab2, tab3 = st.tabs(["הגדרות יעד", "תוצאות (3 חלופות)", "פירוט חישוב / שקיפות"])

# -------------- TAB 1: inputs
with tab1:
    left, right = st.columns([1.15, 1.0], gap="large")

    with right:
        st.subheader("הגדרות כלליות")
        same_manager_only = st.toggle("לחייב שכל השילוב יהיה מאותו גוף מנהל", value=False)
        n_funds = st.radio("כמה קרנות לשלב?", options=[2, 3], horizontal=True, index=0)
        step_pct = st.select_slider(
            "רזולוציית משקלים (ככל שקטן יותר – יותר חישובים)",
            options=[5, 10, 20],
            value=10 if n_funds == 3 else 5
        )

        # Presets
        st.markdown("**Presets מהירים**")
        c1, c2, c3 = st.columns(3)
        preset = None
        if c1.button("תיק גלובלי 60/40", use_container_width=True):
            preset = {"stocks": 60, "abroad": 60, "fx": 60, "illiquid": 10, "max_illiquid": 20, "max_fx": 80, "max_abroad": 100, "min_service": 0}
        if c2.button("מקסימום מט״ח", use_container_width=True):
            preset = {"stocks": 70, "abroad": 80, "fx": 100, "illiquid": 10, "max_illiquid": 20, "max_fx": 100, "max_abroad": 100, "min_service": 0}
        if c3.button("כמה שיותר לא־סחיר עד 20%", use_container_width=True):
            preset = {"stocks": 55, "abroad": 55, "fx": 55, "illiquid": 20, "max_illiquid": 20, "max_fx": 80, "max_abroad": 100, "min_service": 0}

        st.divider()
        st.markdown("**מגבלות**")
        max_illiquid = st.slider("מקסימום לא־סחיר (%)", 0, 40, value=int(preset["max_illiquid"]) if preset else 20, step=1)
        max_fx = st.slider("מקסימום מט״ח (%)", 0, 100, value=int(preset["max_fx"]) if preset else 80, step=1)
        max_abroad = st.slider("מקסימום חו״ל (%)", 0, 100, value=int(preset["max_abroad"]) if preset else 100, step=1)
        min_service = st.slider("מינימום ציון שירות (%)", 0, 100, value=int(preset["min_service"]) if preset else 0, step=1)

        st.divider()
        st.markdown("**מיקוד חישוב**")
        focus = st.radio(
            "מה חשוב יותר?",
            options=["דיוק ליעד", "מאוזן (דיוק+שירות+שארפ)", "שירות/איכות"],
            index=1,
        )

    with left:
        st.subheader("הגדרות יעד וכללים")
        st.write("הגדר את היעדים. החישוב תמיד רץ בגישה **יציבה/יסודית** (אין מצב מהיר).")

        stocks = st.slider("יעד חשיפה מנייתית (%)", 0, 100, value=int(preset["stocks"]) if preset else 60, step=1)
        abroad = st.slider("יעד חשיפה לחו״ל (%)", 0, 100, value=int(preset["abroad"]) if preset else 60, step=1)
        fx = st.slider("יעד חשיפה למט״ח (%)", 0, 100, value=int(preset["fx"]) if preset else 60, step=1)
        illiquid = st.slider("יעד חשיפה ללא־סחיר (%)", 0, 40, value=int(preset["illiquid"]) if preset else 10, step=1)

        # Optional exclusions
        st.divider()
        st.markdown("**סינון מסלולים**")
        default_exclude = ["IRA", "ניהול אישי", "סלייס", "Slice"]
        exclude_text = st.text_input("מילות סינון (מופרד בפסיקים)", value=", ".join(default_exclude))
        exclude_terms = [t.strip() for t in exclude_text.split(",") if t.strip()]

        allowed = funds.copy()
        if exclude_terms:
            pat = "|".join(re.escape(t) for t in exclude_terms)
            allowed = allowed[~allowed["מסלול"].str.contains(pat, case=False, na=False)].copy()

        st.info(f"מסלולים זמינים לאחר סינון: **{len(allowed)}** מתוך {len(funds)}")

        st.divider()
        st.markdown("**איפוס הגדרות**")
        st.caption("אם משהו התבלגן (במיוחד אחרי Preset), אפשר לאפס את כל ה־Session.")
        if st.button("איפוס הגדרות", type="secondary"):
            st.session_state.clear()
            st.rerun()

# Compute once (stable/solid always)
targets = Targets(stocks=stocks, abroad=abroad, fx=fx, illiquid=illiquid)
limits = Limits(max_illiquid=max_illiquid, max_fx=max_fx, max_abroad=max_abroad, min_service=min_service)

if focus == "דיוק ליעד":
    w = Weights(dev=1.00, sharpe=0.35, service=0.55)
elif focus == "מאוזן (דיוק+שירות+שארפ)":
    w = Weights(dev=0.85, sharpe=0.55, service=0.70)
else:  # שירות/איכות
    w = Weights(dev=0.65, sharpe=0.70, service=0.95)

@st.cache_data(show_spinner=True)
def _cached_solutions(
    allowed_df: pd.DataFrame,
    n_funds: int,
    step_pct: int,
    targets: Targets,
    limits: Limits,
    weights: Weights,
    same_manager_only: bool,
) -> pd.DataFrame:
    return enumerate_solutions(
        allowed_df, n_funds=n_funds, step_pct=step_pct,
        targets=targets, limits=limits, weights=weights,
        same_manager_only=same_manager_only
    )

solutions = _cached_solutions(
    allowed,
    n_funds=n_funds,
    step_pct=step_pct,
    targets=targets,
    limits=limits,
    weights=w,
    same_manager_only=same_manager_only
)

picked = pick_three_distinct_manager_solutions(solutions)

# -------------- TAB 2: results
with tab2:
    st.subheader("3 חלופות מוצעות")

    if solutions.empty or len(picked) == 0:
        st.error("לא נמצאו פתרונות שעומדים במגבלות. נסה להקל מגבלות (למשל לא־סחיר/מט״ח/מינימום שירות) או לשנות יעד.")
    else:
        best_dev = float(picked[0]["סטייה מהיעד"])
        best_obj = float(picked[0]["Objective"])

        # KPI cards per alternative (no mini tables)
        alt_cols = st.columns(3, gap="large")
        for i, r in enumerate(picked):
            dev = float(r["סטייה מהיעד"])
            sharpe = float(r["שארפ משוקלל"])
            service = float(r["ציון שירות"])
            abroad_v = float(r["חשיפה לחו״ל (%)"])
            stocks_v = float(r["חשיפה מנייתית (%)"])
            fx_v = float(r["חשיפה למט״ח (%)"])
            ill_v = float(r["חשיפה ללא-סחיר (%)"])

            badge = "🟢" if dev == best_dev else ("🟠" if dev <= best_dev + 10 else "⚪")
            expl = format_solution_explainer(r, best_dev=best_dev, best_obj=best_obj)

            with alt_cols[i]:
                st.markdown(
                    f"""
<div class="pm-kpi">
  <div class="t">{badge} חלופה {i+1}</div>
  <div class="v">סטייה: {dev:.1f}</div>
  <div class="s">{expl}</div>
  <div class="s">{color_chip(f"שארפ {sharpe:.2f}")}{color_chip(f"שירות {service:.0f}")}</div>
  <div class="s">{color_chip(f"חו״ל {abroad_v:.0f}%")}{color_chip(f"מניות {stocks_v:.0f}%")}{color_chip(f"מט״ח {fx_v:.0f}%")}{color_chip(f"לא־סחיר {ill_v:.0f}%")}</div>
  <div class="s"><b>מנהלים:</b> {r["מנהלים"]}</div>
</div>
                    """,
                    unsafe_allow_html=True
                )

        st.divider()
        st.subheader("טבלת תוצאות מלאה (3 חלופות)")

        # Build a full table: each row is a fund in a solution
        rows = []
        for i, r in enumerate(picked, start=1):
            for k in range(int(r["n"])):
                rows.append({
                    "חלופה": i,
                    "גוף מנהל": manager_from_fund_name(r[f"מסלול {k+1}"]),
                    "שם מסלול": r[f"מסלול {k+1}"],
                    "משקל (%)": float(r[f"משקל {k+1}"]),
                    "סטייה מהיעד": float(r["סטייה מהיעד"]),
                    "שארפ משוקלל": float(r["שארפ משוקלל"]),
                    "ציון שירות": float(r["ציון שירות"]),
                    "חו״ל (%)": float(r["חשיפה לחו״ל (%)"]),
                    "מניות (%)": float(r["חשיפה מנייתית (%)"]),
                    "מט״ח (%)": float(r["חשיפה למט״ח (%)"]),
                    "לא־סחיר (%)": float(r["חשיפה ללא-סחיר (%)"]),
                })

        out = pd.DataFrame(rows)

        # Highlight breaches or bests
        def style_row(row):
            styles = [""] * len(row)
            # illiquid breach (shouldn't happen due to feasibility, but keep)
            if row["לא־סחיר (%)"] > limits.max_illiquid + 1e-9:
                styles[out.columns.get_loc("לא־סחיר (%)")] = "color:#ff6b6b;font-weight:700;"
            # high deviation
            if row["סטייה מהיעד"] > best_dev + 10:
                styles[out.columns.get_loc("סטייה מהיעד")] = "color:#ffb86b;font-weight:700;"
            # best alternative marker
            if row["סטייה מהיעד"] == best_dev:
                styles[out.columns.get_loc("חלופה")] = "color:#2ee59d;font-weight:800;"
            return styles

        styled = out.style.apply(style_row, axis=1)

        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
            column_config={
                "שם מסלול": st.column_config.TextColumn("שם מסלול", width="large"),
                "גוף מנהל": st.column_config.TextColumn("גוף מנהל", width="medium"),
            },
            height=520
        )

        st.caption("טיפ: אם אתה על טלפון ורוצה לראות שם מסלול מלא – סובב למסך אופקי או גלול אופקית בטבלה.")

# -------------- TAB 3: transparency
with tab3:
    st.subheader("שקיפות חישוב (ב־Expander כדי לא להעמיס)")
    with st.expander("הצג פירוט חישוב", expanded=False):
        st.write("**יעדים:**", {"מניות": stocks, "חו״ל": abroad, "מט״ח": fx, "לא־סחיר": illiquid})
        st.write("**מגבלות:**", {"מקס' לא־סחיר": max_illiquid, "מקס' מט״ח": max_fx, "מקס' חו״ל": max_abroad, "מינ' שירות": min_service})
        st.write("**משקולות מטרה:**", {"סטייה": w.dev, "שארפ": w.sharpe, "שירות": w.service})
        st.write("**הנוסחה:**")
        st.code("Objective = w_dev*Deviation - w_sharpe*Sharpe - w_service*(Service/100)", language="text")

        if not solutions.empty:
            st.write("דוגמה: 20 השורות הטובות ביותר (כדי להבין מה האלגוריתם 'רואה'):")
            show = solutions.head(20).copy()
            st.dataframe(show, use_container_width=True, hide_index=True)

