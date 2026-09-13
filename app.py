import streamlit as st
import pandas as pd
import sqlite3
import io
import gzip
import shutil
import tempfile
from datetime import date, timedelta, datetime
from dateutil.relativedelta import relativedelta
import time
from hijri_converter import convert
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
import requests
import os
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import base64
import hashlib
import json
import numpy as np

st.set_page_config(page_title="نظام إدارة الإيجارات", page_icon="🏢", layout="wide")

st.markdown("""
<style>
    html, body, [class*="css"] { direction: rtl !important; text-align: right !important; }
    .stApp { direction: rtl !important; }
    .stSidebar { direction: rtl !important; text-align: right !important; }
    .stButton, .stSelectbox, .stTextInput, .stNumberInput, .stDateInput, .stRadio, .stCheckbox {
        direction: rtl !important; text-align: right !important;
    }
    h1, h2, h3, h4, h5, h6 { direction: rtl !important; text-align: right !important; }
    .stTabs [data-baseweb="tab-list"] { direction: rtl !important; }
    input, textarea { direction: rtl !important; text-align: right !important; }
    .stDownloadButton button { direction: rtl !important; }
    .streamlit-expanderHeader { direction: rtl !important; text-align: right !important; }
    .stAlert { direction: rtl !important; text-align: right !important; }
    [data-testid="stMetric"] { direction: rtl !important; text-align: right !important; }
    [data-testid="stDataFrame"] { direction: ltr !important; }
    [data-testid="stDataFrame"] [role="columnheader"] { text-align: center !important; }
</style>
""", unsafe_allow_html=True)

def safe_float(value, default=0.0):
    try:
        if value is None or pd.isna(value): return default
        if isinstance(value, str):
            value = value.replace(',', '').strip()
            if value == '': return default
        return float(value)
    except (TypeError, ValueError): return default

def format_currency(value):
    val = safe_float(value, 0.0)
    if val == int(val): return f"{int(val):,}"
    return f"{val:,.2f}"

def rtl_dataframe(df, key=None, **kwargs):
    df_display = df.copy()
    numeric_cols = df_display.select_dtypes(include=[np.number]).columns.tolist()
    date_cols = [c for c in df_display.columns if 'تاريخ' in c or 'date' in c.lower() or 'بداية' in c or 'نهاية' in c or 'استحقاق' in c]
    number_like = [c for c in df_display.columns if any(kw in c for kw in ['المبلغ','المدفوع','المتبقي','الضريبة','إيجار','التأمين','الرقم','نسبة'])]
    ltr_cols = list(set(numeric_cols + date_cols + number_like))
    rtl_cols = [c for c in df_display.columns if c not in ltr_cols]
    styled = df_display.style
    if ltr_cols: styled = styled.set_properties(subset=ltr_cols, **{'text-align':'left','direction':'ltr'})
    if rtl_cols: styled = styled.set_properties(subset=rtl_cols, **{'text-align':'right','direction':'rtl'})
    st.dataframe(styled, use_container_width=True, key=key, **kwargs)

def display_dataframe_with_reorder(df, key_prefix):
    columns = list(df.columns)
    default = st.session_state.get(f"{key_prefix}_order", columns)
    selected = st.multiselect("اختر الأعمدة وترتيبها", options=columns, default=default, key=f"{key_prefix}_cols")
    if selected:
        df_out = df[selected]
        st.session_state[f"{key_prefix}_order"] = selected
    else:
        df_out = df
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
        pdfmetrics.registerFont(TTFont('Amiri', fp))
        return 'Amiri'
    return 'Helvetica'

def reshape_arabic_text(text):
    return get_display(arabic_reshaper.reshape(str(text)))

def parse_currency(v): return safe_float(v, 0.0)

def parse_date_safe(v, default=None):
    if not v: return default or date.today()
    if isinstance(v, date): return v
    try: return datetime.strptime(str(v), '%Y-%m-%d').date()
    except:
        try: return datetime.fromisoformat(str(v)).date()
        except: return default or date.today()

def wrap_text_for_pdf(text, max_chars_per_line):
    s = str(text)
    if len(s) <= max_chars_per_line: return [s]
    lines = []
    remaining = s
    while len(remaining) > max_chars_per_line:
        chunk = remaining[:max_chars_per_line]
        space_idx = chunk.rfind(' ')
        if space_idx > max_chars_per_line // 3:
            lines.append(remaining[:space_idx].strip())
            remaining = remaining[space_idx:].strip()
        else:
            lines.append(chunk)
            remaining = remaining[max_chars_per_line:]
    if remaining: lines.append(remaining)
    return lines

DATE_COLUMNS = ['تاريخ الاستحقاق','تاريخ السداد','بداية الفترة','نهاية الفترة','أقدم دفعة غير مسددة']

def export_df_to_pdf(df, title, file_name, columns_order=None, extra_info=None, landscape_mode=False):
    if columns_order: df = df[columns_order]
    else: df = df.copy()
    df_num = df.copy()
    for c in df_num.columns:
        try: df_num[c] = df_num[c].apply(parse_currency)
        except: pass
    numeric_cols_set = set()
    for col in df.columns:
        try:
            test = df[col].apply(lambda x: safe_float(x, None) if x is not None else None)
            test_series = pd.Series([x for x in test if x is not None])
            if len(test_series) > 0 and test_series.notna().all():
                numeric_cols_set.add(col)
        except: pass
    buf = io.BytesIO()
    pagesize = landscape(A4) if landscape_mode else A4
    c = canvas.Canvas(buf, pagesize=pagesize)
    w, h = pagesize
    fn = setup_arabic_font()
    c.setFont(fn, 10); c.setFillColor(colors.HexColor("#4A90E2"))
    c.rect(0, h-30, w, 30, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont(fn, 16)
    c.drawCentredString(w/2, h-20, reshape_arabic_text(title))
    y_extra = h - 50
    if extra_info:
        c.setFillColor(colors.black); c.setFont(fn, 12)
        c.drawCentredString(w/2, y_extra, reshape_arabic_text(extra_info))
        y_extra -= 20
    cols = list(df.columns); headers = ["م"] + cols
    widths = []
    for idx, col in enumerate(headers):
        if col == "م": widths.append(30); continue
        max_len = len(reshape_arabic_text(str(col)))
        for v in df[col].tolist():
            s = format_currency(v) if isinstance(v, (int, float)) and not pd.isna(v) else (str(v) if not pd.isna(v) else "")
            max_len = max(max_len, len(reshape_arabic_text(s)))
        if col in ['المبلغ','المدفوع','المتبقي','المبلغ شامل الضريبة','مبلغ الضريبة','المبلغ غير شامل الضريبة','الإيجار السنوي','إجمالي المتبقي']: widths.append(95)
        elif col in DATE_COLUMNS: widths.append(115)
        elif col in ['المستأجر','اسم المستأجر']: widths.append(140)
        elif col in ['العقار','اسم العقار']: widths.append(120)
        elif col in ['المنطقة']: widths.append(80)
        elif col in ['الحالة']: widths.append(65)
        elif col in ['رقم السند','رقم العقد']: widths.append(90)
        elif col in ['طريقة الدفع','طريقة السداد']: widths.append(85)
        elif col in ['عدد الدفعات المستحقة','عدد الدفعات المتأخرة']: widths.append(90)
        else: widths.append(min(max(max_len * 6 + 15, 65), 130))
    tw = sum(widths); max_w = w - 40
    if tw > max_w:
        sf = max_w / tw; widths = [x * sf for x in widths]; tw = max_w
    xs = (w - tw) / 2
    if xs < 20: xs = 20
    y = y_extra - 20 if extra_info else h - 60
    c.setFont(fn, 8); c.setFillColor(colors.HexColor("#f0f0f0"))
    c.rect(xs, y-15, tw, 25, fill=1, stroke=0)
    c.setFillColor(colors.black)
    xc = xs + tw
    for i, hd in enumerate(headers):
        cw = widths[i]; xr = xc; xl = xc - cw
        c.drawCentredString((xl+xr)/2, y-3, reshape_arabic_text(hd)); xc -= cw
    y -= 30; sn = 1; line_height = 11
    for _, row in df.iterrows():
        row_lines = []
        for col in cols:
            v = row[col]
            vs = format_currency(v) if isinstance(v, (int, float)) and not pd.isna(v) else (str(v) if not pd.isna(v) else "")
            col_idx = cols.index(col) + 1
            cw = widths[col_idx]
            if col in DATE_COLUMNS: lines = [vs]
            else:
                max_chars = max(int(cw / 7), 5)
                lines = wrap_text_for_pdf(vs, max_chars)
            row_lines.append(lines)
        max_lines = max((len(lines) for lines in row_lines), default=1)
        row_height = max_lines * line_height + 6
        if y - row_height < 40:
            c.showPage(); c.setFont(fn, 8); y = h - 50
            c.setFillColor(colors.HexColor("#f0f0f0")); c.rect(xs, y-15, tw, 25, fill=1, stroke=0)
            c.setFillColor(colors.black); xc = xs + tw
            for i, hd in enumerate(headers):
                cw = widths[i]; xr = xc; xl = xc - cw
                c.drawCentredString((xl+xr)/2, y-3, reshape_arabic_text(hd)); xc -= cw
            y -= 30
        c.setFillColor(colors.white); c.rect(xs, y - row_height + 5, tw, row_height, fill=1, stroke=0)
        c.setFillColor(colors.black)
        cw = widths[0]; xr = xs + tw; xl = xr - cw
        center_y = y - (row_height / 2) + 3
        c.drawCentredString((xl+xr)/2, center_y, str(sn)); sn += 1; xc = xr - cw
        for i, col in enumerate(cols, 1):
            cw = widths[i]; xr = xc; xl = xc - cw
            lines = row_lines[i-1]; start_y = y - 3
            for li, line in enumerate(lines):
                if col in DATE_COLUMNS: c.setFont(fn, 7)
                else: c.setFont(fn, 8)
                c.drawRightString(xr - 5, start_y - li * line_height, reshape_arabic_text(line))
            c.setFont(fn, 8); xc -= cw
        c.setStrokeColor(colors.grey); c.setLineWidth(0.5)
        c.line(xs, y+5, xs+tw, y+5); c.line(xs, y - row_height + 5, xs+tw, y - row_height + 5)
        xc = xs + tw
        for i in range(len(headers)): c.line(xc, y+5, xc, y - row_height + 5); xc -= widths[i]
        c.line(xs, y+5, xs, y - row_height + 5)
        y -= row_height
    c.line(xs, y+5, xs+tw, y+5); y -= 5
    c.setFillColor(colors.HexColor("#e8f0fe")); c.rect(xs, y-15, tw, 22, fill=1, stroke=0); c.setFillColor(colors.black)
    cw = widths[0]; xr = xs + tw; xl = xr - cw
    c.drawCentredString((xl+xr)/2, y-7, reshape_arabic_text("الإجمالي")); xc = xr - cw
    for i, col in enumerate(cols, 1):
        cw = widths[i]; xr = xc; xl = xc - cw
        if col in numeric_cols_set:
            try:
                total_val = df_num[col].sum()
                c.drawRightString(xr-5, y-7, format_currency(total_val))
            except: pass
        xc -= cw
    c.save(); buf.seek(0)
    orientation_label = "أفقي" if landscape_mode else "عمودي"
    st.download_button(f"تحميل PDF ({orientation_label})", data=buf, file_name=file_name, mime="application/pdf")

def export_tax_pdf(df, title, file_name, columns_order=None, landscape_mode=True):
    if columns_order: df = df[columns_order]
    else: df = df.copy()
    df_num = df.copy()
    for c in ['المبلغ شامل الضريبة','مبلغ الضريبة','المبلغ غير شامل الضريبة']:
        if c in df_num.columns: df_num[c] = df_num[c].apply(parse_currency)
    buf = io.BytesIO()
    pagesize = landscape(A4) if landscape_mode else A4
    c = canvas.Canvas(buf, pagesize=pagesize)
    w, h = pagesize
    fn = setup_arabic_font()
    c.setFont(fn, 10); c.setFillColor(colors.HexColor("#4A90E2"))
    c.rect(0, h-30, w, 30, fill=1, stroke=0); c.setFillColor(colors.white)
    c.setFont(fn, 16); c.drawCentredString(w/2, h-20, reshape_arabic_text(title))
    cols = list(df.columns); headers = ["م"] + cols
    widths = []
    for col in headers:
        if col == "م": widths.append(25)
        elif col in ['المبلغ شامل الضريبة','مبلغ الضريبة','المبلغ غير شامل الضريبة']: widths.append(90)
        elif col == 'نسبة الضريبة': widths.append(60)
        elif col in ['بداية الفترة','نهاية الفترة']: widths.append(115)
        elif col in ['اسم المستأجر','المستأجر']: widths.append(140)
        elif col in ['رقم العقد']: widths.append(90)
        elif col == 'طريقة الدفع': widths.append(85)
        else: widths.append(max(len(reshape_arabic_text(col))*5, 75))
    tw = sum(widths); max_w = w - 40
    if tw > max_w:
        sf = max_w / tw; widths = [x*sf for x in widths]; tw = max_w
    xs = (w - tw) / 2
    if xs < 20: xs = 20
    y = h - 60
    c.setFont(fn, 7); c.setFillColor(colors.HexColor("#f0f0f0"))
    c.rect(xs, y-18, tw, 28, fill=1, stroke=0); c.setFillColor(colors.black)
    xc = xs + tw
    for i, hd in enumerate(headers):
        cw = widths[i]; xr = xc; xl = xc - cw; xm = (xl+xr)/2
        c.drawCentredString(xm, y-3, reshape_arabic_text(hd)); xc -= cw
    y -= 30; c.setFont(fn, 8); sn = 1; line_height = 10
    for _, row in df.iterrows():
        row_lines = []
        for col in cols:
            v = row[col]
            vs = format_currency(v) if isinstance(v, (int, float)) and not pd.isna(v) else (str(v) if not pd.isna(v) else "")
            col_idx = cols.index(col) + 1
            cw = widths[col_idx]
            if col in ['بداية الفترة','نهاية الفترة']: lines = [vs]
            else:
                max_chars = max(int(cw / 6.5), 5)
                lines = wrap_text_for_pdf(vs, max_chars)
            row_lines.append(lines)
        max_lines = max((len(l) for l in row_lines), default=1)
        row_height = max_lines * line_height + 5
        if y - row_height < 40:
            c.showPage(); c.setFont(fn, 7); y = h - 50
            c.setFillColor(colors.HexColor("#f0f0f0")); c.rect(xs, y-18, tw, 28, fill=1, stroke=0)
            c.setFillColor(colors.black); xc = xs + tw
            for i, hd in enumerate(headers):
                cw = widths[i]; xr = xc; xl = xc - cw; xm = (xl+xr)/2
                c.drawCentredString(xm, y-3, reshape_arabic_text(hd)); xc -= cw
            y -= 30; c.setFont(fn, 8)
        c.setFillColor(colors.white); c.rect(xs, y - row_height + 5, tw, row_height, fill=1, stroke=0); c.setFillColor(colors.black)
        cw = widths[0]; xr = xs + tw; xl = xr - cw
        c.drawCentredString((xl+xr)/2, y - row_height/2 + 2, str(sn)); sn += 1; xc = xr - cw
        for i, col in enumerate(cols, 1):
            cw = widths[i]; xr = xc; xl = xc - cw
            for li, line in enumerate(row_lines[i-1]):
                if col in ['بداية الفترة','نهاية الفترة']: c.setFont(fn, 6.5)
                else: c.setFont(fn, 8)
                c.drawRightString(xr-4, y - 3 - li*line_height, reshape_arabic_text(line))
            c.setFont(fn, 8); xc -= cw
        c.setStrokeColor(colors.grey); c.setLineWidth(0.5)
        c.line(xs, y+5, xs+tw, y+5); c.line(xs, y-row_height+5, xs+tw, y-row_height+5)
        xc = xs + tw
        for i in range(len(headers)): c.line(xc, y+5, xc, y-row_height+5); xc -= widths[i]
        c.line(xs, y+5, xs, y-row_height+5)
        y -= row_height
    c.save(); buf.seek(0)
    orientation_label = "أفقي" if landscape_mode else "عمودي"
    st.download_button(f"تحميل PDF ({orientation_label})", data=buf, file_name=file_name, mime="application/pdf")

def print_receipt(receipt_id):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('''SELECT r.receipt_number, t.name, r.amount, r.receipt_date, r.payment_method, r.notes
                   FROM receipts r JOIN tenants t ON r.tenant_id = t.id WHERE r.id = ?''', (receipt_id,))
    r = cur.fetchone(); conn.close()
    if not r: return None
    rn, tn, amt, rd, mt, nt = r
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4; fn = setup_arabic_font()
    c.setFont(fn, 12); c.setFillColor(colors.HexColor("#4A90E2"))
    c.rect(0, h-40, w, 40, fill=1, stroke=0); c.setFillColor(colors.white)
    c.setFont(fn, 18); c.drawCentredString(w/2, h-25, reshape_arabic_text("سند قبض"))
    c.setFont(fn, 12); c.setFillColor(colors.black); y = h - 80
    for lbl, val in [("رقم السند:", rn),("اسم المستأجر:", tn),("المبلغ:", format_currency(amt)),
                     ("تاريخ السداد:", rd),("طريقة الدفع:", mt),("ملاحظات:", nt or "لا يوجد")]:
        c.drawRightString(w-100, y, reshape_arabic_text(f"{lbl} {val}")); y -= 25
    c.save(); buf.seek(0)
    return buf.getvalue()

def get_conn():
    conn = sqlite3.connect("rentals.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn

def ensure_columns(cur, table, cols):
    cur.execute(f"PRAGMA table_info({table})")
    ex = [c[1] for c in cur.fetchall()]
    for cn in cols:
        if cn not in ex:
            if cn in ('interval_months','tax_included','is_temporary'):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {cn} INTEGER DEFAULT 0")
            elif cn == 'tax_rate':
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {cn} REAL DEFAULT 0.15")
            elif cn in ('attachment','contract_file','permissions','temporary_note'):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {cn} TEXT")
            else:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {cn} TEXT")

@st.cache_resource
def init_db():
    conn = get_conn(); cur = conn.cursor()
    cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    cur.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                   password_hash TEXT, role TEXT DEFAULT 'مشاهد', permissions TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    ensure_columns(cur, 'users', ['permissions'])
    cur.execute('''CREATE TABLE IF NOT EXISTS tenants (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                   phone TEXT, national_id TEXT, address TEXT, region TEXT, notes TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS properties (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                   description TEXT, address TEXT, region TEXT, area TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS contracts (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER, property_id INTEGER,
                   contract_number TEXT UNIQUE, start_date TEXT, end_date TEXT, rent_amount REAL, interval_months INTEGER DEFAULT 1,
                   deposit_amount REAL, notes TEXT, status TEXT DEFAULT 'نشط', tax_included INTEGER DEFAULT 0, tax_rate REAL DEFAULT 0.15, contract_file BLOB)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER, tenant_id INTEGER,
                   due_date TEXT, amount REAL, paid_amount REAL DEFAULT 0, paid_date TEXT, status TEXT DEFAULT 'مستحق', notes TEXT, attachment BLOB)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_number TEXT, tenant_id INTEGER,
                   contract_id INTEGER, payment_id INTEGER, amount REAL, receipt_date TEXT, payment_method TEXT, notes TEXT, attachment BLOB)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER, alert_text TEXT,
                   alert_date TEXT, is_read INTEGER DEFAULT 0)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS contract_pricing_tiers (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER,
                   start_date TEXT, end_date TEXT, annual_rent REAL, notes TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS additional_fees (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER, fee_name TEXT, amount REAL,
                   frequency TEXT DEFAULT 'مرة واحدة', tax_included INTEGER DEFAULT 0, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS contract_discounts (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER, discount_type TEXT DEFAULT 'نسبة',
                   discount_value REAL, start_date TEXT, end_date TEXT, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    ensure_columns(cur, 'payments', ['due_date','paid_date','attachment','is_temporary','temporary_note'])
    ensure_columns(cur, 'contracts', ['interval_months','tax_included','tax_rate','contract_file','is_temporary'])
    ensure_columns(cur, 'receipts', ['attachment'])
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users (username, password_hash, role, permissions) VALUES (?, ?, ?, ?)",
                    ('admin', hashlib.sha256('admin123'.encode()).hexdigest(), 'مدير', json.dumps({})))
    conn.commit(); conn.close()

init_db()

PAGE_KEYS = ["لوحة التحكم","إدارة البيانات","الدفعات","سندات القبض","التقارير","عقود منتهية","المستخدمون","الإعدادات","نسخ احتياطي"]

def get_default_permissions(role):
    if role == 'مدير': return {p: True for p in PAGE_KEYS}
    if role == 'محاسب':
        return {"لوحة التحكم":True,"إدارة البيانات":True,"الدفعات":True,"سندات القبض":True,
                "التقارير":True,"عقود منتهية":True,"المستخدمون":False,"الإعدادات":False,"نسخ احتياطي":False}
    return {p: False for p in PAGE_KEYS}

def load_permissions(uid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("PRAGMA table_info(users)")
    if 'permissions' not in [c[1] for c in cur.fetchall()]:
        cur.execute("ALTER TABLE users ADD COLUMN permissions TEXT DEFAULT '{}'"); conn.commit()
    cur.execute("SELECT role, permissions FROM users WHERE id = ?", (uid,))
    r = cur.fetchone(); conn.close()
    if not r: return {}
    role, pj = r
    try: perms = json.loads(pj or '{}')
    except: perms = {}
    dp = get_default_permissions(role)
    for k in PAGE_KEYS:
        if k not in perms: perms[k] = dp.get(k, False)
    return perms

def save_permissions(uid, perms):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET permissions = ? WHERE id = ?", (json.dumps(perms), uid))
    conn.commit(); conn.close(); st.cache_data.clear()

def has_permission(uid, page):
    if not uid: return False
    return load_permissions(uid).get(page, False)

def check_login(u, p):
    conn = get_conn(); cur = conn.cursor()
    ph = hashlib.sha256(p.strip().encode()).hexdigest()
    cur.execute("SELECT id, username, role FROM users WHERE username = ? COLLATE NOCASE AND password_hash = ?", (u.strip(), ph))
    usr = cur.fetchone(); conn.close()
    if usr: return {'id': usr[0], 'username': usr[1], 'role': usr[2]}
    return None

def load_settings():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    rows = cur.fetchall(); conn.close()
    s = {}
    for r in rows:
        try: s[r[0]] = int(r[1]) if r[0] == 'font_size' else r[1]
        except: s[r[0]] = r[1]
    defaults = {'font_size':18,'primary_color':'#4A90E2','secondary_color':'#F5A623','background_color':'#F8F9FA',
                'logo':None,'company_name':'نظام إدارة الإيجارات','telegram_bot_token':'','telegram_chat_id':'',
                'telegram_file_id':'','telegram_session_id':'','telegram_chunks':'1'}
    for k, v in defaults.items():
        if k not in s: s[k] = v
    return s

def save_setting(k, v):
    conn = get_conn(); cur = conn.cursor()
    if v is None: vs = ''
    elif isinstance(v, bytes): vs = base64.b64encode(v).decode('utf-8')
    else: vs = str(v)
    cur.execute('''INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value''', (k, vs))
    conn.commit(); conn.close(); st.cache_data.clear()

def load_logo_data():
    s = load_settings()
    lb = s.get('logo')
    if lb: return base64.b64decode(lb)
    return None

settings = load_settings()
font_size = settings['font_size']
primary_color = settings['primary_color']
secondary_color = settings['secondary_color']
background_color = settings['background_color']
logo_data = load_logo_data()
telegram_bot_token = settings.get('telegram_bot_token', '')
telegram_chat_id = settings.get('telegram_chat_id', '')
telegram_file_id = settings.get('telegram_file_id', '')

if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False; st.session_state.user_info = None

if not st.session_state.logged_in:
    st.markdown(f"""<style>.login-box{{max-width:400px;margin:auto;padding:40px;background:white;border-radius:10px;
        box-shadow:0 0 20px rgba(0,0,0,0.1);text-align:center;}}.login-box h2{{color:{primary_color};margin-bottom:20px;}}
        </style><div class="login-box"><h2>تسجيل الدخول</h2>""", unsafe_allow_html=True)
    with st.form("login_form"):
        u = st.text_input("اسم المستخدم").strip()
        p = st.text_input("كلمة المرور", type="password").strip()
        if st.form_submit_button("دخول"):
            usr = check_login(u, p)
            if usr:
                st.session_state.logged_in = True; st.session_state.user_info = usr; st.rerun()
            else: st.error("بيانات خاطئة")
    st.markdown("</div>", unsafe_allow_html=True); st.stop()

user_info = st.session_state.user_info
current_user_id = user_info['id']
current_role = user_info['role']
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

st.sidebar.markdown(f"**المستخدم:** {user_info['username']}")
st.sidebar.markdown(f"**الدور:** {current_role}")
st.sidebar.markdown("---")
if st.sidebar.button("تسجيل الخروج"):
    st.session_state.logged_in = False; st.session_state.user_info = None; st.rerun()
st.sidebar.markdown("---")
col_up, col_down = st.sidebar.columns(2)
with col_up:
    if st.button('➕ تكبير', use_container_width=True):
        save_setting('font_size', min(24, font_size + 1)); st.rerun()
with col_down:
    if st.button('➖ تصغير', use_container_width=True):
        save_setting('font_size', max(10, font_size - 1)); st.rerun()
st.sidebar.markdown("---")
menu = st.sidebar.radio("القائمة الرئيسية", PAGE_KEYS)

def generate_receipt_number(): return f"RCP-{int(time.time())}"

def generate_contract_number():
    conn = get_conn(); cur = conn.cursor()
    ts = date.today().strftime("%Y%m%d")
    while True:
        num = f"CTR-{ts}-{int(time.time() * 1000) % 100000:05d}"
        if not cur.execute("SELECT id FROM contracts WHERE contract_number = ?", (num,)).fetchone(): break
    conn.close(); return num

def get_pricing_tiers(cid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM contract_pricing_tiers WHERE contract_id=? ORDER BY start_date", (cid,))
    rows = cur.fetchall(); conn.close()
    return [dict(r) for r in rows]

def add_pricing_tier(cid, sd, ed, ar, notes=""):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('INSERT INTO contract_pricing_tiers (contract_id, start_date, end_date, annual_rent, notes) VALUES (?,?,?,?,?)',
                (cid, sd.isoformat(), ed.isoformat(), ar, notes))
    conn.commit(); conn.close(); st.cache_data.clear()

def delete_pricing_tier(tid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM contract_pricing_tiers WHERE id=?", (tid,))
    conn.commit(); conn.close(); st.cache_data.clear()

def get_additional_fees(cid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM additional_fees WHERE contract_id=? ORDER BY id", (cid,))
    rows = cur.fetchall(); conn.close()
    return [dict(r) for r in rows]

def add_additional_fee(cid, name, amt, freq, tax, notes=""):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('INSERT INTO additional_fees (contract_id, fee_name, amount, frequency, tax_included, notes) VALUES (?,?,?,?,?,?)',
                (cid, name, amt, freq, tax, notes))
    conn.commit(); conn.close(); st.cache_data.clear()

def delete_additional_fee(fid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM additional_fees WHERE id=?", (fid,))
    conn.commit(); conn.close(); st.cache_data.clear()

def get_discounts(cid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM contract_discounts WHERE contract_id=? ORDER BY start_date", (cid,))
    rows = cur.fetchall(); conn.close()
    return [dict(r) for r in rows]

def add_discount(cid, dt, dv, sd, ed, reason=""):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('INSERT INTO contract_discounts (contract_id, discount_type, discount_value, start_date, end_date, reason) VALUES (?,?,?,?,?,?)',
                (cid, dt, dv, sd.isoformat(), ed.isoformat(), reason))
    conn.commit(); conn.close(); st.cache_data.clear()

def delete_discount(did):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM contract_discounts WHERE id=?", (did,))
    conn.commit(); conn.close(); st.cache_data.clear()

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

def create_payment_schedule(cid, tid, sd, ed, ra, im):
    step = relativedelta(months=im); cur_d = sd
    conn = get_conn(); cur = conn.cursor(); cnt = 0
    while cur_d <= ed:
        ar = get_annual_rent_for_date(cid, cur_d.isoformat(), ra)
        base = ar * im / 12.0
        dp, da = calc_discount_for_date(cid, cur_d.isoformat())
        final = base * (1 - dp / 100.0) - da
        if final < 0: final = 0
        cur.execute('INSERT INTO payments (contract_id, tenant_id, due_date, amount) VALUES (?,?,?,?)',
                    (cid, tid, cur_d.isoformat(), final))
        cur_d += step; cnt += 1
    conn.commit(); conn.close()
    return cnt

def create_payment_schedule_inline(cur, cid, tid, sd, ed, ra, im):
    """نسخة داخلية تستخدم cursor موجود — بدون فتح اتصال جديد (حل Database is locked)"""
    step = relativedelta(months=im); cur_d = sd
    cnt = 0
    while cur_d <= ed:
        base = ra * im / 12.0
        cur.execute('INSERT INTO payments (contract_id, tenant_id, due_date, amount) VALUES (?,?,?,?)',
                    (cid, tid, cur_d.isoformat(), base))
        cur_d += step; cnt += 1
    return cnt

def create_temporary_payment_schedule(cid, tid, sd, ed, total_amount, interval_months, note=""):
    step = relativedelta(months=interval_months); current = sd
    payment_amount = total_amount * interval_months / 12.0
    conn = get_conn(); cur = conn.cursor(); cnt = 0
    while current <= ed:
        cur.execute('''INSERT INTO payments (contract_id, tenant_id, due_date, amount, status, notes, is_temporary, temporary_note)
                       VALUES (?, ?, ?, ?, 'مستحق', ?, 1, ?)''',
                    (cid, tid, current.isoformat(), payment_amount, note, note))
        current += step; cnt += 1
    conn.commit(); conn.close()
    return cnt

def get_unread_alerts(tid=None):
    conn = get_conn(); cur = conn.cursor()
    if tid:
        cur.execute("SELECT alert_text, alert_date FROM alerts WHERE tenant_id = ? AND is_read = 0 ORDER BY alert_date DESC", (tid,))
    else:
        cur.execute("SELECT a.alert_text, a.alert_date, t.name FROM alerts a JOIN tenants t ON a.tenant_id = t.id WHERE a.is_read = 0 ORDER BY a.alert_date DESC")
    r = cur.fetchall(); conn.close(); return r

def hijri_to_gregorian(hs):
    d, m, y = map(int, hs.split('-')); g = convert.Hijri(y, m, d).to_gregorian()
    return date(g.year, g.month, g.day)

def add_single_temporary_payment(cid, tid, dd, amt, note=""):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('''INSERT INTO payments (contract_id, tenant_id, due_date, amount, status, notes, is_temporary, temporary_note)
                   VALUES (?,?,?,?, 'مستحق', ?, 1, ?)''', (cid, tid, dd.isoformat(), amt, note, note))
    conn.commit(); conn.close(); st.cache_data.clear()

def get_temporary_payments(cid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM payments WHERE contract_id=? AND is_temporary=1 ORDER BY due_date", (cid,))
    r = cur.fetchall(); conn.close(); return [dict(x) for x in r]

def delete_temporary_payment(pid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM payments WHERE id=? AND is_temporary=1", (pid,))
    conn.commit(); conn.close(); st.cache_data.clear()

def delete_all_temporary_payments(cid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM payments WHERE contract_id=? AND is_temporary=1", (cid,))
    conn.commit(); conn.close(); st.cache_data.clear()

def get_all_expired_contracts():
    conn = get_conn(); cur = conn.cursor()
    cur.execute('''SELECT c.id, c.contract_number, t.name as tenant_name, c.end_date, c.tenant_id,
                   p.name as prop_name, c.rent_amount, c.interval_months
                   FROM contracts c JOIN tenants t ON c.tenant_id = t.id
                   JOIN properties p ON c.property_id = p.id
                   WHERE c.end_date < date('now')
                   AND c.tenant_id NOT IN (SELECT tenant_id FROM contracts WHERE status='نشط' AND end_date >= date('now'))
                   ORDER BY c.end_date DESC''')
    r = cur.fetchall(); conn.close(); return [dict(x) for x in r]

def get_total_dues_until(target_date, only_overdue=False):
    conn = get_conn(); cur = conn.cursor()
    if only_overdue:
        cur.execute('''SELECT COALESCE(SUM(amount - paid_amount), 0) FROM payments
                       WHERE due_date <= ? AND (amount - paid_amount) > 0''', (target_date.isoformat(),))
    else:
        cur.execute('''SELECT COALESCE(SUM(amount - paid_amount), 0) FROM payments WHERE due_date <= ?''', (target_date.isoformat(),))
    total = cur.fetchone()[0] or 0
    cur.execute('''SELECT COALESCE(SUM(amount - paid_amount), 0) FROM payments WHERE (amount - paid_amount) > 0''')
    total_all = cur.fetchone()[0] or 0
    cur.execute('''SELECT COUNT(*) FROM payments WHERE (amount - paid_amount) > 0''')
    count_due = cur.fetchone()[0] or 0
    cur.execute('''SELECT COUNT(*) FROM payments WHERE due_date < ? AND (amount - paid_amount) > 0''', (target_date.isoformat(),))
    count_overdue = cur.fetchone()[0] or 0
    conn.close()
    return total, total_all, count_due, count_overdue

@st.cache_data(ttl=60)
def load_tenants():
    conn = get_conn()
    df = pd.read_sql_query('''SELECT t.id as "الرقم", t.name as "الاسم", t.phone as "الهاتف",
        t.national_id as "رقم الهوية / الإقامة", t.address as "العنوان", t.region as "المنطقة",
        COALESCE(c.contract_number, 'لا يوجد عقد') as "رقم العقد",
        CASE WHEN c.id IS NULL THEN 'بدون عقد' WHEN c.end_date < date('now') THEN 'منتهي' ELSE 'ساري' END as "حالة العقد"
        FROM tenants t LEFT JOIN contracts c ON c.tenant_id = t.id AND c.status = 'نشط' ORDER BY t.name''', conn)
    conn.close(); return df

@st.cache_data(ttl=60)
def load_properties():
    conn = get_conn()
    df = pd.read_sql_query('SELECT id as "الرقم", name as "الاسم", description as "الوصف", address as "العنوان", region as "المنطقة", area as "المساحة" FROM properties', conn)
    conn.close(); return df

@st.cache_data(ttl=60)
def load_contracts():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("PRAGMA table_info(contracts)")
    cols = [c[1] for c in cur.fetchall()]
    al = {'id':'الرقم','contract_number':'رقم العقد','start_date':'تاريخ البداية','end_date':'تاريخ النهاية',
          'rent_amount':'قيمة الإيجار السنوي','interval_months':'دورية السداد (شهور)','deposit_amount':'التأمين',
          'status':'الحالة','tax_included':'شامل الضريبة','tax_rate':'نسبة الضريبة','contract_file':'ملف العقد'}
    sc = []
    for c in ['id','contract_number','start_date','end_date','rent_amount','interval_months','deposit_amount','status','tax_included','tax_rate','contract_file']:
        if c in cols: sc.append(f"c.{c} as '{al[c]}'")
        else:
            if c == 'interval_months': sc.append("1 as 'دورية السداد (شهور)'")
            elif c == 'tax_included': sc.append("0 as 'شامل الضريبة'")
            elif c == 'tax_rate': sc.append("0.15 as 'نسبة الضريبة'")
            elif c == 'contract_file': sc.append("NULL as 'ملف العقد'")
            else: sc.append(f"NULL as '{al[c]}'")
    q = f"""SELECT {', '.join(sc)}, t.name as 'اسم المستأجر', p.name as 'اسم العقار'
            FROM contracts c JOIN tenants t ON c.tenant_id = t.id JOIN properties p ON c.property_id = p.id"""
    df = pd.read_sql_query(q, conn); conn.close(); return df

@st.cache_data(ttl=60)
def load_payments(sf='الكل'):
    conn = get_conn()
    q = '''SELECT pay.id as 'الرقم', t.name as 'المستأجر', p.name as 'العقار', pay.due_date as 'تاريخ الاستحقاق',
        pay.amount as 'المبلغ', pay.paid_amount as 'المدفوع', (pay.amount - pay.paid_amount) as 'المتبقي',
        pay.status as 'الحالة', pay.paid_date as 'تاريخ السداد', pay.attachment as 'المرفق',
        pay.is_temporary as 'مؤقت', t.region as 'المنطقة', t.id as 'معرف_المستأجر' 
        FROM payments pay JOIN tenants t ON pay.tenant_id = t.id
        JOIN contracts c ON pay.contract_id = c.id JOIN properties p ON c.property_id = p.id'''
    if sf != 'الكل':
        q += " WHERE pay.status = ?"; p = (sf,)
    else: p = ()
    df = pd.read_sql_query(q, conn, params=p); conn.close(); return df

@st.cache_data(ttl=60)
def load_receipts():
    conn = get_conn()
    df = pd.read_sql_query('''SELECT r.id as 'الرقم', r.receipt_number as 'رقم السند', t.name as 'المستأجر',
        r.amount as 'المبلغ', r.receipt_date as 'التاريخ', r.payment_method as 'طريقة الدفع',
        r.notes as 'ملاحظات', r.attachment as 'المرفق', t.region as 'المنطقة', r.tenant_id as 'معرف_المستأجر'
        FROM receipts r JOIN tenants t ON r.tenant_id = t.id
        ORDER BY r.receipt_date DESC''', conn)
    conn.close(); return df

def import_tenants_from_excel(f):
    try:
        df = pd.read_excel(f)
        if "الاسم" not in df.columns: st.error("يجب أن يحتوي الملف على عمود 'الاسم'"); return
        conn = get_conn(); cur = conn.cursor()
        ex = {r[0] for r in cur.execute("SELECT name FROM tenants").fetchall()}
        add = 0
        for _, row in df.iterrows():
            n = str(row.get("الاسم", "")).strip()
            if not n or n in ex: continue
            cur.execute('INSERT INTO tenants (name, phone, national_id, address, region, notes) VALUES (?,?,?,?,?,?)',
                        (n, str(row.get("الهاتف","")).strip() if "الهاتف" in df.columns else "",
                         str(row.get("رقم الهوية / الإقامة","")).strip() if "رقم الهوية / الإقامة" in df.columns else "",
                         str(row.get("العنوان","")).strip() if "العنوان" in df.columns else "",
                         str(row.get("المنطقة","")).strip() if "المنطقة" in df.columns else "",
                         str(row.get("ملاحظات","")).strip() if "ملاحظات" in df.columns else ""))
            add += 1
        conn.commit(); conn.close(); st.cache_data.clear()
        st.toast(f"تم استيراد {add} مستأجر", icon="✅")
    except Exception as e: st.error(f"خطأ: {e}")

def import_properties_from_excel(f):
    try:
        df = pd.read_excel(f)
        if "الاسم" not in df.columns: st.error("يجب أن يحتوي الملف على عمود 'الاسم'"); return
        conn = get_conn(); cur = conn.cursor()
        ex = {r[0] for r in cur.execute("SELECT name FROM properties").fetchall()}
        add = 0
        for _, row in df.iterrows():
            n = str(row["الاسم"]).strip()
            if not n or n in ex: continue
            cur.execute('INSERT INTO properties (name, description, address, region, area) VALUES (?,?,?,?,?)',
                        (n, str(row.get("الوصف","")).strip() if "الوصف" in df.columns else "",
                         str(row.get("العنوان","")).strip() if "العنوان" in df.columns else "",
                         str(row.get("المنطقة","")).strip() if "المنطقة" in df.columns else "",
                         str(row.get("المساحة","")).strip() if "المساحة" in df.columns else ""))
            add += 1
        conn.commit(); conn.close(); st.cache_data.clear()
        st.toast(f"تم استيراد {add} عقار", icon="✅")
    except Exception as e: st.error(f"خطأ: {e}")

def import_contracts_from_excel(f):
    try:
        df = pd.read_excel(f, sheet_name=0)
        for col in ["اسم المستأجر","اسم العقار","تاريخ البداية","تاريخ النهاية"]:
            if col not in df.columns:
                st.error(f"يجب أن يحتوي الملف على عمود '{col}'")
                return
        conn = get_conn(); cur = conn.cursor()
        tenants_data = cur.execute("SELECT id, name FROM tenants").fetchall()
        properties_data = cur.execute("SELECT id, name FROM properties").fetchall()
        tenant_by_id = {t[0]: t[1] for t in tenants_data}
        prop_by_id = {p[0]: p[1] for p in properties_data}
        tenant_name_count = {}
        tenant_name_to_id = {}
        for t in tenants_data:
            tenant_name_count[t[1]] = tenant_name_count.get(t[1], 0) + 1
            if t[1] not in tenant_name_to_id:
                tenant_name_to_id[t[1]] = t[0]
        prop_name_count = {}
        prop_name_to_id = {}
        for p in properties_data:
            prop_name_count[p[1]] = prop_name_count.get(p[1], 0) + 1
            if p[1] not in prop_name_to_id:
                prop_name_to_id[p[1]] = p[0]

        imp = 0
        errors = []
        has_tenant_id_col = "رقم المستأجر" in df.columns
        has_prop_id_col = "رقم العقار" in df.columns

        for idx, row in df.iterrows():
            try:
                tid = None
                if has_tenant_id_col:
                    tid_val = row.get("رقم المستأجر", None)
                    if pd.notna(tid_val) and str(tid_val).strip() != "":
                        try:
                            tid = int(float(tid_val))
                            if tid not in tenant_by_id:
                                errors.append(f"صف {idx+2}: رقم المستأجر {tid} غير موجود")
                                continue
                        except:
                            tid = None
                if not tid:
                    tn = str(row["اسم المستأجر"]).strip()
                    if tn not in tenant_name_to_id:
                        errors.append(f"صف {idx+2}: المستأجر '{tn}' غير موجود")
                        continue
                    if tenant_name_count.get(tn, 0) > 1:
                        errors.append(f"صف {idx+2}: يوجد {tenant_name_count[tn]} مستأجرين بنفس الاسم '{tn}' — الرجاء استخدام 'رقم المستأجر'")
                        continue
                    tid = tenant_name_to_id[tn]

                pid = None
                if has_prop_id_col:
                    pid_val = row.get("رقم العقار", None)
                    if pd.notna(pid_val) and str(pid_val).strip() != "":
                        try:
                            pid = int(float(pid_val))
                            if pid not in prop_by_id:
                                errors.append(f"صف {idx+2}: رقم العقار {pid} غير موجود")
                                continue
                        except:
                            pid = None
                if not pid:
                    pn = str(row["اسم العقار"]).strip()
                    if pn not in prop_name_to_id:
                        errors.append(f"صف {idx+2}: العقار '{pn}' غير موجود")
                        continue
                    if prop_name_count.get(pn, 0) > 1:
                        errors.append(f"صف {idx+2}: يوجد {prop_name_count[pn]} عقارات بنفس الاسم '{pn}' — الرجاء استخدام 'رقم العقار'")
                        continue
                    pid = prop_name_to_id[pn]

                sd = pd.to_datetime(row["تاريخ البداية"]).date()
                ed = pd.to_datetime(row["تاريخ النهاية"]).date()
                if sd >= ed:
                    errors.append(f"صف {idx+2}: تاريخ البداية بعد النهاية")
                    continue

                cnum = str(row.get("رقم العقد","")).strip() if "رقم العقد" in df.columns else ""
                if not cnum:
                    cnum = generate_contract_number()
                else:
                    ex = cur.execute("SELECT id FROM contracts WHERE contract_number=?", (cnum,)).fetchone()
                    if ex:
                        errors.append(f"صف {idx+2}: رقم العقد '{cnum}' مكرر — تم تجاهله")
                        continue

                ra = safe_float(row.get("قيمة الإيجار السنوي", 0))
                im = int(row.get("دورية السداد (شهور)", 1)) if "دورية السداد (شهور)" in df.columns else 1
                da = safe_float(row.get("التأمين", 0))
                ti = 1 if row.get("شامل الضريبة", False) else 0
                tr = safe_float(row.get("نسبة الضريبة", 0.15))
                nt = str(row.get("ملاحظات","")).strip() if "ملاحظات" in df.columns else ""

                cur.execute('''INSERT INTO contracts (tenant_id, property_id, contract_number, start_date, end_date,
                    rent_amount, interval_months, deposit_amount, notes, tax_included, tax_rate)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (tid, pid, cnum, sd.isoformat(), ed.isoformat(), ra, im, da, nt, ti, tr))
                cid = cur.lastrowid
                create_payment_schedule_inline(cur, cid, tid, sd, ed, ra, im)
                imp += 1
                if imp % 10 == 0:
                    conn.commit()
            except Exception as e:
                errors.append(f"صف {idx+2}: {str(e)}")

        conn.commit(); conn.close(); st.cache_data.clear()
        msg = f"✅ تم استيراد {imp} عقد"
        if errors: msg += f" — فشل {len(errors)} صف"
        st.toast(msg, icon="✅")
        if errors:
            with st.expander(f"⚠️ عرض الأخطاء ({len(errors)})", expanded=True):
                for er in errors[:30]:
                    st.text(er)
    except Exception as e:
        st.error(f"خطأ: {e}")

def add_user(u, p, r):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE username = ? COLLATE NOCASE", (u.strip(),))
    if cur.fetchone()[0] > 0: conn.close(); return False, "الاسم موجود"
    ph = hashlib.sha256(p.strip().encode()).hexdigest()
    cur.execute('INSERT INTO users (username, password_hash, role, permissions) VALUES (?,?,?,?)',
                (u.strip(), ph, r, json.dumps(get_default_permissions(r))))
    conn.commit(); conn.close(); return True, "تمت الإضافة"

def delete_user(uid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit(); conn.close(); st.cache_data.clear()

def load_users():
    conn = get_conn()
    df = pd.read_sql_query("SELECT id as 'الرقم', username as 'اسم المستخدم', role as 'الدور' FROM users", conn)
    conn.close(); return df

def delete_contract(cid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM receipts WHERE contract_id=?", (cid,))
    cur.execute("DELETE FROM payments WHERE contract_id=?", (cid,))
    cur.execute("DELETE FROM contract_pricing_tiers WHERE contract_id=?", (cid,))
    cur.execute("DELETE FROM additional_fees WHERE contract_id=?", (cid,))
    cur.execute("DELETE FROM contract_discounts WHERE contract_id=?", (cid,))
    cur.execute("DELETE FROM contracts WHERE id=?", (cid,))
    conn.commit(); conn.close(); st.cache_data.clear()

def delete_tenant(tid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM tenants WHERE id=?", (tid,))
    conn.commit(); conn.close(); st.cache_data.clear()

def delete_property(pid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM properties WHERE id=?", (pid,))
    conn.commit(); conn.close(); st.cache_data.clear()

def add_tenant(n, p, ni, a, r, nt):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('INSERT INTO tenants (name, phone, national_id, address, region, notes) VALUES (?,?,?,?,?,?)', (n,p,ni,a,r,nt))
    conn.commit(); conn.close(); st.cache_data.clear()

def add_property(n, d, a, r, ar):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('INSERT INTO properties (name, description, address, region, area) VALUES (?,?,?,?,?)', (n,d,a,r,ar))
    conn.commit(); conn.close(); st.cache_data.clear()

def add_contract_full(tid, pid, cn, sd, ed, ra, im, da, ti, tr, nt, fb):
    conn = get_conn(); cur = conn.cursor()
    if cn:
        ex = cur.execute("SELECT id FROM contracts WHERE contract_number=?", (cn,)).fetchone()
        if ex:
            conn.close(); return False, "رقم العقد مستخدم بالفعل", None
    else: cn = generate_contract_number()
    cur.execute('''INSERT INTO contracts (tenant_id, property_id, contract_number, start_date, end_date,
        rent_amount, interval_months, deposit_amount, notes, tax_included, tax_rate, contract_file)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (tid, pid, cn, sd.isoformat(), ed.isoformat(), ra, im, da, nt, ti, tr, fb))
    cid = cur.lastrowid
    conn.commit(); conn.close()
    create_payment_schedule(cid, tid, sd, ed, ra, im)
    st.cache_data.clear()
    return True, "تم إنشاء العقد بنجاح", cn

def get_active_tenants():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name FROM tenants WHERE id NOT IN (SELECT tenant_id FROM contracts WHERE status='نشط') ORDER BY name")
    r = cur.fetchall(); conn.close(); return [(x[0], x[1]) for x in r]

def get_all_tenants():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name FROM tenants ORDER BY name")
    r = cur.fetchall(); conn.close(); return [(x[0], x[1]) for x in r]

def get_all_properties():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name FROM properties ORDER BY name")
    r = cur.fetchall(); conn.close(); return [(x[0], x[1]) for x in r]

def get_receipt_details(rid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM receipts WHERE id=?", (rid,))
    r = cur.fetchone(); conn.close()
    if r: return dict(r)
    return None

def get_contracts_by_tenant(tid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, contract_number FROM contracts WHERE tenant_id=?", (tid,))
    r = cur.fetchall(); conn.close(); return [(x[0], x[1]) for x in r]

def get_payments_by_contract(cid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, due_date, amount, paid_amount FROM payments WHERE contract_id=?", (cid,))
    r = cur.fetchall(); conn.close()
    return [(x[0], f"دفعة {x[0]} - {x[1]} - المطلوب: {format_currency(x[2])}") for x in r]

def update_receipt(rid, rn, tid, cid, pid, amt, rd, pm, nt, att):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT payment_id, amount FROM receipts WHERE id=?", (rid,))
    old = cur.fetchone()
    if not old: conn.close(); return False, "غير موجود"
    opid, oamt = old['payment_id'], old['amount']
    if opid and opid != pid:
        cur.execute("SELECT paid_amount, amount FROM payments WHERE id=?", (opid,))
        op = cur.fetchone()
        if op:
            np = max(0, op['paid_amount'] - oamt)
            st_ = "مدفوع" if np >= op['amount'] else "جزئي" if np > 0 else "مستحق"
            cur.execute("UPDATE payments SET paid_amount=?, status=? WHERE id=?", (np, st_, opid))
    if pid:
        cur.execute("SELECT amount, paid_amount FROM payments WHERE id=?", (pid,))
        np_ = cur.fetchone()
        if np_:
            npaid = np_['paid_amount'] + (amt if opid != pid else -oamt + amt)
            npaid = max(0, npaid)
            st_ = "مدفوع" if npaid >= np_['amount'] else "جزئي"
            cur.execute("UPDATE payments SET paid_amount=?, status=? WHERE id=?", (npaid, st_, pid))
    cur.execute('''UPDATE receipts SET receipt_number=?, tenant_id=?, contract_id=?, payment_id=?, amount=?,
                   receipt_date=?, payment_method=?, notes=?, attachment=? WHERE id=?''',
                (rn, tid, cid, pid, amt, rd.isoformat(), pm, nt, att, rid))
    conn.commit(); conn.close(); st.cache_data.clear()
    return True, "تم التعديل"

def create_compressed_backup():
    try:
        tmp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp_db_path = tmp_db.name; tmp_db.close()
        shutil.copy2("rentals.db", tmp_db_path)
        compressed_path = tmp_db_path + ".gz"
        with open(tmp_db_path, 'rb') as f_in:
            with gzip.open(compressed_path, 'wb', compresslevel=9) as f_out:
                shutil.copyfileobj(f_in, f_out)
        with open(compressed_path, 'rb') as f:
            compressed_data = f.read()
        original_size = os.path.getsize(tmp_db_path)
        compressed_size = len(compressed_data)
        try: os.remove(tmp_db_path)
        except: pass
        try: os.remove(compressed_path)
        except: pass
        return compressed_data, original_size, compressed_size
    except: return None, 0, 0

def split_into_chunks(data, chunk_size):
    return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]

def download_file_from_telegram(token, file_id):
    info_url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    resp = requests.get(info_url, timeout=60).json()
    if not resp.get('ok'): raise Exception(f"فشل: {resp}")
    file_path = resp['result']['file_path']
    download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    db_resp = requests.get(download_url, timeout=300)
    if db_resp.status_code != 200: raise Exception(f"فشل: {db_resp.status_code}")
    return db_resp.content

def restore_from_compressed(compressed_data):
    decompressed = gzip.decompress(compressed_data)
    with open("rentals.db", "wb") as f:
        f.write(decompressed)
    st.cache_data.clear()

# ================== الصفحات ==================
if menu == "لوحة التحكم" and has_permission(current_user_id, "لوحة التحكم"):
    st.subheader("📊 لوحة التحكم")
    df_t = load_tenants(); df_c = load_contracts(); df_p = load_payments()
    today = date.today(); sl = today + timedelta(days=60)
    if not df_c.empty:
        df_c['ed_dt'] = pd.to_datetime(df_c['تاريخ النهاية'])
        exp_s = df_c[(df_c['الحالة']=='نشط') & (df_c['ed_dt']>=pd.Timestamp(today)) & (df_c['ed_dt']<=pd.Timestamp(sl))]
        exp_d = df_c[(df_c['الحالة']=='نشط') & (df_c['ed_dt']<pd.Timestamp(today))]
    else: exp_s = pd.DataFrame(); exp_d = pd.DataFrame()
    total_dues_today, total_dues_all, count_due, count_overdue = get_total_dues_until(today, only_overdue=False)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("إجمالي المستأجرين", len(df_t))
    c2.metric("العقود النشطة", len(df_c[df_c["الحالة"]=="نشط"]) if not df_c.empty else 0)
    c3.metric("دفعات عليها متبقي", f"{count_due} دفعة")
    c4.metric("إجمالي المحصل", format_currency(df_p["المدفوع"].sum() if not df_p.empty else 0))
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("عقود تنتهي خلال شهرين", len(exp_s))
    c6.metric("عقود منتهية", len(exp_d))
    c7.metric("💵 إجمالي المستحقات حتى اليوم", format_currency(total_dues_today))
    c8.metric("⚠️ دفعات متأخرة", f"{count_overdue} دفعة")
    st.markdown("---")
    st.info(f"📌 **إجمالي المستحقات (كل الفترات):** {format_currency(total_dues_all)}")
    st.markdown("---")
    st.subheader("⚠️ التنبيهات")
    al = get_unread_alerts()
    if al:
        for a in al: st.warning(f"**{a[2]}** - {a[0]} ({a[1]})")
    else: st.info("لا توجد تنبيهات")
    st.markdown("---")
    st.subheader("📅 دفعات خلال 30 يوم")
    if not df_p.empty:
        up = df_p[(df_p["تاريخ الاستحقاق"]>=today.isoformat()) & (df_p["تاريخ الاستحقاق"]<=(today+timedelta(days=30)).isoformat()) & (df_p["الحالة"].isin(["مستحق","جزئي"]))]
        if not up.empty: rtl_dataframe(up[["المستأجر","العقار","تاريخ الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة"]])
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
                if uf and st.button("تنفيذ", key="btn_imp_t"): import_tenants_from_excel(uf); st.rerun()
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
                        conn = get_conn(); cur = conn.cursor()
                        ti = cur.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
                        if ti:
                            st.markdown(f"**{ti['name']}** - {ti['phone'] or '-'} - {ti['region'] or '-'}")
                            cons = cur.execute("SELECT c.contract_number, p.name, c.start_date, c.end_date, c.status FROM contracts c JOIN properties p ON c.property_id=p.id WHERE c.tenant_id=?", (tid,)).fetchall()
                            conn.close()
                            if cons: rtl_dataframe(pd.DataFrame(cons, columns=["رقم العقد","العقار","بداية","نهاية","الحالة"]))
                            if current_role == 'مدير':
                                c1, c2 = st.columns(2)
                                if c1.button("تعديل", key=f"btn_ed_t_{tid}"):
                                    st.session_state['edit_tenant_id'] = tid; st.rerun()
                                if c2.button("حذف", key=f"btn_dl_t_{tid}"):
                                    conn = get_conn(); cur = conn.cursor()
                                    hc = cur.execute("SELECT COUNT(*) FROM contracts WHERE tenant_id=?", (tid,)).fetchone()[0]
                                    if hc > 0:
                                        st.error("لديه عقود"); conn.close()
                                    else:
                                        conn.close(); delete_tenant(tid)
                                        st.toast("تم الحذف", icon="🗑️")
                                        if 'sel_tenant_edit' in st.session_state: del st.session_state['sel_tenant_edit']
                                        st.rerun()
                            if st.session_state.get('edit_tenant_id') == tid:
                                conn = get_conn(); cur = conn.cursor()
                                td = cur.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone(); conn.close()
                                if td:
                                    with st.form(f"ed_t_f_{tid}"):
                                        n = st.text_input("الاسم", value=td['name']); p = st.text_input("الهاتف", value=td['phone'] or "")
                                        ni = st.text_input("الهوية", value=td['national_id'] or ""); a = st.text_input("العنوان", value=td['address'] or "")
                                        r = st.text_input("المنطقة", value=td['region'] or ""); nt = st.text_area("ملاحظات", value=td['notes'] or "")
                                        if st.form_submit_button("حفظ"):
                                            conn = get_conn(); cur = conn.cursor()
                                            cur.execute("UPDATE tenants SET name=?, phone=?, national_id=?, address=?, region=?, notes=? WHERE id=?",
                                                        (n,p,ni,a,r,nt,tid))
                                            conn.commit(); conn.close(); st.cache_data.clear()
                                            st.toast("تم التحديث", icon="✅"); st.session_state['edit_tenant_id'] = None; st.rerun()
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
                if uf and st.button("تنفيذ", key="btn_imp_p"): import_properties_from_excel(uf); st.rerun()
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
                        conn = get_conn(); cur = conn.cursor()
                        pi = cur.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
                        if pi:
                            st.markdown(f"**{pi['name']}** - {pi['region'] or '-'}")
                            cons = cur.execute("SELECT c.contract_number, t.name, c.start_date, c.end_date, c.status FROM contracts c JOIN tenants t ON c.tenant_id=t.id WHERE c.property_id=?", (pid,)).fetchall()
                            conn.close()
                            if cons: rtl_dataframe(pd.DataFrame(cons, columns=["رقم العقد","المستأجر","بداية","نهاية","الحالة"]))
                            if current_role == 'مدير':
                                c1, c2 = st.columns(2)
                                if c1.button("تعديل", key=f"btn_ed_p_{pid}"):
                                    st.session_state['edit_property_id'] = pid; st.rerun()
                                if c2.button("حذف", key=f"btn_dl_p_{pid}"):
                                    conn = get_conn(); cur = conn.cursor()
                                    hc = cur.execute("SELECT COUNT(*) FROM contracts WHERE property_id=?", (pid,)).fetchone()[0]
                                    if hc > 0:
                                        st.error("لديه عقود"); conn.close()
                                    else:
                                        conn.close(); delete_property(pid)
                                        st.toast("تم الحذف", icon="🗑️")
                                        if 'sel_prop_edit' in st.session_state: del st.session_state['sel_prop_edit']
                                        st.rerun()
                            if st.session_state.get('edit_property_id') == pid:
                                conn = get_conn(); cur = conn.cursor()
                                pd_ = cur.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone(); conn.close()
                                if pd_:
                                    with st.form(f"ed_p_f_{pid}"):
                                        n = st.text_input("الاسم", value=pd_['name']); d = st.text_area("الوصف", value=pd_['description'] or "")
                                        a = st.text_input("العنوان", value=pd_['address'] or ""); r = st.text_input("المنطقة", value=pd_['region'] or "")
                                        ar = st.text_input("المساحة", value=pd_['area'] or "")
                                        if st.form_submit_button("حفظ"):
                                            conn = get_conn(); cur = conn.cursor()
                                            cur.execute("UPDATE properties SET name=?, description=?, address=?, region=?, area=? WHERE id=?", (n,d,a,r,ar,pid))
                                            conn.commit(); conn.close(); st.cache_data.clear()
                                            st.toast("تم التحديث", icon="✅"); st.session_state['edit_property_id'] = None; st.rerun()
                else: st.info("لا نتائج")
        with t3:
            st.subheader("📄 العقود")
            ci1, ci2 = st.columns(2)
            with ci1:
                conn = get_conn(); cur = conn.cursor()
                tenants_list = cur.execute("SELECT id, name, region FROM tenants ORDER BY name").fetchall()
                props_list = cur.execute("SELECT id, name, region FROM properties ORDER BY name").fetchall()
                conn.close()
                df = pd.DataFrame(columns=["رقم العقد","رقم المستأجر","اسم المستأجر","رقم العقار","اسم العقار",
                                            "تاريخ البداية","تاريخ النهاية","قيمة الإيجار السنوي",
                                            "دورية السداد (شهور)","التأمين","شامل الضريبة","نسبة الضريبة","ملاحظات"])
                df.loc[0] = ["CTR-2025-001","","أحمد","","عمارة","2025-01-01","2025-12-31",60000,6,5000,0,0.15,""]
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr:
                    df.to_excel(wr, index=False, sheet_name='العقود')
                    if tenants_list:
                        pd.DataFrame(tenants_list, columns=["رقم المستأجر","اسم المستأجر","المنطقة"]).to_excel(wr, index=False, sheet_name='المستأجرين')
                    if props_list:
                        pd.DataFrame(props_list, columns=["رقم العقار","اسم العقار","المنطقة"]).to_excel(wr, index=False, sheet_name='العقارات')
                o.seek(0)
                st.download_button("تحميل قالب العقود", data=o.getvalue(), file_name="قالب_العقود.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_tmpl_c")
                st.caption("💡 القالب يحتوي على 3 أوراق: العقود + المستأجرين + العقارات")
            with ci2:
                uf = st.file_uploader("استيراد", type=["xlsx","xls"], key="imp_c")
                if uf and st.button("تنفيذ", key="btn_imp_c"): import_contracts_from_excel(uf); st.rerun()
            if st.button("➕ إضافة عقد", key="btn_add_c"): st.session_state['show_add_c'] = True
            if st.session_state.get('show_add_c'):
                at = get_active_tenants()
                if not at: st.warning("لا يوجد مستأجرين متاحين")
                else:
                    with st.form("add_c_f"):
                        to = {t[0]: t[1] for t in at}
                        tid = st.selectbox("المستأجر *", options=list(to.keys()), format_func=lambda x: to[x])
                        po = {p[0]: p[1] for p in get_all_properties()}
                        if not po: st.warning("لا توجد عقارات")
                        else:
                            pid = st.selectbox("العقار *", options=list(po.keys()), format_func=lambda x: po[x])
                            cn_input = st.text_input("رقم العقد (اتركه فارغاً للتوليد التلقائي)", value="",
                                                    placeholder="مثال: CTR-2025-001")
                            sd = st.date_input("البداية", value=date.today())
                            ed = st.date_input("النهاية", value=date.today() + relativedelta(years=1))
                            ra = st.number_input("الإيجار السنوي", min_value=0.0, step=1000.0, value=0.0)
                            im = st.number_input("الدورية (شهور)", min_value=1, value=1)
                            da = st.number_input("التأمين", min_value=0.0, step=100.0, value=0.0)
                            ti = st.checkbox("شامل الضريبة")
                            tr = st.number_input("نسبة الضريبة (%)", min_value=0.0, max_value=100.0, value=15.0) / 100
                            nt = st.text_area("ملاحظات")
                            cf = st.file_uploader("ملف العقد", type=["pdf"])
                            cs, cc = st.columns(2)
                            s = cs.form_submit_button("حفظ"); c = cc.form_submit_button("إلغاء")
                            if s:
                                if sd >= ed: st.error("تاريخ النهاية يجب أن يكون بعد البداية")
                                else:
                                    fb = cf.read() if cf else None
                                    ok, msg, final_cn = add_contract_full(tid, pid, cn_input.strip() if cn_input.strip() else "",
                                                                         sd, ed, ra, im, da, 1 if ti else 0, tr, nt, fb)
                                    if ok:
                                        st.toast(f"✅ {msg} - رقم العقد: {final_cn}", icon="✅")
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
                    fc = fc.reset_index(drop=True)
                    fc_show = fc.copy()
                    fc_show["الرقم"] = range(1, len(fc_show) + 1)
                    display_dataframe_with_reorder(fc_show, "contracts")
                    c_options = {}
                    for i, row in fc.iterrows():
                        c_options[row['الرقم']] = f"{i+1} - {row['رقم العقد']} - {row['اسم المستأجر']}"
                    cid = st.selectbox("اختر عقد", options=list(c_options.keys()),
                                       format_func=lambda x: c_options[x], key="sel_contract")
                    if cid:
                        conn = get_conn(); cur = conn.cursor()
                        ci = cur.execute('''SELECT c.*, t.name as tenant_name, t.phone as tphone, t.region as tregion,
                            p.name as prop_name, p.address as paddr, p.region as pregion FROM contracts c
                            JOIN tenants t ON c.tenant_id=t.id JOIN properties p ON c.property_id=p.id WHERE c.id=?''', (cid,)).fetchone()
                        conn.close()
                        if ci:
                            st.markdown("### تفاصيل العقد")
                            c1, c2 = st.columns(2)
                            c1.write(f"**رقم العقد:** {ci['contract_number']}")
                            c1.write(f"**المستأجر:** {ci['tenant_name']}")
                            c1.write(f"**الهاتف:** {ci['tphone'] or '-'}")
                            c2.write(f"**العقار:** {ci['prop_name']}")
                            c2.write(f"**العنوان:** {ci['paddr'] or '-'}")
                            st.divider()
                            st.write(f"**البداية:** {ci['start_date']} | **النهاية:** {ci['end_date']}")
                            st.write(f"**الإيجار:** {format_currency(ci['rent_amount'])} | **الدورية:** كل {ci['interval_months']} شهر")
                            st.write(f"**التأمين:** {format_currency(ci['deposit_amount'])} | **شامل الضريبة:** {'نعم' if ci['tax_included'] else 'لا'} | **الضريبة:** {safe_float(ci['tax_rate'])*100:.1f}%")
                            st.write(f"**ملاحظات:** {ci['notes'] or '-'}")
                            if ci['contract_file']:
                                st.download_button("📥 ملف العقد", data=ci['contract_file'], file_name=f"contract_{cid}.pdf", mime="application/pdf", key=f"dl_cf_{cid}")
                            st.markdown("---")
                            adv = st.tabs(["📊 أسعار متدرجة","💧 رسوم إضافية","🎁 خصومات"])
                            with adv[0]:
                                st.markdown("#### الأسعار المتدرجة")
                                tiers = get_pricing_tiers(cid)
                                if tiers:
                                    dft = pd.DataFrame(tiers)[['id','start_date','end_date','annual_rent','notes']]
                                    dft.columns = ['الرقم','من','إلى','الإيجار','ملاحظات']
                                    rtl_dataframe(dft)
                                    dt = st.selectbox("حذف", [t['id'] for t in tiers], key=f"dt_{cid}")
                                    if st.button("🗑️ حذف", key=f"bdt_{cid}"): delete_pricing_tier(dt); st.toast("تم الحذف", icon="🗑️"); st.rerun()
                                with st.form(f"tier_f_{cid}"):
                                    c1,c2,c3 = st.columns(3)
                                    ts = c1.date_input("من", value=parse_date_safe(ci['start_date']))
                                    te = c2.date_input("إلى", value=parse_date_safe(ci['end_date']))
                                    tr_ = c3.number_input("الإيجار السنوي", min_value=0.0, step=1000.0, value=float(ci['rent_amount']))
                                    tn = st.text_input("ملاحظات")
                                    if st.form_submit_button("➕ إضافة فترة"):
                                        add_pricing_tier(cid, ts, te, tr_, tn); st.toast("تمت الإضافة", icon="✅"); st.rerun()
                                if st.button("🔄 إعادة توليد الدفعات", key=f"rgt_{cid}"):
                                    conn = get_conn(); cur = conn.cursor()
                                    cur.execute("DELETE FROM payments WHERE contract_id=? AND (is_temporary IS NULL OR is_temporary=0)", (cid,))
                                    conn.commit(); conn.close()
                                    cnt = create_payment_schedule(cid, ci['tenant_id'], parse_date_safe(ci['start_date']), parse_date_safe(ci['end_date']), ci['rent_amount'], ci['interval_months'])
                                    st.toast(f"تم إعادة توليد {cnt} دفعة", icon="✅"); st.rerun()
                            with adv[1]:
                                st.markdown("#### رسوم إضافية (معفاة من الضريبة افتراضياً)")
                                fees = get_additional_fees(cid)
                                if fees:
                                    dff = pd.DataFrame(fees)[['id','fee_name','amount','frequency','tax_included','notes']]
                                    dff.columns = ['الرقم','الرسم','المبلغ','الدورية','خاضع','ملاحظات']
                                    dff['خاضع'] = dff['خاضع'].apply(lambda x: 'نعم' if x else 'لا')
                                    rtl_dataframe(dff)
                                    df_ = st.selectbox("حذف", [f['id'] for f in fees], key=f"df_{cid}")
                                    if st.button("🗑️ حذف", key=f"bdf_{cid}"): delete_additional_fee(df_); st.toast("تم الحذف", icon="🗑️"); st.rerun()
                                with st.form(f"fee_f_{cid}"):
                                    c1,c2,c3 = st.columns(3)
                                    fn = c1.text_input("الرسم", value="مصاريف مياه")
                                    fa = c2.number_input("المبلغ", min_value=0.0, step=100.0, value=3000.0)
                                    ff = c3.selectbox("الدورية", ["مرة واحدة","شهري","ربع سنوي","سنوي"])
                                    ft = st.checkbox("خاضع للضريبة", value=False)
                                    fnt = st.text_input("ملاحظات")
                                    if st.form_submit_button("➕ إضافة رسم"):
                                        add_additional_fee(cid, fn, fa, ff, 1 if ft else 0, fnt); st.toast("تمت الإضافة", icon="✅"); st.rerun()
                            with adv[2]:
                                st.markdown("#### الخصومات لفترة محددة")
                                discs = get_discounts(cid)
                                if discs:
                                    dfd = pd.DataFrame(discs)[['id','discount_type','discount_value','start_date','end_date','reason']]
                                    dfd.columns = ['الرقم','النوع','القيمة','من','إلى','السبب']
                                    rtl_dataframe(dfd)
                                    dd_ = st.selectbox("حذف", [d['id'] for d in discs], key=f"dd_{cid}")
                                    if st.button("🗑️ حذف", key=f"bdd_{cid}"): delete_discount(dd_); st.toast("تم الحذف", icon="🗑️"); st.rerun()
                                with st.form(f"disc_f_{cid}"):
                                    c1,c2 = st.columns(2)
                                    dtp = c1.selectbox("النوع", ["نسبة","مبلغ"])
                                    dv = c2.number_input("القيمة", min_value=0.0, step=1.0, value=10.0)
                                    c3,c4 = st.columns(2)
                                    ds = c3.date_input("من", value=parse_date_safe(ci['start_date']))
                                    de = c4.date_input("إلى", value=parse_date_safe(ci['end_date']))
                                    dr = st.text_input("السبب", value="ظروف طارئة")
                                    if st.form_submit_button("➕ إضافة خصم"):
                                        add_discount(cid, dtp, dv, ds, de, dr); st.toast("تمت الإضافة", icon="✅"); st.rerun()
                                if st.button("🔄 إعادة توليد الدفعات مع الخصومات", key=f"rgd_{cid}"):
                                    conn = get_conn(); cur = conn.cursor()
                                    cur.execute("DELETE FROM payments WHERE contract_id=? AND (is_temporary IS NULL OR is_temporary=0)", (cid,))
                                    conn.commit(); conn.close()
                                    cnt = create_payment_schedule(cid, ci['tenant_id'], parse_date_safe(ci['start_date']), parse_date_safe(ci['end_date']), ci['rent_amount'], ci['interval_months'])
                                    st.toast(f"تم إعادة توليد {cnt} دفعة", icon="✅"); st.rerun()
                            if current_role == 'مدير':
                                c1, c2 = st.columns(2)
                                if c1.button("تعديل العقد", key=f"btn_ed_c_{cid}"):
                                    st.session_state['edit_contract_id'] = cid; st.rerun()
                                if c2.button("حذف العقد", key=f"btn_dl_c_{cid}"):
                                    delete_contract(cid); st.toast("تم الحذف", icon="🗑️"); st.rerun()
                            if st.session_state.get('edit_contract_id') == cid:
                                conn = get_conn(); cur = conn.cursor()
                                cd = cur.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone(); conn.close()
                                if cd:
                                    dft = load_tenants(); dfp = load_properties()
                                    with st.form(f"ed_c_f_{cid}"):
                                        tid = st.selectbox("المستأجر", dft["الرقم"], index=dft.index[dft["الرقم"]==cd['tenant_id']][0], format_func=lambda x: dft[dft["الرقم"]==x]["الاسم"].iloc[0])
                                        pid = st.selectbox("العقار", dfp["الرقم"], index=dfp.index[dfp["الرقم"]==cd['property_id']][0], format_func=lambda x: dfp[dfp["الرقم"]==x]["الاسم"].iloc[0])
                                        cn = st.text_input("رقم العقد", value=cd['contract_number'])
                                        sd = st.date_input("البداية", value=parse_date_safe(cd['start_date']))
                                        ed = st.date_input("النهاية", value=parse_date_safe(cd['end_date']))
                                        ra = st.number_input("الإيجار", min_value=0.0, step=100.0, value=float(cd['rent_amount']))
                                        im = st.number_input("الدورية", min_value=1, value=int(cd['interval_months']))
                                        da = st.number_input("التأمين", min_value=0.0, step=100.0, value=float(safe_float(cd['deposit_amount'])))
                                        ti = st.checkbox("شامل الضريبة", value=bool(cd['tax_included']))
                                        tr = st.number_input("الضريبة (%)", min_value=0.0, value=float(safe_float(cd['tax_rate']))*100) / 100
                                        nt = st.text_area("ملاحظات", value=cd['notes'] or "")
                                        nf = st.file_uploader("ملف جديد", type=["pdf"])
                                        if st.form_submit_button("حفظ"):
                                            if sd >= ed: st.error("تواريخ خاطئة")
                                            else:
                                                fb = cd['contract_file']
                                                if nf: fb = nf.read()
                                                conn = get_conn(); cur = conn.cursor()
                                                cur.execute('''UPDATE contracts SET tenant_id=?, property_id=?, contract_number=?, start_date=?, end_date=?,
                                                    rent_amount=?, interval_months=?, deposit_amount=?, tax_included=?, tax_rate=?, notes=?, contract_file=?
                                                    WHERE id=?''',
                                                    (tid, pid, cn, sd.isoformat(), ed.isoformat(), ra, im, da, 1 if ti else 0, tr, nt, fb, cid))
                                                cur.execute("DELETE FROM payments WHERE contract_id=? AND (is_temporary IS NULL OR is_temporary=0)", (cid,))
                                                conn.commit(); conn.close()
                                                cnt = create_payment_schedule(cid, tid, sd, ed, ra, im)
                                                st.cache_data.clear(); st.toast(f"تم التحديث ({cnt} دفعة)", icon="✅")
                                                st.session_state['edit_contract_id'] = None; st.rerun()
                else: st.info("لا عقود")

elif menu == "الدفعات":
    st.subheader("💰 متابعة الدفعات")
    if not has_permission(current_user_id, "الدفعات"): st.error("لا تملك صلاحية")
    else:
        t1, t2 = st.tabs(["عرض الدفعات","تعديل دفعة"])
        with t1:
            sf = st.selectbox("الحالة", ["الكل","مستحق","مدفوع","متأخر","جزئي"])
            dfp = load_payments(sf)
            if not dfp.empty:
                sq = st.text_input("بحث", key="ps_")
                f = dfp[dfp["المستأجر"].str.contains(sq, case=False, na=False)] if sq else dfp
                if not f.empty:
                    f_disp = f.drop(columns=["المرفق","معرف_المستأجر"])
                    display_dataframe_with_reorder(f_disp, "payments")
                    st.markdown("### 📄 خيارات الطباعة")
                    orient1 = st.radio("اتجاه الصفحة", ["عمودي (Portrait)", "أفقي (Landscape)"], horizontal=True, key="pay_orient")
                    landscape_choice = (orient1 == "أفقي (Landscape)")
                    c1, c2 = st.columns(2)
                    with c1:
                        o = io.BytesIO()
                        with pd.ExcelWriter(o, engine='xlsxwriter') as wr: f_disp.to_excel(wr, index=False)
                        st.download_button("تحميل Excel", data=o.getvalue(), file_name="دفعات.xlsx", key="dl_pays")
                    with c2: export_df_to_pdf(f_disp, "بيان الدفعات", "دفعات.pdf", landscape_mode=landscape_choice)
                else: st.info("لا نتائج")
            else: st.info("لا دفعات")
        with t2:
            if current_role == 'مدير':
                dfp = load_payments()
                if not dfp.empty:
                    st.markdown("### 🔎 فلاتر البحث")
                    f1, f2, f3 = st.columns(3)
                    with f1:
                        tenant_names = ["الكل"] + sorted(dfp["المستأجر"].dropna().unique().tolist())
                        sel_tenant = st.selectbox("المستأجر", tenant_names, key="pe_tenant")
                    with f2:
                        region_names = ["الكل"] + sorted([r for r in dfp["المنطقة"].dropna().unique().tolist() if r])
                        sel_region = st.selectbox("المنطقة", region_names, key="pe_region")
                    with f3:
                        search_pay = st.text_input("بحث", key="pe_search")
                    dfp_f = dfp.copy()
                    if sel_tenant != "الكل": dfp_f = dfp_f[dfp_f["المستأجر"] == sel_tenant]
                    if sel_region != "الكل": dfp_f = dfp_f[dfp_f["المنطقة"] == sel_region]
                    if search_pay.strip():
                        mask = dfp_f.apply(lambda row: search_pay.lower() in str(row.get("المستأجر","")).lower() or
                                                      search_pay.lower() in str(row.get("العقار","")).lower() or
                                                      search_pay.lower() in str(row.get("الحالة","")).lower(), axis=1)
                        dfp_f = dfp_f[mask]
                    if dfp_f.empty:
                        st.info("لا نتائج")
                    else:
                        st.markdown(f"**عدد النتائج:** {len(dfp_f)}")
                        dfp_show = dfp_f[["الرقم","المستأجر","العقار","تاريخ الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة","تاريخ السداد"]].copy()
                        rtl_dataframe(dfp_show, key="payments_edit_table")
                        pay_options = {row["الرقم"]: f"دفعة {row['الرقم']} - {row['المستأجر']} - {row['العقار']} - {format_currency(row['المبلغ'])} ({row['الحالة']})" 
                                       for _, row in dfp_f.iterrows()}
                        pid = st.selectbox("اختر دفعة للتعديل", options=list(pay_options.keys()),
                                          format_func=lambda x: pay_options[x], key="sel_pay_edit")
                        if pid:
                            conn = get_conn(); cur = conn.cursor()
                            pd_ = cur.execute("SELECT due_date, amount, status, notes FROM payments WHERE id=?", (pid,)).fetchone()
                            conn.close()
                            if pd_:
                                with st.form(f"ed_pay_f_{pid}"):
                                    dd = st.date_input("الاستحقاق", value=parse_date_safe(pd_[0]))
                                    am = st.number_input("المبلغ", min_value=0.0, step=100.0, value=float(pd_[1]))
                                    stt = st.selectbox("الحالة", ["مستحق","مدفوع","جزئي","متأخر"], index=["مستحق","مدفوع","جزئي","متأخر"].index(pd_[2]))
                                    nt = st.text_area("ملاحظات", value=pd_[3] or "")
                                    if st.form_submit_button("حفظ"):
                                        conn = get_conn(); cur = conn.cursor()
                                        cur.execute("UPDATE payments SET due_date=?, amount=?, status=?, notes=? WHERE id=?",
                                                    (dd.isoformat(), am, stt, nt, pid))
                                        conn.commit(); conn.close(); st.cache_data.clear()
                                        st.toast("تم التعديل", icon="✅"); st.rerun()
                else: st.info("لا دفعات")
            else: st.warning("ليس لديك صلاحية")

elif menu == "سندات القبض":
    st.subheader("🧾 سندات القبض")
    if not has_permission(current_user_id, "سندات القبض"): st.error("لا تملك صلاحية")
    else:
        t1, t2 = st.tabs(["تسجيل سداد","سجل السندات"])
        with t1:
            if current_role in ['مدير','محاسب']:
                dft = load_tenants()
                if dft.empty: st.warning("لا مستأجرين")
                else:
                    tid = st.selectbox("المستأجر", dft["الرقم"], format_func=lambda x: dft[dft["الرقم"]==x]["الاسم"].iloc[0], key="sel_tenant_pay")
                    today = date.today()
                    conn = get_conn(); cur = conn.cursor()
                    dues = cur.execute('''SELECT id, due_date, amount, paid_amount, (amount - paid_amount) as remaining
                        FROM payments WHERE tenant_id=? AND status != 'مدفوع' AND due_date <= ? ORDER BY due_date''',
                        (tid, today.isoformat())).fetchall()
                    conn.close()
                    if not dues: st.info("لا دفعات مستحقة")
                    else:
                        dfd = pd.DataFrame(dues, columns=["رقم الدفعة","الاستحقاق","المبلغ","المدفوع","المتبقي"])
                        rtl_dataframe(dfd)
                        pid = st.selectbox("الدفعة", dfd["رقم الدفعة"].tolist(), format_func=lambda x: f"دفعة {x}", key="sel_pay_pay")
                        if pid:
                            od = [d for d in dues if d[0]==pid][0]; rem = od[4]
                            pdte = st.date_input("تاريخ السداد", value=today, key="pay_date_in")
                            am = st.number_input("المبلغ", min_value=0.0, max_value=float(rem), value=float(rem), step=100.0, key="pay_amt_in")
                            mt = st.selectbox("طريقة الدفع", ["نقدي","تحويل بنكي","شيك","دفع في المنصة"], key="pay_mt_in")
                            att = st.file_uploader("مرفق", type=["pdf","png","jpg","jpeg"], key="pay_att_in")
                            if st.button("تسجيل السداد", key="btn_register_pay"):
                                if am <= 0: st.error("المبلغ > 0")
                                else:
                                    fb = att.read() if att else None
                                    conn = get_conn(); cur = conn.cursor()
                                    pd_ = cur.execute("SELECT amount, paid_amount, contract_id FROM payments WHERE id=?", (pid,)).fetchone()
                                    npaid = pd_[1] + am
                                    stt = "مدفوع" if npaid >= pd_[0] else "جزئي"
                                    cur.execute("UPDATE payments SET paid_amount=?, paid_date=?, status=?, attachment=? WHERE id=?",
                                                (npaid, pdte.isoformat(), stt, fb, pid))
                                    rn = generate_receipt_number()
                                    cur.execute('''INSERT INTO receipts (receipt_number, tenant_id, contract_id, payment_id, amount, receipt_date, payment_method, attachment)
                                        VALUES (?,?,?,?,?,?,?,?)''', (rn, tid, pd_[2], pid, am, pdte.isoformat(), mt, fb))
                                    conn.commit(); conn.close(); st.cache_data.clear()
                                    st.toast(f"تم تسجيل {format_currency(am)}", icon="✅"); st.rerun()
            else: st.warning("ليس لديك صلاحية")
        with t2:
            dfr = load_receipts()
            if not dfr.empty:
                st.markdown("### 🔎 فلاتر البحث")
                f1, f2, f3 = st.columns(3)
                with f1:
                    tenant_names = ["الكل"] + sorted(dfr["المستأجر"].dropna().unique().tolist())
                    sel_tenant = st.selectbox("المستأجر", tenant_names, key="flt_rec_tenant")
                with f2:
                    regions = ["الكل"] + sorted([r for r in dfr["المنطقة"].dropna().unique().tolist() if r])
                    sel_region = st.selectbox("المنطقة", regions, key="flt_rec_region")
                with f3:
                    search_txt = st.text_input("بحث", key="flt_rec_search")
                dfr_f = dfr.copy()
                if sel_tenant != "الكل": dfr_f = dfr_f[dfr_f["المستأجر"] == sel_tenant]
                if sel_region != "الكل": dfr_f = dfr_f[dfr_f["المنطقة"] == sel_region]
                if search_txt.strip():
                    mask = dfr_f.apply(lambda row: search_txt.lower() in str(row.get("رقم السند","")).lower() or
                                                  search_txt.lower() in str(row.get("ملاحظات","") or "").lower(), axis=1)
                    dfr_f = dfr_f[mask]
                if dfr_f.empty:
                    st.info("لا نتائج مطابقة للفلاتر")
                else:
                    dfr_show = dfr_f.drop(columns=["المرفق","معرف_المستأجر"])
                    display_dataframe_with_reorder(dfr_show, "receipts")
                    rid = st.selectbox("اختر سند", dfr_f["الرقم"],
                                       format_func=lambda x: f"{dfr_f[dfr_f['الرقم']==x]['رقم السند'].iloc[0]}", key="sel_receipt_edit")
                    if rid:
                        c1, c2, c3 = st.columns(3)
                        with c1:
                            pdf_data = print_receipt(rid)
                            if pdf_data: st.download_button("طباعة", data=pdf_data, file_name=f"r_{rid}.pdf", mime="application/pdf", key=f"dl_pdf_{rid}")
                        with c2:
                            conn = get_conn(); cur = conn.cursor()
                            att = cur.execute("SELECT attachment FROM receipts WHERE id=?", (rid,)).fetchone()
                            conn.close()
                            if att and att[0]:
                                st.download_button("تحميل المرفق", data=att[0], file_name=f"r_{rid}_att",
                                                  mime="application/octet-stream", key=f"dl_att_{rid}")
                        with c3:
                            if current_role == 'مدير':
                                if st.button("تعديل السند", key=f"btn_ed_r_{rid}"):
                                    st.session_state['edit_receipt_id'] = rid; st.rerun()
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
                                    cs = get_contracts_by_tenant(tid)
                                    cn = {c[0]: c[1] for c in cs}
                                    cid = st.selectbox("العقد", options=list(cn.keys()),
                                                       index=list(cn.keys()).index(rd['contract_id']) if rd['contract_id'] in cn else 0,
                                                       format_func=lambda x: cn[x])
                                    ps = get_payments_by_contract(cid)
                                    po = [(None, "بدون ربط")] + ps
                                    pl = {p[0]: p[1] for p in po}
                                    pid = st.selectbox("الدفعة", options=list(pl.keys()),
                                                       index=list(pl.keys()).index(rd['payment_id']) if rd['payment_id'] in pl else 0,
                                                       format_func=lambda x: pl[x])
                                    am = st.number_input("المبلغ", min_value=0.0, step=100.0, value=float(rd['amount']))
                                    rdte = st.date_input("التاريخ", value=parse_date_safe(rd['receipt_date']))
                                    opts = ["نقدي","تحويل بنكي","شيك","دفع في المنصة"]
                                    mt = st.selectbox("طريقة الدفع", opts,
                                                      index=opts.index(rd['payment_method']) if rd['payment_method'] in opts else 0)
                                    nt = st.text_area("ملاحظات", value=rd['notes'] or "")
                                    nf = st.file_uploader("مرفق جديد (اختياري)", type=["pdf","png","jpg","jpeg"])
                                    if st.form_submit_button("حفظ التعديلات"):
                                        fb = rd['attachment']
                                        if nf: fb = nf.read()
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
        if not exp: st.info("لا توجد عقود منتهية بدون تجديد")
        else:
            dfe = pd.DataFrame(exp)
            dfe = dfe.rename(columns={'id':'رقم_داخلي','contract_number':'رقم العقد','tenant_name':'المستأجر',
                                       'end_date':'تاريخ الانتهاء','prop_name':'العقار','rent_amount':'الإيجار',
                                       'interval_months':'الدورية'})
            rtl_dataframe(dfe[['رقم العقد','المستأجر','العقار','تاريخ الانتهاء','الإيجار','الدورية']])
            co = {e['id']: f"{e['contract_number']} - {e['tenant_name']} - انتهى {e['end_date']}" for e in exp}
            sel = st.selectbox("اختر عقد", options=list(co.keys()), format_func=lambda x: co[x], key="sel_expired")
            if sel:
                ci = next(e for e in exp if e['id'] == sel)
                st.markdown(f"### العقد: {ci['contract_number']}")
                st.info(f"**المستأجر:** {ci['tenant_name']} | **العقار:** {ci['prop_name']} | "
                        f"**الإيجار السنوي السابق:** {format_currency(ci['rent_amount'])} | "
                        f"**الدورية السابقة:** كل {ci['interval_months']} شهر")
                st.markdown("---")
                mode = st.radio("طريقة الإضافة", ["📋 جدول دفعات مؤقت (مثل العقد)", "➕ دفعة واحدة فقط"], horizontal=True, key=f"mode_{sel}")
                if mode == "📋 جدول دفعات مؤقت (مثل العقد)":
                    st.markdown("#### توليد جدول دفعات مؤقت")
                    with st.form(f"temp_schedule_f_{sel}"):
                        c1, c2 = st.columns(2)
                        with c1: sd = st.date_input("من تاريخ", value=date.today())
                        with c2: ed = st.date_input("إلى تاريخ", value=date.today() + relativedelta(years=1))
                        c3, c4 = st.columns(2)
                        with c3: total_annual = st.number_input("إجمالي الإيجار السنوي", min_value=0.0, step=1000.0, value=float(ci['rent_amount'] or 0))
                        with c4: interval_months = st.number_input("دورية السداد (شهور)", min_value=1, value=int(ci['interval_months'] or 6))
                        note = st.text_input("ملاحظة عامة", value="فترة مؤقتة حتى تجديد العقد")
                        if total_annual > 0 and interval_months > 0 and sd < ed:
                            payment_amount = total_annual * interval_months / 12.0
                            st.info(f"سيتم إنشاء دفعات بقيمة **{format_currency(payment_amount)}** لكل دفعة")
                        if st.form_submit_button("📋 توليد الجدول"):
                            if sd >= ed: st.error("تاريخ النهاية يجب أن يكون بعد البداية")
                            elif total_annual <= 0: st.error("المبلغ يجب أن يكون أكبر من صفر")
                            else:
                                cnt = create_temporary_payment_schedule(sel, ci['tenant_id'], sd, ed, total_annual, interval_months, note)
                                st.toast(f"تم توليد {cnt} دفعة مؤقتة", icon="✅"); st.rerun()
                else:
                    st.markdown("#### إضافة دفعة واحدة يدوياً")
                    with st.form(f"temp_single_f_{sel}"):
                        c1, c2 = st.columns(2)
                        with c1: dd = st.date_input("تاريخ الاستحقاق", value=date.today())
                        with c2: am = st.number_input("المبلغ", min_value=0.0, step=100.0, value=float(ci['rent_amount'] or 0))
                        nt = st.text_input("ملاحظة", value="دفعة مؤقتة")
                        if st.form_submit_button("➕ إضافة دفعة"):
                            if am <= 0: st.error("المبلغ > 0")
                            else:
                                add_single_temporary_payment(sel, ci['tenant_id'], dd, am, nt)
                                st.toast("تمت الإضافة", icon="✅"); st.rerun()
                st.markdown("---")
                tp = get_temporary_payments(sel)
                if tp:
                    st.markdown(f"#### الدفعات المؤقتة الحالية ({len(tp)} دفعة)")
                    dft = pd.DataFrame(tp)
                    show = ['id','due_date','amount','paid_amount','status','temporary_note']
                    dft_s = dft[[c for c in show if c in dft.columns]]
                    dft_s = dft_s.rename(columns={'id':'الرقم','due_date':'الاستحقاق','amount':'المبلغ',
                                                   'paid_amount':'المدفوع','status':'الحالة','temporary_note':'ملاحظة'})
                    rtl_dataframe(dft_s)
                    tot = dft['amount'].sum()
                    st.write(f"**إجمالي الدفعات المؤقتة:** {format_currency(tot)}")
                    if current_role == 'مدير':
                        col_d1, col_d2 = st.columns(2)
                        with col_d1:
                            did = st.selectbox("حذف دفعة واحدة",
                                              [p['id'] for p in tp],
                                              format_func=lambda x: f"دفعة {x} - {format_currency(next((p['amount'] for p in tp if p['id']==x),0))}",
                                              key=f"del_single_{sel}")
                            if st.button("🗑️ حذف الدفعة", key=f"btn_del_single_{sel}"):
                                delete_temporary_payment(did); st.toast("تم الحذف", icon="🗑️"); st.rerun()
                        with col_d2:
                            if st.button("🗑️ حذف كل الدفعات المؤقتة", key=f"btn_del_all_{sel}"):
                                delete_all_temporary_payments(sel); st.toast("تم حذف كل الدفعات المؤقتة", icon="🗑️"); st.rerun()
                else:
                    st.info("لا دفعات مؤقتة على هذا العقد بعد")

elif menu == "التقارير":
    st.subheader("📈 التقارير")
    if not has_permission(current_user_id, "التقارير"): st.error("لا تملك صلاحية")
    else:
        rt = st.radio("نوع التقرير", ["كشف حساب مستأجر","دفعات بين تاريخين","الإيرادات","الضرائب","تقرير المستحقات"])
        cc = st.radio("نوع التاريخ", ["ميلادي","هجري"], horizontal=True)
        st.markdown("### 📄 خيارات الطباعة")
        orient_choice = st.radio("اتجاه الصفحة", ["عمودي (Portrait)", "أفقي (Landscape)"], horizontal=True, key="report_orientation")
        landscape_choice = (orient_choice == "أفقي (Landscape)")

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
                            hi = st.text_input("من هجري", "01-01-1445", key="kr_h1")
                            try: fd = hijri_to_gregorian(hi)
                            except: st.error("خطأ"); st.stop()
                        else: fd = st.date_input("من", value=date.today().replace(day=1), key="kr_d1")
                    with c2:
                        if cc == "هجري":
                            hi = st.text_input("إلى هجري", "30-12-1445", key="kr_h2")
                            try: td = hijri_to_gregorian(hi)
                            except: st.error("خطأ"); st.stop()
                        else: td = st.date_input("إلى", value=date.today(), key="kr_d2")
                    conn = get_conn(); cur = conn.cursor()
                    tn_row = cur.execute("SELECT name, region FROM tenants WHERE id=?", (tid,)).fetchone()
                    if tn_row:
                        tn, tr = tn_row
                        cr = cur.execute("SELECT c.contract_number FROM contracts c WHERE c.tenant_id=? AND c.status='نشط' LIMIT 1", (tid,)).fetchone()
                        cno = cr[0] if cr else "لا يوجد"
                        pays = cur.execute('''SELECT id, due_date, amount, paid_amount, (amount-paid_amount), status, paid_date, attachment
                            FROM payments WHERE tenant_id=? AND due_date BETWEEN ? AND ? ORDER BY due_date''',
                            (tid, fd.isoformat(), td.isoformat())).fetchall()
                        recs = cur.execute('''SELECT receipt_number, amount, receipt_date, payment_method, attachment
                            FROM receipts WHERE tenant_id=? AND receipt_date BETWEEN ? AND ? ORDER BY receipt_date DESC''',
                            (tid, fd.isoformat(), td.isoformat())).fetchall()
                        conn.close()
                        st.markdown(f"### كشف حساب: {tn}")
                        st.write(f"**المنطقة:** {tr or '-'} | **العقد:** {cno}")
                        st.write(f"**الفترة:** {fd} - {td}")
                        if pays:
                            dfp = pd.DataFrame(pays, columns=["رقم الدفعة","الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة","تاريخ السداد","المرفق"])
                            rtl_dataframe(dfp.drop(columns=["المرفق"]))
                            ta = sum(p[2] for p in pays); tp_ = sum(p[3] for p in pays)
                            st.write(f"**إجمالي المستحق:** {format_currency(ta)}")
                            st.write(f"**إجمالي المدفوع:** {format_currency(tp_)}")
                            st.write(f"**المتبقي:** {format_currency(ta - tp_)}")
                        else: st.info("لا دفعات")
                        if recs:
                            dfr = pd.DataFrame(recs, columns=["رقم السند","المبلغ","التاريخ","الطريقة","المرفق"])
                            rtl_dataframe(dfr.drop(columns=["المرفق"]))
                        if pays:
                            dfe = pd.DataFrame([(p[1],p[2],p[3],p[2]-p[3],p[5],p[6]) for p in pays],
                                               columns=["الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة","تاريخ السداد"])
                            o = io.BytesIO()
                            with pd.ExcelWriter(o, engine='xlsxwriter') as wr:
                                dfe.to_excel(wr, sheet_name='الدفعات', index=False)
                                if recs:
                                    pd.DataFrame([(r[0],r[1],r[2],r[3]) for r in recs],
                                                 columns=["رقم السند","المبلغ","التاريخ","الطريقة"]).to_excel(wr, sheet_name='سندات', index=False)
                            st.download_button("تحميل Excel", data=o.getvalue(), file_name=f"kashf_{tn}.xlsx", key=f"dl_kashf_{tid}")
                            ei = f"المنطقة: {tr or '-'} - رقم العقد: {cno}"
                            export_df_to_pdf(dfe, f"كشف حساب {tn}", f"kashf_{tn}.pdf", extra_info=ei, landscape_mode=landscape_choice)
                    else:
                        conn.close(); st.warning("المستأجر لم يعد موجود")
        elif rt == "دفعات بين تاريخين":
            st.markdown("### تقرير الدفعات بين تاريخين")
            if cc == "هجري":
                c1, c2 = st.columns(2)
                hi1 = c1.text_input("من هجري", "01-01-1445", key="dd_h1")
                hi2 = c2.text_input("إلى هجري", "30-12-1445", key="dd_h2")
                try: fd = hijri_to_gregorian(hi1); td = hijri_to_gregorian(hi2)
                except: st.error("خطأ"); st.stop()
            else:
                c1, c2 = st.columns(2)
                fd = c1.date_input("من", value=date.today().replace(day=1), key="dd_d1")
                td = c2.date_input("إلى", value=date.today(), key="dd_d2")
            only_dues = st.checkbox("💵 عرض المستحقات فقط", value=False, key="only_dues_chk")
            tf = st.selectbox("مستأجر", ["الكل"] + load_tenants()["الاسم"].tolist(), key="dd_tf")
            rf = st.selectbox("المنطقة", ["الكل"] + load_tenants()["المنطقة"].dropna().unique().tolist(), key="dd_rf")
            conn = get_conn(); cur = conn.cursor()
            q = '''SELECT t.name, p.name, pay.due_date, pay.amount, pay.paid_amount, (pay.amount-pay.paid_amount) as remaining,
                   pay.status, t.region FROM payments pay JOIN tenants t ON pay.tenant_id=t.id
                   JOIN contracts c ON pay.contract_id=c.id JOIN properties p ON c.property_id=p.id
                   WHERE pay.due_date BETWEEN ? AND ?'''
            pr = [fd.isoformat(), td.isoformat()]
            if only_dues: q += " AND (pay.amount - pay.paid_amount) > 0"
            if tf != "الكل": q += " AND t.name=?"; pr.append(tf)
            if rf != "الكل": q += " AND t.region=?"; pr.append(rf)
            q += " ORDER BY pay.due_date"
            cur.execute(q, pr); dues = cur.fetchall(); conn.close()
            if dues:
                df = pd.DataFrame(dues, columns=["المستأجر","العقار","الاستحقاق","المبلغ","المدفوع","المتبقي","الحالة","المنطقة"])
                display_dataframe_with_reorder(df.copy(), "rp")
                ta = sum(d[3] for d in dues); tp_ = sum(d[4] for d in dues); tr_ = sum(d[5] for d in dues)
                c1, c2, c3 = st.columns(3)
                c1.metric("إجمالي المستحق", format_currency(ta))
                c2.metric("إجمالي المدفوع", format_currency(tp_))
                c3.metric("إجمالي المتبقي", format_currency(tr_))
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr: df.to_excel(wr, index=False)
                st.download_button("تحميل Excel", data=o.getvalue(), file_name=f"dues_{fd}_{td}.xlsx", key="dl_dues")
                title_txt = "المستحقات" if only_dues else "الدفعات"
                export_df_to_pdf(df, f"{title_txt} من {fd} إلى {td}", f"dues_{fd}_{td}.pdf", landscape_mode=landscape_choice)
            else:
                st.info("لا مستحقات في هذه الفترة" if only_dues else "لا دفعات في هذه الفترة")
        elif rt == "الإيرادات":
            if cc == "هجري":
                c1, c2 = st.columns(2)
                hi1 = c1.text_input("من هجري", "01-01-1445", key="rev_h1"); hi2 = c2.text_input("إلى هجري", "30-12-1445", key="rev_h2")
                try: fd = hijri_to_gregorian(hi1); td = hijri_to_gregorian(hi2)
                except: st.error("خطأ"); st.stop()
            else:
                c1, c2 = st.columns(2)
                fd = c1.date_input("من", value=date.today().replace(day=1), key="rev_d1"); td = c2.date_input("إلى", value=date.today(), key="rev_d2")
            conn = get_conn()
            df = pd.read_sql_query('''SELECT r.receipt_date as 'التاريخ', t.name as 'المستأجر',
                r.receipt_number as 'رقم السند', r.amount as 'المبلغ', r.payment_method as 'طريقة السداد'
                FROM receipts r JOIN tenants t ON r.tenant_id=t.id
                WHERE r.receipt_date BETWEEN ? AND ? ORDER BY r.receipt_date''',
                conn, params=(fd.isoformat(), td.isoformat()))
            conn.close()
            if not df.empty:
                display_dataframe_with_reorder(df.copy(), "rev")
                st.write(f"**الإجمالي:** {format_currency(df['المبلغ'].sum())}")
                o = io.BytesIO()
                with pd.ExcelWriter(o, engine='xlsxwriter') as wr: df.to_excel(wr, index=False)
                st.download_button("Excel", data=o.getvalue(), file_name=f"rev_{fd}_{td}.xlsx", key="dl_rev")
                export_df_to_pdf(df, "الإيرادات", f"rev_{fd}_{td}.pdf", landscape_mode=landscape_choice)
            else: st.info("لا إيرادات")
        elif rt == "الضرائب":
            if cc == "هجري":
                c1, c2 = st.columns(2)
                hi1 = c1.text_input("من هجري", "01-01-1445", key="tx_h1"); hi2 = c2.text_input("إلى هجري", "30-12-1445", key="tx_h2")
                try: fd = hijri_to_gregorian(hi1); td = hijri_to_gregorian(hi2)
                except: st.error("خطأ"); st.stop()
            else:
                c1, c2 = st.columns(2)
                fd = c1.date_input("من", value=date.today().replace(day=1), key="tx_d1"); td = c2.date_input("إلى", value=date.today(), key="tx_d2")
            conn = get_conn()
            df = pd.read_sql_query('''SELECT t.name as 'اسم المستأجر', c.contract_number as 'رقم العقد',
                c.start_date as 'بداية الفترة', c.end_date as 'نهاية الفترة',
                pay.amount as 'المبلغ شامل الضريبة', c.tax_included as 'شامل الضريبة',
                c.tax_rate as 'نسبة الضريبة', r.payment_method as 'طريقة الدفع'
                FROM payments pay JOIN tenants t ON pay.tenant_id=t.id
                JOIN contracts c ON pay.contract_id=c.id LEFT JOIN receipts r ON r.payment_id=pay.id
                WHERE pay.status='مدفوع' AND pay.paid_date BETWEEN ? AND ? ORDER BY pay.paid_date''',
                conn, params=(fd.isoformat(), td.isoformat()))
            conn.close()
            if not df.empty:
                taxes = []
                for _, row in df.iterrows():
                    am = safe_float(row['المبلغ شامل الضريبة']); ti = int(row['شامل الضريبة']); tr = safe_float(row['نسبة الضريبة'])
                    if ti == 1: tax = am * (tr / (1 + tr)) if tr > 0 else 0
                    else: tax = am * tr
                    taxes.append(tax)
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
                export_tax_pdf(dfd, "تقرير الضرائب", f"tax_{fd}_{td}.pdf", columns_order=sc, landscape_mode=landscape_choice)
            else: st.info("لا بيانات")
        elif rt == "تقرير المستحقات":
            st.markdown("### 📋 تقرير المستحقات (مجمع لكل مستأجر)")
            st.caption("يعرض كل مستأجر مرة واحدة مع أقدم دفعة غير مسددة وعدد الدفعات المتأخرة وإجمالي المتبقي.")
            st.markdown("#### 📅 فترة التقرير")
            if cc == "هجري":
                c1, c2 = st.columns(2)
                hi1 = c1.text_input("من تاريخ هجري", "01-01-1445", key="due_h1")
                hi2 = c2.text_input("إلى تاريخ هجري", "30-12-1445", key="due_h2")
                try:
                    fd_due = hijri_to_gregorian(hi1); td_due = hijri_to_gregorian(hi2)
                except:
                    st.error("صيغة التاريخ الهجري غير صحيحة"); st.stop()
            else:
                c1, c2 = st.columns(2)
                fd_due = c1.date_input("من تاريخ", value=date.today().replace(day=1), key="due_d1")
                td_due = c2.date_input("إلى تاريخ", value=date.today() + relativedelta(years=1), key="due_d2")
            st.info(f"📆 الفترة المحددة: من **{fd_due}** إلى **{td_due}**")
            include_past_overdue = st.checkbox("☑️ تضمين الدفعات المتأخرة قبل بداية الفترة", value=True, key="due_include_past")
            all_tenants_df = load_tenants()
            regions_list = ["الكل"] + sorted([r for r in all_tenants_df["المنطقة"].dropna().unique().tolist() if r])
            rf_due = st.selectbox("المنطقة", regions_list, key="due_report_region")
            conn = get_conn()
            if include_past_overdue:
                q = '''SELECT t.id, t.name as tenant_name, t.region, MIN(pay.due_date) as oldest_due,
                       COUNT(*) as num_payments,
                       SUM(CASE WHEN pay.due_date < date('now') THEN 1 ELSE 0 END) as num_overdue,
                       SUM(pay.amount - pay.paid_amount) as total_remaining
                       FROM payments pay JOIN tenants t ON pay.tenant_id = t.id
                       WHERE (pay.amount - pay.paid_amount) > 0 AND pay.due_date <= ?'''
                params = [td_due.isoformat()]
            else:
                q = '''SELECT t.id, t.name as tenant_name, t.region, MIN(pay.due_date) as oldest_due,
                       COUNT(*) as num_payments,
                       SUM(CASE WHEN pay.due_date < date('now') THEN 1 ELSE 0 END) as num_overdue,
                       SUM(pay.amount - pay.paid_amount) as total_remaining
                       FROM payments pay JOIN tenants t ON pay.tenant_id = t.id
                       WHERE (pay.amount - pay.paid_amount) > 0 AND pay.due_date BETWEEN ? AND ?'''
                params = [fd_due.isoformat(), td_due.isoformat()]
            if rf_due != "الكل":
                q += " AND t.region = ?"; params.append(rf_due)
            q += " GROUP BY t.id, t.name, t.region ORDER BY oldest_due ASC, t.name"
            df_due = pd.read_sql_query(q, conn, params=params)
            conn.close()
            if not df_due.empty:
                df_due_display = df_due.rename(columns={
                    'tenant_name': 'المستأجر','region': 'المنطقة','oldest_due': 'أقدم دفعة غير مسددة',
                    'num_payments': 'عدد الدفعات المستحقة','num_overdue': 'عدد الدفعات المتأخرة',
                    'total_remaining': 'إجمالي المتبقي'
                })
                df_due_display = df_due_display[['المستأجر','المنطقة','أقدم دفعة غير مسددة','عدد الدفعات المستحقة','عدد الدفعات المتأخرة','إجمالي المتبقي']]
                df_selected, selected_cols = display_dataframe_with_reorder(df_due_display, "due_report_table")
                st.markdown("---")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("عدد المستأجرين المديونين", len(df_due))
                c2.metric("إجمالي الدفعات المستحقة", f"{int(df_due['num_payments'].sum())} دفعة")
                c3.metric("إجمالي الدفعات المتأخرة", f"{int(df_due['num_overdue'].sum())} دفعة")
                c4.metric("💵 إجمالي المبالغ المتبقية", format_currency(df_due['total_remaining'].sum()))
                st.markdown("---")
                st.markdown("#### 📤 تصدير التقرير")
                df_export = df_selected.copy()
                title_parts = [f"مستحقات سابقة حتى {td_due}"]
                if rf_due != "الكل": title_parts.append(f"المنطقة: {rf_due}")
                pdf_title = " - ".join(title_parts)
                c_exp1, c_exp2 = st.columns(2)
                with c_exp1:
                    o = io.BytesIO()
                    with pd.ExcelWriter(o, engine='xlsxwriter') as wr:
                        df_export.to_excel(wr, index=False, sheet_name='المستحقات')
                    st.download_button("📥 تحميل Excel", data=o.getvalue(), file_name=f"تقرير_المستحقات_{td_due}.xlsx", key="dl_due_report_xl")
                with c_exp2:
                    export_df_to_pdf(df_export, pdf_title, f"تقرير_المستحقات_{td_due}.pdf", landscape_mode=landscape_choice)
            else:
                st.success(f"✅ لا توجد مستحقات خلال الفترة من {fd_due} إلى {td_due}")

elif menu == "المستخدمون":
    st.subheader("👤 المستخدمون")
    if not has_permission(current_user_id, "المستخدمون"): st.error("لا تملك صلاحية")
    else:
        t1, t2 = st.tabs(["عرض","إضافة"])
        with t1:
            dfu = load_users()
            if not dfu.empty:
                rtl_dataframe(dfu)
                uid = st.selectbox("اختر مستخدم", dfu["الرقم"], format_func=lambda x: dfu[dfu["الرقم"]==x]["اسم المستخدم"].iloc[0], key="sel_user")
                if uid:
                    up = load_permissions(uid)
                    st.markdown("### الصلاحيات")
                    np_ = {}
                    for pg in PAGE_KEYS: np_[pg] = st.checkbox(pg, value=up.get(pg, False), key=f"perm_{uid}_{pg}")
                    if st.button("حفظ الصلاحيات", key=f"btn_save_perm_{uid}"):
                        save_permissions(uid, np_); st.toast("تم الحفظ", icon="✅"); st.rerun()
                    if st.button("حذف المستخدم", key=f"btn_del_user_{uid}"):
                        delete_user(uid); st.toast("تم الحذف", icon="🗑️"); st.rerun()
                    if current_role == 'مدير':
                        with st.expander("تغيير كلمة المرور"):
                            npwd = st.text_input("كلمة المرور الجديدة", type="password", key=f"np_{uid}")
                            if st.button("تعيين", key=f"sp_{uid}") and npwd.strip():
                                conn = get_conn(); cur = conn.cursor()
                                nh = hashlib.sha256(npwd.strip().encode()).hexdigest()
                                cur.execute("UPDATE users SET password_hash=? WHERE id=?", (nh, uid))
                                conn.commit(); conn.close()
                                st.toast("تم التحديث", icon="✅"); st.rerun()
        with t2:
            with st.form("add_u_f"):
                u = st.text_input("اسم المستخدم *"); p = st.text_input("كلمة المرور *", type="password")
                r = st.selectbox("الدور", ["مدير","محاسب","مشاهد"])
                if st.form_submit_button("إضافة"):
                    if u.strip() and p.strip():
                        ok, msg = add_user(u, p, r)
                        if ok: st.toast(msg, icon="✅"); st.rerun()
                        else: st.error(msg)
                    else: st.error("بيانات ناقصة")

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
                if lf: save_setting('logo', lf.read())
                st.toast("تم الحفظ", icon="✅"); st.rerun()
        st.markdown("---")
        st.subheader("📱 إعداد تيليجرام")
        with st.form("tg_f"):
            tk = st.text_input("Bot Token", value=telegram_bot_token, type="password")
            ch = st.text_input("Chat ID", value=telegram_chat_id)
            fi = st.text_input("File ID", value=telegram_file_id)
            if st.form_submit_button("حفظ"):
                save_setting('telegram_bot_token', tk); save_setting('telegram_chat_id', ch); save_setting('telegram_file_id', fi)
                st.toast("تم الحفظ", icon="✅"); st.rerun()

elif menu == "نسخ احتياطي":
    st.subheader("💾 النسخ الاحتياطي")
    if not has_permission(current_user_id, "نسخ احتياطي"): st.error("لا تملك صلاحية")
    else:
        st.info("""
        ℹ️ **حدود Telegram Bot API:**
        - الرفع: حتى **50 ميجابايت** | التحميل: حتى **20 ميجابايت**
        ✅ النظام يقوم **بضغط قاعدة البيانات تلقائياً** قبل الرفع.
        """)
        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### 📥 تنزيل نسخة محلية")
            try:
                compressed_data, orig_size, comp_size = create_compressed_backup()
                if compressed_data:
                    orig_mb = orig_size / (1024 * 1024); comp_mb = comp_size / (1024 * 1024)
                    ratio = (1 - comp_size / orig_size) * 100 if orig_size > 0 else 0
                    st.write(f"📊 **الأصلي:** {orig_mb:.2f} MB")
                    st.write(f"📦 **المضغوط:** {comp_mb:.2f} MB (توفير {ratio:.1f}%)")
                    st.download_button("⬇️ تحميل النسخة المضغوطة (.gz)", data=compressed_data,
                                      file_name=f"backup_{date.today()}.db.gz", mime="application/gzip", key="dl_backup_gz")
                    with open("rentals.db", "rb") as f: raw_data = f.read()
                    st.download_button("⬇️ تحميل النسخة الأصلية (.db)", data=raw_data,
                                      file_name=f"backup_{date.today()}.db", mime="application/octet-stream", key="dl_backup_raw")
                else: st.error("فشل إنشاء النسخة")
            except FileNotFoundError: st.warning("لا توجد قاعدة بيانات")
        with c2:
            st.markdown("### 📤 استعادة نسخة محلية")
            uf = st.file_uploader("اختر ملف النسخة", type=["db", "sqlite", "gz"], key="restore_up")
            if uf:
                file_bytes = uf.read(); file_name = uf.name.lower()
                is_gz = file_name.endswith(".gz") or (len(file_bytes) > 2 and file_bytes[:2] == b'\x1f\x8b')
                if is_gz:
                    try:
                        decompressed = gzip.decompress(file_bytes)
                        st.success(f"✅ ملف مضغوط - الحجم: {len(decompressed)/(1024*1024):.2f} MB")
                        if st.button("🔄 استعادة", key="btn_restore_gz"):
                            with open("rentals.db", "wb") as f: f.write(decompressed)
                            st.cache_data.clear(); st.toast("تمت الاستعادة", icon="✅")
                            time.sleep(1); st.rerun()
                    except Exception as e: st.error(f"فشل فك الضغط: {e}")
                else:
                    if st.button("🔄 استعادة", key="btn_restore_raw"):
                        with open("rentals.db", "wb") as f: f.write(file_bytes)
                        st.cache_data.clear(); st.toast("تمت الاستعادة", icon="✅")
                        time.sleep(1); st.rerun()
        st.markdown("---")
        st.subheader("📱 النسخ الاحتياطي عبر تيليجرام")
        if not telegram_bot_token or not telegram_chat_id:
            st.warning("⚠️ أدخل بيانات تيليجرام في الإعدادات أولاً")
        else:
            compressed_data, orig_size, comp_size = create_compressed_backup()
            if compressed_data:
                comp_mb = comp_size / (1024 * 1024); orig_mb = orig_size / (1024 * 1024)
                st.markdown(f"**حجم قاعدة البيانات الحالي:** {orig_mb:.2f} MB | **بعد الضغط:** {comp_mb:.2f} MB")
                if comp_mb <= 20: st.success(f"✅ الحجم أقل من 20 ميجا")
                elif comp_mb <= 50: st.warning(f"⚠️ الحجم بين 20 و 50 ميجا - سيتم تقسيم الملف")
                else: st.warning(f"⚠️ الحجم أكبر من 50 ميجا - سيتم تقسيم الملف")
            col_up, col_down = st.columns(2)
            with col_up:
                st.markdown("### ⬆️ رفع النسخة")
                if st.button("🚀 رفع النسخة المضغوطة", key="btn_tg_up", use_container_width=True):
                    with st.spinner("جاري الرفع..."):
                        try:
                            data, _, csize = create_compressed_backup()
                            if not data: st.error("فشل إنشاء النسخة")
                            else:
                                session_id = int(time.time())
                                if csize <= 50 * 1024 * 1024:
                                    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendDocument"
                                    files = {'document': (f"backup_{session_id}.db.gz", data, 'application/gzip')}
                                    d = {'chat_id': telegram_chat_id, 'caption': f"نسخة احتياطية {datetime.now():%Y-%m-%d %H:%M} - {csize/(1024*1024):.2f} MB"}
                                    resp = requests.post(url, files=files, data=d, timeout=300)
                                    if resp.status_code == 200:
                                        fid = resp.json().get('result', {}).get('document', {}).get('file_id')
                                        if fid:
                                            save_setting('telegram_file_id', fid); save_setting('telegram_session_id', str(session_id))
                                            save_setting('telegram_chunks', "1")
                                            st.success(f"✅ تم الرفع ({csize/(1024*1024):.2f} MB)")
                                            st.code(f"File ID: {fid}", language=None)
                                        else: st.error("فشل استخراج File ID")
                                    else: st.error(f"فشل الرفع: {resp.text[:200]}")
                                else:
                                    chunk_size = 15 * 1024 * 1024
                                    chunks = split_into_chunks(data, chunk_size)
                                    st.info(f"📦 تم تقسيم الملف إلى {len(chunks)} أجزاء")
                                    progress = st.progress(0); status_txt = st.empty(); file_ids = []
                                    for idx, chunk in enumerate(chunks, 1):
                                        status_txt.text(f"رفع الجزء {idx}/{len(chunks)}...")
                                        filename = f"backup_{session_id}_part{idx}of{len(chunks)}.bin.gz"
                                        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendDocument"
                                        files = {'document': (filename, chunk, 'application/gzip')}
                                        d = {'chat_id': telegram_chat_id, 'caption': f"الجزء {idx}/{len(chunks)}"}
                                        r = requests.post(url, files=files, data=d, timeout=300)
                                        if r.status_code == 200:
                                            fid = r.json().get('result', {}).get('document', {}).get('file_id')
                                            if fid: file_ids.append(fid)
                                        progress.progress(idx / len(chunks))
                                    if len(file_ids) == len(chunks):
                                        save_setting('telegram_file_id', ",".join(file_ids))
                                        save_setting('telegram_session_id', str(session_id))
                                        save_setting('telegram_chunks', str(len(chunks)))
                                        st.success(f"✅ تم رفع {len(chunks)} أجزاء")
                                    else: st.error(f"تم رفع {len(file_ids)} من {len(chunks)}")
                        except Exception as e: st.error(f"حدث خطأ: {str(e)}")
            with col_down:
                st.markdown("### ⬇️ استعادة من تيليجرام")
                saved_fid = settings.get('telegram_file_id', '')
                saved_chunks = settings.get('telegram_chunks', '1')
                if saved_fid:
                    with st.expander("معرّف الملف المحفوظ"):
                        st.code(saved_fid, language=None); st.text(f"عدد الأجزاء: {saved_chunks}")
                if st.button("🔄 استعادة النسخة", key="btn_tg_down", use_container_width=True):
                    if not saved_fid: st.error("لا يوجد File ID محفوظ")
                    else:
                        with st.spinner("جاري التحميل والاستعادة..."):
                            try:
                                file_ids = saved_fid.split(",")
                                if len(file_ids) == 1:
                                    data = download_file_from_telegram(telegram_bot_token, file_ids[0].strip())
                                    st.text(f"تم التحميل ({len(data)/(1024*1024):.2f} MB)")
                                    restore_from_compressed(data)
                                    st.success("✅ تمت الاستعادة"); time.sleep(1); st.rerun()
                                else:
                                    progress = st.progress(0); all_data = b""
                                    for i, fid in enumerate(file_ids, 1):
                                        st.text(f"تحميل الجزء {i}/{len(file_ids)}...")
                                        all_data += download_file_from_telegram(telegram_bot_token, fid.strip())
                                        progress.progress(i / len(file_ids))
                                    st.text(f"إجمالي: {len(all_data)/(1024*1024):.2f} MB - فك الضغط...")
                                    restore_from_compressed(all_data)
                                    st.success("✅ تمت الاستعادة"); time.sleep(1); st.rerun()
                            except Exception as e: st.error(f"فشل: {str(e)}")