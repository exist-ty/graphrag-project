"""
Генератор синтетического корпуса документов для GraphRAG в два прохода.

Проход 1: Генерация единого источника истины (ground_truth.json) с омонимами,
алиасами, иерархией подчинения и таймлайном событий в секторе Aurelia Sector.
Проход 2: Генерация противоречивых .md документов от 4 типов предвзятых
нарраторов (encyclopedia, dossier, propaganda, log) с YAML-frontmatter.
"""

import argparse
import asyncio
import datetime
import json
import os
import pathlib
import random
import re
import sys
from typing import Any, Dict, List

import dotenv
from google import genai
from google.genai import errors, types

# Загрузка переменных окружения из .env
dotenv.load_dotenv()

# Переведено с `gemini-3.6-flash`: у free tier лимит 20 запросов В СУТКИ на модель, и прогон
# на 24 документа исчерпал его после 7 успешных. Квота считается на модель — у lite свой бюджет.
MODEL_NAME = "gemini-3.5-flash-lite"

# Иерархическая схема Ground Truth для структурированного вывода Gemini
GROUND_TRUTH_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "sector_name": {"type": "STRING"},
        "houses": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "aliases": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "description": {"type": "STRING"},
                    "motto": {"type": "STRING"},
                },
                "required": ["id", "name", "aliases", "description"],
            },
        },
        "megacorporations": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "aliases": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "description": {"type": "STRING"},
                    "specialization": {"type": "STRING"},
                },
                "required": ["id", "name", "aliases", "description"],
            },
        },
        "persons": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "aliases": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "titles": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "callsigns": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "affiliation_id": {"type": "STRING"},
                    "role": {"type": "STRING"},
                },
                "required": ["id", "name", "aliases", "affiliation_id", "role"],
            },
        },
        "stations_and_ships": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "aliases": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "type": {"type": "STRING"},
                    "is_homonym_risk": {"type": "BOOLEAN"},
                    "owner_id": {"type": "STRING"},
                    "description": {"type": "STRING"},
                },
                "required": ["id", "name", "type", "owner_id", "description"],
            },
        },
        "hierarchy": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "parent_id": {"type": "STRING"},
                    "child_id": {"type": "STRING"},
                    "relation_type": {"type": "STRING"},
                    "level": {"type": "INTEGER"},
                },
                "required": ["parent_id", "child_id", "relation_type", "level"],
            },
        },
        "timeline": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "event_id": {"type": "STRING"},
                    "year": {"type": "INTEGER"},
                    "title": {"type": "STRING"},
                    "description": {"type": "STRING"},
                    "participant_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["event_id", "year", "title", "description", "participant_ids"],
            },
        },
    },
    "required": [
        "sector_name",
        "houses",
        "megacorporations",
        "persons",
        "stations_and_ships",
        "hierarchy",
        "timeline",
    ],
}

NARRATOR_PROPORTIONS = {
    "encyclopedia": 0.30,
    "dossier": 0.25,
    "propaganda": 0.25,
    "log": 0.20,
}

NARRATOR_PROMPTS = {
    "encyclopedia": (
        "Вы — составитель Официальной Имперской Энциклопедии Сектора Аурелия. Напишите энциклопедическую статью. "
        "Стиль: формальный, академический, от третьего лица, демонстративно нейтральный, но отражающий официальную имперскую позицию. "
        "Подробно опишите указанные сущности, их официальную историю, роли и связанные события."
    ),
    "dossier": (
        "Вы — аналитик службы имперской разведки. Напишите секретное шпионское досье или аналитический отчет. "
        "Стиль: циничный, лаконичный, с акцентом на скрытые мотивы, финансовые махинации, теневые альянсы, уязвимости и тайные прегрешения. "
        "Подвергайте сомнению официальные версии событий."
    ),
    "propaganda": (
        "Вы — агитатор и пропагандист одной из конфликтующих сторон сектора Аурелия. Напишите эмоциональный пропагандистский манифест или сводку новостей. "
        "Стиль: экспрессивный, резкий, яростный, делящий участников на героев и предателей. "
        "Искажайте факты в пользу вашей фракции, обеляя свои действия и обвиняя оппонентов во всех грехах."
    ),
    "log": (
        "Вы — офицер, капитана корабля или комендант станции. Напишите серию выписок из бортового/станционного журнала или официальный указ. "
        "Стиль: протокольный, технический, с временными метками/бортовыми датами, фактологический, но ограниченный личным кругозором автора. "
        "Опишите происходящее через призму непосредственного участника событий."
    ),
}


async def call_gemini_async_with_backoff(
    client: genai.Client,
    model: str,
    prompt: str,
    config: types.GenerateContentConfig | None = None,
    max_retries: int = 5,
    initial_delay: float = 2.0,
) -> str:
    """Вызов Gemini API с собственным экспоненциальным backoff для 429 и 5xx ошибок."""
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            kwargs: Dict[str, Any] = {"model": model, "contents": [prompt]}
            if config is not None:
                kwargs["config"] = config
            response = await client.aio.models.generate_content(**kwargs)
            return response.text or ""
        except errors.ClientError as exc:
            # 429 Rate Limit ретраим, остальные 4xx фатальные
            if exc.code == 429 and attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2.0
            else:
                raise
        except errors.ServerError:
            # 5xx ошибки сервера ретраим
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2.0
            else:
                raise
        except errors.APIError:
            raise
    return ""


async def generate_ground_truth(client: genai.Client, target_file: pathlib.Path) -> Dict[str, Any]:
    """Проход 1: Генерация единого источника истины (ground_truth.json)."""
    print("[Pass 1] Generating ground truth seed data...")

    prompt = (
        "Сгенерируйте подробный и логически связанный источник истины (Ground Truth) для сеттинга Aurelia Sector — "
        "кибер-феодальной межзвёздной империи (феодальные дома, мегакорпорации, колонии, флоты, станции).\n"
        "Требования к данным:\n"
        "1. Дома (houses): минимум 3 великих дома с ID, именами, алиасами, описанием и девизом.\n"
        "2. Мегакорпорации (megacorporations): минимум 3 корпорации с ID, именами, алиасами, специализацией.\n"
        "3. Персоны (persons): минимум 8 персонажей с ID, каноническим именем, алиасами/титулами/позывными, фракцией и ролью.\n"
        "4. Станции и корабли (stations_and_ships): минимум 6 объектов. ОБЯЗАТЕЛЬНО создайте омонимы — объекты разных типов или разных владельцев с одинаковым или практически одинаковым названием (например, станция 'Aurelia-Prime' и крейсер 'Aurelia-Prime', или два корабля с одинаковым позывным 'Vanguard'). Пометьте их is_homonym_risk=true.\n"
        "5. Иерархия (hierarchy): глубина минимум 3 уровня владения/подчинения (Империя -> Дом -> Корпорация/Вассал -> Станция/Корабль).\n"
        "6. Таймлайн (timeline): минимум 8 ключевых хронологических событий с event_id, годом, названием, описанием и участниками (participant_ids).\n"
        "Все ID должны быть уникальными строками формата 'house_...', 'corp_...', 'person_...', 'station_...', 'ship_...', 'evt_...'."
    )

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=GROUND_TRUTH_SCHEMA,
    )

    raw_json_str = await call_gemini_async_with_backoff(client, MODEL_NAME, prompt, config=config)
    ground_truth_data = json.loads(raw_json_str)

    target_file.parent.mkdir(parents=True, exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(ground_truth_data, f, ensure_ascii=False, indent=2)

    print(f"[Pass 1] Ground truth saved to {target_file}")
    return ground_truth_data


def make_safe_slug(text: str, max_length: int = 30) -> str:
    """Создаёт безопасный slug для имени файла из текста."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        slug = "entity"
    return slug[:max_length].strip("-")


def build_document_plan(
    ground_truth: Dict[str, Any], count: int, seed: int = 42
) -> List[Dict[str, Any]]:
    """Строит детерминированный план генерации документов до обращения к LLM."""
    rng = random.Random(seed)

    # Распределение типов нарраторов
    counts_by_narrator = {}
    remaining = count
    narrator_keys = list(NARRATOR_PROPORTIONS.keys())
    for i, key in enumerate(narrator_keys):
        if i == len(narrator_keys) - 1:
            counts_by_narrator[key] = remaining
        else:
            c = int(round(count * NARRATOR_PROPORTIONS[key]))
            counts_by_narrator[key] = c
            remaining -= c

    narrator_pool = []
    for key, c in counts_by_narrator.items():
        narrator_pool.extend([key] * c)
    rng.shuffle(narrator_pool)

    # Собираем все сущности и события
    all_entities = []
    for cat in ["houses", "megacorporations", "persons", "stations_and_ships"]:
        all_entities.extend(ground_truth.get(cat, []))

    timeline = ground_truth.get("timeline", [])
    hierarchy = ground_truth.get("hierarchy", [])

    plan = []
    for idx in range(1, count + 1):
        doc_num_str = f"{idx:04d}"
        narrator = narrator_pool[idx - 1]

        # Выбираем ключевую сущность и 1-3 связанных события
        focal_entity = rng.choice(all_entities) if all_entities else {"id": "ent_unknown", "name": "unknown"}
        focal_id = focal_entity["id"]

        # Находим события с участием фокальной сущности
        related_events = [ev for ev in timeline if focal_id in ev.get("participant_ids", [])]
        if not related_events and timeline:
            related_events = rng.sample(timeline, k=min(2, len(timeline)))
        elif len(related_events) > 3:
            related_events = rng.sample(related_events, k=3)

        source_event_ids = [ev["event_id"] for ev in related_events]

        # Собираем другие связанные сущности из выбранных событий
        subject_entity_ids = {focal_id}
        for ev in related_events:
            subject_entity_ids.update(ev.get("participant_ids", []))

        # Выбираем slug для имени файла
        focal_name = focal_entity.get("name") or focal_id
        slug = make_safe_slug(focal_name)
        filename = f"{doc_num_str}_{narrator}_{slug}.md"
        doc_id = f"{doc_num_str}_{narrator}_{slug}"

        # Формируем срез Ground Truth для промпта
        slice_entities = [e for e in all_entities if e["id"] in subject_entity_ids]
        slice_events = [ev for ev in timeline if ev["event_id"] in source_event_ids]
        slice_hierarchy = [
            h
            for h in hierarchy
            if h["parent_id"] in subject_entity_ids or h["child_id"] in subject_entity_ids
        ]

        gt_slice = {
            "sector_name": ground_truth.get("sector_name", "Aurelia Sector"),
            "entities": slice_entities,
            "events": slice_events,
            "hierarchy_links": slice_hierarchy,
        }

        plan.append(
            {
                "doc_num": idx,
                "doc_id": doc_id,
                "filename": filename,
                "narrator": narrator,
                "subject_entity_ids": sorted(list(subject_entity_ids)),
                "source_event_ids": source_event_ids,
                "gt_slice": gt_slice,
            }
        )

    return plan


def clean_llm_markdown(text: str) -> str:
    """Удаляет случайно добавленные LLM блоки ```markdown и лишний YAML frontmatter."""
    text = text.strip()

    # Срезаем внешнее ограждение ```markdown ... ```
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Срезаем ведущий YAML frontmatter, если модель всё же его вставила
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2].strip()

    return text


def build_frontmatter(
    doc_id: str,
    narrator: str,
    subject_entity_ids: List[str],
    source_event_ids: List[str],
    generated_at: str,
) -> str:
    """Сборка программного YAML-frontmatter."""
    lines = ["---", f"doc_id: {doc_id}", f"narrator: {narrator}"]
    lines.append("subject_entity_ids:")
    for eid in subject_entity_ids:
        lines.append(f"  - {eid}")
    lines.append("source_event_ids:")
    for ev_id in source_event_ids:
        lines.append(f"  - {ev_id}")
    lines.append(f"generated_at: '{generated_at}'")
    lines.append("---")
    return "\n".join(lines)


async def generate_single_document(
    client: genai.Client,
    item: Dict[str, Any],
    out_dir: pathlib.Path,
    semaphore: asyncio.Semaphore,
) -> str:
    """Генерация одного документа с учётом ограничений нарратива и формата."""
    async with semaphore:
        narrator = item["narrator"]
        narrator_instruction = NARRATOR_PROMPTS[narrator]
        gt_slice_str = json.dumps(item["gt_slice"], ensure_ascii=False, indent=2)

        prompt = (
            f"{narrator_instruction}\n\n"
            f"Вам предоставлен фрагмент канонической информации (Ground Truth):\n"
            f"```json\n{gt_slice_str}\n```\n\n"
            f"Инструкции:\n"
            f"1. Напишите содержательный документ в формате Markdown на русском языке по мотивам предоставленных сущностей и событий.\n"
            f"2. Отразите специфический стиль и возможные искажения фактов, присущие нарратору '{narrator}'.\n"
            f"3. Упоминайте алиасы, титулы и позывные персонажей и объектов из канона.\n"
            f"4. КРИТИЧЕСКИ ВАЖНО: Не добавляйте никакой YAML-frontmatter или заголовок '---'. Возвращайте ТОЛЬКО тело документа Markdown!"
        )

        body_markdown = await call_gemini_async_with_backoff(client, MODEL_NAME, prompt)
        clean_body = clean_llm_markdown(body_markdown)

        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        frontmatter = build_frontmatter(
            doc_id=item["doc_id"],
            narrator=narrator,
            subject_entity_ids=item["subject_entity_ids"],
            source_event_ids=item["source_event_ids"],
            generated_at=now_utc,
        )

        full_content = f"{frontmatter}\n\n{clean_body}\n"
        filepath = out_dir / item["filename"]
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(full_content)

        return item["filename"]


async def run_pass2(
    client: genai.Client,
    plan: List[Dict[str, Any]],
    out_dir: pathlib.Path,
    concurrency: int,
    overwrite: bool,
) -> tuple[int, int, int]:
    """Проход 2: Параллельная генерация файлов корпусов по плану."""
    out_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)

    created_count = 0
    skipped_count = 0
    failed_count = 0

    tasks_to_run = []
    items_to_run = []

    for item in plan:
        target_filepath = out_dir / item["filename"]
        if target_filepath.exists() and not overwrite:
            skipped_count += 1
        else:
            items_to_run.append(item)
            tasks_to_run.append(generate_single_document(client, item, out_dir, semaphore))

    if tasks_to_run:
        results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                failed_count += 1
                print(f"[Error] Failed to generate document: {res}", file=sys.stderr)
            else:
                created_count += 1

    return created_count, skipped_count, failed_count


def calculate_actual_narrator_distribution(out_dir: pathlib.Path) -> Dict[str, int]:
    """Считывает фактически созданные .md файлы и рассчитывает распределение нарраторов."""
    counts: Dict[str, int] = {k: 0 for k in NARRATOR_PROPORTIONS.keys()}
    if not out_dir.exists():
        return counts

    for filepath in out_dir.glob("*.md"):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read(1024)
                match = re.search(r"^narrator:\s*([a-z_]+)", content, re.MULTILINE)
                if match:
                    narrator = match.group(1)
                    if narrator in counts:
                        counts[narrator] += 1
        except Exception:
            continue

    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic data generator for GraphRAG")
    parser.add_argument("--count", type=int, default=24, help="Number of documents to generate")
    parser.add_argument("--concurrency", type=int, default=4, help="Async concurrency limit")
    parser.add_argument(
        "--out-dir",
        type=str,
        default="data/generated",
        help="Directory to save generated md files",
    )
    parser.add_argument(
        "--ground-truth",
        type=str,
        default="data/ground_truth.json",
        help="Path to ground truth JSON file",
    )
    parser.add_argument("--seed-only", action="store_true", help="Only run Pass 1 ground truth seed generation")
    parser.add_argument("--regenerate-seed", action="store_true", help="Regenerate ground truth JSON file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing document files")

    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Fatal] GEMINI_API_KEY environment variable is missing", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    gt_path = pathlib.Path(args.ground_truth)
    out_dir = pathlib.Path(args.out_dir)

    # Проход 1: Ground Truth
    if gt_path.exists() and not args.regenerate_seed:
        print(f"[Pass 1] Reusing existing ground truth from {gt_path}")
        with open(gt_path, "r", encoding="utf-8") as f:
            ground_truth = json.load(f)
    else:
        ground_truth = await generate_ground_truth(client, gt_path)

    if args.seed_only:
        print("[Info] Seed-only mode completed.")
        return

    # Проход 2: Документы
    print(f"[Pass 2] Building plan for {args.count} documents...")
    plan = build_document_plan(ground_truth, count=args.count)

    created, skipped, failed = await run_pass2(
        client=client,
        plan=plan,
        out_dir=out_dir,
        concurrency=args.concurrency,
        overwrite=args.overwrite,
    )

    actual_dist = calculate_actual_narrator_distribution(out_dir)
    total_existing_docs = sum(actual_dist.values())

    print("\n" + "=" * 50)
    print("Execution Summary:")
    print(f"Created:  {created}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {failed}")
    print(f"Output Directory: {out_dir.resolve()}")
    print("\nActual Narrator Distribution on Disk:")
    for narrator, count in actual_dist.items():
        pct = (count / total_existing_docs * 100) if total_existing_docs > 0 else 0.0
        print(f"  - {narrator:12s}: {count:4d} ({pct:5.1f}%)")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
