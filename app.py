import io
import random
import string
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

import pandas as pd
import streamlit as st
from supabase import create_client, Client
import re

# =====================================================================
# CONFIG
# =====================================================================
st.set_page_config(page_title="TACO Procurement", layout="wide", page_icon="🏢")
BUCKET_NAME = "rfq-attachments"


@st.cache_resource
def get_client() -> Client:
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["service_role_key"]
    return create_client(url, key)


sb = get_client()


# =====================================================================
# AUTH
# =====================================================================
def login(email, password):
    try:
        # Pakai koneksi TERPISAH (bukan `sb` yang global) khusus buat cek password.
        # Ini supaya koneksi utama `sb` tetap punya akses penuh (service role)
        # dan gak ke-downgrade jadi identitas user biasa setelah proses sign-in.
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["service_role_key"]
        temp_client = create_client(url, key)
        auth_res = temp_client.auth.sign_in_with_password({"email": email, "password": password})
        uid = auth_res.user.id

        # Baca data profile pakai koneksi utama `sb` (tetap full akses)
        prof = sb.table("profiles").select("*").eq("id", uid).single().execute()
        return prof.data
    except Exception:
        return None


def register_user(name, email, password, role):
    """role: 'admin', 'proc', atau 'vendor'"""
    try:
        existing = sb.table("profiles").select("id").eq("email", email).execute()
        if existing.data:
            return False, "Email sudah terdaftar."
        created = sb.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        )
        uid = created.user.id
        sb.table("profiles").insert(
            {"id": uid, "email": email, "role": role, "vendor_name": name}
        ).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def bulk_register_users(df, role):
    """
    df harus punya kolom 'name' dan 'email' (case-insensitive).
    Password di-generate random per baris.
    Return: DataFrame hasil (name, email, password, status)
    """
    import random
    import string

    df.columns = [str(c).strip().lower() for c in df.columns]
    results = []
    for _, row in df.iterrows():
        name = str(row.get("name", "")).strip()
        email = str(row.get("email", "")).strip().lower()
        if not name or not email or "@" not in email:
            results.append({"name": name, "email": email, "password": "-", "status": "❌ Data tidak valid"})
            continue
        password = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        ok, err = register_user(name, email, password, role)
        if ok:
            results.append({"name": name, "email": email, "password": password, "status": "✅ Berhasil"})
        else:
            results.append({"name": name, "email": email, "password": "-", "status": f"❌ {err}"})
    return pd.DataFrame(results)


def get_users_by_role(role):
    res = sb.table("profiles").select("*").eq("role", role).execute()
    return pd.DataFrame(res.data)


def reset_user_password(user_id, new_password):
    try:
        sb.auth.admin.update_user_by_id(user_id, {"password": new_password})
        return True, None
    except Exception as e:
        return False, str(e)


def get_vendors():
    return get_users_by_role("vendor")


# =====================================================================
# PR & ITEMS
# =====================================================================
def get_or_create_pr(pr_code, location, priority, uploaded_by):
    existing = sb.table("purchase_requests").select("*").eq("pr_code", pr_code).execute()
    if existing.data:
        return existing.data[0]["id"]
    new_pr = sb.table("purchase_requests").insert(
        {
            "pr_code": pr_code,
            "location": location,
            "priority_status": priority,
            "uploaded_by": uploaded_by,
        }
    ).execute()
    return new_pr.data[0]["id"]

def clean_description(text):
    """
    Hanya menghapus kode angka bertitik di depan deskripsi.
    Contoh: '610.01.98 - OTHER WAREHOUSE...' -> 'OTHER WAREHOUSE...'
    """
    if not text or pd.isna(text):
        return "-"
    
    text_str = str(text).strip()
    
    # Menghapus pola angka bertitik di depan seperti '610.01.98 - ' atau '600.12 -'
    cleaned_text = re.sub(r"^\d+[\.\d]*\s*-\s*", "", text_str)
    
    return cleaned_text.strip() or text_str

def get_or_create_item(pr_id, description, description2, quantity, uom):
    q = (
        sb.table("pr_items")
        .select("*")
        .eq("pr_id", pr_id)
        .eq("description", description)
        .eq("description2", description2)
        .eq("quantity", quantity)
        .eq("uom", uom)
        .execute()
    )
    if q.data:
        return q.data[0]["id"]
    new_item = sb.table("pr_items").insert(
        {
            "pr_id": pr_id,
            "description": description,
            "description2": description2,
            "quantity": quantity,
            "uom": uom,
        }
    ).execute()
    return new_item.data[0]["id"]


def get_already_published_keys():
    """Set of (description, description2) yang sudah pernah di-assign ke vendor manapun."""
    res = sb.table("rfq_assignments").select("item_id, pr_items(description, description2)").execute()
    keys = set()
    for r in res.data:
        item = r.get("pr_items") or {}
        keys.add((str(item.get("description", "")).strip().lower(), str(item.get("description2", "")).strip().lower()))
    return keys


# =====================================================================
# PUBLISH RFQ
# =====================================================================
# =====================================================================
# PUBLISH RFQ (UPDATED)
# =====================================================================
def publish_rfq(rfq_title, pr_code, location, priority, admin_id, items_df, vendor_ids,
                delivery_type, pic_notes, deadline, files):
    pr_id = get_or_create_pr(pr_code, location, priority, admin_id)

    # Simpan/update rfq_title di PR
    sb.table("purchase_requests").update({"rfq_title": rfq_title}).eq("id", pr_id).execute()

    for f in files:
        file_bytes = f.getvalue()
        path = f"{pr_id}/{f.name}"
        try:
            sb.storage.from_(BUCKET_NAME).upload(
                path, file_bytes, {"content-type": f.type or "application/octet-stream", "upsert": "true"}
            )
            sb.table("rfq_attachments").insert(
                {"pr_id": pr_id, "file_name": f.name, "file_path": path}
            ).execute()
        except Exception as e:
            st.warning(f"Gagal upload file {f.name}: {e}")

    for _, row in items_df.iterrows():
        item_id = get_or_create_item(
            pr_id,
            str(row.get("DESCRIPTION", "")),
            str(row.get("DESCRIPTION_2", "")),
            row.get("QUANTITY", 0),
            str(row.get("UOM", "")),
        )
        for v_id in vendor_ids:
            sb.table("rfq_assignments").upsert(
                {
                    "item_id": item_id,
                    "vendor_id": v_id,
                    "delivery_type": delivery_type,
                    "pic_notes": pic_notes,
                    "line_note": str(row.get("CATATAN_BARIS_ATAU_LINK_GAMBAR", "-")),
                    "deadline": str(deadline),
                    "status": "Open",
                },
                on_conflict="item_id,vendor_id",
            ).execute()

    return pr_id


def get_price_comparison_data():
    res = (
        sb.table("quotes")
        .select("unit_price, brand, lead_time_days, ready_stock, rfq_assignments(pr_items(description, description2, quantity, uom, purchase_requests(pr_code)), profiles(vendor_name, email, top_days))")
        .execute()
    )
    rows = []
    for r in res.data:
        assign = r.get("rfq_assignments") or {}
        item = assign.get("pr_items") or {}
        pr = item.get("purchase_requests") or {}
        vendor = assign.get("profiles") or {}
        rows.append(
            {
                "pr_code": pr.get("pr_code"),
                "description": item.get("description"),
                "description2": item.get("description2"),
                "qty": item.get("quantity"),
                "uom": item.get("uom"),
                "vendor": vendor.get("vendor_name"),
                "unit_price": r.get("unit_price"),
                "brand": r.get("brand"),
                "lead_time_days": r.get("lead_time_days"),
                "ready_stock": r.get("ready_stock", "Tidak"),
                "top_days": vendor.get("top_days") or 0,
            }
        )
    return pd.DataFrame(rows)


def compute_recommendation(df_item, w_price, w_top, w_stock, w_leadtime):
    """
    df_item: baris-baris quote untuk SATU item (dari beberapa vendor).
    Mengembalikan df_item + kolom 'score' (0-100, makin tinggi makin direkomendasikan) + 'is_recommended'.
    Normalisasi per-item: harga makin murah makin baik, TOP makin panjang makin baik,
    ready stock 'Ya' dapat nilai penuh, lead time makin pendek makin baik.
    """
    d = df_item.copy()
    if d.empty:
        return d

    def norm_lower_better(s):
        s = pd.to_numeric(s, errors="coerce")
        if s.max() == s.min() or s.isna().all():
            return pd.Series([100.0] * len(s), index=s.index)
        return 100 * (s.max() - s) / (s.max() - s.min())

    def norm_higher_better(s):
        s = pd.to_numeric(s, errors="coerce")
        if s.max() == s.min() or s.isna().all():
            return pd.Series([100.0] * len(s), index=s.index)
        return 100 * (s - s.min()) / (s.max() - s.min())

    price_score = norm_lower_better(d["unit_price"])
    top_score = norm_higher_better(d["top_days"])
    leadtime_score = norm_lower_better(d["lead_time_days"])
    stock_score = d["ready_stock"].apply(lambda x: 100.0 if str(x).strip().lower() == "ya" else 0.0)

    total_w = max(w_price + w_top + w_stock + w_leadtime, 1)
    d["score"] = (
        price_score * w_price + top_score * w_top + stock_score * w_stock + leadtime_score * w_leadtime
    ) / total_w
    d["score"] = d["score"].round(1)
    d["is_recommended"] = d["score"] == d["score"].max()
    return d


def update_vendor_top(vendor_id, top_days):
    sb.table("profiles").update({"top_days": top_days}).eq("id", vendor_id).execute()


def get_history_data():
    res = (
        sb.table("rfq_assignments")
        .select("status, deadline, delivery_type, created_at, pr_items(description, description2, quantity, uom, purchase_requests(pr_code, location)), profiles(vendor_name, email)")
        .execute()
    )
    rows = []
    for r in res.data:
        item = r.get("pr_items") or {}
        pr = item.get("purchase_requests") or {}
        vendor = r.get("profiles") or {}
        rows.append(
            {
                "pr_code": pr.get("pr_code"),
                "location": pr.get("location"),
                "description": item.get("description"),
                "qty": item.get("quantity"),
                "uom": item.get("uom"),
                "vendor": vendor.get("vendor_name"),
                "vendor_email": vendor.get("email"),
                "status": r.get("status"),
                "deadline": r.get("deadline"),
                "created_at": r.get("created_at"),
            }
        )
    return pd.DataFrame(rows)


# =====================================================================
# VENDOR SIDE
# =====================================================================
def get_vendor_assignments(vendor_id):
    res = (
        sb.table("rfq_assignments")
        .select("*, pr_items(*, purchase_requests(*, profiles!purchase_requests_uploaded_by_fkey(vendor_name, email)))")
        .eq("vendor_id", vendor_id)
        .eq("status", "Open")
        .execute()
    )
    return res.data


def get_pr_attachments(pr_id):
    res = sb.table("rfq_attachments").select("*").eq("pr_id", pr_id).execute()
    return res.data


def submit_quote(assignment_id, vendor_id, unit_price, brand, lead_time_days, ready_stock):
    sb.table("quotes").insert(
        {
            "assignment_id": assignment_id,
            "vendor_id": vendor_id,
            "unit_price": unit_price,
            "brand": brand,
            "lead_time_days": lead_time_days,
            "ready_stock": ready_stock,
        }
    ).execute()


# =====================================================================
# EMAIL (UPDATED: JUDUL RFQ)
# =====================================================================
def send_rfq_email(vendor_email, vendor_name, rfq_title, deadline_str, items_text,
                   delivery_type, pic_notes, files):
    if "email_config" not in st.secrets:
        st.error("Konfigurasi 'email_config' tidak ditemukan di st.secrets")
        return False

    sender_email = st.secrets["email_config"].get("smtp_user", "")
    sender_password = st.secrets["email_config"].get("smtp_password", "")
    if not sender_password:
        st.error("'smtp_password' masih kosong di st.secrets")
        return False

    subject = f"Request for Quotation - TACO - {datetime.now().strftime('%d %b %Y')}"
    
    # FIX: "No. PR" diganti menjadi "Judul RFQ"
    body = (
        f"Dear {vendor_name},\n\n"
        f"Kami mengundang Anda untuk mengisi Request for Quotation (RFQ):\n\n"
        f"Judul RFQ: {rfq_title}\n"
        f"Batas Waktu Pengisian: {deadline_str}\n"
        f"Metode Pengiriman: {delivery_type}\n"
        f"Catatan Tambahan PIC: {pic_notes if pic_notes else '-'}\n\n"
        f"Daftar Item:\n{items_text}\n\n"
        f"Silakan login ke portal: https://taco-rfq.streamlit.app/\n\n"
        f"Salam,\nTACO Procurement Team"
    )

    try:
        msg = MIMEMultipart()
        msg["From"] = sender_email
        msg["To"] = vendor_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        for f in files:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.getvalue())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={f.name}")
            msg.attach(part)

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, vendor_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        st.warning(f"⚠️ Notifikasi email gagal terkirim ke {vendor_email}: {e}")
        return False


# =====================================================================
# AI EXECUTIVE INSIGHT (AUTOMATIC ANALYSIS)
# =====================================================================
def render_ai_insight(df_display, rfq_title):
    if "gemini" not in st.secrets or not st.secrets["gemini"].get("api_key"):
        st.caption("💡 Fitur AI belum aktif — tambahkan `gemini.api_key` di secrets.")
        return

    try:
        import google.generativeai as genai
    except ImportError:
        st.caption("⚠️ Library `google-generativeai` belum terinstall.")
        return

    api_key = st.secrets["gemini"]["api_key"].strip()
    genai.configure(api_key=api_key)

    st.markdown("### 🤖 AI Procurement Insight")
    
    insight_key = f"ai_insight_{rfq_title}"
    
    # Tombol untuk trigger atau re-generate analisis AI
    col_a, _ = st.columns([1, 3])
    btn_generate = col_a.button("✨ Analisis Ulang AI", key=f"btn_ai_{rfq_title}")

    # Jalankan jika belum ada hasil analisis atau jika tombol di-klik
    if btn_generate or insight_key not in st.session_state:
        context_table = df_display.to_csv(index=False)
        
        # SYSTEM PROMPT DIPAKSA DI BELAKANG LAYAR
        prompt = f"""Kamu adalah Senior Procurement Specialist & Cost Analyst untuk TACO Group.
Analisis data perbandingan penawaran vendor berikut untuk RFQ: {rfq_title}

DATA PERBANDINGAN:
{context_table}

Tugasmu adalah memberikan analisis otomatis tanpa perlu ditanya.
SUSUN HASIL ANALISIS DENGAN FORMAT MARKDOWN SEPERTI BERIKUT (WAJIB GUNAKAN HEADING & BULLET POINT KONSISTEN):

### ❓Penjelasan Produk:
(Berikan penjelasan SINGKAT dan PENTING mengenai spesifikasi dan merk produk, tipe, atau jenisnya, dan kegunaannya)

### 💡 Rekomendasi Merk Alternative:
(Berikan 2-3 opsi merk pengganti yang setara/lebih baik jika relevan dengan item dan spesifikasi di atas, cantumkan estimasi harga pasar & keunggulannya, atau rekomendasi vendor sesuai lokasi)

### ⚠️ Catatan Penting untuk Procurement:
(Sorot jika ada vendor yang harganya terindikasi jauh diatas harga pasar/overpriced/typo kuantitas, atau lead time terlalu lama)

### 🎯 Rekomendasi Action Plan PIC:
(Berikan langkah konkret 1, 2, 3 untuk PIC Procurement, misal: klarifikasi typo, negosiasi target harga, atau minta RFQ ulang merk alternatif. pertimbangkan juga jika barang tersebut dicatat urgent, maka pilih alternatif yang paling sesuai)

Jawab dengan tegas, profesional, berbasis angka konkret dari data di atas, serta actionable dalam Bahasa Indonesia.
"""

        with st.spinner("⚡ AI sedang menganalisis..."):
            try:
                active_models = [
                    m.name for m in genai.list_models()
                    if "generateContent" in m.supported_generation_methods
                ]
                
                candidate_models = []
                for target in ["flash", "pro"]:
                    candidate_models.extend([m for m in active_models if target in m])
                candidate_models.extend([m for m in active_models if m not in candidate_models])

                if not candidate_models:
                    candidate_models = ["models/gemini-1.5-flash", "models/gemini-2.0-flash"]

                answer = None
                for m_name in candidate_models:
                    try:
                        model = genai.GenerativeModel(m_name)
                        res = model.generate_content(prompt)
                        if res and res.text:
                            answer = res.text
                            break
                    except Exception:
                        continue

                if answer:
                    st.session_state[insight_key] = answer
                else:
                    st.warning("⏳ Kuota API sedang cooldown. Silakan klik tombol 'Analisis Ulang AI' dalam beberapa detik.")
                    return
            except Exception as e:
                st.error(f"Gagal memproses AI: {e}")
                return

    # TAMPILKAN HASILNYA SECARA OTOMATIS
    if insight_key in st.session_state:
        with st.container(border=True):
            st.markdown(st.session_state[insight_key])



def show_login():
    st.title("🛠️ TACO Sparepart RFQ")
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        with st.container(border=True):
            email_input = st.text_input("Email").strip().lower()
            password_input = st.text_input("Password", type="password")
            if st.button("Masuk", type="primary", use_container_width=True):
                profile = login(email_input, password_input)
                if profile:
                    st.session_state["user_info"] = profile
                    st.rerun()
                else:
                    st.error("Email atau Password salah.")


def render_pr_list(df_source, already_published):
    """Render list PR + checkbox item, dipakai buat tab Urgent & Normal."""
    if df_source.empty:
        st.info("Tidak ada item di kategori ini.")
        return

    for pr_no in df_source["PR CODE"].unique():
        df_group = df_source[df_source["PR CODE"] == pr_no].reset_index(drop=True)
        loc = df_group["LOCATION"].iloc[0] if "LOCATION" in df_group.columns else "-"
        prio = str(df_group["PRIORITY STATUS"].iloc[0]) if "PRIORITY STATUS" in df_group.columns else "-"
        label = f"📄 PR: {pr_no} | 📍 {loc}" + (" | 🚨 URGENT" if "URGENT" in prio.upper() else "")

        with st.expander(label, expanded=st.session_state.get("expand_all", False)):
            cA, cB, _ = st.columns([1, 1, 3])
            
            # Tombol Pilih Semua
            if cA.button("✅ Pilih Semua", key=f"all_{pr_no}"):
                for k in df_group["ROW_KEY"]:
                    st.session_state["selected_items_dict"][k] = True
                st.rerun()
                
            # Tombol Hapus Semua
            if cB.button("🗑️ Hapus Semua", key=f"none_{pr_no}"):
                for k in df_group["ROW_KEY"]:
                    st.session_state["selected_items_dict"][k] = False
                st.rerun()

            h1, h2, h3, h4, h5 = st.columns([0.5, 3, 3, 1, 1])
            h1.markdown("**✓**")
            h2.markdown("**Description**")
            h3.markdown("**Description 2**")
            h4.markdown("**Qty**")
            h5.markdown("**UOM**")

            for _, item_row in df_group.iterrows():
                row_key = item_row["ROW_KEY"]
                match_key = (str(item_row.get("DESCRIPTION", "")).strip().lower(), str(item_row.get("DESCRIPTION 2", "")).strip().lower())
                is_published = match_key in already_published
                bg = "#d1fae5" if is_published else "transparent"

                c1, c2, c3, c4, c5 = st.columns([0.5, 3, 3, 1, 1])
                with st.container():
                    st.markdown(f'<div style="background-color:{bg}; padding:4px; border-radius:4px;">', unsafe_allow_html=True)
                    
                    # 1. Ambil nilai status tercentang dari session state
                    is_checked = st.session_state["selected_items_dict"].get(row_key, False)
                    
                    # 2. FIX VISUAL BUG: Sertakan status {is_checked} di dalam key!
                    # Ini memaksa Streamlit me-refresh centang secara visual saat "Pilih Semua" diklik.
                    checked = c1.checkbox(
                        "sel", 
                        key=f"chk_{row_key}_{is_checked}",
                        value=is_checked,
                        label_visibility="collapsed",
                    )
                    
                    # 3. Update status baru ke session state
                    st.session_state["selected_items_dict"][row_key] = checked
                    
                    c2.write(item_row.get("DESCRIPTION", ""))
                    c3.write(item_row.get("DESCRIPTION 2", ""))
                    c4.write(item_row.get("QUANTITY", ""))
                    c5.write(item_row.get("UOM", ""))
                    st.markdown("</div>", unsafe_allow_html=True)


# =====================================================================
# UI: PROC - IMPORT PR LIST
# =====================================================================
def proc_portal_import():
    st.header("📥 Import & Publish Purchase Request")
    already_published = get_already_published_keys()

    uploaded_file = st.file_uploader("Upload File Excel", type=["xlsx"])
    if uploaded_file is None:
        st.session_state["selected_items_dict"] = {}
    else:
        try:
            df_raw = pd.read_excel(uploaded_file, header=2)
            df_raw.columns = [str(c).strip().upper() for c in df_raw.columns]
            if "PR CODE" not in df_raw.columns and "DESCRIPTION" not in df_raw.columns:
                uploaded_file.seek(0)
                df_raw = pd.read_excel(uploaded_file, header=0)
                df_raw.columns = [str(c).strip().upper() for c in df_raw.columns]
        except Exception as e:
            st.error(f"Gagal membaca file Excel: {e}")
            return

        df_raw = df_raw.reset_index(drop=True)
        df_raw["ROW_KEY"] = df_raw.index.astype(str)

        if "selected_items_dict" not in st.session_state:
            st.session_state["selected_items_dict"] = {}
        if "expand_all" not in st.session_state:
            st.session_state["expand_all"] = False

        df_display = df_raw.copy()
        if "STATUS" in df_raw.columns:
            df_display = df_display[df_display["STATUS"].astype(str).str.strip().str.upper() == "OPEN"]
        if "QUANTITY" in df_raw.columns:
            df_raw["QUANTITY"] = pd.to_numeric(df_raw["QUANTITY"], errors="coerce").fillna(0)
            df_display = df_display[pd.to_numeric(df_display["QUANTITY"], errors="coerce") > 0]

        if df_display.empty:
            st.warning("Tidak ada item berstatus 'Open' dengan Qty > 0 di file ini.")
        else:
            search_query = st.text_input("🔍 Cari No. PR atau Nama Item...")
            df_to_show = df_display.copy()
            if search_query:
                q = search_query.lower()
                mask = (
                    df_to_show.get("PR CODE", pd.Series()).astype(str).str.lower().str.contains(q, na=False)
                    | df_to_show.get("DESCRIPTION", pd.Series()).astype(str).str.lower().str.contains(q, na=False)
                )
                df_to_show = df_to_show[mask]

            col_exp, _ = st.columns([1, 4])
            if col_exp.button("📂 Collapse All" if st.session_state["expand_all"] else "📂 Expand All", use_container_width=True):
                st.session_state["expand_all"] = not st.session_state["expand_all"]
                st.rerun()

            sub_tab_urgent, sub_tab_normal = st.tabs(["🚨 Urgent Items", "📦 Normal Items"])

            if "PRIORITY STATUS" in df_to_show.columns:
                df_urgent = df_to_show[df_to_show["PRIORITY STATUS"].astype(str).str.upper().str.contains("URGENT", na=False)]
                df_normal = df_to_show[~df_to_show["PRIORITY STATUS"].astype(str).str.upper().str.contains("URGENT", na=False)]
            else:
                df_urgent = pd.DataFrame()
                df_normal = df_to_show.copy()

            with sub_tab_urgent:
                with st.container(height=400, border=True):
                    render_pr_list(df_urgent, already_published)

            with sub_tab_normal:
                with st.container(height=400, border=True):
                    render_pr_list(df_normal, already_published)

            st.divider()
            st.subheader("🎯 Review & Assign Vendor")
            selected_keys = [k for k, v in st.session_state["selected_items_dict"].items() if v]
            final_items = df_display[df_display["ROW_KEY"].isin(selected_keys)].copy()

            if final_items.empty:
                st.info("Belum ada item yang dipilih.")
            else:
                c_title, c_reset = st.columns([4, 1])
                with c_title:
                    rfq_title_val = st.text_input("🏷️ Judul RFQ (Wajib)", placeholder="Contoh: Pengadaan Sparepart Staples Batam").strip()
                with c_reset:
                    st.write(" ")
                    st.write(" ")
                    if st.button("🔄 Reset Pilihan", use_container_width=True):
                        st.session_state["selected_items_dict"] = {}
                        st.rerun()

                for col in ["PR CODE", "LOCATION", "DESCRIPTION", "DESCRIPTION 2", "QUANTITY", "UOM"]:
                    if col not in final_items.columns:
                        final_items[col] = "-"

                review_df = final_items[["PR CODE", "LOCATION", "DESCRIPTION", "DESCRIPTION 2", "QUANTITY", "UOM"]].copy()
                review_df.columns = ["PR_CODE", "LOCATION", "DESCRIPTION", "DESCRIPTION_2", "QUANTITY", "UOM"]
                review_df["CATATAN_BARIS_ATAU_LINK_GAMBAR"] = "-"

                edited = st.data_editor(review_df, hide_index=True, use_container_width=True,
                                         disabled=["PR_CODE", "LOCATION", "UOM"], key="admin_editor")

                attached_files = st.file_uploader(
                    "📁 Lampirkan file gambar/PDF referensi", accept_multiple_files=True,
                    type=["png", "jpg", "jpeg", "pdf"],
                )

                df_v = get_vendors()
                if df_v.empty:
                    st.warning("Belum ada vendor terdaftar.")
                else:
                    sel_v_names = st.multiselect("Pilih Vendor Penerima RFQ:", df_v["vendor_name"].unique())

                    c_left, c_right = st.columns(2)
                    with c_left:
                        rfq_deadline_val = st.date_input("📅 Batas Waktu Vendor:", value=datetime.today())
                        delivery_type_val = st.radio("🚚 Metode Pengiriman:", ["Franco (Kirim ke lokasi)", "Loco (Pengambilan sendiri)"])
                    with c_right:
                        pic_notes_val = st.text_area("📝 Catatan Tambahan Khusus Vendor:")

                    if st.button("🚀 Publish Undangan RFQ", type="primary", use_container_width=True):
                        if not rfq_title_val:
                            st.error("❌ Mohon isi 'Judul RFQ' terlebih dahulu!")
                        elif not sel_v_names:
                            st.error("❌ Silakan pilih minimal satu vendor.")
                        else:
                            vendor_ids = df_v[df_v["vendor_name"].isin(sel_v_names)]["id"].tolist()
                            pr_code_main = str(edited["PR_CODE"].iloc[0])
                            location_main = str(edited["LOCATION"].iloc[0])
                            priority_main = "-"
                            if "PRIORITY STATUS" in final_items.columns and not final_items["PRIORITY STATUS"].empty:
                                priority_main = str(final_items["PRIORITY STATUS"].iloc[0])

                            pr_id = publish_rfq(
                                rfq_title_val, pr_code_main, location_main, priority_main,
                                st.session_state["user_info"]["id"], edited, vendor_ids,
                                delivery_type_val, pic_notes_val, rfq_deadline_val, attached_files or [],
                            )

                            items_text_email = "\n".join(
                                f"- {r['DESCRIPTION']} {r['DESCRIPTION_2']} ({r['QUANTITY']} {r['UOM']}) [Note: {r['CATATAN_BARIS_ATAU_LINK_GAMBAR']}]"
                                for _, r in edited.iterrows()
                            )

                            with st.spinner("Mengirim notifikasi email ke vendor..."):
                                for v_name in sel_v_names:
                                    v_email = df_v[df_v["vendor_name"] == v_name]["email"].iloc[0]
                                    send_rfq_email(
                                        v_email, 
                                        v_name, 
                                        rfq_title_val,  # <- Kirim variabel Judul RFQ ke fungsi email
                                        rfq_deadline_val.strftime("%d %b %Y"), 
                                        items_text_email,
                                        delivery_type_val, 
                                        pic_notes_val, 
                                        attached_files or []
                                    )

                            st.success("🎉 Berhasil! RFQ tersimpan dan email terkirim ke vendor.")
                            st.session_state["selected_items_dict"] = {}
                            st.rerun()


# =====================================================================
# UI: PROC - MONITORING & COMPARISON (DENGAN DETAIL PAGE)
# =====================================================================
def proc_portal_comparison():
    res_pr = sb.table("purchase_requests").select("id, pr_code, location, priority_status, rfq_title").execute()
    df_pr = pd.DataFrame(res_pr.data) if res_pr.data else pd.DataFrame()

    active_id = st.session_state.get("active_compare_pr_id")

    # TAMPILAN HALAMAN DETAIL (Jika salah satu RFQ diklik)
    if active_id and not df_pr.empty and active_id in df_pr["id"].values:
        pr_info = df_pr[df_pr["id"] == active_id].iloc[0]
        rfq_title_active = pr_info.get("rfq_title") or pr_info["pr_code"]
        loc_active = pr_info.get("location") or "-"

        # Tombol Kembali di Pojok Atas
        if st.button("⬅️ Kembali ke Daftar RFQ"):
            st.session_state["active_compare_pr_id"] = None
            st.rerun()

        st.title(f"📊 {rfq_title_active}")
        st.caption(f"📍 Lokasi Pengiriman: **{loc_active}** | No. PR: **{pr_info['pr_code']}**")

        # Control Action: Mark as Submitted / Relive per RFQ
        st.divider()
        c_sub, c_relive, _ = st.columns([2, 2, 3])
        
        with c_sub:
            if st.button("🔒 Mark as Submitted (Selesai)", use_container_width=True, type="primary"):
                try:
                    # 1. Ambil list item_id milik RFQ/PR yang sedang aktif
                    items_res = sb.table("pr_items").select("id").eq("pr_id", active_id).execute()
                    item_ids = [i["id"] for i in items_res.data] if items_res.data else []
        
                    if item_ids:
                        # 2. Update status rfq_assignments dengan filter .in_()
                        sb.table("rfq_assignments").update({"status": "Submitted"}).in_("item_id", item_ids).execute()
                        st.success(f"🎉 RFQ '{rfq_title_active}' berhasil ditandai sebagai 'Submitted'!")
                        st.session_state["active_compare_pr_id"] = None
                        st.rerun()
                    else:
                        st.warning("Tidak ada item yang ditemukan untuk RFQ ini.")
                except Exception as e:
                    st.error(f"Gagal memperbarui status: {e}")
        
        with c_relive:
            if st.button("🔓 Relive / Re-open RFQ", use_container_width=True):
                try:
                    items_res = sb.table("pr_items").select("id").eq("pr_id", active_id).execute()
                    item_ids = [i["id"] for i in items_res.data] if items_res.data else []
        
                    if item_ids:
                        # Update status kembali ke Open dengan filter .in_()
                        sb.table("rfq_assignments").update({"status": "Open"}).in_("item_id", item_ids).execute()
                        st.success(f"🔓 RFQ '{rfq_title_active}' berhasil dibuka kembali!")
                        st.rerun()
                    else:
                        st.warning("Tidak ada item yang ditemukan untuk RFQ ini.")
                except Exception as e:
                    st.error(f"Gagal memperbarui status: {e}")

        st.markdown("---")
        st.subheader("📋 Matrix Perbandingan Penawaran Vendor")

        # Query all quotes for this active PR
        raw_q = sb.table("quotes").select("*, rfq_assignments(*, pr_items(*), profiles(*))").execute()
        data_matrix = []
        vendors_in_pr = set()

        for q in raw_q.data or []:
            ass = q.get("rfq_assignments") or {}
            item = ass.get("pr_items") or {}
            if item.get("pr_id") == active_id:
                v_name = (ass.get("profiles") or {}).get("vendor_name", "Unknown")
                vendors_in_pr.add(v_name)
                data_matrix.append({
                    "description": item.get("description"),
                    "spec": item.get("description2"),
                    "qty": item.get("quantity", 0),
                    "uom": item.get("uom"),
                    "vendor": v_name,
                    "price": q.get("unit_price", 0),
                    "total": q.get("unit_price", 0) * item.get("quantity", 0)
                })

        if not data_matrix:
            st.warning("Belum ada penawaran harga yang masuk dari vendor untuk RFQ ini.")
        else:
            df_m = pd.DataFrame(data_matrix)
            pivot_items = df_m[["description", "spec", "qty", "uom"]].drop_duplicates().reset_index(drop=True)

            for v in sorted(list(vendors_in_pr)):
                prices = []
                totals = []
                for _, r in pivot_items.iterrows():
                    match = df_m[(df_m["description"] == r["description"]) & (df_m["spec"] == r["spec"]) & (df_m["vendor"] == v)]
                    if not match.empty:
                        p = match["price"].values[0]
                        t = match["total"].values[0]
                        prices.append(f"Rp {p:,.0f}".replace(",", "."))
                        totals.append(f"Rp {t:,.0f}".replace(",", "."))
                    else:
                        prices.append("Rp 0")
                        totals.append("Rp 0")

                pivot_items[f"{v} - Price/Unit"] = prices
                pivot_items[f"{v} - Total Price"] = totals

            # Matrix Table Bersanding
            st.dataframe(pivot_items, hide_index=True, use_container_width=True)

            # Export Excel
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                pivot_items.to_excel(writer, index=False, sheet_name="CQR Matrix")
            st.download_button(
                "📥 Download CQR Comparison Matrix (Excel)",
                output.getvalue(),
                f"CQR_{rfq_title_active}.xlsx",
                use_container_width=True,
            )

            st.divider()
            # AI Executive Insight Otomatis
            render_ai_insight(pivot_items, rfq_title_active)

    # TAMPILAN HALAMAN UTAMA (LIST DAFTAR RFQ)
    else:
        st.header("📊 Monitoring & Price Comparison")

        if df_pr.empty:
            st.info("Belum ada data RFQ yang dipublish.")
        else:
            st.write("Pilih salah satu RFQ di bawah untuk membuka **Halaman Detail Perbandingan**:")
            st.markdown("---")

            quotes_res = sb.table("quotes").select("assignment_id").execute()
            submitted_ass_ids = set([q["assignment_id"] for q in quotes_res.data]) if quotes_res.data else set()

            for _, pr_row in df_pr.iterrows():
                pr_id = pr_row["id"]
                title = pr_row.get("rfq_title") or f"PR: {pr_row['pr_code']}"
                loc = pr_row.get("location") or "-"
                prio = str(pr_row.get("priority_status") or "")
                tag_prio = "🚨 URGENT" if "URGENT" in prio.upper() else "📦 NORMAL"

                with st.container(border=True):
                    c_info, c_btn = st.columns([4, 1])

                    with c_info:
                        st.subheader(f"📋 {title}")
                        st.caption(f"📍 **Lokasi:** {loc} | **Priority:** {tag_prio} | **PR Code:** {pr_row['pr_code']}")

                        # Tracking singkat vendor
                        # FIX: Tambahkan pr_items!inner(pr_id) di dalam .select()
                        # Query join relasi rfq_assignments & pr_items
                        res_ass = (
                            sb.table("rfq_assignments")
                            .select("*, profiles(vendor_name), pr_items!inner(pr_id)")
                            .eq("pr_items.pr_id", pr_id)
                            .execute()
                        )
                        
                        if res_ass.data:
                            # FIX: Pakai dictionary biar nama vendor tidak terduplikasi (kedobel)
                            vendor_status_map = {}
                            for ass in res_ass.data:
                                vn = (ass.get("profiles") or {}).get("vendor_name", "Vendor")
                                is_sub = ass["id"] in submitted_ass_ids
                                
                                # Jika vendor sudah pernah tercatat dan salah satu statusnya sudah submit, pertahankan status True
                                if vn in vendor_status_map:
                                    vendor_status_map[vn] = vendor_status_map[vn] or is_sub
                                else:
                                    vendor_status_map[vn] = is_sub
                        
                            # Format tampilan yang sudah unik (bebas terdobel)
                            v_display_list = [
                                f"{vn} {'✅' if is_sub else '⏳'}" 
                                for vn, is_sub in vendor_status_map.items()
                            ]
                            st.write("**Status Vendor:** " + " | ".join(v_display_list))

                    with c_btn:
                        st.write(" ")
                        # KLIK KE HALAMAN DETAIL BARU
                        if st.button("🔍 Buka Detail", key=f"open_detail_{pr_id}", type="primary", use_container_width=True):
                            st.session_state["active_compare_pr_id"] = pr_id
                            st.rerun()


# =====================================================================
# UI: PROC - HISTORY RFQ
# =====================================================================
def proc_portal_history():
    st.header("🔍 History RFQ")
    df_hist = get_history_data()
    if df_hist.empty:
        st.info("Belum ada riwayat publikasi.")
    else:
        st.dataframe(df_hist, hide_index=True, use_container_width=True)


# =====================================================================
# UI: ADMIN UTILS (REGISTRATION & ACCOUNT MANAGEMENT)
# =====================================================================
def admin_portal_register_pic():
    st.header("➕ Daftarkan PIC Procurement")
    sub1, sub2 = st.tabs(["Satu-satu", "Bulk (Excel/CSV)"])
    with sub1:
        with st.form("form_register_pic", clear_on_submit=True):
            p_name = st.text_input("Nama PIC").strip()
            p_email = st.text_input("Email PIC").strip().lower()
            submitted = st.form_submit_button("Simpan PIC Baru", type="primary")
            if submitted:
                if not p_name or not p_email or "@" not in p_email:
                    st.error("❌ Nama/Email tidak valid.")
                else:
                    auto_password = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                    ok, err = register_user(p_name, p_email, auto_password, "proc")
                    if ok:
                        st.success(f"🎉 PIC {p_name} berhasil didaftarkan.")
                        st.info(f"🔑 Password: `{auto_password}` — catat & kirim manual ke PIC ybs.")
                    else:
                        st.error(f"❌ Gagal: {err}")
    with sub2:
        st.write("Upload file Excel/CSV dengan 2 kolom: **name** dan **email**.")
        bulk_file = st.file_uploader("Upload file", type=["xlsx", "csv"], key="bulk_pic")
        if bulk_file is not None:
            df_bulk = pd.read_csv(bulk_file) if bulk_file.name.endswith(".csv") else pd.read_excel(bulk_file)
            st.dataframe(df_bulk, use_container_width=True, hide_index=True)
            if st.button("🚀 Daftarkan Semua PIC Ini", type="primary"):
                result_df = bulk_register_users(df_bulk, "proc")
                st.success("Selesai! Cek hasil & password di bawah.")
                st.dataframe(result_df, use_container_width=True, hide_index=True)


def admin_portal_register_vendor():
    st.header("➕ Daftarkan Vendor")
    sub1, sub2 = st.tabs(["Satu-satu", "Bulk (Excel/CSV)"])
    with sub1:
        with st.form("form_register_vendor", clear_on_submit=True):
            v_name = st.text_input("Nama Vendor").strip()
            v_email = st.text_input("Email Vendor").strip().lower()
            submitted = st.form_submit_button("Simpan Vendor Baru", type="primary")
            if submitted:
                if not v_name or not v_email or "@" not in v_email:
                    st.error("❌ Nama/Email tidak valid.")
                else:
                    auto_password = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                    ok, err = register_user(v_name, v_email, auto_password, "vendor")
                    if ok:
                        st.success(f"🎉 Vendor {v_name} berhasil didaftarkan.")
                        st.info(f"🔑 Password: `{auto_password}` — catat & kirim manual ke vendor ybs.")
                    else:
                        st.error(f"❌ Gagal: {err}")
    with sub2:
        st.write("Upload file Excel/CSV dengan 2 kolom: **name** dan **email**.")
        bulk_file = st.file_uploader("Upload file", type=["xlsx", "csv"], key="bulk_vendor")
        if bulk_file is not None:
            df_bulk = pd.read_csv(bulk_file) if bulk_file.name.endswith(".csv") else pd.read_excel(bulk_file)
            st.dataframe(df_bulk, use_container_width=True, hide_index=True)
            if st.button("🚀 Daftarkan Semua Vendor Ini", type="primary"):
                result_df = bulk_register_users(df_bulk, "vendor")
                st.success("Selesai! Cek hasil & password di bawah.")
                st.dataframe(result_df, use_container_width=True, hide_index=True)


def admin_portal_user_list():
    st.header("👥 Daftar Semua User")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**PIC Procurement**")
        st.dataframe(get_users_by_role("proc")[["email", "vendor_name", "created_at"]] if not get_users_by_role("proc").empty else pd.DataFrame(), hide_index=True, use_container_width=True)
    with c2:
        st.markdown("**Vendor**")
        df_v = get_vendors()
        st.dataframe(df_v[["email", "vendor_name", "created_at"]] if not df_v.empty else pd.DataFrame(), hide_index=True, use_container_width=True)


def admin_portal_reset_password():
    st.header("🔑 Reset Password User")
    df_proc = get_users_by_role("proc")
    df_vend = get_vendors()
    df_all = pd.concat([df_proc, df_vend], ignore_index=True) if not df_proc.empty or not df_vend.empty else pd.DataFrame()

    if df_all.empty:
        st.info("Belum ada user terdaftar.")
    else:
        df_all["label"] = df_all["vendor_name"].fillna("-") + " (" + df_all["email"] + ") — " + df_all["role"]
        sel_label = st.selectbox("Pilih User:", df_all["label"])
        sel_row = df_all[df_all["label"] == sel_label].iloc[0]

        mode = st.radio("Password baru:", ["Generate otomatis (random)", "Ketik manual"])
        new_pw = st.text_input("Password baru (min. 6 karakter):", type="password") if mode == "Ketik manual" else None

        if st.button("🔄 Reset Password", type="primary"):
            final_pw = new_pw if mode == "Ketik manual" else "".join(random.choices(string.ascii_letters + string.digits, k=10))
            if mode == "Ketik manual" and (not new_pw or len(new_pw) < 6):
                st.error("❌ Password minimal 6 karakter.")
            else:
                ok, err = reset_user_password(sel_row["id"], final_pw)
                if ok:
                    st.success(f"🎉 Password untuk **{sel_row['email']}** berhasil direset.")
                    st.info(f"🔑 Password baru: `{final_pw}`")
                else:
                    st.error(f"❌ Gagal: {err}")


# =====================================================================
# UI: VENDOR PORTAL (NAMA PIC & FORMAT HARGA TITIK)
# =====================================================================
def vendor_portal(vendor_id):
    if "vendor_page" not in st.session_state:
        st.session_state["vendor_page"] = "List RFQ Aktif"

    # Sidebar Navigation
    st.sidebar.markdown("## 🧭 Navigasi Vendor")
    st.sidebar.markdown("---")

    v_menus = [
        ("⚙️ Data Supplier", "v_supplier"),
        ("📋 List RFQ Aktif", "v_rfq"),
        ("🔍 History RFQ", "v_history"),
    ]

    for label, v_id in v_menus:
        is_active = (st.session_state["vendor_page"] == label.split(" ", 1)[1])
        btn_type = "primary" if is_active else "secondary"
        if st.sidebar.button(label, key=f"btn_vmenu_{v_id}", type=btn_type, use_container_width=True):
            st.session_state["vendor_page"] = label.split(" ", 1)[1]
            st.session_state["active_vendor_rfq_id"] = None
            st.rerun()

    st.sidebar.markdown("---")

    selected_v_page = st.session_state["vendor_page"]

    # -----------------------------------------------------------------
    # MENU 1: DATA SUPPLIER
    # -----------------------------------------------------------------
    if selected_v_page == "Data Supplier":
        st.header("⚙️ Data Supplier & Profil Vendor")
        prof = sb.table("profiles").select("*").eq("id", vendor_id).single().execute()
        p_data = prof.data or {}

        with st.container(border=True):
            st.markdown(f"**Nama Perusahaan/Vendor:** {p_data.get('vendor_name', '-')}")
            st.markdown(f"**Email Terdaftar:** {p_data.get('email', '-')}")
            
            st.divider()
            current_top = p_data.get("top_days") or 0
            new_top = st.number_input("TOP / Term of Payment Standard (Hari)", min_value=0, value=int(current_top), step=1)
            if st.button("Simpan Data TOP", type="primary"):
                update_vendor_top(vendor_id, new_top)
                st.success("Term of Payment berhasil diperbarui!")
                st.rerun()

    # -----------------------------------------------------------------
    # MENU 2: LIST RFQ AKTIF
    # -----------------------------------------------------------------
    elif selected_v_page == "List RFQ Aktif":
        assignments = get_vendor_assignments(vendor_id)
        
        if not assignments:
            st.info("Belum ada undangan RFQ aktif untuk Anda saat ini.")
            return

        # Grouping RFQ + Ambil Nama PIC Pengirim
        pr_groups = {}
        for a in assignments:
            item = a.get("pr_items") or {}
            pr = item.get("purchase_requests") or {}
            
            # Ambil nama PIC pengirim dari relasi profiles
            pic_profile = pr.get("profiles") or {}
            pic_name = pic_profile.get("vendor_name") or pic_profile.get("email") or "Procurement Team"

            rfq_title = pr.get("rfq_title") or f"PR: {pr.get('pr_code', '-')}"
            
            pr_groups.setdefault(pr.get("id"), {
                "title": rfq_title,
                "pr_code": pr.get("pr_code", "-"),
                "location": pr.get("location", "-"),
                "prio": str(pr.get("priority_status", "")),
                "pic_name": pic_name,
                "rows": []
            })
            pr_groups[pr.get("id")]["rows"].append(a)

        active_rfq_id = st.session_state.get("active_vendor_rfq_id")

        # TAMPILAN DETAIL PENAWARAN (Jika Tombol 'Buka Detail' Diklik)
        if active_rfq_id and active_rfq_id in pr_groups:
            group = pr_groups[active_rfq_id]
            
            if st.button("⬅️ Kembali ke Daftar RFQ Aktif"):
                st.session_state["active_vendor_rfq_id"] = None
                st.rerun()

            st.title(f"📝 Penawaran Harga: {group['title']}")
            # INFO PIC DITAMBAHKAN DISINI
            st.caption(f"👤 **PIC Procurement:** {group['pic_name']} | 📍 **Lokasi:** {group['location']} | **No. PR:** {group['pr_code']}")
            st.divider()

            # Attachment File
            attachments = get_pr_attachments(active_rfq_id)
            if attachments:
                st.markdown("**📎 File Referensi Lampiran:** " + ", ".join(f"`{a['file_name']}`" for a in attachments))

            # Data Preview Items
            table_rows = []
            for a in group["rows"]:
                item = a.get("pr_items") or {}
                raw_desc = item.get("description", "-")
                clean_desc = clean_description(raw_desc)

                table_rows.append({
                    "assignment_id": a["id"],
                    "Deskripsi": clean_desc,
                    "Spesifikasi": item.get("description2", "-"),
                    "Qty": item.get("quantity", 0),
                    "UOM": item.get("uom", "-"),
                    "Unit_Price": 0,
                    "Brand": "-",
                    "Ready_Stock": "Ya",
                    "Lead_Time_Days": 7,
                })

            df_preview = pd.DataFrame(table_rows)

            st.markdown("##### ✏️ Masukkan Harga & Detail Penawaran:")
            
            # FORMAT HARGA XXX.XXX BER-TITIK DI DATA EDITOR
            edited = st.data_editor(
                df_preview.drop(columns=["assignment_id"]),
                key=f"editor_v_{active_rfq_id}",
                hide_index=True,
                use_container_width=True,
                disabled=["Deskripsi", "Spesifikasi", "Qty", "UOM"],
                column_config={
                    "Unit_Price": st.column_config.NumberColumn(
                        "Unit Price (IDR)",
                        format="Rp %,d",  # Titik ribuan otomatis (Rp 1.000.000)
                        min_value=0,
                        step=1000,
                    ),
                    "Ready_Stock": st.column_config.SelectboxColumn("Ready Stock", options=["Ya", "Tidak"], required=True),
                    "Lead_Time_Days": st.column_config.NumberColumn("Lead Time (Hari)", min_value=1, step=1),
                },
            )

            if st.button("🚀 Kirim Penawaran", type="primary", use_container_width=True):
                for idx, r in edited.iterrows():
                    ass_id = df_preview.iloc[idx]["assignment_id"]
                    submit_quote(
                        ass_id, 
                        vendor_id, 
                        r["Unit_Price"], 
                        r["Brand"], 
                        r["Lead_Time_Days"], 
                        r["Ready_Stock"]
                    )
                st.success(f"🎉 Penawaran untuk '{group['title']}' berhasil terkirim ke PIC {group['pic_name']}!")
                st.session_state["active_vendor_rfq_id"] = None
                st.rerun()

        # TAMPILAN CARD LIST RFQ
        else:
            st.header("📋 List RFQ Aktif")
            st.write("Klik **Buka Detail** untuk mengisi penawaran harga:")
            st.markdown("---")

            for pr_id, group in pr_groups.items():
                prio_tag = "🚨 URGENT" if "URGENT" in group["prio"].upper() else "📦 NORMAL"
                
                with st.container(border=True):
                    c_info, c_btn = st.columns([4, 1])

                    with c_info:
                        st.subheader(f"📋 {group['title']}")
                        # INFO PIC DITAMBAHKAN PADA CARD LIST
                        st.caption(f"👤 **PIC Procurement:** {group['pic_name']} | 📍 **Lokasi:** {group['location']} | **Priority:** {prio_tag} | **PR Code:** {group['pr_code']}")
                    
                    with c_btn:
                        st.write(" ")
                        if st.button("🔍 Buka Detail", key=f"v_detail_{pr_id}", type="primary", use_container_width=True):
                            st.session_state["active_vendor_rfq_id"] = pr_id
                            st.rerun()

    # -----------------------------------------------------------------
    # MENU 3: HISTORY RFQ
    # -----------------------------------------------------------------
    elif selected_v_page == "History RFQ":
        st.header("🔍 History Penawaran Saya")
        res_hist = (
            sb.table("quotes")
            .select("unit_price, brand, ready_stock, lead_time_days, created_at, rfq_assignments(status, pr_items(description, description2, quantity, uom, purchase_requests(rfq_title, pr_code, profiles(vendor_name)))))")
            .eq("vendor_id", vendor_id)
            .execute()
        )
        
        if not res_hist.data:
            st.info("Belum ada riwayat penawaran terkirim.")
        else:
            rows_h = []
            for q in res_hist.data:
                ass = q.get("rfq_assignments") or {}
                item = ass.get("pr_items") or {}
                pr = item.get("purchase_requests") or {}
                pic_p = pr.get("profiles") or {}
                pic_name = pic_p.get("vendor_name") or "-"

                # FORMAT HARGA XXX.XXX BER-TITIK DI HISTORY
                formatted_price = f"Rp {q.get('unit_price', 0):,.0f}".replace(",", ".")

                rows_h.append({
                    "Judul RFQ": pr.get("rfq_title") or pr.get("pr_code"),
                    "PIC Procurement": pic_name,
                    "Deskripsi": clean_description(item.get("description")),
                    "Qty": item.get("quantity"),
                    "UOM": item.get("uom"),
                    "Harga Unit": formatted_price,
                    "Brand": q.get("brand"),
                    "Ready Stock": q.get("ready_stock"),
                    "Lead Time": f"{q.get('lead_time_days')} hari",
                    "Tanggal Submit": q.get("created_at")[:10] if q.get("created_at") else "-",
                })
            st.dataframe(pd.DataFrame(rows_h), hide_index=True, use_container_width=True)


# =====================================================================
# COMBINED ADMIN PORTAL (CUSTOM BUTTON SIDEBAR UI)
# =====================================================================
def combined_admin_portal():
    if "current_page" not in st.session_state:
        st.session_state["current_page"] = "📥 Import PR List"

    # UI Custom Sidebar Navigation
    st.sidebar.markdown("## 🧭 Navigasi Menu")
    st.sidebar.markdown("---")

    menu_options = [
        ("📥 Import PR List", "proc_import"),
        ("📊 Price Comparison", "proc_compare"),
        ("🔍 History RFQ", "proc_history"),
        ("➕ Daftarkan PIC", "admin_pic"),
        ("➕ Daftarkan Vendor", "admin_vendor"),
        ("👥 Daftar User", "admin_users"),
        ("🔑 Reset Password", "admin_reset"),
    ]

    # Render Setiap Menu Sebagai Tombol Custom
    for label, page_id in menu_options:
        is_active = (st.session_state["current_page"] == label)
        
        # Tombol diberi tipe 'primary' jika halaman sedang aktif biar kelihatan beda & cakep
        btn_type = "primary" if is_active else "secondary"
        
        if st.sidebar.button(label, key=f"nav_btn_{page_id}", type=btn_type, use_container_width=True):
            st.session_state["current_page"] = label
            st.rerun()

    st.sidebar.markdown("---")

    # Routing Halaman berdasarkan Pilihan
    selected_page = st.session_state["current_page"]

    if selected_page == "📥 Import PR List":
        proc_portal_import()
    elif selected_page == "📊 Price Comparison":
        proc_portal_comparison()
    elif selected_page == "🔍 History RFQ":
        proc_portal_history()
    elif selected_page == "➕ Daftarkan PIC":
        admin_portal_register_pic()
    elif selected_page == "➕ Daftarkan Vendor":
        admin_portal_register_vendor()
    elif selected_page == "👥 Daftar User":
        admin_portal_user_list()
    elif selected_page == "🔑 Reset Password":
        admin_portal_reset_password()


def main():
    # Keep Alive Session State Login
    if "user_info" not in st.session_state:
        st.session_state["user_info"] = None
    if "selected_items_dict" not in st.session_state:
        st.session_state["selected_items_dict"] = {}

    if st.session_state["user_info"] is None:
        show_login()
        return

    user = st.session_state["user_info"]
    
    # Header Profil di Sidebar
    st.sidebar.markdown(f"### 👋 Hi, **{user.get('vendor_name') or user.get('email')}**")
    st.sidebar.caption(f"Role: `{user.get('role', '').upper()}`")
    
    if st.sidebar.button("🚪 Log Out", use_container_width=True):
        st.session_state["user_info"] = None
        st.session_state["selected_items_dict"] = {}
        st.session_state["active_compare_pr_id"] = None
        st.session_state["active_vendor_rfq_id"] = None
        st.rerun()
        
    st.sidebar.markdown("---")

    if user["role"] in ["admin", "proc"]:
        combined_admin_portal()
    else:
        vendor_portal(user["id"])


if __name__ == "__main__":
    main()
