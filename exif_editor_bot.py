import os
import re
import shutil
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import piexif
from PIL import Image

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States
WAITING_PHOTO, WAITING_LOCATION, WAITING_DATETIME = range(3)

# Directory for temp photo processing
TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_photos")
os.makedirs(TEMP_DIR, exist_ok=True)


# ==========================================
# EXIF HELPER FUNCTIONS
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
    # Add standard padding (2KB) so XMP can be edited in-place without rewriting the file
    padding = ' ' * 2048
    xmp_xml += padding + '<?xpacket end="w"?>'

    header = b"http://ns.adobe.com/xap/1.0/\x00"
    payload = header + xmp_xml.encode('utf-8')
    segment_len = len(payload) + 2
    segment = b"\xff\xe1" + segment_len.to_bytes(2, "big") + payload
    return segment


def extract_exif_info(image_path: str):
    """Membaca koordinat dan tanggal/waktu yang sudah ada di foto jika ada."""
    info = {"lat": None, "lon": None, "datetime": None}
    try:
        exif_dict = piexif.load(image_path)
        gps = exif_dict.get("GPS", {})
        if piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps:
            lat = dms_to_decimal(gps[piexif.GPSIFD.GPSLatitude], gps.get(piexif.GPSIFD.GPSLatitudeRef, 'N'))
            lon = dms_to_decimal(gps[piexif.GPSIFD.GPSLongitude], gps.get(piexif.GPSIFD.GPSLongitudeRef, 'E'))
            info["lat"] = lat
            info["lon"] = lon

        dt_orig = exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal) or \
                  exif_dict.get("0th", {}).get(piexif.ImageIFD.DateTime)
        if dt_orig:
            info["datetime"] = dt_orig.decode('utf-8')
    except Exception as e:
        logger.warning(f"Gagal membaca EXIF awal: {e}")
    return info

def update_photo_exif(image_path: str, output_path: str, lat: float = None, lon: float = None, datetime_str: str = None, tz_offset: str = "+08:00"):
    """Memperbarui metadata EXIF + XMP komplit tanpa kompresi ulang gambar."""
    # Parse tanggal/waktu ke datetime object
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

    # Load EXIF lama atau buat baru
    try:
        exif_dict = piexif.load(image_path)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    # Update GPS IFD
    if lat is not None and lon is not None:
        exif_dict["GPS"] = convert_to_exif_gps(lat, lon, dt_obj)

    # Update 0th IFD & Exif IFD
    exif_dict["0th"][piexif.ImageIFD.DateTime] = dt_exif_str.encode('utf-8')
    if piexif.ImageIFD.Software not in exif_dict["0th"]:
        exif_dict["0th"][piexif.ImageIFD.Software] = b"Timestamp Camera"

    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_exif_str.encode('utf-8')
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_exif_str.encode('utf-8')
    exif_dict["Exif"][piexif.ExifIFD.OffsetTime] = tz_offset.encode('utf-8')
    exif_dict["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = tz_offset.encode('utf-8')
    exif_dict["Exif"][piexif.ExifIFD.OffsetTimeDigitized] = tz_offset.encode('utf-8')

    exif_bytes = piexif.dump(exif_dict)

    # 1. Gunakan piexif.insert untuk memperbarui EXIF segmen tanpa kompresi ulang gambar
    piexif.insert(exif_bytes, image_path, output_path)

    # 2. Baca file hasil dan selipkan segmen APP1 XMP
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
            if data_with_exif[idx:idx+2] == b"\xff\xe1":
                seg_len = int.from_bytes(data_with_exif[idx+2:idx+4], "big")
                seg_body = data_with_exif[idx+4 : idx+2+seg_len]
                if seg_body.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                    idx += 2 + seg_len
                    continue
                else:
                    cleaned_bytes.extend(data_with_exif[idx : idx+2+seg_len])
                    idx += 2 + seg_len
            else:
                cleaned_bytes.extend(data_with_exif[idx:])
                break

        exif_pos = cleaned_bytes.find(b"Exif\x00\x00")
        if exif_pos != -1:
            seg_start = cleaned_bytes.rfind(b"\xff\xe1", 0, exif_pos)
            if seg_start != -1:
                exif_seg_len = int.from_bytes(cleaned_bytes[seg_start+2 : seg_start+4], "big")
                insert_pos = seg_start + 2 + exif_seg_len
            else:
                insert_pos = 2
        else:
            insert_pos = 2

        final_bytes = cleaned_bytes[:insert_pos] + xmp_segment + cleaned_bytes[insert_pos:]

        with open(output_path, "wb") as f:
            f.write(final_bytes)



# ==========================================
# TELEGRAM BOT HANDLERS
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perintah /start"""
    text = (
        "📸 **Selamat Datang di Bot EXIF Photo Editor!**\n\n"
        "Bot ini membantu Anda mengedit metadata **Koordinat Lokasi GPS** & **Tanggal/Waktu** pada foto.\n\n"
        "⚠️ **Tips Penting**: Kirimkan foto sebagai **File / Dokumen (Uncompressed)** agar metadata foto tidak terhapus oleh kompresi Telegram.\n\n"
        "Silakan **kirimkan foto (JPG/JPEG)** sekarang untuk mulai!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return WAITING_PHOTO

async def handle_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima foto (baik dokumen maupun photo compressed)"""
    user_id = update.effective_user.id
    user_dir = os.path.join(TEMP_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    input_path = os.path.join(user_dir, "input.jpg")

    is_document = False
    if update.message.document:
        doc = update.message.document
        if not (doc.mime_type and doc.mime_type.startswith("image/")):
            await update.message.reply_text("❌ File yang dikirim bukan format gambar. Kirim foto berformat JPG/JPEG.")
            return WAITING_PHOTO
        file_obj = await doc.get_file()
        await file_obj.download_to_drive(input_path)
        is_document = True
    elif update.message.photo:
        # Foto berukuran paling besar
        photo_obj = update.message.photo[-1]
        file_obj = await photo_obj.get_file()
        await file_obj.download_to_drive(input_path)
    else:
        await update.message.reply_text("Silakan kirimkan foto JPG/JPEG sebagai File/Dokumen atau Gambar.")
        return WAITING_PHOTO

    context.user_data["input_path"] = input_path
    context.user_data["user_dir"] = user_dir

    # Cek metadata yang ada saat ini
    exif_info = extract_exif_info(input_path)
    context.user_data["orig_info"] = exif_info

    msg_info = ""
    if not is_document:
        msg_info += "⚠️ *Catatan*: Foto dikirim sebagai gambar biasa (bisa terkena kompresi). Disarankan menggunakan opsi *Send as File/Document*.\n\n"

    msg_info += "📥 **Foto Berhasil Diterima!**\n"
    if exif_info["lat"] is not None:
        msg_info += f"📍 Lokasi Terdeteksi: `{exif_info['lat']:.6f}, {exif_info['lon']:.6f}`\n"
    else:
        msg_info += "📍 Lokasi Terdeteksi: *(Belum Ada)*\n"

    if exif_info["datetime"]:
        msg_info += f"📅 Waktu Terdeteksi: `{exif_info['datetime']}`\n\n"
    else:
        msg_info += "📅 Waktu Terdeteksi: *(Belum Ada)*\n\n"

    msg_info += (
        "📍 **Langkah 1: Tentukan Lokasi Baru**\n"
        "• Kirim **Pin Lokasi** via fitur Share Location Telegram 📍\n"
        "• Atau **Ketik Koordinat** teks, contoh: `-3.3194, 114.5908`\n"
    )

    # Keyboard opsi untuk lokasi
    keyboard = [
        [KeyboardButton("📍 Kirim Lokasi Saat Ini (GPS HP)", request_location=True)],
        ["⏭️ Pakai Lokasi Foto Lama / Skip Lokasi"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

    await update.message.reply_text(msg_info, parse_mode="Markdown", reply_markup=reply_markup)
    return WAITING_LOCATION

async def handle_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima input lokasi (Location object dari Telegram atau teks koordinat desimal)"""
    lat, lon = None, None

    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
    elif update.message.text:
        text = update.message.text.strip()
        if text.startswith("⏭️"):
            # Skip lokasi, gunakan yang lama jika ada
            orig = context.user_data.get("orig_info", {})
            lat = orig.get("lat")
            lon = orig.get("lon")
        else:
            # Parse koordinat teks desimal: e.g. "-3.3194, 114.5908" or "-3.3194 114.5908"
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
        return WAITING_LOCATION

    context.user_data["new_lat"] = lat
    context.user_data["new_lon"] = lon

    # Lanjut ke Waktu/Tanggal
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
    return WAITING_DATETIME

async def handle_datetime_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima input tanggal dan waktu"""
    text = update.message.text.strip() if update.message.text else ""
    orig_dt = context.user_data.get("orig_info", {}).get("datetime")
    
    dt_str = None
    if text.startswith("🕒"):
        dt_str = get_now_gmt8().strftime("%Y-%m-%d %H:%M:%S")
    elif text.startswith("⏭️"):
        dt_str = orig_dt or get_now_gmt8().strftime("%Y-%m-%d %H:%M:%S")
    else:
        # Coba validasi teks tanggal yang diketik user
        try:
            # Mengganti karakter pemisah agar mudah diparse
            clean_text = text.replace("/", "-").replace(":", "-")
            # Ex: 2026-08-15-14-30-00 or 2026-08-15 14:30:00
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
            "Gunakan format: `YYYY-MM-DD HH:MM:SS` (Contoh: `2026-08-15 14:30:00`) atau tekan tombol di bawah.",
            parse_mode="Markdown"
        )
        return WAITING_DATETIME

    # Jalankan pengubahan EXIF
    input_path = context.user_data["input_path"]
    user_dir = context.user_data["user_dir"]
    output_path = os.path.join(user_dir, "output_exif.jpg")
    lat = context.user_data["new_lat"]
    lon = context.user_data["new_lon"]

    await update.message.reply_text("⚙️ **Memproses & memperbarui EXIF metadata foto...**", reply_markup=ReplyKeyboardRemove())

    try:
        update_photo_exif(input_path, output_path, lat=lat, lon=lon, datetime_str=dt_str)

        caption = (
            "✅ **Metadata Foto Berhasil Diperbarui!**\n\n"
            f"📍 **Koordinat**: `{lat:.6f}, {lon:.6f}`\n"
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

    # Cleanup temp
    try:
        shutil.rmtree(user_dir, ignore_errors=True)
    except Exception:
        pass

    context.user_data.clear()
    await update.message.reply_text("💡 Kirimkan foto lagi kapan saja untuk mengedit foto berikutnya! /start")
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan proses"""
    user_dir = context.user_data.get("user_dir")
    if user_dir and os.path.exists(user_dir):
        shutil.rmtree(user_dir, ignore_errors=True)
    context.user_data.clear()
    await update.message.reply_text("❌ Proses dibatalkan. Kirim /start untuk mulai kembali.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ==========================================
# MAIN FUNCTION
# ==========================================

import sys
sys.stdout.reconfigure(encoding='utf-8')

def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("="*60)
        print("ERROR: TELEGRAM_BOT_TOKEN belum diset di file .env!")
        print("Silakan buka file .env dan masukkan Token dari @BotFather.")
        print("="*60)
        return

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CommandHandler("help", start_command),
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo_received)
        ],
        states={
            WAITING_PHOTO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo_received)
            ],
            WAITING_LOCATION: [
                MessageHandler(filters.LOCATION | filters.TEXT & ~filters.COMMAND, handle_location_received)
            ],
            WAITING_DATETIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_datetime_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("cancel", cancel_command))


    print("[INFO] EXIF Photo Editor Telegram Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
