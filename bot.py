import asyncio
import email as pyemail
import html
import imaplib
import io
import logging
import os
import re
import secrets
import datetime as dt_mod
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx
import qrcode
from dotenv import load_dotenv
from supabase import create_client
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# KONFIGURASI
# ============================================================
load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


BOT_TOKEN = env("BOT_TOKEN")
SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_KEY = env("SUPABASE_KEY")  # pakai service_role key
OWNER_IDS = {int(x) for x in env("OWNER_IDS").replace(" ", "").split(",") if x}
ADMIN_GROUP_ID = int(env("ADMIN_GROUP_ID") or 0)
ADMIN_USERNAME = env("ADMIN_USERNAME").lstrip("@")
HISTORY_CHANNEL_ID = env("HISTORY_CHANNEL_ID")
if HISTORY_CHANNEL_ID:
    HISTORY_CHANNEL_ID = int(HISTORY_CHANNEL_ID)
STORE_NAME = env("STORE_NAME", "Toko Premium")
REVIEW_LINK = env("REVIEW_LINK")  # opsional: link channel testimoni

OTP_MAX_AGE_MIN = int(env("OTP_MAX_AGE_MIN", "15"))

PROVIDER = env("PAYMENT_PROVIDER", "manual").lower()  # midtrans | manual
MIDTRANS_SERVER_KEY = env("MIDTRANS_SERVER_KEY")
MIDTRANS_SANDBOX = env("MIDTRANS_SANDBOX", "true").lower() == "true"
MIDTRANS_BASE = (
    "https://api.sandbox.midtrans.com" if MIDTRANS_SANDBOX else "https://api.midtrans.com"
)
MELDAPAY_API_KEY = env("MELDAPAY_API_KEY")
QRIS_IMAGE_PATH = env("QRIS_IMAGE_PATH", "qris.jpg")  # dipakai mode manual
PAY_TIMEOUT_MIN = int(env("PAYMENT_TIMEOUT_MIN", "10"))
REMIND_BEFORE_MIN = 30
OTP_DONE_SEC = 30
PAGE_SIZE = 8
ACTIVE_OTP_LOCKS: dict[str, datetime] = {}

TOS_TEXT = (
    "📜 <b>Syarat & Ketentuan</b>\n"
    "1. Akun adalah <b>sharing/sewa</b>, dilarang mengubah email, password, PIN & profil.\n"
    "2. Dilarang membagikan akun ke orang lain.\n"
    "3. Wajib logout setelah masa sewa habis.\n"
    "4. Kode OTP hanya bisa diminta <b>1 kali</b> per order.\n"
    "5. Pembelian yang sudah dibayar tidak dapat di-refund, kecuali akun bermasalah dari kami.\n"
    "6. Kendala? Hubungi admin, jangan komplain sepihak."
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
WIB = ZoneInfo("Asia/Jakarta")
h = html.escape


# ============================================================
# TEKS DINAMIS (semua diatur lewat /admin → 📝 Atur Teks)
# ============================================================
# Format: key -> (nama_menu, teks_default, placeholder_tersedia)
TEXTS = {
    # ---------- KATALOG / AWAL ----------
    "welcome": (
        "Sambutan /start",
        "👋 Selamat datang di <b>{store}</b>!\n\n📱 <b>Katalog Aplikasi</b>\n{katalog}\n\n"
        "Pilih aplikasi dari tombol di bawah, atau ketik angkanya.",
        "{store} {katalog}",
    ),
    "empty_catalog": (
        "Katalog Kosong",
        "Katalog masih kosong. Silakan kembali lagi nanti 🙏",
        "(tidak ada)",
    ),
    "gift_success": (
        "Notif Klaim Gift Berhasil",
        "🎉 <b>Pesanan Berhasil Diklaim!</b>\nBerikut adalah detail akun kamu:",
        "(tidak ada)",
    ),
    "gift_invalid": (
        "Notif Link Gift Tidak Valid",
        "❌ Link klaim tidak valid atau sudah kadaluarsa/digunakan orang lain.",
        "(tidak ada)",
    ),
    "not_number": (
        "Balasan Bukan Angka",
        "Ketik /start untuk melihat katalog aplikasi.",
        "(tidak ada)",
    ),
    "not_found": (
        "Nomor Aplikasi Tidak Ada",
        "Nomor tidak ditemukan. Ketik /start untuk melihat katalog.",
        "(tidak ada)",
    ),

    # ---------- PRICELIST ----------
    "stock_empty": (
        "Stok Aplikasi Kosong",
        "😔 Stok <b>{app}</b> sedang kosong.",
        "{app}",
    ),
    "pricelist_head": (
        "Header Pricelist",
        "🛒 <b>Pricelist {app}</b>\n",
        "{app}",
    ),
    "pricelist_item": (
        "Baris Item Pricelist",
        "• {label}  <i>(stok {stok})</i>",
        "{label} {stok}",
    ),
    "pricelist_foot": (
        "Footer Pricelist",
        "\nPilih paket di bawah 👇",
        "(tidak ada)",
    ),

    # ---------- KONFIRMASI & PEMBAYARAN ----------
    "tos": (
        "Syarat & Ketentuan",
        TOS_TEXT,
        "(tidak ada)",
    ),
    "confirm": (
        "Konfirmasi Pesanan",
        "🛍 <b>Konfirmasi Pesanan</b>\n\n📦 {produk}\n💰 Harga: <b>{harga}</b>\n\n{tos}\n\n"
        "Dengan menekan <b>Setuju</b>, kamu menyetujui S&K di atas.",
        "{produk} {harga} {tos}",
    ),
    "offer_empty": (
        "Alert Stok Habis (Klik Paket)",
        "Maaf, stok paket ini baru saja habis. Buka /start lagi.",
        "(tidak ada)",
    ),
    "pending_exists": (
        "Alert Masih Ada Order Pending",
        "Kamu masih punya order pending ({order}). Selesaikan / batalkan dulu.",
        "{order}",
    ),
    "hold_failed": (
        "Alert Stok Sedang Di-hold",
        "Maaf, stok paket ini sudah habis / sedang di-hold pembeli lain.",
        "(tidak ada)",
    ),
    "creating_payment": (
        "Toast Saat Membuat QRIS",
        "Membuat QRIS...",
        "(tidak ada)",
    ),
    "pay_fail": (
        "Pesan Gagal Buat Pembayaran",
        "⚠️ Gagal membuat pembayaran: {error}\nSilakan coba lagi.",
        "{error}",
    ),
    "pay_caption": (
        "Caption QRIS",
        "🧾 <b>Order <code>{order}</code></b>\n📦 {produk}\n💰 Total: <b>{total}</b>\n"
        "⏳ Bayar sebelum <b>{batas}</b> ({menit} menit)\n\n"
        "Scan QRIS di atas dengan e-wallet / m-banking.",
        "{order} {produk} {total} {batas} {menit}",
    ),
    "pay_auto": (
        "Keterangan Bayar Otomatis (Midtrans)",
        "Pembayaran terdeteksi <b>otomatis</b>.",
        "(tidak ada)",
    ),
    "pay_manual": (
        "Keterangan Bayar Manual",
        "Setelah bayar, tunggu konfirmasi admin (otomatis dikirim akunnya).",
        "(tidak ada)",
    ),
    "waiting_payment": (
        "Pesan Menunggu Pembayaran",
        "⏳ Menunggu pembayaran untuk order <code>{order}</code> ...",
        "{order}",
    ),
    "order_cancelled": (
        "Pesan Order Dibatalkan",
        "❌ Order <code>{order}</code> dibatalkan.\nAlasan: {reason}\n\nKetik /start untuk memesan lagi.",
        "{order} {reason}",
    ),
    "order_not_found": (
        "Alert Order Tidak Ditemukan",
        "Order tidak ditemukan",
        "(tidak ada)",
    ),
    "order_cancelled_toast": (
        "Toast Order Dibatalkan",
        "Order dibatalkan",
        "(tidak ada)",
    ),
    "order_cannot_cancel": (
        "Toast Order Tidak Bisa Dibatalkan",
        "Order sudah tidak bisa dibatalkan",
        "(tidak ada)",
    ),
    "cancel_toast": (
        "Toast Batal Konfirmasi",
        "Dibatalkan",
        "(tidak ada)",
    ),

    # ---------- DETAIL AKUN TERKIRIM ----------
    "delivery_head": (
        "Header Akun Terkirim",
        "✅ <b>Pembayaran berhasil!</b>",
        "(tidak ada)",
    ),
    "delivery_order": (
        "Baris Order ID",
        "🧾 Order: <code>{order}</code>",
        "{order}",
    ),
    "delivery_produk": (
        "Baris Produk",
        "📦 {produk}",
        "{produk}",
    ),
    "delivery_expires": (
        "Baris Masa Aktif",
        "⏰ Berakhir: <b>{expires}</b>",
        "{expires}",
    ),
    "delivery_email": (
        "Baris Email Akun",
        "📧 Email: <code>{email}</code>",
        "{email}",
    ),
    "delivery_password": (
        "Baris Password Akun",
        "🔑 Password: <code>{password}</code>",
        "{password}",
    ),
    "delivery_profile": (
        "Baris Nama Profil",
        "👤 Profil: <code>{profile}</code>",
        "{profile}",
    ),
    "delivery_pin": (
        "Baris PIN Profil",
        "🔒 PIN: <code>{pin}</code>",
        "{pin}",
    ),
    "otp_hint": (
        "Petunjuk Kode OTP",
        "ℹ️ Jika diminta kode verifikasi, <b>minta kode dulu di aplikasi</b>, "
        "lalu tekan <b>Dapatkan Kode</b> (hanya 1x).",
        "(tidak ada)",
    ),
    "delivery_foot": (
        "Penutup Akun Terkirim",
        "Jangan ubah data akun. Terima kasih 🙏",
        "(tidak ada)",
    ),

    # ---------- OTP ----------
    "rent_expired_alert": (
        "Alert Masa Sewa Habis",
        "Masa sewa sudah berakhir.",
        "(tidak ada)",
    ),
    "otp_not_needed": (
        "Alert Aplikasi Tidak Butuh Kode",
        "Aplikasi ini tidak memerlukan kode.",
        "(tidak ada)",
    ),
    "otp_used_alert": (
        "Alert OTP Sudah Terpakai",
        "Kesempatan ambil kode (1x) sudah terpakai.",
        "(tidak ada)",
    ),
    "otp_locked": (
        "Alert Email Sedang Dipantau",
        "⚠️ Email sedang diakses oleh user lain untuk verifikasi.\n"
        "Tunggu sekitar {mins} menit lagi ya.",
        "{mins}",
    ),
    "otp_inactive": (
        "Alert Fitur OTP Belum Aktif",
        "Fitur kode untuk akun ini belum aktif. Hubungi admin.",
        "(tidak ada)",
    ),
    "otp_starting": (
        "Toast Memulai Pemantauan",
        "Memulai pemantauan email...",
        "(tidak ada)",
    ),
    "otp_waiting_start": (
        "Pesan Awal Menunggu Kode",
        "⏳ <b>Mode Menunggu Kode Aktif (05:00)</b>\n\n"
        "Silakan <b>login sekarang</b> di aplikasi.\n"
        "Bot sedang memantau email, kode akan otomatis dikirim ke sini jika masuk.",
        "(tidak ada)",
    ),
    "otp_waiting_live": (
        "Pesan Live Menunggu Kode (Timer)",
        "⏳ <b>Mode Menunggu Kode Aktif ({timer})</b>\n\n"
        "Silakan <b>login sekarang</b> di aplikasi.\n"
        "Bot sedang memantau email secara live...",
        "{timer}",
    ),
    "otp_success": (
        "Pesan Kode Berhasil Didapat",
        "✅ <b>Kode berhasil didapatkan!</b>\n\n"
        "🔑 Kode verifikasi kamu: <code>{code}</code>\n\n"
        "<i>Kesempatan 1x sudah terpakai.</i>",
        "{code}",
    ),
    "otp_poll_done": (
        "Pesan Pemantauan Selesai",
        "✅ <b>Pemantauan Selesai!</b>",
        "(tidak ada)",
    ),
    "otp_timeout_title": (
        "Judul Pemantauan Timeout",
        "❌ <b>Pemantauan Dihentikan (Waktu Habis)</b>",
        "(tidak ada)",
    ),
    "otp_timeout_body": (
        "Body Pemantauan Timeout",
        "⏳ <b>Waktu 5 menit habis.</b>\n"
        "Kode belum masuk ke email. Kesempatan kamu <b>belum terpakai</b>, "
        "silakan coba klik <b>Dapatkan Kode</b> lagi nanti jika sudah siap login.",
        "(tidak ada)",
    ),

    # ---------- SEWA BERAKHIR & REMINDER ----------
    "reminder": (
        "Pengingat Sewa Hampir Habis",
        "⏰ <b>Pengingat</b>\nMasa sewa <b>{produk}</b> akan berakhir pukul <b>{waktu}</b> "
        "(±{menit} menit lagi). Mohon siap-siap <b>logout</b> ya.",
        "{produk} {waktu} {menit}",
    ),
    "expired": (
        "Sewa Berakhir",
        "⌛ Masa sewa <b>{produk}</b> (order <code>{order}</code>) telah berakhir. "
        "Mohon <b>logout</b> dari akun. Terima kasih! 🙏",
        "{produk} {order}",
    ),

    # ---------- SELESAI / TERIMA KASIH ----------
    "thanks": (
        "Ucapan Setelah Selesai",
        "🎉 <b>Terima kasih sudah berbelanja!</b>\n\nBoleh minta <b>honest review</b>-nya? "
        "Testimoni jujur kamu sangat membantu kami berkembang ⭐",
        "(tidak ada)",
    ),
    "thanks_link": (
        "Link Testimoni (Setelah Thanks)",
        "\n\n👉 {link}",
        "{link}",
    ),
    "thanks_extra": (
        "Penutup Setelah Thanks",
        "\n\nJika ada kendala, silakan hubungi admin.",
        "(tidak ada)",
    ),
    "help_text": (
        "Pesan Bantuan Buyer",
        "🆘 Ada kendala dengan order <code>{order}</code>?\n"
        "Hubungi admin dan sertakan ID order di atas.",
        "{order}",
    ),

    # ---------- TOMBOL (LABEL) ----------
    "btn_otp": (
        "Tombol: Dapatkan Kode",
        "🔑 Dapatkan Kode (1x)",
        "(tidak ada)",
    ),
    "btn_done": (
        "Tombol: Selesai",
        "✅ Selesai",
        "(tidak ada)",
    ),
    "btn_help": (
        "Tombol: Bantuan",
        "🆘 Bantuan",
        "(tidak ada)",
    ),
    "btn_cancel_order": (
        "Tombol: Batalkan Order",
        "❌ Batalkan Order",
        "(tidak ada)",
    ),
    "btn_agree_pay": (
        "Tombol: Setuju & Bayar",
        "✅ Setuju & Bayar",
        "(tidak ada)",
    ),
    "btn_cancel": (
        "Tombol: Batal",
        "❌ Batal",
        "(tidak ada)",
    ),
    "btn_contact_admin": (
        "Tombol: Hubungi Admin",
        "💬 Hubungi Admin",
        "(tidak ada)",
    ),
}
_CACHE: dict[str, str] = {}


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def T(key: str, **kw) -> str:
    tpl = _CACHE.get(key) or TEXTS[key][1]
    try:
        return tpl.format_map(_Safe(**kw))
    except Exception:
        return TEXTS[key][1].format_map(_Safe(**kw))


async def load_texts():
    rows = await dbx(sb.table("settings").select("key,value"))
    _CACHE.clear()
    _CACHE.update({r["key"]: r["value"] for r in rows})


# ============================================================
# HELPER UMUM
# ============================================================
async def dbx(builder):
    res = await asyncio.to_thread(builder.execute)
    return res.data

def mask_name(name: str) -> str:
    if not name or name == "-":
        return "Seseorang"
    words = name.split()
    masked = []
    for w in words:
        if len(w) <= 2:
            masked.append(w[0] + "*")
        else:
            masked.append(w[:2] + "*" * (len(w) - 2))
    return " ".join(masked)

def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_dt(v) -> str:
    dt = parse_dt(v) if isinstance(v, str) else v
    return dt.astimezone(WIB).strftime("%d/%m/%Y %H:%M WIB")


def rp(n: int) -> str:
    return "Rp" + f"{int(n):,}".replace(",", ".")


def parse_duration(code: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([HDWM])", code.strip().upper())
    if not m:
        raise ValueError(f"Durasi '{code}' tidak valid (contoh: 12H, 1D, 7D, 1M)")
    n, u = int(m.group(1)), m.group(2)
    return n * {"H": 1, "D": 24, "W": 168, "M": 720}[u]


def is_owner(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in OWNER_IDS)


def slot_key_of(app_id: int, mail: str, profile: str) -> str:
    return f"{app_id}|{mail.strip().lower()}|{(profile or '').strip().lower()}"


async def notify_admin(bot, text: str, markup=None):
    if not ADMIN_GROUP_ID:
        return
    try:
        await bot.send_message(
            ADMIN_GROUP_ID, text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except Exception:
        logger.exception("Gagal kirim notifikasi ke grup admin")


async def safe_edit(cq, text: str, markup=None):
    try:
        await cq.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.debug("edit skip: %s", e)


def admin_contact_markup() -> InlineKeyboardMarkup | None:
    if not ADMIN_USERNAME:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(T("btn_contact_admin"), url=f"https://t.me/{ADMIN_USERNAME}")]]
    )


# ============================================================
# DATABASE HELPER
# ============================================================
async def get_apps(active_only: bool = True):
    b = sb.table("apps").select("*").order("id")
    if active_only:
        b = b.eq("active", True)
    return await dbx(b)

async def register_user(bot, user):
    try:
        # Cek apakah user sudah ada di database
        existing = await dbx(sb.table("users").select("id").eq("id", user.id))
        
        if not existing:
            # Insert user baru
            await dbx(sb.table("users").insert({
                "id": user.id,
                "username": user.username or "",
                "full_name": user.full_name or ""
            }))
            
            # Kirim log new user ke channel history
            if HISTORY_CHANNEL_ID:
                mention = f"@{user.username}" if user.username else f'<a href="tg://user?id={user.id}">{h(user.full_name)}</a>'
                text = f"👤 <b>New User!</b>\n{mention} baru saja memulai bot."
                try:
                    await bot.send_message(HISTORY_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.warning(f"Gagal kirim notif new user ke channel: {e}")
        else:
            # Update data user yang sudah ada
            await dbx(sb.table("users").update({
                "username": user.username or "",
                "full_name": user.full_name or ""
            }).eq("id", user.id))
            
    except Exception as e:
        logger.warning(f"Gagal register user {user.id}: {e}")

async def release_slot(slot_key: str):
    await dbx(
        sb.table("stocks")
        .update({"status": "available", "held_until": None})
        .eq("slot_key", slot_key)
    )


async def pick_and_hold(stock_id: int):
    rep = await dbx(sb.table("stocks").select("*").eq("id", stock_id))
    if not rep:
        return None
    rep = rep[0]
    cands = await dbx(
        sb.table("stocks")
        .select("*, apps(name, need_otp)")
        .eq("app_id", rep["app_id"])
        .eq("label", rep["label"])
        .eq("duration_hours", rep["duration_hours"])
        .eq("price", rep["price"])
        .eq("status", "available")
        .order("id")
    )
    cands.sort(key=lambda r: r["id"] != stock_id)
    until = iso(now() + timedelta(minutes=PAY_TIMEOUT_MIN + 1))
    
    for c in cands:
        # 1. Kunci baris spesifik ini dengan RPC (untuk mencegah race condition)
        ok = await dbx(sb.rpc("hold_slot", {"p_stock_id": c["id"], "p_until": until}))
        if ok:
            # 2. [TAMBAHKAN BLOK INI] 
            # Kunci JUGA semua stok lain yang slot_key-nya sama (termasuk label NOLIM / durasi lain)
            await dbx(
                sb.table("stocks")
                .update({"status": "held", "held_until": until})
                .eq("slot_key", c["slot_key"])
                .eq("status", "available")
            )
            return c
            
    return None


# ============================================================
# PEMBAYARAN QRIS
# ============================================================
def make_qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def create_payment(order: dict):
    if PROVIDER == "midtrans":
        try:
            payload = {
                "payment_type": "qris",
                "transaction_details": {"order_id": order["id"], "gross_amount": order["price"]},
                "qris": {"acquirer": "gopay"},
                "custom_expiry": {"expiry_duration": PAY_TIMEOUT_MIN, "unit": "minute"},
            }
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(
                    f"{MIDTRANS_BASE}/v2/charge", json=payload, auth=(MIDTRANS_SERVER_KEY, "")
                )
                data = r.json()
                if str(data.get("status_code")) not in ("200", "201"):
                    raise RuntimeError(f"Midtrans: {data.get('status_message')}")
                
                qris_url = None
                for a in data.get("actions", []):
                    if "qr" in a.get("name", "").lower() and "url" in a:
                        qris_url = a["url"]
                        break
                
                # Perhatikan tambahan order["price"] di bagian akhir return
                if data.get("qr_string"):
                    return make_qr_png(data["qr_string"]), data.get("transaction_id", ""), qris_url, order["price"]
                if qris_url:
                    img = await c.get(qris_url)
                    return img.content, data.get("transaction_id", ""), qris_url, order["price"]
                
                raise RuntimeError("Midtrans tidak mengembalikan QR")
        
        except Exception as e:
            logger.warning(f"Midtrans gagal ({e}), mengalihkan ke Meldapay...")
            if MELDAPAY_API_KEY:
                return await create_meldapay_invoice(order)
            else:
                raise e

    elif PROVIDER == "meldapay":
        return await create_meldapay_invoice(order)

    # Return untuk metode manual (tambahkan order["price"])
    if os.path.exists(QRIS_IMAGE_PATH):
        with open(QRIS_IMAGE_PATH, "rb") as f:
            return f.read(), "manual", None, order["price"]
    return None, "manual", None, order["price"]


async def mt_status(order_id: str) -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{MIDTRANS_BASE}/v2/{order_id}/status", auth=(MIDTRANS_SERVER_KEY, ""))
        d = r.json()
    if str(d.get("status_code")) == "404":
        return "pending"
    st = d.get("transaction_status")
    if st in ("settlement", "capture"):
        return "paid"
    if st in ("expire", "cancel", "deny", "failure"):
        return "failed"
    return "pending"


async def mt_cancel(order_id: str):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{MIDTRANS_BASE}/v2/{order_id}/cancel", auth=(MIDTRANS_SERVER_KEY, ""))
    except Exception:
        pass
    
async def create_meldapay_invoice(order: dict):
    if not MELDAPAY_API_KEY:
        raise RuntimeError("MELDAPAY_API_KEY belum di-set, fallback gagal.")
    
    url = f"https://meldapay.com/api/invoice?apikey={MELDAPAY_API_KEY}&amount={order['price']}"
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(url)
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"Meldapay error: {data}")
        
        invoice_id = data["invoice_id"]
        qris_url = data.get("payment_link")
        
        # Ambil total tagihan (sudah termasuk fee/kode unik dari Meldapay)
        final_price = data.get("total", order["price"])
        
        # Unduh gambar QRIS dari Meldapay
        img_resp = await c.get(data["qris_image"])
        # Return 4 variabel
        return img_resp.content, f"melda:{invoice_id}", qris_url, final_price

async def meldapay_status(invoice_id: str) -> str:
    url = f"https://meldapay.com/api/invoice/status?apikey={MELDAPAY_API_KEY}&invoice_id={invoice_id}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url)
        data = r.json()
        
    st = data.get("status", "pending")
    if st == "paid":
        return "paid"
    if st in ("expired", "cancelled"):
        return "failed"
    return "pending"

async def meldapay_cancel(invoice_id: str):
    url = f"https://meldapay.com/api/invoice/cancel?apikey={MELDAPAY_API_KEY}&invoice_id={invoice_id}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(url)
    except Exception:
        pass


# ============================================================
# GMAIL OTP (IMAP)
# ============================================================
def _body_text(msg) -> str:
    parts = []
    for p in msg.walk():
        ctype = p.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = p.get_payload(decode=True)
        if not payload:
            continue
        txt = payload.decode(p.get_content_charset() or "utf-8", errors="ignore")
        if ctype == "text/html":
            txt = re.sub(r"<(style|script).*?>.*?</\1>", " ", txt, flags=re.S | re.I)
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = html.unescape(txt)
        parts.append(txt)
    return "\n".join(parts)


def _extract_code(text: str, device: str = "hp") -> str | None:
    # Jika device TV, kita cari link dari Netflix atau kode 8 digit alfanumerik
    if device == "tv":
        # Cari link
        links = re.findall(r"https?://[^\s\"'<>]+", text)
        for link in links:
            if "netflix.com" in link or "account" in link.lower():
                return link  # Langsung return linknya
                
        # Cari kode 8 digit kombinasi huruf dan angka (biasanya kode TV)
        for m in re.finditer(r"(?<![\w-])([a-zA-Z0-9]{8})(?![\w-])", text):
            return m.group(1)

    # Logika bawaan untuk HP/Laptop (cari 4-8 digit angka)
    text = re.sub(r"https?://\S+", " ", text)
    for marker in ("SRC:", "Ada pertanyaan", "Questions?", "Netflix Pte"):
        i = text.find(marker)
        if i > 0:
            text = text[:i]
    for m in re.finditer(r"(?<![\w-])(\d{4,8})(?![\w-])", text):
        c = m.group(1)
        if len(c) == 4 and c[:2] in ("19", "20"):
            continue
        return c
    return None


def fetch_otp_sync(mail_user: str, app_password: str, since_dt: datetime, device: str = "hp") -> str | None:
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        m.login(mail_user, app_password.replace(" ", ""))
        m.select("INBOX", readonly=True)
        typ, data = m.search(None, "X-GM-RAW", '"newer_than:1d"')
        ids = data[0].split() if data and data[0] else []
        for mid in reversed(ids[-5:]):
            typ, md = m.fetch(mid, "(RFC822)")
            msg = pyemail.message_from_bytes(md[0][1])
            try:
                dt = parsedate_to_datetime(msg["Date"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < since_dt:
                    continue
            except Exception:
                pass
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            # Tambahkan argumen device ke _extract_code
            code = _extract_code(subject, device) or _extract_code(_body_text(msg), device)
            if code:
                return code
        return None
    finally:
        try:
            m.logout()
        except Exception:
            pass


# Tambahkan argumen device="hp" di parameternya
async def poll_otp_task(bot, order: dict, app_pw: str, since_dt: datetime, original_msg, status_msg, device: str = "hp"):
    email = order["email"]
    oid = order["id"]
    for _ in range(20):
        time_left = (since_dt + timedelta(minutes=5)) - now()
        if time_left.total_seconds() <= 0:
            break
        mins, secs = divmod(int(time_left.total_seconds()), 60)
        timer_str = f"{mins:02d}:{secs:02d}"
        
        # Bedakan pesan live berdasarkan device
        device_name = "TV 📺" if device == "tv" else "HP/Laptop 📱"
        live_text = f"⏳ <b>Menunggu Kode {device_name} ({timer_str})</b>\n\nBot sedang memantau email secara live..."
        
        try:
            await bot.edit_message_text(
                chat_id=order["tg_user_id"],
                message_id=status_msg.message_id,
                text=live_text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
            
        try:
            # Teruskan parameter device ke fetch_otp_sync
            code = await asyncio.to_thread(fetch_otp_sync, email, app_pw, since_dt, device)
        except Exception as e:
            logger.error(f"Error saat polling OTP: {e}")
            code = None
            
        if code:
            claimed = await dbx(
                sb.table("orders")
                .update({"otp_used": True, "otp_code": code})
                .eq("id", oid)
                .eq("otp_used", False)
            )
            if claimed:
                try:
                    await bot.edit_message_text(
                        chat_id=order["tg_user_id"],
                        message_id=status_msg.message_id,
                        text=T("otp_poll_done"),
                        parse_mode=ParseMode.HTML,
                    )
                    
                    # Jika yang didapat berupa link (awalan http), sesuaikan kalimatnya
                    if code.startswith("http"):
                        success_msg = f"✅ <b>Link Konfirmasi Didapatkan!</b>\n\n🔗 Klik link berikut untuk masuk: \n{code}\n\n<i>Kesempatan 1x sudah terpakai.</i>"
                    else:
                        success_msg = T("otp_success", code=h(code))
                        
                    await bot.send_message(
                        order["tg_user_id"],
                        success_msg,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                except Exception:
                    pass
                await notify_admin(bot, f"🔑 OTP/Link dikirim | <code>{oid}</code> | <code>{h(email)}</code>")
                asyncio.create_task(auto_done_after(bot, oid, OTP_DONE_SEC))
            ACTIVE_OTP_LOCKS.pop(email, None)
            return
        await asyncio.sleep(15)

    ACTIVE_OTP_LOCKS.pop(email, None)
    try:
        await bot.edit_message_text(
            chat_id=order["tg_user_id"],
            message_id=status_msg.message_id,
            text=T("otp_timeout_title"),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            order["tg_user_id"], T("otp_timeout_body"), parse_mode=ParseMode.HTML
        )
        await bot.edit_message_reply_markup(
            chat_id=order["tg_user_id"],
            message_id=original_msg.message_id,
            reply_markup=delivery_markup(order),
        )
    except Exception:
        pass


# ============================================================
# ORDER LIFECYCLE
# ============================================================
def delivery_text(o: dict) -> str:
    lines = [
        T("delivery_head"),
        "",
    ]
    # Sembunyikan baris ID Order jika order hasil generate manual (DIR)
    if not o["id"].startswith("DIR"):
        lines.append(T("delivery_order", order=o["id"]))
        
    lines.append(T("delivery_produk", produk=f"{h(o['app_name'])} {h(o['label'])} ({h(o['duration_code'])})"))
    lines.append(T("delivery_expires", expires=fmt_dt(o["expires_at"])))
    lines.append("")
    lines.append(T("delivery_email", email=h(o["email"])))
    
    if o.get("password"):
        lines.append(T("delivery_password", password=h(o["password"])))
    if o.get("profile_name"):
        lines.append(T("delivery_profile", profile=h(o["profile_name"])))
    if o.get("profile_pin"):
        lines.append(T("delivery_pin", pin=h(o["profile_pin"])))
    lines.append("")
    if o.get("need_otp"):
        lines.append(T("otp_hint"))
    lines.append(T("delivery_foot"))
    return "\n".join(lines)


def delivery_markup(o: dict) -> InlineKeyboardMarkup:
    rows = []
    if o.get("need_otp") and not o.get("otp_used"):
        rows.append([InlineKeyboardButton(T("btn_otp"), callback_data=f"otp:{o['id']}")])
    rows.append(
        [
            InlineKeyboardButton(T("btn_done"), callback_data=f"done:{o['id']}"),
            InlineKeyboardButton(T("btn_help"), callback_data=f"help:{o['id']}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def finalize_paid(bot, oid: str):
    cur = await dbx(sb.table("orders").select("duration_hours").eq("id", oid).eq("status", "pending"))
    if not cur:
        return
    paid = now()
    exp = paid + timedelta(hours=cur[0]["duration_hours"])
    rows = await dbx(
        sb.table("orders")
        .update({"status": "success", "paid_at": iso(paid), "expires_at": iso(exp)})
        .eq("id", oid)
        .eq("status", "pending")
    )
    if not rows:
        return
    o = rows[0]
    await dbx(
        sb.table("stocks")
        .update({"status": "rented", "held_until": None})
        .eq("slot_key", o["slot_key"])
    )
    if o.get("qr_msg_id"):
        try:
            await bot.delete_message(o["tg_user_id"], o["qr_msg_id"])
        except Exception:
            pass
            
    await bot.send_message(
        o["tg_user_id"], delivery_text(o), parse_mode=ParseMode.HTML,
        reply_markup=delivery_markup(o),
    )
    
    await notify_admin(
        bot,
        f"✅ <b>SUCCESS</b> | <code>{o['id']}</code>\n"
        f"{h(o['app_name'])} {h(o['label'])} {h(o['duration_code'])} — {rp(o['price'])}\n"
        f"Buyer: {h(o.get('tg_name') or '-')} (@{h(o.get('tg_username') or '-')}) <code>{o['tg_user_id']}</code>\n"
        f"Akun: <code>{h(o['email'])}</code> {h(o.get('profile_name') or '')}\n"
        f"Berakhir: {fmt_dt(o['expires_at'])}",
    )

    # -------- TAMBAHKAN BLOK INI --------
    if HISTORY_CHANNEL_ID:
        try:
            raw_name = o.get('tg_name') or "-"
            censored_name = mask_name(raw_name)
            
            history_text = (
                f"🛍 <b>Pembelian Berhasil!</b>\n\n"
                f"📦 Produk: <b>{h(o['app_name'])} {h(o['label'])}</b> ({h(o['duration_code'])})\n"
                f"💰 Harga: {rp(o['price'])}\n"
                f"👤 Pembeli: {h(censored_name)}\n\n"
                f"✅ <i>Terima kasih sudah berbelanja!</i>"
            )
            await bot.send_message(HISTORY_CHANNEL_ID, history_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Gagal kirim notif history channel: {e}")


async def cancel_order(bot, oid: str, status: str, reason: str) -> bool:
    rows = await dbx(
        sb.table("orders").update({"status": status}).eq("id", oid).eq("status", "pending")
    )
    if not rows:
        return False
    o = rows[0]
    await release_slot(o["slot_key"])
    if o.get("qr_msg_id"):
        try:
            await bot.delete_message(o["tg_user_id"], o["qr_msg_id"])
        except Exception:
            pass
    ref = o.get("pay_ref") or ""
    if ref.startswith("melda:"):
        await meldapay_cancel(ref.split(":")[1])
    elif PROVIDER == "midtrans":
        await mt_cancel(oid)
    try:
        await bot.send_message(
            o["tg_user_id"],
            T("order_cancelled", order=oid, reason=h(reason)),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await notify_admin(
        bot,
        f"❌ <b>{status.upper()}</b> | <code>{oid}</code>\n"
        f"{h(o['app_name'])} {h(o['label'])} {h(o['duration_code'])} — {rp(o['price'])}\n"
        f"Alasan: {h(reason)}",
    )
    return True


# ============================================================
# BUYER HANDLERS
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    asyncio.create_task(register_user(context.bot, user))

    if context.args and context.args[0].startswith("GIFT"):
        oid = context.args[0]
        rows = await dbx(sb.table("orders").select("*").eq("id", oid).eq("status", "gift"))
        if rows:
            o = rows[0]
            await dbx(sb.table("orders").update({
                "status": "success",
                "tg_user_id": user.id,
                "tg_username": user.username or "",
                "tg_name": user.full_name,
                # paid_at & expires_at TIDAK diubah, sudah diset saat /give
            }).eq("id", oid))
            await dbx(sb.table("stocks").update({"status": "rented", "held_until": None}).eq("slot_key", o["slot_key"]))
            o["tg_user_id"] = user.id
            await update.message.reply_text(T("gift_success"), parse_mode=ParseMode.HTML)
            await update.message.reply_text(
                delivery_text(o), parse_mode=ParseMode.HTML, reply_markup=delivery_markup(o)
            )
            await notify_admin(context.bot, f"🎁 Link Klaim <code>{oid}</code> telah diambil oleh <code>{user.id}</code> (@{user.username or '-'}).")
            return
        else:
            await update.message.reply_text(T("gift_invalid"))

    apps = await get_apps()
    if not apps:
        await update.message.reply_text(T("empty_catalog"))
        return

    names = [f"{i}. {a['name']}" for i, a in enumerate(apps, 1)]
    rows_kb = [names[i : i + 2] for i in range(0, len(names), 2)]
    text = T("welcome", store=h(STORE_NAME), katalog="\n".join(h(n) for n in names))
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(rows_kb, resize_keyboard=True),
    )

async def cmd_ppj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text(
            "⚠️ <b>Format Salah</b>\nGunakan: <code>/ppj email@gmail.com DD/MM/YYYY</code>\n"
            "Contoh: <code>/ppj netflix@gmail.com 20/11/2026</code>",
            parse_mode=ParseMode.HTML
        )
    
    email = args[0].lower()
    date_str = args[1]
    
    try:
        # Konversi input DD/MM/YYYY ke UTC
        dt = datetime.strptime(date_str, "%d/%m/%Y").replace(tzinfo=WIB).astimezone(timezone.utc)
    except ValueError:
        return await update.message.reply_text("❌ Format tanggal salah. Gunakan DD/MM/YYYY (contoh: 20/11/2026)")
    
    # Update semua slot/profil yang menggunakan email tersebut
    rows = await dbx(sb.table("stocks").update({"ppj_date": iso(dt)}).eq("email", email))
    if not rows:
        return await update.message.reply_text("❌ Email tidak ditemukan di database stok.")
    
    await update.message.reply_text(
        f"✅ Pengingat PPJ untuk <b>{h(email)}</b> berhasil diatur ke tanggal <b>{date_str}</b>.", 
        parse_mode=ParseMode.HTML
    )

async def job_daily_ppj(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    try:
        t_now = now()
        t_target = t_now + timedelta(days=3)
        
        # Cari stok yang ppj_date-nya 3 hari ke depan, dan tidak lebih dari 1 hari ke belakang (agar tidak spam kalau sudah telat lama)
        rows = await dbx(
            sb.table("stocks")
            .select("email, ppj_date, apps(name)")
            .not_.is_("ppj_date", "null")
            .lte("ppj_date", iso(t_target))
            .gte("ppj_date", iso(t_now - timedelta(days=1)))
        )
        
        if not rows:
            return
            
        # Karena 1 email punya banyak slot/baris, kita kelompokkan (group by email)
        accounts = {}
        for r in rows:
            if r["email"] not in accounts:
                accounts[r["email"]] = r
                
        if not accounts:
            return
            
        lines = [
            "⚠️ <b>PENGINGAT PPJ AKUN</b> ⚠️\n",
            "Akun langganan berikut akan memasuki masa tenggang/habis dalam waktu dekat:\n"
        ]
        
        for email, data in accounts.items():
            dt = parse_dt(data["ppj_date"])
            lines.append(f"📧 <code>{email}</code>\n📦 {data['apps']['name']} — ⏳ <b>{dt.astimezone(WIB).strftime('%d/%m/%Y')}</b>\n")
            
        lines.append("Harap segera diperpanjang agar slot pembeli tidak terganggu.")
        await notify_admin(bot, "\n".join(lines))
        
    except Exception as e:
        logger.exception("Gagal menjalankan job_daily_ppj")

async def show_pricelist(message, app: dict):
    rows = await dbx(
        sb.table("stocks")
        .select("id,label,duration_code,duration_hours,price")
        .eq("app_id", app["id"])
        .eq("status", "available")
        .order("id")
    )
    if not rows:
        await message.reply_text(
            T("stock_empty", app=h(app["name"])), parse_mode=ParseMode.HTML
        )
        return
    groups: dict = {}
    for r in rows:
        k = (r["label"], r["duration_hours"], r["price"])
        g = groups.setdefault(k, {"rep": r["id"], "n": 0, "r": r})
        g["n"] += 1
    items = sorted(groups.values(), key=lambda g: (g["r"]["duration_hours"], g["r"]["price"]))
    lines = [T("pricelist_head", app=h(app["name"]))]
    kb = []
    for g in items:
        r = g["r"]
        label = f"{app['name']} {r['label']} {r['duration_code']} - {rp(r['price'])}"
        lines.append(T("pricelist_item", label=h(label), stok=g["n"]))
        kb.append([InlineKeyboardButton(label, callback_data=f"o:{g['rep']}")])
    lines.append(T("pricelist_foot"))
    await message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb)
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if is_owner(update) and context.user_data.get("await") == "add_stock":
        return await admin_add_stock(update, context)
    if is_owner(update) and context.user_data.get("await") == "set_text":
        return await admin_set_text(update, context)
    if is_owner(update) and context.user_data.get("await") == "give_paid":
        return await admin_give_finish(update, context)
    m = re.fullmatch(r"(\d+)(?:\s*[.)]\s*(.+))?", text)
    if not m:
        await update.message.reply_text(T("not_number"))
        return
    apps = await get_apps()
    app = None
    if m.group(2):
        app = next((a for a in apps if a["name"].lower() == m.group(2).strip().lower()), None)
    if app is None:
        idx = int(m.group(1)) - 1
        app = apps[idx] if 0 <= idx < len(apps) else None
    if app is None:
        await update.message.reply_text(T("not_found"))
        return
    await show_pricelist(update.message, app)


async def cb_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    sid = int(cq.data.split(":")[1])
    rows = await dbx(sb.table("stocks").select("*, apps(name)").eq("id", sid))
    if not rows or rows[0]["status"] != "available":
        await cq.answer(T("offer_empty"), show_alert=True)
        return
    s = rows[0]
    await cq.answer()
    produk = f"{h(s['apps']['name'])} {h(s['label'])} ({h(s['duration_code'])})"
    text = T("confirm", produk=produk, harga=rp(s["price"]), tos=T("tos"))
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(T("btn_agree_pay"), callback_data=f"tos:{sid}")],
            [InlineKeyboardButton(T("btn_cancel"), callback_data="ctos")],
        ]
    )
    await cq.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_cancel_tos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer(T("cancel_toast"))
    try:
        await cq.message.delete()
    except Exception:
        pass


async def cb_tos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = cq.from_user
    sid = int(cq.data.split(":")[1])

    pend = await dbx(
        sb.table("orders").select("id").eq("tg_user_id", user.id).eq("status", "pending")
    )
    if pend:
        await cq.answer(T("pending_exists", order=pend[0]["id"]), show_alert=True)
        return

    stock = await pick_and_hold(sid)
    if not stock:
        await cq.answer(T("hold_failed"), show_alert=True)
        return
    await cq.answer(T("creating_payment"))

    oid = "ORD" + secrets.token_hex(4).upper()
    hold_until = now() + timedelta(minutes=PAY_TIMEOUT_MIN)
    order = {
        "id": oid,
        "tg_user_id": user.id,
        "tg_username": user.username,
        "tg_name": user.full_name,
        "stock_id": stock["id"],
        "slot_key": stock["slot_key"],
        "app_name": stock["apps"]["name"],
        "need_otp": stock["apps"]["need_otp"],
        "label": stock["label"],
        "duration_code": stock["duration_code"],
        "duration_hours": stock["duration_hours"],
        "price": stock["price"],
        "email": stock["email"],
        "password": stock.get("password"),
        "profile_name": stock.get("profile_name"),
        "profile_pin": stock.get("profile_pin"),
        "status": "pending",
        "hold_until": iso(hold_until),
    }
    try:
        await dbx(sb.table("orders").insert(order))
        
        # Tangkap 4 variabel sekarang
        png, ref, qris_url, final_price = await create_payment(order)
        
        # Jika Meldapay mengembalikan nilai yang include fee, update database
        if final_price != order["price"]:
            order["price"] = final_price
            await dbx(sb.table("orders").update({"price": final_price}).eq("id", oid))
            
    except Exception as e:
        logger.exception("Gagal membuat pembayaran")
        await dbx(sb.table("orders").delete().eq("id", oid))
        await release_slot(stock["slot_key"])
        await cq.message.reply_text(T("pay_fail", error=h(str(e))))
        return

    is_auto = PROVIDER in ("midtrans", "meldapay") or (ref and ref.startswith("melda:"))
    
    caption = T(
        "pay_caption",
        order=oid,
        produk=f"{h(order['app_name'])} {h(order['label'])} ({h(order['duration_code'])})",
        total=rp(order["price"]),
        batas=fmt_dt(hold_until),
        menit=PAY_TIMEOUT_MIN,
    ) + "\n"
    caption += T("pay_auto") if is_auto else T("pay_manual")
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(T("btn_cancel_order"), callback_data=f"cancel:{oid}")]]
    )

    if png:
        sent = await cq.message.reply_photo(png, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        sent = await cq.message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=kb)

    await dbx(sb.table("orders").update({"qr_msg_id": sent.message_id, "pay_ref": ref}).eq("id", oid))
    await safe_edit(cq, T("waiting_payment", order=oid))

    admin_kb = None
    if not is_auto:
        admin_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Konfirmasi Pembayaran", callback_data=f"admok:{oid}")]]
        )

    admin_msg = (
        f"🕐 <b>PENDING</b> | <code>{oid}</code>\n"
        f"{h(order['app_name'])} {h(order['label'])} {h(order['duration_code'])} — {rp(order['price'])}\n"
        f"Buyer: {h(user.full_name)} (@{h(user.username or '-')}) <code>{user.id}</code>"
    )
    if qris_url:
        admin_msg += f"\n\n🔗 <b>Link QRIS Gateway:</b>\n<code>{qris_url}</code>"
    await notify_admin(context.bot, admin_msg, admin_kb)


async def cb_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    oid = cq.data.split(":")[1]
    own = await dbx(sb.table("orders").select("id").eq("id", oid).eq("tg_user_id", cq.from_user.id))
    if not own:
        await cq.answer(T("order_not_found"), show_alert=True)
        return
    ok = await cancel_order(context.bot, oid, "cancelled", "Dibatalkan oleh pembeli")
    await cq.answer(
        T("order_cancelled_toast") if ok else T("order_cannot_cancel"),
        show_alert=not ok,
    )


async def cb_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    oid = cq.data.split(":")[1]
    rows = await dbx(sb.table("orders").select("*").eq("id", oid).eq("tg_user_id", cq.from_user.id))
    if not rows:
        return await cq.answer(T("order_not_found"), show_alert=True)
    o = rows[0]
    
    if o["status"] != "success" or parse_dt(o["expires_at"]) <= now():
        return await cq.answer(T("rent_expired_alert"), show_alert=True)
    if not o["need_otp"]:
        return await cq.answer(T("otp_not_needed"), show_alert=True)
    if o["otp_used"]:
        return await cq.answer(T("otp_used_alert"), show_alert=True)

    # ----------------------------------------------------
    # TAHAP 1: Jangan langsung polling, tanya device dulu
    # ----------------------------------------------------
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 Login di TV (Smart TV/STB)", callback_data=f"otpsel:tv:{oid}")],
        [InlineKeyboardButton("📱 Login di HP / Laptop / Tablet", callback_data=f"otpsel:hp:{oid}")],
        [InlineKeyboardButton("❌ Batal Minta Kode", callback_data=f"otpsel:cancel:{oid}")]
    ])
    
    await cq.answer()
    try:
        await cq.edit_message_reply_markup(reply_markup=kb)
    except Exception:
        pass


# ----------------------------------------------------
# TAHAP 2: Handler baru saat pembeli memilih perangkat
# ----------------------------------------------------
async def cb_otp_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    parts = cq.data.split(":")
    device = parts[1]
    oid = parts[2]

    rows = await dbx(sb.table("orders").select("*").eq("id", oid).eq("tg_user_id", cq.from_user.id))
    if not rows:
        return await cq.answer(T("order_not_found"), show_alert=True)
    o = rows[0]

    # Jika pilih Batal, kembalikan markup ke awal
    if device == "cancel":
        await cq.answer("Dibatalkan")
        try:
            await cq.edit_message_reply_markup(reply_markup=delivery_markup(o))
        except Exception:
            pass
        return

    # Lakukan validasi ulang sebelum memantau
    if o["otp_used"]:
        return await cq.answer(T("otp_used_alert"), show_alert=True)

    email = o["email"]
    if email in ACTIVE_OTP_LOCKS:
        if now() < ACTIVE_OTP_LOCKS[email]:
            mins_left = int((ACTIVE_OTP_LOCKS[email] - now()).total_seconds() / 60) + 1
            return await cq.answer(T("otp_locked", mins=mins_left), show_alert=True)
        else:
            ACTIVE_OTP_LOCKS.pop(email, None)

    srow = await dbx(sb.table("stocks").select("mail_app_password").eq("id", o["stock_id"]))
    app_pw = srow[0].get("mail_app_password") if srow else None
    if not app_pw:
        return await cq.answer(T("otp_inactive"), show_alert=True)

    ACTIVE_OTP_LOCKS[email] = now() + timedelta(minutes=5)
    start_time = now()
    await cq.answer(T("otp_starting"))

    # Edit pesan akun, hapus tombol pilihan device supaya tidak bisa ditekan 2x
    try:
        await cq.edit_message_reply_markup(reply_markup=delivery_markup({**o, "otp_used": True}))
    except Exception:
        pass

    # Beri petunjuk berbeda sesuai perangkat
    if device == "tv":
        hint_text = "⏳ <b>Menunggu Kode/Link TV Aktif (05:00)</b>\n\nSilakan masukkan email di TV, lalu pilih <b>Kirim Link/Kode ke Email</b>. Bot akan menangkap link/kode masuk otomatis."
    else:
        hint_text = "⏳ <b>Menunggu Kode HP Aktif (05:00)</b>\n\nSilakan login dan minta kode dari aplikasi. Bot akan memantau email..."

    status_msg = await cq.message.reply_text(hint_text, parse_mode=ParseMode.HTML)

    # Jalankan polling dan teruskan informasi device
    asyncio.create_task(poll_otp_task(context.bot, o, app_pw, start_time, cq.message, status_msg, device))


async def send_thanks(bot, chat_id: int):
    text = T("thanks")
    if REVIEW_LINK:
        text += T("thanks_link", link=h(REVIEW_LINK))
    text += T("thanks_extra")
    await bot.send_message(
        chat_id, text, parse_mode=ParseMode.HTML, reply_markup=admin_contact_markup()
    )


async def auto_done_after(bot, oid: str, delay: int):
    await asyncio.sleep(delay)
    try:
        rows = await dbx(
            sb.table("orders").update({"buyer_done": True}).eq("id", oid).eq("buyer_done", False)
        )
        if not rows:
            return
        o = rows[0]
        await send_thanks(bot, o["tg_user_id"])
        await notify_admin(
            bot, f"🙌 Auto SELESAI (setelah OTP) | <code>{oid}</code> | {h(o['app_name'])}"
        )
    except Exception:
        logger.exception("auto_done_after error %s", oid)


async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    oid = cq.data.split(":")[1]
    rows = await dbx(
        sb.table("orders").update({"buyer_done": True}).eq("id", oid).eq("tg_user_id", cq.from_user.id)
    )
    if not rows:
        return await cq.answer(T("order_not_found"), show_alert=True)
    o = rows[0]
    await cq.answer()
    await send_thanks(context.bot, cq.from_user.id)
    await notify_admin(context.bot, f"🙌 Buyer menekan SELESAI | <code>{oid}</code> | {h(o['app_name'])}")


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    oid = cq.data.split(":")[1]
    await cq.answer()
    await cq.message.reply_text(
        T("help_text", order=oid),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_contact_markup(),
    )
    await notify_admin(context.bot, f"🆘 Buyer minta bantuan | <code>{oid}</code> | <code>{cq.from_user.id}</code> @{h(cq.from_user.username or '-')}")


# ============================================================
# JOB BACKGROUND
# ============================================================
async def job_payments(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    try:
        pend = await dbx(sb.table("orders").select("*").eq("status", "pending"))
    except Exception:
        logger.exception("job_payments: gagal query")
        return
    for o in pend:
        try:
            if parse_dt(o["hold_until"]) <= now():
                await cancel_order(bot, o["id"], "expired", "Waktu pembayaran habis")
                continue
                
            ref = o.get("pay_ref") or ""
            st = "pending"
            
            if ref.startswith("melda:"):
                invoice_id = ref.split(":")[1]
                st = await meldapay_status(invoice_id)
            elif PROVIDER == "midtrans" and ref != "manual":
                st = await mt_status(o["id"])
            
            if st == "paid":
                await finalize_paid(bot, o["id"])
            elif st == "failed":
                await cancel_order(bot, o["id"], "cancelled", "Pembayaran gagal / kadaluarsa")
        except Exception:
            logger.exception("job_payments: error order %s", o["id"])
    try:
        cutoff = iso(now() - timedelta(minutes=2))
        await dbx(
            sb.table("stocks")
            .update({"status": "available", "held_until": None})
            .eq("status", "held")
            .lt("held_until", cutoff)
        )
    except Exception:
        logger.exception("job_payments: gagal bersihkan hold")


async def job_rentals(context: ContextTypes.DEFAULT_TYPE):
    bot, t = context.bot, now()
    try:
        ended = await dbx(
            sb.table("orders").select("*").eq("status", "success").lte("expires_at", iso(t))
        )
        for o in ended:
            rows = await dbx(
                sb.table("orders")
                .update({"status": "finished"})
                .eq("id", o["id"])
                .eq("status", "success")
            )
            if not rows:
                continue
            await release_slot(o["slot_key"])
            try:
                await bot.send_message(
                    o["tg_user_id"],
                    T("expired", produk=f"{h(o['app_name'])} {h(o['label'])}", order=o["id"]),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            await notify_admin(
                bot,
                f"⌛ <b>SEWA BERAKHIR</b> | <code>{o['id']}</code>\n"
                f"{h(o['app_name'])} {h(o['label'])} — slot <code>{h(o['email'])}</code> "
                f"{h(o.get('profile_name') or '')} tersedia kembali.",
            )

        soon = await dbx(
            sb.table("orders")
            .select("*")
            .eq("status", "success")
            .eq("reminded", False)
            .gt("expires_at", iso(t))
            .lte("expires_at", iso(t + timedelta(minutes=REMIND_BEFORE_MIN)))
        )
        for o in soon:
            await dbx(sb.table("orders").update({"reminded": True}).eq("id", o["id"]))
            try:
                await bot.send_message(
                    o["tg_user_id"],
                    T(
                        "reminder",
                        produk=f"{h(o['app_name'])} {h(o['label'])}",
                        waktu=fmt_dt(o["expires_at"]),
                        menit=REMIND_BEFORE_MIN,
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
    except Exception:
        logger.exception("job_rentals error")


# ============================================================
# OWNER / ADMIN
# ============================================================
def admin_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Tambah Stok", callback_data="ad:add")],
            [
                InlineKeyboardButton("📦 Semua Stok", callback_data="ad:stock:0"),
                InlineKeyboardButton("🟢 Stok Ready", callback_data="ad:avail:0"),
            ],
            [
                InlineKeyboardButton("📱 Aplikasi", callback_data="ad:apps"),
                InlineKeyboardButton("🧾 Transaksi Aktif", callback_data="ad:live"),
            ],
            [
                InlineKeyboardButton("📜 Riwayat Sold Out", callback_data="ad:hist:0"),
                InlineKeyboardButton("📝 Atur Teks", callback_data="ad:txt"),
            ],
        ]
    )


ADMIN_HELP = (
    "🛠 <b>Panel Owner</b>\n\n"
    "Perintah:\n"
    "<code>/addapp Nama</code> — tambah aplikasi\n"
    "<code>/delstok ID</code> — hapus stok (hanya yg tersedia)\n"
    "<code>/edit ID field nilai</code> — field: price, label, durasi, password, pin, mailpw\n"
    "<code>/paid EMAIL</code> — list stok & keluarkan akun instan (bypass bayar)\n"
    "<code>/extend ORDER_ID Durasi</code> — ubah jadwal expires (cth: 1D, 2J, -12H)\n"
    "<code>/freestok ID_STOK</code> — paksa stok jadi available sekarang\n"
    "<code>/give ID_STOK DURASI [ID_BUYER]</code> — kirim akun / buat link klaim\n"
    "<code>/bc [pin]</code> — <b>(REPLY PESAN)</b> broadcast ke semua user\n"
    "<code>/batal</code> — batalkan mode input"
)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(ADMIN_HELP, parse_mode=ParseMode.HTML, reply_markup=admin_menu_markup())

async def cmd_bc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
        
    reply_to = update.message.reply_to_message
    if not reply_to:
        return await update.message.reply_text(
            "⚠️ <b>Cara Broadcast:</b>\n"
            "Tulis pesan (dukung emoji premium, foto, dll), lalu <b>Reply</b> pesan tersebut "
            "dengan command <code>/bc</code> atau <code>/bc pin</code>",
            parse_mode=ParseMode.HTML
        )
        
    pin_msg = len(context.args) > 0 and context.args[0].lower() == "pin"
    
    users = await dbx(sb.table("users").select("id"))
    if not users:
        return await update.message.reply_text("❌ Tidak ada user di database.")
        
    status_msg = await update.message.reply_text(f"⏳ Mulai broadcast ke {len(users)} user...")
    
    sukses = 0
    gagal = 0
    
    for u in users:
        try:
            # copy_message menjamin 100% format & emoji premium tersalin sempurna
            sent = await context.bot.copy_message(
                chat_id=u["id"],
                from_chat_id=reply_to.chat_id,
                message_id=reply_to.message_id
            )
            if pin_msg:
                try:
                    await context.bot.pin_chat_message(chat_id=u["id"], message_id=sent.message_id)
                except Exception:
                    pass # Abaikan jika gagal pin
            sukses += 1
        except Exception:
            gagal += 1
            
        await asyncio.sleep(0.05) # Rate limit protection (maks 30 pesan/detik)
        
    await status_msg.edit_text(
        f"✅ <b>Broadcast Selesai</b>\n\n"
        f"Berhasil: <b>{sukses}</b> user\n"
        f"Gagal (blokir bot): <b>{gagal}</b> user",
        parse_mode=ParseMode.HTML
    )

async def cmd_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        context.user_data.pop("await", None)
        context.user_data.pop("text_key", None)
        context.user_data.pop("give", None)
        await update.message.reply_text("Mode input dibatalkan.")


async def cmd_addapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        return await update.message.reply_text("Format: /addapp NamaAplikasi")
    exist = await dbx(sb.table("apps").select("id").ilike("name", name))
    if exist:
        return await update.message.reply_text("Aplikasi sudah ada.")
    await dbx(sb.table("apps").insert({"name": name}))
    await update.message.reply_text(f"✅ Aplikasi <b>{h(name)}</b> ditambahkan.", parse_mode=ParseMode.HTML)


async def cmd_delstok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Format: /delstok ID")
    sid = int(context.args[0])
    rows = await dbx(sb.table("stocks").select("status").eq("id", sid))
    if not rows:
        return await update.message.reply_text("Stok tidak ditemukan.")
    if rows[0]["status"] != "available":
        return await update.message.reply_text("Stok sedang di-hold / disewa, tidak bisa dihapus.")
    await dbx(sb.table("stocks").delete().eq("id", sid))
    await update.message.reply_text(f"🗑 Stok #{sid} dihapus.")


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    a = context.args
    if len(a) < 3 or not a[0].isdigit():
        return await update.message.reply_text(
            "Format: /edit ID field nilai\nField: price, label, durasi, password, pin, mailpw"
        )
    sid, field, val = int(a[0]), a[1].lower(), " ".join(a[2:])
    rows = await dbx(sb.table("stocks").select("*").eq("id", sid))
    if not rows:
        return await update.message.reply_text("Stok tidak ditemukan.")
    s = rows[0]
    try:
        if field == "price":
            await dbx(sb.table("stocks").update({"price": int(re.sub(r"\D", "", val))}).eq("id", sid))
        elif field == "label":
            await dbx(sb.table("stocks").update({"label": val}).eq("id", sid))
        elif field in ("durasi", "duration"):
            hrs = parse_duration(val)
            await dbx(sb.table("stocks").update({"duration_code": val.upper(), "duration_hours": hrs}).eq("id", sid))
        elif field == "password":
            await dbx(sb.table("stocks").update({"password": val}).eq("slot_key", s["slot_key"]))
        elif field == "pin":
            await dbx(sb.table("stocks").update({"profile_pin": val}).eq("slot_key", s["slot_key"]))
        elif field in ("mailpw", "apppw"):
            await dbx(sb.table("stocks").update({"mail_app_password": val}).eq("email", s["email"]))
        else:
            return await update.message.reply_text("Field tidak dikenal.")
    except ValueError as e:
        return await update.message.reply_text(f"⚠️ {e}")
    await update.message.reply_text(f"✅ Stok #{sid} diperbarui ({field}).")


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not context.args:
        return await update.message.reply_text("Format: /paid <email_akun>\nContoh: /paid netflix1@gmail.com")
    
    email = context.args[0].lower()
    
    # Cari stok available berdasarkan email
    rows = await dbx(
        sb.table("stocks")
        .select("*, apps(name)")
        .eq("email", email)
        .eq("status", "available")
        .order("id")
    )
    
    if not rows:
        return await update.message.reply_text(f"❌ Tidak ada stok 'available' untuk email <b>{h(email)}</b>", parse_mode=ParseMode.HTML)
    
    lines = [f"📦 <b>Stok Available:</b> <code>{h(email)}</code>\nPilih profil/durasi untuk dikeluarkan instan (Bypass & tanpa Order ID):\n"]
    kb = []
    for r in rows:
        prof = r.get("profile_name") or "Tanpa Profil"
        btn_label = f"[{prof}] {r['label']} {r['duration_code']} - {rp(r['price'])}"
        kb.append([InlineKeyboardButton(btn_label, callback_data=f"admpaid:{r['id']}")])
    
    kb.append([InlineKeyboardButton("❌ Batal", callback_data="ctos")])
    
    await update.message.reply_text(
        "\n".join(lines), 
        parse_mode=ParseMode.HTML, 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cb_admin_paid_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if cq.from_user.id not in OWNER_IDS:
        return await cq.answer("Khusus admin.", show_alert=True)
    
    sid = int(cq.data.split(":")[1])
    
    stock = await pick_and_hold(sid)
    if not stock:
        return await cq.answer("❌ Stok baru saja diambil/tidak tersedia.", show_alert=True)
    
    await cq.answer("Mengeluarkan akun...", show_alert=False)
    
    # Buat Order ID prefix DIR (Direct) agar disembunyikan teksnya
    oid = "DIR" + secrets.token_hex(4).upper()
    now_dt = now()
    expires_at = now_dt + timedelta(hours=stock["duration_hours"])
    
    order = {
        "id": oid,
        "tg_user_id": cq.from_user.id,
        "tg_username": cq.from_user.username,
        "tg_name": cq.from_user.full_name,
        "stock_id": stock["id"],
        "slot_key": stock["slot_key"],
        "app_name": stock["apps"]["name"],
        "need_otp": stock["apps"]["need_otp"],
        "label": stock["label"],
        "duration_code": stock["duration_code"],
        "duration_hours": stock["duration_hours"],
        "price": stock["price"],
        "email": stock["email"],
        "password": stock.get("password"),
        "profile_name": stock.get("profile_name"),
        "profile_pin": stock.get("profile_pin"),
        "status": "success",
        "paid_at": iso(now_dt),
        "expires_at": iso(expires_at),
    }
    
    try:
        await dbx(sb.table("orders").insert(order))
        await dbx(sb.table("stocks").update({"status": "rented", "held_until": None}).eq("slot_key", stock["slot_key"]))
        
        await safe_edit(cq, f"✅ <b>Akun berhasil dikeluarkan secara manual!</b>\nSlot <code>{h(stock['email'])}</code> telah ditandai sebagai disewa.")
        
        # Kirim langsung detail akun dengan tombol OTP (Order ID tidak akan nampil)
        await context.bot.send_message(
            cq.from_user.id,
            delivery_text(order),
            parse_mode=ParseMode.HTML,
            reply_markup=delivery_markup(order)
        )
    except Exception as e:
        logger.exception("Gagal admpaid direct")
        await release_slot(stock["slot_key"])
        await cq.message.reply_text("❌ Gagal memproses data.")


async def cmd_freestok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Format: /freestok ID_STOK\nContoh: /freestok 5")
    sid = int(context.args[0])
    rows = await dbx(sb.table("stocks").select("status, slot_key").eq("id", sid))
    if not rows:
        return await update.message.reply_text("❌ Stok tidak ditemukan.")
    slot_key = rows[0]["slot_key"]
    await release_slot(slot_key)
    await dbx(
        sb.table("orders")
        .update({"status": "finished"})
        .eq("slot_key", slot_key)
        .eq("status", "success")
    )
    await update.message.reply_text(
        f"✅ Stok #{sid} berhasil di-reset menjadi <b>available</b> dan siap disewa lagi.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text(
            "Format: /extend ORDER_ID DURASI\n"
            "Contoh tambah waktu: <code>/extend ORD123 1D</code>\n"
            "Contoh potong waktu (biar cepat ready): <code>/extend ORD123 -12H</code>",
            parse_mode=ParseMode.HTML,
        )
    oid = args[0].upper()
    dur_str = args[1].upper()
    is_minus = dur_str.startswith("-")
    clean_dur = dur_str.replace("-", "")
    try:
        hours = parse_duration(clean_dur)
        if is_minus:
            hours = -hours
    except ValueError:
        return await update.message.reply_text("❌ Format durasi salah. Gunakan H, D, W, M (contoh: 12H, 1D, -2D).")
    rows = await dbx(sb.table("orders").select("id, expires_at, status").eq("id", oid))
    if not rows:
        return await update.message.reply_text("❌ Order tidak ditemukan.")
    o = rows[0]
    if o["status"] != "success":
        return await update.message.reply_text("❌ Order ini tidak dalam masa sewa aktif.")
    current_exp = parse_dt(o["expires_at"])
    new_exp = current_exp + timedelta(hours=hours)
    await dbx(
        sb.table("orders")
        .update({"expires_at": iso(new_exp), "reminded": False})
        .eq("id", oid)
    )
    msg = (
        f"✅ <b>Jadwal Ready Diubah</b> | <code>{oid}</code>\n\n"
        f"Sebelumnya: {fmt_dt(current_exp)}\n"
        f"Menjadi: <b>{fmt_dt(new_exp)}</b>\n\n"
        f"<i>*Notifikasi bot ke buyer otomatis disesuaikan ke waktu yang baru.</i>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


def parse_paid_input(s: str) -> datetime:
    """Terima 'sekarang', 'HH:MM' (hari ini), atau 'DD/MM/YYYY HH:MM' (WIB)."""
    s = s.strip().lower().replace(".", ":")
    if s in ("sekarang", "skrg", "now"):
        return now()
    for fmt in ("%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%y %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=WIB).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        t = datetime.strptime(s, "%H:%M")
        today = datetime.now(WIB).replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return today.astimezone(timezone.utc)
    except ValueError:
        raise ValueError(
            "Format waktu salah. Contoh: <code>06/11/2026 14:30</code>, <code>14:30</code> (hari ini), atau <code>sekarang</code>"
        )


async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    args = context.args
    if len(args) < 2 or not args[0].isdigit():
        return await update.message.reply_text(
            "Format: /give ID_STOK DURASI [ID_BUYER_OPSIONAL]\n"
            "Contoh (Buat Link): <code>/give 5 1D</code>\n"
            "Contoh (Langsung Kirim): <code>/give 5 1D 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
    sid = int(args[0])
    dur = args[1].upper()
    buyer_id = int(args[2]) if len(args) > 2 and args[2].isdigit() else 0
    rows = await dbx(sb.table("stocks").select("id,status,label,apps(name)").eq("id", sid))
    if not rows or rows[0]["status"] != "available":
        return await update.message.reply_text("❌ Stok tidak ditemukan atau sedang tidak available.")
    try:
        parse_duration(dur)
    except ValueError:
        return await update.message.reply_text("❌ Format durasi salah (Gunakan 1D, 7D, 1M).")

    context.user_data["await"] = "give_paid"
    context.user_data["give"] = {"sid": sid, "dur": dur, "buyer_id": buyer_id}
    s = rows[0]
    await update.message.reply_text(
        f"🕐 <b>Kapan buyer membayar?</b>\n\n"
        f"📦 {h(s['apps']['name'])} {h(s['label'])} ({dur})\n\n"
        "Durasi akan dihitung dari waktu bayar ini. Kirim salah satu format:\n"
        "• <code>06/11/2026 14:30</code> (WIB)\n"
        "• <code>14:30</code> (hari ini)\n"
        "• <code>sekarang</code>\n\n"
        "/batal untuk membatalkan.",
        parse_mode=ParseMode.HTML,
    )


async def admin_give_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    g = context.user_data.get("give")
    if not g:
        context.user_data.pop("await", None)
        return
    try:
        paid_at = parse_paid_input(update.message.text)
    except ValueError as e:
        return await update.message.reply_text(f"⚠️ {e}\n\nCoba lagi atau /batal.", parse_mode=ParseMode.HTML)

    sid, dur, buyer_id = g["sid"], g["dur"], g["buyer_id"]
    hours = parse_duration(dur)
    expires_at = paid_at + timedelta(hours=hours)

    if paid_at > now() + timedelta(minutes=1):
        return await update.message.reply_text("⚠️ Waktu bayar tidak boleh di masa depan. Kirim ulang atau /batal.")
    if expires_at <= now():
        return await update.message.reply_text(
            f"⚠️ Dengan waktu bayar itu, masa sewa sudah berakhir ({fmt_dt(expires_at)}). "
            "Kirim waktu lain atau /batal."
        )

    # cek ulang stok (bisa saja berubah selama admin mengetik)
    rows = await dbx(sb.table("stocks").select("*, apps(name, need_otp)").eq("id", sid))
    if not rows or rows[0]["status"] != "available":
        context.user_data.pop("await", None)
        context.user_data.pop("give", None)
        return await update.message.reply_text("❌ Stok sudah tidak available.")
    stock = rows[0]

    oid = "GIFT" + secrets.token_hex(4).upper()
    order = {
        "id": oid, "tg_user_id": buyer_id, "tg_username": "", "tg_name": "Admin Gift",
        "stock_id": stock["id"], "slot_key": stock["slot_key"],
        "app_name": stock["apps"]["name"], "need_otp": stock["apps"]["need_otp"],
        "label": stock["label"], "duration_code": dur, "duration_hours": hours,
        "price": stock["price"], "email": stock["email"],
        "password": stock.get("password"), "profile_name": stock.get("profile_name"),
        "profile_pin": stock.get("profile_pin"),
        "status": "success" if buyer_id else "gift",
        "paid_at": iso(paid_at),
        "expires_at": iso(expires_at),
    }
    context.user_data.pop("await", None)
    context.user_data.pop("give", None)

    info = f"💳 Bayar: <b>{fmt_dt(paid_at)}</b>\n⏰ Berakhir: <b>{fmt_dt(expires_at)}</b>"

    if buyer_id:
        await dbx(sb.table("orders").insert(order))
        await dbx(sb.table("stocks").update({"status": "rented", "held_until": None}).eq("slot_key", stock["slot_key"]))
        await update.message.reply_text(
            f"✅ Berhasil di-assign ke ID <code>{buyer_id}</code> (Order: {oid})\n{info}",
            parse_mode=ParseMode.HTML,
        )
        try:
            await context.bot.send_message(
                buyer_id, delivery_text(order),
                parse_mode=ParseMode.HTML, reply_markup=delivery_markup(order),
            )
        except Exception:
            await update.message.reply_text("⚠️ Gagal mengirim pesan ke buyer. (Mungkin bot diblokir atau buyer belum pernah chat bot).")
    else:
        await dbx(sb.table("orders").insert(order))
        await dbx(sb.table("stocks").update({"status": "held", "held_until": iso(now() + timedelta(days=30))}).eq("slot_key", stock["slot_key"]))
        bot_user = await context.bot.get_me()
        link = f"https://t.me/{bot_user.username}?start={oid}"
        await update.message.reply_text(
            f"✅ <b>Tiket Klaim Dibuat!</b>\n\n"
            f"📦 {h(stock['apps']['name'])} {h(stock['label'])} ({dur})\n"
            f"{info}\n\n"
            f"Berikan link ini ke pembeli:\n<code>{link}</code>\n\n"
            f"<i>*Masa sewa dihitung dari waktu bayar di atas, bukan dari saat link diklik.</i>",
            parse_mode=ParseMode.HTML,
        )


async def cb_admin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if cq.from_user.id not in OWNER_IDS:
        return await cq.answer("Hanya owner yang bisa konfirmasi.", show_alert=True)
    oid = cq.data.split(":")[1]
    await finalize_paid(context.bot, oid)
    await cq.answer("Dikonfirmasi ✅")
    try:
        await cq.edit_message_reply_markup(None)
    except Exception:
        pass


async def admin_add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
    ok, errs = 0, []
    for i, line in enumerate(lines, 1):
        p = [x.strip() for x in line.split("|")]
        p += [""] * (9 - len(p))
        app_name, label, dur, price, mail, pw, prof, pin, mail_pw = p[:9]
        clean = lambda v: None if v in ("", "-") else v
        try:
            if not (app_name and label and dur and price and mail):
                raise ValueError("kolom wajib: Jenis|Label|Durasi|Harga|Email")
            hours = parse_duration(dur)
            price_i = int(re.sub(r"\D", "", price) or 0)
            if price_i <= 0:
                raise ValueError("harga tidak valid")
            found = await dbx(sb.table("apps").select("id").ilike("name", app_name))
            if found:
                app_id = found[0]["id"]
            else:
                app_id = (await dbx(sb.table("apps").insert({"name": app_name})))[0]["id"]
            prof, pw, pin = clean(prof), clean(pw), clean(pin)
            mail_pw = clean(mail_pw)
            if not mail_pw:
                ex = await dbx(
                    sb.table("stocks").select("mail_app_password")
                    .eq("email", mail).not_.is_("mail_app_password", "null").limit(1)
                )
                mail_pw = ex[0]["mail_app_password"] if ex else None
            key = slot_key_of(app_id, mail, prof or "")
            sib = await dbx(
                sb.table("stocks").select("status,held_until").eq("slot_key", key).neq("status", "available").limit(1)
            )
            row = {
                "app_id": app_id, "label": label, "duration_code": dur.upper(),
                "duration_hours": hours, "price": price_i, "email": mail,
                "password": pw, "profile_name": prof, "profile_pin": pin,
                "mail_app_password": mail_pw, "slot_key": key,
                "status": sib[0]["status"] if sib else "available",
                "held_until": sib[0]["held_until"] if sib else None,
            }
            await dbx(sb.table("stocks").insert(row))
            ok += 1
        except Exception as e:
            errs.append(f"Baris {i}: {e}")
    context.user_data.pop("await", None)
    msg = f"✅ Berhasil menambah <b>{ok}</b> stok."
    if errs:
        msg += "\n\n⚠️ Gagal:\n" + "\n".join(h(e) for e in errs)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=admin_menu_markup())


async def admin_set_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("text_key")
    if key not in TEXTS:
        context.user_data.pop("await", None)
        return
    val = update.message.text_html
    try:
        preview = val.format_map(_Safe()) if "{" in val else val
        await update.message.reply_text(
            "👁 Preview:\n\n" + preview, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Teks ditolak Telegram: {h(str(e))}\nCoba kirim ulang atau /batal.",
            parse_mode=ParseMode.HTML,
        )
        return
    await dbx(sb.table("settings").upsert({"key": key, "value": val}))
    await load_texts()
    context.user_data.pop("await", None)
    context.user_data.pop("text_key", None)
    await update.message.reply_text("✅ Teks disimpan.", reply_markup=admin_menu_markup())


def nav_row(prefix: str, page: int, has_next: bool):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}:{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}:{page + 1}"))
    return row


async def render_apps(cq):
    apps = await get_apps(active_only=False)
    lines = ["📱 <b>Aplikasi</b>\n"]
    kb = []
    for a in apps:
        lines.append(f"• {h(a['name'])} — OTP: {'ON' if a['need_otp'] else 'OFF'} | {'aktif' if a['active'] else 'nonaktif'}")
        kb.append(
            [
                InlineKeyboardButton(f"{a['name']}: OTP {'ON' if a['need_otp'] else 'OFF'}", callback_data=f"ad:otp:{a['id']}"),
                InlineKeyboardButton("👁 Tampil/Sembunyi", callback_data=f"ad:act:{a['id']}"),
                InlineKeyboardButton("🗑", callback_data=f"ad:del:{a['id']}"),
            ]
        )
    lines.append("\nTambah aplikasi: <code>/addapp Nama</code>")
    kb.append([InlineKeyboardButton("🏠 Menu", callback_data="ad:menu")])
    await safe_edit(cq, "\n".join(lines), InlineKeyboardMarkup(kb))


async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if cq.from_user.id not in OWNER_IDS:
        return await cq.answer("Khusus owner.", show_alert=True)
    parts = cq.data.split(":")
    act = parts[1]
    back = [InlineKeyboardButton("🏠 Menu", callback_data="ad:menu")]
    await cq.answer()

    if act == "menu":
        await safe_edit(cq, ADMIN_HELP, admin_menu_markup())

    elif act == "add":
        context.user_data["await"] = "add_stock"
        await safe_edit(
            cq,
            "➕ <b>Tambah Stok</b>\n\nKirim 1 baris per stok dengan format:\n"
            "<code>Jenis|Label|Durasi|Harga|Email|Password|NamaProfil|PIN|AppPasswordEmail</code>\n\n"
            "Password/Profil/PIN/AppPassword opsional (isi <code>-</code> atau kosongkan).\n"
            "Durasi: <code>12H</code>, <code>1D</code>, <code>7D</code>, <code>1M</code>.\n\n"
            "Contoh:\n"
            "<code>Netflix|1P1U|1D|2000|akun1@gmail.com|passNetflix|Profil 1|1234|abcd efgh ijkl mnop</code>\n"
            "<code>Netflix|1P1U|7D|10000|akun1@gmail.com|passNetflix|Profil 1|1234</code>\n"
            "<code>CapCut|1U1D|1D|3000|capcut@gmail.com|pw99</code>\n\n"
            "Kolom terakhir = App Password Google dari email tsb (untuk fitur OTP). "
            "Boleh kosong jika email sudah pernah diinput.\n\n"
            "Email + Profil yang sama = 1 slot yang sama (otomatis sold out di semua durasi saat disewa).\n"
            "Kirim /batal untuk membatalkan.",
            InlineKeyboardMarkup([back]),
        )

    elif act in ("stock", "avail"):
        page = int(parts[2])
        
        # Buat query dasar (tarik semua stok)
        q = sb.table("stocks").select("*, apps(name)").order("id", desc=True)
        
        # Jika tombol yang ditekan adalah 'Stok Ready', tambahkan filter available
        if act == "avail":
            q = q.eq("status", "available")
            
        rows = await dbx(q.range(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE))
        
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        icon = {"available": "🟢", "held": "🟡", "rented": "🔴"}
        
        judul = "Stok Ready" if act == "avail" else "Daftar Stok"
        lines = [f"📦 <b>{judul}</b> (hal. {page + 1})\n"]
        
        for s in rows:
            lines.append(
                f"{icon.get(s['status'], '⚪')} <code>#{s['id']}</code> {h(s['apps']['name'])} {h(s['label'])} "
                f"{h(s['duration_code'])} {rp(s['price'])}\n    📧 {h(s['email'])}"
                + (f" | 👤 {h(s['profile_name'])}" if s.get("profile_name") else "")
            )
            
        if not rows:
            lines.append("Belum ada stok." if act == "stock" else "Tidak ada stok yang ready.")
            
        if act == "stock":
            lines.append("\n🟢 tersedia  🟡 di-hold  🔴 disewa")
            
        nav = nav_row(f"ad:{act}", page, more)
        kb = [nav, back] if nav else [back]
        await safe_edit(cq, "\n".join(lines), InlineKeyboardMarkup(kb))

    elif act == "apps":
        await render_apps(cq)

    elif act in ("otp", "act", "del"):
        aid = int(parts[2])
        a = await dbx(sb.table("apps").select("*").eq("id", aid))
        if a:
            a = a[0]
            if act == "otp":
                await dbx(sb.table("apps").update({"need_otp": not a["need_otp"]}).eq("id", aid))
            elif act == "act":
                await dbx(sb.table("apps").update({"active": not a["active"]}).eq("id", aid))
            else:
                busy = await dbx(sb.table("stocks").select("id").eq("app_id", aid).neq("status", "available").limit(1))
                if busy:
                    await cq.answer("Masih ada stok disewa/di-hold.", show_alert=True)
                else:
                    await dbx(sb.table("apps").delete().eq("id", aid))
        await render_apps(cq)

    elif act == "txt":
        if len(parts) == 2:
            kb = []
            for k, v in TEXTS.items():
                kb.append([InlineKeyboardButton(v[0], callback_data=f"ad:txt:{k}")])
            kb.append(back)
            await safe_edit(cq, "📝 <b>Atur Teks</b>\n\nPilih teks yang mau diubah:", InlineKeyboardMarkup(kb))
        else:
            key = parts[2]
            if key not in TEXTS:
                return
            cur = _CACHE.get(key) or TEXTS[key][1]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Ubah", callback_data=f"ad:txtset:{key}"),
                 InlineKeyboardButton("♻️ Reset", callback_data=f"ad:txtreset:{key}")],
                [InlineKeyboardButton("⬅️ Kembali", callback_data="ad:txt")],
            ])
            await safe_edit(
                cq,
                f"📝 <b>{h(TEXTS[key][0])}</b>\n\nPlaceholder: <code>{h(TEXTS[key][2])}</code>\n\n"
                f"Saat ini:\n<pre>{h(cur)}</pre>",
                kb,
            )

    elif act == "txtset":
        key = parts[2]
        if key not in TEXTS:
            return
        context.user_data["await"] = "set_text"
        context.user_data["text_key"] = key
        await safe_edit(
            cq,
            f"✏️ Kirim teks baru untuk <b>{h(TEXTS[key][0])}</b>.\n\n"
            f"Placeholder: <code>{h(TEXTS[key][2])}</code> (tulis persis dengan kurung kurawal).\n"
            "Bold, italic, dan emoji premium boleh dipakai.\n/batal untuk membatalkan.",
            InlineKeyboardMarkup([back]),
        )

    elif act == "txtreset":
        key = parts[2]
        await dbx(sb.table("settings").delete().eq("key", key))
        await load_texts()
        await cq.answer("Dikembalikan ke default", show_alert=True)

    elif act == "hist":
        page = int(parts[2])
        rows = await dbx(
            sb.table("orders")
            .select("*")
            .in_("status", ["success", "finished"])
            .order("paid_at", desc=True)
            .range(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE)
        )
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        lines = [f"📜 <b>Riwayat Sold Out</b> (hal. {page + 1})\n"]
        for o in rows:
            lines.append(
                f"{'🔴' if o['status'] == 'success' else '✅'} <code>{o['id']}</code> {h(o['app_name'])} "
                f"{h(o['label'])} {h(o['duration_code'])} {rp(o['price'])}\n"
                f"    📧 {h(o['email'])} | @{h(o.get('tg_username') or '-')}\n"
                f"    {fmt_dt(o['paid_at'])} → {fmt_dt(o['expires_at'])}"
            )
        if not rows:
            lines.append("Belum ada riwayat.")
        nav = nav_row("ad:hist", page, more)
        await safe_edit(cq, "\n".join(lines), InlineKeyboardMarkup([nav, back] if nav else [back]))

    elif act == "live":
        rows = await dbx(
            sb.table("orders").select("*").in_("status", ["pending", "success"]).order("created_at", desc=True).limit(15)
        )
        lines = ["🧾 <b>Transaksi Aktif</b>\n"]
        for o in rows:
            ico = "🕐" if o["status"] == "pending" else "🔴"
            tail = f"habis {fmt_dt(o['expires_at'])}" if o["status"] == "success" else f"bayar sebelum {fmt_dt(o['hold_until'])}"
            lines.append(
                f"{ico} <code>{o['id']}</code> {h(o['app_name'])} {h(o['label'])} {rp(o['price'])}\n"
                f"    @{h(o.get('tg_username') or '-')} | {tail}"
            )
        if not rows:
            lines.append("Tidak ada transaksi aktif.")
        await safe_edit(cq, "\n".join(lines), InlineKeyboardMarkup([back]))


# ============================================================
# ERROR & MAIN
# ============================================================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


async def main():
    missing = [k for k, v in {"BOT_TOKEN": BOT_TOKEN, "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY, "OWNER_IDS": OWNER_IDS}.items() if not v]
    if missing:
        raise SystemExit(f"Isi dulu di .env: {', '.join(missing)}")
    if PROVIDER == "midtrans" and not MIDTRANS_SERVER_KEY:
        raise SystemExit("PAYMENT_PROVIDER=midtrans tetapi MIDTRANS_SERVER_KEY kosong")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .build()
    )
    private = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=private))
    app.add_handler(CommandHandler("batal", cmd_batal, filters=private))
    app.add_handler(CommandHandler("addapp", cmd_addapp, filters=private))
    app.add_handler(CommandHandler("delstok", cmd_delstok, filters=private))
    app.add_handler(CommandHandler("edit", cmd_edit, filters=private))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(CommandHandler("freestok", cmd_freestok, filters=private))
    app.add_handler(CommandHandler("extend", cmd_extend, filters=private))
    app.add_handler(CommandHandler("give", cmd_give, filters=private))
    app.add_handler(CommandHandler("bc", cmd_bc, filters=private))
    app.add_handler(CommandHandler("ppj", cmd_ppj, filters=private))

    app.add_handler(CallbackQueryHandler(cb_offer, pattern=r"^o:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_tos, pattern=r"^tos:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_tos, pattern=r"^ctos$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_order, pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(cb_otp, pattern=r"^otp:"))
    app.add_handler(CallbackQueryHandler(cb_otp_select, pattern=r"^otpsel:"))
    app.add_handler(CallbackQueryHandler(cb_done, pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(cb_help, pattern=r"^help:"))
    app.add_handler(CallbackQueryHandler(cb_admin_confirm, pattern=r"^admok:"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^ad:"))
    app.add_handler(CallbackQueryHandler(cb_admin_paid_direct, pattern=r"^admpaid:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(job_payments, interval=15, first=10)
    app.job_queue.run_repeating(job_rentals, interval=60, first=20)
    
    t_run = dt_mod.time(hour=8, minute=0, tzinfo=WIB)
    app.job_queue.run_daily(job_daily_ppj, time=t_run)

    try:
        await load_texts()
    except Exception:
        logger.exception("Gagal memuat tabel settings, pakai teks default")

    logger.info("Bot berjalan | provider pembayaran: %s", PROVIDER)

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        stop_signal = asyncio.Event()
        await stop_signal.wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot telah dimatikan dengan aman.")