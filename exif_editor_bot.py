import os
import re
import math
import shutil
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import piexif
from PIL import Image

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Configuration & Logging
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Whitelist User ID yang diizinkan mengakses fitur Edit / Inject Metadata
ALLOWED_EDIT_USER_IDS = {171053504, 179537807, 1232138978}
env_allowed = os.getenv("ALLOWED_EDIT_USER_IDS", "")
if env_allowed:
    try:
        for uid in env_allowed.split(","):
            if uid.strip().isdigit():
                ALLOWED_EDIT_USER_IDS.add(int(uid.strip()))
    except Exception:
        pass

def is_user_allowed_edit(user_id: int) -> bool:
    """Mengecek apakah user_id berhak mengakses fitur Edit / Inject Metadata."""
    return user_id in ALLOWED_EDIT_USER_IDS

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States
(
    MENU_CHOICE,
    WAITING_EDIT_PHOTO,
    WAITING_EDIT_LOCATION,
    WAITING_EDIT_DATETIME,
    WAITING_CHECK_PHOTO,
    WAITING_COMPARE_PHOTO_1,
    WAITING_COMPARE_PHOTO_2,
) = range(7)

# Directory for temp photo processing
TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_photos")
os.makedirs(TEMP_DIR, exist_ok=True)


# ==========================================
# EXIF & GEOLOCATION HELPER FUNCTIONS
# ==========================================

def get_now_gmt8():
    """Mengembalikan waktu saat ini dalam timezone GMT+8 (WITA/WIB+1)."""
    tz_gmt8 = timezone(timedelta(hours=8))
    return datetime.now(tz_gmt8)

def decimal_to_dms(val: float):
    """Mengubah derajat desimal ke Deg, Min, Sec."""
    abs_val = abs(val)
    deg = int(abs_val)
    rem = (abs_val - deg) * 60
    minute = int(rem)
    sec = (rem - minute) * 60
    return deg, minute, sec

def dms_to_decimal(dms, ref):
    """Mengubah format DMS EXIF ke desimal float."""
    try:
        deg = dms[0][0] / dms[0][1]
        minute = dms[1][0] / dms[1][1]
        sec = dms[2][0] / dms[2][1]
        dec = deg + (minute / 60.0) + (sec / 3600.0)
        ref_str = ref.decode('utf-8') if isinstance(ref, bytes) else str(ref)
        if ref_str.upper() in ['S', 'W']:
            dec = -dec
        return dec
    except Exception:
        return None

def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Menghitung jarak antara dua koordinat GPS (dalam meter) menggunakan formula Haversine."""
    R = 6371000.0  # Radius bumi dalam meter
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

def format_distance(dist_meters: float) -> str:
    """Format jarak agar ramah dibaca (meter / kilometer)."""
    if dist_meters < 1000:
        return f"{dist_meters:.1f} meter"
    else:
        return f"{dist_meters / 1000.0:.3f} km ({dist_meters:,.0f} meter)"

def parse_exif_datetime(dt_str: str):
    """Parse format standar EXIF YYYY:MM:DD HH:MM:SS ke datetime object jika memungkinkan."""
    if not dt_str:
        return None
    try:
        clean_str = str(dt_str).strip().replace("/", ":").replace("-", ":")
        parts = clean_str.split(" ")
        if len(parts) == 2:
            y, m, d = map(int, parts[0].split(":"))
            hh, mm, ss = map(int, parts[1].split(":"))
            return datetime(y, m, d, hh, mm, ss)
    except Exception:
        pass
    return None

def format_time_difference(dt1: datetime, dt2: datetime) -> str:
    """Format selisih waktu antara dua tanggal."""
    if not dt1 or not dt2:
        return "-"
    diff = abs(dt1 - dt2)
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days} hari")
    if hours > 0:
        parts.append(f"{hours} jam")
    if minutes > 0:
        parts.append(f"{minutes} menit")
    if seconds > 0 or not parts:
        parts.append(f"{seconds} detik")
    return " ".join(parts)

def extract_full_metadata(image_path: str) -> dict:
    """Mengekstrak seluruh metadata komprehensif dari file gambar."""
    meta = {
        "lat": None,
        "lon": None,
        "altitude": None,
        "datetime_str": None,
        "datetime_obj": None,
        "make": None,
        "model": None,
        "software": None,
        "width": None,
        "height": None,
        "megapixels": None,
        "filesize_bytes": 0,
        "filesize_human": "0 KB",
        "has_gps": False,
    }

    if os.path.exists(image_path):
        sz = os.path.getsize(image_path)
        meta["filesize_bytes"] = sz
        if sz >= 1024 * 1024:
            meta["filesize_human"] = f"{sz / (1024 * 1024):.2f} MB"
        else:
            meta["filesize_human"] = f"{sz / 1024:.1f} KB"

    try:
        with Image.open(image_path) as img:
            w, h = img.size
            meta["width"] = w
            meta["height"] = h
            meta["megapixels"] = round((w * h) / 1_000_000, 1)
    except Exception as e:
        logger.warning(f"Gagal membaca dimensi gambar: {e}")

    try:
        exif_dict = piexif.load(image_path)

        # GPS IFD
        gps = exif_dict.get("GPS", {})
        if piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps:
            lat = dms_to_decimal(gps[piexif.GPSIFD.GPSLatitude], gps.get(piexif.GPSIFD.GPSLatitudeRef, 'N'))
            lon = dms_to_decimal(gps[piexif.GPSIFD.GPSLongitude], gps.get(piexif.GPSIFD.GPSLongitudeRef, 'E'))
            if lat is not None and lon is not None:
                meta["lat"] = lat
                meta["lon"] = lon
                meta["has_gps"] = True

        if piexif.GPSIFD.GPSAltitude in gps:
            alt_tuple = gps[piexif.GPSIFD.GPSAltitude]
            if isinstance(alt_tuple, tuple) and len(alt_tuple) == 2 and alt_tuple[1] != 0:
                meta["altitude"] = alt_tuple[0] / alt_tuple[1]

        # Datetime
        dt_raw = (
            exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal) or
            exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeDigitized) or
            exif_dict.get("0th", {}).get(piexif.ImageIFD.DateTime)
        )
        if dt_raw:
            dt_decoded = dt_raw.decode('utf-8', errors='ignore') if isinstance(dt_raw, bytes) else str(dt_raw)
            meta["datetime_str"] = dt_decoded
            meta["datetime_obj"] = parse_exif_datetime(dt_decoded)

        # Device make & model
        make = exif_dict.get("0th", {}).get(piexif.ImageIFD.Make)
        if make:
            meta["make"] = make.decode('utf-8', errors='ignore').strip() if isinstance(make, bytes) else str(make).strip()

        model = exif_dict.get("0th", {}).get(piexif.ImageIFD.Model)
        if model:
            meta["model"] = model.decode('utf-8', errors='ignore').strip() if isinstance(model, bytes) else str(model).strip()

        software = exif_dict.get("0th", {}).get(piexif.ImageIFD.Software)
        if software:
            meta["software"] = software.decode('utf-8', errors='ignore').strip() if isinstance(software, bytes) else str(software).strip()

    except Exception as e:
        logger.warning(f"Gagal membaca EXIF detail: {e}")

    return meta

def format_metadata_report(meta: dict, title: str = "📊 HASIL PENGECEKAN METADATA FOTO") -> str:
    """Membuat format teks laporan metadata foto yang rapi dan detail."""
    lines = [f"**{title}**\n"]

    # GPS
    if meta.get("has_gps") and meta.get("lat") is not None and meta.get("lon") is not None:
        lat = meta["lat"]
        lon = meta["lon"]
        lines.append(f"📍 **Koordinat GPS**: `{lat:.6f}, {lon:.6f}`")
        if meta.get("altitude") is not None:
            lines.append(f"⛰️ **Ketinggian (Alt)**: `{meta['altitude']:.1f} meter`")
        lines.append(f"🗺️ **Google Maps**: [Buka di Google Maps](https://www.google.com/maps?q={lat:.6f},{lon:.6f})")
    else:
        lines.append("📍 **Koordinat GPS**: ❌ *(Tidak Ada / Foto belum ada geotag)*")

    # Waktu
    if meta.get("datetime_str"):
        lines.append(f"📅 **Waktu Pengambilan**: `{meta['datetime_str']}`")
    else:
        lines.append("📅 **Waktu Pengambilan**: ❌ *(Tidak ditemukan di EXIF)*")

    # Device
    device_parts = []
    if meta.get("make"):
        device_parts.append(meta["make"])
    if meta.get("model") and meta["model"] != meta.get("make"):
        device_parts.append(meta["model"])

    if device_parts:
        lines.append(f"📱 **Perangkat/Kamera**: `{' '.join(device_parts)}`")

    if meta.get("software"):
        lines.append(f"⚙️ **Software/Aplikasi**: `{meta['software']}`")

    # Dimensi & Ukuran File
    if meta.get("width") and meta.get("height"):
        mp_str = f" ({meta['megapixels']} MP)" if meta.get("megapixels") else ""
        lines.append(f"📐 **Resolusi**: `{meta['width']} x {meta['height']}{mp_str}`")

    if meta.get("filesize_human"):
        lines.append(f"💾 **Ukuran File**: `{meta['filesize_human']}`")

    return "\n".join(lines)


def convert_to_exif_gps(lat: float, lon: float, dt_obj: datetime = None):
    """Membuat dictionary EXIF GPS IFD komplit dari latitude, longitude, dan waktu."""
    lat_deg, lat_min, lat_sec = decimal_to_dms(lat)
    lon_deg, lon_min, lon_sec = decimal_to_dms(lon)

    lat_ref = 'N' if lat >= 0 else 'S'
    lon_ref = 'E' if lon >= 0 else 'W'

    gps_ifd = {
        piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: lat_ref.encode('ascii'),
        piexif.GPSIFD.GPSLatitude: (
            (lat_deg, 1),
            (lat_min, 1),
            (int(round(lat_sec * 10000)), 10000)
        ),
        piexif.GPSIFD.GPSLongitudeRef: lon_ref.encode('ascii'),
        piexif.GPSIFD.GPSLongitude: (
            (lon_deg, 1),
            (lon_min, 1),
            (int(round(lon_sec * 10000)), 10000)
        ),
        piexif.GPSIFD.GPSAltitudeRef: 0,
        piexif.GPSIFD.GPSAltitude: (0, 1),
        piexif.GPSIFD.GPSMapDatum: b"WGS-84",
        piexif.GPSIFD.GPSProcessingMethod: b"GPS",
    }

    if dt_obj:
        tz_gmt8 = timezone(timedelta(hours=8))
        dt_local = dt_obj.replace(tzinfo=tz_gmt8)
        dt_utc = dt_local.astimezone(timezone.utc)

        gps_ifd[piexif.GPSIFD.GPSDateStamp] = dt_utc.strftime("%Y:%m:%d").encode('ascii')
        gps_ifd[piexif.GPSIFD.GPSTimeStamp] = (
            (dt_utc.hour, 1),
            (dt_utc.minute, 1),
            (dt_utc.second, 1)
        )
    return gps_ifd

def build_xmp_segment(lat: float, lon: float, dt_obj: datetime, tz_offset: str = "+08:00"):
    """Membuat segmen APP1 XMP XML standar Adobe (xpacket format) untuk kompatibilitas penuh aplikasi seluler."""
    dt_iso = dt_obj.strftime("%Y-%m-%dT%H:%M:%S") + tz_offset

    xmp_xml = (
        '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        f'<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 6.0.0">'
        f' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'  <rdf:Description rdf:about=""'
        f'    xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
        f'    xmlns:exif="http://ns.adobe.com/exif/1.0/"'
        f'    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"'
        f'    xmp:CreateDate="{dt_iso}"'
        f'    xmp:ModifyDate="{dt_iso}"'
        f'    xmp:CreatorTool="Timestamp Camera"'
        f'    exif:CompositeImage="2"'
        f'    exif:DateTimeOriginal="{dt_iso}"'
        f'    photoshop:DateCreated="{dt_iso}"/>'
        f' </rdf:RDF>'
        f'</x:xmpmeta>'
    )
    padding = ' ' * 2048
    xmp_xml += padding + '<?xpacket end="w"?>'

    header = b"http://ns.adobe.com/xap/1.0/\x00"
    payload = header + xmp_xml.encode('utf-8')
    segment_len = len(payload) + 2
    segment = b"\xff\xe1" + segment_len.to_bytes(2, "big") + payload
    return segment

def update_photo_exif(image_path: str, output_path: str, lat: float = None, lon: float = None, datetime_str: str = None, tz_offset: str = "+08:00"):
    """Memperbarui metadata EXIF + XMP komplit tanpa kompresi ulang gambar."""
    if datetime_str:
        clean_dt = datetime_str.replace("/", "-").replace(":", "-").strip()
        match = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2})-(\d{1,2})-(\d{1,2})', clean_dt)
        if match:
            y, m, d, hh, mm, ss = map(int, match.groups())
            dt_obj = datetime(y, m, d, hh, mm, ss)
        else:
            dt_obj = get_now_gmt8()
    else:
        dt_obj = get_now_gmt8()

    dt_exif_str = dt_obj.strftime("%Y:%m:%d %H:%M:%S")

    try:
        exif_dict = piexif.load(image_path)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    if lat is not None and lon is not None:
        exif_dict["GPS"] = convert_to_exif_gps(lat, lon, dt_obj)

    exif_dict["0th"][piexif.ImageIFD.DateTime] = dt_exif_str.encode('utf-8')
    if piexif.ImageIFD.Software not in exif_dict["0th"]:
        exif_dict["0th"][piexif.ImageIFD.Software] = b"Timestamp Camera"

    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_exif_str.encode('utf-8')
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_exif_str.encode('utf-8')
    exif_dict["Exif"][piexif.ExifIFD.OffsetTime] = tz_offset.encode('utf-8')
    exif_dict["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = tz_offset.encode('utf-8')
    exif_dict["Exif"][piexif.ExifIFD.OffsetTimeDigitized] = tz_offset.encode('utf-8')

    exif_bytes = piexif.dump(exif_dict)

    # Injeksi EXIF
    piexif.insert(exif_bytes, image_path, output_path)

    # Injeksi XMP segmen
    with open(output_path, "rb") as f:
        data_with_exif = f.read()

    if lat is not None and lon is not None:
        xmp_segment = build_xmp_segment(lat, lon, dt_obj, tz_offset)

        cleaned_bytes = bytearray()
        idx = 0
        length = len(data_with_exif)

        if data_with_exif.startswith(b"\xff\xd8"):
            cleaned_bytes.extend(b"\xff\xd8")
            idx = 2

        while idx < length:
            if data_with_exif[idx:idx + 2] == b"\xff\xe1":
                seg_len = int.from_bytes(data_with_exif[idx + 2 : idx + 4], "big")
                seg_body = data_with_exif[idx + 4 : idx + 2 + seg_len]
                if seg_body.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                    idx += 2 + seg_len
                    continue
                else:
                    cleaned_bytes.extend(data_with_exif[idx : idx + 2 + seg_len])
                    idx += 2 + seg_len
            else:
                cleaned_bytes.extend(data_with_exif[idx:])
                break

        exif_pos = cleaned_bytes.find(b"Exif\x00\x00")
        if exif_pos != -1:
            seg_start = cleaned_bytes.rfind(b"\xff\xe1", 0, exif_pos)
            if seg_start != -1:
                exif_seg_len = int.from_bytes(cleaned_bytes[seg_start + 2 : seg_start + 4], "big")
                insert_pos = seg_start + 2 + exif_seg_len
            else:
                insert_pos = 2
        else:
            insert_pos = 2

        final_bytes = cleaned_bytes[:insert_pos] + xmp_segment + cleaned_bytes[insert_pos:]

        with open(output_path, "wb") as f:
            f.write(final_bytes)


# ==========================================
# TELEGRAM BOT HANDLERS & MENUS
# ==========================================

async def set_bot_commands(application):
    """Mendaftarkan menu popup command resmi di Telegram UI."""
    commands = [
        BotCommand("start", "🏠 Menu Utama"),
        BotCommand("cek", "🔍 Cek Metadata Lengkap Foto"),
        BotCommand("bandingkan", "📏 Bandingkan Jarak GPS 2 Foto"),
        BotCommand("help", "❓ Panduan Lengkap"),
        BotCommand("cancel", "❌ Batalkan Proses"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands popup successfully registered!")
    except Exception as e:
        logger.warning(f"Gagal mendaftarkan bot commands popup: {e}")

def get_main_menu_keyboard(user_id: int = None):
    """Keyboard menu utama yang disesuaikan dengan hak akses user."""
    if user_id and is_user_allowed_edit(user_id):
        keyboard = [
            ["🔍 Cek Metadata Foto", "✏️ Edit / Inject Metadata"],
            ["📏 Bandingkan 2 Foto (Cek Jarak GPS)"],
            ["❓ Bantuan / Panduan"]
        ]
    else:
        keyboard = [
            ["🔍 Cek Metadata Foto", "📏 Bandingkan 2 Foto (Cek Jarak GPS)"],
            ["❓ Bantuan / Panduan"]
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def download_incoming_photo(update: Update, destination_path: str) -> tuple[bool, bool]:
    """Helper untuk mengunduh foto yang dikirim user (baik dokumen atau photo)."""
    is_document = False
    if update.message.document:
        doc = update.message.document
        if not (doc.mime_type and doc.mime_type.startswith("image/")):
            return False, False
        file_obj = await doc.get_file()
        await file_obj.download_to_drive(destination_path)
        is_document = True
        return True, is_document
    elif update.message.photo:
        photo_obj = update.message.photo[-1]
        file_obj = await photo_obj.get_file()
        await file_obj.download_to_drive(destination_path)
        return True, False
    return False, False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /start & /menu"""
    context.user_data.clear()
    user_id = update.effective_user.id

    if is_user_allowed_edit(user_id):
        text = (
            "🤖 **Selamat Datang di Bot EXIF & Metadata Foto!**\n\n"
            "Gunakan tombol menu di bawah atau command pop-up (**/**):\n\n"
            "1️⃣ `/cek` — **🔍 Cek Metadata Foto**: Koordinat GPS, Google Maps, waktu, tipe HP & resolusi.\n"
            "2️⃣ `/edit` — **✏️ Edit / Inject Metadata**: Ubah/isi koordinat lokasi GPS dan tanggal/jam foto.\n"
            "3️⃣ `/bandingkan` — **📏 Bandingkan 2 Foto**: Hitung selisih jarak koordinat GPS (meter/km) & waktu.\n\n"
            "💡 *Tips: Kamu juga bisa langsung kirim foto sekarang untuk langsung mengecek metadatanya.*"
        )
    else:
        text = (
            "🤖 **Selamat Datang di Bot EXIF & Metadata Foto!**\n\n"
            "Gunakan tombol menu di bawah atau command pop-up (**/**):\n\n"
            "1️⃣ `/cek` — **🔍 Cek Metadata Foto**: Koordinat GPS, Google Maps, waktu, tipe HP & resolusi.\n"
            "2️⃣ `/bandingkan` — **📏 Bandingkan 2 Foto**: Hitung selisih jarak koordinat GPS (meter/km) & waktu.\n\n"
            "💡 *Tips: Kamu juga bisa langsung kirim foto sekarang untuk langsung mengecek metadatanya.*"
        )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard(user_id))
    return MENU_CHOICE

async def cek_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler perintah /cek"""
    context.user_data.clear()
    context.user_data["mode"] = "check"
    await update.message.reply_text(
        "🔍 **Mode: Cek Metadata Foto**\n\n"
        "Silakan **kirimkan foto (JPG/JPEG)** sekarang.\n"
        "⚠️ Disarankan kirim sebagai **File/Dokumen** agar metadata tidak terhapus kompresi Telegram.\n\n"
        "Ketik /cancel untuk kembali ke Menu Utama.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_CHECK_PHOTO

async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler perintah /edit (Dibatasi hanya untuk User ID yang terdaftar)."""
    user_id = update.effective_user.id
    if not is_user_allowed_edit(user_id):
        await update.message.reply_text(
            "⛔ **Akses Terbatas**: Menu Edit/Inject Metadata dibatasi khusus untuk admin.\n\n"
            "Anda dapat menggunakan fitur **🔍 Cek Metadata Foto** atau **📏 Bandingkan 2 Foto**.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return MENU_CHOICE

    context.user_data.clear()
    context.user_data["mode"] = "edit"
    await update.message.reply_text(
        "✏️ **Mode: Edit / Inject Metadata**\n\n"
        "Silakan **kirimkan foto (JPG/JPEG)** yang ingin diedit.\n"
        "⚠️ Kirim sebagai **File/Dokumen** agar kualitas & EXIF asli terjaga.\n\n"
        "Ketik /cancel untuk kembali ke Menu Utama.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_EDIT_PHOTO

async def bandingkan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler perintah /bandingkan"""
    context.user_data.clear()
    context.user_data["mode"] = "compare_1"
    await update.message.reply_text(
        "📏 **Mode: Bandingkan 2 Foto (Cek Jarak GPS)**\n\n"
        "📸 Silakan kirimkan **FOTO PERTAMA (Foto 1)** sekarang.\n"
        "⚠️ Kirim sebagai **File/Dokumen** agar metadata GPS terbaca akurat.\n\n"
        "Ketik /cancel untuk membatalkan.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_COMPARE_PHOTO_1

async def handle_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengarahkan pilihan menu utama."""
    text = update.message.text.strip() if update.message.text else ""
    user_id = update.effective_user.id

    if "Cek Metadata" in text:
        return await cek_command(update, context)

    elif "Edit" in text or "Inject" in text:
        return await edit_command(update, context)

    elif "Bandingkan" in text or "Jarak" in text:
        return await bandingkan_command(update, context)

    elif "Bantuan" in text or "Panduan" in text:
        return await help_command(update, context)

    else:
        await update.message.reply_text("Silakan pilih menu dari tombol di bawah atau gunakan command pop-up (**/**):", reply_markup=get_main_menu_keyboard(user_id))
        return MENU_CHOICE

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /help"""
    user_id = update.effective_user.id
    if is_user_allowed_edit(user_id):
        help_text = (
            "📖 **PANDUAN & DAFTAR COMMAND BOT**\n\n"
            "📌 **Daftar Perintah:**\n"
            "• `/start` — Membuka Menu Utama\n"
            "• `/cek` — Cek rincian metadata lengkap foto\n"
            "• `/edit` — Edit koordinat GPS dan tanggal/jam foto (Khusus Admin)\n"
            "• `/bandingkan` — Bandingkan selisih jarak & waktu 2 foto\n"
            "• `/help` — Bantuan & panduan ini\n"
            "• `/cancel` — Batalkan proses saat ini\n\n"
            "⚠️ **Catatan Penting**: Telegram secara default mengompresi foto dan menghapus GPS saat dikirim sebagai foto biasa. Selalu gunakan opsi **'Send as File / Kirim sebagai Dokumen'** untuk hasil terbaik."
        )
    else:
        help_text = (
            "📖 **PANDUAN & DAFTAR COMMAND BOT**\n\n"
            "📌 **Daftar Perintah:**\n"
            "• `/start` — Membuka Menu Utama\n"
            "• `/cek` — Cek rincian metadata lengkap foto\n"
            "• `/bandingkan` — Bandingkan selisih jarak & waktu 2 foto\n"
            "• `/help` — Bantuan & panduan ini\n"
            "• `/cancel` — Batalkan proses saat ini\n\n"
            "⚠️ **Catatan Penting**: Telegram secara default mengompresi foto dan menghapus GPS saat dikirim sebagai foto biasa. Selalu gunakan opsi **'Send as File / Kirim sebagai Dokumen'** untuk hasil terbaik."
        )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard(user_id))
    return MENU_CHOICE


# ==========================================
# 1. CEK METADATA FLOW
# ==========================================

async def handle_check_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima foto pada mode Cek Metadata atau foto langsung di menu utama."""
    # Robust check: if currently waiting for Photo 2 of comparison, route directly!
    if context.user_data.get("mode") == "compare_2":
        return await handle_compare_photo_2(update, context)
    elif context.user_data.get("mode") == "compare_1":
        return await handle_compare_photo_1(update, context)
    elif context.user_data.get("mode") == "edit":
        return await handle_edit_photo_received(update, context)

    user_id = update.effective_user.id
    user_dir = os.path.join(TEMP_DIR, f"check_{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    input_path = os.path.join(user_dir, "check_input.jpg")

    success, is_doc = await download_incoming_photo(update, input_path)
    if not success:
        await update.message.reply_text("❌ File bukan format gambar. Silakan kirimkan foto JPG/JPEG sebagai File/Dokumen atau Gambar.")
        return WAITING_CHECK_PHOTO

    meta = extract_full_metadata(input_path)
    report = format_metadata_report(meta)

    if not is_doc:
        report += "\n\n⚠️ *Perhatian*: Foto dikirim sebagai gambar biasa (kompresi Telegram dapat menghapus tag EXIF). Kirim sebagai *Dokumen/File* untuk data asli."

    # Tombol aksi interaktif
    buttons = []
    if meta.get("has_gps"):
        lat = meta["lat"]
        lon = meta["lon"]
        buttons.append([InlineKeyboardButton("🗺️ Buka Titik di Google Maps", url=f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}")])

    action_row = []
    # Batasi tombol Edit hanya jika User ID diizinkan
    if is_user_allowed_edit(user_id):
        action_row.append(InlineKeyboardButton("✏️ Edit Foto Ini", callback_data="act_edit_this"))
    action_row.append(InlineKeyboardButton("📏 Bandingkan Foto", callback_data="act_compare_this"))
    buttons.append(action_row)

    reply_markup = InlineKeyboardMarkup(buttons)

    # Simpan path sementara jika user ingin langsung edit / bandingkan foto ini
    context.user_data["current_checked_photo"] = input_path
    context.user_data["user_dir"] = user_dir
    context.user_data["current_meta"] = meta

    await update.message.reply_text(report, parse_mode="Markdown", reply_markup=reply_markup)
    await update.message.reply_text("💡 Kirim foto lain untuk dicek, atau pilih menu di bawah:", reply_markup=get_main_menu_keyboard(user_id))
    return MENU_CHOICE


# ==========================================
# 2. EDIT / INJECT METADATA FLOW
# ==========================================

async def handle_edit_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima foto untuk diedit (Khusus User ID yang diizinkan)."""
    user_id = update.effective_user.id
    if not is_user_allowed_edit(user_id):
        await update.message.reply_text("⛔ Akses ditolak: Fitur Edit Metadata dibatasi untuk admin.", reply_markup=get_main_menu_keyboard(user_id))
        return MENU_CHOICE

    user_dir = os.path.join(TEMP_DIR, f"edit_{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    input_path = os.path.join(user_dir, "input.jpg")

    success, is_doc = await download_incoming_photo(update, input_path)
    if not success:
        await update.message.reply_text("❌ File bukan format gambar. Silakan kirimkan foto JPG/JPEG.")
        return WAITING_EDIT_PHOTO

    context.user_data["input_path"] = input_path
    context.user_data["user_dir"] = user_dir
    context.user_data["mode"] = "editing_in_progress"

    # Ekstrak info lama
    meta = extract_full_metadata(input_path)
    context.user_data["orig_meta"] = meta

    msg_info = ""
    if not is_doc:
        msg_info += "⚠️ *Catatan*: Foto dikirim sebagai gambar biasa (bisa terkompresi). Disarankan kirim sebagai *File/Dokumen*.\n\n"

    msg_info += "📥 **Foto Berhasil Diterima!**\n"
    if meta["has_gps"]:
        msg_info += f"📍 Lokasi Asli: `{meta['lat']:.6f}, {meta['lon']:.6f}`\n"
    else:
        msg_info += "📍 Lokasi Asli: *(Belum Ada Geotag)*\n"

    if meta["datetime_str"]:
        msg_info += f"📅 Waktu Asli: `{meta['datetime_str']}`\n\n"
    else:
        msg_info += "📅 Waktu Asli: *(Belum Ada di EXIF)*\n\n"

    msg_info += (
        "📍 **Langkah 1: Tentukan Lokasi Baru**\n"
        "• Kirim **Pin Lokasi** via Share Location Telegram 📍\n"
        "• Atau **Ketik Koordinat** teks, contoh: `-3.3194, 114.5908`\n"
        "• Atau ketik /cancel untuk membatalkan."
    )

    keyboard = [
        [KeyboardButton("📍 Kirim Lokasi Saat Ini (GPS HP)", request_location=True)],
        ["⏭️ Pakai Lokasi Foto Lama / Skip Lokasi"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

    await update.message.reply_text(msg_info, parse_mode="Markdown", reply_markup=reply_markup)
    return WAITING_EDIT_LOCATION

async def handle_edit_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima input lokasi baru."""
    lat, lon = None, None

    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
    elif update.message.text:
        text = update.message.text.strip()
        if text.startswith("⏭️"):
            orig = context.user_data.get("orig_meta", {})
            lat = orig.get("lat")
            lon = orig.get("lon")
        else:
            match = re.search(r'([-+]?\d+\.\d+)[,\s]+([-+]?\d+\.\d+)', text)
            if match:
                try:
                    lat = float(match.group(1))
                    lon = float(match.group(2))
                except ValueError:
                    pass

    if lat is None or lon is None:
        await update.message.reply_text(
            "❌ Format koordinat tidak dikenali.\n"
            "Kirimkan **Pin Lokasi** atau ketik format desimal seperti: `-3.3194, 114.5908`"
        )
        return WAITING_EDIT_LOCATION

    context.user_data["new_lat"] = lat
    context.user_data["new_lon"] = lon

    now_gmt8 = get_now_gmt8()
    now_str = now_gmt8.strftime("%Y-%m-%d %H:%M:%S")
    msg_dt = (
        f"✅ Lokasi diset ke: `{lat:.6f}, {lon:.6f}`\n\n"
        "📅 **Langkah 2: Tentukan Tanggal & Waktu**\n\n"
        "📋 **Format Siap Copy (Ketuk teks di bawah untuk salin)**:\n"
        f"`{now_str}`\n\n"
        "💡 *Silakan salin teks di atas, ubah tanggal/jam sesuai kebutuhan, lalu kirimkan.*\n"
        "• Atau pilih tombol instan di bawah:"
    )

    keyboard = [
        [f"🕒 Gunakan Waktu Sekarang ({now_str})"],
        ["⏭️ Pakai Waktu Foto Lama / Skip Waktu"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

    await update.message.reply_text(msg_dt, parse_mode="Markdown", reply_markup=reply_markup)
    return WAITING_EDIT_DATETIME

async def handle_edit_datetime_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima input tanggal dan waktu, lalu menginjeksi EXIF."""
    text = update.message.text.strip() if update.message.text else ""
    orig_dt = context.user_data.get("orig_meta", {}).get("datetime_str")
    user_id = update.effective_user.id

    dt_str = None
    if text.startswith("🕒"):
        dt_str = get_now_gmt8().strftime("%Y-%m-%d %H:%M:%S")
    elif text.startswith("⏭️"):
        dt_str = orig_dt or get_now_gmt8().strftime("%Y-%m-%d %H:%M:%S")
    else:
        try:
            match = re.search(r'(\d{4})[-:](\d{1,2})[-:](\d{1,2})\s+(\d{1,2})[-:](\d{1,2})[-:](\d{1,2})', text)
            if match:
                y, m, d, hh, mm, ss = match.groups()
                dt_str = f"{y}:{int(m):02d}:{int(d):02d} {int(hh):02d}:{int(mm):02d}:{int(ss):02d}"
            else:
                match_date = re.search(r'(\d{4})[-:](\d{1,2})[-:](\d{1,2})', text)
                if match_date:
                    y, m, d = match_date.groups()
                    dt_str = f"{y}:{int(m):02d}:{int(d):02d} 12:00:00"
        except Exception:
            pass

    if not dt_str:
        await update.message.reply_text(
            "❌ Format tanggal/waktu tidak valid.\n"
            "Gunakan format: `YYYY-MM-DD HH:MM:SS` (Contoh: `2026-08-19 14:30:00`) atau tekan tombol di bawah.",
            parse_mode="Markdown"
        )
        return WAITING_EDIT_DATETIME

    input_path = context.user_data["input_path"]
    user_dir = context.user_data["user_dir"]
    output_path = os.path.join(user_dir, "photo_edited_exif.jpg")
    lat = context.user_data["new_lat"]
    lon = context.user_data["new_lon"]

    await update.message.reply_text("⚙️ **Memproses & memperbarui EXIF metadata foto...**", reply_markup=ReplyKeyboardRemove())

    try:
        update_photo_exif(input_path, output_path, lat=lat, lon=lon, datetime_str=dt_str)

        caption = (
            "✅ **Metadata Foto Berhasil Diperbarui!**\n\n"
            f"📍 **Koordinat**: `{lat:.6f}, {lon:.6f}`\n"
            f"🗺️ **Maps**: [Buka di Google Maps](https://www.google.com/maps?q={lat:.6f},{lon:.6f})\n"
            f"📅 **Waktu**: `{dt_str}`\n\n"
            "📁 *File dikirim sebagai Dokumen agar metadata EXIF tetap utuh.*"
        )

        with open(output_path, "rb") as doc_file:
            await update.message.reply_document(
                document=doc_file,
                filename="photo_with_new_exif.jpg",
                caption=caption,
                parse_mode="Markdown",
                write_timeout=120,
                read_timeout=120,
                connect_timeout=30,
            )

    except Exception as e:
        logger.error(f"Gagal memproses foto: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Terjadi kesalahan saat memproses EXIF: {e}")

    try:
        shutil.rmtree(user_dir, ignore_errors=True)
    except Exception:
        pass

    context.user_data.clear()
    await update.message.reply_text("✨ Selesai! Pilih menu di bawah untuk fitur lainnya:", reply_markup=get_main_menu_keyboard(user_id))
    return MENU_CHOICE


# ==========================================
# 3. BANDINGKAN 2 FOTO (JARAK GPS & WAKTU)
# ==========================================

async def handle_compare_photo_1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima Foto 1 untuk komparasi jarak."""
    user_id = update.effective_user.id
    user_dir = os.path.join(TEMP_DIR, f"compare_{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    photo1_path = os.path.join(user_dir, "photo_1.jpg")

    success, is_doc = await download_incoming_photo(update, photo1_path)
    if not success:
        await update.message.reply_text("❌ File bukan gambar. Kirimkan **Foto 1** berformat JPG/JPEG.")
        return WAITING_COMPARE_PHOTO_1

    meta1 = extract_full_metadata(photo1_path)
    context.user_data["photo1_path"] = photo1_path
    context.user_data["user_dir"] = user_dir
    context.user_data["meta1"] = meta1
    context.user_data["mode"] = "compare_2"

    if not meta1["has_gps"]:
        await update.message.reply_text(
            "⚠️ **Peringatan**: Foto 1 tidak memiliki tag koordinat GPS!\n"
            "Pastikan foto diambil dengan GPS aktif dan dikirim sebagai **File/Dokumen**.\n\n"
            "Tetap ingin lanjut? Silakan kirimkan **FOTO KEDUA (Foto 2)** sekarang."
        )
    else:
        await update.message.reply_text(
            f"✅ **Foto 1 Berhasil Diterima!**\n"
            f"📍 Koordinat Foto 1: `{meta1['lat']:.6f}, {meta1['lon']:.6f}`\n"
            f"📅 Waktu Foto 1: `{meta1['datetime_str'] or '-'}`\n\n"
            "📸 Sekarang, silakan kirimkan **FOTO KEDUA (Foto 2)** untuk dibandingkan:",
            parse_mode="Markdown"
        )

    return WAITING_COMPARE_PHOTO_2

async def handle_compare_photo_2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima Foto 2 dan menampilkan perbandingan jarak GPS & waktu."""
    user_id = update.effective_user.id
    user_dir = context.user_data.get("user_dir") or os.path.join(TEMP_DIR, f"compare_{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    photo2_path = os.path.join(user_dir, "photo_2.jpg")

    success, is_doc = await download_incoming_photo(update, photo2_path)
    if not success:
        await update.message.reply_text("❌ File bukan gambar. Kirimkan **Foto 2** berformat JPG/JPEG.")
        return WAITING_COMPARE_PHOTO_2

    meta1 = context.user_data.get("meta1", {})
    meta2 = extract_full_metadata(photo2_path)

    has_gps1 = meta1.get("has_gps", False)
    has_gps2 = meta2.get("has_gps", False)

    lines = ["📊 **HASIL PERBANDINGAN 2 FOTO**\n"]

    # Rincian Foto 1
    lines.append("🔹 **Foto 1 (Asal)**:")
    if has_gps1:
        lines.append(f"  • GPS: `{meta1['lat']:.6f}, {meta1['lon']:.6f}`")
    else:
        lines.append("  • GPS: ❌ *(Tidak ada koordinat)*")
    lines.append(f"  • Waktu: `{meta1.get('datetime_str') or '-'}`")
    if meta1.get("model"):
        lines.append(f"  • Device: `{meta1.get('make', '')} {meta1['model']}`".strip())

    lines.append("")

    # Rincian Foto 2
    lines.append("🔹 **Foto 2 (Tujuan / Pembanding)**:")
    if has_gps2:
        lines.append(f"  • GPS: `{meta2['lat']:.6f}, {meta2['lon']:.6f}`")
    else:
        lines.append("  • GPS: ❌ *(Tidak ada koordinat)*")
    lines.append(f"  • Waktu: `{meta2.get('datetime_str') or '-'}`")
    if meta2.get("model"):
        lines.append(f"  • Device: `{meta2.get('make', '')} {meta2['model']}`".strip())

    lines.append("\n" + "—" * 25 + "\n")

    # Kalkulasi Selisih Jarak & Waktu
    buttons = []
    if has_gps1 and has_gps2:
        dist_m = calculate_haversine_distance(meta1["lat"], meta1["lon"], meta2["lat"], meta2["lon"])
        dist_formatted = format_distance(dist_m)
        lines.append(f"📏 **Selisih Jarak GPS**: **`{dist_formatted}`**")

        # Evaluasi radius praktis
        if dist_m <= 15:
            lines.append("🟢 *Status: Lokasi SANGAT IDENTIK / Titik Sama (<= 15 meter)*")
        elif dist_m <= 100:
            lines.append("🟡 *Status: Lokasi Berdekatan / Area Sama (<= 100 meter)*")
        else:
            lines.append("🔴 *Status: Lokasi Berjauhan (> 100 meter)*")

        route_url = f"https://www.google.com/maps/dir/?api=1&origin={meta1['lat']:.6f},{meta1['lon']:.6f}&destination={meta2['lat']:.6f},{meta2['lon']:.6f}"
        buttons.append([InlineKeyboardButton("🗺️ Lihat Rute Antar Foto di Google Maps", url=route_url)])
    else:
        lines.append("⚠️ **Jarak GPS Tidak Dapat Dihitung**: Salah satu atau kedua foto tidak memiliki tag koordinat GPS.")

    # Selisih waktu
    if meta1.get("datetime_obj") and meta2.get("datetime_obj"):
        time_diff = format_time_difference(meta1["datetime_obj"], meta2["datetime_obj"])
        lines.append(f"⏱️ **Selisih Waktu Pengambilan**: `{time_diff}`")

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=reply_markup)

    # Cleanup temp
    try:
        shutil.rmtree(user_dir, ignore_errors=True)
    except Exception:
        pass

    context.user_data.clear()
    await update.message.reply_text("💡 Pilih menu di bawah untuk melakukan operasi lainnya:", reply_markup=get_main_menu_keyboard(user_id))
    return MENU_CHOICE


# ==========================================
# CALLBACK QUERY & UNIVERSAL HANDLERS
# ==========================================

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani tombol inline callback."""
    query = update.callback_query
    await query.answer()

    data = query.data
    checked_path = context.user_data.get("current_checked_photo")
    meta = context.user_data.get("current_meta", {})
    user_id = update.effective_user.id

    if data == "act_edit_this":
        if not is_user_allowed_edit(user_id):
            await query.answer("⛔ Akses Ditolak: Fitur Edit hanya untuk user yang diizinkan.", show_alert=True)
            return MENU_CHOICE

        if not checked_path or not os.path.exists(checked_path):
            await query.message.reply_text("⚠️ Foto sebelumnya sudah kedaluwarsa. Silakan kirimkan foto baru untuk diedit.")
            return MENU_CHOICE

        context.user_data["input_path"] = checked_path
        context.user_data["orig_meta"] = meta
        context.user_data["mode"] = "editing_in_progress"

        msg_info = (
            "✏️ **Lanjut Edit Foto Ini**\n\n"
            "📍 **Langkah 1: Tentukan Lokasi Baru**\n"
            "• Kirim **Pin Lokasi** via Share Location Telegram 📍\n"
            "• Atau **Ketik Koordinat** teks, contoh: `-3.3194, 114.5908`\n"
        )
        keyboard = [
            [KeyboardButton("📍 Kirim Lokasi Saat Ini (GPS HP)", request_location=True)],
            ["⏭️ Pakai Lokasi Foto Lama / Skip Lokasi"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        await query.message.reply_text(msg_info, parse_mode="Markdown", reply_markup=reply_markup)
        return WAITING_EDIT_LOCATION

    elif data == "act_compare_this":
        if not checked_path or not os.path.exists(checked_path):
            await query.message.reply_text("⚠️ Foto sebelumnya sudah kedaluwarsa. Silakan pilih menu Bandingkan 2 Foto dari menu utama.")
            return MENU_CHOICE

        context.user_data["photo1_path"] = checked_path
        context.user_data["meta1"] = meta
        context.user_data["mode"] = "compare_2"

        await query.message.reply_text(
            "📸 Foto ini disimpan sebagai **Foto 1**.\n\n"
            "Sekarang, silakan kirimkan **FOTO KEDUA (Foto 2)** untuk dibandingkan:",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_COMPARE_PHOTO_2

    return MENU_CHOICE

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan operasi saat ini dan kembali ke menu utama."""
    user_id = update.effective_user.id
    user_dir = context.user_data.get("user_dir")
    if user_dir and os.path.exists(user_dir):
        shutil.rmtree(user_dir, ignore_errors=True)
    context.user_data.clear()
    await update.message.reply_text("❌ Operasi dibatalkan. Kembali ke menu utama.", reply_markup=get_main_menu_keyboard(user_id))
    return MENU_CHOICE


# ==========================================
# MAIN FUNCTION & BOT RUNNER
# ==========================================

import sys
sys.stdout.reconfigure(encoding='utf-8')

def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("=" * 60)
        print("ERROR: TELEGRAM_BOT_TOKEN belum diset di file .env!")
        print("Silakan buka file .env dan masukkan Token dari @BotFather.")
        print("=" * 60)
        return

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(set_bot_commands)
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CommandHandler("menu", start_command),
            CommandHandler("help", help_command),
            CommandHandler("cek", cek_command),
            CommandHandler("check", cek_command),
            CommandHandler("edit", edit_command),
            CommandHandler("bandingkan", bandingkan_command),
            CommandHandler("compare", bandingkan_command),
            # Direct photo handling
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_check_photo),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_choice),
            CallbackQueryHandler(handle_callback_query),
        ],
        states={
            MENU_CHOICE: [
                CommandHandler("start", start_command),
                CommandHandler("menu", start_command),
                CommandHandler("help", help_command),
                CommandHandler("cek", cek_command),
                CommandHandler("check", cek_command),
                CommandHandler("edit", edit_command),
                CommandHandler("bandingkan", bandingkan_command),
                CommandHandler("compare", bandingkan_command),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_check_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_choice),
                CallbackQueryHandler(handle_callback_query),
            ],
            WAITING_CHECK_PHOTO: [
                CommandHandler("start", start_command),
                CommandHandler("menu", start_command),
                CommandHandler("help", help_command),
                CommandHandler("cek", cek_command),
                CommandHandler("edit", edit_command),
                CommandHandler("bandingkan", bandingkan_command),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_check_photo),
                CallbackQueryHandler(handle_callback_query),
            ],
            WAITING_EDIT_PHOTO: [
                CommandHandler("start", start_command),
                CommandHandler("menu", start_command),
                CommandHandler("help", help_command),
                CommandHandler("cek", cek_command),
                CommandHandler("edit", edit_command),
                CommandHandler("bandingkan", bandingkan_command),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_edit_photo_received),
                CallbackQueryHandler(handle_callback_query),
            ],
            WAITING_EDIT_LOCATION: [
                CommandHandler("start", start_command),
                CommandHandler("menu", start_command),
                CommandHandler("help", help_command),
                CommandHandler("cek", cek_command),
                CommandHandler("edit", edit_command),
                CommandHandler("bandingkan", bandingkan_command),
                MessageHandler(filters.LOCATION | (filters.TEXT & ~filters.COMMAND), handle_edit_location_received),
                CallbackQueryHandler(handle_callback_query),
            ],
            WAITING_EDIT_DATETIME: [
                CommandHandler("start", start_command),
                CommandHandler("menu", start_command),
                CommandHandler("help", help_command),
                CommandHandler("cek", cek_command),
                CommandHandler("edit", edit_command),
                CommandHandler("bandingkan", bandingkan_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_datetime_received),
                CallbackQueryHandler(handle_callback_query),
            ],
            WAITING_COMPARE_PHOTO_1: [
                CommandHandler("start", start_command),
                CommandHandler("menu", start_command),
                CommandHandler("help", help_command),
                CommandHandler("cek", cek_command),
                CommandHandler("edit", edit_command),
                CommandHandler("bandingkan", bandingkan_command),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_compare_photo_1),
                CallbackQueryHandler(handle_callback_query),
            ],
            WAITING_COMPARE_PHOTO_2: [
                CommandHandler("start", start_command),
                CommandHandler("menu", start_command),
                CommandHandler("help", help_command),
                CommandHandler("cek", cek_command),
                CommandHandler("edit", edit_command),
                CommandHandler("bandingkan", bandingkan_command),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_compare_photo_2),
                CallbackQueryHandler(handle_callback_query),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_command),
            CommandHandler("menu", start_command),
            CommandHandler("help", help_command),
            CommandHandler("cek", cek_command),
            CommandHandler("check", cek_command),
            CommandHandler("edit", edit_command),
            CommandHandler("bandingkan", bandingkan_command),
            CommandHandler("compare", bandingkan_command),
            CallbackQueryHandler(handle_callback_query),
        ],
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("help", help_command))

    print("[INFO] Bot EXIF & Metadata Foto (Role Based Access Control) siap berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
