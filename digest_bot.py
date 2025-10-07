# filename: digest_bot.py
import os
import io
import asyncio
import logging
import datetime as dt
from typing import Dict, Any, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile, Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone as tz
from PIL import Image, ImageDraw, ImageFont

# --- анимация ---
import numpy as np
from moviepy.editor import ImageSequenceClip  # pip install moviepy imageio-ffmpeg

# ================== КОНФИГ ==================
BOT_TOKEN      = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
TARGET_CHAT_ID = os.getenv("CHAT_ID", "@your_channel")        # @public_channel или -100... для приватного
TZ_NAME        = os.getenv("TZ_NAME", "Europe/Moscow")
POST_HOUR      = int(os.getenv("POST_HOUR", "9"))
POST_MINUTE    = int(os.getenv("POST_MINUTE", "0"))

USE_TICKER     = os.getenv("USE_TICKER", "1") == "1"          # 1 — анимация, 0 — PNG
TICKER_AS_MP4  = os.getenv("TICKER_AS_MP4", "1") == "1"       # 1 — MP4, 0 — GIF
RUN_NOW        = os.getenv("RUN_NOW", "1") == "1"             # отправить сразу при старте

# скорость и длительность тикера можно править переменными окружения
TICKER_SPEED   = int(os.getenv("TICKER_SPEED", "100"))        # пикселей в секунду
TICKER_DUR     = int(os.getenv("TICKER_DUR", "14"))           # секунд анимации

MONTHS_RU = {1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",
             7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("digestbot")

# ================== ШРИФТЫ ==================
def load_font(size: int, bold=False):
    # Путь к жирному шрифту с хорошей поддержкой символов
    candidates = [
        # macOS
        "/System/Library/Fonts/Supplemental/NotoSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/NotoSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/NotoSansSymbols2-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        # Linux
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()

FONT_56 = load_font(56, bold=True)
FONT_48 = load_font(48, bold=True)
FONT_44 = load_font(44, bold=True)
FONT_40 = load_font(40, bold=True)
FONT_36 = load_font(36, bold=True)


# ================== API ==================
async def fetch_json(session, url):
    async with session.get(url, timeout=40) as r:
        r.raise_for_status()
        return await r.json()

async def get_crypto(session):
    ids = "bitcoin,ethereum,the-open-network"
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    j = await fetch_json(session, url)
    def pack(k,s): return {"symbol":s,"price":float(j[k]["usd"]), "change":float(j[k]["usd_24h_change"])}
    return {"BTC":pack("bitcoin","BTC"),
            "ETH":pack("ethereum","ETH"),
            "TON":pack("the-open-network","TON")}

async def get_fx(session):
    # несколько источников + фолбэки
    urls = [
        ("https://api.exchangerate.host/latest?base=USD&symbols=RUB","USD"),
        ("https://api.exchangerate.host/latest?base=EUR&symbols=RUB","EUR"),
        ("https://api.frankfurter.app/latest?from=USD&to=RUB","USD"),
        ("https://api.frankfurter.app/latest?from=EUR&to=RUB","EUR"),
        ("https://open.er-api.com/v6/latest/USD","USD"),
        ("https://open.er-api.com/v6/latest/EUR","EUR"),
    ]
    usd=eur=None
    for url, base in urls:
        try:
            j = await fetch_json(session, url)
            if "rates" in j and "RUB" in j["rates"]:
                if base=="USD": usd = float(j["rates"]["RUB"])
                else:           eur = float(j["rates"]["RUB"])
        except Exception:
            continue
        if usd and eur: break
    return {"USDRUB": usd or 83.0, "EURRUB": eur or 97.0}

# ================== УТИЛИТЫ РИСОВАНИЯ ==================
def rr(d, xy, r, fill): d.rounded_rectangle(xy, radius=r, fill=fill)

def nice_num(n, d=2):
    s = f"{n:,.{d}f}".replace(",", " ")
    if "." in s: s = s.rstrip("0").rstrip(".")
    return s

# ================== КЛАССИЧЕСКАЯ БЕГУЩАЯ СТРОКА (фон старого типа) ==================
def build_ticker_surface(crypto, fx, width=2400, height=220):
    """
    Тёмный минималистичный стиль тикера (как в старой версии)
    """
    # --- фон: глубокий синий ---
    background_color = (20, 26, 40)  # ровный тёмно-синий
    img = Image.new("RGB", (width, height), background_color)
    draw = ImageDraw.Draw(img)


    # --- Цвета монет и валют ---
    colors = {
        "BTC": (247, 147, 26),   # оранжевый
        "ETH": (136, 99, 255),   # фиолетовый
        "TON": (0, 163, 224),    # голубой
        "USD": (7, 170, 75),     # зелёный
        "EUR": (7, 140, 190)     # сине-бирюзовый
    }

    # --- функция формирования блока ---
    def part_coin(name, p, ch):
        arrow = "▲" if ch >= 0 else "▼"
        color = (7, 200, 85) if ch >= 0 else (240, 60, 60)
        return (f"{name} ${nice_num(p, 0 if p >= 1000 else 2)} {arrow}{abs(ch):.2f}%",
                colors.get(name, color))

    # --- текстовые сегменты ---
    segments = [
        part_coin("BTC", crypto["BTC"]["price"], crypto["BTC"]["change"]),
        part_coin("ETH", crypto["ETH"]["price"], crypto["ETH"]["change"]),
        part_coin("TON", crypto["TON"]["price"], crypto["TON"]["change"]),
        (f"$ {nice_num(fx['USDRUB'], 2)}", colors["USD"]),
        (f"€ {nice_num(fx['EURRUB'], 2)}", colors["EUR"])
    ]

    # --- параметры текста ---
    x = 40
    y = height // 2
    separator = " | "  # разделитель

    # --- отрисовка текста ---
    for seg, col in segments * 3:
        # --- Тень под текстом ---
        shadow_offset = 2
        draw.text((x + shadow_offset, y + shadow_offset), seg,
                  font=FONT_40, fill=(0, 0, 0), anchor="lm")
        draw.text((x, y), seg, font=FONT_40, fill=col, anchor="lm")

        # --- Разделитель ---
        x += draw.textlength(seg, font=FONT_40) + draw.textlength(separator, font=FONT_40)
        draw.text((x - draw.textlength(separator, font=FONT_40), y),
                  separator, font=FONT_40, fill=(220, 220, 230), anchor="lm")


    # --- лёгкие границы сверху и снизу ---
    draw.rectangle((0, 0, width, 2), fill=(40, 50, 70))
    draw.rectangle((0, height - 2, width, height), fill=(40, 50, 70))

    return img, "ticker"

def make_ticker_frames(crypto, fx, frame_w=1280, frame_h=220,
                       duration=None, fps=24, speed_px_per_s=None):
    duration = duration or TICKER_DUR
    speed_px_per_s = speed_px_per_s or TICKER_SPEED
    surf, _ = build_ticker_surface(crypto, fx, width=2400, height=frame_h)
    w = surf.size[0]
    frames = []
    shift = speed_px_per_s / fps
    total = duration * fps
    x = 0
    for _ in range(total):
        xi = int(x) % w
        if xi + frame_w <= w:
            crop = surf.crop((xi, 0, xi + frame_w, frame_h))
        else:
            p1 = surf.crop((xi, 0, w, frame_h))
            p2 = surf.crop((0, 0, frame_w - (w - xi), frame_h))
            crop = Image.new("RGB", (frame_w, frame_h))
            crop.paste(p1, (0, 0)); crop.paste(p2, (p1.size[0], 0))
        frames.append(crop)
        x += shift
    return frames

async def send_ticker(bot: Bot, as_mp4=True):
    try:
        async with aiohttp.ClientSession() as s:
            crypto, fx = await get_crypto(s), await get_fx(s)
        frames = make_ticker_frames(crypto, fx)
        if as_mp4:
            clip = ImageSequenceClip([np.array(f.convert("RGB")) for f in frames], fps=24)
            tmp = "ticker_tmp.mp4"
            clip.write_videofile(tmp, codec="libx264", audio=False, fps=24,
                                 preset="medium", bitrate="1800k",
                                 verbose=False, logger=None)
            with open(tmp, "rb") as f: raw = f.read()
            try: os.remove(tmp)
            except Exception: pass
            media = BufferedInputFile(raw, filename="ticker.mp4")
            await bot.send_animation(chat_id=TARGET_CHAT_ID, animation=media, caption="Утренний тикер 📊")
        else:
            import imageio
            bio = io.BytesIO()
            imageio.mimsave(bio, [np.array(f) for f in frames], format="GIF", duration=1/24)
            media = BufferedInputFile(bio.getvalue(), filename="ticker.gif")
            await bot.send_animation(chat_id=TARGET_CHAT_ID, animation=media, caption="Утренний тикер 📊")
        log.info("Ticker sent ✅")
    except Exception as e:
        log.exception(f"Ticker send failed: {e}")

# ================== PNG-ДАЙДЖЕСТ ==================
def render_digest(crypto, fx):
    W, H = 1280, 800
    img = Image.new("RGB", (W, H), (32,129,255))
    d = ImageDraw.Draw(img)

    def coin(x, y, color, label, val, ch):
        rr(d, (x, y, x+750, y+170), 36, fill=(232,243,255))
        d.ellipse((x+34, y+49, x+106, y+121), fill=color)
        d.text((x+70, y+85-18), label[0], anchor="mm", font=FONT_40, fill="white")
        d.text((x+140, y+30), f"$ {nice_num(val)}", font=FONT_56, fill=(30,30,30))
        arrow = "↑" if ch >= 0 else "↓"
        col = (7,170,75) if ch >= 0 else (220,60,60)
        d.text((x+140, y+130), f"{arrow}{abs(ch):.2f}%", font=FONT_36, fill=col)

    coin(40,  40, (247,147,26), "B", crypto["BTC"]["price"], crypto["BTC"]["change"])
    coin(40, 240, (136,99,255), "E", crypto["ETH"]["price"], crypto["ETH"]["change"])
    coin(40, 440, (0,163,224),  "T", crypto["TON"]["price"], crypto["TON"]["change"])

    rr(d, (830, 40, 1240, 400), 36, fill=(28,51,79))
    d.text((860, 70), "КУРС ВАЛЮТ", font=FONT_36, fill=(210,225,240))
    d.text((860,170), f"$ {nice_num(fx['USDRUB'],2)}", font=FONT_48, fill=(230,240,255))
    d.text((860,260), f"€ {nice_num(fx['EURRUB'],2)}", font=FONT_48, fill=(230,240,255))

    rr(d, (830, 420, 1240, 780), 36, fill=(79,163,255))
    now = dt.datetime.now(tz(TZ_NAME))
    d.text((1035, 540), str(now.day), anchor="mm", font=FONT_56, fill="white")
    d.text((1035, 640), MONTHS_RU[now.month], anchor="mm", font=FONT_44, fill="white")
    return img

async def send_png(bot):
    try:
        async with aiohttp.ClientSession() as s:
            crypto, fx = await get_crypto(s), await get_fx(s)
        img = render_digest(crypto, fx)
        bio = io.BytesIO(); img.save(bio, "PNG")
        await bot.send_photo(
            chat_id=TARGET_CHAT_ID,
            photo=BufferedInputFile(bio.getvalue(), "digest.png"),
            caption="Утренний дайджест 📊 #crypto #fx"
        )
        log.info("PNG sent ✅")
    except Exception as e:
        log.exception(f"PNG send failed: {e}")

# ================== TELEGRAM ==================
router = Router()

@router.message(Command("test"))
async def on_test(msg: Message, bot: Bot):
    await post_now(bot)
    await msg.answer("✅ Отправил пост в канал.")

async def post_now(bot: Bot):
    if USE_TICKER:
        await send_ticker(bot, as_mp4=TICKER_AS_MP4)
    else:
        await send_png(bot)

async def on_startup():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        log.error("BOT_TOKEN не задан"); return
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(); dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=tz(TZ_NAME))
    scheduler.add_job(post_now, "cron", [bot],
                      hour=POST_HOUR, minute=POST_MINUTE,
                      misfire_grace_time=3600, coalesce=True, jitter=30)
    scheduler.start()
    log.info(f"Расписание: {POST_HOUR:02d}:{POST_MINUTE:02d} ({TZ_NAME}); режим={'TICKER' if USE_TICKER else 'PNG'}")

    if RUN_NOW:
        await post_now(bot)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(on_startup())
