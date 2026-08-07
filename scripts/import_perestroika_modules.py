#!/usr/bin/env python3
"""
Headless-версия vk-music-import: создаёт по одному плейлисту VK Музыки
на каждый module*.md файл из репозитория Perestroika и заполняет его
найденными треками. Не использует GUI (PySide2) — только vk_api.

Запуск:
    VK_TOKEN=xxx python3 scripts/import_perestroika_modules.py \
        --modules-dir ../Perestroika --strict --add-to-library=0

Токен получить по ссылке (scope=audio,offline):
    https://oauth.vk.com/oauth/authorize?client_id=6121396&scope=audio,offline&redirect_uri=https://oauth.vk.com/blank.html&display=page&response_type=token
"""
import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict

import vk_api

MODULE_FILES = [
    ("module1-forsazh.md", "🔴 Форсаж"),
    ("module2-takticheskiy-rezerv.md", "🟡 Тактический резерв"),
    ("module3-zona-dekompressii.md", "🟢 Зона декомпрессии"),
    ("module4-buffer-obmena.md", "🔵 Буфер обмена"),
    ("module5-perehodnoy-shlyuz.md", "🎧 Переходный шлюз"),
]

LINE_RE = re.compile(r"^\s*\d+\.\s+(.*?)\s+_\(#\d+.*?\)_\s*$")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[\[\]\(\)!.,'\"«»`]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def split_artist_title(line: str):
    # Разделитель в исходном плейлисте — длинное тире " — "
    for sep in (" — ", " – ", " - "):
        if sep in line:
            artist, title = line.split(sep, 1)
            return artist.strip(), title.strip()
    return None, line.strip()


def parse_module(path: str):
    tracks = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            m = LINE_RE.match(raw)
            if not m:
                continue
            artist, title = split_artist_title(m.group(1))
            if artist:
                tracks.append((artist, title))
    return tracks


def artists_match(expected: str, found: str) -> bool:
    e = normalize(expected)
    f = normalize(found)
    if e == f:
        return True
    # разбиваем составных исполнителей (feat., &, x, ,)
    parts_e = re.split(r"\bfeat\.?\b|&|,|\bx\b", e)
    parts_f = re.split(r"\bfeat\.?\b|&|,|\bx\b", f)
    parts_e = [p.strip() for p in parts_e if p.strip()]
    parts_f = [p.strip() for p in parts_f if p.strip()]
    for pe in parts_e:
        for pf in parts_f:
            if pe == pf:
                return True
    return False


def fetch_library(vk):
    """Скачивает всю личную аудиотеку пользователя (audio.get), чтобы искать
    треки среди уже имеющихся, а не через урезанный для сторонних приложений
    audio.search по общему каталогу."""
    items = []
    offset = 0
    page = 6000
    while True:
        resp = vk.audio.get(count=page, offset=offset)
        batch = resp.get("items", [])
        items.extend(batch)
        total = resp.get("count", len(items))
        offset += len(batch)
        if not batch or offset >= total:
            break
    return items


def index_library(items):
    index = defaultdict(list)
    for it in items:
        index[normalize(it.get("artist", ""))].append(it)
    return index


def best_title_match(title: str, candidates):
    title_n = normalize(title)
    best_item, best_ratio = None, 0.0
    for it in candidates:
        cand_n = normalize(it.get("title", ""))
        if cand_n == title_n:
            return it, 1.0
        ratio = difflib.SequenceMatcher(None, title_n, cand_n).ratio()
        if title_n in cand_n or cand_n in title_n:
            ratio = max(ratio, 0.85)
        if ratio > best_ratio:
            best_item, best_ratio = it, ratio
    return best_item, best_ratio


def find_in_library(library_index, artist: str, title: str):
    artist_n = normalize(artist)
    candidates = list(library_index.get(artist_n, []))
    if not candidates:
        # ищем среди составных исполнителей (feat., &, x, ,)
        parts = [p.strip() for p in re.split(r"\bfeat\.?\b|&|,|\bx\b", artist_n) if p.strip()]
        for p in parts:
            candidates.extend(library_index.get(p, []))
    if not candidates:
        return None
    item, ratio = best_title_match(title, candidates)
    return item if ratio >= 0.6 else None


def find_track(vk, library_index, artist: str, title: str, strict: bool):
    item = find_in_library(library_index, artist, title)
    if item:
        return item, "library"

    query = f"{artist} {title}"
    try:
        resp = vk.audio.search(q=query, count=10, auto_complete=1)
    except vk_api.exceptions.VkApiError as e:
        print(f"    ! ошибка поиска: {e}")
        return None, None
    for item in resp.get("items", []):
        if not strict or artists_match(artist, item.get("artist", "")):
            return item, "search"
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modules-dir", default="../Perestroika")
    ap.add_argument("--strict", action="store_true", default=True)
    ap.add_argument("--no-strict", dest="strict", action="store_false")
    ap.add_argument("--add-to-library", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.4, help="пауза между запросами, сек")
    ap.add_argument("--dry-run", action="store_true", help="только найти треки, ничего не создавать/добавлять")
    args = ap.parse_args()

    token = os.environ.get("VK_TOKEN")
    if not token:
        sys.exit("Не задан VK_TOKEN (переменная окружения)")

    session = vk_api.VkApi(token=token)
    vk = session.get_api()

    print("Скачиваю твою аудиотеку VK...")
    library_items = fetch_library(vk)
    library_index = index_library(library_items)
    print(f"В библиотеке {len(library_items)} треков, будем искать сначала среди них.")

    report = {}

    for filename, playlist_title in MODULE_FILES:
        path = os.path.join(args.modules_dir, filename)
        if not os.path.exists(path):
            print(f"[!] Файл не найден: {path}")
            continue

        tracks = parse_module(path)
        print(f"\n=== {playlist_title} ({filename}) — {len(tracks)} треков ===")

        found_items = []
        not_found = []
        for artist, title in tracks:
            item, source = find_track(vk, library_index, artist, title, args.strict)
            if item:
                found_items.append((artist, title, item))
                tag = "б-ка" if source == "library" else "поиск"
                print(f"  + [{tag}] {artist} — {title}")
            else:
                not_found.append((artist, title))
                print(f"  - НЕ НАЙДЕНО: {artist} — {title}")
            if source != "library":
                # запрос ушёл в audio.search (найден он или нет) — соблюдаем паузу
                time.sleep(args.sleep)

        report[playlist_title] = {
            "total": len(tracks),
            "found": len(found_items),
            "not_found": [f"{a} — {t}" for a, t in not_found],
        }

        if args.dry_run:
            continue

        pl = vk.audio.createPlaylist(group_id=0, title=playlist_title)
        playlist_id = pl["id"]
        owner_id = pl["owner_id"]

        audio_ids = [f"{it['owner_id']}_{it['id']}" for _, _, it in found_items]
        for i in range(0, len(audio_ids), 100):
            chunk = audio_ids[i : i + 100]
            vk.audio.addToPlaylist(
                owner_id=owner_id,
                playlist_id=playlist_id,
                audio_ids=",".join(chunk),
            )
            if args.add_to_library:
                for _, _, it in found_items[i : i + 100]:
                    try:
                        vk.audio.add(audio_id=it["id"], owner_id=it["owner_id"])
                    except vk_api.exceptions.VkApiError as e:
                        print(f"    ! не удалось добавить в библиотеку: {e}")
            time.sleep(args.sleep)

        url = f"https://vk.com/audios{owner_id}?section=all&z=audio_playlist{owner_id}_{playlist_id}"
        report[playlist_title]["url"] = url
        print(f"  -> плейлист создан: {url}")

    print("\n\n=== ИТОГ ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    with open("import_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
