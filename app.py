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
def publish_rfq(pr_code, location, priority, admin_id, items_df, vendor_ids,
                 delivery_type, pic_notes, deadline, files):
    pr_id = get_or_create_pr(pr_code, location, priority, admin_id)

    # Upload attachments sekali per PR (dipakai bareng oleh semua vendor yang dituju)
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
        .select("*, pr_items(*, purchase_requests(*))")
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
# EMAIL
# =====================================================================
def send_rfq_email(vendor_email, vendor_name, pr_code, deadline_str, items_text,
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
    body = (
        f"Dear {vendor_name},\n\n"
        f"Kami mengundang Anda untuk mengisi Request for Quotation (RFQ):\n\n"
        f"No. PR: {pr_code}\nBatas Waktu: {deadline_str}\nMetode Pengiriman: {delivery_type}\n"
        f"Catatan Tambahan PIC: {pic_notes if pic_notes else '-'}\n\nDaftar Item:\n{items_text}\n\n"
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
# AI ASSISTANT (Gemini)
# =====================================================================
def render_ai_chat(df_display, pr_code):
    if "gemini" not in st.secrets or not st.secrets["gemini"].get("api_key"):
        st.caption("💡 Fitur AI belum aktif — tambahkan `gemini.api_key` di secrets untuk mengaktifkan.")
        return

    try:
        import google.generativeai as genai
    except ImportError:
        st.caption("⚠️ Library `google-generativeai` belum terinstall. Tambahkan ke requirements.txt.")
        return

    genai.configure(api_key=st.secrets["gemini"]["api_key"])

    chat_key = f"ai_chat_{pr_code}"
    if chat_key not in st.session_state:
        st.session_state[chat_key] = []

    for msg in st.session_state[chat_key]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    question = st.chat_input("Contoh: vendor mana paling worth it buat item pertama?")
    if question:
        st.session_state[chat_key].append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        context_table = df_display.to_csv(index=False)
        prompt = f"""Kamu adalah asisten procurement yang membantu PIC menganalisis perbandingan harga vendor.
Berikut data perbandingan untuk PR {pr_code} (kolom Skor: makin tinggi makin direkomendasikan, ⭐ Rekomendasi=True berarti vendor terbaik untuk item itu berdasarkan bobot yang dipilih PIC):

{context_table}

Pertanyaan PIC: {question}

Jawab singkat, jelas, dan actionable dalam Bahasa Indonesia. Kalau relevan, sebut nama vendor dan angka konkret dari data di atas. Jangan mengarang data yang tidak ada di tabel."""

        try:
            model = genai.GenerativeModel("gemini-2.5-flash")
            with st.chat_message("assistant"):
                with st.spinner("Mikir..."):
                    response = model.generate_content(prompt)
                    answer = response.text
                    st.markdown(answer)
            st.session_state[chat_key].append({"role": "assistant", "content": answer})
        except Exception as e:
            st.error(f"Gagal menghubungi Gemini: {e}")



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


# =====================================================================
# UI: PROC (PIC procurement — kerja harian RFQ)
# =====================================================================
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
            if cA.button("✅ Pilih Semua", key=f"all_{pr_no}"):
                for k in df_group["ROW_KEY"]:
                    st.session_state["selected_items_dict"][k] = True
                st.rerun()
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
                    checked = c1.checkbox(
                        "sel", key=f"chk_{row_key}",
                        value=st.session_state["selected_items_dict"].get(row_key, False),
                        label_visibility="collapsed",
                    )
                    st.session_state["selected_items_dict"][row_key] = checked
                    c2.write(item_row.get("DESCRIPTION", ""))
                    c3.write(item_row.get("DESCRIPTION 2", ""))
                    c4.write(item_row.get("QUANTITY", ""))
                    c5.write(item_row.get("UOM", ""))
                    st.markdown("</div>", unsafe_allow_html=True)


def proc_portal(tabs=None):
    if tabs is None:
        tabs = st.tabs(["📥 Import PR List", "📊 Monitoring & Comparison", "🔍 History"])
    already_published = get_already_published_keys()

    with tabs[0]:
        st.header("Upload Purchase Request")
        uploaded_file = st.file_uploader("Upload File Excel", type=["xlsx"])
        if uploaded_file is None:
            st.session_state["selected_items_dict"] = {}
        else:
            df_raw = pd.read_excel(uploaded_file, header=2)
            df_raw.columns = [str(c).strip().upper() for c in df_raw.columns]
            df_raw = df_raw.reset_index(drop=True)
            df_raw["ROW_KEY"] = df_raw.index.astype(str)

            if "selected_items_dict" not in st.session_state:
                st.session_state["selected_items_dict"] = {}
            if "expand_all" not in st.session_state:
                st.session_state["expand_all"] = False

            df_display = df_raw.copy()
            if "STATUS" in df_raw.columns and "QUANTITY" in df_raw.columns:
                df_raw["QUANTITY"] = pd.to_numeric(df_raw["QUANTITY"], errors="coerce").fillna(0)
                df_display = df_raw[
                    (df_raw["STATUS"].astype(str).str.strip() == "Open") & (df_raw["QUANTITY"] > 0)
                ].copy()

            if df_display.empty:
                st.warning("Tidak ada item berstatus 'Open' dengan Qty > 0 di file ini.")
            else:
                search_query = st.text_input("🔍 Cari No. PR atau Nama Item...")
                df_to_show = df_display.copy()
                if search_query:
                    q = search_query.lower()
                    mask = (
                        df_to_show["PR CODE"].astype(str).str.lower().str.contains(q, na=False)
                        | df_to_show["DESCRIPTION"].astype(str).str.lower().str.contains(q, na=False)
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
                    with st.container(height=500, border=True):
                        render_pr_list(df_urgent, already_published)

                with sub_tab_normal:
                    with st.container(height=500, border=True):
                        render_pr_list(df_normal, already_published)

                st.divider()
                st.subheader("🎯 Review & Assign Vendor")
                selected_keys = [k for k, v in st.session_state["selected_items_dict"].items() if v]
                final_items = df_display[df_display["ROW_KEY"].isin(selected_keys)].copy()

                if final_items.empty:
                    st.info("Belum ada item yang dipilih.")
                else:
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
                        st.warning("Belum ada vendor terdaftar. Daftarkan dulu di tab 'Register Vendor'.")
                    else:
                        sel_v_names = st.multiselect("Pilih Vendor Penerima RFQ:", df_v["vendor_name"].unique())

                        c_left, c_right = st.columns(2)
                        with c_left:
                            rfq_deadline_val = st.date_input("📅 Batas Waktu Vendor:", value=datetime.today())
                            delivery_type_val = st.radio("🚚 Metode Pengiriman:", ["Franco (Kirim ke lokasi)", "Loco (Pengambilan sendiri)"])
                        with c_right:
                            pic_notes_val = st.text_area("📝 Catatan Tambahan Khusus Vendor:")

                        if st.button("🚀 Publish Undangan RFQ", type="primary", use_container_width=True):
                            if not sel_v_names:
                                st.error("Silakan pilih minimal satu vendor.")
                            else:
                                vendor_ids = df_v[df_v["vendor_name"].isin(sel_v_names)]["id"].tolist()
                                pr_code_main = str(edited["PR_CODE"].iloc[0])
                                location_main = str(edited["LOCATION"].iloc[0])
                                priority_main = prio if "prio" in dir() else "-"

                                pr_id = publish_rfq(
                                    pr_code_main, location_main, priority_main,
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
                                            v_email, v_name, pr_code_main,
                                            rfq_deadline_val.strftime("%d %b %Y"), items_text_email,
                                            delivery_type_val, pic_notes_val, attached_files or [],
                                        )

                                st.success("🎉 Berhasil! RFQ tersimpan dan email terkirim ke vendor.")
                                st.session_state["selected_items_dict"] = {}
                                st.rerun()

    with tabs[1]:
        st.header("Price Comparison & Rekomendasi Vendor")
        df_prices = get_price_comparison_data()
        if df_prices.empty:
            st.info("Belum ada penawaran masuk dari vendor.")
        else:
            pr_list = df_prices["pr_code"].dropna().unique()
            sel_pr = st.selectbox("Pilih Nomor PR:", pr_list)
            sub = df_prices[df_prices["pr_code"] == sel_pr].copy()

            st.markdown("##### ⚖️ Bobot Prioritas (total otomatis dinormalisasi)")
            c1, c2, c3, c4 = st.columns(4)
            w_price = c1.slider("💰 Harga", 0, 100, 40)
            w_top = c2.slider("📅 TOP", 0, 100, 25)
            w_stock = c3.slider("📦 Ready Stock", 0, 100, 20)
            w_leadtime = c4.slider("⏱️ Lead Time", 0, 100, 15)

            # Hitung rekomendasi per item (grouping description+description2+qty+uom)
            result_frames = []
            for keys, grp in sub.groupby(["description", "description2", "qty", "uom"], dropna=False):
                scored = compute_recommendation(grp, w_price, w_top, w_stock, w_leadtime)
                result_frames.append(scored)
            df_scored = pd.concat(result_frames, ignore_index=True) if result_frames else sub

            st.markdown("##### 🏆 Rekomendasi per Item")
            display_cols = ["description", "description2", "qty", "uom", "vendor", "unit_price", "top_days", "ready_stock", "lead_time_days", "score", "is_recommended"]
            df_display = df_scored[display_cols].rename(columns={
                "description": "Deskripsi", "description2": "Deskripsi 2", "qty": "Qty", "uom": "UOM",
                "vendor": "Vendor", "unit_price": "Harga", "top_days": "TOP (hari)",
                "ready_stock": "Ready Stock", "lead_time_days": "Lead Time (hari)",
                "score": "Skor", "is_recommended": "⭐ Rekomendasi",
            }).sort_values(["Deskripsi", "Skor"], ascending=[True, False])

            def highlight_recommended(row):
                return ["background-color: #d1fae5" if row["⭐ Rekomendasi"] else "" for _ in row]

            st.dataframe(df_display.style.apply(highlight_recommended, axis=1), use_container_width=True, hide_index=True)

            # Download Excel
            import io
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df_display.to_excel(writer, index=False, sheet_name="Comparison")
            st.download_button(
                "📥 Download Comparison Sheet (Excel)",
                output.getvalue(),
                f"Comparison_{sel_pr}.xlsx",
                use_container_width=True,
            )

            st.divider()
            st.markdown("##### 🤖 Tanya AI soal perbandingan ini")
            render_ai_chat(df_display, sel_pr)

    with tabs[2]:
        st.header("🔍 History RFQ")
        df_hist = get_history_data()
        if df_hist.empty:
            st.info("Belum ada riwayat publikasi.")
        else:
            st.dataframe(df_hist, hide_index=True, use_container_width=True)


# =====================================================================
# UI: ADMIN (khusus urus akun — TIDAK ikut kerjaan RFQ harian)
# =====================================================================
def admin_portal(tabs=None):
    if tabs is None:
        tabs = st.tabs(["➕ Daftarkan PIC", "➕ Daftarkan Vendor", "👥 Daftar User", "🔑 Reset Password"])

    with tabs[0]:
        st.header("Daftarkan PIC Procurement")
        st.caption("PIC yang didaftarkan di sini akan punya akses ke Import PR, Publish RFQ, Comparison & History — TAPI tidak bisa daftarin user baru.")

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
                        import random, string
                        auto_password = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                        ok, err = register_user(p_name, p_email, auto_password, "proc")
                        if ok:
                            st.success(f"🎉 PIC {p_name} berhasil didaftarkan.")
                            st.info(f"🔑 Password: `{auto_password}` — catat & kirim manual ke PIC ybs, ini cuma muncul sekali.")
                        else:
                            st.error(f"❌ Gagal: {err}")

        with sub2:
            st.write("Upload file Excel/CSV dengan 2 kolom: **name** dan **email** (1 baris = 1 PIC).")
            bulk_file = st.file_uploader("Upload file", type=["xlsx", "csv"], key="bulk_pic")
            if bulk_file is not None:
                df_bulk = pd.read_csv(bulk_file) if bulk_file.name.endswith(".csv") else pd.read_excel(bulk_file)
                st.dataframe(df_bulk, use_container_width=True, hide_index=True)
                if st.button("🚀 Daftarkan Semua PIC Ini", type="primary"):
                    with st.spinner("Mendaftarkan..."):
                        result_df = bulk_register_users(df_bulk, "proc")
                    st.success("Selesai! Cek hasil & password di tabel bawah (SIMPAN sekarang, tidak muncul lagi).")
                    st.dataframe(result_df, use_container_width=True, hide_index=True)
                    csv = result_df.to_csv(index=False).encode("utf-8")
                    st.download_button("📥 Download Hasil (CSV, berisi password)", csv, "hasil_daftar_pic.csv")

    with tabs[1]:
        st.header("Daftarkan Vendor")
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
                        import random, string
                        auto_password = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                        ok, err = register_user(v_name, v_email, auto_password, "vendor")
                        if ok:
                            st.success(f"🎉 Vendor {v_name} berhasil didaftarkan.")
                            st.info(f"🔑 Password: `{auto_password}` — catat & kirim manual ke vendor ybs, ini cuma muncul sekali.")
                        else:
                            st.error(f"❌ Gagal: {err}")

        with sub2:
            st.write("Upload file Excel/CSV dengan 2 kolom: **name** dan **email** (1 baris = 1 vendor).")
            bulk_file = st.file_uploader("Upload file", type=["xlsx", "csv"], key="bulk_vendor")
            if bulk_file is not None:
                df_bulk = pd.read_csv(bulk_file) if bulk_file.name.endswith(".csv") else pd.read_excel(bulk_file)
                st.dataframe(df_bulk, use_container_width=True, hide_index=True)
                if st.button("🚀 Daftarkan Semua Vendor Ini", type="primary"):
                    with st.spinner("Mendaftarkan..."):
                        result_df = bulk_register_users(df_bulk, "vendor")
                    st.success("Selesai! Cek hasil & password di tabel bawah (SIMPAN sekarang, tidak muncul lagi).")
                    st.dataframe(result_df, use_container_width=True, hide_index=True)
                    csv = result_df.to_csv(index=False).encode("utf-8")
                    st.download_button("📥 Download Hasil (CSV, berisi password)", csv, "hasil_daftar_vendor.csv")

    with tabs[2]:
        st.header("👥 Daftar Semua User")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**PIC Procurement**")
            st.dataframe(get_users_by_role("proc")[["email", "vendor_name", "created_at"]] if not get_users_by_role("proc").empty else pd.DataFrame(), hide_index=True, use_container_width=True)
        with c2:
            st.markdown("**Vendor**")
            df_v = get_vendors()
            st.dataframe(df_v[["email", "vendor_name", "created_at"]] if not df_v.empty else pd.DataFrame(), hide_index=True, use_container_width=True)

    with tabs[3]:
        st.header("🔑 Reset Password User")
        st.caption("Reset password untuk PIC maupun Vendor yang lupa password atau butuh password baru.")

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
            if mode == "Ketik manual":
                new_pw = st.text_input("Password baru (min. 6 karakter):", type="password")
            else:
                new_pw = None

            if st.button("🔄 Reset Password", type="primary"):
                import random, string
                final_pw = new_pw if mode == "Ketik manual" else "".join(random.choices(string.ascii_letters + string.digits, k=10))

                if mode == "Ketik manual" and (not new_pw or len(new_pw) < 6):
                    st.error("❌ Password minimal 6 karakter.")
                else:
                    ok, err = reset_user_password(sel_row["id"], final_pw)
                    if ok:
                        st.success(f"🎉 Password untuk **{sel_row['email']}** berhasil direset.")
                        st.info(f"🔑 Password baru: `{final_pw}` — catat & kirim manual ke user ybs, ini cuma muncul sekali.")
                    else:
                        st.error(f"❌ Gagal: {err}")


# =====================================================================
# UI: VENDOR
# =====================================================================
def vendor_portal(vendor_id):
    st.header("📝 Form Penawaran Harga")

    with st.expander("⚙️ Data Vendor Saya (Term of Payment)"):
        prof = sb.table("profiles").select("top_days").eq("id", vendor_id).single().execute()
        current_top = (prof.data or {}).get("top_days") or 0
        new_top = st.number_input("TOP / Term of Payment (hari)", min_value=0, value=int(current_top), step=1)
        if st.button("Simpan TOP"):
            update_vendor_top(vendor_id, new_top)
            st.success("TOP berhasil disimpan.")
            st.rerun()

    assignments = get_vendor_assignments(vendor_id)
    if not assignments:
        st.info("Tidak ada permintaan RFQ untuk Anda.")
        return

    pr_groups = {}
    for a in assignments:
        item = a.get("pr_items") or {}
        pr = item.get("purchase_requests") or {}
        pr_code = pr.get("pr_code", "-")
        pr_groups.setdefault(pr_code, {"pr_id": pr.get("id"), "rows": []})
        pr_groups[pr_code]["rows"].append(a)

    for pr_code, group in pr_groups.items():
        with st.expander(f"📋 PR: {pr_code}", expanded=True):
            rows = group["rows"]
            st.markdown(f"**🚚 Metode Pengiriman:** {rows[0].get('delivery_type','-')} | **📝 Catatan:** {rows[0].get('pic_notes','-')}")

            attachments = get_pr_attachments(group["pr_id"]) if group["pr_id"] else []
            if attachments:
                st.markdown("**📎 File referensi:** " + ", ".join(a["file_name"] for a in attachments))

            table_rows = []
            for a in rows:
                item = a.get("pr_items") or {}
                table_rows.append({
                    "assignment_id": a["id"],
                    "description": item.get("description"),
                    "description2": item.get("description2"),
                    "qty": item.get("quantity"),
                    "uom": item.get("uom"),
                    "Unit_Price": 0.0,
                    "Brand": "-",
                    "Ready_Stock": "Ya",
                    "Lead_Time_Days": 7,
                })
            df_form = pd.DataFrame(table_rows)
            edited = st.data_editor(
                df_form, key=f"edit_{pr_code}", hide_index=True, use_container_width=True,
                disabled=["assignment_id", "description", "description2", "qty", "uom"],
                column_config={
                    "Ready_Stock": st.column_config.SelectboxColumn("Ready Stock", options=["Ya", "Tidak"], required=True),
                },
            )

            if st.button(f"Kirim Penawaran PR {pr_code}", key=f"save_{pr_code}"):
                for _, r in edited.iterrows():
                    submit_quote(r["assignment_id"], vendor_id, r["Unit_Price"], r["Brand"], r["Lead_Time_Days"], r["Ready_Stock"])
                st.success(f"🎉 Penawaran untuk PR {pr_code} berhasil dikirim!")
                st.rerun()


def combined_admin_portal():
    """Dipakai untuk role 'admin' yang JUGA merangkap kerjaan PIC (misal: Phoebe)."""
    all_tabs = st.tabs([
        "📥 Import PR List", "📊 Monitoring & Comparison", "🔍 History",
        "➕ Daftarkan PIC", "➕ Daftarkan Vendor", "👥 Daftar User", "🔑 Reset Password",
    ])
    proc_portal(tabs=all_tabs[0:3])
    admin_portal(tabs=all_tabs[3:7])


# =====================================================================
# MAIN
# =====================================================================
def main():
    if "user_info" not in st.session_state:
        st.session_state["user_info"] = None
    if "selected_items_dict" not in st.session_state:
        st.session_state["selected_items_dict"] = {}

    if st.session_state["user_info"] is None:
        show_login()
        return

    user = st.session_state["user_info"]
    col_u, col_lo = st.columns([6, 1])
    with col_u:
        st.title(f"👋 Welcome, **{user.get('vendor_name') or user.get('email')}**")
    with col_lo:
        if st.button("Log Out"):
            st.session_state["user_info"] = None
            st.session_state["selected_items_dict"] = {}
            st.rerun()
    st.divider()

    if user["role"] == "admin":
        combined_admin_portal()
    elif user["role"] == "proc":
        proc_portal()
    else:
        vendor_portal(user["id"])


if __name__ == "__main__":
    main()
