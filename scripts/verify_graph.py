"""
scripts/verify_graph.py — Автоматическая верификация построенного графа знаний LightRAG
против ground truth корпуса Aurelia Sector.

Скрипт полностью детерминирован, не использует LLM и сетевые запросы.
Проверяет:
  1. Покрытие сущностей (с нечётким сопоставлением алиасов и имён).
  2. Слипание омонимов (разделены ли омонимичные сущности в графе).
  3. Расщепление алиасов (не разъехалась ли одна сущность на несколько узлов).
  4. Покрытие иерархии (достижимость и прямые рёбра между родителями и детьми).
  5. Multi-hop связность и изолированные узлы.
  6. Покрытие событий таймлайна.
  7. Вклад нарраторов в граф (по типам источников).

Использование:
    python scripts/verify_graph.py [--working-dir PATH] [--ground-truth PATH]
                                   [--docs-dir PATH] [--json PATH]
                                   [--fuzzy-threshold FLOAT] [--verbose]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

# ---------------------------------------------------------------------------
# Константы по умолчанию
# ---------------------------------------------------------------------------
DEFAULT_WORKING_DIR = Path("rag_storage")
DEFAULT_GROUND_TRUTH = Path("data/ground_truth.json")
DEFAULT_DOCS_DIR = Path("data/generated")
DEFAULT_FUZZY_THRESHOLD = 0.82
GRAPH_FILENAME = "graph_chunk_entity_relation.graphml"


# ---------------------------------------------------------------------------
# Вспомогательные функции нормализации и сравнения строк
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """
    Нормализует строку для устойчивого сравнения:
    приводит к нижнему регистру, удаляет пунктуацию и лишние пробелы.
    """
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def calculate_similarity(s1: str, s2: str) -> float:
    """Вычисляет коэффициент сходства строк по алгоритму SequenceMatcher."""
    n1 = normalize_text(s1)
    n2 = normalize_text(s2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    return difflib.SequenceMatcher(None, n1, n2).ratio()


def is_name_match(
    graph_node_name: str,
    target_name: str,
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> Tuple[bool, float]:
    """
    Проверяет совпадение имени узла графа с целевым именем сущности или алиасом.
    Учитывает как полное нечёткое совпадение, так и подстроковое вхождение.
    """
    n_node = normalize_text(graph_node_name)
    n_target = normalize_text(target_name)

    if not n_node or not n_target:
        return False, 0.0

    if n_node == n_target:
        return True, 1.0

    # Проверка частичных вхождений для длинных составных имён
    if (len(n_target) >= 6 and n_target in n_node) or (len(n_node) >= 6 and n_node in n_target):
        ratio = max(len(n_target), len(n_node))
        min_len = min(len(n_target), len(n_node))
        coverage = min_len / ratio
        if coverage >= 0.7:
            return True, 0.9

    ratio = difflib.SequenceMatcher(None, n_node, n_target).ratio()
    return ratio >= threshold, ratio


# ---------------------------------------------------------------------------
# Загрузка данных
# ---------------------------------------------------------------------------

def load_ground_truth(gt_path: Path) -> Dict[str, Any]:
    """Загружает эталонный реестр сущностей и таймлайн из ground_truth.json."""
    if not gt_path.exists():
        raise FileNotFoundError(f"Файл Ground Truth не найден: {gt_path}")

    with gt_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # Собираем единый реестр сущностей с указанием категории
    all_entities: List[Dict[str, Any]] = []

    for house in data.get("houses", []):
        all_entities.append({**house, "category": "house"})

    for corp in data.get("megacorporations", []):
        all_entities.append({**corp, "category": "megacorporation"})

    for person in data.get("persons", []):
        all_entities.append({**person, "category": "person"})

    for item in data.get("stations_and_ships", []):
        all_entities.append({**item, "category": "station_or_ship"})

    data["all_entities"] = all_entities
    return data


def load_graph(working_dir: Path) -> nx.Graph:
    """
    Загружает неориентированный граф из GraphML хранилища LightRAG.
    Выбрасывает понятное исключение, если файл отсутствует.
    """
    graphml_path = working_dir / GRAPH_FILENAME
    if not graphml_path.exists():
        raise FileNotFoundError(
            f"Файл графа '{graphml_path}' не найден.\n"
            f"Сначала запустите построение графа: python scripts/build_kg.py --working-dir {working_dir}"
        )

    try:
        return nx.read_graphml(graphml_path)
    except Exception as exc:
        raise RuntimeError(f"Ошибка при чтении GraphML файла '{graphml_path}': {exc}") from exc


def parse_frontmatter(docs_dir: Path) -> Dict[str, str]:
    """
    Читает все .md документы из docs_dir и строит маппинг:
    имя_файла.md -> тип нарратора (encyclopedia / dossier / propaganda / log).
    """
    file_to_narrator: Dict[str, str] = {}
    if not docs_dir.exists():
        return file_to_narrator

    for doc_path in docs_dir.glob("*.md"):
        try:
            content = doc_path.read_text(encoding="utf-8")
            narrator = "unknown"
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.strip().startswith("narrator:"):
                            narrator = line.split(":", 1)[1].strip().strip("'\"")
                            break
            file_to_narrator[doc_path.name] = narrator
        except OSError:
            continue

    return file_to_narrator


# ---------------------------------------------------------------------------
# Логика верификации
# ---------------------------------------------------------------------------

def match_entities_to_graph(
    gt_entities: List[Dict[str, Any]],
    graph_nodes: List[str],
    threshold: float,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Для каждой сущности из Ground Truth находит все узлы графа,
    которые соответствуют её имени, алиасам, титулам или позывным.
    """
    entity_to_nodes: Dict[str, List[Dict[str, Any]]] = {}

    for entity in gt_entities:
        e_id = entity["id"]
        names_to_check: List[Tuple[str, str]] = []

        # Каноническое имя
        names_to_check.append((entity["name"], "canonical"))

        # Алиасы
        for alias in entity.get("aliases", []):
            names_to_check.append((alias, "alias"))

        # Титулы
        for title in entity.get("titles", []):
            names_to_check.append((title, "title"))

        # Позывные
        for callsign in entity.get("callsigns", []):
            names_to_check.append((callsign, "callsign"))

        matched_nodes: List[Dict[str, Any]] = []

        for node_id in graph_nodes:
            best_match_type = ""
            best_score = 0.0
            matched_name = ""

            for name_val, match_type in names_to_check:
                matched, score = is_name_match(node_id, name_val, threshold)
                if matched and score > best_score:
                    best_score = score
                    best_match_type = match_type
                    matched_name = name_val

            if best_score > 0.0:
                matched_nodes.append({
                    "node_id": node_id,
                    "matched_by": best_match_type,
                    "matched_name": matched_name,
                    "score": best_score,
                })

        entity_to_nodes[e_id] = matched_nodes

    return entity_to_nodes


def check_entity_coverage(
    gt_entities: List[Dict[str, Any]],
    entity_to_nodes: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """1. Проверка покрытия сущностей Ground Truth в графе."""
    total = len(gt_entities)
    found_entities: List[Dict[str, Any]] = []
    missing_entities: List[Dict[str, Any]] = []

    for entity in gt_entities:
        e_id = entity["id"]
        nodes = entity_to_nodes.get(e_id, [])
        if nodes:
            found_entities.append({
                "entity": entity,
                "matched_nodes": nodes,
            })
        else:
            missing_entities.append(entity)

    coverage_pct = (len(found_entities) / total * 100.0) if total > 0 else 0.0

    return {
        "total_gt_entities": total,
        "found_count": len(found_entities),
        "missing_count": len(missing_entities),
        "coverage_pct": round(coverage_pct, 2),
        "found_entities": found_entities,
        "missing_entities": missing_entities,
    }


def check_homonym_collapsing(
    gt_entities: List[Dict[str, Any]],
    entity_to_nodes: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """
    2. Проверка слипания омонимов.
    Определяет сущности с одинаковыми/похожими именами или флагом is_homonym_risk
    и проверяет, не схлопнулись ли они в один узел графа.
    """
    # Группируем сущности GT по совпадению баз имён/алиасов
    homonym_entities = [e for e in gt_entities if e.get("is_homonym_risk", False)]

    # Дополнительно сгруппируем по имени
    name_groups: Dict[str, List[Dict[str, Any]]] = {}
    for e in gt_entities:
        norm_name = normalize_text(e["name"])
        name_groups.setdefault(norm_name, []).append(e)

    for group in name_groups.values():
        if len(group) > 1:
            for e in group:
                if e not in homonym_entities:
                    homonym_entities.append(e)

    # Анализ каждой группы омонимов
    results: List[Dict[str, Any]] = []
    collapsed_count = 0

    # Группируем омонимичные сущности по нормализованному имени
    grouped_homonyms: Dict[str, List[Dict[str, Any]]] = {}
    for e in homonym_entities:
        norm_name = normalize_text(e["name"])
        grouped_homonyms.setdefault(norm_name, []).append(e)

    for norm_name, entities in grouped_homonyms.items():
        if len(entities) < 2:
            continue

        e_ids = [e["id"] for e in entities]
        node_sets: Dict[str, Set[str]] = {}
        all_matched_nodes: Set[str] = set()

        for e in entities:
            matched = {n["node_id"] for n in entity_to_nodes.get(e["id"], [])}
            node_sets[e["id"]] = matched
            all_matched_nodes.update(matched)

        # Пересечение узлов между омонимичными сущностями
        intersection = set.intersection(*[node_sets[eid] for eid in e_ids]) if e_ids else set()

        is_collapsed = len(intersection) > 0 or (len(all_matched_nodes) == 1 and len(e_ids) > 1)
        if is_collapsed:
            collapsed_count += 1

        results.append({
            "homonym_group": [f"{e['name']} ({e['id']}, {e.get('type', e['category'])})" for e in entities],
            "entity_ids": e_ids,
            "matched_graph_nodes": list(all_matched_nodes),
            "shared_collapsed_nodes": list(intersection),
            "is_collapsed": is_collapsed,
            "status": "COLLAPSED (BAD)" if is_collapsed else "SEPARATED (GOOD)",
        })

    return {
        "total_homonym_groups": len(results),
        "collapsed_groups_count": collapsed_count,
        "separated_groups_count": len(results) - collapsed_count,
        "details": results,
    }


def check_alias_splitting(
    gt_entities: List[Dict[str, Any]],
    entity_to_nodes: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """
    3. Проверка разрезания алиасов (наоборот).
    Ищет ситуации, когда ОДНА сущность Ground Truth расщепилась на НЕСКОЛЬКО
    раздельных узлов в графе.
    """
    splits: List[Dict[str, Any]] = []

    for entity in gt_entities:
        e_id = entity["id"]
        matched_nodes = entity_to_nodes.get(e_id, [])
        unique_nodes = {n["node_id"] for n in matched_nodes}

        if len(unique_nodes) > 1:
            splits.append({
                "entity_id": e_id,
                "canonical_name": entity["name"],
                "split_node_count": len(unique_nodes),
                "matched_nodes_details": matched_nodes,
            })

    return {
        "total_split_entities": len(splits),
        "details": splits,
    }


def check_hierarchy_coverage(
    gt_hierarchy: List[Dict[str, Any]],
    entity_to_nodes: Dict[str, List[Dict[str, Any]]],
    graph: nx.Graph,
) -> Dict[str, Any]:
    """
    4. Проверка покрытия иерархии и связей.
    Проверяет наличие прямого ребра или пути между узлами родителей и детей.
    """
    hierarchy_results: List[Dict[str, Any]] = []
    covered_links = 0
    direct_edge_links = 0
    max_traceable_level = 0
    # Связи, у которых родитель и ребёнок схлопнулись в один узел графа: это провал,
    # а не покрытие, поэтому считаются отдельно (см. ниже в цикле).
    collapsed_links = 0
    collapsed_pairs: List[Tuple[str, str, str]] = []

    for link in gt_hierarchy:
        p_id = link["parent_id"]
        c_id = link["child_id"]
        rel_type = link.get("relation_type", "")
        level = link.get("level", 1)

        p_nodes = {n["node_id"] for n in entity_to_nodes.get(p_id, [])}
        c_nodes = {n["node_id"] for n in entity_to_nodes.get(c_id, [])}

        has_direct = False
        has_path = False
        min_path_len: Optional[int] = None
        connecting_path: List[str] = []

        if p_nodes and c_nodes:
            for p_node in p_nodes:
                for c_node in c_nodes:
                    if p_node not in graph or c_node not in graph:
                        continue
                    if p_node == c_node:
                        # Родитель и ребёнок отобразились в ОДИН узел — это признак слипания,
                        # а не успешно перенесённая связь. Засчитывать такое как покрытие нельзя:
                        # иначе чем сильнее граф схлопывает сущности, тем выше выглядит метрика.
                        collapsed_links += 1
                        collapsed_pairs.append((p_id, c_id, p_node))
                        continue

                    if graph.has_edge(p_node, c_node):
                        has_direct = True

                    if nx.has_path(graph, p_node, c_node):
                        has_path = True
                        try:
                            path = nx.shortest_path(graph, p_node, c_node)
                            length = len(path) - 1
                            if min_path_len is None or length < min_path_len:
                                min_path_len = length
                                connecting_path = path
                        except nx.NetworkXNoPath:
                            pass

        if has_path:
            covered_links += 1
            if level > max_traceable_level:
                max_traceable_level = level
        if has_direct:
            direct_edge_links += 1

        hierarchy_results.append({
            "parent_id": p_id,
            "child_id": c_id,
            "relation_type": rel_type,
            "level": level,
            "parent_nodes": list(p_nodes),
            "child_nodes": list(c_nodes),
            "has_direct_edge": has_direct,
            "has_path": has_path,
            "path_length": min_path_len,
            "shortest_path": connecting_path,
        })

    total_links = len(gt_hierarchy)
    coverage_pct = (covered_links / total_links * 100.0) if total_links > 0 else 0.0

    return {
        "total_hierarchy_links": total_links,
        "covered_links": covered_links,
        "direct_edge_links": direct_edge_links,
        "collapsed_links": collapsed_links,
        "collapsed_pairs": collapsed_pairs,
        "coverage_pct": round(coverage_pct, 2),
        "max_traceable_level": max_traceable_level,
        "details": hierarchy_results,
    }


def check_multihop_and_connectivity(
    gt_hierarchy: List[Dict[str, Any]],
    entity_to_nodes: Dict[str, List[Dict[str, Any]]],
    graph: nx.Graph,
) -> Dict[str, Any]:
    """
    5. Проверка Multi-hop связности и поиск изолированных узлов.
    """
    # Собираем пары сущностей 2-hop из иерархии (Grandparent -> Grandchild)
    parent_map: Dict[str, List[str]] = {}
    for link in gt_hierarchy:
        parent_map.setdefault(link["parent_id"], []).append(link["child_id"])

    multihop_pairs: List[Tuple[str, str, str]] = []  # (src, via, dst)
    for p_id, children in parent_map.items():
        for c_id in children:
            if c_id in parent_map:
                for gc_id in parent_map[c_id]:
                    multihop_pairs.append((p_id, c_id, gc_id))

    multihop_results: List[Dict[str, Any]] = []
    reachable_multihop = 0

    for src_id, via_id, dst_id in multihop_pairs:
        src_nodes = {n["node_id"] for n in entity_to_nodes.get(src_id, [])}
        dst_nodes = {n["node_id"] for n in entity_to_nodes.get(dst_id, [])}

        is_reachable = False
        shortest_len: Optional[int] = None
        sample_path: List[str] = []

        for s_node in src_nodes:
            for d_node in dst_nodes:
                if s_node in graph and d_node in graph and nx.has_path(graph, s_node, d_node):
                    is_reachable = True
                    try:
                        path = nx.shortest_path(graph, s_node, d_node)
                        length = len(path) - 1
                        if shortest_len is None or length < shortest_len:
                            shortest_len = length
                            sample_path = path
                    except nx.NetworkXNoPath:
                        pass

        if is_reachable:
            reachable_multihop += 1

        multihop_results.append({
            "source": src_id,
            "via": via_id,
            "target": dst_id,
            "is_reachable": is_reachable,
            "shortest_path_length": shortest_len,
            "sample_path": sample_path,
        })

    # Изолированные и слабосвязанные узлы графа
    isolated_nodes: List[str] = []
    sparse_nodes: List[Dict[str, Any]] = []  # degree == 1

    for node in graph.nodes():
        deg = graph.degree(node)
        if deg == 0:
            isolated_nodes.append(node)
        elif deg == 1:
            neighbors = list(graph.neighbors(node))
            sparse_nodes.append({"node": node, "connected_to": neighbors[0]})

    return {
        "total_multihop_pairs_checked": len(multihop_pairs),
        "reachable_multihop_pairs": reachable_multihop,
        "multihop_reachability_pct": (
            round(reachable_multihop / len(multihop_pairs) * 100.0, 2) if multihop_pairs else 100.0
        ),
        "isolated_nodes_count": len(isolated_nodes),
        "isolated_nodes": isolated_nodes,
        "sparse_nodes_count": len(sparse_nodes),
        "sparse_nodes": sparse_nodes,
        "details": multihop_results,
    }


def check_timeline_events(
    gt_timeline: List[Dict[str, Any]],
    entity_to_nodes: Dict[str, List[Dict[str, Any]]],
    graph: nx.Graph,
) -> Dict[str, Any]:
    """
    6. Проверка покрытия событий таймлайна в графе.
    """
    event_results: List[Dict[str, Any]] = []
    covered_events = 0

    for event in gt_timeline:
        e_id = event["event_id"]
        title = event.get("title", "")
        year = str(event.get("year", ""))
        participants = event.get("participant_ids", [])

        # Находим узлы графа для всех участников
        participant_nodes: Dict[str, List[str]] = {}
        all_event_nodes: Set[str] = set()

        for p_id in participants:
            p_matched = [n["node_id"] for n in entity_to_nodes.get(p_id, [])]
            participant_nodes[p_id] = p_matched
            all_event_nodes.update(p_matched)

        # Проверяем упоминание событий в описаниях узлов/рёбер или наличие связей
        found_in_graph = len(all_event_nodes) > 0
        connected_participants = False

        if len(all_event_nodes) >= 2:
            node_list = list(all_event_nodes)
            for i in range(len(node_list)):
                for j in range(i + 1, len(node_list)):
                    n1, n2 = node_list[i], node_list[j]
                    if n1 in graph and n2 in graph and nx.has_path(graph, n1, n2):
                        connected_participants = True
                        break
                if connected_participants:
                    break

        if found_in_graph and (connected_participants or len(all_event_nodes) == 1):
            covered_events += 1

        event_results.append({
            "event_id": e_id,
            "year": year,
            "title": title,
            "participants": participants,
            "matched_nodes_count": len(all_event_nodes),
            "matched_nodes": list(all_event_nodes),
            "participants_connected": connected_participants,
            "is_covered": found_in_graph and connected_participants,
        })

    total_events = len(gt_timeline)
    coverage_pct = (covered_events / total_events * 100.0) if total_events > 0 else 0.0

    return {
        "total_timeline_events": total_events,
        "covered_events_count": covered_events,
        "coverage_pct": round(coverage_pct, 2),
        "details": event_results,
    }


def check_narrator_contributions(
    graph: nx.Graph,
    docs_dir: Path,
) -> Dict[str, Any]:
    """
    7. Анализ вклада нарраторов по типам документов (encyclopedia/dossier/propaganda/log).
    """
    file_to_narrator = parse_frontmatter(docs_dir)

    narrator_node_counts: Dict[str, Set[str]] = {
        "encyclopedia": set(),
        "dossier": set(),
        "propaganda": set(),
        "log": set(),
        "unknown": set(),
    }

    narrator_edge_counts: Dict[str, Set[Tuple[str, str]]] = {
        "encyclopedia": set(),
        "dossier": set(),
        "propaganda": set(),
        "log": set(),
        "unknown": set(),
    }

    # Анализируем узлы
    for node, data in graph.nodes(data=True):
        file_paths_raw = data.get("file_path", "")
        if not file_paths_raw:
            continue
        # В LightRAG возможен разделитель <SEP>
        paths = file_paths_raw.split("<SEP>")
        for p in paths:
            fname = Path(p.strip()).name
            narrator = file_to_narrator.get(fname, "unknown")
            narrator_node_counts.setdefault(narrator, set()).add(node)

    # Анализируем рёбра
    for u, v, data in graph.edges(data=True):
        file_paths_raw = data.get("file_path", "")
        if not file_paths_raw:
            continue
        paths = file_paths_raw.split("<SEP>")
        for p in paths:
            fname = Path(p.strip()).name
            narrator = file_to_narrator.get(fname, "unknown")
            edge_key = (min(u, v), max(u, v))
            narrator_edge_counts.setdefault(narrator, set()).add(edge_key)

    stats: Dict[str, Dict[str, int]] = {}
    all_narrators = set(narrator_node_counts.keys()).union(set(narrator_edge_counts.keys()))

    for n_type in sorted(all_narrators):
        node_c = len(narrator_node_counts.get(n_type, set()))
        edge_c = len(narrator_edge_counts.get(n_type, set()))
        if node_c > 0 or edge_c > 0:
            stats[n_type] = {
                "unique_nodes": node_c,
                "unique_edges": edge_c,
            }

    return {
        "narrator_stats": stats,
        "total_graph_nodes": graph.number_of_nodes(),
        "total_graph_edges": graph.number_of_edges(),
    }


# ---------------------------------------------------------------------------
# Форматирование текстового отчёта для stdout
# ---------------------------------------------------------------------------

def print_human_report(report: Dict[str, Any], verbose: bool = False) -> None:
    """Выводит красивый структурированный отчёт на русском языке в консоль."""
    summary = report["summary"]
    ec = report["entity_coverage"]
    hc = report["homonym_collapsing"]
    as_rep = report["alias_splitting"]
    h_rep = report["hierarchy_coverage"]
    mh = report["multihop_connectivity"]
    tl = report["timeline_coverage"]
    narr = report["narrator_contributions"]

    print("=" * 80)
    print("      ОТЧЁТ ВЕРИФИКАЦИИ ГРАФА ЗНАНИЙ LIGHTRAG (AURELIA SECTOR)")
    print("=" * 80)
    print()
    print("--- ИТОГОВАЯ СВОДКА ПОКАЗАТЕЛЕЙ ---")
    print(f"• Покрытие сущностей Ground Truth:   {ec['coverage_pct']}% ({ec['found_count']}/{ec['total_gt_entities']})")
    print(f"• Группы омонимов (Разделены/Всего): {hc['separated_groups_count']}/{hc['total_homonym_groups']} (Слиплись: {hc['collapsed_groups_count']})")
    print(f"• Расщепление алиасов (Сущностей):   {as_rep['total_split_entities']}")
    print(f"• Покрытие иерархических связей:     {h_rep['coverage_pct']}% ({h_rep['covered_links']}/{h_rep['total_hierarchy_links']})")
    print(f"• Максимальная глубина иерархии:     {h_rep['max_traceable_level']} ур.")
    print(f"• Multi-hop достижимость (2-hop):    {mh['multihop_reachability_pct']}% ({mh['reachable_multihop_pairs']}/{mh['total_multihop_pairs_checked']})")
    print(f"• Изолированные / слабосвязанные:   Изолировано: {mh['isolated_nodes_count']}, Тупиковых (deg 1): {mh['sparse_nodes_count']}")
    print(f"• Покрытие событий таймлайна:       {tl['coverage_pct']}% ({tl['covered_events_count']}/{tl['total_timeline_events']})")
    print(f"• РЕЗУЛЬТАТ ВЕРИФИКАЦИИ:             {'[УСПЕХ / PASS]' if summary['passed'] else '[ПРОВАЛ / FAIL]'}")
    print()

    # 1. ПОКРЫТИЕ СУЩНОСТЕЙ
    print("-" * 80)
    print("1. ПОКРЫТИЕ СУЩНОСТЕЙ GROUND TRUTH")
    print("-" * 80)
    if ec["missing_count"] > 0:
        print(f"⚠️ НЕ НАЙДЕНО В ГРАФЕ ({ec['missing_count']} сущностей):")
        for item in ec["missing_entities"]:
            print(f"   - [{item['category'].upper()}] {item['name']} (ID: {item['id']})")
    else:
        print("✅ Все сущности из Ground Truth найдены в графе!")

    if verbose and ec["found_count"] > 0:
        print("\nНАЙДЕННЫЕ СУЩНОСТИ (Verbose):")
        for item in ec["found_entities"]:
            e = item["entity"]
            matched_str = ", ".join([f"{n['node_id']} (по {n['matched_by']})" for n in item["matched_nodes"]])
            print(f"   - {e['name']} ({e['id']}) -> [{matched_str}]")
    print()

    # 2. СЛИПАНИЕ ОМОНИМОВ
    print("-" * 80)
    print("2. ПРОВЕРКА СЛИПАНИЯ ОМОНИМОВ (HOMONYM COLLAPSING)")
    print("-" * 80)
    if hc["collapsed_groups_count"] > 0:
        print(f"❌ КРИТИЧЕСКИЙ ПРОВАЛ: Найдено {hc['collapsed_groups_count']} схлопнувшихся групп омонимов!")
        for detail in hc["details"]:
            if detail["is_collapsed"]:
                grp_str = " VS ".join(detail["homonym_group"])
                print(f"   - [СЛИПЛИСЬ] {grp_str}")
                print(f"     Общие узлы графа: {detail['shared_collapsed_nodes']}")
    else:
        print("✅ Омонимы успешно разделены на разные узлы!")

    for detail in hc["details"]:
        if not detail["is_collapsed"]:
            grp_str = " VS ".join(detail["homonym_group"])
            print(f"   - [РАЗДЕЛЕНЫ] {grp_str} -> узлы: {detail['matched_graph_nodes']}")
    print()

    # 3. РАЗРЕШЕНИЕ АЛИАСОВ (РАСЩЕПЛЕНИЕ)
    print("-" * 80)
    print("3. РАЗРЕШЕНИЕ АЛИАСОВ НАОБОРОТ (ALIAS SPLITTING)")
    print("-" * 80)
    if as_rep["total_split_entities"] > 0:
        print(f"⚠️ Найдено {as_rep['total_split_entities']} сущностей, разъехавшихся на несколько узлов графа:")
        for split in as_rep["details"]:
            nodes_str = ", ".join([f"'{n['node_id']}'" for n in split["matched_nodes_details"]])
            print(f"   - {split['canonical_name']} ({split['entity_id']}) -> {split['split_node_count']} узла: [{nodes_str}]")
    else:
        print("✅ Расщепления алиасов на отдельные узлы не обнаружено.")
    print()

    # 4. ПОКРЫТИЕ СВЯЗЕЙ И ИЕРАРХИИ
    print("-" * 80)
    print("4. ПОКРЫТИЕ ИЕРАРХИИ И СВЯЗЕЙ")
    print("-" * 80)
    print(f"Дошедшие связи: {h_rep['covered_links']} из {h_rep['total_hierarchy_links']} ({h_rep['coverage_pct']}%)")
    print(f"Прямые рёбра между родителем и ребёнком: {h_rep['direct_edge_links']}")
    if h_rep.get("collapsed_links"):
        print(f"⚠️ Связей, где родитель и ребёнок слиплись в ОДИН узел: {h_rep['collapsed_links']}")
        for p_id, c_id, node in h_rep.get("collapsed_pairs", []):
            print(f"   - {p_id} и {c_id} -> общий узел '{node}'")
    print(f"Прослеживаемая глубина иерархии: {h_rep['max_traceable_level']} уровней")

    missing_hierarchy = [h for h in h_rep["details"] if not h["has_path"]]
    if missing_hierarchy:
        print("\n⚠️ УТЕРЯННЫЕ ИЕРАРХИЧЕСКИЕ СВЯЗИ:")
        for mh_link in missing_hierarchy:
            print(f"   - {mh_link['parent_id']} -/-> {mh_link['child_id']} ({mh_link['relation_type']})")
    print()

    # 5. MULTI-HOP СВЯЗНОСТЬ И ИЗОЛИРОВАННЫЕ УЗЛЫ
    print("-" * 80)
    print("5. MULTI-HOP СВЯЗНОСТЬ И ТОПОЛОГИЯ ГРАФА")
    print("-" * 80)
    print(f"Multi-hop (2-hop) достижимость: {mh['multihop_reachability_pct']}%")
    if mh["isolated_nodes_count"] > 0:
        print(f"⚠️ Изолированные узлы без связей ({mh['isolated_nodes_count']}): {mh['isolated_nodes']}")
    else:
        print("✅ Изолированных узлов не обнаружено.")

    if verbose and mh["sparse_nodes_count"] > 0:
        print(f"\nУзлы с единственной связью (degree=1, всего {mh['sparse_nodes_count']}):")
        for sn in mh["sparse_nodes"][:10]:
            print(f"   - '{sn['node']}' -> связен только с '{sn['connected_to']}'")
    print()

    # 6. ПОКРЫТИЕ СОБЫТИЙ ТАЙМЛАЙНА
    print("-" * 80)
    print("6. ПОКРЫТИЕ СОБЫТИЙ ТАЙМЛАЙНА")
    print("-" * 80)
    print(f"Отражено событий: {tl['covered_events_count']} из {tl['total_timeline_events']} ({tl['coverage_pct']}%)")
    for ev in tl["details"]:
        status = "✅ OK" if ev["is_covered"] else "❌ УТЕРЯНО / РАЗОРВАНО"
        print(f"   - [{status}] {ev['year']} {ev['title']} ({ev['event_id']}) — участников в графе: {ev['matched_nodes_count']}")
    print()

    # 7. ВКЛАД НАРРАТОРОВ
    print("-" * 80)
    print("7. ВКЛАД НАРРАТОРОВ В ГРАФ (ПО ТИПАМ ИСТОЧНИКОВ)")
    print("-" * 80)
    for n_type, stats in narr["narrator_stats"].items():
        print(f"   - {n_type.upper():<12}: {stats['unique_nodes']:>3} узлов, {stats['unique_edges']:>3} рёбер")
    print("=" * 80)


# ---------------------------------------------------------------------------
# Главная логика
# ---------------------------------------------------------------------------

def run_verification(
    working_dir: Path,
    ground_truth_path: Path,
    docs_dir: Path,
    fuzzy_threshold: float,
    verbose: bool,
) -> Dict[str, Any]:
    """
    Запускает полный комплекс верификации графа знаний против Ground Truth.
    """
    gt_data = load_ground_truth(ground_truth_path)
    graph = load_graph(working_dir)

    gt_entities = gt_data["all_entities"]
    graph_nodes = list(graph.nodes())

    # Сопоставление сущностей
    entity_to_nodes = match_entities_to_graph(gt_entities, graph_nodes, fuzzy_threshold)

    # Запуск всех 7 проверок
    ec = check_entity_coverage(gt_entities, entity_to_nodes)
    hc = check_homonym_collapsing(gt_entities, entity_to_nodes)
    as_rep = check_alias_splitting(gt_entities, entity_to_nodes)
    h_rep = check_hierarchy_coverage(gt_data.get("hierarchy", []), entity_to_nodes, graph)
    mh = check_multihop_and_connectivity(gt_data.get("hierarchy", []), entity_to_nodes, graph)
    tl = check_timeline_events(gt_data.get("timeline", []), entity_to_nodes, graph)
    narr = check_narrator_contributions(graph, docs_dir)

    # Критерии успешности (Gate condition):
    # - Покрытие сущностей >= 70%
    # - Отсутствие слипания омонимов (0 схлопнувшихся групп)
    # - Покрытие иерархии >= 50%
    passed = (
        ec["coverage_pct"] >= 70.0
        and hc["collapsed_groups_count"] == 0
        and h_rep["coverage_pct"] >= 50.0
    )

    report = {
        "summary": {
            "passed": passed,
            "entity_coverage_pct": ec["coverage_pct"],
            "homonyms_collapsed_count": hc["collapsed_groups_count"],
            "alias_splits_count": as_rep["total_split_entities"],
            "hierarchy_coverage_pct": h_rep["coverage_pct"],
            "multihop_reachability_pct": mh["multihop_reachability_pct"],
            "timeline_coverage_pct": tl["coverage_pct"],
        },
        "entity_coverage": ec,
        "homonym_collapsing": hc,
        "alias_splitting": as_rep,
        "hierarchy_coverage": h_rep,
        "multihop_connectivity": mh,
        "timeline_coverage": tl,
        "narrator_contributions": narr,
    }

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Автоматическая верификация графа знаний LightRAG против ground truth.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--working-dir",
        type=Path,
        default=DEFAULT_WORKING_DIR,
        help="Рабочая директория LightRAG с построенным графом (graph_chunk_entity_relation.graphml).",
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=DEFAULT_GROUND_TRUTH,
        help="Путь к файлу ground_truth.json.",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=DEFAULT_DOCS_DIR,
        help="Директория со сгенерированными .md документами корпуса.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Путь для сохранения полного отчёта в формате JSON.",
    )
    parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=DEFAULT_FUZZY_THRESHOLD,
        help="Порог нечёткого сравнения имён (от 0.0 до 1.0).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Печатать подробную информацию во все секции отчёта.",
    )
    return parser


def main() -> None:
    """Точка входа CLI."""
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        report = run_verification(
            working_dir=args.working_dir,
            ground_truth_path=args.ground_truth,
            docs_dir=args.docs_dir,
            fuzzy_threshold=args.fuzzy_threshold,
            verbose=args.verbose,
        )
    except FileNotFoundError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"НЕПРЕДВИДЕННАЯ ОШИБКА ВЕРИФИКАЦИИ: {exc}", file=sys.stderr)
        sys.exit(1)

    # Печать человекочитаемого отчёта
    print_human_report(report, verbose=args.verbose)

    # Выгрузка JSON отчёта при необходимости
    if args.json:
        try:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            with args.json.open("w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"📄 Машиночитаемый отчёт сохранён в '{args.json}'")
        except OSError as exc:
            print(f"⚠️ Не удалось сохранить JSON отчёт в '{args.json}': {exc}", file=sys.stderr)

    # Возврат кода завершения для автоматических пайплайнов / гейтов
    if report["summary"]["passed"]:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
