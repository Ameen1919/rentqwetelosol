import streamlit as st
import pandas as pd
import io, gzip, tempfile, time, os, base64, hashlib, json, traceback
from datetime import date, timedelta, datetime
from dateutil.relativedelta import relativedelta
from hijri_converter import convert
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
import requests
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import numpy as np

st.set_page_config(page_title="نظام إدارة الإيجارات", page_icon="🏢", layout="wide")

st.markdown("""
<style>
    html, body, [class*="css"] { direction: rtl !important; text-align: right !important; }
    .stApp { direction: rtl !important; }
    .stButton, .stSelectbox, .stTextInput, .stNumberInput, .stDateInput, .stRadio, .stCheckbox { direction: rtl !important; text-align: right !important; }
    h1, h2, h3, h4, h5, h6 { direction: rtl !important; text-align: right !important; }
    .stTabs [data-baseweb="tab-list"] { direction: rtl !important; }
    input, textarea { direction: rtl !important; text-align: right !important; }
    .stDownloadButton button { direction: rtl !important; }
    .streamlit-expanderHeader { direction: rtl !important; text-align: right !important; }
    .stAlert { direction: rtl !important; text-align: right !important; }
    [data-testid="stMetric"] { direction: rtl !important; text-align: right !important; }
    [data-testid="stDataFrame"] { direction: ltr !important; }
    [data-testid="stDataFrame"] [role="columnheader"] { text-align: center !important; }
    [data-testid="stSidebar"] { direction: rtl !important; text-align: right !important; }
    [data-testid="stSidebarCollapseButton"], button[data-testid="baseButton-headerNoPadding"] {
        position: absolute !important; right: 12px !important; left: auto !important; top: 12px !important; z-index: 999 !important; }
    [data-testid="stSidebarCollapseButton"] svg, button[data-testid="baseButton-headerNoPadding"] svg { transform: scaleX(-1) !important; }
    [data-testid="stSidebar"][aria-expanded="true"] [data-testid="stSidebarCollapseButton"] {
        background-color: rgba(255, 255, 255, 0.2) !important; border-radius: 8px !important; padding: 4px !important; }
    section[data-testid="stSidebar"][aria-expanded="false"] + section [data-testid="stSidebarCollapseButton"], [data-testid="collapsedControl"] {
        position: fixed !important; top: 12px !important; right: 12px !important; left: auto !important;
        background-color: #4A90E2 !important; border-radius: 8px !important; padding: 8px 12px !important;
        box-shadow: 0 2px 8px rgba(0,0,0,0.15) !important; z-index: 9999 !important; }
    [data-testid="collapsedControl"] svg { transform: scaleX(-1) !important; color: white !important; }
    [data-testid="collapsedControl"]:hover { background-color: #357ABD !important; }
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] > div:first-child { padding-top: 30px !important; }
    [data-testid="stSidebar"] .stRadio > label { padding: 8px 12px !important; border-radius: 6px !important; display: block !important; margin-bottom: 4px !important; }
    [data-testid="stSidebar"] .stRadio > label:hover { background-color: rgba(255, 255, 255, 0.15) !important; }
    [data-testid="stSidebar"] .stRadio > label:has(input:checked) { background-color: rgba(255, 255, 255, 0.25) !important; font-weight: bold !important; }
</style>
""", unsafe_allow_html=True)

try:
    TURSO_URL = st.secrets["TURSO_URL"]; TURSO_TOKEN = st.secrets["TURSO_TOKEN"]
except Exception:
    TURSO_URL = os.environ.get("TURSO_URL", ""); TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
try:
    TG_FILE_BOT_TOKEN = st.secrets["telegram"]["bot_token"]; TG_FILE_CHAT_ID = st.secrets["telegram"]["chat_id"]
except Exception:
    TG_FILE_BOT_TOKEN = os.environ.get("TG_FILE_BOT_TOKEN", ""); TG_FILE_CHAT_ID = os.environ.get("TG_FILE_CHAT_ID", "")

if not TURSO_URL or not TURSO_TOKEN:
    st.error("⚠️ يجب إعداد TURSO_URL و TURSO_TOKEN في Secrets"); st.stop()

def _clean_turso_url(u):
    u = u.strip().rstrip("/").replace("libsql://", "https://").replace("wss://", "https://")
    u = u.replace(".aws-us-east-1.", ".").replace(".aws-eu-west-1.", ".").replace(".aws-ap-northeast-1.", ".")
    if not u.startswith("http"): u = "https://" + u
    return u

TURSO_URL_CLEAN = _clean_turso_url(TURSO_URL)
TURSO_PIPELINE = f"{TURSO_URL_CLEAN}/v2/pipeline"


# ============================================================
# Turso HTTP Client with SMART RETRY + SAFETY
# ============================================================
class DictRow:
    def __init__(self, columns, values):
        self._columns = list(columns); self._values = list(values); self._map = dict(zip(self._columns, self._values))
    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._map.get(key)
    def __getattr__(self, name):
        if name.startswith('_'): raise AttributeError(name)
        if name in self._map: return self._map[name]
        raise AttributeError(f"no column {name}")
    def __iter__(self): return iter(self._values)
    def __len__(self): return len(self._values)
    def __contains__(self, k): return k in self._map
    def keys(self): return self._columns
    def values(self): return self._values
    def items(self): return self._map.items()
    def get(self, k, d=None): return self._map.get(k, d)
    def to_dict(self): return dict(self._map)


def _encode_arg(p):
    if p is None: return {"type": "null"}
    if isinstance(p, bool): return {"type": "integer", "value": "1" if p else "0"}
    if isinstance(p, int): return {"type": "integer", "value": str(p)}
    if isinstance(p, float): return {"type": "float", "value": p}
    if isinstance(p, bytes): return {"type": "blob", "base64": base64.b64encode(p).decode()}
    return {"type": "text", "value": str(p)}


def _decode_cell(cell):
    if cell is None: return None
    t = cell.get("type")
    if t == "null": return None
    if t == "integer": 
        v = cell.get("value"); return int(v) if v is not None else None
    if t == "float":
        v = cell.get("value"); return float(v) if v is not None else None
    if t == "text": return cell.get("value")
    if t == "blob": return base64.b64decode(cell.get("base64", ""))
    return cell.get("value")


class WrappedCursor:
    def __init__(self, conn):
        self._conn = conn; self._rows = []; self._idx = 0
        self._columns = []; self.lastrowid = None; self.rowcount = 0; self.description = None

    def execute(self, sql, params=None):
        params = params or []
        if not isinstance(params, (list, tuple)): params = [params]
        sql_str = sql.strip(); sql_upper = sql_str.upper()

        if sql_upper.startswith("PRAGMA"):
            self._rows = []; self._columns = []; self._idx = 0; self.lastrowid = None; return self

        args = [_encode_arg(p) for p in params]
        payload = {"requests": [
            {"type": "execute", "stmt": {"sql": sql_str, "args": args, "want_rows": True}},
            {"type": "close"}
        ]}
        # ✅ Smart retry حسب نوع العملية
        is_insert = sql_upper.startswith("INSERT")
        r = self._conn._safe_post(payload, timeout=120, is_write=is_insert)
        if not r.ok:
            try: err_data = r.json()
            except: err_data = r.text[:300]
            raise Exception(f"Turso HTTP {r.status_code}: {err_data}")
        try: data = r.json()
        except: raise Exception("رد غير صالح من Turso")
        results = data.get("results", [])
        if not results: raise Exception("رد Turso فاضي")
        first = results[0]
        if first.get("type") == "error":
            em = first.get("error", {}).get("message", "خطأ"); raise Exception(f"Turso: {em}")
        resp = first.get("response", {}).get("result", {})
        cols_info = resp.get("cols", [])
        self._columns = [c.get("name") for c in cols_info]
        self.description = [(c.get("name"),) for c in cols_info]
        self._rows = [DictRow(self._columns, [_decode_cell(c) for c in row]) for row in resp.get("rows", [])]
        self._idx = 0
        if is_insert and "RETURNING" in sql_upper and self._rows and 'id' in self._rows[0].keys():
            self.lastrowid = self._rows[0]['id']
        return self

    def executemany(self, sql, params_list):
        for p in params_list: self.execute(sql, p)
        return self
    def fetchone(self):
        if self._idx < len(self._rows):
            r = self._rows[self._idx]; self._idx += 1; return r
        return None
    def fetchall(self):
        rows = self._rows[self._idx:]; self._idx = len(self._rows); return rows
    def fetchmany(self, size=1):
        rows = self._rows[self._idx:self._idx + size]; self._idx += len(rows); return rows
    def close(self): pass
    def __iter__(self): return iter(self._rows)


class WrappedConnection:
    def __init__(self, url, auth_token):
        self._url = url; self._token = auth_token; self._session = None; self._create_session()

    def _create_session(self):
        if self._session:
            try: self._session.close()
            except: pass
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        })
        adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=10, max_retries=0, pool_block=False)
        self._session.mount("https://", adapter); self._session.mount("http://", adapter)

    def _safe_post(self, payload, timeout=120, max_retries=3, is_write=False):
        """Smart retry: INSERT → مرة واحدة فقط (أمان ضد التكرار)، غيره → 3 محاولات"""
        if is_write: max_retries = 1
        last_err = None
        for attempt in range(max_retries):
            try:
                return self._session.post(self._url, json=payload, timeout=timeout)
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                retryable = any(x in err_str for x in ['protocol','connection','timeout','reset','broken','eof','ssl','chunked','incomplete']) or isinstance(e, (
                    requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout, requests.exceptions.RequestException,
                    ConnectionResetError, BrokenPipeError, EOFError))
                if not retryable: raise
                if attempt < max_retries - 1:
                    if any(x in err_str for x in ['protocol','reset','broken','eof']):
                        try: self._create_session()
                        except: pass
                    time.sleep(min(2 ** attempt, 4)); continue
                raise Exception(f"فشل الاتصال بعد {max_retries} محاولات: {last_err}")

    def cursor(self): return WrappedCursor(self)
    def execute(self, sql, params=None):
        cur = WrappedCursor(self); cur.execute(sql, params); return cur
    def executescript(self, script):
        for stmt in script.split(';'):
            s = stmt.strip()
            if s:
                try: self.execute(s)
                except: pass

    def execute_batch(self, queries):
        stmts = []
        for sql, params in queries:
            args = [_encode_arg(p) for p in (params or [])]
            stmts.append({"type": "execute", "stmt": {"sql": sql, "args": args, "want_rows": True}})
        stmts.append({"type": "close"})
        r = self._safe_post({"requests": stmts}, timeout=120, is_write=False)
        if not r.ok: raise Exception(f"Turso HTTP {r.status_code}")
        data = r.json(); results = data.get("results", []); output = []
        for res in results[:-1]:
            if res.get("type") == "error": output.append([]); continue
            resp = res.get("response", {}).get("result", {})
            cols = [c.get("name") for c in resp.get("cols", [])]
            output.append([DictRow(cols, [_decode_cell(c) for c in row]) for row in resp.get("rows", [])])
        return output

    def execute_write_batch(self, queries, is_insert=False):
        if not queries: return 0
        stmts = []
        for sql, params in queries:
            args = [_encode_arg(p) for p in (params or [])]
            stmts.append({"type": "execute", "stmt": {"sql": sql, "args": args, "want_rows": False}})
        stmts.append({"type": "close"})
        r = self._safe_post({"requests": stmts}, timeout=180, is_write=is_insert)
        if not r.ok:
            try: err_data = r.json()
            except: err_data = r.text[:300]
            raise Exception(f"Turso HTTP {r.status_code}: {err_data}")
        results = r.json().get("results", [])
        for res in results[:-1]:
            if res.get("type") == "error":
                raise Exception(f"Turso: {res.get('error',{}).get('message','خطأ')}")
        return len(queries)

    def commit(self): pass
    def rollback(self): pass
    def close(self):
        try: self._session.close()
        except: pass


def get_conn():
    if 'db_conn' not in st.session_state or st.session_state.db_conn is None:
        st.session_state.db_conn = WrappedConnection(TURSO_PIPELINE, TURSO_TOKEN)
    return st.session_state.db_conn


# ============================================================
# Telegram
# ============================================================
def is_telegram_file_id(s):
    return bool(s) and str(s).startswith(('AgAC','BQAC','BAAC','AgAD','BAAD','CAAC'))

def upload_to_telegram(data_bytes, filename, caption="مرفق"):
    if not TG_FILE_BOT_TOKEN or not TG_FILE_CHAT_ID: return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_FILE_BOT_TOKEN}/sendDocument",
                          files={'document': (filename, data_bytes)},
                          data={'chat_id': TG_FILE_CHAT_ID, 'caption': caption}, timeout=300)
        if r.status_code == 200: return r.json().get('result', {}).get('document', {}).get('file_id')
    except: pass
    return None

def download_from_telegram(file_id):
    if not TG_FILE_BOT_TOKEN or not file_id: return None
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TG_FILE_BOT_TOKEN}/getFile?file_id={file_id}", timeout=60).json()
        if not resp.get('ok'): return None
        r = requests.get(f"https://api.telegram.org/file/bot{TG_FILE_BOT_TOKEN}/{resp['result']['file_path']}", timeout=300)
        if r.status_code == 200: return r.content
    except: pass
    return None

def save_uploaded_file(uploader_obj, caption="مرفق"):
    if not uploader_obj: return None
    try:
        data = uploader_obj.read()
        return upload_to_telegram(data, uploader_obj.name, caption) if data else None
    except: return None

def render_attachment_download(att_value, key_prefix, label="📥 تحميل المرفق"):
    if not att_value: return
    s = str(att_value)
    if is_telegram_file_id(s):
        if st.button(label, key=f"{key_prefix}_dl"):
            with st.spinner("جاري التحميل..."):
                data = download_from_telegram(s)
                if data: st.download_button("اضغط هنا للحفظ", data=data, file_name=f"{key_prefix}_file",
                                            mime="application/octet-stream", key=f"{key_prefix}_save")
                else: st.error("فشل تحميل المرفق")


# ============================================================
# Utilities
# ============================================================
def safe_float(v, default=0.0):
    try:
        if v is None or pd.isna(v): return default
        if isinstance(v, str):
            v = v.replace(',','').strip()
            if v == '': return default
        return float(v)
    except: return default

def format_currency(value):
    val = safe_float(value, 0.0)
    return f"{int(val):,}" if val == int(val) else f"{val:,.2f}"

def rtl_dataframe(df, key=None, **kwargs):
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    date_cols = [c for c in df.columns if 'تاريخ' in c or 'date' in c.lower() or 'بداية' in c or 'نهاية' in c or 'استحقاق' in c]
    number_like = [c for c in df.columns if any(kw in c for kw in ['المبلغ','المدفوع','المتبقي','الضريبة','إيجار','التأمين','الرقم','نسبة'])]
    ltr_cols = list(set(numeric_cols + date_cols + number_like))
    rtl_cols = [c for c in df.columns if c not in ltr_cols]
    styled = df.style
    if ltr_cols: styled = styled.set_properties(subset=ltr_cols, **{'text-align':'left','direction':'ltr'})
    if rtl_cols: styled = styled.set_properties(subset=rtl_cols, **{'text-align':'right','direction':'rtl'})
    st.dataframe(styled, use_container_width=True, key=key, **kwargs)

def display_dataframe_with_reorder(df, key_prefix):
    columns = list(df.columns)
    default = st.session_state.get(f"{key_prefix}_order", columns)
    selected = st.multiselect("اختر الأعمدة وترتيبها", options=columns, default=default, key=f"{key_prefix}_cols")
    if selected:
        df_out = df[selected]; st.session_state[f"{key_prefix}_order"] = selected
    else: df_out = df
    rtl_dataframe(df_out, key=f"{key_prefix}_rtl")
    return df_out, selected

def download_arabic_font():
    fp = "Amiri-Regular.ttf"
    if not os.path.exists(fp):
        try:
            r = requests.get("https://github.com/aliftype/amiri/raw/main/fonts/Amiri-Regular.ttf", timeout=10)
            if r.status_code == 200:
                with open(fp, "wb") as f: f.write(r.content)
            else: return None
        except: return None
    return fp

def setup_arabic_font():
    fp = download_arabic_font()
    if fp and os.path.exists(fp):
        try: pdfmetrics.registerFont(TTFont('Amiri', fp)); return 'Amiri'
        except: pass
    return 'Helvetica'

def reshape_arabic_text(text): return get_display(arabic_reshaper.reshape(str(text)))
def parse_currency(v): return safe_float(v, 0.0)

def parse_date_safe(v, default=None):
    if not v: return default or date.today()
    if isinstance(v, date): return v
    try: return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except:
        try: return datetime.fromisoformat(str(v)).date()
        except: return default or date.today()

def parse_hijri_date(s):
    if not s: raise ValueError("تاريخ فارغ")
    s = str(s).strip().replace('/','-').replace('.','-'); parts = s.split('-')
    if len(parts) != 3: raise ValueError(f"صيغة غير صحيحة: {s}")
    try: nums = [int(p) for p in parts]
    except: raise ValueError(f"يجب أن تكون أرقاماً: {s}")
    if nums[0] > 1300: y, m, d = nums
    else: d, m, y = nums
    if not (1 <= m <= 12): raise ValueError(f"شهر غير صحيح: {m}")
    if not (1 <= d <= 30): raise ValueError(f"يوم غير صحيح: {d}")
    if not (1300 <= y <= 1600): raise ValueError(f"سنة هجرية غير صحيحة: {y}")
    g = convert.Hijri(y, m, d).to_gregorian(); return date(g.year, g.month, g.day)

def hijri_to_gregorian(hs): return parse_hijri_date(hs)

def gregorian_to_hijri(gd):
    if not gd: return ""
    if isinstance(gd, str): gd = parse_date_safe(gd)
    h = convert.Gregorian(gd.year, gd.month, gd.day).to_hijri()
    return f"{h.day:02d}-{h.month:02d}-{h.year}"

def add_hijri_months(y, m, d, months):
    total = (y * 12 + (m - 1)) + months
    return total // 12, (total % 12) + 1, min(d, 30)

def wrap_text_for_pdf(text, n):
    s = str(text)
    if len(s) <= n: return [s]
    out, rem = [], s
    while len(rem) > n:
        c = rem[:n]; si = c.rfind(' ')
        if si > n // 3: out.append(rem[:si].strip()); rem = rem[si:].strip()
        else: out.append(c); rem = rem[n:]
    if rem: out.append(rem)
    return out

DATE_COLUMNS = ['تاريخ الاستحقاق','تاريخ السداد','بداية الفترة','نهاية الفترة','أقدم دفعة غير مسددة']

def export_df_to_pdf(df, title, file_name, columns_order=None, extra_info=None, landscape_mode=False):
    if columns_order:
        valid = [c for c in columns_order if c in df.columns]; df = df[valid] if valid else df.copy()
    else: df = df.copy()
    df_num = df.copy()
    for c in df_num.columns:
        try: df_num[c] = df_num[c].apply(parse_currency)
        except: pass
    numeric_cols_set = set()
    for col in df.columns:
        try:
            test = df[col].apply(lambda x: safe_float(x, None) if x is not None else None)
            ts = pd.Series([x for x in test if x is not None])
            if len(ts) > 0 and ts.notna().all(): numeric_cols_set.add(col)
        except: pass
    buf = io.BytesIO(); pagesize = landscape(A4) if landscape_mode else A4
    c = canvas.Canvas(buf, pagesize=pagesize); w, h = pagesize; fn = setup_arabic_font()
    c.setFont(fn, 10); c.setFillColor(colors.HexColor("#4A90E2"))
    c.rect(0, h-30, w, 30, fill=1, stroke=0); c.setFillColor(colors.white); c.setFont(fn, 16)
    c.drawCentredString(w/2, h-20, reshape_arabic_text(title))
    y_extra = h - 50
    if extra_info:
        c.setFillColor(colors.black); c.setFont(fn, 12)
        c.drawCentredString(w/2, y_extra, reshape_arabic_text(extra_info)); y_extra -= 20
    cols = list(df.columns); headers = ["م"] + cols; widths = []
    for idx, col in enumerate(headers):
        if col == "م": widths.append(30); continue
        ml = len(reshape_arabic_text(str(col)))
        for v in df[col].tolist():
            s = format_currency(v) if isinstance(v, (int, float)) and not pd.isna(v) else (str(v) if not pd.isna(v) else "")
            ml = max(ml, len(reshape_arabic_text(s)))
        if col in ['المبلغ','المدفوع','المتبقي','المبلغ شامل الضريبة','مبلغ الضريبة','المبلغ غير شامل الضريبة','الإيجار السنوي','إجمالي المتبقي']: widths.append(95)
        elif col in DATE_COLUMNS or 'هجري' in str(col): widths.append(115)
        elif col in ['المستأجر','اسم المستأجر']: widths.append(140)
        elif col in ['العقار','اسم العقار']: widths.append(120)
        elif col in ['المنطقة']: widths.append(80)
        elif col in ['الحالة']: widths.append(65)
        elif col in ['رقم السند','رقم العقد']: widths.append(90)
        elif col in ['طريقة الدفع','طريقة السداد']: widths.append(85)
        else: widths.append(min(max(ml * 6 + 15, 65), 130))
    tw = sum(widths); mw = w - 40
    if tw > mw: sf = mw / tw; widths = [x * sf for x in widths]; tw = mw
    xs = max((w - tw) / 2, 20); y = (y_extra - 20) if extra_info else (h - 60)
    c.setFont(fn, 8); c.setFillColor(colors.HexColor("#f0f0f0")); c.rect(xs, y-15, tw, 25, fill=1, stroke=0)
    c.setFillColor(colors.black); xc = xs + tw
    for i, hd in enumerate(headers):
        cw = widths[i]; xr = xc; xl = xc - cw
        c.drawCentredString((xl+xr)/2, y-3, reshape_arabic_text(hd)); xc -= cw
    y -= 30; sn = 1; lh = 11
    for _, row in df.iterrows():
        row_lines = []
        for col in cols:
            v = row[col]
            vs = format_currency(v) if isinstance(v, (int, float)) and not pd.isna(v) else (str(v) if not pd.isna(v) else "")
            cw = widths[cols.index(col) + 1]
            if col in DATE_COLUMNS or 'هجري' in str(col): lines = [vs]
            else: lines = wrap_text_for_pdf(vs, max(int(cw / 7), 5))
            row_lines.append(lines)
        max_l = max((len(x) for x in row_lines), default=1); rh = max_l * lh + 6
        if y - rh < 40:
            c.showPage(); c.setFont(fn, 8); y = h - 50
            c.setFillColor(colors.HexColor("#f0f0f0")); c.rect(xs, y-15, tw, 25, fill=1, stroke=0)
            c.setFillColor(colors.black); xc = xs + tw
            for i, hd in enumerate(headers):
                cw = widths[i]; xr = xc; xl = xc - cw
                c.drawCentredString((xl+xr)/2, y-3, reshape_arabic_text(hd)); xc -= cw
            y -= 30
        c.setFillColor(colors.white); c.rect(xs, y - rh + 5, tw, rh, fill=1, stroke=0); c.setFillColor(colors.black)
        cw = widths[0]; xr = xs + tw; xl = xr - cw
        c.drawCentredString((xl+xr)/2, y - (rh/2) + 3, str(sn)); sn += 1; xc = xr - cw
        for i, col in enumerate(cols, 1):
            cw = widths[i]; xr = xc; xl = xc - cw
            for li, line in enumerate(row_lines[i-1]):
                c.setFont(fn, 7 if (col in DATE_COLUMNS or 'هجري' in str(col)) else 8)
                c.drawRightString(xr - 5, y - 3 - li * lh, reshape_arabic_text(line))
            c.setFont(fn, 8); xc -= cw
        c.setStrokeColor(colors.grey); c.setLineWidth(0.5)
        c.line(xs, y+5, xs+tw, y+5); c.line(xs, y-rh+5, xs+tw, y-rh+5)
        xc = xs + tw
        for i in range(len(headers)): c.line(xc, y+5, xc, y-rh+5); xc -= widths[i]
        c.line(xs, y+5, xs, y-rh+5); y -= rh
    c.line(xs, y+5, xs+tw, y+5); y -= 5
    c.setFillColor(colors.HexColor("#e8f0fe")); c.rect(xs, y-15, tw, 22, fill=1, stroke=0); c.setFillColor(colors.black)
    cw = widths[0]; xr = xs + tw; xl = xr - cw
    c.drawCentredString((xl+xr)/2, y-7, reshape_arabic_text("الإجمالي")); xc = xr - cw
    for i, col in enumerate(cols, 1):
        cw = widths[i]; xr = xc; xl = xc - cw
        if col in numeric_cols_set:
            try: c.drawRightString(xr-5, y-7, format_currency(df_num[col].sum()))
            except: pass
        xc -= cw
    c.save(); buf.seek(0)
    or_label = "أفقي" if landscape_mode else "عمودي"
    st.download_button(f"تحميل PDF ({or_label})", data=buf, file_name=file_name, mime="application/pdf")

def export_tax_pdf(df, title, file_name, columns_order=None, landscape_mode=True):
    if columns_order:
        valid = [c for c in columns_order if c in df.columns]; df = df[valid] if valid else df.copy()
    else: df = df.copy()
    buf = io.BytesIO(); pagesize = landscape(A4) if landscape_mode else A4
    c = canvas.Canvas(buf, pagesize=pagesize); w, h = pagesize; fn = setup_arabic_font()
    c.setFont(fn, 10); c.setFillColor(colors.HexColor("#4A90E2"))
    c.rect(0, h-30, w, 30, fill=1, stroke=0); c.setFillColor(colors.white); c.setFont(fn, 16)
    c.drawCentredString(w/2, h-20, reshape_arabic_text(title))
    cols = list(df.columns); headers = ["م"] + cols; widths = []
    for col in headers:
        if col == "م": widths.append(25)
        elif col in ['المبلغ شامل الضريبة','مبلغ الضريبة','المبلغ غير شامل الضريبة']: widths.append(90)
        elif col == 'نسبة الضريبة': widths.append(60)
        elif col in ['بداية الفترة','نهاية الفترة']: widths.append(115)
        elif col in ['اسم المستأجر','المستأجر']: widths.append(140)
        elif col in ['رقم العقد']: widths.append(90)
        elif col == 'طريقة الدفع': widths.append(85)
        else: widths.append(max(len(reshape_arabic_text(col))*5, 75))
    tw = sum(widths); mw = w - 40
    if tw > mw: sf = mw / tw; widths = [x*sf for x in widths]; tw = mw
    xs = max((w - tw) / 2, 20); y = h - 60
    c.setFont(fn, 7); c.setFillColor(colors.HexColor("#f0f0f0")); c.rect(xs, y-18, tw, 28, fill=1, stroke=0)
    c.setFillColor(colors.black); xc = xs + tw
    for i, hd in enumerate(headers):
        cw = widths[i]; xr = xc; xl = xc - cw
        c.drawCentredString((xl+xr)/2, y-3, reshape_arabic_text(hd)); xc -= cw
    y -= 30; c.setFont(fn, 8); sn = 1; lh = 10
    for _, row in df.iterrows():
        row_lines = []
        for col in cols:
            v = row[col]
            vs = format_currency(v) if isinstance(v, (int, float)) and not pd.isna(v) else (str(v) if not pd.isna(v) else "")
            cw = widths[cols.index(col) + 1]
            if col in ['بداية الفترة','نهاية الفترة']: lines = [vs]
            else: lines = wrap_text_for_pdf(vs, max(int(cw / 6.5), 5))
            row_lines.append(lines)
        max_l = max((len(l) for l in row_lines), default=1); rh = max_l * lh + 5
        if y - rh < 40:
            c.showPage(); c.setFont(fn, 7); y = h - 50
            c.setFillColor(colors.HexColor("#f0f0f0")); c.rect(xs, y-18, tw, 28, fill=1, stroke=0)
            c.setFillColor(colors.black); xc = xs + tw
            for i, hd in enumerate(headers):
                cw = widths[i]; xr = xc; xl = xc - cw
                c.drawCentredString((xl+xr)/2, y-3, reshape_arabic_text(hd)); xc -= cw
            y -= 30; c.setFont(fn, 8)
        c.setFillColor(colors.white); c.rect(xs, y-rh+5, tw, rh, fill=1, stroke=0); c.setFillColor(colors.black)
        cw = widths[0]; xr = xs + tw; xl = xr - cw
        c.drawCentredString((xl+xr)/2, y-rh/2+2, str(sn)); sn += 1; xc = xr - cw
        for i, col in enumerate(cols, 1):
            cw = widths[i]; xr = xc; xl = xc - cw
            for li, line in enumerate(row_lines[i-1]):
                c.setFont(fn, 6.5 if col in ['بداية الفترة','نهاية الفترة'] else 8)
                c.drawRightString(xr-4, y-3-li*lh, reshape_arabic_text(line))
            c.setFont(fn, 8); xc -= cw
        c.setStrokeColor(colors.grey); c.setLineWidth(0.5)
        c.line(xs, y+5, xs+tw, y+5); c.line(xs, y-rh+5, xs+tw, y-rh+5)
        xc = xs + tw
        for i in range(len(headers)): c.line(xc, y+5, xc, y-rh+5); xc -= widths[i]
        c.line(xs, y+5, xs, y-rh+5); y -= rh
    c.save(); buf.seek(0)
    or_label = "أفقي" if landscape_mode else "عمودي"
    st.download_button(f"تحميل PDF ({or_label})", data=buf, file_name=file_name, mime="application/pdf")

def print_receipt(receipt_id):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('''SELECT r.receipt_number, COALESCE(t.name, 'مستأجر محذوف') as name,
                   r.amount, r.receipt_date, r.payment_method, r.notes
                   FROM receipts r LEFT JOIN tenants t ON r.tenant_id = t.id WHERE r.id = ?''', [receipt_id])
    r = cur.fetchone()
    if not r: return None
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4); w, h = A4; fn = setup_arabic_font()
    c.setFont(fn, 12); c.setFillColor(colors.HexColor("#4A90E2"))
    c.rect(0, h-40, w, 40, fill=1, stroke=0); c.setFillColor(colors.white)
    c.setFont(fn, 18); c.drawCentredString(w/2, h-25, reshape_arabic_text("سند قبض"))
    c.setFont(fn, 12); c.setFillColor(colors.black); y = h - 80
    for lbl, val in [("رقم السند:", r['receipt_number']),("اسم المستأجر:", r['name']),
                     ("المبلغ:", format_currency(r['amount'])),("تاريخ السداد:", r['receipt_date']),
                     ("طريقة الدفع:", r['payment_method']),("ملاحظات:", r['notes'] or "لا يوجد")]:
        c.drawRightString(w-100, y, reshape_arabic_text(f"{lbl} {val}")); y -= 25
    c.save(); buf.seek(0); return buf.getvalue()


# ============================================================
# Init DB + UNIQUE Indexes (حماية من التكرار)
# ============================================================
def ensure_column(cur, table, col_name, col_type="TEXT", default=None):
    try:
        dflt = f" DEFAULT {default}" if default is not None else ""
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}{dflt}")
    except: pass

@st.cache_resource
def init_db():
    conn = get_conn(); cur = conn.cursor()
    cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    cur.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                   password_hash TEXT, role TEXT DEFAULT 'مشاهد', permissions TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS tenants (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                   phone TEXT, national_id TEXT, address TEXT, region TEXT, notes TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS properties (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                   description TEXT, address TEXT, region TEXT, area TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS contracts (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER, property_id INTEGER,
                   contract_number TEXT UNIQUE, start_date TEXT, end_date TEXT, rent_amount REAL, interval_months INTEGER DEFAULT 1,
                   deposit_amount REAL, notes TEXT, status TEXT DEFAULT 'نشط', tax_included INTEGER DEFAULT 0, tax_rate REAL DEFAULT 0.15,
                   contract_file TEXT, calendar_type TEXT DEFAULT 'ميلادي', hijri_start_date TEXT, hijri_end_date TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER, tenant_id INTEGER,
                   due_date TEXT, amount REAL, paid_amount REAL DEFAULT 0, paid_date TEXT, status TEXT DEFAULT 'مستحق', notes TEXT,
                   attachment TEXT, is_temporary INTEGER DEFAULT 0, temporary_note TEXT, is_advance INTEGER DEFAULT 0)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_number TEXT, tenant_id INTEGER,
                   contract_id INTEGER, payment_id INTEGER, amount REAL, receipt_date TEXT, payment_method TEXT, notes TEXT, attachment TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER, alert_text TEXT,
                   alert_date TEXT, is_read INTEGER DEFAULT 0)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS contract_pricing_tiers (id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER,
                   start_date TEXT, end_date TEXT, annual_rent REAL, notes TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS additional_fees (id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER,
                   fee_name TEXT, amount REAL, frequency TEXT DEFAULT 'مرة واحدة', tax_included INTEGER DEFAULT 0, notes TEXT,
                   created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS contract_discounts (id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER,
                   discount_type TEXT DEFAULT 'نسبة', discount_value REAL, start_date TEXT, end_date TEXT, reason TEXT,
                   created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    for col, typ, dflt in [('is_advance','INTEGER','0'),('is_temporary','INTEGER','0'),('temporary_note','TEXT',None),('attachment','TEXT',None)]:
        ensure_column(cur, 'payments', col, typ, dflt)
    for col, typ, dflt in [('calendar_type','TEXT',"'ميلادي'"),('hijri_start_date','TEXT',None),('hijri_end_date','TEXT',None),('contract_file','TEXT',None)]:
        ensure_column(cur, 'contracts', col, typ, dflt)
    ensure_column(cur, 'receipts', 'attachment', 'TEXT', None)
    ensure_column(cur, 'users', 'permissions', 'TEXT', "'{}'")
    
    # ✅ حماية من التكرار (UNIQUE Indexes)
    try: cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_number ON receipts(receipt_number)")
    except: pass
    try: cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_contracts_number ON contracts(contract_number)")
    except: pass
    try: cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)")
    except: pass
    
    cur.execute("SELECT COUNT(*) as c FROM users")
    r = cur.fetchone()
    if r and r['c'] == 0:
        cur.execute("INSERT INTO users (username, password_hash, role, permissions) VALUES (?, ?, ?, ?)",
                    ['admin', hashlib.sha256('admin123'.encode()).hexdigest(), 'مدير', json.dumps({})])

init_db()

PAGE_KEYS = ["لوحة التحكم","إدارة البيانات","الدفعات","سندات القبض","التقارير","عقود منتهية","المستخدمون","الإعدادات","نسخ احتياطي"]

def get_default_permissions(role):
    if role == 'مدير': return {p: True for p in PAGE_KEYS}
    if role == 'محاسب': return {"لوحة التحكم":True,"إدارة البيانات":True,"الدفعات":True,"سندات القبض":True,"التقارير":True,"عقود منتهية":True,"المستخدمون":False,"الإعدادات":False,"نسخ احتياطي":False}
    return {p: False for p in PAGE_KEYS}

def load_permissions(uid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT role, permissions FROM users WHERE id = ?", [uid]); r = cur.fetchone()
    if not r: return {}
    try: perms = json.loads(r['permissions'] or '{}')
    except: perms = {}
    dp = get_default_permissions(r['role'])
    for k in PAGE_KEYS:
        if k not in perms: perms[k] = dp.get(k, False)
    return perms

def save_permissions(uid, perms):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET permissions = ? WHERE id = ?", [json.dumps(perms), uid]); st.cache_data.clear()

def has_permission(uid, page): return bool(uid) and load_permissions(uid).get(page, False)

def check_login(u, p):
    conn = get_conn(); cur = conn.cursor()
    ph = hashlib.sha256(p.strip().encode()).hexdigest()
    cur.execute("SELECT id, username, role FROM users WHERE username = ? COLLATE NOCASE AND password_hash = ?", [u.strip(), ph])
    usr = cur.fetchone()
    return {'id': usr['id'], 'username': usr['username'], 'role': usr['role']} if usr else None

def load_settings():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings"); rows = cur.fetchall()
    s = {}
    for r in rows:
        try: s[r['key']] = int(r['value']) if r['key'] == 'font_size' else r['value']
        except: s[r['key']] = r['value']
    defaults = {'font_size':18,'primary_color':'#4A90E2','secondary_color':'#F5A623','background_color':'#F8F9FA','logo':None,'company_name':'نظام إدارة الإيجارات'}
    for k, v in defaults.items():
        if k not in s: s[k] = v
    return s

def save_setting(k, v):
    conn = get_conn(); cur = conn.cursor()
    vs = '' if v is None else (base64.b64encode(v).decode('utf-8') if isinstance(v, bytes) else str(v))
    cur.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", [k, vs])
    st.cache_data.clear()

def load_logo_data():
    lb = load_settings().get('logo')
    if lb:
        try: return base64.b64decode(lb)
        except: return None
    return None

settings = load_settings()
font_size = settings['font_size']; primary_color = settings['primary_color']
secondary_color = settings['secondary_color']; background_color = settings['background_color']
logo_data = load_logo_data()


# ============================================================
# Login
# ============================================================
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False; st.session_state.user_info = None

if not st.session_state.logged_in:
    st.markdown(f"""<style>.login-box{{max-width:400px;margin:auto;padding:40px;background:white;border-radius:10px;
        box-shadow:0 0 20px rgba(0,0,0,0.1);text-align:center;}}.login-box h2{{color:{primary_color};margin-bottom:20px;}}
        </style><div class="login-box"><h2>تسجيل الدخول</h2>""", unsafe_allow_html=True)
    with st.form("login_form"):
        u = st.text_input("اسم المستخدم").strip(); p = st.text_input("كلمة المرور", type="password").strip()
        if st.form_submit_button("دخول"):
            usr = check_login(u, p)
            if usr: st.session_state.logged_in = True; st.session_state.user_info = usr; st.rerun()
            else: st.error("بيانات خاطئة")
    st.markdown("</div>", unsafe_allow_html=True); st.stop()

user_info = st.session_state.user_info
current_user_id = user_info['id']; current_role = user_info['role']
user_permissions = load_permissions(current_user_id)

st.markdown(f"""<style>html,body,[class*="css"]{{direction:rtl;text-align:right;font-size:{font_size}px;}}
    .stApp{{background-color:{background_color};}}.stSidebar{{background-color:{primary_color};color:white;}}
    .stSidebar [data-testid="stMarkdown"]{{color:white;}}.stSidebar .stRadio label,.stSidebar .stSelectbox label{{color:white!important;}}
    .stButton>button{{background-color:{secondary_color};color:white;border-radius:8px;border:none;padding:8px 16px;font-weight:bold;}}
    .stButton>button:hover{{background-color:{primary_color};color:white;}}h1,h2,h3,h4{{color:{primary_color};}}
    .stMetric{{background-color:white;padding:15px;border-radius:10px;box-shadow:0 2px 5px rgba(0,0,0,0.1);text-align:center;}}
    .stDataFrame,.stTable{{background-color:white;border-radius:10px;padding:10px;box-shadow:0 2px 5px rgba(0,0,0,0.1);}}
    </style>""", unsafe_allow_html=True)

if logo_data: st.sidebar.image(logo_data, width=150)
else: st.sidebar.markdown("🏢 **نظام الإدارة**")
st.sidebar.markdown(f"**المستخدم:** {user_info['username']}"); st.sidebar.markdown(f"**الدور:** {current_role}")
st.sidebar.markdown("---")
if st.sidebar.button("تسجيل الخروج"):
    st.session_state.logged_in = False; st.session_state.user_info = None; st.session_state.db_conn = None; st.rerun()
st.sidebar.markdown("---")
col_up, col_down = st.sidebar.columns(2)
with col_up:
    if st.button('➕ تكبير', use_container_width=True): save_setting('font_size', min(24, font_size + 1)); st.rerun()
with col_down:
    if st.button('➖ تصغير', use_container_width=True): save_setting('font_size', max(10, font_size - 1)); st.rerun()
st.sidebar.markdown("---")
menu = st.sidebar.radio("القائمة الرئيسية", PAGE_KEYS)


# ============================================================
# Core helpers
# ============================================================
def generate_receipt_number(): return f"RCP-{int(time.time())}-{int(time.time()*1000)%10000}"

def generate_contract_number():
    ts = date.today().strftime("%Y%m%d")
    while True:
        num = f"CTR-{ts}-{int(time.time() * 1000) % 100000:05d}"
        cur = get_conn().cursor(); cur.execute("SELECT id FROM contracts WHERE contract_number = ?", [num])
        if not cur.fetchone(): return num

def get_pricing_tiers(cid):
    cur = get_conn().cursor(); cur.execute("SELECT * FROM contract_pricing_tiers WHERE contract_id=? ORDER BY start_date", [cid])
    return [r.to_dict() for r in cur.fetchall()]

def add_pricing_tier(cid, sd, ed, ar, notes=""):
    cur = get_conn().cursor()
    cur.execute('INSERT INTO contract_pricing_tiers (contract_id, start_date, end_date, annual_rent, notes) VALUES (?,?,?,?,?)',
                [cid, sd.isoformat(), ed.isoformat(), ar, notes]); st.cache_data.clear()

def delete_pricing_tier(tid):
    cur = get_conn().cursor(); cur.execute("DELETE FROM contract_pricing_tiers WHERE id=?", [tid]); st.cache_data.clear()

def get_additional_fees(cid):
    cur = get_conn().cursor(); cur.execute("SELECT * FROM additional_fees WHERE contract_id=? ORDER BY id", [cid])
    return [r.to_dict() for r in cur.fetchall()]

def add_additional_fee(cid, name, amt, freq, tax, notes=""):
    cur = get_conn().cursor()
    cur.execute('INSERT INTO additional_fees (contract_id, fee_name, amount, frequency, tax_included, notes) VALUES (?,?,?,?,?,?)',
                [cid, name, amt, freq, tax, notes]); st.cache_data.clear()

def delete_additional_fee(fid):
    cur = get_conn().cursor(); cur.execute("DELETE FROM additional_fees WHERE id=?", [fid]); st.cache_data.clear()

def get_discounts(cid):
    cur = get_conn().cursor(); cur.execute("SELECT * FROM contract_discounts WHERE contract_id=? ORDER BY start_date", [cid])
    return [r.to_dict() for r in cur.fetchall()]

def add_discount(cid, dt, dv, sd, ed, reason=""):
    cur = get_conn().cursor()
    cur.execute('INSERT INTO contract_discounts (contract_id, discount_type, discount_value, start_date, end_date, reason) VALUES (?,?,?,?,?,?)',
                [cid, dt, dv, sd.isoformat(), ed.isoformat(), reason]); st.cache_data.clear()

def delete_discount(did):
    cur = get_conn().cursor(); cur.execute("DELETE FROM contract_discounts WHERE id=?", [did]); st.cache_data.clear()

def calc_discount_for_date(cid, dd):
    ds = get_discounts(cid); tp, ta = 0.0, 0.0
    for d in ds:
        if d['start_date'] <= dd <= d['end_date']:
            if d['discount_type'] == 'نسبة': tp += d['discount_value']
            else: ta += d['discount_value']
    return tp, ta

def get_annual_rent_for_date(cid, td, default):
    for t in get_pricing_tiers(cid):
        if t['start_date'] <= td <= t['end_date']: return t['annual_rent']
    return default

def create_payment_schedule(cid, tid, sd, ed, ra, im, calendar_type='ميلادي'):
    cur = get_conn().cursor(); cnt = 0
    if calendar_type == 'هجري':
        sh = gregorian_to_hijri(sd); d_h, m_h, y_h = map(int, sh.split('-'))
        cy, cm, cd = y_h, m_h, d_h; safety = 0
        while safety < 500:
            safety += 1
            try: g = hijri_to_gregorian(f"{cd:02d}-{cm:02d}-{cy}")
            except: break
            if g > ed: break
            ar_val = get_annual_rent_for_date(cid, g.isoformat(), ra)
            base = ar_val * im / 12.0
            dp, da = calc_discount_for_date(cid, g.isoformat())
            final = max(0, base * (1 - dp / 100.0) - da)
            cur.execute('INSERT INTO payments (contract_id, tenant_id, due_date, amount) VALUES (?,?,?,?)',
                        [cid, tid, g.isoformat(), final]); cnt += 1
            cy, cm, cd = add_hijri_months(cy, cm, cd, im)
    else:
        step = relativedelta(months=im); curd = sd
        while curd <= ed:
            ar_val = get_annual_rent_for_date(cid, curd.isoformat(), ra)
            base = ar_val * im / 12.0
            dp, da = calc_discount_for_date(cid, curd.isoformat())
            final = max(0, base * (1 - dp / 100.0) - da)
            cur.execute('INSERT INTO payments (contract_id, tenant_id, due_date, amount) VALUES (?,?,?,?)',
                        [cid, tid, curd.isoformat(), final]); curd += step; cnt += 1
    return cnt

def create_temporary_payment_schedule(cid, tid, sd, ed, total_amount, interval_months, note=""):
    step = relativedelta(months=interval_months); curd = sd
    pay = total_amount * interval_months / 12.0; cnt = 0; cur = get_conn().cursor()
    while curd <= ed:
        cur.execute('''INSERT INTO payments (contract_id, tenant_id, due_date, amount, status, notes, is_temporary, temporary_note)
                       VALUES (?, ?, ?, ?, 'مستحق', ?, 1, ?)''', [cid, tid, curd.isoformat(), pay, note, note])
        curd += step; cnt += 1
    return cnt

def add_single_temporary_payment(cid, tid, dd, amt, note=""):
    cur = get_conn().cursor()
    cur.execute('''INSERT INTO payments (contract_id, tenant_id, due_date, amount, status, notes, is_temporary, temporary_note)
                   VALUES (?,?,?,?, 'مستحق', ?, 1, ?)''', [cid, tid, dd.isoformat(), amt, note, note]); st.cache_data.clear()

def get_temporary_payments(cid):
    cur = get_conn().cursor(); cur.execute("SELECT * FROM payments WHERE contract_id=? AND is_temporary=1 ORDER BY due_date", [cid])
    return [r.to_dict() for r in cur.fetchall()]

def delete_temporary_payment(pid):
    cur = get_conn().cursor(); cur.execute("DELETE FROM payments WHERE id=? AND is_temporary=1", [pid]); st.cache_data.clear()

def delete_all_temporary_payments(cid):
    cur = get_conn().cursor(); cur.execute("DELETE FROM payments WHERE contract_id=? AND is_temporary=1", [cid]); st.cache_data.clear()

def get_unread_alerts(tid=None):
    cur = get_conn().cursor()
    if tid: cur.execute("SELECT alert_text, alert_date FROM alerts WHERE tenant_id = ? AND is_read = 0 ORDER BY alert_date DESC", [tid])
    else: cur.execute("SELECT a.alert_text, a.alert_date, t.name FROM alerts a JOIN tenants t ON a.tenant_id = t.id WHERE a.is_read = 0 ORDER BY a.alert_date DESC")
    return cur.fetchall()

def get_all_expired_contracts():
    cur = get_conn().cursor()
    cur.execute('''SELECT c.id, c.contract_number, t.name as tenant_name, c.end_date, c.tenant_id,
                   p.name as prop_name, c.rent_amount, c.interval_months
                   FROM contracts c JOIN tenants t ON c.tenant_id = t.id JOIN properties p ON c.property_id = p.id
                   WHERE c.end_date < date('now')
                   AND c.tenant_id NOT IN (SELECT tenant_id FROM contracts WHERE status='نشط' AND end_date >= date('now'))
                   ORDER BY c.end_date DESC''')
    return [r.to_dict() for r in cur.fetchall()]

def get_expiring_contracts(days=60):
    cur = get_conn().cursor(); target = (date.today() + timedelta(days=days)).isoformat()
    cur.execute('''SELECT c.id, c.contract_number, t.name as tenant_name, t.phone as tenant_phone,
                   t.region as tenant_region, c.end_date, c.tenant_id, p.name as prop_name,
                   c.rent_amount, c.interval_months,
                   CAST(julianday(c.end_date) - julianday('now') AS INTEGER) as days_left
                   FROM contracts c JOIN tenants t ON c.tenant_id = t.id JOIN properties p ON c.property_id = p.id
                   WHERE c.status='نشط' AND c.end_date >= date('now') AND c.end_date <= ? ORDER BY c.end_date ASC''', [target])
    return [r.to_dict() for r in cur.fetchall()]

def get_tenant_contracts_history(tid):
    cur = get_conn().cursor()
    cur.execute('''SELECT c.id, c.contract_number, c.start_date, c.end_date, c.rent_amount,
                   c.status, c.calendar_type, c.hijri_start_date, c.hijri_end_date, p.name as prop_name
                   FROM contracts c JOIN properties p ON c.property_id = p.id
                   WHERE c.tenant_id = ? ORDER BY c.start_date DESC''', [tid])
    return [r.to_dict() for r in cur.fetchall()]

def get_total_dues_until(target_date, only_overdue=False):
    conn = get_conn()
    q1 = "SELECT COALESCE(SUM(amount - paid_amount), 0) as t FROM payments WHERE due_date <= ?" + (" AND (amount - paid_amount) > 0" if only_overdue else "")
    res = conn.execute_batch([
        (q1, [target_date.isoformat()]),
        ("SELECT COALESCE(SUM(amount - paid_amount), 0) as t FROM payments WHERE (amount - paid_amount) > 0", []),
        ("SELECT COUNT(*) as c FROM payments WHERE (amount - paid_amount) > 0", []),
        ("SELECT COUNT(*) as c FROM payments WHERE due_date < ? AND (amount - paid_amount) > 0", [target_date.isoformat()])
    ])
    return (res[0][0]['t'] if res[0] else 0, res[1][0]['t'] if res[1] else 0,
            res[2][0]['c'] if res[2] else 0, res[3][0]['c'] if res[3] else 0)


# ============================================================
# Cached loaders
# ============================================================
@st.cache_data(ttl=60)
def load_tenants():
    cur = get_conn().cursor()
    cur.execute('''SELECT t.id as "الرقم", t.name as "الاسم", t.phone as "الهاتف",
        t.national_id as "رقم الهوية / الإقامة", t.address as "العنوان", t.region as "المنطقة",
        COALESCE(c.contract_number, 'لا يوجد عقد') as "رقم العقد",
        CASE WHEN c.id IS NULL THEN 'بدون عقد' WHEN c.end_date < date('now') THEN 'منتهي' ELSE 'ساري' END as "حالة العقد"
        FROM tenants t LEFT JOIN contracts c ON c.tenant_id = t.id AND c.status = 'نشط' ORDER BY t.name''')
    rows = cur.fetchall()
    if not rows: return pd.DataFrame(columns=["الرقم","الاسم","الهاتف","رقم الهوية / الإقامة","العنوان","المنطقة","رقم العقد","حالة العقد"])
    return pd.DataFrame([list(r) for r in rows], columns=list(rows[0].keys()))

@st.cache_data(ttl=60)
def load_properties():
    cur = get_conn().cursor()
    cur.execute('SELECT id as "الرقم", name as "الاسم", description as "الوصف", address as "العنوان", region as "المنطقة", area as "المساحة" FROM properties')
    rows = cur.fetchall()
    if not rows: return pd.DataFrame(columns=["الرقم","الاسم","الوصف","العنوان","المنطقة","المساحة"])
    return pd.DataFrame([list(r) for r in rows], columns=list(rows[0].keys()))

@st.cache_data(ttl=60)
def load_contracts():
    cur = get_conn().cursor()
    cur.execute("""SELECT c.id as 'الرقم', c.contract_number as 'رقم العقد', c.start_date as 'تاريخ البداية',
           c.end_date as 'تاريخ النهاية', c.rent_amount as 'قيمة الإيجار السنوي', c.interval_months as 'دورية السداد (شهور)',
           c.deposit_amount as 'التأمين', c.status as 'الحالة', c.tax_included as 'شامل الضريبة', c.tax_rate as 'نسبة الضريبة',
           COALESCE(c.calendar_type, 'ميلادي') as 'التقويم', t.name as 'اسم المستأجر', p.name as 'اسم العقار'
           FROM contracts c JOIN tenants t ON c.tenant_id = t.id JOIN properties p ON c.property_id = p.id""")
    rows = cur.fetchall()
    if not rows: return pd.DataFrame()
    return pd.DataFrame([list(r) for r in rows], columns=list(rows[0].keys()))

@st.cache_data(ttl=60)
def load_payments(sf='الكل'):
    cur = get_conn().cursor()
    q = '''SELECT pay.id as 'الرقم', t.name as 'المستأجر', p.name as 'العقار', pay.due_date as 'تاريخ الاستحقاق',
        pay.amount as 'المبلغ', pay.paid_amount as 'المدفوع', (pay.amount - pay.paid_amount) as 'المتبقي',
        pay.status as 'الحالة', pay.paid_date as 'تاريخ السداد', pay.is_temporary as 'مؤقت',
        t.region as 'المنطقة', t.id as 'معرف_المستأجر' 
        FROM payments pay JOIN tenants t ON pay.tenant_id = t.id
        JOIN contracts c ON pay.contract_id = c.id JOIN properties p ON c.property_id = p.id'''
    if sf != 'الكل': q += " WHERE pay.status = ?"; cur.execute(q, [sf])
    else: cur.execute(q)
    rows = cur.fetchall()
    if not rows: return pd.DataFrame()
    return pd.DataFrame([list(r) for r in rows], columns=list(rows[0].keys()))

@st.cache_data(ttl=60)
def load_receipts():
    cur = get_conn().cursor()
    cur.execute('''SELECT r.id as 'الرقم', r.receipt_number as 'رقم السند',
        COALESCE(t.name, 'مستأجر محذوف') as 'المستأجر', r.amount as 'المبلغ', r.receipt_date as 'التاريخ',
        r.payment_method as 'طريقة الدفع', r.notes as 'ملاحظات', COALESCE(t.region, '-') as 'المنطقة',
        r.tenant_id as 'معرف_المستأجر'
        FROM receipts r LEFT JOIN tenants t ON r.tenant_id = t.id ORDER BY r.receipt_date DESC''')
    rows = cur.fetchall()
    if not rows: return pd.DataFrame()
    return pd.DataFrame([list(r) for r in rows], columns=list(rows[0].keys()))


# ============================================================
# Excel imports (BATCH)
# ============================================================
def import_tenants_from_excel(f):
    try:
        df = pd.read_excel(f)
        if "الاسم" not in df.columns: st.error("يجب عمود 'الاسم'"); return
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT name FROM tenants"); existing = {r['name'] for r in cur.fetchall()}
        to_insert = []
        for _, row in df.iterrows():
            n = str(row.get("الاسم", "")).strip()
            if not n or n in existing: continue
            to_insert.append([n,
                str(row.get("الهاتف", "")).strip() if "الهاتف" in df.columns else "",
                str(row.get("رقم الهوية / الإقامة", "")).strip() if "رقم الهوية / الإقامة" in df.columns else "",
                str(row.get("العنوان", "")).strip() if "العنوان" in df.columns else "",
                str(row.get("المنطقة", "")).strip() if "المنطقة" in df.columns else "",
                str(row.get("ملاحظات", "")).strip() if "ملاحظات" in df.columns else ""])
        if not to_insert: st.warning("⚠️ لا يوجد صفوف جديدة"); return
        progress = st.progress(0); status = st.empty()
        BATCH_SIZE = 50
        batches = [to_insert[i:i+BATCH_SIZE] for i in range(0, len(to_insert), BATCH_SIZE)]
        sql = 'INSERT INTO tenants (name, phone, national_id, address, region, notes) VALUES (?,?,?,?,?,?)'
        total_done = 0
        for i, batch in enumerate(batches, 1):
            status.text(f"📦 دفعة {i}/{len(batches)} ({len(batch)} صف)...")
            conn.execute_write_batch([(sql, row) for row in batch], is_insert=True)
            total_done += len(batch); progress.progress(i / len(batches))
        status.text(""); progress.empty(); st.cache_data.clear()
        st.success(f"✅ تم استيراد {total_done} مستأجر"); time.sleep(1); st.rerun()
    except Exception as e:
        st.error(f"❌ خطأ: {e}")
        with st.expander("تفاصيل"): st.code(traceback.format_exc())

def import_properties_from_excel(f):
    try:
        df = pd.read_excel(f)
        if "الاسم" not in df.columns: st.error("يجب عمود 'الاسم'"); return
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT name FROM properties"); existing = {r['name'] for r in cur.fetchall()}
        to_insert = []
        for _, row in df.iterrows():
            n = str(row["الاسم"]).strip()
            if not n or n in existing: continue
            to_insert.append([n,
                str(row.get("الوصف", "")).strip() if "الوصف" in df.columns else "",
                str(row.get("العنوان", "")).strip() if "العنوان" in df.columns else "",
                str(row.get("المنطقة", "")).strip() if "المنطقة" in df.columns else "",
                str(row.get("المساحة", "")).strip() if "المساحة" in df.columns else ""])
        if not to_insert: st.warning("⚠️ لا يوجد صفوف جديدة"); return
        progress = st.progress(0); status = st.empty()
        BATCH_SIZE = 50
        batches = [to_insert[i:i+BATCH_SIZE] for i in range(0, len(to_insert), BATCH_SIZE)]
        sql = 'INSERT INTO properties (name, description, address, region, area) VALUES (?,?,?,?,?)'
        total_done = 0
        for i, batch in enumerate(batches, 1):
            status.text(f"📦 دفعة {i}/{len(batches)}...")
            conn.execute_write_batch([(sql, row) for row in batch], is_insert=True)
            total_done += len(batch); progress.progress(i / len(batches))
        status.text(""); progress.empty(); st.cache_data.clear()
        st.success(f"✅ تم استيراد {total_done} عقار"); time.sleep(1); st.rerun()
    except Exception as e:
        st.error(f"❌ خطأ: {e}")
        with st.expander("تفاصيل"): st.code(traceback.format_exc())

def parse_excel_date(val):
    if val is None or pd.isna(val): return None
    if isinstance(val, datetime): return val.date()
    if isinstance(val, date): return val
    if isinstance(val, (int, float, np.integer, np.floating)):
        try:
            iv = int(val)
            return (datetime(1899, 12, 30) + timedelta(days=iv)).date() if iv > 0 else None
        except: return None
    try:
        d = pd.to_datetime(str(val), errors='coerce')
        return d.date() if not pd.isna(d) else None
    except: return None

def clean_id_value(val):
    if val is None or pd.isna(val): return None
    s = str(val).strip()
    if s in ("", "0", "0.0", "nan", "NaN", "None"): return None
    try: return int(float(s))
    except: return None

def clean_text_value(val):
    if val is None or pd.isna(val): return ""
    s = str(val).strip()
    return "" if s in ("0", "0.0", "nan", "NaN", "None") else s

def import_contracts_from_excel(f):
    try:
        df = pd.read_excel(f, sheet_name=0)
        for col in ["اسم المستأجر","اسم العقار","تاريخ البداية","تاريخ النهاية"]:
            if col not in df.columns: st.error(f"يجب عمود '{col}'"); return
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id, name FROM tenants"); tenants_data = cur.fetchall()
        cur.execute("SELECT id, name FROM properties"); props_data = cur.fetchall()
        tbyid = {t['id']: t['name'] for t in tenants_data}; pbyid = {p['id']: p['name'] for p in props_data}
        tnc = {}; tni = {}
        for t in tenants_data:
            tnc[t['name']] = tnc.get(t['name'], 0) + 1
            if t['name'] not in tni: tni[t['name']] = t['id']
        pnc = {}; pni = {}
        for p in props_data:
            pnc[p['name']] = pnc.get(p['name'], 0) + 1
            if p['name'] not in pni: pni[p['name']] = p['id']
        imp = 0; errors = []
        ht = "رقم المستأجر" in df.columns; hp = "رقم العقار" in df.columns
        for idx, row in df.iterrows():
            try:
                cnum = clean_text_value(row.get("رقم العقد", None)) if "رقم العقد" in df.columns else ""
                if not cnum: cnum = generate_contract_number()
                else:
                    cur.execute("SELECT id FROM contracts WHERE contract_number=?", [cnum])
                    if cur.fetchone(): errors.append(f"صف {idx+2}: رقم العقد '{cnum}' مكرر"); continue
                tid = None
                if ht:
                    tid = clean_id_value(row.get("رقم المستأجر", None))
                    if tid is not None and tid not in tbyid: errors.append(f"صف {idx+2}: رقم المستأجر {tid} غير موجود"); continue
                if not tid:
                    tn = clean_text_value(row.get("اسم المستأجر", ""))
                    if not tn or tn not in tni: errors.append(f"صف {idx+2}: المستأجر '{tn}' غير موجود"); continue
                    if tnc.get(tn, 0) > 1: errors.append(f"صف {idx+2}: يوجد أكثر من مستأجر باسم '{tn}'"); continue
                    tid = tni[tn]
                pid = None
                if hp:
                    pid = clean_id_value(row.get("رقم العقار", None))
                    if pid is not None and pid not in pbyid: errors.append(f"صف {idx+2}: رقم العقار {pid} غير موجود"); continue
                if not pid:
                    pn = clean_text_value(row.get("اسم العقار", ""))
                    if not pn or pn not in pni: errors.append(f"صف {idx+2}: العقار '{pn}' غير موجود"); continue
                    if pnc.get(pn, 0) > 1: errors.append(f"صف {idx+2}: يوجد أكثر من عقار باسم '{pn}'"); continue
                    pid = pni[pn]
                sd = parse_excel_date(row.get("تاريخ البداية", None)); ed = parse_excel_date(row.get("تاريخ النهاية", None))
                if sd is None or ed is None: errors.append(f"صف {idx+2}: تواريخ غير صحيحة"); continue
                if sd >= ed: errors.append(f"صف {idx+2}: البداية بعد النهاية"); continue
                ra = safe_float(row.get("قيمة الإيجار السنوي", 0))
                im = int(row.get("دورية السداد (شهور)", 1)) if "دورية السداد (شهور)" in df.columns else 1
                da = safe_float(row.get("التأمين", 0)); ti = 1 if row.get("شامل الضريبة", False) else 0
                tr = safe_float(row.get("نسبة الضريبة", 0.15)); nt = clean_text_value(row.get("ملاحظات", ""))
                cur.execute('''INSERT INTO contracts (tenant_id, property_id, contract_number, start_date, end_date,
                    rent_amount, interval_months, deposit_amount, notes, tax_included, tax_rate, calendar_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id''',
                    [tid, pid, cnum, sd.isoformat(), ed.isoformat(), ra, im, da, nt, ti, tr, 'ميلادي'])
                cid = cur.lastrowid
                step = relativedelta(months=im); curd = sd
                while curd <= ed:
                    cur.execute('INSERT INTO payments (contract_id, tenant_id, due_date, amount) VALUES (?,?,?,?)',
                                [cid, tid, curd.isoformat(), ra * im / 12.0]); curd += step
                imp += 1
            except Exception as e: errors.append(f"صف {idx+2}: {str(e)}")
        st.cache_data.clear()
        msg = f"✅ تم استيراد {imp} عقد"
        if errors: msg += f" — فشل {len(errors)}"
        st.toast(msg, icon="✅")
        if errors:
            with st.expander(f"⚠️ الأخطاء ({len(errors)})", expanded=True):
                for er in errors[:30]: st.text(er)
    except Exception as e: st.error(f"خطأ: {e}")


# ============================================================
# CRUD
# ============================================================
def add_user(u, p, r):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users WHERE username = ? COLLATE NOCASE", [u.strip()])
    if cur.fetchone()['c'] > 0: return False, "الاسم موجود"
    ph = hashlib.sha256(p.strip().encode()).hexdigest()
    cur.execute('INSERT INTO users (username, password_hash, role, permissions) VALUES (?,?,?,?)',
                [u.strip(), ph, r, json.dumps(get_default_permissions(r))])
    return True, "تمت الإضافة"

def delete_user(uid):
    cur = get_conn().cursor(); cur.execute("DELETE FROM users WHERE id=?", [uid]); st.cache_data.clear()

def load_users():
    cur = get_conn().cursor(); cur.execute("SELECT id as 'الرقم', username as 'اسم المستخدم', role as 'الدور' FROM users")
    rows = cur.fetchall()
    if not rows: return pd.DataFrame(columns=["الرقم","اسم المستخدم","الدور"])
    return pd.DataFrame([list(r) for r in rows], columns=list(rows[0].keys()))

def delete_contract(cid):
    cur = get_conn().cursor()
    for tbl in ['receipts','payments','contract_pricing_tiers','additional_fees','contract_discounts']:
        cur.execute(f"DELETE FROM {tbl} WHERE contract_id=?", [cid])
    cur.execute("DELETE FROM contracts WHERE id=?", [cid]); st.cache_data.clear()

def delete_tenant(tid):
    cur = get_conn().cursor(); cur.execute("DELETE FROM tenants WHERE id=?", [tid]); st.cache_data.clear()

def delete_property(pid):
    cur = get_conn().cursor(); cur.execute("DELETE FROM properties WHERE id=?", [pid]); st.cache_data.clear()

def add_tenant(n, p, ni, a, r, nt):
    cur = get_conn().cursor()
    cur.execute('INSERT INTO tenants (name, phone, national_id, address, region, notes) VALUES (?,?,?,?,?,?)', [n,p,ni,a,r,nt]); st.cache_data.clear()

def add_property(n, d, a, r, ar):
    cur = get_conn().cursor()
    cur.execute('INSERT INTO properties (name, description, address, region, area) VALUES (?,?,?,?,?)', [n,d,a,r,ar]); st.cache_data.clear()

def add_contract_full(tid, pid, cn, sd, ed, ra, im, da, ti, tr, nt, fb_file_id, calendar_type='ميلادي', previous_balance=0.0):
    conn = get_conn(); cur = conn.cursor()
    if cn:
        cur.execute("SELECT id FROM contracts WHERE contract_number=?", [cn])
        if cur.fetchone(): return False, "رقم العقد مستخدم", None
    else: cn = generate_contract_number()
    cur.execute("""SELECT COUNT(*) as c FROM contracts WHERE tenant_id=? AND status='نشط' AND NOT (end_date < ? OR start_date > ?)""",
                [tid, sd.isoformat(), ed.isoformat()])
    if cur.fetchone()['c'] > 0: return False, "⚠️ يوجد عقد نشط متداخل مع هذه الفترة", None
    hs = gregorian_to_hijri(sd) if calendar_type == 'هجري' else None
    he = gregorian_to_hijri(ed) if calendar_type == 'هجري' else None
    cur.execute('''INSERT INTO contracts (tenant_id, property_id, contract_number, start_date, end_date,
        rent_amount, interval_months, deposit_amount, notes, tax_included, tax_rate, contract_file,
        calendar_type, hijri_start_date, hijri_end_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id''',
        [tid, pid, cn, sd.isoformat(), ed.isoformat(), ra, im, da, nt, ti, tr, fb_file_id, calendar_type, hs, he])
    cid = cur.lastrowid
    if previous_balance and previous_balance > 0:
        cur.execute('''INSERT INTO payments (contract_id, tenant_id, due_date, amount, status, notes)
                       VALUES (?, ?, ?, ?, 'مستحق', 'رصيد سابق مرحّل')''', [cid, tid, sd.isoformat(), previous_balance])
    create_payment_schedule(cid, tid, sd, ed, ra, im, calendar_type)
    st.cache_data.clear()
    return True, "تم إنشاء العقد", cn

def get_active_tenants():
    cur = get_conn().cursor()
    cur.execute("""SELECT id, name FROM tenants WHERE id NOT IN (
                   SELECT tenant_id FROM contracts WHERE status='نشط' AND end_date >= date('now')) ORDER BY name""")
    return [(r['id'], r['name']) for r in cur.fetchall()]

def check_overlapping_contract(tid, sd, ed, exclude_cid=None):
    cur = get_conn().cursor()
    if exclude_cid:
        cur.execute("""SELECT COUNT(*) as c FROM contracts WHERE tenant_id=? AND status='نشط' AND id != ?
                       AND NOT (end_date < ? OR start_date > ?)""", [tid, exclude_cid, sd.isoformat(), ed.isoformat()])
    else:
        cur.execute("""SELECT COUNT(*) as c FROM contracts WHERE tenant_id=? AND status='نشط'
                       AND NOT (end_date < ? OR start_date > ?)""", [tid, sd.isoformat(), ed.isoformat()])
    return cur.fetchone()['c'] > 0

def get_all_tenants():
    cur = get_conn().cursor(); cur.execute("SELECT id, name FROM tenants ORDER BY name")
    return [(r['id'], r['name']) for r in cur.fetchall()]

def get_all_properties():
    cur = get_conn().cursor(); cur.execute("SELECT id, name FROM properties ORDER BY name")
    return [(r['id'], r['name']) for r in cur.fetchall()]

def get_receipt_details(rid):
    cur = get_conn().cursor(); cur.execute("SELECT * FROM receipts WHERE id=?", [rid])
    r = cur.fetchone(); return r.to_dict() if r else None

def get_contracts_by_tenant(tid):
    cur = get_conn().cursor(); cur.execute("SELECT id, contract_number FROM contracts WHERE tenant_id=?", [tid])
    return [(r['id'], r['contract_number']) for r in cur.fetchall()]

def get_payments_by_contract(cid):
    cur = get_conn().cursor(); cur.execute("SELECT id, due_date, amount, paid_amount FROM payments WHERE contract_id=?", [cid])
    return [(r['id'], f"دفعة {r['id']} - {r['due_date']} - {format_currency(r['amount'])}") for r in cur.fetchall()]

def update_receipt(rid, rn, tid, cid, pid, amt, rd, pm, nt, att):
    cur = get_conn().cursor()
    cur.execute("SELECT payment_id, amount FROM receipts WHERE id=?", [rid]); old = cur.fetchone()
    if not old: return False, "غير موجود"
    opid, oamt = old['payment_id'], old['amount']
    if opid and opid != pid:
        cur.execute("SELECT paid_amount, amount FROM payments WHERE id=?", [opid]); op = cur.fetchone()
        if op:
            npv = max(0, op['paid_amount'] - oamt)
            st_ = "مدفوع" if npv >= op['amount'] else ("جزئي" if npv > 0 else "مستحق")
            cur.execute("UPDATE payments SET paid_amount=?, status=? WHERE id=?", [npv, st_, opid])
    if pid:
        cur.execute("SELECT amount, paid_amount FROM payments WHERE id=?", [pid]); np_ = cur.fetchone()
        if np_:
            npaid = max(0, np_['paid_amount'] + (amt if opid != pid else -oamt + amt))
            cur.execute("UPDATE payments SET paid_amount=?, status=? WHERE id=?", [npaid, "مدفوع" if npaid >= np_['amount'] else "جزئي", pid])
    cur.execute('''UPDATE receipts SET receipt_number=?, tenant_id=?, contract_id=?, payment_id=?, amount=?,
                   receipt_date=?, payment_method=?, notes=?, attachment=? WHERE id=?''',
                [rn, tid, cid, pid, amt, rd.isoformat(), pm, nt, att, rid])
    st.cache_data.clear(); return True, "تم التعديل"

def delete_receipt(rid):
    cur = get_conn().cursor()
    cur.execute("SELECT payment_id, amount FROM receipts WHERE id=?", [rid]); r = cur.fetchone()
    if not r: return False, "غير موجود"
    pid, amt = r['payment_id'], r['amount']
    if pid:
        cur.execute("SELECT amount, paid_amount FROM payments WHERE id=?", [pid]); pay = cur.fetchone()
        if pay:
            np_ = max(0, pay['paid_amount'] - amt)
            cur.execute("UPDATE payments SET paid_amount=?, status=? WHERE id=?",
                        [np_, "مدفوع" if np_ >= pay['amount'] else ("جزئي" if np_ > 0 else "مستحق"), pid])
    cur.execute("DELETE FROM receipts WHERE id=?", [rid]); st.cache_data.clear(); return True, "تم حذف السند"


# ============================================================
# Backup
# ============================================================
BACKUP_TABLES = ['settings','users','tenants','properties','contracts','payments','receipts',
                 'alerts','contract_pricing_tiers','additional_fees','contract_discounts']

def create_compressed_backup():
    try:
        conn = get_conn()
        results = conn.execute_batch([(f"SELECT * FROM {t}", []) for t in BACKUP_TABLES])
        data = {t: [r.to_dict() for r in results[i]] for i, t in enumerate(BACKUP_TABLES)}
        json_bytes = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        return gzip.compress(json_bytes, compresslevel=9), len(json_bytes), len(gzip.compress(json_bytes, compresslevel=9))
    except: return None, 0, 0

def restore_from_compressed(compressed_data):
    data = json.loads(gzip.decompress(compressed_data).decode('utf-8'))
    conn = get_conn(); cur = conn.cursor()
    for tbl in BACKUP_TABLES:
        rows = data.get(tbl, [])
        if not rows: continue
        try: cur.execute(f"DELETE FROM {tbl}")
        except: pass
        cols = list(rows[0].keys()); ph = ','.join(['?'] * len(cols)); cnames = ','.join(cols)
        for row in rows:
            try: cur.execute(f"INSERT INTO {tbl} ({cnames}) VALUES ({ph})", [row.get(c) for c in cols])
            except: pass
    st.cache_data.clear()

def split_into_chunks(data, chunk_size): return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]

def download_file_from_telegram_backup(file_id):
    resp = requests.get(f"https://api.telegram.org/bot{TG_FILE_BOT_TOKEN}/getFile?file_id={file_id}", timeout=60).json()
    if not resp.get('ok'): raise Exception(f"فشل: {resp}")
    r = requests.get(f"https://api.telegram.org/file/bot{TG_FILE_BOT_TOKEN}/{resp['result']['file_path']}", timeout=300)
    if r.status_code != 200: raise Exception(f"فشل التحميل: {r.status_code}")
    return r.content


# ============================================================
# الصفحات
# ============================================================
if menu == "لوحة التحكم" and has_permission(current_user_id, "لوحة التحكم"):
    st.subheader("📊 لوحة التحكم")
    df_t = load_tenants(); df_c = load_contracts(); df_p = load_payments()
    today = date.today(); sl = today + timedelta(days=60)
    if not df_c.empty:
        df_c['ed_dt'] = pd.to_datetime(df_c['تاريخ النهاية'])
        exp_s = df_c[(df_c['الحالة']=='نشط') & (df_c['ed_dt']>=pd.Timestamp(today)) & (df_c['ed_dt']<=pd.Timestamp(sl))]
        exp_d = df_c[(df_c['الحالة']=='نشط') & (df_c['ed_dt']<pd.Timestamp(today))]
    else: exp_s = pd.DataFrame(); exp_d = pd.DataFrame()
    total_today, total_all, cnt_due, cnt_over = get_total_dues_until(today)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("إجمالي المستأجرين", len(df_t))
    c2.metric("العقود النشطة", len(df_c[df_c["الحالة"]=="نشط"]) if not df_c.empty else 0)
    c3.metric("دفعات عليها متبقي", f"{cnt_due} دفعة")
    c4.metric("إجمالي المحصل", format_currency(df_p["المدفوع"].sum() if not df_p.empty else 0))
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("عقود تنتهي خلال شهرين", len(exp_s)); c6.metric("عقود منتهية", len(exp_d))
    c7.metric("💵 إجمالي المستحقات", format_currency(total_today))
    c8.metric("⚠️ دفعات متأخرة", f"{cnt_over} دفعة")
    st.markdown("---")
    st.subheader("⏰ العقود التي ستنتهي خلال 60 يوم")
    expiring = get_expiring_contracts(60)
    if expiring:
        with st.expander(f"🔔 تفاصيل {len(expiring)} عقد", expanded=True):
            for e in expiring:
                dl = e['days_left']; b = "🔴" if dl <= 15 else ("🟠" if dl <= 30 else "🟡")
                hej = gregorian_to_hijri(parse_date_safe(e['end_date']))
                st.markdown(f"""{b} **{e['tenant_name']}** — `{e['contract_number']}`  
📞 {e['tenant_phone'] or '-'} | 🏢 {e['prop_name']} | 📍 {e['tenant_region'] or '-'}  
📅 ينتهي: **{e['end_date']} م** ({hej} هـ) — متبقي **{dl}** يوم""")
                st.markdown("---")
    else: st.info("لا توجد عقود تنتهي خلال 60 يوم")
    st.info(f"📌 **إجمالي المستحقات (كل الفترات):** {format_currency(total_all)}")
    st.markdown("---")
    st.subheader("📅 دفعات خلال 30 يوم")
    if not df_p.empty:
        up = df_p[(df_p["تاريخ الاستحقاق"]>=today.isoformat()) & (df_p["تاريخ الاستحقاق"]<=(today+timedelta(days=30)).isoformat()) & (df_p["الحالة"].isin(["مستحق","جزئي"]))]
        if not up.empty:
            upd = up[["المستأجر","العقار","تاريخ الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة"]].copy()
            upd.insert(3, "الاستحقاق (هجري)", upd["تاريخ الاستحقاق"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x else ""))
            rtl_dataframe(upd)
        else: st.info("لا توجد دفعات")

elif menu == "إدارة البيانات":
    if not has_permission(current_user_id, "إدارة البيانات"): st.error("لا تملك صلاحية")
    else:
        st.subheader("📂 إدارة البيانات")
        t1, t2, t3 = st.tabs(["المستأجرين","العقارات","العقود"])
        with t1:
            st.subheader("👥 المستأجرين")
            ci1, ci2 = st.columns(2)
            with ci1:
                df = pd.DataFrame(columns=["الاسم","الهاتف","رقم الهوية / الإقامة","العنوان","المنطقة","ملاحظات"])
                df.loc[0] = ["أحمد","05...","123","شارع","الرياض",""]
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr: df.to_excel(wr, index=False, sheet_name='المستأجرين')
                o.seek(0)
                st.download_button("تحميل قالب", data=o.getvalue(), file_name="قالب_المستأجرين.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_tmpl_t")
            with ci2:
                uf = st.file_uploader("استيراد", type=["xlsx","xls"], key="imp_t")
                if uf and st.button("تنفيذ", key="btn_imp_t"): import_tenants_from_excel(uf)
            if st.button("➕ إضافة مستأجر", key="btn_add_t"): st.session_state['show_add_t'] = True
            if st.session_state.get('show_add_t'):
                with st.form("add_t_f"):
                    n = st.text_input("الاسم *"); p = st.text_input("الهاتف"); ni = st.text_input("رقم الهوية")
                    a = st.text_input("العنوان"); r = st.text_input("المنطقة"); nt = st.text_area("ملاحظات")
                    cs, cc = st.columns(2)
                    s = cs.form_submit_button("حفظ"); c = cc.form_submit_button("إلغاء")
                    if s and n.strip():
                        add_tenant(n.strip(),p.strip(),ni.strip(),a.strip(),r.strip(),nt.strip())
                        st.toast("تمت الإضافة", icon="✅"); st.session_state['show_add_t'] = False; st.rerun()
                    if c: st.session_state['show_add_t'] = False; st.rerun()
            st.markdown("---")
            dft = load_tenants()
            if not dft.empty:
                cf1, cf2 = st.columns(2)
                rf = cf1.selectbox("المنطقة", ["الكل"] + dft["المنطقة"].dropna().unique().tolist(), key="tf")
                sq = cf2.text_input("بحث", key="ts")
                f = dft.copy()
                if rf != "الكل": f = f[f["المنطقة"]==rf]
                if sq: f = f[f.apply(lambda row: sq.lower() in str(row.values).lower(), axis=1)]
                if not f.empty:
                    display_dataframe_with_reorder(f, "tenants")
                    tid = st.selectbox("اختر", f["الرقم"], format_func=lambda x: f[f["الرقم"]==x]["الاسم"].iloc[0], key="sel_tenant_edit")
                    if tid:
                        cur = get_conn().cursor(); cur.execute("SELECT * FROM tenants WHERE id=?", [tid]); ti = cur.fetchone()
                        if ti:
                            st.markdown(f"**{ti['name']}** - {ti['phone'] or '-'} - {ti['region'] or '-'}")
                            st.markdown("### 📚 سجل العقود الكامل")
                            history = get_tenant_contracts_history(tid)
                            if history:
                                for h in history:
                                    badge = "🟢 ساري" if h['status'] == 'نشط' else "🔴 منتهي"
                                    hl = f" | هجري: {h['hijri_start_date']} → {h['hijri_end_date']}" if h.get('hijri_start_date') and h.get('hijri_end_date') else ""
                                    st.markdown(f"""**{badge}** — `{h['contract_number']}` ({h['calendar_type'] or 'ميلادي'})  
🏢 {h['prop_name']} | 📅 {h['start_date']} → {h['end_date']}{hl}  
💰 الإيجار: {format_currency(h['rent_amount'])}""")
                                st.markdown("---")
                            else: st.info("لا يوجد سجل عقود")
                            cur.execute("SELECT COALESCE(SUM(amount - paid_amount), 0) as t FROM payments WHERE tenant_id=? AND (amount - paid_amount) > 0", [tid])
                            tdeb = cur.fetchone()['t']
                            if tdeb and tdeb > 0: st.warning(f"💵 **إجمالي المديونية:** {format_currency(tdeb)}")
                            if current_role == 'مدير':
                                c1, c2 = st.columns(2)
                                if c1.button("تعديل", key=f"btn_ed_t_{tid}"): st.session_state['edit_tenant_id'] = tid; st.rerun()
                                if c2.button("حذف", key=f"btn_dl_t_{tid}"):
                                    cur.execute("SELECT COUNT(*) as c FROM contracts WHERE tenant_id=?", [tid])
                                    if cur.fetchone()['c'] > 0: st.error("لديه عقود")
                                    else:
                                        delete_tenant(tid); st.toast("تم الحذف", icon="🗑️")
                                        if 'sel_tenant_edit' in st.session_state: del st.session_state['sel_tenant_edit']
                                        st.rerun()
                            if st.session_state.get('edit_tenant_id') == tid:
                                with st.form(f"ed_t_f_{tid}"):
                                    n = st.text_input("الاسم", value=ti['name']); p = st.text_input("الهاتف", value=ti['phone'] or "")
                                    ni = st.text_input("الهوية", value=ti['national_id'] or ""); a = st.text_input("العنوان", value=ti['address'] or "")
                                    r = st.text_input("المنطقة", value=ti['region'] or ""); nt = st.text_area("ملاحظات", value=ti['notes'] or "")
                                    if st.form_submit_button("حفظ"):
                                        cur.execute("UPDATE tenants SET name=?, phone=?, national_id=?, address=?, region=?, notes=? WHERE id=?",
                                                    [n,p,ni,a,r,nt,tid])
                                        st.cache_data.clear(); st.toast("تم", icon="✅"); st.session_state['edit_tenant_id'] = None; st.rerun()
                else: st.info("لا نتائج")
        with t2:
            st.subheader("🏬 العقارات")
            ci1, ci2 = st.columns(2)
            with ci1:
                df = pd.DataFrame(columns=["الاسم","الوصف","العنوان","المنطقة","المساحة"])
                df.loc[0] = ["عمارة","وصف","شارع","الرياض","500"]
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr: df.to_excel(wr, index=False, sheet_name='العقارات')
                o.seek(0)
                st.download_button("تحميل قالب", data=o.getvalue(), file_name="قالب_العقارات.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_tmpl_p")
            with ci2:
                uf = st.file_uploader("استيراد", type=["xlsx","xls"], key="imp_p")
                if uf and st.button("تنفيذ", key="btn_imp_p"): import_properties_from_excel(uf)
            if st.button("➕ إضافة عقار", key="btn_add_p"): st.session_state['show_add_p'] = True
            if st.session_state.get('show_add_p'):
                with st.form("add_p_f"):
                    n = st.text_input("الاسم *"); d = st.text_area("الوصف"); a = st.text_input("العنوان")
                    r = st.text_input("المنطقة"); ar = st.text_input("المساحة")
                    cs, cc = st.columns(2)
                    s = cs.form_submit_button("حفظ"); c = cc.form_submit_button("إلغاء")
                    if s and n.strip():
                        add_property(n.strip(),d.strip(),a.strip(),r.strip(),ar.strip())
                        st.toast("تمت الإضافة", icon="✅"); st.session_state['show_add_p'] = False; st.rerun()
                    if c: st.session_state['show_add_p'] = False; st.rerun()
            st.markdown("---")
            dfp = load_properties()
            if not dfp.empty:
                sq = st.text_input("بحث", key="ps")
                f = dfp[dfp.apply(lambda row: sq.lower() in str(row.values).lower(), axis=1)] if sq else dfp
                if not f.empty:
                    display_dataframe_with_reorder(f, "props")
                    pid = st.selectbox("اختر", f["الرقم"], format_func=lambda x: f[f["الرقم"]==x]["الاسم"].iloc[0], key="sel_prop_edit")
                    if pid:
                        cur = get_conn().cursor(); cur.execute("SELECT * FROM properties WHERE id=?", [pid]); pi = cur.fetchone()
                        if pi:
                            st.markdown(f"**{pi['name']}** - {pi['region'] or '-'}")
                            cur.execute("SELECT c.contract_number, t.name, c.start_date, c.end_date, c.status FROM contracts c JOIN tenants t ON c.tenant_id=t.id WHERE c.property_id=?", [pid])
                            cons = cur.fetchall()
                            if cons: rtl_dataframe(pd.DataFrame([list(r) for r in cons], columns=["رقم العقد","المستأجر","بداية","نهاية","الحالة"]))
                            if current_role == 'مدير':
                                c1, c2 = st.columns(2)
                                if c1.button("تعديل", key=f"btn_ed_p_{pid}"): st.session_state['edit_property_id'] = pid; st.rerun()
                                if c2.button("حذف", key=f"btn_dl_p_{pid}"):
                                    cur.execute("SELECT COUNT(*) as c FROM contracts WHERE property_id=?", [pid])
                                    if cur.fetchone()['c'] > 0: st.error("لديه عقود")
                                    else:
                                        delete_property(pid); st.toast("تم الحذف", icon="🗑️")
                                        if 'sel_prop_edit' in st.session_state: del st.session_state['sel_prop_edit']
                                        st.rerun()
                            if st.session_state.get('edit_property_id') == pid:
                                with st.form(f"ed_p_f_{pid}"):
                                    n = st.text_input("الاسم", value=pi['name']); d = st.text_area("الوصف", value=pi['description'] or "")
                                    a = st.text_input("العنوان", value=pi['address'] or ""); r = st.text_input("المنطقة", value=pi['region'] or "")
                                    ar = st.text_input("المساحة", value=pi['area'] or "")
                                    if st.form_submit_button("حفظ"):
                                        cur.execute("UPDATE properties SET name=?, description=?, address=?, region=?, area=? WHERE id=?", [n,d,a,r,ar,pid])
                                        st.cache_data.clear(); st.toast("تم", icon="✅"); st.session_state['edit_property_id'] = None; st.rerun()
                else: st.info("لا نتائج")
        with t3:
            st.subheader("📄 العقود")
            ci1, ci2 = st.columns(2)
            with ci1:
                cur = get_conn().cursor()
                cur.execute("SELECT id, name, region FROM tenants ORDER BY name")
                tenants_list = [list(r) for r in cur.fetchall()]
                cur.execute("SELECT id, name, region FROM properties ORDER BY name")
                props_list = [list(r) for r in cur.fetchall()]
                df = pd.DataFrame(columns=["رقم العقد","رقم المستأجر","اسم المستأجر","رقم العقار","اسم العقار",
                                            "تاريخ البداية","تاريخ النهاية","قيمة الإيجار السنوي",
                                            "دورية السداد (شهور)","التأمين","شامل الضريبة","نسبة الضريبة","ملاحظات"])
                df.loc[0] = ["CTR-2025-001","","أحمد","","عمارة","2025-01-01","2025-12-31",60000,6,5000,0,0.15,""]
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr:
                    df.to_excel(wr, index=False, sheet_name='العقود')
                    if tenants_list: pd.DataFrame(tenants_list, columns=["رقم المستأجر","اسم المستأجر","المنطقة"]).to_excel(wr, index=False, sheet_name='المستأجرين')
                    if props_list: pd.DataFrame(props_list, columns=["رقم العقار","اسم العقار","المنطقة"]).to_excel(wr, index=False, sheet_name='العقارات')
                o.seek(0)
                st.download_button("تحميل قالب العقود", data=o.getvalue(), file_name="قالب_العقود.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_tmpl_c")
            with ci2:
                uf = st.file_uploader("استيراد", type=["xlsx","xls"], key="imp_c")
                if uf and st.button("تنفيذ", key="btn_imp_c"): import_contracts_from_excel(uf); st.rerun()
            if st.button("➕ إضافة عقد", key="btn_add_c"): st.session_state['show_add_c'] = True
            if st.session_state.get('show_add_c'):
                at = get_active_tenants()
                if not at: st.warning("لا يوجد مستأجرين متاحين")
                else:
                    cal_type = st.radio("اختر نوع التقويم", ["ميلادي", "هجري"], horizontal=True, key="add_c_cal_type")
                    st.markdown("---")
                    with st.form("add_c_f"):
                        to = {t[0]: t[1] for t in at}
                        tid = st.selectbox("المستأجر *", options=list(to.keys()), format_func=lambda x: to[x])
                        po = {p[0]: p[1] for p in get_all_properties()}
                        if not po: st.warning("لا توجد عقارات")
                        else:
                            pid = st.selectbox("العقار *", options=list(po.keys()), format_func=lambda x: po[x])
                            cn_input = st.text_input("رقم العقد (اتركه فارغاً للتوليد)", value="")
                            if cal_type == "هجري":
                                st.info("📅 التواريخ بصيغة dd-mm-yyyy — مثال: 01-10-1445")
                                hc1, hc2 = st.columns(2)
                                hs = hc1.text_input("البداية (هجري)", value=gregorian_to_hijri(date.today()), key="add_c_hs")
                                he = hc2.text_input("النهاية (هجري)", value=gregorian_to_hijri(date.today() + relativedelta(years=1)), key="add_c_he")
                                try:
                                    sd = hijri_to_gregorian(hs); ed = hijri_to_gregorian(he)
                                    st.success(f"✅ الميلادي: **{sd}** ← **{ed}**")
                                except Exception as e:
                                    st.error(f"⚠️ {e}"); sd = date.today(); ed = date.today() + relativedelta(years=1)
                            else:
                                sd = st.date_input("البداية (ميلادي)", value=date.today(), key="add_c_sd")
                                ed = st.date_input("النهاية (ميلادي)", value=date.today() + relativedelta(years=1), key="add_c_ed")
                            ra = st.number_input("الإيجار السنوي", min_value=0.0, step=1000.0, value=0.0)
                            im = st.number_input("الدورية (شهور)", min_value=1, value=1)
                            da = st.number_input("التأمين", min_value=0.0, step=100.0, value=0.0)
                            prev_bal = st.number_input("💵 رصيد سابق مُرحّل", min_value=0.0, step=100.0, value=0.0)
                            ti = st.checkbox("شامل الضريبة")
                            tr = st.number_input("نسبة الضريبة (%)", min_value=0.0, max_value=100.0, value=15.0) / 100
                            nt = st.text_area("ملاحظات")
                            cf = st.file_uploader("ملف العقد (PDF)", type=["pdf"])
                            cs, cc = st.columns(2)
                            s = cs.form_submit_button("حفظ"); c = cc.form_submit_button("إلغاء")
                            if s:
                                if sd >= ed: st.error("تاريخ النهاية يجب أن يكون بعد البداية")
                                elif check_overlapping_contract(tid, sd, ed): st.error("⚠️ يوجد عقد نشط متداخل")
                                else:
                                    fb_id = save_uploaded_file(cf, "contract_file") if cf else None
                                    ok, msg, final_cn = add_contract_full(tid, pid, cn_input.strip(), sd, ed, ra, im, da,
                                                                          1 if ti else 0, tr, nt, fb_id, cal_type, prev_bal)
                                    if ok:
                                        st.toast(f"✅ {msg} - {final_cn}", icon="✅")
                                        st.session_state['show_add_c'] = False; st.rerun()
                                    else: st.error(msg)
                            if c: st.session_state['show_add_c'] = False; st.rerun()
            st.markdown("---")
            dfc = load_contracts()
            if not dfc.empty:
                cc1, cc2 = st.columns(2)
                rfc = cc1.selectbox("المنطقة", ["الكل"] + load_tenants()["المنطقة"].dropna().unique().tolist(), key="cf")
                tfc = cc2.selectbox("المستأجر", ["الكل"] + dfc["اسم المستأجر"].unique().tolist(), key="ctf")
                fc = dfc.copy()
                if rfc != "الكل":
                    tt = load_tenants()[load_tenants()["المنطقة"]==rfc]["الاسم"].tolist()
                    fc = fc[fc["اسم المستأجر"].isin(tt)]
                if tfc != "الكل": fc = fc[fc["اسم المستأجر"]==tfc]
                if not fc.empty:
                    fc = fc.reset_index(drop=True); fc_show = fc.copy(); fc_show["الرقم"] = range(1, len(fc_show) + 1)
                    display_dataframe_with_reorder(fc_show, "contracts")
                    c_options = {row['الرقم']: f"{i+1} - {row['رقم العقد']} - {row['اسم المستأجر']}" for i, row in fc.iterrows()}
                    cid = st.selectbox("اختر عقد", options=list(c_options.keys()), format_func=lambda x: c_options[x], key="sel_contract")
                    if cid:
                        cur = get_conn().cursor()
                        cur.execute('''SELECT c.*, t.name as tenant_name, t.phone as tphone, t.region as tregion,
                            p.name as prop_name, p.address as paddr FROM contracts c
                            JOIN tenants t ON c.tenant_id=t.id JOIN properties p ON c.property_id=p.id WHERE c.id=?''', [cid])
                        ci = cur.fetchone()
                        if ci:
                            st.markdown("### تفاصيل العقد")
                            c1, c2 = st.columns(2)
                            c1.write(f"**رقم العقد:** {ci['contract_number']}")
                            c1.write(f"**المستأجر:** {ci['tenant_name']}")
                            c1.write(f"**الهاتف:** {ci['tphone'] or '-'}")
                            c1.write(f"**التقويم:** {ci['calendar_type'] or 'ميلادي'}")
                            c2.write(f"**العقار:** {ci['prop_name']}")
                            c2.write(f"**العنوان:** {ci['paddr'] or '-'}")
                            st.divider()
                            st.write(f"**البداية:** {ci['start_date']} | **النهاية:** {ci['end_date']}")
                            if ci['hijri_start_date'] and ci['hijri_end_date']:
                                st.write(f"**هجري:** {ci['hijri_start_date']} → {ci['hijri_end_date']}")
                            st.write(f"**الإيجار:** {format_currency(ci['rent_amount'])} | **الدورية:** كل {ci['interval_months']} شهر")
                            st.write(f"**التأمين:** {format_currency(ci['deposit_amount'])}")
                            st.write(f"**ملاحظات:** {ci['notes'] or '-'}")
                            if ci['contract_file']: render_attachment_download(ci['contract_file'], f"cf_{cid}", "📥 ملف العقد")
                            st.markdown("---")
                            if current_role == 'مدير':
                                c1, c2 = st.columns(2)
                                                           if c1.button("تعديل العقد", key=f"btn_ed_c_{cid}"):
                                    try:
                                        with st.spinner("⏳ جاري تحميل بيانات العقد..."):
                                            load_tenants()
                                            load_properties()
                                            _c = get_conn().cursor()
                                            _c.execute("SELECT id FROM contracts WHERE id=?", [cid])
                                            if not _c.fetchone():
                                                st.error("❌ العقد غير موجود")
                                            else:
                                                st.session_state['edit_contract_id'] = cid
                                                st.rerun()
                                    except Exception as e:
                                        st.error(f"❌ فشل تحميل بيانات العقد: {e}")
                                        with st.expander("تفاصيل الخطأ"):
                                            st.code(traceback.format_exc())
                                
                                if c2.button("حذف العقد", key=f"btn_dl_c_{cid}"):
                                    try:
                                        with st.spinner("🗑️ جاري الحذف..."):
                                            delete_contract(cid)
                                        st.toast("تم الحذف", icon="🗑️")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"❌ فشل الحذف: {e}")
                else: st.info("لا عقود")

elif menu == "الدفعات":
    st.subheader("💰 متابعة الدفعات")
    if not has_permission(current_user_id, "الدفعات"): st.error("لا تملك صلاحية")
    else:
        sf = st.selectbox("الحالة", ["الكل","مستحق","مدفوع","متأخر","جزئي"])
        dfp = load_payments(sf)
        if not dfp.empty:
            ff1, ff2 = st.columns(2)
            with ff1:
                regs = ["الكل"] + sorted([r for r in dfp["المنطقة"].dropna().unique().tolist() if r])
                srp = st.selectbox("المنطقة", regs, key="pay_region_flt")
            with ff2:
                t_in = sorted(dfp[dfp["المنطقة"] == srp]["المستأجر"].dropna().unique().tolist()) if srp != "الكل" else sorted(dfp["المستأجر"].dropna().unique().tolist())
                stp = st.selectbox("المستأجر", ["الكل"] + t_in, key="pay_tenant_flt")
            dfp_f = dfp.copy()
            if srp != "الكل": dfp_f = dfp_f[dfp_f["المنطقة"] == srp]
            if stp != "الكل": dfp_f = dfp_f[dfp_f["المستأجر"] == stp]
            sq = st.text_input("بحث", key="ps_")
            f = dfp_f[dfp_f["المستأجر"].str.contains(sq, case=False, na=False)] if sq else dfp_f
            if not f.empty:
                f_disp = f.drop(columns=["معرف_المستأجر"], errors='ignore').copy()
                if "تاريخ الاستحقاق" in f_disp.columns:
                    f_disp.insert(list(f_disp.columns).index("تاريخ الاستحقاق") + 1, "الاستحقاق (هجري)",
                                  f_disp["تاريخ الاستحقاق"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x and str(x).strip() else ""))
                if "تاريخ السداد" in f_disp.columns:
                    f_disp.insert(list(f_disp.columns).index("تاريخ السداد") + 1, "السداد (هجري)",
                                  f_disp["تاريخ السداد"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x and str(x).strip() else ""))
                display_dataframe_with_reorder(f_disp, "payments")
                orient1 = st.radio("اتجاه الصفحة", ["عمودي (Portrait)", "أفقي (Landscape)"], horizontal=True, key="pay_orient")
                lc = (orient1 == "أفقي (Landscape)")
                c1, c2 = st.columns(2)
                with c1:
                    o = io.BytesIO()
                    with pd.ExcelWriter(o, engine='xlsxwriter') as wr: f_disp.to_excel(wr, index=False)
                    st.download_button("تحميل Excel", data=o.getvalue(), file_name="دفعات.xlsx", key="dl_pays")
                with c2: export_df_to_pdf(f_disp, "بيان الدفعات", "دفعات.pdf", landscape_mode=lc)
            else: st.info("لا نتائج")
        else: st.info("لا دفعات")

elif menu == "سندات القبض":
    st.subheader("🧾 سندات القبض")
    if not has_permission(current_user_id, "سندات القبض"): st.error("لا تملك صلاحية")
    else:
        t1, t2 = st.tabs(["تسجيل سداد","سجل السندات"])
        with t1:
            if current_role in ['مدير','محاسب']:
                atp = load_tenants()
                if atp.empty: st.warning("لا مستأجرين")
                else:
                    f1, f2 = st.columns(2)
                    with f1:
                        rp = ["الكل"] + sorted([r for r in atp["المنطقة"].dropna().unique().tolist() if r])
                        srp = st.selectbox("🔽 المنطقة", rp, key="pay_reg_filter")
                    with f2:
                        tf_ = atp[atp["المنطقة"] == srp] if srp != "الكل" else atp
                        if tf_.empty: st.warning("لا مستأجرين في هذه المنطقة"); st.stop()
                        tid = st.selectbox("🔽 المستأجر", tf_["الرقم"],
                                           format_func=lambda x: tf_[tf_["الرقم"]==x]["الاسم"].iloc[0], key="sel_tenant_pay")
                    today = date.today()
                    show_adv = st.checkbox("🔮 عرض الدفعات المستقبلية (مقدمة)", value=False, key="show_advance_chk")
                    st.markdown("### 💵 ملخص المديونية")
                    conn = get_conn()
                    res = conn.execute_batch([
                        ("SELECT COALESCE(SUM(amount - paid_amount), 0) as t FROM payments WHERE tenant_id=? AND (amount - paid_amount) > 0", [tid]),
                        ("SELECT COALESCE(SUM(amount - paid_amount), 0) as t FROM payments WHERE tenant_id=? AND (amount - paid_amount) > 0 AND due_date < ?", [tid, today.isoformat()]),
                        ("SELECT COALESCE(SUM(amount - paid_amount), 0) as t FROM payments WHERE tenant_id=? AND (amount - paid_amount) > 0 AND due_date > ?", [tid, today.isoformat()])
                    ])
                    td_ = res[0][0]['t'] if res[0] else 0; od_ = res[1][0]['t'] if res[1] else 0; fd_ = res[2][0]['t'] if res[2] else 0
                    m1, m2, m3 = st.columns(3)
                    m1.metric("إجمالي المديونية", format_currency(td_))
                    m2.metric("⚠️ متأخر", format_currency(od_))
                    m3.metric("🔮 مستقبلي", format_currency(fd_))
                    st.markdown("---")
                    cur = conn.cursor()
                    if show_adv:
                        cur.execute('''SELECT id, due_date, amount, paid_amount, (amount - paid_amount) as remaining, contract_id
                            FROM payments WHERE tenant_id=? AND status != 'مدفوع' AND (amount - paid_amount) > 0 ORDER BY due_date''', [tid])
                    else:
                        cur.execute('''SELECT id, due_date, amount, paid_amount, (amount - paid_amount) as remaining, contract_id
                            FROM payments WHERE tenant_id=? AND status != 'مدفوع' AND due_date <= ? AND (amount - paid_amount) > 0 ORDER BY due_date''',
                                    [tid, today.isoformat()])
                    dues = cur.fetchall()
                    if not dues: st.info("لا دفعات مستحقة")
                    else:
                        dfd = pd.DataFrame([list(r) for r in dues], columns=["رقم الدفعة","الاستحقاق","المبلغ","المدفوع","المتبقي","رقم العقد"])
                        dfd["الاستحقاق (هجري)"] = dfd["الاستحقاق"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x else "")
                        rtl_dataframe(dfd[["رقم الدفعة","الاستحقاق","الاستحقاق (هجري)","المبلغ","المدفوع","المتبقي"]])
                        pid = st.selectbox("الدفعة", dfd["رقم الدفعة"].tolist(),
                                           format_func=lambda x: f"دفعة {x} — م: {dfd[dfd['رقم الدفعة']==x]['الاستحقاق'].iloc[0]} — هـ: {dfd[dfd['رقم الدفعة']==x]['الاستحقاق (هجري)'].iloc[0]}",
                                           key="sel_pay_pay")
                        if pid:
                            od = [d for d in dues if d['id'] == pid][0]
                            rem = od['remaining']; due_dt = od['due_date']
                            is_adv = due_dt > today.isoformat()
                            hej_d = gregorian_to_hijri(parse_date_safe(due_dt))
                            st.success(f"🔮 دفعة مقدمة — الاستحقاق: **{due_dt} م** ({hej_d} هـ)") if is_adv else st.info(f"📅 الاستحقاق: **{due_dt} م** ({hej_d} هـ)")
                            pdte = st.date_input("تاريخ السداد", value=today, key="pay_date_in")
                            am = st.number_input("المبلغ", min_value=0.0, max_value=float(rem), value=float(rem), step=100.0, key="pay_amt_in")
                            mt = st.selectbox("طريقة الدفع", ["نقدي","تحويل بنكي","شيك","دفع في المنصة"], key="pay_mt_in")
                            att = st.file_uploader("مرفق", type=["pdf","png","jpg","jpeg"], key="pay_att_in")
                            if st.button("تسجيل السداد", key="btn_register_pay"):
                                if am <= 0: st.error("المبلغ > 0")
                                else:
                                    fb = save_uploaded_file(att, "receipt") if att else None
                                    cur.execute("SELECT amount, paid_amount, contract_id FROM payments WHERE id=?", [pid])
                                    pd_ = cur.fetchone()
                                    npaid = pd_['paid_amount'] + am
                                    stt = "مدفوع" if npaid >= pd_['amount'] else "جزئي"
                                    adv_f = 1 if is_adv else 0
                                    cur.execute("UPDATE payments SET paid_amount=?, paid_date=?, status=?, attachment=?, is_advance=? WHERE id=?",
                                                [npaid, pdte.isoformat(), stt, fb, adv_f, pid])
                                    rn = generate_receipt_number()
                                    nxt = " (دفعة مقدمة)" if is_adv else ""
                                    cur.execute('''INSERT INTO receipts (receipt_number, tenant_id, contract_id, payment_id, amount, receipt_date, payment_method, attachment, notes)
                                        VALUES (?,?,?,?,?,?,?,?,?)''', [rn, tid, pd_['contract_id'], pid, am, pdte.isoformat(), mt, fb, nxt.strip()])
                                    st.cache_data.clear(); st.toast(f"تم تسجيل {format_currency(am)}{nxt}", icon="✅"); st.rerun()
            else: st.warning("ليس لديك صلاحية")
        with t2:
            dfr = load_receipts()
            if not dfr.empty:
                f1, f2, f3 = st.columns(3)
                with f1:
                    regs = ["الكل"] + sorted([r for r in dfr["المنطقة"].dropna().unique().tolist() if r])
                    sr = st.selectbox("المنطقة", regs, key="flt_rec_region")
                with f2:
                    ti_ = sorted(dfr[dfr["المنطقة"] == sr]["المستأجر"].dropna().unique().tolist()) if sr != "الكل" else sorted(dfr["المستأجر"].dropna().unique().tolist())
                    stn = st.selectbox("المستأجر", ["الكل"] + ti_, key="flt_rec_tenant")
                with f3:
                    s_txt = st.text_input("بحث", key="flt_rec_search")
                dfr_f = dfr.copy()
                if sr != "الكل": dfr_f = dfr_f[dfr_f["المنطقة"] == sr]
                if stn != "الكل": dfr_f = dfr_f[dfr_f["المستأجر"] == stn]
                if s_txt.strip(): dfr_f = dfr_f[dfr_f.apply(lambda r: s_txt.lower() in str(r.get("رقم السند","")).lower(), axis=1)]
                if dfr_f.empty: st.info("لا نتائج")
                else:
                    dfr_show = dfr_f.drop(columns=["معرف_المستأجر"], errors='ignore')
                    display_dataframe_with_reorder(dfr_show, "receipts")
                    rid = st.selectbox("اختر سند", dfr_f["الرقم"],
                                       format_func=lambda x: f"{dfr_f[dfr_f['الرقم']==x]['رقم السند'].iloc[0]}", key="sel_receipt_edit")
                    if rid:
                        c1, c2, c3 = st.columns(3)
                        with c1:
                            pdf_data = print_receipt(rid)
                            if pdf_data: st.download_button("طباعة", data=pdf_data, file_name=f"r_{rid}.pdf", mime="application/pdf", key=f"dl_pdf_{rid}")
                        with c2:
                            cur = get_conn().cursor(); cur.execute("SELECT attachment FROM receipts WHERE id=?", [rid]); a = cur.fetchone()
                            if a and a['attachment']: render_attachment_download(a['attachment'], f"rcp_{rid}", "📥 المرفق")
                        with c3:
                            if current_role == 'مدير':
                                cc1, cc2 = st.columns(2)
                                with cc1:
                                    if st.button("✏️ تعديل", key=f"btn_ed_r_{rid}", use_container_width=True):
                                        st.session_state['edit_receipt_id'] = rid; st.rerun()
                                with cc2:
                                    if st.button("🗑️ حذف", key=f"btn_del_r_{rid}", use_container_width=True):
                                        ok, msg = delete_receipt(rid)
                                        if ok:
                                            st.toast(msg, icon="🗑️")
                                            if 'edit_receipt_id' in st.session_state: del st.session_state['edit_receipt_id']
                                            st.rerun()
                                        else: st.error(msg)
                        if st.session_state.get('edit_receipt_id') == rid and current_role == 'مدير':
                            rd = get_receipt_details(rid)
                            if rd:
                                st.markdown("### تعديل السند")
                                with st.form(f"ed_r_f_{rid}"):
                                    rn = st.text_input("رقم السند", value=rd['receipt_number'])
                                    tn = {t[0]: t[1] for t in get_all_tenants()}
                                    tid = st.selectbox("المستأجر", options=list(tn.keys()),
                                                       index=list(tn.keys()).index(rd['tenant_id']) if rd['tenant_id'] in tn else 0,
                                                       format_func=lambda x: tn[x])
                                    cs = get_contracts_by_tenant(tid); cn = {c[0]: c[1] for c in cs}
                                    cid = st.selectbox("العقد", options=list(cn.keys()),
                                                       index=list(cn.keys()).index(rd['contract_id']) if rd['contract_id'] in cn else 0,
                                                       format_func=lambda x: cn[x])
                                    ps = get_payments_by_contract(cid); pl = {p[0]: p[1] for p in [(None, "بدون ربط")] + ps}
                                    pid = st.selectbox("الدفعة", options=list(pl.keys()),
                                                       index=list(pl.keys()).index(rd['payment_id']) if rd['payment_id'] in pl else 0,
                                                       format_func=lambda x: pl[x])
                                    am = st.number_input("المبلغ", min_value=0.0, step=100.0, value=float(rd['amount']))
                                    rdte = st.date_input("التاريخ", value=parse_date_safe(rd['receipt_date']))
                                    opts = ["نقدي","تحويل بنكي","شيك","دفع في المنصة"]
                                    mt = st.selectbox("طريقة الدفع", opts, index=opts.index(rd['payment_method']) if rd['payment_method'] in opts else 0)
                                    nt = st.text_area("ملاحظات", value=rd['notes'] or "")
                                    nf = st.file_uploader("مرفق جديد", type=["pdf","png","jpg","jpeg"])
                                    if st.form_submit_button("حفظ"):
                                        fb = rd['attachment']
                                        if nf: fb = save_uploaded_file(nf, "receipt")
                                        ok, msg = update_receipt(rid, rn, tid, cid, pid, am, rdte, mt, nt, fb)
                                        if ok:
                                            st.toast(msg, icon="✅"); st.session_state['edit_receipt_id'] = None; st.rerun()
                                        else: st.error(msg)
            else: st.info("لا سندات")

elif menu == "عقود منتهية":
    st.subheader("🔁 عقود منتهية ودفعات مؤقتة")
    if not has_permission(current_user_id, "عقود منتهية"): st.error("لا تملك صلاحية")
    else:
        exp = get_all_expired_contracts()
        if not exp: st.info("لا توجد عقود منتهية")
        else:
            dfe = pd.DataFrame(exp).rename(columns={'id':'رقم_داخلي','contract_number':'رقم العقد','tenant_name':'المستأجر',
                                       'end_date':'تاريخ الانتهاء','prop_name':'العقار','rent_amount':'الإيجار','interval_months':'الدورية'})
            rtl_dataframe(dfe[['رقم العقد','المستأجر','العقار','تاريخ الانتهاء','الإيجار','الدورية']])
            co = {e['id']: f"{e['contract_number']} - {e['tenant_name']} - انتهى {e['end_date']}" for e in exp}
            sel = st.selectbox("اختر عقد", options=list(co.keys()), format_func=lambda x: co[x], key="sel_expired")
            if sel:
                ci = next(e for e in exp if e['id'] == sel)
                st.markdown(f"### العقد: {ci['contract_number']}")
                st.info(f"**المستأجر:** {ci['tenant_name']} | **العقار:** {ci['prop_name']}")
                mode = st.radio("طريقة الإضافة", ["📋 جدول دفعات مؤقت","➕ دفعة واحدة"], horizontal=True, key=f"mode_{sel}")
                if mode == "📋 جدول دفعات مؤقت":
                    with st.form(f"temp_sched_{sel}"):
                        c1, c2 = st.columns(2)
                        with c1: sd = st.date_input("من", value=date.today())
                        with c2: ed = st.date_input("إلى", value=date.today() + relativedelta(years=1))
                        c3, c4 = st.columns(2)
                        with c3: ta = st.number_input("الإيجار السنوي", min_value=0.0, step=1000.0, value=float(ci['rent_amount'] or 0))
                        with c4: ivm = st.number_input("الدورية", min_value=1, value=int(ci['interval_months'] or 6))
                        note = st.text_input("ملاحظة", value="فترة مؤقتة")
                        if st.form_submit_button("📋 توليد"):
                            if sd >= ed: st.error("تواريخ خاطئة")
                            elif ta <= 0: st.error("المبلغ > 0")
                            else:
                                cnt = create_temporary_payment_schedule(sel, ci['tenant_id'], sd, ed, ta, ivm, note)
                                st.toast(f"تم {cnt} دفعة", icon="✅"); st.rerun()
                else:
                    with st.form(f"temp_single_{sel}"):
                        c1, c2 = st.columns(2)
                        with c1: dd = st.date_input("الاستحقاق", value=date.today())
                        with c2: am = st.number_input("المبلغ", min_value=0.0, step=100.0, value=float(ci['rent_amount'] or 0))
                        nt = st.text_input("ملاحظة", value="دفعة مؤقتة")
                        if st.form_submit_button("➕ إضافة"):
                            if am <= 0: st.error("المبلغ > 0")
                            else:
                                add_single_temporary_payment(sel, ci['tenant_id'], dd, am, nt)
                                st.toast("تمت الإضافة", icon="✅"); st.rerun()
                st.markdown("---")
                tp = get_temporary_payments(sel)
                if tp:
                    st.markdown(f"#### الدفعات المؤقتة ({len(tp)} دفعة)")
                    dft = pd.DataFrame(tp)
                    show = ['id','due_date','amount','paid_amount','status','temporary_note']
                    dft_s = dft[[c for c in show if c in dft.columns]].rename(columns={'id':'الرقم','due_date':'الاستحقاق','amount':'المبلغ','paid_amount':'المدفوع','status':'الحالة','temporary_note':'ملاحظة'})
                    rtl_dataframe(dft_s)
                else: st.info("لا دفعات مؤقتة")

elif menu == "التقارير":
    st.subheader("📈 التقارير")
    if not has_permission(current_user_id, "التقارير"): st.error("لا تملك صلاحية")
    else:
        rt = st.radio("نوع التقرير", ["كشف حساب مستأجر","دفعات بين تاريخين","الإيرادات","الضرائب","تقرير المستحقات"])
        cc = st.radio("نوع التاريخ", ["ميلادي","هجري"], horizontal=True)
        st.markdown("### 📄 خيارات الطباعة")
        oc = st.radio("اتجاه الصفحة", ["عمودي (Portrait)", "أفقي (Landscape)"], horizontal=True, key="report_orientation")
        lc = (oc == "أفقي (Landscape)")

        if rt == "كشف حساب مستأجر":
            dft = load_tenants()
            if not dft.empty:
                rf = st.selectbox("المنطقة", ["الكل"] + dft["المنطقة"].dropna().unique().tolist(), key="kr_rf")
                ft = dft[dft["المنطقة"]==rf] if rf != "الكل" else dft
                if not ft.empty:
                    tid = st.selectbox("المستأجر", ft["الرقم"], format_func=lambda x: ft[ft["الرقم"]==x]["الاسم"].iloc[0], key="kr_tid")
                    c1, c2 = st.columns(2)
                    with c1:
                        if cc == "هجري":
                            hi = st.text_input("من هجري (dd-mm-yyyy)", "01-01-1445", key="kr_h1")
                            try: fd = hijri_to_gregorian(hi); st.caption(f"✅ م: **{fd}**")
                            except Exception as e: st.error(f"⚠️ {e}"); st.stop()
                        else: fd = st.date_input("من", value=date.today().replace(day=1), key="kr_d1")
                    with c2:
                        if cc == "هجري":
                            hi2 = st.text_input("إلى هجري (dd-mm-yyyy)", "30-12-1445", key="kr_h2")
                            try: td = hijri_to_gregorian(hi2); st.caption(f"✅ م: **{td}**")
                            except Exception as e: st.error(f"⚠️ {e}"); st.stop()
                        else: td = st.date_input("إلى", value=date.today(), key="kr_d2")
                    if cc == "هجري": st.info(f"📆 الفترة (م): من **{fd}** إلى **{td}**")
                    inc_past = st.checkbox("☑️ تضمين المتأخرات قبل الفترة", value=False, key=f"kashf_past_{tid}")
                    conn = get_conn()
                    pay_q = '''SELECT pay.id, pay.due_date, pay.amount, pay.paid_amount, (pay.amount-pay.paid_amount) as rem,
                               pay.status, pay.paid_date, pay.attachment, c.contract_number
                               FROM payments pay JOIN contracts c ON pay.contract_id = c.id
                               WHERE pay.tenant_id=? AND ''' + ("pay.due_date <= ?" if inc_past else "pay.due_date BETWEEN ? AND ?") + " ORDER BY pay.due_date"
                    pay_params = [tid, td.isoformat()] if inc_past else [tid, fd.isoformat(), td.isoformat()]
                    res = conn.execute_batch([
                        ("SELECT name, region FROM tenants WHERE id=?", [tid]),
                        ("SELECT c.contract_number FROM contracts c WHERE c.tenant_id=? AND c.status='نشط' LIMIT 1", [tid]),
                        ("SELECT contract_number, start_date, end_date, status FROM contracts WHERE tenant_id=? ORDER BY start_date DESC", [tid]),
                        ('''SELECT receipt_number, amount, receipt_date, payment_method, attachment
                            FROM receipts WHERE tenant_id=? AND receipt_date BETWEEN ? AND ? ORDER BY receipt_date''',
                         [tid, fd.isoformat(), td.isoformat()]),
                        (pay_q, pay_params)
                    ])
                    tn_row = res[0][0] if res[0] else None; cr = res[1][0] if res[1] else None
                    all_c = res[2]; recs = res[3]; pays = res[4]
                    if tn_row:
                        tn = tn_row['name']; tr = tn_row['region']; cno = cr['contract_number'] if cr else "لا يوجد"
                        st.markdown(f"### كشف حساب: {tn}")
                        st.write(f"**المنطقة:** {tr or '-'} | **العقد الحالي:** {cno} | **الفترة:** {fd} - {td}")
                        if all_c:
                            with st.expander(f"📚 سجل العقود ({len(all_c)})", expanded=False):
                                for ac in all_c:
                                    b = "🟢" if ac['status'] == 'نشط' else "🔴"
                                    st.write(f"{b} **{ac['contract_number']}** — {ac['start_date']} → {ac['end_date']}")
                        if pays:
                            dfp = pd.DataFrame([list(p) for p in pays], columns=["الرقم","الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة","تاريخ السداد","المرفق","رقم العقد"])
                            if cc == "هجري":
                                dfp["الاستحقاق (هجري)"] = dfp["الاستحقاق"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x else "")
                                dfp["السداد (هجري)"] = dfp["تاريخ السداد"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x and str(x).strip() else "")
                                dfp = dfp[["الرقم","الاستحقاق","الاستحقاق (هجري)","المبلغ","المدفوع","المتبقي","الحالة","تاريخ السداد","السداد (هجري)","رقم العقد"]]
                            rtl_dataframe(dfp)
                            ta_ = sum([p['amount'] for p in pays]); tp_ = sum([p['paid_amount'] for p in pays])
                            st.write(f"**إجمالي المستحق:** {format_currency(ta_)}")
                            st.write(f"**إجمالي المدفوع:** {format_currency(tp_)}")
                            st.write(f"**المتبقي:** {format_currency(ta_ - tp_)}")
                        else: st.info("لا دفعات")
                        if recs:
                            dfr = pd.DataFrame([list(r) for r in recs], columns=["رقم السند","المبلغ","التاريخ","الطريقة","المرفق"])
                            if cc == "هجري": dfr["التاريخ (هجري)"] = dfr["التاريخ"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x else "")
                            st.markdown("### 🧾 السندات")
                            rtl_dataframe(dfr.drop(columns=["المرفق"]))
                        if pays or recs:
                            if pays:
                                dfe = pd.DataFrame([(p['due_date'], p['amount'], p['paid_amount'], p['rem'], p['status'], p['paid_date'], p['contract_number']) for p in pays],
                                                    columns=["الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة","تاريخ السداد","رقم العقد"])
                            else:
                                dfe = pd.DataFrame(columns=["الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة","تاريخ السداد","رقم العقد"])
                            o = io.BytesIO()
                            with pd.ExcelWriter(o, engine='xlsxwriter') as wr:
                                dfe.to_excel(wr, sheet_name='الدفعات', index=False)
                                if recs: pd.DataFrame([(r['receipt_number'],r['amount'],r['receipt_date'],r['payment_method']) for r in recs],
                                                       columns=["رقم السند","المبلغ","التاريخ","الطريقة"]).to_excel(wr, sheet_name='سندات', index=False)
                            st.download_button("تحميل Excel", data=o.getvalue(), file_name=f"kashf_{tn}.xlsx", key=f"dl_kashf_{tid}")
                            ei = f"المنطقة: {tr or '-'} - العقد الحالي: {cno}"
                            if pays: export_df_to_pdf(dfe, f"كشف حساب {tn}", f"kashf_{tn}.pdf", extra_info=ei, landscape_mode=lc)
                        else: st.warning("لا توجد حركات")
        elif rt == "دفعات بين تاريخين":
            if cc == "هجري":
                c1, c2 = st.columns(2)
                hi1 = c1.text_input("من هجري", "01-01-1445", key="dd_h1")
                hi2 = c2.text_input("إلى هجري", "30-12-1445", key="dd_h2")
                try: fd = hijri_to_gregorian(hi1); td = hijri_to_gregorian(hi2); st.success(f"✅ م: {fd} → {td}")
                except Exception as e: st.error(f"⚠️ {e}"); st.stop()
            else:
                c1, c2 = st.columns(2)
                fd = c1.date_input("من", value=date.today().replace(day=1), key="dd_d1")
                td = c2.date_input("إلى", value=date.today(), key="dd_d2")
            od = st.checkbox("💵 المستحقات فقط", value=False, key="only_dues_chk")
            tf = st.selectbox("مستأجر", ["الكل"] + load_tenants()["الاسم"].tolist(), key="dd_tf")
            rf = st.selectbox("المنطقة", ["الكل"] + load_tenants()["المنطقة"].dropna().unique().tolist(), key="dd_rf")
            cur = get_conn().cursor()
            q = '''SELECT t.name as "المستأجر", p.name as "العقار", pay.due_date as "الاستحقاق", pay.amount as "المبلغ", pay.paid_amount as "المدفوع",
                   (pay.amount-pay.paid_amount) as "المتبقي", pay.status as "الحالة", t.region as "المنطقة", c.contract_number as "رقم العقد"
                   FROM payments pay JOIN tenants t ON pay.tenant_id=t.id
                   JOIN contracts c ON pay.contract_id=c.id JOIN properties p ON c.property_id=p.id
                   WHERE pay.due_date BETWEEN ? AND ?'''
            pr = [fd.isoformat(), td.isoformat()]
            if od: q += " AND (pay.amount - pay.paid_amount) > 0"
            if tf != "الكل": q += " AND t.name=?"; pr.append(tf)
            if rf != "الكل": q += " AND t.region=?"; pr.append(rf)
            q += " ORDER BY pay.due_date"
            cur.execute(q, pr); dues = cur.fetchall()
            if dues:
                df = pd.DataFrame([list(d) for d in dues], columns=["المستأجر","العقار","الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة","المنطقة","رقم العقد"])
                if cc == "هجري": df.insert(3, "الاستحقاق (هجري)", df["الاستحقاق"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x else ""))
                df_sel, sel_c = display_dataframe_with_reorder(df.copy(), "rp")
                ta = df["المبلغ"].sum(); tp_ = df["المدفوع"].sum(); tr_ = df["المتبقي"].sum()
                c1, c2, c3 = st.columns(3)
                c1.metric("المستحق", format_currency(ta)); c2.metric("المدفوع", format_currency(tp_)); c3.metric("المتبقي", format_currency(tr_))
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr: df_sel.to_excel(wr, index=False)
                st.download_button("تحميل Excel", data=o.getvalue(), file_name=f"dues_{fd}_{td}.xlsx", key="dl_dues")
                title = "المستحقات" if od else "الدفعات"
                export_df_to_pdf(df_sel, f"{title} من {fd} إلى {td}", f"dues_{fd}_{td}.pdf", columns_order=sel_c, landscape_mode=lc)
            else: st.info("لا نتائج")
        elif rt == "الإيرادات":
            if cc == "هجري":
                c1, c2 = st.columns(2)
                hi1 = c1.text_input("من هجري", "01-01-1445", key="rev_h1"); hi2 = c2.text_input("إلى هجري", "30-12-1445", key="rev_h2")
                try: fd = hijri_to_gregorian(hi1); td = hijri_to_gregorian(hi2)
                except Exception as e: st.error(f"⚠️ {e}"); st.stop()
            else:
                c1, c2 = st.columns(2)
                fd = c1.date_input("من", value=date.today().replace(day=1), key="rev_d1"); td = c2.date_input("إلى", value=date.today(), key="rev_d2")
            cur = get_conn().cursor()
            cur.execute('''SELECT r.receipt_date as "التاريخ", t.name as "المستأجر",
                r.receipt_number as "رقم السند", r.amount as "المبلغ", r.payment_method as "طريقة السداد"
                FROM receipts r JOIN tenants t ON r.tenant_id=t.id
                WHERE r.receipt_date BETWEEN ? AND ? ORDER BY r.receipt_date''', [fd.isoformat(), td.isoformat()])
            rows = cur.fetchall()
            if rows:
                df = pd.DataFrame([list(r) for r in rows], columns=["التاريخ","المستأجر","رقم السند","المبلغ","طريقة السداد"])
                if cc == "هجري": df.insert(1, "التاريخ (هجري)", df["التاريخ"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x else ""))
                df_sel, sel_c = display_dataframe_with_reorder(df.copy(), "rev")
                st.write(f"**الإجمالي:** {format_currency(df['المبلغ'].sum())}")
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr: df_sel.to_excel(wr, index=False)
                st.download_button("Excel", data=o.getvalue(), file_name=f"rev_{fd}_{td}.xlsx", key="dl_rev")
                export_df_to_pdf(df_sel, "الإيرادات", f"rev_{fd}_{td}.pdf", columns_order=sel_c, landscape_mode=lc)
            else: st.info("لا إيرادات")
        elif rt == "الضرائب":
            if cc == "هجري":
                c1, c2 = st.columns(2)
                hi1 = c1.text_input("من هجري", "01-01-1445", key="tx_h1"); hi2 = c2.text_input("إلى هجري", "30-12-1445", key="tx_h2")
                try: fd = hijri_to_gregorian(hi1); td = hijri_to_gregorian(hi2)
                except Exception as e: st.error(f"⚠️ {e}"); st.stop()
            else:
                c1, c2 = st.columns(2)
                fd = c1.date_input("من", value=date.today().replace(day=1), key="tx_d1"); td = c2.date_input("إلى", value=date.today(), key="tx_d2")
            cur = get_conn().cursor()
            cur.execute('''SELECT t.name as "اسم المستأجر", c.contract_number as "رقم العقد",
                c.start_date as "بداية الفترة", c.end_date as "نهاية الفترة",
                pay.amount as "المبلغ شامل الضريبة", c.tax_included as "شامل الضريبة",
                c.tax_rate as "نسبة الضريبة", r.payment_method as "طريقة الدفع"
                FROM payments pay JOIN tenants t ON pay.tenant_id=t.id
                JOIN contracts c ON pay.contract_id=c.id LEFT JOIN receipts r ON r.payment_id=pay.id
                WHERE pay.status='مدفوع' AND pay.paid_date BETWEEN ? AND ? ORDER BY pay.paid_date''',
                        [fd.isoformat(), td.isoformat()])
            rows = cur.fetchall()
            if rows:
                taxes = []
                for row in rows:
                    am = safe_float(row['المبلغ شامل الضريبة']); ti = int(row['شامل الضريبة']); tr = safe_float(row['نسبة الضريبة'])
                    taxes.append(am * (tr / (1 + tr)) if ti == 1 and tr > 0 else (am * tr if ti != 1 else 0))
                df = pd.DataFrame([list(r) for r in rows], columns=["اسم المستأجر","رقم العقد","بداية الفترة","نهاية الفترة","المبلغ شامل الضريبة","شامل الضريبة","نسبة الضريبة","طريقة الدفع"])
                df['مبلغ الضريبة'] = taxes
                df['المبلغ غير شامل الضريبة'] = df['المبلغ شامل الضريبة'] - df['مبلغ الضريبة']
                df = df[['اسم المستأجر','رقم العقد','بداية الفترة','نهاية الفترة','المبلغ شامل الضريبة','نسبة الضريبة','مبلغ الضريبة','المبلغ غير شامل الضريبة','طريقة الدفع']]
                dfd, sc = display_dataframe_with_reorder(df.copy(), "tax")
                st.write(f"**إجمالي شامل:** {format_currency(df['المبلغ شامل الضريبة'].sum())}")
                st.write(f"**الضريبة:** {format_currency(df['مبلغ الضريبة'].sum())}")
                st.write(f"**غير شامل:** {format_currency(df['المبلغ غير شامل الضريبة'].sum())}")
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr: dfd.to_excel(wr, index=False)
                st.download_button("Excel", data=o.getvalue(), file_name=f"tax_{fd}_{td}.xlsx", key="dl_tax")
                export_tax_pdf(dfd, "تقرير الضرائب", f"tax_{fd}_{td}.pdf", columns_order=sc, landscape_mode=lc)
            else: st.info("لا بيانات")
        elif rt == "تقرير المستحقات":
            st.markdown("### 📋 تقرير المستحقات")
            if cc == "هجري":
                c1, c2 = st.columns(2)
                hi1 = c1.text_input("من هجري", "01-01-1445", key="due_h1")
                hi2 = c2.text_input("إلى هجري", "30-12-1445", key="due_h2")
                try: fd_due = hijri_to_gregorian(hi1); td_due = hijri_to_gregorian(hi2)
                except Exception as e: st.error(f"⚠️ {e}"); st.stop()
            else:
                c1, c2 = st.columns(2)
                fd_due = c1.date_input("من تاريخ", value=date.today().replace(day=1), key="due_d1")
                td_due = c2.date_input("إلى تاريخ", value=date.today() + relativedelta(years=1), key="due_d2")
            st.info(f"📆 الفترة: **{fd_due}** → **{td_due}**")
            inc_ov = st.checkbox("☑️ تضمين المتأخرات", value=True, key="due_include_past")
            allt = load_tenants()
            rl = ["الكل"] + sorted([r for r in allt["المنطقة"].dropna().unique().tolist() if r])
            rfd = st.selectbox("المنطقة", rl, key="due_report_region")
            cur = get_conn().cursor()
            if inc_ov:
                q = '''SELECT t.id, t.name as tenant_name, t.region, MIN(pay.due_date) as oldest_due,
                       COUNT(*) as num_payments,
                       SUM(CASE WHEN pay.due_date < date('now') THEN 1 ELSE 0 END) as num_overdue,
                       SUM(pay.amount - pay.paid_amount) as total_remaining
                       FROM payments pay JOIN tenants t ON pay.tenant_id = t.id
                       WHERE (pay.amount - pay.paid_amount) > 0 AND pay.due_date <= ?'''
                pr = [td_due.isoformat()]
            else:
                q = '''SELECT t.id, t.name as tenant_name, t.region, MIN(pay.due_date) as oldest_due,
                       COUNT(*) as num_payments,
                       SUM(CASE WHEN pay.due_date < date('now') THEN 1 ELSE 0 END) as num_overdue,
                       SUM(pay.amount - pay.paid_amount) as total_remaining
                       FROM payments pay JOIN tenants t ON pay.tenant_id = t.id
                       WHERE (pay.amount - pay.paid_amount) > 0 AND pay.due_date BETWEEN ? AND ?'''
                pr = [fd_due.isoformat(), td_due.isoformat()]
            if rfd != "الكل": q += " AND t.region = ?"; pr.append(rfd)
            q += " GROUP BY t.id, t.name, t.region ORDER BY oldest_due ASC, t.name"
            cur.execute(q, pr); rows = cur.fetchall()
            if rows:
                dfd = pd.DataFrame([list(r) for r in rows], columns=["id","tenant_name","region","oldest_due","num_payments","num_overdue","total_remaining"])
                dfd = dfd.rename(columns={'tenant_name': 'المستأجر','region': 'المنطقة','oldest_due': 'أقدم دفعة غير مسددة',
                                          'num_payments': 'عدد الدفعات المستحقة','num_overdue': 'عدد الدفعات المتأخرة','total_remaining': 'إجمالي المتبقي'})
                dfd = dfd[['المستأجر','المنطقة','أقدم دفعة غير مسددة','عدد الدفعات المستحقة','عدد الدفعات المتأخرة','إجمالي المتبقي']]
                if cc == "هجري":
                    dfd.insert(3, "أقدم دفعة (هجري)", dfd["أقدم دفعة غير مسددة"].apply(lambda x: gregorian_to_hijri(parse_date_safe(x)) if x else ""))
                df_sel, sc_ = display_dataframe_with_reorder(dfd, "due_report_table")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("مدينين", len(dfd)); c2.metric("دفعات مستحقة", f"{int(dfd['عدد الدفعات المستحقة'].sum())}")
                c3.metric("دفعات متأخرة", f"{int(dfd['عدد الدفعات المتأخرة'].sum())}"); c4.metric("💵 إجمالي المتبقي", format_currency(dfd['إجمالي المتبقي'].sum()))
                c1, c2 = st.columns(2)
                with c1:
                    o = io.BytesIO()
                    with pd.ExcelWriter(o, engine='xlsxwriter') as wr: df_sel.to_excel(wr, index=False)
                    st.download_button("📥 Excel", data=o.getvalue(), file_name=f"due_report_{td_due}.xlsx", key="dl_due_report_xl")
                with c2: export_df_to_pdf(df_sel, f"مستحقات حتى {td_due}", f"due_report_{td_due}.pdf", landscape_mode=lc)
            else: st.success(f"✅ لا مستحقات")

elif menu == "المستخدمون":
    st.subheader("👤 المستخدمون")
    if not has_permission(current_user_id, "المستخدمون"): st.error("لا تملك صلاحية")
    else:
        t1, t2 = st.tabs(["عرض","إضافة"])
        with t1:
            dfu = load_users()
            if not dfu.empty:
                rtl_dataframe(dfu)
                uid = st.selectbox("اختر", dfu["الرقم"], format_func=lambda x: dfu[dfu["الرقم"]==x]["اسم المستخدم"].iloc[0], key="sel_user")
                if uid:
                    up = load_permissions(uid)
                    np_ = {pg: st.checkbox(pg, value=up.get(pg, False), key=f"perm_{uid}_{pg}") for pg in PAGE_KEYS}
                    if st.button("حفظ الصلاحيات", key=f"sv_p_{uid}"): save_permissions(uid, np_); st.toast("تم", icon="✅"); st.rerun()
                    if st.button("حذف المستخدم", key=f"dl_u_{uid}"): delete_user(uid); st.toast("تم الحذف", icon="🗑️"); st.rerun()
                    if current_role == 'مدير':
                        with st.expander("تغيير كلمة المرور"):
                            npwd = st.text_input("جديدة", type="password", key=f"np_{uid}")
                            if st.button("تعيين", key=f"sp_{uid}") and npwd.strip():
                                cur = get_conn().cursor()
                                cur.execute("UPDATE users SET password_hash=? WHERE id=?", [hashlib.sha256(npwd.strip().encode()).hexdigest(), uid])
                                st.toast("تم", icon="✅"); st.rerun()
        with t2:
            with st.form("add_u_f"):
                u = st.text_input("اسم المستخدم *"); p = st.text_input("كلمة المرور *", type="password")
                r = st.selectbox("الدور", ["مدير","محاسب","مشاهد"])
                if st.form_submit_button("إضافة"):
                    if u.strip() and p.strip():
                        ok, msg = add_user(u, p, r)
                        if ok: st.toast(msg, icon="✅"); st.rerun()
                        else: st.error(msg)
                    else: st.error("ناقص")

elif menu == "الإعدادات":
    st.subheader("⚙️ الإعدادات")
    if not has_permission(current_user_id, "الإعدادات"): st.error("لا تملك صلاحية")
    else:
        with st.form("sett_f"):
            cn = st.text_input("اسم الشركة", settings.get('company_name', 'نظام إدارة الإيجارات'))
            pc = st.color_picker("اللون الأساسي", settings['primary_color'])
            sc = st.color_picker("اللون الثانوي", settings['secondary_color'])
            bc = st.color_picker("لون الخلفية", settings['background_color'])
            fs = st.slider("حجم الخط", 14, 28, settings['font_size'])
            lf = st.file_uploader("شعار", type=["png","jpg","jpeg"])
            if st.form_submit_button("حفظ"):
                save_setting('company_name', cn); save_setting('primary_color', pc)
                save_setting('secondary_color', sc); save_setting('background_color', bc); save_setting('font_size', fs)
                if lf:
                    fid = save_uploaded_file(lf, "logo")
                    if fid: save_setting('logo_file_id', fid)
                st.toast("تم الحفظ", icon="✅"); st.rerun()
        st.info(f"📱 تيليجرام: {'✅' if TG_FILE_BOT_TOKEN else '❌'}")
        st.info(f"🗄️ Turso: {'✅ متصل' if TURSO_URL else '❌'}")

elif menu == "نسخ احتياطي":
    st.subheader("💾 النسخ الاحتياطي")
    if not has_permission(current_user_id, "نسخ احتياطي"): st.error("لا تملك صلاحية")
    else:
        st.info("ℹ️ النسخ الاحتياطي بيصدر كل البيانات كـ JSON مضغوط")
        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### 📥 تنزيل نسخة")
            try:
                compressed_data, orig_size, comp_size = create_compressed_backup()
                if compressed_data:
                    orig_mb = orig_size / (1024 * 1024); comp_mb = comp_size / (1024 * 1024)
                    st.write(f"📊 **الأصلي:** {orig_mb:.3f} MB"); st.write(f"📦 **المضغوط:** {comp_mb:.3f} MB")
                    st.download_button("⬇️ تحميل (.json.gz)", data=compressed_data,
                                      file_name=f"backup_{date.today()}.json.gz", mime="application/gzip", key="dl_backup_gz")
                else: st.error("فشل")
            except Exception as e: st.error(f"خطأ: {e}")
        with c2:
            st.markdown("### 📤 استعادة")
            uf = st.file_uploader("اختر ملف", type=["gz", "json"], key="restore_up")
            if uf:
                file_bytes = uf.read()
                if st.button("🔄 استعادة", key="btn_restore_gz"):
                    try:
                        if file_bytes[:2] == b'\x1f\x8b': restore_from_compressed(file_bytes)
                        else:
                            data = json.loads(file_bytes.decode('utf-8'))
                            restore_from_compressed(gzip.compress(json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')))
                        st.cache_data.clear(); st.toast("✅ تمت", icon="✅"); time.sleep(1); st.rerun()
                    except Exception as e: st.error(f"فشل: {e}")
        st.markdown("---")
        st.subheader("📱 نسخ عبر تيليجرام")
        if not TG_FILE_BOT_TOKEN or not TG_FILE_CHAT_ID:
            st.warning("⚠️ بوت تيليجرام غير معد")
        else:
            c1, c2 = st.columns(2)
            with c1:
                if st.button("🚀 رفع الآن", key="btn_tg_up"):
                    with st.spinner("جاري الرفع..."):
                        try:
                            data, _, _ = create_compressed_backup()
                            if data:
                                fid = upload_to_telegram(data, f"backup_{int(time.time())}.json.gz", "نسخة احتياطية")
                                if fid:
                                    save_setting('telegram_backup_file_id', fid); st.success(f"✅ تم الرفع")
                                else: st.error("فشل")
                        except Exception as e: st.error(f"خطأ: {e}")
            with c2:
                saved_fid = settings.get('telegram_backup_file_id', '')
                if st.button("🔄 استعادة من تيليجرام", key="btn_tg_down"):
                    if not saved_fid: st.error("لا يوجد نسخة")
                    else:
                        with st.spinner("جاري التحميل..."):
                            try:
                                data = download_file_from_telegram_backup(saved_fid)
                                restore_from_compressed(data); st.success("✅ تمت"); time.sleep(1); st.rerun()
                            except Exception as e: st.error(f"فشل: {e}")
