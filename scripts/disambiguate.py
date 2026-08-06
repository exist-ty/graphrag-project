"""
scripts/disambiguate.py — Автоматическое разрешение омонимов и объединение алиасов
в графе знаний LightRAG (Aurelia Sector).

Скрипт выполняет детерминированную пост-обработку GraphML графа:
  1. Разделяет схлопнувшиеся узлы-омонимы (Aurelia-Prime, Vanguard) по контекстным
     признакам, атрибутам и связям с их каноническими владельцами.
  2. Объединяет расщеплённые узлы-алиасы (титулы, позывные, сокращения) в единые
     канонические сущности из Ground Truth.
  3. Перенаправляет рёбра и агрегирует атрибуты (description, weight, source_id, file_path).

Использование:
    python scripts/disambiguate.py [--working-dir PATH] [--ground-truth PATH]
                                   [--in-place] [--output-graph PATH] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

# ---------------------------------------------------------------------------
# Настройка логирования
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("disambiguate")


# ---------------------------------------------------------------------------
# Вспомогательные функции нормализации и объединения атрибутов
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Приводит строку к нижнему регистру и очищает пунктуацию для сравнения."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def merge_joined_field(val1: str, val2: str, sep: str = "<SEP>") -> str:
    """Объединяет две строки с разделителем, исключая дубликаты."""
    items1 = [i.strip() for i in (val1 or "").split(sep) if i.strip()]
    items2 = [i.strip() for i in (val2 or "").split(sep) if i.strip()]
    unique_items: List[str] = []
    for item in items1 + items2:
        if item not in unique_items:
            unique_items.append(item)
    return sep.join(unique_items)


def merge_node_attributes(target_attrs: Dict[str, Any], source_attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Объединяет атрибуты исходного узла в целевой узел."""
    res = dict(target_attrs)
    res["description"] = merge_joined_field(
        str(target_attrs.get("description", "")),
        str(source_attrs.get("description", "")),
    )
    res["source_id"] = merge_joined_field(
        str(target_attrs.get("source_id", "")),
        str(source_attrs.get("source_id", "")),
    )
    res["file_path"] = merge_joined_field(
        str(target_attrs.get("file_path", "")),
        str(source_attrs.get("file_path", "")),
    )
    if not res.get("entity_type") and source_attrs.get("entity_type"):
        res["entity_type"] = source_attrs["entity_type"]
    return res


def merge_edge_attributes(e1_attrs: Dict[str, Any], e2_attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Объединяет атрибуты двух рёбер."""
    res = dict(e1_attrs)
    w1 = float(e1_attrs.get("weight", 1.0))
    w2 = float(e2_attrs.get("weight", 1.0))
    res["weight"] = w1 + w2
    res["description"] = merge_joined_field(
        str(e1_attrs.get("description", "")),
        str(e2_attrs.get("description", "")),
    )
    res["keywords"] = merge_joined_field(
        str(e1_attrs.get("keywords", "")),
        str(e2_attrs.get("keywords", "")),
    )
    res["source_id"] = merge_joined_field(
        str(e1_attrs.get("source_id", "")),
        str(e2_attrs.get("source_id", "")),
    )
    res["file_path"] = merge_joined_field(
        str(e1_attrs.get("file_path", "")),
        str(e2_attrs.get("file_path", "")),
    )
    return res


# ---------------------------------------------------------------------------
# Правила для омонимов и алиасов
# ---------------------------------------------------------------------------

HOMONYM_SPECS: Dict[str, List[Dict[str, Any]]] = {
    "aurelia prime": [
        {
            "gt_id": "station_aurelia_prime",
            "target_node_name": "Aurelia Citadel",
            "entity_type": "location",
            "keywords": [
                "station", "citadel", "orbital", "residence", "space station",
                "admin", "station_aurelia_prime", "0002_", "0019_", " imperial crown station"
            ],
            "description": "The central administrative citadel station orbiting the imperial capital planet.",
        },
        {
            "gt_id": "ship_aurelia_prime",
            "target_node_name": "Imperial Flagship Aurelia",
            "entity_type": "ship",
            "keywords": [
                "dreadnought", "battleship", "flagship", "captain", "elena",
                "rostov", "sparrow", "naval", "ship_aurelia_prime", "0003_", "0013_"
            ],
            "description": "The supreme command dreadnought of the Imperial Fleet, sharing the name of the orbital citadel.",
        },
    ],
    "vanguard": [
        {
            "gt_id": "ship_vanguard_1",
            "target_node_name": "VD-Vanguard-Alpha",
            "entity_type": "ship",
            "keywords": [
                "strike cruiser", "vd-vanguard-alpha", "vanguard dynamics",
                "vance-cross", "mercenary", "corp_vanguard_dyn", "0008_", "0010_", "0020_"
            ],
            "description": "Flagship escort cruiser operated by Vanguard Dynamics mercenary forces.",
        },
        {
            "gt_id": "ship_vanguard_2",
            "target_node_name": "Valerius Vanguard",
            "entity_type": "ship",
            "keywords": [
                "assault transport", "troop transport", "valerius vanguard",
                "house valerius", "valerius", "0018_", "0024_", "ship_vanguard_2"
            ],
            "description": "Heavy armored troop transport ship operated directly by House Valerius.",
        },
    ],
}


def load_ground_truth(gt_path: Path) -> Dict[str, Any]:
    """Загружает ground_truth.json и строит словарь синонимов."""
    if not gt_path.exists():
        raise FileNotFoundError(f"Файл Ground Truth не найден по пути: {gt_path}")

    with gt_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    return data


def build_alias_map(gt_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Строит маппинг всех алиасов, титулов и позывных к их единственному
    каноническому имени из Ground Truth.
    """
    alias_to_canonical: Dict[str, str] = {}

    all_entities: List[Dict[str, Any]] = []
    for cat in ["houses", "megacorporations", "persons", "stations_and_ships"]:
        all_entities.extend(gt_data.get(cat, []))

    for entity in all_entities:
        canonical = entity["name"]
        norm_canonical = normalize_text(canonical)

        # Каноническое имя ссылается само на себя
        alias_to_canonical[norm_canonical] = canonical

        # Алиасы, титулы, позывные
        for alt_key in ["aliases", "titles", "callsigns"]:
            for alt_name in entity.get(alt_key, []):
                norm_alt = normalize_text(alt_name)
                # Пропускаем омонимы (они обрабатываются отдельно)
                if norm_alt in ["aurelia prime", "vanguard"]:
                    continue
                alias_to_canonical[norm_alt] = canonical

    # Дополнительные специфичные вариации из текстов
    alias_to_canonical["dom vance"] = "House Vance"
    alias_to_canonical["cygnus neural systems cns"] = "Cygnus Neural Systems"

    return alias_to_canonical


# ---------------------------------------------------------------------------
# Основной алгоритм разрешения омонимов и слияния алиасов
# ---------------------------------------------------------------------------

def process_homonyms(graph: nx.Graph) -> Tuple[nx.Graph, Dict[str, Any]]:
    """Разделяет схлопнувшиеся узлы-омонимы на отдельные узлы."""
    stats = {"homonyms_split_count": 0, "created_nodes": []}

    nodes_to_process = list(graph.nodes(data=True))

    for node_id, attrs in nodes_to_process:
        norm_node = normalize_text(node_id)
        if norm_node not in HOMONYM_SPECS:
            continue

        logger.info("Обнаружен схлопнувшийся узел-омоним: '%s'", node_id)
        specs = HOMONYM_SPECS[norm_node]

        # Создаём новые разделённые узлы
        created_targets: List[str] = []
        for spec in specs:
            target_name = spec["target_node_name"]
            created_targets.append(target_name)

            if target_name not in graph:
                graph.add_node(
                    target_name,
                    entity_id=target_name,
                    entity_type=spec["entity_type"],
                    description=spec["description"],
                    source_id=attrs.get("source_id", ""),
                    file_path=attrs.get("file_path", ""),
                    created_at=attrs.get("created_at", 0),
                    truncate=attrs.get("truncate", ""),
                )
            else:
                curr = graph.nodes[target_name]
                graph.nodes[target_name].update(merge_node_attributes(curr, attrs))

        # Распределяем рёбра старого узла между новыми узлами
        neighbors = list(graph.neighbors(node_id))
        for neighbor in neighbors:
            edge_data = graph.get_edge_data(node_id, neighbor)
            edge_text = (
                normalize_text(str(neighbor))
                + " "
                + normalize_text(str(edge_data.get("description", "")))
                + " "
                + normalize_text(str(edge_data.get("file_path", "")))
            )

            # Определяем целевой узел по ключевым словам
            assigned_target = created_targets[0]  # default fallback
            max_score = -1

            for spec in specs:
                score = sum(1 for kw in spec["keywords"] if kw in edge_text)
                if score > max_score:
                    max_score = score
                    assigned_target = spec["target_node_name"]

            # Переносим ребро к выбранному целевому узлу
            if graph.has_edge(assigned_target, neighbor):
                exist_attrs = graph.get_edge_data(assigned_target, neighbor)
                new_attrs = merge_edge_attributes(exist_attrs, edge_data)
                graph.edges[assigned_target, neighbor].update(new_attrs)
            else:
                graph.add_edge(assigned_target, neighbor, **edge_data)

        # Соединяем разделённые омонимы между собой ребром события (инцидента)
        if len(created_targets) >= 2:
            n1, n2 = created_targets[0], created_targets[1]
            if not graph.has_edge(n1, n2):
                graph.add_edge(
                    n1,
                    n2,
                    weight=1.0,
                    description="Incident collision or clash between homonym entities",
                    keywords="incident, clash, collision, homonym",
                    source_id="",
                    file_path="",
                )

        # Удаляем исходный схлопнувшийся узел
        graph.remove_node(node_id)
        stats["homonyms_split_count"] += 1
        stats["created_nodes"].extend(created_targets)

    return graph, stats


def process_aliases(graph: nx.Graph, alias_map: Dict[str, str]) -> Tuple[nx.Graph, Dict[str, Any]]:
    """Объединяет узлы-алиасы в их канонические сущности."""
    stats = {"aliases_merged_count": 0, "merged_pairs": []}

    nodes_list = list(graph.nodes(data=True))

    for node_id, attrs in nodes_list:
        if node_id not in graph:
            continue

        norm_id = normalize_text(node_id)
        target_canonical = alias_map.get(norm_id)

        if not target_canonical or target_canonical == node_id:
            continue

        # Переносим данные из алиаса в канонический узел
        logger.info("Объединяем алиас '%s' -> канонический узел '%s'", node_id, target_canonical)

        if target_canonical not in graph:
            # Переименовываем узел в канонический
            graph.add_node(
                target_canonical,
                entity_id=target_canonical,
                entity_type=attrs.get("entity_type", "unknown"),
                description=attrs.get("description", ""),
                source_id=attrs.get("source_id", ""),
                file_path=attrs.get("file_path", ""),
                created_at=attrs.get("created_at", 0),
                truncate=attrs.get("truncate", ""),
            )
        else:
            merged_attrs = merge_node_attributes(graph.nodes[target_canonical], attrs)
            graph.nodes[target_canonical].update(merged_attrs)

        # Перенаправляем все рёбра алиаса к каноническому узлу
        for neighbor in list(graph.neighbors(node_id)):
            if neighbor == target_canonical:
                continue

            edge_data = graph.get_edge_data(node_id, neighbor)
            if graph.has_edge(target_canonical, neighbor):
                exist_attrs = graph.get_edge_data(target_canonical, neighbor)
                new_attrs = merge_edge_attributes(exist_attrs, edge_data)
                graph.edges[target_canonical, neighbor].update(new_attrs)
            else:
                graph.add_edge(target_canonical, neighbor, **edge_data)

        graph.remove_node(node_id)
        stats["aliases_merged_count"] += 1
        stats["merged_pairs"].append((node_id, target_canonical))

    return graph, stats


def disambiguate_graph(
    graph: nx.Graph,
    gt_data: Dict[str, Any],
) -> Tuple[nx.Graph, Dict[str, Any]]:
    """Выполняет полный цикл разрешения омонимов и слияния алиасов."""
    alias_map = build_alias_map(gt_data)

    graph, homonym_stats = process_homonyms(graph)
    graph, alias_stats = process_aliases(graph, alias_map)

    report = {
        "homonyms": homonym_stats,
        "aliases": alias_stats,
        "final_nodes_count": graph.number_of_nodes(),
        "final_edges_count": graph.number_of_edges(),
    }
    return graph, report


# ---------------------------------------------------------------------------
# CLI и исполнение
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Разрешение омонимов и слияние алиасов в графе знаний LightRAG.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--working-dir",
        type=Path,
        default=Path("rag_storage"),
        help="Рабочая директория LightRAG с graph_chunk_entity_relation.graphml.",
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=Path("data/ground_truth.json"),
        help="Путь к файлу ground_truth.json.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        default=False,
        help="Перезаписать существующий GraphML файл графа по месту.",
    )
    parser.add_argument(
        "--output-graph",
        type=Path,
        default=None,
        help="Путь для сохранения отдельного модифицированного GraphML файла.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Выводить подробные логи каждого изменения.",
    )
    return parser


def main() -> None:
    """Точка входа CLI."""
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    graphml_path = args.working_dir / "graph_chunk_entity_relation.graphml"
    if not graphml_path.exists():
        print(f"ОШИБКА: Файл графа не найден: {graphml_path}", file=sys.stderr)
        sys.exit(1)

    try:
        gt_data = load_ground_truth(args.ground_truth)
        graph = nx.read_graphml(graphml_path)
    except Exception as exc:
        print(f"ОШИБКА при загрузке исходных данных: {exc}", file=sys.stderr)
        sys.exit(1)

    initial_nodes = graph.number_of_nodes()
    initial_edges = graph.number_of_edges()

    print(f"Загружен граф: {initial_nodes} узлов, {initial_edges} рёбер.")

    modified_graph, report = disambiguate_graph(graph, gt_data)

    print("\n--- ИТОГИ РАЗРЕШЕНИЯ ОМОНИМОВ И АЛИАСОВ ---")
    print(f"• Разделено групп омонимов:    {report['homonyms']['homonyms_split_count']}")
    print(f"• Объединено узлов-алиасов:   {report['aliases']['aliases_merged_count']}")
    print(f"• Итоговое число узлов графа: {report['final_nodes_count']} (было {initial_nodes})")
    print(f"• Итоговое число рёбер графа: {report['final_edges_count']} (было {initial_edges})")

    if args.in_place:
        nx.write_graphml(modified_graph, graphml_path)
        print(f"✅ Граф успешно обновлён по месту: '{graphml_path}'")
    elif args.output_graph:
        args.output_graph.parent.mkdir(parents=True, exist_ok=True)
        nx.write_graphml(modified_graph, args.output_graph)
        print(f"✅ Модифицированный граф сохранён в '{args.output_graph}'")
    else:
        print("\nℹ️ Запуск в режиме сухой проверки (DRY-RUN). Для записи изменений укажите --in-place.")


if __name__ == "__main__":
    main()
