import io
import random
import string
import secrets as pysecrets
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
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.oxml.ns import nsdecls, qn
from docx.oxml import parse_xml, OxmlElement
# =====================================================================
# CONFIG
# =====================================================================
st.set_page_config(page_title="TACO Procurement", layout="wide", page_icon="🏢")
BUCKET_NAME = "rfq-attachments"

# Pilihan "Prioritas Pemilihan Vendor" di halaman Price Comparison
PRIO_ITEM = "Termurah per Item"
PRIO_TOTAL = "Total Termurah (1 PO)"
PRIO_LEAD = "Lead Time Tercepat"
PRIO_COMBO = "Kombinasi Bobot Skor (Default)"
PRIO_SPLIT = "Split Qty (Bagi Qty per Item)"

# Aturan penandatangan SPK dari sisi TACO:
# total SPK <= batas ini -> Manager PIC ybs, di atasnya -> Chief PIC ybs
SPK_APPROVAL_LIMIT = 50_000_000


@st.cache_resource
def get_client() -> Client:
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["service_role_key"]
    return create_client(url, key)


sb = get_client()


def clean(s):
    """Trim spasi & normalisasi None/NaN jadi string kosong.
    Dipakai di SEMUA input teks (email, nama, deskripsi, dll) biar konsisten."""
    if s is None:
        return ""
    try:
        if pd.isna(s):
            return ""
    except (TypeError, ValueError):
        pass
    return str(s).strip()
    
def _safe_filename(s, max_len=60):
    """Bersihkan teks buat nama file: buang karakter terlarang, spasi jadi underscore."""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", clean(s))
    s = re.sub(r"\s+", "_", s).strip("._")
    return s[:max_len] or "RFQ"

def scroll_to_top():
    """Paksa halaman scroll ke atas (Streamlit default-nya mempertahankan posisi scroll)."""
    import streamlit.components.v1 as components
    import time
    components.html(
        f"""
        <script>
        // run-id: {time.time()}
        const doc = window.parent.document;
        const go = () => {{
            [doc.querySelector('[data-testid="stMain"]'),
             doc.querySelector('section.main'),
             doc.querySelector('.main'),
             doc.documentElement].forEach(el => {{
                if (el) el.scrollTo({{top: 0, behavior: 'instant'}});
            }});
        }};
        go(); setTimeout(go, 150); setTimeout(go, 500);
        </script>
        """,
        height=0,
    )


# =====================================================================
# GEMINI MODEL FALLBACK HELPER
# Google kadang "pensiunkan" nama model lama (contoh: gemini-2.5-flash
# sempat dibilang "no longer available to new users"). Daripada app
# error total, kita coba beberapa nama model berurutan sampai ada yang
# jalan -- jadi kalau Google ganti nama model lagi di masa depan, cukup
# update list ini di satu tempat.
# =====================================================================
GEMINI_MODEL_CANDIDATES = [
    "gemini-2.5-flash-lite",       # paling cepat, taruh duluan
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-flash-latest",
]


@st.cache_resource
def _gemini_model_cache():
    """Dict persisten antar rerun: simpan nama model terakhir yang berhasil."""
    return {}


def call_gemini_with_fallback(build_and_call_fn, candidates=None, cache_key="default"):
    candidates = list(candidates or GEMINI_MODEL_CANDIDATES)
    cache = _gemini_model_cache()

    # Model yang terakhir berhasil dicoba PERTAMA -> gak buang waktu di model mati
    last_ok = cache.get(cache_key)
    if last_ok in candidates:
        candidates.remove(last_ok)
        candidates.insert(0, last_ok)

    last_err = None
    for name in candidates:
        try:
            res = build_and_call_fn(name)
            cache[cache_key] = name
            return res, None
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Kuota habis: ganti model gak ngebantu, langsung stop
            if "429" in msg or "quota" in msg:
                break
            continue
    return None, str(last_err)


# =====================================================================
# AUTH
# =====================================================================
def login(email, password):
    """Return (profile_dict_or_None, error_code_or_None).
    error_code: None (sukses), 'rate_limited', 'invalid', atau 'unknown'."""
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
        return prof.data, None
    except Exception as e:
        msg = str(e).lower()
        if "rate limit" in msg or "429" in msg or "too many" in msg or "request rate" in msg:
            return None, "rate_limited"
        if "invalid" in msg or "credential" in msg:
            return None, "invalid"
        return None, "unknown"


# =====================================================================
# SESSION PERSISTENCE (biar gak logout kalau tab diem lama / reconnect)
# =====================================================================
SESSION_DAYS_VALID = 7


def create_session(user_id):
    """Bikin token login baru & simpan ke DB. Return token string, atau None kalau gagal."""
    token = pysecrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS_VALID)).isoformat()
    try:
        sb.table("user_sessions").insert(
            {"user_id": user_id, "token": token, "expires_at": expires}
        ).execute()
        return token
    except Exception:
        return None


def get_session_user(token):
    """Validasi token dari URL & kembalikan profile user kalau masih berlaku."""
    if not token:
        return None
    try:
        res = sb.table("user_sessions").select("*, profiles(*)").eq("token", token).execute()
        if not res.data:
            return None
        sess = res.data[0]
        exp = sess.get("expires_at")
        if exp:
            try:
                exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).replace(tzinfo=None)
                if exp_dt < datetime.now():
                    return None
            except Exception:
                pass
        return sess.get("profiles")
    except Exception:
        return None


def delete_session(token):
    if not token:
        return
    try:
        sb.table("user_sessions").delete().eq("token", token).execute()
    except Exception:
        pass


# =====================================================================
# AUTH & REGISTRATION
# =====================================================================
def send_pic_welcome_email(pic_name, pic_email, password):
    """Kirim email pemberitahuan akun baru khusus untuk PIC Procurement."""
    if "email_config" not in st.secrets:
        return False
    sender_email = st.secrets["email_config"].get("smtp_user", "")
    sender_password = st.secrets["email_config"].get("smtp_password", "")
    if not sender_password or not sender_email:
        return False

    subject = "🎉 Akun Portal Procurement TACO Telah Aktif"
    body = (
        f"Dear {pic_name},\n\n"
        f"Akun Anda untuk Portal TACO Procurement telah berhasil dibuat.\n\n"
        f"🌐 Link Portal: https://proctaco.streamlit.app/\n"
        f"👤 Username/Email: {pic_email}\n"
        f"🔑 Password: {password}\n\n"
        f"Silakan login ke portal untuk mengelola RFQ.\n\n"
        f"Salam,\nTACO Procurement Team"
    )

    try:
        msg = MIMEMultipart()
        msg["From"] = sender_email
        msg["To"] = pic_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, pic_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        st.warning(f"⚠️ Gagal mengirim email login ke PIC {pic_email}: {e}")
        return False


def register_user(name, email_input, password, role, vendor_code="-", manager_name="", chief_name="", manager_title="", chief_title=""):
    try:
        name = clean(name)
        vendor_code = clean(vendor_code) or "-"
        raw_email_str = clean(email_input)
        emails_list = [clean(e).lower() for e in raw_email_str.split(";") if clean(e)]
        if not emails_list:
            return False, "Email tidak valid."

        primary_email = emails_list[0]
        normalized_email_str = "; ".join(emails_list)

        existing = sb.table("profiles").select("id").eq("email", primary_email).execute()
        if existing.data:
            return False, f"Email utama ({primary_email}) sudah terdaftar."

        created = sb.auth.admin.create_user(
            {"email": primary_email, "password": password, "email_confirm": True}
        )
        uid = created.user.id

        profile_payload = {
            "id": uid,
            "email": normalized_email_str,
            "role": role,
            "vendor_name": name,
            "vendor_code": vendor_code,
        }
        if role == "vendor":
            profile_payload["credentials_sent"] = False

        sb.table("profiles").insert(profile_payload).execute()

        if role == "proc" and any(clean(x) for x in (manager_name, chief_name, manager_title, chief_title)):
            save_pic_approvers(uid, manager_name, manager_title, chief_name, chief_title)

        if role == "proc":
            send_pic_welcome_email(name, primary_email, password)

        return True, None
    except Exception as e:
        return False, str(e)


def bulk_register_users(df, role):
    df.columns = [clean(c).lower() for c in df.columns]
    results = []
    for _, row in df.iterrows():
        name = clean(row.get("name", ""))
        email = clean(row.get("email", "")).lower()
        v_code = clean(row.get("code", row.get("vendor_code", "-")))
        mgr = clean(row.get("manager", row.get("manager_name", "")))
        chf = clean(row.get("chief", row.get("chief_name", "")))
        mgr_t = clean(row.get("manager_title", row.get("jabatan_manager", "")))
        chf_t = clean(row.get("chief_title", row.get("jabatan_chief", "")))

        if not name or not email or "@" not in email:
            results.append({"name": name, "code": v_code, "email": email, "password": "-", "status": "❌ Data tidak valid"})
            continue

        password = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        ok, err = register_user(name, email, password, role, vendor_code=v_code, manager_name=mgr, chief_name=chf, manager_title=mgr_t, chief_title=chf_t)
        if ok:
            results.append({"name": name, "code": v_code, "email": email, "password": password, "status": "✅ Berhasil"})
        else:
            results.append({"name": name, "code": v_code, "email": email, "password": "-", "status": f"❌ {err}"})

    return pd.DataFrame(results)


def send_rfq_email(vendor_email_str, vendor_name, rfq_title, deadline_str, items_text,
                   delivery_type, pic_notes, files, vendor_password=None, items_df=None):
    """Kirim email RFQ ke SELURUH email vendor (bisa multi-email dipisah ';').
    Kalau items_df diberikan -> daftar item tampil sebagai TABEL (HTML),
    items_text tetap dipakai sebagai versi teks biasa (fallback)."""
    from html import escape as h

    if "email_config" not in st.secrets:
        st.error("Konfigurasi 'email_config' tidak ditemukan di st.secrets")
        return False

    sender_email = st.secrets["email_config"].get("smtp_user", "")
    sender_password = st.secrets["email_config"].get("smtp_password", "")
    if not sender_password:
        st.error("'smtp_password' masih kosong di st.secrets")
        return False

    recipients = [clean(e).lower() for e in str(vendor_email_str).split(";") if clean(e)]
    if not recipients:
        return False

    primary_email = recipients[0]
    subject = f"Request for Quotation - TACO - {datetime.now().strftime('%d %b %Y')}"
    notes_txt = pic_notes if pic_notes else "-"

    # ---------------- versi teks biasa (fallback) ----------------
    login_info_txt = (
        f"\n🔐 Info Login Portal Anda:\n"
        f"Username (Email Utama): {primary_email}\n"
        f"Password: {vendor_password}\n"
        f"(Mohon simpan baik-baik password ini untuk login ke portal)\n\n"
        if vendor_password else ""
    )
    body_txt = (
        f"Dear {vendor_name},\n\n"
        f"Kami mengundang Anda untuk mengisi Request for Quotation (RFQ):\n\n"
        f"Judul RFQ: {rfq_title}\n"
        f"Batas Waktu Pengisian: {deadline_str}\n"
        f"Metode Pengiriman: {delivery_type}\n"
        f"Catatan Tambahan PIC: {notes_txt}\n\n"
        f"Daftar Item:\n{items_text}\n\n"
        f"{login_info_txt}"
        f"Silakan login ke portal: https://proctaco.streamlit.app/\n\n"
        f"Video Tutorial Penggunaan Web: https://bit.ly/RFQtaco\n\n"
        f"Salam,\nTACO Procurement Team"
    )

    # ---------------- versi HTML (tabel item) ----------------
    th = "border:1px solid #cbd5e1;padding:6px 8px;background:#ED7D31;color:#fff;font-size:13px;"
    td = "border:1px solid #cbd5e1;padding:6px 8px;font-size:13px;vertical-align:top;"

    if items_df is not None and len(items_df) > 0:
        rows_html = ""
        for i, (_, r) in enumerate(items_df.iterrows(), start=1):
            d1 = clean(r.get("DESCRIPTION", ""))
            d2 = clean(r.get("DESCRIPTION_2", ""))
            desc = f"{d1} - {d2}" if (d1 and d2 and d1 != d2) else (d1 or d2 or "-")
            qty = r.get("QUANTITY", "")
            try:
                qf = float(qty)
                qty = str(int(qf)) if qf == int(qf) else f"{qf:g}"
            except Exception:
                qty = clean(qty)
            note = clean(r.get("CATATAN_BARIS_ATAU_LINK_GAMBAR", "")) or "-"
            rows_html += (
                "<tr>"
                f"<td style='{td}text-align:center;'>{i}</td>"
                f"<td style='{td}'>{h(clean_description(desc))}</td>"
                f"<td style='{td}text-align:center;'>{h(qty)}</td>"
                f"<td style='{td}text-align:center;'>{h(clean(r.get('UOM', '')))}</td>"
                f"<td style='{td}'>{h(note)}</td>"
                "</tr>"
            )
        items_html = (
            "<table style='border-collapse:collapse;width:100%;max-width:720px;'>"
            "<thead><tr>"
            f"<th style='{th}width:36px;'>No</th>"
            f"<th style='{th}text-align:left;'>Nama Barang</th>"
            f"<th style='{th}width:60px;'>Qty</th>"
            f"<th style='{th}width:70px;'>UOM</th>"
            f"<th style='{th}text-align:left;'>Catatan</th>"
            f"</tr></thead><tbody>{rows_html}</tbody></table>"
        )
    else:
        items_html = f"<pre style='font-family:inherit;'>{h(items_text)}</pre>"

    login_html = (
        "<div style='background:#fff7ed;border:1px solid #fdba74;border-radius:6px;"
        "padding:10px 12px;margin:14px 0;max-width:720px;'>"
        "<b>🔐 Info Login Portal Anda</b><br>"
        f"Username (Email Utama): <b>{h(primary_email)}</b><br>"
        f"Password: <b>{h(str(vendor_password))}</b><br>"
        "<span style='font-size:12px;color:#555;'>(Mohon simpan baik-baik password ini untuk login ke portal)</span>"
        "</div>"
        if vendor_password else ""
    )

    body_html = f"""
<html><body style="font-family:Calibri,Arial,sans-serif;font-size:14px;color:#111;">
<p>Dear {h(vendor_name)},</p>
<p>Kami mengundang Anda untuk mengisi Request for Quotation (RFQ):</p>
<table style="border-collapse:collapse;margin-bottom:12px;">
  <tr><td style="padding:2px 12px 2px 0;"><b>Judul RFQ</b></td><td>: {h(rfq_title)}</td></tr>
  <tr><td style="padding:2px 12px 2px 0;"><b>Batas Waktu Pengisian</b></td><td>: {h(deadline_str)}</td></tr>
  <tr><td style="padding:2px 12px 2px 0;"><b>Metode Pengiriman</b></td><td>: {h(delivery_type)}</td></tr>
  <tr><td style="padding:2px 12px 2px 0;vertical-align:top;"><b>Catatan Tambahan PIC</b></td>
      <td>: {h(notes_txt).replace(chr(10), '<br>')}</td></tr>
</table>
<p><b>Daftar Item:</b></p>
{items_html}
{login_html}
<p>Silakan login ke portal: <a href="https://proctaco.streamlit.app/">https://proctaco.streamlit.app/</a></p>
<p>Video Tutorial Penggunaan Web: <a href="https://bit.ly/RFQtaco">https://bit.ly/RFQtaco</a></p>
<p>Salam,<br>TACO Procurement Team</p>
</body></html>
"""

    try:
        msg = MIMEMultipart("mixed")
        msg["From"] = sender_email
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_txt, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))   # yang terakhir = yang diprioritaskan client email
        msg.attach(alt)

        for f in files:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.getvalue())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={f.name}")
            msg.attach(part)

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipients, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        st.warning(f"⚠️ Notifikasi email gagal terkirim ke {vendor_email_str}: {e}")
        return False


def send_custom_email(recipients_str, subject, body_text, pdf_attachments=None):
    """
    Kirim email kustom (Awarding / Thank You) dengan atau tanpa lampiran file (.docx / .pdf).
    """
    if "email_config" not in st.secrets:
        st.error("Konfigurasi 'email_config' tidak ditemukan di st.secrets")
        return False

    sender_email = st.secrets["email_config"].get("smtp_user", "")
    sender_password = st.secrets["email_config"].get("smtp_password", "")
    if not sender_password or not sender_email:
        st.error("'smtp_password' atau 'smtp_user' masih kosong di st.secrets")
        return False

    recipients = [clean(e).lower() for e in str(recipients_str).split(";") if clean(e)]
    if not recipients:
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = sender_email
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))

        if pdf_attachments:
            for file_name, file_bytes in pdf_attachments:
                if file_bytes:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(file_bytes)
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={file_name}")
                    msg.attach(part)

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipients, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        st.warning(f"⚠️ Notifikasi email gagal terkirim ke {recipients_str}: {e}")
        return False


def get_users_by_role(role):
    res = sb.table("profiles").select("*").eq("role", role).execute()
    return pd.DataFrame(res.data)


def reset_user_password(user_id, new_password):
    try:
        sb.auth.admin.update_user_by_id(user_id, {"password": new_password})
        return True, None
    except Exception as e:
        return False, str(e)


def mark_credentials_delivered(vendor_id):
    """Tandai vendor ini sudah pernah menerima info login (email+password) via undangan RFQ.
    Sekali dapat, dia gak akan di-reset otomatis lagi tiap ada RFQ baru -- supaya passwordnya
    konsisten walau ada beberapa RFQ jalan berbarengan / dari PIC berbeda."""
    try:
        sb.table("profiles").update({"credentials_sent": True}).eq("id", vendor_id).execute()
        return True
    except Exception:
        return False


def get_vendors():
    return get_users_by_role("vendor")


@st.cache_data(ttl=300, show_spinner=False)
def get_vendors_cached():
    return get_vendors()


# =====================================================================
# PR & ITEMS
# =====================================================================
def get_or_create_pr(pr_code, location, priority, uploaded_by):
    pr_code = clean(pr_code)
    location = clean(location)
    priority = clean(priority)
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
    description = clean(description)
    description2 = clean(description2)
    uom = clean(uom)
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
        keys.add((clean(item.get("description", "")).lower(), clean(item.get("description2", "")).lower()))
    return keys


@st.cache_data(ttl=300, show_spinner=False)
def get_already_published_keys_cached():
    return get_already_published_keys()


# =====================================================================
# UI HELPER: reset checkbox item (dipakai proc_portal_import)
# =====================================================================
def reset_checkbox_selection(df=None):
    """Set FLAG dulu -- reset asli baru dieksekusi di awal render berikutnya
    (render_import_workspace), SEBELUM checkbox digambar. Kalau reset langsung
    di sini, bisa bentrok sama checkbox yang sudah kadung diinstansiasi di run
    yang sama -> StreamlitWidgetAlreadyInstantiatedError."""
    st.session_state["_pending_selection_reset"] = True


# =====================================================================
# PUBLISH RFQ
# =====================================================================
def publish_rfq(rfq_title, pr_code, location, priority, admin_id, items_df, vendor_ids,
                delivery_type, pic_notes, deadline, files, shipment_mode="Langsung (Sekaligus)"):
    rfq_title = clean(rfq_title)
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
            row.get("DESCRIPTION", ""),
            row.get("DESCRIPTION_2", ""),
            row.get("QUANTITY", 0),
            row.get("UOM", ""),
        )
        for v_id in vendor_ids:
            sb.table("rfq_assignments").upsert(
                {
                    "item_id": item_id,
                    "vendor_id": v_id,
                    "delivery_type": delivery_type,
                    "shipment_mode": shipment_mode,
                    "pic_notes": pic_notes,
                    "line_note": clean(row.get("CATATAN_BARIS_ATAU_LINK_GAMBAR", "-")) or "-",
                    "deadline": str(deadline),
                    "status": "Open",
                },
                on_conflict="item_id,vendor_id",
            ).execute()

    # Data berubah -> buang cache lama biar next baca fresh
    get_already_published_keys_cached.clear()
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


def compute_recommendation(df_item, w_price, w_top, w_leadtime):
    """
    df_item: baris-baris quote untuk SATU item (dari beberapa vendor).
    Mengembalikan df_item + kolom 'score' (0-100) + 'is_recommended'.
    Normalisasi per-item: harga makin murah makin baik, TOP makin panjang makin baik,
    lead time makin pendek makin baik. (Ready stock TIDAK ikut skor, hanya ditampilkan di tabel.)
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

    total_w = max(w_price + w_top + w_leadtime, 1)
    d["score"] = (price_score * w_price + top_score * w_top + leadtime_score * w_leadtime) / total_w
    d["score"] = d["score"].round(1)
    d["is_recommended"] = d["score"] == d["score"].max()
    return d


# =====================================================================
# TEMPLATE EMAIL AWARDING & THANK YOU
# =====================================================================
DEFAULT_AWARDING_EMAIL_TEMPLATE = """Dear Tim {vendor_name},

Berdasarkan hasil evaluasi untuk RFQ: {rfq_title}, perusahaan Anda dinyatakan terpilih sebagai PEMENANG TENDER.

Total Nominal: Rp {total_amount}

Mohon dapat mengecek dan menandatangani lampiran Surat Perintah Kerja yang telah kami lampirkan, dan dapat segera mempersiapkan proses pengiriman.

Terima kasih atas kerja samanya.

Salam,
TACO Procurement Team
"""

DEFAULT_THANKYOU_EMAIL_TEMPLATE = """Dear Tim {vendor_name},

Terima kasih atas partisipasi Anda dalam penawaran harga untuk RFQ: {rfq_title}.

Melalui surat ini kami menginformasikan bahwa untuk paket pengadaan kali ini, panitia procurement telah memilih penyedia jasa/barang lainnya yang lebih sesuai dengan kriteria evaluasi kami.

Kami sangat mengapresiasi waktu dan penawaran yang telah Anda berikan, dan berharap dapat bekerja sama di paket pengadaan berikutnya.

Salam,
TACO Procurement Team
"""


def update_vendor_profile_info(vendor_id, top_days, pic_name, pic_jabatan):
    sb.table("profiles").update({
        "top_days": top_days,
        "pic_name": clean(pic_name),
        "pic_jabatan": clean(pic_jabatan),
    }).eq("id", vendor_id).execute()
    get_vendors_cached.clear()
def get_supplier_completeness(vendor_id):
    """Return (lengkap: bool, daftar_field_kosong: list)."""
    try:
        p = sb.table("profiles").select("top_days, pic_name, pic_jabatan").eq("id", vendor_id).single().execute().data or {}
    except Exception:
        p = {}
    missing = []
    if p.get("top_days") is None:
        missing.append("TOP / Term of Payment")
    if not clean(p.get("pic_name")):
        missing.append("Nama PIC / Penandatangan")
    if not clean(p.get("pic_jabatan")):
        missing.append("Jabatan")
    return (not missing), missing

def get_history_data():
    """History RFQ: HANYA RFQ yang sudah di-close (is_archived = True)."""
    res = (
        sb.table("rfq_assignments")
        .select("status, deadline, delivery_type, shipment_mode, created_at, pr_items(description, description2, quantity, uom, purchase_requests(pr_code, location, rfq_title, is_archived)), profiles(vendor_name, email)")
        .execute()
    )
    rows = []
    for r in res.data:
        item = r.get("pr_items") or {}
        pr = item.get("purchase_requests") or {}
        if not pr.get("is_archived"):
            continue  # hanya RFQ yang sudah di-close
        vendor = r.get("profiles") or {}
        rows.append(
            {
                "Judul RFQ": pr.get("rfq_title") or pr.get("pr_code"),
                "PR Code": pr.get("pr_code"),
                "Lokasi": pr.get("location"),
                "Barang": clean_description(item.get("description")),
                "Qty": item.get("quantity"),
                "UOM": item.get("uom"),
                "Vendor": vendor.get("vendor_name"),
                "Email Vendor": vendor.get("email"),
                "Metode Pengiriman": r.get("delivery_type"),
                "Jenis Pengiriman": r.get("shipment_mode") or "-",
                "Status": r.get("status"),
                "Deadline": r.get("deadline"),
                "Tanggal Dibuat": r.get("created_at"),
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


def get_vendors_for_pr(pr_id):
    """Dict {vendor_id: vendor_name} untuk vendor yang sudah di-assign ke RFQ ini."""
    res = (
        sb.table("rfq_assignments")
        .select("vendor_id, profiles(vendor_name, email), pr_items!inner(pr_id)")
        .eq("pr_items.pr_id", pr_id)
        .execute()
    )
    vendors = {}
    for r in res.data or []:
        vid = r.get("vendor_id")
        prof = r.get("profiles") or {}
        if vid and vid not in vendors:
            vendors[vid] = prof.get("vendor_name") or prof.get("email") or "Vendor"
    return vendors


def get_assignments_for_pr_vendor(pr_id, vendor_id):
    """Assignment + item + quote existing untuk kombinasi RFQ & vendor tertentu — dipakai PIC input manual."""
    res = (
        sb.table("rfq_assignments")
        .select("*, quotes(*), pr_items!inner(*)")
        .eq("vendor_id", vendor_id)
        .eq("pr_items.pr_id", pr_id)
        .execute()
    )
    return res.data


def get_pr_attachments(pr_id):
    res = sb.table("rfq_attachments").select("*").eq("pr_id", pr_id).execute()
    return res.data


def upload_vendor_document(pr_id, vendor_id, file, round_num=1):
    """Simpan dokumen resmi vendor. Hanya 1 file per (RFQ, vendor, tahap):
    tahap 1 = awal, tahap 2 = setelah nego. Upload ulang di tahap yang sama = replace."""
    try:
        stage = 1 if (round_num or 1) <= 1 else 2
        path = f"{pr_id}/vendor_docs/{vendor_id}/stage{stage}/{file.name}"
        sb.storage.from_(BUCKET_NAME).upload(
            path, file.getvalue(), {"content-type": file.type or "application/pdf", "upsert": "true"}
        )
        old = (
            sb.table("vendor_quote_documents").select("id, file_path")
            .eq("pr_id", pr_id).eq("vendor_id", vendor_id).eq("stage", stage).execute()
        ).data or []
        for o in old:
            if o.get("file_path") and o["file_path"] != path:
                try:
                    sb.storage.from_(BUCKET_NAME).remove([o["file_path"]])
                except Exception:
                    pass
        sb.table("vendor_quote_documents").delete().eq("pr_id", pr_id).eq("vendor_id", vendor_id).eq("stage", stage).execute()
        sb.table("vendor_quote_documents").insert(
            {"pr_id": pr_id, "vendor_id": vendor_id, "file_name": file.name, "file_path": path, "stage": stage}
        ).execute()
        get_storage_file_bytes.clear()
        return True
    except Exception as e:
        st.warning(f"Gagal upload dokumen: {e}")
        return False


@st.cache_data(ttl=600, show_spinner=False)
def get_storage_file_bytes(file_path):
    return sb.storage.from_(BUCKET_NAME).download(file_path)


def get_vendor_documents(pr_id, vendor_id=None):
    q = sb.table("vendor_quote_documents").select("*").eq("pr_id", pr_id)
    if vendor_id:
        q = q.eq("vendor_id", vendor_id)
    res = q.execute()
    return res.data


def submit_quote(assignment_id, vendor_id, unit_price, brand, lead_time_days, ready_stock, warranty="-", spec_vendor="-", round_num=1, vendor_ref_no=None, validity_period=None, tax_type=None):
    try:
        # Cek apakah sudah ada quote di ROUND yang sama untuk assignment ini
        existing = (
            sb.table("quotes")
            .select("id")
            .eq("assignment_id", assignment_id)
            .eq("vendor_id", vendor_id)
            .eq("round", round_num)
            .execute()
        )

        payload = {
            "unit_price": unit_price,
            "brand": clean(brand) or "-",
            "lead_time_days": lead_time_days,
            "ready_stock": ready_stock,
            "warranty": clean(warranty) or "-",
            "spec_vendor": clean(spec_vendor) or "-",
            "round": round_num,
        }
        if vendor_ref_no is not None:
            payload["vendor_ref_no"] = clean(vendor_ref_no) or "-"
        if validity_period is not None:
            payload["validity_period"] = clean(validity_period) or "-"
        if tax_type is not None:
            payload["tax_type"] = tax_type

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


def request_nego(pr_id, vendor_ids, note=""):
    """Naikkan current_round assignment vendor terpilih untuk RFQ ini & kirim email minta final quotation."""
    try:
        res_ass = (
            sb.table("rfq_assignments")
            .select("id, vendor_id, current_round, profiles(vendor_name, email), pr_items!inner(pr_id, description, description2)")
            .eq("pr_items.pr_id", pr_id)
            .in_("vendor_id", vendor_ids)
            .execute()
        )
        if not res_ass.data:
            return 0

        by_vendor = {}
        for ass in res_ass.data:
            by_vendor.setdefault(ass["vendor_id"], []).append(ass)

        notified = 0
        for v_id, rows in by_vendor.items():
            new_round = max((r.get("current_round") or 1) for r in rows) + 1
            ids = [r["id"] for r in rows]
            sb.table("rfq_assignments").update({"current_round": new_round}).in_("id", ids).execute()

            v_profile = rows[0].get("profiles") or {}
            v_email = v_profile.get("email")
            v_name = v_profile.get("vendor_name", "Vendor")
            if v_email and "email_config" in st.secrets:
                try:
                    subject = f"🤝 Permintaan Final Quotation (Nego) - {v_name}"
                    body = (
                        f"Dear {v_name},\n\n"
                        f"Mohon dapat mengirimkan Final Quotation (harga terbaik) untuk RFQ terkait.\n"
                        f"{('Catatan dari PIC: ' + note) if note else ''}\n\n"
                        f"Silakan login ke portal dan submit ulang penawaran Anda: https://proctaco.streamlit.app/\n\n"
                        f"Salam,\nTACO Procurement Team"
                    )
                    msg = MIMEMultipart()
                    msg["From"] = st.secrets["email_config"].get("smtp_user", "")
                    msg["To"] = v_email
                    msg["Subject"] = subject
                    msg.attach(MIMEText(body, "plain"))
                    server = smtplib.SMTP("smtp.gmail.com", 587)
                    server.starttls()
                    server.login(st.secrets["email_config"].get("smtp_user", ""), st.secrets["email_config"].get("smtp_password", ""))
                    server.sendmail(st.secrets["email_config"].get("smtp_user", ""), v_email, msg.as_string())
                    server.quit()
                    notified += 1
                except Exception:
                    pass
        return notified
    except Exception:
        return 0


# =====================================================================
# CUSTOM SPEC PARAMETERS (reusable master list — PIC bisa nambah sendiri,
# vendor tinggal pilih & isi value-nya). Berlaku per submission vendor
# (satu set spesifikasi tambahan untuk seluruh item di RFQ tsb per round).
# =====================================================================
@st.cache_data(ttl=300, show_spinner=False)
def get_spec_parameter_master():
    res = sb.table("spec_parameter_definitions").select("*").order("name").execute()
    return pd.DataFrame(res.data) if res.data else pd.DataFrame(columns=["id", "name"])


def get_or_create_spec_parameter(name):
    name = clean(name)
    if not name:
        return None
    existing = sb.table("spec_parameter_definitions").select("id").ilike("name", name).execute()
    if existing.data:
        return existing.data[0]["id"]
    new_row = sb.table("spec_parameter_definitions").insert({"name": name}).execute()
    get_spec_parameter_master.clear()
    return new_row.data[0]["id"]


def save_custom_specs(pr_id, vendor_id, round_num, specs_list):
    """specs_list: [{"parameter_id":.., "parameter_name":.., "value":..}, ...]
    Overwrite semua spesifikasi tambahan utk kombinasi pr_id+vendor_id+round ini."""
    try:
        sb.table("rfq_custom_specs").delete().eq("pr_id", pr_id).eq("vendor_id", vendor_id).eq("round", round_num).execute()
        rows = [
            {"pr_id": pr_id, "vendor_id": vendor_id, "round": round_num,
             "parameter_id": s["parameter_id"], "value": clean(s["value"])}
            for s in specs_list if s.get("parameter_id") and clean(s.get("value"))
        ]
        if rows:
            sb.table("rfq_custom_specs").insert(rows).execute()
        return True
    except Exception as e:
        st.warning(f"Gagal menyimpan spesifikasi tambahan: {e}")
        return False


def get_custom_specs(pr_id, vendor_id=None):
    q = sb.table("rfq_custom_specs").select("*, profiles(vendor_name), spec_parameter_definitions(name)").eq("pr_id", pr_id)
    if vendor_id:
        q = q.eq("vendor_id", vendor_id)
    res = q.execute()
    rows = []
    for r in res.data or []:
        rows.append({
            "vendor_id": r.get("vendor_id"),
            "vendor_name": (r.get("profiles") or {}).get("vendor_name", "-"),
            "round": r.get("round"),
            "parameter": (r.get("spec_parameter_definitions") or {}).get("name", "-"),
            "value": r.get("value"),
        })
    return rows


def render_custom_spec_editor(widget_key_prefix):
    """UI reusable: tambah/hapus baris parameter+value custom secara dinamis.
    Vendor pilih dari master list yang sudah ada, atau ketik nama baru (otomatis
    kesimpen ke master list biar bisa dipakai lagi di RFQ lain).
    Return list of {"parameter_id", "parameter_name", "value"} dari session_state."""
    state_key = f"custom_specs_{widget_key_prefix}"
    if state_key not in st.session_state:
        st.session_state[state_key] = []

    master_df = get_spec_parameter_master()
    master_names = master_df["name"].tolist() if not master_df.empty else []

    st.markdown("###### ➕ Spesifikasi Tambahan (Opsional — berlaku untuk semua item di RFQ ini)")
    st.caption("Pilih dari daftar yang sudah pernah dibuat, atau ketik nama baru untuk menambah pilihan ke daftar.")

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        param_choice = st.selectbox(
            "Parameter", ["-- Tambah Baru --"] + master_names, key=f"{widget_key_prefix}_param_choice"
        )
        new_param_name = ""
        if param_choice == "-- Tambah Baru --":
            new_param_name = clean(st.text_input("Nama Parameter Baru", key=f"{widget_key_prefix}_new_param"))
    with c2:
        spec_value = clean(st.text_input("Isi Value", key=f"{widget_key_prefix}_value"))
    with c3:
        st.write(" ")
        st.write(" ")
        if st.button("➕ Tambah", key=f"{widget_key_prefix}_add_btn", use_container_width=True):
            final_name = new_param_name if param_choice == "-- Tambah Baru --" else param_choice
            if final_name and spec_value:
                param_id = get_or_create_spec_parameter(final_name)
                st.session_state[state_key].append(
                    {"parameter_id": param_id, "parameter_name": final_name, "value": spec_value}
                )
                st.rerun(scope="fragment")
            else:
                st.warning("Isi nama parameter & value dulu.")

    if st.session_state[state_key]:
        for i, row in enumerate(st.session_state[state_key]):
            rc1, rc2, rc3 = st.columns([2, 2, 1])
            rc1.write(f"**{row['parameter_name']}**")
            rc2.write(row["value"])
            if rc3.button("🗑️", key=f"{widget_key_prefix}_del_{i}"):
                st.session_state[state_key].pop(i)
                st.rerun(scope="fragment")

    return st.session_state[state_key]


# =====================================================================
# MULTI-VENDOR SPLIT QTY (kriteria custom per item + bobot; ranking cuma
# bantu keputusan, persentase final tetap diisi manual oleh PIC)
# =====================================================================
def get_split_toggle_map(item_ids):
    if not item_ids:
        return {}
    res = sb.table("item_split_toggle").select("item_id, is_split_enabled").in_("item_id", item_ids).execute()
    return {r["item_id"]: bool(r.get("is_split_enabled")) for r in (res.data or [])}


def set_split_toggle(item_id, enabled):
    try:
        sb.table("item_split_toggle").upsert(
            {"item_id": item_id, "is_split_enabled": enabled}, on_conflict="item_id"
        ).execute()
    except Exception as e:
        st.warning(f"Gagal menyimpan toggle split: {e}")


def get_split_criteria(item_id):
    res = sb.table("item_split_criteria").select("*").eq("item_id", item_id).order("id").execute()
    return res.data or []


def add_split_criteria(item_id, name, weight):
    try:
        sb.table("item_split_criteria").insert(
            {"item_id": item_id, "criteria_name": clean(name), "weight": weight}
        ).execute()
        return True
    except Exception as e:
        st.warning(f"Gagal menambah kriteria: {e}")
        return False


def get_split_scores(item_id):
    res = sb.table("item_split_scores").select("*").eq("item_id", item_id).execute()
    scores = {}
    for r in res.data or []:
        scores[(r["vendor_id"], r["criteria_id"])] = r.get("score", 0)
    return scores


def save_split_score(item_id, vendor_id, criteria_id, score):
    try:
        sb.table("item_split_scores").upsert(
            {"item_id": item_id, "vendor_id": vendor_id, "criteria_id": criteria_id, "score": score},
            on_conflict="item_id,vendor_id,criteria_id",
        ).execute()
    except Exception as e:
        st.warning(f"Gagal menyimpan skor: {e}")


def get_split_allocation_map(item_ids):
    if not item_ids:
        return {}
    res = (
        sb.table("item_split_allocation")
        .select("item_id, final_percentage, profiles(id, vendor_name)")
        .in_("item_id", item_ids)
        .execute()
    )
    out = {}
    for r in res.data or []:
        prof = r.get("profiles") or {}
        out.setdefault(r["item_id"], []).append(
            {"vendor_id": prof.get("id"), "vendor_name": prof.get("vendor_name", "Vendor"), "percentage": float(r.get("final_percentage") or 0)}
        )
    return out


def save_split_allocation(item_id, allocations):
    """allocations: list of {"vendor_id":.., "percentage":..} - overwrite semua alokasi utk item ini."""
    try:
        sb.table("item_split_allocation").delete().eq("item_id", item_id).execute()
        rows = [
            {"item_id": item_id, "vendor_id": a["vendor_id"], "final_percentage": a["percentage"]}
            for a in allocations if a["percentage"] > 0 and a["vendor_id"]
        ]
        if rows:
            sb.table("item_split_allocation").insert(rows).execute()
        return True
    except Exception as e:
        st.warning(f"Gagal menyimpan alokasi split: {e}")
        return False


# =====================================================================
# SPLIT QTY WORKSPACE (DENGAN INPUT RANKING 1,2,3... & BOBOT %)
# Hanya dipanggil kalau Prioritas = "Split Qty"
# =====================================================================
def render_multivendor_split_workspace(pr_id, pivot_items, df_m, vendor_list_sorted, split_toggle_map):
    st.markdown("---")
    st.markdown("### ✂️ Split Qty — Bagi Qty per Item ke Beberapa Vendor")

    st.caption(
        "💡 Pilih item yang ingin dibagi kuantitasnya ke beberapa vendor. "
        "Masukkan **Bobot Kriteria (%)** dan **Ranking Vendor (1, 2, 3...)** untuk membantu menentukan alokasi % final."
    )

    for _, r in pivot_items.iterrows():
        item_id = r["item_id"]
        barang = r["Barang"]
        total_qty = r["Qty"] or 0
        rows_for_item = df_m[df_m["Barang"] == barang]
        vendors_for_item = sorted(rows_for_item["vendor"].unique().tolist())
        num_vendors = len(vendors_for_item)
        vendor_name_to_id = {row["vendor"]: row["vendor_id"] for _, row in rows_for_item.iterrows()}

        with st.expander(f"📦 Item: {barang} (Total Qty: {total_qty} {r['UOM']})"):
            is_split = st.checkbox(
                f"Split kuantitas untuk item '{barang}'",
                value=split_toggle_map.get(item_id, False),
                key=f"split_toggle_{item_id}",
            )
            if is_split != split_toggle_map.get(item_id, False):
                set_split_toggle(item_id, is_split)
                st.rerun(scope="fragment")

            if not is_split:
                continue

            # -------------------------------------------------------------
            # STEP 1: Kriteria & Bobot (%)
            # -------------------------------------------------------------
            st.markdown("**1️⃣ Kriteria Evaluasi & Bobot (%)**")
            criteria = get_split_criteria(item_id)
            if criteria:
                st.dataframe(
                    pd.DataFrame(criteria)[["criteria_name", "weight"]].rename(
                        columns={"criteria_name": "Kriteria / Parameter", "weight": "Bobot (%)"}
                    ),
                    hide_index=True, use_container_width=True,
                )

            cc1, cc2, cc3 = st.columns([2, 1, 1])
            new_crit_name = cc1.text_input("Nama Kriteria Baru (misal: Kualitas, Track Record)", key=f"new_crit_name_{item_id}")
            new_crit_weight = cc2.number_input("Bobot (%)", min_value=0, max_value=100, value=0, key=f"new_crit_weight_{item_id}")
            cc3.write(" ")
            cc3.write(" ")
            if cc3.button("➕ Tambah Kriteria", key=f"add_crit_{item_id}"):
                if new_crit_name:
                    add_split_criteria(item_id, new_crit_name, new_crit_weight)
                    st.rerun(scope="fragment")
                else:
                    st.warning("Isi nama kriteria terlebih dahulu.")

            if not criteria:
                st.info("Tambahkan minimal 1 kriteria penilaian di atas untuk mulai memasukkan ranking vendor.")
                continue

            # -------------------------------------------------------------
            # STEP 2: Input Ranking Vendor (1, 2, 3... Max = Jumlah Vendor)
            # -------------------------------------------------------------
            st.markdown(f"**2️⃣ Input Ranking Vendor per Kriteria (Rank 1 s/d {num_vendors})**")
            st.caption("🏆 **Rank 1** = Terbaik | **Rank 2** = Terbaik Kedua, dst.")

            existing_scores = get_split_scores(item_id)
            score_rows = []
            for v in vendors_for_item:
                v_id = vendor_name_to_id.get(v)
                row = {"Vendor": v}
                for c in criteria:
                    # Nilai di DB kita gunakan sebagai Rank (Default Rank 1)
                    saved_rank = int(existing_scores.get((v_id, c["id"]), 1))
                    row[c["criteria_name"]] = min(max(saved_rank, 1), num_vendors)
                score_rows.append(row)

            df_ranks = pd.DataFrame(score_rows)

            # Sediakan konfigurasi min/max rank di st.data_editor
            rank_col_config = {
                c["criteria_name"]: st.column_config.NumberColumn(
                    f"{c['criteria_name']} (Rank)",
                    help=f"Isi ranking 1 sampai {num_vendors}",
                    min_value=1,
                    max_value=num_vendors,
                    step=1
                ) for c in criteria
            }

            edited_ranks = st.data_editor(
                df_ranks,
                hide_index=True,
                use_container_width=True,
                disabled=["Vendor"],
                column_config=rank_col_config,
                key=f"rank_editor_{item_id}",
            )

            if st.button("💾 Simpan Ranking", key=f"save_ranks_{item_id}"):
                for _, srow in edited_ranks.iterrows():
                    v_id = vendor_name_to_id.get(srow["Vendor"])
                    for c in criteria:
                        rank_val = int(srow[c["criteria_name"]] or 1)
                        save_split_score(item_id, v_id, c["id"], rank_val)
                st.success("Ranking berhasil disimpan.")
                st.rerun(scope="fragment")

            # -------------------------------------------------------------
            # STEP 3: Kalkulasi Skor Terbobot & Rekomendasi Ranking
            # -------------------------------------------------------------
            total_weight = sum(c["weight"] for c in criteria) or 1
            ranking_rows = []
            for _, srow in edited_ranks.iterrows():
                # Konversi Rank ke Skor Sederhana (Rank 1 = 100, Rank 2 = 50, dst.) untuk pembobotan
                weighted_score = sum((100 / float(srow[c["criteria_name"]] or 1)) * c["weight"] for c in criteria) / total_weight
                ranking_rows.append({"Vendor": srow["Vendor"], "Skor Terbobot": round(weighted_score, 1)})

            df_ranking_calc = pd.DataFrame(ranking_rows).sort_values("Skor Terbobot", ascending=False).reset_index(drop=True)
            df_ranking_calc.index = df_ranking_calc.index + 1
            df_ranking_calc = df_ranking_calc.rename_axis("Hasil Ranking Vendor")

            st.markdown("**3️⃣ Hasil Kalkulasi Ranking Gabungan**")
            st.dataframe(df_ranking_calc[["Vendor", "Skor Terbobot"]], use_container_width=True)

            # -------------------------------------------------------------
            # STEP 4: Input Manual Persentase (%) Alokasi PO Final
            # -------------------------------------------------------------
            st.markdown("**4️⃣ Alokasi Persentase Split Qty Final (%)**")
            existing_alloc = {a["vendor_name"]: a["percentage"] for a in get_split_allocation_map([item_id]).get(item_id, [])}
            alloc_rows = [{"Vendor": v, "Alokasi Order (%)": existing_alloc.get(v, 0.0)} for v in vendors_for_item]
            df_alloc = pd.DataFrame(alloc_rows)

            edited_alloc = st.data_editor(
                df_alloc,
                hide_index=True,
                use_container_width=True,
                disabled=["Vendor"],
                key=f"alloc_editor_{item_id}",
                column_config={"Alokasi Order (%)": st.column_config.NumberColumn(min_value=0, max_value=100, step=5)},
            )

            total_pct = edited_alloc["Alokasi Order (%)"].sum()
            st.caption(f"Total Alokasi: **{total_pct:.0f}%** (Harus pas 100% untuk menyimpan)")

            if st.button("💾 Simpan Alokasi % Split Qty", key=f"save_alloc_{item_id}", disabled=(total_pct != 100)):
                allocations = [
                    {"vendor_id": vendor_name_to_id.get(row["Vendor"]), "percentage": row["Alokasi Order (%)"]}
                    for _, row in edited_alloc.iterrows()
                ]
                save_split_allocation(item_id, allocations)
                st.success("Alokasi % Split Qty berhasil disimpan!")
                st.rerun(scope="fragment")


# =====================================================================
# AWARDING LETTER & THANK YOU LETTER (docxtpl, download langsung)
# =====================================================================
import subprocess
import tempfile
import os

def build_items_subdoc(doc, items, total_amount):
    """Gambar tabel rincian barang SPK sebagai subdoc docxtpl.
    items: list of dict {no, barang, qty, uom, unit_price, total} (sudah string terformat)."""
    sd = doc.new_subdoc()

    headers = ["No", "Nama Barang / Jasa", "Qty", "UOM", "Harga Satuan (Rp)", "Total (Rp)"]
    widths = [Cm(0.9), Cm(6.0), Cm(1.4), Cm(1.5), Cm(3.0), Cm(3.1)]  # total 15.9 cm

    def shade(cell, hex_fill):
        cell._tc.get_or_add_tcPr().append(
            parse_xml(r'<w:shd {} w:val="clear" w:color="auto" w:fill="{}"/>'.format(nsdecls("w"), hex_fill)))

    def fmt(cell, text, size=9, bold=False, align=WD_ALIGN_PARAGRAPH.LEFT):
        cell.text = str(text)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        p = cell.paragraphs[0]
        p.alignment = align
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1
        run = p.runs[0] if p.runs else p.add_run()
        run.font.name = "Calibri"
        run.font.size = Pt(size)
        run.font.bold = bold

    table = sd.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    table.allow_autofit = False

    # geser tabel supaya sejajar dengan teks poin list (indent 720 dxa)
    tblPr = table._tbl.tblPr
    tblInd = parse_xml(r'<w:tblInd {} w:w="720" w:type="dxa"/>'.format(nsdecls("w")))
    layout = tblPr.find(qn("w:tblLayout"))
    if layout is not None:
        layout.addprevious(tblInd)
    else:
        tblPr.append(tblInd)

    # header (diulang kalau tabel pindah halaman)
    hdr = table.rows[0]
    hdr._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    for i, h in enumerate(headers):
        c = hdr.cells[i]
        shade(c, "ED7D31")
        fmt(c, h, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    aligns = [WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.LEFT, WD_ALIGN_PARAGRAPH.CENTER,
              WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.RIGHT, WD_ALIGN_PARAGRAPH.RIGHT]
    for it in items:
        row = table.add_row()
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        vals = [it["no"], it["barang"], it["qty"], it["uom"], it["unit_price"], it["total"]]
        for i, v in enumerate(vals):
            fmt(row.cells[i], v, align=aligns[i])

    # baris TOTAL (gabung 5 kolom pertama)
    tot = table.add_row()
    merged = tot.cells[0].merge(tot.cells[4])
    fmt(merged, "TOTAL", bold=True, align=WD_ALIGN_PARAGRAPH.RIGHT)
    fmt(tot.cells[5], total_amount, bold=True, align=WD_ALIGN_PARAGRAPH.RIGHT)
    shade(merged, "F2F2F2")
    shade(tot.cells[5], "F2F2F2")

    # lebar grid + tiap sel fixed -> teks panjang otomatis wrap
    for gc, w in zip(table._tbl.tblGrid.findall(qn("w:gridCol")), widths):
        gc.set(qn("w:w"), str(int(w.twips)))
    for row in table.rows:
        for idx, w in enumerate(widths):
            if idx < len(row.cells):
                row.cells[idx].width = w

    sd.add_paragraph("")
    return sd


def _render_letter_doc(template_path, context):
    """Bikin DocxTemplate baru, sisipkan tabel harga (subdoc) kalau ada items, lalu render."""
    from docxtpl import DocxTemplate
    doc = DocxTemplate(template_path)
    ctx = dict(context)
    if ctx.get("items"):
        ctx["tabel_harga"] = build_items_subdoc(doc, ctx["items"], ctx.get("total_amount", ""))
    else:
        ctx["tabel_harga"] = ""
    doc.render(ctx)
    return doc
    
def generate_letter_pdf(template_path, context, output_filename="Letter.pdf"):
    """
    Render template DOCX pakai docxtpl, lalu convert langsung ke PDF via LibreOffice CLI.
    Return: (bytes_pdf, error_message)
    """
    try:
        from docxtpl import DocxTemplate
    except ImportError:
        return None, "Library `docxtpl` belum terinstall di requirements.txt"

    if not os.path.exists(template_path):
        return None, f"File template `{template_path}` tidak ditemukan."

    try:
        # 1. Render data ke file DOCX sementara
        doc = _render_letter_doc(template_path, context)

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_docx_path = os.path.join(tmpdir, "temp_render.docx")
            doc.save(temp_docx_path)

            # 2. Convert DOCX ke PDF pakai LibreOffice CLI
            cmd = [
                "libreoffice",
                "--headless",
                "--convert-to", "pdf",
                "--outdir", tmpdir,
                temp_docx_path
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            temp_pdf_path = os.path.join(tmpdir, "temp_render.pdf")

            if os.path.exists(temp_pdf_path):
                with open(temp_pdf_path, "rb") as f:
                    pdf_bytes = f.read()
                return pdf_bytes, None
            else:
                return None, "Gagal mengkonversi DOCX ke PDF (LibreOffice output not found)."

    except Exception as e:
        # Fallback kalau libreoffice tidak tersedia di lokal/OS: kirim DOCX biasa
        try:
            doc = _render_letter_doc(template_path, context)
            buf = io.BytesIO()
            doc.save(buf)
            buf.seek(0)
            return buf.getvalue(), f"LibreOffice error ({e}), dikirim fallback format DOCX."
        except Exception as err_fallback:
            return None, str(err_fallback)


# =====================================================================
# 📜 AWARDING (SPK) & THANK YOU LETTER + AUTOMATIC EMAIL BLAST
# Alur: Download CQR -> Download SPK -> Upload SPK approved -> Close & kirim email
# =====================================================================
def _identify_winners_and_losers(pivot_items, df_m, split_toggle_map, split_allocation_map, recommended_vendor_per_item):
    winner_items = {}
    for _, r in pivot_items.iterrows():
        item_id = r["item_id"]
        barang = r["Barang"]
        if split_toggle_map.get(item_id) and split_allocation_map.get(item_id):
            for a in split_allocation_map[item_id]:
                match = df_m[(df_m["Barang"] == barang) & (df_m["vendor"] == a["vendor_name"])]
                if match.empty:
                    continue
                unit_price = float(match.iloc[0]["price"])
                alloc_qty = float(r["Qty"] or 0) * (a["percentage"] / 100.0)
                winner_items.setdefault(a["vendor_name"], []).append({
                    "barang": barang, "qty": round(alloc_qty, 2), "uom": r["UOM"],
                    "unit_price": unit_price, "total": unit_price * alloc_qty,
                })
        else:
            best_v = recommended_vendor_per_item.get(barang)
            if not best_v or str(best_v).startswith("🔀"):
                continue
            match = df_m[(df_m["Barang"] == barang) & (df_m["vendor"] == best_v)]
            if match.empty:
                continue
            row_val = match.iloc[0]
            winner_items.setdefault(best_v, []).append({
                "barang": barang, "qty": r["Qty"], "uom": r["UOM"],
                "unit_price": row_val["price"], "total": row_val["total"],
            })
    all_vendor_names = sorted(df_m["vendor"].unique().tolist())
    losing_vendors = [v for v in all_vendor_names if v not in winner_items]
    return winner_items, losing_vendors


@st.cache_data(ttl=600, show_spinner=False)
def get_warehouse_addresses():
    res = sb.table("warehouse_addresses").select("origin, alamat").execute()
    return {
        clean(r["origin"]).lower(): r["alamat"]
        for r in (res.data or [])
        if clean(r.get("origin")) and clean(r.get("alamat"))   # baris kosong diabaikan
    }


def lookup_warehouse_address(location):
    """Cocokkan lokasi PR ke alamat gudang. Exact match dulu, lalu yang
    namanya paling panjang yang terkandung di lokasi. Kalau gak ketemu, balik lokasi aslinya."""
    norm = lambda x: " ".join(clean(x).lower().split())
    loc = norm(location)
    if not loc:
        return "-"
    addr_map = {norm(k): v for k, v in get_warehouse_addresses().items()}
    if loc in addr_map:
        return addr_map[loc]
    hits = [k for k in addr_map if k in loc]
    if hits:
        return addr_map[max(hits, key=len)]
    hits = [k for k in addr_map if loc in k]
    if hits:
        return addr_map[min(hits, key=len)]
    return clean(location) or "-"


# ---------------------------------------------------------------------
# SPK APPROVED (diupload PIC, 1 file per RFQ per vendor, upload ulang = replace)
# ---------------------------------------------------------------------
def get_spk_document(pr_id, vendor_id):
    try:
        res = sb.table("spk_documents").select("*").eq("pr_id", str(pr_id)).eq("vendor_id", str(vendor_id)).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


def save_spk_approved(pr_id, vendor_id, file):
    try:
        path = f"{pr_id}/spk_approved/{vendor_id}/{file.name}"
        old = get_spk_document(pr_id, vendor_id)
        sb.storage.from_(BUCKET_NAME).upload(
            path, file.getvalue(), {"content-type": file.type or "application/pdf", "upsert": "true"}
        )
        if old and old.get("file_path") and old["file_path"] != path:
            try:
                sb.storage.from_(BUCKET_NAME).remove([old["file_path"]])
            except Exception:
                pass
        sb.table("spk_documents").delete().eq("pr_id", str(pr_id)).eq("vendor_id", str(vendor_id)).execute()
        sb.table("spk_documents").insert(
            {"pr_id": str(pr_id), "vendor_id": str(vendor_id), "file_name": file.name, "file_path": path}
        ).execute()
        get_storage_file_bytes.clear()
        return True, None
    except Exception as e:
        return False, str(e)


APPROVER_COLS = ["manager_name", "manager_title", "chief_name", "chief_title"]


def get_pic_approvers_map():
    """{pic_id: {manager_name, manager_title, chief_name, chief_title}} dari tabel pic_approvers."""
    try:
        res = sb.table("pic_approvers").select("*").execute()
        return {r["pic_id"]: r for r in (res.data or [])}
    except Exception:
        return {}


def save_pic_approvers(pic_id, manager_name, manager_title, chief_name, chief_title):
    """Simpan/ubah data Manager & Chief untuk 1 PIC. Return (ok, error)."""
    try:
        sb.table("pic_approvers").upsert({
            "pic_id": str(pic_id),
            "manager_name": clean(manager_name) or None,
            "manager_title": clean(manager_title) or None,
            "chief_name": clean(chief_name) or None,
            "chief_title": clean(chief_title) or None,
        }).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def get_pic_profile(pr_info):
    """Profil PIC pemilik RFQ (uploaded_by). Kalau gak ada, pakai user yang sedang login."""
    uid = None
    try:
        uid = pr_info.get("uploaded_by") if pr_info is not None else None  # pr_info bisa dict / pandas Series
        if uid is None or pd.isna(uid) or not str(uid).strip():
            uid = None
    except Exception:
        uid = None
    if not uid:
        uid = (st.session_state.get("user_info") or {}).get("id")
    if not uid:
        return {}
    try:
        prof = sb.table("profiles").select("vendor_name").eq("id", uid).single().execute().data or {}
    except Exception:
        prof = {}
    try:
        appr = sb.table("pic_approvers").select("*").eq("pic_id", str(uid)).execute().data
        appr = appr[0] if appr else {}
    except Exception:
        appr = {}
    return {**prof, **{k: appr.get(k) for k in APPROVER_COLS}}


def resolve_spk_signer(total_amount, pic_profile):
    """Tentukan penandatangan SPK dari sisi TACO.
    total <= SPK_APPROVAL_LIMIT -> Manager PIC; lebih dari itu -> Chief PIC.
    Return (nama, jabatan lengkap, mis. "Procurement Chemical Manager"; default "Manager"/"Chief" kalau belum diisi). Nama '-' kalau datanya belum diisi admin."""
    p = pic_profile or {}
    try:
        total = round(float(total_amount or 0))
    except Exception:
        total = 0
    if total <= SPK_APPROVAL_LIMIT:
        return (clean(p.get("manager_name")) or "-", clean(p.get("manager_title")) or "Manager")
    return (clean(p.get("chief_name")) or "-", clean(p.get("chief_title")) or "Chief")


def build_spk_context(pr_info, v_name, vendor_id, items, df_m):
    """Susun semua isian template SPK untuk 1 vendor pemenang."""
    tanggal_now = datetime.now().strftime("%d %B %Y")
    v_rows = df_m[df_m["vendor"] == v_name]
    vendor_ref_no = v_rows["vendor_ref_no"].iloc[0] if not v_rows.empty else "-"
    validity = (
        v_rows["validity_period"].iloc[0]
        if (not v_rows.empty and "validity_period" in v_rows.columns) else "-"
    )
    won_names = [it["barang"] for it in items]
    lt_vals = [lt for lt in v_rows[v_rows["Barang"].isin(won_names)]["lead_time"] if lt]
    lead_time_days = max(lt_vals) if lt_vals else "-"
    tax_type = v_rows["tax_type"].iloc[0] if (not v_rows.empty and "tax_type" in v_rows.columns) else "-"

    try:
        ship_res = (
            sb.table("rfq_assignments")
            .select("shipment_mode, pr_items!inner(pr_id)")
            .eq("pr_items.pr_id", pr_info["id"])
            .limit(1)
            .execute()
        )
        shipment_mode = (ship_res.data[0].get("shipment_mode") if ship_res.data else None) or "-"
    except Exception:
        shipment_mode = "-"

    v_data = {}
    if vendor_id:
        try:
            v_data = sb.table("profiles").select("email, pic_name, pic_jabatan").eq("id", vendor_id).single().execute().data or {}
        except Exception:
            v_data = {}

    total_amount = sum(it["total"] for it in items)
    pic_profile = get_pic_profile(pr_info)
    signer_name, signer_title = resolve_spk_signer(total_amount, pic_profile)
    if signer_name == "-":
        st.warning(
            f"⚠️ {signer_title} untuk PIC pemilik RFQ ini belum diisi (SPK {v_name}). "
            f"Minta admin isi di menu ➕ Daftarkan PIC → tab Atur Manager & Chief."
        )
    items_text = "\n".join(
        f"- {it['barang']} ({it['qty']} {it['uom']}) @ Rp {it['unit_price']:,.0f} = Rp {it['total']:,.0f}".replace(",", ".")
        for it in items
    )
    def _fmt_qty(q):
        try:
            q = float(q)
            return str(int(q)) if q == int(q) else f"{q:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return str(q)

    def _rp(n):
        return f"{float(n):,.0f}".replace(",", ".")

    items_table = [
        {
            "no": i,
            "barang": it["barang"],
            "qty": _fmt_qty(it["qty"]),
            "uom": it["uom"],
            "unit_price": _rp(it["unit_price"]),
            "total": _rp(it["total"]),
        }
        for i, it in enumerate(items, start=1)
    ]    
    context = {
        "rfq_title": pr_info.get("rfq_title") or pr_info["pr_code"],
        "pr_number": pr_info["pr_code"],
        "tanggal_rfq": tanggal_now,
        "tanggal_spk": tanggal_now,
        "vendor_name": v_name,
        "rfq_num": vendor_ref_no or "-",
        "validity": validity or "-",
        "alamat_gudang": lookup_warehouse_address(pr_info.get("location")),
        "shipment_mode": shipment_mode,
        "lead_time_days": lead_time_days,
        "awarding_items_text": items_text,
        "items": items_table,
        "total_amount": f"{total_amount:,.0f}".replace(",", "."),
        "tax": str(tax_type).lower() if tax_type and tax_type != "-" else "-",
        "pic": pic_profile.get("vendor_name") or (st.session_state.get("user_info") or {}).get("vendor_name") or "-",
        # --- kolom tanda tangan di template_awarding.docx ---
        "managerchief": signer_name,      # {{ managerchief }}  nama penandatangan TACO
        "jabatan_taco": signer_title,     # {{ jabatan_taco }}  jabatan lengkap penandatangan TACO
        "pic_v": v_data.get("pic_name") or "-",   # {{ pic_v }}  nama PIC vendor
        "direktur": v_data.get("pic_name") or "-",  # alias lama
        "jabatan": v_data.get("pic_jabatan") or "-",  # {{ jabatan }} jabatan PIC vendor
    }
    return context, v_data.get("email")


def generate_spk_for_winners(pr_info, winner_items, df_m, vendor_id_to_name):
    """Buat SPK (PDF) untuk semua vendor pemenang, untuk di-download PIC & ditandatangani."""
    name_to_id = {v: k for k, v in vendor_id_to_name.items()}
    out = {}
    for v_name, items in winner_items.items():
        ctx, _ = build_spk_context(pr_info, v_name, name_to_id.get(v_name), items, df_m)
        pdf_bytes, err = generate_letter_pdf("template/template_awarding.docx", ctx)
        out[v_name] = {"bytes": pdf_bytes, "ext": "pdf" if (pdf_bytes and not err) else "docx", "err": err}
    return out


def _execute_close_and_archive_rfq(pr_info, winner_items, losing_vendors, df_m, vendor_id_to_name, pivot_items):
    """Eksekusi final: kirim email Awarding (lampiran = SPK APPROVED yang diupload PIC) + Thank You,
    update status assignment -> Submitted, dan ARSIPKAN RFQ. Return True kalau sukses."""
    rfq_title = pr_info.get("rfq_title") or pr_info["pr_code"]
    tanggal_now = datetime.now().strftime("%d %B %Y")
    name_to_id = {v: k for k, v in vendor_id_to_name.items()}

    # Pastikan SPK approved semua vendor pemenang sudah ada
    missing = [v for v in winner_items if not (name_to_id.get(v) and get_spk_document(pr_info["id"], name_to_id[v]))]
    if missing:
        st.error("❌ SPK approved belum diupload untuk: " + ", ".join(missing))
        return False

    # A. Kirim ke Vendor Pemenang (lampiran = SPK approved)
    for v_name, items in winner_items.items():
        v_id = name_to_id.get(v_name)
        spk_doc = get_spk_document(pr_info["id"], v_id)
        try:
            spk_bytes = sb.storage.from_(BUCKET_NAME).download(spk_doc["file_path"])
        except Exception as e:
            st.warning(f"⚠️ Gagal mengambil SPK approved {v_name}: {e}")
            continue

        v_prof = sb.table("profiles").select("email").eq("id", v_id).single().execute()
        v_email = v_prof.data.get("email") if (v_prof and v_prof.data) else None
        if not v_email:
            continue

        total_amount = sum(it["total"] for it in items)
        items_text = "\n".join(
            f"- {it['barang']} ({it['qty']} {it['uom']}) @ Rp {it['unit_price']:,.0f} = Rp {it['total']:,.0f}".replace(",", ".")
            for it in items
        )
        email_body = DEFAULT_AWARDING_EMAIL_TEMPLATE.format(
            vendor_name=v_name, rfq_title=rfq_title,
            awarding_items_text=items_text,
            total_amount=f"{total_amount:,.0f}".replace(",", "."),
        )
        attachments = [(f"SPK_{_safe_filename(rfq_title)}_{_safe_filename(v_name)}.pdf", spk_bytes)]
        send_custom_email(v_email, f"🎉 AWARDING LETTER - RFQ: {rfq_title}", email_body, attachments)

    # B. Kirim Email Thank You ke Vendor Lain
    for v_name in losing_vendors:
        v_id = name_to_id.get(v_name)
        v_prof = sb.table("profiles").select("email").eq("id", v_id).single().execute() if v_id else None
        v_email = v_prof.data.get("email") if (v_prof and v_prof.data) else None

        if v_email:
            context = {"rfq_title": rfq_title, "pr_number": pr_info["pr_code"], "tanggal_rfq": tanggal_now, "vendor_name": v_name}
            pdf_thanks_bytes, _ = generate_letter_pdf("template/template_thanks.docx", context)
            email_body = DEFAULT_THANKYOU_EMAIL_TEMPLATE.format(vendor_name=v_name, rfq_title=rfq_title)
            attachments = [(f"Thank_You_Letter_{v_name}.pdf", pdf_thanks_bytes)] if pdf_thanks_bytes else None
            send_custom_email(v_email, f"Hasil Evaluasi RFQ: {rfq_title}", email_body, attachments)

    # C. Update status assignment -> Submitted (otomatis masuk History)
    items_res = sb.table("pr_items").select("id").eq("pr_id", pr_info["id"]).execute()
    item_ids = [i["id"] for i in items_res.data] if items_res.data else []
    if item_ids:
        sb.table("rfq_assignments").update({"status": "Submitted"}).in_("item_id", item_ids).execute()

    # D. ARSIPKAN RFQ -> hilang dari daftar Price Comparison
    try:
        sb.table("purchase_requests").update({"is_archived": True}).eq("id", pr_info["id"]).execute()
    except Exception as e:
        st.warning(f"⚠️ RFQ sudah ditutup & email terkirim, tapi gagal menandai arsip: {e}")
    return True


@st.dialog("Konfirmasi Tutup & Arsip RFQ")
def _confirm_close_rfq_dialog(pr_info, winner_items, losing_vendors, df_m, vendor_id_to_name, pivot_items):
    st.write(
        f"Apakah Anda ingin menutup dan mengarsipkan RFQ **{pr_info.get('rfq_title') or pr_info['pr_code']}** ini? "
        "Email Awarding (dengan lampiran SPK approved) ke vendor pemenang dan Thank You ke vendor lain akan langsung dikirim, "
        "dan RFQ ini akan hilang dari Price Comparison (tetap bisa dilihat di History RFQ)."
    )
    c1, c2 = st.columns(2)
    if c1.button("✅ Ya, Tutup & Archive", type="primary", use_container_width=True):
        with st.spinner("Mengirimkan Email ke Semua Vendor..."):
            ok = _execute_close_and_archive_rfq(pr_info, winner_items, losing_vendors, df_m, vendor_id_to_name, pivot_items)
        if ok:
            st.session_state["active_compare_pr_id"] = None
            st.toast("RFQ Berhasil Di-close dan Diarsipkan!", icon="🎉")
            st.rerun()
    if c2.button("❌ Batal", use_container_width=True):
        st.rerun()


def render_awarding_section(pr_info, recommended_vendor_per_item, split_toggle_map, split_allocation_map, pivot_items, df_m, vendor_id_to_name, cqr_files=None, ai_included=False):
    pr_id = pr_info["id"]
    st.markdown("##### 📜 Awarding & Final Close RFQ")
    st.caption("Alur: **① Download Price COmparison → ② Download SPK → ③ Upload SPK approved → ④ Close RFQ & kirim email.**")

    winner_items, losing_vendors = _identify_winners_and_losers(
        pivot_items, df_m, split_toggle_map, split_allocation_map, recommended_vendor_per_item
    )
    name_to_id = {v: k for k, v in vendor_id_to_name.items()}



    # ① Download CQR
    st.markdown("**① Download Price Comparison**")
    if cqr_files:
        if ai_included:
            st.caption("✅ AI Insight ikut tercetak di PDF Price Comparison.")
        else:
            st.caption("ℹ️ AI Insight belum di-generate — klik 🤖 Asisten AI → Generate Insight dulu kalau mau ikut masuk PDF.")
        base = _safe_filename(pr_info.get("rfq_title") or pr_info["pr_code"])
        for label, b in cqr_files:
            fname = f"Price_Comparison_{base}.pdf" if len(cqr_files) == 1 else f"Price_Comparison_{base}_{_safe_filename(label)}.pdf"
            st.download_button(
                f"📄 Download Price Comparison — {label} (PDF)", b, fname,
                mime="application/pdf", key=f"dl_cqr_{pr_id}_{_safe_filename(label)}", use_container_width=True,
            )
    else:
        st.caption("⚠️ Library `reportlab` belum terinstall.")

    # ② Download SPK
    st.markdown("**② Download SPK (untuk ditandatangani)**")
    spk_key = f"spk_gen_{pr_id}"
    if st.button("📄 Generate SPK", key=f"gen_spk_{pr_id}", use_container_width=True):
        with st.spinner("Membuat SPK untuk semua vendor pemenang..."):
            st.session_state[spk_key] = generate_spk_for_winners(pr_info, winner_items, df_m, vendor_id_to_name)
    st.caption("Klik Generate lagi kalau ada perubahan pemenang / qty / data vendor.")
    rfq_fn = _safe_filename(pr_info.get("rfq_title") or pr_info["pr_code"])
    generated = st.session_state.get(spk_key, {})
    for v_name in winner_items:
        g = generated.get(v_name)
        if g and g.get("bytes"):
            st.download_button(
                f"⬇️ SPK — {v_name}", g["bytes"],
                f"SPK_{rfq_fn}_{_safe_filename(v_name)}.{g['ext']}",
                key=f"dl_spk_{pr_id}_{v_name}", use_container_width=True,
            )
        elif g:
            st.warning(f"Gagal membuat SPK {v_name}: {g.get('err')}")

    # ③ Upload SPK approved
    st.markdown("**③ Upload SPK Approved**")
    spk_status = {}
    for v_name in winner_items:
        v_id = name_to_id.get(v_name)
        spk_status[v_name] = get_spk_document(pr_id, v_id) if v_id else None
        with st.container(border=True):
            st.write(f"**{v_name}**")
            if spk_status[v_name]:
                st.caption(f"✅ Tersimpan: {spk_status[v_name]['file_name']} (upload lagi untuk mengganti)")
            else:
                st.caption("⏳ Belum ada SPK approved")
            up = st.file_uploader(f"SPK approved — {v_name}", type=["pdf"], key=f"spk_up_{pr_id}_{v_id}")
            if up is not None and st.button("💾 Simpan SPK Approved", key=f"spk_save_{pr_id}_{v_id}"):
                ok, err = save_spk_approved(pr_id, v_id, up)
                if ok:
                    st.success("SPK approved tersimpan.")
                    st.rerun(scope="fragment")
                else:
                    st.error(f"Gagal menyimpan: {err}")

    # ④ Close
    st.markdown("**④ Close RFQ & Kirim Email**")
    c_w, c_l = st.columns(2)
    pic_profile_awd = get_pic_profile(pr_info)
    with c_w:
        st.write("**🏆 Vendor Pemenang (Email + SPK approved):**")
        for v_name, items in winner_items.items():
            tot = sum(it["total"] for it in items)
            sg_name, sg_title = resolve_spk_signer(tot, pic_profile_awd)
            st.caption(
                f"• **{v_name}** — Total: Rp {tot:,.0f} → TTD TACO: {sg_title} ({sg_name})".replace(",", ".")
            )
    with c_l:
        st.write("**🙏 Vendor Lain (Email Thank You Letter):**")
        for v_name in losing_vendors:
            st.caption(f"• **{v_name}**")
    all_uploaded = bool(winner_items) and all(spk_status.values())
    if not all_uploaded:
        st.caption("🔒 Tombol aktif setelah SPK approved semua vendor pemenang terupload.")
    if st.button("🚀 Close RFQ & Auto-Send Email Notification", type="primary", use_container_width=True, disabled=not all_uploaded):
        _confirm_close_rfq_dialog(pr_info, winner_items, losing_vendors, df_m, vendor_id_to_name, pivot_items)


# =====================================================================
# AI OCR: BACA PDF QUOTATION VENDOR (GEMINI VISION)
# =====================================================================
def extract_quote_from_pdf(pdf_bytes, items_list):
    """Baca PDF quotation vendor pakai Gemini Vision, cocokkan ke daftar barang RFQ.
    HASIL INI DRAFT — vendor WAJIB review sebelum submit, tidak ada auto-submit."""
    if "gemini" not in st.secrets or not st.secrets["gemini"].get("api_key"):
        return None, "Fitur AI OCR belum aktif — tambahkan `gemini.api_key` di secrets."
    try:
        import google.generativeai as genai
        import json
    except ImportError:
        return None, "Library `google-generativeai` belum terinstall."

    api_key = st.secrets["gemini"]["api_key"].strip()
    genai.configure(api_key=api_key)

    items_text = "\n".join(f"- {it}" for it in items_list)

    prompt = f"""Baca PDF quotation vendor ini DENGAN TELITI, KHUSUSNYA DI ANGKA HARGA (bisa scan/tulisan tangan). Cocokkan ke daftar barang berikut (berdasarkan kemiripan nama):
{items_text}

Untuk tiap barang yang ditemukan, ekstrak: nama_barang_rfq (harus salah satu dari daftar di atas), unit_price (angka saja), brand, spesifikasi, lead_time_days (default 7), ready_stock ("Ya"/"Tidak"), warranty.

Jawab HANYA JSON array tanpa markdown:
[{{"nama_barang_rfq":"...","unit_price":0,"brand":"-","spesifikasi":"-","lead_time_days":7,"ready_stock":"Ya","warranty":"-"}}]
"""

    generation_config = genai.GenerationConfig(
        temperature=0,
        max_output_tokens=2048,
    )

    def _call(model_name):
        model = genai.GenerativeModel(model_name, generation_config=generation_config)
        return model.generate_content(
            [prompt, {"mime_type": "application/pdf", "data": pdf_bytes}]
        )

    res, err = call_gemini_with_fallback(_call)
    if err:
        return None, f"Semua model Gemini gagal dipanggil: {err}"

    try:
        raw = (res.text or "").strip()
        raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        result = {}
        for row in parsed:
            key = str(row.get("nama_barang_rfq", "")).strip()
            if key:
                result[key] = row
        return result, None
    except Exception as e:
        return None, str(e)


# =====================================================================
# AI COST ESTIMATOR: HARGA KOMODITAS ACUAN + BREAKDOWN OE (WEB-GROUNDED)
# =====================================================================
def get_ai_cost_estimate(item_name, additional_desc=""):
    """Cari harga komoditas acuan (web-grounded via Gemini) + breakdown OE estimasi.
    SELALU estimasi AI — wajib direview manual, bukan harga mengikat."""
    if "gemini" not in st.secrets or not st.secrets["gemini"].get("api_key"):
        return None, "Fitur AI belum aktif — tambahkan `gemini.api_key` di secrets."
    try:
        import google.generativeai as genai
    except ImportError:
        return None, "Library `google-generativeai` belum terinstall."

    api_key = st.secrets["gemini"]["api_key"].strip()
    genai.configure(api_key=api_key)

    prompt = f"""Kamu adalah Cost Analyst procurement sparepart industri.
Item: {item_name}
Info tambahan dari PIC: {additional_desc or '-'}

Tugas:
1. Cari harga komoditas/material dasar yang relevan sebagai komponen item ini
   (misal baja, alumunium, tembaga, dll) dari sumber terkini seperti London Metal
   Exchange atau data pasar publik lain yang kamu temukan lewat pencarian.
2. Berikan estimasi breakdown biaya: % Material, % Jasa/Manufaktur, % Margin vendor
   (pakai asumsi umum industri kalau data pasti tidak tersedia).
3. Berikan range harga wajar (Rp) untuk item ini berdasarkan asumsi di atas.

WAJIB tulis di bagian akhir bahwa ini ESTIMASI AI, bukan harga final/mengikat, dan
wajib direview manual oleh tim procurement sebelum dipakai untuk negosiasi.

Format jawaban Markdown dengan heading persis seperti ini:
### 📊 Harga Komoditas Acuan
### 🧮 Estimasi Breakdown Biaya
### 💰 Range Harga Wajar
### ⚠️ Disclaimer
"""
    def _call(model_name):
        try:
            model = genai.GenerativeModel(model_name, tools=[{"google_search": {}}])
            return model.generate_content(prompt)
        except Exception:
            model = genai.GenerativeModel(model_name)
            return model.generate_content(prompt)

    res, err = call_gemini_with_fallback(_call)
    if err:
        return None, f"Semua model Gemini gagal dipanggil: {err}"
    return res.text, None


# =====================================================================
# AI EXECUTIVE INSIGHT + CHAT PROMPT MANUAL
# (Dipanggil dari dalam fragment render_comparison_detail, jadi st.rerun()
#  di sini di-scope="fragment" biar gak nge-rerun seluruh app.)
# =====================================================================
AI_FAB_CSS = """
<style>
/* marker-nya disembunyiin, tombol popover tepat sesudahnya dijadiin floating button */
.element-container:has(.taco-ai-fab-marker),
div[data-testid="stElementContainer"]:has(.taco-ai-fab-marker) { display: none; }

.st-key-taco_ai_fab,
.element-container:has(.taco-ai-fab-marker) + .element-container,
div[data-testid="stElementContainer"]:has(.taco-ai-fab-marker) + div[data-testid="stElementContainer"] {
    position: fixed !important;
    right: 24px;
    bottom: 90px;
    width: auto !important;
    z-index: 999990;
}
.st-key-taco_ai_fab button,
div[data-testid="stElementContainer"]:has(.taco-ai-fab-marker) + div[data-testid="stElementContainer"] button,
.element-container:has(.taco-ai-fab-marker) + .element-container button {
    border-radius: 999px;
    padding: 0.65rem 1.2rem;
    font-weight: 600;
    color: #fff;
    background: linear-gradient(135deg, #ED7D31, #d9480f);
    border: none;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.28);
}
div[data-testid="stElementContainer"]:has(.taco-ai-fab-marker) + div[data-testid="stElementContainer"] button:hover,
.st-key-taco_ai_fab button:hover,
.element-container:has(.taco-ai-fab-marker) + .element-container button:hover {
    filter: brightness(1.08);
    color: #fff;
}
/* panel chat yang kebuka */
div[data-testid="stPopoverBody"]:has(.taco-ai-body-marker) {
    width: min(460px, 92vw) !important;
    max-height: 78vh;
    overflow-y: auto;
}
</style>
"""

AI_CHAT_SYSTEM = (
    "Kamu asisten Procurement & Cost Analyst TACO Group. Jawab SINGKAT, to the point, "
    "dalam Bahasa Indonesia, berbasis angka dari data harga yang diberikan. "
    "Kalau data tidak cukup, bilang apa adanya, jangan mengarang."
)


def _gemini_stream(prompt, system_instruction=None, temperature=0.3, max_tokens=1500, cache_key="stream"):
    """Panggil Gemini mode STREAMING supaya teks langsung muncul kata demi kata
    (jauh terasa lebih cepat daripada nunggu jawaban utuh).
    Chunk pertama diambil di dalam fallback, jadi kalau model mati/kuota habis
    tetap otomatis coba model berikutnya. Return (generator_teks, error)."""
    import google.generativeai as genai
    from itertools import chain

    def _call(model_name):
        kwargs = {"generation_config": genai.GenerationConfig(temperature=temperature, max_output_tokens=max_tokens)}
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        model = genai.GenerativeModel(model_name, **kwargs)
        it = iter(model.generate_content(prompt, stream=True))
        first = next(it)  # error / model mati / kosong ketahuan di sini -> fallback jalan
        return first, it

    res, err = call_gemini_with_fallback(_call, cache_key=cache_key)
    if err or not res:
        return None, err or "Respons kosong"
    first, it = res

    def _gen():
        for chunk in chain([first], it):
            try:
                t = chunk.text
            except ValueError:  # chunk tanpa teks (mis. finish/safety)
                t = ""
            if t:
                yield t

    return _gen(), None


def _ai_error_text(err):
    if "429" in str(err) or "quota" in str(err).lower():
        return "⚠️ Kuota AI harian sudah habis. Coba lagi nanti atau hubungi admin."
    return f"⚠️ AI gagal merespons: {err}"


# =====================================================================
# AI ASSISTANT (floating button kanan bawah -> popup: Generate Insight / Tanya AI)
# Nama fungsi dipertahankan (render_ai_insight) supaya pemanggilnya gak perlu diubah.
# Hasil insight tetap disimpan di st.session_state["ai_insight_<rfq>"] -> otomatis masuk PDF CQR.
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

    genai.configure(api_key=st.secrets["gemini"]["api_key"].strip())

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

    # marker + CSS: tombol popover tepat setelah marker dijadikan floating button
    st.markdown(AI_FAB_CSS + '<span class="taco-ai-fab-marker"></span>', unsafe_allow_html=True)

    try:
        _pop = st.popover("🤖 Asisten AI", key="taco_ai_fab")
    except TypeError:  # Streamlit lama: popover belum punya parameter key
        _pop = st.popover("🤖 Asisten AI")
    with _pop:
        st.markdown('<span class="taco-ai-body-marker"></span>**🤖 Asisten AI Procurement**', unsafe_allow_html=True)
        st.caption(f"RFQ: {rfq_title}")
        tab_ins, tab_chat = st.tabs(["✨ Generate Insight", "💬 Tanya AI"])

        # ---------------- TAB 1: GENERATE INSIGHT (prompt otomatis) ----------------
        with tab_ins:
            has_insight = insight_key in st.session_state
            if has_insight:
                clicked = st.button("🔄 Regenerate Analisis", key=f"regen_{rfq_title}", use_container_width=True)
            else:
                st.caption("Analisis otomatis: cost saving & trade-off, evaluasi bobot, merk alternatif, catatan penting, action plan.")
                clicked = st.button("✨ Generate AI Insight", key=f"gen_{rfq_title}", type="primary", use_container_width=True)

            if clicked:
                context_table = df_display.to_csv(index=False)
                prompt = f"""Kamu adalah Procurement Specialist & Cost Analyst untuk TACO Group.
Analisis data perbandingan penawaran vendor berikut untuk RFQ: {rfq_title}

DATA PERBANDINGAN (kolom "🏆 Rekomendasi" = vendor terbaik per item berdasarkan bobot yang dipilih PIC):
{context_table}

BOBOT PRIORITAS YANG DIPAKAI PIC SAAT INI: {weights_text}
{saving_text}

Tugasmu adalah memberikan analisis otomatis tanpa perlu ditanya.
SUSUN HASIL ANALISIS DENGAN FORMAT MARKDOWN SEPERTI BERIKUT (WAJIB GUNAKAN HEADING & BULLET POINT KONSISTEN):

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
                stream, err = _gemini_stream(prompt, temperature=0.3, max_tokens=1500, cache_key="insight")
                if stream is None:
                    st.error(_ai_error_text(err))
                else:
                    with st.container(height=380, border=True):
                        try:
                            st.session_state[insight_key] = st.write_stream(stream)
                        except Exception as e:
                            st.error(_ai_error_text(e))
            elif has_insight:
                with st.container(height=380, border=True):
                    st.markdown(st.session_state[insight_key])

        # ---------------- TAB 2: TANYA AI (chat bebas) ----------------
        with tab_chat:
            history = st.session_state[history_key]
            chat_box = st.container(height=330, border=True)
            with chat_box:
                if not history:
                    st.caption("Contoh: “Berapa total hemat kalau saya pilih Vendor A semua?”")
                for msg in history:
                    with st.chat_message(msg["role"]):
                        st.markdown(msg["content"])

            with st.form(f"ai_chat_form_{rfq_title}", clear_on_submit=True):
                q = st.text_input("Pertanyaan", placeholder="Tanya apa saja soal penawaran ini…", label_visibility="collapsed")
                sent = st.form_submit_button("Kirim ➤", use_container_width=True)

            if sent and clean(q):
                q = clean(q)
                past = "\n".join(
                    f"{'User' if m['role'] == 'user' else 'AI'}: {m['content']}" for m in history[-6:]
                )
                full_query = (
                    f"Data Price Comparison untuk RFQ {rfq_title}:\n{df_display.to_csv(index=False)}\n\n"
                    f"Bobot prioritas PIC: {weights_text}\n{saving_text}\n\n"
                    + (f"Percakapan sebelumnya:\n{past}\n\n" if past else "")
                    + f"Pertanyaan User: {q}"
                )
                history.append({"role": "user", "content": q})
                with chat_box:
                    with st.chat_message("user"):
                        st.markdown(q)
                    with st.chat_message("assistant"):
                        stream, err = _gemini_stream(
                            full_query, system_instruction=AI_CHAT_SYSTEM,
                            temperature=0.3, max_tokens=800, cache_key="chat",
                        )
                        if stream is None:
                            answer = _ai_error_text(err)
                            st.markdown(answer)
                        else:
                            try:
                                answer = st.write_stream(stream)
                            except Exception as e:
                                answer = _ai_error_text(e)
                                st.markdown(answer)
                history.append({"role": "assistant", "content": answer})


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
    rows = {"Brand": [], "Ready Stock": [], "Lead Time (Hari)": [], "Warranty": [], "Payment Term (TOP)": [], "Pajak (PPN)": []}
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
        taxes = sorted(set(str(t).strip() for t in sub["tax_type"] if str(t).strip() and str(t).strip() != "-")) if "tax_type" in sub.columns else []
        rows["Pajak (PPN)"].append(", ".join(taxes) if taxes else "-")

    summary = pd.DataFrame(rows, index=vendor_list).T.reset_index()
    summary = summary.rename(columns={"index": "Kriteria"})
    return summary


def build_validity_summary(df_m, vendor_list):
    """Tabel kecil: masa berlaku penawaran (validity_period) per vendor, diambil
    dari quote mereka (satu nilai yang sama berlaku untuk seluruh item di RFQ ini)."""
    rows = []
    for v in vendor_list:
        sub = df_m[df_m["vendor"] == v]
        validity = sub["validity_period"].iloc[0] if (not sub.empty and "validity_period" in sub.columns) else "-"
        rows.append({"Vendor": v, "Masa Berlaku Penawaran": validity or "-"})
    return pd.DataFrame(rows)


def generate_cqr_pdf(rfq_title, pr_code, location, weights, display_df, cost_saving, saving_pct, recommended_total, ai_insight_text, summary_df=None, split_data=None, highlight_map=None, grand_total_vendors=None, df_m=None, split_mode=False, split_alloc=None):
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, CondPageBreak
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
    elements.append(Paragraph("Price Comparison", title_style))
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

    from xml.sax.saxutils import escape as _esc

    def _P(val, style):
        return Paragraph(_esc(_strip_emoji_for_pdf(val)), style)

    avail_width = landscape(A4)[0] - 24 * mm
    dark = colors.HexColor("#1f2937")
    grid = colors.HexColor("#cbd5e1")
    green = colors.HexColor("#d1fae5")

    # ------------------------------------------------------------------
    # TABEL PERBANDINGAN: per vendor = [Brand/Stock/Lead Time | Price/Unit | Total]
    # (brand, ready stock, lead time sifatnya beda tiap item -> ikut di tabel item)
    # ------------------------------------------------------------------
    elements.append(CondPageBreak(50 * mm))
    elements.append(Paragraph("Tabel Perbandingan", h2_style))
    cols = list(display_df.columns)
    vendors = [c[: -len(" — Price/Unit")] for c in cols if c.endswith(" — Price/Unit")]
    rec_col = "🏆 Rekomendasi"

    info_lookup = {}
    if df_m is not None and not df_m.empty:
        for _, mr in df_m.iterrows():
            info_lookup[(mr["Barang"], mr["vendor"])] = mr

    def _info_cell(barang, vendor):
        mr = info_lookup.get((barang, vendor))
        if mr is None:
            return "-"
        brand = str(mr.get("brand") or "-").strip() or "-"
        stock = str(mr.get("ready_stock") or "").strip()
        stock_txt = "Ready" if stock == "Ya" else ("Tidak ready" if stock == "Tidak" else "-")
        lt = mr.get("lead_time")
        lt_txt = f"{lt} hari" if lt not in (None, "", 0) and str(lt) != "nan" else "-"
        return f"Brand: {brand}\nStock: {stock_txt}\nLead time: {lt_txt}"

    def _info_para(text):
        return Paragraph("<br/>".join(_esc(_strip_emoji_for_pdf(t)) for t in text.split("\n")), body_cell_style)

    n_v = len(vendors)
    head0 = [_P("", header_cell_style)] * 3
    head1 = [_P("Barang", header_cell_style), _P("Qty", header_cell_style), _P("UOM", header_cell_style)]
    for v in vendors:
        head0 += [_P(v, header_cell_style), "", ""]
        head1 += [_P("Brand / Stock / Lead Time", header_cell_style), _P("Harga", header_cell_style), _P("Total", header_cell_style)]
    head0.append(_P("", header_cell_style))
    head1.append(_P("Rekomendasi", header_cell_style))

    data = [head0, head1]
    n_items = len(display_df) - 1  # baris terakhir = GRAND TOTAL
    for ri, (_, row) in enumerate(display_df.iterrows()):
        is_total = ri == n_items
        barang = str(row["Barang"])
        line = [_P(barang, body_cell_style), _P(row.get("Qty", ""), body_cell_style), _P(row.get("UOM", ""), body_cell_style)]
        for v in vendors:
            line.append(_P("", body_cell_style) if is_total else _info_para(_info_cell(barang, v)))
            line.append(_P(row.get(f"{v} — Price/Unit", ""), body_cell_style))
            line.append(_P(row.get(f"{v} — Total", ""), body_cell_style))
        line.append(_P(row.get(rec_col, ""), body_cell_style))
        data.append(line)

    fixed = {"barang": 0.17, "qty": 0.04, "uom": 0.04, "rec": 0.11}
    per_vendor = (1 - sum(fixed.values())) / max(n_v, 1)
    widths = [avail_width * fixed["barang"], avail_width * fixed["qty"], avail_width * fixed["uom"]]
    for _ in vendors:
        widths += [avail_width * per_vendor * 0.38, avail_width * per_vendor * 0.29, avail_width * per_vendor * 0.33]
    widths.append(avail_width * fixed["rec"])

    tbl = Table(data, colWidths=widths, repeatRows=2, splitByRow=1)
    last_idx = len(data) - 1
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 1), dark),
        ("GRID", (0, 0), (-1, -1), 0.5, grid),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 2), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("BACKGROUND", (0, last_idx), (-1, last_idx), colors.HexColor("#e2e8f0")),
        ("LINEBELOW", (0, 0), (2, 0), 0.5, dark),
        ("LINEBELOW", (3 + 3 * n_v, 0), (3 + 3 * n_v, 0), 0.5, dark),
    ]
    for vi in range(n_v):
        c0 = 3 + 3 * vi
        cmds.append(("SPAN", (c0, 0), (c0 + 2, 0)))
        cmds.append(("ALIGN", (c0, 0), (c0 + 2, 0), "CENTER"))
    # highlight hijau vendor terpilih
    for ri in range(n_items):
        barang = str(display_df.iloc[ri]["Barang"])
        for v in (highlight_map or {}).get(barang, []):
            if v in vendors:
                c0 = 3 + 3 * vendors.index(v)
                cmds.append(("BACKGROUND", (c0, ri + 2), (c0 + 2, ri + 2), green))
        if (highlight_map or {}).get(barang):
            cmds.append(("BACKGROUND", (3 + 3 * n_v, ri + 2), (3 + 3 * n_v, ri + 2), green))
    for v in (grand_total_vendors or []):
        if v in vendors:
            c0 = 3 + 3 * vendors.index(v)
            cmds.append(("BACKGROUND", (c0 + 2, last_idx), (c0 + 2, last_idx), green))
    tbl.setStyle(TableStyle(cmds))
    elements.append(tbl)
    elements.append(Spacer(1, 14))

    # ------------------------------------------------------------------
    # Ringkasan per vendor (tanpa Brand / Ready Stock / Lead Time -> sudah di tabel item)
    # ------------------------------------------------------------------
    if summary_df is not None and not summary_df.empty:
        summary_pdf = summary_df[~summary_df["Kriteria"].isin(["Brand", "Ready Stock", "Lead Time (Hari)"])]
        if not summary_pdf.empty:
            elements.append(CondPageBreak(50 * mm))
            elements.append(Paragraph("Ringkasan per Vendor", h2_style))
            sum_rows = [list(summary_pdf.columns)] + [[str(v) for v in row] for row in summary_pdf.values]
            sum_wrapped = []
            for ri, row in enumerate(sum_rows):
                style = header_cell_style if ri == 0 else body_cell_style
                sum_wrapped.append([_P(val, style) for val in row])
            sum_n_cols = len(summary_pdf.columns)
            sum_tbl = Table(sum_wrapped, colWidths=[avail_width / sum_n_cols] * sum_n_cols, repeatRows=1)
            sum_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), dark),
                ("GRID", (0, 0), (-1, -1), 0.5, grid),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]))
            elements.append(sum_tbl)
            elements.append(Spacer(1, 14))

    # ------------------------------------------------------------------
    # Alokasi Split Qty: HANYA kalau prioritas = Split Qty
    # ------------------------------------------------------------------
    if split_mode and split_alloc:
        elements.append(CondPageBreak(50 * mm))
        elements.append(Paragraph("Alokasi Split Qty", h2_style))
        sa_cols = ["Barang", "Vendor", "Alokasi (%)", "Qty", "UOM"]
        sa_rows = [sa_cols] + [
            [str(r["Barang"]), str(r["Vendor"]), f"{r['Persen']:g}%", f"{r['Qty']:g}", str(r["UOM"])]
            for r in split_alloc
        ]
        sa_wrapped = [[_P(val, header_cell_style if ri == 0 else body_cell_style) for val in row] for ri, row in enumerate(sa_rows)]
        sa_tbl = Table(sa_wrapped, colWidths=[avail_width * w for w in (0.34, 0.26, 0.14, 0.14, 0.12)], repeatRows=1)
        sa_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), dark),
            ("GRID", (0, 0), (-1, -1), 0.5, grid),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ]))
        elements.append(sa_tbl)
        elements.append(Spacer(1, 12))

    # ------------------------------------------------------------------
    # Rincian PO per vendor pemenang (selalu langsung per pemenang)
    # ------------------------------------------------------------------
    if split_data:
        elements.append(CondPageBreak(50 * mm))
        elements.append(Paragraph("Ringkasan PO per Vendor Pemenang", h2_style))
        rows = [["Vendor", "Jumlah Item", "Subtotal PO"]]
        grand_po = 0
        for v_name, items in split_data.items():
            sub = sum(float(i["Total"]) for i in items)
            grand_po += sub
            rows.append([str(v_name), str(len(items)), f"Rp {sub:,.0f}".replace(",", ".")])
        rows.append(["TOTAL", "", f"Rp {grand_po:,.0f}".replace(",", ".")])
        wrapped = [[_P(val, header_cell_style if ri == 0 else body_cell_style) for val in row] for ri, row in enumerate(rows)]
        po_tbl = Table(wrapped, colWidths=[avail_width * w for w in (0.55, 0.15, 0.30)], repeatRows=1)
        po_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), dark),
            ("GRID", (0, 0), (-1, -1), 0.5, grid),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), colors.HexColor("#e2e8f0")),
        ]))
        elements.append(po_tbl)
        elements.append(Spacer(1, 10))

    if ai_insight_text:
        elements.append(CondPageBreak(50 * mm))
        elements.append(Paragraph("AI Procurement Insight", h2_style))
        for raw_line in str(ai_insight_text).split("\n"):
            line = _strip_emoji_for_pdf(raw_line)
            if not line.strip():
                continue
            indent_lvl = (len(raw_line) - len(raw_line.lstrip(" "))) // 2
            line = _esc(line.strip())
            line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
            line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", line)
            m_head = re.match(r"^#{2,4}\s+(.*)", line)
            m_bul = re.match(r"^[-*•]\s+(.*)", line)
            if m_head:
                elements.append(Paragraph(m_head.group(1), ai_head_style))
            elif m_bul:
                st_b = ParagraphStyle("B", parent=bullet_style, leftIndent=12 + 12 * indent_lvl)
                elements.append(Paragraph("• " + m_bul.group(1), st_b))
            elif re.match(r"^\d+[.)]\s+", line):
                elements.append(Paragraph(line, bullet_style))
            else:
                elements.append(Paragraph(line, normal_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()
def build_cqr_pdf_files(rfq_title, pr_code, loc, weights, display_df, cost_saving, saving_pct,
                        recommended_total, ai_insight_text, summary_df, split_data, highlight_map,
                        grand_total_vendors, df_m, split_mode, split_alloc_rows):
    """Return list of (label, pdf_bytes). >1 vendor pemenang -> 1 PDF per vendor pemenang.
    Tiap PDF hanya berisi item yang dimenangkan vendor itu, TAPI tetap menampilkan harga
    SEMUA vendor (pembanding) + highlight hijau pemenang."""
    if len(split_data) <= 1:
        b = generate_cqr_pdf(
            rfq_title, pr_code, loc, weights, display_df, cost_saving, saving_pct, recommended_total,
            ai_insight_text, summary_df=summary_df, split_data=split_data, highlight_map=highlight_map,
            grand_total_vendors=grand_total_vendors, df_m=df_m, split_mode=split_mode, split_alloc=split_alloc_rows,
        )
        return [("Price Comparison", b)] if b else []

    rp = lambda n: f"Rp {float(n):,.0f}".replace(",", ".")
    pct_lookup = {(r["Barang"], r["Vendor"]): r["Persen"] for r in split_alloc_rows}
    split_barang = {r["Barang"] for r in split_alloc_rows}

    # daftar vendor pembanding (semua vendor yang ada kolomnya di display_df)
    suffix = " — Price/Unit"
    all_vendors = [c[: -len(suffix)] for c in display_df.columns if c.endswith(suffix)]

    body_df = display_df[display_df["Barang"] != "GRAND TOTAL"]
    files = []

    for v_name, items in split_data.items():
        won = {it["Barang"] for it in items}

        # 1) Ambil baris item yang dimenangkan vendor ini -- SEMUA kolom vendor tetap ada
        sub = body_df[body_df["Barang"].isin(won)].copy().reset_index(drop=True)

        # 2) Kolom rekomendasi: kalau item di-split tampilkan persennya
        def _rec(barang, current):
            pct = pct_lookup.get((barang, v_name))
            if split_mode and barang in split_barang and pct is not None:
                return f"Split {pct:g}%"
            return current
        sub["🏆 Rekomendasi"] = [_rec(b, c) for b, c in zip(sub["Barang"], sub["🏆 Rekomendasi"])]

        # 3) GRAND TOTAL: total tiap vendor HANYA untuk item yang dimenangkan (apple to apple)
        v_total = sum(float(it["Total"]) for it in items)
        gt = {"Barang": "GRAND TOTAL", "Qty": "", "UOM": ""}
        for v in all_vendors:
            vt = df_m[(df_m["vendor"] == v) & (df_m["Barang"].isin(won))]["total"].sum()
            gt[f"{v}{suffix}"] = ""
            gt[f"{v} — Total"] = rp(vt) if vt else "-"
        gt["🏆 Rekomendasi"] = rp(v_total)
        sub = pd.concat([sub, pd.DataFrame([gt])], ignore_index=True)

        # 4) Cost saving khusus item yang dimenangkan vendor ini
        v_worst = sum(
            float(df_m[df_m["Barang"] == it["Barang"]]["price"].max()) * float(it["Qty"]) for it in items
        )
        v_saving = v_worst - v_total
        v_pct = (v_saving / v_worst * 100) if v_worst > 0 else 0

        # 5) Highlight hijau: pakai highlight_map asli, dibatasi ke item yang dimenangkan
        v_highlight = {b: vs for b, vs in (highlight_map or {}).items() if b in won}

        v_summary = summary_df if summary_df is not None else None
        v_alloc = [r for r in split_alloc_rows if r["Barang"] in won] if split_mode else None

        b = generate_cqr_pdf(
            f"{rfq_title} — {v_name}", pr_code, loc, weights, sub, v_saving, v_pct, v_total,
            ai_insight_text, summary_df=v_summary, split_data=None, highlight_map=v_highlight,
            grand_total_vendors=[v_name], df_m=df_m, split_mode=split_mode, split_alloc=v_alloc,
        )
        if b:
            files.append((v_name, b))
    return files

def show_login():
    st.title("🛠️ TACO Sparepart RFQ")
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        with st.container(border=True):
            email_input = clean(st.text_input("Email")).lower()
            password_input = clean(st.text_input("Password", type="password"))
            st.caption("💡 Kalau pakai autofill/password manager, klik sekali di kolom sebelum menekan Masuk.")

            if st.button("Masuk", type="primary", use_container_width=True):
                if not email_input or not password_input:
                    st.warning("Isi email & password dulu ya.")
                else:
                    profile, err = login(email_input, password_input)
                    if profile:
                        st.session_state["user_info"] = profile
                        token = create_session(profile["id"])
                        if token:
                            st.session_state["session_token"] = token
                            st.query_params["token"] = token
                        st.rerun()
                    elif err == "rate_limited":
                        st.error("⏳ Terlalu banyak percobaan login beruntun. Supabase lagi nge-limit sementara — tunggu 1-2 menit lalu coba lagi (jangan spam klik).")
                    elif err == "invalid":
                        st.error("❌ Email atau Password salah.")
                    else:
                        st.error("⚠️ Gagal login karena masalah koneksi/server. Coba lagi sebentar.")


def _sync_item_selection(row_key):
    """Callback checkbox: sinkronkan status centang ke 'selected_row_keys'
    (persistent, gak akan kehapus otomatis walau checkbox-nya gak dirender lagi
    -- beda sama session_state bawaan widget yang auto-kehapus kalau widgetnya
    gak muncul di suatu run, misal karena ke-filter search)."""
    key = f"chk_{row_key}"
    selected = st.session_state.setdefault("selected_row_keys", set())
    if st.session_state.get(key, False):
        selected.add(row_key)
    else:
        selected.discard(row_key)


def render_pr_list(df_source, already_published, scope_tag):
    """Render list PR + checkbox item, dipakai buat tab Urgent & Normal."""
    if df_source.empty:
        st.info("Tidak ada item di kategori ini.")
        return

    selected_row_keys = st.session_state.setdefault("selected_row_keys", set())

    for pr_no in df_source["PR CODE"].unique():
        df_group = df_source[df_source["PR CODE"] == pr_no].reset_index(drop=True)
        loc = df_group["LOCATION"].iloc[0] if "LOCATION" in df_group.columns else "-"
        prio = str(df_group["PRIORITY STATUS"].iloc[0]) if "PRIORITY STATUS" in df_group.columns else "-"
        label = f"📄 PR: {pr_no} | 📍 {loc}" + (" | 🚨 URGENT" if "URGENT" in prio.upper() else "")

        with st.expander(label, expanded=st.session_state.get("expand_all", False)):
            cA, cB, _ = st.columns([1, 1, 3])

            if cA.button("✅ Pilih Semua", key=f"all_{scope_tag}_{pr_no}"):
                for k in df_group["ROW_KEY"]:
                    st.session_state[f"chk_{k}"] = True
                    selected_row_keys.add(k)
                st.rerun(scope="fragment")

            if cB.button("🗑️ Hapus Semua", key=f"none_{scope_tag}_{pr_no}"):
                for k in df_group["ROW_KEY"]:
                    st.session_state[f"chk_{k}"] = False
                    selected_row_keys.discard(k)
                st.rerun(scope="fragment")

            h1, h2, h3, h4, h5 = st.columns([0.5, 3, 3, 1, 1])
            h1.markdown("**✓**")
            h2.markdown("**Description**")
            h3.markdown("**Description 2**")
            h4.markdown("**Qty**")
            h5.markdown("**UOM**")

            for _, item_row in df_group.iterrows():
                row_key = item_row["ROW_KEY"]
                match_key = (clean(item_row.get("DESCRIPTION", "")).lower(),
                             clean(item_row.get("DESCRIPTION 2", "")).lower())
                is_published = match_key in already_published
                bg = "#d1fae5" if is_published else "transparent"

                c1, c2, c3, c4, c5 = st.columns([0.5, 3, 3, 1, 1])
                st.markdown(f'<div style="background-color:{bg}; padding:4px; border-radius:4px;">', unsafe_allow_html=True)

                chk_key = f"chk_{row_key}"
                # Kalau key checkbox ini sempat "kehapus" oleh Streamlit (karena
                # sebelumnya gak dirender, misal ke-filter search), re-init dari
                # 'selected_row_keys' yang persistent, biar centangnya gak reset.
                if chk_key not in st.session_state:
                    st.session_state[chk_key] = row_key in selected_row_keys

                c1.checkbox(
                    "sel", key=chk_key, label_visibility="collapsed",
                    on_change=_sync_item_selection, args=(row_key,),
                )

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

    uploaded_file = st.file_uploader("Upload File Excel", type=["xlsx"])

    if uploaded_file is not None:
        try:
            df_raw = pd.read_excel(uploaded_file, header=2)
            df_raw.columns = [clean(c).upper() for c in df_raw.columns]
            if "PR CODE" not in df_raw.columns and "DESCRIPTION" not in df_raw.columns:
                uploaded_file.seek(0)
                df_raw = pd.read_excel(uploaded_file, header=0)
                df_raw.columns = [clean(c).upper() for c in df_raw.columns]

            df_raw = df_raw.reset_index(drop=True)
            df_raw["ROW_KEY"] = df_raw.index.astype(str)
            st.session_state["uploaded_pr_df"] = df_raw
        except Exception as e:
            st.error(f"Gagal membaca file Excel: {e}")
            return

    df_raw = st.session_state.get("uploaded_pr_df")

    if df_raw is None or df_raw.empty:
        st.info("Silakan upload file Excel PR untuk memulai.")
        return

    df_display = df_raw.copy()
    if "STATUS" in df_raw.columns:
        df_display = df_display[df_display["STATUS"].astype(str).str.strip().str.upper() == "OPEN"]
    if "QUANTITY" in df_raw.columns:
        df_raw["QUANTITY"] = pd.to_numeric(df_raw["QUANTITY"], errors="coerce").fillna(0)
        df_display = df_display[pd.to_numeric(df_display["QUANTITY"], errors="coerce") > 0]

    if df_display.empty:
        st.warning("Tidak ada item berstatus 'Open' dengan Qty > 0 di file ini.")
        return

    # Semua interaksi (search, filter, checklist, review) ada dalam SATU fragment
    render_import_workspace(df_display)


# =====================================================================
# WORKSPACE: SEARCH + FILTER + CHECKLIST + REVIEW & ASSIGN
# Dibungkus @st.fragment SATU KALI biar semua interaksi (ketik search,
# klik checkbox, pilih semua/hapus semua, expand/collapse) gak
# nge-rerun seluruh app -> gak ada flicker/reset di tabel bawah.
# =====================================================================
@st.fragment
def render_import_workspace(df_display):
    # Kalau ada permintaan reset pilihan tertunda (dari tombol "Reset Pilihan"
    # atau setelah Publish RFQ berhasil), eksekusi SEKARANG -- sebelum checkbox
    # mana pun digambar di run ini -- biar gak bentrok dengan widget yang sudah
    # diinstansiasi (StreamlitWidgetAlreadyInstantiatedError).
    if st.session_state.pop("_pending_selection_reset", False):
        for k in df_display["ROW_KEY"]:
            st.session_state.pop(f"chk_{k}", None)
        st.session_state["selected_row_keys"] = set()

    already_published = get_already_published_keys_cached()

    if "expand_all" not in st.session_state:
        st.session_state["expand_all"] = False

    c_search, c_exp = st.columns([3, 1])
    search_query = c_search.text_input("🔍 Cari (semua kolom: No. PR, Deskripsi, Lokasi, UOM, dll)...")

    if c_exp.button("📂 Collapse All" if st.session_state["expand_all"] else "📂 Expand All", use_container_width=True):
        st.session_state["expand_all"] = not st.session_state["expand_all"]
        st.rerun(scope="fragment")

    locations = ["Semua Lokasi"]
    if "LOCATION" in df_display.columns:
        locations += list(df_display["LOCATION"].dropna().unique())

    selected_loc = st.selectbox("📍 Filter Lokasi Pengiriman:", locations)

    df_to_show = df_display.copy()
    if search_query:
        q = clean(search_query).lower()
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
            render_pr_list(df_urgent, already_published, "urgent")

    with sub_tab_normal:
        with st.container(height=400, border=True):
            render_pr_list(df_normal, already_published, "normal")

    # =========================================================
    # REVIEW & ASSIGN VENDOR (WITH EDITABLE VENDOR EMAIL TABLE)
    # =========================================================
    st.divider()
    st.subheader("🎯 Review & Assign Vendor")
    selected_row_keys = st.session_state.setdefault("selected_row_keys", set())
    final_items = df_display[df_display["ROW_KEY"].isin(selected_row_keys)].copy()

    if final_items.empty:
        st.info("Belum ada item yang dipilih.")
    else:
        c_title, c_reset = st.columns([4, 1])
        with c_title:
            rfq_title_val = clean(st.text_input("🏷️ Judul RFQ (Wajib)", placeholder="Contoh: Pengadaan Sparepart Staples Batam"))
        with c_reset:
            st.write(" ")
            st.write(" ")
            if st.button("🔄 Reset Pilihan", use_container_width=True):
                reset_checkbox_selection(df_display)
                st.rerun(scope="fragment")

        for col in ["PR CODE", "LOCATION", "DESCRIPTION", "DESCRIPTION 2", "QUANTITY", "UOM"]:
            if col not in final_items.columns:
                final_items[col] = "-"

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

        df_v = get_vendors_cached()
        if df_v.empty:
            st.warning("Belum ada vendor terdaftar.")
        else:
            sel_v_names = st.multiselect("Pilih Vendor Penerima RFQ:", df_v["vendor_name"].unique())

            edited_vendor_emails = {}
            if sel_v_names:
                st.markdown("##### 📧 Checking Email Vendor Penerima RFQ")
                st.caption("💡 Anda dapat mengedit / menambahkan email tujuan (pisahkan dengan `;` jika lebih dari satu). Perubahan di sini akan otomatis ter-update ke data vendor.")

                selected_vendors_df = df_v[df_v["vendor_name"].isin(sel_v_names)][["id", "vendor_name", "email"]].copy()
                selected_vendors_df.columns = ["vendor_id", "Nama Vendor", "Email Tujuan (Bisa multi-email pisah ;)"]

                edited_v_df = st.data_editor(
                    selected_vendors_df,
                    hide_index=True,
                    use_container_width=True,
                    disabled=["Nama Vendor"],
                    column_config={
                        "vendor_id": None  # <--- ID DISEMBUNYIKAN DARI TAMPILAN
                    },
                    key="vendor_email_checker_editor"
                )

                for _, v_row in edited_v_df.iterrows():
                    raw_target = clean(v_row["Email Tujuan (Bisa multi-email pisah ;)"])
                    normalized_target = "; ".join(
                        clean(e).lower() for e in raw_target.split(";") if clean(e)
                    )
                    edited_vendor_emails[v_row["vendor_id"]] = {
                        "name": clean(v_row["Nama Vendor"]),
                        "email": normalized_target,
                    }

            has_urgent_item = False
            if "PRIORITY STATUS" in final_items.columns:
                has_urgent_item = final_items["PRIORITY STATUS"].astype(str).str.upper().str.contains("URGENT").any()

            default_priority_index = 0 if has_urgent_item else 1

            c_left, c_right = st.columns(2)
            with c_left:
                default_deadline = datetime.today() + timedelta(days=3)
                rfq_deadline_val = st.date_input("📅 Batas Waktu Vendor:", value=default_deadline)

                priority_val = st.radio(
                    "🚨 Tingkat Prioritas RFQ:",
                    ["URGENT", "NORMAL"],
                    index=default_priority_index
                )
                delivery_type_val = st.radio("🚚 Metode Pengiriman:", ["Franco (Kirim ke lokasi)", "Loco (Pengambilan sendiri)"])
                shipment_mode_val = st.radio(
                    "📦 Jenis Pengiriman:",
                    ["Langsung (Sekaligus)", "Partial (Boleh Bertahap)"],
                    help="Partial berarti vendor boleh mengirim barang bertahap/beberapa kali sampai qty terpenuhi; Langsung berarti harus sekaligus dalam satu pengiriman.",
                )
            with c_right:
                pic_notes_val = st.text_area("📝 Catatan Tambahan Khusus Vendor:")
                st.caption(
                    "💡 Info login (email & password) otomatis disertakan di undangan HANYA untuk "
                    "vendor yang **belum pernah** menerima info login sebelumnya. Vendor yang sudah "
                    "pernah menerima **tidak** akan di-reset otomatis — supaya passwordnya gak "
                    "berubah-ubah kalau ada beberapa RFQ jalan berbarengan. Kalau vendor lupa "
                    "password, reset manual lewat menu 🔑 Reset Password."
                )

            if st.button("🚀 Publish Undangan RFQ", type="primary", use_container_width=True):
                if not rfq_title_val:
                    st.error("❌ Mohon isi 'Judul RFQ' terlebih dahulu!")
                elif not sel_v_names:
                    st.error("❌ Silakan pilih minimal satu vendor.")
                else:
                    vendor_ids = list(edited_vendor_emails.keys())
                    pr_code_main = str(final_items["PR CODE"].iloc[0]) if "PR CODE" in final_items.columns else "-"
                    location_main = str(edited["LOCATION"].iloc[0])

                    pr_id = publish_rfq(
                        rfq_title_val, pr_code_main, location_main, priority_val,
                        st.session_state["user_info"]["id"], edited, vendor_ids,
                        delivery_type_val, pic_notes_val, rfq_deadline_val, attached_files or [],
                        shipment_mode=shipment_mode_val,
                    )

                    items_text_email = "\n".join(
                        f"- {r['DESCRIPTION']} {r['DESCRIPTION_2']} ({r['QUANTITY']} {r['UOM']}) [Note: {r['CATATAN_BARIS_ATAU_LINK_GAMBAR']}]"
                        for _, r in edited.iterrows()
                    )

                    with st.spinner("Mengirim notifikasi email & mengupdate data vendor..."):
                        for v_id, v_info in edited_vendor_emails.items():
                            v_name = v_info["name"]
                            target_email = v_info["email"]

                            try:
                                sb.table("profiles").update({"email": target_email}).eq("id", v_id).execute()
                            except Exception as e_up:
                                st.warning(f"⚠️ Gagal meng-update email di profil {v_name}: {e_up}")

                            # Otomatis: kirim password HANYA kalau vendor ini BELUM PERNAH
                            # menerima info login sebelumnya. Kalau sudah pernah -> jangan
                            # reset (biar gak bentrok kalau ada RFQ lain jalan berbarengan).
                            vendor_row = df_v[df_v["id"] == v_id]
                            already_has_credentials = bool(
                                vendor_row.iloc[0].get("credentials_sent", False)
                            ) if not vendor_row.empty else False

                            new_password = None
                            if not already_has_credentials:
                                new_password = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                                ok_reset, err_reset = reset_user_password(v_id, new_password)
                                if ok_reset:
                                    mark_credentials_delivered(v_id)
                                else:
                                    st.warning(f"⚠️ Gagal membuat password awal untuk {v_name}: {err_reset}")
                                    new_password = None

                            send_rfq_email(
                                target_email, v_name, rfq_title_val,
                                rfq_deadline_val.strftime("%d %b %Y"), items_text_email,
                                delivery_type_val, pic_notes_val, attached_files or [],
                                vendor_password=new_password,items_df=edited,
                            )

                    # Email vendor berubah -> cache vendor perlu di-refresh
                    get_vendors_cached.clear()

                    st.toast("🚀 Undangan RFQ Berhasil Diterbitkan!", icon="🎉")
                    st.success(f"✅ Undangan RFQ '{rfq_title_val}' telah terkirim ke vendor & email berhasil diperbarui!")
                    reset_checkbox_selection(df_display)
                    st.rerun()


# =====================================================================
# UI: PROC - MONITORING & COMPARISON (detail dibungkus @st.fragment
# biar geser slider bobot / expander gak nge-rerun seluruh app)
# =====================================================================
@st.fragment
def render_comparison_detail(pr_info):
    active_id = pr_info["id"]
    rfq_title_active = pr_info.get("rfq_title") or pr_info["pr_code"]
    loc_active = pr_info.get("location") or "-"

    st.title(f"📊 {rfq_title_active}")
    st.caption(f"📍 Lokasi Pengiriman: **{loc_active}** | No. PR: **{pr_info['pr_code']}**")
    render_pending_reminder_box(pr_info) 
    st.markdown("---")
    st.subheader("📋 Matrix Perbandingan Penawaran Vendor")

    st.markdown("##### 🎯 Prioritas Pemilihan Vendor:")
    sort_priority = st.radio(
        "Urutkan & Prioritaskan Berdasarkan:",
        [PRIO_ITEM, PRIO_TOTAL, PRIO_LEAD, PRIO_COMBO, PRIO_SPLIT],
        horizontal=True,
        label_visibility="collapsed",
    )
    # (Harga, TOP, Lead Time)
    weight_presets = {
        PRIO_ITEM: (100, 0, 0),
        PRIO_TOTAL: (100, 0, 0),
        PRIO_LEAD: (0, 0, 100),
        PRIO_COMBO: (50, 25, 25),
        PRIO_SPLIT: (50, 25, 25),
    }
    total_mode = (sort_priority == PRIO_TOTAL)
    split_mode = (sort_priority == PRIO_SPLIT)
    w_price, w_top, w_leadtime = weight_presets[sort_priority]

    k_price = f"w_price_ss_{active_id}"
    k_top = f"w_top_ss_{active_id}"
    k_leadtime = f"w_leadtime_ss_{active_id}"
    weight_keys = [k_price, k_top, k_leadtime]

    if st.session_state.get(f"last_preset_{active_id}") != sort_priority:
        st.session_state[k_price], st.session_state[k_top], st.session_state[k_leadtime] = weight_presets[sort_priority]
        st.session_state[f"last_preset_{active_id}"] = sort_priority

    def _rebalance_weights(changed_key):
        total = sum(st.session_state[k] for k in weight_keys)
        diff = 100 - total
        if diff == 0:
            return
        others = [k for k in weight_keys if k != changed_key]
        others_total = sum(st.session_state[k] for k in others)
        if others_total <= 0:
            share = diff / len(others)
            for k in others:
                st.session_state[k] = max(0, min(100, round(st.session_state[k] + share)))
        else:
            for k in others:
                proportion = st.session_state[k] / others_total
                st.session_state[k] = max(0, min(100, round(st.session_state[k] + diff * proportion)))
        remainder = 100 - sum(st.session_state[k] for k in weight_keys)
        if remainder != 0:
            st.session_state[others[-1]] = max(0, min(100, st.session_state[others[-1]] + remainder))

    with st.expander("⚙️ Atur bobot custom (opsional)"):
        use_custom = st.checkbox("Set bobot custom di bawah ini")
        st.caption("Total bobot otomatis dijaga 100% — geser satu slider, yang lain menyesuaikan sendiri.")
        cw1, cw2, cw3 = st.columns(3)
        cw1.slider("💰 Harga", 0, 100, key=k_price, on_change=_rebalance_weights, args=(k_price,))
        cw2.slider("📅 TOP", 0, 100, key=k_top, on_change=_rebalance_weights, args=(k_top,))
        cw3.slider("⏱️ Lead Time", 0, 100, key=k_leadtime, on_change=_rebalance_weights, args=(k_leadtime,))
        if use_custom:
            w_price = st.session_state[k_price]
            w_top = st.session_state[k_top]
            w_leadtime = st.session_state[k_leadtime]

    if total_mode:
        st.caption("🏷️ Mode Total Termurah: dipilih **1 vendor** dengan total harga semua item paling rendah (bobot tidak dipakai).")
    elif split_mode:
        st.caption("✂️ Mode Split Qty: item yang tidak di-split dipilih dengan bobot kombinasi; item yang di-split diatur di bagian Split Qty di bawah.")
        st.caption(f"⚖️ Bobot dipakai: Harga {w_price}% · TOP {w_top}% · Lead Time {w_leadtime}%")
    else:
        st.caption(f"⚖️ Bobot dipakai: Harga {w_price}% · TOP {w_top}% · Lead Time {w_leadtime}%")

    raw_q = sb.table("quotes").select("*, rfq_assignments(*, pr_items(*), profiles(*))").execute()

    relevant_quotes = []
    max_round_per_assignment = {}
    for q in raw_q.data or []:
        ass = q.get("rfq_assignments") or {}
        item = ass.get("pr_items") or {}
        if item.get("pr_id") != active_id:
            continue
        relevant_quotes.append(q)
        ass_id = q.get("assignment_id")
        q_round = q.get("round") or 1
        if ass_id is not None:
            max_round_per_assignment[ass_id] = max(max_round_per_assignment.get(ass_id, 1), q_round)

    data_matrix = []
    first_quote_lookup = {}
    vendors_in_pr = set()
    vendor_id_to_name = {}
    any_nego_happened = False

    for q in relevant_quotes:
        ass = q.get("rfq_assignments") or {}
        item = ass.get("pr_items") or {}
        v_profile = ass.get("profiles") or {}
        v_name = v_profile.get("vendor_name", "Unknown")
        ass_id = q.get("assignment_id")
        q_round = q.get("round") or 1
        item_id = item.get("id")

        if q_round == 1:
            first_quote_lookup[(item_id, v_name)] = q.get("unit_price", 0)
        if max_round_per_assignment.get(ass_id, 1) > 1:
            any_nego_happened = True

        if q_round != max_round_per_assignment.get(ass_id, 1):
            continue

        vendors_in_pr.add(v_name)
        if v_profile.get("id"):
            vendor_id_to_name[v_profile["id"]] = v_name

        d1 = str(item.get("description") or "").strip()
        d2 = str(item.get("description2") or "").strip()
        full_item_name = f"{d1} - {d2}" if (d1 and d2 and d1 != d2) else (d1 or d2 or "-")
        clean_name = clean_description(full_item_name)

        data_matrix.append({
            "item_id": item.get("id"),
            "Barang": clean_name,
            "Spesifikasi": q.get("spec_vendor", "-"),
            "Qty": item.get("quantity", 0),
            "UOM": item.get("uom", "-"),
            "vendor": v_name,
            "vendor_id": v_profile.get("id"),
            "price": q.get("unit_price", 0),
            "total": q.get("unit_price", 0) * item.get("quantity", 0),
            "brand": q.get("brand", "-"),
            "lead_time": q.get("lead_time_days", 0),
            "ready_stock": q.get("ready_stock", "-"),
            "warranty": q.get("warranty", "-"),
            "top_days": v_profile.get("top_days") or 0,
            "round": q_round,
            "vendor_ref_no": q.get("vendor_ref_no", "-"),
            "tax_type": q.get("tax_type") or "-",
            "validity_period": q.get("validity_period", "-"),
        })

    if not data_matrix:
        st.warning("Belum ada penawaran harga yang masuk dari vendor untuk RFQ ini.")
        return

    df_m = pd.DataFrame(data_matrix)
    vendor_list_sorted = sorted(list(vendors_in_pr))

    pivot_items = df_m[["item_id", "Barang", "Qty", "UOM"]].drop_duplicates(subset=["Barang"]).reset_index(drop=True)

    price_lookup = {}
    recommended_vendor_per_item = {}
    highlight_map = {}   # {Barang: [vendor terpilih]} -> dipakai highlight hijau di tabel & PDF
    recommended_total = 0
    worst_case_total = 0

    # Split Qty hanya aktif di mode Split Qty; mode lain: abaikan data split yang pernah tersimpan
    split_item_ids = [r["item_id"] for r in pivot_items.to_dict("records")]
    if split_mode:
        split_toggle_map = get_split_toggle_map(split_item_ids)
        split_allocation_map = get_split_allocation_map(split_item_ids)
    else:
        split_toggle_map, split_allocation_map = {}, {}

    # Mode Total Termurah: pilih 1 vendor dengan total semua item paling murah
    total_winner = None
    if total_mode:
        n_items = len(pivot_items)
        vendor_totals = df_m.groupby("vendor").agg(total=("total", "sum"), n=("Barang", "nunique"))
        full_coverage = vendor_totals[vendor_totals["n"] >= n_items]
        pool = full_coverage if not full_coverage.empty else vendor_totals
        total_winner = pool["total"].idxmin()
        if full_coverage.empty:
            st.warning("⚠️ Tidak ada vendor yang menawar SEMUA item. Pemenang dipilih dari total terendah, tapi sebagian item tidak tercover.")

    for idx, r in pivot_items.iterrows():
        item_id = r["item_id"]
        rows_for_item = df_m[df_m["Barang"] == r["Barang"]].copy()
        rows_for_item = rows_for_item.rename(columns={
            "price": "unit_price", "lead_time": "lead_time_days",
        })

        is_split = bool(split_toggle_map.get(item_id))

        if is_split and split_allocation_map.get(item_id):
            # Split Qty: total item dihitung dari alokasi % manual PIC, bukan single winner
            allocs = split_allocation_map[item_id]
            recommended_vendor_per_item[r["Barang"]] = "🔀 Split (" + ", ".join(a["vendor_name"] for a in allocs) + ")"
            highlight_map[r["Barang"]] = [a["vendor_name"] for a in allocs]
            for a in allocs:
                match = rows_for_item[rows_for_item["vendor"] == a["vendor_name"]]
                if not match.empty:
                    unit_price = float(match.iloc[0]["unit_price"])
                    alloc_qty = float(r["Qty"] or 0) * (a["percentage"] / 100.0)
                    recommended_total += unit_price * alloc_qty
            if not rows_for_item.empty:
                worst_case_total += float(rows_for_item["unit_price"].max()) * float(r["Qty"] or 0)
        elif total_mode:
            match = rows_for_item[rows_for_item["vendor"] == total_winner]
            if not match.empty:
                recommended_vendor_per_item[r["Barang"]] = total_winner
                highlight_map[r["Barang"]] = [total_winner]
                recommended_total += float(match.iloc[0]["unit_price"]) * float(r["Qty"] or 0)
            if not rows_for_item.empty:
                worst_case_total += float(rows_for_item["unit_price"].max()) * float(r["Qty"] or 0)
        else:
            scored = compute_recommendation(rows_for_item, w_price, w_top, w_leadtime)
            best_row = scored[scored["is_recommended"]].iloc[0] if not scored.empty and scored["is_recommended"].any() else None
            if best_row is not None:
                recommended_vendor_per_item[r["Barang"]] = best_row["vendor"]
                highlight_map[r["Barang"]] = [best_row["vendor"]]
                recommended_total += float(best_row["unit_price"]) * float(r["Qty"] or 0)
            if not rows_for_item.empty:
                worst_case_total += float(rows_for_item["unit_price"].max()) * float(r["Qty"] or 0)

        for v in vendor_list_sorted:
            match = df_m[(df_m["Barang"] == r["Barang"]) & (df_m["vendor"] == v)]
            price_lookup[(r["Barang"], v)] = float(match.iloc[0]["price"]) if not match.empty else None

    display_df = pivot_items.drop(columns=["item_id"]).copy()
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

    grand_total_row = {"Barang": "GRAND TOTAL", "Qty": "", "UOM": ""}
    for v in vendor_list_sorted:
        vendor_total = df_m[df_m["vendor"] == v]["total"].sum()
        grand_total_row[f"{v} — Price/Unit"] = ""
        grand_total_row[f"{v} — Total"] = f"Rp {vendor_total:,.0f}".replace(",", ".")
    grand_total_row["🏆 Rekomendasi"] = f"Rp {recommended_total:,.0f}".replace(",", ".")
    display_df = pd.concat([display_df, pd.DataFrame([grand_total_row])], ignore_index=True)

    grand_total_vendors = [total_winner] if (total_mode and total_winner) else []

    def highlight_recommended_cells(row):
        if row["Barang"] == "GRAND TOTAL":
            styles = ["font-weight: bold; background-color: #f1f5f9;"] * len(row)
            for i, col in enumerate(row.index):
                if any(col == f"{gv} — Total" for gv in grand_total_vendors):
                    styles[i] = "font-weight: bold; background-color: #d1fae5;"
            return styles
        styles = [""] * len(row)
        best_vendors = highlight_map.get(row["Barang"], [])
        for i, col in enumerate(row.index):
            if best_vendors and col == "🏆 Rekomendasi":
                styles[i] = "background-color: #d1fae5; font-weight: 600;"
            for bv in best_vendors:
                if col in (f"{bv} — Price/Unit", f"{bv} — Total"):
                    styles[i] = "background-color: #d1fae5; font-weight: 600;"
        return styles

    st.markdown("##### 📋 Price Comparison")
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
    c_save1.metric("💰 Potensi Cost Saving", f"Rp {cost_saving:,.0f}".replace(",", "."), f"{saving_pct:.1f}% dari highest quote")
    c_save2.metric("🎯 Total Estimasi Amount", f"Rp {recommended_total:,.0f}".replace(",", "."))

    # -----------------------------------------------------------------
    # 📅 MASA BERLAKU PENAWARAN per vendor (di bawah tabel harga)
    # -----------------------------------------------------------------
    st.markdown("##### 📅 Masa Berlaku Penawaran per Vendor")
    df_validity = build_validity_summary(df_m, vendor_list_sorted)
    st.dataframe(df_validity, hide_index=True, use_container_width=True)

    if any_nego_happened:
        st.markdown("##### 🤝 Perbandingan First Quote vs Final (Setelah Nego)")
        nego_rows = []
        for _, dm_row in df_m.iterrows():
            key = (dm_row["item_id"], dm_row["vendor"])
            first_price = first_quote_lookup.get(key)
            final_price = dm_row["price"]
            if first_price is None or dm_row["round"] <= 1:
                continue
            delta = final_price - first_price
            nego_rows.append({
                "Barang": dm_row["Barang"],
                "Vendor": dm_row["vendor"],
                "First Quote": f"Rp {first_price:,.0f}".replace(",", "."),
                "Final (Nego)": f"Rp {final_price:,.0f}".replace(",", "."),
                "Selisih": f"{'-' if delta < 0 else '+'}Rp {abs(delta):,.0f}".replace(",", "."),
            })
        if nego_rows:
            st.dataframe(pd.DataFrame(nego_rows), hide_index=True, use_container_width=True)
        else:
            st.caption("Nego sudah diminta, tapi vendor belum submit final quotation baru.")

    st.markdown("##### 📌 Ringkasan Spesifikasi per Vendor")
    summary_df = build_vendor_summary(df_m, vendor_list_sorted)
    st.dataframe(summary_df, hide_index=True, use_container_width=True)

    # Split Qty: HANYA muncul kalau prioritas = Split Qty
    if split_mode:
        render_multivendor_split_workspace(active_id, pivot_items, df_m, vendor_list_sorted, split_toggle_map)
        # Refresh alokasi (kalau ada perubahan baru saja disimpan)
        split_allocation_map = get_split_allocation_map(split_item_ids)

    split_data = {}
    split_alloc_rows = []   # khusus PDF: tabel Alokasi Split Qty (hanya dipakai di mode Split Qty)
    for _, r in pivot_items.iterrows():
        item_id = r["item_id"]
        if split_toggle_map.get(item_id) and split_allocation_map.get(item_id):
            for a in split_allocation_map[item_id]:
                split_alloc_rows.append({
                    "Barang": r["Barang"], "Vendor": a["vendor_name"], "Persen": float(a["percentage"]),
                    "Qty": round(float(r["Qty"] or 0) * (a["percentage"] / 100.0), 2), "UOM": r["UOM"],
                })
                match = df_m[(df_m["Barang"] == r["Barang"]) & (df_m["vendor"] == a["vendor_name"])]
                if match.empty:
                    continue
                row_val = match.iloc[0]
                alloc_qty = float(r["Qty"] or 0) * (a["percentage"] / 100.0)
                split_data.setdefault(a["vendor_name"], []).append({
                    "Barang": r["Barang"],
                    "Qty": round(alloc_qty, 2),
                    "UOM": r["UOM"],
                    "Brand": row_val["brand"],
                    "Unit Price": row_val["price"],
                    "Total": row_val["price"] * alloc_qty,
                    "Lead Time (Hari)": row_val["lead_time"],
                })
            continue
        best_v = recommended_vendor_per_item.get(r["Barang"])
        if not best_v or str(best_v).startswith("🔀"):
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
        st.markdown("##### 📦 Alokasi PO per Vendor")
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
        st.caption("💡 Semua item dari vendor yang sama — cukup 1 PO.")

    all_docs = get_vendor_documents(active_id)
    if all_docs:
        st.markdown("##### 📎 Dokumen RFQ Resmi dari Vendor")
        for d in all_docs:
            owner_name = vendor_id_to_name.get(d.get("vendor_id"), "Vendor")
            tahap = "Setelah Nego" if (d.get("stage") or 1) >= 2 else "Awal"
            c_doc1, c_doc2 = st.columns([4, 1])
            c_doc1.caption(f"📄 [{owner_name}] ({tahap}) {d['file_name']}")
            try:
                file_bytes = get_storage_file_bytes(d["file_path"])
                c_doc2.download_button(
                    "⬇️ Download",
                    file_bytes,
                    file_name=d["file_name"],
                    mime="application/pdf",
                    key=f"dl_doc_{d['id']}",
                    use_container_width=True,
                )
            except Exception:
                c_doc2.caption("⚠️ Gagal load")

    if total_mode:
        weights_dict = {"Total Harga (1 PO)": 100}
    else:
        weights_dict = {"Harga": w_price, "TOP": w_top, "Lead Time": w_leadtime}

    st.markdown("##### 🤝 Open Final Quotation (Nego)")
    nego_vendors_sel = st.multiselect(
        "Pilih vendor yang akan dimintai final quotation / negosiasi:",
        vendor_list_sorted,
        key=f"nego_sel_{active_id}",
    )
    nego_note = st.text_input("Catatan untuk vendor (opsional):", key=f"nego_note_{active_id}")
    if st.button("📨 Kirim Permintaan Nego", use_container_width=True, disabled=not nego_vendors_sel):
        name_to_id = {v: k for k, v in vendor_id_to_name.items()}
        v_ids = [name_to_id[v] for v in nego_vendors_sel if v in name_to_id]
        notified = request_nego(active_id, v_ids, nego_note)
        st.toast(f"Permintaan nego terkirim ke {notified} vendor.", icon="🤝")
        st.success(f"✅ Permintaan final quotation terkirim ke: {', '.join(nego_vendors_sel)}")
        st.rerun(scope="fragment")

    st.divider()
    render_ai_insight(
        display_df, rfq_title_active,
        weights=weights_dict,
        cost_saving=cost_saving, saving_pct=saving_pct, recommended_total=recommended_total,
    )

    ai_insight_text = st.session_state.get(f"ai_insight_{rfq_title_active}", "")
    cqr_files = build_cqr_pdf_files(
        rfq_title_active, pr_info["pr_code"], loc_active, weights_dict,
        display_df, cost_saving, saving_pct, recommended_total, ai_insight_text,
        summary_df, split_data, highlight_map, grand_total_vendors,
        df_m, split_mode, split_alloc_rows,
    )

    # -----------------------------------------------------------------
    # 📜 AWARDING: Download CQR -> Download SPK -> Upload SPK approved -> Close
    # -----------------------------------------------------------------
    st.divider()
    render_awarding_section(
        pr_info, recommended_vendor_per_item, split_toggle_map, split_allocation_map,
        pivot_items, df_m, vendor_id_to_name, cqr_files=cqr_files,
        ai_included=bool(ai_insight_text),
    )


def proc_portal_comparison():
    current_user = st.session_state["user_info"]
    current_user_id = current_user["id"]
    current_role = current_user.get("role", "proc")

    # 1. FILTER DATA PR SESUAI USER YANG LOGIN (dan BUANG yang sudah diarsipkan)
    try:
        query = sb.table("purchase_requests").select(
            "id, pr_code, location, priority_status, rfq_title, uploaded_at, uploaded_by, is_archived"
        )
        if current_role != "admin":
            query = query.eq("uploaded_by", current_user_id)

        res_pr = query.execute()
        df_pr = pd.DataFrame(res_pr.data) if res_pr.data else pd.DataFrame()
    except Exception:
        # Fallback kalau kolom is_archived/uploaded_at belum ada di DB
        query = sb.table("purchase_requests").select(
            "id, pr_code, location, priority_status, rfq_title, uploaded_by"
        )
        if current_role != "admin":
            query = query.eq("uploaded_by", current_user_id)

        res_pr = query.execute()
        df_pr = pd.DataFrame(res_pr.data) if res_pr.data else pd.DataFrame()
        if not df_pr.empty:
            df_pr["uploaded_at"] = None
            df_pr["is_archived"] = False

    if not df_pr.empty:
        if "is_archived" not in df_pr.columns:
            df_pr["is_archived"] = False
        df_pr = df_pr[df_pr["is_archived"] != True]

    active_id = st.session_state.get("active_compare_pr_id")

    # HALAMAN DETAIL
    if active_id and not df_pr.empty and active_id in df_pr["id"].values:
        pr_info = df_pr[df_pr["id"] == active_id].iloc[0]

        if st.session_state.pop("_scroll_top", False):
            scroll_to_top()

        if st.button("⬅️ Kembali ke Daftar RFQ"):
            st.session_state["active_compare_pr_id"] = None
            st.session_state["_scroll_top"] = True
            st.rerun()

        render_comparison_detail(pr_info)

    # HALAMAN LIST DAFTAR RFQ
    else:
        if st.session_state.pop("_scroll_top", False):
            scroll_to_top()
        st.header("📊 Monitoring & Price Comparison")

        if df_pr.empty:
            st.info("Belum ada data RFQ")
        else:
            st.write("Pilih salah satu RFQ di bawah untuk membuka **Halaman Detail Perbandingan**:")

            quotes_res = sb.table("quotes").select("assignment_id").execute()
            submitted_ass_ids = set([q["assignment_id"] for q in quotes_res.data]) if quotes_res.data else set()

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

            search_query = clean(st.text_input("🔍 Cari Judul RFQ / Lokasi / Vendor / Item...")).lower()

            df_pr_sorted = df_pr.copy()
            df_pr_sorted["uploaded_at"] = pd.to_datetime(df_pr_sorted.get("uploaded_at"), errors="coerce")
            df_pr_sorted = df_pr_sorted.sort_values("uploaded_at", ascending=False, na_position="last")

            st.markdown("---")

            shown_count = 0
            for _, pr_row in df_pr_sorted.iterrows():
                pr_id = pr_row["id"]
                title = pr_row.get("rfq_title") or f"PR: {pr_row['pr_code']}"
                loc = pr_row.get("location") or "-"
                prio = str(pr_row.get("priority_status") or "")
                tag_prio = "🚨 URGENT" if "URGENT" in prio.upper() else "📦 NORMAL"
                assignments_this_pr = ass_by_pr.get(pr_id, [])

                vendor_names = [(a.get("profiles") or {}).get("vendor_name", "") for a in assignments_this_pr]
                item_texts = [
                    f"{(a.get('pr_items') or {}).get('description', '')} {(a.get('pr_items') or {}).get('description2', '')}"
                    for a in assignments_this_pr
                ]
                haystack = " ".join([str(title), str(loc), str(pr_row["pr_code"])] + vendor_names + item_texts).lower()
                if search_query and search_query not in haystack:
                    continue
                shown_count += 1

                total_ass = len(assignments_this_pr)
                submitted_ass_count = sum(1 for a in assignments_this_pr if a["id"] in submitted_ass_ids)
                if total_ass == 0 or submitted_ass_count == 0:
                    status_badge = ""
                elif submitted_ass_count < total_ass:
                    status_badge = " | 🔶 Submit Sebagian"
                else:
                    status_badge = " | ✅ Sudah Submit"

                with st.container(border=True):
                    c_info, c_btn = st.columns([4, 1])

                    with c_info:
                        st.subheader(f"📋 {title}")
                        st.caption(
                            f"📍 **Lokasi:** {loc} | **Priority:** {tag_prio} | **PR Code:** {pr_row['pr_code']}{status_badge}"
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
                            st.session_state["active_compare_pr_id"] = pr_id
                            st.session_state["_scroll_top"] = True
                            st.rerun()

            if search_query and shown_count == 0:
                st.info("Tidak ada RFQ yang cocok dengan pencarian.")


def get_pending_vendors(pr_id):
    """{vendor_id: info} untuk vendor yang BELUM submit di ronde saat ini (per RFQ)."""
    res = (
        sb.table("rfq_assignments")
        .select("id, vendor_id, deadline, current_round, profiles(vendor_name, email), pr_items!inner(pr_id)")
        .eq("pr_items.pr_id", pr_id)
        .eq("status", "Open")
        .execute()
    )
    rows = res.data or []
    if not rows:
        return {}
    ass_ids = [a["id"] for a in rows]
    qres = sb.table("quotes").select("assignment_id, round").in_("assignment_id", ass_ids).execute()
    done = {(q["assignment_id"], q.get("round") or 1) for q in (qres.data or [])}

    pending = {}
    for a in rows:
        rnd = a.get("current_round") or 1
        if (a["id"], rnd) in done:
            continue
        prof = a.get("profiles") or {}
        info = pending.setdefault(a["vendor_id"], {
            "name": prof.get("vendor_name", "Vendor"),
            "emails": [clean(e).lower() for e in str(prof.get("email") or "").split(";") if clean(e)],
            "deadline": a.get("deadline"),
            "round": rnd,
            "n_items": 0,
        })
        info["n_items"] += 1
    return pending


def send_pending_reminders(pr_info, pending, vendor_ids):
    """Kirim 1 email per vendor (bukan per item). Return (list_terkirim, list_error)."""
    if "email_config" not in st.secrets:
        return [], ["Konfigurasi 'email_config' tidak ditemukan di st.secrets"]
    sender = st.secrets["email_config"].get("smtp_user", "")
    pwd = st.secrets["email_config"].get("smtp_password", "")
    rfq_title = pr_info.get("rfq_title") or pr_info["pr_code"]

    sent, errors = [], []
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender, pwd)
    except Exception as e:
        return [], [f"Gagal login SMTP: {e}"]

    try:
        for vid in vendor_ids:
            info = pending.get(vid)
            if not info or not info["emails"]:
                errors.append(f"{(info or {}).get('name', vid)}: email kosong")
                continue
            jenis = "Final Quotation (Nego)" if info["round"] > 1 else "penawaran"
            body = (
                f"Dear {info['name']},\n\n"
                f"Ini pengingat bahwa Anda belum mengirimkan {jenis} untuk RFQ berikut:\n\n"
                f"Judul RFQ: {rfq_title}\n"
                f"Jumlah item: {info['n_items']}\n"
                f"Batas Waktu: {info['deadline'] or '-'}\n\n"
                f"Silakan login & submit di portal: https://proctaco.streamlit.app/\n\n"
                f"Abaikan email ini jika Anda sudah mengirim.\n\n"
                f"Salam,\nTACO Procurement Team"
            )
            try:
                msg = MIMEMultipart()
                msg["From"] = sender
                msg["To"] = ", ".join(info["emails"])
                msg["Subject"] = f"⏰ REMINDER RFQ - TACO - {rfq_title}"
                msg.attach(MIMEText(body, "plain"))
                server.sendmail(sender, info["emails"], msg.as_string())
                sent.append(info["name"])
            except Exception as e:
                errors.append(f"{info['name']}: {e}")
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return sent, errors


def render_pending_reminder_box(pr_info):
    pending = get_pending_vendors(pr_info["id"])
    if not pending:
        return
    with st.container(border=True):
        st.markdown("##### ⏳ Vendor Belum Submit Penawaran")
        for info in pending.values():
            tag = " (🤝 final quotation nego)" if info["round"] > 1 else ""
            st.caption(f"• **{info['name']}** — {info['n_items']} item, deadline {info['deadline'] or '-'}{tag}")

        chosen = st.multiselect(
            "Kirim reminder ke:",
            options=list(pending.keys()),
            default=list(pending.keys()),
            format_func=lambda vid: pending[vid]["name"],
        )
        if st.button("🔔 Send Reminder", use_container_width=True,
                     key=f"send_remind_{pr_info['id']}", disabled=not chosen):
            with st.spinner("Mengirim reminder..."):
                sent, errs = send_pending_reminders(pr_info, pending, chosen)
            if sent:
                st.success(f"✅ Reminder terkirim ke: {', '.join(sent)}")
            for e in errs:
                st.warning(f"⚠️ {e}")
# =====================================================================
# UI: PROC - AI COST ESTIMATOR (HARGA KOMODITAS + BREAKDOWN OE)
# =====================================================================
def proc_portal_cost_estimator():
    st.header("🧮 AI Cost Estimator")
    st.caption(
        "⚠️ Semua hasil di halaman ini adalah **estimasi AI** berbasis pencarian web "
        "(harga komoditas publik & asumsi industri umum) — wajib direview manual oleh "
        "procurement, bukan harga final/mengikat."
    )

    item_name = clean(st.text_input("Nama Barang / Material", placeholder="Contoh: Bearing SKF 6205"))
    additional_desc = clean(st.text_area(
        "Deskripsi tambahan (opsional — makin detail, makin akurat pencarian AI)",
        placeholder="Contoh: dipakai di conveyor gudang, material steel, butuh tahan panas & abrasi tinggi...",
    ))

    if st.button("🔍 Cari Harga Komoditas & Breakdown OE", type="primary", disabled=not item_name):
        with st.spinner("AI sedang mencari data harga & menyusun estimasi..."):
            result, err = get_ai_cost_estimate(item_name, additional_desc)
        if err:
            st.error(f"Gagal memproses: {err}")
        elif result:
            with st.container(border=True):
                st.markdown(result)


def proc_portal_manual_input():
    st.header("✍️ Input Manual Penawaran (as Vendor)")
    st.caption(
        "Gunakan halaman ini kalau vendor kirim penawaran lewat cara lain (WhatsApp, email, telepon) "
        "dan tidak bisa login sendiri ke portal. PIC bisa upload PDF penawarannya di sini (atau isi manual "
        "tanpa PDF), dan hasilnya otomatis tercatat sebagai penawaran atas nama vendor tersebut."
    )

    res_pr = sb.table("purchase_requests").select("id, pr_code, location, rfq_title").execute()
    df_pr_all = pd.DataFrame(res_pr.data) if res_pr.data else pd.DataFrame()
    if df_pr_all.empty:
        st.info("Belum ada RFQ yang dipublish.")
        return

    df_pr_all["label"] = df_pr_all.apply(
        lambda r: (r.get("rfq_title") or r.get("pr_code")) + f" ({r.get('location') or '-'})", axis=1
    )
    sel_pr_label = st.selectbox("Pilih RFQ:", df_pr_all["label"])
    pr_row = df_pr_all[df_pr_all["label"] == sel_pr_label].iloc[0]
    pr_id = pr_row["id"]

    vendors_map = get_vendors_for_pr(pr_id)
    if not vendors_map:
        st.warning("Belum ada vendor yang di-assign ke RFQ ini.")
        return

    sel_vendor_id = st.selectbox(
        "Pilih Vendor:",
        options=list(vendors_map.keys()),
        format_func=lambda vid: vendors_map[vid],
        key=f"manual_vendor_sel_{pr_id}",
    )
    vendor_name = vendors_map[sel_vendor_id]

    assignments = get_assignments_for_pr_vendor(pr_id, sel_vendor_id)
    if not assignments:
        st.warning("Tidak ada item RFQ untuk kombinasi RFQ & vendor ini.")
        return

    current_round = max((a.get("current_round") or 1) for a in assignments)
    if current_round > 1:
        st.info(f"🤝 Vendor ini sedang di ronde nego ke-{current_round}.")

    st.divider()
    st.markdown(f"##### ✏️ Input Penawaran untuk **{vendor_name}**")

    manual_key = f"manual_{pr_id}_{sel_vendor_id}"

    _prev_q = [q for a in assignments for q in (a.get("quotes") or [])]
    _prev_q.sort(key=lambda q: q.get("round") or 1)
    prev_ref = next((q["vendor_ref_no"] for q in reversed(_prev_q)
                     if q.get("vendor_ref_no") and q["vendor_ref_no"] != "-"), "")
    prev_validity = next((q["validity_period"] for q in reversed(_prev_q)
                          if q.get("validity_period") and q["validity_period"] != "-"), "")

    c_ref1, c_ref2 = st.columns(2)
    vendor_ref_no_val = clean(c_ref1.text_input(
        "🔖 Nomor SPH Vendor (Wajib)", value=prev_ref,
        key=f"vendor_ref_{manual_key}",
        help="Nomor SPH vendor. Kalau tidak ada nomor, tulis tanggal SPH-nya.",
    ))
    validity_period_val = clean(c_ref2.text_input(
        "📅 Masa Berlaku Penawaran", value=prev_validity,
        key=f"validity_{manual_key}",
        placeholder="Contoh: 30 hari / s.d. 31 Des 2026",
    ))
    st.markdown("##### 📎 Upload PDF Quotation Vendor (Opsional — isi tabel otomatis)")
    ocr_pdf = st.file_uploader("Upload PDF penawaran vendor", type=["pdf"], key=f"ocr_pdf_{manual_key}")
    if ocr_pdf is not None and st.button("🔍 Read", key=f"ocr_btn_{manual_key}"):
        items_names = []
        for a in assignments:
            it = a.get("pr_items") or {}
            d1 = str(it.get("description") or "").strip()
            d2 = str(it.get("description2") or "").strip()
            fd = f"{d1} - {d2}" if (d1 and d2 and d1 != d2) else (d1 or d2 or "-")
            items_names.append(clean_description(fd))
        with st.spinner("AI sedang membaca PDF..."):
            extracted, ocr_err = extract_quote_from_pdf(ocr_pdf.getvalue(), items_names)
        if ocr_err:
            st.error(f"Gagal membaca PDF: {ocr_err}")
        elif extracted:
            st.session_state[f"ocr_result_{manual_key}"] = extracted
            doc_saved = upload_vendor_document(pr_id, sel_vendor_id, ocr_pdf, round_num=current_round)
            if doc_saved:
                st.success(f"✅ {len(extracted)} baris berhasil dibaca & file otomatis tersimpan sebagai dokumen resmi.")
            else:
                st.success(f"✅ {len(extracted)} baris berhasil dibaca.")
            st.rerun()
        else:
            st.warning("AI tidak menemukan data yang cocok di PDF ini.")

    ocr_result = st.session_state.get(f"ocr_result_{manual_key}", {})
    if ocr_result:
        st.caption("⚠️ Sebagian data di bawah adalah hasil bacaan dari PDF — wajib dicek ulang sebelum disimpan.")

    table_rows = []
    for a in assignments:
        item = a.get("pr_items") or {}
        d1 = str(item.get("description") or "").strip()
        d2 = str(item.get("description2") or "").strip()
        full_desc = f"{d1} - {d2}" if (d1 and d2 and d1 != d2) else (d1 or d2 or "-")
        clean_item_name = clean_description(full_desc)

        existing_quotes = a.get("quotes") or []
        this_round_quotes = [q for q in existing_quotes if (q.get("round") or 1) == current_round]
        last_quote = this_round_quotes[-1] if this_round_quotes else (existing_quotes[-1] if existing_quotes else {})

        ocr_row = ocr_result.get(clean_item_name)

        table_rows.append({
            "assignment_id": a["id"],
            "Barang": clean_item_name,
            "Spesifikasi": (ocr_row.get("spesifikasi") if ocr_row else None) or last_quote.get("spec_vendor", "-"),
            "Qty": item.get("quantity", 0),
            "UOM": item.get("uom", "-"),
            "Unit Price (IDR)": (ocr_row.get("unit_price") if ocr_row else None) or last_quote.get("unit_price", 0),
            "Brand": (ocr_row.get("brand") if ocr_row else None) or last_quote.get("brand", "-"),
            "Ready Stock": (ocr_row.get("ready_stock") if ocr_row else None) or last_quote.get("ready_stock", "Ya"),
            "Lead Time (Hari)": (ocr_row.get("lead_time_days") if ocr_row else None) or last_quote.get("lead_time_days", 7),
            "Warranty": (ocr_row.get("warranty") if ocr_row else None) or last_quote.get("warranty", "-"),
        })

    df_preview = pd.DataFrame(table_rows)

    edited = st.data_editor(
        df_preview.drop(columns=["assignment_id"]),
        key=f"editor_manual_{manual_key}",
        hide_index=True,
        use_container_width=True,
        row_height=80,
        disabled=["Barang", "Qty", "UOM"],
        column_config={
            "Barang": st.column_config.TextColumn("Barang", width=280),
            "Spesifikasi": st.column_config.TextColumn("Spesifikasi", width="medium"),
            "Qty": st.column_config.NumberColumn("Qty", width="small"),
            "UOM": st.column_config.TextColumn("UOM", width="small"),
            "Unit Price (IDR)": st.column_config.NumberColumn("Unit Price (IDR)", format="Rp %,d", min_value=0, step=1000),
            "Ready Stock": st.column_config.SelectboxColumn("Ready Stock", options=["Ya", "Tidak"], required=True),
            "Lead Time (Hari)": st.column_config.NumberColumn("Lead Time (Hari)", min_value=1, step=1),
            "Warranty": st.column_config.TextColumn("Warranty", width="small"),
        },
    )

    st.markdown("##### 📎 Dokumen Resmi Vendor (opsional)")
    st.caption("Kalau sudah otomatis tersimpan lewat proses Read di atas, langkah ini bisa dilewati.")
    official_doc = st.file_uploader("Pilih file PDF", type=["pdf"], key=f"official_doc_manual_{manual_key}")

    existing_docs = get_vendor_documents(pr_id, sel_vendor_id)
    if existing_docs:
        st.write("**Dokumen yang sudah ada:**")
        for d in existing_docs:
            tahap = "Setelah Nego" if (d.get("stage") or 1) >= 2 else "Awal"
            st.caption(f"📄 ({tahap}) {d['file_name']} — {d['uploaded_at'][:10]}")

    if st.button("💾 Simpan Penawaran Vendor Ini", type="primary", use_container_width=True):
        if not vendor_ref_no_val:
            st.error("❌ Mohon isi Nomor SPH Vendor terlebih dahulu!")
        else:
            all_ok = True
            for idx, r in edited.iterrows():
                ass_id = df_preview.iloc[idx]["assignment_id"]
                ok, err = submit_quote(
                    ass_id, sel_vendor_id,
                    r["Unit Price (IDR)"], r["Brand"], r["Lead Time (Hari)"],
                    r["Ready Stock"], r["Warranty"], r["Spesifikasi"],
                    round_num=current_round, vendor_ref_no=vendor_ref_no_val,
                    validity_period=validity_period_val,
                )
                if not ok:
                    all_ok = False
                    st.error(f"❌ Gagal menyimpan baris '{r['Barang']}': {err}")

            if all_ok:
                if official_doc is not None:
                    upload_vendor_document(pr_id, sel_vendor_id, official_doc, round_num=current_round)
                st.success(f"🎉 Penawaran atas nama {vendor_name} berhasil disimpan!")
                st.session_state.pop(f"ocr_result_{manual_key}", None)
                st.rerun()


# =====================================================================
# UI: PROC - HISTORY RFQ (hanya RFQ yang sudah di-close)
# =====================================================================
def proc_portal_history():
    st.header("🔍 History RFQ")

    df_hist = get_history_data()
    if df_hist.empty:
        st.info("Belum ada RFQ yang sudah di-close.")
        return

    df_hist["Tanggal Dibuat"] = pd.to_datetime(df_hist["Tanggal Dibuat"], errors="coerce")
    df_hist = df_hist.sort_values("Tanggal Dibuat", ascending=False, na_position="last")

    c_search, c_status = st.columns([3, 1])
    search_query = clean(c_search.text_input(
        "🔍 Cari Judul RFQ / PR Code / Lokasi / Vendor / Barang...",
        placeholder="Ketik untuk mencari...",
    )).lower()
    status_options = ["Semua Status"] + sorted(df_hist["Status"].dropna().unique().tolist())
    status_filter = c_status.selectbox("Filter Status", status_options)

    df_show = df_hist.copy()
    if search_query:
        search_cols = ["Judul RFQ", "PR Code", "Lokasi", "Barang", "Vendor", "Email Vendor"]
        mask = pd.Series(False, index=df_show.index)
        for col in search_cols:
            if col in df_show.columns:
                mask = mask | df_show[col].astype(str).str.lower().str.contains(search_query, na=False, regex=False)
        df_show = df_show[mask]
    if status_filter != "Semua Status":
        df_show = df_show[df_show["Status"] == status_filter]

    df_show_display = df_show.copy()
    df_show_display["Tanggal Dibuat"] = df_show_display["Tanggal Dibuat"].dt.strftime("%d %b %Y %H:%M").fillna("-")

    st.caption(f"Menampilkan {len(df_show_display)} dari {len(df_hist)} baris riwayat.")
    st.dataframe(
        df_show_display,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Judul RFQ": st.column_config.TextColumn("Judul RFQ", width="medium"),
            "Barang": st.column_config.TextColumn("Barang", width="medium"),
            "Status": st.column_config.TextColumn("Status", width="small"),
        },
    )


# =====================================================================
# UI: ADMIN UTILS (REGISTRATION & ACCOUNT MANAGEMENT)
# =====================================================================
def admin_portal_register_pic():
    st.header("➕ Daftarkan PIC Procurement")
    sub1, sub2, sub3 = st.tabs(["Satu-satu", "Bulk (Excel/CSV)", "Atur Manager & Chief"])
    with sub1:
        with st.form("form_register_pic", clear_on_submit=True):
            p_name = clean(st.text_input("Nama PIC"))
            p_email = clean(st.text_input("Email PIC")).lower()
            p_manager = clean(st.text_input("Nama Manager PIC (TTD SPK ≤ Rp 50 jt)"))
            p_manager_t = clean(st.text_input("Jabatan Manager (mis. Procurement Chemical Manager)"))
            p_chief = clean(st.text_input("Nama Chief PIC (TTD SPK > Rp 50 jt)"))
            p_chief_t = clean(st.text_input("Jabatan Chief (mis. Procurement Chief)"))
            pw_mode = st.radio("Password:", ["Generate otomatis (random)", "Ketik manual"], key="pic_pw_mode")
            p_password_manual = (
                clean(st.text_input("Password (min. 6 karakter):", type="password", key="pic_pw_manual"))
                if pw_mode == "Ketik manual" else None
            )
            submitted = st.form_submit_button("Simpan PIC Baru", type="primary")
            if submitted:
                if not p_name or not p_email or "@" not in p_email:
                    st.session_state["last_pic_single_result"] = {"ok": False, "msg": "❌ Nama/Email tidak valid."}
                elif pw_mode == "Ketik manual" and (not p_password_manual or len(p_password_manual) < 6):
                    st.session_state["last_pic_single_result"] = {"ok": False, "msg": "❌ Password minimal 6 karakter."}
                else:
                    final_password = (
                        p_password_manual if pw_mode == "Ketik manual"
                        else "".join(random.choices(string.ascii_letters + string.digits, k=10))
                    )
                    ok, err = register_user(p_name, p_email, final_password, "proc", manager_name=p_manager, chief_name=p_chief, manager_title=p_manager_t, chief_title=p_chief_t)
                    if ok:
                        st.session_state["last_pic_single_result"] = {
                            "ok": True, "name": p_name, "email": p_email, "password": final_password,
                        }
                    else:
                        st.session_state["last_pic_single_result"] = {"ok": False, "msg": f"❌ Gagal: {err}"}

        result = st.session_state.get("last_pic_single_result")
        if result:
            if result["ok"]:
                st.success(f"🎉 PIC {result['name']} berhasil didaftarkan.")
                st.info(f"📧 Email: `{result['email']}`\n\n🔑 Password: `{result['password']}` — catat & kirim manual ke PIC ybs.")
            else:
                st.error(result["msg"])

    with sub2:
        st.write("Upload file Excel/CSV dengan kolom: **name**, **email**, **manager**, **manager_title**, **chief**, **chief_title** (boleh dikosongkan, bisa diisi nanti di tab *Atur Manager & Chief*).")
        bulk_file = st.file_uploader("Upload file", type=["xlsx", "csv"], key="bulk_pic")
        if bulk_file is not None:
            df_bulk = pd.read_csv(bulk_file) if bulk_file.name.endswith(".csv") else pd.read_excel(bulk_file)
            st.dataframe(df_bulk, use_container_width=True, hide_index=True)
            if st.button("🚀 Daftarkan Semua PIC Ini", type="primary"):
                result_df = bulk_register_users(df_bulk, "proc")
                st.session_state["last_pic_bulk_result"] = result_df

        bulk_result = st.session_state.get("last_pic_bulk_result")
        if bulk_result is not None and not bulk_result.empty:
            st.success("Selesai! Cek hasil, email & password di bawah.")
            st.dataframe(
                bulk_result[["name", "email", "password", "status"]],
                use_container_width=True,
                hide_index=True,
            )

    with sub3:
        st.caption("Aturan TTD SPK: total ≤ Rp 50.000.000 → Manager PIC, di atas itu → Chief PIC.")
        df_pic = get_users_by_role("proc")
        if df_pic.empty:
            st.info("Belum ada PIC terdaftar.")
        else:
            appr_map = get_pic_approvers_map()
            for col in APPROVER_COLS:
                df_pic[col] = df_pic["id"].map(lambda i, c=col: (appr_map.get(i) or {}).get(c))
            with st.expander("📤 Upload massal Manager & Chief (CSV/Excel) — update berdasarkan email PIC"):
                tmpl_df = pd.DataFrame([{
                    "email": "pic@taco.co.id",
                    "manager_name": "Nama Manager",
                    "manager_title": "Procurement Chemical Manager",
                    "chief_name": "Nama Chief",
                    "chief_title": "Procurement Chief",
                }])
                st.download_button(
                    "⬇️ Download template CSV", tmpl_df.to_csv(index=False).encode("utf-8-sig"),
                    "template_pic_approvers.csv", mime="text/csv", key="dl_tmpl_pic_appr",
                )
                st.caption("Kolom wajib: **email** (email PIC yang sudah terdaftar). Kolom lain boleh dikosongkan; sel kosong = nilai lama dihapus.")
                up_appr = st.file_uploader("Upload file", type=["csv", "xlsx"], key="up_pic_appr")
                if up_appr is not None:
                    df_up = pd.read_csv(up_appr) if up_appr.name.lower().endswith(".csv") else pd.read_excel(up_appr)
                    df_up.columns = [clean(c).lower() for c in df_up.columns]
                    df_up = df_up.rename(columns={"manager": "manager_name", "chief": "chief_name",
                                                  "jabatan_manager": "manager_title", "jabatan_chief": "chief_title"})
                    for col in APPROVER_COLS:
                        if col not in df_up.columns:
                            df_up[col] = ""
                    st.dataframe(df_up[["email"] + APPROVER_COLS], hide_index=True, use_container_width=True)
                    if st.button("🚀 Simpan semua", type="primary", key="btn_save_pic_appr_bulk"):
                        email_to_id = {}
                        for _, pr_row in df_pic.iterrows():
                            for em in str(pr_row["email"]).split(";"):
                                if clean(em):
                                    email_to_id[clean(em).lower()] = pr_row["id"]
                        results = []
                        for _, ur in df_up.iterrows():
                            em = clean(ur.get("email")).lower()
                            pid = email_to_id.get(em)
                            if not pid:
                                results.append({"email": em, "status": "❌ Email PIC tidak ditemukan"})
                                continue
                            ok, err = save_pic_approvers(pid, ur["manager_name"], ur["manager_title"], ur["chief_name"], ur["chief_title"])
                            results.append({"email": em, "status": "✅ Tersimpan" if ok else f"❌ {err}"})
                        st.dataframe(pd.DataFrame(results), hide_index=True, use_container_width=True)

            pic_opts = {f"{r['vendor_name']} ({r['email']})": r for _, r in df_pic.iterrows()}
            sel = st.selectbox("Pilih PIC", list(pic_opts.keys()), key="sel_pic_approver")
            row = pic_opts[sel]
            with st.form(f"form_pic_approver_{row['id']}"):
                new_mgr = clean(st.text_input("Nama Manager", value=clean(row.get("manager_name"))))
                new_mgr_t = clean(st.text_input("Jabatan Manager", value=clean(row.get("manager_title"))))
                new_chf = clean(st.text_input("Nama Chief", value=clean(row.get("chief_name"))))
                new_chf_t = clean(st.text_input("Jabatan Chief", value=clean(row.get("chief_title"))))
                if st.form_submit_button("💾 Simpan", type="primary"):
                    ok, err = save_pic_approvers(row["id"], new_mgr, new_mgr_t, new_chf, new_chf_t)
                    if ok:
                        st.success("✅ Tersimpan.")
                    else:
                        st.error(f"Gagal menyimpan (sudah jalanin SQL bikin tabel pic_approvers?): {err}")


def admin_portal_register_vendor():
    st.header("➕ Daftarkan Vendor")
    sub1, sub2 = st.tabs(["Satu-satu", "Bulk (Excel/CSV)"])
    with sub1:
        with st.form("form_register_vendor", clear_on_submit=True):
            v_name = clean(st.text_input("Nama Vendor"))
            v_email = clean(st.text_input("Email Vendor")).lower()
            submitted = st.form_submit_button("Simpan Vendor Baru", type="primary")
            if submitted:
                if not v_name or not v_email or "@" not in v_email:
                    st.session_state["last_vendor_single_result"] = {"ok": False, "msg": "❌ Nama/Email tidak valid."}
                else:
                    auto_password = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                    ok, err = register_user(v_name, v_email, auto_password, "vendor")
                    if ok:
                        get_vendors_cached.clear()
                        st.session_state["last_vendor_single_result"] = {"ok": True, "name": v_name}
                    else:
                        st.session_state["last_vendor_single_result"] = {"ok": False, "msg": f"❌ Gagal: {err}"}

        result = st.session_state.get("last_vendor_single_result")
        if result:
            if result["ok"]:
                st.success(
                    f"🎉 Vendor {result['name']} berhasil didaftarkan. "
                    "Password akan otomatis disertakan saat Anda mengirim undangan RFQ pertama ke vendor ini "
                    "(centang opsi 'Reset & sertakan password' di halaman Import PR List)."
                )
            else:
                st.error(result["msg"])

    with sub2:
        st.write("Upload file Excel/CSV dengan 2 kolom: **name** dan **email**.")
        bulk_file = st.file_uploader("Upload file", type=["xlsx", "csv"], key="bulk_vendor")
        if bulk_file is not None:
            df_bulk = pd.read_csv(bulk_file) if bulk_file.name.endswith(".csv") else pd.read_excel(bulk_file)
            st.dataframe(df_bulk, use_container_width=True, hide_index=True)
            if st.button("🚀 Daftarkan Semua Vendor Ini", type="primary"):
                result_df = bulk_register_users(df_bulk, "vendor")
                get_vendors_cached.clear()
                # Simpan cuma nama, email, status -- password TIDAK disimpan/ditampilkan di sini
                st.session_state["last_vendor_bulk_result"] = result_df[["name", "email", "status"]]

        bulk_result = st.session_state.get("last_vendor_bulk_result")
        if bulk_result is not None and not bulk_result.empty:
            st.success(
                "Selesai! Vendor berhasil didaftarkan. Password akan otomatis disertakan "
                "saat Anda mengirim undangan RFQ pertama ke masing-masing vendor."
            )
            st.dataframe(bulk_result, use_container_width=True, hide_index=True)


def admin_portal_user_list():
    st.header("👥 Daftar Semua User")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**PIC Procurement**")
        df_proc = get_users_by_role("proc")
        if not df_proc.empty:
            _am = get_pic_approvers_map()
            for col in APPROVER_COLS:
                df_proc[col] = df_proc["id"].map(lambda i, c=col: (_am.get(i) or {}).get(c))
        st.dataframe(
            df_proc.reindex(columns=["email", "vendor_name", "manager_name", "manager_title", "chief_name", "chief_title", "created_at"]) if not df_proc.empty else pd.DataFrame(),
            hide_index=True, use_container_width=True,
        )
    with c2:
        st.markdown("**Vendor**")
        df_v = get_vendors_cached()
        st.dataframe(
            df_v[["email", "vendor_name", "created_at"]] if not df_v.empty else pd.DataFrame(),
            hide_index=True, use_container_width=True,
        )


def admin_portal_reset_password():
    st.header("🔑 Reset Password User (Admin Only)")

    df_proc = get_users_by_role("proc")
    df_vend = get_vendors_cached()
    df_all = pd.concat([df_proc, df_vend], ignore_index=True) if not df_proc.empty or not df_vend.empty else pd.DataFrame()

    if df_all.empty:
        st.info("Belum ada user terdaftar.")
    else:
        df_all["label"] = df_all["vendor_name"].fillna("-") + " (" + df_all["email"] + ") — " + df_all["role"].str.upper()
        sel_label = st.selectbox("Pilih User / Vendor:", df_all["label"])
        sel_row = df_all[df_all["label"] == sel_label].iloc[0]

        mode = st.radio("Password baru:", ["Generate otomatis (random)", "Ketik manual"])
        new_pw = clean(st.text_input("Password baru (min. 6 karakter):", type="password")) if mode == "Ketik manual" else None

        if st.button("🔄 Reset Password", type="primary"):
            final_pw = new_pw if mode == "Ketik manual" else "".join(random.choices(string.ascii_letters + string.digits, k=10))
            if mode == "Ketik manual" and (not new_pw or len(new_pw) < 6):
                st.error("❌ Password minimal 6 karakter.")
            else:
                ok, err = reset_user_password(sel_row["id"], final_pw)
                if ok:
                    st.success(f"🎉 Password untuk **{sel_row['email']}** berhasil direset.")
                    st.info(f"🔑 Password baru: `{final_pw}` — silakan berikan password ini kepada user bersangkutan.")
                else:
                    st.error(f"❌ Gagal: {err}")

def _group_is_submitted(group):
    """Submitted = SEMUA item sudah punya quote di ronde saat ini.
    Kalau PIC minta nego (ronde naik), otomatis balik ke tab 'Belum Submit'."""
    cur = max((a.get("current_round") or 1) for a in group["rows"])
    for a in group["rows"]:
        rounds = [(q.get("round") or 1) for q in (a.get("quotes") or [])]
        if cur not in rounds:
            return False
    return True


def _render_vendor_rfq_cards(groups, tab_tag, v_search):
    shown = 0
    for pr_id, group in groups.items():
        prio_tag = "🚨 URGENT" if "URGENT" in group["prio"].upper() else "📦 NORMAL"
        item_texts = [
            f"{(a.get('pr_items') or {}).get('description', '')} {(a.get('pr_items') or {}).get('description2', '')}"
            for a in group["rows"]
        ]
        haystack = " ".join([group["title"], group["location"], group["pic_name"], group["pr_code"]] + item_texts).lower()
        if v_search and v_search not in haystack:
            continue
        shown += 1

        cur = max((a.get("current_round") or 1) for a in group["rows"])
        any_quote = any(a.get("quotes") for a in group["rows"])
        if _group_is_submitted(group):
            badge = " | ✅ Sudah Submit"
        elif cur > 1:
            badge = f" | 🤝 Ronde Nego ke-{cur} (perlu final quotation)"
        elif any_quote:
            badge = " | 🔶 Submit Sebagian"
        else:
            badge = ""

        with st.container(border=True):
            c_info, c_btn = st.columns([4, 1])
            with c_info:
                st.subheader(f"📋 {group['title']}")
                st.caption(
                    f"👤 **PIC Procurement:** {group['pic_name']} | 📍 **Lokasi:** {group['location']} "
                    f"| **Priority:** {prio_tag} | **PR Code:** {group['pr_code']}{badge}"
                )
            with c_btn:
                st.write(" ")
                if st.button("🔍 Buka Detail", key=f"v_detail_{tab_tag}_{pr_id}", type="primary", use_container_width=True):
                    st.session_state["active_vendor_rfq_id"] = pr_id
                    st.session_state["_scroll_top"] = True
                    st.rerun()
    return shown

# =====================================================================
# UI: VENDOR PORTAL
# =====================================================================
def vendor_portal(vendor_id):
    if "vendor_page" not in st.session_state:
        st.session_state["vendor_page"] = "List RFQ Aktif"

    st.sidebar.markdown("## 🧭 Navigasi Menu")
    st.sidebar.markdown("---")

    v_menus = [
        ("⚙️ Data Perusahaan", "v_supplier"),
        ("📋 List RFQ Aktif", "v_rfq"),
        ("🔍 History Penawaran", "v_history"),
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

            c_pic1, c_pic2 = st.columns(2)
            new_pic_name = c_pic1.text_input(
                "Nama PIC / Penandatangan",
                value=p_data.get("pic_name") or "",
                help="Nama yang akan muncul di kolom tanda tangan Surat Perintah Kerja.",
            )
            new_pic_jabatan = c_pic2.text_input(
                "Jabatan",
                value=p_data.get("pic_jabatan") or "",
                placeholder="Contoh: Direktur",
            )

            if st.button("Simpan Data Supplier", type="primary"):
                update_vendor_profile_info(vendor_id, new_top, new_pic_name, new_pic_jabatan)
                st.success("Data supplier berhasil diperbarui!")
                st.rerun()

    # -----------------------------------------------------------------
    # MENU 2: LIST RFQ AKTIF
    # -----------------------------------------------------------------
    elif selected_v_page == "List RFQ Aktif":
        assignments = get_vendor_assignments(vendor_id)

        if not assignments:
            st.info("Belum ada undangan RFQ aktif untuk Anda saat ini.")
            return

        pr_groups = {}
        for a in assignments:
            item = a.get("pr_items") or {}
            pr = item.get("purchase_requests") or {}

            pic_profile = pr.get("profiles") or {}
            pic_name = pic_profile.get("vendor_name") or pic_profile.get("email") or "Procurement Team"

            rfq_title = pr.get("rfq_title") or f"PR: {pr.get('pr_code', '-')}"

            pr_groups.setdefault(pr.get("id"), {
                "title": rfq_title,
                "pr_code": pr.get("pr_code", "-"),
                "location": pr.get("location", "-"),
                "prio": str(pr.get("priority_status", "")),
                "pic_name": pic_name,
                "created_at": pr.get("uploaded_at"),
                "rows": []
            })
            pr_groups[pr.get("id")]["rows"].append(a)

        pr_groups = dict(
            sorted(
                pr_groups.items(),
                key=lambda kv: pd.to_datetime(kv[1].get("created_at"), errors="coerce") or pd.Timestamp.min,
                reverse=True,
            )
        )

        active_rfq_id = st.session_state.get("active_vendor_rfq_id")

        if active_rfq_id and active_rfq_id in pr_groups:
            group = pr_groups[active_rfq_id]
            if st.session_state.pop("_scroll_top", False):
                scroll_to_top()

            if st.button("⬅️ Kembali ke Daftar RFQ Aktif"):
                st.session_state["active_vendor_rfq_id"] = None
                st.rerun()

            st.title(f"📝 Penawaran Harga: {group['title']}")
            st.caption(f"👤 **PIC Procurement:** {group['pic_name']} | 📍 **Lokasi:** {group['location']} | **No. PR:** {group['pr_code']}")

            current_round = max((a.get("current_round") or 1) for a in group["rows"])
            if current_round > 1:
                st.warning(f"🤝 **Ronde Nego ke-{current_round}** — PIC meminta Anda mengirimkan Final Quotation. Harga di bawah adalah penawaran pertama Anda sebagai referensi, silakan update ke harga terbaik.")
            supplier_ok, supplier_missing = get_supplier_completeness(vendor_id)
            if not supplier_ok:
                st.warning(
                    "⚠️ **Data Supplier belum lengkap** (" + ", ".join(supplier_missing) + "). "
                    "Lengkapi dulu agar bisa mengirim penawaran."
                )
                if st.button("⚙️ Lengkapi Data Supplier", key=f"goto_supplier_{active_rfq_id}"):
                    st.session_state["vendor_page"] = "Data Supplier"
                    st.session_state["active_vendor_rfq_id"] = None
                    st.rerun()
            st.divider()

            alamat_kirim = lookup_warehouse_address(group["location"])
            st.markdown("##### 📍 Alamat Pengiriman / Gudang:")
            st.info(alamat_kirim.replace("\n", "  \n"))

            st.markdown("##### ✏️ Masukkan Harga & Detail Penawaran:")
            attachments = get_pr_attachments(active_rfq_id)
            if attachments:
                st.markdown("**📎 File Referensi Lampiran:** " + ", ".join(f"`{a['file_name']}`" for a in attachments))

            _prev_q = [q for a in group["rows"] for q in (a.get("quotes") or [])]
            _prev_q.sort(key=lambda q: q.get("round") or 1)
            prev_ref = next((q["vendor_ref_no"] for q in reversed(_prev_q)
                             if q.get("vendor_ref_no") and q["vendor_ref_no"] != "-"), "")
            prev_validity = next((q["validity_period"] for q in reversed(_prev_q)
                                  if q.get("validity_period") and q["validity_period"] != "-"), "")

            c_ref1, c_ref2 = st.columns(2)
            vendor_ref_no_val = clean(c_ref1.text_input(
                "🔖 Nomor SPH / Tanggal SPH (Wajib)",
                value=prev_ref,
                key=f"vendor_ref_{active_rfq_id}",
                help="Isi nomor SPH (Surat Penawaran Harga) Anda. Kalau tidak ada nomor, tulis tanggal SPH-nya.",
            ))
            validity_period_val = clean(c_ref2.text_input(
                "📅 Masa Berlaku Penawaran",
                value=prev_validity,
                key=f"validity_{active_rfq_id}",
                placeholder="Contoh: 30 hari / s.d. 31 Des 2026",
            ))

            st.markdown("##### 📎 Upload PDF Quotation (Opsional — isi tabel otomatis)")
            ocr_pdf = st.file_uploader(
                "Upload PDF penawaran untuk dicopy ke tabel", type=["pdf"], key=f"ocr_pdf_{active_rfq_id}"
            )
            if ocr_pdf is not None and st.button("🔍 Read", key=f"ocr_btn_{active_rfq_id}"):
                items_names = []
                for a in group["rows"]:
                    it = a.get("pr_items") or {}
                    d1 = str(it.get("description") or "").strip()
                    d2 = str(it.get("description2") or "").strip()
                    fd = f"{d1} - {d2}" if (d1 and d2 and d1 != d2) else (d1 or d2 or "-")
                    items_names.append(clean_description(fd))
                with st.spinner("AI sedang membaca PDF..."):
                    extracted, ocr_err = extract_quote_from_pdf(ocr_pdf.getvalue(), items_names)
                if ocr_err:
                    st.error(f"Gagal membaca PDF: {ocr_err}")
                elif extracted:
                    st.session_state[f"ocr_result_{active_rfq_id}"] = extracted
                    doc_saved = upload_vendor_document(active_rfq_id, vendor_id, ocr_pdf, round_num=current_round)
                    if doc_saved:
                        st.success(
                            f"✅ {len(extracted)} baris berhasil dibaca & file otomatis tersimpan sebagai dokumen resmi. "
                            "Cek & koreksi di tabel di bawah sebelum kirim."
                        )
                    else:
                        st.success(f"✅ {len(extracted)} baris berhasil dibaca. Cek & koreksi di tabel di bawah sebelum kirim.")
                    st.rerun()
                else:
                    st.warning("AI tidak menemukan data yang cocok di PDF ini.")

            ocr_result = st.session_state.get(f"ocr_result_{active_rfq_id}", {})
            if ocr_result:
                st.caption(
                    "⚠️ Sebagian data di bawah adalah hasil bacaan dari PDF — **wajib dicek ulang**, "
                )

            table_rows = []
            for a in group["rows"]:
                item = a.get("pr_items") or {}

                d1 = str(item.get("description") or "").strip()
                d2 = str(item.get("description2") or "").strip()
                full_desc = f"{d1} - {d2}" if (d1 and d2 and d1 != d2) else (d1 or d2 or "-")
                clean_item_name = clean_description(full_desc)

                existing_quotes = a.get("quotes") or []
                this_round_quotes = [q for q in existing_quotes if (q.get("round") or 1) == current_round]
                last_quote = this_round_quotes[-1] if this_round_quotes else (existing_quotes[-1] if existing_quotes else {})

                ocr_row = ocr_result.get(clean_item_name)

                table_rows.append({
                    "assignment_id": a["id"],
                    "Barang": clean_item_name,
                    "Spesifikasi": (ocr_row.get("spesifikasi") if ocr_row else None) or last_quote.get("spec_vendor", "-"),
                    "Qty": item.get("quantity", 0),
                    "UOM": item.get("uom", "-"),
                    "Unit Price (IDR)": (ocr_row.get("unit_price") if ocr_row else None) or last_quote.get("unit_price", 0),
                    "Brand": (ocr_row.get("brand") if ocr_row else None) or last_quote.get("brand", "-"),
                    "Ready Stock": (ocr_row.get("ready_stock") if ocr_row else None) or last_quote.get("ready_stock", "Ya"),
                    "Lead Time (Hari)": (ocr_row.get("lead_time_days") if ocr_row else None) or last_quote.get("lead_time_days", 7),
                    "Warranty": (ocr_row.get("warranty") if ocr_row else None) or last_quote.get("warranty", "-"),
                })

            df_preview = pd.DataFrame(table_rows)

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

            edited = st.data_editor(
                df_preview.drop(columns=["assignment_id"]),
                key=f"editor_v_{active_rfq_id}",
                hide_index=True,
                use_container_width=True,
                row_height=80,
                disabled=["Barang", "Qty", "UOM"],
                column_config={
                    "Barang": st.column_config.TextColumn(
                        "Barang",
                        width=280,
                        help="Nama Barang & Deskripsi Utama"
                    ),
                    "Spesifikasi": st.column_config.TextColumn(
                        "Spesifikasi",
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
            all_q = [q for a in group["rows"] for q in (a.get("quotes") or [])]
            all_q.sort(key=lambda q: q.get("round") or 1)
            prev_tax = next((q["tax_type"] for q in reversed(all_q) if q.get("tax_type")), None)
            tax_options = ["Include", "Exclude"]
            tax_val = st.radio(
                "🧾 Harga di atas sudah termasuk PPN? (Wajib)",
                tax_options,
                index=tax_options.index(prev_tax) if prev_tax in tax_options else None,
                horizontal=True,
                key=f"tax_type_{active_rfq_id}",
                help="Include = harga sudah termasuk PPN. Exclude = harga belum termasuk PPN.",
            )


            st.markdown("##### 📎 Upload RFQ Resmi / Surat Penawaran")
            st.caption(
                "File sudah otomatis tersimpan dari proses Read di atas, langkah ini opsional — "
                "upload di sini hanya jika Anda ingin mengganti dengan file yang berbeda "
                "(yang tersimpan hanya file terakhir per tahap: awal / setelah nego)."
            )
            official_doc = st.file_uploader("Pilih file PDF", type=["pdf"], key=f"official_doc_{active_rfq_id}")

            existing_docs = get_vendor_documents(active_rfq_id, vendor_id)
            if existing_docs:
                st.write("**Dokumen yang sudah diupload:**")
                for d in existing_docs:
                    tahap = "Setelah Nego" if (d.get("stage") or 1) >= 2 else "Awal"
                    st.caption(f"📄 ({tahap}) {d['file_name']} — {d['uploaded_at'][:10]}")

            if st.button("🚀 Kirim Penawaran", type="primary", use_container_width=True, disabled=not supplier_ok):
                has_doc = official_doc is not None or bool(existing_docs)
                if not vendor_ref_no_val:
                    st.error("❌ Mohon isi Nomor SPH Anda dulu sebelum mengirim penawaran.")
                elif not tax_val:
                    st.error("❌ Mohon pilih Include / Exclude PPN dulu sebelum mengirim penawaran.")
                elif not has_doc:
                    st.error("❌ Mohon upload PDF quotation resmi (kop surat/tandatangan) dulu sebelum mengirim penawaran.")
                else:
                    all_ok = True
                    for idx, r in edited.iterrows():
                        ass_id = df_preview.iloc[idx]["assignment_id"]
                        ok, err = submit_quote(
                            ass_id, vendor_id,
                            r["Unit Price (IDR)"], r["Brand"], r["Lead Time (Hari)"],
                            r["Ready Stock"], r["Warranty"], r["Spesifikasi"],
                            round_num=current_round, vendor_ref_no=vendor_ref_no_val,
                            validity_period=validity_period_val,
                            tax_type=tax_val,
                        )

                        if not ok:
                            all_ok = False
                            st.error(f"❌ Gagal menyimpan baris '{r['Barang']}': {err}")

                    if all_ok:
                        if official_doc is not None:
                            upload_vendor_document(active_rfq_id, vendor_id, official_doc, round_num=current_round)
                        st.session_state["_vendor_flash"] = f"🎉 Penawaran untuk '{group['title']}' berhasil dikirim!"
                        st.toast("Penawaran berhasil dikirim!", icon="✅")
                        st.session_state["active_vendor_rfq_id"] = None
                        st.rerun()

        else:
            st.header("📋 List RFQ Aktif")
        
            flash = st.session_state.pop("_vendor_flash", None)
            if flash:
                st.success(flash)
        
            st.write("Klik **Buka Detail** untuk mengisi / merevisi penawaran harga:")
            v_search = clean(st.text_input("🔍 Cari Judul RFQ / Lokasi / PIC / Item...")).lower()
            st.markdown("---")
        
            pending_groups = {k: g for k, g in pr_groups.items() if not _group_is_submitted(g)}
            done_groups = {k: g for k, g in pr_groups.items() if _group_is_submitted(g)}
        
            tab_pending, tab_done = st.tabs([
                f"⏳ Belum Submit ({len(pending_groups)})",
                f"✅ Sudah Submit ({len(done_groups)})",
            ])
            with tab_pending:
                if not pending_groups:
                    st.info("Semua RFQ sudah Anda submit 🎉")
                elif _render_vendor_rfq_cards(pending_groups, "pending", v_search) == 0:
                    st.info("Tidak ada RFQ yang cocok dengan pencarian.")
            with tab_done:
                if not done_groups:
                    st.info("Belum ada RFQ yang disubmit.")
                elif _render_vendor_rfq_cards(done_groups, "done", v_search) == 0:
                    st.info("Tidak ada RFQ yang cocok dengan pencarian.")
    # -----------------------------------------------------------------
    # MENU 3: HISTORY PENAWARAN -- dibagi 2 tab: Menang / Kalah
    # -----------------------------------------------------------------
    elif selected_v_page == "History Penawaran":
        st.header("🔍 History Penawaran Saya")
        res_hist = (
            sb.table("quotes")
            .select("unit_price, brand, ready_stock, lead_time_days, created_at, round, rfq_assignments(status, pr_items(id, description, description2, quantity, uom, awarded_vendor_id, purchase_requests(rfq_title, pr_code, profiles(vendor_name))))")
            .eq("vendor_id", vendor_id)
            .execute()
        )

        if not res_hist.data:
            st.info("Belum ada riwayat penawaran terkirim.")
        else:
            rows_menang, rows_kalah = [], []
            for q in res_hist.data:
                ass = q.get("rfq_assignments") or {}
                item = ass.get("pr_items") or {}
                pr = item.get("purchase_requests") or {}
                pic_p = pr.get("profiles") or {}
                pic_name = pic_p.get("vendor_name") or "-"

                formatted_price = f"Rp {q.get('unit_price', 0):,.0f}".replace(",", ".")

                is_submitted = ass.get("status") == "Submitted"
                awarded_vendor_id = item.get("awarded_vendor_id")
                if not is_submitted:
                    status_menang = "⏳ Menunggu Keputusan"
                elif awarded_vendor_id == vendor_id:
                    status_menang = "🏆 Menang"
                elif awarded_vendor_id:
                    status_menang = "❌ Kalah"
                else:
                    status_menang = "-"

                row = {
                    "Judul RFQ": pr.get("rfq_title") or pr.get("pr_code"),
                    "PIC Procurement": pic_name,
                    "Deskripsi": clean_description(item.get("description")),
                    "Qty": item.get("quantity"),
                    "UOM": item.get("uom"),
                    "Harga Unit": formatted_price,
                    "Brand": q.get("brand"),
                    "Ready Stock": q.get("ready_stock"),
                    "Lead Time": f"{q.get('lead_time_days')} hari",
                    "Round": f"Round {q.get('round') or 1}",
                    "Status": status_menang,
                    "Tanggal Submit": q.get("created_at")[:10] if q.get("created_at") else "-",
                }

                if status_menang == "🏆 Menang":
                    rows_menang.append(row)
                else:
                    # "Kalah" dan "Menunggu Keputusan" digabung di tab yang sama
                    rows_kalah.append(row)

            tab_menang, tab_kalah = st.tabs([f"🏆 Menang ({len(rows_menang)})", f"❌ Kalah / Belum Final ({len(rows_kalah)})"])
            with tab_menang:
                if rows_menang:
                    st.dataframe(pd.DataFrame(rows_menang), hide_index=True, use_container_width=True)
                else:
                    st.info("Belum ada penawaran yang menang.")
            with tab_kalah:
                if rows_kalah:
                    st.dataframe(pd.DataFrame(rows_kalah), hide_index=True, use_container_width=True)
                else:
                    st.info("Tidak ada penawaran kalah / belum final saat ini.")


# =====================================================================
# COMBINED ADMIN & PROC PORTAL
# =====================================================================
def combined_admin_portal():
    current_user = st.session_state["user_info"]
    current_role = current_user.get("role", "proc")

    if "current_page" not in st.session_state:
        st.session_state["current_page"] = "📥 Import PR List"

    st.sidebar.markdown("## 🧭 Navigasi Menu")
    st.sidebar.markdown("---")

    menu_options = [
        ("📥 Import PR List", "proc_import"),
        ("📊 Price Comparison", "proc_compare"),
        ("✍️ Input Manual (as Vendor)", "proc_manual"),
        ("🧮 AI Cost Estimator", "proc_estimator"),
        ("🔍 History RFQ", "proc_history"),
    ]

    if current_role == "admin":
        menu_options.extend([
            ("➕ Daftarkan Vendor", "admin_vendor"),
            ("➕ Daftarkan PIC", "admin_pic"),
            ("🔑 Reset Password", "admin_reset"),
            ("👥 Daftar User", "admin_users"),
        ])

    for label, page_id in menu_options:
        is_active = (st.session_state["current_page"] == label)
        btn_type = "primary" if is_active else "secondary"

        if st.sidebar.button(label, key=f"nav_btn_{page_id}", type=btn_type, use_container_width=True):
            st.session_state["current_page"] = label
            # Setiap pindah menu, tutup detail RFQ yang terakhir dibuka
            # supaya Price Comparison selalu mulai dari daftar RFQ
            st.session_state["active_compare_pr_id"] = None
            st.session_state["_scroll_top"] = True
            st.rerun()

    st.sidebar.markdown("---")

    selected_page = st.session_state["current_page"]

    if selected_page == "📥 Import PR List":
        proc_portal_import()
    elif selected_page == "📊 Price Comparison":
        proc_portal_comparison()
    elif selected_page == "✍️ Input Manual (as Vendor)":
        proc_portal_manual_input()
    elif selected_page == "🧮 AI Cost Estimator":
        proc_portal_cost_estimator()
    elif selected_page == "🔍 History RFQ":
        proc_portal_history()
    elif selected_page == "➕ Daftarkan Vendor" and current_role == "admin":
        admin_portal_register_vendor()
    elif selected_page == "➕ Daftarkan PIC" and current_role == "admin":
        admin_portal_register_pic()
    elif selected_page == "🔑 Reset Password" and current_role == "admin":
        admin_portal_reset_password()
    elif selected_page == "👥 Daftar User" and current_role == "admin":
        admin_portal_user_list()


def main():
    if "user_info" not in st.session_state:
        st.session_state["user_info"] = None

    if st.session_state["user_info"] is None:
        token_from_url = st.query_params.get("token")
        if token_from_url:
            restored_profile = get_session_user(token_from_url)
            if restored_profile:
                st.session_state["user_info"] = restored_profile
                st.session_state["session_token"] = token_from_url

    if st.session_state["user_info"] is None:
        show_login()
        return

    user = st.session_state["user_info"]

    st.sidebar.markdown(f"### 👋 Hi, **{user.get('vendor_name') or user.get('email')}**")
    st.sidebar.caption(f"Role: `{user.get('role', '').upper()}`")

    if st.sidebar.button("🚪 Log Out", use_container_width=True):
        delete_session(st.session_state.get("session_token"))
        st.session_state["user_info"] = None
        st.session_state["active_compare_pr_id"] = None
        st.session_state["active_vendor_rfq_id"] = None
        st.session_state.pop("session_token", None)
        st.query_params.clear()
        st.rerun()

    st.sidebar.markdown("---")

    if user["role"] in ["admin", "proc"]:
        combined_admin_portal()
    else:
        vendor_portal(user["id"])


if __name__ == "__main__":
    main()
