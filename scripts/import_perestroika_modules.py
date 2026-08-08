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
from datetime import datetime, timezone

import vk_api

DEFAULT_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reject_cache.json")

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


ARTIST_SPLIT_RE = re.compile(r"\bfeat\.?\b|\bft\.?\b|&|,|;|\bx\b", re.IGNORECASE)


def split_artists(s: str):
    """VK хранит совместных исполнителей одной строкой ('Тони Раут, Talibal',
    'A feat. B'), а в треклисте обычно указан только основной — разбиваем,
    чтобы искать по каждому имени в связке отдельно."""
    parts = [p.strip() for p in ARTIST_SPLIT_RE.split(s) if p.strip()]
    return parts or [s.strip()]


def artists_match(expected: str, found: str) -> bool:
    e = normalize(expected)
    f = normalize(found)
    if e == f:
        return True
    parts_e = [normalize(p) for p in split_artists(e)]
    parts_f = [normalize(p) for p in split_artists(f)]
    for pe in parts_e:
        for pf in parts_f:
            if pe == pf:
                return True
    return False


def load_reject_cache(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_reject_cache(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def get_existing_playlists(vk, owner_id):
    """title -> playlist_id для уже созданных плейлистов пользователя."""
    result = {}
    offset = 0
    page = 200
    while True:
        resp = vk.audio.getPlaylists(owner_id=owner_id, count=page, offset=offset)
        items = resp.get("items", [])
        for pl in items:
            result[pl["title"]] = pl["id"]
        total = resp.get("count", len(items))
        offset += len(items)
        if not items or offset >= total:
            break
    return result


def get_playlist_track_ids(vk, owner_id, playlist_id):
    ids = set()
    offset = 0
    page = 1000
    while True:
        resp = vk.audio.get(owner_id=owner_id, playlist_id=playlist_id, count=page, offset=offset)
        items = resp.get("items", [])
        for it in items:
            ids.add(f"{it['owner_id']}_{it['id']}")
        total = resp.get("count", len(items))
        offset += len(items)
        if not items or offset >= total:
            break
    return ids


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
        full_artist = it.get("artist", "")
        keys = {normalize(full_artist)}
        keys.update(normalize(p) for p in split_artists(full_artist))
        for key in keys:
            if key and it not in index[key]:
                index[key].append(it)
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
    seen_ids = set()
    candidates = []
    for key in {normalize(artist)} | {normalize(p) for p in split_artists(artist)}:
        for it in library_index.get(key, []):
            uid = (it.get("owner_id"), it.get("id"))
            if uid not in seen_ids:
                seen_ids.add(uid)
                candidates.append(it)
    if not candidates:
        return None
    item, ratio = best_title_match(title, candidates)
    return item if ratio >= 0.6 else None


def find_track(vk, library_index, artist: str, title: str, strict: bool, use_search: bool = True):
    item = find_in_library(library_index, artist, title)
    if item:
        return item, "library"

    if not use_search:
        return None, None

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


def make_captcha_handler(pause_after: float):
    """Ручной ввод капчи: показывает ссылку на картинку, ждёт код в терминале
    и повторяет исходный запрос. Пустой ввод — пропустить этот запрос."""

    def handler(captcha):
        print("\n    [CAPTCHA] Открой ссылку и введи код с картинки:")
        print(f"    {captcha.get_url()}")
        key = input("    Код с картинки (Enter — пропустить): ").strip()
        if not key:
            raise captcha
        try:
            return captcha.try_again(key)
        finally:
            time.sleep(pause_after)

    return handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modules-dir", default="../Perestroika")
    ap.add_argument("--strict", action="store_true", default=True)
    ap.add_argument("--no-strict", dest="strict", action="store_false")
    ap.add_argument("--add-to-library", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.0, help="пауза между запросами audio.search, сек")
    ap.add_argument("--captcha-pause", type=float, default=5.0, help="пауза после решения капчи, сек")
    ap.add_argument(
        "--max-consecutive-fail",
        type=int,
        default=8,
        help="сколько неудачных добавлений подряд считать признаком реального лимита сессии "
        "(а не единичного отказа VK на конкретный трек), после чего остановить все попытки",
    )
    ap.add_argument("--dry-run", action="store_true", help="только найти треки, ничего не создавать/добавлять")
    ap.add_argument(
        "--search-fallback",
        action="store_true",
        help="если трек не нашёлся в твоей аудиотеке — дополнительно попробовать audio.search "
        "(общий каталог VK, может потребовать капчу). По умолчанию выключено: все треки и так "
        "лежат в твоей библиотеке, искать вовне незачем.",
    )
    ap.add_argument(
        "--cache-file",
        default=DEFAULT_CACHE_FILE,
        help="куда сохранять список треков, которые VK стабильно отказывается добавлять "
        "(чтобы не пытаться заново при каждом запуске)",
    )
    ap.add_argument(
        "--retry-known-failures",
        action="store_true",
        help="всё равно попробовать треки из кэша отказов (вдруг у VK что-то поменялось)",
    )
    args = ap.parse_args()

    token = os.environ.get("VK_TOKEN")
    if not token:
        sys.exit("Не задан VK_TOKEN (переменная окружения)")

    session = vk_api.VkApi(token=token, captcha_handler=make_captcha_handler(args.captcha_pause))
    vk = session.get_api()

    user_id = vk.users.get()[0]["id"]

    print("Скачиваю твою аудиотеку VK...")
    library_items = fetch_library(vk)
    library_index = index_library(library_items)
    print(f"В библиотеке {len(library_items)} треков, будем искать сначала среди них.")

    reject_cache = load_reject_cache(args.cache_file)
    if reject_cache:
        print(f"В кэше отказов {len(reject_cache)} треков(а) (файл: {args.cache_file})")

    report = {}
    mutation_state = {"blocked": False, "consecutive_fail": 0}

    # Фаза 1: только чтение — сопоставление треков и подсчёт разницы с уже
    # существующими плейлистами. Ничего не пишем в VK на этом шаге.
    existing = get_existing_playlists(vk, user_id)
    plan = []
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
            item, source = find_track(
                vk, library_index, artist, title, args.strict, use_search=args.search_fallback
            )
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
        print(f"  Итог по модулю: {len(found_items)}/{len(tracks)} найдено, {len(not_found)} не найдено")

        if args.dry_run:
            continue

        desired_ids = {f"{it['owner_id']}_{it['id']}": it for _, _, it in found_items}

        if playlist_title in existing:
            playlist_id = existing[playlist_title]
            owner_id = user_id
            current_ids = get_playlist_track_ids(vk, owner_id, playlist_id)
            to_add = [aid for aid in desired_ids if aid not in current_ids]
            to_remove = [aid for aid in current_ids if aid not in desired_ids]
        else:
            pl = vk.audio.createPlaylist(owner_id=user_id, title=playlist_title)
            playlist_id = pl["id"]
            owner_id = pl["owner_id"]
            to_add = list(desired_ids.keys())
            to_remove = []

        if not args.retry_known_failures:
            skip_cached = [aid for aid in to_add if aid in reject_cache]
            if skip_cached:
                to_add = [aid for aid in to_add if aid not in reject_cache]
                print(
                    f"  Пропускаю {len(skip_cached)} трек(ов) из кэша отказов "
                    "(--retry-known-failures — чтобы всё же попробовать снова)"
                )

        print(f"  Плейлист id={playlist_id}: добавить {len(to_add)}, убрать {len(to_remove)}")
        plan.append(
            {
                "playlist_title": playlist_title,
                "playlist_id": playlist_id,
                "owner_id": owner_id,
                "desired_ids": desired_ids,
                "to_add": to_add,
                "to_remove": to_remove,
            }
        )

    if args.dry_run:
        print("\n\n=== ИТОГ (dry-run) ===")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # Фаза 2: применяем изменения, начиная с модулей, где дефицит меньше —
    # если квота VK на мутации кончится на середине, маленькие модули успеют
    # закрыться полностью, а не всё упрётся в самый большой ("Форсаж").
    plan.sort(key=lambda p: len(p["to_add"]) + len(p["to_remove"]))
    print(
        "\nПорядок применения изменений (сначала с наименьшим дефицитом): "
        + ", ".join(f"{p['playlist_title']} ({len(p['to_add']) + len(p['to_remove'])})" for p in plan)
    )

    for p in plan:
        playlist_title = p["playlist_title"]
        playlist_id = p["playlist_id"]
        owner_id = p["owner_id"]
        desired_ids = p["desired_ids"]
        to_add = p["to_add"]
        to_remove = p["to_remove"]
        print(f"\n--- {playlist_title} (id={playlist_id}) ---")

        # Добавляем строго по одному треку за раз. Пачками мы раньше словили
        # ложную "блокировку": пачка из 11 треков реально добавляла 9 и молча
        # роняла 2 — не потому что кончился лимит сессии, а потому что именно
        # эти конкретные треки VK не даёт скопировать (свои ограничения по
        # каталогу на отдельные записи). Поэтому теперь считаем блокировкой
        # только СЕРИЮ неудач подряд, а не любую единичную осечку.
        skipped = []
        for aid in to_add:
            if mutation_state["blocked"]:
                break
            it = desired_ids.get(aid, {})
            label = f"{it.get('artist', '?')} — {it.get('title', '?')}"
            try:
                resp = vk.audio.addToPlaylist(owner_id=owner_id, playlist_id=playlist_id, audio_ids=aid)
            except vk_api.exceptions.VkApiError as e:
                print(f"    ! ошибка добавления {label}: {e}")
                resp = None
            if resp:
                print(f"    + {label}")
                mutation_state["consecutive_fail"] = 0
                if aid in reject_cache:
                    del reject_cache[aid]
                    save_reject_cache(args.cache_file, reject_cache)
                if args.add_to_library:
                    try:
                        vk.audio.add(audio_id=it["id"], owner_id=it["owner_id"])
                    except vk_api.exceptions.VkApiError as e:
                        print(f"      ! не удалось добавить в библиотеку: {e}")
            else:
                mutation_state["consecutive_fail"] += 1
                skipped.append(aid)
                print(
                    f"    - не добавлен: {label} "
                    f"[{mutation_state['consecutive_fail']}/{args.max_consecutive_fail} неудач подряд]"
                )
                reject_cache[aid] = {
                    "artist": it.get("artist"),
                    "title": it.get("title"),
                    "fails": reject_cache.get(aid, {}).get("fails", 0) + 1,
                    "last_tried": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                save_reject_cache(args.cache_file, reject_cache)
                if mutation_state["consecutive_fail"] >= args.max_consecutive_fail:
                    mutation_state["blocked"] = True
                    print(
                        f"  !! {args.max_consecutive_fail} добавлений подряд не прошли — похоже на настоящий "
                        "лимит записи на сессию. Останавливаю попытки добавления для всех оставшихся модулей."
                    )
            time.sleep(args.sleep)

        if skipped:
            print(f"  Пропущено (VK не даёт добавить именно эти треки): {len(skipped)}")

        for aid in to_remove:
            if mutation_state["blocked"]:
                break
            aid_owner, aid_id = aid.split("_", 1)
            try:
                resp = vk.audio.removeFromPlaylist(owner_id=owner_id, playlist_id=playlist_id, audio_ids=aid_id)
                if resp:
                    mutation_state["consecutive_fail"] = 0
                else:
                    mutation_state["consecutive_fail"] += 1
                    print(f"    - не удалось убрать {aid} [{mutation_state['consecutive_fail']}/{args.max_consecutive_fail}]")
                    if mutation_state["consecutive_fail"] >= args.max_consecutive_fail:
                        mutation_state["blocked"] = True
                        print(f"  !! {args.max_consecutive_fail} неудач подряд на удалении — останавливаюсь.")
            except vk_api.exceptions.VkApiError as e:
                print(f"    ! не удалось убрать {aid}: {e}")
            time.sleep(args.sleep)

        # Проверяем, что реально долетело до VK.
        actual_ids = get_playlist_track_ids(vk, owner_id, playlist_id)
        still_missing = [aid for aid in desired_ids if aid not in actual_ids]

        url = f"https://vk.com/audios{owner_id}?section=all&z=audio_playlist{owner_id}_{playlist_id}"
        report[playlist_title]["url"] = url
        report[playlist_title]["added"] = len(to_add)
        report[playlist_title]["removed"] = len(to_remove)
        report[playlist_title]["actual_count"] = len(actual_ids)
        report[playlist_title]["still_missing"] = len(still_missing)
        print(f"  -> плейлист синхронизирован: {url} (фактически в плейлисте: {len(actual_ids)}/{len(desired_ids)})")

    print("\n\n=== ИТОГ ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    with open("import_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if reject_cache:
        print(
            f"\nВ кэше отказов теперь {len(reject_cache)} трек(ов) ({args.cache_file}) — при следующих "
            "запусках они не будут пытаться добавиться заново без --retry-known-failures."
        )

    if mutation_state["blocked"]:
        print(
            f"\n!!! Поймали {args.max_consecutive_fail} неудачных мутаций подряд — это остановило "
            "дальнейшие попытки в этом запуске (не долблю API впустую). Похоже на настоящий лимит "
            "записи на сессию, а не на единичные отказы по отдельным трекам. Подожди и запусти "
            "скрипт заново со свежим токеном — actual_count/still_missing в отчёте покажет, что "
            "реально долетело."
        )
    else:
        still_missing_total = sum(v.get("still_missing", 0) for v in report.values())
        if still_missing_total:
            print(
                f"\nВсе модули пройдены без серии неудач подряд, но {still_missing_total} треков(а) "
                "VK стабильно отказывается добавлять по отдельности (не лимит сессии — похоже, "
                "конкретные записи недоступны для копирования этим приложением). Список — в "
                "not_found/still_missing по каждому модулю в отчёте выше."
            )


if __name__ == "__main__":
    main()
