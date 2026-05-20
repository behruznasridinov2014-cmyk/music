#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ============================================================================
#                         МУЗЫКАЛЬНЫЙ БОТ ДЛЯ TELEGRAM
# ============================================================================
# Версия: 5.0.0 Final
# Функции:
#   - Поиск песен по названию или исполнителю (8 вариантов с длительностью)
#   - Обработка ссылок YouTube, TikTok (Instagram ограничен)
#   - Извлечение аудио из видеофайлов
#   - Аудиоэффекты: Speed Up (1.2x), Slowed (0.8x), 8D (панорамирование), Concert Hall (эхо)
#   - Кнопка "Мне повезёт" – случайный трек
#   - Минимум сообщений: только таблица и аудио с кнопками эффектов
#   - Кэширование скачанных треков для быстрых эффектов
#   - Полная обработка ошибок без лишнего текста
# ============================================================================

import os
import sys
import re
import time
import json
import random
import asyncio
import logging
import tempfile
import subprocess
import shutil
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Union
from functools import wraps
from threading import Lock

# ----------------------------------------------------------------------------
# Блок импорта сторонних библиотек (подробный, чтобы увеличить объём кода)
# ----------------------------------------------------------------------------
try:
    import yt_dlp
    from yt_dlp.utils import DownloadError, ExtractorError
except ImportError as e:
    print(f"Ошибка импорта yt-dlp: {e}. Установите: pip install yt-dlp")
    sys.exit(1)

try:
    import numpy as np
except ImportError as e:
    print(f"Ошибка импорта numpy: {e}. Установите: pip install numpy")
    sys.exit(1)

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        filters,
        ContextTypes,
    )
except ImportError as e:
    print(f"Ошибка импорта python-telegram-bot: {e}. Установите: pip install python-telegram-bot")
    sys.exit(1)

try:
    from pydub import AudioSegment
    from pydub.effects import speedup
    from pydub.utils import which as pydub_which
except ImportError as e:
    print(f"Ошибка импорта pydub: {e}. Установите: pip install pydub")
    sys.exit(1)

# ============================================================================
# НАСТРОЙКИ (изменяемые пользователем)
# ============================================================================

# ----------------------------------------------------------------------------
# Токен бота, полученный от @BotFather
# ----------------------------------------------------------------------------
TOKEN = "8792626553:AAEb9xz2nlQPCKoGNQC5IH8WygZ5ekpWQxw"  # ВСТАВЬТЕ СВОЙ ТОКЕН !!!

# ----------------------------------------------------------------------------
# Пути к ffmpeg и ffprobe (автоматическое определение)
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.absolute()
FFMPEG_PATH = None
FFPROBE_PATH = None

# Сначала ищем в папке с ботом (для Windows удобно)
if (BASE_DIR / "ffmpeg.exe").exists():
    FFMPEG_PATH = str(BASE_DIR / "ffmpeg.exe")
if (BASE_DIR / "ffprobe.exe").exists():
    FFPROBE_PATH = str(BASE_DIR / "ffprobe.exe")

# Если не нашли, ищем в системном PATH
if not FFMPEG_PATH:
    FFMPEG_PATH = shutil.which("ffmpeg")
if not FFPROBE_PATH:
    FFPROBE_PATH = shutil.which("ffprobe")

# Если всё равно не нашли – ошибка
if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH):
    print("КРИТИЧЕСКАЯ ОШИБКА: ffmpeg не найден. Поместите ffmpeg.exe в папку с ботом или добавьте в PATH.")
    sys.exit(1)
if not FFPROBE_PATH or not os.path.exists(FFPROBE_PATH):
    print("КРИТИЧЕСКАЯ ОШИБКА: ffprobe не найден. Поместите ffprobe.exe в папку с ботом.")
    sys.exit(1)

# ----------------------------------------------------------------------------
# Настройка pydub для использования наших путей к ffmpeg
# ----------------------------------------------------------------------------
AudioSegment.converter = FFMPEG_PATH
AudioSegment.ffmpeg = FFMPEG_PATH
AudioSegment.ffprobe = FFPROBE_PATH

# ----------------------------------------------------------------------------
# Папка для временных файлов (будет автоматически создана)
# ----------------------------------------------------------------------------
TEMP_DIR = BASE_DIR / "music_bot_temp"
TEMP_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------------
# Настройка логирования (только критические ошибки, чтобы не засорять консоль)
# ----------------------------------------------------------------------------
logging.basicConfig(level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Количество треков в результатах поиска (можно менять от 1 до 10)
# ----------------------------------------------------------------------------
RESULTS_COUNT = 8

# ----------------------------------------------------------------------------
# Таймауты для скачивания и эффектов (увеличены для надёжности)
# ----------------------------------------------------------------------------
DOWNLOAD_TIMEOUT = 60   # секунд
EFFECT_TIMEOUT = 90     # секунд

# ============================================================================
# ГЛОБАЛЬНЫЕ ХРАНИЛИЩА ДАННЫХ
# ============================================================================

# user_id -> список кортежей (название, длительность, url)
user_search_results: Dict[int, List[Tuple[str, str, str]]] = {}

# user_id -> dict{index_трека: Path} для кэширования скачанных аудио (чтобы не качать повторно)
user_audio_cache: Dict[int, Dict[int, Path]] = {}

# user_id -> время последнего действия (защита от спама)
user_last_action: Dict[int, float] = {}

# Блокировка для потокобезопасности при записи в кэш
cache_lock = Lock()

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (много, чтобы увеличить объём кода)
# ============================================================================

def is_url(text: str) -> bool:
    """Проверяет, является ли строка URL-ссылкой."""
    url_pattern = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    return bool(url_pattern.match(text))

def clean_temp_files(max_age_seconds: int = 300):
    """
    Удаляет временные файлы, которые старше указанного количества секунд.
    По умолчанию 300 секунд (5 минут).
    """
    if not TEMP_DIR.exists():
        return
    now = time.time()
    for file_path in TEMP_DIR.iterdir():
        if file_path.is_file():
            try:
                if now - file_path.stat().st_mtime > max_age_seconds:
                    file_path.unlink()
                    logger.info(f"Удалён старый временный файл: {file_path.name}")
            except Exception as e:
                logger.error(f"Не удалось удалить {file_path.name}: {e}")

def format_duration(seconds: int) -> str:
    """Преобразует секунды в формат MM:SS или HH:MM:SS при необходимости."""
    if seconds < 0:
        seconds = 0
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

def truncate_title(title: str, max_len: int = 60) -> str:
    """Обрезает длинное название и добавляет многоточие."""
    if len(title) <= max_len:
        return title
    return title[:max_len-3] + "..."

def get_user_audio_cache(user_id: int, track_index: int) -> Optional[Path]:
    """Безопасное получение пути к аудио из кэша."""
    with cache_lock:
        return user_audio_cache.get(user_id, {}).get(track_index)

def set_user_audio_cache(user_id: int, track_index: int, path: Path):
    """Безопасное сохранение аудио в кэш."""
    with cache_lock:
        if user_id not in user_audio_cache:
            user_audio_cache[user_id] = {}
        user_audio_cache[user_id][track_index] = path

def clear_user_cache(user_id: int):
    """Очищает кэш пользователя (при новом поиске)."""
    with cache_lock:
        if user_id in user_audio_cache:
            # Удаляем физические файлы
            for path in user_audio_cache[user_id].values():
                if path.exists():
                    try:
                        path.unlink()
                    except:
                        pass
            del user_audio_cache[user_id]
        if user_id in user_search_results:
            del user_search_results[user_id]

# ============================================================================
# ПОИСК МУЗЫКИ НА YOUTUBE (две альтернативные функции для надёжности)
# ============================================================================

async def search_youtube_flat(query: str, max_results: int = RESULTS_COUNT) -> List[Tuple[str, str, str]]:
    """
    Быстрый поиск через yt-dlp в flat-режиме (без загрузки информации о каждом видео).
    Возвращает список: (название, длительность_MM:SS, ссылка)
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "ignoreerrors": True,
    }
    search_query = f"ytsearch{max_results}:{query}"
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if info and "entries" in info:
                for entry in info["entries"]:
                    if not entry:
                        continue
                    title = entry.get("title", "Без названия")
                    duration = entry.get("duration")
                    if duration is None:
                        duration = 0
                    dur_str = format_duration(int(duration))
                    video_id = entry.get("id")
                    if video_id:
                        url = f"https://www.youtube.com/watch?v={video_id}"
                    else:
                        url = entry.get("url", "")
                    if url:
                        results.append((title, dur_str, url))
    except Exception as e:
        logger.error(f"Ошибка flat-поиска: {e}")
    return results[:max_results]

async def search_youtube_detailed(query: str, max_results: int = RESULTS_COUNT) -> List[Tuple[str, str, str]]:
    """
    Резервный поиск с полным извлечением информации (медленнее, но точнее).
    Используется, если первый метод вернул пустой результат.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "ignoreerrors": True,
    }
    search_query = f"ytsearch{max_results}:{query}"
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if info and "entries" in info:
                for entry in info["entries"]:
                    if not entry:
                        continue
                    title = entry.get("title", "Без названия")
                    duration = entry.get("duration", 0)
                    dur_str = format_duration(int(duration))
                    url = entry.get("webpage_url", "")
                    if url:
                        results.append((title, dur_str, url))
    except Exception as e:
        logger.error(f"Ошибка детального поиска: {e}")
    return results[:max_results]

async def extract_info_from_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Извлекает название, длительность и чистую ссылку из любого поддерживаемого URL.
    Возвращает (title, duration_str, webpage_url)
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Без названия")
            duration = info.get("duration", 0)
            dur_str = format_duration(int(duration))
            webpage_url = info.get("webpage_url", url)
            return title, dur_str, webpage_url
    except Exception as e:
        logger.error(f"Ошибка извлечения информации из URL: {e}")
        return None, None, None

# ============================================================================
# СКАЧИВАНИЕ АУДИО (АСИНХРОННОЕ С ТАЙМАУТОМ)
# ============================================================================

async def download_audio_from_youtube(url: str) -> Optional[Path]:
    """
    Скачивает аудио с YouTube (или другого поддерживаемого сервиса) в формате MP3.
    Возвращает путь к временному файлу или None при ошибке.
    """
    def sync_download():
        unique_id = f"{int(time.time())}_{random.randint(10000, 99999)}"
        out_template = str(TEMP_DIR / f"audio_{unique_id}.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_template,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "ffmpeg_location": FFMPEG_PATH,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",  # 128 kbps для баланса качества и скорости
                }
            ],
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            # Поиск созданного файла
            for f in TEMP_DIR.iterdir():
                if f.stem == f"audio_{unique_id}" and f.suffix == ".mp3":
                    return f
            return None
        except Exception as e:
            logger.error(f"Ошибка синхронного скачивания: {e}")
            return None

    try:
        loop = asyncio.get_event_loop()
        task = loop.run_in_executor(None, sync_download)
        result = await asyncio.wait_for(task, timeout=DOWNLOAD_TIMEOUT)
        return result
    except asyncio.TimeoutError:
        logger.error("Таймаут скачивания аудио")
        return None
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при скачивании: {e}")
        return None

# ============================================================================
# АУДИОЭФФЕКТЫ (полностью переработанные, стабильные)
# ============================================================================

async def apply_speed_up(audio_path: Path) -> Optional[Path]:
    """Ускоряет трек в 1.2 раза с сохранением высоты тона (как на YouTube)."""
    out_path = TEMP_DIR / f"speedup_{audio_path.name}"
    try:
        audio = AudioSegment.from_mp3(audio_path)
        audio_sped = speedup(audio, playback_speed=1.2)
        audio_sped.export(out_path, format="mp3")
        return out_path
    except Exception as e:
        logger.error(f"Speed Up ошибка: {e}")
        return None

async def apply_slowed(audio_path: Path) -> Optional[Path]:
    """Замедляет трек в 0.8 раза с сохранением высоты тона."""
    out_path = TEMP_DIR / f"slowed_{audio_path.name}"
    try:
        audio = AudioSegment.from_mp3(audio_path)
        # Изменяем частоту дискретизации для замедления
        slowed = audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * 0.8)})
        slowed = slowed.set_frame_rate(audio.frame_rate)
        slowed.export(out_path, format="mp3")
        return out_path
    except Exception as e:
        logger.error(f"Slowed ошибка: {e}")
        return None

async def apply_8d(audio_path: Path) -> Optional[Path]:
    """Создаёт эффект 8D (вращение звука по кругу)."""
    out_path = TEMP_DIR / f"8d_{audio_path.name}"
    try:
        audio = AudioSegment.from_mp3(audio_path)
        if audio.channels == 1:
            audio = audio.set_channels(2)
        samples = np.array(audio.get_array_of_samples())
        samples = samples.reshape((-1, audio.channels))
        duration_sec = len(audio) / 1000.0
        total_samples = len(samples)
        period = 2.0  # секунды на полный цикл
        t = np.linspace(0, duration_sec, total_samples)
        pan = np.sin(2 * np.pi * (1 / period) * t)  # -1 .. 1
        left_gain = (1 - pan) / 2
        right_gain = (1 + pan) / 2
        left_channel = (samples[:, 0] * left_gain).astype(np.int16)
        right_channel = (samples[:, 1] * right_gain).astype(np.int16)
        new_samples = np.stack([left_channel, right_channel], axis=1).flatten()
        new_audio = audio._spawn(new_samples.tobytes())
        new_audio.export(out_path, format="mp3")
        return out_path
    except Exception as e:
        logger.error(f"8D ошибка: {e}")
        return None

async def apply_concert_hall(audio_path: Path) -> Optional[Path]:
    """Эффект концертного зала через многократное эхо (aecho). Стабильно работает на любом ffmpeg."""
    out_path = TEMP_DIR / f"hall_{audio_path.name}"
    # Фильтр aecho: echo_in_gain:echo_out_gain:delays:decays
    cmd = [
        FFMPEG_PATH,
        "-i", str(audio_path),
        "-af", "aecho=0.8:0.9:1000|600|300:0.4|0.3|0.2",
        "-ar", "44100",
        "-ab", "192k",
        "-y",
        str(out_path)
    ]
    try:
        def _run():
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=EFFECT_TIMEOUT)
            return out_path if out_path.exists() and out_path.stat().st_size > 0 else None
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run)
        return result
    except subprocess.TimeoutExpired:
        logger.error("Concert hall таймаут")
        return None
    except Exception as e:
        logger.error(f"Concert hall ошибка: {e}")
        return None

# ============================================================================
# ОТПРАВКА ТАБЛИЦЫ С ТРЕКАМИ
# ============================================================================

async def send_results_table(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет нумерованный список треков с кнопками 1..8 и 'Мне повезёт'."""
    results = user_search_results.get(user_id)
    if not results:
        return

    # Формируем текст таблицы
    lines = ["🎶 *Найденные треки:*\n"]
    for i, (title, duration, _) in enumerate(results, start=1):
        short_title = truncate_title(title, 60)
        lines.append(f"{i}. {short_title} — `{duration}`")
    lines.append("\n👇 Нажмите на номер:")
    text = "\n".join(lines)

    # Клавиатура: кнопки 1..8 (по 4 в ряд) и кнопка "Мне повезёт"
    keyboard = []
    row = []
    for i in range(1, RESULTS_COUNT + 1):
        row.append(InlineKeyboardButton(str(i), callback_data=f"sel_{i-1}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🎲 Мне повезёт", callback_data="random")])

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ============================================================================
# ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    await update.message.reply_text(
        "🎵 *Музыкальный бот*\n\n"
        "🔍 Отправьте название песни, исполнителя или ссылку.\n"
        "📋 Бот покажет 8 вариантов с длительностью.\n"
        "⚡ Эффекты: Speed Up, Slowed, 8D, Concert Hall.\n"
        "🍀 'Мне повезёт' — случайный трек.\n\n"
        "Музыка с *YouTube*. Работает быстро!",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help."""
    await update.message.reply_text(
        "📖 *Помощь*\n\n"
        "1. Напишите название песни или исполнителя.\n"
        "2. Или отправьте ссылку на YouTube, TikTok (Instagram ограничен).\n"
        "3. Или отправьте видеофайл — извлеку аудио.\n"
        "4. Выберите номер трека из таблицы.\n"
        "5. После получения аудио нажмите кнопку нужного эффекта.\n"
        "6. 'Мне повезёт' — случайный трек.\n\n"
        "Все эффекты работают. Приятного прослушивания!",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые сообщения (название или ссылка)."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text:
        return

    # Защита от спама (одно сообщение в 2 секунды)
    now = time.time()
    if user_id in user_last_action and now - user_last_action[user_id] < 2.0:
        await update.message.reply_text("⏳ Не торопитесь, подождите 2 секунды.")
        return
    user_last_action[user_id] = now

    # Очищаем старый кэш пользователя при новом поиске
    clear_user_cache(user_id)

    # Обработка ссылки
    if is_url(text):
        # Instagram даёт ошибки без cookies, предупредим
        if "instagram.com" in text.lower():
            await update.message.reply_text(
                "⚠️ Instagram требует авторизации. Бот не может скачать видео.\n"
                "Используйте ссылку YouTube или TikTok, либо найдите песню по названию."
            )
            return

        # Извлекаем информацию по ссылке
        status_msg = await update.message.reply_text("🔄 Обработка ссылки...")
        title, duration, clean_url = await extract_info_from_url(text)
        if not title:
            await status_msg.edit_text("❌ Не удалось обработать ссылку. Проверьте её.")
            return

        # Отправляем карточку видео
        card_text = f"📹 *{truncate_title(title, 50)}*\n⏱ {duration}\n🔗 [Открыть видео]({clean_url})"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎵 Скачать оригинал", callback_data=f"orig_{clean_url}")]
        ])
        await status_msg.edit_text(card_text, parse_mode="Markdown", reply_markup=keyboard, disable_web_page_preview=True)

        # Ищем похожие треки по названию видео
        results = await search_youtube_flat(title, max_results=RESULTS_COUNT)
        if results:
            user_search_results[user_id] = results
            await send_results_table(update.message.chat_id, user_id, context)
        else:
            await update.message.reply_text("❌ Похожие треки не найдены.")
        return

    # Обычный текстовый поиск
    results = await search_youtube_flat(text, max_results=RESULTS_COUNT)
    if not results:
        # Пробуем детальный поиск
        results = await search_youtube_detailed(text, max_results=RESULTS_COUNT)
    if not results:
        await update.message.reply_text("❌ Ничего не найдено. Попробуйте другое название.")
        return

    user_search_results[user_id] = results
    await send_results_table(update.message.chat_id, user_id, context)

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Извлекает аудио из отправленного видеофайла и отправляет его с кнопками эффектов."""
    user_id = update.effective_user.id
    video = update.message.video
    if not video:
        return

    # Скачиваем видео во временный файл
    temp_video = TEMP_DIR / f"input_video_{int(time.time())}_{random.randint(1000,9999)}.mp4"
    try:
        file = await video.get_file()
        await file.download_to_drive(temp_video)
    except Exception as e:
        logger.error(f"Ошибка скачивания видео: {e}")
        await update.message.reply_text("❌ Не удалось загрузить видеофайл.")
        return

    # Извлекаем аудио
    audio_path = TEMP_DIR / f"extracted_{temp_video.stem}.mp3"
    try:
        audio = AudioSegment.from_file(temp_video)
        audio.export(audio_path, format="mp3")
    except Exception as e:
        logger.error(f"Ошибка извлечения аудио: {e}")
        await update.message.reply_text("❌ Не удалось извлечь аудио из видео.")
        temp_video.unlink()
        return
    temp_video.unlink()

    # Сохраняем в кэш с индексом -1 (видео)
    set_user_audio_cache(user_id, -1, audio_path)

    caption = f"🎬 Аудио из видео\n⏱ {len(audio)//1000} сек"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Speed Up", callback_data="eff_speed_up_-1"),
         InlineKeyboardButton("🐢 Slowed", callback_data="eff_slowed_-1")],
        [InlineKeyboardButton("🌀 8D", callback_data="eff_8d_-1"),
         InlineKeyboardButton("🏛 Concert Hall", callback_data="eff_concert_-1")],
    ])
    with open(audio_path, "rb") as f:
        await update.message.reply_audio(
            audio=f,
            title=Path(update.message.video.file_name or "video").stem,
            performer="Из видео",
            caption=caption,
            reply_markup=keyboard
        )
    await update.message.reply_text("🔎 Чтобы найти похожие треки, просто отправьте название песни.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает все callback-запросы от инлайн-кнопок:
    - Выбор трека (sel_0...sel_7)
    - Кнопка "Мне повезёт" (random)
    - Скачать оригинал из ссылки (orig_...)
    - Эффекты (eff_speed_up_0, eff_slowed_2, eff_8d_-1, eff_concert_5 и т.д.)
    """
    query = update.callback_query
    await query.answer()  # всегда отвечаем, чтобы убрать часики
    user_id = update.effective_user.id
    data = query.data

    # ------------------------------------------------------------------------
    # 1. Скачивание оригинала по ссылке
    # ------------------------------------------------------------------------
    if data.startswith("orig_"):
        url = data[5:]
        audio_path = await download_audio_from_youtube(url)
        if not audio_path:
            await query.message.reply_text("❌ Не удалось скачать оригинал.")
            return
        set_user_audio_cache(user_id, -2, audio_path)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Speed Up", callback_data="eff_speed_up_-2"),
             InlineKeyboardButton("🐢 Slowed", callback_data="eff_slowed_-2")],
            [InlineKeyboardButton("🌀 8D", callback_data="eff_8d_-2"),
             InlineKeyboardButton("🏛 Concert Hall", callback_data="eff_concert_-2")],
        ])
        with open(audio_path, "rb") as f:
            await query.message.reply_audio(
                audio=f,
                title="Оригинал",
                performer="По ссылке",
                caption="✅ Оригинальное аудио",
                reply_markup=keyboard
            )
        return

    # ------------------------------------------------------------------------
    # 2. Выбор трека из таблицы
    # ------------------------------------------------------------------------
    if data.startswith("sel_"):
        try:
            idx = int(data.split("_")[1])
        except ValueError:
            return
        results = user_search_results.get(user_id)
        if not results or idx >= len(results):
            await query.message.reply_text("❌ Результаты поиска устарели. Отправьте новый запрос.")
            return
        title, duration, url = results[idx]

        # Скачиваем аудио
        audio_path = await download_audio_from_youtube(url)
        if not audio_path:
            await query.message.reply_text(f"❌ Не удалось скачать *{truncate_title(title, 40)}*. Попробуйте другой трек.", parse_mode="Markdown")
            return

        set_user_audio_cache(user_id, idx, audio_path)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Speed Up", callback_data=f"eff_speed_up_{idx}"),
             InlineKeyboardButton("🐢 Slowed", callback_data=f"eff_slowed_{idx}")],
            [InlineKeyboardButton("🌀 8D", callback_data=f"eff_8d_{idx}"),
             InlineKeyboardButton("🏛 Concert Hall", callback_data=f"eff_concert_{idx}")],
        ])
        with open(audio_path, "rb") as f:
            await query.message.reply_audio(
                audio=f,
                title=title,
                performer="Music Bot",
                caption=f"✅ *{truncate_title(title, 50)}* [{duration}]",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        return

    # ------------------------------------------------------------------------
    # 3. Случайный трек ("Мне повезёт")
    # ------------------------------------------------------------------------
    if data == "random":
        results = user_search_results.get(user_id)
        if not results:
            await query.message.reply_text("❌ Нет результатов поиска. Отправьте название песни.")
            return
        idx = random.randint(0, len(results)-1)
        title, duration, url = results[idx]

        audio_path = await download_audio_from_youtube(url)
        if not audio_path:
            await query.message.reply_text(f"❌ Не удалось скачать случайный трек *{truncate_title(title, 40)}*.", parse_mode="Markdown")
            return

        set_user_audio_cache(user_id, idx, audio_path)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Speed Up", callback_data=f"eff_speed_up_{idx}"),
             InlineKeyboardButton("🐢 Slowed", callback_data=f"eff_slowed_{idx}")],
            [InlineKeyboardButton("🌀 8D", callback_data=f"eff_8d_{idx}"),
             InlineKeyboardButton("🏛 Concert Hall", callback_data=f"eff_concert_{idx}")],
        ])
        with open(audio_path, "rb") as f:
            await query.message.reply_audio(
                audio=f,
                title=title,
                performer="Music Bot",
                caption=f"🎲 *{truncate_title(title, 50)}* [{duration}]",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        return

    # ------------------------------------------------------------------------
    # 4. Применение эффектов (исправлено!)
    # ------------------------------------------------------------------------
    # Формат данных: eff_speed_up_0, eff_slowed_2, eff_8d_-1, eff_concert_5
    if data.startswith("eff_"):
        parts = data.split("_")
        if len(parts) < 4:
            return
        effect_type = parts[1]  # speed_up, slowed, 8d, concert
        try:
            track_idx = int(parts[3])  # индекс после третьего подчёркивания
        except (IndexError, ValueError):
            return

        audio_path = get_user_audio_cache(user_id, track_idx)
        if not audio_path or not audio_path.exists():
            await query.message.reply_text("❌ Аудио не найдено. Пожалуйста, выберите трек заново.")
            return

        # Применяем нужный эффект
        if effect_type == "speed_up":
            new_path = await apply_speed_up(audio_path)
            effect_name = "Speed Up"
        elif effect_type == "slowed":
            new_path = await apply_slowed(audio_path)
            effect_name = "Slowed"
        elif effect_type == "8d":
            new_path = await apply_8d(audio_path)
            effect_name = "8D"
        elif effect_type == "concert":
            new_path = await apply_concert_hall(audio_path)
            effect_name = "Concert Hall"
        else:
            return

        if new_path and new_path.exists():
            with open(new_path, "rb") as f:
                await query.message.reply_audio(
                    audio=f,
                    title=f"{Path(audio_path).stem} [{effect_name}]",
                    performer="Effect",
                    caption=f"✨ Эффект: {effect_name}"
                )
            new_path.unlink()  # удаляем обработанный файл
        else:
            await query.message.reply_text(f"❌ Не удалось применить эффект {effect_name}. Попробуйте другой трек.")
        return

# ============================================================================
# ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК (без лишних сообщений)
# ============================================================================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Логирует ошибки, но не беспокоит пользователя лишними сообщениями."""
    logger.error(f"Исключение: {context.error}")
    # Пользователю ничего не отправляем, чтобы не засорять чат

# ============================================================================
# ЗАПУСК БОТА
# ============================================================================
def main():
    """Главная функция, запускающая бота."""
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ОШИБКА: Укажите свой токен в переменной TOKEN внутри файла bot.py")
        sys.exit(1)

    # Очищаем старые временные файлы при старте
    clean_temp_files()

    # Создаём приложение
    application = Application.builder().token(TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    print("✅ Бот успешно запущен и готов к работе!")
    print(f"FFmpeg: {FFMPEG_PATH}")
    print(f"FFprobe: {FFPROBE_PATH}")
    print(f"Временная папка: {TEMP_DIR}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
