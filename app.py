import streamlit as st
import pandas as pd
from supabase import create_client, Client
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

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
           url = st.secrets["supabase"]["url"]
           key = st.secrets["supabase"]["service_role_key"]
           temp_client = create_client(url, key)
           auth_res = temp_client.auth.sign_in_with_password({"email": email, "password": password})
           uid = auth_res.user.id
           prof = sb.table("profiles").select("*").eq("id", uid).single().execute()
           return prof.data
       except Exception as e:
           st.error(f"DEBUG ERROR: {e}")
           return None


def register_vendor(name, email, password):
    try:
        existing = sb.table("profiles").select("id").eq("email", email).execute()
        if existing.data:
            return False, "Email sudah terdaftar."
        created = sb.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        )
        uid = created.user.id
        sb.table("profiles").insert(
            {"id": uid, "email": email, "role": "vendor", "vendor_name": name}
        ).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def get_vendors():
    res = sb.table("profiles").select("*").eq("role", "vendor").execute()
    return pd.DataFrame(res.data)


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
        .select("unit_price, brand, lead_time_days, rfq_assignments(pr_items(description, description2, quantity, uom, purchase_requests(pr_code)), profiles(vendor_name, email))")
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
            }
        )
    return pd.DataFrame(rows)


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


def submit_quote(assignment_id, vendor_id, unit_price, brand, lead_time_days):
    sb.table("quotes").insert(
        {
            "assignment_id": assignment_id,
            "vendor_id": vendor_id,
            "unit_price": unit_price,
            "brand": brand,
            "lead_time_days": lead_time_days,
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
# UI: LOGIN
# =====================================================================
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
# UI: ADMIN
# =====================================================================
def admin_portal():
    tabs = st.tabs(["📥 Import PR List", "📊 Monitoring & Comparison", "🔍 History", "➕ Register Vendor"])
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

                for pr_no in df_to_show["PR CODE"].unique():
                    df_group = df_to_show[df_to_show["PR CODE"] == pr_no].reset_index(drop=True)
                    loc = df_group["LOCATION"].iloc[0] if "LOCATION" in df_group.columns else "-"
                    prio = str(df_group["PRIORITY STATUS"].iloc[0]) if "PRIORITY STATUS" in df_group.columns else "-"
                    label = f"📄 PR: {pr_no} | 📍 {loc}" + (" | 🚨 URGENT" if "URGENT" in prio.upper() else "")

                    with st.expander(label, expanded=False):
                        cA, cB, _ = st.columns([1, 1, 3])
                        if cA.button("✅ Pilih Semua", key=f"all_{pr_no}"):
                            for k in df_group["ROW_KEY"]:
                                st.session_state["selected_items_dict"][k] = True
                            st.rerun()
                        if cB.button("🗑️ Hapus Semua", key=f"none_{pr_no}"):
                            for k in df_group["ROW_KEY"]:
                                st.session_state["selected_items_dict"][k] = False
                            st.rerun()

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
        st.header("Price Comparison Analysis")
        df_prices = get_price_comparison_data()
        if df_prices.empty:
            st.info("Belum ada penawaran masuk dari vendor.")
        else:
            pr_list = df_prices["pr_code"].dropna().unique()
            sel_pr = st.selectbox("Pilih Nomor PR:", pr_list)
            sub = df_prices[df_prices["pr_code"] == sel_pr]
            pivot = sub.pivot_table(index=["description", "description2", "qty", "uom"], columns="vendor", values="unit_price", aggfunc="min").reset_index()
            id_cols = ["description", "description2", "qty", "uom"]
            price_cols = [c for c in pivot.columns if c not in id_cols]
            if price_cols:
                st.dataframe(pivot.style.highlight_min(axis=1, color="#d1fae5", subset=price_cols), use_container_width=True)
            else:
                st.dataframe(pivot, use_container_width=True)

    with tabs[2]:
        st.header("🔍 History RFQ")
        df_hist = get_history_data()
        if df_hist.empty:
            st.info("Belum ada riwayat publikasi.")
        else:
            st.dataframe(df_hist, hide_index=True, use_container_width=True)

    with tabs[3]:
        st.header("➕ Daftarkan Vendor Baru")
        with st.form("form_register_vendor", clear_on_submit=True):
            v_name = st.text_input("Nama Vendor").strip()
            v_email = st.text_input("Email Vendor").strip().lower()
            auto_password = datetime.now().strftime("%Y%m%d")
            st.info(f"🔑 Password akun vendor otomatis: `{auto_password}`")
            submitted = st.form_submit_button("Simpan Vendor Baru", type="primary")
            if submitted:
                if not v_name or not v_email or "@" not in v_email:
                    st.error("❌ Nama/Email tidak valid.")
                else:
                    ok, err = register_vendor(v_name, v_email, auto_password)
                    if ok:
                        st.success(f"🎉 Vendor {v_name} berhasil didaftarkan.")
                    else:
                        st.error(f"❌ Gagal: {err}")


# =====================================================================
# UI: VENDOR
# =====================================================================
def vendor_portal(vendor_id):
    st.header("📝 Form Penawaran Harga")
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
                    "Lead_Time_Days": 7,
                })
            df_form = pd.DataFrame(table_rows)
            edited = st.data_editor(
                df_form, key=f"edit_{pr_code}", hide_index=True, use_container_width=True,
                disabled=["assignment_id", "description", "description2", "qty", "uom"],
            )

            if st.button(f"Kirim Penawaran PR {pr_code}", key=f"save_{pr_code}"):
                for _, r in edited.iterrows():
                    submit_quote(r["assignment_id"], vendor_id, r["Unit_Price"], r["Brand"], r["Lead_Time_Days"])
                st.success(f"🎉 Penawaran untuk PR {pr_code} berhasil dikirim!")
                st.rerun()


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
        admin_portal()
    else:
        vendor_portal(user["id"])


if __name__ == "__main__":
    main()
