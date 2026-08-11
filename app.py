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
from datetime import datetime, timedelta

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


def mark_pic_viewed(pr_id):
    """Tandai RFQ sudah dibuka oleh PIC (sekali saja, kalau masih null)."""
    try:
        sb.table("purchase_requests").update(
            {"pic_viewed_at": datetime.now().isoformat()}
        ).eq("id", pr_id).is_("pic_viewed_at", "null").execute()
    except Exception:
        pass


def mark_vendor_viewed(assignment_ids):
    """Tandai assignment-assignment RFQ sudah dibuka vendor (sekali saja, kalau masih null)."""
    if not assignment_ids:
        return
    try:
        sb.table("rfq_assignments").update(
            {"vendor_viewed_at": datetime.now().isoformat()}
        ).in_("id", assignment_ids).is_("vendor_viewed_at", "null").execute()
    except Exception:
        pass


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
        .select("*, quotes(*), pr_items(*, purchase_requests(*, profiles(vendor_name, email)))")
        .eq("vendor_id", vendor_id)
        .eq("status", "Open")
        .execute()
    )
    return res.data


def get_pr_attachments(pr_id):
    res = sb.table("rfq_attachments").select("*").eq("pr_id", pr_id).execute()
    return res.data


def upload_vendor_document(pr_id, vendor_id, file):
    try:
        file_bytes = file.getvalue()
        path = f"{pr_id}/vendor_docs/{vendor_id}/{file.name}"
        sb.storage.from_(BUCKET_NAME).upload(
            path, file_bytes, {"content-type": file.type or "application/pdf", "upsert": "true"}
        )
        sb.table("vendor_quote_documents").insert(
            {"pr_id": pr_id, "vendor_id": vendor_id, "file_name": file.name, "file_path": path}
        ).execute()
        return True
    except Exception as e:
        st.warning(f"Gagal upload dokumen: {e}")
        return False


def get_vendor_documents(pr_id, vendor_id=None):
    q = sb.table("vendor_quote_documents").select("*").eq("pr_id", pr_id)
    if vendor_id:
        q = q.eq("vendor_id", vendor_id)
    res = q.execute()
    return res.data


def submit_quote(assignment_id, vendor_id, unit_price, brand, lead_time_days, ready_stock, warranty="-", spec_vendor="-"):
    try:
        # Cek apakah sudah ada quote sebelumnya untuk assignment ini
        existing = sb.table("quotes").select("id").eq("assignment_id", assignment_id).eq("vendor_id", vendor_id).execute()

        payload = {
            "unit_price": unit_price,
            "brand": brand,
            "lead_time_days": lead_time_days,
            "ready_stock": ready_stock,
            "warranty": warranty,
            "spec_vendor": spec_vendor,
        }

        if existing.data:
            quote_id = existing.data[0]["id"]
            sb.table("quotes").update(payload).eq("id", quote_id).execute()
        else:
            payload["assignment_id"] = assignment_id
            payload["vendor_id"] = vendor_id
            sb.table("quotes").insert(payload).execute()
        return True, None
    except Exception as e:
        return False, str(e)


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
# AI EXECUTIVE INSIGHT + CHAT PROMPT MANUAL
# =====================================================================
def render_ai_insight(df_display, rfq_title, weights=None, cost_saving=None, saving_pct=None, recommended_total=None):
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

    st.markdown("---")
    st.markdown("### 🤖 Executive AI Procurement Insight & Assistant")
    
    insight_key = f"ai_insight_{rfq_title}"
    history_key = f"ai_history_{rfq_title}"
    
    if history_key not in st.session_state:
        st.session_state[history_key] = []

    weights_text = ", ".join(f"{k}: {v}%" for k, v in (weights or {}).items())
    saving_text = (
        f"Potensi cost saving dengan bobot ini: Rp {cost_saving:,.0f} ({saving_pct:.1f}% dari skenario termahal). "
        f"Total estimasi belanja sesuai rekomendasi: Rp {recommended_total:,.0f}."
        if cost_saving is not None else "Data cost saving tidak tersedia."
    )

    # 1. GENERATE OTOMATIS SAAT HALAMAN DIBUKA
    if insight_key not in st.session_state:
        context_table = df_display.to_csv(index=False)
        
        # SYSTEM PROMPT DIPAKSA DI BELAKANG LAYAR
        prompt = f"""Kamu adalah Procurement Specialist & Cost Analyst untuk TACO Group.
Analisis data perbandingan penawaran vendor berikut untuk RFQ: {rfq_title}

DATA PERBANDINGAN (kolom "🏆 Rekomendasi" = vendor terbaik per item berdasarkan bobot yang dipilih PIC):
{context_table}

BOBOT PRIORITAS YANG DIPAKAI PIC SAAT INI: {weights_text}
{saving_text}

Tugasmu adalah memberikan analisis otomatis tanpa perlu ditanya.
SUSUN HASIL ANALISIS DENGAN FORMAT MARKDOWN SEPERTI BERIKUT (WAJIB GUNAKAN HEADING & BULLET POINT KONSISTEN):

### ❓Penjelasan Produk:
(Berikan penjelasan SINGKAT dan PENTING mengenai spesifikasi dan merk produk, tipe, atau jenisnya, dan kegunaannya)

### 💰 Analisis Cost Saving & Trade-off:
(Jelaskan angka cost saving di atas dengan bahasa manusia — worth it atau tidak. Untuk item-item di mana vendor rekomendasi BUKAN yang termurah, jelaskan trade-off-nya: kenapa vendor itu tetap direkomendasikan meski bukan termurah — misal karena TOP lebih panjang, stock ready, atau lead time lebih cepat. Sebutkan pro & cons konkret per item kalau ada perbedaan berarti.)

### ⚖️ Evaluasi Bobot Prioritas:
(Komentari apakah bobot yang dipilih PIC saat ini {weights_text} sudah pas untuk RFQ ini. Kalau ada indikasi bobot ini kurang optimal — misal barang urgent tapi bobot lead time kecil, atau nilai RFQ besar tapi bobot harga kecil — sarankan penyesuaian bobot yang lebih masuk akal beserta alasannya.)

### 💡 Rekomendasi Merk Alternative:
(Berikan 2-3 opsi merk pengganti yang setara/lebih baik jika relevan dengan item dan spesifikasi di atas, cantumkan estimasi harga pasar & keunggulannya, atau rekomendasi vendor sesuai lokasi)

### ⚠️ Catatan Penting untuk Procurement:
(Sorot jika ada vendor yang harganya terindikasi jauh diatas harga pasar/overpriced/typo kuantitas, atau lead time terlalu lama)

### 🎯 Rekomendasi Action Plan PIC:
(Berikan langkah konkret 1, 2, 3 untuk PIC Procurement, misal: klarifikasi typo, negosiasi target harga, atau minta RFQ ulang merk alternatif. pertimbangkan juga jika barang tersebut dicatat urgent, maka pilih alternatif yang paling sesuai)

Jawab dengan tegas, profesional, berbasis angka konkret dari data di atas, serta actionable dalam Bahasa Indonesia.
"""
        with st.spinner("⚡ AI sedang menganalisis penawaran vendor..."):
            try:
                model = genai.GenerativeModel("gemini-flash-latest")
                res = model.generate_content(prompt)
                if res and res.text:
                    st.session_state[insight_key] = res.text
            except Exception as e:
                st.session_state[insight_key] = f"⚠️ AI Insight Cooldown / Error: {e}"

    # Tampilkan Hasil Analisis Otomatis
    if insight_key in st.session_state:
        with st.container(border=True):
            st.markdown(st.session_state[insight_key])
        if st.button("🔄 Regenerate Analisis", key=f"regen_{rfq_title}"):
            del st.session_state[insight_key]
            st.rerun()

    # 2. PROMPT BAR CHAT MANUAL (USER BISA NANYA TAMBAHAN)
    st.markdown("##### 💬 Tanya AI seputar penawaran ini:")
    
    # Display Riwayat Chat Manual
    for msg in st.session_state[history_key]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat Input Box
    user_prompt = st.chat_input("Contoh: 'Berapa total potensi hemat jika saya pilih Vendor A?'")
    if user_prompt:
        st.session_state[history_key].append({"role": "user", "content": user_prompt})
        with st.chat_message("user"):
            st.markdown(user_prompt)

        with st.chat_message("assistant"):
            with st.spinner("Mengolah jawaban..."):
                try:
                    context_table = df_display.to_csv(index=False)
                    full_query = f"Data CQR:\n{context_table}\n\nPertanyaan User: {user_prompt}"
                    model = genai.GenerativeModel("gemini-flash-latest")
                    response = model.generate_content(full_query)
                    answer = response.text if response else "Maaf, AI tidak dapat merespons."
                    st.markdown(answer)
                    st.session_state[history_key].append({"role": "assistant", "content": answer})
                except Exception as err:
                    st.error(f"Gagal memproses prompt: {err}")


def _strip_emoji_for_pdf(text):
    """Reportlab base fonts gak support emoji -> nongol kotak. Buang emoji, sisa teksnya tetap."""
    emoji_pattern = re.compile(
        "["
        "\U0001F300-\U0001FAFF"
        "\U00002600-\U000027BF"
        "\U0001F1E6-\U0001F1FF"
        "\U0001F900-\U0001F9FF"
        "\U00002B00-\U00002BFF"
        "\U0000FE0F"
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub("", str(text)).strip()


def build_vendor_summary(df_m, vendor_list):
    """Ringkasan per vendor: Brand, Ready Stock, Lead Time, Warranty, Payment Term (TOP)."""
    rows = {"Brand": [], "Ready Stock": [], "Lead Time (Hari)": [], "Warranty": [], "Payment Term (TOP)": []}
    for v in vendor_list:
        sub = df_m[df_m["vendor"] == v]

        brands = sorted(set(str(b).strip() for b in sub["brand"] if str(b).strip() and str(b).strip() != "-"))
        rows["Brand"].append(", ".join(brands) if brands else "-")

        stocks = set(str(s).strip() for s in sub["ready_stock"])
        if stocks == {"Ya"}:
            stock_val = "Ready Stock (Semua Item)"
        elif "Ya" in stocks:
            stock_val = "Sebagian Ready"
        else:
            stock_val = "Tidak Ready"
        rows["Ready Stock"].append(stock_val)

        lts = [lt for lt in sub["lead_time"] if lt]
        if lts:
            rows["Lead Time (Hari)"].append(f"{min(lts)}-{max(lts)} hari" if min(lts) != max(lts) else f"{lts[0]} hari")
        else:
            rows["Lead Time (Hari)"].append("-")

        warranties = sorted(set(str(w).strip() for w in sub["warranty"] if str(w).strip() and str(w).strip() != "-"))
        rows["Warranty"].append(", ".join(warranties) if warranties else "-")

        top = sub["top_days"].iloc[0] if not sub.empty else 0
        rows["Payment Term (TOP)"].append(f"{int(top)} hari" if top else "-")

    summary = pd.DataFrame(rows, index=vendor_list).T.reset_index()
    summary = summary.rename(columns={"index": "Kriteria"})
    return summary


def generate_cqr_pdf(rfq_title, pr_code, location, weights, display_df, cost_saving, saving_pct, recommended_total, ai_insight_text, summary_df=None):
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        topMargin=15 * mm, bottomMargin=15 * mm, leftMargin=12 * mm, rightMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCustom", parent=styles["Title"], fontSize=16, spaceAfter=4)
    h2_style = ParagraphStyle("H2Custom", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1f2937"))
    normal_style = ParagraphStyle("NormalCustom", parent=styles["Normal"], fontSize=9, leading=12)
    bullet_style = ParagraphStyle("BulletCustom", parent=normal_style, leftIndent=12)
    ai_head_style = ParagraphStyle("AIHead", parent=normal_style, fontSize=10, fontName="Helvetica-Bold", spaceBefore=6, spaceAfter=2)
    header_cell_style = ParagraphStyle("TblHeader", parent=normal_style, fontSize=8, textColor=colors.white, fontName="Helvetica-Bold")
    body_cell_style = ParagraphStyle("TblCell", parent=normal_style, fontSize=7.5, leading=9)

    elements = []
    elements.append(Paragraph("Competitive Quotation Record (CQR)", title_style))
    elements.append(Paragraph(f"<b>{rfq_title}</b>", normal_style))
    elements.append(Paragraph(
        f"PR Code: {pr_code} | Lokasi: {location} | Tanggal: {datetime.now().strftime('%d %b %Y')}", normal_style
    ))
    elements.append(Spacer(1, 8))

    weights_line = " &nbsp;|&nbsp; ".join(f"{k}: {v}%" for k, v in (weights or {}).items())
    elements.append(Paragraph(f"<b>Bobot Prioritas:</b> {weights_line}", normal_style))
    elements.append(Spacer(1, 4))
    elements.append(Paragraph(
        (f"<b>Potensi Cost Saving:</b> Rp {cost_saving:,.0f} ({saving_pct:.1f}%)"
         f" &nbsp;&nbsp; <b>Total Estimasi (Rekomendasi):</b> Rp {recommended_total:,.0f}").replace(",", "."),
        normal_style,
    ))
    elements.append(Spacer(1, 10))

    elements.append(Paragraph("Tabel Perbandingan", h2_style))
    raw_rows = [list(display_df.columns)] + [[str(v) for v in row] for row in display_df.values]
    wrapped_data = []
    for ri, row in enumerate(raw_rows):
        style = header_cell_style if ri == 0 else body_cell_style
        wrapped_data.append([Paragraph(_strip_emoji_for_pdf(val), style) for val in row])

    n_cols = len(display_df.columns)
    avail_width = landscape(A4)[0] - 24 * mm
    barang_width = avail_width * 0.22
    other_width = (avail_width - barang_width) / max(n_cols - 1, 1)
    col_widths = [barang_width] + [other_width] * (n_cols - 1)

    tbl = Table(wrapped_data, colWidths=col_widths, repeatRows=1)
    last_row_idx = len(wrapped_data) - 1
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("BACKGROUND", (0, last_row_idx), (-1, last_row_idx), colors.HexColor("#e2e8f0")),
    ]))
    elements.append(tbl)
    elements.append(Spacer(1, 14))

    if summary_df is not None and not summary_df.empty:
        elements.append(Paragraph("Ringkasan Spesifikasi per Vendor", h2_style))
        sum_rows = [list(summary_df.columns)] + [[str(v) for v in row] for row in summary_df.values]
        sum_wrapped = []
        for ri, row in enumerate(sum_rows):
            style = header_cell_style if ri == 0 else body_cell_style
            sum_wrapped.append([Paragraph(_strip_emoji_for_pdf(val), style) for val in row])
        sum_n_cols = len(summary_df.columns)
        sum_col_widths = [avail_width / sum_n_cols] * sum_n_cols
        sum_tbl = Table(sum_wrapped, colWidths=sum_col_widths, repeatRows=1)
        sum_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ]))
        elements.append(sum_tbl)
        elements.append(Spacer(1, 14))

    if ai_insight_text:
        elements.append(Paragraph("AI Procurement Insight", h2_style))
        for line in ai_insight_text.split("\n"):
            line = _strip_emoji_for_pdf(line)
            if not line:
                continue
            line_html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
            if line_html.startswith("### "):
                elements.append(Paragraph(line_html.replace("### ", ""), ai_head_style))
            elif line_html.startswith(("- ", "* ")):
                elements.append(Paragraph("• " + line_html[2:], bullet_style))
            else:
                elements.append(Paragraph(line_html, normal_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


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


@st.fragment
def render_pr_list(df_source, already_published):
    """Render list PR + checkbox item, dipakai buat tab Urgent & Normal.
    Dibungkus @st.fragment supaya centang checkbox / Pilih Semua / Hapus Semua
    cuma rerun bagian ini aja, bukan seluruh halaman (biar gak blinking)."""
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
                st.rerun(scope="fragment")
                
            # Tombol Hapus Semua
            if cB.button("🗑️ Hapus Semua", key=f"none_{pr_no}"):
                for k in df_group["ROW_KEY"]:
                    st.session_state["selected_items_dict"][k] = False
                st.rerun(scope="fragment")

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
# UI: PROC - IMPORT PR LIST (UPDATED)
# =====================================================================
def proc_portal_import():
    st.header("📥 Import & Publish Purchase Request")
    already_published = get_already_published_keys()

    # POIN 2: Simpan dataframe Excel ke Session State agar tidak hilang saat ganti menu
    uploaded_file = st.file_uploader("Upload File Excel", type=["xlsx"])
    
    if uploaded_file is not None:
        try:
            df_raw = pd.read_excel(uploaded_file, header=2)
            df_raw.columns = [str(c).strip().upper() for c in df_raw.columns]
            if "PR CODE" not in df_raw.columns and "DESCRIPTION" not in df_raw.columns:
                uploaded_file.seek(0)
                df_raw = pd.read_excel(uploaded_file, header=0)
                df_raw.columns = [str(c).strip().upper() for c in df_raw.columns]
            
            df_raw = df_raw.reset_index(drop=True)
            df_raw["ROW_KEY"] = df_raw.index.astype(str)
            st.session_state["uploaded_pr_df"] = df_raw
        except Exception as e:
            st.error(f"Gagal membaca file Excel: {e}")
            return

    # Ambil data dari session state jika ada
    df_raw = st.session_state.get("uploaded_pr_df")

    if df_raw is None or df_raw.empty:
        st.info("Silakan upload file Excel PR untuk memulai.")
        st.session_state["selected_items_dict"] = {}
        return

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
        return

    # Controls: Search, Collapse, & Location Filter
    c_search, c_exp = st.columns([3, 1])
    search_query = c_search.text_input("🔍 Cari (semua kolom: No. PR, Deskripsi, Lokasi, UOM, dll)...")
    
    if c_exp.button("📂 Collapse All" if st.session_state["expand_all"] else "📂 Expand All", use_container_width=True):
        st.session_state["expand_all"] = not st.session_state["expand_all"]
        st.rerun()

    # POIN 3: Filter Location di bawah Collapse All
    locations = ["Semua Lokasi"]
    if "LOCATION" in df_display.columns:
        locations += list(df_display["LOCATION"].dropna().unique())
    
    selected_loc = st.selectbox("📍 Filter Lokasi Pengiriman:", locations)

    # Apply Filter -- OPEN SEARCH: cari di SEMUA kolom (kecuali ROW_KEY internal)
    df_to_show = df_display.copy()
    if search_query:
        q = search_query.lower().strip()
        search_cols = [c for c in df_to_show.columns if c != "ROW_KEY"]
        mask = pd.Series(False, index=df_to_show.index)
        for col in search_cols:
            mask = mask | df_to_show[col].astype(str).str.lower().str.contains(q, na=False, regex=False)
        df_to_show = df_to_show[mask]

    if selected_loc != "Semua Lokasi" and "LOCATION" in df_to_show.columns:
        df_to_show = df_to_show[df_to_show["LOCATION"] == selected_loc]

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

        # POIN 6: Sembunyikan Nomor PR di Tabel Review Editor
        review_df = final_items[["LOCATION", "DESCRIPTION", "DESCRIPTION 2", "QUANTITY", "UOM"]].copy()
        review_df.columns = ["LOCATION", "DESCRIPTION", "DESCRIPTION_2", "QUANTITY", "UOM"]
        review_df["CATATAN_BARIS_ATAU_LINK_GAMBAR"] = "-"

        edited = st.data_editor(
            review_df, 
            hide_index=True, 
            use_container_width=True,
            disabled=["LOCATION", "UOM"], 
            key="admin_editor"
        )

        attached_files = st.file_uploader(
            "📁 Lampirkan file gambar/PDF referensi", accept_multiple_files=True,
            type=["png", "jpg", "jpeg", "pdf"],
        )

        df_v = get_vendors()
        if df_v.empty:
            st.warning("Belum ada vendor terdaftar.")
        else:
            sel_v_names = st.multiselect("Pilih Vendor Penerima RFQ:", df_v["vendor_name"].unique())

            # POIN 7: Logic Default Priority (Jika ada line Urgent -> Urgent, jika tidak -> Normal)
            has_urgent_item = False
            if "PRIORITY STATUS" in final_items.columns:
                has_urgent_item = final_items["PRIORITY STATUS"].astype(str).str.upper().str.contains("URGENT").any()

            default_priority_index = 0 if has_urgent_item else 1

            c_left, c_right = st.columns(2)
            with c_left:
                # POIN 4: Default Batas Waktu +3 Hari
                default_deadline = datetime.today() + timedelta(days=3)
                rfq_deadline_val = st.date_input("📅 Batas Waktu Vendor:", value=default_deadline)
                
                # POIN 7: Selection Priority dengan Default Logic
                priority_val = st.radio(
                    "🚨 Tingkat Prioritas RFQ:", 
                    ["URGENT", "NORMAL"], 
                    index=default_priority_index
                )
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
                    pr_code_main = str(final_items["PR CODE"].iloc[0]) if "PR CODE" in final_items.columns else "-"
                    location_main = str(edited["LOCATION"].iloc[0])

                    pr_id = publish_rfq(
                        rfq_title_val, pr_code_main, location_main, priority_val,
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
                                v_email, v_name, rfq_title_val,
                                rfq_deadline_val.strftime("%d %b %Y"), items_text_email,
                                delivery_type_val, pic_notes_val, attached_files or [],
                            )

                    # POIN 5: Notifikasi Sukses Terkirim
                    st.toast("🚀 Undangan RFQ Berhasil Diterbitkan!", icon="🎉")
                    st.success(f"✅ Undangan RFQ '{rfq_title_val}' telah terkirim ke vendor & email notifikasi berhasil dikirim!")
                    st.session_state["selected_items_dict"] = {}
                    st.rerun()


# =====================================================================
# UI: PROC - MONITORING & COMPARISON (UPDATED)
# =====================================================================
def proc_portal_comparison():
    res_pr = sb.table("purchase_requests").select(
        "id, pr_code, location, priority_status, rfq_title, created_at, pic_viewed_at"
    ).execute()
    df_pr = pd.DataFrame(res_pr.data) if res_pr.data else pd.DataFrame()

    active_id = st.session_state.get("active_compare_pr_id")

    # HALAMAN DETAIL
    if active_id and not df_pr.empty and active_id in df_pr["id"].values:
        pr_info = df_pr[df_pr["id"] == active_id].iloc[0]
        rfq_title_active = pr_info.get("rfq_title") or pr_info["pr_code"]
        loc_active = pr_info.get("location") or "-"

        if st.button("⬅️ Kembali ke Daftar RFQ"):
            st.session_state["active_compare_pr_id"] = None
            st.rerun()

        st.title(f"📊 {rfq_title_active}")
        st.caption(f"📍 Lokasi Pengiriman: **{loc_active}** | No. PR: **{pr_info['pr_code']}**")

        st.markdown("---")
        st.subheader("📋 Matrix Perbandingan Penawaran Vendor")

        # POIN 8: Filter Prioritas Pemilihan Vendor
        st.markdown("##### 🎯 Prioritas Pengurutan Penawaran:")
        sort_priority = st.radio(
            "Urutkan & Prioritaskan Berdasarkan:",
            ["Harga Termurah (Lowest Price)", "Lead Time Tercepat", "Ready Stock Utama", "Kombinasi Bobot Skor (Default)"],
            horizontal=True,
            label_visibility="collapsed",
        )
        # Peta pilihan dropdown -> bobot skor (Harga, TOP, Ready Stock, Lead Time)
        weight_presets = {
            "Harga Termurah (Lowest Price)": (100, 0, 0, 0),
            "Lead Time Tercepat": (0, 0, 0, 100),
            "Ready Stock Utama": (0, 0, 100, 0),
            "Kombinasi Bobot Skor (Default)": (40, 20, 20, 20),
        }
        w_price, w_top, w_stock, w_leadtime = weight_presets[sort_priority]

        with st.expander("⚙️ Atur bobot custom (opsional)"):
            use_custom = st.checkbox("Pakai bobot custom di bawah ini, override preset di atas")
            cw1, cw2, cw3, cw4 = st.columns(4)
            cust_price = cw1.slider("💰 Harga", 0, 100, w_price)
            cust_top = cw2.slider("📅 TOP", 0, 100, w_top)
            cust_stock = cw3.slider("📦 Ready Stock", 0, 100, w_stock)
            cust_leadtime = cw4.slider("⏱️ Lead Time", 0, 100, w_leadtime)
            if use_custom:
                w_price, w_top, w_stock, w_leadtime = cust_price, cust_top, cust_stock, cust_leadtime

        st.caption(f"⚖️ Bobot dipakai: Harga {w_price}% · TOP {w_top}% · Ready Stock {w_stock}% · Lead Time {w_leadtime}%")

        # Query data quotes & assignments untuk CQR Matrix Format
        raw_q = sb.table("quotes").select("*, rfq_assignments(*, pr_items(*), profiles(*))").execute()
        data_matrix = []
        vendors_in_pr = set()
        vendor_id_to_name = {}
        
        for q in raw_q.data or []:
            ass = q.get("rfq_assignments") or {}
            item = ass.get("pr_items") or {}
            
            if item.get("pr_id") == active_id:
                v_profile = ass.get("profiles") or {}
                v_name = v_profile.get("vendor_name", "Unknown")
                vendors_in_pr.add(v_name)
                if v_profile.get("id"):
                    vendor_id_to_name[v_profile["id"]] = v_name
                
                # Concat Description 1 + Description 2
                d1 = str(item.get("description") or "").strip()
                d2 = str(item.get("description2") or "").strip()
                full_item_name = f"{d1} - {d2}" if (d1 and d2 and d1 != d2) else (d1 or d2 or "-")
                clean_name = clean_description(full_item_name)
        
                data_matrix.append({
                    "Barang": clean_name,
                    "Spesifikasi Vendor": q.get("spec_vendor", "-"),
                    "Qty": item.get("quantity", 0),
                    "UOM": item.get("uom", "-"),
                    "vendor": v_name,
                    "price": q.get("unit_price", 0),
                    "total": q.get("unit_price", 0) * item.get("quantity", 0),
                    "brand": q.get("brand", "-"),
                    "lead_time": q.get("lead_time_days", 0),
                    "ready_stock": q.get("ready_stock", "-"),
                    "warranty": q.get("warranty", "-"),
                    "top_days": v_profile.get("top_days") or 0,
                })
        
        if not data_matrix:
            st.warning("Belum ada penawaran harga yang masuk dari vendor untuk RFQ ini.")
        else:
            df_m = pd.DataFrame(data_matrix)
            vendor_list_sorted = sorted(list(vendors_in_pr))

            # Barang unik jadi baris
            pivot_items = df_m[["Barang", "Qty", "UOM"]].drop_duplicates().reset_index(drop=True)

            price_lookup = {}   # (barang, vendor) -> harga numeric, buat highlight
            recommended_vendor_per_item = {}  # barang -> nama vendor terbaik
            recommended_total = 0
            worst_case_total = 0  # kalau PIC apes milih vendor termahal di tiap item, buat baseline saving

            for idx, r in pivot_items.iterrows():
                rows_for_item = df_m[df_m["Barang"] == r["Barang"]].copy()
                rows_for_item = rows_for_item.rename(columns={
                    "price": "unit_price", "lead_time": "lead_time_days",
                })
                scored = compute_recommendation(rows_for_item, w_price, w_top, w_stock, w_leadtime)

                best_row = scored[scored["is_recommended"]].iloc[0] if not scored.empty and scored["is_recommended"].any() else None
                if best_row is not None:
                    recommended_vendor_per_item[r["Barang"]] = best_row["vendor"]
                    recommended_total += float(best_row["unit_price"]) * float(r["Qty"] or 0)
                if not rows_for_item.empty:
                    worst_case_total += float(rows_for_item["unit_price"].max()) * float(r["Qty"] or 0)

                for v in vendor_list_sorted:
                    match = df_m[(df_m["Barang"] == r["Barang"]) & (df_m["vendor"] == v)]
                    price_lookup[(r["Barang"], v)] = float(match.iloc[0]["price"]) if not match.empty else None

            # Susun tabel tampilan: per vendor -> Price/Unit & Total Price (mirip format CQR)
            display_df = pivot_items.copy()
            for v in vendor_list_sorted:
                price_col, total_col = [], []
                for _, r in pivot_items.iterrows():
                    match = df_m[(df_m["Barang"] == r["Barang"]) & (df_m["vendor"] == v)]
                    if not match.empty:
                        row_val = match.iloc[0]
                        price_col.append(f"Rp {row_val['price']:,.0f}".replace(",", "."))
                        total_col.append(f"Rp {row_val['total']:,.0f}".replace(",", "."))
                    else:
                        price_col.append("-")
                        total_col.append("-")
                display_df[f"{v} — Price/Unit"] = price_col
                display_df[f"{v} — Total"] = total_col

            display_df["🏆 Rekomendasi"] = display_df["Barang"].map(recommended_vendor_per_item).fillna("-")

            # Baris Grand Total di bawah (khusus kolom Total per vendor)
            grand_total_row = {"Barang": "GRAND TOTAL", "Qty": "", "UOM": ""}
            for v in vendor_list_sorted:
                vendor_total = df_m[df_m["vendor"] == v]["total"].sum()
                grand_total_row[f"{v} — Price/Unit"] = ""
                grand_total_row[f"{v} — Total"] = f"Rp {vendor_total:,.0f}".replace(",", ".")
            grand_total_row["🏆 Rekomendasi"] = f"Rp {recommended_total:,.0f}".replace(",", ".")
            display_df = pd.concat([display_df, pd.DataFrame([grand_total_row])], ignore_index=True)

            # Styling: highlight sel harga vendor yang direkomendasikan per baris item (hijau)
            def highlight_recommended_cells(row):
                styles = [""] * len(row)
                if row["Barang"] == "GRAND TOTAL":
                    return ["font-weight: bold; background-color: #f1f5f9;"] * len(row)
                best_vendor = recommended_vendor_per_item.get(row["Barang"])
                for i, col in enumerate(row.index):
                    if best_vendor and col in (f"{best_vendor} — Price/Unit", f"{best_vendor} — Total", "🏆 Rekomendasi"):
                        styles[i] = "background-color: #d1fae5; font-weight: 600;"
                return styles

            st.markdown("##### 📋 Competitive Quotation Record (CQR)")
            st.dataframe(
                display_df.style.apply(highlight_recommended_cells, axis=1),
                hide_index=True,
                use_container_width=True,
                row_height=80,
                column_config={
                    "Barang": st.column_config.TextColumn("Barang", width=320),
                    "Qty": st.column_config.TextColumn("Qty", width="small"),
                    "UOM": st.column_config.TextColumn("UOM", width="small"),
                },
            )
            cost_saving = worst_case_total - recommended_total
            saving_pct = (cost_saving / worst_case_total * 100) if worst_case_total > 0 else 0
            c_save1, c_save2 = st.columns(2)
            c_save1.metric("💰 Potensi Cost Saving", f"Rp {cost_saving:,.0f}".replace(",", "."), f"{saving_pct:.1f}% dari skenario termahal")
            c_save2.metric("🎯 Total Estimasi (sesuai rekomendasi)", f"Rp {recommended_total:,.0f}".replace(",", "."))

            st.markdown("##### 📌 Ringkasan Spesifikasi per Vendor")
            summary_df = build_vendor_summary(df_m, vendor_list_sorted)
            st.dataframe(summary_df, hide_index=True, use_container_width=True)

            # ---------------------------------------------------------
            # SPLIT PO: kelompokkan item berdasarkan vendor rekomendasi
            # (kalau vendor terbaik beda-beda per item, PIC bisa split PO)
            # ---------------------------------------------------------
            split_data = {}
            for _, r in pivot_items.iterrows():
                best_v = recommended_vendor_per_item.get(r["Barang"])
                if not best_v:
                    continue
                match = df_m[(df_m["Barang"] == r["Barang"]) & (df_m["vendor"] == best_v)]
                if match.empty:
                    continue
                row_val = match.iloc[0]
                split_data.setdefault(best_v, []).append({
                    "Barang": r["Barang"],
                    "Qty": r["Qty"],
                    "UOM": r["UOM"],
                    "Brand": row_val["brand"],
                    "Unit Price": row_val["price"],
                    "Total": row_val["total"],
                    "Lead Time (Hari)": row_val["lead_time"],
                })

            if len(split_data) > 1:
                st.markdown("##### 📦 Split PO — Rekomendasi Alokasi per Vendor")
                st.caption(
                    "Vendor terbaik beda-beda per item sesuai bobot di atas, jadi PO bisa displit "
                    f"ke **{len(split_data)} vendor** berikut supaya tetap dapat kombinasi termurah/terbaik."
                )
                split_tabs = st.tabs([f"📦 {v} ({len(items)} item)" for v, items in split_data.items()])
                for tab, (v_name, items) in zip(split_tabs, split_data.items()):
                    with tab:
                        df_split = pd.DataFrame(items)
                        df_split_display = df_split.copy()
                        df_split_display["Unit Price"] = df_split_display["Unit Price"].apply(lambda x: f"Rp {x:,.0f}".replace(",", "."))
                        df_split_display["Total"] = df_split_display["Total"].apply(lambda x: f"Rp {x:,.0f}".replace(",", "."))
                        st.dataframe(df_split_display, hide_index=True, use_container_width=True)
                        subtotal = df_split["Total"].sum()
                        st.metric(f"Subtotal PO — {v_name}", f"Rp {subtotal:,.0f}".replace(",", "."))

                        po_buf = io.BytesIO()
                        with pd.ExcelWriter(po_buf, engine="openpyxl") as writer:
                            df_split.to_excel(writer, index=False, sheet_name="PO Items")
                        st.download_button(
                            f"📥 Download List PO — {v_name}",
                            po_buf.getvalue(),
                            f"PO_{rfq_title_active}_{v_name}.xlsx",
                            key=f"po_dl_{pr_info['id']}_{v_name}",
                            use_container_width=True,
                        )
            elif len(split_data) == 1:
                st.caption("💡 Semua item direkomendasikan dari vendor yang sama — tidak perlu split PO.")

            # Dokumen resmi yang diupload vendor (kalau ada)
            all_docs = get_vendor_documents(active_id)
            if all_docs:
                st.markdown("##### 📎 Dokumen RFQ Resmi dari Vendor")
                for d in all_docs:
                    owner_name = vendor_id_to_name.get(d.get("vendor_id"), "Vendor")
                    st.caption(f"📄 [{owner_name}] {d['file_name']}")

            weights_dict = {"Harga": w_price, "TOP": w_top, "Ready Stock": w_stock, "Lead Time": w_leadtime}

            st.divider()
            render_ai_insight(
                display_df, rfq_title_active,
                weights=weights_dict,
                cost_saving=cost_saving, saving_pct=saving_pct, recommended_total=recommended_total,
            )

            # PDF Download -- ditaruh SETELAH AI insight biar analisisnya ikut kecapture di PDF
            ai_insight_text = st.session_state.get(f"ai_insight_{rfq_title_active}", "")
            pdf_bytes = generate_cqr_pdf(
                rfq_title_active, pr_info["pr_code"], loc_active, weights_dict,
                display_df, cost_saving, saving_pct, recommended_total, ai_insight_text,
                summary_df=summary_df,
            )
            st.write(" ")
            if pdf_bytes:
                st.download_button(
                    "📄 Download CQR (PDF)",
                    pdf_bytes,
                    f"CQR_{rfq_title_active}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
            else:
                st.caption("⚠️ Library `reportlab` belum terinstall — tambahkan ke requirements.txt untuk mengaktifkan download PDF.")

            # POIN 1: Tombol Mark as Submitted
            if st.button("🔒 Mark as Submitted (Selesai & Arsip)", type="primary", use_container_width=True):
                try:
                    items_res = sb.table("pr_items").select("id").eq("pr_id", active_id).execute()
                    item_ids = [i["id"] for i in items_res.data] if items_res.data else []

                    if item_ids:
                        sb.table("rfq_assignments").update({"status": "Submitted"}).in_("item_id", item_ids).execute()
                        st.toast("RFQ Berhasil Diarsip!", icon="🔒")
                        st.success(f"🎉 RFQ '{rfq_title_active}' telah ditandai sebagai Submitted & berhasil diarsip.")
                        st.session_state["active_compare_pr_id"] = None
                        st.rerun()
                except Exception as e:
                    st.error(f"Gagal mengarsip RFQ: {e}")

    # HALAMAN LIST DAFTAR RFQ
    else:
        st.header("📊 Monitoring & Price Comparison")

        if df_pr.empty:
            st.info("Belum ada data RFQ yang dipublish.")
        else:
            st.write("Pilih salah satu RFQ di bawah untuk membuka **Halaman Detail Perbandingan**:")

            quotes_res = sb.table("quotes").select("assignment_id").execute()
            submitted_ass_ids = set([q["assignment_id"] for q in quotes_res.data]) if quotes_res.data else set()

            # Ambil semua assignment + vendor + item sekali jalan untuk keperluan search & status
            res_ass_full = (
                sb.table("rfq_assignments")
                .select("id, profiles(vendor_name), pr_items(pr_id, description, description2)")
                .execute()
            )
            ass_by_pr = {}
            for ass in (res_ass_full.data or []):
                item = ass.get("pr_items") or {}
                p_id = item.get("pr_id")
                ass_by_pr.setdefault(p_id, []).append(ass)

            search_query = st.text_input("🔍 Cari Judul RFQ / Lokasi / Vendor / Item...").strip().lower()

            # Sort: RFQ paling baru dikirim ada di paling atas
            df_pr_sorted = df_pr.copy()
            df_pr_sorted["created_at"] = pd.to_datetime(df_pr_sorted.get("created_at"), errors="coerce")
            df_pr_sorted = df_pr_sorted.sort_values("created_at", ascending=False, na_position="last")

            st.markdown("---")

            shown_count = 0
            for _, pr_row in df_pr_sorted.iterrows():
                pr_id = pr_row["id"]
                title = pr_row.get("rfq_title") or f"PR: {pr_row['pr_code']}"
                loc = pr_row.get("location") or "-"
                prio = str(pr_row.get("priority_status") or "")
                tag_prio = "🚨 URGENT" if "URGENT" in prio.upper() else "📦 NORMAL"
                assignments_this_pr = ass_by_pr.get(pr_id, [])

                # Bangun teks pencarian gabungan (judul, lokasi, vendor, item)
                vendor_names = [(a.get("profiles") or {}).get("vendor_name", "") for a in assignments_this_pr]
                item_texts = [
                    f"{(a.get('pr_items') or {}).get('description', '')} {(a.get('pr_items') or {}).get('description2', '')}"
                    for a in assignments_this_pr
                ]
                haystack = " ".join([str(title), str(loc), str(pr_row["pr_code"])] + vendor_names + item_texts).lower()
                if search_query and search_query not in haystack:
                    continue
                shown_count += 1

                # Badge status: Baru (belum pernah dibuka PIC) / Sudah Dibuka
                is_new = pd.isna(pr_row.get("pic_viewed_at")) or not pr_row.get("pic_viewed_at")
                status_badge = "🆕 Baru" if is_new else "👀 Sudah Dibuka"

                with st.container(border=True):
                    c_info, c_btn = st.columns([4, 1])

                    with c_info:
                        st.subheader(f"📋 {title}")
                        st.caption(
                            f"📍 **Lokasi:** {loc} | **Priority:** {tag_prio} | **PR Code:** {pr_row['pr_code']} | {status_badge}"
                        )

                        if assignments_this_pr:
                            vendor_status_map = {}
                            for ass in assignments_this_pr:
                                vn = (ass.get("profiles") or {}).get("vendor_name", "Vendor")
                                is_sub = ass["id"] in submitted_ass_ids
                                vendor_status_map[vn] = vendor_status_map.get(vn, False) or is_sub

                            v_display_list = [f"{vn} {'✅' if is_sub else '⏳'}" for vn, is_sub in vendor_status_map.items()]
                            st.write("**Status Vendor:** " + " | ".join(v_display_list))

                    with c_btn:
                        st.write(" ")
                        if st.button("🔍 Buka Detail", key=f"open_detail_{pr_id}", type="primary", use_container_width=True):
                            mark_pic_viewed(pr_id)
                            st.session_state["active_compare_pr_id"] = pr_id
                            st.rerun()

            if search_query and shown_count == 0:
                st.info("Tidak ada RFQ yang cocok dengan pencarian.")
# =====================================================================
# AUTO-REMINDER VENDOR (POIN 4)
# =====================================================================
def check_and_send_vendor_reminders(pr_id, rfq_title):
    """
    Mengecek vendor mana saja yang belum mengisi penawaran (quotes)
    dan otomatis mengirimkan email reminder.
    """
    # Ambil semua assignment untuk RFQ ini
    res_ass = (
        sb.table("rfq_assignments")
        .select("id, vendor_id, deadline, profiles(vendor_name, email), pr_items!inner(pr_id)")
        .eq("pr_items.pr_id", pr_id)
        .eq("status", "Open")
        .execute()
    )

    if not res_ass.data:
        return 0

    # Ambil ID assignment yang sudah diisi
    quotes_res = sb.table("quotes").select("assignment_id").execute()
    submitted_ids = set([q["assignment_id"] for q in quotes_res.data]) if quotes_res.data else set()

    reminded_count = 0
    for ass in res_ass.data:
        if ass["id"] not in submitted_ids:
            v_profile = ass.get("profiles") or {}
            v_email = v_profile.get("email")
            v_name = v_profile.get("vendor_name", "Vendor")
            deadline_str = ass.get("deadline", "Segera")

            if v_email:
                subject = f"⏰ REMINDER: Undangan RFQ - TACO - {rfq_title}"
                body = (
                    f"Dear {v_name},\n\n"
                    f"Ini adalah pengingat otomatis bahwa Anda belum mengisi Request for Quotation (RFQ):\n\n"
                    f"Judul RFQ: {rfq_title}\n"
                    f"Batas Waktu: {deadline_str}\n\n"
                    f"Mohon untuk segera mengisi penawaran harga Anda di portal: https://taco-rfq.streamlit.app/\n\n"
                    f"Salam,\nTACO Procurement Team"
                )
                
                try:
                    msg = MIMEMultipart()
                    msg["From"] = st.secrets["email_config"].get("smtp_user", "")
                    msg["To"] = v_email
                    msg["Subject"] = subject
                    msg.attach(MIMEText(body, "plain"))

                    server = smtplib.SMTP("smtp.gmail.com", 587)
                    server.starttls()
                    server.login(
                        st.secrets["email_config"].get("smtp_user", ""),
                        st.secrets["email_config"].get("smtp_password", "")
                    )
                    server.sendmail(st.secrets["email_config"].get("smtp_user", ""), v_email, msg.as_string())
                    server.quit()
                    reminded_count += 1
                except Exception as e:
                    st.warning(f"Gagal mengirim reminder ke {v_email}: {e}")

    return reminded_count
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
    st.sidebar.markdown("## 🧭 Navigasi Menu")
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
                "created_at": pr.get("created_at"),
                "rows": []
            })
            pr_groups[pr.get("id")]["rows"].append(a)

        # Sort: RFQ paling baru dikirim ada di paling atas
        pr_groups = dict(
            sorted(
                pr_groups.items(),
                key=lambda kv: pd.to_datetime(kv[1].get("created_at"), errors="coerce") or pd.Timestamp.min,
                reverse=True,
            )
        )

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
            st.markdown("##### ✏️ Masukkan Harga & Detail Penawaran:")
            # Attachment File
            attachments = get_pr_attachments(active_rfq_id)
            if attachments:
                st.markdown("**📎 File Referensi Lampiran:** " + ", ".join(f"`{a['file_name']}`" for a in attachments))

            # Data Preview Items untuk Vendor (Concat Description + Description2)
            table_rows = []
            for a in group["rows"]:
                item = a.get("pr_items") or {}
                
                # 1. Concat Description + Description 2
                d1 = str(item.get("description") or "").strip()
                d2 = str(item.get("description2") or "").strip()
                full_desc = f"{d1} - {d2}" if (d1 and d2 and d1 != d2) else (d1 or d2 or "-")
                clean_item_name = clean_description(full_desc)
            
                # 2. Ambil data quote lama jika ada
                existing_quotes = a.get("quotes") or []
                last_quote = existing_quotes[-1] if existing_quotes else {}
            
                table_rows.append({
                    "assignment_id": a["id"],
                    "Barang": clean_item_name,                     # Concat Nama Barang
                    "Spesifikasi Vendor": last_quote.get("spec_vendor", "-"), # Diisi Vendor
                    "Qty": item.get("quantity", 0),
                    "UOM": item.get("uom", "-"),
                    "Unit Price (IDR)": last_quote.get("unit_price", 0),
                    "Brand": last_quote.get("brand", "-"),
                    "Ready Stock": last_quote.get("ready_stock", "Ya"),
                    "Lead Time (Hari)": last_quote.get("lead_time_days", 7),
                    "Warranty": last_quote.get("warranty", "-"),
                })
            
            df_preview = pd.DataFrame(table_rows)

            # Download daftar barang (Excel) -- buat vendor kerja offline kalau perlu
            excel_buf = io.BytesIO()
            with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
                df_preview.drop(columns=["assignment_id"]).to_excel(writer, index=False, sheet_name="Daftar Barang")
            st.download_button(
                "📥 Download Daftar Barang (Excel)",
                excel_buf.getvalue(),
                f"Daftar_Barang_{group['title']}.xlsx",
                use_container_width=True,
            )

            
            st.caption("Text dapat di copy-paste dari excel (khusus angka mohon copy tanpa format)")
            
            # 3. Data Editor Vendor dengan Column Configuration & Word-Wrap
            edited = st.data_editor(
                df_preview.drop(columns=["assignment_id"]),
                key=f"editor_v_{active_rfq_id}",
                hide_index=True,
                use_container_width=True,
                row_height=80,
                disabled=["Barang", "Qty", "UOM"], # Barang, Qty, UOM dikunci
                column_config={
                    "Barang": st.column_config.TextColumn(
                        "Barang",
                        width=280,  # Lebar fix (px) supaya word-wrap kepakai konsisten
                        help="Nama Barang & Deskripsi Utama"
                    ),
                    "Spesifikasi Vendor": st.column_config.TextColumn(
                        "Spesifikasi Vendor",
                        width="medium",
                        help="Tuliskan spesifikasi detail merk/tipe barang yang Anda tawarkan"
                    ),
                    "Qty": st.column_config.NumberColumn("Qty", width="small"),
                    "UOM": st.column_config.TextColumn("UOM", width="small"),
                    "Unit Price (IDR)": st.column_config.NumberColumn(
                        "Unit Price (IDR)",
                        format="Rp %,d",
                        min_value=0,
                        step=1000,
                    ),
                    "Ready Stock": st.column_config.SelectboxColumn("Ready Stock", options=["Ya", "Tidak"], required=True),
                    "Lead Time (Hari)": st.column_config.NumberColumn("Lead Time (Hari)", min_value=1, step=1),
                    "Warranty": st.column_config.TextColumn("Warranty", width="small", help="Contoh: 1 Tahun, 6 Bulan, atau '-' kalau tidak ada"),
                },
            )

            st.markdown("##### 📎 Upload RFQ Resmi / Surat Penawaran")
            st.caption("Mohon dapat melampirkan RFQ resmi dengan kop surat atau cap perusahaan dan tandatangan.")
            official_doc = st.file_uploader("Pilih file PDF", type=["pdf"], key=f"official_doc_{active_rfq_id}")

            existing_docs = get_vendor_documents(active_rfq_id, vendor_id)
            if existing_docs:
                st.write("**Dokumen yang sudah diupload:**")
                for d in existing_docs:
                    st.caption(f"📄 {d['file_name']} — {d['uploaded_at'][:10]}")

            if st.button("🚀 Kirim Penawaran", type="primary", use_container_width=True):
                all_ok = True
                for idx, r in edited.iterrows():
                    ass_id = df_preview.iloc[idx]["assignment_id"]
                    ok, err = submit_quote(
                        ass_id, vendor_id,
                        r["Unit Price (IDR)"], r["Brand"], r["Lead Time (Hari)"],
                        r["Ready Stock"], r["Warranty"], r["Spesifikasi Vendor"],
                    )
                    if not ok:
                        all_ok = False
                        st.error(f"❌ Gagal menyimpan baris '{r['Barang']}': {err}")

                if all_ok:
                    if official_doc is not None:
                        upload_vendor_document(active_rfq_id, vendor_id, official_doc)
                    st.success(f"🎉 Penawaran berhasil dikirim!")
                    st.session_state["active_vendor_rfq_id"] = None
                    st.rerun()

        # TAMPILAN CARD LIST RFQ
        else:
            st.header("📋 List RFQ Aktif")
            st.write("Klik **Buka Detail** untuk mengisi penawaran harga:")

            v_search = st.text_input("🔍 Cari Judul RFQ / Lokasi / PIC / Item...").strip().lower()

            st.markdown("---")

            shown_count = 0
            for pr_id, group in pr_groups.items():
                prio_tag = "🚨 URGENT" if "URGENT" in group["prio"].upper() else "📦 NORMAL"

                item_texts = []
                for a in group["rows"]:
                    it = a.get("pr_items") or {}
                    item_texts.append(f"{it.get('description', '')} {it.get('description2', '')}")
                haystack = " ".join(
                    [group["title"], group["location"], group["pic_name"], group["pr_code"]] + item_texts
                ).lower()
                if v_search and v_search not in haystack:
                    continue
                shown_count += 1

                # Status: Baru (belum dibuka sama sekali) / Dibuka belum submit / Sudah Submit (semua/sebagian)
                total_rows = len(group["rows"])
                viewed_rows = sum(1 for a in group["rows"] if a.get("vendor_viewed_at"))
                submitted_rows = sum(1 for a in group["rows"] if a.get("quotes"))

                if viewed_rows == 0:
                    status_badge = "🆕 Baru"
                elif submitted_rows == 0:
                    status_badge = "👀 Sudah Dibuka (Belum Submit)"
                elif submitted_rows < total_rows:
                    status_badge = "🔶 Submit Sebagian"
                else:
                    status_badge = "✅ Sudah Submit"

                with st.container(border=True):
                    c_info, c_btn = st.columns([4, 1])

                    with c_info:
                        st.subheader(f"📋 {group['title']}")
                        # INFO PIC DITAMBAHKAN PADA CARD LIST
                        st.caption(
                            f"👤 **PIC Procurement:** {group['pic_name']} | 📍 **Lokasi:** {group['location']} "
                            f"| **Priority:** {prio_tag} | **PR Code:** {group['pr_code']} | {status_badge}"
                        )
                    
                    with c_btn:
                        st.write(" ")
                        if st.button("🔍 Buka Detail", key=f"v_detail_{pr_id}", type="primary", use_container_width=True):
                            mark_vendor_viewed([a["id"] for a in group["rows"]])
                            st.session_state["active_vendor_rfq_id"] = pr_id
                            st.rerun()

            if v_search and shown_count == 0:
                st.info("Tidak ada RFQ yang cocok dengan pencarian.")

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
