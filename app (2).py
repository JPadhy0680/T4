
import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date
import io
import re
import calendar
from typing import Optional, Set, Tuple

st.set_page_config(page_title="E2B_R3 XML Triage Application", layout="wide")
# Ensure multi-line cells render properly (Report Date, Comments, etc.)
st.markdown("""
<style>
[data-testid="stDataFrame"] div[role="cell"] {white-space: pre-wrap;}
[data-testid="stDataEditor"] div[role="gridcell"] {white-space: pre-wrap;}
</style>
""", unsafe_allow_html=True)
st.title("📊🧠 E2B_R3 XML Triage Application 🛠️ 🚀")

# Version header
# v1.6.4: Per-drug Non-Valid reasons appended into Comment (no separate column)
# - Global FRD/LRD/TD (entire XML).
# - Report Date renders FRD/LRD/TD on new lines.
# - Product-wise validity computed per Celix suspect (FRD/LRD/Event/Drug, Product not Launched).
# - Case validity: if ANY Celix suspect is Valid => case Valid; else Non-Valid (combined reasons).
# - Patient Detail includes "Patient record number" via id[root="2.16.840.1.113883.3.989.2.1.3.7"].

# --------------------- Helpers & Maps ---------------------

def _get_password():
    DEFAULT_PASSWORD = "7064242966"
    try:
        return st.secrets["auth"]["password"]
    except Exception:
        return DEFAULT_PASSWORD

def is_authenticated() -> bool:
    exp = st.session_state.get("auth_expires", None)
    if exp and datetime.now() < exp:
        return True
    return False

if not is_authenticated():
    password = st.text_input("Enter Password to Access App:", type="password", help="Enter the password to unlock the application.")
    if password == _get_password():
        st.session_state["auth_expires"] = datetime.now() + timedelta(hours=24)
        st.success("Access granted for 24 hours.")
    else:
        if password:
            st.warning("Please enter the correct password to proceed.")
        st.stop()

with st.expander("📖 Instructions"):
    st.markdown("""
- Upload **multiple E2B XML files** and **LLT-PT mapping Excel file**.
- (Optional) Upload **Listedness Reference** Excel: columns `Drug Name`, `LLT`.
- Parsed data appears in the **Export & Edit** tab.
- **Listedness (Event-level)** uses **LLT terms** and is omitted for **Non-Valid** cases.
- Only **App Assessment** is editable.
""")

def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", (s or "").strip())

def format_date(date_str: str) -> str:
    if not date_str:
        return ""
    digits = _digits_only(date_str)
    try:
        if len(digits) >= 8:
            dt = datetime.strptime(digits[:8], "%Y%m%d").date()
            return dt.strftime("%d-%b-%Y")
        elif len(digits) >= 6:
            year = int(digits[:4])
            month = int(digits[4:6])
            return datetime(year, month, 1).strftime("%b-%Y")
        elif len(digits) >= 4:
            year = int(digits[:4])
            return f"{year}"
        else:
            return ""
    except Exception:
        return ""

def parse_date_obj(date_str: str) -> Optional[date]:
    if not date_str:
        return None
    digits = _digits_only(date_str)
    try:
        if len(digits) >= 8:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        elif len(digits) >= 6:
            year = int(digits[:4])
            month = int(digits[4:6])
            last_day = calendar.monthrange(year, month)[1]
            return datetime(year, month, last_day).date()
        elif len(digits) >= 4:
            year = int(digits[:4])
            return datetime(year, 12, 31).date()
        else:
            return None
    except Exception:
        return None

def map_reporter(code):
    return {"1": "Physician", "2": "Pharmacist", "3": "Other health professional", "4": "Lawyer", "5": "Consumer or other non-health professional"}.get(code, "Unknown")

def map_gender(code):
    return {"1": "Male", "2": "Female"}.get(code, "Unknown")

def map_outcome(code):
    return {"1": "Recovered/Resolved", "2": "Recovering/Resolving", "3": "Not recovered/Ongoing", "4": "Recovered with sequelae", "5": "Fatal", "0": "Unknown"}.get(code, "Unknown")

AGE_UNIT_MAP = {"a": "year", "b": "month"}
def map_age_unit(raw_unit: str) -> str:
    if raw_unit is None:
        return ""
    ru = str(raw_unit).strip().lower()
    return AGE_UNIT_MAP.get(ru, ru)

UNKNOWN_TOKENS = {"unk", "asku", "unknown"}
def is_unknown(value: str) -> bool:
    if value is None:
        return True
    v = str(value).strip()
    if not v:
        return True
    return v.lower() in UNKNOWN_TOKENS

def clean_value(value: str) -> str:
    return "" if is_unknown(value) else str(value)

def normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r'[^a-z0-9\s\+\-]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def contains_company_product(text: str, company_products: list) -> str:
    norm = normalize_text(text)
    for prod in company_products:
        pnorm = normalize_text(prod)
        if not pnorm:
            continue
        pattern = r'\b' + re.escape(pnorm) + r'\b'
        if re.search(pattern, norm):
            return prod
    return ""

PL_PATTERN = re.compile(r'\b(PL|PLGB|PLNI)\s*([0-9]{5})\s*/\s*([0-9]{4,5})\b', re.IGNORECASE)
def extract_pl_numbers(text: str):
    out = []
    if not text:
        return out
    for m in PL_PATTERN.finditer(text):
        prefix = m.group(1).upper()
        company_code = m.group(2)
        product_code = m.group(3)
        out.append(f"{prefix} {company_code}/{product_code}")
    return out

COMMON_FORM_WORDS = {
    'tablet','tablets','capsule','capsules','injection','mg','mcg','ml',
    'solution','suspension','cream','gel','ointment','spray','sirup','syrup','powder',
    'patch','dose','strength','film','coated','extended','release','prn'
}

def get_mah_name_for_drug(drug_elem, ns) -> str:
    if drug_elem is None:
        return ""
    paths = [
        './/hl7:playingOrganization/hl7:name',
        './/hl7:manufacturerOrganization/hl7:name',
        './/hl7:asManufacturedProduct/hl7:manufacturerOrganization/hl7:name',
    ]
    for p in paths:
        node = drug_elem.find(p, ns)
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return ""

# Patient record number via global id OID
def get_patient_record_number(root, ns) -> str:
    """
    Find hl7:id with root = '2.16.840.1.113883.3.989.2.1.3.7'
    - If nullFlavor='MSK' => 'Masked'
    - Else if extension present => return the extension
    - Else => ''
    """
    target_oid = "2.16.840.1.113883.3.989.2.1.3.7"
    for id_elem in root.findall('.//hl7:id', ns):
        if id_elem.attrib.get('root') == target_oid:
            null_flavor = id_elem.attrib.get('nullFlavor', '')
            ext = id_elem.attrib.get('extension', '')
            if null_flavor == 'MSK':
                return 'Masked'
            if ext:
                return ext.strip()
            return ""
    return ""

company_products = [
    "abiraterone", "apixaban", "apremilast", "bexarotene",
    "clobazam", "clonazepam", "cyanocobalamin", "dabigatran",
    "dapagliflozin", "dimethyl fumarate", "famotidine",
    "fesoterodine", "icatibant", "itraconazole", "linagliptin",
    "linagliptin + metformin", "nintedanib", "pirfenidone",
    "raltegravir", "ranolazine", "rivaroxaban", "saxagliptin",
    "sitagliptin", "tamsulosin + solifenacin", "tapentadol",
    "ticagrelor", "tamsulosin", "solifenacin",
    "cyclogest", "progesterone", "luteum", "amelgen"
]

category2_products = {
    "clobazam", "clonazepam", "cyanocobalamin",
    "famotidine", "itraconazole",
    "tamsulosin", "solifenacin",
    "tapentadol", "cyclogest",
    "progesterone", "luteum", "amelgen"
}

def parse_dd_mmm_yy(s):
    return datetime.strptime(s, "%d-%b-%y").date()

LAUNCH_INFO = {
    "abiraterone": ("launched", parse_dd_mmm_yy("08-Sep-22")),
    "apixaban": ("launched", parse_dd_mmm_yy("26-Feb-25")),
    "apremilast": ("yet", None),
    "bexarotene": ("launched", parse_dd_mmm_yy("19-Jan-23")),
    "clobazam": ("launched", parse_dd_mmm_yy("26-Sep-24")),
    "clonazepam": ("launched", parse_dd_mmm_yy("20-Jan-25")),
    "cyanocobalamin": ("awaited", None),
    "dabigatran": ("yet", None),
    "dapagliflozin": ("launched_by_strength", {10.0: parse_dd_mmm_yy("26-Aug-25"), 5.0: parse_dd_mmm_yy("10-Sep-25")}),
    "dimethyl fumarate": ("launched", parse_dd_mmm_yy("05-Feb-24")),
    "famotidine": ("launched", parse_dd_mmm_yy("21-Feb-25")),
    "fesoterodine": ("yet", None),
    "icatibant": ("launched", parse_dd_mmm_yy("28-Jul-22")),
    "itraconazole": ("awaited", None),
    "linagliptin": ("yet", None),
    "linagliptin + metformin": ("awaited", None),
    "nintedanib": ("awaited", None),
    "pirfenidone": ("launched", parse_dd_mmm_yy("29-Jun-22")),
    "raltegravir": ("awaited", None),
    "ranolazine": ("launched", parse_dd_mmm_yy("20-Jul-23")),
    "rivaroxaban": ("launched_by_strength", {2.5: parse_dd_mmm_yy("02-Apr-24"), 10.0: parse_dd_mmm_yy("23-May-24"), 15.0: parse_dd_mmm_yy("23-May-24"), 20.0: parse_dd_mmm_yy("23-May-24")}),
    "saxagliptin": ("yet", None),
    "sitagliptin": ("yet", None),
    "tamsulosin + solifenacin": ("launched", parse_dd_mmm_yy("08-May-23")),
    "tamsulosin": ("launched", parse_dd_mmm_yy("08-May-23")),
    "solifenacin": ("launched", parse_dd_mmm_yy("08-May-23")),
    "tapentadol": ("launched", parse_dd_mmm_yy("01-Feb-24")),
    "ticagrelor": ("yet", None),
    "cyclogest": ("launched", None),
    "progesterone": ("launched", None),
    "luteum": ("launched", None),
    "amelgen": ("launched", None),
}

def get_launch_date(product_name: str, strength_mg) -> Optional[date]:
    key = normalize_text(product_name)
    info = LAUNCH_INFO.get(key)
    if not info:
        return None
    status, payload = info
    if status == "launched":
        return payload
    if status == "launched_by_strength":
        if isinstance(payload, dict) and payload:
            if strength_mg is not None:
                return payload.get(strength_mg) if strength_mg in payload else payload.get(float(strength_mg))
            return min(payload.values())
        return None
    return None

def get_launch_status(product_name: str) -> Optional[str]:
    key = normalize_text(product_name)
    info = LAUNCH_INFO.get(key)
    if not info:
        return None
    return info[0]

MY_COMPANY_NAME = "celix"
DEFAULT_COMPETITOR_NAMES = {"glenmark", "cipla", "sun pharma", "dr reddy", "dr. reddy", "torrent", "lupin", "intas", "mankind", "micro labs", "zydus"}

def contains_competitor_name(lot_text: str, competitor_names: Set[str]) -> bool:
    if not lot_text:
        return False
    norm = lot_text.lower()
    if MY_COMPANY_NAME.lower() in norm:
        return False
    for name in competitor_names:
        nm = (name or "").lower().strip()
        if nm and nm in norm:
            return True
    return False

# --------------------- GLOBAL FRD/LRD/TD ---------------------
def local_name(tag: str) -> str:
    return tag.split('}')[-1] if '}' in tag else tag

def extract_global_frd_lrd_td(root):
    """
    Global rule (document order, entire XML):
      - TD: first <creationTime value> (if present).
      - Walk all elements:
          keep last <low value> seen;
          first <availabilityTime value> => LRD; FRD = that prior low.
      - Returns both raw and formatted values.
    """
    td_raw = None
    for el in root.iter():
        if local_name(el.tag) == "creationTime":
            val = el.attrib.get("value")
            if val:
                td_raw = val
                break

    last_low_value = None
    lrd_raw = None

    for el in root.iter():
        ln = local_name(el.tag)
        if ln == "low":
            v = el.attrib.get("value")
            if v:
                last_low_value = v
        elif ln == "availabilityTime":
            v = el.attrib.get("value")
            if v:
                lrd_raw = v
                break  # first availabilityTime defines LRD

    frd_fmt = format_date(last_low_value) if last_low_value else ""
    lrd_fmt = format_date(lrd_raw) if lrd_raw else ""
    td_fmt  = format_date(td_raw) if td_raw else ""

    return {
        "FRD_raw": last_low_value or "",
        "LRD_raw": lrd_raw or "",
        "TD_raw":  td_raw or "",
        "FRD": frd_fmt,
        "LRD": lrd_fmt,
        "TD": td_fmt,
    }

# --------------------- UI: Upload & Parse ---------------------

tab1, tab2 = st.tabs(["Upload & Parse", "Export & Edit"])
if "uploader_version" not in st.session_state:
    st.session_state["uploader_version"] = 0
all_rows_display = []
current_date = datetime.now().strftime("%d-%b-%Y")

with tab1:
    st.markdown("### 🔎 Upload Files 🗂️")
    if st.button("Clear Inputs", help="Clear uploaded XMLs and parsed data (keep access)."):
        auth_exp = st.session_state.get("auth_expires")
        for k in ["df_display", "edited_df"]:
            st.session_state.pop(k, None)
        st.session_state["uploader_version"] = st.session_state.get("uploader_version", 0) + 1
        if auth_exp:
            st.session_state["auth_expires"] = auth_exp
        st.rerun()

    ver = st.session_state.get("uploader_version", 0)
    uploaded_files = st.file_uploader("Upload E2B XML files", type=["xml"], accept_multiple_files=True, help="Upload one or more E2B XML files for parsing.", key=f"xml_uploader_{ver}")
    mapping_file = st.file_uploader("Upload LLT-PT Mapping Excel file", type=["xlsx"], help="Upload the MedDRA LLT-PT mapping Excel file.", key=f"map_uploader_{ver}")
    listed_ref_file = st.file_uploader("Upload Listedness Reference (Excel: columns 'Drug Name','LLT')", type=["xlsx"], help="Event-level listedness uses (Drug, LLT) pairs.", key=f"listed_ref_{ver}")

    competitor_names: Set[str] = set(DEFAULT_COMPETITOR_NAMES)

    mapping_df = None
    if mapping_file:
        mapping_df = pd.read_excel(mapping_file, engine="openpyxl")
        if "LLT Code" in mapping_df.columns:
            mapping_df["LLT Code"] = mapping_df["LLT Code"].astype(str).str.strip()

    listed_pairs: Set[Tuple[str, str]] = set()
    if listed_ref_file:
        try:
            ref_df = pd.read_excel(listed_ref_file, engine="openpyxl")
            cols_lower = {c.lower(): c for c in ref_df.columns}
            dn_col = cols_lower.get("drug name")
            llt_col = cols_lower.get("llt")
            if dn_col and llt_col:
                for _, row in ref_df.iterrows():
                    dn = normalize_text(str(row[dn_col])) if pd.notna(row[dn_col]) else ""
                    lt = normalize_text(str(row[llt_col])) if pd.notna(row[llt_col]) else ""
                    if dn and lt:
                        listed_pairs.add((dn, lt))
            else:
                st.warning("Listedness Reference must contain columns 'Drug Name' and 'LLT'.")
        except Exception as e:
            st.error(f"Failed to read Listedness Reference Excel: {e}")
    ref_drugs: Set[str] = {dn for (dn, lt) in listed_pairs} if listed_pairs else set()

    seriousness_map = {
        "resultsInDeath": "Death",
        "isLifeThreatening": "LT",
        "requiresInpatientHospitalization": "Hospital",
        "resultsInPersistentOrSignificantDisability": "Disability",
        "congenitalAnomalyBirthDefect": "Congenital",
        "otherMedicallyImportantCondition": "IME"
    }

    if uploaded_files:
        st.markdown("### ⏳ Parsing Files...")
        progress = st.progress(0)
        total_files = len(uploaded_files)
        parsed_rows = 0

        for idx, uploaded_file in enumerate(uploaded_files, start=1):
            warnings = []
            comments = []

            try:
                tree = ET.parse(uploaded_file)
                root = tree.getroot()
            except Exception as e:
                st.error(f"Failed to parse XML file {getattr(uploaded_file, 'name', '(unnamed)')}: {e}")
                progress.progress(idx / total_files)
                continue

            ns = {'hl7': 'urn:hl7-org:v3', 'xsi': 'http://www.w3.org/2001/XMLSchema-instance'}

            # Sender
            sender_elem = root.find('.//hl7:id[@root="2.16.840.1.113883.3.989.2.1.3.1"]', ns)
            sender_id = clean_value(sender_elem.attrib.get('extension', '') if sender_elem is not None else '')

            # TD fallback (for case age)
            creation_elem = root.find('.//hl7:creationTime', ns)
            creation_raw = creation_elem.attrib.get('value', '') if creation_elem is not None else ''
            td_fallback = clean_value(format_date(creation_raw))

            # Reporter Qualification
            reporter_elem = root.find('.//hl7:asQualifiedEntity/hl7:code', ns)
            reporter_qualification = clean_value(map_reporter(reporter_elem.attrib.get('code', '') if reporter_elem is not None else ''))

            # Patient details
            gender_elem = root.find('.//hl7:administrativeGenderCode', ns)
            gender_mapped = map_gender(gender_elem.attrib.get('code', '') if gender_elem is not None else '')
            gender = clean_value(gender_mapped)

            age_elem = root.find('.//hl7:code[@displayName="age"]/../hl7:value', ns)
            age = ""
            if age_elem is not None:
                age_val = age_elem.attrib.get('value', '')
                raw_unit = age_elem.attrib.get('unit', '')
                unit_text = map_age_unit(raw_unit)
                age_val = clean_value(age_val)
                unit_text_disp = clean_value(unit_text)
                if age_val:
                    try:
                        n = float(age_val)
                        if unit_text_disp in ("year", "month"):
                            unit_text_disp = unit_text_disp + ("s" if n != 1 else "")
                    except Exception:
                        pass
                age = f"{age_val}" + (f" {unit_text_disp}" if age_val and unit_text_disp else "") if age_val else ""

            weight_elem = root.find('.//hl7:code[@displayName="bodyWeight"]/../hl7:value', ns)
            weight_val = clean_value(weight_elem.attrib.get('value', '') if weight_elem is not None else '')
            weight_unit = clean_value(weight_elem.attrib.get('unit', '') if weight_elem is not None else '')
            weight = f"{weight_val}" + (f" {weight_unit}" if weight_val and weight_unit else "") if weight_val else ""

            height_elem = root.find('.//hl7:code[@displayName="height"]/../hl7:value', ns)
            height_val = clean_value(height_elem.attrib.get('value', '') if height_elem is not None else '')
            height_unit = clean_value(height_elem.attrib.get('unit', '') if height_elem is not None else '')
            height = f"{height_val}" + (f" {height_unit}" if height_val and height_unit else "") if height_val else ""

            # Patient initials
            patient_initials = ""
            name_elem = root.find('.//hl7:player1/hl7:name', ns)
            if name_elem is not None:
                if 'nullFlavor' in name_elem.attrib and name_elem.attrib.get('nullFlavor') == 'MSK':
                    patient_initials = "Masked"
                else:
                    init_parts = []
                    for g in name_elem.findall('hl7:given', ns):
                        if g.text and g.text.strip():
                            init_parts.append(g.text.strip()[0].upper())
                    fam = name_elem.find('hl7:family', ns)
                    if fam is not None and fam.text and fam.text.strip():
                        init_parts.append(fam.text.strip()[0].upper())
                    if init_parts:
                        patient_initials = "".join(init_parts)
                    else:
                        if name_elem.text and name_elem.text.strip():
                            patient_initials = name_elem.text.strip()
            patient_initials = clean_value(patient_initials)

            # Patient record number via global id OID
            patient_record_number = get_patient_record_number(root, ns)

            age_group_map = {"0": "Foetus", "1": "Neonate", "2": "Infant", "3": "Child", "4": "Adolescent", "5": "Adult", "6": "Elderly"}
            age_group_elem = root.find('.//hl7:code[@displayName="ageGroup"]/../hl7:value', ns)
            age_group = ""
            if age_group_elem is not None:
                code_val = age_group_elem.attrib.get('code', '')
                null_flavor = age_group_elem.attrib.get('nullFlavor', '')
                if code_val in age_group_map:
                    age_group = age_group_map[code_val]
                elif null_flavor in ["MSK", "UNK", "ASKU", "NI"] or code_val in ["MSK", "UNK", "ASKU", "NI"]:
                    age_group = "[Masked/Unknown]"
            age_group = clean_value(age_group)

            # Build Patient Detail (includes patient record number)
            patient_parts = []
            if patient_record_number: patient_parts.append(f"Patient record number: {patient_record_number}")
            if patient_initials:       patient_parts.append(f"Initials: {patient_initials}")
            if gender:                 patient_parts.append(f"Gender: {gender}")
            if age_group:              patient_parts.append(f"Age Group: {age_group}")
            if age:                    patient_parts.append(f"Age: {age}")
            if height:                 patient_parts.append(f"Height: {height}")
            if weight:                 patient_parts.append(f"Weight: {weight}")
            patient_detail = ", ".join(patient_parts)
            has_any_patient_detail = any([patient_record_number, patient_initials, gender, age_group, age, height, weight])

            # Identify suspect products (value==1)
            suspect_ids = []
            for causality in root.findall('.//hl7:causalityAssessment', ns):
                val_elem = causality.find('.//hl7:value', ns)
                if val_elem is not None and val_elem.attrib.get('code') == '1':
                    subj_id_elem = causality.find('.//hl7:subject2/hl7:productUseReference/hl7:id', ns)
                    if subj_id_elem is not None:
                        suspect_ids.append(subj_id_elem.attrib.get('root', ''))

            product_details_list = []
            case_has_category2 = False
            case_drug_dates_display = []  # [(prod_name, strength, drug_start, drug_stop), ...]
            case_event_dates = []
            case_products_norm: Set[str] = set()

            for drug in root.findall('.//hl7:substanceAdministration', ns):
                id_elem = drug.find('.//hl7:id', ns)
                drug_id = id_elem.attrib.get('root', '') if id_elem is not None else ''
                if drug_id in suspect_ids:
                    name_elem_drug = drug.find('.//hl7:kindOfProduct/hl7:name', ns)
                    raw_drug_text = ""
                    if name_elem_drug is not None:
                        if name_elem_drug.text and name_elem_drug.text.strip():
                            raw_drug_text = name_elem_drug.text.strip()
                        else:
                            orig = name_elem_drug.find('hl7:originalText', ns)
                            if orig is not None and orig.text and orig.text.strip():
                                raw_drug_text = orig.text.strip()
                            else:
                                if 'displayName' in name_elem_drug.attrib:
                                    raw_drug_text = name_elem_drug.attrib.get('displayName', '').strip()
                    if not raw_drug_text:
                        alt_name = drug.find('.//hl7:manufacturedProduct/hl7:name', ns)
                        if alt_name is not None and alt_name.text and alt_name.text.strip():
                            raw_drug_text = alt_name.text.strip()

                    matched_company_prod = contains_company_product(raw_drug_text, company_products)
                    if matched_company_prod:
                        case_products_norm.add(normalize_text(matched_company_prod))
                        if normalize_text(matched_company_prod) in category2_products:
                            case_has_category2 = True

                    text_elem = drug.find('.//hl7:text', ns)
                    dose_elem = drug.find('.//hl7:doseQuantity', ns)
                    dose_val_raw = dose_elem.attrib.get('value', '') if dose_elem is not None else ''
                    dose_unit_raw = dose_elem.attrib.get('unit', '') if dose_elem is not None else ''
                    dose_val = clean_value(dose_val_raw)
                    dose_unit = clean_value(dose_unit_raw)

                    start_elem = drug.find('.//hl7:low', ns)
                    stop_elem = drug.find('.//hl7:high', ns)
                    start_date_str = start_elem.attrib.get('value', '') if start_elem is not None else ''
                    stop_date_str = stop_elem.attrib.get('value', '') if stop_elem is not None else ''
                    start_date_disp = clean_value(format_date(start_date_str))
                    stop_date_disp = clean_value(format_date(stop_date_str))
                    drug_start_obj = parse_date_obj(start_date_str)
                    drug_stop_obj = parse_date_obj(stop_date_str)

                    # Form / lot / MAH display
                    form_elem = drug.find('.//hl7:formCode/hl7:originalText', ns)
                    form_clean = ""
                    if form_elem is not None and form_elem.text:
                        form_clean = clean_value(form_elem.text)

                    lot_elem = drug.find('.//hl7:lotNumberText', ns)
                    lot_clean = ""
                    if lot_elem is not None and lot_elem.text:
                        lot_clean = clean_value(lot_elem.text)

                    mah_name_raw = get_mah_name_for_drug(drug, ns)
                    mah_name_clean = clean_value(mah_name_raw)

                    if matched_company_prod:
                        parts = []
                        display_name = raw_drug_text if raw_drug_text else matched_company_prod.title()
                        display_name = clean_value(display_name)
                        if display_name: parts.append(f"Drug: {display_name}")

                        text_clean = ""
                        if text_elem is not None and text_elem.text:
                            text_clean = clean_value(text_elem.text)
                        if text_clean: parts.append(f"Dosage: {text_clean}")

                        if dose_val or dose_unit:
                            if dose_val and dose_unit:
                                parts.append(f"Dose: {dose_val} {dose_unit}")
                            elif dose_val:
                                parts.append(f"Dose: {dose_val}")
                            elif dose_unit:
                                parts.append(f"Dose Unit: {dose_unit}")

                        if start_date_disp:
                            parts.append(f"Start Date: {start_date_disp}")
                        if stop_date_disp:
                            parts.append(f"Stop Date: {stop_date_disp}")

                        if form_clean:
                            parts.append(f"Formulation: {form_clean}")
                        if lot_clean:
                            parts.append(f"Lot No: {lot_clean}")

                        if mah_name_clean:
                            parts.append(f"MAH: {mah_name_clean}")

                        # PL references (optional)
                        for t in [display_name, text_clean, form_clean, lot_clean]:
                            for pl in extract_pl_numbers(t):
                                parts.append(f"PL: {pl}")

                        if parts:
                            product_details_list.append(" \n ".join(parts))

                        case_drug_dates_display.append((matched_company_prod, None, drug_start_obj, drug_stop_obj))

            seriousness_criteria = list(seriousness_map.keys())
            event_details_list = []
            event_count = 1
            case_has_serious_event = False
            event_listedness_items = []

            # Events summary (FRD/LRD are global)
            for reaction in root.findall('.//hl7:observation', ns):
                code_elem = reaction.find('hl7:code', ns)
                if code_elem is not None and code_elem.attrib.get('displayName') == 'reaction':
                    value_elem = reaction.find('hl7:value', ns)
                    llt_code = value_elem.attrib.get('code', '') if value_elem is not None else ''
                    llt_term, pt_term = "", ""

                    if mapping_df is not None and llt_code:
                        try:
                            llt_code_str = str(llt_code).strip()
                            row = mapping_df[mapping_df['LLT Code'] == llt_code_str]
                            if not row.empty:
                                llt_term = str(row['LLT Term'].values[0])
                                pt_term = str(row['PT Term'].values[0])
                            else:
                                warnings.append(f"LLT code {llt_code_str} not found in mapping file — listedness cannot be assessed for this event.")
                        except Exception as e:
                            warnings.append(f"LLT mapping failed for code {llt_code}: {e}")
                    elif llt_code:
                        warnings.append(f"LLT mapping file not provided — listedness cannot be assessed for LLT code {llt_code}.")

                    def assess_event_listedness(llt_norm: str, suspect_products_norm: Set[str], listed_pairs_set: Set[Tuple[str, str]], ref_drugs_set: Set[str]) -> str:
                        if not listed_pairs_set or not ref_drugs_set:
                            return "Reference not uploaded"
                        suspect_in_ref = {p for p in suspect_products_norm if p in ref_drugs_set}
                        if not suspect_in_ref:
                            return "Reference not updated"
                        for p in suspect_in_ref:
                            if (p, llt_norm) in listed_pairs_set:
                                return "Listed"
                        return "Unlisted"

                    ev_status = "LLT mapping missing"
                    if llt_term:
                        llt_norm = normalize_text(llt_term)
                        ev_status = assess_event_listedness(llt_norm, case_products_norm, listed_pairs, ref_drugs)
                    event_listedness_items.append(f"Event {event_count}: {ev_status}")

                    seriousness_flags = []
                    for criterion in seriousness_criteria:
                        criterion_elem = reaction.find(f'.//hl7:code[@displayName="{criterion}"]/../hl7:value', ns)
                        if criterion_elem is not None and criterion_elem.attrib.get('value') == 'true':
                            seriousness_flags.append(seriousness_map.get(criterion, criterion))
                    seriousness_display = "Non-serious" if not seriousness_flags else ", ".join(seriousness_flags)
                    if seriousness_flags:
                        case_has_serious_event = True

                    outcome_elem = reaction.find('.//hl7:code[@displayName="outcome"]/../hl7:value', ns)
                    outcome = map_outcome(outcome_elem.attrib.get('code', '') if outcome_elem is not None else '')
                    outcome = clean_value(outcome)

                    evt_low = reaction.find('.//hl7:effectiveTime/hl7:low', ns)
                    evt_high = reaction.find('.//hl7:effectiveTime/hl7:high', ns)
                    evt_low_str = evt_low.attrib.get('value', '') if evt_low is not None else ''
                    evt_high_str = evt_high.attrib.get('value', '') if evt_high is not None else ''
                    evt_low_disp = clean_value(format_date(evt_low_str))
                    evt_high_disp = clean_value(format_date(evt_high_str))
                    evt_low_obj = parse_date_obj(evt_low_str)
                    evt_high_obj = parse_date_obj(evt_high_str)
                    case_event_dates.append(("event", evt_low_obj, evt_high_obj))

                    details_parts = [f"Event {event_count}: {llt_term} ({pt_term})", f"Seriousness: {seriousness_display}"]
                    if outcome:
                        details_parts.append(f"Outcome: {outcome}")
                    if evt_low_disp:
                        details_parts.append(f"Event Start: {evt_low_disp}")
                    if evt_high_disp:
                        details_parts.append(f"Event End: {evt_high_disp}")
                    event_details_list.append("; ".join(details_parts))
                    event_count += 1

            event_details_combined_display = "\n".join(event_details_list)

            # Reportability (unchanged)
            reportability = "Category 2, serious, reportable case" if (case_has_serious_event and case_products_norm.intersection(category2_products)) else "Non-Reportable"

            # ---------- GLOBAL FRD/LRD/TD for Report Date ----------
            global_dates = extract_global_frd_lrd_td(root)
            frd_disp = global_dates["FRD"]
            lrd_disp = global_dates["LRD"]
            td_disp  = global_dates["TD"] or td_fallback

            # Case Age (days) based on raw TD if available
            case_age_days = ""
            if global_dates["TD_raw"]:
                td_obj = parse_date_obj(global_dates["TD_raw"])
                if td_obj:
                    case_age_days = (datetime.now().date() - td_obj).days
                    if case_age_days < 0:
                        case_age_days = 0

            # ---------- Product-wise validity checks & aggregation ----------
            per_product_reasons = {}  # prod_name(normalized) -> list of reasons
            has_celix_suspect = bool(case_products_norm)
            case_block_reason = None
            if not has_any_patient_detail:
                case_block_reason = "No patient details"
            elif not has_celix_suspect and bool(suspect_ids):
                case_block_reason = "Non-company product"

            # Will append to Comment only if needed
            per_drug_nonvalid_display = ""

            if case_block_reason is None and has_celix_suspect:
                frd_raw_obj = parse_date_obj(global_dates["FRD_raw"]) if global_dates["FRD_raw"] else None
                lrd_raw_obj = parse_date_obj(global_dates["LRD_raw"]) if global_dates["LRD_raw"] else None

                # Collect per-product reasons
                for prod, strength_mg, drug_start, drug_stop in case_drug_dates_display:
                    if not prod:
                        continue
                    reasons = []
                    status = get_launch_status(prod)
                    if status in ("yet", "awaited"):
                        reasons.append("Product not Launched")
                    else:
                        ld = get_launch_date(prod, strength_mg)
                        if ld:
                            if frd_raw_obj and frd_raw_obj < ld:
                                reasons.append("FRD")
                            if lrd_raw_obj and lrd_raw_obj < ld:
                                reasons.append("LRD")
                            event_prior = any(
                                (evt_start and evt_start < ld) or (evt_stop and evt_stop < ld)
                                for _, evt_start, evt_stop in case_event_dates
                            )
                            if event_prior:
                                reasons.append("Event")
                            if (drug_start and drug_start < ld) or (drug_stop and drug_stop < ld):
                                reasons.append("Drug")
                    per_product_reasons[normalize_text(prod)] = reasons

                # Case-level aggregation: Valid if ANY product has no reasons
                any_product_valid = any(len(reasons) == 0 for reasons in per_product_reasons.values())
                num_products = len(per_product_reasons)

                if any_product_valid:
                    validity_value = "Valid"
                else:
                    # All products non-valid => combined reason at case level
                    combined = set()
                    for reasons in per_product_reasons.values():
                        for r in reasons:
                            combined.add(r)
                    if not combined:
                        validity_value = "Valid"  # safety net
                    else:
                        if "Product not Launched" in combined and len(combined) == 1:
                            validity_value = "Non-Valid (Product not Launched)"
                        else:
                            combined.discard("Product not Launched")
                            if combined:
                                validity_value = f"Non-Valid (Drug exposure prior to Launch; {', '.join(sorted(combined))})"
                            else:
                                validity_value = "Non-Valid (Drug exposure prior to Launch)"

                    # NEW: if multiple suspects and all are non-valid, render per-drug reasons in Comment
                    if num_products >= 2:
                        lines = []
                        # Map normalized product to a readable display name
                        for prod_norm, reasons in per_product_reasons.items():
                            # Find a display name from case_drug_dates_display or fallback to title-case of norm
                            display_name = None
                            for p, _, _, _ in case_drug_dates_display:
                                if normalize_text(p) == prod_norm:
                                    display_name = p.title()
                                    break
                            if not display_name:
                                display_name = prod_norm.title()
                            if reasons:
                                lines.append(f"{display_name}: {', '.join(sorted(set(reasons)))}")
                            else:
                                lines.append(f"{display_name}: Valid")
                        per_drug_nonvalid_display = "\n".join(lines)

            else:
                # Case-level blocked reasons take precedence
                validity_value = f"Non-Valid ({case_block_reason})" if case_block_reason else "Valid"

            # Narrative
            narrative_elem = root.find('.//hl7:code[@code="PAT_ADV_EVNT"]/../hl7:text', ns)
            narrative_full_raw = narrative_elem.text if narrative_elem is not None else ''
            narrative_full = clean_value(narrative_full_raw)

            # Compose Comment field:
            # - existing comments (semicolon-separated as before)
            # - append per-drug non-valid block (multi-line) when applicable
            base_comments = "; ".join(sorted(set(comments))) if comments else ""
            if per_drug_nonvalid_display:
                if base_comments:
                    comment_display = base_comments + "\n\nPer-drug Non-Valid Reasons:\n" + per_drug_nonvalid_display
                else:
                    comment_display = "Per-drug Non-Valid Reasons:\n" + per_drug_nonvalid_display
            else:
                comment_display = base_comments

            # Listedness suppressed for Non-Valid
            if isinstance(validity_value, str) and validity_value.startswith("Non-Valid"):
                reportability = "NA"
                event_listedness_items = []
            listedness_event_level_display = "; ".join(event_listedness_items)

            if ("Reference not uploaded" in listedness_event_level_display) or ("Reference not updated" in listedness_event_level_display):
                warnings.append("Listedness reference is missing or incomplete—please upload an updated (Drug Name, LLT) list.")

            # ---------- Report Date (new lines) ----------
            report_date_parts = []
            if frd_disp: report_date_parts.append(f"FRD: {frd_disp}")
            if lrd_disp: report_date_parts.append(f"LRD: {lrd_disp}")
            if td_disp:  report_date_parts.append(f"TD: {td_disp}")
            report_date_display = "\n".join(report_date_parts)

            # Append row
            all_rows_display.append({
                'SL No': idx,
                'Date': current_date,
                'Sender ID': sender_id,
                'Report Date': report_date_display,
                'Case Age (days)': case_age_days,
                'Reporter Qualification': reporter_qualification,
                'Patient Detail': patient_detail,
                'Product Detail': " \n ".join(product_details_list),
                'Event Details': event_details_combined_display,
                'Narrative': narrative_full,
                'Validity': validity_value,
                'Comment': comment_display,  # <-- includes per-drug non-valid reasons when applicable
                'Listedness (Event-level)': listedness_event_level_display,
                'Reportability': reportability,
                'App Assessment': '',
                'Parsing Warnings': "; ".join(warnings) if warnings else ""
            })

            parsed_rows += 1
            progress.progress(idx / total_files)

        st.success(f"Parsing complete ✅ — Files processed: {total_files}, Rows created: {parsed_rows}")

# --------------------- UI: Export & Edit ---------------------

with tab2:
    st.markdown("### 📋 Parsed Data Table 🗃️")
    if all_rows_display:
        df_display = pd.DataFrame(all_rows_display)
        show_full_narrative = st.checkbox("Show full narrative (may be long)", value=True)
        if not show_full_narrative:
            df_display['Narrative'] = df_display['Narrative'].astype(str).str.slice(0, 1000)
        preferred_order = [
            'SL No','Date','Sender ID','Report Date','Case Age (days)','Reporter Qualification',
            'Patient Detail','Product Detail','Event Details','Narrative','Validity',
            'Comment','Listedness (Event-level)','Reportability','App Assessment','Parsing Warnings'
        ]
        df_display = df_display[[c for c in preferred_order if c in df_display.columns]]
        editable_cols = ['App Assessment']
        disabled_cols = [col for col in df_display.columns if col not in editable_cols]
        edited_df = st.data_editor(df_display, num_rows="dynamic", use_container_width=True, disabled=disabled_cols)
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            edited_df.to_excel(writer, index=False, sheet_name="Parsed Data")
        st.download_button("⬇️ Download Excel", excel_buffer.getvalue(), "parsed_data.xlsx")
    else:
        st.info("No data available yet. Please upload files in the first tab.")

st.markdown("""
**Developed by Jagamohan** _Disclaimer: App is in developmental stage, validate before using the data._
""", unsafe_allow_html=True)








